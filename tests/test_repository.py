"""GitLab integration tests use isolated settings and an in-memory HTTP provider."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from repository import LOGIN_ERROR, SESSION_ERROR, RepositoryError, RepositoryStore


CALLBACK = 'https://community.example/auth/gitlab/callback'
CLIENT_SECRET = 'fixture-client-secret-not-for-production'
ACCESS_TOKEN = 'fixture-access-token-never-persisted'
REFRESH_TOKEN = 'fixture-refresh-token-encrypted-only'
SESSION_TOKEN = 'fixture-community-cookie-not-in-database'


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='repository-integration-')
        self.root = Path(self.temp.name)
        self.requests = []
        self.response_user = {'id': 42, 'username': 'creator', 'name': '游戏创作者', 'state': 'active', 'bot': False}
        self.token_status, self.user_status = 200, 200
        self.token_body = {'access_token': ACCESS_TOKEN, 'refresh_token': REFRESH_TOKEN,
                           'token_type': 'Bearer', 'scope': 'read_user', 'expires_in': 7200}
        self.refresh_body = None
        self.change_during_request = False
        self.store = RepositoryStore(self.root, transport=httpx.MockTransport(self.provider))

    def tearDown(self):
        self.temp.cleanup()

    def provider(self, request):
        self.requests.append(request)
        if request.url.path.endswith('/oauth/token'):
            form = parse_qs(request.content.decode())
            if form.get('grant_type') == ['refresh_token'] and self.refresh_body is not None:
                return httpx.Response(self.token_status, json=self.refresh_body)
            return httpx.Response(self.token_status, json=self.token_body)
        if request.url.path.endswith('/oauth/revoke'):
            return httpx.Response(200, json={})
        if request.url.path.endswith('/api/v4/user'):
            if self.change_during_request:
                self.store.save({'oauth_enabled': False})
            return httpx.Response(self.user_status, json=self.response_user)
        self.fail('Unexpected provider endpoint: ' + request.url.path)

    def configure(self, **updates):
        payload = {'mode': 'external', 'url': 'https://gitlab.example/group-root/', 'oauth_enabled': True,
                   'oauth_client_id': 'application-id', 'oauth_client_secret': CLIENT_SECRET}
        payload.update(updates)
        return self.store.save(payload)

    async def finish(self, begin, **updates):
        values = {'code': 'authorization-code', 'state': begin['state'], 'cookie_state': begin['cookie_state'], 'redirect_uri': CALLBACK}
        values.update(updates)
        return await self.store.complete_oauth(**values)

    async def bound_session(self):
        self.configure()
        grant = await self.finish(self.store.begin_oauth(CALLBACK))
        self.store.bind_session(grant['grant_id'], SESSION_TOKEN)
        self.requests.clear()
        return grant

    def force_check(self, *, expired_token=False):
        with self.store.connection() as db:
            db.execute('UPDATE repository_oauth_sessions SET verified_at=0')
            if expired_token:
                row = db.execute('SELECT * FROM repository_oauth_sessions').fetchone()
                tokens = self.store._open_tokens(row['tokens'])
                tokens['expires_at'] = 0
                db.execute('UPDATE repository_oauth_sessions SET tokens=?', (self.store._seal(tokens),))

    def test_initial_configuration_prefers_managed_gitlab_and_waits_for_bootstrap(self):
        self.assertEqual(self.store.get_public(), {'mode': 'managed', 'url': 'http://localhost:18083', 'configured': True,
            'oauth_enabled': False, 'oauth_client_id': '', 'has_client_secret': False, 'managed_defaults': True})
        with self.assertRaises(RepositoryError):
            self.store.begin_oauth(CALLBACK)

    def test_managed_mode_uses_configured_environment_url(self):
        with patch.dict('os.environ', {'COMMUNITY_GITLAB_URL': 'https://GITLAB.EXAMPLE:443/gitlab/'}):
            public = self.store.save({'mode': 'managed'})
        self.assertEqual(public['url'], 'https://gitlab.example/gitlab')
        self.assertTrue(public['configured'])
        self.assertFalse(public['oauth_enabled'])

    def test_external_configuration_persists_encrypted_secret_and_explicit_clearing(self):
        public = self.configure()
        self.assertTrue(public['oauth_enabled'])
        self.assertTrue(public['has_client_secret'])
        self.assertNotIn(CLIENT_SECRET, json.dumps(public))
        self.assertNotIn(CLIENT_SECRET.encode(), self.store.path.read_bytes())
        reloaded = RepositoryStore(self.root)
        self.assertEqual(reloaded.get_public(), public)
        self.assertTrue(self.store.save({'oauth_client_secret': ''})['has_client_secret'])
        self.assertTrue(self.store.save({'url': 'https://another.example'})['has_client_secret'])
        public = self.store.save({'clear_client_secret': True})
        self.assertFalse(public['has_client_secret'])
        self.assertFalse(public['oauth_enabled'])
        with self.assertRaises(RepositoryError):
            self.store.save({'oauth_enabled': True})

    def test_enabling_oauth_requires_both_credentials(self):
        for missing in ['oauth_client_secret', 'oauth_client_id']:
            with self.subTest(missing=missing), self.assertRaises(RepositoryError):
                self.configure(**{missing: ''})
        self.assertFalse(self.store.get_public()['oauth_enabled'])

    def test_rejects_unsafe_urls_and_invalid_configuration_fields(self):
        bad_urls = ['javascript:alert(1)', '//gitlab.example', 'https://user:secret@gitlab.example',
                    'https://gitlab.example/?token=secret', 'https://gitlab.example/#fragment',
                    'https://gitlab.example?', 'https://gitlab.example#', 'https://gitlab.example:99999',
                    'https://gitlab.example\\@evil.example', 'https://gitlab.example/../other',
                    'https://gitlab.example/\n', 'https://gitlab.example:0', 'https://@gitlab.example']
        for address in bad_urls:
            with self.subTest(address=address), self.assertRaises(RepositoryError):
                self.store.save({'mode': 'external', 'url': address})
        for payload in [{'mode': []}, {'oauth_enabled': 'yes'}, {'clear_client_secret': 'yes'}, {'unexpected': 1}]:
            with self.subTest(payload=payload), self.assertRaises(RepositoryError):
                self.store.save(payload)

    async def test_pkce_flow_uses_exact_callback_read_user_scope_and_private_http_client(self):
        self.configure()
        begin = self.store.begin_oauth(CALLBACK, actor_id='existing-local-user')
        query = parse_qs(urlsplit(begin['authorization_url']).query)
        self.assertEqual(query['scope'], ['read_user'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['redirect_uri'], [CALLBACK])
        self.assertEqual(begin['expires_in'], 600)
        native_client = httpx.AsyncClient
        with patch('repository.httpx.AsyncClient', wraps=native_client) as factory:
            result = await self.finish(begin)
        self.assertFalse(factory.call_args.kwargs['trust_env'])
        self.assertFalse(factory.call_args.kwargs['follow_redirects'])
        self.assertEqual(len(self.requests), 2)
        posted = parse_qs(self.requests[0].content.decode())
        self.assertEqual(posted['redirect_uri'], [CALLBACK])
        self.assertEqual(posted['client_secret'], [CLIENT_SECRET])
        verifier = posted['code_verifier'][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(query['code_challenge'], [challenge])
        self.assertEqual(self.requests[1].headers['authorization'], 'Bearer ' + ACCESS_TOKEN)
        self.assertEqual(result['actor_id'], 'existing-local-user')
        self.assertEqual(result['username'], 'creator')
        self.assertEqual(result['external_id'], hashlib.sha256(b'https://gitlab.example/group-root').hexdigest() + ':42')
        data = self.store.path.read_bytes()
        for sensitive in (ACCESS_TOKEN, REFRESH_TOKEN, 'authorization-code', verifier, CLIENT_SECRET, begin['state'], result['grant_id']):
            self.assertNotIn(sensitive.encode(), data)
        with self.assertRaises(RepositoryError):
            await self.finish(begin)
        self.assertEqual(len(self.requests), 2)

    async def test_state_requires_matching_cookie_and_exact_callback(self):
        self.configure()
        begin = self.store.begin_oauth(CALLBACK)
        for cookie in ['', 'not-the-cookie', '雪' * 43]:
            with self.subTest(cookie=cookie), self.assertRaisesRegex(RepositoryError, LOGIN_ERROR):
                await self.finish(begin, cookie_state=cookie)
        with self.assertRaisesRegex(RepositoryError, LOGIN_ERROR):
            await self.finish(begin, redirect_uri=CALLBACK + '/')
        self.assertEqual(self.requests, [])
        with self.assertRaises(RepositoryError):
            await self.finish(begin)

    async def test_expired_state_and_saved_settings_invalidate_login(self):
        self.configure()
        begin = self.store.begin_oauth(CALLBACK)
        with self.store.connection() as db:
            db.execute('UPDATE repository_oauth_states SET expires=0')
        with self.assertRaises(RepositoryError):
            await self.finish(begin)
        begin = self.store.begin_oauth(CALLBACK)
        self.store.save({'oauth_client_id': 'replacement-id'})
        with self.assertRaises(RepositoryError):
            await self.finish(begin)
        self.assertEqual(self.requests, [])

    async def test_concurrent_callback_consumes_state_only_once(self):
        self.configure()
        begin = self.store.begin_oauth(CALLBACK)
        results = await asyncio.gather(self.finish(begin), self.finish(begin), return_exceptions=True)
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(sum(isinstance(value, RepositoryError) for value in results), 1)
        self.assertEqual(len(self.requests), 2)

    async def test_network_errors_and_provider_bodies_are_never_exposed_or_retried(self):
        self.configure()
        begin = self.store.begin_oauth(CALLBACK)
        self.token_status = 400
        self.token_body = {'error': CLIENT_SECRET + ACCESS_TOKEN + 'authorization-code'}
        with self.assertRaises(RepositoryError) as failure:
            await self.finish(begin)
        self.assertEqual(str(failure.exception), LOGIN_ERROR)
        self.assertEqual(len(self.requests), 1)
        with self.assertRaises(RepositoryError):
            await self.finish(begin)
        self.assertEqual(len(self.requests), 1)

    async def test_provider_redirects_are_rejected_without_following(self):
        self.configure()
        self.store.transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={'Location': 'https://untrusted.example'}))
        with self.assertRaisesRegex(RepositoryError, LOGIN_ERROR):
            await self.finish(self.store.begin_oauth(CALLBACK))

    async def test_inactive_bot_and_malformed_users_are_rejected(self):
        self.configure()
        variants = [{'state': 'blocked'}, {'bot': True}, {'bot': None}, {'id': True}, {'id': 0},
                    {'id': '42'}, {'username': 'wrong\nname'}, {'name': ''}]
        for update in variants:
            with self.subTest(update=update):
                self.response_user = {'id': 42, 'username': 'creator', 'name': 'User', 'state': 'active', 'bot': False, **update}
                with self.assertRaisesRegex(RepositoryError, LOGIN_ERROR):
                    await self.finish(self.store.begin_oauth(CALLBACK))

    async def test_instances_with_equal_user_id_have_distinct_external_identities(self):
        self.configure(url='https://first.example')
        first = await self.finish(self.store.begin_oauth(CALLBACK))
        self.store.save({'url': 'https://second.example'})
        second = await self.finish(self.store.begin_oauth(CALLBACK))
        self.assertNotEqual(first['external_id'], second['external_id'])
        self.assertIsNone(first['actor_id'])

    async def test_managed_public_authorization_and_private_container_exchange_are_separate(self):
        with patch.dict('os.environ', {'COMMUNITY_GITLAB_URL': 'http://localhost:18083',
                                      'COMMUNITY_GITLAB_INTERNAL_URL': 'http://gitlab:80'}):
            self.store.save({'mode': 'managed', 'oauth_enabled': True, 'oauth_client_id': 'application-id',
                             'oauth_client_secret': CLIENT_SECRET})
            begin = self.store.begin_oauth(CALLBACK)
            self.assertTrue(begin['authorization_url'].startswith('http://localhost:18083/oauth/authorize?'))
            result = await self.finish(begin)
        self.assertEqual([request.url.host for request in self.requests], ['gitlab', 'gitlab'])
        self.assertEqual([request.headers['host'] for request in self.requests], ['localhost:18083', 'localhost:18083'])
        self.assertEqual(result['external_id'], hashlib.sha256(b'http://localhost:18083').hexdigest() + ':42')
        self.assertNotIn('internal', json.dumps(self.store.get_public()))

    async def test_external_mode_ignores_managed_internal_url(self):
        self.configure()
        with patch.dict('os.environ', {'COMMUNITY_GITLAB_INTERNAL_URL': 'http://gitlab:80'}):
            await self.finish(self.store.begin_oauth(CALLBACK))
        self.assertTrue(all(request.url.host == 'gitlab.example' for request in self.requests))

    async def test_custom_ca_uses_verified_tls_context_and_missing_ca_is_safe(self):
        self.configure()
        context = object()
        native_client = httpx.AsyncClient
        with patch.dict('os.environ', {'COMMUNITY_GITLAB_CA_FILE': '/trusted/admin-ca.pem'}), \
                patch('repository.ssl.create_default_context', return_value=context) as make_context, \
                patch('repository.httpx.AsyncClient', wraps=native_client) as factory:
            await self.finish(self.store.begin_oauth(CALLBACK))
        make_context.assert_called_once_with(cafile='/trusted/admin-ca.pem')
        self.assertIs(factory.call_args.kwargs['verify'], context)
        with patch.dict('os.environ', {'COMMUNITY_GITLAB_CA_FILE': '/missing/admin-ca.pem'}):
            with self.assertRaisesRegex(RepositoryError, LOGIN_ERROR):
                await self.finish(self.store.begin_oauth(CALLBACK))

    async def test_settings_replaced_during_exchange_prevent_login(self):
        self.configure()
        self.change_during_request = True
        with self.assertRaisesRegex(RepositoryError, LOGIN_ERROR):
            await self.finish(self.store.begin_oauth(CALLBACK))

    async def test_grant_binding_is_one_use_encrypted_and_cache_preserves_local_sessions(self):
        grant = await self.bound_session()
        with self.assertRaises(RepositoryError):
            self.store.bind_session(grant['grant_id'], 'another-session-cookie')
        self.assertTrue(await self.store.validate_session(SESSION_TOKEN))
        self.assertTrue(await self.store.validate_session('independent-local-cookie'))
        self.assertEqual(self.requests, [])
        data = self.store.path.read_bytes()
        for sensitive in (ACCESS_TOKEN, REFRESH_TOKEN, SESSION_TOKEN, grant['grant_id']):
            self.assertNotIn(sensitive.encode(), data)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_grants').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_sessions').fetchone()[0], 1)

    async def test_expired_unbound_grant_and_discarded_link_grant_cannot_bind(self):
        self.configure()
        grant = await self.finish(self.store.begin_oauth(CALLBACK))
        with self.store.connection() as db:
            db.execute('UPDATE repository_oauth_grants SET expires=0')
        with self.assertRaises(RepositoryError):
            self.store.bind_session(grant['grant_id'], SESSION_TOKEN)
        link = await self.finish(self.store.begin_oauth(CALLBACK, actor_id='local-account'))
        self.store.discard_grant(link['grant_id'])
        with self.assertRaises(RepositoryError):
            self.store.bind_session(link['grant_id'], SESSION_TOKEN)
        link = await self.finish(self.store.begin_oauth(CALLBACK, actor_id='local-account'))
        with self.assertRaises(RepositoryError):
            self.store.bind_session(link['grant_id'], SESSION_TOKEN)

    async def test_disabled_gitlab_user_rejects_cached_identity_after_cache_deadline(self):
        await self.bound_session()
        self.response_user['state'] = 'blocked'
        self.assertTrue(await self.store.validate_session(SESSION_TOKEN))
        self.force_check()
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(await self.store.validate_session('local-cookie-still-valid'))

    async def test_identity_change_rejects_session_without_becoming_local_login(self):
        await self.bound_session()
        self.force_check()
        self.response_user['id'] = 999
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))

    async def test_config_change_invalidates_binding_without_sending_tokens_to_new_host(self):
        await self.bound_session()
        self.store.save({'url': 'https://replacement.example'})
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertEqual(self.requests, [])

    async def test_expired_session_is_removed_and_rejected_even_with_fresh_cached_identity(self):
        await self.bound_session()
        with self.store.connection() as db:
            db.execute('UPDATE repository_oauth_sessions SET expires=0')
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertEqual(self.requests, [])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_sessions').fetchone()[0], 0)

    async def test_refresh_is_serialized_and_rotated_tokens_are_persisted_once(self):
        await self.bound_session()
        self.force_check(expired_token=True)
        self.refresh_body = {'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                             'token_type': 'Bearer', 'scope': 'read_user', 'expires_in': 7200}
        self.assertEqual(await asyncio.gather(self.store.validate_session(SESSION_TOKEN), self.store.validate_session(SESSION_TOKEN)), [True, True])
        refreshes = [request for request in self.requests if request.url.path.endswith('/oauth/token')]
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(parse_qs(refreshes[0].content.decode())['refresh_token'], [REFRESH_TOKEN])
        self.assertEqual(self.requests[-1].headers['authorization'], 'Bearer rotated-access')
        with self.store.connection() as db:
            row = db.execute('SELECT tokens FROM repository_oauth_sessions').fetchone()
        self.assertEqual(self.store._open_tokens(row['tokens'])['refresh_token'], 'rotated-refresh')
        self.assertNotIn(b'rotated-access', self.store.path.read_bytes())

    async def test_revoked_refresh_invalidates_only_this_session(self):
        await self.bound_session()
        self.force_check(expired_token=True)
        self.token_status = 400
        self.refresh_body = {'error': 'invalid_grant', 'secret': REFRESH_TOKEN}
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        self.assertTrue(await self.store.validate_session('local-login-cookie'))
        self.assertEqual(len(self.requests), 1)

    async def test_access_unauthorized_attempts_one_refresh_then_checks_same_user(self):
        await self.bound_session()
        self.force_check()
        self.refresh_body = {'access_token': 'renewed-after-401', 'refresh_token': 'new-refresh',
                             'token_type': 'Bearer', 'scope': 'read_user', 'expires_in': 7200}
        original = self.provider

        def provider(request):
            if request.url.path.endswith('/api/v4/user') and request.headers.get('authorization') == 'Bearer ' + ACCESS_TOKEN:
                self.requests.append(request)
                return httpx.Response(401, json={'message': 'Unauthorized'})
            return original(request)

        self.store.transport = httpx.MockTransport(provider)
        self.assertTrue(await self.store.validate_session(SESSION_TOKEN))
        self.assertEqual([request.url.path.split('/')[-1] for request in self.requests], ['user', 'token', 'user'])

    async def test_provider_outage_fails_closed_without_discarding_rotated_credentials(self):
        await self.bound_session()
        self.force_check(expired_token=True)
        self.refresh_body = {'access_token': 'fresh-access', 'refresh_token': 'fresh-refresh',
                             'token_type': 'Bearer', 'scope': 'read_user', 'expires_in': 7200}
        self.user_status = 503
        with self.assertRaisesRegex(RepositoryError, SESSION_ERROR):
            await self.store.validate_session(SESSION_TOKEN)
        with self.store.connection() as db:
            row = db.execute('SELECT tokens FROM repository_oauth_sessions').fetchone()
        self.assertEqual(self.store._open_tokens(row['tokens'])['refresh_token'], 'fresh-refresh')
        self.user_status = 200
        self.assertTrue(await self.store.validate_session(SESSION_TOKEN))
        self.assertEqual(sum(request.url.path.endswith('/oauth/token') for request in self.requests), 1)

    async def test_logout_revokes_remotely_and_clears_local_binding_even_if_provider_fails(self):
        await self.bound_session()
        native_client = httpx.AsyncClient
        with patch('repository.httpx.AsyncClient', wraps=native_client) as factory:
            await self.store.revoke_session(SESSION_TOKEN)
        self.assertEqual(self.requests[-1].url.path, '/group-root/oauth/revoke')
        self.assertEqual(parse_qs(self.requests[-1].content.decode())['token'], [ACCESS_TOKEN])
        self.assertEqual(factory.call_args.kwargs['timeout'].read, 3)
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))
        await self.store.revoke_session(SESSION_TOKEN)
        self.assertEqual(len(self.requests), 1)

    async def test_logout_is_not_blocked_by_remote_failure_or_changed_configuration(self):
        await self.bound_session()

        def unavailable(request):
            raise httpx.ConnectError(ACCESS_TOKEN + CLIENT_SECRET, request=request)

        self.store.transport = httpx.MockTransport(unavailable)
        await self.store.revoke_session(SESSION_TOKEN)
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))

    async def test_revoke_after_configuration_change_does_not_contact_replacement_provider(self):
        await self.bound_session()
        self.store.save({'url': 'https://replacement.example'})
        await self.store.revoke_session(SESSION_TOKEN)
        self.assertEqual(self.requests, [])
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))

    async def test_identical_settings_preserve_live_and_pending_logins(self):
        await self.bound_session()
        begin = self.store.begin_oauth(CALLBACK)
        with self.store.connection() as db:
            revision = self.store._config(db)['revision']
        self.configure()  # Same plaintext secret is encrypted with a new nonce.
        with self.store.connection() as db:
            self.assertEqual(self.store._config(db)['revision'], revision)
        self.assertTrue(await self.store.validate_session(SESSION_TOKEN))
        await self.finish(begin)

    async def test_refresh_across_store_instances_rotates_only_once(self):
        await self.bound_session()
        self.force_check(expired_token=True)
        self.refresh_body = {'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                             'token_type': 'Bearer', 'scope': 'read_user', 'expires_in': 7200}
        async def delayed(request):
            await asyncio.sleep(0.05)
            return self.provider(request)
        self.store.transport = httpx.MockTransport(delayed)
        other = RepositoryStore(self.root, transport=httpx.MockTransport(delayed))
        self.assertEqual(await asyncio.gather(self.store.validate_session(SESSION_TOKEN), other.validate_session(SESSION_TOKEN)), [True, True])
        self.assertEqual(sum(r.url.path.endswith('/oauth/token') for r in self.requests), 1)

    async def test_broken_client_configuration_does_not_destroy_refresh_grant(self):
        await self.bound_session()
        self.force_check(expired_token=True)
        self.token_status, self.refresh_body = 401, {'error': 'invalid_client'}
        with self.assertRaisesRegex(RepositoryError, SESSION_ERROR):
            await self.store.validate_session(SESSION_TOKEN)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_oauth_sessions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM repository_revoked_sessions').fetchone()[0], 0)

    async def test_logout_does_not_revoke_token_shared_with_new_login(self):
        await self.bound_session()
        grant = await self.finish(self.store.begin_oauth(CALLBACK))
        self.requests.clear()
        await self.store.revoke_session(SESSION_TOKEN)
        self.assertEqual(self.requests, [])
        self.store.bind_session(grant['grant_id'], 'replacement-cookie')
        self.assertTrue(await self.store.validate_session('replacement-cookie'))

    async def test_pat_login_is_encrypted_revalidated_and_logout_does_not_revoke_pat(self):
        self.configure()
        self.response_user['is_admin'] = True
        identity = await self.store.complete_pat('fixture-personal-access-token', 'creator')
        self.assertTrue(identity['admin'])
        self.store.bind_session(identity['grant_id'], SESSION_TOKEN)
        self.assertNotIn(b'fixture-personal-access-token', self.store.path.read_bytes())
        with self.assertRaisesRegex(RepositoryError, '不一致'):
            await self.store.complete_pat('fixture-personal-access-token', 'someone-else')
        self.force_check()
        self.assertTrue(await self.store.validate_session(SESSION_TOKEN))
        self.requests.clear()
        await self.store.revoke_session(SESSION_TOKEN)
        self.assertEqual(self.requests, [])
        self.assertFalse(await self.store.validate_session(SESSION_TOKEN))


if __name__ == '__main__':
    unittest.main()

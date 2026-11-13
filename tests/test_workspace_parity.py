"""HTTP contracts for restored workspace and GitLab SSO; no external service calls."""
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from portal.auth import COOKIE

ORIGIN = 'http://localhost:18082'


class WorkspaceParity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='community-workspace-test-')
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'COMMUNITY_DATA_DIR': self.temp.name,
            'COMMUNITY_ENABLE_ANIMATION': 'false', 'COMMUNITY_RUN_QUEUE': 'false', 'APP_ORIGIN': ORIGIN})
        self.env.start()
        self.addCleanup(self.env.stop)
        application = importlib.import_module('app')
        self.application = application.create_app(Path(self.temp.name), enable_animation=False, run_queue=False)
        self.client = TestClient(self.application, base_url=ORIGIN)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.calls = []
        self.provider_user_status = 200
        self.provider_user_changes = {}

    def login(self):
        result = self.client.post('/api/login', json={'username': 'admin', 'password': '12345678'})
        self.assertEqual(result.status_code, 200)
        self.session = result.json()
        self.headers = {'X-Community-CSRF': self.session['csrf'], 'Origin': ORIGIN}
        return self.session

    def configure_oauth(self):
        self.login()
        result = self.client.put('/api/repository', headers=self.headers, json={
            'mode': 'external', 'url': 'https://gitlab.example.test', 'oauth_enabled': True,
            'oauth_client_id': 'public-client', 'oauth_client_secret': 'test-only-secret-value'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn('test-only-secret-value', result.text)
        self.application.state.repository.transport = httpx.MockTransport(self.provider)
        return result.json()

    def provider(self, request):
        self.calls.append((request.method, str(request.url)))
        if request.url.path == '/oauth/token':
            return httpx.Response(200, json={'access_token': 'test-private-token', 'token_type': 'Bearer',
                'expires_in': 3600, 'created_at': 1, 'refresh_token': 'unused-refresh'})
        if request.url.path == '/api/v4/user':
            return httpx.Response(self.provider_user_status, json={'id': 42, 'username': 'admin', 'name': 'GitLab Member',
                'state': 'active', 'locked': False, 'bot': False, 'is_admin': True, **self.provider_user_changes})
        if request.url.path == '/oauth/revoke':
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def callback(self, authorization_url):
        state = parse_qs(urlsplit(authorization_url).query)['state'][0]
        return self.client.get('/auth/gitlab/callback', params={'state': state, 'code': 'single-use-code'},
            follow_redirects=False)

    def oauth_session(self):
        self.configure_oauth()
        self.client.post('/api/logout', headers=self.headers, json={})
        start = self.client.get('/auth/gitlab/start', follow_redirects=False)
        result = self.callback(start.headers['location'])
        self.assertEqual(result.status_code, 303, result.text)
        session = self.client.get('/api/session').json()
        self.calls.clear()
        return self.client.cookies.get(COOKIE), session

    def force_external_revalidation(self):
        with self.application.state.repository.connection() as db:
            db.execute('UPDATE repository_oauth_sessions SET verified_at=0')

    def test_workspace_has_nine_modules_and_repository_requires_admin_csrf(self):
        self.assertEqual(self.client.get('/api/workspace').status_code, 401)
        self.assertEqual(self.client.get('/gitlab/', follow_redirects=False).status_code, 303)
        self.login()
        result = self.client.get('/api/workspace').json()
        self.assertEqual(set(result['modules']), {'gitlab', 'knowledge', 'animation', 'image',
            'speech', 'music', 'model3d', 'narrative', 'effects'})
        self.assertEqual(result['modules']['narrative']['status'], 'ready')
        self.assertEqual(result['modules']['gitlab']['status'], 'ready')
        self.assertEqual(self.client.put('/api/repository', json={'mode': 'external', 'url': 'https://gitlab.test'}).status_code, 403)
        good = self.client.put('/api/repository', headers=self.headers,
            json={'mode': 'external', 'url': 'https://gitlab.example.test'})
        self.assertEqual(good.status_code, 200)
        redirect = self.client.get('/gitlab/', follow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertEqual(redirect.headers['location'].rstrip('/'), 'https://gitlab.example.test')
        self.application.state.auth.create_user('member', 'member-pass-123')
        self.client.post('/api/login', json={'username': 'member', 'password': 'member-pass-123'})
        session = self.client.get('/api/session').json()
        denied = self.client.put('/api/repository', headers={'X-Community-CSRF': session['csrf']},
            json={'mode': 'disabled'})
        self.assertEqual(denied.status_code, 403)

    def test_dashboard_theme_is_forwarded_only_to_managed_gitlab(self):
        self.login()
        enabled = self.client.put('/api/repository', headers=self.headers,
            json={'mode': 'managed', 'use_managed_defaults': True})
        self.assertEqual(enabled.status_code, 200)
        base = enabled.json()['url']
        for theme in ('light', 'dark'):
            response = self.client.get('/gitlab/', params={'fantasy_theme': theme}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            target = urlsplit(response.headers['location'])
            self.assertEqual(target.netloc, urlsplit(base).netloc)
            self.assertEqual(target.path, '/dashboard/projects')
            self.assertEqual(parse_qs(target.query), {'fantasy_theme': [theme]})
        invalid = self.client.get('/gitlab/', params={'fantasy_theme': 'https://untrusted.example'}, follow_redirects=False)
        self.assertEqual(invalid.headers['location'], base)
        self.client.put('/api/repository', headers=self.headers,
            json={'mode': 'external', 'url': 'https://external-gitlab.example', 'oauth_enabled': False})
        external = self.client.get('/gitlab/?fantasy_theme=dark', follow_redirects=False)
        self.assertEqual(external.headers['location'], 'https://external-gitlab.example')

    def test_oauth_login_does_not_inherit_same_named_admin_or_replay_code(self):
        self.configure_oauth()
        self.assertTrue(self.client.get('/api/auth/options').json()['gitlab_login'])
        self.client.post('/api/logout', headers=self.headers, json={})
        start = self.client.get('/auth/gitlab/start', follow_redirects=False)
        self.assertEqual(start.status_code, 303)
        authorization = start.headers['location']
        query = parse_qs(urlsplit(authorization).query)
        self.assertEqual(query['redirect_uri'], [ORIGIN + '/auth/gitlab/callback'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        result = self.callback(authorization)
        self.assertEqual(result.status_code, 303, result.text)
        user = self.client.get('/api/session').json()['user']
        # Numeric identity remains separate from the same-named local account;
        # its administrator role now comes from verified GitLab is_admin.
        self.assertTrue(user['admin'])
        self.assertNotEqual(user['id'], self.session['user']['id'])
        calls = len(self.calls)
        self.assertEqual(self.callback(authorization).status_code, 400)
        self.assertEqual(len(self.calls), calls)
        self.assertNotIn('test-private-token', result.text)

    def test_explicit_link_preserves_local_user_id_and_requires_current_session(self):
        self.configure_oauth()
        denied = self.client.post('/auth/gitlab/link-start', json={})
        self.assertEqual(denied.status_code, 403)
        start = self.client.post('/auth/gitlab/link-start', headers=self.headers, json={})
        self.assertEqual(start.status_code, 200)
        self.assertEqual(self.callback(start.json()['authorization_url']).status_code, 303)
        user = self.client.get('/api/session').json()['user']
        self.assertEqual(user['id'], self.session['user']['id'])
        self.assertTrue(user['admin'])
        self.assertEqual(user['auth_provider'], 'local')

    def test_link_cannot_attach_to_a_different_signed_in_account(self):
        self.configure_oauth()
        start = self.client.post('/auth/gitlab/link-start', headers=self.headers, json={})
        self.application.state.auth.create_user('second', 'second-pass-123')
        self.client.post('/api/login', json={'username': 'second', 'password': 'second-pass-123'})
        result = self.callback(start.json()['authorization_url'])
        self.assertEqual(result.status_code, 403)

    def test_oauth_uses_configured_origin_not_untrusted_host(self):
        self.configure_oauth()
        redirect = self.client.get('/auth/gitlab/start?next=https://attacker.example/&redirect_uri=https://attacker.example/',
            headers={'Host': 'attacker.example', 'X-Forwarded-Host': 'another.example'}, follow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertEqual(redirect.headers['location'], ORIGIN + '/auth/gitlab/start')
        self.assertNotIn('set-cookie', redirect.headers)
        with self.application.state.repository.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_states').fetchone()[0], 0)
        start = self.client.get(redirect.headers['location'], follow_redirects=False)
        query = parse_qs(urlsplit(start.headers['location']).query)
        self.assertEqual(query['redirect_uri'], [ORIGIN + '/auth/gitlab/callback'])

    def test_alias_redirects_before_state_cookie_then_completes_canonical_login(self):
        self.configure_oauth()
        self.client.post('/api/logout', headers=self.headers, json={})
        for origin in ('http://127.0.0.1:18082', 'http://[::1]:18082', 'http://localhost:18099'):
            with self.subTest(origin=origin):
                start = self.client.get(origin + '/auth/gitlab/start', follow_redirects=False)
                self.assertEqual(start.status_code, 303)
                self.assertEqual(start.headers['location'], ORIGIN + '/auth/gitlab/start')
                self.assertNotIn('set-cookie', start.headers)
                self.assertEqual(start.headers['cache-control'], 'no-store')
                with self.application.state.repository.connection() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_states').fetchone()[0], 0)
        canonical = self.client.get(start.headers['location'], follow_redirects=False)
        self.assertIn('fantasy_gitlab_state=', canonical.headers['set-cookie'])
        self.assertNotIn('domain=', canonical.headers['set-cookie'].lower())
        result = self.callback(canonical.headers['location'])
        self.assertEqual(result.status_code, 303, result.text)
        self.assertEqual(self.client.get('/api/session').status_code, 200)

    def test_configured_ip_origin_accepts_login_from_localhost_alias(self):
        self.configure_oauth()
        self.client.post('/api/logout', headers=self.headers, json={})
        ip_origin = 'http://127.0.0.1:18082'
        with patch.dict(os.environ, {'APP_ORIGIN': ip_origin}):
            redirect = self.client.get('/auth/gitlab/start', follow_redirects=False)
            self.assertEqual(redirect.headers['location'], ip_origin + '/auth/gitlab/start')
            self.assertNotIn('set-cookie', redirect.headers)
            start = self.client.get(redirect.headers['location'], follow_redirects=False)
            query = parse_qs(urlsplit(start.headers['location']).query)
            self.assertEqual(query['redirect_uri'], [ip_origin + '/auth/gitlab/callback'])
            result = self.client.get(ip_origin + '/auth/gitlab/callback',
                params={'state': query['state'][0], 'code': 'single-use-code'}, follow_redirects=False)
            self.assertEqual(result.status_code, 303, result.text)
            self.assertEqual(self.client.get(ip_origin + '/api/session').status_code, 200)

    def test_link_on_alias_does_not_replay_authenticated_post_across_hosts(self):
        self.configure_oauth()
        alias = TestClient(self.application, base_url='http://127.0.0.1:18082')
        self.addCleanup(alias.close)
        login = alias.post('/api/login', json={'username': 'admin', 'password': '12345678'})
        self.assertEqual(login.status_code, 200)
        response = alias.post('/auth/gitlab/link-start', json={}, follow_redirects=False,
            headers={'X-Community-CSRF': login.json()['csrf'], 'Origin': 'http://127.0.0.1:18082'})
        self.assertEqual(response.status_code, 409)
        self.assertIn(ORIGIN + '/login', response.json()['detail'])
        self.assertNotIn('location', response.headers)
        self.assertNotIn('set-cookie', response.headers)
        with self.application.state.repository.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_states').fetchone()[0], 0)

    def test_default_ports_and_tls_proxy_do_not_cause_login_redirect_loop(self):
        self.configure_oauth()
        for configured, incoming in (('https://community.example:443', 'http://community.example'),
                                     ('http://community.example:80', 'http://community.example')):
            with self.subTest(configured=configured), patch.dict(os.environ, {'APP_ORIGIN': configured}):
                response = self.client.get(incoming + '/auth/gitlab/start', follow_redirects=False)
                self.assertEqual(urlsplit(response.headers['location']).hostname, 'gitlab.example.test')
                self.assertIn('fantasy_gitlab_state=', response.headers['set-cookie'])

    def test_external_revalidation_failure_clears_local_session_and_cookie(self):
        token, session = self.oauth_session()
        self.provider_user_changes = {'state': 'blocked'}
        self.force_external_revalidation()
        response = self.client.get('/api/workspace')
        self.assertEqual(response.status_code, 401, response.text)
        self.assertIsNone(self.application.state.auth.get_session(token))
        self.assertNotIn(COOKIE, self.client.cookies)
        self.assertEqual(self.calls, [('GET', 'https://gitlab.example.test/api/v4/user')])
        with self.application.state.auth.connection() as db:
            account = db.execute('SELECT active FROM accounts WHERE id=?', (session['user']['id'],)).fetchone()
        self.assertTrue(account['active'])

    def test_temporary_provider_outage_returns_503_without_revoking_account_or_session(self):
        token, session = self.oauth_session()
        self.provider_user_status = 503
        self.force_external_revalidation()
        response = self.client.get('/api/workspace')
        self.assertEqual(response.status_code, 503, response.text)
        retained = self.application.state.auth.get_session(token)
        self.assertEqual(retained['user']['id'], session['user']['id'])
        self.assertEqual(self.client.cookies.get(COOKIE), token)
        self.assertNotIn('test-private-token', response.text)
        self.provider_user_status = 200
        self.assertEqual(self.client.get('/api/workspace').status_code, 200)

    def test_login_and_explicit_logout_remain_available_during_gitlab_outage(self):
        token, session = self.oauth_session()
        self.provider_user_status = 503
        self.force_external_revalidation()
        self.assertEqual(self.client.get('/login').status_code, 200)
        self.assertEqual(self.client.get('/logout').status_code, 200)
        self.assertEqual(self.client.get('/api/session').status_code, 503)
        self.assertEqual(self.client.get('/api/workspace').status_code, 503)
        context = self.client.get('/api/logout-context')
        self.assertEqual(context.status_code, 200)
        self.assertEqual(set(context.json()), {'user', 'csrf'})
        response = self.client.post('/api/logout', headers={
            'X-Community-CSRF': context.json()['csrf'], 'Origin': ORIGIN}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.application.state.auth.get_session(token))

    def test_logout_calls_gitlab_revoke_and_removes_local_binding(self):
        token, session = self.oauth_session()
        response = self.client.post('/api/logout', headers={
            'X-Community-CSRF': session['csrf'], 'Origin': ORIGIN}, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(('POST', 'https://gitlab.example.test/oauth/revoke'), self.calls)
        self.assertIsNone(self.application.state.auth.get_session(token))
        self.assertNotIn(COOKIE, self.client.cookies)
        with self.application.state.repository.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_sessions').fetchone()[0], 0)

    def test_linking_same_identity_preserves_existing_gitlab_session_validation(self):
        token, session = self.oauth_session()
        start = self.client.post('/auth/gitlab/link-start', headers={
            'X-Community-CSRF': session['csrf'], 'Origin': ORIGIN}, json={})
        self.assertEqual(start.status_code, 200, start.text)
        response = self.callback(start.json()['authorization_url'])
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(self.client.cookies.get(COOKIE), token)
        with self.application.state.repository.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_sessions').fetchone()[0], 1)
        self.provider_user_changes = {'state': 'blocked'}
        self.force_external_revalidation()
        self.assertEqual(self.client.get('/api/workspace').status_code, 401)

    def test_link_callback_revalidates_existing_session_even_when_new_identity_is_valid(self):
        token, session = self.oauth_session()
        start = self.client.post('/auth/gitlab/link-start', headers={
            'X-Community-CSRF': session['csrf'], 'Origin': ORIGIN}, json={})
        self.assertEqual(start.status_code, 200, start.text)
        self.force_external_revalidation()
        native_provider = self.provider
        identity_reads = 0

        def provider(request):
            nonlocal identity_reads
            if request.url.path == '/api/v4/user':
                identity_reads += 1
                # New OAuth grant is valid; the old session subsequently fails.
                if identity_reads == 2:
                    self.provider_user_changes = {'state': 'blocked'}
            return native_provider(request)

        self.application.state.repository.transport = httpx.MockTransport(provider)
        result = self.callback(start.json()['authorization_url'])
        self.assertEqual(result.status_code, 401, result.text)
        self.assertIsNone(self.application.state.auth.get_session(token))
        self.assertNotIn(COOKIE, self.client.cookies)
        self.assertNotIn('fantasy_gitlab_state', self.client.cookies)
        with self.application.state.repository.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_oauth_grants').fetchone()[0], 0)


    def test_pat_login_keeps_bound_uuid_and_tracks_upstream_admin_and_revocation(self):
        self.configure_oauth()
        original = self.session['user']['id']
        repository, auth = self.application.state.repository, self.application.state.auth
        import hashlib
        external = hashlib.sha256(b'https://gitlab.example.test').hexdigest() + ':42'
        auth.link_external(original, 'gitlab', external)
        result = self.client.post('/api/login', json={'credential_type': 'gitlab_pat', 'username': 'admin', 'token': 'fixture-pat'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['user']['id'], original)
        self.assertEqual(result.json()['user']['auth_provider'], 'gitlab')
        self.assertTrue(result.json()['user']['admin'])
        self.assertEqual(self.client.post('/api/login', json={'username': 'admin', 'password': '12345678'}).status_code, 400)
        management = self.client.get('/api/users').json()['management']
        self.assertEqual(management['url'], 'https://gitlab.example.test/admin/users')
        self.provider_user_changes['is_admin'] = False
        with repository.connection() as db:
            db.execute('UPDATE repository_oauth_sessions SET verified_at=0')
        self.assertFalse(self.client.get('/api/session').json()['user']['admin'])
        self.assertEqual(self.client.get('/api/users').status_code, 403)
        self.provider_user_status = 401
        with repository.connection() as db:
            db.execute('UPDATE repository_oauth_sessions SET verified_at=0')
        self.assertEqual(self.client.get('/api/session').status_code, 401)
        self.assertNotIn(COOKIE, self.client.cookies)


if __name__ == '__main__':
    unittest.main()

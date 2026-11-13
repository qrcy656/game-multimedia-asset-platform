"""Bundled GitLab defaults and signed one-time account bootstrap, without GitLab."""
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from portal.auth import AuthError, AuthStore
from repository import RepositoryStore


class ManagedGitLabBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='managed-gitlab-test-')
        self.root = Path(self.temp.name)
        self.bootstrap = self.root / 'managed-oauth.json'
        self.env = patch.dict(os.environ, {
            'COMMUNITY_IDENTITY_SECRET': 'fixture-installation-signing-key-32-characters',
            'COMMUNITY_GITLAB_URL': 'http://localhost:18083', 'APP_ORIGIN': 'http://localhost:18082',
            'COMMUNITY_REPOSITORY_DEFAULT': 'managed', 'COMMUNITY_GITLAB_ADMIN_ACCOUNT_ID': '',
            'COMMUNITY_GITLAB_BOOTSTRAP_FILE': str(self.bootstrap)})
        self.env.start()
        self.auth = AuthStore(self.root)
        self.store = RepositoryStore(self.root, auth=self.auth)
        self.payload = {'version': 1, 'url': 'http://localhost:18083',
            'redirect_uri': 'http://localhost:18082/auth/gitlab/callback', 'client_id': 'fixture-client',
            'client_secret': 'fixture-oauth-secret', 'root_user_id': 1, 'installation_id': 'fixture-installation'}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def publish(self, **updates):
        payload = json.dumps({**self.payload, **updates}, separators=(',', ':'))
        signature = hmac.new(os.environ['COMMUNITY_IDENTITY_SECRET'].encode(), payload.encode(), hashlib.sha256).hexdigest()
        self.bootstrap.write_text(json.dumps({'payload': payload, 'signature': signature}), encoding='utf-8')

    def test_fresh_install_prefers_managed_gitlab_while_local_login_remains_available(self):
        public = self.store.get_public()
        self.assertEqual(public['mode'], 'managed')
        self.assertTrue(public['configured'])
        self.assertFalse(public['oauth_enabled'])
        options = self.store.auth_options()
        self.assertEqual(options['preferred_login'], 'gitlab')
        self.assertTrue(options['gitlab_initializing'])
        self.assertTrue(options['local_login'])
        self.auth.login('admin', '12345678')

    def test_signed_oauth_bootstrap_preserves_initial_admin_uuid_and_encrypts_secret(self):
        original = self.auth.list_users()[0]
        self.publish()
        public = self.store.get_public()
        self.assertTrue(public['oauth_enabled'])
        self.assertNotIn(self.payload['client_secret'], json.dumps(public))
        self.assertNotIn(self.payload['client_secret'].encode(), self.store.path.read_bytes())
        external = hashlib.sha256(self.payload['url'].encode()).hexdigest() + ':1'
        user = self.auth.get_or_create_external('gitlab', external, 'Administrator', 'root')
        self.assertEqual(user['id'], original['id'])
        self.assertTrue(user['admin'])
        self.auth.login('admin', '12345678')
        with self.store.connection() as db:
            revision = self.store._config(db)['revision']
        self.store.get_public()
        with self.store.connection() as db:
            self.assertEqual(self.store._config(db)['revision'], revision)

    def test_bootstrap_failure_keeps_local_login_and_does_not_install_credentials(self):
        self.publish()
        envelope = json.loads(self.bootstrap.read_text())
        envelope['signature'] = '0' * 64
        self.bootstrap.write_text(json.dumps(envelope))
        self.assertFalse(self.store.get_public()['oauth_enabled'])
        self.assertTrue(self.store.auth_options()['gitlab_setup_error'])
        self.auth.login('admin', '12345678')

    def test_wrong_url_or_callback_never_enables_oauth(self):
        for updates in ({'url': 'https://different.example'}, {'redirect_uri': 'https://different.example/auth/gitlab/callback'}):
            with self.subTest(updates=updates):
                self.publish(**updates)
                self.assertFalse(self.store.get_public()['oauth_enabled'])

    def test_manual_disable_and_external_configuration_are_preserved(self):
        self.publish()
        self.store.get_public()
        self.store.save({'mode': 'disabled'})
        self.publish(client_id='new-client')
        self.assertEqual(RepositoryStore(self.root, auth=self.auth).get_public()['mode'], 'disabled')
        self.store.save({'mode': 'external', 'url': 'https://existing.example', 'oauth_enabled': False})
        self.assertEqual(RepositoryStore(self.root).get_public()['url'], 'https://existing.example')
        restored = self.store.save({'mode': 'managed', 'use_managed_defaults': True})
        self.assertTrue(restored['managed_defaults'])
        self.assertTrue(restored['oauth_enabled'])
        self.assertEqual(restored['oauth_client_id'], 'new-client')

    def test_existing_account_requires_explicit_uuid_and_never_matches_username(self):
        original = self.auth.list_users()[0]
        with self.auth.connection() as db:
            db.execute('DELETE FROM installation_identity')
        self.publish()
        self.assertTrue(self.store.get_public()['oauth_enabled'])
        with self.auth.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_identities').fetchone()[0], 0)
        # A newly configured explicit UUID is applied after restart even when
        # OAuth itself was already initialized; no credentials need rotation.
        with patch.dict(os.environ, {'COMMUNITY_GITLAB_ADMIN_ACCOUNT_ID': original['id']}):
            RepositoryStore(self.root, auth=self.auth).get_public()
        with self.auth.connection() as db:
            self.assertEqual(db.execute('SELECT user_id FROM external_identities').fetchone()[0], original['id'])

    def test_existing_conflicting_identity_is_never_reassigned(self):
        original = self.auth.list_users()[0]
        self.auth.link_external(original['id'], 'gitlab', 'previous-trusted-provider:1')
        self.publish()
        self.assertFalse(self.store.get_public()['oauth_enabled'])
        self.assertTrue(self.store.auth_options()['gitlab_setup_error'])
        with self.auth.connection() as db:
            self.assertEqual(db.execute('SELECT external_id FROM external_identities').fetchone()[0], 'previous-trusted-provider:1')

    def test_recreated_gitlab_root_cannot_take_existing_administrator_identity(self):
        self.publish()
        self.store.get_public()
        self.publish(root_user_id=2, installation_id='recreated-gitlab')
        self.store.get_public()
        self.assertTrue(self.store.auth_options()['gitlab_setup_error'])
        with self.auth.connection() as db:
            self.assertTrue(db.execute('SELECT external_id FROM external_identities').fetchone()[0].endswith(':1'))

    def test_untouched_legacy_defaults_migrate_but_edited_configuration_does_not(self):
        with self.store.connection() as db:
            db.execute("UPDATE repository_config SET mode='disabled',url='',revision=1")
            db.execute('DELETE FROM repository_installation')
        self.assertEqual(RepositoryStore(self.root).get_public()['mode'], 'managed')
        with self.store.connection() as db:
            db.execute("UPDATE repository_config SET mode='disabled',url='',revision=2")
            db.execute('DELETE FROM repository_installation')
        self.assertEqual(RepositoryStore(self.root).get_public()['mode'], 'disabled')

    def test_repeated_save_of_managed_defaults_does_not_revoke_sessions(self):
        self.publish()
        self.store.get_public()
        with self.store.connection() as db:
            revision = self.store._config(db)['revision']
        self.store.save({'mode': 'managed', 'use_managed_defaults': True})
        with self.store.connection() as db:
            self.assertEqual(self.store._config(db)['revision'], revision)

    def test_v2_makes_existing_binding_authoritative_once_and_preserves_uuid_hash(self):
        self.publish()
        self.store.get_public()
        original, old = self.auth.login('admin', '12345678')
        with self.auth.connection() as db:
            hashed = db.execute('SELECT password_hash FROM accounts').fetchone()[0]
        with self.store.connection() as db:
            revision = self.store._config(db)['revision']
        self.publish(version=2, admin_user_id=1)
        self.store.get_public()
        self.assertEqual(self.auth.list_users()[0]['id'], original['id'])
        self.assertEqual(self.auth.list_users()[0]['auth_provider'], 'gitlab')
        self.assertIsNone(self.auth.get_session(old['token']))
        with self.assertRaises(AuthError):
            self.auth.login('admin', '12345678')
        with self.auth.connection() as db:
            self.assertEqual(db.execute('SELECT password_hash FROM accounts').fetchone()[0], hashed)
        with self.store.connection() as db:
            self.assertEqual(self.store._config(db)['revision'], revision)
        # A restart must not reset roles or invalidate an existing user session.
        external = hashlib.sha256(self.payload['url'].encode()).hexdigest() + ':1'
        self.auth.sync_external_identity(external, 'Administrator', False)
        current = self.auth.new_session(original['id'])
        RepositoryStore(self.root, auth=self.auth).get_public()
        self.assertFalse(self.auth.list_users()[0]['admin'])
        self.assertIsNotNone(self.auth.get_session(current['token']))

    def test_fresh_v2_install_binds_original_admin_without_matching_names(self):
        original = self.auth.list_users()[0]
        self.publish(version=2, admin_user_id=1)
        self.store.get_public()
        external = hashlib.sha256(self.payload['url'].encode()).hexdigest() + ':1'
        result = self.auth.get_or_create_external('gitlab', external, 'Upstream Admin', 'root', admin=True)
        self.assertEqual(result['id'], original['id'])
        self.assertEqual(result['username'], 'admin')
        self.assertEqual(result['auth_provider'], 'gitlab')
        with self.assertRaisesRegex(AuthError, 'GitLab'):
            self.auth.update_user(result['id'], result['id'], {'name': 'local override'})

    def test_explicit_console_recovery_switches_mode_without_resetting_password(self):
        self.publish(version=2, admin_user_id=1)
        self.store.get_public()
        original = self.auth.list_users()[0]
        spec = importlib.util.spec_from_file_location('restore_local_admin', Path(__file__).parents[1] / 'scripts/restore-local-admin.py')
        recovery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recovery)
        with self.assertRaises(ValueError):
            recovery.restore(self.root, original['id'])
        recovery.restore(self.root, original['id'], disable_gitlab=True)
        self.assertEqual(self.store.get_public()['mode'], 'disabled')
        self.assertEqual(self.auth.login('admin', '12345678')[0]['id'], original['id'])
        self.store.save({'mode': 'managed', 'use_managed_defaults': True})
        self.assertEqual(self.auth.list_users()[0]['auth_provider'], 'gitlab')
        with self.assertRaises(AuthError):
            self.auth.login('admin', '12345678')


class DockerInstallationInitialization(unittest.TestCase):
    def test_linux_new_environment_belongs_to_installation_user(self):
        if not hasattr(os, 'getuid') or os.getuid() != 0:
            self.skipTest('Docker root helper ownership check')
        spec = importlib.util.spec_from_file_location('fantasy_initialize', Path(__file__).parents[1] / 'scripts/initialize.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='fantasy-initialize-test-') as temporary:
            root = Path(temporary)
            module.initialize(root, 1234, 1234)
            self.assertEqual(((root / '.env').stat().st_uid, (root / '.env').stat().st_gid), (1234, 1234))
            self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / 'data/effects').stat().st_uid, 1234)

    def test_defaults_are_added_without_replacing_existing_keys_or_explicit_profile(self):
        spec = importlib.util.spec_from_file_location('fantasy_initialize', Path(__file__).parents[1] / 'scripts/initialize.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='fantasy-initialize-test-') as temporary:
            root = Path(temporary)
            existing = 'POSTGRES_PASSWORD=original-db\nCOMMUNITY_IDENTITY_SECRET=original-key\nANIMATION_API_KEY=original-api\nCOMPOSE_PROFILES=\n'
            (root / '.env').write_text(existing)
            module.initialize(root, os.getuid(), os.getgid())
            first = (root / '.env').read_text()
            self.assertTrue(first.startswith(existing))
            self.assertNotIn('COMPOSE_PROFILES=gitlab', first)
            self.assertIn('COMMUNITY_GITLAB_ROOT_PASSWORD=', first)
            module.initialize(root, os.getuid(), os.getgid())
            self.assertEqual(first, (root / '.env').read_text())

    def test_empty_example_does_not_get_silently_rekeyed(self):
        spec = importlib.util.spec_from_file_location('fantasy_initialize', Path(__file__).parents[1] / 'scripts/initialize.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='fantasy-initialize-test-') as temporary:
            root = Path(temporary)
            (root / '.env').write_text('POSTGRES_PASSWORD=\n')
            with self.assertRaises(ValueError):
                module.initialize(root, os.getuid(), os.getgid())
            self.assertEqual((root / '.env').read_text(), 'POSTGRES_PASSWORD=\n')

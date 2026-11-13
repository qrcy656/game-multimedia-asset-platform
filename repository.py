"""GitLab entry configuration and one-use OAuth identity verification.

Bundled GitLab is the default. OAuth only requests read_user; session tokens are
encrypted using the same protected key as API credentials and never sent to UI.
The portal supplies its fixed callback URL and decides whether an authenticated
identity signs in or explicitly links to an already authenticated local account.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager, asynccontextmanager
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import ssl
import sqlite3
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from portal.registry import Registry


STATE_SECONDS = 600
SESSION_SECONDS = 8 * 3600
SESSION_CACHE_SECONDS = 60
LOGIN_ERROR = 'GitLab 登录未完成或验证已失效，请重新发起登录。'
SESSION_ERROR = '暂时无法验证 GitLab 登录状态，请稍后重试或使用平台账号登录。'
logger = logging.getLogger(__name__)


class RepositoryError(ValueError):
    """A safe message suitable for the UI; no provider response text is exposed."""


def _url(value, *, callback=False):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(char.isspace() or ord(char) < 32 for char in value) or '\\' in value):
        raise RepositoryError('GitLab 地址必须是有效的 http(s) 地址。')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
        if (parsed.scheme not in ('http', 'https') or not hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or '?' in value or '#' in value or any(part in ('.', '..') for part in parsed.path.split('/'))):
            raise ValueError()
        if any(char in hostname for char in ('%', '/', '@')) or port == 0:
            raise ValueError()
        hostname = hostname.encode('idna').decode('ascii').lower()
    except (ValueError, UnicodeError):
        raise RepositoryError('GitLab 地址不能包含账号、参数、片段或无效端口。') from None
    if callback:
        # Keep the exact callback string used at initiation for token exchange.
        return value
    authority = '[' + hostname + ']' if ':' in hostname else hostname
    if port is not None and port != (443 if parsed.scheme == 'https' else 80):
        authority += ':' + str(port)
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', ''))


def _small_text(value, *, limit, empty=False):
    return (isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


class RepositoryStore:
    def __init__(self, root, *, transport=None, auth=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Share the registry's existing key lifecycle; do not create a second key.
        self.cipher = Registry(self.root).cipher
        self.transport = transport
        self.auth = auth
        self.bootstrap_error = False
        self._bootstrap_admin_digest = None
        # The deployed app has one ASGI worker. Serialize refresh rotation and
        # logout so two requests cannot consume the same refresh token twice.
        self._session_lock = asyncio.Lock()
        self.path = self.root / 'repository.sqlite3'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS repository_config (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    mode TEXT NOT NULL, url TEXT NOT NULL,
                    oauth_enabled INTEGER NOT NULL, oauth_client_id TEXT NOT NULL,
                    client_secret TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS repository_oauth_states (
                    state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL, actor_id TEXT,
                    revision INTEGER NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS repository_oauth_grants (
                    grant_hash TEXT PRIMARY KEY, external_id TEXT NOT NULL, actor_id TEXT,
                    tokens TEXT NOT NULL, revision INTEGER NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS repository_oauth_sessions (
                    session_hash TEXT PRIMARY KEY, external_id TEXT NOT NULL,
                    tokens TEXT NOT NULL, revision INTEGER NOT NULL,
                    verified_at REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS repository_revoked_sessions (
                    session_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS repository_session_leases (
                    session_hash TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
                INSERT OR IGNORE INTO repository_config VALUES(1,'disabled','',0,'','',1);
                CREATE TABLE IF NOT EXISTS repository_installation (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    managed_defaults INTEGER NOT NULL, bootstrap_digest TEXT NOT NULL);
            ''')
            if not db.execute('SELECT 1 FROM repository_installation WHERE singleton=1').fetchone():
                current = self._config(db)
                untouched = (current['revision'] == 1 and current['mode'] == 'disabled'
                             and not current['url'] and not current['oauth_client_id'] and not current['client_secret'])
                managed = untouched and os.environ.get('COMMUNITY_REPOSITORY_DEFAULT', 'managed') == 'managed'
                db.execute('INSERT INTO repository_installation VALUES(1,?,?)', (int(managed), ''))
                if managed:
                    db.execute("UPDATE repository_config SET mode='managed',url=? WHERE singleton=1",
                               (_url(os.environ.get('COMMUNITY_GITLAB_URL', 'http://localhost:18083')),))
            self._prune(db)
        if os.name != 'nt':
            self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _config(db):
        return dict(db.execute('SELECT * FROM repository_config WHERE singleton=1').fetchone())

    @staticmethod
    def _prune(db):
        now = time.time()
        db.execute('DELETE FROM repository_oauth_states WHERE expires<=?', (now,))
        db.execute('DELETE FROM repository_oauth_grants WHERE expires<=?', (now,))
        # Keep a short rejection marker when pruning; the portal independently
        # enforces the same eight-hour maximum on its own authenticated sessions.
        db.execute('''INSERT OR REPLACE INTO repository_revoked_sessions
                      SELECT session_hash,? FROM repository_oauth_sessions WHERE expires<=?''', (now + 60, now))
        db.execute('DELETE FROM repository_oauth_sessions WHERE expires<=?', (now,))
        db.execute('DELETE FROM repository_revoked_sessions WHERE expires<=?', (now,))

    def _seal(self, value):
        return self.cipher.encrypt(json.dumps(value, separators=(',', ':')).encode()).decode()

    def _open_tokens(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    @staticmethod
    def _token_data(token, previous_refresh=''):
        if not isinstance(token, dict):
            raise ValueError()
        access, refresh = token.get('access_token'), token.get('refresh_token', previous_refresh)
        expires_in = token.get('expires_in', 7200)
        if (not _small_text(access, limit=8192) or not access.isascii() or any(char.isspace() for char in access)
                or not _small_text(refresh, limit=8192, empty=True) or not refresh.isascii()
                or any(char.isspace() for char in refresh)
                or str(token.get('token_type', '')).lower() != 'bearer'
                or type(expires_in) not in (int, float) or not math.isfinite(expires_in) or not 0 < expires_in <= 365 * 86400
                or ('scope' in token and (not isinstance(token['scope'], str) or 'read_user' not in token['scope'].split()))):
            raise ValueError()
        return {'access_token': access, 'refresh_token': refresh, 'expires_at': time.time() + expires_in}

    @staticmethod
    def _user_identity(config, user):
        if (not isinstance(user, dict) or type(user.get('id')) is not int or not 0 < user['id'] < 2**63
                or user.get('state') != 'active' or user.get('bot') is not False
                or not _small_text(user.get('username'), limit=255)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', user['username'])
                or not _small_text(user.get('name'), limit=255)):
            raise ValueError()
        namespace = hashlib.sha256(config['url'].encode()).hexdigest()
        if 'is_admin' in user and type(user['is_admin']) is not bool:
            raise ValueError()
        return {'external_id': namespace + ':' + str(user['id']), 'username': user['username'],
                'name': user['name'].strip()[:80], 'admin': user.get('is_admin', False)}

    def _http(self, config, *, timeout=20):
        target, headers = config['url'], {}
        if config['mode'] == 'managed':
            target = _url(os.environ.get('COMMUNITY_GITLAB_INTERNAL_URL', 'http://gitlab:80'))
            if not urlsplit(target).path:
                target += urlsplit(config['url']).path
            headers['Host'] = urlsplit(config['url']).netloc
        certificate = os.environ.get('COMMUNITY_GITLAB_CA_FILE')
        verify = ssl.create_default_context(cafile=certificate) if certificate else True
        return target, httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
            verify=verify, headers=headers, timeout=httpx.Timeout(timeout, connect=min(timeout, 10)))

    @staticmethod
    def _json(response):
        response.raise_for_status()
        if len(response.content) > 65536:
            raise ValueError()
        return response.json()

    @staticmethod
    def _public(row):
        configured = row['mode'] != 'disabled' and bool(row['url'])
        return {'mode': row['mode'], 'url': row['url'], 'configured': configured,
                'oauth_enabled': bool(configured and row['oauth_enabled'] and row['oauth_client_id'] and row['client_secret']),
                'oauth_client_id': row['oauth_client_id'], 'has_client_secret': bool(row['client_secret'])}

    def get_public(self):
        self.sync_managed_bootstrap()
        with self.connection() as db:
            automatic = bool(db.execute('SELECT managed_defaults FROM repository_installation WHERE singleton=1').fetchone()[0])
            return {**self._public(self._config(db)), 'managed_defaults': automatic}

    def _managed_bootstrap(self):
        """Read only the signed, installation-local Docker volume handoff."""
        raw_path = os.environ.get('COMMUNITY_GITLAB_BOOTSTRAP_FILE')
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768:
            raise RepositoryError('随附 GitLab 初始化文件无效。')
        try:
            envelope = json.loads(path.read_text(encoding='utf-8'))
            payload, signature = envelope['payload'], envelope['signature']
            key = os.environ['COMMUNITY_IDENTITY_SECRET']
            if not isinstance(payload, str) or not isinstance(signature, str) or len(key) < 32:
                raise ValueError()
            expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            data = json.loads(payload)
            address = _url(os.environ.get('COMMUNITY_GITLAB_URL', 'http://localhost:18083'))
            callback = os.environ.get('APP_ORIGIN', 'http://localhost:18082').rstrip('/') + '/auth/gitlab/callback'
            identity_key = 'admin_user_id' if data.get('version') == 2 else 'root_user_id'
            if (data.get('version') not in (1, 2) or data.get('url') != address or data.get('redirect_uri') != callback
                    or not _small_text(data.get('client_id'), limit=512)
                    or not _small_text(data.get('client_secret'), limit=8192)
                    or type(data.get(identity_key)) is not int or data[identity_key] <= 0
                    or not _small_text(data.get('installation_id'), limit=128)):
                raise ValueError()
            if data.get('legacy_root_user_id') is not None and (type(data['legacy_root_user_id']) is not int or data['legacy_root_user_id'] != 1):
                raise ValueError()
            return data, hashlib.sha256(payload.encode()).hexdigest()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise RepositoryError('随附 GitLab 初始化尚未验证，请检查部署配置。') from None

    def sync_managed_bootstrap(self):
        # UI/API calls can arrive while GitLab performs its first reconfigure.
        # Missing or invalid bootstrap data never blocks local maintenance login.
        with self.connection() as db:
            state = dict(db.execute('SELECT * FROM repository_installation WHERE singleton=1').fetchone())
            current = self._config(db)
        if not state['managed_defaults'] or current['mode'] != 'managed':
            return
        try:
            bootstrap = self._managed_bootstrap()
            if bootstrap is None:
                return
            data, digest = bootstrap
            namespace = hashlib.sha256(data['url'].encode()).hexdigest() + ':'
            external_id = namespace + str(data.get('admin_user_id', data.get('root_user_id')))
            if self.auth is not None and self._bootstrap_admin_digest != digest:
                self.auth.bind_installation_gitlab_admin(external_id, os.environ.get('COMMUNITY_GITLAB_ADMIN_ACCOUNT_ID') or None,
                    installation_id=data['installation_id'], authoritative=data['version'] == 2,
                    previous_external_id=namespace + str(data['legacy_root_user_id']) if data.get('legacy_root_user_id') else None)
                self._bootstrap_admin_digest = digest
            if digest == state['bootstrap_digest']:
                self.bootstrap_error = False
                return
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                state = db.execute('SELECT * FROM repository_installation WHERE singleton=1').fetchone()
                current = self._config(db)
                if not state['managed_defaults'] or current['mode'] != 'managed':
                    return
                unchanged = (current['url'] == data['url'] and current['oauth_enabled'] and current['oauth_client_id'] == data['client_id']
                             and current['client_secret'] and self.cipher.decrypt(current['client_secret'].encode()).decode() == data['client_secret'])
                if not unchanged:
                    db.execute('''UPDATE repository_config SET url=?,oauth_enabled=1,oauth_client_id=?,
                                client_secret=?,revision=revision+1 WHERE singleton=1''',
                               (data['url'], data['client_id'], self.cipher.encrypt(data['client_secret'].encode()).decode()))
                db.execute('UPDATE repository_installation SET bootstrap_digest=? WHERE singleton=1', (digest,))
                if not unchanged:
                    db.execute('DELETE FROM repository_oauth_states')
                    db.execute('DELETE FROM repository_oauth_grants')
            self.bootstrap_error = False
        except Exception:
            self.bootstrap_error = True

    def auth_options(self):
        public = self.get_public()
        with self.connection() as db:
            automatic = bool(db.execute('SELECT managed_defaults FROM repository_installation WHERE singleton=1').fetchone()[0])
        return {'gitlab_login': public['oauth_enabled'],
                'preferred_login': 'gitlab' if public['oauth_enabled'] or automatic else 'local',
                'gitlab_initializing': automatic and not public['oauth_enabled'],
                'gitlab_setup_error': self.bootstrap_error,
                'gitlab_url': public['url'], 'local_login': any(user['active'] and user['auth_provider'] == 'local' for user in self.auth.list_users()) if self.auth else True}

    def account_management(self):
        public = self.get_public()
        return {'provider': 'gitlab' if public['oauth_enabled'] else 'local',
                'url': public['url'] + '/admin/users' if public['oauth_enabled'] else '',
                'profile_url': public['url'] + '/-/user_settings/password' if public['oauth_enabled'] else ''}

    def save(self, payload):
        self.sync_managed_bootstrap()
        allowed = {'mode', 'url', 'oauth_enabled', 'oauth_client_id', 'oauth_client_secret', 'clear_client_secret', 'use_managed_defaults'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise RepositoryError('代码仓库配置格式无效。')
        automatic = payload.get('use_managed_defaults', False)
        if type(automatic) is not bool:
            raise RepositoryError('随附 GitLab 自动配置开关必须为布尔值。')
        if automatic:
            if payload.get('mode') != 'managed' or payload.get('oauth_client_secret') or payload.get('clear_client_secret'):
                raise RepositoryError('自动配置需选择随附 GitLab，并清空本次手动输入的 OAuth 密钥。')
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                old = self._config(db)
                managed = db.execute('SELECT managed_defaults FROM repository_installation WHERE singleton=1').fetchone()[0]
                if managed and old['mode'] == 'managed' and old['url'] == _url(os.environ.get('COMMUNITY_GITLAB_URL', 'http://localhost:18083')):
                    return {**self._public(old), 'managed_defaults': True}
                db.execute("""UPDATE repository_config SET mode='managed',url=?,oauth_enabled=0,
                              oauth_client_id='',client_secret='',revision=revision+1 WHERE singleton=1""",
                           (_url(os.environ.get('COMMUNITY_GITLAB_URL', 'http://localhost:18083')),))
                db.execute("UPDATE repository_installation SET managed_defaults=1,bootstrap_digest='' WHERE singleton=1")
                db.execute('DELETE FROM repository_oauth_states')
                db.execute('DELETE FROM repository_oauth_grants')
            self._bootstrap_admin_digest = None
            return self.get_public()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._config(db)
            mode = payload.get('mode', old['mode'])
            if not isinstance(mode, str) or mode not in {'disabled', 'managed', 'external'}:
                raise RepositoryError('请选择随附 GitLab、外部 GitLab 或停用。')
            address = payload.get('url', old['url'])
            if mode == 'managed' and (not address or ('url' not in payload and 'mode' in payload)):
                address = os.environ.get('COMMUNITY_GITLAB_URL', '')
            if address:
                address = _url(address)
            elif mode != 'disabled':
                raise RepositoryError('请填写 GitLab 地址；随附模式也可设置 COMMUNITY_GITLAB_URL。')
            elif not isinstance(address, str):
                raise RepositoryError('GitLab 地址格式无效。')
            enabled = payload.get('oauth_enabled', bool(old['oauth_enabled']))
            clear = payload.get('clear_client_secret', False)
            if type(enabled) is not bool or type(clear) is not bool:
                raise RepositoryError('OAuth 开关和清空密钥选项必须为布尔值。')
            client_id = payload.get('oauth_client_id', old['oauth_client_id'])
            if not _small_text(client_id, limit=512, empty=True):
                raise RepositoryError('OAuth Application ID 格式无效。')
            client_id = client_id.strip()
            encrypted = old['client_secret']
            if 'oauth_client_secret' in payload:
                secret = payload['oauth_client_secret']
                if not _small_text(secret, limit=8192, empty=True):
                    raise RepositoryError('OAuth Client Secret 格式无效。')
                if secret and not secret.strip():
                    raise RepositoryError('OAuth Client Secret 不能只包含空白字符。')
                # A blank password input preserves the saved value; clearing is explicit.
                if secret:
                    if clear:
                        raise RepositoryError('不能同时填写和清空 OAuth Client Secret。')
                    encrypted = self.cipher.encrypt(secret.encode()).decode()
            if clear:
                encrypted = ''
                enabled = False
            if mode == 'disabled':
                enabled = False
            if enabled and (not client_id or not encrypted):
                raise RepositoryError('启用 GitLab 登录前，请填写 Application ID 和 Client Secret。')
            same_secret = (encrypted == old['client_secret'] or (encrypted and old['client_secret'] and
                           self.cipher.decrypt(encrypted.encode()) == self.cipher.decrypt(old['client_secret'].encode())))
            unchanged = (mode == old['mode'] and address == old['url'] and enabled == bool(old['oauth_enabled'])
                         and client_id == old['oauth_client_id'] and same_secret)
            if not unchanged:
                db.execute('''UPDATE repository_config SET mode=?,url=?,oauth_enabled=?,oauth_client_id=?,
                              client_secret=?,revision=revision+1 WHERE singleton=1''',
                           (mode, address, int(enabled), client_id, encrypted))
            # An administrator's explicit choices always outrank defaults.
            db.execute('UPDATE repository_installation SET managed_defaults=0 WHERE singleton=1')
            # Any settings replacement invalidates pending logins, including key changes.
            if not unchanged:
                db.execute('DELETE FROM repository_oauth_states')
                db.execute('DELETE FROM repository_oauth_grants')
            self._prune(db)
            result = {**self._public(self._config(db)), 'managed_defaults': False}
        return result

    def begin_oauth(self, redirect_uri, actor_id=None):
        self.sync_managed_bootstrap()
        redirect_uri = _url(redirect_uri, callback=True)
        if actor_id is not None and not _small_text(actor_id, limit=128):
            raise RepositoryError(LOGIN_ERROR)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        now = time.time()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self._config(db)
            if not self._public(config)['oauth_enabled']:
                raise RepositoryError('尚未启用 GitLab 登录，请先使用平台账号登录。')
            self._prune(db)
            if db.execute('SELECT COUNT(*) FROM repository_oauth_states').fetchone()[0] >= 1000:
                raise RepositoryError('登录请求过多，请稍后重试。')
            db.execute('INSERT INTO repository_oauth_states VALUES(?,?,?,?,?,?)',
                       (hashlib.sha256(state.encode()).hexdigest(), self.cipher.encrypt(verifier.encode()).decode(),
                        redirect_uri, actor_id, config['revision'], now + STATE_SECONDS))
        query = urlencode({'client_id': config['oauth_client_id'], 'redirect_uri': redirect_uri,
                           'response_type': 'code', 'scope': 'read_user', 'state': state,
                           'code_challenge': challenge, 'code_challenge_method': 'S256'})
        return {'authorization_url': config['url'] + '/oauth/authorize?' + query,
                'state': state, 'cookie_state': state, 'expires_in': STATE_SECONDS}

    async def complete_oauth(self, code, state, cookie_state, redirect_uri):
        if (not _small_text(code, limit=8192) or not isinstance(state, str)
                or not isinstance(cookie_state, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', state)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', cookie_state)
                or not hmac.compare_digest(state, cookie_state)):
            raise RepositoryError(LOGIN_ERROR)
        try:
            redirect_uri = _url(redirect_uri, callback=True)
        except RepositoryError:
            raise RepositoryError(LOGIN_ERROR) from None
        digest = hashlib.sha256(state.encode()).hexdigest()
        # Consume before any network call; concurrent callbacks cannot exchange twice.
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM repository_oauth_states WHERE state_hash=?', (digest,)).fetchone()
            db.execute('DELETE FROM repository_oauth_states WHERE state_hash=?', (digest,))
            config = self._config(db)
        if (row is None or row['expires'] <= time.time() or row['redirect_uri'] != redirect_uri
                or row['revision'] != config['revision'] or not self._public(config)['oauth_enabled']):
            raise RepositoryError(LOGIN_ERROR)
        try:
            secret = self.cipher.decrypt(config['client_secret'].encode()).decode()
            verifier = self.cipher.decrypt(row['verifier'].encode()).decode()
            target, client = self._http(config)
            async with client:
                response = await client.post(target + '/oauth/token', data={
                    'grant_type': 'authorization_code', 'client_id': config['oauth_client_id'],
                    'client_secret': secret, 'code': code, 'redirect_uri': redirect_uri, 'code_verifier': verifier})
                tokens = self._token_data(self._json(response))
                response = await client.get(target + '/api/v4/user', headers={'Authorization': 'Bearer ' + tokens['access_token']})
                identity = self._user_identity(config, self._json(response))
        except Exception:
            # Never include response bodies, request URLs, code, access tokens, or secrets.
            raise RepositoryError(LOGIN_ERROR) from None
        grant_id = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = self._config(db)
            if latest['revision'] != config['revision'] or not self._public(latest)['oauth_enabled']:
                raise RepositoryError(LOGIN_ERROR)
            self._prune(db)
            db.execute('INSERT INTO repository_oauth_grants VALUES(?,?,?,?,?,?)',
                       (hashlib.sha256(grant_id.encode()).hexdigest(), identity['external_id'], row['actor_id'],
                        self._seal(tokens), config['revision'], time.time() + STATE_SECONDS))
        return {**identity, 'actor_id': row['actor_id'], 'grant_id': grant_id}

    async def complete_pat(self, token, username=None):
        """MCP/non-browser login with a GitLab PAT restricted to read_user.

        The PAT stays encrypted server-side; logout revokes the platform session,
        never the user's persistent PAT. GitLab still checks it every cache period.
        """
        if not _small_text(token, limit=8192) or not token.isascii() or any(c.isspace() for c in token):
            raise RepositoryError('GitLab 访问令牌无效。')
        self.sync_managed_bootstrap()
        with self.connection() as db:
            config = self._config(db)
        if not self._public(config)['oauth_enabled']:
            raise RepositoryError('请先配置 GitLab 统一登录。')
        try:
            target, client = self._http(config)
            async with client:
                response = await client.get(target + '/api/v4/user', headers={'Authorization': 'Bearer ' + token})
                identity = self._user_identity(config, self._json(response))
        except Exception:
            raise RepositoryError('GitLab 访问令牌无效、已撤销或账号已停用。') from None
        if username and username != identity['username']:
            # A platform account name is allowed only when it is already linked
            # to this exact numeric GitLab identity, never by matching names.
            linked = self.auth.external_username_matches(identity['external_id'], username) if self.auth else False
            if not linked:
                raise RepositoryError('GitLab 访问令牌与指定账号不一致。')
        grant_id = secrets.token_urlsafe(32)
        tokens = {'kind': 'personal_access_token', 'access_token': token, 'refresh_token': '', 'expires_at': time.time() + SESSION_SECONDS}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = self._config(db)
            if latest['revision'] != config['revision'] or not self._public(latest)['oauth_enabled']:
                raise RepositoryError(LOGIN_ERROR)
            self._prune(db)
            db.execute('INSERT INTO repository_oauth_grants VALUES(?,?,?,?,?,?)',
                (hashlib.sha256(grant_id.encode()).hexdigest(), identity['external_id'], None,
                 self._seal(tokens), config['revision'], time.time() + STATE_SECONDS))
        return {**identity, 'actor_id': None, 'grant_id': grant_id}

    @staticmethod
    def _session_hash(raw_token):
        if not _small_text(raw_token, limit=1024) or not raw_token.isascii():
            raise RepositoryError(LOGIN_ERROR)
        return hashlib.sha256(raw_token.encode()).hexdigest()

    def bind_session(self, grant_id, raw_community_session_token):
        """Consume an OAuth grant once, binding its encrypted tokens to a new session."""
        digest = self._session_hash(raw_community_session_token)
        if not isinstance(grant_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', grant_id):
            raise RepositoryError(LOGIN_ERROR)
        grant_hash = hashlib.sha256(grant_id.encode()).hexdigest()
        valid, now = False, time.time()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db)
            grant = db.execute('SELECT * FROM repository_oauth_grants WHERE grant_hash=?', (grant_hash,)).fetchone()
            db.execute('DELETE FROM repository_oauth_grants WHERE grant_hash=?', (grant_hash,))
            config = self._config(db)
            already_bound = db.execute('SELECT 1 FROM repository_oauth_sessions WHERE session_hash=?', (digest,)).fetchone()
            revoked = db.execute('SELECT 1 FROM repository_revoked_sessions WHERE session_hash=?', (digest,)).fetchone()
            valid = bool(grant and grant['expires'] > now and grant['revision'] == config['revision']
                         and grant['actor_id'] is None and self._public(config)['oauth_enabled'] and not already_bound and not revoked)
            if valid:
                db.execute('INSERT INTO repository_oauth_sessions VALUES(?,?,?,?,?,?)',
                           (digest, grant['external_id'], grant['tokens'], grant['revision'], now, now + SESSION_SECONDS))
        if not valid:
            raise RepositoryError(LOGIN_ERROR)

    def discard_grant(self, grant_id):
        """Explicit account linking verifies identity without changing the login session."""
        if not isinstance(grant_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', grant_id):
            return
        with self.connection() as db:
            db.execute('DELETE FROM repository_oauth_grants WHERE grant_hash=?', (hashlib.sha256(grant_id.encode()).hexdigest(),))
            self._prune(db)

    @staticmethod
    def _invalidate(db, row):
        db.execute('DELETE FROM repository_oauth_sessions WHERE session_hash=?', (row['session_hash'],))
        # Removing a binding must never make the same cookie look like a local
        # login on its next request. Mark it rejected for its remaining lifetime.
        db.execute('INSERT OR REPLACE INTO repository_revoked_sessions VALUES(?,?)',
                   (row['session_hash'], max(row['expires'], time.time() + 60)))

    def _invalid_session(self, row, reason='identity_rejected'):
        logger.warning('GitLab session rejected reason=%s session=%s', reason, row['session_hash'][:12])
        with self.connection() as db:
            self._invalidate(db, row)
        return False

    async def _refresh_session(self, client, target, config, row, tokens):
        if not tokens.get('refresh_token'):
            return None
        secret = self.cipher.decrypt(config['client_secret'].encode()).decode()
        response = await client.post(target + '/oauth/token', data={
            'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
            'client_id': config['oauth_client_id'], 'client_secret': secret})
        if response.status_code in {400, 401, 403}:
            # A revoked grant ends this session; a broken client configuration
            # is a recoverable provider failure, not evidence that the user left.
            data = response.json()
            if isinstance(data, dict) and data.get('error') in {'invalid_grant', 'invalid_token', 'access_denied'}:
                return None
            raise RepositoryError(SESSION_ERROR)
        renewed = self._token_data(self._json(response), tokens['refresh_token'])
        # Persist rotated credentials before checking user state. A network error
        # on the following GET must not lose the only usable refresh token.
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = self._config(db)
            if latest['revision'] != config['revision'] or not self._public(latest)['oauth_enabled']:
                self._invalidate(db, row)
                return None
            updated = db.execute('UPDATE repository_oauth_sessions SET tokens=? WHERE session_hash=? AND revision=?',
                                 (self._seal(renewed), row['session_hash'], row['revision']))
            if updated.rowcount != 1:
                return None
        return renewed

    @asynccontextmanager
    async def _session_guard(self, digest):
        """Serialize rotation/logout across processes sharing the SQLite store."""
        owner, deadline = secrets.token_hex(16), time.monotonic() + 30
        async with self._session_lock:
            while True:
                with self.connection() as db:
                    acquired = db.execute('''INSERT INTO repository_session_leases VALUES(?,?,?)
                        ON CONFLICT(session_hash) DO UPDATE SET owner=excluded.owner,expires=excluded.expires
                        WHERE repository_session_leases.expires<=?''', (digest, owner, time.time() + 120, time.time())).rowcount == 1
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise RepositoryError(SESSION_ERROR)
                await asyncio.sleep(0.05)
            try:
                yield
            finally:
                with self.connection() as db:
                    db.execute('DELETE FROM repository_session_leases WHERE session_hash=? AND owner=?', (digest, owner))

    async def validate_session(self, raw_token):
        """Return False for revoked/expired identities; transient provider errors fail closed.

        Local sessions have no OAuth binding and return True. The portal must
        independently validate its own cookie lifetime before calling this method.
        """
        digest = self._session_hash(raw_token)
        async with self._session_guard(digest):
            with self.connection() as db:
                self._prune(db)
                if db.execute('SELECT 1 FROM repository_revoked_sessions WHERE session_hash=?', (digest,)).fetchone():
                    return False
                found = db.execute('SELECT * FROM repository_oauth_sessions WHERE session_hash=?', (digest,)).fetchone()
                if found is None:
                    local = self.auth.get_session(raw_token) if self.auth else None
                    if local and local['user']['auth_provider'] == 'gitlab':
                        return False
                    return True
                row, config = dict(found), self._config(db)
            if row['revision'] != config['revision'] or not self._public(config)['oauth_enabled']:
                return self._invalid_session(row, 'provider_configuration_changed')
            try:
                tokens = self._open_tokens(row['tokens'])
                if row['verified_at'] > time.time() - SESSION_CACHE_SECONDS and tokens['expires_at'] > time.time() + 30:
                    return True
                target, client = self._http(config)
                async with client:
                    refreshed = False
                    if tokens['expires_at'] <= time.time() + 30:
                        tokens = await self._refresh_session(client, target, config, row, tokens)
                        refreshed = True
                        if tokens is None:
                            return self._invalid_session(row, 'refresh_grant_revoked')
                    response = await client.get(target + '/api/v4/user', headers={'Authorization': 'Bearer ' + tokens['access_token']})
                    if response.status_code == 401 and not refreshed and tokens.get('refresh_token'):
                        tokens = await self._refresh_session(client, target, config, row, tokens)
                        if tokens is None:
                            return self._invalid_session(row, 'refresh_grant_revoked')
                        response = await client.get(target + '/api/v4/user', headers={'Authorization': 'Bearer ' + tokens['access_token']})
                    if response.status_code in {401, 403}:
                        return self._invalid_session(row, 'upstream_access_denied')
                    user = self._json(response)
                    try:
                        identity = self._user_identity(config, user)
                    except ValueError:
                        return self._invalid_session(row)
                    if identity['external_id'] != row['external_id']:
                        return self._invalid_session(row, 'upstream_identity_changed')
                    if self.auth is not None:
                        self.auth.sync_external_identity(identity['external_id'], identity['name'], identity['admin'])
            except Exception:
                raise RepositoryError(SESSION_ERROR) from None
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                latest = self._config(db)
                if latest['revision'] != config['revision'] or not self._public(latest)['oauth_enabled']:
                    self._invalidate(db, row)
                    return False
                updated = db.execute('UPDATE repository_oauth_sessions SET verified_at=? WHERE session_hash=?', (time.time(), digest))
                return updated.rowcount == 1

    async def revoke_session(self, raw_token):
        """Always remove the local grant; remote revocation is bounded best effort."""
        try:
            digest = self._session_hash(raw_token)
        except RepositoryError:
            return
        async with self._session_guard(digest):
            with self.connection() as db:
                self._prune(db)
                found = db.execute('SELECT * FROM repository_oauth_sessions WHERE session_hash=?', (digest,)).fetchone()
                if found is None:
                    return
                row, config = dict(found), self._config(db)
                self._invalidate(db, row)
            if row['revision'] != config['revision']:
                return  # Never send an old provider's tokens to a newly configured URL.
            try:
                tokens = self._open_tokens(row['tokens'])
                if tokens.get('kind') == 'personal_access_token':
                    return
                # Some external providers reuse one token for multiple browser
                # sessions or a newly issued login grant. Do not revoke those.
                with self.connection() as db:
                    others = list(db.execute('SELECT tokens FROM repository_oauth_sessions WHERE revision=?', (row['revision'],)))
                    others += list(db.execute('SELECT tokens FROM repository_oauth_grants WHERE revision=?', (row['revision'],)))
                if any(self._open_tokens(other['tokens']).get('access_token') == tokens['access_token'] for other in others):
                    return
                secret = self.cipher.decrypt(config['client_secret'].encode()).decode()
                target, client = self._http(config, timeout=3)
                async with client:
                    await client.post(target + '/oauth/revoke', data={'token': tokens['access_token'],
                        'client_id': config['oauth_client_id'], 'client_secret': secret})
            except Exception:
                pass  # Logout succeeds even when GitLab is offline or has revoked the token.

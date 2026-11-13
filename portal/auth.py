"""Local accounts and revocable browser sessions for the community edition."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.cookies import SimpleCookie
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

COOKIE = "fantasy_community_session"
SESSION_SECONDS = 8 * 3600
PBKDF2_ROUNDS = 600_000


class AuthError(ValueError):
    pass


def password_hash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise AuthError("密码需要 8–256 个字符")
    salt = secrets.token_bytes(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${value.hex()}"


def password_matches(password, hashed):
    try:
        algorithm, rounds, salt, value = hashed.split("$")
        if algorithm != "pbkdf2_sha256" or not isinstance(password, str) or len(password) > 256:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, value)
    except (ValueError, TypeError):
        return False


class AuthStore:
    def __init__(self, data_dir):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "accounts.sqlite3"
        with self.connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL, password_hash TEXT NOT NULL, admin INTEGER NOT NULL, created REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, auth_provider TEXT NOT NULL DEFAULT 'local');
              CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL
                REFERENCES accounts(id) ON DELETE CASCADE, csrf TEXT NOT NULL, expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS external_identities (provider TEXT NOT NULL, external_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                PRIMARY KEY(provider,external_id), UNIQUE(provider,user_id));
              CREATE TABLE IF NOT EXISTS installation_identity (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), admin_id TEXT NOT NULL,
                managed_gitlab_bound INTEGER NOT NULL DEFAULT 0);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "active" not in {row[1] for row in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "auth_provider" not in {row[1] for row in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'local'")
            if "installation_id" not in {row[1] for row in db.execute("PRAGMA table_info(installation_identity)")}:
                db.execute("ALTER TABLE installation_identity ADD COLUMN installation_id TEXT NOT NULL DEFAULT ''")
            # Existing accounts are never reset during restart or upgrade.
            if db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
                identifier = str(uuid.uuid4())
                db.execute("INSERT INTO accounts(id,username,name,password_hash,admin,created) VALUES(?,?,?,?,?,?)", (identifier, "admin", "admin",
                    password_hash("12345678"), 1, time.time()))
                db.execute("INSERT OR IGNORE INTO installation_identity(singleton,admin_id,managed_gitlab_bound) VALUES(?,?,0)", (1, identifier))
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _public(row):
        return {"id": row["id"], "username": row["username"], "name": row["name"],
                "admin": bool(row["admin"]), "active": bool(row["active"]), "auth_provider": row["auth_provider"]}

    def list_users(self):
        with self.connection() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM accounts ORDER BY created")]

    def create_user(self, username, password, name=None, admin=False):
        if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,40}", username):
            raise AuthError("账号需为 1–40 位字母、数字、下划线、点或连字符")
        name = name or username
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or type(admin) is not bool:
            raise AuthError("显示名称或账号权限无效")
        hashed = password_hash(password)
        identifier = str(uuid.uuid4())
        try:
            with self.connection() as db:
                db.execute("INSERT INTO accounts(id,username,name,password_hash,admin,created) VALUES(?,?,?,?,?,?)", (identifier, username, name.strip(), hashed, int(admin), time.time()))
        except sqlite3.IntegrityError:
            raise AuthError("账号已存在") from None
        return {"id": identifier, "username": username, "name": name.strip(), "admin": admin, "active": True, "auth_provider": "local"}

    def get_or_create_external(self, provider, external_id, name, username, *, admin=None):
        if provider != "gitlab" or not isinstance(external_id, str) or not external_id or len(external_id) > 256:
            raise AuthError("外部账号身份无效")
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or not isinstance(username, str):
            raise AuthError("外部账号资料无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT a.* FROM external_identities e JOIN accounts a ON a.id=e.user_id "
                "WHERE e.provider=? AND e.external_id=?", (provider, external_id)).fetchone()
            if row:
                if not row["active"] and admin is None:
                    raise AuthError("此账号已停用，请联系社区管理员")
                if admin is not None:
                    if type(admin) is not bool:
                        raise AuthError("GitLab 账号权限无效")
                    db.execute("UPDATE accounts SET name=?,admin=?,active=1,auth_provider='gitlab' WHERE id=?",
                               (name.strip(), int(admin), row['id']))
                    row = db.execute("SELECT * FROM accounts WHERE id=?", (row['id'],)).fetchone()
                return self._public(row)
            # External usernames are display hints, never proof of local ownership.
            stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", username)[:22] or "member"
            suffix = hashlib.sha256((provider + "\0" + external_id).encode()).hexdigest()[:8]
            local_name = provider + "_" + stem + "_" + suffix
            if db.execute("SELECT 1 FROM accounts WHERE username=?", (local_name,)).fetchone():
                local_name = provider + "_" + uuid.uuid4().hex
            identifier = str(uuid.uuid4())
            db.execute("INSERT INTO accounts(id,username,name,password_hash,admin,created,auth_provider) VALUES(?,?,?,?,?,?,?)",
                (identifier, local_name, name.strip(), "external$" + provider, int(admin is True), time.time(), provider))
            db.execute("INSERT INTO external_identities VALUES(?,?,?)", (provider, external_id, identifier))
            return self._public(db.execute("SELECT * FROM accounts WHERE id=?", (identifier,)).fetchone())

    def link_external(self, user_id, provider, external_id):
        if provider != "gitlab" or not isinstance(external_id, str) or not external_id or len(external_id) > 256:
            raise AuthError("外部账号身份无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute("SELECT * FROM accounts WHERE id=? AND active=1", (user_id,)).fetchone()
            if not account:
                raise AuthError("当前账号已失效，请重新登录")
            existing = db.execute("SELECT * FROM external_identities WHERE provider=? AND external_id=?", (provider, external_id)).fetchone()
            own = db.execute("SELECT * FROM external_identities WHERE provider=? AND user_id=?", (provider, user_id)).fetchone()
            if (existing and existing["user_id"] != user_id) or (own and own["external_id"] != external_id):
                raise AuthError("GitLab 身份已关联其他账号，不能替换现有身份")
            if not existing:
                db.execute("INSERT INTO external_identities VALUES(?,?,?)", (provider, external_id, user_id))
            # Preserve the account ID, privilege, password method, and all assets.
            return self._public(account)

    def bind_installation_gitlab_admin(self, external_id, explicit_account_id=None, *, installation_id='', previous_external_id=None, authoritative=False):
        """Link a signed installation identity, never a matching external username.

        Fresh installs record their initial administrator UUID once. Existing
        installs require an explicit UUID in installation configuration.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            installation = db.execute("SELECT * FROM installation_identity WHERE singleton=1").fetchone()
            target = explicit_account_id or (installation['admin_id'] if installation else None)
            if not target:
                return False
            account = db.execute("SELECT * FROM accounts WHERE id=?", (target,)).fetchone()
            if not account:
                raise AuthError("GitLab 初始管理员关联配置无效，请核对本地管理员 ID。")
            existing = db.execute("SELECT * FROM external_identities WHERE provider='gitlab' AND external_id=?", (external_id,)).fetchone()
            own = db.execute("SELECT * FROM external_identities WHERE provider='gitlab' AND user_id=?", (target,)).fetchone()
            if installation and installation['installation_id'] and installation_id != installation['installation_id']:
                raise AuthError("GitLab 安装身份已变化，已保留原账号关联。")
            migration = bool(authoritative and previous_external_id and own and own['external_id'] == previous_external_id
                             and installation and installation['managed_gitlab_bound'])
            if (existing and existing['user_id'] != target) or (own and own['external_id'] != external_id and not migration):
                raise AuthError("GitLab 初始管理员身份与已有绑定冲突，已保留原账号关联。")
            if installation and installation['managed_gitlab_bound'] and not existing and not migration:
                # A deleted/recreated GitLab instance cannot silently take over
                # the previous installation's administrator.
                raise AuthError("GitLab 安装身份已变化，请从本地管理员账号重新关联。")
            if not (existing or migration) and not (account['active'] and account['admin']):
                raise AuthError("GitLab 初始管理员关联配置无效，请核对本地管理员 ID。")
            if migration:
                db.execute("DELETE FROM external_identities WHERE provider='gitlab' AND user_id=?", (target,))
            if not existing:
                db.execute("INSERT INTO external_identities VALUES('gitlab',?,?)", (external_id, target))
            if not installation:
                db.execute("INSERT INTO installation_identity VALUES(?,?,1,?)", (1, target, installation_id))
            else:
                db.execute("UPDATE installation_identity SET managed_gitlab_bound=1,installation_id=? WHERE singleton=1", (installation_id,))
            if authoritative and (migration or account['auth_provider'] != 'gitlab'):
                # Keep UUID/assets/password hash, but do not leave a local login
                # that bypasses a block or role change in GitLab.
                db.execute("UPDATE accounts SET auth_provider='gitlab' WHERE id=?", (target,))
                db.execute("DELETE FROM sessions WHERE user_id=?", (target,))
            return True

    def sync_external_identity(self, external_id, name, admin):
        """Apply the just-verified upstream identity to every subsequent request."""
        if type(admin) is not bool:
            raise AuthError("GitLab 账号权限无效")
        with self.connection() as db:
            db.execute("UPDATE accounts SET name=?,admin=?,active=1,auth_provider='gitlab' "
                       "WHERE id IN (SELECT user_id FROM external_identities WHERE provider='gitlab' AND external_id=?)",
                       (name, int(admin), external_id))

    def external_username_matches(self, external_id, username):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM accounts a JOIN external_identities e ON a.id=e.user_id "
                              "WHERE e.provider='gitlab' AND e.external_id=? AND a.username=?", (external_id, username)).fetchone() is not None

    @staticmethod
    def _local_management(target):
        if target['auth_provider'] == 'gitlab':
            raise AuthError("此账号由 GitLab 统一管理，请在 GitLab 中修改资料、权限、密码或停用账号")

    @staticmethod
    def _admin_target(db, actor_id, user_id):
        actor = db.execute("SELECT * FROM accounts WHERE id=? AND active=1 AND admin=1", (actor_id,)).fetchone()
        if not actor:
            raise AuthError("仅有效管理员可管理账号")
        target = db.execute("SELECT * FROM accounts WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise AuthError("账号不存在")
        return target

    def update_user(self, actor_id, user_id, values):
        if not isinstance(values, dict) or not values or set(values) - {"name", "admin", "active"}:
            raise AuthError("账号信息字段无效")
        if "name" in values and (not isinstance(values["name"], str) or not values["name"].strip() or len(values["name"]) > 80):
            raise AuthError("显示名称需要 1–80 个字符")
        if any(key in values and type(values[key]) is not bool for key in ("admin", "active")):
            raise AuthError("账号状态或权限无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self._admin_target(db, actor_id, user_id)
            self._local_management(target)
            name = values.get("name", target["name"]).strip()
            admin = values.get("admin", bool(target["admin"]))
            active = values.get("active", bool(target["active"]))
            if actor_id == user_id and (not admin or not active):
                raise AuthError("不能停用或撤销自己的管理员权限")
            if target["admin"] and target["active"] and not (admin and active):
                count = db.execute("SELECT COUNT(*) FROM accounts WHERE admin=1 AND active=1").fetchone()[0]
                if count <= 1:
                    raise AuthError("必须保留至少一个有效管理员")
            db.execute("UPDATE accounts SET name=?,admin=?,active=? WHERE id=?", (name, int(admin), int(active), user_id))
            if bool(target["admin"]) != admin or bool(target["active"]) != active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return self._public(db.execute("SELECT * FROM accounts WHERE id=?", (user_id,)).fetchone())

    def reset_password(self, actor_id, user_id, password):
        if actor_id == user_id:
            raise AuthError("请通过“修改密码”更新自己的密码")
        hashed = password_hash(password)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self._admin_target(db, actor_id, user_id)
            self._local_management(target)
            if target["auth_provider"] != "local":
                raise AuthError("请在 GitLab 中修改该账号的密码")
            db.execute("UPDATE accounts SET password_hash=? WHERE id=?", (hashed, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def delete_user(self, actor_id, user_id):
        if actor_id == user_id:
            raise AuthError("不能删除自己的账号")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self._admin_target(db, actor_id, user_id)
            self._local_management(target)
            if target["admin"] and target["active"]:
                count = db.execute("SELECT COUNT(*) FROM accounts WHERE admin=1 AND active=1").fetchone()[0]
                if count <= 1:
                    raise AuthError("必须保留至少一个有效管理员")
            # Creative assets retain their immutable owner ID; deletion never gives
            # a subsequently recreated username access to the previous account.
            db.execute("DELETE FROM accounts WHERE id=?", (user_id,))

    def login(self, username, password, peer=""):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 256:
            raise AuthError("账号或密码错误")
        key = hashlib.sha256((peer + "\0" + username).encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            row = db.execute("SELECT * FROM login_attempts WHERE key=?", (key,)).fetchone()
            if row and row["until"] > now and row["failures"] >= 10:
                raise AuthError("登录尝试过于频繁，请 10 分钟后重试")
            account = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
        # Keep unknown-account timing similar without saving a second default credential.
        dummy = "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32
        valid = password_matches(password, account["password_hash"] if account else dummy)
        if not valid or account is None or not account["active"] or account["auth_provider"] != "local":
            with self.connection() as db:
                db.execute("INSERT INTO login_attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET "
                    "failures=CASE WHEN until<? THEN 1 ELSE failures+1 END, until=?", (key, now + 600, now, now + 600))
            raise AuthError("账号或密码错误")
        with self.connection() as db:
            db.execute("DELETE FROM login_attempts WHERE key=?", (key,))
        return self._public(account), self.new_session(account["id"])

    def new_session(self, user_id):
        raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), user_id, csrf, time.time() + SESSION_SECONDS))
        return {"token": raw, "csrf": csrf}

    def get_session(self, raw):
        if not isinstance(raw, str) or len(raw) > 128:
            return None
        with self.connection() as db:
            row = db.execute("SELECT a.*,s.csrf,s.expires FROM sessions s JOIN accounts a ON a.id=s.user_id "
                "WHERE token_hash=? AND expires>? AND a.active=1", (hashlib.sha256(raw.encode()).hexdigest(), time.time())).fetchone()
        return {"user": self._public(row), "csrf": row["csrf"], "expires": row["expires"]} if row else None

    def logout(self, token):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self, user_id, old_password, new_password):
        with self.connection() as db:
            row = db.execute("SELECT password_hash,auth_provider FROM accounts WHERE id=?", (user_id,)).fetchone()
            if row and row["auth_provider"] != "local":
                raise AuthError("请在 GitLab 中修改该账号的密码")
            if not row or not password_matches(old_password, row["password_hash"]):
                raise AuthError("当前密码不正确")
            hashed = password_hash(new_password)
            db.execute("UPDATE accounts SET password_hash=? WHERE id=?", (hashed, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def authenticate(self, request):
        raw = request.cookies.get(COOKIE, "")
        session = self.get_session(raw)
        request.state.community_session = session
        request.state.user = session["user"] if session else None
        return request.state.user

    def validate_csrf(self, request, token=None):
        session = self.get_session(request.cookies.get(COOKIE, ""))
        supplied = token if token is not None else (request.headers.get("x-community-csrf") or request.headers.get("x-csrf-token", ""))
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        return bool(session and isinstance(supplied, str) and hmac.compare_digest(session["csrf"], supplied)
                    and (origin is None or origin == expected_origin))


def authenticate(request, auth=None):
    """Root middleware can pass its shared AuthStore explicitly; returns user or None."""
    if isinstance(request, dict):
        from starlette.requests import Request
        request = Request(request)
    store = auth or getattr(request.app.state, "auth", None)
    if store is None:
        raise RuntimeError("Set app.state.auth=AuthStore(data_dir), or pass auth explicitly")
    return store.authenticate(request)

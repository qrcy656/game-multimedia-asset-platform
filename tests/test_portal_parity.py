"""Portal parity and identity upgrades, using isolated data and mock providers."""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from portal import create_app
from portal.auth import AuthError, AuthStore, COOKIE, password_hash
from portal.registry import Registry


def sign_in(client, username="admin", password="12345678"):
    result = client.post("/api/login", json={"username": username, "password": password})
    assert result.status_code == 200, result.text
    return result.json(), {"X-Community-CSRF": result.json()["csrf"]}


def member(client, headers, username="writer"):
    result = client.post("/api/users", headers=headers,
        json={"username": username, "password": "member-password", "name": "Writer"})
    assert result.status_code == 201, result.text
    return result.json()


def test_old_account_schema_upgrades_without_replacing_identity_or_password(tmp_path):
    with sqlite3.connect(tmp_path / "accounts.sqlite3") as db:
        db.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,"
                   "password_hash TEXT NOT NULL,admin INTEGER NOT NULL,created REAL NOT NULL)")
        db.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)",
                   ("existing-owner", "writer", "Existing", password_hash("existing-password"), 1, 1))
    store = AuthStore(tmp_path)
    user, _ = store.login("writer", "existing-password")
    assert user["id"] == "existing-owner" and user["active"] and user["auth_provider"] == "local"
    assert len(store.list_users()) == 1
    assert AuthStore(tmp_path).login("writer", "existing-password")[0] == user


def test_suspend_restore_revoke_sessions_and_keep_same_owner(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as writer:
        _, headers = sign_in(admin)
        user = member(admin, headers)
        signed, _ = sign_in(writer, "writer", "member-password")
        result = admin.patch(f'/api/users/{user["id"]}', headers=headers, json={"active": False})
        assert result.status_code == 200 and not result.json()["active"]
        assert writer.get("/api/session").status_code == 401
        assert writer.post("/api/login", json={"username": "writer", "password": "member-password"}).status_code == 400
        restored = admin.patch(f'/api/users/{user["id"]}', headers=headers, json={"active": True})
        assert restored.status_code == 200 and restored.json()["id"] == signed["user"]["id"]
        assert sign_in(writer, "writer", "member-password")[0]["user"]["id"] == user["id"]


def test_admin_password_reset_and_privilege_change_revoke_existing_sessions(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as writer:
        _, headers = sign_in(admin)
        user = member(admin, headers)
        sign_in(writer, "writer", "member-password")
        assert admin.patch(f'/api/users/{user["id"]}', headers=headers, json={"admin": True}).status_code == 200
        assert writer.get("/api/session").status_code == 401
        sign_in(writer, "writer", "member-password")
        assert writer.get("/api/users").status_code == 200
        reset = admin.post(f'/api/users/{user["id"]}/password', headers=headers, json={"new_password": "changed-password"})
        assert reset.status_code == 200
        assert writer.get("/api/session").status_code == 401
        assert writer.post("/api/login", json={"username": "writer", "password": "member-password"}).status_code == 400
        sign_in(writer, "writer", "changed-password")
        assert admin.patch(f'/api/users/{user["id"]}', headers=headers, json={"admin": False}).status_code == 200
        assert writer.get("/api/session").status_code == 401
        sign_in(writer, "writer", "changed-password")
        assert writer.get("/api/users").status_code == 403


def test_delete_preserves_assets_and_recreated_name_gets_new_owner_id(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as writer:
        _, headers = sign_in(admin)
        user = member(admin, headers)
        sign_in(writer, "writer", "member-password")
        asset = tmp_path / "creative-asset-owner.txt"
        asset.write_text(user["id"])
        assert admin.delete(f'/api/users/{user["id"]}', headers=headers).status_code == 200
        assert writer.get("/api/session").status_code == 401
        recreated = member(admin, headers)
        assert recreated["id"] != user["id"] and asset.read_text() == user["id"]


def test_account_mutations_require_admin_csrf_and_preserve_self(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as writer:
        own, headers = sign_in(admin)
        user = member(admin, headers)
        _, writer_headers = sign_in(writer, "writer", "member-password")
        for method, suffix, payload in [("PATCH", "", {"admin": True}), ("POST", "/password", {"new_password": "new-password"}), ("DELETE", "", None)]:
            path = f'/api/users/{user["id"]}{suffix}'
            assert writer.request(method, path, headers=writer_headers, json=payload).status_code == 403
            assert admin.request(method, path, json=payload).status_code == 403
            assert admin.request(method, path, headers={**headers, "Origin": "https://evil.invalid"}, json=payload).status_code == 403
        own_path = f'/api/users/{own["user"]["id"]}'
        for change in ({"active": False}, {"admin": False}, {"active": "false"}, {"username": "other"}):
            assert admin.patch(own_path, headers=headers, json=change).status_code == 400
        assert admin.delete(own_path, headers=headers).status_code == 400
        assert admin.post(own_path + "/password", headers=headers, json={"new_password": "new-password"}).status_code == 400
        assert admin.get("/api/session").status_code == 200


@pytest.mark.parametrize("provider_fails", [False, True])
def test_logout_revokes_locally_before_external_hook_and_survives_provider_outage(tmp_path, provider_fails):
    app = create_app(tmp_path)
    seen = []
    async def external_logout(token):
        assert app.state.auth.get_session(token) is None
        seen.append(token)
        if provider_fails:
            raise RuntimeError("provider-token-must-not-leak")
    app.state.external_session_logout = external_logout
    with TestClient(app) as client:
        _, headers = sign_in(client)
        token = client.cookies.get(COOKIE)
        result = client.post("/api/logout", headers=headers, json={})
        assert result.status_code == 200 and result.json() == {"ok": True}
        assert seen == [token] and client.cookies.get(COOKIE) is None
        assert app.state.auth.get_session(token) is None
        assert client.get("/api/session").status_code == 401
        assert "provider-token" not in result.text and token not in result.text


@pytest.mark.parametrize("provider_fails", [False, True])
def test_successful_login_revokes_replaced_external_session_without_breaking_new_login(tmp_path, provider_fails):
    app = create_app(tmp_path)
    seen = []
    async def external_logout(token):
        assert app.state.auth.get_session(token) is None
        seen.append(token)
        if provider_fails:
            raise RuntimeError("provider-token-must-not-leak")
    app.state.external_session_logout = external_logout
    with TestClient(app) as client:
        original, _ = sign_in(client)
        old = client.cookies.get(COOKIE)
        signed, _ = sign_in(client)
        new = client.cookies.get(COOKIE)
        assert seen == [old] and old != new
        assert app.state.auth.get_session(old) is None
        assert app.state.auth.get_session(new)["user"]["id"] == original["user"]["id"]
        assert signed["user"]["id"] == original["user"]["id"]
        assert client.get("/api/session").status_code == 200


def test_external_identity_never_matches_local_username_and_cannot_password_login(tmp_path):
    store = AuthStore(tmp_path)
    admin, _ = store.login("admin", "12345678")
    external = store.get_or_create_external("gitlab", "instance-a:1", "GitLab Administrator", "admin")
    assert external["id"] != admin["id"] and not external["admin"] and external["auth_provider"] == "gitlab"
    assert store.get_or_create_external("gitlab", "instance-a:1", "Renamed upstream", "new-name")["id"] == external["id"]
    other = store.get_or_create_external("gitlab", "instance-b:1", "Other instance", "admin")
    assert other["id"] != external["id"]
    with pytest.raises(AuthError):
        store.login(external["username"], "12345678")
    with pytest.raises(AuthError, match="GitLab"):
        store.reset_password(admin["id"], external["id"], "new-password")
    with pytest.raises(AuthError, match="GitLab"):
        store.change_password(external["id"], "", "new-password")


def test_external_accounts_are_managed_upstream_and_keep_their_identity(tmp_path):
    store = AuthStore(tmp_path)
    admin, _ = store.login("admin", "12345678")
    external = store.get_or_create_external("gitlab", "instance:1", "Writer", "writer")
    session = store.new_session(external["id"])
    with pytest.raises(AuthError, match="GitLab"):
        store.update_user(admin["id"], external["id"], {"active": False})
    # A legacy locally disabled record stays disabled until a new successful
    # upstream identity check explicitly synchronizes it.
    with store.connection() as db:
        db.execute('UPDATE accounts SET active=0 WHERE id=?', (external['id'],))
    assert store.get_session(session["token"]) is None
    with pytest.raises(AuthError, match="停用"):
        store.get_or_create_external("gitlab", "instance:1", "Writer", "writer")
    assert len(store.list_users()) == 2
    assert store.get_or_create_external("gitlab", "instance:1", "Writer", "writer", admin=False)["id"] == external["id"]


def test_explicit_gitlab_link_preserves_local_identity_privilege_and_password(tmp_path):
    store = AuthStore(tmp_path)
    admin, _ = store.login("admin", "12345678")
    linked = store.link_external(admin["id"], "gitlab", "instance:7")
    assert linked == admin
    assert store.link_external(admin["id"], "gitlab", "instance:7") == admin
    external_login = store.get_or_create_external("gitlab", "instance:7", "Different display", "someone")
    assert external_login["id"] == admin["id"] and external_login["admin"]
    assert store.login("admin", "12345678")[0]["id"] == admin["id"]
    store.change_password(admin["id"], "12345678", "changed-password")
    assert store.login("admin", "changed-password")[0]["id"] == admin["id"]


def test_gitlab_link_rejects_ownership_collisions_rebinding_and_inactive_users(tmp_path):
    store = AuthStore(tmp_path)
    admin, _ = store.login("admin", "12345678")
    member = store.create_user("writer", "member-password")
    store.link_external(admin["id"], "gitlab", "instance:1")
    with pytest.raises(AuthError, match="关联"):
        store.link_external(member["id"], "gitlab", "instance:1")
    with pytest.raises(AuthError, match="关联"):
        store.link_external(admin["id"], "gitlab", "instance:2")
    store.update_user(admin["id"], member["id"], {"active": False})
    with pytest.raises(AuthError, match="失效"):
        store.link_external(member["id"], "gitlab", "instance:3")
    assert store.get_or_create_external("gitlab", "instance:1", "Writer", "writer")["id"] == admin["id"]


@pytest.mark.parametrize("declares_training,validates_training,expected", [(False, True, False), (True, False, False), (True, True, True)])
def test_music_training_requires_explicit_declaration_and_validation(tmp_path, declares_training, validates_training, expected):
    calls = []
    def provider(request):
        if request.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"api_version": "fantasy-v1", "model": "music-test",
                "capabilities": ["music", "music_train"] if declares_training else ["music"],
                "input_schema": {"type": "object"}, "output_schema": {"type": "object"}})
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["validate_only"] is True
        return httpx.Response(200, json={"valid": validates_training if payload["kind"] == "music_train" else True,
            "kind": payload["kind"], "model": "music-test"})
    registry = Registry(tmp_path, transport=httpx.MockTransport(provider))
    profile = registry.save_profile({"name": "Music", "kind": "music", "protocol": "rest", "model": "music-test", "base_url": "https://provider.invalid"})
    result = asyncio.run(registry.verify(profile["id"]))
    assert result["verified"] and ("music_train" in result["capabilities"]) is expected
    assert any(call["kind"] == "music_train" for call in calls) is declares_training
    registry.bind("music", "music", profile["id"])


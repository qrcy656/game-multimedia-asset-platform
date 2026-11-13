import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from fantasy_mcp.common import AdapterError
from fantasy_mcp.config import Settings
from fantasy_mcp.private import PrivateBackend


def settings(tmp_path):
    return Settings(portal_url="https://fantasy.test:18443", username="worker", password="test-password",
                    output_dir=tmp_path, upload_roots=(tmp_path,))


class LoginFixture:
    def __init__(self, config):
        self.config = config
        self.requests = []
        self.password_posts = 0

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/login":
            return httpx.Response(303, headers={"location": self.config.gitlab_url + "/oauth/authorize?state=login-state"})
        if path == "/oauth/authorize":
            if "_gitlab_session=browser-cookie" not in request.headers.get("cookie", ""):
                return httpx.Response(302, headers={"location": "/users/sign_in"})
            callback = self.config.portal_url + "/auth/callback?state=login-state&amp;code=one-use-code"
            return httpx.Response(200, text=f'<a href="{callback}">Continue</a>')
        if path == "/users/sign_in":
            if request.method == "GET":
                return httpx.Response(200, text='<form action="/users/sign_in" method="post">'
                                      '<input name="authenticity_token" value="form-csrf">'
                                      '<input name="user[login]"><input name="user[password]" type="password"></form>')
            fields = parse_qs(request.content.decode())
            assert fields["user[login]"] == ["worker"]
            assert fields["user[password]"] == ["test-password"]
            assert fields["authenticity_token"] == ["form-csrf"]
            assert request.headers["origin"] == self.config.gitlab_url
            self.password_posts += 1
            return httpx.Response(302, headers={"location": "/oauth/authorize?state=login-state",
                                               "set-cookie": "_gitlab_session=browser-cookie; Path=/; Secure; HttpOnly"})
        if path == "/auth/callback":
            assert request.url.params["state"] == "login-state"
            return httpx.Response(303, headers={"location": self.config.portal_url + "/",
                                               "set-cookie": "fantasy_session=central-cookie; Path=/; Secure; HttpOnly"})
        if path == "/":
            return httpx.Response(200, text="Portal")
        if path == "/api/catalog":
            assert "fantasy_session=central-cookie" in request.headers["cookie"]
            return httpx.Response(200, json={"characters": [], "motions": []})
        raise AssertionError(f"Unexpected request {request.method} {path}")


@pytest.mark.asyncio
async def test_native_gitlab_sso_follows_exact_fallback_callback(tmp_path):
    config = settings(tmp_path)
    fixture = LoginFixture(config)
    backend = PrivateBackend(config, httpx.MockTransport(fixture))
    try:
        assert await backend.list_references() == {"characters": [], "motions": []}
        assert await backend.list_references() == {"characters": [], "motions": []}
        assert fixture.password_posts == 1
        assert all(b"test-password" not in req.content for req in fixture.requests if req.url.path != "/users/sign_in")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_login_rejects_redirect_before_sending_cookies_or_credentials(tmp_path):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://attacker.test/users/sign_in"})
    backend = PrivateBackend(settings(tmp_path), httpx.MockTransport(handler))
    try:
        with pytest.raises(AdapterError, match="origins"):
            await backend.list_references()
        assert len(seen) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_login_refuses_password_form_destination_at_other_trusted_service(tmp_path):
    config = settings(tmp_path)
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path == "/login":
            return httpx.Response(302, headers={"location": config.gitlab_url + "/users/sign_in"})
        return httpx.Response(200, text=f'<form method="post" action="{config.portal_url}/users/sign_in">'
                              '<input name="authenticity_token" value="csrf"><input name="user[login]">'
                              '<input name="user[password]"></form>')
    backend = PrivateBackend(config, httpx.MockTransport(handler))
    try:
        with pytest.raises(AdapterError, match="destination"):
            await backend.list_references()
        assert all(req.method == "GET" for req in seen)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_oauth_callback_requires_matching_state(tmp_path):
    config = settings(tmp_path)
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path == "/login":
            return httpx.Response(302, headers={"location": config.gitlab_url + "/oauth/authorize?state=expected"})
        return httpx.Response(302, headers={"location": config.portal_url + "/auth/callback?state=wrong&code=code"})
    backend = PrivateBackend(config, httpx.MockTransport(handler))
    try:
        with pytest.raises(AdapterError, match="callback"):
            await backend.list_references()
        assert len(seen) == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_media_upload_sends_csrf_and_module_origin(tmp_path):
    config = settings(tmp_path)
    photo = tmp_path / "reference.png"
    photo.write_bytes(b"test-image")
    received = []
    def handler(request):
        received.append(request)
        if request.url.path == "/api/session":
            assert request.extensions["timeout"]["read"] == 30
            return httpx.Response(200, json={"csrf": "module-csrf"})
        assert request.method == "POST"
        assert request.extensions["timeout"]["read"] == 300
        assert request.headers["origin"] == config.module_url("model3d")
        assert request.headers["x-csrf-token"] == "module-csrf"
        assert "multipart/form-data" in request.headers["content-type"]
        assert b"test-image" in request.content and b'trellis2' in request.content
        return httpx.Response(201, json={"id": "job-1", "artifacts": [], "token": "must-not-return"})
    backend = PrivateBackend(config, httpx.MockTransport(handler))
    backend._authenticated = True
    try:
        result = await backend.submit_job("model3d", "trellis2", {"steps": 12}, photo)
        assert result == {"id": "job-1", "artifacts": []}
        assert len(received) == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_uncertain_media_write_is_never_retried(tmp_path):
    writes = []
    def handler(request):
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"csrf": "csrf"})
        writes.append(request)
        assert request.extensions["timeout"]["read"] == 300
        raise httpx.ReadTimeout("upstream-token-must-not-leak", request=request)
    backend = PrivateBackend(settings(tmp_path), httpx.MockTransport(handler))
    backend._authenticated = True
    try:
        with pytest.raises(AdapterError, match="unknown") as error:
            await backend.submit_job("image", "qwen-image21", {"prompt": "stone"})
        assert "upstream-token" not in str(error.value)
        assert len(writes) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_animation_submission_preserves_recovery_key(tmp_path):
    config = settings(tmp_path)
    def handler(request):
        payload = json.loads(request.content)
        assert payload["idempotency_key"] == "specific-request"
        assert request.headers["idempotency-key"] == "specific-request"
        assert request.headers["origin"] == config.module_url("animation")
        assert "x-csrf-token" not in request.headers
        return httpx.Response(202, json={"id": "animation-job", "status": "queued"})
    backend = PrivateBackend(config, httpx.MockTransport(handler))
    backend._authenticated = True
    try:
        assert (await backend.submit_animation({"character_id": "char", "idempotency_key": "specific-request"}))["idempotency_key"] == "specific-request"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_read_request_reauthenticates_once_after_401(tmp_path):
    reads = []
    def handler(request):
        reads.append(request)
        return httpx.Response(401) if len(reads) == 1 else httpx.Response(200, json={"id": "job-1"})
    backend = PrivateBackend(settings(tmp_path), httpx.MockTransport(handler))
    login_calls = []
    async def ensure_login():
        if not backend._authenticated:
            login_calls.append(True)
            backend._authenticated = True
    backend._ensure_login = ensure_login
    backend._authenticated = True
    try:
        assert await backend.get_job("model3d", "job-1") == {"id": "job-1"}
        assert len(reads) == 2 and len(login_calls) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_animation_downloads_use_published_file_urls(tmp_path):
    config = settings(tmp_path)
    def handler(request):
        if request.url.path == "/api/jobs/job-1":
            return httpx.Response(200, json={"id": "job-1", "asset_id": "asset-1"})
        assert request.url.path == "/api/assets/asset-1"
        return httpx.Response(200, json={"id": "asset-1", "export_url": "/api/assets/asset-1/export.zip",
                                        "files": {"spritesheet": "/files/assets/asset-1/sheet.png", "frames": ["/files/assets/asset-1/frames/001.png"]}})
    backend = PrivateBackend(config, httpx.MockTransport(handler))
    backend._authenticated = True
    try:
        result = await backend.get_downloads("animation", job_id="job-1")
        assert [r["name"] for r in result] == ["animation-asset-1.zip", "sheet.png", "001.png"]
        assert all(r["url"].startswith(config.module_url("animation")) for r in result)
    finally:
        await backend.close()


@pytest.mark.parametrize("url", ["https://attacker.test/file.glb", "/logout", "/api/jobs/job-1/files/../logout", "/api/jobs/job-1/files/%2e%2e/logout", "//attacker.test/api/jobs/j/files/a.glb"])
@pytest.mark.asyncio
async def test_download_url_rejects_external_nonartifact_and_traversal(tmp_path, url):
    backend = PrivateBackend(settings(tmp_path), httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        with pytest.raises(AdapterError):
            backend._download_url("model3d", url)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_create_character_does_not_approve_implicitly(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/characters" and request.method == "POST"
        return httpx.Response(201, json={"id": "new-char", "approved": False})
    backend = PrivateBackend(settings(tmp_path), httpx.MockTransport(handler))
    backend._authenticated = True
    try:
        assert (await backend.create_character({"name": "Draft", "images": [{"path": "characters/uploads/ref.png", "label": "front"}]}))["approved"] is False
        assert len(requests) == 1
    finally:
        await backend.close()

import asyncio
import hashlib
import json
import socket
from urllib.parse import parse_qs

import anyio
import httpx
import pytest

from fantasy_mcp.common import AdapterError
from fantasy_mcp.community import CommunityBackend
from fantasy_mcp.config import Settings


def settings(tmp_path, **changes):
    return Settings(edition="community", portal_url="http://127.0.0.1:28082", origin="http://localhost:18082",
                    username="worker", password="test-password", output_dir=tmp_path,
                    upload_roots=(tmp_path,), **changes)


class CommunityFixture:
    def __init__(self):
        self.requests = []
        self.logins = 0
        self.after_auth = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.headers["host"] == "localhost:18082"
        if request.url.path == "/api/login":
            assert json.loads(request.content) == {"username": "worker", "password": "test-password"}
            assert request.headers["origin"] == "http://localhost:18082"
            self.logins += 1
            return httpx.Response(200, json={"csrf": "portal-csrf", "user": {"admin": False}},
                                  headers={"set-cookie": "fantasy_community_session=community-cookie; HttpOnly; Path=/"})
        assert "fantasy_community_session=community-cookie" in request.headers.get("cookie", "")
        if request.method == "POST":
            assert request.headers["x-community-csrf"] == "portal-csrf"
            assert request.headers["origin"] == "http://localhost:18082"
            if request.url.path.split("/")[1] in {"model3d", "image", "speech", "music"}:
                assert request.headers["x-csrf-token"] == "module-csrf"
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"csrf": "portal-csrf", "user": {"admin": False}})
        if request.url.path in {f"/{module}/api/session" for module in ("model3d", "image", "speech", "music")}:
            return httpx.Response(200, json={"csrf": "module-csrf"})
        if self.after_auth:
            return self.after_auth(request)
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_gitlab_pat_login_keeps_csrf_cookie_and_never_exposes_credential(tmp_path):
    secret = 'fixture-gitlab-personal-token'
    observed = []
    def provider(request):
        observed.append(request)
        if request.url.path == '/api/login':
            assert json.loads(request.content) == {'credential_type': 'gitlab_pat', 'username': 'admin', 'token': secret}
            return httpx.Response(200, json={'csrf': 'portal-csrf', 'user': {'admin': True}},
                headers={'set-cookie': 'fantasy_community_session=community-cookie; HttpOnly; Path=/'})
        assert 'community-cookie' in request.headers.get('cookie', '')
        return httpx.Response(200, json={'csrf': 'portal-csrf', 'user': {'admin': True}, 'gitlab_token': secret})
    config = Settings(edition='community', portal_url='http://localhost:18082', username='admin',
                      credential_type='gitlab_pat', gitlab_token=secret, output_dir=tmp_path)
    assert secret not in repr(config)
    backend = CommunityBackend(config, httpx.MockTransport(provider))
    try:
        await backend._ensure_login()
        assert backend._authenticated and backend._portal_csrf == 'portal-csrf'
        from fantasy_mcp.common import redact
        result = redact(await backend._api('portal', 'GET', '/api/session'))
        assert secret not in json.dumps(result)
        assert len([r for r in observed if r.url.path == '/api/login']) == 1
    finally:
        await backend.close()


def test_gitlab_pat_credentials_file_and_environment_are_explicit_and_private(tmp_path, monkeypatch):
    monkeypatch.delenv('FANTASY_USERNAME', raising=False)
    monkeypatch.delenv('FANTASY_GITLAB_TOKEN', raising=False)
    path = tmp_path / 'worker.credentials.json'
    path.write_text(json.dumps({'credential_type': 'gitlab_pat', 'username': 'worker', 'token': 'fixture-pat'}))
    config = Settings(edition='community', portal_url='http://localhost:18082', credentials_file=path)
    assert config.community_login_payload() == {'credential_type': 'gitlab_pat', 'username': 'worker', 'token': 'fixture-pat'}
    monkeypatch.setenv('FANTASY_GITLAB_TOKEN', 'env-pat')
    assert config.community_login_payload()['token'] == 'env-pat'
    assert 'fixture-pat' not in repr(config) and 'env-pat' not in repr(config)
    with pytest.raises(ValueError, match='community'):
        Settings(edition='private', credential_type='gitlab_pat')


@pytest.mark.asyncio
async def test_pat_login_failure_never_returns_provider_body_or_token(tmp_path):
    secret = 'fixture-gitlab-secret-token'
    backend = CommunityBackend(Settings(edition='community', portal_url='http://localhost:18082',
        credential_type='gitlab_pat', gitlab_token=secret, output_dir=tmp_path),
        httpx.MockTransport(lambda request: httpx.Response(401, json={'detail': secret})))
    try:
        with pytest.raises(AdapterError) as failure:
            await backend._ensure_login()
        assert secret not in str(failure.value)
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("assisted_failure", [False, True])
async def test_capabilities_distinguish_animation_direct_and_assisted_readiness(tmp_path, assisted_failure):
    fixture = CommunityFixture()
    direct = {"status": "ok", "generation_ready": True, "generation_missing_services": []}
    assisted = {"status": "ok", "generation_ready": False,
                "generation_missing_services": ["vision"],
                "generation_message": "animation/vision requires a verified vision API"}

    def answer(request):
        if request.url.path == "/api/workspace":
            return httpx.Response(200, json={"modules": {"animation": {"status": "ready"}}})
        if request.url.path == "/animation/api/health":
            mode = request.url.params["prompt_mode"]
            assert mode in {"direct", "assisted"}
            if mode == "direct":
                return httpx.Response(200, json=direct)
            if assisted_failure:
                return httpx.Response(503, json={"detail": "provider-sensitive-data"})
            return httpx.Response(200, json=assisted)
        return httpx.Response(404)

    fixture.after_auth = answer
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(fixture))
    try:
        animation = (await backend.capabilities())["modules"]["animation"]
        assert animation["available"] is True
        assert animation["health"] == direct
        assert animation["assisted_health"]["generation_ready"] is False
        if assisted_failure:
            assert animation["assisted_health"]["available"] is False
            assert "provider-sensitive-data" not in json.dumps(animation)
        else:
            assert animation["assisted_health"] == assisted
        modes = [request.url.params["prompt_mode"] for request in fixture.requests
                 if request.url.path == "/animation/api/health"]
        assert modes == ["direct", "assisted"]
        assert fixture.logins == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_login_and_two_csrf_layers_never_escape_results(tmp_path):
    fixture = CommunityFixture()
    fixture.after_auth = lambda request: httpx.Response(201, json={"id": "job1", "csrf": "not-public"})
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(fixture))
    try:
        assert await backend.submit_job("speech", "voice-profile", {"text": "hello"}) == {"id": "job1"}
        submitted = [r for r in fixture.requests if r.url.path == "/speech/api/jobs"]
        assert len(submitted) == 1
        form = parse_qs(submitted[0].content.decode())
        assert json.loads(form["params"][0]) == {"text": "hello"}
        assert fixture.logins == 1
        assert all(b"test-password" not in r.content for r in fixture.requests if r.url.path != "/api/login")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_read_reauthenticates_once_after_expiry(tmp_path):
    fixture = CommunityFixture()
    calls = 0
    def answer(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401) if calls == 1 else httpx.Response(200, json={"items": []})
    fixture.after_auth = answer
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(fixture))
    try:
        assert await backend.list_jobs("animation") == {"items": []}
        assert fixture.logins == 2 and calls == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 403, 503, "timeout"])
async def test_mutation_failure_is_never_automatically_replayed(tmp_path, failure):
    fixture = CommunityFixture()
    calls = 0
    def answer(request):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("provider-sensitive-data", request=request)
        return httpx.Response(failure, json={"detail": "provider-sensitive-data"})
    fixture.after_auth = answer
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(fixture))
    try:
        with pytest.raises(AdapterError) as error:
            await backend.submit_job("image", "image-profile", {"prompt": "tree"})
        assert "provider-sensitive-data" not in str(error.value)
        assert calls == 1 and fixture.logins == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_animation_paths_idempotency_and_files_prefix(tmp_path):
    fixture = CommunityFixture()
    def answer(request):
        if request.url.path == "/animation/api/animation/generate":
            body = json.loads(request.content)
            assert body["idempotency_key"] == request.headers["idempotency-key"] == "stable-key"
            assert "x-csrf-token" not in request.headers
            return httpx.Response(202, json={"id": "job1"})
        if request.url.path == "/animation/api/jobs/job1":
            return httpx.Response(200, json={"id": "job1", "asset_id": "asset1"})
        if request.url.path == "/animation/api/assets/asset1":
            return httpx.Response(200, json={"export_url": "/animation/api/assets/asset1/export.zip",
                "files": {"spritesheet": "/animation/files/assets/asset1/sheet.png"}})
        raise AssertionError(request.url)
    fixture.after_auth = answer
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(fixture))
    try:
        task = {"character_id": "hero", "prompt_mode": "direct", "video_prompt": "Walk", "idempotency_key": "stable-key"}
        assert (await backend.submit_animation(task))["idempotency_key"] == "stable-key"
        downloads = await backend.get_downloads("animation", "job1")
        assert [item["url"] for item in downloads] == [
            "http://127.0.0.1:28082/animation/api/assets/asset1/export.zip",
            "http://127.0.0.1:28082/animation/files/assets/asset1/sheet.png"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_default_profile_uses_discovered_id_and_ambiguous_default_fails(tmp_path):
    fixture = CommunityFixture()
    models = [{"id": "profile-a", "ready": True, "enabled": True, "default": True}]
    def answer(request):
        if request.url.path.endswith("/api/config"):
            return httpx.Response(200, json={"models": models})
        assert parse_qs(request.content.decode())["model_id"] == ["profile-a"]
        return httpx.Response(201, json={"id": "job1"})
    fixture.after_auth = answer
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(fixture))
    try:
        assert (await backend.submit_job("image", None, {"prompt": "tree"}))["id"] == "job1"
        models.append({"id": "profile-b", "ready": True, "enabled": True, "default": True})
        with pytest.raises(AdapterError, match="explicit community model_id"):
            await backend.submit_job("image", None, {"prompt": "tree"})
        assert len([r for r in fixture.requests if r.method == "POST" and r.url.path.endswith("/api/jobs")]) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_same_origin_download_resolves_by_module_path_and_hash(tmp_path):
    fixture = CommunityFixture()
    fixture.after_auth = lambda request: httpx.Response(200, content=b"GLB-BYTES", headers={"content-type": "model/gltf-binary"})
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(fixture))
    try:
        target = tmp_path / "model.glb"
        result = await backend.download("http://127.0.0.1:28082/model3d/api/jobs/job1/files/model.glb", target)
        assert result["sha256"] == hashlib.sha256(b"GLB-BYTES").hexdigest()
        assert target.read_bytes() == b"GLB-BYTES"
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://evil.example/model3d/api/jobs/j/files/model.glb",
    "/image/api/jobs/j/files/model.glb",
    "/model3d/api/jobs/j/files/%252e%252e/secret",
    "/api/session",
])
async def test_download_refuses_cross_module_and_encoded_traversal(tmp_path, url):
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(lambda req: pytest.fail("No HTTP request expected")))
    try:
        with pytest.raises(AdapterError):
            backend._download_url("model3d", url)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_login_redirect_never_sends_password_to_redirect_target(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://evil.example/login"})
    backend = CommunityBackend(settings(tmp_path), httpx.MockTransport(handler))
    try:
        with pytest.raises(AdapterError, match="login returned HTTP 302"):
            await backend.list_references()
        assert len(requests) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_tunnel_never_adopts_an_existing_listener(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        config = Settings(edition="community", portal_url=f"http://127.0.0.1:{port}",
                          username="worker", password="test-password", output_dir=tmp_path,
                          ssh_tunnel={"target": "worker@server", "local_port": port, "remote_port": 18082})
        backend = CommunityBackend(config)
        try:
            with pytest.raises(AdapterError, match="already occupied"):
                await backend._ensure_tunnel()
            assert backend._tunnel is None
            assert listener.getsockname()[1] == port
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_mcp_cancel_scope_cleans_up_ssh_spawn_in_flight(tmp_path, monkeypatch):
    calls = []
    class Process:
        returncode = None
        def terminate(self):
            calls.append("terminate")
            self.returncode = 0
        async def wait(self):
            await asyncio.sleep(0)
            calls.append("wait")
            return self.returncode
    async def spawn(*args, **kwargs):
        await asyncio.sleep(0.03)
        calls.append("created")
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = Settings(edition="community", portal_url=f"http://127.0.0.1:{port}",
                      username="worker", password="test-password", output_dir=tmp_path,
                      ssh_tunnel={"target": "worker@server", "local_port": port, "remote_port": 18082})
    backend = CommunityBackend(config)
    try:
        with anyio.move_on_after(0.01) as cancelled:
            await backend._ensure_tunnel()
        assert cancelled.cancel_called
        assert calls == ["created", "terminate", "wait"]
        assert backend._tunnel is None
    finally:
        await backend.close()

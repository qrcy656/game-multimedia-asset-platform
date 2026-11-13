"""Capability boundaries and local-account isolation; no live model calls or production data."""
import asyncio
import base64
import io
import json
import sqlite3
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from portal import create_app
from portal.auth import AuthError, AuthStore, COOKIE
from portal.registry import Registry, RegistryError, _vision_probe


def run(value):
    return asyncio.run(value)


def profile(kind="llm", protocol="openai", **updates):
    return {"name": "Test API", "kind": kind, "protocol": protocol, "base_url": "https://example.invalid/v1",
            "model": "test-model", "api_key": "private-test-key", "options": {}, **updates}


def chat(content="READY"):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def registry(tmp_path, handler):
    return Registry(tmp_path, transport=httpx.MockTransport(handler))


def login(client, username="admin", password="12345678"):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"X-Community-CSRF": response.json()["csrf"]}


def test_bootstrap_once_and_password_hash(tmp_path):
    auth = AuthStore(tmp_path)
    user, session = auth.login("admin", "12345678")
    with sqlite3.connect(auth.path) as db:
        saved = db.execute("SELECT password_hash FROM accounts").fetchone()[0]
        assert "12345678" not in saved and saved.startswith("pbkdf2_sha256$")
    auth.change_password(user["id"], "12345678", "new-password")
    assert auth.get_session(session["token"]) is None
    restarted = AuthStore(tmp_path)
    with pytest.raises(AuthError):
        restarted.login("admin", "12345678")
    assert restarted.login("admin", "new-password")[0]["id"] == user["id"]


def test_login_csrf_accounts_and_permissions(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/profiles").status_code == 401
        assert client.post("/api/login", headers={"Origin": "https://evil.invalid"}, json={"username": "admin", "password": "12345678"}).status_code == 403
        headers = login(client)
        assert "HttpOnly" in client.post("/api/login", json={"username": "admin", "password": "12345678"}).headers["set-cookie"]
        headers = {"X-Community-CSRF": client.get("/api/session").json()["csrf"]}
        assert client.post("/api/profiles", json=profile()).status_code == 403
        assert client.post("/api/profiles", headers={**headers, "Origin": "https://evil.invalid"}, json=profile()).status_code == 403
        result = client.post("/api/profiles", headers=headers, json=profile())
        assert result.status_code == 201
        assert "private-test-key" not in result.text
        assert client.post("/api/users", headers=headers, json={"username": "writer", "name": "Writer", "password": "pass-123456"}).status_code == 201
        assert client.post("/api/logout", headers=headers, json={}).status_code == 200
        assert client.get("/api/session").status_code == 401
        member_headers = login(client, "writer", "pass-123456")
        assert client.get("/api/profiles").status_code == 200
        assert client.post("/api/profiles", headers=member_headers, json=profile()).status_code == 201
        assert client.post("/api/profiles", headers=member_headers, json=profile(scope='global')).status_code == 403
        assert client.get("/api/users").status_code == 403


def test_get_logout_does_not_change_session(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        login(client)
        assert client.get("/logout").status_code == 200
        assert client.get("/api/session").status_code == 200


def test_verify_generation_option_must_be_boolean(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        headers = login(client)
        item = client.post("/api/profiles", headers=headers, json=profile("image")).json()
        result = client.post(f"/api/profiles/{item['id']}/verify", headers=headers, json={"allow_generation_probe": "false"})
        assert result.status_code == 400


def test_password_rotation_invalidates_other_sessions(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as first, TestClient(app) as other:
        headers = login(first)
        login(other)
        result = first.post("/api/password", headers=headers, json={"old_password": "12345678", "new_password": "updated-password"})
        assert result.status_code == 200
        assert first.get("/api/session").status_code == 200
        assert other.get("/api/session").status_code == 401
        assert first.post("/api/logout", headers=headers, json={}).status_code == 403


def test_key_encryption_mask_preserve_and_clear(tmp_path):
    reg = registry(tmp_path, lambda req: httpx.Response(200, json=chat()))
    item = reg.save_profile(profile())
    assert "api_key" not in item and item["has_api_key"]
    assert b"private-test-key" not in reg.path.read_bytes()
    run(reg.verify(item["id"]))
    reg.bind("image", "llm", item["id"])
    assert reg.resolve("image", "llm")["api_key"] == "private-test-key"
    reg.save_profile({"name": "Renamed"}, item["id"])
    assert not reg.get_profile(item["id"])["verified"]
    with pytest.raises(RegistryError):
        reg.resolve("image", "llm")
    run(reg.verify(item["id"]))
    assert reg.resolve("image", "llm")["api_key"] == "private-test-key"
    reg.save_profile({"api_key": ""}, item["id"])
    assert not reg.get_profile(item["id"])["has_api_key"]


def test_no_binding_before_verify_and_wrong_role(tmp_path):
    reg = registry(tmp_path, lambda req: httpx.Response(200, json=chat()))
    item = reg.save_profile(profile())
    with pytest.raises(RegistryError):
        reg.bind("knowledge", "llm", item["id"])
    run(reg.verify(item["id"]))
    with pytest.raises(RegistryError):
        reg.bind("knowledge", "embedding", item["id"])
    with pytest.raises(RegistryError):
        reg.resolve("speech", "workflow", item["id"])
    reg.bind("knowledge", "llm", item["id"])
    reg.delete_profile(item["id"])
    assert reg.list_bindings() == []


def test_embedding_cannot_masquerade_as_llm(tmp_path):
    reg = registry(tmp_path, lambda req: httpx.Response(200, json={"data": [{"embedding": [.1, .2]}]}))
    item = reg.save_profile(profile())
    with pytest.raises(RegistryError, match="聊天回答"):
        run(reg.verify(item["id"]))
    assert not reg.get_profile(item["id"])["verified"]


@pytest.mark.parametrize("bad", [[], [True, 2], [1], [1, float("nan")], ["1", "2"]])
def test_invalid_embedding_vectors_rejected(tmp_path, bad):
    # json.dumps permits NaN for the mock; decoder then rejects it as a vector.
    reg = registry(tmp_path, lambda req: httpx.Response(200, text=json.dumps({"data": [{"embedding": bad}]})))
    item = reg.save_profile(profile("embedding"))
    with pytest.raises(RegistryError):
        run(reg.verify(item["id"]))


def test_embedding_and_reranker_real_structures(tmp_path):
    def handler(req):
        if req.url.path.endswith("embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [.1, .2, .3]}]})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": .95}]})
    reg = registry(tmp_path, handler)
    for kind, protocol in [("embedding", "openai"), ("rerank", "rerank")]:
        item = reg.save_profile(profile(kind, protocol))
        assert run(reg.verify(item["id"]))["verified"]
        reg.bind("knowledge", kind, item["id"])


def test_vision_content_and_one_way_compatibility(tmp_path):
    seen = []
    def handler(req):
        data = json.loads(req.content)
        seen.append(data)
        return httpx.Response(200, json=chat("BLUE"))
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile("vision"))
    result = run(reg.verify(item["id"]))
    assert result["capabilities"] == ["vision", "llm"]
    image_url = seen[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(image_url.split(",")[1]).startswith(b"\x89PNG")
    reg.bind("knowledge", "llm", item["id"])
    reg.bind("knowledge", "vision", item["id"])


def test_vision_wrong_answer_rejected(tmp_path):
    reg = registry(tmp_path, lambda req: httpx.Response(200, json=chat("RED")))
    item = reg.save_profile(profile("vision"))
    with pytest.raises(RegistryError, match="图片识别"):
        run(reg.verify(item["id"]))


def test_ollama_metadata_mismatch(tmp_path):
    reg = registry(tmp_path, lambda req: httpx.Response(200, json={"capabilities": ["embedding"]}))
    item = reg.save_profile(profile("llm", "ollama"))
    with pytest.raises(RegistryError, match="声明"):
        run(reg.verify(item["id"]))


def test_ollama_json_and_inline_image_adaptation(tmp_path):
    calls = []
    def handler(req):
        data = json.loads(req.content)
        calls.append((req.url.path, data))
        if req.url.path.endswith("show"):
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "BLUE"}})
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile("vision", "ollama", base_url="http://localhost:11434"))
    run(reg.verify(item["id"]))
    response = run(reg.invoke("knowledge", "vision", {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Describe"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _vision_probe()}}]}],
        "max_tokens": 50, "response_format": {"type": "json_object"}}, item["id"]))
    assert calls[-1][1]["format"] == "json"
    assert calls[-1][1]["options"]["num_predict"] == 50
    assert calls[-1][1]["messages"][0]["images"]
    assert response["choices"][0]["message"]["content"] == "BLUE"


def test_verification_race_cannot_validate_changed_profile(tmp_path):
    reg = None
    item = None
    def handler(req):
        reg.save_profile({"base_url": "https://other.invalid"}, item["id"])
        return httpx.Response(200, json=chat())
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile())
    with pytest.raises(RegistryError, match="检测期间"):
        run(reg.verify(item["id"]))
    assert not reg.get_profile(item["id"])["verified"]


def test_comfy_workflow_probe_and_submit(tmp_path):
    def handler(req):
        if req.url.path.endswith("system_stats"):
            return httpx.Response(200, json={"system": {}, "devices": []})
        if req.url.path.endswith("object_info"):
            return httpx.Response(200, json={"KSampler": {"input": {"required": {}}}})
        assert json.loads(req.content)["prompt"] == {"1": {"class_type": "KSampler"}}
        return httpx.Response(200, json={"prompt_id": "123"})
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile("workflow", "comfyui", model=""))
    run(reg.verify(item["id"]))
    assert run(reg.invoke("image", "workflow", {"prompt": {"1": {"class_type": "KSampler"}}}, item["id"]))["prompt_id"] == "123"


def declaration(kind="image"):
    return {"api_version": "fantasy-v1", "model": "test-model", "capabilities": [kind],
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"}}


def test_rest_typed_contract_and_async_poll(tmp_path):
    def handler(req):
        if req.url.path.endswith("capabilities"):
            return httpx.Response(200, json=declaration())
        if req.url.path.endswith("validate"):
            return httpx.Response(200, json={"valid": True, "kind": "image", "model": "test-model"})
        if req.method == "POST":
            data = json.loads(req.content)
            assert data["model"] == "test-model" and data["kind"] == "image"
            return httpx.Response(200, json={"id": "job1", "status": "queued"})
        return httpx.Response(200, json={"id": "job1", "status": "succeeded", "artifacts": []})
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile("image", "rest"))
    run(reg.verify(item["id"]))
    response = run(reg.invoke("image", "image", {"model": "untrusted", "kind": "speech", "input": {"prompt": "test"}}, item["id"]))
    assert response["id"] == "job1"
    assert run(reg.poll("image", "image", "job1", item["id"]))["status"] == "succeeded"
    with pytest.raises(RegistryError):
        run(reg.poll("image", "image", "../../private", item["id"]))


def test_rest_wrong_declared_type_rejected(tmp_path):
    reg = registry(tmp_path, lambda req: httpx.Response(200, json=declaration("embedding")))
    item = reg.save_profile(profile("music", "rest"))
    with pytest.raises(RegistryError):
        run(reg.verify(item["id"]))


def test_openai_generation_probe_requires_opt_in_and_valid_bytes(tmp_path):
    calls = []
    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("capabilities"):
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"b64_json": _vision_probe()}]})
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile("image"))
    with pytest.raises(RegistryError, match="可能产生"):
        run(reg.verify(item["id"]))
    assert not any(path.endswith("generations") for path in calls)
    assert run(reg.verify(item["id"], allow_generation_probe=True))["verified"]


def test_image_artifact_download_never_receives_api_key(tmp_path):
    def handler(req):
        if req.url.host == "cdn.invalid":
            assert "authorization" not in req.headers
            return httpx.Response(200, content=base64.b64decode(_vision_probe()))
        assert req.headers["authorization"] == "Bearer private-test-key"
        return httpx.Response(200, json={"data": [{"url": "https://cdn.invalid/p.png?signature=test"}]})
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile("image"))
    assert run(reg.verify(item["id"], allow_generation_probe=True))["verified"]


def test_speech_probe_rejects_json_200_and_accepts_wav(tmp_path):
    state = {"valid": False}
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000); audio.writeframes(b"\0\0" * 80)
    reg = registry(tmp_path, lambda req: httpx.Response(200, content=stream.getvalue() if state["valid"] else b'{"error":"not audio"}'))
    item = reg.save_profile(profile("speech"))
    with pytest.raises(RegistryError, match="音频"):
        run(reg.verify(item["id"], allow_generation_probe=True))
    state["valid"] = True
    assert run(reg.verify(item["id"], allow_generation_probe=True))["verified"]


def test_errors_do_not_disclose_provider_key(tmp_path):
    reg = registry(tmp_path, lambda req: httpx.Response(401, text="private-test-key provider secret"))
    item = reg.save_profile(profile())
    with pytest.raises(RegistryError) as result:
        run(reg.verify(item["id"]))
    assert "private-test-key" not in str(result.value)


def test_videos_explicit_capability_and_query(tmp_path):
    def handler(req):
        if req.url.path.endswith("models"):
            return httpx.Response(200, json={"data": [{"id": "test-model", "capabilities": ["video"]}]})
        return httpx.Response(200, json={"data": []})
    reg = registry(tmp_path, handler)
    item = reg.save_profile(profile("video", "videos"))
    assert run(reg.verify(item["id"]))["verified"]


@pytest.mark.parametrize("address", ["file:///etc/passwd", "https://user:password@example.invalid", "https://example.invalid?secret=abc", "http://example.invalid/#fragment", "https://example.invalid:bad"])
def test_invalid_profile_urls(tmp_path, address):
    reg = Registry(tmp_path)
    with pytest.raises(RegistryError):
        reg.save_profile(profile(base_url=address))


def test_malformed_profile_types_fail_cleanly(tmp_path):
    reg = Registry(tmp_path)
    for updates in [{"protocol": []}, {"kind": {}}, {"options": []}]:
        with pytest.raises(RegistryError):
            reg.save_profile(profile(**updates))
    with pytest.raises(RegistryError):
        reg.bind("knowledge", "llm", {})

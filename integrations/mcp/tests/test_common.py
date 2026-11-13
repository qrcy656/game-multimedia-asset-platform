"""Offline tests of credential and artifact boundaries; never submit generation jobs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from fantasy_mcp.common import (
    AdapterError,
    approved_url,
    checked_destination,
    checked_upload,
    create_http_client,
    download_to_file,
    redact,
)
from fantasy_mcp.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("FANTASY_USERNAME", raising=False)
    monkeypatch.delenv("FANTASY_PASSWORD", raising=False)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    return Settings(
        portal_url="https://fantasy.invalid:18443",
        output_dir=tmp_path / "outputs",
        upload_roots=(inputs,),
        max_download_bytes=32,
        max_upload_bytes=32,
    )


class Chunks(httpx.AsyncByteStream):
    """Responses with no Content-Length exercise the actual streamed byte limit."""

    def __init__(self, *parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


@pytest.mark.parametrize(
    "contents",
    [
        "账户：test-worker\n密码：private-test-password\n",
        "账号=test-worker\npassword=private-test-password\n",
        json.dumps({"username": "test-worker", "password": "private-test-password"}),
    ],
)
def test_credentials_read_only_configured_file_without_printing(settings, tmp_path, monkeypatch, capsys, contents):
    credential_file = tmp_path / "explicit-credentials.txt"
    credential_file.write_text(contents, encoding="utf-8-sig")
    (tmp_path / "unrelated-credentials.txt").write_text("do not read", encoding="utf-8")
    settings.credentials_file = credential_file
    original_read = Path.read_text
    reads = []

    def tracked_read(path, *args, **kwargs):
        reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read)
    assert settings.credentials() == ("test-worker", "private-test-password")
    assert reads == [credential_file]
    assert "private-test-password" not in repr(settings)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_explicit_credentials_are_not_in_settings_repr(settings):
    settings.username = "private-test-username"
    settings.password = "private-test-password"
    assert settings.credentials() == (settings.username, settings.password)
    assert settings.username not in repr(settings)
    assert settings.password not in repr(settings)


def test_missing_credentials_error_does_not_echo_file_contents(settings, tmp_path):
    credential_file = tmp_path / "credentials.txt"
    credential_file.write_text("password=private-test-password\n", encoding="utf-8")
    settings.credentials_file = credential_file
    with pytest.raises(ValueError) as error:
        settings.credentials()
    assert "private-test-password" not in str(error.value)


def test_config_relative_paths_are_anchored_to_config_file(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "credentials_file": "secrets.txt", "output_dir": "artifacts",
        "upload_roots": ["inputs"],
    }), encoding="utf-8")
    loaded = Settings.load(config_file)
    assert loaded.credentials_file == tmp_path / "secrets.txt"
    assert loaded.output_dir == tmp_path / "artifacts"
    assert loaded.upload_roots == (tmp_path / "inputs",)


def test_redact_removes_nested_tokens_but_keeps_artifact_metadata():
    response = {"job_id": "job-123", "Password": "p", "nested": [
        {"ACCESS-TOKEN": "t", "Set-Cookie": "c", "filename": "mesh.glb"},
        {"csrf_token": "s", "bytes": 12},
    ]}
    assert redact(response) == {"job_id": "job-123", "nested": [
        {"filename": "mesh.glb"}, {"bytes": 12},
    ]}


def test_upload_accepts_regular_input_inside_root(settings):
    asset = settings.upload_roots[0] / "参考图.png"
    asset.write_bytes(b"reference")
    assert checked_upload(str(asset), settings) == asset


def test_upload_does_not_allow_parent_traversal_or_sibling_prefix(settings, tmp_path):
    sibling = tmp_path / "inputs-other"
    sibling.mkdir()
    asset = sibling / "reference.png"
    asset.write_bytes(b"outside")
    for path in (asset, settings.upload_roots[0] / ".." / "inputs-other" / asset.name):
        with pytest.raises(AdapterError, match="upload_roots"):
            checked_upload(str(path), settings)


def test_upload_requires_explicit_roots(settings):
    asset = settings.upload_roots[0] / "reference.png"
    asset.write_bytes(b"image")
    settings.upload_roots = ()
    with pytest.raises(AdapterError, match="upload_roots"):
        checked_upload(str(asset), settings)


@pytest.mark.parametrize("name", [".env", "id_rsa", "id_ed25519", "tls.pem", "TLS.KEY", "auth.pfx"])
def test_upload_rejects_known_secret_files(settings, name):
    secret = settings.upload_roots[0] / name
    secret.write_bytes(b"private")
    with pytest.raises(AdapterError, match="Credential files"):
        checked_upload(str(secret), settings)


@pytest.mark.parametrize("name", [".env.local", ".env.production", "service-account.json", "credentials.txt", "private.py"])
def test_upload_requires_supported_media_extension(settings, name):
    secret = settings.upload_roots[0] / name
    secret.write_bytes(b"private")
    with pytest.raises(AdapterError):
        checked_upload(str(secret), settings)


def test_upload_rejects_configured_credentials_even_with_image_extension(settings):
    secret = settings.upload_roots[0] / "looks-like-art.png"
    secret.write_bytes(b"private")
    settings.credentials_file = secret
    with pytest.raises(AdapterError, match="credentials file"):
        checked_upload(str(secret), settings)


def test_upload_rejects_directory_and_oversized_file(settings):
    asset = settings.upload_roots[0] / "big.glb"
    asset.write_bytes(b"x" * (settings.max_upload_bytes + 1))
    for candidate in (asset, settings.upload_roots[0]):
        with pytest.raises(AdapterError, match="regular file|exceeds"):
            checked_upload(str(candidate), settings)


@pytest.mark.parametrize("subdirectory", ["../outside", "../../outside"])
def test_download_destination_rejects_directory_traversal(settings, subdirectory):
    with pytest.raises(AdapterError, match="inside output_dir"):
        checked_destination(subdirectory, "mesh.glb", settings)


@pytest.mark.parametrize("name", ["../../mesh.glb", "..\\..\\mesh.glb", "%2e%2e%2fmesh.glb"])
def test_server_filename_cannot_select_output_directory(settings, name):
    destination = checked_destination("job-123", name, settings)
    assert destination == settings.output_dir / "job-123" / "mesh.glb"


@pytest.mark.parametrize("url", [
    "https://attacker.invalid/mesh.glb",
    "https://fantasy.invalid.attacker.invalid:18449/mesh.glb",
    "https://fantasy.invalid:18448/mesh.glb",
    "file:///tmp/mesh.glb",
    "https://user:secret@fantasy.invalid:18449/mesh.glb",
    "https://fantasy.invalid:18449/mesh.glb#fragment",
    "https://fantasy.invalid:18449/mesh.glb?access_token=secret",
])
def test_download_url_must_use_exact_configured_service_origin(settings, url):
    with pytest.raises(AdapterError):
        approved_url(url, settings)


async def test_download_normal_file_computes_size_and_sha256(settings):
    url = settings.module_url("model3d") + "/artifacts/mesh.glb"
    requests = []
    payload = b"glTF-test-data"

    def handle(request):
        requests.append(request)
        assert request.url.path == "/artifacts/mesh.glb"
        return httpx.Response(200, headers={"content-type": "model/gltf-binary"}, stream=Chunks(payload[:4], payload[4:]))

    destination = checked_destination("job", "mesh.glb", settings)
    async with create_http_client(settings, transport=httpx.MockTransport(handle)) as client:
        result = await download_to_file(client, url, destination, settings)
    assert destination.read_bytes() == payload
    assert result == {"path": str(destination), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    assert len(requests) == 1
    assert list(destination.parent.glob(".fantasy-*.part")) == []


@pytest.mark.parametrize("location", [
    "/users/sign_in",
    "https://fantasy.invalid:18444/users/sign_in",
    "https://attacker.invalid/stolen",
])
async def test_download_rejects_all_redirects_before_contact(settings, location):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    destination = checked_destination("job", "mesh.glb", settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AdapterError, match="redirect"):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", destination, settings,
                                   headers={"Authorization": "Bearer test-only-secret"})
    assert len(requests) == 1
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


@pytest.mark.parametrize("headers,parts", [
    ({"content-length": "33"}, (b"unused",)),
    ({}, (b"a" * 20, b"b" * 20)),
    ({"content-length": "2"}, (b"a" * 20, b"b" * 20)),
])
async def test_download_enforces_declared_and_actual_streamed_limit(settings, headers, parts):
    def handle(request):
        return httpx.Response(200, headers=headers, stream=Chunks(*parts))

    destination = checked_destination("job", "mesh.glb", settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AdapterError, match="max_download_bytes"):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", destination, settings)
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "application/xhtml+xml"])
async def test_download_does_not_save_success_status_login_page(settings, content_type):
    def handle(request):
        return httpx.Response(200, headers={"content-type": content_type}, content=b"<html>Log in</html>")

    destination = checked_destination("job", "mesh.glb", settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AdapterError, match="login/HTML"):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", destination, settings)
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


async def test_download_cannot_overwrite_existing_file_and_never_requests(settings):
    destination = checked_destination("job", "mesh.glb", settings)
    destination.write_bytes(b"existing-art")

    def handle(request):
        pytest.fail("Existing-file rejection must occur before a network request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AdapterError, match="already exists"):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", destination, settings)
    assert destination.read_bytes() == b"existing-art"


async def test_download_cannot_overwrite_file_created_while_streaming(settings):
    destination = checked_destination("job", "mesh.glb", settings)

    class ConcurrentWrite(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"downloaded-art"
            destination.write_bytes(b"concurrent-art")

    def handle(request):
        return httpx.Response(200, stream=ConcurrentWrite())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises((AdapterError, FileExistsError)):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", destination, settings)
    assert destination.read_bytes() == b"concurrent-art"
    assert list(destination.parent.glob(".fantasy-*.part")) == []


async def test_download_cannot_bypass_output_root_with_direct_destination(settings, tmp_path):
    def handle(request):
        pytest.fail("Output-boundary rejection must occur before a network request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AdapterError, match="outside output_dir"):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", tmp_path / "outside.glb", settings)


async def test_download_stops_redirect_loop(settings):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/artifact"})

    destination = checked_destination("job", "mesh.glb", settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AdapterError, match="redirect"):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", destination, settings)
    assert len(requests) == 1
    assert not destination.exists()


async def test_download_stream_failure_cleans_partial_file(settings):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("simulated disconnection")

    def handle(request):
        return httpx.Response(200, stream=BrokenStream())

    destination = checked_destination("job", "mesh.glb", settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadError):
            await download_to_file(client, settings.module_url("model3d") + "/artifact", destination, settings)
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []

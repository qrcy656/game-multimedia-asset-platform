"""Exercise the registered public MCP tools with an offline backend."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fantasy_mcp.config import Settings
from fantasy_mcp.server import build_server


def result_data(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads("".join(item.text for item in result.content if item.type == "text"))


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.artifacts = [{"url": "https://fantasy.invalid:18449/api/jobs/job-1/files/mesh.glb", "name": "mesh.glb"}]

    async def capabilities(self):
        self.calls.append(("capabilities",))
        if self.failure:
            raise self.failure
        return {"modules": {"model3d": {"available": True}}, "password": "backend-password",
                "nested": {"access_token": "backend-token", "ready": True}}

    async def list_jobs(self, module, limit):
        self.calls.append(("list_jobs", module, limit))
        return {"jobs": []}

    async def list_assets(self, module, limit):
        self.calls.append(("list_assets", module, limit))
        return {"items": []}

    async def upload_reference(self, path, kind):
        self.calls.append(("upload_reference", path, kind))
        return {"upload_id": "upload-1"}

    async def submit_job(self, *args):
        self.calls.append(("submit_job", *args))
        return {"id": "job-1"}

    async def submit_animation(self, body):
        self.calls.append(("submit_animation", body))
        return {"job_id": "job-1"}

    async def submit_animation_batch(self, batch):
        self.calls.append(("submit_animation_batch", batch))
        return {"batch_id": batch["batch_id"]}

    async def get_downloads(self, module, job_id, asset_id):
        self.calls.append(("get_downloads", module, job_id, asset_id))
        return self.artifacts

    async def download(self, url, target):
        self.calls.append(("download", url, target))
        payload = b"glTF-test-artifact"
        target.write_bytes(payload)
        return {"path": str(target), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

    async def close(self):
        self.calls.append(("close",))


@pytest.fixture
def fixture(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    settings = Settings(portal_url="https://fantasy.invalid:18443", output_dir=tmp_path / "outputs",
                        upload_roots=(inputs,), username="not-for-tools", password="not-for-tools-password")
    backend = FakeBackend()
    return build_server(settings, backend=backend), settings, backend


async def test_public_tools_annotations_and_credentials_free_input_schemas(fixture):
    server, _, _ = fixture
    tools = await server.list_tools()
    by_name = {tool.name: tool for tool in tools}
    reads = {"list_capabilities", "list_animation_references", "list_jobs", "get_job_status", "list_assets", "list_downloads",
             "get_effects_guide", "list_effects", "read_effect", "validate_effect", "get_effect_export"}
    writes = {"upload_animation_reference", "create_character", "create_motion", "preview_animation_prompt",
              "submit_model_job", "submit_media_job", "submit_animation_job", "submit_animation_batch", "download_artifacts",
              "export_effect", "start_effect_export", "download_effect_export"}
    edits = {"save_effect", "cancel_effect_export"}
    assert by_name.keys() == reads | writes | edits
    sensitive = {"username", "password", "credentials", "credentials_file", "access_token", "cookie", "authorization"}
    for name, tool in by_name.items():
        assert tool.annotations.read_only_hint is (name in reads)
        assert tool.annotations.destructive_hint is (name in edits)
        assert tool.annotations.idempotent_hint is (name in reads)
        assert not sensitive.intersection(tool.input_schema.get("properties", {}))
        assert "not-for-tools" not in json.dumps(tool.input_schema)
    for name in ("list_downloads", "download_artifacts"):
        assert "url" not in by_name[name].input_schema["properties"]
    assert "file_path" not in by_name["download_artifacts"].input_schema["properties"]


async def test_community_backend_is_enabled_with_the_same_public_contract(tmp_path):
    backend = FakeBackend()
    server = build_server(Settings(edition="community", output_dir=tmp_path), backend=backend)
    result = await server.call_tool("list_capabilities", {})
    assert result_data(result)["modules"]["model3d"]["available"] is True
    assert len(await server.list_tools()) == 25


@pytest.mark.parametrize("edition,expected", [("private", "trellis2"), ("community", None)])
async def test_model_default_is_selected_for_its_edition(tmp_path, edition, expected):
    image = tmp_path / "model.png"
    image.write_bytes(b"reference")
    backend = FakeBackend()
    server = build_server(Settings(edition=edition, output_dir=tmp_path / "out", upload_roots=(tmp_path,)), backend=backend)
    await server.call_tool("submit_model_job", {"file_path": str(image), "params": {}})
    assert backend.calls == [("submit_job", "model3d", expected, {}, image, "direct")]


async def test_explicit_community_model_id_is_preserved(tmp_path):
    image = tmp_path / "model.png"
    image.write_bytes(b"reference")
    backend = FakeBackend()
    server = build_server(Settings(edition="community", output_dir=tmp_path / "out", upload_roots=(tmp_path,)), backend=backend)
    await server.call_tool("submit_model_job", {"file_path": str(image), "params": {}, "model_id": "configured-model-2"})
    assert backend.calls[0][2] == "configured-model-2"


async def test_tool_output_recursively_redacts_backend_credentials(fixture):
    server, _, backend = fixture
    result = await server.call_tool("list_capabilities", {})
    assert result_data(result) == {"modules": {"model3d": {"available": True}}, "nested": {"ready": True}}
    serialized = result.model_dump_json()
    assert "backend-password" not in serialized
    assert "backend-token" not in serialized
    assert backend.calls == [("capabilities",)]


@pytest.mark.parametrize("error", [
    ValueError("private-password-in-value-error"),
    OSError("private-password-in-os-error"),
    RuntimeError("private-password-in-runtime-error"),
    httpx.ReadError("https://private-password-in-url.invalid"),
])
async def test_unexpected_backend_errors_do_not_expose_sensitive_details(fixture, error):
    server, _, backend = fixture
    backend.failure = error
    with pytest.raises(ToolError) as caught:
        await server.call_tool("list_capabilities", {})
    assert "private-password" not in str(caught.value)
    assert len(backend.calls) == 1


@pytest.mark.parametrize("name,args", [
    ("submit_media_job", {"module": "admin", "model_id": "test", "params": {}}),
    ("submit_media_job", {"module": "image", "model_id": "test", "params": {}, "prompt_mode": "run-shell"}),
    ("submit_animation_batch", {"tasks": [], "batch_id": "stable"}),
    ("submit_animation_batch", {"tasks": [{}] * 11, "batch_id": "stable"}),
    ("list_jobs", {"module": "model3d", "limit": 0}),
    ("list_assets", {"module": "model3d", "limit": 101}),
])
async def test_invalid_tool_arguments_do_not_call_backend(fixture, name, args):
    server, _, backend = fixture
    with pytest.raises(ToolError):
        await server.call_tool(name, args)
    assert backend.calls == []


async def test_upload_outside_roots_is_rejected_before_backend(fixture, tmp_path):
    server, _, backend = fixture
    path = tmp_path / "outside.png"
    path.write_bytes(b"outside")
    with pytest.raises(ToolError, match="upload_roots"):
        await server.call_tool("upload_animation_reference", {"file_path": str(path), "kind": "character"})
    assert backend.calls == []


async def test_upload_inside_root_reaches_backend_as_resolved_path(fixture):
    server, settings, backend = fixture
    path = settings.upload_roots[0] / "reference.png"
    path.write_bytes(b"image")
    result = await server.call_tool("upload_animation_reference", {"file_path": str(path), "kind": "character"})
    assert result_data(result) == {"upload_id": "upload-1"}
    assert backend.calls == [("upload_reference", path, "character")]


async def test_animation_explicit_idempotency_key_remains_stable_across_calls(fixture):
    server, _, backend = fixture
    task = {"character_id": "character-1", "idempotency_key": "overridden-key"}
    for _ in range(2):
        result = await server.call_tool("submit_animation_job", {"task": task, "idempotency_key": "stable-key"})
        assert result_data(result)["idempotency_key"] == "stable-key"
    assert len(backend.calls) == 2
    assert all(call[1]["idempotency_key"] == "stable-key" for call in backend.calls)
    assert task["idempotency_key"] == "overridden-key"


async def test_animation_task_key_is_preserved_or_generated_and_returned(fixture):
    server, _, backend = fixture
    preserved = await server.call_tool("submit_animation_job", {"task": {"character_id": "character-1", "idempotency_key": "task-key"}})
    assert result_data(preserved)["idempotency_key"] == "task-key"
    generated = await server.call_tool("submit_animation_job", {"task": {"character_id": "character-1"}})
    key = result_data(generated)["idempotency_key"]
    assert isinstance(key, str) and len(key) >= 32
    assert backend.calls[-1][1]["idempotency_key"] == key


async def test_download_rejects_unlisted_names_before_local_writes(fixture):
    server, settings, backend = fixture
    with pytest.raises(ToolError, match="artifact_names"):
        await server.call_tool("download_artifacts", {"module": "model3d", "job_id": "job-1", "artifact_names": ["secret.txt"]})
    assert backend.calls == [("get_downloads", "model3d", "job-1", None)]
    assert not settings.output_dir.exists()


async def test_download_directory_traversal_does_not_start_transfer(fixture):
    server, _, backend = fixture
    with pytest.raises(ToolError, match="inside output_dir"):
        await server.call_tool("download_artifacts", {"module": "model3d", "job_id": "job-1", "subdirectory": "../outside"})
    assert backend.calls == [("get_downloads", "model3d", "job-1", None)]


async def test_download_uses_backend_artifact_and_writes_checksum_manifest(fixture):
    server, settings, backend = fixture
    result = await server.call_tool("download_artifacts", {"module": "model3d", "job_id": "job-1", "subdirectory": "test-job"})
    data = result_data(result)
    artifact = data["artifacts"][0]
    payload = Path(artifact["path"]).read_bytes()
    assert Path(artifact["path"]).is_relative_to(settings.output_dir)
    assert artifact["sha256"] == hashlib.sha256(payload).hexdigest()
    manifest = json.loads(Path(data["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["artifacts"] == data["artifacts"]
    assert manifest["job_id"] == "job-1"
    assert backend.calls[1][1] == backend.artifacts[0]["url"]


async def test_animation_default_download_selects_zip_instead_of_duplicate_frames(fixture):
    server, _, backend = fixture
    backend.artifacts = [
        {"name": "frame-01.png", "url": "https://fantasy.invalid:18445/files/frame-01.png"},
        {"name": "animation-asset-1.zip", "url": "https://fantasy.invalid:18445/api/assets/asset-1/export.zip"},
    ]
    result = await server.call_tool("download_artifacts", {"module": "animation", "asset_id": "asset-1"})
    assert [artifact["name"] for artifact in result_data(result)["artifacts"]] == ["animation-asset-1.zip"]
    downloads = [call for call in backend.calls if call[0] == "download"]
    assert len(downloads) == 1
    assert downloads[0][1].endswith("/export.zip")

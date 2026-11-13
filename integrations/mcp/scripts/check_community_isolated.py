"""Run inside the community application image with network disabled.

The script creates only temporary data, disables animation/queue workers and
uses simulated providers. Never mount the live community data directory.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import struct
import tempfile
from unittest.mock import patch
import wave

import httpx
from PIL import Image

from fantasy_mcp.community import CommunityBackend
from fantasy_mcp.config import Settings


async def check(root):
    os.environ.update(COMMUNITY_DATA_DIR=str(root / "data"), COMMUNITY_ENABLE_ANIMATION="false",
                      COMMUNITY_RUN_QUEUE="false", APP_ORIGIN="http://localhost:18082")
    import app as application
    app = application.app
    registry = app.state.registry
    app.state.auth.create_user("mcp-check", "temporary-test-password", "Isolated MCP check", False)
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(image, format="PNG")
    png = image.getvalue()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 800)
    document = b'{"asset":{"version":"2.0"}}'
    document += b" " * (-len(document) % 4)
    glb = b"glTF" + struct.pack("<IIII", 2, 20 + len(document), len(document), 0x4E4F534A) + document
    expected = {"image": png, "model3d": glb, "speech": audio.getvalue(), "music": audio.getvalue()}
    provider_calls = []

    def provider(request):
        kind = request.url.host.split(".")[0]
        body = json.loads(request.content) if request.content else {}
        provider_calls.append((request.method, request.url.path))
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"model": kind + "-model", "capabilities": [kind],
                "api_version": "fantasy-v1", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}})
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json={"valid": body["kind"] == kind, "kind": kind, "model": kind + "-model"})
        if request.url.path.endswith("/generate"):
            if kind == "model3d":
                assert base64.b64decode(body["input"]["image"]["data_base64"]) == png
            return httpx.Response(200, json={"artifacts": [{"data_base64": base64.b64encode(expected[kind]).decode()}]})
        raise AssertionError("Unexpected mock provider request")

    registry.transport = httpx.MockTransport(provider)
    cfg = Settings(edition="community", portal_url="http://localhost:18082", username="mcp-check",
                   password="temporary-test-password", output_dir=root / "output", upload_roots=(root,))
    backend = CommunityBackend(cfg, httpx.ASGITransport(app=app))
    native_client = httpx.Client
    records = []
    try:
        with patch("providers._registry", return_value=registry), patch(
            "media.remote.httpx.Client", side_effect=lambda **kwargs: native_client(transport=httpx.MockTransport(provider), **kwargs)
        ):
            for kind in ("model3d", "image", "speech", "music"):
                profile = registry.save_profile({"name": "Isolated " + kind, "kind": kind, "protocol": "rest",
                    "base_url": "https://" + kind + ".example.invalid", "model": kind + "-model",
                    "api_key": "isolated-provider-secret"})
                await registry.verify(profile["id"])
                registry.bind(kind, kind, profile["id"])
                reference = None
                if kind == "model3d":
                    reference = root / "reference.png"
                    reference.write_bytes(png)
                params = {"text": "Hello"} if kind == "speech" else {"prompt": "A blue test asset"}
                submitted = await backend.submit_job(kind, None, params, reference)
                await asyncio.to_thread(app.state.queue.execute, app.state.queue.raw(submitted["id"]))
                completed = await backend.get_job(kind, submitted["id"])
                assert completed["status"] == "succeeded", completed
                assert "isolated-provider-secret" not in json.dumps(completed)
                downloads = await backend.get_downloads(kind, submitted["id"])
                assert downloads
                target = root / "output" / kind / downloads[0]["name"]
                downloaded = await backend.download(downloads[0]["url"], target)
                assert target.read_bytes() == expected[kind]
                assert downloaded["bytes"] == len(expected[kind])
                records.append({"module": kind, "status": completed["status"], "download_bytes": downloaded["bytes"]})
            assert len([call for call in provider_calls if call[1].endswith("/generate")]) == 4
            assert app.state.auth.list_users()[1]["admin"] is False
        print(json.dumps({"status": "passed", "isolated": True, "real_generation_calls": 0,
                          "temporary_data": True, "modules": records}, ensure_ascii=False))
    finally:
        await backend.close()
        app.state.queue.stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="community-mcp-check-") as temporary:
        asyncio.run(check(Path(temporary)))

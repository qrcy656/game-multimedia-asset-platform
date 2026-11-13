"""MCP tools for both Fantasy editions, including account-owned Arcadia effects."""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import wraps
import json
from typing import Any, Literal
from uuid import uuid4

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .common import AdapterError, checked_destination, checked_upload, redact
from .config import Settings

Module = Literal["model3d", "image", "speech", "music", "animation"]
MediaModule = Literal["model3d", "image", "speech", "music"]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def guarded(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return redact(await function(*args, **kwargs))
        except AdapterError as exc:
            raise ToolError(str(exc)) from None
        except (OSError, ValueError, TypeError):
            raise ToolError("Invalid input or local configuration/file access failed; check configured paths and arguments") from None
        except httpx.HTTPError:
            raise ToolError("Service connection failed; check task status before retrying a submission") from None
        except Exception as exc:
            raise ToolError(f"Adapter failed ({type(exc).__name__}); no automatic submission retry was performed") from None
    return wrapped


def build_server(settings: Settings, backend=None) -> MCPServer:
    if backend is None:
        if settings.edition == "community":
            from .community import CommunityBackend
            backend = CommunityBackend(settings)
        else:
            from .private import PrivateBackend
            backend = PrivateBackend(settings)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            await backend.close()

    mcp = MCPServer(
        "fantasy-workflow" + ("-community" if settings.edition == "community" else ""),
        version=__version__, log_level="WARNING", lifespan=lifespan,
        instructions=(
            "Fantasy creative assets: discover model capabilities and reference IDs before submitting. "
            "Generation uses server GPU resources: submit once, keep the returned job ID, and poll status. "
            "On submission timeout, inspect jobs before retrying. Reuse an explicit idempotency key for animation retries. "
            "Download only completed artifacts into the configured output directory. Uploads must be under configured roots. "
            "Animation produces transparent frames and spritesheets. Credentials and session cookies are never tool arguments. "
            "Community assisted animation requires a verified vision API bound to animation/vision and sends the reference images with the task text. "
            "A text-only LLM does not satisfy assisted animation; direct mode does not require this prompt model. "
            "Do not enable models, change credentials, approve characters, or delete jobs implicitly. "
            "For effects, read get_effects_guide, list/read projects, validate JSON, save, then export frame/atlas PNG. "
            "Effects is account-isolated; edits require overwrite=true and the revision returned by read_effect. "
            "Effects uses CPU rendering and is not a media generation job. "
               "For long exports use start_effect_export, keep its job ID, poll get_effect_export, then download_effect_export; "
               "cancel_effect_export stops an unwanted render."
        ),
    )

    @mcp.tool(annotations=READ)
    @guarded
    async def list_capabilities() -> dict[str, Any]:
        """Read available models, parameter schemas and health. Check both enabled and ready before generation."""
        return await backend.capabilities()

    @mcp.tool(annotations=READ)
    @guarded
    async def get_effects_guide() -> dict[str, Any]:
        """Read the live Arcadia JSON format, renderer limits and workflow before creating or editing effects."""
        return await backend.effects_guide()

    @mcp.tool(annotations=READ)
    @guarded
    async def list_effects() -> dict[str, Any]:
        """List saved effects; community includes shared projects with owner_key and editable permission. Factory presets are in the editor."""
        return await backend.effects_list()

    @mcp.tool(annotations=READ)
    @guarded
    async def read_effect(file: str, owner_key: str | None = None) -> dict[str, Any]:
        """Read a filename from list_effects and return its JSON plus SHA256 revision for conflict-safe editing."""
        return await backend.effects_read(file, **({'owner_key': owner_key} if owner_key is not None else {}))

    @mcp.tool(annotations=READ)
    @guarded
    async def validate_effect(data: dict) -> dict[str, Any]:
        """Validate Arcadia JSON without saving. Inspect valid, errors and warnings before save/export."""
        return await backend.effects_validate(data)

    @mcp.tool(annotations=EDIT)
    @guarded
    async def save_effect(name: str, data: dict, overwrite: bool = False,
                          expected_revision: str | None = None, owner_key: str | None = None) -> dict[str, Any]:
        """Create an account-owned effect by default. Editing requires explicit overwrite=true and read_effect's revision; conflicts never overwrite newer changes."""
        return await backend.effects_save(name, data, overwrite, expected_revision, **({'owner_key': owner_key} if owner_key is not None else {}))

    @mcp.tool(annotations=WRITE)
    @guarded
    async def export_effect(file: str, format: Literal['frame', 'atlas'] = 'frame',
                            time: float | None = None, subdirectory: str | None = None, owner_key: str | None = None) -> dict[str, Any]:
        """Submit, poll without a total deadline and download a saved effect as PNG. Client cancellation attempts to cancel the render. For long work or clients with tool timeouts use start/get/download_effect_export instead. Never overwrites local files; time is seconds."""
        return await backend.effects_export(file, format, time, subdirectory, **({'owner_key': owner_key} if owner_key is not None else {}))

    @mcp.tool(annotations=WRITE)
    @guarded
    async def start_effect_export(file: str, format: Literal['frame', 'atlas'] = 'frame',
                                  time: float | None = None, owner_key: str | None = None) -> dict[str, Any]:
        """Start a saved effect's PNG export once and immediately return its account-owned job UUID. Keep the UUID; poll get_effect_export until done. Time is seconds. Never automatically resubmit an uncertain request."""
        return await backend.effects_start_export(file, format, time, **({'owner_key': owner_key} if owner_key is not None else {}))

    @mcp.tool(annotations=READ)
    @guarded
    async def get_effect_export(job_id: str) -> dict[str, Any]:
        """Read an export job UUID from start_effect_export: running, done, failed or cancelled. Only the configured account can read it. Successful download releases the temporary server job, so subsequent reads can return 404."""
        return await backend.effects_get_export(job_id)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def download_effect_export(job_id: str, subdirectory: str | None = None) -> dict[str, Any]:
        """Download a done export into output_dir, validate the PNG and save its SHA256 manifest with job_id. Never overwrites files. After complete local saves, release the temporary server job; cleanup failures return a warning without losing the files."""
        return await backend.effects_download_export(job_id, subdirectory)

    @mcp.tool(annotations=EDIT)
    @guarded
    async def cancel_effect_export(job_id: str) -> dict[str, Any]:
        """Cancel an explicitly unwanted export job belonging to this account. This stops rendering; it does not modify the saved effect JSON or previously downloaded files. Check status if cancellation is not confirmed."""
        return await backend.effects_cancel_export(job_id)


    @mcp.tool(annotations=READ)
    @guarded
    async def list_animation_references() -> dict[str, Any]:
        """Read existing character IDs, reference image IDs, motion video IDs and approval state."""
        return await backend.list_references()

    @mcp.tool(annotations=READ)
    @guarded
    async def list_jobs(module: Module, limit: int = 20) -> dict[str, Any]:
        """List recent jobs visible to the configured account; useful after an ambiguous submission timeout."""
        if not 1 <= limit <= 100:
            raise AdapterError("limit must be between 1 and 100")
        return await backend.list_jobs(module, limit)

    @mcp.tool(annotations=READ)
    @guarded
    async def get_job_status(module: Module, job_id: str) -> dict[str, Any]:
        """Read one job's state, progress, errors and available artifact references; does not wait for completion."""
        return await backend.get_job(module, job_id)

    @mcp.tool(annotations=READ)
    @guarded
    async def list_assets(module: Module, limit: int = 20) -> dict[str, Any]:
        """List completed assets visible to the configured account."""
        if not 1 <= limit <= 100:
            raise AdapterError("limit must be between 1 and 100")
        return await backend.list_assets(module, limit)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def upload_animation_reference(file_path: str, kind: Literal["character", "motion"] = "character") -> dict[str, Any]:
        """Upload a local character image or motion video. Returns an upload record for creating references/tasks."""
        return await backend.upload_reference(checked_upload(file_path, settings), kind)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def create_character(character: dict) -> dict[str, Any]:
        """Create a character draft from uploaded references using the API schema; does not automatically approve it."""
        return await backend.create_character(character)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def create_motion(motion: dict) -> dict[str, Any]:
        """Create a named motion reference from an uploaded video using the API schema."""
        return await backend.create_motion(motion)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def preview_animation_prompt(task: dict) -> dict[str, Any]:
        """Compile/validate an AnimationTask without submitting video generation. Community assisted mode requires a verified animation/vision model and sends reference images plus task text; text-only LLMs are insufficient. Check discovered configuration readiness first. Direct mode skips this prompt model."""
        return await backend.compile_animation(task)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def submit_model_job(file_path: str, params: dict, model_id: str | None = None) -> dict[str, Any]:
        """Queue a 3D job using a discovered model ID. Omit for private TRELLIS.2 or a unique community default."""
        selected = model_id or ("trellis2" if settings.edition == "private" else None)
        return await backend.submit_job("model3d", selected, params, checked_upload(file_path, settings), "direct")

    @mcp.tool(annotations=WRITE)
    @guarded
    async def submit_media_job(module: MediaModule, model_id: str, params: dict,
                               file_path: str | None = None,
                               prompt_mode: Literal["direct", "assisted"] = "direct") -> dict[str, Any]:
        """Queue a 3D/image/speech/music job using a discovered model ID and its parameter schema. Does not enable services."""
        path = checked_upload(file_path, settings) if file_path else None
        return await backend.submit_job(module, model_id, params, path, prompt_mode)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def submit_animation_job(task: dict, idempotency_key: str | None = None) -> dict[str, Any]:
        """Queue an AnimationTask. Community prompt_mode=assisted requires a verified animation/vision API that receives text and reference images; check discovered readiness. Direct mode skips the prompt model. Keep the returned idempotency key and job ID. Set preview=true for an unapproved draft."""
        body = dict(task)
        body["idempotency_key"] = idempotency_key or body.get("idempotency_key") or str(uuid4())
        result = await backend.submit_animation(body)
        return {"idempotency_key": body["idempotency_key"], "result": result}

    @mcp.tool(annotations=WRITE)
    @guarded
    async def submit_animation_batch(tasks: list[dict], batch_id: str) -> dict[str, Any]:
        """Queue 1–10 AnimationTasks under a stable batch ID. Reuse the batch ID only for the same logical submission."""
        if not 1 <= len(tasks) <= 10:
            raise AdapterError("A batch must contain 1–10 tasks")
        return await backend.submit_animation_batch({"batch_id": batch_id, "tasks": tasks})

    @mcp.tool(annotations=READ)
    @guarded
    async def list_downloads(module: Module, job_id: str | None = None, asset_id: str | None = None) -> dict[str, Any]:
        """Resolve authorized artifact names and URLs from an existing job/asset. No arbitrary URL input is accepted."""
        return {"artifacts": await backend.get_downloads(module, job_id, asset_id)}

    @mcp.tool(annotations=WRITE)
    @guarded
    async def download_artifacts(module: Module, job_id: str | None = None, asset_id: str | None = None,
                                 artifact_names: list[str] | None = None, subdirectory: str | None = None) -> dict[str, Any]:
        """Download job/asset artifacts to configured output_dir, retaining names and SHA256 hashes. Never overwrites files."""
        candidates = await backend.get_downloads(module, job_id, asset_id)
        if artifact_names is not None:
            requested = set(artifact_names)
            names = {item["name"] for item in candidates}
            if not requested or requested - names:
                raise AdapterError("Select artifact_names returned by list_downloads")
            candidates = [item for item in candidates if item["name"] in requested]
        if not candidates:
            raise AdapterError("This task has no downloadable artifacts yet")
        # Prefer the packaged animation export to hundreds of duplicate frame downloads.
        if artifact_names is None:
            packages = [item for item in candidates if item["name"].lower().endswith(".zip")]
            if packages and module == "animation":
                candidates = packages[:1]
        if len(candidates) > 100:
            raise AdapterError("Choose up to 100 artifact_names, or select the animation ZIP")
        folder = subdirectory or f"{module}/{uuid4().hex}"
        results = []
        for item in candidates:
            target = checked_destination(folder, item["name"], settings)
            result = await backend.download(item["url"], target)
            results.append({"name": item["name"], **result})
        manifest_path = checked_destination(folder, "fantasy-download-manifest.json", settings)
        manifest = {"edition": settings.edition, "module": module, "job_id": job_id,
                    "asset_id": asset_id, "artifacts": results}
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        return {**manifest, "manifest_path": str(manifest_path)}

    @mcp.resource("fantasy://usage")
    def usage() -> str:
        return ("Read list_capabilities and list_animation_references first. Submit tasks once, poll get_job_status "
                "approximately every 5 seconds, then list_downloads and download_artifacts. For animation, "
                "use the JSON schema from fantasy://animation-schema. "
                "Community assisted animation requires a verified vision API bound to animation/vision and passes reference images together with the task text; "
                "check discovered configuration readiness before using it. A text-only LLM is insufficient. Direct mode skips this prompt model. "
                "Runtime GLB files are inspected in Blender before Godot import; transparent frame animations need FPS, direction, loop and foot anchor metadata. "
                "For particle effects use fantasy://effects-usage and get_effects_guide; effects are not GPU model jobs.")

    @mcp.resource("fantasy://effects-usage")
    def effects_usage() -> str:
        return json.dumps({
            "workflow": ["get_effects_guide", "list_effects", "read_effect", "validate_effect", "save_effect", "export_effect"],
            "data": "Arcadia JSON object; get_effects_guide returns the current upstream FORMAT and runtime limits.",
            "creation": "save_effect(name, data) creates a new file; an existing filename returns a conflict.",
            "editing": "Read the current JSON, validate modifications, then save_effect with overwrite=true and expected_revision from that read. On conflict reread and reconcile.",
            "exports": "export_effect submits once, polls without a total deadline and downloads PNG/manifest into output_dir; files are never overwritten. A client's own tool timeout may cancel this convenience call and trigger best-effort cancellation of its render.",
            "long_exports": ["start_effect_export(file, format, time): keep job.id", "get_effect_export(job_id): poll until done", "download_effect_export(job_id, subdirectory): save and release the completed job", "cancel_effect_export(job_id): explicitly stop an unwanted render"],
            "export_cache": "Successful downloads retain job_id in the local manifest and release temporary server storage. Later status checks may return 404; the saved effect JSON and local artifacts remain intact. Cleanup failures return a warning with the job ID, while the completed local files remain valid.",
            "edition": settings.edition,
            "accounts": "All work belongs to the configured platform account (community local account or private GitLab account). The running MCP connection stays bound to its first effects account; restart deliberately to change accounts.",
            "recovery": "Writes are not automatically retried. For saves, read/list effects before retrying. For export polling/download failures, resume with the same job_id instead of starting another render. Split start/get/download calls allow rendering to continue between client turns.",
            "deletion": "No MCP deletion tool is provided.",
        }, ensure_ascii=False, indent=2)


    @mcp.resource("fantasy://animation-schema")
    def animation_schema() -> str:
        from importlib.resources import files
        name = "animation-schema.community.json" if settings.edition == "community" else "animation-schema.json"
        return files("fantasy_mcp").joinpath(name).read_text(encoding="utf-8")

    return mcp


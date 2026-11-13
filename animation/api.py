from contextlib import asynccontextmanager
import copy
import hashlib
import json
from pathlib import Path
from typing import Literal
import subprocess
import shutil
import time
import uuid
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

from .auth import AuthenticationError, Identity
from .clients import GenerationServicesUnavailable, PermanentError, TransientError, NeedsAttention
from .config import settings as default_settings
from .files import file_url, relative_path, safe_path, sha256
from .models import ACTIONS, FACINGS, MAX_CHARACTER_REFERENCES, AnimationTask, Approval, CharacterInput, CharacterReferenceInput, CharacterLabelInput, MotionInput, NotificationInput
from .service import Service
from .store import Conflict, Store
from .privacy import public_metadata, public_document


def public_record(row):
    from providers import current_model_context
    actor = (current_model_context() or {}).get('user') or {}
    return public_metadata(row, owner_id=row.get('owner_id'), viewer_id=actor.get('id'))


def public_job(row):
    value = copy.deepcopy(row)
    value.get('snapshot', {}).pop('model_context', None)
    checkpoint = value.get('checkpoint', {})
    # Preserve task diagnostics/progress, but not remote receipts or signed URLs.
    for key in ('submit_response', 'generation_response'):
        checkpoint.pop(key, None)
    return public_record(value)


def create_app(settings=default_settings, store=None):
    store = store or Store(settings)
    service = Service(settings, store)
    identity = Identity(settings)

    @asynccontextmanager
    async def lifespan(app):
        identity.validate_configuration()
        store.initialize()
        service.bootstrap()
        yield

    app = FastAPI(title="Animation Studio", version="1.1.0", lifespan=lifespan)
    app.state.store, app.state.service = store, service

    def motion_view(row):
        return public_record({**row, "path": row["video_path"], "url": file_url(service.root, row["video_path"])})

    @app.middleware("http")
    async def auth(request, call_next):
        path = request.scope.get("path", request.url.path)
        root_path = request.scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path):] or "/"
        public = {"/api/health/live"}
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.app_origin.rstrip("/"):
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        if (path.startswith("/api/") or path.startswith("/files/")) and path not in public:
            try:
                request.state.principal = identity.authenticate(request)
            except AuthenticationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401,
                                    headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if path.startswith(("/api/", "/files/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def principal(request):
        return request.state.principal

    def require_admin(request):
        if not principal(request).admin:
            raise HTTPException(403, "Administrator permission required")

    def listed_owner(request, all_users):
        return None if all_users else principal(request).id

    def visible(request, table, object_id, shared=False, edit=False):
        row = store.get(table, object_id)
        if not row:
            raise KeyError(object_id)
        if edit and row["owner_id"] != principal(request).id and not principal(request).admin:
            raise HTTPException(403, "仅创建者或管理员可以修改此内容")
        return row

    def require_model_owner(request, job):
        if job['owner_id'] == principal(request).id:
            return
        from providers import resolve
        selections = job.get('snapshot', {}).get('model_context') or {}
        choices = dict(selections.get('selections', {}).get('animation', {}))
        checkpoint = job.get('checkpoint', {})
        for role, provider in [('video', checkpoint.get('compiled', {}).get('video_provider')),
                               ('image', checkpoint.get('matting_provider'))]:
            if isinstance(provider, dict) and provider.get('id'):
                choices[role] = provider['id']
        for role, identifier in choices.items():
            if identifier:
                try:
                    profile = resolve('animation', role, identifier)
                    if profile.get('scope', 'global') != 'global':
                        raise ValueError('Personal model')
                except (ValueError, KeyError, RuntimeError):
                    raise HTTPException(403, '此任务使用创建者的个人模型配置，仅创建者可原地重试。请复用素材并使用自己的模型新建任务。') from None

    def registered_paths(path):
        """Exact registered references only; never make DATA_ROOT a file browser."""
        with store.connection() as conn:
            uploads = conn.execute("SELECT * FROM uploads WHERE deleted_at IS NULL AND path=%s", (path,)).fetchall()
            characters = conn.execute("""SELECT id,owner_id,owner_name FROM characters WHERE deleted_at IS NULL
                AND EXISTS (SELECT 1 FROM jsonb_each_text(views) AS entry WHERE entry.value=%s)""", (path,)).fetchall()
            motions = conn.execute("SELECT id,owner_id,owner_name FROM motions WHERE deleted_at IS NULL AND video_path=%s", (path,)).fetchall()
        return uploads, characters, motions

    def source_path(request, value, kind):
        source = safe_path(service.root, value)
        path = relative_path(service.root, source)
        uploads, characters, motions = registered_paths(path)
        records = [row for row in uploads if row["kind"] == kind] + (characters if kind == "character" else motions)
        if not records:
            raise KeyError("Reference")
        return source

    def character_image_path(request, value):
        path = source_path(request, value, "character")
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Character references must be PNG, JPEG or WebP")
        try:
            with Image.open(path) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 25_000_000:
                    raise ValueError("Reference must be a supported image under 25 megapixels")
                image.verify()
        except OSError as exc:
            raise ValueError("Reference is not a valid image") from exc
        return path

    def borrow_reference(source, owner, kind):
        """New catalogue content gets its own copy of another author's upload."""
        uploads, characters, motions = registered_paths(relative_path(service.root, source))
        records = uploads + characters + motions
        if any(row["owner_id"] == owner["owner_id"] for row in records):
            return source
        identifier = uuid.uuid4().hex
        target = service.root / ("characters" if kind == "character" else "motions") / "uploads" / (identifier + source.suffix.lower())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        try:
            store.upload(identifier, kind, relative_path(service.root, target), sha256(target),
                         {"copied_reference": relative_path(service.root, source)}, owner=owner)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        detail = "补充参考图不存在或无权使用，请重新选择或上传" if exc.args == ("Additional reference upload",) else "Requested object does not exist"
        return JSONResponse({"detail": detail}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def forbidden_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(PermanentError)
    async def permanent_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(TransientError)
    @app.exception_handler(NeedsAttention)
    async def unavailable_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(GenerationServicesUnavailable)
    async def generation_unavailable_handler(request, exc):
        return JSONResponse({"detail": str(exc), "code": "generation_services_not_ready",
                             "missing_services": exc.readiness["missing_services"], "ready": False}, status_code=503)

    @app.get("/api/auth/session")
    def session(request: Request):
        user = principal(request)
        return {"authenticated": True, "user": user.user,
                "permissions": {"can_delete": True, "can_view_all": True}}

    @app.get("/api/catalog")
    def catalog():
        return {"characters": [public_record(service.character(c["id"])) for c in store.list("characters")],
                "motions": [motion_view(m) for m in store.list("motions")], "actions": [{"id": k, "name": v[0]} for k, v in ACTIONS.items()],
                "facings": FACINGS, "reference_limits": {"max_images": MAX_CHARACTER_REFERENCES, "max_additional_images": MAX_CHARACTER_REFERENCES - 1}}

    @app.get("/api/characters")
    def characters():
        return {"items": [public_record(service.character(c["id"])) for c in store.list("characters")]}

    @app.get("/api/characters/{character_id}")
    def character(character_id: str):
        return public_record(service.character(character_id))

    @app.post("/api/characters", status_code=201)
    def create_character(body: CharacterInput, request: Request):
        character_id = body.id or uuid.uuid4().hex
        views, labels = {}, {}
        for facing, value in body.views.items():
            if facing.upper() not in FACINGS:
                raise ValueError("Invalid reference facing")
            path = borrow_reference(character_image_path(request, value), principal(request).owner, "character")
            views[facing.upper()] = relative_path(service.root, path)
            labels[facing.upper()] = facing.upper()
        for image in body.images:
            path = borrow_reference(character_image_path(request, image.path), principal(request).owner, "character")
            reference_id = uuid.uuid4().hex
            views[reference_id] = relative_path(service.root, path)
            labels[reference_id] = image.label
        if not views:
            raise ValueError("请至少上传一张档案图片")
        if len(views) > 200:
            raise ValueError("每个档案最多保存 200 张图片")
        store.create_character({"id": character_id, "name": body.name, "views": views,
                                "reference_labels": labels, "profile": body.profile},
                               owner=principal(request).owner)
        return public_record(service.character(character_id))

    @app.post("/api/characters/{character_id}/analyze")
    def analyze(character_id: str, request: Request):
        with store.resource_guard("characters", character_id):
            visible(request, "characters", character_id, shared=True, edit=True)
            return public_record(service.analyze(character_id))

    @app.post("/api/characters/{character_id}/approve")
    def approve(character_id: str, body: Approval, request: Request):
        with store.resource_guard("characters", character_id):
            visible(request, "characters", character_id, shared=True, edit=True)
            return public_record(service.approve(character_id, body.profile))

    @app.post("/api/characters/{character_id}/references")
    def reference(character_id: str, body: CharacterReferenceInput, request: Request):
        with store.resource_guard("characters", character_id):
            row = visible(request, "characters", character_id, shared=True, edit=True)
            path = borrow_reference(character_image_path(request, body.path), row, "character")
            if body.facing:
                store.save_reference(character_id, body.facing, relative_path(service.root, path))
            else:
                store.add_reference(character_id, relative_path(service.root, path), body.label)
            return public_record(service.character(character_id))

    @app.delete("/api/characters/{character_id}/references/{reference_id}")
    def remove_reference(character_id: str, reference_id: str, request: Request):
        with store.resource_guard("characters", character_id):
            visible(request, "characters", character_id, shared=True, edit=True)
            return store.remove_reference(character_id, reference_id, principal(request).owner["owner_id"])

    @app.patch("/api/characters/{character_id}/references/{reference_id}")
    def label_reference(character_id: str, reference_id: str, body: CharacterLabelInput, request: Request):
        with store.resource_guard("characters", character_id):
            visible(request, "characters", character_id, shared=True, edit=True)
            store.label_reference(character_id, reference_id, body.label)
            return public_record(service.character(character_id))

    @app.get("/api/motions")
    def motions():
        return {"items": [motion_view(m) for m in store.list("motions")]}

    @app.post("/api/motions", status_code=201)
    def motion(body: MotionInput, request: Request):
        if body.upload_id:
            uploaded = visible(request, "uploads", body.upload_id)
            if not uploaded or uploaded["kind"] != "motion":
                raise ValueError("A motion video upload ID is required")
            path = uploaded["path"]
        elif body.video_path:
            path = body.video_path
        else:
            raise ValueError("upload_id is required")
        source = source_path(request, path, "motion")
        if source.suffix.lower() not in {".mp4", ".mov", ".webm", ".mkv"}:
            raise ValueError("Motion reference must be a video")
        source = borrow_reference(source, principal(request).owner, "motion")
        return motion_view(store.create_motion(body.name, relative_path(service.root, source), body.description,
                                   {"sha256": sha256(source)}, owner=principal(request).owner))

    @app.post("/api/uploads", status_code=201)
    def upload(request: Request, kind: str = Form(...), file: UploadFile = File(...)):
        if kind == "character-reference":
            kind = "character"
        if kind not in {"character", "motion"}:
            raise ValueError("kind must be character or motion")
        suffix = Path(file.filename or "").suffix.lower()
        allowed = {".png", ".jpg", ".jpeg", ".webp"} if kind == "character" else {".mp4", ".mov", ".webm", ".mkv"}
        if suffix not in allowed:
            raise ValueError("Unsupported file type")
        upload_id = uuid.uuid4().hex
        path = service.root / ("characters" if kind == "character" else "motions") / "uploads" / (upload_id + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(suffix + ".part")
        size = 0
        try:
            with part.open("wb") as output:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.upload_max_bytes:
                        raise HTTPException(413, "Upload is too large")
                    output.write(chunk)
            if not size:
                raise ValueError("Empty upload")
            if kind == "character":
                with Image.open(part) as image:
                    if image.width * image.height > 25_000_000 or image.format not in {"PNG", "JPEG", "WEBP"}:
                        raise ValueError("Image format or dimensions are unsupported")
                    metadata = {"width": image.width, "height": image.height, "format": image.format}
                    image.verify()
            else:
                probe = subprocess.run([settings.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                    "-show_entries", "format=duration,format_name:stream=codec_type,width,height", "-of", "json", str(part)],
                    capture_output=True, text=True, timeout=30, check=True)
                metadata = json.loads(probe.stdout)
                if not any(s.get("codec_type") == "video" for s in metadata.get("streams", [])):
                    raise ValueError("Upload has no video stream")
                if not 0 < float(metadata["format"]["duration"]) <= 180:
                    raise ValueError("Motion reference must be at most 180 seconds")
            part.replace(path)
            row = store.upload(upload_id, kind, relative_path(service.root, path), sha256(path), metadata,
                               owner=principal(request).owner)
            return {**row, "url": file_url(service.root, path)}
        except (subprocess.SubprocessError, OSError, KeyError) as exc:
            raise ValueError("Cannot validate uploaded media: " + str(exc)) from exc
        finally:
            part.unlink(missing_ok=True)

    @app.post("/api/compile")
    def compile_task(body: AnimationTask, request: Request):
        return service.compile(body.model_dump(), owner=principal(request).owner, admin=principal(request).admin)

    @app.post("/api/animation/generate", status_code=202)
    def generate(body: AnimationTask, request: Request):
        task = body.model_dump()
        task["idempotency_key"] = task.get("idempotency_key") or request.headers.get("Idempotency-Key")
        if task["idempotency_key"] and len(task["idempotency_key"]) > 200:
            raise ValueError("Idempotency key exceeds 200 characters")
        return public_job(service.enqueue(task, owner=principal(request).owner, admin=principal(request).admin))

    @app.get("/api/jobs")
    def jobs(request: Request, limit: int = Query(100, ge=1, le=500), all_users: bool = True):
        return {"items": [public_job(row) for row in store.list("jobs", limit, owner_id=listed_owner(request, all_users))]}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, request: Request):
        return public_job(visible(request, "jobs", job_id))

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str, request: Request):
        row = visible(request, "jobs", job_id, edit=True)
        require_model_owner(request, row)
        from providers import restore_model_context
        with restore_model_context(row.get("snapshot", {}).get("model_context"), owner_id=row["owner_id"]):
            service.snapshot(row["task"], owner=principal(request).owner, admin=principal(request).admin)
            service.require_generation_ready(row["task"])
        return public_job(store.retry_manual(job_id, owner=principal(request).owner, admin=principal(request).admin))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        visible(request, "jobs", job_id, edit=True)
        return public_job(store.cancel(job_id))

    @app.post("/api/jobs/{job_id}/reprocess", status_code=202)
    def reprocess(job_id: str, request: Request):
        require_model_owner(request, visible(request, "jobs", job_id, edit=True))
        return public_job(service.reprocess(job_id, owner=principal(request).owner, admin=principal(request).admin))

    @app.get("/api/assets")
    def assets(request: Request, limit: int = Query(100, ge=1, le=500), all_users: bool = True):
        return {"items": [public_record(service.asset(a["id"])) for a in store.list("assets", limit, owner_id=listed_owner(request, all_users))]}

    @app.get("/api/assets/{asset_id}")
    def asset(asset_id: str, request: Request):
        visible(request, "assets", asset_id)
        return public_record(service.asset(asset_id))

    @app.get("/api/assets/{asset_id}/export.zip")
    def export(asset_id: str, request: Request):
        with store.resource_guard("assets", asset_id):
            visible(request, "assets", asset_id)
            service.asset(asset_id)
            folder = safe_path(service.root, "assets/" + asset_id, must_exist=False)
            target = folder / "export-public-v2.zip"
            if not target.exists():
                part = folder / ("export-" + uuid.uuid4().hex + ".zip.part")
                checksums = {}
                with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for source in sorted(folder.rglob("*")):
                        if source.is_file() and not source.name.startswith("export") and source.name != 'checksums.json' and source.resolve().is_relative_to(folder.resolve()):
                            name = source.relative_to(folder).as_posix()
                            if source.suffix == '.json':
                                data = json.dumps(public_document(json.loads(source.read_text(encoding='utf-8')), source.name), ensure_ascii=False, indent=2).encode()
                                archive.writestr(name, data)
                                checksums[name] = hashlib.sha256(data).hexdigest()
                            else:
                                archive.write(source, name)
                                checksums[name] = sha256(source)
                    archive.writestr('checksums.json', json.dumps(checksums, indent=2))
                part.replace(target)
            return FileResponse(target, media_type="application/zip", filename=f"animation-{asset_id}.zip")

    @app.get("/api/notifications")
    def notifications(request: Request, limit: int = Query(100, ge=1, le=500), all_users: bool = True):
        return {"items": store.list("notifications", limit, owner_id=listed_owner(request, all_users))}

    @app.get("/api/notifications/{notification_id}")
    def notification(notification_id: str, request: Request):
        return visible(request, "notifications", notification_id)

    @app.post("/api/notifications", status_code=201)
    def notify(body: NotificationInput, request: Request):
        if body.job_id:
            visible(request, "jobs", body.job_id, edit=True)
        return store.notify(**body.model_dump(), owner=principal(request).owner)

    def deletion_route(table):
        def delete_record(object_id: str, request: Request):
            return service.delete(table, object_id, actor_id=principal(request).id, admin=principal(request).admin)
        return delete_record

    for table in ("characters", "jobs", "assets", "notifications", "motions", "uploads"):
        app.add_api_route("/api/" + table + "/{object_id}", deletion_route(table),
                          methods=["DELETE"], name="delete_" + table)

    class Batch(BaseModel):
        batch_id: str = Field(min_length=1, max_length=100)
        tasks: list[AnimationTask] = Field(min_length=1, max_length=10)

    @app.post("/api/batches", status_code=202)
    def batch(body: Batch, request: Request):
        actor = principal(request)
        tasks = []
        for index, item in enumerate(body.tasks):
            task = item.model_dump()
            task["idempotency_key"] = "batch:" + body.batch_id + ":" + str(index)
            service.snapshot(task, owner=actor.owner, admin=actor.admin)
            tasks.append(task)
        # Durable per-task idempotency makes an interrupted batch safe to repeat.
        jobs = [public_job(service.enqueue(task, owner=actor.owner, admin=actor.admin)) for task in tasks]
        return {"batch_id": body.batch_id, "jobs": jobs, "count": len(jobs)}

    @app.get("/api/health/live")
    def live():
        return {"status": "ok", "service": "animation-api"}

    @app.get("/api/health")
    def health(request: Request, prompt_mode: Literal["assisted", "direct"] = "assisted"):
        result = {"status": "ok", "providers": {}, "queue": {}, "worker": None}
        try:
            with store.connection() as conn:
                operator = principal(request).admin
                if operator:
                    counts = conn.execute("SELECT status,count(*) AS count FROM jobs WHERE deleted_at IS NULL GROUP BY status").fetchall()
                else:
                    counts = conn.execute("SELECT status,count(*) AS count FROM jobs WHERE deleted_at IS NULL AND owner_id=%s GROUP BY status",
                                          (principal(request).id,)).fetchall()
                result["queue"] = {r["status"]: r["count"] for r in counts}
                worker = conn.execute("SELECT state,updated_at FROM worker_state WHERE id='main'").fetchone()
                # Operator CLI needs complete queue state. Browser users must not see other job IDs.
                if operator or not worker:
                    result["worker"] = worker
                else:
                    result["worker"] = {"state": {"status": worker["state"].get("status")}, "updated_at": worker["updated_at"]}
                result["database"] = True
        except Exception:
            result.update(status="degraded", database=False)
        try:
            result["redis"] = bool(store.redis.ping())
        except Exception:
            result["redis"] = False
        readiness = service.clients.generation_readiness(prompt_mode=prompt_mode)
        result["providers"].update(readiness["services"])
        result.update(generation_ready=readiness["ready"], generation_missing_services=readiness["missing_services"],
                      generation_message=readiness["message"])
        from .matting import configuration
        result["matting"] = configuration()
        if not result["matting"]["available"]:
            result.update(generation_ready=False, generation_message=result["matting"]["message"])
        return result

    @app.get("/files/{path:path}")
    def media(path: str, request: Request):
        source = safe_path(service.root, path, must_exist=False)
        if not source.is_file():
            raise KeyError(path)
        normalized = relative_path(service.root, source)
        parts = Path(normalized).parts
        if len(parts) >= 3 and parts[0] in {"jobs", "assets"}:
            row = visible(request, parts[0], parts[1])
            if parts[0] == 'jobs' and tuple(parts[2:]) != ('generation.mp4',):
                # Job work directories contain provider receipts and caches;
                # only the original video is an intended intermediate download.
                raise KeyError(path)
            if parts[0] == 'assets':
                if source.suffix == '.zip' and source.name.startswith('export'):
                    return export(parts[1], request)
                if source.suffix == '.json':
                    return JSONResponse(public_document(json.loads(source.read_text(encoding='utf-8')), source.name,
                        owner_id=row['owner_id'], viewer_id=principal(request).id))
        else:
            uploads, characters, motions = registered_paths(normalized)
            shared_reference = bool(characters or motions)
            if not shared_reference and not uploads:
                raise KeyError(path)
        return FileResponse(source)

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")
    app.mount("/", StaticFiles(directory=static, html=True, check_dir=False), name="portal")
    return app


app = create_app()

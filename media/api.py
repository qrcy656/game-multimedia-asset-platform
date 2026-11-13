"""Four isolated, identity-aware HTTP applications; inference stays on the host."""
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
import asyncio
from providers import data_root, feature_capabilities
from .queue import ApiQueue, QueueError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, UnidentifiedImageError

from .common import JOB_ID, MODULES, csrf_token, csrf_valid, identity, module_data, safe_child
from .image_library import ImageLibrary, LibraryError, MAX_BATCH_IMAGES, MAX_IMAGE_BYTES, inspect_image
from .catalog import prompt_draft

MAX_UPLOAD = 256 * 1024 * 1024

def create_app(module=None, root=None, identity_secret=None, control_secret=None, transport=None, queue=None):
    module = module or os.environ.get("WORKBENCH_MODULE", "image")
    root = Path(root or data_root())
    queue = queue or ApiQueue(root)
    identity_secret = identity_secret or os.environ.get("COMMUNITY_IDENTITY_SECRET", "")
    control_secret = control_secret or ""
    if module not in MODULES:
        raise RuntimeError("Unknown module")
    app = FastAPI(title=MODULES[module]["title"], docs_url=None, redoc_url=None, openapi_url=None)
    data = module_data(root, module)
    static = Path(__file__).parent / "static"
    library = ImageLibrary(data) if module == "image" else None

    @app.exception_handler(LibraryError)
    async def library_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    async def call(method, path, payload=None, query=None):
        try:
            return await asyncio.to_thread(queue.handle, method, path, payload, query)
        except QueueError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.middleware("http")
    async def boundary(request, next_call):
        if request.url.path == "/health":
            return await next_call(request)
        try:
            auth = request.headers.get("authorization", "")
            user = identity(auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else "", identity_secret, module)
            request.state.user = user
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                expected = os.environ.get("APP_ORIGIN", "http://localhost:18082")
                if request.headers.get("origin") != expected or not csrf_valid(request.headers.get("x-csrf-token", ""), identity_secret, module, user["id"]):
                    return JSONResponse({"detail": "页面验证已失效，请刷新后重试。"}, status_code=403)
                length = request.headers.get("content-length", "0")
                if not length.isdigit() or int(length) > MAX_UPLOAD + 1024 * 1024:
                    return JSONResponse({"detail": "上传总大小不能超过 256 MiB。"}, status_code=413)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401)
        response = await next_call(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'self'; base-uri 'none'"
        return response

    def principal(request):
        return request.state.user

    def scope(request):
        user = principal(request)
        view = request.query_params.get("scope", "all")
        if view not in {"mine", "all"}:
            raise HTTPException(422, "内容查看范围无效。")
        return {"module": module, "owner": user["id"], "admin": "1" if user["admin"] else "0", "scope": view}

    def decorate(job):
        for artifact in job.get("artifacts", []):
            artifact["url"] = f"/{module}/api/jobs/{job['id']}/files/" + quote(artifact["name"], safe="/")
        return job

    async def owned_job(request, job_id):
        if not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, "任务不存在。")
        # Every authenticated member can read shared results; writes are checked in the queue.
        result = await call("GET", "/jobs/" + job_id, query=scope(request))
        return decorate(result)

    @app.get("/health")
    async def health():
        return {"status": "ok", "module": module}

    @app.get("/api/session")
    async def session(request: Request):
        user = principal(request)
        return {"csrf": csrf_token(identity_secret, module, user["id"]), "user": user}

    @app.get("/api/config")
    async def config(request: Request):
        state = await call("GET", "/models")
        return {"module": module, **MODULES[module], **await session(request),
                "models": [model for model in state["models"] if model["module"] == module],
                "draft_model": prompt_draft(module),
                **feature_capabilities(module),
                "image_profiles": module == "image",
                "prompt_assistant": module in {"image", "speech", "music"},
                "home_url": "/", "logout_url": "/logout"}

    @app.get("/api/jobs")
    async def jobs(request: Request):
        result = await call("GET", "/jobs", query=scope(request))
        return {"jobs": [decorate(job) for job in result["jobs"]]}

    @app.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: str):
        return await owned_job(request, job_id)

    @app.post("/api/compile")
    @app.post("/api/jobs", status_code=201)
    async def create_job(request: Request):
        form = await request.form(max_files=1, max_fields=7, max_part_size=128 * 1024)
        upload_path = None
        try:
            model_id = form.get("model_id")
            params = json.loads(form.get("params", "{}"))
            if not isinstance(model_id, str) or not isinstance(params, dict):
                raise ValueError("无效任务参数。")
            upload = form.get("file")
            upload_id = None
            reference = None
            profile_id, image_id = form.get("profile_id"), form.get("image_id")
            if profile_id or image_id:
                if not library or not profile_id or not image_id or upload and getattr(upload, "filename", ""):
                    raise ValueError("请选择一张档案图片，或使用临时上传图片，不能同时指定。")
                source = library.file(profile_id, image_id)
                upload_id = uuid.uuid4().hex + source.suffix.lower()
                upload_path = data / "uploads" / upload_id
                upload_path.parent.mkdir(parents=True, exist_ok=True)
                reference = library.snapshot(profile_id, image_id, upload_path)
            if upload and getattr(upload, "filename", ""):
                extension = Path(upload.filename).suffix.lower()
                if extension not in {".png", ".jpg", ".jpeg", ".webp", ".zip"}:
                    raise ValueError("参考图支持 PNG、JPG、WebP；训练数据支持 ZIP。")
                upload_id = uuid.uuid4().hex + extension
                upload_path = data / "uploads" / upload_id
                upload_path.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with upload_path.open("xb") as stream:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > (MAX_UPLOAD if extension == ".zip" else MAX_IMAGE_BYTES if module == "image" else 16 * 1024 * 1024):
                            raise ValueError("图片模块参考图不能超过 200 MiB，3D 参考图不能超过 16 MiB，训练 ZIP 不能超过 256 MiB。")
                        stream.write(chunk)
                if extension != ".zip":
                    if module == "image":
                        inspect_image(upload_path)
                    else:
                        with Image.open(upload_path) as picture:
                            if picture.width * picture.height > 36_000_000:
                                raise ValueError("参考图不能超过 3600 万像素。")
                            picture.verify()
            preview = request.url.path == "/api/compile"
            result = await call("POST", "/compile" if preview else "/jobs", {"module": module, "owner": principal(request), "model_id": model_id, "params": params, "prompt_mode": form.get("prompt_mode", "direct"), "upload_id": upload_id, "reference": reference})
            return result if preview else decorate(result)
        except LibraryError:
            raise
        except (ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "无法读取参考图片。") from None
        finally:
            if upload_path:
                upload_path.unlink(missing_ok=True)
            await form.close()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(request: Request, job_id: str):
        await owned_job(request, job_id)
        return decorate(await call("POST", f"/jobs/{job_id}/cancel", scope(request)))

    @app.post("/api/jobs/{job_id}/retry", status_code=201)
    async def retry(request: Request, job_id: str):
        await owned_job(request, job_id)
        return decorate(await call("POST", f"/jobs/{job_id}/retry", {**scope(request), "actor": principal(request)}))

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(request: Request, job_id: str):
        if not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, "任务不存在。")
        return await call("POST", f"/jobs/{job_id}/delete", scope(request))

    def require_library():
        if not library:
            raise HTTPException(404, "此模块没有图片档案。")
        return library

    @app.get("/api/profiles")
    async def profiles(request: Request):
        return {"items": require_library().list(principal(request))}

    @app.get("/api/profiles/{profile_id}")
    async def profile(request: Request, profile_id: str):
        return require_library().read(profile_id, principal(request))

    async def import_profile(request, profile_id=None):
        current = require_library()
        form = await request.form(max_files=MAX_BATCH_IMAGES, max_fields=5, max_part_size=128 * 1024)
        staged = []
        try:
            uploads = form.getlist("files")
            labels = json.loads(form.get("labels", "[]"))
            if not isinstance(labels, list) or labels and len(labels) != len(uploads):
                raise LibraryError("图片标签必须与上传文件一一对应。")
            total, items = 0, []
            for index, upload in enumerate(uploads):
                name = getattr(upload, "filename", "")
                if not name or Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise LibraryError("档案图片仅支持 PNG、JPEG 或 WebP。")
                target = data / "uploads" / (uuid.uuid4().hex + Path(name).suffix.lower())
                target.parent.mkdir(parents=True, exist_ok=True)
                staged.append(target)
                size = 0
                with target.open("xb") as stream:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_IMAGE_BYTES or total > MAX_UPLOAD:
                            raise LibraryError("单张图片最多 200 MiB，每批图片总计最多 256 MiB。", 413)
                        stream.write(chunk)
                items.append((target, labels[index] if labels else Path(name).stem[:80], Path(name).name))
            return current.add(principal(request), items, name=form.get("name"), description=form.get("description", ""), profile_id=profile_id)
        except json.JSONDecodeError:
            raise LibraryError("图片标签格式无效。") from None
        finally:
            for path in staged:
                path.unlink(missing_ok=True)
            await form.close()

    @app.post("/api/profiles", status_code=201)
    async def create_profile(request: Request):
        return await import_profile(request)

    @app.post("/api/profiles/{profile_id}/images", status_code=201)
    async def add_profile_images(request: Request, profile_id: str):
        return await import_profile(request, profile_id)

    @app.patch("/api/profiles/{profile_id}")
    async def update_profile(request: Request, profile_id: str):
        body = await request.json()
        if not isinstance(body, dict):
            raise LibraryError("档案信息格式无效。")
        return require_library().update(profile_id, principal(request), body)

    @app.patch("/api/profiles/{profile_id}/images/{image_id}")
    async def update_profile_image(request: Request, profile_id: str, image_id: str):
        body = await request.json()
        if not isinstance(body, dict):
            raise LibraryError("图片标签格式无效。")
        return require_library().update(profile_id, principal(request), body, image_id)

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(request: Request, profile_id: str):
        return require_library().delete(profile_id, principal(request))

    @app.delete("/api/profiles/{profile_id}/images/{image_id}")
    async def delete_profile_image(request: Request, profile_id: str, image_id: str):
        return require_library().delete(profile_id, principal(request), image_id)

    @app.get("/api/profiles/{profile_id}/images/{image_id}/file")
    async def profile_image(request: Request, profile_id: str, image_id: str):
        return FileResponse(require_library().file(profile_id, image_id), content_disposition_type="inline")

    @app.get("/api/jobs/{job_id}/files/{name:path}")
    async def file(request: Request, job_id: str, name: str):
        job = await owned_job(request, job_id)
        artifact = next((item for item in job.get("artifacts", []) if item["name"] == name), None)
        if artifact is None:
            raise HTTPException(404, "文件不存在。")
        try:
            path = safe_child(data / "jobs" / job_id / "output", name)
        except ValueError:
            raise HTTPException(404, "文件不存在。") from None
        if not path.is_file():
            raise HTTPException(404, "文件不存在。")
        inline = artifact["kind"] in {"image", "audio", "model"}
        return FileResponse(path, filename=path.name, content_disposition_type="inline" if inline and request.query_params.get("download") != "1" else "attachment")

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    @app.get("/static/{name:path}")
    async def asset(name: str):
        try:
            path = safe_child(static, name)
        except ValueError:
            raise HTTPException(404) from None
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    return app


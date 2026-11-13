"""Authenticated narrative editor HTTP API; inference and models are not involved."""
import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .auth import csrf_token, csrf_valid, identity
from .domain import DomainError, MAX_DOCUMENT_BYTES, MAX_PROJECTS, ProjectStore, obj, text, prepare_document
from .seed import blank, snow

ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:18082")
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob: data:; connect-src 'self'; font-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"


def create_app(data_dir=None, identity_secret=None, static_dir=None):
    identity_secret = identity_secret if identity_secret is not None else os.environ.get("MEDIA_IDENTITY_SECRET", "")
    data_dir = data_dir or os.environ.get("NARRATIVE_DATA", "/app/data")
    static = Path(static_dir or Path(__file__).parent / "static").resolve()
    app = FastAPI(title="剧情设计", docs_url=None, redoc_url=None, openapi_url=None)
    store = ProjectStore(data_dir)
    app.state.store = store

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(sqlite3.OperationalError)
    async def unavailable(request, exc):
        return JSONResponse({"detail": "项目存储暂时不可用，请稍后重试。"}, status_code=503)

    @app.middleware("http")
    async def boundary(request, next_call):
        response = None
        if request.url.path != "/health":
            try:
                auth = request.headers.get("authorization", "")
                user = identity(auth[7:] if auth.startswith("Bearer ") else "", identity_secret)
                request.state.user = user
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if request.headers.get("origin") != ORIGIN or not csrf_valid(request.headers.get("x-csrf-token", ""), identity_secret, user["id"]):
                        response = JSONResponse({"detail": "页面验证已失效，请刷新后重试。"}, status_code=403)
                    length = request.headers.get("content-length", "0")
                    if not length.isdigit() or len(length) > 10 or int(length) > MAX_DOCUMENT_BYTES:
                        response = JSONResponse({"detail": "请求内容不能超过 32 MiB。"}, status_code=413)
            except ValueError as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=401)
        if response is None:
            response = await next_call(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = CSP
        return response

    def owner(request):
        return request.state.user["id"]

    def all_owners(request):
        scope = request.query_params.get("scope", "all")
        if scope not in {"mine", "all"}:
            raise DomainError("项目查看范围无效。", 422)
        return scope == "all"

    async def payload(request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise DomainError("请使用 JSON 提交剧情数据。", 415)
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > MAX_DOCUMENT_BYTES:
                raise DomainError("请求内容不能超过 32 MiB。", 413)
            chunks.append(chunk)

        def strict_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        def invalid_constant(value):
            raise ValueError("non-finite number")

        try:
            result = json.loads(b"".join(chunks), object_pairs_hook=strict_object, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise DomainError("JSON 数据无效，请检查文件格式。") from None
        if not isinstance(result, dict):
            raise DomainError("请求必须为 JSON 对象。")
        return result

    @app.get("/health")
    async def health():
        return {"ok": len(identity_secret) >= 32}

    @app.get("/api/config")
    async def config(request: Request):
        return {"title": "剧情设计", "user": request.state.user, "csrf": csrf_token(identity_secret, owner(request)), "home_url": "/", "logout_url": "/logout", "limits": {"projects": MAX_PROJECTS, "document_bytes": MAX_DOCUMENT_BYTES, "asset_bytes": 5 * 1024 * 1024, "assets_bytes": 20 * 1024 * 1024}}

    @app.get("/api/projects")
    async def projects(request: Request):
        return {"projects": await run_in_threadpool(store.list, owner(request), all_owners=all_owners(request))}

    @app.post("/api/projects", status_code=201)
    async def create(request: Request):
        body = await payload(request)
        obj(body, {"title", "template"}, "新建项目请求")
        title = text(body["title"], 120, "项目名称", False)
        if body["template"] not in ("blank", "snow"):
            raise DomainError("请选择空白项目或雪原示例。")
        document = snow(title) if body["template"] == "snow" else blank(title)
        return await run_in_threadpool(store.create, owner(request), document)

    @app.post("/api/import", status_code=201)
    async def import_project(request: Request):
        body = await payload(request)
        obj(body, {"document"}, "导入项目请求")
        return await run_in_threadpool(store.create, owner(request), body["document"])

    @app.post("/api/import/prepare")
    async def prepare_import(request: Request):
        body = await payload(request)
        obj(body, {"document"}, "准备子剧情导入请求")
        return {"document": await run_in_threadpool(prepare_document, body["document"])}

    @app.get("/api/projects/{project_id}")
    async def read(request: Request, project_id: str):
        all_owners(request)  # Validate the optional listing filter; reads are shared.
        return await run_in_threadpool(store.read, owner(request), project_id, admin=request.state.user["admin"])

    @app.post("/api/projects/{project_id}/copy", status_code=201)
    async def copy_project(request: Request, project_id: str):
        source = await run_in_threadpool(store.read, owner(request), project_id)
        document = source["document"]
        document["title"] = document["title"][:115] + "（副本）"
        return await run_in_threadpool(store.create, owner(request), document)

    @app.put("/api/projects/{project_id}")
    async def save(request: Request, project_id: str):
        body = await payload(request)
        obj(body, {"version", "document"}, "保存项目请求")
        return await run_in_threadpool(store.update, owner(request), project_id, body["version"], body["document"], admin=request.state.user["admin"])

    @app.delete("/api/projects/{project_id}")
    async def delete(request: Request, project_id: str):
        value = request.query_params.get("version", "")
        if not value.isascii() or not value.isdigit() or len(value) > 10:
            raise DomainError("删除必须提供有效版本号。")
        await run_in_threadpool(store.delete, owner(request), project_id, int(value), admin=request.state.user["admin"])
        return {"deleted": True}

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "接口不存在。"}, status_code=404)
        target = (static / (path or "index.html")).resolve()
        if static not in target.parents or not target.is_file():
            return JSONResponse({"detail": "页面不存在。"}, status_code=404)
        return FileResponse(target)

    return app



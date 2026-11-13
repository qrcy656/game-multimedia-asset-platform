"""Community portal. Mount this app last at '/'; reuse state.auth and state.registry."""
from pathlib import Path
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import AuthError, AuthStore, COOKIE, SESSION_SECONDS, authenticate
from .registry import MODULE_ROLES, PROTOCOL_KINDS, Registry, RegistryError, RegistryPermissionError
from providers import model_context, parse_model_selection


def create_app(data_dir):
    app = FastAPI(title="Fantasy Community", docs_url=None, redoc_url=None)
    app.state.auth = AuthStore(data_dir)
    app.state.registry = Registry(data_dir)
    static = Path(__file__).parent / "static"
    app.mount("/portal-static", StaticFiles(directory=static), name="portal-static")

    @app.middleware("http")
    async def headers(request, call_next):
        try:
            selections = parse_model_selection(request.headers.get('X-Fantasy-Model-Selection'))
        except RegistryError as error:
            return JSONResponse({'detail': str(error)}, status_code=400)
        user = authenticate(request, app.state.auth)
        selection_actor = request.headers.get('X-Fantasy-Model-Account')
        if selection_actor and user and selection_actor != str(user['id']):
            return JSONResponse({'detail': '登录账号已切换，请刷新页面。'}, status_code=409)
        with model_context(user, selections):
            response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(AuthError)
    @app.exception_handler(RegistryError)
    async def controlled_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=403 if isinstance(error, RegistryPermissionError) else 400)

    async def body(request):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "请求需使用 JSON")
        content = await request.body()
        if len(content) > 32768:
            raise HTTPException(413, "请求过大")
        try:
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "请求 JSON 无效") from None

    def require(request, *, admin=False, mutate=False):
        user = authenticate(request, app.state.auth)
        if not user:
            raise HTTPException(401, "请先登录")
        if admin and not user["admin"]:
            raise HTTPException(403, "仅管理员可管理模型配置和账号")
        if mutate and not app.state.auth.validate_csrf(request):
            raise HTTPException(403, "请求验证失败，请刷新页面")
        return user

    def set_session(response, request, session):
        response.set_cookie(COOKIE, session["token"], max_age=SESSION_SECONDS,
            secure=request.url.scheme == "https", httponly=True, samesite="lax", path="/")

    async def revoke_external_session(token):
        hook = getattr(app.state, "external_session_logout", None)
        if hook is not None:
            try:
                await hook(token)
            except Exception:
                # Local logout has already completed. A provider outage must not
                # preserve the browser's community session or disclose its token.
                pass

    @app.get("/")
    @app.get("/login")
    @app.get("/logout")
    async def home():
        return FileResponse(static / "index.html")

    @app.get("/health")
    async def health():
        return {"status": "ok", "edition": "community"}

    @app.get('/community.js')
    async def browser_preferences():
        return FileResponse(Path(__file__).parent.parent / 'community.js', media_type='application/javascript')

    @app.get("/api/session")
    async def session(request: Request):
        user = require(request)
        return {"user": user, "csrf": request.state.community_session["csrf"]}

    @app.post("/api/login")
    async def login(request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "登录来源不匹配")
        payload = await body(request)
        repository = getattr(app.state, 'repository', None)
        if payload.get('credential_type') == 'gitlab_pat':
            if repository is None:
                raise HTTPException(400, '尚未配置 GitLab 统一登录')
            from repository import RepositoryError
            identity, session = None, None
            try:
                identity = await repository.complete_pat(payload.get('token'), payload.get('username'))
                user = app.state.auth.get_or_create_external('gitlab', identity['external_id'],
                    identity['name'], identity['username'], admin=identity['admin'])
                session = app.state.auth.new_session(user['id'])
                repository.bind_session(identity['grant_id'], session['token'])
            except (RepositoryError, AuthError) as error:
                if session:
                    app.state.auth.logout(session['token'])
                raise HTTPException(401, str(error)) from None
            finally:
                if identity:
                    repository.discard_grant(identity['grant_id'])
        elif payload.get('credential_type', 'local') == 'local':
            if repository:
                repository.sync_managed_bootstrap()
            user, session = app.state.auth.login(payload.get("username"), payload.get("password"), request.client.host if request.client else "")
        else:
            raise HTTPException(400, '不支持的登录凭据类型')
        old = request.cookies.get(COOKIE)
        if old:
            app.state.auth.logout(old)
            await revoke_external_session(old)
        response = JSONResponse({"user": user, "csrf": session["csrf"]})
        set_session(response, request, session)
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        require(request, mutate=True)
        app.state.auth.logout(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        await revoke_external_session(request.cookies.get(COOKIE, ""))
        return response

    @app.post("/api/password")
    async def password(request: Request):
        user = require(request, mutate=True)
        payload = await body(request)
        app.state.auth.change_password(user["id"], payload.get("old_password"), payload.get("new_password"))
        session = app.state.auth.new_session(user["id"])
        response = JSONResponse({"ok": True, "csrf": session["csrf"]})
        set_session(response, request, session)
        return response

    @app.get("/api/users")
    async def users(request: Request):
        require(request, admin=True)
        repository = getattr(app.state, 'repository', None)
        management = repository.account_management() if repository else {'provider': 'local', 'url': '', 'profile_url': ''}
        return {"users": app.state.auth.list_users(), 'management': management}

    @app.post("/api/users", status_code=201)
    async def add_user(request: Request):
        require(request, admin=True, mutate=True)
        repository = getattr(app.state, 'repository', None)
        if repository and repository.account_management()['provider'] == 'gitlab':
            raise HTTPException(403, '请在 GitLab 用户管理中创建账号；用户首次登录平台时会自动建立资料')
        payload = await body(request)
        return app.state.auth.create_user(payload.get("username"), payload.get("password"), payload.get("name"), payload.get("admin", False))

    @app.patch("/api/users/{user_id}")
    async def edit_user(user_id: str, request: Request):
        actor = require(request, admin=True, mutate=True)
        return app.state.auth.update_user(actor["id"], user_id, await body(request))

    @app.post("/api/users/{user_id}/password")
    async def reset_user_password(user_id: str, request: Request):
        actor = require(request, admin=True, mutate=True)
        payload = await body(request)
        if set(payload) != {"new_password"}:
            raise HTTPException(400, "请提供新密码")
        app.state.auth.reset_password(actor["id"], user_id, payload["new_password"])
        return {"ok": True}

    @app.delete("/api/users/{user_id}")
    async def delete_user(user_id: str, request: Request):
        actor = require(request, admin=True, mutate=True)
        app.state.auth.delete_user(actor["id"], user_id)
        return {"ok": True}

    @app.get("/api/profiles")
    async def profiles(request: Request):
        require(request)
        return {"profiles": app.state.registry.list_profiles(), "bindings": app.state.registry.list_bindings(),
            "default_bindings": app.state.registry.list_bindings(effective=False),
            "module_roles": MODULE_ROLES, "protocol_kinds": {key: sorted(value) for key, value in PROTOCOL_KINDS.items()}}

    @app.post("/api/profiles", status_code=201)
    async def add_profile(request: Request):
        require(request, mutate=True)
        return app.state.registry.save_profile(await body(request))

    @app.put("/api/profiles/{profile_id}")
    async def edit_profile(profile_id: str, request: Request):
        require(request, mutate=True)
        return app.state.registry.save_profile(await body(request), profile_id)

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: str, request: Request):
        require(request, mutate=True)
        app.state.registry.delete_profile(profile_id)
        return {"ok": True}

    @app.post("/api/profiles/{profile_id}/verify")
    async def verify_profile(profile_id: str, request: Request):
        require(request, mutate=True)
        payload = await body(request)
        if type(payload.get("allow_generation_probe", False)) is not bool:
            raise HTTPException(400, "生成检测选项无效")
        return await app.state.registry.verify(profile_id, allow_generation_probe=payload.get("allow_generation_probe", False))

    @app.get("/api/bindings")
    async def bindings(request: Request):
        require(request)
        return {"bindings": app.state.registry.list_bindings(), "module_roles": MODULE_ROLES}

    @app.put("/api/bindings/{module}/{role}")
    async def bind(module: str, role: str, request: Request):
        require(request, admin=True, mutate=True)
        payload = await body(request)
        app.state.registry.bind(module, role, payload.get("profile_id"))
        return {"ok": True}

    return app


__all__ = ["create_app", "authenticate", "AuthStore", "Registry", "RegistryError", "MODULE_ROLES"]

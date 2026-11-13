"""Single-origin community portal with isolated, API-backed creative modules."""
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from portal import create_app as create_portal
from portal.registry import RegistryError, RegistryPermissionError
from portal.auth import AuthError, COOKIE, SESSION_SECONDS
from providers import data_root, feature_capabilities, model_context, parse_model_selection
from repository import RepositoryStore, RepositoryError
from effects_bridge import EffectsBridge

MODULES = ('knowledge', 'animation', 'image', 'speech', 'music', 'model3d', 'narrative', 'effects')


def identity_key(root):
    configured = os.environ.get('COMMUNITY_IDENTITY_SECRET')
    if configured and len(configured) >= 32:
        return configured
    path = root / 'module-identity.key'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_hex(32))
    return path.read_text()


def identity_token(user, secret, module):
    encode = lambda data: base64.urlsafe_b64encode(data).decode().rstrip('=')
    now = int(time.time())
    audience = 'knowledge-ques' if module == 'knowledge' else 'animation-studio' if module == 'animation' else 'fantasy-' + module
    claims = {'iss': 'fantasy-main', 'aud': audience, 'sub': user['id'], 'name': user['name'],
              'role': 'admin' if user['admin'] else 'user', 'iat': now, 'exp': now + 60}
    message = encode(b'{"alg":"HS256","typ":"JWT"}') + '.' + encode(json.dumps(claims).encode())
    return message + '.' + encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest())


class ModuleBridge:
    """Only authenticated portal sessions can reach an internal module app."""
    def __init__(self, application, module, secret):
        self.application, self.module, self.secret = application, module, secret

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.application(scope, receive, send)
        user = scope.get('state', {}).get('community_user')
        if not user:
            return await JSONResponse({'detail': '请先登录社区版。'}, status_code=401)(scope, receive, send)
        inner = dict(scope)
        prefix = '/' + self.module
        inner['path'] = scope['path'][len(prefix):] or '/'
        inner['raw_path'] = scope.get('raw_path', scope['path'].encode())[len(prefix):] or b'/'
        inner['root_path'] = ''
        inner['headers'] = [(key, value) for key, value in scope['headers']
            if key.lower() not in (b'authorization', b'x-api-key', b'x-animation-context')]
        inner['headers'].append((b'authorization', ('Bearer ' + identity_token(user, self.secret, self.module)).encode()))
        return await self.application(inner, receive, send)


def create_app(root=None, *, enable_animation=True, run_queue=True):
    root = Path(root or data_root()).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['COMMUNITY_DATA_DIR'] = str(root)
    secret = identity_key(root)
    os.environ['COMMUNITY_IDENTITY_SECRET'] = secret
    os.environ['MEDIA_IDENTITY_SECRET'] = secret
    os.environ.setdefault('ANIMATION_IDENTITY_SECRET', secret)
    os.environ.setdefault('ANIMATION_IDENTITY_ISSUER', 'fantasy-main')
    os.environ.setdefault('ANIMATION_IDENTITY_AUDIENCE', 'animation-studio')
    os.environ.setdefault('ANIMATION_API_KEY', secrets.token_hex(32))
    os.environ['ANIMATION_DATA_DIR'] = str(root / 'animation')
    os.environ['NARRATIVE_DATA'] = str(root / 'narrative')
    portal = create_portal(root)
    repository = RepositoryStore(root, auth=portal.state.auth)
    portal.state.repository = repository
    portal.state.external_session_logout = repository.revoke_session
    from media.queue import ApiQueue
    from media.api import create_app as create_media
    from narrative.api import create_app as create_narrative
    from knowledge.backend.app import app as knowledge
    queue = ApiQueue(root)
    animation = None
    if enable_animation:
        from animation.api import create_app as create_animation
        animation = create_animation()

    @asynccontextmanager
    async def lifespan(application):
        # Mounted apps do not receive ASGI lifespan automatically.
        if animation:
            async with animation.router.lifespan_context(animation):
                if run_queue: queue.start()
                try: yield
                finally: queue.stop()
        else:
            if run_queue: queue.start()
            try: yield
            finally: queue.stop()

    app = FastAPI(title='Fantasy 创作平台 · 社区版', docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.auth, app.state.registry, app.state.queue = portal.state.auth, portal.state.registry, queue
    app.state.repository = repository
    effects = EffectsBridge(lambda user: identity_token(user, secret, 'effects'))
    app.state.effects = effects

    def require_user(request, *, admin=False, mutate=False):
        user = portal.state.auth.authenticate(request)
        if not user:
            raise HTTPException(401, '请先登录社区版。')
        if admin and not user['admin']:
            raise HTTPException(403, '仅管理员可以更改代码仓库设置。')
        if mutate and not portal.state.auth.validate_csrf(request):
            raise HTTPException(403, '页面验证已失效，请刷新后重试。')
        return user

    def callback_uri():
        # Use the installation's trusted public origin, never a caller's Host.
        from urllib.parse import urlsplit
        origin = os.environ.get('APP_ORIGIN', 'http://localhost:18082').rstrip('/')
        try:
            parsed = urlsplit(origin)
            parsed.port
        except ValueError:
            raise HTTPException(503, '请管理员检查 APP_ORIGIN 对外访问地址。') from None
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or any(character.isspace() for character in origin)):
            raise HTTPException(503, '请管理员检查 APP_ORIGIN 对外访问地址。')
        return origin + '/auth/gitlab/callback'

    def public_repository():
        return {**repository.get_public(), 'callback_url': callback_uri()}

    def oauth_origin_matches(request, callback):
        from urllib.parse import urlsplit
        canonical = urlsplit(callback)
        # Cookies are host-bound. Normalize aliases before creating OAuth state,
        # using only the configured destination, never caller-supplied redirects.
        # Compare authority rather than the ASGI scheme: HTTPS may terminate at
        # a reverse proxy while this app runs HTTP with --no-proxy-headers.
        default_port = 443 if canonical.scheme == 'https' else 80
        try:
            return (request.url.hostname, request.url.port or default_port) == (
                canonical.hostname, canonical.port or default_port)
        except ValueError:
            return False

    async def controlled_body(request):
        if request.headers.get('content-type', '').split(';', 1)[0] != 'application/json':
            raise HTTPException(415, '请求需使用 JSON。')
        raw = await request.body()
        if len(raw) > 16384:
            raise HTTPException(413, '请求过大。')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, '请求 JSON 无效。') from None
        if not isinstance(payload, dict):
            raise HTTPException(400, '请求 JSON 无效。')
        return payload

    @app.exception_handler(RepositoryError)
    @app.exception_handler(AuthError)
    async def repository_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    @app.get('/api/workspace')
    async def workspace(request: Request):
        require_user(request)
        repo = public_repository()
        modules = {'gitlab': {'status': 'ready' if repo['configured'] else 'not_configured',
            'detail': '代码仓库入口已配置' if repo['configured'] else '请管理员配置独立或已有的 GitLab',
            'href': '/gitlab/'}}
        required = {'knowledge': ('llm',), 'animation': ('video', 'image'),
                    'image': ('image',), 'speech': ('speech',), 'music': ('music',),
                    'model3d': ('model3d',), 'narrative': ()}
        for module, roles in required.items():
            missing = []
            for role in roles:
                try:
                    portal.state.registry.resolve(module, role)
                except (RegistryError, ValueError, KeyError):
                    missing.append(role)
            if module == 'image' and missing:
                try:
                    portal.state.registry.resolve('image', 'workflow')
                    missing = []
                except (RegistryError, ValueError, KeyError):
                    pass
            capabilities = feature_capabilities(module, portal.state.registry)
            if capabilities['features'].get('generation', {}).get('available'):
                missing = []
            status = 'ready'
            detail = '本地管理可用；AI 功能按各自模型配置启用' if missing else ('本地功能可用' if not roles else '本地管理与生成所需 API 已就绪')
            if module == 'animation' and animation is None:
                status, detail = 'unavailable', '动画服务未启用'
                capabilities['local_available'] = False
            modules[module] = {'status': status, 'detail': detail, 'href': '/' + module + '/',
                               'model_status': 'not_configured' if missing else 'ready', **capabilities}
        effects_ready = await effects.available()
        modules['effects'] = {'status': 'ready' if effects_ready else 'unavailable',
            'detail': '粒子编辑、个人特效库和 PNG 导出可用' if effects_ready else '特效服务暂不可用',
            'href': '/effects/'}
        return {'modules': modules, 'repository': repo}

    @app.put('/api/repository')
    async def repository_settings(request: Request):
        require_user(request, admin=True, mutate=True)
        repository.save(await controlled_body(request))
        return public_repository()

    @app.get('/gitlab')
    @app.get('/gitlab/')
    async def code_repository(request: Request):
        user = portal.state.auth.authenticate(request)
        if not user:
            return RedirectResponse('/login', status_code=303)
        config = repository.get_public()
        if not config['configured']:
            raise HTTPException(409, '代码仓库尚未配置，请管理员在主页设置 GitLab 地址。')
        destination = config['url']
        theme = request.query_params.get('fantasy_theme')
        if config['mode'] == 'managed' and theme in ('light', 'dark'):
            from urllib.parse import urlencode, urlsplit, urlunsplit
            parsed = urlsplit(destination)
            # GitLab's root redirect drops query parameters before its theme adapter runs.
            path = '/dashboard/projects' if parsed.path in ('', '/') else parsed.path
            destination = urlunsplit((parsed.scheme, parsed.netloc, path,
                                      urlencode({'fantasy_theme': theme}), parsed.fragment))
        return RedirectResponse(destination, status_code=303)

    @app.get('/api/auth/options')
    async def auth_options():
        return repository.auth_options()

    @app.get('/api/logout-context')
    async def logout_context(request: Request):
        # Only identity and CSRF for the logout screen. Project/API requests
        # still require a valid external identity even while GitLab is offline.
        user = require_user(request)
        return {'user': user, 'csrf': request.state.community_session['csrf']}

    def oauth_response(request, authorization, *, json_response=False):
        response = (JSONResponse({'authorization_url': authorization['authorization_url']}) if json_response
                    else RedirectResponse(authorization['authorization_url'], status_code=303))
        response.set_cookie('fantasy_gitlab_state', authorization['cookie_state'],
            max_age=authorization['expires_in'], secure=request.url.scheme == 'https',
            httponly=True, samesite='lax', path='/auth/gitlab/')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/auth/gitlab/start')
    async def gitlab_login(request: Request):
        callback = callback_uri()
        if not oauth_origin_matches(request, callback):
            return RedirectResponse(callback.removesuffix('/callback') + '/start', status_code=303,
                headers={'Cache-Control': 'no-store'})
        return oauth_response(request, repository.begin_oauth(callback))

    @app.post('/auth/gitlab/link-start')
    async def gitlab_link(request: Request):
        user = require_user(request, mutate=True)
        callback = callback_uri()
        if not oauth_origin_matches(request, callback):
            raise HTTPException(409, '请从配置的统一入口 ' + callback.removesuffix('/auth/gitlab/callback')
                + '/login 重新登录后关联 GitLab。')
        return oauth_response(request, repository.begin_oauth(callback, actor_id=user['id']), json_response=True)

    @app.get('/auth/gitlab/callback')
    async def gitlab_callback(request: Request):
        identity, session = None, None
        try:
            if request.query_params.get('error'):
                raise RepositoryError('GitLab 登录未完成，请重新发起登录。')
            identity = await repository.complete_oauth(request.query_params.get('code'),
                request.query_params.get('state'), request.cookies.get('fantasy_gitlab_state'), callback_uri())
            actor = identity.get('actor_id')
            if actor:
                user = require_user(request)
                if user['id'] != actor:
                    raise HTTPException(403, '登录账号已变化，请重新发起 GitLab 关联。')
                try:
                    valid = await repository.validate_session(request.cookies.get(COOKIE))
                except RepositoryError as error:
                    raise HTTPException(503, str(error)) from None
                if not valid:
                    portal.state.auth.logout(request.cookies.get(COOKIE))
                    raise HTTPException(401, 'GitLab 登录已失效，请重新登录。')
                portal.state.auth.link_external(user['id'], 'gitlab', identity['external_id'])
                repository.discard_grant(identity['grant_id'])
                response = RedirectResponse('/', status_code=303)
            else:
                user = portal.state.auth.get_or_create_external('gitlab', identity['external_id'],
                    identity['name'], identity['username'], admin=identity['admin'])
                old = request.cookies.get(COOKIE)
                if old:
                    portal.state.auth.logout(old)
                    await repository.revoke_session(old)
                session = portal.state.auth.new_session(user['id'])
                repository.bind_session(identity['grant_id'], session['token'])
                response = RedirectResponse('/', status_code=303)
                response.set_cookie(COOKIE, session['token'], max_age=SESSION_SECONDS,
                    secure=request.url.scheme == 'https', httponly=True, samesite='lax', path='/')
        except (RepositoryError, AuthError) as error:
            if session:
                portal.state.auth.logout(session['token'])
            response = JSONResponse({'detail': str(error)}, status_code=400)
        except HTTPException as error:
            response = JSONResponse({'detail': error.detail}, status_code=error.status_code)
            if error.status_code == 401:
                response.delete_cookie(COOKIE, path='/')
        finally:
            if identity:
                repository.discard_grant(identity['grant_id'])
        response.delete_cookie('fantasy_gitlab_state', path='/auth/gitlab/')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        token = request.cookies.get(COOKIE)
        path = request.url.path
        session_check = (token and path not in ('/', '/login', '/logout', '/api/auth/options',
                         '/api/logout-context', '/api/logout', '/api/login', '/auth/gitlab/callback')
                         and not path.startswith(('/portal-static/', '/community.js')))
        if session_check and portal.state.auth.authenticate(request):
            try:
                valid = await repository.validate_session(token)
            except RepositoryError as error:
                return JSONResponse({'detail': str(error)}, status_code=503,
                    headers={'Cache-Control': 'no-store'})
            if not valid:
                portal.state.auth.logout(token)
                is_page = request.method == 'GET' and '/api/' not in path and not path.startswith('/auth/')
                response = (RedirectResponse('/login', status_code=303) if is_page
                            else JSONResponse({'detail': 'GitLab 登录已失效，请重新登录。'}, status_code=401))
                response.delete_cookie(COOKIE, path='/')
                response.headers['Cache-Control'] = 'no-store'
                return response
        module = request.url.path.split('/')[1]
        if module in MODULES:
            user = portal.state.auth.authenticate(request)
            if not user:
                if request.method == 'GET' and '/api/' not in request.url.path and '/files/' not in request.url.path:
                    return RedirectResponse('/login', status_code=303)
                return JSONResponse({'detail': '请先登录社区版。'}, status_code=401)
            request.state.community_user = user
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and not portal.state.auth.validate_csrf(request):
                return JSONResponse({'detail': '页面验证已失效，请刷新后重试。'}, status_code=403)
        try:
            selections = parse_model_selection(request.headers.get('X-Fantasy-Model-Selection'))
        except (ValueError, RegistryError) as error:
            return JSONResponse({'detail': str(error)}, status_code=400)
        model_user = portal.state.auth.authenticate(request)
        model_account = request.headers.get('X-Fantasy-Model-Account')
        if model_account and (not model_user or model_account != model_user['id']):
            return JSONResponse({'detail': '登录账号已切换，请刷新页面。'}, status_code=409)
        with model_context(model_user, selections):
            response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith(('/api/', '/auth/')):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/health')
    def health():
        return {'status': 'ok', 'edition': 'community'}

    @app.get('/community.js')
    def shared_script():
        return FileResponse(Path(__file__).parent / 'community.js', media_type='application/javascript')

    async def registry_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=403 if isinstance(error, RegistryPermissionError) else 422)

    subapps = {'knowledge': knowledge, 'narrative': create_narrative(root / 'narrative', secret)}
    for module in ('image', 'speech', 'music', 'model3d'):
        subapps[module] = create_media(module, root, secret, queue=queue)
    if animation:
        subapps['animation'] = animation
    for module, subapp in subapps.items():
        subapp.add_exception_handler(RegistryError, registry_error)
        app.mount('/' + module, ModuleBridge(subapp, module, secret))
    app.mount('/effects', effects)
    app.mount('/', portal)
    return app


app = create_app(enable_animation=os.environ.get('COMMUNITY_ENABLE_ANIMATION', 'true') == 'true',
                 run_queue=os.environ.get('COMMUNITY_RUN_QUEUE', 'true') == 'true')

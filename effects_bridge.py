"""Authenticated same-origin transport to the isolated effects renderer."""
import os
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse


class EffectsBridge:
    def __init__(self, token_for_user, base_url=None):
        self.token_for_user = token_for_user
        self.base_url = (base_url or os.environ.get('COMMUNITY_EFFECTS_URL', 'http://effects:8080')).rstrip('/')
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('COMMUNITY_EFFECTS_URL must be an internal HTTP(S) origin')
        self.transport = None

    async def available(self):
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=1.5, trust_env=False) as client:
                response = await client.get(self.base_url + '/health')
                return response.status_code == 200 and response.json().get('ok') is True
        except (httpx.HTTPError, ValueError):
            return False

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        user = scope.get('state', {}).get('community_user')
        if not user:
            return await JSONResponse({'detail': '请先登录社区版。'}, status_code=401)(scope, receive, send)
        request = Request(scope, receive)
        # Only these protocol headers cross the internal boundary. In particular,
        # no caller's Authorization, Cookie, Host or forwarding headers reach Node.
        allowed = {'content-type', 'origin', 'x-effects-owner', 'x-effects-csrf', 'if-none-match', 'if-match'}
        headers = {key: value for key, value in request.headers.items() if key.lower() in allowed}
        headers['authorization'] = 'Bearer ' + self.token_for_user(user)
        raw_path = scope.get('raw_path', scope['path'].encode())[len('/effects'):] or b'/'
        target = self.base_url + raw_path.decode('ascii')
        if scope.get('query_string'):
            target += '?' + scope['query_string'].decode('ascii')
        client = httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(connect=10, read=None, write=120, pool=10))
        upstream = None
        try:
            upstream = await client.send(client.build_request(request.method, target,
                headers=headers, content=request.stream()), stream=True)
            kept = {'content-type', 'content-length', 'content-encoding', 'content-disposition', 'etag',
                    'cache-control', 'pragma', 'expires', 'vary', 'content-security-policy',
                    'x-content-type-options', 'referrer-policy', 'x-frame-options'}
            response_headers = {key: value for key, value in upstream.headers.items()
                if key in kept or key.startswith('x-effects-')}
            response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                         headers=response_headers)
            await response(scope, receive, send)
        except httpx.HTTPError:
            if upstream is None:
                await JSONResponse({'detail': '特效服务暂不可用，请稍后重试。'}, status_code=503,
                    headers={'Cache-Control': 'no-store'})(scope, receive, send)
            else:
                raise
        finally:
            if upstream is not None:
                await upstream.aclose()
            await client.aclose()

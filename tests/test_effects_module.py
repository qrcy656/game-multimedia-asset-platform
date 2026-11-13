"""Community auth/CSRF boundary and the effects HTTP transport, using temporary data."""
import base64
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx
from fastapi.testclient import TestClient

ORIGIN = 'http://localhost:18082'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')


class EffectsModule(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='community-effects-test-')
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'COMMUNITY_DATA_DIR': self.temp.name,
            'COMMUNITY_ENABLE_ANIMATION': 'false', 'COMMUNITY_RUN_QUEUE': 'false',
            'APP_ORIGIN': ORIGIN, 'COMMUNITY_EFFECTS_URL': 'http://effects:8080'})
        env.start(); self.addCleanup(env.stop)
        application = importlib.import_module('app')
        self.app = application.create_app(Path(self.temp.name), enable_animation=False, run_queue=False)
        self.secret = os.environ['COMMUNITY_IDENTITY_SECRET']
        self.calls, self.library, self.exports = [], {}, {}
        self.app.state.effects.transport = httpx.MockTransport(self.provider)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)

    def reply(self, status=200, body=None, *, content=None, headers=None):
        raw = content if content is not None else json.dumps(body).encode()
        return httpx.Response(status, stream=httpx.ByteStream(raw), headers={
            'content-type': 'application/octet-stream' if content is not None else 'application/json',
            'cache-control': 'no-store, private', 'content-length': str(len(raw)), **(headers or {})})

    def csrf(self, owner):
        return hmac.new(self.secret.encode(), ('fantasy-effects\0csrf\0' + owner).encode(), hashlib.sha256).hexdigest()

    def provider(self, request):
        self.calls.append(request)
        self.assertEqual(str(request.url).split('/')[2], 'effects:8080')
        self.assertNotIn('cookie', request.headers)
        self.assertNotIn('x-community-csrf', request.headers)
        self.assertNotIn('x-forwarded-user', request.headers)
        if request.url.path == '/health':
            return self.reply(body={'ok': True})
        encoded = request.headers['authorization'].removeprefix('Bearer ')
        first, second, signature = encoded.split('.')
        calculated = base64.urlsafe_b64encode(hmac.new(self.secret.encode(), (first + '.' + second).encode(), hashlib.sha256).digest()).decode().rstrip('=')
        self.assertEqual(signature, calculated)
        claims = json.loads(base64.urlsafe_b64decode(second + '=' * (-len(second) % 4)))
        self.assertEqual(claims['aud'], 'fantasy-effects')
        self.assertEqual(claims['iss'], 'fantasy-main')
        self.assertLessEqual(claims['exp'] - claims['iat'], 60)
        owner = claims['sub']
        route = request.url.path
        if route != '/api/session' and request.headers.get('x-effects-owner') != owner:
            return self.reply(403, {'error': 'Account changed.'})
        if request.method == 'POST' and (request.headers.get('origin') != ORIGIN or request.headers.get('x-effects-csrf') != self.csrf(owner)):
            return self.reply(403, {'error': 'Invalid effects CSRF'})
        if route == '/api/session':
            return self.reply(body={'user': {'id': owner, 'name': claims['name']}, 'csrf': self.csrf(owner),
                'api_base': '/effects', 'home_url': ORIGIN + '/', 'storage_key': owner})
        if route == '/api/list':
            return self.reply(body=list(self.library.get(owner, {})))
        if route == '/api/save':
            body = json.loads(request.content)
            self.library.setdefault(owner, {})[body['name'] + '.json'] = body['data']
            return self.reply(body={'ok': True, 'file': body['name'] + '.json'})
        if route == '/api/effect':
            value = self.library.get(owner, {}).get(request.url.params['f'])
            return self.reply(200 if value is not None else 404, value, headers={'etag': '"revision-test"'})
        if route == '/api/exports':
            identifier = str(uuid.uuid4()); self.exports[identifier] = owner
            return self.reply(202, {'job': {'id': identifier, 'status': 'done'}})
        if route.startswith('/api/exports/'):
            identifier = route.split('/')[3]
            if self.exports.get(identifier) != owner:
                return self.reply(404, {'error': 'Not found'})
            if route.endswith('/result'):
                return self.reply(content=PNG, headers={'content-type': 'image/png', 'x-effects-width': '1',
                    'x-effects-height': '1', 'x-effects-frames': '1', 'x-effects-fps': '30'})
            return self.reply(body={'job': {'id': identifier, 'status': 'done'}})
        return self.reply(404, {'error': 'Not found'})

    def login(self, username='admin', password='12345678'):
        response = self.client.post('/api/login', json={'username': username, 'password': password})
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()
        return session, {'Origin': ORIGIN, 'X-Community-CSRF': session['csrf'],
            'X-Effects-Owner': session['user']['id'], 'X-Effects-CSRF': self.csrf(session['user']['id'])}

    def test_anonymous_cannot_forge_internal_identity(self):
        self.assertEqual(self.client.get('/effects/', follow_redirects=False).status_code, 303)
        denied = self.client.get('/effects/api/list', headers={'Authorization': 'Bearer forged', 'X-Effects-Owner': str(uuid.uuid4())})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(self.calls, [])

    def test_identity_and_effects_owner_are_bound_to_current_portal_session(self):
        session, headers = self.login()
        response = self.client.get('/effects/api/session', headers={'Authorization': 'Bearer attacker', 'X-Forwarded-User': 'attacker'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user']['id'], session['user']['id'])
        self.assertEqual(response.json()['api_base'], '/effects')
        self.assertEqual(self.client.get('/effects/api/list', headers={'X-Effects-Owner': str(uuid.uuid4())}).status_code, 403)
        self.assertEqual(self.client.get('/effects/api/list', headers=headers).json(), [])

    def test_double_csrf_origin_and_revoked_login(self):
        session, headers = self.login()
        body = {'name': 'test', 'data': {'test': True}}
        before = len(self.calls)
        self.assertEqual(self.client.post('/effects/api/save', json=body, headers={'Origin': ORIGIN}).status_code, 403)
        self.assertEqual(len(self.calls), before)
        only_portal = {key: value for key, value in headers.items() if key != 'X-Effects-CSRF'}
        self.assertEqual(self.client.post('/effects/api/save', json=body, headers=only_portal).status_code, 403)
        self.assertEqual(self.client.post('/effects/api/save', json=body, headers={**headers, 'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.post('/effects/api/save', json=body, headers=headers).status_code, 200)
        self.client.post('/api/logout', json={}, headers=headers)
        self.assertEqual(self.client.get('/effects/api/list', headers=headers).status_code, 401)

    def test_library_and_export_download_are_account_scoped(self):
        first, headers = self.login()
        content = {'app': 'arcadia-effects', 'v': 2, 'doc': {'name': 'fixture'}}
        self.client.post('/effects/api/save', json={'name': 'fixture', 'data': content}, headers=headers)
        effect = self.client.get('/effects/api/effect', params={'f': 'fixture.json'}, headers=headers)
        self.assertEqual(effect.json(), content)
        self.assertEqual(effect.headers['etag'], '"revision-test"')
        job = self.client.post('/effects/api/exports', json={'file': 'fixture.json', 'format': 'frame'}, headers=headers).json()['job']['id']
        result = self.client.get(f'/effects/api/exports/{job}/result', headers=headers)
        self.assertEqual(result.content, PNG)
        self.assertEqual(result.headers['x-effects-width'], '1')
        self.app.state.auth.create_user('second', 'second-password')
        second, other = self.login('second', 'second-password')
        self.assertNotEqual(first['user']['id'], second['user']['id'])
        self.assertEqual(self.client.get('/effects/api/list', headers=other).json(), [])
        self.assertEqual(self.client.get('/effects/api/effect', params={'f': 'fixture.json'}, headers=other).status_code, 404)
        self.assertEqual(self.client.get(f'/effects/api/exports/{job}/result', headers=other).status_code, 404)
        self.assertEqual(self.client.get('/effects/api/list', headers=headers).status_code, 403)

    def test_dashboard_reports_health_and_proxy_does_not_follow_redirects(self):
        self.login()
        workspace = self.client.get('/api/workspace').json()
        self.assertEqual(workspace['modules']['effects']['status'], 'ready')
        self.assertEqual(workspace['modules']['effects']['href'], '/effects/')
        self.app.state.effects.transport = httpx.MockTransport(lambda request: httpx.Response(503))
        self.assertEqual(self.client.get('/api/workspace').json()['modules']['effects']['status'], 'unavailable')

    def test_connection_failure_is_an_explicit_unavailable_response(self):
        self.login()
        def unavailable(request):
            raise httpx.ConnectError('internal connection failed', request=request)
        self.app.state.effects.transport = httpx.MockTransport(unavailable)
        response = self.client.get('/effects/api/session')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('internal connection', response.text)


if __name__ == '__main__':
    unittest.main()

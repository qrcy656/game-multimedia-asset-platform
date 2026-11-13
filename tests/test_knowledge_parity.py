"""API-backed knowledge regression tests; every provider response is simulated."""
import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image


SECRET = 'isolated-knowledge-parity-secret-' * 2


def authorization(role='admin'):
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    now = int(time.time())
    claims = {'iss': 'fantasy-main', 'aud': 'knowledge-ques', 'sub': 'parity-user',
              'name': 'Parity user', 'role': role, 'iat': now, 'exp': now + 60}
    message = encode(b'{"alg":"HS256","typ":"JWT"}') + '.' + encode(json.dumps(claims).encode())
    token = message + '.' + encode(hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest())
    return {'Authorization': 'Bearer ' + token, 'Origin': 'http://testserver'}


class KnowledgeParity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='knowledge-parity-')
        self.root = Path(self.temp.name)
        # Load a separate module instance so discovery alongside gateway tests cannot
        # redirect their singleton store to this test's temporary directory.
        source = Path(__file__).resolve().parents[1] / 'knowledge/backend/app.py'
        spec = importlib.util.spec_from_file_location('knowledge.backend._parity_app', source)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        with patch.dict(os.environ, {'COMMUNITY_DATA_DIR': str(self.root), 'COMMUNITY_IDENTITY_SECRET': SECRET}):
            spec.loader.exec_module(self.module)
        self.client = TestClient(self.module.app)
        self.client.headers.update(authorization())
        self.calls = []

    def tearDown(self):
        self.client.close()
        sys.modules.pop(self.module.__name__, None)
        self.temp.cleanup()

    @staticmethod
    def completion(**value):
        return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}

    def image_file(self):
        path = self.module.store.vault / '素材库/地图.png'
        path.parent.mkdir(exist_ok=True)
        Image.new('RGB', (80, 60), 'blue').save(path)
        return path

    def test_visual_read_routes_actual_image_to_configured_vision_api_and_keeps_citations(self):
        path = self.image_file()

        async def invoke(module, role, payload):
            self.calls.append((module, role, payload))
            if role == 'llm':
                return self.completion(read_requests=[], image_requests=[{'path': path.relative_to(self.module.store.vault).as_posix(), 'crop': [0, 0, 500, 500]}],
                                       insufficient=False, paragraphs=[])
            self.assertEqual(role, 'vision')
            return self.completion(read_requests=[], image_requests=[], insufficient=False,
                                   paragraphs=[{'text': '图中区域为蓝色。', 'sources': ['S1']}])

        with patch.object(self.module, 'invoke', side_effect=invoke), patch.object(self.module, 'configured', side_effect=lambda module, role: {'name': 'Visual API'} if role in ('llm', 'vision') else None):
            response = self.client.post('/api/chat', json={'message': '查看地图左上角', 'allow_read': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row[1] for row in self.calls], ['llm', 'vision'])
        messages = self.calls[-1][2]['messages']
        images = [part for message in messages if isinstance(message['content'], list)
                  for part in message['content'] if part['type'] == 'image_url']
        self.assertEqual(len(images), 1)
        uri = images[0]['image_url']['url']
        self.assertTrue(uri.startswith('data:image/jpeg;base64,'))
        with Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1]))) as decoded:
            self.assertEqual(decoded.size, (40, 30))
        answer = response.json()
        self.assertIn('[S1]', answer['answer'])
        source = answer['sources'][0]
        self.assertEqual((source['path'], source['kind'], source['crop']), (path.relative_to(self.module.store.vault).as_posix(), 'image', [0, 0, 500, 500]))
        preview = self.client.get('/api/asset', params={'path': path.relative_to(self.module.store.vault).as_posix(), 'hash': source['hash']})
        self.assertEqual(preview.status_code, 200)
        Image.new('RGB', (80, 60), 'red').save(path)
        self.assertEqual(self.client.get('/api/asset', params={'path': path.relative_to(self.module.store.vault).as_posix(), 'hash': source['hash']}).status_code, 409)

    def test_missing_vision_binding_does_not_fabricate_image_evidence(self):
        path = self.image_file()

        async def invoke(module, role, payload):
            self.calls.append((role, payload))
            if len(self.calls) == 1:
                return self.completion(read_requests=[], image_requests=[{'path': path.relative_to(self.module.store.vault).as_posix(), 'crop': None}],
                                       insufficient=False, paragraphs=[])
            return self.completion(read_requests=[], image_requests=[], insufficient=True, paragraphs=[])

        with patch.object(self.module, 'invoke', side_effect=invoke), patch.object(self.module, 'configured', side_effect=lambda module, role: {'name': 'LLM'} if role == 'llm' else None):
            response = self.client.post('/api/chat', json={'message': '地图上有什么？', 'allow_read': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(all(role == 'llm' for role, _ in self.calls))
        self.assertEqual(response.json()['sources'], [])
        self.assertTrue(response.json()['warnings'])

    def test_changed_embedding_api_uses_an_independent_index(self):
        saved = self.client.put('/api/file', json={'path': 'world.md', 'content': '# 测试城\n测试城位于北方。', 'create': True})
        self.assertEqual(saved.status_code, 200, saved.text)

        async def invoke(module, role, payload, **kwargs):
            self.calls.append((role, payload))
            self.assertEqual(role, 'embedding')
            return {'data': [{'index': index, 'embedding': [1.0, 0.0, 0.0]} for index, _ in enumerate(payload['input'])]}

        with patch.object(self.module, 'invoke', side_effect=invoke), patch.object(self.module, 'resolve', return_value={'id': 'embedding-a', 'kind': 'embedding'}):
            response = self.client.post('/api/index', json={'paths': None})
            self.assertEqual(response.status_code, 200, response.text)
            first_index = self.module.retrieval_store()
            self.assertGreater(first_index.stats()['chunks'], 0)
        with patch.object(self.module, 'invoke', side_effect=invoke), patch.object(self.module, 'resolve', return_value={'id': 'embedding-b', 'kind': 'embedding'}):
            partial = self.client.post('/api/index', json={'paths': ['world.md']})
            self.assertEqual(partial.status_code, 200, partial.text)
            response = self.client.post('/api/index', json={'paths': None})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotEqual(first_index.db, self.module.retrieval_store().db)
            self.assertGreater(self.module.retrieval_store().stats()['chunks'], 0)
        self.assertGreater(first_index.stats()['chunks'], 0)

    def test_member_can_create_owned_source_and_index_requires_a_model(self):
        saved = self.client.put('/api/file', json={'path': 'shared.md', 'content': '# 共用设定', 'create': True})
        self.assertEqual(saved.status_code, 200, saved.text)
        headers = authorization('user')
        self.assertEqual(self.client.get('/api/file', params={'path': 'shared.md'}, headers=headers).status_code, 200)
        self.assertEqual(self.client.put('/api/file', json={'path': 'owned.md', 'content': 'x', 'create': True}, headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/index', json={'paths': None}, headers=headers).status_code, 503)


if __name__ == '__main__':
    unittest.main()

"""Animation model selection requires verified vision while keeping saved profiles."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from portal import create_app
from portal.registry import MODULE_ROLES, Registry, RegistryError


class AnimationVisionBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='animation-vision-binding-')
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.answer = 'BLUE'
        self.registry = Registry(Path(self.temp.name), transport=httpx.MockTransport(self.provider))

    def provider(self, request):
        self.calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': self.answer}}]})

    def profile(self, kind):
        return self.registry.save_profile({'name': 'Model whose name is not trusted', 'kind': kind,
            'protocol': 'openai', 'base_url': 'https://example.invalid/v1',
            'model': 'vision-model-name', 'api_key': 'saved-test-key', 'options': {}})

    def verify(self, item):
        return asyncio.run(self.registry.verify(item['id']))

    def test_text_only_model_cannot_bind_or_override_animation_vision(self):
        item = self.profile('llm')
        self.verify(item)
        self.assertEqual(self.registry.get_profile(item['id'])['capabilities'], ['llm'])
        for operation in (lambda: self.registry.bind('animation', 'vision', item['id']),
                          lambda: self.registry.resolve('animation', 'vision', item['id'])):
            with self.assertRaisesRegex(RegistryError, '识图模型'):
                operation()
        # The same validated text model remains usable for the text-only translators.
        for module in ('image', 'speech', 'music'):
            self.registry.bind(module, 'llm', item['id'])
            self.assertEqual(self.registry.resolve(module, 'llm')['id'], item['id'])

    def test_verified_picture_understanding_can_bind_animation(self):
        item = self.profile('vision')
        self.verify(item)
        content = self.calls[0]['messages'][0]['content']
        self.assertTrue(content[1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.registry.bind('animation', 'vision', item['id'])
        self.assertEqual(self.registry.resolve('animation', 'vision')['id'], item['id'])

    def test_model_label_alone_does_not_pass_picture_probe(self):
        self.answer = 'RED'
        item = self.profile('vision')
        with self.assertRaisesRegex(RegistryError, '图片识别'):
            self.verify(item)
        with self.assertRaisesRegex(RegistryError, '先通过模型类型检测'):
            self.registry.bind('animation', 'vision', item['id'])

    def test_edit_invalidates_existing_vision_binding(self):
        item = self.profile('vision')
        self.verify(item)
        self.registry.bind('animation', 'vision', item['id'])
        self.registry.save_profile({'model': 'replacement-model'}, item['id'])
        with self.assertRaisesRegex(RegistryError, '重新检测'):
            self.registry.resolve('animation', 'vision')

    def test_portal_advertises_vision_and_keeps_legacy_binding_and_key(self):
        item = self.profile('llm')
        self.verify(item)
        with self.registry.connection() as db:
            db.execute('INSERT INTO bindings VALUES(?,?,?)', ('animation', 'llm', item['id']))
        app = create_app(Path(self.temp.name))
        with TestClient(app) as client:
            login = client.post('/api/login', json={'username': 'admin', 'password': '12345678'})
            self.assertEqual(login.status_code, 200)
            response = client.get('/api/profiles')
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data['module_roles']['animation'], ['vision', 'image', 'video'])
            self.assertNotIn('llm', MODULE_ROLES['animation'])
            self.assertIn({'module': 'animation', 'role': 'llm', 'profile_id': item['id']}, data['bindings'])
            self.assertTrue(data['profiles'][0]['has_api_key'])
            self.assertNotIn('saved-test-key', response.text)
            headers = {'X-Community-CSRF': login.json()['csrf']}
            invalid = client.put('/api/bindings/animation/vision', headers=headers, json={'profile_id': item['id']})
            self.assertEqual(invalid.status_code, 400)
            self.assertIn('识图模型', invalid.json()['detail'])
        self.registry.bind('speech', 'llm', item['id'])
        self.assertEqual(self.registry.resolve('speech', 'llm')['api_key'], 'saved-test-key')


if __name__ == '__main__':
    unittest.main()

"""Isolated HTTP integration of the community portal and API-backed modules.

Run inside the built community image: python -m unittest discover -s tests -p test_modules.py -v
All accounts, registry entries, documents, images and audio use a temporary directory.
"""
import base64
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest
import uuid
import wave
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

ORIGIN = 'http://localhost:18082'


class CommunityModules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='community-integration-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        environment = patch.dict(os.environ, {'COMMUNITY_DATA_DIR': str(cls.root), 'COMMUNITY_ENABLE_ANIMATION': 'false',
                          'COMMUNITY_RUN_QUEUE': 'false', 'APP_ORIGIN': ORIGIN,
                          'COMMUNITY_IDENTITY_SECRET': 'community-modules-isolated-identity-secret-' * 2})
        environment.start()
        cls.addClassCleanup(environment.stop)
        # Other gateway suites may have imported the process-level application
        # while their own temporary directory existed. Build this suite's app
        # explicitly and give its mounted vault a separate module instance.
        source = Path(__file__).resolve().parents[1] / 'knowledge/backend/app.py'
        module_name = 'knowledge.backend._modules_fixture'
        spec = importlib.util.spec_from_file_location(module_name, source)
        knowledge = importlib.util.module_from_spec(spec)
        modules = patch.dict(sys.modules, {module_name: knowledge, 'knowledge.backend.app': knowledge})
        modules.start()
        cls.addClassCleanup(modules.stop)
        spec.loader.exec_module(knowledge)
        import app as application
        cls.app = application.create_app(cls.root, enable_animation=False, run_queue=False)
        cls.transcript = []
        image = io.BytesIO()
        Image.new('RGB', (32, 32), 'blue').save(image, format='PNG')
        cls.png = image.getvalue()
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b'\0\0' * 800)
        cls.wav = audio.getvalue()
        document = b'{"asset":{"version":"2.0"}}'
        document += b' ' * (-len(document) % 4)
        cls.glb = b'glTF' + struct.pack('<IIII', 2, 20 + len(document), len(document), 0x4E4F534A) + document
        cls.registry = cls.app.state.registry
        cls.registry.transport = httpx.MockTransport(cls.provider)
        cls.registry_patch = patch('providers._registry', return_value=cls.registry)
        cls.registry_patch.start()
        cls.addClassCleanup(cls.registry_patch.stop)
        cls.lifecycle = TestClient(cls.app, base_url=ORIGIN)
        cls.lifecycle.__enter__()
        cls.addClassCleanup(cls.lifecycle.__exit__, None, None, None)

    @classmethod
    def provider(cls, request):
        body = json.loads(request.content) if request.content else {}
        kind = request.url.host.split('.')[0]
        cls.transcript.append((request.method, str(request.url), body, dict(request.headers)))
        if kind == 'workflow':
            if request.url.path == '/system_stats':
                return httpx.Response(200, json={'system': {'os': 'test'}, 'devices': []})
            if request.url.path == '/object_info':
                return httpx.Response(200, json={'SaveImage': {'input': {'required': {}}}})
            if request.url.path == '/prompt':
                return httpx.Response(200, json={'prompt_id': 'mock-prompt'})
            if request.url.path == '/history/mock-prompt':
                return httpx.Response(200, json={'mock-prompt': {'status': {'completed': True},
                    'outputs': {'2': {'images': [{'filename': 'result.png', 'subfolder': '', 'type': 'output'}]}}}})
            if request.url.path == '/view':
                return httpx.Response(200, content=cls.png, headers={'Content-Type': 'image/png'})
        if request.url.path.endswith('/capabilities'):
            return httpx.Response(200, json={'model': kind + '-model', 'capabilities': [kind] + (['music_train'] if kind == 'music' else []),
                'api_version': 'fantasy-v1', 'input_schema': {'type': 'object'}, 'output_schema': {'type': 'object'}})
        if request.url.path.endswith('/validate'):
            return httpx.Response(200, json={'valid': body['kind'] == kind or kind == 'music' and body['kind'] == 'music_train', 'kind': body['kind'], 'model': kind + '-model'})
        if request.url.path.endswith('/generate'):
            if kind == 'music' and body.get('input', {}).get('task') == 'train':
                header = json.dumps({'weight': {'dtype': 'F32', 'shape': [1], 'data_offsets': [0, 4]}}).encode()
                weights = struct.pack('<Q', len(header)) + header + b'\0' * 4
                return httpx.Response(200, json={'artifacts': [{'data_base64': base64.b64encode(weights).decode()}]})
            if kind == 'llm':
                messages = body['messages']
                last = messages[-1]['content']
                if isinstance(last, str) and '当前问题：' in last:
                    content = {'read_requests': [{'path': 'qa-test.md', 'start_line': 1, 'end_line': 5}], 'image_requests': [], 'paragraphs': [], 'insufficient': False}
                elif isinstance(last, str) and '读取工具返回：' in last:
                    content = {'read_requests': [], 'image_requests': [], 'paragraphs': [{'text': '测试城位于北方。', 'sources': ['S1']}], 'insufficient': False}
                else:
                    content = {'description': 'A calm English creative instruction.'}
                return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content, ensure_ascii=False)}}]})
            raw = cls.png if kind == 'image' else cls.glb if kind == 'model3d' else cls.wav
            return httpx.Response(200, json={'artifacts': [{'data_base64': base64.b64encode(raw).decode()}]})
        return httpx.Response(404, json={'detail': 'Unexpected mock endpoint'})

    def setUp(self):
        self.client, self.csrf, self.user = self.login()

    def tearDown(self):
        self.client.close()

    def login(self, username='admin', password='12345678'):
        client = TestClient(self.app, base_url=ORIGIN)
        result = client.post('/api/login', json={'username': username, 'password': password}, headers={'Origin': ORIGIN})
        self.assertEqual(result.status_code, 200, result.text)
        return client, result.json()['csrf'], result.json()['user']

    def headers(self, module=None, client=None, csrf=None):
        client = client or self.client
        result = {'Origin': ORIGIN, 'X-Community-CSRF': csrf or self.csrf}
        if module in {'image', 'speech', 'music', 'model3d', 'narrative'}:
            response = client.get('/' + module + '/api/config')
            self.assertEqual(response.status_code, 200, response.text)
            result['X-CSRF-Token'] = response.json()['csrf']
        return result

    def new_user(self):
        username = 'tester-' + uuid.uuid4().hex[:8]
        response = self.client.post('/api/users', json={'username': username, 'password': 'test-password', 'name': 'Integration user', 'admin': False}, headers=self.headers())
        self.assertEqual(response.status_code, 201, response.text)
        return self.login(username, 'test-password')

    def profile(self, kind, module=None, role=None):
        payload = {'name': 'Integration ' + kind, 'kind': kind, 'protocol': 'rest', 'model': kind + '-model', 'scope': 'global',
                   'base_url': 'https://' + kind + '.example', 'api_key': 'integration-only-secret'}
        if kind == 'workflow':
            payload.update(protocol='comfyui', model='')
        created = self.client.post('/api/profiles', json=payload, headers=self.headers())
        self.assertEqual(created.status_code, 201, created.text)
        identifier = created.json()['id']
        verified = self.client.post('/api/profiles/' + identifier + '/verify', json={}, headers=self.headers())
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(verified.json()['verified'])
        if module:
            bound = self.client.put(f'/api/bindings/{module}/{role or kind}', json={'profile_id': identifier}, headers=self.headers())
            self.assertEqual(bound.status_code, 200, bound.text)
        return identifier

    def execute(self, identifier):
        native_client = httpx.Client
        with patch('media.remote.httpx.Client', side_effect=lambda **kwargs: native_client(transport=httpx.MockTransport(self.provider), **kwargs)):
            self.app.state.queue.execute(self.app.state.queue.raw(identifier))

    def test_00_clean_initial_state_and_default_login(self):
        self.assertTrue(self.user['admin'])
        self.assertEqual(self.user['username'], 'admin')
        self.assertEqual(self.client.get('/api/profiles').json()['profiles'], [])
        self.assertEqual(self.client.get('/knowledge/api/files').json()['files'], [])
        self.assertEqual(self.client.get('/knowledge/api/assets').json()['assets'], [])
        self.assertEqual(self.client.get('/narrative/api/projects').json()['projects'], [])
        for module in ('image', 'speech', 'music', 'model3d'):
            self.assertEqual(self.client.get(f'/{module}/api/jobs').json()['jobs'], [])
        self.assertEqual(self.client.get('/image/api/profiles').json()['items'], [])
        self.assertFalse(any(self.root.rglob('世界地图.jpg')))

    def test_authenticated_navigation_and_static_urls(self):
        for module in ('knowledge', 'narrative', 'image', 'speech', 'music', 'model3d'):
            page = self.client.get('/' + module + '/')
            self.assertEqual(page.status_code, 200, page.text[:100])
            self.assertIn('/community.js', page.text)
            for href in re.findall(r'(?:src|href)="([^\"]+\.(?:js|css)(?:\?[^\"]*)?)"', page.text):
                target = str(httpx.URL(ORIGIN + '/' + module + '/').join(href))
                self.assertEqual(self.client.get(target).status_code, 200, target)
        session = self.client.get('/knowledge/api/session', headers={'Authorization': 'Bearer forged', 'X-API-Key': 'forged'}).json()
        self.assertTrue(session['authenticated'])
        self.assertEqual(session['user']['id'], self.user['id'])
        self.assertEqual(self.client.get('/image/api/config').json()['user']['id'], self.user['id'])

    def test_anonymous_and_forged_identity_cannot_enter_modules(self):
        client = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)
        try:
            self.assertEqual(client.get('/knowledge/').status_code, 303)
            for module, endpoint in [('knowledge', 'files'), ('image', 'jobs'), ('narrative', 'projects')]:
                result = client.get(f'/{module}/api/{endpoint}', headers={'Authorization': 'Bearer forged', 'X-API-Key': 'fake'})
                self.assertEqual(result.status_code, 401)
        finally:
            client.close()

    def test_csrf_and_origin_are_enforced_at_both_boundaries(self):
        self.assertEqual(self.client.post('/api/users', json={}).status_code, 403)
        self.assertEqual(self.client.post('/narrative/api/projects', json={'title': 'Blocked', 'template': 'blank'}, headers={'Origin': ORIGIN}).status_code, 403)
        headers = self.headers('narrative'); headers['Origin'] = 'https://foreign.example'
        self.assertEqual(self.client.post('/narrative/api/projects', json={'title': 'Blocked', 'template': 'blank'}, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/narrative/api/projects', json={'title': 'Blocked', 'template': 'blank'}, headers=self.headers()).status_code, 403)

    def test_comfy_workflow_roundtrip_and_unknown_node_rejection(self):
        model = self.profile('workflow', 'image')
        graph = {'1': {'class_type': 'EmptyImage', 'inputs': {'width': 256, 'height': 256, 'batch_size': 1, 'color': 255}},
                 '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': 'test'}}}
        params = {'workflow': json.dumps(graph), 'bindings': '{}'}
        headers = self.headers('image')
        created = self.client.post('/image/api/jobs', data={'model_id': model, 'params': json.dumps(params)}, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        identifier = created.json()['id']
        self.execute(identifier)
        result = self.client.get('/image/api/jobs/' + identifier).json()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.client.get(result['artifacts'][0]['url']).content, self.png)
        submitted = [row for row in self.transcript if row[1] == 'https://workflow.example/prompt'][-1]
        self.assertEqual(submitted[2]['prompt']['2']['inputs']['filename_prefix'], 'fantasy-' + identifier + '-2')
        graph['1']['class_type'] = 'ExecuteArbitraryPython'
        rejected = self.client.post('/image/api/compile', data={'model_id': model, 'params': json.dumps({'workflow': json.dumps(graph), 'bindings': '{}'})}, headers=headers)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(self.client.delete('/image/api/jobs/' + identifier, headers=headers).status_code, 200)

    def test_knowledge_crud_conflicts_and_reader_permissions(self):
        headers = self.headers('knowledge')
        created = self.client.put('/knowledge/api/file', json={'path': 'integration.md', 'content': '# 测试\n初稿', 'create': True}, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)
        digest = created.json()['hash']
        updated = self.client.put('/knowledge/api/file', json={'path': 'integration.md', 'content': '# 测试\n新稿', 'expected_hash': digest}, headers=headers)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.put('/knowledge/api/file', json={'path': 'integration.md', 'content': '旧稿覆盖', 'expected_hash': digest}, headers=headers).status_code, 409)
        other, csrf, _ = self.new_user()
        try:
            self.assertEqual(other.get('/knowledge/api/file', params={'path': 'integration.md'}).status_code, 200)
            self.assertEqual(other.put('/knowledge/api/file', json={'path': 'owned.md', 'content': 'x', 'create': True}, headers=self.headers(client=other, csrf=csrf)).status_code, 200)
            self.assertEqual(other.put('/knowledge/api/file', json={'path': 'integration.md', 'content': 'forbidden', 'expected_hash': updated.json()['hash']}, headers=self.headers(client=other, csrf=csrf)).status_code, 403)
            self.assertEqual(other.get('/api/users').status_code, 403)
        finally:
            other.close()
        deleted = self.client.request('DELETE', '/knowledge/api/file', json={'path': 'integration.md', 'expected_hash': updated.json()['hash']}, headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/knowledge/api/file', params={'path': 'integration.md'}).status_code, 404)

    def test_knowledge_raw_document_qa_uses_configured_llm_and_citations(self):
        self.profile('llm', 'knowledge')
        headers = self.headers('knowledge')
        saved = self.client.put('/knowledge/api/file', json={'path': 'qa-test.md', 'content': '# 测试城\n测试城位于北方。', 'create': True}, headers=headers)
        self.assertEqual(saved.status_code, 200)
        answer = self.client.post('/knowledge/api/chat', json={'message': '测试城在哪里？', 'allow_read': True}, headers=headers)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()['sources'][0]['path'], 'qa-test.md')
        self.assertIn('[S1]', answer.json()['answer'])
        self.client.request('DELETE', '/knowledge/api/file', json={'path': 'qa-test.md', 'expected_hash': saved.json()['hash']}, headers=headers)

    def test_narrative_crud_version_conflict_and_owner_isolation(self):
        headers = self.headers('narrative')
        created = self.client.post('/narrative/api/projects', json={'title': 'Integration story', 'template': 'blank'}, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        value = created.json(); identifier = value['id']; document = value['document']
        self.assertEqual(document['nodes'], [])
        document['title'] = 'Revised story'
        saved = self.client.put('/narrative/api/projects/' + identifier, json={'version': value['version'], 'document': document}, headers=headers)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.put('/narrative/api/projects/' + identifier, json={'version': value['version'], 'document': document}, headers=headers).status_code, 409)
        other, csrf, _ = self.new_user()
        try:
            self.assertEqual(other.get('/narrative/api/projects/' + identifier).status_code, 200)
            self.assertEqual(other.delete('/narrative/api/projects/' + identifier + '?version=2', headers=self.headers('narrative', other, csrf)).status_code, 403)
        finally:
            other.close()
        self.assertEqual(self.client.delete('/narrative/api/projects/' + identifier + '?version=2', headers=headers).status_code, 200)

    def test_model_profiles_are_typed_and_secrets_not_returned(self):
        identifier = self.profile('embedding', 'knowledge')
        mismatch = self.client.put('/api/bindings/knowledge/llm', json={'profile_id': identifier}, headers=self.headers())
        self.assertEqual(mismatch.status_code, 400)
        self.assertNotIn('integration-only-secret', self.client.get('/api/profiles').text)
        self.assertNotIn(b'integration-only-secret', (self.root / 'model_registry.sqlite3').read_bytes())
        other, csrf, _ = self.new_user()
        try:
            self.assertEqual(other.post('/api/profiles', json={'scope': 'global'}, headers=self.headers(client=other, csrf=csrf)).status_code, 403)
        finally:
            other.close()

    def test_wrong_model_declaration_cannot_pass_verification(self):
        created = self.client.post('/api/profiles', json={'name': 'Embedding incorrectly labelled LLM',
            'kind': 'llm', 'protocol': 'rest', 'model': 'embedding-model',
            'base_url': 'https://embedding.example', 'api_key': 'integration-only-secret'}, headers=self.headers())
        self.assertEqual(created.status_code, 201)
        identifier = created.json()['id']
        verified = self.client.post('/api/profiles/' + identifier + '/verify', json={}, headers=self.headers())
        self.assertEqual(verified.status_code, 400)
        selected = self.client.put('/api/bindings/image/llm', json={'profile_id': identifier}, headers=self.headers())
        self.assertEqual(selected.status_code, 400)

    def test_admin_can_read_shared_jobs_without_scope_gate(self):
        model = self.profile('image', 'image')
        other, csrf, _ = self.new_user()
        try:
            headers = self.headers('image', other, csrf)
            created = other.post('/image/api/jobs', data={'model_id': model, 'params': json.dumps({'prompt': 'Private job'})}, headers=headers)
            self.assertEqual(created.status_code, 201, created.text)
            identifier = created.json()['id']
            self.assertEqual(self.client.get('/image/api/jobs/' + identifier).status_code, 200)
            self.assertEqual(self.client.get('/image/api/jobs/' + identifier + '?scope=all').status_code, 200)
            self.assertEqual(other.post('/image/api/jobs/' + identifier + '/cancel', json={}, headers=headers).status_code, 200)
            self.assertEqual(other.delete('/image/api/jobs/' + identifier, headers=headers).status_code, 200)
        finally:
            other.close()

    def test_media_translation_keeps_spoken_text_and_lyrics(self):
        for module, params in [('image', {'prompt': '平静的森林'}), ('speech', {'text': '你好，旅人。', 'instruct': '温柔地说'}), ('music', {'prompt': '安静的背景音乐', 'lyrics': '月光照在河面'})]:
            model = self.profile(module, module)
            self.profile('llm', module)
            headers = self.headers(module)
            before = len(self.transcript)
            direct = self.client.post(f'/{module}/api/compile', data={'model_id': model, 'params': json.dumps(params), 'prompt_mode': 'direct'}, headers=headers)
            self.assertEqual(direct.status_code, 200, direct.text)
            self.assertIsNone(direct.json()['translation'])
            self.assertEqual(len(self.transcript), before)
            assisted = self.client.post(f'/{module}/api/compile', data={'model_id': model, 'params': json.dumps(params), 'prompt_mode': 'assisted'}, headers=headers)
            self.assertEqual(assisted.status_code, 200, assisted.text)
            field = 'instruct' if module == 'speech' else 'prompt'
            self.assertEqual(assisted.json()['params'][field], 'A calm English creative instruction.')
            for untouched in ('text', 'lyrics'):
                if untouched in params:
                    self.assertEqual(assisted.json()['params'][untouched], params[untouched])

    def test_media_jobs_store_valid_artifacts_and_enforce_owner_scope(self):
        for module, params, expected in [('image', {'prompt': 'Blue square'}, self.png), ('speech', {'text': 'Hello'}, self.wav), ('music', {'prompt': 'Quiet music'}, self.wav)]:
            model = self.profile(module, module)
            headers = self.headers(module)
            created = self.client.post(f'/{module}/api/jobs', data={'model_id': model, 'params': json.dumps(params)}, headers=headers)
            self.assertEqual(created.status_code, 201, created.text)
            identifier = created.json()['id']
            self.execute(identifier)
            result = self.client.get(f'/{module}/api/jobs/{identifier}').json()
            self.assertEqual(result['status'], 'succeeded', result)
            url = result['artifacts'][0]['url']
            self.assertTrue(url.startswith(f'/{module}/api/jobs/'))
            self.assertEqual(self.client.get(url).content, expected)
            self.assertNotIn('integration-only-secret', json.dumps(result))
            other, csrf, _ = self.new_user()
            try:
                self.assertEqual(other.get(f'/{module}/api/jobs/{identifier}').status_code, 200)
                self.assertEqual(other.get(url).status_code, 200)
                self.assertEqual(other.get(f'/{module}/api/jobs/{identifier}?scope=all').status_code, 200)
                self.assertEqual(other.delete(f'/{module}/api/jobs/{identifier}', headers=self.headers(module, other, csrf)).status_code, 403)
            finally:
                other.close()
            self.assertEqual(self.client.delete(f'/{module}/api/jobs/{identifier}', headers=headers).status_code, 200)
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_model3d_reference_is_transferred_and_glb_is_archived(self):
        model = self.profile('model3d', 'model3d')
        headers = self.headers('model3d')
        created = self.client.post('/model3d/api/jobs', data={'model_id': model, 'params': json.dumps({'prompt': 'Small test object'})},
                                   files={'file': ('reference.png', self.png, 'image/png')}, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        identifier = created.json()['id']
        self.execute(identifier)
        result = self.client.get('/model3d/api/jobs/' + identifier).json()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.client.get(result['artifacts'][0]['url']).content, self.glb)
        submitted = [row for row in self.transcript if row[1] == 'https://model3d.example/generate'][-1]
        image = submitted[2]['input']['image']
        self.assertEqual(image['mime_type'], 'image/png')
        self.assertEqual(base64.b64decode(image['data_base64']), self.png)
        self.assertNotIn('/tmp/', json.dumps(submitted[2]))
        self.assertEqual(self.client.delete('/model3d/api/jobs/' + identifier, headers=headers).status_code, 200)

    def test_music_training_permissions_upload_download_and_full_deletion(self):
        import zipfile
        model = self.profile('music', 'music')
        headers = self.headers('music')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('sample.wav', self.wav)
        payload = {'model_id': model, 'params': json.dumps({'task': 'train', 'prompt': 'My style', 'duration': 30})}
        other, csrf, _ = self.new_user()
        try:
            member_task = other.post('/music/api/jobs', data=payload,
                files={'file': ('dataset.zip', archive.getvalue(), 'application/zip')}, headers=self.headers('music', other, csrf))
            self.assertEqual(member_task.status_code, 201, member_task.text)
            other.post('/music/api/jobs/' + member_task.json()['id'] + '/cancel', headers=self.headers('music', other, csrf))
            other.delete('/music/api/jobs/' + member_task.json()['id'], headers=self.headers('music', other, csrf))
        finally:
            other.close()
        created = self.client.post('/music/api/jobs', data=payload,
            files={'file': ('dataset.zip', archive.getvalue(), 'application/zip')}, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        identifier = created.json()['id']
        self.execute(identifier)
        job = self.client.get('/music/api/jobs/' + identifier).json()
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(job['artifacts'][0]['name'].endswith('.safetensors'))
        url = job['artifacts'][0]['url']
        self.assertEqual(self.client.get(url).status_code, 200)
        folder = self.app.state.queue.folder(self.app.state.queue.raw(identifier))
        self.assertTrue((folder / 'reference.zip').is_file())
        self.assertEqual(self.client.delete('/music/api/jobs/' + identifier, headers=headers).status_code, 200)
        self.assertFalse(folder.exists())
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_image_profile_import_select_append_label_and_delete(self):
        headers = self.headers('image')
        created = self.client.post('/image/api/profiles', data={'name': '角色集合', 'labels': '["SE"]'},
            files={'files': ('first.png', self.png, 'image/png')}, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        profile = created.json()
        identifier = profile['id']
        first = profile['images'][0]
        self.assertEqual(self.client.get(first['url']).content, self.png)
        added = self.client.post(f'/image/api/profiles/{identifier}/images', data={'labels': '["N"]'},
            files={'files': ('second.png', self.png, 'image/png')}, headers=headers)
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(len(added.json()['images']), 2)
        labelled = self.client.patch(f'/image/api/profiles/{identifier}/images/{first["id"]}', json={'label': '东南'}, headers=headers)
        self.assertEqual(labelled.status_code, 200, labelled.text)
        model = self.profile('image', 'image')
        job = self.client.post('/image/api/jobs', data={'model_id': model, 'params': '{"prompt":"参考角色"}',
            'profile_id': identifier, 'image_id': first['id']}, headers=headers)
        self.assertEqual(job.status_code, 201, job.text)
        job_id = job.json()['id']
        self.execute(job_id)
        self.assertEqual(self.client.get('/image/api/jobs/' + job_id).json()['status'], 'succeeded')
        self.assertEqual(self.client.delete(f'/image/api/profiles/{identifier}/images/{first["id"]}', headers=headers).status_code, 200)
        self.assertEqual(self.client.get(first['url']).status_code, 404)
        self.assertEqual(self.client.delete(f'/image/api/profiles/{identifier}', headers=headers).status_code, 200)
        self.assertEqual(self.client.delete('/image/api/jobs/' + job_id, headers=headers).status_code, 200)

    def test_z_logout_revokes_module_access(self):
        result = self.client.post('/api/logout', json={}, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get('/image/api/jobs').status_code, 401)
        self.assertEqual(self.client.get('/api/session').status_code, 401)


if __name__ == '__main__':
    unittest.main()

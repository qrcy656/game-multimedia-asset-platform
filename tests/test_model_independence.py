"""Feature boundaries with temporary data and controlled model responses only."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from PIL import Image

from providers import feature_capabilities
from portal.registry import RegistryError
from media.catalog import DRAFT_MODEL_ID, prompt_draft
from media.queue import ApiQueue
from animation.clients import PermanentError
from animation.config import Settings
from animation.models import AnimationTask
from animation.service import Service
import test_knowledge_parity as knowledge_helpers


class ConfiguredRoles:
    def __init__(self, *roles):
        self.roles = set(roles)

    def resolve(self, module, role):
        if role not in self.roles:
            raise RegistryError('Not configured: ' + role)
        return {'id': role, 'kind': role, 'name': role, 'model': role,
                'protocol': 'rest', 'base_url': 'https://model.invalid', 'options': {}}


class FeatureBoundaries(unittest.TestCase):
    def test_no_models_keeps_management_available_in_every_module(self):
        for module in ('knowledge', 'animation', 'image', 'speech', 'music', 'model3d', 'narrative'):
            result = feature_capabilities(module, ConfiguredRoles())
            self.assertTrue(result['local_available'])
            self.assertTrue(result['features']['management']['available'])
            self.assertTrue(all(not value for value in result['roles'].values()))

    def test_knowledge_model_roles_enable_only_their_features(self):
        llm = feature_capabilities('knowledge', ConfiguredRoles('llm'))['features']
        self.assertTrue(llm['raw_qa']['available'])
        self.assertFalse(llm['vector_index']['available'])
        self.assertFalse(llm['image_qa']['available'])
        embedding = feature_capabilities('knowledge', ConfiguredRoles('embedding'))['features']
        self.assertTrue(embedding['vector_index']['available'])
        self.assertTrue(embedding['vector_search']['available'])
        self.assertFalse(embedding['raw_qa']['available'])
        rag = feature_capabilities('knowledge', ConfiguredRoles('embedding', 'llm'))['features']
        self.assertTrue(rag['rag_qa']['available'])
        self.assertFalse(rag['rerank']['available'])
        vision = feature_capabilities('knowledge', ConfiguredRoles('vision'))['features']
        self.assertTrue(vision['raw_qa']['available'])
        self.assertTrue(vision['image_qa']['available'])

    def test_direct_generators_workflow_and_prompt_assist_are_independent(self):
        image = feature_capabilities('image', ConfiguredRoles('workflow'))['features']
        self.assertTrue(image['generation']['available'])
        self.assertTrue(image['workflow']['available'])
        self.assertFalse(image['prompt_assistant']['available'])
        for module in ('speech', 'music'):
            features = feature_capabilities(module, ConfiguredRoles(module))['features']
            self.assertTrue(features['generation']['available'])
            self.assertFalse(features['prompt_assistant']['available'])
        animation = feature_capabilities('animation', ConfiguredRoles('video', 'image'))['features']
        self.assertTrue(animation['direct_generation']['available'])
        self.assertFalse(animation['assisted_generation']['available'])
        self.assertTrue(animation['prompt_preview']['available'])

    def test_verified_media_choice_does_not_require_a_default_binding(self):
        source = ConfiguredRoles()
        source.list_profiles = lambda: [{'kind': 'workflow', 'verified': True}, {'kind': 'speech', 'verified': False}]
        image = feature_capabilities('image', source)
        self.assertFalse(image['roles']['workflow'])
        self.assertTrue(image['features']['generation']['available'])
        self.assertTrue(image['features']['workflow']['available'])
        self.assertFalse(feature_capabilities('speech', source)['features']['generation']['available'])


class KnowledgeIndependence(unittest.TestCase):
    setUp = knowledge_helpers.KnowledgeParity.setUp
    tearDown = knowledge_helpers.KnowledgeParity.tearDown
    completion = staticmethod(knowledge_helpers.KnowledgeParity.completion)

    def save_document(self):
        response = self.client.put('/api/file', json={
            'path': 'world.md', 'content': '# 测试城\n测试城位于北方。', 'create': True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_no_models_keeps_markdown_crud_and_returns_targeted_ai_errors(self):
        with patch.object(self.module, 'configured', return_value=None), \
             patch.object(self.module, 'resolve', side_effect=RegistryError('missing model')), \
             patch.object(self.module, 'invoke') as invoke:
            saved = self.save_document()
            self.assertEqual(self.client.get('/api/file', params={'path': 'world.md'}).status_code, 200)
            updated = self.client.put('/api/file', json={'path': 'world.md', 'content': '# 新设定', 'expected_hash': saved['hash']})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(self.client.post('/api/chat', json={'message': '城在哪里？'}).status_code, 503)
            self.assertEqual(self.client.post('/api/index', json={'paths': None}).status_code, 503)
            deleted = self.client.request('DELETE', '/api/file', json={'path': 'world.md', 'expected_hash': updated.json()['hash']})
            self.assertEqual(deleted.status_code, 200, deleted.text)
            invoke.assert_not_called()

    def test_llm_only_can_read_raw_documents_with_old_vectors_present(self):
        self.save_document()
        async def embed(texts):
            return [[1.0, 0.0] for _ in texts]
        asyncio.run(self.module.store.index(['world.md'], embed))
        self.assertGreater(self.module.store.stats()['chunks'], 0)
        calls = []
        async def invoke(module, role, payload):
            calls.append(role)
            if len(calls) == 1:
                return self.completion(read_requests=[{'path': 'world.md', 'start_line': 1, 'end_line': 3}], image_requests=[], insufficient=False, paragraphs=[])
            return self.completion(read_requests=[], image_requests=[], insufficient=False,
                                   paragraphs=[{'text': '测试城位于北方。', 'sources': ['S1']}])
        with patch.object(self.module, 'configured', side_effect=lambda module, role: {'name': 'LLM'} if role == 'llm' else None), \
             patch.object(self.module, 'invoke', side_effect=invoke):
            response = self.client.post('/api/chat', json={'message': '测试城在哪里？'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, ['llm', 'llm'])
        self.assertEqual(response.json()['sources'][0]['path'], 'world.md')
        self.assertEqual(response.json()['read_paths'], ['world.md'])

    def test_embedding_without_reranker_can_index_search_and_answer(self):
        self.save_document()
        calls = []
        async def invoke(module, role, payload, **kwargs):
            calls.append(role)
            if role == 'embedding':
                return {'data': [{'index': i, 'embedding': [1.0, 0.0]} for i, _ in enumerate(payload['input'])]}
            self.assertEqual(role, 'llm')
            return self.completion(read_requests=[], image_requests=[], insufficient=False,
                                   paragraphs=[{'text': '测试城位于北方。', 'sources': ['S1']}])
        registry = ConfiguredRoles('llm', 'embedding')
        def configured(module, role):
            return registry.resolve(module, role) if role in registry.roles else None
        with patch.object(self.module, 'configured', side_effect=configured), \
             patch.object(self.module, 'resolve', side_effect=registry.resolve), \
             patch.object(self.module, 'invoke', side_effect=invoke):
            indexed = self.client.post('/api/index', json={'paths': None})
            self.assertEqual(indexed.status_code, 200, indexed.text)
            found = self.client.post('/api/search', json={'query': '测试城'})
            self.assertEqual(found.status_code, 200, found.text)
            self.assertTrue(found.json()['sources'])
            self.assertEqual(found.json()['warnings'], [])
            answer = self.client.post('/api/chat', json={'message': '测试城在哪里？', 'allow_read': False})
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertIn('[S1]', answer.json()['answer'])
            registry.roles.remove('llm')
            self.assertTrue(self.client.post('/api/search', json={'query': '测试城'}).json()['sources'])
            self.assertEqual(self.client.post('/api/chat', json={'message': '测试城在哪里？'}).status_code, 503)
        self.assertNotIn('rerank', calls)

    def test_vision_only_can_start_text_turn_and_request_actual_image(self):
        path = knowledge_helpers.KnowledgeParity.image_file(self)
        calls = []
        async def invoke(module, role, payload):
            self.assertEqual(role, 'vision')
            calls.append(payload)
            if len(calls) == 1:
                return self.completion(read_requests=[], image_requests=[{'path': '素材库/地图.png', 'crop': None}], insufficient=False, paragraphs=[])
            return self.completion(read_requests=[], image_requests=[], insufficient=False,
                                   paragraphs=[{'text': '图片是蓝色的。', 'sources': ['S1']}])
        with patch.object(self.module, 'configured', side_effect=lambda module, role: {'name': 'Vision'} if role == 'vision' else None), \
             patch.object(self.module, 'invoke', side_effect=invoke):
            response = self.client.post('/api/chat', json={'message': '查看地图'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(response.json()['sources'][0]['kind'], 'image')


class PreviewIndependence(unittest.TestCase):
    def test_media_llm_preview_without_generation_api_does_not_admit_a_job(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = ApiQueue(directory)
            for module, params in [('image', {'prompt': '冰雪'}), ('speech', {'text': '你好', 'instruct': '平静'}), ('music', {'prompt': '安静的森林'})]:
                body = {'module': module, 'owner': {'id': 'author'}, 'model_id': DRAFT_MODEL_ID,
                        'params': params, 'prompt_mode': 'assisted'}
                with patch('media.catalog.model_catalog', return_value=[]), \
                     patch('media.queue.resolve', return_value={'id': 'llm', 'model': 'llm', 'protocol': 'rest'}), \
                     patch('media.queue.invoke_sync', return_value={'choices': [{'message': {'content': '{"description":"Calm creative instruction."}'}}]}) as invoke:
                    result = queue.compile(body)
                    self.assertIsNotNone(result['translation'])
                    self.assertTrue(all(call.args[1] == 'llm' for call in invoke.call_args_list))
                    with self.assertRaisesRegex(ValueError, '模型配置'):
                        queue.create(body)
            with queue.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_animation_prompt_preview_without_video_api_keeps_generation_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'reference.png'
            Image.new('RGB', (32, 32), 'blue').save(image)
            clients = Mock()
            clients.video_profile.side_effect = PermanentError('Video API is not configured')
            service = Service(Settings(data_root=root), Mock(), clients)
            snapshot = {'character': {'profile': {}, 'revision': 1}, 'reference': {'path': str(image)}, 'motion': None}
            direct = AnimationTask(character_id='test', prompt_mode='direct', video_prompt='walk', preview=True).model_dump()
            result = service.compile_snapshot(direct, snapshot, root / 'analysis', for_generation=False)
            self.assertIn('walk', result['prompt'])
            self.assertIsNone(result['video_provider'])
            clients.translate_animation.assert_not_called()
            clients.require_visual_intent_ready.assert_not_called()
            with self.assertRaises(PermanentError):
                service.compile_snapshot(direct, snapshot, root / 'analysis')

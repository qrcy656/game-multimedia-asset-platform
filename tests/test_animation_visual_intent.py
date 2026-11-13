"""Reference-grounded assisted motion contracts, without real model inference."""
import base64
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
COMMUNITY = (ROOT / 'providers.py').is_file()
sys.path.insert(0, str(ROOT if COMMUNITY else ROOT / 'production'))

import httpx
from PIL import Image
from animation.clients import ModelClients, PermanentError, TransientError, GenerationServicesUnavailable
from animation.compiler import compile_prompt
from animation.config import Settings
from animation.models import AnimationTask
from animation.service import Service
from animation.visual_intent import ANIMATION_VISUAL_INTENT_SYSTEM

VISION = {'id': 'vision-profile', 'name': 'Visual motion planner', 'model': 'vision-model',
          'kind': 'vision', 'protocol': 'openai', 'base_url': 'https://vision.invalid/v1',
          'api_key': 'test-only', 'capabilities': ['vision', 'llm'], 'verified': True}
VIDEO = {**VISION, 'id': 'video-profile', 'kind': 'video', 'protocol': 'videos', 'model': 'video-model'}
DESCRIPTION = ('From the pictured side-facing stance, shift weight onto the supporting leg, lift the other foot, '
               'bend its visible knee and swing it forward. Place that foot, transfer support, then repeat with '
               'the opposite leg. Keep the root stationary and return to the opening gait phase in four seconds.')


class VisualAnimationIntentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image_dir = self.root / 'characters/test/references'
        self.image_dir.mkdir(parents=True)
        self.primary = self.image_dir / 'primary.png'
        self.extra = self.image_dir / 'appearance.png'
        Image.new('RGB', (24, 32), 'blue').save(self.primary)
        Image.new('RGB', (24, 32), 'green').save(self.extra)
        self.settings = Settings(data_root=self.root)
        if not COMMUNITY:
            self.settings = replace(self.settings, managed_models=False)
        self.clients = ModelClients(self.settings)
        self.task = AnimationTask(character_id='test', prompt_mode='assisted',
                                  supplementary_description='让这个角色行走', duration=4, preview=True).model_dump()

    def test_assisted_request_contains_real_primary_and_supplementary_image_bytes(self):
        if COMMUNITY:
            with patch('animation.clients.providers.resolve', return_value=VISION) as resolve, \
                 patch('animation.clients.providers.invoke_sync', return_value={'choices': [{'message': {'content': json.dumps({'description': DESCRIPTION})}}]}) as invoke:
                result = self.clients.translate_animation(self.task, {}, self.primary, additional_references=[{'path': self.extra}])
            self.assertTrue(all(call.args[:2] == ('animation', 'vision') for call in resolve.call_args_list))
            self.assertEqual(invoke.call_args.args[:2], ('animation', 'vision'))
            messages = invoke.call_args.args[2]['messages']
            content = messages[-1]['content']
            sent = [base64.b64decode(item['image_url']['url'].split(',', 1)[1]) for item in content if item['type'] == 'image_url']
            data = json.loads(content[0]['text'])
        else:
            response = httpx.Response(200, request=httpx.Request('POST', 'http://ollama.invalid/api/chat'),
                                      json={'message': {'content': json.dumps({'description': DESCRIPTION})}})
            with patch('animation.clients.httpx.post', return_value=response) as post:
                result = self.clients.translate_animation(self.task, {}, self.primary, additional_references=[{'path': self.extra}])
            body = post.call_args.kwargs['json']
            messages = body['messages']
            sent = [base64.b64decode(item) for item in messages[-1]['images']]
            data = json.loads(messages[-1]['content'])
        self.assertEqual(sent, [self.primary.read_bytes(), self.extra.read_bytes()])
        self.assertEqual(data['user_animation_intent'], '让这个角色行走')
        self.assertEqual(data['duration_seconds'], 4)
        self.assertEqual(result['description'], DESCRIPTION)
        self.assertEqual(result['role'], 'reference-grounded-motion-planner')
        self.assertEqual(len(result['reference_sha256']), 2)
        self.assertNotIn('api_key', json.dumps(result))
        system = messages[0]['content'].lower()
        for concept in ('primary reference image', 'reference pose', 'body facing', 'anatomy',
                        'chronological', 'weight transfer', 'joint motion', 'support and swing',
                        'stepping in place', 'duration_seconds'):
            self.assertIn(concept, system)

    def test_missing_or_non_image_reference_rejected_before_model_request(self):
        invalid = self.image_dir / 'not-an-image.png'
        invalid.write_bytes(b'not image bytes')
        for reference in [None, self.image_dir / 'missing.png', invalid]:
            with self.subTest(reference=reference), patch.object(self.clients, 'require_visual_intent_ready') as ready, \
                 patch.object(self.clients, 'qwen') as qwen:
                with self.assertRaisesRegex(PermanentError, '有效的角色参考图片'):
                    self.clients.translate_animation(self.task, {}, reference)
                ready.assert_not_called()
                qwen.assert_not_called()

    def test_compiler_uses_visual_action_plan_and_keeps_production_constraints(self):
        compiled = compile_prompt(self.task, {}, self.primary, translation={'description': DESCRIPTION})
        self.assertIn('Image-grounded animation instructions: ' + DESCRIPTION, compiled['prompt'])
        self.assertIn('stationary root', compiled['prompt'])
        self.assertIn('RGB 128, 128, 128', compiled['prompt'])
        self.assertIn('exact viewing angle', compiled['prompt'])
        self.assertEqual(compiled['h3_request']['seconds'], 4)
        self.assertEqual(compiled['llm_translation']['description'], DESCRIPTION)

    def service_and_snapshot(self):
        clients = Mock(spec=ModelClients)
        clients.video_profile = Mock(return_value=VIDEO)
        service = Service(self.settings, Mock(), clients=clients)
        service.validate_h3_references = Mock()
        service.motion_description = Mock(return_value='motion reference')
        snapshot = {'character': {'profile': {}, 'revision': 1}, 'reference': {'path': str(self.primary)}}
        return service, clients, snapshot

    def test_direct_compile_does_not_resolve_or_invoke_visual_model(self):
        service, clients, snapshot = self.service_and_snapshot()
        direct = AnimationTask(character_id='test', prompt_mode='direct', video_prompt='walk', preview=True).model_dump()
        result = service.compile_snapshot(direct, snapshot, self.root / 'analysis')
        clients.require_visual_intent_ready.assert_not_called()
        clients.translate_animation.assert_not_called()
        service.motion_description.assert_not_called()
        self.assertIsNone(result['llm_translation'])

    def test_assisted_preview_checks_visual_provider_before_motion_analysis(self):
        service, clients, snapshot = self.service_and_snapshot()
        snapshot['motion'] = {'id': 'motion'}
        clients.require_visual_intent_ready.side_effect = PermanentError('需要能识图的模型')
        with self.assertRaisesRegex(PermanentError, '识图'):
            service.compile_snapshot(self.task, snapshot, self.root / 'analysis')
        clients.translate_animation.assert_not_called()
        service.motion_description.assert_not_called()

    @unittest.skipUnless(COMMUNITY, 'Community capability registry boundary')
    def test_missing_vision_fails_preview_and_admission_without_any_inference(self):
        with patch.object(self.clients, 'video_profile', return_value=VIDEO), \
             patch('animation.matting.configuration', return_value={'available': True, 'name': 'Matting API'}), \
             patch('animation.clients.providers.resolve', side_effect=ValueError('missing vision')) as resolve, \
             patch('animation.clients.providers.invoke_sync') as invoke:
            with self.assertRaisesRegex(PermanentError, '识图'):
                self.clients.translate_animation(self.task, {}, self.primary)
            readiness = self.clients.generation_readiness(prompt_mode='assisted')
            self.assertFalse(readiness['ready'])
            self.assertEqual(readiness['missing_services'], ['vision'])
            self.assertIn('识图', readiness['message'])
            service = Service(self.settings, Mock(), clients=self.clients)
            service.store.find_idempotency.return_value = None
            service.snapshot = Mock(return_value={})
            with self.assertRaises(GenerationServicesUnavailable):
                service.enqueue(self.task)
            service.store.enqueue.assert_not_called()
            self.assertFalse((self.root / 'jobs').exists())
            invoke.assert_not_called()
            resolve.reset_mock()
            self.assertTrue(self.clients.generation_readiness(prompt_mode='direct')['ready'])
            resolve.assert_not_called()

    @unittest.skipIf(COMMUNITY, 'Private managed Qwen boundary')
    def test_private_managed_assisted_checks_qwen_but_direct_does_not(self):
        clients = ModelClients(replace(self.settings, managed_models=True))
        h3 = httpx.Response(200, json={'status': 'ok'})
        comfy = httpx.Response(200, json={'system': {'os': 'linux'}, 'devices': []})
        with patch('animation.clients.httpx.get', side_effect=[h3, comfy, h3, comfy]), \
             patch.object(clients, 'require_qwen_loaded', side_effect=TransientError('Qwen未启动')) as qwen:
            self.assertEqual(clients.generation_readiness(prompt_mode='assisted')['missing_services'], ['qwen'])
            qwen.reset_mock()
            self.assertTrue(clients.generation_readiness(prompt_mode='direct')['ready'])
            qwen.assert_not_called()


if __name__ == '__main__':
    unittest.main()

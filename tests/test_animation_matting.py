"""Isolated API matting, crash recovery and worker postprocess wiring."""
import base64
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from PIL import Image, ImageDraw

from animation.clients import NeedsAttention, PermanentError, TransientError
from animation.files import sha256
from animation.matting import ApiMatting, configuration, freeze_provider, matte_provider
from animation.postprocess import process_video
from animation.worker import Worker


class MattingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='api-matting-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = {'id': 'matte-1', 'name': 'Image API', 'kind': 'image', 'protocol': 'rest',
                        'base_url': 'https://image.example', 'model': 'matte-model',
                        'api_key': 'test-only-secret', 'options': {}}
        self.bindings = [{'module': 'animation', 'role': 'image', 'profile_id': self.profile['id']}]
        registry = SimpleNamespace(list_bindings=lambda: self.bindings)
        self.addCleanup(patch.stopall)
        patch('providers.registry', return_value=registry).start()
        patch('providers.resolve', side_effect=lambda *args: copy.deepcopy(self.profile)).start()
        self.job = {'id': 'isolated', 'owner_id': 'member-1', 'stage': 'queued', 'checkpoint': {}}
        self.saved = []
        self.worker = SimpleNamespace(settings=SimpleNamespace(data_root=self.root, max_poll_seconds=10, poll_seconds=0),
            clock=lambda: 100, sleep=lambda _: None, check_cancel=lambda job: None, checkpoint=self.checkpoint)
        self.source = self.root / 'source.png'
        image = Image.new('RGB', (64, 64), (128, 128, 128))
        ImageDraw.Draw(image).rectangle((20, 10, 43, 58), fill='red')
        image.save(self.source)
        self.png = self.rgba()
        self.calls = []
        self.original_client = httpx.Client

    def checkpoint(self, job, stage, **values):
        job['checkpoint'].update(values)
        job['stage'] = stage
        self.saved.append(copy.deepcopy(job['checkpoint']))

    @staticmethod
    def rgba(size=(64, 64), mode='RGBA', alpha=None):
        image = Image.new(mode, size, (0, 0, 0, 0) if mode == 'RGBA' else 'white')
        ImageDraw.Draw(image).rectangle((20, 10, 43, 58), fill=(255, 0, 0, 255) if mode == 'RGBA' else 'red')
        if alpha is not None:
            image.putalpha(alpha)
        stream = io.BytesIO()
        image.save(stream, 'PNG')
        return stream.getvalue()

    def result(self, data=None):
        return {'status': 'succeeded', 'artifacts': [{'data_base64': base64.b64encode(data or self.png).decode()}]}

    def transport(self, handler):
        def dispatch(request):
            self.calls.append(request)
            return handler(request)
        return patch('media.remote.httpx.Client', side_effect=lambda **kwargs:
                     self.original_client(transport=httpx.MockTransport(dispatch), **kwargs))

    def provider(self):
        freeze_provider(self.worker, self.job)
        return ApiMatting(self.worker, self.job)

    def run_frames(self, provider=None):
        return (provider or self.provider())([self.source], self.root / 'unused')

    def test_missing_binding_blocks_generation_without_cpu_fallback(self):
        self.bindings.clear()
        with patch('providers.resolve', side_effect=AssertionError('no API')):
            with self.assertRaises(PermanentError):
                matte_provider(self.worker, self.job)
            self.assertFalse(configuration()['available'])
            self.assertNotIn('matting_provider', self.job['checkpoint'])

    def test_old_cpu_checkpoint_never_silently_changes_or_bypasses_image_api(self):
        self.job['checkpoint']['matting_provider'] = {'mode': 'cpu', 'version': 1}
        with self.assertRaises(NeedsAttention):
            matte_provider(self.worker, self.job)

    def test_unverified_binding_is_not_silently_cpu(self):
        with patch('providers.resolve', side_effect=ValueError('unverified')):
            self.assertFalse(configuration()['available'])
            with self.assertRaises(PermanentError): self.provider()

    def test_rest_request_and_durable_completed_cache(self):
        def handler(request):
            self.assertEqual(self.saved[-1]['matting_frames'][0]['state'], 'submitting')
            payload = json.loads(request.content)
            self.assertEqual(payload['kind'], 'image')
            self.assertEqual(payload['input']['operation'], 'remove_background')
            self.assertEqual(payload['input']['image']['mime_type'], 'image/png')
            self.assertEqual(base64.b64decode(payload['input']['image']['data_base64']), self.source.read_bytes())
            return httpx.Response(200, json=self.result())
        with self.transport(handler):
            result = self.run_frames()
            self.run_frames(self.provider())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['metadata']['frame_count'], 1)
        self.assertNotIn('test-only-secret', json.dumps(self.job))
        with Image.open(result['paths'][0]) as image:
            self.assertEqual(image.mode, 'RGBA')

    def test_openai_uses_edit_multipart_and_transparent_parameters(self):
        self.profile['protocol'] = 'openai'
        def handler(request):
            self.assertEqual(request.url.path, '/images/edits')
            self.assertIn(b'name="image"', request.content)
            self.assertIn(b'name="background"\r\n\r\ntransparent', request.content)
            self.assertIn(b'name="output_format"\r\n\r\npng', request.content)
            self.assertIn(b'64x64', request.content)
            return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(self.png).decode()}]})
        with self.transport(handler): self.run_frames()

    def test_generated_rgb_is_discarded_original_frame_colors_are_kept(self):
        image = Image.open(io.BytesIO(self.png))
        alpha = image.getchannel('A')
        image = Image.new('RGBA', image.size, 'blue')
        image.putalpha(alpha)
        stream = io.BytesIO(); image.save(stream, 'PNG')
        with self.transport(lambda request: httpx.Response(200, json=self.result(stream.getvalue()))):
            result = self.run_frames()
        with Image.open(result['paths'][0]) as output:
            self.assertEqual(output.getpixel((30, 30)), (255, 0, 0, 255))

    def test_unknown_submit_survives_restart_without_second_post(self):
        def handler(request): raise httpx.ReadTimeout('lost reply', request=request)
        with self.transport(handler):
            with self.assertRaises(NeedsAttention): self.run_frames()
            self.job = copy.deepcopy(self.job)
            with self.assertRaises(NeedsAttention): self.run_frames(self.provider())
        self.assertEqual(len(self.calls), 1)

    def test_rejected_submit_is_not_repeated_on_reprocess(self):
        with self.transport(lambda request: httpx.Response(400)):
            with self.assertRaises(PermanentError): self.run_frames()
            with self.assertRaises(NeedsAttention): self.run_frames(self.provider())
        self.assertEqual(len(self.calls), 1)

    def test_async_restart_polls_saved_id_without_reposting(self):
        def first(request):
            return httpx.Response(200, json={'id': 'frame-1', 'status': 'queued'}) if request.method == 'POST' else httpx.Response(503)
        with self.transport(first):
            with self.assertRaises(TransientError): self.run_frames()
        self.assertEqual(self.job['checkpoint']['matting_frames'][0]['remote_id'], 'frame-1')
        with self.transport(lambda request: httpx.Response(200, json={'id': 'frame-1', **self.result()})):
            self.run_frames(self.provider())
        self.assertEqual(sum(request.method == 'POST' for request in self.calls), 1)
        self.assertEqual(self.calls[-1].url.path, '/jobs/frame-1')

    def test_saved_response_reused_after_crash_before_png(self):
        provider = self.provider()
        with self.transport(lambda request: httpx.Response(200, json=self.result())):
            with patch.object(provider, 'completed_image', side_effect=SystemExit('crash')):
                with self.assertRaises(SystemExit): self.run_frames(provider)
            self.run_frames(self.provider())
        self.assertEqual(len(self.calls), 1)

    def test_result_download_failure_only_retries_saved_download(self):
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, json={'artifacts': [{'url': 'https://cdn.example/frame.png'}]})
            self.assertNotIn('authorization', request.headers)
            raise httpx.ReadTimeout('download interrupted', request=request)
        with self.transport(handler):
            with self.assertRaises(TransientError): self.run_frames()
        with self.transport(lambda request: httpx.Response(200, content=self.png)):
            self.run_frames(self.provider())
        self.assertEqual(sum(request.method == 'POST' for request in self.calls), 1)

    def test_changed_profile_never_posts_and_key_rotation_is_allowed(self):
        provider = self.provider()
        self.profile['model'] = 'different'
        with self.transport(lambda request: self.fail('unexpected request')):
            with self.assertRaises(NeedsAttention): self.run_frames(provider)
        self.profile['model'] = 'matte-model'
        self.profile['api_key'] = 'rotated-key'
        with self.transport(lambda request: httpx.Response(200, json=self.result())): self.run_frames(provider)

    def test_changed_source_does_not_reuse_or_regenerate_paid_frame(self):
        with self.transport(lambda request: httpx.Response(200, json=self.result())):
            self.run_frames()
            Image.new('RGB', (64, 64), 'green').save(self.source)
            with self.assertRaises(NeedsAttention): self.run_frames(self.provider())
        self.assertEqual(len(self.calls), 1)

    def test_missing_completed_file_never_regenerates(self):
        with self.transport(lambda request: httpx.Response(200, json=self.result())):
            result = self.run_frames()
            result['paths'][0].unlink()
            with self.assertRaises(NeedsAttention): self.run_frames(self.provider())
        self.assertEqual(len(self.calls), 1)

    def test_invalid_output_modes_sizes_and_alpha_are_rejected(self):
        invalid = [self.rgba((32, 32)), self.rgba(mode='RGB'), self.rgba(alpha=255), self.rgba(alpha=0)]
        for data in invalid:
            with self.subTest(size=len(data)), self.transport(lambda request: httpx.Response(200, json=self.result(data))):
                self.job = {'id': str(len(self.calls)), 'stage': 'queued', 'checkpoint': {}}
                with self.assertRaises(PermanentError): self.run_frames()

    def test_unsafe_async_id_is_unknown_and_never_used_as_path(self):
        with self.transport(lambda request: httpx.Response(200, json={'id': '../other', 'status': 'queued'})):
            with self.assertRaises(NeedsAttention): self.run_frames()
        self.assertEqual(len(self.calls), 1)

    def test_other_job_id_from_poll_is_rejected(self):
        def handler(request):
            return httpx.Response(200, json={'id': 'first', 'status': 'queued'} if request.method == 'POST' else {'id': 'other', **self.result()})
        with self.transport(handler):
            with self.assertRaises(NeedsAttention): self.run_frames()
        self.assertEqual(len(self.calls), 2)

    def test_worker_preserves_needs_attention_from_postprocessor(self):
        folder = self.root / 'jobs' / self.job['id']
        folder.mkdir(parents=True)
        video = folder / 'generation.mp4'
        video.write_bytes(b'existing-video')
        self.job.update(task={'postprocessing': {}, 'target': {'anchor': 'feet_center'}}, snapshot={},
                        attempts=1, max_attempts=3)
        self.job['checkpoint'] = {'h3_job_id': 'already-generated', 'h3_terminal': 'completed',
                                  'compiled': {'compiler_version': 'test'}, 'video': {'sha256': sha256(video)}}
        updates = []
        store = SimpleNamespace(get=lambda *args: {'cancel_requested': False},
            update_job=lambda job_id, **values: updates.append(values), notify=lambda *args: None)
        def processor(video, destination, options, *, matte_provider):
            self.assertIsInstance(matte_provider, ApiMatting)
            raise NeedsAttention('uncertain frame')
        worker = Worker(self.worker.settings, store, clients=object(), postprocessor=processor)
        worker.handle(self.job)
        self.assertEqual(updates[-1]['status'], 'needs_attention')
        self.assertFalse(updates[-1]['retryable'])

    def test_real_ffmpeg_postprocess_accepts_api_rgba_and_records_algorithm(self):
        video = self.root / 'tiny.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-loop', '1', '-i', str(self.source), '-t', '0.5',
                        '-r', '4', '-pix_fmt', 'yuv420p', str(video)], check=True)
        with self.transport(lambda request: httpx.Response(200, json=self.result())):
            result = process_video(video, self.root / 'asset', {'fps': 4, 'frame_width': 64, 'frame_height': 64,
                'max_frames': 2}, matte_provider=self.provider())
        manifest = json.loads((self.root / 'asset' / 'manifest.json').read_text())
        self.assertEqual(manifest['algorithm'], 'api-matte-rgba-v1')
        self.assertEqual(result['background_model']['frame_count'], 2)
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__': unittest.main()

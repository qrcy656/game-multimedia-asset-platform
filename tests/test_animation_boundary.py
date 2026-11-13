"""Community animation boundary checks; no production services or model calls."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
import httpx
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient
from animation.auth import sign_token
from animation.api import create_app
from animation.clients import ModelClients, NeedsAttention, PermanentError, H3NotSubmitted, GenerationServicesUnavailable
from animation.compiler import compile_prompt
from animation.config import Settings
from animation.files import file_url
from animation.models import AnimationTask
from animation.postprocess import process_video
from animation.service import Service

PROFILE = {'id':'video-profile','name':'Video provider','kind':'video','protocol':'videos',
           'base_url':'https://video.example/v1','model':'video-model','api_key':'test-secret','options':{}}

def response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request('POST', 'https://video.example/v1/videos'))

class AnimationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = Settings(data_root=self.root, identity_secret='a'*40)
        (self.root/'characters'/'test').mkdir(parents=True)
        self.image = self.root/'characters'/'test'/'reference.png'
        Image.new('RGB',(128,128),(128,128,128)).save(self.image)
        self.client = ModelClients(self.settings)
        self.task = AnimationTask(character_id='test',prompt_mode='direct',video_prompt='Wave once',preview=True).model_dump()

    def tearDown(self):
        self.tmp.cleanup()

    def token(self):
        now = int(time.time())
        return sign_token({'iss':'fantasy-main','aud':'animation-studio','sub':'user-1','name':'Tester',
                           'role':'admin','iat':now,'exp':now+300},self.settings.identity_secret)

    def test_compile_does_not_choose_a_private_model(self):
        compiled = compile_prompt(self.task,{},self.image)
        self.assertNotIn('model',compiled['h3_request'])
        self.assertEqual(file_url(self.root,self.image),'/animation/files/characters/test/reference.png')

    def test_direct_generation_requires_video_and_matting_but_no_llm(self):
        with patch.object(self.client, 'video_profile', return_value=PROFILE), patch('animation.matting.configuration',
                return_value={'mode': 'api', 'available': False, 'name': '抠图 API', 'message': '请配置抠图 API'}):
            readiness = self.client.generation_readiness()
            self.assertFalse(readiness['ready'])
            self.assertEqual(readiness['missing_services'], ['image'])
            with self.assertRaises(GenerationServicesUnavailable):
                self.client.require_generation_ready()
        with patch.object(self.client, 'video_profile', return_value=PROFILE), patch('animation.matting.configuration',
                return_value={'mode': 'api', 'available': True, 'name': '抠图 API'}), patch('providers.invoke_sync') as llm:
            self.assertTrue(self.client.require_generation_ready()['ready'])
            llm.assert_not_called()

    def test_submit_sends_bytes_and_selected_model(self):
        request = compile_prompt(self.task,{},self.image)['h3_request']
        with patch('animation.clients.providers.resolve',return_value=PROFILE), patch('animation.clients.httpx.post',return_value=response({'id':'remote-1','status':'queued'})) as post:
            self.assertEqual(self.client.submit_h3(request,PROFILE)['id'],'remote-1')
        args, kwargs = post.call_args
        self.assertEqual(args[0],'https://video.example/v1/videos')
        self.assertEqual(kwargs['headers'],{'Authorization':'Bearer test-secret'})
        body = kwargs['json']
        self.assertEqual(body['model'],'video-model')
        self.assertEqual(base64.b64decode(body['conditions'][0]['uri'].split(',',1)[1]),self.image.read_bytes())
        self.assertNotIn('file:',json.dumps(body))
        self.assertFalse(kwargs['follow_redirects'])

    def test_frozen_provider_change_cannot_retarget_a_job(self):
        with patch('animation.clients.providers.resolve',return_value={**PROFILE,'base_url':'https://different.example'}):
            with self.assertRaises(NeedsAttention):
                self.client.h3_status('old-id',PROFILE)

    def test_video_response_keeps_only_contract_fields_and_rejects_other_job(self):
        raw = {'id': 'remote-1', 'status': 'failed', 'authorization': 'secret-key',
               'error': {'debug': 'secret-key'}, 'artifacts': [{'type': 'video', 'url': '/result.mp4', 'api_key': 'secret-key'}]}
        state = self.client._state(raw)
        self.assertNotIn('secret-key', json.dumps(state))
        self.assertEqual(state['artifacts'], [{'type': 'video', 'url': '/result.mp4'}])
        with patch('animation.clients.providers.resolve', return_value=PROFILE), patch('animation.clients.httpx.get', return_value=response(raw)):
            with self.assertRaises(NeedsAttention):
                self.client.h3_status('other-job', PROFILE)

    def test_public_job_responses_hide_remote_receipts_but_keep_progress(self):
        from types import SimpleNamespace
        row = {'id': 'job-1', 'owner_id': 'user-1', 'status': 'running', 'checkpoint': {
            'submit_response': {'debug': 'secret-key'}, 'generation_response': {'url': '/signed?key=secret-key'},
            'h3_job_id': 'remote-1', 'video': {'sha256': 'abc'}, 'matting_frames': [{'state': 'completed'}]}}
        store = SimpleNamespace(get=lambda *args: row, list=lambda *args, **kwargs: [row])
        client = TestClient(create_app(self.settings, store=store))
        for endpoint in ('/api/jobs', '/api/jobs/job-1'):
            result = client.get(endpoint, headers={'Authorization': 'Bearer ' + self.token()})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertNotIn('secret-key', result.text)
            self.assertIn('matting_frames', result.text)
        self.assertIn('submit_response', row['checkpoint'])
        folder = self.root / 'jobs' / 'job-1'
        folder.mkdir(parents=True)
        (folder / 'receipt.json').write_text('{"debug":"secret-key"}')
        (folder / 'generation.mp4').write_bytes(b'owned-video')
        headers = {'Authorization': 'Bearer ' + self.token()}
        self.assertEqual(client.get('/files/jobs/job-1/receipt.json', headers=headers).status_code, 404)
        self.assertEqual(client.get('/files/jobs/job-1/generation.mp4', headers=headers).content, b'owned-video')

    def test_transport_connect_failure_is_distinguished(self):
        request = compile_prompt(self.task,{},self.image)['h3_request']
        with patch('animation.clients.providers.resolve',return_value=PROFILE), patch('animation.clients.httpx.post',side_effect=httpx.ConnectError('no connection')):
            with self.assertRaises(H3NotSubmitted):
                self.client.submit_h3(request,PROFILE)

    def test_foreign_artifact_never_receives_api_key(self):
        profile = {**PROFILE,'protocol':'rest'}
        with patch('animation.clients.providers.resolve',return_value=profile), patch('animation.clients.httpx.stream') as stream:
            with self.assertRaises(PermanentError):
                self.client.download_h3('id',self.root/'download.mp4',profile,{'artifacts':[{'type':'video','url':'https://foreign.example/secret.mp4'}]})
            stream.assert_not_called()

    def test_rest_sync_artifact_is_completed_without_polling(self):
        profile = {**PROFILE,'protocol':'rest'}
        request = compile_prompt(self.task,{},self.image)['h3_request']
        with patch('animation.clients.providers.resolve',return_value=profile), patch('animation.clients.httpx.post',return_value=response({'artifacts':[{'type':'video','url':'/result.mp4'}]})) as post:
            result = self.client.submit_h3(request,profile)
        self.assertEqual(result['status'],'completed')
        self.assertTrue(result['id'].startswith('sync-'))
        self.assertEqual(post.call_args.kwargs['json']['kind'],'video')
        self.assertIn('input',post.call_args.kwargs['json'])

    def test_assisted_action_instructions_use_vision_and_primary_image(self):
        task = {**self.task,'prompt_mode':'assisted','supplementary_description':'挥手'}
        llm = {**PROFILE,'id':'visual-planner','protocol':'openai','kind':'vision','model':'visual-model'}
        with patch('animation.clients.providers.resolve',return_value=llm) as resolve, patch('animation.clients.providers.invoke_sync',return_value={'choices':[{'message':{'content':'{"description":"Wave once."}'}}]}) as invoke:
            result = self.client.translate_animation(task,{},self.image)
        self.assertEqual(resolve.call_args.args,('animation','vision'))
        self.assertEqual(invoke.call_args.args[1],'vision')
        content = invoke.call_args.args[2]['messages'][-1]['content']
        self.assertEqual(base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1]), self.image.read_bytes())
        self.assertEqual(result['model'],'visual-model')
        self.assertNotIn('api_key',json.dumps(result))

    def test_visual_analysis_uses_vision_role_with_image_bytes(self):
        with patch('animation.clients.providers.resolve',return_value=PROFILE), patch('animation.clients.providers.invoke_sync',return_value={'choices':[{'message':{'content':'{"description":"A visible character."}'}}]}) as invoke:
            self.client.analyze_character([self.image])
        self.assertEqual(invoke.call_args.args[1],'vision')
        self.assertTrue(invoke.call_args.args[2]['messages'][-1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_only_gateway_identity_and_same_origin_mutations(self):
        client = TestClient(create_app(self.settings,store=object()))
        self.assertEqual(client.get('/api/auth/session').status_code,401)
        self.assertEqual(client.get('/api/auth/session',headers={'X-API-Key':'a'*40,'X-Animation-Context':self.token()}).status_code,401)
        self.assertEqual(client.get('/api/auth/session',headers={'Authorization':'Bearer '+self.token()}).json()['user']['id'],'user-1')
        self.assertEqual(client.post('/api/batches',json={},headers={'Authorization':'Bearer '+self.token(),'Origin':'https://foreign.example'}).status_code,403)
        self.assertEqual(client.post('/api/auth/exchange',json={'token':self.token()}).status_code,401)

    def test_batch_native_idempotency_is_owned_and_stable(self):
        app = create_app(self.settings,store=object())
        service, records = app.state.service, {}
        def enqueue(task,owner,admin):
            key = (owner['owner_id'],task['idempotency_key'])
            records.setdefault(key,{'id':'job-'+str(len(records)),'task':task})
            return records[key]
        with patch.object(service,'snapshot',return_value={}), patch.object(service,'enqueue',side_effect=enqueue):
            client = TestClient(app)
            headers = {'Authorization':'Bearer '+self.token(),'Origin':'http://localhost:18082'}
            payload = {'batch_id':'batch-1','tasks':[self.task,self.task]}
            first = client.post('/api/batches',json=payload,headers=headers)
            again = client.post('/api/batches',json=payload,headers=headers)
        self.assertEqual(first.status_code,202)
        self.assertEqual(first.json(),again.json())
        self.assertEqual(set(records),{('user-1','batch:batch-1:0'),('user-1','batch:batch-1:1')})

    def test_empty_bootstrap_never_imports_private_examples(self):
        class Store:
            def recover_deletions(self): pass
            def create_character(self,*args,**kwargs): raise AssertionError('No seed data may be imported')
        Service(self.settings,Store()).bootstrap()
        self.assertEqual(set(p.name for p in self.root.iterdir()),{'characters','motions','jobs','assets'})

    def test_cpu_postprocessing_exports_transparency(self):
        picture = Image.new('RGB',(128,128),(128,128,128))
        ImageDraw.Draw(picture).rectangle((45,25,80,110),fill=(220,30,30))
        source, video = self.root/'source.png', self.root/'source.mp4'
        picture.save(source)
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-loop','1','-i',str(source),'-t','1','-r','4','-pix_fmt','yuv420p',str(video)],check=True,timeout=30)
        result = process_video(video,self.root/'output',{'fps':4,'frame_width':64,'frame_height':64})
        frames = sorted((self.root/'output'/result['frames_dir']).glob('*.png'))
        self.assertTrue(frames)
        with Image.open(frames[0]) as image:
            self.assertEqual(image.mode,'RGBA')
            self.assertEqual(image.getchannel('A').getextrema(),(0,255))

    def test_community_ui_contains_no_private_links(self):
        static = PACKAGE_ROOT/'animation'/'static'
        text = (static/'index.html').read_text()+(static/'app.js').read_text()
        for forbidden in ('n8n','172.16.0.53','Qwen-Image',"character_id:'eros'"):
            self.assertNotIn(forbidden,text)
        self.assertIn('/animation/static/',text)
        self.assertIn('/community.js',text)
        self.assertIn('视频 API 与抠图 API',text)
        self.assertNotIn('未绑定时使用 CPU',text)

if __name__ == '__main__':
    unittest.main()

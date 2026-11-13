"""Real PostgreSQL + Redis + worker acceptance, using a temporary HTTP video API.

Run only with an EMPTY dedicated PostgreSQL database and Redis instance. The app
data lives in this container's temporary directory; no GPU or real provider runs.
The synthetic image-removal API is always bound; no real model runs.
"""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import zipfile

import psycopg
import redis
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ORIGIN = 'http://localhost:18082'


def check(response, expected=200):
    if response.status_code != expected:
        raise AssertionError(f'{response.request.method} {response.request.url}: {response.status_code} {response.text[:2000]}')
    return response


def main():
    api_matting = True
    db_url = os.environ['DATABASE_URL']
    redis_url = os.environ['REDIS_URL']
    deadline = time.monotonic() + 60
    while True:
        try:
            with psycopg.connect(db_url) as conn:
                tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
                if tables:
                    raise AssertionError('Smoke test requires an empty isolated database')
            cache = redis.Redis.from_url(redis_url)
            assert cache.ping()
            assert cache.dbsize() == 0, 'Smoke test requires an empty isolated Redis instance'
            break
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)

    with tempfile.TemporaryDirectory(prefix='animation-postgres-smoke-') as directory:
        root = Path(directory)
        os.environ.update(COMMUNITY_DATA_DIR=str(root), ANIMATION_DATA_DIR=str(root / 'animation'),
                          COMMUNITY_ENABLE_ANIMATION='true', COMMUNITY_RUN_QUEUE='false',
                          COMMUNITY_REPOSITORY_DEFAULT='disabled',
                          APP_ORIGIN=ORIGIN, ANIMATION_POLL_SECONDS='0.1')
        picture = Image.new('RGB', (128, 128), (128, 128, 128))
        ImageDraw.Draw(picture).rectangle((45, 25, 80, 110), fill=(220, 30, 30))
        source = root / 'reference.png'
        picture.save(source)
        video = root / 'generated.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-loop', '1', '-i', str(source),
                        '-t', '4', '-r', '4', '-pix_fmt', 'yuv420p', str(video)], check=True, timeout=30)
        video_bytes = video.read_bytes()
        calls = []
        provider_errors = []

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200, content_type='application/json'):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urlsplit(self.path).path
                calls.append(('GET', path))
                if self.headers.get('Authorization') != 'Bearer isolated-test-key':
                    return self.send({'detail': 'Missing configured key'}, 401)
                if path == '/v1/models':
                    return self.send({'data': [{'id': 'integration-video', 'capabilities': ['video']}]})
                if path == '/image/capabilities' and api_matting:
                    return self.send({'model': 'integration-matting', 'capabilities': ['image'],
                        'api_version': 'fantasy-v1', 'input_schema': {'type': 'object'},
                        'output_schema': {'type': 'object'}})
                if path == '/v1/videos':
                    return self.send({'data': []})
                if path == '/v1/videos/remote-smoke-id':
                    return self.send({'id': 'remote-smoke-id', 'status': 'completed'})
                if path == '/v1/videos/remote-smoke-id/content':
                    return self.send(video_bytes, content_type='video/mp4')
                return self.send({'detail': 'Unexpected endpoint'}, 404)

            def do_POST(self):
                path = urlsplit(self.path).path
                calls.append(('POST', path))
                try:
                    assert self.headers.get('Authorization') == 'Bearer isolated-test-key'
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if path == '/image/validate' and api_matting:
                        assert payload['validate_only'] is True
                        assert payload['model'] == 'integration-matting' and payload['kind'] == 'image'
                        return self.send({'valid': True, 'kind': 'image', 'model': 'integration-matting'})
                    if path == '/image/generate' and api_matting:
                        assert payload['kind'] == 'image' and payload['model'] == 'integration-matting'
                        params = payload['input']
                        assert params['operation'] == 'remove_background'
                        assert params['image']['mime_type'] == 'image/png'
                        with Image.open(io.BytesIO(base64.b64decode(params['image']['data_base64']))) as original:
                            assert original.size == (params['width'], params['height'])
                            pixels = original.convert('RGB')
                        mask = Image.new('L', pixels.size)
                        mask.putdata([255 if r > g + 50 and r > b + 50 else 0 for r, g, b in pixels.getdata()])
                        assert mask.getextrema() == (0, 255)
                        # Deliberately recolor the result: the application must consume only API alpha.
                        output = Image.new('RGBA', pixels.size, (255, 0, 255, 255))
                        output.putalpha(mask)
                        encoded = io.BytesIO(); output.save(encoded, format='PNG')
                        return self.send({'artifacts': [{'data_base64': base64.b64encode(encoded.getvalue()).decode()}]})
                    assert path == '/v1/videos'
                    assert payload['model'] == 'integration-video'
                    assert payload['seconds'] == 4
                    assert 'file:' not in json.dumps(payload)
                    reference = payload['conditions'][0]['uri']
                    assert reference.startswith('data:image/png;base64,')
                    assert base64.b64decode(reference.split(',', 1)[1]) == source.read_bytes()
                    assert 'Wave once' in payload['prompt']
                except Exception as error:
                    provider_errors.append(repr(error))
                    return self.send({'detail': 'Provider contract assertion failed'}, 400)
                return self.send({'id': 'remote-smoke-id', 'status': 'queued'}, 201)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        worker = None
        member_client = None
        log = (root / 'worker.log').open('w+')
        try:
            import app as application
            with TestClient(application.app, base_url=ORIGIN) as client:
                login = check(client.post('/api/login', json={'username': 'admin', 'password': '12345678'}, headers={'Origin': ORIGIN})).json()
                headers = {'Origin': ORIGIN, 'X-Community-CSRF': login['csrf']}
                actor = login['user']['id']
                session = check(client.get('/animation/api/auth/session')).json()
                assert session['user']['id'] == actor
                assert check(client.get('/animation/api/catalog')).json()['characters'] == []
                health = check(client.get('/animation/api/health')).json()
                assert health['database'] and health['redis'], health
                profile = check(client.post('/api/profiles', json={'scope': 'global', 'name': 'Isolated video API', 'kind': 'video', 'protocol': 'videos',
                    'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'integration-video', 'api_key': 'isolated-test-key'}, headers=headers), 201).json()
                verified = check(client.post(f"/api/profiles/{profile['id']}/verify", json={}, headers=headers)).json()
                assert verified['verified']
                check(client.put('/api/bindings/animation/video', json={'profile_id': profile['id']}, headers=headers))
                readiness = check(client.get('/animation/api/health')).json()
                assert not readiness['generation_ready'] and 'image' in readiness['generation_missing_services']
                if api_matting:
                    matte = check(client.post('/api/profiles', json={'scope': 'global', 'name': 'Isolated removal API', 'kind': 'image', 'protocol': 'rest',
                        'base_url': f'http://127.0.0.1:{server.server_port}/image', 'model': 'integration-matting',
                        'api_key': 'isolated-test-key'}, headers=headers), 201).json()
                    assert check(client.post(f"/api/profiles/{matte['id']}/verify", json={}, headers=headers)).json()['verified']
                    check(client.put('/api/bindings/animation/image', json={'profile_id': matte['id']}, headers=headers))
                uploaded = check(client.post('/animation/api/uploads', data={'kind': 'character'},
                    files={'file': ('reference.png', source.read_bytes(), 'image/png')}, headers=headers), 201).json()
                character = check(client.post('/animation/api/characters', json={'id': 'smoke-character', 'name': 'Synthetic smoke fixture',
                    'images': [{'path': uploaded['path'], 'label': 'reference'}], 'profile': {}}, headers=headers), 201).json()
                original_actor = actor
                check(client.post('/api/users', json={'username': 'member-smoke', 'password': 'test-only-member-password',
                    'name': 'Smoke member', 'admin': False}, headers=headers), 201)
                member_client = TestClient(application.app, base_url=ORIGIN)
                member = check(member_client.post('/api/login', json={'username': 'member-smoke', 'password': 'test-only-member-password'}, headers={'Origin': ORIGIN})).json()
                actor = member['user']['id']
                member_headers = {'Origin': ORIGIN, 'X-Community-CSRF': member['csrf']}
                personal = check(member_client.post('/api/profiles', json={'scope': 'personal', 'name': 'Member video API', 'kind': 'video', 'protocol': 'videos',
                    'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'integration-video', 'api_key': 'isolated-test-key'}, headers=member_headers), 201).json()
                assert check(member_client.post(f"/api/profiles/{personal['id']}/verify", json={}, headers=member_headers)).json()['verified']
                member_headers['X-Fantasy-Model-Account'] = actor
                member_headers['X-Fantasy-Model-Selection'] = json.dumps({'animation': {'video': personal['id']}})
                assert check(member_client.get('/animation/api/characters/' + character['id'])).json()['owner_id'] == original_actor
                check(member_client.post('/animation/api/characters/' + character['id'] + '/approve', json={'profile': {'description': 'not mine'}}, headers=member_headers), 403)
                check(member_client.delete('/animation/api/characters/' + character['id'], headers=member_headers), 403)
                copied = check(member_client.post('/animation/api/characters', json={'name': 'Borrowed reference copy',
                    'images': [{'path': uploaded['path'], 'label': 'copy'}], 'profile': {}}, headers=member_headers), 201).json()
                assert copied['owner_id'] == actor and copied['images'][0]['path'] != uploaded['path']
                check(member_client.delete('/animation/api/characters/' + copied['id'], headers=member_headers))
                assert check(client.get(uploaded['url'])).content == source.read_bytes()
                task = {'character_id': character['id'], 'reference_id': character['images'][0]['id'],
                        'prompt_mode': 'direct', 'video_prompt': 'Wave once.', 'preview': True, 'duration': 4,
                        'target': {'fps': 4, 'frame_width': 64, 'frame_height': 64, 'anchor': 'feet_center'},
                        'idempotency_key': 'isolated-smoke-single-submission'}
                queued = check(member_client.post('/animation/api/animation/generate', json=task, headers=member_headers), 202).json()
                duplicate = check(member_client.post('/animation/api/animation/generate', json=task, headers=member_headers), 202).json()
                assert duplicate['id'] == queued['id']
                identifier = queued['id']
                with psycopg.connect(db_url) as conn:
                    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
                    assert conn.execute('SELECT owner_id,status FROM jobs').fetchone() == (actor, 'queued')
                    context = conn.execute('SELECT snapshot FROM jobs').fetchone()[0]['model_context']
                    assert context['user']['id'] == actor and context['selections']['animation']['video'] == personal['id']
                worker = subprocess.Popen([sys.executable, '-m', 'animation.worker'], stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
                deadline = time.monotonic() + 90
                while True:
                    job = check(client.get('/animation/api/jobs/' + identifier)).json()
                    if job['status'] == 'completed':
                        break
                    if job['status'] in ('failed', 'needs_attention', 'cancelled') or worker.poll() is not None or time.monotonic() > deadline:
                        log.flush(); log.seek(0)
                        raise AssertionError(json.dumps({'job': job, 'worker_log': log.read(), 'provider_errors': provider_errors}, ensure_ascii=False, default=str))
                    time.sleep(0.25)
                asset = check(client.get('/animation/api/assets/' + job['asset_id'])).json()
                assert asset['owner_id'] == actor
                assert 'id' not in job['checkpoint']['compiled']['video_provider']
                own_job = check(member_client.get('/animation/api/jobs/' + identifier)).json()
                assert own_job['checkpoint']['compiled']['video_provider']['id'] == personal['id']
                assert 'model_context' not in job['snapshot']
                assert asset['files']['frames']
                frame = check(client.get(asset['files']['frames'][0])).content
                with Image.open(io.BytesIO(frame)) as result:
                    assert result.mode == 'RGBA'
                    assert result.getchannel('A').getextrema() == (0, 255)
                    if api_matting:
                        opaque = [(r, g, b) for r, g, b, a in result.getdata() if a > 240]
                        assert opaque and all(r > b + 50 for r, g, b in opaque), 'API must not repaint original RGB pixels'
                with Image.open(io.BytesIO(check(client.get(asset['files']['spritesheet'])).content)) as sheet:
                    assert sheet.mode == 'RGBA'
                package = check(client.get(asset['export_url'])).content
                with zipfile.ZipFile(io.BytesIO(package)) as archive:
                    assert 'metadata.json' in archive.namelist()
                    assert any(name.startswith('frames/') and name.endswith('.png') for name in archive.namelist())
                    assert b'isolated-test-key' not in archive.read('metadata.json')
                    assert b'model_context' not in archive.read('metadata.json')
                with psycopg.connect(db_url) as conn:
                    row = conn.execute('SELECT status,stage,checkpoint FROM jobs WHERE id=%s', (identifier,)).fetchone()
                    assert row[0:2] == ('completed', 'completed')
                    assert row[2]['h3_job_id'] == 'remote-smoke-id'
                    assert row[2]['video']['sha256']
                    assert row[2]['matting_provider']['mode'] == ('api' if api_matting else 'cpu')
                    if api_matting:
                        assert len(row[2]['matting_frames']) == len(asset['files']['frames'])
                        assert all(item['state'] == 'completed' and item['output_sha256'] for item in row[2]['matting_frames'])
                        assert 'isolated-test-key' not in json.dumps(row[2])
                    assert conn.execute('SELECT count(*) FROM assets').fetchone()[0] == 1
                    assert conn.execute("SELECT state FROM worker_state WHERE id='main'").fetchone()
                # Notification is committed immediately after the completed job.
                deadline = time.monotonic() + 5
                while True:
                    with psycopg.connect(db_url) as conn:
                        notices = conn.execute('SELECT count(*) FROM notifications WHERE owner_id=%s AND job_id=%s', (actor, identifier)).fetchone()[0]
                    if notices == 1:
                        break
                    if notices > 1 or time.monotonic() > deadline:
                        raise AssertionError('Expected one completion notification')
                    time.sleep(0.1)
                assert calls.count(('POST', '/v1/videos')) == 1, calls
                assert calls.count(('POST', '/image/generate')) == (len(asset['files']['frames']) if api_matting else 0), calls
                assert not provider_errors, provider_errors
                worker.terminate(); worker.wait(timeout=10); worker = None
                source_folder = root / 'animation' / 'jobs' / identifier
                assert (source_folder / 'generation.mp4').is_file()
                assert list((source_folder / 'matting').rglob('response.json'))
                check(client.delete('/animation/api/assets/' + job['asset_id'], headers=headers))
                assert not source_folder.exists(), 'Delete original video, API base64 receipts and all matte caches together'
                assert not (root / 'animation' / 'assets' / job['asset_id']).exists()
                check(member_client.delete('/animation/api/jobs/' + identifier, headers=member_headers))
                check(client.delete('/animation/api/characters/' + character['id'], headers=headers))
                assert check(client.get('/animation/api/jobs')).json()['items'] == []
                assert check(client.get('/animation/api/assets')).json()['items'] == []
                print(json.dumps({'result': 'PASS', 'postgres': 'real schema + owned records + durable checkpoints',
                    'redis': 'real wakeup queue', 'worker': 'separate process', 'video_posts': 1,
                    'transparent_frames': len(asset['files']['frames']), 'spritesheet': True, 'zip': True,
                    'matting_mode': 'api' if api_matting else 'cpu',
                    'matting_posts': calls.count(('POST', '/image/generate')),
                    'cleanup_api': True, 'gpu_calls': 0}, ensure_ascii=False), flush=True)
        finally:
            if member_client:
                member_client.close()
            if worker and worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill(); worker.wait(timeout=5)
            server.shutdown(); server.server_close()
            log.close()


if __name__ == '__main__':
    main()

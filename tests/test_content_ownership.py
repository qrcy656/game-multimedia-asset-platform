"""Authenticated shared content boundaries; fixtures never call a real model."""
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from PIL import Image

from animation.api import create_app, public_job
from animation.auth import sign_token
from animation.config import Settings
from animation.files import sha256
from animation.models import AnimationTask
from animation.service import Service
from animation.store import Store
from animation.worker import Worker
from providers import capture_model_context, model_context


def auth(settings, user='bob', role='user'):
    now = int(time.time())
    token = sign_token({'iss': settings.identity_issuer, 'aud': settings.identity_audience,
        'sub': user, 'name': 'Same display name', 'role': role, 'iat': now, 'exp': now + 60}, settings.identity_secret)
    return {'Authorization': 'Bearer ' + token, 'Origin': settings.app_origin}


def test_animation_api_shared_reads_and_edit_checks_without_service_lifespan(tmp_path):
    settings = Settings(data_root=tmp_path, identity_secret='a' * 40)
    row = {'id': 'shared-job', 'owner_id': 'alice', 'owner_name': 'Same display name',
           'status': 'queued', 'task': {}, 'checkpoint': {}}
    store = SimpleNamespace(get=lambda *args: copy.deepcopy(row),
        list=lambda table, limit=100, owner_id=None: [copy.deepcopy(row)] if owner_id in (None, 'alice') else [],
        cancel=Mock(return_value={**row, 'status': 'cancelled'}),
        resource_guard=lambda *args: contextlib.nullcontext())
    app = create_app(settings, store=store)
    folder = tmp_path / 'jobs' / 'shared-job'; folder.mkdir(parents=True)
    (folder / 'generation.mp4').write_bytes(b'shared video')
    (folder / 'receipt.json').write_text('private receipt')
    client = TestClient(app); client.headers.update(auth(settings))
    assert client.get('/api/jobs').json()['items'][0]['id'] == row['id']
    assert client.get('/api/jobs?all_users=false').json()['items'] == []
    assert client.get('/api/jobs/shared-job').status_code == 200
    assert client.get('/files/jobs/shared-job/generation.mp4').content == b'shared video'
    assert client.get('/files/jobs/shared-job/receipt.json').status_code == 404
    for action in ('cancel', 'retry', 'reprocess'):
        assert client.post('/api/jobs/shared-job/' + action).status_code == 403
    for route, body in [('analyze', {}), ('approve', {'profile': {'description': 'change'}}),
                        ('references', {'path': 'characters/source.png', 'label': 'change'})]:
        assert client.post('/api/characters/shared-job/' + route, json=body).status_code == 403
    assert client.delete('/api/characters/shared-job/references/ref').status_code == 403
    assert client.patch('/api/characters/shared-job/references/ref', json={'label': 'change'}).status_code == 403
    store.cancel.assert_not_called()
    assert client.post('/api/jobs/shared-job/cancel', headers=auth(settings, 'operator', 'admin')).status_code == 200
    assert client.post('/api/jobs/shared-job/cancel', headers=auth(settings, 'alice')).status_code == 200
    client.close()


def test_animation_borrowed_upload_snapshot_and_motion_analysis_do_not_rewrite_source(tmp_path):
    source = tmp_path / 'characters' / 'shared.png'; source.parent.mkdir()
    Image.new('RGB', (32, 32), 'blue').save(source)
    character = {'id': 'shared', 'name': 'Shared', 'owner_id': 'alice', 'owner_name': 'Alice',
                 'views': {'primary': 'characters/shared.png'}, 'profile': {}, 'approved': False, 'revision': 1}
    upload = {'id': 'additional', 'kind': 'character', 'owner_id': 'alice',
              'path': 'characters/shared.png', 'sha256': sha256(source)}
    store = Mock()
    store.get.side_effect = lambda table, identifier: copy.deepcopy(character if table == 'characters' else upload)
    service = Service(Settings(data_root=tmp_path), store, clients=Mock())
    task = AnimationTask(character_id='shared', reference_id='primary', prompt_mode='direct',
        video_prompt='Move', preview=True, additional_reference_upload_ids=['additional']).model_dump()
    snapshot = service.snapshot(task, owner={'owner_id': 'bob', 'owner_name': 'Bob'})
    assert snapshot['additional_references'][0]['upload_id'] == 'additional'
    service.clients.analyze_motion.return_value = {'description': 'A walk', 'video_sha256': 'sha'}
    motion = {'id': 'motion', 'video_path': 'shared.mp4', 'sha256': 'sha', 'metadata': {}, 'description': ''}
    service.motion_description({'motion': motion}, tmp_path / 'analysis')
    store.save_motion_analysis.assert_not_called()


def test_animation_worker_uses_saved_actor_and_hides_selection_from_public_job():
    worker = Worker.__new__(Worker)
    worker.store = Mock()
    seen = []
    worker.process = lambda job: seen.append(capture_model_context())
    context = {'user': {'id': 'alice', 'admin': False}, 'selections': {'animation': {'video': 'personal-video'}}}
    job = {'id': 'job', 'owner_id': 'alice', 'snapshot': {'model_context': context}}
    with model_context({'id': 'operator', 'admin': True}):
        worker.handle(job)
        assert capture_model_context()['user']['id'] == 'operator'
    assert len(seen) == 1 and seen[0]['user'] == context['user']
    assert seen[0]['selections']['animation']['video'] == 'personal-video'
    assert 'model_context' not in public_job(job)['snapshot']
    assert job['snapshot']['model_context'] == context
    broken = copy.deepcopy(job); broken['snapshot']['model_context']['user']['id'] = 'other'
    worker.handle(broken)
    assert len(seen) == 1
    assert worker.store.update_job.call_args.kwargs['status'] == 'failed'


def test_legacy_shared_upload_is_preserved_when_borrower_deletes_reference(tmp_path):
    source = tmp_path / 'characters' / 'uploads' / 'shared.png'; source.parent.mkdir(parents=True)
    source.write_bytes(b'original author upload')
    class Connection:
        def execute(self, sql, params=()):
            values = [{'id': 'upload', 'path': 'characters/uploads/shared.png', 'owner_id': 'alice'}] if 'FROM uploads' in sql else []
            return SimpleNamespace(fetchall=lambda: values)
    store = Store.__new__(Store); store.settings = SimpleNamespace(data_root=tmp_path)
    files, _, retained = store._deletion_files(Connection(), 'uploads',
        {'id': 'borrowed-ref', 'path': 'characters/uploads/shared.png', 'owner_id': 'bob'})
    assert files == [] and retained == ['characters/uploads/shared.png']
    assert source.read_bytes() == b'original author upload'


def test_private_model_details_are_hidden_from_shared_json_and_legacy_zip(tmp_path):
    settings = Settings(data_root=tmp_path, identity_secret='a' * 40)
    connection = {'id': 'PRIVATE-ID', 'name': 'PRIVATE-NAME', 'model': 'PRIVATE-MODEL',
                  'base_url': 'https://PRIVATE-HOST.invalid', 'scope': 'personal', 'protocol': 'rest'}
    metadata = {'owner_id': 'alice', 'versions': {'video_provider': connection, 'translator_model': 'PRIVATE-MODEL'},
                'task': {'video_prompt': 'A public wave'}, 'references': {'model_context': {'private': 'value'}}}
    folder = tmp_path / 'assets' / 'asset'; folder.mkdir(parents=True)
    (folder / 'metadata.json').write_text(json.dumps(metadata))
    (folder / 'h3_request.json').write_text(json.dumps({'model': 'PRIVATE-MODEL', 'prompt': 'A public wave'}))
    (folder / 'frame.png').write_bytes(b'public pixels')
    (folder / 'export.zip').write_bytes(b'PRIVATE-OLD-CACHE')
    row = {'id': 'asset', 'owner_id': 'alice', 'metadata': metadata}
    store = SimpleNamespace(get=lambda *args: copy.deepcopy(row), resource_guard=lambda *args: contextlib.nullcontext())
    client = TestClient(create_app(settings, store=store)); client.headers.update(auth(settings, 'operator', 'admin'))
    for route in ('/api/assets/asset', '/files/assets/asset/metadata.json', '/files/assets/asset/h3_request.json'):
        response = client.get(route)
        assert response.status_code == 200, response.text
        assert 'PRIVATE-' not in response.text and 'A public wave' in response.text
    for route in ('/api/assets/asset/export.zip', '/files/assets/asset/export.zip'):
        response = client.get(route)
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            checksums = json.loads(archive.read('checksums.json'))
            for name in archive.namelist():
                data = archive.read(name)
                assert b'PRIVATE-' not in data and b'model_context' not in data
                if name != 'checksums.json':
                    assert hashlib.sha256(data).hexdigest() == checksums[name]
    assert 'PRIVATE-' in (folder / 'metadata.json').read_text()
    client.close()


def test_admin_cannot_restart_another_members_personal_api_task(tmp_path):
    settings = Settings(data_root=tmp_path, identity_secret='a' * 40)
    job = {'id': 'job', 'owner_id': 'alice', 'owner_name': 'Alice', 'task': {},
           'snapshot': {'model_context': {'user': {'id': 'alice'}, 'selections': {'animation': {'image': 'private-image'}}}},
           'checkpoint': {}, 'status': 'failed'}
    store = SimpleNamespace(get=lambda *args: copy.deepcopy(job), retry_manual=Mock(return_value=job))
    app = create_app(settings, store=store)
    app.state.service.reprocess = Mock(return_value=job)
    app.state.service.snapshot = Mock(return_value={})
    app.state.service.require_generation_ready = Mock()
    client = TestClient(app)
    with patch('providers.resolve', side_effect=ValueError('private configuration is not visible')) as resolve:
        for action in ('retry', 'reprocess'):
            assert client.post('/api/jobs/job/' + action, headers=auth(settings, 'operator', 'admin')).status_code == 403
        store.retry_manual.assert_not_called()
        app.state.service.reprocess.assert_not_called()
    assert client.post('/api/jobs/job/retry', headers=auth(settings, 'alice')).status_code == 200
    client.close()

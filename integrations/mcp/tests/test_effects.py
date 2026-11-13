"""Offline effects identity, conflict-safe writes and bounded artifact exports."""
import asyncio
import hashlib
import json
from pathlib import Path
import struct
import zlib
from uuid import UUID

import httpx
import pytest

from fantasy_mcp.common import AdapterError
from fantasy_mcp.config import Settings
from fantasy_mcp.private import PrivateBackend
from fantasy_mcp.server import build_server
from test_server import result_data
from test_private import LoginFixture


OWNER = 'aed048d8-a838-4472-bc2d-69339cc99cea'
OTHER = '796f80c6-488c-4779-8367-5c6e7bb0c76f'
DATA = {'name': 'Spark', 'duration': 1, 'emitters': []}
RAW = json.dumps(DATA).encode()
REVISION = hashlib.sha256(RAW).hexdigest()


def png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\xff\xff\xff')) + chunk(b'IEND', b''))


class EffectsFixture:
    def __init__(self):
        self.requests = []
        self.owner = OWNER
        self.special = None
        self.login_count = 0
        self.jobs = {}
        self.job_count = 0
        self.poll_statuses = []

    def __call__(self, request):
        self.requests.append(request)
        if self.special:
            response = self.special(request)
            if response is not None:
                return response
        path = request.url.path
        if path == '/api/session':
            return httpx.Response(200, json={'user': {'id': self.owner, 'name': 'Agent', 'admin': False}, 'csrf': 'private-csrf'})
        assert request.headers['x-effects-owner'] == OWNER
        assert 'authorization' not in request.headers
        assert request.url.port == 18454
        if request.method == 'POST':
            assert request.headers['x-effects-csrf'] == 'private-csrf'
            assert request.headers['origin'] == 'https://fantasy.test:18454'
        if path == '/api/guide':
            return httpx.Response(200, json={'format': 'Effect JSON format', 'limits': {'maxEmitters': 16}})
        if path == '/api/list':
            return httpx.Response(200, json=['spark.json'])
        if path == '/api/effect':
            assert request.url.params['f'] == 'spark.json'
            return httpx.Response(200, content=RAW, headers={'etag': '"' + REVISION + '"', 'content-type': 'application/json'})
        if path == '/api/save':
            return httpx.Response(200, json={'ok': True, 'file': 'spark.json', 'revision': REVISION})
        if path == '/api/validate':
            return httpx.Response(200, json={'valid': True, 'errors': [], 'warnings': [], 'infos': []})
        if path == '/api/exports':
            assert request.method == 'POST'
            self.job_count += 1
            job_id = str(UUID(int=self.job_count))
            self.jobs[job_id] = {'id': job_id, 'status': 'running', 'created_at': '2026-10-30T00:00:00Z',
                                 **json.loads(request.content)}
            return httpx.Response(202, json={'job': self.jobs[job_id]})
        if path.startswith('/api/exports/'):
            job_id = path.split('/')[3]
            job = self.jobs.get(job_id)
            if job is None:
                return httpx.Response(404)
            if path.endswith('/cancel'):
                assert request.method == 'POST'
                if job['status'] == 'running':
                    job['status'] = 'cancelled'
                return httpx.Response(200, json={'job': job})
            if path.endswith('/release'):
                assert request.method == 'POST'
                if job['status'] == 'running':
                    return httpx.Response(409)
                del self.jobs[job_id]
                return httpx.Response(200, json={'ok': True})
            if not path.endswith('/result'):
                assert request.method == 'GET'
                if job['status'] == 'running':
                    job['status'] = self.poll_statuses.pop(0) if self.poll_statuses else 'done'
                return httpx.Response(200, json={'job': job})
            assert request.method == 'GET' and job['status'] == 'done'
            return httpx.Response(200, content=png(), headers={
                'content-type': 'image/png', 'x-effects-width': '1', 'x-effects-height': '1',
                'x-effects-frames': '1', 'x-effects-fps': '30'})
        raise AssertionError('Unexpected effects route')


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr('fantasy_mcp.effects.EXPORT_POLL_SECONDS', 0)
    transport = EffectsFixture()
    settings = Settings(portal_url='https://fantasy.test:18443', output_dir=tmp_path / 'out')
    backend = PrivateBackend(settings, httpx.MockTransport(transport))
    backend._authenticated = True
    async def login():
        if not backend._authenticated:
            transport.login_count += 1
            backend._authenticated = True
    backend._ensure_login = login
    yield backend, transport
    await backend.close()


async def test_effects_discovers_guide_lists_and_checks_read_revision(fixture):
    backend, transport = fixture
    assert backend.settings.module_url('effects') == 'https://fantasy.test:18454'
    assert (await backend.effects_guide())['format']
    assert await backend.effects_list() == {'effects': ['spark.json']}
    assert await backend.effects_read('spark.json') == {'file': 'spark.json', 'data': DATA, 'revision': REVISION}
    sessions = [r for r in transport.requests if r.url.path == '/api/session']
    assert 'x-effects-owner' not in sessions[0].headers
    assert all(r.headers['x-effects-owner'] == OWNER for r in sessions[1:])


async def test_effects_uses_existing_real_sso_flow_without_credentials_in_tools(tmp_path):
    config = Settings(portal_url='https://fantasy.test:18443', username='worker', password='test-password', output_dir=tmp_path)
    login, effects = LoginFixture(config), EffectsFixture()
    backend = PrivateBackend(config, httpx.MockTransport(lambda r: effects(r) if r.url.port == 18454 else login(r)))
    try:
        assert (await backend.effects_list())['effects'] == ['spark.json']
        assert login.password_posts == 1
        assert all(b'test-password' not in r.content for r in effects.requests)
        assert all('central-cookie' in r.headers.get('cookie', '') for r in effects.requests)
    finally:
        await backend.close()


async def test_owner_change_cannot_write_and_binding_survives_relogin(fixture):
    backend, transport = fixture
    await backend.effects_list()
    transport.owner = OTHER
    backend._authenticated = False
    with pytest.raises(AdapterError, match='account changed'):
        await backend.effects_save('spark', DATA)
    assert backend._effects_owner == OWNER
    assert not [r for r in transport.requests if r.method == 'POST']


async def test_save_defaults_to_create_only_and_edit_requires_explicit_revision(fixture):
    backend, transport = fixture
    await backend.effects_save('spark', DATA)
    body = json.loads(transport.requests[-1].content)
    assert body == {'name': 'spark', 'data': DATA, 'create_only': True}
    with pytest.raises(AdapterError, match='expected_revision'):
        await backend.effects_save('spark', DATA, overwrite=True)
    with pytest.raises(AdapterError, match='explicitly'):
        await backend.effects_save('spark', DATA, expected_revision=REVISION)
    await backend.effects_save('spark', DATA, overwrite=True, expected_revision=REVISION)
    assert json.loads(transport.requests[-1].content) == {'name': 'spark', 'data': DATA, 'expected_sha256': REVISION}


@pytest.mark.parametrize('filename', ['spark.json', 'a' * 239 + '.json'])
async def test_save_filename_is_normalized_once_before_http_and_length_check(fixture, filename):
    backend, transport = fixture
    await backend.effects_save(filename, DATA, overwrite=True, expected_revision=REVISION)
    body = json.loads(transport.requests[-1].content)
    assert body['name'] + '.json' == filename
    assert len((body['name'] + '.json').encode('utf-8')) <= 244
    assert body['expected_sha256'] == REVISION


async def test_revision_conflict_is_not_retried(fixture):
    backend, transport = fixture
    transport.special = lambda r: httpx.Response(409, json={'code': 'revision_conflict', 'error': 'private path'}) if r.url.path == '/api/save' else None
    with pytest.raises(AdapterError, match='reconcile') as error:
        await backend.effects_save('spark', DATA, True, REVISION)
    assert 'private path' not in str(error.value)
    assert len([r for r in transport.requests if r.url.path == '/api/save']) == 1


@pytest.mark.parametrize('etag', [None, 'W/"' + REVISION + '"', '"' + '0' * 64 + '"'])
async def test_invalid_or_mismatched_revision_prevents_edit_token(fixture, etag):
    backend, transport = fixture
    transport.special = lambda r: httpx.Response(200, content=RAW, headers={'etag': etag} if etag else {}) if r.url.path == '/api/effect' else None
    with pytest.raises(AdapterError, match='revision'):
        await backend.effects_read('spark.json')


@pytest.mark.parametrize('file', ['../x.json', '/x.json', 'x\\y.json', '.private.json', 'bad:name.json', 'x.json ', 'x.txt', '界' * 90 + '.json'])
async def test_effect_file_boundary_rejected_before_http(fixture, file):
    backend, transport = fixture
    with pytest.raises(AdapterError, match='filename'):
        await backend.effects_read(file)
    assert not transport.requests


@pytest.mark.parametrize('route', ['/api/session', '/api/list'])
async def test_expired_read_reauthenticates_only_once(fixture, route):
    backend, transport = fixture
    failures = 0
    def special(request):
        nonlocal failures
        if request.url.path == route and failures < 1:
            failures += 1
            return httpx.Response(401)
    transport.special = special
    assert (await backend.effects_list())['effects']
    assert transport.login_count == 1


async def test_persistent_401_read_stops_after_one_relogin(fixture):
    backend, transport = fixture
    transport.special = lambda r: httpx.Response(401)
    with pytest.raises(AdapterError, match='expired'):
        await backend.effects_list()
    assert transport.login_count == 1 and len(transport.requests) == 2


@pytest.mark.parametrize('failure', ['401', 'timeout'])
async def test_uncertain_write_never_retries(fixture, failure):
    backend, transport = fixture
    def special(request):
        if request.url.path == '/api/save':
            if failure == '401':
                return httpx.Response(401)
            raise httpx.ReadTimeout('private request data', request=request)
    transport.special = special
    with pytest.raises(AdapterError) as caught:
        await backend.effects_save('spark', DATA)
    assert 'private request data' not in str(caught.value)
    assert len([r for r in transport.requests if r.url.path == '/api/save']) == 1
    assert transport.login_count == 0


async def test_export_writes_complete_png_and_manifest_without_secrets(fixture):
    backend, transport = fixture
    result = await backend.effects_export('spark.json', 'atlas', subdirectory='test')
    artifact = result['artifacts'][0]
    assert Path(artifact['path']).read_bytes() == png()
    assert artifact['sha256'] == hashlib.sha256(png()).hexdigest()
    assert {k: artifact[k] for k in ('width', 'height', 'frames', 'fps')} == {'width': 1, 'height': 1, 'frames': 1, 'fps': 30}
    manifest = Path(result['manifest_path']).read_text()
    assert OWNER not in manifest and 'private-csrf' not in manifest
    assert json.loads(manifest)['job_id'] == result['job_id']
    assert result['server_cache_released'] is True
    assert not transport.jobs
    before = len(transport.requests)
    with pytest.raises(AdapterError, match='already exists'):
        await backend.effects_export('spark.json', 'atlas', subdirectory='test')
    assert len(transport.requests) == before


async def test_export_polls_without_one_long_http_request(fixture):
    backend, transport = fixture
    transport.poll_statuses = ['running'] * 15 + ['done']
    await backend.effects_export('spark.json', subdirectory='long-render')
    await backend.effects_guide()
    await backend.effects_validate(DATA)
    await backend.effects_save('spark', DATA)
    assert len([r for r in transport.requests if r.url.path == '/api/exports']) == 1
    assert len([r for r in transport.requests if r.method == 'GET' and r.url.path.startswith('/api/exports/')]) == 18
    assert not [r for r in transport.requests if r.url.path == '/api/export']
    for request in transport.requests:
        expected = 30 if request.url.path == '/api/session' else 180
        assert all(value == expected for value in request.extensions['timeout'].values())


@pytest.mark.parametrize('failure', ['html', 'oversize', 'truncated', 'bad_png', 'bad_metadata', 'redirect', '500'])
async def test_failed_export_never_creates_artifacts(fixture, failure):
    backend, transport = fixture
    def special(request):
        if not request.url.path.endswith('/result'):
            return None
        content = png()
        headers = {'content-type': 'image/png', 'x-effects-width': '1', 'x-effects-height': '1', 'x-effects-frames': '1', 'x-effects-fps': '30'}
        if failure == 'html': headers['content-type'] = 'text/html'
        if failure == 'oversize': headers['content-length'] = str(backend.settings.max_download_bytes + 1)
        if failure == 'truncated': headers['content-length'] = str(len(content) + 1)
        if failure == 'bad_png': content = b'not-png'
        if failure == 'bad_metadata': headers['x-effects-fps'] = 'nan'
        return httpx.Response(302 if failure == 'redirect' else 500 if failure == '500' else 200, content=content, headers=headers)
    transport.special = special
    with pytest.raises(AdapterError):
        await backend.effects_export('spark.json', subdirectory='failed')
    assert not list(backend.settings.output_dir.rglob('*.png'))
    assert not list(backend.settings.output_dir.rglob('*.json'))
    assert len([r for r in transport.requests if r.url.path == '/api/exports']) == 1
    assert not [r for r in transport.requests if r.url.path.endswith('/release')]


@pytest.mark.parametrize('raced_file', ['spark-frame.png', 'fantasy-effects-manifest.json'])
async def test_output_race_preserves_other_writer_and_rolls_back_only_own_file(fixture, raced_file):
    backend, transport = fixture
    target = backend.settings.output_dir / 'race' / raced_file
    def special(request):
        if request.url.path.endswith('/result'):
            target.write_bytes(b'other-writer')
    transport.special = special
    with pytest.raises(AdapterError, match='already exists'):
        await backend.effects_export('spark.json', subdirectory='race')
    assert target.read_bytes() == b'other-writer'
    assert sorted(p.name for p in target.parent.iterdir()) == [raced_file]


async def test_split_job_workflow_and_cleanup_preserve_source_effect(fixture):
    backend, transport = fixture
    started = await backend.effects_start_export('spark.json', 'frame', .5)
    job_id = started['job']['id']
    assert started['job']['status'] == 'running'
    assert (await backend.effects_get_export(job_id))['job']['status'] == 'done'
    result = await backend.effects_download_export(job_id, 'split')
    assert result['job_id'] == job_id and result['time'] == .5
    assert result['server_cache_released'] is True
    with pytest.raises(AdapterError, match='404'):
        await backend.effects_get_export(job_id)
    assert (await backend.effects_read('spark.json'))['data'] == DATA


async def test_running_job_cannot_download_but_can_be_cancelled(fixture):
    backend, transport = fixture
    started = await backend.effects_start_export('spark.json')
    job_id = started['job']['id']
    transport.poll_statuses = ['running']
    with pytest.raises(AdapterError, match='running'):
        await backend.effects_download_export(job_id, 'running')
    assert not [r for r in transport.requests if r.url.path.endswith('/result')]
    assert (await backend.effects_cancel_export(job_id))['job']['status'] == 'cancelled'
    assert (await backend.effects_get_export(job_id))['job']['status'] == 'cancelled'


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
async def test_terminal_failure_is_not_resubmitted(fixture, status):
    backend, transport = fixture
    transport.poll_statuses = [status]
    with pytest.raises(AdapterError, match=status) as error:
        await backend.effects_export('spark.json')
    assert str(UUID(int=1)) in str(error.value)
    assert len([r for r in transport.requests if r.url.path == '/api/exports']) == 1
    assert not [r for r in transport.requests if r.url.path.endswith('/result')]


@pytest.mark.parametrize('job_id', ['../escape', '/api/list', 'a?x', 'not-a-uuid', 'A' * 36, None])
async def test_job_id_boundary_is_checked_before_http(fixture, job_id):
    backend, transport = fixture
    for action in (backend.effects_get_export, backend.effects_cancel_export, backend.effects_download_export):
        with pytest.raises(AdapterError, match='UUID'):
            await action(job_id)
    assert not transport.requests


async def test_poll_connection_failure_retains_job_id_and_never_resubmits(fixture):
    backend, transport = fixture
    def special(request):
        if request.method == 'GET' and request.url.path.startswith('/api/exports/'):
            raise httpx.ReadTimeout('private network details', request=request)
    transport.special = special
    with pytest.raises(AdapterError) as error:
        await backend.effects_export('spark.json')
    assert str(UUID(int=1)) in str(error.value)
    assert 'private network details' not in str(error.value)
    assert len([r for r in transport.requests if r.url.path == '/api/exports']) == 1
    assert not [r for r in transport.requests if r.url.path.endswith('/cancel')]


@pytest.mark.parametrize('action', ['start', 'cancel'])
async def test_uncertain_export_writes_are_never_retried(fixture, action):
    backend, transport = fixture
    if action == 'cancel':
        job_id = (await backend.effects_start_export('spark.json'))['job']['id']
    def special(request):
        if (action == 'start' and request.url.path == '/api/exports'
                or action == 'cancel' and request.url.path.endswith('/cancel')):
            raise httpx.ReadTimeout('private network details', request=request)
    transport.special = special
    with pytest.raises(AdapterError) as error:
        if action == 'start':
            await backend.effects_start_export('spark.json')
        else:
            await backend.effects_cancel_export(job_id)
    assert 'private network details' not in str(error.value)
    if action == 'cancel':
        assert job_id in str(error.value)
        assert len([r for r in transport.requests if r.url.path.endswith('/cancel')]) == 1
    else:
        assert len([r for r in transport.requests if r.url.path == '/api/exports']) == 1


async def test_cancelled_convenience_call_cancels_one_known_job(fixture):
    backend, transport = fixture
    task = asyncio.create_task(backend.effects_export('spark.json'))
    while not transport.jobs:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.jobs[str(UUID(int=1))]['status'] == 'cancelled'
    assert len([r for r in transport.requests if r.url.path.endswith('/cancel')]) == 1


async def test_cleanup_failure_keeps_successful_files_and_job_id(fixture):
    backend, transport = fixture
    transport.special = lambda r: httpx.Response(503) if r.url.path.endswith('/release') else None
    result = await backend.effects_export('spark.json', subdirectory='complete')
    assert result['job_id'] in result['warning']
    assert Path(result['artifacts'][0]['path']).read_bytes() == png()
    assert json.loads(Path(result['manifest_path']).read_text())['job_id'] == result['job_id']
    assert result['job_id'] in transport.jobs
    assert len([r for r in transport.requests if r.url.path.endswith('/release')]) == 1


async def test_job_response_cannot_switch_identity_or_leak_arbitrary_fields(fixture):
    backend, transport = fixture
    job_id = (await backend.effects_start_export('spark.json'))['job']['id']
    transport.jobs[job_id]['token'] = 'server-secret'
    assert 'server-secret' not in json.dumps(await backend.effects_get_export(job_id))
    transport.jobs[job_id]['id'] = str(UUID(int=2))
    with pytest.raises(AdapterError, match='invalid export job'):
        await backend.effects_get_export(job_id)


async def test_mcp_tools_execute_against_account_bound_transport(fixture):
    backend, transport = fixture
    server = build_server(backend.settings, backend)
    guide = result_data(await server.call_tool('get_effects_guide', {}))
    assert guide['format']
    assert result_data(await server.call_tool('list_effects', {}))['effects'] == ['spark.json']
    read = result_data(await server.call_tool('read_effect', {'file': 'spark.json'}))
    assert read['revision'] == REVISION
    assert result_data(await server.call_tool('validate_effect', {'data': DATA}))['valid']
    assert result_data(await server.call_tool('save_effect', {'name': 'spark', 'data': DATA}))['ok']
    exported = result_data(await server.call_tool('export_effect', {'file': 'spark.json'}))
    assert Path(exported['manifest_path']).exists()
    job_id = result_data(await server.call_tool('start_effect_export', {'file': 'spark.json'}))['job']['id']
    assert result_data(await server.call_tool('get_effect_export', {'job_id': job_id}))['job']['status'] == 'done'
    downloaded = result_data(await server.call_tool('download_effect_export', {'job_id': job_id}))
    assert downloaded['server_cache_released'] is True
    job_id = result_data(await server.call_tool('start_effect_export', {'file': 'spark.json'}))['job']['id']
    assert result_data(await server.call_tool('cancel_effect_export', {'job_id': job_id}))['job']['status'] == 'cancelled'
    resources = await server.list_resources()
    assert 'fantasy://effects-usage' in {str(r.uri) for r in resources}


async def test_effects_cannot_be_used_as_generic_media_jobs(fixture):
    backend, transport = fixture
    with pytest.raises(AdapterError):
        await backend.submit_job('effects', 'any', {})
    with pytest.raises(AdapterError):
        await backend.list_jobs('effects')
    with pytest.raises(AdapterError):
        await backend.get_job('effects', 'any')
    assert not transport.requests

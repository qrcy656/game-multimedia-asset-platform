"""Community effects keep both CSRF boundaries and one account binding per MCP connection."""
import hashlib
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from fantasy_mcp.common import AdapterError
from fantasy_mcp.community import CommunityBackend
from fantasy_mcp.config import Settings
from fantasy_mcp.server import build_server
from test_effects import OWNER, OTHER, DATA, RAW, REVISION, png
from test_server import result_data


class CommunityEffectsFixture:
    def __init__(self):
        self.requests = []
        self.owner = OWNER
        self.logins = 0
        self.session_reads = 0
        self.csrf = 'outer-0'
        self.special = None
        self.jobs = {}
        self.job_count = 0

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == '127.0.0.1' and request.url.port == 28082
        assert request.headers['host'] == 'localhost:18082'
        assert 'authorization' not in request.headers
        path = request.url.path
        if path == '/api/login':
            assert json.loads(request.content) == {'username': 'agent', 'password': 'test-password'}
            assert request.headers['origin'] == 'http://localhost:18082'
            self.logins += 1
            return httpx.Response(200, json={'csrf': self.csrf, 'user': {'id': self.owner}},
                headers={'set-cookie': 'fantasy_community_session=community-test; HttpOnly; Path=/'})
        assert 'fantasy_community_session=community-test' in request.headers.get('cookie', '')
        if path == '/api/session':
            self.session_reads += 1
            self.csrf = f'outer-{self.session_reads}'
            return httpx.Response(200, json={'csrf': self.csrf, 'user': {'id': self.owner}})
        if path == '/api/workspace':
            return httpx.Response(200, json={'modules': {'effects': {'status': 'ready'}}})
        if not path.startswith('/effects/'):
            return httpx.Response(200, json={'models': [], 'status': 'ready'})
        if request.method != 'GET':
            assert request.headers['x-community-csrf'] == self.csrf
            assert request.headers['x-effects-csrf'] == 'inner-csrf'
            assert request.headers['origin'] == 'http://localhost:18082'
        if self.special:
            response = self.special(request)
            if response is not None:
                return response
        if path == '/effects/api/session':
            return httpx.Response(200, json={'user': {'id': self.owner}, 'csrf': 'inner-csrf', 'api_base': '/effects'})
        assert request.headers['x-effects-owner'] == OWNER
        if path == '/effects/api/guide':
            return httpx.Response(200, json={'format': 'Arcadia JSON', 'limits': {'render': {'timeoutMs': None}}})
        if path == '/effects/api/list':
            return httpx.Response(200, json=[{'file': 'spark.json'}])
        if path == '/effects/api/effect':
            assert request.url.params['f'] == 'spark.json'
            return httpx.Response(200, content=RAW, headers={'etag': '"' + REVISION + '"'})
        if path == '/effects/api/validate':
            return httpx.Response(200, json={'valid': True, 'errors': []})
        if path == '/effects/api/save':
            return httpx.Response(200, json={'ok': True, 'file': 'spark.json', 'revision': REVISION})
        if path == '/effects/api/exports':
            self.job_count += 1
            job_id = str(UUID(int=self.job_count))
            self.jobs[job_id] = {'id': job_id, 'status': 'running', **json.loads(request.content)}
            return httpx.Response(202, json={'job': self.jobs[job_id]})
        if path.startswith('/effects/api/exports/'):
            job_id = path.split('/')[4]
            job = self.jobs.get(job_id)
            if job is None:
                return httpx.Response(404)
            if path.endswith('/release'):
                del self.jobs[job_id]
                return httpx.Response(200, json={'ok': True})
            if path.endswith('/cancel'):
                job['status'] = 'cancelled'
                return httpx.Response(200, json={'job': job})
            if path.endswith('/result'):
                return httpx.Response(200, content=png(), headers={'content-type': 'image/png',
                    'x-effects-width': '1', 'x-effects-height': '1', 'x-effects-frames': '1', 'x-effects-fps': '30'})
            if job['status'] == 'running':
                job['status'] = 'done'
            return httpx.Response(200, json={'job': job})
        raise AssertionError(path)


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr('fantasy_mcp.effects.EXPORT_POLL_SECONDS', 0)
    transport = CommunityEffectsFixture()
    settings = Settings(edition='community', portal_url='http://127.0.0.1:28082',
        origin='http://localhost:18082', username='agent', password='test-password', output_dir=tmp_path)
    backend = CommunityBackend(settings, httpx.MockTransport(transport))
    yield backend, transport
    await backend.close()


async def test_discovery_and_same_origin_read_revision(fixture):
    backend, transport = fixture
    assert backend.settings.module_url('effects') == 'http://127.0.0.1:28082/effects'
    result = await backend.capabilities()
    assert result['modules']['effects']['available'] is True
    assert result['modules']['effects']['limits']['render']['timeoutMs'] is None
    assert (await backend.effects_list())['effects'] == [{'file': 'spark.json'}]
    assert await backend.effects_read('spark.json') == {'file': 'spark.json', 'revision': REVISION, 'data': DATA}
    assert transport.logins == 1
    assert all(b'test-password' not in r.content for r in transport.requests if r.url.path != '/api/login')


async def test_effects_writes_refresh_outer_csrf_and_keep_inner_owner_guard(fixture):
    backend, transport = fixture
    assert (await backend.effects_validate(DATA))['valid']
    await backend.effects_save('spark', DATA)
    await backend.effects_save('spark.json', DATA, True, REVISION)
    writes = [r for r in transport.requests if r.method == 'POST' and r.url.path.startswith('/effects/')]
    assert len(writes) == 3
    assert len({r.headers['x-community-csrf'] for r in writes}) == 3
    assert all(r.headers['x-effects-csrf'] == 'inner-csrf' and r.headers['x-effects-owner'] == OWNER for r in writes)
    assert json.loads(writes[1].content)['create_only'] is True
    assert json.loads(writes[2].content)['expected_sha256'] == REVISION


async def test_owner_binding_survives_expired_login_and_prevents_new_account_write(fixture):
    backend, transport = fixture
    await backend.effects_list()
    transport.owner = OTHER
    backend._authenticated = False
    with pytest.raises(AdapterError, match='account changed'):
        await backend.effects_save('spark', DATA)
    assert backend._effects_owner == OWNER and transport.logins == 2
    assert not [r for r in transport.requests if r.method == 'POST' and r.url.path.startswith('/effects/')]


async def test_effects_expired_get_reauthenticates_once_with_host_preserved(fixture):
    backend, transport = fixture
    failed = False
    def special(request):
        nonlocal failed
        if request.url.path == '/effects/api/session' and not failed:
            failed = True
            return httpx.Response(401)
    transport.special = special
    assert (await backend.effects_list())['effects']
    assert transport.logins == 2


@pytest.mark.parametrize('failure', [401, 403, 409, 'timeout'])
async def test_community_effects_mutation_never_replays(fixture, failure):
    backend, transport = fixture
    def special(request):
        if request.url.path == '/effects/api/save':
            if failure == 'timeout':
                raise httpx.ReadTimeout('sensitive-provider-error', request=request)
            return httpx.Response(failure, json={'error': 'sensitive-provider-error'})
    transport.special = special
    with pytest.raises(AdapterError) as error:
        await backend.effects_save('spark', DATA)
    assert 'sensitive-provider-error' not in str(error.value)
    assert len([r for r in transport.requests if r.url.path == '/effects/api/save']) == 1


async def test_async_export_download_manifest_and_cleanup_are_community_bound(fixture):
    backend, transport = fixture
    server = build_server(backend.settings, backend)
    job = result_data(await server.call_tool('start_effect_export', {'file': 'spark.json', 'format': 'atlas'}))['job']
    assert result_data(await server.call_tool('get_effect_export', {'job_id': job['id']}))['job']['status'] == 'done'
    result = result_data(await server.call_tool('download_effect_export', {'job_id': job['id'], 'subdirectory': 'test'}))
    assert result['edition'] == 'community' and result['module'] == 'effects'
    assert result['server_cache_released'] is True
    assert result['artifacts'][0]['sha256'] == hashlib.sha256(png()).hexdigest()
    assert Path(result['artifacts'][0]['path']).read_bytes() == png()
    manifest = Path(result['manifest_path']).read_text()
    assert json.loads(manifest)['edition'] == 'community'
    assert OWNER not in manifest and 'inner-csrf' not in manifest and 'outer-' not in manifest
    assert not transport.jobs
    assert len([r for r in transport.requests if r.url.path == '/effects/api/exports']) == 1


async def test_owner_change_before_export_download_creates_no_artifact(fixture):
    backend, transport = fixture
    job = (await backend.effects_start_export('spark.json'))['job']['id']
    transport.owner = OTHER
    with pytest.raises(AdapterError, match='account changed'):
        await backend.effects_download_export(job, 'changed-owner')
    assert not list(backend.settings.output_dir.rglob('*.png'))
    assert not [r for r in transport.requests if r.url.path.endswith('/result')]


async def test_community_effects_resources_and_cancel(fixture):
    backend, transport = fixture
    server = build_server(backend.settings, backend)
    assert len(await server.list_tools()) == 25
    assert 'fantasy://effects-usage' in {str(r.uri) for r in await server.list_resources()}
    job = (await backend.effects_start_export('spark.json'))['job']['id']
    assert result_data(await server.call_tool('cancel_effect_export', {'job_id': job}))['job']['status'] == 'cancelled'


async def test_effects_not_mistaken_for_api_model_jobs(fixture):
    backend, transport = fixture
    for action in [lambda: backend.submit_job('effects', None, {}), lambda: backend.list_jobs('effects'),
                   lambda: backend.get_job('effects', 'something')]:
        with pytest.raises(AdapterError):
            await action()
    assert not transport.requests

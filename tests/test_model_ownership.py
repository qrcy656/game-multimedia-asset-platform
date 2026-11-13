"""Model credentials, browser selections and worker identities stay separated."""
import asyncio
import json
import sqlite3
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from portal import create_app
from portal.registry import Registry, RegistryError, RegistryPermissionError
from providers import (capture_model_context, current_model_context, model_context,
                       parse_model_selection, restore_model_context, fingerprint)

ADMIN = {'id': 'admin-id', 'admin': True}
ALICE = {'id': 'alice-id', 'admin': False}
BOB = {'id': 'bob-id', 'admin': False}


def value(**extra):
    return {'name': 'Test', 'kind': 'llm', 'protocol': 'openai',
            'base_url': 'https://example.invalid/v1', 'model': 'text', 'api_key': 'secret-value', **extra}


def approved(registry, actor, **extra):
    with model_context(actor):
        item = registry.save_profile(value(**extra))
        with registry.connection() as db:
            db.execute('UPDATE profiles SET verified_revision=revision,capabilities=? WHERE id=?',
                       (json.dumps([item['kind']]), item['id']))
        return registry.get_profile(item['id'])


def test_personal_visibility_is_owner_only_including_administrators(tmp_path):
    registry = Registry(tmp_path)
    own = approved(registry, ALICE)
    shared = approved(registry, ADMIN, scope='global')
    for actor in (BOB, ADMIN, None):
        with model_context(actor):
            assert [p['id'] for p in registry.list_profiles()] == [shared['id']]
            for call in (lambda: registry.get_profile(own['id']),
                         lambda: registry.save_profile({'name': 'overwrite'}, own['id']),
                         lambda: registry.delete_profile(own['id']),
                         lambda: registry.resolve('knowledge', 'llm', own['id']),
                         lambda: asyncio.run(registry.verify(own['id']))):
                with pytest.raises(RegistryPermissionError):
                    call()
    with model_context(ALICE):
        assert {p['id'] for p in registry.list_profiles()} == {own['id'], shared['id']}
        assert 'secret-value' not in json.dumps(registry.list_profiles())
        assert registry.resolve('knowledge', 'llm', own['id'])['api_key'] == 'secret-value'
        assert registry.get_profile(own['id'])['can_manage']
        assert not registry.get_profile(shared['id'])['can_manage']
        with pytest.raises(RegistryError, match='未知字段'):
            registry.save_profile(value(owner_id=BOB['id']))
        with pytest.raises(RegistryError, match='创建后不能修改'):
            registry.save_profile({'scope': 'global'}, own['id'])


def test_global_writes_and_defaults_are_admin_only(tmp_path):
    registry = Registry(tmp_path)
    shared = approved(registry, ADMIN, scope='global')
    own = approved(registry, ADMIN)
    with model_context(ALICE):
        for call in (lambda: registry.save_profile(value(scope='global')),
                     lambda: registry.save_profile({'name': 'overwrite'}, shared['id']),
                     lambda: registry.bind('knowledge', 'llm', shared['id']),
                     lambda: registry.bind('knowledge', 'llm', None),
                     lambda: asyncio.run(registry.verify(shared['id']))):
            with pytest.raises(RegistryPermissionError):
                call()
    with model_context(ADMIN):
        with pytest.raises(RegistryError, match='全局默认配置'):
            registry.bind('knowledge', 'llm', own['id'])
        registry.bind('knowledge', 'llm', shared['id'])
    with model_context(ALICE):
        assert registry.resolve('knowledge', 'llm')['id'] == shared['id']


def test_browser_selection_is_ephemeral_typed_and_cannot_cross_owner(tmp_path):
    registry = Registry(tmp_path)
    global_model = approved(registry, ADMIN, scope='global')
    alice_model = approved(registry, ALICE)
    wrong_kind = approved(registry, ALICE, kind='embedding')
    with model_context(ADMIN):
        registry.bind('knowledge', 'llm', global_model['id'])
    with model_context(ALICE, {'knowledge': {'llm': alice_model['id']}}):
        assert registry.resolve('knowledge', 'llm')['id'] == alice_model['id']
        assert registry.list_bindings()[0]['profile_id'] == alice_model['id']
        assert registry.list_bindings(effective=False)[0]['profile_id'] == global_model['id']
    for actor in (ALICE, BOB):
        with model_context(actor):
            assert registry.resolve('knowledge', 'llm')['id'] == global_model['id']
    with model_context(BOB, {'knowledge': {'llm': alice_model['id']}}):
        with pytest.raises(RegistryPermissionError):
            registry.resolve('knowledge', 'llm')
        assert registry.list_bindings() == []
    with model_context(ALICE, {'knowledge': {'llm': wrong_kind['id']}}):
        with pytest.raises(RegistryError, match='能力与用途'):
            registry.resolve('knowledge', 'llm')
        with pytest.raises(RegistryError, match='能力与用途'):
            asyncio.run(registry.invoke('knowledge', 'llm', {}, wrong_kind['id']))
    with model_context(ALICE, {'knowledge': {'llm': None}}):
        with pytest.raises(RegistryError, match='未选择'):
            registry.resolve('knowledge', 'llm')


def test_worker_snapshot_freezes_defaults_and_checks_owner(tmp_path):
    registry = Registry(tmp_path)
    first = approved(registry, ADMIN, scope='global')
    second = approved(registry, ADMIN, scope='global')
    with model_context(ADMIN):
        registry.bind('knowledge', 'llm', first['id'])
    with patch('providers.registry', return_value=registry), model_context(ALICE):
        snapshot = capture_model_context()
    assert 'secret-value' not in json.dumps(snapshot)
    with model_context(ADMIN):
        registry.bind('knowledge', 'llm', second['id'])
    with restore_model_context(snapshot, owner_id=ALICE['id']):
        assert registry.resolve('knowledge', 'llm')['id'] == first['id']
        assert not current_model_context()['user']['admin']
    assert current_model_context() is None
    with pytest.raises(RegistryError, match='所属账号'):
        with restore_model_context(snapshot, owner_id=BOB['id']):
            pass
    with restore_model_context(None, owner_id=ALICE['id']):
        assert current_model_context()['user'] == ALICE


def test_concurrent_invocations_keep_their_actors_and_selected_keys(tmp_path):
    seen = []
    async def respond(request):
        await asyncio.sleep(0)
        seen.append(request.headers['authorization'])
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
    registry = Registry(tmp_path, transport=httpx.MockTransport(respond))
    a = approved(registry, ALICE, api_key='alice-secret')
    b = approved(registry, BOB, api_key='bob-secret')
    async def invoke(actor, item):
        with model_context(actor, {'knowledge': {'llm': item['id']}}):
            return await registry.invoke('knowledge', 'llm', {'messages': []})
    async def both():
        await asyncio.gather(invoke(ALICE, a), invoke(BOB, b))
    asyncio.run(both())
    assert sorted(seen) == ['Bearer alice-secret', 'Bearer bob-secret']
    with model_context(ADMIN):
        with pytest.raises(RegistryPermissionError):
            asyncio.run(registry.invoke('knowledge', 'llm', {'messages': []}, a['id']))
    assert len(seen) == 2


def test_frozen_invocation_survives_default_changes_and_rejects_modified_profile(tmp_path):
    seen = []
    def respond(request):
        seen.append(json.loads(request.content)['model'])
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]}]})
    registry = Registry(tmp_path, transport=httpx.MockTransport(respond))
    first = approved(registry, ADMIN, scope='global', kind='embedding', model='space-a')
    second = approved(registry, ADMIN, scope='global', kind='embedding', model='space-b')
    with model_context(ADMIN):
        registry.bind('knowledge', 'embedding', first['id'])
    with model_context(ALICE):
        frozen = registry.resolve('knowledge', 'embedding')
        digest = fingerprint(frozen)
    with model_context(ADMIN):
        registry.bind('knowledge', 'embedding', second['id'])
    with model_context(ALICE), patch('providers.registry', return_value=registry):
        import providers
        asyncio.run(providers.invoke('knowledge', 'embedding', {'input': ['a']}, first['id'], expected_fingerprint=digest))
    assert seen == ['space-a']
    with model_context(ADMIN):
        registry.save_profile({'model': 'different-space'}, first['id'])
        with registry.connection() as db:
            db.execute('UPDATE profiles SET verified_revision=revision,capabilities=? WHERE id=?',
                       (json.dumps(['embedding']), first['id']))
    with model_context(ALICE), patch('providers.registry', return_value=registry):
        with pytest.raises(RegistryError, match='配置已改变'):
            providers.invoke_sync('knowledge', 'embedding', {'input': ['a']}, first['id'], expected_fingerprint=digest)
    assert seen == ['space-a']


@pytest.mark.parametrize('raw', ['[]', '{', '{"knowledge":{"llm":5}}', '{"fake":{}}',
                                      '{"knowledge":{"image":"id"}}', 'x' * 8193])
def test_selection_header_rejects_bad_shape(raw):
    with pytest.raises(RegistryError):
        parse_model_selection(raw)


def test_legacy_registry_rows_migrate_to_global_without_changing_credentials(tmp_path):
    registry = Registry(tmp_path)
    legacy = registry.save_profile(value())
    with sqlite3.connect(registry.path) as db:
        db.execute('ALTER TABLE profiles DROP COLUMN scope')
        db.execute('ALTER TABLE profiles DROP COLUMN owner_id')
    migrated = Registry(tmp_path)
    with model_context(ALICE):
        assert migrated.get_profile(legacy['id'])['scope'] == 'global'
        assert migrated.get_profile(legacy['id'])['owner_id'] is None
    with migrated.connection() as db:
        row = db.execute('SELECT api_key FROM profiles WHERE id=?', (legacy['id'],)).fetchone()
    assert migrated.cipher.decrypt(row[0].encode()).decode() == 'secret-value'


def test_http_member_personal_admin_isolation_and_account_selection_guard(tmp_path):
    app = create_app(tmp_path)
    app.state.auth.create_user('alice', 'alice-pass123', 'Alice')
    with TestClient(app) as client:
        logged = client.post('/api/login', json={'username': 'alice', 'password': 'alice-pass123'}).json()
        headers = {'X-Community-CSRF': logged['csrf']}
        response = client.post('/api/profiles', headers=headers, json=value())
        assert response.status_code == 201
        item = response.json()
        assert item['scope'] == 'personal' and item['owner_id'] == logged['user']['id']
        assert client.post('/api/profiles', headers=headers, json=value(scope='global')).status_code == 403
        client.post('/api/logout', headers=headers, json={})
        admin = client.post('/api/login', json={'username': 'admin', 'password': '12345678'}).json()
        admin_headers = {'X-Community-CSRF': admin['csrf']}
        assert client.get('/api/profiles').json()['profiles'] == []
        assert client.put('/api/profiles/' + item['id'], headers=admin_headers, json={'name': 'stolen'}).status_code == 403
        assert client.get('/api/profiles', headers={'X-Fantasy-Model-Account': logged['user']['id']}).status_code == 409
        assert client.get('/api/profiles', headers={'X-Fantasy-Model-Selection': '[]'}).status_code == 400

"""Community owner selectors are forwarded without changing the authenticated actor."""
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from fantasy_mcp.common import AdapterError
from fantasy_mcp.effects import EffectsMixin


class Backend(EffectsMixin):
    def __init__(self, edition='community'):
        self.settings = SimpleNamespace(edition=edition)
        self.calls = []

    async def _effects_request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == '/api/effect':
            raw = json.dumps({'app': 'arcadia-effects', 'doc': {'name': 'Shared'}}).encode()
            return httpx.Response(200, content=raw, headers={'etag': '"' + hashlib.sha256(raw).hexdigest() + '"'})
        if path == '/api/exports':
            return httpx.Response(202, json={'job': {'id': '11111111-1111-4111-8111-111111111111',
                'status': 'running', 'file': 'shared.json', 'format': 'frame', 'time': None}})
        return httpx.Response(200, json={'ok': True})


@pytest.mark.asyncio
async def test_shared_read_export_and_explicit_admin_write_forward_owner_key():
    backend = Backend()
    owner = 'a' * 64
    result = await backend.effects_read('shared.json', owner_key=owner)
    assert result['owner_key'] == owner
    assert backend.calls[-1][2]['params'] == {'f': 'shared.json', 'owner_key': owner}
    await backend.effects_start_export('shared.json', owner_key=owner)
    assert backend.calls[-1][2]['json']['owner_key'] == owner
    await backend.effects_save('shared', result['data'], True, result['revision'], owner_key=owner)
    assert backend.calls[-1][2]['json']['owner_key'] == owner
    await backend.effects_save('my-copy', result['data'])
    assert 'owner_key' not in backend.calls[-1][2]['json']
    assert backend.calls[-1][2]['json']['create_only'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['../outside', 'a' * 63, '', 123])
async def test_invalid_content_owner_never_reaches_http(owner):
    backend = Backend()
    with pytest.raises(AdapterError):
        await backend.effects_read('shared.json', owner_key=owner)
    assert not backend.calls


@pytest.mark.asyncio
async def test_private_edition_cannot_silently_ignore_a_community_owner_selector():
    backend = Backend('private')
    with pytest.raises(AdapterError):
        await backend.effects_save('shared', {'doc': {}}, owner_key='a' * 64)
    assert not backend.calls

"""Shared sources retain author ownership; vector spaces follow model selection."""
import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch

import test_knowledge_parity as helpers
SECRET = helpers.SECRET


def headers(user='alice', role='user'):
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    now = int(time.time())
    claims = {'iss': 'fantasy-main', 'aud': 'knowledge-ques', 'sub': user,
              'name': user, 'role': role, 'iat': now, 'exp': now + 60}
    signed = encode(b'{"alg":"HS256","typ":"JWT"}') + '.' + encode(json.dumps(claims).encode())
    token = signed + '.' + encode(hmac.new(SECRET.encode(), signed.encode(), hashlib.sha256).digest())
    return {'Authorization': 'Bearer ' + token, 'Origin': 'http://testserver'}


class SharedKnowledge(unittest.TestCase):
    setUp = helpers.KnowledgeParity.setUp
    tearDown = helpers.KnowledgeParity.tearDown

    def create(self, path='alice.md', user='alice', role='user'):
        result = self.client.put('/api/file', headers=headers(user, role),
                                 json={'path': path, 'content': '# 设定\n原始设定。', 'create': True})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_members_manage_own_sources_but_cannot_mutate_shared_sources(self):
        document = self.create()
        read = self.client.get('/api/file', headers=headers('bob'), params={'path': document['path']})
        self.assertEqual(read.status_code, 200)
        self.assertFalse(read.json()['editable'])
        self.assertEqual(read.json()['owner_id'], 'alice')
        for method, endpoint, body in [
            ('PUT', '/api/file', {'path': 'alice.md', 'content': 'forbidden', 'expected_hash': document['hash']}),
            ('POST', '/api/move', {'path': 'alice.md', 'new_path': 'bob.md', 'expected_hash': document['hash']}),
            ('DELETE', '/api/file', {'path': 'alice.md', 'expected_hash': document['hash']}),
        ]:
            denied = self.client.request(method, endpoint, headers=headers('bob'), json=body)
            self.assertEqual(denied.status_code, 403, denied.text)
        own = self.client.put('/api/file', headers=headers(), json={
            'path': 'alice.md', 'content': '# 自己编辑', 'expected_hash': document['hash']})
        self.assertEqual(own.status_code, 200)
        removed = self.client.request('DELETE', '/api/file', headers=headers(),
                                      json={'path': 'alice.md', 'expected_hash': own.json()['hash']})
        self.assertEqual(removed.status_code, 200)

    def test_admin_edit_preserves_author_and_move_preserves_subtree_ownership(self):
        self.assertEqual(self.client.post('/api/directory', headers=headers(), json={'path': 'alice'}).status_code, 200)
        document = self.create('alice/world.md')
        self.assertEqual(self.client.post('/api/directory', headers=headers('bob'), json={'path': 'alice/bob'}).status_code, 403)
        self.assertEqual(self.client.put('/api/file', headers=headers('bob'),
                         json={'path': 'alice/bob.md', 'content': 'no', 'create': True}).status_code, 403)
        updated = self.client.put('/api/file', headers=headers('admin', 'admin'),
            json={'path': document['path'], 'content': '# 管理员编辑', 'expected_hash': document['hash']})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['owner_id'], 'alice')
        moved = self.client.post('/api/move', headers=headers(), json={'path': 'alice', 'new_path': 'renamed'})
        self.assertEqual(moved.status_code, 200, moved.text)
        read = self.client.get('/api/file', headers=headers(), params={'path': 'renamed/world.md'})
        self.assertEqual(read.json()['owner_id'], 'alice')
        self.assertTrue(read.json()['editable'])
        self.create('renamed/admin.md', 'admin', 'admin')
        blocked = self.client.post('/api/move', headers=headers(), json={'path': 'renamed', 'new_path': 'again'})
        self.assertEqual(blocked.status_code, 403)

    def test_existing_sources_are_admin_managed_and_never_claimed_by_first_reader(self):
        self.module.store.write('legacy.md', '# 已有资料', create=True)
        read = self.client.get('/api/file', headers=headers(), params={'path': 'legacy.md'}).json()
        self.assertIsNone(read['owner_id'])
        self.assertFalse(read['editable'])
        self.assertEqual(self.client.put('/api/file', headers=headers(), json={
            'path': 'legacy.md', 'content': '# 偷换', 'expected_hash': read['hash']}).status_code, 403)

    def test_embedding_selections_keep_independent_indexes_and_shared_sources_can_be_used(self):
        self.create()
        async def embed(texts, **kwargs):
            return [[1., 0.] for _ in texts]
        profiles = [{'id': key, 'kind': 'embedding', 'protocol': 'rest', 'model': key} for key in ['global-a', 'personal-b']]
        with patch.object(self.module, 'embed', side_effect=embed), patch.object(self.module, 'configured', return_value={'name': 'embedding'}):
            indexes = []
            for profile in profiles:
                with patch.object(self.module, 'resolve', return_value=profile):
                    response = self.client.post('/api/index', headers=headers('bob'), json={'paths': ['alice.md']})
                    self.assertEqual(response.status_code, 200, response.text)
                    selected = self.module.retrieval_store()
                    self.assertGreater(selected.stats()['chunks'], 0)
                    indexes.append(selected)
            self.assertNotEqual(indexes[0].db, indexes[1].db)
            self.assertGreater(indexes[0].stats()['chunks'], 0)
            self.assertEqual(self.module.store.read('alice.md')['content'], '# 设定\n原始设定。')

    def test_default_binding_changes_do_not_mix_or_erase_an_in_progress_index(self):
        self.create()
        self.module.store.write('many.md', '# 长设定\n' + ('风暴与城池。' * 3000), create=True)
        profiles = {key: {'id': key, 'kind': 'embedding', 'model': key} for key in ('a', 'b')}
        default = ['a']
        calls = []
        def resolve(module, role, profile_id=None):
            return profiles[profile_id or default[0]]
        async def invoke(module, role, payload, **kwargs):
            calls.append(kwargs)
            default[0] = 'b'
            return {'data': [{'index': i, 'embedding': [1., 0.]} for i, _ in enumerate(payload['input'])]}
        with patch.object(self.module, 'resolve', side_effect=resolve), patch.object(self.module, 'invoke', side_effect=invoke):
            selected = self.module.retrieval_store()
            result = self.client.post('/api/index', headers=headers(), json={'paths': ['alice.md', 'many.md']})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertGreater(len(calls), 2)
            self.assertTrue(all(call['profile_id'] == 'a' for call in calls))
            self.assertTrue(all(call['expected_fingerprint'] == self.module.fingerprint(profiles['a']) for call in calls))
            self.assertEqual(selected.stats()['indexed'], 2)
            self.assertEqual(self.module.retrieval_store().stats()['indexed'], 0)

"""Narrative parity without production accounts, projects, or AI services."""
import copy
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from narrative.api import ORIGIN, create_app
from narrative.auth import csrf_token, encode
from narrative.domain import ProjectStore, prepare_document, validate_document
from narrative.seed import blank, legacy_snow, snow


SECRET = 'isolated-narrative-parity-secret-' * 2


def headers(owner='alice', role='user'):
    now = int(time.time())
    claims = {'iss': 'fantasy-main', 'aud': 'fantasy-narrative', 'sub': owner,
              'name': owner, 'role': role, 'iat': now, 'exp': now + 60}
    message = encode(b'{"alg":"HS256","typ":"JWT"}') + '.' + encode(json.dumps(claims).encode())
    token = message + '.' + encode(hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest())
    return {'Authorization': 'Bearer ' + token, 'Origin': ORIGIN,
            'X-CSRF-Token': csrf_token(SECRET, owner)}


class NarrativeParity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='narrative-parity-')
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, SECRET)
        self.client = TestClient(self.app)
        self.client.headers.update(headers())

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_both_templates_create_editable_projects_without_play_state(self):
        for template, factory in [('blank', blank), ('snow', snow)]:
            with self.subTest(template=template):
                response = self.client.post('/api/projects', json={'title': '可编辑大纲', 'template': template})
                self.assertEqual(response.status_code, 201, response.text)
                value = response.json()
                self.assertEqual(value['document'], factory('可编辑大纲'))
                validate_document(value['document'])
                self.assertNotIn('variables', value['document'])
                self.assertNotIn('试玩', json.dumps(value['document'], ensure_ascii=False))
        self.assertEqual(len(self.client.get('/api/projects').json()['projects']), 2)

    def test_shared_reads_owner_writes_and_admin_preserve_ownership(self):
        original = self.client.post('/api/projects', json={'title': 'Alice 的示例', 'template': 'snow'}).json()
        url = '/api/projects/' + original['id']
        bob = headers('bob')
        self.assertEqual(len(self.client.get('/api/projects', headers=bob).json()['projects']), 1)
        self.assertEqual(self.client.get('/api/projects?scope=mine', headers=bob).json()['projects'], [])
        read = self.client.get(url, headers=bob)
        self.assertEqual(read.status_code, 200)
        self.assertTrue(read.json()['read_only'])
        self.assertEqual(read.json()['document'], original['document'])
        body = {'version': 1, 'document': {**original['document'], 'title': 'Edited'}}
        self.assertEqual(self.client.put(url, headers=bob, json=body).status_code, 403)
        self.assertEqual(self.client.delete(url + '?version=1', headers=bob).status_code, 403)
        self.assertEqual(self.client.get(url).json(), original)
        admin = headers('operator', 'admin')
        self.assertFalse(self.client.get(url, headers=admin).json()['read_only'])
        changed = self.client.put(url, headers=admin, json=body)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['owner_id'], 'alice')
        self.assertEqual(changed.json()['version'], 2)
        self.assertEqual(self.client.put(url, headers=admin, json=body).status_code, 409)
        self.assertEqual(self.client.delete(url + '?version=2', headers=admin).status_code, 200)

    def test_copy_and_substory_import_create_current_users_independent_content(self):
        document = snow('Alice 的完整剧情')
        image = io.BytesIO(); Image.new('RGB', (2, 2), 'blue').save(image, format='PNG')
        document['assets'].append({'id': 'reference', 'name': '场景图片', 'type': 'image',
            'mime': 'image/png', 'data': 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()})
        document['nodes'][0]['asset_id'] = 'reference'
        document['nodes'].append({'id': 'nested', 'board_id': 'main', 'type': 'substory',
            'position': {'x': 0, 'y': 800}, 'title': '完整子剧情', 'body': '', 'speaker': '',
            'color': '#54b7a7', 'asset_id': '', 'substory': snow('共享子剧情'),
            'dialogue': [], 'prerequisites': []})
        source = self.client.post('/api/import', json={'document': document}).json()
        bob = headers('bob')
        response = self.client.post('/api/projects/' + source['id'] + '/copy', headers=bob)
        self.assertEqual(response.status_code, 201, response.text)
        copied = response.json()
        self.assertEqual(copied['owner_id'], 'bob')
        self.assertNotEqual(copied['id'], source['id'])
        self.assertFalse(copied['read_only'])
        self.assertEqual(copied['document']['assets'], document['assets'])
        self.assertEqual(copied['document']['nodes'], document['nodes'])
        prepared = self.client.post('/api/import/prepare', headers=bob, json={'document': document['nodes'][-1]['substory']})
        self.assertEqual(prepared.status_code, 200)
        self.assertEqual(self.client.get('/api/projects/' + source['id']).json(), source)
        self.assertEqual(self.client.delete('/api/projects/' + copied['id'] + '?version=1', headers=bob).status_code, 200)
        self.assertEqual(self.client.get('/api/projects/' + source['id']).json(), source)

    def test_scope_is_a_listing_filter_for_all_members(self):
        project = self.client.post('/api/projects', json={'title': '范围检查', 'template': 'blank'}).json()
        for url in ['/api/projects', '/api/projects/' + project['id']]:
            for auth in [headers('alice'), headers('bob'), headers('operator', 'admin')]:
                self.assertEqual(self.client.get(url + '?scope=all', headers=auth).status_code, 200)
                for scope in ['', 'others', 'ALL', 'invalid']:
                    self.assertEqual(self.client.get(url + '?scope=' + scope, headers=auth).status_code, 422)

    def test_exact_legacy_example_import_uses_outline_and_prepare_is_non_mutating(self):
        response = self.client.post('/api/import/prepare', json={'document': legacy_snow()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['document'], snow())
        self.assertEqual(self.client.get('/api/projects').json()['projects'], [])
        imported = self.client.post('/api/import', json={'document': legacy_snow()})
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(imported.json()['document'], snow())

    def test_edited_legacy_example_is_never_replaced_with_template(self):
        for change in ['title', 'body', 'position']:
            with self.subTest(change=change):
                original = legacy_snow()
                if change == 'title':
                    original['title'] = '作者自己的项目'
                elif change == 'body':
                    original['nodes'][0]['body'] = '作者自己的开场'
                else:
                    original['nodes'][0]['position'] = {'x': 1234, 'y': -567}
                document = prepare_document(original)
                self.assertEqual(document['title'], original['title'])
                self.assertEqual(document['nodes'][0]['body'], original['nodes'][0]['body'])
                self.assertEqual(document['nodes'][0]['position'], original['nodes'][0]['position'])
                self.assertEqual(document['boards'], original['boards'])
                self.assertNotEqual(document, snow(original['title']))

    def test_existing_v1_database_migrates_once_preserving_project_identity(self):
        store = self.app.state.store
        source = legacy_snow()
        project_id = 'a' * 32
        encoded = json.dumps(source, ensure_ascii=False)
        with store.connect() as db:
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)',
                       (project_id, 'alice', source['title'], source['description'], 7,
                        '2026-10-13T00:00:00+00:00', len(source['nodes']), len(encoded.encode()), encoded))
            db.execute('PRAGMA user_version=1')
        migrated = ProjectStore(self.root).read('alice', project_id)
        self.assertEqual(migrated['id'], project_id)
        self.assertEqual(migrated['version'], 8)
        self.assertEqual(migrated['document'], snow())
        self.assertEqual(ProjectStore(self.root).read('alice', project_id), migrated)

    def test_dialogue_groups_conditions_four_handles_and_substory_round_trip(self):
        document = snow('主线与支线')
        for number, edge in enumerate(document['edges']):
            edge['source_handle'] = ['top', 'bottom', 'left', 'right'][number % 4]
            edge['target_handle'] = ['bottom', 'top', 'right', 'left'][number % 4]
        document['nodes'].append({'id': 'nested', 'board_id': 'main', 'type': 'substory',
            'position': {'x': 0, 'y': 800}, 'title': '新建的子剧情', 'body': '', 'speaker': '',
            'color': '#54b7a7', 'asset_id': '', 'substory': snow('嵌套剧情'),
            'dialogue': [], 'prerequisites': []})
        response = self.client.post('/api/import', json={'document': document})
        self.assertEqual(response.status_code, 201, response.text)
        value = response.json()
        saved = self.client.get('/api/projects/' + value['id']).json()['document']
        self.assertEqual(saved, document)
        dialogue = next(node for node in saved['nodes'] if node['id'] == 'wolf')
        self.assertEqual(len(dialogue['dialogue']), 2)
        self.assertEqual(dialogue['title'], '')
        self.assertEqual(dialogue['prerequisites'][0]['node_id'], 'station')
        self.assertEqual(saved['nodes'][-1]['substory'], snow('嵌套剧情'))
        changed = copy.deepcopy(document)
        changed['title'] = '修改名称'
        url = '/api/projects/' + value['id']
        self.assertEqual(self.client.put(url, json={'version': 1, 'document': changed}).status_code, 200)
        self.assertEqual(self.client.put(url, json={'version': 1, 'document': document}).status_code, 409)


if __name__ == '__main__':
    unittest.main()

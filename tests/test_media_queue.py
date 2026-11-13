"""API media lifecycle regressions. All inference providers are simulated."""
import base64
import copy
import io
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
from PIL import Image
import pytest

from media import catalog, queue as queue_module, remote
from media.comfy_workflow import ComfyError, SubmissionUncertain
from media.queue import ApiQueue, QueueError


OWNER = {'id': 'member-1', 'name': 'Member'}


@pytest.fixture
def profiles(monkeypatch):
    values = {}
    for kind in ('image', 'workflow', 'speech', 'music', 'model3d'):
        values[kind] = {'id': kind + '-profile', 'name': kind + ' provider', 'kind': kind,
            'protocol': 'comfyui' if kind == 'workflow' else 'rest', 'model': 'model-a',
            'base_url': 'http://provider.test/v1', 'api_key': 'test-credential', 'options': {}, 'verified': True}
    def resolve(module, role, profile_id=None):
        profile = copy.deepcopy(values[role])
        if profile_id and profile_id != profile['id']:
            raise ValueError('missing profile')
        return profile
    registry = Mock()
    registry.list_profiles.return_value = list(values.values())
    registry.list_bindings.return_value = []
    monkeypatch.setattr(catalog, 'registry', lambda: registry)
    monkeypatch.setattr(catalog, 'resolve', resolve)
    monkeypatch.setattr(queue_module, 'resolve', resolve)
    return values


@pytest.fixture
def queue(tmp_path, profiles, monkeypatch):
    instance = ApiQueue(tmp_path)
    monkeypatch.setattr(instance.quit, 'wait', lambda seconds: False)
    return instance


def payload(kind='image', **changes):
    params = {'prompt': 'A castle'}
    if kind == 'workflow':
        params = {'workflow': json.dumps({'1': {'class_type': 'EmptyImage', 'inputs': {'width': 256, 'height': 256, 'batch_size': 1, 'color': 0}},
                    '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': 'test'}}}), 'bindings': '{}'}
    elif kind == 'speech':
        params = {'text': '  原台词\n不能改  ', 'instruct': '轻柔'}
    return {'module': 'image' if kind == 'workflow' else kind, 'model_id': kind + '-profile',
            'owner': OWNER, 'params': params, **changes}


def scope(module='image'):
    return {'module': module, 'owner': OWNER['id'], 'actor': OWNER}


def png():
    output = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(output, format='PNG')
    return output.getvalue()


def image_result(**changes):
    return {'artifacts': [{'data_base64': base64.b64encode(png()).decode()}], **changes}


def fake_client(monkeypatch, handle):
    original = httpx.Client
    monkeypatch.setattr(remote.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))


def test_shared_jobs_allow_reads_but_only_creator_or_admin_can_mutate(queue):
    created = queue.create(payload())
    other = {'module': 'image', 'owner': 'other-member', 'admin': '0'}
    assert queue.handle('GET', '/jobs/' + created['id'], query=other)['id'] == created['id']
    assert [item['id'] for item in queue.handle('GET', '/jobs', query=other)['jobs']] == [created['id']]
    assert queue.handle('GET', '/jobs', query={**other, 'scope': 'mine'})['jobs'] == []
    for action in ('cancel', 'delete'):
        with pytest.raises(QueueError) as error:
            queue.handle('POST', '/jobs/' + created['id'] + '/' + action, other)
        assert error.value.status == 403
    assert queue.raw(created['id'])['status'] == 'queued'
    admin = {**other, 'admin': '1'}
    cancelled = queue.handle('POST', '/jobs/' + created['id'] + '/cancel', admin)
    assert cancelled['owner']['id'] == OWNER['id']
    assert cancelled['status'] == 'cancelled'
    assert queue.handle('POST', '/jobs/' + created['id'] + '/delete', admin)['deleted']


def test_borrowed_job_retry_has_new_owner_context_and_independent_input(queue, tmp_path):
    from providers import model_context
    source = tmp_path / 'original.png'; source.write_bytes(png())
    original = queue.create(payload(), source=source)
    queue._update(original['id'], status='failed')
    before = queue.raw(original['id'])
    actor = {'id': 'other-member', 'name': 'Member', 'admin': False}
    with model_context(actor, {'image': {'image': 'image-profile'}}):
        copied = queue.handle('POST', '/jobs/' + original['id'] + '/retry',
                              {'module': 'image', 'owner': actor['id'], 'actor': actor})
    assert copied['id'] != original['id'] and copied['owner']['id'] == actor['id']
    assert 'model_context' not in copied
    stored = queue.raw(copied['id'])
    assert stored['model_context']['user']['id'] == actor['id']
    assert queue.raw(original['id']) == before
    own_input = queue.folder(stored) / stored['input']
    assert own_input.read_bytes() == png()
    queue.handle('POST', '/jobs/' + original['id'] + '/delete', scope())
    assert own_input.read_bytes() == png()


def test_media_worker_restores_frozen_model_context_instead_of_current_actor(queue, monkeypatch):
    from providers import capture_model_context, model_context
    with model_context(OWNER, {'image': {'image': 'image-profile'}}):
        created = queue.create(payload())
    seen = []
    monkeypatch.setattr(queue, '_execute', lambda job: seen.append(capture_model_context()))
    with model_context({'id': 'unrelated', 'admin': True}, {'image': {'image': None}}):
        queue.execute(queue.raw(created['id']))
        assert capture_model_context()['user']['id'] == 'unrelated'
    assert seen[0]['user']['id'] == OWNER['id']
    assert seen[0]['selections']['image']['image'] == 'image-profile'
    bad = queue.raw(created['id']); bad['model_context']['user']['id'] = 'unrelated'
    queue.execute(bad)
    assert len(seen) == 1 and queue.raw(created['id'])['status'] == 'failed'


def test_shared_media_never_exposes_personal_connection_even_to_admin(queue, profiles):
    from providers import model_context
    profiles['image']['scope'] = 'personal'
    with model_context(OWNER):
        created = queue.create(payload())
        assert created['model_id'] == 'image-profile'
    stored = queue.raw(created['id'])
    stored['translation'] = {'scope': 'personal', 'profile_id': 'PRIVATE-LLM-ID', 'model': 'PRIVATE-MODEL',
                             'provider': 'PRIVATE-PROTOCOL', 'original': 'A public instruction', 'translated': 'A public result'}
    queue.put(stored)
    for actor in ({'id': 'member-2', 'admin': False}, {'id': 'operator', 'admin': True}):
        with model_context(actor):
            public = queue.public(stored)
            assert 'model_id' not in public and public['model_label'] == '个人模型'
            assert 'PRIVATE-' not in json.dumps(public)
            assert public['translation']['translated'] == 'A public result'
    with model_context(OWNER):
        assert queue.public(stored)['translation']['model'] == 'PRIVATE-MODEL'


def test_preview_allows_reference_binding_without_upload_but_submit_requires_it(queue):
    request = payload('workflow')
    request['params']['workflow'] = json.dumps({'1': {'class_type': 'LoadImage', 'inputs': {'image': 'ignored.png'}},
        '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': 'test'}}})
    request['params']['bindings'] = '{"image":{"node":"1","field":"image"}}'
    assert queue.compile(request)['prompt_mode'] == 'direct'
    with pytest.raises(ValueError, match='参考图'):
        queue.create(request)


def test_comfy_dimensions_and_subsecond_audio_preserve_private_parameters(queue):
    request = payload('workflow')
    request['params'].update(width=72, height=4096)
    assert queue.compile(request)['params']['width'] == 72
    assert queue.compile(request)['params']['height'] == 4096
    request['params']['width'] = 65
    with pytest.raises(ValueError, match='步长'):
        queue.compile(request)
    audio = payload('music', params={'prompt': 'A short footstep', 'duration': 1.5})
    assert queue.compile(audio)['params']['duration'] == 1.5


@pytest.mark.parametrize('key,value', [('color', float('nan')), ('color', float('inf')), ('width', 4097), ('height', 0), ('steps', 151), ('steps', 0)])
def test_comfy_workflow_preserves_private_numeric_guards(queue, key, value):
    request = payload('workflow')
    graph = json.loads(request['params']['workflow'])
    graph['1']['inputs'][key] = value
    request['params']['workflow'] = json.dumps(graph)
    with pytest.raises(ComfyError):
        queue.compile(request)


def test_comfy_conflict_is_unknown_and_never_automatically_reposts(profiles, monkeypatch):
    fake_client(monkeypatch, lambda request: httpx.Response(409, json={'error': 'provider private detail'}))
    client = remote.ConfiguredComfy(profiles['workflow'])
    with pytest.raises(SubmissionUncertain):
        client.request('/prompt', {'prompt': {'1': {}}})
    import urllib.error
    from media.comfy_workflow import ComfyClient
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError('http://provider.test/prompt', 409, 'Conflict', {}, None)
    monkeypatch.setattr('media.comfy_workflow.urllib.request.build_opener', Mock(return_value=opener))
    with pytest.raises(SubmissionUncertain):
        ComfyClient('http://provider.test').request('/prompt', {'prompt': {'1': {}}})


def test_direct_dialogue_and_lyrics_are_exact_and_no_translation(queue, monkeypatch):
    llm = Mock(side_effect=AssertionError('no LLM in direct mode'))
    monkeypatch.setattr(queue_module, 'invoke_sync', llm)
    request = payload('speech')
    result = queue.compile(request)
    assert result['params']['text'] == request['params']['text']
    music = payload('music', params={'prompt': '森林', 'lyrics': '  原歌词\n  '})
    assert queue.compile(music)['params']['lyrics'] == music['params']['lyrics']
    llm.assert_not_called()


def test_music_tag_translation_keeps_lyrics_verbatim_and_works_without_prompt(queue, profiles, monkeypatch):
    profiles['llm'] = {'id': 'translator', 'model': 'translator', 'protocol': 'rest'}
    llm = Mock(return_value={'choices': [{'message': {'content': '{"description":"quiet strings"}'}}]})
    monkeypatch.setattr(queue_module, 'invoke_sync', llm)
    request = payload('music', params={'tags': '安静的弦乐', 'lyrics': '  原歌词\n保持原文  '}, prompt_mode='assisted')
    compiled = queue.compile(request)
    assert compiled['params']['tags'] == 'quiet strings'
    assert compiled['params']['lyrics'] == request['params']['lyrics']
    assert compiled['translation']['field'] == 'tags'
    assert '原歌词' not in json.dumps(llm.call_args.args[2], ensure_ascii=False)


def test_submission_timeout_blocks_retry_and_delete_across_restart(queue, monkeypatch):
    job = queue.create(payload())
    monkeypatch.setattr(queue_module, 'submit', Mock(side_effect=SubmissionUncertain('unknown POST')))
    queue.execute(job)
    saved = queue.raw(job['id'])
    assert saved['status'] == 'running' and saved['submission_uncertain']
    for action in ('retry', 'delete'):
        with pytest.raises(QueueError):
            queue.handle('POST', '/jobs/' + job['id'] + '/' + action, scope())
    restarted = ApiQueue(queue.root)
    assert restarted.raw(job['id'])['status'] == 'running'
    assert restarted.raw(job['id'])['submission_uncertain']


def test_definitive_rejection_is_failed_and_has_no_unknown_flag(queue, monkeypatch):
    job = queue.create(payload())
    monkeypatch.setattr(queue_module, 'submit', Mock(side_effect=remote.SubmissionRejected('HTTP 401')))
    queue.execute(job)
    assert queue.raw(job['id'])['status'] == 'failed'
    assert queue.raw(job['id'])['submission_uncertain'] is False


def test_restart_with_remote_id_resumes_polling_without_second_post(queue, monkeypatch):
    job = queue.create(payload())
    saved = queue.raw(job['id'])
    saved.update(status='running', submission_started=True, remote_id='remote-1')
    queue.put(saved)
    restarted = ApiQueue(queue.root)
    monkeypatch.setattr(restarted.quit, 'wait', lambda seconds: False)
    post = Mock(side_effect=AssertionError('must not resubmit'))
    monkeypatch.setattr(queue_module, 'submit', post)
    poll = Mock(return_value=image_result(id='remote-1', status='completed'))
    monkeypatch.setattr(queue_module, 'request_json', poll)
    restarted.execute(restarted.raw(job['id']))
    assert restarted.raw(job['id'])['status'] == 'succeeded'
    post.assert_not_called()
    assert poll.call_args.args[1] == '/jobs/remote-1'


def test_empty_pending_artifacts_still_poll_and_download(queue, monkeypatch):
    job = queue.create(payload())
    monkeypatch.setattr(queue_module, 'submit', Mock(return_value={'id': 'remote-1', 'status': 'queued', 'artifacts': []}))
    poll = Mock(return_value=image_result(id='remote-1', status='completed'))
    monkeypatch.setattr(queue_module, 'request_json', poll)
    queue.execute(job)
    assert queue.raw(job['id'])['status'] == 'succeeded'
    poll.assert_called_once()


@pytest.mark.parametrize('identifier', ['../secret', 'a/b', 'a%2fb', 'a?x=y', 'x' * 161])
def test_invalid_remote_ids_never_become_poll_paths(queue, monkeypatch, identifier):
    job = queue.create(payload())
    monkeypatch.setattr(queue_module, 'submit', Mock(return_value={'id': identifier, 'status': 'queued'}))
    poll = Mock(side_effect=AssertionError('invalid path must not be used'))
    monkeypatch.setattr(queue_module, 'request_json', poll)
    queue.execute(job)
    assert queue.raw(job['id'])['submission_uncertain']
    poll.assert_not_called()


def test_comfy_ambiguous_post_recovers_by_client_without_reposting(queue, monkeypatch):
    job = queue.create(payload('workflow'))
    client = Mock()
    client.submit.side_effect = SubmissionUncertain('timeout')
    client.recover_submission.return_value = 'found-id'
    client.history.return_value = {'status': {'status_str': 'success', 'completed': True}}
    client.download.return_value = [{'name': 'test.png', 'kind': 'image', 'size': 100}]
    monkeypatch.setattr(queue_module, 'ConfiguredComfy', lambda profile: client)
    queue.execute(job)
    assert queue.raw(job['id'])['status'] == 'succeeded'
    client.submit.assert_called_once()
    client.recover_submission.assert_called_once_with(job['id'])


def test_comfy_restart_resumes_history_and_does_not_upload_or_post(queue, monkeypatch):
    job = queue.create(payload('workflow'))
    saved = queue.raw(job['id'])
    saved.update(status='running', submission_started=True, remote_id='saved-id')
    queue.put(saved)
    restarted = ApiQueue(queue.root)
    monkeypatch.setattr(restarted.quit, 'wait', lambda seconds: False)
    client = Mock()
    client.history.return_value = {'status': {'status_str': 'error', 'completed': False}}
    monkeypatch.setattr(queue_module, 'ConfiguredComfy', lambda profile: client)
    restarted.execute(restarted.raw(job['id']))
    assert restarted.raw(job['id'])['status'] == 'failed'
    client.submit.assert_not_called()
    client.upload.assert_not_called()


def test_comfy_unknown_without_matching_history_stays_protected(queue, monkeypatch):
    job = queue.create(payload('workflow'))
    saved = queue.raw(job['id'])
    saved.update(submission_started=True)
    queue.put(saved)
    client = Mock()
    client.recover_submission.return_value = None
    monkeypatch.setattr(queue_module, 'ConfiguredComfy', lambda profile: client)
    queue.execute(queue.raw(job['id']))
    assert queue.raw(job['id'])['submission_uncertain']
    client.submit.assert_not_called()


def test_cancelled_known_remote_job_stays_tracked_and_not_deletable(queue, monkeypatch):
    job = queue.create(payload())
    saved = queue.raw(job['id'])
    saved.update(status='running', submission_started=True, remote_id='known')
    queue.put(saved)
    result = queue.handle('POST', '/jobs/' + job['id'] + '/cancel', scope())
    assert result['status'] == 'queued' and result['cancel_requested']
    with pytest.raises(QueueError):
        queue.handle('POST', '/jobs/' + job['id'] + '/delete', scope())


@pytest.mark.parametrize('protocol,kind', [('rest', 'image'), ('openai', 'image'), ('openai', 'speech')])
def test_transport_timeout_is_unknown_for_all_generation_posts(profiles, monkeypatch, protocol, kind):
    profile = {**profiles[kind], 'protocol': protocol}
    def handle(request):
        raise httpx.ReadTimeout('provider accepted but disconnected', request=request)
    fake_client(monkeypatch, handle)
    params = {'prompt': 'castle', 'width': 256, 'height': 256} if kind == 'image' else {'text': 'test', 'speaker': 'alloy', 'instruct': ''}
    with pytest.raises(SubmissionUncertain):
        remote.submit(kind, kind, profile, params, None)


@pytest.mark.parametrize('status,exception', [(400, remote.SubmissionRejected), (401, remote.SubmissionRejected),
    (408, SubmissionUncertain), (429, SubmissionUncertain), (500, SubmissionUncertain)])
def test_http_rejection_and_unknown_outcomes_are_distinguished(profiles, monkeypatch, status, exception):
    fake_client(monkeypatch, lambda request: httpx.Response(status, json={'error': 'secret provider detail'}))
    with pytest.raises(exception) as caught:
        remote.submit('image', 'image', profiles['image'], {'prompt': 'castle'}, None)
    assert 'secret provider detail' not in str(caught.value)


def test_invalid_success_json_and_empty_success_are_unknown(profiles, monkeypatch):
    fake_client(monkeypatch, lambda request: httpx.Response(200, json={}))
    with pytest.raises(SubmissionUncertain):
        remote.submit('image', 'image', profiles['image'], {'prompt': 'castle'}, None)


def test_download_never_sends_key_to_cdn_and_never_follows_redirects(profiles, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'http://unrelated.test/steal'}) if request.url.path.endswith('/redirect') else httpx.Response(200, content=png())
    fake_client(monkeypatch, handle)
    remote.fetch_bytes(profiles['image'], 'http://cdn.test/asset.png')
    assert 'authorization' not in requests[-1].headers
    remote.fetch_bytes(profiles['image'], '/v1/file.png')
    assert requests[-1].headers['authorization'] == 'Bearer test-credential'
    with pytest.raises(ValueError):
        remote.fetch_bytes(profiles['image'], '/redirect')
    assert len(requests) == 3


@pytest.mark.parametrize('result', [{}, {'artifacts': []}, {'data': []}, None, {'artifacts': [{'url': 'file:///etc/passwd'}]}])
def test_missing_or_unsafe_artifacts_are_not_success(profiles, tmp_path, result):
    with pytest.raises(ValueError):
        remote.ingest(profiles['image'], 'image', result, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_size_cap_and_glb_header_validation(profiles, monkeypatch):
    fake_client(monkeypatch, lambda request: httpx.Response(200, content=b'x' * 11))
    with pytest.raises(ValueError, match='大小'):
        remote.fetch_bytes(profiles['image'], '/large', maximum=10)
    with pytest.raises(ValueError):
        remote.artifact_type(b'glTF' + b'\x00' * 20, 'model3d')


def test_comfy_queue_and_history_accept_empty_success_body(profiles, monkeypatch):
    fake_client(monkeypatch, lambda request: httpx.Response(200, content=b''))
    client = remote.ConfiguredComfy(profiles['workflow'])
    assert client.request('/queue', {'delete': ['owned-id']}) == {}
    assert client.request('/history', {'delete': ['owned-id']}) == {}
    with pytest.raises(SubmissionUncertain):
        client.request('/prompt', {'prompt': {'1': {}}})


def test_rest_reference_matches_documented_file_object(profiles, tmp_path):
    source = tmp_path / 'reference.png'
    source.write_bytes(png())
    data = remote.generation_payload(profiles['image'], {'prompt': 'test'}, source)
    assert data['input']['image'] == {'mime_type': 'image/png', 'data_base64': base64.b64encode(png()).decode()}


# Community/private parity: remote training and independent API concurrency.
def audio_zip(path, name='sample.wav'):
    import zipfile
    import wave
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
        output.writeframes(b'\0\0' * 800)
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr(name, audio.getvalue())
    if '\\' in name:
        # Windows ZipInfo normalizes its native separator; restore the hostile
        # central-directory and local-header spelling used by a foreign ZIP.
        path.write_bytes(path.read_bytes().replace(name.replace('\\', '/').encode(), name.encode()))
    return path


def weight_bytes():
    import struct
    header = json.dumps({'weight': {'dtype': 'F32', 'shape': [1], 'data_offsets': [0, 4]}}).encode()
    return struct.pack('<Q', len(header)) + header + b'\0' * 4


def training_payload(**changes):
    return payload('music', params={'task': 'train', 'prompt': 'Music style', 'epochs': 2,
        'rank': 8, 'learning_rate': 0.0001, 'duration': 30}, owner={**OWNER, 'admin': True}, **changes)


def test_training_requires_verified_capability_and_zip_for_all_members(queue, profiles, tmp_path):
    source = audio_zip(tmp_path / 'audio.zip')
    with pytest.raises(ValueError):
        queue.create(training_payload(), source)
    profiles['music']['capabilities'] = ['music', 'music_train']
    request = training_payload()
    request['owner'] = OWNER
    member_job = queue.create(request, source)
    assert member_job['owner']['id'] == OWNER['id']
    with pytest.raises(ValueError, match='ZIP'):
        queue.create(training_payload())
    job = queue.create(training_payload(), source)
    assert job['params']['learning_rate'] == 0.0001
    assert job['params']['rank'] == 8
    with pytest.raises(QueueError, match='不调用翻译'):
        queue.create(training_payload(prompt_mode='assisted'), source)


@pytest.mark.parametrize('name', ['../sample.wav', '/sample.wav', 'C:/sample.wav', 'nested\\sample.wav', 'script.py'])
def test_training_dataset_rejects_unsafe_paths_and_non_audio(tmp_path, name):
    from media.training import validate_dataset
    with pytest.raises(ValueError):
        validate_dataset(audio_zip(tmp_path / 'bad.zip', name))


def test_training_transfers_zip_bytes_archives_weights_and_deletes_complete_set(queue, profiles, tmp_path, monkeypatch):
    profiles['music']['capabilities'] = ['music', 'music_train']
    source = audio_zip(tmp_path / 'audio.zip')
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'artifacts': [{'data_base64': base64.b64encode(weight_bytes()).decode()}]})
    fake_client(monkeypatch, handle)
    job = queue.create(training_payload(), source)
    queue.execute(job)
    saved = queue.raw(job['id'])
    assert saved['status'] == 'succeeded'
    assert saved['artifacts'][0]['name'].endswith('.safetensors')
    assert requests[0]['kind'] == 'music'
    assert requests[0]['input']['task'] == 'train'
    assert base64.b64decode(requests[0]['input']['dataset']['data_base64']) == source.read_bytes()
    assert 'image' not in requests[0]['input'] and 'lyrics' not in requests[0]['input']
    folder = queue.folder(saved)
    assert (folder / saved['input']).is_file()
    queue.handle('POST', '/jobs/' + job['id'] + '/delete', scope('music'))
    assert not folder.exists()


def test_training_rejects_pickle_and_zip_with_executable(tmp_path):
    import zipfile
    from media.training import training_artifact
    with pytest.raises(ValueError):
        training_artifact(b'\x80\x04unsafe-pickle')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as package:
        package.writestr('model.safetensors', weight_bytes())
        package.writestr('run.py', 'print(1)')
    with pytest.raises(ValueError):
        training_artifact(stream.getvalue())


def test_independent_profiles_run_concurrently_same_profile_waits_and_cancel_is_owned(tmp_path, profiles, monkeypatch):
    import threading
    import time
    instance = ApiQueue(tmp_path, max_workers=2)
    entered = {kind: threading.Event() for kind in ('image', 'speech')}
    release = threading.Event()
    posts = []
    def submit(module, role, profile, params, source):
        posts.append((module, params.get('prompt')))
        entered[module].set()
        assert release.wait(5)
        return image_result()
    monkeypatch.setattr(queue_module, 'submit', submit)
    monkeypatch.setattr(queue_module, 'ingest', lambda *args: [{'name': 'saved.bin', 'kind': 'file', 'size': 1}])
    first = instance.create(payload())
    second = instance.create(payload(params={'prompt': 'Second image'}))
    speech = instance.create(payload('speech'))
    instance.start()
    try:
        assert entered['image'].wait(3) and entered['speech'].wait(3)
        assert len(posts) == 2
        assert instance.raw(second['id'])['status'] == 'queued'
        instance.handle('POST', '/jobs/' + first['id'] + '/cancel', scope())
        with pytest.raises(QueueError):
            instance.handle('POST', '/jobs/' + first['id'] + '/delete', scope())
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and instance.raw(second['id'])['status'] != 'succeeded':
            time.sleep(0.02)
        assert instance.raw(first['id'])['status'] == 'cancelled'
        assert instance.raw(speech['id'])['status'] == 'succeeded'
        assert instance.raw(second['id'])['status'] == 'succeeded'
        assert len(posts) == 3
    finally:
        release.set()
        instance.stop()
    assert not instance.active_ids


def test_stop_and_restart_resume_remote_poll_without_new_submit(tmp_path, profiles, monkeypatch):
    import threading
    import time
    instance = ApiQueue(tmp_path)
    job = instance.create(payload())
    submitted = threading.Event()
    def submit(*args):
        submitted.set()
        return {'id': 'remote-original', 'status': 'queued'}
    post = Mock(side_effect=submit)
    monkeypatch.setattr(queue_module, 'submit', post)
    instance.start()
    assert submitted.wait(3)
    instance.stop()
    assert instance.raw(job['id'])['remote_id'] == 'remote-original'
    assert not instance.active_ids
    restarted = ApiQueue(tmp_path)
    monkeypatch.setattr(restarted.quit, 'wait', lambda seconds: False)
    monkeypatch.setattr(queue_module, 'request_json', Mock(return_value=image_result(id='remote-original', status='completed')))
    restarted.execute(restarted.raw(job['id']))
    assert restarted.raw(job['id'])['status'] == 'succeeded'
    assert post.call_count == 1


@pytest.mark.parametrize('operation,expected', [('cancel', 'cancelled'), ('stop', 'queued')])
def test_cancel_or_shutdown_before_submission_commit_never_calls_api(queue, monkeypatch, operation, expected):
    import threading
    job = queue.create(payload())
    entered, release = threading.Event(), threading.Event()
    original_begin = queue._begin_submission
    def begin(*args):
        entered.set()
        assert release.wait(3)
        return original_begin(*args)
    monkeypatch.setattr(queue, '_begin_submission', begin)
    post = Mock(side_effect=AssertionError('cancelled before submission'))
    monkeypatch.setattr(queue_module, 'submit', post)
    worker = threading.Thread(target=queue.execute, args=(job,))
    worker.start()
    try:
        assert entered.wait(3)
        if operation == 'cancel':
            queue.handle('POST', '/jobs/' + job['id'] + '/cancel', scope())
        else:
            queue.stop()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert queue.raw(job['id'])['status'] == expected
    assert not queue.raw(job['id']).get('submission_started')
    post.assert_not_called()

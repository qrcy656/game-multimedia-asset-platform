"""Bounded remote inference and artifact ingestion. Credentials stay on origin."""
import base64
import io
import json
import mimetypes
from pathlib import Path
import struct
from urllib.parse import quote, urljoin, urlsplit

import httpx
from PIL import Image

from providers import invoke_sync
from .comfy_workflow import ComfyClient, ComfyError, SubmissionUncertain
from .training import training_artifact

MAX_ARTIFACT = 256 * 1024 * 1024
MAX_JSON = MAX_ARTIFACT * 4 // 3 + 1024 * 1024


class SubmissionRejected(ValueError):
    """A definitive pre-acceptance HTTP rejection, safe to correct and retry."""


def _response_bytes(response, maximum):
    chunks, count = [], 0
    for chunk in response.iter_bytes():
        count += len(chunk)
        if count > maximum:
            raise ValueError('模型 API 返回内容超过限制。')
        chunks.append(chunk)
    return b''.join(chunks)


def _status(response, *, submitting=False):
    if response.status_code < 300:
        return
    if submitting:
        if response.status_code in (408, 409, 429) or response.status_code >= 500:
            raise SubmissionUncertain('模型 API 提交结果不明确；已保存本任务，禁止直接重试以免重复制作。')
        raise SubmissionRejected(f'模型 API 拒绝提交（HTTP {response.status_code}），请检查配置和参数。')
    raise ValueError(f'模型 API 请求失败（HTTP {response.status_code}），请检查配置和参数。')


def headers(profile):
    return {'Authorization': 'Bearer ' + profile['api_key']} if profile.get('api_key') else {}


def fetch_bytes(profile, url, maximum=MAX_ARTIFACT):
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError('API 返回无效下载地址。')
    target = urljoin(profile['base_url'] + '/', url)
    parsed = urlsplit(target)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('API 返回无效下载地址。')
    same_origin = (parsed.scheme, parsed.netloc) == (urlsplit(profile['base_url']).scheme, urlsplit(profile['base_url']).netloc)
    # A signed CDN URL is allowed, but never receives the model API credential.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=180) as client:
        with client.stream('GET', target, headers=headers(profile) if same_origin else {}) as response:
            if response.status_code != 200:
                raise ValueError(f'成果下载失败（HTTP {response.status_code}）。')
            chunks, count = [], 0
            for chunk in response.iter_bytes():
                count += len(chunk)
                if count > maximum:
                    raise ValueError('API 成果超过文件大小上限。')
                chunks.append(chunk)
            return b''.join(chunks)


def request_json(profile, path, payload=None, timeout=120, *, maximum=8*1024*1024):
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('无效 API 路径。')
    submitting = payload is not None
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            with client.stream('GET' if not submitting else 'POST', profile['base_url'] + path,
                              headers=headers(profile), json=payload) as response:
                _status(response, submitting=submitting)
                result = json.loads(_response_bytes(response, maximum))
        if not isinstance(result, dict) or not result:
            raise ValueError('API 必须返回非空 JSON 对象。')
        return result
    except (SubmissionRejected, SubmissionUncertain):
        raise
    except (httpx.HTTPError, ValueError):
        if submitting:
            raise SubmissionUncertain('模型 API 提交后未收到有效响应；请核对远程服务，不会自动重发。') from None
        raise ValueError('无法读取模型 API 的有效响应，请检查远程服务。') from None


class ConfiguredComfy(ComfyClient):
    def __init__(self, profile):
        self.profile = profile
        self.base = profile['base_url']

    def request(self, path, body=None, *, raw=False, content_type=None, maximum=8*1024*1024, timeout=30):
        if not path.startswith('/') or path.startswith('//'):
            raise ComfyError('无效 ComfyUI API 路径。')
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
                with client.stream('GET' if body is None else 'POST', self.base + path,
                        headers={**headers(self.profile), 'Content-Type': content_type or 'application/json'},
                        content=body if isinstance(body, bytes) else None,
                        json=body if isinstance(body, dict) else None) as response:
                    if response.status_code >= 300:
                        if path == '/prompt' and (response.status_code in (408, 409, 429) or response.status_code >= 500):
                            raise SubmissionUncertain('ComfyUI 提交结果不明确；不会自动重发。')
                        raise ComfyError(f'ComfyUI 请求失败（HTTP {response.status_code}）。')
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > maximum:
                            if path == '/prompt':
                                raise SubmissionUncertain('ComfyUI 提交后的响应超过限制，结果不明确；不会自动重发。')
                            raise ComfyError('ComfyUI 返回内容超过限制。')
                        chunks.append(chunk)
            data = b''.join(chunks)
            if not data and body is not None and path in ('/queue', '/history'):
                return {}
            return data if raw else json.loads(data)
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, (ComfyError, SubmissionUncertain)):
                raise
            if path == '/prompt':
                raise SubmissionUncertain('ComfyUI 提交结果不明确；请检查远程任务，不会自动重发。') from None
            raise ComfyError('无法连接 ComfyUI。') from None


def generation_payload(profile, params, input_path):
    values = {key: value for key, value in params.items() if key != 'title'}
    if profile['protocol'] == 'rest':
        training = profile['kind'] == 'music' and params.get('task') == 'train'
        if training:
            if 'music_train' not in profile.get('capabilities', []) or input_path is None:
                raise SubmissionRejected('此 API 未验证训练能力或缺少训练数据。')
            values = {key: value for key, value in values.items() if key in {'task', 'prompt', 'duration', 'epochs', 'rank', 'learning_rate'}}
        elif profile['kind'] == 'music':
            values = {key: value for key, value in values.items() if key not in {'epochs', 'rank', 'learning_rate'}}
        if input_path:
            source = Path(input_path)
            values['dataset' if training else 'image'] = {'mime_type': 'application/zip' if training else mimetypes.guess_type(source.name)[0] or 'image/png',
                               'data_base64': base64.b64encode(source.read_bytes()).decode()}
        return {'input': values}
    if profile['kind'] == 'image':
        if input_path:
            # Multipart image edits are handled by submit; never silently ignore a reference.
            return {'prompt': params['prompt'], 'size': f"{params['width']}x{params['height']}", 'n': 1}
        return {'prompt': params['prompt'], 'size': f"{params['width']}x{params['height']}", 'n': 1, 'response_format': 'b64_json'}
    if profile['kind'] == 'speech':
        return {'input': params['text'], 'voice': params['speaker'], 'instructions': params['instruct'], 'response_format': 'wav'}
    raise ValueError('该模型类型需要 fantasy-v1 REST API。')


def submit(module, role, profile, params, input_path):
    payload = generation_payload(profile, params, input_path)
    data = {**profile['options'].get('parameters', {}), **payload, 'model': profile['model']}
    timeout = profile['options'].get('timeout', 180)
    if profile['protocol'] == 'openai' and role == 'image' and input_path:
        try:
            with Path(input_path).open('rb') as image, httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
                with client.stream('POST', profile['base_url'] + '/images/edits', headers=headers(profile),
                    data={key: str(value) for key, value in data.items()},
                    files={'image': (Path(input_path).name, image, mimetypes.guess_type(str(input_path))[0] or 'image/png')}) as response:
                    _status(response, submitting=True)
                    result = json.loads(_response_bytes(response, MAX_JSON))
            if not isinstance(result, dict) or not result:
                raise ValueError()
            return result
        except (SubmissionRejected, SubmissionUncertain):
            raise
        except (httpx.HTTPError, ValueError):
            raise SubmissionUncertain('图片编辑提交结果不明确；请核对远程服务，不会自动重发。') from None
    if profile['protocol'] == 'rest':
        return request_json(profile, '/generate', {**data, 'kind': role}, timeout, maximum=MAX_JSON)
    if profile['protocol'] == 'openai' and role == 'image':
        return request_json(profile, '/images/generations', data, timeout, maximum=MAX_JSON)
    if profile['protocol'] == 'openai' and role == 'speech':
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
                with client.stream('POST', profile['base_url'] + '/audio/speech', headers=headers(profile), json=data) as response:
                    _status(response, submitting=True)
                    binary = _response_bytes(response, MAX_ARTIFACT)
            if not binary:
                raise ValueError()
            return {'data_base64': base64.b64encode(binary).decode()}
        except (SubmissionRejected, SubmissionUncertain):
            raise
        except (httpx.HTTPError, ValueError):
            raise SubmissionUncertain('语音制作提交结果不明确；请核对远程服务，不会自动重发。') from None
    raise ValueError('该媒体配置不支持此提交协议。')


def artifact_type(data, role):
    if role == 'music_train':
        return training_artifact(data)
    if role in ('image', 'workflow'):
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 100_000_000:
                raise ValueError('生成图片格式或尺寸不受支持。')
            image.verify()
            return 'image', {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[image.format]
    if role in ('speech', 'music'):
        if data[:4] == b'RIFF' and data[8:12] == b'WAVE': return 'audio', '.wav'
        if data[:4] == b'fLaC': return 'audio', '.flac'
        if data[:4] == b'OggS': return 'audio', '.ogg'
        if data[:3] == b'ID3' or len(data) > 2 and data[0] == 255 and data[1] & 224 == 224: return 'audio', '.mp3'
        raise ValueError('API 未返回支持的音频文件。')
    if role == 'model3d' and data[:4] == b'glTF' and len(data) >= 20:
        version, size, chunk_size, chunk_type = struct.unpack('<IIII', data[4:20])
        if version == 2 and size == len(data) and chunk_type == 0x4E4F534A and 0 < chunk_size <= len(data) - 20:
            try:
                document = json.loads(data[20:20+chunk_size])
                if document.get('asset', {}).get('version') == '2.0':
                    return 'model', '.glb'
            except (ValueError, AttributeError):
                pass
    raise ValueError('API 成果格式与模块不符。')


def ingest(profile, role, result, output):
    if not isinstance(result, dict):
        raise ValueError('API 未返回有效成果对象。')
    items = result.get('artifacts') or result.get('data')
    if 'data_base64' in result:
        items = [result]
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ValueError('API 未返回有效成果列表。')
    artifacts, total = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('API 成果项格式无效。')
        encoded = item.get('data_base64') or item.get('b64_json')
        if encoded:
            if not isinstance(encoded, str) or len(encoded) > MAX_ARTIFACT * 4 // 3 + 4:
                raise ValueError('API 成果过大。')
            data = base64.b64decode(encoded, validate=True)
        elif item.get('url'):
            data = fetch_bytes(profile, item['url'])
        else:
            raise ValueError('API 成果缺少 data_base64 或 url。')
        total += len(data)
        if not data or total > MAX_ARTIFACT:
            raise ValueError('API 成果为空或超过 256 MiB。')
        kind, extension = artifact_type(data, role)
        name = f'output-{len(artifacts)+1:02d}{extension}'
        (Path(output) / name).write_bytes(data)
        artifacts.append({'name': name, 'kind': kind, 'size': len(data)})
    return artifacts

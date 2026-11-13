"""Required API matting with durable, one-submission-per-frame receipts."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import re

import httpx
from PIL import Image

import providers
from media import remote
from .clients import Cancelled, NeedsAttention, PermanentError, TransientError
from .files import atomic_json, sha256

VERSION = 'api-matte-rgba-v1'
MAX_IMAGE = 64 * 1024 * 1024
JOB_ID = re.compile(r'^[A-Za-z0-9_-]{1,160}$')
PENDING = {'queued', 'pending', 'processing', 'in_progress', 'running'}
SUCCEEDED = {'succeeded', 'completed', 'success', 'done'}
FAILED = {'failed', 'cancelled', 'canceled', 'deleted', 'error'}
PROMPT = ('Remove only the background from this exact animation frame. Preserve the subject, '
          'all visible details, colors, pose and pixel positions. Do not redraw, crop, resize, '
          'add shadows or change the canvas. Return one PNG at the exact input dimensions '
          'with a transparent alpha background and an opaque subject.')


def configuration():
    """Public readiness metadata; never include a key, URL or advanced options."""
    try:
        binding = next((row for row in providers.registry().list_bindings()
                        if row['module'] == 'animation' and row['role'] == 'image'), None)
        if not binding:
            return {'mode': 'api', 'available': False, 'name': '抠图 API',
                    'message': '请在主页为动画绑定支持透明背景图片编辑的图片 API；视频与抠图 API 均就绪后才能生成。'}
        profile = providers.resolve('animation', 'image', binding['profile_id'])
        if profile['protocol'] not in {'openai', 'rest'}:
            raise ValueError('Unsupported matting protocol')
        return {'mode': 'api', 'available': True, 'name': profile['name']}
    except (ValueError, KeyError, RuntimeError):
        return {'mode': 'api', 'available': False, 'name': '图片 API',
                'message': '动画抠图 API 配置不可用，请在主页重新检测并绑定支持透明背景图片编辑的图片 API。'}


def freeze_provider(worker, job):
    """Freeze the required binding before any generation, without saving its key."""
    checkpoint = job['checkpoint']
    if 'matting_provider' not in checkpoint:
        bindings = providers.registry().list_bindings()
        binding = next((row for row in bindings if row['module'] == 'animation' and row['role'] == 'image'), None)
        if not binding:
            raise PermanentError('动画必须配置抠图 API；本次尚未提交视频生成。请在主页绑定支持透明背景图片编辑的图片 API。')
        if binding:
            try:
                profile = providers.resolve('animation', 'image', binding['profile_id'])
            except (ValueError, KeyError, RuntimeError) as exc:
                raise PermanentError('动画背景处理的图片 API 配置未通过验证，请在主页重新检测。') from exc
            if profile['protocol'] not in {'openai', 'rest'}:
                raise PermanentError('动画 API 背景处理仅支持 OpenAI 图片编辑或 Fantasy REST 图片协议。')
            frozen = {'mode': 'api', 'version': VERSION,
                      **{key: profile.get(key) for key in ('id', 'name', 'model', 'protocol', 'scope')},
                      'fingerprint': providers.fingerprint(profile)}
        worker.checkpoint(job, job['stage'], matting_provider=frozen)
    if checkpoint['matting_provider'].get('mode') != 'api':
        raise NeedsAttention('此旧任务冻结了 CPU 背景处理。请重新建立使用图片 API 的任务；系统不会静默更换已冻结的处理方案。')
    return checkpoint['matting_provider']


class ApiMatting:
    def __init__(self, worker, job):
        self.worker, self.job = worker, job
        self.frozen = job['checkpoint']['matting_provider']
        self.root = worker.settings.data_root / 'jobs' / job['id'] / 'matting'
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self):
        self.worker.checkpoint(self.job, 'postprocessing', matting_frames=self.job['checkpoint']['matting_frames'])

    def profile(self):
        try:
            profile = providers.resolve('animation', 'image', self.frozen['id'])
        except (ValueError, KeyError, RuntimeError) as exc:
            raise NeedsAttention('原抠图 API 配置不可用，请恢复已冻结的配置；不会改用其他模型重发。') from exc
        if providers.fingerprint(profile) != self.frozen['fingerprint']:
            raise NeedsAttention('抠图 API 的端点、模型或参数已改变，请恢复原配置后继续，避免重复计费。')
        return profile

    @staticmethod
    def validate(path, size):
        try:
            with Image.open(path) as image:
                if image.format not in {'PNG', 'WEBP'} or image.size != tuple(size) or image.mode != 'RGBA':
                    raise ValueError('图片必须是原尺寸的 RGBA PNG/WebP')
                image.load()
                low, high = image.getchannel('A').getextrema()
                if low > 8 or high < 128:
                    raise ValueError('图片必须同时包含透明背景与不透明前景')
                return image.copy()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise PermanentError('抠图 API 返回无效 alpha 图片：' + str(exc)) from exc

    def response(self, result, path, record):
        # Store large inline image data on disk, never in the database checkpoint.
        atomic_json(path, result)
        record['response_sha256'] = sha256(path)
        self.save()

    def completed_image(self, profile, result, destination, size, source):
        items = result.get('artifacts') or result.get('data')
        if 'data_base64' in result:
            items = [result]
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise PermanentError('抠图 API 必须为每帧返回且仅返回一张图片。')
        item = items[0]
        encoded = item.get('data_base64') or item.get('b64_json')
        try:
            if encoded:
                if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE * 4 // 3 + 4:
                    raise ValueError('抠图图片超过 64 MiB')
                binary = base64.b64decode(encoded, validate=True)
            elif item.get('url'):
                binary = remote.fetch_bytes(profile, item['url'], maximum=MAX_IMAGE)
            else:
                raise ValueError('抠图结果缺少图片数据或下载地址')
        except httpx.HTTPError as exc:
            raise TransientError('抠图结果下载暂时中断，将重读已保存结果，不会重新生成。') from exc
        except (ValueError, OSError) as exc:
            raise PermanentError('无法读取已生成的抠图结果；不会重新提交：' + str(exc)) from exc
        if not binary or len(binary) > MAX_IMAGE:
            raise PermanentError('抠图图片为空或超过 64 MiB')
        matte = self.validate(io.BytesIO(binary), size)
        # Image-edit providers may recolor/redraw pixels. Consume only their
        # alpha channel; every RGB pixel comes from the original video frame.
        with Image.open(source) as original:
            image = original.convert('RGBA')
        image.putalpha(matte.getchannel('A'))
        temporary = destination.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            image.save(stream, format='PNG')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)

    def frame(self, source, index, record):
        folder = self.root / f'{index:06d}'
        folder.mkdir(exist_ok=True)
        output, response_path = folder / 'matte.png', folder / 'response.json'
        if output.exists():
            self.validate(output, record['size'])
            digest = sha256(output)
            if record.get('output_sha256') and record['output_sha256'] != digest:
                raise NeedsAttention('已保存抠图帧校验失败；不会自动重新生成。')
            record.update(state='completed', output_sha256=digest)
            self.save()
            return output
        if record.get('state') in {'completed', 'rejected'}:
            raise NeedsAttention('抠图帧已完成但文件缺失，或已明确被拒绝；不会自动重新提交。')
        profile = self.profile()
        result = None
        if response_path.exists():
            if record.get('response_sha256') and sha256(response_path) != record['response_sha256']:
                raise NeedsAttention('已保存抠图 API 回执校验失败；不会自动重新提交。')
            try:
                result = json.loads(response_path.read_text(encoding='utf-8'))
            except (ValueError, OSError) as exc:
                raise NeedsAttention('抠图 API 回执损坏；不会自动重新提交。') from exc
        elif record.get('submission_started_at') is not None and not record.get('remote_id'):
            raise NeedsAttention('抠图帧提交结果未知，请核对远程服务；系统不会自动重复提交。')
        if result is None and not record.get('remote_id'):
            self.worker.check_cancel(self.job)
            record.update(state='submitting', submission_started_at=self.worker.clock())
            self.save()  # Commit before POST, including before any network setup.
            params = {'prompt': PROMPT, 'width': record['size'][0], 'height': record['size'][1],
                      'operation': 'remove_background', 'background': 'transparent', 'output_format': 'png'}
            submitted_profile = copy.deepcopy(profile)
            if profile['protocol'] == 'openai':
                submitted_profile['options']['parameters'] = {
                    **profile['options'].get('parameters', {}), 'background': 'transparent', 'output_format': 'png'}
            try:
                result = remote.submit('animation', 'image', submitted_profile, params, source)
            except remote.SubmissionRejected as exc:
                record.update(state='rejected')
                self.save()
                raise PermanentError('抠图 API 拒绝请求：' + str(exc)) from exc
            except remote.SubmissionUncertain as exc:
                raise NeedsAttention('抠图帧提交结果未知，请核对远程服务；系统不会自动重复提交。') from exc
            self.response(result, response_path, record)
        while True:
            if result is not None:
                if not isinstance(result, dict):
                    raise NeedsAttention('抠图 API 返回无效结果；不会重复提交。')
                identifier = result.get('id') or result.get('job_id')
                if identifier is not None:
                    if not isinstance(identifier, str) or not JOB_ID.fullmatch(identifier):
                        raise NeedsAttention('抠图 API 返回无效任务 ID；不会重复提交。')
                    if record.get('remote_id') and identifier != record['remote_id']:
                        raise NeedsAttention('抠图 API 返回了其他任务 ID；不会重复提交。')
                    record['remote_id'] = identifier
                    record.setdefault('poll_started_at', self.worker.clock())
                    self.save()
                status = result.get('status')
                if status in FAILED:
                    record['state'] = 'rejected'
                    self.save()
                    raise PermanentError('远程抠图任务失败，已保留原视频与回执。')
                if status in SUCCEEDED or (status is None and any(result.get(key) for key in ('artifacts', 'data', 'data_base64'))):
                    break
                if status not in PENDING or not record.get('remote_id'):
                    raise NeedsAttention('抠图 API 未返回有效图片或可跟踪任务；不会重复提交。')
            if not record.get('remote_id'):
                raise NeedsAttention('抠图任务无法追踪；不会重复提交。')
            if self.worker.clock() - record.get('poll_started_at', self.worker.clock()) > self.worker.settings.max_poll_seconds:
                raise NeedsAttention('抠图任务轮询超时，请检查远程任务；不会重复提交。')
            self.worker.check_cancel(self.job)
            self.worker.sleep(self.worker.settings.poll_seconds)
            try:
                result = remote.request_json(profile, '/jobs/' + record['remote_id'], maximum=remote.MAX_JSON)
            except ValueError as exc:
                raise TransientError('暂时无法读取抠图任务状态，将只继续查询同一任务。') from exc
            self.response(result, response_path, record)
        self.completed_image(profile, result, output, record['size'], source)
        record.update(state='completed', output_sha256=sha256(output))
        self.save()
        return output

    def __call__(self, sources, work_dir):
        del work_dir  # Cache belongs to the durable job, not postprocess temp files.
        if not 1 <= len(sources) <= 120:
            raise PermanentError('API 背景处理支持 1 至 120 个已选帧。')
        proposed = []
        for source in sources:
            source = Path(source)
            with Image.open(source) as image:
                size = list(image.size)
            if source.stat().st_size > MAX_IMAGE or size[0] * size[1] > 16_777_216:
                raise PermanentError('待抠图帧超过图片大小或像素上限。')
            proposed.append({'source_sha256': sha256(source), 'size': size, 'state': 'pending'})
        records = self.job['checkpoint'].get('matting_frames')
        if records is not None:
            if len(records) != len(proposed) or any(
                    row.get('source_sha256') != new['source_sha256'] or row.get('size') != new['size']
                    for row, new in zip(records, proposed)):
                raise NeedsAttention('重新提取的帧与已提交抠图帧不一致；不会继续生成或重复计费。')
        else:
            self.job['checkpoint']['matting_frames'] = records = proposed
            self.save()
        outputs = []
        for index, (source, record) in enumerate(zip(sources, records)):
            self.worker.check_cancel(self.job)
            outputs.append(self.frame(Path(source), index, record))
        return {'paths': outputs, 'metadata': {**self.frozen, 'frame_count': len(outputs),
            'alpha_validation': 'exact-input-size/RGBA/transparent-background/opaque-foreground',
            'rgb_source': 'original-video-frame',
            'visual_review_required': True}}


def matte_provider(worker, job):
    freeze_provider(worker, job)
    return ApiMatting(worker, job)

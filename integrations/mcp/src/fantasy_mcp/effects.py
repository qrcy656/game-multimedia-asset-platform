"""Account-bound Arcadia tools using the edition's authenticated portal session."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from .common import AdapterError, checked_destination

EXPORT_POLL_SECONDS = 1.5


def effect_filename(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value.encode('utf-8')) <= 244 or value.startswith('.')
            or value.strip() != value or '..' in value or any(c in value for c in '/\\:*?"<>|')
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or not value.endswith('.json')):
        raise AdapterError('Use an effects JSON filename returned by list_effects, without directories.')
    return value


def revision(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise AdapterError('expected_revision must be the SHA256 revision returned by read_effect.')
    return value


def export_job_id(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AdapterError('Use the export job UUID returned by start_effect_export.') from None
    return value


def export_body(file: str, format: str, time: float | None) -> dict:
    file = effect_filename(file)
    if format not in ('frame', 'atlas'):
        raise AdapterError('Effects export format must be frame or atlas.')
    if time is not None and (isinstance(time, bool) or not isinstance(time, (int, float))
                             or not math.isfinite(time) or time < 0):
        raise AdapterError('Export time must be a finite nonnegative number of seconds.')
    body = {'file': file, 'format': format}
    if time is not None:
        body['time'] = time
    return body


class EffectsMixin:
    def _content_owner(self, owner_key):
        if owner_key is None:
            return {}
        if self.settings.edition != 'community' or not isinstance(owner_key, str) or not re.fullmatch(r'[a-f0-9]{64}', owner_key):
            raise AdapterError('owner_key must come from a community list_effects entry.')
        return {'owner_key': owner_key}

    async def _effects_transport_headers(self, *, write: bool) -> dict[str, str]:
        """Edition-specific portal headers; private SSO needs only its session cookie."""
        return {}

    def _effects_request_origin(self) -> str:
        parts = urlsplit(self.settings.module_url('effects'))
        return f'{parts.scheme}://{parts.netloc}'

    async def _effects_request(self, method: str, path: str, *, stream=False, **kwargs) -> httpx.Response:
        """Refresh read sessions once; writes are never resent after any ambiguous result."""
        base = self.settings.module_url('effects')
        for attempt in range(2):
            await self._ensure_login()
            portal_headers = await self._effects_transport_headers(write=method != 'GET')
            owner = self._effects_owner
            try:
                session = await self.http.get(base + '/api/session',
                                              headers={**portal_headers, **({'X-Effects-Owner': owner} if owner else {})})
                if session.status_code == 401:
                    self._authenticated = False
                    self._csrf.clear()
                    if method == 'GET' and attempt == 0:
                        continue
                    raise AdapterError('Effects session expired; retry the operation explicitly to sign in again.')
                self._effects_status(session)
                try:
                    payload = session.json()
                    account = payload['user']['id']
                    csrf = payload['csrf']
                    if not isinstance(account, str) or str(UUID(account)) != account or not isinstance(csrf, str) or not csrf:
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise AdapterError('Effects returned an invalid account session.') from None
                bound_owner = self._effects_owner
                if bound_owner is not None and account != bound_owner:
                    raise AdapterError('Effects account changed. Restart the MCP connection deliberately before using another account.')
                self._effects_owner = account
                headers = {**portal_headers, 'Accept': 'image/png' if stream else 'application/json', 'X-Effects-Owner': account}
                if method != 'GET':
                    headers.update({'Origin': self._effects_request_origin(), 'X-Effects-CSRF': csrf})
                request = self.http.build_request(method, base + path, headers=headers,
                                                  timeout=max(self.settings.timeout_seconds, 180), **kwargs)
                response = await self.http.send(request, stream=stream)
            except httpx.HTTPError:
                suffix = ' Its result is unknown; read the effect before deciding whether to retry.' if method != 'GET' else ''
                raise AdapterError('Effects HTTP request failed.' + suffix) from None
            if response.status_code == 401:
                await response.aclose()
                self._authenticated = False
                self._csrf.clear()
                if method == 'GET' and attempt == 0:
                    continue
                raise AdapterError('Effects session expired; no write was retried automatically.')
            try:
                self._effects_status(response)
            except AdapterError:
                await response.aclose()
                raise
            return response
        raise AdapterError('Could not restore the Effects session.')

    @staticmethod
    def _effects_status(response):
        if response.status_code == 409:
            raise AdapterError('Effects revision conflict: read the current effect, reconcile changes, and use its new revision.')
        if response.status_code == 403:
            raise AdapterError('Effects access denied or account changed; check the configured account before restarting MCP.')
        if not 200 <= response.status_code < 300:
            raise AdapterError(f'Effects returned HTTP {response.status_code}; check account access, readiness and input. No write was retried.')

    @staticmethod
    def _effects_json(response, expected_type=dict):
        try:
            result = response.json()
        except ValueError:
            raise AdapterError('Effects returned an unexpected non-JSON response.') from None
        if not isinstance(result, expected_type):
            raise AdapterError('Effects returned an unexpected response shape.')
        return result

    async def effects_guide(self) -> dict:
        return self._effects_json(await self._effects_request('GET', '/api/guide'))

    async def effects_list(self) -> dict:
        return {'effects': self._effects_json(await self._effects_request('GET', '/api/list'), list)}

    async def effects_read(self, file: str, *, owner_key: str | None = None) -> dict:
        file = effect_filename(file)
        response = await self._effects_request('GET', '/api/effect', params={'f': file, **self._content_owner(owner_key)})
        etag = response.headers.get('etag', '')
        if not re.fullmatch(r'"[a-f0-9]{64}"', etag):
            raise AdapterError('Effects did not provide a valid revision; editing is unavailable until a verified read succeeds.')
        digest = hashlib.sha256(response.content).hexdigest()
        if etag[1:-1] != digest:
            raise AdapterError('Effects revision does not match the returned document.')
        return {'file': file, 'revision': digest, 'data': self._effects_json(response), **self._content_owner(owner_key)}

    async def effects_validate(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise AdapterError('Effect data must be a JSON object; read get_effects_guide first.')
        return self._effects_json(await self._effects_request('POST', '/api/validate', json={'data': data}))

    async def effects_save(self, name: str, data: dict, overwrite: bool = False,
                           expected_revision: str | None = None, *, owner_key: str | None = None) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise AdapterError('Provide a nonempty effect name.')
        name = name.strip()
        # The HTTP service always appends .json; accept a filename from read_effect without doubling it.
        if name.endswith('.json'):
            name = name[:-5]
        effect_filename(name + '.json')
        if not isinstance(data, dict) or not isinstance(overwrite, bool):
            raise AdapterError('Effect data must be a JSON object and overwrite must be a boolean.')
        body = {'name': name, 'data': data, **self._content_owner(owner_key)}
        if overwrite:
            body['expected_sha256'] = revision(expected_revision)
        elif expected_revision is not None:
            raise AdapterError('Set overwrite=true explicitly when editing an effect using expected_revision.')
        else:
            body['create_only'] = True
        return self._effects_json(await self._effects_request('POST', '/api/save', json=body))

    def _effects_export_job(self, response: httpx.Response, expected_id: str | None = None) -> dict:
        payload = self._effects_json(response)
        try:
            job = payload['job']
            if not isinstance(job, dict):
                raise ValueError()
            job_id = export_job_id(job['id'])
            if expected_id is not None and job_id != expected_id:
                raise ValueError()
            if job['status'] not in ('running', 'done', 'failed', 'cancelled'):
                raise ValueError()
            body = export_body(job['file'], job['format'], job.get('time'))
        except (ValueError, KeyError, TypeError, AdapterError):
            raise AdapterError('Effects returned an invalid export job; no submission was retried.') from None
        # Only expose the public job fields; never forward arbitrary server data.
        clean = {'id': job_id, 'status': job['status'], **body}
        for key in ('created_at', 'started_at', 'finished_at', 'error'):
            if isinstance(job.get(key), str):
                clean[key] = job[key]
        return {'job': clean}

    async def effects_start_export(self, file: str, format: str = 'frame', time: float | None = None, *, owner_key: str | None = None) -> dict:
        """Submit once. All subsequent rendering work is identified by the returned UUID."""
        body = {**export_body(file, format, time), **self._content_owner(owner_key)}
        response = await self._effects_request('POST', '/api/exports', json=body)
        return self._effects_export_job(response)

    async def effects_get_export(self, job_id: str) -> dict:
        job_id = export_job_id(job_id)
        try:
            response = await self._effects_request('GET', f'/api/exports/{job_id}')
            return self._effects_export_job(response, job_id)
        except AdapterError as error:
            raise AdapterError(f'Export job {job_id}: {error} Use this job ID to check again; do not resubmit the render.') from None

    async def effects_cancel_export(self, job_id: str) -> dict:
        job_id = export_job_id(job_id)
        try:
            response = await self._effects_request('POST', f'/api/exports/{job_id}/cancel', json={})
            return self._effects_export_job(response, job_id)
        except AdapterError as error:
            raise AdapterError(f'Export job {job_id}: cancellation was not confirmed. {error} Check this job ID before another action.') from None

    async def effects_export(self, file: str, format: str = 'frame', time: float | None = None,
                             subdirectory: str | None = None, *, owner_key: str | None = None) -> dict:
        body = export_body(file, format, time)
        folder = subdirectory or f'effects/{uuid4().hex}'
        # Reject unusable output paths before starting potentially expensive work.
        checked_destination(folder, body['file'][:-5] + '-' + format + '.png', self.settings)
        checked_destination(folder, 'fantasy-effects-manifest.json', self.settings)
        job = (await self.effects_start_export(file, format, time, **self._content_owner(owner_key)))['job']
        job_id = job['id']
        try:
            while job['status'] == 'running':
                await asyncio.sleep(EXPORT_POLL_SECONDS)
                job = (await self.effects_get_export(job_id))['job']
            if job['status'] != 'done':
                raise AdapterError(f'Export job {job_id} is {job["status"]}; use get_effect_export for details.')
            return await self.effects_download_export(job_id, folder)
        except asyncio.CancelledError:
            # A client cancelling the convenience call should not leave a hidden
            # render. The split start/get/download workflow intentionally persists.
            try:
                await asyncio.wait_for(self.effects_cancel_export(job_id), timeout=5)
            except (Exception, asyncio.CancelledError):
                pass
            raise
        except AdapterError as error:
            if job_id in str(error):
                raise
            raise AdapterError(f'Export job {job_id}: {error} Resume with get_effect_export/download_effect_export; do not resubmit.') from None

    async def effects_download_export(self, job_id: str, subdirectory: str | None = None) -> dict:
        job_id = export_job_id(job_id)
        try:
            return await self._effects_download_export(job_id, subdirectory)
        except AdapterError as error:
            if job_id in str(error):
                raise
            raise AdapterError(f'Export job {job_id}: {error} The job ID remains available for recovery.') from None

    async def _effects_download_export(self, job_id: str, subdirectory: str | None) -> dict:
        job = (await self.effects_get_export(job_id))['job']
        if job['status'] != 'done':
            raise AdapterError(f'Export job {job_id} is {job["status"]}; download requires done status.')
        file, format, time = job['file'], job['format'], job.get('time')
        folder = subdirectory or f'effects/{uuid4().hex}'
        target = checked_destination(folder, file[:-5] + '-' + format + '.png', self.settings)
        manifest_path = checked_destination(folder, 'fantasy-effects-manifest.json', self.settings)
        response = await self._effects_request('GET', f'/api/exports/{job_id}/result', stream=True)
        # Buffer a bounded PNG before creating outputs, so a failed/truncated stream cannot become an artifact.
        content = bytearray()
        try:
            if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'image/png':
                raise AdapterError('Effects export did not return a PNG image.')
            try:
                declared = int(response.headers['content-length']) if 'content-length' in response.headers else None
                if declared is not None and not 0 < declared <= self.settings.max_download_bytes:
                    raise ValueError()
                metadata = {key: int(response.headers['x-effects-' + key]) for key in ('width', 'height', 'frames')}
                metadata['fps'] = float(response.headers['x-effects-fps'])
                if (any(value <= 0 for value in metadata.values()) or not math.isfinite(metadata['fps'])):
                    raise ValueError()
            except (KeyError, ValueError):
                raise AdapterError('Effects export returned invalid dimensions, frame count, FPS or size.') from None
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > self.settings.max_download_bytes:
                    raise AdapterError('Effects export exceeds max_download_bytes.')
            if declared is not None and len(content) != declared:
                raise AdapterError('Effects export was truncated; no output was saved.')
            if not content.startswith(b'\x89PNG\r\n\x1a\n') or not content.endswith(b'\x00\x00\x00\x00IEND\xaeB`\x82'):
                raise AdapterError('Effects export is not a complete PNG; no output was saved.')
        except httpx.HTTPError:
            raise AdapterError('Effects export download failed; no output was saved and no export was retried.') from None
        finally:
            await response.aclose()
        artifact = {'path': str(target), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(), **metadata}
        manifest = {'edition': self.settings.edition, 'module': 'effects', 'job_id': job_id, 'file': file, 'format': format,
                    'time': time, 'artifacts': [artifact]}
        # Exclusive creation prevents overwrite races; remove only files created by this call on failure.
        created = []
        try:
            with target.open('xb') as out:
                created.append(target)
                out.write(content)
            with manifest_path.open('x', encoding='utf-8') as out:
                created.append(manifest_path)
                json.dump(manifest, out, ensure_ascii=False, indent=2)
        except OSError:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise AdapterError('Effects export output could not be saved or already exists; choose another subdirectory.') from None
        result = {**manifest, 'manifest_path': str(manifest_path)}
        # Only release the server cache after both validated local files exist.
        # Cleanup failure must not turn a successfully saved PNG into a failed export.
        try:
            await self._effects_request('POST', f'/api/exports/{job_id}/release', json={})
        except (AdapterError, asyncio.CancelledError):
            result['warning'] = f'Local files are complete, but temporary server cleanup was not confirmed for job {job_id}.'
        else:
            result['server_cache_released'] = True
        return result

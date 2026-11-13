import base64
import json
import os
import re
import mimetypes
import uuid
from urllib.parse import quote, unquote, urljoin, urlsplit
from pathlib import Path
import subprocess

import httpx

from .files import atomic_json, prose, sha256
import providers
from .files import safe_path


from .visual_intent import ANIMATION_VISUAL_INTENT_SYSTEM, animation_reference_images

ANIMATION_TRANSLATOR_SYSTEM = ANIMATION_VISUAL_INTENT_SYSTEM


class TransientError(RuntimeError):
    pass


class GenerationServicesUnavailable(TransientError):
    def __init__(self, readiness):
        self.readiness = readiness
        super().__init__(readiness["message"] + "本次未提交新的生成请求。")


class H3NotSubmitted(TransientError):
    """The HTTP transport could not acquire/connect a socket; no POST was sent."""
    def __init__(self, transport_error):
        self.transport_error = transport_error
        super().__init__("视频 API 连接未建立，本次生成请求尚未发送；请检查配置后重试。")


class PermanentError(RuntimeError):
    pass


class H3Rejected(PermanentError):
    """A definitive HTTP rejection before H3 accepted a generation job."""
    def __init__(self, status_code, response_text):
        self.status_code = status_code
        self.response_text = response_text[:2000]
        super().__init__("Video API rejected request: " + self.response_text)


class NeedsAttention(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class ModelClients:
    def __init__(self, settings):
        self.settings = settings

    def video_profile(self, frozen=None):
        try:
            profile = providers.resolve("animation", "video", (frozen or {}).get("id"))
        except (ValueError, KeyError, RuntimeError) as exc:
            if frozen:
                raise NeedsAttention("原视频 API 配置不可用；请恢复该配置后继续，系统不会向其他模型重发。") from exc
            raise PermanentError("请在主页的 AI 模型配置中选择动画视频 API。") from exc
        if profile["protocol"] not in {"videos", "rest"}:
            raise PermanentError("动画视频 API 需使用 videos 或 rest 协议")
        if frozen and any(profile.get(key) != frozen.get(key) for key in ("id", "model", "protocol", "base_url")):
            raise NeedsAttention("视频 API 配置已改变，请恢复原端点和模型后继续原任务，避免重复生成。")
        return profile

    def generation_readiness(self, prompt_mode="direct"):
        from .matting import configuration
        try:
            profile = self.video_profile()
            ready, message = True, "视频 API 已配置；请求时将验证服务响应。"
            label = profile["name"]
        except (PermanentError, NeedsAttention) as exc:
            ready, message, label = False, str(exc), "视频 API"
        matting = configuration()
        vision = None
        if prompt_mode == "assisted":
            try:
                visual = self.require_visual_intent_ready()
                vision = {"available": True, "name": visual["name"]}
            except PermanentError as exc:
                vision = {"available": False, "name": "识图 LLM", "message": str(exc)}
        missing = ([] if ready else ["video"]) + ([] if matting['available'] else ["image"])
        services = {"video": {"available": ready, "name": label}, "image": matting}
        if vision is not None:
            services["vision"] = vision
            if not vision["available"]:
                missing.append("vision")
                message = vision["message"]
        if not matting['available']:
            message = matting['message']
        return {"ready": not missing, "missing_services": missing,
                "services": services, "message": message}

    def require_generation_ready(self, prompt_mode="direct"):
        readiness = self.generation_readiness(prompt_mode=prompt_mode)
        if not readiness["ready"]:
            raise GenerationServicesUnavailable(readiness)
        return readiness

    def require_visual_intent_ready(self):
        try:
            # Registry.resolve enforces verified vision capability, not a model-name guess.
            return providers.resolve("animation", "vision")
        except (ValueError, KeyError, RuntimeError) as exc:
            raise PermanentError("动画 LLM 辅助需要能识图的模型；请在 AI 模型配置→动画→LLM辅助与参考图理解（需识图）中配置并通过识图检测。") from exc

    def _json_chat(self, images, instruction, *, role="vision", system_instruction=None, num_predict=600):
        content = [{"type": "text", "text": instruction}]
        for path in images:
            content.append({"type": "image_url", "image_url": {"url": self._media_uri(Path(path))}})
        messages = [{"role": "user", "content": content if images else instruction}]
        if system_instruction:
            messages.insert(0, {"role": "system", "content": system_instruction})
        try:
            profile = providers.resolve("animation", role)
            result = providers.invoke_sync("animation", role, {"messages": messages, "temperature": 0,
                "max_tokens": num_predict, "response_format": {"type": "json_object"}}, profile["id"], expected_fingerprint=providers.fingerprint(profile))
            raw = result["choices"][0]["message"]["content"]
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("description"), str):
                raise ValueError("Missing description")
            result["description"] = prose(result["description"], limit=8000)
            if not result["description"]:
                raise ValueError("Empty description")
            result["provider"] = {key: profile.get(key) for key in ("id", "name", "model", "protocol", "scope")}
            return result
        except httpx.TransportError as exc:
            raise TransientError("模型 API 暂时不可达，请检查配置。") from exc
        except (ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
            raise PermanentError("模型 API 需返回包含 description 字符串的 JSON；请检查所选模型配置和输出。") from exc

    def qwen(self, images, instruction, **kwargs):
        # Compatibility name for the visual analysis used by existing jobs.
        return self._json_chat(images, instruction, **kwargs)

    def _media_uri(self, path):
        path = safe_path(self.settings.data_root, path)
        if not 0 < path.stat().st_size <= self.settings.upload_max_bytes:
            raise PermanentError("参考文件超过 API 传输大小限制")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    @staticmethod
    def _headers(profile):
        return {"Authorization": "Bearer " + profile["api_key"]} if profile.get("api_key") else {}

    @staticmethod
    def _endpoint(profile, suffix):
        return profile["base_url"].rstrip("/") + "/" + suffix.lstrip("/")

    def translate_animation(self, task, profile, reference, motion_description="", additional_references=None):
        """Plan concrete motion from reference images and intent; the compiler owns production constraints."""
        from .models import ACTIONS
        raw_intent = task.get("supplementary_description", "")
        intent = prose(raw_intent)
        legacy_action = task.get("action")
        legacy_description = ACTIONS[legacy_action][1] if legacy_action else ""
        if not intent and not motion_description and not legacy_description:
            raise PermanentError("AI 辅助编写需要填写动作意图或选择动作参考视频；不会自动套用默认动作。")
        try:
            paths = animation_reference_images(reference, additional_references)
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
        self.require_visual_intent_ready()
        input_data = {"user_animation_intent": intent, "duration_seconds": task["duration"],
                      "motion_reference_description": motion_description,
                      "legacy_explicit_action": legacy_description,
                      "identity_description": prose(profile.get("description", "")) if task.get("use_character_description", False) else ""}
        try:
            response = self._json_chat(paths, json.dumps(input_data, ensure_ascii=False), role="vision",
                                 system_instruction=ANIMATION_TRANSLATOR_SYSTEM, num_predict=1200)
        except (TransientError, PermanentError) as exc:
            raise type(exc)("AI 辅助动作指令生成失败；尚未向 video API 提交本次生成：" + str(exc)) from exc
        description = response["description"]
        if len(description) > 8000:
            raise PermanentError("AI 辅助动作指令过长；尚未向 video API 提交本次生成。")
        return {"description": description, "model": response["provider"]["model"],
                "provider": response["provider"], "role": "reference-grounded-motion-planner", "version": "2.0.0",
                "user_intent": raw_intent, "input": input_data,
                "reference_sha256": [sha256(path) for path in paths]}

    def analyze_character(self, paths):
        return self.qwen(paths, "Describe ONLY the visible character identity, hairstyle, colors, clothing, proportions and art style. All images are views of one character. Do not infer a weapon or unseen details. Do not supply camera, orientation, background or motion instructions. Return JSON {\"description\":\"concise English visual description\",\"uncertainties\":\"observations needing human review\"}.")

    def analyze_motion(self, video, output_dir):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            probe = subprocess.run([self.settings.ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(video)], check=True, capture_output=True, text=True, timeout=30)
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            if not 0 < duration <= 180:
                raise PermanentError("Motion reference must be at most 180 seconds")
            frames = []
            for index, fraction in enumerate((0.15, 0.5, 0.85)):
                output = output_dir / f"motion_frame_{index}.jpg"
                subprocess.run([self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(duration * fraction), "-i", str(video), "-frames:v", "1", "-vf", "scale=768:-2",
                    str(output)], check=True, capture_output=True, timeout=45)
                frames.append(output)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            raise PermanentError("Cannot extract motion reference frames: " + str(exc)) from exc
        result = self.qwen(frames, "These three chronological video frames describe a motion reference. Describe ONLY limb mechanics, action phases, rhythm and timing. Ignore all identity, face, clothing, colors, background, camera and screen facing. The generated character identity comes from a separate character image. Return JSON {\"description\":\"concise English motion mechanics\"}.")
        result["video_sha256"] = sha256(video)
        result["sample_fractions"] = [0.15, 0.5, 0.85]
        result["frames"] = [{"path": str(p), "sha256": sha256(p)} for p in frames]
        atomic_json(output_dir / "analysis.json", result)
        return result

    def submit_h3(self, request, provider=None):
        profile = self.video_profile(provider)
        body = json.loads(json.dumps(request))
        body["model"] = profile["model"]
        for item in body.get("conditions", []):
            value = item["uri"]
            if not value.startswith("file:"):
                raise PermanentError("参考图必须来自本工作台上传的文件")
            parsed = urlsplit(value)
            if parsed.netloc:
                raise PermanentError("不支持远程文件路径")
            raw = unquote(parsed.path)
            if os.name == "nt" and len(raw) > 3 and raw[0] == "/" and raw[2] == ":":
                raw = raw[1:]
            item["uri"] = self._media_uri(Path(raw))
        suffix = "videos" if profile["protocol"] == "videos" else "generate"
        if profile["protocol"] == "rest":
            body = {"kind": "video", "model": profile["model"], "input": body}
        try:
            response = httpx.post(self._endpoint(profile, suffix), json=body, headers=self._headers(profile),
                                  timeout=300, trust_env=False, follow_redirects=False)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise H3NotSubmitted(type(exc).__name__) from exc
        except httpx.TransportError as exc:
            raise NeedsAttention("提交结果未知；请检查视频 API 任务后再操作，系统不会自动重复提交。") from exc
        if response.status_code >= 500 or response.status_code in (408, 429) or 300 <= response.status_code < 400:
            raise NeedsAttention("视频 API 提交结果未知，HTTP " + str(response.status_code))
        if response.status_code >= 400:
            raise H3Rejected(response.status_code, "视频 API 拒绝请求")
        try:
            result = response.json()
            if profile["protocol"] == "rest" and result.get("artifacts") and not result.get("id"):
                result = {**result, "id": "sync-" + uuid.uuid4().hex, "status": "completed"}
            if not isinstance(result.get("id"), str) or not result["id"]:
                raise ValueError("Missing id")
            return self._state(result, default_status="queued")
        except (ValueError, AttributeError) as exc:
            raise NeedsAttention("视频 API 未返回可持久追踪的任务 ID；请人工检查，不会自动重发。") from exc

    @staticmethod
    def _state(result, *, expected_id=None, default_status=None):
        """Only persist contract fields; provider debug/error bodies may echo keys."""
        if not isinstance(result, dict):
            raise ValueError("Invalid provider state")
        identifier = result.get('id', expected_id)
        if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', identifier)
                or expected_id is not None and identifier != expected_id):
            raise ValueError("Provider task identity mismatch")
        status = result.get('status', default_status)
        if not isinstance(status, str):
            raise ValueError("Invalid provider status")
        status = {"succeeded": "completed", "success": "completed", "error": "failed"}.get(status, status)
        if status not in {'completed', 'failed', 'cancelled', 'canceled', 'deleted', 'queued', 'pending', 'processing', 'in_progress', 'running'}:
            raise ValueError("Unknown provider status")
        clean = {'id': identifier, 'status': status}
        if result.get('artifacts'):
            if not isinstance(result['artifacts'], list) or len(result['artifacts']) > 16:
                raise ValueError("Invalid provider artifacts")
            clean['artifacts'] = []
            for artifact in result['artifacts']:
                if not isinstance(artifact, dict):
                    raise ValueError("Invalid provider artifact")
                item = {key: artifact[key] for key in ('type', 'mime_type', 'mime', 'url') if key in artifact}
                if any(not isinstance(value, str) or len(value) > (8192 if key == 'url' else 256) for key, value in item.items()):
                    raise ValueError("Invalid provider artifact fields")
                clean['artifacts'].append(item)
        return clean

    def h3_status(self, job_id, provider=None):
        profile = self.video_profile(provider)
        suffix = ("videos/" if profile["protocol"] == "videos" else "jobs/") + quote(job_id, safe="")
        try:
            response = httpx.get(self._endpoint(profile, suffix), headers=self._headers(profile),
                                 timeout=60, trust_env=False, follow_redirects=False)
            if response.status_code == 404:
                raise NeedsAttention("视频 API 找不到原任务，请人工检查，系统不会重复提交。")
            if response.status_code >= 500 or response.status_code == 429:
                raise TransientError("视频 API 暂时无法查询任务")
            response.raise_for_status()
            return self._state(response.json(), expected_id=job_id)
        except httpx.TransportError as exc:
            raise TransientError("视频 API 轮询连接失败") from exc
        except (httpx.HTTPStatusError, ValueError, TypeError) as exc:
            raise NeedsAttention("视频 API 返回了无效的任务状态") from exc

    def download_h3(self, job_id, path, provider=None, state=None):
        profile = self.video_profile(provider)
        if profile["protocol"] == "videos":
            url = self._endpoint(profile, "videos/" + quote(job_id, safe="") + "/content")
        else:
            state = state or self.h3_status(job_id, provider)
            artifacts = state.get("artifacts") or []
            artifact = next((a for a in artifacts if isinstance(a, dict) and (a.get("type", "").startswith("video") or a.get("mime_type", a.get("mime", "")).startswith("video") or str(a.get("url", "")).split("?")[0].endswith(".mp4"))), None)
            if not artifact or not artifact.get("url"):
                raise PermanentError("视频 API 未提供可下载的 video artifact")
            url = urljoin(profile["base_url"].rstrip("/") + "/", artifact["url"])
            a, b = urlsplit(url), urlsplit(profile["base_url"])
            if (a.scheme, a.hostname, a.port) != (b.scheme, b.hostname, b.port) or a.username or a.password:
                raise PermanentError("视频下载地址必须与配置的 API 同源；不会向其他站点传递密钥")
        path = Path(path)
        part = path.with_suffix(".mp4.part")
        try:
            with httpx.stream("GET", url, headers=self._headers(profile), timeout=180,
                              trust_env=False, follow_redirects=False) as response:
                response.raise_for_status()
                size = 0
                with part.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > 1024 * 1024 * 1024:
                            raise PermanentError("视频下载超过 1 GiB 限制")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                expected = response.headers.get("content-length")
                if not size or (expected and size != int(expected)):
                    raise TransientError("视频下载不完整")
            os.replace(part, path)
            return {"sha256": sha256(path), "bytes": path.stat().st_size}
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise TransientError("视频下载失败") from exc
        finally:
            part.unlink(missing_ok=True)

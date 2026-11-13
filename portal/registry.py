"""Encrypted API profiles, capability verification, bindings and provider adapters."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import time
import uuid
import zlib
from contextlib import contextmanager
from urllib.parse import urlsplit, quote

from cryptography.fernet import Fernet
import httpx

ROLES = ("llm", "vision", "embedding", "rerank", "image", "speech", "music", "model3d", "video", "workflow")
MODULE_ROLES = {
    "knowledge": ("llm", "vision", "embedding", "rerank"),
    "image": ("llm", "image", "workflow"),
    "speech": ("llm", "speech"), "music": ("llm", "music"),
    "model3d": ("model3d",), "animation": ("vision", "image", "video"),
    "narrative": ("llm",),
}
PROTOCOL_KINDS = {
    "openai": {"llm", "vision", "embedding", "image", "speech"},
    "ollama": {"llm", "vision", "embedding"}, "rerank": {"rerank"},
    "comfyui": {"workflow"}, "videos": {"video"}, "rest": set(ROLES),
}


class RegistryError(ValueError):
    pass


class RegistryPermissionError(RegistryError):
    pass


def _base_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise RegistryError("API 地址无效")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise RegistryError("API 地址或端口无效") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise RegistryError("API 地址必须是 http(s) 地址，不能包含账号、参数或片段")
    return value.rstrip("/")


def _vector(value):
    return (isinstance(value, list) and len(value) >= 2 and
            all(type(x) in (int, float) and math.isfinite(x) for x in value))


def _vision_probe():
    """Small synthetic capability fixture: blue square centered on red background."""
    rows = []
    for y in range(64):
        rows.append(b"\0" + b"".join(b"\0\0\xff" if 16 <= x < 48 and 16 <= y < 48 else b"\xff\0\0" for x in range(64)))
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode()


class Registry:
    def __init__(self, data_dir, *, transport=None):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "model_registry.sqlite3"
        key_path = self.directory / "model-key.fernet"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.transport = transport
        with self.connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                protocol TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL,
                api_key TEXT NOT NULL, options TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, verified_revision INTEGER,
                verified_at REAL, capabilities TEXT NOT NULL DEFAULT '[]');
              CREATE TABLE IF NOT EXISTS bindings (
                module TEXT NOT NULL, role TEXT NOT NULL, profile_id TEXT NOT NULL
                  REFERENCES profiles(id) ON DELETE CASCADE,
                PRIMARY KEY(module, role));
            """)
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(profiles)')}
            if 'scope' not in columns:
                db.execute("ALTER TABLE profiles ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'")
            if 'owner_id' not in columns:
                db.execute('ALTER TABLE profiles ADD COLUMN owner_id TEXT')
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _context():
        from providers import current_model_context
        return current_model_context()

    def _visible(self, profile):
        context = self._context()
        actor = context.get('user') if context is not None else None
        return profile['scope'] == 'global' or (actor is not None and profile['owner_id'] == actor['id'])

    def _may_edit(self, profile):
        context = self._context()
        actor = context.get('user') if context is not None else None
        # A direct, in-process registry call without request context is the
        # maintenance interface for legacy global profiles. HTTP boundaries
        # always set an explicit context, including anonymous requests.
        return ((profile['scope'] == 'global' and (context is None or bool(actor and actor['admin'])))
                or (profile['scope'] == 'personal' and bool(actor and profile['owner_id'] == actor['id'])))

    def _authorize(self, profile, *, edit=False):
        if not (self._may_edit(profile) if edit else self._visible(profile)):
            raise RegistryPermissionError('模型配置不存在或无权访问')

    def _row(self, profile_id, *, edit=False):
        with self.connection() as db:
            row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            raise RegistryPermissionError('模型配置不存在或无权访问')
        result = dict(row)
        self._authorize(result, edit=edit)
        return result

    def _decode(self, row, private=False):
        value = dict(row)
        key = self.cipher.decrypt(value.pop("api_key").encode()).decode()
        value["options"] = json.loads(value["options"])
        value["capabilities"] = json.loads(value["capabilities"])
        value["verified"] = value["verified_revision"] == value["revision"]
        value["has_api_key"] = bool(key)
        value["api_key_mask"] = "••••••••" if key else ""
        value['can_manage'] = self._may_edit(value)
        if private:
            value["api_key"] = key
        return value

    def list_profiles(self):
        with self.connection() as db:
            return [self._decode(row) for row in db.execute("SELECT * FROM profiles ORDER BY rowid") if self._visible(row)]

    def get_profile(self, profile_id):
        return self._decode(self._row(profile_id))

    def save_profile(self, value, profile_id=None):
        if not isinstance(value, dict):
            raise RegistryError("配置必须是对象")
        allowed = {"name", "kind", "protocol", "base_url", "model", "api_key", "options", "scope"}
        if set(value) - allowed:
            raise RegistryError("配置包含未知字段")
        old = self._decode(self._row(profile_id, edit=True), True) if profile_id else None
        merged = {key: old[key] for key in allowed if old and key in old}
        merged.update(value)
        context = self._context()
        actor = context.get('user') if context is not None else None
        scope = merged.get('scope', 'global' if context is None else 'personal')
        if scope not in ('global', 'personal'):
            raise RegistryError('配置范围必须是全局或个人')
        if old and scope != old['scope']:
            raise RegistryError('配置范围创建后不能修改，请另建配置')
        owner_id = old['owner_id'] if old else (actor['id'] if scope == 'personal' and actor else None)
        if scope == 'personal' and not owner_id:
            raise RegistryPermissionError('个人配置需要登录账号')
        self._authorize({'scope': scope, 'owner_id': owner_id}, edit=True)
        name = merged.get("name", "")
        kind, protocol = merged.get("kind"), merged.get("protocol")
        model = merged.get("model", "")
        key = merged.get("api_key", "")
        options = merged.get("options", {})
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise RegistryError("请输入 1–100 字的配置名称")
        if not isinstance(protocol, str) or not isinstance(kind, str) or protocol not in PROTOCOL_KINDS or kind not in PROTOCOL_KINDS[protocol]:
            raise RegistryError("所选 API 协议不支持这个模型类型")
        if not isinstance(model, str) or len(model) > 256 or (not model.strip() and protocol != "comfyui"):
            raise RegistryError("请输入模型 ID")
        if not isinstance(key, str) or len(key) > 8192 or any(c in key for c in "\r\n"):
            raise RegistryError("API Key 无效")
        if not isinstance(options, dict) or len(json.dumps(options)) > 16000:
            raise RegistryError("高级参数必须是大小合理的 JSON 对象")
        if not isinstance(options.get("parameters", {}), dict):
            raise RegistryError("parameters 必须是对象")
        timeout = options.get("timeout", 180)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 1800:
            raise RegistryError("timeout 必须在 1–1800 秒之间")
        base_url = _base_url(merged.get("base_url"))
        identifier = profile_id or uuid.uuid4().hex
        encrypted = self.cipher.encrypt(key.encode()).decode()
        with self.connection() as db:
            if profile_id:
                updated = db.execute("""UPDATE profiles SET name=?,kind=?,protocol=?,base_url=?,model=?,api_key=?,options=?,
                    revision=revision+1,verified_revision=NULL,verified_at=NULL,capabilities='[]' WHERE id=?""",
                    (name.strip(), kind, protocol, base_url, model.strip(), encrypted, json.dumps(options), identifier))
                if updated.rowcount != 1:
                    raise RegistryError("模型配置不存在")
            else:
                db.execute("INSERT INTO profiles(id,name,kind,protocol,base_url,model,api_key,options,scope,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (identifier, name.strip(), kind, protocol, base_url, model.strip(), encrypted, json.dumps(options), scope, owner_id))
        return self.get_profile(identifier)

    def delete_profile(self, profile_id):
        self._row(profile_id, edit=True)
        with self.connection() as db:
            if db.execute("DELETE FROM profiles WHERE id=?", (profile_id,)).rowcount != 1:
                raise RegistryError("模型配置不存在")

    @staticmethod
    def _role(module, role):
        if module not in MODULE_ROLES or role not in MODULE_ROLES[module]:
            raise RegistryError("模块与模型用途不匹配")

    def list_bindings(self, *, effective=True):
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT b.* FROM bindings b JOIN profiles p ON b.profile_id=p.id WHERE p.scope='global' ORDER BY module,role")]
        context = self._context()
        if effective and context:
            mapping = {(row['module'], row['role']): row['profile_id'] for row in rows}
            for module, choices in context['selections'].items():
                for role, identifier in choices.items():
                    if identifier is None:
                        mapping.pop((module, role), None)
                    else:
                        # Untrusted selectors never expose a different owner's profile.
                        try:
                            self._row(identifier)
                        except RegistryError:
                            mapping.pop((module, role), None)
                        else:
                            mapping[(module, role)] = identifier
            rows = [{'module': module, 'role': role, 'profile_id': identifier} for (module, role), identifier in sorted(mapping.items())]
        return rows

    def bind(self, module, role, profile_id):
        self._role(module, role)
        self._authorize({'scope': 'global', 'owner_id': None}, edit=True)
        if profile_id is None:
            with self.connection() as db:
                db.execute("DELETE FROM bindings WHERE module=? AND role=?", (module, role))
            return
        if not isinstance(profile_id, str) or len(profile_id) > 64:
            raise RegistryError("模型配置 ID 无效")
        # Verify within the write transaction to avoid a concurrent edit restoring a stale binding.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if not row:
                raise RegistryError("模型配置不存在")
            self._authorize(row)
            profile = self._decode(row, True)
            if profile['scope'] != 'global':
                raise RegistryError('全局默认配置只能使用全局 API；个人 API 请在当前浏览器选择')
            self._require_verified(profile, role)
            db.execute("INSERT INTO bindings VALUES(?,?,?) ON CONFLICT(module,role) DO UPDATE SET profile_id=excluded.profile_id",
                       (module, role, profile_id))

    @staticmethod
    def _require_verified(profile, role):
        if not profile["verified"]:
            raise RegistryError("请先通过模型类型检测；修改配置后需要重新检测")
        if role not in profile["capabilities"] or profile['kind'] not in ({'llm', 'vision'} if role == 'llm' else {role}):
            if role == "vision":
                raise RegistryError("此用途需要识图模型；请将配置类型设为 Vision，并通过图片识别能力检测")
            raise RegistryError("此配置的模型能力与用途不匹配")

    def resolve(self, module, role, profile_id=None):
        self._role(module, role)
        context = self._context()
        if context is not None and context['user'] is None:
            raise RegistryPermissionError('调用模型前请先登录')
        choices = context['selections'].get(module, {}) if context else {}
        if profile_id is None and role in choices:
            profile_id = choices[role]
            if profile_id is None:
                raise RegistryError('当前浏览器未选择此用途的模型')
        if not profile_id:
            with self.connection() as db:
                row = db.execute("SELECT profile_id FROM bindings WHERE module=? AND role=?", (module, role)).fetchone()
            if not row:
                raise RegistryError("请先在 AI 模型配置中选择此模块使用的模型")
            profile_id = row["profile_id"]
        profile = self._decode(self._row(profile_id), True)
        self._require_verified(profile, role)
        return profile

    async def _request(self, profile, path, payload=None, *, timeout=None, binary=False):
        headers = {"Authorization": "Bearer " + profile["api_key"]} if profile["api_key"] else {}
        try:
            async with httpx.AsyncClient(timeout=timeout or profile["options"].get("timeout", 180),
                    transport=self.transport, trust_env=False, follow_redirects=False) as client:
                response = await client.request("GET" if payload is None else "POST",
                    profile["base_url"] + path, headers=headers, json=payload)
            if response.status_code >= 300:
                raise RegistryError(f"API 请求未成功（HTTP {response.status_code}）；请检查地址、Key、模型 ID 与能力类型")
            if binary:
                return {"data_base64": base64.b64encode(response.content).decode(),
                        "mime_type": response.headers.get("content-type", "application/octet-stream")}
            result = response.json()
            if not isinstance(result, dict):
                raise RegistryError("API 返回结构不符合所选协议")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, RegistryError):
                raise
            raise RegistryError("无法连接 API，或 API 未返回合法 JSON；请检查服务地址和协议") from None

    async def _generated_image_probe(self, profile):
        result = await self._request(profile, "/images/generations", {"model": profile["model"],
            "prompt": "A solid blue square on a plain white background.", "n": 1, "size": "1024x1024"})
        try:
            first = result["data"][0]
            if first.get("b64_json"):
                if len(first["b64_json"]) > 32 * 1024 * 1024:
                    raise ValueError()
                raw = base64.b64decode(first["b64_json"], validate=True)
            else:
                # Signed artifact URLs may contain a query. Never forward API credentials.
                raw = await self._download_probe(first["url"])
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.width < 1 or picture.height < 1 or picture.width * picture.height > 40_000_000:
                    raise ValueError()
                picture.verify()
        except (KeyError, IndexError, TypeError, ValueError, OSError):
            raise RegistryError("图片接口未返回可解码的图片，检测未通过") from None

    async def _download_probe(self, value):
        parsed = urlsplit(value) if isinstance(value, str) else None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise RegistryError("图片接口返回的文件地址无效")
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=30) as client:
                async with client.stream("GET", value) as response:
                    if response.status_code != 200:
                        raise RegistryError("图片文件无法读取")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 24 * 1024 * 1024:
                            raise RegistryError("检测图片超过 24 MiB 限制")
                    return bytes(data)
        except httpx.HTTPError:
            raise RegistryError("无法读取图片检测结果") from None

    async def _generated_speech_probe(self, profile):
        result = await self._request(profile, "/audio/speech", {"model": profile["model"], "input": "Test.",
            "voice": profile["options"].get("parameters", {}).get("voice", "alloy"), "response_format": "wav"}, binary=True)
        raw = base64.b64decode(result["data_base64"])
        valid = len(raw) > 32 and (raw[:3] == b"ID3" or raw[:4] in (b"fLaC", b"OggS") or
            raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
        if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            try:
                import wave
                with wave.open(io.BytesIO(raw)) as stream:
                    valid = stream.getnframes() > 0 and stream.getnchannels() > 0 and bool(stream.readframes(1))
            except (ValueError, EOFError, wave.Error):
                valid = False
        if not valid:
            raise RegistryError("语音接口未返回有效音频格式，检测未通过")

    async def verify(self, profile_id, *, allow_generation_probe=False):
        profile = self._decode(self._row(profile_id, edit=True), True)
        kind, protocol = profile["kind"], profile["protocol"]
        # Invalidate the previous result before a fresh probe, including when a provider has changed.
        with self.connection() as db:
            db.execute("UPDATE profiles SET verified_revision=NULL,verified_at=NULL,capabilities='[]' WHERE id=? AND revision=?",
                       (profile_id, profile["revision"]))
        capabilities = [kind]
        if protocol == "comfyui":
            state = await self._request(profile, "/system_stats", timeout=20)
            nodes = await self._request(profile, "/object_info", timeout=20)
            if not isinstance(state.get("system"), dict) or not isinstance(state.get("devices"), list) or not nodes or not any(
                    isinstance(value, dict) and isinstance(value.get("input"), dict) for value in nodes.values()):
                raise RegistryError("目标不是兼容的 ComfyUI 工作流服务")
        elif protocol == "openai" and kind in ("image", "speech") and allow_generation_probe:
            if kind == "image":
                await self._generated_image_probe(profile)
            else:
                await self._generated_speech_probe(profile)
        elif protocol == "videos":
            listing = await self._request(profile, "/models", timeout=30)
            models = listing.get("data")
            match = next((row for row in models if isinstance(row, dict) and row.get("id") == profile["model"]), None) if isinstance(models, list) else None
            if not match or not isinstance(match.get("capabilities"), list) or "video" not in match["capabilities"]:
                raise RegistryError("Videos API 必须在 /models 的配置模型条目中明确声明 capabilities:[video]；不能仅凭名称识别类型")
            jobs = await self._request(profile, "/videos?limit=1", timeout=30)
            if not isinstance(jobs.get("data"), list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or
                    not isinstance(row.get("status"), str) for row in jobs["data"]):
                raise RegistryError("Videos API 的任务查询结构不符合协议")
        elif protocol == "rest" or (protocol == "openai" and kind in ("image", "speech")):
            # Generative endpoints have no free universal probe. Require an explicit typed, non-generating contract.
            try:
                declaration = await self._request(profile, "/capabilities", timeout=20)
            except RegistryError:
                if protocol == "openai":
                    raise RegistryError("该服务未提供免生成的类型声明。若确认可以调用，请勾选“允许生成一次用于检测”后重试，可能产生 API 费用") from None
                raise
            declared = declaration.get("capabilities")
            if (declaration.get("model") != profile["model"] or not isinstance(declared, list) or kind not in declared
                    or declaration.get("api_version") != "fantasy-v1"
                    or not isinstance(declaration.get("input_schema"), dict) or declaration["input_schema"].get("type") != "object"
                    or not isinstance(declaration.get("output_schema"), dict) or declaration["output_schema"].get("type") != "object"):
                raise RegistryError("API 未明确声明 fantasy-v1 模型类型；请使用文档中的兼容适配接口")
            result = await self._request(profile, "/validate", {"model": profile["model"], "kind": kind,
                "validate_only": True, "input": {"prompt": "connection test"}}, timeout=30)
            if result.get("valid") is not True or result.get("kind") != kind or result.get("model") != profile["model"]:
                raise RegistryError("API 模型结构验证未通过")
            if kind == "vision" and "llm" in declared:
                check = await self._request(profile, "/validate", {"model": profile["model"], "kind": "llm",
                    "validate_only": True, "input": {"prompt": "connection test"}}, timeout=30)
                if check.get("valid") is True and check.get("kind") == "llm" and check.get("model") == profile["model"]:
                    capabilities.append("llm")
            if kind == "music" and "music_train" in declared:
                check = await self._request(profile, "/validate", {"model": profile["model"], "kind": "music_train",
                    "validate_only": True, "input": {}}, timeout=30)
                if check.get("valid") is True and check.get("kind") == "music_train" and check.get("model") == profile["model"]:
                    capabilities.append("music_train")
        elif protocol == "rerank":
            result = await self._request(profile, "/rerank", {"model": profile["model"], "query": "blue",
                "documents": ["blue sky", "red flower"], "top_n": 2}, timeout=45)
            rows = result.get("results")
            if (not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or
                    type(row.get("index")) is not int or row["index"] not in (0, 1) or
                    type(row.get("relevance_score")) not in (int, float) or not math.isfinite(row["relevance_score"]) for row in rows)):
                raise RegistryError("接口未返回重排序分数，不能作为 Reranker 使用")
        else:
            if protocol == "ollama":
                details = await self._request(profile, "/api/show", {"model": profile["model"]}, timeout=30)
                declared = details.get("capabilities", [])
                required = {"llm": "completion", "vision": "vision", "embedding": "embedding"}[kind]
                if required not in declared:
                    raise RegistryError("Ollama 模型声明的能力与选择的模型类型不符")
            if kind == "embedding":
                result = await self._invoke_profile(profile, kind, {"input": ["capability test"]})
                rows = result.get("data")
                if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or not _vector(rows[0].get("embedding")):
                    raise RegistryError("接口未返回有效向量，不能作为 Embedding 使用")
            else:
                content = "Reply with the single word READY."
                if kind == "vision":
                    content = [{"type": "text", "text": "What color is the square in the CENTER of this image? Reply with only the English color name."},
                               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _vision_probe()}}]
                result = await self._invoke_profile(profile, kind, {"messages": [{"role": "user", "content": content}],
                    "max_tokens": 80, "temperature": 0})
                try:
                    answer = result["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    raise RegistryError("接口未返回聊天回答，不能作为 LLM 使用") from None
                if not isinstance(answer, str) or not answer.strip():
                    raise RegistryError("LLM 未返回文本回答")
                if kind == "vision":
                    if answer.strip().lower().strip(" .!\n\"'") not in ("blue", "蓝色", "蓝"):
                        raise RegistryError("图片识别能力检测未通过，请选择支持视觉输入的模型")
                    capabilities.append("llm")
        with self.connection() as db:
            changed = db.execute("UPDATE profiles SET verified_revision=revision,verified_at=?,capabilities=? WHERE id=? AND revision=?",
                (time.time(), json.dumps(capabilities), profile_id, profile["revision"]))
            if changed.rowcount != 1:
                raise RegistryError("检测期间配置已修改，请重新检测")
        return self.get_profile(profile_id)

    async def invoke(self, module, role, payload, profile_id=None, *, expected_fingerprint=None):
        if not isinstance(payload, dict):
            raise RegistryError("模型请求必须为对象")
        profile = self.resolve(module, role, profile_id)
        if expected_fingerprint is not None:
            from providers import fingerprint
            if fingerprint(profile) != expected_fingerprint:
                raise RegistryError('模型配置已改变，请重新开始本次操作')
        return await self._invoke_profile(profile, role, payload)

    async def poll(self, module, role, job_id, profile_id=None):
        """Poll the typed REST job contract or ComfyUI history with the same profile."""
        if not isinstance(job_id, str) or not 1 <= len(job_id) <= 160 or not all(c.isalnum() or c in "_-" for c in job_id):
            raise RegistryError("任务 ID 无效")
        profile = self.resolve(module, role, profile_id)
        prefix = {"rest": "/jobs/", "comfyui": "/history/", "videos": "/videos/"}.get(profile["protocol"])
        if prefix is None:
            raise RegistryError("此协议不支持异步任务轮询")
        return await self._request(profile, prefix + quote(job_id, safe=""))

    async def _invoke_profile(self, profile, role, payload):
        protocol = profile["protocol"]
        data = {**profile["options"].get("parameters", {}), **payload, "model": profile["model"]}
        if protocol == "rest":
            return await self._request(profile, "/generate", {**data, "kind": role})
        if protocol == "comfyui":
            prompt = payload.get("prompt")
            if not isinstance(prompt, dict) or not prompt:
                raise RegistryError("ComfyUI 请求需要 API 格式的 prompt 工作流对象")
            return await self._request(profile, "/prompt", {"prompt": prompt, "client_id": payload.get("client_id", uuid.uuid4().hex)})
        if protocol == "rerank":
            return await self._request(profile, "/rerank", data)
        if protocol == "videos":
            return await self._request(profile, "/videos", data)
        if protocol == "openai":
            if role in ("llm", "vision"):
                return await self._request(profile, "/chat/completions", {**data, "stream": False})
            if role == "embedding":
                return await self._request(profile, "/embeddings", data)
            if role == "image":
                return await self._request(profile, "/images/generations", data)
            if role == "speech":
                return await self._request(profile, "/audio/speech", data, binary=True)
        if protocol == "ollama":
            if role == "embedding":
                result = await self._request(profile, "/api/embed", {"model": profile["model"], "input": payload.get("input")})
                values = result.get("embeddings")
                if not isinstance(values, list):
                    raise RegistryError("Ollama 未返回向量")
                return {"model": profile["model"], "data": [{"index": i, "embedding": value} for i, value in enumerate(values)]}
            messages = []
            for message in payload.get("messages", []):
                content = message.get("content", "")
                images = list(message.get("images", []))
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if part.get("type") == "text":
                            parts.append(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            image = part.get("image_url", {}).get("url", "")
                            if not image.startswith("data:image/") or ";base64," not in image:
                                raise RegistryError("Ollama 视觉输入需要内联 base64 图片")
                            images.append(image.split(";base64,", 1)[1])
                    content = "\n".join(parts)
                messages.append({"role": message.get("role", "user"), "content": content, **({"images": images} if images else {})})
            options = {key: data[key] for key in ("temperature", "top_p", "seed") if key in data}
            if "max_tokens" in data:
                options["num_predict"] = data["max_tokens"]
            request = {"model": profile["model"], "messages": messages, "stream": False, "options": options}
            response_format = data.get("response_format", {})
            if isinstance(response_format, dict) and response_format.get("type") == "json_object":
                request["format"] = "json"
            elif isinstance(response_format, dict) and response_format.get("type") == "json_schema":
                schema = response_format.get("json_schema", {}).get("schema")
                if isinstance(schema, dict):
                    request["format"] = schema
            result = await self._request(profile, "/api/chat", request)
            if not isinstance(result.get("message"), dict):
                raise RegistryError("Ollama 未返回聊天消息")
            return {"model": profile["model"], "choices": [{"index": 0, "message": result["message"], "finish_reason": "stop"}]}
        raise RegistryError("模型协议不支持此调用用途")

"""Client for Fantasy Main's GitLab SSO and existing production HTTP APIs.

Only the public gateway is used. Gateway signing keys and internal administrator
endpoints are deliberately unnecessary. Login cookies stay in this process.
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from uuid import uuid4

import httpx

from .common import AdapterError, create_http_client, download_to_file, redact, safe_component
from .config import Settings
from .effects import EffectsMixin


MEDIA_MODULES = frozenset({"model3d", "image", "speech", "music"})


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class _Forms(HTMLParser):
    """Read native GitLab forms and its non-JavaScript OAuth callback link."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self.current = {"action": values.get("action", ""),
                            "method": values.get("method", "get").lower(), "fields": {}}
        elif tag == "input" and self.current is not None and values.get("name"):
            if values.get("type", "").lower() not in {"submit", "button", "image", "file"} and "disabled" not in values:
                self.current["fields"][values["name"]] = values.get("value", "")
        elif tag == "meta" and values.get("name"):
            self.meta[values["name"]] = values.get("content", "")
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"])

    def handle_endtag(self, tag):
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


class PrivateBackend(EffectsMixin):
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.http = create_http_client(settings, transport=transport)
        self._authenticated = False
        self._login_lock = asyncio.Lock()
        self._csrf: dict[str, str] = {}
        self._oauth_state: str | None = None
        # Retained across reauthentication: a running agent must never switch owners silently.
        self._effects_owner: str | None = None

    def _module(self, module: str) -> str:
        module = "animation" if module == "workflow" else module
        if module not in MEDIA_MODULES | {"animation", "effects"}:
            raise AdapterError("Supported private modules: animation, model3d, image, speech, music, effects.")
        return module

    def _base(self, module: str) -> str:
        module = self._module(module)
        if module == "animation" and "animation" not in self.settings.module_urls and "workflow" in self.settings.module_urls:
            module = "workflow"
        return self.settings.module_url(module).rstrip("/")

    def _login_url(self, url: str) -> str:
        parts = urlsplit(url)
        allowed = {_origin(self.settings.portal_url), _origin(self.settings.gitlab_url)}
        if (_origin(url) not in allowed or parts.username or parts.password or parts.fragment
                or "\\" in url or parts.scheme != "https"):
            raise AdapterError("Login tried to leave the configured HTTPS identity origins.")
        if _origin(url) == _origin(self.settings.gitlab_url) and parts.path == "/oauth/authorize":
            states = parse_qs(parts.query).get("state", [])
            if states:
                if len(states) != 1 or (self._oauth_state is not None and self._oauth_state != states[0]):
                    raise AdapterError("GitLab OAuth state changed during login.")
                self._oauth_state = states[0]
        if _origin(url) == _origin(self.settings.portal_url) and parts.path == "/auth/callback":
            query = parse_qs(parts.query)
            if not self._oauth_state or query.get("state") != [self._oauth_state] or len(query.get("code", [])) != 1:
                raise AdapterError("GitLab OAuth callback did not match the current login.")
        return url

    async def _login_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await self.http.request(method, self._login_url(url), **kwargs)
            for _ in range(16):
                if response.status_code not in {301, 302, 303}:
                    if response.status_code in {307, 308}:
                        raise AdapterError("Unexpected method-preserving redirect during login.")
                    return response
                target = urljoin(str(response.url), response.headers.get("location", ""))
                response = await self.http.get(self._login_url(target))
        except httpx.HTTPError:
            raise AdapterError("Could not contact the configured login service; check network and TLS trust.") from None
        raise AdapterError("GitLab login exceeded its redirect limit.")

    async def _ensure_login(self) -> None:
        if self._authenticated:
            return
        async with self._login_lock:
            if self._authenticated:
                return
            username, password = self.settings.credentials()
            self._oauth_state = None
            self._csrf.clear()
            # A stale central cookie must not short-circuit the OAuth flow.
            for cookie in list(self.http.cookies.jar):
                if cookie.name == "fantasy_session":
                    self.http.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
            response = await self._login_request("GET", self.settings.portal_url.rstrip("/") + "/login")
            submitted = False
            for _ in range(8):
                names = {c.name for c in self.http.cookies.jar}
                if {"fantasy_session", "_gitlab_session"} <= names:
                    self._authenticated = True
                    return
                parts = urlsplit(str(response.url))
                if response.status_code != 200 or _origin(str(response.url)) != _origin(self.settings.gitlab_url):
                    raise AdapterError("GitLab SSO did not reach its expected sign-in page.")
                forms = _Forms(response.text)
                if parts.path == "/oauth/authorize":
                    callbacks = []
                    for link in forms.links:
                        target = urljoin(str(response.url), link)
                        parsed = urlsplit(target)
                        query = parse_qs(parsed.query)
                        if (f"{_origin(target)}{parsed.path}" == self.settings.portal_url.rstrip("/") + "/auth/callback"
                                and not parsed.fragment and not parsed.username and not parsed.password
                                and self._oauth_state and query.get("state") == [self._oauth_state]
                                and len(query.get("code", [])) == 1):
                            callbacks.append(target)
                    if callbacks:
                        if len(callbacks) != 1:
                            raise AdapterError("GitLab returned ambiguous OAuth callback links.")
                        response = await self._login_request("GET", callbacks[0])
                        continue
                candidates = [f for f in forms.forms if {"user[login]", "user[password]"} <= f["fields"].keys()]
                if candidates:
                    if len(candidates) != 1 or submitted:
                        raise AdapterError("GitLab rejected the configured credentials; automatic retries stopped.")
                    chosen = candidates[0]
                    fields = {**chosen["fields"], "user[login]": username,
                              "user[password]": password, "user[remember_me]": "0"}
                    submitted = True
                    required_path = "/users/sign_in"
                else:
                    if any("user[new_password]" in f["fields"] for f in forms.forms):
                        raise AdapterError("GitLab requires a password update. Update the credential file after changing it in the browser.")
                    candidates = [f for f in forms.forms
                                  if urlsplit(urljoin(str(response.url), f["action"])).path == "/oauth/authorize"
                                  and f["method"] == "post" and f["fields"].get("_method", "").lower() != "delete"]
                    if len(candidates) != 1:
                        raise AdapterError("GitLab requires an interactive sign-in step or an unsupported authorization form.")
                    chosen = candidates[0]
                    fields = {**chosen["fields"], "commit": "Authorize"}
                    required_path = "/oauth/authorize"
                target = self._login_url(urljoin(str(response.url), chosen["action"]))
                if (_origin(target) != _origin(self.settings.gitlab_url)
                        or urlsplit(target).path != required_path or chosen["method"] != "post"):
                    raise AdapterError("GitLab login form destination is not the configured identity service.")
                if not fields.get("authenticity_token"):
                    fields["authenticity_token"] = forms.meta.get("csrf-token", "")
                if not fields.get("authenticity_token"):
                    raise AdapterError("GitLab login form is missing CSRF protection.")
                response = await self._login_request("POST", target, data=fields,
                                                     headers={"Origin": _origin(target), "Referer": str(response.url)})
            raise AdapterError("GitLab login exceeded its form limit.")

    async def _api(self, module: str, method: str, path: str, **kwargs) -> dict:
        await self._ensure_login()
        base = self.settings.portal_url.rstrip("/") if module == "portal" else self._base(module)
        headers = {"Accept": "application/json", **kwargs.pop("headers", {})}
        if method not in {"GET", "HEAD"}:
            headers["Origin"] = _origin(base)
            if module in MEDIA_MODULES:
                if module not in self._csrf:
                    session = await self._api(module, "GET", "/api/session")
                    if not isinstance(session.get("csrf"), str):
                        raise AdapterError("The module did not provide its CSRF token.")
                    self._csrf[module] = session["csrf"]
                headers["X-CSRF-Token"] = self._csrf[module]
        for attempt in range(2):
            try:
                response = await self.http.request(method, base + path, headers=headers, **kwargs)
            except httpx.HTTPError:
                suffix = " Its submission status is unknown; inspect jobs before resubmitting." if method != "GET" else ""
                raise AdapterError("Fantasy HTTP request failed." + suffix) from None
            login_redirect = False
            if response.status_code in {301, 302, 303}:
                target = urljoin(str(response.url), response.headers.get("location", ""))
                login_redirect = (_origin(target) in {_origin(self.settings.portal_url), _origin(self.settings.gitlab_url)}
                                  and urlsplit(target).path in {"/login", "/users/sign_in"})
            if response.status_code == 401 or login_redirect:
                self._authenticated = False
                self._csrf.clear()
                if method == "GET" and attempt == 0:
                    await self._ensure_login()
                    continue
                raise AdapterError("The session expired before the request was accepted; retry to sign in again.")
            if response.status_code >= 400:
                # Do not echo raw server bodies: errors can contain upstream secrets.
                raise AdapterError(f"Fantasy {module} returned HTTP {response.status_code}; check module readiness, permissions, and input parameters.")
            if response.status_code >= 300:
                raise AdapterError("Unexpected redirect from the Fantasy API.")
            try:
                result = response.json()
            except ValueError:
                raise AdapterError("Fantasy returned an unexpected non-JSON API response.") from None
            if not isinstance(result, dict):
                raise AdapterError("Fantasy returned an unexpected API response shape.")
            return result
        raise AdapterError("Could not restore the Fantasy login session.")

    async def capabilities(self) -> dict:
        portal = redact(await self._api("portal", "GET", "/api/portal"))
        async def inspect(module):
            try:
                if module == "effects":
                    guide = await self.effects_guide()
                    return module, {"available": True, "kind": "particle-effects-editor", "limits": guide.get("limits", {}),
                                    "usage": "get_effects_guide, list_effects/read_effect, validate_effect, save_effect, export_effect"}
                if module == "animation":
                    health = await self._api(module, "GET", "/api/health", params={"prompt_mode": "direct"})
                    return module, {"available": True, "health": redact(health), "output": "2D transparent frames and spritesheets"}
                return module, {"available": True, "config": redact(await self._api(module, "GET", "/api/config"))}
            except AdapterError as error:
                return module, {"available": False, "error": str(error)}
        modules = dict(await asyncio.gather(*(inspect(m) for m in ("animation", "model3d", "image", "speech", "music", "effects"))))
        return {"backend": "private", "portal": portal, "modules": modules,
                "notes": ["Animation produces transparent frames and spritesheets for the current HD-2D workflow.",
                          "Media job submission has no server idempotency; do not retry uncertain submissions automatically."]}

    async def list_references(self) -> dict:
        return redact(await self._api("animation", "GET", "/api/catalog"))

    async def list_jobs(self, module: str, limit: int = 20) -> dict:
        module = self._module(module)
        if module == "effects":
            raise AdapterError("Effects uses list_effects/read_effect instead of media jobs.")
        if not 1 <= limit <= 500:
            raise AdapterError("limit must be between 1 and 500.")
        result = await self._api(module, "GET", "/api/jobs", params={"limit": limit} if module == "animation" else {})
        key = "items" if module == "animation" else "jobs"
        result[key] = result.get(key, [])[:limit]
        return redact(result)

    async def get_job(self, module: str, job_id: str) -> dict:
        if module == "effects":
            raise AdapterError("Effects uses read_effect instead of media jobs.")
        return redact(await self._api(self._module(module), "GET", f"/api/jobs/{safe_component(job_id)}"))

    async def list_assets(self, module: str, limit: int = 20) -> dict:
        module = self._module(module)
        if not 1 <= limit <= 500:
            raise AdapterError("limit must be between 1 and 500.")
        if module == "animation":
            return redact(await self._api(module, "GET", "/api/assets", params={"limit": limit}))
        jobs = await self.list_jobs(module, limit)
        return {"items": [{"job_id": job["id"], **artifact} for job in jobs.get("jobs", [])
                          for artifact in job.get("artifacts", [])]}

    @staticmethod
    def _file(path: Path, suffixes: set[str], maximum: int) -> Path:
        try:
            path = Path(path).expanduser().resolve(strict=True)
            if not path.is_file() or path.suffix.lower() not in suffixes or not 0 < path.stat().st_size <= maximum:
                raise AdapterError("The upload has an unsupported type or size.")
        except OSError:
            raise AdapterError("The upload file could not be read.") from None
        return path

    async def submit_job(self, module: str, model_id: str, params: dict, file_path: Path | None = None,
                         prompt_mode: str = "direct") -> dict:
        module = self._module(module)
        if module not in MEDIA_MODULES:
            raise AdapterError("Use submit_animation for animation tasks.")
        if prompt_mode not in {"direct", "assisted"} or not isinstance(params, dict):
            raise AdapterError("Invalid prompt mode or generation parameters.")
        fields = {"model_id": safe_component(model_id), "params": json.dumps(params, ensure_ascii=False, allow_nan=False),
                  "prompt_mode": prompt_mode}
        if file_path is None:
            if module == "model3d":
                raise AdapterError("3D generation requires one reference image.")
            result = await self._api(module, "POST", "/api/jobs", data=fields,
                                     timeout=max(self.settings.timeout_seconds, 300))
        else:
            maximum = 200 * 1024 * 1024 if module == "image" else 16 * 1024 * 1024
            suffixes = {".png", ".jpg", ".jpeg", ".webp"}
            if module == "music" and Path(file_path).suffix.lower() == ".zip":
                maximum, suffixes = 256 * 1024 * 1024, {".zip"}
            file_path = self._file(file_path, suffixes, maximum)
            with file_path.open("rb") as stream:
                result = await self._api(module, "POST", "/api/jobs", data=fields,
                                         timeout=max(self.settings.timeout_seconds, 300),
                                         files={"file": (file_path.name, stream, mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")})
        return redact(result)

    async def submit_animation(self, task: dict) -> dict:
        payload = {**task}
        key = payload.get("idempotency_key") or str(uuid4())
        if not isinstance(key, str) or len(key) > 200:
            raise AdapterError("Animation idempotency_key must be a string of at most 200 characters.")
        payload["idempotency_key"] = key
        try:
            result = await self._api("animation", "POST", "/api/animation/generate", json=payload,
                                     headers={"Idempotency-Key": key})
        except AdapterError as error:
            raise AdapterError(f"{error} Preserve idempotency_key={key} when recovering this submission.") from None
        return redact({**result, "idempotency_key": key})

    async def submit_animation_batch(self, batch: dict) -> dict:
        payload = {**batch}
        batch_id = payload.get("batch_id") or str(uuid4())
        safe_component(batch_id)
        payload["batch_id"] = batch_id
        try:
            result = await self._api("animation", "POST", "/api/batches", json=payload)
        except AdapterError as error:
            raise AdapterError(f"{error} Preserve batch_id={batch_id} when recovering this submission.") from None
        return redact({**result, "batch_id": batch_id})

    async def upload_reference(self, file_path: Path, kind: str = "character") -> dict:
        kind = "character" if kind == "character-reference" else kind
        if kind not in {"character", "motion"}:
            raise AdapterError("Reference kind must be character or motion.")
        suffixes = {".png", ".jpg", ".jpeg", ".webp"} if kind == "character" else {".mp4", ".mov", ".webm", ".mkv"}
        file_path = self._file(file_path, suffixes, 200 * 1024 * 1024)
        with file_path.open("rb") as stream:
            return redact(await self._api("animation", "POST", "/api/uploads", data={"kind": kind},
                                          files={"file": (file_path.name, stream, mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")}))

    async def compile_animation(self, task: dict) -> dict:
        return redact(await self._api("animation", "POST", "/api/compile", json=task))

    async def create_character(self, body: dict) -> dict:
        """Create a draft from paths returned by upload_reference; no auto-approval."""
        return redact(await self._api("animation", "POST", "/api/characters", json=body))

    async def create_motion(self, body: dict) -> dict:
        return redact(await self._api("animation", "POST", "/api/motions", json=body))

    def _download_url(self, module: str, url: str) -> str:
        base = self._base(module)
        absolute = urljoin(base + "/", url)
        parts = urlsplit(absolute)
        decoded = unquote(parts.path)
        if (_origin(absolute) != _origin(base) or parts.username or parts.password or parts.fragment
                or "\\" in absolute or "\\" in decoded or ".." in decoded.split("/")):
            raise AdapterError("The artifact URL is outside the configured module origin.")
        if module in {"animation", "workflow"}:
            allowed = parts.path.startswith("/files/") or (parts.path.startswith("/api/assets/") and parts.path.endswith("/export.zip"))
        else:
            allowed = parts.path.startswith("/api/jobs/") and "/files/" in parts.path
        if not allowed:
            raise AdapterError("The URL is not a supported artifact download route.")
        return absolute

    async def get_downloads(self, module: str, job_id: str | None = None, asset_id: str | None = None) -> list[dict]:
        module = self._module(module)
        if module != "animation":
            if not job_id or asset_id:
                raise AdapterError("Media artifact downloads require job_id only.")
            job = await self.get_job(module, job_id)
            return [{"url": self._download_url(module, a["url"]), "name": a.get("name") or Path(urlsplit(a["url"]).path).name,
                     "kind": a.get("kind")} for a in job.get("artifacts", []) if isinstance(a.get("url"), str)]
        if not asset_id:
            if not job_id:
                raise AdapterError("Animation downloads require a job_id or asset_id.")
            job = await self.get_job(module, job_id)
            asset_id = job.get("asset_id")
            if not asset_id:
                return []
        asset = await self._api(module, "GET", f"/api/assets/{safe_component(asset_id)}")
        result = []
        if isinstance(asset.get("export_url"), str):
            result.append({"url": self._download_url(module, asset["export_url"]), "name": f"animation-{asset_id}.zip", "kind": "bundle"})
        for kind, value in asset.get("files", {}).items():
            for url in value if isinstance(value, list) else [value]:
                if isinstance(url, str):
                    result.append({"url": self._download_url(module, url), "name": unquote(Path(urlsplit(url).path).name), "kind": kind})
        return result

    async def download(self, url: str, destination: Path) -> dict:
        module = next((m for m in ("animation", "model3d", "image", "speech", "music") if _origin(url) == _origin(self._base(m))), None)
        if module is None:
            raise AdapterError("Download URL must be an absolute URL from a configured module.")
        url = self._download_url(module, url)
        # A safe read refreshes expired sessions before the streaming request.
        await self._api(module, "GET", "/api/auth/session" if module == "animation" else "/api/session")
        return await download_to_file(self.http, url, destination, self.settings, headers={"Accept": "application/octet-stream"})

    async def close(self) -> None:
        await self.http.aclose()

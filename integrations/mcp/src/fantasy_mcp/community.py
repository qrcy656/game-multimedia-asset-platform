"""Community edition: GitLab PAT or local accounts, one origin, two CSRF layers.

Generation payloads and result formats follow the same public workbench APIs as
Fantasy Main. Only their transport/authentication boundary differs. No internal
identity keys, provider credentials, or administrator APIs are needed here.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import socket
import subprocess
from urllib.parse import unquote, urljoin, urlsplit

import anyio
import httpx

from .common import AdapterError, download_to_file, redact
from .config import Settings, url_origin
from .private import MEDIA_MODULES, PrivateBackend


class CommunityBackend(PrivateBackend):
    """Reuse workbench operations through the community portal's login."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._origin = settings.origin or url_origin(settings.portal_url)
        origin = urlsplit(self._origin)
        portal = urlsplit(settings.portal_url)
        if origin.path or origin.query or origin.fragment or origin.scheme != portal.scheme:
            raise AdapterError("Community origin must be a plain HTTP(S) origin matching the portal scheme.")
        for base in settings.module_urls.values():
            if url_origin(base) != url_origin(settings.portal_url) or urlsplit(base).query:
                raise AdapterError("All community modules must use the same portal origin.")
        super().__init__(settings, transport)
        self._portal_csrf: str | None = None
        self._tunnel: asyncio.subprocess.Process | None = None
        # SSH forwarding may use a different local port. The server's CSRF
        # origin and Host must still agree with its configured APP_ORIGIN.
        self._host_headers = {"Host": origin.netloc}

    async def _ensure_tunnel(self) -> None:
        options = self.settings.ssh_tunnel
        if not options:
            return
        if self._tunnel is not None:
            if self._tunnel.returncode is not None:
                raise AdapterError("The community SSH tunnel exited; reconnect the MCP server.")
            return
        if not isinstance(options, dict) or set(options) - {"target", "local_port", "remote_port", "ssh_port"}:
            raise AdapterError("ssh_tunnel accepts target, local_port, remote_port and optional ssh_port.")
        target = options.get("target", "")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9_.-]*", target):
            raise AdapterError("ssh_tunnel.target must be a user@hostname using SSH key authentication.")
        ports = [options.get("local_port"), options.get("remote_port", 18082), options.get("ssh_port", 22)]
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
            raise AdapterError("SSH ports must be integers between 1 and 65535.")
        local_port, remote_port, ssh_port = ports
        portal = urlsplit(self.settings.portal_url)
        if portal.hostname not in {"localhost", "127.0.0.1"} or portal.port != local_port:
            raise AdapterError("The tunneled portal_url must use localhost or 127.0.0.1 and ssh_tunnel.local_port.")
        # Never adopt/terminate somebody else's tunnel or existing listener.
        try:
            with socket.socket() as check:
                check.bind(("127.0.0.1", local_port))
        except OSError:
            raise AdapterError("The configured community tunnel port is already occupied; choose a separate local_port.") from None
        command = ["ssh", "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                   "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                   "-p", str(ssh_port), "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}", target]
        environment = dict(os.environ)
        if os.name == "nt" and not any(key.lower() == "programdata" for key in environment):
            # MCP's minimal Windows environment omits ProgramData. Windows
            # OpenSSH exits 255 before printing diagnostics without it. Read
            # the actual OS-known folder instead of assuming a drive/path.
            import ctypes
            folder = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x23, None, 0, folder) != 0 or not folder.value:
                raise AdapterError("Could not resolve the Windows ProgramData directory required by SSH.")
            environment["ProgramData"] = folder.value
        try:
            starting = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env=environment,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            ))
            try:
                self._tunnel = await asyncio.shield(starting)
            except asyncio.CancelledError:
                # Do not lose the process handle if cancellation lands exactly
                # while Python is spawning ssh.
                with anyio.CancelScope(shield=True):
                    self._tunnel = await starting
                raise
            for _ in range(120):
                if self._tunnel.returncode is not None:
                    raise AdapterError("Community SSH forwarding failed; check the SSH key, known host and port.")
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", local_port), 0.25)
                    writer.close()
                    await writer.wait_closed()
                    return
                except (OSError, asyncio.TimeoutError):
                    await asyncio.sleep(0.1)
            raise AdapterError("Community SSH forwarding did not become ready.")
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await self._close_tunnel()
            raise
        except (OSError, AdapterError):
            await self._close_tunnel()
            raise AdapterError("Community SSH forwarding failed; check the SSH key, known host and port.") from None

    async def _close_tunnel(self) -> None:
        process, self._tunnel = self._tunnel, None
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    def _module(self, module: str) -> str:
        module = "animation" if module == "workflow" else module
        if module not in MEDIA_MODULES | {"animation", "effects"}:
            raise AdapterError("Supported community modules: animation, model3d, image, speech, music, effects.")
        return module

    def _base(self, module: str) -> str:
        return self.settings.module_url(self._module(module)).rstrip("/")

    def _effects_request_origin(self) -> str:
        return self._origin

    async def _effects_transport_headers(self, *, write: bool) -> dict[str, str]:
        headers = dict(self._host_headers)
        if write:
            # Refresh the outer portal CSRF before the effects session is read.
            # EffectsMixin then verifies the bound owner and adds its own CSRF.
            # No mutation is replayed if either authentication layer rejects it.
            session = await self._api("portal", "GET", "/api/session")
            csrf = session.get("csrf")
            if not isinstance(csrf, str) or not csrf:
                raise AdapterError("Community session did not provide its CSRF token.")
            self._portal_csrf = csrf
            headers["X-Community-CSRF"] = csrf
        return headers

    async def _ensure_login(self) -> None:
        if self._authenticated:
            return
        async with self._login_lock:
            if self._authenticated:
                return
            await self._ensure_tunnel()
            login_payload = self.settings.community_login_payload()
            self._portal_csrf = None
            self._csrf.clear()
            for cookie in list(self.http.cookies.jar):
                if cookie.name == "fantasy_community_session":
                    self.http.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
            try:
                response = await self.http.post(
                    self.settings.portal_url + "/api/login",
                    json=login_payload,
                    headers={**self._host_headers, "Origin": self._origin, "Accept": "application/json"},
                )
            except httpx.HTTPError:
                raise AdapterError("Could not contact the community login service; check the SSH tunnel and configured address.") from None
            if response.status_code != 200:
                raise AdapterError(f"Community login returned HTTP {response.status_code}; check the configured GitLab read_user PAT or local credentials and account state.")
            try:
                session = response.json()
                csrf = session.get("csrf")
            except (ValueError, AttributeError):
                raise AdapterError("Community login returned an unexpected response.") from None
            if not isinstance(csrf, str) or not csrf or not any(
                cookie.name == "fantasy_community_session" for cookie in self.http.cookies.jar
            ):
                raise AdapterError("Community login did not establish its expected session and CSRF token.")
            self._portal_csrf = csrf
            self._authenticated = True

    async def _api(self, module: str, method: str, path: str, **kwargs) -> dict:
        await self._ensure_login()
        method = method.upper()
        base = self.settings.portal_url if module == "portal" else self._base(module)
        headers = {"Accept": "application/json", **kwargs.pop("headers", {}), **self._host_headers}
        if method not in {"GET", "HEAD", "OPTIONS"}:
            # Refresh before, never replay after, a write. This covers the
            # eight-hour login session and hourly module CSRF rotation without
            # creating duplicate uploads or possibly billable generations.
            session = await self._api("portal", "GET", "/api/session")
            csrf = session.get("csrf")
            if not isinstance(csrf, str) or not csrf:
                raise AdapterError("Community session did not provide its CSRF token.")
            self._portal_csrf = csrf
            headers.update({"Origin": self._origin, "X-Community-CSRF": csrf})
            if module in MEDIA_MODULES:
                module_session = await self._api(module, "GET", "/api/session")
                module_csrf = module_session.get("csrf")
                if not isinstance(module_csrf, str) or not module_csrf:
                    raise AdapterError("The community module did not provide its CSRF token.")
                headers["X-CSRF-Token"] = module_csrf
        for attempt in range(2):
            try:
                response = await self.http.request(method, base + path, headers=headers, **kwargs)
            except httpx.HTTPError:
                suffix = " Its submission status is unknown; inspect jobs before resubmitting." if method not in {"GET", "HEAD"} else ""
                raise AdapterError("Community HTTP request failed." + suffix) from None
            if response.status_code == 401:
                self._authenticated = False
                self._portal_csrf = None
                if method in {"GET", "HEAD"} and attempt == 0:
                    await self._ensure_login()
                    continue
                raise AdapterError("The community session expired before acceptance; reconnect before retrying.")
            if response.status_code >= 400:
                # Provider errors may contain secrets. Never copy raw bodies.
                raise AdapterError(f"Community {module} returned HTTP {response.status_code}; check account permissions, configured APIs and input parameters.")
            if response.status_code >= 300:
                raise AdapterError("Unexpected redirect from the community API; redirects are not followed.")
            try:
                result = response.json()
            except ValueError:
                raise AdapterError("Community returned an unexpected non-JSON API response.") from None
            if not isinstance(result, dict):
                raise AdapterError("Community returned an unexpected API response shape.")
            return result
        raise AdapterError("Could not restore the community session.")

    async def capabilities(self) -> dict:
        workspace = redact(await self._api("portal", "GET", "/api/workspace"))

        async def inspect(module):
            try:
                if module == "effects":
                    guide = await self.effects_guide()
                    return module, {"available": True, "kind": "particle-effects-editor", "limits": guide.get("limits", {}),
                                    "usage": "get_effects_guide, list_effects/read_effect, validate_effect, save_effect, start/get/download_effect_export"}
                if module == "animation":
                    health = await self._api(module, "GET", "/api/health", params={"prompt_mode": "direct"})
                    try:
                        assisted_health = redact(await self._api(module, "GET", "/api/health", params={"prompt_mode": "assisted"}))
                    except AdapterError as error:
                        # An unavailable assisted provider must not hide a usable direct mode.
                        assisted_health = {"available": False, "generation_ready": False, "error": str(error)}
                    return module, {"available": True, "health": redact(health),
                                    "assisted_health": assisted_health,
                                    "output": "2D transparent frames and spritesheets"}
                return module, {"available": True, "config": redact(await self._api(module, "GET", "/api/config"))}
            except AdapterError as error:
                return module, {"available": False, "error": str(error)}

        modules = dict(await asyncio.gather(*(inspect(m) for m in ("animation", "model3d", "image", "speech", "music", "effects"))))
        return {"backend": "community", "portal": workspace, "modules": modules,
                "notes": ["Use model IDs returned here: community model IDs are verified API configuration IDs, not fixed model names.",
                          "The community uses independent local accounts; GitLab OAuth is optional for browser users.",
                          "Animation requires configured video and image background-removal APIs, including direct mode.",
                          "Animation health reports direct-mode readiness; use assisted_health when prompt_mode=assisted. Module available only means its API can be reached.",
                          "Assisted animation additionally requires a verified animation/vision API and sends reference images with task text; a text-only LLM is insufficient. Direct mode skips this prompt model.",
                          "Effects are account-owned Arcadia projects and CPU exports; no model API is required.",
                          "Media job submissions are not idempotent; never automatically repeat an uncertain submission."]}

    async def submit_job(self, module: str, model_id: str | None, params: dict, file_path: Path | None = None,
                         prompt_mode: str = "direct") -> dict:
        module = self._module(module)
        if module not in MEDIA_MODULES:
            raise AdapterError("Use submit_animation for animation tasks.")
        if model_id is None:
            configuration = await self._api(module, "GET", "/api/config")
            models = [item for item in configuration.get("models", [])
                      if item.get("ready") and item.get("enabled")]
            defaults = [item for item in models if item.get("default")]
            selected = defaults if defaults else models
            if len(selected) != 1:
                raise AdapterError("Choose an explicit community model_id from list_capabilities; no unique default is configured.")
            model_id = selected[0]["id"]
        return await super().submit_job(module, model_id, params, file_path, prompt_mode)

    def _download_url(self, module: str, url: str) -> str:
        module = self._module(module)
        base = self._base(module)
        absolute = urljoin(base + "/", url)
        parts = urlsplit(absolute)
        decoded = parts.path
        for _ in range(3):
            decoded = unquote(decoded)
        if (url_origin(absolute) != url_origin(base) or parts.username or parts.password or parts.fragment
                or "\\" in absolute or "\\" in decoded or ".." in decoded.split("/")
                or any(ord(char) < 32 for char in decoded)):
            raise AdapterError("The artifact URL is outside the configured community module.")
        prefix = urlsplit(base).path.rstrip("/")
        if module == "animation":
            allowed = parts.path.startswith(prefix + "/files/") or (
                parts.path.startswith(prefix + "/api/assets/") and parts.path.endswith("/export.zip"))
        else:
            allowed = parts.path.startswith(prefix + "/api/jobs/") and "/files/" in parts.path
        if not allowed:
            raise AdapterError("The URL is not a supported community artifact download route.")
        return absolute

    async def download(self, url: str, destination: Path) -> dict:
        parts = urlsplit(url)
        module = next((name for name in ("animation", "model3d", "image", "speech", "music")
                       if url_origin(url) == url_origin(self._base(name))
                       and parts.path.startswith(urlsplit(self._base(name)).path.rstrip("/") + "/")), None)
        if module is None:
            raise AdapterError("Download URL must be from a configured community module.")
        url = self._download_url(module, url)
        await self._api("portal", "GET", "/api/session")
        return await download_to_file(self.http, url, destination, self.settings,
                                      headers={"Accept": "application/octet-stream", **self._host_headers})

    async def close(self) -> None:
        with anyio.CancelScope(shield=True):
            try:
                await self.http.aclose()
            finally:
                await self._close_tunnel()

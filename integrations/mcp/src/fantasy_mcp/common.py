"""Shared transport, output redaction and file boundaries."""
from __future__ import annotations

import hashlib
import os
import re
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote

import httpx
import truststore

from . import __version__
from .config import Settings, url_origin


class AdapterError(Exception):
    """An actionable, credential-free error suitable for the MCP client."""


SENSITIVE_KEYS = {"password", "new_password", "password_confirmation", "csrf", "csrf_token", "token",
                  "access_token", "refresh_token", "id_token", "gitlab_token", "personal_access_token", "secret", "api_key", "authorization",
                  "cookie", "set-cookie", "session", "session_cookie", "_gitlab_session", "fantasy_session"}


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if str(k).lower().replace("-", "_") not in
                {key.replace("-", "_") for key in SENSITIVE_KEYS}}
    if isinstance(value, (tuple, list)):
        return [redact(v) for v in value]
    return value


def safe_component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) or ".." in value:
        raise AdapterError("Invalid identifier; use an ID returned by the service")
    return value


def create_http_client(settings: Settings, *, transport=None) -> httpx.AsyncClient:
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if settings.ca_bundle:
        context.load_verify_locations(cafile=str(settings.ca_bundle))
    return httpx.AsyncClient(
        verify=context, transport=transport, timeout=httpx.Timeout(settings.timeout_seconds),
        follow_redirects=False, trust_env=False, headers={"User-Agent": "Fantasy-MCP/" + __version__},
    )


def approved_url(url: str, settings: Settings) -> str:
    try:
        origin = url_origin(url)
    except ValueError as exc:
        raise AdapterError("Service returned an invalid URL") from exc
    if origin not in settings.allowed_origins:
        raise AdapterError("Refusing a URL outside the configured Fantasy services")
    if any(key in urlsplit(url).query.lower() for key in ("access_token=", "password=", "refresh_token=")):
        raise AdapterError("Credential-bearing download URLs are not supported")
    return url


def checked_upload(path: str, settings: Settings) -> Path:
    candidate = Path(path).expanduser().resolve(strict=True)
    if not any(candidate.is_relative_to(root) for root in settings.upload_roots):
        raise AdapterError("Upload file must be inside a configured upload_roots directory")
    if not candidate.is_file() or candidate.stat().st_size > settings.max_upload_bytes:
        raise AdapterError("Upload is not a regular file or exceeds max_upload_bytes")
    if settings.credentials_file and candidate == settings.credentials_file:
        raise AdapterError("The credentials file cannot be uploaded")
    if candidate.name.lower() in (".env", "id_rsa", "id_ed25519") or candidate.suffix.lower() in (".pem", ".key", ".pfx"):
        raise AdapterError("Credential files are not valid creative inputs")
    if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".mkv",
                                        ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".zip"}:
        raise AdapterError("Only supported image, video, audio or training ZIP files may be uploaded")
    return candidate


def checked_destination(subdirectory: str, name: str, settings: Settings) -> Path:
    root = settings.output_dir.resolve()
    folder = (root / subdirectory).resolve()
    if not folder.is_relative_to(root):
        raise AdapterError("Download directory must stay inside output_dir")
    # A server filename is data, never an output path.
    clean_name = re.sub(r"[^\w. -]", "_", unquote(str(name)).replace("\\", "/").split("/")[-1])[:160]
    if clean_name in ("", ".", ".."):
        clean_name = "artifact.bin"
    folder.mkdir(parents=True, exist_ok=True)
    candidate = (folder / clean_name).resolve()
    if not candidate.is_relative_to(root) or candidate.exists():
        raise AdapterError("Download destination is outside output_dir or already exists; choose another subdirectory")
    return candidate


async def download_to_file(client: httpx.AsyncClient, url: str, destination: Path, settings: Settings,
                           *, headers: dict | None = None) -> dict:
    destination = Path(destination).resolve()
    if not destination.is_relative_to(settings.output_dir.resolve()) or destination.exists():
        raise AdapterError("Download destination is outside output_dir or already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    current = approved_url(url, settings)
    temporary = None
    digest = hashlib.sha256()
    size = 0
    try:
        for _ in range(6):
            async with client.stream("GET", current, headers=headers) as response:
                if response.is_redirect:
                    raise AdapterError("Artifact download redirected; reconnect and query the job again")
                if response.status_code != 200:
                    raise AdapterError(f"Artifact download returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type or "application/xhtml+xml" in content_type:
                    raise AdapterError("Artifact download returned a login/HTML page; reconnect first")
                declared = response.headers.get("content-length")
                if declared and int(declared) > settings.max_download_bytes:
                    raise AdapterError("Artifact exceeds max_download_bytes")
                fd, temporary = tempfile.mkstemp(prefix=".fantasy-", suffix=".part", dir=destination.parent)
                with os.fdopen(fd, "wb") as out:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > settings.max_download_bytes:
                            raise AdapterError("Artifact exceeds max_download_bytes")
                        out.write(chunk)
                        digest.update(chunk)
                # Exclusive create preserves any file written by another process during the download.
                with destination.open("xb") as out, open(temporary, "rb") as source:
                    import shutil
                    shutil.copyfileobj(source, out)
                return {"path": str(destination), "bytes": size, "sha256": digest.hexdigest()}
        raise AdapterError("Too many artifact redirects")
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


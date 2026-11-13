"""Configuration is local; credentials never become tool arguments or results."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MODULES = ("model3d", "image", "speech", "music", "animation", "effects")
PRIVATE_PORTS = {"model3d": 18449, "image": 18450, "speech": 18451, "music": 18452, "animation": 18445, "effects": 18454}


def url_origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    if parts.fragment:
        raise ValueError("URL fragments are not supported")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class Settings:
    edition: str = "private"
    portal_url: str = "https://localhost:18443"
    gitlab_url: str | None = None
    module_urls: dict[str, str] = field(default_factory=dict)
    credentials_file: Path | None = None
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    credential_type: str = 'local'
    gitlab_token: str | None = field(default=None, repr=False)
    ca_bundle: Path | None = None
    timeout_seconds: float = 30.0
    max_download_bytes: int = 512 * 1024 * 1024
    max_upload_bytes: int = 200 * 1024 * 1024
    origin: str | None = None
    output_dir: Path = field(default_factory=lambda: Path.cwd() / "downloads")
    upload_roots: tuple[Path, ...] = field(default_factory=tuple)
    ssh_tunnel: dict | None = None

    def __post_init__(self) -> None:
        if self.edition not in ("private", "community"):
            raise ValueError("edition must be private or community")
        if self.credential_type not in ('local', 'gitlab_pat') or (self.edition == 'private' and self.credential_type != 'local'):
            raise ValueError('gitlab_pat authentication is supported only by the community edition')
        self.portal_url = self.portal_url.rstrip("/")
        parts = urlsplit(self.portal_url)
        url_origin(self.portal_url)
        if parts.query:
            raise ValueError("portal_url cannot contain a query")
        if self.edition == "private":
            host = parts.hostname
            host = f"[{host}]" if host and ":" in host else host
            self.gitlab_url = self.gitlab_url or f"{parts.scheme}://{host}:18444"
            defaults = {k: f"{parts.scheme}://{host}:{v}" for k, v in PRIVATE_PORTS.items()}
        else:
            defaults = {k: f"{self.portal_url}/{k}" for k in MODULES}
        self.module_urls = {**defaults, **self.module_urls}
        for value in [*self.module_urls.values(), self.gitlab_url, self.origin]:
            if value:
                url_origin(value)
        for name in ("credentials_file", "ca_bundle", "output_dir"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, Path(value).expanduser().resolve())
        self.upload_roots = tuple(Path(p).expanduser().resolve() for p in self.upload_roots)
        if self.timeout_seconds <= 0 or self.max_download_bytes <= 0 or self.max_upload_bytes <= 0:
            raise ValueError("timeouts and byte limits must be positive")

    def module_url(self, module: str) -> str:
        if module not in MODULES:
            raise ValueError(f"Unsupported module: {module}")
        return self.module_urls[module].rstrip("/")

    @property
    def allowed_origins(self) -> set[str]:
        urls = [self.portal_url, *self.module_urls.values()]
        if self.gitlab_url:
            urls.append(self.gitlab_url)
        return {url_origin(url) for url in urls}

    def credentials(self) -> tuple[str, str]:
        username = self.username or os.environ.get("FANTASY_USERNAME")
        password = self.password or os.environ.get("FANTASY_PASSWORD")
        if username and password:
            return username, password
        if self.credentials_file:
            raw = self.credentials_file.read_text(encoding="utf-8-sig")
            if raw.lstrip().startswith("{"):
                data = json.loads(raw)
                username, password = data.get("username"), data.get("password")
            else:
                user_match = re.search(r"^(?:账户|账号|username)\s*[:：=]\s*(.+)$", raw, re.M | re.I)
                pass_match = re.search(r"^(?:密码|password)\s*[:：=]\s*(.+)$", raw, re.M | re.I)
                username = user_match.group(1).strip() if user_match else None
                password = pass_match.group(1).strip() if pass_match else None
        if not username or not password:
            raise ValueError("Configure credentials_file or FANTASY_USERNAME and FANTASY_PASSWORD")
        return str(username), str(password)

    def community_login_payload(self) -> dict[str, str]:
        data = {}
        if self.credentials_file:
            raw = self.credentials_file.read_text(encoding='utf-8-sig')
            if raw.lstrip().startswith('{'):
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('The credentials file must contain an object')
        kind = data.get('credential_type', self.credential_type)
        if kind == 'local':
            username, password = self.credentials()
            return {'username': username, 'password': password}
        if kind != 'gitlab_pat':
            raise ValueError('Unsupported community credential_type')
        token = self.gitlab_token or os.environ.get('FANTASY_GITLAB_TOKEN') or data.get('token')
        username = self.username or os.environ.get('FANTASY_USERNAME') or data.get('username')
        if not isinstance(token, str) or not token or len(token) > 8192 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('Configure a GitLab read_user PAT in the credentials file or FANTASY_GITLAB_TOKEN')
        if username is not None and (not isinstance(username, str) or not username or len(username) > 80):
            raise ValueError('The optional account username is invalid')
        return {'credential_type': 'gitlab_pat', 'token': token, **({'username': username} if username else {})}

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        config_path = Path(path).expanduser().resolve()
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        for name in ("credentials_file", "ca_bundle", "output_dir"):
            if data.get(name):
                candidate = Path(data[name]).expanduser()
                data[name] = candidate if candidate.is_absolute() else config_path.parent / candidate
        if "upload_roots" in data:
            data["upload_roots"] = tuple(
                Path(p) if Path(p).is_absolute() else config_path.parent / p for p in data["upload_roots"]
            )
        return cls(**data)


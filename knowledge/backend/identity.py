"""Verify the short-lived identity assertion supplied by Fantasy Main's gateway."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

DEFAULT_LOGIN_URL = 'http://localhost:18082/login'


def configured_urls(login_url: str = DEFAULT_LOGIN_URL) -> tuple[str, str]:
    """Only administrator-configured absolute HTTP(S) redirects are accepted."""
    parsed = urlsplit(login_url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
            or any(ord(char) < 32 for char in login_url)):
        raise RuntimeError('FANTASY_LOGIN_URL must be an absolute HTTP(S) login URL')
    return login_url, urlunsplit((parsed.scheme, parsed.netloc, '/logout', '', ''))


def _decode(segment: str) -> bytes:
    if not segment or not re.fullmatch(r'[A-Za-z0-9_-]+', segment):
        raise ValueError('Invalid JWT encoding')
    decoded = base64.b64decode(segment + '=' * (-len(segment) % 4), altchars=b'-_', validate=True)
    if base64.urlsafe_b64encode(decoded).rstrip(b'=').decode('ascii') != segment:
        raise ValueError('Noncanonical JWT encoding')
    return decoded


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JWT claim')
        result[key] = value
    return result


def verify_identity(authorization: str, secret: str, *, now: int | None = None) -> dict | None:
    """Fail closed without accepting legacy cookies or unverified identity headers."""
    if len(secret) < 32 or len(authorization) > 8192:
        return None
    kind, separator, token = authorization.partition(' ')
    if not separator or kind.lower() != 'bearer':
        return None
    try:
        header_part, claims_part, signature_part = token.split('.')
        header = json.loads(_decode(header_part), object_pairs_hook=_object)
        if (not isinstance(header, dict) or header.get('alg') != 'HS256'
                or header.get('typ', 'JWT') != 'JWT' or 'crit' in header or 'b64' in header):
            return None
        signature = _decode(signature_part)
        expected = hmac.new(secret.encode('utf-8'), f'{header_part}.{claims_part}'.encode('ascii'), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(_decode(claims_part), object_pairs_hook=_object)
        if not isinstance(claims, dict) or claims.get('iss') != 'fantasy-main' or claims.get('aud') != 'knowledge-ques':
            return None
        issued, expires = claims.get('iat'), claims.get('exp')
        current = int(time.time()) if now is None else now
        if (type(issued) is not int or type(expires) is not int
                or not 0 < issued <= current < expires or not 0 < expires - issued <= 120):
            return None
        if 'nbf' in claims and (type(claims['nbf']) is not int or claims['nbf'] > current):
            return None
        if claims.get('role') not in ('user', 'admin'):
            return None
        if any(not isinstance(claims.get(key), str) or not claims[key].strip()
               or len(claims[key]) > limit for key, limit in (('sub', 128), ('name', 256))):
            return None
        return {'id': claims['sub'], 'name': claims['name'], 'role': claims['role']}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def permissions(identity: dict | None) -> list[str]:
    if not identity:
        return []
    # Writes are authorized against the target source's owner at the endpoint.
    return ['knowledge.read', 'knowledge.write', 'knowledge.index']


def required_permission(method: str, path: str) -> str:
    if method in ('GET', 'HEAD', 'OPTIONS') or path in ('/api/chat', '/api/search'):
        return 'knowledge.read'
    return 'knowledge.index' if path == '/api/index' else 'knowledge.write'

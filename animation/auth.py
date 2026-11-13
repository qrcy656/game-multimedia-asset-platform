"""Small identity boundary for a future login service; no user database lives here."""
import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets
import time


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    role: str
    mode: str
    expires_at: int

    @property
    def admin(self):
        return self.role == "admin"

    @property
    def owner(self):
        return {"owner_id": self.id, "owner_name": self.name}

    @property
    def user(self):
        return {"id": self.id, "name": self.name, "role": self.role}


def _encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decoded(value):
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AuthenticationError("Invalid identity token")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_token(claims, secret):
    header = _encoded(b'{"alg":"HS256","typ":"JWT"}')
    payload = _encoded(json.dumps(claims, ensure_ascii=False, separators=(",", ":")).encode())
    data = header + "." + payload
    return data + "." + _encoded(hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest())


def verify_token(token, secret, issuer, audience, mode):
    try:
        if not isinstance(token, str) or len(token) > 8192 or not secret:
            raise AuthenticationError("Invalid identity token")
        header_part, payload_part, signature = token.split(".")
        expected = hmac.new(secret.encode(), (header_part + "." + payload_part).encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_decoded(signature), expected):
            raise AuthenticationError("Invalid identity token")
        header, claims = json.loads(_decoded(header_part)), json.loads(_decoded(payload_part))
        if header.get("alg") != "HS256" or header.get("typ") != "JWT" or "crit" in header or "b64" in header:
            raise AuthenticationError("Unsupported identity token")
        if claims.get("iss") != issuer or claims.get("aud") != audience:
            raise AuthenticationError("Invalid identity audience or issuer")
        now = int(time.time())
        issued, expires = claims["iat"], claims["exp"]
        if type(issued) is not int or type(expires) is not int:
            raise AuthenticationError("Identity token times must be integer seconds")
        if issued > now + 30 or expires <= now or not 0 < expires - issued <= 86400:
            raise AuthenticationError("Identity token expired or has invalid lifetime")
        if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now):
            raise AuthenticationError("Identity token is not yet valid")
        user_id, name, role = claims["sub"], claims["name"], claims["role"]
        if (not isinstance(user_id, str) or not 1 <= len(user_id) <= 200
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
                or role not in {"user", "admin"}
                or any(ord(c) < 32 or ord(c) == 127 for c in user_id + name)):
            raise AuthenticationError("Invalid identity claims")
        return Principal(user_id, name.strip(), role, mode, expires)
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as exc:
        if isinstance(exc, AuthenticationError):
            raise
        raise AuthenticationError("Invalid identity token") from exc


class Identity:
    """Accept only identities supplied by the community gateway."""
    def __init__(self, settings):
        self.settings = settings

    def validate_configuration(self):
        if len(self.settings.identity_secret) < 32:
            raise RuntimeError("COMMUNITY_IDENTITY_SECRET must contain at least 32 characters")

    def external(self, token):
        return verify_token(token, self.settings.identity_secret, self.settings.identity_issuer,
                            self.settings.identity_audience, "external_bearer")

    def authenticate(self, request):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("Sign in through the community platform")
        return self.external(token)

"""Identity boundary shared with the Fantasy gateway, scoped to narrative."""
import base64
import hashlib
import hmac
import json
import time

MODULE = "narrative"


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def identity(token, secret):
    try:
        if not token or len(token) > 8192 or len(secret) < 32:
            raise ValueError()
        head, body, signature = token.split(".")
        expected = encode(hmac.new(secret.encode(), (head + "." + body).encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        now = int(time.time())
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise ValueError()
        if claims["iss"] != "fantasy-main" or claims["aud"] != "fantasy-narrative":
            raise ValueError()
        if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
            raise ValueError()
        if not (claims["iat"] <= now + 10 and now < claims["exp"] <= claims["iat"] + 90):
            raise ValueError()
        if claims.get("nbf", 0) > now or claims["role"] not in {"admin", "user"}:
            raise ValueError()
        if any(not isinstance(claims[k], str) or not 1 <= len(claims[k]) <= 200 or any(ord(c) < 32 for c in claims[k]) for k in ("sub", "name")):
            raise ValueError()
        return {"id": claims["sub"], "name": claims["name"], "admin": claims["role"] == "admin"}
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise ValueError("请通过统一入口登录。") from None


def csrf_token(secret, owner, period=None):
    period = int(time.time()) // 3600 if period is None else period
    return str(period) + "." + hmac.new(secret.encode(), f"csrf:{MODULE}:{owner}:{period}".encode(), hashlib.sha256).hexdigest()


def csrf_valid(token, secret, owner):
    if not isinstance(token, str) or not token.isascii() or len(token) > 100:
        return False
    return any(hmac.compare_digest(token, csrf_token(secret, owner, period)) for period in (int(time.time()) // 3600, int(time.time()) // 3600 - 1))

"""Shared, dependency-free boundaries for the Fantasy media workbenches."""
import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import Path

MODULES = {
    "model3d": {"title": "3D 模型生成", "description": "从参考图制作可用于游戏与场景的三维资产。"},
    "image": {"title": "图片生成", "description": "通过文字与参考图，制作角色、场景和美术素材。"},
    "speech": {"title": "语音制作", "description": "设计角色声音，选择预设音色，制作 NPC 台词与对白。"},
    "music": {"title": "音乐制作", "description": "制作氛围与短音效、纯音乐、环境配乐和带词歌曲。"},
}
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
FINAL = {"succeeded", "failed", "cancelled", "interrupted"}

def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")

def sign(claims, secret):
    data = encode(b'{"alg":"HS256","typ":"JWT"}') + "." + encode(json.dumps(claims).encode())
    return data + "." + encode(hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest())

def identity(token, secret, module):
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
        if claims["iss"] != "fantasy-main" or claims["aud"] != "fantasy-" + module:
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

def csrf_token(secret, module, owner, period=None):
    period = int(time.time()) // 3600 if period is None else period
    return str(period) + "." + hmac.new(secret.encode(), f"csrf:{module}:{owner}:{period}".encode(), hashlib.sha256).hexdigest()

def csrf_valid(token, secret, module, owner):
    if not isinstance(token, str) or not token.isascii() or len(token) > 100:
        return False
    return any(hmac.compare_digest(token, csrf_token(secret, module, owner, period)) for period in (int(time.time()) // 3600, int(time.time()) // 3600 - 1))

def module_data(root, module):
    if module not in MODULES:
        raise ValueError("未知工作模块。")
    return Path(root) / (module + "-workbench") / "data"

def safe_child(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise ValueError("无效文件路径。")
    return target

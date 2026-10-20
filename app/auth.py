"""坐席令牌：签发、校验、FastAPI 依赖。

单独成模块的原因：令牌逻辑是纯函数（HMAC + 过期时间），不应该和数据库初始化绑在一起。
抽出来之后可以直接单测，导入 `app.auth` 不会触发任何连接。

编码格式（重要）：
    base64url( "username.exp" + HMAC-SHA256 的 32 字节 )

签名按**固定 32 字节**从尾部切，不能靠分隔符解析 —— 签名本身是随机字节，
约 12.5% 的概率会包含 `.`，一旦用 rsplit(".") 就会把签名切断。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from fastapi import Header, HTTPException

from . import db

SIG_LEN = hashlib.sha256().digest_size  # 32

# 未显式配置时每个进程随机生成：重启后旧令牌全部失效，这是刻意的
_SECRET = os.environ.get("AGENT_TOKEN_SECRET") or secrets.token_hex(16)
_TOKEN_TTL = int(os.environ.get("AGENT_TOKEN_TTL", str(12 * 3600)))


def _sign(raw: bytes) -> bytes:
    return hmac.new(_SECRET.encode(), raw, hashlib.sha256).digest()


def issue_token(username: str) -> str:
    exp = int(time.time()) + _TOKEN_TTL
    raw = f"{username}.{exp}".encode()
    return base64.urlsafe_b64encode(raw + _sign(raw)).decode()


def parse_token(token: str) -> str | None:
    try:
        data = base64.urlsafe_b64decode(token.encode())
    except Exception:  # noqa: BLE001
        return None

    if len(data) <= SIG_LEN:
        return None

    raw, sig = data[:-SIG_LEN], data[-SIG_LEN:]
    if not hmac.compare_digest(sig, _sign(raw)):
        return None

    try:
        username_raw, exp_raw = raw.rsplit(b".", 1)
        if int(exp_raw) < time.time():
            return None
        return username_raw.decode()
    except (ValueError, UnicodeDecodeError):
        return None


def require_agent(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    username = parse_token(authorization[7:])
    if not username:
        raise HTTPException(status_code=401, detail="登录已失效")
    user = db.get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="账号不存在")
    return user

"""坐席令牌：签发、校验、FastAPI 依赖。

单独成模块的原因：令牌逻辑是纯函数（HMAC + 过期时间），不应该和数据库初始化绑在一起。
抽出来之后可以直接单测，导入 `app.auth` 不会触发任何连接。
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

# 未显式配置时每个进程随机生成：重启后旧令牌全部失效，这是刻意的
_SECRET = os.environ.get("AGENT_TOKEN_SECRET") or secrets.token_hex(16)
_TOKEN_TTL = int(os.environ.get("AGENT_TOKEN_TTL", str(12 * 3600)))


def issue_token(username: str) -> str:
    exp = int(time.time()) + _TOKEN_TTL
    raw = f"{username}.{exp}".encode()
    sig = hmac.new(_SECRET.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + b"." + sig).decode()


def parse_token(token: str) -> str | None:
    try:
        data = base64.urlsafe_b64decode(token.encode())
        username, exp, sig = data.rsplit(b".", 2)
        expected = hmac.new(_SECRET.encode(), username + b"." + exp, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp) < time.time():
            return None
        return username.decode()
    except Exception:  # noqa: BLE001 - 任何解析失败都视为无效令牌
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

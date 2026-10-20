"""令牌是纯函数：签发、篡改、过期都不该碰数据库。"""
from __future__ import annotations

import pytest

from app import auth


def test_roundtrip():
    token = auth.issue_token("agent@selfdesk.local")
    assert auth.parse_token(token) == "agent@selfdesk.local"


def test_tampered_signature_rejected():
    token = auth.issue_token("agent@selfdesk.local")
    # 改掉签名最后一个字符，长度不变
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert auth.parse_token(bad) is None


def test_garbage_rejected():
    assert auth.parse_token("not-a-token") is None
    assert auth.parse_token("") is None


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setattr(auth, "_TOKEN_TTL", -1)
    token = auth.issue_token("agent@selfdesk.local")
    assert auth.parse_token(token) is None


def test_secret_change_invalidates(monkeypatch):
    token = auth.issue_token("agent@selfdesk.local")
    monkeypatch.setattr(auth, "_SECRET", "another-secret")
    assert auth.parse_token(token) is None

def test_many_roundtrips():
    """回归：签名是随机字节，可能包含 '.'。

    早期实现用 rsplit(b".") 解析，约 12.5% 的令牌会被切错；
    批量签发再解析可以稳定复现这一类问题。
    """
    for i in range(300):
        username = f"agent{i}@selfdesk.local"
        assert auth.parse_token(auth.issue_token(username)) == username


def test_signature_of_fixed_length_assumed():
    token = auth.issue_token("agent@selfdesk.local")
    raw = __import__("base64").urlsafe_b64decode(token.encode())
    assert len(raw) > auth.SIG_LEN

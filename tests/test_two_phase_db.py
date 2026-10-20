"""两阶段写操作：确认一次只落一单。

需要真实 PostgreSQL（表结构、条件更新都在库里）。
设置 SELFDESK_TEST_DSN 才会执行，否则跳过——CI 里由 postgres service 提供。

    SELFDESK_TEST_DSN="host=127.0.0.1 port=5432 dbname=selfdesk_test user=selfdesk password=selfdesk_local" \
        pytest -m db
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.environ.get("SELFDESK_TEST_DSN"),
        reason="未设置 SELFDESK_TEST_DSN，跳过数据库测试",
    ),
]


@pytest.fixture(scope="module")
def env():
    # 让 config 在 import 阶段就指向测试库
    os.environ["SELFDESK_PG_DSN"] = os.environ["SELFDESK_TEST_DSN"]
    from app import db, pg, tools

    db.init_db(default_agent=("qa@test.local", "qa-pass"))
    sid = db.create_session(visitor_name="pytest-两阶段")["id"]
    yield db, pg, tools, sid
    with pg.cursor() as cur:
        cur.execute("DELETE FROM pending_actions WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM messages WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM sessions WHERE id=%s", (sid,))


def test_create_order_is_idempotent(env):
    db, pg, tools, sid = env
    pending = db.create_pending(sid, "create_order", {"product": "标准版（月付）", "amount": 129})

    first = tools.execute_pending(sid, db.get_pending(sid))
    assert first["ok"] is True
    assert first["order_no"]

    # 同一个 pending 再确认一次：不应再生成订单
    second = tools.execute_pending(sid, pending)
    assert second["ok"] is False
    assert second["error"] == "already_done"

    assert db.get_order(first["order_no"]) is not None
    with pg.cursor() as cur:
        cur.execute("DELETE FROM orders WHERE order_no=%s", (first["order_no"],))


def test_pending_expires_or_disappears_after_finish(env):
    db, _, _, sid = env
    db.create_pending(sid, "create_order", {"product": "标准版（年付）", "amount": 1288})
    pending = db.get_pending(sid)
    assert pending is not None
    assert db.finish_pending(pending["id"], "DONE") is True
    assert db.get_pending(sid) is None


def test_unknown_pending_is_rejected(env):
    _, _, tools, sid = env
    res = tools.execute_pending(sid, None)
    assert res["ok"] is False
    assert res["error"] == "no_pending"

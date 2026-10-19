"""PostgreSQL 数据层：租户、坐席、会话、消息、工单、待确认动作、订单。

表设计要点（面试可讲）：
1. **时间用 TIMESTAMPTZ**，默认 `now()` 由数据库生成。应用层不再自己拼时间字符串，
   多实例部署时也不会出现「各机器时钟不一致」的排序错乱。
2. **外键 + 级联**：删会话时消息一起删，不留孤儿行（SQLite 版只能靠应用自己保证）。
3. **幂等写入**：订单用 `ON CONFLICT ... DO UPDATE`，待确认动作用条件 UPDATE
   （`WHERE status='PENDING'`）保证同一动作不会被执行两次。
4. **SOLID 中的单一职责**：知识库向量表在 `pg_store`，这里只管业务表，
   避免两套 schema 定义互相打架。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid

from . import pg
from .config import DEFAULT_TENANT
from .pg import norm

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        plan       TEXT NOT NULL DEFAULT 'standard',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name  TEXT,
        role          TEXT NOT NULL DEFAULT 'agent',
        tenant_id     TEXT NOT NULL DEFAULT 't_default',
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id           TEXT PRIMARY KEY,
        tenant_id    TEXT NOT NULL DEFAULT 't_default',
        channel      TEXT NOT NULL DEFAULT 'web',
        visitor_name TEXT,
        status       TEXT NOT NULL DEFAULT 'BOT',
        intent       TEXT,
        agent_name   TEXT,
        summary      TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id         BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        tenant_id  TEXT NOT NULL DEFAULT 't_default',
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        source     TEXT,
        tokens     INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id         TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL DEFAULT 't_default',
        session_id TEXT,
        title      TEXT NOT NULL,
        detail     TEXT,
        priority   TEXT NOT NULL DEFAULT 'NORMAL',
        assignee   TEXT,
        status     TEXT NOT NULL DEFAULT 'OPEN',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_actions (
        id         TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL DEFAULT 't_default',
        session_id TEXT NOT NULL,
        action     TEXT NOT NULL,
        payload    JSONB,
        status     TEXT NOT NULL DEFAULT 'PENDING',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_no   TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL DEFAULT 't_default',
        item       TEXT NOT NULL,
        amount     TEXT NOT NULL,
        status     TEXT NOT NULL,
        logistics  TEXT,
        shipped_at TEXT,
        refund     TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)

# 索引在补列之后建，老库升级时不会因为列不存在而整段失败
INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_tenant_time ON messages(tenant_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_tenant ON tickets(tenant_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_actions(session_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_tenant ON orders(tenant_id)",
)

# 版本升级用；全部幂等，可反复执行
MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't_default'",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't_default'",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'web'",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't_default'",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS latency_ms INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't_default'",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'NORMAL'",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS assignee TEXT",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
)


# ── 密码 ────────────────────────────────────────────────────
def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    return f"{salt}${_hash(password, salt)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(_hash(password, salt), digest)


# ── 初始化 ──────────────────────────────────────────────────
def init_db(default_agent: tuple[str, str] | None = None,
            tenants: list[tuple[str, str]] | None = None) -> None:
    """建池 + 建表 + 补列 + 种子数据。连不上数据库直接抛 PgUnavailable。"""
    from . import pg_store

    pg.init(wait=True)
    with pg.cursor() as cur:
        for stmt in SCHEMA:
            cur.execute(stmt)
        for stmt in MIGRATIONS:
            cur.execute(stmt)
        for stmt in INDEXES:
            cur.execute(stmt)

        for tid, name in (tenants or [(DEFAULT_TENANT, "SelfDesk 智能客服")]):
            cur.execute(
                "INSERT INTO tenants(id, name, plan) VALUES(%s,%s,'standard')"
                " ON CONFLICT(id) DO NOTHING",
                (tid, name),
            )
        if default_agent:
            username, password = default_agent
            cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO users(id, username, password_hash, display_name, role, tenant_id)"
                    " VALUES(%s,%s,%s,%s,%s,%s)",
                    (uuid.uuid4().hex[:12], username, hash_password(password),
                     "客服坐席", "agent", DEFAULT_TENANT),
                )
    # 知识库表（pgvector）与业务表同库不同表，单独建
    pg_store.ensure_schema()


# ── 租户 / 账号 ─────────────────────────────────────────────
def get_user(username: str) -> dict | None:
    return pg.query_one("SELECT * FROM users WHERE username=%s", (username,))


def list_tenants() -> list[dict]:
    return pg.query("SELECT * FROM tenants ORDER BY created_at")


def get_tenant(tid: str) -> dict | None:
    return pg.query_one("SELECT * FROM tenants WHERE id=%s", (tid,))


# ── 会话 ────────────────────────────────────────────────────
def create_session(visitor_name: str = "访客", tenant_id: str = DEFAULT_TENANT,
                   channel: str = "web") -> dict:
    sid = uuid.uuid4().hex[:16]
    row = pg.query_one(
        "INSERT INTO sessions(id, tenant_id, channel, visitor_name, status)"
        " VALUES(%s,%s,%s,%s,'BOT')"
        " RETURNING id, tenant_id, channel, visitor_name, status, created_at, updated_at",
        (sid, tenant_id, channel, visitor_name),
    )
    assert row is not None
    return row


def get_session(sid: str) -> dict | None:
    return pg.query_one("SELECT * FROM sessions WHERE id=%s", (sid,))


def update_session(sid: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items()
               if k in ("status", "intent", "agent_name", "summary", "channel", "visitor_name")}
    if not allowed:
        return
    sets = ", ".join(f"{k}=%s" for k in allowed)
    pg.execute(
        f"UPDATE sessions SET {sets}, updated_at=now() WHERE id=%s",
        (*allowed.values(), sid),
    )


def list_sessions(status: str | None = None, limit: int = 50,
                  tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    sql = "SELECT * FROM sessions WHERE tenant_id=%s"
    args: list = [tenant_id]
    if status:
        sql += " AND status=%s"
        args.append(status)
    sql += " ORDER BY updated_at DESC LIMIT %s"
    args.append(limit)
    return pg.query(sql, tuple(args))


# ── 消息 ────────────────────────────────────────────────────
def add_message(session_id: str, role: str, content: str, source: str = "",
                tokens: int = 0, latency_ms: int = 0,
                tenant_id: str | None = None) -> dict:
    with pg.cursor() as cur:
        tid = tenant_id
        if tid is None:
            cur.execute("SELECT tenant_id FROM sessions WHERE id=%s", (session_id,))
            row = cur.fetchone()
            tid = (row or {}).get("tenant_id") or DEFAULT_TENANT
        cur.execute(
            "INSERT INTO messages(session_id, tenant_id, role, content, source, tokens, latency_ms)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at",
            (session_id, tid, role, content, source, tokens, latency_ms),
        )
        created = cur.fetchone()
        cur.execute("UPDATE sessions SET updated_at=now() WHERE id=%s", (session_id,))
    return {"id": created["id"], "session_id": session_id, "role": role,
            "content": content, "source": source, "created_at": norm(created["created_at"])}


def list_messages(session_id: str, limit: int = 200) -> list[dict]:
    return pg.query(
        "SELECT * FROM messages WHERE session_id=%s ORDER BY id ASC LIMIT %s",
        (session_id, limit),
    )


# ── 工单 ────────────────────────────────────────────────────
def create_ticket(session_id: str, title: str, detail: str = "",
                  tenant_id: str = DEFAULT_TENANT, priority: str = "NORMAL") -> dict:
    tid = uuid.uuid4().hex[:10]
    row = pg.query_one(
        "INSERT INTO tickets(id, tenant_id, session_id, title, detail, priority, status)"
        " VALUES(%s,%s,%s,%s,%s,%s,'OPEN')"
        " RETURNING id, title, status, priority, created_at",
        (tid, tenant_id, session_id, title[:120], detail, priority),
    )
    assert row is not None
    return row


def list_tickets(limit: int = 100, tenant_id: str = DEFAULT_TENANT,
                 status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM tickets WHERE tenant_id=%s"
    args: list = [tenant_id]
    if status:
        sql += " AND status=%s"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT %s"
    args.append(limit)
    return pg.query(sql, tuple(args))


def update_ticket(ticket_id: str, tenant_id: str = DEFAULT_TENANT, **fields) -> bool:
    allowed = {k: v for k, v in fields.items()
               if k in ("status", "assignee", "priority") and v is not None}
    if not allowed:
        return False
    sets = ", ".join(f"{k}=%s" for k in allowed)
    n = pg.execute(
        f"UPDATE tickets SET {sets}, updated_at=now() WHERE id=%s AND tenant_id=%s",
        (*allowed.values(), ticket_id, tenant_id),
    )
    return n > 0


# ── 待确认动作（写操作的第一阶段）──────────────────────────
PENDING_TTL_SEC = 300


def create_pending(session_id: str, action: str, payload: dict,
                   tenant_id: str = DEFAULT_TENANT) -> dict:
    pid = uuid.uuid4().hex[:12]
    row = pg.query_one(
        "INSERT INTO pending_actions(id, tenant_id, session_id, action, payload, status)"
        " VALUES(%s,%s,%s,%s,%s::jsonb,'PENDING')"
        " RETURNING id, session_id, action, status, created_at",
        (pid, tenant_id, session_id, action, json.dumps(payload, ensure_ascii=False)),
    )
    assert row is not None
    row["payload"] = payload
    return row


def get_pending(session_id: str) -> dict | None:
    """取该会话最新的待确认动作；超过 TTL 自动作废，避免「昨天的话今天生效」。"""
    with pg.cursor() as cur:
        cur.execute(
            "SELECT *, EXTRACT(EPOCH FROM (now() - created_at))::int AS age_sec"
            "  FROM pending_actions WHERE session_id=%s AND status='PENDING'"
            " ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        item = cur.fetchone()
        if item is None:
            return None
        if int(item["age_sec"] or 0) > PENDING_TTL_SEC:
            cur.execute(
                "UPDATE pending_actions SET status='EXPIRED' WHERE id=%s AND status='PENDING'",
                (item["id"],),
            )
            return None
    out = norm(item)
    payload = out.get("payload")
    out["payload"] = json.loads(payload) if isinstance(payload, str) else (payload or {})
    return out


def finish_pending(pending_id: str, status: str = "DONE") -> bool:
    """条件更新：只有 PENDING 才能流转，保证同一动作不会被执行两次。"""
    n = pg.execute(
        "UPDATE pending_actions SET status=%s WHERE id=%s AND status='PENDING'",
        (status, pending_id),
    )
    return n > 0


# ── 订单（演示数据持久化；真实场景替换为订单服务调用）──────
def get_order(order_no: str, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    return pg.query_one(
        "SELECT * FROM orders WHERE order_no=%s AND tenant_id=%s",
        (order_no, tenant_id),
    )


def save_order(order: dict, tenant_id: str = DEFAULT_TENANT) -> None:
    pg.execute(
        "INSERT INTO orders(order_no, tenant_id, item, amount, status, logistics,"
        " shipped_at, refund) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT(order_no) DO UPDATE SET"
        "  item=EXCLUDED.item, amount=EXCLUDED.amount, status=EXCLUDED.status,"
        "  logistics=EXCLUDED.logistics, shipped_at=EXCLUDED.shipped_at, refund=EXCLUDED.refund",
        (order["order_no"], tenant_id, order["item"], order["amount"], order["status"],
         order.get("logistics", "-"), order.get("shipped_at", "-"), order.get("refund")),
    )


def set_order_refund(order_no: str, refund: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    n = pg.execute(
        "UPDATE orders SET refund=%s WHERE order_no=%s AND tenant_id=%s",
        (refund, order_no, tenant_id),
    )
    return n > 0


def order_exists(order_no: str) -> bool:
    return pg.query_one("SELECT 1 AS ok FROM orders WHERE order_no=%s", (order_no,)) is not None


def seed_orders(orders: list[dict]) -> int:
    """只在表为空时导入种子数据，避免每次重启覆盖用户新建的订单。"""
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM orders")
        if (cur.fetchone() or {}).get("n"):
            return 0
        for o in orders:
            cur.execute(
                "INSERT INTO orders(order_no, tenant_id, item, amount, status, logistics,"
                " shipped_at, refund) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT(order_no) DO NOTHING",
                (o["order_no"], DEFAULT_TENANT, o["item"], o["amount"], o["status"],
                 o.get("logistics", "-"), o.get("shipped_at", "-"), o.get("refund")),
            )
        return len(orders)


# ── 统计 ────────────────────────────────────────────────────
def stats(tenant_id: str = DEFAULT_TENANT) -> dict:
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE tenant_id=%s", (tenant_id,))
        total = cur.fetchone()["n"]
        cur.execute("SELECT status, COUNT(*) AS n FROM sessions WHERE tenant_id=%s GROUP BY status",
                    (tenant_id,))
        by_status = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(tokens),0) AS t FROM messages"
                    " WHERE tenant_id=%s", (tenant_id,))
        msg_row = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM tickets WHERE tenant_id=%s", (tenant_id,))
        tickets = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM tickets WHERE tenant_id=%s AND status<>'CLOSED'",
                    (tenant_id,))
        open_tickets = cur.fetchone()["n"]
        cur.execute("SELECT source, COUNT(*) AS n FROM messages"
                    " WHERE tenant_id=%s AND role='assistant' GROUP BY source", (tenant_id,))
        by_source = {(r["source"] or "unknown"): r["n"] for r in cur.fetchall()}
        cur.execute("SELECT channel, COUNT(*) AS n FROM sessions WHERE tenant_id=%s GROUP BY channel",
                    (tenant_id,))
        by_channel = {(r["channel"] or "web"): r["n"] for r in cur.fetchall()}

    from . import pg_store
    return {
        "sessions": total,
        "byStatus": by_status,
        "messages": msg_row["n"],
        "tokens": msg_row["t"],
        "kbDocs": pg_store.count(tenant_id),
        "tickets": tickets,
        "openTickets": open_tickets,
        "bySource": by_source,
        "byChannel": by_channel,
    }


def backend() -> str:
    """健康探针用：当前数据库后端标识。"""
    return "postgres"

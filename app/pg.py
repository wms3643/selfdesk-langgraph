"""PostgreSQL 连接层：全项目唯一的数据库入口。

职责只有三件事：
1. 维护进程内连接池，避免每个请求重连（PostgreSQL 建连成本远高于 SQLite 打开文件）
2. 统一事务边界：一次 `cursor()` 是一个事务，正常退出提交、异常回滚
3. 把时间列规范成 `YYYY-MM-DD HH:MM:SS` 字符串，保持 HTTP 契约不变

为什么业务库和 checkpoint 用两个池：
- LangGraph 的 PostgresSaver 要求连接 `autocommit=True`（它自己按语句控制事务）
- 业务写入需要显式事务（一个请求里多条 SQL 要么全成要么全败）
混在同一个池里，autocommit 会互相污染，排查起来非常隐蔽。

这里**不做降级、不吞异常**。上一版有「PG 挂了回落 SQLite」的兜底，结果故障被
掩盖成「数据好像变少了」；现在连不上就抛 PgUnavailable，由启动脚本/接口显式报错。
"""
from __future__ import annotations

import atexit
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .config import PG_DSN, PG_POOL_MAX, PG_POOL_MIN, PG_STARTUP_TIMEOUT

TIME_FMT = "%Y-%m-%d %H:%M:%S"

_business_pool: ConnectionPool | None = None
_checkpoint_pool: ConnectionPool | None = None
_lock = threading.Lock()


class PgUnavailable(RuntimeError):
    """数据库不可用。上层据此返回 503 或让启动失败，而不是悄悄降级。"""


def _build_pool(name: str, min_size: int, max_size: int, autocommit: bool) -> ConnectionPool:
    kwargs: dict[str, Any] = {
        "row_factory": dict_row,
        "application_name": f"selfdesk-{name}",
        # 关掉服务端预处理语句缓存：连接池 + PgBouncer 场景下容易踩到 prepared statement 冲突
        "prepare_threshold": 0,
    }
    if autocommit:
        kwargs["autocommit"] = True
    return ConnectionPool(
        conninfo=PG_DSN,
        min_size=min_size,
        max_size=max_size,
        kwargs=kwargs,
        timeout=10.0,
        max_idle=300.0,
        max_lifetime=3600.0,
        open=False,
    )


def init(wait: bool = True, timeout: float | None = None) -> None:
    """建池并预热。wait=True 时会真的连一次，连不上立刻抛错（启动即失败）。"""
    global _business_pool, _checkpoint_pool
    with _lock:
        if _business_pool is None:
            _business_pool = _build_pool("app", PG_POOL_MIN, PG_POOL_MAX, autocommit=False)
        if _checkpoint_pool is None:
            _checkpoint_pool = _build_pool("checkpoint", 1, max(2, PG_POOL_MAX // 2), autocommit=True)
    for pool in (_business_pool, _checkpoint_pool):
        if pool.closed:
            try:
                pool.open(wait=wait, timeout=timeout or PG_STARTUP_TIMEOUT)
            except Exception as exc:  # PoolTimeout / OperationalError
                raise PgUnavailable(
                    "PostgreSQL 连接失败：" + str(exc).strip().splitlines()[0]
                    + "\n  检查：WSL 是否启动、`pg_ctlcluster 16 main start` 是否执行、"
                    "SELFDESK_PG_DSN 是否正确"
                ) from exc
    # 脚本（seed / reindex）正常退出时优雅关闭，否则 psycopg_pool 会在退出阶段
    # 打印一堆 "couldn't stop thread" 噪音，看起来像报错
    atexit.register(close)


def business_pool() -> ConnectionPool:
    if _business_pool is None:
        init(wait=True)
    assert _business_pool is not None
    return _business_pool


def checkpoint_pool() -> ConnectionPool:
    if _checkpoint_pool is None:
        init(wait=True)
    assert _checkpoint_pool is not None
    return _checkpoint_pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """一个事务。正常退出提交，异常回滚后把异常抛出去。"""
    try:
        with business_pool().connection() as conn:
            yield conn
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise PgUnavailable(f"数据库连接中断：{exc}") from exc


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    with connection() as conn:
        with conn.cursor() as cur:
            yield cur


def query(sql: str, params: tuple | None = None) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params or ())
        return [norm(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple | None = None) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return norm(row) if row else None


def execute(sql: str, params: tuple | None = None) -> int:
    with cursor() as cur:
        cur.execute(sql, params or ())
        return cur.rowcount


def ping() -> bool:
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except Exception:
        return False


def server_version() -> str:
    try:
        row = query_one("SELECT current_setting('server_version') AS v")
        return f"PostgreSQL {row['v']}" if row else "PostgreSQL"
    except Exception:
        return "unavailable"


def norm(value):
    """把 psycopg 返回值转成前端友好的形态：时间 → 本地时间字符串。"""
    if isinstance(value, datetime):
        # TIMESTAMPTZ 转本地时区；naive 时间直接格式化
        return (value.astimezone() if value.tzinfo else value).strftime(TIME_FMT)
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [norm(v) for v in value]
    return value


def close() -> None:
    """进程退出时释放连接（uvicorn 停机、种子脚本结束）。"""
    global _business_pool, _checkpoint_pool
    for pool in (_business_pool, _checkpoint_pool):
        try:
            if pool is not None and not pool.closed:
                pool.close()
        except Exception:
            pass
    _business_pool = None
    _checkpoint_pool = None

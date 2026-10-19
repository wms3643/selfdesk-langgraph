"""pgvector 向量库：知识库的存储与检索（唯一后端，无回落）。

为什么是 PostgreSQL + pgvector：
1. **ANN 索引**（HNSW）：条数上去后不会退化成全表扫描；
2. **向量与元数据同库**：删文档时 chunks 走外键级联，不留孤儿向量；
3. **和业务表同一个实例**：备份、监控、权限只维护一套，不需要额外向量服务；
4. **标准 SQL**：排查问题直接 psql，不依赖某个 Python 进程。

上一版有「PG 不可用则回落 SQLite + numpy 暴力扫描」的兜底，已移除：
向量库故障时检索直接返回空、上层转人工，比偷偷降级成另一套数据源更可预期——
后者会让运维看到「知识库变少了」却找不到原因。
"""
from __future__ import annotations

import re
import time
import uuid

from . import pg
from .config import DEFAULT_TENANT, EMBEDDING_DIM

# 切分参数：中文按字符计，500 字约 350 tokens，留足上下文余量
MAX_CHARS = 500
OVERLAP = 80
MIN_TAIL = 120

_ok_cache = {"ts": 0.0, "ok": False, "version": ""}
_CACHE_TTL = 15.0


def available(force: bool = False) -> bool:
    """探测 PG + pgvector 是否可用。缓存 15 秒，避免每次请求都探。"""
    now = time.time()
    if not force and now - _ok_cache["ts"] < _CACHE_TTL:
        return _ok_cache["ok"]
    ok = False
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            row = cur.fetchone()
            ok = row is not None
            if ok:
                _ok_cache["version"] = str(row["extversion"])
    except Exception:
        ok = False
    _ok_cache.update(ts=now, ok=ok)
    return ok


def ensure_schema() -> bool:
    """建扩展与知识库表。幂等，每次启动执行。"""
    try:
        with pg.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kb_docs (
                    id          TEXT PRIMARY KEY,
                    tenant_id   TEXT NOT NULL DEFAULT 't_default',
                    title       TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    source      TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id          BIGSERIAL PRIMARY KEY,
                    doc_id      TEXT NOT NULL REFERENCES kb_docs(id) ON DELETE CASCADE,
                    tenant_id   TEXT NOT NULL DEFAULT 't_default',
                    title       TEXT,
                    content     TEXT NOT NULL,
                    seq         INT NOT NULL DEFAULT 0,
                    embedding   vector({EMBEDDING_DIM}) NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_tenant ON kb_chunks(tenant_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_docs_tenant ON kb_docs(tenant_id, created_at DESC)")
            # 唯一约束保证同一租户下标题不重复，避免重复灌库
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_docs_title ON kb_docs(tenant_id, title)")
            # HNSW：数据量小时全表扫描更快，量上来后自动受益
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_vec ON kb_chunks
                USING hnsw (embedding vector_cosine_ops)
            """)
        _ok_cache.update(ts=time.time(), ok=True)
        return True
    except Exception:
        return False


def _vec_literal(vec) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def split_text(title: str, content: str) -> list[str]:
    """结构优先、长度兜底的切分。

    1. 短文本（<= 500 字）整条返回 —— FAQ 本身就是最小语义单元，切开反而破坏语义
    2. 长文本先按空行分段，无空行则按句末标点切
    3. 段落合并到 500 字以内，超长段硬切并保留 80 字重叠
    4. 尾块过短并入前一块，避免出现只有一句话的碎块
    """
    text = (content or "").strip()
    if not text:
        return []
    if len(text) <= MAX_CHARS:
        return [text]

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 1:
        paras = [s.strip() for s in re.split(r"(?<=[。！？；])", text) if s.strip()]

    chunks: list[str] = []
    cur = ""
    for p in paras:
        while len(p) > MAX_CHARS:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:MAX_CHARS])
            p = p[MAX_CHARS - OVERLAP:]
        if len(cur) + len(p) <= MAX_CHARS:
            cur += p
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)

    if len(chunks) >= 2 and len(chunks[-1]) < MIN_TAIL:
        chunks[-2] += chunks[-1]
        chunks.pop()
    return chunks


def ingest(title: str, content: str, embed_fn, source: str = "",
           tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """写入一篇文档：切分 → 向量化 → 同事务写入文档与块。

    标题已存在时覆盖（先删旧块再写新块），保证「改一篇 FAQ 不会留下两份」。
    """
    try:
        ensure_schema()
    except Exception:
        return None
    chunks = split_text(title, content)
    if not chunks:
        return None
    # 每块带上标题，检索时能保留文档级语义
    try:
        vecs = embed_fn([f"{title}\n{c}" for c in chunks])
    except Exception:
        return None
    if len(vecs) != len(chunks):
        return None

    doc_id = uuid.uuid4().hex[:12]
    try:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT id FROM kb_docs WHERE tenant_id=%s AND title=%s",
                (tenant_id, title),
            )
            existing = cur.fetchone()
            if existing:
                doc_id = existing["id"]
                cur.execute("DELETE FROM kb_chunks WHERE doc_id=%s", (doc_id,))
                cur.execute(
                    "UPDATE kb_docs SET content=%s, source=%s WHERE id=%s",
                    (content, source, doc_id),
                )
            else:
                cur.execute(
                    "INSERT INTO kb_docs(id, tenant_id, title, content, source)"
                    " VALUES(%s,%s,%s,%s,%s)",
                    (doc_id, tenant_id, title, content, source),
                )
            for i, (c, v) in enumerate(zip(chunks, vecs)):
                cur.execute(
                    "INSERT INTO kb_chunks(doc_id, tenant_id, title, content, seq, embedding)"
                    " VALUES(%s,%s,%s,%s,%s,%s::vector)",
                    (doc_id, tenant_id, title, c, i, _vec_literal(v)),
                )
        return {"doc_id": doc_id, "chunks": len(chunks)}
    except Exception:
        return None


def search(qvec, top_k: int = 4, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """余弦检索。向量库不可用时返回空列表（上层据此转人工，不再回落别处）。"""
    if not available():
        return []
    lit = _vec_literal(qvec)
    try:
        return pg.query(
            "SELECT c.id, c.doc_id, c.title, c.content,"
            "       1 - (c.embedding <=> %s::vector) AS score"
            "  FROM kb_chunks c"
            " WHERE c.tenant_id = %s"
            " ORDER BY c.embedding <=> %s::vector"
            " LIMIT %s",
            (lit, tenant_id, lit, top_k),
        )
    except Exception:
        return []


def list_docs(tenant_id: str = DEFAULT_TENANT, limit: int = 200) -> list[dict]:
    try:
        return pg.query(
            "SELECT id, title, content, source, created_at"
            "  FROM kb_docs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, limit),
        )
    except Exception:
        return []


def count(tenant_id: str = DEFAULT_TENANT) -> int:
    try:
        row = pg.query_one("SELECT COUNT(*) AS n FROM kb_docs WHERE tenant_id=%s", (tenant_id,))
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def chunk_stats(tenant_id: str = DEFAULT_TENANT) -> dict:
    try:
        row = pg.query_one(
            "SELECT COUNT(*) AS n,"
            "       COALESCE(AVG(length(content))::int, 0) AS avg,"
            "       COALESCE(MAX(length(content))::int, 0) AS mx"
            "  FROM kb_chunks WHERE tenant_id=%s",
            (tenant_id,),
        )
        if not row:
            return {}
        return {"chunks": int(row["n"]), "avgChars": int(row["avg"]), "maxChars": int(row["mx"])}
    except Exception:
        return {}


def delete_doc(doc_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    n = pg.execute("DELETE FROM kb_docs WHERE id=%s AND tenant_id=%s", (doc_id, tenant_id))
    return n > 0

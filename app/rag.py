"""检索层：本地 bge-small-zh 服务做向量化 + pgvector 做 ANN 检索。

这里只负责「把问题变成向量、交给向量库、整理结果」，不持有任何存储实现——
存储细节在 pg_store，方便以后换 Milvus 之类的后端时只改一个模块。
"""
from __future__ import annotations

import json
import urllib.request

from . import pg_store
from .config import (DEFAULT_TENANT, EMBEDDING_MODEL, EMBEDDING_URL,
                     RETRIEVE_MIN_SCORE, RETRIEVE_TOP_K)


def embed(texts: list[str]) -> list[list[float]]:
    payload = json.dumps({"model": EMBEDDING_MODEL, "input": texts}).encode()
    req = urllib.request.Request(EMBEDDING_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return [item["embedding"] for item in data["data"]]


def retrieve(query: str, top_k: int = RETRIEVE_TOP_K,
             tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """向量检索。嵌入服务不可用时返回空，由上层走「转人工」而不是硬编答案。"""
    try:
        raw = embed([query])[0]
    except Exception:
        return []
    return pg_store.search(raw, top_k, tenant_id)


def backend() -> str:
    """当前检索后端，供 /api/health 展示。"""
    return "pgvector" if pg_store.available() else "unavailable"


def best_score(docs: list[dict]) -> float:
    return max((d["score"] for d in docs), default=0.0)


def has_reliable_answer(docs: list[dict]) -> bool:
    return best_score(docs) >= RETRIEVE_MIN_SCORE

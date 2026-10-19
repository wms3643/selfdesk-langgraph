"""重建知识库索引：按 kb_docs 原文重新切分 + 重新向量化。

什么时候用：
- 调整了切分参数（MAX_CHARS / OVERLAP）想重跑
- 换了 embedding 模型，维度变了
- 怀疑索引和原文不一致（比如手工改过库）

只动 kb_chunks，不动 kb_docs —— 原文是事实源，索引随时可以重建。
"""
from __future__ import annotations

import sys

from app import db, pg, pg_store, rag


def main() -> int:
    db.init_db()
    if not pg_store.available(force=True):
        print("✗ pgvector 不可用：确认 WSL 已启动且 CREATE EXTENSION vector 成功")
        return 1

    docs = pg_store.list_docs(limit=5000)
    if not docs:
        print("知识库为空，无需重建（可先跑 seed_kb.py）")
        return 0
    print(f"待重建文档: {len(docs)} 篇")

    ok = failed = 0
    for d in docs:
        # ingest 遇到同名标题会覆盖：删旧块 → 写新块，正好就是重建语义
        res = pg_store.ingest(d["title"], d["content"], rag.embed, d.get("source") or "")
        if res:
            ok += 1
            print(f"  ✓ {d['title'][:28]:30} {res['chunks']} 块")
        else:
            failed += 1
            print(f"  ✗ {d['title'][:28]}")

    print(f"\n完成 {ok} 篇 / 失败 {failed} 篇 · 现状 {pg_store.chunk_stats()}")
    pg.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

"""知识库切分：短文本整条、长文本限长、超长段有重叠、尾块合并。"""
from __future__ import annotations

from app import pg_store


def test_empty_returns_nothing():
    assert pg_store.split_text("标题", "") == []
    assert pg_store.split_text("标题", "   \n  ") == []


def test_short_text_kept_whole():
    text = "退款一般 3 个工作日内原路退回。"
    chunks = pg_store.split_text("退款时效", text)
    assert chunks == [text]


def test_exactly_max_chars_is_single_chunk():
    text = "字" * pg_store.MAX_CHARS
    assert len(pg_store.split_text("标题", text)) == 1


def test_long_text_is_split_under_limit():
    text = "退款政策说明。" * 200  # 1400 字
    chunks = pg_store.split_text("退款政策", text)
    assert len(chunks) > 1
    assert all(len(c) <= pg_store.MAX_CHARS for c in chunks)
    # 不应丢字：拼起来长度不小于原文（重叠会多，不会少）
    assert sum(len(c) for c in chunks) >= len(text)


def test_hard_cut_keeps_overlap():
    text = "啊" * (pg_store.MAX_CHARS * 2)  # 无标点无空行，只能硬切
    chunks = pg_store.split_text("长文", text)
    assert len(chunks) >= 2
    assert all(len(c) <= pg_store.MAX_CHARS for c in chunks)
    # 第一块的尾部 OVERLAP 字应出现在第二块开头
    assert chunks[1][: pg_store.OVERLAP] == chunks[0][-pg_store.OVERLAP:]


def test_paragraphs_are_merged_up_to_limit():
    paras = ["第一段内容。" * 20, "第二段内容。" * 20, "第三段内容。" * 20]
    text = "\n\n".join(paras)
    chunks = pg_store.split_text("多段", text)
    assert len(chunks) >= 1
    assert all(len(c) <= pg_store.MAX_CHARS for c in chunks)


def test_tiny_tail_is_merged():
    body = "正文。" * 120          # 480 字，接近上限
    tail = "尾巴。" * 3            # 18 字，低于 MIN_TAIL
    chunks = pg_store.split_text("尾块", body + "\n\n" + tail)
    assert all(len(c) >= pg_store.MIN_TAIL for c in chunks[:-1])
    assert len(chunks[-1]) >= pg_store.MIN_TAIL or len(chunks) == 1

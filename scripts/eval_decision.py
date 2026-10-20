"""决策模型选型：同一批句子跑对照，输出命中率与延迟。

用法：
    set LLM_MODEL=deepseek-flash  && python scripts/eval_decision.py
    set LLM_MODEL=deepseek-v4-pro && python scripts/eval_decision.py

分两组：
- 常见集（24 句）：提示词里给过类似示例，会偏高，用来看"能不能覆盖日常说法"
- 留出集（8 句）：没进过提示词，用来判断真实泛化
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import router, tools  # noqa: E402

HUMAN_WORDS = ("人工", "转人工", "客服人员", "真人", "投诉", "经理")

CASES: list[tuple[str, set[str]]] = [
    ("订单 202610190001 到哪了", {"query_order"}),
    ("我的快递到哪了", {"query_order"}),
    ("202610190001 这个单子的物流信息", {"query_order"}),
    ("帮我看看 202610190003 退款到哪一步了", {"query_refund"}),
    ("我的退款进度怎么样", {"query_refund"}),
    ("我要申请退款 202610190002", {"submit_refund"}),
    ("这单退了吧 202610190001", {"submit_refund"}),
    ("订单号 202610180003 还能退吗", {"query_refund", "submit_refund", "query_order"}),
    ("帮我下单标准版", {"create_order"}),
    ("续费一下专业版", {"create_order"}),
    ("我要买 5 个坐席", {"create_order"}),
    ("我的套餐还有多久到期", {"account_info"}),
    ("余额还剩多少", {"account_info"}),
    ("我上次提的工单处理得怎么样", {"query_ticket"}),
    ("你们几点上班", {"business_info"}),
    ("客服电话多少", {"business_info"}),
    ("退款多久能到账", {"kb"}),
    ("发票怎么开", {"kb"}),
    ("怎么修改密码", {"kb"}),
    ("你好", {"kb"}),
    ("我要转人工", {"human"}),
    ("你们这什么服务，我要投诉", {"human"}),
    ("发什么快递", {"query_order"}),
    ("帮我查下 202610190002 付了多少钱", {"query_order"}),
]

HELDOUT: list[tuple[str, set[str]]] = [
    ("快递到哪儿了", {"query_order"}),
    ("202610190001 什么时候发货", {"query_order"}),
    ("上次那个工单有结果了吗", {"query_ticket"}),
    ("你们的服务时间到几点", {"business_info"}),
    ("我要开专用发票", {"kb", "create_ticket"}),
    ("帮我开一个专业版的账户", {"create_order"}),
    ("我想把订单 202610180003 退掉", {"submit_refund"}),
    ("人工客服", {"human"}),
]


def by_rules(text: str) -> str:
    if any(w in text for w in HUMAN_WORDS):
        return "human"
    return tools.pick_tool(text) or "kb"


def by_model(text: str) -> tuple[str, int]:
    started = time.perf_counter()
    out = router.decide(text)
    return out.get("tool_name") or out["intent"], int((time.perf_counter() - started) * 1000)


def run(name: str, cases: list[tuple[str, set[str]]], detail: bool) -> tuple[int, list[int]]:
    ok, ms_all = 0, []
    if detail:
        print(f"{'句子':<34}{'期望':<26}{'规则':<16}{'模型':<16}{'ms':>6}")
        print("-" * 100)
    for text, expect in cases:
        rule = by_rules(text)
        model, ms = by_model(text)
        ms_all.append(ms)
        hit = model in expect
        ok += hit
        if detail:
            flag = "" if hit else "  <- 错"
            print(f"{text:<34}{'/'.join(sorted(expect)):<26}{rule:<16}{model:<16}{ms:>6}{flag}")
    n = len(cases)
    print(f"{name}: 模型 {ok}/{n} ({ok / n:.0%})   延迟 中位 {statistics.median(ms_all):.0f}ms / 最大 {max(ms_all)}ms")
    return ok, ms_all


def main() -> None:
    print(f"模型: {os.environ.get('LLM_MODEL') or '(默认)'}\n")
    run("常见集", CASES, detail=False)
    print()
    run("留出集", HELDOUT, detail=True)


if __name__ == "__main__":
    main()

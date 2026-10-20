"""决策模型 vs 关键词规则：同一批句子跑对照，看选工具准确率与延迟。

用法：
    python scripts/eval_decision.py

不需要数据库，只调模型与规则，输出一张对照表 + 汇总。
成本：约 24 次短调用，可反复跑。
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import router, tools  # noqa: E402

HUMAN_WORDS = ("人工", "转人工", "客服人员", "真人", "投诉", "经理")

# (句子, 可接受的工具集合；kb=知识问答，human=转人工)
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


def by_rules(text: str) -> str:
    if any(w in text for w in HUMAN_WORDS):
        return "human"
    return tools.pick_tool(text) or "kb"


def by_model(text: str) -> tuple[str, int]:
    started = time.perf_counter()
    out = router.decide(text)
    ms = int((time.perf_counter() - started) * 1000)
    return out.get("tool_name") or out["intent"], ms


def main() -> None:
    rows = []
    rule_ok = model_ok = 0
    model_ms: list[int] = []

    print(f"{'句子':<34}{'期望':<18}{'规则':<16}{'模型':<16}{'ms':>5}")
    print("-" * 92)
    for text, expect in CASES:
        rule = by_rules(text)
        model, ms = by_model(text)
        model_ms.append(ms)
        r_hit = rule in expect
        m_hit = model in expect
        rule_ok += r_hit
        model_ok += m_hit
        exp = "/".join(sorted(expect))
        print(f"{text:<34}{exp:<18}{rule:<16}{model:<16}{ms:>5}"
              f"{'' if m_hit else '  <- 错'}")

    n = len(CASES)
    print("-" * 92)
    print(f"规则命中 {rule_ok}/{n} ({rule_ok / n:.0%})   "
          f"模型命中 {model_ok}/{n} ({model_ok / n:.0%})   "
          f"模型延迟 中位 {statistics.median(model_ms):.0f}ms / 最大 {max(model_ms)}ms")


if __name__ == "__main__":
    main()

"""决策模型的单元测试：用假 LLM 验证漏斗、解析与降级。

不打真实 API，跑得很快；真实效果由 scripts/eval_decision.py 跑对照实验。
"""
from __future__ import annotations

import pytest

from app import graph, router, validate

ORDER = "202610190001"


class FakeResp:
    def __init__(self, tool_calls=None, usage=None):
        self.tool_calls = tool_calls or []
        self.usage_metadata = usage or {}


class FakeLLM:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = 0

    def bind_tools(self, *a, **k):
        return self

    def invoke(self, *a, **k):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._resp


def use(monkeypatch, resp=None, exc=None) -> FakeLLM:
    fake = FakeLLM(resp=resp, exc=exc)
    monkeypatch.setattr(router, "_llm", lambda: fake)
    monkeypatch.delenv("SELFDESK_DECISION", raising=False)
    monkeypatch.setenv("SELFDESK_FASTPATH", "0")   # 这些用例专门测模型
    return fake


# ── 漏斗第 1 级：规则短路，不花模型调用 ──────────────────────
def test_human_words_short_circuit(monkeypatch):
    fake = use(monkeypatch)
    out = router.decide("我要投诉，转人工")
    assert out["intent"] == "human"
    assert out["decision_source"] == "rule"
    assert fake.calls == 0


def test_empty_question(monkeypatch):
    fake = use(monkeypatch)
    out = router.decide("   ")
    assert out["intent"] == "kb" and fake.calls == 0


# ── 漏斗第 2 级：模型给出工具与参数 ─────────────────────────
def test_model_picks_tool_and_args(monkeypatch):
    use(monkeypatch, FakeResp(
        tool_calls=[{"name": "query_order", "args": {"order_no": ORDER}}],
        usage={"total_tokens": 42},
    ))
    out = router.decide(f"订单 {ORDER} 到哪了")
    assert out["intent"] == "biz"
    assert out["tool_name"] == "query_order"
    assert out["tool_args_raw"] == {"order_no": ORDER}
    assert out["decision_source"] == "model"
    assert out["tokens"] == 42


def test_model_returns_no_tool_means_kb(monkeypatch):
    use(monkeypatch, FakeResp(tool_calls=[], usage={"total_tokens": 10}))
    out = router.decide("退款政策是怎么规定的")
    assert out["intent"] == "kb"
    assert "tool_name" not in out


def test_unknown_tool_degrades_to_kb(monkeypatch):
    use(monkeypatch, FakeResp(tool_calls=[{"name": "delete_all_orders", "args": {}}]))
    out = router.decide("把订单全删了")
    assert out["intent"] == "kb"


def test_empty_args_are_dropped(monkeypatch):
    use(monkeypatch, FakeResp(
        tool_calls=[{"name": "submit_refund", "args": {"order_no": ORDER, "reason": ""}}],
    ))
    out = router.decide(f"退 {ORDER}")
    assert out["tool_args_raw"] == {"order_no": ORDER}


# ── 模型挂了退回规则，服务不中断 ───────────────────────────
def test_fallback_to_rules_on_error(monkeypatch):
    use(monkeypatch, exc=RuntimeError("api down"))
    out = router.decide(f"订单 {ORDER} 到哪了")
    assert out["decision_source"] == "fallback"
    assert out["intent"] == "biz" and out["tool_name"] == "query_order"


# ── 模型编造参数：入口校验必须拦下 ─────────────────────────
def test_hallucinated_order_no_is_blocked(monkeypatch):
    use(monkeypatch, FakeResp(tool_calls=[{"name": "query_order",
                                           "args": {"order_no": ORDER}}]))
    state = {"question": "帮我查一下我的订单到哪了"}
    out = router.decide(state["question"])
    state.update(out)
    v = graph.v_order(state)
    assert v["tool_result"]["error"] == "not_grounded"
    assert v["tool_args"] == {}


def test_grounded_model_arg_passes(monkeypatch):
    use(monkeypatch, FakeResp(tool_calls=[{"name": "query_order",
                                           "args": {"order_no": ORDER}}]))
    state = {"question": f"订单 {ORDER} 到哪了"}
    state.update(router.decide(state["question"]))
    v = graph.v_order(state)
    assert v["tool_result"] is None
    assert v["tool_args"] == {"order_no": ORDER}


# ── classify 节点整合 ──────────────────────────────────────
def test_classify_merges_model_output(monkeypatch):
    use(monkeypatch, FakeResp(tool_calls=[{"name": "account_info", "args": {}}]))
    out = graph.classify({"question": "我的套餐什么时候到期"})
    assert out["intent"] == "biz"
    assert out["tool_name"] == "account_info"
    assert out["decision_source"] == "model"


def test_classify_rules_mode(monkeypatch):
    monkeypatch.setenv("SELFDESK_DECISION", "rules")
    out = graph.classify({"question": f"订单 {ORDER} 到哪了"})
    assert out["decision_source"] == "rule"
    assert out["tool_name"] == "query_order"


def test_tool_schemas_cover_registry():
    names = {s["function"]["name"] for s in router.tool_schemas()}
    assert names == set(router.tools.TOOL_REGISTRY)

# ── 高置信快路：带单号时不花模型调用 ─────────────────────
def test_fastpath_query_with_order_no(monkeypatch):
    fake = use(monkeypatch)
    monkeypatch.delenv("SELFDESK_FASTPATH", raising=False)
    out = router.decide(f"订单 {ORDER} 到哪了")
    assert out["decision_source"] == "fastpath"
    assert out["tool_name"] == "query_order"
    assert out["tool_args_raw"] == {"order_no": ORDER}
    assert fake.calls == 0


def test_fastpath_refund_with_order_no(monkeypatch):
    fake = use(monkeypatch)
    monkeypatch.delenv("SELFDESK_FASTPATH", raising=False)
    out = router.decide(f"申请退款 {ORDER}")
    assert out["tool_name"] == "submit_refund"
    assert fake.calls == 0


def test_fastpath_skips_when_no_order_no(monkeypatch):
    use(monkeypatch, FakeResp(tool_calls=[{"name": "query_order", "args": {}}]))
    monkeypatch.delenv("SELFDESK_FASTPATH", raising=False)
    out = router.decide("我的快递到哪了")
    assert out["decision_source"] == "model"


def test_fastpath_can_be_disabled(monkeypatch):
    use(monkeypatch, FakeResp(tool_calls=[{"name": "query_order",
                                           "args": {"order_no": ORDER}}]))
    out = router.decide(f"订单 {ORDER} 到哪了")
    assert out["decision_source"] == "model"

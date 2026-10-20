"""参数前缀校验的回归测试。

覆盖三类：
1. 可溯源判定（含分隔符、短值）
2. 取值优先级（模型参数 → 当前问题 → 最近对话）
3. 两个前缀节点的分支结果与路由
"""
from __future__ import annotations

import pytest

from app import graph, tools, validate

ORDER = "202610190001"


# ── 可溯源 ──────────────────────────────────────────────────
def test_grounded_exact():
    assert validate.grounded(ORDER, f"订单 {ORDER} 到哪了")


def test_grounded_ignores_separators():
    assert validate.grounded(ORDER, "订单 2026 1019 0001 到哪了")
    assert validate.grounded(ORDER, "订单 2026-1019-0001")


def test_not_grounded_when_absent():
    assert not validate.grounded(ORDER, "我的订单到哪了")


def test_short_value_requires_exact_match():
    assert validate.grounded("A12", "编号 A12")
    assert not validate.grounded("A12", "编号 A1234")


# ── 取值优先级 ──────────────────────────────────────────────
def test_pick_from_question():
    value, err = validate.pick_order_no({"question": f"订单 {ORDER} 到哪了"})
    assert (value, err) == (ORDER, None)


def test_pick_from_history():
    state = {"question": "到哪了", "history": [{"role": "user", "content": f"订单 {ORDER}"}]}
    value, err = validate.pick_order_no(state)
    assert (value, err) == (ORDER, None)


def test_model_arg_must_be_grounded():
    state = {"question": "帮我退个款", "tool_args_raw": {"order_no": ORDER}}
    value, err = validate.pick_order_no(state)
    assert value is None and err == "not_grounded"


def test_model_arg_grounded_ok():
    state = {"question": f"帮我退 {ORDER}", "tool_args_raw": {"order_no": ORDER}}
    value, err = validate.pick_order_no(state)
    assert (value, err) == (ORDER, None)


def test_missing_arg():
    value, err = validate.pick_order_no({"question": "我要退款"})
    assert value is None and err == "missing_arg"


# ── v_order ────────────────────────────────────────────────
def test_v_order_missing():
    out = graph.v_order({"question": "我的快递到哪了"})
    assert out["tool_result"]["error"] == "missing_arg"
    assert out["tool_args"] == {}


def test_v_order_ok():
    out = graph.v_order({"question": f"订单 {ORDER} 到哪了"})
    assert out["tool_result"] is None
    assert out["tool_args"] == {"order_no": ORDER}


# ── v_refund ───────────────────────────────────────────────
def test_v_refund_missing():
    out = graph.v_refund({"question": "我要退款"})
    assert out["tool_result"]["error"] == "missing_arg"


def test_v_refund_not_found(monkeypatch):
    monkeypatch.setattr(graph.db, "get_order", lambda *a, **k: None)
    out = graph.v_refund({"question": f"退 {ORDER}"})
    assert out["tool_result"]["error"] == "not_found"


def test_v_refund_already_refunded(monkeypatch):
    monkeypatch.setattr(graph.db, "get_order",
                        lambda *a, **k: {"order_no": ORDER, "refund": "退款已到账"})
    out = graph.v_refund({"question": f"退 {ORDER}"})
    assert out["tool_result"]["ok"] is True
    assert out["tool_result"]["done"] is True


def test_v_refund_pass(monkeypatch):
    monkeypatch.setattr(graph.db, "get_order",
                        lambda *a, **k: {"order_no": ORDER, "refund": None})
    out = graph.v_refund({"question": f"退 {ORDER}"})
    assert out["tool_result"] is None
    assert out["tool_args"] == {"order_no": ORDER}


# ── 路由 ───────────────────────────────────────────────────
def test_route_order_tools_go_to_validator(monkeypatch):
    monkeypatch.delenv("SELFDESK_ARG_VALIDATE", raising=False)
    assert graph._route_after_classify({"intent": "biz", "tool_name": "query_order"}) == "v_order"
    assert graph._route_after_classify({"intent": "biz", "tool_name": "submit_refund"}) == "v_refund"


def test_route_no_param_tool_skips_validator(monkeypatch):
    monkeypatch.delenv("SELFDESK_ARG_VALIDATE", raising=False)
    assert graph._route_after_classify({"intent": "biz", "tool_name": "account_info"}) == "tool"


def test_route_validator_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SELFDESK_ARG_VALIDATE", "0")
    assert graph._route_after_classify({"intent": "biz", "tool_name": "query_order"}) == "tool"


@pytest.mark.parametrize("result,expect", [
    (None, "call_tool"),
    ({"ok": False, "error": "missing_arg"}, "answer"),
    ({"ok": True, "done": True}, "answer"),
    ({"ok": True}, "call_tool"),
])
def test_route_after_validate(result, expect):
    assert graph._route_after_validate({"tool_result": result}) == expect


# ── 参数不被二次抽取覆盖 ───────────────────────────────────
def test_run_tool_uses_validated_args(monkeypatch):
    captured = {}

    def fake(order_no):
        captured["order_no"] = order_no
        return {"ok": True}

    monkeypatch.setitem(tools.TOOL_REGISTRY["query_order"], "fn", fake)
    tools.run_tool("query_order", "看下订单", args={"order_no": ORDER})
    assert captured["order_no"] == ORDER

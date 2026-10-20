"""意图路由与只读工具：不碰数据库，纯规则。"""
from __future__ import annotations

import pytest

from app import tools


@pytest.mark.parametrize(
    "question,expected",
    [
        ("帮我查一下订单 202610190001 到哪了", "query_order"),
        ("订单 202610190001 退款到账了吗", "query_refund"),
        ("我要申请退款", "submit_refund"),
        ("帮我下单标准版", "create_order"),
        ("你们营业时间是几点", "business_info"),
        ("我这个月的套餐还有多少坐席", "account_info"),
    ],
)
def test_routing_hits_expected_tool(question, expected):
    assert tools.pick_tool(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "你好",
        "退款多久到账",
        "发票怎么开",
        "标准版多少钱",
    ],
)
def test_policy_questions_go_to_knowledge_base(question):
    # 这些是政策/闲聊，不该弹工具
    assert tools.pick_tool(question) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("订单 202610190001", "202610190001"),
        ("单号是 202610180003。", "202610180003"),
        ("1234567890", "1234567890"),
        ("没有单号", None),
        ("12345", None),           # 太短
        ("1234567890123456789", None),  # 太长
    ],
)
def test_extract_order_no(text, expected):
    assert tools.extract_order_no(text) == expected


def test_query_order_missing_number():
    res = tools.query_order(None)
    assert res["ok"] is False
    assert res["error"] == "missing_order_no"


def test_query_order_not_found(monkeypatch):
    # 只验证分支逻辑，不依赖数据库
    monkeypatch.setattr(tools.db, "get_order", lambda _no: None)
    res = tools.query_order("999999999999")
    assert res["ok"] is False
    assert res["error"] == "not_found"


def test_business_info_contains_hours():
    res = tools.business_info("营业时间")
    assert res["ok"] is True
    assert "9:00" in res["message"]


def test_product_catalog_prices_are_positive():
    assert tools.PRODUCTS
    assert all(price > 0 for price in tools.PRODUCTS.values())

"""参数前缀校验：在进入业务流程之前，先判断"参数够不够、可不可信"。

三条原则：

1. **节点分开、规则共享**——每个流程有自己的 `v_*` 节点（v_order / v_refund），
   但判断逻辑全部调用本模块，避免同一条安全规则在 N 处各写一遍、改一处漏三处。
2. **只做入口能判定的事**——齐全性、可溯源性（参数是否真的出现在用户原话/上下文里）。
   存在性与归属交给工具和数据库：`db.get_order(order_no, tenant_id)` 本身就带租户过滤，
   查不到就是查不到，不在这里重复实现。
3. **失败不是异常，是数据**——统一返回 `{ok: False, error: <code>, field, message}`，
   由 decide 节点按错误码决定"追问"还是"转人工"。

为什么需要这层：参数如果由模型抽取（function calling），模型可能编出一个不存在的单号。
现在参数由正则从原话里抄，天然可信；但一旦换成模型抽取，这条约束就消失了。
把校验放在流程入口，两种抽取方式都能复用同一套守卫。

开关：`SELFDESK_ARG_VALIDATE=0` 关闭（退回旧路径，classify 直接进 call_tool）。
"""
from __future__ import annotations

import os
import re

from . import tools

# 错误码 → 后续动作的约定（decide 节点依赖这个划分）
#   追问类：missing_arg / not_grounded / bad_tool
#   转人类：not_found / forbidden
CLARIFY_CODES = {"missing_arg", "not_grounded", "bad_tool"}
TRANSFER_CODES = {"not_found", "forbidden"}


def enabled() -> bool:
    return os.environ.get("SELFDESK_ARG_VALIDATE", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def fail(code: str, field: str, message: str) -> dict:
    """统一的失败结构。字段名固定，answer/decide 才能稳定处理。"""
    return {"ok": False, "error": code, "field": field, "message": message}


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def grounded(value: str, *texts: str) -> bool:
    """参数是否能在给定文本里找到（忽略空格、连字符等分隔）。

    用于挡住"模型凭空生成参数"：单号必须是用户说过的那串数字。
    短值（<6 位）不做忽略分隔符的匹配，避免 "12" 命中任何文本。
    """
    if not value:
        return False
    raw = value.strip()
    if len(_digits(raw)) < 6:
        # 短值必须整词命中："A12" 不能命中 "A1234"
        pattern = r"(?<![A-Za-z0-9])" + re.escape(raw) + r"(?![A-Za-z0-9])"
        return any(re.search(pattern, t or "") for t in texts)
    needle = _digits(raw)
    return any(needle and needle in _digits(t) for t in texts)


def context_text(state: dict, turns: int = 4) -> str:
    """当前问题 + 最近几轮对话，作为"可溯源"的文本范围。

    只回看几轮：太久之前的单号不该被当成这一次的意图依据，
    也避免用户长篇聊天时把整段历史都塞进匹配。
    """
    parts = [state.get("question") or ""]
    history = state.get("history") or []
    for item in history[-turns:]:
        parts.append(str(item.get("content") or ""))
    return "\n".join(parts)


def pick_order_no(state: dict) -> tuple[str | None, str | None]:
    """取订单号，返回 (值, 错误来源)。

    优先级：决策模型给的 args（若将来接入 function calling）→ 当前问题 → 最近对话。
    模型给的值必须可溯源，否则判 not_grounded。
    """
    raw_args = state.get("tool_args_raw") or {}
    from_model = raw_args.get("order_no")
    question = state.get("question") or ""
    ctx = context_text(state)

    if from_model:
        if not grounded(from_model, question, ctx):
            return None, "not_grounded"
        return str(from_model), None

    for source in (question, ctx):
        value = tools.extract_order_no(source)
        if value:
            return value, None
    return None, "missing_arg"

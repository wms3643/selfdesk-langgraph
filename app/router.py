"""决策模型：一次调用同时给出「意图 + 工具 + 参数」。

为什么单独一层：
- 规则版 `pick_tool` 只有关键词，换个说法就漏；参数靠正则，多单号、口语、指代都容易错。
- 这里用 function calling，把「选哪个工具」和「参数从哪来」交给同一次模型调用，
  避免"分类用一套词、选工具用另一套词"各自漂移。

漏斗结构（两级）：
    第 1 级：硬规则，零成本短路——明确要人工、空消息；
    第 2 级：决策模型——其余全部走模型，返回 tool_call 就执行，没有 tool_call 就当知识问答。

安全边界（不因为上了模型就放松）：
- 模型给的参数一律当"待校验"处理，交给 `v_*` 前缀节点校验可溯源性：
  参数必须是用户说过的那串数字，否则判 `not_grounded`。
- 模型给出未注册的工具名，直接降级为知识问答，不执行任何写操作。
- 决策的 temperature=0，同一句话结果稳定，便于回归测试。

开关：`SELFDESK_DECISION=model|rules`，默认 model；置 rules 退回纯关键词。
"""
from __future__ import annotations

import os
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from . import tools
from .config import LLM_BASE_URL, LLM_MODEL, load_api_key

HUMAN_WORDS = ("人工", "转人工", "客服人员", "真人", "投诉", "经理")

DECISION_PROMPT = """你是 SelfDesk 在线客服的分诊器。判断用户这句话要不要调工具，要调就一次调对。

判断顺序：
1. 出现「人工 / 转人工 / 投诉 / 经理」→ 不调工具（系统会转人工）
2. 在问政策、流程、怎么操作、价格表这类公共信息 → 不调工具（交给知识库）
3. 其余涉及「他自己的订单 / 退款 / 套餐 / 工单 / 公司联系方式」→ 调工具

关键规则：
- **写操作也要调工具**：用户说「申请退款」「退了吧」「下单」「续费」「买 N 个坐席」都是写操作，
  调 submit_refund / create_order。不要因为"涉及钱"就不敢调——系统有两阶段确认，不会直接执行。
- **缺参数也要调**：用户没给订单号/产品名就先留空，系统会追问。不要因为参数不全而放弃调工具。
- 用户提到物流、快递、发货、签收 → query_order（哪怕没给单号）。
- 不确定是查订单还是查退款时：句子里有「退款/退钱/到账」→ query_refund，否则 query_order。

参数要求：
- 订单号必须是用户原话里出现过的数字串，一位都不能补，没说就留空。
- 产品名用用户原话（标准版 / 专业版 / 坐席包），没提就留空。

示例：
- 「我要申请退款 202610190002」→ submit_refund(order_no=202610190002)
- 「这单退了吧」→ submit_refund(order_no 留空)
- 「帮我下单标准版」→ create_order(product=标准版)
- 「我要买 5 个坐席」→ create_order()
- 「发什么快递」→ query_order()
- 「退款多久能到账」→ 不调工具
- 「订单号 202610180003 还能退吗」→ query_refund(order_no=202610180003)
"""


def mode() -> str:
    return (os.environ.get("SELFDESK_DECISION") or "model").strip().lower()


def enabled() -> bool:
    return mode() != "rules"


def tool_schemas() -> list[dict]:
    """把 TOOL_REGISTRY 转成 OpenAI function calling 的 schema。"""
    schemas = []
    for name, spec in tools.TOOL_REGISTRY.items():
        properties = {p: {"type": "string", "description": f"{p}，没有就留空"} for p in spec["params"]}
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec["desc"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [],
                },
            },
        })
    return schemas


def _llm():
    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=load_api_key(),
        base_url=LLM_BASE_URL,
        temperature=0,
        timeout=20,
        max_retries=1,
    )


def decide(question: str) -> dict:
    """返回 {intent, tool_name?, tool_args_raw?, decision_source, decision_ms, tokens}。

    失败一律降级为知识问答：宁可多检索一次，也不要瞎执行工具。
    """
    started = time.perf_counter()
    text = (question or "").strip()

    # ── 漏斗第 1 级：硬规则短路（不花模型调用） ──
    if not text:
        return {"intent": "kb", "decision_source": "rule", "decision_ms": 0, "tokens": 0}
    if any(w in text for w in HUMAN_WORDS):
        return {"intent": "human", "decision_source": "rule",
                "decision_ms": int((time.perf_counter() - started) * 1000), "tokens": 0}

    # ── 漏斗第 2 级：决策模型 ──
    try:
        bound = _llm().bind_tools(tool_schemas(), tool_choice="auto")
        resp = bound.invoke([SystemMessage(DECISION_PROMPT), HumanMessage(text)])
    except Exception:
        # 模型不可用：退回规则，保证服务可用
        name = tools.pick_tool(text)
        return {"intent": "biz" if name else "kb", **({"tool_name": name} if name else {}),
                "decision_source": "fallback",
                "decision_ms": int((time.perf_counter() - started) * 1000), "tokens": 0}

    usage = getattr(resp, "usage_metadata", None) or {}
    tokens = int(usage.get("total_tokens") or 0)
    elapsed = int((time.perf_counter() - started) * 1000)
    calls = getattr(resp, "tool_calls", None) or []

    if not calls:
        return {"intent": "kb", "decision_source": "model", "decision_ms": elapsed, "tokens": tokens}

    call = calls[0] or {}
    name = call.get("name") or ""
    args = call.get("args") or {}

    # 模型编了个不存在的工具：不执行，降级为知识问答
    if name not in tools.TOOL_REGISTRY:
        return {"intent": "kb", "decision_source": "model",
                "decision_ms": elapsed, "tokens": tokens}

    return {
        "intent": "biz",
        "tool_name": name,
        "tool_args_raw": {k: v for k, v in args.items() if v not in (None, "")},
        "decision_source": "model",
        "decision_ms": elapsed,
        "tokens": tokens,
    }

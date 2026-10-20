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

# 决策与生成可以分开选模型：决策要快、要稳、要便宜；生成要文笔
DECISION_MODEL = os.environ.get("SELFDESK_DECISION_MODEL") or LLM_MODEL
DECISION_BASE_URL = os.environ.get("SELFDESK_DECISION_BASE_URL") or LLM_BASE_URL
DECISION_API_KEY = os.environ.get("SELFDESK_DECISION_API_KEY")

HUMAN_WORDS = ("人工", "转人工", "客服人员", "真人", "投诉", "经理")

DECISION_PROMPT = """你是客服分诊器：判断要不要调工具、调哪个。

- 要人工/投诉 → 不调工具
- 政策、流程、价格表等公共信息 → 不调工具
- 涉及用户自己的订单、退款、套餐、工单、公司联系方式 → 调工具
- 写操作（申请退款/退了吧/下单/续费/买坐席）也要调工具，系统会二次确认，不要因为涉及钱就不敢调
- 缺参数也照调，参数留空，系统会追问
- 订单号只能是用户原话里的数字，不能自己补

示例：「我要申请退款 202610190002」→ submit_refund；「这单退了吧」→ submit_refund；
「发什么快递」→ query_order；「退款多久能到账」→ 不调工具
"""


def mode() -> str:
    return (os.environ.get("SELFDESK_DECISION") or "model").strip().lower()


def enabled() -> bool:
    return mode() != "rules"


def tool_schemas() -> list[dict]:
    """把 TOOL_REGISTRY 转成 OpenAI function calling 的 schema。"""
    schemas = []
    for name, spec in tools.TOOL_REGISTRY.items():
        properties = {p: {"type": "string"} for p in spec["params"]}
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
        model=DECISION_MODEL,
        api_key=DECISION_API_KEY or load_api_key(),
        base_url=DECISION_BASE_URL,
        temperature=0,
        timeout=20,
        max_retries=1,
    )



# ── 高置信快路：带单号且动作明确，规则直接定，不花模型调用 ──
FASTPATH_REFUND = ("申请退款", "退款", "退钱", "退了吧", "退掉", "要退")
FASTPATH_QUERY = ("物流", "快递", "发货", "签收", "到哪", "多少钱", "付了", "状态", "查一下", "查下")
FASTPATH_PROGRESS = ("到哪一步", "进度", "到哪了", "到哪", "怎么样了", "审核到")


def fastpath_enabled() -> bool:
    return (os.environ.get("SELFDESK_FASTPATH") or "1").strip().lower() not in ("0", "false", "off", "no")


def _fast_path(text: str) -> dict | None:
    """只处理"有单号"这一类高信号输入。

    没有单号时不做快路：用户可能只是在问政策，交给模型判断更稳。
    """
    order_no = tools.extract_order_no(text)
    if not order_no:
        return None
    if any(w in text for w in FASTPATH_REFUND):
        # 「退款到哪一步了」是问进度，不是要提交——先看进度词
        if any(w in text for w in FASTPATH_PROGRESS):
            return {"intent": "biz", "tool_name": "query_refund",
                    "tool_args_raw": {"order_no": order_no}, "decision_source": "fastpath"}
        return {"intent": "biz", "tool_name": "submit_refund",
                "tool_args_raw": {"order_no": order_no}, "decision_source": "fastpath"}
    if any(w in text for w in FASTPATH_QUERY):
        return {"intent": "biz", "tool_name": "query_order",
                "tool_args_raw": {"order_no": order_no}, "decision_source": "fastpath"}
    # 只报了个单号，默认按查询处理
    return {"intent": "biz", "tool_name": "query_order",
            "tool_args_raw": {"order_no": order_no}, "decision_source": "fastpath"}

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

    if fastpath_enabled():
        hit = _fast_path(text)
        if hit:
            hit.update({"decision_ms": int((time.perf_counter() - started) * 1000), "tokens": 0})
            return hit

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

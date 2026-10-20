"""LangGraph 状态图：单 Agent + 流程编排。

图结构：
    START → classify ─┬─ kb    → retrieve ─┐
                      ├─ biz   → call_tool ─┼→ answer → decide ─┬→ END
                      └─ human ─────────────┴───────────────────┴→ handoff（挂起等坐席）

设计取舍：
- 只有一个 LLM 节点（answer），其余都是确定性节点，延迟可控、行为可预测
- 人工接管用 interrupt 实现，图会真正挂起，坐席回复后 resume，状态由 checkpoint 保存
"""
from __future__ import annotations

import concurrent.futures
import time
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import db, pg, rag, router, tools, validate
from .config import (ESCALATE_ON_LOW_SCORE, LLM_BASE_URL, LLM_MODEL,
                     LLM_TEMPERATURE, RETRIEVE_TOP_K, load_api_key)

HUMAN_WORDS = ("人工", "转人工", "客服人员", "真人", "投诉", "经理")
# 工具关键词已收敛到 tools.pick_tool（单一事实源），这里不再维护第二份，
# 避免「判断走业务」和「选哪个工具」两套规则各自漂移

STRONG_CONFIRM = ("确认", "确定提交", "同意退款", "提交吧", "确认提交")
SHORT_CONFIRM = ("是", "对", "好", "可以", "嗯", "ok", "yes", "行", "提交")
CANCEL_WORDS = ("取消", "不要", "算了", "不用了", "撤销", "别退", "no")

# 只有这些错误才升级人工；其余（缺参数、无工单、内部异常）先引导用户
TRANSFER_ERRORS = {"not_found"}

# 需要订单号的工具 → 各自的入口校验节点
ORDER_TOOLS = {"query_order", "query_refund"}
REFUND_TOOLS = {"submit_refund"}


def _is_confirm(text: str) -> bool:
    """判断是否为确认。刻意收紧：长句里出现「对」不算确认，避免误触发写操作。"""
    t = (text or "").strip().lower()
    if any(w in t for w in STRONG_CONFIRM):
        return True
    return len(t) <= 4 and any(w in t for w in SHORT_CONFIRM)

SYSTEM_PROMPT = """你是 SelfDesk 的在线客服助手。请遵守：
1. 只能依据【知识片段】和【工具结果】回答，不要编造价格、政策、时效、承诺。
2. 信息不足或超出范围时，直接说明需要转接人工，不要猜测。
3. 回答口语化、简洁，不超过 120 字，不要用 Markdown 列表或标题。
4. 不要提及"知识片段""工具结果"这类内部说法。"""


class ChatState(TypedDict, total=False):
    session_id: str
    tenant_id: str
    channel: str
    question: str
    history: list[dict]
    intent: str
    tool_name: str
    tool_args: dict
    tool_args_raw: dict
    decision_source: str
    decision_ms: int
    decision_tokens: int
    pending_action: dict | None
    action_decision: str
    docs: list[dict]
    tool_result: dict | None
    answer: str
    need_human: bool
    human_reason: str
    tokens: int
    latency_ms: int


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=load_api_key(),
        base_url=LLM_BASE_URL,
        temperature=LLM_TEMPERATURE,
        timeout=60,
        max_retries=1,
    )


def guard(state: ChatState) -> dict:
    """写操作安全闸门 —— 图的第一站。

    整张图里唯一「不关心用户问了什么、只关心有没有待办」的节点。
    作用：任何写操作都必须经过一次明确的用户同意，模型和规则都不能自己拍板。
    无待办时零成本放行，不增加正常问答的延迟。
    """
    pending = db.get_pending(state["session_id"])
    if not pending:
        return {"pending_action": None, "action_decision": "none"}

    q = state.get("question") or ""
    if any(w in q for w in CANCEL_WORDS):
        db.finish_pending(pending["id"], "CANCELLED")
        return {"pending_action": pending, "action_decision": "cancel"}
    if _is_confirm(q):
        return {"pending_action": pending, "action_decision": "confirm"}
    # 用户说了别的事：保留待确认，正常走问答，不打断他
    return {"pending_action": pending, "action_decision": "none"}


def execute_action(state: ChatState) -> dict:
    started = time.perf_counter()
    pending = state.get("pending_action") or {}
    result = tools.execute_pending(state["session_id"], pending)
    return {"answer": result.get("message", ""), "intent": "action",
            "tool_result": result, "tool_name": pending.get("action", ""),
            "tokens": 0, "latency_ms": int((time.perf_counter() - started) * 1000)}


def cancel_action(state: ChatState) -> dict:
    return {"answer": "好的，已取消本次操作，没有任何变更。如需其他帮助随时说。",
            "intent": "action", "tokens": 0, "latency_ms": 0}


def classify(state: ChatState) -> dict:
    """意图识别 + 参数抽取：漏斗结构。

    第 1 级是硬规则（明确要人工、空消息），零成本短路；
    第 2 级是决策模型，一次返回 tool_call（工具名 + 参数）。
    结果里的参数是"待校验"状态，由 v_* 前缀节点做可溯源校验后才执行。

    SELFDESK_DECISION=rules 时退回纯关键词规则（旧行为）。
    """
    q = state.get("question") or ""

    if router.enabled():
        out = router.decide(q)
        merged: dict = {
            "intent": out.get("intent", "kb"),
            "decision_source": out.get("decision_source", ""),
            "decision_ms": int(out.get("decision_ms") or 0),
            "decision_tokens": int(out.get("tokens") or 0),
        }
        if out.get("tool_name"):
            merged["tool_name"] = out["tool_name"]
        if out.get("tool_args_raw"):
            merged["tool_args_raw"] = out["tool_args_raw"]
        if merged["intent"] == "human":
            merged["human_reason"] = "用户要求人工"
        return merged

    # 规则版：关键词命中即定
    if any(w in q for w in HUMAN_WORDS):
        return {"intent": "human", "human_reason": "用户要求人工", "decision_source": "rule"}
    name = tools.pick_tool(q)
    if name:
        return {"intent": "biz", "tool_name": name, "decision_source": "rule"}
    return {"intent": "kb", "decision_source": "rule"}


def retrieve(state: ChatState) -> dict:
    return {"docs": rag.retrieve(state["question"], RETRIEVE_TOP_K,
                                 state.get("tenant_id") or db.DEFAULT_TENANT)}


def v_order(state: ChatState) -> dict:
    """查单类流程的入口校验：单号有没有、是不是用户真的说过。"""
    order_no, err = validate.pick_order_no(state)
    if err == "not_grounded":
        raw = (state.get("tool_args_raw") or {}).get("order_no", "")
        return {"tool_args": {},
                "tool_result": validate.fail("not_grounded", "order_no",
                                             f"对话里没有出现订单号 {raw}，请确认后再试。")}
    if not order_no:
        return {"tool_args": {},
                "tool_result": validate.fail("missing_arg", "order_no",
                                             "没有识别到订单号，请提供 10~18 位订单号。")}
    return {"tool_args": {"order_no": order_no}, "tool_result": None}


def v_refund(state: ChatState) -> dict:
    """退款流程的入口校验：单号 + 订单存在 + 没有已经提交过的退款。

    已退款属于"短路成功"——不用进工具，直接告诉用户结果。
    """
    order_no, err = validate.pick_order_no(state)
    if err == "not_grounded":
        raw = (state.get("tool_args_raw") or {}).get("order_no", "")
        return {"tool_args": {},
                "tool_result": validate.fail("not_grounded", "order_no",
                                             f"对话里没有出现订单号 {raw}，请确认后再试。")}
    if not order_no:
        return {"tool_args": {},
                "tool_result": validate.fail("missing_arg", "order_no",
                                             "申请退款需要订单号，请提供 10~18 位订单号。")}

    order = db.get_order(order_no, state.get("tenant_id") or db.DEFAULT_TENANT)
    if not order:
        # 不存在与越权共用一句话，避免泄露"这个单号存在但不属于你"
        return {"tool_args": {"order_no": order_no},
                "tool_result": validate.fail("not_found", "order_no",
                                             f"没有查到订单 {order_no}，请核对单号或转人工。")}
    if order.get("refund"):
        return {"tool_args": {"order_no": order_no},
                "tool_result": {"ok": True, "done": True,
                                "message": (f"订单 {order_no} 已有退款记录："
                                            f"{order['refund']}，无需重复提交。")}}
    return {"tool_args": {"order_no": order_no}, "tool_result": None}


def call_tool(state: ChatState) -> dict:
    """业务查询：执行 classify 选定的工具，同时检索知识库做兜底。

    兜底的意义：工具没命中（比如单号不存在）时，知识库里的政策说明仍能派上用场。
    """
    tid = state.get("tenant_id") or db.DEFAULT_TENANT
    q = state["question"]
    name = state.get("tool_name") or tools.pick_tool(q) or ""
    result = tools.run_tool(name, q, state.get("session_id", ""),
                            args=state.get("tool_args"))
    docs = rag.retrieve(q, 3, tid)
    return {"tool_result": result, "docs": docs, "tool_name": name}


def answer(state: ChatState) -> dict:
    """唯一的 LLM 节点。"""
    started = time.perf_counter()

    # 写操作的确认提示必须原样返回：交给模型改写的话，
    # 「回复确认」这类关键指令可能被润色掉，用户就不知道该怎么确认了。
    early = state.get("tool_result")
    if early and early.get("need_confirm"):
        return {"answer": early.get("message", ""), "tokens": 0,
                "latency_ms": int((time.perf_counter() - started) * 1000)}

    parts: list[str] = []

    docs = state.get("docs") or []
    if docs:
        blocks = [f"{i+1}. {d['title']}（相关度 {d['score']}）\n{d['content']}"
                  for i, d in enumerate(docs)]
        parts.append("【知识片段】\n" + "\n".join(blocks))

    tool_result = state.get("tool_result")
    if tool_result:
        parts.append("【工具结果】\n" + tool_result.get("message", ""))

    # 有待确认的写操作时，明确告知模型，让它在回答末尾提醒一句。
    # 不靠模型从对话历史里自己推断——那不稳，用户插一句别的话题就可能忘掉。
    pending = state.get("pending_action")
    if pending:
        parts.append("【待确认操作】用户还有一项写操作未确认（"
                     + str(pending.get("action")) + "），请在回答末尾用一句话提醒他回复「确认」或「取消」。")

    context = "\n\n".join(parts) if parts else "（无可用资料）"

    history = state.get("history") or []
    messages: list[tuple[str, str]] = [("system", SYSTEM_PROMPT)]
    for item in history[-6:]:
        role = "assistant" if item["role"] == "assistant" else "user"
        messages.append((role, item["content"]))
    messages.append(("user", f"{context}\n\n【用户问题】\n{state['question']}"))

    try:
        resp = _llm().invoke(messages)
        text = (resp.content or "").strip()
        usage = getattr(resp, "usage_metadata", None) or {}
        tokens = int(usage.get("total_tokens") or 0)
    except Exception as exc:  # 模型不可用时退化，不阻塞会话
        text = "抱歉，我这边暂时无法生成回答，需要为你转接人工客服。"
        tokens = 0
        state["human_reason"] = f"模型调用失败：{exc}"[:120]

    return {
        "answer": text,
        "tokens": tokens,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def decide(state: ChatState) -> dict:
    """是否需要人工介入。"""
    if state.get("human_reason"):
        return {"need_human": True}
    q = state["question"]
    if any(w in q for w in HUMAN_WORDS):
        return {"need_human": True, "human_reason": "用户要求人工"}

    tool_result = state.get("tool_result")
    if tool_result:
        if tool_result.get("ok"):
            return {"need_human": False, "human_reason": ""}
        # 只有「确实查不到该记录」才值得转人工核实。
        # 缺参数、没有工单、工具内部异常都应该先引导用户，否则会把正常用户困在人工队列里。
        if tool_result.get("error") in TRANSFER_ERRORS:
            return {"need_human": True, "human_reason": "业务查询未命中"}
        return {"need_human": False, "human_reason": ""}

    docs = state.get("docs") or []
    if ESCALATE_ON_LOW_SCORE:
        if not docs:
            return {"need_human": True, "human_reason": "知识库无匹配"}
        if not rag.has_reliable_answer(docs):
            return {"need_human": True, "human_reason": "知识库相关度过低"}
    return {"need_human": False, "human_reason": ""}


def handoff(state: ChatState) -> dict:
    """挂起等待坐席；坐席回复后由 resume 唤醒。"""
    payload = {
        "reason": state.get("human_reason") or "需要人工确认",
        "question": state.get("question", ""),
        "draft": state.get("answer", ""),
    }
    agent_reply = interrupt(payload)
    return {"answer": str(agent_reply or "").strip() or state.get("answer", "")}


def _route_after_guard(state: ChatState) -> str:
    d = state.get("action_decision")
    if d == "confirm":
        return "execute"
    if d == "cancel":
        return "cancel"
    return "classify"


def _route_after_classify(state: ChatState) -> str:
    intent = state.get("intent")
    if intent == "human":
        return "handoff"
    if intent == "biz":
        # 需要参数的流程先过各自的入口校验；无参数的工具直接执行
        if validate.enabled():
            name = state.get("tool_name") or ""
            if name in ORDER_TOOLS:
                return "v_order"
            if name in REFUND_TOOLS:
                return "v_refund"
        return "tool"
    return "retrieve"


def _route_after_validate(state: ChatState) -> str:
    """校验通过进工具，不通过直接去 answer（那里会把 fail 的 message 原样说出）。"""
    result = state.get("tool_result")
    if result and (not result.get("ok") or result.get("done")):
        return "answer"
    return "call_tool"


def _route_after_decide(state: ChatState) -> str:
    return "handoff" if state.get("need_human") else "end"


def build_graph(checkpointer=None):
    builder = StateGraph(ChatState)
    builder.add_node("guard", guard)
    builder.add_node("execute", execute_action)
    builder.add_node("cancel", cancel_action)
    builder.add_node("classify", classify)
    builder.add_node("retrieve", retrieve)
    builder.add_node("v_order", v_order)
    builder.add_node("v_refund", v_refund)
    builder.add_node("call_tool", call_tool)
    builder.add_node("answer", answer)
    builder.add_node("decide", decide)
    builder.add_node("handoff", handoff)

    builder.add_edge(START, "guard")
    builder.add_conditional_edges("guard", _route_after_guard,
                                  {"classify": "classify", "execute": "execute", "cancel": "cancel"})
    builder.add_conditional_edges("classify", _route_after_classify,
                                  {"retrieve": "retrieve", "tool": "call_tool",
                                   "v_order": "v_order", "v_refund": "v_refund",
                                   "handoff": "handoff"})
    builder.add_conditional_edges("v_order", _route_after_validate,
                                  {"call_tool": "call_tool", "answer": "answer"})
    builder.add_conditional_edges("v_refund", _route_after_validate,
                                  {"call_tool": "call_tool", "answer": "answer"})
    builder.add_edge("retrieve", "answer")
    builder.add_edge("call_tool", "answer")
    builder.add_edge("answer", "decide")
    builder.add_conditional_edges("decide", _route_after_decide, {"end": END, "handoff": "handoff"})
    builder.add_edge("handoff", END)
    builder.add_edge("execute", END)
    builder.add_edge("cancel", END)
    return builder.compile(checkpointer=checkpointer or _make_checkpointer())


def _make_checkpointer():
    """持久化 checkpoint（PostgreSQL 独立表），服务重启后挂起态仍在。

    关键设计：
    1. 用**独立连接池**（autocommit=True）—— LangGraph 自己按语句控制事务，
       共用业务池会让 autocommit 设置互相污染。
    2. 表由 `setup()` 自动创建（checkpoints / checkpoint_blobs / checkpoint_writes），
       和业务表同库不同表，备份时一起走 pg_dump。
    3. 这里存的是「执行位置」，不是业务事实。业务事实（消息、会话状态）始终在
       业务表里，所以快照即使丢失也能从历史重建，不会出现「重启后历史没了」。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    saver = PostgresSaver(pg.checkpoint_pool())
    saver.setup()
    return saver


class Runner:
    """包装图调用，负责 checkpoint、中断查询与自愈。

    自愈规则（对应双语言架构的状态撕裂风险）：
    1. 图还挂起，但业务侧已不是「等待人工」→ 该挂起线程已过期，丢弃后重跑
    2. 图无挂起，但业务侧说在等待人工 → 记录为不一致，供对账接口暴露
    3. 调用异常/超时 → 返回 error，由上层决定降级（不抛给业务）
    """

    TIMEOUT_SEC = 45

    def __init__(self) -> None:
        self.graph = build_graph()

    @staticmethod
    def _config(session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 25}

    def run(self, session_id: str, question: str) -> dict:
        cfg = self._config(session_id)
        session = db.get_session(session_id) or {}

        # 自愈 1：残留挂起态与业务状态不符，先丢弃再跑，避免图卡在旧线程上
        try:
            if self.is_waiting(session_id) and session.get("status") not in ("WAITING_AGENT",):
                self.reset(session_id)
        except Exception:
            pass

        history = db.list_messages(session_id, 40)[-8:]
        # 每轮必须显式重置「本轮结果」字段。
        # LangGraph 的 checkpoint 按 thread_id 累积 state：上一轮的 tool_name / docs /
        # tool_result 不清掉就会串到这一轮（表现为：问知识库问题却标记成上一轮的工具）。
        state: ChatState = {
            "session_id": session_id,
            "question": question,
            "history": history,
            "tenant_id": session.get("tenant_id") or db.DEFAULT_TENANT,
            "channel": session.get("channel") or "web",
            "intent": "",
            "tool_name": "",
            "docs": [],
            "tool_result": None,
            "answer": "",
            "need_human": False,
            "human_reason": "",
            "tokens": 0,
            "latency_ms": 0,
            "pending_action": None,
            "action_decision": "none",
        }

        started = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.graph.invoke, state, cfg)
                out = future.result(timeout=self.TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            return {"answer": "", "need_human": True, "interrupted": False,
                    "error": "编排超时", "intent": "", "tokens": 0,
                    "latency_ms": int((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            return {"answer": "", "need_human": True, "interrupted": False,
                    "error": str(exc)[:200], "intent": "", "tokens": 0,
                    "latency_ms": int((time.perf_counter() - started) * 1000)}

        snapshot = self.graph.get_state(cfg)
        pending = bool(snapshot.next)
        return {
            "answer": out.get("answer", ""),
            "intent": out.get("intent", ""),
            "tool_name": out.get("tool_name", ""),
            "need_human": bool(out.get("need_human")) or pending,
            "interrupted": pending,
            "human_reason": out.get("human_reason", ""),
            "tokens": int(out.get("tokens") or 0),
            "latency_ms": int(out.get("latency_ms") or 0),
            "docs": out.get("docs") or [],
        }

    def is_waiting(self, session_id: str) -> bool:
        try:
            return bool(self.graph.get_state(self._config(session_id)).next)
        except Exception:
            return False

    def resume(self, session_id: str, agent_reply: str) -> dict:
        cfg = self._config(session_id)
        out = self.graph.invoke(Command(resume=agent_reply), cfg)
        return {"answer": out.get("answer", ""), "interrupted": bool(self.graph.get_state(cfg).next)}

    def reset(self, session_id: str) -> bool:
        """丢弃某会话的挂起线程（关单、或状态已不一致时调用）。"""
        try:
            self.graph.checkpointer.delete_thread(session_id)
            return True
        except Exception:
            return False

    def reconcile(self) -> dict:
        """对账：业务侧说在等人工，但图里没有挂起线程的记录。

        这些会话坐席点「领取」后 resume 会失败，需要人工关注或重置。
        """
        sessions = db.list_sessions(status="WAITING_AGENT", limit=200)
        orphan = [s["id"] for s in sessions if not self.is_waiting(s["id"])]
        return {"waiting": len(sessions), "orphan": orphan, "ok": not orphan}

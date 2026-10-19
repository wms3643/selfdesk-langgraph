"""OpenAI 兼容层：让 Java 业务侧（Spring AI）把 AI 请求接进来。

Java 侧只需要把 deepseek 的 base-url 指到本服务：
    SPRING_AI_DEEPSEEK_BASE_URL = http://127.0.0.1:8003
业务代码、会话状态机、坐席、工单全部留在 Java，一行不用改。

两种工作模式：
1. 透传模式（默认）：把 messages 原样交给上游模型，行为与直连 DeepSeek 一致。
   这是安全兜底——切换过来不该让现有功能变差。
2. 编排模式：请求头带 X-SelfDesk-Session 时，交给 LangGraph 状态图处理，
   由状态图决定检索、工具调用与是否转人工。

为什么不用「改 Java 代码调 /api/chat」：
- 改 jar 有版本负担，每次上游更新都要重打补丁
- OpenAI 兼容协议是稳定契约，Spring AI 换版本也不影响
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from langchain_openai import ChatOpenAI

from .config import LLM_BASE_URL, LLM_MODEL, LLM_TEMPERATURE, load_api_key

router = APIRouter()

# DeepSeek 官方路径是 /chat/completions；OpenAI SDK 习惯 /v1/chat/completions
PATHS = ("/v1/chat/completions", "/chat/completions")

_runner = None


def _get_runner():
    """惰性单例：图与 checkpointer 只建一次，避免每请求重建 graph（连接池本身复用）。"""
    global _runner
    if _runner is None:
        from .graph import Runner
        _runner = Runner()
    return _runner


def _llm(model: str | None = None, temperature: float | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        model=model or LLM_MODEL,
        api_key=load_api_key(),
        base_url=LLM_BASE_URL,
        temperature=LLM_TEMPERATURE if temperature is None else temperature,
        timeout=60,
        max_retries=1,
    )


def _to_lc_messages(messages: list[dict]) -> list[tuple[str, str]]:
    """OpenAI messages → LangChain 的 (role, content) 元组列表。"""
    out: list[tuple[str, str]] = []
    for m in messages or []:
        role = m.get("role") or "user"
        if role not in ("system", "user", "assistant"):
            role = "user"
        content = m.get("content")
        if isinstance(content, list):
            # 多段内容（text 数组）只取文本部分
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            content = "".join(parts)
        out.append((role, str(content or "")))
    return out or [("user", "")]


def _usage(resp: Any) -> dict:
    u = getattr(resp, "usage_metadata", None) or {}
    prompt = int(u.get("input_tokens") or 0)
    completion = int(u.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(u.get("total_tokens") or (prompt + completion)),
    }


def _chunk(cid: str, model: str, delta: dict, finish: str | None = None) -> str:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _handle(request: Request, session_id: str | None):
    body = await request.json()
    model = body.get("model") or LLM_MODEL
    messages = _to_lc_messages(body.get("messages") or [])
    stream = bool(body.get("stream"))
    cid = "chatcmpl-" + uuid.uuid4().hex[:20]

    # 编排模式：带会话标识时走状态图（检索 / 工具 / 转人工判断）
    # 熔断原则：编排拿不到结果就静默回落透传，绝不让 AI 层的失败传导成业务故障
    if session_id:
        question = next((c for r, c in reversed(messages) if r == "user"), "")
        text, tokens = "", 0
        try:
            result = _get_runner().run(session_id, question)
            text = (result.get("answer") or "").strip()
            tokens = int(result.get("tokens") or 0)
        except Exception:
            text = ""

        if text:
            if stream:
                async def orchestrated():
                    yield _chunk(cid, model, {"role": "assistant", "content": text}, None)
                    yield _chunk(cid, model, {}, "stop")
                    yield "data: [DONE]\n\n"
                return StreamingResponse(orchestrated(), media_type="text/event-stream")
            return {
                "id": cid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": tokens},
            }

    # 透传模式（编排未接管或失败时走这里）
    llm = _llm(model, body.get("temperature"))

    if stream:
        async def gen():
            yield _chunk(cid, model, {"role": "assistant", "content": ""}, None)
            try:
                for piece in llm.stream(messages):
                    text = getattr(piece, "content", "") or ""
                    if text:
                        yield _chunk(cid, model, {"content": text}, None)
            except Exception as exc:  # 上游异常要让客户端看到，而不是静默断流
                err = {"error": {"message": str(exc)[:300], "type": "upstream_error"}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield _chunk(cid, model, {}, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    resp = llm.invoke(messages)
    text = (getattr(resp, "content", "") or "").strip()
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": _usage(resp),
    }


async def _handle_route(request: Request,
                        x_selfdesk_session: str | None = Header(default=None)):
    return await _handle(request, x_selfdesk_session)


for _path in PATHS:
    router.add_api_route(_path, _handle_route, methods=["POST"], include_in_schema=False)

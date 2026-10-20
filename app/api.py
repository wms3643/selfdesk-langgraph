"""HTTP 接口层：访客端、坐席端、知识库、仪表盘。"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import db, pg, pg_store, rag, tools
from .auth import issue_token, parse_token, require_agent
from .config import STATIC_DIR
from .graph import Runner
from .openai_compat import router as compat_router

app = FastAPI(title="SelfDesk Agent")
app.include_router(compat_router)
runner = Runner()


# 演示坐席：本机免密登录与种子数据共用同一份，避免两处各写一遍
DEMO_USERNAME = os.environ.get("SELFDESK_AGENT_USER", "agent@selfdesk.local")
DEMO_PASSWORD = os.environ.get("SELFDESK_AGENT_PASSWORD", "SelfDesk#2026")

# 启动即建池、建表、灌种子。数据库不可用时这里直接抛错，服务不会带着坏的依赖上线。
db.init_db(default_agent=(DEMO_USERNAME, DEMO_PASSWORD))
tools.seed_demo_orders()


@app.exception_handler(pg.PgUnavailable)
async def _pg_unavailable(request: Request, exc: pg.PgUnavailable):
    """数据库故障返回 503，而不是 500 —— 让调用方知道「可以重试」。"""
    return JSONResponse(status_code=503, content={"detail": str(exc), "retryable": True})


# ── 模型 ────────────────────────────────────────────────────
class LoginBody(BaseModel):
    username: str
    password: str


class SessionBody(BaseModel):
    visitor_name: str = "访客"


class ChatBody(BaseModel):
    session_id: str
    message: str


class ReplyBody(BaseModel):
    session_id: str
    content: str


class ClaimBody(BaseModel):
    session_id: str


class DocBody(BaseModel):
    title: str
    content: str
    source: str = "manual"


# ── 页面 ────────────────────────────────────────────────────
@app.get("/")
def chat_page():
    return FileResponse(STATIC_DIR / "chat.html", media_type="text/html; charset=utf-8")


@app.get("/console")
def console_page():
    return FileResponse(STATIC_DIR / "console.html", media_type="text/html; charset=utf-8")


# ── 访客端 ──────────────────────────────────────────────────
@app.post("/api/sessions")
def create_session(body: SessionBody):
    return db.create_session(body.visitor_name or "访客")


@app.post("/api/chat")
def chat(body: ChatBody):
    session = db.get_session(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息为空")

    db.add_message(body.session_id, "visitor", message)

    # 已转人工/坐席接待中：只入库，不调模型
    if session["status"] in ("WAITING_AGENT", "AGENT"):
        return {"mode": "human", "waiting": session["status"] == "WAITING_AGENT",
                "answer": "", "need_human": True}

    result = runner.run(body.session_id, message)

    if result.get("answer"):
        db.add_message(body.session_id, "assistant", result["answer"],
                       source=result.get("intent") or "bot",
                       tokens=result.get("tokens", 0),
                       latency_ms=result.get("latency_ms", 0))

    if result.get("need_human"):
        # 首次转入人工时自动建单；已在等待中的会话不重复建
        if session["status"] not in ("WAITING_AGENT",):
            try:
                tools.create_ticket(body.session_id, message[:60], message, priority="HIGH")
            except Exception:
                pass
        db.update_session(body.session_id, status="WAITING_AGENT",
                          intent=result.get("intent"), summary=result.get("human_reason"))
    else:
        db.update_session(body.session_id, status="BOT", intent=result.get("intent"))

    return {
        "mode": "bot",
        "answer": result.get("answer", ""),
        "intent": result.get("intent", ""),
        "tool": result.get("tool_name", ""),
        "need_human": result.get("need_human", False),
        "human_reason": result.get("human_reason", ""),
        "docs": [{"title": d["title"], "score": d["score"]} for d in result.get("docs", [])],
        "tokens": result.get("tokens", 0),
        "latency_ms": result.get("latency_ms", 0),
        "error": result.get("error"),
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session": session, "messages": db.list_messages(session_id)}


# ── 坐席端 ──────────────────────────────────────────────────
@app.post("/api/auth/login")
def login(body: LoginBody):
    user = db.get_user(body.username)
    if not user or not db.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return {"token": issue_token(user["username"]),
            "display_name": user["display_name"] or user["username"],
            "role": user["role"]}


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    # IPv6 回环可能带 zone 后缀；反向代理场景不在此列（那属于远程访问）
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


@app.get("/api/auth/local")
def local_login(request: Request):
    """本机免密进入：只有回环地址可用，供本地演示。

    远程访问（局域网/公网）仍然要求账号密码，避免免密变成后门。
    """
    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="仅本机可免密进入，请使用账号密码登录")
    user = db.get_user(DEMO_USERNAME)
    if not user:
        raise HTTPException(status_code=404, detail="演示账号未初始化")
    return {"token": issue_token(user["username"]),
            "display_name": user["display_name"] or user["username"],
            "role": user["role"],
            "mode": "local"}


@app.get("/api/console/sessions")
def console_sessions(status: str | None = None, user: dict = Depends(require_agent)):
    return {"sessions": db.list_sessions(status=status)}


@app.post("/api/console/claim")
def claim(body: ClaimBody, user: dict = Depends(require_agent)):
    session = db.get_session(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    db.update_session(body.session_id, status="AGENT",
                      agent_name=user.get("display_name") or user["username"])
    return {"ok": True}


@app.post("/api/console/reply")
def reply(body: ReplyBody, user: dict = Depends(require_agent)):
    session = db.get_session(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="回复为空")

    db.add_message(body.session_id, "assistant", content, source="human")
    db.update_session(body.session_id, status="AGENT",
                      agent_name=user.get("display_name") or user["username"])

    # 图若仍挂起，唤醒它走到 END，避免 checkpoint 残留
    if runner.is_waiting(body.session_id):
        try:
            runner.resume(body.session_id, content)
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/console/close")
def close(body: ClaimBody, user: dict = Depends(require_agent)):
    db.update_session(body.session_id, status="CLOSED")
    # 关单即丢弃挂起线程，避免残留快照与新会话状态打架
    runner.reset(body.session_id)
    return {"ok": True}


@app.post("/api/console/repair")
def repair(user: dict = Depends(require_agent)):
    """修复「状态撕裂」：业务侧在等人工，但图里已没有挂起线程。

    成因：进程重启丢失快照（旧版用内存 checkpoint）、或会话被外部改写。
    处理：降级回机器人并留一条系统消息，让访客能继续说话，而不是永久卡在队列里。
    """
    rec = runner.reconcile()
    fixed = []
    for sid in rec["orphan"]:
        db.update_session(sid, status="BOT")
        db.add_message(sid, "assistant", "会话已重新接入智能助手，请再描述一次您的问题。",
                       source="system")
        fixed.append(sid)
    return {"fixed": fixed, "count": len(fixed), "waiting": rec["waiting"]}


@app.get("/api/admin/reconcile")
def reconcile(user: dict = Depends(require_agent)):
    """查看挂起态对账结果（只读，不改数据）。"""
    return runner.reconcile()


# ── 知识库 ──────────────────────────────────────────────────
@app.post("/api/kb/docs")
def add_doc(body: DocBody, user: dict = Depends(require_agent)):
    title = (body.title or "").strip()
    content = (body.content or "").strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="标题和内容不能为空")

    res = pg_store.ingest(title, content, rag.embed, body.source)
    if not res:
        raise HTTPException(status_code=503,
                            detail="知识库写入失败：PostgreSQL 或 Embedding 服务不可用")
    return {"id": res["doc_id"], "count": pg_store.count(),
            "chunks": res["chunks"], "backend": "pgvector"}


@app.get("/api/kb/docs")
def list_docs(user: dict = Depends(require_agent)):
    docs = pg_store.list_docs()
    return {"docs": docs, "count": len(docs), "backend": "pgvector",
            "chunks": pg_store.chunk_stats()}


class KbDocItem(BaseModel):
    title: str
    content: str
    source: str = "import"


class KbImportBody(BaseModel):
    docs: list[KbDocItem]


@app.post("/api/kb/import")
def import_docs(body: KbImportBody, user: dict = Depends(require_agent)):
    """批量导入：前端把 .md/.txt 解析成 [{title, content}] 后提交。

    逐篇切分+向量化，失败的条目单独返回，不因为一篇出错就整体回滚。
    """
    if not body.docs:
        raise HTTPException(status_code=400, detail="没有可导入的文档")
    if len(body.docs) > 50:
        raise HTTPException(status_code=400, detail="单次最多导入 50 篇")

    added, failed = 0, []
    for item in body.docs:
        title = (item.title or "").strip()
        content = (item.content or "").strip()
        if not title or not content:
            failed.append({"title": title or "(无标题)", "reason": "标题或内容为空"})
            continue
        res = pg_store.ingest(title, content, rag.embed, item.source or "import")
        if res:
            added += 1
        else:
            failed.append({"title": title, "reason": "写入失败"})
    return {"added": added, "failed": failed, "count": pg_store.count(),
            "chunks": pg_store.chunk_stats()}


@app.delete("/api/kb/docs/{doc_id}")
def delete_doc(doc_id: str, user: dict = Depends(require_agent)):
    if not pg_store.delete_doc(doc_id):
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"ok": True, "count": pg_store.count(), "chunks": pg_store.chunk_stats()}


# ── 工单 / 仪表盘 ───────────────────────────────────────────
@app.get("/api/tickets")
def tickets(user: dict = Depends(require_agent)):
    return {"tickets": db.list_tickets()}


@app.get("/api/dashboard")
def dashboard(user: dict = Depends(require_agent)):
    return {"stats": db.stats(), "sessions": db.list_sessions(limit=12)}


@app.get("/api/tools")
def list_tools():
    """能力清单：当前注册了哪些业务工具。"""
    return {"count": len(tools.TOOL_SPECS), "tools": tools.TOOL_SPECS}


@app.get("/api/health")
def health(deep: int = 0):
    """健康探针。deep=1 时额外做挂起态对账（较慢，仅供排障）。"""
    info = {
        "ok": pg.ping(),
        "db": db.backend(),
        "dbVersion": pg.server_version(),
        "kbDocs": pg_store.count(),
        "kbBackend": rag.backend(),
        "checkpoint": "postgres",
        "tenants": len(db.list_tenants()),
    }
    if deep:
        try:
            info["reconcile"] = runner.reconcile()
        except Exception as exc:
            info["reconcile"] = {"error": str(exc)[:160]}
    return info

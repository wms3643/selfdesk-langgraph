# SelfDesk · LangGraph Customer-Service Orchestrator

![ci](https://github.com/wms3643/selfdesk-langgraph/actions/workflows/ci.yml/badge.svg)

A standalone graph-orchestrated AI layer for customer service:
**intent routing → retrieval → tool calls → two-phase writes → human handoff that actually suspends.**

Not a prompt wrapper. Every step has explicit state, replayable checkpoints, and evidence you can query afterwards (intent, matched chunks, tool used, latency, tokens).

![visitor](docs/screenshots/visitor.png)

## Why it is interesting

| Typical approach | Here |
|---|---|
| Let the model decide everything | Rule-based routing first: knowledge → RAG, business → tools, explicit request → human |
| Model writes to your DB directly | Two-phase: proposal → user confirms → idempotent execution |
| "Handoff" is just a UI banner | LangGraph `interrupt`, state persisted in PostgreSQL, survives restarts |
| Vector DB + business DB + state store | All three in one PostgreSQL (pgvector + checkpoints) |
| Rewrites break integration | Exposes an OpenAI-compatible `/v1/chat/completions` |

## Architecture

```
 Visitor (:8003/)                    Agent console (:8003/console)
      │ POST /api/chat                        │
      ▼                                        ▼
 ┌──────────────────── FastAPI ─────────────────────┐
 │  LangGraph state machine                         │
 │  ├─ classify   knowledge / business / human      │
 │  ├─ retrieve   pgvector · bge-small-zh · 512d    │
 │  ├─ tool       9 tools, writes need confirmation │
 │  ├─ handoff    interrupt → resume on agent reply │
 │  └─ answer     model output + cited chunks       │
 └───────┬──────────────────────┬───────────────────┘
         │                      │
  PostgreSQL 16 + pgvector     Embedding (:8000)
  business / KB / checkpoints  fastembed
```

## Features

- Multi-turn sessions persisted in PostgreSQL; refresh-safe
- RAG answers cite matched chunks; low confidence → human
- Order / refund / ticket lookup; ordering and refund submission require confirmation
- Agent console: sessions, search, claim, reply, close; knowledge base with bulk `.md/.txt` import
- Per-reply observability: intent, chunks, tool, latency, tokens
- OpenAI-compatible endpoint with streaming

## Quick start

```bash
# Docker route (any OS)
docker compose up -d                 # PostgreSQL + pgvector
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # set DEEPSEEK_API_KEY
python -m uvicorn embedding_server:app --port 8000 &
python run.py                        # :8003
python seed_kb.py                    # demo knowledge base
```

Open http://127.0.0.1:8003/ and http://127.0.0.1:8003/console

> On Windows + WSL, `.\start.ps1` brings up PostgreSQL, embeddings and the app in one go.
> Console demo account: `agent@selfdesk.local` / `SelfDesk#2026` (local access issues a token automatically).

## Tests

```bash
pip install -r requirements-dev.txt
pytest            # 32 unit tests, ~0.4s, no external services
pytest -m db      # two-phase idempotency, requires SELFDESK_TEST_DSN
```

Covered: token signing/expiry/tampering, chunking boundaries (500 chars, 80-char overlap, tail merge),
intent routing, order-number extraction, and "confirming the same proposal twice creates one order".

## Docs in Chinese

See [README.md](README.md) for the full write-up: design decisions, API table, tool list, known limitations.

## Limitations

- Single agent, single tenant (multi-tenant column reserved, no isolation strategy yet)
- Rule-based intent classification — explainable but not general
- Only `.md` / `.txt` import; docx/pdf parsing not implemented
- Local embeddings download a ~100 MB model on first run

## License

MIT

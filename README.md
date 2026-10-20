# SelfDesk · LangGraph 智能客服编排层

![ci](https://github.com/wms3643/selfdesk-langgraph/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-0E7A68)
![python](https://img.shields.io/badge/python-3.11%2B-0E7A68)

[English](README.en.md) · 简体中文

把客服 AI 做成一个**能被现有业务系统调用的编排服务**——不是替换你的客服入口，而是挂在它后面：
意图分流 → 检索增强 → 工具调用 → 两阶段写操作 → 转人工挂起/恢复。


> 想先跑起来 → [快速开始](#快速开始)；想看设计取舍 → [核心设计](#核心设计)。

不是「套一层 prompt 的聊天框」：每一步都有明确状态、可回放的 checkpoint，以及可查的证据（意图、命中片段、工具调用、延迟、token）。

*调试台：用来验证链路，不是生产入口*

![调试台](docs/screenshots/visitor.png)

---

## 目录

- [两个版本](#两个版本本仓库是版本-b-的-ai-层)
- [先明确：它是接进去，不是换掉](#先明确它是接进去不是换掉)
- [为什么单独部署](#为什么单独部署而不是写进业务代码)
- [为什么值得看](#为什么值得看)
- [架构](#架构)
- [核心设计](#核心设计)
- [功能](#功能)
- [快速开始](#快速开始)
- [配置](#配置)
- [HTTP 接口](#http-接口)
- [业务工具](#业务工具)
- [目录结构](#目录结构)
- [验证状态](#验证状态)
- [测试](#测试)
- [已知限制](#已知限制)

---

## 为什么值得看

| 常见做法 | 这里的做法 |
|---|---|
| 所有问题丢给模型自由发挥 | 先做意图分流：知识问答走 RAG、业务查询走工具、明确要人工才转接 |
| 让模型直接「帮用户下单/退款」 | **两阶段确认**：先返回提案，用户确认后才执行，带幂等键 |
| 转人工 = 前端弹个提示 | `interrupt` 真挂起，状态写进 checkpoint，进程重启也不丢 |
| 向量库、业务库、会话状态三套存储 | 全部放 PostgreSQL：业务表 + pgvector + checkpoint 同库 |
| 换语言重写就接不进业务 | 提供 OpenAI 兼容 `/v1/chat/completions`，Java Spring AI 改个 base-url 即可接入 |

## 两个版本（本仓库是版本 B 的 AI 层）

同一套客服产品，两种 AI 实现。**入口、页面、业务逻辑都一样，换的是 AI 引擎。**

| | 版本 A · 全 Java | 版本 B · 双语言 |
|---|---|---|
| 业务系统 | bytedesk（Java / Spring Boot） | 同一套，不改 |
| AI 实现 | Spring AI + DeepSeek | Python + LangGraph 编排 |
| 进程数 | 1 个（:9003） | 2 个（:9003 业务 + :8003 AI） |
| 会话与状态 | 全在 H2 | 业务在 H2，AI 状态在 PostgreSQL |
| 访客入口 | `:9003/chat/` | **同一个** `:9003/chat/`，AI 请求转给 8003 |
| 代码仓库 | [selfdesk-springai](https://github.com/wms3643/selfdesk-springai) | 本仓库（AI 层）+ selfdesk-springai（业务） |

版本之间切换只改一个环境变量（`SPRING_AI_DEEPSEEK_BASE_URL`），业务代码零改动：

```
.\scripts\ai-gateway.ps1 on -Restart     # A → B
.\scripts\ai-gateway.ps1 off -Restart    # B → A
```

## 先明确：它是接进去，不是换掉

生产环境里用户看到的**永远是业务系统自己的入口**（网站、公众号、App 里的客服窗口）。
这个服务在它后面，通过 HTTP 被调用：

```
用户 ──► 你的客服窗口（原有前端，一行不改）
              │
              ▼
        你的业务后端 ──POST /v1/chat/completions──► 本服务
              │                                      │
              │◄──────── 回答 / 转人工标记 ───────────┘
              ▼
        原有会话库、坐席分配、工单系统
```

两种接入方式：

| 方式 | 适合 | 改动量 |
|---|---|---|
| OpenAI 兼容端点 | 已有 Spring AI / LangChain / 任意 SDK 的系统 | 只改 base-url 与 API Key |
| 直接调 `POST /api/chat` | 需要会话、转人工、工具执行结果 | 一个 HTTP 调用，带 `session_id` |

```bash
curl http://127.0.0.1:8003/v1/chat/completions   -H "Authorization: Bearer any"   -H "X-SelfDesk-Session: your-session-id"   -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"退款多久到账"}]}'
```

> **仓库里的 8003 页面是什么？**
> 它是一个调试台——用来验证「问一句 → 检索 → 工具 → 转人工」这条链路是否通，顺便给演示用。
> 不要把它当成要让用户访问的生产入口。

## 为什么单独部署，而不是写进业务代码

| 维度 | 内嵌进业务服务 | 独立服务（本项目） |
|---|---|---|
| 语言生态 | Java 里调 Python 图编排要跨进程/嵌入解释器 | 各用各的生态，边界是 HTTP |
| 迭代速度 | 改一句提示词要发整个业务版本 | AI 层独立发版，分钟级 |
| 故障隔离 | AI 超时拖慢业务线程池 | 挂了就返回降级话术，业务照常 |
| 观测 | 埋点混在业务日志里 | 意图/工具/延迟/token 独立记录 |
| 复用 | 每个入口重写一遍 | 一套服务接网页、企微、App |

代价也说清楚：**多一个部署单元、多一次网络跳转、会话状态要约定清楚由谁持有**。
如果你的场景是单入口、强事务、AI 只是辅助字段生成，那内嵌更简单，不必拆。

## 架构

```
       访客端 (8003 /)                     AI 工作台 (8003 /console)
             │  POST /api/chat                      │
             ▼                                      ▼
        ┌──────────────────── FastAPI ────────────────────┐
        │                                                │
        │   LangGraph 状态图                              │
        │   ├─ classify   漏斗：规则短路 → 决策模型        │
        │   ├─ v_*        参数入口校验（单号/可退状态）    │
        │   ├─ retrieve   pgvector 检索（bge-small-zh）   │
        │   ├─ tool       8 个业务工具，写操作走两阶段     │
        │   ├─ handoff    interrupt 挂起，等坐席 resume   │
        │   └─ answer     模型生成 + 引用来源             │
        │                                                │
        └───────┬──────────────────┬─────────────────────┘
                │                  │
     PostgreSQL 16 + pgvector     本地 Embedding (:8000)
     业务表 / 知识库 / checkpoint   fastembed · 512 维
```

## 核心设计

### 1. 分诊是漏斗：规则短路 + 决策模型一次给出工具与参数

`classify` 节点分两级，目的只有一个——**别让"选工具"和"抽参数"变成两拨人干**：

```
第 1 级（零成本）  明确要人工 / 空消息  ──────────────► 直接定 intent
第 2 级（一次调用） 其余全部交给决策模型 ──► tool_call(name + args) ──► 进 v_* 校验
```

决策模型用 function calling，**一次返回工具名和参数**：

```python
bound = llm.bind_tools(tool_schemas(), tool_choice="auto")   # temperature=0
resp  = bound.invoke([SystemMessage(DECISION_PROMPT), HumanMessage(q)])
call  = resp.tool_calls[0]        # {"name": "query_order", "args": {"order_no": "..."}}
```

三条边界防止"上了模型就放飞"：

| 风险 | 处置 |
|---|---|
| 模型编造订单号 | 参数进 `v_*` 做可溯源校验，不在原话里就 `not_grounded` |
| 模型调不存在的工具 | 工具名不在 `TOOL_REGISTRY` → 直接降级为知识问答 |
| 模型超时/报错 | 退回关键词规则（`decision_source=fallback`），服务不中断 |

**实测（同一批句子，脚本 `scripts/eval_decision.py`）**

| 指标 | 关键词规则 | 决策模型 |
|---|---|---|
| 24 句常见问法 | 22/24 (92%) | **24/24 (100%)** |
| 8 句留出集（未进提示词） | —— | 7/8 (88%) |
| 单次决策延迟 | ~0 ms | 中位 842 ms，最慢 1587 ms |

规则那 92% 是"我按这些句子调的"，模型 100% 也含少量拟合成分；**留出集 88% 才是可信数字**。
规则漏掉的典型是「帮我查下 202610190002 付了多少钱」→ 规则返回知识库，模型正确选 `query_order`。
代价是每轮多一次模型调用，中位 +0.8 秒。要退回纯规则：`SELFDESK_DECISION=rules`。

### 2. 参数在流程入口校验，不带病执行

需要参数的流程（查单、退款）在进业务前先过自己的前缀节点 `v_order` / `v_refund`：

```
classify ──biz──► v_order ──ok──► call_tool ──► answer
                     └──fail──────────────────► answer（引导用户补参数）
```

**节点分开，规则共享。** 每个 `v_*` 只写自己特有的判断，三条通用规则
（参数齐全、可在原话/上下文中溯源、错误结构统一）都调 `app/validate.py`，避免同一条安全规则在 N 处各写一遍。

**为什么参数要溯源。** 现在参数由正则从用户原话里抄，天然可信；一旦换成模型抽取（function calling），
模型可能编出一个不存在的单号。校验层把"参数必须能在原文里找到"变成硬约束，两种抽取方式共用。

错误码约定（`decide` 依赖它区分追问还是转人工）：

| 错误码 | 含义 | 后续动作 |
|---|---|---|
| `missing_arg` | 缺参数 | 追问，不转人工 |
| `not_grounded` | 参数不在原话里 | 复述确认 |
| `not_found` | 查了但没有 | 转人工 |
| `forbidden` | 越权访问 | 转人工 + 审计 |

开关：`SELFDESK_ARG_VALIDATE=0` 退回旧路径（classify 直接进 `call_tool`）。

### 3. 两阶段写操作

模型不能直接动数据。下单/退款/建工单都是：

```
用户：帮我下单标准版
AI：  待确认 — 标准版月付 ¥129，确认后创建（提案已落库）
用户：确认
AI：  已创建订单 2026xxxx（幂等键 pi_xxx，重复确认不会重复下单）
```

`execute_pending` 读取提案并落库，幂等键保证同一提案只执行一次。

### 4. 转人工是真挂起

`handoff` 节点调用 LangGraph 的 `interrupt`，图停在那一刻，状态进入 PostgreSQL checkpoint。
坐席在 8003 工作台回复时调 `resume`，图从断点继续，把坐席消息带给访客。
服务重启、进程崩溃都不影响——状态不在内存里。

### 5. 三份数据一个库

业务表（sessions / messages / tickets / orders）、向量块（kb_chunks）、图状态（checkpoints）都在同一个 PostgreSQL。
少一套依赖，事务边界清楚，备份只备一个库。

### 6. OpenAI 兼容层

`/v1/chat/completions` 支持流式，语义与 OpenAI 一致，额外带 `X-SelfDesk-Session` 头复用会话。
Java 侧只要把 Spring AI 的 `base-url` 指过来，业务代码一行不改就能用上这套编排。

## 功能

**访客侧**

- 多轮对话，刷新不丢历史（会话 ID 存 localStorage）
- 知识问答带引用，不确定就转人工，不硬编
- 订单/退款/工单查询，写操作二次确认

**坐席侧（8003/console，本机免密）**

- 会话：列表、搜索、状态筛选、接管、回复、结束
- 知识库：新增、批量导入 `.md/.txt`（Markdown 按一级标题拆多条）、删除、切分统计
- 概览：会话/消息/Token/工单，状态分布

**运营指标**

- 每条回复记录意图、命中片段、工具调用、延迟、token
- `/api/dashboard` 汇总；`/api/health` 暴露 DB、pgvector、checkpoint 状态

## 快速开始

前置：

- Python 3.11+（实测 3.13）
- PostgreSQL 14+ 且安装 `pgvector` 扩展
- 一个 DeepSeek API Key（或任何 OpenAI 兼容端点）

### 方式 A：Windows + WSL（本机开发用）

```powershell
cd selfdesk-langgraph
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # 填 DEEPSEEK_API_KEY
.\start.ps1
```

脚本会依次拉起 PostgreSQL（WSL）、Embedding（:8000）、主服务（:8003），并打开访客端与工作台。

首次使用灌一份演示知识库：

```powershell
.\.venv\Scripts\python seed_kb.py
```

### 方式 B：任意平台（Docker）

```bash
docker compose up -d                # PostgreSQL + pgvector
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                # 填 DEEPSEEK_API_KEY

python -m uvicorn embedding_server:app --host 127.0.0.1 --port 8000 &
python run.py                       # 主服务 :8003
```

打开 http://127.0.0.1:8003/ 和 http://127.0.0.1:8003/console

> 工作台默认账号 `agent@selfdesk.local` / `SelfDesk#2026`。本机访问时 `/api/auth/local` 会自动签发令牌，不用输密码；远程访问需登录。

![AI 工作台](docs/screenshots/workbench.png)

![知识库](docs/screenshots/knowledge-base.png)

![概览](docs/screenshots/overview.png)

## 配置

全部通过环境变量，代码里没有硬编码密钥。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填；也可放项目根 `.deepseek-key` |
| `SELFDESK_KEY_FILE` | — | 额外的密钥文件路径 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 任何 OpenAI 兼容端点 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_TEMPERATURE` | `0.3` | 生成温度 |
| `SELFDESK_DECISION` | `model` | `model` 用决策模型分诊；`rules` 退回关键词 |
| `SELFDESK_ARG_VALIDATE` | `1` | `0` 关闭参数入口校验 |
| `SELFDESK_PG_DSN` | `host=127.0.0.1 port=5432 dbname=selfdesk user=selfdesk password=selfdesk_local` | 整体覆盖连接串 |
| `EMBEDDING_URL` | `http://127.0.0.1:8000/v1/embeddings` | 向量服务 |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 512 维，中文短文本够用且快 |
| `EMBEDDING_DIM` | `512` | 必须与模型一致 |

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions` | 新建会话 |
| POST | `/api/chat` | 发消息，返回回答/转人工状态 |
| GET | `/api/sessions/{id}` | 会话详情与消息 |
| POST | `/api/auth/login` | 坐席登录 |
| GET | `/api/auth/local` | 本机免密（仅回环地址） |
| GET | `/api/console/sessions` | 坐席侧会话列表 |
| POST | `/api/console/claim` | 接管会话 |
| POST | `/api/console/reply` | 坐席回复（唤醒挂起的图） |
| POST | `/api/console/close` | 结束会话 |
| POST/GET | `/api/kb/docs` | 新增 / 列出知识条目 |
| POST | `/api/kb/import` | 批量导入（≤50 篇，逐篇返回失败原因） |
| DELETE | `/api/kb/docs/{id}` | 删除条目（级联删向量块） |
| GET | `/api/dashboard` | 运营概览 |
| POST | `/v1/chat/completions` | OpenAI 兼容（支持流式） |

## 业务工具

| 工具 | 作用 | 写操作 |
|---|---|---|
| `query_order` | 按订单号查详情与物流 | 否 |
| `query_refund` | 查退款进度 | 否 |
| `business_info` | 营业时间/电话/地址 | 否 |
| `query_ticket` | 查本会话工单 | 否 |
| `account_info` | 账号与套餐 | 否 |
| `create_ticket` | 建工单（当前仅由退款流程内部触发） | 是 |
| `submit_refund` | 提交退款申请 | 是（两阶段） |
| `create_order` | 创建订单 | 是（两阶段） |

> 共 8 个注册工具，其中 7 个能由 `pick_tool` 的规则直接选中；`create_ticket` 已注册但规则尚未接入，
> 目前只在 `submit_refund` 内部创建工单时调用。
> `match_product` 是 `create_order` 内部使用的产品匹配函数，不单独暴露为工具。

## 目录结构

```
app/
├─ api.py            FastAPI 路由、认证、知识库、工作台
├─ graph.py          LangGraph 状态图（classify/retrieve/tool/handoff/answer）
├─ tools.py          业务工具 + 两阶段执行
├─ rag.py            切分与检索
├─ pg_store.py       知识库读写（pgvector）
├─ pg.py             连接池与健康检查
├─ db.py             会话/消息/工单/订单
├─ openai_compat.py  OpenAI 兼容层（含流式）
├─ validate.py       参数前缀校验（齐全 / 溯源 / 统一错误码）
├─ router.py         决策模型（function calling：工具 + 参数一次给出）
├─ config.py         集中配置
├─ static/           访客端与工作台页面
└─ auth.py           令牌签发与校验（可脱离数据库单测）
tests/               单元测试（pytest）
seed_kb.py           演示知识库
reindex_kb.py        重建索引（换模型/调切分参数时用）
embedding_server.py  本地向量服务（fastembed）
scripts/eval_decision.py  决策模型 vs 关键词规则的对照实验
```

## 验证状态

不是「应该能跑」：用全新 venv 从零安装依赖，把完整链路跑了一遍。

| 验证项 | 结果 |
|---|---|
| `pip install -r requirements.txt`（全新 venv） | 通过 |
| `/api/health` | `db=postgres`、`kbBackend=pgvector`、`checkpoint=postgres` |
| 知识问答 | 命中 2 个片段（0.69 / 0.64），2.5 秒返回 |
| 工具调用 | `query_order` 正确返回订单、金额、物流 |
| 转人工 | 落为 `WAITING_AGENT`，工作台列表可见 |
| 坐席接管 + 回复 | 访客端收到人工消息，状态变 `AGENT`（checkpoint 恢复成功） |
| 两阶段写操作 | 提案 → 「确认」→ 创建订单并返回单号 |
| 自带向量服务 | `/health` 与 `/v1/embeddings` 正常返回 512 维向量 |
| 静态页面 | `/` 与 `/console` 均 200 |
| 参数校验（实机） | 缺单号追问、带单号查询、退款提案、确认执行、重复退款短路，5 条全通 |
| 决策模型（实机） | 24 句常见问法 24/24；留出集 8 句 7/8；中位 842ms |
| 单元测试 | 69 passed + 3 db skipped（`pytest`） |
| CI | GitHub Actions：测试 + 密钥扫描 + 敏感文件检查 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest                     # 72 个用例，约 0.7 秒，不依赖外部服务
pytest -m db               # 两阶段幂等（需要 SELFDESK_TEST_DSN 指向一个测试库）
```

| 文件 | 覆盖什么 |
|---|---|
| `tests/test_auth.py` | 令牌签发、签名被改、过期、换密钥后失效 |
| `tests/test_chunking.py` | 短文本整条、500 字上限、超长段 80 字重叠、尾块合并 |
| `tests/test_routing.py` | 意图路由（查单/退款/下单/政策问题不误触）、单号提取边界 |
| `tests/test_two_phase_db.py` | 同一提案确认两次只落一单；待确认动作流转与过期 |
| `tests/test_validate.py` | 参数可溯源（含分隔符/短值）、模型参数被篡改时拦下、两个前缀节点的分支与短路 |
| `tests/test_router.py` | 漏斗短路（不花模型调用）、tool_call 解析、未知工具降级、模型报错退回规则、编造参数被 `v_order` 拦下 |

CI 里还会额外跑：脚本语法、密钥扫描、敏感文件、README 图片引用。

## 已知限制

- **单 Agent、单租户**：演示够用，多租户字段已预留（`tenant_id`），但没有做隔离策略。
- **决策要花一次模型调用**：中位 +0.8 秒，最慢 1.6 秒；对延迟敏感的入口可切 `SELFDESK_DECISION=rules`。
- **提示词里有 few-shot 示例**：那几句的准确率会被抬高（24 句 100%），真实水平看留出集（88%）。
- **写操作的单号会回看最近 4 轮对话**：用户接着说「退了吧」会沿用上一轮提到的单号，确认提示里会显示具体订单号，由确认这一步兜底；不想沿用可关掉 `v_refund` 的上下文回退。
- **只支持 `.md/.txt` 导入**：docx/pdf 需要额外解析，尚未实现。
- **Embedding 首次启动会下载模型**（约 100MB），之后走本地缓存。
- **工作台免密只对回环地址**：`/api/auth/local` 在远程访问时返回 403。

## License

MIT

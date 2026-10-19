"""集中配置：模型、嵌入服务、数据库连接。密钥一律从文件/环境变量读取，不写死。"""
from __future__ import annotations

import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"

# ── PostgreSQL（唯一数据源：业务表 + 知识库 + 工作流 checkpoint）──
# 优先级：SELFDESK_PG_DSN 整体覆盖 > 标准 PG* 环境变量 > 本机默认值。
PG_DSN = os.environ.get("SELFDESK_PG_DSN") or " ".join(
    [
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"dbname={os.environ.get('PGDATABASE', 'selfdesk')}",
        f"user={os.environ.get('PGUSER', 'selfdesk')}",
        f"password={os.environ.get('PGPASSWORD', 'selfdesk_local')}",
        "connect_timeout=5",
    ]
)
PG_POOL_MIN = int(os.environ.get("SELFDESK_PG_POOL_MIN", "1"))
PG_POOL_MAX = int(os.environ.get("SELFDESK_PG_POOL_MAX", "8"))
# 启动时等待数据库就绪的秒数：WSL 冷启动 + PG 恢复通常 10 秒内
PG_STARTUP_TIMEOUT = float(os.environ.get("SELFDESK_PG_STARTUP_TIMEOUT", "20"))

# 单租户演示；多租户时由调用方传入，所有查询都带 tenant_id 过滤
DEFAULT_TENANT = "t_default"

# 复用本地已有的 embedding 服务（bge-small-zh-v1.5，512 维），避免重复下载模型
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://127.0.0.1:8000/v1/embeddings")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "512"))

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.3"))

# 密钥查找顺序：环境变量 DEEPSEEK_API_KEY → 项目根 .deepseek-key → SELFDESK_KEY_FILE 指定的文件
KEY_CANDIDATES = [BASE_DIR / ".deepseek-key"]
_extra_key = os.environ.get("SELFDESK_KEY_FILE")
if _extra_key:
    KEY_CANDIDATES.append(pathlib.Path(_extra_key))


def load_api_key() -> str:
    env = os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env.strip()
    for path in KEY_CANDIDATES:
        try:
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            continue
    raise RuntimeError("未找到 DeepSeek API Key：请设置 DEEPSEEK_API_KEY 或在项目根放置 .deepseek-key")


# 检索阈值：低于该分数认为知识库没有可靠答案
RETRIEVE_TOP_K = int(os.environ.get("RETRIEVE_TOP_K", "4"))
RETRIEVE_MIN_SCORE = float(os.environ.get("RETRIEVE_MIN_SCORE", "0.35"))

# 知识库没有答案时是否直接转人工
ESCALATE_ON_LOW_SCORE = os.environ.get("ESCALATE_ON_LOW_SCORE", "1") == "1"

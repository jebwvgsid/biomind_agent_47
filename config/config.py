import os
from pathlib import Path

from dotenv import dotenv_values


def _load_local_env_defaults() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for key, value in dotenv_values(env_path).items():
        if not key or value is None or os.getenv(key):
            continue
        os.environ[key] = value


_load_local_env_defaults()


def _split_env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


# --- RAGFlow 服务配置 ---
RAGFLOW_ADDRESS = os.getenv("RAGFLOW_ADDRESS", "")
RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")

# 软件与环境路径。真实路径放在 .env 中，不要硬编码到仓库。
BIO_SOFTWARE_ROOT = os.getenv("BIO_SOFTWARE_ROOT", "")
BIO_SOURCE_INSTALL_DIR = os.getenv("BIO_SOURCE_INSTALL_DIR", "")
ANACONDA_ROOT = os.getenv("ANACONDA_ROOT", "")

# --- 知识库 ID 映射表 ---
KB_MAP = {
    "SOP": os.getenv("RAGFLOW_KB_SOP", ""),
    "TOOLS": os.getenv("RAGFLOW_KB_TOOLS", ""),
    "ERRORS": os.getenv("RAGFLOW_KB_ERRORS", ""),
}

RAGFLOW_DATASET_IDS = _split_env_list("RAGFLOW_DATASET_IDS") or [
    dataset_id for dataset_id in KB_MAP.values() if dataset_id
]

RERANK_MODEL_ID = os.getenv("RERANK_MODEL_ID", "gte-rerank@Tongyi-Qianwen")

DEFAULT_PROJECT_ID = os.getenv("BIOMIND_PROJECT_ID", "default")
BIOMIND_MEMORY_DIR = os.getenv("BIOMIND_MEMORY_DIR", "").strip()
BIOMIND_WIKI_ENABLED = os.getenv("BIOMIND_WIKI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
BIOMIND_WIKI_DIR = os.getenv("BIOMIND_WIKI_DIR", "").strip()
BIOMIND_WIKI_INGEST_MODE = os.getenv(
    "BIOMIND_WIKI_INGEST_MODE",
    "ask" if BIOMIND_WIKI_ENABLED else "disabled",
).strip().lower()
if not BIOMIND_WIKI_ENABLED:
    BIOMIND_WIKI_INGEST_MODE = "disabled"
elif BIOMIND_WIKI_INGEST_MODE not in {"manual", "ask", "auto"}:
    BIOMIND_WIKI_INGEST_MODE = "ask"


def _env_bool(name: str, default: bool = True) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


# Recovery is enabled by default. These switches are intentionally process
# local: task-specific runtime paths and credentials stay out of .env.
ENABLE_STRUCTURED_RECOVERY = _env_bool("ENABLE_STRUCTURED_RECOVERY", True)
ENABLE_RECOVERY_PATCHING = _env_bool("ENABLE_RECOVERY_PATCHING", True)
ENABLE_PROGRESS_DETECTOR = _env_bool("ENABLE_PROGRESS_DETECTOR", True)
ENABLE_ARTIFACT_GATE = _env_bool("ENABLE_ARTIFACT_GATE", True)
ENABLE_ROOT_CAUSE_ANALYZER = _env_bool("ENABLE_ROOT_CAUSE_ANALYZER", True)
ENABLE_COMMAND_TRACE = _env_bool("ENABLE_COMMAND_TRACE", True)
ENABLE_PIPESTATUS_CAPTURE = _env_bool("ENABLE_PIPESTATUS_CAPTURE", True)
ENABLE_KNOWLEDGE_CONFLICT_DETECTOR = _env_bool("ENABLE_KNOWLEDGE_CONFLICT_DETECTOR", True)
ENABLE_PLAN_ASSUMPTION_VALIDATION = _env_bool("ENABLE_PLAN_ASSUMPTION_VALIDATION", True)
ENABLE_PLAN_VERSIONING = _env_bool("ENABLE_PLAN_VERSIONING", True)
ENABLE_PLAN_IMPACT_ANALYSIS = _env_bool("ENABLE_PLAN_IMPACT_ANALYSIS", True)
ENABLE_PLAN_OSCILLATION_GUARD = _env_bool("ENABLE_PLAN_OSCILLATION_GUARD", True)

LLM_API_KEY = (
    os.getenv("DEEPSEEK_API_KEY")
    or os.getenv("QWEN_API_KEY")
    or os.getenv("DASHSCOPE_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or ""
)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_DEFAULT_MODEL = os.getenv("LLM_DEFAULT_MODEL", "")


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


ROOT_CAUSE_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_ROOT_CAUSE_TIMEOUT_SECONDS",
    3.0,
    minimum=0.1,
)


# Bound provider calls at the client layer. These are process settings rather
# than task-specific values, so an API failure cannot wait indefinitely or
# retry the same request forever.
LLM_TIMEOUT_SECONDS = _env_float("BIOMIND_LLM_TIMEOUT_SECONDS", 90.0, minimum=10.0)
LLM_READONLY_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_LLM_READONLY_TIMEOUT_SECONDS",
    45.0,
    minimum=10.0,
)
LLM_MAX_RETRIES = _env_int("BIOMIND_LLM_MAX_RETRIES", 1, minimum=0)
LLM_MAX_HISTORY_MESSAGES = _env_int("BIOMIND_LLM_MAX_HISTORY_MESSAGES", 12, minimum=2)
LLM_MAX_HISTORY_CHARS = _env_int("BIOMIND_LLM_MAX_HISTORY_CHARS", 24000, minimum=4000)
# Downstream managed nodes use a smaller step-local view than the full audit
# history.  Keep these configurable for providers with different context
# limits without changing the persisted task state.
LLM_STEP_CONTEXT_MESSAGES = _env_int("BIOMIND_LLM_STEP_CONTEXT_MESSAGES", 6, minimum=3)
LLM_STEP_CONTEXT_CHARS = _env_int("BIOMIND_LLM_STEP_CONTEXT_CHARS", 12000, minimum=4000)
LLM_STEP_PLAN_CHARS = _env_int("BIOMIND_LLM_STEP_PLAN_CHARS", 6000, minimum=2000)
LLM_STEP_SKILL_DOC_CHARS = _env_int("BIOMIND_LLM_STEP_SKILL_DOC_CHARS", 18000, minimum=4000)
LLM_STEP_EPHEMERAL_CHARS = _env_int("BIOMIND_LLM_STEP_EPHEMERAL_CHARS", 26000, minimum=8000)
# Planner prompt sizing uses a nominal engineering budget plus real token
# headroom thresholds.  The character budget is not a model context proxy.
PLANNER_NOMINAL_CHAR_BUDGET = _env_int("BIOMIND_PLANNER_NOMINAL_CHAR_BUDGET", 50000, minimum=8000)
PLANNER_SOFT_TOKEN_RATIO = _env_float("BIOMIND_PLANNER_SOFT_TOKEN_RATIO", 0.60, minimum=0.0)
PLANNER_COMPRESSION_TOKEN_RATIO = _env_float("BIOMIND_PLANNER_COMPRESSION_TOKEN_RATIO", 0.75, minimum=0.0)
PLANNER_HARD_TOKEN_RATIO = _env_float("BIOMIND_PLANNER_HARD_TOKEN_RATIO", 0.90, minimum=0.0)
PLANNER_RESERVED_HEADROOM_TOKENS = _env_int(
    "BIOMIND_PLANNER_RESERVED_HEADROOM_TOKENS",
    8192,
    minimum=0,
)

WIKI_EMBEDDING_ENABLED = BIOMIND_WIKI_ENABLED and os.getenv("WIKI_EMBEDDING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
WIKI_EMBEDDING_BASE_URL = os.getenv("WIKI_EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
WIKI_EMBEDDING_API_KEY = (
    os.getenv("WIKI_EMBEDDING_API_KEY")
    or os.getenv("DASHSCOPE_API_KEY")
    or os.getenv("QWEN_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or ""
)
WIKI_EMBEDDING_MODEL = os.getenv("WIKI_EMBEDDING_MODEL", "text-embedding-v4")
try:
    WIKI_EMBEDDING_DIMENSIONS = int(os.getenv("WIKI_EMBEDDING_DIMENSIONS", "1024"))
except ValueError:
    WIKI_EMBEDDING_DIMENSIONS = 1024

# skill 目录：优先使用当前项目自带 skills，避免加载到旧的硬编码目录。
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCAL_SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")
SKILLS_DIR = os.getenv(
    "BIOMIND_SKILLS_DIR",
    LOCAL_SKILLS_DIR if os.path.isdir(LOCAL_SKILLS_DIR) else "",
)

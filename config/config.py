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
BIOMIND_STEP_WORKER_BACKEND = os.getenv(
    "BIOMIND_STEP_WORKER_BACKEND", "legacy"
).strip().lower() or "legacy"
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
RECOVERY_THINKING_ENABLED = _env_bool("BIOMIND_RECOVERY_THINKING_ENABLED", True)
RECOVERY_REASONING_EFFORT = os.getenv(
    "BIOMIND_RECOVERY_REASONING_EFFORT", "high"
).strip().lower()
if RECOVERY_REASONING_EFFORT not in {"high", "max"}:
    RECOVERY_REASONING_EFFORT = "high"
# Consultant discovery owns an independent policy surface.  Recovery settings
# must not silently change whether skill-discovery escalation can think.
CONSULTANT_DISCOVERY_THINKING_ENABLED = _env_bool(
    "BIOMIND_CONSULTANT_DISCOVERY_THINKING_ENABLED", True
)
CONSULTANT_DISCOVERY_REASONING_EFFORT = os.getenv(
    "BIOMIND_CONSULTANT_DISCOVERY_REASONING_EFFORT", "high"
).strip().lower()
if CONSULTANT_DISCOVERY_REASONING_EFFORT not in {"high", "max"}:
    CONSULTANT_DISCOVERY_REASONING_EFFORT = "high"
GENERATE_THINKING_ENABLED = _env_bool("BIOMIND_GENERATE_THINKING_ENABLED", True)
GENERATE_REASONING_EFFORT = os.getenv(
    "BIOMIND_GENERATE_REASONING_EFFORT", "high"
).strip().lower()
if GENERATE_REASONING_EFFORT not in {"high", "max"}:
    GENERATE_REASONING_EFFORT = "high"
PLANNER_CORRECTION_THINKING_ENABLED = _env_bool(
    "BIOMIND_PLANNER_CORRECTION_THINKING_ENABLED", True
)
PLANNER_CORRECTION_REASONING_EFFORT = os.getenv(
    "BIOMIND_PLANNER_CORRECTION_REASONING_EFFORT", "high"
).strip().lower()
if PLANNER_CORRECTION_REASONING_EFFORT not in {"high", "max"}:
    PLANNER_CORRECTION_REASONING_EFFORT = "high"
try:
    DEEPSEEK_TEMPERATURE = float(os.getenv("BIOMIND_DEEPSEEK_TEMPERATURE", "0.6"))
except (TypeError, ValueError):
    DEEPSEEK_TEMPERATURE = 0.6
if not 0.0 <= DEEPSEEK_TEMPERATURE <= 2.0:
    DEEPSEEK_TEMPERATURE = 0.6
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
CONSULTANT_OPTIONAL_ARCHIVE_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_CONSULTANT_OPTIONAL_ARCHIVE_TIMEOUT_SECONDS",
    5.0,
    minimum=0.1,
)


# Bound provider calls at the client layer. These are process settings rather
# than task-specific values, so an API failure cannot wait indefinitely or
# retry the same request forever.
LLM_TIMEOUT_SECONDS = _env_float("BIOMIND_LLM_TIMEOUT_SECONDS", 600.0, minimum=10.0)
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
# Progressive Skill access uses explicit named limits so context acquisition
# cannot silently truncate canonical source text or loop indefinitely.
SKILL_FULL_READ_MAX_CHARS = _env_int(
    "BIOMIND_SKILL_FULL_READ_MAX_CHARS",
    60_000,
    minimum=1_000,
)
SKILL_EXCERPT_MAX_CHARS = _env_int(
    "BIOMIND_SKILL_EXCERPT_MAX_CHARS",
    24_000,
    minimum=500,
)
SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND = _env_int(
    "BIOMIND_SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND",
    6,
    minimum=1,
)
MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION = _env_int(
    "BIOMIND_MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION",
    8,
    minimum=1,
)
MAX_SKILL_SEARCH_ROUNDS_PER_STEP = _env_int(
    "BIOMIND_MAX_SKILL_SEARCH_ROUNDS_PER_STEP",
    5,
    minimum=1,
)
MAX_SKILL_BINDING_PROPOSALS_PER_STEP = _env_int(
    "BIOMIND_MAX_SKILL_BINDING_PROPOSALS_PER_STEP",
    3,
    minimum=1,
)
MAX_CONSULTANT_IDENTICAL_NO_PROGRESS_TRANSITIONS = _env_int(
    "BIOMIND_MAX_CONSULTANT_IDENTICAL_NO_PROGRESS_TRANSITIONS",
    2,
    minimum=2,
)
# Generate still applies the provider-level token/headroom gate before an LLM
# call.  This larger character budget prevents the older 26k engineering cap
# from rejecting valid current-step contracts far below a 200k-token window.
LLM_STEP_EPHEMERAL_CHARS = _env_int(
    "BIOMIND_LLM_STEP_EPHEMERAL_CHARS",
    200_000,
    minimum=8_000,
)
# One application maximum for generation and its context reservation. A legacy
# reserve environment variable must not under-reserve the actual wire limit.
from agent_core.llm_safety import MODEL_OUTPUT_LIMIT_TOKENS
SCHEMA2_CONTEXT_OUTPUT_RESERVE_TOKENS = MODEL_OUTPUT_LIMIT_TOKENS
RECOVERY_OUTPUT_LIMIT_TOKENS = MODEL_OUTPUT_LIMIT_TOKENS
RECOVERY_PROVIDER_OUTPUT_LIMIT_TOKENS = (
    _env_int("BIOMIND_RECOVERY_PROVIDER_OUTPUT_LIMIT_TOKENS", 50_000, minimum=1)
    if os.getenv("BIOMIND_RECOVERY_PROVIDER_OUTPUT_LIMIT_TOKENS", "").strip()
    else None
)
RECOVERY_MAX_OUTPUT_RESUBMISSIONS = _env_int(
    "BIOMIND_RECOVERY_MAX_OUTPUT_RESUBMISSIONS", 2, minimum=0,
)
# Consultant compiles natural-language intent and may need both a substantial
# conversation/audit history and complete documentation for several candidate
# skills.  Keep these limits in tokens (rather than reusing the older character
# caps) so they can be compared directly with the provider context window.
# Each pool may grow to 100k tokens, while the assembled request is still
# constrained by ``BioAnalystAgent.llm_context_limit_tokens`` (200k by default).
CONSULTANT_HISTORY_TOKEN_BUDGET = _env_int(
    "BIOMIND_CONSULTANT_HISTORY_TOKEN_BUDGET",
    100_000,
    minimum=4_000,
)
CONSULTANT_SKILL_TOKEN_BUDGET = _env_int(
    "BIOMIND_CONSULTANT_SKILL_TOKEN_BUDGET",
    100_000,
    minimum=4_000,
)
CONSULTANT_HISTORY_MAX_MESSAGES = _env_int(
    "BIOMIND_CONSULTANT_HISTORY_MAX_MESSAGES",
    128,
    minimum=8,
)
CONSULTANT_CANDIDATE_SKILL_LIMIT = _env_int(
    "BIOMIND_CONSULTANT_CANDIDATE_SKILL_LIMIT",
    64,
    minimum=12,
)
CONSULTANT_RESERVED_HEADROOM_TOKENS = _env_int(
    "BIOMIND_CONSULTANT_RESERVED_HEADROOM_TOKENS",
    MODEL_OUTPUT_LIMIT_TOKENS,
    minimum=0,
)
# Consultant FINALIZE has an independent output budget.  The history/skill
# pools above are input-only limits and must never be reused as an implicit
# completion cap.  ``MAX_OUTPUT`` is the requested semantic setting; the
# provider capability resolver and the per-request context calculation may
# lower it, but may not silently raise it.
CONSULTANT_FINALIZE_MAX_OUTPUT_TOKENS = MODEL_OUTPUT_LIMIT_TOKENS
CONSULTANT_FINALIZE_REQUESTED_OUTPUT_TOKENS = (
    CONSULTANT_FINALIZE_MAX_OUTPUT_TOKENS
)
CONSULTANT_FINALIZE_PROVIDER_OUTPUT_CAP_TOKENS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_PROVIDER_OUTPUT_CAP_TOKENS",
    MODEL_OUTPUT_LIMIT_TOKENS,
    minimum=1,
)
CONSULTANT_FINALIZE_CONTEXT_LIMIT_TOKENS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_CONTEXT_LIMIT_TOKENS",
    200_000,
    minimum=2,
)
CONSULTANT_FINALIZE_OUTPUT_HEADROOM_TOKENS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_OUTPUT_HEADROOM_TOKENS",
    2_048,
    minimum=0,
)

# Finite run-level limits are required in addition to a per-call token cap.
# These defaults are deliberately roomy for long bioinformatics contracts, but
# still make runaway repartition/retry loops impossible.  All values are
# persisted into the versioned work-budget artifact before provider I/O.
CONSULTANT_FINALIZE_BUDGET_POLICY_VERSION = os.getenv(
    "BIOMIND_CONSULTANT_FINALIZE_BUDGET_POLICY_VERSION",
    "finalize-work-budget-v1",
).strip() or "finalize-work-budget-v1"
CONSULTANT_FINALIZE_MAX_RESOLUTION_ITEMS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_RESOLUTION_ITEMS",
    1_024,
    minimum=1,
)
CONSULTANT_FINALIZE_MAX_BATCH_ITEMS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_BATCH_ITEMS",
    64,
    minimum=1,
)
CONSULTANT_FINALIZE_MAX_CREATED_BATCHES = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_CREATED_BATCHES",
    128,
    minimum=1,
)
CONSULTANT_FINALIZE_MAX_PROVIDER_CALLS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_PROVIDER_CALLS",
    128,
    minimum=1,
)
CONSULTANT_FINALIZE_MAX_REPARTITION_DEPTH = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_REPARTITION_DEPTH",
    8,
    minimum=1,
)
CONSULTANT_FINALIZE_MAX_TOTAL_COMPLETION_TOKENS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_TOTAL_COMPLETION_TOKENS",
    200_000,
    minimum=1,
)
CONSULTANT_FINALIZE_MAX_TOTAL_ESTIMATED_COST_MICROUSD = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_TOTAL_ESTIMATED_COST_MICROUSD",
    50_000_000,
    minimum=1,
)
# Versioned controller-side accounting rate.  It is intentionally conservative
# and does not pretend to be a provider invoice; its purpose is to make the
# finite cost guard operational even when the provider omits billing metadata.
CONSULTANT_FINALIZE_ESTIMATED_MICROUSD_PER_TOKEN = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_ESTIMATED_MICROUSD_PER_TOKEN",
    1,
    minimum=1,
)
CONSULTANT_FINALIZE_MAX_WALL_TIME_MS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_MAX_WALL_TIME_MS",
    900_000,
    minimum=1,
)
CONSULTANT_FINALIZE_PER_CALL_TIMEOUT_MS = _env_int(
    "BIOMIND_CONSULTANT_FINALIZE_PER_CALL_TIMEOUT_MS",
    max(1, int(LLM_TIMEOUT_SECONDS * 1_000)),
    minimum=1,
)
# Planner prompt sizing uses a nominal engineering budget plus real token
# headroom thresholds.  The character budget is not a model context proxy.
PLANNER_NOMINAL_CHAR_BUDGET = _env_int("BIOMIND_PLANNER_NOMINAL_CHAR_BUDGET", 50000, minimum=8000)
PLANNER_SOFT_TOKEN_RATIO = _env_float("BIOMIND_PLANNER_SOFT_TOKEN_RATIO", 0.60, minimum=0.0)
PLANNER_COMPRESSION_TOKEN_RATIO = _env_float("BIOMIND_PLANNER_COMPRESSION_TOKEN_RATIO", 0.75, minimum=0.0)
PLANNER_HARD_TOKEN_RATIO = _env_float("BIOMIND_PLANNER_HARD_TOKEN_RATIO", 0.90, minimum=0.0)
PLANNER_RESERVED_HEADROOM_TOKENS = _env_int(
    "BIOMIND_PLANNER_RESERVED_HEADROOM_TOKENS",
    MODEL_OUTPUT_LIMIT_TOKENS,
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

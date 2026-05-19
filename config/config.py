import os


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
BIOMIND_WIKI_DIR = os.getenv("BIOMIND_WIKI_DIR", "").strip()

LLM_API_KEY = (
    os.getenv("QWEN_API_KEY")
    or os.getenv("DASHSCOPE_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or ""
)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_DEFAULT_MODEL = os.getenv("LLM_DEFAULT_MODEL", "qwen3-max-2026-01-23")

WIKI_EMBEDDING_ENABLED = os.getenv("WIKI_EMBEDDING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
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

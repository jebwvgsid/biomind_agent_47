# biomind/config/__init__.py

# 1. 从 settings.py 导出基础设施 (路径、环境加载)
# 注意：现在 settings.py 只负责这些，不再包含 RAG/LLM 配置
from .settings import (
    PathManager,
    EnvironmentManager
)

# 2. [核心修改] 从 config.py 导出所有的业务配置变量
# 包括 RAG, LLM, BIO路径, KB_MAP 等
from .config import (
    # RAGFlow 配置
    RAGFLOW_ADDRESS,
    RAGFLOW_API_KEY,
    KB_MAP,
    RAGFLOW_DATASET_IDS,
    RERANK_MODEL_ID,
    DEFAULT_PROJECT_ID,
    BIOMIND_MEMORY_DIR,
    BIOMIND_WIKI_DIR,
    BIOMIND_WIKI_INGEST_MODE,
    
    # LLM 配置
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    WIKI_EMBEDDING_ENABLED,
    WIKI_EMBEDDING_BASE_URL,
    WIKI_EMBEDDING_API_KEY,
    WIKI_EMBEDDING_MODEL,
    WIKI_EMBEDDING_DIMENSIONS,
    
    # 生信软件路径
    BIO_SOFTWARE_ROOT,
    BIO_SOURCE_INSTALL_DIR,
    ANACONDA_ROOT
)

# 3. 从 logger.py 导出日志配置
from .logger import LoggingConfig

# 4. 从 startup.py 导出启动检查工具
from .startup import (
    StartupCheck, 
    ReadlineConfig
)

# 定义对外暴露的列表 (方便外部 from config import *)
__all__ = [
    # Infrastructure
    'PathManager',
    'EnvironmentManager',
    'LoggingConfig',
    'StartupCheck',
    'ReadlineConfig',

    # RAG
    'RAGFLOW_ADDRESS',
    'RAGFLOW_API_KEY',
    'KB_MAP',
    'RAGFLOW_DATASET_IDS',
    'RERANK_MODEL_ID',
    'DEFAULT_PROJECT_ID',
    'BIOMIND_MEMORY_DIR',
    'BIOMIND_WIKI_DIR',
    'BIOMIND_WIKI_INGEST_MODE',
    
    # LLM
    'LLM_API_KEY',
    'LLM_BASE_URL',
    'LLM_DEFAULT_MODEL',
    'WIKI_EMBEDDING_ENABLED',
    'WIKI_EMBEDDING_BASE_URL',
    'WIKI_EMBEDDING_API_KEY',
    'WIKI_EMBEDDING_MODEL',
    'WIKI_EMBEDDING_DIMENSIONS',
    
    # BIO
    'BIO_SOFTWARE_ROOT',
    'BIO_SOURCE_INSTALL_DIR',
    'ANACONDA_ROOT'
]

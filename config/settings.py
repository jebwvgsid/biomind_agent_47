import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# ================================
# 1. 路径管理 
# ================================

class PathManager:
    """路径管理器 - 智能分离软件配置与用户数据"""
    
    # A. 软件安装目录 (System Paths)
    # ---------------------------------------------------
    # agent.py 所在的目录 (BioMind/agent_core)
    _CORE_DIR = Path(__file__).parent.absolute()
    # 项目根目录 (BioMind) - 存放代码、配置、数据库
    SYSTEM_ROOT = _CORE_DIR.parent
    
    PROJECT_ROOT = SYSTEM_ROOT  # 解决 PROJECT_ROOT 报错
    # 系统级文件存放位置 (跟着软件走)
    SYSTEM_CONFIG_DIR = SYSTEM_ROOT / "config"
    SYSTEM_LOG_DIR = SYSTEM_ROOT / "logs"
    SYSTEM_DATA_DIR = SYSTEM_ROOT / "data"
    SYSTEM_DB_PATH = SYSTEM_ROOT / "task_states.db"  # 记忆库放在软件目录，方便跨目录共享记忆
    HISTORY_FILE = SYSTEM_LOG_DIR / ".agent_history"     # 命令行历史

    PROJECT_ROOT = SYSTEM_ROOT
    CONFIG_DIR = SYSTEM_CONFIG_DIR
    LOG_DIR = SYSTEM_LOG_DIR
    DATA_DIR = SYSTEM_DATA_DIR
    # B. 用户工作目录 (User Working Paths)
    # ---------------------------------------------------
    # 用户在哪个目录下敲的命令，哪里就是 WORK_DIR
    WORK_DIR = Path(os.getcwd())
    
    # 结果输出目录 (默认在当前工作目录下生成 task_outputs)
    DEFAULT_OUTPUT_DIR = WORK_DIR / "task_outputs"

    @classmethod
    def setup_paths(cls):
        """初始化路径"""
        # 1. 将代码目录加入 Python 搜索路径 (解决 import 问题)
        for path in [cls.SYSTEM_ROOT, cls._CORE_DIR]:
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        
        # 2. 创建系统级目录 (如果没有的话)
        for path in [cls.SYSTEM_LOG_DIR, cls.SYSTEM_CONFIG_DIR, cls.SYSTEM_DATA_DIR]:
            path.mkdir(parents=True, exist_ok=True)
            
        # 注意：我们不自动创建 WORK_DIR 下的目录，等任务执行时再建，保持用户目录整洁

# 应用路径设置
PathManager.setup_paths()

# ================================
# 2. 环境变量管理 (EnvironmentManager)
# ================================
class EnvironmentManager:
    @staticmethod
    def load_environment():
        """加载环境变量 (.env)"""
        # 这一步非常关键：它保证了后续 config.py 能读到 .env 里的值
        env_paths = [
            PathManager.WORK_DIR / ".env",      # 当前目录优先级最高
            PathManager.SYSTEM_ROOT / ".env",   # 软件目录默认配置
        ]
        
        for env_path in env_paths:
            if env_path.exists():
                load_dotenv(env_path, override=True)

# 自动加载环境 (必须在 config.py 导入前执行)
EnvironmentManager.load_environment()
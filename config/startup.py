import os
import sys
import logging
import readline
import glob
import atexit
from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, List, Optional, Any
from .settings import PathManager, EnvironmentManager
from . import config as sys_config

# ================================
# 2. 环境变量管理 (EnvironmentManager)
# ================================
class EnvironmentManager:
    @staticmethod
    def load_environment():
        """加载环境变量 (.env)"""
        # 优先级：
        # 1. 用户当前目录下的 .env (针对特定项目的配置)
        # 2. 软件安装目录下的 .env (全局默认配置)
        env_paths = [
            PathManager.WORK_DIR / ".env",      #当前目录
            PathManager.SYSTEM_ROOT / ".env",   #软件目录
        ]
        
        loaded = False
        for env_path in env_paths:
            if env_path.exists():
                load_dotenv(env_path, override=True) # override=True 表示前面的覆盖后面的
                logging.info(f"✓ Loaded environment from: {env_path}")
                loaded = True
        
        if not loaded:
            logging.warning("⚠️ No .env file found in Work or System dir")

    @staticmethod
    def get_required_vars() -> Dict[str, str]:
        """获取关键环境变量"""
        return {
            'RAGFLOW_ADDRESS': os.getenv('RAGFLOW_ADDRESS', ''),
            'RAGFLOW_API_KEY': os.getenv('RAGFLOW_API_KEY', ''),
            'RAGFLOW_DATASET_IDS': os.getenv('RAGFLOW_DATASET_IDS', ''),
            'RERANK_MODEL_ID': os.getenv('RERANK_MODEL_ID', ''),
            'DASHSCOPE_API_KEY': os.getenv('DASHSCOPE_API_KEY', ''),
            'QWEN_API_KEY': os.getenv('QWEN_API_KEY', ''), # 确保获取 Qwen Key
            'LOG_LEVEL': os.getenv('LOG_LEVEL', 'INFO')
        }

# 加载环境
EnvironmentManager.load_environment()

# ================================
# 3. 全局常量与映射 (从 config.py 或 Env 导出)
# ================================

# 尝试读取旧的 config.py (兼容旧代码)
try:
    sys.path.append(str(PathManager.SYSTEM_ROOT))
    import config as _legacy_config
    KB_MAP = getattr(_legacy_config, 'KB_MAP', {})
    RAGFLOW_DATASET_IDS = getattr(_legacy_config, 'RAGFLOW_DATASET_IDS', [])
except ImportError:
    KB_MAP = {}
    ids_str = os.getenv('RAGFLOW_DATASET_IDS', '')
    RAGFLOW_DATASET_IDS = ids_str.split(',') if ids_str else []

# 导出核心变量
RAGFLOW_ADDRESS = os.getenv('RAGFLOW_ADDRESS')
RAGFLOW_API_KEY = os.getenv('RAGFLOW_API_KEY')
RERANK_MODEL_ID = os.getenv('RERANK_MODEL_ID')



# ================================
# 8. 启动检查 (新增)
# ================================
# 获取 Logger (前提是 logger 已经在 main.py 或 config/logger.py 中初始化过)
logger = logging.getLogger("BioMind")

class StartupCheck:
    """启动时的系统检查"""
    
    @staticmethod
    def run_checks():
        """运行启动检查"""
        logger.info("="*60)
        logger.info("🧬 BioAnalyst Agent - System Check")
        logger.info("="*60)
        
        checks = []
        
        # 检查 Python 版本
        python_version = sys.version_info
        if python_version >= (3, 8):
            logger.info(f"✓ Python version: {sys.version.split()[0]}")
            checks.append(True)
        else:
            logger.error(f"❌ Python 3.8+ required, got {sys.version.split()[0]}")
            checks.append(False)
        
        # 检查必需的环境变量
        env_vars = EnvironmentManager.get_required_vars()
        
        # === [开始修改] ===
        # 获取 config.py 中的 Key (如果文件上方导入成功)
        file_config_key = getattr(_legacy_config, 'RAGFLOW_API_KEY', None) if '_legacy_config' in globals() else None
        
        # 只要 环境变量 或 config.py 任意一个有值，就算通过
        if env_vars['RAGFLOW_API_KEY'] or file_config_key:
            logger.info("✓ RAGFlow API Key: Present")
            checks.append(True)
        else:
            logger.warning("⚠️ RAGFlow API Key: Missing (RAG disabled)")
            checks.append(True)  # 非致命错误
        
        if env_vars['DASHSCOPE_API_KEY'] or env_vars['QWEN_API_KEY']:
            logger.info("✓ LLM API Key: Present")
            checks.append(True)
        else:
            logger.error("❌ LLM API Key: Missing (Need DASHSCOPE_API_KEY or QWEN_API_KEY)")
            checks.append(False)
        
        # 检查目录
        for name, path in [
            ("Data", PathManager.DATA_DIR),
            ("Logs", PathManager.LOG_DIR),
            ("Config", PathManager.CONFIG_DIR)
        ]:
            if path.exists():
                logger.info(f"✓ {name} directory: {path}")
                checks.append(True)
            else:
                logger.error(f"❌ {name} directory: Missing")
                checks.append(False)
        
        # 总结
        passed = sum(checks)
        total = len(checks)
        logger.info("="*60)
        logger.info(f"System Check: {passed}/{total} passed")
        logger.info("="*60)
        
        return all(checks)

# 运行启动检查 (可选)
if os.getenv('SKIP_STARTUP_CHECK', 'false').lower() != 'true':
    startup_ok = StartupCheck.run_checks()
    if not startup_ok:
        logger.error("⚠️ Startup checks failed. Some features may not work.")

# ================================
# 4. Readline 配置 (增强版 - 支持 Tab 补全)
# ================================

class ReadlineConfig:
    """Readline 配置 (改善命令行输入体验 + Tab 补全)"""
    
    @staticmethod
    def setup_readline():
        """配置 readline (如果可用)"""
        if sys.platform == 'win32':
            # Windows 通常不支持标准 readline，除非安装 pyreadline3
            # 这里简单处理，避免报错
            return False

        try:
            # 1. 定义补全逻辑 (内部函数，防止污染全局命名空间)
            def completer(text, state):
                """
                text: 用户当前正在输入的单词片段 (例如 'wo')
                state: 尝试的索引 (0, 1, 2...)
                """
                line = readline.get_line_buffer().strip()
                
                # A. 如果是输入系统命令 (以 / 开头)
                if line.startswith('/') and ' ' not in line:
                    options = ['/tasks', '/new', '/resume', '/stop', '/debug', '/exit']
                    matches = [s for s in options if s.startswith(text)]
                    if state < len(matches):
                        return matches[state] + ' '
                    return None

                # B. 默认：文件/目录路径补全
                # 处理波浪号 ~
                expanded_text = os.path.expanduser(text)
                
                # 获取匹配列表
                matches = glob.glob(expanded_text + '*')
                
                # 优化体验：如果是目录，自动追加 '/'，否则追加空格
                refined_matches = []
                for match in matches:
                    if os.path.isdir(match):
                        refined_matches.append(match + "/")
                    else:
                        refined_matches.append(match + " ")
                
                if state < len(refined_matches):
                    return refined_matches[state]
                else:
                    return None

            # 2. 绑定 Tab 键
            readline.set_completer(completer)
            readline.parse_and_bind("tab: complete")
            
            # 优化分隔符 (让路径中的斜杠不被当作分隔符，这样才能补全深层目录)
            readline.set_completer_delims(' \t\n`~!@#$%^&*()=+[{]}\\|;:\'",<>?')

            # 3. 历史记录配置
            history_file = PathManager.HISTORY_FILE
            if history_file.exists():
                readline.read_history_file(str(history_file))
            readline.set_history_length(1000)
            
            atexit.register(readline.write_history_file, str(history_file))
            
            # 只有在 logger 初始化后才能打印，否则用 print
            if logger.handlers:
                logger.info("✓ Readline configured (with Tab completion)")
            
            return True
            
        except ImportError:
            # 这种情况常见于没有安装 readline 模块的环境
            return False
        except Exception as e:
            if logger.handlers:
                logger.error(f"❌ Readline setup failed: {e}")
            else:
                print(f"❌ Readline setup failed: {e}")
            return False

if sys.platform != 'win32':
    ReadlineConfig.setup_readline()
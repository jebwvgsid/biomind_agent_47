import sys
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# 导入上面的 settings 以获取路径
from .settings import PathManager

def setup_utf8_io():
    """配置 UTF-8 输入输出 (仅 Windows 需要)"""
    if sys.platform == 'win32':
        try:
            import io
            # 只在 Windows 上修改编码
            sys.stdin.reconfigure(encoding='utf-8')
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            # Python 3.6 或更早版本的后备方案
            import io
            sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 应用编码设置
setup_utf8_io()

class LoggingConfig:
    """日志配置管理"""
    
    @staticmethod
    def setup_logging(
        level: str = "INFO",
        log_to_file: bool = True,
        log_file: Optional[str] = None
    ):
        """配置日志系统"""
        
        # 确定日志级别
        log_level = getattr(logging, level.upper(), logging.INFO)
        
        # 日志格式
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        date_format = '%Y-%m-%d %H:%M:%S'
        
        # 处理器列表
        handlers = [logging.StreamHandler()]
        
        # 添加文件处理器
        if log_to_file:
            if log_file is None:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                log_file = PathManager.SYSTEM_LOG_DIR / f"biomind_{timestamp}.log"
            
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(
                logging.Formatter(log_format, datefmt=date_format)
            )
            handlers.append(file_handler)
        
        # 配置根日志记录器
        logging.basicConfig(
            level=log_level,
            format=log_format,
            datefmt=date_format,
            handlers=handlers,
            force=True  # 强制重新配置
        )
        
        logger = logging.getLogger(__name__)
        logger.info("✓ Logging system initialized")
        logger.info(f"  Log Level: {level}")
        logger.info(f"  Log File: {log_file if log_to_file else 'None'}")
        
        return logger

# 配置日志
logger = LoggingConfig.setup_logging(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    log_to_file=True
)
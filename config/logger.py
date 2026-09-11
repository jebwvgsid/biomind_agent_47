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
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if not callable(reconfigure):
                continue
            try:
                reconfigure(encoding='utf-8')
            except (OSError, ValueError, TypeError):
                pass

# 应用编码设置
setup_utf8_io()


class InteractiveConsoleFilter(logging.Filter):
    """Keep background archive-writer telemetry out of the live prompt."""

    BACKGROUND_PREFIXES = ("memory.session_archive_writer",)

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(
            str(record.name).startswith(prefix)
            for prefix in self.BACKGROUND_PREFIXES
        )

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
        
        from agent_core.cli_logging import configure_logging
        if log_to_file and log_file is None:
            # Stable within the process, including repeated session initialization.
            log_file = PathManager.SYSTEM_LOG_DIR / f"biomind_{os.getpid()}.log"
        configure_logging(log_level, log_file if log_to_file else None)

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

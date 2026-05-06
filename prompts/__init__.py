# prompts/__init__.py

# 1. 导出动态 Prompt 管理器 (主智能体使用)
from .manager import PromptManager

# 2. 导出特定的静态模板 
# (安装子智能体可能直接需要 INSTALLER_SYSTEM_PROMPT，而不经过 Manager)
# 假设你在 templates.py 里定义了 INSTALLER_SYSTEM_PROMPT
from .templates import (
    INSTALLER_SYSTEM_PROMPT
)

__all__ = [
    'PromptManager',
    'INSTALLER_SYSTEM_PROMPT'
]
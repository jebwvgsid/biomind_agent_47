import asyncio
import sys
import os
import io
import ast
import re
from io import StringIO
from typing import Tuple, Dict, Any, Optional

# 全局变量：用于保持 Python REPL 的上下文记忆 (变量、导入库等)
_persistent_namespace: Dict[str, Any] = {
    'pd': None, 
    'np': None
}

_FENCE_RE = re.compile(r"^\s*```(?P<lang>[A-Za-z0-9_-]+)?\s*\n(?P<body>.*?)\n```\s*$", re.DOTALL)


def _strip_outer_code_fence(code: str) -> Tuple[str, Optional[str]]:
    """Return code without an enclosing markdown fence, plus the fence language."""
    match = _FENCE_RE.match(code.strip())
    if not match:
        return code.strip(), None
    return match.group("body").strip(), (match.group("lang") or "").lower() or None


def normalize_script_code(code: str) -> str:
    """Normalize LLM-generated executable snippets before review/save/run."""
    code, _ = _strip_outer_code_fence(code)
    code = re.sub(r"^\s*#!\s*(?:PYTHON|BASH|SH)\s*\n?", "", code, flags=re.IGNORECASE)
    return code.strip()


def detect_script_language(code: str) -> str:
    """
    Detect whether an <execute> block should run as Bash or Python.

    The old heuristic treated any snippet containing "import " as Python, which
    breaks Bash scripts that create Python files with heredocs. Prefer explicit
    markers and Bash syntax before considering Python imports.
    """
    raw = code.strip()
    stripped, fence_lang = _strip_outer_code_fence(raw)
    first_nonempty = next((line.strip() for line in stripped.splitlines() if line.strip()), "")
    first_lower = first_nonempty.lower()

    if fence_lang in {"bash", "sh", "shell", "zsh"}:
        return "Bash"
    if fence_lang in {"python", "py"}:
        return "Python"
    if first_lower.startswith(("#!/bin/bash", "#!/usr/bin/env bash", "#!/bin/sh", "#!/usr/bin/env sh", "#!bash", "#!sh")):
        return "Bash"
    if first_lower.startswith(("#!/usr/bin/env python", "#!/bin/python", "#!python", "#!py")):
        return "Python"

    bash_patterns = [
        r"<<\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?",  # heredoc
        r"(^|\n)\s*(if|elif)\s+\[\[",
        r"(^|\n)\s*(then|fi|done|esac)\s*(#.*)?($|\n)",
        r"(^|\n)\s*(for|while)\b.*;\s*do\b",
        r"(^|\n)\s*(case)\b.*\bin\b",
        r"(^|\n)\s*(cd|export|source|mkdir|chmod|conda|pip|cat|echo|ls|grep|awk|sed|Rscript)\b",
        r"\$\{[^}]+\}",
    ]
    if any(re.search(pattern, stripped, re.IGNORECASE | re.MULTILINE) for pattern in bash_patterns):
        return "Bash"

    try:
        ast.parse(stripped)
        return "Python"
    except SyntaxError:
        pass

    python_patterns = [
        r"(^|\n)\s*import\s+\w+",
        r"(^|\n)\s*from\s+\w[\w.]*\s+import\s+",
        r"(^|\n)\s*(def|class)\s+\w+",
    ]
    if any(re.search(pattern, stripped, re.MULTILINE) for pattern in python_patterns):
        return "Python"

    return "Bash"


async def run_command(command: str, timeout=None) -> Tuple[str, str, int]:
    """通用 Shell 命令执行器"""
    try:
        # 移除 timeout 的外层包裹（如果默认传 None 的话），并切断 stdin
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,  # 👈 核心修改：切断输入。遇到需要按 y/n 的地方直接报错，拒绝死等
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # 无限期等待进程执行完毕
        if timeout:
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return "", f"TIMEOUT after {timeout} seconds", 124
        else:
            stdout, stderr = await process.communicate()
        
        return stdout.decode(), stderr.decode(), process.returncode
    except Exception as e:
        return "", str(e), 1

async def run_bash_script(code: str, timeout: int = None) -> Tuple[str, str, int]:
    """执行 Bash 脚本 (自动处理权限和临时文件)"""
    try:
        filename = "temp_script.sh"
        with open(filename, "w", encoding="utf-8") as f:
            # 👈 核心修改：强制添加 bash 解释器声明
            if not code.strip().startswith("#!"):
                f.write("#!/bin/bash\n\n")
            f.write(code)
        
        # 赋予执行权限
        os.chmod(filename, 0o755)
        
        # 执行（直接传 None 确保没有超时限制）
        return await run_command(f"./{filename}", timeout=None)
    except Exception as e:
        return "", str(e), 1

async def run_python_repl(code: str, timeout: int = None) -> Tuple[str, str, int]:
    """
    在持久化命名空间中执行 Python 代码
    注意：这是模拟 REPL，不是独立的 Python 进程
    """
    try:
        # 预加载常用库
        if _persistent_namespace['pd'] is None:
            try:
                import pandas as pd
                import numpy as np
                _persistent_namespace['pd'] = pd
                _persistent_namespace['np'] = np
            except ImportError:
                pass
        
        # 捕获 stdout
        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()
        
        # 在全局命名空间执行
        exec(code, _persistent_namespace)
        
        # 恢复 stdout
        sys.stdout = old_stdout
        output = captured_output.getvalue()
        
        return output, "", 0
    except Exception as e:
        # 恢复 stdout (防止崩溃后没恢复)
        if 'old_stdout' in locals():
            sys.stdout = old_stdout
        return "", str(e), 1

async def run_r_code(code: str, timeout: int = None) -> Tuple[str, str, int]:
    """执行 R 脚本"""
    try:
        with open("temp_script.R", "w", encoding="utf-8") as f:
            f.write(code)
        return await run_command("Rscript temp_script.R", timeout)
    except Exception as e:
        return "", str(e), 1


def reset_python_namespace():
    global _persistent_namespace
    _persistent_namespace = {
        'pd': None,
        'np': None
    }

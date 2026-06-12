import asyncio
import contextlib
import sys
import os
import io
import ast
import re
import signal
import time
from io import StringIO
from typing import Tuple, Dict, Any, Optional

from execution.profiling import get_current_node, get_current_profiler, get_current_task_id, timestamp_now
from execution.shell_safety import shell_safety_error

# 全局变量：用于保持 Python REPL 的上下文记忆 (变量、导入库等)
_persistent_namespace: Dict[str, Any] = {
    'pd': None, 
    'np': None
}

_FENCE_RE = re.compile(r"^\s*```(?P<lang>[A-Za-z0-9_-]+)?\s*\n(?P<body>.*?)\n```\s*$", re.DOTALL)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _collect_process_tree_cpu_ticks(root_pid: int) -> Optional[int]:
    """Return cumulative utime+stime ticks for a Linux process tree."""
    proc_root = "/proc"
    if os.name == "nt" or not os.path.isdir(proc_root):
        return None

    processes: Dict[int, Tuple[int, int]] = {}
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        stat_path = os.path.join(proc_root, name, "stat")
        try:
            stat = open(stat_path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        end_comm = stat.rfind(")")
        if end_comm < 0:
            continue
        fields = stat[end_comm + 2 :].split()
        if len(fields) < 15:
            continue
        try:
            pid = int(name)
            ppid = int(fields[1])
            utime = int(fields[11])
            stime = int(fields[12])
        except (TypeError, ValueError):
            continue
        processes[pid] = (ppid, utime + stime)

    if root_pid not in processes:
        return None

    children: Dict[int, list[int]] = {}
    for pid, (ppid, _) in processes.items():
        children.setdefault(ppid, []).append(pid)

    total = 0
    stack = [root_pid]
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        item = processes.get(pid)
        if not item:
            continue
        total += item[1]
        stack.extend(children.get(pid, []))
    return total


async def _read_stream_chunks(stream: Optional[asyncio.StreamReader], chunks: list[bytes], progress: Dict[str, float]) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
        progress["last_output_at"] = time.monotonic()


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    else:
        process.kill()
    with contextlib.suppress(Exception):
        await process.wait()


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


async def run_command(command: str, timeout=None, profile: bool = True) -> Tuple[str, str, int]:
    """通用 Shell 命令执行器"""
    start_time = time.time()
    start_monotonic = time.monotonic()
    started_at = timestamp_now()
    health_interval = max(0.2, _env_float("BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS", 30.0))
    stall_timeout = _env_float("BIOMIND_EXEC_STALL_TIMEOUT_SECONDS", 600.0)
    wall_timeout = float(timeout) if timeout else _env_float("BIOMIND_EXEC_WALL_TIMEOUT_SECONDS", 0.0)
    try:
        # 切断 stdin。遇到需要按 y/n 的地方直接报错，拒绝死等。
        popen_kwargs: Dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs,
        )

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        progress = {"last_output_at": start_monotonic, "last_cpu_at": start_monotonic}
        last_cpu_ticks = _collect_process_tree_cpu_ticks(process.pid)
        stdout_task = asyncio.create_task(_read_stream_chunks(process.stdout, stdout_chunks, progress))
        stderr_task = asyncio.create_task(_read_stream_chunks(process.stderr, stderr_chunks, progress))
        stderr_reason = ""

        while True:
            try:
                await asyncio.wait_for(process.wait(), timeout=health_interval)
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                cpu_ticks = _collect_process_tree_cpu_ticks(process.pid)
                if cpu_ticks is not None and last_cpu_ticks is not None and cpu_ticks > last_cpu_ticks:
                    progress["last_cpu_at"] = now
                if cpu_ticks is not None:
                    last_cpu_ticks = cpu_ticks

                last_progress_at = max(progress["last_output_at"], progress["last_cpu_at"])
                if wall_timeout > 0 and now - start_monotonic >= wall_timeout:
                    stderr_reason = f"TIMEOUT after {wall_timeout:g} seconds"
                    await _terminate_process_tree(process)
                    break
                if stall_timeout > 0 and now - last_progress_at >= stall_timeout:
                    idle_for = now - last_progress_at
                    stderr_reason = (
                        "STALLED: command showed no stdout/stderr output and no process-tree CPU progress "
                        f"for {idle_for:.1f} seconds; terminated by BioMind watchdog."
                    )
                    await _terminate_process_tree(process)
                    break

        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        stdout_text = b"".join(stdout_chunks).decode(errors="replace")
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")
        if stderr_reason:
            stderr_text = "\n".join(part for part in [stderr_text.strip(), stderr_reason] if part)
            result = stdout_text, stderr_text, 124
        else:
            result = stdout_text, stderr_text, process.returncode
        return result
    except Exception as e:
        result = "", str(e), 1
        return result
    finally:
        if profile and "result" in locals():
            profiler = get_current_profiler()
            task_id = get_current_task_id()
            if profiler and task_id:
                stdout, stderr, returncode = result
                profiler.record_command(
                    task_id=task_id,
                    language="Bash",
                    code=command,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    duration_seconds=time.time() - start_time,
                    node=get_current_node(),
                    started_at=started_at,
                )

async def run_bash_script(code: str, timeout: int = None) -> Tuple[str, str, int]:
    """执行 Bash 脚本 (自动处理权限和临时文件)"""
    start_time = time.time()
    started_at = timestamp_now()
    try:
        safety_error = shell_safety_error(code, "Bash")
        if safety_error:
            return "", safety_error, 126

        filename = "temp_script.sh"
        with open(filename, "w", encoding="utf-8") as f:
            # 👈 核心修改：强制添加 bash 解释器声明
            if not code.strip().startswith("#!"):
                f.write("#!/bin/bash\n\n")
            f.write(code)
        
        # 赋予执行权限
        os.chmod(filename, 0o755)
        
        # 执行时使用 run_command 的健康检查；timeout 作为可选硬性 wall limit。
        stdout, stderr, returncode = await run_command(f"./{filename}", timeout=timeout, profile=False)
        return stdout, stderr, returncode
    except Exception as e:
        stdout, stderr, returncode = "", str(e), 1
        return stdout, stderr, returncode
    finally:
        if "returncode" in locals():
            profiler = get_current_profiler()
            task_id = get_current_task_id()
            if profiler and task_id:
                profiler.record_command(
                    task_id=task_id,
                    language="Bash",
                    code=code,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    duration_seconds=time.time() - start_time,
                    node=get_current_node(),
                    started_at=started_at,
                )

async def run_python_repl(code: str, timeout: int = None) -> Tuple[str, str, int]:
    """
    在持久化命名空间中执行 Python 代码
    注意：这是模拟 REPL，不是独立的 Python 进程
    """
    start_time = time.time()
    started_at = timestamp_now()
    old_stdout = sys.stdout
    captured_output = None
    try:
        safety_error = shell_safety_error(code, "Python")
        if safety_error:
            return "", safety_error, 126

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
        sys.stdout = captured_output = StringIO()
        
        # 在全局命名空间执行
        exec(code, _persistent_namespace)
        
        output = captured_output.getvalue()
        
        stdout, stderr, returncode = output, "", 0
        return stdout, stderr, returncode
    except SystemExit as e:
        stdout, stderr, returncode = "", f"Generated Python code attempted to exit the agent process: {e}", 1
        return stdout, stderr, returncode
    except Exception as e:
        stdout, stderr, returncode = "", str(e), 1
        return stdout, stderr, returncode
    finally:
        sys.stdout = old_stdout
        if captured_output is not None:
            captured_output.close()
        if "returncode" in locals():
            profiler = get_current_profiler()
            task_id = get_current_task_id()
            if profiler and task_id:
                profiler.record_command(
                    task_id=task_id,
                    language="Python",
                    code=code,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    duration_seconds=time.time() - start_time,
                    node=get_current_node(),
                    started_at=started_at,
                )

async def run_r_code(code: str, timeout: int = None) -> Tuple[str, str, int]:
    """执行 R 脚本"""
    start_time = time.time()
    started_at = timestamp_now()
    try:
        with open("temp_script.R", "w", encoding="utf-8") as f:
            f.write(code)
        stdout, stderr, returncode = await run_command("Rscript temp_script.R", timeout, profile=False)
        return stdout, stderr, returncode
    except Exception as e:
        stdout, stderr, returncode = "", str(e), 1
        return stdout, stderr, returncode
    finally:
        if "returncode" in locals():
            profiler = get_current_profiler()
            task_id = get_current_task_id()
            if profiler and task_id:
                profiler.record_command(
                    task_id=task_id,
                    language="R",
                    code=code,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    duration_seconds=time.time() - start_time,
                    node=get_current_node(),
                    started_at=started_at,
                )


def reset_python_namespace():
    global _persistent_namespace
    _persistent_namespace = {
        'pd': None,
        'np': None
    }

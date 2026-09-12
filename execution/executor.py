import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import sys
import os
import io
import ast
import re
import shlex
import signal
import tempfile
import time
from io import StringIO
from pathlib import Path
from typing import Tuple, Dict, Any, Optional, Callable, Mapping

from execution.profiling import get_current_node, get_current_profiler, get_current_task_id, timestamp_now
from execution.runtime_contract import (
    BASH_SCRIPT_PROLOGUE,
    EXECUTION_RUNTIME_POLICY,
)
from execution.shell_safety import shell_safety_error

from agent_core.cli_display import StreamDisplayCopy, emit as emit_display, terminal as active_terminal

logger = logging.getLogger(__name__)

# Shell commands run in their own process groups so tools that spawn workers can
# be cleaned up as one unit. Keep their leaders here so Ctrl-C can also stop a
# command that no longer shares the interactive terminal's process group.
_ACTIVE_PROCESS_GROUPS: set[int] = set()
_LAST_EXECUTION_METADATA: Dict[str, Any] = {}

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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class _BoundedStreamCapture:
    """Keep a bounded head/tail projection while counting the full stream."""

    def __init__(self, limit: int):
        self.limit = max(16_384, int(limit))
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total_bytes = 0

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.limit

    @property
    def projected_bytes(self) -> int:
        return len(self.head) + len(self.tail)

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        remaining = chunk
        if len(self.head) < self.head_limit:
            take = min(self.head_limit - len(self.head), len(remaining))
            self.head.extend(remaining[:take])
            remaining = remaining[take:]
        if remaining and self.tail_limit:
            self.tail.extend(remaining)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    def text(self, stream_name: str, full_log_path: str = "") -> str:
        head = bytes(self.head).decode(errors="replace")
        tail = bytes(self.tail).decode(errors="replace")
        if not self.truncated:
            return head + tail
        detail = (
            f"\n[BIOMIND_OUTPUT_TRUNCATED stream={stream_name} "
            f"projected_bytes={self.projected_bytes} total_bytes={self.total_bytes}"
        )
        if full_log_path:
            detail += f" full_log={full_log_path}"
        return head + detail + "]\n" + tail


def _execution_stream_log_paths(process_pid: int) -> Tuple[str, str]:
    """Return durable full-stream paths when a task profiler is active."""

    profiler = get_current_profiler()
    task_id = get_current_task_id()
    output_root = getattr(profiler, "output_root", None) if profiler else None
    if not output_root or not task_id:
        return "", ""
    execution_dir = Path(output_root) / str(task_id) / "executions"
    execution_dir.mkdir(parents=True, exist_ok=True)
    stem = f"executor_{process_pid}_{time.time_ns()}"
    return (
        str(execution_dir / f"{stem}.stdout.full.log"),
        str(execution_dir / f"{stem}.stderr.full.log"),
    )


def _watchdog_command_class(command: str) -> str:
    """Classify commands whose silence is usually diagnostic rather than compute.

    This relies on shell operation classes, not on any domain-specific tool.
    A silent alignment or database build can still make CPU progress, while a
    file-system scan or network probe with no progress is worth stopping early.
    """
    text = str(command or "").lower()
    if re.search(r"\b(?:find|locate)\b|\b(?:rg|grep)\s+-[a-z]*r", text):
        return "discovery"
    if re.search(r"\b(?:curl|wget|git\s+clone)\b", text):
        return "network"
    return ""


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
            with open(stat_path, "r", encoding="utf-8", errors="replace") as handle:
                stat = handle.read()
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


async def _read_stream_chunks(
    stream: Optional[asyncio.StreamReader],
    capture: _BoundedStreamCapture,
    progress: Dict[str, float],
    log_handle: Optional[io.BufferedWriter] = None,
    *,
    stream_name: str = "stream",
    output_limit: int = 0,
    output_limit_event: Optional[asyncio.Event] = None,
    display_context: Mapping[str, Any] | None = None,
    display_enabled: bool = True,
) -> None:
    if stream is None:
        return
    limit_reached = False
    display_copy = _stream_display_copy(stream_name, display_context) if display_enabled else None
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            if display_copy is not None:
                _feed_display_copy(display_copy, b"", final=True)
            break
        if limit_reached:
            # Keep draining until the process group is reaped so PIPE backpressure
            # cannot deadlock termination. The excess is intentionally discarded.
            continue
        capture.feed(chunk)
        if log_handle is not None:
            log_handle.write(chunk)
        if display_copy is not None:
            _feed_display_copy(display_copy, chunk)
        progress["last_output_at"] = time.monotonic()
        if output_limit > 0 and capture.total_bytes > output_limit:
            progress["output_limit_stream"] = stream_name
            progress["output_limit_bytes"] = capture.total_bytes
            if output_limit_event is not None:
                output_limit_event.set()
            limit_reached = True


async def _terminate_process_tree(process: asyncio.subprocess.Process, *, short_check=False) -> None:
    if short_check and os.name != "nt":
        # Opt-in for nested short checks. The shell leader may already have
        # exited while a descendant still holds its pipes. Its original pgid
        # remains the owned spawn pid, so do not rediscover it through the leader.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.025)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=1.0)
        return
    if process.returncode is not None:
        return
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while process.returncode is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    else:
        process.kill()
    deadline = time.monotonic() + 2
    while process.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def terminate_active_processes() -> int:
    """Request termination of every command currently owned by this executor.

    This function is signal-handler safe: it sends SIGTERM but does not await.
    ``run_command`` then performs the normal wait/cleanup while cancellation
    unwinds through the graph.
    """
    stopped = 0
    for pid in list(_ACTIVE_PROCESS_GROUPS):
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
            stopped += 1
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return stopped


def get_last_execution_metadata() -> Dict[str, Any]:
    """Return the latest executor metadata without changing the tuple API."""
    return dict(_LAST_EXECUTION_METADATA)


def _short_check_cleanup_facts(pid):
    if os.name == "nt" or pid is None:
        return {"group_reaped": None, "group_cleanup_status": "unconfirmed"}
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return {"group_reaped": True, "group_cleanup_status": "confirmed_absent"}
    except (OSError, PermissionError):
        return {"group_reaped": None, "group_cleanup_status": "unconfirmed"}
    return {"group_reaped": False, "group_cleanup_status": "group_still_observed"}


def _execution_display_scope(context: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Copy only runtime identity fields; never expose execution arguments."""
    return {
        key: value for key, value in dict(context or {}).items()
        if key in {"task_id", "node", "call_id", "action_id", "execution_attempt_id",
                   "execution_attempt", "pid", "tool_request_id", "tool_call_id"}
        and isinstance(value, (str, int)) and not isinstance(value, bool)
    }


def _stream_display_copy(name: str, context: Mapping[str, Any] | None):
    try:
        if active_terminal() is not None:
            return StreamDisplayCopy(name, **_execution_display_scope(context))
    except Exception:
        logger.debug("Execution display unavailable", exc_info=True)
    return None


def _feed_display_copy(copy, chunk: bytes, *, final: bool = False) -> None:
    try:
        copy.feed(chunk, final=final)
    except Exception:
        # Display is a copy after raw capture/log writes, never their authority.
        logger.debug("Execution display failed", exc_info=True)


async def _notify_lifecycle(
    callback: Optional[Callable[..., Any]], event: str, payload: Dict[str, Any],
    *, display_context: Mapping[str, Any] | None = None,
) -> None:
    scope = _execution_display_scope(display_context)
    if payload.get("pid") is not None:
        scope["pid"] = payload["pid"]
    emit_display("details", f"工具事件 {event}", source="runtime_fact", **scope)
    if event == "RUNNING":
        emit_display("tool_start", "工具进程已启动；等待执行结果", source="runtime_fact", tool_category="process", **scope)
    elif event == "PROCESS_EXITED":
        returncode = payload.get("returncode")
        text = ("命令 rc=0，等待正式核验" if returncode == 0 else
                f"命令退出 rc={returncode}；等待后端处理")
        if payload.get("pid") is None:
            text = "命令未启动；等待后端处理"
        emit_display("tool_end", text, source="runtime_fact", returncode=returncode,
                     command_started=payload.get("pid") is not None, tool_category="process",
                     status="returned" if returncode == 0 else "unknown" if returncode is None else "failed", **scope)
    elif event == "PROCESS_CANCELLED":
        emit_display("cancel", "工具执行已取消", source="runtime_fact", **scope)
    if callback is None:
        return
    result = callback(event, dict(payload))
    if inspect.isawaitable(result):
        await result


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


async def _follow_managed_logs(paths, process, captures, progress, log_handles, limit, limit_event, errors,
                               *, display_context=None):
    """Observe existing wrapper logs by offset; no pipe/tee or command mutation."""
    import stat as stat_module
    offsets = {name: 0 for name in paths}
    last_display = {name: 0.0 for name in paths}
    identities = {}
    display_copies = {name: _stream_display_copy(name, display_context) for name in paths}
    while True:
        exited = process.returncode is not None
        for name, path in paths.items():
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(descriptor, "rb") as source:
                    stat = os.fstat(source.fileno())
                    if not stat_module.S_ISREG(stat.st_mode):
                        raise OSError("LOG_NOT_REGULAR_FILE")
                    identity = (stat.st_dev, stat.st_ino)
                    if (name in identities and identities[name] != identity) or stat.st_size < offsets[name]:
                        raise OSError("LOG_REPLACED_OR_TRUNCATED")
                    identities[name] = identity
                    source.seek(offsets[name])
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        offsets[name] += len(chunk)
                        captures[name].feed(chunk)
                        progress["last_output_at"] = time.monotonic()
                        if log_handles[name] is not None:
                            try:
                                log_handles[name].write(chunk)
                                log_handles[name].flush()
                            except OSError as exc:
                                errors.append({"stage": "execution.log_writer", "stream": name, "error": type(exc).__name__})
                                log_handles[name] = None
                        if display_copies[name] is not None:
                            _feed_display_copy(display_copies[name], chunk)
                        if limit and captures[name].total_bytes > limit:
                            progress["output_limit_stream"] = name
                            progress["output_limit_bytes"] = captures[name].total_bytes
                            limit_event.set()
                        await asyncio.sleep(0)
                        now = time.monotonic()
                        if now - last_display[name] >= 1.0 or exited:
                            logger.info("Execution output stream=%s offset=%s tail=%s", name, offsets[name], chunk[-2000:].decode(errors="replace"))
                            last_display[name] = now
            except FileNotFoundError:
                if exited:
                    errors.append({"stage": "execution.log_reader", "stream": name, "error": "LOG_MISSING"})
            except OSError as exc:
                errors.append({"stage": "execution.log_reader", "stream": name, "error": type(exc).__name__})
                paths = {key: value for key, value in paths.items() if key != name}
        if exited:
            for copy in display_copies.values():
                if copy is not None:
                    _feed_display_copy(copy, b"", final=True)
            break
        await asyncio.sleep(0.2)


async def run_command(
    command: str,
    timeout=None,
    profile: bool = True,
    lifecycle_callback: Optional[Callable[..., Any]] = None,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int | None = None,
    managed_log_paths: Mapping[str, str] | None = None,
    display_context: Mapping[str, Any] | None = None,
    capture_log_paths: Mapping[str, str] | None = None,
    display_streams: bool = True,
    short_check: bool = False,
) -> Tuple[str, str, int]:
    """通用 Shell 命令执行器"""
    global _LAST_EXECUTION_METADATA
    start_time = time.time()
    start_monotonic = time.monotonic()
    started_at = timestamp_now()
    policy = EXECUTION_RUNTIME_POLICY
    health_interval = max(
        0.2,
        _env_float(
            policy.health_interval_env,
            policy.health_interval_default_seconds,
        ),
    )
    early_health_seconds = _env_float(
        policy.early_health_env,
        policy.early_health_default_seconds,
    )
    suspect_stall_seconds = _env_float(
        policy.suspect_stall_env,
        policy.suspect_stall_default_seconds,
    )
    stall_timeout = _env_float(
        policy.stall_timeout_env,
        policy.stall_timeout_default_seconds,
    )
    wall_timeout = float(timeout) if timeout else _env_float(
        policy.wall_timeout_env,
        policy.wall_timeout_default_seconds,
    )
    capture_limit = max(16_384, _env_int("BIOMIND_EXEC_MAX_CAPTURE_BYTES", 262_144))
    stream_output_limit = max(0, _env_int("BIOMIND_EXEC_MAX_STREAM_BYTES", 67_108_864))
    if max_output_bytes is not None:
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("EXECUTION_OUTPUT_LIMIT_INVALID")
        stream_output_limit = min(stream_output_limit or max_output_bytes, max_output_bytes)
    command_class = _watchdog_command_class(command)
    stdout_log_handle = None
    stderr_log_handle = None
    stdout_log_path = ""
    stderr_log_path = ""
    log_observation_errors = []
    display_scope = _execution_display_scope(display_context)
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
            env=(
                {str(key): str(value) for key, value in env.items()}
                if env is not None
                else None
            ),
            **popen_kwargs,
        )
        _ACTIVE_PROCESS_GROUPS.add(process.pid)
        display_scope["pid"] = process.pid
        try:
            pgid = os.getpgid(process.pid) if os.name != "nt" else process.pid
        except ProcessLookupError:
            # Very short commands may exit between spawn and bookkeeping.
            pgid = process.pid
        await _notify_lifecycle(lifecycle_callback, "RUNNING", {
            "pid": process.pid,
            "pgid": pgid,
            "started_at": started_at,
            "heartbeat_at": timestamp_now(),
        }, display_context=display_scope)

        stdout_capture = _BoundedStreamCapture(capture_limit)
        stderr_capture = _BoundedStreamCapture(capture_limit)
        if capture_log_paths is not None:
            # The caller owns a new execution-evidence directory. Exclusive
            # creation prevents a new candidate overwriting an earlier receipt.
            stdout_log_path = str(capture_log_paths["stdout"])
            stderr_log_path = str(capture_log_paths["stderr"])
        else:
            stdout_log_path, stderr_log_path = _execution_stream_log_paths(process.pid)
        if stdout_log_path:
            stdout_log_handle = open(stdout_log_path, "xb" if capture_log_paths is not None else "wb")
        if stderr_log_path:
            stderr_log_handle = open(stderr_log_path, "xb" if capture_log_paths is not None else "wb")
        progress = {"last_output_at": start_monotonic, "last_cpu_at": start_monotonic}
        # Start draining PIPEs immediately. Even a short read-only command can
        # fill the pipe before process bookkeeping finishes when records contain
        # very long lines.
        last_cpu_ticks = None
        output_limit_event = asyncio.Event()
        # A managed wrapper owns separate full logs. Display those actual
        # streams once; keep draining/capturing wrapper pipes unchanged.
        stdout_task = asyncio.create_task(
            _read_stream_chunks(
                process.stdout,
                stdout_capture,
                progress,
                stdout_log_handle,
                stream_name="stdout",
                output_limit=stream_output_limit,
                output_limit_event=output_limit_event,
                display_context=display_scope,
                display_enabled=display_streams and not (managed_log_paths and "stdout" in managed_log_paths),
            )
        )
        stderr_task = asyncio.create_task(
            _read_stream_chunks(
                process.stderr,
                stderr_capture,
                progress,
                stderr_log_handle,
                stream_name="stderr",
                output_limit=stream_output_limit,
                output_limit_event=output_limit_event,
                display_context=display_scope,
                display_enabled=display_streams and not (managed_log_paths and "stderr" in managed_log_paths),
            )
        )
        process_wait_task = asyncio.create_task(process.wait())
        output_limit_task = asyncio.create_task(output_limit_event.wait())
        pending_stream_tasks = {stdout_task, stderr_task}
        follow_task = None
        if managed_log_paths:
            follow_task = asyncio.create_task(_follow_managed_logs(
                dict(managed_log_paths), process,
                {"stdout": stdout_capture, "stderr": stderr_capture}, progress,
                {"stdout": stdout_log_handle, "stderr": stderr_log_handle},
                stream_output_limit, output_limit_event, log_observation_errors,
                display_context=display_scope))
            pending_stream_tasks.add(follow_task)
        stderr_reason = ""
        early_health_reported = False

        while True:
            watched_tasks = {process_wait_task, output_limit_task, *pending_stream_tasks}
            wait_timeout = health_interval if pending_stream_tasks else min(health_interval, 0.2)
            if wall_timeout > 0:
                wait_timeout = min(wait_timeout, max(0.0, wall_timeout - (time.monotonic() - start_monotonic)))
            done, _ = await asyncio.wait(
                watched_tasks,
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if output_limit_task in done and output_limit_event.is_set():
                stderr_reason = (
                    "OUTPUT_LIMIT: {stream} exceeded {limit} bytes (observed at least {observed}); "
                    "terminated by BioMind to prevent an unbounded terminal/state stream. Redirect "
                    "bulk data to a file and print only a bounded summary."
                ).format(
                    stream=progress.get("output_limit_stream") or "output",
                    limit=stream_output_limit,
                    observed=progress.get("output_limit_bytes") or stream_output_limit,
                )
                await (_terminate_process_tree(process, short_check=True) if short_check
                       else _terminate_process_tree(process))
                break
            if process_wait_task in done:
                await process_wait_task
                break
            pending_stream_tasks.difference_update(done)
            if not pending_stream_tasks and process.returncode is not None:
                # Python's subprocess transport can reap a process and set its
                # return code while an earlier wait() future remains pending
                # after a large PIPE drain. Trust the transport's return code
                # and cancel only that stale future.
                if not process_wait_task.done():
                    process_wait_task.cancel()
                break
            now = time.monotonic()
            await _notify_lifecycle(lifecycle_callback, "HEARTBEAT", {
                "pid": process.pid,
                "pgid": pgid,
                "heartbeat_at": timestamp_now(),
            }, display_context=display_scope)
            cpu_ticks = _collect_process_tree_cpu_ticks(process.pid)
            if cpu_ticks is not None and last_cpu_ticks is not None and cpu_ticks > last_cpu_ticks:
                progress["last_cpu_at"] = now
            if cpu_ticks is not None:
                last_cpu_ticks = cpu_ticks

            last_progress_at = max(progress["last_output_at"], progress["last_cpu_at"])
            idle_for = now - last_progress_at
            elapsed = now - start_monotonic
            if (
                not early_health_reported
                and early_health_seconds > 0
                and idle_for >= early_health_seconds
            ):
                logger.warning(
                    "Execution watchdog: command has shown no output or CPU progress for %.1f seconds "
                    "(class=%s, pid=%s).",
                    idle_for,
                    command_class or "compute_or_unknown",
                    process.pid,
                )
                early_health_reported = True
            if wall_timeout > 0 and now - start_monotonic >= wall_timeout:
                stderr_reason = f"TIMEOUT after {wall_timeout:g} seconds"
                await (_terminate_process_tree(process, short_check=True) if short_check
                       else _terminate_process_tree(process))
                break
            if (
                command_class
                and suspect_stall_seconds > 0
                and idle_for >= suspect_stall_seconds
            ):
                stderr_reason = (
                    "SUSPECT_STALL: a {kind} command showed no stdout/stderr output and no process-tree "
                    "CPU progress for {idle:.1f} seconds (elapsed {elapsed:.1f} seconds); terminated "
                    "by BioMind watchdog. Regenerate a bounded, local check or record a truthful blocked "
                    "state instead of retrying the same probe."
                ).format(kind=command_class, idle=idle_for, elapsed=elapsed)
                await (_terminate_process_tree(process, short_check=True) if short_check
                       else _terminate_process_tree(process))
                break
            if stall_timeout > 0 and now - last_progress_at >= stall_timeout:
                stderr_reason = (
                    "STALLED: command showed no stdout/stderr output and no process-tree CPU progress "
                    f"for {idle_for:.1f} seconds; terminated by BioMind watchdog."
                )
                await (_terminate_process_tree(process, short_check=True) if short_check
                       else _terminate_process_tree(process))
                break

        await asyncio.gather(stdout_task, stderr_task, *([follow_task] if follow_task else []), return_exceptions=True)
        stdout_text = stdout_capture.text("stdout", stdout_log_path)
        stderr_text = stderr_capture.text("stderr", stderr_log_path)
        if stderr_reason:
            stderr_text = "\n".join(part for part in [stderr_text.strip(), stderr_reason] if part)
            result = stdout_text, stderr_text, 124
        else:
            result = stdout_text, stderr_text, process.returncode
        return result
    except asyncio.CancelledError:
        cancellation_observed = True
        if "process" in locals():
            with contextlib.suppress(Exception):
                # Python 3.11+ keeps a cancellation count after this exception
                # is caught. Clear this one pending request just long enough to
                # reap the process group, then re-raise cancellation below.
                current_task = asyncio.current_task()
                uncancel = getattr(current_task, "uncancel", None)
                if callable(uncancel):
                    uncancel()
                await (_terminate_process_tree(process, short_check=True) if short_check
                       else _terminate_process_tree(process))
                reader_tasks = []
                for task_name in ("stdout_task", "stderr_task"):
                    task = locals().get(task_name)
                    if task is not None:
                        reader_tasks.append(task)
                if reader_tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*reader_tasks, return_exceptions=True),
                        timeout=2,
                    )
                wait_task = locals().get("process_wait_task")
                if wait_task is not None and not wait_task.done():
                    wait_task.cancel()
                limit_task = locals().get("output_limit_task")
                if limit_task is not None and not limit_task.done():
                    limit_task.cancel()
        raise
    except Exception as e:
        if "process" in locals():
            with contextlib.suppress(Exception):
                await (_terminate_process_tree(process, short_check=True) if short_check
                       else _terminate_process_tree(process))
        result = "", str(e), 1
        return result
    finally:
        for handle in (stdout_log_handle, stderr_log_handle):
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.flush()
                    handle.close()
        wait_task = locals().get("process_wait_task")
        if wait_task is not None and not wait_task.done():
            wait_task.cancel()
        limit_task = locals().get("output_limit_task")
        if limit_task is not None and not limit_task.done():
            limit_task.cancel()
        follower = locals().get("follow_task")
        if follower is not None and not follower.done():
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
        if "process" in locals():
            _ACTIVE_PROCESS_GROUPS.discard(process.pid)
        if locals().get("cancellation_observed"):
            cancelled_record = {"command": command, "status": "cancelled",
                "cancellation_source": "caller_unspecified", "started_at": started_at,
                "finished_at": timestamp_now(), "elapsed_seconds": time.monotonic() - start_monotonic,
                "pid": process.pid if "process" in locals() else None,
                "process_returncode": process.returncode if "process" in locals() else None,
                "process_reaped": bool("process" in locals() and process.returncode is not None),
                "log_observation_errors": log_observation_errors}
            if short_check:
                cancelled_record.update(_short_check_cleanup_facts(process.pid if "process" in locals() else None))
            with contextlib.suppress(Exception):
                await _notify_lifecycle(lifecycle_callback, "PROCESS_CANCELLED", cancelled_record,
                                        display_context=display_scope)
            logger.info("Execution cancelled: %s", cancelled_record)
        if locals().get("cancellation_observed"):
            _LAST_EXECUTION_METADATA = cancelled_record
        if "result" in locals():
            stdout_value, stderr_value, returncode_value = result
            try:
                command_argv = shlex.split(command, posix=True)
            except ValueError:
                command_argv = None
            _LAST_EXECUTION_METADATA = {
                "command": command,
                "argv": command_argv,
                "log_observation_errors": log_observation_errors,
                "effective_timeouts": {"wall_seconds": wall_timeout, "wall_source": "argument" if timeout else policy.wall_timeout_env,
                                       "stall_seconds": stall_timeout, "stall_source": policy.stall_timeout_env},
                "pid": process.pid if "process" in locals() else None,
                "returncode": returncode_value,
                "process_returncode": process.returncode if "process" in locals() else None,
                "observed_signal": (-process.returncode if "process" in locals() and process.returncode is not None and process.returncode < 0 else None),
                "termination_reason": locals().get("stderr_reason", ""),
                "process_reaped": bool("process" in locals() and process.returncode is not None),
                "elapsed_seconds": time.monotonic() - start_monotonic,
                "started_at": started_at,
                "finished_at": timestamp_now(),
                "stdout_bytes": (
                    stdout_capture.total_bytes if "stdout_capture" in locals()
                    else len(stdout_value.encode(errors="replace"))
                ),
                "stderr_bytes": (
                    stderr_capture.total_bytes if "stderr_capture" in locals()
                    else len(stderr_value.encode(errors="replace"))
                ),
                "stdout_projected_bytes": len(stdout_value.encode(errors="replace")),
                "stderr_projected_bytes": len(stderr_value.encode(errors="replace")),
                "stdout_truncated": bool(
                    stdout_capture.truncated if "stdout_capture" in locals() else False
                ),
                "stderr_truncated": bool(
                    stderr_capture.truncated if "stderr_capture" in locals() else False
                ),
                "stdout_log": stdout_log_path,
                "stderr_log": stderr_log_path,
                "stream_output_limit_bytes": stream_output_limit,
            }
            if short_check:
                _LAST_EXECUTION_METADATA.update(_short_check_cleanup_facts(process.pid if "process" in locals() else None))
            with contextlib.suppress(Exception):
                await _notify_lifecycle(lifecycle_callback, "PROCESS_EXITED", {
                    "pid": process.pid if "process" in locals() else None,
                    "pgid": pgid if "pgid" in locals() else None,
                    "returncode": returncode_value,
                    "finished_at": timestamp_now(),
                    "heartbeat_at": timestamp_now(),
                    "stdout_path": stdout_log_path,
                    "stderr_path": stderr_log_path,
                }, display_context=display_scope)
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


async def run_script_path(
    script_path: str | Path,
    *,
    expected_sha256: str,
    interpreter: str = "",
    timeout: int | None = None,
    profile: bool = True,
    lifecycle_callback: Optional[Callable[..., Any]] = None,
    exit_status_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int | None = None,
    managed_log_paths: Mapping[str, str] | None = None,
    display_context: Mapping[str, Any] | None = None,
) -> Tuple[str, str, int]:
    """Execute one immutable saved script; never materialize a second script."""
    global _LAST_EXECUTION_METADATA
    path = Path(script_path).expanduser().resolve()
    if not path.is_file():
        return "", f"EXECUTED_SCRIPT_MISSING: {path}", 126
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if not expected_sha256 or actual_sha256 != str(expected_sha256):
        return "", (
            f"SCRIPT_HASH_MISMATCH: path={path} expected={expected_sha256 or '<missing>'} "
            f"actual={actual_sha256}"
        ), 126
    runner = str(interpreter or "").strip()
    if not runner:
        runner = "python" if path.suffix.lower() == ".py" else "bash"
    command = f"{shlex.quote(runner)} {shlex.quote(str(path))}"
    stdout, stderr, returncode = await run_command(
        command,
        timeout=timeout,
        profile=profile,
        lifecycle_callback=lifecycle_callback,
        env=env,
        max_output_bytes=max_output_bytes,
        managed_log_paths=managed_log_paths,
        display_context=display_context,
    )
    metadata = dict(get_last_execution_metadata())
    metadata.update({
        "executed_script_path": str(path),
        "executed_script_sha256": actual_sha256,
        "interpreter": runner,
    })
    _LAST_EXECUTION_METADATA = metadata
    if exit_status_path:
        status_path = Path(exit_status_path)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "script_path": str(path),
            "script_sha256": actual_sha256,
            "returncode": returncode,
            "finished_at": timestamp_now(),
            "stdout_path": str(metadata.get("stdout_log") or ""),
            "stderr_path": str(metadata.get("stderr_log") or ""),
        }
        tmp = status_path.with_name(f".{status_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, status_path)
    return stdout, stderr, returncode

async def run_bash_script(
    code: str,
    timeout: int = None,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int | None = None,
    managed_log_paths: Mapping[str, str] | None = None,
) -> Tuple[str, str, int]:
    """Compatibility API using a unique temporary script, never a fixed filename."""
    start_time = time.time()
    started_at = timestamp_now()
    try:
        safety_error = shell_safety_error(code, "Bash")
        if safety_error:
            return "", safety_error, 126

        fd, filename = tempfile.mkstemp(prefix="biomind-bash-", suffix=".sh")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if not code.strip().startswith("#!"):
                f.write(BASH_SCRIPT_PROLOGUE)
            f.write(code)
        
        # 赋予执行权限
        os.chmod(filename, 0o755)
        
        # 执行时使用 run_command 的健康检查；timeout 作为可选硬性 wall limit。
        digest = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        stdout, stderr, returncode = await run_script_path(
            filename,
            expected_sha256=digest,
            interpreter="bash",
            timeout=timeout,
            profile=False,
            env=env,
            max_output_bytes=max_output_bytes,
        )
        return stdout, stderr, returncode
    except Exception as e:
        stdout, stderr, returncode = "", str(e), 1
        return stdout, stderr, returncode
    finally:
        if "filename" in locals():
            with contextlib.suppress(OSError):
                os.unlink(filename)
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
    script_path = ""
    try:
        fd, script_path = tempfile.mkstemp(prefix="biomind-r-", suffix=".R")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(code)
        stdout, stderr, returncode = await run_command(
            f"Rscript {shlex.quote(script_path)}", timeout, profile=False
        )
        return stdout, stderr, returncode
    except Exception as e:
        stdout, stderr, returncode = "", str(e), 1
        return stdout, stderr, returncode
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except OSError:
                pass
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

"""Human input helpers for long-running SSH sessions.

The agent should not occupy the foreground terminal forever while waiting for
approval. These helpers add timeout/EOF handling and persist the waiting state
so a later session can resume with context.
"""

from __future__ import annotations

import json
import locale
import os
import glob
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from agent_core.cli_display import interactive_terminal as active_terminal, input_terminal


class HumanInputUnavailable(Exception):
    """Base class for input that cannot be collected now."""


class HumanInputTimeout(HumanInputUnavailable):
    """Raised when no human input arrives before the configured timeout."""


class HumanInputEOF(HumanInputUnavailable):
    """Raised when stdin is closed, which is common after SSH disconnects."""


class HumanInputDecodeError(HumanInputUnavailable):
    """Raised when terminal input cannot be decoded safely."""


_READLINE_CONFIGURED = False


def timestamp_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def resolve_input_timeout(default: int = 900) -> int:
    """Resolve timeout seconds from BIOMIND_INPUT_TIMEOUT_SECONDS.

    Set BIOMIND_INPUT_TIMEOUT_SECONDS=0 to disable timeout and use regular
    blocking input.
    """

    raw = os.getenv("BIOMIND_INPUT_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return max(0, int(default))
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return max(0, int(default))


def resolve_paste_grace_seconds(default: float = 0.08) -> float:
    """Resolve the short stdin-drain window used after a terminal paste."""

    raw = os.getenv("BIOMIND_PASTE_GRACE_SECONDS", "").strip()
    if not raw:
        return max(0.0, float(default))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return max(0.0, float(default))


def resolve_paste_max_lines(default: int = 2000) -> int:
    """Resolve the maximum number of continuation lines to absorb from paste."""

    raw = os.getenv("BIOMIND_PASTE_MAX_LINES", "").strip()
    if not raw:
        return max(1, int(default))
    try:
        return max(1, int(float(raw)))
    except ValueError:
        return max(1, int(default))


def _input_encodings() -> list[str]:
    candidates = [
        getattr(sys.stdin, "encoding", None),
        locale.getpreferredencoding(False),
        sys.getfilesystemencoding(),
        "utf-8",
        "gb18030",
        "gbk",
        "latin-1",
    ]
    seen = set()
    encodings: list[str] = []
    for encoding in candidates:
        if not encoding:
            continue
        key = str(encoding).lower()
        if key in seen:
            continue
        seen.add(key)
        encodings.append(str(encoding))
    return encodings


def decode_terminal_bytes(raw: bytes) -> str:
    """Decode one terminal input line with UTF-8 and Windows Chinese fallbacks."""

    raw = raw.rstrip(b"\n")
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    for encoding in _input_encodings():
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        except LookupError:
            continue
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise HumanInputDecodeError("terminal input could not be decoded") from exc


def repair_terminal_text(text: str) -> str:
    """Repair mojibake caused by UTF-8 TextIO with surrogateescaped GBK bytes."""

    if not any("\udc80" <= char <= "\udcff" for char in text):
        return text
    for source_encoding in _input_encodings():
        try:
            raw = text.encode(source_encoding, errors="surrogateescape")
        except UnicodeEncodeError:
            continue
        except LookupError:
            continue
        repaired = decode_terminal_bytes(raw)
        if not any("\udc80" <= char <= "\udcff" for char in repaired):
            return repaired
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _configure_stdin_decoding() -> None:
    """Avoid UnicodeDecodeError in input() while preserving original bytes."""

    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors="surrogateescape")
        except Exception:
            pass


def _configure_readline() -> None:
    """Enable line editing and basic path completion for agent prompts."""

    global _READLINE_CONFIGURED
    if _READLINE_CONFIGURED or os.name != "posix":
        return
    try:
        import readline  # type: ignore
    except Exception:
        _READLINE_CONFIGURED = True
        return

    def complete_path(text: str, state: int):
        expanded = os.path.expanduser(text or "")
        matches = glob.glob(expanded + "*")
        formatted = []
        for item in matches:
            display = os.path.expanduser(item)
            if os.path.isdir(display):
                formatted.append(item + "/")
            else:
                formatted.append(item + " ")
        try:
            return formatted[state]
        except IndexError:
            return None

    try:
        readline.set_completer(complete_path)
        readline.parse_and_bind("set editing-mode emacs")
        readline.parse_and_bind("set enable-bracketed-paste on")
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass
    _READLINE_CONFIGURED = True


def _snapshot_readline_history(readline_module: Any) -> list[str]:
    try:
        length = int(readline_module.get_current_history_length())
    except Exception:
        return []
    history: list[str] = []
    for idx in range(1, length + 1):
        try:
            item = readline_module.get_history_item(idx)
        except Exception:
            item = None
        if item is not None:
            history.append(str(item))
    return history


def _clear_readline_history(readline_module: Any) -> None:
    clear_history = getattr(readline_module, "clear_history", None)
    if callable(clear_history):
        clear_history()
        return

    remove_history_item = getattr(readline_module, "remove_history_item", None)
    if not callable(remove_history_item):
        return
    try:
        length = int(readline_module.get_current_history_length())
    except Exception:
        return
    for idx in range(length - 1, -1, -1):
        try:
            remove_history_item(idx)
        except Exception:
            pass


@contextmanager
def _readline_history_scope(enabled: bool) -> Iterator[None]:
    """Temporarily hide readline history for one-off answer prompts."""

    if enabled or os.name != "posix":
        yield
        return

    try:
        import readline  # type: ignore
    except Exception:
        yield
        return

    saved_history = _snapshot_readline_history(readline)
    try:
        _clear_readline_history(readline)
        yield
    finally:
        try:
            _clear_readline_history(readline)
            for item in saved_history:
                readline.add_history(item)
        except Exception:
            pass


def _readline_input(prompt: str, *, history: bool = True) -> str:
    _configure_stdin_decoding()
    _configure_readline()
    try:
        with _readline_history_scope(history):
            return repair_terminal_text(input(prompt))
    except EOFError as exc:
        raise HumanInputEOF("stdin closed while waiting for input") from exc
    except UnicodeDecodeError as exc:
        raise HumanInputDecodeError("terminal input could not be decoded") from exc


def _can_timeout_readline() -> bool:
    return (
        os.name == "posix"
        and sys.stdin.isatty()
        and threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
    )


def _readline_input_with_timeout(prompt: str, timeout_seconds: int, *, history: bool = True) -> str:
    """Read through readline while interrupting idle waits on POSIX.

    A select-before-input approach keeps the terminal outside readline while
    the user edits a line, so arrow keys appear as ``^[[D`` and Tab completion
    is unavailable. Keeping ``input()`` active and using SIGALRM preserves
    normal terminal line editing.
    """

    if timeout_seconds <= 0 or not _can_timeout_readline():
        return _readline_input(prompt, history=history)

    def _timeout_handler(signum, frame):
        raise HumanInputTimeout(f"no input after {timeout_seconds} seconds")

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, float(timeout_seconds))
        return _readline_input(prompt, history=history)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])


def _read_terminal_line_text() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        raw = buffer.readline()
        if raw == b"":
            raise HumanInputEOF("stdin closed while waiting for input")
        return decode_terminal_bytes(raw)

    try:
        line = sys.stdin.readline()
    except UnicodeDecodeError as exc:
        raise HumanInputDecodeError("terminal input could not be decoded") from exc
    if line == "":
        raise HumanInputEOF("stdin closed while waiting for input")
    return line.rstrip("\n")


def _can_read_pending_terminal_lines() -> bool:
    return (
        os.name == "posix"
        and sys.stdin.isatty()
        and threading.current_thread() is threading.main_thread()
    )


def read_pending_paste_lines(
    *,
    grace_seconds: Optional[float] = None,
    max_lines: Optional[int] = None,
    owned_read: bool = False,
) -> list[str]:
    """Drain complete lines that are already pending after a multiline paste.

    ``input()`` returns at the first newline, so a pasted task can otherwise be
    split into several prompts. This function only runs on POSIX TTYs and uses
    ``select`` with a tiny timeout so normal interactive typing remains single
    line.
    """
    if active_terminal() is not None and not owned_read:
        return []

    timeout = resolve_paste_grace_seconds() if grace_seconds is None else max(0.0, float(grace_seconds))
    limit = resolve_paste_max_lines() if max_lines is None else max(1, int(max_lines))
    if timeout <= 0 or not (_can_read_pending_terminal_lines()
                            or (owned_read and os.name == 'posix' and sys.stdin.isatty())):
        return []

    try:
        import select
    except Exception:
        return []

    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while len(lines) < limit:
        wait_seconds = max(0.0, deadline - time.monotonic())
        try:
            ready, _, _ = select.select([sys.stdin], [], [], wait_seconds)
        except Exception:
            break
        if not ready:
            break
        try:
            if owned_read:
                # The owner reads one canonical TTY record directly. Avoid a
                # TextIO prefetch hiding another pasted line from select.
                raw_line = os.read(sys.stdin.fileno(), 65536)
                if not raw_line:
                    break
                lines.append(decode_terminal_bytes(raw_line).rstrip('\n'))
            else:
                lines.append(_read_terminal_line_text())
        except HumanInputUnavailable:
            break
        deadline = time.monotonic() + min(timeout, 0.02)
    return lines


@contextmanager
def terminal_line_framing():
    """Preserve CR/LF distinctions while the single plain reader owns the TTY.

    Canonical editing/echo stay enabled. CR is an explicit record delimiter;
    the reader normalizes CRLF as one ending. TCSANOW never flushes input.
    """
    saved = None
    try:
        if sys.stdin.isatty():
            import termios
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            current = list(saved)
            current[6] = list(saved[6])
            current[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
            current[6][termios.VEOL] = b'\r'
            termios.tcsetattr(fd, termios.TCSANOW, current)
        yield
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except (OSError, termios.error):
                pass  # A disconnected terminal cannot be restored.


def safe_input(prompt: str, timeout_seconds: Optional[int] = None, *, history: bool = True) -> str:
    """Read one line from stdin with POSIX timeout support.

    On POSIX TTYs in the main thread this uses SIGALRM around readline(), so an
    SSH-disconnected session does not block forever and line editing still works.
    On unsupported consoles it falls back to builtin input().
    Set ``history=False`` for transient answers so Up does not recall an old
    response into the current prompt.
    """

    timeout = resolve_input_timeout() if timeout_seconds is None else int(timeout_seconds)
    if input_terminal() is not None:
        return input_terminal().read(prompt, timeout=timeout)
    if os.getenv("BIOMIND_UI", "").lower() == "plain" or os.getenv("TERM") == "dumb":
        from agent_core.cli_terminal import plain_input
        return plain_input(prompt, timeout)
    return _readline_input_with_timeout(prompt, timeout, history=history)


def safe_multiline_input(
    prompt: str,
    timeout_seconds: Optional[int] = None,
    *,
    terminator: str = "/end",
    history: bool = False,
) -> str:
    """Read a multiline block until a line exactly matching ``terminator``."""

    timeout = resolve_input_timeout() if timeout_seconds is None else int(timeout_seconds)
    if input_terminal() is not None:
        return input_terminal().read(prompt, timeout=timeout, multiline=True)
    end_marker = str(terminator or "/end").strip()
    lines: list[str] = []
    first_prompt = prompt
    while True:
        line = safe_input(
            first_prompt if not lines else "... ",
            timeout,
            history=history and not lines,
        )
        if line.strip() == end_marker:
            break
        lines.append(line)
        first_prompt = ""
    return "\n".join(lines)


def task_output_dir(data_path: str | Path, task_id: str) -> Path:
    return Path(data_path) / "task_outputs" / str(task_id or "default")


def waiting_state_path(data_path: str | Path, task_id: str) -> Path:
    return task_output_dir(data_path, task_id) / "waiting_state.json"


def save_waiting_state(
    data_path: str | Path,
    task_id: str,
    *,
    stage: str,
    prompt: str,
    payload: Optional[Dict[str, Any]] = None,
    reason: str = "timeout",
    timeout_seconds: Optional[int] = None,
    started_at: Optional[str] = None,
) -> Path:
    path = waiting_state_path(data_path, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": str(task_id or "default"),
        "stage": str(stage or "human_input"),
        "prompt": str(prompt or ""),
        "payload": payload or {},
        "reason": str(reason or "timeout"),
        "timeout_seconds": timeout_seconds,
        "started_at": started_at or timestamp_now(),
        "updated_at": timestamp_now(),
        "status": "waiting_user",
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_waiting_state(data_path: str | Path, task_id: str) -> Dict[str, Any]:
    path = waiting_state_path(data_path, task_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_waiting_state(data_path: str | Path, task_id: str) -> None:
    try:
        waiting_state_path(data_path, task_id).unlink()
    except FileNotFoundError:
        pass


def restore_terminal_state() -> None:
    """Best-effort terminal restore for interrupted/suspended POSIX sessions."""
    if active_terminal() is not None:
        return None

    if os.name != "posix":
        return
    try:
        os.system("stty sane 2>/dev/null || stty echo 2>/dev/null")
    except Exception:
        pass

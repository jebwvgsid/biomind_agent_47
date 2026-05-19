from __future__ import annotations

import contextlib
import contextvars
import json
import time
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - only used when LangChain is unavailable
    class BaseCallbackHandler:  # type: ignore[no-redef]
        pass


_CURRENT_PROFILER: contextvars.ContextVar[Optional["RunProfiler"]] = contextvars.ContextVar(
    "biomind_current_profiler",
    default=None,
)
_CURRENT_TASK_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "biomind_current_task_id",
    default=None,
)
_CURRENT_NODE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "biomind_current_node",
    default=None,
)


def get_current_profiler() -> Optional["RunProfiler"]:
    return _CURRENT_PROFILER.get()


def get_current_task_id() -> Optional[str]:
    return _CURRENT_TASK_ID.get()


def get_current_node() -> Optional[str]:
    return _CURRENT_NODE.get()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def timestamp_now() -> str:
    return _now_iso()


def _estimate_tokens(text: str) -> int:
    text = text or ""
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return max(1, len(text) // 4)


def _message_text(message: Any) -> str:
    if hasattr(message, "content"):
        return str(message.content)
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(message)


def _messages_text(messages: Any) -> str:
    if not messages:
        return ""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, Iterable):
        parts: List[str] = []
        for item in messages:
            if isinstance(item, Iterable) and not isinstance(item, (str, bytes, dict)) and not hasattr(item, "content"):
                parts.append(_messages_text(item))
            else:
                parts.append(_message_text(item))
        return "\n".join(parts)
    return str(messages)


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _normalize_usage(data: Any) -> Dict[str, Optional[int]]:
    if not isinstance(data, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    prompt = (
        _as_int(data.get("prompt_tokens"))
        or _as_int(data.get("input_tokens"))
        or _as_int(data.get("prompt_token_count"))
    )
    completion = (
        _as_int(data.get("completion_tokens"))
        or _as_int(data.get("output_tokens"))
        or _as_int(data.get("completion_token_count"))
    )
    total = _as_int(data.get("total_tokens")) or _as_int(data.get("total_token_count"))
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _find_usage_dict(value: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    if depth > 4:
        return None
    if isinstance(value, dict):
        normalized = _normalize_usage(value)
        if normalized["total_tokens"] is not None or normalized["prompt_tokens"] is not None:
            return value
        for key in ("token_usage", "usage", "usage_metadata", "response_metadata", "llm_output"):
            nested = value.get(key)
            found = _find_usage_dict(nested, depth + 1)
            if found:
                return found
        for nested in value.values():
            found = _find_usage_dict(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_usage_dict(item, depth + 1)
            if found:
                return found
    return None


def _response_text(response: Any) -> str:
    texts: List[str] = []
    for generation_list in getattr(response, "generations", []) or []:
        for generation in generation_list or []:
            message = getattr(generation, "message", None)
            if message is not None and hasattr(message, "content"):
                texts.append(str(message.content))
            elif hasattr(generation, "text"):
                texts.append(str(generation.text))
    return "\n".join(texts)


def _response_usage(response: Any) -> Dict[str, Optional[int]]:
    candidates: List[Any] = [getattr(response, "llm_output", None)]
    for generation_list in getattr(response, "generations", []) or []:
        for generation in generation_list or []:
            candidates.extend(
                [
                    getattr(generation, "generation_info", None),
                    getattr(getattr(generation, "message", None), "usage_metadata", None),
                    getattr(getattr(generation, "message", None), "response_metadata", None),
                    getattr(getattr(generation, "message", None), "additional_kwargs", None),
                ]
            )
    for candidate in candidates:
        found = _find_usage_dict(candidate)
        normalized = _normalize_usage(found)
        if normalized["total_tokens"] is not None or normalized["prompt_tokens"] is not None:
            return normalized
    return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}


def _shorten(text: Any, limit: int = 500) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n")
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [truncated {len(value) - limit} chars]"


class RunProfiler:
    """Collects per-task wall time, LLM usage, human wait, and command timings."""

    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def start_task(self, task_id: str, status: str = "running") -> None:
        task = self._tasks.get(task_id)
        if task:
            task["status"] = status
            if status == "running":
                task["ended_at"] = None
                task.pop("_ended_wall_seconds", None)
            task["last_updated_at"] = _now_iso()
            return
        self._tasks[task_id] = {
            "task_id": task_id,
            "status": status,
            "started_at": _now_iso(),
            "_started_monotonic": time.perf_counter(),
            "last_updated_at": _now_iso(),
            "ended_at": None,
            "events": [],
        }

    def finish_task(self, task_id: str, status: str = "completed") -> None:
        task = self._ensure_task(task_id)
        task["status"] = status
        task["ended_at"] = _now_iso()
        task["last_updated_at"] = task["ended_at"]
        task["_ended_wall_seconds"] = round(time.perf_counter() - task["_started_monotonic"], 3)
        self.flush(task_id)

    @contextlib.contextmanager
    def task_context(self, task_id: str):
        self.start_task(task_id)
        profiler_token = _CURRENT_PROFILER.set(self)
        task_token = _CURRENT_TASK_ID.set(task_id)
        try:
            yield
        finally:
            _CURRENT_TASK_ID.reset(task_token)
            _CURRENT_PROFILER.reset(profiler_token)

    @contextlib.contextmanager
    def node(self, task_id: str, node_name: str):
        self.start_task(task_id)
        profiler_token = _CURRENT_PROFILER.set(self)
        task_token = _CURRENT_TASK_ID.set(task_id)
        node_token = _CURRENT_NODE.set(node_name)
        start = time.perf_counter()
        started_at = _now_iso()
        event_id = self._new_event_id("node")
        error = None
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self._append_event(
                task_id,
                {
                    "id": event_id,
                    "type": "node",
                    "node": node_name,
                    "started_at": started_at,
                    "duration_seconds": round(time.perf_counter() - start, 3),
                    "error": error,
                },
            )
            _CURRENT_NODE.reset(node_token)
            _CURRENT_TASK_ID.reset(task_token)
            _CURRENT_PROFILER.reset(profiler_token)

    def record_llm(
        self,
        task_id: str,
        node: Optional[str],
        duration_seconds: float,
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        total_tokens: Optional[int],
        estimated: bool,
        model: str = "",
        error: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> None:
        self._append_event(
            task_id,
            {
                "id": self._new_event_id("llm"),
                "type": "llm",
                "node": node,
                "model": model,
                "started_at": started_at or _now_iso(),
                "duration_seconds": round(duration_seconds, 3),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "tokens_estimated": estimated,
                "error": error,
            },
        )

    def record_command(
        self,
        task_id: str,
        language: str,
        code: str,
        stdout: str,
        stderr: str,
        returncode: int,
        duration_seconds: float,
        node: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> None:
        self._append_event(
            task_id,
            {
                "id": self._new_event_id("cmd"),
                "type": "command",
                "node": node or get_current_node(),
                "language": language,
                "started_at": started_at or _now_iso(),
                "duration_seconds": round(duration_seconds, 3),
                "returncode": returncode,
                "success": returncode == 0,
                "code": _shorten(code, 1200),
                "stdout_preview": _shorten(stdout, 800),
                "stderr_preview": _shorten(stderr, 800),
            },
        )

    def record_confirmation(
        self,
        task_id: str,
        prompt_type: str,
        response: Any = "",
        duration_seconds: float = 0.0,
        node: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        automatic: bool = False,
        started_at: Optional[str] = None,
    ) -> None:
        payload = payload or {}
        content = payload.get("content") or payload.get("msg") or payload.get("reason") or ""
        risk = payload.get("risk", {})
        if isinstance(risk, dict):
            risk_level = risk.get("level") or risk.get("risk_level")
            risk_score = risk.get("score") or risk.get("risk_score")
        else:
            risk_level = str(risk) if risk else ""
            risk_score = None
        self._append_event(
            task_id,
            {
                "id": self._new_event_id("confirm"),
                "type": "confirmation",
                "node": node or get_current_node(),
                "prompt_type": prompt_type,
                "started_at": started_at or _now_iso(),
                "duration_seconds": round(max(0.0, duration_seconds), 3),
                "automatic": automatic,
                "approved": self._is_approval_response(response, automatic),
                "response": _shorten(response, 200),
                "risk_level": risk_level,
                "risk_score": risk_score,
                "content_preview": _shorten(content, 800),
            },
        )

    def flush(self, task_id: str) -> Optional[Path]:
        if task_id not in self._tasks:
            return None
        out_dir = self.output_root / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot(task_id)
        json_path = out_dir / "run_metrics.json"
        md_path = out_dir / "run_metrics.md"
        json_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(self.render_markdown(snapshot), encoding="utf-8")
        return json_path

    def snapshot(self, task_id: str) -> Dict[str, Any]:
        task = self._ensure_task(task_id)
        events = list(task["events"])
        wall = round(time.perf_counter() - task["_started_monotonic"], 3)
        if task.get("ended_at"):
            wall = task.get("_ended_wall_seconds", wall)

        command_events = [e for e in events if e.get("type") == "command"]
        human_events = [e for e in events if e.get("type") == "confirmation" and not e.get("automatic")]
        llm_events = [e for e in events if e.get("type") == "llm"]
        node_events = [e for e in events if e.get("type") == "node"]

        command_total = sum(float(e.get("duration_seconds") or 0) for e in command_events)
        command_success = sum(
            float(e.get("duration_seconds") or 0)
            for e in command_events
            if e.get("success")
        )
        human_total = sum(float(e.get("duration_seconds") or 0) for e in human_events)
        llm_total = sum(float(e.get("duration_seconds") or 0) for e in llm_events)
        install_repair = self._install_repair_seconds(events)

        node_totals: Dict[str, float] = {}
        for event in node_events:
            node = event.get("node") or "unknown"
            node_totals[node] = round(node_totals.get(node, 0.0) + float(event.get("duration_seconds") or 0), 3)

        token_totals = {
            "prompt_tokens": sum(int(e.get("prompt_tokens") or 0) for e in llm_events),
            "completion_tokens": sum(int(e.get("completion_tokens") or 0) for e in llm_events),
            "total_tokens": sum(int(e.get("total_tokens") or 0) for e in llm_events),
            "contains_estimates": any(e.get("tokens_estimated") for e in llm_events),
        }

        summary = {
            "total_wall_seconds": wall,
            "agent_active_seconds_excluding_human_wait": round(max(0.0, wall - human_total), 3),
            "human_confirmation_wait_seconds": round(human_total, 3),
            "command_execution_seconds": round(command_total, 3),
            "successful_command_execution_seconds": round(command_success, 3),
            "llm_call_seconds": round(llm_total, 3),
            "install_or_repair_seconds_estimate": round(install_repair, 3),
            "analysis_planning_overhead_seconds": round(max(0.0, wall - command_total - human_total), 3),
            "llm_calls": len(llm_events),
            "commands": len(command_events),
            "successful_commands": sum(1 for e in command_events if e.get("success")),
            "failed_commands": sum(1 for e in command_events if not e.get("success")),
            "manual_confirmations": len(human_events),
            "auto_confirmations": sum(1 for e in events if e.get("type") == "confirmation" and e.get("automatic")),
            "node_seconds": node_totals,
            "tokens": token_totals,
        }

        return {
            "task_id": task_id,
            "status": task.get("status", "running"),
            "started_at": task.get("started_at"),
            "last_updated_at": _now_iso(),
            "ended_at": task.get("ended_at"),
            "summary": summary,
            "events": events,
        }

    def render_markdown(self, snapshot: Dict[str, Any]) -> str:
        summary = snapshot["summary"]
        token_note = "yes" if summary["tokens"].get("contains_estimates") else "no"
        lines = [
            f"# Run Metrics: {snapshot['task_id']}",
            "",
            f"- Status: {snapshot.get('status')}",
            f"- Started: {snapshot.get('started_at')}",
            f"- Updated: {snapshot.get('last_updated_at')}",
            f"- Ended: {snapshot.get('ended_at') or 'running'}",
            "",
            "## Summary",
            "",
            "| Type | Seconds | Human time |",
            "| --- | ---: | --- |",
            f"| Total wall time | {summary['total_wall_seconds']:.3f} | {self._format_seconds(summary['total_wall_seconds'])} |",
            f"| Human confirmation wait | {summary['human_confirmation_wait_seconds']:.3f} | {self._format_seconds(summary['human_confirmation_wait_seconds'])} |",
            f"| Command execution | {summary['command_execution_seconds']:.3f} | {self._format_seconds(summary['command_execution_seconds'])} |",
            f"| Successful command execution | {summary['successful_command_execution_seconds']:.3f} | {self._format_seconds(summary['successful_command_execution_seconds'])} |",
            f"| LLM calls | {summary['llm_call_seconds']:.3f} | {self._format_seconds(summary['llm_call_seconds'])} |",
            f"| Analysis/planning/overhead | {summary['analysis_planning_overhead_seconds']:.3f} | {self._format_seconds(summary['analysis_planning_overhead_seconds'])} |",
            f"| Install or repair estimate | {summary['install_or_repair_seconds_estimate']:.3f} | {self._format_seconds(summary['install_or_repair_seconds_estimate'])} |",
            "",
            "## Counts",
            "",
            f"- LLM calls: {summary['llm_calls']}",
            f"- Commands: {summary['commands']} ({summary['successful_commands']} success, {summary['failed_commands']} failed)",
            f"- Confirmations: {summary['manual_confirmations']} manual, {summary['auto_confirmations']} automatic",
            f"- Tokens: prompt={summary['tokens']['prompt_tokens']}, completion={summary['tokens']['completion_tokens']}, total={summary['tokens']['total_tokens']} (contains estimates: {token_note})",
            "",
            "## Node Seconds",
            "",
        ]
        if summary["node_seconds"]:
            for node, seconds in sorted(summary["node_seconds"].items()):
                lines.append(f"- {node}: {seconds:.3f}s ({self._format_seconds(seconds)})")
        else:
            lines.append("- none recorded")

        lines.extend(["", "## Events", ""])
        for event in snapshot["events"]:
            event_type = event.get("type")
            duration = float(event.get("duration_seconds") or 0)
            node = event.get("node") or "-"
            if event_type == "command":
                status = "ok" if event.get("success") else f"failed rc={event.get('returncode')}"
                code_preview = _shorten(event.get("code", ""), 160).replace("\n", " ; ")
                lines.append(f"- command [{node}] {duration:.3f}s {status}: `{code_preview}`")
            elif event_type == "confirmation":
                mode = "auto" if event.get("automatic") else "manual"
                lines.append(f"- confirmation [{node}] {duration:.3f}s {mode} {event.get('prompt_type')}: {event.get('response')!r}")
            elif event_type == "llm":
                total_tokens = event.get("total_tokens")
                token_text = f", tokens={total_tokens}" if total_tokens is not None else ""
                lines.append(f"- llm [{node}] {duration:.3f}s{token_text}")
            elif event_type == "node":
                err = f", error={event.get('error')}" if event.get("error") else ""
                lines.append(f"- node [{node}] {duration:.3f}s{err}")
        lines.append("")
        return "\n".join(lines)

    def _append_event(self, task_id: str, event: Dict[str, Any]) -> None:
        task = self._ensure_task(task_id)
        task["events"].append(event)
        task["last_updated_at"] = _now_iso()

    def _ensure_task(self, task_id: str) -> Dict[str, Any]:
        if task_id not in self._tasks:
            self.start_task(task_id)
        return self._tasks[task_id]

    def _new_event_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def _install_repair_seconds(self, events: List[Dict[str, Any]]) -> float:
        total = 0.0
        seen: set[str] = set()
        repair_patterns = ("conda create", "conda install", "pip install", "install", "register_environment")
        for event in events:
            event_id = event.get("id")
            node = str(event.get("node") or "")
            text = str(event.get("code") or event.get("content_preview") or "").lower()
            is_repair = node == "install_subagent" or any(pattern in text for pattern in repair_patterns)
            if is_repair and event_id not in seen:
                total += float(event.get("duration_seconds") or 0)
                seen.add(event_id)
        return total

    def _is_approval_response(self, response: Any, automatic: bool) -> bool:
        if automatic:
            return True
        text = str(response).strip().lower()
        return text in {"", "y", "yes", "approve", "ok"}

    def _format_seconds(self, seconds: float) -> str:
        seconds = max(0.0, float(seconds or 0))
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(int(minutes), 60)
        if hours:
            return f"{hours}h {minutes}m {sec:.1f}s"
        if minutes:
            return f"{minutes}m {sec:.1f}s"
        return f"{sec:.1f}s"


class ProfilingCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that records LLM timing and token usage."""

    def __init__(self, profiler: RunProfiler):
        super().__init__()
        self.profiler = profiler
        self._starts: Dict[str, Dict[str, Any]] = {}

    def on_chat_model_start(self, serialized: Dict[str, Any], messages: List[List[Any]], run_id: Any, **kwargs: Any) -> None:
        self._record_start(run_id, serialized, messages, kwargs)

    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], run_id: Any, **kwargs: Any) -> None:
        self._record_start(run_id, serialized, prompts, kwargs)

    def on_llm_end(self, response: Any, run_id: Any, **kwargs: Any) -> None:
        start = self._starts.pop(str(run_id), None)
        if not start:
            return
        usage = _response_usage(response)
        output_tokens_estimate = _estimate_tokens(_response_text(response))
        prompt_tokens = usage["prompt_tokens"] if usage["prompt_tokens"] is not None else start["prompt_tokens_estimate"]
        completion_tokens = usage["completion_tokens"] if usage["completion_tokens"] is not None else output_tokens_estimate
        total_tokens = usage["total_tokens"] if usage["total_tokens"] is not None else prompt_tokens + completion_tokens
        estimated = (
            usage["prompt_tokens"] is None
            or usage["completion_tokens"] is None
            or usage["total_tokens"] is None
        )
        self.profiler.record_llm(
            task_id=start["task_id"],
            node=start.get("node"),
            model=start.get("model", ""),
            duration_seconds=time.perf_counter() - start["started_monotonic"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated=estimated,
            started_at=start.get("started_at"),
        )

    def on_llm_error(self, error: BaseException, run_id: Any, **kwargs: Any) -> None:
        start = self._starts.pop(str(run_id), None)
        if not start:
            return
        self.profiler.record_llm(
            task_id=start["task_id"],
            node=start.get("node"),
            model=start.get("model", ""),
            duration_seconds=time.perf_counter() - start["started_monotonic"],
            prompt_tokens=start["prompt_tokens_estimate"],
            completion_tokens=0,
            total_tokens=start["prompt_tokens_estimate"],
            estimated=True,
            error=type(error).__name__,
            started_at=start.get("started_at"),
        )

    def _record_start(self, run_id: Any, serialized: Dict[str, Any], messages: Any, kwargs: Dict[str, Any]) -> None:
        task_id = get_current_task_id()
        if not task_id:
            return
        model = (
            kwargs.get("invocation_params", {}).get("model")
            or kwargs.get("metadata", {}).get("ls_model_name")
            or serialized.get("name")
            or serialized.get("id", [""])[-1]
        )
        prompt_text = _messages_text(messages)
        self._starts[str(run_id)] = {
            "task_id": task_id,
            "node": get_current_node(),
            "model": str(model or ""),
            "started_at": _now_iso(),
            "started_monotonic": time.perf_counter(),
            "prompt_tokens_estimate": _estimate_tokens(prompt_text),
        }

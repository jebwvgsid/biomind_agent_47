from __future__ import annotations

import contextlib
import contextvars
import csv
import json
import os
import time
import uuid
from collections.abc import Iterable, Mapping
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


def _first_int(*values: Any) -> Optional[int]:
    """Return the first integer-like value while preserving a real zero."""
    for value in values:
        normalized = _as_int(value)
        if normalized is not None:
            return normalized
    return None


def _mapping_view(value: Any) -> Optional[Dict[str, Any]]:
    """Return a shallow mapping for SDK/Pydantic response objects when possible."""
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return None


def _nested_first_int(
    data: Mapping[str, Any],
    container_keys: Iterable[str],
    value_keys: Iterable[str],
) -> Optional[int]:
    """Read the first integer-like token detail from common SDK shapes."""
    for container_key in container_keys:
        details = _mapping_view(data.get(container_key))
        if details is None:
            continue
        value = _first_int(*(details.get(key) for key in value_keys))
        if value is not None:
            return value
    return None


def _normalize_usage(data: Any) -> Dict[str, Any]:
    data = _mapping_view(data)
    if data is None:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "reasoning_tokens": None,
            "prompt_cache_hit_tokens": None,
            "prompt_cache_miss_tokens": None,
            "cache_metric_status": "not_reported",
        }

    prompt = _first_int(
        data.get("prompt_tokens"),
        data.get("input_tokens"),
        data.get("prompt_token_count"),
    )
    completion = _first_int(
        data.get("completion_tokens"),
        data.get("output_tokens"),
        data.get("completion_token_count"),
    )
    total = _first_int(data.get("total_tokens"), data.get("total_token_count"))
    reasoning = _first_int(
        data.get("reasoning_tokens"),
        _nested_first_int(
            data,
            ("completion_tokens_details", "output_tokens_details"),
            ("reasoning_tokens", "reasoning"),
        ),
        _nested_first_int(
            data,
            ("output_token_details",),
            ("reasoning", "reasoning_tokens"),
        ),
    )
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    # DeepSeek exposes the first two direct fields. OpenAI-compatible SDKs
    # commonly expose cached input as either ``cached_tokens`` directly or
    # below prompt/input token details. LangChain standardizes the same value
    # as ``input_token_details.cache_read``. Keep one provider-neutral shape in
    # the profiler so cache accounting is not silently discarded.
    prompt_cache_hit = _first_int(
        data.get("prompt_cache_hit_tokens"),
        data.get("cached_tokens"),
        data.get("cache_read_input_tokens"),
        data.get("cache_read_tokens"),
        _nested_first_int(
            data,
            ("prompt_tokens_details", "input_tokens_details"),
            ("cached_tokens",),
        ),
        _nested_first_int(
            data,
            ("input_token_details",),
            ("cache_read", "cached_tokens", "cache_read_tokens"),
        ),
    )
    prompt_cache_miss = _first_int(data.get("prompt_cache_miss_tokens"))
    cache_fields_reported = (
        prompt_cache_hit is not None or prompt_cache_miss is not None
    )
    cache_derived = False

    # Providers that report only cached_tokens still give enough information
    # to account for the uncached portion when prompt_tokens is the total input.
    if (
        prompt_cache_miss is None
        and prompt is not None
        and prompt_cache_hit is not None
        and 0 <= prompt_cache_hit <= prompt
    ):
        prompt_cache_miss = prompt - prompt_cache_hit
        cache_derived = True
    elif (
        prompt_cache_hit is None
        and prompt is not None
        and prompt_cache_miss is not None
        and 0 <= prompt_cache_miss <= prompt
    ):
        prompt_cache_hit = prompt - prompt_cache_miss
        cache_derived = True

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "reasoning_tokens": reasoning,
        "prompt_cache_hit_tokens": prompt_cache_hit,
        "prompt_cache_miss_tokens": prompt_cache_miss,
        "cache_metric_status": (
            "partially_reported_derived"
            if cache_fields_reported and cache_derived
            else "reported"
            if cache_fields_reported
            else "not_reported"
        ),
    }


def _find_usage_dict(value: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    if depth > 4:
        return None
    mapping = _mapping_view(value)
    if mapping is not None:
        normalized = _normalize_usage(mapping)
        if any(
            normalized.get(key) is not None
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "reasoning_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            )
        ):
            return mapping
        for key in ("token_usage", "usage", "usage_metadata", "response_metadata", "llm_output"):
            nested = mapping.get(key)
            found = _find_usage_dict(nested, depth + 1)
            if found:
                return found
        for nested in mapping.values():
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


def _response_usage(response: Any) -> Dict[str, Any]:
    candidates: List[Any] = [
        getattr(response, "usage", None),
        getattr(response, "usage_metadata", None),
        getattr(response, "llm_output", None),
        response,
    ]
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
    merged: Dict[str, Any] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "reasoning_tokens": None,
        "prompt_cache_hit_tokens": None,
        "prompt_cache_miss_tokens": None,
        "cache_metric_status": "not_reported",
    }
    for candidate in candidates:
        found = _find_usage_dict(candidate)
        normalized = _normalize_usage(found)
        for key, value in normalized.items():
            if key == "cache_metric_status":
                if value != "not_reported":
                    merged[key] = value
                continue
            if merged[key] is None and value is not None:
                merged[key] = value

    # Some LangChain adapters split standard token totals into usage_metadata
    # and provider-specific cache counters into response_metadata. Merge those
    # views instead of returning the first one and losing cache observability.
    prompt = merged["prompt_tokens"]
    prompt_cache_hit = merged["prompt_cache_hit_tokens"]
    prompt_cache_miss = merged["prompt_cache_miss_tokens"]
    if (
        prompt_cache_miss is None
        and prompt is not None
        and prompt_cache_hit is not None
        and 0 <= prompt_cache_hit <= prompt
    ):
        merged["prompt_cache_miss_tokens"] = prompt - prompt_cache_hit
        merged["cache_metric_status"] = "partially_reported_derived"
    elif (
        prompt_cache_hit is None
        and prompt is not None
        and prompt_cache_miss is not None
        and 0 <= prompt_cache_miss <= prompt
    ):
        merged["prompt_cache_hit_tokens"] = prompt - prompt_cache_miss
        merged["cache_metric_status"] = "partially_reported_derived"
    return merged


def _response_finish_reason(response: Any) -> str:
    """Extract a provider finish reason from LangChain or SDK response shapes."""
    mapping = _mapping_view(response) or {}
    direct = str(mapping.get("finish_reason") or getattr(response, "finish_reason", "") or "").strip()
    if direct:
        return direct

    choices = mapping.get("choices") if isinstance(mapping.get("choices"), list) else getattr(response, "choices", None)
    for choice in choices or []:
        choice_mapping = _mapping_view(choice) or {}
        reason = str(choice_mapping.get("finish_reason") or getattr(choice, "finish_reason", "") or "").strip()
        if reason:
            return reason

    for generation_list in getattr(response, "generations", []) or []:
        for generation in generation_list or []:
            for candidate in (
                getattr(generation, "generation_info", None),
                getattr(getattr(generation, "message", None), "response_metadata", None),
                getattr(getattr(generation, "message", None), "additional_kwargs", None),
            ):
                candidate_mapping = _mapping_view(candidate) or {}
                reason = str(
                    candidate_mapping.get("finish_reason")
                    or candidate_mapping.get("stop_reason")
                    or ""
                ).strip()
                if reason:
                    return reason
    return ""


def _completion_text(completion: Any) -> str:
    """Extract partial assistant text from an SDK ChatCompletion without persisting it."""
    parts: List[str] = []
    mapping = _mapping_view(completion) or {}
    choices = mapping.get("choices") if isinstance(mapping.get("choices"), list) else getattr(completion, "choices", None)
    for choice in choices or []:
        choice_mapping = _mapping_view(choice) or {}
        message = choice_mapping.get("message") or getattr(choice, "message", None)
        message_mapping = _mapping_view(message) or {}
        content = message_mapping.get("content") if message_mapping else getattr(message, "content", None)
        if content not in (None, ""):
            parts.append(str(content))
            continue
        text = choice_mapping.get("text") if choice_mapping else getattr(choice, "text", None)
        if text not in (None, ""):
            parts.append(str(text))
    return "\n".join(parts)


def _exception_completion(error: BaseException) -> Any:
    """Return the typed provider completion retained by parsing exceptions."""
    for candidate in (
        getattr(error, "completion", None),
        getattr(error, "response", None),
    ):
        if candidate is not None and (
            getattr(candidate, "choices", None) is not None
            or (_mapping_view(candidate) or {}).get("choices") is not None
        ):
            return candidate
    return None


def _collect_evidence_refs(value: Any) -> List[str]:
    refs: List[str] = []

    def visit(item: Any) -> None:
        mapping = _mapping_view(item)
        if mapping is not None:
            for key, child in mapping.items():
                normalized_key = str(key).strip().lower()
                if normalized_key.endswith("_ref") and str(child or "").strip():
                    refs.append(str(child).strip())
                elif normalized_key.endswith("_refs"):
                    for ref in _ensure_list(child):
                        if str(ref or "").strip():
                            refs.append(str(ref).strip())
                else:
                    visit(child)
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)

    visit(value)
    return _dedupe_text(refs)


def _shorten(text: Any, limit: int = 500) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n")
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [truncated {len(value) - limit} chars]"


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _dedupe_text(items: Any) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for item in _ensure_list(items):
        text = str(item or "").strip()
        if not text or text.lower() in {"none", "n/a", "null"}:
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _merge_text(existing: Any, incoming: Any) -> List[str]:
    return _dedupe_text([*_ensure_list(existing), *_ensure_list(incoming)])


def _normalize_path_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 3 and text[1] == ":":
        while "\\\\" in text:
            text = text.replace("\\\\", "\\")
    return text


def _dedupe_paths(items: Any) -> List[str]:
    return _dedupe_text([_normalize_path_text(item) for item in _ensure_list(items)])


def _join_tsv(items: Any) -> str:
    return "; ".join(text.replace("\t", " ").replace("\n", " ") for text in _dedupe_text(items))


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _format_bytes(size: Any) -> str:
    size_int = _coerce_int(size)
    if size_int <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_int)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.2f} {unit}"


def _resolve_repo_root(repo_root: str | Path | None = None) -> Path:
    if repo_root:
        return Path(repo_root).resolve()
    return Path(__file__).resolve().parents[1]


def _file_size_bytes(raw_path: str, root: Path) -> int:
    value = str(raw_path or "").strip()
    if not value:
        return 0
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        if path.exists() and path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    return 0


def generic_metadata_for_prompt(
    task_id: str,
    prompt: Any,
    repo_root: str | Path | None = None,
) -> Dict[str, Any]:
    """Return audit metadata for any non-benchmark task prompt."""
    task_key = str(task_id or "").strip() or "default"
    prompt_text = str(prompt or "").strip()
    root = _resolve_repo_root(repo_root)
    if not prompt_text:
        return {
            "test_task_id": task_key,
            "test_content": task_key,
            "test_prompt": "",
            "test_data": [],
            "data_size_bytes": 0,
        }

    semantics: Dict[str, Any] = {}
    data_paths: List[str] = []
    try:
        from memory.task_semantics import extract_path_like_values

        data_paths = _dedupe_paths(extract_path_like_values(prompt_text))
    except Exception:
        data_paths = []
    try:
        from memory.task_semantics import infer_task_semantics

        semantics = infer_task_semantics(
            task_id=task_key,
            stage="start",
            messages=[prompt_text],
            payload={"user_query": prompt_text},
        )
        data_paths = _dedupe_paths([*_ensure_list(semantics.get("output_artifacts")), *data_paths])
    except Exception:
        semantics = {}

    compact_prompt = " ".join(prompt_text.split())
    task_type = str(semantics.get("task_type") or "general")
    analysis_type = str(semantics.get("analysis_type") or "general_analysis")
    inferred_title = str(semantics.get("task_title") or "").strip()
    if inferred_title and not (task_type == "general" and analysis_type == "general_analysis"):
        test_content = inferred_title
    else:
        test_content = compact_prompt[:160] or task_key

    return {
        "test_task_id": task_key,
        "test_content": test_content,
        "test_prompt": prompt_text,
        "test_data": data_paths,
        "data_size_bytes": sum(_file_size_bytes(path, root) for path in data_paths),
        "semantics": semantics,
    }


def benchmark_metadata_for_task(task_id: str, repo_root: str | Path | None = None) -> Dict[str, Any]:
    """Return benchmark prompt/data metadata for a BM-* task id, if available."""
    task_key = str(task_id or "").strip()
    if not task_key.startswith("BM-"):
        return {}

    root = _resolve_repo_root(repo_root)
    tasks_path = root / "benchmarks" / "biomind_agent_benchmark" / "questions" / "tasks.yaml"
    if not tasks_path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(tasks_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        return {}
    task = next((item for item in tasks if isinstance(item, dict) and str(item.get("id")) == task_key), None)
    if not task:
        return {}

    data_paths: List[str] = []
    data_size = 0
    for entry in task.get("data_files") or []:
        if isinstance(entry, dict):
            rel_path = str(entry.get("path") or "").strip()
            availability = str(entry.get("availability") or "available").strip()
        else:
            rel_path = str(entry or "").strip()
            availability = "available"
        if not rel_path:
            continue
        data_paths.append(rel_path)
        if availability != "available":
            continue
        path = Path(rel_path)
        if not path.is_absolute():
            path = root / path
        data_size += _file_size_bytes(str(path), root)

    return {
        "test_task_id": task_key,
        "test_content": str(task.get("title") or task.get("category") or task_key),
        "test_prompt": str(task.get("prompt") or ""),
        "test_data": data_paths,
        "data_size_bytes": data_size,
        "benchmark": {
            "benchmark_id": data.get("benchmark_id", ""),
            "version": data.get("version", ""),
            "category": task.get("category", ""),
            "question_type": task.get("question_type", ""),
            "execution_mode": task.get("execution_mode", ""),
            "expected_skills": _dedupe_text(task.get("expected_skills")),
            "optional_skills": _dedupe_text(task.get("optional_skills")),
            "forbidden_skills": _dedupe_text(task.get("forbidden_skills")),
        },
    }


class RunProfiler:
    """Collects per-task wall time, LLM usage, human wait, and command timings."""

    RUN_RECORD_FIELDS = [
        "测试时间",
        "测试任务id",
        "测试内容",
        "测试提示词",
        "测试数据",
        "数据大小",
        "选择skills",
        "使用skills",
        "分析总用时",
        "计算用时",
        "纠错次数",
        "人工确认时间",
        "输出结果",
        "状态",
        "run_metrics_json",
    ]

    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)
        self._tasks: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _process_identity(pid: int) -> str:
        """Return a PID reuse-resistant Linux process identity when available."""
        try:
            fields = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").split()
            return f"{int(pid)}:{fields[21]}"
        except (OSError, ValueError, IndexError):
            return f"{int(pid)}:unknown"

    @classmethod
    def _lease_owner_alive(cls, lease: Dict[str, Any]) -> bool:
        try:
            pid = int(lease.get("owner_pid") or 0)
            if pid <= 0:
                return False
            os.kill(pid, 0)
        except (OSError, TypeError, ValueError):
            return False
        expected = str(lease.get("process_identity") or "")
        return not expected or expected.endswith(":unknown") or cls._process_identity(pid) == expected

    def reconcile_stale_run(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Convert an orphaned on-disk `running` snapshot into `interrupted`."""
        metrics_path = self.output_root / task_id / "run_metrics.json"
        if not metrics_path.is_file():
            return None
        try:
            snapshot = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if str(snapshot.get("status") or "") != "running":
            return None
        lease = dict(snapshot.get("run_lease") or {})
        if self._lease_owner_alive(lease):
            return None
        now = _now_iso()
        snapshot["status"] = "interrupted"
        snapshot["ended_at"] = now
        snapshot["last_updated_at"] = now
        snapshot["run_lease"] = {
            **lease,
            "state": "orphaned",
            "released_at": now,
            "reconciliation_reason": "owner_process_identity_not_alive",
        }
        events = list(snapshot.get("events") or [])
        events.append({
            "id": self._new_event_id("lease"),
            "type": "run_lifecycle",
            "stage": "ORPHAN_RECONCILED",
            "started_at": now,
            "reason": "owner_process_identity_not_alive",
        })
        snapshot["events"] = events
        metrics_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        return snapshot

    def start_task(self, task_id: str, status: str = "running") -> None:
        task = self._tasks.get(task_id)
        if task:
            task["status"] = status
            if status == "running":
                task["ended_at"] = None
                task.pop("_ended_wall_seconds", None)
                now = _now_iso()
                pid = os.getpid()
                lease = task.setdefault("run_lease", {})
                if lease.get("state") != "active":
                    lease.update({
                        "run_uuid": str(uuid.uuid4()),
                        "owner_pid": pid,
                        "process_identity": self._process_identity(pid),
                        "state": "active",
                        "acquired_at": now,
                        "last_heartbeat_at": now,
                    })
            task["last_updated_at"] = _now_iso()
            return
        reconciled = self.reconcile_stale_run(task_id)
        now = _now_iso()
        pid = os.getpid()
        self._tasks[task_id] = {
            "task_id": task_id,
            "status": status,
            "started_at": now,
            "_started_monotonic": time.perf_counter(),
            "last_updated_at": now,
            "ended_at": None,
            "events": list((reconciled or {}).get("events") or [])[-1:] if reconciled else [],
            "metadata": {
                "previous_run_reconciliation": {
                    "status": reconciled.get("status"),
                    "run_lease": reconciled.get("run_lease"),
                }
            } if reconciled else {},
            "skills": {
                "selected": [],
                "selected_raw": [],
                "active": [],
                "future": [],
                "candidate": [],
                "selection_source": "",
                "workflow_id": "",
                "active_batch_id": "",
                "used": [],
                "used_by_type": {},
            },
            "correction_count": 0,
            "max_retry_count": 0,
            "final_result": {},
            "run_lease": {
                "run_uuid": str(uuid.uuid4()),
                "owner_pid": pid,
                "process_identity": self._process_identity(pid),
                "state": "active",
                "acquired_at": now,
                "last_heartbeat_at": now,
            },
        }

    def finish_task(
        self,
        task_id: str,
        status: str = "completed",
        *,
        terminal_projection: Optional[Mapping[str, Any]] = None,
    ) -> None:
        task = self._ensure_task(task_id)
        self._reconcile_durable_terminal_projection(
            task_id,
            status,
            terminal_projection=terminal_projection,
        )
        task["status"] = status
        task["ended_at"] = _now_iso()
        task["last_updated_at"] = task["ended_at"]
        lease = task.setdefault("run_lease", {})
        lease.update({"state": "released", "released_at": task["ended_at"], "last_heartbeat_at": task["ended_at"]})
        task["_ended_wall_seconds"] = round(time.perf_counter() - task["_started_monotonic"], 3)
        self.flush(task_id)

    def finish_for_exception(self, task_id: str, exc: BaseException) -> str:
        """Map controller termination modes to a durable non-running state."""
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            status = "interrupted"
        elif isinstance(exc, TimeoutError):
            status = "timeout"
        elif exc.__class__.__name__ == "CancelledError":
            status = "cancelled"
        else:
            status = "failed"
        self.finish_task(task_id, status)
        return status

    def heartbeat(self, task_id: str) -> None:
        task = self._ensure_task(task_id)
        now = _now_iso()
        task["last_updated_at"] = now
        task.setdefault("run_lease", {})["last_heartbeat_at"] = now

    def record_executor_lifecycle(
        self,
        task_id: str,
        stage: str,
        *,
        action_fingerprint: str = "",
        consultation_id: str = "",
        requirement_ids: Optional[List[str]] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._append_event(task_id, {
            "id": self._new_event_id("executor"),
            "type": "executor_lifecycle",
            "stage": str(stage),
            "node": get_current_node(),
            "started_at": _now_iso(),
            "action_fingerprint": str(action_fingerprint),
            "consultation_id": str(consultation_id),
            "requirement_ids": list(requirement_ids or []),
            "detail": dict(detail or {}),
        })
        self.heartbeat(task_id)

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
        finish_reason: str = "",
        provider_status: str = "",
        completion_integrity: str = "",
        protocol_status: str = "",
        response_artifact_ref: str = "",
        usage_source: str = "",
        reasoning_tokens: Optional[int] = None,
        prompt_cache_hit_tokens: Optional[int] = None,
        prompt_cache_miss_tokens: Optional[int] = None,
        cache_metric_status: str = "not_reported",
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
                "reasoning_tokens": reasoning_tokens,
                "prompt_cache_hit_tokens": prompt_cache_hit_tokens,
                "prompt_cache_miss_tokens": prompt_cache_miss_tokens,
                "cache_metric_status": str(
                    cache_metric_status or "not_reported"
                ),
                "tokens_estimated": estimated,
                "error": error,
                "finish_reason": str(finish_reason or ""),
                "provider_status": str(provider_status or ""),
                "completion_integrity": str(completion_integrity or ""),
                "protocol_status": str(protocol_status or ""),
                "response_artifact_ref": str(response_artifact_ref or ""),
                "usage_source": str(usage_source or ""),
            },
        )

    def record_context_budget(
        self,
        task_id: str,
        report: Dict[str, Any],
        *,
        node: Optional[str] = None,
    ) -> None:
        """Persist block-level prompt packing decisions for later diagnosis."""
        if not isinstance(report, dict):
            return
        self._append_event(
            task_id,
            {
                "id": self._new_event_id("context"),
                "type": "context_budget",
                "node": node or get_current_node(),
                "started_at": _now_iso(),
                "budget_chars": _coerce_int(report.get("budget_chars")),
                "rendered_chars": _coerce_int(report.get("rendered_chars")),
                "overflow": bool(report.get("overflow")),
                "blocks": list(report.get("blocks") or []),
            },
        )

    def record_consultant_preprocess(
        self,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Persist one secret-free Consultant preprocessing timing row."""

        row = dict(payload or {})
        self._append_event(task_id, {
            "id": self._new_event_id("consultant_preprocess"),
            "type": "consultant_preprocess_phase",
            "node": "consultant",
            "started_at": str(row.get("started_at") or _now_iso()),
            **{
                key: value
                for key, value in row.items()
                if key not in {"query_text", "prompt", "content", "secret"}
                and not any(
                    marker in str(key).lower()
                    for marker in ("api_key", "password", "authorization", "credential")
                )
            },
        })

    def record_consultant_progress(
        self,
        task_id: str,
        *,
        code: str,
        message: str,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Record observation-only discovery progress for UI/CLI consumers."""

        self._append_event(task_id, {
            "id": self._new_event_id("consultant_progress"),
            "type": "consultant_progress",
            "node": "consultant",
            "started_at": _now_iso(),
            "code": str(code or "consultant_progress"),
            "message": _shorten(str(message or ""), 500),
            "detail": {
                str(key): value
                for key, value in dict(detail or {}).items()
                if isinstance(value, (str, int, float, bool)) or value is None
                if not any(
                    marker in str(key).lower()
                    for marker in ("api_key", "password", "authorization", "credential", "secret")
                )
            },
            "semantic_effect": "none",
        })

    def record_inspection(self, task_id, result):
        """Observation costs, separate from approved scientific execution."""
        self._append_event(task_id, {
            'id': self._new_event_id('inspection'), 'type': 'inspection',
            'node': result.get('stage'), 'tool': result.get('tool'),
            'started_at': result.get('observed_at'), 'status': result.get('status'),
            'duration_seconds': result.get('duration_seconds', 0),
            'timing': dict(result.get('timing') or {}), 'receipt_ref': result.get('receipt_ref'),
        })


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

    def record_task_metadata(
        self,
        task_id: str,
        *,
        test_content: str = "",
        test_prompt: str = "",
        test_data: Any = None,
        data_size_bytes: Any = None,
        test_time: Optional[str] = None,
        benchmark: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        task = self._ensure_task(task_id)
        metadata = task.setdefault("metadata", {})
        metadata.setdefault("test_task_id", task_id)
        metadata.setdefault("test_time", test_time or task.get("started_at") or _now_iso())
        if test_content:
            metadata["test_content"] = str(test_content)
        if test_prompt:
            metadata["test_prompt"] = str(test_prompt)
        if test_data is not None:
            data_items = _dedupe_text(test_data)
            if data_items or not metadata.get("test_data"):
                metadata["test_data"] = data_items
        if data_size_bytes is not None:
            data_size = _coerce_int(data_size_bytes)
            if data_size > 0 or not metadata.get("data_size_bytes"):
                metadata["data_size_bytes"] = data_size
        if benchmark:
            existing = metadata.get("benchmark") if isinstance(metadata.get("benchmark"), dict) else {}
            metadata["benchmark"] = {**existing, **benchmark}
        if extra:
            metadata.update(extra)
        task["last_updated_at"] = _now_iso()

    def record_skill_selection(
        self,
        task_id: str,
        *,
        selected_skills: Any = None,
        selected_skills_raw: Any = None,
        active_skills: Any = None,
        future_skills: Any = None,
        candidate_skills: Any = None,
        source: str = "",
        workflow_id: str = "",
        active_batch_id: str = "",
        node: Optional[str] = None,
        replace_selected: bool = False,
    ) -> None:
        task = self._ensure_task(task_id)
        skills = task.setdefault("skills", {})
        active = _dedupe_text(active_skills)
        future = _dedupe_text(future_skills)
        explicitly_selected = _dedupe_text(selected_skills)
        incoming_selected = _dedupe_text([*explicitly_selected, *active, *future])
        previous_selected = _dedupe_text(skills.get("selected"))
        selected = (
            incoming_selected
            if replace_selected
            else _dedupe_text([*previous_selected, *incoming_selected])
        )
        if selected_skills is not None or active or future:
            skills["selected"] = selected
        if selected_skills_raw is not None:
            skills["selected_raw"] = _dedupe_text(selected_skills_raw)
        if active_skills is not None:
            skills["active"] = active
        if future_skills is not None:
            skills["future"] = future
        if candidate_skills is not None:
            skills["candidate"] = _dedupe_text(candidate_skills)
        if source:
            skills["selection_source"] = str(source)
        if workflow_id:
            skills["workflow_id"] = str(workflow_id)
        if active_batch_id:
            skills["active_batch_id"] = str(active_batch_id)
        task["last_updated_at"] = _now_iso()
        if selected or active or future or candidate_skills:
            self._append_event(
                task_id,
                {
                    "id": self._new_event_id("skill_select"),
                    "type": "skill_selection",
                    "node": node or get_current_node(),
                    "started_at": _now_iso(),
                    "selected": list(selected),
                    "active": list(active),
                    "future": list(future),
                    "candidate": _dedupe_text(candidate_skills),
                    "source": source,
                    "workflow_id": workflow_id,
                    "active_batch_id": active_batch_id,
                },
            )

    def record_skill_usage(
        self,
        task_id: str,
        skills: Any,
        *,
        usage_type: str = "used",
        node: Optional[str] = None,
        detail: str = "",
    ) -> None:
        used = _dedupe_text(skills)
        if not used:
            return
        task = self._ensure_task(task_id)
        skills_state = task.setdefault("skills", {})
        skills_state["used"] = _merge_text(skills_state.get("used"), used)
        used_by_type = skills_state.setdefault("used_by_type", {})
        used_by_type[usage_type] = _merge_text(used_by_type.get(usage_type), used)
        self._append_event(
            task_id,
            {
                "id": self._new_event_id("skill_use"),
                "type": "skill_usage",
                "node": node or get_current_node(),
                "started_at": _now_iso(),
                "usage_type": usage_type,
                "skills": used,
                "detail": _shorten(detail, 500),
            },
        )

    def record_supervisor_verdict(
        self,
        task_id: str,
        verdict: Any,
        *,
        retry_count: Any = None,
        node: Optional[str] = None,
    ) -> None:
        verdict_text = str(verdict or "").strip().upper()
        if not verdict_text:
            return
        task = self._ensure_task(task_id)
        retry_int = _coerce_int(retry_count)
        task["max_retry_count"] = max(_coerce_int(task.get("max_retry_count")), retry_int)
        if verdict_text == "RETRY":
            task["correction_count"] = _coerce_int(task.get("correction_count")) + 1
        self._append_event(
            task_id,
            {
                "id": self._new_event_id("supervisor"),
                "type": "supervisor_verdict",
                "node": node or get_current_node(),
                "started_at": _now_iso(),
                "verdict": verdict_text,
                "retry_count": retry_int,
            },
        )

    def record_final_result(
        self,
        task_id: str,
        *,
        status: str = "",
        output_result: str = "",
        output_artifacts: Any = None,
        node: Optional[str] = None,
        terminal_diagnostic: Optional[Mapping[str, Any]] = None,
        evidence_refs: Any = None,
        evidence: Any = None,
    ) -> None:
        task = self._ensure_task(task_id)
        final_result = task.setdefault("final_result", {})
        if status:
            final_result["status"] = str(status)
            task["status"] = str(status)
        if output_result:
            final_result["output_result"] = _shorten(output_result, 3000)
        if output_artifacts is not None:
            if isinstance(output_artifacts, (list, tuple)) and all(
                isinstance(item, dict) for item in output_artifacts
            ):
                deduped: List[Dict[str, Any]] = []
                seen: set[str] = set()
                for item in output_artifacts:
                    key = str(item.get("artifact_ref") or item.get("output_id") or item.get("path") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    deduped.append(dict(item))
                final_result["output_artifacts"] = deduped
            else:
                final_result["output_artifacts"] = _dedupe_text(output_artifacts)
        if terminal_diagnostic is not None:
            diagnostic = dict(terminal_diagnostic or {})
            final_result["terminal_diagnostic"] = diagnostic
            if str(status or final_result.get("status") or "").strip().lower() in {
                "blocked", "failed", "cancelled", "timeout", "interrupted",
            }:
                final_result["output_result"] = self._terminal_diagnostic_text(
                    str(status or final_result.get("status") or "blocked"),
                    diagnostic,
                )
        if evidence_refs is not None:
            final_result["evidence_refs"] = _dedupe_text(evidence_refs)
        if evidence is not None:
            final_result["evidence"] = list(_ensure_list(evidence))
        final_result["updated_at"] = _now_iso()
        task["last_updated_at"] = final_result["updated_at"]
        self._append_event(
            task_id,
            {
                "id": self._new_event_id("final"),
                "type": "final_result",
                "node": node or get_current_node(),
                "started_at": final_result["updated_at"],
                "status": final_result.get("status", ""),
                "output_result": _shorten(final_result.get("output_result", ""), 800),
                "output_artifacts": final_result.get("output_artifacts", []),
                "terminal_diagnostic": final_result.get("terminal_diagnostic", {}),
                "evidence_refs": final_result.get("evidence_refs", []),
            },
        )

    @staticmethod
    def _terminal_diagnostic_text(status: str, diagnostic: Mapping[str, Any]) -> str:
        code = str(
            diagnostic.get("error_code")
            or diagnostic.get("reason_code")
            or diagnostic.get("code")
            or str(status or "terminal").upper()
        ).strip()
        owner = str(diagnostic.get("error_owner") or diagnostic.get("owner") or "").strip()
        detail = str(
            diagnostic.get("detail")
            or diagnostic.get("reason")
            or diagnostic.get("message")
            or ""
        ).strip()
        parts = [f"{str(status or 'terminal').strip().lower()}: {code}"]
        if owner:
            parts.append(f"owner={owner}")
        if detail:
            parts.append(detail)
        terminal_failure = diagnostic.get("terminal_failure")
        if not isinstance(terminal_failure, Mapping):
            terminal_failure = diagnostic.get("latest_failure")
        if isinstance(terminal_failure, Mapping) and terminal_failure:
            terminal_code = str(
                terminal_failure.get("error_code")
                or terminal_failure.get("code")
                or ""
            ).strip()
            terminal_detail = str(
                terminal_failure.get("message")
                or terminal_failure.get("detail")
                or ""
            ).strip()
            if terminal_code and (
                terminal_code != code
                or str(terminal_failure.get("fingerprint") or "")
                != str(diagnostic.get("fingerprint") or "")
            ):
                terminal_text = f"corrective_terminal={terminal_code}"
                if terminal_detail:
                    terminal_text += f": {terminal_detail}"
                parts.append(terminal_text)
        termination_reason = str(diagnostic.get("termination_reason") or "").strip()
        if termination_reason:
            parts.append(f"termination_reason={termination_reason}")
        if diagnostic.get("execution_started") is False:
            parts.append("execution_started=false")
        refs = _collect_evidence_refs(diagnostic)
        if refs:
            parts.append("evidence_refs=" + ", ".join(refs))
        return _shorten(" | ".join(parts), 3000)

    def record_terminal_projection(
        self,
        task_id: str,
        *,
        status: str,
        terminal_diagnostic: Optional[Mapping[str, Any]] = None,
        evidence_refs: Any = None,
        selected_skills: Any = None,
        node: str = "terminal_projection",
    ) -> None:
        """Project one durable terminal verdict without reusing command stdout."""
        normalized_status = str(status or "").strip().lower()
        selected = _dedupe_text(selected_skills)
        if selected:
            current = _dedupe_text(self._ensure_task(task_id).setdefault("skills", {}).get("selected"))
            if selected != current:
                self.record_skill_selection(
                    task_id,
                    selected_skills=selected,
                    source="durable_terminal_projection",
                    node=node,
                )

        diagnostic = dict(terminal_diagnostic or {})
        refs = _dedupe_text([*_ensure_list(evidence_refs), *_collect_evidence_refs(diagnostic)])
        if normalized_status in {"blocked", "failed", "cancelled", "timeout", "interrupted"}:
            if not diagnostic:
                diagnostic = {
                    "error_code": f"TASK_{normalized_status.upper()}",
                    "detail": f"Task ended with terminal status {normalized_status}.",
                }
            self.record_final_result(
                task_id,
                status=normalized_status,
                terminal_diagnostic=diagnostic,
                evidence_refs=refs,
                node=node,
            )

    def _durable_terminal_projection(self, task_id: str) -> Dict[str, Any]:
        """Read the compact durable state used by resume and terminal routing."""
        path = self.output_root / str(task_id) / "plan_state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        task_state = data.get("task_state") if isinstance(data.get("task_state"), dict) else {}
        from agent_core.workflow.managed_stop import current_managed_stop, projection
        common = current_managed_stop(data)
        if common:
            return projection(common)
        consultation = (
            task_state.get("consultation_state")
            if isinstance(task_state.get("consultation_state"), dict)
            else {}
        )
        final_contract = (
            consultation.get("final_contract")
            if isinstance(consultation.get("final_contract"), dict)
            else {}
        )
        finalize_protocol = (
            consultation.get("finalize_protocol")
            if isinstance(consultation.get("finalize_protocol"), dict)
            else {}
        )
        skill_candidates = (
            task_state.get("selected_skills"),
            task_state.get("selected_skill_ids"),
            data.get("selected_skill_ids"),
            consultation.get("resolved_selected_skill_ids"),
            final_contract.get("selected_skills"),
        )
        selected = next(
            (_dedupe_text(candidate) for candidate in skill_candidates if _dedupe_text(candidate)),
            [],
        )
        lineage_primary_failure = next(
            (
                dict(candidate)
                for candidate in (
                    data.get("primary_failure"),
                    task_state.get("primary_failure"),
                    data.get("failure"),
                )
                if isinstance(candidate, Mapping)
                and str(candidate.get("error_code") or "")
            ),
            {},
        )
        durable_terminal_failure = next(
            (
                dict(candidate)
                for candidate in (
                    data.get("terminal_failure"),
                    task_state.get("terminal_failure"),
                )
                if isinstance(candidate, Mapping)
                and str(candidate.get("error_code") or "")
            ),
            {},
        )
        durable_failure = (
            dict(data.get("failure") or {})
            if isinstance(data.get("failure"), Mapping)
            else {}
        )
        planning_failure = (
            dict(task_state.get("planning_failure") or {})
            if isinstance(task_state.get("planning_failure"), Mapping)
            else {}
        )
        planning_terminal = (
            dict(planning_failure.get("terminal_diagnostic") or {})
            if str(planning_failure.get("status") or "") == "terminal"
            and isinstance(planning_failure.get("terminal_diagnostic"), Mapping)
            else {}
        )
        if (
            durable_failure.get("execution_started") is True
            and not str(durable_failure.get("termination_reason") or "")
        ):
            # A repaired candidate subsequently crossed the process boundary;
            # retain that history in plan_state without projecting it as the
            # terminal diagnosis for a later, unrelated outcome.
            durable_failure = {}
        diagnostic_candidates = (
            durable_terminal_failure,
            durable_failure,
            planning_terminal,
            task_state.get("terminal_diagnostic"),
            consultation.get("terminal_diagnostic"),
            data.get("terminal_diagnostic"),
        )
        diagnostic = next(
            (dict(candidate) for candidate in diagnostic_candidates if isinstance(candidate, Mapping) and candidate),
            {},
        )
        source_failure = durable_terminal_failure or durable_failure
        if source_failure:
            diagnostic.setdefault(
                "error_owner",
                str(source_failure.get("owner") or source_failure.get("error_owner") or ""),
            )
            diagnostic.setdefault(
                "stage",
                str(source_failure.get("stage") or source_failure.get("failure_stage") or ""),
            )
            diagnostic.setdefault(
                "detail",
                str(source_failure.get("detail") or source_failure.get("message") or ""),
            )
        if durable_terminal_failure and lineage_primary_failure:
            terminal_failure_id = str(
                durable_terminal_failure.get("failure_id") or ""
            )
            lineage_failure_id = str(
                lineage_primary_failure.get("failure_id") or ""
            )
            same_failure = (
                bool(terminal_failure_id and lineage_failure_id)
                and terminal_failure_id == lineage_failure_id
            ) or durable_terminal_failure == lineage_primary_failure
            if not same_failure:
                diagnostic.setdefault(
                    "lineage_primary_cause",
                    lineage_primary_failure,
                )
        if not diagnostic and str(task_state.get("status_reason") or "").strip():
            diagnostic = {
                "error_code": f"TASK_{str(task_state.get('status') or 'terminal').upper()}",
                "detail": str(task_state.get("status_reason") or ""),
            }
        finalize_status = str(
            finalize_protocol.get("finalize_status")
            or finalize_protocol.get("status")
            or ""
        ).strip().lower()
        compile_outcome = (
            finalize_protocol.get("compile_outcome")
            if isinstance(finalize_protocol.get("compile_outcome"), dict)
            else {}
        )
        if not diagnostic and (
            finalize_status in {"blocked", "blocked_internal_contract", "blocked_internal_provider"}
            or str(compile_outcome.get("status") or "").strip().lower() == "blocked"
        ):
            attempts = [
                item for item in finalize_protocol.get("attempts", []) or []
                if isinstance(item, Mapping)
            ]
            latest_attempt = dict(attempts[-1]) if attempts else {}
            integrity = str(
                latest_attempt.get("completion_integrity")
                or finalize_protocol.get("completion_integrity")
                or ""
            ).strip().lower()
            error_code = str(
                latest_attempt.get("error_code")
                or finalize_protocol.get("terminal_error_code")
                or ("FINALIZE_RESPONSE_TRUNCATED" if integrity == "truncated" else "FINALIZE_BLOCKED")
            )
            diagnostic = {
                "error_code": error_code,
                "error_owner": str(
                    latest_attempt.get("error_owner")
                    or finalize_protocol.get("terminal_owner")
                    or "controller"
                ),
                "detail": (
                    "Consultant FINALIZE ended in a durable blocked state"
                    + (f" with completion_integrity={integrity}" if integrity else "")
                    + "."
                ),
                "evidence_refs": _collect_evidence_refs(finalize_protocol),
            }
        return {
            "selected_skills": selected,
            "terminal_diagnostic": diagnostic,
            "evidence_refs": _collect_evidence_refs({
                "diagnostic": diagnostic,
                "consultation_state": consultation,
            }),
        }

    def _reconcile_durable_terminal_projection(
        self,
        task_id: str,
        status: str,
        *,
        terminal_projection: Optional[Mapping[str, Any]] = None,
    ) -> None:
        durable = self._durable_terminal_projection(task_id)
        explicit = dict(terminal_projection or {})
        projection = dict(durable)
        if explicit:
            # A terminal graph outcome is newer and more specific than an
            # incomplete-Plan fallback.  Durable skill identities and evidence
            # may still supplement it, but cannot replace its diagnosis.
            projection = {
                "selected_skills": (
                    explicit.get("selected_skills")
                    or durable.get("selected_skills")
                    or []
                ),
                "terminal_diagnostic": (
                    explicit.get("terminal_diagnostic")
                    or durable.get("terminal_diagnostic")
                    or {}
                ),
                "evidence_refs": _dedupe_text([
                    *_ensure_list(explicit.get("evidence_refs")),
                    *_ensure_list(durable.get("evidence_refs")),
                ]),
            }
        if not projection and str(status or "").strip().lower() not in {
            "blocked", "failed", "cancelled", "timeout", "interrupted",
        }:
            return
        self.record_terminal_projection(
            task_id,
            status=status,
            terminal_diagnostic=projection.get("terminal_diagnostic") or {},
            evidence_refs=projection.get("evidence_refs") or [],
            selected_skills=projection.get("selected_skills") or [],
            node=(
                "typed_generate_terminal_projection"
                if explicit
                else "terminal_projection"
            ),
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
        self._write_run_record(snapshot, json_path)
        return json_path

    def snapshot(self, task_id: str) -> Dict[str, Any]:
        task = self._ensure_task(task_id)
        events = list(task["events"])
        wall = round(time.perf_counter() - task["_started_monotonic"], 3)
        if task.get("ended_at"):
            wall = task.get("_ended_wall_seconds", wall)

        inspection_events = [e for e in events if e.get("type") == "inspection"]
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
        review_prompt_types = {"code_review", "legacy_code_review", "plan_review", "plan_review_feedback"}
        review_feedback_or_rejections = [
            e for e in human_events
            if e.get("prompt_type") in review_prompt_types
            and not e.get("approved")
            and not str(e.get("response") or "").startswith("<waiting_user:")
        ]

        node_totals: Dict[str, float] = {}
        for event in node_events:
            node = event.get("node") or "unknown"
            node_totals[node] = round(node_totals.get(node, 0.0) + float(event.get("duration_seconds") or 0), 3)

        cache_usage_events = [
            event
            for event in llm_events
            if event.get("prompt_cache_hit_tokens") is not None
            or event.get("prompt_cache_miss_tokens") is not None
        ]
        hit_usage_events = [
            event for event in cache_usage_events
            if event.get("prompt_cache_hit_tokens") is not None
        ]
        miss_usage_events = [
            event for event in cache_usage_events
            if event.get("prompt_cache_miss_tokens") is not None
        ]
        reasoning_usage_events = [
            event for event in llm_events
            if event.get("reasoning_tokens") is not None
        ]
        prompt_cache_hit_tokens = sum(
            int(event.get("prompt_cache_hit_tokens") or 0)
            for event in hit_usage_events
        ) if hit_usage_events else None
        prompt_cache_miss_tokens = sum(
            int(event.get("prompt_cache_miss_tokens") or 0)
            for event in miss_usage_events
        ) if miss_usage_events else None
        reasoning_tokens = sum(
            int(event.get("reasoning_tokens") or 0)
            for event in reasoning_usage_events
        ) if reasoning_usage_events else None
        prompt_cache_observed_tokens = (
            prompt_cache_hit_tokens + prompt_cache_miss_tokens
            if prompt_cache_hit_tokens is not None
            and prompt_cache_miss_tokens is not None
            else None
        )
        token_totals = {
            "prompt_tokens": sum(int(e.get("prompt_tokens") or 0) for e in llm_events),
            "completion_tokens": sum(int(e.get("completion_tokens") or 0) for e in llm_events),
            "total_tokens": sum(int(e.get("total_tokens") or 0) for e in llm_events),
            "reasoning_tokens": reasoning_tokens,
            "contains_estimates": any(e.get("tokens_estimated") for e in llm_events),
            "prompt_cache_hit_tokens": prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": prompt_cache_miss_tokens,
            "prompt_cache_usage_reported_calls": len(cache_usage_events),
            "cache_metric_status": (
                "reported" if cache_usage_events else "not_reported"
            ),
            "prompt_cache_hit_rate": (
                round(prompt_cache_hit_tokens / prompt_cache_observed_tokens, 6)
                if prompt_cache_observed_tokens is not None
                and prompt_cache_observed_tokens > 0
                else None
            ),
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
            "local_inspections": len(inspection_events),
            "local_inspection_seconds": sum(float(e.get('duration_seconds') or 0) for e in inspection_events),
            "local_inspection_timing": {key: sum(float((e.get('timing') or {}).get(key) or 0) for e in inspection_events)
                for key in ('prepare_seconds', 'scan_seconds', 'command_seconds')},
            "successful_commands": sum(1 for e in command_events if e.get("success")),
            "failed_commands": sum(1 for e in command_events if not e.get("success")),
            "manual_confirmations": len(human_events),
            "auto_confirmations": sum(1 for e in events if e.get("type") == "confirmation" and e.get("automatic")),
            "correction_count": _coerce_int(task.get("correction_count")),
            "max_retry_count": _coerce_int(task.get("max_retry_count")),
            "manual_feedback_or_rejection_count": len(review_feedback_or_rejections),
            "node_seconds": node_totals,
            "tokens": token_totals,
        }
        final_result = dict(task.get("final_result") or {})
        task_status = str(task.get("status") or "running").strip().lower()
        if not final_result.get("status") and task_status in {
            "blocked", "failed", "cancelled", "timeout", "interrupted", "completed",
        }:
            final_result["status"] = task_status
        if not final_result.get("output_result"):
            if task_status in {"blocked", "failed", "cancelled", "timeout", "interrupted"}:
                final_result["output_result"] = f"Task ended with terminal status {task_status}."
            else:
                final_result["output_result"] = self._fallback_output_result(events)

        terminal_diagnostic = (
            dict(final_result.get("terminal_diagnostic") or {})
            if isinstance(final_result.get("terminal_diagnostic"), Mapping)
            else {}
        )
        failure_projection: Dict[str, Any] = {}
        if terminal_diagnostic:
            failure_projection = {
                "error_code": str(
                    terminal_diagnostic.get("error_code")
                    or terminal_diagnostic.get("reason_code")
                    or terminal_diagnostic.get("code")
                    or ""
                ),
                "owner": str(
                    terminal_diagnostic.get("owner")
                    or terminal_diagnostic.get("error_owner")
                    or ""
                ),
                "stage": str(
                    terminal_diagnostic.get("stage")
                    or terminal_diagnostic.get("failure_stage")
                    or ""
                ),
                "termination_reason": str(
                    terminal_diagnostic.get("termination_reason") or ""
                ),
                "execution_started": terminal_diagnostic.get("execution_started"),
            }
            for key in (
                "fingerprint",
                "candidate_fingerprint",
                "last_error_fingerprint",
                "last_candidate_fingerprint",
                "repair_attempt_count",
                "latest_failure",
                "terminal_failure",
            ):
                value = terminal_diagnostic.get(key)
                if value not in (None, "", [], {}):
                    failure_projection[key] = (
                        dict(value) if isinstance(value, Mapping) else value
                    )
            failure_projection = {
                key: value
                for key, value in failure_projection.items()
                if value not in (None, "")
            }
            for key, value in failure_projection.items():
                final_result.setdefault(key, value)

        return {
            "task_id": task_id,
            "status": task.get("status", "running"),
            "started_at": task.get("started_at"),
            "last_updated_at": _now_iso(),
            "ended_at": task.get("ended_at"),
            "run_lease": dict(task.get("run_lease") or {}),
            "metadata": dict(task.get("metadata") or {}),
            "skills": dict(task.get("skills") or {}),
            "final_result": final_result,
            "failure": failure_projection,
            "summary": summary,
            "events": events,
        }

    def render_markdown(self, snapshot: Dict[str, Any]) -> str:
        summary = snapshot["summary"]
        metadata = snapshot.get("metadata") or {}
        skills = snapshot.get("skills") or {}
        final_result = snapshot.get("final_result") or {}
        token_note = "yes" if summary["tokens"].get("contains_estimates") else "no"
        cache_hit_rate = summary["tokens"].get("prompt_cache_hit_rate")
        cache_hit_rate_text = (
            f"{cache_hit_rate:.2%}" if cache_hit_rate is not None else "not reported"
        )
        cache_hit_tokens = summary["tokens"].get("prompt_cache_hit_tokens")
        cache_miss_tokens = summary["tokens"].get("prompt_cache_miss_tokens")
        cache_hit_text = str(cache_hit_tokens) if cache_hit_tokens is not None else "not reported"
        cache_miss_text = str(cache_miss_tokens) if cache_miss_tokens is not None else "not reported"
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
            f"| Corrections | {summary['correction_count']:.3f} | {summary['correction_count']} retry verdict(s) |",
            "",
            "## Counts",
            "",
            f"- LLM calls: {summary['llm_calls']}",
            f"- Commands: {summary['commands']} ({summary['successful_commands']} success, {summary['failed_commands']} failed)",
            f"- Local inspections (separate from approved Commands): {summary.get('local_inspections', 0)}, {summary.get('local_inspection_seconds', 0):.3f}s; components={summary.get('local_inspection_timing', {})}; scan is included in preparation; inspection is included in node/overhead time, not additive",
            f"- Confirmations: {summary['manual_confirmations']} manual, {summary['auto_confirmations']} automatic",
            f"- Review feedback/rejections: {summary['manual_feedback_or_rejection_count']}",
            f"- Tokens: prompt={summary['tokens']['prompt_tokens']}, completion={summary['tokens']['completion_tokens']}, total={summary['tokens']['total_tokens']} (contains estimates: {token_note})",
            f"- Prompt cache: hit={cache_hit_text}, miss={cache_miss_text}, rate={cache_hit_rate_text}, status={summary['tokens'].get('cache_metric_status', 'not_reported')}, reported calls={summary['tokens'].get('prompt_cache_usage_reported_calls', 0)}",
            "",
            "## Task Metadata",
            "",
            f"- Test content: {metadata.get('test_content') or 'none recorded'}",
            f"- Test prompt: {_shorten(metadata.get('test_prompt', ''), 300) or 'none recorded'}",
            f"- Test data: {_join_tsv(metadata.get('test_data')) or 'none recorded'}",
            f"- Data size: {metadata.get('data_size_bytes', 0)} bytes ({_format_bytes(metadata.get('data_size_bytes'))})",
            "",
            "## Skills",
            "",
            f"- Selected: {_join_tsv(skills.get('selected')) or 'none recorded'}",
            f"- Active: {_join_tsv(skills.get('active')) or 'none recorded'}",
            f"- Future: {_join_tsv(skills.get('future')) or 'none recorded'}",
            f"- Used: {_join_tsv(skills.get('used')) or 'none recorded'}",
            f"- Selection source: {skills.get('selection_source') or 'none recorded'}",
            "",
            "## Final Result",
            "",
            f"- Status: {final_result.get('status') or snapshot.get('status')}",
            f"- Output: {_shorten(final_result.get('output_result', ''), 600) or 'none recorded'}",
            f"- Output artifacts: {_join_tsv(final_result.get('output_artifacts')) or 'none recorded'}",
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
            elif event_type == "skill_usage":
                lines.append(f"- skill_usage [{node}] {event.get('usage_type')}: {_join_tsv(event.get('skills'))}")
            elif event_type == "skill_selection":
                lines.append(f"- skill_selection [{node}] selected={_join_tsv(event.get('selected'))}")
            elif event_type == "supervisor_verdict":
                lines.append(f"- supervisor [{node}] {event.get('verdict')} retry_count={event.get('retry_count')}")
            elif event_type == "final_result":
                lines.append(f"- final [{node}] {event.get('status')}: {_shorten(event.get('output_result', ''), 120)}")
        lines.append("")
        return "\n".join(lines)

    def _write_run_record(self, snapshot: Dict[str, Any], json_path: Path) -> None:
        record_path = self.output_root / "run_records.tsv"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        rows: List[Dict[str, str]] = []
        if record_path.exists():
            try:
                with record_path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle, delimiter="\t")
                    rows = [
                        {field: str(row.get(field, "") or "") for field in self.RUN_RECORD_FIELDS}
                        for row in reader
                        if row
                    ]
            except Exception:
                rows = []
        new_row = self._run_record_row(snapshot, json_path)
        replaced = False
        for index, row in enumerate(rows):
            if row.get("测试任务id") == new_row["测试任务id"]:
                rows[index] = new_row
                replaced = True
                break
        if not replaced:
            rows.append(new_row)
        with record_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.RUN_RECORD_FIELDS, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def _run_record_row(self, snapshot: Dict[str, Any], json_path: Path) -> Dict[str, str]:
        metadata = snapshot.get("metadata") or {}
        summary = snapshot.get("summary") or {}
        skills = snapshot.get("skills") or {}
        final_result = snapshot.get("final_result") or {}
        selected = skills.get("selected") or _merge_text(skills.get("active"), skills.get("future"))
        return {
            "测试时间": str(metadata.get("test_time") or snapshot.get("started_at") or ""),
            "测试任务id": str(metadata.get("test_task_id") or snapshot.get("task_id") or ""),
            "测试内容": str(metadata.get("test_content") or ""),
            "测试提示词": str(metadata.get("test_prompt") or "").replace("\t", " ").replace("\n", " "),
            "测试数据": _join_tsv(metadata.get("test_data")),
            "数据大小": str(_coerce_int(metadata.get("data_size_bytes"))),
            "选择skills": _join_tsv(selected),
            "使用skills": _join_tsv(skills.get("used")),
            "分析总用时": f"{float(summary.get('total_wall_seconds') or 0):.3f}",
            "计算用时": f"{float(summary.get('command_execution_seconds') or 0):.3f}",
            "纠错次数": str(_coerce_int(summary.get("correction_count"))),
            "人工确认时间": f"{float(summary.get('human_confirmation_wait_seconds') or 0):.3f}",
            "输出结果": str(final_result.get("output_result") or "").replace("\t", " ").replace("\n", " "),
            "状态": str(final_result.get("status") or snapshot.get("status") or ""),
            "run_metrics_json": str(json_path),
        }

    def _fallback_output_result(self, events: List[Dict[str, Any]]) -> str:
        for event in reversed(events):
            if event.get("type") == "final_result" and event.get("output_result"):
                return str(event.get("output_result"))
            if event.get("type") == "command" and event.get("success") and event.get("stdout_preview"):
                return _shorten(event.get("stdout_preview", ""), 800)
        return ""

    def _append_event(self, task_id: str, event: Dict[str, Any]) -> None:
        task = self._ensure_task(task_id)
        task["events"].append(event)
        now = _now_iso()
        task["last_updated_at"] = now
        task.setdefault("run_lease", {})["last_heartbeat_at"] = now

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
        finish_reason = _response_finish_reason(response)
        truncated = finish_reason == "length"
        self.profiler.record_llm(
            task_id=start["task_id"],
            node=start.get("node"),
            model=start.get("model", ""),
            duration_seconds=time.perf_counter() - start["started_monotonic"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            reasoning_tokens=usage["reasoning_tokens"],
            prompt_cache_hit_tokens=usage["prompt_cache_hit_tokens"],
            prompt_cache_miss_tokens=usage["prompt_cache_miss_tokens"],
            cache_metric_status=usage["cache_metric_status"],
            estimated=estimated,
            started_at=start.get("started_at"),
            finish_reason=finish_reason,
            provider_status="succeeded" if truncated else "",
            completion_integrity="truncated" if truncated else "",
            protocol_status="not_validated" if truncated else "",
            usage_source="provider_response" if not estimated else "provider_response_with_estimate",
        )

    def on_llm_error(self, error: BaseException, run_id: Any, **kwargs: Any) -> None:
        start = self._starts.pop(str(run_id), None)
        if not start:
            return
        completion = _exception_completion(error)
        finish_reason = _response_finish_reason(completion) if completion is not None else ""
        typed_length = (
            type(error).__name__ == "LengthFinishReasonError"
            or finish_reason == "length"
        ) and completion is not None
        if typed_length:
            usage = _response_usage(completion)
            partial_tokens_estimate = _estimate_tokens(_completion_text(completion))
            prompt_tokens = (
                usage["prompt_tokens"]
                if usage["prompt_tokens"] is not None
                else start["prompt_tokens_estimate"]
            )
            completion_tokens = (
                usage["completion_tokens"]
                if usage["completion_tokens"] is not None
                else partial_tokens_estimate
            )
            total_tokens = (
                usage["total_tokens"]
                if usage["total_tokens"] is not None
                else prompt_tokens + completion_tokens
            )
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
                reasoning_tokens=usage["reasoning_tokens"],
                prompt_cache_hit_tokens=usage["prompt_cache_hit_tokens"],
                prompt_cache_miss_tokens=usage["prompt_cache_miss_tokens"],
                cache_metric_status=usage["cache_metric_status"],
                estimated=estimated,
                error=type(error).__name__,
                started_at=start.get("started_at"),
                finish_reason=finish_reason or "length",
                provider_status="succeeded",
                completion_integrity="truncated",
                protocol_status="not_validated",
                usage_source=(
                    "provider_exception_completion"
                    if not estimated
                    else "provider_exception_completion_with_estimate"
                ),
            )
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
            usage_source="prompt_estimate_after_error",
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

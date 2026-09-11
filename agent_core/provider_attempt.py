"""Provider-attempt lifecycle, stream activity, cleanup, and fencing.

This module deliberately knows nothing about Generate parsing or controller
recovery.  One semantic invocation may dispatch at most a bounded number of
provider attempts, each with independent identity and telemetry.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from langchain_core.callbacks import BaseCallbackHandler

from agent_core.context_lineage import redact_payload

try:  # Optional at import time; qualified/MRO matching remains as a fallback.
    import httpcore as _httpcore
except Exception:  # pragma: no cover - exercised only in minimal installations.
    _httpcore = None

try:
    import httpx as _httpx
except Exception:  # pragma: no cover - exercised only in minimal installations.
    _httpx = None

try:
    import openai as _openai
except Exception:  # pragma: no cover - exercised only in minimal installations.
    _openai = None


logger = logging.getLogger(__name__)

PROVIDER_CONNECT_TIMEOUT = "PROVIDER_CONNECT_TIMEOUT"
PROVIDER_FIRST_CHUNK_TIMEOUT = "PROVIDER_FIRST_CHUNK_TIMEOUT"
PROVIDER_STREAM_IDLE_TIMEOUT = "PROVIDER_STREAM_IDLE_TIMEOUT"
PROVIDER_TOTAL_TIMEOUT = "PROVIDER_TOTAL_TIMEOUT"
PROVIDER_HTTP_ERROR = "PROVIDER_HTTP_ERROR"
PROVIDER_SDK_ERROR = "PROVIDER_SDK_ERROR"
PROVIDER_CONNECTION_FAILURE = "PROVIDER_CONNECTION_FAILURE"
PROVIDER_STREAM_READ_INTERRUPTED = "PROVIDER_STREAM_READ_INTERRUPTED"
EMPTY_FINAL_CONTENT = "EMPTY_FINAL_CONTENT"
GENERATE_PROVIDER_RETRY_EXHAUSTED = "GENERATE_PROVIDER_RETRY_EXHAUSTED"
GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR = "GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR"
PROVIDER_CANCEL_CLEANUP_FAILED = "PROVIDER_CANCEL_CLEANUP_FAILED"
PROVIDER_OUTCOME_UNKNOWN = "PROVIDER_OUTCOME_UNKNOWN"
PROVIDER_CANONICAL_CONTEXT_CHANGED = "PROVIDER_CANONICAL_CONTEXT_CHANGED"
PROVIDER_CALLER_CANCELLED = "PROVIDER_CALLER_CANCELLED"
PROVIDER_CLIENT_CANCELLED = "PROVIDER_CLIENT_CANCELLED"
PROVIDER_ATTEMPT_START_PERSISTENCE_FAILED = (
    "PROVIDER_ATTEMPT_START_PERSISTENCE_FAILED"
)

_MAX_EXCEPTION_CHAIN_DEPTH = 8
_MAX_EXCEPTION_CHAIN_NODES = 32
_MAX_EXCEPTION_FIELD_CHARS = 1200
_MAX_EXCEPTION_DIAGNOSTIC_CHARS = 32_000
_RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_SAFE_RESPONSE_HEADERS = {
    "date": "Date",
    "content-type": "Content-Type",
    "retry-after": "Retry-After",
    "request-id": "Request-Id",
    "x-request-id": "X-Request-Id",
    "openai-request-id": "OpenAI-Request-Id",
    "trace-id": "Trace-Id",
    "x-trace-id": "X-Trace-Id",
    "cf-ray": "CF-Ray",
}


def _available_exception_types(module: Any, *names: str) -> Tuple[type, ...]:
    if module is None:
        return ()
    return tuple(
        value
        for name in names
        if isinstance((value := getattr(module, name, None)), type)
    )


_HTTPX_CONNECTION_TYPES = _available_exception_types(
    _httpx,
    "ReadError", "ReadTimeout", "ConnectError", "ConnectTimeout",
    "RemoteProtocolError", "PoolTimeout",
)
_HTTPCORE_CONNECTION_TYPES = _available_exception_types(
    _httpcore,
    "ReadError", "ReadTimeout", "ConnectError", "ConnectTimeout",
    "RemoteProtocolError", "PoolTimeout",
)
_OPENAI_CONNECTION_TYPES = _available_exception_types(
    _openai,
    "APIConnectionError", "APITimeoutError",
)
_OPENAI_TERMINAL_REQUEST_TYPES = _available_exception_types(
    _openai,
    "BadRequestError", "AuthenticationError", "PermissionDeniedError",
    "NotFoundError", "UnprocessableEntityError",
)

CLEANUP_NOT_REQUIRED = "CLEANUP_NOT_REQUIRED"
CLEANUP_CONFIRMED = "CLEANUP_CONFIRMED"
CLEANUP_FAILED = "CLEANUP_FAILED"
CLEANUP_TIMEOUT = "CLEANUP_TIMEOUT"
CLEANUP_NOT_OBSERVABLE = "CLEANUP_NOT_OBSERVABLE"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProviderTimeoutPolicy:
    first_chunk_seconds: float
    idle_seconds: float
    total_seconds: float
    cleanup_seconds: float

    def normalized(self) -> "ProviderTimeoutPolicy":
        total = max(0.001, float(self.total_seconds))
        return ProviderTimeoutPolicy(
            first_chunk_seconds=min(total, max(0.001, float(self.first_chunk_seconds))),
            idle_seconds=min(total, max(0.001, float(self.idle_seconds))),
            total_seconds=total,
            cleanup_seconds=max(0.001, float(self.cleanup_seconds)),
        )

    def to_dict(self) -> Dict[str, float]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class ProviderAttemptEnvelope:
    node_name: str
    step_id: str
    semantic_invocation_id: str
    attempt_id: str
    attempt_ordinal: int
    parent_attempt_id: str
    call_id: str
    node_context_hash: str
    request_hash: str
    provider: str
    model: str
    thinking_policy: Mapping[str, Any]
    timeout_policy: ProviderTimeoutPolicy
    retry_budget_before: int
    retry_budget_after: int
    sdk_max_retries: int
    prompt_hash: str = ""
    model_parameter_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        row = asdict(self)
        row["thinking_policy"] = dict(self.thinking_policy or {})
        row["timeout_policy"] = self.timeout_policy.to_dict()
        row["sdk_internal_retry_observable"] = False
        row["sdk_internal_retry_count"] = None
        row["logical_invocation_id"] = self.semantic_invocation_id
        row["attempt_number"] = self.attempt_ordinal
        row["configured_max_attempts"] = (
            self.attempt_ordinal + self.retry_budget_before
        )
        return row


@dataclass
class ProviderAttemptRecord:
    envelope: ProviderAttemptEnvelope
    state: str = "ATTEMPT_STARTED"
    request_started_at: str = field(default_factory=utc_now)
    headers_received_at: Optional[str] = None
    first_any_chunk_at: Optional[str] = None
    first_reasoning_chunk_at: Optional[str] = None
    first_content_chunk_at: Optional[str] = None
    last_any_chunk_at: Optional[str] = None
    last_reasoning_chunk_at: Optional[str] = None
    last_content_chunk_at: Optional[str] = None
    total_chunk_count: int = 0
    reasoning_chunk_count: int = 0
    content_chunk_count: int = 0
    tool_call_fragment_count: int = 0
    observed_payload_callback_count: int = 0
    reasoning_char_count: int = 0
    content_char_count: int = 0
    observation_at: str = ""
    elapsed_seconds: float = 0.0
    payload_idle_seconds: Optional[float] = None
    max_payload_gap_seconds: float = 0.0
    activity_summary: list[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    provider_request_id: str = ""
    provider_trace_id: str = ""
    last_event_type: Optional[str] = None
    completion_marker_seen: bool = False
    stream_started: bool = False
    stream_completed: bool = False
    cancel_requested_at: str = ""
    cancel_completed_at: str = ""
    stream_close_requested_at: str = ""
    stream_close_completed_at: str = ""
    stream_close_supported: bool = False
    cleanup_result: str = CLEANUP_NOT_REQUIRED
    cleanup_failure_type: str = ""
    cancellation_source: str = ""
    attempt_completed_at: str = ""
    failure_type: str = ""
    failure_detail: str = ""
    caused_by: str = ""
    retryable: bool = False
    normalized_failure_reason: str = ""
    retry_budget_exhausted: bool = False
    terminal_failure_reason: str = ""
    retry_after_seconds: Optional[float] = None
    retry_delay_seconds: Optional[float] = None
    exception_at: Optional[str] = None
    exception_diagnostic: Dict[str, Any] = field(default_factory=dict)
    partial_payload_disposition: str = ""
    late_chunk_count: int = 0
    content_sha256: str = ""
    reasoning_sha256: str = ""
    usage_status: str = "UNKNOWN"
    usage_reported_at: str = ""
    provider_usage: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        record = {
            key: value
            for key, value in asdict(self).items()
            if key not in {"envelope", "exception_diagnostic"}
        }
        row = {
            **self.envelope.to_dict(),
            **record,
            **dict(self.exception_diagnostic or {}),
        }
        row.update({
            "normalized_error_type": self.failure_type or None,
            "attempt_retryable": self.retryable,
            "retry_decision_reason": self.normalized_failure_reason or None,
            "provider_attempt_id": self.envelope.attempt_id,
            "reasoning_callback_fragment_count": self.reasoning_chunk_count,
            "reasoning_character_count": self.reasoning_char_count,
            "content_callback_fragment_count": self.content_chunk_count,
            "content_character_count": self.content_char_count,
            "tool_call_fragment_count": self.tool_call_fragment_count,
            "observed_payload_callback_count": self.observed_payload_callback_count,
            "last_attempt_retryable": self.retryable,
            "first_event_at": self.first_any_chunk_at,
            "first_reasoning_at": self.first_reasoning_chunk_at,
            "first_content_at": self.first_content_chunk_at,
            "last_event_at": self.last_any_chunk_at,
            "model_name": self.envelope.model,
            # LangChain's ``ainvoke`` callback contract exposes token/chunk
            # activity but not the underlying HTTP response-head boundary.
            # Keep that absence explicit instead of treating a null timestamp
            # as evidence that no headers were received by the SDK.
            "headers_observation_status": (
                "OBSERVED" if self.headers_received_at else "NOT_OBSERVABLE"
            ),
            "headers_observation_reason": (
                ""
                if self.headers_received_at
                else "AINVOKE_CALLBACK_HEADERS_UNAVAILABLE"
            ),
            "time_to_headers_seconds": _elapsed_seconds(
                self.request_started_at, self.headers_received_at
            ),
            "time_to_first_event_seconds": _elapsed_seconds(
                self.request_started_at, self.first_any_chunk_at
            ),
            "time_to_first_reasoning_seconds": _elapsed_seconds(
                self.request_started_at, self.first_reasoning_chunk_at
            ),
            "time_to_first_content_seconds": _elapsed_seconds(
                self.request_started_at, self.first_content_chunk_at
            ),
            "time_since_last_event_seconds": _elapsed_seconds(
                self.last_any_chunk_at,
                self.exception_at or self.attempt_completed_at or self.observation_at,
            ),
            "total_elapsed_seconds": _elapsed_seconds(
                self.request_started_at, self.attempt_completed_at or self.observation_at
            ),
        })
        return row


def _elapsed_seconds(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        start_at = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, round((end_at - start_at).total_seconds(), 6))


def _response_provider_usage(response: Any) -> Tuple[str, Dict[str, int]]:
    """Return only provider-reported token counts, never local estimates.

    LangChain providers expose usage either on ``usage_metadata`` or inside
    ``response_metadata``. Missing fields stay absent: in particular, this
    helper never fills unknown usage with zero and never derives ``total``.
    """

    response_metadata = _safe_getattr(response, "response_metadata", {}) or {}
    candidates = (
        _safe_getattr(response, "usage_metadata", None),
        (
            response_metadata.get("token_usage")
            if isinstance(response_metadata, Mapping)
            else None
        ),
        (
            response_metadata.get("usage")
            if isinstance(response_metadata, Mapping)
            else None
        ),
    )
    raw_usage = next(
        (dict(item) for item in candidates if isinstance(item, Mapping) and item),
        {},
    )
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    normalized: Dict[str, int] = {}
    for target, source_keys in aliases.items():
        value = next(
            (raw_usage.get(key) for key in source_keys if key in raw_usage),
            None,
        )
        if isinstance(value, bool):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            normalized[target] = count
    if not normalized:
        return "UNKNOWN", {}
    if set(normalized) == set(aliases):
        return "REPORTED", normalized
    return "PARTIAL", normalized


class ProviderAttemptFailure(RuntimeError):
    """Typed provider failure before a completed response is committed."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        record: ProviderAttemptRecord,
        retryable: bool,
    ) -> None:
        super().__init__(f"{error_code}: {message}")
        self.error_code = str(error_code)
        self.record = record
        self.retryable = bool(retryable)
        self.error_owner = "PROVIDER"
        self.failure_stage = "GENERATE_PROVIDER"


class ProviderAttemptTimeout(ProviderAttemptFailure, TimeoutError):
    """Compatibility subtype for code that already catches ``TimeoutError``."""


class ProviderStreamObserver(BaseCallbackHandler):
    """Invocation-local metadata observer; routine state never stores body text."""

    run_inline = True

    def __init__(self, attempt_id: str, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.attempt_id = str(attempt_id)
        self._loop = loop
        self._lock = threading.Lock()
        self._activity_event = asyncio.Event()
        self._version = 0
        self._active = True
        self._late_chunk_count = 0
        self._first_any: Optional[str] = None
        self._first_reasoning: Optional[str] = None
        self._first_content: Optional[str] = None
        self._last_any: Optional[str] = None
        self._last_reasoning: Optional[str] = None
        self._last_content: Optional[str] = None
        self._last_activity_monotonic: Optional[float] = None
        self._max_payload_gap_seconds = 0.0
        self._total = 0
        self._reasoning = 0
        self._content = 0
        self._tool_calls = 0
        self._observed_payload_callbacks = 0
        self._reasoning_chars = 0
        self._content_chars = 0
        self._reasoning_hash = hashlib.sha256()
        self._content_hash = hashlib.sha256()
        self._finish_reason = ""
        self._provider_request_id = ""
        self._last_event_type: Optional[str] = None
        self._completion_marker_seen = False

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple, dict)):
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    @staticmethod
    def _chunk_message(chunk: Any) -> Any:
        return getattr(chunk, "message", chunk)

    @classmethod
    def _chunk_fields(
        cls,
        token: Any,
        chunk: Any,
    ) -> Tuple[str, str, int, str, str]:
        message = cls._chunk_message(chunk)
        additional = getattr(message, "additional_kwargs", {}) or {}
        response_metadata = getattr(message, "response_metadata", {}) or {}
        generation_info = getattr(chunk, "generation_info", {}) or {}
        reasoning = cls._text(
            additional.get("reasoning_content")
            or getattr(message, "reasoning_content", "")
        )
        content = cls._text(token) or cls._text(getattr(message, "content", ""))
        finish_reason = cls._text(
            generation_info.get("finish_reason")
            or response_metadata.get("finish_reason")
        )
        request_id = cls._text(
            response_metadata.get("request_id")
            or response_metadata.get("id")
            or additional.get("request_id")
            or getattr(message, "id", "")
        )
        tool_fragments = (
            getattr(message, "tool_call_chunks", None)
            or additional.get("tool_call_chunks")
            or additional.get("tool_calls")
            or getattr(message, "tool_calls", None)
            or []
        )
        tool_count = (
            len(tool_fragments)
            if isinstance(tool_fragments, (list, tuple))
            else int(bool(tool_fragments))
        )
        return reasoning, content, tool_count, finish_reason, request_id

    def on_llm_new_token(self, token: Any, **kwargs: Any) -> None:
        reasoning, content, tool_count, finish_reason, request_id = self._chunk_fields(
            token,
            kwargs.get("chunk"),
        )
        if not reasoning and not content and not tool_count:
            with self._lock:
                if finish_reason:
                    self._finish_reason = finish_reason
                    self._completion_marker_seen = True
                    self._last_event_type = "completion_marker"
                if request_id:
                    self._provider_request_id = request_id
            return
        now_iso = utc_now()
        now_monotonic = time.monotonic()
        with self._lock:
            if not self._active:
                self._late_chunk_count += 1
                return
            self._version += 1
            self._total += 1
            self._observed_payload_callbacks += 1
            self._first_any = self._first_any or now_iso
            self._last_any = now_iso
            if self._last_activity_monotonic is not None:
                self._max_payload_gap_seconds = max(
                    self._max_payload_gap_seconds,
                    max(0.0, now_monotonic - self._last_activity_monotonic),
                )
            self._last_activity_monotonic = now_monotonic
            if reasoning:
                raw = reasoning.encode("utf-8", errors="replace")
                self._reasoning += 1
                self._reasoning_chars += len(reasoning)
                self._reasoning_hash.update(raw)
                self._first_reasoning = self._first_reasoning or now_iso
                self._last_reasoning = now_iso
                self._last_event_type = "reasoning"
            if content:
                raw = content.encode("utf-8", errors="replace")
                self._content += 1
                self._content_chars += len(content)
                self._content_hash.update(raw)
                self._first_content = self._first_content or now_iso
                self._last_content = now_iso
                self._last_event_type = "content"
            if tool_count:
                self._tool_calls += tool_count
                self._last_event_type = "tool_call"
            if finish_reason:
                self._finish_reason = finish_reason
                self._completion_marker_seen = True
            if request_id:
                self._provider_request_id = request_id
        self._loop.call_soon_threadsafe(self._activity_event.set)

    def fence(self) -> None:
        with self._lock:
            self._active = False

    async def wait_for_activity(self, after_version: int) -> int:
        while True:
            with self._lock:
                current = self._version
            if current > after_version:
                return current
            self._activity_event.clear()
            with self._lock:
                current = self._version
            if current > after_version:
                return current
            await self._activity_event.wait()

    def complete_from_response(self, response: Any) -> None:
        metadata = getattr(response, "response_metadata", {}) or {}
        additional = getattr(response, "additional_kwargs", {}) or {}
        with self._lock:
            self._finish_reason = self._finish_reason or self._text(
                metadata.get("finish_reason")
            )
            self._completion_marker_seen = bool(
                self._completion_marker_seen or self._finish_reason
            )
            self._provider_request_id = self._provider_request_id or self._text(
                metadata.get("request_id")
                or metadata.get("id")
                or additional.get("request_id")
                or getattr(response, "id", "")
            )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "first_any_chunk_at": self._first_any,
                "first_reasoning_chunk_at": self._first_reasoning,
                "first_content_chunk_at": self._first_content,
                "last_any_chunk_at": self._last_any,
                "last_reasoning_chunk_at": self._last_reasoning,
                "last_content_chunk_at": self._last_content,
                "last_activity_monotonic": self._last_activity_monotonic,
                "max_payload_gap_seconds": round(self._max_payload_gap_seconds, 6),
                "total_chunk_count": self._total,
                "reasoning_chunk_count": self._reasoning,
                "content_chunk_count": self._content,
                "tool_call_fragment_count": self._tool_calls,
                "observed_payload_callback_count": self._observed_payload_callbacks,
                "reasoning_char_count": self._reasoning_chars,
                "content_char_count": self._content_chars,
                "finish_reason": self._finish_reason,
                "provider_request_id": self._provider_request_id,
                "late_chunk_count": self._late_chunk_count,
                "content_sha256": self._content_hash.hexdigest() if self._content else "",
                "reasoning_sha256": self._reasoning_hash.hexdigest() if self._reasoning else "",
                "last_event_type": self._last_event_type,
                "completion_marker_seen": self._completion_marker_seen,
                "version": self._version,
            }


@dataclass(frozen=True)
class ProviderExceptionClassification:
    error_code: str
    retryable: bool
    reason: str
    diagnostic: Mapping[str, Any]
    cancellation: Optional[BaseException] = None
    retry_after_seconds: Optional[float] = None


def _safe_getattr(value: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(value, name, default)
    except BaseException:
        return default


def _safe_exception_text(value: Any, *, representation: bool = False) -> str:
    try:
        raw = repr(value) if representation else str(value)
    except BaseException as exc:  # Never let broken __str__/__repr__ hide the failure.
        raw = f"<{type(value).__name__} unavailable: {type(exc).__name__}>"
    redacted, _ = redact_payload(raw)
    return str(redacted)[:_MAX_EXCEPTION_FIELD_CHARS]


def _qualified_exception_names(exc: BaseException) -> Tuple[str, ...]:
    names = []
    for cls in type(exc).__mro__[:12]:
        names.append(f"{cls.__module__}.{cls.__qualname__}")
    return tuple(names)


def _exception_children(exc: BaseException) -> Tuple[Tuple[str, BaseException], ...]:
    children = []
    cause = _safe_getattr(exc, "__cause__")
    context = _safe_getattr(exc, "__context__")
    if isinstance(cause, BaseException):
        children.append(("cause", cause))
    if isinstance(context, BaseException) and context is not cause:
        children.append(("context", context))
    group_children = _safe_getattr(exc, "exceptions", ())
    if isinstance(group_children, (list, tuple)):
        children.extend(
            (f"exception_group[{index}]", child)
            for index, child in enumerate(group_children[:8])
            if isinstance(child, BaseException)
        )
    return tuple(children)


def _walk_exception_chain(
    root: BaseException,
) -> Tuple[Tuple[BaseException, str, int], ...]:
    pending = [(root, "exception", 0)]
    seen = set()
    result = []
    while pending and len(result) < _MAX_EXCEPTION_CHAIN_NODES:
        current, relation, depth = pending.pop(0)
        identity = id(current)
        if identity in seen or depth > _MAX_EXCEPTION_CHAIN_DEPTH:
            continue
        seen.add(identity)
        result.append((current, relation, depth))
        if depth < _MAX_EXCEPTION_CHAIN_DEPTH:
            pending.extend(
                (child, child_relation, depth + 1)
                for child_relation, child in _exception_children(current)
            )
    return tuple(result)


def _exception_node_diagnostic(
    exc: BaseException,
    *,
    relation: str,
    depth: int,
) -> Dict[str, Any]:
    args = []
    for value in tuple(_safe_getattr(exc, "args", ()) or ())[:8]:
        if value is None or isinstance(value, (bool, int, float)):
            args.append(value)
        else:
            args.append(_safe_exception_text(value, representation=True)[:512])
    qualified = _qualified_exception_names(exc)
    return {
        "relation": relation,
        "depth": depth,
        "exception_class": type(exc).__name__,
        "exception_module": type(exc).__module__,
        "exception_qualified_name": qualified[0],
        "exception_mro": list(qualified),
        "exception_str": _safe_exception_text(exc),
        "exception_repr": _safe_exception_text(exc, representation=True),
        "exception_args": args,
    }


def _linear_exception_chain(root: BaseException, attribute: str) -> list[Dict[str, Any]]:
    result = []
    seen = {id(root)}
    current = _safe_getattr(root, attribute)
    depth = 1
    relation = "cause" if attribute == "__cause__" else "context"
    while (
        isinstance(current, BaseException)
        and id(current) not in seen
        and depth <= _MAX_EXCEPTION_CHAIN_DEPTH
    ):
        seen.add(id(current))
        result.append(
            _exception_node_diagnostic(current, relation=relation, depth=depth)
        )
        current = _safe_getattr(current, attribute)
        depth += 1
    return result


def _status_code(exc: BaseException) -> int:
    response = _safe_getattr(exc, "response")
    status = _safe_getattr(exc, "status_code") or _safe_getattr(
        response, "status_code"
    )
    try:
        return int(status) if status is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_response_headers(
    chain: Tuple[Tuple[BaseException, str, int], ...],
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for exc, _, _ in chain:
        response = _safe_getattr(exc, "response")
        headers = _safe_getattr(response, "headers") or _safe_getattr(exc, "headers")
        if not isinstance(headers, Mapping) and not hasattr(headers, "items"):
            continue
        try:
            items = list(headers.items())
        except BaseException:
            continue
        for raw_name, raw_value in items:
            canonical = _SAFE_RESPONSE_HEADERS.get(str(raw_name).casefold())
            if canonical and canonical not in result:
                result[canonical] = _safe_exception_text(raw_value)
    return result


def _retry_after_seconds(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        delay = float(text)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(text)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if 0.0 <= delay <= 60.0:
        return round(delay, 6)
    return None


def exception_diagnostic(exc: BaseException) -> Dict[str, Any]:
    chain = _walk_exception_chain(exc)
    root = _exception_node_diagnostic(exc, relation="exception", depth=0)
    headers = _safe_response_headers(chain)
    status = next((_status_code(item) for item, _, _ in chain if _status_code(item)), 0)
    request_id = headers.get("X-Request-Id") or headers.get("Request-Id") or headers.get(
        "OpenAI-Request-Id", ""
    )
    trace_id = headers.get("X-Trace-Id") or headers.get("Trace-Id") or headers.get(
        "CF-Ray", ""
    )
    diagnostic = {
        **root,
        "exception_chain": [
            _exception_node_diagnostic(item, relation=relation, depth=depth)
            for item, relation, depth in chain
        ],
        "exception_cause_chain": _linear_exception_chain(exc, "__cause__"),
        "exception_context_chain": _linear_exception_chain(exc, "__context__"),
        "http_status_code": status or None,
        "http_response_headers": headers,
        "provider_request_id_from_headers": request_id or None,
        "provider_trace_id_from_headers": trace_id or None,
        "retry_after_seconds": _retry_after_seconds(headers.get("Retry-After")),
        "exception_chain_depth_limit": _MAX_EXCEPTION_CHAIN_DEPTH,
        "exception_chain_cycle_safe": True,
    }
    safe, _ = redact_payload(diagnostic)
    safe = dict(safe)
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > _MAX_EXCEPTION_DIAGNOSTIC_CHARS:
        def compact(nodes: Any, limit: int) -> list[Dict[str, Any]]:
            return [
                {
                    "relation": node.get("relation"),
                    "depth": node.get("depth"),
                    "exception_class": node.get("exception_class"),
                    "exception_module": node.get("exception_module"),
                    "exception_qualified_name": node.get("exception_qualified_name"),
                    "exception_str": str(node.get("exception_str") or "")[:256],
                }
                for node in list(nodes or [])[:limit]
                if isinstance(node, Mapping)
            ]

        safe["exception_chain"] = compact(safe.get("exception_chain"), 8)
        safe["exception_cause_chain"] = compact(
            safe.get("exception_cause_chain"), 4
        )
        safe["exception_context_chain"] = compact(
            safe.get("exception_context_chain"), 4
        )
        safe["exception_str"] = str(safe.get("exception_str") or "")[:512]
        safe["exception_repr"] = str(safe.get("exception_repr") or "")[:512]
        safe["exception_args"] = list(safe.get("exception_args") or [])[:2]
        safe["exception_diagnostic_truncated"] = True
    else:
        safe["exception_diagnostic_truncated"] = False
    return safe


def inspect_provider_exception(
    exc: BaseException,
    *,
    stream_started: bool = False,
) -> ProviderExceptionClassification:
    chain = _walk_exception_chain(exc)
    diagnostic = exception_diagnostic(exc)
    retry_after = diagnostic.get("retry_after_seconds")

    for item, _, _ in chain:
        if isinstance(item, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            return ProviderExceptionClassification(
                PROVIDER_SDK_ERROR,
                False,
                "cancellation_or_shutdown",
                diagnostic,
                cancellation=item,
            )

    status = next((_status_code(item) for item, _, _ in chain if _status_code(item)), 0)
    if status:
        if status in _RETRYABLE_HTTP_STATUSES:
            return ProviderExceptionClassification(
                PROVIDER_HTTP_ERROR,
                True,
                f"retryable_http_status_{status}",
                diagnostic,
                retry_after_seconds=retry_after,
            )
        return ProviderExceptionClassification(
            GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR,
            False,
            f"terminal_http_status_{status}",
            diagnostic,
        )

    if any(isinstance(item, _OPENAI_TERMINAL_REQUEST_TYPES) for item, _, _ in chain):
        return ProviderExceptionClassification(
            GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR,
            False,
            "terminal_provider_request_exception",
            diagnostic,
        )

    transport_types = (
        _HTTPX_CONNECTION_TYPES
        + _HTTPCORE_CONNECTION_TYPES
        + _OPENAI_CONNECTION_TYPES
    )
    if transport_types and any(isinstance(item, transport_types) for item, _, _ in chain):
        return ProviderExceptionClassification(
            PROVIDER_STREAM_READ_INTERRUPTED if stream_started else PROVIDER_CONNECTION_FAILURE,
            True,
            "transport_read_interrupted" if stream_started else "transport_connection_failure",
            diagnostic,
            retry_after_seconds=retry_after,
        )

    qualified_text = " ".join(
        " ".join(_qualified_exception_names(item))
        for item, _, _ in chain
    ).casefold()
    if any(marker in qualified_text for marker in (
        "readerror", "readtimeout", "connecterror", "connecttimeout",
        "remoteprotocolerror", "pooltimeout", "apiconnectionerror", "apitimeouterror",
    )):
        return ProviderExceptionClassification(
            PROVIDER_STREAM_READ_INTERRUPTED if stream_started else PROVIDER_CONNECTION_FAILURE,
            True,
            "qualified_transport_exception_fallback",
            diagnostic,
            retry_after_seconds=retry_after,
        )

    # Last-resort message matching is deliberately after isinstance and MRO checks.
    text = " ".join(
        f"{type(item).__name__}: {_safe_exception_text(item)}"
        for item, _, _ in chain
    ).casefold()
    if any(marker in text for marker in (
        "authentication", "invalid api key", "unauthorized", "permission denied",
        "content_filter", "input data may contain", "invalid model", "context length",
        "malformed request", "badrequesterror",
    )):
        return ProviderExceptionClassification(
            GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR,
            False,
            "terminal_request_message_fallback",
            diagnostic,
        )
    if any(marker in text for marker in (
        "connection error", "connection reset", "timed out", "timeout",
        "temporarily unavailable", "rate limit", "remote protocol",
    )):
        return ProviderExceptionClassification(
            PROVIDER_STREAM_READ_INTERRUPTED if stream_started else PROVIDER_CONNECTION_FAILURE,
            True,
            "transport_message_fallback",
            diagnostic,
            retry_after_seconds=retry_after,
        )
    return ProviderExceptionClassification(
        PROVIDER_SDK_ERROR,
        False,
        "unclassified_provider_sdk_error",
        diagnostic,
    )


def classify_provider_exception(exc: BaseException) -> Tuple[str, bool]:
    classification = inspect_provider_exception(exc)
    return classification.error_code, classification.retryable


def provider_retry_delay_seconds(
    record: ProviderAttemptRecord,
    *,
    attempt_ordinal: int,
    jitter_sample: float,
    base_seconds: float = 0.5,
    cap_seconds: float = 8.0,
) -> float:
    if record.retry_after_seconds is not None:
        return min(60.0, max(0.0, float(record.retry_after_seconds)))
    jitter = 0.75 + (0.25 * min(1.0, max(0.0, float(jitter_sample))))
    bounded_cap = min(8.0, max(0.0, float(cap_seconds)))
    exponential = min(
        bounded_cap,
        max(0.0, float(base_seconds)) * (2 ** max(0, int(attempt_ordinal) - 1)),
    )
    return round(exponential * jitter, 6)


def _apply_observer(
    record: ProviderAttemptRecord,
    observer: ProviderStreamObserver,
    *,
    started_monotonic: Optional[float] = None,
) -> None:
    state = observer.snapshot()
    for key in (
        "first_any_chunk_at", "first_reasoning_chunk_at", "first_content_chunk_at",
        "last_any_chunk_at", "last_reasoning_chunk_at", "last_content_chunk_at",
        "total_chunk_count", "reasoning_chunk_count", "content_chunk_count",
        "tool_call_fragment_count", "observed_payload_callback_count",
        "reasoning_char_count", "content_char_count", "finish_reason",
        "provider_request_id", "late_chunk_count", "content_sha256",
        "reasoning_sha256", "last_event_type", "completion_marker_seen",
        "max_payload_gap_seconds",
    ):
        setattr(record, key, state[key])
    record.stream_started = bool(state["observed_payload_callback_count"])
    if started_monotonic is not None:
        now = time.monotonic()
        record.observation_at = utc_now()
        record.elapsed_seconds = round(max(0.0, now - started_monotonic), 6)
        last_payload = state["last_activity_monotonic"]
        record.payload_idle_seconds = (
            round(max(0.0, now - last_payload), 6)
            if last_payload is not None else None
        )
        # Bounded metadata samples preserve visible activity and open gaps.
        # Sampling is strictly read-only with respect to the observer: it never
        # changes the payload version, timestamps, counters or watchdog clock.
        record.activity_summary.append({
            "observation_at": record.observation_at,
            "elapsed_seconds": record.elapsed_seconds,
            "payload_idle_seconds": record.payload_idle_seconds,
            "content_char_count": record.content_char_count,
            "reasoning_char_count": record.reasoning_char_count,
            "observed_payload_callback_count": record.observed_payload_callback_count,
        })
        record.activity_summary = record.activity_summary[-32:]


def _emit(
    callback: Optional[Callable[[str, Mapping[str, Any]], None]],
    event: str,
    record: ProviderAttemptRecord,
    *,
    fail_closed: bool = False,
) -> None:
    payload = record.to_dict()
    # Routine logs are an operational state channel, not an exception/body
    # archive.  Exact failure text remains available in the protected lineage
    # snapshot while this payload stays metadata-only.
    payload.pop("failure_detail", None)
    for key in tuple(payload):
        if key.startswith("exception_") or key == "http_response_headers":
            payload.pop(key, None)
    payload["event"] = event
    from agent_core.cli_display import emit as emit_display
    scope = {"node": str(payload.get("node_name") or ""),
             "call_id": str(payload.get("call_id") or ""),
             "provider_attempt_id": str(payload.get("provider_attempt_id") or payload.get("attempt_id") or ""),
             "event": event, "source": "runtime_fact"}
    if event in {"attempt_started", "attempt_progress", "stream_active"}:
        observed = bool(payload.get("observed_payload_callback_count") or payload.get("first_content_chunk_at")
                        or payload.get("first_reasoning_chunk_at") or payload.get("last_payload_at"))
        emit_display("activity", "已接收模型响应活动" if observed else "等待模型返回", **scope)
    elif event == "attempt_cancelled":
        emit_display("cancel", f"provider 取消回执：{payload.get('cleanup_result', '未确认')}；工具与任务清理另行确认。", **scope)
    elif event == "attempt_failed":
        emit_display("error", f"provider attempt 失败：{payload.get('failure_type') or 'UNKNOWN'}", **scope)
    if event in {"attempt_completed", "attempt_cancelled", "attempt_failed"}:
        emit_display("provider_fenced", "", **{**scope, "event": "fenced"})
    try:
        logger.info("PROVIDER_ATTEMPT_EVENT %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        # Operational logs are observers, including the initial marker. The
        # separate callback can still require durable dispatch acceptance.
        pass
    if callback is not None:
        try:
            callback(event, payload)
        except Exception:
            if fail_closed:
                raise
            try:
                logger.debug("Provider progress callback failed", exc_info=True)
            except Exception:
                pass


async def _cancel_and_drain(
    task: "asyncio.Task[Any]",
    observer: ProviderStreamObserver,
    record: ProviderAttemptRecord,
    cleanup_seconds: float,
) -> str:
    deadline = time.monotonic() + max(0.001, cleanup_seconds)
    observer.fence()
    record.cancel_requested_at = utc_now()
    record.state = "CANCEL_REQUESTED"
    task.cancel()
    try:
        done, _ = await asyncio.wait(
            {task},
            timeout=max(0.001, deadline - time.monotonic()),
        )
    except BaseException:
        record.cleanup_result = CLEANUP_FAILED
        record.cancel_completed_at = utc_now()
        return CLEANUP_FAILED
    if not done:
        record.cleanup_result = CLEANUP_TIMEOUT
        return CLEANUP_TIMEOUT
    try:
        task.result()
    except BaseException:
        pass
    record.cancel_completed_at = utc_now()
    record.cleanup_result = CLEANUP_CONFIRMED
    return CLEANUP_CONFIRMED


async def run_provider_attempt(
    invoke: Callable[[ProviderStreamObserver], Awaitable[Any]],
    envelope: ProviderAttemptEnvelope,
    *,
    progress_callback: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    progress_start_fail_closed: bool = False,
    observation_interval_seconds: float = 5.0,
) -> Tuple[Any, ProviderAttemptRecord]:
    """Run one provider dispatch with phased watchdogs and bounded cleanup."""

    loop = asyncio.get_running_loop()
    policy = envelope.timeout_policy.normalized()
    observer = ProviderStreamObserver(envelope.attempt_id, loop)
    record = ProviderAttemptRecord(envelope=envelope)
    activity_version = 0
    stream_announced = False
    try:
        observation_interval = float(observation_interval_seconds)
    except (TypeError, ValueError):
        observation_interval = 5.0
    if not math.isfinite(observation_interval):
        observation_interval = 5.0
    # Logging/output can itself be slow under a captured terminal.  Emit the
    # lifecycle marker before starting the provider clock so observer overhead
    # cannot consume the first-chunk allowance before dispatch is scheduled.
    try:
        _emit(
            progress_callback,
            "attempt_started",
            record,
            fail_closed=progress_start_fail_closed,
        )
    except Exception as exc:
        record.failure_type = PROVIDER_ATTEMPT_START_PERSISTENCE_FAILED
        record.failure_detail = type(exc).__name__
        record.normalized_failure_reason = "attempt_start_persistence_failed"
        record.exception_at = utc_now()
        record.attempt_completed_at = utc_now()
        record.retryable = False
        record.state = "FAILED_TERMINAL"
        raise ProviderAttemptFailure(
            record.failure_type,
            "provider attempt start could not be persisted",
            record=record,
            retryable=False,
        ) from exc
    started_monotonic = time.monotonic()
    next_observation = (
        started_monotonic + observation_interval
        if observation_interval > 0 else None
    )
    invoke_task = asyncio.create_task(invoke(observer), name=envelope.attempt_id)
    try:
        await asyncio.sleep(0)
        while True:
            state = observer.snapshot()
            total_remaining = policy.total_seconds - (time.monotonic() - started_monotonic)
            last_activity = state["last_activity_monotonic"]
            if last_activity is None:
                phase_remaining = policy.first_chunk_seconds - (
                    time.monotonic() - started_monotonic
                )
                timeout_code = PROVIDER_FIRST_CHUNK_TIMEOUT
            else:
                phase_remaining = policy.idle_seconds - (time.monotonic() - last_activity)
                timeout_code = PROVIDER_STREAM_IDLE_TIMEOUT
            if total_remaining <= 0 or phase_remaining <= 0:
                selected = (
                    PROVIDER_TOTAL_TIMEOUT
                    if total_remaining <= phase_remaining
                    else timeout_code
                )
                _apply_observer(record, observer, started_monotonic=started_monotonic)
                record.failure_type = selected
                record.normalized_failure_reason = "provider_watchdog_timeout"
                record.exception_at = utc_now()
                if record.stream_started:
                    record.partial_payload_disposition = "discarded_before_parse"
                record.state = "TIMED_OUT"
                _emit(progress_callback, "timeout_cleanup_started", record)
                cleanup = await _cancel_and_drain(
                    invoke_task,
                    observer,
                    record,
                    policy.cleanup_seconds,
                )
                _apply_observer(record, observer, started_monotonic=started_monotonic)
                record.attempt_completed_at = utc_now()
                if cleanup != CLEANUP_CONFIRMED:
                    record.cleanup_failure_type = PROVIDER_CANCEL_CLEANUP_FAILED
                    record.caused_by = PROVIDER_CANCEL_CLEANUP_FAILED
                    record.failure_type = PROVIDER_OUTCOME_UNKNOWN
                    record.retryable = False
                    record.state = "OUTCOME_UNKNOWN"
                    _emit(progress_callback, "attempt_failed", record)
                    raise ProviderAttemptFailure(
                        PROVIDER_OUTCOME_UNKNOWN,
                        f"{selected}; cancellation cleanup did not complete ({cleanup})",
                        record=record,
                        retryable=False,
                    )
                record.retryable = True
                record.state = "FAILED_TRANSIENT"
                _emit(progress_callback, "attempt_failed", record)
                raise ProviderAttemptTimeout(
                    selected,
                    f"{envelope.node_name} provider attempt timed out",
                    record=record,
                    retryable=True,
                )

            activity_task = asyncio.create_task(observer.wait_for_activity(activity_version))
            wait_seconds = min(total_remaining, phase_remaining)
            if next_observation is not None:
                wait_seconds = min(wait_seconds, next_observation - time.monotonic())
            try:
                done, _ = await asyncio.wait(
                    {invoke_task, activity_task},
                    timeout=max(0.001, wait_seconds),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not activity_task.done():
                    activity_task.cancel()
                    try:
                        await activity_task
                    except asyncio.CancelledError:
                        # The activity waiter is disposable; its caller is
                        # not. A caller cancellation can arrive at this drain
                        # suspension after asyncio.wait already returned.
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            raise
                    except BaseException:
                        pass
            if invoke_task in done:
                try:
                    response = invoke_task.result()
                except asyncio.CancelledError as exc:
                    _apply_observer(record, observer, started_monotonic=started_monotonic)
                    observer.fence()
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        # Cancellation belongs to the caller.  The outer handler
                        # owns bounded provider cleanup and emits the durable
                        # terminal attempt event before propagation.
                        raise
                    # The provider/client task cancelled itself while the
                    # semantic caller remains live.  This is a transport
                    # outcome, not user cancellation, and is therefore exposed
                    # through the typed provider-failure contract.
                    record.failure_type = PROVIDER_CLIENT_CANCELLED
                    record.failure_detail = "provider/client invocation cancelled itself"
                    record.retryable = True
                    record.normalized_failure_reason = "provider_client_cancelled"
                    record.exception_at = utc_now()
                    record.cleanup_result = CLEANUP_CONFIRMED
                    record.attempt_completed_at = utc_now()
                    record.state = "FAILED_TRANSIENT"
                    _emit(progress_callback, "attempt_failed", record)
                    raise ProviderAttemptTimeout(
                        PROVIDER_CLIENT_CANCELLED,
                        "provider request was cancelled by the provider/client",
                        record=record,
                        retryable=True,
                    ) from exc
                except BaseException as exc:
                    _apply_observer(record, observer, started_monotonic=started_monotonic)
                    observer.fence()
                    classification = inspect_provider_exception(
                        exc,
                        stream_started=record.stream_started,
                    )
                    if classification.cancellation is not None:
                        raise classification.cancellation
                    record.failure_type = classification.error_code
                    record.failure_detail = _safe_exception_text(exc)
                    record.retryable = classification.retryable
                    record.normalized_failure_reason = classification.reason
                    record.retry_after_seconds = classification.retry_after_seconds
                    record.exception_at = utc_now()
                    record.exception_diagnostic = dict(classification.diagnostic)
                    record.provider_request_id = record.provider_request_id or str(
                        classification.diagnostic.get("provider_request_id_from_headers")
                        or ""
                    )
                    record.provider_trace_id = str(
                        classification.diagnostic.get("provider_trace_id_from_headers")
                        or ""
                    )
                    if record.stream_started:
                        record.partial_payload_disposition = "discarded_before_parse"
                    record.cleanup_result = CLEANUP_CONFIRMED
                    record.attempt_completed_at = utc_now()
                    record.state = (
                        "FAILED_TRANSIENT"
                        if classification.retryable
                        else "FAILED_TERMINAL"
                    )
                    _emit(progress_callback, "attempt_failed", record)
                    failure_class = ProviderAttemptTimeout if isinstance(exc, TimeoutError) else ProviderAttemptFailure
                    raise failure_class(
                        classification.error_code,
                        record.failure_detail,
                        record=record,
                        retryable=classification.retryable,
                    ) from exc
                observer.complete_from_response(response)
                observer.fence()
                _apply_observer(record, observer, started_monotonic=started_monotonic)
                record.usage_status, record.provider_usage = (
                    _response_provider_usage(response)
                )
                if record.usage_status != "UNKNOWN":
                    record.usage_reported_at = utc_now()
                record.state = "COMPLETED"
                record.stream_completed = True
                record.cleanup_result = CLEANUP_NOT_REQUIRED
                record.attempt_completed_at = utc_now()
                _emit(progress_callback, "attempt_completed", record)
                return response, record
            state = observer.snapshot()
            activity_version = int(state["version"])
            if activity_version and not stream_announced:
                stream_announced = True
                record.state = "STREAM_ACTIVE"
                _apply_observer(record, observer, started_monotonic=started_monotonic)
                _emit(progress_callback, "stream_active", record)
                if next_observation is not None:
                    next_observation = time.monotonic() + observation_interval
            elif next_observation is not None and time.monotonic() >= next_observation:
                # An observation wake-up is not provider activity. Expired
                # watchdogs are handled by the original branch on the next
                # loop, before any periodic UI event can claim ongoing work.
                now = time.monotonic()
                last_payload = state["last_activity_monotonic"]
                phase_deadline = (
                    last_payload + policy.idle_seconds
                    if last_payload is not None
                    else started_monotonic + policy.first_chunk_seconds
                )
                if now < min(started_monotonic + policy.total_seconds, phase_deadline):
                    _apply_observer(record, observer, started_monotonic=started_monotonic)
                    _emit(progress_callback, "attempt_progress", record)
                next_observation = time.monotonic() + observation_interval
    except asyncio.CancelledError as exc:
        cleanup = record.cleanup_result
        if not invoke_task.done():
            cleanup = await _cancel_and_drain(
                invoke_task,
                observer,
                record,
                policy.cleanup_seconds,
            )
        else:
            # Consume a completed task so asyncio does not report an orphaned
            # exception while the caller's cancellation remains authoritative.
            try:
                invoke_task.result()
            except BaseException:
                pass
            cleanup = CLEANUP_NOT_REQUIRED
        observer.fence()
        _apply_observer(record, observer, started_monotonic=started_monotonic)
        record.cancellation_source = "caller"
        record.retryable = False
        record.normalized_failure_reason = "caller_cancelled"
        record.exception_at = record.exception_at or utc_now()
        if record.stream_started:
            record.partial_payload_disposition = "discarded_after_caller_cancellation"
        if cleanup in {CLEANUP_CONFIRMED, CLEANUP_NOT_REQUIRED}:
            record.failure_type = PROVIDER_CALLER_CANCELLED
            record.state = "CANCELLED"
        else:
            record.caused_by = PROVIDER_CALLER_CANCELLED
            record.cleanup_failure_type = PROVIDER_CANCEL_CLEANUP_FAILED
            record.failure_type = PROVIDER_OUTCOME_UNKNOWN
            record.state = "OUTCOME_UNKNOWN"
        record.attempt_completed_at = utc_now()
        _emit(progress_callback, "attempt_cancelled", record)
        # Preserve the original cancellation identity while allowing the
        # orchestration adapter to close its durable call manifest.
        try:
            setattr(exc, "provider_attempt_record", record)
        except Exception:
            pass
        raise


def partial_response_diagnostic(record: ProviderAttemptRecord) -> Dict[str, Any]:
    """Return metadata-only partial-response evidence; never executable text."""

    return {
        "schema_version": 2,
        "classification": "DIAGNOSTIC_ONLY",
        "complete": False,
        "executable": False,
        "semantic_invocation_id": record.envelope.semantic_invocation_id,
        "attempt_id": record.envelope.attempt_id,
        "call_id": record.envelope.call_id,
        "request_hash": record.envelope.request_hash,
        "chunk_counts": {
            "total": record.total_chunk_count,
            "reasoning_callback_fragment_count": record.reasoning_chunk_count,
            "reasoning_character_count": record.reasoning_char_count,
            "content_callback_fragment_count": record.content_chunk_count,
            "content_character_count": record.content_char_count,
            "tool_call_fragment_count": record.tool_call_fragment_count,
            "observed_payload_callback_count": record.observed_payload_callback_count,
        },
        "first_any_chunk_at": record.first_any_chunk_at,
        "last_any_chunk_at": record.last_any_chunk_at,
        "finish_reason": None,
        "failure_type": record.failure_type,
        "cleanup_result": record.cleanup_result,
        "truncation": {"raw_content_persisted": False},
        "content_sha256": record.content_sha256,
        "reasoning_sha256": record.reasoning_sha256,
        "provider_attempt": record.to_dict(),
    }


def persist_partial_response_diagnostic(path: str | Path, record: ProviderAttemptRecord) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = partial_response_diagnostic(record)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(target)
    return target


def recovery_connection_retry_allowed(error):
    """Recovery-only policy: no accepted/partial payload, bounded exception chain."""
    if error.error_code != PROVIDER_CONNECTION_FAILURE or not error.retryable:
        return False
    record = error.record
    if record.observed_payload_callback_count or record.tool_call_fragment_count:
        return False
    current = error
    seen = set()
    for _ in range(16):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__.lower()
        text = str(current).lower()
        if any(x in name for x in ('invalidurl', 'unsupportedprotocol', 'localprotocol', 'ssl', 'certificate', 'authentication', 'permission', 'readerror', 'readtimeout', 'writeerror', 'writetimeout')):
            return False
        if any(x in text for x in ('certificate verify failed', 'invalid url', 'unknown url type')):
            return False
        current = current.__cause__ or current.__context__
    return True

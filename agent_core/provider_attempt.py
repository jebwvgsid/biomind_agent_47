"""Provider-attempt lifecycle, stream activity, cleanup, and fencing.

This module deliberately knows nothing about Generate parsing or controller
recovery.  One semantic invocation may dispatch at most a bounded number of
provider attempts, each with independent identity and telemetry.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from langchain_core.callbacks import BaseCallbackHandler


logger = logging.getLogger(__name__)

PROVIDER_CONNECT_TIMEOUT = "PROVIDER_CONNECT_TIMEOUT"
PROVIDER_FIRST_CHUNK_TIMEOUT = "PROVIDER_FIRST_CHUNK_TIMEOUT"
PROVIDER_STREAM_IDLE_TIMEOUT = "PROVIDER_STREAM_IDLE_TIMEOUT"
PROVIDER_TOTAL_TIMEOUT = "PROVIDER_TOTAL_TIMEOUT"
PROVIDER_HTTP_ERROR = "PROVIDER_HTTP_ERROR"
PROVIDER_SDK_ERROR = "PROVIDER_SDK_ERROR"
PROVIDER_CANCEL_CLEANUP_FAILED = "PROVIDER_CANCEL_CLEANUP_FAILED"
PROVIDER_OUTCOME_UNKNOWN = "PROVIDER_OUTCOME_UNKNOWN"
PROVIDER_CANONICAL_CONTEXT_CHANGED = "PROVIDER_CANONICAL_CONTEXT_CHANGED"

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

    def to_dict(self) -> Dict[str, Any]:
        row = asdict(self)
        row["thinking_policy"] = dict(self.thinking_policy or {})
        row["timeout_policy"] = self.timeout_policy.to_dict()
        row["sdk_internal_retry_observable"] = False
        row["sdk_internal_retry_count"] = None
        return row


@dataclass
class ProviderAttemptRecord:
    envelope: ProviderAttemptEnvelope
    state: str = "ATTEMPT_STARTED"
    request_started_at: str = field(default_factory=utc_now)
    headers_received_at: str = ""
    first_any_chunk_at: str = ""
    first_reasoning_chunk_at: str = ""
    first_content_chunk_at: str = ""
    last_any_chunk_at: str = ""
    last_reasoning_chunk_at: str = ""
    last_content_chunk_at: str = ""
    total_chunk_count: int = 0
    reasoning_chunk_count: int = 0
    content_chunk_count: int = 0
    reasoning_char_count: int = 0
    content_char_count: int = 0
    finish_reason: str = ""
    provider_request_id: str = ""
    cancel_requested_at: str = ""
    cancel_completed_at: str = ""
    stream_close_requested_at: str = ""
    stream_close_completed_at: str = ""
    stream_close_supported: bool = False
    cleanup_result: str = CLEANUP_NOT_REQUIRED
    cleanup_failure_type: str = ""
    attempt_completed_at: str = ""
    failure_type: str = ""
    failure_detail: str = ""
    caused_by: str = ""
    retryable: bool = False
    late_chunk_count: int = 0
    content_sha256: str = ""
    reasoning_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {**self.envelope.to_dict(), **{
            key: value
            for key, value in asdict(self).items()
            if key != "envelope"
        }}


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
        self._first_any = ""
        self._first_reasoning = ""
        self._first_content = ""
        self._last_any = ""
        self._last_reasoning = ""
        self._last_content = ""
        self._last_activity_monotonic: Optional[float] = None
        self._total = 0
        self._reasoning = 0
        self._content = 0
        self._reasoning_chars = 0
        self._content_chars = 0
        self._reasoning_hash = hashlib.sha256()
        self._content_hash = hashlib.sha256()
        self._finish_reason = ""
        self._provider_request_id = ""

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
    def _chunk_fields(cls, token: Any, chunk: Any) -> Tuple[str, str, str, str]:
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
        return reasoning, content, finish_reason, request_id

    def on_llm_new_token(self, token: Any, **kwargs: Any) -> None:
        reasoning, content, finish_reason, request_id = self._chunk_fields(
            token,
            kwargs.get("chunk"),
        )
        if not reasoning and not content:
            with self._lock:
                if finish_reason:
                    self._finish_reason = finish_reason
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
            self._first_any = self._first_any or now_iso
            self._last_any = now_iso
            self._last_activity_monotonic = now_monotonic
            if reasoning:
                raw = reasoning.encode("utf-8", errors="replace")
                self._reasoning += 1
                self._reasoning_chars += len(reasoning)
                self._reasoning_hash.update(raw)
                self._first_reasoning = self._first_reasoning or now_iso
                self._last_reasoning = now_iso
            if content:
                raw = content.encode("utf-8", errors="replace")
                self._content += 1
                self._content_chars += len(content)
                self._content_hash.update(raw)
                self._first_content = self._first_content or now_iso
                self._last_content = now_iso
            if finish_reason:
                self._finish_reason = finish_reason
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
                "total_chunk_count": self._total,
                "reasoning_chunk_count": self._reasoning,
                "content_chunk_count": self._content,
                "reasoning_char_count": self._reasoning_chars,
                "content_char_count": self._content_chars,
                "finish_reason": self._finish_reason,
                "provider_request_id": self._provider_request_id,
                "late_chunk_count": self._late_chunk_count,
                "content_sha256": self._content_hash.hexdigest() if self._content else "",
                "reasoning_sha256": self._reasoning_hash.hexdigest() if self._reasoning else "",
                "version": self._version,
            }


def classify_provider_exception(exc: BaseException) -> Tuple[str, bool]:
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    try:
        status_code = int(status) if status is not None else 0
    except (TypeError, ValueError):
        status_code = 0
    text = f"{type(exc).__name__}: {exc}".casefold()
    if status_code:
        retryable = status_code in {408, 409, 425, 429, 500, 502, 503, 504}
        return PROVIDER_HTTP_ERROR, retryable
    if "connecttimeout" in text or "connection timeout" in text:
        return PROVIDER_CONNECT_TIMEOUT, True
    if any(marker in text for marker in (
        "authentication", "invalid api key", "unauthorized", "permission denied",
        "content_filter", "input data may contain", "badrequesterror",
    )):
        return PROVIDER_SDK_ERROR, False
    retryable = any(marker in text for marker in (
        "apiconnectionerror", "connection error", "connection reset",
        "timed out", "timeout", "temporarily unavailable", "rate limit",
    ))
    return PROVIDER_SDK_ERROR, retryable


def _apply_observer(record: ProviderAttemptRecord, observer: ProviderStreamObserver) -> None:
    state = observer.snapshot()
    for key in (
        "first_any_chunk_at", "first_reasoning_chunk_at", "first_content_chunk_at",
        "last_any_chunk_at", "last_reasoning_chunk_at", "last_content_chunk_at",
        "total_chunk_count", "reasoning_chunk_count", "content_chunk_count",
        "reasoning_char_count", "content_char_count", "finish_reason",
        "provider_request_id", "late_chunk_count", "content_sha256",
        "reasoning_sha256",
    ):
        setattr(record, key, state[key])


def _emit(
    callback: Optional[Callable[[str, Mapping[str, Any]], None]],
    event: str,
    record: ProviderAttemptRecord,
) -> None:
    payload = record.to_dict()
    # Routine logs are an operational state channel, not an exception/body
    # archive.  Exact failure text remains available in the protected lineage
    # snapshot while this payload stays metadata-only.
    payload.pop("failure_detail", None)
    payload["event"] = event
    logger.info("PROVIDER_ATTEMPT_EVENT %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if callback is not None:
        try:
            callback(event, payload)
        except Exception:
            logger.debug("Provider progress callback failed", exc_info=True)


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
) -> Tuple[Any, ProviderAttemptRecord]:
    """Run one provider dispatch with phased watchdogs and bounded cleanup."""

    loop = asyncio.get_running_loop()
    policy = envelope.timeout_policy.normalized()
    observer = ProviderStreamObserver(envelope.attempt_id, loop)
    record = ProviderAttemptRecord(envelope=envelope)
    activity_version = 0
    stream_announced = False
    # Logging/output can itself be slow under a captured terminal.  Emit the
    # lifecycle marker before starting the provider clock so observer overhead
    # cannot consume the first-chunk allowance before dispatch is scheduled.
    _emit(progress_callback, "attempt_started", record)
    started_monotonic = time.monotonic()
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
                _apply_observer(record, observer)
                record.failure_type = selected
                record.state = "TIMED_OUT"
                _emit(progress_callback, "timeout_cleanup_started", record)
                cleanup = await _cancel_and_drain(
                    invoke_task,
                    observer,
                    record,
                    policy.cleanup_seconds,
                )
                _apply_observer(record, observer)
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
            try:
                done, _ = await asyncio.wait(
                    {invoke_task, activity_task},
                    timeout=max(0.001, min(total_remaining, phase_remaining)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not activity_task.done():
                    activity_task.cancel()
                    try:
                        await activity_task
                    except BaseException:
                        pass
            if invoke_task in done:
                try:
                    response = invoke_task.result()
                except asyncio.CancelledError as exc:
                    if asyncio.current_task() and asyncio.current_task().cancelling():
                        raise
                    code, retryable = PROVIDER_SDK_ERROR, True
                    _apply_observer(record, observer)
                    observer.fence()
                    record.failure_type = code
                    record.failure_detail = "provider/client cancelled the invocation"
                    record.retryable = retryable
                    record.cleanup_result = CLEANUP_CONFIRMED
                    record.cancel_completed_at = utc_now()
                    record.attempt_completed_at = utc_now()
                    record.state = "FAILED_TRANSIENT"
                    _emit(progress_callback, "attempt_failed", record)
                    raise ProviderAttemptTimeout(
                        code,
                        "request was cancelled by the provider/client",
                        record=record,
                        retryable=retryable,
                    ) from exc
                except BaseException as exc:
                    code, retryable = classify_provider_exception(exc)
                    _apply_observer(record, observer)
                    observer.fence()
                    record.failure_type = code
                    record.failure_detail = f"{type(exc).__name__}: {exc}"[:1200]
                    record.retryable = retryable
                    record.cleanup_result = CLEANUP_CONFIRMED
                    record.attempt_completed_at = utc_now()
                    record.state = "FAILED_TRANSIENT" if retryable else "FAILED_TERMINAL"
                    _emit(progress_callback, "attempt_failed", record)
                    failure_class = ProviderAttemptTimeout if isinstance(exc, TimeoutError) else ProviderAttemptFailure
                    raise failure_class(
                        code,
                        record.failure_detail,
                        record=record,
                        retryable=retryable,
                    ) from exc
                observer.complete_from_response(response)
                observer.fence()
                _apply_observer(record, observer)
                record.state = "COMPLETED"
                record.cleanup_result = CLEANUP_NOT_REQUIRED
                record.attempt_completed_at = utc_now()
                _emit(progress_callback, "attempt_completed", record)
                return response, record
            state = observer.snapshot()
            activity_version = int(state["version"])
            if activity_version and not stream_announced:
                stream_announced = True
                record.state = "STREAM_ACTIVE"
                _apply_observer(record, observer)
                _emit(progress_callback, "stream_active", record)
    except asyncio.CancelledError:
        if not invoke_task.done():
            await _cancel_and_drain(
                invoke_task,
                observer,
                record,
                policy.cleanup_seconds,
            )
        observer.fence()
        raise


def partial_response_diagnostic(record: ProviderAttemptRecord) -> Dict[str, Any]:
    """Return metadata-only partial-response evidence; never executable text."""

    return {
        "schema_version": 1,
        "classification": "DIAGNOSTIC_ONLY",
        "complete": False,
        "executable": False,
        "semantic_invocation_id": record.envelope.semantic_invocation_id,
        "attempt_id": record.envelope.attempt_id,
        "call_id": record.envelope.call_id,
        "request_hash": record.envelope.request_hash,
        "chunk_counts": {
            "total": record.total_chunk_count,
            "reasoning": record.reasoning_chunk_count,
            "content": record.content_chunk_count,
        },
        "first_any_chunk_at": record.first_any_chunk_at,
        "last_any_chunk_at": record.last_any_chunk_at,
        "finish_reason": None,
        "failure_type": record.failure_type,
        "cleanup_result": record.cleanup_result,
        "truncation": {"raw_content_persisted": False},
        "content_sha256": record.content_sha256,
        "reasoning_sha256": record.reasoning_sha256,
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

"""Small helpers for deterministic LLM failure handling."""

from __future__ import annotations

import re
from typing import Any


CONTENT_FILTER_MARKERS = (
    "badrequesterror",
    "data_inspection_failed",
    "inappropriate content",
    "content_filter",
    "content management policy",
    "input data may contain",
)

TRANSIENT_FAILURE_MARKERS = (
    "apiconnectionerror",
    "connection error",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "rate limit",
    "status code 429",
    "status code 502",
    "status code 503",
    "status code 504",
)


def clip_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    return f"{text[:head]}\n... [truncated {len(text) - limit} chars] ...\n{text[-tail:]}"


def is_llm_input_rejected(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in CONTENT_FILTER_MARKERS)


def is_transient_llm_failure(exc: BaseException) -> bool:
    """Classify provider/network failures that are safe to retry once."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in TRANSIENT_FAILURE_MARKERS)


def llm_failure_summary(exc: BaseException, stage: str, task_id: str = "") -> str:
    kind = "input_rejected" if is_llm_input_rejected(exc) else "request_failed"
    raw = re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()
    return (
        f"LLM {kind} during {stage}"
        + (f" for task {task_id}" if task_id else "")
        + f". Detail: {clip_text(raw, 700)}"
    )


def deterministic_failure_solution(exc: BaseException, stage: str, task_id: str = "") -> str:
    summary = llm_failure_summary(exc, stage, task_id)
    if is_llm_input_rejected(exc):
        guidance = (
            "The model provider rejected the prompt payload. The task was stopped "
            "instead of retrying with the same accumulated logs."
        )
    else:
        guidance = "The LLM call failed. The task was stopped with a deterministic report."
    return (
        "<solution>"
        f"Task stopped before the next generated action.\n\nReason: {summary}\n\n{guidance}"
        "</solution>"
    )

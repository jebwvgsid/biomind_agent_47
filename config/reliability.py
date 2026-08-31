"""Narrow runtime reliability settings for provider attempts and optional memory.

The legacy ``LLM_TIMEOUT_SECONDS`` remains the default total request deadline.
The settings here split observable provider phases without changing the existing
global model timeout or SDK retry configuration.
"""

from __future__ import annotations

import os

from .config import LLM_TIMEOUT_SECONDS


def _env_float(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), value)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return min(int(maximum), max(int(minimum), value))


# Preserve the pre-existing effective hard deadline unless explicitly split by
# deployment configuration. Thinking-mode first-token latency can exceed the
# duration of ordinary successful calls, so do not impose a shorter first-chunk
# deadline by default; deployments can still opt into one via the environment.
LLM_TOTAL_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_LLM_TOTAL_TIMEOUT_SECONDS",
    LLM_TIMEOUT_SECONDS,
    minimum=10.0,
)
LLM_FIRST_CHUNK_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_LLM_FIRST_CHUNK_TIMEOUT_SECONDS",
    LLM_TOTAL_TIMEOUT_SECONDS,
    minimum=1.0,
)
LLM_STREAM_IDLE_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_LLM_STREAM_IDLE_TIMEOUT_SECONDS",
    min(60.0, LLM_TOTAL_TIMEOUT_SECONDS),
    minimum=1.0,
)
LLM_CANCEL_CLEANUP_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_LLM_CANCEL_CLEANUP_TIMEOUT_SECONDS",
    5.0,
    minimum=0.1,
)
GENERATE_PROVIDER_MAX_ATTEMPTS = _env_int(
    "BIOMIND_GENERATE_PROVIDER_MAX_ATTEMPTS",
    2,
    minimum=1,
    maximum=2,
)


SESSION_ARCHIVE_OBSERVATION_QUEUE_SIZE = _env_int(
    "BIOMIND_SESSION_ARCHIVE_OBSERVATION_QUEUE_SIZE",
    256,
    minimum=1,
    maximum=100_000,
)
SESSION_ARCHIVE_WRITER_BUSY_TIMEOUT_SECONDS = _env_float(
    "BIOMIND_SESSION_ARCHIVE_WRITER_BUSY_TIMEOUT_SECONDS",
    0.25,
    minimum=0.01,
)
SESSION_ARCHIVE_WRITER_MAX_ATTEMPTS = _env_int(
    "BIOMIND_SESSION_ARCHIVE_WRITER_MAX_ATTEMPTS",
    2,
    minimum=1,
    maximum=3,
)
SESSION_ARCHIVE_WRITER_BACKOFF_SECONDS = _env_float(
    "BIOMIND_SESSION_ARCHIVE_WRITER_BACKOFF_SECONDS",
    0.05,
    minimum=0.0,
)
SESSION_ARCHIVE_SHUTDOWN_DRAIN_SECONDS = _env_float(
    "BIOMIND_SESSION_ARCHIVE_SHUTDOWN_DRAIN_SECONDS",
    2.0,
    minimum=0.1,
)

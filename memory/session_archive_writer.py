"""Bounded process-local writer for OPTIONAL SessionArchive observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

from .session_archive import SessionArchive


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ObservationEnvelope:
    logical_event_id: str
    enqueue_sequence: int
    created_at: str
    content_hash: str
    task_id: str
    stage: str
    event_type: str
    payload: Mapping[str, Any]


class SessionArchiveObservationWriter:
    """Serialize optional archive writes within one process without node waits.

    This is intentionally not a global/distributed single writer. Multiple
    BioMind processes can still contend on the same SQLite database.
    """

    def __init__(
        self,
        *,
        db_path: str | Path = "",
        write_call: Optional[Callable[..., Any]] = None,
        capacity: int = 256,
        busy_timeout_seconds: float = 0.25,
        max_write_attempts: int = 2,
        backoff_seconds: float = 0.05,
        shutdown_drain_seconds: float = 2.0,
    ) -> None:
        if write_call is None and not str(db_path):
            raise ValueError("db_path or write_call is required")
        self.db_path = Path(db_path) if str(db_path) else None
        self._write_call = write_call
        self.capacity = max(1, int(capacity))
        self.busy_timeout_seconds = max(0.01, float(busy_timeout_seconds))
        self.max_write_attempts = max(1, int(max_write_attempts))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.shutdown_drain_seconds = max(0.01, float(shutdown_drain_seconds))
        self._queue: "queue.Queue[ObservationEnvelope | object]" = queue.Queue(self.capacity)
        self._sentinel = object()
        self._lock = threading.Lock()
        self._pending_ids: set[str] = set()
        self._sequence = 0
        self._accepting = True
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._archive: Optional[SessionArchive] = None
        self._stats: Dict[str, Any] = {
            "scope": "PER_PROCESS",
            "capacity": self.capacity,
            "enqueued_count": 0,
            "written_count": 0,
            "dropped_count": 0,
            "coalesced_count": 0,
            "retry_count": 0,
            "write_failure_count": 0,
            "remaining_not_durable_count": 0,
            "last_success_at": "",
            "last_failure_at": "",
            "last_failure_type": "",
            "last_enqueue_latency_ms": 0.0,
            "last_queue_delay_ms": 0.0,
            "last_write_latency_ms": 0.0,
            "started_at": _now(),
            "stopped_at": "",
        }

    @staticmethod
    def _identity(payload: Mapping[str, Any]) -> tuple[str, str]:
        identity = {
            "task_id": str(payload.get("task_id") or "default"),
            "session_id": str(payload.get("session_id") or ""),
            "stage": str(payload.get("stage") or "event"),
            "event_type": str(payload.get("event_type") or "event"),
            "content": str(payload.get("content") or ""),
            "summary": str(payload.get("summary") or ""),
            "metadata": payload.get("metadata") or {},
        }
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"archive-observation-{content_hash[:24]}", content_hash

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._accepting:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="biomind-session-archive-observation-writer",
                daemon=True,
            )
            self._thread.start()

    def enqueue_nowait(self, **payload: Any) -> str:
        enqueue_started = time.monotonic()
        logical_event_id, content_hash = self._identity(payload)
        with self._lock:
            if not self._accepting:
                self._stats["dropped_count"] += 1
                self._stats["last_enqueue_latency_ms"] = round(
                    (time.monotonic() - enqueue_started) * 1000.0,
                    3,
                )
                self._emit_locked("ARCHIVE_OBSERVATION_DROPPED", logical_event_id)
                return "dropped"
            if logical_event_id in self._pending_ids:
                self._stats["coalesced_count"] += 1
                self._stats["last_enqueue_latency_ms"] = round(
                    (time.monotonic() - enqueue_started) * 1000.0,
                    3,
                )
                self._emit_locked("ARCHIVE_OBSERVATION_COALESCED", logical_event_id)
                return "coalesced"
            self._sequence += 1
            envelope = ObservationEnvelope(
                logical_event_id=logical_event_id,
                enqueue_sequence=self._sequence,
                created_at=_now(),
                content_hash=content_hash,
                task_id=str(payload.get("task_id") or "default"),
                stage=str(payload.get("stage") or "event"),
                event_type=str(payload.get("event_type") or "event"),
                payload=dict(payload),
            )
            try:
                self._queue.put_nowait(envelope)
            except queue.Full:
                self._stats["dropped_count"] += 1
                self._stats["last_enqueue_latency_ms"] = round(
                    (time.monotonic() - enqueue_started) * 1000.0,
                    3,
                )
                self._emit_locked("ARCHIVE_OBSERVATION_DROPPED", logical_event_id)
                return "dropped"
            self._pending_ids.add(logical_event_id)
            self._stats["enqueued_count"] += 1
            self._stats["last_enqueue_latency_ms"] = round(
                (time.monotonic() - enqueue_started) * 1000.0,
                3,
            )
            self._emit_locked("ARCHIVE_OBSERVATION_ENQUEUED", logical_event_id)
        self._ensure_started()
        return "enqueued"

    def _resolve_write_call(self) -> Callable[..., Any]:
        if self._write_call is not None:
            return self._write_call
        if self._archive is None:
            self._archive = SessionArchive(
                self.db_path,
                busy_timeout_seconds=self.busy_timeout_seconds,
            )
        return self._archive.append_event

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            if item is self._sentinel:
                self._queue.task_done()
                return
            assert isinstance(item, ObservationEnvelope)
            write_started = time.monotonic()
            try:
                created = datetime.fromisoformat(item.created_at)
                queue_delay_ms = max(
                    0.0,
                    (datetime.now(timezone.utc) - created).total_seconds() * 1000.0,
                )
            except ValueError:
                queue_delay_ms = 0.0
            succeeded = False
            coalesced = False
            for ordinal in range(1, self.max_write_attempts + 1):
                try:
                    self._resolve_write_call()(**dict(item.payload))
                    succeeded = True
                    break
                except sqlite3.IntegrityError:
                    # Cross-process SELECT/INSERT races are duplicate success for
                    # optional observations, never a workflow exception.
                    coalesced = True
                    break
                except Exception as exc:
                    with self._lock:
                        self._stats["last_failure_at"] = _now()
                        self._stats["last_failure_type"] = type(exc).__name__
                    if ordinal < self.max_write_attempts:
                        with self._lock:
                            self._stats["retry_count"] += 1
                        if self.backoff_seconds:
                            time.sleep(self.backoff_seconds * ordinal)
                        self._archive = None
                        continue
                    with self._lock:
                        self._stats["write_failure_count"] += 1
                        self._emit_locked("ARCHIVE_OBSERVATION_FAILED", item.logical_event_id)
            with self._lock:
                self._stats["last_queue_delay_ms"] = round(queue_delay_ms, 3)
                self._stats["last_write_latency_ms"] = round(
                    (time.monotonic() - write_started) * 1000.0,
                    3,
                )
                if succeeded:
                    self._stats["written_count"] += 1
                    self._stats["last_success_at"] = _now()
                    self._emit_locked("ARCHIVE_OBSERVATION_WRITTEN", item.logical_event_id)
                elif coalesced:
                    self._stats["coalesced_count"] += 1
                    self._emit_locked("ARCHIVE_OBSERVATION_COALESCED", item.logical_event_id)
                self._pending_ids.discard(item.logical_event_id)
            self._queue.task_done()

    def _oldest_age_ms_locked(self) -> float:
        with self._queue.mutex:
            rows = [row for row in list(self._queue.queue) if isinstance(row, ObservationEnvelope)]
        if not rows:
            return 0.0
        try:
            created = datetime.fromisoformat(rows[0].created_at)
            return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() * 1000.0)
        except ValueError:
            return 0.0

    def _emit_locked(self, event: str, logical_event_id: str) -> None:
        telemetry = {
            "event": event,
            "logical_event_id": logical_event_id,
            "scope": "PER_PROCESS",
            "queue_depth": self._queue.qsize(),
            "enqueued_count": self._stats["enqueued_count"],
            "written_count": self._stats["written_count"],
            "dropped_count": self._stats["dropped_count"],
            "coalesced_count": self._stats["coalesced_count"],
            "retry_count": self._stats["retry_count"],
            "write_failure_count": self._stats["write_failure_count"],
            "last_success_at": self._stats["last_success_at"],
            "last_enqueue_latency_ms": self._stats["last_enqueue_latency_ms"],
            "last_queue_delay_ms": self._stats["last_queue_delay_ms"],
            "last_write_latency_ms": self._stats["last_write_latency_ms"],
        }
        logger.info("SESSION_ARCHIVE_QUEUE_EVENT %s", json.dumps(telemetry, sort_keys=True))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            row = dict(self._stats)
            row.update({
                "queue_depth": self._queue.qsize(),
                "oldest_event_age_ms": round(self._oldest_age_ms_locked(), 3),
                "accepting": self._accepting,
                "writer_alive": bool(self._thread and self._thread.is_alive()),
            })
            return row

    def close(self, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        timeout = self.shutdown_drain_seconds if timeout_seconds is None else max(
            0.01, float(timeout_seconds)
        )
        deadline = time.monotonic() + timeout
        with self._lock:
            self._accepting = False
            thread = self._thread
        if thread is not None and thread.is_alive():
            while self._queue.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            self._stop_event.set()
            try:
                self._queue.put_nowait(self._sentinel)
            except queue.Full:
                pass
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        remaining = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, ObservationEnvelope):
                remaining += 1
                with self._lock:
                    self._pending_ids.discard(item.logical_event_id)
            self._queue.task_done()
        with self._lock:
            self._stats["remaining_not_durable_count"] += remaining
            self._stats["dropped_count"] += remaining
            self._stats["stopped_at"] = _now()
        return self.snapshot()


_WRITER_REGISTRY: Dict[str, tuple[SessionArchiveObservationWriter, int]] = {}
_REGISTRY_LOCK = threading.Lock()


def acquire_observation_writer(db_path: str | Path, **kwargs: Any) -> SessionArchiveObservationWriter:
    """Return one process-local writer per canonical archive path."""

    key = str(Path(db_path).resolve())
    with _REGISTRY_LOCK:
        current = _WRITER_REGISTRY.get(key)
        if current is not None:
            writer, refs = current
            _WRITER_REGISTRY[key] = (writer, refs + 1)
            return writer
        writer = SessionArchiveObservationWriter(db_path=key, **kwargs)
        _WRITER_REGISTRY[key] = (writer, 1)
        return writer


def release_observation_writer(
    db_path: str | Path,
    *,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    key = str(Path(db_path).resolve())
    with _REGISTRY_LOCK:
        current = _WRITER_REGISTRY.get(key)
        if current is None:
            return {}
        writer, refs = current
        if refs > 1:
            _WRITER_REGISTRY[key] = (writer, refs - 1)
            return writer.snapshot()
        _WRITER_REGISTRY.pop(key, None)
    return writer.close(timeout_seconds)


def shutdown_observation_writers(timeout_seconds: float = 0.5) -> Dict[str, Dict[str, Any]]:
    """Best-effort process shutdown; explicit agent close remains preferred."""

    with _REGISTRY_LOCK:
        rows = list(_WRITER_REGISTRY.items())
        _WRITER_REGISTRY.clear()
    return {
        key: writer.close(timeout_seconds)
        for key, (writer, _refs) in rows
    }

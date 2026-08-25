"""Episode-scoped Consultant discovery continuation and timing primitives.

The Controller may reuse stable preprocessing facts inside one discovery
episode, while every discovery decision remains dynamic and Consultant-owned.
This module contains no routing or scientific-selection policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, MutableMapping, Sequence

from agent_core.task_spec import task_semantic_hash, task_spec_revision


CONTINUATION_SCHEMA_VERSION = 1
PREPROCESS_TRACE_SCHEMA_VERSION = 1

CONSULTANT_PREPROCESS_PHASES = (
    "task_state",
    "memory",
    "session_archive",
    "input_inventory",
    "input_resolution",
    "skill_catalog",
    "skill_ranking",
    "runtime_evidence",
    "discovery_projection",
    "prompt_build",
    "prompt_serialize",
    "context_trace_write",
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_continuation_identity(
    *,
    task_id: str,
    consultation_id: str,
    conversation_evidence: Mapping[str, Any] | None,
    task_spec: Mapping[str, Any] | None,
    project_id: str,
    task_mode: str,
    task_ownership: Mapping[str, Any] | None = None,
    resolved_bindings: Mapping[str, Any] | None = None,
    work_dir: str = "",
) -> Dict[str, Any]:
    """Return the stable identity that is allowed to own one base context."""

    conversation = dict(conversation_evidence or {})
    spec = dict(task_spec or {})
    ownership = {
        str(key): deepcopy(value)
        for key, value in dict(task_ownership or {}).items()
        if value not in (None, "", [], {})
    }
    binding_digest = _digest(dict(resolved_bindings or {}))
    conversation_identity = {
        "head_ref": str(conversation.get("head_ref") or ""),
        "turn_count": int(conversation.get("turn_count") or 0),
        "user_turn_count": int(conversation.get("user_turn_count") or 0),
    }
    episode_seed = {
        "task_id": str(task_id or "default"),
        "consultation_id": str(consultation_id or ""),
        "conversation": conversation_identity,
    }
    identity = {
        "schema_version": CONTINUATION_SCHEMA_VERSION,
        "episode_id": "consultant-discovery-" + _digest(episode_seed)[:20],
        "task_id": str(task_id or "default"),
        "consultation_id": str(consultation_id or ""),
        "conversation": conversation_identity,
        # A metadata-only revision bump does not invalidate semantic context.
        "task_semantic_hash": task_semantic_hash(spec),
        "task_revision_observed": task_spec_revision(spec),
        "project_id": str(project_id or "default"),
        "task_mode": str(task_mode or "managed"),
        "task_ownership": ownership,
        "resolved_bindings_digest": binding_digest,
        "work_dir": os.path.realpath(str(work_dir or ".")),
    }
    identity["cache_key"] = _digest({
        key: value
        for key, value in identity.items()
        if key not in {"cache_key", "task_revision_observed"}
    })
    return identity


def _path_identity(path_text: str) -> Dict[str, Any]:
    path = Path(str(path_text or "")).expanduser()
    row: Dict[str, Any] = {"path": str(path)}
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        row.update({
            "path": str(resolved),
            "exists": True,
            "kind": "file" if resolved.is_file() else "directory" if resolved.is_dir() else "other",
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "mode": int(stat.st_mode),
        })
        if resolved.is_file() and os.access(resolved, os.R_OK):
            hasher = hashlib.sha256()
            with resolved.open("rb") as handle:
                hasher.update(handle.read(65536))
                if stat.st_size > 65536:
                    handle.seek(max(0, stat.st_size - 65536))
                    hasher.update(handle.read(65536))
            row["content_sample_sha256"] = hasher.hexdigest()
    except OSError:
        row.update({"exists": False, "kind": "missing"})
    return row


def input_inventory_signature(inventory: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Fingerprint the paths that made the cached formal-input view true.

    File content sampling is bounded to the same head/tail size used by the
    formal artifact resolver. Directory metadata catches ordinary additions or
    removals without recursively rescanning a large project tree.
    """

    current = dict(inventory or {})
    paths: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        text = str(value or "").strip()
        if text and text not in paths:
            paths.append(text)

    for record in [
        *list(current.get("resolved_inputs") or []),
        *list(current.get("formal_candidates") or []),
    ]:
        if not isinstance(record, Mapping):
            continue
        add(record.get("path") or record.get("resolved_path"))
        add(record.get("paths"))
        add(record.get("r1"))
        add(record.get("r2"))
    add(current.get("verified_explicit_paths"))
    add(current.get("search_roots"))
    rows = [_path_identity(path) for path in sorted(paths)]
    return {
        "schema_version": 1,
        "path_count": len(rows),
        "paths": rows,
        "digest": _digest(rows),
    }


def evaluate_continuation_cache(
    cache: Mapping[str, Any] | None,
    *,
    identity: Mapping[str, Any],
) -> Dict[str, Any]:
    """Decide whether stable base facts are reusable for this invocation."""

    current = dict(cache or {})
    expected = dict(identity or {})
    if not current:
        return {"cache_hit": False, "reason": "cache_empty"}
    if int(current.get("schema_version") or 0) != CONTINUATION_SCHEMA_VERSION:
        return {"cache_hit": False, "reason": "schema_changed"}
    prior_identity = dict(current.get("identity") or {})
    checks = (
        ("episode_id", "episode_changed"),
        ("conversation", "conversation_changed"),
        ("task_semantic_hash", "task_spec_material_change"),
        ("project_id", "task_ownership_changed"),
        ("task_mode", "task_ownership_changed"),
        ("task_ownership", "task_ownership_changed"),
        ("resolved_bindings_digest", "formal_binding_changed"),
        ("work_dir", "input_scope_changed"),
    )
    for field, reason in checks:
        if prior_identity.get(field) != expected.get(field):
            return {"cache_hit": False, "reason": reason, "changed_field": field}
    base = dict(current.get("base") or {})
    cached_inventory = dict(current.get("input_inventory_signature") or {})
    inventory = base.get("input_resolution_result")
    if cached_inventory and isinstance(inventory, Mapping):
        live_inventory = input_inventory_signature(inventory)
        if str(live_inventory.get("digest") or "") != str(cached_inventory.get("digest") or ""):
            return {
                "cache_hit": False,
                "reason": "input_inventory_changed",
                "cached_inventory_digest": str(cached_inventory.get("digest") or ""),
                "live_inventory_digest": str(live_inventory.get("digest") or ""),
            }
    return {
        "cache_hit": True,
        "reason": "same_discovery_episode",
        "base": deepcopy(base),
        "hit_count": int(current.get("hit_count") or 0) + 1,
    }


def build_continuation_cache(
    *,
    identity: Mapping[str, Any],
    base: Mapping[str, Any],
    prior: Mapping[str, Any] | None = None,
    cache_hit: bool = False,
) -> Dict[str, Any]:
    base_copy = deepcopy(dict(base or {}))
    return {
        "schema_version": CONTINUATION_SCHEMA_VERSION,
        "episode_id": str(identity.get("episode_id") or ""),
        "cache_key": str(identity.get("cache_key") or ""),
        "identity": deepcopy(dict(identity or {})),
        "base": base_copy,
        "input_inventory_signature": input_inventory_signature(
            base_copy.get("input_resolution_result")
            if isinstance(base_copy.get("input_resolution_result"), Mapping)
            else {}
        ),
        "hit_count": (
            int(dict(prior or {}).get("hit_count") or 0) + 1
            if cache_hit
            else 0
        ),
        "last_cache_hit": bool(cache_hit),
        "updated_at": _now(),
    }


def consultant_discovery_delta(state: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Project only per-round discovery facts; never copy the cached base."""

    current = dict(state or {})
    return {
        "routing_status": str(current.get("routing_status") or "UNRESOLVED"),
        "discovery_round": int(current.get("normal_search_count") or 0),
        "task_hypothesis": str(current.get("task_hypothesis") or ""),
        "search_queries_attempted": deepcopy(list(current.get("search_queries_attempted") or [])),
        "candidate_skill_ids": deepcopy(list(current.get("candidate_skill_ids") or [])),
        "candidate_evidence": deepcopy(dict(current.get("candidate_evidence") or {})),
        "loaded_skill_ids": deepcopy(list(current.get("loaded_skill_ids") or [])),
        "loaded_skill_docs": deepcopy(dict(current.get("loaded_skill_docs") or {})),
        "rejected_candidates": deepcopy(list(current.get("rejected_candidates") or [])),
        "reasoning_level": str(current.get("reasoning_level") or "normal"),
        "reasoning_escalation_count": int(current.get("reasoning_escalation_count") or 0),
        "remaining_discovery_budget": {
            "normal_searches": max(
                0,
                int((current.get("budgets") or {}).get("normal_searches") or 0)
                - int(current.get("normal_search_count") or 0),
            ),
            "catalog_expansions": max(
                0,
                int((current.get("budgets") or {}).get("catalog_expansions") or 0)
                - int(current.get("catalog_expansion_count") or 0),
            ),
            "loaded_skill_docs": max(
                0,
                int((current.get("budgets") or {}).get("loaded_skill_docs") or 0)
                - len(current.get("loaded_skill_ids") or []),
            ),
            "reasoning_escalations": max(
                0,
                int((current.get("budgets") or {}).get("reasoning_escalations") or 0)
                - int(current.get("reasoning_escalation_count") or 0),
            ),
        },
        "selected_skill_ids": deepcopy(list(current.get("selected_skill_ids") or [])),
        "pending_user_ambiguity": deepcopy(current.get("pending_user_ambiguity") or {}),
        "controller_feedback": deepcopy(list(current.get("last_feedback") or [])),
    }


def compact_archive_query(
    *,
    consultant_context: str,
    task_hypothesis: str = "",
    task_goal: str = "",
    input_roles: Sequence[str] = (),
    max_chars: int = 800,
) -> str:
    """Build a bounded retrieval query without serializing the whole TaskSpec."""

    parts: list[str] = []
    for value in (task_hypothesis, task_goal):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    for line in str(consultant_context or "").splitlines():
        text = line.strip().lstrip("- ")
        if not text or text in parts:
            continue
        # Paths are already represented by typed input roles and add noisy FTS terms.
        if "/" in text and (":" in text or text.startswith(("/", "."))):
            continue
        parts.append(text)
        if len(parts) >= 4:
            break
    roles = list(dict.fromkeys(str(item).strip() for item in input_roles if str(item).strip()))
    if roles:
        parts.append("input_roles: " + " ".join(roles))
    query = "\n".join(parts).strip()
    return query[: max(64, int(max_chars))]


async def run_optional_enrichment(
    call: Callable[[], Any],
    *,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """Run OPTIONAL_ENRICHMENT with measured, bounded latency.

    Canonical state must never use this helper. A timeout returns an empty
    enrichment value and explicit telemetry while the task continues.
    """

    started = time.perf_counter()
    outcome: Dict[str, Any] = {}

    def invoke() -> None:
        try:
            outcome["value"] = call()
        except Exception as exc:  # projected below without exception text
            outcome["error_type"] = type(exc).__name__
            error_text = str(exc).lower()
            outcome["busy_or_locked"] = (
                "locked" in error_text or "busy" in error_text
            )

    # The join is deliberately synchronous: this node is sequential already,
    # and a daemon worker gives us a real bounded return even when the storage
    # call itself cannot be cancelled.  It also avoids depending on an event
    # loop's cross-thread wakeup implementation.
    worker = threading.Thread(
        target=invoke,
        name="biomind-consultant-optional-enrichment",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=max(0.1, float(timeout_seconds)))
    if worker.is_alive():
        return {
            "value": "",
            "timed_out": True,
            "failed_open": True,
            "error_type": "TimeoutError",
            "busy_or_locked": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    if outcome.get("error_type"):
        return {
            "value": "",
            "timed_out": False,
            "failed_open": True,
            "error_type": str(outcome.get("error_type") or "OptionalEnrichmentError"),
            "busy_or_locked": bool(outcome.get("busy_or_locked")),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    return {
        "value": outcome.get("value"),
        "timed_out": False,
        "failed_open": False,
        "error_type": "",
        "busy_or_locked": False,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


class ConsultantPreprocessRecorder:
    """Collect monotonic phase timings without writing during measured work."""

    def __init__(
        self,
        *,
        task_id: str,
        episode_id: str,
        invocation: int,
        started_perf: float | None = None,
        started_at: str = "",
    ):
        self.task_id = str(task_id or "default")
        self.episode_id = str(episode_id or "")
        self.invocation = max(1, int(invocation or 1))
        self.started_at = str(started_at or _now())
        self._started = (
            float(started_perf)
            if started_perf is not None
            else time.perf_counter()
        )
        self._phases: Dict[str, Dict[str, Any]] = {
            phase: {
                "phase": phase,
                "elapsed_ms": 0.0,
                "cache_hit": False,
                "cache_miss": False,
                "call_count": 0,
            }
            for phase in CONSULTANT_PREPROCESS_PHASES
        }

    @contextmanager
    def phase(self, phase: str, **metadata: Any) -> Iterator[MutableMapping[str, Any]]:
        if phase not in self._phases:
            raise ValueError(f"Unknown Consultant preprocess phase: {phase}")
        stats: Dict[str, Any] = dict(metadata)
        started = time.perf_counter()
        try:
            yield stats
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            current = self._phases[phase]
            current["elapsed_ms"] = round(float(current.get("elapsed_ms") or 0.0) + elapsed_ms, 3)
            current["call_count"] = int(current.get("call_count") or 0) + int(stats.pop("call_count", 1))
            self._merge_metrics(current, stats)

    def mark(self, phase: str, *, elapsed_ms: float = 0.0, **metadata: Any) -> None:
        if phase not in self._phases:
            raise ValueError(f"Unknown Consultant preprocess phase: {phase}")
        current = self._phases[phase]
        current["elapsed_ms"] = round(float(current.get("elapsed_ms") or 0.0) + max(0.0, float(elapsed_ms)), 3)
        current["call_count"] = int(current.get("call_count") or 0) + int(metadata.pop("call_count", 0))
        self._merge_metrics(current, metadata)

    @staticmethod
    def _merge_metrics(target: MutableMapping[str, Any], values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            if key.startswith("_"):
                continue
            if key in {"query_text", "prompt", "content", "secret"} or any(
                marker in key.lower()
                for marker in ("api_key", "password", "authorization", "credential")
            ):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                target[key] = value
            elif isinstance(value, (list, tuple, set)):
                target[key] = len(value)
            elif isinstance(value, Mapping):
                target[key] = {
                    str(child_key): child_value
                    for child_key, child_value in value.items()
                    if isinstance(child_value, (str, int, float, bool)) or child_value is None
                }

    def phase_rows(self) -> list[Dict[str, Any]]:
        return [deepcopy(self._phases[phase]) for phase in CONSULTANT_PREPROCESS_PHASES]

    def event_rows(self, *, include_phases: Sequence[str] | None = None) -> list[Dict[str, Any]]:
        selected = set(include_phases or CONSULTANT_PREPROCESS_PHASES)
        return [
            {
                "event_kind": "consultant_preprocess_phase",
                "payload": {
                    "schema_version": PREPROCESS_TRACE_SCHEMA_VERSION,
                    "episode_id": self.episode_id,
                    "invocation": self.invocation,
                    **deepcopy(self._phases[phase]),
                },
            }
            for phase in CONSULTANT_PREPROCESS_PHASES
            if phase in selected
        ]

    def total_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": PREPROCESS_TRACE_SCHEMA_VERSION,
            "episode_id": self.episode_id,
            "invocation": self.invocation,
            "phase": "total",
            "elapsed_ms": round((time.perf_counter() - self._started) * 1000, 3),
            "started_at": self.started_at,
            "finished_at": _now(),
            "phase_count": len(CONSULTANT_PREPROCESS_PHASES),
        }

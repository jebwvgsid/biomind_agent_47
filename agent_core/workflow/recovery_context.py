"""Bounded Recovery observation storage, checkpoints, and phase archives.

All projections in this module are deterministic and exclude hidden reasoning.
Large tool results are stored behind immutable archive references.  Token
pressure archives the complete source episode before a separately validated
LLM compaction candidate is made active for a new private Recovery epoch.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from agent_core.workflow.phase_archive import PhaseArchiveRecord, PhaseArchiveStore
from agent_core.workflow.stage_contracts import RecoverySeed
from agent_core.workflow.inspection_evidence import (
    InspectionEvidenceError, InspectionObjectReader, inspection_scope,
    read_inspection_authority, bounded_inspection_body_page,
)


RECOVERY_CHECKPOINT_SCHEMA_VERSION = 1
RECOVERY_OBSERVATION_EXCERPT_CHARS = 2_048


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _unique_text(values: Sequence[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


@dataclass(frozen=True)
class RecoveryCheckpoint:
    """Deterministic hard-bound state; never a conversational summary."""

    checkpoint_id: str
    failure_id: str
    seed_digest: str
    inspected_refs: tuple[dict[str, str], ...]
    accepted_machine_facts: tuple[dict[str, Any], ...]
    unresolved_evidence: tuple[str, ...]
    tool_cursors: tuple[dict[str, Any], ...]
    budget_counters: dict[str, int]
    linked_previous_archive_ref: str = ""
    overflow_epoch: int = 0
    schema_version: int = RECOVERY_CHECKPOINT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "checkpoint_id": self.checkpoint_id,
            "failure_id": self.failure_id,
            "seed_digest": self.seed_digest,
            "inspected_refs": [deepcopy(item) for item in self.inspected_refs],
            "accepted_machine_facts": [
                deepcopy(item) for item in self.accepted_machine_facts
            ],
            "unresolved_evidence": list(self.unresolved_evidence),
            "tool_cursors": [deepcopy(item) for item in self.tool_cursors],
            "budget_counters": deepcopy(self.budget_counters),
            "overflow_epoch": self.overflow_epoch,
        }
        if self.linked_previous_archive_ref:
            value["linked_previous_archive_ref"] = self.linked_previous_archive_ref
        return value


class RecoveryObservationStore:
    """Externalize one complete tool result behind an immutable opaque ref."""

    def __init__(
        self,
        phase_store: PhaseArchiveStore,
        *,
        excerpt_chars: int = RECOVERY_OBSERVATION_EXCERPT_CHARS,
    ) -> None:
        self.phase_store = phase_store
        self.excerpt_chars = max(0, int(excerpt_chars))

    def externalize(
        self,
        *,
        seed: RecoverySeed,
        request_id: str,
        tool: str,
        source_ref: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = deepcopy(dict(result))
        payload_digest = _digest(payload)
        invocation_id = (
            f"{seed.seed_id}:observation:{request_id}:"
            f"{payload_digest.removeprefix('sha256:')[:24]}"
        )
        record = self.phase_store.archive(
            task_id=seed.failure_packet.task_id,
            phase="recovery-observation",
            invocation_id=invocation_id,
            summary={
                "schema_version": 1,
                "tool": tool,
                "source_ref": source_ref,
                "content_sha256": payload_digest,
                "result": payload,
            },
            metadata={
                "seed_id": seed.seed_id,
                "failure_id": seed.failure_packet.failure_id,
                "request_id": request_id,
                "step_id": seed.failure_packet.step_id,
                "scope": inspection_scope(seed.failure_packet, seed.seed_id),
                "object_revision": 1,
                "allowed_tools": ["evidence.read"],
            },
        )
        serialized = _canonical(payload)
        return {
            "status": "EXTERNALIZED",
            "observation_ref": record.archive_ref,
            "content_sha256": payload_digest,
            "observed_chars": len(serialized),
            "bounded_excerpt": serialized[: self.excerpt_chars],
            "reader": "evidence.read",
            "object_revision": 1,
            "cursor": {"next_start_line": 1},
        }

    def resolve(self, *, seed: RecoverySeed, observation_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Authorize an immutable observation from the current durable episode.

        The archive's existing invocation anchor is its publication record;
        the dispatcher admits that exact scoped handle into its existing
        registry. Historical/ordinary phase archives grant no observation read.
        """
        scope = inspection_scope(seed.failure_packet, seed.seed_id)
        reader = InspectionObjectReader(str(self.phase_store.root.parent), scope=scope)
        read_inspection_authority(reader.store, scope)
        from agent_core.workflow.inspection_evidence import MAX_INSPECTION_OBJECT_BYTES
        from agent_core.workflow.phase_archive import PhaseArchiveIntegrityError
        try:
            record = self.phase_store.read(observation_ref, max_object_bytes=MAX_INSPECTION_OBJECT_BYTES)
        except PhaseArchiveIntegrityError as exc:
            if str(exc) == "PHASE_ARCHIVE_OBJECT_SIZE_LIMIT":
                raise InspectionEvidenceError("INSPECTION_OBJECT_SIZE_LIMIT") from exc
            raise
        plain = record.to_dict()
        meta, summary = plain["metadata"], plain["summary"]
        if record.phase != "recovery-observation" or record.task_id != scope["task_id"] or meta.get("scope") != scope:
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", "observation")
        if type(meta.get("object_revision")) is not int or meta["object_revision"] != 1:
            raise InspectionEvidenceError("INSPECTION_OBJECT_REVISION_MISMATCH")
        if meta.get("allowed_tools") != ["evidence.read"]:
            raise InspectionEvidenceError("INSPECTION_TOOL_NOT_ALLOWED")
        if meta.get("seed_id") != seed.seed_id or meta.get("failure_id") != seed.failure_packet.failure_id:
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", "failure")
        payload = summary.get("result")
        if summary.get("schema_version") != 1 or not isinstance(payload, Mapping):
            raise InspectionEvidenceError("INSPECTION_OBJECT_SCHEMA_INVALID")
        content_hash = _digest(payload)
        if summary.get("content_sha256") != content_hash:
            raise InspectionEvidenceError("INSPECTION_OBJECT_DIGEST_MISMATCH")
        expected_invocation = f"{seed.seed_id}:observation:{meta.get('request_id')}:" + content_hash.removeprefix("sha256:")[:24]
        if record.invocation_id != expected_invocation:
            raise InspectionEvidenceError("INSPECTION_OBJECT_IDENTITY_MISMATCH")
        descriptor = {
            "ref_id": observation_ref, "kind": "evidence", "task_id": scope["task_id"],
            "plan_id": scope["plan_id"], "step_id": scope["step_id"],
            "backend": "recovery_observation", "digest": "sha256:" + record.digest,
            "size_hint": len(_canonical(payload)),
            "scope": {**scope, "access": "current_step", "object_revision": 1,
                "object_kind": "recovery_observation", "source_object_id": meta["request_id"],
                "allowed_tools": ["evidence.read"], "readable": True, "content_sha256": content_hash},
        }
        return descriptor, dict(payload)

    def read(self, *, seed: RecoverySeed, descriptor: Mapping[str, Any], start_line: int = 1,
             end_line: int | None = None) -> dict[str, Any]:
        verified, payload = self.resolve(seed=seed, observation_ref=str(descriptor.get("ref_id") or ""))
        if verified != dict(descriptor):
            raise InspectionEvidenceError("INSPECTION_REF_INDEX_MISMATCH")
        return {
            "tool": "evidence.read", "source_ref": verified["ref_id"],
            "source_object_id": verified["scope"]["source_object_id"],
            "object_kind": "recovery_observation", "object_revision": 1,
            "digest": verified["digest"], "content_sha256": verified["scope"]["content_sha256"],
            "scope": inspection_scope(seed.failure_packet, seed.seed_id),
            "authority": "current_inspection", "observation_page": True,
            **bounded_inspection_body_page(payload, start_line=start_line, end_line=end_line),
        }


def build_recovery_checkpoint(
    *,
    seed: RecoverySeed,
    seed_digest: str,
    observation_receipts: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any],
    linked_previous_archive_ref: str = "",
    overflow_epoch: int = 0,
) -> RecoveryCheckpoint:
    inspected: list[dict[str, str]] = []
    facts: list[dict[str, Any]] = []
    unresolved: list[str] = []
    cursors: list[dict[str, Any]] = []
    for raw in observation_receipts:
        item = dict(raw)
        source_ref = str(item.get("source_ref") or "")
        result = dict(item.get("result") or {})
        content_hash = str(
            result.get("content_sha256") or item.get("content_sha256") or ""
        )
        if source_ref:
            inspected.append({"ref": source_ref, "content_sha256": content_hash})
        fact = {
            key: deepcopy(value)
            for key, value in {
                "request_id": item.get("request_id"),
                "tool": item.get("tool"),
                "source_ref": source_ref,
                "status": result.get("status"),
                "returncode": result.get("returncode"),
                "exit_code": result.get("exit_code"),
                "content_sha256": content_hash,
            }.items()
            if value not in (None, "", [], {})
        }
        if fact:
            facts.append(fact)
        cursor = item.get("cursor")
        if isinstance(cursor, Mapping) and cursor:
            cursors.append({
                "request_id": str(item.get("request_id") or ""),
                "tool": str(item.get("tool") or ""),
                "cursor": deepcopy(dict(cursor)),
            })
        status = str(result.get("status") or "").upper()
        error = dict(item.get("error") or {})
        if error:
            unresolved.append(
                f"{str(item.get('request_id') or '')}:{str(error.get('code') or 'TOOL_ERROR')}"
            )
        elif status in {
            "REQUIRES_NARROWING",
            "RECOVERY_OBSERVATION_BUDGET_EXCEEDED",
        }:
            unresolved.append(f"{str(item.get('request_id') or '')}:{status}")

    counters = {
        key: int(audit.get(key) or 0)
        for key in (
            "turn_count",
            "tool_call_count",
            "observation_chars",
            "no_progress_rounds",
            "transport_attempt_count",
        )
    }
    identity = {
        "failure_id": seed.failure_packet.failure_id,
        "seed_digest": seed_digest,
        "inspected_refs": inspected,
        "accepted_machine_facts": facts,
        "unresolved_evidence": unresolved,
        "tool_cursors": cursors,
        "budget_counters": counters,
        "linked_previous_archive_ref": linked_previous_archive_ref,
        "overflow_epoch": int(overflow_epoch),
    }
    return RecoveryCheckpoint(
        checkpoint_id=(
            "recovery-checkpoint-" + _digest(identity).removeprefix("sha256:")[:24]
        ),
        failure_id=seed.failure_packet.failure_id,
        seed_digest=seed_digest,
        inspected_refs=tuple(inspected),
        accepted_machine_facts=tuple(facts),
        unresolved_evidence=_unique_text(unresolved),
        tool_cursors=tuple(cursors),
        budget_counters=counters,
        linked_previous_archive_ref=str(linked_previous_archive_ref or ""),
        overflow_epoch=int(overflow_epoch),
    )


def archive_recovery_episode(
    *,
    phase_store: PhaseArchiveStore,
    seed: RecoverySeed,
    episode: Mapping[str, Any],
    overflow_epoch: int = 0,
    linked_previous_archive_ref: str = "",
) -> PhaseArchiveRecord:
    """Persist exactly the provider-visible episode plus bounded audit facts."""

    directive = deepcopy(dict(episode.get("directive") or {}))
    terminal_error = deepcopy(dict(episode.get("terminal_error") or {}))
    checkpoint = deepcopy(dict(episode.get("checkpoint") or {}))
    compaction_required = deepcopy(dict(
        episode.get("compaction_required") or {}
    ))
    transcript = deepcopy(list(episode.get("_provider_episode") or ()))
    audit = deepcopy(dict(episode.get("audit") or {}))
    maintenance = deepcopy(dict((episode.get("_resume_state") or {}).get("compaction_maintenance") or {}))
    episode_digest = _digest({
        "seed_id": seed.seed_id,
        "transcript": transcript,
        "directive": directive,
        "terminal_error": terminal_error,
        "checkpoint": checkpoint,
        "compaction_required": compaction_required,
        "audit": audit,
        "compaction_maintenance": maintenance,
        "overflow_epoch": int(overflow_epoch),
        "linked_previous_archive_ref": str(linked_previous_archive_ref or ""),
    })
    invocation_id = (
        f"{seed.seed_id}:epoch:{int(overflow_epoch)}:"
        f"{episode_digest.removeprefix('sha256:')[:24]}"
    )
    evidence = [
        {"ref": str(ref)}
        for ref in audit.get("inspected_refs") or ()
        if str(ref)
    ]
    if directive and not terminal_error:
        terminal_state = "directive_ready"
    elif compaction_required and not terminal_error:
        terminal_state = "compaction_boundary"
    else:
        terminal_state = "failed_closed"
    return phase_store.archive(
        task_id=seed.failure_packet.task_id,
        phase="recovery",
        invocation_id=invocation_id,
        summary={
            "schema_version": 1,
            "terminal_state": terminal_state,
            "failure_id": seed.failure_packet.failure_id,
            "final_recovery_directive": directive,
            "terminal_error": terminal_error,
            "recovery_checkpoint": checkpoint,
            "context_compaction_required": compaction_required,
            "compaction_maintenance": maintenance,
            "validator_feedback": deepcopy(list(episode.get("validator_feedback") or ())),
            "provider_observability": {
                "provider_call_count": int(audit.get("provider_call_count") or 0),
                "epoch_provider_call_count": int(
                    audit.get("epoch_provider_call_count") or 0
                ),
                "provider_request_audit": deepcopy(list(
                    audit.get("provider_request_audit") or ()
                )),
                "provider_usage": deepcopy(list(
                    audit.get("provider_usage") or ()
                )),
                "context_budget_checks": deepcopy(list(
                    audit.get("context_budget_checks") or ()
                )),
            },
            "business_budget_counters": {
                "format_correction_count": int(audit.get("format_correction_count") or 0),
                "turn_count": int(audit.get("turn_count") or 0),
                "tool_call_count": int(audit.get("tool_call_count") or 0),
                "tool_request_counts": deepcopy(dict(
                    audit.get("tool_request_counts") or {}
                )),
                "no_progress_rounds": int(
                    audit.get("no_progress_rounds") or 0
                ),
                "transport_attempt_count": int(
                    audit.get("transport_attempt_count") or 0
                ),
            },
            "context_compaction_failures": deepcopy(list(
                audit.get("compaction_failures") or ()
            )),
        },
        evidence=evidence,
        transcript=transcript,
        metadata={
            "prompt_version": str(audit.get("prompt_version") or ""),
            "prompt_ref": str(audit.get("prompt_version") or ""),
            "frozen_seed_digest": str(audit.get("seed_digest") or ""),
            "seed_id": seed.seed_id,
            "step_id": seed.failure_packet.step_id,
            "provider_message_count": int(audit.get("provider_message_count") or 0),
            "provider_call_count": int(audit.get("provider_call_count") or 0),
            "epoch_provider_call_count": int(
                audit.get("epoch_provider_call_count") or 0
            ),
            "context_epoch_id": str(audit.get("context_epoch_id") or ""),
            "inherited_material_message_count": int(
                audit.get("inherited_material_message_count") or 0
            ),
            "append_only": bool(audit.get("append_only")),
            "overflow_epoch": int(overflow_epoch),
            "linked_previous_archive_ref": str(linked_previous_archive_ref or ""),
            "hidden_reasoning_excluded": True,
        },
    )


def archive_recovery_compaction_epoch(
    *,
    phase_store: PhaseArchiveStore,
    seed: RecoverySeed,
    prepared: Any,
    source_archive_ref: str,
    overflow_epoch: int,
    compactor_observability: Sequence[Mapping[str, Any]] = (),
    provider_baseline_manifest: Sequence[Mapping[str, Any]] = (),
    business_resume_state: Mapping[str, Any] | None = None,
) -> PhaseArchiveRecord:
    """Archive a validated compaction candidate before local activation."""

    epoch = prepared.epoch
    record = epoch.record
    candidate_digest = _digest({
        "record": record.to_dict(),
        "source_archive_ref": str(source_archive_ref),
        "attempts": [item.to_dict() for item in prepared.attempts],
        "compactor_observability": [
            dict(item) for item in compactor_observability
        ],
        "provider_baseline_manifest": [
            dict(item) for item in provider_baseline_manifest
        ],
        "business_resume_state": dict(business_resume_state or {}),
    })
    invocation_id = (
        f"{seed.seed_id}:compacted-epoch:{int(overflow_epoch)}:"
        f"{str(record.epoch_id)}:{candidate_digest.removeprefix('sha256:')[:24]}"
    )
    return phase_store.archive(
        task_id=seed.failure_packet.task_id,
        phase="recovery-context-epoch",
        invocation_id=invocation_id,
        summary={
            "schema_version": 1,
            "source_failure_id": seed.failure_packet.failure_id,
            "source_archive_ref": str(source_archive_ref),
            "prepared_context_epoch": epoch.to_archive_payload(),
            "compaction_attempts": [
                item.to_dict() for item in prepared.attempts
            ],
            "initial_budget": prepared.initial_budget.to_dict(),
            "compactor_observability": [
                deepcopy(dict(item)) for item in compactor_observability
            ],
            # Exact retained bodies stay single-sourced in the linked source
            # archive.  This manifest stores only source-unit pointers plus
            # newly generated lossy summary messages, so a post-CAS restart
            # can rebuild the active provider prefix without copying source.
            "provider_baseline_manifest": [
                deepcopy(dict(item))
                for item in provider_baseline_manifest
            ],
            "business_resume_state": deepcopy(dict(
                business_resume_state or {}
            )),
        },
        evidence=[{"ref": str(source_archive_ref)}],
        metadata={
            "source_failure_id": seed.failure_packet.failure_id,
            "source_context_epoch_id": str(record.source_epoch_id),
            "active_context_epoch_id": str(record.epoch_id),
            "overflow_epoch": int(overflow_epoch),
            "archive_before_activate": True,
            "hidden_reasoning_excluded": True,
        },
    )


__all__ = [
    "RECOVERY_CHECKPOINT_SCHEMA_VERSION",
    "RECOVERY_OBSERVATION_EXCERPT_CHARS",
    "RecoveryCheckpoint",
    "RecoveryObservationStore",
    "archive_recovery_compaction_epoch",
    "archive_recovery_episode",
    "build_recovery_checkpoint",
]

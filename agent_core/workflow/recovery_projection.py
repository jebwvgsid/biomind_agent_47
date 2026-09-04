"""Causal, transcript-free projection into the typed Recovery boundary.

A failing producer (or its deterministic adapter) creates and persists one
immutable ``FailurePacket`` before the graph enters Recovery.  Recovery resolves
that exact packet; retained outcome channels are deliberately never scanned.
Full execution/validation records stay upstream and are represented only by
bounded facts and immutable references.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import (
    canonical_task_spec_for_state,
    load_plan_state,
    plan_record_hash,
    save_plan_state,
    semantic_plan_hash,
)
from agent_core.task_spec import (
    task_record_hash,
    task_semantic_hash,
    task_spec_is_initialized,
    task_spec_revision,
)
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    InstallSubagentOutcome,
    VerificationOutcome,
)
from agent_core.workflow.stage_contracts import (
    FailurePacket,
    PlanSpecRef,
    RecoverySeed,
    SelectedSkillBundle,
    SelectedSkillRef,
    StageContractError,
    TaskSpecRef,
    freeze,
)


Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]
RuntimeNode = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


_SOURCE_CONTEXT_KEYS = frozenset({
    "messages",
    "message_history",
    "history",
    "conversation",
    "conversation_history",
    "transcript",
    "raw_transcript",
    "prior_transcript",
    "tool_transcript",
    "full_skill_doc",
    "full_skill_docs",
    "full_skill_document",
    "full_skill_documents",
    "skill_doc",
    "skill_docs",
    "skill_document",
    "skill_documents",
    "loaded_skill_documents",
    "exact_text",
    "raw_response",
    "raw_content",
    "system_prompt",
    "prompt",
    "tool_calls",
    "tool_output",
    "tool_outputs",
})

_CURRENT_STEP_FIELDS = (
    "step_id",
    "title",
    "goal",
    "purpose",
    "action",
    "inputs",
    "outputs",
    "dependencies",
    "success_criteria",
    "skill_ids",
    "execution_capability_id",
    "capability_contract_hash",
    "risk_level",
)

_COMMON_RUNTIME_PAYLOAD_FIELDS = (
    "disposition",
    "diagnosis",
    "reason_code",
    "violation_code",
    "summary",
    "decision_id",
    "incident_id",
    "failure_fingerprint",
    "candidate_fingerprint",
    "failure_owner",
    "strategy",
    "repair_instruction",
    "prior_validation_id",
    "question_id",
    "question",
    "error_code",
    "attempt_id",
    "action_id",
    "envelope_identity",
    "status",
    "return_code",
    "command_started",
    "expected_state_generation",
    "current_state_generation",
    "failed_invariant",
    "no_partial_mutation",
)


def _hash(value: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return digest[:24]


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _reject_recursive_context(value: Any, path: tuple[str, ...] = ()) -> None:
    """Reject conversational/Skill payloads while permitting raw stream evidence.

    Typed execution evidence may legitimately contain ``stdout``/``stderr``;
    those records never enter the projection and are replaced by references.
    Conversation transcripts and full Skill documents have no such exception.
    """

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = _normalized_key(key)
            location = ".".join((*path, key))
            if (
                normalized in _SOURCE_CONTEXT_KEYS
                or normalized.endswith("_transcript")
                or normalized.endswith("_messages")
                or "full_skill_doc" in normalized
            ):
                raise StageContractError("STAGE_CONTEXT_LEAK_FORBIDDEN", location)
            _reject_recursive_context(child, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_recursive_context(child, (*path, str(index)))


_SOURCE_CONTRACTS: Mapping[str, type[Any]] = {
    "execute_outcome": ExecuteOutcome,
    "verification_outcome": VerificationOutcome,
    "completion_gate_outcome": VerificationOutcome,
    "commit_step_outcome": CommitStepOutcome,
    "install_subagent_outcome": InstallSubagentOutcome,
}


def _validated_source(source_kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
    contract = _SOURCE_CONTRACTS.get(str(source_kind or ""))
    if contract is None:
        raise StageContractError(
            "RECOVERY_CAUSAL_SOURCE_UNKNOWN", str(source_kind or "<missing>")
        )
    return contract.from_mapping(value).to_dict()


def recovery_source_kind(
    outcome_field: str, outcome: Mapping[str, Any]
) -> str:
    """Return the producer-owned semantic source kind for one exact outcome."""

    if outcome_field == "verification_outcome":
        payload = dict(outcome.get("payload") or {})
        gate = payload.get("completion_gate")
        if (
            str(outcome.get("type") or "") == "rejected"
            and isinstance(gate, Mapping)
            and gate.get("passed") is False
        ):
            return "completion_gate_outcome"
    return str(outcome_field or "")


def is_recovery_failure_source(
    source_kind: str, source: Mapping[str, Any]
) -> bool:
    """Match only outcome semantics whose graph edge enters Recovery."""

    outcome_type = str(source.get("type") or "")
    payload = dict(source.get("payload") or {})
    if source_kind == "execute_outcome":
        if outcome_type in {"execution_failed", "diagnostic_completed"}:
            return True
        return (
            outcome_type == "pre_execution_rejected"
            and str(payload.get("violation_owner") or "").upper()
            in {"VERIFICATION", "DEPENDENCY"}
        )
    if source_kind in {"verification_outcome", "completion_gate_outcome"}:
        return outcome_type not in {"verified", "verified_with_warnings", "harness_error"}
    if source_kind == "commit_step_outcome":
        return outcome_type in {"commit_conflict", "contract_rejected"}
    if source_kind == "install_subagent_outcome":
        return outcome_type == "installation_failed"
    return False


def _unique_text(*values: Any) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return tuple(result)


def _compact_diagnostic(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "execution_id",
        "attempt_id",
        "action_id",
        "evidence_ref",
        "diagnostic_id",
        "validation_id",
        "status",
        "returncode",
        "exit_code",
        "failure_fingerprint",
    ):
        item = value.get(key)
        if item not in (None, "", [], {}):
            result[key] = item
    refs = _unique_text(value.get("evidence_refs") or ())
    if refs:
        result["evidence_refs"] = list(refs)
    return result


def _compact_budget(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = (
        "incident_id",
        "ordinal",
        "limit",
        "remaining",
        "consumed",
        "progress_proven",
        "source_fingerprint",
    )
    return {
        key: value[key]
        for key in allowed
        if key in value and value[key] not in (None, "", [], {})
    }


def _runtime_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: deepcopy(payload[key])
        for key in _COMMON_RUNTIME_PAYLOAD_FIELDS
        if key in payload and payload[key] not in (None, "", [], {})
    }
    for key in ("contract_violations", "missing_facts", "new_evidence_refs"):
        if payload.get(key) not in (None, "", [], {}):
            result[key] = deepcopy(payload[key])
    diagnostic = _compact_diagnostic(payload.get("diagnostic_record"))
    if diagnostic:
        result["diagnostic_record"] = diagnostic
    budget = _compact_budget(payload.get("budget_receipt"))
    if budget:
        result["budget_receipt"] = budget
    blocked = payload.get("blocked_evidence")
    if isinstance(blocked, Mapping):
        result["blocked_evidence"] = deepcopy(dict(blocked))
    return result


def _runtime_source(source_kind: str, source: Mapping[str, Any]) -> Mapping[str, Any]:
    _reject_recursive_context(source, (source_kind,))
    projected: dict[str, Any] = {
        "type": str(source.get("type") or ""),
        "summary": str(source.get("summary") or ""),
        "scope": {
            key: value
            for key, value in dict(source.get("scope") or {}).items()
            if key in {
                "task_id",
                "plan_id",
                "plan_version",
                "step_id",
                "state_generation",
                "action_id",
                "attempt_id",
                "validation_id",
            }
            and value not in (None, "")
        },
        "payload": _runtime_payload(dict(source.get("payload") or {})),
    }
    refs = _unique_text(source.get("evidence_refs") or ())
    if refs:
        projected["evidence_refs"] = list(refs)
    return projected


def _task_ref(durable: Mapping[str, Any], task_id: str) -> TaskSpecRef:
    current = dict(durable.get("current_task") or {})
    try:
        spec = canonical_task_spec_for_state(durable)
    except Exception as exc:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", f"Recovery TaskSpec: {type(exc).__name__}"
        ) from exc
    if not task_spec_is_initialized(spec):
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery TaskSpec is not initialized"
        )
    spec_task_id = str(spec.get("task_id") or "")
    if spec_task_id and spec_task_id != task_id:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery TaskSpec task_id"
        )
    revision = task_spec_revision(spec)
    semantic_hash = task_semantic_hash(spec)
    record_hash = task_record_hash(spec)
    return TaskSpecRef(
        schema_version=1,
        task_id=task_id,
        task_revision=revision,
        task_semantic_hash=semantic_hash,
        task_record_hash=record_hash,
        task_spec_ref=str(
            current.get("task_spec_ref")
            or current.get("task_snapshot_ref")
            or ""
        ),
    )


def _plan_ref(durable: Mapping[str, Any], task_ref: TaskSpecRef) -> PlanSpecRef:
    task_id = task_ref.task_id
    current = dict(durable.get("current_plan") or {})
    spec = current.get("plan_spec")
    if not isinstance(spec, Mapping) or not spec:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec is not materialized"
        )
    try:
        revisions = {
            int(value)
            for value in (
                current.get("plan_revision"),
                spec.get("plan_revision"),
                durable.get("plan_revision"),
            )
            if value not in (None, "")
        }
    except (TypeError, ValueError) as exc:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec revision is invalid"
        ) from exc
    if len(revisions) > 1:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec revision mismatch"
        )
    plan_revision = next(iter(revisions), 0)
    if plan_revision < 1:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec revision is invalid"
        )
    plan_id = str(spec.get("plan_id") or durable.get("plan_id") or "")
    plan_version = str(
        spec.get("plan_version")
        or durable.get("plan_version")
        or ""
    )
    if not plan_id or not plan_version:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec identity is incomplete"
        )
    if durable.get("plan_id") and str(durable.get("plan_id")) != plan_id:
        raise StageContractError("STAGE_SCOPE_MISMATCH", "Recovery PlanSpec plan_id")
    if durable.get("plan_version") and str(durable.get("plan_version")) != plan_version:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery PlanSpec plan_version"
        )
    semantic_hash = "sha256:" + semantic_plan_hash(dict(spec)).removeprefix("sha256:")
    record_hash = plan_record_hash(dict(spec))
    declared_semantic = str(current.get("plan_semantic_hash") or "")
    declared_record = str(current.get("plan_record_hash") or "")
    if declared_semantic and declared_semantic != semantic_hash:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec semantic hash mismatch"
        )
    if declared_record and declared_record != record_hash:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec record hash mismatch"
        )
    base_revision = int(
        current.get("base_task_revision")
        or spec.get("generated_from_task_revision")
        or 0
    )
    base_hash = str(
        current.get("base_task_semantic_hash")
        or spec.get("base_task_semantic_hash")
        or ""
    )
    if base_revision and base_revision != task_ref.task_revision:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec task revision is stale"
        )
    if base_hash and base_hash != task_ref.task_semantic_hash:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec TaskSpec hash is stale"
        )
    return PlanSpecRef(
        schema_version=1,
        task_id=task_id,
        plan_id=plan_id,
        plan_version=plan_version,
        plan_revision=plan_revision,
        plan_semantic_hash=semantic_hash,
        plan_record_hash=record_hash,
        plan_snapshot_ref=str(
            current.get("plan_snapshot_ref")
            or (durable.get("current_approval") or {}).get("plan_snapshot_ref")
            or ""
        ),
    )


def _selected_skill_refs(durable: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_state = dict(durable.get("task_state") or {})
    candidates = (
        (task_state.get("skill_access_state") or {}).get("selected_skill_refs"),
        task_state.get("selected_skill_refs"),
        (durable.get("skill_access_state") or {}).get("selected_skill_refs"),
        durable.get("selected_skill_refs"),
    )
    for value in candidates:
        if isinstance(value, (list, tuple)) and value:
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _skill_bundle(
    durable: Mapping[str, Any], task_revision: int, step_id: str
) -> SelectedSkillBundle:
    parsed: list[SelectedSkillRef] = []
    for ref in _selected_skill_refs(durable):
        status = str(ref.get("status") or "active").lower()
        scope = str(ref.get("scope") or "task").lower()
        target = str(ref.get("target_step_id") or "")
        if status != "active" or (scope == "step" and target != step_id):
            continue
        _reject_recursive_context(
            ref, ("selected_skill_refs", str(ref.get("skill_id") or "<unknown>"))
        )
        parsed.append(SelectedSkillRef(
            schema_version=1,
            skill_id=str(ref.get("skill_id") or ""),
            snapshot_id=str(ref.get("snapshot_id") or ""),
            content_hash=str(
                ref.get("content_hash") or ref.get("document_digest") or ""
            ),
            scope=scope,
            source_path=str(ref.get("source_path") or ""),
            target_step_id=target if scope == "step" else "",
        ))
    wire = [item.to_dict() for item in parsed]
    return SelectedSkillBundle(
        schema_version=1,
        bundle_id="recovery-skills-" + _hash({
            "task_revision": task_revision,
            "skills": wire,
        }),
        task_revision=task_revision,
        skills=tuple(parsed),
        selection_summary=(
            "Active immutable Skill snapshots applicable to the failed step."
            if parsed
            else "No immutable Skill snapshot is bound to this step."
        ),
    )


def _source_scope(source_kind: str, source: Mapping[str, Any]) -> dict[str, Any]:
    if source_kind.endswith("outcome"):
        return dict(source.get("scope") or {})
    return {
        key: source[key]
        for key in ("task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id")
        if source.get(key) not in (None, "")
    }


def _source_payload(source_kind: str, source: Mapping[str, Any]) -> dict[str, Any]:
    return (
        dict(source.get("payload") or {})
        if source_kind.endswith("outcome")
        else dict(source)
    )


def _assert_scope(
    task_id: str,
    durable: Mapping[str, Any],
    source_kind: str,
    source: Mapping[str, Any],
) -> str:
    scope = _source_scope(source_kind, source)
    payload = _source_payload(source_kind, source)
    record = (
        dict(payload.get("execution_record") or {})
        if isinstance(payload.get("execution_record"), Mapping)
        else {}
    )
    validation = (
        dict(payload.get("validation_evidence") or {})
        if isinstance(payload.get("validation_evidence"), Mapping)
        else {}
    )
    diagnostic = (
        dict(payload.get("diagnostic_record") or {})
        if isinstance(payload.get("diagnostic_record"), Mapping)
        else {}
    )

    def require_one(field: str, *values: Any) -> str:
        observed = {str(value) for value in values if value not in (None, "")}
        if len(observed) > 1:
            raise StageContractError(
                "STAGE_SCOPE_MISMATCH", f"Recovery source {field}"
            )
        return next(iter(observed), "")

    require_one(
        "task_id",
        task_id,
        durable.get("task_id"),
        scope.get("task_id"),
        record.get("task_id"),
        validation.get("task_id"),
        diagnostic.get("task_id"),
    )
    require_one(
        "plan_id",
        durable.get("plan_id"),
        scope.get("plan_id"),
        record.get("plan_id"),
        validation.get("plan_id"),
        diagnostic.get("plan_id"),
    )
    require_one(
        "plan_version",
        durable.get("plan_version"),
        scope.get("plan_version"),
        record.get("plan_version"),
        validation.get("plan_version"),
        diagnostic.get("plan_version"),
    )
    require_one(
        "action_id",
        scope.get("action_id"),
        payload.get("action_id"),
        record.get("action_id"),
        validation.get("action_id"),
        diagnostic.get("action_id"),
    )
    require_one(
        "attempt_id",
        scope.get("attempt_id"),
        payload.get("attempt_id"),
        record.get("attempt_id"),
        validation.get("attempt_id"),
        diagnostic.get("attempt_id"),
    )
    durable_step = str(durable.get("current_step_id") or "")
    step_id = require_one(
        "step_id",
        durable_step,
        scope.get("step_id"),
        payload.get("step_id"),
        record.get("step_id"),
        validation.get("step_id"),
        diagnostic.get("step_id"),
    )
    if not step_id:
        raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery step_id")
    return step_id


def _current_step(durable: Mapping[str, Any], step_id: str) -> dict[str, Any]:
    plan_spec = dict((durable.get("current_plan") or {}).get("plan_spec") or {})
    source = next((
        item
        for item in plan_spec.get("steps", ()) or ()
        if isinstance(item, Mapping) and str(item.get("step_id") or "") == step_id
    ), {})
    if not source:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery current step is absent from PlanSpec"
        )
    result = {
        key: source[key]
        for key in _CURRENT_STEP_FIELDS
        if key in source and source[key] not in (None, "", [], {})
    }
    result["step_id"] = step_id
    if "skill_ids" not in result and source.get("selected_skill_id"):
        result["skill_ids"] = [str(source["selected_skill_id"])]
    return result


def _dependency_ids(step: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    for item in step.get("dependencies", ()) or ():
        if isinstance(item, Mapping):
            value = item.get("step_id") or item.get("dependency_step_id") or item.get("id")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _row_refs(value: Any, *keys: str) -> tuple[str, ...]:
    result: list[str] = []
    items = value if isinstance(value, (list, tuple)) else ()
    for item in items:
        if isinstance(item, Mapping):
            candidate = next((item.get(key) for key in keys if item.get(key)), None)
        else:
            candidate = item
        text = str(candidate or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _dependency_outcomes(
    durable: Mapping[str, Any], current_step: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    steps = {
        str(item.get("step_id") or ""): item
        for item in durable.get("steps", ()) or ()
        if isinstance(item, Mapping) and item.get("step_id")
    }
    receipts = dict((durable.get("task_state") or {}).get("typed_step_commit_receipts") or {})
    result: list[Mapping[str, Any]] = []
    for dependency_id in _dependency_ids(current_step):
        step = dict(steps.get(dependency_id) or {})
        status = str(step.get("status") or "unknown")
        row: dict[str, Any] = {
            "step_id": dependency_id,
            "status": status,
            "summary": str(
                step.get("outcome_summary")
                or step.get("summary")
                or step.get("title")
                or f"Dependency status: {status}."
            ),
        }
        matching_receipt = next((
            (receipt_id, receipt)
            for receipt_id, receipt in receipts.items()
            if isinstance(receipt, Mapping)
            and str(
                ((receipt.get("outcome") or {}).get("payload") or {}).get(
                    "completed_step_id"
                )
                or ""
            ) == dependency_id
        ), None)
        if matching_receipt:
            row["outcome_ref"] = str(matching_receipt[0])
        evidence_refs = _unique_text(step.get("evidence_refs") or ())
        if evidence_refs:
            row["evidence_refs"] = list(evidence_refs)
        artifact_refs = _row_refs(
            step.get("artifacts"), "artifact_id", "content_ref", "path", "id"
        )
        if artifact_refs:
            row["artifact_refs"] = list(artifact_refs)
        binding_refs = _row_refs(
            step.get("accepted_bindings"), "binding_id", "value_ref", "name"
        )
        if binding_refs:
            row["binding_refs"] = list(binding_refs)
        result.append(freeze(row))
    return tuple(result)


def _evidence_refs(source: Mapping[str, Any]) -> tuple[str, ...]:
    payload = dict(source.get("payload") or {})
    execution = payload.get("execution_record")
    diagnostic = payload.get("diagnostic_record")
    validation = payload.get("validation_evidence")
    return _unique_text(
        source.get("evidence_refs") or (),
        execution.get("evidence_refs") if isinstance(execution, Mapping) else (),
        execution.get("execution_id") if isinstance(execution, Mapping) else "",
        diagnostic.get("evidence_refs") if isinstance(diagnostic, Mapping) else (),
        diagnostic.get("evidence_ref") if isinstance(diagnostic, Mapping) else "",
        diagnostic.get("execution_id") if isinstance(diagnostic, Mapping) else "",
        validation.get("evidence_refs") if isinstance(validation, Mapping) else (),
        validation.get("validation_id") if isinstance(validation, Mapping) else "",
    )


def _failure_identity(
    source_kind: str,
    source: Mapping[str, Any],
) -> tuple[str, str, str, bool]:
    payload = _source_payload(source_kind, source)
    outcome_type = str(source.get("type") or payload.get("failure_type") or "recovery_fact")
    if source_kind == "execute_outcome":
        if outcome_type == "execution_failed":
            record = dict(payload.get("execution_record") or {})
            return (
                outcome_type,
                str(payload.get("failure_owner") or "GENERATED_ACTION"),
                str(record.get("error_code") or payload.get("error_code") or "EXECUTION_FAILED"),
                True,
            )
        if outcome_type == "diagnostic_completed":
            return outcome_type, "DIAGNOSTIC", "DIAGNOSTIC_EVIDENCE_RECORDED", True
        return (
            outcome_type,
            str(payload.get("violation_owner") or "INTERNAL_PROTOCOL"),
            str(payload.get("violation_code") or "PRE_EXECUTION_REJECTED"),
            True,
        )
    if source_kind in {"verification_outcome", "completion_gate_outcome"}:
        owner = str(payload.get("failure_owner") or "VERIFICATION")
        code = str(payload.get("error_code") or {
            "rejected": "VERIFICATION_REJECTED",
            "incomplete_evidence": "VERIFICATION_EVIDENCE_INCOMPLETE",
            "diagnostic_evidence_recorded": "DIAGNOSTIC_EVIDENCE_RECORDED",
            "verification_error": "VERIFICATION_ERROR",
            "harness_error": "VERIFICATION_HARNESS_ERROR",
        }.get(outcome_type, "VERIFICATION_FAILED"))
        # Match the existing TypedRecovery disposition table: only
        # verification_error is terminal internal protocol; other
        # Verification failures enter a bounded corrective path.
        return outcome_type, owner, code, outcome_type != "verification_error"
    if source_kind == "commit_step_outcome":
        if outcome_type == "commit_conflict":
            return outcome_type, "PLAN", "COMMIT_CONFLICT", True
        return (
            outcome_type,
            "INTERNAL_PROTOCOL",
            str(payload.get("reason_code") or "COMMIT_CONTRACT_REJECTED"),
            False,
        )
    if source_kind == "install_subagent_outcome":
        return (
            outcome_type,
            "RUNTIME_CAPABILITY",
            str(payload.get("reason_code") or "INSTALLATION_FAILED"),
            True,
        )

    raise StageContractError("RECOVERY_CAUSAL_SOURCE_UNKNOWN", source_kind)


@dataclass(frozen=True)
class RecoveryProjection:
    """Validated input consumed by ``TypedRecovery.run``."""

    source_kind: str
    source: Mapping[str, Any]
    source_fingerprint: str
    failure_packet: FailurePacket
    recovery_seed: RecoverySeed


def _content_ref(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:sha256:{digest}"


def _diagnostic_facts(
    source_kind: str, source: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    payload = _source_payload(source_kind, source)
    execution = (
        dict(payload.get("execution_record") or {})
        if isinstance(payload.get("execution_record"), Mapping)
        else {}
    )
    return_code = (
        payload.get("return_code")
        if payload.get("return_code") is not None
        else execution.get("returncode")
    )
    facts: list[Mapping[str, Any]] = []
    if return_code is not None:
        facts.append({
            "fact_id": "failure-return-code",
            "kind": "return_code",
            "summary": f"Process return code: {return_code}",
            "metadata": {"return_code": return_code},
        })
    status = payload.get("status")
    if status not in (None, ""):
        facts.append({
            "fact_id": "failure-status",
            "kind": "status",
            "summary": f"Producer status: {status}",
            "metadata": {"status": status},
        })
    return tuple(freeze(item) for item in facts)


def build_causal_failure_record(
    source_kind: str,
    source: Mapping[str, Any],
    durable: Mapping[str, Any],
    *,
    task_id: str = "",
    inspection_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the immutable record owned by one failing producer adapter."""

    raw_source = _validated_source(source_kind, source)
    if not is_recovery_failure_source(source_kind, raw_source):
        raise StageContractError(
            "RECOVERY_CAUSAL_SOURCE_NOT_FAILURE",
            f"{source_kind}:{raw_source.get('type')}",
        )
    resolved_task_id = str(
        task_id
        or (raw_source.get("scope") or {}).get("task_id")
        or durable.get("task_id")
        or ""
    )
    if not resolved_task_id:
        raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery task_id")
    step_id = _assert_scope(
        resolved_task_id, durable, source_kind, raw_source
    )
    task_ref = _task_ref(durable, resolved_task_id)
    plan_ref = _plan_ref(durable, task_ref)
    runtime_source = _runtime_source(source_kind, raw_source)
    raw_payload = _source_payload(source_kind, raw_source)
    payload = _source_payload(source_kind, runtime_source)
    failure_type, failure_owner, error_code, retryable = _failure_identity(
        source_kind, raw_source
    )
    scope = _source_scope(source_kind, raw_source)
    execution = raw_payload.get("execution_record")
    attempt_ref = str(
        scope.get("attempt_id")
        or raw_payload.get("attempt_id")
        or (execution.get("attempt_id") if isinstance(execution, Mapping) else "")
        or ""
    )
    action_ref = str(
        scope.get("action_id")
        or raw_payload.get("action_id")
        or (execution.get("action_id") if isinstance(execution, Mapping) else "")
        or ""
    )
    source_outcome_ref = _content_ref("outcome", raw_source)
    source_fingerprint = _hash({"kind": source_kind, "source": raw_source})
    fingerprint = str(
        payload.get("failure_fingerprint")
        or payload.get("candidate_fingerprint")
        or source_fingerprint
    )
    summary = str(
        payload.get("diagnosis")
        or payload.get("reason_code")
        or payload.get("violation_code")
        or runtime_source.get("summary")
        or error_code
    )
    evidence_refs = _evidence_refs(raw_source)
    packet = FailurePacket(
        schema_version=1,
        failure_id="failure-" + source_fingerprint,
        task_id=resolved_task_id,
        step_id=step_id,
        failure_type=failure_type,
        failure_owner=failure_owner,
        error_code=error_code,
        summary=summary,
        retryable=retryable,
        evidence_refs=evidence_refs,
        source=source_kind,
        source_outcome_ref=source_outcome_ref,
        plan_spec_ref=plan_ref,
        attempt_ref=attempt_ref,
        action_ref=action_ref,
        failure_fingerprint=fingerprint,
        diagnostic_facts=_diagnostic_facts(source_kind, raw_source),
        inspection_refs=_unique_text(source_outcome_ref, inspection_refs),
    )
    packet_wire = packet.to_dict()
    packet_ref = _content_ref("failure-packet", packet_wire)
    failure_source = deepcopy(runtime_source)
    return {
        "schema_version": 1,
        "packet_ref": packet_ref,
        "source_kind": source_kind,
        "source_outcome_ref": source_outcome_ref,
        "source_fingerprint": source_fingerprint,
        "failure_source_ref": _content_ref("failure-source", failure_source),
        "failure_packet": packet_wire,
        "failure_source": failure_source,
    }


class CausalFailurePacketBinder:
    """Persist an exact producer packet and expose its graph-state projection."""

    def __init__(
        self,
        *,
        data_path: str,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
    ) -> None:
        self.data_path = str(data_path)
        self.loader = loader
        self.saver = saver

    @staticmethod
    def _state_update(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "active_failure_packet_ref": str(record.get("packet_ref") or ""),
            "failure_packet": deepcopy(dict(record.get("failure_packet") or {})),
            "failure_source": deepcopy(dict(record.get("failure_source") or {})),
        }

    def bind(
        self,
        state: Mapping[str, Any],
        source_kind: str,
        source: Mapping[str, Any],
    ) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery task_id")
        durable = self.loader(self.data_path, task_id)
        step_worker = state.get("step_worker_outcome")
        step_archive_ref = (
            str(step_worker.get("archive_ref") or "")
            if isinstance(step_worker, Mapping)
            else ""
        )
        record = build_causal_failure_record(
            source_kind,
            source,
            durable,
            task_id=task_id,
            inspection_refs=((step_archive_ref,) if step_archive_ref else ()),
        )
        packet_ref = str(record["packet_ref"])
        task_state = dict(durable.get("task_state") or {})
        registry = dict(task_state.get("causal_failure_packets") or {})
        prior = registry.get(packet_ref)
        if isinstance(prior, Mapping):
            if dict(prior) != record:
                raise StageContractError(
                    "RECOVERY_CAUSAL_PACKET_REF_COLLISION", packet_ref
                )
            if str(task_state.get("active_failure_packet_ref") or "") == packet_ref:
                return self._state_update(record)

        candidate = deepcopy(durable)
        candidate_task = dict(candidate.get("task_state") or {})
        candidate_registry = dict(candidate_task.get("causal_failure_packets") or {})
        candidate_registry[packet_ref] = deepcopy(record)
        candidate_task["causal_failure_packets"] = candidate_registry
        candidate_task["active_failure_packet_ref"] = packet_ref
        candidate["task_state"] = candidate_task
        try:
            self.saver(self.data_path, task_id, candidate)
        except StateCASConflict:
            refreshed = self.loader(self.data_path, task_id)
            refreshed_task = dict(refreshed.get("task_state") or {})
            refreshed_record = dict(
                (refreshed_task.get("causal_failure_packets") or {}).get(packet_ref)
                or {}
            )
            if (
                refreshed_record == record
                and str(refreshed_task.get("active_failure_packet_ref") or "")
                == packet_ref
            ):
                return self._state_update(record)
            raise
        return self._state_update(record)


def bind_causal_failure_node(
    handler: RuntimeNode,
    *,
    binder: CausalFailurePacketBinder,
    outcome_field: str,
) -> RuntimeNode:
    """Attach producer-owned packet persistence to one validated typed node."""

    async def wrapped(state: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(await handler(state))
        raw = result.get(outcome_field)
        if not isinstance(raw, Mapping):
            return result
        source_kind = recovery_source_kind(outcome_field, raw)
        if is_recovery_failure_source(source_kind, raw):
            result.update(binder.bind(state, source_kind, raw))
        return result

    return wrapped


def _resolve_causal_record(
    state: Mapping[str, Any], durable: Mapping[str, Any]
) -> dict[str, Any] | None:
    task_state = dict(durable.get("task_state") or {})
    registry = dict(task_state.get("causal_failure_packets") or {})
    active_ref = str(task_state.get("active_failure_packet_ref") or "")
    state_ref = str(state.get("active_failure_packet_ref") or "")
    consumed = dict(task_state.get("consumed_failure_packet_refs") or {})

    if state_ref and active_ref and state_ref != active_ref:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_STALE",
            f"edge={state_ref}; active={active_ref}",
        )
    packet_ref = state_ref or active_ref
    if not packet_ref:
        return None
    if state_ref and not active_ref and state_ref not in consumed:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_STALE", f"inactive={state_ref}"
        )
    record = registry.get(packet_ref)
    if not isinstance(record, Mapping):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_MISSING", packet_ref
        )
    resolved = dict(record)
    if str(resolved.get("packet_ref") or "") != packet_ref:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_REF_MISMATCH", packet_ref
        )
    packet = FailurePacket.from_mapping(resolved.get("failure_packet") or {})
    if _content_ref("failure-packet", packet.to_dict()) != packet_ref:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_REF_MISMATCH", packet_ref
        )
    if state.get("failure_packet"):
        edge_packet = FailurePacket.from_mapping(state.get("failure_packet") or {})
        if edge_packet.to_dict() != packet.to_dict():
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_EDGE_MISMATCH", packet_ref
            )
    source_kind = str(resolved.get("source_kind") or "")
    source = dict(resolved.get("failure_source") or {})
    if _content_ref("failure-source", source) != str(
        resolved.get("failure_source_ref") or ""
    ):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_SOURCE_MISMATCH", packet_ref
        )
    if packet.source != source_kind:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_SOURCE_MISMATCH", packet_ref
        )
    if str(resolved.get("source_outcome_ref") or "") != packet.source_outcome_ref:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_SOURCE_MISMATCH", packet_ref
        )
    return resolved


def project_recovery_seed(
    state: Mapping[str, Any], durable: Mapping[str, Any]
) -> RecoveryProjection | None:
    """Resolve the one edge-bound or durable-active packet without scanning."""

    record = _resolve_causal_record(state, durable)
    if record is None:
        return None
    source_kind = str(record.get("source_kind") or "")
    runtime_source = dict(record.get("failure_source") or {})
    source_fingerprint = str(record.get("source_fingerprint") or "")
    packet = FailurePacket.from_mapping(record.get("failure_packet") or {})
    task_id = str(state.get("task_id") or durable.get("task_id") or "")
    if not task_id:
        raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery task_id")
    task_ref = _task_ref(durable, task_id)
    plan_ref = _plan_ref(durable, task_ref)
    if packet.task_id != task_id or packet.plan_spec_ref != plan_ref:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery causal packet task/plan identity"
        )
    if packet.step_id != str(durable.get("current_step_id") or ""):
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery causal packet step identity"
        )
    current_step = _current_step(durable, packet.step_id)
    dependencies = _dependency_outcomes(durable, current_step)
    bundle = _skill_bundle(durable, task_ref.task_revision, packet.step_id)
    seed = RecoverySeed(
        schema_version=1,
        seed_id="recovery-seed-" + source_fingerprint,
        task_spec_ref=task_ref,
        plan_spec_ref=plan_ref,
        selected_skill_bundle=bundle,
        current_step=current_step,
        failure_packet=packet,
        dependency_outcomes=dependencies,
        evidence_refs=packet.evidence_refs,
    )
    # A final wire round-trip proves the complete recursive boundary, rather
    # than only the individual component constructors, is leak free.
    RecoverySeed.from_mapping(seed.to_dict())
    return RecoveryProjection(
        source_kind=source_kind,
        source=runtime_source,
        source_fingerprint=source_fingerprint,
        failure_packet=packet,
        recovery_seed=seed,
    )


__all__ = [
    "CausalFailurePacketBinder",
    "RecoveryProjection",
    "bind_causal_failure_node",
    "build_causal_failure_record",
    "is_recovery_failure_source",
    "project_recovery_seed",
    "recovery_source_kind",
]

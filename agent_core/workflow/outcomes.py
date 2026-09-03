"""Strict semantic outcomes for the schema-2 runtime graph.

Outcome objects describe *what happened*.  They deliberately cannot carry a
LangGraph destination or legacy Controller transition metadata.  Node
selection is owned by conditional edges outside this module.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Mapping


OUTCOME_SCHEMA_VERSION = 1
WORKFLOW_ROUTING_SCHEMA_LEGACY = 1
WORKFLOW_ROUTING_SCHEMA_TYPED = 2

_FORBIDDEN_CONTROL_KEYS = frozenset({
    "next_step",
    "next_route",
    "target_route",
    "goto",
    "dispatch_route",
    "effective_route",
    "route_resolution",
    "proposed_route",
    "node_recommended_next_step",
    "required_next_action",
    "pending_recovery_action",
    "graph_node",
    "graph_node_name",
    "source_graph_node",
    "destination_graph_node",
    "controller_transition",
    "controller_transition_metadata",
    "command_goto",
    "resume_route_hint",
    "resume_step",
    "terminal_route",
    "pre_guard_effective_route",
    "override_source",
    "override_decision_id",
    "route_event_id",
    "resolved_resume_node",
})


class OutcomeContractError(ValueError):
    """Stable fail-closed error raised by a typed outcome boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "OUTCOME_CONTRACT_ERROR")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _freeze_json(value: Any, *, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise OutcomeContractError(
                    "OUTCOME_JSON_INVALID",
                    f"{'.'.join(path) or '<root>'} contains a non-string key",
                )
            key = raw_key.strip()
            if not key:
                raise OutcomeContractError(
                    "OUTCOME_JSON_INVALID", "empty object keys are not allowed"
                )
            if key.lower() in _FORBIDDEN_CONTROL_KEYS:
                location = ".".join((*path, key))
                raise OutcomeContractError(
                    "OUTCOME_ROUTE_AUTHORITY_FORBIDDEN", location
                )
            frozen[key] = _freeze_json(item, path=(*path, key))
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, path=(*path, str(index)))
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OutcomeContractError(
                "OUTCOME_JSON_INVALID", "non-finite numbers are not allowed"
            )
        return value
    raise OutcomeContractError(
        "OUTCOME_JSON_INVALID", f"unsupported value type: {type(value).__name__}"
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def route_blind_state_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively remove legacy routing authority before a schema-2 node."""

    def scrub(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): scrub(child)
                for key, child in item.items()
                if str(key).strip().lower() not in _FORBIDDEN_CONTROL_KEYS
            }
        if isinstance(item, list):
            return [scrub(child) for child in item]
        if isinstance(item, tuple):
            return tuple(scrub(child) for child in item)
        return item

    return scrub(dict(value))


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OutcomeContractError("OUTCOME_FIELD_REQUIRED", field)
    return text


@dataclass(frozen=True)
class StrictOutcome:
    """Frozen JSON outcome with node-specific type and payload validation."""

    schema_version: int
    type: str
    summary: str
    scope: Mapping[str, Any]
    payload: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()

    OUTCOME_NAME: ClassVar[str] = "StrictOutcome"
    ALLOWED_TYPES: ClassVar[frozenset[str]] = frozenset()
    REQUIRED_PAYLOAD: ClassVar[Mapping[str, frozenset[str]]] = MappingProxyType({})

    def __post_init__(self) -> None:
        try:
            schema_version = int(self.schema_version)
        except (TypeError, ValueError) as exc:
            raise OutcomeContractError(
                "OUTCOME_SCHEMA_UNSUPPORTED", f"schema_version={self.schema_version}"
            ) from exc
        if schema_version != OUTCOME_SCHEMA_VERSION:
            raise OutcomeContractError(
                "OUTCOME_SCHEMA_UNSUPPORTED", f"schema_version={schema_version}"
            )
        object.__setattr__(self, "schema_version", schema_version)

        outcome_type = _required_text(self.type, "type")
        if outcome_type not in self.ALLOWED_TYPES:
            raise OutcomeContractError(
                "OUTCOME_TYPE_UNKNOWN",
                f"{self.OUTCOME_NAME}.v1 type={outcome_type}",
            )
        object.__setattr__(self, "type", outcome_type)
        object.__setattr__(self, "summary", _required_text(self.summary, "summary"))

        if not isinstance(self.scope, Mapping) or not isinstance(self.payload, Mapping):
            raise OutcomeContractError(
                "OUTCOME_FIELD_INVALID", "scope and payload must be objects"
            )
        scope = _freeze_json(dict(self.scope), path=("scope",))
        payload = _freeze_json(dict(self.payload), path=("payload",))
        if not str(scope.get("task_id") or "").strip():
            raise OutcomeContractError("OUTCOME_FIELD_REQUIRED", "scope.task_id")
        required = self.REQUIRED_PAYLOAD.get(outcome_type, frozenset())
        missing = sorted(key for key in required if key not in payload)
        if missing:
            raise OutcomeContractError(
                "OUTCOME_PAYLOAD_INCOMPLETE",
                f"{self.OUTCOME_NAME}.v1/{outcome_type}: {', '.join(missing)}",
            )
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(str(item).strip() for item in self.evidence_refs if str(item).strip()),
        )
        json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StrictOutcome":
        if not isinstance(value, Mapping):
            raise OutcomeContractError(
                "OUTCOME_OBJECT_REQUIRED", f"received {type(value).__name__}"
            )
        allowed = {
            "schema_version", "type", "summary", "scope", "payload", "evidence_refs"
        }
        required = {"schema_version", "type", "summary", "scope", "payload"}
        missing = sorted(required - set(value))
        extra = sorted(set(value) - allowed)
        if missing:
            raise OutcomeContractError(
                "OUTCOME_FIELD_REQUIRED", ", ".join(missing)
            )
        if extra:
            raise OutcomeContractError(
                "OUTCOME_FIELD_UNKNOWN", ", ".join(extra)
            )
        refs = value.get("evidence_refs") or ()
        if isinstance(refs, (str, bytes)) or not isinstance(refs, (list, tuple)):
            raise OutcomeContractError(
                "OUTCOME_FIELD_INVALID", "evidence_refs must be an array"
            )
        return cls(
            schema_version=value.get("schema_version"),
            type=value.get("type"),
            summary=value.get("summary"),
            scope=value.get("scope"),
            payload=value.get("payload"),
            evidence_refs=tuple(refs),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "type": self.type,
            "summary": self.summary,
            "scope": _thaw_json(self.scope),
            "payload": _thaw_json(self.payload),
            "evidence_refs": list(self.evidence_refs),
        }


def _requirements(**rows: tuple[str, ...]) -> Mapping[str, frozenset[str]]:
    return MappingProxyType({key: frozenset(value) for key, value in rows.items()})


class GenerateOutcome(StrictOutcome):
    OUTCOME_NAME = "GenerateOutcome"
    ALLOWED_TYPES = frozenset({
        "action_ready", "completion_claimed", "user_input_needed",
        "plan_revision_needed", "skill_context_needed", "candidate_rejected",
        "provider_failed", "stale_result", "internal_protocol_error", "blocked",
        "cancelled", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        action_ready=("action", "action_id", "candidate_fingerprint", "validation_evidence"),
        completion_claimed=("step_id", "candidate_fingerprint"),
        user_input_needed=("question_id", "question", "requested_slots", "owner"),
        plan_revision_needed=("step_id", "reason_code", "contract_violations"),
        skill_context_needed=("step_id", "requested_capability", "continuation_fingerprint"),
        candidate_rejected=("step_id", "violation_owner", "candidate_fingerprint", "error_fingerprint", "feedback", "budget_receipt", "repairable"),
        provider_failed=("error_code", "provider_attempt_ref", "claim_status", "retryability_policy"),
        stale_result=("changed_preconditions", "invocation_id", "consumed_revision_hashes", "current_revision_hashes", "counts_as_failure"),
        internal_protocol_error=("error_code", "component"),
        blocked=("owner", "reason", "terminal_receipt"),
        cancelled=("cancellation_source", "cancellation_receipt"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class ExecuteOutcome(StrictOutcome):
    OUTCOME_NAME = "ExecuteOutcome"
    ALLOWED_TYPES = frozenset({
        "execution_succeeded", "execution_failed", "diagnostic_completed",
        "pre_execution_rejected", "user_input_needed", "installation_requested",
        "approval_rejected", "stale_result", "lightweight_completed", "blocked",
        "cancelled", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        execution_succeeded=("execution_record", "action_id", "attempt_id", "return_code", "validation"),
        execution_failed=("execution_record", "action_id", "attempt_id", "return_code", "failure_fingerprint", "progress_snapshot"),
        diagnostic_completed=("diagnostic_record", "envelope_identity", "attempt_id", "status"),
        pre_execution_rejected=("violation_owner", "violation_code", "candidate_fingerprint", "command_started", "budget_receipt"),
        user_input_needed=("question_id", "question", "requested_slots", "owner"),
        installation_requested=("tool_or_capability", "requirement_evidence", "approval_policy", "attempt_fingerprint"),
        approval_rejected=("approval_request_id", "guidance", "command_started"),
        stale_result=("changed_preconditions", "action_id", "attempt_id", "audit_ref", "counts_as_failure"),
        lightweight_completed=("execution_record", "status", "task_mode"),
        blocked=("owner", "blocker_evidence", "command_started"),
        cancelled=("cancellation_receipt", "command_started"),
        harness_error=("error_code", "exception_class", "phase", "command_started"),
    )


class VerificationOutcome(StrictOutcome):
    OUTCOME_NAME = "VerificationOutcome"
    ALLOWED_TYPES = frozenset({
        "verified", "verified_with_warnings", "rejected", "incomplete_evidence",
        "diagnostic_evidence_recorded", "verification_error", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        verified=("validation_evidence", "completion_gate", "accepted_warnings"),
        verified_with_warnings=("validation_evidence", "completion_gate", "accepted_warnings"),
        rejected=("validation_evidence", "completion_gate", "contract_violations", "failure_owner"),
        incomplete_evidence=("validation_evidence", "missing_evidence", "eligible_probe_capabilities"),
        diagnostic_evidence_recorded=("diagnostic_record", "validation_evidence", "command_role"),
        verification_error=("validation_evidence", "error_code", "exception_class"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class RecoveryOutcome(StrictOutcome):
    OUTCOME_NAME = "RecoveryOutcome"
    ALLOWED_TYPES = frozenset({
        "candidate_repair_required", "plan_revision_required",
        "reconsultation_required", "revalidation_required", "user_input_required",
        "blocked", "internal_protocol_error", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        candidate_repair_required=("diagnosis", "failure_owner", "strategy", "repair_instruction", "source_scope", "decision_id", "incident_id", "failure_fingerprint", "budget_receipt"),
        plan_revision_required=("diagnosis", "contract_violations", "source_scope", "decision_id", "budget_receipt"),
        reconsultation_required=("diagnosis", "missing_facts", "source_scope", "decision_id"),
        revalidation_required=("diagnosis", "new_evidence_refs", "prior_validation_id", "source_scope", "decision_id"),
        user_input_required=("diagnosis", "question_id", "question", "owner", "requires_user_input", "source_scope", "decision_id"),
        blocked=("reason_code", "blocked_evidence", "source_scope", "budget_receipt"),
        internal_protocol_error=("error_code", "source_scope", "identity_conflict"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class CommitStepOutcome(StrictOutcome):
    OUTCOME_NAME = "CommitStepOutcome"
    ALLOWED_TYPES = frozenset({
        "next_step_ready", "plan_complete", "commit_conflict",
        "contract_rejected", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        next_step_ready=("commit_id", "receipt", "before_state_generation", "after_state_generation", "completed_step_id", "successor_step_id"),
        plan_complete=("commit_id", "receipt", "before_state_generation", "after_state_generation", "completed_step_id"),
        commit_conflict=("expected_state_generation", "current_state_generation", "no_partial_mutation"),
        contract_rejected=("reason_code", "failed_invariant", "no_partial_mutation"),
        harness_error=("error_code", "exception_class", "atomicity_evidence"),
    )


class FinalAuditOutcome(StrictOutcome):
    OUTCOME_NAME = "FinalAuditOutcome"
    ALLOWED_TYPES = frozenset({
        "task_verified", "task_incomplete", "entry_rejected", "harness_error"
    })
    REQUIRED_PAYLOAD = _requirements(
        task_verified=("validation_evidence", "completion_gate", "terminal_receipt"),
        task_incomplete=("validation_evidence", "completion_gate", "missing_or_stale_refs", "failure_owner"),
        entry_rejected=("reason_code", "observed_scope"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class TriageOutcome(StrictOutcome):
    OUTCOME_NAME = "TriageOutcome"
    ALLOWED_TYPES = frozenset({
        "direct_response_ready", "consultation_ready", "step_execution_ready",
        "resume_classified", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        direct_response_ready=("task_mode", "request_kind"),
        consultation_ready=("task_mode", "request_kind"),
        step_execution_ready=("task_mode", "request_kind"),
        resume_classified=("semantic_state", "reason_code"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class ConsultantOutcome(StrictOutcome):
    OUTCOME_NAME = "ConsultantOutcome"
    ALLOWED_TYPES = frozenset({
        "planning_ready", "consultation_continues", "user_input_needed",
        "stale_result", "terminal", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        planning_ready=("task_revision", "contract_status"),
        consultation_continues=("phase", "completion_decision"),
        user_input_needed=("question_id", "question", "owner"),
        stale_result=("changed_preconditions",),
        terminal=("status", "reason_code"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class PlannerOutcome(StrictOutcome):
    OUTCOME_NAME = "PlannerOutcome"
    ALLOWED_TYPES = frozenset({
        "plan_review_ready", "consultation_required", "planning_continues",
        "stale_result", "terminal", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        plan_review_ready=("plan_id", "plan_version", "plan_revision"),
        consultation_required=("reason_code",),
        planning_continues=("reason_code", "attempt_ordinal"),
        stale_result=("changed_preconditions",),
        terminal=("status", "reason_code"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class PlanReviewOutcome(StrictOutcome):
    OUTCOME_NAME = "PlanReviewOutcome"
    ALLOWED_TYPES = frozenset({
        "approved_for_execution", "revision_required",
        "reconsultation_required", "user_input_needed", "approval_pending", "terminal",
        "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        approved_for_execution=("plan_id", "plan_version", "approval_status", "execution_backend"),
        revision_required=("validation_issues", "reason_code"),
        reconsultation_required=("reason_code",),
        user_input_needed=("question_id", "question", "owner"),
        approval_pending=("approval_scope", "reason_code"),
        terminal=("status", "reason_code"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class AskUserOutcome(StrictOutcome):
    OUTCOME_NAME = "AskUserOutcome"
    ALLOWED_TYPES = frozenset({
        "answer_recorded", "question_missing", "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        answer_recorded=("question_id", "semantic_state", "answer_recorded"),
        question_missing=("semantic_state", "reason_code"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class InstallSubagentOutcome(StrictOutcome):
    OUTCOME_NAME = "InstallSubagentOutcome"
    ALLOWED_TYPES = frozenset({
        "capability_available", "installation_failed", "terminal",
        "harness_error",
    })
    REQUIRED_PAYLOAD = _requirements(
        capability_available=("status", "evidence"),
        installation_failed=("status", "evidence"),
        terminal=("status", "reason_code"),
        harness_error=("error_code", "exception_class", "detail"),
    )


class ResumeState(StrictOutcome):
    """Semantic resume classification; it can never encode a graph node."""

    OUTCOME_NAME = "ResumeState"
    ALLOWED_TYPES = frozenset({
        "awaiting_user_input", "consultation_ready", "planning_ready",
        "plan_review_pending", "step_execution_ready", "verification_pending",
        "action_execution_ready",
        "recovery_pending", "installation_pending", "final_audit_pending",
        "terminal",
    })
    REQUIRED_PAYLOAD = _requirements(
        awaiting_user_input=("reason_code",),
        consultation_ready=("reason_code",),
        planning_ready=("reason_code",),
        plan_review_pending=("reason_code",),
        step_execution_ready=("reason_code",),
        action_execution_ready=("reason_code",),
        verification_pending=("reason_code",),
        recovery_pending=("reason_code",),
        installation_pending=("reason_code",),
        final_audit_pending=("reason_code",),
        terminal=("reason_code", "status"),
    )


def workflow_routing_schema_version(*states: Mapping[str, Any] | None) -> int:
    """Resolve a sticky routing schema without mutating or migrating state.

    The first explicit value wins.  Missing historical checkpoints therefore
    remain schema 1.  Callers must pass the durable state before graph mirrors.
    """

    for raw in states:
        if not isinstance(raw, Mapping):
            continue
        candidates = [raw]
        task_state = raw.get("task_state")
        if isinstance(task_state, Mapping):
            candidates.insert(0, task_state)
        for candidate in candidates:
            value = candidate.get("workflow_routing_schema_version")
            if value in (None, ""):
                continue
            try:
                version = int(value)
            except (TypeError, ValueError) as exc:
                raise OutcomeContractError(
                    "WORKFLOW_ROUTING_SCHEMA_INVALID", repr(value)
                ) from exc
            if version not in {
                WORKFLOW_ROUTING_SCHEMA_LEGACY, WORKFLOW_ROUTING_SCHEMA_TYPED
            }:
                raise OutcomeContractError(
                    "WORKFLOW_ROUTING_SCHEMA_UNSUPPORTED", str(version)
                )
            return version
    return WORKFLOW_ROUTING_SCHEMA_LEGACY


def bind_workflow_routing_schema_for_task(
    state: Mapping[str, Any], *, brand_new: bool
) -> dict[str, Any]:
    """Return a sticky durable schema binding for task creation/continuation."""
    bound = dict(state or {})
    task_state = dict(bound.get("task_state") or {})
    explicit = (
        task_state.get("workflow_routing_schema_version")
        if task_state.get("workflow_routing_schema_version") not in (None, "")
        else bound.get("workflow_routing_schema_version")
    )
    version = (
        WORKFLOW_ROUTING_SCHEMA_TYPED
        if brand_new and explicit in (None, "")
        else workflow_routing_schema_version(bound)
    )
    bound["workflow_routing_schema_version"] = version
    task_state["workflow_routing_schema_version"] = version
    bound["task_state"] = task_state
    return bound


__all__ = [
    "OUTCOME_SCHEMA_VERSION",
    "WORKFLOW_ROUTING_SCHEMA_LEGACY",
    "WORKFLOW_ROUTING_SCHEMA_TYPED",
    "OutcomeContractError",
    "StrictOutcome",
    "GenerateOutcome",
    "ExecuteOutcome",
    "VerificationOutcome",
    "RecoveryOutcome",
    "CommitStepOutcome",
    "FinalAuditOutcome",
    "TriageOutcome",
    "ConsultantOutcome",
    "PlannerOutcome",
    "PlanReviewOutcome",
    "AskUserOutcome",
    "InstallSubagentOutcome",
    "ResumeState",
    "workflow_routing_schema_version",
    "bind_workflow_routing_schema_for_task",
    "route_blind_state_projection",
]

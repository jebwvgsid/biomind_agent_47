"""Route-free Recovery decision boundary for routing schema 2."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Callable, Mapping

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.workflow.outcomes import (
    RecoveryOutcome,
    VerificationOutcome,
    WORKFLOW_ROUTING_SCHEMA_TYPED,
    workflow_routing_schema_version,
)
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.stage_contracts import StageContractError


Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:24]


class TypedRecovery:
    """Consume one semantic failure and persist one route-blind receipt.

    The durable receipt is the exactly-once boundary.  No legacy
    ``RecoveryDecision`` or Controller route projection is read or written.
    """

    def __init__(
        self,
        *,
        data_path: str,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
        repair_limit: int = 2,
    ) -> None:
        self.data_path = str(data_path)
        self.loader = loader
        self.saver = saver
        self.repair_limit = max(1, int(repair_limit))

    @staticmethod
    def _result(outcome: RecoveryOutcome) -> dict[str, Any]:
        return {"recovery_outcome": outcome.to_dict()}

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("TYPED_RECOVERY_TASK_ID_REQUIRED")
        durable = self.loader(self.data_path, task_id)
        scope = {
            "task_id": task_id,
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "step_id": str(durable.get("current_step_id") or ""),
            "state_generation": int(durable.get("state_generation") or 0),
        }
        if workflow_routing_schema_version(durable, state) != WORKFLOW_ROUTING_SCHEMA_TYPED:
            return self._result(RecoveryOutcome(
                schema_version=1,
                type="internal_protocol_error",
                summary="Typed Recovery rejected a legacy routing checkpoint.",
                scope=scope,
                payload={
                    "error_code": "WORKFLOW_ROUTING_SCHEMA_NOT_TYPED",
                    "source_scope": scope,
                    "identity_conflict": {"observed_schema": workflow_routing_schema_version(durable, state)},
                },
            ))

        try:
            projection = project_recovery_seed(state, durable)
        except StageContractError as exc:
            return self._result(RecoveryOutcome(
                schema_version=1,
                type="internal_protocol_error",
                summary="Recovery rejected an invalid causal failure packet.",
                scope=scope,
                payload={
                    "error_code": exc.code,
                    "source_scope": scope,
                    "identity_conflict": {"detail": exc.detail},
                },
            ))
        source_kind = projection.source_kind if projection is not None else ""
        source = projection.source if projection is not None else {}
        failure_packet = (
            projection.recovery_seed.failure_packet
            if projection is not None
            else None
        )

        source_payload = dict(source.get("payload") or {}) if source_kind.endswith("outcome") else dict(source)
        source_scope = dict(source.get("scope") or {}) if source_kind.endswith("outcome") else {}
        for key in ("action_id", "attempt_id"):
            value = source_scope.get(key) or source_payload.get(key)
            if value:
                scope[key] = str(value)
        source_fingerprint = (
            projection.source_fingerprint
            if projection is not None
            else _hash({"kind": source_kind, "source": source})
        )
        explicit_decision_id = str(source_payload.get("decision_id") or "").strip()
        decision_id = explicit_decision_id or f"typed-recovery-{source_fingerprint}"
        incident_id = str(source_payload.get("incident_id") or f"incident-{source_fingerprint}")
        failure_fingerprint = str(
            source_payload.get("failure_fingerprint")
            or source_payload.get("candidate_fingerprint")
            or (failure_packet.failure_fingerprint if failure_packet else "")
            or source_fingerprint
        )
        task_state = dict(durable.get("task_state") or {})
        receipts = dict(task_state.get("typed_recovery_receipts") or {})
        prior = receipts.get(decision_id)
        if isinstance(prior, Mapping):
            if str(prior.get("source_fingerprint") or "") != source_fingerprint:
                return self._result(RecoveryOutcome(
                    schema_version=1,
                    type="internal_protocol_error",
                    summary="Recovery decision identity collided with different evidence.",
                    scope=scope,
                    payload={
                        "error_code": "RECOVERY_DECISION_IDENTITY_CONFLICT",
                        "source_scope": scope,
                        "identity_conflict": {
                            "decision_id": decision_id,
                            "stored_source_fingerprint": str(prior.get("source_fingerprint") or ""),
                            "observed_source_fingerprint": source_fingerprint,
                        },
                    },
                ))
            return {"recovery_outcome": RecoveryOutcome.from_mapping(prior.get("outcome") or {}).to_dict()}

        disposition = str(source_payload.get("disposition") or "").strip().lower()
        diagnosis = str(
            source_payload.get("diagnosis")
            or source_payload.get("reason_code")
            or source_payload.get("violation_code")
            or source.get("summary")
            or "Runtime evidence requires a recovery decision."
        )
        evidence_refs = tuple(
            str(item)
            for item in source.get("evidence_refs", ()) or ()
            if str(item)
        )
        outcome_type = "internal_protocol_error"
        payload: dict[str, Any]

        if disposition in {"complete_step", "complete_task"}:
            payload = {
                "error_code": "RECOVERY_COMPLETION_AUTHORITY_FORBIDDEN",
                "source_scope": scope,
                "identity_conflict": {"disposition": disposition},
            }
        elif not source_kind:
            payload = {
                "error_code": "RECOVERY_CAUSAL_PACKET_MISSING",
                "source_scope": scope,
                "identity_conflict": {},
            }
        else:
            if not disposition:
                if source_kind == "execute_outcome" and source.get("type") == "execution_failed":
                    disposition = "candidate_repair"
                elif source_kind == "execute_outcome" and source.get("type") == "diagnostic_completed":
                    disposition = "revalidation"
                elif source_kind == "execute_outcome" and source.get("type") == "pre_execution_rejected":
                    disposition = "candidate_repair"
                elif source_kind in {"verification_outcome", "completion_gate_outcome"}:
                    if source.get("type") == "verification_error":
                        disposition = "internal_protocol"
                    elif source.get("type") == "diagnostic_evidence_recorded":
                        disposition = "revalidation"
                    elif str(source_payload.get("failure_owner") or "").upper() in {"PLAN", "PLANNER"}:
                        disposition = "plan_revision"
                    else:
                        disposition = "candidate_repair"
                elif source_kind == "commit_step_outcome" and source.get("type") == "commit_conflict":
                    disposition = "plan_revision"
                elif source_kind == "commit_step_outcome":
                    disposition = "internal_protocol"
                elif source_kind == "install_subagent_outcome":
                    disposition = "candidate_repair"

            ledger = dict(task_state.get("typed_recovery_budget") or {})
            consumed_before = int(ledger.get(incident_id) or 0)
            budget_receipt = {
                "incident_id": incident_id,
                "ordinal": consumed_before + 1,
                "limit": self.repair_limit,
                "remaining": max(0, self.repair_limit - consumed_before - 1),
                "consumed": disposition in {"candidate_repair", "plan_revision"},
                "source_fingerprint": source_fingerprint,
            }
            if disposition in {"candidate_repair", "plan_revision"} and consumed_before >= self.repair_limit:
                outcome_type = "blocked"
                payload = {
                    "reason_code": "RECOVERY_BUDGET_EXHAUSTED",
                    "blocked_evidence": {
                        "decision_id": decision_id,
                        "incident_id": incident_id,
                        "failure_fingerprint": failure_fingerprint,
                    },
                    "source_scope": scope,
                    "budget_receipt": {**budget_receipt, "consumed": False, "ordinal": consumed_before},
                }
            elif disposition == "candidate_repair":
                outcome_type = "candidate_repair_required"
                payload = {
                    "diagnosis": diagnosis,
                    "failure_owner": str(
                        source_payload.get("failure_owner")
                        or (failure_packet.failure_owner if failure_packet else "")
                        or "GENERATED_ACTION"
                    ),
                    "strategy": str(source_payload.get("strategy") or "repair_candidate_from_persisted_failure"),
                    "repair_instruction": str(source_payload.get("repair_instruction") or "Generate a changed candidate that addresses the recorded failure."),
                    "source_scope": scope,
                    "decision_id": decision_id,
                    "incident_id": incident_id,
                    "failure_fingerprint": failure_fingerprint,
                    "budget_receipt": budget_receipt,
                }
            elif disposition == "plan_revision":
                outcome_type = "plan_revision_required"
                payload = {
                    "diagnosis": diagnosis,
                    "contract_violations": list(source_payload.get("contract_violations") or [diagnosis]),
                    "source_scope": scope,
                    "decision_id": decision_id,
                    "budget_receipt": budget_receipt,
                }
            elif disposition == "reconsultation":
                outcome_type = "reconsultation_required"
                payload = {
                    "diagnosis": diagnosis,
                    "missing_facts": list(source_payload.get("missing_facts") or ()),
                    "source_scope": scope,
                    "decision_id": decision_id,
                }
            elif disposition == "revalidation":
                new_refs = list(source_payload.get("new_evidence_refs") or evidence_refs)
                diagnostic = dict(source_payload.get("diagnostic_record") or {})
                if not new_refs:
                    new_refs = [
                        str(value)
                        for value in (
                            diagnostic.get("execution_id"),
                            diagnostic.get("attempt_id"),
                            source_payload.get("envelope_identity"),
                        )
                        if value is not None and str(value).strip()
                    ]
                prior_validation = str(source_payload.get("prior_validation_id") or "")
                if not prior_validation and isinstance(state.get("verification_outcome"), Mapping):
                    try:
                        prior_validation = str(
                            VerificationOutcome.from_mapping(
                                state["verification_outcome"]
                            ).scope.get("validation_id")
                            or ""
                        )
                    except Exception:
                        prior_validation = ""
                if not new_refs:
                    outcome_type = "internal_protocol_error"
                    payload = {
                        "error_code": "REVALIDATION_REQUIRES_NEW_EVIDENCE",
                        "source_scope": scope,
                        "identity_conflict": {"prior_validation_id": prior_validation},
                    }
                else:
                    outcome_type = "revalidation_required"
                    payload = {
                        "diagnosis": diagnosis,
                        "new_evidence_refs": new_refs,
                        "prior_validation_id": prior_validation,
                        "source_scope": scope,
                        "decision_id": decision_id,
                    }
            elif disposition == "user_input":
                outcome_type = "user_input_required"
                payload = {
                    "diagnosis": diagnosis,
                    "question_id": str(source_payload.get("question_id") or f"question-{source_fingerprint}"),
                    "question": str(source_payload.get("question") or "Additional input is required."),
                    "owner": "INPUT_DATA",
                    "requires_user_input": True,
                    "source_scope": scope,
                    "decision_id": decision_id,
                }
            elif disposition == "blocked":
                outcome_type = "blocked"
                payload = {
                    "reason_code": str(source_payload.get("reason_code") or "RECOVERY_BLOCKED"),
                    "blocked_evidence": dict(source_payload.get("blocked_evidence") or {"diagnosis": diagnosis}),
                    "source_scope": scope,
                    "budget_receipt": budget_receipt,
                }
            else:
                payload = {
                    "error_code": str(source_payload.get("error_code") or "RECOVERY_DISPOSITION_INVALID"),
                    "source_scope": scope,
                    "identity_conflict": {"disposition": disposition},
                }

            if outcome_type in {"candidate_repair_required", "plan_revision_required"}:
                ledger[incident_id] = consumed_before + 1
                task_state["typed_recovery_budget"] = ledger

        outcome = RecoveryOutcome(
            schema_version=1,
            type=outcome_type,
            summary={
                "candidate_repair_required": "A bounded changed candidate is required.",
                "plan_revision_required": "The approved Plan contract requires revision.",
                "reconsultation_required": "Authoritative task facts require reconsultation.",
                "revalidation_required": "New diagnostic evidence requires deterministic revalidation.",
                "user_input_required": "Recovery requires an input-owned user fact.",
                "blocked": "Recovery reached a durable terminal blocker.",
                "internal_protocol_error": "Recovery failed closed on an invalid internal protocol.",
            }[outcome_type],
            scope=scope,
            payload=payload,
            evidence_refs=evidence_refs,
        )
        candidate = deepcopy(durable)
        candidate_task = dict(candidate.get("task_state") or {})
        if "typed_recovery_budget" in task_state:
            candidate_task["typed_recovery_budget"] = deepcopy(task_state["typed_recovery_budget"])
        candidate_receipts = dict(candidate_task.get("typed_recovery_receipts") or {})
        candidate_receipts[decision_id] = {
            "schema_version": 1,
            "source_fingerprint": source_fingerprint,
            "outcome": outcome.to_dict(),
        }
        candidate_task["typed_recovery_receipts"] = candidate_receipts
        packet_ref = str(
            state.get("active_failure_packet_ref")
            or candidate_task.get("active_failure_packet_ref")
            or ""
        )
        if packet_ref:
            consumed_packets = dict(
                candidate_task.get("consumed_failure_packet_refs") or {}
            )
            consumed_packets[packet_ref] = {
                "decision_id": decision_id,
                "source_fingerprint": source_fingerprint,
            }
            candidate_task["consumed_failure_packet_refs"] = consumed_packets
            if str(candidate_task.get("active_failure_packet_ref") or "") == packet_ref:
                candidate_task["active_failure_packet_ref"] = ""
        candidate["task_state"] = candidate_task
        try:
            self.saver(self.data_path, task_id, candidate)
        except StateCASConflict:
            refreshed = self.loader(self.data_path, task_id)
            refreshed_receipt = dict((refreshed.get("task_state") or {}).get("typed_recovery_receipts") or {}).get(decision_id)
            if isinstance(refreshed_receipt, Mapping):
                return {"recovery_outcome": RecoveryOutcome.from_mapping(refreshed_receipt.get("outcome") or {}).to_dict()}
            return self._result(RecoveryOutcome(
                schema_version=1,
                type="internal_protocol_error",
                summary="Recovery CAS changed before its route-blind receipt committed.",
                scope=scope,
                payload={
                    "error_code": "RECOVERY_RECEIPT_CAS_CONFLICT",
                    "source_scope": scope,
                    "identity_conflict": {
                        "expected_state_generation": int(durable.get("state_generation") or 0),
                        "current_state_generation": int(refreshed.get("state_generation") or 0),
                    },
                },
            ))
        return self._result(outcome)


def recovery_harness_error(exc: Exception, state: Mapping[str, Any]) -> RecoveryOutcome:
    return RecoveryOutcome(
        schema_version=1,
        type="harness_error",
        summary="Typed Recovery boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "TYPED_RECOVERY_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "detail": str(exc)[:800],
        },
    )


__all__ = ["TypedRecovery", "recovery_harness_error"]

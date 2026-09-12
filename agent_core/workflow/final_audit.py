"""Deterministic terminal transaction for routing schema 2."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

from agent_core.completion_gate import CompletionGate
from agent_core.committed_observations import no_file_final_response
from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import (
    STEP_SUCCESS_STATUSES,
    TASK_STATUS_COMPLETED,
    load_plan_state,
    save_plan_state,
    set_task_status,
)
from agent_core.recovery import ValidationEvidence, ValidationStatus
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    FinalAuditOutcome,
    VerificationOutcome,
    WORKFLOW_ROUTING_SCHEMA_TYPED,
    workflow_routing_schema_version,
)


Auditor = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]]
GateEvaluator = Callable[[str, str, dict[str, Any], str], Mapping[str, Any]]
Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]


def _default_gate(
    data_path: str, task_id: str, plan_state: dict[str, Any], current_plan: str
) -> Mapping[str, Any]:
    return CompletionGate.validate_final(
        data_path,
        task_id,
        plan_state,
        current_plan,
        require_execution_evidence=True,
        require_supervisor_evidence=False,
    ).to_dict()


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


class TypedFinalAudit:
    """Validate current execution/verification/gate evidence, then CAS complete."""

    def __init__(
        self,
        *,
        data_path: str,
        auditor: Auditor,
        gate_evaluator: GateEvaluator | None = None,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
    ) -> None:
        self.data_path = str(data_path)
        self.auditor = auditor
        self.gate_evaluator = gate_evaluator or _default_gate
        self.loader = loader
        self.saver = saver

    @staticmethod
    def _result(
        outcome_type: str,
        *,
        task_id: str,
        summary: str,
        payload: Mapping[str, Any],
        evidence_refs: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return {
            "final_audit_outcome": FinalAuditOutcome(
                schema_version=1,
                type=outcome_type,
                summary=summary,
                scope={"task_id": task_id or "unknown"},
                payload=payload,
                evidence_refs=evidence_refs,
            ).to_dict()
        }

    def _entry_rejected(
        self, task_id: str, reason_code: str, observed_scope: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._result(
            "entry_rejected",
            task_id=task_id,
            summary="FinalAudit rejected stale or ineligible entry evidence.",
            payload={
                "reason_code": reason_code,
                "observed_scope": dict(observed_scope),
            },
        )

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("TYPED_FINAL_AUDIT_TASK_ID_REQUIRED")
        durable = self.loader(self.data_path, task_id)
        observed_scope = {
            "task_id": task_id,
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "state_generation": int(durable.get("state_generation") or 0),
        }
        if workflow_routing_schema_version(durable, state) != WORKFLOW_ROUTING_SCHEMA_TYPED:
            return self._entry_rejected(task_id, "WORKFLOW_ROUTING_SCHEMA_NOT_TYPED", observed_scope)

        try:
            commit = CommitStepOutcome.from_mapping(state.get("commit_step_outcome") or {})
            verification = VerificationOutcome.from_mapping(state.get("verification_outcome") or {})
            execution_outcome = ExecuteOutcome.from_mapping(state.get("execute_outcome") or {})
        except Exception as exc:
            return self._entry_rejected(task_id, "FINAL_AUDIT_TYPED_EVIDENCE_INVALID", {
                **observed_scope, "exception_class": type(exc).__name__,
            })
        if commit.type != "plan_complete":
            return self._entry_rejected(task_id, "FINAL_AUDIT_PLAN_COMPLETE_RECEIPT_REQUIRED", observed_scope)
        if verification.type not in {"verified", "verified_with_warnings"}:
            return self._entry_rejected(task_id, "FINAL_AUDIT_VERIFICATION_REQUIRED", observed_scope)
        if execution_outcome.type != "execution_succeeded":
            return self._entry_rejected(task_id, "FINAL_AUDIT_SUCCESSFUL_EXECUTION_REQUIRED", observed_scope)

        commit_payload = dict(commit.to_dict().get("payload") or {})
        verification_scope = dict(verification.scope)
        verification_payload = dict(verification.to_dict().get("payload") or {})
        execution_payload = dict(execution_outcome.to_dict().get("payload") or {})
        execution = dict(execution_payload.get("execution_record") or {})
        validation = ValidationEvidence.from_mapping(
            dict(verification_payload.get("validation_evidence") or {})
        )
        expected_scope = {
            "task_id": str(durable.get("task_id") or task_id),
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "step_id": str(commit_payload.get("completed_step_id") or ""),
            "action_id": validation.action_id,
            "attempt_id": validation.attempt_id,
            "validation_id": validation.validation_id,
        }
        for key, expected in expected_scope.items():
            if key in verification_scope and str(verification_scope.get(key) or "") != str(expected or ""):
                return self._entry_rejected(task_id, "FINAL_AUDIT_VERIFICATION_SCOPE_STALE", {
                    **observed_scope, "field": key,
                    "expected": str(expected or ""),
                    "observed": str(verification_scope.get(key) or ""),
                })
        for key in ("step_id", "action_id", "attempt_id"):
            if str(execution.get(key) or "") != str(expected_scope[key] or ""):
                return self._entry_rejected(task_id, "FINAL_AUDIT_EXECUTION_SCOPE_STALE", {
                    **observed_scope, "field": key,
                    "expected": str(expected_scope[key] or ""),
                    "observed": str(execution.get(key) or ""),
                })
        if validation.status not in {
            ValidationStatus.VALID.value,
            ValidationStatus.VALID_WITH_WARNINGS.value,
        }:
            return self._entry_rejected(task_id, "FINAL_AUDIT_VERIFICATION_STATUS_INVALID", observed_scope)

        commit_id = str(commit_payload.get("commit_id") or "")
        step_receipts = dict((durable.get("task_state") or {}).get("typed_step_commit_receipts") or {})
        stored_commit = step_receipts.get(commit_id)
        if not isinstance(stored_commit, Mapping):
            return self._entry_rejected(task_id, "FINAL_AUDIT_COMMIT_RECEIPT_NOT_DURABLE", {
                **observed_scope, "commit_id": commit_id,
            })
        try:
            stored_commit_outcome = CommitStepOutcome.from_mapping(stored_commit.get("outcome") or {})
        except Exception as exc:
            return self._entry_rejected(task_id, "FINAL_AUDIT_COMMIT_RECEIPT_CORRUPT", {
                **observed_scope, "exception_class": type(exc).__name__,
            })
        if stored_commit_outcome.to_dict() != commit.to_dict():
            return self._entry_rejected(task_id, "FINAL_AUDIT_COMMIT_RECEIPT_STALE", observed_scope)
        unfinished = [
            str(item.get("step_id") or "")
            for item in durable.get("steps", ())
            if isinstance(item, Mapping)
            and str(item.get("status") or "") not in STEP_SUCCESS_STATUSES
        ]
        if unfinished:
            return self._entry_rejected(task_id, "FINAL_AUDIT_UNFINISHED_STEPS", {
                **observed_scope, "unfinished_step_ids": unfinished,
            })

        receipt_identity = {
            "task_id": task_id,
            "plan_id": expected_scope["plan_id"],
            "plan_version": expected_scope["plan_version"],
            "commit_id": commit_id,
            "validation_id": validation.validation_id,
            "attempt_id": validation.attempt_id,
        }
        audit_id = "final-audit-" + _hash(receipt_identity)
        final_receipts = dict((durable.get("task_state") or {}).get("typed_final_audit_receipts") or {})
        prior = final_receipts.get(audit_id)
        if isinstance(prior, Mapping):
            return {
                "final_audit_outcome": FinalAuditOutcome.from_mapping(
                    prior.get("outcome") or {}
                ).to_dict()
            }

        audit_state = {
            **dict(state),
            "execution_evidence": execution,
            "validation_evidence": validation.to_dict(),
        }
        raw_audit = await self.auditor(audit_state)
        if not isinstance(raw_audit, Mapping):
            raise TypeError("Final auditor must return a mapping")
        final_validation = ValidationEvidence.from_mapping(
            dict(raw_audit.get("validation_evidence") or {})
        )
        for key, expected in (
            ("task_id", task_id),
            ("step_id", validation.step_id),
            ("action_id", validation.action_id),
            ("attempt_id", validation.attempt_id),
        ):
            if str(getattr(final_validation, key) or "") != str(expected or ""):
                return self._entry_rejected(task_id, "FINAL_AUDIT_VALIDATION_SCOPE_STALE", {
                    **observed_scope, "field": key,
                    "expected": str(expected or ""),
                    "observed": str(getattr(final_validation, key) or ""),
                })
        gate = dict(self.gate_evaluator(
            self.data_path,
            task_id,
            durable,
            str(state.get("current_plan") or ""),
        ))
        valid = final_validation.status in {
            ValidationStatus.VALID.value,
            ValidationStatus.VALID_WITH_WARNINGS.value,
        }
        if not valid or gate.get("passed") is not True:
            missing_refs = list(gate.get("missing_artifacts") or gate.get("stale_artifacts") or ())
            if not valid:
                missing_refs.extend(final_validation.errors or [final_validation.validation_id])
            return self._result(
                "task_incomplete",
                task_id=task_id,
                summary="Final validation or CompletionGate did not establish current completion.",
                payload={
                    "validation_evidence": final_validation.to_dict(),
                    "completion_gate": gate,
                    "missing_or_stale_refs": list(dict.fromkeys(str(item) for item in missing_refs if str(item))),
                    "failure_owner": "verification" if not valid else "completion_contract",
                },
                evidence_refs=(validation.validation_id, final_validation.validation_id),
            )

        try:
            final_response = no_file_final_response(durable)
        except (TypeError, ValueError) as exc:
            return self._result(
                "task_incomplete", task_id=task_id,
                summary="Accepted no-file results could not be delivered as a factual answer.",
                payload={"validation_evidence": final_validation.to_dict(), "completion_gate": gate,
                         "missing_or_stale_refs": [str(exc)], "failure_owner": "result_delivery"},
                evidence_refs=(validation.validation_id, final_validation.validation_id, commit_id),
            )
        terminal_receipt = "typed-task-completion:" + audit_id
        semantic_outcome = FinalAuditOutcome(
            schema_version=1,
            type="task_verified",
            summary="Current execution, verification, and final CompletionGate evidence were atomically accepted.",
            scope={"task_id": task_id},
            payload={
                "validation_evidence": final_validation.to_dict(),
                "completion_gate": gate,
                "terminal_receipt": terminal_receipt,
                **({"final_response": final_response} if final_response is not None else {}),
            },
            evidence_refs=(validation.validation_id, final_validation.validation_id, commit_id),
        )
        candidate = deepcopy(durable)
        set_task_status(
            candidate,
            TASK_STATUS_COMPLETED,
            "Typed FinalAudit accepted current execution, verification, and CompletionGate evidence.",
        )
        candidate["execution_status"] = "completed"
        candidate_task = dict(candidate.get("task_state") or {})
        final_receipts = dict(candidate_task.get("typed_final_audit_receipts") or {})
        final_receipts[audit_id] = {
            "schema_version": 1,
            "identity": receipt_identity,
            "outcome": semantic_outcome.to_dict(),
        }
        candidate_task["typed_final_audit_receipts"] = final_receipts
        candidate["task_state"] = candidate_task
        try:
            self.saver(self.data_path, task_id, candidate)
        except StateCASConflict:
            refreshed = self.loader(self.data_path, task_id)
            refreshed_receipt = dict((refreshed.get("task_state") or {}).get("typed_final_audit_receipts") or {}).get(audit_id)
            if isinstance(refreshed_receipt, Mapping):
                return {
                    "final_audit_outcome": FinalAuditOutcome.from_mapping(
                        refreshed_receipt.get("outcome") or {}
                    ).to_dict()
                }
            return self._result(
                "task_incomplete",
                task_id=task_id,
                summary="FinalAudit CAS changed before terminal completion committed.",
                payload={
                    "validation_evidence": final_validation.to_dict(),
                    "completion_gate": gate,
                    "missing_or_stale_refs": ["plan_state_generation"],
                    "failure_owner": "concurrency",
                },
                evidence_refs=(validation.validation_id, final_validation.validation_id),
            )
        return {"final_audit_outcome": semantic_outcome.to_dict()}


def final_audit_harness_error(exc: Exception, state: Mapping[str, Any]) -> FinalAuditOutcome:
    return FinalAuditOutcome(
        schema_version=1,
        type="harness_error",
        summary="Typed FinalAudit boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "TYPED_FINAL_AUDIT_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "detail": str(exc)[:800],
        },
    )


__all__ = ["TypedFinalAudit", "final_audit_harness_error"]

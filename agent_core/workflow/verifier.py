"""Schema-2 Supervisor adapter: validation facts in, route-free outcome out."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

from agent_core.completion_gate import CompletionGate
from agent_core.execution_authority import is_approved_step_execution, is_recovery_diagnostic_execution
from agent_core.plan_tracker import load_plan_state, record_provenance_event, save_plan_state
from agent_core.recovery import ValidationEvidence, ValidationStatus
from agent_core.semantic_completion import (
    COMPLETION_ACCEPTED,
    build_completion_report,
)
from agent_core.workflow.outcomes import ExecuteOutcome, VerificationOutcome


Verifier = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]]
GateEvaluator = Callable[[str, str, dict[str, Any]], Mapping[str, Any]]


def _default_gate(data_path: str, task_id: str, state: dict[str, Any]) -> Mapping[str, Any]:
    return CompletionGate.validate_current_step(data_path, task_id, state).to_dict()


class TypedSupervisor:
    """Narrow adapter around the existing deterministic ``SupervisorNode``.

    Legacy compatibility fields returned by ``SupervisorNode.run`` are never
    copied.  Only its immutable ValidationEvidence is interpreted here.
    """

    def __init__(
        self,
        *,
        data_path: str,
        verifier: Verifier,
        gate_evaluator: GateEvaluator | None = None,
    ) -> None:
        self.data_path = str(data_path)
        self.verifier = verifier
        self.gate_evaluator = gate_evaluator or _default_gate

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("TYPED_SUPERVISOR_TASK_ID_REQUIRED")
        verifier_state = dict(state)
        execution = (
            dict(state.get("execution_evidence") or {})
            if isinstance(state.get("execution_evidence"), Mapping)
            else {}
        )
        if not execution and isinstance(state.get("execute_outcome"), Mapping):
            execute_outcome = ExecuteOutcome.from_mapping(state["execute_outcome"])
            execute_payload = dict(execute_outcome.to_dict().get("payload") or {})
            execution = dict(
                execute_payload.get("execution_record")
                or execute_payload.get("diagnostic_record")
                or {}
            )
            verifier_state["execution_evidence"] = execution
        if execution.get("recovery_execution_facts_ref"):
            # Execute archives the original allowed fields before its compact
            # graph projection. Semantic review consumes that same durable
            # evidence, not an abbreviated stdout_summary labelled complete.
            try:
                from agent_core.state_contract_store import StateContractStore
                from agent_core.workflow.inspection_evidence import read_execution_facts
                held = read_execution_facts(StateContractStore(self.data_path, task_id),
                    str(execution["recovery_execution_facts_ref"]))
                current = load_plan_state(self.data_path, task_id)
                for key in ("task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id"):
                    expected = execution.get(key, task_id if key == "task_id" else "")
                    if str(held.get(key) or "") != str(expected or ""):
                        raise ValueError("SUPERVISOR_EXECUTION_FACTS_SCOPE_MISMATCH:" + key)
                for key, expected in (("task_id", task_id), ("plan_id", current.get("plan_id")),
                        ("plan_version", current.get("plan_version")), ("step_id", current.get("current_step_id"))):
                    if str(held.get(key) or "") != str(expected or ""):
                        raise ValueError("SUPERVISOR_EXECUTION_FACTS_SCOPE_MISMATCH:" + key)
                execution = {**execution, **{key: held[key] for key in (
                    "code", "raw_stdout", "raw_stderr", "stdout_truncated", "stderr_truncated",
                ) if key in held}}
                for stream in ("stdout", "stderr"):
                    if "raw_" + stream not in held and stream in held:
                        execution["raw_" + stream] = str(held[stream])
                verifier_state["execution_evidence"] = execution
            except (ValueError, OSError, TypeError) as exc:
                return {"verification_outcome": VerificationOutcome(schema_version=1,
                    type="harness_error", summary="The recorded execution facts could not be safely read for verification.",
                    scope={"task_id": task_id, **{key: str(execution.get(key) or "") for key in ("plan_id", "plan_version", "step_id", "action_id", "attempt_id")}},
                    payload={"error_code": str(getattr(exc, "code", "") or "SUPERVISOR_EXECUTION_FACTS_READ_FAILED"),
                        "exception_class": type(exc).__name__, "detail": str(exc),
                        "failure_stage": "supervisor.execution_evidence_read", "execution_record": execution}).to_dict()}
        # A Recovery choice changes the registered realization only after a
        # fresh mechanical check. The original execution/approval/stdout stay
        # in its admitted receipt; the normal verifier owns validity.
        recovery = state.get("recovery_outcome") or {}
        decision_id = str(execution.get("recovery_decision_id") or "")
        if recovery.get("type") == "revalidation_required":
            decision_id = str((recovery.get("payload") or {}).get("decision_id") or decision_id)
        if decision_id:
            from agent_core.workflow.result_resolution import (
                get_result_resolution_request, result_resolution_reconciliation, register_result_resolution,
            )
            try:
                result_state = load_plan_state(self.data_path, task_id)
                request = get_result_resolution_request(result_state, decision_id)
                if request:
                    receipt = result_resolution_reconciliation(request,
                        execution if request["operation"] == "copy_to_exact" else None)
                    if request["operation"] == "copy_to_exact":
                        # The organization attempt keeps its own deterministic
                        # validation. It cannot silently remain the latest
                        # unverified attempt when the source step is committed.
                        local_result = await self.verifier({**verifier_state, "validation_evidence": {}})
                        local_validation = ValidationEvidence.from_mapping(dict(local_result.get("validation_evidence") or {}))
                        if (local_validation.attempt_id != receipt["local_attempt_id"]
                                or local_validation.status not in {ValidationStatus.VALID.value, ValidationStatus.VALID_WITH_WARNINGS.value}):
                            raise ValueError("RESULT_LOCAL_ORGANIZATION_VALIDATION_FAILED:" + str(local_validation.errors))
                        result_state = load_plan_state(self.data_path, task_id)
                        record_provenance_event(result_state, "validation_recorded", {
                            "attempt_id": local_validation.attempt_id,
                            "validation": local_validation.to_dict(),
                            "validation_evidence_ref": local_validation.validation_id})
                    execution = register_result_resolution(result_state, request, receipt, data_path=self.data_path)
                    save_plan_state(self.data_path, task_id, result_state)
                    verifier_state.update(execution_evidence=execution, validation_evidence={},
                        reconciliation_evidence={"result_resolution_receipt": receipt,
                            "reason": "Explicit current-result choice; independent deterministic revalidation required."})
            except (ValueError, OSError, TypeError) as exc:
                return {"verification_outcome": VerificationOutcome(schema_version=1,
                    type="incomplete_evidence", summary="The selected result could not be safely registered and reverified.",
                    scope={"task_id": task_id, **{key: str(execution.get(key) or "") for key in ("plan_id", "plan_version", "step_id", "action_id", "attempt_id")}},
                    payload={"missing_evidence": [str(getattr(exc, "code", "") or "RESULT_RESOLUTION_REVALIDATION_FAILED")],
                        "error_code": str(getattr(exc, "code", "") or "RESULT_RESOLUTION_REVALIDATION_FAILED"),
                        "detail": str(exc), "execution_record": execution}).to_dict()}
        result = await self.verifier(verifier_state)
        if not isinstance(result, Mapping):
            raise TypeError("SupervisorNode.run must return a mapping")
        raw_validation = result.get("validation_evidence")
        if not isinstance(raw_validation, dict):
            raise ValueError("TYPED_SUPERVISOR_VALIDATION_EVIDENCE_REQUIRED")
        validation = ValidationEvidence.from_mapping(raw_validation)
        durable = load_plan_state(self.data_path, task_id)
        plan_id = str(durable.get("plan_id") or "")
        plan_version = str(durable.get("plan_version") or "")
        step_id = str(durable.get("current_step_id") or validation.step_id or "")
        action_id = str(validation.action_id or execution.get("action_id") or "")
        attempt_id = str(validation.attempt_id or execution.get("attempt_id") or "")
        scope = {
            "task_id": task_id,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "step_id": step_id,
            "state_generation": int(durable.get("state_generation") or 0),
            "action_id": action_id,
            "attempt_id": attempt_id,
            "validation_id": validation.validation_id,
        }
        if validation.task_id != task_id or validation.step_id != step_id:
            raise ValueError("TYPED_SUPERVISOR_VALIDATION_SCOPE_MISMATCH")

        validation_value = validation.to_dict()
        producer_report = deepcopy(dict(result.get("completion_report") or {}))
        semantic_refs: list[str] = []
        role = str(validation.command_role or execution.get("command_role") or "").lower()
        if is_recovery_diagnostic_execution(durable, execution) or (
            role in {"probe", "diagnostic", "optional_probe", "capability_probe"}
            and not is_approved_step_execution(durable, execution)
        ):
            outcome_type = "diagnostic_evidence_recorded"
            payload = {
                "diagnostic_record": execution,
                "validation_evidence": validation_value,
                "command_role": role,
            }
            summary = "Diagnostic evidence recorded without step completion eligibility."
        elif validation.status == ValidationStatus.INCOMPLETE_EVIDENCE.value:
            outcome_type = "incomplete_evidence"
            payload = {
                "validation_evidence": validation_value,
                "missing_evidence": list(validation.errors or validation.warnings),
                "eligible_probe_capabilities": list(validation.recoverability_hints),
            }
            summary = "Verification evidence is incomplete."
        elif validation.status == ValidationStatus.VALIDATION_ERROR.value:
            outcome_type = "verification_error"
            payload = {
                "validation_evidence": validation_value,
                "error_code": "VALIDATION_ERROR",
                "exception_class": "DeterministicValidatorError",
            }
            summary = "The deterministic validator reported an internal error."
        elif validation.status == ValidationStatus.INVALID.value:
            outcome_type = "rejected"
            payload = {
                "validation_evidence": validation_value,
                "completion_gate": {"passed": False, "classification": "validation_rejected"},
                "contract_violations": list(validation.errors),
                "failure_owner": "verification",
            }
            summary = "Deterministic verification rejected the current execution."
        else:
            # CompletionGate is a pure fact evaluator here.  Its artifact and
            # StepEffect findings must be part of the CompletionReport already
            # consumed by the structured reviewer; it does not route or mutate
            # durable state.
            gate_state = deepcopy(durable)
            attempts = dict(
                ((gate_state.get("provenance_state") or {}).get("attempts"))
                or {}
            )
            if attempt_id and attempt_id in attempts:
                record_provenance_event(gate_state, "validation_recorded", {
                    "attempt_id": attempt_id,
                    "validation": validation_value,
                    "validation_evidence_ref": validation.validation_id,
                })
            gate = dict(self.gate_evaluator(self.data_path, task_id, gate_state))
            report = build_completion_report(
                plan_state=durable,
                validation=validation_value,
                execution_evidence=execution,
                completion_gate=gate,
            )
            report_value = report.to_dict()
            decision_value = report.effective_decision.to_dict()
            semantic_refs = [report.report_id, report.effective_decision.decision_id]
            if report.effective_disposition != COMPLETION_ACCEPTED:
                outcome_type = report.effective_disposition
                failed_predicates = [
                    dict(item)
                    for item in report.predicate_results
                    if item.get("passed") is not True
                ]
                violations = list(dict.fromkeys([
                    *[
                        str(item.get("reason_code") or "SEMANTIC_PREDICATE_FAILED")
                        for item in failed_predicates
                    ],
                    *list(report.effective_decision.concerns),
                ]))
                payload = {
                    "validation_evidence": validation_value,
                    "completion_gate": {
                        "passed": False,
                        "classification": "semantic_completion_rejected",
                        "plan_version": plan_version,
                        "evidence_refs": list(semantic_refs),
                    },
                    "completion_report": report_value,
                    "semantic_decision": decision_value,
                    "contract_violations": violations or [
                        "SEMANTIC_COMPLETION_NOT_ACCEPTED"
                    ],
                    "failure_owner": (
                        "deterministic_predicate"
                        if report.deterministic_veto
                        else "semantic_review"
                    ),
                }
                if outcome_type == "user_information_required":
                    payload.update({
                        "question_id": (
                            "semantic-question-"
                            + report.effective_decision.decision_id.rsplit("-", 1)[-1][:20]
                        ),
                        "question": (
                            violations[0]
                            if violations
                            else "Additional user information is required."
                        ),
                    })
                summary = (
                    "Deterministic semantic completion predicates rejected the "
                    "candidate."
                    if report.deterministic_veto
                    else "The typed semantic reviewer disposition requires follow-up."
                )
            else:
                if not gate.get("passed"):
                    outcome_type = "rejected"
                    payload = {
                        "validation_evidence": validation_value,
                        "completion_gate": gate,
                        "completion_report": report_value,
                        "semantic_decision": decision_value,
                        "contract_violations": [
                            str(gate.get("message") or gate.get("feedback") or "completion gate rejected")
                        ],
                        "failure_owner": "completion_contract",
                    }
                    summary = "CompletionGate rejected the verified execution candidate."
                else:
                    outcome_type = (
                        "verified_with_warnings"
                        if validation.status == ValidationStatus.VALID_WITH_WARNINGS.value
                        else "verified"
                    )
                    payload = {
                        "validation_evidence": validation_value,
                        "completion_gate": gate,
                        "completion_report": report_value,
                        "semantic_decision": decision_value,
                        "accepted_warnings": list(validation.warnings),
                    }
                    summary = "Current execution and CompletionGate are verified."

        if producer_report and "completion_report" not in payload:
            payload["completion_report"] = producer_report
            payload["semantic_decision"] = deepcopy(producer_report.get("effective_decision") or {})

        # The local Reviewer owns its persistent request limit. Re-entering
        # Supervisor through a fresh inspection wrapper grants no new quota.
        advisory=dict(validation_value.get('llm_advisory') or {})
        review_receipt=dict(advisory.get('review_policy_receipt') or {})
        assessment=(durable.get('task_state') or {}).get('semantic_review_receipts',{}).get(
            str(review_receipt.get('assessment_id') or ''),{})
        if outcome_type=='assessment_pending' and review_receipt.get('automatic_review_stopped') and assessment:
            from agent_core.semantic_completion import REVIEWER_POLICY_VERSION
            if (assessment.get('producer')==REVIEWER_POLICY_VERSION
                    and all(str((assessment.get('scope') or {}).get(key) or '')==str(scope[key])
                            for key in ('task_id','plan_id','plan_version','step_id','action_id','attempt_id'))):
                payload.update(automatic_review_stopped=True,review_policy_receipt=review_receipt,
                    error_code=str((advisory.get('review_error') or {}).get('code') or 'SEMANTIC_REVIEW_PROTOCOL_EXHAUSTED'),
                    execution_record=deepcopy(execution))
                summary='Execution evidence retained; automatic semantic review stopped without step commit.'

        if execution.get("result_resolution_receipt"):
            payload["result_resolution_ref"] = str(execution["result_resolution_receipt"]["decision_id"])
            payload["result_resolution_source_attempt_id"] = str(execution["result_resolution_receipt"]["source_attempt_id"])
            payload["result_resolution_local_attempt_id"] = str(execution["result_resolution_receipt"]["local_attempt_id"])

        outcome = VerificationOutcome(
            schema_version=1,
            type=outcome_type,
            summary=summary,
            scope=scope,
            payload=payload,
            evidence_refs=tuple(dict.fromkeys([
                validation.validation_id,
                *semantic_refs,
            ])),
        )
        update: dict[str, Any] = {"verification_outcome": outcome.to_dict()}
        if outcome_type == "user_information_required":
            update["pending_human_question"] = {
                "question_id": str(payload.get("question_id") or "semantic-question"),
                "question": str(
                    payload.get("question")
                    or "Additional user information is required."
                ),
                "owner": "user",
                "type": "semantic_completion",
                "status": "pending",
            }
        return update


def verification_harness_error(exc: Exception, state: Mapping[str, Any]) -> VerificationOutcome:
    return VerificationOutcome(
        schema_version=1,
        type="harness_error",
        summary="Typed Supervisor boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "TYPED_SUPERVISOR_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "detail": str(exc)[:800],
        },
    )


__all__ = ["TypedSupervisor", "verification_harness_error"]

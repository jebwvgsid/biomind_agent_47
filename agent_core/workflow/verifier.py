"""Schema-2 Supervisor adapter: validation facts in, route-free outcome out."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from agent_core.completion_gate import CompletionGate
from agent_core.plan_tracker import load_plan_state
from agent_core.recovery import ValidationEvidence, ValidationStatus
from agent_core.workflow.outcomes import VerificationOutcome


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
        result = await self.verifier(dict(state))
        if not isinstance(result, Mapping):
            raise TypeError("SupervisorNode.run must return a mapping")
        raw_validation = result.get("validation_evidence")
        if not isinstance(raw_validation, dict):
            raise ValueError("TYPED_SUPERVISOR_VALIDATION_EVIDENCE_REQUIRED")
        validation = ValidationEvidence.from_mapping(raw_validation)
        durable = load_plan_state(self.data_path, task_id)
        execution = (
            dict(state.get("execution_evidence") or {})
            if isinstance(state.get("execution_evidence"), Mapping)
            else {}
        )
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
        role = str(validation.command_role or execution.get("command_role") or "").lower()
        if role in {"probe", "diagnostic", "optional_probe", "capability_probe"}:
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
            gate = dict(self.gate_evaluator(self.data_path, task_id, durable))
            if not gate.get("passed"):
                outcome_type = "rejected"
                payload = {
                    "validation_evidence": validation_value,
                    "completion_gate": gate,
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
                    "accepted_warnings": list(validation.warnings),
                }
                summary = "Current execution and CompletionGate are verified."

        outcome = VerificationOutcome(
            schema_version=1,
            type=outcome_type,
            summary=summary,
            scope=scope,
            payload=payload,
            evidence_refs=(validation.validation_id,),
        )
        return {"verification_outcome": outcome.to_dict()}


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

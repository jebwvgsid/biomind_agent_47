"""Atomic schema-2 step acceptance boundary."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Callable, Mapping

from agent_core.completion_gate import CompletionGate
from agent_core.execution_authority import is_approved_step_execution, is_recovery_diagnostic_execution
from agent_core.plan_bindings import PlanBindingError, accept_action_binding_candidates
from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import (
    ATTEMPT_COMPLETED,
    ATTEMPT_EVIDENCE_COMMITTED,
    ATTEMPT_VALIDATED,
    ATTEMPT_VALIDATION_PASSED,
    STEP_ACTIVE_STATUSES,
    TASK_TERMINAL_STATUSES,
    advance_plan_after_verified_step,
    load_plan_state,
    record_provenance_event,
    save_plan_state,
    task_status,
)
from agent_core.recovery import ValidationEvidence, ValidationStatus
from agent_core.semantic_completion import (
    COMPLETION_ACCEPTED,
    CompletionReport,
    SemanticCompletionError,
    SemanticDecision,
    build_completion_report,
)
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    GenerateOutcome,
    VerificationOutcome,
    WORKFLOW_ROUTING_SCHEMA_TYPED,
    workflow_routing_schema_version,
)


GateEvaluator = Callable[[str, str, dict[str, Any]], Mapping[str, Any]]


def _default_gate(data_path: str, task_id: str, state: dict[str, Any]) -> Mapping[str, Any]:
    return CompletionGate.validate_current_step(data_path, task_id, state).to_dict()


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _accepted_effect_refs(
    effect_results: Mapping[str, Any], gate: Mapping[str, Any]
) -> list[str]:
    refs = [str(item) for item in gate.get("evidence_refs", ()) or () if str(item)]
    identity_keys = (
        "evidence_ref", "receipt_id", "artifact_version_id", "binding_id",
        "observation_id", "side_effect_id", "effect_id", "identity", "id",
        "name",
    )
    for kind, raw in dict(effect_results or {}).items():
        if kind not in {
            "produces_artifacts", "produces_bindings", "produces_observations",
            "workspace_mutations", "explicit_side_effects",
        }:
            continue
        if isinstance(raw, Mapping):
            values = [raw] if any(key in raw for key in identity_keys) else [
                ({**dict(item), "name": str(name)} if isinstance(item, Mapping) else {
                    "name": str(name), "value": item,
                })
                for name, item in raw.items()
            ]
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            values = [raw]
        for value in values:
            if isinstance(value, Mapping):
                ref = next(
                    (
                        str(value.get(key))
                        for key in identity_keys
                        if str(value.get(key) or "")
                    ),
                    "",
                )
                if not ref and kind == "workspace_mutations":
                    workspace_ref = str(value.get("workspace_ref") or value.get("workspace_id") or "")
                    mutation = str(value.get("mutation") or value.get("operation") or "")
                    ref = f"{workspace_ref}::{mutation}" if workspace_ref and mutation else ""
            else:
                ref = str(value or "")
            if ref:
                refs.append(ref)
    return list(dict.fromkeys(refs))


def _successor_projection(projection, *, state, durable, completed_step_id):
    """An acknowledged commit never erases a newer step's live handoff."""
    if any(str(durable.get(key) or "") != str(projection.get(key) or "")
           for key in ("current_step_id", "plan_id", "plan_version")):
        return {}
    result = {key: projection[key] for key in ("current_step_id", "plan_id", "plan_version") if key in projection}
    for key in ("recovery_outcome", "generate_seed"):
        if key not in projection:
            continue
        value = state.get(key) or {}
        scope = value.get("scope") or value if isinstance(value, Mapping) else {}
        if not value or str(scope.get("step_id") or "") == completed_step_id:
            result[key] = {}
    return result


class CommitStep:
    """Validate and commit one verified attempt through one PlanState CAS write."""

    def __init__(self, *, data_path: str, gate_evaluator: GateEvaluator | None = None) -> None:
        self.data_path = str(data_path)
        self.gate_evaluator = gate_evaluator or _default_gate

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
            "commit_step_outcome": CommitStepOutcome(
                schema_version=1,
                type=outcome_type,
                summary=summary,
                scope={"task_id": task_id or "unknown"},
                payload=payload,
                evidence_refs=evidence_refs,
            ).to_dict()
        }

    def _rejected(
        self, task_id: str, reason_code: str, failed_invariant: str, *refs: str
    ) -> dict[str, Any]:
        return self._result(
            "contract_rejected",
            task_id=task_id,
            summary="CommitStep rejected an ineligible verification contract.",
            payload={
                "reason_code": reason_code,
                "failed_invariant": failed_invariant,
                "no_partial_mutation": True,
            },
            evidence_refs=tuple(item for item in refs if item),
        )

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        raw_outcome = state.get("verification_outcome")
        try:
            verification = (
                raw_outcome
                if isinstance(raw_outcome, VerificationOutcome)
                else VerificationOutcome.from_mapping(raw_outcome or {})
            )
        except Exception as exc:
            return self._rejected(
                task_id or "unknown", "VERIFICATION_OUTCOME_INVALID", type(exc).__name__
            )
        scope = dict(verification.scope)
        verification_value = verification.to_dict()
        verification_payload = dict(verification_value.get("payload") or {})
        task_id = str(scope.get("task_id") or task_id)
        if verification.type not in {"verified", "verified_with_warnings"}:
            return self._rejected(
                task_id, "VERIFICATION_NOT_COMMIT_ELIGIBLE", verification.type
            )
        raw_report = verification_payload.get("completion_report")
        raw_decision = verification_payload.get("semantic_decision")
        try:
            report = CompletionReport.from_mapping(raw_report or {})
            decision = SemanticDecision.from_mapping(raw_decision or {})
        except SemanticCompletionError as exc:
            return self._rejected(
                task_id,
                exc.code,
                "payload.completion_report_or_semantic_decision",
            )
        if report.effective_decision.decision_id != decision.decision_id:
            return self._rejected(
                task_id,
                "SEMANTIC_DECISION_REPORT_MISMATCH",
                "payload.semantic_decision.decision_id",
                report.report_id,
                decision.decision_id,
            )
        for key, report_value in report.scope.items():
            supplied_value = scope.get(key)
            if key == "state_generation":
                try:
                    matches = int(supplied_value) == int(report_value)
                except (TypeError, ValueError):
                    matches = False
            else:
                matches = str(supplied_value or "") == str(report_value or "")
            if not matches:
                return self._rejected(
                    task_id,
                    "SEMANTIC_REPORT_SCOPE_MISMATCH",
                    key,
                    report.report_id,
                    decision.decision_id,
                )
        if (
            report.deterministic_veto
            or report.effective_disposition != COMPLETION_ACCEPTED
            or decision.disposition != COMPLETION_ACCEPTED
        ):
            return self._rejected(
                task_id,
                "SEMANTIC_COMPLETION_NOT_COMMIT_ELIGIBLE",
                report.effective_disposition,
                report.report_id,
                decision.decision_id,
            )
        request_identity = {
            key: scope.get(key)
            for key in (
                "task_id", "plan_id", "plan_version", "step_id",
                "state_generation", "action_id", "attempt_id", "validation_id",
            )
        }
        request_identity.update({
            "completion_report_id": report.report_id,
            "semantic_decision_id": decision.decision_id,
            "semantic_consumption_token": report.consumption_token,
        })
        request_fingerprint = _fingerprint(request_identity)
        commit_id = "commit-step-" + request_fingerprint[:24]
        current = load_plan_state(self.data_path, task_id)
        task_state = dict(current.get("task_state") or {})
        receipts = dict(task_state.get("typed_step_commit_receipts") or {})
        prior = receipts.get(commit_id)
        if isinstance(prior, Mapping):
            if str(prior.get("request_fingerprint") or "") != request_fingerprint:
                return self._rejected(
                    task_id, "COMMIT_RECEIPT_IDENTITY_CONFLICT", commit_id
                )
            stored = prior.get("outcome")
            try:
                replay = CommitStepOutcome.from_mapping(stored or {})
            except Exception as exc:
                return self._rejected(
                    task_id, "COMMIT_RECEIPT_CORRUPT", type(exc).__name__, commit_id
                )
            semantic_consumptions = dict(
                task_state.get("semantic_completion_consumptions") or {}
            )
            consumed = semantic_consumptions.get(report.consumption_token)
            if not isinstance(consumed, Mapping) or any((
                str(consumed.get("report_id") or "") != report.report_id,
                str(consumed.get("decision_id") or "") != decision.decision_id,
                str(consumed.get("commit_id") or "") != commit_id,
            )):
                return self._rejected(
                    task_id,
                    "SEMANTIC_CONSUMPTION_RECEIPT_INVALID",
                    report.consumption_token,
                    commit_id,
                    report.report_id,
                )
            result = {"commit_step_outcome": replay.to_dict()}
            # Old receipts do not manufacture new projection authority. New
            # receipts replay their acknowledged successor, never latest state.
            projection = prior.get("successor_projection")
            if isinstance(projection, Mapping):
                result.update(_successor_projection(projection, state=state, durable=current,
                    completed_step_id=str(replay.payload.get("completed_step_id") or "")))
            return result

        # A terminal PlanState owns the race regardless of which stale
        # generation the late verifier observed.  Report the semantic terminal
        # conflict before the generic CAS mismatch and never construct a
        # candidate mutation from cancelled/completed authority.
        if task_status(current) in TASK_TERMINAL_STATUSES:
            return self._rejected(
                task_id, "TERMINAL_STATE_CONFLICT", task_status(current)
            )

        try:
            expected_generation = int(scope.get("state_generation"))
        except (TypeError, ValueError):
            return self._rejected(
                task_id, "STATE_GENERATION_INVALID", "scope.state_generation"
            )
        current_generation = int(current.get("state_generation") or 0)
        if current_generation != expected_generation:
            return self._result(
                "commit_conflict",
                task_id=task_id,
                summary="PlanState generation changed before CommitStep CAS.",
                payload={
                    "expected_state_generation": expected_generation,
                    "current_state_generation": current_generation,
                    "no_partial_mutation": True,
                },
            )
        if workflow_routing_schema_version(current) != WORKFLOW_ROUTING_SCHEMA_TYPED:
            return self._rejected(
                task_id, "WORKFLOW_ROUTING_SCHEMA_NOT_TYPED", "workflow_routing_schema_version"
            )
        expected_scope = {
            "task_id": str(current.get("task_id") or task_id),
            "plan_id": str(current.get("plan_id") or ""),
            "plan_version": str(current.get("plan_version") or ""),
            "step_id": str(current.get("current_step_id") or ""),
        }
        for key, expected in expected_scope.items():
            if str(scope.get(key) or "") != expected:
                return self._rejected(
                    task_id, "COMMIT_SCOPE_MISMATCH", key, verification.evidence_refs[0] if verification.evidence_refs else ""
                )
        step = next(
            (
                item for item in current.get("steps", ())
                if isinstance(item, Mapping)
                and str(item.get("step_id") or "") == expected_scope["step_id"]
            ),
            None,
        )
        if not step or str(step.get("status") or "") not in STEP_ACTIVE_STATUSES:
            return self._rejected(task_id, "CURRENT_STEP_INELIGIBLE", expected_scope["step_id"])

        raw_validation = verification_payload.get("validation_evidence")
        if not isinstance(raw_validation, Mapping):
            return self._rejected(task_id, "VALIDATION_EVIDENCE_INVALID", "payload.validation_evidence")
        try:
            validation = ValidationEvidence.from_mapping(dict(raw_validation))
        except (TypeError, ValueError) as exc:
            return self._rejected(
                task_id,
                "VALIDATION_EVIDENCE_INVALID",
                type(exc).__name__,
                report.report_id,
            )
        for key, actual in (
            ("task_id", validation.task_id),
            ("step_id", validation.step_id),
            ("action_id", validation.action_id),
            ("attempt_id", validation.attempt_id),
            ("validation_id", validation.validation_id),
        ):
            if str(scope.get(key) or "") != str(actual or ""):
                return self._rejected(task_id, "VALIDATION_SCOPE_MISMATCH", key, validation.validation_id)
        if validation.status not in {
            ValidationStatus.VALID.value, ValidationStatus.VALID_WITH_WARNINGS.value
        }:
            return self._rejected(task_id, "VALIDATION_STATUS_INELIGIBLE", validation.status, validation.validation_id)
        role = str(validation.command_role or "").lower()
        if is_recovery_diagnostic_execution(current, validation.to_dict()) or (
            role in {"probe", "diagnostic", "optional_probe", "capability_probe"}
            and not is_approved_step_execution(current, validation.to_dict())
        ):
            return self._rejected(task_id, "PROBE_COMPLETION_FORBIDDEN", role, validation.validation_id)

        provenance = dict(current.get("provenance_state") or {})
        attempt = dict((provenance.get("attempts") or {}).get(validation.attempt_id) or {})
        if not attempt:
            return self._rejected(task_id, "EXECUTION_ATTEMPT_MISSING", validation.attempt_id)
        for key, expected in (
            ("plan_id", expected_scope["plan_id"]),
            ("plan_version", expected_scope["plan_version"]),
            ("step_id", expected_scope["step_id"]),
            ("action_id", validation.action_id),
        ):
            if str(attempt.get(key) or "") != str(expected or ""):
                return self._rejected(task_id, "EXECUTION_ATTEMPT_SCOPE_MISMATCH", key, validation.attempt_id)
        if is_recovery_diagnostic_execution(current, attempt) or (
            str(attempt.get("command_role") or "").lower() in {
                "probe", "diagnostic", "optional_probe", "capability_probe"
            } and not is_approved_step_execution(current, attempt)
        ):
            return self._rejected(task_id, "PROBE_COMPLETION_FORBIDDEN", "attempt.command_role", validation.attempt_id)
        if str(attempt.get("status") or "").upper() not in {
            ATTEMPT_EVIDENCE_COMMITTED, ATTEMPT_VALIDATED, ATTEMPT_VALIDATION_PASSED
        }:
            return self._rejected(task_id, "EXECUTION_ATTEMPT_STATUS_INELIGIBLE", str(attempt.get("status") or ""), validation.attempt_id)

        action = dict(state.get("pending_action") or {}) if isinstance(state.get("pending_action"), Mapping) else {}
        if not action and isinstance(state.get("generate_outcome"), Mapping):
            generate_outcome = GenerateOutcome.from_mapping(state["generate_outcome"])
            action = dict(generate_outcome.to_dict().get("payload", {}).get("action") or {})
        execution = dict(state.get("execution_evidence") or {}) if isinstance(state.get("execution_evidence"), Mapping) else {}
        if not execution and isinstance(state.get("execute_outcome"), Mapping):
            execute_outcome = ExecuteOutcome.from_mapping(state["execute_outcome"])
            execute_payload = dict(execute_outcome.to_dict().get("payload") or {})
            execution = dict(
                execute_payload.get("execution_record")
                or execute_payload.get("diagnostic_record")
                or {}
            )
        if action and str(action.get("action_id") or "") != validation.action_id:
            return self._rejected(task_id, "ACTION_SCOPE_MISMATCH", "pending_action.action_id", validation.validation_id)
        if execution:
            for key, expected in (
                ("step_id", validation.step_id), ("action_id", validation.action_id),
                ("attempt_id", validation.attempt_id),
            ):
                if str(execution.get(key) or "") != str(expected or ""):
                    return self._rejected(task_id, "EXECUTION_RECORD_SCOPE_MISMATCH", key, validation.validation_id)

        semantic_candidate = deepcopy(current)
        if str(attempt.get("status") or "").upper() != ATTEMPT_VALIDATION_PASSED:
            record_provenance_event(semantic_candidate, "validation_recorded", {
                "attempt_id": validation.attempt_id,
                "validation": validation.to_dict(),
                "validation_evidence_ref": validation.validation_id,
            })
        semantic_gate = dict(
            self.gate_evaluator(self.data_path, task_id, semantic_candidate)
        )
        try:
            recomputed_report = build_completion_report(
                plan_state=current,
                validation=validation.to_dict(),
                execution_evidence=execution or attempt,
                completion_gate=semantic_gate,
            )
        except SemanticCompletionError as exc:
            return self._rejected(
                task_id,
                exc.code,
                "completion_report_recompute",
                report.report_id,
            )
        failing_predicate = next(
            (
                dict(item)
                for item in recomputed_report.predicate_results
                if item.get("passed") is not True
            ),
            {},
        )
        if (
            recomputed_report.deterministic_veto
            or recomputed_report.effective_disposition != COMPLETION_ACCEPTED
        ):
            return self._rejected(
                task_id,
                "DETERMINISTIC_SEMANTIC_VETO",
                str(
                    failing_predicate.get("reason_code")
                    or recomputed_report.effective_disposition
                ),
                recomputed_report.report_id,
                recomputed_report.effective_decision.decision_id,
            )
        if any((
            recomputed_report.report_id != report.report_id,
            recomputed_report.effective_decision.decision_id != decision.decision_id,
            recomputed_report.consumption_token != report.consumption_token,
        )):
            return self._rejected(
                task_id,
                "COMPLETION_REPORT_RECOMPUTE_MISMATCH",
                "report_id_or_decision_id_or_consumption_token",
                report.report_id,
                recomputed_report.report_id,
            )
        current_consumptions = dict(
            (current.get("task_state") or {}).get(
                "semantic_completion_consumptions"
            )
            or {}
        )
        if report.consumption_token in current_consumptions:
            return self._rejected(
                task_id,
                "SEMANTIC_CONSUMPTION_ALREADY_RECORDED",
                report.consumption_token,
                report.report_id,
            )

        candidate = semantic_candidate
        gate = semantic_gate
        supplied_gate = dict(verification_payload.get("completion_gate") or {})
        if not supplied_gate.get("passed") or not gate.get("passed"):
            return self._rejected(task_id, "COMPLETION_GATE_REJECTED", str(gate.get("classification") or "gate_failed"), validation.validation_id)
        if supplied_gate.get("plan_version") and str(supplied_gate.get("plan_version")) != expected_scope["plan_version"]:
            return self._rejected(task_id, "COMPLETION_GATE_SCOPE_MISMATCH", "plan_version", validation.validation_id)

        for version_id in attempt.get("artifact_version_ids", ()) or ():
            artifact = dict((candidate.get("provenance_state") or {}).get("artifacts", {}).get(str(version_id)) or {})
            if (
                not artifact
                or str(artifact.get("producer_attempt_id") or "") != validation.attempt_id
                or str(artifact.get("producer_action_id") or "") != validation.action_id
                or str(artifact.get("producer_step_id") or "") != validation.step_id
            ):
                return self._rejected(task_id, "ARTIFACT_OWNERSHIP_MISMATCH", str(version_id), validation.validation_id)
        try:
            accepted = accept_action_binding_candidates(
                candidate, action, execution or attempt, validation.to_dict()
            )
        except PlanBindingError as exc:
            return self._rejected(task_id, "BINDING_OWNERSHIP_REJECTED", str(exc), validation.validation_id)
        advanced = advance_plan_after_verified_step(
            candidate,
            f"CommitStep {commit_id} accepted {validation.validation_id}",
            expected_step_id=validation.step_id,
            transition_id=commit_id,
            legacy_route_projection=False,
        )
        if not advanced:
            return self._rejected(task_id, "STEP_ADVANCE_REJECTED", validation.step_id, validation.validation_id)
        candidate_provenance = dict(candidate.get("provenance_state") or {})
        committed_attempt = (candidate_provenance.get("attempts") or {}).get(validation.attempt_id)
        if isinstance(committed_attempt, dict):
            committed_attempt["status"] = ATTEMPT_COMPLETED
        successor_step_id = str(candidate.get("current_step_id") or "")
        outcome_type = "next_step_ready" if successor_step_id else "plan_complete"
        accepted_effect_refs = _accepted_effect_refs(validation.effect_results, gate)
        outcome_payload = {
            "commit_id": commit_id,
            "receipt": f"typed-step-commit:{commit_id}",
            "before_state_generation": expected_generation,
            "after_state_generation": expected_generation + 1,
            "completed_step_id": validation.step_id,
            "accepted_binding_refs": [item.binding_id for item in accepted],
            "accepted_artifact_refs": list(attempt.get("artifact_version_ids") or []),
            "accepted_effect_refs": accepted_effect_refs,
            "completion_report_ref": report.report_id,
            "semantic_decision_ref": decision.decision_id,
            "semantic_consumption_ref": report.consumption_token,
        }
        if successor_step_id:
            outcome_payload["successor_step_id"] = successor_step_id
        semantic_outcome = CommitStepOutcome(
            schema_version=1,
            type=outcome_type,
            summary=(
                "Verified step committed and one successor activated."
                if successor_step_id else "Verified final step committed; final audit is required."
            ),
            scope={"task_id": task_id},
            payload=outcome_payload,
            evidence_refs=tuple(dict.fromkeys((
                validation.validation_id,
                report.report_id,
                decision.decision_id,
                report.consumption_token,
            ))),
        )
        task_state = dict(candidate.get("task_state") or {})
        receipts = dict(task_state.get("typed_step_commit_receipts") or {})
        receipts[commit_id] = {
            "schema_version": 1,
            "request_fingerprint": request_fingerprint,
            "outcome": semantic_outcome.to_dict(),
            "successor_projection": {"current_step_id": successor_step_id,
                "plan_id": str(candidate.get("plan_id") or ""), "plan_version": str(candidate.get("plan_version") or ""),
                # The verified CAS consumed the completed step's Recovery
                # instruction. Persist this projection for exact replay; a
                # stale unrelated handoff still fails TypedGenerate admission.
                "recovery_outcome": {}, "generate_seed": {}},
        }
        task_state["typed_step_commit_receipts"] = receipts
        semantic_consumptions = dict(
            task_state.get("semantic_completion_consumptions") or {}
        )
        semantic_consumptions[report.consumption_token] = {
            "schema_version": 1,
            "report_id": report.report_id,
            "decision_id": decision.decision_id,
            "commit_id": commit_id,
            "request_fingerprint": request_fingerprint,
            "scope": dict(report.scope),
        }
        task_state["semantic_completion_consumptions"] = semantic_consumptions
        candidate["task_state"] = task_state
        try:
            save_plan_state(self.data_path, task_id, candidate)
        except StateCASConflict:
            refreshed = load_plan_state(self.data_path, task_id)
            refreshed_generation = int(refreshed.get("state_generation") or 0)
            return self._result(
                "commit_conflict",
                task_id=task_id,
                summary="PlanState generation changed during CommitStep CAS.",
                payload={
                    "expected_state_generation": expected_generation,
                    "current_state_generation": refreshed_generation,
                    "no_partial_mutation": True,
                },
            )
        return {
            "commit_step_outcome": semantic_outcome.to_dict(),
            **_successor_projection(receipts[commit_id]["successor_projection"],
                state=state, durable=candidate, completed_step_id=validation.step_id),
        }


def commit_step_harness_error(exc: Exception, state: Mapping[str, Any]) -> CommitStepOutcome:
    return CommitStepOutcome(
        schema_version=1,
        type="harness_error",
        summary="CommitStep boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "COMMIT_STEP_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "atomicity_evidence": "authoritative PlanState CAS was not acknowledged",
        },
    )


__all__ = ["CommitStep", "commit_step_harness_error"]

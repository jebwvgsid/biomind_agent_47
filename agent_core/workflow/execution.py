"""Schema-2 Generate/Execute semantic adapters.

The adapters consume legacy implementations during the migration window, but
discard every legacy destination field.  Only strict semantic outcomes cross
the schema-2 graph boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable, Mapping

from langgraph.errors import GraphBubbleUp

from agent_core.workflow.outcomes import (
    ExecuteOutcome,
    GenerateOutcome,
    route_blind_state_projection,
)


NodeCallable = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]]

_REPAIRABLE_GENERATE_EXITS = frozenset({
    "unrecognized_action",
    "binding_operation_contract_mismatch",
    "managed_artifact_preaction_rejected",
    "controller_script_audit_failed",
    "external_bundle_preaction_rejected",
})
_INTERNAL_GENERATE_EXITS = frozenset({
    "recovery_execution_contract_invalid",
    "managed_artifact_projection_conflict",
    "controller_script_audit_non_convergent",
    "prompt_assembly_terminal",
    "generate_exception",
    "invalid_generate_return",
    "external_bundle_preaction_rejection_exhausted",
    "external_generated_script_drift",
    "recovery_middleware_internal_failure",
    "ambiguous_legacy_continuation",
})


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:24]


def _scope(state: Mapping[str, Any], *, action: Mapping[str, Any] | None = None) -> dict[str, Any]:
    action_value = dict(action or {})
    return {
        "task_id": str(state.get("task_id") or "unknown"),
        "plan_id": str(action_value.get("plan_id") or state.get("plan_id") or ""),
        "plan_version": str(action_value.get("plan_version") or state.get("plan_version") or ""),
        "step_id": str(action_value.get("step_id") or state.get("current_step_id") or ""),
    }


class TypedGenerate:
    """Translate Generate facts and enforce a monotonic corrective loop."""

    def __init__(self, *, generator: NodeCallable, repair_limit: int = 2) -> None:
        self.generator = generator
        self.repair_limit = max(1, int(repair_limit))

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state = route_blind_state_projection(state)
        try:
            result = await self.generator(dict(state))
        except GraphBubbleUp:
            raise
        except asyncio.CancelledError:
            raise
        if not isinstance(result, Mapping):
            raise TypeError("Generate must return a mapping")
        value = dict(result)
        action = dict(value.get("pending_action") or {}) if isinstance(value.get("pending_action"), Mapping) else {}
        scope = _scope(state, action=action)
        exit_reason = str(value.get("generate_exit_reason") or "").strip().lower()
        generate_result = str(value.get("generate_result") or "").strip().upper()
        evidence_refs = tuple(
            str(item) for item in value.get("evidence_refs", ()) or () if str(item)
        )

        if isinstance(value.get("cancellation_receipt"), Mapping):
            outcome_type = "cancelled"
            payload = {
                "cancellation_source": str(value.get("cancellation_source") or "user"),
                "cancellation_receipt": dict(value["cancellation_receipt"]),
            }
            summary = "Generate stopped on an authoritative cancellation receipt."
        elif action:
            candidate_fingerprint = str(
                action.get("candidate_fingerprint") or _fingerprint(action)
            )
            outcome_type = "action_ready"
            payload = {
                "action": action,
                "action_id": str(action.get("action_id") or ""),
                "candidate_fingerprint": candidate_fingerprint,
                "validation_evidence": {
                    "status": str(value.get("action_validation_status") or "validated"),
                    "reason_code": str(value.get("reason_code") or ""),
                },
            }
            summary = "A validated action candidate is ready for Execute."
        elif value.get("step_completion_candidate") or value.get("completion_candidate") or exit_reason in {
            "completion_candidate", "step_completion_candidate", "renderer_step_completion_candidate"
        }:
            claim = dict(value.get("step_completion_candidate") or value.get("completion_candidate") or {})
            candidate_fingerprint = str(claim.get("candidate_fingerprint") or _fingerprint(claim))
            outcome_type = "completion_claimed"
            payload = {
                "step_id": str(claim.get("step_id") or scope.get("step_id") or ""),
                "candidate_fingerprint": candidate_fingerprint,
                "claim": claim,
            }
            summary = "Generate claims existing evidence may complete the current step."
        elif isinstance(value.get("pending_human_question"), Mapping) and value.get("pending_human_question"):
            question = dict(value["pending_human_question"])
            outcome_type = "user_input_needed"
            payload = {
                "question_id": str(question.get("question_id") or _fingerprint(question)),
                "question": str(question.get("question") or question.get("prompt") or "Input is required."),
                "requested_slots": list(question.get("requested_slots") or ()),
                "owner": "INPUT_DATA",
            }
            summary = "Generate requires an input-owned user fact."
        elif exit_reason in {"current_step_missing", "plan_patch", "plan_patch_owner_rejected"}:
            outcome_type = "plan_revision_needed"
            payload = {
                "step_id": str(scope.get("step_id") or ""),
                "reason_code": exit_reason or "plan_revision_required",
                "contract_violations": list(value.get("validation_issues") or ()),
            }
            summary = "The approved Plan needs a structured revision."
        elif generate_result in {"SKILL_CONTINUATION", "SKILL_BINDING_PROPOSED", "SKILL_NO_PROGRESS"} or exit_reason in {
            "skill_decision_rejected", "skill_context_needed"
        }:
            continuation = dict(value.get("skill_decision_result") or value.get("skill_access_state") or {})
            outcome_type = "skill_context_needed"
            payload = {
                "step_id": str(scope.get("step_id") or ""),
                "requested_capability": str(continuation.get("requested_capability") or continuation.get("capability") or "selected_skill_context"),
                "continuation_fingerprint": _fingerprint(continuation or value.get("selected_skill_ids") or []),
            }
            summary = "Selected skill or capability context is insufficient."
        elif generate_result == "STALE_NODE_RESULT" or exit_reason == "node_result_stale_discarded" or value.get("stale_node_result"):
            stale = dict(value.get("stale_node_result") or {})
            outcome_type = "stale_result"
            payload = {
                "changed_preconditions": list(stale.get("changed_preconditions") or ()),
                "invocation_id": str(stale.get("invocation_id") or stale.get("decision_id") or _fingerprint(stale)),
                "consumed_revision_hashes": dict(stale.get("consumed_revision_hashes") or {}),
                "current_revision_hashes": dict(stale.get("current_revision_hashes") or {}),
                "counts_as_failure": False,
            }
            summary = "Generate result became stale before adoption."
        elif "provider" in exit_reason and ("failed" in exit_reason or "terminal" in exit_reason):
            provider = dict(value.get("provider_attempt_terminal") or value.get("generation_provenance") or {})
            outcome_type = "provider_failed"
            payload = {
                "error_code": str(value.get("reason_code") or exit_reason or "PROVIDER_FAILED"),
                "provider_attempt_ref": str(provider.get("attempt_ref") or provider.get("provider_attempt_ref") or _fingerprint(provider)),
                "claim_status": str(provider.get("claim_status") or "terminal"),
                "retryability_policy": str(provider.get("retryability_policy") or "typed_v1_fail_closed"),
            }
            summary = "Provider lifecycle ended without an adoptable action."
        elif exit_reason in _REPAIRABLE_GENERATE_EXITS and not isinstance(
            value.get("action_format_repair"), Mapping
        ):
            outcome_type = "internal_protocol_error"
            payload = {
                "error_code": "GENERATE_REPAIR_RECEIPT_MISSING",
                "component": "typed_generate_adapter",
            }
            summary = "Generate declared a repairable exit without a candidate-repair receipt."
        elif exit_reason in _REPAIRABLE_GENERATE_EXITS:
            repair = dict(value.get("action_format_repair") or {})
            candidate_fingerprint = str(
                repair.get("candidate_fingerprint")
                or repair.get("invalid_response_hash")
                or value.get("candidate_fingerprint")
                or _fingerprint(repair or value.get("reason_code") or exit_reason)
            )
            error_fingerprint = str(
                repair.get("error_fingerprint")
                or repair.get("validation_fingerprint")
                or _fingerprint({"reason": exit_reason, "repair": repair})
            )
            prior_raw = state.get("generate_outcome")
            prior = None
            if isinstance(prior_raw, Mapping):
                try:
                    prior = GenerateOutcome.from_mapping(prior_raw)
                except Exception:
                    prior = None
            prior_payload = dict(prior.to_dict().get("payload") or {}) if prior and prior.type == "candidate_rejected" else {}
            prior_receipt = dict(prior_payload.get("budget_receipt") or {})
            ordinal = int(prior_receipt.get("ordinal") or 0) + 1
            identical = bool(
                prior_payload
                and str(prior_payload.get("candidate_fingerprint") or "") == candidate_fingerprint
                and str(prior_payload.get("error_fingerprint") or "") == error_fingerprint
            )
            if identical or ordinal > self.repair_limit:
                outcome_type = "internal_protocol_error"
                payload = {
                    "error_code": "GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED" if identical else "GENERATE_REPAIR_BUDGET_EXHAUSTED",
                    "component": "typed_generate_repair_governance",
                    "candidate_fingerprint": candidate_fingerprint,
                    "error_fingerprint": error_fingerprint,
                }
                summary = "Corrective Generate failed closed after non-progress or budget exhaustion."
            else:
                outcome_type = "candidate_rejected"
                payload = {
                    "step_id": str(scope.get("step_id") or repair.get("step_id") or ""),
                    "violation_owner": str(value.get("failure_owner") or repair.get("failure_owner") or "GENERATED_ACTION"),
                    "candidate_fingerprint": candidate_fingerprint,
                    "error_fingerprint": error_fingerprint,
                    "feedback": str(repair.get("feedback") or repair.get("repair_instruction") or value.get("reason_code") or exit_reason),
                    "budget_receipt": {
                        "ordinal": ordinal,
                        "limit": self.repair_limit,
                        "remaining": self.repair_limit - ordinal,
                        "progress_proven": not identical,
                    },
                    "repairable": True,
                }
                summary = "A bounded generated candidate defect can be corrected."
        elif exit_reason in _INTERNAL_GENERATE_EXITS or str(value.get("recovery_status") or "").upper() == "INTERNAL_PROTOCOL_ERROR":
            outcome_type = "internal_protocol_error"
            payload = {
                "error_code": str(value.get("reason_code") or exit_reason or "GENERATE_INTERNAL_PROTOCOL_ERROR"),
                "component": "generate",
            }
            summary = "Generate encountered an internal protocol violation and failed closed."
        elif generate_result in {"BLOCKED", "FINISHED"}:
            outcome_type = "blocked"
            payload = {
                "owner": str(value.get("failure_owner") or "workflow"),
                "reason": str(value.get("reason_code") or exit_reason or "generate_blocked"),
                "terminal_receipt": str(value.get("terminal_receipt") or _fingerprint(value.get("terminal_internal_protocol_failure") or value)),
            }
            summary = "Generate recorded a durable terminal blocker."
        else:
            outcome_type = "internal_protocol_error"
            payload = {
                "error_code": "GENERATE_OUTCOME_AMBIGUOUS",
                "component": "typed_generate_adapter",
            }
            summary = "Generate returned an ambiguous legacy continuation under schema 2."

        outcome = GenerateOutcome(
            schema_version=1,
            type=outcome_type,
            summary=summary,
            scope=scope,
            payload=payload,
            evidence_refs=evidence_refs,
        )
        return {"generate_outcome": outcome.to_dict()}


class TypedExecute:
    """Translate exactly one Execute invocation into an ExecuteOutcome."""

    def __init__(self, *, executor: NodeCallable) -> None:
        self.executor = executor

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state = route_blind_state_projection(state)
        invocation = dict(state)
        raw_generate = state.get("generate_outcome")
        generate = None
        if isinstance(raw_generate, Mapping):
            generate = GenerateOutcome.from_mapping(raw_generate)
            if generate.type == "action_ready":
                invocation["pending_action"] = dict(
                    generate.to_dict().get("payload", {}).get("action") or {}
                )
        action = dict(invocation.get("pending_action") or {})
        try:
            result = await self.executor(invocation)
        except GraphBubbleUp:
            raise
        except asyncio.CancelledError:
            raise
        if not isinstance(result, Mapping):
            raise TypeError("Execute must return a mapping")
        value = dict(result)
        execution = dict(value.get("execution_evidence") or {}) if isinstance(value.get("execution_evidence"), Mapping) else {}
        scope = _scope(state, action=action)

        failure = dict(
            (value.get("terminal_internal_protocol_failure") or {}).get("primary_failure") or {}
        ) if isinstance(value.get("terminal_internal_protocol_failure"), Mapping) else {}
        if failure and failure.get("command_started") is False:
            outcome_type = "harness_error"
            payload = {
                "error_code": str(failure.get("error_code") or "EXECUTE_PRELAUNCH_HARNESS_ERROR"),
                "exception_class": str(failure.get("exception_type") or "InternalError"),
                "phase": str(failure.get("phase") or failure.get("stage") or "prelaunch"),
                "command_started": False,
            }
            summary = "Execute failed inside the harness before command launch."
        elif value.get("stale_node_result") or str((value.get("execution_result") or {}).get("status") or "") == "discarded":
            stale = dict(value.get("stale_node_result") or value.get("execution_result") or {})
            outcome_type = "stale_result"
            payload = {
                "changed_preconditions": list(stale.get("changed_preconditions") or ()),
                "action_id": str(stale.get("action_id") or action.get("action_id") or ""),
                "attempt_id": str(stale.get("attempt_id") or "not_started"),
                "audit_ref": str(stale.get("audit_ref") or _fingerprint(stale)),
                "counts_as_failure": False,
            }
            summary = "Execute discarded a stale action without launching it."
        elif isinstance(value.get("approval_decision"), Mapping):
            decision = dict(value["approval_decision"])
            outcome_type = "approval_rejected"
            payload = {
                "approval_request_id": str(decision.get("approval_request_id") or _fingerprint(decision)),
                "guidance": str(decision.get("guidance") or decision.get("reason") or "rejected"),
                "command_started": False,
            }
            summary = "User rejected or revised the candidate before execution."
        elif isinstance(value.get("cancellation_receipt"), Mapping):
            outcome_type = "cancelled"
            payload = {
                "cancellation_receipt": dict(value["cancellation_receipt"]),
                "command_started": False,
            }
            summary = "User cancelled before command launch."
        elif isinstance(value.get("pending_human_question"), Mapping) and value.get("pending_human_question"):
            question = dict(value["pending_human_question"])
            outcome_type = "user_input_needed"
            payload = {
                "question_id": str(question.get("question_id") or _fingerprint(question)),
                "question": str(question.get("question") or "Input is required."),
                "requested_slots": list(question.get("requested_slots") or ()),
                "owner": "INPUT_DATA",
            }
            summary = "Execute preflight requires an input-owned user decision."
        elif isinstance(value.get("installation_request"), Mapping):
            request = dict(value["installation_request"])
            outcome_type = "installation_requested"
            payload = {
                "tool_or_capability": str(request.get("tool_or_capability") or request.get("tool_name") or "runtime"),
                "requirement_evidence": dict(request.get("requirement_evidence") or request),
                "approval_policy": str(request.get("approval_policy") or "existing_install_policy"),
                "attempt_fingerprint": str(request.get("attempt_fingerprint") or _fingerprint(request)),
            }
            summary = "An approved runtime installation is required."
        elif execution:
            action_id = str(execution.get("action_id") or action.get("action_id") or "")
            attempt_id = str(execution.get("attempt_id") or execution.get("execution_id") or "")
            return_code = execution.get("returncode", execution.get("exit_code"))
            role = str(execution.get("command_role") or "analysis_execution").lower()
            if role in {"probe", "diagnostic", "optional_probe", "capability_probe"} or value.get("diagnostic_evidence"):
                diagnostic = dict(value.get("diagnostic_evidence") or execution.get("diagnostic_evidence") or execution)
                outcome_type = "diagnostic_completed"
                payload = {
                    "diagnostic_record": diagnostic,
                    "envelope_identity": str((action.get("recovery_execution_envelope") or {}).get("immutable_hash") or _fingerprint(action)),
                    "attempt_id": attempt_id,
                    "status": str(diagnostic.get("status") or "recorded"),
                }
                summary = "A bounded diagnostic command produced immutable evidence."
            elif str(state.get("task_mode") or "managed").lower() in {"readonly", "simple_execute"}:
                outcome_type = "lightweight_completed"
                payload = {
                    "execution_record": execution,
                    "status": "succeeded" if return_code in (0, "0") else "failed",
                    "task_mode": str(state.get("task_mode") or ""),
                }
                summary = "Lightweight execution completed."
            elif return_code in (0, "0") and not (
                isinstance(execution.get("validation"), Mapping)
                and execution.get("validation", {}).get("passed") is False
            ):
                outcome_type = "execution_succeeded"
                payload = {
                    "execution_record": execution,
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                    "return_code": 0,
                    "validation": dict(execution.get("validation") or {"passed": True}),
                }
                summary = "The command completed and immutable execution evidence was recorded."
            else:
                outcome_type = "execution_failed"
                payload = {
                    "execution_record": execution,
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                    "return_code": return_code,
                    "failure_fingerprint": str(execution.get("failure_fingerprint") or _fingerprint(execution)),
                    "progress_snapshot": dict(value.get("recovery_progress") or execution.get("progress_snapshot") or {}),
                }
                summary = "A launched command failed after immutable evidence persistence."
        elif str(value.get("task_status") or "").lower() == "blocked":
            outcome_type = "blocked"
            payload = {
                "owner": str(value.get("failure_owner") or "policy"),
                "blocker_evidence": dict(value.get("failure") or {"reason": value.get("contract_status") or "blocked"}),
                "command_started": False,
            }
            summary = "Execute policy established a terminal blocker."
        elif (
            isinstance(value.get("pre_execution_rejection"), Mapping)
            or isinstance(value.get("failure"), Mapping)
            or str(value.get("contract_status") or "").lower().startswith(
                ("blocked", "rejected", "invalid", "missing", "failed")
            )
        ):
            rejection = dict(value.get("pre_execution_rejection") or value.get("failure") or {})
            violation_owner = str(
                rejection.get("failure_owner")
                or rejection.get("owner")
                or value.get("failure_owner")
                or "INTERNAL_PROTOCOL"
            )
            violation_code = str(
                rejection.get("violation_code")
                or rejection.get("reason_code")
                or value.get("contract_status")
                or "PRE_EXECUTION_REJECTED"
            )
            outcome_type = "pre_execution_rejected"
            payload = {
                "violation_owner": violation_owner,
                "violation_code": violation_code,
                "candidate_fingerprint": str(action.get("candidate_fingerprint") or _fingerprint(action)),
                "command_started": False,
                "budget_receipt": dict(value.get("budget_receipt") or {"consumed": False}),
            }
            summary = "Execute rejected the candidate before command launch."
        else:
            outcome_type = "harness_error"
            payload = {
                "error_code": "EXECUTE_OUTCOME_AMBIGUOUS",
                "exception_class": "SemanticCoverageError",
                "phase": "post_execution_classification",
                "command_started": False,
            }
            summary = "Execute returned without authoritative execution or rejection evidence."

        outcome = ExecuteOutcome(
            schema_version=1,
            type=outcome_type,
            summary=summary,
            scope=scope,
            payload=payload,
            evidence_refs=tuple(
                str(item) for item in execution.get("evidence_refs", ()) or () if str(item)
            ),
        )
        return {"execute_outcome": outcome.to_dict()}


def generate_harness_error(exc: Exception, state: Mapping[str, Any]) -> GenerateOutcome:
    return GenerateOutcome(
        schema_version=1,
        type="harness_error",
        summary="Typed Generate boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "GENERATE_NODE_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "detail": str(exc)[:800],
        },
    )


def execute_harness_error(exc: Exception, state: Mapping[str, Any]) -> ExecuteOutcome:
    return ExecuteOutcome(
        schema_version=1,
        type="harness_error",
        summary="Typed Execute boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "EXECUTE_NODE_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "phase": "typed_execute_adapter",
            "command_started": False,
        },
    )


__all__ = [
    "TypedGenerate", "TypedExecute", "generate_harness_error", "execute_harness_error"
]

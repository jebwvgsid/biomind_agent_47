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

from agent_core.context_lineage import redact_text
from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.candidate_repair import (
    CANDIDATE_REPAIR_RECEIPT_PRODUCER,
    CandidateRepairReceiptError,
    candidate_repair_scope,
    validate_candidate_repair_receipt,
)
from agent_core.workflow.generate_handoff import project_corrective_generate_seed
from agent_core.workflow.outcomes import (
    ExecuteOutcome,
    GenerateOutcome,
    RecoveryOutcome,
    route_blind_state_projection,
)


NodeCallable = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]]

_REPAIRABLE_GENERATE_EXITS = frozenset({
    "unrecognized_action",
    "binding_operation_contract_mismatch",
    "public_binding_rejected",
    "workspace_effect_contract_mismatch",
    "full_binding_admission_rejected",
    "managed_artifact_preaction_rejected",
    "external_bundle_preaction_rejected",
    "lightweight_action_rejected",
    "completion_candidate_rejected",
    "plan_patch_owner_rejected",
    "blocked_evidence_rejected",
    "execute_empty_body_rejected",
    "legacy_activation_rejected",
    "knowledge_search_action_rejected",
})
_INTERNAL_GENERATE_EXITS = frozenset({
    "recovery_execution_contract_invalid",
    "managed_artifact_projection_conflict",
    "controller_script_audit_non_convergent",
    "controller_script_audit_contract_invalid",
    "prompt_assembly_terminal",
    "generate_exception",
    "invalid_generate_return",
    "external_bundle_preaction_rejection_exhausted",
    "external_generated_script_drift",
    "recovery_middleware_internal_failure",
    "ambiguous_legacy_continuation",
    "operation_binding_contract_invalid",
    "managed_artifact_preaction_rejection_exhausted",
    "candidate_repair_receipt_issue_failed",
    "candidate_repair_receipt_projection_invalid",
    "candidate_repair_recovery_rollover_failed",
    "recovery_generation_adoption_failed",
    "candidate_admission_scope_stale",
    "external_bundle_contract_invalid",
})

_GENERATE_SKILL_CALLERS = frozenset({"generate", "corrective_generate"})
_SKILL_CONTEXT_READY_STATUSES = frozenset({"COMPLETE", "REQUIRES_NARROWING"})
_SKILL_SEARCH_READY_STATUSES = frozenset({"MATCHES_FOUND", "NO_MATCH"})


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


def _repair_receipt_scope(
    state: Mapping[str, Any],
    *,
    data_path: str | None,
) -> dict[str, Any]:
    """Resolve receipt scope from durable PlanState when it is available."""

    task_id = str(state.get("task_id") or "unknown")
    durable: Mapping[str, Any] = state
    if data_path is not None:
        try:
            durable = load_plan_state(data_path, task_id)
        except Exception:
            durable = state
    return candidate_repair_scope(
        durable,
        task_id=task_id,
        step_id=str(
            durable.get("current_step_id")
            or state.get("current_step_id")
            or ""
        ),
    )


def _durable_repair_state(
    state: Mapping[str, Any],
    *,
    data_path: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read scope and the CAS-committed candidate-repair ledger together."""

    task_id = str(state.get("task_id") or "unknown")
    durable: Mapping[str, Any] = state
    if data_path is not None:
        durable = load_plan_state(data_path, task_id)
    scope = candidate_repair_scope(
        durable,
        task_id=task_id,
        step_id=str(
            durable.get("current_step_id")
            or state.get("current_step_id")
            or ""
        ),
    )
    repair_state = dict(
        ((durable.get("task_state") or {}).get("candidate_repair_state"))
        or {}
    )
    return scope, repair_state


def _internal_generate_failure_payload(
    value: Mapping[str, Any],
    *,
    exit_reason: str,
) -> dict[str, Any]:
    """Keep causal admission details when compatibility still says BLOCKED."""

    error_code = str(
        value.get("reason_code") or exit_reason or "GENERATE_INTERNAL_PROTOCOL_ERROR"
    )
    primary = dict(value.get("primary_failure") or value.get("failure") or {})
    secondary = [
        dict(item)
        for item in value.get("secondary_failures") or []
        if isinstance(item, Mapping)
    ]
    payload: dict[str, Any] = {
        "error_code": error_code,
        "component": "generate",
        "actual_outcome": "internal_protocol_error",
        "actual_route": "END",
        "execution_status": str(value.get("execution_status") or "not_started"),
        "recovery_entered": bool(value.get("recovery_entered", False)),
        "outputs_produced": list(value.get("outputs_produced") or []),
    }
    if primary:
        payload["primary_cause"] = primary
        payload["latest_failure"] = dict(primary.get("latest_failure") or primary)
    payload["termination_reason"] = str(value.get("termination_reason") or "")
    event = dict(value.get("candidate_rejection_event") or {})
    payload["budget_receipt"] = {k: event[k] for k in ("repair_ordinal", "repair_limit", "event_id", "scope") if k in event}
    if secondary:
        payload["secondary_causes"] = secondary
    return payload


def _repair_handoff_failure_payload(
    value: Mapping[str, Any],
    *,
    exit_reason: str,
    error_code: str,
    detail: str = "",
) -> dict[str, Any]:
    """Preserve the candidate-admission cause and the adapter failure."""

    primary = dict(value.get("primary_failure") or value.get("failure") or {})
    if not primary:
        primary = {
            "error_code": str(value.get("reason_code") or exit_reason or "CANDIDATE_REJECTED"),
            "stage": "pre_execution_candidate_admission",
            "message": str(
                value.get("reason_code")
                or "Generate rejected a candidate before execution."
            ),
            "execution_started": False,
        }
    secondary = {
        "error_code": str(error_code),
        "stage": "typed_generate_handoff",
        "message": str(detail or "The candidate-repair receipt failed validation."),
        "execution_started": False,
    }
    return {
        "error_code": str(error_code),
        "component": "typed_generate_adapter",
        "detail": str(detail or ""),
        "primary_cause": primary,
        "secondary_causes": [secondary],
        "actual_outcome": "internal_protocol_error",
        "execution_status": "not_started",
        "recovery_entered": False,
        "outputs_produced": [],
    }


def _skill_continuation_receipt_issue(
    value: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    expected_request_kind: str,
    allowed_result_statuses: frozenset[str],
) -> tuple[str, str] | None:
    """Validate a same-node observation against its persisted owner receipt."""

    continuation_id = str(result.get("continuation_id") or "")
    result_status = str(result.get("status") or "").upper()
    pointer = str(value.get("skill_continuation_id") or "")
    access = value.get("skill_access_state")
    if not continuation_id or not pointer or pointer != continuation_id:
        return (
            "GENERATE_SKILL_CONTINUATION_POINTER_MISMATCH",
            "The returned continuation does not match the durable continuation pointer.",
        )
    if result_status not in allowed_result_statuses:
        return (
            "GENERATE_SKILL_CONTINUATION_STATUS_INVALID",
            f"result_status={result_status or 'missing'} is not resumable.",
        )
    if not isinstance(access, Mapping):
        return (
            "GENERATE_SKILL_CONTINUATION_RECEIPT_MISSING",
            "The canonical Skill access projection is missing.",
        )
    raw_continuations = access.get("continuations")
    raw_results = access.get("skill_tool_results")
    continuation = (
        dict(raw_continuations.get(continuation_id) or {})
        if isinstance(raw_continuations, Mapping)
        else {}
    )
    stored_result = (
        dict(raw_results.get(continuation_id) or {})
        if isinstance(raw_results, Mapping)
        else {}
    )
    owner = str(continuation.get("caller_node") or "")
    result_identity_key = (
        "request_id"
        if expected_request_kind == "REQUEST_SKILL_CONTEXT"
        else "search_id"
    )
    if (
        str(continuation.get("continuation_id") or "") != continuation_id
        or owner not in _GENERATE_SKILL_CALLERS
        or str(continuation.get("status") or "") != "served"
        or str(continuation.get("request_kind") or "") != expected_request_kind
    ):
        return (
            "GENERATE_SKILL_CONTINUATION_OWNER_MISMATCH",
            "The continuation is not a served observation owned by Generate.",
        )
    if (
        str(continuation.get(result_identity_key) or "")
        != str(result.get(result_identity_key) or "")
        or str(continuation.get("result_status") or "").upper()
        != result_status
    ):
        return (
            "GENERATE_SKILL_CONTINUATION_IDENTITY_MISMATCH",
            "The continuation identity does not match the returned observation.",
        )
    if not stored_result or _fingerprint(stored_result) != _fingerprint(result):
        return (
            "GENERATE_SKILL_CONTINUATION_RESULT_MISMATCH",
            "The returned observation does not match its canonical stored result.",
        )
    return None


class TypedGenerate:
    """Translate Generate facts and enforce a monotonic corrective loop."""

    def __init__(
        self,
        *,
        generator: NodeCallable,
        repair_limit: int = 2,
        data_path: str | None = None,
    ) -> None:
        self.generator = generator
        self.repair_limit = max(1, int(repair_limit))
        self.data_path = str(data_path) if data_path is not None else None

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state = route_blind_state_projection(state)
        generator_state = dict(state)
        raw_prior_generate = state.get("generate_outcome")
        if isinstance(raw_prior_generate, Mapping):
            try:
                prior_generate = GenerateOutcome.from_mapping(raw_prior_generate)
            except Exception:
                prior_generate = None
            if prior_generate is not None and prior_generate.type == "candidate_rejected":
                prior_payload = dict(
                    prior_generate.to_dict().get("payload") or {}
                )
                repair_context = dict(prior_payload.get("repair_context") or {})
                generator_state["action_format_repair"] = {
                    **repair_context,
                    "step_id": str(prior_payload.get("step_id") or ""),
                    "candidate_fingerprint": str(
                        prior_payload.get("candidate_fingerprint") or ""
                    ),
                    "error_fingerprint": str(
                        prior_payload.get("error_fingerprint") or ""
                    ),
                    "feedback": str(prior_payload.get("feedback") or ""),
                    "budget_receipt": dict(
                        prior_payload.get("budget_receipt") or {}
                    ),
                }
                # Preserve the exact trusted receipt body separately from the
                # compatibility projection above.  The production Generate
                # prompt boundary re-reads durable PlanState and validates this
                # body immediately before making any of it provider-visible.
                generator_state[
                    "_candidate_repair_receipt_for_projection"
                ] = dict(repair_context)
                generator_state[
                    "_candidate_repair_receipt_required_for_projection"
                ] = True
        raw_prior_execute = state.get("execute_outcome")
        if isinstance(raw_prior_execute, Mapping):
            try:
                prior_execute = ExecuteOutcome.from_mapping(raw_prior_execute)
            except Exception:
                prior_execute = None
            if prior_execute is not None and prior_execute.type == "pre_execution_rejected":
                execute_value = prior_execute.to_dict()
                execute_payload = dict(execute_value.get("payload") or {})
                execute_scope = dict(execute_value.get("scope") or {})
                repair_context = dict(execute_payload.get("repair_context") or {})
                generator_state["action_format_repair"] = {
                    **repair_context,
                    "repair_stage": str(
                        repair_context.get("repair_stage") or "execute_preflight"
                    ),
                    "step_id": str(execute_scope.get("step_id") or ""),
                    "candidate_fingerprint": str(
                        execute_payload.get("candidate_fingerprint") or ""
                    ),
                    "error_fingerprint": str(
                        execute_payload.get("error_fingerprint")
                        or _fingerprint({
                            "violation_owner": execute_payload.get("violation_owner"),
                            "violation_code": execute_payload.get("violation_code"),
                            "repair_context": repair_context,
                        })
                    ),
                    "feedback": str(
                        execute_payload.get("feedback")
                        or "Repair the rejected current-step action before execution."
                    ),
                    "budget_receipt": dict(
                        execute_payload.get("budget_receipt") or {}
                    ),
                }
                generator_state[
                    "_candidate_repair_receipt_for_projection"
                ] = dict(repair_context)
                generator_state[
                    "_candidate_repair_receipt_required_for_projection"
                ] = True
        raw_recovery = state.get("recovery_outcome")
        if isinstance(raw_recovery, Mapping):
            try:
                recovery_outcome = RecoveryOutcome.from_mapping(raw_recovery)
            except Exception:
                recovery_outcome = None
            if recovery_outcome is not None and recovery_outcome.type == "candidate_repair_required":
                if self.data_path is None:
                    return {"generate_outcome": GenerateOutcome(
                        schema_version=1,
                        type="internal_protocol_error",
                        summary="Corrective Generate could not resolve its Recovery handoff.",
                        scope=_scope(state),
                        payload={
                            "error_code": "GENERATE_RECOVERY_DURABLE_STATE_REQUIRED",
                            "component": "generate_recovery_handoff",
                            "detail": "TypedGenerate.data_path is not configured.",
                        },
                    ).to_dict()}
                try:
                    durable = load_plan_state(
                        self.data_path, str(state.get("task_id") or "default")
                    )
                    generate_seed = project_corrective_generate_seed(
                        recovery_outcome, durable
                    )
                except Exception as exc:
                    return {"generate_outcome": GenerateOutcome(
                        schema_version=1,
                        type="internal_protocol_error",
                        summary="Corrective Generate rejected an invalid Recovery handoff.",
                        scope=_scope(state),
                        payload={
                            "error_code": str(
                                getattr(exc, "code", None)
                                or "GENERATE_RECOVERY_HANDOFF_INVALID"
                            ),
                            "component": "generate_recovery_handoff",
                            "detail": str(getattr(exc, "detail", None) or exc)[:800],
                        },
                    ).to_dict()}
                for key in list(generator_state):
                    if key.startswith("recovery_") or key in {
                        "messages", "failure_packet", "failure_source",
                        "active_failure_packet_ref", "_provider_episode",
                    }:
                        generator_state.pop(key, None)
                generator_state["generate_seed"] = generate_seed.to_dict()

        # Production schema-2 always configures ``data_path``.  Re-read the
        # current durable scope and ledger immediately before crossing into the
        # legacy Generate implementation, which is the provider-prompt owner.
        # Only producer-marked pre-execution candidate repairs claim this
        # authority; legacy post-execution Recovery compatibility shapes do not.
        format_state = (
            dict(generator_state.get("action_format_repair") or {})
            if isinstance(generator_state.get("action_format_repair"), Mapping)
            else {}
        )
        exact_projection_receipt = generator_state.get(
            "_candidate_repair_receipt_for_projection"
        )
        projection_receipt_required = bool(
            generator_state.get(
                "_candidate_repair_receipt_required_for_projection"
            )
            or str(format_state.get("producer") or "")
            == CANDIDATE_REPAIR_RECEIPT_PRODUCER
            or str(format_state.get("receipt_id") or "").startswith(
                "candidate-repair-receipt:"
            )
            or str(format_state.get("rejection_event_id") or "").startswith(
                "candidate-rejection:"
            )
            or str(format_state.get("failure_stage") or "")
            == "pre_execution_candidate_admission"
        )
        if projection_receipt_required and self.data_path is not None:
            raw_projection_receipt = (
                exact_projection_receipt
                if exact_projection_receipt is not None
                else format_state
            )
            try:
                durable_scope, committed_repair_state = _durable_repair_state(
                    state,
                    data_path=self.data_path,
                )
                validated_projection_receipt = validate_candidate_repair_receipt(
                    raw_projection_receipt,
                    data_path=self.data_path,
                    task_id=str(state.get("task_id") or "unknown"),
                    expected_scope=durable_scope,
                    expected_candidate_fingerprint=str(
                        format_state.get("candidate_fingerprint")
                        or (
                            raw_projection_receipt.get("candidate_fingerprint")
                            if isinstance(raw_projection_receipt, Mapping)
                            else ""
                        )
                        or ""
                    ),
                    committed_repair_state=committed_repair_state,
                )
                for key, expected in (
                    (
                        "step_id",
                        str(
                            (validated_projection_receipt.get("scope") or {}).get(
                                "step_id"
                            )
                            or ""
                        ),
                    ),
                    (
                        "candidate_fingerprint",
                        str(
                            validated_projection_receipt[
                                "candidate_fingerprint"
                            ]
                        ),
                    ),
                    (
                        "error_fingerprint",
                        str(validated_projection_receipt["error_fingerprint"]),
                    ),
                    ("feedback", str(validated_projection_receipt["feedback"])),
                ):
                    if (
                        key in format_state
                        and (
                            type(format_state.get(key)) is not type(expected)
                            or format_state.get(key) != expected
                        )
                    ):
                        raise CandidateRepairReceiptError(
                            "CANDIDATE_REPAIR_RECEIPT_ENVELOPE_MISMATCH",
                            key,
                        )
                budget_receipt = format_state.get("budget_receipt")
                if isinstance(budget_receipt, Mapping):
                    for key, expected in (
                        (
                            "ordinal",
                            int(validated_projection_receipt["repair_ordinal"]),
                        ),
                        ("progress_proven", True),
                        (
                            "rejection_event_id",
                            str(
                                validated_projection_receipt[
                                    "rejection_event_id"
                                ]
                            ),
                        ),
                        (
                            "receipt_ref",
                            str(validated_projection_receipt["receipt_ref"]),
                        ),
                    ):
                        if (
                            key in budget_receipt
                            and (
                                type(budget_receipt.get(key))
                                is not type(expected)
                                or budget_receipt.get(key) != expected
                            )
                        ):
                            raise CandidateRepairReceiptError(
                                "CANDIDATE_REPAIR_RECEIPT_ENVELOPE_MISMATCH",
                                f"budget_receipt.{key}",
                            )
            except (CandidateRepairReceiptError, TypeError, ValueError) as exc:
                receipt_code = str(
                    getattr(exc, "code", None)
                    or "CANDIDATE_REPAIR_RECEIPT_INVALID"
                )
                return {"generate_outcome": GenerateOutcome(
                    schema_version=1,
                    type="internal_protocol_error",
                    summary=(
                        "Corrective Generate rejected an invalid provider-visible "
                        "candidate-repair receipt."
                    ),
                    scope=_scope(state),
                    payload={
                        "error_code": "GENERATE_REPAIR_RECEIPT_INVALID",
                        "receipt_error_code": receipt_code,
                        "component": "candidate_repair_prompt_projection",
                        "detail": str(getattr(exc, "detail", None) or exc)[:800],
                        "execution_status": "not_started",
                        "provider_call_attempted": False,
                    },
                ).to_dict()}
            generator_state[
                "_candidate_repair_receipt_for_projection"
            ] = dict(validated_projection_receipt)
        try:
            result = await self.generator(generator_state)
        except GraphBubbleUp:
            raise
        except asyncio.CancelledError:
            raise
        if not isinstance(result, Mapping):
            raise TypeError("Generate must return a mapping")
        value = dict(result)
        action = dict(value.get("pending_action") or {}) if isinstance(value.get("pending_action"), Mapping) else {}
        invocation = dict(value.get("generate_invocation") or {})
        invocation_scope = dict(invocation.get("scope") or {})
        scope = invocation_scope or _scope(state, action=action)
        conflicts = {}
        for label, other in (("action", action), ("receipt", dict(value.get("candidate_repair_receipt") or {}).get("scope") or {}),
                             ("rejection", dict(value.get("candidate_rejection_event") or {}).get("scope") or {})):
            for key in ("task_id", "task_revision", "plan_id", "plan_version", "plan_revision", "step_id"):
                if invocation_scope and other.get(key) not in (None, "") and str(other[key]) != str(invocation_scope.get(key, "")):
                    conflicts[label + "." + key] = {"expected": invocation_scope.get(key), "observed": other[key]}
        if conflicts:
            return {"generate_outcome": GenerateOutcome(schema_version=1, type="internal_protocol_error",
                summary="Generate action/receipt disagrees with its fixed invocation scope.", scope=scope,
                payload={"error_code": "GENERATE_INVOCATION_SCOPE_MISMATCH", "conflicts": conflicts,
                         "generate_invocation": invocation, "execution_status": "not_started"}).to_dict()}
        exit_reason = str(value.get("generate_exit_reason") or "").strip().lower()
        generate_result = str(value.get("generate_result") or "").strip().upper()
        evidence_refs = tuple(
            str(item) for item in value.get("evidence_refs", ()) or () if str(item)
        )
        skill_receipt_value = value
        durable_skill_error = ""
        if (
            self.data_path is not None
            and generate_result in {"SKILL_CONTINUATION", "SKILL_BINDING_PROPOSED"}
            and exit_reason not in {
                "skill_context_needed",
                "skill_decision_rejected",
            }
        ):
            try:
                durable = load_plan_state(
                    self.data_path, str(state.get("task_id") or "default")
                )
                task_state = dict(durable.get("task_state") or {})
                skill_receipt_value = {
                    **value,
                    "skill_access_state": (
                        task_state.get("skill_access_state")
                        or durable.get("skill_access_state")
                        or {}
                    ),
                    "skill_continuation_id": str(
                        task_state.get("skill_continuation_id") or ""
                    ),
                    "pending_skill_binding_proposal": dict(
                        task_state.get("pending_skill_binding_proposal") or {}
                    ),
                }
            except Exception as exc:
                durable_skill_error = f"{type(exc).__name__}: {exc}"[:800]

        if isinstance(value.get("cancellation_receipt"), Mapping):
            outcome_type = "cancelled"
            payload = {
                "cancellation_source": str(value.get("cancellation_source") or "user"),
                "cancellation_receipt": dict(value["cancellation_receipt"]),
            }
            summary = "Generate stopped on an authoritative cancellation receipt."
        elif exit_reason == "runtime_evidence_required":
            request = dict(value.get("pending_runtime_evidence_request") or {})
            outcome_type = "runtime_evidence_required"
            payload = {
                "request_id": request["request_id"], "episode_id": request["episode_id"],
                "step_id": request["scope"]["step_id"],
                "missing_binding_ids": list(request["missing_binding_ids"]),
                "request": request, "dispatch_state": "not_dispatched",
            }
            scope = {key: request["scope"][key] for key in ("task_id", "plan_id", "plan_version", "step_id")}
            summary = "Current capability requires runtime evidence before provider invocation."
        elif exit_reason == "runtime_recovery_stopped":
            outcome_type = "runtime_recovery_stopped"
            explanation = dict(value.get("runtime_recovery_result") or {})
            payload = {
                "reason_code": explanation["reason_code"],
                "resolution_disposition": explanation["resolution_disposition"],
                "explanation": explanation["explanation"],
                "execution_status": explanation["execution_status"],
                "original_error": explanation["original_error"],
            }
            summary = str(explanation["explanation"])
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
        elif exit_reason in {"current_step_missing", "plan_patch"}:
            outcome_type = "plan_revision_needed"
            payload = {
                "step_id": str(scope.get("step_id") or ""),
                "reason_code": exit_reason or "plan_revision_required",
                "contract_violations": list(value.get("validation_issues") or ()),
            }
            summary = "The approved Plan needs a structured revision."
        elif durable_skill_error:
            outcome_type = "internal_protocol_error"
            payload = {
                "error_code": "GENERATE_DURABLE_SKILL_RECEIPT_UNAVAILABLE",
                "component": "generate_skill_access",
                "detail": durable_skill_error,
            }
            summary = "Generate could not verify its durable Skill receipt."
        elif exit_reason == "skill_decision_rejected":
            rejection = dict(value.get("skill_decision_result") or {})
            outcome_type = "internal_protocol_error"
            payload = {
                "error_code": str(
                    rejection.get("code")
                    or rejection.get("error_code")
                    or "GENERATE_SKILL_DECISION_REJECTED"
                ),
                "component": "generate_skill_access",
                "detail": str(rejection.get("detail") or "")[:800],
            }
            summary = "Generate emitted an invalid typed Skill decision."
        elif generate_result == "SKILL_NO_PROGRESS" or exit_reason == "skill_no_progress":
            no_progress = dict(value.get("skill_decision_result") or {})
            outcome_type = "internal_protocol_error"
            payload = {
                "error_code": str(
                    no_progress.get("code")
                    or no_progress.get("status")
                    or "GENERATE_SKILL_NO_PROGRESS"
                ),
                "component": "generate_skill_access",
                "detail": (
                    "The current Generate invocation exhausted its bounded "
                    "Skill observation budget without new evidence."
                ),
            }
            summary = "Generate exhausted its bounded Skill-observation loop."
        elif generate_result == "SKILL_BINDING_PROPOSED" or exit_reason == "skill_binding_proposed":
            proposal_receipt = dict(value.get("skill_decision_result") or {})
            source_proposal = dict(
                value.get("pending_skill_binding_proposal") or {}
            )
            proposal = dict(
                skill_receipt_value.get("pending_skill_binding_proposal") or {}
            )
            proposal_id = str(proposal.get("proposal_id") or "")
            receipt_proposal_id = str(proposal_receipt.get("proposal_id") or "")
            candidate_skill_id = str(proposal.get("candidate_skill_id") or "")
            requested_scope = str(proposal.get("requested_scope") or "")
            relation = str(proposal.get("relation") or "")
            candidate_snapshot_id = str(
                proposal.get("candidate_snapshot_id") or ""
            )
            caller_invocation_id = str(
                proposal.get("caller_invocation_id") or ""
            )
            verified_excerpt_refs = [
                str(item)
                for item in proposal.get("verified_excerpt_refs") or []
                if str(item)
            ]
            access = skill_receipt_value.get("skill_access_state")
            binding_proposals = (
                access.get("binding_proposals")
                if isinstance(access, Mapping)
                else None
            )
            stored_proposal = (
                dict(binding_proposals.get(proposal_id) or {})
                if isinstance(binding_proposals, Mapping) and proposal_id
                else {}
            )
            direct_plan_impact = bool(
                str(proposal_receipt.get("status") or "").lower()
                == "pending_plan_impact_review"
                or str(proposal_receipt.get("review_route") or "")
                == "planner_plan_impact_review"
                or str(proposal.get("review_route") or "")
                == "planner_plan_impact_review"
            )
            if direct_plan_impact:
                outcome_type = "internal_protocol_error"
                payload = {
                    "error_code": "GENERATE_DIRECT_PLAN_IMPACT_FORBIDDEN",
                    "component": "generate_skill_access",
                    "detail": (
                        "Generate cannot bypass Consultant applicability review "
                        "for a new or replacement Skill binding."
                    ),
                }
                summary = "Generate returned an impossible direct plan-impact proposal."
            elif not (
                all((
                    proposal_id,
                    candidate_skill_id,
                    requested_scope,
                    relation,
                    candidate_snapshot_id,
                    caller_invocation_id,
                ))
                and verified_excerpt_refs
                and all(
                    key in proposal
                    for key in (
                        "base_task_revision",
                        "base_plan_revision",
                        "base_user_turn_revision",
                    )
                )
            ):
                outcome_type = "internal_protocol_error"
                payload = {
                    "error_code": "GENERATE_SKILL_BINDING_RECEIPT_INVALID",
                    "component": "generate_skill_access",
                    "detail": (
                        "A new Skill binding may enter Consultant review only "
                        "with a complete identity-bound proposal receipt."
                    ),
                }
                summary = "Generate returned an incomplete Skill binding proposal."
            elif (
                receipt_proposal_id != proposal_id
                or _fingerprint(source_proposal) != _fingerprint(proposal)
                or str(proposal_receipt.get("decision_result") or "")
                != "SKILL_BINDING_PROPOSAL_RECORDED"
                or str(proposal_receipt.get("status") or "").lower()
                != "pending_consultant_review"
                or str(proposal_receipt.get("review_route") or "")
                != "consultant_skill_resolution"
                or not stored_proposal
                or _fingerprint(stored_proposal) != _fingerprint(proposal)
                or str(stored_proposal.get("caller_node") or "")
                not in _GENERATE_SKILL_CALLERS
                or str(stored_proposal.get("status") or "") != "pending_review"
                or str(stored_proposal.get("review_route") or "")
                != "consultant_skill_resolution"
            ):
                outcome_type = "internal_protocol_error"
                payload = {
                    "error_code": "GENERATE_SKILL_BINDING_IDENTITY_MISMATCH",
                    "component": "generate_skill_access",
                    "detail": (
                        "The proposal pointer, canonical record, and Consultant "
                        "review receipt do not share one Generate-owned identity."
                    ),
                }
                summary = "Generate returned an unbound Skill proposal receipt."
            else:
                outcome_type = "skill_binding_review_needed"
                payload = {
                    "step_id": str(scope.get("step_id") or ""),
                    "proposal_id": proposal_id,
                    "candidate_skill_id": candidate_skill_id,
                    "requested_scope": requested_scope,
                    "relation": relation,
                    "proposal_fingerprint": _fingerprint({
                        "proposal": proposal,
                        "receipt": proposal_receipt,
                    }),
                }
                summary = "A new or replacement Skill binding requires Consultant review."
        elif exit_reason == "skill_context_needed":
            # The old exit label does not distinguish a served observation
            # from a new-Skill proposal.  Schema 2 must not reconstruct that
            # missing semantic fact or silently send the caller to Consultant.
            outcome_type = "internal_protocol_error"
            payload = {
                "error_code": "GENERATE_LEGACY_SKILL_CONTEXT_AMBIGUOUS",
                "component": "generate_skill_access",
                "detail": (
                    "Generate must emit an identity-bound context, search, or "
                    "new-Skill proposal receipt."
                ),
            }
            summary = "Generate returned an ambiguous legacy Skill-context exit."
        elif generate_result == "SKILL_CONTINUATION":
            continuation = dict(value.get("skill_decision_result") or {})
            decision_result = str(continuation.get("decision_result") or "").upper()
            continuation_id = str(continuation.get("continuation_id") or "")
            result_status = str(continuation.get("status") or "")
            if decision_result == "SKILL_CONTEXT_RESULT":
                request_id = str(continuation.get("request_id") or "")
                receipt_issue = _skill_continuation_receipt_issue(
                    skill_receipt_value,
                    continuation,
                    expected_request_kind="REQUEST_SKILL_CONTEXT",
                    allowed_result_statuses=_SKILL_CONTEXT_READY_STATUSES,
                )
                if not all((continuation_id, request_id, result_status)):
                    outcome_type = "internal_protocol_error"
                    payload = {
                        "error_code": "GENERATE_SKILL_CONTEXT_RECEIPT_INVALID",
                        "component": "generate_skill_access",
                        "detail": (
                            "Served Skill context is missing its continuation, "
                            "request, or result-status identity."
                        ),
                    }
                    summary = "Generate received an incomplete Skill context receipt."
                elif receipt_issue is not None:
                    outcome_type = "internal_protocol_error"
                    payload = {
                        "error_code": receipt_issue[0],
                        "component": "generate_skill_access",
                        "detail": receipt_issue[1],
                    }
                    summary = "Generate received an unbound Skill context receipt."
                else:
                    outcome_type = "skill_context_served"
                    payload = {
                        "step_id": str(scope.get("step_id") or ""),
                        "continuation_id": continuation_id,
                        "request_id": request_id,
                        "result_status": result_status,
                        "continuation_fingerprint": _fingerprint(continuation),
                    }
                    evidence_refs = tuple(dict.fromkeys((
                        *evidence_refs,
                        *(
                            str(item.get("excerpt_id") or "")
                            for item in continuation.get("results") or []
                            if isinstance(item, Mapping)
                            and str(item.get("excerpt_id") or "")
                        ),
                    )))
                    summary = "Requested immutable Skill context is ready for Generate."
            elif decision_result == "SKILL_SEARCH_RESULT":
                search_id = str(continuation.get("search_id") or "")
                receipt_issue = _skill_continuation_receipt_issue(
                    skill_receipt_value,
                    continuation,
                    expected_request_kind="SEARCH_SKILLS",
                    allowed_result_statuses=_SKILL_SEARCH_READY_STATUSES,
                )
                if not all((continuation_id, search_id, result_status)):
                    outcome_type = "internal_protocol_error"
                    payload = {
                        "error_code": "GENERATE_SKILL_SEARCH_RECEIPT_INVALID",
                        "component": "generate_skill_access",
                        "detail": (
                            "A Skill search observation is missing its continuation, "
                            "search, or result-status identity."
                        ),
                    }
                    summary = "Generate received an incomplete Skill search receipt."
                elif receipt_issue is not None:
                    outcome_type = "internal_protocol_error"
                    payload = {
                        "error_code": receipt_issue[0],
                        "component": "generate_skill_access",
                        "detail": receipt_issue[1],
                    }
                    summary = "Generate received an unbound Skill search receipt."
                else:
                    candidates = [
                        dict(item)
                        for item in continuation.get("candidates") or []
                        if isinstance(item, Mapping)
                    ]
                    outcome_type = "skill_search_result_ready"
                    payload = {
                        "step_id": str(scope.get("step_id") or ""),
                        "continuation_id": continuation_id,
                        "search_id": search_id,
                        "result_status": result_status,
                        "candidate_count": len(candidates),
                        "continuation_fingerprint": _fingerprint(continuation),
                    }
                    summary = "A bounded local Skill search observation is ready for Generate."
            else:
                outcome_type = "internal_protocol_error"
                payload = {
                    "error_code": "GENERATE_SKILL_CONTINUATION_RESULT_AMBIGUOUS",
                    "component": "generate_skill_access",
                    "detail": (
                        "SKILL_CONTINUATION must carry either a Skill context "
                        "or Skill search result receipt."
                    ),
                }
                summary = "Generate returned an ambiguous Skill continuation."
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
        elif exit_reason in _REPAIRABLE_GENERATE_EXITS:
            raw_receipt = value.get("candidate_repair_receipt")
            if not isinstance(raw_receipt, Mapping) or not raw_receipt:
                outcome_type = "internal_protocol_error"
                payload = _repair_handoff_failure_payload(
                    value,
                    exit_reason=exit_reason,
                    error_code="GENERATE_REPAIR_RECEIPT_MISSING",
                    detail=(
                        "A repairable Generate exit did not carry its trusted "
                        "candidate-repair receipt. Legacy action_format_repair "
                        "state is not a receipt."
                    ),
                )
                summary = "Generate declared a repairable exit without a candidate-repair receipt."
            else:
                try:
                    durable_scope, committed_repair_state = _durable_repair_state(
                        state,
                        data_path=self.data_path,
                    )
                    repair = validate_candidate_repair_receipt(
                        raw_receipt,
                        data_path=self.data_path,
                        task_id=str(state.get("task_id") or "unknown"),
                        expected_scope=durable_scope,
                        expected_candidate_fingerprint=str(
                            value.get("candidate_fingerprint") or ""
                        ),
                        committed_repair_state=committed_repair_state,
                    )
                except CandidateRepairReceiptError as exc:
                    outcome_type = "internal_protocol_error"
                    payload = _repair_handoff_failure_payload(
                        value,
                        exit_reason=exit_reason,
                        error_code="GENERATE_REPAIR_RECEIPT_INVALID",
                        detail=f"{exc.code}: {exc.detail}"[:1200],
                    )
                    payload["receipt_error_code"] = exc.code
                    summary = "Generate supplied an invalid candidate-repair receipt."
                else:
                    candidate_fingerprint = str(repair["candidate_fingerprint"])
                    error_fingerprint = str(repair["error_fingerprint"])
                    ordinal = int(repair["repair_ordinal"])
                    receipt_limit = int(repair["repair_limit"])
                    effective_limit = min(self.repair_limit, receipt_limit)
                    progress_proven = repair.get("progress_proven") is True
                    if not progress_proven or ordinal > effective_limit:
                        outcome_type = "internal_protocol_error"
                        payload = {
                            "error_code": "GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED" if not progress_proven else "GENERATE_REPAIR_BUDGET_EXHAUSTED",
                            "component": "typed_generate_repair_governance",
                            "candidate_fingerprint": candidate_fingerprint,
                            "error_fingerprint": error_fingerprint,
                            "primary_cause": {
                                "error_code": str(value.get("reason_code") or exit_reason),
                                "stage": "pre_execution_candidate_admission",
                                "execution_started": False,
                            },
                            "execution_status": "not_started",
                            "recovery_entered": False,
                            "outputs_produced": [],
                        }
                        summary = "Corrective Generate failed closed after non-progress or budget exhaustion."
                    else:
                        outcome_type = "candidate_rejected"
                        payload = {
                            "step_id": str(repair["scope"]["step_id"]),
                            "violation_owner": str(repair["violation_owner"]),
                            "candidate_fingerprint": candidate_fingerprint,
                            "error_fingerprint": error_fingerprint,
                            "feedback": str(repair["feedback"]),
                            "budget_receipt": {
                                "ordinal": ordinal,
                                "limit": effective_limit,
                                "remaining": effective_limit - ordinal,
                                "progress_proven": progress_proven,
                                "rejection_event_id": str(repair["rejection_event_id"]),
                                "receipt_ref": str(repair["receipt_ref"]),
                            },
                            "repairable": True,
                            "repair_context": route_blind_state_projection(repair),
                            "execution_status": "not_started",
                        }
                        summary = "A bounded generated candidate defect can be corrected."
        elif exit_reason in _INTERNAL_GENERATE_EXITS or str(value.get("recovery_status") or "").upper() == "INTERNAL_PROTOCOL_ERROR":
            outcome_type = "internal_protocol_error"
            payload = _internal_generate_failure_payload(
                value,
                exit_reason=exit_reason,
            )
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

        if invocation_scope:
            payload["generate_invocation"] = invocation
        outcome = GenerateOutcome(
            schema_version=1,
            type=outcome_type,
            summary=summary,
            scope=scope,
            payload=payload,
            evidence_refs=evidence_refs,
        )
        if outcome_type in {"runtime_evidence_required", "runtime_recovery_stopped"}:
            return {
                "generate_outcome": outcome.to_dict(),
                "pending_runtime_evidence_request": dict(value.get("pending_runtime_evidence_request") or {}),
                "runtime_recovery_result": dict(value.get("runtime_recovery_result") or {}),
                "messages": list(value.get("messages") or []),
                **{key: value[key] for key in ("pending_action", "execution_started") if key in value},
            }
        return {"generate_outcome": outcome.to_dict()}


class TypedExecute:
    """Translate exactly one Execute invocation into an ExecuteOutcome."""

    def __init__(
        self,
        *,
        executor: NodeCallable,
        data_path: str | None = None,
    ) -> None:
        self.executor = executor
        self.data_path = str(data_path) if data_path is not None else None

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

        terminal_failure_envelope = (
            dict(value.get("terminal_internal_protocol_failure") or {})
            if isinstance(value.get("terminal_internal_protocol_failure"), Mapping)
            else {}
        )
        lineage_primary_failure = dict(
            terminal_failure_envelope.get("primary_failure") or {}
        )
        current_terminal_failure = dict(
            terminal_failure_envelope.get("terminal_failure") or {}
        )
        # The lineage primary intentionally preserves the first failure.  The
        # terminal failure is the exception that ended this Execute invocation
        # and therefore owns the typed phase/detail projection.
        failure = current_terminal_failure or lineage_primary_failure
        if failure and failure.get("command_started") is False:
            secondary_failures = []
            for item in terminal_failure_envelope.get(
                "secondary_failures", []
            ):
                if not isinstance(item, Mapping):
                    continue
                candidate = dict(item)
                candidate_id = str(candidate.get("failure_id") or "")
                failure_id = str(failure.get("failure_id") or "")
                if (
                    candidate == failure
                    or (
                        bool(candidate_id and failure_id)
                        and candidate_id == failure_id
                    )
                ):
                    continue
                secondary_failures.append(candidate)
            failure_diagnostic = (
                dict(failure.get("exception_diagnostic") or {})
                if isinstance(failure.get("exception_diagnostic"), Mapping)
                else {}
            )
            detail = redact_text(
                failure.get("detail")
                or failure_diagnostic.get("detail")
                or failure.get("message")
                or ""
            )[:1200]
            outcome_type = "harness_error"
            payload = {
                "error_code": str(failure.get("error_code") or "EXECUTE_PRELAUNCH_HARNESS_ERROR"),
                "exception_class": str(failure.get("exception_type") or "InternalError"),
                "phase": str(failure.get("phase") or failure.get("stage") or "prelaunch"),
                "component": str(failure.get("component") or "execute"),
                "failed_event_type": str(
                    failure.get("failed_event_type") or ""
                ),
                "attempt_id": str(failure.get("attempt_id") or ""),
                "last_successful_phase": str(
                    failure.get("last_successful_phase") or ""
                ),
                "diagnostic_ref": str(failure.get("diagnostic_ref") or ""),
                "detail": detail,
                "command_started": False,
                "execution_status": "not_started",
                "primary_cause": failure,
                "lineage_primary_cause": lineage_primary_failure,
                "secondary_causes": secondary_failures,
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
                rejection.get("violation_owner")
                or rejection.get("failure_owner")
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
            candidate_owned = violation_owner.strip().upper() in {
                "GENERATED_ACTION", "CANDIDATE", "MODEL",
            }
            if candidate_owned:
                raw_receipt = value.get("candidate_repair_receipt")
                try:
                    if not isinstance(raw_receipt, Mapping) or not raw_receipt:
                        raise CandidateRepairReceiptError(
                            "EXECUTE_REPAIR_RECEIPT_MISSING"
                        )
                    durable_scope, committed_repair_state = _durable_repair_state(
                        state,
                        data_path=self.data_path,
                    )
                    repair = validate_candidate_repair_receipt(
                        raw_receipt,
                        data_path=self.data_path,
                        task_id=str(state.get("task_id") or "unknown"),
                        expected_scope=durable_scope,
                        expected_candidate_fingerprint=str(
                            value.get("candidate_fingerprint")
                            or rejection.get("candidate_fingerprint")
                            or ""
                        ),
                        committed_repair_state=committed_repair_state,
                    )
                except CandidateRepairReceiptError as exc:
                    outcome_type = "harness_error"
                    payload = {
                        "error_code": (
                            "EXECUTE_REPAIR_RECEIPT_MISSING"
                            if exc.code == "EXECUTE_REPAIR_RECEIPT_MISSING"
                            else "EXECUTE_REPAIR_RECEIPT_INVALID"
                        ),
                        "exception_class": type(exc).__name__,
                        "phase": "typed_execute_candidate_repair_handoff",
                        "command_started": False,
                        "detail": f"{exc.code}: {exc.detail}"[:1200],
                    }
                    summary = (
                        "Execute candidate repair failed closed because its trusted "
                        "receipt was missing or invalid."
                    )
                else:
                    ordinal = int(repair["repair_ordinal"])
                    limit = int(repair["repair_limit"])
                    outcome_type = "pre_execution_rejected"
                    payload = {
                        "violation_owner": str(repair["violation_owner"]),
                        "violation_code": violation_code,
                        "candidate_fingerprint": str(
                            repair["candidate_fingerprint"]
                        ),
                        "command_started": False,
                        "budget_receipt": {
                            "ordinal": ordinal,
                            "limit": limit,
                            "remaining": limit - ordinal,
                            "progress_proven": bool(repair["progress_proven"]),
                            "rejection_event_id": str(
                                repair["rejection_event_id"]
                            ),
                            "receipt_ref": str(repair["receipt_ref"]),
                        },
                        "feedback": str(repair["feedback"]),
                        "error_fingerprint": str(repair["error_fingerprint"]),
                        "repair_context": route_blind_state_projection(repair),
                    }
                    summary = (
                        "Execute rejected the candidate before command launch and "
                        "committed a trusted corrective-Generate receipt."
                    )
            else:
                outcome_type = "pre_execution_rejected"
                payload = {
                    "violation_owner": violation_owner,
                    "violation_code": violation_code,
                    "candidate_fingerprint": str(
                        action.get("candidate_fingerprint") or _fingerprint(action)
                    ),
                    "command_started": False,
                    "budget_receipt": dict(
                        value.get("budget_receipt") or {"consumed": False}
                    ),
                    "feedback": str(
                        rejection.get("feedback")
                        or "The current action cannot launch until its owner repairs the preflight condition."
                    ),
                    "error_fingerprint": str(
                        rejection.get("error_fingerprint")
                        or _fingerprint({
                            "violation_owner": violation_owner,
                            "violation_code": violation_code,
                            "evidence": rejection.get("evidence") or {},
                        })
                    ),
                    "repair_context": route_blind_state_projection({
                        "repair_stage": "execute_preflight",
                        "exact_violations": [dict(rejection)],
                    }),
                }
                summary = "Execute rejected the action before command launch."
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
            evidence_refs=tuple(dict.fromkeys(
                str(item)
                for item in [
                    *(execution.get("evidence_refs", ()) or ()),
                    failure.get("diagnostic_ref") if failure else "",
                ]
                if str(item)
            )),
        )
        # Execute has consumed or retired the action on every typed exit.  The
        # durable PlanState is authoritative, but clearing the graph-local
        # projection here prevents a successor node from observing the stale
        # Generate action.  Installation metadata remains single-sourced in
        # the typed outcome payload consumed by InstallSubagent.
        return {
            "execute_outcome": outcome.to_dict(),
            "pending_action": {},
        }


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

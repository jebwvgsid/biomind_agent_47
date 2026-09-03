"""Route-blind adapters for the schema-2 front half.

The legacy implementations remain intact for schema-1 checkpoints.  These
adapters reuse their domain side effects, discard every legacy route
projection, and publish one strict node-specific semantic outcome.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

from agent_core.consultant_binding import consultant_planning_readiness
from agent_core.plan_tracker import (
    has_materialized_plan,
    load_plan_state,
    task_spec_is_initialized,
    task_status,
)
from agent_core.human_io import load_waiting_state
from agent_core.workflow.outcomes import (
    AskUserOutcome,
    ConsultantOutcome,
    InstallSubagentOutcome,
    PlannerOutcome,
    PlanReviewOutcome,
    ResumeState,
    StrictOutcome,
    TriageOutcome,
    route_blind_state_projection,
)


SourceNode = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]

_LEGACY_ROUTE_PROJECTIONS = frozenset({
    "next_step", "next_route", "target_route", "goto", "proposed_route",
    "effective_route", "dispatch_route", "route_resolution",
    "node_recommended_next_step", "required_next_action",
    "pending_recovery_action", "resume_route_hint", "resolved_resume_node",
    "resume_step", "terminal_route",
})


def _scrub_legacy_routes(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_legacy_routes(item)
            for key, item in value.items()
            if str(key).lower() not in _LEGACY_ROUTE_PROJECTIONS
        }
    if isinstance(value, list):
        return [_scrub_legacy_routes(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_legacy_routes(item) for item in value)
    return value


def _clean_update(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _scrub_legacy_routes(dict(raw))


def _scope(state: Mapping[str, Any], durable: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(state.get("task_id") or durable.get("task_id") or "default"),
        "plan_id": str(durable.get("plan_id") or ""),
        "plan_version": str(durable.get("plan_version") or ""),
        "step_id": str(durable.get("current_step_id") or state.get("current_step_id") or ""),
    }


def _outcome(
    contract: type[StrictOutcome],
    outcome_type: str,
    state: Mapping[str, Any],
    durable: Mapping[str, Any],
    payload: Mapping[str, Any],
    summary: str,
) -> dict[str, Any]:
    return contract(
        schema_version=1,
        type=outcome_type,
        summary=summary,
        scope=_scope(state, durable),
        payload=dict(payload),
    ).to_dict()


def _semantic_harness_error(
    contract: type[StrictOutcome],
    state: Mapping[str, Any],
    durable: Mapping[str, Any],
    *,
    error_code: str,
    detail: str,
) -> dict[str, Any]:
    return _outcome(
        contract,
        "harness_error",
        state,
        durable,
        {
            "error_code": error_code,
            "exception_class": "SemanticCoverageError",
            "detail": detail,
        },
        "The typed adapter failed closed on ambiguous semantic state.",
    )


def early_harness_error(
    contract: type[StrictOutcome], exc: Exception, state: Mapping[str, Any]
) -> StrictOutcome:
    return contract(
        schema_version=1,
        type="harness_error",
        summary="Front-half node harness failed closed.",
        scope={"task_id": str(state.get("task_id") or "default")},
        payload={
            "error_code": "FRONT_HALF_NODE_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "detail": str(exc)[:800],
        },
    )


class _TypedEarlyNode:
    outcome_field = ""
    contract: type[StrictOutcome]

    def __init__(self, *, data_path: str, source: SourceNode) -> None:
        self.data_path = data_path
        self.source = source

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state = route_blind_state_projection(state)
        raw = await self.source(state)
        if not isinstance(raw, Mapping):
            raise TypeError(f"front-half source returned {type(raw).__name__}")
        durable = load_plan_state(
            self.data_path, str(state.get("task_id") or "default")
        )
        clean = _clean_update(raw)
        merged = {**dict(state), **clean}
        clean[self.outcome_field] = self.classify(state, merged, durable)
        return clean

    def classify(
        self,
        before: Mapping[str, Any],
        state: Mapping[str, Any],
        durable: Mapping[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


class TypedTriage(_TypedEarlyNode):
    outcome_field = "triage_outcome"
    contract = TriageOutcome

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state = route_blind_state_projection(state)
        raw_resume = state.get("resume_state")
        if raw_resume:
            resume = (
                raw_resume
                if isinstance(raw_resume, ResumeState)
                else ResumeState.from_mapping(raw_resume)
            )
            durable = load_plan_state(
                self.data_path, str(state.get("task_id") or "default")
            )
            return {
                "triage_outcome": _outcome(
                    TriageOutcome,
                    "resume_classified",
                    state,
                    durable,
                    {
                        "semantic_state": resume.type,
                        "reason_code": str(resume.payload.get("reason_code") or "semantic_resume"),
                    },
                    "Durable task state was classified semantically for resume.",
                )
            }
        return await super().run(state)

    def classify(self, before, state, durable):
        mode = str(state.get("task_mode") or "chat").lower()
        request_kind = str((state.get("request_intent") or {}).get("request_kind") or "question")
        if mode == "chat":
            kind = "direct_response_ready"
        elif mode in {"readonly", "simple_execute"}:
            kind = "step_execution_ready"
        elif mode == "managed":
            kind = "consultation_ready"
        else:
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="TRIAGE_MODE_INVALID",
                detail=f"Unsupported task_mode: {mode!r}",
            )
        return _outcome(
            TriageOutcome, kind, state, durable,
            {"task_mode": mode, "request_kind": request_kind},
            f"Triage classified the request as {mode}.",
        )


class TypedConsultant(_TypedEarlyNode):
    outcome_field = "consultant_outcome"
    contract = ConsultantOutcome

    def classify(self, before, state, durable):
        task_state = dict(durable.get("task_state") or {})
        # Intermediate Consultant actions (for example SEARCH_SKILLS and
        # LOAD_SKILL_DOC) advance the LangGraph consultation state without
        # committing a canonical TaskSpec yet.  That node-local semantic
        # result is authoritative for deciding whether consultation continues;
        # plan_state remains authoritative for the final Planner handoff.
        current_consultation = state.get("consultation_state")
        consultation = dict(
            current_consultation
            if isinstance(current_consultation, Mapping) and current_consultation
            else task_state.get("consultation_state") or {}
        )
        pending = dict(
            state.get("pending_human_question")
            or task_state.get("pending_question")
            or consultation.get("pending_question")
            or consultation.get("pending_human_question")
            or {}
        )
        if pending:
            question = str(pending.get("question") or "").strip()
            question_id = str(pending.get("question_id") or "semantic-question")
            return _outcome(self.contract, "user_input_needed", state, durable, {
                "question_id": question_id, "question": question or "User input is required.",
                "owner": str(pending.get("owner") or "user"),
            }, "Consultation requires authoritative user input.")
        stale = dict(state.get("stale_node_result") or {})
        if stale:
            return _outcome(self.contract, "stale_result", state, durable, {
                "changed_preconditions": list(stale.get("changed_preconditions") or []),
            }, "Consultation result became stale before commit.")
        status = str(state.get("task_status") or task_status(durable) or "").lower()
        contract_status = str(state.get("contract_status") or "")
        if status in {"blocked", "failed", "cancelled", "completed"} or contract_status.startswith("blocked_"):
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status or "blocked", "reason_code": contract_status or "consultation_terminal",
            }, "Consultation reached a terminal business state.")
        readiness = consultant_planning_readiness(durable)
        if readiness["ready"]:
            return _outcome(self.contract, "planning_ready", state, durable, {
                "task_revision": int(readiness["task_revision"]),
                "contract_status": contract_status or str(readiness["contract_status"]),
            }, "Consultation produced a planning-ready semantic contract.")
        phase = str(consultation.get("phase") or "").lower()
        consultation_status = str(consultation.get("consultation_status") or "").lower()
        completion = str(consultation.get("completion_decision") or "").lower()
        explicit_continuation = bool(
            phase in {"consulting", "finalize", "skill_discovery"}
            and consultation_status in {"active", "pending", "in_progress"}
            and completion not in {"ready_to_finalize", "ready_to_plan", "completed"}
        )
        if explicit_continuation:
            return _outcome(self.contract, "consultation_continues", state, durable, {
                "phase": phase,
                "completion_decision": completion or "needs_evidence",
            }, "Authoritative consultation lifecycle proves more convergence work is required.")
        return _semantic_harness_error(
            self.contract,
            state,
            durable,
            error_code="CONSULTANT_SEMANTIC_STATE_AMBIGUOUS",
            detail=str(readiness["reason_code"]),
        )


class TypedPlanner(_TypedEarlyNode):
    outcome_field = "planner_outcome"
    contract = PlannerOutcome

    def classify(self, before, state, durable):
        status = str(state.get("task_status") or task_status(durable) or "").lower()
        if status in {"blocked", "failed", "cancelled", "completed", "paused_retryable"}:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status, "reason_code": "planning_terminal",
            }, "Planner reached a terminal business state.")
        stale = dict(state.get("stale_node_result") or {})
        if stale:
            return _outcome(self.contract, "stale_result", state, durable, {
                "changed_preconditions": list(stale.get("changed_preconditions") or []),
            }, "Planner result became stale before commit.")
        readiness = consultant_planning_readiness(durable)
        if not readiness["ready"] and not has_materialized_plan(durable):
            return _outcome(self.contract, "consultation_required", state, durable, {
                "reason_code": str(readiness["reason_code"]),
            }, "Planner requires a finalized semantic contract.")
        issues = [
            dict(item)
            for item in durable.get("validation_issues") or state.get("validation_issues") or []
            if isinstance(item, Mapping)
            and str(item.get("severity") or "").lower() in {"error", "blocking", "fatal"}
        ]
        owners = {str(item.get("owner") or "internal").lower() for item in issues}
        if has_materialized_plan(durable) and not issues:
            return _outcome(self.contract, "plan_review_ready", state, durable, {
                "plan_id": str(durable.get("plan_id") or "pending-plan"),
                "plan_version": str(durable.get("plan_version") or durable.get("plan_revision") or "1"),
                "plan_revision": int(durable.get("plan_revision") or 0),
            }, "Planner produced a reviewable plan.")
        if owners & {"task_spec", "external_input", "consultant"}:
            return _outcome(self.contract, "consultation_required", state, durable, {
                "reason_code": "plan_validation_requires_semantic_reconsultation",
            }, "Planner validation requires authoritative semantic correction.")
        task_state = dict(durable.get("task_state") or {})
        planning_failure = dict(task_state.get("planning_failure") or {})
        planning_epoch = dict(task_state.get("planning_epoch") or state.get("planning_epoch") or {})
        explicit_continuation = bool(
            (
                planning_failure
                and str(planning_failure.get("status") or "").lower()
                in {"active", "pending", "unresolved", "repairing"}
            )
            or (
                str(planning_epoch.get("status") or "").lower() == "active"
                and int(planning_epoch.get("planner_calls_total") or 0) > 0
            )
        )
        if explicit_continuation:
            attempt = int(
                planning_epoch.get("planner_calls_total")
                or planning_failure.get("attempt")
                or 1
            )
            return _outcome(self.contract, "planning_continues", state, durable, {
                "reason_code": str(
                    planning_failure.get("validation_source")
                    or "durable_planning_epoch_active"
                ),
                "attempt_ordinal": attempt,
            }, "Durable planning convergence evidence requires another bounded turn.")
        return _semantic_harness_error(
            self.contract,
            state,
            durable,
            error_code="PLANNER_SEMANTIC_STATE_AMBIGUOUS",
            detail="No committed Plan and no durable planning-progress receipt.",
        )


class TypedPlanReview(_TypedEarlyNode):
    outcome_field = "plan_review_outcome"
    contract = PlanReviewOutcome

    def __init__(self, *, data_path: str, source: SourceNode, execution_backend: str) -> None:
        super().__init__(data_path=data_path, source=source)
        self.execution_backend = str(execution_backend or "legacy")

    def classify(self, before, state, durable):
        waiting = load_waiting_state(
            self.data_path, str(state.get("task_id") or "default")
        )
        if str(waiting.get("stage") or "") in {"plan_review", "plan_review_feedback"}:
            return _outcome(self.contract, "approval_pending", state, durable, {
                "approval_scope": dict(waiting.get("payload") or {}),
                "reason_code": "human_plan_review_pending",
            }, "Plan review is durably waiting for human approval.")
        pending = dict(state.get("pending_human_question") or {})
        if pending:
            return _outcome(self.contract, "user_input_needed", state, durable, {
                "question_id": str(pending.get("question_id") or "plan-review-question"),
                "question": str(pending.get("question") or "Plan review needs user input."),
                "owner": str(pending.get("owner") or "user"),
            }, "Plan review requires authoritative user input.")
        status = str(state.get("task_status") or task_status(durable) or "").lower()
        if status in {"blocked", "failed", "cancelled", "completed"}:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status, "reason_code": "plan_review_terminal",
            }, "Plan review reached a terminal business state.")
        task_state = dict(durable.get("task_state") or {})
        if task_state.get("pending_review_classification"):
            return _outcome(self.contract, "reconsultation_required", state, durable, {
                "reason_code": "review_feedback_needs_semantic_classification",
            }, "Plan-review feedback requires Consultant classification.")
        approval = str(
            durable.get("approval_status")
            or (durable.get("current_approval") or {}).get("status")
            or ""
        ).lower()
        if approval in {"approved", "accepted"} and has_materialized_plan(durable):
            backend = "external_worker" if self.execution_backend != "legacy" else "in_process"
            return _outcome(self.contract, "approved_for_execution", state, durable, {
                "plan_id": str(durable.get("plan_id") or "approved-plan"),
                "plan_version": str(durable.get("plan_version") or durable.get("plan_revision") or "1"),
                "approval_status": approval,
                "execution_backend": backend,
            }, "Plan review approved the plan for execution.")
        if not has_materialized_plan(durable):
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="PLAN_REVIEW_PLAN_NOT_MATERIALIZED",
                detail="PlanReview has neither a committed PlanSpec nor PlanSnapshot.",
            )
        issues = [dict(item) for item in state.get("validation_issues") or durable.get("validation_issues") or [] if isinstance(item, Mapping)]
        owners = {str(item.get("owner") or "") for item in issues}
        if owners & {"task_spec", "external_input", "consultant"}:
            return _outcome(self.contract, "reconsultation_required", state, durable, {
                "reason_code": "semantic_contract_validation_failed",
            }, "Plan review found a semantic-contract issue.")
        return _outcome(self.contract, "revision_required", state, durable, {
            "validation_issues": issues,
            "reason_code": "plan_not_approved",
        }, "Plan review requires a planner revision.")


class TypedAskUser(_TypedEarlyNode):
    outcome_field = "ask_user_outcome"
    contract = AskUserOutcome

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state = route_blind_state_projection(state)
        pending = deepcopy(dict(state.get("pending_human_question") or {}))
        raw = await self.source(state)
        if not isinstance(raw, Mapping):
            raise TypeError(f"ask-user source returned {type(raw).__name__}")
        durable = load_plan_state(self.data_path, str(state.get("task_id") or "default"))
        clean = _clean_update(raw)
        if not pending.get("question"):
            kind = "question_missing"
            payload = {"semantic_state": "consultation_ready", "reason_code": "question_missing"}
            summary = "No durable human question was available."
        else:
            answered = dict(
                (clean.get("consultation_state") or {}).get("pending_question")
                or (clean.get("consultation_state") or {}).get("pending_human_question")
                or {}
            )
            answer_matches = bool(
                str(answered.get("status") or "") == "answered_pending_interpretation"
                and str(answered.get("answer_turn_ref") or "").strip()
                and str(answered.get("question_id") or pending.get("question_id") or "")
                == str(pending.get("question_id") or "")
            )
            if not answer_matches:
                clean[self.outcome_field] = _semantic_harness_error(
                    self.contract,
                    state,
                    durable,
                    error_code="ASK_USER_ANSWER_RECEIPT_MISSING",
                    detail="AskUser returned without a matching durable answer-turn receipt.",
                )
                return clean
            semantic = "planning_ready" if str(pending.get("type") or "") == "plan_impact_review" else "consultation_ready"
            kind = "answer_recorded"
            payload = {
                "question_id": str(pending.get("question_id") or "semantic-question"),
                "semantic_state": semantic,
                "answer_recorded": True,
            }
            summary = "The authoritative user answer was recorded exactly once."
        clean[self.outcome_field] = _outcome(self.contract, kind, state, durable, payload, summary)
        return clean


class TypedInstallSubagent(_TypedEarlyNode):
    outcome_field = "install_subagent_outcome"
    contract = InstallSubagentOutcome

    def classify(self, before, state, durable):
        status = str(state.get("task_status") or task_status(durable) or "").lower()
        if status in {"blocked", "failed", "cancelled"}:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status, "reason_code": "installation_terminal",
            }, "Installation reached a terminal business state.")
        runtime = dict(state.get("runtime_status") or {})
        records = [dict(item) for item in runtime.values() if isinstance(item, Mapping)]
        current_step_id = str(durable.get("current_step_id") or "")
        current_step = next(
            (
                dict(item)
                for item in durable.get("steps") or []
                if isinstance(item, Mapping)
                and str(item.get("step_id") or "") == current_step_id
            ),
            {},
        )
        step_status = str(current_step.get("status") or "").lower()
        if records and any(item.get("ok") is True for item in records):
            kind, evidence = "capability_available", {"runtime_status": records}
        elif records and any(item.get("ok") is False for item in records):
            kind, evidence = "installation_failed", {"runtime_status": records}
        elif step_status in {"done", "passed", "completed", "success"}:
            kind, evidence = "capability_available", {"step_id": current_step_id, "step_status": step_status}
        elif step_status in {"blocked", "failed"}:
            kind, evidence = "installation_failed", {"step_id": current_step_id, "step_status": step_status}
        else:
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="INSTALLATION_SEMANTIC_RECEIPT_MISSING",
                detail="Installer returned without runtime healthcheck or durable step status.",
            )
        return _outcome(self.contract, kind, state, durable, {
            "status": "failed" if kind == "installation_failed" else "available",
            "evidence": evidence,
        }, "Installation capability check completed from authoritative receipts.")


async def route_blind_direct_response(source: SourceNode, state: Mapping[str, Any]) -> dict[str, Any]:
    raw = await source(route_blind_state_projection(state))
    if not isinstance(raw, Mapping):
        raise TypeError(f"direct-response source returned {type(raw).__name__}")
    return _clean_update(raw)


__all__ = [
    "TypedTriage", "TypedConsultant", "TypedPlanner", "TypedPlanReview",
    "TypedAskUser", "TypedInstallSubagent", "early_harness_error",
    "route_blind_direct_response",
]

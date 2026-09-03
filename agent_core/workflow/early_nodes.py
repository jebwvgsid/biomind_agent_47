"""Route-blind adapters for the schema-2 front half.

The legacy implementations remain intact for schema-1 checkpoints.  These
adapters reuse their domain side effects, discard every legacy route
projection, and publish one strict node-specific semantic outcome.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

from agent_core.plan_tracker import load_plan_state, task_spec_is_initialized
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
        else:
            kind = "consultation_ready"
        return _outcome(
            TriageOutcome, kind, state, durable,
            {"task_mode": mode, "request_kind": request_kind},
            f"Triage classified the request as {mode}.",
        )


class TypedConsultant(_TypedEarlyNode):
    outcome_field = "consultant_outcome"
    contract = ConsultantOutcome

    def classify(self, before, state, durable):
        pending = dict(state.get("pending_human_question") or {})
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
        status = str(state.get("task_status") or durable.get("task_status") or "").lower()
        contract_status = str(state.get("contract_status") or "")
        if status in {"blocked", "failed", "cancelled", "completed"} or contract_status.startswith("blocked_"):
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status or "blocked", "reason_code": contract_status or "consultation_terminal",
            }, "Consultation reached a terminal business state.")
        consultation = dict(state.get("consultation_state") or {})
        protocol = dict(consultation.get("finalize_protocol") or {})
        compile_outcome = dict(protocol.get("compile_outcome") or {})
        compiled = (
            int(protocol.get("schema_version") or 0) == 4
            and str(protocol.get("finalize_status") or "") == "compiled"
            and str(compile_outcome.get("status") or "") == "compiled"
        )
        task_spec = (durable.get("current_task") or {}).get("task_spec")
        if compiled or (task_spec_is_initialized(task_spec) and state.get("pending_action")):
            revision = int((durable.get("current_task") or {}).get("task_revision") or state.get("task_revision") or 0)
            return _outcome(self.contract, "planning_ready", state, durable, {
                "task_revision": revision, "contract_status": contract_status or "compiled",
            }, "Consultation produced a planning-ready semantic contract.")
        return _outcome(self.contract, "consultation_continues", state, durable, {
            "phase": str(consultation.get("phase") or "consulting"),
            "completion_decision": str(consultation.get("completion_decision") or "needs_evidence"),
        }, "Consultation needs another semantic convergence turn.")


class TypedPlanner(_TypedEarlyNode):
    outcome_field = "planner_outcome"
    contract = PlannerOutcome

    def classify(self, before, state, durable):
        status = str(state.get("task_status") or durable.get("task_status") or "").lower()
        if status in {"blocked", "failed", "cancelled", "completed", "paused_retryable"}:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status, "reason_code": "planning_terminal",
            }, "Planner reached a terminal business state.")
        stale = dict(state.get("stale_node_result") or {})
        if stale:
            return _outcome(self.contract, "stale_result", state, durable, {
                "changed_preconditions": list(stale.get("changed_preconditions") or []),
            }, "Planner result became stale before commit.")
        task_spec = (durable.get("current_task") or {}).get("task_spec")
        consultation = dict(state.get("consultation_state") or {})
        protocol = dict(consultation.get("finalize_protocol") or {})
        compile_outcome = dict(protocol.get("compile_outcome") or {})
        compiled = (
            int(protocol.get("schema_version") or 0) == 4
            and str(protocol.get("finalize_status") or "") == "compiled"
            and str(compile_outcome.get("status") or "") == "compiled"
        )
        selected = list(
            state.get("selected_skills")
            or (durable.get("task_state") or {}).get("selected_skills")
            or durable.get("selected_skill_ids")
            or []
        )
        if not task_spec_is_initialized(task_spec) or (
            str(consultation.get("phase") or "") in {"consulting", "finalize"}
            and not compiled
        ) or (not selected and not durable.get("steps")):
            return _outcome(self.contract, "consultation_required", state, durable, {
                "reason_code": "semantic_contract_not_planning_ready",
            }, "Planner requires a finalized semantic contract.")
        if durable.get("steps") or str(state.get("current_plan") or "").strip():
            return _outcome(self.contract, "plan_review_ready", state, durable, {
                "plan_id": str(durable.get("plan_id") or "pending-plan"),
                "plan_version": str(durable.get("plan_version") or durable.get("plan_revision") or "1"),
                "plan_revision": int(durable.get("plan_revision") or 0),
            }, "Planner produced a reviewable plan.")
        retry_counts = dict(state.get("llm_retry_counts") or {})
        return _outcome(self.contract, "planning_continues", state, durable, {
            "reason_code": "plan_not_yet_committed",
            "attempt_ordinal": sum(int(v or 0) for v in retry_counts.values()) + 1,
        }, "Planner needs another bounded convergence turn.")


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
        status = str(state.get("task_status") or durable.get("task_status") or "").lower()
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
        if approval in {"approved", "accepted"} and durable.get("steps"):
            backend = "external_worker" if self.execution_backend != "legacy" else "in_process"
            return _outcome(self.contract, "approved_for_execution", state, durable, {
                "plan_id": str(durable.get("plan_id") or "approved-plan"),
                "plan_version": str(durable.get("plan_version") or durable.get("plan_revision") or "1"),
                "approval_status": approval,
                "execution_backend": backend,
            }, "Plan review approved the plan for execution.")
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
        text = "\n".join(str(getattr(item, "content", item) or "") for item in state.get("messages") or [])[-6000:]
        lowered = text.lower()
        status = str(state.get("task_status") or durable.get("task_status") or "").lower()
        if status in {"blocked", "failed", "cancelled"}:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status, "reason_code": "installation_terminal",
            }, "Installation reached a terminal business state.")
        failed = any(marker in lowered for marker in ("installation failed", "healthcheck failed", "blocked:", "loop detected"))
        kind = "installation_failed" if failed else "capability_available"
        return _outcome(self.contract, kind, state, durable, {
            "status": "failed" if failed else "available",
            "evidence": text[-1200:] or "installer completed without textual evidence",
        }, "Installation capability check completed.")


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

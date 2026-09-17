"""Route-blind adapters for the schema-2 front half.

The legacy implementations remain intact for schema-1 checkpoints.  These
adapters reuse their domain side effects, discard every legacy route
projection, and publish one strict node-specific semantic outcome.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Awaitable, Callable, Mapping

from agent_core.consultant_binding import consultant_planning_readiness
from agent_core.consultation_state import (
    ConsultationDisposition,
    classify_consultation_lifecycle,
)
from agent_core.planner_loop import (
    PlannerDisposition,
    classify_planner_lifecycle,
    plan_review_acceptance_readiness,
    plan_review_candidate_scope,
    plan_review_submission_readiness,
    review_evidence_from_issues,
)
from agent_core.plan_tracker import (
    has_materialized_plan,
    load_plan_state,
    plan_approval_is_current,
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


def _semantic_receipt(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}:" + hashlib.sha256(encoded).hexdigest()[:24]


def _prior_payload(
    contract: type[StrictOutcome],
    before: Mapping[str, Any],
    field: str,
    outcome_type: str,
) -> dict[str, Any]:
    raw = before.get(field)
    if not isinstance(raw, Mapping):
        return {}
    try:
        outcome = contract.from_mapping(raw)
    except Exception:
        return {}
    if outcome.type != outcome_type:
        return {}
    return dict(outcome.to_dict().get("payload") or {})


def _stale_result_or_replay(
    contract: type[StrictOutcome],
    field: str,
    before: Mapping[str, Any],
    state: Mapping[str, Any],
    durable: Mapping[str, Any],
    stale: Mapping[str, Any],
) -> dict[str, Any]:
    receipt_id = str(
        stale.get("caller_invocation_id")
        or stale.get("invocation_id")
        or stale.get("decision_id")
        or stale.get("audit_ref")
        or _semantic_receipt("stale-result", {
            "changed_preconditions": list(
                stale.get("changed_preconditions") or []
            ),
            "detail": str(stale.get("detail") or ""),
            "caller_node": str(stale.get("caller_node") or ""),
        })
    )
    prior = _prior_payload(contract, before, field, "stale_result")
    if (
        prior
        and str(prior.get("stale_receipt_id") or "") == receipt_id
    ):
        return _semantic_harness_error(
            contract,
            state,
            durable,
            error_code=(
                contract.OUTCOME_NAME.removesuffix("Outcome").upper()
                + "_STALE_RESULT_REPLAY"
            ),
            detail=(
                "The same one-shot stale-result receipt was emitted twice "
                "without a new source invocation identity."
            ),
        )
    return _outcome(contract, "stale_result", state, durable, {
        "changed_preconditions": list(
            stale.get("changed_preconditions") or []
        ),
        "stale_receipt_id": receipt_id,
    }, "The node result became stale before commit.")


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
            "source_node": contract.OUTCOME_NAME.removesuffix("Outcome").lower(),
            "semantic_state": {
                "phase": str(
                    (state.get("consultation_state") or {}).get("phase") or ""
                ) if isinstance(state.get("consultation_state"), Mapping) else "",
                "task_status": str(state.get("task_status") or ""),
            },
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
    skill_continuation_owner = ""

    def __init__(self, *, data_path: str, source: SourceNode) -> None:
        self.data_path = data_path
        self.source = source

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state = route_blind_state_projection(state)
        if self.skill_continuation_owner:
            durable_before = load_plan_state(
                self.data_path, str(state.get("task_id") or "default")
            )
            issue = self._skill_continuation_issue(durable_before)
            if issue is not None:
                error_code, detail = issue
                return {
                    self.outcome_field: _semantic_harness_error(
                        self.contract,
                        state,
                        durable_before,
                        error_code=error_code,
                        detail=detail,
                    ),
                    "terminal_diagnostic": {
                        "source_node": self.skill_continuation_owner,
                        "error_code": error_code,
                        "detail": detail,
                    },
                }
        # These are one-invocation observations. LangGraph LastValue channels
        # retain omitted fields, so forwarding either receipt would let an old
        # result classify every later invocation forever.
        one_shot_fields = (
            "stale_node_result",
            "skill_resolution_transition",
        )
        source_state = dict(state)
        for field in one_shot_fields:
            source_state.pop(field, None)
        raw = await self.source(source_state)
        if not isinstance(raw, Mapping):
            raise TypeError(f"front-half source returned {type(raw).__name__}")
        durable = load_plan_state(
            self.data_path, str(state.get("task_id") or "default")
        )
        clean = _clean_update(raw)
        for field in one_shot_fields:
            if state.get(field) and field not in clean:
                # Clear a consumed LastValue receipt when the source did not
                # emit a replacement during this invocation.
                clean[field] = {}
        merged = {
            **{
                key: value
                for key, value in dict(state).items()
                if key not in one_shot_fields
            },
            **clean,
        }
        clean[self.outcome_field] = self.classify(state, merged, durable)
        outcome = dict(clean[self.outcome_field] or {})
        if str(outcome.get("type") or "") == "harness_error":
            payload = dict(outcome.get("payload") or {})
            clean["terminal_diagnostic"] = {
                "source_node": self.outcome_field.removesuffix("_outcome"),
                "error_code": str(payload.get("error_code") or "FRONT_HALF_HARNESS_ERROR"),
                "detail": str(payload.get("detail") or "")[:800],
                "semantic_state": deepcopy(payload.get("semantic_state") or {}),
            }
        return clean

    def _skill_continuation_issue(
        self, durable: Mapping[str, Any]
    ) -> tuple[str, str] | None:
        """Reject a durable continuation not consumable by this source node."""

        task_state = dict(durable.get("task_state") or {})
        continuation_id = str(task_state.get("skill_continuation_id") or "")
        if not continuation_id:
            return None
        access = task_state.get("skill_access_state") or durable.get(
            "skill_access_state"
        )
        continuations = (
            access.get("continuations") if isinstance(access, Mapping) else None
        )
        continuation = (
            dict(continuations.get(continuation_id) or {})
            if isinstance(continuations, Mapping)
            else {}
        )
        if (
            not continuation
            or str(continuation.get("continuation_id") or "")
            != continuation_id
            or not str(continuation.get("caller_node") or "")
        ):
            return (
                "SKILL_CONTINUATION_RECEIPT_INVALID",
                f"continuation={continuation_id}; durable owner receipt missing",
            )
        owner = str(continuation.get("caller_node") or "")
        if owner != self.skill_continuation_owner:
            return (
                "SKILL_CONTINUATION_CALLER_MISMATCH",
                f"continuation={continuation_id}; expected={owner}; "
                f"actual={self.skill_continuation_owner}",
            )
        status = str(continuation.get("status") or "")
        if status not in {"served", "resumed", "rejected"}:
            return (
                "SKILL_CONTINUATION_NOT_RESUMABLE",
                f"continuation={continuation_id}; status={status or 'missing'}",
            )
        return None

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
    skill_continuation_owner = "consultant"

    def _classify_skill_resolution_transition(
        self,
        state: Mapping[str, Any],
        durable: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        raw = state.get("skill_resolution_transition")
        if not isinstance(raw, Mapping) or not raw:
            return None
        transition = dict(raw)

        def invalid(code: str, detail: str) -> dict[str, Any]:
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code=code,
                detail=detail,
            )

        receipt_id = str(transition.get("resolution_receipt_id") or "")
        material = {
            str(key): deepcopy(value)
            for key, value in transition.items()
            if key not in {"schema_version", "resolution_receipt_id"}
        }
        if (
            int(transition.get("schema_version") or 0) != 1
            or not receipt_id
            or receipt_id != _semantic_receipt("skill-resolution", material)
        ):
            return invalid(
                "SKILL_RESOLUTION_RECEIPT_INVALID",
                "Consultant Skill resolution lacks a valid semantic receipt.",
            )

        disposition = str(transition.get("disposition") or "")
        proposal_id = str(transition.get("proposal_id") or "")
        requester_role = str(transition.get("requester_role") or "")
        role_owners = {
            "action_generation": {"generate", "corrective_generate"},
            "plan_authoring": {"planner"},
            "failure_verification": {"recovery"},
            "semantic_consultation": {"consultant"},
        }
        task_state = dict(durable.get("task_state") or {})
        access = task_state.get("skill_access_state") or durable.get(
            "skill_access_state"
        )
        proposals = (
            access.get("binding_proposals")
            if isinstance(access, Mapping)
            else None
        )
        proposal = (
            dict(proposals.get(proposal_id) or {})
            if isinstance(proposals, Mapping) and proposal_id
            else {}
        )
        proposal_owner = str(proposal.get("caller_node") or "")
        if (
            requester_role not in role_owners
            or not proposal
            or str(proposal.get("proposal_id") or "") != proposal_id
            or proposal_owner not in role_owners.get(requester_role, set())
        ):
            return invalid(
                "SKILL_RESOLUTION_IDENTITY_MISMATCH",
                "Resolution role and durable proposal owner do not match.",
            )

        common = {
            "proposal_id": proposal_id,
            "resolution_receipt_id": receipt_id,
        }
        if disposition == "observation_ready":
            continuation_id = str(transition.get("continuation_id") or "")
            observation_id = str(transition.get("observation_id") or "")
            result_status = str(transition.get("result_status") or "")
            continuations = (
                access.get("continuations")
                if isinstance(access, Mapping)
                else None
            )
            results = (
                access.get("skill_tool_results")
                if isinstance(access, Mapping)
                else None
            )
            continuation = (
                dict(continuations.get(continuation_id) or {})
                if isinstance(continuations, Mapping)
                else {}
            )
            stored_result = (
                dict(results.get(continuation_id) or {})
                if isinstance(results, Mapping)
                else {}
            )
            result_kind = str(stored_result.get("decision_result") or "")
            identity_key = (
                "request_id"
                if result_kind == "SKILL_CONTEXT_RESULT"
                else "search_id"
            )
            ready_statuses = (
                {"COMPLETE", "REQUIRES_NARROWING"}
                if result_kind == "SKILL_CONTEXT_RESULT"
                else {"MATCHES_FOUND", "NO_MATCH"}
            )
            state_result = dict(state.get("skill_decision_result") or {})
            if (
                not continuation_id
                or str(task_state.get("skill_continuation_id") or "")
                != continuation_id
                or str(continuation.get("continuation_id") or "")
                != continuation_id
                or str(continuation.get("caller_node") or "")
                != proposal_owner
                or str(continuation.get("status") or "") != "served"
                or not stored_result
                or _semantic_receipt("skill-result", stored_result)
                != _semantic_receipt("skill-result", state_result)
                or str(stored_result.get(identity_key) or "") != observation_id
                or str(stored_result.get("status") or "") != result_status
                or result_status not in ready_statuses
            ):
                return invalid(
                    "SKILL_RESOLUTION_OBSERVATION_MISMATCH",
                    "Resolution observation is not bound to its original caller.",
                )
            return _outcome(
                self.contract,
                "skill_observation_ready",
                state,
                durable,
                {
                    **common,
                    "requester_role": requester_role,
                    "continuation_id": continuation_id,
                    "observation_id": observation_id,
                    "result_status": result_status,
                },
                "Consultant produced a caller-bound Skill observation.",
            )

        if disposition == "proposal_rejected":
            if str(proposal.get("status") or "") != "rejected":
                return invalid(
                    "SKILL_RESOLUTION_REJECTION_MISMATCH",
                    "Rejected resolution lacks a durable rejected proposal.",
                )
            return _outcome(
                self.contract,
                "skill_proposal_rejected",
                state,
                durable,
                {**common, "requester_role": requester_role},
                "Consultant rejected the proposed Skill binding.",
            )

        if disposition == "task_commit_required":
            pending = dict(task_state.get("pending_skill_binding_proposal") or {})
            if (
                str(proposal.get("status") or "") != "approved_for_task_commit"
                or str(pending.get("proposal_id") or "") != proposal_id
            ):
                return invalid(
                    "SKILL_RESOLUTION_TASK_COMMIT_MISMATCH",
                    "Task-level Skill change lacks its durable commit candidate.",
                )
            return _outcome(
                self.contract,
                "task_skill_commit_required",
                state,
                durable,
                {
                    **common,
                    "task_revision": int(transition.get("task_revision") or 0),
                },
                "A reviewed task-level Skill change requires semantic commit.",
            )

        if disposition == "plan_impact_ready":
            pending = dict(task_state.get("pending_plan_impact_review") or {})
            if (
                str(proposal.get("status") or "") != "approved_for_plan_impact"
                or str(pending.get("proposal_id") or "") != proposal_id
            ):
                return invalid(
                    "SKILL_RESOLUTION_PLAN_IMPACT_MISMATCH",
                    "Step Skill approval lacks its durable plan-impact receipt.",
                )
            return _outcome(
                self.contract,
                "skill_plan_impact_ready",
                state,
                durable,
                {
                    **common,
                    "plan_revision": int(transition.get("plan_revision") or 0),
                },
                "A reviewed step Skill change requires Planner impact review.",
            )

        return invalid(
            "SKILL_RESOLUTION_DISPOSITION_INVALID",
            f"Unsupported Skill resolution disposition: {disposition or 'missing'}",
        )

    def classify(self, before, state, durable):
        task_state = dict(durable.get("task_state") or {})
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
            return _stale_result_or_replay(
                self.contract,
                self.outcome_field,
                before,
                state,
                durable,
                stale,
            )
        skill_resolution = self._classify_skill_resolution_transition(
            state, durable
        )
        if skill_resolution is not None:
            return skill_resolution
        status = str(state.get("task_status") or task_status(durable) or "").lower()
        contract_status = str(state.get("contract_status") or "")
        readiness = consultant_planning_readiness(
            durable, require_agent_initiated=True
        )
        lifecycle = classify_consultation_lifecycle(
            consultation,
            planning_handoff_committed=bool(readiness["ready"]),
        )
        disposition = str(lifecycle.get("disposition") or "")
        if (
            status in {"blocked", "failed", "cancelled", "completed"}
            or contract_status.startswith("blocked_")
        ) and disposition != ConsultationDisposition.ANSWER_READY.value:
            diagnostic = dict(state.get("terminal_diagnostic") or {})
            invocation_id = str((task_state.get("consultant_invocation") or {}).get("invocation_id") or "")
            current_provider_failure = (
                diagnostic.get("source_node") == "consultant"
                and diagnostic.get("error_code") == "CONSULTANT_PROVIDER_FAILED"
                and invocation_id and diagnostic.get("consultant_invocation_id") == invocation_id
            )
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status or "blocked", "reason_code": diagnostic["error_code"] if current_provider_failure else contract_status or "consultation_terminal",
                **({"error_code": diagnostic["error_code"], "detail": str(diagnostic.get("detail") or "")} if current_provider_failure else {}),
            }, "Consultation reached a terminal business state.")
        if disposition == ConsultationDisposition.PLANNING_READY.value:
            return _outcome(self.contract, "planning_ready", state, durable, {
                "task_revision": int(readiness["task_revision"]),
                "contract_status": contract_status or str(readiness["contract_status"]),
            }, "The Agent initiated and durably committed the planning handoff.")
        if disposition == ConsultationDisposition.ANSWER_READY.value:
            return _outcome(self.contract, "answer_ready", state, durable, {
                "answer_id": str(lifecycle.get("answer_id") or ""),
                "completion_mode": "consultation_only",
            }, "The Consultant completed the request without an execution plan.")
        if disposition == ConsultationDisposition.CONTINUE.value:
            semantic = dict(lifecycle.get("semantic_state") or {})
            agent_progress = dict(consultation.get("agent_progress") or {})
            tool_observations = [
                dict(item)
                for item in consultation.get("tool_observations") or []
                if isinstance(item, Mapping)
            ]
            last_observation = tool_observations[-1] if tool_observations else {}
            skill_decision_result = dict(
                state.get("skill_decision_result") or {}
            )
            progress_receipt_id = _semantic_receipt(
                "consultant-progress",
                {
                    "task_revision": int(readiness.get("task_revision") or 0),
                    "selected_skill_ids": list(
                        readiness.get("selected_skill_ids") or []
                    ),
                    "contract_status": contract_status,
                    "phase": str(semantic.get("phase") or ""),
                    "consultation_status": str(
                        semantic.get("consultation_status") or ""
                    ),
                    "completion_decision": str(
                        semantic.get("completion_decision") or ""
                    ),
                    "agent_disposition": str(
                        semantic.get("agent_disposition") or ""
                    ),
                    "turn_count": int(agent_progress.get("turn_count") or 0),
                    "last_fingerprint": str(
                        agent_progress.get("last_fingerprint") or ""
                    ),
                    "last_observation_fingerprint": str(
                        last_observation.get("fingerprint")
                        or last_observation.get("observation_id")
                        or ""
                    ),
                    "skill_decision_result_fingerprint": (
                        _semantic_receipt(
                            "consultant-skill-observation",
                            skill_decision_result,
                        )
                        if skill_decision_result
                        else ""
                    ),
                },
            )
            prior = _prior_payload(
                self.contract,
                before,
                self.outcome_field,
                "consultation_continues",
            )
            if (
                prior
                and str(prior.get("progress_receipt_id") or "")
                == progress_receipt_id
            ):
                return _semantic_harness_error(
                    self.contract,
                    state,
                    durable,
                    error_code="CONSULTANT_NON_PROGRESS_REPLAY",
                    detail=(
                        "Consultant attempted to self-loop without a new "
                        "semantic or tool-observation receipt."
                    ),
                )
            return _outcome(self.contract, "consultation_continues", state, durable, {
                "phase": str(semantic.get("phase") or "consulting"),
                "completion_decision": str(
                    semantic.get("completion_decision") or "needs_reasoning"
                ),
                "progress_receipt_id": progress_receipt_id,
                "turn_ordinal": int(agent_progress.get("turn_count") or 0),
            }, "The local Consultant agent has another reasoning turn available.")
        if disposition == ConsultationDisposition.NEEDS_USER.value:
            # The pending-question protocol above owns the detailed payload.
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="CONSULTANT_QUESTION_RECEIPT_INVALID",
                detail="Lifecycle requires user input but no pending question was projected.",
            )
        if disposition == ConsultationDisposition.BLOCKED.value:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": "blocked",
                "reason_code": str(lifecycle.get("reason_code") or "consultation_blocked"),
            }, "The Consultant explicitly reported an irrecoverable business block.")
        return _semantic_harness_error(
            self.contract,
            state,
            durable,
            error_code=str(
                lifecycle.get("reason_code")
                or "CONSULTANT_SEMANTIC_STATE_AMBIGUOUS"
            ),
            detail=str(lifecycle.get("detail") or readiness["reason_code"]),
        )


class TypedPlanner(_TypedEarlyNode):
    outcome_field = "planner_outcome"
    contract = PlannerOutcome
    skill_continuation_owner = "planner"

    def classify(self, before, state, durable):
        outcome = self._classify(before, state, durable)
        kind = str(outcome.get("type") or "")
        payload = dict(outcome.get("payload") or {})
        status = str(payload.get("status") or ("failed" if kind == "harness_error" else ""))
        if kind not in {"terminal", "harness_error"} or status not in {"blocked", "failed"}:
            return outcome
        task = dict(durable.get("task_state") or {})
        failure = dict(task.get("planning_failure") or {})
        epoch = dict(task.get("planning_epoch") or {})
        current_task = dict(durable.get("current_task") or {})
        revision = int(current_task.get("task_revision") or durable.get("task_revision") or 0)
        # This projection owns pre-execution planning stops. A stale approved
        # plan with past attempts keeps its existing execution-aware projection.
        if (plan_approval_is_current(durable, revision)
            or dict(durable.get("provenance_state") or {}).get("attempts")
            or task.get("typed_step_commit_receipts")):
            return outcome
        matching_failure = bool(
            failure.get("planning_epoch_id") == epoch.get("planning_epoch_id")
            and int(failure.get("task_spec_revision") or 0) == revision)
        if not matching_failure:
            failure = {}  # Old rejection facts do not supply this stop's cause.
        terminal = deepcopy(dict(failure.get("terminal_diagnostic") or {}))
        if kind == "harness_error":
            terminal = {"error_code": str(payload.get("error_code") or "PLANNER_HARNESS_ERROR"),
                        "detail": str(payload.get("detail") or ""), "error_owner": "planner"}
        code = str(terminal.get("error_code") or payload.get("error_code") or payload.get("reason_code") or "PLANNER_TERMINAL_REASON_NOT_PERSISTED")
        issues = deepcopy(list(failure.get("validation_issues") or []))
        scope = {
            "task_id": str(durable.get("task_id") or state.get("task_id") or ""),
            "plan_id": "", "plan_version": "", "step_id": "", "attempt_id": "",
            "task_revision": revision,
            "planning_epoch_id": str(epoch.get("planning_epoch_id") or ""),
            "failure_fingerprint": str(failure.get("failure_fingerprint") or ""),
            "candidate_context_ref": str(failure.get("context_ref") or ""),
        }
        planning = {
            "schema_version": 1, "producer": "planner_terminal_facts_v1",
            "scope": scope, "task_semantic_hash": str(current_task.get("task_semantic_hash") or ""),
            "task_summary": str(dict(current_task.get("task_spec") or {}).get("goal") or ""),
            "no_approved_plan": True, "plan_materialized": has_materialized_plan(durable),
            "validation_issues": issues, "source_failure_status": str(failure.get("status") or "not_persisted"),
            "primary_failure": deepcopy(failure.get("primary_failure") or {
                "status": "not_persisted", "reason_code": "PLANNER_ORIGIN_NOT_PERSISTED",
            }),
            "candidate_source": deepcopy(dict(failure.get("candidate_source") or {})),
            "primary_context_ref": str(failure.get("primary_context_ref") or ""),
            "terminal_diagnostic": terminal, "planning_epoch": deepcopy(epoch),
            "validation_source": str(failure.get("validation_source") or ""),
            "attempt": int(failure.get("attempt") or 0),
            "repeat_count": int(failure.get("repeat_count") or 0),
            "response_integrity": deepcopy(dict(failure.get("response_integrity") or {})),
            "occurrence_receipt": deepcopy(dict(failure.get("occurrence_receipt") or {})),
            "attempted_remediation": deepcopy(list((epoch.get("length_recovery") or {}).get("history") or [])),
            "first_truncation": deepcopy(dict((epoch.get("length_recovery") or {}).get("first_truncation") or {})),
            "output_policy": deepcopy(dict((failure.get("response_integrity") or {}).get("resolved_output_policy") or {})),
        }
        payload.update(status=status, reason_code=code, error_code=code, planning_terminal=planning)
        return self.contract(
            schema_version=1, type=kind, scope=scope, payload=payload,
            summary=f"{code}: Planner stopped without an approved Plan; this planning invocation executed no step.",
        ).to_dict()

    def _classify(self, before, state, durable):
        status = str(state.get("task_status") or task_status(durable) or "").lower()
        if status in {"blocked", "failed", "cancelled", "completed", "paused_retryable"}:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": status, "reason_code": "planning_terminal",
            }, "Planner reached a terminal business state.")
        stale = dict(state.get("stale_node_result") or {})
        if stale:
            return _stale_result_or_replay(
                self.contract,
                self.outcome_field,
                before,
                state,
                durable,
                stale,
            )
        readiness = consultant_planning_readiness(
            durable, require_agent_initiated=True
        )
        if not readiness["ready"] and not has_materialized_plan(durable):
            return _outcome(self.contract, "consultation_required", state, durable, {
                "reason_code": str(readiness["reason_code"]),
            }, "Planner requires a finalized semantic contract.")
        lifecycle = classify_planner_lifecycle(durable)
        disposition = str(lifecycle.get("disposition") or "")
        if disposition == PlannerDisposition.TERMINAL.value:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": str(lifecycle.get("status") or "failed"),
                "reason_code": str(lifecycle.get("reason_code") or "PLANNER_STOPPED"),
            }, str(lifecycle.get("detail") or "Planner stopped after a bounded correction."))
        if disposition == PlannerDisposition.REVIEW_READY.value:
            return _outcome(self.contract, "plan_review_ready", state, durable, {
                "plan_id": str(durable.get("plan_id") or "pending-plan"),
                "plan_version": str(durable.get("plan_version") or durable.get("plan_revision") or "1"),
                "plan_revision": int(durable.get("plan_revision") or 0),
                "submission_id": str(lifecycle.get("submission_id") or ""),
            }, "Planner explicitly submitted a durable candidate for review.")
        if disposition == PlannerDisposition.CONSULTATION_REQUIRED.value:
            return _outcome(self.contract, "consultation_required", state, durable, {
                "reason_code": str(lifecycle.get("reason_code") or "TASKSPEC_RECONCILIATION_REQUIRED"),
            }, "Planner explicitly reported a TaskSpec reconciliation requirement.")
        if disposition == PlannerDisposition.CONTINUE.value:
            progress_receipt_id = str(
                lifecycle.get("progress_receipt_id") or ""
            )
            if not progress_receipt_id:
                return _semantic_harness_error(
                    self.contract,
                    state,
                    durable,
                    error_code="PLANNER_PROGRESS_RECEIPT_MISSING",
                    detail=(
                        "planning_continues requires a durable Planner progress "
                        "receipt produced by the current local loop."
                    ),
                )
            prior_payload = _prior_payload(
                self.contract,
                before,
                self.outcome_field,
                "planning_continues",
            )
            if (
                prior_payload
                and str(prior_payload.get("progress_receipt_id") or "")
                == progress_receipt_id
            ):
                return _semantic_harness_error(
                    self.contract,
                    state,
                    durable,
                    error_code="PLANNER_NON_PROGRESS_REPLAY",
                    detail=(
                        "Planner attempted to consume the same durable occurrence again. No new call, counter increment or approval is authorized."
                        if int(lifecycle.get("receipt_version") or 1) >= 2
                        else "Legacy Planner attempted to self-loop with the same semantic progress receipt; historical occurrence identity is unavailable."
                    ),
                )
            if (int(lifecycle.get("receipt_version") or 1) >= 2 and prior_payload
                and lifecycle.get("semantic_failure_fingerprint")
                and lifecycle.get("semantic_failure_fingerprint") == prior_payload.get("semantic_failure_fingerprint")
                and (lifecycle.get("remediation_authorization") or {}).get("status") != "available"):
                return _outcome(self.contract, "terminal", state, durable, {
                    "status": "failed", "reason_code": "PLANNER_REPEATED_FAILURE",
                }, "A new occurrence repeated the same failure without an unused effective remediation; changing event IDs is not progress.")
            return _outcome(self.contract, "planning_continues", state, durable, {
                "reason_code": str(lifecycle.get("reason_code") or "PLANNER_LOCAL_LOOP_ACTIVE"),
                "attempt_ordinal": int(lifecycle.get("attempt_ordinal") or 1),
                "progress_receipt_id": progress_receipt_id,
                "progress_kind": str(
                    lifecycle.get("progress_kind") or "planner_progress"
                ),
                "receipt_version": int(lifecycle.get("receipt_version") or 1),
                "occurrence_receipt": deepcopy(lifecycle.get("occurrence_receipt") or {}),
                "semantic_failure_fingerprint": str(lifecycle.get("semantic_failure_fingerprint") or ""),
                "effective_remediation_signature": str(lifecycle.get("effective_remediation_signature") or ""),
            }, "The local Planner agent has another reasoning turn available.")
        if disposition == PlannerDisposition.INVALID.value:
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code=str(lifecycle.get("reason_code") or "PLANNER_LOOP_INVALID"),
                detail=str(lifecycle.get("detail") or "Planner lifecycle is invalid."),
            )
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
        scope = plan_review_candidate_scope(durable)
        task = dict(durable.get("task_state") or {})
        stale_receipt = bool(task.get("plan_review_submission") and
                             not plan_review_submission_readiness(durable)["identity_matches"])
        if ((not scope["eligible"] or stale_receipt)
                and not task.get("pending_review_classification")
                and task_status(durable) not in {"blocked", "failed", "cancelled", "completed"}):
            issues = [{"code": "PLAN_REVIEW_CANDIDATE_SCOPE_STALE",
                       "field_path": "$.task_state.plan_review_submission",
                       "expected": {"task_revision": scope["task_revision"],
                                    "planner_claim_id": scope["planner_claim_id"]},
                       "actual": dict((durable.get("task_state") or {}).get("plan_review_submission") or {}),
                       "severity": "error", "owner": "planner"}]
            return _outcome(self.contract, "revision_required", state, durable, {
                "validation_issues": issues,
                "reason_code": "current_candidate_not_reviewable",
            }, "The current task/candidate has no eligible review submission; the historical Plan is retained.")
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
        acceptance = plan_review_acceptance_readiness(durable)
        submission = plan_review_submission_readiness(durable)
        if acceptance["ready"] and has_materialized_plan(durable):
            backend = "external_worker" if self.execution_backend != "legacy" else "in_process"
            return _outcome(self.contract, "approved_for_execution", state, durable, {
                "plan_id": str(durable.get("plan_id") or "approved-plan"),
                "plan_version": str(durable.get("plan_version") or durable.get("plan_revision") or "1"),
                "approval_status": approval,
                "execution_backend": backend,
                "review_receipt_id": str(acceptance.get("review_receipt_id") or ""),
            }, "Plan review approved the plan for execution.")
        if approval in {"approved", "accepted"}:
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="PLAN_REVIEW_ACCEPTANCE_RECEIPT_MISSING",
                detail="An approval-like state cannot execute without a matching durable review receipt.",
            )
        if not submission["identity_matches"]:
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="PLAN_REVIEW_SUBMISSION_MISSING_OR_STALE",
                detail=(
                    "PlanReview cannot consume a candidate without a matching "
                    "Agent-initiated Planner submission receipt."
                ),
            )
        if not has_materialized_plan(durable):
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="PLAN_REVIEW_PLAN_NOT_MATERIALIZED",
                detail="PlanReview has neither a committed PlanSpec nor PlanSnapshot.",
            )
        issues = [dict(item) for item in state.get("validation_issues") or durable.get("validation_issues") or [] if isinstance(item, Mapping)]
        task_state = dict(durable.get("task_state") or {})
        submission = dict(task_state.get("plan_review_submission") or {})
        review_evidence = list(
            state.get("plan_review_evidence")
            or submission.get("review_evidence")
            or review_evidence_from_issues(issues)
        )
        if str(submission.get("status") or "") != "repair_required":
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="PLAN_REVIEW_RESULT_RECEIPT_MISSING",
                detail=(
                    "PlanReview cannot request another Planner turn without "
                    "a durable repair-required receipt bound to the submitted "
                    "Plan identity."
                ),
            )
        if not review_evidence:
            return _semantic_harness_error(
                self.contract,
                state,
                durable,
                error_code="PLAN_REVIEW_EVIDENCE_MISSING",
                detail=(
                    "A repair-required review receipt must include structured "
                    "evidence for the next Planner turn."
                ),
            )
        return _outcome(self.contract, "revision_required", state, durable, {
            "validation_issues": issues,
            "review_evidence": review_evidence,
            "reason_code": "plan_not_approved",
        }, "PlanReview produced evidence for another Planner turn.")


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
            if pending.get("type") == "installation_resolution":
                request = dict((durable.get("task_state") or {}).get("installation_request") or {})
                event = dict(request.get("resolution_event") or {})
                if (event.get("question_id") == pending.get("question_id")
                        and event.get("answer_turn_ref") == answered.get("answer_turn_ref")
                        and event.get("request_id") == pending.get("request_id")
                        and event.get("request_hash") == pending.get("request_hash")
                        and event.get("action") == request.get("resolution")
                        and clean.get("installation_request") == request):
                    semantic = "installation_pending"
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
        from agent_core.action_runtime import stable_digest
        request = dict(state.get("installation_request") or before.get("installation_request") or {})
        request_id = str(request.get("request_id") or request.get("fingerprint") or "")
        receipt = dict(state.get("installation_receipt") or {})
        receipt_status = str(receipt.get("status") or "").lower()
        stopped = receipt_status in {"cancelled", "rejected", "blocked", "manual_wait"}
        identity_fields = ("attempt_id", "target_env", "target_prefix")
        matches = bool(request_id and receipt.get("request_id") == request_id
                       and receipt.get("request_hash") == stable_digest(request)
                       and receipt.get("generation_source_scope") == request.get("generation_source_scope")
                       and request.get("attempt_id")
                       and all((stopped or request.get(key)) and receipt.get(key) == request.get(key)
                               for key in identity_fields)
                       and all(sorted(receipt.get(key) or []) == sorted(request.get(key) or [])
                               for key in ("selected_skills", "capabilities")))
        if not matches:
            return _semantic_harness_error(
                self.contract, state, durable,
                error_code="INSTALLATION_SEMANTIC_RECEIPT_MISSING",
                detail="Installer returned without a receipt matching the current request, attempt, target and selected capabilities.",
            )
        pending = dict(state.get("pending_human_question") or {})
        if (receipt_status in {"manual_wait", "execution_failed", "verification_failed", "registration_failed"}
                and pending.get("type") == "installation_resolution"
                and pending.get("request_id") == request_id
                and pending.get("request_hash") == stable_digest(request)
                and pending == (durable.get("task_state") or {}).get("pending_question")):
            return _outcome(self.contract, "user_input_needed", state, durable, {
                "question_id": pending["question_id"], "question": pending["question"], "evidence": receipt,
            }, "Installation requires an explicit scoped user decision.")
        if stopped:
            return _outcome(self.contract, "terminal", state, durable, {
                "status": "cancelled" if receipt_status == "cancelled" else "blocked",
                "reason_code": "installation_" + receipt_status,
                "evidence": receipt,
            }, "Installation stopped with a scoped user decision or blocked work order.")
        required_skills = set(request.get("selected_skills") or [])
        required_capabilities = set(request.get("capabilities") or [])
        checks = [dict(row) for row in receipt.get("verification") or []
                  if isinstance(row, Mapping)
                  and row.get("target_prefix") == request.get("target_prefix")]
        verified_skills = {row.get("skill_id") for row in checks if row.get("ok") is True}
        verified_capabilities = {row.get("capability_id") for row in checks if row.get("ok") is True}
        registration = dict(receipt.get("registration") or {})
        complete = bool(
            receipt_status == "available" and checks
            and all(row.get("ok") is True for row in checks)
            and required_skills <= verified_skills
            and required_capabilities <= verified_capabilities
            and all(isinstance(registration.get(name), Mapping)
                    and registration[name].get("ok") is True
                    for name in ("skill_registry", "software_registry"))
        )
        kind = "capability_available" if complete else "installation_failed"
        return _outcome(self.contract, kind, state, durable, {
            "status": "available" if complete else "failed",
            "evidence": receipt,
        }, "Installation capability check completed from target-bound verification and registration receipts.")


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

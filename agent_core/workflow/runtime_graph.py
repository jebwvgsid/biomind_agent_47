"""Conditional-edge authority for the schema-2 runtime graph.

This module is the only runtime migration module allowed to map semantic
outcome types to LangGraph node names.  It never reads legacy route fields.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from langgraph.graph import END, START, StateGraph

from agent_core.state import AgentState
from agent_core.workflow.outcomes import (
    AskUserOutcome,
    CommitStepOutcome,
    ConsultantOutcome,
    ExecuteOutcome,
    FinalAuditOutcome,
    GenerateOutcome,
    InstallSubagentOutcome,
    PlannerOutcome,
    PlanReviewOutcome,
    RecoveryOutcome,
    TriageOutcome,
    VerificationOutcome,
)
from agent_core.workflow.step_worker import StepOutcome


def route_generate_outcome(state: Mapping[str, Any]) -> str:
    outcome = GenerateOutcome.from_mapping(state.get("generate_outcome") or {})
    if outcome.type == "candidate_rejected":
        payload = dict(outcome.to_dict().get("payload") or {})
        receipt = dict(payload.get("budget_receipt") or {})
        if (
            payload.get("repairable") is True
            and receipt.get("progress_proven") is True
            and int(receipt.get("ordinal") or 0) <= int(receipt.get("limit") or 0)
        ):
            return "generate"
        return END
    if outcome.type == "stale_result":
        changed = set(outcome.to_dict().get("payload", {}).get("changed_preconditions") or ())
        return "consultant" if changed & {"task_revision", "authoritative_user_turn_revision"} else "planner"
    return {
        "action_ready": "execute",
        "completion_claimed": "supervisor",
        "user_input_needed": "ask_user",
        "plan_revision_needed": "planner",
        "skill_context_needed": "consultant",
        "provider_failed": END,
        "internal_protocol_error": END,
        "blocked": END,
        "cancelled": END,
        "harness_error": END,
    }[outcome.type]


def route_execute_outcome(state: Mapping[str, Any]) -> str:
    outcome = ExecuteOutcome.from_mapping(state.get("execute_outcome") or {})
    payload = dict(outcome.to_dict().get("payload") or {})
    if outcome.type == "pre_execution_rejected":
        owner = str(payload.get("violation_owner") or "").strip().upper()
        if owner in {"GENERATED_ACTION", "CANDIDATE", "MODEL"}:
            return "generate"
        if owner in {"PLAN", "PLANNER"}:
            return "planner"
        if owner in {"INPUT_DATA", "USER"}:
            return "ask_user"
        if owner in {"VERIFICATION", "DEPENDENCY"}:
            return "recovery"
        return END
    if outcome.type == "stale_result":
        changed = set(payload.get("changed_preconditions") or ())
        return "consultant" if changed & {"task_revision", "authoritative_user_turn_revision"} else "planner"
    return {
        "execution_succeeded": "supervisor",
        "execution_failed": "recovery",
        "diagnostic_completed": "recovery",
        "user_input_needed": "ask_user",
        "installation_requested": "install_subagent",
        "approval_rejected": "generate",
        "lightweight_completed": END,
        "blocked": END,
        "cancelled": END,
        "harness_error": END,
    }[outcome.type]


def route_verification_outcome(state: Mapping[str, Any]) -> str:
    outcome = VerificationOutcome.from_mapping(state.get("verification_outcome") or {})
    return {
        "verified": "commit_step",
        "verified_with_warnings": "commit_step",
        "rejected": "recovery",
        "incomplete_evidence": "recovery",
        "diagnostic_evidence_recorded": "recovery",
        "verification_error": "recovery",
        "harness_error": END,
    }[outcome.type]


def route_commit_step_outcome(state: Mapping[str, Any]) -> str:
    outcome = CommitStepOutcome.from_mapping(state.get("commit_step_outcome") or {})
    return {
        "next_step_ready": "generate",
        "plan_complete": "final_audit",
        "commit_conflict": "recovery",
        "contract_rejected": "recovery",
        "harness_error": END,
    }[outcome.type]


def route_recovery_outcome(state: Mapping[str, Any]) -> str:
    outcome = RecoveryOutcome.from_mapping(state.get("recovery_outcome") or {})
    return {
        "candidate_repair_required": "generate",
        "plan_revision_required": "planner",
        "reconsultation_required": "consultant",
        "revalidation_required": "supervisor",
        "user_input_required": "ask_user",
        "blocked": END,
        "internal_protocol_error": END,
        "harness_error": END,
    }[outcome.type]


def route_final_audit_outcome(state: Mapping[str, Any]) -> str:
    FinalAuditOutcome.from_mapping(state.get("final_audit_outcome") or {})
    return END


RuntimeNode = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


def build_schema2_runtime_graph(
    node_handlers: Mapping[str, RuntimeNode],
    *,
    boundary_handlers: Mapping[str, RuntimeNode] | None = None,
) -> StateGraph:
    """Build the isolated typed runtime segment without activating it.

    Early-graph destinations are explicit boundary nodes until CTRL-03 owns
    their typed contracts.  They terminate this isolated segment and never
    interpret legacy route fields.
    """

    required = {
        "generate", "execute", "supervisor", "commit_step", "recovery", "final_audit"
    }
    missing = sorted(required - set(node_handlers))
    if missing:
        raise ValueError("SCHEMA2_RUNTIME_HANDLER_MISSING: " + ", ".join(missing))

    async def boundary(_state: Mapping[str, Any]) -> dict[str, Any]:
        return {}

    # Keep graph decisions on the async scheduler.  Some deployed LangGraph
    # builds delegate synchronous conditional callables to a shared executor;
    # a cyclic runtime segment can then wait on executor shutdown after END.
    async def generate_route(state: Mapping[str, Any]) -> str:
        return route_generate_outcome(state)

    async def execute_route(state: Mapping[str, Any]) -> str:
        return route_execute_outcome(state)

    async def verification_route(state: Mapping[str, Any]) -> str:
        return route_verification_outcome(state)

    async def commit_route(state: Mapping[str, Any]) -> str:
        return route_commit_step_outcome(state)

    async def recovery_route(state: Mapping[str, Any]) -> str:
        return route_recovery_outcome(state)

    async def final_route(state: Mapping[str, Any]) -> str:
        return route_final_audit_outcome(state)

    graph = StateGraph(AgentState)
    for name in sorted(required):
        graph.add_node(name, node_handlers[name])
    supplied_boundaries = dict(boundary_handlers or {})
    for name in ("planner", "consultant", "ask_user", "install_subagent"):
        graph.add_node(name, supplied_boundaries.get(name, boundary))
        graph.add_edge(name, END)
    graph.add_edge(START, "generate")
    graph.add_conditional_edges(
        "generate",
        generate_route,
        ["generate", "execute", "supervisor", "planner", "consultant", "ask_user", END],
    )
    graph.add_conditional_edges(
        "execute",
        execute_route,
        [
            "supervisor", "recovery", "generate", "planner", "consultant",
            "ask_user", "install_subagent", END,
        ],
    )
    graph.add_conditional_edges(
        "supervisor", verification_route, ["commit_step", "recovery", END]
    )
    graph.add_conditional_edges(
        "commit_step", commit_route, ["generate", "final_audit", "recovery", END]
    )
    graph.add_conditional_edges(
        "recovery",
        recovery_route,
        ["generate", "planner", "consultant", "supervisor", "ask_user", END],
    )
    graph.add_conditional_edges("final_audit", final_route, [END])
    return graph


def build_schema2_production_graph(
    node_handlers: Mapping[str, RuntimeNode],
) -> StateGraph:
    """Build the complete routing-schema-2 graph.

    Every string-to-node decision for this graph is contained here.  Node
    handlers publish facts only and legacy route fields are never inspected.
    """

    required = {
        "triage", "direct_response", "consultant", "planner", "plan_review",
        "ask_user", "install_subagent", "generate", "step_worker", "execute",
        "supervisor", "commit_step", "recovery", "final_audit",
    }
    missing = sorted(required - set(node_handlers))
    if missing:
        raise ValueError("SCHEMA2_PRODUCTION_HANDLER_MISSING: " + ", ".join(missing))

    async def triage_route(state: Mapping[str, Any]) -> str:
        outcome = TriageOutcome.from_mapping(state.get("triage_outcome") or {})
        if outcome.type == "resume_classified":
            semantic = str(outcome.payload.get("semantic_state") or "")
            return {
                "awaiting_user_input": "ask_user",
                "consultation_ready": "consultant",
                "planning_ready": "planner",
                "plan_review_pending": "plan_review",
                "step_execution_ready": "generate",
                "action_execution_ready": "execute",
                "verification_pending": "supervisor",
                "recovery_pending": "recovery",
                "installation_pending": "install_subagent",
                "final_audit_pending": "final_audit",
                "terminal": END,
            }[semantic]
        return {
            "direct_response_ready": "direct_response",
            "consultation_ready": "consultant",
            "step_execution_ready": "generate",
            "harness_error": END,
        }[outcome.type]

    async def consultant_route(state: Mapping[str, Any]) -> str:
        outcome = ConsultantOutcome.from_mapping(state.get("consultant_outcome") or {})
        return {
            "planning_ready": "planner",
            "consultation_continues": "consultant",
            "user_input_needed": "ask_user",
            "stale_result": "consultant",
            "terminal": END,
            "harness_error": END,
        }[outcome.type]

    async def planner_route(state: Mapping[str, Any]) -> str:
        outcome = PlannerOutcome.from_mapping(state.get("planner_outcome") or {})
        return {
            "plan_review_ready": "plan_review",
            "consultation_required": "consultant",
            "planning_continues": "planner",
            "stale_result": "planner",
            "terminal": END,
            "harness_error": END,
        }[outcome.type]

    async def plan_review_route(state: Mapping[str, Any]) -> str:
        outcome = PlanReviewOutcome.from_mapping(state.get("plan_review_outcome") or {})
        if outcome.type == "approved_for_execution":
            return (
                "step_worker"
                if str(outcome.payload.get("execution_backend") or "") == "external_worker"
                else "generate"
            )
        return {
            "revision_required": "planner",
            "reconsultation_required": "consultant",
            "user_input_needed": "ask_user",
            "approval_pending": END,
            "terminal": END,
            "harness_error": END,
        }[outcome.type]

    async def ask_user_route(state: Mapping[str, Any]) -> str:
        outcome = AskUserOutcome.from_mapping(state.get("ask_user_outcome") or {})
        semantic = str(outcome.payload.get("semantic_state") or "consultation_ready")
        return {
            "consultation_ready": "consultant",
            "planning_ready": "planner",
            "step_execution_ready": "generate",
            "verification_pending": "supervisor",
            "recovery_pending": "recovery",
        }.get(semantic, END)

    async def install_route(state: Mapping[str, Any]) -> str:
        outcome = InstallSubagentOutcome.from_mapping(
            state.get("install_subagent_outcome") or {}
        )
        return {
            "capability_available": "generate",
            "installation_failed": "recovery",
            "terminal": END,
            "harness_error": END,
        }[outcome.type]

    async def step_worker_route(state: Mapping[str, Any]) -> str:
        outcome = StepOutcome.from_mapping(state.get("step_worker_outcome") or {})
        return {
            "completion_claimed": "supervisor",
            "needs_replan": "planner",
            "blocked": END,
            "harness_error": END,
            "cancelled": END,
        }[outcome.type]

    async def generate_route(state: Mapping[str, Any]) -> str:
        return route_generate_outcome(state)

    async def execute_route(state: Mapping[str, Any]) -> str:
        return route_execute_outcome(state)

    async def verification_route(state: Mapping[str, Any]) -> str:
        return route_verification_outcome(state)

    async def commit_route(state: Mapping[str, Any]) -> str:
        return route_commit_step_outcome(state)

    async def recovery_route(state: Mapping[str, Any]) -> str:
        return route_recovery_outcome(state)

    async def final_route(state: Mapping[str, Any]) -> str:
        return route_final_audit_outcome(state)

    graph = StateGraph(AgentState)
    for name in sorted(required):
        graph.add_node(name, node_handlers[name])
    graph.add_edge(START, "triage")
    graph.add_edge("direct_response", END)
    graph.add_conditional_edges("triage", triage_route, [
        "direct_response", "consultant", "planner", "plan_review", "ask_user",
        "generate", "execute", "supervisor", "recovery", "install_subagent", "final_audit", END,
    ])
    graph.add_conditional_edges("consultant", consultant_route, ["planner", "consultant", "ask_user", END])
    graph.add_conditional_edges("planner", planner_route, ["plan_review", "consultant", "planner", END])
    graph.add_conditional_edges("plan_review", plan_review_route, ["generate", "step_worker", "planner", "consultant", "ask_user", END])
    graph.add_conditional_edges("ask_user", ask_user_route, ["consultant", "planner", "generate", "supervisor", "recovery", END])
    graph.add_conditional_edges("install_subagent", install_route, ["generate", "recovery", END])
    graph.add_conditional_edges("step_worker", step_worker_route, ["supervisor", "planner", END])
    graph.add_conditional_edges("generate", generate_route, ["generate", "execute", "supervisor", "planner", "consultant", "ask_user", END])
    graph.add_conditional_edges("execute", execute_route, ["supervisor", "recovery", "generate", "planner", "consultant", "ask_user", "install_subagent", END])
    graph.add_conditional_edges("supervisor", verification_route, ["commit_step", "recovery", END])
    graph.add_conditional_edges("commit_step", commit_route, ["generate", "final_audit", "recovery", END])
    graph.add_conditional_edges("recovery", recovery_route, ["generate", "planner", "consultant", "supervisor", "ask_user", END])
    graph.add_conditional_edges("final_audit", final_route, [END])
    return graph


__all__ = [
    "route_generate_outcome",
    "route_execute_outcome",
    "route_verification_outcome",
    "route_commit_step_outcome",
    "route_recovery_outcome",
    "route_final_audit_outcome",
    "build_schema2_runtime_graph",
    "build_schema2_production_graph",
]

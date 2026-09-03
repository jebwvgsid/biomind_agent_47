"""Conditional-edge authority for the schema-2 runtime graph.

This module is the only runtime migration module allowed to map semantic
outcome types to LangGraph node names.  It never reads legacy route fields.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from langgraph.graph import END, START, StateGraph

from agent_core.state import AgentState
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    FinalAuditOutcome,
    GenerateOutcome,
    RecoveryOutcome,
    VerificationOutcome,
)


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


__all__ = [
    "route_generate_outcome",
    "route_execute_outcome",
    "route_verification_outcome",
    "route_commit_step_outcome",
    "route_recovery_outcome",
    "route_final_audit_outcome",
    "build_schema2_runtime_graph",
]

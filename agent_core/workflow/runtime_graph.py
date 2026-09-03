"""Conditional-edge authority for the schema-2 runtime graph.

This module is the only runtime migration module allowed to map semantic
outcome types to LangGraph node names.  It never reads legacy route fields.
"""

from __future__ import annotations

from typing import Any, Mapping

from langgraph.graph import END

from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    GenerateOutcome,
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
        "plan_complete": "supervisor_final_audit",
        "commit_conflict": "recovery",
        "contract_rejected": "recovery",
        "harness_error": END,
    }[outcome.type]


__all__ = [
    "route_generate_outcome",
    "route_execute_outcome",
    "route_verification_outcome",
    "route_commit_step_outcome",
]

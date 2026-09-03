"""Conditional-edge authority for the schema-2 runtime graph.

This module is the only runtime migration module allowed to map semantic
outcome types to LangGraph node names.  It never reads legacy route fields.
"""

from __future__ import annotations

from typing import Any, Mapping

from langgraph.graph import END

from agent_core.workflow.outcomes import CommitStepOutcome, VerificationOutcome


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


__all__ = ["route_verification_outcome", "route_commit_step_outcome"]

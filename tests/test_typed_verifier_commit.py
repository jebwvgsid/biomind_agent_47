from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langgraph.graph import END, START, StateGraph

from agent_core.completion_gate import CompletionGate
from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import record_provenance_event
from agent_core.recovery import ValidationEvidence
from agent_core.semantic_completion import build_completion_report
from agent_core.state import AgentState
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    VerificationOutcome,
)


def _durable_state() -> dict:
    return {
        "schema_version": 10,
        "workflow_routing_schema_version": 2,
        "task_id": "task-typed",
        "plan_id": "plan-typed",
        "plan_version": "plan-v2",
        "plan_revision": 2,
        "state_generation": 7,
        "current_step_id": "step-1",
        "task_state": {
            "status": "running",
            "workflow_routing_schema_version": 2,
            "recovery_state": {},
        },
        "steps": [
            {"step_id": "step-1", "status": "verification_pending", "depends_on": []},
            {"step_id": "step-2", "status": "pending", "depends_on": ["step-1"]},
        ],
        "provenance_state": {
            "schema_version": 2,
            "task_id": "task-typed",
            "actions": {},
            "attempts": {
                "attempt-1": {
                    "attempt_id": "attempt-1",
                    "action_id": "action-1",
                    "plan_id": "plan-typed",
                    "plan_version": "plan-v2",
                    "step_id": "step-1",
                    "status": "EVIDENCE_COMMITTED",
                    "command_role": "analysis_execution",
                    "artifact_version_ids": [],
                }
            },
            "artifacts": {},
            "artifact_heads": {},
            "manifests": {},
            "stages": {},
            "validations": {},
            "attempt_counters": {},
            "migration_log": [],
            "updated_at": "",
        },
    }


def _validation(*, role: str = "analysis_execution", status: str = "VALID") -> dict:
    return {
        "schema_version": 1,
        "validation_id": "validation-1",
        "task_id": "task-typed",
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "status": status,
        "errors": [],
        "warnings": [],
        "info": [],
        "returncode_check": {"returncode": 0, "passed": True},
        "manifest_results": [],
        "artifact_results": [],
        "success_criteria_results": [],
        "scientific_checks": [],
        "recoverability_hints": [],
        "evidence_complete": True,
        "evidence_source": "execution_evidence",
        "command_role": role,
    }


def _attach_semantic_report(
    outcome: dict,
    plan_state: dict,
    *,
    execution_evidence: dict | None = None,
    completion_gate: dict | None = None,
) -> dict:
    validation = ValidationEvidence.from_mapping(
        outcome["payload"]["validation_evidence"]
    ).to_dict()
    outcome["payload"]["validation_evidence"] = validation
    report = build_completion_report(
        plan_state=plan_state,
        validation=validation,
        execution_evidence=execution_evidence or {
            "step_id": validation["step_id"],
            "action_id": validation["action_id"],
            "attempt_id": validation["attempt_id"],
        },
        completion_gate=(
            dict(completion_gate)
            if completion_gate is not None
            else dict(outcome["payload"].get("completion_gate") or {})
        ),
    )
    outcome["payload"]["completion_report"] = report.to_dict()
    outcome["payload"]["semantic_decision"] = report.effective_decision.to_dict()
    return outcome


def _raw_verified_outcome(
    *, generation: int = 7, role: str = "analysis_execution"
) -> dict:
    return VerificationOutcome(
        schema_version=1,
        type="verified",
        summary="verified fixture",
        scope={
            "task_id": "task-typed",
            "plan_id": "plan-typed",
            "plan_version": "plan-v2",
            "step_id": "step-1",
            "state_generation": generation,
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "validation_id": "validation-1",
        },
        payload={
            "validation_evidence": _validation(role=role),
            "completion_gate": {"passed": True, "plan_version": "plan-v2"},
            "accepted_warnings": [],
        },
        evidence_refs=("validation-1",),
    ).to_dict()


def _verified_outcome(*, generation: int = 7, role: str = "analysis_execution") -> dict:
    plan_state = _durable_state()
    plan_state["state_generation"] = generation
    outcome = _raw_verified_outcome(generation=generation, role=role)
    return _attach_semantic_report(outcome, plan_state)


class _MemoryStore:
    def __init__(self, value: dict):
        self.value = copy.deepcopy(value)
        self.save_count = 0
        self.conflict_on_save = False

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.save_count += 1
        if self.conflict_on_save:
            self.value["state_generation"] += 1
            raise StateCASConflict("fixture concurrent writer")
        expected = int(candidate.get("state_generation") or 0)
        if expected != int(self.value.get("state_generation") or 0):
            raise StateCASConflict("fixture stale generation")
        committed = copy.deepcopy(candidate)
        committed["state_generation"] = expected + 1
        self.value = committed










if __name__ == "__main__":
    unittest.main()

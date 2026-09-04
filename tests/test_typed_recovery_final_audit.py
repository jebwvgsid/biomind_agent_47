from __future__ import annotations

import copy
import inspect
import unittest
from unittest.mock import AsyncMock, patch

from langgraph.graph import END

import agent_core.workflow.final_audit as typed_final_audit_module
import agent_core.workflow.recovery_runtime as typed_recovery_module
from agent_core.plan_tracker import plan_record_hash, semantic_plan_hash
from agent_core.state import AgentState
from agent_core.task_spec import create_task_spec, task_record_hash, task_semantic_hash
from agent_core.workflow.final_audit import TypedFinalAudit
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    GenerateOutcome,
    RecoveryOutcome,
    VerificationOutcome,
)
from agent_core.workflow.recovery_runtime import TypedRecovery
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.runtime_graph import (
    build_schema2_runtime_graph,
    route_final_audit_outcome,
    route_recovery_outcome,
)
from bio_agents import BioAnalystAgent


def _validation(validation_id: str = "validation-1") -> dict:
    return {
        "schema_version": 1,
        "validation_id": validation_id,
        "task_id": "task-typed",
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "status": "VALID",
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
        "command_role": "analysis_execution",
    }


def _execution(returncode: int = 0) -> dict:
    return {
        "task_id": "task-typed",
        "plan_id": "plan-typed",
        "plan_version": "plan-v2",
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "execution_id": "execution-1",
        "returncode": returncode,
        "command_role": "analysis_execution",
        "failure_fingerprint": "failure-1" if returncode else "",
    }


def _execute_outcome(
    returncode: int = 0,
    *,
    failure_fingerprint: str = "failure-1",
    incident_id: str = "",
) -> dict:
    execution = _execution(returncode)
    execution["failure_fingerprint"] = failure_fingerprint if returncode else ""
    return ExecuteOutcome(
        schema_version=1,
        type="execution_succeeded" if returncode == 0 else "execution_failed",
        summary="execution fixture",
        scope={
            "task_id": "task-typed",
            "plan_id": "plan-typed",
            "plan_version": "plan-v2",
            "step_id": "step-1",
        },
        payload={
            "execution_record": execution,
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "return_code": returncode,
            **({"validation": {"passed": True}} if returncode == 0 else {
                "failure_fingerprint": failure_fingerprint,
                "progress_snapshot": {},
                **({"incident_id": incident_id} if incident_id else {}),
            }),
        },
        evidence_refs=("execution-1",),
    ).to_dict()


def _verification(*, plan_version: str = "plan-v2") -> dict:
    validation = _validation()
    return VerificationOutcome(
        schema_version=1,
        type="verified",
        summary="verified fixture",
        scope={
            "task_id": "task-typed",
            "plan_id": "plan-typed",
            "plan_version": plan_version,
            "step_id": "step-1",
            "state_generation": 7,
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "validation_id": "validation-1",
        },
        payload={
            "validation_evidence": validation,
            "completion_gate": {"passed": True, "plan_version": "plan-v2"},
            "accepted_warnings": [],
        },
        evidence_refs=("validation-1",),
    ).to_dict()


def _commit() -> dict:
    return CommitStepOutcome(
        schema_version=1,
        type="plan_complete",
        summary="plan complete",
        scope={"task_id": "task-typed"},
        payload={
            "commit_id": "commit-1",
            "receipt": "typed-step-commit:commit-1",
            "before_state_generation": 7,
            "after_state_generation": 8,
            "completed_step_id": "step-1",
        },
        evidence_refs=("validation-1",),
    ).to_dict()


def _durable(*, final: bool = False) -> dict:
    commit = _commit()
    task_spec = create_task_spec(
        "Execute the typed Recovery fixture.", task_id="task-typed"
    )
    steps = [{"step_id": "step-1", "status": "passed" if final else "running"}]
    plan_spec = {
        "plan_id": "plan-typed",
        "plan_version": "plan-v2",
        "plan_revision": 1,
        "generated_from_task_revision": 1,
        "base_task_semantic_hash": task_semantic_hash(task_spec),
        "steps": copy.deepcopy(steps),
    }
    state = {
        "schema_version": 10,
        "workflow_routing_schema_version": 2,
        "task_id": "task-typed",
        "plan_id": "plan-typed",
        "plan_version": "plan-v2",
        "state_generation": 8 if final else 4,
        "current_step_id": "" if final else "step-1",
        "current_task": {
            "task_spec": copy.deepcopy(task_spec),
            "task_revision": 1,
            "task_semantic_hash": task_semantic_hash(task_spec),
            "task_record_hash": task_record_hash(task_spec),
        },
        "current_plan": {
            "plan_spec": copy.deepcopy(plan_spec),
            "plan_revision": 1,
            "plan_semantic_hash": "sha256:" + semantic_plan_hash(plan_spec),
            "plan_record_hash": plan_record_hash(plan_spec),
            "base_task_revision": 1,
            "base_task_semantic_hash": task_semantic_hash(task_spec),
        },
        "task_state": {
            "status": "running",
            "workflow_routing_schema_version": 2,
            "typed_step_commit_receipts": ({
                "commit-1": {
                    "schema_version": 1,
                    "request_fingerprint": "fixture",
                    "outcome": commit,
                }
            } if final else {}),
        },
        "steps": steps,
        "provenance_state": {
            "attempts": {
                "attempt-1": {
                    "attempt_id": "attempt-1",
                    "action_id": "action-1",
                    "step_id": "step-1",
                    "plan_id": "plan-typed",
                    "plan_version": "plan-v2",
                    "status": "COMPLETED" if final else "EVIDENCE_COMMITTED",
                }
            }
        },
    }
    return state


class _MemoryStore:
    def __init__(self, value: dict):
        self.value = copy.deepcopy(value)
        self.save_count = 0

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.save_count += 1
        expected = int(candidate.get("state_generation") or 0)
        if expected != int(self.value.get("state_generation") or 0):
            from agent_core.plan_contract import StateCASConflict
            raise StateCASConflict("fixture conflict")
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = expected + 1


def _bind_causal(
    store: _MemoryStore, source_kind: str, source: dict
) -> dict:
    record = build_causal_failure_record(
        source_kind, source, store.value, task_id="task-typed"
    )
    task_state = dict(store.value.get("task_state") or {})
    packets = dict(task_state.get("causal_failure_packets") or {})
    packets[record["packet_ref"]] = copy.deepcopy(record)
    task_state["causal_failure_packets"] = packets
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    store.value["task_state"] = task_state
    return {
        "task_id": "task-typed",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": copy.deepcopy(record["failure_packet"]),
        "failure_source": copy.deepcopy(record["failure_source"]),
    }


def _recovery_directive(
    action: str,
    *,
    directive_id: str,
    summary: str = "The causal failure requires a semantic correction.",
    evidence_refs=(),
) -> dict:
    value = {
        "schema_version": 1,
        "directive_id": directive_id,
        "task_id": "task-typed",
        "action": action,
        "summary": summary,
        "reason_code": "CAUSAL_FAILURE_DIAGNOSED",
        "evidence_refs": list(evidence_refs),
    }
    if action in {"retry_step", "revalidate"}:
        value["target_step_id"] = "step-1"
    if action in {"retry_step", "revise_plan", "reconsult"}:
        value["instruction"] = "Correct only the failed semantic scope."
    return value


class TypedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_free_decision_is_consumed_once(self):
        store = _MemoryStore(_durable())
        recovery = TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        state = {
            **_bind_causal(store, "execute_outcome", _execute_outcome(17)),
            "recovery_directive": _recovery_directive(
                "retry_step",
                directive_id="directive-route-free",
                evidence_refs=("execution-1",),
            ),
            "next_step": "finish",
            "proposed_route": "finish",
            "effective_route": "finish",
            "dispatch_route": "finish",
            "route_resolution": {"dispatch_route": "finish"},
            "node_recommended_next_step": "finish",
            "required_next_action": "COMPLETE_TASK",
            "pending_recovery_action": {"type": "complete_task"},
            "recovery_decision": {"target_route": "finish", "next_route": "finish"},
        }
        with (
            patch.object(BioAnalystAgent, "_apply_transition_guard", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_resolve_and_commit_effective_transition", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_dispatch_effective_route", side_effect=AssertionError),
        ):
            first = await recovery.run(state)
            second = await recovery.run(state)
        self.assertEqual(first, second)
        self.assertEqual(first["recovery_outcome"]["type"], "candidate_repair_required")
        self.assertEqual(route_recovery_outcome({**state, **first}), "generate")
        self.assertEqual(store.save_count, 1)
        serialized = str(first["recovery_outcome"])
        for poison in ("next_step", "target_route", "dispatch_route", "required_next_action"):
            self.assertNotIn(poison, serialized)

    async def test_completion_disposition_is_internal_protocol_not_generate(self):
        store = _MemoryStore(_durable())
        result = await TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({
            "task_id": "task-typed",
            "recovery_fact": {
                "disposition": "complete_step",
                "decision_id": "forbidden-completion",
            },
        })
        self.assertEqual(result["recovery_outcome"]["type"], "internal_protocol_error")
        self.assertEqual(route_recovery_outcome(result), END)

    async def test_revalidation_requires_new_evidence(self):
        store = _MemoryStore(_durable())
        result = await TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save
        ).run({
            "task_id": "task-typed",
            "recovery_fact": {
                "disposition": "revalidation",
                "decision_id": "revalidate-without-evidence",
                "prior_validation_id": "validation-1",
            },
        })
        self.assertEqual(result["recovery_outcome"]["type"], "internal_protocol_error")

    async def test_repair_budget_is_monotonic_across_new_decisions(self):
        store = _MemoryStore(_durable())
        recovery = TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save, repair_limit=2
        )
        outcomes = []
        for ordinal in (1, 2, 3):
            source = _execute_outcome(
                17,
                failure_fingerprint=f"failure-shared-{ordinal}",
                incident_id="incident-shared",
            )
            outcomes.append(await recovery.run(
                {
                    **_bind_causal(store, "execute_outcome", source),
                    "recovery_directive": _recovery_directive(
                        "retry_step",
                        directive_id=f"directive-budget-{ordinal}",
                        summary=f"Changed diagnosis {ordinal}.",
                        evidence_refs=("execution-1",),
                    ),
                }
            ))
        self.assertEqual(
            [item["recovery_outcome"]["type"] for item in outcomes],
            ["candidate_repair_required", "candidate_repair_required", "blocked"],
        )
        self.assertEqual(store.value["task_state"]["typed_recovery_budget"]["incident-shared"], 2)

    async def test_consumed_checkpoint_outcome_cannot_shadow_new_failure(self):
        store = _MemoryStore(_durable())
        recovery = TypedRecovery(
            data_path="/fixture", loader=store.load, saver=store.save
        )
        conflict = CommitStepOutcome(
            schema_version=1,
            type="commit_conflict",
            summary="fixture conflict",
            scope={"task_id": "task-typed"},
            payload={
                "expected_state_generation": 3,
                "current_state_generation": 4,
                "no_partial_mutation": True,
            },
        ).to_dict()
        first = await recovery.run({
            **_bind_causal(store, "commit_step_outcome", conflict),
            "recovery_directive": _recovery_directive(
                "revise_plan", directive_id="directive-conflict"
            ),
        })
        second_state = _bind_causal(
            store, "execute_outcome", _execute_outcome(17)
        )
        second = await recovery.run({
            **second_state,
            "recovery_directive": _recovery_directive(
                "retry_step",
                directive_id="directive-execute",
                evidence_refs=("execution-1",),
            ),
            "commit_step_outcome": conflict,
            "execute_outcome": _execute_outcome(17),
        })
        self.assertEqual(first["recovery_outcome"]["type"], "plan_revision_required")
        self.assertEqual(second["recovery_outcome"]["type"], "candidate_repair_required")


class TypedFinalAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_transaction_and_replay_are_exactly_once(self):
        store = _MemoryStore(_durable(final=True))
        auditor = AsyncMock(return_value={"validation_evidence": _validation("final-validation-1")})
        audit = TypedFinalAudit(
            data_path="/fixture",
            auditor=auditor,
            gate_evaluator=lambda *_args: {"passed": True, "classification": "fixture_pass"},
            loader=store.load,
            saver=store.save,
        )
        state = {
            "task_id": "task-typed",
            "commit_step_outcome": _commit(),
            "verification_outcome": _verification(),
            "execute_outcome": _execute_outcome(0),
            "next_step": "generate",
            "dispatch_route": "recovery",
            "route_resolution": {"dispatch_route": "planner"},
            "required_next_action": "COMPLETE_TASK",
            "pending_recovery_action": {"type": "complete_task"},
            "recovery_decision": {"target_route": "finish", "next_route": "finish"},
        }
        with (
            patch.object(BioAnalystAgent, "_apply_transition_guard", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_resolve_and_commit_effective_transition", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_dispatch_effective_route", side_effect=AssertionError),
        ):
            first = await audit.run(state)
            second = await audit.run(state)
        self.assertEqual(first, second)
        self.assertEqual(first["final_audit_outcome"]["type"], "task_verified")
        self.assertEqual(store.save_count, 1)
        self.assertEqual(auditor.await_count, 1)
        self.assertEqual(store.value["task_state"]["status"], "completed")
        self.assertEqual(route_final_audit_outcome(first), END)

    async def test_stale_cross_plan_verification_cannot_complete(self):
        store = _MemoryStore(_durable(final=True))
        auditor = AsyncMock(return_value={"validation_evidence": _validation("final-validation-1")})
        result = await TypedFinalAudit(
            data_path="/fixture",
            auditor=auditor,
            gate_evaluator=lambda *_args: {"passed": True},
            loader=store.load,
            saver=store.save,
        ).run({
            "task_id": "task-typed",
            "commit_step_outcome": _commit(),
            "verification_outcome": _verification(plan_version="stale-plan"),
            "execute_outcome": _execute_outcome(0),
        })
        self.assertEqual(result["final_audit_outcome"]["type"], "entry_rejected")
        self.assertEqual(store.save_count, 0)
        self.assertEqual(auditor.await_count, 0)

    async def test_failed_final_gate_is_incomplete_and_non_terminal(self):
        store = _MemoryStore(_durable(final=True))
        result = await TypedFinalAudit(
            data_path="/fixture",
            auditor=AsyncMock(return_value={"validation_evidence": _validation("final-validation-1")}),
            gate_evaluator=lambda *_args: {
                "passed": False,
                "classification": "missing_artifact",
                "missing_artifacts": ["report.md"],
            },
            loader=store.load,
            saver=store.save,
        ).run({
            "task_id": "task-typed",
            "commit_step_outcome": _commit(),
            "verification_outcome": _verification(),
            "execute_outcome": _execute_outcome(0),
        })
        self.assertEqual(result["final_audit_outcome"]["type"], "task_incomplete")
        self.assertEqual(store.save_count, 0)
        self.assertEqual(store.value["task_state"]["status"], "running")


class TypedRuntimeGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_graph_uses_only_typed_edges_under_route_poison(self):
        visits = []
        generate_calls = 0

        async def generate(_state):
            nonlocal generate_calls
            generate_calls += 1
            visits.append("generate")
            if generate_calls > 1:
                return {"generate_outcome": GenerateOutcome(
                    schema_version=1,
                    type="blocked",
                    summary="fixture terminal",
                    scope={"task_id": "task-typed"},
                    payload={"owner": "fixture", "reason": "done", "terminal_receipt": "terminal-1"},
                ).to_dict()}
            action = {
                "type": "execute", "action_id": "action-1", "task_id": "task-typed",
                "plan_id": "plan-typed", "plan_version": "plan-v2", "step_id": "step-1",
            }
            return {"generate_outcome": GenerateOutcome(
                schema_version=1,
                type="action_ready",
                summary="fixture action",
                scope={"task_id": "task-typed"},
                payload={
                    "action": action, "action_id": "action-1",
                    "candidate_fingerprint": "candidate-1",
                    "validation_evidence": {"status": "validated"},
                },
            ).to_dict()}

        async def execute(_state):
            visits.append("execute")
            return {"execute_outcome": _execute_outcome(17)}

        async def recovery(_state):
            visits.append("recovery")
            return {"recovery_outcome": RecoveryOutcome(
                schema_version=1,
                type="candidate_repair_required",
                summary="repair",
                scope={"task_id": "task-typed"},
                payload={
                    "diagnosis": "failed", "failure_owner": "GENERATED_ACTION",
                    "strategy": "repair", "repair_instruction": "change candidate",
                    "source_scope": {"task_id": "task-typed"}, "decision_id": "decision-1",
                    "incident_id": "incident-1", "failure_fingerprint": "failure-1",
                    "budget_receipt": {"ordinal": 1, "limit": 2},
                },
            ).to_dict()}

        async def unreachable(_state):
            raise AssertionError("unexpected typed graph node")

        graph = build_schema2_runtime_graph({
            "generate": generate,
            "execute": execute,
            "supervisor": unreachable,
            "commit_step": unreachable,
            "recovery": recovery,
            "final_audit": unreachable,
        }).compile()
        poison: AgentState = {
            "task_id": "task-typed",
            "next_step": "final_audit",
            "proposed_route": "final_audit",
            "effective_route": "final_audit",
            "dispatch_route": "final_audit",
            "route_resolution": {"dispatch_route": "final_audit"},
            "node_recommended_next_step": "final_audit",
            "required_next_action": "COMPLETE_TASK",
            "pending_recovery_action": {"type": "complete_task"},
            "recovery_decision": {"target_route": "final_audit", "next_route": "final_audit"},
        }
        with (
            patch.object(BioAnalystAgent, "_apply_transition_guard", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_resolve_and_commit_effective_transition", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_dispatch_effective_route", side_effect=AssertionError),
        ):
            await graph.ainvoke(poison)
        self.assertEqual(visits, ["generate", "execute", "recovery", "generate"])


class StageDSourceInvariantTests(unittest.TestCase):
    def test_typed_recovery_and_final_audit_have_no_controller_calls(self):
        source = inspect.getsource(typed_recovery_module) + inspect.getsource(typed_final_audit_module)
        for forbidden in (
            "_apply_transition_guard",
            "_resolve_and_commit_effective_transition",
            "_dispatch_effective_route",
            "RecoveryDecision(",
        ):
            self.assertNotIn(forbidden, source)

    def test_all_recovery_destinations_are_graph_owned(self):
        expected = {
            "candidate_repair_required": "generate",
            "plan_revision_required": "planner",
            "reconsultation_required": "consultant",
            "revalidation_required": "supervisor",
            "user_input_required": "ask_user",
            "blocked": END,
            "internal_protocol_error": END,
            "harness_error": END,
        }
        for outcome_type, destination in expected.items():
            payload = {
                key: False if key == "requires_user_input" else [] if key in {
                    "contract_violations", "missing_facts", "new_evidence_refs"
                } else {} if key in {
                    "source_scope", "budget_receipt", "blocked_evidence", "identity_conflict"
                } else f"fixture-{key}"
                for key in RecoveryOutcome.REQUIRED_PAYLOAD[outcome_type]
            }
            outcome = RecoveryOutcome(
                schema_version=1,
                type=outcome_type,
                summary="fixture",
                scope={"task_id": "task-typed"},
                payload=payload,
            )
            self.assertEqual(route_recovery_outcome({"recovery_outcome": outcome.to_dict()}), destination)


if __name__ == "__main__":
    unittest.main()

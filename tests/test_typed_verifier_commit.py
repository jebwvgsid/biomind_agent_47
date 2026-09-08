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
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    VerificationOutcome,
)
from agent_core.workflow.runtime_graph import (
    route_commit_step_outcome,
    route_verification_outcome,
)
from agent_core.workflow.verifier import TypedSupervisor
from bio_agents import BioAnalystAgent


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


class TypedSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_poison_and_controller_poison_cannot_change_edge(self):
        durable = _durable_state()
        verifier = AsyncMock(return_value={
            "validation_evidence": _validation(),
            "next_step": "finish",
            "proposed_route": "recovery",
            "effective_route": "planner",
            "dispatch_route": "finish",
            "route_resolution": {"dispatch_route": "finish"},
            "required_next_action": "REPLAN",
            "pending_recovery_action": {"type": "blocked"},
            "recovery_decision": {"target_route": "planner", "next_route": "finish"},
        })
        adapter = TypedSupervisor(
            data_path="/fixture",
            verifier=verifier,
            gate_evaluator=lambda *_args: {
                "passed": True,
                "classification": "fixture_pass",
                "plan_version": "plan-v2",
            },
        )
        poisoned_state = {
            "task_id": "task-typed",
            "execution_evidence": {
                "step_id": "step-1", "action_id": "action-1", "attempt_id": "attempt-1"
            },
            "next_step": "finish",
            "effective_route": "planner",
            "dispatch_route": "finish",
            "route_resolution": {"dispatch_route": "finish"},
        }
        with (
            patch("agent_core.workflow.verifier.load_plan_state", return_value=durable),
            patch.object(
                BioAnalystAgent,
                "_apply_transition_guard",
                side_effect=AssertionError("typed Supervisor called Controller guard"),
            ) as guard,
            patch.object(
                BioAnalystAgent,
                "_resolve_and_commit_effective_transition",
                side_effect=AssertionError("typed Supervisor called Controller resolver"),
            ) as resolver,
            patch.object(
                BioAnalystAgent,
                "_dispatch_effective_route",
                side_effect=AssertionError("typed Supervisor called Controller dispatcher"),
            ) as dispatcher,
        ):
            result = await adapter.run(poisoned_state)
        self.assertEqual(set(result), {"verification_outcome"})
        self.assertEqual(result["verification_outcome"]["type"], "verified")
        self.assertEqual(
            route_verification_outcome({**poisoned_state, **result}), "commit_step"
        )
        guard.assert_not_called()
        resolver.assert_not_called()
        dispatcher.assert_not_called()

    async def test_probe_is_diagnostic_and_hard_invalid_cannot_be_elevated(self):
        durable = _durable_state()
        with patch("agent_core.workflow.verifier.load_plan_state", return_value=durable):
            probe = await TypedSupervisor(
                data_path="/fixture",
                verifier=AsyncMock(return_value={"validation_evidence": _validation(role="probe")}),
            ).run({"task_id": "task-typed"})
            invalid = await TypedSupervisor(
                data_path="/fixture",
                verifier=AsyncMock(return_value={"validation_evidence": _validation(status="INVALID")}),
            ).run({"task_id": "task-typed"})
        self.assertEqual(probe["verification_outcome"]["type"], "diagnostic_evidence_recorded")
        self.assertEqual(invalid["verification_outcome"]["type"], "rejected")
        self.assertEqual(route_verification_outcome(probe), "recovery")
        self.assertEqual(route_verification_outcome(invalid), "recovery")

    async def test_artifact_validation_is_projected_for_gate_without_precommit_write(self):
        durable = _durable_state()
        durable["steps"][0].update({
            "outputs": ["plan.result"],
            "output_artifact_ids": ["artifact-result"],
            "step_effect": {
                "schema_version": 1,
                "produces_artifacts": ["plan.result"],
            },
        })
        attempt = durable["provenance_state"]["attempts"]["attempt-1"]
        attempt["artifact_version_ids"] = ["artifact-result@attempt-1"]
        captured_gate_state = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.tsv"
            output.write_text("value\n", encoding="utf-8")
            durable["provenance_state"]["artifact_heads"] = {
                "artifact-result": "artifact-result@attempt-1"
            }
            durable["provenance_state"]["artifacts"] = {
                "artifact-result@attempt-1": {
                    "artifact_id": "artifact-result",
                    "logical_artifact_id": "plan.result",
                    "artifact_version_id": "artifact-result@attempt-1",
                    "producer_step_id": "step-1",
                    "producer_action_id": "action-1",
                    "producer_attempt_id": "attempt-1",
                    "status": "materialized",
                    "validation_status": "unknown",
                    "path": str(output),
                }
            }

            def gate(_data_path, _task_id, candidate):
                captured_gate_state.update(copy.deepcopy(candidate))
                artifact = candidate["provenance_state"]["artifacts"][
                    "artifact-result@attempt-1"
                ]
                return {
                    "passed": artifact.get("status") == "valid",
                    "classification": "projected_validation",
                    "plan_version": "plan-v2",
                }

            with patch(
                "agent_core.workflow.verifier.load_plan_state",
                return_value=durable,
            ):
                result = await TypedSupervisor(
                    data_path="/fixture",
                    verifier=AsyncMock(return_value={
                        "validation_evidence": _validation()
                    }),
                    gate_evaluator=gate,
                ).run({
                    "task_id": "task-typed",
                    "execution_evidence": {
                        "step_id": "step-1",
                        "action_id": "action-1",
                        "attempt_id": "attempt-1",
                    },
                })

        self.assertEqual(result["verification_outcome"]["type"], "verified")
        projected_artifact = captured_gate_state["provenance_state"]["artifacts"][
            "artifact-result@attempt-1"
        ]
        self.assertEqual(projected_artifact["status"], "valid")
        self.assertEqual(
            projected_artifact["validation_id"], "validation-1"
        )
        # TypedSupervisor does not steal CommitStep's atomic CAS write.
        self.assertEqual(
            durable["provenance_state"]["artifacts"][
                "artifact-result@attempt-1"
            ]["status"],
            "materialized",
        )


class CommitStepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = _MemoryStore(_durable_state())
        self.committer = CommitStep(
            data_path="/fixture",
            gate_evaluator=lambda *_args: {
                "passed": True,
                "plan_version": "plan-v2",
            },
        )
        self.patch_load = patch(
            "agent_core.workflow.commit_step.load_plan_state", self.store.load
        )
        self.patch_save = patch(
            "agent_core.workflow.commit_step.save_plan_state", self.store.save
        )
        self.patch_load.start()
        self.patch_save.start()

    def tearDown(self):
        self.patch_save.stop()
        self.patch_load.stop()

    async def test_atomic_commit_and_duplicate_replay_advance_once(self):
        state = {
            "task_id": "task-typed",
            "verification_outcome": _verified_outcome(),
            "pending_action": {"action_id": "action-1"},
            "execution_evidence": {
                "step_id": "step-1", "action_id": "action-1", "attempt_id": "attempt-1", "returncode": 0
            },
        }
        first = await self.committer.run(state)
        replay = await self.committer.run(state)
        self.assertEqual(first, replay)
        self.assertEqual(first["commit_step_outcome"]["type"], "next_step_ready")
        self.assertEqual(self.store.save_count, 1)
        self.assertEqual(self.store.value["steps"][0]["status"], "passed")
        self.assertEqual(self.store.value["steps"][1]["status"], "running")
        self.assertEqual(self.store.value["current_step_id"], "step-2")
        self.assertEqual(
            self.store.value["provenance_state"]["attempts"]["attempt-1"]["status"],
            "COMPLETED",
        )
        self.assertNotIn("next_route", self.store.value["task_state"])
        self.assertEqual(
            route_commit_step_outcome({
                **state,
                **first,
                "next_step": "finish",
                "dispatch_route": "recovery",
            }),
            "generate",
        )

    async def test_cas_conflict_has_zero_partial_plan_mutation(self):
        before = copy.deepcopy(self.store.value)
        self.store.conflict_on_save = True
        result = await self.committer.run({
            "task_id": "task-typed",
            "verification_outcome": _verified_outcome(),
        })
        self.assertEqual(result["commit_step_outcome"]["type"], "commit_conflict")
        self.assertEqual(self.store.value["steps"], before["steps"])
        self.assertEqual(self.store.value["current_step_id"], before["current_step_id"])
        self.assertEqual(self.store.value["task_state"], before["task_state"])

    async def test_canonical_cancel_wins_before_late_commitstep(self):
        class Profiler:
            def finish_task(self, *_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory(prefix="commit-cancel-race-") as root:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = root
            agent.state_manager = None
            agent.profiler = Profiler()
            with (
                patch("bio_agents.load_plan_state", self.store.load),
                patch("bio_agents.save_plan_state", self.store.save),
            ):
                receipt = agent._commit_user_cancellation(
                    "task-typed",
                    source="signal",
                    reason="user cancelled before late CommitStep",
                )
            cancelled = copy.deepcopy(self.store.value)
            save_count = self.store.save_count

            fake_decision = SimpleNamespace(
                decision_id="decision-terminal-race",
                disposition="completion_accepted",
            )
            fake_report = SimpleNamespace(
                report_id="report-terminal-race",
                scope={},
                deterministic_veto=False,
                effective_disposition="completion_accepted",
                effective_decision=fake_decision,
                consumption_token="consumption-terminal-race",
            )
            with (
                patch(
                    "agent_core.workflow.commit_step.CompletionReport.from_mapping",
                    return_value=fake_report,
                ),
                patch(
                    "agent_core.workflow.commit_step.SemanticDecision.from_mapping",
                    return_value=fake_decision,
                ),
            ):
                result = await self.committer.run({
                    "task_id": "task-typed",
                    "verification_outcome": _raw_verified_outcome(generation=7),
                })

        self.assertTrue(receipt["receipt_id"].startswith("cancel-"))
        self.assertEqual(
            result["commit_step_outcome"]["payload"]["reason_code"],
            "TERMINAL_STATE_CONFLICT",
        )
        self.assertEqual(self.store.value, cancelled)
        self.assertEqual(self.store.save_count, save_count)

    async def test_completed_commit_wins_before_late_cancel(self):
        # Model the successful terminal authority as the first PlanState CAS.
        # CommitStep itself yields ``plan_complete`` and FinalAudit owns this
        # later terminal write; the cancellation helper must not overwrite it.
        candidate = copy.deepcopy(self.store.value)
        candidate["task_state"]["status"] = "completed"
        self.store.save("/fixture", "task-typed", candidate)
        completed = copy.deepcopy(self.store.value)
        save_count = self.store.save_count
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/unused-terminal-race"
        agent.state_manager = None
        agent.profiler = None
        with (
            patch("bio_agents.load_plan_state", self.store.load),
            patch("bio_agents.save_plan_state", self.store.save),
        ):
            cancellation = agent._commit_user_cancellation(
                "task-typed",
                source="signal",
                reason="late cancellation after completed CAS",
            )

        self.assertEqual(completed["task_state"]["status"], "completed")
        self.assertEqual(
            cancellation["status"],
            "not_committed_terminal_already_authoritative",
        )
        self.assertEqual(cancellation["authoritative_task_status"], "completed")
        self.assertEqual(self.store.value, completed)
        self.assertEqual(self.store.save_count, save_count)

    async def test_artifact_and_binding_heads_are_accepted_in_same_write(self):
        attempt = self.store.value["provenance_state"]["attempts"]["attempt-1"]
        attempt["artifact_version_ids"] = ["artifact-report@attempt-1"]
        self.store.value["provenance_state"]["artifact_heads"] = {
            "artifact-report": "artifact-report@attempt-1"
        }
        self.store.value["provenance_state"]["artifacts"] = {
            "artifact-report@attempt-1": {
                "artifact_id": "artifact-report",
                "artifact_version_id": "artifact-report@attempt-1",
                "producer_step_id": "step-1",
                "producer_action_id": "action-1",
                "producer_attempt_id": "attempt-1",
                "status": "materialized",
            }
        }
        state = {
            "task_id": "task-typed",
            "verification_outcome": _verified_outcome(),
            "pending_action": {
                "action_id": "action-1",
                "produced_bindings": {
                    "assembly": {"type": "string", "value": "fixture-v1"}
                },
            },
            "execution_evidence": {
                "step_id": "step-1",
                "action_id": "action-1",
                "attempt_id": "attempt-1",
                "returncode": 0,
            },
        }
        result = await self.committer.run(state)
        self.assertEqual(result["commit_step_outcome"]["type"], "next_step_ready")
        task_state = self.store.value["task_state"]
        binding_id = task_state["accepted_plan_bindings"]["assembly"]
        binding = next(
            row for row in task_state["plan_binding_history"]
            if row["binding_id"] == binding_id
        )
        self.assertEqual(binding["producer_execution_attempt_id"], "attempt-1")
        self.assertEqual(
            self.store.value["provenance_state"]["artifact_heads"]["artifact-report"],
            "artifact-report@attempt-1",
        )
        self.assertEqual(
            self.store.value["provenance_state"]["artifacts"]["artifact-report@attempt-1"]["status"],
            "valid",
        )

    async def test_declared_binding_effect_is_gated_and_persisted_atomically(self):
        binding_candidate = {
            "assembly": {"type": "string", "value": "fixture-v1"},
        }
        step = self.store.value["steps"][0]
        step.update({
            "outputs": [],
            "output_artifact_ids": [],
            "success_criteria": [],
            "step_effect": {
                "schema_version": 1,
                "produces_bindings": ["assembly"],
            },
        })
        attempt = self.store.value["provenance_state"]["attempts"]["attempt-1"]
        attempt.update({
            "returncode": 0,
            "declared_step_effect": copy.deepcopy(step["step_effect"]),
            "effect_evidence": {
                "produces_bindings": [{
                    "binding_id": "assembly",
                    "status": "candidate",
                    "source": "generated_action",
                }],
            },
        })
        self.store.value["provenance_state"]["actions"]["action-1"] = {
            "action_id": "action-1",
            "normalized_action": {
                "action_type": "execute",
                "produced_bindings": copy.deepcopy(binding_candidate),
            },
        }
        verified = _verified_outcome()
        verified["payload"]["validation_evidence"]["effect_results"] = {
            "produces_bindings": [{
                "binding_id": "assembly",
                "status": "candidate",
                "source": "generated_action",
            }],
        }
        projected = copy.deepcopy(self.store.value)
        projected_validation = ValidationEvidence.from_mapping(
            verified["payload"]["validation_evidence"]
        )
        record_provenance_event(projected, "validation_recorded", {
            "attempt_id": "attempt-1",
            "validation": projected_validation.to_dict(),
            "validation_evidence_ref": projected_validation.validation_id,
        })
        projected_gate = CompletionGate.validate_current_step(
            "/fixture", "task-typed", projected
        ).to_dict()
        verified["payload"]["completion_gate"] = projected_gate
        verified = _attach_semantic_report(
            verified,
            self.store.value,
            completion_gate=projected_gate,
        )
        self.committer.gate_evaluator = (
            lambda data_path, task_id, state: CompletionGate.validate_current_step(
                data_path,
                task_id,
                state,
            ).to_dict()
        )

        result = await self.committer.run({
            "task_id": "task-typed",
            "verification_outcome": verified,
            "pending_action": {
                "action_id": "action-1",
                "produced_bindings": copy.deepcopy(binding_candidate),
            },
            "execution_evidence": {
                "step_id": "step-1",
                "action_id": "action-1",
                "attempt_id": "attempt-1",
                "returncode": 0,
            },
        })

        self.assertEqual(result["commit_step_outcome"]["type"], "next_step_ready")
        task_state = self.store.value["task_state"]
        binding_id = task_state["accepted_plan_bindings"]["assembly"]
        binding = next(
            row for row in task_state["plan_binding_history"]
            if row["binding_id"] == binding_id
        )
        self.assertEqual(binding["producer_step_id"], "step-1")
        self.assertEqual(binding["producer_action_id"], "action-1")
        self.assertEqual(binding["producer_execution_attempt_id"], "attempt-1")
        self.assertEqual(
            result["commit_step_outcome"]["payload"]["accepted_binding_refs"],
            [binding_id],
        )

    async def test_foreign_artifact_head_rejects_without_save(self):
        attempt = self.store.value["provenance_state"]["attempts"]["attempt-1"]
        attempt["artifact_version_ids"] = ["artifact-foreign@attempt-other"]
        self.store.value["provenance_state"]["artifacts"] = {
            "artifact-foreign@attempt-other": {
                "producer_step_id": "step-1",
                "producer_action_id": "action-1",
                "producer_attempt_id": "attempt-other",
            }
        }
        before = copy.deepcopy(self.store.value)
        result = await self.committer.run({
            "task_id": "task-typed",
            "verification_outcome": _verified_outcome(),
        })
        self.assertEqual(result["commit_step_outcome"]["type"], "contract_rejected")
        self.assertEqual(self.store.value, before)
        self.assertEqual(self.store.save_count, 0)

    async def test_wrong_plan_step_attempt_and_probe_never_commit(self):
        mutations = []
        wrong_plan = _verified_outcome()
        wrong_plan["scope"]["plan_version"] = "other-plan"
        mutations.append(wrong_plan)
        wrong_step = _verified_outcome()
        wrong_step["scope"]["step_id"] = "step-other"
        wrong_step["payload"]["validation_evidence"]["step_id"] = "step-other"
        mutations.append(wrong_step)
        wrong_attempt = _verified_outcome()
        wrong_attempt["scope"]["attempt_id"] = "attempt-other"
        wrong_attempt["payload"]["validation_evidence"]["attempt_id"] = "attempt-other"
        mutations.append(wrong_attempt)
        mutations.append(_verified_outcome(role="probe"))
        for outcome in mutations:
            with self.subTest(scope=outcome["scope"]):
                before = copy.deepcopy(self.store.value)
                result = await self.committer.run({
                    "task_id": "task-typed", "verification_outcome": outcome
                })
                self.assertEqual(result["commit_step_outcome"]["type"], "contract_rejected")
                self.assertEqual(self.store.value, before)
        self.assertEqual(self.store.save_count, 0)

    async def test_stale_generation_conflicts_before_candidate_mutation(self):
        result = await self.committer.run({
            "task_id": "task-typed",
            "verification_outcome": _verified_outcome(generation=6),
        })
        self.assertEqual(result["commit_step_outcome"]["type"], "commit_conflict")
        self.assertEqual(self.store.save_count, 0)


class TypedRuntimeMappingTests(unittest.TestCase):
    def test_all_stage_b_outcomes_have_graph_owned_destinations(self):
        verification_destinations = {
            "verified": "commit_step",
            "verified_with_warnings": "commit_step",
            "rejected": "recovery",
            "incomplete_evidence": "recovery",
            "diagnostic_evidence_recorded": "recovery",
            "verification_error": "recovery",
            "inspection_required": "recovery",
            "correction_required": "recovery",
            "plan_revision_required": "planner",
            "user_information_required": "ask_user",
            "assessment_pending": "recovery",
            "harness_error": END,
        }
        for outcome_type, expected in verification_destinations.items():
            payload = {
                key: f"fixture-{key}"
                for key in VerificationOutcome.REQUIRED_PAYLOAD[outcome_type]
            }
            outcome = VerificationOutcome(
                schema_version=1,
                type=outcome_type,
                summary="fixture",
                scope={"task_id": "task-typed"},
                payload=payload,
            )
            self.assertEqual(
                route_verification_outcome({
                    "verification_outcome": outcome.to_dict(),
                    "next_step": "poison",
                    "recovery_decision": {"target_route": "poison"},
                }),
                expected,
            )


class CompiledStageBGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_supervisor_edge_ignores_all_route_poison(self):
        workflow = StateGraph(AgentState)

        async def supervisor_node(_state):
            return {"verification_outcome": _verified_outcome()}

        async def commit_node(_state):
            return {"commit_step_outcome": CommitStepOutcome(
                schema_version=1,
                type="plan_complete",
                summary="compiled edge reached CommitStep",
                scope={"task_id": "task-typed"},
                payload={
                    "commit_id": "commit-compiled",
                    "receipt": "receipt-compiled",
                    "before_state_generation": 7,
                    "after_state_generation": 8,
                    "completed_step_id": "step-1",
                },
            ).to_dict()}

        async def recovery_node(_state):
            raise AssertionError("route poison reached Recovery")

        async def verification_route(state):
            return route_verification_outcome(state)

        workflow.add_node("supervisor", supervisor_node)
        workflow.add_node("commit_step", commit_node)
        workflow.add_node("recovery", recovery_node)
        workflow.add_edge(START, "supervisor")
        workflow.add_conditional_edges(
            "supervisor",
            verification_route,
            ["commit_step", "recovery", END],
        )
        workflow.add_edge("commit_step", END)
        workflow.add_edge("recovery", END)
        app = workflow.compile()
        result = await app.ainvoke({
            "task_id": "task-typed",
            "next_step": "recovery",
            "proposed_route": "recovery",
            "effective_route": "recovery",
            "dispatch_route": "recovery",
            "route_resolution": {"dispatch_route": "recovery"},
            "required_next_action": "REPLAN",
            "pending_recovery_action": {"type": "blocked"},
            "recovery_decision": {"target_route": "recovery", "next_route": "recovery"},
        })
        self.assertEqual(result["commit_step_outcome"]["type"], "plan_complete")

        commit_destinations = {
            "next_step_ready": "generate",
            "plan_complete": "final_audit",
            "commit_conflict": "recovery",
            "contract_rejected": "recovery",
            "harness_error": END,
        }
        for outcome_type, expected in commit_destinations.items():
            payload = {
                key: f"fixture-{key}"
                for key in CommitStepOutcome.REQUIRED_PAYLOAD[outcome_type]
            }
            outcome = CommitStepOutcome(
                schema_version=1,
                type=outcome_type,
                summary="fixture",
                scope={"task_id": "task-typed"},
                payload=payload,
            )
            self.assertEqual(
                route_commit_step_outcome({
                    "commit_step_outcome": outcome.to_dict(),
                    "dispatch_route": "poison",
                }),
                expected,
            )


if __name__ == "__main__":
    unittest.main()

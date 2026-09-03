from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent_core.state import AgentState
import agent_core.workflow.execution as typed_execution_module
from agent_core.workflow.execution import (
    TypedExecute,
    TypedGenerate,
    execute_harness_error,
)
from agent_core.workflow.outcomes import ExecuteOutcome, GenerateOutcome
from agent_core.workflow.runtime_graph import (
    route_execute_outcome,
    route_generate_outcome,
)
from agent_core.workflow.typed_node import profile_typed_node
from bio_agents import BioAnalystAgent


def _action() -> dict:
    return {
        "type": "execute",
        "action_id": "action-1",
        "task_id": "task-typed",
        "plan_id": "plan-typed",
        "plan_version": "plan-v2",
        "step_id": "step-1",
        "candidate_fingerprint": "candidate-action-1",
        "code": "printf ok",
        "language": "bash",
    }


def _action_outcome() -> dict:
    action = _action()
    return GenerateOutcome(
        schema_version=1,
        type="action_ready",
        summary="action ready",
        scope={
            "task_id": "task-typed",
            "plan_id": "plan-typed",
            "plan_version": "plan-v2",
            "step_id": "step-1",
        },
        payload={
            "action": action,
            "action_id": "action-1",
            "candidate_fingerprint": "candidate-action-1",
            "validation_evidence": {"status": "validated"},
        },
    ).to_dict()


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
        "execution_status": "succeeded" if returncode == 0 else "failed",
        "command_role": "analysis_execution",
        "validation": {"passed": returncode == 0},
        "execution_record_path": "attempts/attempt-1/execution_record.json",
    }


class TypedGenerateTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_route_poison_and_controller_poison_are_ignored(self):
        async def generator(_state):
            return {
                "pending_action": _action(),
                "generate_result": "GENERATED_ACTION",
                "generate_exit_reason": "generated_action",
                "next_step": "finish",
                "proposed_route": "planner",
                "effective_route": "finish",
                "dispatch_route": "finish",
                "route_resolution": {"dispatch_route": "finish"},
                "required_next_action": "REPLAN",
                "pending_recovery_action": {"type": "blocked"},
                "recovery_decision": {"target_route": "planner", "next_route": "finish"},
            }

        adapter = TypedGenerate(generator=generator)
        with (
            patch.object(BioAnalystAgent, "_apply_transition_guard", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_resolve_and_commit_effective_transition", side_effect=AssertionError),
            patch.object(BioAnalystAgent, "_dispatch_effective_route", side_effect=AssertionError),
        ):
            result = await adapter.run({
                "task_id": "task-typed",
                "next_step": "finish",
                "dispatch_route": "finish",
            })
        self.assertEqual(set(result), {"generate_outcome"})
        self.assertEqual(result["generate_outcome"]["type"], "action_ready")
        self.assertEqual(route_generate_outcome(result), "execute")

    async def test_bounded_corrective_loop_requires_changed_fingerprint(self):
        candidate = {"fingerprint": "candidate-a", "error": "schema-a"}

        async def rejected(_state):
            return {
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "unrecognized_action",
                "failure_owner": "GENERATED_ACTION",
                "action_format_repair": {
                    "step_id": "step-1",
                    "candidate_fingerprint": candidate["fingerprint"],
                    "error_fingerprint": candidate["error"],
                    "feedback": "return a valid action object",
                },
                "next_step": "generate",
            }

        adapter = TypedGenerate(generator=rejected, repair_limit=2)
        first = await adapter.run({"task_id": "task-typed", "current_step_id": "step-1"})
        self.assertEqual(first["generate_outcome"]["type"], "candidate_rejected")
        self.assertEqual(route_generate_outcome(first), "generate")

        candidate.update({"fingerprint": "candidate-b", "error": "schema-b"})
        changed = await adapter.run({
            "task_id": "task-typed",
            "current_step_id": "step-1",
            **first,
        })
        self.assertEqual(changed["generate_outcome"]["type"], "candidate_rejected")
        self.assertEqual(
            changed["generate_outcome"]["payload"]["budget_receipt"]["ordinal"], 2
        )
        self.assertEqual(route_generate_outcome(changed), "generate")

        repeated = await adapter.run({
            "task_id": "task-typed",
            "current_step_id": "step-1",
            **changed,
        })
        self.assertEqual(repeated["generate_outcome"]["type"], "internal_protocol_error")
        self.assertEqual(route_generate_outcome(repeated), END)

    async def test_identical_candidate_fails_closed_on_first_reentry(self):
        async def rejected(_state):
            return {
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "binding_operation_contract_mismatch",
                "failure_owner": "GENERATED_ACTION",
                "action_format_repair": {
                    "step_id": "step-1",
                    "candidate_fingerprint": "same-candidate",
                    "error_fingerprint": "same-error",
                    "feedback": "repair binding declaration",
                },
            }

        adapter = TypedGenerate(generator=rejected)
        first = await adapter.run({"task_id": "task-typed", "current_step_id": "step-1"})
        second = await adapter.run({"task_id": "task-typed", **first})
        self.assertEqual(first["generate_outcome"]["type"], "candidate_rejected")
        self.assertEqual(second["generate_outcome"]["type"], "internal_protocol_error")
        self.assertEqual(
            second["generate_outcome"]["payload"]["error_code"],
            "GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED",
        )

    async def test_internal_failures_and_ambiguous_continuation_do_not_reenter(self):
        for legacy_result in (
            {
                "generate_result": "BLOCKED",
                "generate_exit_reason": "recovery_execution_contract_invalid",
                "next_step": "generate",
            },
            {
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "unknown_legacy_branch",
                "next_step": "generate",
            },
        ):
            async def generator(_state, result=legacy_result):
                return result

            outcome = await TypedGenerate(generator=generator).run({"task_id": "task-typed"})
            self.assertEqual(outcome["generate_outcome"]["type"], "internal_protocol_error")
            self.assertEqual(route_generate_outcome(outcome), END)


class TypedExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_failure_use_persisted_execution_fact_only(self):
        for returncode, expected_type, expected_destination in (
            (0, "execution_succeeded", "supervisor"),
            (17, "execution_failed", "recovery"),
        ):
            calls = []

            async def executor(state):
                calls.append(state["pending_action"]["action_id"])
                return {
                    "execution_evidence": _execution(returncode),
                    "next_step": "finish",
                    "dispatch_route": "finish",
                    "route_resolution": {"dispatch_route": "finish"},
                    "recovery_decision": {"target_route": "finish"},
                }

            with (
                patch.object(BioAnalystAgent, "_apply_transition_guard", side_effect=AssertionError),
                patch.object(BioAnalystAgent, "_resolve_and_commit_effective_transition", side_effect=AssertionError),
                patch.object(BioAnalystAgent, "_dispatch_effective_route", side_effect=AssertionError),
            ):
                result = await TypedExecute(executor=executor).run({
                    "task_id": "task-typed",
                    "generate_outcome": _action_outcome(),
                })
            self.assertEqual(calls, ["action-1"])
            self.assertEqual(result["execute_outcome"]["type"], expected_type)
            self.assertEqual(route_execute_outcome(result), expected_destination)
            self.assertEqual(
                result["execute_outcome"]["payload"]["execution_record"]["execution_record_path"],
                "attempts/attempt-1/execution_record.json",
            )

    async def test_real_graph_interrupt_pause_resume_launches_once(self):
        counters = {"attempts": 0, "launches": 0, "receipts": 0}

        async def interrupting_executor(state):
            answer = interrupt({
                "type": "execute_approval",
                "action_id": state["pending_action"]["action_id"],
            })
            if answer != "approve":
                return {
                    "approval_decision": {
                        "approval_request_id": "approval-1",
                        "guidance": str(answer),
                        "command_started": False,
                    }
                }
            counters["attempts"] += 1
            counters["launches"] += 1
            counters["receipts"] += 1
            return {"execution_evidence": _execution(0)}

        wrapped = profile_typed_node(
            "execute",
            TypedExecute(executor=interrupting_executor).run,
            outcome_field="execute_outcome",
            outcome_contract=ExecuteOutcome,
            harness_error_factory=execute_harness_error,
        )
        workflow = StateGraph(AgentState)
        workflow.add_node("execute", wrapped)
        workflow.add_edge(START, "execute")
        workflow.add_edge("execute", END)
        app = workflow.compile(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "typed-execute-pause"}}
        paused = await app.ainvoke({
            "task_id": "task-typed",
            "generate_outcome": _action_outcome(),
        }, config)
        self.assertTrue(paused.get("__interrupt__"))
        self.assertEqual(counters, {"attempts": 0, "launches": 0, "receipts": 0})
        resumed = await app.ainvoke(Command(resume="approve"), config)
        self.assertEqual(resumed["execute_outcome"]["type"], "execution_succeeded")
        self.assertEqual(counters, {"attempts": 1, "launches": 1, "receipts": 1})

    async def test_prelaunch_python_failure_is_harness_not_recovery(self):
        launches = 0

        async def broken(_state):
            nonlocal launches
            raise RuntimeError("fixture prelaunch bug")

        wrapped = profile_typed_node(
            "execute",
            TypedExecute(executor=broken).run,
            outcome_field="execute_outcome",
            outcome_contract=ExecuteOutcome,
            harness_error_factory=execute_harness_error,
        )
        result = await wrapped({
            "task_id": "task-typed", "generate_outcome": _action_outcome()
        })
        self.assertEqual(launches, 0)
        self.assertEqual(result["execute_outcome"]["type"], "harness_error")
        self.assertEqual(route_execute_outcome(result), END)

    async def test_stale_replay_never_launches_or_self_routes(self):
        launches = 0

        async def stale(_state):
            return {
                "stale_node_result": {
                    "status": "discarded",
                    "action_id": "action-1",
                    "attempt_id": "not-started",
                    "changed_preconditions": ["plan_version"],
                    "counts_as_failure": False,
                },
                "next_step": "execute",
            }

        adapter = TypedExecute(executor=stale)
        for _ in range(2):
            result = await adapter.run({
                "task_id": "task-typed", "generate_outcome": _action_outcome()
            })
            self.assertEqual(result["execute_outcome"]["type"], "stale_result")
            self.assertEqual(route_execute_outcome(result), "planner")
        self.assertEqual(launches, 0)

    async def test_cancelled_error_identity_is_preserved(self):
        cancelled = asyncio.CancelledError()

        async def executor(_state):
            raise cancelled

        with self.assertRaises(asyncio.CancelledError) as raised:
            await TypedExecute(executor=executor).run({
                "task_id": "task-typed", "generate_outcome": _action_outcome()
            })
        self.assertIs(raised.exception, cancelled)


class StageCMappingTests(unittest.TestCase):
    def test_typed_execution_handlers_do_not_reference_controller_routing_core(self):
        source = inspect.getsource(typed_execution_module)
        for forbidden in (
            "_apply_transition_guard",
            "_resolve_and_commit_effective_transition",
            "_dispatch_effective_route",
        ):
            self.assertNotIn(forbidden, source)

    def test_execute_has_no_generic_self_edge(self):
        destinations = set()
        for outcome_type in ExecuteOutcome.ALLOWED_TYPES:
            payload = {
                key: False if key in {"command_started", "counts_as_failure"} else f"fixture-{key}"
                for key in ExecuteOutcome.REQUIRED_PAYLOAD[outcome_type]
            }
            if outcome_type == "pre_execution_rejected":
                payload["violation_owner"] = "INTERNAL_PROTOCOL"
            outcome = ExecuteOutcome(
                schema_version=1,
                type=outcome_type,
                summary="fixture",
                scope={"task_id": "task-typed"},
                payload=payload,
            )
            destinations.add(route_execute_outcome({"execute_outcome": outcome.to_dict()}))
        self.assertNotIn("execute", destinations)


if __name__ == "__main__":
    unittest.main()

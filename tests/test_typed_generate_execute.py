from __future__ import annotations

import asyncio
import inspect
import tempfile
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
from agent_core.workflow.candidate_repair import (
    candidate_repair_error_fingerprint,
    candidate_repair_fingerprint,
    candidate_repair_rejection_event_id,
    candidate_repair_scope,
    issue_candidate_repair_receipt,
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


def _trusted_rejection(
    data_path: str,
    *,
    task_id: str = "task-typed",
    step_id: str = "step-1",
    candidate: dict | str,
    ordinal: int,
    event_suffix: str,
    repair_state: dict | None = None,
) -> tuple[dict, dict]:
    scope = candidate_repair_scope(
        {
            "task_id": task_id,
            "plan_id": "",
            "plan_version": "",
            "plan_revision": 0,
            "task_revision": 0,
            "current_step_id": step_id,
        },
        task_id=task_id,
        step_id=step_id,
    )
    violations = [{
        "code": "ACTION_SCHEMA_INVALID",
        "owner": "GENERATED_ACTION",
        "message": "Return a valid current-step action object.",
        "field_path": "response",
    }]
    contract_ref = {"protocol": "semantic_action_v2"}
    candidate_fingerprint = candidate_repair_fingerprint(candidate)
    error_fingerprint = candidate_repair_error_fingerprint(
        repair_stage="action_schema",
        validation_source="test.validator",
        contract_ref=contract_ref,
        exact_violations=violations,
    )
    candidate_event_id = f"generate-candidate:{event_suffix}"
    event_id = candidate_repair_rejection_event_id(
        scope=scope,
        candidate_event_id=candidate_event_id,
        candidate_fingerprint=candidate_fingerprint,
        error_fingerprint=error_fingerprint,
        repair_stage="action_schema",
    )
    receipt = issue_candidate_repair_receipt(
        data_path=data_path,
        task_id=task_id,
        scope=scope,
        candidate_fingerprint=candidate_fingerprint,
        error_fingerprint=error_fingerprint,
        violation_owner="GENERATED_ACTION",
        feedback="Return a valid current-step action object.",
        repair_domain="representation",
        repair_stage="action_schema",
        repair_ordinal=ordinal,
        validation_source="test.validator",
        contract_ref=contract_ref,
        exact_violations=violations,
        previous_candidate=candidate,
        candidate_event_id=candidate_event_id,
        rejection_event_id=event_id,
        repair_limit=2,
        progress_proven=True,
    )
    state = dict(repair_state or {"schema_version": 1, "events": {}})
    events = dict(state.get("events") or {})
    events[event_id] = {
        "event_id": event_id,
        "scope": scope,
        "candidate_event_id": candidate_event_id,
        "candidate_fingerprint": candidate_fingerprint,
        "error_fingerprint": error_fingerprint,
        "repair_ordinal": ordinal,
        "repair_limit": 2,
        "status": "repairable",
        "receipt_id": receipt["receipt_id"],
        "receipt_ref": receipt["receipt_ref"],
    }
    state["events"] = events
    raw = {
        "generate_result": "NEEDS_RECOVERY",
        "generate_exit_reason": "unrecognized_action",
        "failure_owner": "GENERATED_ACTION",
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_repair_receipt": receipt,
    }
    return raw, state


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
        with tempfile.TemporaryDirectory() as data_path:
            raw_first, repair_state = _trusted_rejection(
                data_path,
                candidate={"fingerprint": "candidate-a"},
                ordinal=1,
                event_suffix="one",
            )

            async def first_rejection(_state):
                return raw_first

            adapter = TypedGenerate(generator=first_rejection, repair_limit=2)
            first = await adapter.run({
                "task_id": "task-typed",
                "current_step_id": "step-1",
                "task_state": {"candidate_repair_state": repair_state},
            })
            self.assertEqual(first["generate_outcome"]["type"], "candidate_rejected")
            self.assertEqual(route_generate_outcome(first), "generate")

            raw_changed, repair_state = _trusted_rejection(
                data_path,
                candidate={"fingerprint": "candidate-b"},
                ordinal=2,
                event_suffix="two",
                repair_state=repair_state,
            )

            async def changed_rejection(_state):
                return raw_changed

            changed = await TypedGenerate(
                generator=changed_rejection,
                repair_limit=2,
            ).run({
                "task_id": "task-typed",
                "current_step_id": "step-1",
                "task_state": {"candidate_repair_state": repair_state},
                **first,
            })
            self.assertEqual(changed["generate_outcome"]["type"], "candidate_rejected")
            self.assertEqual(
                changed["generate_outcome"]["payload"]["budget_receipt"]["ordinal"], 2
            )
            self.assertEqual(route_generate_outcome(changed), "generate")

    async def test_identical_candidate_fails_closed_on_first_reentry(self):
        with tempfile.TemporaryDirectory() as data_path:
            raw_first, repair_state = _trusted_rejection(
                data_path,
                candidate={"fingerprint": "same-candidate"},
                ordinal=1,
                event_suffix="one",
            )

            async def first_rejection(_state):
                return raw_first

            first = await TypedGenerate(generator=first_rejection).run({
                "task_id": "task-typed",
                "current_step_id": "step-1",
                "task_state": {"candidate_repair_state": repair_state},
            })

            async def exhausted(_state):
                return {
                    "generate_result": "BLOCKED",
                    "generate_exit_reason": "binding_operation_contract_mismatch_exhausted",
                    "recovery_status": "INTERNAL_PROTOCOL_ERROR",
                    "reason_code": "GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED",
                    "primary_failure": {
                        "error_code": "ACTION_OPERATION_SCOPE_MISMATCH",
                        "execution_started": False,
                    },
                    "execution_status": "not_started",
                    "recovery_entered": False,
                    "outputs_produced": [],
                }

            second = await TypedGenerate(generator=exhausted).run({
                "task_id": "task-typed",
                "current_step_id": "step-1",
                **first,
            })
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

    async def test_prelaunch_primary_cause_detail_reaches_typed_logger(self):
        detail = (
            "ValueError: Unsupported provenance event: "
            "workspace_mutation_intent_recorded"
        )

        async def terminal_failure(_state):
            return {
                "terminal_internal_protocol_failure": {
                    "primary_failure": {
                        "error_code": "EXECUTE_PREPARATION_INTERNAL_ERROR",
                        "exception_type": "ValueError",
                        "phase": "mutation_provenance_registration",
                        "component": (
                            "agent_core.plan_tracker.record_provenance_event"
                        ),
                        "failed_event_type": (
                            "workspace_mutation_intent_recorded"
                        ),
                        "attempt_id": "attempt-1",
                        "last_successful_phase": "mutation_intent_journal",
                        "detail": detail,
                        "diagnostic_ref": "object:sha256:fixture",
                        "exception_diagnostic": {"detail": detail},
                        "command_started": False,
                    },
                    "secondary_failures": [],
                },
                "execution_started": False,
            }

        wrapped = profile_typed_node(
            "execute",
            TypedExecute(executor=terminal_failure).run,
            outcome_field="execute_outcome",
            outcome_contract=ExecuteOutcome,
            harness_error_factory=execute_harness_error,
        )
        with self.assertLogs(
            "agent_core.workflow.typed_node", level="WARNING"
        ) as captured:
            result = await wrapped({
                "task_id": "task-typed",
                "generate_outcome": _action_outcome(),
            })

        outcome = result["execute_outcome"]
        payload = outcome["payload"]
        self.assertEqual(outcome["type"], "harness_error")
        self.assertEqual(payload["detail"], detail)
        self.assertEqual(payload["primary_cause"]["detail"], detail)
        self.assertEqual(
            payload["component"],
            "agent_core.plan_tracker.record_provenance_event",
        )
        self.assertEqual(
            payload["failed_event_type"],
            "workspace_mutation_intent_recorded",
        )
        self.assertEqual(payload["attempt_id"], "attempt-1")
        self.assertEqual(
            payload["last_successful_phase"], "mutation_intent_journal"
        )
        self.assertEqual(
            payload["diagnostic_ref"], "object:sha256:fixture"
        )
        self.assertEqual(
            outcome["evidence_refs"], ["object:sha256:fixture"]
        )
        self.assertEqual(
            payload["primary_cause"]["failed_event_type"],
            "workspace_mutation_intent_recorded",
        )
        self.assertIn(detail, "\n".join(captured.output))
        diagnostic_log = "\n".join(captured.output)
        self.assertIn("phase=mutation_provenance_registration", diagnostic_log)
        self.assertIn(
            "component=agent_core.plan_tracker.record_provenance_event",
            diagnostic_log,
        )
        self.assertIn(
            "failed_event_type=workspace_mutation_intent_recorded",
            diagnostic_log,
        )
        self.assertIn("trace_ref=object:sha256:fixture", diagnostic_log)
        self.assertEqual(route_execute_outcome(result), END)

    async def test_current_prelaunch_failure_owns_typed_projection_with_lineage(self):
        old_primary = {
            "failure_id": "failure-old-command",
            "error_code": "OLD_COMMAND_FAILURE",
            "exception_type": "RuntimeError",
            "phase": "command_launch",
            "component": "execution.runtime.run_script_path",
            "detail": "historical launched-command failure",
            "command_started": True,
        }
        current = {
            "failure_id": "failure-current-prelaunch",
            "error_code": "EXECUTE_PREPARATION_INTERNAL_ERROR",
            "exception_type": "ValueError",
            "phase": "mutation_provenance_registration",
            "component": "agent_core.plan_tracker.record_provenance_event",
            "failed_event_type": "workspace_mutation_intent_recorded",
            "attempt_id": "attempt-current",
            "last_successful_phase": "mutation_journal_reference_validation",
            "detail": "ValueError: current sanitized preparation failure",
            "diagnostic_ref": "object:sha256:current",
            "command_started": False,
        }

        async def terminal_failure(_state):
            return {
                "terminal_internal_protocol_failure": {
                    "primary_failure": old_primary,
                    "terminal_failure": current,
                    # The append-only commit contains the current terminal as
                    # a later failure.  Typed projection must not duplicate it.
                    "secondary_failures": [current],
                },
                "execution_started": False,
            }

        wrapped = profile_typed_node(
            "execute",
            TypedExecute(executor=terminal_failure).run,
            outcome_field="execute_outcome",
            outcome_contract=ExecuteOutcome,
            harness_error_factory=execute_harness_error,
        )
        with self.assertLogs(
            "agent_core.workflow.typed_node", level="WARNING"
        ) as captured:
            result = await wrapped({
                "task_id": "task-typed",
                "generate_outcome": _action_outcome(),
            })

        outcome = result["execute_outcome"]
        payload = outcome["payload"]
        self.assertEqual(outcome["type"], "harness_error")
        self.assertEqual(payload["error_code"], current["error_code"])
        self.assertEqual(payload["phase"], current["phase"])
        self.assertEqual(payload["detail"], current["detail"])
        self.assertEqual(payload["diagnostic_ref"], current["diagnostic_ref"])
        self.assertEqual(payload["primary_cause"], current)
        self.assertEqual(payload["lineage_primary_cause"], old_primary)
        self.assertEqual(payload["secondary_causes"], [])
        self.assertEqual(outcome["evidence_refs"], [current["diagnostic_ref"]])
        diagnostic_log = "\n".join(captured.output)
        self.assertIn(current["detail"], diagnostic_log)
        self.assertNotIn(old_primary["detail"], diagnostic_log)
        self.assertEqual(route_execute_outcome(result), END)

    async def test_candidate_owned_preflight_rejection_requires_trusted_receipt(self):
        for raw_receipt, expected_detail in (
            (None, "EXECUTE_REPAIR_RECEIPT_MISSING"),
            ({"producer": "forged"}, "EXECUTE_REPAIR_RECEIPT_INVALID"),
        ):
            with self.subTest(expected_detail=expected_detail):
                async def rejected(_state):
                    result = {
                        "pre_execution_rejection": {
                            "violation_owner": "GENERATED_ACTION",
                            "violation_code": "BINDING_REFERENCE_MISMATCH",
                            "candidate_fingerprint": "candidate-action-1",
                            "feedback": "Use the approved binding identity.",
                            "command_started": False,
                        },
                        "candidate_fingerprint": "candidate-action-1",
                        "execution_started": False,
                    }
                    if raw_receipt is not None:
                        result["candidate_repair_receipt"] = raw_receipt
                    return result

                result = await TypedExecute(executor=rejected).run({
                    "task_id": "task-typed",
                    "generate_outcome": _action_outcome(),
                })
                outcome = result["execute_outcome"]
                self.assertEqual(outcome["type"], "harness_error")
                self.assertEqual(
                    outcome["payload"]["error_code"], expected_detail
                )
                self.assertFalse(outcome["payload"]["command_started"])
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

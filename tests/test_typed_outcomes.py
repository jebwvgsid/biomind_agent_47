import asyncio
import json
import unittest
from contextlib import contextmanager

from langgraph.errors import GraphBubbleUp

from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    FinalAuditOutcome,
    GenerateOutcome,
    OutcomeContractError,
    RecoveryOutcome,
    VerificationOutcome,
    workflow_routing_schema_version,
)
from agent_core.workflow.typed_node import profile_typed_node


OUTCOME_CLASSES = (
    GenerateOutcome,
    ExecuteOutcome,
    VerificationOutcome,
    RecoveryOutcome,
    CommitStepOutcome,
    FinalAuditOutcome,
)


def _payload(outcome_class, outcome_type):
    return {
        key: False if key in {"repairable", "counts_as_failure", "command_started", "requires_user_input", "no_partial_mutation"} else f"fixture-{key}"
        for key in outcome_class.REQUIRED_PAYLOAD[outcome_type]
    }


def _mapping(outcome_class, outcome_type):
    return {
        "schema_version": 1,
        "type": outcome_type,
        "summary": f"fixture {outcome_type}",
        "scope": {"task_id": "task-1", "nested": {"items": ["a", "b"]}},
        "payload": _payload(outcome_class, outcome_type),
        "evidence_refs": ["evidence/one.json"],
    }


class TypedOutcomeContractTests(unittest.TestCase):
    def test_every_frozen_v1_outcome_type_round_trips_as_json(self):
        for outcome_class in OUTCOME_CLASSES:
            for outcome_type in sorted(outcome_class.ALLOWED_TYPES):
                with self.subTest(contract=outcome_class.__name__, type=outcome_type):
                    outcome = outcome_class.from_mapping(
                        _mapping(outcome_class, outcome_type)
                    )
                    self.assertEqual(outcome.type, outcome_type)
                    self.assertEqual(
                        json.loads(json.dumps(outcome.to_dict(), allow_nan=False)),
                        outcome.to_dict(),
                    )
                    with self.assertRaises(TypeError):
                        outcome.scope["task_id"] = "changed"
                    with self.assertRaises(TypeError):
                        outcome.scope["nested"]["items"] = ()

    def test_unknown_schema_type_and_top_level_fields_fail_closed(self):
        valid = _mapping(GenerateOutcome, "action_ready")
        for mutation in (
            {**valid, "schema_version": 2},
            {**valid, "type": "execute"},
            {**valid, "unexpected": True},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(OutcomeContractError):
                    GenerateOutcome.from_mapping(mutation)

    def test_required_type_payload_is_enforced(self):
        value = _mapping(VerificationOutcome, "verified")
        value["payload"].pop("completion_gate")
        with self.assertRaisesRegex(
            OutcomeContractError, "OUTCOME_PAYLOAD_INCOMPLETE"
        ):
            VerificationOutcome.from_mapping(value)

    def test_structural_route_authority_is_rejected_recursively(self):
        route_poison = {
            "next_step": "execute",
            "next_route": "generate",
            "target_route": "planner",
            "goto": "supervisor",
            "dispatch_route": "finish",
            "effective_route": "execute",
            "route_resolution": {"dispatch_route": "finish"},
            "proposed_route": "generate",
            "node_recommended_next_step": "execute",
            "required_next_action": "REPLAN",
            "pending_recovery_action": {"type": "retry"},
            "active_transition": {"target_route": "generate"},
            "graph_node": "execute",
        }
        for poison_key, poison_value in route_poison.items():
            value = _mapping(GenerateOutcome, "action_ready")
            value["payload"]["nested"] = {poison_key: poison_value}
            with self.subTest(poison_key=poison_key):
                with self.assertRaisesRegex(
                    OutcomeContractError, "OUTCOME_ROUTE_AUTHORITY_FORBIDDEN"
                ):
                    GenerateOutcome.from_mapping(value)

    def test_scientific_prose_and_filenames_are_not_route_metadata(self):
        value = _mapping(GenerateOutcome, "action_ready")
        value["payload"]["scientific_note"] = (
            "generate a supervisor plot from execute_results.tsv"
        )
        value["evidence_refs"] = ["results/generate_supervisor_execute.tsv"]
        outcome = GenerateOutcome.from_mapping(value)
        self.assertIn("generate", outcome.payload["scientific_note"])

    def test_missing_or_invalid_routing_schema_stays_legacy(self):
        self.assertEqual(workflow_routing_schema_version({}), 1)
        self.assertEqual(
            workflow_routing_schema_version(
                {"task_state": {"workflow_routing_schema_version": 1}},
                {"workflow_routing_schema_version": 2},
            ),
            1,
        )
        self.assertEqual(
            workflow_routing_schema_version(
                {"task_state": {"workflow_routing_schema_version": 2}}
            ),
            2,
        )
        with self.assertRaises(OutcomeContractError):
            workflow_routing_schema_version(
                {"workflow_routing_schema_version": 3}
            )


class _RecordingProfiler:
    def __init__(self):
        self.events = []

    @contextmanager
    def node(self, task_id, node_name):
        self.events.append((task_id, node_name, "start"))
        try:
            yield
        finally:
            self.events.append((task_id, node_name, "finish"))


class TypedNodeWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_profiles_and_returns_only_the_node_update(self):
        profiler = _RecordingProfiler()

        async def node(_state):
            return {
                "generate_outcome": _mapping(GenerateOutcome, "action_ready"),
                "semantic_fact": {"candidate": "c1"},
            }

        wrapped = profile_typed_node(
            "generate",
            node,
            outcome_field="generate_outcome",
            outcome_contract=GenerateOutcome,
            profiler=profiler,
        )
        result = await wrapped({"task_id": "task-1", "next_step": "poison"})
        self.assertEqual(result["generate_outcome"]["type"], "action_ready")
        self.assertEqual(result["semantic_fact"], {"candidate": "c1"})
        self.assertEqual(
            profiler.events,
            [("task-1", "generate", "start"), ("task-1", "generate", "finish")],
        )
        self.assertNotIn("next_step", result)

    async def test_graphbubble_and_cancelled_error_keep_identity(self):
        bubble = GraphBubbleUp()

        async def bubble_node(_state):
            raise bubble

        wrapped_bubble = profile_typed_node(
            "generate",
            bubble_node,
            outcome_field="generate_outcome",
            outcome_contract=GenerateOutcome,
            harness_error_factory=lambda exc, _state: GenerateOutcome.from_mapping(
                _mapping(GenerateOutcome, "harness_error")
            ),
        )
        with self.assertRaises(GraphBubbleUp) as raised:
            await wrapped_bubble({"task_id": "task-1"})
        self.assertIs(raised.exception, bubble)

        cancelled = asyncio.CancelledError()

        async def cancelled_node(_state):
            raise cancelled

        wrapped_cancelled = profile_typed_node(
            "execute",
            cancelled_node,
            outcome_field="execute_outcome",
            outcome_contract=ExecuteOutcome,
            harness_error_factory=lambda exc, _state: ExecuteOutcome.from_mapping(
                _mapping(ExecuteOutcome, "harness_error")
            ),
        )
        with self.assertRaises(asyncio.CancelledError) as raised_cancelled:
            await wrapped_cancelled({"task_id": "task-1"})
        self.assertIs(raised_cancelled.exception, cancelled)

    async def test_ordinary_exception_can_become_strict_harness_error(self):
        async def broken(_state):
            raise RuntimeError("bounded fixture error")

        def harness(exc, _state):
            value = _mapping(GenerateOutcome, "harness_error")
            value["payload"].update({
                "error_code": "GENERATE_NODE_HARNESS_ERROR",
                "exception_class": type(exc).__name__,
                "detail": str(exc),
            })
            return GenerateOutcome.from_mapping(value)

        wrapped = profile_typed_node(
            "generate",
            broken,
            outcome_field="generate_outcome",
            outcome_contract=GenerateOutcome,
            harness_error_factory=harness,
        )
        result = await wrapped({"task_id": "task-1"})
        self.assertEqual(result["generate_outcome"]["type"], "harness_error")
        self.assertEqual(
            result["generate_outcome"]["payload"]["exception_class"],
            "RuntimeError",
        )


if __name__ == "__main__":
    unittest.main()

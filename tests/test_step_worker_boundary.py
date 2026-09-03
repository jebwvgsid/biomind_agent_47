from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

import bio_agents
from agent_core.state import AgentState
from agent_core.task_spec import create_task_spec
from agent_core.workflow.step_worker import (
    STEP_OUTCOME_SCHEMA_VERSION,
    STEP_REQUEST_SCHEMA_VERSION,
    StepOutcome,
    StepRequest,
    StepWorkerContractError,
    build_step_request,
    invoke_step_worker,
    plan_review_destination,
    resolve_step_worker_backend_name,
    route_step_worker_outcome,
)
from bio_agents import BioAnalystAgent


FORBIDDEN_ROUTE_FIELDS = {
    "next_node",
    "next_step",
    "target_route",
    "proposed_route",
    "effective_route",
    "dispatch_route",
    "route_resolution",
    "required_next_action",
    "pending_recovery_action",
    "recovery_action",
}


def _binding_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _approved_plan_state(root: str) -> dict[str, Any]:
    task_id = "typed-worker-task"
    input_path = str(Path(root) / "input.vcf")
    task_spec = create_task_spec(
        "Process the approved input without changing scientific semantics.",
        task_id=task_id,
        structured_source={
            "question": "Process the approved input without changing scientific semantics.",
            "input_files": [{"path": input_path, "role": "variants"}],
        },
        base_dir=root,
        project_root=root,
    )
    input_row = dict(task_spec["inputs"][0])
    input_ref = str(
        input_row.get("artifact_id")
        or input_row.get("input_id")
        or input_row.get("semantic_handle")
    )
    binding_value = {"assembly": "fixture-assembly"}
    binding_id = "plan-binding-fixture"
    return {
        "schema_version": 7,
        "task_id": task_id,
        "plan_id": "plan-fixture",
        "plan_version": "plan_v3",
        "plan_revision": 3,
        "plan_status": "approved",
        "approval_status": "approved",
        "current_step_id": "step-approved",
        "selected_skill_ids": ["skill/global-fallback"],
        "task_spec": task_spec,
        "steps": [{
            "step_id": "step-approved",
            "title": "Run the approved scientific step",
            "purpose": "analysis",
            "status": "ready",
            "inputs": [input_ref],
            "outputs": ["output.report"],
            "success_criteria": ["the declared report is produced"],
            "skill_ids": ["skill/step-specific"],
            "artifacts": [{
                "artifact_id": "report",
                "path": str(Path(root) / "report.tsv"),
                "required": True,
            }],
        }],
        "task_state": {
            "accepted_plan_bindings": {"reference": binding_id},
            "plan_binding_history": [{
                "binding_id": binding_id,
                "name": "reference",
                "value": binding_value,
                "value_type": "json",
                "producer_plan_id": "plan-fixture",
                "producer_plan_version": "plan_v3",
                "producer_plan_revision": 3,
                "producer_step_id": "step-upstream",
                "producer_action_id": "action-upstream",
                "producer_execution_attempt_id": "attempt-upstream",
                "status": "accepted",
                "revision": 1,
                "value_hash": _binding_hash(binding_value),
                "accepted_at": "2026-09-03T00:00:00+00:00",
            }],
        },
    }


class _RecordingBackend:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.requests: list[StepRequest] = []

    async def run_step(self, request: StepRequest) -> Any:
        self.requests.append(request)
        return self.outcome


class _RaisingBackend:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.requests: list[StepRequest] = []

    async def run_step(self, request: StepRequest) -> Any:
        self.requests.append(request)
        raise self.error


class _NoProviderLLM:
    call_count = 0

    def bind(self, **_kwargs: Any) -> "_NoProviderLLM":
        return self

    def model_copy(self, update: Any = None) -> "_NoProviderLLM":
        del update
        return self

    async def ainvoke(self, _messages: Any, **_kwargs: Any) -> AIMessage:
        type(self).call_count += 1
        raise AssertionError("PI-MIG-01 tests must not call a model provider")


class StepWorkerContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="biomind-step-worker-")
        self.addCleanup(self.temp.cleanup)
        self.plan_state = _approved_plan_state(self.temp.name)

    async def asyncTearDown(self) -> None:
        loop = asyncio.get_running_loop()
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            loop._default_executor = None

    def request(self) -> StepRequest:
        return build_step_request(
            self.plan_state,
            workspace=self.temp.name,
            graph_scope={
                "task_id": "typed-worker-task",
                "plan_version": "plan_v3",
                "current_step_id": "step-approved",
            },
            attempt_ordinal=2,
        )

    def test_step_request_is_authoritative_serializable_and_route_free(self) -> None:
        request = self.request()
        payload = request.to_dict()

        self.assertEqual(request.schema_version, STEP_REQUEST_SCHEMA_VERSION)
        self.assertEqual(request.task_id, "typed-worker-task")
        self.assertEqual(request.plan_id, "plan-fixture")
        self.assertEqual(request.plan_version, "plan_v3")
        self.assertEqual(request.step_id, "step-approved")
        self.assertEqual(request.selected_skill_ids, ("skill/step-specific",))
        self.assertEqual(request.attempt_ordinal, 2)
        self.assertEqual(request.accepted_bindings[0]["name"], "reference")
        self.assertEqual(request.resolved_inputs[0]["resolution_status"], "resolved_artifact")
        self.assertTrue(request.available_artifacts)
        self.assertFalse(FORBIDDEN_ROUTE_FIELDS.intersection(payload))
        json.dumps(payload, allow_nan=False)

    def test_step_request_is_deeply_immutable(self) -> None:
        request = self.request()
        with self.assertRaises(FrozenInstanceError):
            request.step_id = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            request.plan_version = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            request.completion_contract["success_criteria"] = []  # type: ignore[index]
        with self.assertRaises(TypeError):
            request.accepted_bindings[0]["name"] = "changed"  # type: ignore[index]

    def test_step_request_serialization_is_deterministic(self) -> None:
        first = self.request().to_dict()
        equivalent_state = {
            key: deepcopy(self.plan_state[key])
            for key in reversed(tuple(self.plan_state))
        }
        second = build_step_request(
            equivalent_state,
            workspace=self.temp.name,
            graph_scope={
                "current_step_id": "step-approved",
                "plan_version": "plan_v3",
                "task_id": "typed-worker-task",
            },
            attempt_ordinal=2,
        ).to_dict()
        encode = lambda value: json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(first, second)
        self.assertEqual(encode(first), encode(second))
        self.assertEqual(
            hashlib.sha256(encode(first)).hexdigest(),
            hashlib.sha256(encode(second)).hexdigest(),
        )

    def test_all_step_outcome_types_validate_and_are_route_free(self) -> None:
        for outcome_type in (
            "completion_claimed",
            "needs_replan",
            "blocked",
            "harness_error",
            "cancelled",
        ):
            with self.subTest(outcome_type=outcome_type):
                outcome = StepOutcome(
                    schema_version=STEP_OUTCOME_SCHEMA_VERSION,
                    type=outcome_type,  # type: ignore[arg-type]
                    summary=f"fixture {outcome_type}",
                    blocker={"code": "FIXTURE_BLOCK"} if outcome_type == "blocked" else None,
                )
                self.assertFalse(FORBIDDEN_ROUTE_FIELDS.intersection(outcome.to_dict()))
                json.dumps(outcome.to_dict(), allow_nan=False)

        blocked = StepOutcome(
            schema_version=1,
            type="blocked",
            summary="fixture blocker",
            blocker={"code": "FIXTURE_BLOCK", "evidence": ["e1"]},
        )
        with self.assertRaises(FrozenInstanceError):
            blocked.type = "cancelled"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            blocked.blocker["code"] = "changed"  # type: ignore[index,union-attr]
        with self.assertRaisesRegex(
            StepWorkerContractError, "STEP_WORKER_OUTCOME_PROTOCOL_ERROR"
        ):
            StepOutcome(1, "blocked", "missing structured blocker")
        with self.assertRaisesRegex(
            StepWorkerContractError, "STEP_WORKER_OUTCOME_PROTOCOL_ERROR"
        ):
            StepOutcome.from_mapping({
                "schema_version": 1,
                "type": "blocked",
                "summary": "malformed blocker",
                "blocker": ["not", "an", "object"],
            })

    async def test_backend_receives_only_step_request_and_node_name_outcomes_fail(self) -> None:
        request = self.request()
        valid = _RecordingBackend(StepOutcome(
            schema_version=1,
            type="completion_claimed",
            summary="worker completed its attempt",
        ))
        outcome = await invoke_step_worker(valid, request)
        self.assertEqual(outcome.type, "completion_claimed")
        self.assertEqual(valid.requests, [request])

        for forbidden_type in ("execute", "generate", "planner", "supervisor", "finish"):
            invalid = _RecordingBackend({
                "schema_version": 1,
                "type": forbidden_type,
                "summary": "backend tried to route the graph",
            })
            with self.subTest(forbidden_type=forbidden_type):
                with self.assertRaisesRegex(
                    StepWorkerContractError,
                    "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                ):
                    await invoke_step_worker(invalid, request)

        for route_field in FORBIDDEN_ROUTE_FIELDS:
            route_metadata = _RecordingBackend({
                "schema_version": 1,
                "type": "completion_claimed",
                "summary": "valid semantic outcome with forbidden route metadata",
                route_field: (
                    {"dispatch_route": "finish"}
                    if route_field == "route_resolution"
                    else "planner"
                ),
            })
            with self.subTest(route_field=route_field):
                with self.assertRaisesRegex(
                    StepWorkerContractError, "STEP_WORKER_OUTCOME_PROTOCOL_ERROR"
                ):
                    await invoke_step_worker(route_metadata, request)

        for malformed in (None, "{}", b"{}", {"schema_version": 1}):
            with self.subTest(malformed=repr(malformed)):
                with self.assertRaisesRegex(
                    StepWorkerContractError, "STEP_WORKER_OUTCOME_PROTOCOL_ERROR"
                ):
                    await invoke_step_worker(_RecordingBackend(malformed), request)

    def test_scope_mismatch_fails_closed_without_guessing(self) -> None:
        with self.assertRaisesRegex(
            StepWorkerContractError, "STEP_REQUEST_SCOPE_INVALID"
        ):
            build_step_request(
                self.plan_state,
                workspace=self.temp.name,
                graph_scope={"current_step_id": "different-step"},
            )

    async def test_typed_outcomes_own_actual_langgraph_edges(self) -> None:
        async def actual_destination(
            outcome: StepOutcome,
            *,
            corrupt_legacy_routes: bool = False,
        ) -> list[str]:
            visited: list[str] = []
            backend = _RecordingBackend(outcome)
            request = self.request()
            workflow = StateGraph(AgentState)

            async def worker(_state: AgentState) -> AgentState:
                worker_outcome = await invoke_step_worker(backend, request)
                return {"step_worker_outcome": worker_outcome.to_dict()}

            async def supervisor(_state: AgentState) -> AgentState:
                visited.append("supervisor")
                return {}

            async def planner(_state: AgentState) -> AgentState:
                visited.append("planner")
                return {}

            def router(state: AgentState) -> str:
                destination = route_step_worker_outcome(state)
                return END if destination == "finish" else destination

            workflow.add_node("step_worker", worker)
            workflow.add_node("supervisor", supervisor)
            workflow.add_node("planner", planner)
            workflow.add_edge(START, "step_worker")
            workflow.add_conditional_edges(
                "step_worker", router, ["supervisor", "planner", END]
            )
            workflow.add_edge("supervisor", END)
            workflow.add_edge("planner", END)
            initial: AgentState = {}
            if corrupt_legacy_routes:
                initial.update({
                    "next_step": "planner",
                    "proposed_route": "finish",
                    "effective_route": "generate",
                    "dispatch_route": "execute",
                    "route_resolution": {"dispatch_route": "finish"},
                    "required_next_action": "REPLAN",
                    "pending_recovery_action": {
                        "type": "INTERNAL_PROTOCOL_ERROR"
                    },
                })
            await workflow.compile().ainvoke(initial)
            self.assertEqual(backend.requests, [request])
            return visited

        completion = StepOutcome(1, "completion_claimed", "done")
        replan = StepOutcome(1, "needs_replan", "plan cannot continue")
        harness = StepOutcome(
            1,
            "harness_error",
            "worker runtime failed",
            blocker={"code": "WORKER_RUNTIME_FAILED"},
        )
        self.assertEqual(await actual_destination(completion), ["supervisor"])
        self.assertEqual(await actual_destination(replan), ["planner"])
        # END is selected directly: there is no Generate or Recovery
        # destination to invoke for a worker Harness failure.
        self.assertEqual(await actual_destination(harness), [])
        self.assertEqual(
            await actual_destination(completion, corrupt_legacy_routes=True),
            ["supervisor"],
        )

    def test_unknown_outcome_router_fails_closed_and_legacy_is_default(self) -> None:
        polluted = {
            "step_worker_outcome": {
                "schema_version": 1,
                "type": "execute",
                "summary": "invalid node name",
            },
            "next_step": "generate",
        }
        self.assertEqual(route_step_worker_outcome(polluted), "finish")
        self.assertEqual(resolve_step_worker_backend_name(environ={}), "legacy")
        self.assertEqual(plan_review_destination("generate", "legacy"), "generate")
        self.assertEqual(
            plan_review_destination("generate", "typed_stub"), "step_worker"
        )
        with self.assertRaisesRegex(
            StepWorkerContractError, "STEP_WORKER_BACKEND_UNSUPPORTED"
        ):
            resolve_step_worker_backend_name(
                environ={"BIOMIND_STEP_WORKER_BACKEND": "unknown-backend"}
            )


class ProductionStepWorkerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="biomind-step-worker-wiring-")
        self.root = Path(self.temp.name)
        self.backend = _RecordingBackend(StepOutcome(
            schema_version=1,
            type="completion_claimed",
            summary="typed backend completed the step",
        ))
        _NoProviderLLM.call_count = 0
        self.patches = [
            patch.object(bio_agents, "init_chat_model", return_value=_NoProviderLLM()),
            patch.object(bio_agents, "BIOMIND_MEMORY_DIR", str(self.root / "memory")),
            patch.object(bio_agents, "BIOMIND_WIKI_ENABLED", False),
        ]
        for item in self.patches:
            item.start()
        self.agent = BioAnalystAgent(
            data_path=str(self.root),
            enable_persistence=False,
            use_llm_supervisor=False,
            step_worker_backend_name="typed_stub",
            step_worker_backend=self.backend,
        )

    async def asyncTearDown(self) -> None:
        await self.agent.aclose(timeout_seconds=0.2)
        for item in reversed(self.patches):
            item.stop()
        loop = asyncio.get_running_loop()
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            loop._default_executor = None
        self.temp.cleanup()

    async def test_production_node_bypasses_controller_and_protocol_errors_terminate(self) -> None:
        durable = _approved_plan_state(str(self.root))
        handler = self.agent._graph_node_handlers["step_worker"]
        graph = self.agent.app.get_graph()
        edges = {
            (edge.source, edge.target)
            for edge in graph.edges
            if bool(getattr(edge, "conditional", False))
        }
        self.assertIn(("plan_review", "step_worker"), edges)
        self.assertIn(("step_worker", "supervisor"), edges)
        self.assertIn(("step_worker", "planner"), edges)
        self.assertIn("step_worker", graph.nodes)
        self.assertEqual(handler.__name__, "step_worker")
        polluted_state: AgentState = {
            "task_id": durable["task_id"],
            "plan_version": durable["plan_version"],
            "current_step_id": durable["current_step_id"],
            "next_step": "planner",
            "effective_route": "generate",
            "dispatch_route": "execute",
            "route_resolution": {"dispatch_route": "finish"},
            "required_next_action": "REPLAN",
            "pending_recovery_action": {"type": "INTERNAL_PROTOCOL_ERROR"},
        }
        with (
            patch.object(bio_agents, "load_plan_state", return_value=durable),
            patch.object(bio_agents, "load_approved_plan_snapshot", return_value={}),
            patch.object(
                self.agent,
                "_apply_transition_guard",
                side_effect=AssertionError("typed worker called Controller guard"),
            ) as guard,
            patch.object(
                self.agent,
                "_resolve_and_commit_effective_transition",
                side_effect=AssertionError("typed worker called Controller resolver"),
            ) as resolver,
            patch.object(
                self.agent,
                "_dispatch_effective_route",
                side_effect=AssertionError("typed worker called Controller dispatcher"),
            ) as dispatcher,
        ):
            result = await handler(polluted_state)

        self.assertEqual(len(self.backend.requests), 1)
        self.assertIsInstance(self.backend.requests[0], StepRequest)
        self.assertEqual(result["step_worker_outcome"]["type"], "completion_claimed")
        self.assertEqual(route_step_worker_outcome({**polluted_state, **result}), "supervisor")
        self.assertEqual(result["step_worker_route_audit"], {
            "schema_version": 1,
            "route_authority": "STEP_WORKER_OUTCOME",
            "legacy_route_fields_authoritative": False,
            "backend": "typed_stub",
        })
        guard.assert_not_called()
        resolver.assert_not_called()
        dispatcher.assert_not_called()
        self.assertEqual(_NoProviderLLM.call_count, 0)

        invalid_payloads = (
            {
                "schema_version": 1,
                "type": "execute",
                "summary": "illegal graph route",
            },
            {
                "schema_version": 1,
                "type": "completion_claimed",
                "summary": "route metadata injection",
                "next_step": "generate",
            },
            {
                "schema_version": 1,
                "type": "blocked",
                "summary": "invalid blocker",
                "blocker": ["not", "an", "object"],
            },
            {"schema_version": 1, "type": "completion_claimed"},
            b"{}",
        )
        for invalid_payload in invalid_payloads:
            self.agent.step_worker_backend = _RecordingBackend(invalid_payload)
            with (
                patch.object(bio_agents, "load_plan_state", return_value=durable),
                patch.object(bio_agents, "load_approved_plan_snapshot", return_value={}),
            ):
                failed = await handler(polluted_state)
            with self.subTest(invalid_payload=repr(invalid_payload)):
                self.assertEqual(
                    failed["step_worker_outcome"]["type"], "harness_error"
                )
                self.assertEqual(
                    failed["step_worker_outcome"]["blocker"]["code"],
                    "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                )
                self.assertEqual(route_step_worker_outcome(failed), "finish")
        self.assertEqual(_NoProviderLLM.call_count, 0)

    async def test_backend_exception_cancellation_and_graphbubble_are_distinct(self) -> None:
        durable = _approved_plan_state(str(self.root))
        handler = self.agent._graph_node_handlers["step_worker"]
        state: AgentState = {
            "task_id": durable["task_id"],
            "plan_version": durable["plan_version"],
            "current_step_id": durable["current_step_id"],
        }

        runtime_backend = _RaisingBackend(RuntimeError("fixture backend failure"))
        self.agent.step_worker_backend = runtime_backend
        with (
            patch.object(bio_agents, "load_plan_state", return_value=durable),
            patch.object(bio_agents, "load_approved_plan_snapshot", return_value={}),
        ):
            failed = await handler(state)
        self.assertEqual(len(runtime_backend.requests), 1)
        self.assertEqual(failed["step_worker_outcome"]["type"], "harness_error")
        self.assertEqual(
            failed["step_worker_outcome"]["blocker"]["code"],
            "STEP_WORKER_BACKEND_ERROR",
        )
        self.assertEqual(route_step_worker_outcome(failed), "finish")

        cancelled_backend = _RecordingBackend(StepOutcome(
            schema_version=1,
            type="cancelled",
            summary="controlled fixture cancellation",
        ))
        self.agent.step_worker_backend = cancelled_backend
        with (
            patch.object(bio_agents, "load_plan_state", return_value=durable),
            patch.object(bio_agents, "load_approved_plan_snapshot", return_value={}),
        ):
            cancelled = await handler(state)
        self.assertEqual(cancelled["step_worker_outcome"]["type"], "cancelled")
        self.assertIsNone(cancelled["step_worker_outcome"]["blocker"])
        self.assertNotEqual(
            cancelled["step_worker_outcome"]["type"],
            failed["step_worker_outcome"]["type"],
        )
        self.assertEqual(route_step_worker_outcome(cancelled), "finish")

        bubble = GraphBubbleUp()
        bubble_backend = _RaisingBackend(bubble)
        self.agent.step_worker_backend = bubble_backend
        with (
            patch.object(bio_agents, "load_plan_state", return_value=durable),
            patch.object(bio_agents, "load_approved_plan_snapshot", return_value={}),
        ):
            with self.assertRaises(GraphBubbleUp) as raised:
                await handler(state)
        self.assertIs(raised.exception, bubble)
        self.assertEqual(len(bubble_backend.requests), 1)
        self.assertEqual(_NoProviderLLM.call_count, 0)


if __name__ == "__main__":
    unittest.main()

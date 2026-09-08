from __future__ import annotations

import unittest
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent_core.state import AgentState
from agent_core.workflow.early_nodes import (
    TypedAskUser,
    TypedConsultant,
    TypedInstallSubagent,
    TypedPlanner,
    TypedPlanReview,
    TypedTriage,
)
from agent_core.workflow.outcomes import (
    AskUserOutcome,
    CommitStepOutcome,
    ConsultantOutcome,
    ExecuteOutcome,
    FinalAuditOutcome,
    GenerateOutcome,
    InstallSubagentOutcome,
    PlannerOutcome,
    PlanReviewOutcome,
    RecoveryOutcome,
    TriageOutcome,
    VerificationOutcome,
)
from agent_core.workflow.runtime_graph import build_schema2_production_graph
from bio_agents import BioAnalystAgent


def outcome(contract, kind, required=None):
    required = dict(required or {})
    payload = {
        key: required.get(key, False if key in {
            "repairable", "counts_as_failure", "command_started",
            "requires_user_input", "no_partial_mutation",
        } else f"fixture-{key}")
        for key in contract.REQUIRED_PAYLOAD[kind]
    }
    payload.update(required)
    return contract(
        schema_version=1,
        type=kind,
        summary=f"fixture {kind}",
        scope={"task_id": "schema2-production"},
        payload=payload,
    ).to_dict()


class Schema2ProductionGraphTests(unittest.IsolatedAsyncioTestCase):
    def handlers(self, calls):
        typed = {
            "triage": ("triage_outcome", outcome(TriageOutcome, "consultation_ready", {"task_mode": "managed", "request_kind": "execution"})),
            "consultant": ("consultant_outcome", outcome(ConsultantOutcome, "planning_ready", {"task_revision": 1, "contract_status": "compiled"})),
            "planner": ("planner_outcome", outcome(PlannerOutcome, "plan_review_ready", {"plan_id": "p", "plan_version": "v", "plan_revision": 1})),
            "plan_review": ("plan_review_outcome", outcome(PlanReviewOutcome, "approved_for_execution", {"plan_id": "p", "plan_version": "v", "approval_status": "approved", "execution_backend": "in_process"})),
            "generate": ("generate_outcome", outcome(GenerateOutcome, "action_ready", {"action": {}, "action_id": "a", "candidate_fingerprint": "c", "validation_evidence": {}})),
            "execute": ("execute_outcome", outcome(ExecuteOutcome, "execution_succeeded", {"execution_record": {}, "action_id": "a", "attempt_id": "x", "return_code": 0, "validation": {}})),
            "supervisor": ("verification_outcome", outcome(VerificationOutcome, "verified", {"validation_evidence": {}, "completion_gate": {}, "accepted_warnings": []})),
            "commit_step": ("commit_step_outcome", outcome(CommitStepOutcome, "plan_complete", {"commit_id": "c", "receipt": {}, "before_state_generation": 1, "after_state_generation": 2, "completed_step_id": "s"})),
            "final_audit": ("final_audit_outcome", outcome(FinalAuditOutcome, "task_verified", {"validation_evidence": {}, "completion_gate": {}, "terminal_receipt": {}})),
        }

        def make(name, field, value):
            async def node(_state):
                calls.append(name)
                return {field: value}
            return node

        handlers = {
            name: make(name, field, value)
            for name, (field, value) in typed.items()
        }
        for name in ("direct_response", "ask_user", "install_subagent", "step_worker", "recovery"):
            async def unused(_state, name=name):
                calls.append(name)
                raise AssertionError(f"unexpected node {name}")
            handlers[name] = unused
        return handlers

    async def test_complete_graph_path_is_invariant_under_legacy_route_poison(self):
        expected = [
            "triage", "consultant", "planner", "plan_review", "generate",
            "execute", "supervisor", "commit_step", "final_audit",
        ]
        poison = {
            "next_step": "finish",
            "next_node": "finish",
            "proposed_route": "finish",
            "effective_route": "finish",
            "dispatch_route": "finish",
            "route_resolution": {"dispatch_route": "finish"},
            "node_recommended_next_step": "finish",
            "required_next_action": "REPLAN",
            "pending_recovery_action": {"type": "blocked"},
            "recovery_decision": {"target_route": "finish", "next_route": "finish"},
            "resume_route_hint": "finish",
        }
        for initial in ({}, poison):
            calls = []
            app = build_schema2_production_graph(self.handlers(calls)).compile()
            with (
                patch.object(BioAnalystAgent, "_apply_transition_guard", side_effect=AssertionError),
                patch.object(BioAnalystAgent, "_resolve_and_commit_effective_transition", side_effect=AssertionError),
                patch.object(BioAnalystAgent, "_dispatch_effective_route", side_effect=AssertionError),
            ):
                await app.ainvoke({
                    "task_id": "schema2-production",
                    "workflow_routing_schema_version": 2,
                    **initial,
                })
            self.assertEqual(calls, expected)

    async def test_typed_ask_user_interrupt_resumes_once(self):
        recorded = []

        async def source(_state):
            answer = interrupt({"question_id": "q1", "question": "Proceed?"})
            recorded.append(answer)
            return {"pending_human_question": {}, "next_step": "finish"}

        typed = TypedAskUser(data_path="/unused", source=source)

        async def ask(state):
            return await typed.run(state)

        async def route(state):
            AskUserOutcome.from_mapping(state["ask_user_outcome"])
            return END

        graph = StateGraph(AgentState)
        graph.add_node("ask", ask)
        graph.add_edge(START, "ask")
        graph.add_conditional_edges("ask", route, [END])
        app = graph.compile(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "ask-once"}}
        initial = {
            "task_id": "schema2-production",
            "pending_human_question": {
                "question_id": "q1", "question": "Proceed?", "type": "consultant_question"
            },
        }
        with patch("agent_core.workflow.early_nodes.load_plan_state", return_value={"task_id": "schema2-production"}):
            first = await app.ainvoke(initial, config=config)
            self.assertIn("__interrupt__", first)
            await app.ainvoke(Command(resume="yes"), config=config)
        self.assertEqual(recorded, ["yes"])

    async def test_all_front_half_adapters_have_zero_controller_routing_calls(self):
        durable = {
            "task_id": "schema2-production",
            "task_status": "running",
            "plan_id": "p",
            "plan_version": "v",
            "plan_revision": 1,
            "steps": [{"step_id": "s", "status": "pending"}],
            "approval_status": "approved",
            "current_task": {"task_spec": {"goal": "test"}, "task_revision": 1},
        }

        async def triage_source(_state):
            return {"task_mode": "managed", "request_intent": {"request_kind": "execution"}, "next_step": "finish"}

        async def consultant_source(_state):
            return {"consultation_state": {"finalize_protocol": {"schema_version": 4, "finalize_status": "compiled", "compile_outcome": {"status": "compiled"}}}, "next_step": "finish"}

        async def planner_source(_state):
            return {"current_plan": "plan", "next_step": "finish"}

        async def review_source(_state):
            return {"approval_status": "approved", "next_step": "finish"}

        async def ask_source(_state):
            return {"pending_human_question": {}, "next_step": "finish"}

        async def install_source(_state):
            return {"messages": ["runtime healthcheck passed"], "next_step": "finish"}

        adapters = [
            TypedTriage(data_path="/unused", source=triage_source),
            TypedConsultant(data_path="/unused", source=consultant_source),
            TypedPlanner(data_path="/unused", source=planner_source),
            TypedPlanReview(data_path="/unused", source=review_source, execution_backend="legacy"),
            TypedAskUser(data_path="/unused", source=ask_source),
            TypedInstallSubagent(data_path="/unused", source=install_source),
        ]
        base = {
            "task_id": "schema2-production",
            "pending_human_question": {"question_id": "q", "question": "Clarify?"},
            "next_step": "finish",
            "route_resolution": {"dispatch_route": "finish"},
        }
        forbidden = {
            "next_step", "next_route", "target_route", "goto", "proposed_route",
            "effective_route", "dispatch_route", "route_resolution",
            "node_recommended_next_step", "required_next_action",
            "pending_recovery_action", "resume_route_hint", "resume_step",
        }

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    yield from keys(item)

        with (
            patch("agent_core.workflow.early_nodes.load_plan_state", return_value=durable),
            patch("agent_core.workflow.early_nodes.load_waiting_state", return_value={}),
            patch.object(BioAnalystAgent, "_apply_transition_guard", side_effect=AssertionError) as guard,
            patch.object(BioAnalystAgent, "_resolve_and_commit_effective_transition", side_effect=AssertionError) as resolver,
            patch.object(BioAnalystAgent, "_dispatch_effective_route", side_effect=AssertionError) as dispatcher,
        ):
            for adapter in adapters:
                result = await adapter.run(base)
                self.assertFalse(forbidden & set(keys(result)))
        self.assertEqual((guard.call_count, resolver.call_count, dispatcher.call_count), (0, 0, 0))


class Schema2ResumeStateTests(unittest.TestCase):
    def test_resume_is_semantic_and_ignores_all_node_hints(self):
        durable = {
            "task_id": "resume-typed",
            "workflow_routing_schema_version": 2,
            "task_state": {"workflow_routing_schema_version": 2},
            "task_status": "running",
            "steps": [{"step_id": "s1", "status": "verification_pending"}],
        }
        recovery_store = unittest.mock.MagicMock()
        recovery_store.load_phase.return_value = {}
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/unused"
        poison = {
            "next_step": "consultant",
            "dispatch_route": "planner",
            "effective_route": "generate",
            "route_resolution": {"dispatch_route": "finish"},
            "required_next_action": "REPLAN",
            "pending_recovery_action": {"type": "blocked"},
            "recovery_decision": {"target_route": "finish", "next_route": "finish"},
            "resume_route_hint": "finish",
        }
        with (
            patch("bio_agents.load_plan_state", return_value=durable),
            patch("bio_agents.load_waiting_state", return_value={}),
            patch("bio_agents.RecoveryStateStore", return_value=recovery_store),
            patch.object(BioAnalystAgent, "_resolve_resume_node", side_effect=AssertionError),
        ):
            result = agent._resolve_schema2_resume_state("resume-typed", poison)
        self.assertEqual(result.type, "verification_pending")
        rendered = result.to_dict()
        self.assertNotIn("resume_route_hint", str(rendered))
        self.assertNotIn("finish", str(rendered))

    def test_resume_uses_shared_lifecycle_for_needs_evidence(self):
        durable = {
            "task_id": "resume-needs-evidence",
            "workflow_routing_schema_version": 2,
            "current_task": {
                "task_spec": {
                    "schema_version": 1,
                    "task_id": "resume-needs-evidence",
                    "goal": "inspect more evidence",
                    "revision": 1,
                },
                "task_revision": 1,
            },
            "task_state": {
                "workflow_routing_schema_version": 2,
                "status": "running",
                "consultation_state": {
                    "schema_version": 2,
                    "phase": "needs_evidence",
                    "consultation_status": "active",
                    "completion_decision": "needs_evidence",
                    "agent_disposition": "continue",
                },
            },
            "task_status": "running",
            "steps": [],
        }
        recovery_store = unittest.mock.MagicMock()
        recovery_store.load_phase.return_value = {}
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/unused"
        with (
            patch("bio_agents.load_plan_state", return_value=durable),
            patch("bio_agents.load_waiting_state", return_value={}),
            patch("bio_agents.RecoveryStateStore", return_value=recovery_store),
        ):
            result = agent._resolve_schema2_resume_state(
                "resume-needs-evidence", {"next_step": "finish"}
            )
        self.assertEqual(result.type, "consultation_ready")
        self.assertEqual(result.payload["reason_code"], "needs_evidence")


if __name__ == "__main__":
    unittest.main()

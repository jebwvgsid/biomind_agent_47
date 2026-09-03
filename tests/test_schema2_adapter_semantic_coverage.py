from __future__ import annotations

import unittest
import inspect
from copy import deepcopy
from unittest.mock import MagicMock, patch

from agent_core.workflow.early_nodes import (
    TypedAskUser,
    TypedConsultant,
    TypedInstallSubagent,
    TypedPlanner,
    TypedPlanReview,
    TypedTriage,
)
from agent_core.workflow.execution import TypedExecute, TypedGenerate
from agent_core.consultant_continuation import consultant_discovery_delta
from agent_core.consultation_state import new_consultation_state
from agent_core.skill_discovery import (
    new_skill_discovery_state,
    record_skill_search_batch,
)
from bio_agents import BioAnalystAgent


ROUTE_POISON = {
    "next_step": "finish",
    "next_route": "finish",
    "target_route": "finish",
    "proposed_route": "finish",
    "effective_route": "finish",
    "dispatch_route": "finish",
    "route_resolution": {"dispatch_route": "finish"},
    "required_next_action": "REPLAN",
    "pending_recovery_action": {"type": "blocked"},
    "resume_route_hint": "finish",
}


def _ready_consultant(task_id: str = "semantic-coverage") -> dict:
    return {
        "task_id": task_id,
        "workflow_routing_schema_version": 2,
        "plan_id": "plan-semantic",
        "plan_version": "plan-v1",
        "plan_revision": 1,
        "current_task": {
            "task_revision": 1,
            "task_spec": {
                "schema_version": 6,
                "task_id": task_id,
                "revision": 1,
                "goal": "Use the accepted documented capability",
            },
        },
        "selected_skill_ids": ["genomics/snpeff-annotation"],
        "task_state": {
            "status": "running",
            "workflow_routing_schema_version": 2,
            "selected_skills": ["genomics/snpeff-annotation"],
            "accepted_consultant_handoff": {
                "summary": "Canonical Consultant commit succeeded.",
                "selected_skills": ["genomics/snpeff-annotation"],
                "goal_interpretation": "Use the accepted documented capability",
                "handoff_kind": "execution_planning",
                "agent_initiated": True,
                "handoff_protocol_version": 1,
            },
            "consultation_state": {
                "phase": "ready_to_plan",
                "consultation_status": "ready",
                "completion_decision": "ready_to_finalize",
                "completion_reason": "candidate_state_complete",
                "requirements": {
                    "skill-routing": {
                        "id": "skill-routing",
                        "mandatory": True,
                        "status": "satisfied",
                    }
                },
            },
        },
        "steps": [],
    }


def _materialized_plan(*, approved: bool = False) -> dict:
    state = _ready_consultant()
    steps = [{
        "step_id": "step-1",
        "title": "Run accepted capability",
        "action": "execute",
        "status": "pending",
        "dependencies": [],
    }]
    state.update({
        "current_plan": {
            "plan_spec": {"steps": deepcopy(steps)},
            "plan_semantic_hash": "sha256:semantic",
            "plan_record_hash": "sha256:record",
            "plan_snapshot_ref": "snapshots/plan-v1.json",
        },
        "steps": steps,
        "plan_status": "approved" if approved else "pending_review",
        "approval_status": "approved" if approved else "pending",
        "approved_plan_hash": "sha256:approved" if approved else "",
    })
    return state


def _uninitialized_consultant(task_id: str = "semantic-coverage") -> dict:
    return {
        "task_id": task_id,
        "workflow_routing_schema_version": 2,
        "current_task": {
            "task_spec": None,
            "task_revision": 0,
            "lifecycle": "uninitialized",
        },
        "selected_skill_ids": [],
        "task_state": {
            "status": "running",
            "workflow_routing_schema_version": 2,
        },
        "steps": [],
    }


class FrontHalfSemanticCoverageTests(unittest.IsolatedAsyncioTestCase):
    def test_consultant_real_handoff_shape_and_poison_are_planning_ready(self):
        durable = _ready_consultant()
        adapter = TypedConsultant(data_path="/unused", source=None)
        clean = adapter.classify({}, {}, durable)
        poisoned = adapter.classify({}, ROUTE_POISON, durable)
        self.assertEqual(clean["type"], "planning_ready")
        self.assertEqual(poisoned, clean)

    def test_consultant_ready_like_state_without_handoff_stays_in_loop(self):
        durable = _ready_consultant()
        durable["task_state"].pop("accepted_consultant_handoff")
        durable["task_state"]["consultation_state"].update({
            "phase": "consulting",
            "consultation_status": "active",
            "completion_decision": "needs_evidence",
        })
        adapter = TypedConsultant(data_path="/unused", source=None)
        self.assertEqual(adapter.classify({}, {}, durable)["type"], "consultation_continues")
        durable["task_state"]["consultation_state"].update({
            "phase": "ready_to_plan", "consultation_status": "ready"
        })
        self.assertEqual(
            adapter.classify({}, {}, durable)["type"],
            "consultation_continues",
        )

    async def test_consultant_search_result_continues_before_durable_handoff(self):
        """Reproduce the fresh-session SEARCH_SKILLS production timing."""

        durable = _uninitialized_consultant("search-in-progress")
        consultation_state = new_consultation_state(
            "consultant-discovery-search-in-progress",
            "search-in-progress",
        )
        discovery = record_skill_search_batch(
            new_skill_discovery_state(),
            queries=[{
                "query_id": "q1",
                "text": "snpeff custom genome annotation with GTF",
                "purpose": "primary scientific method",
            }],
            candidate_cards=[{
                "skill_id": "genomics/snpeff-annotation",
                "name": "snpEff annotation",
                "summary": "Build and use a custom snpEff database.",
                "deterministic_score": 100,
                "source_query_id": "q1",
            }],
            catalog_digest="sha256:catalog",
            input_roles=["reference_fasta", "annotation_gtf", "input_vcf"],
            task_hypothesis="Annotate a VCF with a custom snpEff database.",
        )
        consultation_state["skill_discovery"] = discovery
        consultation_state["discovery_continuation"] = {
            "discovery_delta": consultant_discovery_delta(discovery),
        }

        async def search_source(_state):
            return {
                "messages": ["SEARCH_SKILLS", "skill_search_result"],
                "consultation_state": deepcopy(consultation_state),
                "pending_human_question": {},
                **ROUTE_POISON,
            }

        adapter = TypedConsultant(data_path="/unused", source=search_source)
        with patch(
            "agent_core.workflow.early_nodes.load_plan_state",
            return_value=durable,
        ):
            result = await adapter.run({
                "task_id": "search-in-progress",
                "task_mode": "managed",
                **ROUTE_POISON,
            })

        self.assertEqual(result["consultant_outcome"]["type"], "consultation_continues")
        self.assertEqual(
            result["consultant_outcome"]["payload"]["phase"], "consulting"
        )
        self.assertEqual(result["consultation_state"], consultation_state)
        for route_field in ROUTE_POISON:
            self.assertNotIn(route_field, result)

    def test_consultant_transient_ready_state_cannot_bypass_durable_commit(self):
        durable = _uninitialized_consultant("uncommitted-ready")
        transient = {
            "consultation_state": {
                "phase": "ready_to_plan",
                "consultation_status": "ready",
                "completion_decision": "ready_to_finalize",
            },
            **ROUTE_POISON,
        }
        outcome = TypedConsultant(data_path="/unused", source=None).classify(
            {}, transient, durable
        )
        self.assertEqual(outcome["type"], "consultation_continues")
        self.assertNotEqual(outcome["type"], "planning_ready")

    def test_planner_success_continuation_and_ambiguous_fallthrough(self):
        adapter = TypedPlanner(data_path="/unused", source=None)
        plan = _materialized_plan()
        expected = adapter.classify({}, {}, plan)
        self.assertEqual(expected["type"], "plan_review_ready")
        self.assertEqual(adapter.classify({}, ROUTE_POISON, plan), expected)

        active = _ready_consultant()
        active["task_state"]["planning_epoch"] = {
            "status": "active", "planner_calls_total": 1
        }
        self.assertEqual(adapter.classify({}, {}, active)["type"], "planning_continues")
        active["task_state"].pop("planning_epoch")
        self.assertEqual(adapter.classify({}, {}, active)["type"], "harness_error")

    def test_plan_review_uses_current_approved_plan_fact(self):
        plan = _materialized_plan(approved=True)
        adapter = TypedPlanReview(
            data_path="/unused", source=None, execution_backend="legacy"
        )
        with patch("agent_core.workflow.early_nodes.load_waiting_state", return_value={}):
            expected = adapter.classify({}, {}, plan)
            poisoned = adapter.classify({}, ROUTE_POISON, plan)
            missing = adapter.classify({}, {}, _ready_consultant())
        self.assertEqual(expected["type"], "approved_for_execution")
        self.assertEqual(poisoned, expected)
        self.assertEqual(missing["type"], "harness_error")

    async def test_ask_user_requires_matching_answer_turn_receipt(self):
        pending = {"question_id": "q-1", "question": "Proceed?", "type": "consultant_question"}

        async def answered(_state):
            return {
                "consultation_state": {
                    "pending_question": {
                        **pending,
                        "status": "answered_pending_interpretation",
                        "answer_turn_ref": "conversation-turn-2",
                    }
                },
                **ROUTE_POISON,
            }

        with patch("agent_core.workflow.early_nodes.load_plan_state", return_value=_ready_consultant()):
            result = await TypedAskUser(data_path="/unused", source=answered).run({
                "task_id": "semantic-coverage", "pending_human_question": pending
            })
        self.assertEqual(result["ask_user_outcome"]["type"], "answer_recorded")

        async def missing(_state):
            return {"pending_human_question": {}}

        with patch("agent_core.workflow.early_nodes.load_plan_state", return_value=_ready_consultant()):
            rejected = await TypedAskUser(data_path="/unused", source=missing).run({
                "task_id": "semantic-coverage", "pending_human_question": pending
            })
        self.assertEqual(rejected["ask_user_outcome"]["type"], "harness_error")

    def test_install_uses_runtime_or_step_receipt_not_messages(self):
        adapter = TypedInstallSubagent(data_path="/unused", source=None)
        durable = _materialized_plan()
        success = {
            "runtime_status": {
                "skill::env": {"skill_id": "skill", "env": "env", "ok": True}
            },
            **ROUTE_POISON,
        }
        self.assertEqual(adapter.classify({}, success, durable)["type"], "capability_available")
        self.assertEqual(adapter.classify({}, {"messages": ["healthcheck passed"]}, durable)["type"], "harness_error")

    async def test_triage_known_modes_and_route_poison(self):
        for mode, expected in (
            ("chat", "direct_response_ready"),
            ("readonly", "step_execution_ready"),
            ("simple_execute", "step_execution_ready"),
            ("managed", "consultation_ready"),
        ):
            async def source(_state, mode=mode):
                return {
                    "task_mode": mode,
                    "request_intent": {"request_kind": "execution"},
                    **ROUTE_POISON,
                }
            with patch("agent_core.workflow.early_nodes.load_plan_state", return_value={}):
                result = await TypedTriage(data_path="/unused", source=source).run({"task_id": "t"})
            self.assertEqual(result["triage_outcome"]["type"], expected)

        async def invalid(_state):
            return {"task_mode": "mystery", "request_intent": {"request_kind": "execution"}}
        with patch("agent_core.workflow.early_nodes.load_plan_state", return_value={}):
            rejected = await TypedTriage(data_path="/unused", source=invalid).run({"task_id": "t"})
        self.assertEqual(rejected["triage_outcome"]["type"], "harness_error")

    def test_classifier_sources_do_not_read_legacy_routes(self):
        forbidden = (
            "next_step", "next_route", "target_route", "dispatch_route",
            "effective_route", "route_resolution", "resume_route_hint",
            "resolved_resume_node", "required_next_action",
            "pending_recovery_action",
        )
        for classifier in (
            TypedTriage.classify,
            TypedConsultant.classify,
            TypedPlanner.classify,
            TypedPlanReview.classify,
            TypedInstallSubagent.classify,
        ):
            source = inspect.getsource(classifier)
            for field in forbidden:
                self.assertNotIn(field, source, f"{classifier.__qualname__} reads {field}")

        resume_source = inspect.getsource(BioAnalystAgent._resolve_schema2_resume_state)
        for field in forbidden:
            self.assertNotIn(field, resume_source, f"Resume classifier reads {field}")


class GenerateExecuteSemanticCoverageTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_adapter_sources_do_not_read_legacy_routes(self):
        forbidden = (
            "next_step", "next_route", "target_route", "dispatch_route",
            "effective_route", "route_resolution", "required_next_action",
            "pending_recovery_action",
        )
        for method in (TypedGenerate.run, TypedExecute.run):
            source = inspect.getsource(method)
            for field in forbidden:
                self.assertNotIn(field, source, f"{method.__qualname__} reads {field}")

    async def test_generate_legacy_recovery_projection_cannot_prove_repair(self):
        async def generator(_state):
            return {
                "generate_result": "NEEDS_RECOVERY",
                "generate_exit_reason": "unrecognized_action",
                "pending_recovery_action": {
                    "candidate_fingerprint": "legacy-candidate",
                    "error_fingerprint": "legacy-error",
                },
                **ROUTE_POISON,
            }
        result = await TypedGenerate(generator=generator).run({"task_id": "t"})
        self.assertEqual(
            result["generate_outcome"]["type"], "internal_protocol_error"
        )
        self.assertEqual(
            result["generate_outcome"]["payload"]["error_code"],
            "GENERATE_REPAIR_RECEIPT_MISSING",
        )

    async def test_execute_ambiguous_legacy_routes_fail_closed(self):
        async def executor(_state):
            return dict(ROUTE_POISON)
        result = await TypedExecute(executor=executor).run({"task_id": "t"})
        self.assertEqual(result["execute_outcome"]["type"], "harness_error")


class ResumeSemanticCoverageTests(unittest.TestCase):
    def _classify(self, durable: dict):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/unused"
        recovery_store = MagicMock()
        recovery_store.load_phase.return_value = deepcopy(
            durable.pop("fixture_recovery", {})
        )
        waiting = deepcopy(durable.pop("fixture_waiting", {}))
        validation = deepcopy(durable.pop("fixture_validation", {"valid": False}))
        with (
            patch("bio_agents.load_plan_state", return_value=durable),
            patch("bio_agents.load_waiting_state", return_value=waiting),
            patch("bio_agents.RecoveryStateStore", return_value=recovery_store),
            patch("bio_agents.validate_plan_for_generate", return_value=validation),
        ):
            return agent._resolve_schema2_resume_state("semantic-coverage", ROUTE_POISON)

    def test_every_resume_state_has_one_realistic_durable_classification(self):
        cases = []
        base = {
            "task_id": "semantic-coverage",
            "workflow_routing_schema_version": 2,
            "task_state": {"status": "running", "workflow_routing_schema_version": 2},
            "steps": [],
        }
        cases.append(("consultation_ready", deepcopy(base)))
        cases.append(("planning_ready", _ready_consultant()))

        plan_review = _materialized_plan()
        plan_review["fixture_validation"] = {"valid": False, "reason_code": "plan_not_approved"}
        cases.append(("plan_review_pending", plan_review))

        execution_ready = _materialized_plan(approved=True)
        execution_ready["fixture_validation"] = {"valid": True}
        cases.append(("step_execution_ready", execution_ready))

        action_ready = deepcopy(base)
        action_ready["task_state"]["pending_action"] = {"type": "execute", "action_id": "a"}
        cases.append(("action_execution_ready", action_ready))

        verification = deepcopy(base)
        verification["steps"] = [{"step_id": "s", "status": "verification_pending"}]
        cases.append(("verification_pending", verification))

        recovery = deepcopy(base)
        recovery["fixture_recovery"] = {"phase": "diagnose", "status": "active"}
        cases.append(("recovery_pending", recovery))

        installation = deepcopy(base)
        installation["task_state"]["pending_action"] = {"type": "install_tool"}
        cases.append(("installation_pending", installation))

        final_audit = deepcopy(base)
        final_audit["steps"] = [{"step_id": "s", "status": "passed"}]
        cases.append(("final_audit_pending", final_audit))

        waiting = deepcopy(base)
        waiting["task_state"]["pending_question"] = {"question_id": "q", "question": "Proceed?"}
        cases.append(("awaiting_user_input", waiting))

        terminal = deepcopy(base)
        terminal["task_state"]["status"] = "completed"
        cases.append(("terminal", terminal))

        self.assertEqual(len(cases), 11)
        for expected, durable in cases:
            with self.subTest(expected=expected):
                result = self._classify(deepcopy(durable))
                self.assertEqual(result.type, expected)
                self.assertNotIn("finish", str(result.to_dict()))

    def test_initialized_ready_like_task_without_handoff_resumes_consultant(self):
        durable = _ready_consultant()
        durable["task_state"].pop("accepted_consultant_handoff")
        result = self._classify(durable)
        self.assertEqual(result.type, "consultation_ready")
        self.assertEqual(
            result.payload["reason_code"], "PLANNING_HANDOFF_NOT_COMMITTED"
        )


if __name__ == "__main__":
    unittest.main()

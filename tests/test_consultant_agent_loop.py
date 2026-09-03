from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.graph import END

from agent_core.consultant_binding import consultant_planning_readiness
from agent_core.plan_tracker import commit_consultant_candidate_state
from agent_core.task_spec import apply_task_spec_mutation_proposal
from agent_core.consultation_state import (
    classify_consultation_lifecycle,
    inspect_resource,
    new_consultation_state,
    record_consultant_answer,
    record_consultant_tool_observation,
    record_planning_handoff_attempt,
    resource_binding,
    seed_structured_evidence,
    validate_planning_handoff_protocol,
)
from agent_core.workflow.early_nodes import TypedConsultant
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
from prompts.manager import PromptManager


ROUTE_POISON = {
    "next_step": "planner",
    "proposed_route": "planner",
    "effective_route": "planner",
    "dispatch_route": "planner",
    "route_resolution": {"dispatch_route": "planner"},
    "required_next_action": "FINISH",
    "pending_recovery_action": {"type": "blocked"},
    "resume_route_hint": "finish",
}


def strict_outcome(contract, kind, values=None):
    supplied = dict(values or {})
    payload = {
        key: supplied.get(key, False if key in {
            "repairable", "counts_as_failure", "command_started",
            "requires_user_input", "no_partial_mutation",
        } else f"fixture-{key}")
        for key in contract.REQUIRED_PAYLOAD[kind]
    }
    payload.update(supplied)
    return contract(
        schema_version=1,
        type=kind,
        summary=f"fixture {kind}",
        scope={"task_id": "consultant-agent-loop"},
        payload=payload,
    ).to_dict()


def uninitialized_durable(task_id="consultant-agent-loop"):
    return {
        "task_id": task_id,
        "workflow_routing_schema_version": 2,
        "current_task": {"task_spec": None, "task_revision": 0},
        "task_state": {"status": "running"},
        "steps": [],
    }


class ConsultantAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def adapter_outcome(self, consultation, durable=None, extra=None):
        state = {"consultation_state": deepcopy(consultation), **dict(extra or {})}
        return TypedConsultant(data_path="/unused", source=None).classify(
            {}, state, durable or uninitialized_durable()
        )

    def test_real_seed_search_result_needs_evidence_continues(self):
        state = new_consultation_state("search-loop", "search-loop")
        state = seed_structured_evidence(
            state,
            input_audit={"relevant": False},
            candidate_skill_ids=["genomics/snpeff-annotation"],
            routing_candidate_ids=[],
        )
        self.assertEqual(state["phase"], "needs_evidence")
        state = record_consultant_tool_observation(
            state,
            action_type="search_skills",
            action={"queries": [{"query_id": "q1", "text": "snpeff"}]},
            observation={"candidate_skill_ids": ["genomics/snpeff-annotation"]},
        )
        outcome = self.adapter_outcome(state)
        self.assertEqual(outcome["type"], "consultation_continues")
        self.assertNotIn(outcome["type"], {"planning_ready", "harness_error", "terminal"})

    def test_skill_doc_is_an_observation_then_reason_again(self):
        state = new_consultation_state("doc-loop", "doc-loop")
        searched = record_consultant_tool_observation(
            state,
            action_type="search_skills",
            action={"query": "annotation"},
            observation={"candidate_skill_ids": ["genomics/snpeff-annotation"]},
        )
        loaded = record_consultant_tool_observation(
            searched,
            action_type="load_skill_doc",
            action={"skill_ids": ["genomics/snpeff-annotation"]},
            observation={"loaded_skill_ids": ["genomics/snpeff-annotation"]},
        )
        self.assertEqual(self.adapter_outcome(loaded)["type"], "consultation_continues")
        self.assertEqual(loaded["agent_progress"]["identical_no_progress_count"], 0)

    def test_hypothetical_and_advisory_answer_without_files_or_taskspec(self):
        conceptual = resource_binding(
            "sample.vcf", role="variants", epistemic_status="conceptual"
        )
        self.assertEqual(conceptual["epistemic_status"], "conceptual")
        state = record_consultant_answer(
            new_consultation_state("advisory", "advisory"),
            "A hypothetical snpEff workflow can be described without local files.",
        )
        outcome = self.adapter_outcome(state, uninitialized_durable("advisory"))
        self.assertEqual(outcome["type"], "answer_ready")
        self.assertEqual(outcome["payload"]["completion_mode"], "consultation_only")

    def test_resource_exists_and_missing_are_observations_not_verdicts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = Path(tmpdir) / "sample.vcf"
            existing.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
            present = inspect_resource(str(existing), role="variants")
            missing = inspect_resource(str(Path(tmpdir) / "missing.vcf"), role="variants")
        self.assertTrue(present["exists"])
        self.assertFalse(missing["exists"])
        for observation in (present, missing):
            state = record_consultant_tool_observation(
                new_consultation_state("resource", "resource"),
                action_type="inspect_resource",
                action={"resource": observation["resource"]},
                observation=observation,
            )
            self.assertEqual(self.adapter_outcome(state)["type"], "consultation_continues")
            self.assertNotIn("disposition", observation)

    def test_explicit_handoff_needs_protocol_and_durable_commit(self):
        task_spec = apply_task_spec_mutation_proposal(
            None,
            {
                "proposal_type": "task_spec_mutation",
                "schema_version": 1,
                "operations": [{
                    "op": "set_goal",
                    "value": "Prepare an execution plan",
                    "reason": "test_fixture",
                    "supporting_evidence": ["U1"],
                }],
                "unresolved_ambiguities": [],
                "proposal_evidence_refs": [],
            },
            task_id="handoff",
        )
        candidate = {
            "task_spec": task_spec,
            "consultation_state": new_consultation_state("handoff", "handoff"),
            "output_contract_violations": [],
        }
        self.assertTrue(validate_planning_handoff_protocol(candidate)["valid"])
        consultation = record_planning_handoff_attempt(
            candidate["consultation_state"], committed=True
        )
        transient = self.adapter_outcome(
            consultation, uninitialized_durable("handoff")
        )
        self.assertEqual(transient["type"], "consultation_continues")

        with tempfile.TemporaryDirectory() as root:
            durable = commit_consultant_candidate_state(root, "handoff", {
                "task_spec": task_spec,
                "selected_skills": [],
                "resolved_bindings": {},
                "pending_question": {},
                "consultation_state": consultation,
                "accepted_consultant_handoff": {
                    "summary": "explicit handoff",
                    "handoff_kind": "execution_planning",
                    "agent_initiated": True,
                    "handoff_protocol_version": 1,
                },
            })
            self.assertTrue(consultant_planning_readiness(durable)["ready"])
            self.assertEqual(
                self.adapter_outcome(consultation, durable)["type"],
                "planning_ready",
            )

    def test_legacy_compiled_receipt_cannot_replace_schema2_agent_handoff(self):
        consultation = new_consultation_state("legacy-ready", "legacy-ready")
        consultation.update({
            "phase": "ready_to_plan",
            "consultation_status": "ready",
            "completion_decision": "ready_to_finalize",
            "finalize_protocol": {
                "schema_version": 4,
                "finalize_status": "compiled",
                "compile_outcome": {"status": "compiled"},
            },
        })
        durable = {
            "task_id": "legacy-ready",
            "workflow_routing_schema_version": 2,
            "current_task": {
                "task_spec": {
                    "schema_version": 1,
                    "task_id": "legacy-ready",
                    "goal": "legacy compiled candidate",
                    "revision": 1,
                },
                "task_revision": 1,
            },
            "task_state": {
                "status": "running",
                "consultation_state": consultation,
            },
            "steps": [],
        }
        self.assertTrue(consultant_planning_readiness(durable)["ready"])
        typed = consultant_planning_readiness(
            durable, require_agent_initiated=True
        )
        self.assertFalse(typed["ready"])
        self.assertEqual(typed["reason_code"], "agent_initiated_handoff_missing")
        self.assertEqual(
            self.adapter_outcome(consultation, durable)["type"],
            "consultation_continues",
        )

    def test_route_poison_cannot_change_continue_or_answer(self):
        continued = new_consultation_state("poison", "poison")
        expected = self.adapter_outcome(continued)
        poisoned = self.adapter_outcome(continued, extra=ROUTE_POISON)
        self.assertEqual(poisoned, expected)
        answer = record_consultant_answer(continued, "Direct answer")
        self.assertEqual(
            self.adapter_outcome(answer, extra=ROUTE_POISON)["type"],
            "answer_ready",
        )

    def test_unknown_state_fails_closed_with_compact_diagnostic(self):
        outcome = self.adapter_outcome({}, uninitialized_durable("corrupt"))
        self.assertEqual(outcome["type"], "harness_error")
        self.assertEqual(
            outcome["payload"]["error_code"],
            "CONSULTATION_LIFECYCLE_CONTRADICTION",
        )
        self.assertIn("semantic_state", outcome["payload"])

    async def test_adapter_projects_terminal_diagnostic_without_route_authority(self):
        async def corrupt_source(_state):
            return {"consultation_state": {}, **ROUTE_POISON}

        adapter = TypedConsultant(data_path="/unused", source=corrupt_source)
        with patch(
            "agent_core.workflow.early_nodes.load_plan_state",
            return_value=uninitialized_durable("corrupt"),
        ):
            result = await adapter.run({"task_id": "corrupt", **ROUTE_POISON})
        diagnostic = result["terminal_diagnostic"]
        self.assertEqual(diagnostic["source_node"], "consultant")
        self.assertEqual(
            diagnostic["error_code"],
            "CONSULTATION_LIFECYCLE_CONTRADICTION",
        )
        self.assertFalse(set(ROUTE_POISON).intersection(result))

    def test_answer_ready_snapshot_is_a_completed_terminal_result(self):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/unused"
        answer = record_consultant_answer(
            new_consultation_state("answer-terminal", "answer-terminal"),
            "Consultation complete.",
        )
        snapshot = SimpleNamespace(
            values={
                "task_status": "completed",
                "consultation_state": answer,
                "consultant_outcome": strict_outcome(
                    ConsultantOutcome,
                    "answer_ready",
                    {"answer_id": "answer-1", "completion_mode": "consultation_only"},
                ),
            }
        )
        with (
            patch("bio_agents.load_plan_state", return_value={}),
            patch("bio_agents.load_waiting_state", return_value={}),
        ):
            self.assertEqual(
                agent._terminal_status_from_snapshot("answer-terminal", snapshot),
                "completed",
            )

    def test_genuine_identical_no_progress_is_bounded_as_internal_diagnostic(self):
        state = new_consultation_state("no-progress", "no-progress", max_no_progress=2)
        for _ in range(3):
            state = record_consultant_tool_observation(
                state,
                action_type="inspect_resource",
                action={"resource": "/missing.vcf"},
                observation={"resource": "/missing.vcf", "exists": False},
            )
        lifecycle = classify_consultation_lifecycle(state)
        self.assertEqual(lifecycle["disposition"], "invalid")
        self.assertEqual(lifecycle["reason_code"], "CONSULTANT_AGENT_NO_PROGRESS")
        self.assertEqual(self.adapter_outcome(state)["type"], "harness_error")
        self.assertNotEqual(state.get("consultation_status"), "blocked")

    async def test_compiled_graph_allows_three_consultant_turns_then_answer(self):
        calls = []
        outcomes = iter([
            strict_outcome(ConsultantOutcome, "consultation_continues", {
                "phase": "needs_evidence", "completion_decision": "needs_reasoning"
            }),
            strict_outcome(ConsultantOutcome, "consultation_continues", {
                "phase": "needs_evidence", "completion_decision": "needs_reasoning"
            }),
            strict_outcome(ConsultantOutcome, "answer_ready", {
                "answer_id": "answer-1", "completion_mode": "consultation_only"
            }),
        ])

        async def triage(_state):
            calls.append("triage")
            return {"triage_outcome": strict_outcome(
                TriageOutcome, "consultation_ready",
                {"task_mode": "managed", "request_kind": "question"},
            )}

        async def consultant(_state):
            calls.append("consultant")
            return {"consultant_outcome": next(outcomes)}

        async def unused(_state):
            raise AssertionError("unexpected graph node")

        handlers = {name: unused for name in (
            "direct_response", "planner", "plan_review", "ask_user",
            "install_subagent", "generate", "step_worker", "execute",
            "supervisor", "commit_step", "recovery", "final_audit",
        )}
        handlers.update({"triage": triage, "consultant": consultant})
        app = build_schema2_production_graph(handlers).compile()
        await app.ainvoke({
            "task_id": "consultant-agent-loop",
            "workflow_routing_schema_version": 2,
            **ROUTE_POISON,
        })
        self.assertEqual(calls, ["triage", "consultant", "consultant", "consultant"])

    def test_prompt_and_action_parser_expose_agent_loop_contract(self):
        prompt = PromptManager.get_consultant_v2_prompt({})
        for phrase in (
            '"action_type":"inspect_resource"',
            '"action_type":"answer_user"',
            '"action_type":"submit_planning_handoff"',
            "Every tool result is an observation",
            "A TaskSpec is optional",
        ):
            self.assertIn(phrase, prompt)
        self.assertEqual(
            BioAnalystAgent._consultant_command_from_json(
                '{"action_type":"inspect_resource","resource":"/tmp/x.vcf"}'
            ),
            "INSPECT_RESOURCE /tmp/x.vcf",
        )
        self.assertEqual(
            BioAnalystAgent._consultant_command_from_json(
                '{"action_type":"answer_user","answer":"Use a conceptual workflow."}'
            ),
            "ANSWER_USER Use a conceptual workflow.",
        )
        self.assertEqual(
            BioAnalystAgent._consultant_command_from_json(
                '{"action_type":"submit_planning_handoff","task_spec_operations":[]}'
            ),
            "SUBMIT_PLANNING_HANDOFF",
        )


if __name__ == "__main__":
    unittest.main()

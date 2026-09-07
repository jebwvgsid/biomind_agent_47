"""HEAD-only production Planner handoff prerequisite (no runtime evidence API)."""
from __future__ import annotations

import importlib.util
import json
import unittest
from copy import deepcopy
from pathlib import Path

from langchain_core.messages import AIMessage
from agent_core.consultant_binding import consultant_planning_readiness
from agent_core.plan_tracker import (
    commit_consultant_candidate_state, load_plan_state, save_plan_state,
)
from agent_core.skill_manager import SkillManager
from agent_core.skill_access import SkillAccessService

_path = Path(__file__).with_name("test_consultant_first_graph_integration_v21.py")
_spec = importlib.util.spec_from_file_location("planner_prerequisite_head_fixture", _path)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


class Schema2PlannerHandoffPrerequisiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = _fixture.ConsultantFirstGraphIntegrationV21Tests()
        await self.fixture.asyncSetUp()
        self.root, self.agent = self.fixture.root, self.fixture.agent
        skill_root = self.root / "test-skills"
        directory = skill_root / _fixture.SKILL_ID
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("""---
name: Offline report
description: Read reference FASTA and write a report.
metadata:
  inputs: [reference_fasta]
  outputs: [report]
---
Read the supplied reference and write the requested report.
""", encoding="utf-8")
        self.agent.skill_manager = SkillManager(str(skill_root))
        self.agent.skill_access_service = SkillAccessService(self.agent.skill_manager)
        self.calls = []

        async def scripted(_messages, *, stage, **_kwargs):
            self.calls.append(stage)
            if stage != "planner":
                raise AssertionError("Unexpected provider stage: " + stage)
            plan = json.loads(_fixture._planner_v2_plan())
            registry = self.agent.skill_manager.get_execution_capability_registry([_fixture.SKILL_ID])
            plan["steps"][0]["execution_capability_id"] = next(iter(registry))
            return AIMessage(content=json.dumps(plan))

        self.agent._ainvoke_node_llm = scripted

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def _state(self, task_id, *, schema=2, handoff=True):
        input_path, output_path = self.root / (task_id + ".fasta"), self.root / (task_id + ".txt")
        input_path.write_text(">x\nA\n", encoding="utf-8")
        state = self.fixture._ready_planner_state(
            task_id, input_path=input_path, output_path=output_path,
        )
        accepted = {
            "summary": "Use the supplied reference and write the requested report.",
            "selected_skills": [_fixture.SKILL_ID],
            "agent_initiated": True,
            "handoff_kind": "execution_planning",
            "handoff_protocol_version": 1,
        } if handoff else {}
        durable = commit_consultant_candidate_state(self.root, task_id, {
            "task_spec": state["task_spec"],
            "selected_skills": [_fixture.SKILL_ID],
            "consultation_state": deepcopy(state["consultation_state"]),
            "resolved_bindings": {},
            "pending_question": {},
            "accepted_consultant_handoff": accepted,
        })
        durable["workflow_routing_schema_version"] = schema
        durable.setdefault("task_state", {})["workflow_routing_schema_version"] = schema
        save_plan_state(self.root, task_id, durable)
        state["workflow_routing_schema_version"] = schema
        return state

    async def test_durable_agent_handoff_ignores_transient_phase_and_reaches_provider(self):
        state = self._state("prerequisite-ready")
        durable = load_plan_state(self.root, state["task_id"])
        self.assertTrue(consultant_planning_readiness(durable, require_agent_initiated=True)["ready"])
        state["consultation_state"] = {
            "phase": "needs_evidence", "completion_decision": "needs_reasoning",
            "next_step": "consultant", "finalize_protocol": {"schema_version": 99},
        }
        result = await self.agent._schema2_early_sources["planner"](state)
        self.assertIn("planner", self.calls, str(result))
        self.assertNotEqual(result.get("contract_status"), "semantic_contract_finalize_pending")

    async def test_missing_handoff_cannot_be_forged_by_transient_ready_phase(self):
        state = self._state("prerequisite-missing", handoff=False)
        state.update(next_step="planner", contract_status="canonical_task_ready", agent_initiated=True)
        state["consultation_state"].update(phase="ready_to_plan", completion_decision="ready_to_finalize")
        result = await self.agent._schema2_early_sources["planner"](state)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.get("contract_status"), "schema2_planning_handoff_required")

    async def test_handoff_for_previous_skill_selection_cannot_reach_provider(self):
        state = self._state("prerequisite-stale-selection")
        durable = load_plan_state(self.root, state["task_id"])
        durable["task_state"]["accepted_consultant_handoff"]["selected_skills"] = ["previous/skill"]
        save_plan_state(self.root, state["task_id"], durable)
        result = await self.agent._schema2_early_sources["planner"](state)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.get("contract_status"), "schema2_planning_handoff_required")

    async def test_pending_question_prevents_provider_despite_ready_transient_state(self):
        state = self._state("prerequisite-pending-question")
        durable = load_plan_state(self.root, state["task_id"])
        durable["task_state"]["pending_question"] = {"question_id": "q1", "question": "Choose the report scope"}
        save_plan_state(self.root, state["task_id"], durable)
        state["pending_human_question"] = {}
        result = await self.agent._schema2_early_sources["planner"](state)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.get("contract_status"), "schema2_planning_handoff_required")

    async def test_schema1_legacy_ready_phase_still_reaches_provider(self):
        state = self._state("prerequisite-schema1", schema=1, handoff=False)
        result = await self.agent._graph_node_handlers["planner"](state)
        self.assertIn("planner", self.calls, str(result))

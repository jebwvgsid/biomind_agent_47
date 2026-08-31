from __future__ import annotations

import unittest

from agent_core.node_context import build_node_context, evaluate_node_staleness
from bio_agents import BioAnalystAgent


def _task_spec(*, revision: int = 4) -> dict:
    return {
        "schema_version": 4,
        "task_id": "task-current",
        "revision": revision,
        "goal": "Transform the declared input into the declared output.",
        "inputs": [{
            "input_id": "input-current",
            "role": "source_document",
            "path": "/workspace/source.txt",
            "format": "text",
            "required": True,
        }],
        "output_contracts": [{
            "output_id": "output-current",
            "role": "result_document",
            "path": "/workspace/result.txt",
            "format": "text",
            "required": True,
        }],
        "requirements": [],
    }


def _plan_state(*, task_revision: int = 4) -> dict:
    return {
        "task_spec": _task_spec(revision=task_revision),
        "plan_version": "plan_v9",
        "plan_id": "plan-current",
        "plan_revision": 9,
        "runtime_state": {"execution_epoch": "epoch-3"},
        "current_step_id": "step-current",
        "conversation_evidence": {"user_turn_count": 12},
        "steps": [{
            "step_id": "step-current",
            "title": "Create the result",
            "action": "Apply the selected capability",
            "inputs": ["input-current"],
            "outputs": ["output-current"],
            "dependencies": [],
            "success_criteria": ["The declared output exists"],
            "status": "running",
        }],
    }


def _skill_context() -> dict:
    return {
        "selected_skill_refs": [{
            "skill_id": "capability/alpha",
            "snapshot_id": "snapshot-alpha",
            "content_hash": "a" * 64,
            "status": "active",
            "scope": "task",
        }],
        "available_skill_headings": [{
            "skill_id": "capability/alpha",
            "snapshot_id": "snapshot-alpha",
            "headings": [{"heading": "Procedure", "start_line": 4, "end_line": 8}],
        }],
        "relevant_skill_excerpts": [{
            "skill_id": "capability/alpha",
            "snapshot_id": "snapshot-alpha",
            "section": "Procedure",
            "content": "Validate the input, then create the output.",
        }],
        "skill_tool_results": [{
            "operation": "REQUEST_SKILL_CONTEXT",
            "skill_id": "capability/alpha",
            "result": "The requested section was returned.",
        }],
    }


class ExecuteRevisionGateTests(unittest.TestCase):
    def test_pending_action_matches_all_current_authority_revisions(self) -> None:
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        action = {
            "step_id": "step-current",
            "task_revision": 4,
            "plan_version": "plan_v9",
            "plan_revision": 9,
            "authoritative_user_turn_revision": 12,
            "plan_id": "plan-current",
            "execution_epoch": "epoch-3",
        }

        self.assertEqual(
            agent._pending_action_revision_mismatches(action, _plan_state()),
            {},
        )

    def test_pending_action_reports_each_changed_authority_revision(self) -> None:
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        action = {
            "step_id": "step-old",
            "task_revision": 3,
            "plan_version": "plan_v8",
            "plan_revision": 8,
            "authoritative_user_turn_revision": 11,
            "plan_id": "plan-old",
            "execution_epoch": "epoch-2",
        }

        mismatches = agent._pending_action_revision_mismatches(
            action,
            _plan_state(),
        )

        self.assertEqual(
            set(mismatches),
            {
                "step_id",
                "task_revision",
                "plan_version",
                "plan_revision",
                "authoritative_user_turn_revision",
                "plan_id",
                "execution_epoch",
            },
        )
        self.assertEqual(
            mismatches["authoritative_user_turn_revision"],
            {"bound": "11", "current": "12"},
        )
        self.assertEqual(
            mismatches["task_revision"],
            {"bound": "3", "current": "4"},
        )

    def test_pending_action_identity_must_still_be_durable(self) -> None:
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        action = {
            "action_id": "action-current",
            "step_id": "step-current",
            "task_revision": 4,
            "plan_version": "plan_v9",
            "plan_revision": 9,
            "authoritative_user_turn_revision": 12,
            "plan_id": "plan-current",
            "execution_epoch": "epoch-3",
        }
        plan = _plan_state()

        missing = agent._pending_action_revision_mismatches(action, plan)
        self.assertEqual(
            missing["pending_action_identity"],
            {"bound": "action-current", "current": ""},
        )

        plan["task_state"] = {
            "pending_action": {"action_id": "action-successor"},
        }
        replaced = agent._pending_action_revision_mismatches(action, plan)
        self.assertEqual(
            replaced["pending_action_identity"],
            {"bound": "action-current", "current": "action-successor"},
        )

        plan["task_state"]["pending_action"] = {
            "action_id": "action-current",
        }
        self.assertEqual(
            agent._pending_action_revision_mismatches(action, plan),
            {},
        )


class SkillNodeContextStaleGateTests(unittest.TestCase):
    def _context(self):
        plan = _plan_state()
        return build_node_context(
            node_type="consultant",
            task_spec=plan["task_spec"],
            plan_state=plan,
            state_generation=7,
            current_step_id="step-current",
            authoritative_user_turn_revision=12,
            continuation_id="continuation-alpha",
            skill_context=_skill_context(),
            allowed_operations=[
                "REQUEST_SKILL_CONTEXT",
                "SEARCH_SKILLS",
                "PROPOSE_SKILL_BINDING",
            ],
        )

    def test_consultant_receives_bounded_skill_projection(self) -> None:
        payload = self._context().llm_payload.to_dict()

        self.assertEqual(
            payload["selected_skill_refs"][0]["snapshot_id"],
            "snapshot-alpha",
        )
        self.assertEqual(
            payload["available_skill_headings"][0]["headings"][0]["heading"],
            "Procedure",
        )
        self.assertEqual(
            payload["relevant_skill_excerpts"][0]["content"],
            "Validate the input, then create the output.",
        )
        self.assertEqual(
            payload["skill_tool_results"][0]["operation"],
            "REQUEST_SKILL_CONTEXT",
        )

    def test_record_only_change_is_revalidated_without_staling_skill_context(self) -> None:
        binding = self._context().binding
        current = binding.to_dict()
        current["state_generation"] += 1
        current["task_record_hash"] = "record-only-change"

        decision = evaluate_node_staleness(binding, current)

        self.assertTrue(decision.usable)
        self.assertEqual(decision.code, "REVALIDATE_ON_CURRENT_RECORD")
        self.assertEqual(
            decision.ignored_record_changes,
            ("state_generation", "task_record_hash"),
        )

    def test_new_user_turn_and_continuation_discard_old_skill_result(self) -> None:
        binding = self._context().binding
        current = binding.to_dict()
        current["authoritative_user_turn_revision"] = 13
        current["continuation_id"] = "continuation-beta"

        decision = evaluate_node_staleness(binding, current)

        self.assertFalse(decision.usable)
        self.assertEqual(decision.code, "STALE_TASK_SEMANTICS")
        self.assertEqual(
            decision.changed_preconditions,
            ("authoritative_user_turn_revision", "continuation_id"),
        )
        self.assertTrue(decision.stale_signature.startswith("sha256:"))

    def test_task_and_plan_revision_changes_discard_old_skill_result(self) -> None:
        binding = self._context().binding
        current = binding.to_dict()
        current["task_revision"] = 5
        current["plan_revision"] = 10

        decision = evaluate_node_staleness(binding, current)

        self.assertFalse(decision.usable)
        self.assertEqual(
            decision.changed_preconditions,
            ("plan_revision", "task_revision"),
        )

    def test_planner_result_is_stale_when_current_step_changes_in_place(self) -> None:
        plan = _plan_state()
        context = build_node_context(
            node_type="planner",
            task_spec=plan["task_spec"],
            plan_state=plan,
            state_generation=7,
            current_step_id="step-current",
            authoritative_user_turn_revision=12,
            skill_context=_skill_context(),
            allowed_operations=[
                "REQUEST_SKILL_CONTEXT",
                "SEARCH_SKILLS",
                "PROPOSE_SKILL_BINDING",
            ],
        )
        current = context.binding.to_dict()
        current["step_id"] = "step-successor"

        decision = evaluate_node_staleness(context.binding, current)

        self.assertFalse(decision.usable)
        self.assertIn("step_id", decision.changed_preconditions)


if __name__ == "__main__":
    unittest.main()

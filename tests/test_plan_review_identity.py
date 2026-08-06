import tempfile
import unittest

from agent_core.plan_tracker import (
    approve_plan_state,
    build_plan_state,
    plan_identity,
    render_plan_review_content,
    save_plan_state,
    validate_plan_for_generate,
)
from agent_core.task_spec import create_task_spec
from bio_agents import BioAnalystAgent


class PlanReviewIdentityTests(unittest.TestCase):
    def _plan(self):
        state = build_plan_state(
            "task-review",
            "\n".join([
                "- [ ] Step 1: Validate input | Action: SEARCH | Skills: [] | Done: input valid",
                "- [ ] Step 4: Produce table | Action: EXECUTE | Skills: [] | Done: table exists",
            ]),
        )
        state["generated_from_task_revision"] = 1
        return state

    def test_review_always_contains_effective_plan_before_bounded_diff(self):
        state = self._plan()
        state["plan_version"] = "plan_v2"
        state["parent_plan_version"] = "plan_v1"
        state["pending_review_diff"] = [{
            "operation": "REPAIR_PLAN",
            "changed_step_ids": ["step-4"],
            "reason": [{"reason": "required output missing"}],
            "raw_patch": {"must_not_be_rendered": True},
        }]

        rendered = render_plan_review_content(state)

        self.assertLess(rendered.index("## Effective Plan"), rendered.index("## Changes Since Parent Revision"))
        self.assertIn("Step 1: Validate input", rendered)
        self.assertIn("Step 4: Produce table", rendered)
        self.assertIn("Changed Stable Step IDs: `step-4`", rendered)
        self.assertNotIn("must_not_be_rendered", rendered)

    def test_approval_record_is_bound_to_exact_plan_identity(self):
        state = self._plan()
        approve_plan_state(state, 1, review_event_id="review-1")
        identity = plan_identity(state)
        approval = state["approval_record"]

        self.assertEqual(approval["approved_plan_id"], identity["plan_id"])
        self.assertEqual(approval["approved_plan_version"], identity["plan_version"])
        self.assertEqual(approval["approved_plan_semantic_hash"], identity["plan_semantic_hash"])
        self.assertEqual(validate_plan_for_generate(state, 1)["reason_code"], "valid")

    def test_semantic_change_makes_approval_stale_and_routes_to_review(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._plan()
            approve_plan_state(state, 1)
            state["steps"][0]["success_criteria"] = ["new semantic requirement"]
            save_plan_state(root, "task-review", state)
            validation = validate_plan_for_generate(state, 1)
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = root
            agent._task_revision_for_state = lambda _state: 1
            agent._record_plan_current_evidence = lambda *_args: "progress"
            update = agent._generate_plan_redirect(
                {"task_mode": "managed", "task_spec": create_task_spec("run")},
                "task-review",
                validation,
            )

        self.assertEqual(validation["reason_code"], "plan_approval_stale")
        self.assertEqual(update["next_step"], "plan_review")
        self.assertEqual(update["plan_approval_status"], "PLAN_APPROVAL_STALE")
        self.assertEqual(update["required_next_action"], "PLAN_APPROVAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()

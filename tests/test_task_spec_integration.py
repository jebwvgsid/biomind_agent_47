import tempfile
import unittest

from agent_core.plan_tracker import bind_plan_to_task_revision, build_plan_state, save_plan_state
from agent_core.task_spec import apply_user_feedback, create_task_spec
from bio_agents import BioAnalystAgent


class TaskSpecIntegrationTests(unittest.TestCase):
    def test_task_feedback_makes_older_plan_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            plan = build_plan_state("task-a", "- [ ] Step 1: Run analysis")
            bind_plan_to_task_revision(plan, 1)
            save_plan_state(tmpdir, "task-a", plan)
            task_spec = apply_user_feedback(
                create_task_spec("run analysis"),
                "use the custom reference",
                kind="plan_review_feedback",
            )

            reason = agent._stale_plan_task_revision_reason(
                {"task_spec": task_spec, "task_revision": 2},
                "task-a",
            )

        self.assertIn("effective task is revision 2", reason)
        self.assertIn("generated from task revision 1", reason)

    def test_matching_task_and_plan_revision_is_current(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            plan = build_plan_state("task-a", "- [ ] Step 1: Run analysis")
            bind_plan_to_task_revision(plan, 1)
            save_plan_state(tmpdir, "task-a", plan)

            reason = agent._stale_plan_task_revision_reason(
                {"task_spec": create_task_spec("run analysis"), "task_revision": 1},
                "task-a",
            )

        self.assertEqual(reason, "")

    def test_pending_action_and_supervisor_feedback_are_revision_bound(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            plan = build_plan_state(
                "task-a",
                "- [>] Step 1: Run analysis | Action: EXECUTE\nSuccess criteria: output exists",
            )
            bind_plan_to_task_revision(plan, 1)
            save_plan_state(tmpdir, "task-a", plan)
            state = {
                "task_spec": create_task_spec("run analysis"),
                "task_revision": 1,
                "execution_evidence": {"execution_id": "exec-current"},
            }
            action = agent._bind_pending_action({"type": "execute", "code": "echo ok"}, state, "task-a")

            self.assertEqual(agent._pending_action_stale_reason(action, state, "task-a"), "")
            self.assertIn("stale", agent._pending_action_stale_reason(action, {**state, "task_revision": 2}, "task-a"))

            state["last_supervisor_feedback"] = {
                "step_id": "step-1",
                "task_revision": 1,
                "plan_version": "plan_v2",
                "execution_id": "exec-old",
                "required_fix": ["old fix"],
            }
            self.assertEqual(agent._fresh_supervisor_feedback(state, "task-a"), {})


if __name__ == "__main__":
    unittest.main()

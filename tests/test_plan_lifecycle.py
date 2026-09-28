import tempfile
import unittest
import json
import shutil
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.plan_tracker import (
    TASK_STATUS_BLOCKED,
    approve_plan_state,
    block_plan_and_downstream,
    bind_plan_to_task_revision,
    build_plan_state,
    commit_consultant_candidate_state,
    load_plan_state,
    save_plan_state,
    sync_plan_state_from_stage_status,
    task_status,
)
from agent_core.task_spec import create_task_spec
from agent_core.recovery import ExecutionRecord, RecoveryStateStore
from agent_core.completion_gate import CompletionGate


_LIFECYCLE_TEST_SKILL_ID = "tests/plan-lifecycle"
_LIFECYCLE_TEST_CAPABILITY_ID = "tests.plan_lifecycle.execute"


class _LifecycleSkillManager:
    """Minimal deterministic registry for action-lifecycle unit fixtures."""

    def get_execution_capability_registry(self, skill_ids):
        if _LIFECYCLE_TEST_SKILL_ID not in set(skill_ids or []):
            return {}
        return {
            _LIFECYCLE_TEST_CAPABILITY_ID: {
                "schema_version": 1,
                "capability_id": _LIFECYCLE_TEST_CAPABILITY_ID,
                "selected_skill_id": _LIFECYCLE_TEST_SKILL_ID,
                "required_runtime_binding_ids": [],
                "optional_runtime_binding_ids": [],
                "required_artifact_binding_ids": [],
                "execution_guidance": [],
            }
        }

    @staticmethod
    def get_renderer_record(_skill_id):
        return {}


class _StateManager:
    def __init__(self, query="run analysis", status="paused_retryable"):
        self.query = query
        self.status = status

    def get_task_context(self, _task_id):
        return {"task_info": {"user_query": self.query, "status": self.status}}

    def update_task_status(self, _task_id, status):
        self.status = status


class PlanLifecycleTests(unittest.TestCase):

    @staticmethod
    def _approved_plan(
        task_id="task-a",
        *,
        execution_capability=False,
        purpose="analysis",
        goal="run analysis",
    ):
        state = build_plan_state(
            task_id,
            "- [>] Step 1: Run | Action: EXECUTE | Skills: [] | Done: output exists",
        )
        state["steps"][0]["purpose"] = purpose
        if execution_capability:
            state["steps"][0].update({
                "skill_ids": [_LIFECYCLE_TEST_SKILL_ID],
                "selected_skill_id": _LIFECYCLE_TEST_SKILL_ID,
                "execution_capability_id": _LIFECYCLE_TEST_CAPABILITY_ID,
            })
        bind_plan_to_task_revision(state, 1)
        state["current_task"] = {"task_spec": create_task_spec(goal)}
        approve_plan_state(state, 1)
        return state















    def test_plan_approval_does_not_modify_task_spec_revision(self):
        state = build_plan_state(
            "task-a",
            "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: output exists",
        )
        state["current_task"] = {"task_spec": create_task_spec("run analysis")}
        before = state["current_task"]["task_spec"]["revision"]

        approve_plan_state(state, before)

        self.assertEqual(state["current_task"]["task_spec"]["revision"], before)
        self.assertEqual(state["approved_task_revision"], before)




    def test_blocked_step_is_terminal_and_propagates_downstream(self):
        state = build_plan_state(
            "task-a",
            "- [>] Step 1: Analyze | Action: EXECUTE | Skills: [] | Done: result exists\n"
            "- [ ] Step 2: Report | Action: EXECUTE | Skills: [] | Done: report exists",
        )

        changed = block_plan_and_downstream(state, "required input unavailable")

        self.assertTrue(changed)
        self.assertEqual(task_status(state), TASK_STATUS_BLOCKED)
        self.assertEqual([step["status"] for step in state["steps"]], ["blocked", "skipped_upstream_blocked"])

    def test_blocked_stage_status_does_not_mark_plan_done(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "- [>] Step 1: Analyze | Action: EXECUTE | Skills: [] | Done: result exists\n"
                "- [ ] Step 2: Report | Action: EXECUTE | Skills: [] | Done: report exists",
            )
            status_path = Path(root) / "stage_status.tsv"
            status_path.write_text(
                "stage\tstatus\toutput_path\tnotes\nanalysis\tblocked\t-\tmissing runtime\n",
                encoding="utf-8",
            )

            changed = sync_plan_state_from_stage_status(state, status_path)

        self.assertEqual(changed, 2)
        self.assertEqual([step["status"] for step in state["steps"]], ["blocked", "skipped_upstream_blocked"])



































if __name__ == "__main__":
    unittest.main()

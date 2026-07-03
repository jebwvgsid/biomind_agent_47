import json
import tempfile
import unittest
from pathlib import Path

from agent_core.plan_tracker import (
    build_plan_state,
    ensure_plan_state,
    load_plan_state,
    mark_first_pending_running,
    mark_plan_step_done,
    parse_plan_steps,
    record_current_step_evidence,
    render_plan_progress,
    reset_plan_state,
    save_plan_state,
    sync_plan_state_from_stage_status,
    update_plan_markdown_status,
)


class PlanTrackerTests(unittest.TestCase):
    def test_parse_execution_checklist(self):
        plan = """
## Execution Checklist
- [ ] Step 1: Inspect inputs | Goal: verify files | Action: SEARCH | Done: paths confirmed
- [ ] Step 2: Run QC | Goal: produce report | Action: EXECUTE | Done: report exists
"""

        steps = parse_plan_steps(plan)

        self.assertEqual([step["step_id"] for step in steps], ["step-1", "step-2"])
        self.assertEqual(steps[0]["status"], "pending")
        self.assertIn("Inspect inputs", steps[0]["title"])

    def test_parse_ignores_validation_checklist_items(self):
        plan = """
## Execution Checklist
- [ ] Step 1: Run standard wrapper | Goal: produce report | Action: EXECUTE | Done: report exists

## Section 4: Validation Checklist
- [ ] Confirm all expected output files exist.
- [ ] Check QC thresholds.
"""

        steps = parse_plan_steps(plan)

        self.assertEqual(len(steps), 1)
        self.assertIn("Run standard wrapper", steps[0]["title"])
        self.assertNotIn("Confirm all expected output files", steps[0]["title"])

    def test_parse_legacy_numbered_steps(self):
        plan = """
## Section 2: Step-by-Step Execution Plan
Step 1: Collect inputs
  - Tool: shell
Step 2: Validate outputs
  - Tool: Python
"""

        steps = parse_plan_steps(plan)

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["step_id"], "step-1")
        self.assertIn("Collect inputs", steps[0]["title"])

    def test_save_load_and_render_progress(self):
        plan = "- [ ] Step 1: Collect inputs\n- [ ] Step 2: Run analysis\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            state = build_plan_state("task_a", plan, plan_file=Path(tmpdir) / "task_outputs" / "task_a" / "plan.md")
            save_plan_state(tmpdir, "task_a", state)
            loaded = load_plan_state(tmpdir, "task_a")

            mark_first_pending_running(loaded, "entered generate")
            progress = render_plan_progress(loaded)

        self.assertIn("- [>] Step 1: Collect inputs", progress)
        self.assertIn("Next step: Step 1: Collect inputs", progress)

    def test_update_plan_markdown_status(self):
        plan = "- [ ] Step 1: Collect inputs\n- [ ] Step 2: Run analysis\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / "task_outputs" / "task_a" / "plan.md"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(plan, encoding="utf-8")
            state = build_plan_state("task_a", plan, plan_file=plan_file)

            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "success")
            update_plan_markdown_status(plan_file, state)
            content = plan_file.read_text(encoding="utf-8")

        self.assertIn("- [x] Step 1: Collect inputs", content)
        self.assertIn("- [ ] Step 2: Run analysis", content)

    def test_evidence_and_reset(self):
        plan = "- [ ] Step 1: Collect inputs\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_a", plan_text=plan)
            mark_first_pending_running(state, "started")
            record_current_step_evidence(state, "supervisor_verdict", "RETRY")
            save_plan_state(tmpdir, "task_a", state)

            data = json.loads((Path(tmpdir) / "task_outputs" / "task_a" / "plan_state.json").read_text(encoding="utf-8"))
            reset_plan_state(tmpdir, "task_a")

            self.assertEqual(data["steps"][0]["status"], "running")
            self.assertEqual(data["steps"][0]["evidence"][-1]["event"], "supervisor_verdict")
            self.assertFalse((Path(tmpdir) / "task_outputs" / "task_a" / "plan_state.json").exists())

    def test_terminal_stage_status_syncs_desynchronised_plan(self):
        plan = """
- [ ] Step 1: Inspect grouping | Action: EXECUTE | Done: grouping decision recorded
- [ ] Step 2: Run preprocess | Action: EXECUTE | Done: seurat_norm.rds generated
- [ ] Step 3: Generate report | Action: EXECUTE | Done: report.md generated
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            status = Path(tmpdir) / "run" / "stage_status.tsv"
            status.parent.mkdir(parents=True)
            status.write_text(
                "stage\tstatus\treason\n"
                "preprocess\tcompleted\tok\n"
                "cluster\tcompleted\tok\n"
                "de_analysis\tstage_blocked_de\tno contrast\n"
                "scrna_report\tcompleted\treport.md generated\n",
                encoding="utf-8",
            )
            state = build_plan_state("task_a", plan)
            mark_first_pending_running(state, "started")

            changed = sync_plan_state_from_stage_status(state, status)

        self.assertEqual(changed, 3)
        self.assertTrue(all(step["status"] == "done" for step in state["steps"]))
        self.assertEqual(state["steps"][0]["evidence"][-1]["event"], "stage_status_sync")


if __name__ == "__main__":
    unittest.main()

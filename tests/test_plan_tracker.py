import json
import tempfile
import unittest
from pathlib import Path

from agent_core import plan_tracker as plan_tracker_module
from agent_core.plan_tracker import (
    apply_artifact_reconciliation,
    approve_plan_state,
    bind_plan_to_task_revision,
    build_plan_state,
    carry_forward_plan_lifecycle,
    collapse_renderer_composite_steps,
    ensure_plan_state,
    load_plan_state,
    mark_first_pending_running,
    mark_plan_step_done,
    plan_contract_violations,
    parse_plan_steps,
    record_current_step_evidence,
    render_plan_progress,
    reset_plan_state,
    save_plan_state,
    sync_plan_state_from_stage_status,
    update_plan_markdown_status,
    update_task_state_projection,
    validate_plan_for_generate,
)


class PlanTrackerTests(unittest.TestCase):
    def test_renderer_manifest_reconciles_legacy_artifact_without_retry_budget(self):
        state = build_plan_state(
            "task_a",
            "- [>] Step 1: Render database script | Action: EXECUTE | Done: build_db.sh exists\n",
        )
        before = dict(state["task_state"]["budgets"])
        manifest = {
            "outdir": "/tmp/rendered",
            "artifacts": [{
                "artifact_id": "snpeff_database_build_script",
                "path": "02.build_database.sh",
                "artifact_type": "script",
                "required": True,
                "producer_step_id": "step-1",
                "validator": "nonempty_executable_script",
            }],
        }

        events = apply_artifact_reconciliation(
            state,
            "step-1",
            manifest,
            manifest_path="/tmp/rendered/manifest.json",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["old_path"], "build_db.sh")
        self.assertEqual(events[0]["new_path"], "02.build_database.sh")
        self.assertEqual(state["steps"][0]["artifacts"][0]["source"], "renderer_manifest")
        budgets = state["task_state"]["budgets"]
        self.assertEqual(budgets["contract_reconciliation_count"], 1)
        self.assertEqual(budgets["execution_retry_count"], before["execution_retry_count"])
        self.assertEqual(budgets["generate_attempts_by_step"], before["generate_attempts_by_step"])

    def test_renderer_internal_steps_collapse_to_one_composite_execute_step(self):
        state = build_plan_state(
            "task_a",
            "- [ ] Step 1: Preflight | Action: EXECUTE | Skills: [genomics/snpeff-annotation] | Inputs: [a.vcf] | Done: runtime ready\n"
            "- [ ] Step 2: Build database | Action: EXECUTE | Skills: [genomics/snpeff-annotation] | Outputs: [database] | Done: database ready\n"
            "- [ ] Step 3: Annotate | Action: EXECUTE | Skills: [genomics/snpeff-annotation] | Outputs: [annotated.vcf] | Done: annotated.vcf exists\n",
        )

        normalized, audit = collapse_renderer_composite_steps(
            state,
            ["genomics/snpeff-annotation"],
        )

        self.assertEqual(len(normalized["steps"]), 1)
        step = normalized["steps"][0]
        self.assertTrue(step["renderer_composite"])
        self.assertTrue(step["skills_declared"])
        self.assertEqual(step["tool"], "renderer")
        self.assertIn("annotated.vcf", step["outputs"])
        self.assertEqual(audit[0]["removed_step_ids"], ["step-2", "step-3"])

    def test_semantic_plan_patch_surface_is_retired(self):
        for name in (
            "PLAN_PATCH_OPERATION",
            "build_plan_repair_request",
            "parse_plan_patch",
            "apply_plan_patch",
        ):
            self.assertFalse(hasattr(plan_tracker_module, name), name)

    def test_load_migrates_old_artifact_path_from_referenced_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            rendered = root / "rendered"
            rendered.mkdir()
            script = rendered / "02.build_database.sh"
            script.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
            script.chmod(0o700)
            manifest = rendered / "manifest.json"
            manifest.write_text(json.dumps({
                "outdir": str(rendered),
                "artifacts": [{
                    "artifact_id": "snpeff_database_build_script",
                    "path": script.name,
                    "artifact_type": "script",
                    "producer_step_id": "step-1",
                    "validator": "nonempty_executable_script",
                }],
            }), encoding="utf-8")
            state = build_plan_state(
                "task_a",
                "- [>] Step 1: Render database script | Action: EXECUTE | Done: build_db.sh exists\n",
            )
            state["steps"][0]["evidence"].append({
                "event": "execute_success",
                "detail": f"renderer output: {manifest}",
            })
            save_plan_state(root, "task_a", state)

            loaded = load_plan_state(root, "task_a")

        artifact = loaded["steps"][0]["artifacts"][0]
        self.assertEqual(artifact["path"], "02.build_database.sh")
        self.assertEqual(artifact["source"], "renderer_manifest")
        self.assertIn("reconcile_renderer_artifact_manifest", loaded["migrations_applied"])

    def test_plan_contract_violations_report_exact_step_and_field(self):
        state = build_plan_state(
            "task_a",
            "- [ ] Step 1: Inspect | Action: SEARCH | Skills: [] | Done: N/A\n"
            "- [ ] Step 2: Execute | Skills: [] | Done: output exists\n",
        )

        violations = plan_contract_violations(state)

        self.assertEqual(
            violations,
            [
                {"step_id": "step-1", "field": "success_criteria", "reason": "steps[0].success_criteria is required."},
                {"step_id": "step-2", "field": "action", "reason": "steps[1].action is required."},
            ],
        )

    def test_persisted_nonempty_skill_ids_heal_stale_declared_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = build_plan_state(
                "task_a",
                "- [>] Step 1: Run renderer | Action: EXECUTE | Skills: [genomics/snpeff-annotation] "
                "| Done: renderer completes\n",
            )
            state["steps"][0]["skills_declared"] = False
            path = Path(tmpdir) / "task_outputs" / "task_a" / "plan_state.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded = load_plan_state(tmpdir, "task_a")

        self.assertEqual(loaded["steps"][0]["skill_ids"], ["genomics/snpeff-annotation"])
        self.assertTrue(loaded["steps"][0]["skills_declared"])

    def test_task_state_projection_is_atomic_and_versioned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = update_task_state_projection(
                tmpdir,
                "task_a",
                {"current_step_id": "step-1", "diagnosis_state": {"status": "probing"}},
                node="supervisor",
                next_step="generate",
            )
            second = update_task_state_projection(
                tmpdir,
                "task_a",
                {"current_step_id": "step-2"},
                node="generate",
                next_step="execute",
            )

        self.assertEqual(first["task_state"]["state_version"], 1)
        self.assertEqual(second["task_state"]["state_version"], 2)
        self.assertEqual(second["task_state"]["diagnosis_state"], {"status": "probing"})
        self.assertEqual(second["task_state"]["last_transition"]["next_step"], "execute")

    def test_plan_can_be_bound_to_effective_task_revision(self):
        state = build_plan_state("task_a", "- [ ] Step 1: Run analysis")

        bind_plan_to_task_revision(state, 4)

        self.assertEqual(state["generated_from_task_revision"], 4)
        self.assertEqual(state["steps"][0]["generated_from_task_revision"], 4)

    def test_parse_execution_checklist(self):
        plan = """
## Execution Checklist
- [ ] Step 1: Inspect inputs | Goal: verify files | Action: SEARCH | Purpose: validation | Skills: [] | Stage: input_inventory | Batch: 1 | Done: paths confirmed
- [ ] Step 2: Run QC | Goal: produce report | Action: EXECUTE | Purpose: analysis | Skills: [scrna/qc/filter] | Stage: qc_filter | Batch: 1 | Done: report exists
"""

        steps = parse_plan_steps(plan)

        self.assertEqual([step["step_id"] for step in steps], ["step-1", "step-2"])
        self.assertEqual(steps[0]["status"], "pending")
        self.assertIn("Inspect inputs", steps[0]["title"])
        self.assertTrue(steps[0]["skills_declared"])
        self.assertEqual(steps[0]["skill_ids"], [])
        self.assertEqual(steps[1]["skill_ids"], ["scrna/qc/filter"])
        self.assertEqual(steps[1]["action"], "execute")
        self.assertEqual(steps[0]["purpose"], "validation")
        self.assertEqual(steps[1]["purpose"], "analysis")
        self.assertEqual(steps[1]["workflow_stage_ids"], ["qc_filter"])
        self.assertEqual(steps[1]["batch_id"], "1")
        self.assertEqual(steps[0]["success_criteria"], ["paths confirmed"])
        self.assertEqual(steps[1]["success_criteria"], ["report exists"])

    def test_legacy_validation_stage_backfills_step_purpose(self):
        steps = parse_plan_steps(
            "- [ ] Step 1: Inspect inputs | Action: EXECUTE | Skills: [] "
            "| Stage: input_validation | Done: inputs verified\n"
        )

        self.assertEqual(steps[0]["purpose"], "validation")

    def test_load_backfills_done_contract_for_existing_plan_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "task_outputs" / "task_a" / "plan_state.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "task_id": "task_a",
                        "steps": [
                            {
                                "step_id": "step-1",
                                "title": "Step 1: Check env | Action: SEARCH | Done: tools verified",
                                "status": "running",
                                "success_criteria": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_plan_state(tmpdir, "task_a")

        self.assertEqual(loaded["steps"][0]["success_criteria"], ["tools verified"])

    def test_success_aliases_and_multiline_done_are_step_scoped(self):
        plan = """
Done: this top-level note is not a step contract
## Execution Checklist
- [ ] Step 1: Check inputs | Action: SEARCH | Skills: [] | Done:
  - tools verified
  - inputs exist
- [ ] Step 2: Annotate | Action: EXECUTE | Skills: []
  Completion criteria: annotated.vcf is non-empty
- [ ] Step 3: Verify database | Action: SEARCH | Skills: [] | Verify: database can be loaded
"""

        steps = parse_plan_steps(plan)

        self.assertEqual(steps[0]["success_criteria"], ["tools verified", "inputs exist"])
        self.assertEqual(steps[1]["success_criteria"], ["annotated.vcf is non-empty"])
        self.assertEqual(steps[2]["success_criteria"], ["database can be loaded"])
        self.assertNotIn("top-level note", json.dumps(steps, ensure_ascii=False))

    def test_empty_or_na_success_is_not_valid(self):
        steps = parse_plan_steps(
            "- [ ] Step 1: Check | Action: SEARCH | Skills: [] | Done: N/A\n"
            "- [ ] Step 2: Check | Action: SEARCH | Skills: [] | Success:\n"
        )

        self.assertEqual(steps[0]["success_criteria"], [])
        self.assertEqual(steps[1]["success_criteria"], [])

    def test_legacy_step_is_marked_without_explicit_skill_mapping(self):
        steps = parse_plan_steps("- [ ] Step 1: Run QC | Action: EXECUTE | Done: report exists")

        self.assertEqual(len(steps), 1)
        self.assertFalse(steps[0]["skills_declared"])
        self.assertEqual(steps[0]["skill_ids"], [])

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

    def test_migration_is_persisted_idempotently_and_preserves_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "task_outputs" / "task_a" / "plan_state.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({
                    "task_id": "task_a",
                    "unknown_extension": {"keep": True},
                    "generated_from_task_revision": 3,
                    "steps": [{
                        "step_id": "step-1",
                        "title": "Step 1: Check | Action: SEARCH | Skills: [] | Done: tools verified",
                        "status": "running",
                    }],
                }),
                encoding="utf-8",
            )

            first = load_plan_state(tmpdir, "task_a")
            first_text = path.read_text(encoding="utf-8")
            second = load_plan_state(tmpdir, "task_a")
            second_text = path.read_text(encoding="utf-8")

        self.assertEqual(first_text, second_text)
        self.assertEqual(first["unknown_extension"], {"keep": True})
        self.assertEqual(second["generated_from_task_revision"], 3)
        self.assertEqual(second["steps"][0]["status"], "running")
        self.assertEqual(second["plan_status"], "pending_review")
        self.assertFalse(second["can_generate"])
        self.assertTrue(second["validation_recompute_required"])
        self.assertIn("recompute_plan_contract_validation_v7", second["migrations_applied"])
        self.assertIn("normalize_plan_step_schema", second["migrations_applied"])

    def test_semantic_noop_preserves_approval_but_real_change_invalidates_it(self):
        previous = build_plan_state(
            "task_a",
            "- [ ] Step 1: Check | Action: SEARCH | Skills: [] | Done: tools verified",
        )
        bind_plan_to_task_revision(previous, 1)
        approve_plan_state(previous, 1)
        equivalent = build_plan_state(
            "task_a",
            "- [ ] Step 1: Check | Action: SEARCH | Skills: [] | Success criteria: tools verified",
        )
        bind_plan_to_task_revision(equivalent, 1)

        carried = carry_forward_plan_lifecycle(previous, equivalent)

        self.assertEqual(carried["plan_status"], "approved")
        self.assertTrue(validate_plan_for_generate(carried, 1)["valid"])

        changed = build_plan_state(
            "task_a",
            "- [ ] Step 1: Check | Action: SEARCH | Skills: [] | Done: inputs verified",
        )
        bind_plan_to_task_revision(changed, 1)
        changed = carry_forward_plan_lifecycle(previous, changed)
        self.assertEqual(changed["plan_status"], "pending_review")
        self.assertEqual(validate_plan_for_generate(changed, 1)["reason_code"], "plan_not_approved")

    def test_validator_reports_exact_missing_success_field(self):
        state = build_plan_state(
            "task_a",
            "- [ ] Step 1: Check | Action: SEARCH | Skills: [] | Done: N/A",
        )
        bind_plan_to_task_revision(state, 1)
        approve_plan_state(state, 1)

        validation = validate_plan_for_generate(state, 1)

        self.assertEqual(validation["reason_code"], "missing_success_criteria")
        self.assertEqual(validation["missing_fields"], ["steps[0].success_criteria"])

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

    def test_skipped_checkbox_can_be_reopened_in_plan_markdown(self):
        plan = (
            "- [x] Step 1: Validate inputs | Action: EXECUTE | Done: inputs valid\n"
            "- [-] Step 2: Run analysis | Action: EXECUTE | Done: output exists\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_file = Path(tmpdir) / "task_outputs" / "task_a" / "plan.md"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(plan, encoding="utf-8")
            state = build_plan_state("task_a", plan, plan_file=plan_file)
            state["steps"][1]["status"] = "running"

            update_plan_markdown_status(plan_file, state)
            content = plan_file.read_text(encoding="utf-8")

        self.assertEqual(state["steps"][1]["status"], "running")
        self.assertIn("- [>] Step 2: Run analysis", content)

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
        self.assertEqual(
            [step["status"] for step in state["steps"]],
            ["blocked", "skipped_upstream_blocked", "skipped_upstream_blocked"],
        )
        self.assertEqual(state["steps"][0]["evidence"][-1]["event"], "blocked")


if __name__ == "__main__":
    unittest.main()

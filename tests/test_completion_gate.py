import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from agent_core.completion_gate import CompletionGate, _candidate_output_roots_from_text, _contract_exists
from agent_core.plan_tracker import (
    apply_artifact_reconciliation,
    build_plan_state,
    ensure_plan_state,
    load_plan_state,
    mark_first_pending_running,
    mark_plan_step_done,
    record_current_step_evidence,
    register_renderer_manifest,
    save_plan_state,
)


class CompletionGateTests(unittest.TestCase):
    def test_registered_renderer_actions_must_finish_before_step_or_final_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest = {
                "schema_version": 3,
                "outdir": str(root),
                "renderer": {"id": "test.renderer"},
                "provenance": {
                    "task_id": "task-a",
                    "step_id": "step-1",
                    "producer_action_id": "render-a",
                },
                "actions": [
                    {"id": "preflight", "script": "00.preflight.sh", "required": True},
                    {"id": "run", "script": "01.run.sh", "depends_on": ["preflight"], "required": True},
                ],
                "artifacts": [],
                "requirements": [],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            state = build_plan_state(
                "task-a",
                "- [>] Step 1: Run renderer workflow | Action: EXECUTE | Skills: [test/renderer] "
                "| Done: all manifest actions complete",
            )
            register_renderer_manifest(
                state,
                "step-1",
                manifest,
                manifest_path=manifest_path,
                task_id="task-a",
                producer_action_id="render-a",
            )

            step_result = CompletionGate.validate_current_step(root, "task-a", state)
            final_result = CompletionGate.validate_final(root, "task-a", state)

        self.assertEqual(step_result.classification, "renderer_actions_incomplete")
        self.assertEqual(final_result.classification, "renderer_actions_incomplete")
        self.assertEqual(
            step_result.reconciliation["remaining_action_ids"],
            ["preflight", "run"],
        )

    def test_nonempty_directory_contract_is_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "results"
            output.mkdir()
            (output / "part.tsv").write_text("value\n", encoding="utf-8")

            self.assertTrue(_contract_exists({
                "path": "results",
                "base_dir": str(root),
                "validator": "nonempty_directory",
                "required": True,
            }, [root]))

    def test_gate_result_has_serializable_classification(self):
        from agent_core.completion_gate import GateResult

        result = GateResult(
            passed=False,
            message="required artifact is missing",
            feedback="create report.md",
            missing_artifacts=["report.md"],
            checked_artifacts=["stage_status.tsv"],
            classification="artifact_missing",
        )

        value = result.to_dict()
        self.assertEqual(value["classification"], "artifact_missing")
        self.assertEqual(value["missing_artifacts"], ["report.md"])
        self.assertFalse(value["passed"])

    def test_step_gate_rejects_old_nonempty_artifact_without_current_producer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report = root / "report.md"
            report.write_text("# result from an older task\n", encoding="utf-8")
            old_time = time.time() - 3600
            os.utime(report, (old_time, old_time))
            state = ensure_plan_state(
                root,
                "task_stale",
                plan_text="- [ ] Step 1: Generate report.md | Action: EXECUTE | Done: report.md exists\n",
            )
            mark_first_pending_running(state, "started")
            execution = root / "task_outputs" / "task_stale" / "step_1.sh"
            execution.parent.mkdir(parents=True, exist_ok=True)
            execution.write_text(f"test -s {report}\n", encoding="utf-8")
            state["steps"][0]["evidence"].append({
                "event": "execute_success",
                "detail": f"{execution}\nSTDOUT:\nverified {report}\n",
            })
            save_plan_state(root, "task_stale", state)

            result = CompletionGate.validate_current_step(
                root, "task_stale", load_plan_state(root, "task_stale")
            )

        self.assertFalse(result.passed)
        self.assertEqual(result.classification, "stale_preexisting_artifact")
        self.assertIn("report.md", result.missing_artifacts)

    def _renderer_contract_state(self, root: Path, *, create_script: bool = True):
        rendered = root / "rendered"
        rendered.mkdir(parents=True)
        physical = rendered / "02.build_database.sh"
        if create_script:
            physical.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
            physical.chmod(0o700)
        manifest_data = {
            "manifest_type": "renderer_artifact_contract",
            "outdir": str(rendered),
            "artifacts": [{
                "artifact_id": "snpeff_database_build_script",
                "path": physical.name,
                "artifact_type": "script",
                "required": True,
                "producer_step_id": "step-1",
                "producer": "renderer.py",
                "validator": "nonempty_executable_script",
            }],
        }
        manifest = rendered / "manifest.json"
        manifest.write_text(__import__("json").dumps(manifest_data), encoding="utf-8")
        state = ensure_plan_state(
            root,
            "task_contract",
            plan_text="- [ ] Step 1: Render database build script | Action: EXECUTE | Done: build_db.sh exists\n",
        )
        mark_first_pending_running(state, "started")
        execution = root / "task_outputs" / "task_contract" / "step_1.sh"
        execution.parent.mkdir(parents=True, exist_ok=True)
        execution.write_text(f"python renderer.py --outdir {rendered}\n", encoding="utf-8")
        state["steps"][0]["evidence"].append({
            "event": "execute_success",
            "detail": f'{execution}\nSTDOUT:\n{{"outdir": "{rendered}", "scripts": ["02.build_database.sh"]}}\n',
        })
        save_plan_state(root, "task_contract", state)
        return state, manifest_data, manifest

    def test_manifest_contract_drift_reconciles_then_passes_without_generate_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state, manifest_data, manifest = self._renderer_contract_state(root)
            first = CompletionGate.validate_current_step(root, "task_contract", state)

            self.assertEqual(first.classification, "contract_reconciliation_required")
            events = apply_artifact_reconciliation(
                state,
                "step-1",
                manifest_data,
                manifest_path=manifest,
            )
            save_plan_state(root, "task_contract", state)
            second = CompletionGate.validate_current_step(root, "task_contract", load_plan_state(root, "task_contract"))

            state["steps"][0]["status"] = "done"
            final = CompletionGate.validate_final(
                root,
                "task_contract",
                state,
                "Outputs: build_db.sh\n",
            )

        self.assertEqual(len(events), 1)
        self.assertTrue(second.passed, second.feedback)
        self.assertTrue(final.passed, final.feedback)
        self.assertNotIn("build_db.sh", final.checked_artifacts)
        self.assertIn("02.build_database.sh", final.checked_artifacts)
        budgets = state["task_state"]["budgets"]
        self.assertEqual(budgets["contract_reconciliation_count"], 1)
        self.assertEqual(budgets["generate_attempts_by_step"], {})
        self.assertEqual(budgets["execution_retry_count"], 0)

    def test_manifest_canonical_artifact_missing_is_real_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _, manifest_data, manifest = self._renderer_contract_state(root, create_script=False)
            state = load_plan_state(root, "task_contract")
            apply_artifact_reconciliation(state, "step-1", manifest_data, manifest_path=manifest)
            save_plan_state(root, "task_contract", state)

            result = CompletionGate.validate_current_step(root, "task_contract", load_plan_state(root, "task_contract"))

        self.assertFalse(result.passed)
        self.assertEqual(result.classification, "artifact_missing")
        self.assertIn("02.build_database.sh", result.missing_artifacts)

    def test_similar_artifact_without_manifest_requires_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidate = root / "02.build_database.sh"
            candidate.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
            candidate.chmod(0o700)
            state = ensure_plan_state(
                root,
                "task_contract",
                plan_text="- [ ] Step 1: Render database build script | Action: EXECUTE | Done: build_db.sh exists\n",
            )
            mark_first_pending_running(state, "started")
            execution = root / "task_outputs" / "task_contract" / "step_1.sh"
            execution.parent.mkdir(parents=True, exist_ok=True)
            execution.write_text("python renderer.py\n", encoding="utf-8")
            record_current_step_evidence(state, "execute_success", f"{execution}\nSTDOUT:\nrender complete\n")
            save_plan_state(root, "task_contract", state)

            result = CompletionGate.validate_current_step(root, "task_contract", load_plan_state(root, "task_contract"))

        self.assertFalse(result.passed)
        self.assertEqual(result.classification, "diagnostic_probe_required")
        self.assertIn(str(candidate), str(result.reconciliation))

    def test_degraded_manifest_requires_complete_scientific_disclosure(self):
        plan = (
            "- [x] Step 1: Finalize outputs | Action: EXECUTE | Done: report.md, stage_status.tsv, and reproduce.sh exist\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "manifest.json").write_text(__import__("json").dumps({
                "manifest_type": "renderer_artifact_contract",
                "outdir": str(root),
                "validation": {
                    "validation_mode": "unchecked_missing_cds_protein",
                    "skipped_checks": ["CDS", "protein"],
                    "required_disclosures": ["report.md", "stage_status.tsv", "reproduce.sh"],
                    "reproduction_markers": ["--allow-unchecked-build"],
                },
            }), encoding="utf-8")
            for name in ("report.md", "stage_status.tsv", "reproduce.sh"):
                (root / name).write_text("completed\n", encoding="utf-8")
            state = ensure_plan_state(root, "task_degraded", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "done")
            save_plan_state(root, "task_degraded", state)

            failed = CompletionGate.validate_final(root, "task_degraded", load_plan_state(root, "task_degraded"), plan)
            (root / "report.md").write_text(
                "Degraded validation: CDS and protein consistency checks were skipped.\n",
                encoding="utf-8",
            )
            (root / "stage_status.tsv").write_text(
                "stage\tstatus\tnotes\nbuild\tdegraded\tCDS and protein checks skipped\n",
                encoding="utf-8",
            )
            (root / "reproduce.sh").write_text(
                "#!/bin/bash\npython renderer.py --allow-unchecked-build\n",
                encoding="utf-8",
            )
            passed = CompletionGate.validate_final(root, "task_degraded", load_plan_state(root, "task_degraded"), plan)

        self.assertFalse(failed.passed)
        self.assertEqual(failed.classification, "incomplete_degraded_validation_disclosure")
        self.assertTrue(passed.passed, passed.feedback)

    def test_code_boundary_gate_does_not_semantically_block_wrapper(self):
        context = "请使用 BioMind 的 3d-dna/tad-calling skill 对 leaf 和 bud 两个 mcool 文件做 TAD calling。"
        code = (
            "python Biomind_v1/skills/3d-dna/tad-calling/scripts/call_tads.py "
            "--sample leaf leaf.mcool --sample bud bud.mcool --resolution-bp 40000"
        )

        result = CompletionGate.validate_code_boundary(code, context)

        self.assertTrue(result.passed, result.message)

    def test_code_boundary_gate_does_not_hardcode_direct_tool_bans(self):
        context = "禁止直接调用 hicFindTADs 而不检查 wrapper 输出。"
        code = "hicFindTADs --matrix leaf.mcool::/resolutions/40000 --outPrefix leaf"

        result = CompletionGate.validate_code_boundary(code, context)

        self.assertTrue(result.passed, result.message)

    def test_code_boundary_gate_does_not_hardcode_analysis_bans(self):
        context = "本任务只做 preflight。禁止进行 TAD calling，不要调用任何 TAD caller。"
        code = "python Biomind_v1/skills/3d-dna/tad-calling/scripts/call_tads.py --sample leaf leaf.mcool"

        result = CompletionGate.validate_code_boundary(code, context)

        self.assertTrue(result.passed, result.message)

    def test_code_boundary_gate_rejects_benchmark_root_under_code_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "benchmarks").mkdir()
            (project_root / "Biomind_v1").mkdir()
            bad_path = project_root / "Biomind_v1" / "benchmarks" / "data" / "ont1.fq.gz"
            context = f"benchmark 根目录：{project_root / 'benchmarks'}"
            code = f"minimap2 -ax map-ont ref.fa {bad_path} | samtools sort -o ont1.sort.bam"

            result = CompletionGate.validate_code_boundary(code, context)

        self.assertFalse(result.passed)
        self.assertIn("Biomind_v1/benchmarks", result.feedback)
        self.assertIn(str(project_root / "benchmarks"), result.feedback)

    def test_code_boundary_gate_accepts_declared_sibling_benchmark_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "benchmarks").mkdir()
            good_path = project_root / "benchmarks" / "data" / "ont1.fq.gz"
            context = f"benchmark 根目录：{project_root / 'benchmarks'}"
            code = f"minimap2 -ax map-ont ref.fa {good_path} | samtools sort -o ont1.sort.bam"

            result = CompletionGate.validate_code_boundary(code, context)

        self.assertTrue(result.passed, result.feedback)

    def test_code_boundary_gate_rejects_unbounded_broad_find_scan(self):
        result = CompletionGate.validate_code_boundary(
            "find /public/home/example -name 'snpEff.jar' -type f 2>/dev/null",
            "Verify the declared runtime before annotation.",
        )

        self.assertFalse(result.passed)
        self.assertIn("unbounded broad file-system scan", result.message)
        self.assertIn("-maxdepth", result.feedback)

    def test_code_boundary_gate_allows_bounded_environment_find_scan(self):
        result = CompletionGate.validate_code_boundary(
            "find \"$CONDA_PREFIX/share\" -maxdepth 4 -name snpEff.jar -type f -print -quit",
            "Verify the declared runtime before annotation.",
        )

        self.assertTrue(result.passed, result.feedback)

    def test_step_gate_rejects_report_step_satisfied_only_by_sibling_run(self):
        plan = """
- [ ] Step 1: Document results | Goal: Create report and reproduce script | Action: EXECUTE | Done: report.md and reproduce.sh completed
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "runs" / "3d_dna"
            sibling = root / "OTHER-RUN"
            sibling.mkdir(parents=True)
            (sibling / "report.md").write_text("# sibling\n", encoding="utf-8")
            (sibling / "reproduce.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            state = ensure_plan_state(root, "task_comp", plan_text=plan)
            mark_first_pending_running(state, "started")
            script = root / "task_outputs" / "task_comp" / "step_1.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("cat 40000/leaf/orientation_validation.tsv\n", encoding="utf-8")
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": f"{script}\nSTDOUT:\norientation ok\n",
                }
            )
            save_plan_state(root, "task_comp", state)

            result = CompletionGate.validate_current_step(root, "task_comp", load_plan_state(root, "task_comp"))

        self.assertFalse(result.passed)
        self.assertIn("report.md", result.message)
        self.assertIn("reproduce.sh", result.message)

    def test_step_gate_rejects_preexisting_artifact_when_latest_command_is_unrelated(self):
        plan = """
- [ ] Step 1: Generate report.md and reproduce.sh | Goal: Document results | Action: EXECUTE | Done: report.md and reproduce.sh exist
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "report.md").write_text("# previous report\n", encoding="utf-8")
            (root / "reproduce.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            state = ensure_plan_state(root, "task_scrna", plan_text=plan)
            mark_first_pending_running(state, "started")
            script = root / "task_outputs" / "task_scrna" / "step_1.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("cat > stage_status.tsv <<'EOF'\nstage\tstatus\nEOF\ncat stage_status.tsv\n", encoding="utf-8")
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": f"{script}\nSTDOUT:\nstage\tstatus\n",
                }
            )
            save_plan_state(root, "task_scrna", state)

            result = CompletionGate.validate_current_step(root, "task_scrna", load_plan_state(root, "task_scrna"))

        self.assertFalse(result.passed)
        self.assertIn("latest command did not mention", result.feedback)
        self.assertIn("report.md", result.feedback)
        self.assertIn("reproduce.sh", result.feedback)

    def test_step_gate_requires_nonempty_binary_database_artifact(self):
        plan = """
- [ ] Step 1: Build custom database | Goal: Build the database | Action: EXECUTE | Done: data/custom_genome/snpEffectPredictor.bin exists and is non-empty
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = ensure_plan_state(root, "task_database", plan_text=plan)
            mark_first_pending_running(state, "started")
            script = root / "task_outputs" / "task_database" / "step_1.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("echo database build attempted\n", encoding="utf-8")
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": f"{script}\nSTDOUT:\ndatabase build attempted\n",
                }
            )
            save_plan_state(root, "task_database", state)

            result = CompletionGate.validate_current_step(
                root,
                "task_database",
                load_plan_state(root, "task_database"),
            )

        self.assertFalse(result.passed)
        self.assertIn("data/custom_genome/snpEffectPredictor.bin", result.missing_artifacts)

    def test_step_gate_accepts_one_of_conditional_artifacts(self):
        plan = """
- [ ] Step 1: Run RBH or write follow-up plan | Goal: Generate rbh_results.tsv or similarity_followup_plan.md | Action: EXECUTE | Done: conditional artifact exists
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "similarity_followup_plan.md").write_text("# skipped RBH\n", encoding="utf-8")

            state = ensure_plan_state(root, "task_seq", plan_text=plan)
            mark_first_pending_running(state, "started")
            script = root / "task_outputs" / "task_seq" / "step_1.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(
                f"cd {root} && cat > similarity_followup_plan.md <<'EOF'\n# skipped RBH\nEOF\n",
                encoding="utf-8",
            )
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": f"{script}\nSTDOUT:\n",
                }
            )
            save_plan_state(root, "task_seq", state)

            result = CompletionGate.validate_current_step(root, "task_seq", load_plan_state(root, "task_seq"))

        self.assertTrue(result.passed, result.feedback)
        self.assertEqual(result.missing_artifacts, [])

    def test_step_gate_uses_declared_nested_output_path_from_current_evidence(self):
        plan = """
- [ ] Step 1: Generate gene density BED at 40kbp | Goal: Create phasing track | Action: EXECUTE | Done: `gene_density.bed` exists with harmonized names and gene count > 0
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "runs" / "3d_dna"
            run_root = root / "BM-3DDNA-COMP-VAGUE-015"
            nested = run_root / "Compartments" / "PC1_AB"
            nested.mkdir(parents=True)
            (nested / "gene_density.bed").write_text("Chr03\t0\t40000\t1\n", encoding="utf-8")

            state = ensure_plan_state(root, "task_comp", plan_text=plan)
            mark_first_pending_running(state, "started")
            script = root / "task_outputs" / "task_comp" / "step_1.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(
                f"cd {run_root} && cat stage_status.tsv\n",
                encoding="utf-8",
            )
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": (
                        f"{script}\nSTDOUT:\n"
                        "gene_density_generation\tcompleted\tCompartments/PC1_AB/gene_density.bed\tok\n"
                    ),
                }
            )
            save_plan_state(root, "task_comp", state)

            result = CompletionGate.validate_current_step(root, "task_comp", load_plan_state(root, "task_comp"))

        self.assertTrue(result.passed, result.feedback)

    def test_step_gate_rejects_status_claiming_missing_report_outputs(self):
        plan = """
- [ ] Step 1: Write final status and report outputs | Goal: Generate final report, reproduce.sh, and stage_status.tsv | Action: EXECUTE | Done: final files exist
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out = root / "runs" / "remaining" / "BM-REMAIN-SCRNA-002" / "medium"
            out.mkdir(parents=True)
            status = out / "stage_status.tsv"
            status.write_text(
                "stage\tstatus\treason\n"
                "scrna_report\tcompleted\tFinal report, stage_status.tsv, and reproduce.sh generated\n",
                encoding="utf-8",
            )

            state = ensure_plan_state(root, "task_scrna", plan_text=plan)
            mark_first_pending_running(state, "started")
            script = root / "task_outputs" / "task_scrna" / "step_1.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(f"cat > {status} <<'EOF'\n...\nEOF\n", encoding="utf-8")
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": f"{script}\nSTDOUT:\n",
                }
            )
            save_plan_state(root, "task_scrna", state)

            result = CompletionGate.validate_current_step(root, "task_scrna", load_plan_state(root, "task_scrna"))

        self.assertFalse(result.passed)
        self.assertIn("report.md", result.feedback)
        self.assertIn("reproduce.sh", result.feedback)

    def test_output_root_discovery_does_not_recursively_expand_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "run").mkdir()
            (root / "output").mkdir()
            (root / "run" / "output").mkdir()

            roots = _candidate_output_roots_from_text(
                root,
                "Done: run/manifest.yaml and output/stage_status.tsv were verified",
            )

        self.assertIn(root / "run", roots)
        self.assertIn(root / "output", roots)
        self.assertNotIn(root / "run" / "output", roots)

class FinalSolutionGateTests(unittest.TestCase):
    def test_final_gate_checks_expected_artifacts_even_with_pending_steps(self):
        plan = """
- [ ] Step 1: Generate report.md and missing.tsv | Done: outputs exist

## Final outputs
- report.md
- missing.tsv
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_pending_final", plan_text=plan)
            save_plan_state(tmpdir, "task_pending_final", state)
            Path(tmpdir, "report.md").write_text("# partial\n", encoding="utf-8")

            result = CompletionGate.validate_final(
                tmpdir,
                "task_pending_final",
                load_plan_state(tmpdir, "task_pending_final"),
                plan,
            )

        self.assertFalse(result.passed)
        self.assertIn("missing.tsv", result.feedback)

    def test_final_gate_rejects_unfinished_plan_even_when_outputs_exist(self):
        plan = """
- [ ] Step 1: Generate report.md | Done: output exists
- [ ] Step 2: Run clustering | Done: clustered object exists

## Final outputs
- report.md
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_unfinished_final", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "report written")
            save_plan_state(tmpdir, "task_unfinished_final", state)
            Path(tmpdir, "report.md").write_text("# partial\n", encoding="utf-8")

            result = CompletionGate.validate_final(
                tmpdir,
                "task_unfinished_final",
                load_plan_state(tmpdir, "task_unfinished_final"),
                plan,
            )

        self.assertFalse(result.passed)
        self.assertIn("unfinished required steps", result.feedback)
        self.assertIn("Run clustering", result.feedback)

    def test_final_gate_rejects_managed_finish_without_execute_evidence(self):
        plan = """
- [ ] Step 1: Run downstream analysis | Action: EXECUTE | Done: report.md exists

## Final outputs
- report.md
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_no_execute", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "claimed done")
            save_plan_state(tmpdir, "task_no_execute", state)
            Path(tmpdir, "report.md").write_text("# unsupported\n", encoding="utf-8")

            result = CompletionGate.validate_final(
                tmpdir,
                "task_no_execute",
                load_plan_state(tmpdir, "task_no_execute"),
                plan,
                require_execution_evidence=True,
            )

        self.assertFalse(result.passed)
        self.assertIn("recorded `execute_success` evidence", result.feedback)

    def test_final_gate_rejects_managed_finish_without_supervisor_evidence(self):
        plan = """
- [ ] Step 1: Run downstream analysis | Action: EXECUTE | Done: report.md exists

## Final outputs
- report.md
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_no_supervisor", plan_text=plan)
            mark_first_pending_running(state, "started")
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": f"{Path(tmpdir) / 'task_outputs' / 'task_no_supervisor' / 'step_1.sh'}\nSTDOUT:\nok\n",
                }
            )
            mark_plan_step_done(state, "claimed done")
            save_plan_state(tmpdir, "task_no_supervisor", state)
            Path(tmpdir, "report.md").write_text("# unsupported\n", encoding="utf-8")

            result = CompletionGate.validate_final(
                tmpdir,
                "task_no_supervisor",
                load_plan_state(tmpdir, "task_no_supervisor"),
                plan,
                require_supervisor_evidence=True,
            )

        self.assertFalse(result.passed)
        self.assertIn("Supervisor PASS/WARN", result.feedback)

    def test_final_gate_accepts_managed_finish_with_execute_and_supervisor_evidence(self):
        plan = """
- [ ] Step 1: Run downstream analysis | Action: EXECUTE | Done: report.md exists

## Final outputs
- report.md
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_verified", plan_text=plan)
            mark_first_pending_running(state, "started")
            state["steps"][0]["evidence"].append(
                {
                    "event": "execute_success",
                    "detail": f"{Path(tmpdir) / 'task_outputs' / 'task_verified' / 'step_1.sh'}\nSTDOUT:\nok\n",
                }
            )
            mark_plan_step_done(state, "supervisor_verdict: PASS")
            save_plan_state(tmpdir, "task_verified", state)
            Path(tmpdir, "report.md").write_text("# supported\n", encoding="utf-8")

            result = CompletionGate.validate_final(
                tmpdir,
                "task_verified",
                load_plan_state(tmpdir, "task_verified"),
                plan,
                require_execution_evidence=True,
                require_supervisor_evidence=True,
            )

        self.assertTrue(result.passed, result.feedback)

    def test_final_gate_accepts_recovery_policy_complete_step_evidence(self):
        plan = """
- [ ] Step 1: Run downstream analysis | Action: EXECUTE | Done: report.md exists

## Final outputs
- report.md
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_policy_verified", plan_text=plan)
            mark_first_pending_running(state, "started")
            state["steps"][0]["evidence"].extend([
                {"event": "execute_success", "detail": "analysis command returned 0"},
                {"event": "recovery_decision", "detail": "COMPLETE_STEP: validation_satisfied"},
            ])
            mark_plan_step_done(state, "RecoveryPolicy accepted ValidationEvidence")
            save_plan_state(tmpdir, "task_policy_verified", state)
            Path(tmpdir, "report.md").write_text("# supported\n", encoding="utf-8")

            result = CompletionGate.validate_final(
                tmpdir,
                "task_policy_verified",
                load_plan_state(tmpdir, "task_policy_verified"),
                plan,
                require_execution_evidence=True,
                require_supervisor_evidence=True,
            )

        self.assertTrue(result.passed, result.feedback)

    def test_final_gate_rejects_done_plan_when_expected_artifacts_are_missing(self):
        plan = """
- [ ] Step 1: Run TAD calling | Done: 40000/leaf_domains.bed and report.md exist

## Final outputs
- 40000/leaf_domains.bed
- report.md
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = ensure_plan_state(tmpdir, "task_tad", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "claimed done")
            save_plan_state(tmpdir, "task_tad", state)
            Path(tmpdir, "report.md").write_text("# partial\n", encoding="utf-8")

            result = CompletionGate.validate_final(tmpdir, "task_tad", load_plan_state(tmpdir, "task_tad"), plan)

        self.assertFalse(result.passed)
        self.assertIn("leaf_domains.bed", result.feedback)

    def test_final_gate_accepts_one_of_conditional_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "similarity_followup_plan.md").write_text("# no RBH needed\n", encoding="utf-8")
            (root / "report.md").write_text("# report\n", encoding="utf-8")
            plan = f"""
- [x] Step 1: Conditional sequence follow-up | Done: rbh_results.tsv or similarity_followup_plan.md exists
- [x] Step 2: Generate report.md | Done: report.md exists

Output directory: `{root}`

## Final outputs
- rbh_results.tsv or similarity_followup_plan.md
- report.md
"""
            state = ensure_plan_state(root, "task_seq_final", plan_text=plan)
            while any(step["status"] == "pending" for step in state["steps"]):
                mark_first_pending_running(state, "started")
                mark_plan_step_done(state, "done")
            save_plan_state(root, "task_seq_final", state)

            result = CompletionGate.validate_final(root, "task_seq_final", load_plan_state(root, "task_seq_final"), plan)

        self.assertTrue(result.passed, result.feedback)
        self.assertEqual(result.missing_artifacts, [])

    def test_final_gate_searches_declared_output_directory_outside_task_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_root = Path(tmpdir) / "results" / "04.BM-3DDNA-TAD-STRICT-004"
            output_root = Path(tmpdir) / "benchmarks" / "biomind_agent_benchmark" / "runs" / "3d_dna" / "BM-3DDNA-TAD-STRICT-004" / "TADs" / "HiCExplorer"
            output_40000 = output_root / "40000"
            output_40000.mkdir(parents=True)
            task_root.mkdir(parents=True)
            for rel in [
                "40000/leaf_domains.bed",
                "40000/leaf_boundaries.bed",
                "40000/leaf_score.bedgraph",
                "40000/tad_domains_long.tsv",
                "40000/tad_domain_lengths.png",
            ]:
                (output_root / rel).write_text("ok\n", encoding="utf-8")
            (task_root / "report.md").write_text("# ok\n", encoding="utf-8")

            plan = f"""
- [ ] Step 1: Run TAD calling | Done: all files exist

Output directory: `{output_root}`

## Final outputs
- 40000/leaf_domains.bed
- 40000/leaf_boundaries.bed
- 40000/leaf_score.bedgraph
- 40000/tad_domains_long.tsv
- 40000/tad_domain_lengths.png
- report.md
"""
            state = ensure_plan_state(task_root, "task_tad", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "claimed done")
            save_plan_state(task_root, "task_tad", state)

            result = CompletionGate.validate_final(task_root, "task_tad", load_plan_state(task_root, "task_tad"), plan)

        self.assertTrue(result.passed, result.feedback)

    def test_final_gate_discovers_output_root_from_plan_evidence_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_root = Path(tmpdir) / "benchmarks" / "biomind_agent_benchmark" / "runs" / "remaining"
            output_root = data_root / "BM-REMAIN-WGS-005" / "strict"
            for rel in [
                "ref/genome.fasta",
                "variants/cohort.combined.g.vcf.gz",
                "variants/cohort.raw.vcf.gz",
                "variants/cohort.filtered.snp.vcf.gz",
                "variants/cohort.filtered.indel.vcf.gz",
                "stats/variant_summary.tsv",
                "stats/raw.bcftools.stats.txt",
                "stats/pass.snp.bcftools.stats.txt",
                "stage_status.tsv",
                "report.md",
                "reproduce.sh",
            ]:
                target = output_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("ok\n", encoding="utf-8")

            plan = f"""
- [ ] Step 1: Combine GVCFs | Done: variants/cohort.combined.g.vcf.gz exists
- [ ] Step 2: GenotypeGVCFs | Done: variants/cohort.raw.vcf.gz exists
- [ ] Step 3: Filter SNPs | Done: variants/cohort.filtered.snp.vcf.gz exists
- [ ] Step 4: Filter InDels | Done: variants/cohort.filtered.indel.vcf.gz exists
- [ ] Step 5: Generate stats | Done: stats/variant_summary.tsv exists
- [ ] Step 6: Write report | Done: stage_status.tsv, report.md, reproduce.sh exist

Reference genome: `{output_root / "ref" / "genome.fasta"}`

## Final outputs
- variants/cohort.combined.g.vcf.gz
- variants/cohort.raw.vcf.gz
- variants/cohort.filtered.snp.vcf.gz
- variants/cohort.filtered.indel.vcf.gz
- stats/variant_summary.tsv
- stats/*.bcftools.stats.txt
- stage_status.tsv
- report.md
- reproduce.sh
"""
            state = ensure_plan_state(data_root, "task_wgs", plan_text=plan)
            while any(step["status"] == "pending" for step in state["steps"]):
                mark_first_pending_running(state, "started")
                record_current_step_evidence(
                    state,
                    "execute_success",
                    f"{data_root / 'task_outputs' / 'task_wgs' / 'step.sh'}\nSTDOUT:\ncompleted\n",
                )
                mark_plan_step_done(state, "supervisor_verdict: PASS")
            save_plan_state(data_root, "task_wgs", state)

            result = CompletionGate.validate_final(
                data_root,
                "task_wgs",
                load_plan_state(data_root, "task_wgs"),
                plan,
                require_execution_evidence=True,
            )

        self.assertTrue(result.passed, result.feedback)
        self.assertEqual(result.missing_artifacts, [])

    def test_final_gate_ignores_sibling_run_artifacts(self):
        plan = """
- [x] Step 1: Document results | Goal: Create report and reproduce script | Action: EXECUTE | Done: report.md and reproduce.sh completed

## Final outputs
- report.md
- reproduce.sh
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "runs" / "3d_dna"
            sibling = root / "OTHER-RUN"
            sibling.mkdir(parents=True)
            (sibling / "report.md").write_text("# sibling\n", encoding="utf-8")
            (sibling / "reproduce.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            state = ensure_plan_state(root, "task_comp", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "claimed done")
            save_plan_state(root, "task_comp", state)

            result = CompletionGate.validate_final(root, "task_comp", load_plan_state(root, "task_comp"), plan)

        self.assertFalse(result.passed)
        self.assertIn("report.md", result.feedback)
        self.assertIn("reproduce.sh", result.feedback)

    def test_final_gate_accepts_blocked_plan_with_must_not_exist_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "runs" / "remain" / "BM-REMAIN-SCRNA-001" / "strict"
            plan = f"""
- [x] Step 1: Verify environment | Goal: Confirm missing runtime | Action: SEARCH | Done: bio_scrna_core absent
- [x] Step 2: Generate blocked status outputs | Goal: Create report.md, stage_status.tsv, reproduce.sh | Action: EXECUTE | Done: blocked files exist

Output directory: `{out}`

## Section 4: Validation Checklist

### Final Outputs to Verify
1. report.md
2. stage_status.tsv
3. reproduce.sh

### Files That Must NOT Exist
- raw_seurat_object.rds — Cannot be generated without Seurat
- qc_filtered_seurat_object.rds — Cannot be generated without QC
- qc_metrics.tsv — Do not generate fake QC output
"""
            out.mkdir(parents=True)
            (out / "report.md").write_text("# blocked\nblocked_dependency_missing\n", encoding="utf-8")
            (out / "stage_status.tsv").write_text("stage\tstatus\treason\nenvironment_check\tblocked\tmissing env\n", encoding="utf-8")
            (out / "reproduce.sh").write_text("#!/bin/bash\n# blocked verification only\n", encoding="utf-8")

            state = ensure_plan_state(tmpdir, "task_scrna", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "env absent")
            mark_first_pending_running(state, "started blocked output")
            mark_plan_step_done(state, "blocked files written")
            save_plan_state(tmpdir, "task_scrna", state)

            result = CompletionGate.validate_final(tmpdir, "task_scrna", load_plan_state(tmpdir, "task_scrna"), plan)

        self.assertTrue(result.passed, result.feedback)
        self.assertNotIn("raw_seurat_object.rds", result.missing_artifacts)
        self.assertNotIn("qc_filtered_seurat_object.rds", result.missing_artifacts)
        self.assertNotIn("qc_metrics.tsv", result.missing_artifacts)

    def test_final_gate_accepts_semantic_artifact_filenames_in_nested_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "runs" / "remain" / "BM-REMAIN-SCRNA-001" / "strict"
            objects = out / "objects"
            objects.mkdir(parents=True)
            (objects / "seurat_raw.rds").write_bytes(b"raw")
            (objects / "seurat_qc.rds").write_bytes(b"qc")
            (out / "report.md").write_text("# complete\n", encoding="utf-8")
            (out / "stage_status.tsv").write_text("stage\tstatus\ningest_qc\tcompleted\n", encoding="utf-8")
            (out / "reproduce.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            plan = f"""
- [x] Step 1: Generate raw_seurat_object.rds and qc_filtered_seurat_object.rds | Done: outputs exist
- [x] Step 2: Generate report.md, stage_status.tsv, reproduce.sh | Done: docs exist

Output directory: `{out}`

## Final outputs
- raw_seurat_object.rds
- qc_filtered_seurat_object.rds
- report.md
- stage_status.tsv
- reproduce.sh
"""
            state = ensure_plan_state(tmpdir, "task_scrna", plan_text=plan)
            while any(step["status"] == "pending" for step in state["steps"]):
                mark_first_pending_running(state, "started")
                mark_plan_step_done(state, "done")
            save_plan_state(tmpdir, "task_scrna", state)

            result = CompletionGate.validate_final(tmpdir, "task_scrna", load_plan_state(tmpdir, "task_scrna"), plan)

        self.assertTrue(result.passed, result.feedback)
        self.assertEqual(result.missing_artifacts, [])

    def test_final_gate_ignores_known_absent_inputs_sibling_branches_and_root_fragments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out = root / "benchmarks" / "biomind_agent_benchmark" / "runs" / "remaining" / "BM-REMAIN-SCRNA-002" / "vague"
            for rel in [
                "objects/seurat_norm.rds",
                "objects/seurat_cluster.rds",
                "objects/seurat_annotated.rds",
                "tables/variable_features.tsv",
                "tables/preprocess_summary.tsv",
                "tables/cluster_summary.tsv",
                "tables/markers_per_cluster.tsv",
                "tables/annotation_summary.tsv",
                "plots/variable_features.png",
                "plots/umap.png",
                "report.md",
                "reproduce.sh",
                "stage_status.tsv",
            ]:
                target = out / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("ok\n", encoding="utf-8")

            plan = f"""
- [x] Step 1: Execute Preprocess | Done: objects/seurat_norm.rds, tables/variable_features.tsv, tables/preprocess_summary.tsv, plots/variable_features.png generated.
- [x] Step 2: Execute Clustering | Done: objects/seurat_cluster.rds, plots/umap.png, tables/cluster_summary.tsv generated.
- [x] Step 3: Execute Markers | Done: tables/markers_per_cluster.tsv generated.
- [x] Step 4: Execute Annotation | Done: objects/seurat_annotated.rds and tables/annotation_summary.tsv generated.
- [x] Step 5: Write final report | Done: report.md, reproduce.sh, stage_status.tsv generated.

Output directory: `{out}`

KNOWN_ABSENT_FOR_THIS_TASK: contrast_definition.tsv

## Final outputs
- /objects/seurat_norm.rds
- /tables/variable_features.tsv
- /tables/preprocess_summary.tsv
- /plots/variable_features.png
- benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-SCRNA-002/vague/objects/seurat_norm.rds
- benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-SCRNA-002/vague/objects/seurat_cluster.rds
- benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-SCRNA-002/vague/tables/markers_per_cluster.tsv
- contrast_definition.tsv
- /public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-SCRNA-001/vague/barcodes.tsv
- /public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-SCRNA-002/strict/tables/CellMarker.csv
- /public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-SCRNA-002/medium/report.md
"""
            state = ensure_plan_state(root, "task_scrna", plan_text=plan)
            while any(step["status"] == "pending" for step in state["steps"]):
                mark_first_pending_running(state, "started")
                mark_plan_step_done(state, "done")
            save_plan_state(root, "task_scrna", state)

            result = CompletionGate.validate_final(root, "task_scrna", load_plan_state(root, "task_scrna"), plan)

        self.assertTrue(result.passed, result.feedback)
        self.assertNotIn("contrast_definition.tsv", result.checked_artifacts)
        self.assertNotIn("/objects/seurat_norm.rds", result.checked_artifacts)
        self.assertFalse(any("BM-REMAIN-SCRNA-001/vague" in item for item in result.checked_artifacts))
        self.assertFalse(any("BM-REMAIN-SCRNA-002/strict" in item for item in result.checked_artifacts))
        self.assertFalse(any("BM-REMAIN-SCRNA-002/medium" in item for item in result.checked_artifacts))

    def test_final_gate_rejects_status_claiming_missing_report_outputs(self):
        plan = """
- [x] Step 1: Write final status and report outputs | Goal: Generate final report, reproduce.sh, and stage_status.tsv | Action: EXECUTE | Done: final files exist

Output directory: runs/remaining/BM-REMAIN-SCRNA-002/medium
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out = root / "runs" / "remaining" / "BM-REMAIN-SCRNA-002" / "medium"
            out.mkdir(parents=True)
            (out / "stage_status.tsv").write_text(
                "stage\tstatus\treason\n"
                "scrna_report\tcompleted\tFinal report, stage_status.tsv, and reproduce.sh generated\n",
                encoding="utf-8",
            )
            state = ensure_plan_state(root, "task_scrna", plan_text=plan)
            mark_first_pending_running(state, "started")
            mark_plan_step_done(state, "status table written")
            save_plan_state(root, "task_scrna", state)

            result = CompletionGate.validate_final(root, "task_scrna", load_plan_state(root, "task_scrna"), plan)

        self.assertFalse(result.passed)
        self.assertIn("report.md", result.feedback)
        self.assertIn("reproduce.sh", result.feedback)

    def test_step_gate_does_not_scan_sibling_status_files_from_broad_root(self):
        plan = """
- [ ] Step 1: Write final status and report outputs | Goal: Generate final report, reproduce.sh, and stage_status.tsv | Action: EXECUTE | Done: final files exist
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current = root / "runs" / "remaining" / "BM-REMAIN-SCRNA-002" / "vague"
            sibling = root / "runs" / "remaining" / "BM-REMAIN-SCRNA-002" / "medium"
            current.mkdir(parents=True)
            sibling.mkdir(parents=True)
            for name in ("report.md", "reproduce.sh", "stage_status.tsv"):
                (current / name).write_text("completed\n", encoding="utf-8")
            (current / "stage_status.tsv").write_text(
                "stage\tstatus\treason\n"
                "scrna_report\tcompleted\treport.md, reproduce.sh, stage_status.tsv generated\n",
                encoding="utf-8",
            )
            (sibling / "stage_status.tsv").write_text(
                "stage\tstatus\treason\n"
                "scrna_report\tcompleted\treport.md and reproduce.sh generated\n",
                encoding="utf-8",
            )
            script = root / "task_outputs" / "task_scrna" / "step_1.sh"
            script.parent.mkdir(parents=True)
            script.write_text(f"ls -lh {current / 'report.md'} {current / 'reproduce.sh'} {current / 'stage_status.tsv'}\n", encoding="utf-8")
            state = ensure_plan_state(root, "task_scrna", plan_text=plan)
            mark_first_pending_running(state, "started")
            record_current_step_evidence(
                state,
                "execute_success",
                f"{script}\nSTDOUT:\n-rw report.md\n-rw reproduce.sh\n-rw stage_status.tsv\n",
            )
            save_plan_state(root, "task_scrna", state)

            result = CompletionGate.validate_current_step(root, "task_scrna", load_plan_state(root, "task_scrna"))

        self.assertTrue(result.passed, result.feedback)
        self.assertFalse(any("medium" in item for item in result.checked_artifacts))

    def test_solution_branch_uses_final_gate_before_finishing(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _prepare_final_solution_progress", source)
        self.assertIn("CompletionGate.validate_final", source)
        self.assertIn("can_finish, plan_progress, final_status, gate_feedback", source)
        self.assertIn('"next_step": "generate"', source)
        self.assertNotIn('while mark_plan_step_done(final_state, evidence="final solution accepted by completion gate")', source)


if __name__ == "__main__":
    unittest.main()

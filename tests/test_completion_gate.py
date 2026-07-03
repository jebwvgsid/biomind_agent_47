import tempfile
import unittest
from pathlib import Path

from agent_core.completion_gate import CompletionGate, _candidate_output_roots_from_text
from agent_core.plan_tracker import (
    ensure_plan_state,
    load_plan_state,
    mark_first_pending_running,
    mark_plan_step_done,
    record_current_step_evidence,
    save_plan_state,
)


class CompletionGateTests(unittest.TestCase):
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

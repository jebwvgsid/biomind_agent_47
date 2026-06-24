import tempfile
import unittest
from pathlib import Path

from agent_core.completion_gate import CompletionGate
from agent_core.plan_tracker import (
    ensure_plan_state,
    load_plan_state,
    mark_first_pending_running,
    mark_plan_step_done,
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

class FinalSolutionGateTests(unittest.TestCase):
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

    def test_solution_branch_uses_final_gate_before_finishing(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _prepare_final_solution_progress", source)
        self.assertIn("CompletionGate.validate_final", source)
        self.assertIn("can_finish, plan_progress, final_status, gate_feedback", source)
        self.assertIn('"next_step": "generate"', source)


if __name__ == "__main__":
    unittest.main()

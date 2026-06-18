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


TAD_STRICT_CONTEXT = """
请使用 BioMind 的 3d-dna/tad-calling skill 对 leaf 和 bud 两个 mcool 文件做 TAD calling。
允许：
- 允许使用 call_tads.py wrapper 默认 auto-balance 行为。

禁止：
- 禁止跳过 resolution 和 balance preflight。
- 禁止直接调用 hicFindTADs 而不检查 wrapper 输出。
- 禁止使用 toy TAD domains/boundaries。
"""


class CompletionGateTests(unittest.TestCase):
    def test_tad_wrapper_allowed_when_direct_hicfindtads_is_forbidden(self):
        code = (
            "python Biomind_v1/skills/3d-dna/tad-calling/scripts/call_tads.py "
            "--sample leaf leaf.mcool --sample bud bud.mcool --resolution-bp 40000"
        )

        result = CompletionGate.validate_code_boundary(code, TAD_STRICT_CONTEXT)

        self.assertTrue(result.passed, result.message)

    def test_direct_hicfindtads_blocked_when_direct_tool_is_forbidden(self):
        code = "hicFindTADs --matrix leaf.mcool::/resolutions/40000 --outPrefix leaf"

        result = CompletionGate.validate_code_boundary(code, TAD_STRICT_CONTEXT)

        self.assertFalse(result.passed)
        self.assertIn("direct hicFindTADs", result.message)

    def test_explicit_tad_calling_ban_still_blocks_wrapper(self):
        context = "本任务只做 preflight。禁止进行 TAD calling，不要调用任何 TAD caller。"
        code = "python Biomind_v1/skills/3d-dna/tad-calling/scripts/call_tads.py --sample leaf leaf.mcool"

        result = CompletionGate.validate_code_boundary(code, context)

        self.assertFalse(result.passed)
        self.assertIn("forbids TAD calling", result.message)

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

    def test_solution_branch_uses_final_gate_before_finishing(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _prepare_final_solution_progress", source)
        self.assertIn("CompletionGate.validate_final", source)
        self.assertIn("can_finish, plan_progress, final_status, gate_feedback", source)
        self.assertIn('"next_step": "generate"', source)


if __name__ == "__main__":
    unittest.main()

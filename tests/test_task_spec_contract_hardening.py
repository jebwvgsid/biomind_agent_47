import tempfile
import unittest
from pathlib import Path

from agent_core.plan_tracker import build_plan_state, save_plan_state
from agent_core.task_spec import (
    classify_path_mentions,
    create_task_spec,
    ensure_task_spec,
    extract_request_contracts,
    format_effective_user_facts_context,
)
from bio_agents import BioAnalystAgent
from execution.profiling import RunProfiler


class TaskSpecContractHardeningTests(unittest.TestCase):
    def test_required_output_is_extracted_outside_expected_outputs_section(self):
        contracts = extract_request_contracts("交付文件包括 annotated.vcf、report.md 和 reproduce.sh。")

        self.assertEqual(
            [item["filename"] for item in contracts["output_contracts"]],
            ["annotated.vcf", "report.md", "reproduce.sh"],
        )

    def test_forbidden_skill_and_control_tokens_never_become_active_inputs(self):
        request = """
        Inputs:
        - benchmarks/data/all.filtered.snp.vcf
        - benchmarks/data/genome.fasta
        - benchmarks/data/genome.gtf
        Skill: genomics/snpeff-annotation
        禁止使用 tests/demo_ref.fa
        不确定时 ask_user/blocked_need_user_decision
        不要读取 benchmark/hidden/fixture。输出目录：benchmarks/output/strict
        """

        contracts = extract_request_contracts(request)
        mentions = classify_path_mentions(request)

        self.assertEqual(
            [Path(item["path"]).name for item in contracts["inputs"]],
            ["all.filtered.snp.vcf", "genome.fasta", "genome.gtf"],
        )
        classes = {item["requested_path"]: item["classification"] for item in mentions}
        self.assertEqual(classes["tests/demo_ref.fa"], "forbidden")
        self.assertEqual(classes["genomics/snpeff-annotation"], "skill_id")
        self.assertEqual(classes["ask_user/blocked_need_user_decision"], "control_token")
        self.assertEqual(classes["benchmark/hidden/fixture"], "control_token")
        self.assertEqual(classes["benchmarks/output/strict"], "output")

        spec = create_task_spec(request)
        forbidden = [
            item for item in spec["artifact_bindings"]
            if item.get("classification") == "forbidden"
        ]
        self.assertEqual(len(forbidden), 1)
        self.assertTrue(all(not item["required"] and item["status"] == "withdrawn" for item in forbidden))

    def test_unique_existing_same_role_path_resolves_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            formal = root / "benchmarks" / "data" / "genome.fasta"
            formal.parent.mkdir(parents=True)
            formal.write_text(">chr1\nACGT\n", encoding="utf-8")
            spec = ensure_task_spec({
                "goal": "use reference",
                "inputs": [
                    {"role": "reference_fasta", "path": "data/genome.fasta", "source": "user_explicit"},
                    {"role": "reference_fasta", "path": str(formal), "source": "user_explicit"},
                ],
            }, project_root=root)

        self.assertEqual(len(spec["inputs"]), 1)
        self.assertEqual(spec["inputs"][0]["path"], str(formal))
        self.assertEqual(spec["input_alias_audit"][0]["status"], "INPUT_ALIAS_RESOLVED")

    def test_effective_context_budget_includes_headers_and_source_refs(self):
        spec = create_task_spec(
            "Use benchmarks/data/input.vcf and output report.md. " + "constraint text " * 300,
            task_id="budget-task",
        )

        rendered = format_effective_user_facts_context(spec, audience="planner", max_chars=900)

        self.assertLessEqual(len(rendered), 900)
        self.assertIn("[EFFECTIVE USER FACTS]", rendered)
        self.assertIn("[USER SOURCE REFERENCES]", rendered)

    def test_final_metrics_artifacts_come_from_structured_contracts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outdir = root / "outputs"
            outdir.mkdir()
            report = outdir / "report.md"
            report.write_text("complete\n", encoding="utf-8")
            task_spec = ensure_task_spec({
                "goal": "use genomics/snpeff-annotation and input/data.vcf",
                "output_contracts": [
                    {"role": "output_dir", "path": str(outdir), "required": True},
                    {"filename": "report.md", "required": True, "source_type": "system_required"},
                ],
            })
            plan = build_plan_state(
                "metrics-task",
                "- [ ] Step 1: Report | Action: EXECUTE | Skills: [] | Outputs: [report.md] | Done: report exists\n",
            )
            plan["task_spec"] = task_spec
            save_plan_state(root, "metrics-task", plan)
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = str(root)

            artifacts = agent._structured_final_artifacts("metrics-task", {})
            profiler = RunProfiler(root)
            profiler.start_task("metrics-task")
            profiler.record_final_result("metrics-task", status="completed", output_artifacts=artifacts)
            snapshot = profiler.snapshot("metrics-task")

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["path"], str(report))
        self.assertEqual(artifacts[0]["producer_type"], "plan_step")
        self.assertTrue(artifacts[0]["exists"])
        self.assertIsInstance(snapshot["final_result"]["output_artifacts"][0], dict)
        serialized = str(snapshot["final_result"]["output_artifacts"])
        self.assertNotIn("snpeff-annotation", serialized)
        self.assertNotIn("input/data.vcf", serialized)


if __name__ == "__main__":
    unittest.main()

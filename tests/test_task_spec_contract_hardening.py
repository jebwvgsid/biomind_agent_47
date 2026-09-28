import tempfile
import unittest
from pathlib import Path

from agent_core.plan_tracker import build_plan_state, save_plan_state
from agent_core.task_spec import (
    artifact_locator,
    classify_path_mentions,
    create_task_spec,
    ensure_task_spec,
    extract_request_contracts,
    format_effective_user_facts_context,
)
from execution.profiling import RunProfiler


class TaskSpecContractHardeningTests(unittest.TestCase):
    def test_required_output_is_extracted_outside_expected_outputs_section(self):
        contracts = extract_request_contracts("交付文件包括 annotated.vcf、report.md 和 reproduce.sh。")

        self.assertEqual(
            [item["required_basename"] for item in contracts["output_contracts"]],
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
            [Path(artifact_locator(item)).name for item in contracts["inputs"]],
            ["all.filtered.snp.vcf", "genome.fasta", "genome.gtf"],
        )
        classes = {item["requested_path"]: item["classification"] for item in mentions}
        self.assertEqual(classes["tests/demo_ref.fa"], "forbidden")
        self.assertEqual(classes["genomics/snpeff-annotation"], "skill_id")
        self.assertEqual(classes["ask_user/blocked_need_user_decision"], "control_token")
        self.assertEqual(classes["benchmark/hidden/fixture"], "control_token")
        self.assertEqual(classes["benchmarks/output/strict"], "output")

        spec = create_task_spec(request)
        self.assertNotIn(
            "tests/demo_ref.fa",
            {artifact_locator(item) for item in spec["inputs"]},
        )
        self.assertNotIn("artifact_bindings", spec)

    def test_chinese_slash_prose_is_not_classified_as_a_path(self):
        mentions = classify_path_mentions(
            "建议/目标 skills；构建/验证本地 database；不要读取 benchmark/hidden/fixture。"
        )

        paths = {item["requested_path"] for item in mentions}
        self.assertNotIn("/目标", paths)
        self.assertNotIn("/验证本地", paths)
        controls = {
            item["requested_path"]: item["classification"]
            for item in mentions
        }
        self.assertEqual(controls["benchmark/hidden/fixture"], "control_token")

    def test_structured_benchmark_outputs_keep_contract_provenance(self):
        spec = create_task_spec(
            "请输出 annotated.vcf 和 ann_gene_summary.tsv。",
            task_id="BM-REMAIN-ANNOT-007__strict",
            structured_source={
                "question": "请输出 annotated.vcf。",
                "question_source_type": "benchmark_question",
                "question_source_ref": "cases.yaml#strict",
                "contract_source_type": "benchmark_contract",
                "contract_source_ref": "cases.yaml#BM-REMAIN-ANNOT-007",
                "input_files": ["benchmarks/data/input.vcf"],
                "expected_outputs": ["annotated.vcf", "ann_gene_summary.tsv"],
                "output_dir": "benchmarks/output/strict",
            },
        )

        outputs = {
            item["required_basename"]: item
            for item in spec["output_contracts"]
            if item.get("required_basename")
        }
        gene = outputs["ann_gene_summary.tsv"]
        self.assertTrue(gene["required"])
        self.assertEqual(gene["source_type"], "benchmark_contract")
        self.assertEqual(
            gene["source_ref"],
            "cases.yaml#BM-REMAIN-ANNOT-007#expected_outputs[1]",
        )
        self.assertEqual(spec["inputs"][0]["source"], "benchmark_contract")

    def test_manual_expected_output_remains_user_explicit(self):
        spec = create_task_spec("预期产物：\n- ann_gene_summary.tsv")
        output = next(
            item for item in spec["output_contracts"]
            if item.get("required_basename") == "ann_gene_summary.tsv"
        )

        self.assertEqual(output["source_type"], "user_explicit")

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
        self.assertEqual(spec["inputs"][0]["canonical_path"], str(formal))
        self.assertNotIn("input_alias_audit", spec)

    def test_effective_context_budget_includes_headers_and_source_refs(self):
        spec = create_task_spec(
            "Use benchmarks/data/input.vcf and output report.md. " + "constraint text " * 300,
            task_id="budget-task",
        )

        rendered = format_effective_user_facts_context(spec, audience="planner", max_chars=900)

        self.assertLessEqual(len(rendered), 900)
        self.assertIn("[EFFECTIVE USER FACTS]", rendered)
        self.assertIn("[USER SOURCE REFERENCES]", rendered)



if __name__ == "__main__":
    unittest.main()

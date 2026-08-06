import importlib.util
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bio_agents


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = PROJECT_ROOT / "benchmarks/biomind_agent_benchmark/scripts/run/run_biomind_prompt.py"
spec = importlib.util.spec_from_file_location("biomind_benchmark_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


class BenchmarkContractSourceTests(unittest.TestCase):
    def test_annotation_case_keeps_expected_output_source_reference(self):
        records = runner.load_prompts(runner.DEFAULT_PACK)
        record = next(item for item in records if item.get("prompt_id") == "BM-REMAIN-ANNOT-007__strict")

        source = runner.benchmark_source_context(record)

        self.assertEqual(source["contract_source_type"], "benchmark_contract")
        self.assertIn("remaining_skills_agent_benchmark_cases.yaml", source["contract_source_ref"])
        self.assertIn("ann_gene_summary.tsv", source["expected_outputs"])
        self.assertEqual(source["question_source_type"], "benchmark_question")

    def test_runner_passes_typed_contract_to_agent_go(self):
        records = runner.load_prompts(runner.DEFAULT_PACK)
        record = dict(next(item for item in records if item.get("prompt_id") == "BM-REMAIN-ANNOT-007__strict"))
        record["_runtime_task_id"] = record["prompt_id"]
        calls = []

        class FakeAgent:
            def __init__(self, **_kwargs):
                self.app = SimpleNamespace(get_state=lambda _config: SimpleNamespace(tasks=[]))

            async def go(self, prompt, **kwargs):
                calls.append((prompt, kwargs))

        with patch.object(bio_agents, "BioAnalystAgent", FakeAgent):
            asyncio.run(runner.run_prompt(record, runner.PROJECT_ROOT, runner.BIOMIND_ROOT))

        structured = calls[0][1]["structured_source"]
        self.assertEqual(structured["contract_source_type"], "benchmark_contract")
        self.assertIn("ann_gene_summary.tsv", structured["expected_outputs"])
        self.assertEqual(calls[0][0], structured["question"])
        self.assertNotEqual(calls[0][0], record["copy_paste_prompt"])


if __name__ == "__main__":
    unittest.main()

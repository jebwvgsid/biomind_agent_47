import csv
import json
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILING_PATH = REPO_ROOT / "execution" / "profiling.py"
spec = importlib.util.spec_from_file_location("biomind_profiling_under_test", PROFILING_PATH)
profiling_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(profiling_module)
RunProfiler = profiling_module.RunProfiler
benchmark_metadata_for_task = profiling_module.benchmark_metadata_for_task
generic_metadata_for_prompt = profiling_module.generic_metadata_for_prompt


class RunProfilerTests(unittest.TestCase):
    def test_flush_writes_json_and_markdown_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profiler = RunProfiler(Path(tmpdir))
            task_id = "session_test"
            profiler.start_task(task_id)
            profiler.record_confirmation(
                task_id=task_id,
                prompt_type="code_review",
                response="y",
                duration_seconds=12.5,
                payload={"content": "echo ok", "risk": {"risk_level": "medium", "risk_score": 50}},
            )
            profiler.record_command(
                task_id=task_id,
                language="Bash",
                code="echo ok",
                stdout="ok\n",
                stderr="",
                returncode=0,
                duration_seconds=2.25,
                node="execute",
            )
            profiler.record_llm(
                task_id=task_id,
                node="generate",
                model="test-model",
                duration_seconds=1.0,
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                estimated=False,
            )
            profiler.finish_task(task_id)

            json_path = Path(tmpdir) / task_id / "run_metrics.json"
            md_path = Path(tmpdir) / task_id / "run_metrics.md"
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())

            data = json.loads(json_path.read_text(encoding="utf-8"))
            summary = data["summary"]
            self.assertEqual(summary["manual_confirmations"], 1)
            self.assertEqual(summary["commands"], 1)
            self.assertEqual(summary["successful_commands"], 1)
            self.assertEqual(summary["tokens"]["total_tokens"], 15)
            self.assertAlmostEqual(summary["human_confirmation_wait_seconds"], 12.5)
            self.assertAlmostEqual(summary["command_execution_seconds"], 2.25)
            self.assertIn("Run Metrics: session_test", md_path.read_text(encoding="utf-8"))

    def test_audit_metadata_skills_retries_final_result_and_tsv_upsert(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profiler = RunProfiler(Path(tmpdir))
            task_id = "BM-UNIT-001"
            profiler.start_task(task_id)
            profiler.record_task_metadata(
                task_id,
                test_content="Unit benchmark",
                test_prompt="Run the unit task",
                test_data=["data/a.tsv", "data/b.tsv"],
                data_size_bytes=123,
            )
            profiler.record_skill_selection(
                task_id,
                selected_skills=["scrna/qc/filter"],
                active_skills=["scrna/qc/filter"],
                future_skills=["scrna/preprocess/basic"],
                selected_skills_raw=["qc-filter"],
                source="planner_active_skills",
                workflow_id="scrna_standard",
                node="planner",
            )
            profiler.record_skill_usage(
                task_id,
                ["scrna/qc/filter"],
                usage_type="loaded_for_generate",
                node="generate",
            )
            profiler.record_skill_usage(
                task_id,
                ["scrna/qc/filter"],
                usage_type="executed",
                node="execute",
                detail="step_1.sh",
            )
            profiler.record_supervisor_verdict(task_id, "RETRY", retry_count=1, node="supervisor")
            profiler.record_supervisor_verdict(task_id, "PASS", retry_count=0, node="supervisor")
            profiler.record_final_result(
                task_id,
                status="completed",
                output_result="Selected answer A",
                output_artifacts=["results/out.tsv"],
                node="finalize",
            )
            profiler.finish_task(task_id)

            json_path = Path(tmpdir) / task_id / "run_metrics.json"
            data = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual(data["metadata"]["test_content"], "Unit benchmark")
            self.assertEqual(data["metadata"]["data_size_bytes"], 123)
            self.assertEqual(data["skills"]["selected"], ["scrna/qc/filter", "scrna/preprocess/basic"])
            self.assertEqual(data["skills"]["used"], ["scrna/qc/filter"])
            self.assertEqual(data["summary"]["correction_count"], 1)
            self.assertEqual(data["final_result"]["output_result"], "Selected answer A")

            tsv_path = Path(tmpdir) / "run_records.tsv"
            rows = list(csv.DictReader(tsv_path.open("r", encoding="utf-8", newline=""), delimiter="\t"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["测试任务id"], task_id)
            self.assertEqual(rows[0]["测试内容"], "Unit benchmark")
            self.assertEqual(rows[0]["数据大小"], "123")
            self.assertEqual(rows[0]["选择skills"], "scrna/qc/filter; scrna/preprocess/basic")
            self.assertEqual(rows[0]["使用skills"], "scrna/qc/filter")
            self.assertEqual(rows[0]["纠错次数"], "1")
            self.assertEqual(rows[0]["输出结果"], "Selected answer A")

            profiler.record_final_result(task_id, status="completed", output_result="Updated answer")
            profiler.flush(task_id)
            rows = list(csv.DictReader(tsv_path.open("r", encoding="utf-8", newline=""), delimiter="\t"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["输出结果"], "Updated answer")

    def test_benchmark_metadata_for_task_reads_prompt_data_and_size(self):
        metadata = benchmark_metadata_for_task("BM-SCRNA-PBMC-QC-001", repo_root=REPO_ROOT)

        self.assertEqual(metadata["test_task_id"], "BM-SCRNA-PBMC-QC-001")
        self.assertIn("PBMC3k", metadata["test_content"])
        self.assertIn("pbmc3k_10x_subset/matrix.mtx", metadata["test_prompt"])
        self.assertIn(
            "benchmarks/biomind_agent_benchmark/fixtures_public/pbmc3k_10x_subset/matrix.mtx",
            metadata["test_data"],
        )
        self.assertGreater(metadata["data_size_bytes"], 0)
        self.assertIn("scrna/qc/filter", metadata["benchmark"]["expected_skills"])

    def test_generic_metadata_for_prompt_records_non_benchmark_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_path = root / "inputs" / "counts.tsv"
            data_path.parent.mkdir()
            data_path.write_text("gene\tcell\nA\t1\n", encoding="utf-8")

            prompt = f"请分析这个普通任务的数据文件 {data_path} 并输出QC摘要。"
            metadata = generic_metadata_for_prompt("ordinary_task_001", prompt, repo_root=root)

            self.assertEqual(metadata["test_task_id"], "ordinary_task_001")
            self.assertEqual(metadata["test_prompt"], prompt)
            self.assertIn(str(data_path), metadata["test_data"])
            self.assertEqual(metadata["data_size_bytes"], data_path.stat().st_size)
            self.assertIn("普通任务", metadata["test_content"])


if __name__ == "__main__":
    unittest.main()

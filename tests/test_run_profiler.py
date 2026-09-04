import csv
import json
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILING_PATH = REPO_ROOT / "execution" / "profiling.py"
spec = importlib.util.spec_from_file_location("biomind_profiling_under_test", PROFILING_PATH)
profiling_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(profiling_module)
RunProfiler = profiling_module.RunProfiler
ProfilingCallbackHandler = profiling_module.ProfilingCallbackHandler
benchmark_metadata_for_task = profiling_module.benchmark_metadata_for_task
generic_metadata_for_prompt = profiling_module.generic_metadata_for_prompt
normalize_usage = profiling_module._normalize_usage


class RunProfilerTests(unittest.TestCase):
    def test_typed_length_error_preserves_provider_usage_and_finish_reason(self):
        class LengthFinishReasonError(RuntimeError):
            def __init__(self, completion):
                super().__init__("length limit reached")
                self.completion = completion

        with tempfile.TemporaryDirectory() as tmpdir:
            profiler = RunProfiler(Path(tmpdir))
            handler = ProfilingCallbackHandler(profiler)
            task_id = "session_length_usage"
            completion = SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=25_162,
                    completion_tokens=8_192,
                    total_tokens=33_354,
                    prompt_cache_hit_tokens=8_000,
                    prompt_cache_miss_tokens=17_162,
                    model_dump=lambda: {
                        "prompt_tokens": 25_162,
                        "completion_tokens": 8_192,
                        "total_tokens": 33_354,
                        "prompt_cache_hit_tokens": 8_000,
                        "prompt_cache_miss_tokens": 17_162,
                    },
                ),
                choices=[SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content='{"partial":'),
                )],
            )

            with profiler.task_context(task_id):
                handler.on_chat_model_start(
                    {"name": "deepseek-test"},
                    [[SimpleNamespace(content="input")]],
                    "length-run",
                )
                handler.on_llm_error(
                    LengthFinishReasonError(completion),
                    "length-run",
                )

            event = next(
                item for item in profiler.snapshot(task_id)["events"]
                if item.get("type") == "llm"
            )

        self.assertEqual(event["prompt_tokens"], 25_162)
        self.assertEqual(event["completion_tokens"], 8_192)
        self.assertEqual(event["total_tokens"], 33_354)
        self.assertEqual(event["prompt_cache_hit_tokens"], 8_000)
        self.assertEqual(event["prompt_cache_miss_tokens"], 17_162)
        self.assertFalse(event["tokens_estimated"])
        self.assertEqual(event["finish_reason"], "length")
        self.assertEqual(event["provider_status"], "succeeded")
        self.assertEqual(event["completion_integrity"], "truncated")
        self.assertEqual(event["protocol_status"], "not_validated")
        self.assertEqual(event["usage_source"], "provider_exception_completion")

    def test_success_response_preserves_deepseek_prompt_cache_usage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profiler = RunProfiler(Path(tmpdir))
            handler = ProfilingCallbackHandler(profiler)
            task_id = "session_deepseek_cache_usage"
            response = SimpleNamespace(
                llm_output={
                    "token_usage": {
                        "prompt_tokens": 10_000,
                        "completion_tokens": 100,
                        "total_tokens": 10_100,
                        "reasoning_tokens": 40,
                        "prompt_cache_hit_tokens": 8_000,
                        "prompt_cache_miss_tokens": 2_000,
                    }
                },
                generations=[[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok"),
                        generation_info={"finish_reason": "stop"},
                    )
                ]],
            )

            with profiler.task_context(task_id):
                handler.on_chat_model_start(
                    {"name": "deepseek-test"},
                    [[SimpleNamespace(content="input")]],
                    "cache-run",
                )
                handler.on_llm_end(response, "cache-run")

            snapshot = profiler.snapshot(task_id)
            event = next(
                item for item in snapshot["events"]
                if item.get("type") == "llm"
            )

        self.assertEqual(event["prompt_cache_hit_tokens"], 8_000)
        self.assertEqual(event["prompt_cache_miss_tokens"], 2_000)
        self.assertEqual(event["reasoning_tokens"], 40)
        self.assertEqual(event["cache_metric_status"], "reported")
        self.assertEqual(snapshot["summary"]["tokens"], {
            "prompt_tokens": 10_000,
            "completion_tokens": 100,
            "total_tokens": 10_100,
            "reasoning_tokens": 40,
            "contains_estimates": False,
            "prompt_cache_hit_tokens": 8_000,
            "prompt_cache_miss_tokens": 2_000,
            "prompt_cache_usage_reported_calls": 1,
            "cache_metric_status": "reported",
            "prompt_cache_hit_rate": 0.8,
        })

    def test_common_cached_token_shapes_normalize_to_prompt_cache_usage(self):
        cases = [
            (
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 64},
                },
                64,
            ),
            (
                {
                    "input_tokens": 100,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 48},
                },
                48,
            ),
            (
                {
                    "input_tokens": 100,
                    "output_tokens": 1,
                    "input_token_details": {"cache_read": 32},
                },
                32,
            ),
            (
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 1,
                    "cached_tokens": 16,
                },
                16,
            ),
        ]

        for usage, expected_hit in cases:
            with self.subTest(usage=usage):
                normalized = normalize_usage(usage)
                self.assertEqual(
                    normalized["prompt_cache_hit_tokens"],
                    expected_hit,
                )
                self.assertEqual(
                    normalized["prompt_cache_miss_tokens"],
                    100 - expected_hit,
                )
                self.assertEqual(
                    normalized["cache_metric_status"],
                    "partially_reported_derived",
                )

    def test_reasoning_tokens_and_unreported_cache_remain_lossless(self):
        normalized = normalize_usage({
            "input_tokens": 100,
            "output_tokens": 21,
            "total_tokens": 121,
            "output_token_details": {"reasoning": 13},
        })
        self.assertEqual(normalized["reasoning_tokens"], 13)
        self.assertIsNone(normalized["prompt_cache_hit_tokens"])
        self.assertIsNone(normalized["prompt_cache_miss_tokens"])
        self.assertEqual(normalized["cache_metric_status"], "not_reported")

        with tempfile.TemporaryDirectory() as tmpdir:
            profiler = RunProfiler(Path(tmpdir))
            profiler.start_task("missing-cache")
            profiler.record_llm(
                "missing-cache",
                "recovery",
                0.1,
                prompt_tokens=100,
                completion_tokens=21,
                total_tokens=121,
                reasoning_tokens=13,
                estimated=False,
            )
            tokens = profiler.snapshot("missing-cache")["summary"]["tokens"]
        self.assertEqual(tokens["reasoning_tokens"], 13)
        self.assertIsNone(tokens["prompt_cache_hit_tokens"])
        self.assertIsNone(tokens["prompt_cache_miss_tokens"])
        self.assertIsNone(tokens["prompt_cache_hit_rate"])
        self.assertEqual(tokens["cache_metric_status"], "not_reported")

    def test_split_langchain_usage_and_response_metadata_are_merged(self):
        response = SimpleNamespace(
            generations=[[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="ok",
                        usage_metadata={
                            "input_tokens": 1_000,
                            "output_tokens": 10,
                            "total_tokens": 1_010,
                        },
                        response_metadata={
                            "token_usage": {
                                "prompt_cache_hit_tokens": 750,
                                "prompt_cache_miss_tokens": 250,
                            }
                        },
                        additional_kwargs={},
                    ),
                    generation_info={},
                )
            ]],
            llm_output=None,
        )

        normalized = profiling_module._response_usage(response)

        self.assertEqual(normalized["prompt_tokens"], 1_000)
        self.assertEqual(normalized["completion_tokens"], 10)
        self.assertEqual(normalized["total_tokens"], 1_010)
        self.assertEqual(normalized["prompt_cache_hit_tokens"], 750)
        self.assertEqual(normalized["prompt_cache_miss_tokens"], 250)

    def test_blocked_finish_projects_durable_diagnostic_and_selected_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_id = "session_blocked_projection"
            task_dir = root / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "plan_state.json").write_text(
                json.dumps({
                    "task_state": {
                        "status": "blocked",
                        "selected_skills": [],
                        "consultation_state": {
                            "resolved_selected_skill_ids": ["genomics/snpeff-annotation"],
                            "terminal_diagnostic": {
                                "error_code": "FINALIZE_RESPONSE_TRUNCATED",
                                "error_owner": "controller_provider",
                                "detail": "Provider returned finish_reason=length.",
                                "partial_response_ref": "context_trace/partial.json",
                            },
                        },
                    },
                    "selected_skill_ids": [],
                }),
                encoding="utf-8",
            )
            profiler = RunProfiler(root)
            profiler.start_task(task_id)
            profiler.record_command(
                task_id=task_id,
                language="Bash",
                code="check-runtime",
                stdout="RESOLVED_SNPEFF_JAR=/env/snpEff.jar\n",
                stderr="",
                returncode=0,
                duration_seconds=0.1,
            )

            profiler.finish_task(task_id, "blocked")
            snapshot = profiler.snapshot(task_id)

        self.assertEqual(
            snapshot["skills"]["selected"],
            ["genomics/snpeff-annotation"],
        )
        final_result = snapshot["final_result"]
        self.assertEqual(final_result["status"], "blocked")
        self.assertIn("FINALIZE_RESPONSE_TRUNCATED", final_result["output_result"])
        self.assertNotIn("RESOLVED_SNPEFF_JAR", final_result["output_result"])
        self.assertEqual(
            final_result["terminal_diagnostic"]["error_owner"],
            "controller_provider",
        )
        self.assertEqual(
            final_result["evidence_refs"],
            ["context_trace/partial.json"],
        )

    def test_empty_later_skill_projection_does_not_erase_resolved_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profiler = RunProfiler(Path(tmpdir))
            task_id = "session_skill_projection"
            profiler.start_task(task_id)
            profiler.record_skill_selection(
                task_id,
                selected_skills=["genomics/snpeff-annotation"],
                node="consultant",
            )
            profiler.record_skill_selection(
                task_id,
                selected_skills=[],
                active_skills=[],
                future_skills=[],
                node="terminal_projection",
            )

            snapshot = profiler.snapshot(task_id)

        self.assertEqual(
            snapshot["skills"]["selected"],
            ["genomics/snpeff-annotation"],
        )

    def test_schema_v4_block_derives_truncation_diagnostic_from_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_id = "session_v4_blocked_projection"
            task_dir = root / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "plan_state.json").write_text(
                json.dumps({
                    "task_state": {
                        "status": "blocked",
                        "selected_skill_ids": ["genomics/snpeff-annotation"],
                        "consultation_state": {
                            "finalize_protocol": {
                                "schema_version": 4,
                                "finalize_status": "blocked",
                                "completion_integrity": "truncated",
                                "compile_outcome": {"status": "blocked"},
                                "attempts": [{
                                    "completion_integrity": "truncated",
                                    "finish_reason": "length",
                                    "partial_response_artifact_ref": "context_trace/partial-v4.json",
                                }],
                            },
                        },
                    },
                }),
                encoding="utf-8",
            )
            profiler = RunProfiler(root)
            profiler.start_task(task_id)

            profiler.finish_task(task_id, "blocked")
            snapshot = profiler.snapshot(task_id)

        self.assertIn(
            "FINALIZE_RESPONSE_TRUNCATED",
            snapshot["final_result"]["output_result"],
        )
        self.assertIn(
            "context_trace/partial-v4.json",
            snapshot["final_result"]["evidence_refs"],
        )
        self.assertEqual(
            snapshot["skills"]["selected"],
            ["genomics/snpeff-annotation"],
        )

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

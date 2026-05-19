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


if __name__ == "__main__":
    unittest.main()

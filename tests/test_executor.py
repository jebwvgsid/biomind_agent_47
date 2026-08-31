import asyncio
import os
import shlex
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from langchain_core.messages import HumanMessage  # type: ignore  # noqa: F401
except (ImportError, ModuleNotFoundError):
    langchain_core = types.ModuleType("langchain_core")
    messages_module = types.ModuleType("langchain_core.messages")

    class HumanMessage:
        def __init__(self, content: str = ""):
            self.content = content

    messages_module.HumanMessage = HumanMessage
    sys.modules.setdefault("langchain_core", langchain_core)
    sys.modules["langchain_core.messages"] = messages_module

from execution.executor import (
    BASH_SCRIPT_PROLOGUE,
    get_last_execution_metadata,
    run_bash_script,
    run_command,
    terminate_active_processes,
)
from execution.profiling import RunProfiler


class ExecutorWatchdogTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(os.name == "nt", "POSIX shell watchdog behavior is tested on Linux/macOS only")
    async def test_run_command_terminates_idle_stalled_process(self):
        with patch.dict(
            os.environ,
            {
                "BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS": "0.2",
                "BIOMIND_EXEC_STALL_TIMEOUT_SECONDS": "0.4",
                "BIOMIND_EXEC_WALL_TIMEOUT_SECONDS": "0",
            },
        ):
            stdout, stderr, rc = await run_command("sleep 5", profile=False)

        self.assertEqual(stdout, "")
        self.assertEqual(rc, 124)
        self.assertIn("STALLED", stderr)

    @unittest.skipIf(os.name == "nt", "POSIX shell watchdog behavior is tested on Linux/macOS only")
    async def test_run_command_stops_silent_discovery_probe_before_general_stall_timeout(self):
        with patch.dict(
            os.environ,
            {
                "BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS": "0.05",
                "BIOMIND_EXEC_EARLY_HEALTH_SECONDS": "0.1",
                "BIOMIND_EXEC_SUSPECT_STALL_SECONDS": "0.25",
                "BIOMIND_EXEC_STALL_TIMEOUT_SECONDS": "5",
                "BIOMIND_EXEC_WALL_TIMEOUT_SECONDS": "0",
            },
        ):
            stdout, stderr, rc = await run_command(
                "find /tmp -maxdepth 0 -exec sleep 5 ';'",
                profile=False,
            )

        self.assertEqual(stdout, "")
        self.assertEqual(rc, 124)
        self.assertIn("SUSPECT_STALL", stderr)
        self.assertIn("discovery", stderr)

    @unittest.skipIf(os.name == "nt", "POSIX process-group cancellation is tested on Linux/macOS only")
    async def test_cancelling_run_command_terminates_its_process_group(self):
        task = asyncio.create_task(run_command("sleep 10", profile=False))
        await asyncio.sleep(0.1)

        with self.assertRaises(asyncio.CancelledError):
            task.cancel()
            await task
        self.assertEqual(terminate_active_processes(), 0)

    @unittest.skipIf(os.name == "nt", "POSIX metadata behavior is tested on Linux/macOS only")
    async def test_executor_keeps_raw_argv_metadata_without_changing_result_tuple(self):
        stdout, stderr, rc = await run_command("printf 'ok\n'", profile=False)
        metadata = get_last_execution_metadata()
        self.assertEqual((stdout, stderr, rc), ("ok\n", "", 0))
        self.assertEqual(metadata["argv"], ["printf", "ok\n"])
        self.assertEqual(metadata["returncode"], 0)

    @unittest.skipIf(os.name == "nt", "POSIX stream capture behavior is tested on Linux/macOS only")
    async def test_large_stdout_is_bounded_and_records_true_size(self):
        with patch.dict(os.environ, {"BIOMIND_EXEC_MAX_CAPTURE_BYTES": "16384"}):
            stdout, stderr, rc = await run_command(
                f"{sys.executable} -c \"import sys; sys.stdout.write('A' * 1000000)\"",
                profile=False,
            )

        metadata = get_last_execution_metadata()
        self.assertEqual(rc, 0, stderr)
        self.assertLess(len(stdout), 20000)
        self.assertIn("BIOMIND_OUTPUT_TRUNCATED", stdout)
        self.assertEqual(metadata["stdout_bytes"], 1_000_000)
        self.assertTrue(metadata["stdout_truncated"])
        self.assertEqual(metadata["stderr_bytes"], 0)

    @unittest.skipIf(os.name == "nt", "POSIX PIPE completion behavior is tested on Linux/macOS only")
    async def test_fast_reader_with_large_single_line_does_not_wait_for_watchdog_tick(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "long-line.txt"
            source.write_bytes(b">record\n" + (b"A" * 1_000_000) + b"\n")
            with patch.dict(
                os.environ,
                {
                    "BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS": "30",
                    "BIOMIND_EXEC_MAX_CAPTURE_BYTES": "16384",
                },
            ):
                stdout, stderr, rc = await asyncio.wait_for(
                    run_command(f"head -5 {shlex.quote(str(source))}", profile=False),
                    timeout=2,
                )

        metadata = get_last_execution_metadata()
        self.assertEqual(rc, 0, stderr)
        self.assertIn("BIOMIND_OUTPUT_TRUNCATED", stdout)
        self.assertEqual(metadata["stdout_bytes"], 1_000_009)
        self.assertTrue(metadata["stdout_truncated"])

    @unittest.skipIf(os.name == "nt", "POSIX stream log behavior is tested on Linux/macOS only")
    async def test_large_stdout_keeps_full_durable_log_for_active_task(self):
        with tempfile.TemporaryDirectory() as root:
            profiler = RunProfiler(Path(root) / "task_outputs")
            with profiler.task_context("task-a"):
                with patch.dict(os.environ, {"BIOMIND_EXEC_MAX_CAPTURE_BYTES": "16384"}):
                    stdout, stderr, rc = await run_command(
                        f"{sys.executable} -c \"import sys; sys.stdout.write('B' * 1000000)\"",
                        profile=False,
                    )
            metadata = get_last_execution_metadata()
            full_log = Path(metadata["stdout_log"])

            self.assertEqual(rc, 0, stderr)
            self.assertIn("BIOMIND_OUTPUT_TRUNCATED", stdout)
            self.assertTrue(full_log.is_file())
            self.assertEqual(full_log.stat().st_size, 1_000_000)
            self.assertEqual(full_log.read_bytes()[:4], b"BBBB")

    @unittest.skipIf(os.name == "nt", "POSIX output-limit behavior is tested on Linux/macOS only")
    async def test_unbounded_stdout_is_terminated_at_configured_stream_limit(self):
        with patch.dict(
            os.environ,
            {
                "BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS": "30",
                "BIOMIND_EXEC_MAX_CAPTURE_BYTES": "16384",
                "BIOMIND_EXEC_MAX_STREAM_BYTES": "65536",
            },
        ):
            stdout, stderr, rc = await asyncio.wait_for(
                run_command(
                    f"{sys.executable} -c \"import sys; sys.stdout.write('C' * 1000000)\"",
                    profile=False,
                ),
                timeout=2,
            )

        metadata = get_last_execution_metadata()
        self.assertEqual(rc, 124)
        self.assertIn("OUTPUT_LIMIT", stderr)
        self.assertGreater(metadata["stdout_bytes"], 65_536)
        self.assertLess(metadata["stdout_bytes"], 100_000)

    async def test_default_bash_prologue_does_not_force_nounset_on_environment_hooks(self):
        self.assertIn("set -eo pipefail", BASH_SCRIPT_PROLOGUE)
        self.assertNotIn("set -euo pipefail", BASH_SCRIPT_PROLOGUE)

        stdout, stderr, rc = await run_bash_script(
            "unset ADDR2LINE\nprintf 'ADDR2LINE=%s\\n' \"$ADDR2LINE\""
        )

        self.assertEqual(rc, 0, stderr)
        self.assertEqual(stdout, "ADDR2LINE=\n")


if __name__ == "__main__":
    unittest.main()

import io
import logging
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from bio_agents import BioAnalystAgent, InteractiveRunResult, OutputRouter
from config.logger import InteractiveConsoleFilter


class OutputRouterTests(unittest.TestCase):
    def _viewer_patches(self):
        return (
            patch("bio_agents.sys.stdin.isatty", return_value=True),
            patch("bio_agents.sys.stdout.isatty", return_value=True),
            patch("bio_agents.shutil.get_terminal_size", return_value=type("Size", (), {"columns": 100})()),
            patch("builtins.print"),
            patch("bio_agents.restore_terminal_state"),
        )

    def test_inline_viewer_y_continues_to_followup_approval(self):
        router = OutputRouter()
        patches = self._viewer_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as restore:
            with patch("bio_agents.safe_input", return_value="y") as read:
                handled = router._inline_window_viewer("Plan", "\n".join(str(i) for i in range(20)))

        self.assertTrue(handled)
        read.assert_called_once()
        restore.assert_called()

    def test_inline_viewer_does_not_swallow_ctrl_c(self):
        router = OutputRouter()
        patches = self._viewer_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as restore:
            with patch("bio_agents.safe_input", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    router._inline_window_viewer("Plan", "\n".join(str(i) for i in range(20)))

        restore.assert_called()

    def test_retryable_pause_wrapper_keeps_user_visible_message(self):
        router = OutputRouter()
        content = (
            "<llm_retryable_pause>\n"
            "Planner request timed out. Resume with /resume task-a.\n"
            "</llm_retryable_pause>"
        )

        with patch.object(router, "page_text") as page_text:
            router.print_ai(content)

        page_text.assert_called_once()
        rendered = page_text.call_args.args[1]
        self.assertIn("Planner request timed out", rendered)
        self.assertIn("/resume task-a", rendered)
        self.assertNotIn("llm_retryable_pause", rendered)

    def test_machine_only_validation_block_is_not_printed_as_ai_message(self):
        router = OutputRouter()

        with patch.object(router, "page_text") as page_text:
            router.print_ai(
                '<validation_result status="passed">internal validator details</validation_result>'
            )

        page_text.assert_not_called()

    def test_failed_terminal_result_is_atomic_once_before_next_prompt(self):
        router = OutputRouter()
        result = InteractiveRunResult(
            task_id="task-failed",
            status="failed",
            stage="consultant",
            failure_code="SKILL_DOC_REQUIRED",
            reason="Required skill document was not loaded.",
            result_path="/tmp/task-failed/run_metrics.json",
            trace_path="/tmp/task-failed/context_trace/events.jsonl",
        )
        events = []

        with patch("builtins.print", side_effect=lambda *args, **kwargs: events.append(str(args[0]))):
            self.assertTrue(router.render_terminal_result(result))
            self.assertFalse(router.render_terminal_result(result))
            events.append("PROMPT")

        terminal_blocks = [row for row in events if "Terminal result" in row]
        self.assertEqual(len(terminal_blocks), 1)
        self.assertIn("Status: failed", terminal_blocks[0])
        self.assertIn("Stage: consultant", terminal_blocks[0])
        self.assertIn("Failure code: SKILL_DOC_REQUIRED", terminal_blocks[0])
        self.assertLess(events.index(terminal_blocks[0]), events.index("PROMPT"))

    def test_archive_writer_telemetry_is_file_only_at_interactive_console(self):
        console_filter = InteractiveConsoleFilter()
        background = __import__("logging").LogRecord(
            "memory.session_archive_writer",
            20,
            __file__,
            1,
            "SESSION_ARCHIVE_QUEUE_EVENT",
            (),
            None,
        )
        foreground = __import__("logging").LogRecord(
            "bio_agents",
            20,
            __file__,
            1,
            "foreground",
            (),
            None,
        )

        self.assertFalse(console_filter.filter(background))
        self.assertTrue(console_filter.filter(foreground))

    def test_background_writer_completion_cannot_concatenate_with_prompt(self):
        console = io.StringIO()
        file_log = io.StringIO()
        console_handler = logging.StreamHandler(console)
        console_handler.addFilter(InteractiveConsoleFilter())
        file_handler = logging.StreamHandler(file_log)
        writer_logger = logging.getLogger(
            "memory.session_archive_writer.output_race_test"
        )
        writer_logger.handlers = [console_handler, file_handler]
        writer_logger.propagate = False
        writer_logger.setLevel(logging.INFO)
        barrier = threading.Barrier(2)

        def finish_writer() -> None:
            barrier.wait()
            writer_logger.info("SESSION_ARCHIVE_QUEUE_EVENT completed")

        worker = threading.Thread(target=finish_writer)
        worker.start()
        router = OutputRouter()
        result = InteractiveRunResult(
            task_id="race-task",
            status="failed",
            stage="consultant",
            failure_code="TEST_FAILURE",
            reason="deterministic test",
        )
        with redirect_stdout(console):
            barrier.wait()
            router.render_terminal_result(result)
            print("[race-task] runs>", flush=True)
        worker.join(timeout=2)

        rendered = console.getvalue()
        self.assertNotIn("SESSION_ARCHIVE_QUEUE_EVENT", rendered)
        self.assertEqual(rendered.count("Terminal result"), 1)
        self.assertIn("\n[race-task] runs>\n", rendered)
        self.assertIn("SESSION_ARCHIVE_QUEUE_EVENT completed", file_log.getvalue())

    def test_task_acceptance_precedes_optional_startup_and_parse_is_neutral(self):
        source = (
            Path(__file__).resolve().parents[1] / "bio_agents.py"
        ).read_text(encoding="utf-8")
        go_start = source.index("    async def go(")
        accepted = source.index("● Task accepted:", go_start)
        startup_sync = source.index("self._sync_runtime_registries_at_startup()", go_start)

        self.assertLess(accepted, startup_sync)
        self.assertIn("• Consultant action parsed; validating...", source)
        self.assertNotIn("✓ Consultant JSON action parsed and normalized.", source)

    def test_completed_projection_ignores_historical_rejection(self):
        with TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            snapshot = SimpleNamespace(values={
                "consultant_commit_rejection": {
                    "code": "SKILL_DOC_REQUIRED",
                    "detail": "historical",
                },
                "completion_gate_result": {
                    "stage": "finalize",
                    "reason": "All required outputs verified.",
                },
            })

            result = agent._interactive_run_result(
                "task-completed",
                "completed",
                snapshot,
            )

        self.assertEqual(result.failure_code, "")
        self.assertEqual(result.stage, "finalize")
        self.assertEqual(result.reason, "All required outputs verified.")


if __name__ == "__main__":
    unittest.main()

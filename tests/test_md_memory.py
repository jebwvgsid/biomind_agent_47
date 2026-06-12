import tempfile
import unittest
from pathlib import Path

from memory.session_archive import SessionArchive
from memory.task_summary import TaskSummaryRenderer
from memory.wiki_memory import WikiMemoryManager


class TaskSummaryRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_summary_renders_from_archive_without_pending_or_dream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            archive = SessionArchive(root / "session_archive.sqlite3")
            wiki = WikiMemoryManager(None, root)
            renderer = TaskSummaryRenderer(root, archive, wiki)

            archive.append_event(
                task_id="session_1",
                session_id="session_1",
                project_id="project_alpha",
                stage="planner",
                event_type="plan_created",
                content="Plan: run QC and create task_outputs/session_1/plan.md",
                summary="Planner created QC plan.",
                metadata={"goal": "Run QC", "plan_file": "task_outputs/session_1/plan.md"},
                paths=["task_outputs/session_1/plan.md"],
            )
            archive.append_event(
                task_id="session_1",
                session_id="session_1",
                project_id="project_alpha",
                stage="execute",
                event_type="execution_finished",
                content="Generated tables/qc_summary.tsv",
                summary="QC summary table generated.",
                metadata={"script_file": "task_outputs/session_1/run.sh", "returncode": 0},
                returncode=0,
            )

            path = renderer.write_task_summary("session_1")
            text = path.read_text(encoding="utf-8")

            self.assertIn("Derived from SessionArchive events", text)
            self.assertIn("Run QC", text)
            self.assertIn("QC summary table generated.", text)
            self.assertIn("task_outputs/session_1/plan.md", text)
            self.assertFalse((root / "history" / "PENDING.md").exists())
            self.assertFalse((root / "dreams" / "DREAM_LOG.md").exists())

    async def test_task_summary_can_be_rebuilt_after_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            archive = SessionArchive(root / "session_archive.sqlite3")
            renderer = TaskSummaryRenderer(root, archive)
            archive.append_event(
                task_id="session_2",
                session_id="session_2",
                stage="finalize",
                event_type="task_finalized",
                content="Task completed.",
                summary="Task completed.",
                metadata={"status": "completed"},
            )

            first = renderer.write_task_summary("session_2")
            first.unlink()
            rebuilt = renderer.read_task_summary("session_2")

            self.assertIn("Task completed.", rebuilt)
            self.assertIn("Status: completed", rebuilt)

    async def test_prompt_context_uses_archive_search_as_related_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "memory"
            archive = SessionArchive(root / "session_archive.sqlite3")
            renderer = TaskSummaryRenderer(root, archive)
            archive.append_event(
                task_id="session_3",
                session_id="session_3",
                stage="execute",
                event_type="execution_finished",
                content="Generated beta markers table.",
                summary="Beta markers table generated.",
            )

            context = await renderer.build_prompt_context_async("session_3", "beta markers")

            self.assertEqual(context["long_term_memory"], "")
            self.assertIn("Beta markers table generated.", context["task_memory"])
            self.assertIn("Retrieved Related Session Events", context["related_memory"])


if __name__ == "__main__":
    unittest.main()

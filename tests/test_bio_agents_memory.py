import tempfile
import unittest
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from agent_core.plan_tracker import (
    TASK_STATUS_CANCELLED,
    build_plan_state,
    load_plan_state,
    save_plan_state,
    task_status,
)
from agent_core.task_spec import create_task_spec, merge_task_spec
from memory.session_archive import SessionArchive
from memory.task_semantics import infer_task_semantics


class BioAgentMemoryTests(unittest.TestCase):






    def test_loop_calling_task_semantics_are_inferred(self):
        semantics = infer_task_semantics(
            "session_loop",
            messages=["对 leaf 和 bud 进行单样本 loop 鉴定，必须检查 balance weight"],
        )

        self.assertEqual(semantics["task_type"], "chromatin_contact_loop")
        self.assertEqual(semantics["analysis_type"], "loop_calling")

    def test_resume_archive_events_are_selected_and_formatted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            task_id = "resume_task"
            archive = SessionArchive(Path(tmpdir) / "session_archive.sqlite3")
            archive.append_event(
                task_id=task_id,
                session_id=task_id,
                stage="execute",
                event_type="execution_finished",
                content="Generated tables/qc_summary.tsv",
                summary="QC summary table generated.",
                returncode=0,
            )
            archive.append_event(
                task_id=task_id,
                session_id=task_id,
                stage="planner",
                event_type="plan_created",
                content="Plan: continue with marker detection after QC.",
                summary="Planner created marker-detection follow-up.",
            )

            hits = archive.resume_events(task_id)
            formatted = archive.format_hits(hits, title="Retrieved Session Archive for Resume")

        self.assertIn("Retrieved Session Archive for Resume", formatted)
        self.assertIn("QC summary table generated.", formatted)
        self.assertIn("Planner created marker-detection follow-up.", formatted)

    def test_session_archive_dedupes_externalizes_and_tracks_wiki_pending(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            archive = SessionArchive(Path(tmpdir) / "session_archive.sqlite3", max_inline_chars=40)
            kwargs = dict(
                task_id="wiki_task",
                session_id="wiki_task",
                project_id="project_alpha",
                stage="execute",
                event_type="execution_finished",
                content="Generated output.",
                summary="Generated output.",
                metadata={"stdout": "A" * 120, "values": list(range(200))},
                task_semantics={"task_type": "general", "output_artifacts": ["out.tsv"]},
            )

            first = archive.append_event(**kwargs)
            second = archive.append_event(**kwargs)
            pending = archive.pending_for_wiki("project_alpha")

            self.assertEqual(first, second)
            self.assertEqual([event.id for event in pending], [first])
            self.assertEqual(archive.pending_for_wiki_count("project_alpha", task_id="wiki_task"), 1)
            self.assertEqual(
                [event.id for event in archive.pending_for_wiki("project_alpha", task_id="wiki_task")],
                [first],
            )
            self.assertEqual(
                [event.id for event in archive.search_pending_for_wiki("Generated output", task_id="wiki_task", project_id="project_alpha")],
                [first],
            )
            self.assertEqual(pending[0].task_semantics["output_artifacts"], ["out.tsv"])
            self.assertEqual(pending[0].metadata["stdout"]["type"], "large_text_ref")
            self.assertEqual(pending[0].metadata["values"]["type"], "large_json_ref")
            self.assertTrue(Path(pending[0].metadata["stdout"]["path"]).exists())
            self.assertTrue(Path(pending[0].metadata["values"]["path"]).exists())

            archive.mark_wiki_ingested([first], project_id="project_alpha", summary="done")
            self.assertEqual(archive.pending_for_wiki("project_alpha"), [])
            self.assertEqual(archive.search_pending_for_wiki("Generated output", task_id="wiki_task", project_id="project_alpha"), [])












    def test_prompts_distinguish_install_tool_from_runner(self):
        templates = (Path(__file__).resolve().parents[1] / "prompts" / "templates.py").read_text(encoding="utf-8")

        self.assertIn("INSTALL_TOOL IS NOT A RUNNER", templates)
        self.assertIn("not an environment name, not an interpreter", templates)
        self.assertIn("Planner must not describe `install_tool` as the command used to run analysis scripts", templates)
        self.assertIn("Never write commands like `install_tool_name <<EOF`", templates)





    def test_generate_stable_prompt_is_execution_node_scoped(self):
        templates = (Path(__file__).resolve().parents[1] / "prompts" / "templates.py").read_text(encoding="utf-8")

        self.assertIn("Generate is step-local by default", templates)
        self.assertIn("Follow the first running or pending item", templates)
        self.assertIn("You are the **Generate node**", templates)
        self.assertIn("produce exactly one step-local action", templates)






















































if __name__ == "__main__":
    unittest.main()

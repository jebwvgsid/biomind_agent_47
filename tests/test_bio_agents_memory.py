import tempfile
import unittest
from pathlib import Path

from memory.session_archive import SessionArchive


class BioAgentMemoryTests(unittest.TestCase):
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

    def test_go_resume_uses_session_archive_context(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _archive_resume_context", source)
        self.assertIn("def _build_resume_prompt", source)
        self.assertIn("archive_resume = self._archive_resume_context(task_id)", source)
        self.assertIn("prompt = self._build_resume_prompt(task_id, prompt)", source)

    def test_consultant_wiki_context_is_defined_before_loop_guard(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        wiki_index = source.index("consultant_wiki_context = self._wiki_memory_stage_context")
        guard_index = source.index("if self.skill_manager and (len(load_actions) >= 6")

        self.assertLess(wiki_index, guard_index)

    def test_generate_prunes_internal_consultant_load_artifacts(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _generate_visible_messages", source)
        self.assertIn("LOAD_WORKFLOW_TEMPLATE", source)
        self.assertIn("LOAD_SKILL_DOC", source)
        self.assertIn("<loaded_skill_doc", source)
        self.assertIn("visible_messages = self._generate_visible_messages(messages)", source)
        self.assertIn("] + visible_messages", source)

    def test_generate_prefers_active_skills_and_can_activate_batches(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('selected = state.get("active_skills", []) or state.get("selected_skills", [])', source)
        self.assertIn("activate_match = re.search", source)
        self.assertIn("self._activate_workflow_batch(state", source)
        self.assertIn("plan_active_skills = self.skill_manager.extract_active_skills_from_plan(plan_content)", source)
        self.assertIn('skill_selection_source = "planner_active_skills"', source)

    def test_plan_tracker_is_persisted_and_reinjected_after_compression(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("build_plan_state(task_id, plan_content", source)
        self.assertIn("save_plan_state(self.data_path, task_id, plan_state)", source)
        self.assertIn("update_plan_markdown_status(plan_file, plan_state)", source)
        self.assertIn("messages, compressed = await self.memory_manager.maybe_compress(messages, task_id)", source)
        self.assertIn("plan_progress = self._start_or_load_plan_progress", source)
        self.assertIn("plan_progress=plan_progress", source)
        self.assertIn("reset_plan_state(self.data_path, task_id)", source)

    def test_memory_status_reads_checkpoint_messages(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _messages_for_memory_status", source)
        self.assertIn("snapshot = self.app.get_state(config)", source)
        self.assertIn('values.get("messages", [])', source)
        self.assertIn("self.memory_manager.print_status(", source)

    def test_skill_runtime_guard_blocks_bare_managed_cli(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("managed_tools.add(\"cooler\")", source)
        self.assertIn("\"hicDifferentialTAD\"", source)
        self.assertIn("calls bare `{tool}`", source)
        self.assertIn("Do not ask for code review", source)

    def test_generate_does_not_reset_supervisor_retry_count(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertNotIn('"next_step": next_step, "retry_count": 0', source)
        self.assertIn('"next_step": next_step}', source)

    def test_execute_success_waits_for_supervisor_before_completing_plan_step(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('"execute_success"', source)
        self.assertNotIn('self._mark_plan_current_done(task_id, f"execute success:', source)
        self.assertIn('verdict in {"PASS", "WARN"}', source)

    def test_future_workflow_skill_guard_is_installed(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _workflow_future_skill_violation", source)
        self.assertIn("future_skill_guard", source)
        self.assertIn("future-stage skill", source)

    def test_ctrl_c_exits_interactive_loop(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("Interrupted. Exiting interactive mode.", source)
        self.assertIn('self.profiler.finish_task(current_session_id, "interrupted")\n                break', source)

    def test_consultant_accepts_structured_handoff_without_finish_prefix(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("looks_like_structured_handoff", source)
        self.assertIn("\"[WORKFLOW_DECISION]\" in content", source)
        self.assertIn("\"[SELECTED_SKILLS:\" in content", source)
        self.assertIn("else content.strip()", source)


if __name__ == "__main__":
    unittest.main()

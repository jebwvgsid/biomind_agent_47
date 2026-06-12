import tempfile
import unittest
from pathlib import Path

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

    def test_agent_memory_runtime_uses_archive_and_task_summary_renderer(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("TaskSummaryRenderer", source)
        self.assertIn("BIOMIND_MEMORY_DIR", source)
        self.assertIn("def _resolve_runtime_memory_dir", source)
        self.assertIn("async def _observe_session_memory", source)
        self.assertIn("self.session_archive.append_event", source)
        self.assertIn("ingest_from_archive", source)
        self.assertIn("/task summarize [id]", source)
        self.assertNotIn("MarkdownMemoryManager", source)
        self.assertNotIn("append_raw_event", source)
        self.assertNotIn("/dream", source)

    def test_consultant_memory_packet_is_reused_downstream(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("async def _consultant_memory_retrieval_context", source)
        self.assertIn("def _consultant_handoff_memory_packet", source)
        self.assertIn('"memory_packet_context": self._consultant_handoff_memory_packet', source)
        self.assertIn('planner_wiki_context = state.get("memory_packet_context")', source)
        self.assertIn('memory_packet_context = state.get("memory_packet_context", "") or ""', source)
        self.assertIn("memory_packet_context=memory_packet_context", source)
        self.assertIn('elif not memory_packet_context.strip() and state.get("wiki_memory_context")', source)
        self.assertIn('"wiki_digest": ""', source)

    def test_planner_uses_packet_and_generate_uses_on_demand_retrieval(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _pending_archive_buffer_context", source)
        self.assertIn("def _artifact_index_context", source)
        self.assertIn("async def _generate_on_demand_memory_contexts", source)
        self.assertIn("search_pending_for_wiki", source)
        self.assertIn("planner_memory_parts = [", source)
        self.assertIn("recovery_trigger = (", source)
        self.assertIn('supervisor_verdict in {"RETRY", "WARN"}', source)
        self.assertIn("missing_input = self._looks_like_missing_input_context", source)
        self.assertIn("evidence_query = self._looks_like_evidence_query", source)
        self.assertIn("artifact_context = self._artifact_index_context", source)
        self.assertIn("on_demand_memory = await self._generate_on_demand_memory_contexts", source)
        self.assertNotIn("archive_context = self._archive_prompt_context(task_id, md_query", source)

    def test_wiki_ingest_modes_and_task_scoped_command_are_wired(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("BIOMIND_WIKI_INGEST_MODE", source)
        self.assertIn("self.wiki_ingest_mode", source)
        self.assertIn("async def _maybe_offer_wiki_ingest", source)
        self.assertIn("async def _maybe_auto_wiki_ingest_after_finalize", source)
        self.assertIn('/wiki ingest task', source)
        self.assertIn("task_id=ingest_task_id", source)

    def test_consultant_unrecognized_output_retries_instead_of_human_input_end(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("Consultant response did not use any supported command", source)
        self.assertIn('f"consultant_unrecognized:{len(validation_errors)}"', source)
        self.assertIn('"next_step": "consultant"', source)
        self.assertNotIn('return {"messages": [response], "next_step": "human_input"}', source)

    def test_interactive_loop_does_not_complete_waiting_human_input(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _snapshot_next_step", source)
        self.assertIn("def _terminal_status_from_snapshot", source)
        self.assertIn("terminal_status = self._terminal_status_from_snapshot(current_session_id, snapshot)", source)
        self.assertIn('if terminal_status == "waiting_user":', source)
        self.assertIn('self.profiler.finish_task(current_session_id, "waiting_user")', source)
        waiting_guard = source.index('if terminal_status == "waiting_user":')
        terminal_finish = source.index("self.profiler.finish_task(current_session_id, terminal_status)")
        wiki_guard = source.index('if terminal_status == "completed":', terminal_finish)
        wiki_offer = source.index("await self._maybe_offer_wiki_ingest", wiki_guard)
        self.assertLess(waiting_guard, terminal_finish)
        self.assertLess(wiki_guard, wiki_offer)

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

    def test_execute_includes_post_run_validation(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _validate_execution_result", source)
        self.assertIn("<validation_result level=", source)
        self.assertIn("validation_failed", source)

    def test_execute_observation_truncation_preserves_head_and_tail(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _clip_observation_text", source)
        self.assertIn("...[truncated {omitted} chars]...", source)
        self.assertIn("stdout_obs = self._clip_observation_text(stdout, 2000)", source)
        self.assertIn("stderr_obs = self._clip_observation_text(stderr, 1000)", source)
        self.assertNotIn("STDOUT:\\n{stdout[:2000]}", source)
        self.assertNotIn("STDERR:\\n{stderr[:1000]}", source)

    def test_future_workflow_skill_guard_is_installed(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _workflow_future_skill_violation", source)
        self.assertIn("future_skill_guard", source)
        self.assertIn("future-stage skill", source)

    def test_rrblup_runtime_missing_redirects_to_install_before_review(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _infer_runtime_skills_from_code", source)
        self.assertIn("crop-breeding/crop-gs-rrblup-gblup", source)
        self.assertIn("run_rrblup_package.r", source)
        self.assertIn("def _runtime_install_redirect_from_execution", source)
        self.assertIn("runtime_missing_after_execute", source)

        runtime_check = source.index("runtime_ready, runtime_redirect = self._ensure_skill_runtime_ready(runtime_skills_state")
        code_review = source.index("action, final_code = self.middleware.review_code(code, language)")
        self.assertLess(runtime_check, code_review)

    def test_execute_router_can_route_to_install_subagent(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('if state.get("next_step") == "install_subagent":', source)
        self.assertIn('return "install_subagent"', source)
        self.assertIn('workflow.add_conditional_edges("execute", execute_router', source)
        self.assertIn('["generate", "install_subagent", "supervisor", END]', source)

    def test_execute_guard_can_route_back_to_generate(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        router_idx = source.index("def execute_router(state):")
        generate_guard_idx = source.index('if state.get("next_step") == "generate":', router_idx)
        supervisor_idx = source.index('return "supervisor"', router_idx)

        self.assertLess(generate_guard_idx, supervisor_idx)
        self.assertIn('return "generate"', source[generate_guard_idx:supervisor_idx])

    def test_install_subagent_failure_routes_to_end(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def install_subagent_router(state):", source)
        self.assertIn('workflow.add_conditional_edges("install_subagent", install_subagent_router', source)
        self.assertNotIn('workflow.add_edge("install_subagent", "generate")', source)
        self.assertIn('"next_step": "generate" if install_success else "finish"', source)
        self.assertIn('"next_step": "finish"', source)

    def test_repeated_installation_loop_detection_routes_to_finish(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        loop_idx = source.index("repeated installation loop detected")
        return_idx = source.index('return {', loop_idx)
        finish_idx = source.index('"next_step": "finish"', return_idx)
        next_generate_idx = source.find('"next_step": "generate"', return_idx, finish_idx)

        self.assertEqual(-1, next_generate_idx)

    def test_install_subagent_prints_final_observation(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        observe_idx = source.index('await self._observe_session_memory(\n                task_id,\n                "install_subagent"', source.index('final_res_lower = str(final_res).lower()'))
        print_idx = source.index('output.print_observation(f"<observation>{final_res}</observation>")', observe_idx)
        return_idx = source.index('return {"messages": [AIMessage(content=f"<observation>{final_res}</observation>")]', print_idx)

        self.assertLess(print_idx, return_idx)

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

    def test_fast_router_is_graph_entrypoint(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("async def triage", source)
        self.assertIn("async def direct_chat", source)
        self.assertIn('workflow.add_edge(START, "triage")', source)
        self.assertIn('workflow.add_conditional_edges("triage"', source)
        self.assertIn("classify_task_mode(self.llm", source)
        self.assertIn('task_mode") or "").strip().lower() in {"readonly", "simple_execute"}', source)

    def test_consultant_handoff_does_not_block_on_inferred_analysis_type(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertNotIn("_skill_selection_intent_violation", source)
        self.assertNotIn("_analysis_type_substitution_violation", source)
        self.assertNotIn("analysis_type_substitution_blocked", source)
        self.assertNotIn("PLANNER_INTENT_CORRECTION", source)
        self.assertNotIn("consultant_semantic_validation", source)
        self.assertNotIn("Selected skills do not match the inferred task semantics", source)

    def test_consultant_loop_guard_asks_instead_of_auto_handoff(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("consultant_loop_guard_blocked", source)
        self.assertIn("auto-select skills or hand off to Planner", source)
        self.assertIn('"resume_step": "consultant"', source)
        self.assertIn('"next_step": "ask_user"', source)
        self.assertNotIn('"skill_selection_source": "consultant_loop_guard"', source)
        self.assertNotIn("using the current skill context and handing to Planner", source)

    def test_consultant_loop_guard_counts_since_latest_human_message(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("recent_consultant_messages = messages[last_human_index + 1:]", source)
        self.assertIn("for m in recent_consultant_messages", source)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
import json
from pathlib import Path

from agent_core.plan_tracker import build_plan_state, save_plan_state
from agent_core.task_spec import create_task_spec, merge_task_spec
from bio_agents import (
    BioAnalystAgent,
    _handle_interrupt,
    _handle_suspend,
    missing_supervisor_retry_fields,
)
from memory.session_archive import SessionArchive
from memory.task_semantics import infer_task_semantics


class BioAgentMemoryTests(unittest.TestCase):
    def test_supervisor_routes_only_committed_recovery_policy_decisions(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")
        router = source[source.index("def supervisor_router(state):"):source.index("def final_audit_router(state):")]

        self.assertIn('committed_recovery_route(state, "supervisor")', router)
        self.assertIn('recovery_route in {"generate", "supervisor", "ask_user"}', router)
        self.assertNotIn('state.get("next_step")', router)

    def test_generate_logs_every_early_return(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")
        wrapper_start = source.index("async def generate(state: AgentState):")
        execute_start = source.index("async def execute_logic", wrapper_start)
        wrapper = source[wrapper_start:execute_start]

        self.assertIn('"event": "generate_enter"', wrapper)
        self.assertIn('"event": "generate_exit"', wrapper)
        self.assertIn("result = await _generate_impl(state)", wrapper)
        self.assertIn('"llm_invoked"', wrapper)
        self.assertIn('"state_changes"', wrapper)

    def test_generate_cannot_silently_end_after_retry(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")
        self.assertIn('"generate_result": "NEEDS_RECOVERY"', source)
        self.assertIn('"next_step": "supervisor"', source)
        router = source[source.index("def generate_router(state):"):source.index("def execute_router(state):")]
        self.assertIn('if step == "supervisor":', router)

    def test_prompt_assembly_failure_is_internal_and_does_not_block_plan_step(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")
        start = source.index('reason = f"PROMPT_ASSEMBLY_FAILURE: {compact_exc}"')
        end = source.index("self.profiler.record_context_budget", start)
        branch = source[start:end]

        self.assertIn('"type": "internal_protocol_error"', branch)
        self.assertIn('"task_status": TASK_STATUS_FAILED', branch)
        self.assertIn('"failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value', branch)
        self.assertNotIn("_mark_plan_current_blocked", branch)

    def test_generate_missing_retry_context_routes_to_recovery(self):
        missing = missing_supervisor_retry_fields({
            "supervisor_verdict": "RETRY",
            "retry_reason": "build failed",
        })

        self.assertIn("failed_step_id", missing)
        self.assertIn("required_next_action", missing)
        self.assertEqual(missing_supervisor_retry_fields({"supervisor_verdict": "PASS"}), [])

    def test_generate_task_projection_keeps_constraints_and_drops_old_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmpdir
            plan = build_plan_state(
                "task-a",
                "- [>] Step 1: Annotate | Action: EXECUTE | Skills: [genomics/snpeff-annotation] "
                "| Inputs: [input.vcf] | Outputs: [annotated.vcf] | Done: annotated.vcf is non-empty",
            )
            save_plan_state(tmpdir, "task-a", plan)
            spec = merge_task_spec(
                create_task_spec("Annotate the declared VCF"),
                {"must_not_do": ["do not guess a species database"]},
                source="user",
                reason="constraint",
            )
            state = {
                "task_id": "task-a",
                "task_spec": spec,
                "input_audit": {"resolved_inputs": [{"role": "vcf", "path": "/data/input.vcf"}]},
            }
            projection = json.loads(agent._generate_task_spec_projection(
                state,
                [],
            ))
            current_step = agent._current_step_prompt_context(
                "task-a",
                state=state,
            )

        self.assertIn("do not guess a species database", projection["user_constraints"])
        self.assertNotIn("current_step", projection)
        self.assertIn("annotated.vcf", current_step)
        self.assertEqual(projection["resolved_inputs"][0]["path"], "/data/input.vcf")
        self.assertNotIn("change_log", projection)

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

    def test_go_resume_uses_durable_business_state_before_archive_context(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _archive_resume_context", source)
        self.assertIn("def _build_resume_prompt", source)
        self.assertIn("archive_resume = self._archive_resume_context(task_id)", source)
        self.assertIn("def _build_resume_runner_state", source)
        self.assertIn("runner_input = self._build_resume_runner_state(task_id, prompt, checkpoint_values)", source)

    def test_consultant_wiki_context_is_defined_before_loop_guard(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        wiki_index = source.index("consultant_wiki_context = self._wiki_memory_stage_context")
        guard_index = source.index("len(load_actions) >= CONSULTANT_LOAD_ACTION_LOOP_LIMIT")

        self.assertLess(wiki_index, guard_index)

    def test_generate_prunes_internal_consultant_load_artifacts(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _generate_visible_messages", source)
        self.assertIn("LOAD_WORKFLOW_TEMPLATE", source)
        self.assertIn("LOAD_SKILL_DOC", source)
        self.assertIn("<loaded_skill_doc", source)
        self.assertIn("self._generate_visible_messages(", source)
        self.assertIn("structured_recovery=bool(recovery_context_obj)", source)
        self.assertIn("visible_messages = (", source)
        self.assertIn("] + visible_messages", source)

    def test_consultant_uses_bounded_skill_retrieval_and_message_history(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("candidate_skill_ids_for_prompt", source)
        self.assertIn("get_skill_catalog_prompt(candidate_skill_ids)", source)
        self.assertIn("self._pack_consultant_context(", source)
        self.assertIn("Consultant prompt components", source)
        self.assertNotIn("skill_catalog = self.skill_manager.get_skill_catalog_prompt()", source)
        self.assertIn("consultation_state_context=consultation_state_context", source)
        self.assertNotIn("requesting FINISH/ASK repair.", source)
        self.assertIn("llm_transient_retry", source)
        self.assertIn("llm_retry_counts", source)
        self.assertIn("task state was preserved for resume", source)

    def test_generate_uses_only_step_derived_active_skills(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('selected = state.get("active_skills", []) or []', source)
        self.assertIn("Return the task-wide selection bound; Planner owns per-step narrowing.", source)
        self.assertNotIn("multi_markers = [", source)
        self.assertNotIn("*candidate_skills,\n                                *stage_skills", source)
        self.assertNotIn('active_skills_state = state.get("active_skills", []) or []', source)
        self.assertIn("def _current_plan_step_skill_ids", source)
        self.assertIn("def _plan_step_skill_violations", source)
        self.assertIn('"skill_selection_source": "plan_step_declared_skills"', source)
        self.assertIn('"active_skills_derived_from_plan_step"', source)

    def test_workflow_batches_are_step_scheduled_before_finish(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "bio_agents.py").read_text(encoding="utf-8")
        prompt = (root / "prompts" / "manager.py").read_text(encoding="utf-8")

        self.assertIn("def _next_workflow_batch_id", source)
        self.assertIn("def _activate_next_workflow_batch_after_plan_completion", source)
        self.assertIn("next_batch_update = self._activate_next_workflow_batch_after_plan_completion", source)
        self.assertIn('activation["next_step"] = "planner"', source)
        self.assertIn("Batch switching is an internal controller transition", prompt)
        self.assertIn("Later-stage skills remain only in later checklist items", prompt)
        self.assertNotIn("def _workflow_stage_requested", source)
        self.assertNotIn("stage_requested = self._workflow_stage_requested(stage, query_text)", source)

    def test_consultant_readonly_loop_handoffs_before_another_llm_repair(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("preflight_execute(", source)
        self.assertIn("record_execution_observation(", source)
        self.assertIn("begin_finalize(consultation_state)", source)
        self.assertIn('"duplicate_action_reused"', source)
        self.assertNotIn("consultant_execute_count_loop_guard_handoff", source)
        self.assertNotIn("<consultant_force_finish_from_observations>", source)

    def test_consultant_internal_failures_use_skill_only_fallback_without_main_controller(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("<consultant_loop_guard_blocked>", source)
        self.assertIn("Candidate skill hints", source)
        self.assertNotIn("consultant_loop_guard_skill_only", source)
        self.assertNotIn("semantic_contract_ready", source)
        self.assertIn('"next_step": "ask_user"', source)
        self.assertNotIn("from agent_core.main_controller import MainController", source)
        self.assertNotIn("self.main_controller = MainController()", source)
        self.assertNotIn("consultant_internal_failure_decision", source)

    def test_consultant_format_repair_budget_is_three_attempts(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("CONSULTANT_PARSE_REPAIR_LIMIT = 3", source)
        self.assertIn("format_repair_errors", source)
        self.assertIn("internal_validation_errors", source)
        self.assertIn("len(format_repair_errors) > CONSULTANT_PARSE_REPAIR_LIMIT", source)
        self.assertIn("len(internal_validation_errors) >= CONSULTANT_INTERNAL_VALIDATION_ERROR_LIMIT", source)

    def test_legacy_graph_routing_is_used_by_default(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "bio_agents.py").read_text(encoding="utf-8")
        manager = (root / "prompts" / "manager.py").read_text(encoding="utf-8")
        templates = (root / "prompts" / "templates.py").read_text(encoding="utf-8")
        state = (root / "agent_core" / "state.py").read_text(encoding="utf-8")

        self.assertNotIn("MAIN_AGENT_PROMPT", templates)
        self.assertNotIn("def get_main_agent_prompt", manager)
        self.assertNotIn("def main_agent(state: AgentState):", source)
        self.assertNotIn("PromptManager.get_main_agent_prompt", source)
        self.assertIn("node_recommended_next_step", source)
        self.assertIn('"workflow_state"', source)
        self.assertIn("workflow_state: dict", state)
        self.assertNotIn('workflow.add_node("main_agent"', source)
        self.assertNotIn('return "main_agent"', source)
        self.assertNotIn('workflow.add_edge("planner",     "main_agent")', source)
        self.assertIn('workflow.add_conditional_edges("planner", planner_router', source)
        self.assertIn('return "consultant"', source)

    def test_structured_actions_require_nonempty_execute_body(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "bio_agents.py").read_text(encoding="utf-8")
        templates = (root / "prompts" / "templates.py").read_text(encoding="utf-8")

        self.assertIn("def _recent_nonempty_execute_body", source)
        self.assertIn("def _extract_structured_action", source)
        self.assertIn('self._pending_action_is(pending_action, "execute")', source)
        self.assertIn('pending_action.get("code")', source)
        self.assertIn("has_execute_body = bool", source)
        self.assertIn('"execute_empty_body"', source)
        self.assertIn("Put the actual runnable command/script inside the tag or in `action_json.code`", templates)

    def test_prompts_distinguish_install_tool_from_runner(self):
        templates = (Path(__file__).resolve().parents[1] / "prompts" / "templates.py").read_text(encoding="utf-8")

        self.assertIn("INSTALL_TOOL IS NOT A RUNNER", templates)
        self.assertIn("not an environment name, not an interpreter", templates)
        self.assertIn("Planner must not describe `install_tool` as the command used to run analysis scripts", templates)
        self.assertIn("Never write commands like `install_tool_name <<EOF`", templates)

    def test_execute_reads_recent_execute_body_after_interleaving_message(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('pending_action = state.get("pending_action") or {}', source)
        self.assertIn("original_code = self._recent_nonempty_execute_body(state.get(\"messages\", []), window=6)", source)
        self.assertNotIn("last_message = state[\"messages\"][-1].content", source)

    def test_ask_user_answer_advances_ask_user_plan_step(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('self._plan_step_action_is(running_step, "ask_user")', source)
        self.assertIn('if stage == "recovery":', source)
        self.assertIn('"recovery_status": "USER_DECISION_RECEIVED"', source)
        self.assertIn("user_answered:", source)
        self.assertIn('"pending_human_question": {}', source)
        self.assertIn('"plan_progress": plan_progress', source)

    def test_context_search_does_not_complete_execute_step(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('self._plan_step_action_is(self._running_plan_step(load_plan_state(self.data_path, task_id)), "search")', source)
        self.assertIn('"search_completed"', source)

    def test_planner_assigns_skills_per_step(self):
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "prompts" / "manager.py").read_text(encoding="utf-8")
        source = (root / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("Skills: [canonical_skill_id,...]", prompt)
        self.assertIn("`Skills:` is mandatory on every checklist item", prompt)
        self.assertIn("Do not create `ACTIVATE_BATCH` steps", prompt)
        self.assertIn('active_skill_docs = ""', source)
        self.assertNotIn(
            "active_skill_docs = self.skill_manager.load_skills_for_prompt(planner_context_skills)",
            source,
        )
        self.assertIn("Planner prompt components: task=%s %s", source)

    def test_generate_stable_prompt_is_execution_node_scoped(self):
        templates = (Path(__file__).resolve().parents[1] / "prompts" / "templates.py").read_text(encoding="utf-8")

        self.assertIn("Generate is step-local by default", templates)
        self.assertIn("Follow the first running or pending item", templates)
        self.assertIn("You are the **Generate node**", templates)
        self.assertIn("produce exactly one step-local action", templates)

    def test_planner_validates_user_requirement_coverage(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "bio_agents.py").read_text(encoding="utf-8")
        prompt = (root / "prompts" / "manager.py").read_text(encoding="utf-8")

        self.assertIn("Plan coverage missing workflow handoff stage", source)
        self.assertIn("effective_output_contracts(", source)
        self.assertIn('"user_msg": str(user_msg)', source)
        self.assertIn("workflow as a minimum coverage scaffold, not a ceiling", prompt)
        self.assertIn("custom stages beyond the workflow", prompt)

    def test_consultant_selects_and_planner_schedules_workflow_skills(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "bio_agents.py").read_text(encoding="utf-8")
        consultant_prompt = (root / "prompts" / "templates.py").read_text(encoding="utf-8")
        planner_prompt = (root / "prompts" / "manager.py").read_text(encoding="utf-8")

        self.assertIn("Consultant owns skill discovery and coarse stage decomposition", consultant_prompt)
        self.assertIn("Consultant must never activate a batch or skill", consultant_prompt)
        self.assertIn("Never emit ACTIVE_SKILLS, FUTURE_SKILLS", consultant_prompt)
        self.assertIn("Planner owns execution scheduling", planner_prompt)
        self.assertIn("Each checklist item's `Skills:` field", planner_prompt)
        self.assertIn("strip_workflow_activation", source)

    def test_plan_tracker_is_persisted_and_reinjected_after_compression(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("build_plan_state(task_id, plan_content", source)
        self.assertIn("save_plan_state(self.data_path, task_id, plan_state)", source)
        self.assertIn("update_plan_markdown_status(plan_file, plan_state)", source)
        self.assertIn("compacted_messages, compressed = await self.memory_manager.maybe_compress", source)
        self.assertIn("RemoveMessage(id=REMOVE_ALL_MESSAGES)", source)
        self.assertIn("plan_progress = self._start_or_load_plan_progress", source)
        self.assertIn("plan_progress=plan_progress", source)
        self.assertIn("reset_plan_state(self.data_path, task_id)", source)

    def test_completed_resume_plans_are_sanitized_to_finish_only(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _planner_declares_completed_task", source)
        self.assertIn("def _finish_only_plan_from_completed_plan", source)
        self.assertIn("Action: FINISH", source)
        self.assertIn("plan_content = self._sanitize_completed_planner_plan(response.content)", source)
        self.assertNotIn('"already complete",', source)

    def test_generate_rewrites_completed_resume_plan_before_execution(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _replace_plan_tracker_with_plan", source)
        self.assertIn("def _resume_plan_material", source)
        self.assertIn("sanitized_current_plan = self._sanitize_completed_planner_plan", source)
        self.assertIn("resume_plan_material = self._resume_plan_material", source)
        self.assertIn("generate replaced completed-task plan with FINISH-only plan", source)
        self.assertIn("if self._current_plan_step_requests_finish(task_id):", source)
        self.assertIn("self._finish_solution_from_plan", source)

    def test_generate_plain_final_markdown_uses_final_gate(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _looks_like_plain_final_solution", source)
        self.assertIn("plain_final_solution = (", source)
        self.assertIn('generated_action = (\n                "finish" if answer_match or action_type == "solution" or plain_final_solution', source)
        self.assertIn("if answer_match or action_type == \"solution\" or plain_final_solution:", source)
        self.assertIn("else str(content or \"\").strip()", source)
        self.assertIn('"generate_exit_reason": "completion_candidate"', source)
        self.assertIn('"next_step": "supervisor_final_audit"', source)
        self.assertIn("_reconcile_completion_candidate_plan", source)

    def test_lightweight_tasks_cannot_route_to_installer(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('task_mode in {"readonly", "simple_execute"} and generated_action in {"install", "activate_batch", "search"}', source)
        self.assertIn("lightweight_disallowed_action", source)
        self.assertIn("lightweight_runtime_missing_no_install", source)
        self.assertIn('"next_step": "finish" if lightweight_mode else self._next_step_after_runtime_block(task_id)', source)
        self.assertIn('elif (install_match or action_type == "install_tool") and task_mode not in {"readonly", "simple_execute"}:', source)

    def test_installer_success_uses_direct_skill_healthcheck(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("recheck_successes = []", source)
        self.assertIn("Installation/runtime is usable after direct skill healthcheck", source)
        self.assertIn("Original installer final message was overridden because the verified runtime healthcheck passed", source)
        self.assertIn("if recheck_successes and not recheck_failures:", source)

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
        self.assertIn('elif self.wiki_enabled and not memory_packet_context.strip() and state.get("wiki_memory_context")', source)
        self.assertIn('"wiki_digest": ""', source)

    def test_consultant_uses_formal_upstream_artifact_audit_before_prompt(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("resolve_formal_upstream_artifacts", source)
        self.assertIn("format_formal_upstream_context", source)
        self.assertIn("input_resolution_context=input_resolution_context", source)
        self.assertNotIn("formal_upstream_artifact_audit", source)
        self.assertNotIn("formal_upstream_question", source)

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

        self.assertIn("Consultant response did not use any supported action format", source)
        self.assertIn("<action_json>", source)
        self.assertIn('f"consultant_unrecognized:{len(validation_errors)}"', source)
        self.assertIn('"next_step": "consultant"', source)
        self.assertNotIn('return {"messages": [response], "next_step": "human_input"}', source)

    def test_interactive_loop_does_not_complete_waiting_human_input(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("async def _run_agent_interrupt_loop", source)
        self.assertIn("def _snapshot_next_step", source)
        self.assertIn("def _terminal_status_from_snapshot", source)
        self.assertIn("terminal_status = self._terminal_status_from_snapshot(task_id, snapshot)", source)
        self.assertIn('if terminal_status == "waiting_user":', source)
        self.assertIn('self.profiler.finish_task(task_id, "waiting_user")', source)
        waiting_guard = source.index('if terminal_status == "waiting_user":')
        terminal_finish = source.index("self.profiler.finish_task(task_id, terminal_status)")
        wiki_guard = source.index('if terminal_status == "completed":', terminal_finish)
        wiki_offer = source.index("await self._maybe_offer_wiki_ingest", wiki_guard)
        self.assertLess(waiting_guard, terminal_finish)
        self.assertLess(wiki_guard, wiki_offer)

    def test_resume_reuses_agent_interrupt_loop(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("async def _run_agent_interrupt_loop", source)
        self.assertIn("await self._run_agent_interrupt_loop(Command(resume=user_response), tid)", source)
        self.assertIn('await self._run_agent_interrupt_loop("", tid, resume_initial=True)', source)

    def test_user_preferences_markdown_is_loaded_into_prompts(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _load_user_preferences", source)
        self.assertIn('os.getenv("BIOMIND_USER_PREFERENCES"', source)
        self.assertIn('"user_preferences.md"', source)
        self.assertIn("user_preferences_context = self._load_user_preferences()", source)
        self.assertIn('"user_preferences": user_preferences_context', source)

    def test_skill_runtime_guard_blocks_bare_managed_cli(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("managed_tools.add(\"cooler\")", source)
        self.assertIn("\"hicDifferentialTAD\"", source)
        self.assertIn("calls bare `{tool}`", source)
        self.assertIn("Do not ask for code review", source)

    def test_generate_does_not_reset_supervisor_retry_count(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertNotIn('"next_step": next_step, "retry_count": 0', source)
        self.assertIn('"next_step": next_step,', source)
        self.assertIn('"pending_action": structured_action if structured_action else {}', source)

    def test_supervisor_router_defaults_to_generate_not_planner(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")
        router_source = source.split("def supervisor_router(state):", 1)[1].split(
            'workflow.add_conditional_edges("supervisor"', 1
        )[0]

        self.assertIn('if recovery_route == "planner": return "planner"', router_source)
        self.assertNotIn('state.get("next_step")', router_source)
        self.assertTrue(router_source.rstrip().endswith('return "generate"'))

    def test_execute_success_waits_for_supervisor_before_completing_plan_step(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('"execute_success"', source)
        self.assertNotIn('self._mark_plan_current_done(task_id, f"execute success:', source)
        self.assertIn('verdict in {"PASS", "WARN"}', source)

    def test_actionable_supervisor_warn_routes_back_to_generate(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _supervisor_warn_requires_followup", source)
        self.assertIn('"warn_followup_required"', source)
        self.assertIn('warn_requires_followup = verdict == "WARN" and self._supervisor_warn_requires_followup(update_messages)', source)
        self.assertIn('update["supervisor_verdict"] = "RETRY"', source)
        self.assertIn('update["next_step"] = "generate"', source)

    def test_supervisor_escalate_continues_pending_plan(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        escalate_idx = source.index('elif verdict == "ESCALATE":')
        block_idx = source.index('self._mark_plan_current_blocked(task_id, "supervisor_verdict: ESCALATE")', escalate_idx)
        route_idx = source.index('update["next_step"] = self._next_step_after_runtime_block(task_id)', block_idx)
        finalizer_idx = source.index('if isinstance(update, dict) and update.get("next_step") == "finish":', route_idx)

        self.assertLess(route_idx, finalizer_idx)

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
        self.assertIn('f"STDERR:\\n{stderr_obs}"', source)
        self.assertNotIn("STDOUT:\\n{stdout[:2000]}", source)
        self.assertNotIn("STDERR:\\n{stderr[:1000]}", source)

    def test_consultant_blocks_unbounded_whole_file_viewers(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _consultant_unbounded_output_violation", source)
        self.assertIn("whole-file viewer", source)
        self.assertIn("_consultant_unbounded_output_violation(clean_code)", source)
        self.assertIn("head -c 2048", source)

    def test_supervisor_retry_budget_is_two_for_agent(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("max_retries=2", source)

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

    def test_execute_router_returns_to_stable_legacy_chain(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('"install_subagent"', source)
        self.assertIn('if step == "install_subagent": return "install_subagent"', source)
        self.assertIn('workflow.add_conditional_edges("execute", execute_router', source)
        self.assertIn('["generate", "planner", "install_subagent", "supervisor", "supervisor_final_audit", END]', source)
        self.assertIn('return "planner" if self._plan_patch_requested(state) else "generate"', source)

    def test_execute_guard_routes_back_to_generate_without_main_agent(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        router_idx = source.index("def execute_router(state):")
        generate_guard_idx = source.index('if state.get("next_step") == "generate":', router_idx)
        supervisor_idx = source.index('return "supervisor"', router_idx)

        self.assertLess(generate_guard_idx, supervisor_idx)
        self.assertIn('return "generate"', source[generate_guard_idx:supervisor_idx])

    def test_install_subagent_failure_returns_to_legacy_generate_chain(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def install_subagent_router(state):", source)
        self.assertIn('workflow.add_conditional_edges("install_subagent", install_subagent_router', source)
        self.assertNotIn('workflow.add_edge("install_subagent", "generate")', source)
        self.assertIn('return "generate"', source[source.index("def install_subagent_router(state):"):])
        self.assertIn("def _next_step_after_install_result", source)
        self.assertIn('"next_step": self._next_step_after_install_result(task_id)', source)

    def test_repeated_installation_loop_detection_continues_pending_plan(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        loop_idx = source.index("repeated installation loop detected")
        return_idx = source.index('return {', loop_idx)

        self.assertIn('"next_step": self._next_step_after_install_result(task_id)', source[return_idx:])

    def test_install_subagent_resume_interrupt_does_not_increment_attempt_count(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        pending_idx = source.index("installer_has_pending_interrupt = bool(")
        attempt_idx = source.index("self._install_attempt_counts[attempt_key]", pending_idx)
        guard_idx = source.index("if not installer_has_pending_interrupt:", pending_idx)
        loop_idx = source.index("repeated installation loop detected", attempt_idx)

        self.assertLess(guard_idx, attempt_idx)
        self.assertLess(attempt_idx, loop_idx)
        self.assertIn(
            "if self._install_attempt_counts.get(attempt_key, 0) > 2:",
            source[attempt_idx:loop_idx],
        )
        self.assertIn(
            "if not installer_has_pending_interrupt:\n                    self._install_scope_attempt_counts[scope_key]",
            source[pending_idx:loop_idx],
        )

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

    def test_top_level_quit_aliases_do_not_start_a_new_task(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn(
            'user_input.strip().lower() in {"/exit", "/quit", "q", "quit", "exit"}',
            source,
        )

    def test_signal_handlers_keep_terminal_repair_out_of_interrupt_path(self):
        from unittest.mock import patch

        with patch("bio_agents.terminate_active_processes", return_value=1) as terminate:
            with patch("bio_agents.restore_terminal_state") as restore:
                with self.assertRaises(KeyboardInterrupt):
                    _handle_interrupt(None, None)
        terminate.assert_called_once_with()
        restore.assert_not_called()

    def test_suspend_uses_default_signal_then_restores_after_resume(self):
        import signal
        from unittest.mock import call, patch

        with patch("bio_agents.signal.signal") as set_handler:
            with patch("bio_agents.os.kill") as kill:
                with patch("bio_agents.restore_terminal_state") as restore:
                    _handle_suspend(signal.SIGTSTP, None)

        self.assertEqual(
            set_handler.call_args_list,
            [call(signal.SIGTSTP, signal.SIG_DFL), call(signal.SIGTSTP, _handle_suspend)],
        )
        kill.assert_called_once()
        restore.assert_called_once_with()

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

    def test_consultant_loop_guard_uses_skill_only_handoff_before_asking(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("consultant_loop_guard_blocked", source)
        self.assertNotIn("consultant_loop_guard_skill_only", source)
        self.assertNotIn("semantic_contract_ready", source)
        self.assertIn("FINISH", source)
        self.assertIn("BioMind could not derive any valid canonical skills", source)

    def test_consultant_loop_guard_counts_since_latest_human_message(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("recent_consultant_messages = messages[last_human_index + 1:]", source)
        self.assertIn("for m in recent_consultant_messages", source)

    def test_plan_step_can_switch_active_skill_without_losing_selected_pool(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("def _active_skill_update_for_current_plan_step", source)
        self.assertIn('"skill_selection_source": "plan_step_declared_skills"', source)
        self.assertIn('"active_skills_derived_from_plan_step"', source)
        self.assertIn('"selected_skills": selected_pool', source)
        self.assertIn("state.update(step_skill_update)", source)
        self.assertIn("**step_skill_update", source)

    def test_runtime_redirect_preserves_selected_skill_pool(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn('selected_skill_pool_state = state.get("selected_skills", []) or runtime_skills_state', source)
        self.assertIn('"selected_skills": selected_skill_pool_state', source)
        self.assertIn('"selected_skills": state.get("selected_skills", []) or runtime_skills_after_execute', source)
        self.assertNotIn('"selected_skills": runtime_skills_state', source)
        self.assertNotIn('"selected_skills": handoff.get("active_skills", [])', source)

    def test_consultant_execute_after_readonly_loop_guard_is_blocked(self):
        source = (Path(__file__).resolve().parents[1] / "bio_agents.py").read_text(encoding="utf-8")

        self.assertIn("finalize_execute_forbidden", source)
        self.assertIn("Every probe must declare unresolved requirement_ids", source)
        self.assertIn('"consultation_state": consultation_state', source)
        self.assertNotIn("consultant_execute_after_force_finish_blocked", source)


if __name__ == "__main__":
    unittest.main()

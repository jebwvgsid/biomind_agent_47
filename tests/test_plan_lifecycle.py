import tempfile
import unittest
import json
import shutil
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.plan_tracker import (
    TASK_STATUS_BLOCKED,
    approve_plan_state,
    block_plan_and_downstream,
    bind_plan_to_task_revision,
    build_plan_state,
    commit_consultant_candidate_state,
    load_plan_state,
    save_plan_state,
    sync_plan_state_from_stage_status,
    task_status,
)
from agent_core.task_spec import create_task_spec
from agent_core.recovery import ExecutionRecord, RecoveryStateStore
from agent_core.completion_gate import CompletionGate
from bio_agents import BioAnalystAgent


class _StateManager:
    def __init__(self, query="run analysis", status="paused_retryable"):
        self.query = query
        self.status = status

    def get_task_context(self, _task_id):
        return {"task_info": {"user_query": self.query, "status": self.status}}

    def update_task_status(self, _task_id, status):
        self.status = status


class PlanLifecycleTests(unittest.TestCase):
    def _agent(self, root):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = root
        agent.state_manager = _StateManager()
        return agent

    @staticmethod
    def _approved_plan(task_id="task-a"):
        state = build_plan_state(
            task_id,
            "- [>] Step 1: Run | Action: EXECUTE | Skills: [] | Done: output exists",
        )
        bind_plan_to_task_revision(state, 1)
        state["current_task"] = {"task_spec": create_task_spec("run analysis")}
        approve_plan_state(state, 1)
        return state

    def test_approved_legacy_plan_resumes_at_generate_without_reapproval(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            node, audit = agent._resolve_resume_node("task-a")

        self.assertEqual(node, "generate")
        self.assertEqual(audit["resume_reason"], "approved_valid_plan")
        self.assertEqual(audit["plan_status"], "approved")

    def test_fresh_pending_action_resumes_at_execute(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {"task_spec": create_task_spec("run analysis"), "task_revision": 1}
            action = agent._bind_pending_action(
                {"type": "execute", "code": "echo ok"},
                graph_state,
                "task-a",
            )

            node, audit = agent._resolve_resume_node("task-a", {**graph_state, "pending_action": action})

        self.assertEqual(node, "execute")
        self.assertEqual(audit["resume_reason"], "fresh_pending_action")

    def test_readonly_execute_is_bound_as_diagnostic_even_if_model_calls_it_analysis(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {"task_spec": create_task_spec("run analysis"), "task_revision": 1}

            action = agent._bind_pending_action(
                {
                    "type": "execute",
                    "code": "command -v snpEff && tail -n 20 logs/snpeff.build.log",
                    "purpose": "analysis",
                },
                graph_state,
                "task-a",
            )

        self.assertEqual(action["purpose"], "probe")
        self.assertEqual(action["command_role"], "diagnostic")

    def test_verified_runtime_paths_are_transported_without_path_reconstruction(self):
        with tempfile.TemporaryDirectory() as root:
            env = Path(root) / "envs" / "bio_gatk"
            jar = env / "share" / "snpeff" / "snpEff.jar"
            java_home = Path(root) / "temurin-21"
            jar.parent.mkdir(parents=True)
            jar.write_bytes(b"jar")
            java_home.mkdir()

            bindings = BioAnalystAgent._runtime_bindings_from_state({
                "runtime_audit": [{
                    "status": "ready",
                    "resolved_runtime_paths": {
                        "RESOLVED_SNPEFF_JAR": str(jar),
                        "RESOLVED_JAVA_HOME": str(java_home),
                    },
                }],
            })

        self.assertEqual(bindings["SNPEFF_JAR"], str(jar.resolve()))
        self.assertEqual(bindings["JAVA_HOME"], str(java_home.resolve()))
        self.assertNotIn("PROJECT_ROOT", bindings)

    def test_repeated_no_op_plan_patch_is_fused_to_same_step_generate(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            signature = f"step-1:{state['semantic_plan_hash']}"
            state["task_state"]["budgets"]["no_op_plan_patch_by_signature"] = {signature: 1}
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            guarded = agent._apply_transition_guard(
                "supervisor",
                {"task_spec": create_task_spec("run analysis"), "task_revision": 1},
                {
                    "next_step": "planner",
                    "required_next_action": "PLAN_PATCH",
                    "failure_owner": "PLAN",
                    "pending_recovery_action": {"type": "plan_patch", "failure_owner": "PLAN"},
                    "supervisor_verdict": "WARN",
                },
                "task-a",
            )

        self.assertEqual(guarded["next_step"], "generate")
        self.assertEqual(guarded["required_next_action"], "SAME_STEP_GENERATE")
        self.assertEqual(guarded["pending_recovery_action"]["type"], "same_step_generate")
        self.assertEqual(guarded["supervisor_verdict"], "")
        self.assertEqual(guarded["transition_guard"], "repeated_no_op_plan_patch_fused")

    def test_unapproved_plan_resumes_at_plan_review(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: output exists",
            )
            bind_plan_to_task_revision(state, 1)
            state["task_spec"] = create_task_spec("run analysis")
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            node, _ = agent._resolve_resume_node("task-a")

        self.assertEqual(node, "plan_review")

    def test_durable_preplan_state_requires_versioned_finalize_before_planner(self):
        with tempfile.TemporaryDirectory() as root:
            commit_consultant_candidate_state(
                root,
                "task-a",
                {
                    "task_spec": create_task_spec("run analysis", task_id="task-a"),
                    "selected_skills": ["genomics/snpeff-annotation"],
                    "consultation_state": {},
                    "resolved_bindings": {},
                    "pending_question": {},
                },
            )
            state = load_plan_state(root, "task-a")
            state["generated_from_task_revision"] = 1
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            node, audit = agent._resolve_resume_node("task-a")

        self.assertEqual(node, "consultant")
        self.assertEqual(
            audit["resume_reason"],
            "versioned_finalize_required_before_planner",
        )
        self.assertFalse(audit["finalize_compiled_for_planner"])

    def test_durable_compiled_v4_preplan_state_resumes_at_planner(self):
        with tempfile.TemporaryDirectory() as root:
            commit_consultant_candidate_state(
                root,
                "task-a",
                {
                    "task_spec": create_task_spec("run analysis", task_id="task-a"),
                    "selected_skills": ["genomics/snpeff-annotation"],
                    "consultation_state": {
                        "finalize_protocol": {
                            "schema_version": 4,
                            "wire_schema_version": 3,
                            "finalize_status": "compiled",
                            "compile_outcome": {"status": "compiled"},
                        }
                    },
                    "resolved_bindings": {},
                    "pending_question": {},
                },
            )
            state = load_plan_state(root, "task-a")
            state["generated_from_task_revision"] = 1
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            node, audit = agent._resolve_resume_node("task-a")

        self.assertEqual(node, "planner")
        self.assertEqual(
            audit["resume_reason"],
            "compiled_task_spec_and_skills_without_valid_plan",
        )
        self.assertTrue(audit["finalize_compiled_for_planner"])

    def test_resume_runner_restores_durable_finalize_owner_without_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            state = load_plan_state(root, "task-a")
            state["task_spec"] = create_task_spec("run analysis")
            state["selected_skill_ids"] = ["genomics/snpeff-annotation"]
            state["generated_from_task_revision"] = 1
            durable_owner = {
                "phase": "finalize",
                "completion_decision": "ready_to_finalize",
                "finalize_protocol": {
                    "schema_version": 4,
                    "wire_schema_version": 3,
                    "finalize_status": "resolving",
                    "attempts": [{
                        "attempt_id": "attempt-held",
                        "provider_status": "in_flight",
                    }],
                },
            }
            state.setdefault("task_state", {})[
                "consultation_state"
            ] = durable_owner
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            agent.default_project_id = "default"
            agent._get_or_create_stable_prompt = lambda *_args, **_kwargs: {
                "prompt": "stable",
                "core_memory_loaded_at": "fixture",
            }
            agent._runtime_status_for_task = lambda *_args, **_kwargs: {}

            runner = agent._build_resume_runner_state(
                "task-a", "continue", checkpoint_values={}
            )

        self.assertEqual(runner["next_step"], "consultant")
        self.assertEqual(runner["consultation_state"], durable_owner)
        self.assertEqual(
            runner["consultation_state"]["finalize_protocol"]["attempts"][0][
                "attempt_id"
            ],
            "attempt-held",
        )

    def test_stale_durable_completed_cannot_override_active_v4_finalize(self):
        with tempfile.TemporaryDirectory() as root:
            state = load_plan_state(root, "task-a")
            state["task_spec"] = create_task_spec("run analysis")
            state["selected_skill_ids"] = ["genomics/snpeff-annotation"]
            state["generated_from_task_revision"] = 1
            state.setdefault("task_state", {})["status"] = "completed"
            state["task_state"]["consultation_state"] = {
                "phase": "finalize",
                "finalize_protocol": {
                    "schema_version": 4,
                    "wire_schema_version": 3,
                    "finalize_status": "resolving",
                    "attempts": [{
                        "attempt_id": "attempt-held",
                        "provider_status": "in_flight",
                    }],
                },
            }
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            node, audit = agent._resolve_resume_node("task-a")

        self.assertEqual(node, "consultant")
        self.assertEqual(
            audit["resume_reason"],
            "versioned_finalize_required_before_planner",
        )
        self.assertTrue(audit["finalize_owner_requires_consultant"])

    def test_stale_durable_completed_cannot_override_legacy_finalize_repair(self):
        with tempfile.TemporaryDirectory() as root:
            state = load_plan_state(root, "task-a")
            state["task_spec"] = create_task_spec("run analysis")
            state["selected_skill_ids"] = ["genomics/snpeff-annotation"]
            state["generated_from_task_revision"] = 1
            state.setdefault("task_state", {})["status"] = "completed"
            state["task_state"]["consultation_state"] = {
                "phase": "finalize",
                "finalize_protocol": {
                    "schema_version": 2,
                    "wire_schema_version": 2,
                    "status": "repairing",
                    "attempts": [],
                },
            }
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            node, _ = agent._resolve_resume_node("task-a")

        self.assertEqual(node, "consultant")

    def test_planner_timeout_preserves_approved_plan_and_resume_node(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            update = agent._pause_retryable_llm_failure(
                {"task_spec": create_task_spec("run analysis"), "task_revision": 1},
                "task-a",
                "planner",
                TimeoutError("planner timed out"),
            )
            persisted = load_plan_state(root, "task-a")
            node, audit = agent._resolve_resume_node("task-a")

        self.assertEqual(update["recovery_status"], "paused_retryable")
        self.assertEqual(persisted["plan_status"], "approved")
        self.assertEqual(persisted["steps"][0]["status"], "running")
        self.assertEqual(persisted["failure"]["failure_stage"], "planner")
        self.assertEqual(node, "generate")
        self.assertEqual(audit["failure_stage"], "planner")

    def test_plain_continue_is_control_event_and_does_not_change_task_revision(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            before = state["current_task"]["task_spec"]["revision"]

            decision = agent._continue_control_state("task-a")
            after = load_plan_state(root, "task-a")["current_task"]["task_spec"]["revision"]

        self.assertEqual(decision["action"], "resume")
        self.assertEqual(before, after)

    def test_stale_state_manager_completed_cannot_override_durable_preplan_state(self):
        with tempfile.TemporaryDirectory() as root:
            state = load_plan_state(root, "task-a")
            state["current_task"] = {"task_spec": create_task_spec("run analysis")}
            state["selected_skill_ids"] = ["genomics/snpeff-annotation"]
            state["generated_from_task_revision"] = 1
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            agent.state_manager.status = "completed"

            decision = agent._continue_control_state("task-a")

        self.assertEqual(decision["action"], "resume")
        self.assertEqual(
            decision["resume_audit"]["resume_reason"],
            "versioned_finalize_required_before_planner",
        )

    def test_plan_approval_does_not_modify_task_spec_revision(self):
        state = build_plan_state(
            "task-a",
            "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: output exists",
        )
        state["current_task"] = {"task_spec": create_task_spec("run analysis")}
        before = state["current_task"]["task_spec"]["revision"]

        approve_plan_state(state, before)

        self.assertEqual(state["current_task"]["task_spec"]["revision"], before)
        self.assertEqual(state["approved_task_revision"], before)

    def test_generate_redirect_audit_reports_exact_missing_field(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "- [>] Step 1: Run | Action: EXECUTE | Skills: [] | Done: N/A",
            )
            bind_plan_to_task_revision(state, 1)
            state["current_task"] = {"task_spec": create_task_spec("run analysis")}
            approve_plan_state(state, 1)
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            task_spec = state["current_task"]["task_spec"]

            validation = agent._validate_generate_context(
                {"task_mode": "managed", "task_spec": task_spec, "task_revision": 1},
                "task-a",
            )
            update = agent._generate_plan_redirect(
                {"task_mode": "managed", "task_spec": task_spec, "task_revision": 1},
                "task-a",
                validation,
            )

        self.assertEqual(validation["reason_code"], "missing_success_criteria")
        self.assertEqual(update["next_step"], "planner")
        self.assertEqual(update["resume_audit"]["missing_fields"], ["steps[0].success_criteria"])
        self.assertEqual(update["resume_audit"]["validation_function"], "validate_plan_for_generate")

    def test_resume_uses_persisted_recovery_phase_without_replanning(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            RecoveryStateStore(root, "task-a").save_phase({
                "phase": "probe",
                "status": "active",
                "pending_action": {"type": "execute", "code": "bash -x preflight.sh"},
            })
            agent = self._agent(root)

            node, audit = agent._resolve_resume_node("task-a")

        self.assertEqual(node, "generate")
        self.assertEqual(audit["resume_reason"], "resume_persisted_local_recovery")
        self.assertEqual(audit["recovery_phase"], "probe")

    def test_resolved_durable_recovery_ignores_stale_checkpoint_retry_action(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            RecoveryStateStore(root, "task-a").save_phase({
                "phase": "resolved",
                "status": "RESOLVED",
                "pending_action": {},
            })
            agent = self._agent(root)

            node, audit = agent._resolve_resume_node(
                "task-a",
                {
                    "pending_recovery_action": {
                        "type": "execute",
                        "code": "repeat_stale_probe",
                    },
                    "supervisor_verdict": "RETRY",
                },
            )

        self.assertEqual(node, "generate")
        self.assertEqual(audit["resume_reason"], "approved_valid_plan")
        self.assertEqual(audit["recovery_phase"], "resolved")

    def test_blocked_step_is_terminal_and_propagates_downstream(self):
        state = build_plan_state(
            "task-a",
            "- [>] Step 1: Analyze | Action: EXECUTE | Skills: [] | Done: result exists\n"
            "- [ ] Step 2: Report | Action: EXECUTE | Skills: [] | Done: report exists",
        )

        changed = block_plan_and_downstream(state, "required input unavailable")

        self.assertTrue(changed)
        self.assertEqual(task_status(state), TASK_STATUS_BLOCKED)
        self.assertEqual([step["status"] for step in state["steps"]], ["blocked", "skipped_upstream_blocked"])

    def test_blocked_stage_status_does_not_mark_plan_done(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "- [>] Step 1: Analyze | Action: EXECUTE | Skills: [] | Done: result exists\n"
                "- [ ] Step 2: Report | Action: EXECUTE | Skills: [] | Done: report exists",
            )
            status_path = Path(root) / "stage_status.tsv"
            status_path.write_text(
                "stage\tstatus\toutput_path\tnotes\nanalysis\tblocked\t-\tmissing runtime\n",
                encoding="utf-8",
            )

            changed = sync_plan_state_from_stage_status(state, status_path)

        self.assertEqual(changed, 2)
        self.assertEqual([step["status"] for step in state["steps"]], ["blocked", "skipped_upstream_blocked"])

    def test_resume_terminal_recovery_wins_over_pending_action(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {"task_spec": create_task_spec("run analysis"), "task_revision": 1}
            action = agent._bind_pending_action({"type": "execute", "code": "echo ok"}, graph_state, "task-a")
            RecoveryStateStore(root, "task-a").save_phase({
                "phase": "blocked",
                "status": "BLOCKED",
                "pending_action": {"type": "blocked", "reason": "budget exhausted"},
            })

            node, audit = agent._resolve_resume_node("task-a", {**graph_state, "pending_action": action})

        self.assertEqual(node, "finish")
        self.assertEqual(audit["resume_reason"], "legacy_terminal_recovery_with_active_steps")

    def test_generate_plan_status_mutation_is_restored(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            mutated = load_plan_state(root, "task-a")
            mutated["steps"][0]["status"] = "done"
            save_plan_state(root, "task-a", mutated)
            agent = self._agent(root)

            attempted = agent._restore_generate_plan_authority("task-a", {"step-1": "running"})
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(attempted, {"step-1": "done"})
        self.assertEqual(persisted["steps"][0]["status"], "running")

    def test_generate_controller_start_transition_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: output exists",
            )
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            agent._start_or_load_plan_progress("task-a", "", "generate_started")
            attempted = agent._restore_generate_plan_authority(
                "task-a",
                {"step-1": "pending"},
            )
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(attempted, {})
        self.assertEqual(persisted["steps"][0]["status"], "running")

    def test_verified_pending_step_advances_without_replanning(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "\n".join([
                    "- [ ] Step 1: Preflight | Action: EXECUTE | Skills: [] | Done: input exists",
                    "- [ ] Step 2: Analyze | Action: EXECUTE | Skills: [] | Done: result exists",
                ]),
            )
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            progress = agent._mark_plan_current_done(
                "task-a",
                "supervisor_verdict: PASS",
            )
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(
            [step["status"] for step in persisted["steps"]],
            ["passed", "running"],
        )
        self.assertIn("Next step: Step 2", progress)

    def test_plain_language_completion_is_detected_as_audit_candidate(self):
        text = (
            "任务已经全部完成，所有预期产物均已生成：annotated.vcf、snpEff_summary.html、"
            "ann_effect_summary.tsv、report.md 和 reproduce.sh。"
        )

        self.assertTrue(BioAnalystAgent._looks_like_plain_final_solution(text))

    def test_runtime_node_cannot_route_to_planner_without_plan_patch(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {
                "task_mode": "managed",
                "task_spec": create_task_spec("run analysis"),
                "task_revision": 1,
            }

            rejected = agent._apply_transition_guard(
                "generate",
                graph_state,
                {"next_step": "planner", "pending_action": {}},
                "task-a",
            )
            accepted = agent._apply_transition_guard(
                "generate",
                graph_state,
                {
                    "next_step": "planner",
                    "pending_action": {},
                    "required_next_action": "PLAN_PATCH",
                    "failure_owner": "PLAN",
                    "recovery_phase": "plan_repair",
                },
                "task-a",
            )

        self.assertEqual(rejected["next_step"], "generate")
        self.assertEqual(rejected["transition_guard"], "planner_requires_explicit_plan_patch")
        self.assertEqual(accepted["next_step"], "planner")

    def test_plan_patch_signal_without_plan_owner_is_rejected(self):
        self.assertFalse(BioAnalystAgent._plan_patch_requested({
            "required_next_action": "PLAN_PATCH",
            "failure_owner": "RUNTIME",
        }))
        self.assertTrue(BioAnalystAgent._plan_patch_requested({
            "required_next_action": "PLAN_PATCH",
            "failure_owner": "PLAN",
        }))

    def test_invalid_generated_action_contract_returns_to_generate_not_planner(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {
                "task_mode": "managed",
                "task_spec": create_task_spec("run analysis"),
                "task_revision": 1,
            }

            guarded = agent._apply_transition_guard(
                "generate",
                graph_state,
                {
                    "next_step": "execute",
                    "pending_action": {"type": "execute", "code": "echo ok"},
                },
                "task-a",
            )

        self.assertEqual(guarded["next_step"], "generate")
        self.assertEqual(guarded["required_next_action"], "ACTION_FORMAT_REPAIR")
        self.assertEqual(guarded["failure_owner"], "INTERNAL_PROTOCOL")
        self.assertNotEqual(guarded.get("required_next_action"), "PLAN_PATCH")

    def test_completion_candidate_syncs_terminal_status_then_final_gate_passes(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "final"
            output_dir.mkdir()
            for name in ("report.md", "reproduce.sh"):
                (output_dir / name).write_text("completed\n", encoding="utf-8")
            stage_status = output_dir / "stage_status.tsv"
            stage_status.write_text(
                "stage\tstatus\toutput_path\tnotes\n"
                "preflight\tcompleted\t-\tverified\n"
                "report\tcompleted\treport.md\tcomplete\n",
                encoding="utf-8",
            )
            plan_text = "\n".join([
                "- [>] Step 1: Validate inputs | Action: EXECUTE | Skills: [] | Done: inputs verified",
                "- [ ] Step 2: Finalize report.md, reproduce.sh, and stage_status.tsv | Action: EXECUTE | Skills: [] | Done: final artifacts exist",
            ])
            plan_state = build_plan_state("task-a", plan_text)
            plan_state["steps"][0]["status"] = "passed"
            plan_state["steps"][0]["evidence"] = [{"event": "passed", "detail": "Supervisor PASS"}]
            plan_state["steps"][1]["status"] = "running"
            plan_state["output_dir"] = str(output_dir)
            save_plan_state(root, "task-a", plan_state)
            script = Path(root) / "task_outputs" / "task-a" / "step_1.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("echo verified\n", encoding="utf-8")
            agent = self._agent(root)

            changed, _ = agent._reconcile_completion_candidate_plan(
                "task-a",
                {
                    "current_plan": f"Output directory: {output_dir}\n{plan_text}",
                    "completion_candidate": {
                        "text": f"Task complete. Deliverables are in {output_dir}."
                    },
                    "task_spec": {"goal": "run analysis", "output_dir": str(output_dir)},
                },
            )
            persisted = load_plan_state(root, "task-a")
            gate = CompletionGate.validate_final(
                root,
                "task-a",
                persisted,
                f"Output directory: {output_dir}\n{plan_text}",
                require_execution_evidence=True,
                require_supervisor_evidence=False,
            )

        self.assertTrue(changed)
        self.assertEqual([step["status"] for step in persisted["steps"]], ["passed", "done"])
        self.assertTrue(gate.passed, gate.feedback)

    def test_invalid_execute_action_is_format_repaired_once_then_escalated(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {
                "task_mode": "managed",
                "task_spec": create_task_spec("run analysis"),
                "task_revision": 1,
            }

            first = agent._apply_transition_guard(
                "generate", graph_state, {"next_step": "execute", "pending_action": {}}, "task-a"
            )
            second = agent._apply_transition_guard(
                "generate", graph_state, {"next_step": "execute", "pending_action": {}}, "task-a"
            )
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(first["next_step"], "generate")
        self.assertEqual(first["required_next_action"], "ACTION_FORMAT_REPAIR")
        self.assertEqual(first["failure_owner"], "INTERNAL_PROTOCOL")
        self.assertEqual(second["next_step"], "supervisor")
        self.assertEqual(second["recovery_decision"]["kind"], "ESCALATE")
        self.assertEqual(second["required_next_action"], "INTERNAL_PROTOCOL_ERROR")
        self.assertEqual(task_status(persisted), "diagnosing")
        self.assertEqual(
            (persisted.get("task_state") or {}).get("resume_route_hint"),
            "supervisor",
        )

    def test_generated_blocked_status_text_is_not_record_failure_intent(self):
        purpose = BioAnalystAgent._pending_action_purpose(
            {"type": "execute", "code": "printf 'analysis\\tblocked\\n' > stage_status.tsv"},
            {"action": "execute"},
        )

        self.assertEqual(purpose, "analysis")

    def test_generated_failure_text_does_not_override_validation_plan_purpose(self):
        sources = {
            "pass_and_blocked_branches": """
write_report() { printf 'status: %s\\n' "$1" > compatibility_report.txt; }
if test -s missing_in_reference.txt; then
  write_report "BLOCKED"
  exit 1
fi
write_report "PASS"
""",
            "comment_only": "# BLOCKED result would be written to compatibility_report.txt\nprintf 'PASS\\n'",
            "string_or_help_template": "printf 'help: report status may be BLOCKED\\n' > help.txt",
        }
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "- [>] Step 1: Validate inputs | Action: EXECUTE | Skills: [] | Done: validation passes",
            )
            state["steps"][0]["purpose"] = "validation"
            bind_plan_to_task_revision(state, 1)
            state["current_task"] = {"task_spec": create_task_spec("validate inputs")}
            approve_plan_state(state, 1)
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {
                "task_spec": state["current_task"]["task_spec"],
                "task_revision": 1,
            }

            for label, code in sources.items():
                with self.subTest(label=label):
                    action = agent._bind_pending_action(
                        {"type": "execute", "code": code},
                        graph_state,
                        "task-a",
                    )
                    self.assertEqual(action["purpose"], "validation")
                    self.assertEqual(action["command_role"], "required_validation")

    def test_record_failure_requires_typed_intent_and_matching_plan_purpose(self):
        purpose = BioAnalystAgent._pending_action_purpose(
            {
                "type": "execute",
                "purpose": "record_failure",
                "code": "printf 'status recorded\\n' > stage_status.tsv",
            },
            {"action": "execute", "purpose": "analysis"},
        )

        self.assertEqual(purpose, "record_failure")
        self.assertFalse(
            BioAnalystAgent._execution_purpose_completes_step(
                {"action": "execute", "purpose": "analysis"},
                purpose,
            )
        )
        self.assertTrue(
            BioAnalystAgent._execution_purpose_completes_step(
                {"action": "execute", "purpose": "record_failure"},
                purpose,
            )
        )

    def test_validation_step_binds_readonly_execute_as_validation(self):
        purpose = BioAnalystAgent._pending_action_purpose(
            {"type": "execute", "code": "test -s input.vcf && head -n 2 input.vcf"},
            {"action": "execute", "purpose": "validation"},
        )

        self.assertEqual(purpose, "validation")
        self.assertTrue(
            BioAnalystAgent._execution_purpose_completes_step(
                {"action": "execute", "purpose": "validation"},
                purpose,
            )
        )

    def test_probe_cannot_complete_analysis_step(self):
        self.assertFalse(
            BioAnalystAgent._execution_purpose_completes_step(
                {"action": "execute", "purpose": "analysis"},
                "probe",
            )
        )

    def test_blocked_task_never_routes_to_generate(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            block_plan_and_downstream(state, "upstream failed")
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            guarded = agent._apply_transition_guard(
                "supervisor", {"task_mode": "managed"}, {"next_step": "generate"}, "task-a"
            )

        self.assertEqual(guarded["next_step"], "finish")
        self.assertEqual(guarded["transition_guard"], "terminal_blocked")

    def test_authoritative_controller_block_still_finalizes_and_skips_downstream(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_plan_state(
                "task-a",
                "- [>] Step 1: Validate | Action: EXECUTE | Skills: [] | Done: validation complete\n"
                "- [ ] Step 2: Analyze | Action: EXECUTE | Skills: [] | Done: result exists",
            )
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            agent._finalize_blocked_state("task-a", "authoritative controller blocker")
            persisted = load_plan_state(root, "task-a")
            output_root = Path(root) / "task_outputs" / "task-a"
            blocked_status_exists = (output_root / "blocked_status.json").is_file()
            stage_status_text = (output_root / "stage_status.tsv").read_text(encoding="utf-8")
            report_text = (output_root / "report.md").read_text(encoding="utf-8")

        self.assertEqual(task_status(persisted), TASK_STATUS_BLOCKED)
        self.assertEqual(
            [step["status"] for step in persisted["steps"]],
            ["blocked", "skipped_upstream_blocked"],
        )
        self.assertTrue(blocked_status_exists)
        self.assertIn("\tblocked\t", stage_status_text)
        self.assertIn("Status: blocked", report_text)

    def test_recovery_question_is_not_overridden_by_exhausted_phase(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            guarded = agent._apply_transition_guard(
                "execute",
                {"task_mode": "managed"},
                {
                    "next_step": "ask_user",
                    "recovery_phase": "exhausted",
                    "pending_recovery_action": {
                        "type": "ask_user",
                        "reason": "no safe probe remains",
                        "failure_owner": "INPUT_DATA",
                        "requires_user_input": True,
                    },
                    "pending_human_question": {
                        "stage": "recovery",
                        "question": "Provide another recovery direction or confirm blocked.",
                    },
                },
                "task-a",
            )

        self.assertEqual(guarded["next_step"], "ask_user")
        self.assertEqual(guarded["transition_guard"], "awaiting_recovery_user_decision")

    def test_same_transition_budget_persists_and_stops(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            def resolve_generate_transition():
                guarded = agent._apply_transition_guard(
                    "generate", {}, {"next_step": "generate"}, "task-a"
                )
                return agent._resolve_and_commit_effective_transition(
                    "generate", {}, guarded, "task-a"
                )

            first = resolve_generate_transition()
            second = resolve_generate_transition()
            third = resolve_generate_transition()
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(first["next_step"], "generate")
        self.assertEqual(second["next_step"], "generate")
        self.assertEqual(third["next_step"], "finish")
        signatures = persisted["task_state"]["budgets"]["same_transition_by_signature"]
        self.assertEqual(sum(signatures.values()), 3)

    def test_generate_parser_accepts_step_completion_candidate(self):
        action = BioAnalystAgent._extract_structured_action(
            '<action_json>{"action":"STEP_COMPLETION_CANDIDATE","step_id":"step-2",'
            '"reason":"Existing evidence satisfies the step.",'
            '"evidence_ids":["execution-1"]}</action_json>'
        )

        self.assertEqual(action["type"], "step_completion_candidate")
        self.assertEqual(action["step_id"], "step-2")
        self.assertEqual(action["evidence_ids"], ["execution-1"])

    def test_manifest_absent_renderer_step_rejects_step_completion_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            state["steps"][0].update({
                "renderer_composite": True,
                "renderer_id": "genomics.snpeff_annotation",
            })
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            guarded = agent._apply_transition_guard(
                "generate",
                {},
                {
                    "next_step": "generate",
                    "step_completion_candidate": {
                        "type": "step_completion_candidate",
                        "step_id": "step-1",
                        "reason": "probe passed",
                        "evidence_ids": ["probe-1"],
                    },
                },
                "task-a",
            )

        self.assertEqual(guarded["next_step"], "generate")
        self.assertEqual(guarded["transition_guard"], "renderer_manifest_required_before_step_completion")
        self.assertEqual(guarded["required_next_action"], "ACTION_FORMAT_REPAIR")
        self.assertFalse(guarded.get("step_completion_candidate"))

    def test_action_format_feedback_is_visible_to_next_generate_call(self):
        messages = [
            HumanMessage(content="run analysis"),
            HumanMessage(content="<observation>validation passed</observation>"),
            AIMessage(content="Everything is ready. Should I continue?"),
            SystemMessage(content=(
                "<action_format_repair>\nCurrent step: step-2\n"
                "Allowed actions: EXECUTE, STEP_COMPLETION_CANDIDATE, PLAN_PATCH, BLOCKED\n"
                "</action_format_repair>"
            )),
        ]

        visible = BioAnalystAgent._generate_visible_messages(messages)
        rendered = "\n".join(str(message.content) for message in visible)

        self.assertIn("<action_format_repair>", rendered)
        self.assertIn("STEP_COMPLETION_CANDIDATE", rendered)

    def test_action_format_budget_is_separate_from_execution_retry(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            first = agent._record_action_format_failure("task-a", "step-1", "narrative one")
            second = agent._record_action_format_failure("task-a", "step-1", "narrative one")
            persisted = load_plan_state(root, "task-a")
            budgets = persisted["task_state"]["budgets"]
            protocol_files = list(
                (Path(root) / "task_outputs" / "task-a" / "action_protocol").glob("*")
            )

        self.assertEqual(first[:2], (1, 1))
        self.assertEqual(second[:2], (1, 2))
        # Evidence creation is read-only with respect to recovery budgets;
        # _apply_transition_guard is the sole decision commit entry.
        self.assertEqual(budgets["action_format_repair_count"], 0)
        self.assertEqual(budgets["execution_retry_count"], 0)
        self.assertEqual(budgets["generate_action_count"], 0)
        self.assertEqual(len(protocol_files), 4)
        self.assertTrue(any(path.suffix == ".txt" for path in protocol_files))
        self.assertTrue(any(path.suffix == ".json" for path in protocol_files))

    def test_internal_recovery_error_cannot_enter_ask_user(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            guarded = agent._apply_transition_guard(
                "execute",
                {"task_mode": "managed"},
                {
                    "next_step": "ask_user",
                    "failure_owner": "RUNTIME",
                    "pending_recovery_action": {
                        "type": "ask_user",
                        "failure_owner": "RUNTIME",
                        "reason": "runtime drift",
                    },
                    "pending_human_question": {"question": "Which environment should I use?"},
                },
                "task-a",
            )

        self.assertEqual(guarded["next_step"], "supervisor")
        self.assertEqual(guarded["transition_guard"], "internal_error_cannot_ask_user")
        self.assertEqual(guarded["pending_human_question"], {})

    def test_action_format_repair_does_not_consume_generic_loop_budget(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            guarded = agent._apply_transition_guard(
                "generate",
                {"task_mode": "managed"},
                {
                    "next_step": "generate",
                    "generate_exit_reason": "unrecognized_action",
                    "pending_recovery_action": {
                        "type": "ACTION_FORMAT_REPAIR",
                        "invalid_response_hash": "abc",
                    },
                },
                "task-a",
            )
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(guarded["next_step"], "generate")
        self.assertEqual(
            persisted["task_state"]["budgets"].get("same_transition_by_signature", {}),
            {},
        )

    def test_step_completion_candidate_advances_only_after_evidence_gate(self):
        with tempfile.TemporaryDirectory() as root:
            plan = build_plan_state(
                "task-a",
                "\n".join([
                    "- [>] Step 1: Validate inputs | Action: EXECUTE | Purpose: validation | Skills: [] | Done: input files verified",
                    "- [ ] Step 2: Run analysis | Action: EXECUTE | Purpose: analysis | Skills: [] | Done: result.tsv exists",
                ]),
            )
            save_plan_state(root, "task-a", plan)
            record = ExecutionRecord.from_mapping({
                "execution_id": "execution-1",
                "step_id": "step-1",
                "execution_status": "succeeded",
                "purpose": "validation",
                "returncode": 0,
                "code": "test -s input.vcf && echo input files verified",
                "stdout": "input files verified\n",
                "script_path": str(Path(root) / "check.sh"),
            })
            RecoveryStateStore(root, "task-a").save_execution_record(record)
            agent = self._agent(root)

            passed, gate, _ = agent._validate_and_advance_step_candidate(
                "task-a",
                {
                    "step_id": "step-1",
                    "reason": "existing validation evidence",
                    "evidence_ids": ["execution-1"],
                },
            )
            persisted = load_plan_state(root, "task-a")

        self.assertTrue(passed, gate.feedback)
        self.assertEqual([step["status"] for step in persisted["steps"]], ["passed", "running"])
        self.assertTrue(any(
            evidence.get("event") == "execution_evidence_relation"
            for evidence in persisted["steps"][0]["evidence"]
        ))

    def test_controller_routes_verified_step_candidate_without_generate_plan_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            plan = build_plan_state(
                "task-a",
                "- [>] Step 1: Validate inputs | Action: EXECUTE | Purpose: validation | Skills: [] | Done: input files verified",
            )
            save_plan_state(root, "task-a", plan)
            RecoveryStateStore(root, "task-a").save_execution_record(ExecutionRecord.from_mapping({
                "execution_id": "execution-1",
                "step_id": "step-1",
                "execution_status": "succeeded",
                "purpose": "validation",
                "returncode": 0,
                "stdout": "input files verified\n",
            }))
            agent = self._agent(root)

            guarded = agent._apply_transition_guard(
                "generate",
                {"task_mode": "managed"},
                {
                    "next_step": "generate",
                    "step_completion_candidate": {
                        "step_id": "step-1",
                        "reason": "existing validation evidence",
                        "evidence_ids": ["execution-1"],
                    },
                },
                "task-a",
            )
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(guarded["next_step"], "supervisor_final_audit")
        self.assertEqual(guarded["transition_guard"], "step_completion_candidate_final_audit")
        self.assertEqual(persisted["steps"][0]["status"], "passed")

    def test_repeated_identical_step_candidate_rejection_routes_to_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            plan = build_plan_state(
                "task-a",
                "- [>] Step 1: Run analysis | Action: EXECUTE | Purpose: analysis | Skills: [] "
                "| Done: a criterion absent from all execution evidence",
            )
            save_plan_state(root, "task-a", plan)
            RecoveryStateStore(root, "task-a").save_execution_record(ExecutionRecord.from_mapping({
                "execution_id": "execution-1",
                "step_id": "step-1",
                "execution_status": "succeeded",
                "purpose": "analysis",
                "returncode": 0,
                "stdout": "unrelated output",
            }))
            agent = self._agent(root)
            candidate = {
                "step_id": "step-1",
                "reason": "unsupported claim",
                "evidence_ids": ["execution-1"],
            }

            first = agent._apply_transition_guard(
                "generate", {"task_mode": "managed"},
                {"next_step": "generate", "step_completion_candidate": candidate},
                "task-a",
            )
            second = agent._apply_transition_guard(
                "generate", {"task_mode": "managed"},
                {"next_step": "generate", "step_completion_candidate": candidate},
                "task-a",
            )

        self.assertEqual(first["next_step"], "generate")
        self.assertEqual(second["next_step"], "supervisor")
        self.assertEqual(second["transition_guard"], "step_completion_candidate_rejected")
        self.assertEqual(second["required_next_action"], "INTERNAL_PROTOCOL_ERROR")
        self.assertEqual(second["recovery_decision"]["kind"], "ESCALATE")

    def test_one_execution_cannot_converge_consecutive_validation_steps(self):
        with tempfile.TemporaryDirectory() as root:
            plan = build_plan_state(
                "task-a",
                "\n".join([
                    "- [>] Step 1: Validate inputs | Action: EXECUTE | Purpose: validation | Skills: [] | Done: input files verified",
                    "- [ ] Step 2: Validate names | Action: EXECUTE | Purpose: validation | Skills: [] | Done: reference names consistent",
                    "- [ ] Step 3: Run analysis | Action: EXECUTE | Purpose: analysis | Skills: [] | Done: result.tsv exists",
                ]),
            )
            save_plan_state(root, "task-a", plan)
            record = ExecutionRecord.from_mapping({
                "execution_id": "execution-1",
                "step_id": "preflight",
                "execution_status": "succeeded",
                "purpose": "validation",
                "returncode": 0,
                "code": "echo input files verified; echo reference names consistent",
                "stdout": "input files verified\nreference names consistent\n",
            })
            RecoveryStateStore(root, "task-a").save_execution_record(record)
            agent = self._agent(root)

            advanced, _ = agent._advance_satisfied_validation_steps(
                "task-a", {"execution_id": "execution-1"}
            )
            persisted = load_plan_state(root, "task-a")

        self.assertEqual(advanced, 0)
        self.assertEqual(
            [step["status"] for step in persisted["steps"]],
            ["running", "pending", "pending"],
        )

    def test_session_1785308919_replay_closes_input_step_but_not_unproven_name_check(self):
        source = Path(
            "/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/"
            "task_outputs/session_1785308919"
        )
        if not source.exists():
            self.skipTest("session_1785308919 evidence is not available")
        with tempfile.TemporaryDirectory() as root:
            task_id = "session_1785308919"
            destination = Path(root) / "task_outputs" / task_id / "executions"
            destination.mkdir(parents=True)
            shutil.copy2(
                source / "executions" / "execution_records.jsonl",
                destination / "execution_records.jsonl",
            )
            plan = json.loads((source / "plan_state.json").read_text(encoding="utf-8"))
            plan["task_state"]["status"] = "running"
            for index, step in enumerate(plan["steps"]):
                step["status"] = "passed" if index == 0 else "running" if index == 1 else "pending"
                if index > 0:
                    step["evidence"] = []
            save_plan_state(root, task_id, plan)
            evidence_id = json.loads(
                (destination / "execution_records.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )["execution_id"]
            agent = self._agent(root)

            passed, gate, _ = agent._validate_and_advance_step_candidate(
                task_id,
                {
                    "step_id": "step-2",
                    "reason": "input preflight already ran",
                    "evidence_ids": [evidence_id],
                },
            )
            auto_advanced, _ = agent._advance_satisfied_validation_steps(
                task_id, {"execution_id": evidence_id}
            )
            persisted = load_plan_state(root, task_id)

        self.assertFalse(passed)
        self.assertEqual(gate.classification, "step_candidate_cross_step_scope")
        self.assertEqual(auto_advanced, 0)
        self.assertEqual(persisted["steps"][1]["status"], "running")
        self.assertEqual(persisted["steps"][2]["status"], "pending")

    def test_consultant_internal_self_transitions_use_consultant_loop_policy(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)

            results = [
                agent._apply_transition_guard(
                    "consultant",
                    {},
                    {"next_step": "consultant"},
                    "task-a",
                )
                for _ in range(4)
            ]
            persisted = load_plan_state(root, "task-a")

        self.assertTrue(all(result["next_step"] == "consultant" for result in results))
        counters = persisted["task_state"].get("budgets", {}).get(
            "same_transition_by_signature",
            {},
        )
        self.assertNotIn("consultant->consultant", counters)

    def test_duplicate_successful_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._approved_plan()
            save_plan_state(root, "task-a", state)
            agent = self._agent(root)
            graph_state = {"task_spec": create_task_spec("run analysis"), "task_revision": 1}
            action = agent._bind_pending_action({"type": "execute", "code": "echo ok"}, graph_state, "task-a")
            RecoveryStateStore(root, "task-a").save_execution_record(ExecutionRecord.from_mapping({
                "execution_id": action["action_id"],
                "action_id": action["action_id"],
                "step_id": "step-1",
                "returncode": 0,
            }))

            reason = agent._validate_pending_action_contract(action, graph_state, "task-a")

        self.assertIn("already executed successfully", reason)



if __name__ == "__main__":
    unittest.main()

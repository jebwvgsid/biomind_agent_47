import json
import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.consultation_state import (
    begin_finalize,
    consultation_prompt_context,
    evaluate_consultation_completion,
    new_consultation_state,
    preflight_execute,
    record_execution_observation,
    seed_structured_evidence,
)
from bio_agents import BioAnalystAgent
from execution.profiling import RunProfiler


def _seeded(*, max_total_execute=4, max_no_progress=2):
    state = new_consultation_state(
        "consultation-1",
        "task-1",
        max_total_execute=max_total_execute,
        max_no_progress=max_no_progress,
    )
    return seed_structured_evidence(
        state,
        input_audit={
            "relevant": True,
            "status": "resolved",
            "resolved_inputs": [{"role": "variants", "path": "/data/input.vcf"}],
            "unresolved": False,
        },
        candidate_skill_ids=["domain/example-tool"],
        routing_candidate_ids=["domain/example-tool"],
    )


def _proposal(command="tool --version"):
    return {
        "command": command,
        "requirement_ids": ["runtime:domain-example-tool"],
        "purpose": "Verify the declared runtime is callable.",
        "expected_evidence": {"returncode": 0},
    }


class ConsultationStateTests(unittest.TestCase):
    def test_a_one_successful_probe_is_ready_to_finish(self):
        state = _seeded()
        check = preflight_execute(state, _proposal(), cwd="/work")
        self.assertEqual(check["decision"], "allow")
        state = record_execution_observation(
            check["state"],
            fingerprint=check["fingerprint"],
            proposal=_proposal(),
            returncode=0,
            observation="runtime ready",
            provenance={"kind": "dummy"},
        )
        self.assertEqual(state["completion_decision"], "ready_to_finalize")
        self.assertEqual(begin_finalize(state)["phase"], "finalize")

    def test_b_success_observation_routes_to_finalize_not_execute_loop(self):
        check = preflight_execute(_seeded(), _proposal(), cwd="/work")
        state = record_execution_observation(
            check["state"], fingerprint=check["fingerprint"], proposal=_proposal(),
            returncode=0, observation="ok",
        )
        rendered = consultation_prompt_context(begin_finalize(state))
        self.assertIn('"allowed_actions": [\n    "finish",\n    "ask"', rendered)
        self.assertNotIn('"execute",', rendered)

    def test_c_duplicate_success_reuses_observation_without_increment(self):
        first = preflight_execute(_seeded(), _proposal(), cwd="/work")
        state = record_execution_observation(
            first["state"], fingerprint=first["fingerprint"], proposal=_proposal(),
            returncode=0, observation="original observation",
        )
        # Simulate a stale requirement projection while preserving the action ledger.
        state["requirements"]["runtime:domain-example-tool"]["status"] = "unresolved"
        state["phase"] = "needs_evidence"
        second = preflight_execute(state, _proposal(), cwd="/work")
        self.assertEqual(second["decision"], "duplicate_action_reused")
        self.assertEqual(second["observation"], "original observation")
        self.assertEqual(second["state"]["execute_call_count"], 1)

    def test_d_structured_input_audit_prevents_redundant_input_probe(self):
        state = _seeded()
        proposal = {
            "command": "inspect /data/input.vcf",
            "requirement_ids": ["formal-inputs"],
            "purpose": "Inspect formal input",
            "expected_evidence": "exists",
        }
        check = preflight_execute(state, proposal, cwd="/work")
        self.assertEqual(check["decision"], "evidence_already_satisfied")
        self.assertEqual(check["state"]["execute_call_count"], 0)

    def test_e_similar_actions_with_different_arguments_are_not_duplicates(self):
        first = preflight_execute(_seeded(), _proposal("dummy --target a"), cwd="/work")
        second = preflight_execute(_seeded(), _proposal("dummy --target b"), cwd="/work")
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_f_no_progress_guard_is_semantic_not_command_keyword_based(self):
        state = _seeded(max_no_progress=2)
        for command in ("dummy --probe one", "dummy --probe two", "dummy --probe three"):
            check = preflight_execute(state, _proposal(command), cwd="/work")
            self.assertEqual(check["decision"], "allow")
            state = record_execution_observation(
                check["state"], fingerprint=check["fingerprint"], proposal=_proposal(command),
                returncode=1, observation="no new evidence",
            )
            if state["completion_decision"] == "needs_user":
                break
        self.assertEqual(state["completion_decision"], "needs_user")
        self.assertEqual(state["completion_reason"], "no_progress_limit_reached")

    def test_e2_duplicate_failed_action_is_not_executed_again(self):
        proposal = _proposal("dummy --fails")
        first = preflight_execute(_seeded(), proposal, cwd="/work")
        state = record_execution_observation(
            first["state"], fingerprint=first["fingerprint"], proposal=proposal,
            returncode=1, observation="failed once",
        )
        second = preflight_execute(state, proposal, cwd="/work")
        self.assertEqual(second["decision"], "duplicate_failed_action_suppressed")
        self.assertEqual(second["state"]["execute_call_count"], 1)

    def test_g_total_execute_ceiling_is_only_emergency_guard(self):
        state = _seeded(max_total_execute=2, max_no_progress=10)
        for command in ("dummy --one", "dummy --two"):
            check = preflight_execute(state, _proposal(command), cwd="/work")
            state = record_execution_observation(
                check["state"], fingerprint=check["fingerprint"], proposal=_proposal(command),
                returncode=1, observation="failed",
            )
        self.assertEqual(state["completion_decision"], "blocked")
        self.assertEqual(state["completion_reason"], "emergency_execute_ceiling_reached")

    def test_h_finalize_schema_allows_only_finish_or_ask(self):
        state = _seeded()
        check = preflight_execute(state, _proposal(), cwd="/work")
        state = record_execution_observation(
            check["state"], fingerprint=check["fingerprint"], proposal=_proposal(),
            returncode=0, observation="ok",
        )
        context = json.loads(consultation_prompt_context(begin_finalize(state)).split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertEqual(context["allowed_actions"], ["finish", "ask"])

    def test_i_finished_contract_has_no_unresolved_evidence(self):
        state = _seeded()
        check = preflight_execute(state, _proposal(), cwd="/work")
        state = record_execution_observation(
            check["state"], fingerprint=check["fingerprint"], proposal=_proposal(),
            returncode=0, observation="ok",
        )
        state = begin_finalize(state)
        self.assertEqual(state["unresolved_requirement_ids"], [])

    def test_i2_finish_compiles_semantics_before_planner_without_future_gate(self):
        source = inspect.getsource(BioAnalystAgent._build_graph)
        consultant_source = source[source.index("async def consultant"):source.index("async def ask_user")]
        self.assertNotIn("semantic_contract_ready", consultant_source)
        finish_source = consultant_source[consultant_source.index("# FINISH") :]
        self.assertLess(
            finish_source.index("merge_compiled_constraints("),
            finish_source.index('"next_step": "planner"'),
        )

    def test_j_consultant_memory_projection_omits_raw_prompt_and_command(self):
        hit = SimpleNamespace(
            id=7,
            evidence_ref="session_archive:7",
            task_id="older-task",
            event_type="root_cause_analysis",
            summary="Durable conclusion: environment identity drift caused failure.",
            content="RAW USER PROMPT; command: secret-tool --all",
            metadata={"root_cause": "environment identity drift", "status": "confirmed"},
            paths=["/safe/artifact"],
        )
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.session_archive = SimpleNamespace(search=lambda *args, **kwargs: [hit])
        text = agent._consultant_archive_facts_context(
            query="related task", project_id="project", exclude_task_id="current", limit=4,
        )
        self.assertIn("environment identity drift", text)
        self.assertNotIn("RAW USER PROMPT", text)
        self.assertNotIn("secret-tool --all", text)

    def test_k_orphaned_running_snapshot_is_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task-1"
            task_dir.mkdir()
            metrics = {
                "task_id": "task-1", "status": "running", "events": [],
                "run_lease": {"owner_pid": 99999999, "process_identity": "99999999:1", "state": "active"},
            }
            (task_dir / "run_metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
            reconciled = RunProfiler(tmp).reconcile_stale_run("task-1")
            self.assertEqual(reconciled["status"], "interrupted")
            self.assertEqual(reconciled["run_lease"]["state"], "orphaned")

    def test_j2_exception_timeout_and_cancel_never_remain_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiler = RunProfiler(tmp)
            cases = [
                ("exception", RuntimeError("boom"), "failed"),
                ("timeout", TimeoutError("late"), "timeout"),
                ("cancel", asyncio.CancelledError(), "cancelled"),
                ("signal", KeyboardInterrupt(), "interrupted"),
            ]
            for task_id, exc, expected in cases:
                profiler.start_task(task_id)
                self.assertEqual(profiler.finish_for_exception(task_id, exc), expected)
                self.assertEqual(profiler.snapshot(task_id)["status"], expected)

    def test_l_executor_lifecycle_is_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiler = RunProfiler(tmp)
            profiler.start_task("task-1")
            for stage in ("REQUESTED", "APPROVED", "SPAWN_STARTED", "PROCESS_STARTED", "PROCESS_COMPLETED", "OBSERVATION_RECORDED"):
                profiler.record_executor_lifecycle(
                    "task-1", stage, action_fingerprint="abc", consultation_id="consultation-1",
                    requirement_ids=["runtime:domain-example-tool"],
                )
            stages = [event["stage"] for event in profiler.snapshot("task-1")["events"]]
            self.assertEqual(stages, ["REQUESTED", "APPROVED", "SPAWN_STARTED", "PROCESS_STARTED", "PROCESS_COMPLETED", "OBSERVATION_RECORDED"])

    def test_l2_executor_failure_lifecycle_names_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiler = RunProfiler(tmp)
            profiler.start_task("task-1")
            expected = ["SPAWN_FAILED", "PROCESS_TERMINATED", "TIMEOUT", "CANCELLED"]
            for stage in expected:
                profiler.record_executor_lifecycle("task-1", stage)
            self.assertEqual(
                [event["stage"] for event in profiler.snapshot("task-1")["events"]],
                expected,
            )

    def test_negative_dummy_tool_has_no_tool_specific_special_case(self):
        state = _seeded()
        check = preflight_execute(state, _proposal("acme-dummy --health --value 17"), cwd="/work")
        self.assertEqual(check["decision"], "allow")
        self.assertTrue(check["fingerprint"])


if __name__ == "__main__":
    unittest.main()

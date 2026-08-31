import json
import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.consultation_state import (
    begin_finalize,
    commit_finalize_decision,
    consultation_prompt_context,
    emit_finalize_repair_projection,
    evaluate_consultation_completion,
    finalize_repair_projection,
    initialize_finalize_protocol,
    migrate_finalize_protocol_state,
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


def _finalize_state():
    return initialize_finalize_protocol(
        new_consultation_state("consultation-1", "task-1"),
        finalize_context_id="finalize-context-1",
        base_contract_hash="base-hash-1",
    )


class ConsultationStateTests(unittest.TestCase):
    def test_structured_runtime_evidence_upgrades_an_existing_unresolved_requirement(self):
        state = _seeded()
        self.assertEqual(
            state["requirements"]["runtime:domain-example-tool"]["status"],
            "unresolved",
        )

        state = seed_structured_evidence(
            state,
            candidate_skill_ids=["domain/example-tool"],
            routing_candidate_ids=["domain/example-tool"],
            runtime_evidence={
                "domain/example-tool": {
                    "status": "ready",
                    "healthcheck_status": "passed",
                    "skill_id": "domain/example-tool",
                }
            },
        )

        requirement = state["requirements"]["runtime:domain-example-tool"]
        self.assertEqual(requirement["status"], "satisfied")
        self.assertEqual(requirement["satisfied_by"], ["structured_runtime_evidence"])
        self.assertEqual(state["completion_decision"], "ready_to_finalize")

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

    def test_i2_finish_enters_versioned_finalize_before_planner(self):
        source = inspect.getsource(BioAnalystAgent._build_graph)
        consultant_source = source[source.index("async def consultant"):source.index("async def ask_user")]
        self.assertNotIn("semantic_contract_ready", consultant_source)
        finish_source = consultant_source[consultant_source.index("# FINISH") :]
        self.assertLess(
            finish_source.index("merge_compiled_constraints("),
            finish_source.index(
                '"contract_status": "semantic_contract_finalize_pending"'
            ),
        )
        self.assertIn('"next_step": "consultant"', finish_source)
        self.assertIn(
            '"completion_reason": "formal_contract_ready_for_versioned_finalize"',
            finish_source,
        )
        self.assertNotIn('"final_contract": {', finish_source)

    def test_i3_real_consultant_node_enters_versioned_finalize_dispatcher(self):
        source = inspect.getsource(BioAnalystAgent._build_graph)
        consultant_source = source[
            source.index("async def consultant"):source.index("async def ask_user")
        ]
        finalize_branch = consultant_source[
            consultant_source.index("if finalize_mode:"):
            consultant_source.index("deterministic_base_constraints =")
        ]
        self.assertIn(
            "return await self._run_consultant_finalize(",
            finalize_branch,
        )
        self.assertNotIn(
            "return await self._run_consultant_finalize_v2(",
            finalize_branch,
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


class FinalizeProtocolStateTests(unittest.TestCase):
    binding = {
        "finalize_context_id": "finalize-context-1",
        "base_contract_hash": "base-hash-1",
    }

    def _commit(self, state, transition, **values):
        return commit_finalize_decision(
            state,
            {"transition": transition, **self.binding, **values},
        )

    def _started(self, *, request_hash="request-1", call_kind="initial"):
        return self._commit(
            _finalize_state(),
            "request_started",
            attempt_id="attempt-1",
            call_id="call-1",
            call_kind=call_kind,
            canonical_request_hash=request_hash,
        )

    def _responded(self):
        return self._commit(
            self._started(),
            "provider_response",
            attempt_id="attempt-1",
            call_id="call-1",
            canonical_request_hash="request-1",
            response_artifact_ref="/protected/response-1",
            response_content_hash="content-1",
            response_excerpt="bounded",
        )

    def test_finalize_protocol_is_the_only_mutable_owner(self):
        state = self._responded()
        self.assertNotIn("consultant_contract_repair", state)
        projection = finalize_repair_projection(state)
        emitted = emit_finalize_repair_projection(state)
        self.assertEqual(
            emitted["consultant_contract_repair"]["latest_response_artifact_ref"],
            projection["latest_response_artifact_ref"],
        )
        emitted["consultant_contract_repair"]["latest_response_artifact_ref"] = "changed"
        self.assertEqual(
            state["finalize_protocol"]["repair"]["latest_response_artifact_ref"],
            "/protected/response-1",
        )

    def test_provider_success_is_distinct_from_protocol_failure(self):
        state = self._responded()
        self.assertEqual(state["finalize_protocol"]["provider_status"], "succeeded")
        self.assertEqual(state["finalize_protocol"]["protocol_status"], "not_validated")
        state = self._commit(
            state,
            "protocol_validation",
            attempt_id="attempt-1",
            protocol_status="wire_invalid",
            error_code="FINALIZE_RESPONSE_NOT_JSON",
            error_owner="controller_model_protocol",
            validation_errors=[{"path": "$", "reason": "one JSON object required"}],
        )
        protocol = state["finalize_protocol"]
        self.assertEqual(protocol["provider_status"], "succeeded")
        self.assertEqual(protocol["protocol_status"], "wire_invalid")
        self.assertEqual(protocol["status"], "repair_pending")
        self.assertEqual(protocol["repair"]["repair_kind"], "wire_format")

    def test_wire_and_field_repair_budgets_are_independent(self):
        state = self._commit(
            self._responded(),
            "protocol_validation",
            attempt_id="attempt-1",
            protocol_status="wire_invalid",
            error_code="FINALIZE_RESPONSE_NOT_JSON",
        )
        state = self._commit(
            state,
            "request_started",
            attempt_id="attempt-2",
            call_id="call-2",
            call_kind="wire_repair",
            canonical_request_hash="request-2",
        )
        state = self._commit(
            state,
            "provider_response",
            attempt_id="attempt-2",
            response_artifact_ref="/protected/response-2",
            response_content_hash="content-2",
        )
        parsed = {"action_type": "finish", "semantic_constraints": []}
        state = self._commit(
            state,
            "protocol_validation",
            attempt_id="attempt-2",
            protocol_status="field_invalid",
            error_code="FINALIZE_SCHEMA_INVALID",
            parsed_payload=parsed,
            allowed_payload_fields=["summary"],
        )
        state = self._commit(
            state,
            "request_started",
            attempt_id="attempt-3",
            call_id="call-3",
            call_kind="field_repair",
            canonical_request_hash="request-3",
        )
        repair = state["finalize_protocol"]["repair"]
        self.assertEqual(repair["wire_repair_attempt"], 1)
        self.assertEqual(repair["field_repair_attempt"], 1)
        self.assertEqual(repair["original_payload"], parsed)

    def test_first_response_and_first_parsed_payload_are_immutable(self):
        first_payload = {"action_type": "finish", "summary": "first"}
        state = self._commit(
            self._responded(),
            "protocol_validation",
            attempt_id="attempt-1",
            protocol_status="field_invalid",
            error_code="FINALIZE_SCHEMA_INVALID",
            parsed_payload=first_payload,
        )
        state = self._commit(
            state,
            "request_started",
            attempt_id="attempt-2",
            call_id="call-2",
            call_kind="field_repair",
            canonical_request_hash="request-2",
        )
        state = self._commit(
            state,
            "provider_response",
            attempt_id="attempt-2",
            response_artifact_ref="/protected/response-2",
            response_content_hash="content-2",
        )
        second_payload = {"action_type": "finish", "summary": "second"}
        state = self._commit(
            state,
            "protocol_validation",
            attempt_id="attempt-2",
            protocol_status="field_invalid",
            error_code="FINALIZE_SCHEMA_INVALID",
            parsed_payload=second_payload,
        )
        repair = state["finalize_protocol"]["repair"]
        self.assertEqual(repair["original_response_artifact_ref"], "/protected/response-1")
        self.assertEqual(repair["original_payload"], first_payload)
        self.assertEqual(repair["base_payload"], second_payload)

    def test_repeated_repair_response_blocks_without_third_call(self):
        state = self._commit(
            self._responded(),
            "protocol_validation",
            attempt_id="attempt-1",
            protocol_status="wire_invalid",
            error_code="FINALIZE_RESPONSE_NOT_JSON",
        )
        state = self._commit(
            state,
            "request_started",
            attempt_id="attempt-2",
            call_id="call-2",
            call_kind="wire_repair",
            canonical_request_hash="request-2",
        )
        state = self._commit(
            state,
            "provider_response",
            attempt_id="attempt-2",
            response_artifact_ref="/protected/response-2",
            response_content_hash="content-1",
        )
        protocol = state["finalize_protocol"]
        self.assertEqual(protocol["status"], "blocked_internal_contract")
        self.assertEqual(protocol["terminal_error_code"], "FINALIZE_IDENTICAL_RESPONSE_LOOP")
        self.assertEqual(len(protocol["attempts"]), 2)
        self.assertIsNone(state["pending_human_question"])

    def test_same_request_requires_safe_transport_failure(self):
        state = self._started()
        state = self._commit(
            state,
            "provider_failure",
            attempt_id="attempt-1",
            provider_status="transient_failed",
            error_code="FINALIZE_PROVIDER_TRANSIENT",
            retry_safe=True,
        )
        state = self._commit(
            state,
            "request_started",
            attempt_id="attempt-2",
            call_id="call-2",
            call_kind="transport_retry",
            canonical_request_hash="request-1",
        )
        self.assertEqual(len(state["finalize_protocol"]["attempts"]), 2)
        self.assertEqual(state["finalize_protocol"]["provider_status"], "in_flight")

    def test_provider_success_with_artifact_persistence_failure_is_storage_terminal(self):
        state = self._commit(
            self._started(),
            "response_persistence_failure",
            attempt_id="attempt-1",
        )
        protocol = state["finalize_protocol"]
        attempt = protocol["attempts"][-1]
        self.assertEqual(protocol["status"], "blocked_internal_contract")
        self.assertEqual(protocol["provider_status"], "succeeded")
        self.assertEqual(protocol["protocol_status"], "not_validated")
        self.assertEqual(protocol["terminal_error_code"], "FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
        self.assertEqual(protocol["terminal_owner"], "controller_storage")
        self.assertEqual(attempt["provider_status"], "succeeded")
        self.assertEqual(attempt["protocol_status"], "not_validated")
        self.assertFalse(attempt["retry_safe"])
        self.assertIsNone(state["pending_human_question"])

    def test_compiled_projection_can_only_be_overridden_by_checkpoint_integrity_failure(self):
        state = self._commit(self._responded(), "compiled")
        with self.assertRaisesRegex(ValueError, "terminal"):
            self._commit(
                state,
                "blocked_internal",
                error_code="FINALIZE_REPAIR_EXHAUSTED",
                error_owner="controller",
            )
        state = self._commit(
            state,
            "checkpoint_integrity_failure",
            error_code="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            error_owner="controller_storage",
        )
        protocol = state["finalize_protocol"]
        self.assertEqual(protocol["status"], "blocked_internal_contract")
        self.assertEqual(protocol["terminal_owner"], "controller_storage")
        self.assertEqual(protocol["protocol_status"], "blocked")

    def test_duplicate_answered_request_blocks_before_provider(self):
        state = self._responded()
        state = self._commit(
            state,
            "request_started",
            attempt_id="attempt-2",
            call_id="call-2",
            call_kind="initial",
            canonical_request_hash="request-1",
        )
        protocol = state["finalize_protocol"]
        self.assertEqual(protocol["terminal_error_code"], "FINALIZE_IDENTICAL_REQUEST_BLOCKED")
        self.assertEqual(protocol["attempts"][-1]["provider_status"], "not_called")

    def test_raw_response_cannot_enter_graph_state(self):
        with self.assertRaisesRegex(ValueError, "protected artifact"):
            self._commit(
                self._started(),
                "provider_response",
                attempt_id="attempt-1",
                response_artifact_ref="/protected/response",
                response_content_hash="hash",
                raw_response="exact assistant content",
            )

    def test_only_existing_ambiguity_can_enter_needs_user(self):
        responded = self._responded()
        with self.assertRaisesRegex(ValueError, "active context"):
            self._commit(
                responded,
                "needs_user",
                ambiguity_ids=["new-id"],
                context_unresolved_semantic_ids=["existing-id"],
                pending_human_question={"question": "choose"},
            )
        state = self._commit(
            responded,
            "needs_user",
            ambiguity_ids=["existing-id"],
            context_unresolved_semantic_ids=["existing-id"],
            pending_human_question={"question": "choose"},
        )
        self.assertEqual(state["finalize_protocol"]["pending_ambiguity_id"], "existing-id")
        self.assertEqual(state["phase"], "needs_user")

    def test_user_answer_replaces_context_with_zeroed_budgets(self):
        state = self._responded()
        state = self._commit(
            state,
            "needs_user",
            ambiguity_ids=["existing-id"],
            context_unresolved_semantic_ids=["existing-id"],
            pending_human_question={"question": "choose"},
        )
        with self.assertRaisesRegex(ValueError, "applied ambiguity answer"):
            commit_finalize_decision(state, {
                "transition": "replace_context",
                **self.binding,
                "new_finalize_context_id": "finalize-context-2",
                "new_base_contract_hash": "base-hash-2",
            })
        with self.assertRaisesRegex(ValueError, "exact pending ambiguity"):
            self._commit(
                state,
                "ambiguity_answer_applied",
                task_spec_revision=2,
                ambiguity_id="wrong-id",
            )
        state = self._commit(
            state,
            "ambiguity_answer_applied",
            task_spec_revision=2,
            ambiguity_id="existing-id",
        )
        state = commit_finalize_decision(state, {
            "transition": "replace_context",
            **self.binding,
            "new_finalize_context_id": "finalize-context-2",
            "new_base_contract_hash": "base-hash-2",
        })
        protocol = state["finalize_protocol"]
        self.assertEqual(protocol["finalize_context_id"], "finalize-context-2")
        self.assertEqual(protocol["attempts"], [])
        self.assertEqual(protocol["repair"]["wire_repair_attempt"], 0)
        self.assertEqual(protocol["repair"]["field_repair_attempt"], 0)
        self.assertIsNone(state["pending_human_question"])

    def test_legacy_migration_is_bound_or_blocks(self):
        legacy = {
            "consultant_contract_repair": {
                "schema_version": 2,
                "repair_attempt": 1,
                "original_payload": {"action_type": "finish"},
                "errors": [{"path": "$.summary", "reason": "required"}],
            },
        }
        blocked = migrate_finalize_protocol_state(legacy)
        self.assertEqual(
            blocked["finalize_protocol"]["terminal_error_code"],
            "FINALIZE_CONTEXT_BINDING_UNRECOVERABLE",
        )
        self.assertNotIn("consultant_contract_repair", blocked)
        migrated = migrate_finalize_protocol_state(
            legacy,
            finalize_context_id="context",
            base_contract_hash="base",
        )
        self.assertEqual(migrated["finalize_protocol"]["repair"]["field_repair_attempt"], 1)
        self.assertEqual(migrated["finalize_protocol"]["repair"]["original_payload"], {"action_type": "finish"})
        self.assertNotIn("consultant_contract_repair", migrated)


if __name__ == "__main__":
    unittest.main()

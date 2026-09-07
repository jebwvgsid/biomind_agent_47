"""Offline draft acceptance: real persistence/CAS and binding consumer, fake probe only."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langgraph.errors import GraphBubbleUp

from agent_core.action_runtime import capability_contract_hash
from agent_core.plan_tracker import build_plan_state, load_plan_state, save_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.runtime_evidence import digest, make_runtime_evidence, merge_runtime_evidence, runtime_evidence_state

_module_path = Path(__file__).resolve().parents[1] / "agent_core" / "workflow" / "runtime_evidence_recovery.py"
_spec = importlib.util.spec_from_file_location("runtime_evidence_recovery_under_test", _module_path)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
RuntimeEvidenceRecovery = _module.RuntimeEvidenceRecovery
RuntimeRecoveryPolicy = _module.RuntimeRecoveryPolicy
RuntimeRecoveryError = _module.RuntimeRecoveryError


class RuntimeEvidenceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-recovery-draft-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task_id = "task-runtime-test"
        self.now = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)
        self.skill_id = "fixture/generic"
        self.subject = {
            "skill_id": self.skill_id, "skill_document_digest": digest("doc"),
            "skill_contract_digest": digest("contracts"), "environment_id": "fixture_env",
            "environment_fingerprint": digest("env"), "host_id": "fixture-host",
            "inspection_contract_digest": digest("probe"),
        }
        self.contract = {
            "capability_id": "fixture.run", "selected_skill_id": self.skill_id,
            "required_runtime_binding_ids": ["runtime.tool.bin"],
            "optional_runtime_binding_ids": [], "required_artifact_binding_ids": [],
            "execution_guidance": [],
        }
        durable = build_plan_state(self.task_id,
            "- [x] Step 1: Prepare | Action: EXECUTE | Skills: [fixture/generic] | Done: prep complete\n"
            "- [>] Step 2: Run | Action: EXECUTE | Skills: [fixture/generic] | Done: real output\n")
        durable["workflow_routing_schema_version"] = 2
        durable["selected_skill_ids"] = [self.skill_id]
        durable["task_state"]["selected_skills"] = [self.skill_id]
        for row in durable["steps"]:
            if row["step_id"] == durable["current_step_id"]:
                row["execution_capability_id"] = "fixture.run"
                row["capability_contract_hash"] = capability_contract_hash(self.contract)
        save_plan_state(self.root, self.task_id, durable)
        self.state = {"task_id": self.task_id}
        self.calls = []
        self.inspection_contract = {
            "contract_id": "fixture-probe", "contract_digest": digest("probe"),
            "permitted": True, "safe_repeat": True,
            "coverage_binding_ids": ["runtime.tool.bin"],
            "cache_restore_permitted": False, "deadline_seconds": 1,
        }
        self.responses = [self.positive()]

        async def probe(request):
            self.calls.append(copy.deepcopy(request))
            if not self.responses:
                raise AssertionError("An already completed inspection was repeated")
            response = self.responses.pop(0)
            return await response(request) if callable(response) else copy.deepcopy(response)

        self.probe = probe
        self.service = self.new_service()

    def new_service(self, **kwargs):
        return RuntimeEvidenceRecovery(
            data_path=str(self.root), scope_provider=lambda _skill: copy.deepcopy(self.subject),
            inspect_runtime=self.probe, inspection_contract_provider=lambda _skill: copy.deepcopy(self.inspection_contract),
            clock=lambda: self.now, **kwargs,
        )

    def positive(self, path="/approved/runtime/tool"):
        return {
            "inspection_status": "succeeded", "observed_at": self.now.isoformat(),
            "audit": {"skill_id": self.skill_id, "env": "fixture_env", "status": "ready", "resolved_runtime_paths": {"RESOLVED_TOOL_BIN": path}},
            "coverage": {"runtime.tool.bin": {"availability": "AVAILABLE", "output_name": "RESOLVED_TOOL_BIN", "value_kind": "executable_path"}},
        }

    def request(self, **kwargs):
        return self.service.create_request(self.state, selected_skill_id=self.skill_id,
            capability_contract=self.contract, missing_binding_ids=["runtime.tool.bin"],
            original_error={"code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"}, **kwargs)

    def episode(self, request):
        return load_plan_state(self.root, self.task_id)["task_state"]["runtime_recovery"]["episodes"][request["episode_id"]]

    def persist_observations(self, *observations):
        records = [make_runtime_evidence(row["audit"], task_id=self.task_id,
            skill_scope=self.subject, observed_at=row["observed_at"],
            producer="runtime_inspection", coverage=row["coverage"])
            for row in observations]
        durable = load_plan_state(self.root, self.task_id)
        prior = durable["task_state"].get("runtime_evidence") or {}
        merged = merge_runtime_evidence(prior.get("records") or [], records,
            task_id=self.task_id, accepted_at=self.now.isoformat())
        durable["task_state"]["runtime_evidence"] = runtime_evidence_state(merged,
            prior_revision=prior.get("revision", 0))
        save_plan_state(self.root, self.task_id, durable)

    async def test_inspection_durable_acceptance_and_reloaded_same_step_resume(self):
        before = load_plan_state(self.root, self.task_id)
        request = self.request()
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "evidence_ready", result)
        after = load_plan_state(self.root, self.task_id)
        self.assertEqual(after["current_step_id"], before["current_step_id"])
        self.assertEqual([row["status"] for row in after["steps"]], [row["status"] for row in before["steps"]])
        self.assertEqual(after["provenance_state"]["attempts"], before["provenance_state"]["attempts"])
        self.assertEqual(result["episode"]["inspections_used"], 1)
        self.assertEqual(result["episode"]["llm_decisions_used"], 0)
        self.assertEqual(result["explanation"]["execution_status"], "not_started")
        self.assertTrue(after["task_state"]["runtime_evidence"]["records"])
        replay = await self.new_service().recover(self.state, request)
        self.assertEqual(replay["status"], "evidence_ready")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(replay["episode"], result["episode"])

    async def test_durable_restore_is_first_and_has_no_probe_or_budget_cost(self):
        record = make_runtime_evidence(self.positive()["audit"], task_id=self.task_id,
            skill_scope=self.subject, observed_at=self.now.isoformat(), producer="runtime_inspection")
        durable = load_plan_state(self.root, self.task_id)
        durable["task_state"]["runtime_evidence"] = runtime_evidence_state(merge_runtime_evidence([], [record], task_id=self.task_id, accepted_at=self.now.isoformat()))
        save_plan_state(self.root, self.task_id, durable)
        request = self.request()
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "evidence_ready")
        self.assertFalse(self.calls)
        self.assertEqual(result["episode"]["actions_used"], 0)

    async def test_conflicting_durable_facts_stop_without_inspection(self):
        self.persist_observations(self.positive("/approved/runtime/first"),
            self.positive("/approved/runtime/second"))
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "internal_error", result)
        self.assertEqual(result["reason_code"], "RUNTIME_EVIDENCE_CONFLICT")
        self.assertEqual(result["per_binding_status"]["runtime.tool.bin"]["availability"], "AMBIGUOUS")
        self.assertEqual(result["episode"]["actions_used"], 0)
        self.assertFalse(self.calls)

    async def test_conflicting_probe_records_stop_after_first_receipt(self):
        rows = [self.positive("/approved/runtime/first"), self.positive("/approved/runtime/second")]
        records = [make_runtime_evidence(row["audit"], task_id=self.task_id,
            skill_scope=self.subject, observed_at=row["observed_at"],
            producer="runtime_inspection", coverage=row["coverage"]) for row in rows]
        self.responses = [{"inspection_status": "succeeded", "records": records}]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "internal_error", result)
        self.assertEqual(result["reason_code"], "RUNTIME_EVIDENCE_CONFLICT")
        self.assertEqual(result["episode"]["inspections_used"], 1)
        self.assertEqual(len(self.calls), 1)

    async def test_not_run_preserves_unknown_and_original_reason(self):
        self.responses = [{"inspection_status": "not_run", "reason_code": "DECLARED_INSPECTION_SAFETY_UNPROVEN",
            "audit": {}, "observed_at": self.now.isoformat(), "coverage": {}}]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "unresolved", result)
        self.assertEqual(result["reason_code"], "DECLARED_INSPECTION_SAFETY_UNPROVEN")
        self.assertEqual(result["per_binding_status"]["runtime.tool.bin"]["availability"], "UNKNOWN")
        self.assertEqual(result["episode"]["receipts"][0]["inspection_status"], "not_run")
        self.assertEqual(len(self.calls), 1)

    async def test_reopened_episode_keeps_original_wall_deadline(self):
        request = self.request()
        self.now += timedelta(seconds=181)
        result = await self.new_service().recover(self.state, request)
        self.assertEqual(result["status"], "unresolved", result)
        self.assertEqual(result["reason_code"], "RUNTIME_RECOVERY_DEADLINE_EXCEEDED")
        self.assertEqual(result["episode"]["termination_reason"], "runtime_recovery_deadline_exceeded")
        self.assertGreaterEqual(self.episode(request)["elapsed_seconds"], 181)
        self.assertFalse(self.calls)
        self.assertEqual(result["per_binding_status"]["runtime.tool.bin"]["availability"], "UNKNOWN")
        repeated_request = self.request()
        self.assertEqual(repeated_request, request)
        replay = await self.new_service().recover(self.state, repeated_request)
        self.assertEqual(replay["reason_code"], result["reason_code"])
        self.assertFalse(self.calls)

    async def test_corrupt_durable_episode_fields_return_protocol_diagnostic(self):
        request = self.request()
        original = self.episode(request)
        for field in ("policy", "elapsed_seconds", "actions_used", "started_at", "request"):
            with self.subTest(field=field):
                durable = load_plan_state(self.root, self.task_id)
                corrupt = copy.deepcopy(original)
                corrupt.pop(field)
                durable["task_state"]["runtime_recovery"]["episodes"][request["episode_id"]] = corrupt
                save_plan_state(self.root, self.task_id, durable)
                result = await self.new_service().recover(self.state, request)
                self.assertEqual(result["status"], "internal_error", result)
                self.assertEqual(result["explanation"]["failure_class"], "internal_protocol")
                self.assertTrue(result["explanation"]["user_summary"])
        self.assertFalse(self.calls)

    async def test_ready_acceptance_archives_only_its_own_stop_diagnostic(self):
        request = self.request()
        diagnostic = {"episode_id": request["episode_id"], "status": "unresolved", "reason_code": "EARLIER_TIMEOUT"}
        durable = load_plan_state(self.root, self.task_id)
        durable["task_state"]["runtime_recovery_result"] = diagnostic
        save_plan_state(self.root, self.task_id, durable)
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "evidence_ready", result)
        task = load_plan_state(self.root, self.task_id)["task_state"]
        self.assertNotIn("runtime_recovery_result", task)
        self.assertEqual(task["runtime_recovery_diagnostics"][request["episode_id"]], diagnostic)
        # A late report for the same recovered episode is cleared on replay.
        durable = load_plan_state(self.root, self.task_id)
        durable["task_state"]["runtime_recovery_result"] = diagnostic
        save_plan_state(self.root, self.task_id, durable)
        replay = await self.new_service().recover(self.state, request)
        self.assertEqual(replay["status"], "evidence_ready")
        self.assertNotIn("runtime_recovery_result", load_plan_state(self.root, self.task_id)["task_state"])
        self.assertEqual(len(self.calls), 1)
        # A different episode's diagnostic is outside this cleanup authority.
        durable = load_plan_state(self.root, self.task_id)
        other = {"episode_id": "runtime-episode:different", "status": "unresolved"}
        durable["task_state"]["runtime_recovery_result"] = other
        save_plan_state(self.root, self.task_id, durable)
        await self.new_service().recover(self.state, request)
        self.assertEqual(load_plan_state(self.root, self.task_id)["task_state"]["runtime_recovery_result"], other)

    async def test_unknown_timeout_retries_once_and_remains_unknown_after_budget(self):
        self.responses = [{"inspection_status": "timeout", "reason_code": "PROBE_TIMEOUT", "transient": True}] * 2
        request = self.request()
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason_code"], "PROBE_TIMEOUT")
        self.assertEqual(result["per_binding_status"]["runtime.tool.bin"]["availability"], "UNKNOWN")
        self.assertEqual(result["episode"]["inspections_used"], 2)
        self.assertEqual(result["episode"]["transient_retries_used"], 1)
        self.assertEqual(result["explanation"]["failure_class"], "inspection_error")
        repeated_request = self.request()
        replay = await self.new_service().recover(self.state, repeated_request)
        self.assertEqual(request, repeated_request)
        self.assertEqual(replay["status"], "unresolved")
        self.assertEqual(len(self.calls), 2)

    async def test_transient_retry_can_gain_evidence(self):
        self.responses = [{"inspection_status": "timeout", "reason_code": "PROBE_TIMEOUT", "transient": True}, self.positive()]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "evidence_ready")
        self.assertEqual(result["episode"]["inspections_used"], 2)

    async def test_permission_error_and_generic_exit_never_establish_unavailable(self):
        self.responses = [{"inspection_status": "permission_error", "reason_code": "READ_DENIED"}]
        request = self.request()
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["per_binding_status"]["runtime.tool.bin"]["availability"], "UNKNOWN")
        self.assertEqual(len(self.calls), 1)

    async def test_explicit_complete_negative_establishes_dependency_block(self):
        self.responses = [{"inspection_status": "succeeded", "observed_at": self.now.isoformat(),
            "audit": {"skill_id": self.skill_id, "env": "fixture_env", "status": "ready", "resolved_runtime_paths": {}},
            "coverage": {"runtime.tool.bin": {"availability": "UNAVAILABLE", "reason_code": "required_executable_absent", "conclusion_complete": True}}}]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "dependency_blocked", result)
        self.assertEqual(result["explanation"]["failure_class"], "dependency_unavailable")

    async def test_incomplete_output_has_one_attempt_and_internal_explanation(self):
        self.responses = [{"inspection_status": "succeeded", "audit": {"status": "ready"}}]
        request = self.request()
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason_code"], "RUNTIME_INSPECTION_OBSERVATION_TIME_MISSING")
        self.assertEqual(len(self.calls), 1)

    async def test_missing_producer_or_unproven_safety_never_dispatches(self):
        self.inspection_contract["coverage_binding_ids"] = []
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason_code"], "RUNTIME_INSPECTION_PRODUCER_MISSING")
        self.assertEqual(len(self.calls), 0)

    async def test_safety_denied_contract_returns_truthful_unresolved(self):
        self.inspection_contract.update(permitted=False, reason_code="RUNTIME_INSPECTION_SAFETY_UNPROVEN")
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason_code"], "RUNTIME_INSPECTION_SAFETY_UNPROVEN")
        self.assertFalse(self.calls)

    async def test_method_digest_and_complete_inspection_contract_have_distinct_identity(self):
        self.inspection_contract["method_digest"] = self.subject["inspection_contract_digest"]
        self.inspection_contract["contract_digest"] = digest({"method": "probe", "roots": ["/approved"]})
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "evidence_ready", result)

    async def test_changed_inspection_authorization_rejects_late_result(self):
        self.inspection_contract["method_digest"] = self.subject["inspection_contract_digest"]
        async def changed(_request):
            self.inspection_contract["contract_digest"] = digest({"method": "probe", "roots": ["/different"]})
            return self.positive()
        self.responses = [changed]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "internal_error")
        self.assertEqual(result["reason_code"], "RUNTIME_RECOVERY_INSPECTION_CONTRACT_CHANGED")
        self.assertFalse(load_plan_state(self.root, self.task_id)["task_state"].get("runtime_evidence"))

    async def test_current_approval_reference_drift_rejects_late_acceptance(self):
        durable = load_plan_state(self.root, self.task_id)
        durable["current_approval"] = {"approval_id": "approval:one",
            "approval_record_ref": "approval-record:one", "plan_snapshot_ref": "plan-snapshot:one", "status": "approved"}
        save_plan_state(self.root, self.task_id, durable)
        async def changed(_request):
            current = load_plan_state(self.root, self.task_id)
            current["current_approval"]["approval_record_ref"] = "approval-record:two"
            save_plan_state(self.root, self.task_id, current)
            return self.positive()
        self.responses = [changed]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "internal_error", result)
        self.assertEqual(result["reason_code"], "RUNTIME_RECOVERY_SCOPE_CHANGED")
        self.assertFalse(load_plan_state(self.root, self.task_id)["task_state"].get("runtime_evidence"))

    async def test_top_level_approval_record_drift_prevents_dispatch(self):
        durable = load_plan_state(self.root, self.task_id)
        durable["approval_record_ref"] = "approval-record:one"
        save_plan_state(self.root, self.task_id, durable)
        request = self.request()
        durable = load_plan_state(self.root, self.task_id)
        durable["approval_record_ref"] = "approval-record:two"
        save_plan_state(self.root, self.task_id, durable)
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "internal_error", result)
        self.assertEqual(result["reason_code"], "RUNTIME_RECOVERY_SCOPE_CHANGED")
        self.assertFalse(self.calls)

    async def test_scope_changed_during_probe_rejects_late_acceptance(self):
        async def changed(_request):
            self.subject["environment_fingerprint"] = digest("replaced-environment")
            return self.positive()
        self.responses = [changed]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "internal_error")
        self.assertEqual(result["reason_code"], "RUNTIME_RECOVERY_SCOPE_CHANGED")
        durable = load_plan_state(self.root, self.task_id)
        self.assertFalse(durable["task_state"].get("runtime_evidence"))

    async def test_unrelated_durable_change_survives_result_acceptance(self):
        async def changed(_request):
            durable = load_plan_state(self.root, self.task_id)
            durable["task_state"]["unrelated_user_note"] = "preserve this note"
            save_plan_state(self.root, self.task_id, durable)
            return self.positive()
        self.responses = [changed]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "evidence_ready")
        self.assertEqual(load_plan_state(self.root, self.task_id)["task_state"]["unrelated_user_note"], "preserve this note")

    async def test_changed_request_id_does_not_authorize_or_reset_budget(self):
        request = self.request()
        forged = copy.deepcopy(request)
        forged["request_id"] = "model-invented-id"
        result = await self.service.recover(self.state, forged)
        self.assertEqual(result["status"], "internal_error")
        self.assertEqual(result["reason_code"], "RUNTIME_RECOVERY_REQUEST_IDENTITY_INVALID")
        self.assertFalse(self.calls)

    async def test_existing_attempt_cannot_be_faked_as_pregeneration(self):
        durable = load_plan_state(self.root, self.task_id)
        durable["provenance_state"]["attempts"]["existing"] = {"attempt_id": "existing", "step_id": durable["current_step_id"], "status": "running"}
        save_plan_state(self.root, self.task_id, durable)
        with self.assertRaisesRegex(RuntimeRecoveryError, "EXECUTION_ALREADY_OWNED"):
            self.request()
        self.assertFalse(self.calls)

    async def test_task_budget_can_stop_inspection_but_not_claim_absence(self):
        durable = load_plan_state(self.root, self.task_id)
        durable["task_state"]["budgets"]["runtime_evidence_actions_remaining"] = 0
        save_plan_state(self.root, self.task_id, durable)
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason_code"], "TASK_RUNTIME_RECOVERY_BUDGET_EXHAUSTED")
        self.assertFalse(self.calls)

    async def test_concurrent_same_episode_singleflight(self):
        async def delayed(_request):
            await asyncio.sleep(0.01)
            return self.positive()
        self.responses = [delayed]
        request = self.request()
        left, right = await asyncio.gather(self.service.recover(self.state, request), self.new_service().recover(self.state, request))
        self.assertEqual(left["status"], "evidence_ready")
        self.assertEqual(right["status"], "evidence_ready")
        self.assertEqual(len(self.calls), 1)

    async def test_dispatch_receipt_exists_before_inspector_runs(self):
        async def observe(request):
            episode = self.episode(request)
            self.assertEqual(episode["receipts"][-1]["status"], "started")
            self.assertEqual(episode["receipts"][-1]["receipt_id"], request["inspection_receipt_id"])
            self.assertEqual(episode["inspections_used"], 1)
            self.assertFalse(load_plan_state(self.root, self.task_id)["task_state"].get("runtime_evidence"))
            return self.positive()
        self.responses = [observe]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "evidence_ready")

    async def test_interrupted_unknown_inspection_waits_then_safely_replays_once(self):
        async def interrupted(_request):
            raise asyncio.CancelledError()
        self.responses = [interrupted]
        request = self.request()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.recover(self.state, request)
        before_lease = await self.new_service().recover(self.state, request)
        self.assertEqual(before_lease["reason_code"], "RUNTIME_INSPECTION_ALREADY_RUNNING")
        self.assertEqual(len(self.calls), 1)
        self.now += timedelta(seconds=2)
        self.responses.append(self.positive())
        replay = await self.new_service().recover(self.state, request)
        self.assertEqual(replay["status"], "evidence_ready", replay)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(replay["episode"]["transient_retries_used"], 1)
        self.assertEqual(replay["episode"]["receipts"][0]["status"], "interrupted_unknown")

    async def test_interrupted_check_not_declared_repeatable_cannot_replay(self):
        self.inspection_contract["safe_repeat"] = False
        async def interrupted(_request):
            raise asyncio.CancelledError()
        self.responses = [interrupted]
        request = self.request()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.recover(self.state, request)
        self.now += timedelta(seconds=2)
        result = await self.new_service().recover(self.state, request)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason_code"], "RUNTIME_INSPECTION_COMPLETION_UNKNOWN")
        self.assertEqual(len(self.calls), 1)

    async def test_interrupted_safe_check_cannot_replay_after_original_deadline(self):
        async def interrupted(_request):
            raise asyncio.CancelledError()
        self.responses = [interrupted]
        request = self.request()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.recover(self.state, request)
        self.now += timedelta(seconds=181)
        result = await self.new_service().recover(self.state, request)
        self.assertEqual(result["status"], "unresolved", result)
        self.assertEqual(result["episode"]["termination_reason"], "runtime_recovery_deadline_exceeded")
        self.assertEqual(result["episode"]["transient_retries_used"], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["per_binding_status"]["runtime.tool.bin"]["availability"], "UNKNOWN")

    async def test_langgraph_control_signal_is_not_an_inspection_failure(self):
        async def interrupted(_request):
            raise GraphBubbleUp("graph-control")
        self.responses = [interrupted]
        request = self.request()
        with self.assertRaises(GraphBubbleUp):
            await self.service.recover(self.state, request)
        self.assertEqual(self.episode(request)["receipts"][0]["status"], "started")
        self.assertEqual(len(self.calls), 1)

    async def test_real_storage_loss_cannot_return_evidence_ready(self):
        from agent_core.plan_tracker import plan_state_path
        path = plan_state_path(self.root, self.task_id)
        async def remove_store(_request):
            path.unlink()
            path.mkdir()
            return self.positive()
        self.responses = [remove_store]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "internal_error")
        self.assertEqual(result["runtime_audit"], [])
        self.assertTrue(result["explanation"]["user_summary"])

    async def test_actionable_user_decision_stays_separate_from_dependency_block(self):
        self.responses = [{"inspection_status": "permission_error", "reason_code": "SCOPE_APPROVAL_REQUIRED", "needs_user_decision": {"kind": "runtime_scope_permission", "question": "Authorize inspection of the declared execution environment?"}}]
        result = await self.service.recover(self.state, self.request())
        self.assertEqual(result["status"], "needs_user_decision")
        self.assertEqual(result["episode"]["user_decision"]["kind"], "runtime_scope_permission")
        self.assertTrue(result["explanation"]["requires_user_input"])

    async def test_answered_question_without_materialized_scope_change_cannot_repeat(self):
        self.responses = [{"inspection_status": "permission_error", "reason_code": "SCOPE_APPROVAL_REQUIRED",
            "needs_user_decision": {"kind": "runtime_scope_permission", "question": "Authorize inspection of the declared execution environment?"}}]
        request = self.request()
        result = await self.service.recover(self.state, request)
        self.assertEqual(result["status"], "needs_user_decision")
        store = StateContractStore(self.root, self.task_id)

        def append_answer(question_id):
            durable = load_plan_state(self.root, self.task_id)
            index, _ = store.append_conversation_turn(durable.get("conversation_evidence") or {},
                role="user", content="Use the declared environment.", question_id=question_id)
            durable["conversation_evidence"] = index
            save_plan_state(self.root, self.task_id, durable)

        append_answer("runtime-question:another-episode")
        unrelated = await self.new_service().recover(self.state, request)
        self.assertEqual(unrelated["status"], "needs_user_decision")
        append_answer("runtime-question:" + request["episode_id"])
        answered = await self.new_service().recover(self.state, request)
        self.assertEqual(answered["status"], "unresolved", answered)
        self.assertEqual(answered["reason_code"], "RUNTIME_USER_ANSWER_WITHOUT_SCOPE_CHANGE")
        self.assertFalse(answered["explanation"]["requires_user_input"])
        self.assertIn("已收到", answered["explanation"]["user_summary"])
        self.assertEqual(answered["per_binding_status"]["runtime.tool.bin"]["availability"], "UNKNOWN")
        self.assertEqual(answered["episode"]["inspections_used"], 1)
        replay = await self.new_service().recover(self.state, request)
        self.assertEqual(replay["status"], "unresolved")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()

"""Old runtime diagnostics must not take ownership from a newer scope/attempt."""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from bio_agents import BioAnalystAgent
from agent_core.action_runtime import capability_contract_hash
from agent_core.plan_tracker import build_plan_state, load_plan_state, save_plan_state
from agent_core.runtime_evidence import digest
from agent_core.task_spec import create_task_spec
from agent_core.workflow.runtime_evidence_recovery import RuntimeEvidenceRecovery


class RuntimeRecoveryOuterScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-outer-scope-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root, self.task_id = Path(self.temporary.name), "outer-runtime"
        durable = build_plan_state(self.task_id,
            "- [>] Step 1: Run | Action: EXECUTE | Skills: [fixture/generic] | Done: accepted output\n")
        durable["workflow_routing_schema_version"] = 2
        durable["task_spec"] = create_task_spec("Run the supplied capability.", task_id=self.task_id)
        durable["selected_skill_ids"] = ["fixture/generic"]
        durable["task_state"]["selected_skills"] = ["fixture/generic"]
        save_plan_state(self.root, self.task_id, durable)
        self.subject = {"skill_id": "fixture/generic", "skill_document_digest": digest("doc"),
            "skill_contract_digest": digest("contract"), "environment_id": "testenv",
            "environment_fingerprint": digest("env"), "host_id": "host-test",
            "inspection_contract_digest": digest("inspection")}
        self.contract = {"capability_id": "fixture.run", "selected_skill_id": "fixture/generic",
            "required_runtime_binding_ids": ["runtime.widget.bin"], "optional_runtime_binding_ids": [],
            "required_artifact_binding_ids": [], "execution_guidance": []}
        async def forbidden_probe(_request):
            raise AssertionError("Outer-result scope checks must not launch inspection")
        self.service = RuntimeEvidenceRecovery(data_path=str(self.root), scope_provider=lambda _skill: deepcopy(self.subject), inspect_runtime=forbidden_probe,
            inspection_contract_provider=lambda _skill: {"contract_id": "fixture-inspection", "contract_digest": digest("inspection"),
                "permitted": False, "safe_repeat": True, "coverage_binding_ids": ["runtime.widget.bin"], "deadline_seconds": 1,
                "reason_code": "SAFE_PRODUCER_UNAVAILABLE", "cache_restore_permitted": False})
        self.request = self.service.create_request({"task_id": self.task_id}, selected_skill_id="fixture/generic",
            capability_contract=self.contract, missing_binding_ids=["runtime.widget.bin"],
            original_error={"code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"})
        self.agent = BioAnalystAgent.__new__(BioAnalystAgent)
        self.agent.data_path = str(self.root)
        self.diagnostic = {"schema_version": 1, "producer": "runtime_recovery_reporting_v1", "episode_id": self.request["episode_id"], "scope": {key: self.request["scope"][key] for key in ("task_id", "plan_id", "plan_version", "step_id")},
            "stage": "runtime_evidence_recovery", "resolution_disposition": "unresolved", "reason_code": "OLD_MISSING_EVIDENCE",
            "explanation": "old runtime diagnostic", "execution_status": "not_dispatched", "original_error": {"error_code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"}}
        current = load_plan_state(self.root, self.task_id)
        current["task_state"]["runtime_recovery"]["episodes"][self.request["episode_id"]].update(status="unresolved", reason_code="OLD_MISSING_EVIDENCE")
        current["task_state"]["runtime_recovery_result"] = deepcopy(self.diagnostic)
        save_plan_state(self.root, self.task_id, current)

    def test_old_graph_diagnostic_is_ignored_for_new_scope(self):
        current = load_plan_state(self.root, self.task_id)
        current["plan_id"] = "new-plan"
        current["plan_version"] = "new-version"
        current["current_step_id"] = "new-step"
        current["task_state"]["runtime_recovery"]["active_episode_id"] = "new-episode"
        value = self.agent._runtime_recovery_diagnostic({"runtime_recovery_result": self.diagnostic}, current)
        self.assertEqual(value, {})

    def test_ready_episode_ignores_old_graph_and_durable_stop(self):
        current = load_plan_state(self.root, self.task_id)
        current["task_state"]["runtime_recovery"]["episodes"][self.request["episode_id"]]["status"] = "evidence_ready"
        self.assertEqual(self.agent._runtime_recovery_diagnostic({}, current), {})
        self.assertEqual(self.agent._runtime_recovery_diagnostic({"runtime_recovery_result": self.diagnostic}, current), {})

    def test_matching_terminal_diagnostic_remains_available(self):
        current = load_plan_state(self.root, self.task_id)
        self.assertEqual(self.agent._runtime_recovery_diagnostic({}, current)["reason_code"], "OLD_MISSING_EVIDENCE")
        self.assertEqual(self.agent._runtime_recovery_diagnostic({"runtime_recovery_result": self.diagnostic}, current)["reason_code"], "OLD_MISSING_EVIDENCE")

    def test_scope_mismatched_stop_preserves_new_pending_action(self):
        current = load_plan_state(self.root, self.task_id)
        current["task_state"]["runtime_recovery"]["active_episode_id"] = "new-episode"
        save_plan_state(self.root, self.task_id, current)
        new_action = {"type": "execute", "action_id": "new-action", "step_id": "new-step"}
        before = {"task_id": self.task_id, "pending_action": new_action, "execution_started": False,
            "pending_runtime_evidence_request": self.request}
        update = self.agent._runtime_recovery_stop(before, {"status": "internal_error", "request": self.request, "reason_code": "RUNTIME_RECOVERY_SCOPE_CHANGED"})
        merged = {**before, **update}
        self.assertEqual(merged["pending_action"], new_action)
        self.assertEqual(merged["execution_started"], False)

    def test_existing_attempt_stop_preserves_running_execution_owner(self):
        current = load_plan_state(self.root, self.task_id)
        attempt_id = "existing-current-attempt"
        current["provenance_state"]["attempts"][attempt_id] = {"attempt_id": attempt_id,
            "step_id": self.request["scope"]["step_id"], "status": "RUNNING", "execution_record_id": "real-existing-execution"}
        save_plan_state(self.root, self.task_id, current)
        verified = load_plan_state(self.root, self.task_id)
        self.assertIn(attempt_id, verified["provenance_state"]["attempts"])
        action = {"type": "execute", "action_id": "existing-action", "step_id": self.request["scope"]["step_id"]}
        before = {"task_id": self.task_id, "pending_action": action, "execution_started": True,
            "pending_runtime_evidence_request": self.request}
        update = self.agent._runtime_recovery_stop(before, {"status": "internal_error", "request": self.request, "reason_code": "RUNTIME_RECOVERY_EXECUTION_ALREADY_OWNED"})
        merged = {**before, **update}
        self.assertEqual(merged["pending_action"], action)
        self.assertTrue(merged["execution_started"])
        self.assertIn(attempt_id, load_plan_state(self.root, self.task_id)["provenance_state"]["attempts"])

    def test_fallback_report_scope_is_projected_from_real_request(self):
        result = self.agent._runtime_recovery_stop({"task_id": self.task_id},
            {"status": "internal_error", "request": self.request, "reason_code": "RUNTIME_RECOVERY_STORAGE_OR_PROTOCOL_FAILURE", "episode": {"failure_class": "internal_configuration"}})
        scope = result["runtime_recovery_result"]["scope"]
        self.assertEqual(scope, {key: self.request["scope"][key] for key in ("task_id", "plan_id", "plan_version", "step_id")})
        self.assertEqual(result["runtime_recovery_result"]["failure_class"], "internal_configuration")


if __name__ == "__main__":
    unittest.main()

"""Offline reporting checks with real durable/CAS state and temporary files."""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent_core.action_runtime import capability_contract_hash
from agent_core.plan_tracker import approve_plan_state, build_plan_state, load_plan_state, save_plan_state
from agent_core.runtime_evidence import digest
from agent_core.task_spec import create_task_spec
from agent_core.workflow.runtime_evidence_recovery import RuntimeEvidenceRecovery, runtime_authorization_scope_digest
from agent_core.workflow.runtime_recovery_reporting import finalize_runtime_recovery_result


class RuntimeRecoveryReportingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-reporting-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root, self.task_id = Path(self.temporary.name), "report-test"
        self.task_root = self.root / "task_outputs" / self.task_id
        self.subject = {"skill_id": "fixture/generic", "skill_document_digest": digest("doc"),
            "skill_contract_digest": digest("contract"), "environment_id": "testenv",
            "environment_fingerprint": digest("environment"), "host_id": "host-test",
            "inspection_contract_digest": digest("check")}
        self.contract = {"capability_id": "fixture.run", "selected_skill_id": "fixture/generic",
            "required_runtime_binding_ids": ["runtime.widget.bin"], "optional_runtime_binding_ids": [],
            "required_artifact_binding_ids": [], "execution_guidance": []}
        self.probe_result = {"inspection_status": "output_invalid", "reason_code": "DECLARED_OUTPUT_INCOMPLETE", "transient": False}
        self.calls = 0
        self.outputs = ["report.md", "reproduce.sh", "stage_status.tsv"]

    def initialize(self, outputs=None, *, input_paths=(), approve=False):
        durable = build_plan_state(self.task_id,
            "- [x] Step 1: Prepare | Action: EXECUTE | Skills: [fixture/generic] | Done: prepare accepted\n"
            "- [>] Step 2: Annotate | Action: EXECUTE | Skills: [fixture/generic] | Done: verified annotation\n")
        durable["workflow_routing_schema_version"] = 2
        durable["task_spec"] = create_task_spec("Prepare then annotate; return requested status documents.", task_id=self.task_id,
            structured_source={"question": "Prepare then annotate; return requested status documents.",
                "contract_source_type": "user_explicit", "expected_outputs": self.outputs if outputs is None else outputs,
                "input_files": list(input_paths)})
        durable["selected_skill_ids"] = ["fixture/generic"]
        durable["task_state"]["selected_skills"] = ["fixture/generic"]
        self.prepare_id = durable["steps"][0]["step_id"]
        self.current_id = durable["current_step_id"]
        for row in durable["steps"]:
            if row["step_id"] == self.current_id:
                row.update(execution_capability_id="fixture.run", capability_contract_hash=capability_contract_hash(self.contract))
        self.task_root.mkdir(parents=True)
        script = self.task_root / "step_prepare.sh"
        script.write_text("# accepted fixture script reference; never read or executed by report\n")
        artifact = self.task_root / "prepared.txt"
        artifact.write_text("accepted preparation data\n")
        metadata = artifact.stat()
        durable["provenance_state"]["attempts"]["attempt_prepare"] = {
            "attempt_id": "attempt_prepare", "step_id": self.prepare_id, "status": "COMPLETED",
            "returncode": 0, "execution_record_id": "execution-prepare", "executed_script_path": str(script),
            "validation_evidence_ref": "validation-prepare"}
        durable["provenance_state"]["validations"]["validation-prepare"] = {"status": "valid", "attempt_id": "attempt_prepare"}
        durable["provenance_state"]["artifact_heads"]["prepared"] = "prepared@attempt_prepare"
        durable["provenance_state"]["artifacts"]["prepared@attempt_prepare"] = {
            "artifact_id": "prepared", "artifact_version_id": "prepared@attempt_prepare", "path": str(artifact),
            "producer_step_id": self.prepare_id, "producer_attempt_id": "attempt_prepare", "status": "valid",
            "validation_status": "valid", "validation_id": "validation-prepare",
            "digest_or_signature": {"size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns}}
        save_plan_state(self.root, self.task_id, durable)
        if approve:
            approved = load_plan_state(self.root, self.task_id)
            approved = approve_plan_state(approved, int((approved.get("current_task") or {}).get("task_revision") or approved.get("task_revision") or 0))
            save_plan_state(self.root, self.task_id, approved)

        async def probe(_request):
            self.calls += 1
            return deepcopy(self.probe_result)

        self.service = RuntimeEvidenceRecovery(data_path=str(self.root), scope_provider=lambda _skill: deepcopy(self.subject), inspect_runtime=probe,
            inspection_contract_provider=lambda _skill: {"contract_id": "check", "contract_digest": digest("check"),
                "permitted": True, "safe_repeat": True, "coverage_binding_ids": ["runtime.widget.bin"], "deadline_seconds": 1,
                "cache_restore_permitted": False})
        self.request = self.service.create_request({"task_id": self.task_id}, selected_skill_id="fixture/generic",
            capability_contract=self.contract, missing_binding_ids=["runtime.widget.bin"], original_error={"code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"})
        self.current_id = self.request["scope"]["step_id"]

    async def recovered(self):
        return await self.service.recover({"task_id": self.task_id}, self.request)

    async def test_unknown_reports_actual_preparation_and_unexecuted_annotation(self):
        self.initialize()
        result = await self.recovered()
        before = load_plan_state(self.root, self.task_id)
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        after = load_plan_state(self.root, self.task_id)
        self.assertEqual(final["resolution_disposition"], "unresolved", final)
        self.assertIn("不能证明依赖不存在", final["explanation"])
        self.assertIn("DECLARED_OUTPUT_INCOMPLETE", final["explanation"])
        self.assertEqual(final["runtime_recovery_result"]["completed_stages"][0]["step_id"], self.prepare_id)
        self.assertEqual(final["runtime_recovery_result"]["unfinished_stages"][0]["status"], "not_dispatched")
        self.assertEqual(len(final["runtime_recovery_result"]["verified_deliverables"]), 1)
        self.assertEqual(set(final["report_paths"]), set(self.outputs), final)
        self.assertEqual(before["steps"], after["steps"])
        self.assertEqual(before["current_step_id"], after["current_step_id"])
        self.assertEqual(before["provenance_state"], after["provenance_state"])
        script = (self.task_root / "reproduce.sh").read_text()
        self.assertIn("# Accepted step", script)
        self.assertIn("# Not completed:", script)
        self.assertTrue(script.endswith("exit 1\n"))
        self.assertNotIn("java", script)
        self.assertIn("explanation", after["task_state"]["runtime_recovery_result"])
        again = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(again["report_paths"], final["report_paths"])
        self.assertEqual(again["secondary_failures"], [])

    async def test_no_requested_reports_produces_only_durable_text(self):
        self.initialize(outputs=[])
        final = finalize_runtime_recovery_result(self.root, self.task_id, await self.recovered())
        self.assertEqual(final["report_paths"], {})
        self.assertFalse((self.task_root / "report.md").exists())
        self.assertIn("原始错误", final["explanation"])
        self.assertEqual(final["runtime_recovery_result"]["summary_llm_calls"], 0)

    async def test_negative_requires_durable_complete_evidence(self):
        self.initialize()
        self.probe_result = {"inspection_status": "succeeded", "observed_at": datetime.now(timezone.utc).isoformat(),
            "audit": {"skill_id": "fixture/generic", "env": "testenv", "status": "ready"},
            "coverage": {"runtime.widget.bin": {"availability": "UNAVAILABLE", "reason_code": "complete_search_absent", "conclusion_complete": True, "value_kind": "executable_path"}}}
        final = finalize_runtime_recovery_result(self.root, self.task_id, await self.recovered())
        self.assertEqual(final["resolution_disposition"], "dependency_blocked", final)
        self.assertIn("有效且完整", final["explanation"])
        self.assertIn("UNAVAILABLE", final["explanation"])

    async def test_fake_negative_and_ann_success_not_reported(self):
        self.initialize()
        result = await self.recovered()
        result["status"] = "dependency_blocked"
        result["explanation"]["completed_stages"] = [{"step_id": self.current_id, "title": "ANN successfully generated"}]
        result["explanation"]["verified_deliverables"] = [{"path": str(self.task_root / "annotated.vcf")}]
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["resolution_disposition"], "unresolved")
        self.assertNotIn("ANN successfully generated", final["explanation"])
        self.assertNotIn("annotated.vcf", final["explanation"])
        self.assertFalse(final["runtime_recovery_result"]["scientific_goal_completed"])

    async def test_report_write_failure_has_no_fake_paths_and_keeps_text(self):
        self.initialize()
        result = await self.recovered()
        with patch("os.link", side_effect=PermissionError("secret filesystem error")):
            final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["report_paths"], {})
        self.assertTrue(any(row["stage"] == "report" for row in final["secondary_failures"]))
        self.assertIn("当前步骤", final["explanation"])
        self.assertNotIn("secret filesystem error", final["explanation"])
        self.assertFalse(list(self.task_root.glob(".runtime-recovery-*.tmp")))

    async def test_persist_failure_returns_full_summary_and_existing_reports(self):
        self.initialize()
        result = await self.recovered()
        with patch("os.replace", side_effect=PermissionError("private persistence error")):
            final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertIn("原始错误", final["explanation"])
        self.assertTrue(any(row["stage"] == "persist" for row in final["secondary_failures"]))
        self.assertTrue(all(Path(value).is_file() for value in final["report_paths"].values()))
        self.assertNotIn("private persistence error", str(final))

    async def test_current_approval_identity_is_shared_then_drift_is_stale(self):
        self.initialize(outputs=[], approve=True)
        approved = load_plan_state(self.root, self.task_id)
        approved["current_approval"] = {**deepcopy(approved["approval_record"]), "approval_record_ref": "approval-record:fixture"}
        save_plan_state(self.root, self.task_id, approved)
        self.request = self.service.create_request({"task_id": self.task_id}, selected_skill_id="fixture/generic",
            capability_contract=self.contract, missing_binding_ids=["runtime.widget.bin"],
            original_error={"code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"})
        result = await self.recovered()
        current = load_plan_state(self.root, self.task_id)
        self.assertTrue(current.get("current_approval"))
        self.assertEqual(self.request["scope"]["authorization_scope_digest"], runtime_authorization_scope_digest(current))
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["resolution_disposition"], "unresolved", final)
        changed = load_plan_state(self.root, self.task_id)
        prior_result = deepcopy(changed["task_state"]["runtime_recovery_result"])
        changed["current_approval"]["approval_record_ref"] = "approval-record:changed"
        save_plan_state(self.root, self.task_id, changed)
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["resolution_disposition"], "stale_scope", final)
        self.assertEqual(load_plan_state(self.root, self.task_id)["task_state"]["runtime_recovery_result"], prior_result)

    async def test_request_creation_persist_failure_has_no_episode_and_no_report_writes(self):
        self.initialize()
        durable = load_plan_state(self.root, self.task_id)
        durable["task_state"].pop("runtime_recovery", None)
        save_plan_state(self.root, self.task_id, durable)
        durable = load_plan_state(self.root, self.task_id)
        before = (self.task_root / "plan_state.json").read_bytes()
        with patch("os.replace", side_effect=PermissionError("fixture request persistence failure")):
            with self.assertRaises(PermissionError):
                self.service.create_request({"task_id": self.task_id}, selected_skill_id="fixture/generic",
                    capability_contract=self.contract, missing_binding_ids=["runtime.widget.bin"],
                    original_error={"code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"})
        result = {"status": "internal_error", "reason_code": "RUNTIME_RECOVERY_REQUEST_PERSISTENCE_FAILED",
            "request": {"scope": {key: durable[field] for key, field in (("task_id", "task_id"), ("plan_id", "plan_id"),
                ("plan_version", "plan_version"), ("step_id", "current_step_id"))},
                "original_error_code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"}}
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["resolution_disposition"], "internal_error", final)
        self.assertEqual(final["runtime_recovery_result"]["request_status"], "unrecorded_request")
        self.assertEqual(final["reason_code"], result["reason_code"])
        self.assertNotIn("范围已经变化", final["explanation"])
        self.assertIn("恢复请求未能持久保存", final["explanation"])
        self.assertNotIn("RUNTIME_REPORT_SCOPE_CHANGED", str(final))
        self.assertEqual(final["report_paths"], {})
        self.assertFalse((self.task_root / "report.md").exists())
        self.assertEqual((self.task_root / "plan_state.json").read_bytes(), before)

    async def test_unrecorded_request_does_not_replace_existing_active_episode(self):
        self.initialize()
        result = await self.recovered()
        prior = finalize_runtime_recovery_result(self.root, self.task_id, result)
        before = (self.task_root / "plan_state.json").read_bytes()
        result = {"status": "internal_error", "reason_code": "RUNTIME_RECOVERY_REQUEST_PERSISTENCE_FAILED",
            "request": {"scope": {key: self.request["scope"][key] for key in ("task_id", "plan_id", "plan_version", "step_id")},
                "original_error_code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"}}
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["resolution_disposition"], "internal_error")
        self.assertEqual(final["runtime_recovery_result"]["request_status"], "unrecorded_request")
        self.assertEqual(final["report_paths"], {})
        self.assertEqual((self.task_root / "plan_state.json").read_bytes(), before)
        self.assertEqual(load_plan_state(self.root, self.task_id)["task_state"]["runtime_recovery_result"], prior["runtime_recovery_result"])

    async def test_answer_without_effective_change_preserves_class_and_next_condition(self):
        self.initialize(outputs=[])
        result = await self.recovered()
        result["reason_code"] = "RUNTIME_USER_ANSWER_WITHOUT_SCOPE_CHANGE"
        result["episode"]["failure_class"] = "authorization_unresolved"
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["runtime_recovery_result"]["failure_class"], "authorization_unresolved")
        self.assertEqual(final["resolution_disposition"], "unresolved")
        self.assertIn("已收到用户答复", final["explanation"])
        self.assertIn("有效授权范围尚未变化", final["explanation"])

    async def test_unknown_existing_file_and_symlink_are_protected(self):
        self.initialize()
        report = self.task_root / "report.md"
        report.write_text("preexisting user report\n")
        external = self.root / "input.scientific"
        external.write_text("immutable input\n")
        (self.task_root / "reproduce.sh").symlink_to(external)
        final = finalize_runtime_recovery_result(self.root, self.task_id, await self.recovered())
        self.assertEqual(report.read_text(), "preexisting user report\n")
        self.assertEqual(external.read_text(), "immutable input\n")
        self.assertNotIn("report.md", final["report_paths"])
        self.assertNotIn("reproduce.sh", final["report_paths"])

    async def test_explicit_output_needs_approval_and_cannot_escape_via_symlink(self):
        output = self.root / "requested" / "report.md"
        self.initialize(outputs=[str(output)])
        final = finalize_runtime_recovery_result(self.root, self.task_id, await self.recovered())
        self.assertEqual(final["report_paths"], {})
        self.assertFalse(output.exists())

    async def test_approved_exact_report_output_and_symlink_parent(self):
        outside = self.root / "approved-report-dir"
        outside.mkdir()
        output = outside / "report.md"
        self.initialize(outputs=[str(output)], approve=True)
        result = await self.recovered()
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["report_paths"], {"report.md": str(output)}, final)
        self.assertIn("科学目标完成：否", output.read_text())
        output.unlink()
        outside.rmdir()
        destination = self.root / "not-authorized"
        destination.mkdir()
        outside.symlink_to(destination, target_is_directory=True)
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["report_paths"], {})
        self.assertFalse((destination / "report.md").exists())

    async def test_artifact_changed_after_acceptance_is_not_listed(self):
        self.initialize()
        (self.task_root / "prepared.txt").write_text("changed after prior accepted observation\n")
        final = finalize_runtime_recovery_result(self.root, self.task_id, await self.recovered())
        self.assertEqual(final["runtime_recovery_result"]["verified_deliverables"], [])
        self.assertIn("暂无可核验交付物", final["explanation"])

    async def test_stale_scope_only_records_old_episode_diagnostics(self):
        self.initialize()
        result = await self.recovered()
        changed = load_plan_state(self.root, self.task_id)
        changed["task_state"]["runtime_recovery"]["active_episode_id"] = "new-episode"
        changed["task_state"]["runtime_recovery_result"] = {"episode_id": "new-episode", "message": "preserve"}
        save_plan_state(self.root, self.task_id, changed)
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        durable = load_plan_state(self.root, self.task_id)
        self.assertEqual(final["resolution_disposition"], "stale_scope")
        self.assertEqual(final["report_paths"], {})
        self.assertEqual(durable["task_state"]["runtime_recovery_result"], changed["task_state"]["runtime_recovery_result"])
        self.assertIn(self.request["episode_id"], durable["task_state"]["runtime_recovery_diagnostics"])

    async def test_current_execution_record_is_never_labeled_not_dispatched(self):
        self.initialize(outputs=[])
        result = await self.recovered()
        changed = load_plan_state(self.root, self.task_id)
        changed["provenance_state"]["attempts"]["late-current-attempt"] = {
            "attempt_id": "late-current-attempt", "step_id": self.current_id, "status": "RUNNING", "execution_record_id": "late-execution"}
        save_plan_state(self.root, self.task_id, changed)
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["execution_status"], "existing_execution_record", final)
        self.assertEqual(final["runtime_recovery_result"]["unfinished_stages"][0]["status"], "executed_unverified")

    async def test_missing_durable_state_still_has_all_top_level_diagnostics(self):
        final = finalize_runtime_recovery_result(self.root, self.task_id,
            {"status": "internal_error", "reason_code": "STATE_READ_FAILED", "request": {"original_error_code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"}})
        for key in ("reason_code", "resolution_disposition", "explanation", "execution_status", "original_error", "stage"):
            self.assertTrue(final[key])
        self.assertEqual(final["report_paths"], {})
        self.assertFalse((self.task_root / "plan_state.json").exists())

    async def test_needs_user_requires_a_concrete_durable_question(self):
        self.initialize()
        result = await self.recovered()
        result["status"] = "needs_user_decision"
        final = finalize_runtime_recovery_result(self.root, self.task_id, result)
        self.assertEqual(final["resolution_disposition"], "unresolved")
        self.assertNotIn("user_question", final["runtime_recovery_result"])

    async def test_concrete_durable_question_is_preserved_without_inventing_one(self):
        self.initialize(outputs=[])
        self.probe_result = {"inspection_status": "succeeded", "observed_at": datetime.now(timezone.utc).isoformat(),
            "audit": {"skill_id": "fixture/generic", "env": "testenv", "status": "ready"},
            "coverage": {"runtime.widget.bin": {"availability": "UNKNOWN", "value_kind": "executable_path"}},
            "needs_user_decision": {"question": "允许读取已声明的运行工具目录以验证路径吗？", "kind": "runtime_read_permission"}}
        final = finalize_runtime_recovery_result(self.root, self.task_id, await self.recovered())
        self.assertEqual(final["resolution_disposition"], "needs_user_decision", final)
        self.assertEqual(final["runtime_recovery_result"]["user_question"], self.probe_result["needs_user_decision"]["question"])
        self.assertIn("未回答前不执行", final["explanation"])


if __name__ == "__main__":
    unittest.main()

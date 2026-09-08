"""Scoped inspection terminal tests with real temporary PlanTracker/CAS/files."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core.plan_tracker import approve_plan_state, build_plan_state, load_plan_state, save_plan_state
from agent_core.task_spec import create_task_spec
from agent_core.workflow.inspection_stop import (
    build_inspection_stop, current_inspection_stop, finalize_inspection_stop,
    inspection_stop_outcome, inspection_stop_scope, inspection_terminal_projection,
)
from agent_core.workflow.outcomes import RecoveryOutcome
from execution.profiling import RunProfiler


class InspectionStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inspection-stop-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task_id = "inspection-test"

    def fixture(self, *, outputs=None, materializations=None, approve=False):
        self.fixture_count = getattr(self, "fixture_count", 0) + 1
        self.task_id = "inspection-test-" + str(self.fixture_count)
        state = build_plan_state(self.task_id,
            "- [>] Step 1: Check input compatibility | Action: EXECUTE | Skills: [] | Done: verified compatibility\n"
            "- [ ] Step 2: Annotation | Action: EXECUTE | Skills: [] | Done: genuine scientific output\n")
        state["task_spec"] = create_task_spec("Inspect compatibility then annotate; return status reports.", task_id=self.task_id,
            structured_source={"question": "Inspect compatibility then annotate; return status reports.",
                "contract_source_type": "user_explicit", "expected_outputs":
                ["report.md", "reproduce.sh", "stage_status.tsv"] if outputs is None else outputs})
        self.step_id = state["steps"][0]["step_id"]
        state["current_step_id"] = self.step_id
        state["steps"][0].update(status="verification_pending", verification_attempt_id="attempt-one")
        state["task_state"]["verification_pending"] = {"step_id": self.step_id, "attempt_id": "attempt-one"}
        state["task_state"]["workflow_routing_schema_version"] = 2
        source = {"type": "inspection_required", "payload": {
            "contract_violations": ["Method ownership remains uncertain; inspect the independent receipt."],
            "completion_report": {"effective_disposition": "inspection_required", "deterministic_veto": False,
                "completion_gate": {"passed": True}, "process_outcome": {"returncode": 0,
                    "validation_status": "VALID", "execution_status": "succeeded"}}}}
        packet = {"task_id": self.task_id, "step_id": self.step_id, "attempt_ref": "attempt-one",
            "failure_id": "failure-one", "failure_type": "inspection_required", "failure_owner": "semantic_review",
            "error_code": "VERIFICATION_FAILED", "summary": "Method concern requires inspection.",
            "evidence_refs": ["validation-one", "semantic-one"]}
        state["task_state"]["active_failure_packet_ref"] = "packet-one"
        state["task_state"]["causal_failure_packets"] = {"packet-one": {
            "source_kind": "verification_outcome", "source_fingerprint": "episode-one",
            "failure_packet": packet, "failure_source": source}}
        state["provenance_state"]["attempts"]["attempt-one"] = {"attempt_id": "attempt-one", "step_id": self.step_id,
            "returncode": 0, "status": "COMPLETED", "executed_script_path": str(self.root / "executed-evidence.sh")}
        if materializations:
            state["task_output_materializations"] = deepcopy(materializations)
            state["steps"][-1]["outputs"] = [row["output_ref"] for row in materializations]
        save_plan_state(self.root, self.task_id, state)
        state = load_plan_state(self.root, self.task_id)
        if approve:
            state = approve_plan_state(state, int((state.get("current_task") or {}).get("task_revision") or 0))
            save_plan_state(self.root, self.task_id, state)
            state = load_plan_state(self.root, self.task_id)
        seed = {"seed_id": "recovery-seed-episode-one", "failure_packet": packet,
            "plan_spec_ref": {"plan_id": state["plan_id"], "plan_version": state["plan_version"]}}
        receipts = [{"request_id": f"tool-{n}", "tool": tool, "source_ref": ref,
            "error": {"code": code, "detail": ref}}
            for n, (tool, ref, code) in enumerate([
                ("failure.inspect", "failure-one", "INSPECTION_REF_NOT_DECLARED"),
                ("step.inspect", self.step_id, "INSPECTION_REF_NOT_DECLARED"),
                ("evidence.read", "validation-one", "INSPECTION_REF_NOT_RESOLVABLE"),
                ("evidence.read", "semantic-one", "INSPECTION_REF_NOT_RESOLVABLE")], 1)]
        episode = {"terminal_error": {"code": "RECOVERY_NO_PROGRESS_LIMIT", "detail": "2"},
            "audit": {"no_progress_rounds": 2, "turn_count": 2, "tool_call_count": 4},
            "_observation_receipts": receipts}
        stop = build_inspection_stop(seed=seed, durable=state, source=source, episode=episode,
            archive_ref="phase-archive:sha256:fixture")
        self.seed, self.source, self.episode = seed, source, episode
        return state, stop

    def test_last_round_failures_and_causal_layers_survive_typed_profiler_reload(self):
        before, stop = self.fixture()
        final = finalize_inspection_stop(self.root, stop)
        after = load_plan_state(self.root, self.task_id)
        self.assertEqual(after["task_state"]["status"], "blocked")
        self.assertEqual(before["steps"], after["steps"])
        self.assertEqual(before["provenance_state"], after["provenance_state"])
        self.assertEqual(before["current_step_id"], after["current_step_id"])
        self.assertEqual(len(final["inspection_failures"]), 4)
        self.assertEqual(final["inspection_failures"][-1]["tool_call_id"], "tool-4")
        self.assertIn("semantic-one", final["explanation"])
        self.assertTrue(final["execution"]["deterministic_passed"])
        self.assertFalse(final["execution"]["committed"])
        outcome = inspection_stop_outcome(final).to_dict()
        RecoveryOutcome.from_mapping(outcome)
        projection = inspection_terminal_projection({"recovery_outcome": outcome}, after)
        self.assertEqual(projection["terminal_diagnostic"]["error_code"], "RECOVERY_NO_PROGRESS_LIMIT")
        self.assertEqual(projection["terminal_diagnostic"]["stage"], "recovery.inspection")
        profiler = RunProfiler(self.root / "task_outputs")
        profiler.finish_task(self.task_id, projection["status"], terminal_projection=projection)
        import json
        metrics = json.loads((self.root / "task_outputs" / self.task_id / "run_metrics.json").read_text())
        self.assertEqual(metrics["final_result"]["terminal_diagnostic"]["error_code"], "RECOVERY_NO_PROGRESS_LIMIT")
        self.assertEqual(inspection_terminal_projection({}, after), projection)

    def test_reports_exist_true_step_status_reproduce_is_inert_and_idempotent(self):
        before, stop = self.fixture()
        first = finalize_inspection_stop(self.root, stop)
        self.assertEqual(set(first["report_paths"]), {"report.md", "reproduce.sh", "stage_status.tsv"})
        for path in first["report_paths"].values():
            self.assertTrue(Path(path).is_file())
        script = Path(first["report_paths"]["reproduce.sh"]).read_text()
        self.assertTrue(script.endswith("exit 1\n"))
        self.assertIn("Exact executed command body is not available", script)
        self.assertFalse(any(line and not line.startswith("#") and line != "exit 1" for line in script.splitlines()))
        statuses = Path(first["report_paths"]["stage_status.tsv"]).read_text()
        self.assertIn("verification_pending", statuses)
        self.assertIn("\tpending\t", statuses)
        self.assertIn("run\tblocked", statuses)
        generation = load_plan_state(self.root, self.task_id)["state_generation"]
        second = finalize_inspection_stop(self.root, stop)
        self.assertEqual(second, first)
        self.assertEqual(load_plan_state(self.root, self.task_id)["state_generation"], generation)
        self.assertEqual(current_inspection_stop(load_plan_state(self.root, self.task_id),
            scope=inspection_stop_scope(self.seed)), first)

    def test_same_scope_nonsemantic_generation_advance_is_allowed_but_cas_is_real(self):
        _before, stop = self.fixture(outputs=[])
        fresh = load_plan_state(self.root, self.task_id)
        fresh["task_state"]["inspection_read_telemetry"] = {"read_count": 4}
        save_plan_state(self.root, self.task_id, fresh)
        expected = load_plan_state(self.root, self.task_id)["state_generation"]
        final = finalize_inspection_stop(self.root, stop)
        self.assertEqual(final["persistence_status"], "committed")
        self.assertEqual(final["commit_expected_state_generation"], expected)

    def test_policy_no_progress_without_tool_fault_does_not_claim_corruption(self):
        durable, _ = self.fixture(outputs=[])
        self.episode["_observation_receipts"] = []
        stop = build_inspection_stop(seed=self.seed, durable=durable, source=self.source, episode=self.episode)
        self.assertEqual(stop["technical_classification"], "inspection_unresolved")
        self.assertEqual(inspection_stop_outcome(stop).type, "blocked")
        self.assertIn("没有已记录的工具读取错误", stop["explanation"])
        self.assertNotIn("系统损坏", stop["explanation"])

    def test_writer_failure_and_unknown_existing_files_keep_primary_and_text(self):
        _before, stop = self.fixture()
        target = self.root / "task_outputs" / self.task_id / "report.md"
        target.write_text("Other session owns this file.\n")
        final = finalize_inspection_stop(self.root, stop)
        self.assertNotIn("report.md", final["report_paths"])
        self.assertEqual(target.read_text(), "Other session owns this file.\n")
        self.assertIn("RUNTIME_REPORT_EXISTING_FILE_PROTECTED", final["explanation"])
        self.assertEqual(final["stop_reason"]["error_code"], "RECOVERY_NO_PROGRESS_LIMIT")
        self.assertEqual(len(final["report_paths"]), 2)

    def test_store_failure_returns_inmemory_projection_without_reader_or_writer(self):
        before, stop = self.fixture()
        def no_store(*args, **kwargs):
            raise PermissionError("secret token should not be shown")
        with patch("agent_core.workflow.inspection_stop._write_report", side_effect=AssertionError("must not write")):
            final = finalize_inspection_stop(self.root, stop, saver=no_store)
        self.assertEqual(final["persistence_status"], "unconfirmed")
        self.assertEqual(final["report_paths"], {})
        self.assertNotIn("secret token", final["explanation"])
        projection = inspection_terminal_projection({"recovery_outcome": inspection_stop_outcome(final).to_dict()}, {})
        self.assertEqual(projection["terminal_diagnostic"]["error_code"], "RECOVERY_NO_PROGRESS_LIMIT")
        self.assertEqual(load_plan_state(self.root, self.task_id)["steps"], before["steps"])
        self.assertEqual(load_plan_state(self.root, self.task_id)["task_state"]["status"], "running")

    def test_load_failure_preserves_held_facts(self):
        _before, stop = self.fixture()
        def no_load(*args, **kwargs):
            raise OSError("storage unreachable")
        final = finalize_inspection_stop(self.root, stop, loader=no_load)
        self.assertIn("RECOVERY_NO_PROGRESS_LIMIT", final["explanation"])
        self.assertIn("tool-4", final["explanation"])
        self.assertEqual(final["persistence_status"], "unconfirmed")

    def test_scope_drift_parameters_never_stop_new_authority(self):
        for key in ("task_id", "plan_id", "plan_version", "step_id", "attempt_id", "episode_id"):
            with self.subTest(key=key):
                before, stop = self.fixture(outputs=[])
                stop["scope"][key] += "-wrong"
                # A different task ID is not allowed to make any alternate
                # session file; loader has no state and the stop remains local.
                final = finalize_inspection_stop(self.root, stop)
                after = load_plan_state(self.root, self.task_id)
                self.assertNotEqual(final["persistence_status"], "committed")
                self.assertEqual(after["task_state"]["status"], "running")
                self.assertEqual(before["steps"], after["steps"])

    def test_late_stop_after_step_commit_or_runtime_handoff_is_not_projected(self):
        for change in ("commit", "runtime", "episode"):
            with self.subTest(change=change):
                before, stop = self.fixture(outputs=[])
                fresh = load_plan_state(self.root, self.task_id)
                if change == "commit":
                    fresh["steps"][0]["status"] = "done"
                    fresh["task_state"]["status"] = "completed"
                elif change == "runtime":
                    fresh["task_state"]["runtime_recovery"] = {"active_episode_id": "rt", "episodes": {"rt": {"status": "pending",
                        "request": {"scope": {"task_id": self.task_id, "plan_id": fresh["plan_id"],
                            "plan_version": fresh["plan_version"], "step_id": self.step_id}}}}}
                else:
                    fresh["task_state"]["causal_failure_packets"]["packet-one"]["source_fingerprint"] = "new-episode"
                save_plan_state(self.root, self.task_id, fresh)
                final = finalize_inspection_stop(self.root, stop)
                after = load_plan_state(self.root, self.task_id)
                self.assertEqual(final["resolution"], "stale_scope")
                self.assertNotIn("inspection_stop", after["task_state"])
                self.assertEqual(inspection_terminal_projection({"recovery_outcome": inspection_stop_outcome(final).to_dict()}, after), {})

    def test_blocked_directive_consumed_packet_remains_eligible_but_revalidation_does_not(self):
        _before, stop = self.fixture(outputs=[])
        durable = load_plan_state(self.root, self.task_id)
        task = durable["task_state"]
        task["active_failure_packet_ref"] = ""
        task["typed_recovery_receipts"] = {"decision-one": {"source_fingerprint": "episode-one", "failure_packet_ref": "packet-one",
            "outcome": {"type": "blocked"}}}
        task["consumed_failure_packet_refs"] = {"packet-one": {"decision_id": "decision-one"}}
        save_plan_state(self.root, self.task_id, durable)
        final = finalize_inspection_stop(self.root, stop)
        self.assertEqual(final["persistence_status"], "committed")
        fresh = load_plan_state(self.root, self.task_id)
        fresh["task_state"]["typed_recovery_receipts"]["decision-one"]["outcome"]["type"] = "revalidation_required"
        self.assertEqual(current_inspection_stop(fresh), {})

    def test_sensitive_tool_details_are_not_rendered_or_embedded(self):
        durable, _ = self.fixture(outputs=[])
        self.episode["_observation_receipts"][-1]["error"]["detail"] = "Authorization: Bearer private-value"
        stop = build_inspection_stop(seed=self.seed, durable=durable, source=self.source, episode=self.episode)
        self.assertNotIn("private-value", str(stop))
        self.assertIn("[redacted]", stop["explanation"])

    def test_writer_permission_failure_does_not_hide_stop(self):
        _before, stop = self.fixture()
        import os
        real_link = os.link
        def failing_report_link(src, dst, *args, **kwargs):
            if str(dst) in {"report.md", "reproduce.sh", "stage_status.tsv"}:
                raise PermissionError("private detail")
            return real_link(src, dst, *args, **kwargs)
        with patch("os.link", side_effect=failing_report_link):
            final = finalize_inspection_stop(self.root, stop)
        self.assertEqual(final["report_paths"], {})
        self.assertIn("RECOVERY_NO_PROGRESS_LIMIT", final["explanation"])
        self.assertEqual(final["persistence_status"], "committed")
        self.assertTrue(any(row["stage"].startswith("report.") for row in final["secondary_failures"]))
        self.assertFalse(list((self.root / "task_outputs" / self.task_id).glob(".runtime-recovery-*.tmp")))

    def test_cas_race_preserves_new_plan_and_reports_nothing(self):
        _before, stop = self.fixture()
        def racing_save(data_path, task_id, candidate):
            fresh = load_plan_state(data_path, task_id)
            fresh["plan_version"] = "plan-newer"
            save_plan_state(data_path, task_id, fresh)
            save_plan_state(data_path, task_id, candidate)
        final = finalize_inspection_stop(self.root, stop, saver=racing_save)
        self.assertEqual(final["persistence_status"], "unconfirmed")
        self.assertEqual(final["report_paths"], {})
        self.assertEqual(load_plan_state(self.root, self.task_id)["plan_version"], "plan-newer")

    def test_publication_failure_is_held_without_reopening_reader(self):
        durable, _stop = self.fixture(outputs=[])
        self.source["payload"]["inspection_publication_failures"] = [{"code": "INSPECTION_OBJECT_STORE_FAILED",
            "detail": "No durable locator was published", "source_object_id": "validation-one", "handle": "handle-one"}]
        stop = build_inspection_stop(seed=self.seed, durable=durable, source=self.source, episode=self.episode)
        self.assertEqual(stop["publication_failures"][0]["error_code"], "INSPECTION_OBJECT_STORE_FAILED")
        self.assertIn("INSPECTION_OBJECT_STORE_FAILED", stop["explanation"])

    def test_approved_planner_materialized_reports_use_declared_output_root(self):
        output_root = self.root / "requested-output"
        rows = [{"output_id": "output_dir", "role": "output_dir", "artifact_kind": "directory",
            "canonical_path": str(output_root), "path_policy": "managed_exact", "source": "user_explicit"}]
        materializations = []
        for ref, name in [("report", "report.md"), ("reproduce_script", "reproduce.sh"), ("stage_status", "stage_status.tsv")]:
            rows.append({"output_id": ref, "role": ref, "artifact_kind": "file", "root_ref": "output_dir",
                "path_policy": "planner_materialized", "source": "user_explicit"})
            materializations.append({"output_ref": ref, "output_id": ref, "path": name,
                "canonical_path": str(output_root / name), "origin": "planner"})
        _durable, stop = self.fixture(outputs=rows, materializations=materializations, approve=True)
        final = finalize_inspection_stop(self.root, stop)
        self.assertEqual(set(final["report_paths"]), {"report.md", "reproduce.sh", "stage_status.tsv"}, final)
        self.assertTrue(all(Path(path).parent == output_root for path in final["report_paths"].values()))

    def test_unapproved_external_output_and_symlink_parent_are_refused(self):
        target = self.root / "unapproved" / "report.md"
        _durable, stop = self.fixture(outputs=[str(target)])
        final = finalize_inspection_stop(self.root, stop)
        self.assertEqual(final["report_paths"], {})
        self.assertFalse(target.exists())
        parent = self.root / "approved-link"
        outside = self.root / "outside"
        outside.mkdir()
        parent.symlink_to(outside, target_is_directory=True)
        _durable, stop = self.fixture(outputs=[str(parent / "report.md")], approve=True)
        final = finalize_inspection_stop(self.root, stop)
        self.assertEqual(final["report_paths"], {})
        self.assertFalse((outside / "report.md").exists())

    def test_task_or_plan_revision_drift_is_stale_even_if_six_scope_ids_match(self):
        for key in ("plan_revision", "approved_plan_hash"):
            with self.subTest(key=key):
                _durable, stop = self.fixture(outputs=[])
                fresh = load_plan_state(self.root, self.task_id)
                fresh[key] = 999 if key == "plan_revision" else "new-approved-plan-hash"
                save_plan_state(self.root, self.task_id, fresh)
                final = finalize_inspection_stop(self.root, stop)
                self.assertEqual(final["resolution"], "stale_scope")

    def test_malformed_task_path_is_rejected_before_any_store_read(self):
        _durable, stop = self.fixture(outputs=[])
        stop["scope"]["task_id"] = "../../outside"
        with patch("agent_core.workflow.inspection_stop._write_report", side_effect=AssertionError("no write")):
            def fail_if_loaded(*args):
                self.fail("unsafe task_id reached loader")
            final = finalize_inspection_stop(self.root, stop, loader=fail_if_loaded)
        self.assertEqual(final["persistence_status"], "unconfirmed")

    def test_archive_failure_after_no_progress_is_secondary_and_reader_io_is_technical(self):
        durable, _stop = self.fixture(outputs=[])
        self.episode["secondary_failures"] = [{"stage": "recovery.archive", "error_code": "RECOVERY_PHASE_ARCHIVE_FAILED",
            "detail": "credential content must not escape"}]
        self.episode["_observation_receipts"][-1]["error"]["code"] = "INSPECTION_OBJECT_IO_ERROR"
        stop = build_inspection_stop(seed=self.seed, durable=durable, source=self.source, episode=self.episode)
        self.assertEqual(stop["stop_reason"]["error_code"], "RECOVERY_NO_PROGRESS_LIMIT")
        self.assertEqual(stop["secondary_failures"][0]["error_code"], "RECOVERY_PHASE_ARCHIVE_FAILED")
        self.assertEqual(stop["secondary_failures"][0]["detail"], "[redacted]")
        self.assertEqual(stop["technical_classification"], "inspection_infrastructure_error")

    def test_old_runtime_graph_result_does_not_shadow_current_inspection(self):
        durable, stop = self.fixture(outputs=[])
        final = finalize_inspection_stop(self.root, stop)
        durable = load_plan_state(self.root, self.task_id)
        values = {"runtime_recovery_result": {"episode_id": "old-rt", "resolution_disposition": "internal_error"},
            "pending_runtime_evidence_request": {"episode_id": "old-rt"},
            "recovery_outcome": inspection_stop_outcome(final).to_dict()}
        durable["task_state"]["runtime_recovery"] = {"active_episode_id": "old-rt", "episodes": {"old-rt": {
            "status": "pending", "request": {"scope": {"task_id": self.task_id, "plan_id": "old-plan",
                "plan_version": durable["plan_version"], "step_id": self.step_id}}}}}
        self.assertEqual(inspection_terminal_projection(values, durable)["terminal_diagnostic"]["error_code"], "RECOVERY_NO_PROGRESS_LIMIT")
        self.assertEqual(inspection_terminal_projection(values, None), {})
        self.assertEqual(inspection_terminal_projection({**values, "task_status": "cancelled"}, durable), {})

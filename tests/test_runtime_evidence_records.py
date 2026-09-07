from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent_core.action_runtime import build_runtime_binding_registry
from agent_core.runtime_evidence import (
    RuntimeEvidenceError, digest, evaluate_runtime_evidence,
    make_runtime_evidence, merge_runtime_evidence, project_runtime_audits,
    runtime_evidence_state, validate_runtime_evidence,
)


class RuntimeEvidenceRecordTests(unittest.TestCase):
    OBSERVED = "2026-09-07T10:00:00+00:00"
    NOW = "2026-09-07T10:05:00+00:00"

    def setUp(self):
        self.scope = {
            "skill_id": "fixture/generic", "skill_document_digest": digest("doc"),
            "skill_contract_digest": digest("contract"), "environment_id": "fixture",
            "environment_fingerprint": digest("prefix"), "host_id": "fixture-host",
            "inspection_contract_digest": digest("probe"),
        }
        self.audit = {
            "skill_id": "fixture/generic", "env": "fixture", "status": "ready",
            "resolved_runtime_paths": {"RESOLVED_TOOL_BIN": "/approved/runtime/tool"},
            "declared_detail": "SECRET_RAW_OUTPUT_MUST_NOT_BE_PERSISTED",
            "healthcheck": "arbitrary original shell text is not copied",
            "random_model_field": "not trusted",
        }

    def make(self, **changes):
        params = dict(audit=self.audit, task_id="task-1", skill_scope=self.scope,
                      observed_at=self.OBSERVED, source_ref="probe:fixture")
        params.update(changes)
        return make_runtime_evidence(**params)

    def state(self, records):
        admitted = merge_runtime_evidence([], records, task_id="task-1", accepted_at=self.NOW)
        return {"runtime_evidence": runtime_evidence_state(admitted)}

    def evaluate(self, state, **kwargs):
        return evaluate_runtime_evidence(state, task_id="task-1", now=self.NOW,
                                        required_binding_ids=["runtime.tool.bin"], **kwargs)

    def test_immutable_record_allowlist_and_disk_reload_projection(self):
        record = self.make()
        self.assertNotIn("SECRET_RAW_OUTPUT", json.dumps(record))
        self.assertNotIn("healthcheck", record)
        state = self.state([record])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "task_state.json"
            target.write_text(json.dumps(state))
            reloaded = json.loads(target.read_text())
        audits = project_runtime_audits(reloaded, task_id="task-1", now=self.NOW)
        bindings = build_runtime_binding_registry(audits)
        self.assertEqual(bindings["runtime.tool.bin"]["value"], "/approved/runtime/tool")
        self.assertEqual(bindings["runtime.tool.bin"]["status"], "ready")
        self.assertEqual(audits[0]["observed_at"], "2026-09-07T10:00:00.000000+00:00")
        self.assertNotIn("acceptance", record)

    def test_identity_is_deterministic_and_replay_does_not_refresh_observation(self):
        record = self.make()
        admitted = merge_runtime_evidence([], [record], task_id="task-1", accepted_at=self.NOW)
        again = merge_runtime_evidence(admitted, [self.make()], task_id="task-1", accepted_at="2026-09-07T10:06:00Z")
        self.assertEqual(again, admitted)
        self.assertEqual(runtime_evidence_state(again)["revision"], 1)

    def test_same_id_different_content_and_tampering_rejected(self):
        record = self.make(evidence_id="fixture-stable-id")
        changed_audit = copy.deepcopy(self.audit)
        changed_audit["resolved_runtime_paths"]["RESOLVED_TOOL_BIN"] = "/approved/runtime/other"
        changed = self.make(audit=changed_audit, evidence_id="fixture-stable-id")
        with self.assertRaisesRegex(RuntimeEvidenceError, "ID_CONFLICT"):
            merge_runtime_evidence(self.state([record])["runtime_evidence"]["records"], [changed], task_id="task-1")
        damaged = copy.deepcopy(record)
        damaged["resolved_runtime_paths"]["RESOLVED_TOOL_BIN"] = "/forged/path"
        with self.assertRaisesRegex(RuntimeEvidenceError, "DIGEST_MISMATCH"):
            validate_runtime_evidence(damaged)

    def test_unaccepted_and_cross_task_records_do_not_project(self):
        record = self.make()
        with self.assertRaisesRegex(RuntimeEvidenceError, "TASK_MISMATCH"):
            project_runtime_audits({"runtime_evidence": runtime_evidence_state([record])}, now=self.NOW)
        state = self.state([record])
        with self.assertRaisesRegex(RuntimeEvidenceError, "TASK_MISMATCH"):
            project_runtime_audits(state, task_id="another-task", now=self.NOW)
        with self.assertRaisesRegex(RuntimeEvidenceError, "TASK_MISMATCH"):
            merge_runtime_evidence([], state["runtime_evidence"]["records"], task_id="another-task")

    def test_legacy_and_incomplete_scope_remain_unknown(self):
        self.assertEqual(project_runtime_audits({"runtime_audit": [self.audit]}), [])
        legacy = self.make(producer="legacy_unverified")
        partial = self.make(skill_scope={"skill_id": "fixture/generic"})
        for record in (legacy, partial):
            state = self.state([record])
            self.assertEqual(self.evaluate(state)["runtime.tool.bin"]["availability"], "UNKNOWN")
            self.assertEqual(project_runtime_audits(state, now=self.NOW), [])

    def test_healthcheck_failure_is_not_negative_dependency_evidence(self):
        for status in ("timeout", "permission_error", "execution_error", "output_invalid", "cancelled"):
            record = self.make(inspection_status=status)
            state = self.state([record])
            evaluation = self.evaluate(state)["runtime.tool.bin"]
            self.assertEqual(evaluation["availability"], "UNKNOWN")
            self.assertEqual(evaluation["inspection_statuses"], [status])
        missing_audit = {**self.audit, "status": "missing"}
        self.assertEqual(self.evaluate(self.state([self.make(audit=missing_audit)]))["runtime.tool.bin"]["availability"], "UNKNOWN")

    def test_partial_coverage_and_unknown_never_erase_available_fact(self):
        first = self.make()
        second_audit = {**self.audit, "resolved_runtime_paths": {"RESOLVED_OTHER_BIN": "/approved/other"}}
        second = self.make(audit=second_audit, observed_at="2026-09-07T10:01:00Z")
        unknown = self.make(inspection_status="timeout", observed_at="2026-09-07T10:02:00Z", supersedes=[first["evidence_id"]])
        state = self.state([first, second, unknown])
        evaluation = self.evaluate(state)
        self.assertEqual(evaluation["runtime.tool.bin"]["availability"], "AVAILABLE")
        self.assertEqual(evaluation["runtime.other.bin"]["availability"], "AVAILABLE")

    def test_explicit_complete_negative_supersedes_only_covered_fact(self):
        first = self.make(audit={**self.audit, "resolved_runtime_paths": {
            "RESOLVED_TOOL_BIN": "/approved/runtime/tool", "RESOLVED_OTHER_BIN": "/approved/runtime/other"}})
        negative = self.make(
            audit={**self.audit, "resolved_runtime_paths": {}},
            observed_at="2026-09-07T10:01:00Z", supersedes=[first["evidence_id"]],
            coverage={"runtime.tool.bin": {"availability": "UNAVAILABLE", "reason_code": "required_executable_absent", "conclusion_complete": True}},
        )
        state = self.state([first, negative])
        evaluations = self.evaluate(state)
        self.assertEqual(evaluations["runtime.tool.bin"]["availability"], "UNAVAILABLE")
        self.assertEqual(evaluations["runtime.other.bin"]["availability"], "AVAILABLE")
        bindings = build_runtime_binding_registry(project_runtime_audits(state, now=self.NOW))
        self.assertNotIn("runtime.tool.bin", bindings)
        self.assertIn("runtime.other.bin", bindings)

    def test_positive_negative_conflict_needs_explicit_supersession(self):
        positive = self.make()
        negative = self.make(
            audit={**self.audit, "resolved_runtime_paths": {}},
            observed_at="2026-09-07T10:01:00Z",
            coverage={"runtime.tool.bin": {"availability": "UNAVAILABLE", "reason_code": "required_executable_absent", "conclusion_complete": True}},
        )
        state = self.state([positive, negative])
        self.assertEqual(self.evaluate(state)["runtime.tool.bin"]["availability"], "AMBIGUOUS")
        self.assertEqual(project_runtime_audits(state, now=self.NOW), [])

    def test_uncovered_output_is_never_projected(self):
        record = self.make(
            audit={**self.audit, "resolved_runtime_paths": {
                "RESOLVED_TOOL_BIN": "/approved/runtime/tool",
                "RESOLVED_UNCOVERED_BIN": "/uncovered/other",
            }},
            coverage={"runtime.tool.bin": {"availability": "AVAILABLE", "output_name": "RESOLVED_TOOL_BIN", "value_kind": "executable_path"}},
        )
        state = self.state([record])
        bindings = build_runtime_binding_registry(project_runtime_audits(state, now=self.NOW))
        self.assertEqual(set(bindings), {"runtime.tool.bin"})

    def test_negative_without_complete_conclusion_remains_unknown(self):
        negative = self.make(coverage={"runtime.tool.bin": {"availability": "UNAVAILABLE", "reason_code": "absence_unproven"}})
        self.assertEqual(self.evaluate(self.state([negative]))["runtime.tool.bin"]["availability"], "UNKNOWN")

    def test_explicit_fact_failure_cannot_be_bypassed_by_existing_path_derivation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = home / "bin" / "java"
            executable.parent.mkdir()
            executable.write_bytes(b"offline fixture; never executed")
            executable.chmod(0o700)
            audit = {**self.audit, "resolved_runtime_paths": {"RESOLVED_JAVA_HOME": str(home)}}
            home_fact = self.make(audit=audit)
            for availability in ("UNAVAILABLE", "AMBIGUOUS"):
                negative = self.make(audit={**self.audit, "resolved_runtime_paths": {}}, coverage={
                    "runtime.java.bin": {"availability": availability,
                                         "reason_code": "capability_validation_failed",
                                         "conclusion_complete": True},
                })
                state = self.state([home_fact, negative])
                source = {
                    "runtime_audit": project_runtime_audits(state, now=self.NOW),
                    "runtime_binding_evaluations": evaluate_runtime_evidence(state, now=self.NOW),
                }
                materialized = build_runtime_binding_registry(source)
                self.assertEqual(materialized["runtime.java.home"]["status"], "ready")
                self.assertNotEqual(materialized["runtime.java.bin"]["status"], "ready")

    def test_conflict_remains_ambiguous_regardless_of_order(self):
        first = self.make()
        changed = {**self.audit, "resolved_runtime_paths": {"RESOLVED_TOOL_BIN": "/approved/runtime/other"}}
        second = self.make(audit=changed, observed_at="2026-09-07T10:01:00Z")
        for records in ([first, second], [second, first]):
            state = self.state(records)
            self.assertEqual(self.evaluate(state)["runtime.tool.bin"]["availability"], "AMBIGUOUS")
            self.assertEqual(project_runtime_audits(state, now=self.NOW), [])

    def test_scope_and_expiry_prevent_stale_projection(self):
        state = self.state([self.make()])
        self.assertEqual(project_runtime_audits(state, now="2026-09-07T12:00:00Z"), [])
        self.assertEqual(project_runtime_audits(state, now="2026-09-07T09:00:00Z"), [])
        self.assertEqual(project_runtime_audits(state, now=self.NOW, skill_scope={"skill_contract_digest": digest("changed")}), [])
        with self.assertRaisesRegex(RuntimeEvidenceError, "SCOPE_MISMATCH"):
            self.make(skill_scope={**self.scope, "environment_id": "different"})

    def test_invalid_fact_kind_mapping_and_scope_digest_are_rejected(self):
        for coverage in (
            {"runtime.tool.bin": {"availability": "AVAILABLE", "output_name": "RESOLVED_TOOL_BIN", "value_kind": "invented_kind"}},
            {"runtime.other.bin": {"availability": "AVAILABLE", "output_name": "RESOLVED_TOOL_BIN", "value_kind": "executable_path"}},
        ):
            with self.assertRaises(RuntimeEvidenceError):
                self.make(coverage=coverage)
        with self.assertRaisesRegex(RuntimeEvidenceError, "SCOPE_DIGEST_INVALID"):
            self.make(skill_scope={**self.scope, "skill_contract_digest": "model-says-verified"})
        with self.assertRaises(RuntimeEvidenceError):
            self.make(source_ref="/config/.env.api_keys")

    def test_sensitive_path_unknown_kind_and_impossible_supersession_rejected(self):
        for paths in ({"RESOLVED_API_KEY": "/approved/tool"}, {"RESOLVED_TOOL_BIN": "/approved/.env.api_keys"}, {"RESOLVED_TOOL_BIN": "relative/tool"}):
            with self.assertRaises(RuntimeEvidenceError):
                self.make(audit={**self.audit, "resolved_runtime_paths": paths})
        with self.assertRaisesRegex(RuntimeEvidenceError, "PRODUCER_INVALID"):
            self.make(producer="model")
        bad = self.make(supersedes=["unknown-evidence"])
        with self.assertRaisesRegex(RuntimeEvidenceError, "SUPERSESSION_INVALID"):
            self.state([bad])
        with self.assertRaisesRegex(RuntimeEvidenceError, "TIMEZONE_REQUIRED"):
            self.make(observed_at="2026-09-07T10:00:00")


if __name__ == "__main__":
    unittest.main()

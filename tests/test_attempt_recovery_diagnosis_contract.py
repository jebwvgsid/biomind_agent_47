from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage

import agent_core.middleware as middleware_module
from agent_core.context_lineage import ContextLineageStore
from agent_core.diagnostics import RecoveryContext
from agent_core.node_context import (
    ControllerBinding,
    LLMPayload,
    NodeContext,
    StalenessDecision,
)
from agent_core.plan_tracker import (
    ATTEMPT_FAILED,
    ATTEMPT_VALIDATION_FAILED,
    ATTEMPT_VALIDATION_PASSED,
    build_plan_state,
    classify_attempt_resume,
    record_provenance_event,
)
from agent_core.recovery import (
    AttemptFailureEvidence,
    ExecutionRecord,
    RecoveryAction,
    RecoveryTrigger,
    RecoveryStateStore,
)
from agent_core.recovery_evidence import (
    RecoveryEvidenceAccessError,
    RecoveryEvidenceLimits,
    RecoveryEvidenceReader,
)


def _plan_state() -> dict:
    state = build_plan_state(
        "task-b",
        "- [ ] Execute annotation | Action: EXECUTE | Done: annotated output exists",
    )
    state["plan_id"] = "plan-b"
    state["plan_version"] = "plan_v1"
    state["steps"][0]["step_id"] = "step-b"
    state["steps"][0]["status"] = "running"
    return state


def _bind_action(state: dict, action_id: str, *, epoch: str = "epoch-1") -> dict:
    return record_provenance_event(state, "action_bound", {
        "action": {
            "action_id": action_id,
            "step_id": "step-b",
            "type": "execute",
            "execution_epoch": epoch,
            "action_hash": f"hash-{action_id}",
        }
    })


def _advance_to_execution_finished(state: dict, action_id: str) -> dict:
    attempt = record_provenance_event(
        state, "attempt_created", {"action_id": action_id}
    )
    for status in ("SCRIPT_READY", "APPROVED", "STARTING", "RUNNING"):
        record_provenance_event(state, "attempt_state_changed", {
            "attempt_id": attempt["attempt_id"],
            "status": status,
        })
    record_provenance_event(state, "execution_finished", {
        "attempt_id": attempt["attempt_id"],
        "execution_record": {
            "execution_id": f"exec-{attempt['attempt_id']}",
            "attempt_id": attempt["attempt_id"],
            "action_id": action_id,
            "step_id": "step-b",
            "returncode": 1,
            "execution_status": "failed",
        },
    })
    return attempt


class ActionAttemptIdentityContractTests(unittest.TestCase):
    def test_new_action_forbids_old_attempt_resume_and_allocates_new_attempt(self):
        state = _plan_state()
        action_a = _bind_action(state, "action-a")
        attempt_a = record_provenance_event(
            state, "attempt_created", {"action_id": action_a["action_id"]}
        )
        action_b = _bind_action(state, "action-b")

        resume_b = classify_attempt_resume(
            state,
            step_id="step-b",
            action_id="action-b",
            plan_id="plan-b",
            plan_version="plan_v1",
            execution_epoch="epoch-1",
        )
        self.assertTrue(resume_b["create_new_attempt"])
        self.assertEqual(resume_b["route"], "none")

        attempt_b = record_provenance_event(
            state, "attempt_created", {"action_id": action_b["action_id"]}
        )
        persisted_a = state["provenance_state"]["attempts"][attempt_a["attempt_id"]]

        self.assertNotEqual(attempt_a["attempt_id"], attempt_b["attempt_id"])
        self.assertEqual(attempt_b["action_id"], "action-b")
        self.assertEqual(persisted_a["status"], ATTEMPT_FAILED)
        self.assertEqual(persisted_a["superseded_by_action_id"], "action-b")

    def test_resume_requires_plan_step_action_and_epoch_identity(self):
        state = _plan_state()
        action = _bind_action(state, "action-a", epoch="epoch-7")
        attempt = record_provenance_event(
            state, "attempt_created", {"action_id": action["action_id"]}
        )

        exact = classify_attempt_resume(
            state,
            step_id="step-b",
            action_id="action-a",
            plan_id="plan-b",
            plan_version="plan_v1",
            execution_epoch="epoch-7",
        )
        wrong_epoch = classify_attempt_resume(
            state,
            step_id="step-b",
            action_id="action-a",
            plan_id="plan-b",
            plan_version="plan_v1",
            execution_epoch="epoch-8",
        )

        self.assertEqual(exact["attempt"]["attempt_id"], attempt["attempt_id"])
        self.assertFalse(exact["create_new_attempt"])
        self.assertTrue(wrong_epoch["create_new_attempt"])

    def test_invalid_validation_is_not_transition_guard_resumable(self):
        failed_state = _plan_state()
        failed_action = _bind_action(failed_state, "action-failed")
        failed_attempt = _advance_to_execution_finished(
            failed_state, failed_action["action_id"]
        )
        record_provenance_event(failed_state, "validation_recorded", {
            "attempt_id": failed_attempt["attempt_id"],
            "validation": {"validation_id": "validation-failed", "status": "invalid"},
        })
        failed_resume = classify_attempt_resume(
            failed_state,
            step_id="step-b",
            action_id="action-failed",
            plan_id="plan-b",
            plan_version="plan_v1",
            execution_epoch="epoch-1",
        )

        passed_state = _plan_state()
        passed_action = _bind_action(passed_state, "action-passed")
        passed_attempt = _advance_to_execution_finished(
            passed_state, passed_action["action_id"]
        )
        record_provenance_event(passed_state, "validation_recorded", {
            "attempt_id": passed_attempt["attempt_id"],
            "validation": {"validation_id": "validation-passed", "status": "passed"},
        })
        passed_resume = classify_attempt_resume(
            passed_state,
            step_id="step-b",
            action_id="action-passed",
            plan_id="plan-b",
            plan_version="plan_v1",
            execution_epoch="epoch-1",
        )

        failed_record = failed_state["provenance_state"]["attempts"][
            failed_attempt["attempt_id"]
        ]
        passed_record = passed_state["provenance_state"]["attempts"][
            passed_attempt["attempt_id"]
        ]
        self.assertEqual(failed_record["status"], ATTEMPT_VALIDATION_FAILED)
        self.assertEqual(failed_resume["route"], "supervisor")
        self.assertEqual(passed_record["status"], ATTEMPT_VALIDATION_PASSED)
        self.assertEqual(passed_resume["route"], "transition_guard")


class ImmutableFailureEvidenceContractTests(unittest.TestCase):
    def _failure(self) -> AttemptFailureEvidence:
        return AttemptFailureEvidence.from_mapping({
            "session_id": "session-b",
            "task_id": "task-b",
            "plan_id": "plan-b",
            "plan_version": "plan_v1",
            "step_id": "step-b",
            "action_id": "action-b",
            "attempt_id": "attempt-b",
            "failed_command": '"$JAVA_BIN" -jar "$TOOL_JAR"',
            "returncode": 1,
            "stdout_log": "/run/attempt-b/stdout.log",
            "stderr_log": "/run/attempt-b/stderr.log",
            "trace_file": "/run/attempt-b/command_trace.log",
            "stdout_summary": "startup completed",
            "stderr_summary": "runtime failed",
            "expected_runtime_contract": {
                "bindings": {"JAVA_BIN": "/verified/java"},
            },
            "actual_environment": {
                "resolved_executables": {"java": "/usr/bin/java"},
            },
            "machine_audit_findings": [{
                "code": "RUNTIME_EXECUTABLE_MISMATCH",
                "severity": "HIGH",
            }],
            "diagnostic_code": "EXECUTION_FAILED",
            "diagnostic_summary": "the command exited non-zero",
        })

    def test_failure_snapshot_is_per_attempt_immutable_and_reload_complete(self):
        with tempfile.TemporaryDirectory() as root:
            store = RecoveryStateStore(root, "task-b")
            path = store.save_attempt_failure_evidence(self._failure())
            first_bytes = path.read_bytes()
            store.save_attempt_failure_evidence({
                "task_id": "task-b",
                "action_id": "action-b",
                "attempt_id": "attempt-b",
                "returncode": 1,
            })
            reloaded = store.load_attempt_failure_evidence("attempt-b")

            self.assertEqual(path.read_bytes(), first_bytes)
            self.assertEqual(
                path,
                Path(root)
                / "task_outputs"
                / "task-b"
                / "attempts"
                / "attempt-b"
                / "failure_evidence.json",
            )
            self.assertEqual(reloaded["failed_command"], '"$JAVA_BIN" -jar "$TOOL_JAR"')
            self.assertEqual(reloaded["exit_code"], 1)
            self.assertEqual(reloaded["stderr_ref"], "/run/attempt-b/stderr.log")
            self.assertEqual(
                reloaded["expected_runtime"]["bindings"]["JAVA_BIN"],
                "/verified/java",
            )
            self.assertEqual(
                reloaded["machine_audit_findings"][0]["code"],
                "RUNTIME_EXECUTABLE_MISMATCH",
            )

            with self.assertRaisesRegex(ValueError, "identity conflict"):
                store.save_attempt_failure_evidence({
                    "task_id": "task-b",
                    "action_id": "action-other",
                    "attempt_id": "attempt-b",
                    "returncode": 1,
                })

    def test_execution_record_reload_cannot_erase_or_reassign_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store = RecoveryStateStore(root, "task-b")
            first = ExecutionRecord.from_mapping({
                "execution_id": "exec-b",
                "plan_id": "plan-b",
                "plan_version": "plan_v1",
                "step_id": "step-b",
                "action_id": "action-b",
                "attempt_id": "attempt-b",
                "returncode": 1,
                "failed_command": "tool input",
                "stderr": "first failure",
                "expected_runtime_contract": {
                    "bindings": {"TOOL_BIN": "/verified/tool"},
                },
            })
            store.save_execution_record(first)
            refreshed = ExecutionRecord.from_mapping({
                "execution_id": "exec-b",
                "plan_id": "plan-b",
                "plan_version": "plan_v1",
                "step_id": "step-b",
                "action_id": "action-b",
                "attempt_id": "attempt-b",
                "returncode": 1,
            })
            store.save_execution_record(refreshed)
            row = store.load_execution_records()[0]

            self.assertEqual(row["failed_command"], "tool input")
            self.assertEqual(
                row["expected_runtime_contract"]["bindings"]["TOOL_BIN"],
                "/verified/tool",
            )
            with self.assertRaisesRegex(ValueError, "identity conflict"):
                store.save_execution_record(ExecutionRecord.from_mapping({
                    "execution_id": "exec-b",
                    "plan_id": "plan-b",
                    "plan_version": "plan_v1",
                    "step_id": "step-b",
                    "action_id": "action-other",
                    "attempt_id": "attempt-b",
                    "returncode": 1,
                }))


class RecoveryEvidenceReaderContractTests(unittest.TestCase):
    def test_read_and_glob_are_bounded_to_current_task_root(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "task-b"
            attempt = root / "attempts" / "attempt-b"
            attempt.mkdir(parents=True)
            stderr = attempt / "stderr.log"
            stderr.write_text(
                "\n".join(f"line-{index}" for index in range(1, 401)) + "\n",
                encoding="utf-8",
            )
            (attempt / "stdout.log").write_text("ok\n", encoding="utf-8")
            outside = Path(parent) / "other-session.log"
            outside.write_text("secret\n", encoding="utf-8")
            symlink = root / "outside.log"
            try:
                symlink.symlink_to(outside)
            except OSError:
                symlink = None

            reader = RecoveryEvidenceReader(
                root,
                RecoveryEvidenceLimits(
                    max_read_bytes=128,
                    max_read_lines=5,
                    max_scan_bytes=4096,
                    max_glob_results=8,
                    max_glob_depth=4,
                    max_glob_entries=32,
                ),
            )
            read = reader.read(
                "attempts/attempt-b/stderr.log",
                start_line=100,
                end_line=200,
            )
            globbed = reader.glob("*.log")

            self.assertEqual(read["start_line"], 100)
            self.assertEqual(read["end_line"], 104)
            self.assertEqual(read["content"].splitlines()[0], "line-100")
            self.assertTrue(read["truncated"])
            self.assertLessEqual(read["scanned_bytes"], 4096)
            self.assertEqual(
                globbed["matches"],
                [
                    "attempts/attempt-b/stderr.log",
                    "attempts/attempt-b/stdout.log",
                ],
            )
            self.assertNotIn("other-session.log", json.dumps(globbed))

            for forbidden in ("../other-session.log", str(outside)):
                with self.subTest(forbidden=forbidden):
                    with self.assertRaises(RecoveryEvidenceAccessError):
                        reader.read(forbidden)
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.glob("../*.log")
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.execute_request({
                    "tool": "read",
                    "path": "attempts/attempt-b/stderr.log",
                    "start_line": "not-an-integer",
                })
            if symlink is not None:
                with self.assertRaises(RecoveryEvidenceAccessError):
                    reader.read("outside.log")


def _judge_payload() -> dict:
    return {
        "schema_version": 4,
        "verdict": "repair",
        "failure_class": "SCRIPT_BUG",
        "failure_owner": "GENERATED_ACTION",
        "failure_type": "runtime_invocation",
        "error_is_real": True,
        "primary_execution_succeeded": False,
        "evidence_sufficient": True,
        "confidence": 0.95,
        "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
        "rerun_required": False,
        "consume_retry_budget": False,
        "user_notification_required": False,
        "reason": "the previous invocation bypassed the verified executable",
        "root_cause": "the previous script invoked the bare tool name",
        "evidence": ["stderr-bounded", "controller-machine-audit"],
        "evidence_refs": ["stderr-bounded"],
        "machine_findings_considered": ["RUNTIME_EXECUTABLE_MISMATCH"],
        "missing_evidence": [],
        "repair_strategy": "repair only the runtime invocation",
        "repair_target": "runtime invocation",
        "repair_instruction": "use TOOL_BIN and preserve input/output checks",
        "next_action": RecoveryAction.PATCH_SCRIPT.value,
        "requires_user_input": False,
    }


def _recovery_identity_state() -> dict:
    task_spec = {
        "task_id": "task-b",
        "goal": "recover the failed action",
        "inputs": [],
        "output_contracts": [],
        "semantic_constraints": [],
    }
    return {
        "task_id": "task-b",
        "state_generation": 7,
        "current_step_id": "step-b",
        "current_task": {"task_spec": task_spec},
        "task_spec": task_spec,
        "task_state": {"budgets": {}},
        "steps": [{"step_id": "step-b", "status": "running"}],
        "approval_snapshot_ref": "approval-ref-a",
        "provenance_state": {
            "actions": {
                "action-a": {
                    "action_id": "action-a",
                    "step_id": "step-b",
                    "created_at": "2026-08-21T00:00:00",
                },
            },
            "attempts": {
                "attempt-a": {
                    "attempt_id": "attempt-a",
                    "action_id": "action-a",
                    "step_id": "step-b",
                    "status": "VALIDATION_FAILED",
                    "execution_record_id": "execution-a",
                    "validation_evidence_ref": "validation-a",
                    "returncode": 0,
                    "created_at": "2026-08-21T00:00:01",
                },
            },
        },
        "failure": {
            "error_code": "OUTPUT_REALIZATION_MISMATCH",
            "owner": "ARTIFACT_CONTRACT",
            "stage": "post_execution_reconciliation",
            "attempt_id": "attempt-a",
            "fingerprint": "failure-a",
        },
    }


def _recovery_plan_snapshot(snapshot_id: str = "snapshot-a") -> dict:
    return {
        "plan_snapshot_id": snapshot_id,
        "plan_revision": 1,
        "steps": [{
            "step_id": "step-b",
            "status": "running",
            "inputs": [],
            "outputs": [],
            "dependencies": [],
        }],
    }




if __name__ == "__main__":
    unittest.main()

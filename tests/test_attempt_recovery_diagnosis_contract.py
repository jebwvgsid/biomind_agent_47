from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage

import bio_agents
import agent_core.middleware as middleware_module
from bio_agents import BioAnalystAgent
from agent_core.context_lineage import ContextLineageStore
from agent_core.diagnostics import RecoveryContext
from agent_core.middleware import error_recovery_middleware
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


class RecoveryRoutingContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_reply_survives_unrelated_mutable_state_changes(self):
        class LegalRecoveryJudge:
            async def ainvoke(self, _messages, *args, **kwargs):
                return AIMessage(content=json.dumps(_judge_payload()))

        initial = _recovery_identity_state()
        latest = deepcopy(initial)
        latest["state_generation"] = 99
        latest["task_state"]["budgets"] = {"recovery_judge_count": 1}
        latest["telemetry"] = {"last_provider_latency_ms": 42}
        latest["diagnostics"] = [{"at": "2026-08-21T00:01:00"}]
        snapshot = _recovery_plan_snapshot()

        with tempfile.TemporaryDirectory() as data_path:
            with (
                patch.object(
                    middleware_module,
                    "load_plan_state",
                    return_value=latest,
                ),
                patch.object(
                    middleware_module,
                    "load_approved_plan_snapshot",
                    side_effect=[snapshot, deepcopy(snapshot)],
                ),
            ):
                message, _ = await middleware_module._invoke_recovery_v2(
                    LegalRecoveryJudge(),
                    data_path=data_path,
                    task_id="task-b",
                    plan_state=initial,
                    step_id="step-b",
                    action_id="action-a",
                    attempt_id="attempt-a",
                    failure_fingerprint="diagnostic-a",
                )

        self.assertEqual(
            json.loads(message.content)["failure_type"],
            "runtime_invocation",
        )

    async def test_recovery_reply_stales_on_authoritative_identity_changes(self):
        class LegalRecoveryJudge:
            async def ainvoke(self, _messages, *args, **kwargs):
                return AIMessage(content=json.dumps(_judge_payload()))

        cases = []

        action_changed = _recovery_identity_state()
        action_changed["provenance_state"]["actions"]["action-b"] = {
            "action_id": "action-b",
            "step_id": "step-b",
            "created_at": "2026-08-21T00:00:02",
        }
        cases.append((
            "action",
            action_changed,
            _recovery_plan_snapshot(),
            "action_identity",
        ))

        attempt_changed = _recovery_identity_state()
        attempt_changed["provenance_state"]["attempts"]["attempt-b"] = {
            "attempt_id": "attempt-b",
            "action_id": "action-a",
            "step_id": "step-b",
            "status": "VALIDATION_FAILED",
            "execution_record_id": "execution-b",
            "validation_evidence_ref": "validation-b",
            "returncode": 0,
            "created_at": "2026-08-21T00:00:03",
        }
        cases.append((
            "attempt",
            attempt_changed,
            _recovery_plan_snapshot(),
            "attempt_id",
        ))

        approval_changed = _recovery_identity_state()
        approval_changed["approval_snapshot_ref"] = "approval-ref-b"
        changed_snapshot = _recovery_plan_snapshot("snapshot-b")
        changed_snapshot["plan_revision"] = 2
        cases.append((
            "approved-plan",
            approval_changed,
            changed_snapshot,
            "approval_ref",
        ))

        evidence_changed = _recovery_identity_state()
        evidence_changed["failure"]["fingerprint"] = "failure-b"
        cases.append((
            "failure-evidence",
            evidence_changed,
            _recovery_plan_snapshot(),
            "evidence_identity",
        ))

        for name, latest, latest_snapshot, expected_change in cases:
            with self.subTest(change=name), tempfile.TemporaryDirectory() as data_path:
                with (
                    patch.object(
                        middleware_module,
                        "load_plan_state",
                        return_value=latest,
                    ),
                    patch.object(
                        middleware_module,
                        "load_approved_plan_snapshot",
                        side_effect=[
                            _recovery_plan_snapshot(),
                            latest_snapshot,
                        ],
                    ),
                ):
                    _message, response_snapshot = (
                        await middleware_module._invoke_recovery_v2(
                            LegalRecoveryJudge(),
                            data_path=data_path,
                            task_id="task-b",
                            plan_state=_recovery_identity_state(),
                            step_id="step-b",
                            action_id="action-a",
                            attempt_id="attempt-a",
                            failure_fingerprint="diagnostic-a",
                        )
                    )
                stale_result = response_snapshot["stale_node_result"]
                self.assertEqual(
                    stale_result["decision_result"],
                    "STALE_NODE_RESULT",
                )
                self.assertFalse(stale_result["counts_as_failure"])
                self.assertFalse(stale_result["consumes_execution_retry"])
                self.assertIn(
                    expected_change,
                    stale_result["changed_preconditions"],
                )

    async def test_post_review_judge_revalidates_against_approved_snapshot(self):
        """A budget write must not make an approved-plan recovery reply stale."""
        plan_state = {
            "task_id": "task-b",
            "plan_id": "plan-b",
            "plan_version": "plan-v1",
            "state_generation": 7,
            "task_state": {"budgets": {}},
            "steps": [{"step_id": "step-b", "status": "running"}],
        }
        approved_snapshot = {
            "plan_snapshot_id": "approved-plan-b",
            "steps": [{"step_id": "step-b", "status": "running"}],
        }
        recovery_context = RecoveryContext(
            context_id="recovery-context-b",
            task_id="task-b",
            step_id="step-b",
            action_id="action-b",
            attempt_id="attempt-b",
            error_fingerprint="missing-parent-directory",
            current_step={"step_id": "step-b", "success_criteria": []},
            execution={"returncode": 1, "code": "write_report"},
            diagnosis={
                "failure_owner": "GENERATED_ACTION",
                "failure_type": "missing_output_parent_directory",
                "evidence": ["No such file or directory: plan_artifacts/report.txt"],
                "evidence_sufficient": True,
                "collection_complete": False,
            },
        )
        build_calls: list[dict] = []

        def fake_build_node_context(**kwargs):
            build_calls.append(dict(kwargs))
            snapshot = kwargs.get("approved_plan_snapshot")
            plan_hash = (
                "approved-plan-semantic-hash"
                if snapshot is approved_snapshot
                else "mutable-plan-semantic-hash"
            )
            binding = ControllerBinding(
                invocation_id=f"recovery-{len(build_calls)}",
                node_type="recovery",
                staleness_domain="recovery",
                state_generation=7,
                task_semantic_hash="task-semantic-hash",
                task_record_hash="task-record-hash",
                plan_semantic_hash=plan_hash,
                plan_record_hash=plan_hash,
                approval_ref="approved-plan-b",
                approval_applicability="applicable",
                step_id="step-b",
                attempt_id="attempt-b",
                failure_fingerprint="missing-parent-directory",
            )
            return NodeContext(
                binding=binding,
                llm_payload=LLMPayload({
                    "semantic_history": {},
                    "current_focus": {},
                }),
            )

        judge_payload = _judge_payload()
        judge_payload.update({
            "failure_type": "missing_output_parent_directory",
            "root_cause": "the generated script did not create the report parent directory",
            "reason": "the generated script did not create the report parent directory",
            "repair_target": "generated script output preparation",
            "repair_instruction": "create the approved output parent before writing",
        })
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = None
        agent.recovery_llm = object()
        agent.recovery_llm_policy = {
            "thinking_enabled": True,
            "reasoning_effort": "high",
            "scope": "recovery_only",
        }
        agent._node_context_semantic_inputs = lambda *args, **kwargs: {}
        agent._ainvoke_node_llm = AsyncMock(
            return_value=AIMessage(content=json.dumps(judge_payload))
        )

        state = {
            "task_id": "task-b",
            "current_step_id": "step-b",
            "execution_evidence": {
                "execution_id": "exec-b",
                "attempt_id": "attempt-b",
                "action_id": "action-b",
                "step_id": "step-b",
                "returncode": 1,
                "execution_status": "failed",
            },
        }
        with tempfile.TemporaryDirectory() as data_path:
            agent.data_path = data_path
            with (
                patch.object(
                    bio_agents,
                    "evaluate_recovery_trigger",
                    return_value=RecoveryTrigger(
                        required=True,
                        reasons=["structured_execution_failure"],
                        judge_required=True,
                    ),
                ),
                patch.object(
                    bio_agents,
                    "build_recovery_context",
                    return_value=recovery_context,
                ),
                patch.object(bio_agents, "diagnosis_required", return_value=True),
                patch.object(
                    bio_agents,
                    "canonical_task_spec_for_state",
                    return_value={"goal": "write the annotated report"},
                ),
                patch.object(
                    bio_agents,
                    "load_plan_state",
                    return_value=plan_state,
                ),
                patch.object(bio_agents, "save_plan_state"),
                patch.object(
                    bio_agents,
                    "load_approved_plan_snapshot",
                    return_value=approved_snapshot,
                ) as snapshot_loader,
                patch.object(
                    bio_agents,
                    "build_node_context",
                    side_effect=fake_build_node_context,
                ),
                patch.object(
                    bio_agents.PromptManager,
                    "get_recovery_v2_prompt",
                    return_value="classify this execution failure",
                ),
            ):
                result = await agent._adjudicate_post_review_recovery(
                    state,
                    {},
                    "task-b",
                )

        self.assertEqual(snapshot_loader.call_count, 2)
        self.assertEqual(len(build_calls), 2)
        recovery_focus = build_calls[0]["runtime"]["recovery_focus"]
        rendered_focus = json.dumps(recovery_focus, sort_keys=True)
        self.assertNotIn("expected_runtime", rendered_focus)
        self.assertNotIn("actual_runtime", rendered_focus)
        self.assertNotIn("manifest", rendered_focus)
        self.assertNotIn("sha256:", rendered_focus)
        self.assertIs(
            build_calls[0]["approved_plan_snapshot"],
            approved_snapshot,
        )
        self.assertIs(
            build_calls[1]["approved_plan_snapshot"],
            approved_snapshot,
        )
        self.assertEqual(
            result["recovery_decision"]["recommended_action"],
            RecoveryAction.PATCH_SCRIPT.value,
        )
        self.assertEqual(result["required_next_action"], "SCRIPT_PATCH")

    async def test_execution_failure_calls_recovery_and_supports_one_bounded_read(self):
        calls: list[str] = []

        class FakeRecoveryLLM:
            async def ainvoke(self, messages, *args, **kwargs):
                calls.append(str(messages[0].content))
                if len(calls) == 1:
                    return type("Message", (), {"content": json.dumps({
                        "evidence_request": {
                            "tool": "read",
                            "path": "attempts/attempt-b/stderr.log",
                            "start_line": 1,
                            "end_line": 2,
                        }
                    })})()
                return type("Message", (), {
                    "content": json.dumps(_judge_payload())
                })()

        with tempfile.TemporaryDirectory() as data_path:
            attempt_root = (
                Path(data_path)
                / "task_outputs"
                / "task-b"
                / "attempts"
                / "attempt-b"
            )
            attempt_root.mkdir(parents=True)
            stderr_path = attempt_root / "stderr.log"
            stderr_path.write_text(
                "TARGET_RUNTIME_LINE\nsecond line\nthird line\n",
                encoding="utf-8",
            )
            stdout_path = attempt_root / "stdout.log"
            stdout_path.write_text("startup\n", encoding="utf-8")
            script_path = attempt_root / "generated.sh"
            script_path.write_text(
                "check_input\ntool input\ncheck_output\n",
                encoding="utf-8",
            )
            input_path = attempt_root / "input.vcf"
            input_path.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
            output_path = attempt_root / "output.vcf"

            async def execute(_state):
                return {
                    "messages": [HumanMessage(content="<observation>failed</observation>")],
                    "execution_evidence": {
                        "execution_id": "exec-b",
                        "attempt_id": "attempt-b",
                        "action_id": "action-b",
                        "step_id": "step-b",
                        "returncode": 1,
                        "execution_status": "failed",
                        "command_role": "primary",
                        "command": "tool input",
                        "failed_command": "tool input",
                        "stdout_log": str(stdout_path),
                        "stderr_log": str(stderr_path),
                        "generated_script": {"path": str(script_path)},
                        "approved_script": {"path": str(script_path)},
                        "generated_code": "check_input\ntool input\ncheck_output",
                        "action_contract": {
                            "type": "execute",
                            "language": "bash",
                            "code": (
                                f"tool {input_path} -o {output_path}"
                            ),
                            "concrete_artifact_bindings": [
                                {
                                    "semantic_handle": "input.variant_vcf",
                                    "direction": "input",
                                    "path": str(input_path),
                                    "exists": True,
                                    "readable": True,
                                },
                                {
                                    "semantic_handle": "output.annotated_vcf",
                                    "direction": "output",
                                    "expected_path": str(output_path),
                                },
                            ],
                        },
                        "expected_runtime_contract": {
                            "bindings": {"TOOL_BIN": "/verified/tool"},
                            "executables": {"tool": "/verified/tool"},
                        },
                        "actual_environment": {
                            "resolved_executables": {"tool": "/usr/bin/tool"},
                        },
                        "controller_machine_audit_findings": [{
                            "code": "RUNTIME_EXECUTABLE_MISMATCH",
                            "severity": "HIGH",
                            "expected": "/verified/tool",
                            "observed": "/usr/bin/tool",
                        }],
                    },
                }

            wrapped = error_recovery_middleware(
                FakeRecoveryLLM(),
                data_path=data_path,
                recovery_llm_policy={
                    "thinking_enabled": True,
                    "reasoning_effort": "high",
                    "scope": "recovery_only",
                },
            )(execute)
            result = await wrapped({
                "task_id": "task-b",
                "current_step_id": "step-b",
            })
            snapshots = ContextLineageStore(
                data_path, "task-b"
            ).list_snapshots()
            trace = (
                Path(data_path)
                / "task_outputs"
                / "task-b"
                / "recovery"
                / "recovery_trace.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(
            row["recovery_llm_policy"]["thinking_enabled"]
            and row["recovery_llm_policy"]["reasoning_effort"] == "high"
            and row["recovery_llm_policy"]["scope"] == "recovery_only"
            for row in snapshots
        ))
        for row in snapshots:
            sections = {
                item["section"]: item
                for item in row["provider_context_manifest"]["sections"]
            }
            self.assertTrue(sections["previous_script"]["present"])
            self.assertTrue(sections["execution_evidence"]["present"])
            self.assertFalse(sections["runtime_bindings"]["present"])
            self.assertFalse(sections["machine_audit"]["present"])
            self.assertFalse(sections["execution_runtime_contract"]["present"])
            self.assertFalse(sections["validation_scope_contract"]["present"])
        initial_prompt = calls[0]
        followup_prompt = calls[1]
        for marker in (
            "previous_script",
            "failed_command",
            "exit_code",
            "bounded_stdout",
            "bounded_stderr",
            "allowed_recovery_actions",
        ):
            self.assertIn(marker, initial_prompt)
        for forbidden in (
            "expected_runtime",
            "actual_runtime",
            "runtime_snapshot_digest",
            "value_digest",
            "manifest_hash",
            "registry_digest",
            "RUNTIME_EXECUTABLE_MISMATCH",
            "=== CONTROLLER MACHINE AUDIT ===",
        ):
            self.assertNotIn(forbidden, initial_prompt)
        self.assertNotIn("The The Controller", initial_prompt)
        self.assertIn(str(input_path), initial_prompt)
        self.assertIn(str(output_path), initial_prompt)
        self.assertIn("<evidence_result>", followup_prompt)
        self.assertIn("TARGET_RUNTIME_LINE", followup_prompt)
        self.assertEqual(
            result["recovery_decision"]["repair_target"],
            "runtime invocation",
        )
        self.assertEqual(result["next_step"], "generate")
        self.assertIn(" RECOVERY_JUDGE", trace)
        self.assertIn('"judge_invoked": true', trace)

    async def test_local_optional_fallback_uses_policy_trace_without_judge(self):
        class FailIfCalled:
            async def ainvoke(self, _messages, *args, **kwargs):
                raise AssertionError("local policy must not invoke RecoveryJudge")

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>optional failed</observation>")],
                "execution_evidence": {
                    "execution_id": "optional-b",
                    "attempt_id": "attempt-optional-b",
                    "action_id": "action-optional-b",
                    "step_id": "step-b",
                    "returncode": 1,
                    "execution_status": "failed",
                    "command_role": "optional_probe",
                    "fallback_used": True,
                },
            }

        with tempfile.TemporaryDirectory() as data_path:
            wrapped = error_recovery_middleware(
                FailIfCalled(), data_path=data_path
            )(execute)
            result = await wrapped({"task_id": "task-b", "current_step_id": "step-b"})
            trace = (
                Path(data_path)
                / "task_outputs"
                / "task-b"
                / "recovery"
                / "recovery_trace.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            result["recovery_decision"]["recommended_action"],
            RecoveryAction.PASS_WITH_WARNING.value,
        )
        self.assertIn(" RECOVERY_POLICY", trace)
        self.assertNotIn(" RECOVERY_JUDGE\n", trace)

    async def test_stale_internal_protocol_owner_cannot_suppress_current_execute_failure(self):
        calls = []

        class RecoverySpy:
            async def ainvoke(self, messages, *args, **kwargs):
                calls.append(str(messages[-1].content))
                return AIMessage(content=json.dumps({
                    "schema_version": 4,
                    "failure_class": "SCRIPT_BUG",
                    "failure_owner": "SCRIPT",
                    "error_is_real": True,
                    "primary_execution_succeeded": False,
                    "evidence_sufficient": True,
                    "confidence": 0.97,
                    "recommended_action": "PATCH_SCRIPT",
                    "rerun_required": True,
                    "consume_retry_budget": True,
                    "user_notification_required": False,
                    "reason": "The current script failed.",
                    "evidence": ["exit_code=2"],
                    "verdict": "repair",
                    "root_cause": "The current script invoked an invalid option.",
                    "evidence_refs": ["stderr_summary"],
                    "machine_findings_considered": [],
                    "repair_target": "previous script",
                    "repair_instruction": "Correct only the invalid option.",
                }))

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>failed</observation>")],
                "execution_evidence": {
                    "execution_id": "exec-current",
                    "attempt_id": "attempt-current",
                    "action_id": "action-current",
                    "step_id": "step-current",
                    "returncode": 2,
                    "execution_status": "failed",
                    "command_role": "primary",
                    "stderr_summary": "tool: invalid option",
                },
            }

        with tempfile.TemporaryDirectory() as data_path:
            wrapped = error_recovery_middleware(
                RecoverySpy(), data_path=data_path
            )(execute)
            result = await wrapped({
                "task_id": "task-current",
                "current_step_id": "step-current",
                "failure_owner": "INTERNAL_PROTOCOL",
            })

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            result["recovery_decision"]["recommended_action"],
            RecoveryAction.PATCH_SCRIPT.value,
            result,
        )

    async def test_provider_failure_is_traced_as_judge_attempt_not_local_policy(self):
        class FailingRecoveryProvider:
            async def ainvoke(self, _messages, *args, **kwargs):
                raise RuntimeError("provider unavailable")

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>failed</observation>")],
                "execution_evidence": {
                    "execution_id": "exec-provider-error",
                    "attempt_id": "attempt-provider-error",
                    "action_id": "action-provider-error",
                    "step_id": "step-provider-error",
                    "returncode": 1,
                    "execution_status": "failed",
                    "command_role": "primary",
                    "stderr_summary": "primary command failed",
                },
            }

        with tempfile.TemporaryDirectory() as data_path:
            wrapped = error_recovery_middleware(
                FailingRecoveryProvider(), data_path=data_path
            )(execute)
            result = await wrapped({
                "task_id": "task-provider-error",
                "current_step_id": "step-provider-error",
            })
            trace = (
                Path(data_path)
                / "task_outputs"
                / "task-provider-error"
                / "recovery"
                / "recovery_trace.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            result["recovery_decision"]["recommended_action"],
            RecoveryAction.ESCALATE.value,
        )
        self.assertEqual(
            result["recovery_decision"]["failure_type"],
            "recovery_judge_provider_error",
        )
        self.assertIn(" RECOVERY_JUDGE", trace)
        self.assertIn('"judge_invoked": true', trace)
        self.assertNotIn(" RECOVERY_POLICY\n", trace)

    async def test_valid_judge_response_rejected_as_stale_is_not_schema_error(self):
        class LegalRecoveryJudge:
            async def ainvoke(self, _messages, *args, **kwargs):
                return AIMessage(content=json.dumps(_judge_payload()))

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>failed</observation>")],
                "execution_evidence": {
                    "execution_id": "exec-stale",
                    "attempt_id": "attempt-stale",
                    "action_id": "action-stale",
                    "step_id": "step-stale",
                    "returncode": 1,
                    "execution_status": "failed",
                    "command_role": "primary",
                    "stderr_summary": "primary command failed",
                },
            }

        stale = StalenessDecision(
            usable=False,
            code="STALE_RECOVERY_CONTEXT",
            changed_preconditions=("action_identity",),
            stale_signature="stale-action-identity",
        )
        with tempfile.TemporaryDirectory() as data_path:
            with patch.object(
                middleware_module,
                "evaluate_node_staleness",
                return_value=stale,
            ):
                wrapped = error_recovery_middleware(
                    LegalRecoveryJudge(), data_path=data_path
                )(execute)
                result = await wrapped({
                    "task_id": "task-stale",
                    "current_step_id": "step-stale",
                })

        self.assertEqual(result["decision_result"], "STALE_NODE_RESULT")
        self.assertFalse(result["counts_as_failure"])
        self.assertFalse(result["consumes_execution_retry"])
        self.assertNotIn("pending_recovery_action", result)
        self.assertEqual(result["next_step"], "supervisor")
        self.assertNotIn("recovery_decision", result)
        self.assertEqual(
            result["execution_evidence"]["attempt_id"],
            "attempt-stale",
        )

    async def test_malformed_judge_response_remains_schema_error(self):
        class MalformedRecoveryJudge:
            async def ainvoke(self, _messages, *args, **kwargs):
                return AIMessage(content="not-json")

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>failed</observation>")],
                "execution_evidence": {
                    "execution_id": "exec-schema",
                    "attempt_id": "attempt-schema",
                    "action_id": "action-schema",
                    "step_id": "step-schema",
                    "returncode": 1,
                    "execution_status": "failed",
                    "command_role": "primary",
                    "stderr_summary": "primary command failed",
                },
            }

        with tempfile.TemporaryDirectory() as data_path:
            wrapped = error_recovery_middleware(
                MalformedRecoveryJudge(), data_path=data_path
            )(execute)
            result = await wrapped({
                "task_id": "task-schema",
                "current_step_id": "step-schema",
            })

        self.assertEqual(
            result["recovery_decision"]["failure_type"],
            "recovery_judge_schema_error",
        )

    async def test_recovery_thinking_is_bound_only_to_deepseek_recovery_client(self):
        from bio_agents import bind_recovery_llm

        class Bindable:
            def __init__(self):
                self.calls = []
                self.bound = object()

            def bind(self, **kwargs):
                self.calls.append(kwargs)
                return self.bound

        shared = Bindable()
        recovery, policy = bind_recovery_llm(
            shared,
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            enabled=True,
            reasoning_effort="high",
        )
        non_deepseek = Bindable()
        unchanged, disabled_policy = bind_recovery_llm(
            non_deepseek,
            model="other-model",
            base_url="https://example.invalid",
            enabled=True,
            reasoning_effort="high",
        )

        self.assertIs(recovery, shared.bound)
        self.assertEqual(shared.calls, [{
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
            "temperature": None,
        }])
        self.assertTrue(policy["thinking_enabled"])
        self.assertEqual(policy["thinking"], {"type": "enabled"})
        self.assertEqual(policy["reasoning_effort"], "high")
        self.assertEqual(policy["scope"], "recovery_only")
        self.assertIs(unchanged, non_deepseek)
        self.assertEqual(non_deepseek.calls, [])
        self.assertFalse(disabled_policy["thinking_enabled"])
        self.assertEqual(disabled_policy["thinking"], {"type": "disabled"})

    async def test_generate_thinking_is_node_scoped_and_temperature_is_effective(self):
        class Bindable:
            def __init__(self):
                self.calls = []

            def bind(self, **kwargs):
                self.calls.append(kwargs)
                return self

            async def ainvoke(self, _messages):
                return AIMessage(content="ok")

        shared = Bindable()
        generate, policy = bio_agents.bind_generate_llm(
            shared,
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            enabled=True,
            reasoning_effort="high",
        )
        self.assertIs(generate, shared)
        self.assertEqual(shared.calls, [{
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
            "temperature": None,
        }])
        self.assertEqual(policy["scope"], "generate_only")
        self.assertTrue(policy["thinking_enabled"])

        planner_correction_llm = Bindable()
        planner_correction, planner_policy = (
            bio_agents.bind_planner_correction_llm(
                planner_correction_llm,
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                enabled=True,
                reasoning_effort="high",
            )
        )
        self.assertIs(planner_correction, planner_correction_llm)
        self.assertEqual(planner_policy["scope"], "planner_correction_only")
        self.assertTrue(planner_policy["thinking_enabled"])
        self.assertEqual(planner_correction_llm.calls, [{
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
            "temperature": None,
        }])
        self.assertFalse(bio_agents.planner_correction_requires_thinking({
            "repeat_count": 0,
            "correction_progress": "new_failure_objective",
        }))
        self.assertTrue(bio_agents.planner_correction_requires_thinking({
            "repeat_count": 1,
            "correction_progress": "no_structural_progress",
        }))

        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = ""
        agent.llm = Bindable()
        agent.llm_provider_is_deepseek = True
        agent.llm_timeout_seconds = 10
        agent.llm_readonly_timeout_seconds = 10
        agent.llm_context_limit_tokens = 200_000
        with patch.object(bio_agents, "DEEPSEEK_TEMPERATURE", 0.6):
            await agent._ainvoke_llm(
                [HumanMessage(content="ordinary DeepSeek request")],
                stage="consultant",
                temperature=0.1,
            )
        self.assertEqual(agent.llm.calls, [{"temperature": 0.6}])

        planner_llm = Bindable()
        await agent._ainvoke_llm(
            [HumanMessage(content="Planner request")],
            stage="planner",
            temperature=0.1,
            llm_override=planner_llm,
        )
        self.assertEqual(planner_llm.calls, [{"temperature": 0.1}])

        thinking_llm = Bindable()
        await agent._ainvoke_llm(
            [HumanMessage(content="Generate thinking request")],
            stage="generate",
            temperature=0.1,
            llm_override=thinking_llm,
            llm_policy=policy,
        )
        self.assertEqual(thinking_llm.calls, [])


if __name__ == "__main__":
    unittest.main()

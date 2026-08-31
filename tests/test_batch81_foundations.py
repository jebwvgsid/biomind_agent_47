import asyncio
import hashlib
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_core.context_lineage import redact_payload
from agent_core.recovery import recovery_action_route
from agent_core.middleware import error_recovery_middleware
from agent_core.plan_tracker import (
    apply_recovery_decision,
    build_plan_state,
    classify_attempt_resume,
    record_provenance_event,
)
from execution.executor import run_script_path
from bio_agents import BioAnalystAgent


class ScriptSourceOfTruthTests(unittest.TestCase):
    def test_executor_runs_exact_saved_script_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "executed.sh"
            marker = root / "marker.txt"
            script.write_text(f"#!/bin/bash\nprintf ok > {marker}\n", encoding="utf-8")
            script.chmod(0o700)
            digest = hashlib.sha256(script.read_bytes()).hexdigest()

            stdout, stderr, returncode = asyncio.run(
                run_script_path(script, expected_sha256=digest)
            )

            self.assertEqual(returncode, 0, stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "ok")
            self.assertFalse((root / "temp_script.sh").exists())

    def test_executor_rejects_script_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "executed.sh"
            script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            stdout, stderr, returncode = asyncio.run(
                run_script_path(script, expected_sha256="0" * 64)
            )
            self.assertEqual(stdout, "")
            self.assertEqual(returncode, 126)
            self.assertIn("SCRIPT_HASH_MISMATCH", stderr)

    def test_script_versions_are_immutable_and_parented(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = tmp
            state = DurableAttemptTests._state()
            generated = agent._persist_script_version(
                "task-a", state, "step-1", "action-1", "generated", "printf ok\n",
                extension=".sh", mutation_reason="model_generation",
            )
            approved = agent._persist_script_version(
                "task-a", state, "step-1", "action-1", "approved", "printf ok\n",
                extension=".sh", parent_script_ref=generated, mutation_reason="approved",
            )
            self.assertEqual(approved["parent_script_ref"]["sha256"], generated["sha256"])
            self.assertEqual(generated["producer"], "controller")
            self.assertEqual(approved["producer"], "controller")
            with self.assertRaisesRegex(ValueError, "Immutable script version"):
                agent._persist_script_version(
                    "task-a", state, "step-1", "action-1", "approved", "printf changed\n",
                    extension=".sh", parent_script_ref=generated, mutation_reason="edited",
                )


class DurableAttemptTests(unittest.TestCase):
    @staticmethod
    def _state():
        return build_plan_state(
            "task-a",
            "- [ ] Step 1 | Action: EXECUTE | Done: output exists",
            [{"step_id": "step-1", "title": "Step 1", "action": "EXECUTE", "success_criteria": ["output exists"], "status": "running"}],
        )

    def test_attempt_lifecycle_and_single_active_attempt(self):
        state = self._state()
        action = record_provenance_event(state, "action_bound", {
            "action": {"action_id": "action-1", "step_id": "step-1", "type": "execute"},
        })
        first = record_provenance_event(state, "attempt_created", {"action_id": action["action_id"]})
        record_provenance_event(state, "attempt_state_changed", {
            "attempt_id": first["attempt_id"], "status": "SCRIPT_READY",
        })
        with self.assertRaisesRegex(ValueError, "active attempt"):
            record_provenance_event(state, "attempt_created", {"action_id": action["action_id"]})

    def test_resume_classification_never_blindly_reruns_unknown_attempt(self):
        state = self._state()
        action = record_provenance_event(state, "action_bound", {
            "action": {"action_id": "action-1", "step_id": "step-1", "type": "execute"},
        })
        attempt = record_provenance_event(state, "attempt_created", {"action_id": action["action_id"]})
        record_provenance_event(state, "attempt_state_changed", {
            "attempt_id": attempt["attempt_id"],
            "status": "UNKNOWN_AFTER_CRASH",
        })
        decision = classify_attempt_resume(state)
        self.assertEqual(decision["route"], "reconcile_attempt")
        self.assertFalse(decision["create_new_attempt"])

    def test_illegal_attempt_transition_is_rejected(self):
        state = self._state()
        action = record_provenance_event(state, "action_bound", {
            "action": {"action_id": "action-1", "step_id": "step-1", "type": "execute"},
        })
        attempt = record_provenance_event(state, "attempt_created", {"action_id": action["action_id"]})
        with self.assertRaisesRegex(ValueError, "Invalid attempt transition"):
            record_provenance_event(state, "attempt_state_changed", {
                "attempt_id": attempt["attempt_id"], "status": "COMPLETED",
            })

    def test_running_attempt_is_monitored_and_not_reallocated(self):
        state = self._state()
        action = record_provenance_event(state, "action_bound", {
            "action": {"action_id": "action-1", "step_id": "step-1", "type": "execute"},
        })
        attempt = record_provenance_event(state, "attempt_created", {"action_id": action["action_id"]})
        for status in ("SCRIPT_READY", "APPROVED", "STARTING"):
            record_provenance_event(state, "attempt_state_changed", {
                "attempt_id": attempt["attempt_id"], "status": status,
            })
        record_provenance_event(state, "attempt_state_changed", {
            "attempt_id": attempt["attempt_id"], "status": "RUNNING", "pid": os.getpid(),
        })
        decision = classify_attempt_resume(state)
        self.assertEqual(decision["route"], "monitor_running_attempt")
        self.assertFalse(decision["create_new_attempt"])

    def test_exited_and_validated_attempts_resume_without_rerun(self):
        state = self._state()
        action = record_provenance_event(state, "action_bound", {
            "action": {"action_id": "action-1", "step_id": "step-1", "type": "execute"},
        })
        attempt = record_provenance_event(state, "attempt_created", {"action_id": action["action_id"]})
        for status in ("SCRIPT_READY", "APPROVED", "STARTING", "RUNNING", "PROCESS_EXITED"):
            record_provenance_event(state, "attempt_state_changed", {
                "attempt_id": attempt["attempt_id"], "status": status,
            })
        self.assertEqual(classify_attempt_resume(state)["route"], "supervisor")
        record_provenance_event(state, "execution_finished", {
            "attempt_id": attempt["attempt_id"],
            "execution_record": {"execution_id": "exec-1", "returncode": 0},
        })
        record_provenance_event(state, "validation_recorded", {
            "attempt_id": attempt["attempt_id"],
            "validation": {"validation_id": "validation-1", "status": "passed"},
        })
        self.assertEqual(classify_attempt_resume(state)["route"], "transition_guard")

    def test_old_plan_attempt_is_not_resumed_after_plan_patch(self):
        state = self._state()
        action = record_provenance_event(state, "action_bound", {
            "action": {"action_id": "action-1", "step_id": "step-1", "type": "execute"},
        })
        record_provenance_event(state, "attempt_created", {"action_id": action["action_id"]})
        state["plan_version"] = "plan_v2"
        decision = classify_attempt_resume(state)
        self.assertEqual(decision["route"], "none")
        self.assertTrue(decision["create_new_attempt"])


class RecoverySingleWriterTests(unittest.TestCase):
    @staticmethod
    def _state():
        state = DurableAttemptTests._state()
        state["plan_version"] = "plan_v1"
        return state

    @staticmethod
    def _decision(decision_id="decision-1", incident_id="incident-1"):
        return {
            "decision_id": decision_id,
            "idempotency_key": decision_id,
            "incident_id": incident_id,
            "decision_owner": "RecoveryPolicy",
            "kind": "RETRY_CURRENT_ACTION",
            "reason": "same-step retry",
            "reason_code": "test",
            "step_id": "step-1",
            "plan_version": "plan_v1",
            "consume_budget": "execution_retry_count",
            "next_route": "generate",
        }

    def test_decision_replay_is_idempotent_and_incident_has_one_commit(self):
        state = self._state()
        first = apply_recovery_decision(state, self._decision())
        second = apply_recovery_decision(state, self._decision())
        self.assertTrue(first["applied"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(state["task_state"]["budgets"]["execution_retry_count"], 1)
        statuses = [
            item["apply_status"]
            for item in state["task_state"]["recovery_state"]["decision_audit"]
            if item["decision_id"] == "decision-1"
        ]
        self.assertEqual(statuses, ["created", "validated", "applied"])
        with self.assertRaisesRegex(ValueError, "incident already has a committed decision"):
            apply_recovery_decision(state, self._decision("decision-2", "incident-1"))
        self.assertEqual(statuses, ["created", "validated", "applied"])

    def test_stale_plan_decision_is_rejected_without_budget_use(self):
        state = self._state()
        before = json.dumps(state, sort_keys=True)
        decision = self._decision()
        decision["plan_version"] = "plan_v0"
        with self.assertRaisesRegex(ValueError, "plan_version is stale"):
            apply_recovery_decision(state, decision)
        self.assertEqual(json.dumps(state, sort_keys=True), before)
        self.assertEqual(state["task_state"]["budgets"]["execution_retry_count"], 0)

    def test_active_middleware_does_not_apply_plan_decisions(self):
        source = inspect.getsource(error_recovery_middleware)
        self.assertNotIn("apply_recovery_decision(", source)

    def test_transition_guard_is_the_only_runtime_commit_callsite(self):
        source = inspect.getsource(BioAnalystAgent)
        self.assertEqual(source.count("apply_recovery_decision("), 1)
        guard = inspect.getsource(BioAnalystAgent._apply_transition_guard)
        self.assertIn("apply_recovery_decision(", guard)

    def test_reconsult_and_revalidate_have_explicit_routes(self):
        self.assertEqual(recovery_action_route("RECONSULT")["next_step"], "consultant")
        self.assertEqual(recovery_action_route("REVALIDATE")["next_step"], "supervisor")

    def test_plan_schema_version_is_not_plan_revision(self):
        state = self._state()
        self.assertEqual(state["plan_version"], "plan_v1")
        self.assertEqual(state["plan_schema_version"], state["schema_version"])


class UnifiedRedactionTests(unittest.TestCase):
    def test_cookie_url_credential_and_ssh_key_are_redacted(self):
        payload = {
            "text": (
                "Cookie: sid=abcdef123456; token=qwerty123456\n"
                "https://alice:password@example.org/path?access_token=secret123456\n"
                "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-body\n-----END OPENSSH PRIVATE KEY-----"
            )
        }
        redacted, manifest = redact_payload(payload)
        text = redacted["text"]
        self.assertNotIn("abcdef123456", text)
        self.assertNotIn("password", text)
        self.assertNotIn("secret123456", text)
        self.assertNotIn("secret-body", text)
        self.assertTrue(manifest)

    def test_redaction_never_mutates_the_executed_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "executed.sh"
            original = "#!/bin/bash\nexport ACCESS_TOKEN=local-secret-value\nprintf ok\n"
            script.write_text(original, encoding="utf-8")
            redacted, _ = redact_payload({"script_preview": original})
            self.assertNotIn("local-secret-value", redacted["script_preview"])
            self.assertEqual(script.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()

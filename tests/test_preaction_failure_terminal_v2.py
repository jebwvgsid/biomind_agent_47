from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_core.managed_artifacts import managed_action_fingerprint
from agent_core.plan_tracker import record_preaction_contract_failure
from execution.profiling import RunProfiler
from supervisor import RuleEngine, SupervisorNode


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "session_1786694833_preaction_replay.json"
)


def _violation(code: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "owner": "GENERATED_ACTION",
        "stage": "pre_action_contract",
        "field_path": "argv[2]",
        "message": message,
    }


class DurablePreactionFailureTests(unittest.TestCase):
    def test_primary_failure_is_first_write_wins_and_termination_is_separate(self) -> None:
        state = {
            "task_state": {"status": "running"},
            "steps": [{"step_id": "step-1", "status": "running"}],
        }
        first = record_preaction_contract_failure(
            state,
            _violation(
                "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
                "Managed placeholders must occupy a complete argv/env value.",
            ),
            error_fingerprint="error-first",
            candidate_fingerprint="candidate-first",
        )
        terminal = record_preaction_contract_failure(
            state,
            _violation(
                "MANAGED_RUNTIME_PLACEHOLDER_UNSUPPORTED",
                "Runtime placeholders are unsupported.",
            ),
            error_fingerprint="error-later",
            candidate_fingerprint="candidate-later",
            repair_attempt_count=1,
            termination_reason="repair_no_progress",
        )

        for field in ("error_code", "owner", "stage", "fingerprint", "candidate_fingerprint"):
            self.assertEqual(terminal[field], first[field])
        self.assertEqual(terminal["last_candidate_fingerprint"], "candidate-later")
        self.assertEqual(terminal["last_error_fingerprint"], "error-later")
        self.assertEqual(terminal["repair_attempt_count"], 1)
        self.assertEqual(terminal["termination_reason"], "repair_no_progress")
        self.assertEqual(
            terminal["latest_failure"]["error_code"],
            "MANAGED_RUNTIME_PLACEHOLDER_UNSUPPORTED",
        )
        self.assertEqual(
            terminal["terminal_failure"]["candidate_fingerprint"],
            "candidate-later",
        )
        self.assertIs(terminal["execution_started"], False)
        self.assertEqual(state["task_state"]["status"], "failed")
        self.assertEqual(state["steps"][0]["status"], "failed")

    def test_incident_replay_uses_sanitized_copy_and_preserves_first_error(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertIs(fixture["sanitized"], True)
        self.assertEqual(fixture["source_session"], "session_1786694833")
        self.assertEqual(
            managed_action_fingerprint(fixture["initial_action"]),
            managed_action_fingerprint(fixture["repair_action"]),
        )
        self.assertEqual(
            fixture["fingerprints"]["initial_candidate"],
            fixture["fingerprints"]["repair_candidate"],
        )

        state = {
            "task_state": {"status": "running"},
            "steps": [{"step_id": "step-1", "status": "running"}],
        }
        failure = record_preaction_contract_failure(
            state,
            fixture["violation"],
            error_fingerprint=fixture["fingerprints"]["error"],
            candidate_fingerprint=fixture["fingerprints"]["initial_candidate"],
        )
        decision = fixture["terminal_recovery_decision"]
        failure = record_preaction_contract_failure(
            state,
            fixture["violation"],
            error_fingerprint=fixture["fingerprints"]["error"],
            candidate_fingerprint=fixture["fingerprints"]["repair_candidate"],
            repair_attempt_count=1,
            termination_reason=decision["termination_reason"],
        )

        self.assertEqual(failure["error_code"], "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED")
        self.assertEqual(failure["owner"], "GENERATED_ACTION")
        self.assertEqual(failure["stage"], "pre_action_contract")
        self.assertEqual(failure["termination_reason"], "repair_no_progress")
        self.assertIs(failure["execution_started"], False)
        self.assertEqual(decision["commands"], [])

    def test_run_metrics_prioritizes_durable_preaction_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_id = "session_preaction_metrics"
            task_dir = root / task_id
            task_dir.mkdir(parents=True)
            failure = {
                "error_code": "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
                "owner": "GENERATED_ACTION",
                "stage": "pre_action_contract",
                "message": "Managed placeholders must occupy a complete argv/env value.",
                "fingerprint": "error-first",
                "candidate_fingerprint": "candidate-first",
                "last_candidate_fingerprint": "candidate-later",
                "last_error_fingerprint": "error-later",
                "execution_started": False,
                "termination_reason": "repair_no_progress",
                "repair_attempt_count": 1,
                "latest_failure": {
                    "error_code": "MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL",
                    "owner": "GENERATED_ACTION",
                    "stage": "pre_action_contract",
                    "message": "The repaired script still contains a managed binding.",
                    "fingerprint": "error-later",
                    "candidate_fingerprint": "candidate-later",
                },
                "terminal_failure": {
                    "error_code": "MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL",
                    "owner": "GENERATED_ACTION",
                    "stage": "pre_action_contract",
                    "message": "The repaired script still contains a managed binding.",
                    "fingerprint": "error-later",
                    "candidate_fingerprint": "candidate-later",
                    "termination_reason": "repair_no_progress",
                },
            }
            (task_dir / "plan_state.json").write_text(
                json.dumps({
                    "failure": failure,
                    "task_state": {
                        "status": "failed",
                        "status_reason": "The action-format repair budget is exhausted.",
                        "terminal_diagnostic": {
                            "error_code": "TASK_FAILED",
                            "error_owner": "legacy",
                        },
                    },
                }),
                encoding="utf-8",
            )
            profiler = RunProfiler(root)
            profiler.start_task(task_id)
            profiler.finish_task(task_id, "failed")
            snapshot = profiler.snapshot(task_id)

        self.assertEqual(snapshot["failure"]["error_code"], failure["error_code"])
        self.assertEqual(snapshot["failure"]["owner"], "GENERATED_ACTION")
        self.assertEqual(snapshot["failure"]["stage"], "pre_action_contract")
        self.assertEqual(snapshot["failure"]["termination_reason"], "repair_no_progress")
        self.assertIs(snapshot["failure"]["execution_started"], False)
        self.assertEqual(
            snapshot["failure"]["terminal_failure"]["error_code"],
            "MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL",
        )
        self.assertEqual(snapshot["summary"]["commands"], 0)
        self.assertEqual(
            snapshot["final_result"]["terminal_diagnostic"]["error_code"],
            "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
        )
        self.assertIn(
            "corrective_terminal=MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL",
            snapshot["final_result"]["output_result"],
        )
        self.assertIn(
            "termination_reason=repair_no_progress",
            snapshot["final_result"]["output_result"],
        )


class NoExecutionSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_preaction_terminal_failure_skips_execution_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(object(), output_dir=tmpdir, use_llm_review=True)
            node._llm_reviewer.review = AsyncMock(
                side_effect=AssertionError("scientific reviewer must not run")
            )
            failure = {
                "error_code": "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
                "owner": "GENERATED_ACTION",
                "stage": "pre_action_contract",
                "message": "Managed placeholders must occupy a complete argv/env value.",
                "execution_started": False,
                "termination_reason": "repair_no_progress",
            }
            with patch.object(
                RuleEngine,
                "run",
                side_effect=AssertionError("RuleEngine must not run"),
            ):
                result = await node.run({
                    "task_id": "session-no-execution",
                    "failure": failure,
                    "latest_execution": {},
                })

        node._llm_reviewer.review.assert_not_awaited()
        self.assertEqual(result["next_step"], "finish")
        self.assertEqual(result["task_status"], "failed")
        self.assertEqual(result["step_status"], "failed")
        self.assertEqual(result["required_next_action"], "GENERATED_ACTION_CONTRACT_ERROR")
        self.assertNotIn("validation_evidence", result)
        self.assertNotIn("returncode", json.dumps(result, default=str))
        serialized = json.dumps(result, default=str)
        self.assertNotIn("INCOMPLETE_EVIDENCE", serialized)
        self.assertNotIn("RUN_ALLOWED_PROBE", serialized)
        self.assertNotIn("diagnosing", serialized)


if __name__ == "__main__":
    unittest.main()

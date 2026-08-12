import json
import inspect
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from agent_core.recovery import (
    RecoveryDecisionKind,
    RecoveryPolicy,
    ValidationEvidence,
    ValidationStatus,
)
from supervisor import SupervisorNode
from bio_agents import BioAnalystAgent


class _Response:
    def __init__(self, content: str):
        self.content = content


class _CountingLLM:
    def __init__(self, content: str = ""):
        self.content = content or json.dumps({
            "assessment": "CONCERN",
            "concerns": ["advisory concern only"],
            "suggestions": ["inspect if contract evidence is incomplete"],
            "confidence": 0.5,
        })
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _Response(self.content)


class _FailingLLM:
    async def ainvoke(self, messages):
        raise RuntimeError("advisory unavailable")


def _structured_state(
    *,
    returncode: int = 0,
    command_role: str = "analysis_execution",
    stdout: str = "",
    stderr: str = "",
    attempt_id: str = "attempt-1",
):
    observation = (
        "<observation>\n[Bash] ❌ Failed\nSTDOUT:\nlegacy display\n"
        "STDERR:\nlegacy display error\n</observation>"
    )
    return {
        "task_id": "validation-task",
        "current_step_id": "step-1",
        "retry_count": 7,
        "messages": [
            AIMessage(content="<execute>tool run</execute>"),
            HumanMessage(content=observation),
        ],
        "execution_evidence": {
            "execution_id": "execution-1",
            "attempt_id": attempt_id,
            "action_id": "action-1",
            "step_id": "step-1",
            "command": "tool run",
            "command_role": command_role,
            "returncode": returncode,
            "execution_status": "succeeded" if returncode == 0 else "failed",
            "stdout_summary": stdout,
            "stderr_summary": stderr,
        },
    }


class SupervisorValidationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_active_supervisor_controller_has_no_legacy_route_or_budget_writes(self):
        source = inspect.getsource(BioAnalystAgent._build_graph)
        active = source.split("async def supervisor(state", 1)[1].split(
            "async def _legacy_supervisor_controller", 1
        )[0]
        router = source.split("def supervisor_router(state)", 1)[1].split(
            "workflow.add_conditional_edges(\"supervisor\"", 1
        )[0]

        self.assertNotIn('update["next_step"]', active)
        self.assertNotIn('update["retry_count"]', active)
        self.assertNotIn("_commit_legacy_recovery_projection", active)
        self.assertNotIn("supervisor_verdict", router)
        self.assertNotIn('state.get("next_step")', router)
        self.assertIn("committed_recovery_route", router)

    async def test_supervisor_returns_evidence_without_routing_or_budget_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(_CountingLLM(), output_dir=tmpdir, use_llm_review=False)
            result = await node.run(_structured_state(returncode=9, stderr="boom"))

        self.assertEqual(set(result), {"messages", "validation_evidence", "supervisor_verdict"})
        self.assertNotIn("next_step", result)
        self.assertNotIn("retry_count", result)
        self.assertNotIn("recovery_decision", result)
        self.assertEqual(result["validation_evidence"]["status"], ValidationStatus.INVALID.value)
        self.assertEqual(result["supervisor_verdict"], "FAIL")

    async def test_structured_returncode_wins_over_legacy_observation_emoji(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(_CountingLLM(), output_dir=tmpdir, use_llm_review=False)
            result = await node.run(_structured_state(returncode=0, stdout="completed"))

        evidence = ValidationEvidence.from_mapping(result["validation_evidence"])
        self.assertEqual(evidence.returncode_check["returncode"], 0)
        self.assertIn(evidence.status, {ValidationStatus.VALID.value, ValidationStatus.VALID_WITH_WARNINGS.value})
        self.assertEqual(evidence.evidence_source, "execution_evidence")
        self.assertTrue(evidence.evidence_complete)

    async def test_persisted_execution_record_exit_code_is_structured_evidence(self):
        state = _structured_state(returncode=0, stdout="renderer usage")
        evidence = dict(state["execution_evidence"])
        evidence["exit_code"] = evidence.pop("returncode")
        evidence["raw_stdout"] = evidence.pop("stdout_summary")
        evidence["raw_stderr"] = evidence.pop("stderr_summary")
        state["execution_evidence"] = evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(_CountingLLM(), output_dir=tmpdir, use_llm_review=False)
            result = await node.run(state)

        validation = ValidationEvidence.from_mapping(result["validation_evidence"])
        self.assertEqual(validation.returncode_check["returncode"], 0)
        self.assertEqual(validation.evidence_source, "execution_evidence")
        self.assertTrue(validation.evidence_complete)

    async def test_legacy_observation_is_migrated_as_incomplete(self):
        state = {
            "task_id": "legacy-task",
            "current_step_id": "step-1",
            "messages": [
                AIMessage(content="<execute>false</execute>"),
                HumanMessage(content="<observation>[Bash] ❌ Failed\nSTDERR:\nboom</observation>"),
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(_CountingLLM(), output_dir=tmpdir, use_llm_review=False)
            result = await node.run(state)

        evidence = result["validation_evidence"]
        self.assertEqual(evidence["status"], ValidationStatus.INCOMPLETE_EVIDENCE.value)
        self.assertEqual(evidence["evidence_source"], "legacy_observation")
        self.assertFalse(evidence["evidence_complete"])
        self.assertTrue(evidence["migration_warnings"])

    async def test_probe_skips_formal_artifact_contract_and_deprecated_scan(self):
        state = _structured_state(
            command_role="capability_probe",
            returncode=0,
            stdout="usage: tool --help\nDeprecated compatibility option",
        )
        state["expected_artifacts"] = ["missing-formal-result.tsv"]
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(_CountingLLM(), output_dir=tmpdir, use_llm_review=False)
            result = await node.run(state)

        evidence = result["validation_evidence"]
        self.assertEqual(evidence["status"], ValidationStatus.VALID.value)
        self.assertEqual(evidence["artifact_results"], [])
        self.assertFalse(any("deprecat" in warning.lower() for warning in evidence["warnings"]))

    async def test_silent_command_with_satisfied_artifact_is_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.tsv"
            output.write_text("id\tvalue\na\t1\n", encoding="utf-8")
            state = _structured_state(returncode=0)
            state["execution_evidence"]["cwd"] = tmpdir
            state["verification_checks"] = [{
                "check_type": "file_nonempty",
                "target": "result.tsv",
                "required": True,
                "criterion_id": "result_exists",
            }]
            node = SupervisorNode(_CountingLLM(), output_dir=tmpdir, use_llm_review=False)
            result = await node.run(state)

        evidence = result["validation_evidence"]
        self.assertEqual(evidence["status"], ValidationStatus.VALID.value)
        self.assertEqual(evidence["warnings"], [])
        self.assertTrue(evidence["artifact_results"][0]["satisfied"])

    async def test_llm_advisory_cannot_override_deterministic_valid_status(self):
        llm = _CountingLLM(json.dumps({
            "assessment": "CONCERN",
            "concerns": ["RETRY immediately"],
            "suggestions": ["retry"],
            "confidence": 0.9,
        }))
        state = _structured_state(returncode=0, stdout="scientific summary")
        state["validation_level"] = "scientific"
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(llm, output_dir=tmpdir, use_llm_review=True)
            result = await node.run(state)

        evidence = result["validation_evidence"]
        self.assertEqual(evidence["status"], ValidationStatus.VALID.value)
        self.assertEqual(evidence["llm_advisory"]["assessment"], "CONCERN")
        self.assertTrue(evidence["llm_advisory_conflict"])
        self.assertNotIn("next_step", result)

    async def test_same_attempt_is_idempotent_and_does_not_repeat_llm_review(self):
        llm = _CountingLLM()
        state = _structured_state(returncode=0, stdout="scientific summary")
        state["validation_level"] = "scientific"
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(llm, output_dir=tmpdir, use_llm_review=True)
            first = await node.run(state)
            second = await node.run({**state, "validation_evidence": first["validation_evidence"]})

        self.assertEqual(first["validation_evidence"]["validation_id"], second["validation_evidence"]["validation_id"])
        self.assertEqual(llm.calls, 1)

    async def test_llm_advisory_failure_does_not_crash_or_change_status(self):
        state = _structured_state(returncode=0, stdout="scientific summary")
        state["validation_level"] = "scientific"
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(_FailingLLM(), output_dir=tmpdir, use_llm_review=True)
            result = await node.run(state)

        evidence = result["validation_evidence"]
        self.assertEqual(evidence["status"], ValidationStatus.VALID.value)
        self.assertEqual(evidence["llm_advisory"]["assessment"], "UNKNOWN")

    def test_legacy_validation_migration_is_idempotent(self):
        state = {
            "task_id": "legacy-task",
            "current_step_id": "step-1",
            "supervisor_verdict": "RETRY",
        }
        first = ValidationEvidence.from_legacy_state(state)
        second = ValidationEvidence.from_legacy_state({
            **state,
            "validation_evidence": first.to_dict(),
        })

        self.assertEqual(first.validation_id, second.validation_id)
        self.assertEqual(first.status, ValidationStatus.INCOMPLETE_EVIDENCE.value)
        self.assertEqual(first.migration_source, "legacy_supervisor_state")

    def test_legacy_state_accepts_persisted_exit_code_field(self):
        evidence = ValidationEvidence.from_legacy_state({
            "task_id": "legacy-execution-record",
            "supervisor_verdict": "PASS",
            "execution_evidence": {
                "execution_id": "execution-1",
                "attempt_id": "attempt-1",
                "step_id": "step-1",
                "exit_code": 0,
                "command": "true",
            },
        })

        self.assertTrue(evidence.evidence_complete)
        self.assertEqual(evidence.status, ValidationStatus.VALID.value)
        self.assertEqual(evidence.returncode_check["returncode"], 0)

    async def test_recovery_policy_interprets_invalid_evidence_after_supervisor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            node = SupervisorNode(_CountingLLM(), output_dir=tmpdir, use_llm_review=False)
            state = _structured_state(returncode=0)
            state["verification_checks"] = [{
                "check_type": "file_nonempty",
                "target": "missing.tsv",
                "required": True,
            }]
            result = await node.run(state)

        decision = RecoveryPolicy().decide(
            execution_evidence=state["execution_evidence"],
            validation_evidence=result["validation_evidence"],
            action_id="action-1",
            step_id="step-1",
            attempt_id="attempt-1",
        )
        self.assertEqual(decision.kind, RecoveryDecisionKind.RETRY_CURRENT_ACTION.value)


if __name__ == "__main__":
    unittest.main()

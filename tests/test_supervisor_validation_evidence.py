import json
import inspect
import re
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


class _Response:
    def __init__(self, content: str):
        self.content = content


class _CountingLLM:
    def __init__(self, content: str = ""):
        self.content = content or json.dumps({
            "assessment": "CONCERN",
            "disposition": "inspection_required",
            "concerns": ["advisory concern only"],
            "suggestions": ["inspect if contract evidence is incomplete"],
            "confidence": 0.5,
        })
        self.calls = 0
        self.last_visible = ""
        self.last_content = ""

    async def ainvoke(self, messages):
        self.calls += 1
        content = self.content
        visible = "\n".join(str(getattr(item, "content", "")) for item in messages)
        self.last_visible = visible
        match = re.search(
            r"<completion_report>\n(.*?)\n</completion_report>",
            visible,
            re.DOTALL,
        )
        if match:
            report = json.loads(match.group(1))
            decision = json.loads(content)
            decision["report_id"] = report["report_id"]
            if decision.get("disposition") == "completion_accepted":
                decision["issue_dispositions"] = []
            else:
                rows = list(report.get("predicate_results") or [])
                issue_ids = [
                    row["result_id"]
                    for row in rows
                    if row.get("passed") is not True
                ] or [
                    rows[0]["result_id"]
                    if rows
                    else report["scope"]["validation_id"]
                ]
                decision["issue_dispositions"] = [{
                    "issue_id": issue_id,
                    "disposition": decision["disposition"],
                    "explanation": "fixture consumed the current report issue",
                } for issue_id in issue_ids]
            content = json.dumps(decision)
        self.last_content = content
        return _Response(content)


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



if __name__ == "__main__":
    unittest.main()

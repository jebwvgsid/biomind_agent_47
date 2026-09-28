from supervisor import returncode_from_observation
from supervisor import root_cause_check_results
from supervisor import verification_checks_from_state
from supervisor import extract_stream
import json
import tempfile
import unittest
import sys
import types
from pathlib import Path
from unittest.mock import patch

try:
    from langchain_core.messages import AIMessage, HumanMessage  # type: ignore  # noqa: F401
except (ImportError, ModuleNotFoundError):
    langchain_core = types.ModuleType("langchain_core")
    messages_module = types.ModuleType("langchain_core.messages")

    class BaseMessage:
        def __init__(self, content: str = ""):
            self.content = content

    class AIMessage(BaseMessage):
        pass

    class HumanMessage(BaseMessage):
        pass

    class SystemMessage(BaseMessage):
        pass

    messages_module.BaseMessage = BaseMessage
    messages_module.AIMessage = AIMessage
    messages_module.HumanMessage = HumanMessage
    messages_module.SystemMessage = SystemMessage
    sys.modules.setdefault("langchain_core", langchain_core)
    sys.modules["langchain_core.messages"] = messages_module

from supervisor import CheckResult, RuleEngine, Verdict, bound_supervisor_contract


def _review_json(verdict: str, reason: str = "review") -> str:
    return json.dumps({
        "verdict_recommendation": verdict,
        "evidence_findings": [],
        "proposed_requirements": [],
        "failure_owner": None,
        "reason": reason,
        "next_action_recommendation": "continue",
    })


class _Response:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str = _review_json("RETRY", "retry")):
        self.content = content

    async def ainvoke(self, messages):
        return _Response(self.content)


def _state(code: str, observation: str, retry_count: int = 0):
    returncode = returncode_from_observation(observation)
    return {
        "task_id": "task_supervisor",
        "retry_count": retry_count,
        "current_step_id": "step-1",
        "execution_evidence": {
            "execution_id": f"exec-{abs(hash((code, observation))) % 100000}",
            "attempt_id": f"attempt-{abs(hash((code, observation))) % 100000}",
            "action_id": "action-1",
            "step_id": "step-1",
            "command": code,
            "command_role": "analysis_execution",
            "returncode": returncode,
            "execution_status": "succeeded" if returncode == 0 else "failed",
            "stdout_summary": extract_stream(observation, "STDOUT"),
            "stderr_summary": extract_stream(observation, "STDERR"),
            "validation": {
                "passed": 'status="failed"' not in observation,
                "message": "Post-run validation failed." if 'status="failed"' in observation else "",
            },
        },
        "messages": [
            AIMessage(content=f"<execute>{code}</execute>"),
            HumanMessage(content=observation),
        ],
    }


class SupervisorTests(unittest.IsolatedAsyncioTestCase):

    def test_renderer_capability_probe_ignores_deprecated_source_text(self):
        checks = RuleEngine.run(
            "python /skills/render_workflow.py --help",
            "--allow-old-mode  Deprecated compatibility option",
            "",
            0,
            command_role="capability_probe",
        )

        self.assertFalse(any(check.rule == "SilentFailureDetector" for check in checks), checks)
        self.assertFalse(any(check.rule == "NonProgressCommandCheck" for check in checks), checks)



    def test_legacy_trap_root_cause_is_not_accepted_as_runtime_failure(self):
        rows = root_cause_check_results({
            "execution_evidence": {
                "returncode": 0,
                "root_cause_analysis": {
                    "status": "confirmed",
                    "failed_command": "trap 'printf [ERROR]' ERR",
                    "primary_error_message": "ERROR: Database check failed.",
                    "masked_failure": True,
                },
            }
        })

        self.assertEqual(rows[0].rule, "RootCauseEvidenceProvenance")
        self.assertEqual(rows[0].severity, "warning")
        self.assertNotIn("Inner command failed", rows[0].message)


    def test_supervisor_proposals_cannot_promote_optional_requirement(self):
        bounded = bound_supervisor_contract(
            {
                "proposed_requirements": [{
                    "requirement_id": "protein_fasta",
                    "level": "required",
                }],
            },
            [{
                "requirement_id": "protein_fasta",
                "role": "validation_input",
                "level": "optional_validation",
                "path": "",
            }],
        )

        self.assertEqual(bounded["contract_status"], "valid")
        self.assertEqual(bounded["skipped_optional_checks"], ["protein_fasta"])
        self.assertFalse(bounded["proposed_requirements"][0]["accepted"])
        self.assertIsNone(bounded["proposed_requirements"][0]["effective_level"])

    def test_environment_name_does_not_trigger_gatk_biocheck(self):
        results = RuleEngine.run(
            "conda run -n bio_gatk bash -c 'java -jar snpEff.jar -version'",
            "SnpEff 5.3a",
            "",
            0,
        )

        self.assertNotIn("BioCheck:gatk", {item.rule for item in results})















    def test_stdout_stderr_stream_extraction_does_not_bleed(self):
        obs = (
            "<observation>\n[Bash] ❌ Failed\nSTDOUT:\nout line\nSTDERR:\nerr line\n"
            "<validation_result level=\"basic\" status=\"failed\">\nvalidation\n</validation_result>\n"
            "</observation>"
        )

        self.assertEqual(extract_stream(obs, "STDOUT"), "out line")
        self.assertEqual(extract_stream(obs, "STDERR"), "err line")









    def test_narrative_recovery_check_is_not_reused_as_artifact_gate(self):
        checks = verification_checks_from_state({
            "current_step_id": "step-1",
            "execution_evidence": {"step_id": "step-1"},
            "verification_checks": [{
                "check_type": "content_pattern",
                "target": "old_step.sh",
                "expected": "Conditional block around line 20 must be identified",
                "required": True,
            }],
        })

        self.assertEqual(checks, [])

    def test_recovery_check_is_scoped_to_its_step(self):
        checks = verification_checks_from_state({
            "execution_evidence": {"step_id": "step-2"},
            "verification_checks": [{
                "check_type": "file_nonempty",
                "target": "step-1.out",
                "required": True,
                "_recovery_step_id": "step-1",
            }],
        })

        self.assertEqual(checks, [])





if __name__ == "__main__":
    unittest.main()

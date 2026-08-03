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

from supervisor import CheckResult, RuleEngine, SupervisorNode, Verdict, bound_supervisor_contract


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
    returncode = SupervisorNode._returncode_from_observation(observation)
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
            "stdout_summary": SupervisorNode._extract_stream(observation, "STDOUT"),
            "stderr_summary": SupervisorNode._extract_stream(observation, "STDERR"),
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
    async def test_structured_execution_evidence_is_authoritative_over_observation_streams(self):
        obs = (
            "<observation>\n[Bash] ❌ Failed\nSTDOUT:\ndisplay-only output\nSTDERR:\n"
            "<validation_result level=\"scientific\" status=\"failed\">\n"
            "validator summary only\n</validation_result>\n</observation>"
        )
        state = _state("tool build", obs, retry_count=0)
        state["execution_evidence"] = {
            "execution_id": "exec-structured",
            "command": "tool build",
            "returncode": 17,
            "stdout_summary": "ERROR: real tool failure",
            "stderr_summary": "",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM(), output_dir=tmpdir, use_llm_review=False)
            with patch.object(RuleEngine, "run", return_value=[]) as run_rules:
                await supervisor.run(state)

        _, stdout, stderr, returncode = run_rules.call_args.args
        self.assertEqual(stdout, "ERROR: real tool failure")
        self.assertEqual(stderr, "")
        self.assertEqual(returncode, 17)

    async def test_supervisor_invalid_result_contains_validation_facts_only(self):
        obs = "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\nERROR: build failed\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM(), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("tool build", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], "FAIL")
        self.assertEqual(result["validation_evidence"]["status"], "INVALID")
        for key in (
            "retry_reason", "failed_step_id", "failure_fingerprint",
            "failure_owner", "required_next_action", "recovery_scope", "evidence_status",
            "next_step", "retry_count",
        ):
            self.assertNotIn(key, result)

    def test_blocking_knowledge_conflict_does_not_patch_plan_without_revision_decision(self):
        row = CheckResult(
            "KnowledgeConflictCheck", False, "error", "runtime evidence conflicts", "probe first"
        )
        probe = SupervisorNode._retry_contract({}, [row])
        plan = SupervisorNode._retry_contract({
            "knowledge_conflict_summary": {
                "decisions": [{
                    "decision": "local_replan",
                    "requires_plan_revision": True,
                }],
            },
        }, [row])

        self.assertEqual(probe["required_next_action"], "PROBE")
        self.assertEqual(probe["failure_owner"], "DIAGNOSTIC")
        self.assertEqual(plan["required_next_action"], "PLAN_PATCH")
        self.assertEqual(plan["failure_owner"], "PLAN")

    def test_legacy_trap_root_cause_is_not_accepted_as_runtime_failure(self):
        rows = SupervisorNode._root_cause_check_results({
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

    def test_hard_override_explains_zero_outer_return_code(self):
        review = _review_json("PASS", "looks fine")
        repaired = SupervisorNode._enforce_review_hard_constraints(
            review,
            hard_failed=True,
            returncode=0,
            hard_failure_reasons=["HardFailureOutputDetector"],
        )

        decision = json.loads(repaired)
        self.assertEqual(decision["verdict_recommendation"], "RETRY")
        self.assertIn("HardFailureOutputDetector", repaired)
        self.assertIn("return code=0", repaired)

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

    async def test_supervisor_does_not_own_retry_or_escalation_budget(self):
        obs = "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\nboom\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM(), output_dir=tmpdir, max_retries=3, use_llm_review=False)

            first = await supervisor.run(_state("false", obs, retry_count=0))
            third = await supervisor.run(_state("false", obs, retry_count=2))
            escalated = await supervisor.run(_state("false", obs, retry_count=3))

        for result in (first, third, escalated):
            self.assertEqual(result["supervisor_verdict"], "FAIL")
            self.assertNotIn("retry_count", result)
            self.assertNotIn("next_step", result)

    async def test_retry_count_is_not_incremented_twice_after_execute_records_failure(self):
        obs = "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\nboom\n</observation>"
        state = _state("false", obs, retry_count=1)
        state["execution_evidence"] = {
            "execution_id": "exec-primary-1",
            "command": "false",
            "returncode": 1,
            "stderr_summary": "boom",
            "command_role": "primary",
            "retry_count_recorded": True,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(
                _FakeLLM(), output_dir=tmpdir, max_retries=3, use_llm_review=False
            )
            result = await supervisor.run(state)

        self.assertEqual(result["supervisor_verdict"], "FAIL")
        self.assertNotIn("retry_count", result)

    async def test_empty_output_warning_is_not_upgraded_to_retry_by_llm(self):
        obs = "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n\nSTDERR:\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: RETRY\n**Reason**: empty\n**Action**: retry"), output_dir=tmpdir)
            result = await supervisor.run(_state("python run_analysis.py", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.PASS.value)
        self.assertNotIn("retry_count", result)

    async def test_validated_silent_success_does_not_call_scientific_llm_retry(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n\nSTDERR:\n"
            "<validation_result level=\"scientific\" status=\"passed\">\n"
            "✅ Execution Validated: No critical errors found.\n"
            "</validation_result>\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: RETRY\n**Reason**: hallucinated\n**Action**: retry"), output_dir=tmpdir)
            result = await supervisor.run(_state("echo ok > report.md\nchmod +x reproduce.sh", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.PASS.value)
        self.assertNotIn("retry_count", result)

    async def test_expected_silent_file_write_is_pass_without_validation(self):
        obs = "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n\nSTDERR:\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: RETRY"), output_dir=tmpdir)
            result = await supervisor.run(_state("printf 'x\\n' > out.txt\nchmod +x run.sh", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.PASS.value)

    async def test_zero_byte_required_artifact_retries_then_escalates(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\n"
            "STDOUT:\n-rw-r--r-- 1 user group 0 Jul 03 10:00 external_lookup.tsv\n"
            "STDERR:\nexternal service returned no tabular rows\n"
            "</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, max_retries=2, use_llm_review=False)
            first = await supervisor.run(_state("ls -l external_lookup.tsv", obs, retry_count=0))
            escalated = await supervisor.run(_state("ls -l external_lookup.tsv", obs, retry_count=2))

        self.assertEqual(first["supervisor_verdict"], "FAIL")
        self.assertEqual(escalated["supervisor_verdict"], "FAIL")
        self.assertNotIn("retry_count", first)
        self.assertNotIn("next_step", escalated)
        self.assertIn("external_lookup.tsv", escalated["messages"][0].content)

    async def test_zero_byte_status_explained_attempt_is_allowed(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\n"
            "STDOUT:\n"
            "-rw-r--r-- 1 user group 0 Jul 03 10:00 external_lookup_attempt.tsv\n"
            "stage\tstatus\toutput_path\tnotes\n"
            "external_lookup\tstage_blocked_missing_dependency\tresults/external_lookup_attempt.tsv\t"
            "required input metadata missing; external service unavailable for this conditional branch\n"
            "STDERR:\n"
            "</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: RETRY"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("cat stage_status.tsv && ls -l external_lookup_attempt.tsv", obs, retry_count=0))

        self.assertNotEqual(result["supervisor_verdict"], "FAIL")
        self.assertNotIn("Required-looking artifact is empty: external_lookup_attempt.tsv", result["messages"][0].content)

    async def test_zero_byte_core_result_still_retries_without_external_reason(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\n"
            "STDOUT:\n"
            "-rw-r--r-- 1 user group 0 Jul 03 10:00 primary_results.tsv\n"
            "primary_analysis\tstage_blocked_empty_result\tresults/primary_results.tsv\tno usable analysis output\n"
            "STDERR:\n"
            "</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("ls -l primary_results.tsv && cat stage_status.tsv", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], "FAIL")
        self.assertIn("Required-looking artifact is empty: primary_results.tsv", result["messages"][0].content)

    async def test_empty_no_hits_artifact_is_allowed(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\n"
            "STDOUT:\n-rw-r--r-- 1 user group 0 Jul 03 10:00 no_hits.txt\n"
            "STDERR:\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: RETRY"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("python summarize_outputs.py", obs, retry_count=0))

        self.assertNotEqual(result["supervisor_verdict"], "FAIL")
        self.assertNotIn("EmptyArtifactCheck", result["messages"][0].content)

    async def test_llm_retry_cannot_upgrade_warning_without_hard_or_scientific_signal(self):
        obs = "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\nWARNING: no reads mapped\nSTDERR:\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(
                _FakeLLM("**Verdict**: RETRY\n**Reason**: retry anyway\n**Action**: retry"),
                output_dir=tmpdir,
                use_llm_review=True,
            )
            result = await supervisor.run(_state("python run_analysis.py", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.WARN.value)

    def test_warn_feedback_keeps_llm_action_for_generate(self):
        review = (
            "**Verdict**: WARN\n"
            "**Reason**: only created a shell script without running actual analysis.\n"
            "**Action**: Execute the generated reproduce.sh script."
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM(review), output_dir=tmpdir, use_llm_review=True)
            feedback = supervisor._build_feedback_message(
                [
                    CheckResult(
                        rule="IncompleteStep",
                        passed=False,
                        severity="warning",
                        message="Only created a shell script.",
                        suggestion="Execute it.",
                    )
                ],
                Verdict.WARN,
                review,
                retry_count=0,
            )

        self.assertIn("EXPERT DIAGNOSIS", feedback)
        self.assertIn("Execute the generated reproduce.sh script", feedback)

    async def test_non_progress_help_command_is_validation_warning_without_routing(self):
        obs = "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\nusage\nSTDERR:\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            analysis_step = _state("python run_analysis.py --help", obs, retry_count=0)
            analysis_step["plan_progress"] = "Plan checklist:\n- [>] Step 2: Run analysis\nNext step: Step 2: Run analysis"
            preflight_step = _state("python run_analysis.py --help", obs, retry_count=0)
            preflight_step["plan_progress"] = "Plan checklist:\n- [>] Step 1: Verify tool help\nNext step: Step 1: Verify tool help"

            retry_result = await supervisor.run(analysis_step)
            warn_result = await supervisor.run(preflight_step)

        self.assertEqual(retry_result["supervisor_verdict"], Verdict.WARN.value)
        self.assertEqual(warn_result["supervisor_verdict"], Verdict.WARN.value)

    async def test_non_progress_artifact_listing_is_not_upgraded_to_retry_when_validated(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n"
            "leaf_domains.bed\nleaf_boundaries.bed\ntad_domains_long.tsv\n"
            "STDERR:\n"
            "<validation_result level=\"scientific\" status=\"passed\">\n"
            "✅ Execution Validated: No critical errors found.\n"
            "</validation_result>\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("ls -l TADs/HiCExplorer/40000", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.WARN.value)
        self.assertNotIn("retry_count", result)

    async def test_non_progress_parent_listing_without_artifacts_is_warning(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n"
            "40000\n"
            "STDERR:\n"
            "<validation_result level=\"scientific\" status=\"passed\">\n"
            "✅ Execution Validated: No critical errors found.\n"
            "</validation_result>\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("ls -l TADs/HiCExplorer", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.WARN.value)

    def test_stdout_stderr_stream_extraction_does_not_bleed(self):
        obs = (
            "<observation>\n[Bash] ❌ Failed\nSTDOUT:\nout line\nSTDERR:\nerr line\n"
            "<validation_result level=\"basic\" status=\"failed\">\nvalidation\n</validation_result>\n"
            "</observation>"
        )

        self.assertEqual(SupervisorNode._extract_stream(obs, "STDOUT"), "out line")
        self.assertEqual(SupervisorNode._extract_stream(obs, "STDERR"), "err line")

    async def test_validation_failure_converts_success_to_retry(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\nfinished\n"
            "<validation_result level=\"basic\" status=\"failed\">\n"
            "❌ Status: FAILED\n\n🚨 Critical Failures:\n  • [Generic Error Check] command not found\n"
            "</validation_result>\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("python run_analysis.py", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], "FAIL")

    async def test_success_stdout_status_icons_do_not_change_exit_code(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n"
            "| stage | status |\n"
            "| optional_followup | ❌ skipped |\n"
            "STDERR:\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("python make_status_table.py", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.PASS.value)

    async def test_calder_not_available_stdout_is_hard_failure_even_with_exit_zero(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n"
            "=== Testing CALDER R package ===\nCALDER not available\n"
            "STDERR:\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("conda run -n hicexplore Rscript -e \"library(CALDER)\" || echo 'CALDER not available'", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], "FAIL")

    async def test_tool_user_error_stdout_is_hard_failure_even_with_exit_zero(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (23.4s)\nSTDOUT:\n"
            "A USER ERROR has occurred: sample-name-map is not a recognized option\n"
            "ERROR conda.cli.main_run:execute(142): `conda run ...` failed.\n"
            "STDERR:\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            result = await supervisor.run(_state("gatk CombineGVCFs 2>&1 | grep -iE 'error|exception' | head -20", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], "FAIL")
        self.assertIn("Tool reported A USER ERROR", result["messages"][0].content)
        self.assertIn("conda run wrapped command failed", result["messages"][0].content)

    async def test_hard_failure_is_invalid_independent_of_llm_advisory(self):
        obs = "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\nboom\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(
                _FakeLLM(_review_json("PASS", "looks fine")),
                output_dir=tmpdir,
                use_llm_review=True,
            )
            result = await supervisor.run(_state("false", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], "FAIL")
        self.assertIn("Process exited with code 1", result["messages"][0].content)

    async def test_scientific_success_cannot_be_retried_by_llm_advisory(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\nfinished\n"
            "<validation_result level=\"scientific\" status=\"passed\">\n"
            "✅ Execution Validated: No critical errors found.\n"
            "</validation_result>\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(
                _FakeLLM(_review_json("RETRY", "parameter mismatch")),
                output_dir=tmpdir,
                use_llm_review=True,
            )
            result = await supervisor.run(_state("python run_analysis.py --genome hg19", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.PASS.value)

    async def test_artifact_success_cannot_be_retried_by_llm_advisory(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n"
            "saved report.md and stage_status.tsv\n"
            "<validation_result level=\"artifact\" status=\"passed\">\n"
            "✅ Execution Validated: No critical errors found.\n"
            "</validation_result>\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(
                _FakeLLM(_review_json("RETRY", "report conclusion is not supported by the visible status table")),
                output_dir=tmpdir,
                use_llm_review=True,
            )
            result = await supervisor.run(_state("python make_report.py", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.PASS.value)

    async def test_required_artifact_check_overrides_success_exit_code(self):
        obs = (
            "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\nanalysis finished\n"
            "STDERR:\n</observation>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            state = _state("python run_analysis.py", obs, retry_count=0)
            state["verification_checks"] = [
                {"check_type": "file_nonempty", "target": "annotated.vcf", "required": True}
            ]
            result = await supervisor.run(state)

        self.assertEqual(result["supervisor_verdict"], "FAIL")
        self.assertIn("Artifact verification failed", result["messages"][0].content)

    def test_narrative_recovery_check_is_not_reused_as_artifact_gate(self):
        checks = SupervisorNode._verification_checks_from_state({
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
        checks = SupervisorNode._verification_checks_from_state({
            "execution_evidence": {"step_id": "step-2"},
            "verification_checks": [{
                "check_type": "file_nonempty",
                "target": "step-1.out",
                "required": True,
                "_recovery_step_id": "step-1",
            }],
        })

        self.assertEqual(checks, [])

    async def test_final_audit_requires_real_artifact_and_execution_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM(), output_dir=tmpdir, use_llm_review=False)
            state = {
                "task_id": "final-audit",
                "execution_evidence": {"command": "python run.py", "returncode": 0, "cwd": tmpdir},
                "verification_checks": [
                    {"check_type": "file_nonempty", "target": "report.md", "required": True}
                ],
            }
            missing = await supervisor.final_audit(state)
            Path(tmpdir, "report.md").write_text("# verified\n", encoding="utf-8")
            passed = await supervisor.final_audit(state)

        self.assertEqual(missing["supervisor_verdict"], "FAIL")
        self.assertEqual(passed["supervisor_verdict"], Verdict.PASS.value)
        self.assertNotIn("next_step", passed)

    async def test_masked_inner_failure_overrides_zero_wrapper_exit(self):
        obs = "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n\nSTDERR:\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            state = _state("tool input 2>&1 | tee run.log", obs, retry_count=0)
            state["execution_evidence"] = {
                "command": "tool input 2>&1 | tee run.log",
                "returncode": 0,
                "cwd": tmpdir,
                "stdout": "",
                "stderr": "",
                "root_cause_analysis": {
                    "status": "confirmed",
                    "masked_failure": True,
                    "failed_command": "tool input",
                    "primary_error_message": "ERROR: tool failed",
                    "root_cause_category": "masked_pipeline_failure",
                },
            }
            result = await supervisor.run(state)

        self.assertEqual(result["supervisor_verdict"], "FAIL")
        self.assertIn("inner command failed", result["messages"][0].content.lower())

    async def test_masked_optional_failure_is_warning_when_fallback_outputs_validate(self):
        obs = (
            '<observation>\n[Bash] ✅ Success\nSTDOUT:\nfallback summary written\nSTDERR:\n'
            '<validation_result level="artifact" status="passed">validated</validation_result>\n'
            '</observation>'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "summary.tsv"
            output.write_text("effect\tcount\nmissense\t3\n", encoding="utf-8")
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            state = _state("optional_tool || fallback_tool", obs, retry_count=0)
            state["execution_evidence"] = {
                "command": "optional_tool || fallback_tool",
                "returncode": 0,
                "cwd": tmpdir,
                "stdout": "fallback summary written",
                "stderr": "optional_tool: command not found",
                "validation": {"passed": True},
                "declared_outputs": ["summary.tsv"],
                "observed_outputs": [str(output)],
                "root_cause_analysis": {
                    "status": "confirmed",
                    "masked_failure": True,
                    "failed_command": "optional_tool",
                    "primary_error_message": "command not found",
                    "root_cause_category": "masked_pipeline_failure",
                },
            }
            result = await supervisor.run(state)

        self.assertEqual(result["supervisor_verdict"], Verdict.WARN.value)
        self.assertIn("bounded auxiliary/fallback evidence", result["messages"][0].content)


if __name__ == "__main__":
    unittest.main()

import sys
import types
import json
import tempfile
import unittest
from unittest.mock import patch

try:
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage  # type: ignore  # noqa: F401
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

from langchain_core.messages import HumanMessage

try:
    import langgraph.types  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    langgraph_module = types.ModuleType("langgraph")
    langgraph_types_module = types.ModuleType("langgraph.types")

    def interrupt(payload):
        return payload

    langgraph_types_module.interrupt = interrupt
    sys.modules.setdefault("langgraph", langgraph_module)
    sys.modules["langgraph.types"] = langgraph_types_module

from execution.confirmation import ConfirmationUI, RiskAnalyzer, RiskLevel
from execution.executor import run_bash_script, run_python_repl
from execution.shell_safety import analyze_shell_safety, shell_safety_error
import agent_core.middleware as middleware_module
from agent_core.middleware import (
    HITLMiddleware,
    _current_step_plan_constraints,
    classify_execution_failure,
    error_recovery_middleware,
)
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.knowledge_conflicts import ConflictResolutionDecision, KnowledgeConflict


class ShellSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_recovery_projects_current_step_constraints_not_full_plan(self):
        with tempfile.TemporaryDirectory() as root:
            save_plan_state(root, "task-a", {
                "task_id": "task-a",
                "steps": [{
                    "step_id": "step-1",
                    "status": "running",
                    "required_constraints": ["must use the declared reference"],
                    "environment_contract": "bio_gatk",
                }],
            })
            constraints = _current_step_plan_constraints(
                {"current_plan": "X" * 20_000}, root, "task-a", "step-1"
            )

        self.assertIn("required_constraints: must use the declared reference", constraints)
        self.assertIn("environment_contract: bio_gatk", constraints)
        self.assertTrue(all("X" * 100 not in item for item in constraints))

    def test_allows_reading_shell_startup_files(self):
        code = "sed -n '1,120p' ~/.bashrc"

        self.assertEqual(analyze_shell_safety(code, "Bash"), [])
        self.assertEqual(shell_safety_error(code, "Bash"), "")

    def test_detects_shell_startup_file_mutation(self):
        code = "echo 'python bio_agents.py' >> ~/.bashrc"

        violations = analyze_shell_safety(code, "Bash")

        self.assertTrue(any(item.code == "mutates_shell_startup" for item in violations))
        self.assertIn("blocked", shell_safety_error(code, "Bash"))

    def test_detects_source_shell_startup_file(self):
        code = "source ~/.bashrc"

        violations = analyze_shell_safety(code, "Bash")

        self.assertTrue(any(item.code == "source_shell_startup" for item in violations))

    def test_detects_unsafe_stty_and_python_input(self):
        stty_violations = analyze_shell_safety("stty -echo raw", "Bash")
        input_violations = analyze_shell_safety("name = input('name: ')", "Python")

        self.assertTrue(any(item.code == "unsafe_terminal_mode" for item in stty_violations))
        self.assertTrue(any(item.code == "interactive_python_input" for item in input_violations))

    def test_blocks_cooler_balance_as_generated_preprocessing(self):
        code = "cooler balance sample.mcool::10000"

        violations = analyze_shell_safety(code, "Bash")

        self.assertTrue(any(item.code == "cooler_balance_mutation" for item in violations))
        self.assertIn("cooler balance", shell_safety_error(code, "Bash"))

    def test_blocks_cooler_balance_stdout_as_balance_check(self):
        code = "cooler balance --stdout sample.mcool::/resolutions/10000 > /dev/null"

        violations = analyze_shell_safety(code, "Bash")

        self.assertTrue(any(item.code == "cooler_balance_mutation" for item in violations))
        self.assertIn("weight", shell_safety_error(code, "Bash"))

    def test_allows_cooler_balance_help(self):
        self.assertEqual(analyze_shell_safety("cooler balance --help", "Bash"), [])

    def test_risk_analyzer_marks_shell_startup_mutation_critical(self):
        result = RiskAnalyzer.analyze("echo x >> ~/.bashrc", "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.CRITICAL)
        self.assertTrue(any("mutates_shell_startup" in item for item in result["risky_operations"]))

    def test_risk_analyzer_auto_approves_read_only_conda_python_probe(self):
        code = '''conda run -n compartment python -c "
import cooler
c = cooler.Cooler('/data/leaf.mcool::/resolutions/40000')
print('leaf chroms:', c.chromnames[:])
print('leaf bins count:', c.bins()[:].shape[0])
"'''

        result = RiskAnalyzer.analyze(code, "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.SAFE)
        self.assertIn("Whitelisted Command", result["safe_operations"])

    def test_risk_analyzer_auto_approves_read_only_conda_rscript_probe(self):
        code = 'conda run -n calder2 Rscript -e "library(CALDER); cat(\\\'OK\\\\n\\\')"'

        result = RiskAnalyzer.analyze(code, "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.SAFE)

    def test_risk_analyzer_keeps_conda_python_write_probe_interactive(self):
        code = 'conda run -n compartment python -c "import pandas as pd; pd.DataFrame().to_csv(\\\'out.tsv\\\')"'

        result = RiskAnalyzer.analyze(code, "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.MEDIUM)
        self.assertTrue(result["risky_operations"])
        self.assertEqual(result["safe_operations"], [])

    def test_risk_analyzer_allows_stderr_to_stdout_read_only_probe(self):
        code = "ls -lh result.tsv 2>&1 && wc -l result.tsv 2>&1 && head -3 result.tsv 2>&1"

        result = RiskAnalyzer.analyze(code, "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.SAFE)
        self.assertIn("Whitelisted Command", result["safe_operations"])

    def test_risk_analyzer_allows_stderr_devnull_read_only_preview(self):
        code = "zcat reads.fq.gz 2>/dev/null | head -8"

        result = RiskAnalyzer.analyze(code, "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.SAFE)
        self.assertIn("Whitelisted Command", result["safe_operations"])

    def test_risk_analyzer_keeps_file_redirection_interactive(self):
        code = "ls -lh result.tsv > listing.txt"

        result = RiskAnalyzer.analyze(code, "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.MEDIUM)
        self.assertTrue(any("redirection" in item for item in result["risky_operations"]))

    def test_non_safe_confirmation_requires_explicit_y(self):
        import inspect

        source_text = inspect.getsource(ConfirmationUI.prompt_confirmation)

        self.assertIn("enter_executes = risk_level == RiskLevel.SAFE", source_text)
        self.assertIn("Please type 'y' to execute this non-SAFE command", source_text)
        self.assertIn("if enter_executes:\n            print", source_text)
        self.assertIn("if response == '':", source_text)

    def test_hitl_plain_text_review_response_is_feedback_not_code(self):
        action, payload = HITLMiddleware()._parse_code_response("40k", original_code="echo ok")

        self.assertEqual(action, "reject")
        self.assertIn("40k", payload)

    def test_hitl_edit_requires_explicit_prefix(self):
        action, payload = HITLMiddleware()._parse_code_response("e: echo edited", original_code="echo ok")

        self.assertEqual(action, "edit")
        self.assertEqual(payload, "echo edited")

    async def test_executor_blocks_shell_startup_mutation(self):
        stdout, stderr, returncode = await run_bash_script("echo x >> ~/.bashrc")

        self.assertEqual(stdout, "")
        self.assertEqual(returncode, 126)
        self.assertIn("shell-safety guard blocked", stderr)

    async def test_bash_executor_uses_pipefail(self):
        stdout, stderr, returncode = await run_bash_script("false | cat")

        self.assertNotEqual(returncode, 0)
        self.assertEqual(stdout, "")
        self.assertIn("[ERROR]", stderr)
        self.assertIn("exit_code=1", stderr)
        self.assertRegex(stderr, r"line=\d+")

    async def test_python_repl_blocks_interactive_input_and_restores_stdout(self):
        before = sys.stdout

        stdout, stderr, returncode = await run_python_repl("value = input('value: ')")

        self.assertEqual(stdout, "")
        self.assertEqual(returncode, 126)
        self.assertIn("interactive input", stderr.lower())
        self.assertIs(sys.stdout, before)

    async def test_python_repl_catches_system_exit_and_restores_stdout(self):
        before = sys.stdout

        stdout, stderr, returncode = await run_python_repl("raise SystemExit('stop')")

        self.assertEqual(stdout, "")
        self.assertEqual(returncode, 1)
        self.assertIn("attempted to exit", stderr)
        self.assertIs(sys.stdout, before)

    async def test_recovery_treats_hicfindtads_path_error_as_env_runner_issue_after_healthcheck(self):
        captured = {}

        class FakeLLM:
            async def ainvoke(self, messages):
                captured["prompt"] = messages[0].content
                return type("Msg", (), {"content": "diagnosis"})()

        async def failing_execute(state):
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "<observation>\n[Bash] ❌ Failed\n"
                            "STDOUT:\n[ERROR] hicFindTADs not found on PATH\n"
                            "STDERR:\n</observation>"
                        )
                    )
                ],
                "runtime_status": {
                    "genomics/chromatin/tad-calling::hicexplore": {
                        "ok": True,
                        "env": "hicexplore",
                        "env_runner": "conda run -n hicexplore python",
                        "healthcheck": "conda run -n hicexplore python -c \"assert True\"",
                    }
                },
            }

        wrapped = error_recovery_middleware(FakeLLM(), retriever=None)(failing_execute)
        result = await wrapped({})

        self.assertNotIn("prompt", captured)
        self.assertEqual(result["pending_recovery_action"]["type"], "runtime_recovery")
        self.assertEqual(result["required_next_action"], "SCRIPT_PATCH")
        self.assertNotEqual(result["pending_recovery_action"]["recommended_action"], "RETRY_EXECUTION")

    async def test_runtime_drift_returns_to_same_step_without_llm_or_user_question(self):
        with tempfile.TemporaryDirectory() as root:
            script = f"{root}/step.sh"
            trace = f"{root}/trace.log"
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(
                    "set -euo pipefail\n"
                    "export RESOLVED_ENV_PREFIX=/opt/verified/bio_runtime\n"
                    "bash -lc 'TOOL_JAR=$(find \"$CONDA_PREFIX/share\" -name tool.jar -print -quit); "
                    "test -s \"$TOOL_JAR\"'\n"
                )
            with open(trace, "w", encoding="utf-8") as handle:
                handle.write(
                    "+BIO_ERR|./step.sh:3|1| bash -lc 'TOOL_JAR=$(find \"$CONDA_PREFIX/share\" "
                    "-name tool.jar -print -quit); test -s \"$TOOL_JAR\"'\nPIPESTATUS=(1)\n"
                )

            class UnexpectedLLM:
                async def ainvoke(self, messages):
                    raise AssertionError("deterministic runtime drift must not call RecoveryJudge")

            async def failing_execute(state):
                return {
                    "messages": [HumanMessage(content="<observation>[Bash] failed</observation>")],
                    "execution_evidence": {
                        "execution_id": "exec-runtime-drift",
                        "step_id": "step-2",
                        "action_id": "action-runtime-drift",
                        "action_kind": "execute",
                        "action_source": "generate",
                        "command_role": "primary",
                        "returncode": 1,
                        "execution_status": "failed",
                        "script_path": script,
                        "trace_file": trace,
                        "actual_environment": {"CONDA_PREFIX": "/opt/controller/runtime"},
                    },
                }

            wrapped = error_recovery_middleware(
                UnexpectedLLM(), retriever=None, data_path=root
            )(failing_execute)
            result = await wrapped({
                "task_id": "runtime-drift",
                "current_step_id": "step-2",
                "runtime_audit": [{
                    "skill_id": "example/tool",
                    "status": "ready",
                    "env": "bio_runtime",
                    "resolved_runtime_paths": {
                        "RESOLVED_ENV_PREFIX": "/opt/verified/bio_runtime",
                        "RESOLVED_TOOL_JAR": "/opt/verified/bio_runtime/share/tool.jar",
                    },
                }],
            })

        self.assertEqual(result["next_step"], "generate")
        self.assertEqual(result["required_next_action"], "SCRIPT_PATCH")
        self.assertEqual(result["pending_recovery_action"]["type"], "runtime_recovery")
        self.assertEqual(result["diagnostic_summary"]["failure_type"], "runtime_drift")
        self.assertEqual(result["structured_diagnosis"]["failure_type"], "runtime_drift")
        self.assertEqual(result["failure_owner"], "RUNTIME")
        self.assertNotIn("pending_human_question", result)

    async def test_recovery_llm_failure_is_deterministic_fallback(self):
        class FailingLLM:
            async def ainvoke(self, messages):
                raise RuntimeError("Error code: 400 - data_inspection_failed")

        async def failing_execute(state):
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "<observation>\n[Bash] ❌ Failed\n"
                            "STDOUT:\nout\nSTDERR:\nerr\n</observation>"
                        )
                    )
                ],
                "runtime_status": {},
            }

        wrapped = error_recovery_middleware(FailingLLM(), retriever=None)(failing_execute)
        result = await wrapped({
            "supervisor_verdict": "PASS",
            "completion_gate_result": {"passed": False, "classification": "artifact_missing"},
        })

        self.assertEqual(result["pending_recovery_action"]["type"], "internal_protocol_error")
        self.assertEqual(result["next_step"], "supervisor")
        self.assertFalse(result["recovery_decision"]["requires_user_input"])

    async def test_malformed_recovery_decision_regenerates_instead_of_finishing(self):
        malformed = json.dumps({
            "verdict": "repair",
            "root_cause": "wrong interpreter",
            "confidence": 0.9,
            "action_type": "regenerate_script",
            "evidence": ["launcher failed"],
            "commands": [],
            "verification_checks": ["rerun with the correct interpreter"],
        })

        class MalformedLLM:
            async def ainvoke(self, messages):
                raise AssertionError("deterministic artifact failure must not invoke RecoveryJudge")

        async def failing_execute(state):
            return {
                "messages": [HumanMessage(content=(
                    "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\n"
                    "Launcher class not found.\n"
                    "Exception in thread \"main\" java.lang.NoClassDefFoundError: "
                    "org/graalvm/polyglot/PolyglotException\n</observation>"
                ))],
                "execution_evidence": {
                    "execution_id": "exec-render",
                    "command": "conda run -n analysis python render.py",
                    "returncode": 255,
                    "stderr_summary": (
                        "Launcher class not found. "
                        "java.lang.NoClassDefFoundError: org/graalvm/polyglot/PolyglotException"
                    ),
                    "step_id": "step-3",
                    "root_cause_analysis": {
                        "status": "confirmed",
                        "primary_error_message": "Launcher class not found.",
                    },
                },
                "runtime_status": {},
            }

        wrapped = error_recovery_middleware(MalformedLLM(), retriever=None)(failing_execute)
        result = await wrapped({
            "task_id": "recover-json",
            "current_step_id": "step-3",
            "supervisor_verdict": "PASS",
            "completion_gate_result": {"passed": False, "classification": "artifact_missing"},
        })

        self.assertEqual(result["next_step"], "generate")
        self.assertIn(
            result["pending_recovery_action"]["type"],
            {"same_step_repair", "script_patch"},
        )
        self.assertEqual(result["recovery_phase"], "diagnose")
        self.assertNotEqual(result["pending_recovery_action"]["type"], "blocked")

    async def test_recovery_prompt_json_hint_does_not_break_f_string(self):
        captured = {"prompt": ""}

        class FakeLLM:
            async def ainvoke(self, messages):
                captured["prompt"] = messages[0].content
                return type("Msg", (), {"content": json.dumps({
                    "schema_version": 2,
                    "failure_class": "SCIENTIFIC_WARNING",
                    "error_is_real": True,
                    "primary_execution_succeeded": True,
                    "evidence_sufficient": True,
                    "confidence": 0.8,
                    "recommended_action": "RUN_DIAGNOSTIC_PROBE",
                    "rerun_required": False,
                    "consume_retry_budget": False,
                    "user_notification_required": False,
                    "reason": "validator and execution evidence conflict",
                    "evidence": ["validator status failed"],
                })})()

        async def failing_execute(state):
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "<observation>\n[Bash] ❌ Failed validation\n"
                            "STDOUT:\nBLASTN_FAILED: protein database cannot be used for nucleotide query\n"
                            "STDERR:\n"
                            "<validation_result level=\"scientific\" status=\"failed\">\n"
                            "❌ Status: FAILED\n"
                            "</validation_result>\n"
                            "</observation>"
                        )
                    )
                ],
                "runtime_status": {},
            }

        with tempfile.TemporaryDirectory() as root:
            wrapped = error_recovery_middleware(
                FakeLLM(), retriever=None, data_path=root
            )(failing_execute)
            result = await wrapped({
                "task_id": "judge-budget",
                "retry_count": 0,
                "supervisor_verdict": "PASS",
                "completion_gate_result": {"passed": False, "classification": "scientific_warning"},
            })
            budgets = load_plan_state(root, "judge-budget")["task_state"]["budgets"]

        self.assertIn("RecoveryJudge", captured["prompt"])
        self.assertIn("consume_retry_budget", captured["prompt"])
        self.assertEqual(result["recovery_decision"]["commands"], [])
        self.assertEqual(budgets["recovery_judge_count"], 1)
        self.assertEqual(budgets["execution_retry_count"], 0)

    async def test_recovery_prefers_structured_execution_evidence_over_observation(self):
        captured = {"prompt": ""}
        decision = json.dumps({
            "schema_version": 2,
            "failure_class": "AMBIGUOUS",
            "error_is_real": True,
            "primary_execution_succeeded": False,
            "evidence_sufficient": False,
            "confidence": 0.7,
            "recommended_action": "ASK_USER",
            "rerun_required": False,
            "consume_retry_budget": False,
            "user_notification_required": True,
            "reason": "database build evidence remains ambiguous",
            "evidence": ["ERROR: Database check failed."],
        })

        class FakeLLM:
            async def ainvoke(self, messages):
                raise AssertionError("deterministic artifact failure must not invoke RecoveryJudge")

        async def failing_execute(state):
            return {
                "messages": [HumanMessage(content=(
                    "<observation>\n[Bash] ❌ Failed\nSTDOUT:\ntruncated\nSTDERR:\n"
                    "<validation_result level=\"scientific\" status=\"failed\">\n"
                    "validator summary only\n</validation_result>\n</observation>"
                ))],
                "execution_evidence": {
                    "execution_id": "exec-real",
                    "command": "java -jar snpEff.jar build -gtf22 customGenome",
                    "returncode": 255,
                    "stdout_summary": "Reading GTF22 data file\nERROR: Database check failed.",
                    "stderr_summary": "",
                    "root_cause_analysis": {
                        "status": "confirmed",
                        "primary_error_message": "ERROR: Database check failed.",
                    },
                },
            }

        wrapped = error_recovery_middleware(FakeLLM(), retriever=None)(failing_execute)
        result = await wrapped({
            "task_id": "structured-evidence",
            "supervisor_verdict": "PASS",
            "completion_gate_result": {"passed": False, "classification": "artifact_missing"},
        })

        self.assertEqual(captured["prompt"], "")
        self.assertEqual(
            result["execution_evidence"]["stdout_summary"],
            "Reading GTF22 data file\nERROR: Database check failed.",
        )
        self.assertTrue(result["recovery_context"]["context_id"])
        self.assertNotIn("execution", result["recovery_context"])
        self.assertEqual(result["next_step"], "generate")
        self.assertEqual(result["pending_recovery_action"]["type"], "same_step_repair")
        self.assertNotIn("pending_human_question", result)

    async def test_recovery_internal_error_preserves_execute_result_and_routes_supervisor(self):
        decision = json.dumps({
            "verdict": "blocked",
            "root_cause": "database build failed",
            "confidence": 1.0,
            "action_type": "block_task",
            "evidence": ["ERROR: Database check failed."],
            "commands": [],
        })

        class FakeLLM:
            async def ainvoke(self, messages):
                return type("Msg", (), {"content": decision})()

        async def failing_execute(state):
            return {
                "messages": [HumanMessage(content=(
                    "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n"
                    "ERROR: Database check failed.\nSTDERR:\n</observation>"
                ))],
                "execution_evidence": {
                    "execution_id": "exec-preserved",
                    "step_id": "step-7",
                    "command": "tool build",
                    "returncode": 1,
                    "stdout_summary": "ERROR: Database check failed.",
                    "stderr_summary": "",
                },
            }

        wrapped = error_recovery_middleware(FakeLLM(), retriever=None)(failing_execute)
        with patch.object(
            middleware_module,
            "build_recovery_context",
            side_effect=RuntimeError("recovery formatter broke"),
        ):
            result = await wrapped({"task_id": "recovery-isolation", "current_step_id": "step-7"})

        self.assertEqual(result["next_step"], "supervisor")
        self.assertEqual(result["recovery_status"], "RECOVERY_INTERNAL_ERROR")
        self.assertEqual(result["execution_evidence"]["execution_id"], "exec-preserved")
        self.assertEqual(result["pending_recovery_action"]["type"], "recovery_internal_error")
        self.assertIn("<recovery_internal_error>", result["messages"][-1].content)

    async def test_blocking_conflict_with_require_probe_continues_recovery(self):
        decision = json.dumps({
            "verdict": "probe",
            "root_cause": "renderer option parsing requires command inspection",
            "confidence": 0.6,
            "action_type": "run_probe",
            "evidence": ["argument --java-options: expected one argument"],
            "commands": ["grep -n -- '--java-options' step.sh"],
            "verification_checks": [],
        })

        class FakeLLM:
            async def ainvoke(self, messages):
                return type("Msg", (), {"content": decision})()

        async def failing_execute(state):
            return {
                "messages": [HumanMessage(content=(
                    "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\n"
                    "renderer.py: error: argument --java-options: expected one argument\n</observation>"
                ))],
                "execution_evidence": {
                    "execution_id": "exec-option",
                    "step_id": "step-2",
                    "command": "python renderer.py --java-options -Xmx8g",
                    "returncode": 2,
                    "stderr_summary": "argument --java-options: expected one argument",
                },
            }

        conflict = KnowledgeConflict(
            conflict_type="PLAN_RUNTIME_CONFLICT",
            description="runtime option parsing differs from the plan",
            severity="blocking",
            status="needs_probe",
            affects_step_ids=["step-2"],
        )
        with tempfile.TemporaryDirectory() as root:
            wrapped = error_recovery_middleware(
                FakeLLM(), retriever=None, data_path=root
            )(failing_execute)
            with patch.object(
                middleware_module.KnowledgeConflictDetector,
                "detect",
                return_value=[conflict],
            ):
                result = await wrapped({"task_id": "probe-conflict", "current_step_id": "step-2"})

        self.assertEqual(result["next_step"], "generate")
        self.assertEqual(result["pending_recovery_action"]["type"], "same_step_repair")
        self.assertNotEqual(result["recovery_status"], "BLOCKED")

    async def test_conflict_requiring_user_decision_routes_to_ask_user(self):
        decision = json.dumps({
            "schema_version": 2,
            "failure_class": "AMBIGUOUS",
            "failure_owner": "INPUT_DATA",
            "failure_type": "scientific_decision_required",
            "error_is_real": True,
            "primary_execution_succeeded": False,
            "evidence_sufficient": False,
            "confidence": 0.5,
            "recommended_action": "ASK_USER",
            "rerun_required": False,
            "consume_retry_budget": False,
            "user_notification_required": True,
            "reason": "two user-constrained repair directions conflict",
            "root_cause": "two user-constrained repair directions conflict",
            "evidence": ["repair direction is ambiguous"],
            "missing_evidence": ["user_scientific_decision"],
            "repair_strategy": "ask the user to choose between constrained scientific methods",
            "next_action": "ASK_USER",
            "requires_user_input": True,
        })

        class FakeLLM:
            async def ainvoke(self, messages):
                return type("Msg", (), {"content": decision})()

        async def failing_execute(state):
            return {
                "messages": [HumanMessage(content=(
                    "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\nambiguous repair\n</observation>"
                ))],
                "execution_evidence": {
                    "execution_id": "exec-user-conflict",
                    "step_id": "step-3",
                    "command": "tool run",
                    "returncode": 1,
                    "stderr_summary": "ambiguous repair",
                },
            }

        conflict = KnowledgeConflict(
            conflict_type="SCIENTIFIC_METHOD_CONFLICT",
            description="two user-constrained methods cannot both be selected",
            severity="blocking",
            status="detected",
            user_constraint_involved=True,
            hard_constraint_involved=True,
            affects_step_ids=["step-3"],
        )
        resolution = ConflictResolutionDecision(
            conflict_id=conflict.conflict_id,
            decision="request_user_decision",
            reason="User must choose one of the incompatible methods.",
            requires_user_approval=True,
        )
        with tempfile.TemporaryDirectory() as root:
            wrapped = error_recovery_middleware(
                FakeLLM(), retriever=None, data_path=root
            )(failing_execute)
            with patch.object(
                middleware_module.KnowledgeConflictDetector,
                "detect",
                return_value=[conflict],
            ), patch.object(
                middleware_module.ConflictResolver,
                "resolve",
                return_value=resolution,
            ):
                result = await wrapped({
                    "task_id": "user-conflict",
                    "current_step_id": "step-3",
                    "supervisor_verdict": "PASS",
                    "completion_gate_result": {"passed": False, "classification": "artifact_missing"},
                    "conflict_resolution": resolution.to_dict(),
                })

        self.assertEqual(result["next_step"], "ask_user")
        self.assertEqual(result["recovery_status"], "ASK_USER")
        self.assertEqual(result["pending_recovery_action"]["type"], "ask_user")
        self.assertIn("question", result["pending_human_question"])

    async def test_exhausted_managed_recovery_reports_internal_escalation(self):
        decision = json.dumps({
            "schema_version": 2,
            "failure_class": "AMBIGUOUS",
            "error_is_real": True,
            "primary_execution_succeeded": False,
            "evidence_sufficient": False,
            "confidence": 0.8,
            "recommended_action": "ESCALATE",
            "rerun_required": False,
            "consume_retry_budget": False,
            "user_notification_required": True,
            "reason": "same failure repeated without progress",
            "evidence": ["same fingerprint and code hash"],
        })

        class FakeLLM:
            async def ainvoke(self, messages):
                return type("Msg", (), {"content": decision})()

        async def failing_execute(state):
            return {
                "messages": [HumanMessage(content=(
                    "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\n"
                    "renderer failed again\n</observation>"
                ))],
                "execution_evidence": {
                    "execution_id": "exec-exhausted",
                    "step_id": "step-4",
                    "command": "python renderer.py",
                    "returncode": 2,
                    "stderr_summary": "renderer failed again",
                },
            }

        wrapped = error_recovery_middleware(FakeLLM(), retriever=None)(failing_execute)
        result = await wrapped({
            "task_id": "exhausted-managed",
            "task_mode": "managed",
            "current_step_id": "step-4",
            "failure_fingerprint": "same",
            "recovery_attempts": [{"failure_fingerprint": "same"}],
        })

        self.assertEqual(result["next_step"], "supervisor")
        self.assertEqual(result["recovery_status"], "ESCALATE")
        self.assertEqual(result["pending_recovery_action"]["type"], "internal_protocol_error")
        self.assertNotIn("pending_human_question", result)

    async def test_exhausted_noninteractive_recovery_can_finish_blocked(self):
        decision = json.dumps({
            "schema_version": 2,
            "failure_class": "AMBIGUOUS",
            "error_is_real": True,
            "primary_execution_succeeded": False,
            "evidence_sufficient": False,
            "confidence": 0.8,
            "recommended_action": "ESCALATE",
            "rerun_required": False,
            "consume_retry_budget": False,
            "user_notification_required": True,
            "reason": "same failure repeated without progress",
            "evidence": ["same fingerprint and code hash"],
        })

        class FakeLLM:
            async def ainvoke(self, messages):
                return type("Msg", (), {"content": decision})()

        async def failing_execute(state):
            return {
                "messages": [HumanMessage(content=(
                    "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\n"
                    "renderer failed again\n</observation>"
                ))],
                "execution_evidence": {
                    "execution_id": "exec-exhausted-batch",
                    "step_id": "step-4",
                    "command": "python renderer.py",
                    "returncode": 2,
                    "stderr_summary": "renderer failed again",
                },
            }

        wrapped = error_recovery_middleware(FakeLLM(), retriever=None)(failing_execute)
        result = await wrapped({
            "task_id": "exhausted-batch",
            "task_mode": "managed",
            "non_interactive": True,
            "current_step_id": "step-4",
            "failure_fingerprint": "same",
            "recovery_attempts": [{"failure_fingerprint": "same"}],
        })

        # RecoveryJudge may block, but only Supervisor/CompletionGate may finish.
        self.assertEqual(result["next_step"], "supervisor")
        self.assertEqual(result["recovery_status"], "ESCALATE")
        self.assertEqual(result["pending_recovery_action"]["type"], "internal_protocol_error")

    def test_recovery_classifies_rscript_missing_as_missing_executable(self):
        failure_class, action = classify_execution_failure("/tmp/script: line 3: Rscript: command not found")

        self.assertEqual(failure_class, "missing_executable")
        self.assertIn("r-base", action)

    async def test_first_failure_with_missing_internal_evidence_does_not_generate_probe(self):
        captured = {"llm_calls": 0}

        class FakeLLM:
            async def ainvoke(self, messages):
                captured["llm_calls"] += 1
                raise AssertionError("RecoveryJudge must not run for a clear first failure")

        class FakeRetriever:
            def __init__(self):
                self.calls = 0

            def retrieve(self, query, target_datasets=None):
                self.calls += 1
                return "known fix"

        async def failing_execute(state):
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "<observation>\n[Bash] ❌ Failed\n"
                            "STDOUT:\nout\nSTDERR:\nValueError: bad input\n</observation>"
                        )
                    )
                ],
                "runtime_status": {},
            }

        retriever = FakeRetriever()
        wrapped = error_recovery_middleware(FakeLLM(), retriever=retriever)(failing_execute)
        result = await wrapped({"retry_count": 0, "current_step_id": "step-1"})

        self.assertEqual(retriever.calls, 0)
        self.assertEqual(captured["llm_calls"], 0)
        self.assertEqual(result["next_step"], "supervisor")
        self.assertEqual(result["pending_recovery_action"]["type"], "internal_protocol_error")
        self.assertIn("failed_command", result["recovery_decision"]["missing_evidence"])


if __name__ == "__main__":
    unittest.main()

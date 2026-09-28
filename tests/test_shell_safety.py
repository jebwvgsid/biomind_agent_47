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
from agent_core.middleware import HITLMiddleware, classify_execution_failure
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.knowledge_conflicts import ConflictResolutionDecision, KnowledgeConflict


class ShellSafetyTests(unittest.IsolatedAsyncioTestCase):

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












    def test_recovery_classifies_rscript_missing_as_missing_executable(self):
        failure_class, action = classify_execution_failure("/tmp/script: line 3: Rscript: command not found")

        self.assertEqual(failure_class, "missing_executable")
        self.assertIn("r-base", action)



if __name__ == "__main__":
    unittest.main()

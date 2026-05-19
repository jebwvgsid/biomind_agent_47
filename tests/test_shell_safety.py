import sys
import types
import unittest

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

from execution.confirmation import RiskAnalyzer, RiskLevel
from execution.executor import run_bash_script, run_python_repl
from execution.shell_safety import analyze_shell_safety, shell_safety_error
from agent_core.middleware import HITLMiddleware, error_recovery_middleware


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

    def test_risk_analyzer_marks_shell_startup_mutation_critical(self):
        result = RiskAnalyzer.analyze("echo x >> ~/.bashrc", "Bash")

        self.assertEqual(result["risk_level"], RiskLevel.CRITICAL)
        self.assertTrue(any("mutates_shell_startup" in item for item in result["risky_operations"]))

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
        await wrapped({})

        self.assertIn("not an installation failure", captured["prompt"])
        self.assertIn("conda run -n hicexplore python", captured["prompt"])
        self.assertIn("Do not output `<install_tool>`", captured["prompt"])

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
        result = await wrapped({})

        self.assertIn("Automatic diagnosis LLM call failed", result["messages"][-1].content)
        self.assertIn("data_inspection_failed", result["messages"][-1].content)


if __name__ == "__main__":
    unittest.main()

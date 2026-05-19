import tempfile
import unittest
import sys
import types

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

from supervisor import SupervisorNode, Verdict


class _Response:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str = "**Verdict**: RETRY\n**Reason**: retry\n**Action**: retry"):
        self.content = content

    async def ainvoke(self, messages):
        return _Response(self.content)


def _state(code: str, observation: str, retry_count: int = 0):
    return {
        "task_id": "task_supervisor",
        "retry_count": retry_count,
        "messages": [
            AIMessage(content=f"<execute>{code}</execute>"),
            HumanMessage(content=observation),
        ],
    }


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_count_increments_until_max_then_escalates(self):
        obs = "<observation>\n[Bash] ❌ Failed\nSTDOUT:\n\nSTDERR:\nboom\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM(), output_dir=tmpdir, max_retries=3, use_llm_review=False)

            first = await supervisor.run(_state("false", obs, retry_count=0))
            third = await supervisor.run(_state("false", obs, retry_count=2))
            escalated = await supervisor.run(_state("false", obs, retry_count=3))

        self.assertEqual(first["supervisor_verdict"], Verdict.RETRY.value)
        self.assertEqual(first["retry_count"], 1)
        self.assertEqual(third["supervisor_verdict"], Verdict.RETRY.value)
        self.assertEqual(third["retry_count"], 3)
        self.assertEqual(escalated["supervisor_verdict"], Verdict.ESCALATE.value)
        self.assertEqual(escalated["next_step"], "finish")

    async def test_empty_output_warning_is_not_upgraded_to_retry_by_llm(self):
        obs = "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\n\nSTDERR:\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: RETRY\n**Reason**: empty\n**Action**: retry"), output_dir=tmpdir)
            result = await supervisor.run(_state("python run_analysis.py", obs, retry_count=0))

        self.assertEqual(result["supervisor_verdict"], Verdict.WARN.value)
        self.assertEqual(result["retry_count"], 0)

    async def test_non_progress_help_command_retries_unless_preflight_step(self):
        obs = "<observation>\n[Bash] ✅ Success (0.1s)\nSTDOUT:\nusage\nSTDERR:\n</observation>"
        with tempfile.TemporaryDirectory() as tmpdir:
            supervisor = SupervisorNode(_FakeLLM("**Verdict**: PASS"), output_dir=tmpdir, use_llm_review=False)
            analysis_step = _state("python run_analysis.py --help", obs, retry_count=0)
            analysis_step["plan_progress"] = "Plan checklist:\n- [>] Step 2: Run analysis\nNext step: Step 2: Run analysis"
            preflight_step = _state("python run_analysis.py --help", obs, retry_count=0)
            preflight_step["plan_progress"] = "Plan checklist:\n- [>] Step 1: Verify tool help\nNext step: Step 1: Verify tool help"

            retry_result = await supervisor.run(analysis_step)
            warn_result = await supervisor.run(preflight_step)

        self.assertEqual(retry_result["supervisor_verdict"], Verdict.RETRY.value)
        self.assertEqual(warn_result["supervisor_verdict"], Verdict.WARN.value)

    def test_stdout_stderr_stream_extraction_does_not_bleed(self):
        obs = "<observation>\n[Bash] ❌ Failed\nSTDOUT:\nout line\nSTDERR:\nerr line\n</observation>"

        self.assertEqual(SupervisorNode._extract_stream(obs, "STDOUT"), "out line")
        self.assertEqual(SupervisorNode._extract_stream(obs, "STDERR"), "err line")


if __name__ == "__main__":
    unittest.main()

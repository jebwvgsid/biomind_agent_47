import os
import sys
import types
import unittest
from unittest.mock import patch

try:
    from langchain_core.messages import HumanMessage  # type: ignore  # noqa: F401
except (ImportError, ModuleNotFoundError):
    langchain_core = types.ModuleType("langchain_core")
    messages_module = types.ModuleType("langchain_core.messages")

    class HumanMessage:
        def __init__(self, content: str = ""):
            self.content = content

    messages_module.HumanMessage = HumanMessage
    sys.modules.setdefault("langchain_core", langchain_core)
    sys.modules["langchain_core.messages"] = messages_module

from execution.executor import run_command


class ExecutorWatchdogTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(os.name == "nt", "POSIX shell watchdog behavior is tested on Linux/macOS only")
    async def test_run_command_terminates_idle_stalled_process(self):
        with patch.dict(
            os.environ,
            {
                "BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS": "0.2",
                "BIOMIND_EXEC_STALL_TIMEOUT_SECONDS": "0.4",
                "BIOMIND_EXEC_WALL_TIMEOUT_SECONDS": "0",
            },
        ):
            stdout, stderr, rc = await run_command("sleep 5", profile=False)

        self.assertEqual(stdout, "")
        self.assertEqual(rc, 124)
        self.assertIn("STALLED", stderr)


if __name__ == "__main__":
    unittest.main()

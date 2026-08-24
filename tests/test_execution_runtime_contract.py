from __future__ import annotations

import unittest

from execution.executor import BASH_SCRIPT_PROLOGUE, run_bash_script
from execution.runtime_contract import (
    EXECUTION_RUNTIME_POLICY,
    execution_runtime_contract,
    render_bash_script_prologue,
)


class ExecutionRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    def test_executor_prologue_is_derived_from_canonical_policy(self):
        contract = execution_runtime_contract()

        self.assertEqual(
            BASH_SCRIPT_PROLOGUE,
            render_bash_script_prologue(EXECUTION_RUNTIME_POLICY),
        )
        self.assertTrue(contract["shell_policy"]["errexit"])
        self.assertTrue(contract["shell_policy"]["pipefail"])
        self.assertTrue(contract["shell_policy"]["errtrace"])
        self.assertFalse(contract["shell_policy"]["nounset"])
        self.assertIn("set -eo pipefail", BASH_SCRIPT_PROLOGUE)
        self.assertNotIn("set -euo pipefail", BASH_SCRIPT_PROLOGUE)

    async def test_unguarded_optional_command_still_fails_fast(self):
        stdout, _stderr, returncode = await run_bash_script(
            "false\nrc=$?\necho warning\n"
        )

        self.assertNotEqual(returncode, 0)
        self.assertNotIn("warning", stdout)

    async def test_guarded_optional_command_can_continue(self):
        stdout, stderr, returncode = await run_bash_script(
            "if false; then\n"
            "  :\n"
            "else\n"
            "  rc=$?\n"
            "  echo warning:$rc\n"
            "fi\n"
            "echo continued\n"
        )

        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(stdout.splitlines(), ["warning:1", "continued"])


if __name__ == "__main__":
    unittest.main()

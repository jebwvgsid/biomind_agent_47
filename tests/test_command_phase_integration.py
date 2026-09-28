"""Exercise production phase adapters, with provider I/O replaced by messages."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage


class Service:
    def __init__(self): self.calls = []
    def describe(self, task_id): return {'project_dir': '/portable-project'}
    async def run(self, arguments, **scope):
        self.calls.append((arguments, scope))
        return {'status': 'completed', 'exit_code': 0, 'stdout': 'observed-file', 'receipt_ref': '/receipt.json',
                'truncated': {'stdout': False, 'stderr': False}}


class Model:
    def bind_tools(self, tools):
        self.tools = tools
        return self


def query():
    return AIMessage(content='', tool_calls=[{'id': 'query-1', 'name': 'exec_command', 'args': {'cmd': 'ls'}}])


class PhaseIntegrationTests(unittest.IsolatedAsyncioTestCase):






    def test_repeated_command_receipts_are_not_new_recovery_progress(self):
        from agent_core.workflow.recovery_runtime import meaningful_inspection_facts
        def observation(receipt, status='completed'):
            return {'tool': 'exec.command', 'source_ref': receipt, 'result': {
                'status': status, 'cmd': 'ls', 'stdout': 'same-file', 'exit_code': 0,
                'observed_at': receipt, 'age_seconds': len(receipt),
                'receipt_ref': receipt, 'log_refs': {'stdout': receipt + '/stdout.log'}}}
        first = meaningful_inspection_facts([observation('first')])
        self.assertTrue(first)
        self.assertEqual(first, meaningful_inspection_facts([observation('second')]))
        self.assertFalse(meaningful_inspection_facts([observation('third', status='sandbox_error')]))


if __name__ == '__main__':
    unittest.main()

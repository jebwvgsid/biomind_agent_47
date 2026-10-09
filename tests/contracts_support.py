"""Fixture isolation only: no production validators, approvals, or state gates replaced."""
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import patch
import sys
import os

@contextmanager
def isolated_host():
    # Host environment detection and optional resource telemetry are outside this audit.
    # The production read/network guard remains active; never relax it.
    with patch.object(Path, 'home', return_value=Path(os.environ['TMPDIR'])/'synthetic-home'), \
         patch('agent_core.command_execution._runtime_locations',return_value=({Path(sys.prefix)},'', ['/usr/bin','/bin'])), \
         patch('execution.resource_evidence.ProcessResourceEvidence',side_effect=RuntimeError('AUDIT_RESOURCE_COLLECTION_DISABLED')):
        yield

@asynccontextmanager
async def contract_fixture(task_id='contract-audit'):
    from tests.react_v3.support import task_fixture
    with isolated_host():
        async with task_fixture(task_id) as fixture:
            yield fixture

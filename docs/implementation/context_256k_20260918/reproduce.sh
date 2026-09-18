#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
test_python="${BIOMIND_TEST_PYTHON:-python}"
export BIOMIND_DISABLE_DOTENV=1
export PYTHONDONTWRITEBYTECODE=1
"$test_python" - "$repo_dir" "$@" <<'PY'
import asyncio
import os
import sys
root = sys.argv.pop(1)
sys.path.insert(0, root)
os.chdir(root)
# Keep teardown responsive when the outer sandbox suppresses the self-pipe
# wakeup. This test-only scheduler tick does not change production execution.
original = asyncio.BaseEventLoop.run_until_complete
def run(self, future):
    handles = []
    def tick():
        handles.append(self.call_later(.02, tick))
    tick()
    try:
        return original(self, future)
    finally:
        for handle in handles:
            handle.cancel()
asyncio.BaseEventLoop.run_until_complete = run
# Import installed message classes before the historical conftest stubs.
import langchain_core.messages
import langchain_core.callbacks
import pytest
suite = ['tests/test_context_256k_entrypoints.py',
    'tests/test_recovery_observation_projection.py', 'tests/test_command_phase_integration.py',
    'tests/test_unified_installer_boundary.py', 'tests/test_command_execution.py',
    'tests/test_scoped_context.py']
raise SystemExit(pytest.main(['-q', *suite, *sys.argv[1:]]))
PY

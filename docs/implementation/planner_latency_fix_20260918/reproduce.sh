#!/usr/bin/env bash
set -euo pipefail
biomind_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd -- "$biomind_repo_dir"
export BIOMIND_DISABLE_DOTENV=1
export PYTHONDONTWRITEBYTECODE=1
"${BIOMIND_TEST_PYTHON:-python}" - <<'PY'
import asyncio, sys
sys.path.insert(0, '.')
# Load installed LangChain before the repository's optional-dependency stubs.
import langchain_core.messages, langchain_core.callbacks
import pytest
original = asyncio.BaseEventLoop.run_until_complete
# Test runner only: outer seccomp can suppress the thread->loop socket wakeup.
def run_until_complete(self, future):
    current = None
    def tick():
        nonlocal current
        current = self.call_later(.02, tick)
    tick()
    try:
        return original(self, future)
    finally:
        if current:
            current.cancel()
asyncio.BaseEventLoop.run_until_complete = run_until_complete
raise SystemExit(pytest.main(['-q',
    'tests/test_planner_inspection_latency.py',
    'tests/test_command_execution.py',
    'tests/test_command_phase_integration.py',
    'tests/test_formal_artifact_resolver.py',
    'tests/test_cli_model_progress.py',
    'tests/test_run_profiler.py']))
PY

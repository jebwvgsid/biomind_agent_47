"""New OS interpreter witnesses using the existing exact restore registration."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_core.action_runtime import execute_formal_command
from agent_core.plan_tracker import load_plan_state
from tests.contracts_support import contract_fixture
from tests.react_v3.support import actual_micro_runner


def launch_child(f, data):
    descriptor = f.root / 'restore-fixture.json'
    descriptor.write_text(json.dumps({'data_path': str(f.root), 'task_id': f.task_id,
        'skills_root': str(f.skills_root), 'parent_pid': os.getpid(), **data}, ensure_ascii=False))
    bootstrap = Path(sys.modules['__main__'].__file__).resolve()
    command = [sys.executable, str(bootstrap), str(Path.cwd()), '--context-restore', str(descriptor)]
    environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8', 'TMPDIR': os.environ['TMPDIR']}
    registry = sys.modules['__main__'].OFFLINE_RESTORE_PROCESSES
    registry[tuple(command)] = {'cwd': str(Path.cwd()), 'env': environment}
    try:
        child = subprocess.run(command, cwd=Path.cwd(), env=environment,
            text=True, capture_output=True, timeout=90)
    finally:
        registry.pop(tuple(command), None)
    assert child.returncode == 0, child.stdout + child.stderr
    result = json.loads((f.root / 'restore-result.json').read_text())
    assert result['child_pid'] != os.getpid() and result['parent_pid'] == os.getpid()
    assert 'SOURCE_UNCHANGED True' in child.stdout
    artifact = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('outputfix-new-process-' + data['mode'] + '.json')
    artifact.write_text(json.dumps({'child': result, 'stdout': child.stdout, 'stderr': child.stderr},
        ensure_ascii=False, indent=2))
    return result




def test_C05_actual_new_process_retains_failed_chain_budget_and_hands_to_recovery():
    async def run():
        async with contract_fixture('outputfix-os-restore') as f:
            launches = []
            with actual_micro_runner(launches):
                for number in (1, 2):
                    result = await execute_formal_command(f.agent.command_service,
                        {'argv': [sys.executable, '-c', 'raise SystemExit(' + str(number) + ')'],
                         'workdir': str(f.root)}, task_id=f.task_id, call_id='parent-failure-' + str(number))
                    assert result['exit_code'] == number
            state = load_plan_state(f.root, f.task_id)
            small = f.root / 'restore-small.txt'
            small.write_text('synthetic descriptor locator')
            result = launch_child(f, {'mode': 'failure-budget', 'small': str(small),
                'expected_budget': deepcopy(state['task_state']['typed_recovery_budget'])})
            assert len(launches) == 2 and result['new_dispatches'] == 0
    asyncio.run(run())

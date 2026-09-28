"""Exact offline --context-restore child; no provider or scientific command."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys


def run(path):
    from agent_core.plan_tracker import load_plan_state
    data = json.loads(Path(path).read_text())
    root = Path(data['data_path'])
    assert root == Path(path).parent
    assert os.getpid() != data['parent_pid']
    state = load_plan_state(root, data['task_id'])
    result = {'mode': data['mode'], 'parent_pid': data['parent_pid'], 'child_pid': os.getpid(),
              'real_api_usage': 'NOT_MEASURED', 'scientific_execution': 'NOT_RUN'}
    if data['mode'] == 'authorization':
        from agent_core.work_output_protection import prepare_output_protection
        from agent_core.work_outputs import step_outputs
        from agent_core.action_runtime import ActionRuntimeContractError
        from agent_core.request_state import request_scope
        auth = state['task_state']['output_write_authorization']
        assert auth == data['expected_authorization']
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        assert hashlib.sha256(canonical(auth['guard'])).hexdigest() == data['guard_sha256']
        scope = request_scope(state)
        def scope_guard():
            assert request_scope(load_plan_state(root, data['task_id'])) == scope
        step = next(s for s in state['steps'] if s['step_id'] == state['current_step_id'])
        rows = step_outputs(state, root, step)
        old = Path(data['small'])
        before = old.read_bytes()
        directory = root / 'restored-approval'
        directory.mkdir()
        guard, protection = prepare_output_protection(state, rows, data_path=root,
            attempt_dir=directory, scope_guard=scope_guard)
        assert protection['guard_hash'] == auth['guard']['guard_hash']
        assert protection['source_ref'] == auth['source_ref']
        assert [(r['path'], r['before']) for r in guard['targets']] == [(r['path'], r['before']) for r in auth['guard']['targets']]
        assert old.read_bytes() == before
        assert all(Path(r['backup_path']).read_bytes() == before for r in protection['backups'])
        failures = {}
        def reject(label, candidate, selected, expected):
            target = root / ('rejected-' + label)
            target.mkdir()
            try:
                prepare_output_protection(candidate, selected, data_path=root,
                    attempt_dir=target, scope_guard=scope_guard)
            except ActionRuntimeContractError as exc:
                assert expected in str(exc), str(exc)
                failures[label] = str(exc)
            else:
                raise AssertionError('Old authorization unexpectedly admitted ' + label)
        moved_scope = deepcopy(state)
        moved_scope['authoritative_user_turn_revision'] = scope['user_turn_revision'] + 1
        reject('new-scope', moved_scope, rows, 'OUTPUT_REPLACEMENT_NOT_APPROVED')
        other = old.with_name('child-new-object.tsv')
        other.write_text('a distinct preexisting object')
        reject('new-object', state, [{**rows[0], 'canonical_path': str(other)}], 'OUTPUT_REPLACEMENT_NOT_APPROVED')
        old.unlink()
        old.write_bytes(before)
        reject('replaced-same-bytes', state, rows, 'OUTPUT_TARGET_CHANGED_SINCE_REVIEW')
        old.write_bytes(before + b'changed after approval\n')
        reject('changed-target', state, rows, 'OUTPUT_TARGET_CHANGED_SINCE_REVIEW')
        assert load_plan_state(root, data['task_id'])['task_state']['output_write_authorization'] == auth
        result.update(authorization_unchanged=True, guard_sha256=data['guard_sha256'],
                      same_scope_accepted=True, backups=protection['backups'], refusals=failures)
    elif data['mode'] == 'failure-budget':
        from agent_core.action_runtime import execute_formal_command
        from agent_core.command_execution import CommandExecutionService
        budget = deepcopy(state['task_state']['typed_recovery_budget'])
        assert budget == data['expected_budget'] and list(budget.values()) == [1]
        attempts = set(state['provenance_state']['attempts'])
        # This child registers no execution command. A regression that tries
        # launching here is denied by the unchanged offline process guard.
        service = CommandExecutionService(data_path=root, project_dir=root)
        observed = asyncio.run(execute_formal_command(service,
            {'argv': [sys.executable, '-c', 'raise SystemExit(0)'], 'workdir': str(root)},
            task_id=data['task_id'], call_id='fresh-os-process-third-intent'))
        assert observed['error'] == 'LOCAL_EXECUTION_REPAIR_EXHAUSTED', observed
        assert observed['execution_started'] is False and observed['recovery_handoff']['failure_packet_ref']
        current = load_plan_state(root, data['task_id'])
        assert current['task_state']['typed_recovery_budget'] == budget
        assert set(current['provenance_state']['attempts']) == attempts
        assert current['task_state']['budgets']['primary_execution_dispatches'] == 2
        assert not sys.modules['__main__'].REGISTERED_PROCESS_STARTS
        result.update(receipt=observed, budget=budget, attempt_count=len(attempts), new_dispatches=0)
    else:
        raise AssertionError('Unregistered restore scenario')
    (root / 'restore-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))

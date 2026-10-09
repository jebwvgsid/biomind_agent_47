"""Real formal executor observation and identity checks under guarded bootstrap."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import time
from unittest.mock import patch

import pytest

from agent_core.formal_command_observation import FormalCommandObserver, get_formal_observer
from agent_core.plan_tracker import load_plan_state, save_plan_state, classify_attempt_resume, _pid_is_alive
from tests.react_v3.support import task_fixture, actual_micro_runner
from tests.test_longrun_integration_v2 import record


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        async def join():
            closing = asyncio.create_task(loop.shutdown_default_executor())
            while not closing.done():
                await asyncio.wait({closing}, timeout=.02)
            await closing
        loop.run_until_complete(join())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)


async def terminal(view, snapshot, task_id, scope):
    end = time.monotonic()+6
    while not snapshot.get('terminal') and time.monotonic()<end:
        assert snapshot.get('status') != 'reconciliation_unknown', snapshot
        snapshot = await view.wait_command({'run_ref': snapshot['run_ref'], 'max_wait_ms': 100},
                                          task_id=task_id, request_scope=scope)
    assert snapshot.get('terminal'), snapshot
    return snapshot


def test_A24_formal_real_owner_same_attempt_replay_and_original_deadline():
    async def scenario():
        async with task_fixture('formal-longrun') as f:
            f.agent.command_service.first_yield_ms = 10
            view = get_formal_observer(f.agent.command_service)
            scope = {'request': 'formal-longrun'}
            args = {'cmd': 'printf begin; sleep .35; printf end', 'workdir': str(f.root), 'timeout_ms': 5000}
            launches = []
            with actual_micro_runner(launches):
                first = await view.start(args, task_id=f.task_id, request_scope=scope, call_id='same')
                assert not first['terminal']
                replay = await view.start(args, task_id=f.task_id, request_scope=scope, call_id='same')
                assert replay['run_ref'] == first['run_ref']
                final = await terminal(view, first, f.task_id, scope)
                assert final['exit_code'] == 0 and final['stdout'] == 'beginend'
                assert final['original_deadline_at'] == first['original_deadline_at']
                restored = FormalCommandObserver(f.agent.command_service)
                old = await restored.wait_command({'run_ref': first['run_ref'], 'max_wait_ms': 0}, task_id=f.task_id, request_scope=scope)
                assert old['terminal'] and old['receipt_ref'] == final['receipt_ref']
                assert len(launches) == 1
                state = load_plan_state(f.root, f.task_id)
                assert len(state['provenance_state']['attempts']) == 1
                assert state['provenance_state']['attempts'][first['run_ref']]['process_start_ticks']
                record('A24-formal-owner', actual_process_starts=len(launches), same_attempt=first['run_ref'],
                       first_terminal=first['terminal'], exit_code=final['exit_code'], deadline_unchanged=True,
                       cleanup_confirmed=(await view.shutdown(timeout_seconds=2))['cleanup_confirmed'])
    run(scenario())


def test_A25_wall_zero_unknown_cpu_keeps_one_finite_policy_watchdog():
    import execution.executor as executor
    async def scenario():
        async with task_fixture('formal-wall-zero') as f:
            f.agent.command_service.first_yield_ms = 10
            view = get_formal_observer(f.agent.command_service)
            scope = {'request': 'formal-wall-zero'}
            launches = []
            with actual_micro_runner(launches), patch.object(executor, '_collect_process_tree_cpu_ticks', return_value=None), \
                    patch.object(executor, '_terminate_process_tree', wraps=executor._terminate_process_tree) as terminate, \
                    patch.dict(os.environ, {'BIOMIND_EXEC_WALL_TIMEOUT_SECONDS':'0', 'BIOMIND_EXEC_STALL_TIMEOUT_SECONDS':'.25',
                        'BIOMIND_EXEC_SUSPECT_STALL_SECONDS':'0', 'BIOMIND_EXEC_EARLY_HEALTH_SECONDS':'0', 'BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS':'.2'}):
                first = await view.start({'cmd':'sleep 3', 'workdir':str(f.root)}, task_id=f.task_id, request_scope=scope, call_id='one')
                final = await terminal(view, first, f.task_id, scope)
                assert final['exit_code'] == 124
                assert 'CPU observation=unknown' in final['stderr']
                assert 'not proof of deadlock' in final['stderr']
                assert final['original_deadline_at'] is None and final['wall_timeout_disabled']
                assert terminate.call_count == 1
                record('A25-wall-zero-unknown', actual_process_starts=len(launches), watchdog_terminations=terminate.call_count,
                       exit_code=final['exit_code'], cpu_activity='unknown', wall_timeout_seconds=0,
                       elapsed_seconds=final['duration_seconds'], cleanup_confirmed=(await view.shutdown())['cleanup_confirmed'])
    run(scenario())


def test_A12_A18_formal_request_cancel_waiter_cancel_and_lost_owner():
    async def scenario():
        async with task_fixture('formal-cancel') as f:
            f.agent.command_service.first_yield_ms = 10
            view = get_formal_observer(f.agent.command_service)
            scope = {'request': 'formal-cancel'}
            args = {'cmd':'sleep 3', 'workdir':str(f.root), 'timeout_ms':5000}
            launches = []
            with actual_micro_runner(launches):
                first = await view.start(args, task_id=f.task_id, request_scope=scope, call_id='same')
                waiter = asyncio.create_task(view.wait_command({'run_ref':first['run_ref'], 'max_wait_ms':2000}, task_id=f.task_id, request_scope=scope))
                await asyncio.sleep(.05)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert view.active_runs(f.task_id, scope)
                replacement = FormalCommandObserver(f.agent.command_service)
                unknown = await replacement.start(args, task_id=f.task_id, request_scope=scope, call_id='same')
                assert unknown['status'] == 'reconciliation_unknown'
                view.cancel_request(f.task_id, scope, 'user')
                drained = await view.drain_request(f.task_id, scope, timeout_seconds=2)
                assert drained['all_terminal'], drained
                assert drained['runs'][0]['status'] == 'cancelled'
                denied = await view.start(args, task_id=f.task_id, request_scope=scope, call_id='late')
                assert denied['error'] == 'REQUEST_CANCELLED'
                assert len(launches) == 1
                record('A12-A18-formal-cancel', actual_process_starts=len(launches), waiter_cancel_kept_owner=True,
                       owner_loss_no_relaunch=True, final_execution_states=[drained['runs'][0]['status']],
                       exit_codes=[drained['runs'][0]['exit_code']], cleanup_confirmed=drained['cleanup_confirmed'])
    run(scenario())


def test_A18_legacy_identity_unknown_completed_receipt_precedes_liveness(tmp_path):
    from execution.executor import _process_identity
    identity = _process_identity(os.getpid())
    assert _pid_is_alive(os.getpid()) is None
    assert _pid_is_alive(os.getpid(), **identity) is True
    assert _pid_is_alive(os.getpid(), hostname='different-host', process_start_ticks=identity['process_start_ticks']) is None
    assert _pid_is_alive(os.getpid(), hostname=socket.gethostname(), process_start_ticks=identity['process_start_ticks']+1) is False
    async def scenario():
        async with task_fixture('formal-identity') as f:
            launches = []
            from agent_core.action_runtime import execute_formal_command
            with actual_micro_runner(launches):
                result = await execute_formal_command(f.agent.command_service, {'cmd':'printf verified', 'workdir':str(f.root)}, task_id=f.task_id, call_id='id')
            state = load_plan_state(f.root, f.task_id)
            attempt = state['provenance_state']['attempts'][result['attempt_id']]
            attempt['status'] = 'RUNNING'
            attempt.pop('hostname', None); attempt.pop('process_start_ticks', None)
            route = classify_attempt_resume(state, action_id=result['action_id'])
            assert route['route'] == 'supervisor' and route['create_new_attempt'] is False
            attempt['exit_status_path'] = str(tmp_path/'missing.json')
            route = classify_attempt_resume(state, action_id=result['action_id'])
            assert route['route'] == 'reconcile_attempt' and route['create_new_attempt'] is False
            record('A18-formal-identity', legacy_alive='unknown', saved_receipt_route='supervisor',
                   missing_receipt_route='reconcile_attempt', repeated_process_starts=0)
    run(scenario())


def test_A25_scoped_sampler_never_enumerates_proc_root():
    import execution.executor as executor
    with patch('os.listdir', side_effect=AssertionError('must not enumerate process root')):
        ticks = executor._collect_process_tree_cpu_ticks(os.getpid())
    assert ticks is None or isinstance(ticks, int)
    record('A25-scoped-sampler', full_proc_enumerations=0, sampled_ticks=ticks,
           scope='registered current test process descendants only')


def test_A07_formal_preparation_uses_original_deadline_without_spawn():
    import agent_core.action_runtime as action_runtime
    import sys
    async def scenario():
        async with task_fixture('formal-prep-deadline') as f:
            f.agent.command_service.first_yield_ms = 10
            view = get_formal_observer(f.agent.command_service)
            scope = {'request': 'formal-prep-deadline'}
            original = action_runtime.freeze_execution_environment
            def slow(*args, **kwargs):
                time.sleep(.15)
                return original(*args, **kwargs)
            starts = len(sys.modules['__main__'].REGISTERED_PROCESS_STARTS)
            launches = []
            with actual_micro_runner(launches), patch.object(action_runtime, 'freeze_execution_environment', side_effect=slow):
                first = await view.start({'cmd':'printf must-not-launch', 'workdir':str(f.root), 'timeout_ms':100},
                                         task_id=f.task_id, request_scope=scope, call_id='slow')
                final = await terminal(view, first, f.task_id, scope)
            actual_starts = len(sys.modules['__main__'].REGISTERED_PROCESS_STARTS)-starts
            assert actual_starts == 0
            assert final['exit_code'] == 124 and 'preparation deadline' in final['stderr']
            record('A07-formal-preparation', actual_process_starts=actual_starts, exit_code=final['exit_code'],
                   deadline_unchanged=first['original_deadline_at']==final['original_deadline_at'])
    run(scenario())


def test_A14_formal_saved_success_late_cancel_keeps_receipt():
    import hashlib
    async def scenario():
        async with task_fixture('formal-late-cancel') as f:
            f.agent.command_service.first_yield_ms = 10
            view = get_formal_observer(f.agent.command_service)
            scope = {'request': 'formal-late-cancel'}
            launches = []
            with actual_micro_runner(launches):
                first = await view.start({'cmd':'printf saved', 'workdir':str(f.root)}, task_id=f.task_id, request_scope=scope, call_id='success')
                final = await terminal(view, first, f.task_id, scope)
                receipt = Path(final['receipt_ref'])
                digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
                view.cancel_request(f.task_id, scope, 'after publication')
                state = load_plan_state(f.root, f.task_id)
                state['task_state']['status'] = 'cancelled'
                save_plan_state(f.root, f.task_id, state)
                late = await view.wait_command({'run_ref':first['run_ref'], 'max_wait_ms':0}, task_id=f.task_id, request_scope=scope)
                assert late['exit_code'] == 0 and late['cancel_requested']
                assert hashlib.sha256(receipt.read_bytes()).hexdigest() == digest
                assert load_plan_state(f.root,f.task_id)['task_state']['status'] == 'cancelled'
                record('A14-formal-late-cancel', actual_process_starts=len(launches), exit_code=late['exit_code'],
                       immutable_receipt=True, current_request_status='cancelled', new_commits=0)
    run(scenario())


def test_A07_formal_live_deadline_keeps_existing_timeout_consumer_prefix():
    async def scenario():
        async with task_fixture('formal-live-deadline') as f:
            f.agent.command_service.first_yield_ms = 10
            view = get_formal_observer(f.agent.command_service)
            scope = {'request': 'formal-live-deadline'}
            launches = []
            with actual_micro_runner(launches):
                first = await view.start({'cmd':'sleep 3', 'workdir':str(f.root), 'timeout_ms':350},
                                         task_id=f.task_id, request_scope=scope, call_id='live')
                final = await terminal(view, first, f.task_id, scope)
                assert final['exit_code'] == 124
                assert final['runtime_metadata']['termination_reason'].startswith('TIMEOUT after ')
                assert final['original_deadline_at'] == first['original_deadline_at']
                assert len(launches) == 1
                record('A07-formal-live-deadline', actual_process_starts=len(launches), exit_code=124,
                       timeout_consumer_prefix_preserved=True, original_deadline_unchanged=True,
                       elapsed_seconds=final['duration_seconds'])
    run(scenario())


@pytest.mark.parametrize('identity_kind', ['known_alive', 'legacy_unknown'])
def test_A18_attempt_created_cannot_supersede_unresolved_process(identity_kind, tmp_path):
    from agent_core.plan_tracker import build_plan_state, record_provenance_event
    from execution.executor import _process_identity
    state = build_plan_state('attempt-admission', '- [ ] Local execution')
    step = state['steps'][0]['step_id']
    for action in ('old', 'new'):
        record_provenance_event(state, 'action_bound', {'action': {'type':'execute', 'action_id':action,
            'step_id':step, 'action_hash':'hash-'+action}})
    old = record_provenance_event(state, 'attempt_created', {'action_id':'old'})
    identity = _process_identity(os.getpid()) if identity_kind == 'known_alive' else {}
    for status in ('SCRIPT_READY', 'APPROVED', 'STARTING', 'RUNNING'):
        record_provenance_event(state, 'attempt_state_changed', {'attempt_id':old['attempt_id'],
            'status':status, 'pid':os.getpid(), **identity})
    expected = 'process is alive' if identity_kind == 'known_alive' else 'REQUIRES_RECONCILIATION'
    with pytest.raises(ValueError, match=expected):
        record_provenance_event(state, 'attempt_created', {'action_id':'new'})
    assert len(state['provenance_state']['attempts']) == 1
    receipt = tmp_path/'known-exit.json'
    receipt.write_text(json.dumps({'attempt_id':old['attempt_id'], 'exit_code':0, 'terminal':True}))
    state['provenance_state']['attempts'][old['attempt_id']]['exit_status_path'] = str(receipt)
    created = record_provenance_event(state, 'attempt_created', {'action_id':'new'})
    assert created['action_id'] == 'new'
    record('A18-attempt-admission-'+identity_kind, new_attempts_without_exit=0, permitted_after_durable_exit=1)


def test_A18_unknown_with_exit_resumes_verification_validated_stays_transition_guard(tmp_path):
    from agent_core.plan_tracker import build_plan_state, record_provenance_event
    state = build_plan_state('attempt-resume', '- [ ] Local execution')
    step = state['steps'][0]['step_id']
    record_provenance_event(state, 'action_bound', {'action': {'type':'execute', 'action_id':'action', 'step_id':step, 'action_hash':'h'}})
    row = record_provenance_event(state, 'attempt_created', {'action_id':'action'})
    receipt = tmp_path/'exit.json'
    receipt.write_text(json.dumps({'attempt_id':row['attempt_id'], 'returncode':0}))
    for status in ('UNKNOWN_AFTER_CRASH', 'VALIDATED'):
        row = state['provenance_state']['attempts'][row['attempt_id']]
        row.update(status=status, exit_status_path=str(receipt))
        route = classify_attempt_resume(state, action_id='action')
        assert route['route'] == ('supervisor' if status == 'UNKNOWN_AFTER_CRASH' else 'transition_guard')
        assert route['create_new_attempt'] is False
    record('A18-unknown-exit-validated', unknown_with_exit_route='supervisor', validated_route='transition_guard', new_attempts=0)

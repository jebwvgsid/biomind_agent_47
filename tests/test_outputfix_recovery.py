"""Real tiny subprocesses + production graph/Recovery; only model output controlled."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest
from langchain_core.messages import AIMessage

from agent_core.action_runtime import execute_formal_command
from agent_core.command_execution import CommandExecutionService
from agent_core.plan_tracker import load_plan_state, save_plan_state, save_execution_plan
from agent_core.request_state import request_scope
from agent_core.workflow.outcomes import TriageOutcome
from tests.contracts_support import contract_fixture
from tests.react_v3.support import actual_micro_runner


def command(f, rc):
    return {'argv': [sys.executable, '-c',
        'import sys; print("controlled engineering execution rc=%d"); sys.exit(%d)' % (rc, rc)],
        'workdir': str(f.root)}




def provider(f, operations, requests):
    async def invoke(messages, **kwargs):
        ordinal = len(requests)
        requests.append({'names': [m.name for m in messages],
                         'tools': deepcopy(f.provider.bound_tool_schemas)})
        assert ordinal < len(operations), 'Unexpected model call: ' + str(ordinal)
        value = operations[ordinal]
        if callable(value): value = value(messages)
        if isinstance(value, AIMessage): return value
        name, args = value
        return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': 'repair-call-' + str(ordinal)}],
                         response_metadata={'finish_reason': 'tool_calls', 'complete': True})
    f.provider.ainvoke = invoke


def directive(f):
    def value(messages):
        current = load_plan_state(f.root, f.task_id)
        task = current['task_state']
        packet = task['causal_failure_packets'][task['active_failure_packet_ref']]['failure_packet']
        return AIMessage(content=json.dumps({'schema_version': 1, 'directive_id': 'actual-recovery-retry',
            'task_id': f.task_id, 'action': 'retry_step',
            'summary': 'The micro-process reported its controlled nonzero exit; replace the explicit exit value.',
            'reason_code': 'GENERATED_ACTION_FAILED', 'target_step_id': current['current_step_id'],
            'instruction': 'Run the same isolated engineering check with exit zero; do not change the Plan or inputs.',
            'evidence_refs': list(packet['evidence_refs']) or [current['execution_evidence']['receipt_ref']]}),
            response_metadata={'finish_reason': 'stop', 'complete': True})
    return value


def record(name, data):
    target = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    (target / ('outputfix-recovery-' + name + '.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))




def test_revision_restart_new_call_does_not_reset_local_budget_and_terminal_replays():
    async def run():
        async with contract_fixture('outputfix-restart') as f:
            launches = []
            with actual_micro_runner(launches):
                first = await execute_formal_command(f.agent.command_service, command(f, 7), task_id=f.task_id, call_id='first')
                prior = deepcopy(load_plan_state(f.root, f.task_id))
                step_id = prior['current_step_id']
                save_execution_plan(f.root, f.task_id, {'steps': [{'step_id': step_id,
                    'description': 'Clarified engineering check; the existing failure and authorization remain.',
                    'validation': 'Observe the explicit exit value in the engineering receipt.'}]})
                service = CommandExecutionService(data_path=f.root, project_dir=f.root)
                second = await execute_formal_command(service, command(f, 8), task_id=f.task_id, call_id='new-after-restart')
                blocked = await execute_formal_command(service, command(f, 0), task_id=f.task_id, call_id='third-call')
                replay = await execute_formal_command(service, command(f, 8), task_id=f.task_id, call_id='new-after-restart')
            current = load_plan_state(f.root, f.task_id)
            assert len(launches) == 2
            assert blocked['error'] == 'LOCAL_EXECUTION_REPAIR_EXHAUSTED'
            assert blocked['recovery_handoff']['failure_packet_ref']
            assert replay['replayed'] and replay['attempt_id'] == second['attempt_id']
            assert current['plan_revision'] > prior['plan_revision']
            assert list(current['task_state']['typed_recovery_budget'].values()) == [1]
            assert len(current['task_state']['causal_failure_incident_heads']) == 1
            record('restart', {'first': first, 'second': second, 'blocked': blocked, 'replay': replay, 'final': current})
    asyncio.run(run())






@pytest.mark.parametrize('status', ['RUNNING', 'UNKNOWN_AFTER_CRASH'])
def test_unsettled_prior_attempt_never_launches_replacement(status):
    async def run():
        async with contract_fixture('outputfix-owner-' + status.lower()) as f:
            from agent_core.plan_tracker import record_provenance_event
            state = load_plan_state(f.root, f.task_id)
            # Real persisted ownership record, representing a process whose
            # owner/terminal receipt is unavailable after a boundary crash.
            record_provenance_event(state, 'action_bound', {'action': {'action_id': 'owned-action',
                'step_id': state['current_step_id'], 'type': 'execute', 'execution_protocol_version': 3}})
            record_provenance_event(state, 'attempt_created', {'action_id': 'owned-action',
                'attempt_id': 'owned-attempt', 'step_id': state['current_step_id']})
            if status == 'RUNNING':
                record_provenance_event(state, 'attempt_state_changed', {'attempt_id': 'owned-attempt', 'status': 'STARTING'})
            record_provenance_event(state, 'attempt_state_changed', {'attempt_id': 'owned-attempt', 'status': status})
            save_plan_state(f.root, f.task_id, state)
            launches = []
            with actual_micro_runner(launches), pytest.raises(ValueError, match='PRIOR_PROCESS_REQUIRES_MONITOR_OR_RECONCILIATION'):
                await execute_formal_command(f.agent.command_service, command(f, 0), task_id=f.task_id, call_id='replacement')
            current = load_plan_state(f.root, f.task_id)
            assert not launches
            assert current['provenance_state']['attempts']['owned-attempt']['status'] == status
            assert not current['task_state'].get('typed_recovery_budget')
    asyncio.run(run())

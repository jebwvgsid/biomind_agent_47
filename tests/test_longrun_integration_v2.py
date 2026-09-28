"""V2 acceptance through production provider, phase, dispatcher and process owner.

Run only under tests/longrun_offline_bootstrap.py. Network replies and tiny
commands are synthetic; ownership, sandboxing, persistence and cancellation
remain production code. Each case emits its measured counters to JSON.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import agent_core.command_execution as command_execution
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.process_registry import ACTIVE_PROCESS_GROUPS
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as prior_fixture


def bootstrap():
    value = sys.modules['__main__']
    assert hasattr(value, 'OFFLINE_BWRAP_COMMANDS'), 'Use guarded longrun_offline_bootstrap.py'
    return value


def record(case, **facts):
    target = Path(os.environ.get('BIOMIND_LONGRUN_CASES',
        str(Path(os.environ['BIOMIND_OFFLINE_RESULTS']).with_name('longrun-cases.json'))))
    rows = json.loads(target.read_text()) if target.exists() else {}
    rows[case] = {'case': case, **facts}
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


def inspect_receipts(fixture, task_id):
    root = fixture.root / 'task_outputs' / task_id / 'command_inspection'
    return [json.loads(path.read_text()) for path in sorted(root.glob('*/receipt.json'))]


def loop_state(fixture, task_id):
    durable = load_plan_state(fixture.root, task_id)
    loops = (durable.get('task_state') or {}).get('tool_loops') or {}
    assert len(loops) == 1, list(loops)
    return next(iter(loops.values()))


def tool_payloads(messages, name=None):
    return [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)
        and (name is None or m.name == name)]


def register_command(command):
    bootstrap().OFFLINE_BWRAP_COMMANDS.add(command)
    return {'cmd': command, 'timeout_ms': 5000, 'max_output_chars': 1024}


def gated_command(fixture, *, name='gate', text='longrun-final', error_code=0):
    control = fixture.root / name
    control.mkdir()
    release = control / 'release'
    code = ('import os,time,sys; print("probe begin",flush=True); '
        f'p={str(release)!r}; deadline=time.monotonic()+4; '
        '\nwhile not os.path.exists(p) and time.monotonic()<deadline: time.sleep(.005)\n'
        f'print({text!r},flush=True); sys.exit({error_code})')
    arguments = register_command('/usr/bin/python3 -u -c ' + shlex.quote(code))
    arguments['read_paths'] = [str(control)]
    return arguments, release




def install_provider(fixture, script):
    async def provider(messages, *, config=None, **kwargs):
        index = len(fixture.provider_requests)
        fixture.provider_requests.append(deepcopy(messages))
        fixture.provider_attempts.append(deepcopy((config or {}).get('metadata') or {}))
        answer = script(index, messages)
        if hasattr(answer, '__await__'):
            answer = await answer
        return answer
    fixture.provider.ainvoke = provider


def facts(fixture, *, decisions=None, final=None, **extra):
    assert all(row.get('provider_attempt_id') and row.get('semantic_invocation_id')
               for row in fixture.provider_attempts), 'Real provider-attempt wrapper was bypassed'
    ledger = loop_state(fixture, fixture.task_id)
    receipts = inspect_receipts(fixture, fixture.task_id)
    actual = bootstrap().REGISTERED_PROCESS_STARTS[fixture.actual_starts_before:]
    result = {'admitted_intents': len([x for x in (ledger.get('intents') or {}).values()
                if x.get('tool', x.get('name')) == 'exec_command' and x.get('admitted')]),
        'actual_outer_starts': len(actual),
        'maintenance_observations': ledger.get('maintenance_calls', ledger.get('maintenance_observations')),
        'logical_model_decisions': decisions if decisions is not None else len(fixture.provider_requests),
        'actual_provider_attempts': len(fixture.provider_attempts),
        'provider_attempt_metadata': fixture.provider_attempts,
        'work': ledger.get('work_calls'), 'inspection': (ledger.get('counts') or {}).get('exec_command|inspect_paths', 0),
        'closure': ledger.get('finish_calls'), 'closure_dispatched': ledger.get('closure_dispatched'),
        'run_refs': [r.get('run_ref', r.get('activity_id')) for r in receipts],
        'original_deadlines': [r.get('original_deadline_at', r.get('deadline_at', r.get('deadline'))) for r in receipts],
        'tool_message_count': sum(m.get('type') == 'tool' for m in ledger.get('messages') or []),
        'host_event_count': sum((m.get('data') or {}).get('name') == 'runtime_observation' for m in ledger.get('messages') or []),
        'final_execution_states': [r.get('status') for r in receipts],
        'exit_codes': [r.get('exit_code') for r in receipts],
        'registered_process_groups_remaining': len(ACTIVE_PROCESS_GROUPS),
        'cleanup': [{'confirmed': r.get('cleanup_confirmed'), 'scope': r.get('cleanup_scope'),
                     'log_integrity': r.get('log_integrity')} for r in receipts],
        'downstream_planner_calls': 0, 'new_commits': 0,
        'provider_tokens_cost': 'NOT_MEASURED_FAKE_NETWORK',
        **extra}
    if final is not None:
        if isinstance(final, dict) and 'consultant_outcome' in final:
            outcome = final['consultant_outcome']
            result['outcome'] = {k: outcome.get(k) for k in ('type', 'summary', 'scope')}
        else:
            result['outcome'] = final
    return result


def finish(index, text='Inspection facts saved; scientific task not executed.'):
    return native('finish_task', {'message': text, 'outcome': 'incomplete'}, index)


































def formal_gate(f):
    control = f.root / 'formal-control'
    control.mkdir()
    release, output = control / 'release', f.root / 'actual-result.txt'
    durable = load_plan_state(f.root, f.task_id)
    durable['steps'][0]['outputs'] = [str(output)]
    save_plan_state(f.root, f.task_id, durable)
    script = ('printf "formal begin\\n"; '
        f'while [ ! -f {shlex.quote(str(release))} ]; do sleep .01; done; '
        'printf "verified result\\n" > actual-result.txt')
    return {'cmd': script, 'workdir': str(f.root), 'timeout_ms': 5000}, release, output





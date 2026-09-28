"""Detached Plan admission: controlled model, real tools/graph/state/authorization.

Only synthetic isolated files and tiny engineering observations are executed.
No real API, science, approval answer, production session, or production write.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from tests.test_thin_plan_contract_repair import fixture, artifact, read_whole
from tests.react_v3.support import actual_micro_runner
from agent_core.plan_tracker import load_plan_state, save_plan_state, save_execution_plan


def record(name, value):
    artifact('detached_'+name+'.json', json.loads(json.dumps(value, default=str)))


def candidate(f):
    path=f.root/'leaf.input.json'
    path.write_text(json.dumps({'available_resolutions':[40000]}))
    return {'goal':'Keep the original descriptive check fields without Plan resubmission.',
            'path_bindings':{'project_dir':str(f.root)},
            'inputs':[{'sample':'leaf','path':str(path)}],
            'parameters':{'resolution_bp':40000},
            'steps':[{'step_id':'inspect','description':'Check the exact declared resolution.',
                      'inputs':[str(path)],'outputs':[],
                      'validation':{'kind':'resolution','inputs':['leaf'],'resolution_bp':40000}},
                     {'step_id':'results','description':'Keep every declared sample result obligation.',
                      'dependencies':['inspect'],'outputs':[],
                      'validation':{'kind':'sample_results','samples':['leaf']}}]}








@pytest.mark.parametrize('boundary',['approval','cancel'])
def test_detached_admission_never_bypasses_execution_authority(boundary):
    from agent_core.action_runtime import execute_formal_command
    async def run():
        async with fixture('detached-authority-'+boundary) as f:
            save_execution_plan(f.root,f.task_id,candidate(f))
            state=load_plan_state(f.root,f.task_id)
            if boundary=='approval':state['task_state']['intent_admission']['review_before_execution']=True
            else:state['task_state']['status']='cancelled'
            save_plan_state(f.root,f.task_id,state)
            # No micro-runner allowance: the real gate must reject before launch.
            expected=ValueError if boundary=='approval' else asyncio.CancelledError
            with pytest.raises(expected) as caught:
                await execute_formal_command(f.agent.command_service,
                    {'argv':['printf','must-not-run'],'workdir':str(f.root)},
                    task_id=f.task_id,call_id='forbidden-'+boundary)
            if boundary=='approval':assert 'USER_REQUESTED_REVIEW_BEFORE_EXECUTION' in str(caught.value)
            after=load_plan_state(f.root,f.task_id)
            assert not (after.get('provenance_state') or {}).get('attempts')
            assert not after.get('current_approval') and not after.get('approval_record_ref')
            assert not after['task_state'].get('step_commit_receipts')
            record('authority-'+boundary,{'exception':type(caught.value).__name__,'formal_processes':0,'final':after})
    asyncio.run(run())






@pytest.mark.parametrize('kind', ['future_unknown_check', '', None, ['resolution'], {}])
def test_unknown_runtime_validation_never_becomes_passed(kind):
    from agent_core.work_validation import validate_step_facts
    from agent_core.completion_gate import verify_work_result
    async def run():
        async with fixture('detached-unknown-kind') as f:
            body=candidate(f);body['steps'][0]['validation']={'kind':kind,'free_text':'preserve this requirement'}
            state=save_execution_plan(f.root,f.task_id,body)
            assert state['steps'][0]['validation']==body['steps'][0]['validation']
            with pytest.raises(ValueError) as caught:validate_step_facts(state,state['steps'][0],[],[])
            assert str(caught.value)
            checked=verify_work_result(state,{'message':'No evidence'},project_dir=f.root)
            assert not checked['passed'] and checked['issues'][0]['code']=='STEP_VALIDATION_NOT_EXECUTABLE'
            record('unknown-kind',{'preserved':state['steps'][0]['validation'],'runtime_rejection':str(caught.value)})
    asyncio.run(run())

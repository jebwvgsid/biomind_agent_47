"""Real durable epoch copy, read in a second guarded process; no provider call."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

PROCESS_NONCE=uuid.uuid4().hex
import pytest
from tests.plan_resource_fix_support import scenario,save
from tests.test_plan_resource_fix import c_fix
from tests.test_plan_stop_fix import bad

async def preserve_epoch(case,task,state,runner):
    from agent_core.plan_tracker import load_plan_state
    root=Path(tempfile.mkdtemp(prefix='closed-epoch-',dir=os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS']))
    shutil.copytree(case.root/'task_outputs'/task,root/'task_outputs'/task)
    durable=load_plan_state(case.root,task)
    pointer={'root':str(root),'task':task,'writer_pid':os.getpid(),'writer_process_nonce':PROCESS_NONCE,
        'epoch':durable['task_state']['planning_epoch']}
    save('FIX_epoch_reload_pointer',pointer)
    return pointer


def test_write_real_exhausted_epoch_for_independent_reload():
    result=asyncio.run(scenario([bad('A'),bad('B'),bad('evidence'),bad('output'),c_fix()],
        'FIX_epoch_independent',bounded_rejection=False,after_plan=preserve_epoch))
    assert result['after_plan']['epoch']['planner_calls_total']==4
    assert not result['external_stop']


def test_read_closed_epoch_without_refund_or_dispatch():
    from agent_core.plan_tracker import load_plan_state,PlannerAdmissionClosed
    from agent_core.planner_recovery import claim_planner_call,complete_planner_claim,PlannerCallError
    from agent_core.workflow.early_nodes import TypedPlanner
    pointer=json.loads((Path(os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS'])/'FIX_epoch_reload_pointer.json').read_text())
    # The final receipt additionally runs this reader in a separate process and compares process nonces (PID namespaces may reuse numbers).
    state=load_plan_state(pointer['root'],pointer['task']);before=deepcopy(state)
    epoch=state['task_state']['planning_epoch'];claim=epoch['call_claim']
    assert epoch==pointer['epoch'] and epoch['planner_calls_total']==4
    for _ in range(2):complete_planner_claim(state,claim,claim['response_integrity'],outcome=claim['outcome'])
    assert state==before
    with pytest.raises((PlannerAdmissionClosed,PlannerCallError)):
        claim_planner_call(pointer['root'],pointer['task'],state,claim['scope'],claim['output_policy'])
    classified=TypedPlanner(data_path=pointer['root'],source=None).classify({}, {},state)
    assert classified['type']=='terminal'
    assert load_plan_state(pointer['root'],pointer['task'])['task_state']['planning_epoch']==epoch
    save('FIX_epoch_independent_read',{'writer_pid':pointer['writer_pid'],'reader_pid':os.getpid(),'writer_process_nonce':pointer['writer_process_nonce'],'reader_process_nonce':PROCESS_NONCE,
        'epoch':epoch,'typed_outcome':classified,'post_reload_claims':0,'post_reload_dispatches':0})


def test_public_stop_example_has_concrete_complete_values():
    from agent_core.plan_contract import planner_stop_wire_example,validate_planner_stop
    example=planner_stop_wire_example()
    assert '<' not in json.dumps(example) and '>' not in json.dumps(example)
    assert validate_planner_stop(example)['verified'] is False
    assert example['diagnostic']['error_codes']==['PLAN_RESOURCE_ROLE_ASSOCIATION_MISSING']

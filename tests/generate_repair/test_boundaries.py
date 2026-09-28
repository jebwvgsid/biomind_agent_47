import asyncio,json,os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage,HumanMessage,SystemMessage
from tests.react_v3.support import task_fixture
from tests.generate_repair.test_correctness import install_receipt,sample_plan,write_samples,Model,schemas
from agent_core.plan_tracker import load_plan_state,save_plan_state,save_execution_plan,StateCASConflict
from agent_core.work_outputs import resolve_output,WorkOutputError
from agent_core.completion_gate import verify_work_result
from agent_core.work_budget_types import ToolLoopLimit
from agent_core.work_validation import validate_step_facts
from tests.generate_repair.test_reuse_delivery import record


def test_A04_A08_D03_negative_contracts_and_authorized_external():
 async def run():
  async with task_fixture('negative-paths') as f:
   p=sample_plan(f);save_execution_plan(f.root,f.task_id,p);path=write_samples(f)
   state=install_receipt(f,[path])
   for body in ['', 'garbage\n','sample\tmatrix\nleaf\t/wrong\n']:
    path.write_text(body);state=install_receipt(f,[path]);assert not verify_work_result(state,{'message':'Review'},project_dir=f.root)['passed']
   with pytest.raises(WorkOutputError):resolve_output(state,'../unauthorized.txt',f.root)
   outside=f.root.parent/(f.root.name+'-authorized.txt');outside.write_text('explicit external output')
   try:
    state['current_task']['task_spec']['output_contracts']=[{'artifact_id':'ext','canonical_path':str(outside),'artifact_kind':'file','source':'user_explicit'}]
    state['current_task'].pop('task_semantic_hash',None);state['current_task'].pop('task_record_hash',None)
    assert resolve_output(state,str(outside),f.root)==outside
   finally:outside.unlink()
   obs={'ok':True,'resolution_bp':10000,'inputs':[{'resource_handle':s,'present':True,'target_group':'/resolutions/10000'} for s in ['leaf','bud']]}
   with pytest.raises(ValueError):validate_step_facts(state,{'outputs':[]},[{'stdout':json.dumps(obs)}],[],{'kind':'resolution'})
   stdout='sample\tresolution_bp\tstatus\treturn_code\nleaf\t40000\tsuccess\t0\nbud\t40000\tfailed\t1\n'
   with pytest.raises(ValueError,match='per-sample failure'):validate_step_facts(state,{'outputs':[]},[{'stdout':stdout,'returncode':0}],[],{'kind':'sample_results'})
   p['output_contracts']=[{'artifact_id':'beds','path':str(f.root/'{sample}.bed'),'format':'bed'}];p['steps'][0]['outputs']=['beds'];save_execution_plan(f.root,f.task_id,p)
   leaf=f.root/'leaf.bed';leaf.write_text('chr1\t0\t40000\n');state=install_receipt(f,[leaf]);v=verify_work_result(state,{'message':'Missing bud'},project_dir=f.root)
   assert not v['passed'] and any(i['code']=='REQUIRED_OUTPUT_UNAVAILABLE' for i in v['issues'])
 asyncio.run(run())






def test_E01_host_empty_waits_phase_transition_and_cancellation():
 from agent_core.command_observation import CommandObservationCoordinator
 from agent_core.request_state import request_scope
 async def run():
  async with task_fixture('host-waits') as f:
   service=f.agent.command_service;scope=request_scope(load_plan_state(f.root,f.task_id));service.observation_review_intervals=(.004,.004);service.observation_model_review_seconds=2.;service.observation_tick_seconds=.001
   c=CommandObservationCoordinator(service,task_id=f.task_id,request_scope=scope,loop_key='test',stage='generate');waits=[]
   async def wait(args,**kwargs):
    waits.append(args);await asyncio.sleep(.001)
    return {'run_ref':'owned-one','terminal':len(waits)>=8,'status':'exited' if len(waits)>=8 else 'running','next_cursor':len(waits),'stdout_delta':'key-final' if len(waits)>=8 else '', 'progress_phase':'same'}
   batch=[{'tool':'wait_command','admitted':True,'result':{'run_ref':'owned-one','terminal':False,'next_cursor':0,'progress_phase':'same'}}]
   with patch.object(service,'wait_command',side_effect=wait):
    result=await c.before_provider({'silent_reviews':1,'decision_calls':45},batch)
   assert len(waits)==8 and result['observations'][0]['terminal'] and result['observations'][0]['stdout_delta']=='key-final'
   assert all(r['after_cursor']==0 for r in waits)
   service.cancel_request(f.task_id,scope,reason='explicit user cancel')
   with pytest.raises(asyncio.CancelledError):await c.before_provider({},batch)
 asyncio.run(run())








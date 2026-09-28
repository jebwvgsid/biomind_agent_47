import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage
from tests.react_v3.support import task_fixture, actual_micro_runner
from agent_core.plan_tracker import load_plan_state,save_plan_state,save_execution_plan
from agent_core.work_outputs import plan_identity,step_outputs,file_fingerprint,resolve_output,WorkOutputError
from agent_core.completion_gate import verify_work_result
from agent_core.work_budget_types import ToolLoopLimit


def install_receipt(f, files=(), *, step_id=None, stdout='', aid='synthetic-a', returncode=0):
    """Explicit synthetic executor evidence; never represented as a historical receipt."""
    state=load_plan_state(f.root,f.task_id);step_id=step_id or state['current_step_id']
    p=f.root/(aid+'.json')
    actual={'task_id':f.task_id,'attempt_id':aid,'action_id':'action-'+aid,'step_id':step_id,'plan_id':state.get('plan_id'),'plan_version':state['plan_version'],
        'work_plan_identity':plan_identity(state),'status':'exited','terminal':True,'execution_protocol_version':3,'returncode':returncode,'exit_code':returncode,'stdout':stdout,
        'before_files':{},'after_files':{str(path):{'exists':True,'size':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns} for path in files},'receipt_ref':str(p),'cwd':str(f.root)}
    p.write_text(json.dumps(actual));state['execution_evidence']=deepcopy(actual)
    state.setdefault('provenance_state',{}).setdefault('attempts',{})[aid]={'attempt_id':aid,'action_id':actual['action_id'],'step_id':step_id,'returncode':returncode,'status':'SUCCEEDED','command_receipt':str(p),'exit_status_path':str(p)}
    save_plan_state(f.root,f.task_id,state)
    return load_plan_state(f.root,f.task_id)


def sample_plan(f):
    return {'path_bindings':{'project_dir':str(f.root),'samples_tsv':str(f.root/'samples.tsv'),'leaf_matrix':str(f.root/'leaf.mcool'),'bud_matrix':str(f.root/'bud.mcool')},
        'parameters':{'samples':['leaf','bud'],'resolution_bp':40000},
        'steps':[{'step_id':'1','description':'Write samples','outputs':['samples_tsv']}]}


def write_samples(f):
    p=f.root/'samples.tsv';p.write_text('sample\tmatrix\nleaf\t'+str(f.root/'leaf.mcool')+'\nbud\t'+str(f.root/'bud.mcool')+'\n');return p


def test_A01_A02_A03_A04_paths_content_templates():
 async def run():
  async with task_fixture('paths') as f:
   payload=sample_plan(f);save_execution_plan(f.root,f.task_id,payload);p=write_samples(f)
   state=install_receipt(f,[p]);v=verify_work_result(state,{'message':'Reviewed samples'},project_dir=f.root)
   assert v['passed'],v
   assert v['files'][0]['path']==str(p) and not (f.root/'samples_tsv').exists()
   p.unlink();(f.root/'samples_tsv').write_text('wrong\n')
   assert not verify_work_result(state,{'message':'wrong alias'},project_dir=f.root)['passed']
   p.write_text('sample\tmatrix\nleaf\t/wrong\n');state=install_receipt(f,[p])
   assert any(i['code']=='OUTPUT_CONTENT_INVALID' for i in verify_work_result(state,{'message':'bad'},project_dir=f.root)['issues'])
   with pytest.raises(WorkOutputError,match='OUTPUT_MAPPING_MISSING'):resolve_output(state,{'artifact_ref':'unknown'},f.root)
   assert resolve_output(state,'LICENSE',f.root)==f.root/'LICENSE'
   assert resolve_output(state,'relative/file.txt',f.root)==f.root/'relative/file.txt'
   state['plan_document']['output_contracts']=[{'artifact_id':'samples_tsv','path':str(f.root/'other')}]
   with pytest.raises(WorkOutputError,match='OUTPUT_MAPPING_CONFLICT'):resolve_output(state,'samples_tsv',f.root)
   state['plan_document']['output_contracts']=[{'artifact_id':'domains','path':str(f.root/'{sample}_domains.bed'),'format':'bed'}]
   state['steps'][0]['outputs']=['domains'];rows=step_outputs(state,f.root)
   assert {r['sample'] for r in rows}=={'leaf','bud'}
   assert {Path(r['canonical_path']).name for r in rows}=={'leaf_domains.bed','bud_domains.bed'}
   state['plan_document']['parameters']['samples']=[]
   with pytest.raises(WorkOutputError,match='OUTPUT_TEMPLATE_UNRESOLVED'):step_outputs(state,f.root)
 asyncio.run(run())






class Model:
 def bind_tools(self,schemas,**kw):return self

def schemas(names):return [{'type':'function','function':{'name':n,'description':n,'parameters':{'type':'object','properties':{}}}} for n in names]







def test_B03_B05_shared_accounting_and_migration():
 from types import SimpleNamespace
 from agent_core.work_budget import request_policy,request_totals,charge_recovery_event
 from agent_core.request_state import request_scope
 async def run():
  async with task_fixture('shared-budget') as f:
   state=load_plan_state(f.root,f.task_id);scope=request_scope(state)
   state['task_state']['tool_loops']={'generate-old-step':{'request_scope':scope,'budget_policy_version':2,'decision_calls':1,'work_calls':5}}
   f.agent.command_service.command_max_decisions=2
   policy=request_policy(state,scope,f.agent.command_service);save_plan_state(f.root,f.task_id,state)
   payload={'seed_id':'seed','turn_ordinal':1,'transport_attempt_ordinal':1}
   charge_recovery_event(f.root,f.task_id,f.agent.command_service,'recovery_model_turn_started',payload)
   charge_recovery_event(f.root,f.task_id,f.agent.command_service,'recovery_model_turn_started',{**payload,'transport_attempt_ordinal':2})
   state=load_plan_state(f.root,f.task_id);assert request_totals(state,scope)['decision_calls']==2
   with pytest.raises(ToolLoopLimit):charge_recovery_event(f.root,f.task_id,f.agent.command_service,'recovery_model_turn_started',{**payload,'turn_ordinal':2})
   policy=request_policy(state,{**scope,'request_key':'new'},SimpleNamespace())
   assert policy['limits']['decisions']==2 and 'inherited' in policy['sources']['decisions']
   legacy={'task_state':{'tool_loops':{'old':{'request_scope':scope,'request_metrics':[{'budget':{'work_limit':24,'model_decision_limit':27,'groups':[{'tools':['inspect_paths'],'limit':8}]}}]}}}}
   policy=request_policy(legacy,scope,SimpleNamespace());assert policy['limits']=={'work':24,'decisions':27,'inspect':8}
   assert set(policy['sources'].values())=={'legacy_source_unknown_preserved'}
   fresh=request_policy({'task_state':{}},scope,SimpleNamespace());assert all(v is None for v in fresh['limits'].values())
 asyncio.run(run())







def test_A06_legacy_relative_output_project_change_invalidates_progress():
 async def run():
  async with task_fixture('legacy-project-binding') as f:
   payload={'path_bindings':{'project_dir':str(f.root/'old')},'steps':[{'step_id':'1','description':'Same','outputs':['result.txt']},{'step_id':'2','description':'Next','outputs':['next.txt'],'dependencies':['1']}]}
   save_execution_plan(f.root,f.task_id,payload)
   state=load_plan_state(f.root,f.task_id);state['steps'][0]['outputs']=['result.txt'];state['steps'][0]['status']='done';save_plan_state(f.root,f.task_id,state)
   payload['path_bindings']['project_dir']=str(f.root/'new')
   changed=save_execution_plan(f.root,f.task_id,payload)
   assert changed['steps'][0]['status']=='pending' and changed['steps'][1]['status']=='pending'
   assert changed['steps'][0]['outputs'][0]['canonical_path']==str(f.root/'new/result.txt')
 asyncio.run(run())

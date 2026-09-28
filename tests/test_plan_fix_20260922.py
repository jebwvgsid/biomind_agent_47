"""Confirmed Plan fix acceptance: controlled providers, real admission/CAS, tiny files only."""
import asyncio, json, os, shlex, hashlib, importlib.util, sys, types
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage
from tests.react_v3.support import task_fixture, actual_micro_runner
from tests.plan_fix_support import derivative, exact_incident_io, read, wires, final_obligations
from tests.generate_repair.test_correctness import install_receipt
from agent_core.plan_tracker import load_plan_state, save_plan_state, save_execution_plan
from agent_core.plan_contract import prepare_simple_plan
from agent_core.completion_gate import verify_work_result, work_verification_context
from agent_core.work_outputs import plan_identity, materialize_outputs, WorkOutputError
from agent_core.request_state import current_finish, request_scope
from agent_core.action_runtime import execute_formal_command
R=Path(os.environ['TMPDIR'])
def record(n,v):(R/(n+'.json')).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str))
def obs_plan(f, outputs=None):
 p={'path_bindings':{'project_dir':str(f.root),'input':str(f.root/'input.txt')},'steps':[
 {'step_id':'check','description':'Check exact input','outputs':outputs or [],'validation':{'kind':'observation','checks':[{'resource_handle':'input','path':str(f.root/'input.txt'),'predicate':'readable'}]}},
 {'step_id':'next','description':'Later work','dependencies':['check'],'outputs':['later.txt']}]}
 return p

def observation(f, *, value=True):return {'kind':'observation','checks':[{'resource_handle':'input','path':str(f.root/'input.txt'),'predicate':'readable','value':value}]}
def input_check_command(f, checks):
 # Real tiny-file read-only check with its own negative rc. No science/env probe.
 code='import json,os,sys; checks='+repr(checks)+'; rows=[dict(c,value=os.path.isfile(c["path"]) and os.access(c["path"],os.R_OK)) for c in checks]; print(json.dumps(dict(kind="observation",checks=rows))); sys.exit(0 if all(r["value"] for r in rows) else 1)'
 return '/usr/bin/python3 -c '+shlex.quote(code)

def test_A01_original_exact_identity_still_rejects(tmp_path):
 with exact_incident_io(tmp_path) as s:
  # Supplement the previously exported projection with exact source request counters; no identity edits.
  scope=json.loads((Path(__file__).parent/'plan_fix_incident_request_scope.json').read_text())
  s['conversation_evidence']=scope['conversation_evidence']
  identity=plan_identity(s);checks=[verify_work_result(s,read('generate-'+str(n).zfill(4)+'_response.json')['tool_calls'][0]['args'],project_dir='/public/home/liusiqi/03.毕设') for n in (20,21,22)]
  assert all(not c['passed'] and any(i['code']=='OUTPUT_PROVENANCE_UNCONFIRMED' for i in c['issues']) for c in checks)
  assert identity==read('attempt-1f3487e131f756cfa5eb37fd.json')['work_plan_identity']
  record('A01-original-replay',{'identity':identity,'checks':checks,'scientific_executions':0})




@pytest.mark.parametrize('bad',['wrong_path','negative','parameter','wrong_plan','wrong_step','no_receipt','unfinished','environment','earlier_negative','valid_multi'])
def test_A05_A06_observation_scope(bad):
 async def run():
  async with task_fixture('observation-'+bad) as f:
   p=obs_plan(f);rule=p['steps'][0]['validation'];rule['parameters']={'threshold':2}
   if bad=='environment':rule['execution_env']={'CONDA_PREFIX':'/approved'}
   if bad in {'valid_multi','earlier_negative'}:
    rule['checks'].append({**rule['checks'][0],'predicate':'nonempty'})
   save_execution_plan(f.root,f.task_id,p);value=observation(f);value['parameters']={'threshold':3 if bad=='parameter' else 2}
   if bad=='wrong_path':value['checks'][0]['path']='/other'
   if bad=='negative':value['checks'][0]['value']=False
   if bad in {'valid_multi','earlier_negative'}:
    early=deepcopy(value);early['checks'][0]['value']=bad!='earlier_negative';install_receipt(f,stdout=json.dumps(early),aid='early')
    value['checks'][0]['predicate']='nonempty'
   if bad=='no_receipt':s=load_plan_state(f.root,f.task_id)
   else:
    s=install_receipt(f,stdout=json.dumps(value))
    if bad in {'wrong_plan','wrong_step','unfinished'}:
     ref=Path(s['execution_evidence']['receipt_ref']);r=json.loads(ref.read_text())
     if bad=='wrong_plan':r['work_plan_identity']='wrong'
     elif bad=='wrong_step':r['step_id']='wrong'
     else:r.update(terminal=False,status='running')
     ref.write_text(json.dumps(r))
   v=verify_work_result(s,{'message':'observations'},project_dir=f.root)
   assert v['passed']==(bad=='valid_multi'),v
   if bad=='valid_multi':assert len(v['source_receipts'])==2
   record('observation-'+bad,v)
 asyncio.run(run())

def test_A02_diagnostics_all_fields_and_original_prose():
 original=read('accepted_raw_plan.json');p=prepare_simple_plan(original)
 assert len(p['validation_not_evaluated'])==8
 broken=deepcopy(original)
 for r in broken['steps']:r['validation']={'kind':'invented'}
 with pytest.raises(ValueError) as e:prepare_simple_plan(broken)
 assert len(e.value.issues)==8
 record('validation-diagnostics',{'notes':p['validation_not_evaluated'],'errors':e.value.issues})

def test_A16_A17_idempotence_wording_cycle_and_progress():
 async def run():
  async with task_fixture('cycles') as f:
   p=obs_plan(f);a=save_execution_plan(f.root,f.task_id,p);first=plan_identity(a)
   assert plan_identity(save_execution_plan(f.root,f.task_id,p))==first
   prose=deepcopy(p);prose['steps'][0]['description']='New wording';prose['revision']='uuid-wrapper'
   assert plan_identity(save_execution_plan(f.root,f.task_id,prose))==first
   p2=deepcopy(p);p2['steps'][0]['validation']['checks'][0]['predicate']='nonempty';save_execution_plan(f.root,f.task_id,p2)
   with pytest.raises(ValueError,match='CYCLE_NO_PROGRESS'):save_execution_plan(f.root,f.task_id,p)
   assert load_plan_state(f.root,f.task_id)['plan_revision']==a['plan_revision']+1
   record('progress-cycle',{'same_and_prose_identity_retained':True,'A_B_A_rejected':True})
 asyncio.run(run())

@pytest.mark.parametrize('mode',['touch','copy_old','placeholder'])
def test_A08_original_preflight_mutations_not_checks(mode):
 async def run():
  async with task_fixture('not-check-'+mode) as f:
   save_execution_plan(f.root,f.task_id,derivative(f));p=f.root/'out/stage_status.tsv';p.write_text('stage\tstatus\nold\tdone\n')
   other=f.root/'old.txt';other.write_text('old unrelated content')
   cmd='touch '+shlex.quote(str(p)) if mode=='touch' else 'cp '+shlex.quote(str(other))+' '+shlex.quote(str(p)) if mode=='copy_old' else 'printf placeholder > '+shlex.quote(str(p))
   launches=[]
   with actual_micro_runner(launches):await execute_formal_command(f.agent.command_service,{'cmd':cmd,'workdir':str(f.root)},task_id=f.task_id,call_id='not-a-check')
   v=verify_work_result(load_plan_state(f.root,f.task_id),{'message':'check complete'},project_dir=f.root)
   assert not v['passed'] and any(i['code']=='OUTPUT_CONTENT_INVALID' for i in v['issues'])
   record('not-check-'+mode,v)
 asyncio.run(run())

def test_A09_exact_output_rules_and_conflict():
 async def run():
  async with task_fixture('exact') as f:
   p={'path_bindings':{'project_dir':str(f.root)},'output_contracts':[{'artifact_id':'samples_tsv','path':str(f.root/'data.tsv'),'role':'samples','format':'tsv'}],'steps':[{'description':'Any title','outputs':[str(f.root/'data.tsv')]}]}
   s=save_execution_plan(f.root,f.task_id,p);r=materialize_outputs(s,[str(f.root/'data.tsv')],f.root);assert r[0]['role']=='samples'
   s['plan_document']['output_contracts'].append({'path':str(f.root/'data.tsv'),'role':'balance'})
   with pytest.raises(WorkOutputError,match='OUTPUT_CONTRACT_CONFLICT'):materialize_outputs(s,[str(f.root/'data.tsv')],f.root)
 asyncio.run(run())


def test_A20_cli_old_new_negative_without_scientific_dependencies():
 path=Path(__file__).parents[1]/'skills/3d-dna/tad-calling/scripts/check_cooler_resolution.py'
 spec=importlib.util.spec_from_file_location('isolated_resolution_checker',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 results=[]
 for legacy in [False,True]:
  for positive in [True,False]:
   args=['--input','x','/tiny/input','--resolution-bp','40000']+(['--observation','observed_resolution'] if legacy else [])
   with patch.dict(sys.modules,{'h5py':types.ModuleType('h5py'),'cooler':types.ModuleType('cooler')}),patch.object(m,'check',side_effect=lambda samples,res:m_check(samples,res,positive)):
    rc=m.main(args);assert rc==(0 if positive else 1);results.append({'legacy':legacy,'positive':positive,'rc':rc})
 record('cli-compatibility',results)

def m_check(samples,res,positive):
 return {'ok':positive,'resolution_bp':res,'inputs':[{'resource_handle':h,'path':p,'present':positive,'target_group':'/resolutions/'+str(res)} for h,p in samples]}








def test_A13_negative_check_resolved_by_new_complete_check():
 async def run():
  async with task_fixture('check-retry') as f:
   p=obs_plan(f);save_execution_plan(f.root,f.task_id,p);launch=[]
   with actual_micro_runner(launch):
    first=await execute_formal_command(f.agent.command_service,{'cmd':input_check_command(f,p['steps'][0]['validation']['checks']),'workdir':str(f.root)},task_id=f.task_id,call_id='negative-check')
    assert first['returncode']!=0
    (f.root/'input.txt').write_text('test represents user supplying missing tiny input')
    second=await execute_formal_command(f.agent.command_service,{'cmd':input_check_command(f,p['steps'][0]['validation']['checks']),'workdir':str(f.root)},task_id=f.task_id,call_id='positive-check')
   s=load_plan_state(f.root,f.task_id);v=verify_work_result(s,{'message':'New exact complete observation'},project_dir=f.root)
   assert v['passed'] and v['superseded_check_attempts']==[first['attempt_id']]
   assert len(v['source_receipts'])==2 and first['returncode']!=0
   record('negative-to-positive',v)
 asyncio.run(run())






def test_A02_descriptive_string_remains_not_evaluated():
 async def run():
  async with task_fixture('prose-validation') as f:
   s=save_execution_plan(f.root,f.task_id,{'steps':[{'description':'Write','outputs':['tiny.txt'],'validation':'A human interpretation'}]})
   p=f.root/'tiny.txt';p.write_text('tiny');s=install_receipt(f,[p]);v=verify_work_result(s,{'message':'Actual file only'},project_dir=f.root)
   assert v['passed'] and any(isinstance(r,dict) and r.get('status')=='not_evaluated' for r in v['not_checked'])
 asyncio.run(run())









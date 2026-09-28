"""V1 implementation acceptance: controlled provider, real production components."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from tests.react_v3.support import task_fixture, actual_micro_runner
from tests.plan_fix_support import derivative, final_obligations, wires
from tests.generate_repair.test_correctness import install_receipt
from agent_core.plan_tracker import load_plan_state, save_plan_state, save_execution_plan
from agent_core.completion_gate import verify_work_result
from agent_core.workspace_files import WorkspaceFileService

OUT = Path(os.environ['TMPDIR']).parent/'witnesses'
def record(name, data):
    (OUT/(name+'.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))






def test_baseline_issue_diagnosis():
 async def run():
  async with task_fixture('baseline-diagnosis') as f:
   save_execution_plan(f.root,f.task_id,derivative(f));final_obligations(f)
   out=f.root/'out/stage_status.tsv';out.write_bytes((Path(__file__).parent/'plan_fix_evidence/stage_status_original.tsv').read_bytes())
   install_receipt(f,stdout='old readonly check; no producer')
   s=load_plan_state(f.root,f.task_id)
   v=verify_work_result(s,{'message':'Only checks'},project_dir=f.root)
   record('baseline-issue-diagnosis',v)
   assert not v['passed']
 asyncio.run(run())



def install_sequence(f, operations, requests):
 async def invoke(messages, **kwargs):
  ordinal=len(requests)
  requests.append({'messages':wires(messages),'tools':deepcopy(f.provider.bound_tool_schemas)})
  if ordinal>=len(operations):raise AssertionError('Unexpected extra provider call '+str(ordinal))
  operation=operations[ordinal]
  name,args=operation(f,messages) if callable(operation) else operation
  return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':'v1-call-'+str(ordinal)}],response_metadata={'complete':True,'finish_reason':'tool_calls'})
 f.provider.ainvoke=invoke




def exact_selector(calls):
 from tests.test_phase_context_compaction_runtime import _request_from_wire,_valid_plan
 async def select(messages,**kwargs):
  request=_request_from_wire(messages);calls.append(deepcopy(request))
  required=set(request.get('selection_policy',{}).get('required_exact_unit_ids') or [])
  required.update(u['unit_id'] for u in request['source_manifest']['units'] if u.get('provenance',{}).get('required_exact'))
  # Host protocol groups remain indivisible, including the latest tool pair.
  for group in request['source_manifest'].get('protocol_groups',{}).values():
   if required.intersection(group):required.update(group)
  return AIMessage(content=json.dumps(_valid_plan(request,retain=sorted(required),summarize=[u['unit_id'] for u in request['source_units'] if u['unit_id'] not in required])))
 return select



@pytest.mark.parametrize('layout',['default','explicit_output','external_state'])
def test_A02_host_sources_in_three_layouts(layout):
 from agent_core.command_execution import CommandExecutionService
 from agent_core.draft_files import DraftFileSession
 from agent_core.planner_consultation import source_locators,reader_sources
 from agent_core.request_state import request_scope
 from agent_core.work_outputs import plan_identity
 async def run():
  async with task_fixture('layout-'+layout) as f:
   project=f.root/'project';project.mkdir()
   output=f.root/'explicit-output';output.mkdir()
   service=CommandExecutionService(data_path=f.root,project_dir=project if layout=='external_state' else f.root)
   state=load_plan_state(f.root,f.task_id)
   loc=source_locators(f.root,f.task_id,state)
   state['task_state']['plan_revision_request']={'consultation_id':'fixture-consult','request_scope':request_scope(state),'plan_identity':plan_identity(state),'step_id':state['current_step_id'],'sources':loc,'status':'pending'}
   save_plan_state(f.root,f.task_id,state)
   session=DraftFileSession(WorkspaceFileService(service),task_id=f.task_id,stage='planner',
    load_state=lambda:load_plan_state(f.root,f.task_id),persist_state=lambda s:save_plan_state(f.root,f.task_id,s),validate_submit=lambda *a:{'accepted':False},
    directory=output if layout=='explicit_output' else None,read_sources=reader_sources(state))
   receipts=[]
   for locator in loc.values():
    row=await session.dispatch('read_file',{'path':locator['path'],'expected_sha256':locator['sha256']})
    assert row['status']=='completed',row;receipts.append(row)
   foreign=f.root/'task_outputs/another-task/secret.txt';foreign.parent.mkdir(parents=True);foreign.write_text('other-task')
   assert (await session.dispatch('read_file',{'path':str(foreign)}))['status']=='not_run'
   link=session.directory/'link.txt';link.symlink_to(foreign)
   assert (await session.dispatch('read_file',{'path':str(link)}))['status']=='not_run'
   record('source-layout-'+layout,receipts)
 asyncio.run(run())





















def test_C05_incomplete_large_input_fingerprint_is_not_full_version_proof():
 from tests.test_plan_fix_20260922 import input_check_command
 from agent_core.action_runtime import execute_formal_command
 async def run():
  async with task_fixture('partial-input-hash') as f:
   p=f.root/'larger-input.txt';p.write_bytes(b'x'*(4*1024*1024+1))
   checks=[{'resource_handle':'x','path':str(p),'predicate':'readable'}]
   plan={'path_bindings':{'x':str(p),'project_dir':str(f.root)},'steps':[
    {'step_id':'1','description':'Read input','inputs':[str(p)],'outputs':[str(f.root/'former.txt')]}]}
   save_execution_plan(f.root,f.task_id,plan);launches=[]
   with actual_micro_runner(launches):await execute_formal_command(f.agent.command_service,{'cmd':input_check_command(f,checks),'workdir':str(f.root)},task_id=f.task_id,call_id='bounded-read')
   info=p.stat()
   with p.open('r+b') as stream:stream.seek(-1,2);stream.write(b'y')
   os.utime(p,ns=(info.st_atime_ns,info.st_mtime_ns))
   revised=deepcopy(plan);revised['steps'][0].update(outputs=[],validation={'kind':'observation','checks':checks})
   state=save_execution_plan(f.root,f.task_id,revised)
   result=verify_work_result(state,{'message':'A prefix-only hash cannot prove an unchanged whole input'},project_dir=f.root)
   assert not result['passed'] and 'REVALIDATION_INPUT_FINGERPRINT_INCOMPLETE' in json.dumps(result)
   record('incomplete-input-fingerprint',{'verification':result,'launches':launches})
 asyncio.run(run())




def test_F05_unknown_usage_extensions_obey_existing_redaction():
 from agent_core.provider_usage import raw_usage
 from execution.profiling import _response_usage
 from agent_core.context_lineage import response_payload
 value={'prompt_tokens':100,'completion_tokens':2,'total_tokens':102,'vendor_counter':{'zero':0,'missing_value':None},'api_key':'synthetic-private-value'}
 response=AIMessage(content='synthetic',response_metadata={'token_usage':value})
 actual=raw_usage(response)
 assert actual['reported'] and actual['events'][0]['vendor_counter']==value['vendor_counter']
 assert actual['events'][0]['total_tokens']==102 and actual['events'][0]['api_key']=='[REDACTED]'
 assert actual['redactions'] and 'synthetic-private-value' not in json.dumps(response_payload(response))
 assert 'synthetic-private-value' not in json.dumps(_response_usage(response))
 record('usage-extension-redaction',{'raw_safe':actual,'normalized':_response_usage(response),'real_usage':'NOT_MEASURED'})

"""P05/P07/P08/P09: recorded 21-step Planner through real production graph.
Runs only in the guarded /tmp candidate bootstrap. Model and human-input IO are
external substitutes; planner/binder/compiler/CAS/store/phase/graph stay real.
"""
from __future__ import annotations
import asyncio,copy,json,os,hashlib,tempfile,shutil
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from langchain_core.messages import AIMessage,HumanMessage
import bio_agents
from bio_agents import BioAnalystAgent
from agent_core.plan_tracker import save_plan_state,load_plan_state,canonical_task_spec_for_state,commit_consultant_candidate_state
from agent_core.task_spec import task_semantic_hash
from agent_core.skill_access import SkillAccessService,new_skill_access_state,record_provider_visible_materials
from agent_core.skill_manager import SkillManager
from agent_core.workflow.outcomes import ResumeState
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.human_io import HumanInputUnavailable

F=Path(__file__).parent/'fixtures/planner_session_1788855155'
if not F.exists():F=Path(__file__).parent/'fixtures'
SKILLS=['genomics/wgs-short-read-mapping','genomics/wgs-gatk-snp-indel']

class RecordedModel:
 def __init__(self,responses):self.responses=list(responses);self.calls=[];self.binds=[];self.observe=None;self.observed=[];self.cancel_target=None
 def bind_tools(self,tools,**kwargs):return self
 def bind(self,**kwargs):self.binds.append(kwargs);return self
 def model_copy(self,update=None):return self
 async def ainvoke(self,messages,**kwargs):
  self.calls.append([provider_visible_payload(m) for m in messages])
  n=len(self.calls)-1
  if self.observe:self.observed.append(self.observe())
  if n>=len(self.responses):raise AssertionError('Unexpected external model call: '+str(n))
  response=self.responses[n]
  if isinstance(response,asyncio.CancelledError) and self.cancel_target:
   self.cancel_target.cancel('isolated process-restart boundary')
   await asyncio.Future()
  if isinstance(response,BaseException):raise response
  return AIMessage(content=response)

def wire_objects(messages):
 decoder=json.JSONDecoder()
 def descend(value):
  yield value
  if isinstance(value,dict):
   for child in value.values():yield from descend(child)
  elif isinstance(value,list):
   for child in value:yield from descend(child)
 for m in messages:
  text=str(m.get('content',''))
  for index,char in enumerate(text):
   if char not in '[{':continue
   try:value,_=decoder.raw_decode(text[index:])
   except (ValueError,TypeError):continue
   yield from descend(value)

def evidence(name,value):
 root=Path(os.environ['BIOMIND_OFFLINE_RESULTS']).parent/'planner-recorded-evidence';root.mkdir(exist_ok=True)
 (root/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))

class Harness:
 def __init__(self,root,model):
  self.root=root;self.model=model;self.stack=ExitStack();self.human_answers=[]
  for key,value in [('init_chat_model',lambda *args,**kwargs:model),('BIOMIND_MEMORY_DIR',str(root/'memory')),('BIOMIND_WIKI_ENABLED',False),('ENABLE_ROOT_CAUSE_ANALYZER',False),('SCHEMA2_CONTEXT_COMPACTION_ENABLED',False)]:
   self.stack.enter_context(patch.object(bio_agents,key,value))
  self.agent=BioAnalystAgent(data_path=str(root),enable_persistence=False,use_llm_supervisor=False)
  self.manager=SkillManager(str(Path(bio_agents.__file__).parent/'skills'));self.service=SkillAccessService(self.manager)
  self.agent.skill_manager=self.manager;self.agent.skill_access_service=self.service
  # No stdin access. This external reader reports unavailable; actual PlanReview
  # must durably suspend through its production HITL path.
  def no_human(*a,**k):
   if self.human_answers:return self.human_answers.pop(0)
   raise HumanInputUnavailable('offline test: awaiting real review')
  self.stack.enter_context(patch.object(bio_agents,'safe_input',no_human))
  async def no_observe(*a,**k):return None
  async def no_compress(messages,*a):return list(messages),False
  self.agent._observe_session_memory=no_observe
  self.agent._finalize_task_memory=no_observe
  self.agent.memory_manager.maybe_compress=no_compress
 async def close(self):
  try:
   # Separate teardown: the previous probe retained aclose's to_thread timeout.
   # Invoke its same bounded release helper directly, with no model or store mock.
   result=bio_agents.release_observation_writer(self.agent.session_archive.db_path,timeout_seconds=.2)
   self.agent._session_archive_writer_released=True
   evidence('cleanup-'+self.root.name,{'method':'direct production release_observation_writer helper; aclose wrapper not re-tested','result':result})
  finally:self.stack.close()
 def bootstrap(self,task_id):
  recorded=json.loads((F/'recorded_state.json').read_text())
  task=copy.deepcopy(recorded['current_task']['task_spec']);task['task_id']=task_id
  # Relocation only, no source file opened. Preserve even the recorded invalid
  # paired_fastq value so this PLAN test does not claim INPUT-FIX correctness.
  def relocate(x):
   if isinstance(x,str):return x.replace('/public/home/liusiqi/03.毕设',str(self.root/'relocated'))
   if isinstance(x,list):return [relocate(v) for v in x]
   if isinstance(x,dict):return {k:relocate(v) for k,v in x.items()}
   return x
  task=relocate(task)
  submission=task['goal']
  state,_,raw_ref=self.agent._persist_user_submission(task_id,submission)
  # This synthetic task owns its freshly persisted raw submission. Historical
  # object identities are not copied into its authoritative audit root.
  task['raw_submission_ref']=raw_ref
  state['selected_skill_ids']=SKILLS;state['workflow_routing_schema_version']=2
  access=self.service.ensure_selected_skill_refs(new_skill_access_state(),SKILLS,task_revision=task['revision'])
  access,docs=self.service.load_skill_documents(access,SKILLS,task_revision=task['revision'],request_id='offline-consultant-recorded-handoff')
  access=record_provider_visible_materials(access,[{'kind':'skill_document','snapshot_id':d['snapshot_id'],'content_hash':d['content_hash']} for d in docs],phase='consultant',invocation_id='offline-consultant-recorded-handoff',event_id='recorded-material-handoff')
  ts=state.setdefault('task_state',{})
  ts.update(status='running',workflow_routing_schema_version=2,selected_skills=SKILLS,skill_access_state=access,
    accepted_consultant_handoff=copy.deepcopy(recorded['task_state']['accepted_consultant_handoff']),
    consultation_state=copy.deepcopy(recorded['task_state']['consultation_state']))
  ts['consultation_state']['phase']='ready_to_plan';ts['consultation_state']['consultation_status']='ready'
  ts['consultation_state']['completion_decision']='ready_to_plan'
  ts.pop('planning_failure',None);ts.pop('planning_epoch',None);ts.pop('cancellation_receipt',None)
  state=commit_consultant_candidate_state(self.root,task_id,{'task_spec':task,'selected_skills':SKILLS,'skill_access_state':access,'selected_skill_refs':access['selected_skill_refs'],'consultation_state':ts['consultation_state'],'accepted_consultant_handoff':ts['accepted_consultant_handoff'],'resolved_bindings':{},'pending_question':{}})
  state['workflow_routing_schema_version']=2
  state['task_state']['workflow_routing_schema_version']=2
  state['task_state']['status']='running'
  save_plan_state(self.root,task_id,state)
  return self.input(task_id)
 def input(self,task_id):
  state=load_plan_state(self.root,task_id);task=canonical_task_spec_for_state(state)
  return {'task_id':task_id,'session_id':task_id,'task_mode':'managed','task_status':'running','workflow_routing_schema_version':2,
   'messages':[HumanMessage(content=task['goal'])],'selected_skills':SKILLS,'task_spec':task,'task_revision':task['revision'],
   'resume_state':ResumeState(schema_version=1,type='planning_ready',summary='Resume recorded Consultant handoff for isolated Planner acceptance',scope={'task_id':task_id},payload={'reason_code':'accepted_consultant_handoff'}).to_dict()}
 async def run(self,state):
  self.model.cancel_target=asyncio.current_task()
  return await self.agent.schema2_app.ainvoke(state,config={'configurable':{'thread_id':state['task_id']},'recursion_limit':20})

def test_recorded_21_step_candidate_to_real_plan_review_and_feedback():
 async def scenario():
  with tempfile.TemporaryDirectory(prefix='planner-recorded-21-') as tmp:
   root=Path(tmp);corrected=(F/'corrected_candidate.json').read_text();original=(F/'original_response.txt').read_text()
   model=RecordedModel([original,corrected]);h=Harness(root,model);task='offline-planner-recorded-21'
   try:
    initial=h.bootstrap(task)
    model.observe=lambda:copy.deepcopy(load_plan_state(root,task).get('task_state',{}).get('planning_failure',{}))
    result=await h.run(initial);durable=load_plan_state(root,task)
    evidence('P05-P07-P09',{'provider_calls':model.calls,'provider_binds':model.binds,'observed_failures':model.observed,'result':result,'durable':durable,'source':bio_agents.__file__})
    assert len(model.calls)==2
    assert len(durable['steps'])==21
    assert durable['approval_status']!='approved'
    assert not durable.get('execution_attempts')
    assert result['planner_outcome']['type']=='plan_review_ready',result
    assert result['plan_review_outcome']['type']=='approval_pending',result
    first,second=model.calls
    assert first[:3]==second[:3], 'Frozen System/Seed/Skill prefix changed within same epoch'
    secondtext=json.dumps(second,ensure_ascii=False)
    assert original.strip() in secondtext or json.dumps(original.strip(),ensure_ascii=False)[1:-1] in secondtext
    full_issues=model.observed[1]['validation_issues']
    objects=list(wire_objects(second))
    for issue in full_issues:assert issue in objects,issue
    assert full_issues
    assert 'resource_binding' in secondtext and 'create_reference_indexes' in secondtext
    firsttext=json.dumps(first,ensure_ascii=False)
    for term in ['artifact_contract','resource_kind','lifecycle','transition','produce_files','validation_observation']:
     assert term in firsttext,term
    for doc in h.service.load_skill_documents(durable['skill_access_state'],SKILLS,task_revision=1)[1]:
     assert doc['document'] in '\n'.join(str(m.get('content','')) for m in first),doc['skill_id']
    receipt=durable['task_state']['plan_review_submission'];assert receipt
    assert durable['plan_compilation_receipt']['status']=='compiled'
    assert durable['plan_dataflow_receipt']['status']=='compiled'
    assert not list(root.rglob('execution_result.json'))
   finally:await h.close()
 asyncio.run(scenario())

def test_new_instance_restores_full_recorded_candidate_feedback_and_count():
 async def scenario():
  with tempfile.TemporaryDirectory(prefix='planner-recorded-resume-') as tmp:
   root=Path(tmp);task='offline-planner-recorded-resume'
   first=Harness(root,RecordedModel([(F/'original_response.txt').read_text(),asyncio.CancelledError('isolated process-restart boundary')]))
   try:
    state=first.bootstrap(task)
    # The first full production phase durably rejects raw. Its next external
    # provider is interrupted to emulate loss of this process; no partial Plan
    # is published. A new explicit instance below performs the authorized resume.
    try:
     interrupted_result=await asyncio.create_task(first.run(state))
    except asyncio.CancelledError:
     update={'process_restart_boundary':'provider caller cancelled; propagated','provider_calls':first.model.calls}
    else:
     evidence('P08-interruption-not-propagated',{'result':interrupted_result,'calls':first.model.calls})
     raise AssertionError('Synthetic process interruption was swallowed')
    before=load_plan_state(root,task)
    issues=copy.deepcopy(before['task_state']['planning_failure']['validation_issues'])
    counter=before['task_state']['planning_epoch']['planner_calls_total']
    assert issues and counter==2
    assert not before.get('steps')
   finally:await first.close()
   model=RecordedModel([(F/'corrected_candidate.json').read_text()]);second=Harness(root,model)
   try:
    result=await second.run(second.input(task));after=load_plan_state(root,task)
    evidence('P08',{'first_producer_update':update,'durable_rejection':before,'reloaded_provider_request':model.calls,'durable_after':after,'result':result,'source':bio_agents.__file__})
    assert len(model.calls)==1
    request='\n'.join(str(m.get('content','')) for m in model.calls[0])
    original=json.loads((F/'original_candidate.json').read_text())
    for step in original['steps']:assert step['step_id'] in request,step['step_id']
    for a in original['proposed_artifacts']:
     assert a['local_ref'] in request and a['resource_binding']['canonical_resource_id'] in request
    objects=list(wire_objects(model.calls[0]))
    for issue in issues:
     assert issue in objects,issue
     # Structured JSON from durable feedback must be present, not merely code
     # counts or a human summary (JSON formatting is intentionally ignored).
     assert issue['code'] in request and issue.get('field_path','') in request
     if issue.get('canonical_resource_id'):assert issue['canonical_resource_id'] in request
    assert after['task_state']['planning_epoch']['planner_calls_total']==counter+1
    assert after['task_state']['planning_epoch']['planning_epoch_id']==before['task_state']['planning_epoch']['planning_epoch_id']
    assert len(after['steps'])==21
    assert result['plan_review_outcome']['type']=='approval_pending'
   finally:await second.close()
 asyncio.run(scenario())


def test_corrected_recorded_plan_real_approval_reaches_generate_consumer():
 async def scenario():
  with tempfile.TemporaryDirectory(prefix='planner-recorded-consumer-') as tmp:
   root=Path(tmp);task='offline-planner-recorded-consumer'
   model=RecordedModel([(F/'corrected_candidate.json').read_text(),asyncio.CancelledError('stop after observing Generate request')])
   h=Harness(root,model);h.human_answers=['y']
   try:
    initial=h.bootstrap(task)
    try:result=await h.run(initial)
    except asyncio.CancelledError:pass
    else:
     loaded=load_plan_state(root,task)
     evidence('P09-consumer-unreached',{'result':result,'provider_calls':model.calls,'durable':loaded,'raw_root':json.loads((root/'task_outputs'/task/'plan_state.json').read_text()),'source':bio_agents.__file__})
     retained=Path(os.environ['BIOMIND_OFFLINE_RESULTS']).parent/'P09-failed-durable'
     shutil.copytree(root/'task_outputs'/task,retained,dirs_exist_ok=True)
     raise AssertionError('Generate provider not reached: '+str(loaded.get('load_error') or result.get('generate_outcome')))
    durable=load_plan_state(root,task)
    evidence('P09-minimal-consumer',{'provider_calls':model.calls,'provider_binds':model.binds,'durable':durable,'source':bio_agents.__file__,'human_response':'y through safe_input; production approval machinery unchanged'})
    assert len(model.calls)==2
    assert durable['approval_status']=='approved'
    assert durable['approval_record']
    assert len(durable['steps'])==21
    request='\n'.join(str(m.get('content','')) for m in model.calls[1])
    assert 'preflight_check' in request and 'validation_observation' in request
    assert 'genomics.wgs.short.read.mapping.inspect' in request
    assert 'input.reference_fasta' in request and 'input.paired_fastq' in request
    assert not list(root.rglob('execution_result.json'))
    assert not durable.get('execution_attempts')
   finally:await h.close()
 asyncio.run(scenario())

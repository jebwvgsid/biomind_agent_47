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
from agent_core.plan_tracker import save_plan_state,load_plan_state,canonical_task_spec_for_state,commit_consultant_candidate_state
from agent_core.task_spec import task_semantic_hash
from agent_core.skill_access import SkillAccessService,new_skill_access_state,record_provider_visible_materials
from agent_core.skill_manager import SkillManager
from agent_core.workflow.outcomes import ResumeState
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.human_io import HumanInputUnavailable
from agent_core.planner_context import provider_correction_feedback
from agent_core.provider_message_safety import assert_v2_llm_messages_clean

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






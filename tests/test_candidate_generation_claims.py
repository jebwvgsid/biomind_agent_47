"""Claims use real allocation, approval and atomic CAS; no store substitute."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest
from agent_core.plan_tracker import save_plan_state,load_plan_state,approve_plan_state
from agent_core.workflow.candidate_repair import (candidate_repair_scope,claim_candidate_generation,
 complete_candidate_generation,CandidateRepairReceiptError)
from agent_core.candidate_repair_policy import candidate_repair_limit
from tests.test_base01_allocation_reload import proposed_state


def approved(root):
 state,registry=proposed_state(root);task=state['task_id']
 save_plan_state(root,task,state,allow_canonical_task_write=True,step_effect_capability_registry=registry)
 state=load_plan_state(root,task);approve_plan_state(state,1,approval_source='offline-test-review')
 save_plan_state(root,task,state,step_effect_capability_registry=registry)
 state=load_plan_state(root,task)
 return task,state,candidate_repair_scope(state,task_id=task,step_id=state['current_step_id'])


def test_concurrent_authorization_unknown_no_refund_then_durable_replay(tmp_path):
 task,state,scope=approved(tmp_path)
 assert candidate_repair_limit(state['task_state']['budgets'])==4
 assert candidate_repair_limit({'max_candidate_repairs_per_epoch':2})==2
 def claim(i):
  try:return claim_candidate_generation(tmp_path,task,scope,invocation_id=str(i))
  except CandidateRepairReceiptError as e:return e.code
 with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(claim,[1,2]))
 winners=[r for r in results if isinstance(r,dict)]
 assert len(winners)==1 and 'CANDIDATE_GENERATION_OUTCOME_UNKNOWN' in results
 assert claim(3)=='CANDIDATE_GENERATION_OUTCOME_UNKNOWN'
 winner=winners[0]
 complete_candidate_generation(tmp_path,task,winner,content='recorded candidate')
 complete_candidate_generation(tmp_path,task,winner,content='recorded candidate')
 cached=claim_candidate_generation(tmp_path,task,scope,invocation_id='reconstructed')
 assert cached['cached_response']=='recorded candidate' and cached['claim_id']==winner['claim_id']
 assert len(load_plan_state(tmp_path,task)['task_state']['candidate_repair_state']['provider_claims'])==1
 with pytest.raises(CandidateRepairReceiptError,match='COMPLETION_CONFLICT'):
  complete_candidate_generation(tmp_path,task,winner,content='different callback')
 changed=deepcopy(scope);changed['plan_revision']+=1
 with pytest.raises(CandidateRepairReceiptError,match='SCOPE_STALE'):
  claim_candidate_generation(tmp_path,task,changed,invocation_id='new-request-id')


def test_confirmed_noncandidate_uses_existing_skill_budget_not_semantic_repair(tmp_path):
 task,state,scope=approved(tmp_path)
 claim=claim_candidate_generation(tmp_path,task,scope,invocation_id='tool-request')
 complete_candidate_generation(tmp_path,task,claim,content='recorded typed skill decision',noncandidate=True)
 next_call=claim_candidate_generation(tmp_path,task,scope,invocation_id='after-tool')
 assert next_call['corrective_ordinal']==0 and next_call['claim_id']!=claim['claim_id']

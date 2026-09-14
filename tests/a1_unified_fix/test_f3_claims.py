"""F3 dedup/reload checks using formal allocation, approval and real CAS store.

Forked child inherits the established offline guard; no shell or provider runs.
"""
from copy import deepcopy
import multiprocessing
import pytest
from agent_core.plan_tracker import load_plan_state,save_plan_state
from agent_core.workflow.candidate_repair import (
    claim_candidate_generation,complete_candidate_generation,CandidateRepairReceiptError,
)
from agent_core.candidate_repair_policy import freeze_candidate_policy
from tests.test_candidate_generation_claims import approved
from tests.test_preflight_recovery_audit import save


def child_reload(root,task,scope,pipe):
    try:
        state=load_plan_state(root,task)
        before=state['task_state']['candidate_repair_state']
        claim=claim_candidate_generation(root,task,scope,invocation_id='independent-child-reload')
        after=load_plan_state(root,task)['task_state']['candidate_repair_state']
        pipe.send({'claim_id':claim['claim_id'],'cached_response':claim['cached_response'],
            'policy':claim['policy_receipt'],'before_count':len(before['provider_claims']),
            'after_count':len(after['provider_claims'])})
    except BaseException as exc:pipe.send({'error':type(exc).__name__+': '+str(exc)})
    finally:pipe.close()


def test_frozen_budget_and_claim_survive_unknown_exact_replay_and_independent_reload(tmp_path):
    task,state,scope=approved(tmp_path)
    claim=claim_candidate_generation(tmp_path,task,scope,invocation_id='original-call')
    assert claim['policy_receipt']['limit']==4
    with pytest.raises(CandidateRepairReceiptError,match='OUTCOME_UNKNOWN'):
        claim_candidate_generation(tmp_path,task,scope,invocation_id='retry-unknown')
    before=load_plan_state(tmp_path,task)['task_state']['candidate_repair_state']
    assert len(before['provider_claims'])==1
    # A new process/default cannot rewrite the frozen policy of this scope.
    assert freeze_candidate_policy(deepcopy(before),scope,{'max_candidate_repairs_per_epoch':1})['limit']==4
    complete_candidate_generation(tmp_path,task,claim,content='synthetic public candidate',response_metadata={'finish_reason':'stop'})
    complete_candidate_generation(tmp_path,task,claim,content='synthetic public candidate',response_metadata={'finish_reason':'stop'})
    with pytest.raises(CandidateRepairReceiptError,match='COMPLETION_CONFLICT'):
        complete_candidate_generation(tmp_path,task,claim,content='different late callback')
    context=multiprocessing.get_context('fork');receiver,sender=context.Pipe(duplex=False)
    process=context.Process(target=child_reload,args=(tmp_path,task,scope,sender))
    process.start();sender.close()
    try:
        assert receiver.poll(20),'isolated child did not respond'
        result=receiver.recv();process.join(10)
        assert process.exitcode==0 and 'error' not in result,result
    finally:
        if process.is_alive():process.terminate();process.join(5)
        receiver.close()
    assert result['claim_id']==claim['claim_id']
    assert result['cached_response']=='synthetic public candidate'
    assert result['before_count']==result['after_count']==1
    assert result['policy']['limit']==4
    save('f3-independent-reload',result)


def test_legacy_recorded_limit_and_stopped_scope_are_not_reopened():
    from agent_core.workflow.candidate_repair import latest_candidate_repair_receipt
    scope={'task_id':'synthetic','step_id':'one'}
    legacy={'events':{'stop':{'scope':scope,'repair_limit':2,'repair_ordinal':3,
        'event_id':'stop','status':'internal_error'}}}
    assert freeze_candidate_policy(legacy,scope,{'max_candidate_repairs_per_epoch':4})['limit']==2
    # Stopped status is rejected before any receipt read; no fake authority or IO.
    with pytest.raises(CandidateRepairReceiptError,match='SCOPE_STOPPED'):
        latest_candidate_repair_receipt('/unused','synthetic',{'task_state':{'candidate_repair_state':legacy}},scope)

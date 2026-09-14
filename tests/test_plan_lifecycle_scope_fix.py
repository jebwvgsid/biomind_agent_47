"""Equivalent business controls for the preserved nine historical fixture failures.
Each starts from a candidate admitted by the production graph, not a forged LIFE
scope. Original tests/ledger remain unchanged; adapter observations below are
explicit fixtures, while parsing, registry, claims, stores and approval are real.
"""
import asyncio
from copy import deepcopy
import json
import pytest
from langchain_core.messages import AIMessage
from tests.plan_resource_fix_support import scenario, save
from tests.test_plan_resource_fix import c_fix

CASES=['c_explicit_submission','d_review_execution','e_identity_evidence','e2_rejection_receipt',
       'f_reject_then_correct','h_ready_poison','i_reverse_poison','k3_stale_once','k5_stale_cleared']
POISON={'next_step':'finish','proposed_route':'finish','effective_route':'finish',
        'required_next_action':'GENERATE','route_resolution':{'dispatch_route':'finish'}}


def acceptance(mode):
    async def check(case,task,state,runner):
        from agent_core.plan_tracker import load_plan_state,save_plan_state,approve_plan_state,canonical_task_spec_for_state,render_structured_plan,record_planning_failure
        from agent_core.planner_loop import (record_plan_review_acceptance,record_plan_review_rejection,
            plan_review_acceptance_readiness,plan_review_submission_readiness,record_planner_observation,
            review_evidence_from_issues,classify_planner_lifecycle)
        from agent_core.workflow.early_nodes import TypedPlanner,TypedPlanReview
        pending=load_plan_state(case.root,task)
        scope=pending['task_state']['planning_epoch']['call_claim']['scope']
        assert scope['candidate_scope_id'].startswith('candidate-scope-')
        assert pending['task_state']['planning_epoch']['call_claim']['outcome']=='candidate_ready_for_review'
        planner=TypedPlanner(data_path=str(case.root),source=None)
        review=TypedPlanReview(data_path=str(case.root),source=None,execution_backend='legacy')
        issue={'code':'OUTPUT_IDENTITY_MISMATCH','field_path':'$.steps[1].outputs[0]',
            'expected_identity':'output.annotated_vcf','observed_identity':'plan.annotation','constraint_source':'TaskSpec.outputs[0]'}
        def approved(value):
            value=approve_plan_state(value,value['current_task']['task_revision'],review_event_id='synthetic-explicit-review')
            save_plan_state(case.root,task,value)
            value=record_plan_review_acceptance(load_plan_state(case.root,task))
            save_plan_state(case.root,task,value)
            return load_plan_state(case.root,task)
        if mode=='c_explicit_submission':
            assert planner.classify({}, {},pending)['type']=='plan_review_ready'
            unsubmitted=deepcopy(pending)
            unsubmitted['task_state'].pop('plan_review_submission',None)
            unsubmitted['task_state']['planner_loop']=record_planner_observation(None,action_type='inspect_task_spec',
                action={'task_revision':scope['task_revision']},observation={'status':'inspected'})
            assert not plan_review_submission_readiness(unsubmitted)['ready']
            assert planner.classify({}, {},unsubmitted)['type']=='planning_continues'
        elif mode=='d_review_execution':
            current=approved(pending)
            assert plan_review_acceptance_readiness(current)['ready']
            assert review.classify({}, {},current)['type']=='approved_for_execution'
        elif mode=='e_identity_evidence':
            evidence=review_evidence_from_issues([issue])[0]
            assert evidence['json_path']==issue['field_path'] and evidence['expected']=='output.annotated_vcf'
            assert evidence['actual']=='plan.annotation' and evidence['constraint_source']=='TaskSpec.outputs[0]'
            rejected=record_plan_review_rejection(pending,[issue]);save_plan_state(case.root,task,rejected)
            observed=load_plan_state(case.root,task)['task_state']['planner_loop']['last_observation']
            assert observed['observation']['review_evidence'][0]==evidence
        elif mode=='e2_rejection_receipt':
            missing=review.classify({}, {},pending)
            assert missing['type']=='harness_error' and missing['payload']['error_code']=='PLAN_REVIEW_RESULT_RECEIPT_MISSING'
            rejected=record_plan_review_rejection(pending,[issue]);save_plan_state(case.root,task,rejected)
            repair=review.classify({}, {},load_plan_state(case.root,task))
            assert repair['type']=='revision_required'
            assert repair['payload']['review_evidence'][0]['json_path']==issue['field_path']
        elif mode=='f_reject_then_correct':
            rejected=record_plan_review_rejection(pending,[issue]);save_plan_state(case.root,task,rejected)
            assert classify_planner_lifecycle(rejected)['disposition']=='continue'
            calls=[]
            async def response(messages,*,stage,**kw):
                assert stage=='planner';calls.append(messages)
                return AIMessage(content=json.dumps(c_fix()),response_metadata={'finish_reason':'stop'})
            case.agent._ainvoke_node_llm=response
            invocation={**runner,'task_mode':'managed','task_revision':pending['current_task']['task_revision'],
                'task_spec':canonical_task_spec_for_state(pending),'current_plan':render_structured_plan(pending),
                'selected_skills':['genomics/snpeff-annotation']}
            update=await TypedPlanner(data_path=str(case.root),source=case.agent._schema2_early_sources['planner']).run(invocation)
            assert update['planner_outcome']['type']=='plan_review_ready',update
            corrected=load_plan_state(case.root,task)
            new_scope=corrected['task_state']['planning_epoch']['call_claim']['scope']
            assert new_scope['candidate_scope_id']!=scope['candidate_scope_id'] and len(calls)==1
            assert plan_review_acceptance_readiness(approved(corrected))['ready']
        elif mode=='h_ready_poison':
            transient=deepcopy(pending);transient['approval_status']='approved'
            assert review.classify({},POISON,transient)['type']=='harness_error'
        elif mode=='i_reverse_poison':
            current=approved(pending)
            clean=review.classify({}, {},current);poisoned=review.classify({},POISON,current)
            assert clean['type']=='approved_for_execution' and poisoned==clean
            # A failed subsequent candidate cannot select the historical approval.
            failed=record_planning_failure(current,validation_issues=[issue],planning_epoch_id=scope['planning_epoch_id'],
                task_spec_revision_value=scope['task_revision'],validation_source='equivalent_review_control',candidate=c_fix())
            assert not plan_review_acceptance_readiness(failed)['ready']
        elif mode in {'k3_stale_once','k5_stale_cleared'}:
            calls=[]
            async def source(value):
                calls.append(deepcopy(value))
                return {'stale_node_result':{'caller_node':'planner','caller_invocation_id':'synthetic-old-invocation',
                    'changed_preconditions':['task_revision']}} if len(calls)==1 else {}
            adapter=TypedPlanner(data_path=str(case.root),source=source)
            first=await adapter.run({'task_id':task})
            second=await adapter.run({'task_id':task,**first})
            assert first['planner_outcome']['type']=='stale_result'
            assert second['planner_outcome']['type']=='plan_review_ready'
            assert second.get('stale_node_result',{})=={}
            assert 'stale_node_result' not in calls[1]
        save('FIX_LIFE_'+mode,{'scope':scope,'business_assertions':'passed','provider':'fixed','science':False})
        return {'scope':scope,'business_assertions':'passed'}
    return check


@pytest.mark.parametrize('mode',CASES)
def test_nine_legacy_business_controls_with_real_candidate_scope(mode):
    result=asyncio.run(scenario([c_fix()], 'FIX_LIFE_'+mode,bounded_rejection=False,after_plan=acceptance(mode)))
    assert result['after_plan']['business_assertions']=='passed'

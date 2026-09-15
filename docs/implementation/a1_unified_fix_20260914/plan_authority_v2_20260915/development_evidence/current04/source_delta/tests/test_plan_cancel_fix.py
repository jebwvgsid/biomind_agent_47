"""Deterministic barriers around production claim/await/atomic admission; no sleeps."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import patch
import pytest
from tests.plan_resource_fix_support import scenario, save
from tests.test_plan_resource_fix import c_fix
from tests.test_plan_stop_fix import bad


def install(case, item):
    case.patches.append(item);item.start()


def barrier_setup(boundary):
    async def setup(case, task, runner, counts):
        import agent_core.plan_tracker as tracker
        import agent_core.planner_recovery as recovery
        from agent_core.state_contract_store import StateContractStore
        facts={'barrier':boundary,'cancel_count':0,'plan_object_writes':[]}
        case.cancel_facts=facts
        original_put=StateContractStore.put_object
        def put(store, kind, value, *a, **kw):
            if kind in {'plan_snapshot_v121','plan_approval_record_v121'}:
                facts['plan_object_writes'].append(kind)
            return original_put(store,kind,value,*a,**kw)
        install(case,patch.object(StateContractStore,'put_object',new=put))
        def cancel():
            facts['cancel_count']+=1
            receipt=case.agent._commit_user_cancellation(task,source='fixed_barrier_'+boundary,reason='Explicit synthetic user cancellation')
            facts['receipt']=receipt
        if boundary=='pre_claim':
            original=recovery.claim_planner_call
            def claim(*a,**kw):
                cancel();return original(*a,**kw)
            install(case,patch.object(recovery,'claim_planner_call',new=claim))
        elif boundary=='pre_dispatch':
            original=case.agent._planner_progress
            def progress(tid,event,payload):
                original(tid,event,payload)
                if event=='candidate_started':cancel()
            case.agent._planner_progress=progress
        elif boundary in {'atomic','pre_claim_lock'}:
            original=tracker._atomic_write_plan_state
            def atomic(path,state,**kw):
                if not facts['cancel_count'] and ((boundary=='atomic' and (state.get('allocation_intent') or state.get('plan_commit_requested'))) or (boundary=='pre_claim_lock' and kw.get('require_planner_active'))):
                    # Cancellation commits before the real admission acquires its
                    # lock, after all earlier response and semantic gates passed.
                    cancel()
                return original(path,state,**kw)
            install(case,patch.object(tracker,'_atomic_write_plan_state',new=atomic))
    return setup


async def cancellation_facts(case,task,state,runner):
    from agent_core.plan_tracker import load_plan_state
    from agent_core.planner_loop import plan_review_submission_readiness, plan_review_acceptance_readiness
    loaded=load_plan_state(case.root,task)
    assert loaded['task_state']['status']=='cancelled'
    assert not loaded['steps'] and not loaded.get('current_approval')
    assert not plan_review_submission_readiness(loaded)['ready']
    assert not plan_review_acceptance_readiness(loaded)['ready']
    facts=getattr(case,'cancel_facts',{})
    assert not facts.get('plan_object_writes')
    from agent_core.workflow.early_nodes import TypedPlanner
    update=await TypedPlanner(data_path=str(case.root),source=case.agent._schema2_early_sources['planner']).run({**runner,'task_mode':'managed'})
    assert update['planner_outcome']['type']=='terminal'
    assert update['planner_outcome']['payload']['status']=='cancelled'
    assert load_plan_state(case.root,task)['task_state']['status']=='cancelled'
    facts['reload_dispatches']=0
    return facts


@pytest.mark.parametrize('boundary,requests,charged',[('pre_claim',0,0),('pre_claim_lock',0,0),('pre_dispatch',0,1),('atomic',1,1)])
def test_cancellation_barriers_prevent_admission(boundary,requests,charged):
    result=asyncio.run(scenario([c_fix(),c_fix()], 'FIX_cancel_'+boundary,bounded_rejection=False,
        setup=barrier_setup(boundary),after_plan=cancellation_facts))
    assert result['counts']['planner']==requests
    assert not result['external_stop']
    s=result['final_states'][-1]
    assert int(s['task_state']['planning_epoch'].get('planner_calls_total') or 0)==charged
    assert next(e['planner']['planner_outcome'] for e in result['events'] if 'planner' in e)['type']=='terminal'
    assert next(e['planner']['planner_outcome'] for e in result['events'] if 'planner' in e)['payload']['status']=='cancelled'
    assert result['after_plan']['cancel_count']==1
    if boundary=='atomic':
        assert any(c['phase']=='validate_candidate' for c in result['validator_calls'])
        assert s['task_state']['planner_discarded_response']['boundary']=='atomic_admission'


@pytest.mark.parametrize('response',['valid','invalid','stop'])
def test_post_await_cancel_has_priority_over_any_response(response):
    from agent_core.plan_contract import planner_stop_wire_example
    value=c_fix() if response=='valid' else bad('B') if response=='invalid' else planner_stop_wire_example()
    result=asyncio.run(scenario([value,c_fix()], 'FIX_cancel_late_'+response,bounded_rejection=False,late_cancel=True,after_plan=cancellation_facts))
    s=result['final_states'][-1]
    assert result['counts']['planner']==1 and not result['validator_calls']
    assert s['task_state']['planner_discarded_response']['boundary']=='post_await'
    assert not s['task_state'].get('planning_failure')
    assert next(e['planner']['planner_outcome'] for e in result['events'] if 'planner' in e)['payload']['status']=='cancelled'


async def harmless_generation(case,task,counts,index):
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    current=load_plan_state(case.root,task)
    current['task_state']['synthetic_nonsemantic_observer_note']='No TaskSpec, Plan, Skill, terminal or user revision changed.'
    save_plan_state(case.root,task,current)


def test_nonsemantic_generation_still_accepts_valid_plan():
    result=asyncio.run(scenario([c_fix()], 'FIX_nonsemantic_generation',bounded_rejection=False,before_dispatch=harmless_generation))
    s=result['final_states'][-1]
    assert result['counts']['planner']==1 and s['current_approval']['status']=='pending'
    assert s['task_state']['synthetic_nonsemantic_observer_note']


async def admission_then_cancel(case,task,state,runner):
    from agent_core.plan_tracker import load_plan_state, approve_plan_state, save_plan_state
    from agent_core.planner_loop import plan_review_submission_readiness,plan_review_acceptance_readiness
    # Approval is a separate explicit synthetic test action using the genuine
    # candidate scope; no command or scientific capability is executed.
    approved=approve_plan_state(state,state['current_task']['task_revision'],review_event_id='synthetic-review-before-cancel')
    save_plan_state(case.root,task,approved)
    before=load_plan_state(case.root,task)
    case.agent._commit_user_cancellation(task,source='after_admission',reason='Synthetic user cancel after accepted history')
    after=load_plan_state(case.root,task)
    assert after['current_plan']==before['current_plan']
    assert after['plan_snapshot_ref']==before['plan_snapshot_ref']
    assert after['task_state']['status']=='cancelled'
    assert not plan_review_submission_readiness(after)['ready'] and not plan_review_acceptance_readiness(after)['ready']
    from agent_core.workflow.execution import TypedGenerate
    requests=[]
    async def forbidden(*a,**kw):requests.append(kw);raise AssertionError('Cancelled task must not dispatch Generate provider')
    case.agent._ainvoke_node_llm=forbidden
    result=await TypedGenerate(generator=case.agent._schema2_runtime_sources['generate'],data_path=str(case.root)).run({**runner,'task_mode':'managed'})
    assert not requests
    assert result['generate_outcome']['type']!='action_ready'
    save('FIX_admitted_then_cancelled',{'before':before,'after':after,'generate':result})
    return {'history_preserved':True,'generate_provider_calls':0}


def test_admission_first_preserves_history_and_closes_execution():
    result=asyncio.run(scenario([c_fix()], 'FIX_admission_first',bounded_rejection=False,after_plan=admission_then_cancel))
    assert result['after_plan']['generate_provider_calls']==0

"""Real Planner parser/claims/feedback/routing; fixed provider, no science."""
import asyncio
from copy import deepcopy
import json
import pytest
from langchain_core.messages import AIMessage
from tests.plan_resource_fix_support import scenario, save
from tests.test_plan_resource_fix import c_fix, AUDIT


def bad(fault):
    c = c_fix()
    workspace = next(r for r in c['proposed_artifacts'] if r['local_ref']=='plan.snpeff_data_dir')
    if fault=='A': workspace['resource_binding']={'resource_ref':'artifact.snpeff_data_dir'}
    elif fault=='B': workspace.pop('role_associations')
    elif fault=='evidence': c['steps'][2]['workspace_mutations'][0]['evidence_contract']['postconditions']=[]
    elif fault=='output': c['steps'][-1]['outputs'].remove('output.report')
    return c


def terminal(result, code, calls):
    assert not result['external_stop']
    assert result['counts']['planner']==calls
    state=result['final_states'][-1]
    failure=state['task_state']['planning_failure']
    assert failure['status']=='terminal', failure
    assert failure['terminal_diagnostic']['error_code']==code, failure
    assert not state['steps'] and not state.get('current_approval')
    assert state['task_state']['planning_epoch']['planner_calls_total']==calls
    assert any(e.get('planner',{}).get('planner_outcome',{}).get('type')=='terminal' for e in result['events']), result['events'][-1]
    return failure


def test_active_model_stop_with_budget_remaining():
    from agent_core.plan_contract import planner_stop_wire_example
    result=asyncio.run(scenario([bad('B'), planner_stop_wire_example(), c_fix()], 'FIX_model_stop', bounded_rejection=False))
    f=terminal(result,'PLANNER_MODEL_STOPPED',2)
    assert f['terminal_diagnostic']['remaining_requests']==2
    assert f['terminal_diagnostic']['model_diagnostic']['verified'] is False
    assert f['actual_issues'][0]['code']=='PLAN_RESOURCE_ROLE_ASSOCIATION_MISSING'
    assert f['actual_issues'][0]['not_evaluated']==['step_effects','dataflow','required_workspace_coverage']
    text=json.dumps(result['requests'][1],ensure_ascii=False)
    assert 'STOP_PLANNING' in text and 'attempted_changes' in text and 'remaining_requests' in text


def test_first_response_active_stop():
    from agent_core.plan_contract import planner_stop_wire_example
    result=asyncio.run(scenario([planner_stop_wire_example(), c_fix()], 'FIX_first_stop', bounded_rejection=False))
    terminal(result,'PLANNER_MODEL_STOPPED',1)


def test_actual_a_b_a_stops_before_fourth_dispatch():
    result=asyncio.run(scenario([bad('A'),bad('B'),bad('A'),c_fix()], 'FIX_ABA',bounded_rejection=False))
    f=terminal(result,'PLANNER_REPEATED_FAILURE',3)
    assert f['cycle_detected']
    assert len(result['states'])==3
    assert len({s['task_state']['planning_failure']['candidate_correction_signature'] for s in result['states']})==2


def test_compiled_candidate_uuid_does_not_look_like_progress():
    a=bad('evidence'); b=deepcopy(a);b['steps'][2]['title']='presentation-only edit'
    result=asyncio.run(scenario([a,b,c_fix()], 'FIX_UUID',bounded_rejection=False))
    terminal(result,'PLANNER_REPEATED_FAILURE',2)
    f=[s['task_state']['planning_failure'] for s in result['states']]
    assert f[0]['candidate_correction_signature']==f[1]['candidate_correction_signature']
    assert f[0]['issue_fingerprint']==f[1]['issue_fingerprint']


def test_correction_succeeds_and_fourth_valid_is_accepted():
    result=asyncio.run(scenario([bad('A'),bad('B'),bad('evidence'),c_fix()], 'FIX_fourth_valid',bounded_rejection=False))
    assert result['counts']['planner']==4 and not result['external_stop']
    s=result['final_states'][-1]
    assert s['current_approval']['status']=='pending'
    assert s['task_state']['planning_failure']['status']=='resolved'
    assert len(s['steps'])==6
    assert len(result['requests'])==4
    # Original model responses, actual complete validator facts, and next feedback retained by real store.
    assert all('PLANNER_CANDIDATE_REJECTED' in json.dumps(req) for req in result['requests'][1:])


def test_fourth_invalid_stops_and_never_dispatches_fifth():
    result=asyncio.run(scenario([bad('A'),bad('B'),bad('evidence'),bad('output'),c_fix()], 'FIX_budget',bounded_rejection=False))
    terminal(result,'PLANNING_EPOCH_EXHAUSTED',4)


@pytest.mark.parametrize('fault',['mixed','mixed_skill','missing','text','fragment','truncated'])
def test_stop_invalid_never_routes_by_keyword(fault):
    from agent_core.plan_contract import planner_stop_wire_example
    value=planner_stop_wire_example()
    if fault=='mixed': value.update(steps=c_fix()['steps'])
    elif fault=='mixed_skill': value['decision']='REQUEST_SKILL_CONTEXT'
    elif fault=='missing': value['diagnostic'].pop('uncertainties')
    elif fault=='text': value=AIMessage(content='<finish> no progress STOP_PLANNING')
    elif fault=='fragment': value=AIMessage(content='{"outcome_type":"STOP_PLANNING",')
    elif fault=='truncated': value=AIMessage(content=json.dumps(value),response_metadata={'finish_reason':'length'})
    result=asyncio.run(scenario([value,c_fix()], 'FIX_invalid_stop_'+fault,bounded_rejection=False))
    assert result['counts']['planner']==2 and not result['external_stop']
    assert result['final_states'][-1]['current_approval']['status']=='pending'
    assert result['states'][0]['task_state']['planning_failure']['status']=='unresolved'


def test_comparison_preserves_paths_parameters_roles_and_trusted_facts():
    from agent_core.plan_tracker import _candidate_correction_signature, record_planning_failure
    c=c_fix(); issues=[{'code':'X','field_path':'$.steps'}]
    original=_candidate_correction_signature(c,issues)
    for path in ['candidate:USER:plan.literal','/tmp/中文 path/new']:
        changed=deepcopy(c);changed['steps'][1]['parameters']={'path':path}
        assert _candidate_correction_signature(changed,issues)!=original
    changed=deepcopy(c);changed['proposed_artifacts'][1]['role_associations']=[]
    assert _candidate_correction_signature(changed,issues)!=original
    state={'task_id':'synthetic','task_state':{'planning_epoch':{'planning_epoch_id':'e','status':'active','planner_response_protocol_version':3}}}
    def reject(s, req): return record_planning_failure(s,validation_issues=issues,planning_epoch_id='e',task_spec_revision_value=1,validation_source='test',candidate=c,requirements=req)
    first=reject(state,[{'skill_digest':'one'}]);second=reject(first,[{'skill_digest':'two'}])
    assert not second['task_state']['planning_failure']['cycle_detected']


async def closed_reload(case, task, state, runner):
    from agent_core.plan_tracker import load_plan_state, canonical_task_spec_for_state
    from agent_core.planner_recovery import claim_planner_call, complete_planner_claim, PlannerCallError
    from agent_core.workflow.early_nodes import TypedPlanner
    loaded=load_plan_state(case.root,task)
    epoch=loaded['task_state']['planning_epoch'];claim=epoch['call_claim']
    before=deepcopy(loaded)
    complete_planner_claim(loaded,claim,claim['response_integrity'],outcome=claim['outcome'])
    complete_planner_claim(loaded,claim,claim['response_integrity'],outcome=claim['outcome'])
    assert loaded==before
    with pytest.raises(PlannerCallError):
        claim_planner_call(case.root,task,loaded,claim['scope'],claim['output_policy'])
    invocation={**runner,'task_mode':'managed','task_revision':loaded['current_task']['task_revision'],
        'task_spec':canonical_task_spec_for_state(loaded),'selected_skills':['genomics/snpeff-annotation']}
    result=await TypedPlanner(data_path=str(case.root),source=case.agent._schema2_early_sources['planner']).run(invocation)
    assert result['planner_outcome']['type']=='terminal'
    after=load_plan_state(case.root,task)
    assert after['task_state']['planning_epoch']['planner_calls_total']==epoch['planner_calls_total']
    assert after['task_state']['planning_epoch']['planning_epoch_id']==epoch['planning_epoch_id']
    save('FIX_stop_reload',{'before':before,'after':after,'update':result})
    return {'replayed_provider_requests':0,'calls_total':epoch['planner_calls_total']}


def test_closed_epoch_reload_and_claim_replay_are_not_new_calls():
    from agent_core.plan_contract import planner_stop_wire_example
    result=asyncio.run(scenario([planner_stop_wire_example(),c_fix()], 'FIX_closed_reload',bounded_rejection=False,after_plan=closed_reload))
    assert result['after_plan']['replayed_provider_requests']==0

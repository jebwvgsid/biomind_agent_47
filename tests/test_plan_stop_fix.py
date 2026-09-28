"""Real Planner parser/claims/feedback/routing; fixed provider, no science."""
import asyncio
from copy import deepcopy
import json
import pytest
from langchain_core.messages import AIMessage
from tests.plan_resource_fix_support import save
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





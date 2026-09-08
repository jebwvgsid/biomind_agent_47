"""New synthetic Plan authored through production allocation/compilation/store."""
from copy import deepcopy
import json
from agent_core.plan_tracker import (load_plan_state, build_plan_state, bind_plan_to_task_revision,
    canonical_task_spec_for_state, save_plan_state, approve_plan_state, load_approved_plan_snapshot)
from agent_core.plan_bindings import compile_plan_resource_bindings
from agent_core.plan_contract import prepare_allocation_intent, validate_planner_candidate_deterministically
from agent_core.step_effects import compile_plan_step_effects, compile_plan_dataflow
from tests.test_plan_dataflow_p1 import _resource


def formalize(fixture, graph_state):
    task_id=graph_state['task_id']
    prior=load_plan_state(fixture.root,task_id)
    spec=canonical_task_spec_for_state(prior)
    raw=deepcopy(fixture.plan_fixture['candidate'])
    old_workspace=next(k for k in prior['plan_artifacts'] if k.startswith('plan-artifact-'))
    mapping={old_workspace:'plan.snpeff_data_dir','annotated_vcf':'plan.annotated_vcf','ann_summary':'plan.ann_summary'}
    handles={}; origins={}; authoritative={}
    for direction,field in [('input','inputs'),('output','output_contracts')]:
        for row in spec.get(field,[]):
            identity=str(row.get('artifact_id') or row.get(direction+'_id'))
            semantic=row.get('semantic_handle') or direction+'.'+row['role']
            handles[semantic]=identity;handles[identity]=identity;mapping[semantic]=identity
            origins[identity]='task_input' if direction=='input' else 'not_yet_available'
            authoritative[semantic]={'initially_available':direction=='input','resource_aliases':['artifact.'+row['role']]}
            authoritative[identity]=authoritative[semantic]
    def rewrite(value):
        if isinstance(value,str):return mapping.get(value,value)
        if isinstance(value,list):return [rewrite(v) for v in value]
        if isinstance(value,dict):return {k:rewrite(v) for k,v in value.items()}
        return value
    raw=rewrite(raw)
    # New authoring follows the current fixed Skill: workspace mutation is an
    # effect, never an immutable artifact output.
    for step in raw['steps']:
        step['outputs']=[ref for ref in step.get('outputs',[]) if ref!='plan.snpeff_data_dir']
    raw['steps'][1]['outputs'] = ['plan.snpeff_config']
    for step in raw['steps'][2:4]:
        step['inputs'].append('plan.snpeff_config')
    raw['proposed_artifacts']=[_resource('plan.snpeff_config',canonical_resource_id='artifact.snpeff_config'),_resource('plan.snpeff_data_dir',canonical_resource_id='artifact.snpeff_data_dir',resource_kind='mutable_workspace'),
        _resource('plan.annotated_vcf',canonical_resource_id='artifact.annotated_vcf'),
        _resource('plan.ann_summary',canonical_resource_id='artifact.ann_summary')]
    _, usage, issues=validate_planner_candidate_deterministically(raw,candidate_scope_id='formal-fixture',
        authoritative_handles=handles,availability_origins=origins,authoritative_bindings=authoritative,validate_dataflow=False)
    assert not issues,issues
    state=build_plan_state(task_id,json.dumps(raw),strict=True)
    state['artifact_usage']=usage
    state['state_generation']=prior['state_generation']
    state['current_task']=deepcopy(prior['current_task'])
    state['task_state']=deepcopy(prior['task_state'])
    for key in ['conversation_evidence','audit_index','selected_skill_ids']:
        state[key]=deepcopy(prior.get(key))
    state=fixture.manager.bind_plan_execution_capability_contracts(state,prior['selected_skill_ids'])
    bind_plan_to_task_revision(state,spec['revision'])
    registry=fixture.manager.get_execution_capability_registry(prior['selected_skill_ids'])
    bindings=compile_plan_resource_bindings(raw,candidate_scope_id='formal-fixture',authoritative_handles=handles,
        availability_origins=origins,authoritative_bindings=authoritative)
    state['plan_resource_bindings']=bindings
    state,receipt,issues=compile_plan_step_effects(state,registry,resource_bindings=bindings,allow_legacy_untyped=False)
    assert not issues,issues
    state['plan_compilation_receipt']=receipt
    state,flow,issues=compile_plan_dataflow(state,registry,resource_bindings=bindings,compile_effect_contracts=False,allow_legacy_untyped=False)
    assert not issues,issues
    state['plan_dataflow_receipt']=flow
    state['allocation_intent']=prepare_allocation_intent(raw,candidate_scope_id='formal-fixture',planning_epoch_id='formal-fixture-epoch')
    state['plan_commit_requested']=True
    save_plan_state(fixture.root,task_id,state,step_effect_capability_registry=registry)
    state=load_plan_state(fixture.root,task_id)
    approve_plan_state(state,spec['revision'],approval_source='offline-new-formal-plan-review')
    save_plan_state(fixture.root,task_id,state,step_effect_capability_registry=registry)
    loaded=load_plan_state(fixture.root,task_id)
    snapshot=load_approved_plan_snapshot(fixture.root,task_id,state=loaded)
    assert snapshot['plan_local_artifacts'] and snapshot['resolved_artifact_refs']
    return {**graph_state,'current_step_id':loaded['current_step_id'],'current_plan':json.dumps({'steps':loaded['steps']}),
        'plan_id':loaded['plan_id'],'plan_version':loaded['plan_version']}

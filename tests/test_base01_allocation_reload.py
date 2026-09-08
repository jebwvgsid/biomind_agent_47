"""BASE-01: real allocation, compiler, atomic store, approval, reload and CAS.

Only the declared capability and proposed Plan are synthetic input. No store,
canonicalizer, compiler, approval or snapshot method is replaced.
"""
from copy import deepcopy
import json
import pytest
from agent_core.plan_bindings import compile_plan_resource_bindings
from agent_core.plan_contract import prepare_allocation_intent, StateCASConflict
from agent_core.plan_tracker import (build_plan_state, save_plan_state, load_plan_state,
    bind_plan_to_task_revision, approve_plan_state, plan_approval_is_current,
    load_approved_plan_snapshot, semantic_plan_hash)
from agent_core.task_spec import create_task_spec
from agent_core.step_effects import compile_plan_step_effects, compile_plan_dataflow, validate_plan_compilation_receipt, validate_plan_dataflow_receipt
from agent_core.state_contract_store import StateContractStore
from test_plan_dataflow_p1 import _plan, _step, _resource, _mutation


def proposed_state(root):
    task_id='base01-real-allocation'
    mutation=_mutation('plan.workspace',mutation='prepare',requires_available=False,establishes_state='prepared')
    candidate=_plan([_step('prepare',workspace_mutations=[mutation],capability_id='fixture.prepare',operation='prepare')],
                    [_resource('plan.workspace',canonical_resource_id='artifact.workspace',resource_kind='mutable_workspace')])
    task=create_task_spec('Prepare one approved workspace.',task_id=task_id,base_dir=str(root),project_root=str(root))
    state=build_plan_state(task_id,json.dumps(candidate),strict=True)
    state['current_task']={'task_spec':task}
    bind_plan_to_task_revision(state,task['revision'])
    state['selected_skill_ids']=['fixture/skill']
    registry={'fixture.prepare':{'schema_version':1,'capability_id':'fixture.prepare','selected_skill_id':'fixture/skill','operation':'prepare',
        'required_runtime_binding_ids':[],'optional_runtime_binding_ids':[],'required_artifact_binding_ids':[],
        'produces_artifacts':[],'produces_bindings':[],'produces_observations':[],
        'workspace_mutations':[deepcopy(mutation)],'explicit_side_effects':[]}}
    bindings=compile_plan_resource_bindings(candidate,candidate_scope_id='base01-candidate',authoritative_handles={})
    state['plan_resource_bindings']=bindings
    state,receipt,issues=compile_plan_step_effects(state,registry,allow_legacy_untyped=False,resource_bindings=bindings)
    assert not issues,issues
    state['plan_compilation_receipt']=receipt
    state,flow,issues=compile_plan_dataflow(state,registry,resource_bindings=bindings,compile_effect_contracts=False,allow_legacy_untyped=False)
    assert not issues,issues
    state['plan_dataflow_receipt']=flow
    state['allocation_intent']=prepare_allocation_intent(candidate,candidate_scope_id='base01-candidate',planning_epoch_id='base01-epoch')
    state['plan_commit_requested']=True
    return state,registry


def test_real_atomic_allocation_snapshot_approval_reload_and_cas(tmp_path):
    state,registry=proposed_state(tmp_path)
    task_id=state['task_id']
    save_plan_state(tmp_path,task_id,state,allow_canonical_task_write=True,step_effect_capability_registry=registry)
    loaded=load_plan_state(tmp_path,task_id)
    allocated=loaded['artifact_allocation_mapping']['local_to_authoritative']['plan.workspace']
    assert allocated.startswith('plan-artifact-')
    assert loaded['plan_artifacts'][allocated]['canonical_resource_id']=='artifact.workspace'
    assert loaded['plan_resource_bindings'][allocated]['canonical_resource_id']=='artifact.workspace'
    assert 'allocation_intent' not in loaded
    assert validate_plan_compilation_receipt(loaded['plan_compilation_receipt'],loaded,registry,require_complete=True)['valid']
    assert validate_plan_dataflow_receipt(loaded['plan_dataflow_receipt'],loaded,loaded['plan_resource_bindings'])['valid']
    snapshot_ref=loaded['plan_snapshot_ref']
    assert snapshot_ref
    approve_plan_state(loaded,1,approval_source='offline-human-review')
    # The existing durable approval remains pending until the real CAS save.
    assert not plan_approval_is_current(loaded, 1)
    save_plan_state(tmp_path,task_id,loaded,step_effect_capability_registry=registry)
    reloaded=load_plan_state(tmp_path,task_id)
    assert plan_approval_is_current(reloaded, 1)
    assert reloaded['plan_snapshot_ref']==snapshot_ref
    snapshot=load_approved_plan_snapshot(tmp_path,task_id,state=reloaded)
    assert snapshot['plan_local_artifacts'][allocated]==reloaded['plan_artifacts'][allocated]
    assert reloaded['plan_compilation_receipt']==loaded['plan_compilation_receipt']
    assert reloaded['compiled_dataflow']==loaded['compiled_dataflow']
    assert semantic_plan_hash(reloaded)==semantic_plan_hash(loaded)
    # Real competing CAS writers, including a fresh loader, cannot both commit.
    stale=deepcopy(reloaded)
    reloaded['task_state']['base01_marker']='first'
    save_plan_state(tmp_path,task_id,reloaded)
    stale['task_state']['base01_marker']='second'
    with pytest.raises(StateCASConflict):save_plan_state(tmp_path,task_id,stale)
    assert load_plan_state(tmp_path,task_id)['task_state']['base01_marker']=='first'

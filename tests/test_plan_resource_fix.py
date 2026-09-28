"""Positive FIX acceptance on new isolated tasks; historical witnesses stay intact."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import os
import shutil
import tempfile
import pytest

from tests.plan_resource_fix_support import save

AUDIT = Path(__file__).resolve().parents[1]/'docs/audits/session_1789369423_plan_resource_cycle'

def c_fix():
    candidate = json.loads((AUDIT/'candidate_C_rejected.json').read_text())
    workspace = next(row for row in candidate['proposed_artifacts'] if row['local_ref'] == 'plan.snpeff_data_dir')
    workspace['role_associations'] = [
        {'step_id': name, 'capability_id': 'genomics.snpeff_annotation.'+name, 'role': 'artifact.snpeff_data_dir'}
        for name in ['prepare_database', 'build_database', 'annotate_variants']]
    return candidate





async def freeze_and_consume(case, task, state, runner):
    from agent_core.plan_tracker import approve_plan_state, save_plan_state, load_plan_state, load_approved_plan_snapshot
    from agent_core.workspace_attestation import project_current_action_workspace_bindings, resolve_artifact_backed_binding_value, validate_workspace_action_admission
    from agent_core.plan_bindings import public_binding_requirements, normalize_public_action_bindings, validate_operation_binding_admission
    registry = case.agent.skill_manager.get_execution_capability_registry(['genomics/snpeff-annotation'])
    with pytest.raises(ValueError, match='NOT_APPROVED'):
        load_approved_plan_snapshot(case.root, task, state=state)
    approved = approve_plan_state(state, int(state['current_task']['task_revision']), review_event_id='synthetic-user-plan-approval')
    save_plan_state(case.root, task, approved)
    loaded = load_plan_state(case.root, task)
    snapshot = load_approved_plan_snapshot(case.root, task, state=loaded)
    assert snapshot['resource_role_protocol_version'] == 1
    resource = loaded['plan_resource_bindings']['plan.snpeff_data_dir']
    assert not Path(loaded['plan_artifacts'][resource['canonical_resource_id']]['canonical_path']).exists()
    catalogs = {}
    for step in snapshot['steps'][1:4]:
        cap = registry[step['execution_capability_id']]
        frozen = deepcopy(step['generate_binding_input_contract']); frozen['operation'] = cap['operation']
        contract = frozen['binding_contract']; rule = contract['operations'][cap['operation']]
        catalog = project_current_action_workspace_bindings(plan_state=loaded, approved_plan_snapshot=snapshot,
            current_step=step, capability_contract=cap, binding_rule=rule, derived_artifact_layout=contract.get('derived_artifact_layout'))
        assert len(catalog) == 1
        row = catalog[0]
        assert row['canonical_resource_id'] == resource['canonical_resource_id']
        assert row['access_scope'] == ('read_only' if step['step_id'] == 'annotate_variants' else 'read_write')
        value = {key: row[key] for key in ['canonical_resource_id','resource_kind','binding_revision','plan_handle','binding_id']}
        assert resolve_artifact_backed_binding_value(value, value_type='artifact_ref', workspace_bindings=catalog) == row['formal_concrete_path']
        for key, bad in [('canonical_resource_id','candidate:foreign:plan.snpeff_data_dir'), ('binding_revision',99), ('plan_handle','foreign')]:
            with pytest.raises(Exception):
                resolve_artifact_backed_binding_value({**value,key:bad}, value_type='artifact_ref', workspace_bindings=catalog)
        if step['step_id'] == 'prepare_database':
            public = {'type':'execute','operation':cap['operation'],'language':'bash','code':'true',
                'produced_bindings':{'genome_version':{'type':'string','value':'synthetic-genome'},
                    'snpeff_db':{'type':'artifact_ref','value':{'handle':row['plan_handle']}}}}
            scope = {'task_id':task,'task_revision':loaded['current_task']['task_revision'],'plan_id':loaded['plan_id'],
                'plan_version':loaded['plan_version'],'plan_revision':loaded['plan_revision'],'step_id':step['step_id']}
            normalized, receipt = normalize_public_action_bindings(public, frozen=frozen, scope=scope, workspace_bindings=catalog)
            admission = validate_operation_binding_admission(normalized, capability_contract=cap, capability_registry=registry, binding_contract=contract)
            assert admission.status == 'passed', admission
            workspace_admission = validate_workspace_action_admission(normalized, capability_contract=cap, workspace_bindings=catalog)
            assert workspace_admission.status == 'passed', workspace_admission
            assert receipt['scope'] == scope
        catalogs[step['step_id']] = {'catalog':catalog,'requirements':public_binding_requirements(frozen, operation=cap['operation'], workspace_bindings=catalog)}
    keep = Path(tempfile.mkdtemp(prefix='reload-approved-',dir=os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS']))
    shutil.copytree(case.root/'task_outputs'/task, keep/'task_outputs'/task)
    save('FIX_reload_pointer', {'root':str(keep),'task':task,'identity':resource['canonical_resource_id'],'snapshot_ref':loaded['plan_snapshot_ref']})
    return {'catalogs':catalogs,'snapshot_ref':loaded['plan_snapshot_ref'],'approval':loaded['current_approval'],'scientific_execution':False}




def test_independent_process_frozen_reload():
    from agent_core.plan_tracker import load_plan_state, load_approved_plan_snapshot
    pointer = json.loads((Path(os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS'])/'FIX_reload_pointer.json').read_text())
    state = load_plan_state(pointer['root'], pointer['task'])
    snapshot = load_approved_plan_snapshot(pointer['root'], pointer['task'], state=state)
    assert snapshot['plan_resource_bindings']['plan.snpeff_data_dir']['canonical_resource_id'] == pointer['identity']
    assert state['plan_snapshot_ref'] == pointer['snapshot_ref']
    save('FIX_independent_reload', {'pid':os.getpid(),'snapshot_ref':state['plan_snapshot_ref'],'identity':pointer['identity']})






def dual_workspace_candidate():
    candidate = c_fix()
    original_steps = deepcopy(candidate['steps'][1:4])
    refs = {'plan.snpeff_config':'plan.second_config', 'plan.snpeff_data_dir':'plan.second_workspace',
        'output.annotated_vcf':'plan.second_vcf'}
    # Local refs follow the public ASCII grammar; physical paths remain Unicode.
    for declaration in deepcopy(candidate['proposed_artifacts']):
        declaration['local_ref'] = refs[declaration['local_ref']]
        declaration['artifact_contract'].pop('path', None)
        declaration['artifact_contract']['path_policy'] = 'model_selectable'
        for association in declaration.get('role_associations', []): association['step_id'] += '_second'
        candidate['proposed_artifacts'].append(declaration)
    candidate['proposed_artifacts'].append({'local_ref':'plan.second_vcf',
        'semantic_view':{'role':'second_annotation','format':'vcf','description':'Synthetic second group output'},
        'artifact_contract':{'object_kind':'file','lifecycle':'plan_local','resource_kind':'immutable_artifact',
            'required':True,'path_policy':'model_selectable','materialization_policy_id':'managed_intermediate_v1'}})
    names = {step['step_id'] for step in original_steps}
    for step in original_steps:
        step['step_id'] += '_second'
        for field in ['inputs','outputs']: step[field] = [refs.get(ref, ref) for ref in step[field]]
        step['dependencies'] = [name+'_second' if name in names else name for name in step['dependencies']]
        for mutation in step.get('workspace_mutations', []): mutation['workspace_ref'] = refs[mutation['workspace_ref']]
    candidate['steps'].extend(original_steps)
    return candidate


async def dual_projection(case, task, state, runner):
    from agent_core.plan_tracker import approve_plan_state, save_plan_state, load_plan_state, load_approved_plan_snapshot
    from agent_core.workspace_attestation import project_current_action_workspace_bindings, resolve_artifact_backed_binding_value, WorkspaceAttestationError
    state = approve_plan_state(state, state['current_task']['task_revision'], review_event_id='synthetic-dual-approval')
    save_plan_state(case.root, task, state)
    state = load_plan_state(case.root, task)
    snapshot = load_approved_plan_snapshot(case.root, task, state=state)
    registry = case.agent.skill_manager.get_execution_capability_registry(['genomics/snpeff-annotation'])
    catalogs = {}
    for step in snapshot['steps']:
        operation = registry[step['execution_capability_id']]['operation']
        if operation not in ['prepare_database','build_database','annotate_variants']: continue
        contract = step['generate_binding_input_contract']['binding_contract']
        catalogs[step['step_id']] = project_current_action_workspace_bindings(plan_state=state, approved_plan_snapshot=snapshot,
            current_step=step, capability_contract=registry[step['execution_capability_id']], binding_rule=contract['operations'][operation])
    first = catalogs['prepare_database'][0]['canonical_resource_id']
    second = catalogs['prepare_database_second'][0]['canonical_resource_id']
    assert first != second
    for name, rows in catalogs.items():
        assert len(rows) == 1
        assert rows[0]['canonical_resource_id'] == (second if name.endswith('_second') else first)
    wrong = {key:catalogs['prepare_database'][0][key] for key in ['canonical_resource_id','resource_kind','binding_revision']}
    with pytest.raises(WorkspaceAttestationError):
        resolve_artifact_backed_binding_value(wrong,value_type='artifact_ref',workspace_bindings=catalogs['annotate_variants_second'])
    return {'catalogs':catalogs,'distinct_instances':[first,second],'synthetic_two_groups':True}






def test_role_reader_compatibility():
    from agent_core.plan_contract import assert_reader_compatible, ReaderCompatibilityError
    assert_reader_compatible({'minimum_reader_version':'1.2.1'},reader_version='1.2.1')
    with pytest.raises(ReaderCompatibilityError):
        assert_reader_compatible({'minimum_reader_version':'1.2.2'},reader_version='1.2.1')
    assert_reader_compatible({'minimum_reader_version':'1.2.2'})


def test_existing_formal_resource_retains_explicit_role_associations():
    """Existing-authority reuse control, separate from the NEW-workspace C_FIX."""
    from agent_core.plan_bindings import compile_plan_resource_bindings,resource_bindings_for_step,instantiate_step_binding_contract
    from agent_core.skill_manager import SkillManager
    registry=SkillManager(str(Path(__file__).resolve().parents[1]/'skills')).get_execution_capability_registry(['genomics/snpeff-annotation'])
    c=c_fix();workspace=next(x for x in c['proposed_artifacts'] if x['local_ref']=='plan.snpeff_data_dir')
    workspace['resource_binding']={'resource_ref':'output.explicit_workspace'}
    bindings=compile_plan_resource_bindings(c,candidate_scope_id='synthetic-reuse',
        authoritative_handles={'output.explicit_workspace':'formal-existing-workspace'},
        authoritative_bindings={'output.explicit_workspace':{'metadata':{'resource_kind':'mutable_workspace','contract_revision':'r7'}}},
        capability_registry=registry)
    for step in c['steps'][1:4]:
        row=resource_bindings_for_step(bindings,step)['artifact.snpeff_data_dir']
        assert row['canonical_resource_id']=='formal-existing-workspace'
        assert row['contract_revision']=='r7'
        assert row['role_association_version']==1
    assert 'artifact.snpeff_data_dir' not in bindings

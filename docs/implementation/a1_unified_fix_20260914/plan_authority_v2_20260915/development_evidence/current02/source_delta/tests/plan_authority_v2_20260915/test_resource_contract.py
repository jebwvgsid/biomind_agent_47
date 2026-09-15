"""V3 resource-use contract; execute only in the existing safe export guard."""
from copy import deepcopy

import pytest

from agent_core.plan_bindings import PlanBindingError, compile_plan_resource_bindings, resource_bindings_for_step
from agent_core.plan_contract import structured_plan_candidate_diagnostics, candidate_artifact_ref_keys


CAP = 'fixture.workspace.prepare'
READER = 'fixture.workspace.read'
ROLE = 'artifact.workspace'
REGISTRY = {
    CAP: {'capability_id': CAP, 'selected_skill_id': 'fixture/skill', 'workspace_mutations': [{'workspace_ref': ROLE}]},
    READER: {'capability_id': READER, 'selected_skill_id': 'fixture/skill', 'binding_applicability': {
        'artifact_binding_requirements': {'db': {'canonical_resource_id': ROLE, 'resource_kind': 'mutable_workspace'}}}},
}


def candidate():
    def step(identity, cap, access):
        return {'step_id': identity, 'title': identity, 'action': 'execute', 'purpose': 'analysis',
            'tool': 'fixture/skill', 'skill_ids': ['fixture/skill'], 'execution_capability_id': cap,
            'operation': 'prepare' if access == 'mutate' else 'read',
            'resource_uses': [{'resource_ref': 'plan.workspace', 'access': access, 'role': ROLE}],
            'outputs': [], 'dependencies': [], 'success_criteria': ['real evidence required'],
            'workflow_stage_ids': [], 'batch_id': '', 'risk_level': 'low'}
    first = step('setup-instance', CAP, 'mutate')
    first['workspace_mutations'] = [{'workspace_ref': ROLE, 'mutation': 'prepare',
        'requires_available': False, 'available_after_success': True, 'establishes_state': 'prepared'}]
    second = step('check-instance', READER, 'read')
    second['dependencies'] = ['setup-instance']
    return {'steps': [first, second], 'proposed_artifacts': [{'local_ref': 'plan.workspace',
        'semantic_view': {'role': 'workspace', 'format': 'directory', 'description': 'New isolated resource'},
        'artifact_contract': {'object_kind': 'directory', 'resource_kind': 'mutable_workspace',
            'lifecycle': 'plan_local', 'materialization_policy_id': 'managed_intermediate_v1'}}],
        'validation_checklist': [{'check_id': 'evidence', 'description': 'do not treat plans as success'}],
        'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2', 'resource_contract_version': 3}}


def compile_resources(value):
    return compile_plan_resource_bindings(value, candidate_scope_id='new-scope', authoritative_handles={},
        require_declared_references=True, capability_registry=REGISTRY)


def test_v3_wire_has_one_read_declaration():
    value = candidate()
    assert structured_plan_candidate_diagnostics(value) == []
    rows = compile_resources(value)
    scoped = resource_bindings_for_step(rows, value['steps'][1])
    assert scoped[ROLE]['canonical_resource_id'] == rows['plan.workspace']['canonical_resource_id']
    assert not rows['plan.workspace']['initially_available']
    assert 'inputs' not in value['steps'][1]
    assert 'role_associations' not in value['proposed_artifacts'][0]


@pytest.mark.parametrize('field', ['inputs', 'role_associations'])
def test_v3_explicit_retired_fields_rejected(field):
    value = candidate()
    target = value['steps'][1] if field == 'inputs' else value['proposed_artifacts'][0]
    target[field] = []
    diagnostics = structured_plan_candidate_diagnostics(value)
    assert any(field in row['field_path'] and 'retired' in row['message'].lower() for row in diagnostics), diagnostics


def test_v3_reference_is_checked_at_real_source_field():
    value = candidate()
    value['steps'][1]['resource_uses'][0]['resource_ref'] = 'plan.absent'
    with pytest.raises(Exception) as caught:
        candidate_artifact_ref_keys(value, candidate_scope_id='new-scope', authoritative_handles={})
    assert caught.value.field_path == '$.steps[1].resource_uses[0].resource_ref'


def test_v3_access_cannot_drop_selected_helper_requirement():
    value = candidate()
    value['steps'][1]['resource_uses'][0]['access'] = 'mutate'
    with pytest.raises(PlanBindingError) as caught:
        compile_resources(value)
    assert caught.value.code == 'PLAN_RESOURCE_USE_ACCESS_MISMATCH'


def test_v3_source_changes_approval_semantics_and_rebuilds_index():
    from agent_core.plan_tracker import normalize_plan_step, _semantic_step
    step = candidate()['steps'][1]
    normalized = normalize_plan_step(deepcopy(step))
    assert normalized['inputs'] == ['plan.workspace']
    normalized['inputs'] = ['plan.invented-index']
    assert normalize_plan_step(normalized)['inputs'] == ['plan.workspace']
    original = _semantic_step(step)
    changed = deepcopy(step)
    changed['resource_uses'][0]['role'] = 'artifact.other'
    assert _semantic_step(changed) != original
    assert original['resource_uses'] == step['resource_uses']


def test_legacy_stale_association_is_still_rejected():
    value = candidate()
    value['metadata']['resource_contract_version'] = 2
    for step in value['steps']:
        uses = step.pop('resource_uses')
        step['inputs'] = [use['resource_ref'] for use in uses if use['access'] == 'read']
    value['proposed_artifacts'][0]['role_associations'] = [
        {'step_id': 'deleted-instance', 'capability_id': CAP, 'role': ROLE},
        {'step_id': 'check-instance', 'capability_id': READER, 'role': ROLE}]
    with pytest.raises(PlanBindingError) as caught:
        compile_resources(value)
    assert caught.value.code == 'PLAN_RESOURCE_ROLE_SCOPE_INVALID'

"""V3 resource-use contract; execute only in the existing safe export guard."""
from copy import deepcopy

import pytest

from agent_core.plan_bindings import PlanBindingError, compile_plan_resource_bindings, resource_bindings_for_step
from agent_core.plan_contract import structured_plan_candidate_diagnostics, candidate_artifact_ref_keys, PlannerCandidateError


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
    with pytest.raises(PlannerCandidateError) as caught:
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
    assert caught.value.diagnostic['machine_evidence']['step_lookup_found'] is False


@pytest.mark.parametrize('kind,expected', [('empty_id', 'PLAN_STEP_ID_MISSING'),
    ('duplicate_id', 'PLAN_STEP_ID_DUPLICATE'), ('missing_dependency', 'PLAN_DEPENDENCY_UNKNOWN'),
    ('cycle', 'PLAN_DEPENDENCY_CYCLE')])
def test_identity_and_dependency_guards_remain(kind, expected):
    from agent_core.plan_contract import validate_candidate_graph_and_flow
    value = candidate()
    if kind == 'empty_id': value['steps'][0]['step_id'] = ''
    elif kind == 'duplicate_id': value['steps'][1]['step_id'] = value['steps'][0]['step_id']
    elif kind == 'missing_dependency': value['steps'][1]['dependencies'] = ['deleted-step']
    else: value['steps'][0]['dependencies'] = [value['steps'][1]['step_id']]
    issues = validate_candidate_graph_and_flow(value, ref_keys={}, usage=[], availability_origins={})
    assert expected in {row['code'] for row in issues}, issues


@pytest.mark.parametrize('kind', ['forged_scope', 'foreign_resource'])
def test_authority_cannot_be_added_by_resource_use(kind):
    value = candidate()
    if kind == 'forged_scope': value['steps'][0]['resource_uses'][0]['candidate_scope_id'] = 'foreign-task'
    else: value['steps'][0]['resource_uses'][0]['resource_ref'] = 'candidate:foreign-task:plan.workspace'
    with pytest.raises(PlannerCandidateError):
        candidate_artifact_ref_keys(value, candidate_scope_id='new-scope', authoritative_handles={})


def test_mutate_cannot_promote_an_immutable_resource():
    value = candidate()
    value['proposed_artifacts'][0]['artifact_contract'].update(object_kind='file', resource_kind='immutable_artifact')
    with pytest.raises(PlanBindingError) as caught:
        compile_resources(value)
    assert caught.value.code == 'PLAN_RESOURCE_KIND_INVALID'


def test_read_needs_actual_planned_source_even_without_template():
    from agent_core.step_effects import compile_plan_dataflow
    value = candidate()
    value['steps'] = [value['steps'][1]]
    value['steps'][0]['dependencies'] = []
    value['steps'][0]['resource_uses'][0].pop('role')
    value['proposed_artifacts'][0]['artifact_contract'].update(object_kind='file', resource_kind='immutable_artifact', required=True)
    _, _, issues = compile_plan_dataflow(value, {}, compile_effect_contracts=False)
    assert 'PLAN_ARTIFACT_UNAVAILABLE' in {row['code'] for row in issues}, issues


def output_state(tmp_path, *, exact=False):
    task = {'output_contracts': [
        {'output_id': 'output_dir', 'artifact_id': 'out-root', 'role': 'output_dir', 'artifact_kind': 'directory',
         'canonical_path': str(tmp_path), 'required': True},
        {'output_id': 'report', 'artifact_id': 'out-report', 'semantic_handle': 'output.report', 'role': 'report',
         'artifact_kind': 'file', 'canonical_path': str(tmp_path / 'fixed.txt') if exact else '', 'required': True,
         'root_ref': 'output_dir', 'path_policy': 'managed_exact' if exact else 'planner_materialized'}]}
    state = {'plan_document': {'metadata': {'resource_contract_version': 3}},
             'current_task': {'task_spec': task}, 'task_output_materializations': []}
    return state, task


def test_v3_task_output_necessity_is_not_repeated(tmp_path):
    from agent_core.plan_tracker import normalize_task_output_materializations
    from agent_core.plan_contract import plan_result_contracts
    state, task = output_state(tmp_path)
    state['task_output_materializations'] = [{'output_ref': 'out-report', 'path': 'chosen.txt'}]
    normalized, issues = normalize_task_output_materializations(state, task)
    assert not issues
    assert normalized['task_output_materializations'][0]['required'] is True
    assert plan_result_contracts(normalized)['out-report']['necessity_source'] == 'task_spec'
    assert plan_result_contracts(normalized)['out-report']['required'] is True


@pytest.mark.parametrize('policy', ['planner_materialized', 'model_selectable'])
def test_v3_required_unresolved_path_still_needs_choice(tmp_path, policy):
    from agent_core.plan_tracker import normalize_task_output_materializations
    state, task = output_state(tmp_path)
    task['output_contracts'][1]['path_policy'] = policy
    _, issues = normalize_task_output_materializations(state, task)
    assert {row['code'] for row in issues} == {'TASK_OUTPUT_MATERIALIZATION_MISSING'}


def test_v3_task_output_cannot_be_downgraded(tmp_path):
    from agent_core.plan_tracker import normalize_task_output_materializations
    state, task = output_state(tmp_path)
    state['task_output_materializations'] = [{'output_ref': 'out-report', 'path': 'chosen.txt', 'required': False}]
    _, issues = normalize_task_output_materializations(state, task)
    assert 'TASK_OUTPUT_AUTHORITY_CONFLICT' in {row['code'] for row in issues}


def test_v3_exact_task_output_needs_no_materialization_copy(tmp_path):
    from agent_core.plan_tracker import normalize_task_output_materializations
    from agent_core.plan_contract import plan_result_contracts
    state, task = output_state(tmp_path, exact=True)
    normalized, issues = normalize_task_output_materializations(state, task)
    assert not issues and normalized['task_output_materializations'] == []
    assert plan_result_contracts(normalized)['out-report']['canonical_path'] == str(tmp_path / 'fixed.txt')


def test_v3_frozen_output_projection_requires_its_reader(tmp_path):
    from agent_core.plan_contract import plan_result_contracts, minimum_reader_for_plan, assert_reader_compatible, ReaderCompatibilityError
    state, task = output_state(tmp_path, exact=True)
    frozen = {'plan_document': state['plan_document'], 'task_output_contracts': task['output_contracts'],
              'steps': candidate()['steps'], 'minimum_reader_version': minimum_reader_for_plan(state)}
    assert frozen['minimum_reader_version'] == '1.2.3'
    assert plan_result_contracts(frozen)['out-report']['required'] is True
    with pytest.raises(ReaderCompatibilityError): assert_reader_compatible(frozen, reader_version='1.2.2')
    assert_reader_compatible(frozen)
    assert minimum_reader_for_plan({'plan_resource_bindings': {'x': {'role_associations': [{}]}}}) == '1.2.2'


def test_changed_consumption_is_advice_without_confirmation():
    from agent_core.plan_contract import validate_planner_consumption_correction
    from agent_core.plan_tracker import derive_plan_validation_state
    prior = candidate(); current = deepcopy(prior)
    current['steps'][1]['resource_uses'] = []
    advice = validate_planner_consumption_correction(prior, current, [
        {'code': 'PLAN_ARTIFACT_UNAVAILABLE', 'consumer_step_id': 'check-instance', 'resource_id': 'plan.workspace'}])
    assert advice and all(row['severity'] == 'warning' for row in advice)
    assert all(row['code'] != 'PLAN_CONSUMPTION_CHANGE_REVIEW_REQUIRED' for row in advice)
    baseline = derive_plan_validation_state(current, [], record_convergence=True)
    reviewed = derive_plan_validation_state(current, advice, record_convergence=True)
    assert reviewed['plan_validation_state']['status'] == baseline['plan_validation_state']['status']
    assert reviewed.get('validation_convergence_history') == baseline.get('validation_convergence_history')


@pytest.mark.parametrize('required,expected', [(None, True), (False, False), (True, True)])
def test_v3_local_necessity_default_and_allocation_are_consistent(tmp_path, required, expected):
    from agent_core.plan_contract import plan_result_contracts, prepare_allocation_intent, promote_allocation_intent
    value = candidate()
    if required is not None:
        value['proposed_artifacts'][0]['artifact_contract']['required'] = required
    assert plan_result_contracts(value)['plan.workspace']['required'] is expected
    intent = prepare_allocation_intent(value, candidate_scope_id='new-scope', planning_epoch_id='new-epoch')
    mapping, bindings = promote_allocation_intent(intent, task_id='new-task', managed_root=str(tmp_path))
    assert bindings[mapping['plan.workspace']]['required'] is expected


def test_explicit_optional_local_cannot_feed_unconditional_read():
    from agent_core.step_effects import compile_plan_dataflow
    value = candidate()
    value['steps'] = [value['steps'][1]]
    value['steps'][0]['dependencies'] = []
    value['steps'][0]['resource_uses'][0].pop('role')
    value['proposed_artifacts'][0]['artifact_contract'].update(object_kind='file', resource_kind='immutable_artifact', required=False)
    _, _, issues = compile_plan_dataflow(value, {}, compile_effect_contracts=False)
    matching = [row for row in issues if row['code'] == 'PLAN_OPTIONAL_RESULT_REQUIRED_BY_CONSUMER']
    assert matching and matching[0]['field_path'] == '$.steps[0].resource_uses[0].resource_ref'

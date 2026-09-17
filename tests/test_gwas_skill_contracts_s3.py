"""Updated real Skill metadata and helper closure through production core.

Run under tests/offline_managed_bootstrap.py; configuration/network detection
alone is isolated by that bootstrap. No binding or approval core replacement.
"""
from copy import deepcopy
from pathlib import Path
import shutil

import pytest

from agent_core.skill_access import SkillAccessError, SkillAccessService, new_skill_access_state
from agent_core.skill_manager import SkillManager
from agent_core.skill_resource_binding import bind_action_skill_resources, bind_skill_resources, validate_action_skill_resources

SID = 'population-genetics/gwas-vcf-prep-qc'
SKILLS = Path(__file__).resolve().parents[1] / 'skills'


def fixture(tmp_path):
    # Mutable synthetic installed copy lets drift tests leave the frozen candidate alone.
    root = tmp_path / 'skills'
    shutil.copytree(SKILLS / SID, root / SID)
    manager = SkillManager(str(root))
    service = SkillAccessService(manager)
    access = service.ensure_selected_skill_refs(new_skill_access_state(), [SID], task_revision=1)
    return manager, service, access, root


def test_current_skill_loads_distinct_capabilities_and_standardlib_needs_no_scientific_binding(tmp_path):
    manager, _, _, _ = fixture(tmp_path)
    assert manager.get_skill_meta(SID)['version'] == '1.1.0'
    capabilities = manager.get_execution_capability_registry([SID])
    prefix = 'population.genetics.gwas.vcf.prep.qc.'
    assert capabilities[prefix + 'inspect_vcf_samples']['required_runtime_binding_ids'] == ['runtime.bcftools.samples.bin']
    for operation in ['inspect_inputs', 'prepare_phenotype', 'prepare_q_covariates', 'execute']:
        assert capabilities[prefix + operation]['required_runtime_binding_ids'] == []
    inspect = capabilities[prefix + 'inspect_inputs']
    assert inspect['produces_artifacts'] == []
    assert inspect['produces_observations'] == [{'observation_id': 'gwas_input_structure', 'evidence_contract': {'required_fields': ['value']}}]
    assert 'unknown' in ' '.join(inspect['execution_guidance']).lower()
    assert 'execute' == capabilities[prefix + 'execute']['operation']  # legacy capability identity survives


@pytest.mark.parametrize('script,arguments', [
    ('inspect_inputs.py', ['--vcf', 'input.vcf', '--phenotype', 'input.table', '--observation-id', 'gwas_input_structure']),
    ('sort_phenotype.py', ['--tfam', 'target.tfam', '--phenotype', 'input.table', '--trait', '2', '--header', 'absent']),
])
def test_real_helper_root_explicit_closure_and_argument_contract(tmp_path, script, arguments):
    _, service, access, _ = fixture(tmp_path)
    handle = SID + '#scripts/' + script
    receipts = bind_skill_resources(service, access, [handle], step_id='input_check', cwd=tmp_path)
    assert [row['relative_path'] for row in receipts] == ['scripts/' + script, 'scripts/input_contract.py']
    assert receipts[0]['invocation']['runtime'] == 'controller_python'
    action = {'type': 'execute', 'argv': receipts[0]['invocation']['argv_prefix'] + arguments, 'skill_resource_refs': [handle]}
    bound = bind_action_skill_resources(action, service, access, step_id='input_check', cwd=tmp_path)
    assert bound['argv'] == action['argv']
    assert len(bound['formal_skill_resources']) == 2
    validate_action_skill_resources(bound, service, access, step_id='input_check', cwd=tmp_path)


def test_dependency_source_drift_is_rejected_without_mutating_frozen_action(tmp_path):
    _, service, access, root = fixture(tmp_path)
    handle = SID + '#scripts/inspect_inputs.py'
    receipts = bind_skill_resources(service, access, [handle], step_id='input_check', cwd=tmp_path)
    action = {'type': 'execute', 'argv': receipts[0]['invocation']['argv_prefix'] + [
        '--vcf', 'a.vcf', '--phenotype', 'a.tsv', '--observation-id', 'gwas_input_structure'], 'skill_resource_refs': [handle]}
    bound = bind_action_skill_resources(action, service, access, step_id='input_check', cwd=tmp_path)
    old = deepcopy(bound)
    helper = root / SID / 'scripts/input_contract.py'
    helper.write_text(helper.read_text() + '\n# Synthetic drift after approval identity was frozen.\n')
    with pytest.raises(SkillAccessError):
        validate_action_skill_resources(bound, service, access, step_id='input_check', cwd=tmp_path)
    assert bound == old


def test_formal_invocation_keeps_explicit_tassel_flag_value(tmp_path):
    _, service, access, _ = fixture(tmp_path)
    handle = SID + '#scripts/sort_phenotype.py'
    receipts = bind_skill_resources(service, access, [handle], step_id='input_check', cwd=tmp_path)
    argv = receipts[0]['invocation']['argv_prefix'] + ['--tfam', 'a.tfam', '--phenotype', 'a.tsv', '--trait', '2', '--tassel-covariate']
    with pytest.raises(SkillAccessError) as caught:
        bind_action_skill_resources({'type': 'execute', 'argv': argv, 'skill_resource_refs': [handle]}, service, access, step_id='input_check', cwd=tmp_path)
    assert caught.value.code.startswith('SKILL_INVOCATION_')
    bound = bind_action_skill_resources({'type': 'execute', 'argv': argv + ['true'], 'skill_resource_refs': [handle]}, service, access, step_id='input_check', cwd=tmp_path)
    assert bound['argv'] == argv + ['true']


@pytest.mark.parametrize('operation,outputs,observations', [
    ('inspect_inputs', [], ['gwas_input_structure']),
    ('inspect_vcf_samples', [], ['gwas_input_structure']),
    ('extract_vcf_samples', ['artifact.vcf_samples'], []),
])
def test_real_plan_effect_compilation_allows_optional_initial_sample_file(tmp_path, operation, outputs, observations):
    from agent_core.step_effects import compile_plan_step_effects, validate_plan_compilation_receipt
    manager, _, _, _ = fixture(tmp_path)
    registry = manager.get_execution_capability_registry([SID])
    cap = 'population.genetics.gwas.vcf.prep.qc.' + operation
    step = {'step_id': 'input_check', 'title': 'Inspect input sample structure', 'action': 'execute',
        'purpose': 'validation', 'tool': cap, 'execution_capability_id': cap, 'operation': operation,
        'inputs': ['input.vcf'], 'outputs': outputs, 'dependencies': [], 'skill_ids': [SID],
        'workflow_stage_ids': [], 'batch_id': '', 'risk_level': 'low',
        'produces_observations': observations, 'success_criteria': ['Publish the declared scoped observation or nonempty sample-list artifact.']}
    plan = {'task_id': 'gwas-input', 'plan_id': 'plan-gwas-input', 'plan_version': 'plan_v1',
        'plan_revision': 1, 'steps': [step], 'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}
    compiled, receipt, issues = compile_plan_step_effects(plan, registry, allow_legacy_untyped=False)
    assert issues == []
    assert receipt['effect_complete'] is True
    assert compiled['steps'][0]['outputs'] == outputs
    effect = compiled['steps'][0]['step_effect']
    assert effect['produces_artifacts'] == outputs
    assert [row['identity'] if isinstance(row, dict) else row for row in effect['produces_observations']] == observations
    assert validate_plan_compilation_receipt(receipt, compiled, registry, require_complete=True)['valid']
    if operation == 'extract_vcf_samples':
        assert registry[cap]['required_runtime_binding_ids'] == ['runtime.bcftools.samples.bin']
        plan['steps'][0]['outputs'] = []
        _, _, missing = compile_plan_step_effects(plan, registry, allow_legacy_untyped=False)
        assert any(row['code'] == 'PLAN_STEP_ARTIFACT_EFFECT_UNBOUND' for row in missing)

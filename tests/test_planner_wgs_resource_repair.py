"""Offline production Planner entry replay; never execute scientific inputs.

Archived input paths are semantic evidence only. Every mutable task store and
all test reports live under pytest/TemporaryDirectory roots, not the benchmark.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_bindings import compile_plan_resource_bindings, PlanBindingError
from agent_core.plan_tracker import load_plan_state, save_plan_state, canonical_task_spec_for_state

PROJECT = Path(__file__).resolve().parents[2]
ARCHIVE = Path(os.environ.get('BIOMIND_WGS_PLANNER_ARCHIVE', str(PROJECT /
    'benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789718925')))
GATK = 'genomics/wgs-gatk-snp-indel'


def evidence(name, value):
    root = os.environ.get('BIOMIND_WGS_REPAIR_EVIDENCE')
    if root:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def recorded():
    if not (ARCHIVE / 'plan_state.json').is_file():
        pytest.skip('Recorded WGS session unavailable; set BIOMIND_WGS_PLANNER_ARCHIVE')
    state = json.loads((ARCHIVE / 'plan_state.json').read_text())
    candidates = [json.loads(json.loads(path.read_text())['content'])
        for path in sorted((ARCHIVE / 'context_trace').glob('planner_initial*/response.json'))]
    assert len(candidates) == 4
    return state, candidates






def resource_corrected(candidate):
    value = deepcopy(candidate)
    for row in value['proposed_artifacts']:
        row['artifact_contract'].pop('resource_kind', None)  # Existing new-artifact default.
    for step in value['steps']:
        step['resource_uses'] = [use for use in step['resource_uses'] if use['access'] == 'read']
        step.pop('workspace_mutations', None)
        if step['step_id'] == 'prepare_reference':
            step['outputs'] = ['plan.reference_bundle', 'plan.reference_prep_evidence']
            # Fixed capability facts need not be copied into each candidate.
            for field in ('operation', 'produces_observations', 'explicit_side_effects'):
                step.pop(field, None)
    return value


def complete_wgs(candidate):
    value = resource_corrected(candidate)
    rows = {step['step_id']: step for step in value['steps']}
    target = rows['check_output_dir_conflicts']
    target['resource_uses'] = []
    target['success_criteria'] = [
        'Inspect the declared output directory location/parent for writability and filename collisions without consuming future result contents.',
        'Preserve existing files; on a collision ask the user before any affected write. No scientific output is asserted to exist.']
    rows['prepare_reference']['dependencies'].append(target['step_id'])
    question = deepcopy(rows['confirm_ploidy'])
    question.update(step_id='confirm_read_groups', title='Confirm read groups before mapping', dependencies=['inspect_inputs'],
        success_criteria=['Ask for sample identity, platform, library and platform unit/read-group values from official metadata or the user.',
            'Do not infer PL/LB/PU from S1/S2 filenames. Mapping must wait; no unresolved placeholder is an executable value.'])
    value['steps'].insert(3, question)
    for sample in ('s1', 's2'):
        step = rows['map_' + sample]
        step['dependencies'].append(question['step_id'])
        step['success_criteria'][0] = 'Use BWA-MEM, coordinate sorting and duplicate marking; use only read-group values confirmed by confirm_read_groups. Missing selected-stage tools means stage_blocked.'
    calling = rows['call_variants']
    calling.update(title='GATK per-sample HaplotypeCaller gVCFs then joint GenotypeGVCFs',
        execution_capability_id='genomics.wgs.gatk.snp.indel.execute', operation='produce_files', skill_ids=[GATK])
    calling['success_criteria'] = [
        'Select the GATK workflow: HaplotypeCaller per sample in gVCF mode, then combine and joint genotype with GenotypeGVCFs. No alternative bcftools calling chain.',
        'Use only confirmed sample ploidy. Wait for confirm_ploidy; no default diploidy and no unresolved executable argument.',
        'Record missing GATK or its selected-stage dependencies as stage_blocked with affected downstream stages. A tool used only by the unselected caller cannot block mapping.']
    # Replace misleading per-sample VCF intermediates by explicit gVCFs plus the
    # joint raw VCF, then one joint filtered result. This fixture is a model
    # candidate repair, not an automatic production semantic transformation.
    for row in value['proposed_artifacts']:
        if row['local_ref'] in {'plan.s1_raw_variants', 'plan.s2_raw_variants'}:
            row['semantic_view'].update(format='gvcf.gz', description='Per-sample GATK gVCF intermediate before joint genotyping')
    joint = deepcopy(next(row for row in value['proposed_artifacts'] if row['local_ref'] == 'plan.s1_raw_variants'))
    joint['local_ref'] = 'plan.joint_raw_vcf'
    joint['semantic_view'].update(role='joint_raw_vcf', format='vcf.gz', description='Joint GATK genotype VCF')
    joint['artifact_contract'].pop('path', None)
    joint['artifact_contract']['path_policy'] = 'model_selectable'
    value['proposed_artifacts'].append(joint)
    calling['outputs'].append(joint['local_ref'])
    filter_question = deepcopy(question)
    filter_question.update(step_id='confirm_filters', title='Confirm filtering assumptions and thresholds', dependencies=['call_variants'],
        success_criteria=['Ask the user to confirm scientifically applicable SNP/InDel filtering criteria and thresholds after reviewing available official evidence.',
            'No QUAL/DP/QD/FS/MQ constants have been authorized yet; filtering waits for an explicit decision.'])
    position = value['steps'].index(rows['filter_variants'])
    value['steps'].insert(position, filter_question)
    filtering = rows['filter_variants']
    filtering.update(execution_capability_id=calling['execution_capability_id'], operation='produce_files', skill_ids=[GATK],
        dependencies=['call_variants', 'confirm_filters'], resource_uses=[{'resource_ref': joint['local_ref'], 'access': 'read'}],
        outputs=['output.filtered_vcf'], success_criteria=[
            'Use the selected GATK filtering workflow with explicitly confirmed SNP/InDel criteria; publish the filtered joint VCF with sample genotypes retained.',
            'Record exact filters, tool roles and provenance. Block this stage if upstream calling or the decision is missing; never fabricate VCF.'])
    value['proposed_artifacts'] = [row for row in value['proposed_artifacts'] if '_filtered_' not in row['local_ref']]
    stats = rows['summarize_variants']
    stats['resource_uses'] = [use for use in stats['resource_uses'] if '_filtered_' not in use['resource_ref']]
    stats['resource_uses'].insert(0, {'resource_ref': 'output.filtered_vcf', 'access': 'read'})
    stats['outputs'].extend(['plan.s1_flagstat', 'plan.s2_flagstat'])
    stats['success_criteria'][0] = 'Use bcftools only for auxiliary VCF statistics and samtools flagstat for mapping metrics; record this division of work with GATK as the sole caller.'
    rows['write_report']['success_criteria'][0] = 'Explain mapping Skill selection and the GATK gVCF/joint-calling route; identify bcftools as auxiliary statistics only.'
    return value












def test_planning_budget_display_never_invents_counts():
    from agent_core.cli_lifecycle import _stop_text
    assert '未知/未知' in _stop_text({}, 'PLANNING_EPOCH_EXHAUSTED')
    assert '4/未知' in _stop_text({'model_turns': 4}, 'PLANNING_EPOCH_EXHAUSTED')


def test_unique_workspace_role_can_supply_mechanical_transition_but_conflicts_still_fail():
    from agent_core.step_effects import compile_plan_step_effects
    from tests.test_planner_resource_protocol_v2 import candidate
    from agent_core.workspace_attestation import BOUNDED_SNAPSHOT_COLLECTOR
    plan = candidate('mutable_workspace')
    plan['metadata']['resource_contract_version'] = 3
    transition = {'workspace_ref': 'workspace.cache', 'mutation': 'create_and_stage',
        'requires_available': False, 'available_after_success': True, 'establishes_state': 'ready',
        'evidence_contract': {'schema_version': 1, 'collector': BOUNDED_SNAPSHOT_COLLECTOR,
                             'postconditions': [{'kind': 'file_nonempty', 'relative_path': 'manifest.txt'}]}}
    cap = {'capability_id': 'fixture.prepare', 'operation': 'execute', 'workspace_mutations': [transition]}
    plan['steps'] = [{'step_id': 'prepare', 'action': 'execute', 'execution_capability_id': cap['capability_id'],
        'dependencies': [], 'outputs': [], 'resource_uses': [
            {'resource_ref': 'plan.new', 'role': 'workspace.cache', 'access': 'mutate'}]}]
    original = deepcopy(plan)
    bindings = compile_plan_resource_bindings(plan, candidate_scope_id='workspace', authoritative_handles={},
        capability_registry={cap['capability_id']: cap})
    compiled, _, issues = compile_plan_step_effects(plan, {cap['capability_id']: cap}, resource_bindings=bindings)
    assert issues == [], issues
    assert compiled['steps'][0]['step_effect']['workspace_mutations']
    assert plan == original
    conflicting = deepcopy(plan)
    conflicting['steps'][0]['workspace_mutations'] = [{**transition, 'mutation': 'replace_unapproved_state'}]
    _, _, issues = compile_plan_step_effects(conflicting, {cap['capability_id']: cap}, resource_bindings=bindings)
    assert any(i['code'] == 'STEP_EFFECT_PROTOCOL_INVALID' for i in issues), issues
    assert conflicting['steps'][0]['workspace_mutations'][0]['mutation'] == 'replace_unapproved_state'
    missing = deepcopy(plan)
    missing['steps'][0]['resource_uses'] = []
    with pytest.raises(PlanBindingError) as raised:
        compile_plan_resource_bindings(missing, candidate_scope_id='workspace', authoritative_handles={},
            capability_registry={cap['capability_id']: cap})
    assert any(i['code'] == 'PLAN_RESOURCE_USE_ROLE_MISSING' for i in raised.value.to_dict()['validation_issues'])



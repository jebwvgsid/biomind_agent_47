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


@asynccontextmanager
async def harness(candidates, *, selected_gatk=False, output_dir=None):
    from tests.test_consultant_first_graph_integration_v21 import ConsultantFirstGraphIntegrationV21Tests
    case = ConsultantFirstGraphIntegrationV21Tests(methodName='test_schema2_ready_to_plan_handoff_enters_production_planner')
    await case.asyncSetUp()
    try:
        archive, _ = recorded()
        tid = archive['task_id']
        task = archive['current_task']['task_spec']
        task_state = archive['task_state']
        if selected_gatk:
            archive['selected_skill_ids'].append(GATK)
            task_state['accepted_consultant_handoff']['selected_skills'] = list(archive['selected_skill_ids'])
            task_state['accepted_consultant_handoff']['summary'] = (
                'Offline corrected handoff: select mapping then GATK gVCF/joint calling; '
                'bcftools may provide auxiliary statistics only. Read groups, ploidy and '
                'filter thresholds remain unconfirmed and must wait before first use.')
            for refs in (archive['selected_skill_refs'], archive['skill_access_state']['selected_skill_refs'],
                         task_state['selected_skill_refs'], task_state['skill_access_state']['selected_skill_refs']):
                for ref in refs:
                    if ref['skill_id'] == GATK:
                        ref.update(status='active', selected_at_task_revision=1)
        if output_dir is not None:
            # Only terminal-writer tests substitute a destination. Scientific
            # input references and raw benchmark files are never changed/read.
            old = '/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-WGS-005/medium'
            task = json.loads(json.dumps(task, ensure_ascii=False).replace(old, str(output_dir)))
        shutil.copytree(ARCHIVE / 'state_contract', case.root / 'task_outputs' / tid / 'state_contract', dirs_exist_ok=True)
        clean = {'task_id': tid, 'task_spec': task, 'steps': [], 'state_generation': 0,
            'workflow_routing_schema_version': 2, 'selected_skill_ids': archive['selected_skill_ids'],
            'skill_access_state': archive['skill_access_state'], 'selected_skill_refs': archive['selected_skill_refs'],
            'conversation_evidence': archive['conversation_evidence'],
            'task_state': {key: task_state[key] for key in ('consultation_state', 'accepted_consultant_handoff',
                'selected_skills', 'skill_access_state', 'selected_skill_refs', 'intent_admission') if key in task_state}}
        clean['task_state'].update(status='running', workflow_routing_schema_version=2)
        if selected_gatk:
            clean['task_state']['selected_skills'] = list(archive['selected_skill_ids'])
        save_plan_state(case.root, tid, clean)
        runner = {'messages': [], 'task_id': tid, 'session_id': tid, 'project_id': case.agent.default_project_id,
            'task_mode': 'managed', 'workflow_routing_schema_version': 2, 'task_spec': task, 'task_revision': 1,
            'selected_skills': archive['selected_skill_ids'], 'selected_skills_raw': archive['selected_skill_ids'],
            'consultation_state': task_state['consultation_state'],
            'requirements_summary': task_state['accepted_consultant_handoff']['summary'],
            'resolved_bindings': {}, 'runtime_audit': []}
        captures = []
        async def scripted(messages, *, node_context, stage, **kwargs):
            assert stage == 'planner', 'Unexpected provider stage: ' + stage
            assert len(captures) < len(candidates), 'Unexpected extra provider request'
            captures.append({'messages': [str(message.content) for message in messages],
                'payload': node_context.llm_payload.to_dict()})
            return AIMessage(content=json.dumps(candidates[len(captures) - 1], ensure_ascii=False))
        case.agent._ainvoke_node_llm = scripted
        yield case, runner, captures
    finally:
        await case.asyncTearDown()
        shutil.rmtree(case.root)


async def submit(case, runner):
    result = await case.agent._graph_node_handlers['planner'](runner)
    runner.update({key: value for key, value in result.items() if key != 'messages'})
    return result, load_plan_state(case.root, runner['task_id'])


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


def test_four_archived_candidates_reenter_actual_planner_with_full_feedback():
    async def run():
        _archive, candidates = recorded()
        initial = (ARCHIVE / 'plan_state.json').read_bytes()
        async with harness(candidates) as (case, runner, captures):
            expected_refs = {'plan.reference_bundle', 'plan.reference_prep_evidence',
                'plan.s1_sorted_markdup_bam', 'plan.s2_sorted_markdup_bam'}
            for i in range(4):
                _, state = await submit(case, runner)
                issues = state['task_state']['planning_failure']['validation_issues']
                evidence('candidate_' + str(i + 1), {'validation_issues': issues,
                    'planner_calls': len(captures), 'accepted': False,
                    'original_candidate_sha256': hashlib.sha256(json.dumps(candidates[i], sort_keys=True).encode()).hexdigest()})
                conflicts = [row for row in issues if row['code'] == 'PLAN_RESOURCE_USE_EFFECT_CONFLICT']
                assert (expected_refs if i < 3 else {'plan.reference_bundle', 'plan.reference_prep_evidence'}) <= {row['resource_ref'] for row in conflicts}
                if i == 0:
                    assert any(row['code'] == 'PLAN_RESOURCE_USE_ROLE_INVALID' for row in issues)
                assert all(row['expected'].get('output_interfaces') for row in conflicts)
                assert all('outputs' in row['expected']['declaration'] for row in conflicts)
                assert not any(row['code'] == 'REQUIRED_WORKSPACE_UNCOVERED' for row in issues)
                assert not state['steps'] and not state.get('current_approval')
                assert state['task_state']['planning_epoch']['planner_calls_total'] == i + 1
                if i:
                    request = json.dumps(captures[-1], ensure_ascii=False)
                    assert 'PLAN_RESOURCE_USE_EFFECT_CONFLICT' in request
                    assert all(ref in request for ref in expected_refs)
            failure = state['task_state']['planning_failure']
            assert failure['terminal_diagnostic']['error_code'] == 'PLANNING_EPOCH_EXHAUSTED'
            assert failure['terminal_diagnostic']['machine_evidence']['planner_calls_total'] == 4
            assert len(captures) == 4
        assert (ARCHIVE / 'plan_state.json').read_bytes() == initial
    asyncio.run(run())


def test_one_resource_correction_reveals_real_remaining_dependencies():
    async def run():
        _archive, candidates = recorded()
        async with harness([candidates[0], resource_corrected(candidates[-1])]) as (case, runner, captures):
            await submit(case, runner)
            _, state = await submit(case, runner)
            issues = state['task_state']['planning_failure']['validation_issues']
            assert not any(row['code'] in {'PLAN_RESOURCE_USE_ROLE_INVALID', 'PLAN_RESOURCE_USE_EFFECT_CONFLICT', 'PLAN_RESOURCE_KIND_INVALID'} for row in issues)
            assert any(row['code'] == 'PLAN_ARTIFACT_UNAVAILABLE' for row in issues)
            assert any(row['code'] == 'REQUIRED_OUTPUT_UNCOVERED' for row in issues)
            assert len(captures) == 2
    asyncio.run(run())


def test_complete_wgs_plan_is_admitted_without_executing_or_guessing_parameters():
    async def run():
        archive, candidates = recorded()
        fixed = complete_wgs(candidates[-1])
        async with harness([fixed], selected_gatk=True) as (case, runner, captures):
            result, state = await submit(case, runner)
            assert state.get('current_approval', {}).get('status') == 'pending', state.get('task_state', {}).get('planning_failure')
            assert len(captures) == 1
            steps = {step['step_id']: step for step in state['steps']}
            assert 'confirm_read_groups' in steps['map_s1']['dependencies']
            assert 'confirm_ploidy' in steps['call_variants']['dependencies']
            assert 'confirm_filters' in steps['filter_variants']['dependencies']
            assert steps['prepare_reference']['step_effect']['produces_artifacts']
            assert not steps['prepare_reference']['step_effect']['workspace_mutations']
            assert canonical_task_spec_for_state(state).get('parameters') in ({}, None)
            assert not state.get('provenance_state', {}).get('attempts')
            assert result.get('next_step') == 'plan_review'
            assert 'PL:ILLUMINA' not in json.dumps(fixed)
            assert 'QUAL>=20' not in json.dumps(fixed)
            evidence('complete_wgs_plan', {'candidate': fixed, 'approval': state['current_approval'],
                'compiled_steps': state['steps'], 'planner_calls': len(captures),
                'scientific_commands_executed': 0, 'live_model_calls': 0})
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['unknown_ref', 'missing_dependency', 'foreign_input'])
def test_bad_reference_does_not_disable_other_checks_or_gain_admission(fault):
    async def run():
        _, candidates = recorded()
        fixed = complete_wgs(candidates[-1])
        if fault == 'unknown_ref':
            fixed = deepcopy(candidates[0])
            fixed['steps'][2]['resource_uses'].append({'resource_ref': 'plan.unknown', 'access': 'mutate'})
        elif fault == 'missing_dependency':
            next(s for s in fixed['steps'] if s['step_id'] == 'map_s1')['dependencies'] = ['confirm_read_groups']
        else:
            next(s for s in fixed['steps'] if s['step_id'] == 'call_variants')['resource_uses'].append(
                {'resource_ref': 'other_task.old_genomeDB', 'access': 'read'})
        async with harness([fixed], selected_gatk=fault != 'unknown_ref') as (case, runner, captures):
            _, state = await submit(case, runner)
            issues = state['task_state']['planning_failure']['validation_issues']
            assert not state.get('current_approval') and not state['steps']
            assert len(captures) == 1
            if fault == 'unknown_ref':
                unknown = [i for i in issues if i.get('resource_ref') == 'plan.unknown']
                assert len(unknown) == 1
                assert unknown[0]['code'] == 'PLAN_RESOURCE_REFERENCE_NOT_DECLARED'
                assert 'resource_kind' in unknown[0]['not_evaluated']
                assert any(i.get('resource_ref') == 'plan.s2_sorted_markdup_bam'
                           and i['code'] == 'PLAN_RESOURCE_USE_EFFECT_CONFLICT' for i in issues)
            elif fault == 'missing_dependency':
                assert any(i['code'] == 'PLAN_ARTIFACT_UNAVAILABLE' for i in issues), issues
            else:
                assert any(i['code'] in {'ARTIFACT_REFERENCE_UNKNOWN', 'PLAN_RESOURCE_REFERENCE_NOT_DECLARED'} for i in issues)
    asyncio.run(run())


@pytest.mark.parametrize('protected', [False, True])
def test_actual_planning_stop_publishes_only_stop_materials_and_protects_others(tmp_path, protected):
    from agent_core.workflow.early_nodes import TypedPlanner
    from agent_core.workflow.managed_stop import finalize_managed_stop, projection, current_managed_stop
    from agent_core.cli_lifecycle import terminal_text
    async def run():
        _, candidates = recorded()
        destination = tmp_path / 'requested'
        names = ('report.md', 'reproduce.sh', 'stage_status.tsv')
        if protected:
            destination.mkdir()
            for name in names:
                (destination / name).write_text('Another task owns this file.\n')
        async with harness(candidates, output_dir=destination) as (case, runner, captures):
            for _ in candidates:
                _, state = await submit(case, runner)
            adapter = TypedPlanner(data_path=str(case.root), source=case.agent._graph_node_handlers['planner'])
            outcome = adapter.classify({}, runner, state)
            assert outcome['type'] == 'terminal', outcome
            case.agent.terminal_summary_policy = {'enabled': False}
            stop = await finalize_managed_stop(case.agent, runner['task_id'], runner,
                {'node': 'planner', 'status': outcome['payload']['status'], 'outcome': outcome})
            assert stop['persistence_status'] == 'committed', stop
            assert current_managed_stop(load_plan_state(case.root, runner['task_id']))
            assert not stop['execution']['executed']
            assert stop['scientific_completion'] == 'not_completed'
            assert stop['summary_llm_calls'] == 0 and len(captures) == 4
            text = terminal_text(stop['status'], projection(stop)['terminal_diagnostic'])
            assert '4/4' in text and 'Plan 候选' in text
            assert '查看当前步骤的执行与核验详情' not in text
            assert not list(destination.glob('*.vcf*'))
            if protected:
                assert stop['required_output_status'] == 'conflict'
                assert all((destination / name).read_text() == 'Another task owns this file.\n' for name in names)
                assert stop['internal_diagnostic_delivery']['status'] == 'complete'
            else:
                assert stop['required_output_status'] == 'complete', stop['report_receipts']
                assert set(stop['report_paths'].values()) == {str(destination / name) for name in names}
                script = (destination / 'reproduce.sh').read_text()
                assert 'no scientific commands were executed' in script
                assert [line for line in script.splitlines() if line and not line.startswith('#')] == ['exit 1']
                assert 'PLANNING_EPOCH_EXHAUSTED' in (destination / 'stage_status.tsv').read_text()
            evidence('planning_stop_' + ('protected' if protected else 'empty'),
                {'terminal_text': text, 'stop': stop, 'planner_calls': len(captures)})
    asyncio.run(run())


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


def test_real_planner_keeps_explicit_workspace_state_and_rejects_missing_transition():
    from agent_core.workspace_attestation import BOUNDED_SNAPSHOT_COLLECTOR
    async def run():
        _, candidates = recorded()
        valid = complete_wgs(candidates[-1])
        workspace = deepcopy(next(row for row in valid['proposed_artifacts'] if row['local_ref'] == 'plan.reference_bundle'))
        workspace['local_ref'] = 'plan.analysis_workspace'
        workspace['semantic_view'].update(role='analysis_workspace', description='Explicit stateful workspace, distinct from immutable reference indexes')
        workspace['artifact_contract']['resource_kind'] = 'mutable_workspace'
        workspace['artifact_contract'].pop('path', None)
        workspace['artifact_contract']['path_policy'] = 'model_selectable'
        valid['proposed_artifacts'].append(workspace)
        template = deepcopy(next(step for step in valid['steps'] if step['step_id'] == 'check_output_dir_conflicts'))
        for ordinal, name in enumerate(('prepare_work_state', 'update_work_state')):
            step = deepcopy(template)
            step.update(step_id=name, title=name, dependencies=[] if ordinal == 0 else ['prepare_work_state'],
                resource_uses=[{'resource_ref': workspace['local_ref'], 'access': 'mutate'}], outputs=[],
                success_criteria=['The explicit workspace state transition is attested.'],
                workspace_mutations=[{'workspace_ref': workspace['local_ref'], 'mutation': 'create_and_stage' if ordinal == 0 else 'update',
                    'requires_available': bool(ordinal), 'requires_state': 'ready' if ordinal else '',
                    'available_after_success': True, 'establishes_state': 'updated' if ordinal else 'ready',
                    'evidence_contract': {'schema_version': 1, 'collector': BOUNDED_SNAPSHOT_COLLECTOR,
                        'postconditions': [{'kind': 'file_nonempty', 'relative_path': 'manifest.txt'}]}}])
            valid['steps'].append(step)
        invalid = deepcopy(valid)
        invalid['steps'][-1].pop('workspace_mutations')
        async with harness([invalid, valid], selected_gatk=True) as (case, runner, captures):
            _, rejected = await submit(case, runner)
            issues = rejected['task_state']['planning_failure']['validation_issues']
            assert any(i['code'] == 'PLAN_RESOURCE_MUTATION_DECLARATION_MISSING' for i in issues), issues
            assert not rejected.get('current_approval')
            _, accepted = await submit(case, runner)
            evidence('workspace_entry_validation', accepted['task_state'].get('planning_failure'))
            assert accepted.get('current_approval', {}).get('status') == 'pending', accepted['task_state'].get('planning_failure')
            assert len(captures) == 2
            step = next(s for s in accepted['steps'] if s['step_id'] == 'update_work_state')
            assert step['step_effect']['workspace_mutations'][0]['requires_state'] == 'ready'
            assert not accepted.get('provenance_state', {}).get('attempts')
            evidence('explicit_workspace_entry', {'rejection': issues, 'accepted': True, 'planner_calls': 2, 'compiled_step': step})
    asyncio.run(run())

"""Registration reuse is metadata-only; science execution is never exercised."""
from pathlib import Path
from unittest.mock import Mock
import pytest

from package_manager import PackageManager
from agent_core.runtime_evidence import (make_runtime_evidence, merge_runtime_evidence,
    runtime_evidence_state, project_runtime_audits, digest)
from agent_core.action_runtime import (build_runtime_binding_registry, freeze_execution_environment,
    build_exact_action_environment, resolve_execution_runner)


def setup_registry(tmp_path):
    prefix = tmp_path / 'external' / 'science'
    (prefix / 'bin').mkdir(parents=True)
    python = prefix / 'bin' / 'python'
    python.write_text('#!/bin/sh\nexit 0\n')
    python.chmod(0o755)
    pm = PackageManager.__new__(PackageManager)
    pm.conda_root = str(tmp_path / 'conda')
    pm.registry = {'environments': {'science': {'env': 'science', 'prefix': str(prefix),
        'status': 'success', 'packages': ['cooler==0.9.3'], 'capabilities': ['tad-calling'],
        'language': 'python', 'last_verified_at': '1998-01-01T00:00:00'}}, 'tools': {}}
    pm._known_environment_prefixes = Mock(return_value={'science': [str(prefix)]})
    pm.verify_environment = Mock(side_effect=AssertionError('health probe forbidden'))
    pm.recent_environment_healthcheck = Mock(side_effect=AssertionError('health probe forbidden'))
    requirements = {'skill_id': 'tad', 'env': 'science', 'packages': ['cooler>=0.9'],
        'capabilities': ['tad-calling'], 'tools': ['cooler'], 'required_binding_ids': ['runtime.environment.prefix']}
    return pm, requirements, prefix


def test_registration_reuse_never_probes_or_refreshes_old_verification(tmp_path):
    pm, requirements, prefix = setup_registry(tmp_path)
    result = pm.registered_runtime_reuse(requirements)
    assert result['availability'] == 'AVAILABLE'
    audit = result['selected_candidate']['audit']
    assert audit['healthcheck_status'] == 'not_run'
    assert audit['last_verified_at'].startswith('1998')
    assert audit['resolved_runtime_paths']['RESOLVED_ENV_PREFIX'] == str(prefix)
    pm.verify_environment.assert_not_called()
    pm.recent_environment_healthcheck.assert_not_called()


def test_registered_evidence_retains_source_without_ttl_and_selects_exact_runner(tmp_path):
    pm, requirements, prefix = setup_registry(tmp_path)
    row = pm.registered_runtime_reuse(requirements)['selected_candidate']
    scope = {'skill_id': 'tad', 'skill_document_digest': digest('doc'), 'skill_contract_digest': digest('contract'),
        'environment_id': 'science', 'environment_fingerprint': digest(str(prefix)), 'host_id': 'test-host',
        'inspection_contract_digest': digest('requirements')}
    record = make_runtime_evidence(row['audit'], task_id='task', skill_scope=scope,
        observed_at='2000-01-01T00:00:00Z', producer='registered_runtime', source_ref=row['source_ref'],
        coverage=row['coverage'], inspection_status='not_run')
    state = {'runtime_evidence': runtime_evidence_state(merge_runtime_evidence([], [record], task_id='task'))}
    assert record['expires_at'] == ''
    projected = project_runtime_audits(state, task_id='task', now='2050-01-01T00:00:00Z')
    assert projected[0]['healthcheck_status'] == 'not_run'
    binding = build_runtime_binding_registry({'task_state': state, 'task_id': 'task'})['runtime.environment.prefix']
    assert binding['source'] == 'registered_runtime'
    frozen = freeze_execution_environment({'PATH': '/usr/bin:/bin'}, {'ENV_PREFIX': binding['value']})
    actual = build_exact_action_environment({'PATH': '/ambient/incorrect'}, action_environment=frozen)
    assert actual['PATH'].split(':')[0] == str(prefix / 'bin')
    assert resolve_execution_runner('Python', actual) == str(prefix / 'bin' / 'python')


def test_coverage_gap_does_not_infer_new_version_or_capability(tmp_path):
    pm, requirements, _ = setup_registry(tmp_path)
    for key, value in [('packages', ['cooler>=9']), ('capabilities', ['new-science']), ('tools', ['absent-cli'])]:
        result = pm.registered_runtime_reuse({**requirements, key: value})
        assert result['availability'] == 'UNKNOWN'
        assert result['reason_code'] == 'REGISTERED_RUNTIME_COVERAGE_GAP'
        assert not result['install_required']


def test_invalidated_registry_does_not_reuse_success_elsewhere(tmp_path):
    pm, requirements, _ = setup_registry(tmp_path)
    skill = dict(pm.registry['environments']['science'])
    pm.registry['environments']['science']['status'] = 'broken'
    assert pm.registered_runtime_reuse(requirements, skill_record=skill)['reason_code'] == 'REGISTERED_RUNTIME_INVALIDATED'


def test_external_prefix_and_same_name_inventory_ambiguity_are_not_guessed(tmp_path):
    pm, requirements, prefix = setup_registry(tmp_path)
    assert pm._environment_runtime_available('science')
    assert pm.discover_runtime_candidates(requirements)[0]['prefix'] == str(prefix)
    other = tmp_path / 'second' / 'science'
    other.mkdir(parents=True)
    del pm.registry['environments']['science']['prefix']
    pm._known_environment_prefixes.return_value = {'science': [str(prefix), str(other)]}
    result = pm.registered_runtime_reuse(requirements)
    assert result['reason_code'] == 'REGISTERED_RUNTIME_LOCATION_AMBIGUOUS'
    candidates = pm.discover_runtime_candidates(requirements)
    assert {row['prefix'] for row in candidates} == {str(prefix), str(other)}
    assert not pm._environment_runtime_available('science')


def test_declaration_is_not_registration(tmp_path):
    pm, requirements, _ = setup_registry(tmp_path)
    pm.registry = {'environments': {}, 'tools': {}}
    assert pm.registered_runtime_reuse(requirements)['reason_code'] == 'REGISTERED_RUNTIME_NOT_FOUND'


def test_formal_command_consumes_persisted_registration_without_model_env(tmp_path):
    import asyncio
    from agent_core.action_runtime import execute_formal_command
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    from tests.react_v3.support import task_fixture, actual_micro_runner

    async def scenario():
        async with task_fixture('registration-formal') as fixture:
            pm, requirements, prefix = setup_registry(tmp_path)
            row = pm.registered_runtime_reuse(requirements)['selected_candidate']
            scope = {'skill_id': 'tad', 'skill_document_digest': digest('doc'), 'skill_contract_digest': digest('contract'),
                'environment_id': 'science', 'environment_fingerprint': digest(str(prefix)), 'host_id': 'test-host',
                'inspection_contract_digest': digest('requirements')}
            evidence = make_runtime_evidence(row['audit'], task_id=fixture.task_id, skill_scope=scope,
                observed_at='2000-01-01T00:00:00Z', producer='registered_runtime', source_ref=row['source_ref'],
                coverage=row['coverage'], inspection_status='not_run')
            state = load_plan_state(fixture.root, fixture.task_id)
            state['task_state']['runtime_evidence'] = runtime_evidence_state(
                merge_runtime_evidence([], [evidence], task_id=fixture.task_id))
            save_plan_state(fixture.root, fixture.task_id, state)
            launches = []
            with actual_micro_runner(launches):
                result = await execute_formal_command(fixture.agent.command_service,
                    {'cmd': 'printf registered-runtime', 'workdir': str(fixture.root)},
                    task_id=fixture.task_id, call_id='registered-execution')
            assert result['exit_code'] == 0
            assert result['env']['CONDA_PREFIX'] == str(prefix)
            assert result['env']['PATH'].split(':')[0] == str(prefix / 'bin')
            assert len(launches) == 1
            pm.verify_environment.assert_not_called()
    asyncio.run(scenario())


def test_real_negative_supersedes_registration_without_host_fallback(tmp_path):
    from agent_core.action_runtime import registered_execution_environment, ActionRuntimeContractError
    pm, requirements, prefix = setup_registry(tmp_path)
    row = pm.registered_runtime_reuse(requirements)['selected_candidate']
    scope = {'skill_id': 'tad', 'skill_document_digest': digest('doc'), 'skill_contract_digest': digest('contract'),
        'environment_id': 'science', 'environment_fingerprint': digest(str(prefix)), 'host_id': 'test-host',
        'inspection_contract_digest': digest('requirements')}
    registered = make_runtime_evidence(row['audit'], task_id='task', skill_scope=scope,
        observed_at='2000-01-01T00:00:00Z', producer='registered_runtime', source_ref=row['source_ref'],
        coverage=row['coverage'], inspection_status='not_run')
    from datetime import datetime, timezone
    negative = make_runtime_evidence({'skill_id': 'tad', 'env': 'science', 'status': 'ready'},
        task_id='task', skill_scope=scope, observed_at=datetime.now(timezone.utc).isoformat(),
        source_ref='actual-execution:missing-python', supersedes=[registered['evidence_id']], coverage={
            'runtime.environment.prefix': {'availability': 'UNAVAILABLE', 'output_name': 'RESOLVED_ENV_PREFIX',
                'value_kind': 'directory_path', 'conclusion_complete': True, 'reason_code': 'EXACT_ENVIRONMENT_REMOVED'}})
    state = {'task_id': 'task', 'task_state': {'runtime_evidence': runtime_evidence_state(
        merge_runtime_evidence([], [registered, negative], task_id='task'))}}
    with pytest.raises(ActionRuntimeContractError, match='REGISTERED_EXECUTION_ENVIRONMENT_UNRESOLVED'):
        registered_execution_environment(state, {})
    # A newly approved explicit target is never overwritten by old registration.
    assert registered_execution_environment(state, {'ENV_PREFIX': '/explicit/new'}) == {'ENV_PREFIX': '/explicit/new'}


def test_persisted_registration_does_not_gain_new_declared_requirements(tmp_path):
    from agent_core.skill_manager import SkillManager
    manager = SkillManager.__new__(SkillManager)
    manager.runtime_registry = {'skills': {'tad': {'skill_id': 'tad', 'env': 'science', 'status': 'success', 'packages': ['cooler']}}}
    manager.get_skill_meta = Mock(return_value={})
    record = manager.get_runtime_registration('tad')
    assert record['packages'] == ['cooler']
    assert 'declared_runtime' not in record
    pm, requirements, _ = setup_registry(tmp_path)
    pm.registry['environments'] = {}
    requirements['packages'] = ['cooler>=9']
    assert pm.registered_runtime_reuse(requirements, skill_record=record)['availability'] == 'UNKNOWN'


def test_named_launch_is_pinned_to_registered_external_prefix(tmp_path):
    pm, requirements, prefix = setup_registry(tmp_path)
    requirements['env_runner'] = 'conda run -n science python'
    audit = pm.registered_runtime_reuse(requirements)['selected_candidate']['audit']
    assert audit['env_runner'] == 'conda run -p ' + str(prefix) + ' python'


def test_only_actual_dependency_failure_invalidates_selected_registration(tmp_path):
    from agent_core.registered_runtime import record_execution_invalidation
    from agent_core.action_runtime import registered_execution_environment, ActionRuntimeContractError
    pm, requirements, prefix = setup_registry(tmp_path)
    row = pm.registered_runtime_reuse(requirements)['selected_candidate']
    scope = {'skill_id': 'tad', 'skill_document_digest': digest('doc'), 'skill_contract_digest': digest('contract'),
        'environment_id': 'science', 'environment_fingerprint': digest(str(prefix)), 'host_id': 'test-host',
        'inspection_contract_digest': digest('requirements')}
    record = make_runtime_evidence(row['audit'], task_id='task', skill_scope=scope,
        observed_at='2000-01-01T00:00:00Z', producer='registered_runtime', source_ref=row['source_ref'],
        coverage=row['coverage'], inspection_status='not_run')
    state = {'task_id': 'task', 'task_state': {'runtime_evidence': runtime_evidence_state(
        merge_runtime_evidence([], [record], task_id='task'))}}
    receipt = {'status': 'exited', 'execution_started': True, 'exit_code': 1,
        'receipt_ref': '/recorded/execution-receipt.json', 'env': {'CONDA_PREFIX': str(prefix)}}
    for stderr in ['', 'TIMEOUT after 120 seconds', 'FileNotFoundError: input.mcool']:
        assert not record_execution_invalidation(state, {**receipt, 'stderr': stderr})
    precise = {**receipt, 'stderr': "ModuleNotFoundError: No module named 'cooler'\n"}
    assert not record_execution_invalidation(state, {**precise, 'status': 'cancelled'})
    rows = record_execution_invalidation(state, precise)
    assert rows[0]['missing_dependency'] == 'cooler'
    assert rows[0]['receipt_ref'] == receipt['receipt_ref']
    assert pm.registry['environments']['science']['status'] == 'success'
    for explicit in ({}, {'ENV_PREFIX': str(prefix)}):
        with pytest.raises(ActionRuntimeContractError, match='TARGETED_DIAGNOSIS'):
            registered_execution_environment(state, explicit)


def test_distinct_registered_operation_scopes_share_one_exact_launch_prefix(tmp_path):
    from agent_core.action_runtime import registered_execution_environment
    pm, requirements, prefix = setup_registry(tmp_path)
    row = pm.registered_runtime_reuse(requirements)['selected_candidate']
    scope = {'skill_id': 'tad', 'skill_document_digest': digest('doc'), 'skill_contract_digest': digest('contract'),
        'environment_id': 'science', 'environment_fingerprint': digest(str(prefix)), 'host_id': 'test-host'}
    records = [make_runtime_evidence(row['audit'], task_id='task',
        skill_scope={**scope, 'inspection_contract_digest': digest(operation)}, observed_at='2000-01-01T00:00:00Z',
        producer='registered_runtime', source_ref=row['source_ref'], coverage=row['coverage'], inspection_status='not_run')
        for operation in ('preflight', 'analysis')]
    state = {'task_id': 'task', 'task_state': {'runtime_evidence': runtime_evidence_state(
        merge_runtime_evidence([], records, task_id='task'))},
        'current_step_id': 'analysis', 'steps': [{'step_id': 'analysis', 'skill_ids': ['tad']}]}
    assert registered_execution_environment(state, {})['ENV_PREFIX'] == str(prefix)


def test_no_runtime_requirements_does_not_select_unrelated_registered_environment(tmp_path):
    pm, _, _ = setup_registry(tmp_path)
    pm._known_environment_prefixes.reset_mock()
    for requirements in ({}, {'skill_id': 'plain-document', 'language': 'python'}):
        result = pm.registered_runtime_reuse(requirements)
        assert result['availability'] == 'NOT_REQUIRED'
        assert not result['selected_candidate']
    pm._known_environment_prefixes.assert_not_called()
    result = pm.registered_runtime_reuse({'skill_id': 'explicit', 'default_env': 'different'})
    assert result['availability'] == 'UNKNOWN'




def test_actual_tad_skill_reuses_historical_registered_schema_without_healthcheck(tmp_path, monkeypatch):
    """Real exported TAD metadata, synthetic prefix; no science package imports."""
    from copy import deepcopy
    import json
    from agent_core.skill_manager import SkillManager
    import package_manager
    import agent_core.runtime_probe_contract as probes
    manager = SkillManager(str(Path(__file__).resolve().parents[1] / 'skills'))
    sid = '3d-dna/tad-calling'
    registered = manager.get_runtime_registration(sid)
    assert registered['env'] == 'hicexplore'
    assert registered['status'] == 'success'
    assert registered['last_verified_at'].startswith('2026-06-26')
    assert {'hicexplorer', 'cooler', 'matplotlib'} <= set(registered['packages'])
    assert {'tad-calling', 'hicfindtads', 'cooler-preflight'} <= set(registered['capabilities'])
    prefix = tmp_path / 'external-mamba' / 'hicexplore'
    (prefix / 'bin').mkdir(parents=True)
    pm = PackageManager.__new__(PackageManager)
    pm.conda_root = str(tmp_path / 'launch-conda-root')
    pm.registry = {'environments': {'hicexplore': {key: deepcopy(registered[key]) for key in
        ('env', 'status', 'packages', 'capabilities', 'language', 'last_verified_at')}}, 'tools': {}}
    pm._known_environment_prefixes = Mock(return_value={'hicexplore': [str(prefix)]})
    forbidden = Mock(side_effect=AssertionError('registered TAD must not run any health or science process'))
    pm.verify_environment = forbidden
    pm.recent_environment_healthcheck = forbidden
    monkeypatch.setattr(package_manager.subprocess, 'run', forbidden)
    monkeypatch.setattr(package_manager.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(probes, 'inspect_declared_runtime', forbidden)
    whole_skill = manager.get_runtime_requirements(sid)
    result = pm.registered_runtime_reuse(whole_skill, skill_record=registered)
    assert result['availability'] == 'AVAILABLE'
    assert result['selected_candidate']['prefix'] == str(prefix)
    assert result['selected_candidate']['audit']['healthcheck_status'] == 'not_run'
    assert result['selected_candidate']['audit']['last_verified_at'] == registered['last_verified_at']
    contracts = manager.get_execution_capability_registry([sid])
    checked = {}
    for cid, contract in contracts.items():
        if contract.get('selected_skill_id') != sid:
            continue
        requirements = manager.get_runtime_requirements(sid, capability_id=cid)
        current = pm.registered_runtime_reuse(requirements, skill_record=registered)
        assert current['availability'] == 'AVAILABLE', (cid, requirements, current)
        covered = current['selected_candidate']['coverage']
        assert all(covered[binding]['availability'] == 'AVAILABLE'
                   for binding in requirements['required_binding_ids'])
        checked[cid] = {'required_binding_ids': requirements['required_binding_ids'],
                        'covered_binding_ids': sorted(covered)}
    assert checked, 'Real TAD execution capability must be exercised'
    forbidden.assert_not_called()
    (tmp_path / 'actual-tad-registration-facts.json').write_text(json.dumps({
        'skill': sid, 'historical_verified_at': registered['last_verified_at'],
        'prefix': str(prefix), 'capabilities': checked, 'health_process_count': 0,
        'scientific_execution': False}, ensure_ascii=False, indent=2))

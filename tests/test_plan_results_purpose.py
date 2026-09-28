"""Synthetic stage-purpose regression; no scientific programs or live provider."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
import pytest








@pytest.mark.parametrize('negative', [False, True])
def test_actual_runtime_stop_keeps_science_incomplete_even_with_status_reports(negative):
    from tests.test_runtime_recovery_reporting import RuntimeRecoveryReportingTests
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.runtime_recovery_reporting import finalize_runtime_recovery_result
    from tests.test_cli_model_progress_integration import artifact

    async def run():
        case = RuntimeRecoveryReportingTests(methodName='test_negative_requires_durable_complete_evidence')
        await case.asyncSetUp()
        try:
            case.initialize()
            if negative:
                case.probe_result = {'inspection_status': 'succeeded', 'observed_at': datetime.now(timezone.utc).isoformat(),
                    'audit': {'skill_id': 'fixture/generic', 'env': 'testenv', 'status': 'ready'},
                    'coverage': {'runtime.widget.bin': {'availability': 'UNAVAILABLE', 'reason_code': 'complete_search_absent',
                        'conclusion_complete': True, 'value_kind': 'executable_path'}}}
            result = await case.recovered()
            before = load_plan_state(case.root, case.task_id)
            final = finalize_runtime_recovery_result(case.root, case.task_id, result)
            after = load_plan_state(case.root, case.task_id)
            assert final['resolution_disposition'] == ('dependency_blocked' if negative else 'unresolved'), final
            assert final['runtime_recovery_result']['scientific_goal_completed'] is False
            assert final['runtime_recovery_result']['summary_llm_calls'] == 0
            assert before['steps'] == after['steps'] and before['provenance_state'] == after['provenance_state']
            assert set(final['report_paths']) == {'report.md', 'reproduce.sh', 'stage_status.tsv'}
            assert not (case.task_root / 'annotated.vcf').exists()
            assert '科学目标完成：否' in Path(final['report_paths']['report.md']).read_text()
            artifact('plan-results-real-stop-' + str(negative) + '.json', {
                'source': 'synthetic durable runtime recovery, stub inspection',
                'result': final, 'provenance_unchanged': True, 'steps_unchanged': True,
                'scope_limit': 'Status report generation does not complete the unexecuted scientific stage.'})
        finally:
            await case.asyncTearDown()
            case.doCleanups()
    asyncio.run(run())




@pytest.mark.parametrize('marker,materialized', [(True, False), (False, True)])
def test_allow_probe_producer_is_only_a_legacy_compatibility_flag(tmp_path, marker, materialized):
    from agent_core.plan_tracker import ensure_provenance_state, record_provenance_event
    from tests.test_plan_result_contract_v1 import state_with_observation

    state = state_with_observation(tmp_path, required=True, with_file=True)
    if not marker:
        state['plan_document']['metadata'].pop('result_contract_version')
    provenance = ensure_provenance_state(state)
    provenance['attempts']['attempt-1']['produced_artifact_ids'] = []
    provenance['attempts']['attempt-1']['artifact_version_ids'] = []
    artifact_id = state['steps'][0]['output_artifact_ids'][0]
    definition = provenance['artifacts'][provenance['artifact_heads'][artifact_id]]
    definition['allow_probe_producer'] = True
    path = Path(definition['path'])
    path.write_text('a diagnostic-produced file, not an approved Plan execution\n')
    record_provenance_event(state, 'execution_finished', {
        'attempt_id': 'attempt-1', 'execution_record': {
            'execution_id': 'probe-1', 'command_role': 'diagnostic', 'purpose': 'probe',
            'step_id': 'inspect', 'exit_code': 0, 'produced_artifacts': [str(path)]}})
    assert (artifact_id + '@attempt-1' in state['provenance_state']['artifacts']) is materialized


@pytest.mark.parametrize('artifact_failed', [False, True])
def test_warning_acceptance_preserves_individual_artifact_failure_and_downstream_visibility(tmp_path, artifact_failed):
    from agent_core.plan_tracker import ensure_provenance_state, record_provenance_event
    from agent_core.managed_artifacts import _validated_runtime_realization
    from tests.test_plan_result_contract_v1 import state_with_observation

    state = state_with_observation(tmp_path, required=False, with_file=True)
    provenance = ensure_provenance_state(state)
    artifact_id = state['steps'][0]['output_artifact_ids'][0]
    head = provenance['artifact_heads'][artifact_id]
    row = provenance['artifacts'][head]
    row.update(status='materialized', producer_attempt_id='attempt-1')
    Path(row['path']).write_bytes(b'')
    provenance['attempts']['attempt-1']['artifact_version_ids'] = [head]
    validation = {'validation_id': 'warning-validation', 'status': 'VALID_WITH_WARNINGS',
        'warnings': ['terminal evidence mentions a zero-byte file'],
        'artifact_results': [{'path': row['path'], 'satisfied': False}] if artifact_failed else []}
    record_provenance_event(state, 'validation_recorded', {'attempt_id': 'attempt-1', 'validation': validation})
    assert provenance['validations']['warning-validation']['status'] == 'VALID_WITH_WARNINGS'
    accepted = _validated_runtime_realization(state, 'plan.result')
    assert accepted == ('' if artifact_failed else row['path'])
    assert (row['status'] == 'valid') is not artifact_failed

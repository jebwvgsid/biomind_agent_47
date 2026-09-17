"""S5: real reconciliation -> TypedExecute -> immutable cause -> stop/CLI.

Only the process leaf supplies a deterministic execution record. No scientific
execution, original session access, provider call or report publication.
"""
import asyncio
from copy import deepcopy

import pytest

from agent_core.cli_lifecycle import terminal_text
from agent_core.managed_artifacts import reconcile_runtime_outputs
from agent_core.workflow.execution import TypedExecute
from agent_core.workflow.inspection_stop import _execution, build_inspection_stop
from agent_core.workflow.managed_stop import build_managed_stop
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.stage_contracts import StageContractError
from tests.test_recovery_projection import _durable, _execute_failure


def rejected_execution():
    durable, source = _durable(), _execute_failure()
    record = source['payload']['execution_record']
    path = '/tmp/approved-input-check/vcf.samples.txt'
    contract = {'artifact_id': 'vcf-samples', 'semantic_handle': 'output.vcf_samples',
                'expected_path': path, 'artifact_kind': 'file', 'format': 'txt',
                'validation_contract_id': 'file_exists', 'validation_contract_version': 1,
                'required': True, 'constraint_origin': 'physical_hard'}
    observed = {'path': path, 'attempt_id': record['attempt_id'], 'current_attempt_owned': True,
                'artifact_kind': 'file', 'format': 'txt', 'content_valid': False, 'size': 0}
    rejection = reconcile_runtime_outputs([contract], [observed], attempt_id=record['attempt_id'], exit_code=0)
    assert rejection['status'] == 'recovery'
    assert rejection['violations'][0]['code'] == 'OBSERVED_ARTIFACT_CONTENT_INVALID'
    record.update(returncode=0, exit_code=0, failure_code=rejection['violations'][0]['code'],
                  runtime_output_reconciliation=rejection,
                  validation={'passed': False, 'errors': rejection['violations']})
    async def process_leaf(_state):
        return {'execution_evidence': deepcopy(record)}
    invocation = {**source['scope'], 'current_step_id': 'step-1', 'task_mode': 'managed'}
    typed = asyncio.run(TypedExecute(executor=process_leaf).run(invocation))['execute_outcome']
    causal = build_causal_failure_record('execute_outcome', typed, durable)
    return durable, typed, causal


def test_rc_zero_rejection_keeps_specific_cause_target_commit_and_delivery_separate():
    durable, typed, causal = rejected_execution()
    assert typed['type'] == 'execution_failed'  # Existing routing is compatible.
    assert 'exited successfully' in typed['summary'] and 'not committed' in typed['summary']
    packet = causal['failure_packet']
    assert packet['error_code'] == 'OBSERVED_ARTIFACT_CONTENT_INVALID'
    before = deepcopy(causal)
    seed = {'failure_packet': packet, 'seed_id': 'recovery-seed-' + causal['source_fingerprint'],
            'plan_spec_ref': {'plan_id': durable['plan_id'], 'plan_version': durable['plan_version']}}
    stop = build_inspection_stop(seed=seed, durable=durable, source=causal['failure_source'],
        episode={'terminal_error': {'code': 'RECOVERY_FIXED_CONTEXT_TOKEN_LIMIT',
                                   'detail': 'tokens=210000; safe=187712',
                                   'failure_stage': 'recovery.context_budget'},
                 'audit': {'turn_count': 0, 'tool_call_count': 0}})
    assert stop['execution']['returncode'] == 0
    assert stop['execution']['validation_status'] == 'failed'
    assert not stop['execution']['committed']
    assert any(row['source_pointer'].startswith('/execution_record/')
               for row in stop['execution']['validation_checks'])
    assert 'vcf.samples.txt' in str(typed['payload']['execution_record']['runtime_output_reconciliation'])
    stop.update(required_output_status='complete', prior_completed_work=[{'step_id': 'step-0'}])
    text = terminal_text('failed', stop)
    assert '进程退出码 0' in text and '必需产物内容未通过正式核验' in text
    assert '210000 tokens' in text and '187712 tokens' in text
    assert '文件交付：complete' in text and '已有 1 步正式提交' in text
    assert '脚本执行失败，退出码 0' not in text
    assert causal == before  # A read projection cannot rewrite historical facts.


@pytest.mark.parametrize('failure_code,error_code,validation,expected', [
    ('SPECIFIC_OUTPUT_ERROR', 'EXECUTION_FAILED', False, 'SPECIFIC_OUTPUT_ERROR'),
    ('', '', False, 'VALIDATION_FAILED'),
    ('', '', None, 'EXECUTION_FAILED'),
    ('', 'PROCESS_TIMEOUT', None, 'PROCESS_TIMEOUT'),
])
def test_causal_fallback_uses_specific_owner_code_then_explicit_validation(failure_code, error_code, validation, expected):
    source, durable = _execute_failure(), _durable()
    record = source['payload']['execution_record']
    record.update(failure_code=failure_code, error_code=error_code)
    if validation is not None:
        record['validation'] = {'passed': validation}
    result = build_causal_failure_record('execute_outcome', source, durable)
    assert result['failure_packet']['error_code'] == expected


def test_cross_attempt_specific_failure_is_rejected_not_consumed():
    source = _execute_failure()
    source['payload']['execution_record'].update(attempt_id='different-attempt', failure_code='FOREIGN')
    with pytest.raises(StageContractError, match='STAGE_SCOPE_MISMATCH'):
        build_causal_failure_record('execute_outcome', source, _durable())


def test_projection_ignores_foreign_attempt_index():
    durable, typed, causal = rejected_execution()
    source = deepcopy(causal['failure_source'])
    source['payload'].pop('execution_record', None)
    source['payload']['producer_failure_index']['scope']['attempt_id'] = 'other-attempt'
    scope = {**typed['scope'], 'attempt_id': 'attempt-1', 'step_id': 'step-1'}
    actual = _execution(durable, source, scope)
    assert actual['validation_status'] == 'unknown' and not actual['validation_checks']


def test_pre_dispatch_zero_has_explicit_scope_and_historical_char_units():
    durable, typed, causal = rejected_execution()
    durable['task_state'].update(active_failure_packet_ref=causal['packet_ref'],
        causal_failure_packets={causal['packet_ref']: causal})
    record = typed['payload']['execution_record']
    durable['provenance_state'] = {'attempts': {'attempt-1': {**record, 'status': 'EVIDENCE_COMMITTED'}}}
    boundary = {'node': 'recovery', 'status': 'failed', 'outcome': {'type': 'internal_protocol_error',
        'scope': typed['scope'], 'payload': {'error_code': 'RECOVERY_SEED_CAPACITY_EXCEEDED',
            'recovery_episode_facts': {'terminal_error': {'code': 'RECOVERY_SEED_CAPACITY_EXCEEDED',
                'detail': 'chars=32097; limit=32000', 'failure_stage': 'recovery.seed_admission',
                'dispatch_status': 'not_started'}}}}}
    stop = build_managed_stop(durable['task_id'], {}, durable, boundary)
    assert stop['recovery_usage'] == {'scope': 'current_admission', 'turn_count': 0, 'tool_call_count': 0}
    text = terminal_text('failed', stop)
    assert '32' in text and '32097 / 32000 字符' in text and '首次调用前' in text
    assert '不是全会话累计' in text
    assert stop['origin']['error_code'] == 'OBSERVED_ARTIFACT_CONTENT_INVALID'
    boundary['outcome']['payload']['recovery_episode_facts']['terminal_error'].pop('dispatch_status')
    unknown = build_managed_stop(durable['task_id'], {}, durable, boundary)
    assert unknown['recovery_usage']['scope'] == 'unknown'


@pytest.mark.parametrize('stage,nested', [('recovery_provider_admission', False), ('recovery_compactor_admission', True)])
def test_actual_dispatch_budget_reaches_cli_without_relabeling_origin(stage, nested):
    durable, typed, causal = rejected_execution()
    budget = {'failure_stage': stage, 'estimated_input_tokens': 187086,
        'safe_input_tokens': 179904, 'effective_output_limit_tokens': 16000,
        'exact_or_estimated': 'estimated', 'estimator_name': 'reference',
        'messages': ['private prompt body must not appear']}
    if nested:
        budget = {'effective_output_limit_tokens': 4096, 'capacity_feedback': budget,
                  'source_request_budget': {'estimated_input_tokens': 90000, 'safe_input_tokens': 191808}}
    stop = build_inspection_stop(seed={'failure_packet': causal['failure_packet'],
        'seed_id': 'recovery-seed-' + causal['source_fingerprint']}, durable=durable,
        source=causal['failure_source'], episode={'terminal_error': {
            'code': 'RECOVERY_PROVIDER_INPUT_UNSAFE', 'detail': 'bounded dispatch receipt',
            'failure_stage': stage, 'budget': budget}, 'audit': {'turn_count': 1, 'tool_call_count': 0}})
    assert 'private prompt' not in str(stop)
    text = terminal_text('failed', stop)
    assert stage in text and '187086 tokens' in text and '179904 tokens' in text
    assert '有效输出上限 16000 tokens' in text and '字符' not in text
    assert stop['origin']['error_code'] == 'OBSERVED_ARTIFACT_CONTENT_INVALID'
    assert stop['execution']['returncode'] == 0 and not stop['execution']['committed']

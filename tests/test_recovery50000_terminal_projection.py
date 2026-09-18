"""Frozen causal failures survive recovery stop and compact terminal display."""
import asyncio
from copy import deepcopy
import json

import pytest
from agent_core.cli_lifecycle import terminal_text
from agent_core.workflow.inspection_stop import build_inspection_stop, _failure_findings, _execution
from agent_core.workflow.managed_stop import build_managed_stop, projection
from agent_core.workflow.recovery_projection import build_causal_failure_record
from tests.test_recovery_projection import _durable, _verification_rejected, _execute_failure


def _stopped(durable, source, kind='verification_outcome', *, stop_code='RECOVERY_RESPONSE_INCOMPLETE', finish='length'):
    causal = build_causal_failure_record(kind, source, durable)
    durable['task_state'].update(active_failure_packet_ref=causal['packet_ref'],
        causal_failure_packets={causal['packet_ref']: causal})
    episode = {'terminal_error': {'code': stop_code, 'detail': finish,
                  'output_resubmission_exhausted': True},
        'audit': {'turn_count': 6, 'tool_call_count': 3, 'output_resubmission_count': 2,
                  'output_resubmission_exhausted': True},
        'reception': {'response_receipts': [{'error_code': stop_code, 'finish_reason': finish,
                       'status': 'not_accepted', 'directive_adopted': False}]}}
    boundary = {'node': 'recovery', 'status': 'failed', 'outcome': {'type': 'internal_protocol_error',
        'scope': source['scope'], 'payload': {'error_code': stop_code, 'recovery_episode_facts': episode}}}
    stop = build_managed_stop(durable['task_id'], {}, durable, boundary)
    return causal, stop, projection(stop)['terminal_diagnostic']


def _current_wgs_failure():
    durable, source = _durable(), _verification_rejected()
    source['scope']['attempt_id'] = 'attempt-1'
    scope = {**source['scope']}
    source['payload']['completion_report'] = {'scope': scope,
        'process_outcome': {'returncode': 0, 'execution_status': 'succeeded', 'validation_status': 'failed'},
        'predicate_results': [{'parser_id': 'biomind.genomics.reference_bundle.v2', 'passed': False,
             'reason_code': 'REFERENCE_DICTIONARY_URI_REGENERATION_REQUIRED',
             'parser_receipt': {'parser_id': 'biomind.genomics.reference_bundle.v2', 'value': False,
                 'error_code': 'REFERENCE_DICTIONARY_URI_REGENERATION_REQUIRED',
                 'detail': 'UR is a historical unescaped path; sequence_content_verified=true'}}],
        'binding_and_receipt_findings': [{'status': 'failed', 'code': 'REFERENCE_EFFECT_INPUT_MISMATCH',
            'message': 'create_reference_indexes references another input',
            'affected_field': 'explicit_side_effects.create_reference_indexes.reference_resource_ref'}]}
    source['payload']['contract_violations'] = []
    # This is a previous attempt, not the current source. No arbitrary scan of
    # a task's earlier errors may turn the resolved Picard problem into current.
    durable['provenance_state'] = {'attempts': {'old-attempt': {'step_id': 'step-1', 'returncode': 137,
        'stderr': 'Picard rejects -R/-O; possible OOM'}}}
    return durable, source


def test_same_causal_chain_keeps_current_dictionary_and_effect_errors_separate_from_length():
    durable, source = _current_wgs_failure()
    before = deepcopy(source)
    causal, stop, projected = _stopped(durable, source)
    text = terminal_text('failed', projected)
    assert '进程退出码 0' in text
    assert 'REFERENCE_DICTIONARY_URI_REGENERATION_REQUIRED' in text
    assert 'UR is a historical unescaped path' in text
    assert 'biomind.genomics.reference_bundle.v2' in text
    assert 'REFERENCE_EFFECT_INPUT_MISMATCH' in text and 'another input' in text
    assert '单次输出达到上限' in text and '完整重提 2 次' in text and '额度已耗尽' in text
    assert '尚未定位' not in text and '137' not in text and 'OOM' not in text and 'Picard rejects' not in text
    assert '本次调查：6 次模型请求，3 次工具调用；不是全会话累计' in text
    assert stop['origin']['error_code'] == causal['failure_packet']['error_code']
    assert source == before
    compact = causal['failure_source']['payload']['completion_report']['failed_predicates'][0]
    assert compact['parser_receipt']['detail'].startswith('UR is a historical')


def test_real_parser_rejection_flows_through_actual_verifier_causal_and_terminal(tmp_path, monkeypatch):
    from agent_core.plan_tracker import load_plan_state
    import tests.test_unified_reference_bundle as fixture
    task_id, bundle = fixture._setup(tmp_path)
    original = fixture._write_damage
    def damage(root, ignored):
        original(root, None)
        dictionary = root / 'reference.dict'
        dictionary.write_bytes(dictionary.read_bytes().replace(b'\tUR:file:', b'\tUR:file:\xd5\xff', 1))
    monkeypatch.setattr(fixture, '_write_damage', damage)
    verified = asyncio.run(fixture._verify(tmp_path, task_id, bundle))
    assert verified['verification_outcome']['type'] not in {'verified', 'verified_with_warnings'}
    durable = load_plan_state(tmp_path, task_id)
    causal, stop, projected = _stopped(durable, verified['verification_outcome'])
    text = terminal_text('failed', projected)
    assert 'REFERENCE_DICTIONARY_URI_ENCODING_INVALID' in text
    assert 'biomind.genomics.reference_bundle.v1' in text
    assert any(row['detail'] for row in projected['unresolved_findings']
               if row['code'] == 'REFERENCE_DICTIONARY_URI_ENCODING_INVALID')
    assert '尚未定位' not in text
    assert not stop['execution']['committed']


def test_unknown_validator_code_and_original_source_survive_to_terminal():
    durable, source = _durable(), _verification_rejected()
    source['scope']['attempt_id'] = 'attempt-1'
    source['payload']['contract_violations'] = [{'code': 'NEW_UNREGISTERED_CHECK',
        'detail': 'Actual new invariant rejected this candidate', 'validation_source': 'custom.current.validator',
        'field_path': '$.candidate.input', 'expected': 'explicit input binding'}]
    _, _, projected = _stopped(durable, source, stop_code='RECOVERY_RESOURCE_LIMIT', finish='total_requests')
    text = terminal_text('failed', projected)
    assert 'NEW_UNREGISTERED_CHECK' in text and 'Actual new invariant' in text
    assert 'custom.current.validator' in text and '$.candidate.input' in text
    assert '产物路径引用非法' not in text and '单次输出达到上限' not in text


def test_foreign_attempt_checks_and_materializations_are_not_current():
    durable, source = _current_wgs_failure()
    packet = {'diagnostic_facts': []}
    scope = source['scope']
    payload = deepcopy(source['payload'])
    payload['completion_report']['scope']['attempt_id'] = 'foreign'
    payload['producer_failure_index'] = {'scope': {'attempt_id': 'foreign'},
                                        'checks': [{'code': 'FOREIGN_INDEX', 'detail': 'not current'}]}
    assert not _failure_findings(payload, packet, scope)
    record = {**scope, 'attempt_id': 'foreign', 'returncode': 137,
        'observed_output_records': [{'attempt_id': 'foreign', 'current_attempt_owned': True,
                                    'path': '/tmp/not-current', 'exists': True}]}
    result = _execution(durable, {'payload': {'execution_record': record}}, scope)
    assert result['returncode'] is None and not result['candidate_outputs']


def test_candidate_presence_commit_count_and_delivery_conflict_remain_distinct():
    durable, source = _durable(), _execute_failure()
    record = source['payload']['execution_record']
    record.update(returncode=0, failure_code='VALIDATION_FAILED', validation={'passed': False, 'errors': [
        {'code': 'REFERENCE_CONTENT_REJECTED', 'message': 'dictionary needs regeneration'}]},
        observed_output_records=[{'path': '/tmp/current/reference.dict', 'attempt_id': 'attempt-1',
            'current_attempt_owned': True, 'exists': True, 'content_valid': False, 'size': 128}])
    # Reuse the existing authoritative receipt-to-semantic-consumption pairing.
    durable['task_state'].update(typed_step_commit_receipts={'c0': {'outcome': {'payload': {
        'completed_step_id': 'step-0', 'commit_id': 'c0', 'receipt': 'typed-step-commit:c0',
        'semantic_consumption_ref': 's0'}}}}, semantic_completion_consumptions={'s0': {
        'commit_id': 'c0', 'scope': {k: durable[k] for k in ('task_id', 'plan_id', 'plan_version')}}})
    _, stop, _ = _stopped(durable, source, 'execute_outcome')
    stop.update(required_output_status='conflict',
        report_receipts={'report.md': {'status': 'failed', 'reason': 'RUNTIME_REPORT_EXISTING_FILE_PROTECTED'}},
        report_paths={'report.md': '/tmp/old/report.md'},
        internal_diagnostic_delivery={'status': 'complete', 'paths': {'report.md': '/tmp/current/diagnostic/report.md'},
             'receipts': {'report.md': {'status': 'created', 'sha256': 'a' * 64}}})
    text = terminal_text('failed', projection(stop)['terminal_diagnostic'])
    assert '已有 1 步正式提交' in text and '尚未正式提交' in text
    assert '本次候选产物：1 项' in text and '内容检查未通过' in text
    assert '文件交付：conflict' in text and '内部诊断归档=complete' in text
    assert '/tmp/current/diagnostic/report.md' in text and '/tmp/old/report.md' not in text


@pytest.mark.parametrize('finish', ['stop', 'UNKNOWN', 'content_filter', 'network_interrupted'])
def test_incomplete_without_length_is_not_relabelled_output_exhaustion(finish):
    text = terminal_text('failed', {'origin': {'error_code': 'CHECK_FAILED', 'summary': 'Known check reason'},
        'stop_reason': {'error_code': 'RECOVERY_RESPONSE_INCOMPLETE', 'finish_reason': finish}})
    assert 'Known check reason' in text and '单次输出达到上限' not in text and '额度已耗尽' not in text

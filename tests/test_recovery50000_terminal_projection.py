"""Frozen causal failures survive recovery stop and compact terminal display."""
import asyncio
from copy import deepcopy
import json

import pytest
from agent_core.cli_lifecycle import terminal_text
from agent_core.workflow.inspection_stop import build_inspection_stop, _failure_findings, _execution
from agent_core.terminal_facts import projection
from agent_core.workflow.recovery_projection import build_causal_failure_record
from tests.test_recovery_projection import _durable, _verification_rejected, _execute_failure




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




@pytest.mark.parametrize('finish', ['stop', 'UNKNOWN', 'content_filter', 'network_interrupted'])
def test_incomplete_without_length_is_not_relabelled_output_exhaustion(finish):
    text = terminal_text('failed', {'origin': {'error_code': 'CHECK_FAILED', 'summary': 'Known check reason'},
        'stop_reason': {'error_code': 'RECOVERY_RESPONSE_INCOMPLETE', 'finish_reason': finish}})
    assert 'Known check reason' in text and '单次输出达到上限' not in text and '额度已耗尽' not in text

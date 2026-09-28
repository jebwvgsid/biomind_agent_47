"""Offline real-publication replay and durable Recovery admission regression."""
import copy
import json
from pathlib import Path
import unittest

from agent_core.workflow.recovery_projection import (
    build_causal_failure_record, inherited_verification_cause,
    verification_causal_signature,
)
from tests.test_recovery_projection import _durable, _execute_failure, _verification_rejected


class VerificationPublicationTests(unittest.TestCase):
    def test_two_real_report_generation_pairs_inherit_trusted_parent(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/plan_generate_20260919/historical_verification_pairs.json').read_text())
        for before, after in zip(fixture['records'][::2], fixture['records'][1::2]):
            durable = {**fixture['scope'], **{k: before['failure_packet']['plan_spec_ref'][k] for k in ('plan_id', 'plan_version', 'plan_revision')}, 'task_state': {'causal_failure_packets': {before['packet_ref']: before}}}
            cause = inherited_verification_cause('completion_gate_outcome', after['failure_source'], durable)
            self.assertEqual(cause['incident_id'], before['incident_id'])
            self.assertEqual(cause['causal_parent_packet_ref'], before['packet_ref'])
            self.assertEqual(verification_causal_signature('completion_gate_outcome', before['failure_source']),
                             verification_causal_signature('completion_gate_outcome', after['failure_source']))
            changed = copy.deepcopy(after['failure_source'])
            changed['scope']['attempt_id'] = 'new-execution-attempt'
            changed['payload']['completion_report']['scope']['attempt_id'] = 'new-execution-attempt'
            self.assertEqual(inherited_verification_cause('completion_gate_outcome', changed, durable), {})
            changed = copy.deepcopy(after['failure_source'])
            changed['payload']['completion_report']['validation_digest'] = 'sha256:new-factual-validation'
            self.assertEqual(inherited_verification_cause('completion_gate_outcome', changed, durable), {})

    def test_typed_producer_record_keeps_original_source_and_cumulative_incident(self):
        durable = _durable()
        source = _verification_rejected()
        source['scope'].update(action_id='action-1', attempt_id='attempt-1', state_generation=4)
        source['payload']['completion_report'] = {
            'report_id': 'report-first', 'scope': copy.deepcopy(source['scope']),
            'validation_digest': 'sha256:validation-facts', 'effective_disposition': 'rejected',
        }
        first = build_causal_failure_record('verification_outcome', source, durable)
        durable['task_state']['causal_failure_packets'] = {first['packet_ref']: first}
        durable['task_state']['typed_recovery_budget'] = {first['incident_id']: 2}
        second_source = copy.deepcopy(source)
        second_source['scope']['state_generation'] = 9
        second_source['payload']['completion_report']['scope']['state_generation'] = 9
        second_source['payload']['completion_report']['report_id'] = 'report-new-publication'
        second = build_causal_failure_record('verification_outcome', second_source, durable,
            packet_version=2, supersedes_packet_ref=first['packet_ref'])
        self.assertEqual(first['incident_id'], second['incident_id'])
        self.assertNotEqual(first['source_fingerprint'], second['source_fingerprint'])
        self.assertEqual(second['failure_source']['payload']['completion_report']['report_id'], 'report-new-publication')
        self.assertEqual(durable['task_state']['typed_recovery_budget'][second['incident_id']], 2)
        second_source['payload']['completion_report']['validation_digest'] = 'sha256:new-validation'
        third = build_causal_failure_record('verification_outcome', second_source, durable)
        self.assertNotEqual(first['incident_id'], third['incident_id'])

    def test_other_real_failure_producers_do_not_enter_report_dedup(self):
        source = _execute_failure()
        self.assertEqual(verification_causal_signature('execute_outcome', source), '')
        first = build_causal_failure_record('execute_outcome', source, _durable())
        source['payload']['failure_fingerprint'] = 'failure-new-attempt'
        source['payload']['execution_record'].pop('failure_fingerprint')
        source['scope']['attempt_id'] = 'new-attempt'
        source['payload']['attempt_id'] = 'new-attempt'
        source['payload']['execution_record']['attempt_id'] = 'new-attempt'
        second = build_causal_failure_record('execute_outcome', source, _durable())
        self.assertNotEqual(first['incident_id'], second['incident_id'])


    def test_new_admission_and_tool_failures_keep_their_producer_identity(self):
        from agent_core.workflow.outcomes import ExecuteOutcome, InstallSubagentOutcome
        scope = _execute_failure()['scope']
        for kind, make in (
            ('execute_outcome', lambda code: ExecuteOutcome(schema_version=1,
                type='pre_execution_rejected', summary='A new dependency admission failed.', scope=scope,
                payload={'violation_owner': 'DEPENDENCY', 'violation_code': code,
                         'candidate_fingerprint': 'candidate-1', 'command_started': False,
                         'budget_receipt': {}}).to_dict()),
            ('install_subagent_outcome', lambda code: InstallSubagentOutcome(schema_version=1,
                type='installation_failed', summary='Tool capability validation failed.', scope=scope,
                payload={'status': 'failed', 'error_code': code, 'evidence': {}}).to_dict()),
        ):
            first = build_causal_failure_record(kind, make('FIRST_FAILURE'), _durable())
            durable = _durable()
            durable['task_state']['causal_failure_packets'] = {first['packet_ref']: first}
            second = build_causal_failure_record(kind, make('NEW_FAILURE'), durable)
            self.assertNotEqual(first['incident_id'], second['incident_id'])




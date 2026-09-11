"""Failure-event identity at the real packet publisher and inspection reader."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.plan_contract import StateCASConflict
from agent_core.workflow.inspection_evidence import InspectionEvidenceError, InspectionObjectReader, inspection_scope
from agent_core.workflow.managed_stop import build_managed_stop
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, project_recovery_seed
from tests.test_recovery_projection import _durable, _execute_failure


class Store:
    def __init__(self, root):
        self.root = root
        self.value = _durable()
        self.path = root / 'task_outputs/task-recovery/plan_state.json'
        self.path.parent.mkdir(parents=True)
        self.value['provenance_state'] = {'attempts': {}}
        self.save('', '', self.value)

    def load(self, *_):
        return json.loads(self.path.read_text())

    def save(self, _root, _task, value):
        self.value = deepcopy(value)
        self.path.write_text(json.dumps(value))

    def attempt(self, ordinal, rc):
        source = _execute_failure()
        aid, act = f'attempt-{ordinal}', f'action-{ordinal}'
        source['scope'].update(attempt_id=aid, action_id=act)
        source['payload'].update(attempt_id=aid, action_id=act, return_code=rc,
            incident_id='incident-fixture', failure_fingerprint=f'failure-{ordinal}')
        record = source['payload']['execution_record']
        record.update(attempt_id=aid, action_id=act, returncode=rc, failure_fingerprint=f'failure-{ordinal}',
            code='printf fixture', raw_stderr='syntax error' if rc else '', status='EVIDENCE_COMMITTED')
        self.value['provenance_state']['attempts'][aid] = deepcopy(record)
        self.save('', '', self.value)
        return source

    def binder(self):
        return CausalFailurePacketBinder(data_path=str(self.root), loader=self.load, saver=self.save)


def test_retry_publication_uses_new_attempt_with_stale_verification_pointer(tmp_path):
    store = Store(tmp_path)
    first = store.binder().bind({'task_id':'task-recovery'}, 'execute_outcome', store.attempt(1, 17))
    source = store.attempt(2, 2)
    task = store.value['task_state']
    store.value['steps'][1]['verification_attempt_id'] = 'attempt-1'
    task['verification_pending'] = {'attempt_id':'attempt-1'}
    task['recovery_state'] = {'active_recovery': {'recovery_id':'decision', 'envelope_id':'envelope'}}
    task['typed_recovery_receipts'] = {'decision': {
        'directive': {'action':'retry_step'}, 'execution_envelope_digest':'envelope',
        'execution_envelope': {'action_id':'action-2', 'step_id':'step-1'},
    }}
    store.save('', '', store.value)
    second = store.binder().bind({'task_id':'task-recovery'}, 'execute_outcome', source)
    projection = project_recovery_seed(second, store.load())
    assert projection.failure_packet.attempt_ref == 'attempt-2'
    entries = store.value['task_state']['inspection_ref_registry']
    packet = projection.failure_packet
    reader = InspectionObjectReader(str(tmp_path), scope=inspection_scope(packet, projection.recovery_seed.seed_id))
    execution = next(entries[r] for r in packet.inspection_refs if r in entries and entries[r]['scope']['object_kind']=='execution')
    body = reader.read_body(execution, execution['locator'])
    assert 'attempt-2' in json.dumps(body) and 'syntax error' in json.dumps(body)
    old_ref = next(r for r in first['failure_packet']['inspection_refs'] if r in entries)
    with pytest.raises(InspectionEvidenceError):
        reader.read_body(entries[old_ref], entries[old_ref]['locator'])
    assert store.value['steps'][1]['verification_attempt_id'] == 'attempt-1'
    assert not any(x.get('kind')=='inspection_publication_failure' for x in packet.diagnostic_facts)


def test_bad_inspection_scope_keeps_direct_failure_and_explains_unavailable(tmp_path):
    store = Store(tmp_path)
    source = store.attempt(2, 2)
    store.value['steps'][1]['verification_attempt_id'] = 'attempt-1'
    store.save('', '', store.value)
    result = store.binder().bind({'task_id':'task-recovery'}, 'execute_outcome', source)
    packet = result['failure_packet']
    assert packet['attempt_ref'] == 'attempt-2'
    failures = [x for x in packet['diagnostic_facts'] if x['kind']=='inspection_publication_failure']
    assert failures[0]['metadata']['code']=='INSPECTION_REF_SCOPE_MISMATCH'
    assert failures[0]['metadata']['evidence_used'] is False
    assert 'return_code' in str(packet['diagnostic_facts'])


def test_candidate_stop_does_not_inherit_previous_attempt_or_rc(tmp_path):
    store = Store(tmp_path)
    store.binder().bind({'task_id':'task-recovery'}, 'execute_outcome', store.attempt(1, 17))
    boundary = {'node':'generate', 'status':'blocked', 'outcome': {
        'type':'blocked', 'summary':'Invalid candidate syntax',
        'payload':{'reason_code':'GENERATE_SCRIPT_BLOCK_INVALID'}}}
    stop = build_managed_stop('task-recovery', {}, store.value, boundary)
    assert stop['scope']['attempt_id']==''
    assert stop['execution']['executed'] is False
    assert stop['execution']['returncode'] is None
    assert stop['origin']['attempt_id']==''


def test_cancel_is_not_caught_by_publication_diagnostics(tmp_path):
    import asyncio
    store=Store(tmp_path); source=store.attempt(1, 2)
    with patch('agent_core.workflow.recovery_projection.prepare_inspection_evidence', side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            store.binder().bind({'task_id':'task-recovery'}, 'execute_outcome', source)


def test_cas_conflict_does_not_overwrite_new_durable_failure(tmp_path):
    store=Store(tmp_path); source=store.attempt(1, 2)
    before=store.path.read_bytes()
    def conflict(*_): raise StateCASConflict('synthetic conflict')
    binder=CausalFailurePacketBinder(data_path=str(tmp_path), loader=store.load, saver=conflict)
    with pytest.raises(StateCASConflict):binder.bind({'task_id':'task-recovery'},'execute_outcome',source)
    assert store.path.read_bytes()==before

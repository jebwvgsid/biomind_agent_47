"""INSP-01 controls use real PlanTracker CAS, binder, store and dispatcher."""
from __future__ import annotations

import copy
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.plan_tracker import (
    build_plan_state, load_plan_state, save_plan_state,
    mark_step_verification_pending, record_provenance_event,
)
from agent_core.plan_contract import StateCASConflict
from agent_core.recovery import ValidationEvidence
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow.inspection_evidence import (
    INSPECTION_BACKEND, InspectionEvidenceError, InspectionObjectReader,
    inspection_scope,
)
from agent_core.workflow.outcomes import VerificationOutcome
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, project_recovery_seed
from agent_core.workflow.recovery_tools import (
    RecoveryLocalToolDispatcher, RecoveryToolError, build_recovery_inspection_registry,
)
from tests.test_recovery_projection import _durable


def create_inspection_case(root):
    """Synthetic input DTO, actual producer handoff and all persistence layers."""
    durable = build_plan_state("task-recovery", "- [ ] Prepare input\n- [ ] Run analysis")
    durable.update(_durable())
    durable['state_generation'] = 0
    durable['steps'][1]['status'] = 'verification_pending'
    durable['steps'][1]['verification_attempt_id'] = 'attempt-1'
    durable['task_state']['verification_pending'] = {'step_id':'step-1','attempt_id':'attempt-1'}
    save_plan_state(str(root), 'task-recovery', durable, allow_canonical_task_write=True)
    durable = load_plan_state(str(root), 'task-recovery')
    scope = {'task_id':'task-recovery','plan_id':durable['plan_id'],
             'plan_version':durable['plan_version'],'step_id':'step-1',
             'attempt_id':'attempt-1','action_id':'action-1',
             'validation_id':'validation-1','state_generation':durable['state_generation']}
    validation = ValidationEvidence(
        task_id='task-recovery',step_id='step-1',attempt_id='attempt-1',action_id='action-1',
        validation_id='validation-1',status='VALID',evidence_complete=True,
        returncode_check={'returncode':0,'passed':True},
        scientific_checks=[{'validator':'independent_contig_parser','passed':True,'receipt':{'contigs_compatible':True}}],
        llm_advisory={'assessment':'CONCERN','concerns':['approved method requires inspection']},
        llm_advisory_conflict=True,
    ).to_dict()
    report={'schema_version':1,'report_id':'completion-report-fixture','scope':scope,
            'effective_disposition':'inspection_required','deterministic_veto':False,
            'process_outcome':{'returncode':0,'validation_status':'VALID'}}
    decision={'schema_version':1,'decision_id':'semantic-decision-fixture','scope':scope,
              'assessment':'CONCERN','disposition':'inspection_required','reviewed_report_id':report['report_id'],
              'concerns':['approved method requires inspection']}
    source=VerificationOutcome(schema_version=1,type='inspection_required',summary='Method concern',scope=scope,
        payload={'failure_owner':'semantic_review','validation_evidence':validation,'completion_report':report,
                 'semantic_decision':decision,'contract_violations':['method requires inspection'],
                 'completion_gate':{'passed':False}},
        evidence_refs=('validation-1',report['report_id'],decision['decision_id'])).to_dict()
    execution={'schema_version':1,'task_id':'task-recovery','step_id':'step-1','attempt_id':'attempt-1',
               'action_id':'action-1','returncode':0,'status':'success'}
    state={'task_id':'task-recovery','execution_evidence':execution}
    return source,state


def publish(root,source,state):
    update=CausalFailurePacketBinder(data_path=str(root)).bind(state,'verification_outcome',source)
    return update, reopen(root)


def reopen(root):
    durable=load_plan_state(str(root),'task-recovery')
    seed=project_recovery_seed({'task_id':'task-recovery'},durable).recovery_seed
    store=PhaseArchiveStore(root)
    registry=build_recovery_inspection_registry(seed,durable,phase_store=store)
    dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=store)
    return durable,seed,registry,dispatcher


def typed_rows(registry):
    return [r for r in registry.provider_manifest() if r['backend']==INSPECTION_BACKEND]


def test_I02_I03_I04_I10_real_handoff_reload_and_body(tmp_path):
    source,state=create_inspection_case(tmp_path)
    _,(_,seed,registry,dispatcher)=publish(tmp_path,source,state)
    rows=typed_rows(registry)
    assert len(rows)==4 and all(row['readable'] for row in rows)
    assert not any(row['kind']=='phase_archive' for row in registry.provider_manifest())
    assert dispatcher.dispatch_full('failure.inspect',{'ref_id':seed.failure_packet.source_outcome_ref})['failure_packet']['failure_type']=='inspection_required'
    found={row['scope']['object_kind']:json.loads(dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})['content']) for row in rows}
    assert found['validation']==source['payload']['validation_evidence']
    assert found['completion_report']==source['payload']['completion_report']
    assert found['semantic_decision']==source['payload']['semantic_decision']
    assert found['step_attempt']['execution_record']['returncode']==0
    assert found['step_attempt']['commit_status']=='not_committed'
    assert found['validation']['scientific_checks'][0]['receipt']['contigs_compatible'] is True


def test_I05_idempotent_bind_no_new_objects_or_generation(tmp_path):
    source,state=create_inspection_case(tmp_path)
    first,(durable,_,registry,_)=publish(tmp_path,source,state)
    files=sorted(p.name for p in (tmp_path/'task_outputs/task-recovery/state_contract/objects').glob('*.json'))
    second,(again,_,reg2,_)=publish(tmp_path,source,state)
    assert first==second
    assert durable['state_generation']==again['state_generation']
    assert typed_rows(registry)==typed_rows(reg2)
    assert files==sorted(p.name for p in (tmp_path/'task_outputs/task-recovery/state_contract/objects').glob('*.json'))


def test_I06_crash_between_objects_and_CAS_then_recover(tmp_path):
    source,state=create_inspection_case(tmp_path)
    def crash(*args):raise OSError('synthetic crash before index CAS')
    with pytest.raises(OSError,match='crash'):
        CausalFailurePacketBinder(data_path=str(tmp_path),saver=crash).bind(state,'verification_outcome',source)
    durable=load_plan_state(str(tmp_path),'task-recovery')
    assert not durable['task_state'].get('inspection_ref_registry')
    assert not durable['task_state'].get('active_failure_packet_ref')
    before=set((tmp_path/'task_outputs/task-recovery/state_contract/objects').glob('*.json'))
    _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    assert len(typed_rows(registry))==4
    assert before.issubset(set((tmp_path/'task_outputs/task-recovery/state_contract/objects').glob('*.json')))
    for row in typed_rows(registry):assert dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})['content']


def test_I07_partial_object_write_failure_is_unavailable_with_cause(tmp_path):
    source,state=create_inspection_case(tmp_path)
    real_put=StateContractStore.put_object
    def fail_one(self,kind,body):
        if kind=='recovery_inspection.validation':raise PermissionError('synthetic validation storage denied')
        return real_put(self,kind,body)
    with patch.object(StateContractStore,'put_object',fail_one):
        _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    rows=typed_rows(registry)
    bad=next(r for r in rows if r['scope']['object_kind']=='validation')
    assert not bad['readable'] and bad['unavailable_reason']=='INSPECTION_OBJECT_PERSIST_FAILED'
    assert 'storage denied' in bad['scope']['unavailable_detail']
    assert sum(bool(r['readable']) for r in rows)==3
    with pytest.raises(InspectionEvidenceError,match='NOT_RESOLVABLE'):
        dispatcher.dispatch_full('evidence.read',{'evidence_ref':bad['ref_id']})


def test_I08_I09_I16_raw_ids_rejected_manifest_has_lawful_hints(tmp_path):
    source,state=create_inspection_case(tmp_path)
    _,(_,seed,registry,dispatcher)=publish(tmp_path,source,state)
    with patch.object(dispatcher.inspection_reader,'read',side_effect=AssertionError('reader must not run')):
        for tool,ref in [('failure.inspect',seed.failure_packet.failure_id),('step.inspect','step-1')]:
            with pytest.raises(RecoveryToolError,match='INSPECTION_REF_NOT_DECLARED') as exc:
                dispatcher.dispatch_full(tool,{'ref_id':ref})
            assert 'declared_readable_candidates=' in exc.value.detail
        with pytest.raises(RecoveryToolError,match='INSPECTION_REF_NOT_RESOLVABLE'):
            dispatcher.dispatch_full('evidence.read',{'evidence_ref':'validation-1'})
    for row in typed_rows(registry):
        assert row['source_object_id']!=row['ref_id']
        assert row['allowed_tools']==['evidence.read']
    old=next(r for r in registry.provider_manifest() if r['ref_id']=='validation-1')
    assert not old['readable'] and old['unavailable_reason']


@pytest.mark.parametrize('field',['task_id','plan_id','plan_version','step_id','attempt_id','episode_id'])
def test_I11_scope_rejected_before_body_read(tmp_path,field):
    source,state=create_inspection_case(tmp_path)
    _,(_,seed,registry,dispatcher)=publish(tmp_path,source,state)
    row=copy.deepcopy(typed_rows(registry)[0]);row['scope'][field]='wrong'
    locator=dict(registry._locators[row['ref_id']])
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('body must not read')):
        with pytest.raises(InspectionEvidenceError,match='SCOPE_MISMATCH'):
            dispatcher.inspection_reader.read(row,locator)


@pytest.mark.parametrize('change',['digest','revision','revision_bool','type','locator','backend','remote_locator'])
def test_I12_I13_descriptor_and_locator_rejected_before_read(tmp_path,change):
    source,state=create_inspection_case(tmp_path)
    _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    row=copy.deepcopy(typed_rows(registry)[0]);locator=dict(registry._locators[row['ref_id']])
    if change=='digest':row['digest']='sha256:'+'0'*64
    elif change=='revision':row['scope']['object_revision']=2
    elif change=='revision_bool':row['scope']['object_revision']=True
    elif change=='type':row['scope']['object_kind']='arbitrary_global_state'
    elif change=='locator':locator['object_ref']='../../outside'
    elif change=='backend':row['backend']='unapproved_remote'
    else:locator['url']='https://forbidden.example/body'
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('body must not read')):
        with pytest.raises(InspectionEvidenceError):dispatcher.inspection_reader.read(row,locator)


def test_I12_tampered_body_digest_rejected(tmp_path):
    source,state=create_inspection_case(tmp_path)
    _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    row=typed_rows(registry)[0];ref=registry._locators[row['ref_id']]['object_ref']
    file=tmp_path/'task_outputs/task-recovery/state_contract/objects'/ (ref.rsplit(':',1)[-1]+'.json')
    data=json.loads(file.read_text());data['payload']['body']['forged']=True;file.write_text(json.dumps(data))
    with pytest.raises(InspectionEvidenceError,match='INTEGRITY_ERROR'):
        dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})


def test_I13_parent_symlink_denied_before_body(tmp_path):
    source,state=create_inspection_case(tmp_path)
    _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    objects=tmp_path/'task_outputs/task-recovery/state_contract/objects'
    real=objects.with_name('untrusted_objects');objects.rename(real);objects.symlink_to(real,target_is_directory=True)
    row=typed_rows(registry)[0]
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('body must not read')):
        with pytest.raises(InspectionEvidenceError,match='PATH_DENIED'):
            dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})


def test_I14_bounded_pages_keep_identity_and_content(tmp_path):
    source,state=create_inspection_case(tmp_path)
    source['payload']['validation_evidence']['info']=['unique-line-'+str(i) for i in range(220)]
    _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    row=next(r for r in typed_rows(registry) if r['scope']['object_kind']=='validation')
    first=dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id'],'start_line':1,'end_line':7})
    second=dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id'],'start_line':8,'end_line':14})
    assert first['truncated'] and first['cursor']=={'next_start_line':8}
    assert second['start_line']==8 and first['digest']==second['digest']==row['digest']
    assert first['source_object_id']==second['source_object_id']
    assert first['content']!=second['content']
    assert len(first['content'])<=16000


def test_I15_real_reader_oserror_preserves_error_type_and_detail(tmp_path):
    source,state=create_inspection_case(tmp_path)
    _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    row=typed_rows(registry)[0]
    with patch.object(StateContractStore,'read_object',side_effect=OSError('synthetic device unavailable')):
        with pytest.raises(InspectionEvidenceError) as exc:
            dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})
    assert exc.value.code=='INSPECTION_OBJECT_IO_ERROR'
    assert 'OSError: synthetic device unavailable' in exc.value.detail


def test_I06_actual_CAS_conflict_keeps_inspection_index_unpublished(tmp_path):
    source,state=create_inspection_case(tmp_path)
    def concurrent_save(data_path,task_id,candidate):
        concurrent=load_plan_state(data_path,task_id)
        concurrent['task_state']['unrelated_note']='newer authoritative save'
        save_plan_state(data_path,task_id,concurrent)
        save_plan_state(data_path,task_id,candidate)
    with pytest.raises(StateCASConflict):
        CausalFailurePacketBinder(data_path=str(tmp_path),saver=concurrent_save).bind(state,'verification_outcome',source)
    durable=load_plan_state(str(tmp_path),'task-recovery')
    assert durable['task_state']['unrelated_note']=='newer authoritative save'
    assert not durable['task_state'].get('inspection_ref_registry')
    assert not durable['task_state'].get('active_failure_packet_ref')


@pytest.mark.parametrize('field',['task_id','plan_id','plan_version','step_id','attempt_id','episode_id'])
def test_I11_new_registry_rejects_tampered_durable_scope(tmp_path,field):
    source,state=create_inspection_case(tmp_path)
    _,(durable,seed,registry,_)=publish(tmp_path,source,state)
    row=typed_rows(registry)[0]
    durable['task_state']['inspection_ref_registry'][row['ref_id']]['scope'][field]='wrong'
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('body must not read')):
        with pytest.raises(InspectionEvidenceError,match='SCOPE_MISMATCH'):
            build_recovery_inspection_registry(seed,durable,phase_store=PhaseArchiveStore(tmp_path))


def test_I07_missing_historical_body_and_publication_failure_reach_packet(tmp_path):
    source,state=create_inspection_case(tmp_path)
    source['payload']['validation_evidence']={}
    update,(_,seed,registry,_)=publish(tmp_path,source,state)
    failures=update['failure_source']['payload']['inspection_publication_failures']
    assert failures[0]['code']=='HISTORICAL_BODY_UNAVAILABLE'
    assert failures[0]['object_kind']=='validation'
    facts=[dict(f) for f in seed.failure_packet.diagnostic_facts if f['kind']=='inspection_publication_failure']
    assert facts[0]['metadata']['code']=='HISTORICAL_BODY_UNAVAILABLE'
    assert sum(r['readable'] for r in typed_rows(registry))==3


def test_I13_symlink_above_data_root_denied_before_body(tmp_path):
    actual=tmp_path/'actual';actual.mkdir()
    source,state=create_inspection_case(actual)
    _,(_,seed,registry,_)=publish(actual,source,state)
    linked=tmp_path/'linked';linked.symlink_to(actual,target_is_directory=True)
    reader=InspectionObjectReader(str(linked),scope=inspection_scope(seed.failure_packet,seed.seed_id))
    row=typed_rows(registry)[0];locator=registry._locators[row['ref_id']]
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('outside body must not read')):
        with pytest.raises(InspectionEvidenceError,match='PATH_DENIED'):
            reader.read(row,locator)


def test_I14_long_single_line_never_reports_complete_or_skips_tail(tmp_path):
    source,state=create_inspection_case(tmp_path)
    source['payload']['validation_evidence']['info']=['x'*18000]
    _,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    row=next(r for r in typed_rows(registry) if r['scope']['object_kind']=='validation')
    result=dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})
    assert result['truncated']
    next_line=result['cursor']['next_start_line']
    with pytest.raises(InspectionEvidenceError,match='LINE_REQUIRES_NARROWING'):
        dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id'],'start_line':next_line})


def test_I07_I15_safe_error_details_do_not_publish_absolute_paths(tmp_path):
    source,state=create_inspection_case(tmp_path)
    real_put=StateContractStore.put_object
    def fail_one(self,kind,body):
        if kind=='recovery_inspection.validation':raise PermissionError('denied /private/secret/object.json')
        return real_put(self,kind,body)
    with patch.object(StateContractStore,'put_object',fail_one):
        update,(_,_,registry,dispatcher)=publish(tmp_path,source,state)
    failure=update['failure_source']['payload']['inspection_publication_failures'][0]
    assert '/private' not in failure['detail'] and '[path]' in failure['detail']
    row=next(r for r in typed_rows(registry) if r['readable'])
    with patch.object(StateContractStore,'read_object',side_effect=OSError('missing /private/other/evidence.json')):
        with pytest.raises(InspectionEvidenceError) as exc:
            dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})
    assert '/private' not in exc.value.detail and '[path]' in exc.value.detail


def _real_handoff_reader(root):
    from tests.test_inspection_workflow import make_inspection_fixture
    fixture=make_inspection_fixture(root,resolve_second=False)
    asyncio.run(fixture.inspect_handoff())
    durable=fixture.reload()
    seed=project_recovery_seed(fixture.state,durable).recovery_seed
    store=PhaseArchiveStore(root)
    registry=build_recovery_inspection_registry(seed,durable,phase_store=store)
    dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=store)
    return fixture,seed,registry,dispatcher


def _advance_real_attempt(fixture):
    durable=fixture.reload()
    newer=record_provenance_event(durable,'attempt_started',{
        'action_id':fixture.execution['action_id'],'step_id':fixture.execution['step_id']})
    mark_step_verification_pending(durable,step_id=fixture.execution['step_id'],attempt_id=newer['attempt_id'])
    save_plan_state(fixture.root,fixture.task_id,durable)
    return fixture.reload()


def test_I11_durable_attempt_advance_revokes_existing_reader_and_old_seed_registry(tmp_path):
    fixture,seed,registry,dispatcher=_real_handoff_reader(tmp_path)
    row=typed_rows(registry)[0]
    newer=_advance_real_attempt(fixture)
    assert newer['steps'][0]['verification_attempt_id']!=seed.failure_packet.attempt_ref
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('stale inspection body must not read')):
        with pytest.raises(InspectionEvidenceError,match='SCOPE_MISMATCH.*attempt_id'):
            dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})
        with pytest.raises(InspectionEvidenceError,match='SCOPE_MISMATCH.*attempt_id'):
            build_recovery_inspection_registry(seed,newer,phase_store=PhaseArchiveStore(tmp_path))


def test_I11_stale_producer_cannot_publish_after_actual_attempt_advance(tmp_path):
    fixture,_seed,_registry,_dispatcher=_real_handoff_reader(tmp_path)
    source=fixture.supervisor_updates[0]['verification_outcome']
    _advance_real_attempt(fixture)
    before=fixture.reload()['task_state']['inspection_ref_registry']
    real_put=StateContractStore.put_object
    def reject_inspection(self,kind,body):
        assert not kind.startswith('recovery_inspection.'),'stale producer must not save inspection objects'
        return real_put(self,kind,body)
    with patch.object(StateContractStore,'put_object',reject_inspection):
        with pytest.raises(InspectionEvidenceError,match='SCOPE_MISMATCH.*attempt_id'):
            CausalFailurePacketBinder(data_path=str(tmp_path)).bind(fixture.state,'verification_outcome',source)
    assert fixture.reload()['task_state']['inspection_ref_registry']==before


def test_I11_new_episode_revokes_existing_reader_without_attempt_change(tmp_path):
    fixture,seed,registry,dispatcher=_real_handoff_reader(tmp_path)
    row=typed_rows(registry)[0]
    asyncio.run(fixture.inspect_handoff())
    newer=fixture.reload()
    current=project_recovery_seed(fixture.state,newer).recovery_seed
    assert current.seed_id!=seed.seed_id
    assert current.failure_packet.attempt_ref==seed.failure_packet.attempt_ref
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('old episode body must not read')):
        with pytest.raises(InspectionEvidenceError,match='SCOPE_MISMATCH.*episode_id'):
            dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})


def test_I11_unrelated_CAS_save_does_not_revoke_same_episode_reader(tmp_path):
    fixture,seed,registry,dispatcher=_real_handoff_reader(tmp_path)
    row=typed_rows(registry)[0]
    durable=fixture.reload();generation=durable['state_generation']
    durable['task_state']['inspection_unrelated_note']='safe metadata update'
    save_plan_state(tmp_path,fixture.task_id,durable)
    assert fixture.reload()['state_generation']>generation
    result=dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})
    assert result['content'] and result['scope']['episode_id']==seed.seed_id


def test_I12_durable_index_key_mismatch_rejected_before_read(tmp_path):
    fixture,seed,registry,dispatcher=_real_handoff_reader(tmp_path)
    row=typed_rows(registry)[0]
    durable=fixture.reload()
    durable['task_state']['inspection_ref_registry'][row['ref_id']]['ref_id']='inspection:sha256:'+'0'*64
    save_plan_state(tmp_path,fixture.task_id,durable)
    newer=fixture.reload()
    with patch.object(StateContractStore,'read_object',side_effect=AssertionError('substituted object must not read')):
        with pytest.raises(RecoveryToolError,match='INSPECTION_REF_INDEX_MISMATCH'):
            build_recovery_inspection_registry(seed,newer,phase_store=PhaseArchiveStore(tmp_path))
        with pytest.raises(InspectionEvidenceError,match='INSPECTION_REF_INDEX_MISMATCH'):
            dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})


@pytest.mark.parametrize('owner,status',[
    ('step','done'),('step','passed'),('step','completed'),
    ('task','completed'),('task','cancelled'),
])
def test_I11_finished_authority_revokes_reader(tmp_path,owner,status):
    fixture,_seed,registry,dispatcher=_real_handoff_reader(tmp_path)
    row=typed_rows(registry)[0]
    # Scope helper also serves producer/registry: terminal status must win
    # even if an old active packet and verification attempt are retained.
    from agent_core.workflow.inspection_evidence import assert_current_inspection_scope
    durable=fixture.reload()
    if owner=='step':durable['steps'][0]['status']=status
    else:durable['task_state']['status']=status
    with pytest.raises(InspectionEvidenceError,match='EPISODE_INACTIVE'):
        assert_current_inspection_scope(durable,dispatcher.inspection_reader.scope)


def test_I14_authority_read_is_bounded_before_inspection_body(tmp_path):
    fixture,_seed,registry,dispatcher=_real_handoff_reader(tmp_path)
    row=typed_rows(registry)[0]
    with patch('agent_core.workflow.inspection_evidence.MAX_INSPECTION_AUTHORITY_BYTES',128):
        with patch.object(StateContractStore,'read_object',side_effect=AssertionError('oversized authority must not read inspection body')):
            with pytest.raises(InspectionEvidenceError,match='AUTHORITY_SIZE_LIMIT'):
                dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})

"""Retry source and authorized historical inspection through real Recovery tools."""
import asyncio
import json
from langchain_core.messages import AIMessage
from agent_core.workflow.inspection_evidence import InspectionObjectReader, inspection_scope
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher
from tests.test_ger_failure_boundary import Store
from tests.test_recovery_projection import _verification_rejected


def two_attempts(root):
    store=Store(root)
    executed=store.attempt(1,0)
    first=_verification_rejected()
    first['type']='correction_required'
    first['summary']='rc=0 but validation_observation was not emitted'
    first['scope'].update(action_id='action-1',attempt_id='attempt-1')
    first['payload'].update(failure_owner='VERIFICATION',incident_id='incident-first',
        semantic_decision={},
        completion_gate={'passed':False,'classification':'step_effect_incomplete','missing_effects':{'produces_observations':['validation_observation']}},
        completion_report={'schema_version':1,'scope':dict(first['scope']),
            'completion_gate':{'passed':False},'process_outcome':{'returncode':0}},
        execution_record=executed['payload']['execution_record'])
    first['payload']['contract_violations']=[]
    first['payload']['validation_evidence'].update(task_id='task-recovery',step_id='step-1',attempt_id='attempt-1',status='VALID',returncode=0)
    first_update=store.binder().bind({'task_id':'task-recovery'},'verification_outcome',first)
    source=store.attempt(2,2)
    source['payload']['execution_record']['code']='<script language="bash">\ntrue\n</script>'
    task=store.value['task_state'];parent=first_update['active_failure_packet_ref']
    store.value['steps'][1].update(verification_attempt_id='attempt-1',status='verification_pending')
    task['verification_pending']={'attempt_id':'attempt-1'}
    task['recovery_state']={'active_recovery':{'recovery_id':'decision','envelope_id':'envelope'}}
    task['typed_recovery_receipts']={'decision':{'directive':{'action':'retry_step'},
        'failure_packet_ref':parent,'incident_id':'incident-first','execution_envelope_digest':'envelope',
        'execution_envelope':{'action_id':'action-2','step_id':'step-1','source_scope':{'attempt_id':'attempt-1'}}}}
    task['consumed_failure_packet_refs']={parent:{'decision_id':'decision'}}
    task['active_failure_packet_ref']=''
    for aid in ('attempt-1','attempt-2'):
        store.value['provenance_state']['attempts'][aid]['effect_evidence']={'produces_observations':[],
            'parser_receipt':{'status':'parsed','emission_count':0}}
    store.save('','',store.value)
    second=store.binder().bind({'task_id':'task-recovery'},'execute_outcome',source)
    return store,first_update,second,source


def test_two_attempt_source_history_wrong_reference_and_reload(tmp_path):
    store,first,second,source=two_attempts(tmp_path)
    durable=store.load()
    projection=project_recovery_seed(second,durable)
    seed=projection.recovery_seed;packet=projection.failure_packet
    assert packet.attempt_ref=='attempt-2'
    registry=build_recovery_inspection_registry(seed,durable,phase_store=PhaseArchiveStore(str(tmp_path)))
    dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=PhaseArchiveStore(str(tmp_path)))
    entries=durable['task_state']['inspection_ref_registry']
    history=next(entries[ref] for ref in packet.inspection_refs if entries.get(ref,{}).get('scope',{}).get('object_kind')=='causal_history')
    body=InspectionObjectReader(str(tmp_path),scope=inspection_scope(packet,seed.seed_id)).read_body(history,history['locator'])
    assert body['use']=='historical_diagnostic_only' and body['formal_evidence_reuse'] is False
    assert body['original_failure']['attempt_ref']=='attempt-1'
    rows=body['parent_evidence'];assert rows
    assert all(row['source_scope']['attempt_id']=='attempt-1' for row in rows)
    assert any(row.get('body') for row in rows)
    old_ref=next(ref for ref in first['failure_packet']['inspection_refs'] if ref in entries)
    # Use the actual Recovery agent loop so a bad tool reference is returned
    # to the fake model as an explainable observation, not an uncaught error.
    from agent_core.workflow.recovery_agent import RecoveryAgentLoop
    messages_seen=[]
    async def invoke(messages,**_kwargs):
        messages_seen.append(list(messages))
        if len(messages_seen)==1:return AIMessage(content='',tool_calls=[{'id':'wrong-history','name':'evidence_read','args':{'evidence_ref':old_ref},'type':'tool_call'}])
        assert 'INSPECTION_REF_' in str(messages)
        return AIMessage(content=json.dumps({'schema_version':1,'directive_id':'second-retry','task_id':'task-recovery',
            'action':'retry_step','summary':'Repair the current syntax error.','reason_code':'SCRIPT_SYNTAX_FAILED',
            'target_step_id':'step-1','instruction':'Generate a valid unwrapped body and measured observation.'}))
    result=asyncio.run(RecoveryAgentLoop(model=object(),dispatcher=dispatcher,bind_tools=False,provider_invoke=invoke).ainvoke(seed=seed,durable=durable))
    assert not result['terminal_error'],result
    assert len(messages_seen)==2
    assert store.load()==durable

"""New S3 receipt/ownership counterexamples; original audit witnesses remain."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import select
import signal
from unittest.mock import patch
import pytest
from agent_core.workflow import runtime_recovery_reporting as reporting
from agent_core.plan_tracker import load_plan_state,save_plan_state,record_preaction_contract_failure
from tests.test_preflight_recovery_audit import save


def test_F20_primary_execution_flag_keeps_original_scope():
    state={'task_id':'new','plan_id':'plan','plan_version':'v1','current_step_id':'s','steps':[],
        'task_state':{},'failure':{'error_code':'TOOL_FAILED','attempt_id':'actual','execution_started':True}}
    record_preaction_contract_failure(state,{'code':'GENERATE_RESPONSE_INCOMPLETE'},error_fingerprint='e',candidate_fingerprint='c',termination_reason='exhausted')
    assert state['failure']['execution_started'] is True and state['failure']['attempt_id']=='actual'
    assert state['failure']['latest_failure']['execution_started'] is False and state['failure']['latest_failure']['attempt_id']==''


def test_F25_F26_real_journal_recovers_lost_receipt_not_unowned_content(tmp_path):
    task='new-publish';state=load_plan_state(tmp_path,task);save_plan_state(tmp_path,task,state)
    target=tmp_path/'allowed'/'report.md';body='Frozen synthetic failure; no scientific success.\n'
    def publish(path=target,text=body):
        return reporting.publish_report(tmp_path,task,path,text,owner='new-stop',authorization_ref='explicit-isolated-test-root',
            scope_guard=lambda s:s['task_id']==task)
    actual_save=reporting.save_plan_state
    def lose_receipt(root,tid,value):
        if any(r.get('status')=='published' for r in value['task_state'].get('report_publications',{}).values()):
            raise OSError('synthetic final receipt failure')
        return actual_save(root,tid,value)
    with patch.object(reporting,'save_plan_state',lose_receipt),pytest.raises(OSError,match='final receipt'):
        publish()
    assert target.read_text()==body
    durable=load_plan_state(tmp_path,task);row=next(iter(durable['task_state']['report_publications'].values()))
    assert row['status']=='prepared' and row['prepared'] and not row.get('receipt')
    read,write=os.pipe();pid=os.fork()
    if pid==0:
        try:
            os.close(read)
            # A new process loads the durable journal. No receipt from the
            # failed parent return is transferred to it.
            result={'pid':os.getpid(),'receipt':publish()}
            os.write(write,json.dumps(result).encode());os.close(write);os._exit(0)
        except BaseException:os._exit(1)
    os.close(write)
    try:
        assert select.select([read],[],[],10)[0],'reload process timeout'
        result=json.loads(os.read(read,8192));assert os.waitpid(pid,0)[1]==0
        assert result['pid']!=os.getpid()
        receipt=result['receipt'];assert receipt['recovered_from']=='prepared_inode_hardlink'
    finally:
        os.close(read)
        try:
            if os.waitpid(pid,os.WNOHANG)[0]==0:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
        except ChildProcessError:pass
    assert publish()['sha256']==receipt['sha256']
    with pytest.raises(reporting.RuntimeReportError,match='FROZEN_BODY_CONFLICT'):publish(text='changed body')
    unowned=tmp_path/'unowned'/'report.md';unowned.parent.mkdir();unowned.write_text(body)
    for _ in range(2):
        with pytest.raises(reporting.RuntimeReportError,match='EXISTING_FILE_PROTECTED'):publish(path=unowned)
    # This file has an identical body and a recorded intent, but no own inode proof.
    assert unowned.read_text()==body
    save('F25-F26-publication',{'receipt':receipt,'journal':load_plan_state(tmp_path,task)['task_state']['report_publications'],
        'protected_orphan_sha256':hashlib.sha256(unowned.read_bytes()).hexdigest(),'executions':0})


def test_F28_two_real_processes_race_without_overwrite(tmp_path):
    path=tmp_path/'report.md';ready_r,ready_w=os.pipe();go_r,go_w=os.pipe();children=[]
    for owner in ('one','two'):
        read,write=os.pipe();pid=os.fork()
        if pid==0:
            try:
                os.close(read);body='owner='+owner+'\n';sha=hashlib.sha256(body.encode()).hexdigest()
                identity=hashlib.sha256(owner.encode()).hexdigest()
                intent={'operation_id':identity,'path':str(path),'sha256':sha}
                def prepared(row):
                    (tmp_path/(owner+'-proof.json')).write_text(json.dumps(row));os.write(ready_w,b'r');os.read(go_r,1)
                receipt=reporting._write_report(path,body,publication=intent,record_prepared=prepared)
                result={'owner':owner,'status':'created','receipt':receipt}
            except BaseException as exc:result={'owner':owner,'status':'failed','code':getattr(exc,'code',type(exc).__name__)}
            os.write(write,json.dumps(result).encode());os.close(write);os._exit(0)
        os.close(write);children.append((pid,read))
    os.close(ready_w);os.close(go_r)
    try:
        ready=b''
        while len(ready)<2:
            assert select.select([ready_r],[],[],10)[0],'process preparation timeout'
            chunk=os.read(ready_r,2-len(ready));assert chunk,'child exited before preparation';ready+=chunk
        os.write(go_w,b'gg');results=[]
        for pid,read in children:
            assert select.select([read],[],[],10)[0],'process publication timeout'
            results.append(json.loads(os.read(read,8192)));os.close(read)
            assert os.waitpid(pid,0)[1]==0
        assert sorted(r['status'] for r in results)==['created','failed'],results
        winner=next(r for r in results if r['status']=='created');loser=next(r for r in results if r['status']=='failed')
        assert loser['code']=='RUNTIME_REPORT_EXISTING_FILE_PROTECTED'
        assert path.read_text()=='owner='+winner['owner']+'\n'
        save('F28-real-process-race',{'processes':2,'results':results,'actual_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    finally:
        for fd in (ready_r,go_w):os.close(fd)
        for pid,read in children:
            try:
                if os.waitpid(pid,os.WNOHANG)[0]==0:
                    os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
            except ChildProcessError:pass
            try:os.close(read)
            except OSError:pass


def test_F21_causal_reference_does_not_choose_last_consumed():
    from agent_core.active_state import current_generation_causal_reference
    scope={'task_id':'new','plan_id':'p','plan_version':'v','step_id':'s','attempt_id':'actual'}
    envelope={'recovery_decision_id':'chosen','source_scope':scope}
    state={'task_id':'new','plan_id':'p','plan_version':'v','plan_revision':1,'current_step_id':'s',
      'provenance_state':{'attempts':{'actual':scope}},'task_state':{
       'recovery_state':{'active_recovery':{'recovery_id':'chosen','target_route':'generate','step_id':'s','plan_revision':1,'target_generation_id':'g','source_attempt_id':'actual'},
         'generation_claim':{'target_generation_id':'g','recovery_id':'chosen'},'active_recovery_envelope':envelope},
       'typed_recovery_receipts':{'chosen':{'failure_packet_ref':'packet','incident_id':'i','execution_envelope':envelope},'unrelated-later':{'failure_packet_ref':'other'}},
       'consumed_failure_packet_refs':{'packet':{'decision_id':'chosen'},'other':{'decision_id':'unrelated-later'}},
       'causal_failure_packets':{'packet':{'failure_packet':{'task_id':'new','step_id':'s','attempt_ref':'actual','incident_id':'i','plan_spec_ref':{'plan_id':'p','plan_version':'v'}}}}}}
    before=deepcopy(state);result=current_generation_causal_reference(state)
    assert result['status']=='verified' and result['source_attempt_id']=='actual' and state==before
    for key,value in [('source_attempt_id','foreign'),('target_generation_id','old'),('step_id','other-step')]:
        bad=deepcopy(state);bad['task_state']['recovery_state']['active_recovery'][key]=value
        assert current_generation_causal_reference(bad)['status']=='UNKNOWN'

    bad=deepcopy(state);bad['task_id']='another-session'
    assert current_generation_causal_reference(bad)['status']=='UNKNOWN'


def test_F22_two_failure_parents_exclude_input_and_foreign_history():
    from agent_core.workflow.managed_stop import _associated_attempts
    scope={'task_id':'new','plan_id':'p','plan_version':'v','step_id':'prepare'}
    def packet(attempt,step='prepare',task='new'):
        return {'task_id':task,'step_id':step,'plan_spec_ref':{'plan_id':'p','plan_version':'v'},'attempt_ref':attempt,
            'incident_id':'incident-'+attempt,'action_ref':'retry-action' if attempt=='second' else 'other'}
    state={**scope,'steps':[{'step_id':'prepare','status':'failed'}],
        'provenance_state':{'attempts':{name:{**scope,'attempt_id':name,'returncode':rc} for name,rc in [('first',7),('second',8),('input',0),('foreign',9)]}},
        'task_state':{'causal_failure_packets':{},'typed_recovery_receipts':{},'consumed_failure_packet_refs':{}}}
    task=state['task_state']
    for name,step,tid in [('first','prepare','new'),('input','input','new'),('foreign','prepare','foreign-task')]:
        p=packet(name,step,tid);ref='packet:'+name;decision='decision:'+name
        task['causal_failure_packets'][ref]={'failure_packet':p}
        task['typed_recovery_receipts'][decision]={'failure_packet_ref':ref,'incident_id':p['incident_id'],'directive':{'action':'retry_step'},
            'execution_envelope':{'action_id':'retry-action','source_scope':{**scope,'task_id':tid,'step_id':step,'attempt_id':name}}}
        task['consumed_failure_packet_refs'][ref]={'decision_id':decision}
    result=_associated_attempts(state,packet('second'),'packet:second')
    assert [(r['attempt_id'],r['returncode']) for r in result]==[('first',7),('second',8)],result
    assert result[0]['use']=='historical_diagnostic' and result[1]['use']=='current_failure'
    save('F22-two-failures',{'associated_attempts':result,'excluded':['input','foreign']})

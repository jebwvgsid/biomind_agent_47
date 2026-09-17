from copy import deepcopy
import pytest
from tests.test_unified_runtime_preparation import _recovery_fixture
from agent_core.plan_tracker import load_plan_state,save_plan_state,record_provenance_event

@pytest.mark.parametrize("authority",["stale_install","stale_recovery"])
def test_stale_authority_retires_new_failure(tmp_path,authority):
    tid,sid,contract,service,original,aid,calls=_recovery_fixture(tmp_path)
    prepared=service.prepare_generation({'task_id':tid},selected_skill_id=sid,capability_contract=contract)
    epoch=prepared['runtime_state']['execution_epoch']
    action={'action_id':'new-failure','step_id':'science','plan_id':prepared['plan_id'],'plan_version':prepared['plan_version'],'execution_epoch':epoch,'type':'execute','purpose':'analysis','code':'# new externally ended failure','outputs':[]}
    record_provenance_event(prepared,'action_bound',{'action':action})
    attempt=record_provenance_event(prepared,'attempt_started',{'action_id':action['action_id'],'step_id':'science'})
    record_provenance_event(prepared,'execution_finished',{'attempt_id':attempt['attempt_id'],'execution_record':{'attempt_id':attempt['attempt_id'],'execution_id':'new-failed-execution','action_id':action['action_id'],'step_id':'science','plan_id':prepared['plan_id'],'plan_version':prepared['plan_version'],'returncode':1,'exit_code':1,'execution_status':'failed','purpose':'analysis'}})
    if authority=='stale_install':
        prepared['task_state']['installation_request']={'request_id':'current-unapproved-request','target_env':'newtarget'}
        prepared['task_state']['installation_receipt']={'status':'available','request_id':'stale-other-request','target_env':'other-target','target_prefix':'/tmp/other-target','selected_skills':[sid],'capabilities':[contract['capability_id']]}
    else:
        prepared['task_state']['recovery_state']['active_recovery']={'recovery_id':'stale-r','step_id':'science','status':'completed','plan_revision':-1,'source_attempt_id':aid}
        prepared['task_state']['typed_recovery_receipts']={'stale-r':{'directive':{'action':'retry_step','evidence_refs':['stale:other']}}}
    save_plan_state(tmp_path,tid,prepared)
    accepted=service.prepare_generation({'task_id':tid},selected_skill_id=sid,capability_contract=contract)
    assert accepted['runtime_state']['execution_epoch']==epoch+1
    assert accepted['provenance_state']['attempts'][attempt['attempt_id']]['status']=='FAILED'

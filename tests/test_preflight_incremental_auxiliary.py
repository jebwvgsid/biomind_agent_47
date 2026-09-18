"""Receipt/display/resource regressions using only isolated local fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_core.cli_display import DisplayModel
from agent_core.cli_lifecycle import tool_result_text, cancellation_receipt_text
from agent_core.cli_terminal import Terminal
from agent_core.skill_resource_binding import continue_action_skill_selection, bind_action_skill_resources
from agent_core.skill_access import SkillAccessError
from tests.test_skill_resource_binding import fixture


@pytest.mark.parametrize('size',[1156,3814,1126])
def test_archived_exec_truncation_flags_do_not_claim_missing_cursor(size):
    text,status=tool_result_text('exec.command',{'result':{'stdout':'x'*size,
        'truncated':{'stderr':False,'stdout':False},'receipt_ref':'/fixture/receipt.json',
        'log_refs':{'stdout':'/fixture/stdout.log','stderr':'/fixture/stderr.log'}}})
    assert status=='returned' and '裁剪' not in text


def test_truncated_exec_receipt_preserves_a_readable_exact_log_reference():
    text,status=tool_result_text('exec.command',{'result':{'stdout':'first page',
        'truncated':{'stderr':False,'stdout':True},'receipt_ref':'/fixture/receipt.json',
        'log_refs':{'stdout':'/fixture/stdout.log','stderr':'/fixture/stderr.log'}}})
    assert status=='partial' and 'exec_command' in text
    assert '/fixture/stdout.log' in text and '/fixture/stderr.log' not in text


def test_repeated_cancel_wait_is_deduplicated_per_actual_input_cycle():
    model=DisplayModel()
    terminal=SimpleNamespace(fallback=False,pending=None,cancel_requested=False,
        context=('task','running',{}),_request_serial=1,receive=model.receive,cancel_callback=None)
    for _ in range(8):Terminal.request_cancel(terminal)
    assert sum('仍待确认' in s for s in model.history)==1
    assert sum('已请求取消' in s for s in model.history)==1
    terminal.cancel_requested=False;terminal._request_serial+=1
    for _ in range(8):Terminal.request_cancel(terminal)
    assert sum('仍待确认' in s for s in model.history)==2


def test_local_cleanup_receipt_never_claims_global_or_remote_cleanup():
    receipt={'receipt_id':'cancel-1'}
    confirmed=cancellation_receipt_text(receipt,{'cleanup_result':'CLEANUP_CONFIRMED'},set())
    partial=cancellation_receipt_text(receipt,{'cleanup_result':'CLEANUP_TIMEOUT'},{123})
    assert '末次模型请求本地清理已确认' in confirmed and '整体清理未确认' not in confirmed
    assert '当前无登记中的子进程组' in confirmed and '未声明远端任务已终止' in confirmed
    assert '本地清理未确认' in partial and '仍登记 1' in partial


def test_public_english_is_not_keyword_filtered_and_internal_events_stay_in_details():
    model=DisplayModel()
    body='1. Primary Request and Intent: user-facing diagnostic evidence.'
    model.receive('message',body,purpose='telemetry',node='recovery_context_compaction')
    assert not model.history and body in model.details
    model.receive('model_public',body,source='model_public',node='recovery')
    assert body in model.history


@pytest.mark.parametrize('change',['none','explicit_new','ordinary','snapshot','stale','capability'])
def test_same_helper_selection_is_scoped_and_permissions_are_rebuilt(tmp_path,change):
    service,access=fixture(tmp_path/'skills')
    handle='3d-dna/tad-calling#scripts/call_tads.py'
    old=bind_action_skill_resources({'skill_resource_refs':[handle],'execution_capability_id':'cap',
        'env':{'OLD_PRIVILEGE':'never-copy'}},service,access,step_id='step',cwd=tmp_path)
    durable={'task_id':'task','plan_id':'plan','plan_version':'v1','task_state':{'typed_recovery_receipts':{
        'decision':{'directive':{'action':'retry_step'},'outcome':{'scope':{
            'task_id':'task','plan_id':'plan','plan_version':'v1','step_id':'step','action_id':'old'}}}}},
        'provenance_state':{'actions':{'old':{'normalized_action':old}}}}
    action={'execution_capability_id':'cap','recovery_decision_id':'decision','code':'print("fixture")'}
    if change=='explicit_new':action['skill_resource_refs']=['3d-dna/tad-calling#scripts/check_mcool_balance.py']
    if change=='ordinary':action['skill_resource_refs']=[]
    if change=='snapshot':Path(old['formal_skill_resources'][0]['canonical_path']).write_text('print("changed")')
    if change=='stale':durable['plan_version']='v2'
    if change=='capability':action['execution_capability_id']='new-capability'
    if change in {'snapshot','stale'}:
        with pytest.raises(SkillAccessError):continue_action_skill_selection(action,durable,service,access,step_id='step',cwd=tmp_path)
        return
    continued=continue_action_skill_selection(action,durable,service,access,step_id='step',cwd=tmp_path)
    assert 'env' not in continued and 'formal_skill_resources' not in continued
    bound=bind_action_skill_resources(continued,service,access,step_id='step',cwd=tmp_path)
    assert 'OLD_PRIVILEGE' not in bound.get('env',{})
    if change=='none':assert bound['skill_resource_refs']==[handle]
    if change=='explicit_new':assert bound['skill_resource_refs']==action['skill_resource_refs']
    if change in {'ordinary','capability'}:assert not bound.get('formal_skill_resources')

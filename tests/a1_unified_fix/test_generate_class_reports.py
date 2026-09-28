"""New local report ownership/failure combinations through the actual writer."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_core.plan_tracker import load_plan_state,save_plan_state
from agent_core.workflow import runtime_recovery_reporting as reporting


@pytest.mark.parametrize('owner',['same_task_older_stop','another_task','unknown_user'])
def test_equal_bytes_do_not_borrow_other_report_ownership(tmp_path,owner):
    task='current-report-run';save_plan_state(tmp_path,task,load_plan_state(tmp_path,task))
    target=tmp_path/'approved-root'/'report.md';target.parent.mkdir()
    body='New isolated old result; owner='+owner+'\n'
    if owner=='unknown_user':
        target.write_text(body)
    else:
        old_task=task if owner=='same_task_older_stop' else 'other-report-run'
        save_plan_state(tmp_path,old_task,load_plan_state(tmp_path,old_task))
        old=reporting.publish_report(tmp_path,old_task,target,body,owner='previous-stop',authorization_ref='old-explicit-test-root',scope_guard=lambda s:s['task_id']==old_task)
        assert old['operation_id'] and old['ownership']
    before=(target.read_bytes(),target.stat().st_mtime_ns,target.stat().st_ino)
    with pytest.raises(reporting.RuntimeReportError,match='EXISTING_FILE_PROTECTED'):
        reporting.publish_report(tmp_path,task,target,body,owner='current-stop',authorization_ref='new-test-root',scope_guard=lambda s:s['task_id']==task)
    assert (target.read_bytes(),target.stat().st_mtime_ns,target.stat().st_ino)==before


def test_partial_conflict_and_unwritable_diagnostics_are_truthful(tmp_path):
    task='partial-report-run';save_plan_state(tmp_path,task,load_plan_state(tmp_path,task))
    root=tmp_path/'approved';root.mkdir();(root/'report.md').write_text('isolated user file')
    result={}
    for name in ['report.md','stage_status.tsv','reproduce.sh']:
        try:
            result[name]=reporting.publish_report(tmp_path,task,root/name,'new '+name,owner='stop',authorization_ref='explicit-test',scope_guard=lambda s:s['task_id']==task)
        except reporting.RuntimeReportError as exc:result[name]={'error':exc.code}
    assert result['report.md']['error']=='RUNTIME_REPORT_EXISTING_FILE_PROTECTED'
    assert all(result[n]['sha256'] for n in ['stage_status.tsv','reproduce.sh'])
    opened=reporting._open_directory
    def denied(path,**kwargs):
        if 'no-write' in Path(path).parts:raise PermissionError('isolated checker-injected filesystem failure')
        return opened(path,**kwargs)
    with patch.object(reporting,'_open_directory',denied),pytest.raises(PermissionError):
        reporting.publish_report(tmp_path,task,tmp_path/'no-write'/'report.md','new diagnosis',owner='stop:internal',authorization_ref='explicit-test',scope_guard=lambda s:s['task_id']==task)
    assert not (tmp_path/'no-write'/'report.md').exists()
    rows=load_plan_state(tmp_path,task)['task_state']['report_publications'].values()
    assert not any(r.get('status')=='published' for r in rows if 'no-write' in r['path'])



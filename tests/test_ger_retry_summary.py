"""Two-attempt lineage, real read-only Recovery tools and report protection."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage
from agent_core.workflow.inspection_evidence import InspectionObjectReader, inspection_scope
from agent_core.workflow.managed_stop import build_managed_stop, finalize_managed_stop
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher
from agent_core.workflow.runtime_recovery_reporting import _write_report, RuntimeReportError
from tests.test_ger_failure_boundary import Store
from tests.test_recovery_projection import _verification_rejected


from tests.test_ger_retry_history import two_attempts


def test_two_attempt_summary_keeps_primary_and_secondary_causes(tmp_path):
    store,first,second,source=two_attempts(tmp_path)
    boundary={'node':'recovery','status':'failed','outcome':{'type':'failed','summary':'Recovery budget exhausted',
        'payload':{'error_code':'RECOVERY_BUDGET_EXHAUSTED'}}}
    stop=build_managed_stop('task-recovery',{'runtime_recovery_result':{'reason_code':'REPORT_WRITE_FAILED','explanation':'secondary report failure'}},store.load(),boundary)
    assert stop['execution']['returncode']==2 and stop['origin']['attempt_id']=='attempt-2'
    assert stop['stop_reason']['error_code']=='RECOVERY_BUDGET_EXHAUSTED'
    assert [(r['attempt_id'],r['returncode']) for r in stop['associated_attempts']]==[('attempt-1',0),('attempt-2',2)]
    assert all(r['step_completed'] is False for r in stop['associated_attempts'])
    assert stop['associated_attempts'][0]['formal_evidence_satisfied'] is False
    assert '历史诊断 attempt：attempt-1；rc=0' in stop['explanation']
    assert '当前故障 attempt：attempt-2；rc=2' in stop['explanation']
    assert store.load()['steps'][1]['verification_attempt_id']=='attempt-1'


def test_report_idempotence_and_foreign_content_protected(tmp_path):
    path=tmp_path/'report.md'
    receipt=_write_report(path,'authorized stop A')
    assert _write_report(path,'authorized stop A',prior_receipt=receipt)==receipt
    for body,prior in [('authorized stop A',None),('different stop B',receipt)]:
        with pytest.raises(RuntimeReportError,match='EXISTING_FILE_PROTECTED'):_write_report(path,body,prior_receipt=prior)
    assert path.read_text()=='authorized stop A'


def test_report_publish_race_is_protected_not_generic_io(tmp_path):
    path=tmp_path/'report.md'
    with patch('agent_core.workflow.runtime_recovery_reporting.os.link',side_effect=FileExistsError):
        with pytest.raises(RuntimeReportError,match='EXISTING_FILE_PROTECTED'):_write_report(path,'candidate')
    assert not list(tmp_path.glob('.runtime-recovery-*'))


def test_finalizer_report_conflict_is_secondary_and_reload_is_idempotent(tmp_path):
    store,_,second,source=two_attempts(tmp_path)
    root=store.path.parent
    target=root/'report.md';target.write_text('old producer report')
    agent=SimpleNamespace(data_path=str(tmp_path),terminal_summary_llm=None,terminal_summary_policy={})
    boundary={'node':'recovery','status':'failed','outcome':{'type':'failed','summary':'Current syntax failure unresolved',
        'payload':{'error_code':'RECOVERY_BUDGET_EXHAUSTED'}}}
    with patch('agent_core.workflow.managed_stop._report_targets',return_value={'report.md':target}):
        stop=asyncio.run(finalize_managed_stop(agent,'task-recovery',second,boundary,loader=store.load,saver=store.save))
        again=asyncio.run(finalize_managed_stop(agent,'task-recovery',second,boundary,loader=store.load,saver=store.save))
    assert stop['execution']['returncode']==2
    assert stop['stop_reason']['error_code']=='RECOVERY_BUDGET_EXHAUSTED'
    assert stop['report_receipts']['report.md']['reason']=='RUNTIME_REPORT_EXISTING_FILE_PROTECTED'
    assert stop['report_receipts']['report.md']['status']=='failed'
    assert again==stop
    assert target.read_text()=='old producer report'

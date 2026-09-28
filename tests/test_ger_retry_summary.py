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
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher
from agent_core.workflow.runtime_recovery_reporting import _write_report, RuntimeReportError
from tests.test_ger_failure_boundary import Store
from tests.test_recovery_projection import _verification_rejected






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



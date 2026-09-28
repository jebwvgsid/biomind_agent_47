"""A/D acceptance: scripted decisions, real readers/dispatch/questions/Plan consumers.

Run only through the guarded offline launcher. No science, live API or approval
auto-answer; interrupt below supplies the explicit synthetic user's answer.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
from unittest.mock import patch

import pytest
from agent_core.consult_handoff import output_preservation_authorization
from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.runtime_recovery_reporting import report_publication_preflight
from prompts.manager import stage_work_prompt, WORK_PROTOCOL_REVISION
from tests.contracts_support import isolated_host
from tests.test_longrun_integration_v2 import register_command, tool_payloads, record
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as skills


def wire(messages):
    return [{'role': m.type, 'content': m.content, 'tool_calls': getattr(m, 'tool_calls', []),
             'name': getattr(m, 'name', None)} for m in messages]










@pytest.mark.parametrize('kind', ['none', 'unmaterialized', 'mixed', 'conflict', 'symlink_conflict', 'unverified'])
def test_A04_report_coverage_is_not_directory_clearance(tmp_path, kind):
    root=tmp_path/'results';root.mkdir()
    rows=[]
    if kind!='none':
        rows=[{'artifact_id':'report','role':'report','artifact_kind':'file','source':'user_explicit'}]
    if kind in ('mixed','conflict','symlink_conflict','unverified'):
        rows.append({'artifact_id':'reproduce','role':'reproduce_script','artifact_kind':'file','source':'user_explicit',
                     'canonical_path':str(root/'reproduce.sh')})
    if kind=='conflict':(root/'reproduce.sh').write_text('old script')
    if kind=='symlink_conflict':(root/'reproduce.sh').symlink_to(tmp_path/'not-a-file')
    if kind=='unverified':root.chmod(0)
    state={'task_id':'coverage','current_task':{'task_revision':1,'task_spec':{'task_id':'coverage','goal':'Coverage only',
           'revision':1,'inputs':[],'output_contracts':rows}},'task_state':{}}
    before=deepcopy(state)
    try:value=report_publication_preflight(state,tmp_path/'task-root')
    finally:
        if kind=='unverified':root.chmod(0o700)
    assert state==before and value['publication_authorized'] is False
    assert value['coverage']['scope']=='declared_report_targets_only'
    assert value['publication_ready'] is False
    if kind=='none':assert value['status']=='not_applicable' and value['coverage']['status']=='not_requested'
    elif kind=='unmaterialized':
        assert value['status']=='deferred' and value['targets']==[] and value['coverage']['unresolved']
    elif kind=='mixed':assert value['status']=='deferred' and value['coverage']['checked_count']==1
    else:assert value['status']=='needs_user_decision' and not value['planning_allowed']
    if kind in ('none','unmaterialized','mixed'):assert value['planning_allowed']
    if kind=='unverified':assert value['targets'][0]['status']=='unverified'
    record('A04_'+kind,result=value)











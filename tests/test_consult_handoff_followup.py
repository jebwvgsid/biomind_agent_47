"""Regression for session_1789889270 through the real Consult phase/transport.

Only provider responses and temporary files are synthetic; no live science/API.
"""
import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from agent_core.cli_activity import skill_display_result
from agent_core.plan_tracker import load_plan_state
from agent_core.skill_tools import skill_tool_schemas
from agent_core.task_spec import normalize_output_binding
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as fixture


@pytest.mark.parametrize("row,kind,fmt", [
    ({'role': 'tad_output_root', 'path': '/out/HiCExplorer'}, 'directory', 'directory'),
    ({'role': 'boundary_change_tables', 'path': '/out/changes/'}, 'directory', 'directory'),
    ({'role': 'results', 'format': 'directory', 'path': '/out/results.v1'}, 'directory', 'directory'),
    ({'role': 'domains', 'path': '/out/leaf_domains.bed'}, 'file', 'bed'),
    ({'role': 'readme', 'artifact_kind': 'file', 'path': '/out/README'}, 'file', 'file'),
    ({'role': 'results', 'artifact_kind': 'directory', 'path': '/out/results.v1'}, 'directory', 'directory'),
    # Explicit contradictions are not silently repaired.
    ({'role': 'results', 'artifact_kind': 'file', 'format': 'directory', 'path': '/out/results'}, 'file', 'directory'),
])
def test_output_kind_default_is_consistent_with_legacy_format(row, kind, fmt):
    result = normalize_output_binding(row)
    assert result['artifact_kind'] == kind
    assert result['format'] == fmt
    assert normalize_output_binding(result) == result


def test_native_handoff_declares_output_types_and_partial_display():
    handoff = next(s['function'] for s in skill_tool_schemas('consultant')
                   if s['function']['name'] == 'submit_planning_handoff')
    row = handoff['parameters']['properties']['outputs']['items']
    assert 'artifact_kind' in row['required']
    assert set(row['properties']['artifact_kind']['enum']) == {'file', 'directory'}
    receipt = ({}, {'status': 'PARTIAL', 'results': [{'status': 'PARTIAL', 'has_more': True}]})
    before = deepcopy(receipt)
    assert skill_display_result(receipt) == 'partial'
    assert receipt == before





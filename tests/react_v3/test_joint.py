"""v1.1 joint acceptance: actual nodes/state/runner, only provider is offline."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shlex
import sys
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def test_A01_same_basename_keeps_missing_input(tmp_path):
    from agent_core.task_spec import _dedupe_input_aliases
    old = tmp_path/'old'/'same.vcf'; old.parent.mkdir(); old.write_text('real old path')
    missing = tmp_path/'new'/'same.vcf'
    rows = [{'role': 'variants', 'canonical_path': str(p)} for p in (old, missing)]
    result, audit = _dedupe_input_aliases(rows)
    assert len(result) == 2 and result == rows
    assert not missing.exists()


def test_P02_P03_simple_plan_dependencies_and_notes():
    from agent_core.plan_contract import prepare_simple_plan, simple_plan_diagnostics
    plan = prepare_simple_plan({'notes': 'keep this', 'steps': ['Read environment', {'description': 'Run', 'dependencies': [1], 'notes': 'language chosen later'}]})
    assert plan['notes'] == 'keep this'
    assert plan['steps'][1]['dependencies'] == ['step_1']
    assert not plan['steps'][0].get('effect')
    assert simple_plan_diagnostics({'steps': [{'description': 'a', 'dependencies': ['missing']}]})[0]['code'] == 'PLAN_DEPENDENCY_MISSING'
    assert simple_plan_diagnostics({'steps': [{'description': 'a', 'dependencies': [2]}, {'description': 'b', 'dependencies': [1]}]})[0]['code'] == 'PLAN_DEPENDENCY_CYCLE'








def test_A01_roles_and_paths_are_not_guessed_from_suffix(tmp_path):
    from agent_core.task_spec import normalize_input_binding
    path=tmp_path/'reference.gtf'
    declared=normalize_input_binding({'role':'annotation_file','path':str(path),'sample':'sample-A'})
    assert declared['role']=='annotation_file' and declared['canonical_path']==str(path)
    unknown=normalize_input_binding({'path':str(path)})
    assert unknown['role']=='input_file' and unknown['role_suggestion']=='annotation_gtf'


def test_B02_actual_input_changes_invalidate_not_plan_notes(tmp_path):
    from agent_core.plan_bindings import binding_producer_signature
    path=tmp_path/'input.txt';path.write_text('version1')
    state={'plan_revision':1,'steps':[{'step_id':'producer','inputs':[str(path)],'parameters':{'resolution':40000},'dependencies':[]}]}
    before=binding_producer_signature(state,'producer')
    state['plan_revision']=2;state['steps'][0]['notes']='clearer wording'
    assert binding_producer_signature(state,'producer')==before
    path.write_text('version2-changed')
    assert binding_producer_signature(state,'producer')!=before


def test_B03_prefilled_measurement_is_not_runtime_observation():
    from agent_core.plan_bindings import accept_action_binding_candidates
    state={}
    action={'produced_bindings':{'measured_count':{'type':'integer','value':999}}}
    assert accept_action_binding_candidates(state,action,{'returncode':0},{'passed':True})==()
    assert not state.get('plan_bindings')


def test_G01_four_archived_tad_candidates_no_helper_or_language_gate():
    from agent_core.public_response import project_generate_response
    from agent_core.controller_audit import parse_executable_script_block
    from agent_core.step_effects import materialize_effect_template, compile_step_effect
    from execution.script_validation import check_executable_action
    rows=json.loads((Path(__file__).parents[1]/'fixtures/tad_20260919/generate_responses.json').read_text())
    assert len(rows)==5
    for row in rows[:4]:
        projected=project_generate_response(row['content'],script_protocol=True)
        block=parse_executable_script_block(projected.payload)
        assert materialize_effect_template(block.body,{},language=block.language)==block.body
        checked=check_executable_action({'type':'execute','language':block.language,'code':block.body})
        assert checked['check_status']=='PASS',checked
    # Missing effects stay absent rather than being called a successful effect.
    compiled=compile_step_effect({'step_id':'preflight','step_protocol_version':3}, {})
    assert compiled['status']=='not_applicable' and compiled['complete'] is None

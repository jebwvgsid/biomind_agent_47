"""Approved Plan -> Generate -> approval -> real shell -> reconcile -> Gate/Commit.

Model replies/approval and environment discovery are explicit fixture leaves.
All managed provenance is emitted by production Execute, never by the test.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage


def test_owned_candidate_reuse_rejects_foreign_changed_inputs_and_explicit_freshness(tmp_path):
    from agent_core.managed_artifacts import (capture_managed_output_snapshot, discover_current_attempt_outputs,
        discover_owned_unchanged_outputs, reconcile_runtime_outputs, prepare_approved_output_parents)
    target = tmp_path / 'new-parent' / 'result.txt'
    contract = {'artifact_id': 'r', 'semantic_handle': 'output.r', 'expected_path': str(target),
        'artifact_kind': 'file', 'validation_contract_id': 'file_exists', 'freshness': 'owned_candidate'}
    assert not target.parent.exists()
    assert prepare_approved_output_parents([contract]) == [str(target.parent)]
    assert not target.exists()
    before = capture_managed_output_snapshot([contract]); target.write_text('')
    after = capture_managed_output_snapshot([contract])
    observed = discover_current_attempt_outputs(before, after, attempt_id='origin')
    assert reconcile_runtime_outputs([contract], observed, attempt_id='origin', exit_code=0)['status'] == 'accepted'
    prior = dict(attempt_id='origin', step_id='s', plan_id='p', plan_version='v', returncode=0,
        managed_input_signature={'in': 'unchanged'}, managed_output_contracts=[contract],
        managed_output_snapshot_after=after, observed_output_records=observed)
    state = {'task_id': 'one', 'plan_id': 'p', 'plan_version': 'v', 'provenance_state': {'attempts': {'origin': prior}}}
    from agent_core.plan_tracker import semantic_plan_hash
    prior['managed_reuse_scope'] = {'task_id': 'one', 'task_revision': None, 'plan_semantic_hash': semantic_plan_hash(state)}
    def reuse(contracts=None, inputs=None):
        return discover_owned_unchanged_outputs(state, contracts or [contract], after,
            capture_managed_output_snapshot([contract]), attempt_id='retry', step_id='s',
            input_signature=inputs or {'in': 'unchanged'})
    accepted = reuse(); assert len(accepted) == 1 and not accepted[0]['current_attempt_owned']
    assert accepted[0]['source_attempt_id'] == 'origin'
    reconciled = reconcile_runtime_outputs([contract], accepted, attempt_id='retry', exit_code=0)
    assert reconciled['bindings'][0]['realization'] == 'revalidated_existing'
    assert not reuse(inputs={'in': 'changed'})
    prior['status']='invalidated'; assert not reuse(); prior.pop('status')
    state['current_task']={'task_revision':2}; assert not reuse(); state.pop('current_task')
    assert not reuse([{**contract, 'freshness': 'current_attempt'}])
    state['plan_version'] = 'new'; assert not reuse(); state['plan_version'] = 'v'
    prior['observed_output_records'] = []; assert not reuse(); prior['observed_output_records'] = observed
    target.write_text('foreign mutation'); assert not reuse()
    foreign = tmp_path / 'foreign'; foreign.mkdir(); (tmp_path / 'link').symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match='SYMLINK'):
        prepare_approved_output_parents([{**contract, 'expected_path': str(tmp_path / 'link' / 'result')}])


def test_reuse_tracks_upstream_directory_members_and_rejects_partial_snapshots(tmp_path):
    from agent_core.managed_artifacts import managed_input_signature, _bounded_reuse_signature
    upstream = tmp_path / 'upstream'; upstream.mkdir(); child = upstream / 'member'; child.write_text('old')
    output = tmp_path / 'new-result'
    action = {'materialized_artifacts': {'output.prior': str(upstream), 'output.current': str(output)},
              'controller_output_bindings': [{'expected_path': str(output)}]}
    before = managed_input_signature(action)
    assert set(before) == {'output.prior'} and before['output.prior']['complete']
    child.write_text('new')
    assert managed_input_signature(action) != before
    assert not _bounded_reuse_signature(upstream, max_entries=1)['complete']
    assert not _bounded_reuse_signature(upstream, max_depth=0)['complete']
    (upstream / 'alias').symlink_to(child)
    assert not managed_input_signature(action)['output.prior']['complete']


def test_frozen_review_level_changes_plan_hash_and_skills_do_not_request_review():
    from agent_core.plan_contract import approved_step_validation_level
    from agent_core.plan_tracker import semantic_plan_hash
    state = {'steps': [{'step_id': 's', 'title': 'x', 'action': 'execute', 'status': 'ready', 'skill_ids': ['domain']}], 'current_step': 0}
    assert approved_step_validation_level(state, 's') == 'basic'
    before = semantic_plan_hash(state)
    state['steps'][0]['validation_level'] = 'scientific'
    assert approved_step_validation_level(state, 's') == 'scientific'
    assert semantic_plan_hash(state) != before




def test_reference_helper_emits_approved_plan_local_identity_without_alias_rewriting(tmp_path, monkeypatch, capsys):
    from tests.test_recovery50000_reference_helper import helper, _request, _fake_command
    from agent_core.managed_artifacts import approved_artifact_emission_identities
    from agent_core.step_effects import structured_effect_parse_receipt
    req = _request(tmp_path); calls=[]; monkeypatch.setattr(helper, '_command', _fake_command(calls))
    req['artifact_ref']='output.plan-artifact-bundle'; req['evidence_artifact_ref']='output.plan-artifact-evidence'
    effect = {'produces_artifacts': ['plan-artifact-bundle','plan-artifact-evidence'],
        'produces_observations': ['reference_bundle_valid'], 'explicit_side_effects': ['create_reference_indexes']}
    bindings = [{'artifact_id': key, 'semantic_handle': 'output.'+key} for key in effect['produces_artifacts']]
    for key, value in req['environment'].items(): monkeypatch.setenv(key, value)
    monkeypatch.setenv('BIOMIND_ARTIFACT_IDENTITIES_JSON', json.dumps(approved_artifact_emission_identities(bindings, effect)))
    argv=[]
    for key,value in [('source',req['source']),('output',req['output']),('work-dir',req['work_dir']),
            ('reference-ref',req['reference_ref']),('artifact-ref',req['artifact_ref']),
            ('evidence-artifact-ref',req['evidence_artifact_ref']),*req['tools'].items()]:argv += ['--'+key,str(value)]
    assert helper.main(argv) == 0
    stdout=capsys.readouterr().out
    receipt=structured_effect_parse_receipt(stdout,effect)
    assert not receipt['issues'], receipt
    assert receipt['emission_count'] == 1 and calls == ['bwa','fai','dictionary']
    # The parser remains strict for unrelated/undeclared access references.
    assert structured_effect_parse_receipt(stdout.replace('plan-artifact-bundle','foreign'),effect)['issues']


def test_reference_scope_reads_nested_plan_local_role(tmp_path):
    from agent_core.genomics_reference_bundle import _formal_paths, REFERENCE_BUNDLE_FINAL_URI_PARSER_ID
    from agent_core.task_spec import create_task_spec
    source=tmp_path/'source.fa';source.write_text('>x\nAC\n')
    task=create_task_spec('Prepare reference',task_id='role',base_dir=tmp_path,project_root=tmp_path,
        structured_source={'input_files':[{'role':'reference_fasta','path':str(source),'format':'fasta'}]})
    rows={key:{'logical_artifact_id':key,'canonical_path':str(tmp_path/key),'object_kind':'directory',
        'resource_kind':'immutable_artifact','semantic_view':{'role':role,'format':'directory'}}
        for key,role in [('bundle','reference_indexes'),('evidence','reference_preparation_evidence')]}
    step={'step_id':'s','inputs':['input.reference_fasta'],'outputs':list(rows),
        'produces_artifacts':list(rows),'produces_observations':[{'identity':'reference_bundle_valid',
        'predicate':{'predicate_id':'reference','operator':'boolean_equals','expected':True,
            'parser_id':REFERENCE_BUNDLE_FINAL_URI_PARSER_ID,'failure_disposition':'correction_required','missing_disposition':'inspection_required'}}]}
    plan={'task_id':'role','steps':[step],'plan_artifacts':rows,'current_task':{'task_spec':task}}
    paths=_formal_paths(plan,step)
    assert paths[2:] == ('bundle',tmp_path/'bundle')










def test_freshness_is_semantic_and_survives_task_output_normalization(tmp_path):
    from agent_core.plan_tracker import semantic_plan_hash, plan_semantic_payload, normalize_task_output_materializations
    from tests.test_plan_result_contract_v1 import candidate
    plan = candidate(output=False)
    plan['plan_artifacts'] = {'result': {'object_kind': 'file'}}
    original = semantic_plan_hash(plan)
    assert 'artifact_freshness' not in plan_semantic_payload(plan)
    plan['plan_artifacts']['result']['freshness'] = 'current_attempt'
    assert semantic_plan_hash(plan) != original
    assert plan_semantic_payload(plan)['artifact_freshness'] == {'result': 'current_attempt'}
    plan['task_output_materializations'] = [{'output_ref': 'output.report', 'path': 'report.md',
        'required': False, 'path_policy': 'managed_exact', 'validator': 'file_exists', 'freshness': 'current_attempt'}]
    spec = {'output_contracts': [
        {'output_id': 'result-report', 'semantic_handle': 'output.report', 'artifact_kind': 'file', 'required': True},
        {'output_id': 'root', 'role': 'output_dir', 'artifact_kind': 'directory', 'path': str(tmp_path)}]}
    normalized, issues = normalize_task_output_materializations(plan, spec)
    assert not issues
    assert normalized['task_output_materializations'][0]['freshness'] == 'current_attempt'

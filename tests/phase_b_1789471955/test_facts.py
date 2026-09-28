"""Saved history is input only; production projection/verification owns the result."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_core.evidence_projection import candidate_binding_facts, committed_dependency_facts, dependency_input_scope
from agent_core.semantic_completion import _derive_report_facts
from tests.test_typed_verifier_commit import _durable_state, _validation, _raw_verified_outcome, _attach_semantic_report

HISTORY = Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789471955')
REPORTS = ('bfc96a2038a28b03972fc380ce23fe5cd934e23d7f908a7b430b8cf99d17eb6d',
           'bfa8d0c46f6d8dafec0798ff6a01504513629180d48ea51fdad960c6a72a0118',
           'e56c71385ecdc865a1633a1e4467eeab0b87b0a16b1e5b5f7c437a5a02f29f1e')

def historical():
    return json.loads((HISTORY/'plan_state.json').read_text())

@pytest.mark.parametrize('digest', REPORTS)
def test_T01_historical_report_immutable_binding_projection(digest,tmp_path):
    plan = historical()
    report = json.loads((HISTORY/'state_contract/objects'/f'{digest}.json').read_text())['payload']['body']
    assert all(row['value_present'] is False for row in report['effect_presence']['observed']['produces_bindings'])
    descriptors = (plan['task_state']['inspection_ref_registry']).values()
    descriptor = next(row for row in descriptors if row.get('scope', {}).get('object_kind') == 'validation'
        and row['scope']['source_object_id'] == report['scope']['validation_id'])
    validation_digest = descriptor['locator']['object_ref'].split(':')[-1]
    validation = json.loads((HISTORY/'state_contract/objects'/f'{validation_digest}.json').read_text())['payload']['body']
    facts = _derive_report_facts(plan_state=plan, validation=validation, execution_evidence={},
        predicate_results=report['predicate_results'], completion_gate=report['completion_gate'], semantic_review_required=True)
    expected = plan['provenance_state']['actions'][report['scope']['action_id']]['normalized_action']['produced_bindings']
    actual = facts['effect_presence']['observed']['produces_bindings']
    assert {row['identity'] for row in actual} == set(expected)
    for row in actual:
        assert row['value_present'] is True
        assert row['value'] == expected[row['identity']]['value']
        assert row['value_type'] == expected[row['identity']]['type']
        assert row['acceptance_status'] == 'not_accepted'
        assert row['attempt_id'] == report['scope']['attempt_id']
    dependencies = facts['effect_presence']['declared_dependency_facts']
    assert any(row['step_id']=='check_contig_compatibility' and row['status']=='committed' for row in dependencies)
    assert all(row.get('parser_evidence_status') != 'recorded' for row in dependencies)
    assert facts['effect_presence']['workspace_contracts']
    assert report['effective_disposition'] == 'inspection_required'  # no historical approval manufactured
    (tmp_path/f'fact_projection_{digest}.json').write_text(json.dumps({
        'original_report_id':report['report_id'],'attempt_id':report['scope']['attempt_id'],
        'before':report['effect_presence']['observed']['produces_bindings'],'after':actual,
        'dependency_facts':dependencies,'after_effect_presence_chars':len(json.dumps(facts['effect_presence'],ensure_ascii=False)),
        'before_effect_presence_chars':len(json.dumps(report['effect_presence'],ensure_ascii=False)),
        'source_report_object_digest':digest,'source_validation_object_digest':validation_digest,
        'scope':'offline production projection only; no historical Commit or review rewritten'},indent=2))


def test_T01_missing_or_foreign_action_never_fabricates_value():
    plan = _durable_state()
    assert candidate_binding_facts(plan, _validation()) == []
    plan['provenance_state']['actions']['action-1'] = {'plan_id': 'other', 'normalized_action': {'produced_bindings': {'x': {'value': True}}}}
    assert candidate_binding_facts(plan, _validation()) == []


def committed_fixture():
    plan = _durable_state()
    step = plan['steps'][0]
    step.update(status='passed', input_artifact_ids=['input-vcf'])
    plan['provenance_state']['artifact_heads'] = {'input-vcf':'version-1'}
    plan['provenance_state']['artifacts'] = {'version-1': {'sha256':'fixture-content-v1'}}
    validation = _validation()
    validation['effect_results'] = {'produces_observations':[{'observation_id':'contigs_compatible','value':True}]}
    plan['provenance_state']['validations']['validation-1'] = validation
    plan['task_state']['typed_step_commit_receipts'] = {'commit-1': {
        'outcome': {'scope': {'task_id':plan['task_id']}, 'payload':{'completed_step_id':'step-1'}, 'evidence_refs':['validation-1']},
        'successor_projection': {key: plan[key] for key in ('plan_id','plan_version')},
        'dependency_evidence': {'plan_revision':2, 'step_effect': __import__('agent_core.step_effects', fromlist=['canonical_step_effect']).canonical_step_effect(step),
            'input_artifact_ids':['input-vcf'], 'input_scope':dependency_input_scope(plan,step),
            'predicate_findings':[{'predicate_id':'name_compatibility_only','passed':True,'receipt_ref':'parser-fixture'}]}}}
    return plan

@pytest.mark.parametrize('change', ['none','missing','revision','input','undeclared','effect','scope'])
def test_T02_declared_dependency_applicability(change):
    plan = committed_fixture()
    if change=='missing': plan['task_state']['typed_step_commit_receipts']={}
    if change=='revision': plan['plan_revision']+=1
    if change=='input': plan['provenance_state']['artifact_heads']['input-vcf']='version-2'
    if change=='undeclared': plan['steps'][1]['depends_on']=[]
    if change=='effect': plan['steps'][0]['step_effect']={'schema_version':1,'produces_bindings':['changed']}
    if change=='scope': plan['task_state']['typed_step_commit_receipts']['commit-1']['outcome']['scope']['task_id']='foreign'
    rows = committed_dependency_facts(plan,plan['steps'][1])
    if change=='none':
        assert rows[0]['status']=='committed'
        assert rows[0]['predicate_findings'][0]['receipt_ref']=='parser-fixture'
        assert 'no additional scientific conclusion' in rows[0]['applicability']
    elif change=='undeclared': assert rows==[]
    else: assert rows[0]['status'] != 'committed'






def test_T04_reviewer_identity_uses_evidence_not_receipt_wrappers():
    from agent_core.semantic_completion import reviewer_assessment_identity
    plan=_durable_state()
    report={'scope':{**{k:plan[k] for k in ('task_id','plan_id','plan_version')},'step_id':'step-1','action_id':'action-1','attempt_id':'attempt-1'},
        'effect_presence':{'revalidation_inspection_facts':[{'decision_ref':'read-1','observation':{'source_ref':'ref-a','content_sha256':'digest-a'}}]}}
    first=reviewer_assessment_identity(report,plan,{})
    report['scope']['state_generation']=900;report['report_id']='new-report'
    report['effect_presence']['revalidation_inspection_facts'][0]['decision_ref']='new-wrapper'
    assert reviewer_assessment_identity(report,plan,{})==first
    report['effect_presence']['revalidation_inspection_facts'][0]['observation']['content_sha256']='digest-new'
    assert reviewer_assessment_identity(report,plan,{})!=first

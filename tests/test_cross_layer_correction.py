"""Current same-layer correction acceptance, not historical witness assertions."""
from supervisor import parse_review_decision
from copy import deepcopy
import json
import pytest
from agent_core.semantic_completion import (validate_reviewer_payload,SemanticCompletionError,
    reviewer_response_schema,reviewer_assessment_identity,REVIEWER_PROTOCOL_CORRECTIONS)


def decision():
    return {'report_id':'current-report','assessment':'ACCEPTABLE','disposition':'completion_accepted',
        'concerns':[],'suggestions':[],'confidence':0.9,'issue_dispositions':[
            {'issue_id':'issue','disposition':'completion_accepted','explanation':'Current execution only.'}]}


def test_F10_F13_parser_preserves_exact_errors_and_scope():
    value=decision();value['issue_dispositions'][0]['confidence']=0.9
    parsed=parse_review_decision(json.dumps(value),expected_report_id='current-report',allowed_issue_ids=['issue'],include_error=True)
    assert parsed['_validation_error']['field_path']=='$.issue_dispositions[0].confidence'
    assert parsed['_validation_error']['expected']==reviewer_response_schema()['issue_fields']
    value=decision();value['report_id']='old-report'
    assert parse_review_decision(json.dumps(value),expected_report_id='current-report',include_error=True)['_validation_error']['code']=='SEMANTIC_REVIEW_REPORT_ID_MISMATCH'
    value=decision();value['review_policy_receipt']={'remaining_requests':1000}
    with pytest.raises(SemanticCompletionError,match='FIELD_UNKNOWN'):validate_reviewer_payload(value)
    value=decision();value.update(assessment='CONCERN',disposition='inspection_required')
    value['issue_dispositions'][0]['disposition']='inspection_required'
    parsed=parse_review_decision(json.dumps(value),expected_report_id='current-report',allowed_issue_ids=['issue'],include_error=True)
    assert parsed['parse_status']=='valid' and parsed['disposition']=='inspection_required'


def test_F12_assessment_scope_ignores_wrappers_not_evidence():
    report={'scope':{'task_id':'new','plan_id':'plan','plan_version':'v1','step_id':'check','action_id':'action','attempt_id':'attempt','validation_id':'v1','state_generation':3},
        'report_id':'report-a','process_outcome':{'returncode':0},'effect_presence':{'observed':{'obs':{'value':True}}},
        'predicate_results':[{'result_id':'result1','predicate_id':'required-check','passed':True,'authoritative_actual':True}]}
    state={'plan_revision':1,'steps':[{'step_id':'check','step_effect':{'schema_version':1,'produces_observations':[{'identity':'obs','evidence_contract':{'required_fields':['value']}}]}}]}
    hashes={'executed_action':'actual-code-sha','stdout':'actual-output-sha','stderr':'actual-error-sha'}
    first=reviewer_assessment_identity(report,state,hashes)
    renamed=deepcopy(report);renamed.update(report_id='report-b',inspection_refs=['inspection:new'])
    renamed['scope'].update(validation_id='v2',state_generation=900)
    renamed['predicate_results'][0]['result_id']='result2'
    assert reviewer_assessment_identity(renamed,state,hashes)==first
    renamed['effect_presence']['observed']['obs']['value']=False
    assert reviewer_assessment_identity(renamed,state,hashes)['assessment_id']!=first['assessment_id']
    assert REVIEWER_PROTOCOL_CORRECTIONS==2


def test_F08_bounded_compile_checks_exact_static_source(tmp_path):
    from execution.script_validation import check_script_body
    bad="python - <<'PY'\nprint({'missing': 1)\nPY\n"
    row=check_script_body(bad,'bash')
    assert row['status']=='invalid' and row['source_line']==2,row
    assert row['static_python_fragments'][0]['execution_started'] is False
    marker=tmp_path/'MUST_NOT_EXECUTE'
    source='from pathlib import Path\nPath('+repr(str(marker))+').write_text("forbidden")\n'
    assert check_script_body(source,'python')['status']=='valid'
    assert not marker.exists()
    assert check_script_body("python - <<PY\nprint($runtime)\nPY\n",'bash')['uncovered']
    assert check_script_body('x'*524289,'python')['code']=='SCRIPT_SYNTAX_CHECK_SIZE_LIMIT'
    assert check_script_body('print(1)\n','python',timeout=0.01)['status'] in {'valid','unavailable'}
    assert check_script_body('print(1)','r')['status']=='unavailable'


def test_F14_F17_policy_scope_freeze_and_classification():
    from agent_core.candidate_repair_policy import freeze_candidate_policy,correction_category
    ledger={};scope={'task_id':'new','plan_id':'plan','plan_version':'v1','step_id':'s'}
    first=freeze_candidate_policy(ledger,scope,{})
    assert first['limit']==4 and first['unit']=='additional_candidate_calls'
    assert freeze_candidate_policy(json.loads(json.dumps(ledger)),scope,{'max_candidate_repairs_per_epoch':90})==first
    other=freeze_candidate_policy(ledger,{**scope,'plan_version':'v2'},{'max_candidate_repairs_per_epoch':2})
    assert other['limit']==2
    legacy={'events':{'old':{'scope':scope,'repair_limit':1,'repair_ordinal':1,'event_id':'old'}}}
    assert freeze_candidate_policy(legacy,scope,{})['version']=='legacy-recorded'
    assert legacy['events']['old']['repair_limit']==1
    assert correction_category('script_block',[{'protocol_error':{'cause_code':'GENERATE_RESPONSE_INCOMPLETE'}}])=='response_completion'







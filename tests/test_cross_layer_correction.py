"""Current same-layer correction acceptance, not historical witness assertions."""
from copy import deepcopy
import json
import pytest
from agent_core.semantic_completion import (validate_reviewer_payload,SemanticCompletionError,
    reviewer_response_schema,reviewer_assessment_identity,REVIEWER_PROTOCOL_CORRECTIONS)
from supervisor import LLMReviewer


def decision():
    return {'report_id':'current-report','assessment':'ACCEPTABLE','disposition':'completion_accepted',
        'concerns':[],'suggestions':[],'confidence':0.9,'issue_dispositions':[
            {'issue_id':'issue','disposition':'completion_accepted','explanation':'Current execution only.'}]}


def test_F10_F13_parser_preserves_exact_errors_and_scope():
    value=decision();value['issue_dispositions'][0]['confidence']=0.9
    parsed=LLMReviewer._parse_decision(json.dumps(value),expected_report_id='current-report',allowed_issue_ids=['issue'],include_error=True)
    assert parsed['_validation_error']['field_path']=='$.issue_dispositions[0].confidence'
    assert parsed['_validation_error']['expected']==reviewer_response_schema()['issue_fields']
    value=decision();value['report_id']='old-report'
    assert LLMReviewer._parse_decision(json.dumps(value),expected_report_id='current-report',include_error=True)['_validation_error']['code']=='SEMANTIC_REVIEW_REPORT_ID_MISMATCH'
    value=decision();value['review_policy_receipt']={'remaining_requests':1000}
    with pytest.raises(SemanticCompletionError,match='FIELD_UNKNOWN'):validate_reviewer_payload(value)
    value=decision();value.update(assessment='CONCERN',disposition='inspection_required')
    value['issue_dispositions'][0]['disposition']='inspection_required'
    parsed=LLMReviewer._parse_decision(json.dumps(value),expected_report_id='current-report',allowed_issue_ids=['issue'],include_error=True)
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

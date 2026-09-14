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


def test_F19_safe_output_policy_unknown_and_bound_override():
    from types import SimpleNamespace
    from agent_core.provider_attempt import output_policy_projection
    assert output_policy_projection(object())['client_resolved_output_limit']=='UNKNOWN'
    base=SimpleNamespace(max_tokens=4096,model_kwargs={'api_key':'synthetic-do-not-project'})
    bound=SimpleNamespace(bound=base,kwargs={'max_tokens':6144,'headers':{'synthetic':'do-not-project'}})
    result=output_policy_projection(bound,requested={'max_tokens':4096})
    assert result['client_resolved_output_limit']==6144 and result['overridden'] is True
    assert result['remote_effective_limit']=='UNKNOWN' and 'api_key' not in json.dumps(result)


def test_F19_actual_generate_transport_preserves_explicit_output_limit():
    import asyncio
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.test_preflight_recovery_audit import save
    async def run():
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp();captured=[]
        class Transport:
            @property
            def with_raw_response(self):return self
            async def create(self,**body):
                captured.append(body)
                payload={'id':'new-offline-response','model':'offline-model','object':'chat.completion','created':1,
                    'choices':[{'index':0,'message':{'role':'assistant','content':'<shell>printf synthetic</shell>'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':3,'completion_tokens':4,'total_tokens':7}}
                from types import SimpleNamespace
                return SimpleNamespace(parse=lambda:payload,headers={})
        try:
            model=ChatOpenAI(model='offline-model',api_key='offline-fixture',base_url='https://offline.invalid/v1',
                max_tokens=2048,max_retries=0,async_client=Transport()).bind(max_tokens=3072)
            case.agent.generate_requested_output_limits={'max_tokens':2048}
            result=await case.agent._ainvoke_llm([HumanMessage(content='Only a synthetic script.')],stage='generate',
                task_id='new-output-policy',llm_override=model,llm_policy={'sdk_max_retries':0})
            assert len(captured)==1 and result.response_metadata['finish_reason']=='stop'
            limits={k:v for k,v in captured[0].items() if k in {'max_tokens','max_completion_tokens','max_output_tokens'}}
            assert list(limits.values())==[3072],limits
            from agent_core.context_lineage import ContextLineageStore
            snapshots=ContextLineageStore(case.root,'new-output-policy').list_snapshots()
            policies=[row for row in snapshots]
            save('F19-real-adapter',{'final_transport_limits':limits,'calls':1,'network':0,
                'response_metadata':result.response_metadata,'snapshots':policies})
        finally:await case.asyncTearDown()
    asyncio.run(run())

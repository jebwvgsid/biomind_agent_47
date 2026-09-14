"""F3 FIX checks: actual parser/Plan/TypedGenerate and ledger; fixed provider only.

No execution is dispatched. A separate combined suite crosses real approval,
execution, evidence and Commit. Every task/state here is new and isolated.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest

from bio_agents import BioAnalystAgent
from agent_core.workflow.candidate_repair import (
    candidate_progress_assessment, candidate_repair_fingerprint,
)
from tests.test_preflight_recovery_audit import save


def action(**updates):
    return {'action_type':'execute','operation':'check_contig_compatibility',
        'argv':['/usr/bin/printf','synthetic'], 'env':{}, 'outputs':[], **updates}


@pytest.mark.parametrize('env,actual', [([], 'array'), (None,'null'), ('text','string'), ({'VALUE':7},'number')])
def test_env_parser_preserves_first_failure_without_fingerprint_exception(env,actual):
    candidate=action(env=env)
    parsed=BioAnalystAgent._extract_structured_action_result(json.dumps(candidate))
    assert not parsed.succeeded
    assert parsed.error['code']=='ACTION_SCHEMA_TYPE_ERROR'
    assert parsed.error['field_path']==('env.VALUE' if isinstance(env,dict) else 'env')
    assert parsed.error['actual']==actual
    assert parsed.candidate==candidate
    assert len(candidate_repair_fingerprint(parsed.candidate))==64


@pytest.mark.parametrize('field,value', [('code',[]),('body',{}),('command',None),('operation',{})])
def test_execute_field_type_is_not_silently_stringified(field,value):
    parsed=BioAnalystAgent._structured_action_from_object_result(action(**{field:value}))
    assert parsed.error['field_path']==field
    assert parsed.error['expected']=='string'


async def graph_sequence(mode,env):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol,generate_until_ready_or_stopped
    from agent_core.plan_tracker import load_plan_state,save_plan_state,load_approved_plan_snapshot
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    case.SKILL_ID='genomics/snpeff-annotation'
    await case.asyncSetUp()
    task='f3-'+mode+'-'+type(env).__name__
    try:
        configure_protocol(case);case._configure_one_step_no_output_plan()
        original=case._planner_payload
        def plan():
            p=original();p['steps'][0].update(
                execution_capability_id='genomics.snpeff_annotation.check_contig_compatibility',
                operation='check_contig_compatibility',produces_observations=[{
                    'identity':'contigs_compatible','evidence_contract':{'required_fields':['value']}}])
            return p
        case._planner_payload=plan
        base=action()
        missing=deepcopy(base);missing.pop('action_type')
        responses=['{"unclosed":',json.dumps(missing),json.dumps(action(env=env)),
                   json.dumps(action(env={'VALUE':7})),json.dumps(base)]
        if mode=='empty_object':responses[1]='{}'
        if mode=='blocked':responses=responses[:3]+[json.dumps({'action_type':'blocked','reason':'No new basis for a safe correction.','missing_evidence':['synthetic_required_measurement']})]
        if mode=='repeat':responses=[json.dumps(action(env=env))]*2
        if mode=='uuid':responses=[json.dumps(action(env=env,action_id='framework-001')),json.dumps(action(env=env,action_id='framework-002'))]
        if mode in ('helper_checkpoint','syntax_checkpoint'):
            wrong={'action_type':'execute','operation':'wrong-operation','language':'python','env':{},'outputs':[]}
            if mode=='helper_checkpoint':
                bad="if True:\n  # BIOMIND_EFFECT_HELPER\n  print('synthetic')\n"
                good="# BIOMIND_EFFECT_HELPER\nprint('synthetic')\n"
            else:
                bad="if True print('synthetic')\n"
                good="print('synthetic')\n"
            responses=[json.dumps({**wrong,'code':bad}),json.dumps({**wrong,'code':good}),
                json.dumps({**wrong,'code':good,'operation':'check_contig_compatibility'})]
        replay_record={}
        def provider(payload,ordinal):
            assert ordinal<=len(responses),(ordinal,mode)
            if mode=='replay' and ordinal==2:
                before=load_plan_state(case.root,task)
                original_row=next(iter(before['task_state']['candidate_repair_state']['events'].values()))
                repeated=case.agent._candidate_admission_rejection_update(
                    task_id=task,plan_state=before,
                    approved_plan_snapshot=load_approved_plan_snapshot(case.root,task,state=before),
                    step_id=before['current_step_id'],
                    candidate=original_row['recovery_context']['action_contract']['structured_candidate'],
                    candidate_fingerprint=original_row['candidate_fingerprint'],previous_candidate_fingerprint='',
                    violations=original_row['exact_violations'],visible_artifacts={},
                    repair_domain=original_row['repair_domain'],repair_stage=original_row['repair_stage'],
                    validation_source=original_row['validation_source'],contract_ref=original_row['contract_ref'],
                    exit_reason=original_row['exit_reason'],candidate_event_id=original_row['candidate_event_id'])
                after=load_plan_state(case.root,task)
                assert before['state_generation']==after['state_generation']
                assert repeated['candidate_rejection_event']['event_id']==original_row['event_id']
                assert repeated['candidate_repair_receipt']['receipt_ref']==original_row['receipt_ref']
                assert len(after['task_state']['candidate_repair_state']['events'])==1
                replay_record.update(state_generation=after['state_generation'],event_count=1,
                    receipt_ref=original_row['receipt_ref'],same_event_and_receipt=True)
            if mode=='legacy' and ordinal==3:
                legacy=load_plan_state(case.root,task)
                for row in legacy['task_state']['candidate_repair_state']['events'].values():
                    row.pop('progress_assessment',None)
                # Remove only optional new diagnostic summaries. The actual
                # parser candidate, receipts, approvals, counts and statuses stay.
                save_plan_state(case.root,task,legacy)
            return responses[ordinal-1]
        events,requests,_=await generate_until_ready_or_stopped(case,task,provider)
        durable=load_plan_state(case.root,task)
        rows=sorted((durable['task_state'].get('candidate_repair_state',{}).get('events') or {}).values(),key=lambda r:r['repair_ordinal'])
        outcomes=[u['generate_outcome'] for e in events for u in e.values() if isinstance(u,dict) and u.get('generate_outcome')]
        result={'mode':mode,'env_type':type(env).__name__,'provider_calls':len(requests),'replay':replay_record,
            'outcomes':outcomes,'rows':rows,'pending_action':durable['task_state'].get('pending_action'),
            'requests_contain_prior_exact_error':[r.get('_provider_request','').find('ACTION_SCHEMA_TYPE_ERROR')>=0 for r in requests],
            'loaded_bio_agents_sha256':hashlib.sha256(Path(__import__('bio_agents').__file__).read_bytes()).hexdigest(),
            'boundary':'real Plan/approval/TypedGenerate/parser/CAS/ledger, fixed provider; no execution dispatched'}
        save(task,result)
        assert len(requests)==len(responses),result
        assert all(o.get('payload',{}).get('error_code')!='generate_exception' for o in outcomes),result
        if mode in ('valid','empty_object','legacy','replay'):
            assert outcomes[-1]['type']=='action_ready',result
            assert len(rows)==4
            assert [r['reason_code'] for r in rows]==['JSON_DECODE_ERROR','ACTION_SCHEMA_REQUIRED_FIELD','ACTION_SCHEMA_TYPE_ERROR','ACTION_SCHEMA_TYPE_ERROR']
            assert all(r['progress_proven'] for r in rows)
            assert rows[-1]['policy_receipt']['remaining']==0
            if mode!='legacy':assert rows[0]['progress_assessment']['binding_axis']=='not_evaluated'
            else:assert 'progress_assessment' not in rows[0], 'historical rows must not be rewritten'
            assert 'execute.env_object' in rows[-1]['progress_assessment']['newly_satisfied_targets']
            assert 'progress_assessment' in requests[-1]['_provider_request']
            assert 'env.VALUE' in requests[-1]['_provider_request']
        elif mode in ('helper_checkpoint','syntax_checkpoint'):
            assert outcomes[-1]['type']=='action_ready',result
            assert len(rows)==2 and all(r['progress_proven'] for r in rows)
            target='effect_helper_materialization' if mode=='helper_checkpoint' else 'executable_syntax'
            assert target not in rows[0]['progress_assessment']['representation_targets']
            assert target in rows[1]['progress_assessment']['newly_satisfied_targets'],result
            assert rows[1]['policy_decision']['kind']=='RETRY_CURRENT_ACTION',result
            assert rows[1]['policy_receipt']['remaining']==2
        elif mode=='blocked':
            assert outcomes[-1]['type']=='blocked',result
            assert not durable['task_state'].get('pending_action')
            assert rows[-1]['policy_receipt']['remaining']==1
        else:
            assert rows[-1]['progress_assessment']['basis']=='recurrent_rejected_candidate'
            assert rows[-1]['termination_reason']=='repair_no_progress'
            assert rows[-1]['policy_receipt']['remaining']==3
            assert not durable['task_state'].get('pending_action')
        return result
    finally:await case.asyncTearDown()


@pytest.mark.parametrize('env',[[],None,'text'])
def test_multiround_same_script_reaches_admission_on_last_allowed_response(env):
    asyncio.run(graph_sequence('valid',env))


def test_decoded_empty_object_preserves_json_progress():
    asyncio.run(graph_sequence('empty_object',[]))


@pytest.mark.parametrize('mode',['blocked','repeat','uuid'])
def test_formal_stop_and_genuine_repeat(mode):
    asyncio.run(graph_sequence(mode,[]))


def assess(candidate,targets,prior=(),facts=None,code='SAME_ERROR'):
    return candidate_progress_assessment(candidate=candidate,representation_targets=targets,
        binding_facts=facts,violations=[{'code':code,'field_path':'env'}],prior_rows=prior,contract_ref={})


def row(candidate,assessment,ordinal):
    return {'candidate_fingerprint':candidate_repair_fingerprint(candidate),
        'progress_assessment':assessment,'repair_ordinal':ordinal}


def test_aba_error_code_and_system_uuid_cannot_manufacture_progress():
    a=action(env=[]);b=action(env={'VALUE':7})
    targets=['json_object','action_type']
    first=assess(a,targets)
    second=assess(b,targets+['execute.env_object'],[row(a,first,1)])
    assert second['progress_proven']
    recurrence=assess(a,targets,[row(a,first,1),row(b,second,2)],code='DIFFERENT_ERROR')
    assert not recurrence['progress_proven'] and recurrence['basis']=='recurrent_rejected_candidate'
    unchanged=assess({**a,'action_id':'new-framework-uuid'},targets,[row(a,first,1)],code='DIFFERENT_ERROR')
    assert not unchanged['progress_proven']
    unrelated=assess({**a,'purpose':'changed prose'},targets,[row(a,first,1)],code='OTHER_CODE')
    assert not unrelated['progress_proven'] and unrelated['basis']=='no_validated_target_progress'


def test_recovery_policy_consumes_same_trusted_progress_for_same_candidate():
    from agent_core.recovery import RecoveryPolicy
    base={'valid':False,'repairable':True,'repair_domain':'generated_semantic',
        'failure_owner':'GENERATED_ACTION','candidate_fingerprint':'same','previous_candidate_fingerprint':'same',
        'violations':['generated binding failure']}
    policy=RecoveryPolicy(max_candidate_repairs=4)
    for progress in (True,False):
        evidence={**base,'candidate_progress':{'producer':'biomind.generate_candidate_admission.v1','progress_proven':progress}}
        decision=policy.decide(pre_action_evidence=evidence,budgets={'generated_semantic_repair_count':1})
        assert decision.kind==('RETRY_CURRENT_ACTION' if progress else 'ESCALATE'),decision.to_dict()


def test_comparison_keeps_permissions_effects_program_and_versions():
    from agent_core.workflow.candidate_repair import candidate_progress_view
    candidate=action(generation_provenance={'mode':'RECOVERY','plan_revision':1,
        'schema_version':1,'recovery_id':'temporary-1','recovery_envelope_id':'temporary-packet-1'},
        evidence_version='1',produced_bindings={'workspace':{'binding_id':'authority-1'}},
        effect_obligations=['must-measure'])
    wrapped=deepcopy(candidate);wrapped['generation_provenance']['recovery_id']='temporary-2'
    assert candidate_progress_view(candidate)==candidate_progress_view(wrapped)
    assert candidate_repair_fingerprint(candidate)!=candidate_repair_fingerprint(wrapped)
    for name,value in [('argv',['/usr/bin/printf','changed']),('evidence_version','2'),
            ('produced_bindings',{'workspace':{'binding_id':'authority-2'}}),('effect_obligations',['other'])]:
        changed={**candidate,name:value}
        assert candidate_progress_view(candidate)!=candidate_progress_view(changed)
    changed=deepcopy(candidate);changed['generation_provenance']['plan_revision']=2
    assert candidate_progress_view(candidate)!=candidate_progress_view(changed)


def test_legacy_progress_metadata_is_compared_without_rewriting_history():
    asyncio.run(graph_sequence('legacy',[]))


def test_exact_rejection_event_replay_does_not_charge_or_rewrite():
    asyncio.run(graph_sequence('replay',[]))


@pytest.mark.parametrize('mode',['helper_checkpoint','syntax_checkpoint'])
def test_actual_helper_and_syntax_success_survive_next_real_admission_rejection(mode):
    asyncio.run(graph_sequence(mode,[]))

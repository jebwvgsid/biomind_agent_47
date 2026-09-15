"""Bounded carrier/progress checks; run only in the guarded offline export."""
from copy import deepcopy
import random
from unittest.mock import patch
import pytest
from agent_core.step_effects import materialize_effect_action
from execution.script_validation import check_executable_action
from agent_core.workflow.candidate_repair import candidate_progress_assessment
from agent_core.recovery import RecoveryPolicy
from tests.a1_unified_fix.test_f6_graph import save

BODY = "value = 2 + 3\nassert value == 5\n"


def carrier(kind, body=BODY):
    argv = ['python', '-I', '-S', '-c', body]
    if kind == 'code': return {'type': 'execute', 'language': 'python', 'code': body}
    if kind == 'both': return {'type': 'execute', 'language': 'python', 'code': body, 'argv': argv}
    if kind == 'argv': return {'type': 'execute', 'argv': argv}
    return {'type': 'execute', 'argv': ['bash', '--noprofile', '--norc', '-c', "python - <<'PY'\n"+body+'PY\n']}


@pytest.mark.parametrize('kind', ['code','both','argv','wrapper'])
@pytest.mark.parametrize('body,expected', [(BODY,'PASS'), ('if broken syntax\n','FAIL')])
def test_actual_carrier_checked(kind, body, expected):
    action=materialize_effect_action(carrier(kind,body), {})
    result=check_executable_action(action)
    assert result['check_status']==expected, result
    assert result['execution_started'] is False
    if kind!='wrapper':
        assert result['body_sha256']==check_executable_action(carrier('code',body))['body_sha256']


def test_checker_states_and_conflicting_carrier():
    assert check_executable_action({'argv':['/usr/bin/printf','hello']})['check_status']=='NOT_APPLICABLE'
    assert check_executable_action({'argv':['python','new-approved-file.py']})['check_status']=='NOT_APPLICABLE'
    assert check_executable_action({'code':'unknown source','language':'unknown'})['check_status']=='NOT_EVALUATED'
    with patch('execution.script_validation.check_script_body',side_effect=RuntimeError('new isolated injection')):
        assert check_executable_action(carrier('argv'))['check_status']=='CHECK_ERROR'
    with pytest.raises(ValueError,match='BODY_ARGV_MISMATCH'):
        materialize_effect_action({**carrier('both'),'code':'different = True'}, {})
    with pytest.raises(ValueError,match='LANGUAGE_ARGV_MISMATCH'):
        materialize_effect_action({**carrier('argv'),'language':'bash'}, {})


def assess(candidate, checks=(), prior=(), bindings=()):
    return candidate_progress_assessment(candidate=candidate,representation_targets=['json_object'],
        binding_facts=bindings,violations=[{'field_path':'binding_env','code':'INVALID'}],
        prior_rows=prior,contract_ref={'operation':'prepare_database'},check_records=checks)


@pytest.mark.parametrize('kind',['code','both','argv','wrapper'])
@pytest.mark.parametrize('status',['PASS','FAIL','NOT_EVALUATED','NOT_APPLICABLE','CHECK_ERROR','MISSING'])
def test_partial_repair_and_unknown_do_not_authorize_heuristic_stop(kind,status):
    a=carrier('both');a['binding_env']={'NAME':'wrong'}
    first=assess(a,[{'check_id':'executable_syntax','check_status':'PASS'}])
    first['representation_targets'].append('executable_syntax')
    first['pending_targets'].append('operation')
    b={**carrier(kind),'operation':'prepare_database','binding_env':a['binding_env']}
    checks=[] if status=='MISSING' else [{'check_id':'executable_syntax','check_status':status}]
    second=assess(b,checks,[{'repair_ordinal':1,'progress_assessment':first}],['operation'])
    assert second['resolved_targets']==['operation']
    assert second['progress_proven'] and not second['repeat_proven']
    assert second['check_records']['executable_syntax']['check_status']==('NOT_EVALUATED' if status=='MISSING' else status)
    decision=RecoveryPolicy(max_candidate_repairs=4).decide(pre_action_evidence={
        'valid':False,'repairable':True,'repair_domain':'generated_semantic','failure_owner':'GENERATED_ACTION',
        'candidate_progress':second,'violations':['binding_env invalid']},budgets={'generated_semantic_repair_count':1})
    assert decision.kind=='RETRY_CURRENT_ACTION'


def test_bounded_sequences_progress_is_diagnostic_repeat_is_evidence():
    seed=1789464978;rng=random.Random(seed);traces=[]
    for case in range(100):
        rows=[];accepted=0
        for ordinal in range(1,21):
            candidate={**carrier(rng.choice(['code','argv','both','wrapper'])),
                'operation':rng.choice(['prepare_database','wrong']), 'env':{'V':str(rng.randrange(3))}}
            check=[{'check_id':'executable_syntax','check_status':rng.choice(['PASS','FAIL','NOT_EVALUATED','NOT_APPLICABLE','CHECK_ERROR'])}]
            got=assess(candidate,check,rows,['operation'] if candidate['operation']=='prepare_database' else [])
            decision=RecoveryPolicy(max_candidate_repairs=4).decide(pre_action_evidence={
                'valid':False,'repairable':True,'repair_domain':'generated_semantic',
                'failure_owner':'GENERATED_ACTION','candidate_progress':got,
                'violations':['binding_env invalid']},budgets={'generated_semantic_repair_count':accepted})
            allowed=decision.kind=='RETRY_CURRENT_ACTION'
            # Independent oracle: only a proven repeat or the fixed cap ends
            # this repairable sequence, never a diagnostic progress score.
            assert allowed==(not got['repeat_proven'] and accepted<4)
            traces.append({'case':case,'ordinal':ordinal,'allowed':allowed,'progress':got['progress_proven'],
                'repeat':got['repeat_proven'],'remaining_before':4-accepted})
            if not allowed:break
            accepted+=1;rows.append({'repair_ordinal':ordinal,'progress_assessment':got})
        assert accepted<=4 and ordinal<=5
    assert any(not t['progress'] and t['allowed'] for t in traces)
    assert any(t['repeat'] for t in traces)
    save('generate-class-sequences',{'seed':seed,'sequences':100,'max_test_actions':20,'traces':traces,
        'scope':'pure production assessment/policy matrix; graph ledger and claims are separate integration tests'})


@pytest.mark.parametrize('provided', ['omitted','correct','value','source','extra','null','array'])
def test_derived_binding_source_is_optional_but_explicit_errors_survive(provided):
    from tests.test_binding_normalization_protocol import invocation
    from agent_core.plan_bindings import normalize_public_action_bindings, PlanBindingError
    action,frozen,row=invocation()
    mapping=frozen['binding_contract']['operations']['prepare']['required_binding_env']
    values={'correct':mapping,'value':{'DB_PATH':'actual-path'},
        'source':{'DB_PATH':{'source':'accepted_binding','name':'db'}},
        'extra':{**mapping,'OTHER':{'source':'produced_binding','name':'db'}},'null':None,'array':[]}
    if provided!='omitted':action['binding_env']=deepcopy(values[provided])
    original=deepcopy(action)
    if provided in {'omitted','correct'}:
        normalized,receipt=normalize_public_action_bindings(action,frozen=frozen,scope={'task_id':'new'},workspace_bindings=[row])
        assert normalized['binding_env']==mapping
        assert receipt['raw_candidate']==original
    else:
        with pytest.raises(PlanBindingError) as error:
            normalize_public_action_bindings(action,frozen=frozen,scope={'task_id':'new'},workspace_bindings=[row])
        facts=error.value.to_dict()
        assert facts['code']=='BINDING_ENV_CONTRACT_MISMATCH'
        assert facts['actual']==values[provided] and facts['expected']==mapping
        assert 'Delete optional binding_env' in facts['detail']
    assert action==original


def test_r_syntax_unavailable_is_not_an_invented_pass_or_new_execution_ban():
    record=check_executable_action({'argv':['Rscript','-e','x <- 1']})
    assert record['check_status']=='NOT_EVALUATED' and record['required'] is False


def test_checker_internal_failure_does_not_blame_model_or_spend_repair_budget():
    import asyncio,json
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol,generate_until_ready_or_stopped
    from agent_core.plan_tracker import load_plan_state
    async def run():
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        case.SKILL_ID='genomics/snpeff-annotation'
        await case.asyncSetUp()
        task='category-checker-failure'
        try:
            capability='genomics.snpeff_annotation.check_contig_compatibility'
            configure_protocol(case,execution_capability_id=capability)
            case._configure_one_step_no_output_plan(execution_capability_id=capability)
            original_plan=case._planner_payload
            def plan():
                value=original_plan()
                value['steps'][0].update(operation='check_contig_compatibility',
                    produces_observations=[{'identity':'contigs_compatible',
                        'evidence_contract':{'required_fields':['value']}}])
                return value
            case._planner_payload=plan
            def provider(payload,ordinal):
                assert ordinal==1
                return json.dumps({'action_type':'execute','operation':'check_contig_compatibility',
                    'language':'python','code':"print('isolated candidate, never executed')\n"})
            with patch('execution.script_validation.check_script_body',side_effect=RuntimeError('isolated checker fault')):
                events,requests,_=await generate_until_ready_or_stopped(case,task,provider)
            state=load_plan_state(case.root,task)
            save('checker-error-real-boundary',{'events':events,'requests':requests,'state':state})
            rows=list(state['task_state']['candidate_repair_state']['events'].values())
            assert len(rows)==len(requests)==1
            row=rows[0]
            assert row['terminal_reason_code']=='SCRIPT_SYNTAX_CHECK_INTERNAL_ERROR'
            assert row['violation_owner']=='INTERNAL_PROTOCOL'
            assert row['policy_receipt']['remaining']==4
            assert row['progress_assessment']['check_records']['executable_syntax']['check_status']=='CHECK_ERROR'
            assert not state.get('provenance_state',{}).get('attempts')
            save('checker-error-real-boundary',{'events':events,'requests':requests,'row':row})
        finally:await case.asyncTearDown()
    asyncio.run(run())

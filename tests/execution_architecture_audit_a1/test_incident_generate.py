"""Audit witnesses only: parser/helper/native generation/progress, no production fixes.

Provider responses and observations are explicitly synthetic. The real graph
constructs/approves a fresh Plan; no historical session is loaded or executed.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shlex
import pytest

from bio_agents import BioAnalystAgent
from agent_core.plan_bindings import binding_input_progress_facts
from agent_core.step_effects import declared_effect_emission_contract, effect_template_provider_view, materialize_effect_template, extract_structured_effect_evidence
from agent_core.workflow.candidate_repair import candidate_repair_fingerprint
from tests.test_preflight_recovery_audit import approved_tiny, save, PYTHON


def test_parser_preserves_distinct_representation_candidates():
    script="# BIOMIND_EFFECT_HELPER\nvalues = {str(n) for n in (1, 2)}\nprint(emit_effect_evidence({'renamed_observation': {'value': False}}))"
    fenced='```bash\n'+script+'\n```\nSynthetic public description.'
    wrapped=json.dumps({'action_json': {'operation':'synthetic_check','code':script}})
    first=BioAnalystAgent._extract_structured_action_result(fenced)
    second=BioAnalystAgent._extract_structured_action_result(wrapped)
    assert first.error['code']=='JSON_DECODE_ERROR'
    assert first.error['raw_excerpt'].startswith('{str(n)')
    assert second.error['code']=='ACTION_SCHEMA_REQUIRED_FIELD'
    assert second.error['field_path']=='action_type'
    assert candidate_repair_fingerprint(first.candidate)!=candidate_repair_fingerprint(second.candidate)
    requirements={'operation':'synthetic_check','produced_bindings':{},'consumed_bindings':{},'binding_env':{'declared_mapping':{}}}
    # Current frozen binding progress does not see JSON decoding/envelope repair.
    identities=[BioAnalystAgent._malformed_candidate_receipt_identity(fenced,parser_candidate=first.candidate),second.candidate]
    facts=[sorted(binding_input_progress_facts(x,requirements)) for x in identities]
    assert facts==[['binding_env'],['binding_env']]
    save('incident-parser',{'classification':'DEFECT_REPRODUCED_COMPONENT','errors':[first.error,second.error],
        'candidate_digests':[candidate_repair_fingerprint(x) for x in identities],'public_valid_facts':facts,'executions':0})


@pytest.mark.parametrize('mode',['bash','python'])
def test_actual_serialized_negative_fact_and_bad_fact_shape(tmp_path,mode):
    effect={'steps':[{'step_id':'renamed_step','produces_observations':[{'identity':'renamed_observation','evidence_contract':{'required_fields':['value']}}]}]}
    template=declared_effect_emission_contract(effect,'renamed_step')
    public=effect_template_provider_view(template)
    assert 'python_template' not in public
    correct="print(emit_effect_evidence({'produces_observations': {'renamed_observation': {'value': False}}}))\n"
    source=template['marker']+'\n'+correct
    frozen=materialize_effect_template(source,template)
    if mode=='bash':
        shell=shlex.quote(PYTHON)+" - <<'PY'\n"+frozen+'PY\n'
    else:
        script=tmp_path/'中文 空格 helper.py';script.write_text(frozen)
        shell=shlex.quote(PYTHON)+' '+shlex.quote(str(script))
    out,err,rc=asyncio.run(approved_tiny(tmp_path/'new-synthetic.sh',shell))
    assert rc==0 and not err
    parsed=extract_structured_effect_evidence(out)
    assert parsed['produces_observations'][0]['value'] is False
    bad=materialize_effect_template(template['marker']+"\nprint(emit_effect_evidence({'renamed_observation': {'value': False}}))\n",template)
    out2,err2,rc2=asyncio.run(approved_tiny(tmp_path/'new-bad-shape.sh',shlex.quote(PYTHON)+" - <<'PY'\n"+bad+'PY\n'))
    assert rc2==1 and 'EFFECT_FIELD_UNDECLARED' in err2 and not out2
    save('incident-helper-'+mode,{'classification':'CURRENT_BEHAVIOR_VERIFIED','language':mode,'rc':rc,'parsed':parsed,
        'invalid_shape_rc':rc2,'invalid_shape_error':'EFFECT_FIELD_UNDECLARED','template_sha256':template['template_sha256'],
        'execution_engine':'real execution.executor.run_command; fresh registered synthetic script; no scientific tools'})


async def native_scenario(mode):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol,generate_until_ready_or_stopped
    from agent_core.plan_tracker import load_plan_state
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    case.SKILL_ID='genomics/snpeff-annotation'
    await case.asyncSetUp()
    try:
        configure_protocol(case);case._configure_one_step_no_output_plan()
        original=case._planner_payload
        def plan():
            p=original();s=p['steps'][0]
            s.update(execution_capability_id='genomics.snpeff_annotation.check_contig_compatibility',operation='check_contig_compatibility',
                     produces_observations=[{'identity':'contigs_compatible','evidence_contract':{'required_fields':['value']}}])
            return p
        case._planner_payload=plan
        body=shlex.quote(PYTHON)+" - <<'PY'\n# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence({'produces_observations': {'contigs_compatible': {'value': False}}}))\nPY\n"
        def response(payload,ordinal):
            if mode in ('repair','malformed_env'):
                if ordinal==1:return json.dumps({'operation':'check_contig_compatibility','code':body,**({'env':{'SYNTHETIC_VALUE':7}} if mode=='repair' else {})})
                if ordinal==2:return json.dumps({'action_type':'execute','operation':'check_contig_compatibility','code':body,'env':['invalid-parent'] if mode=='malformed_env' else {'SYNTHETIC_VALUE': 7}})
                raise AssertionError('Unexpected third provider call')
            raw={'action_type':'execute','operation':'check_contig_compatibility','env':{},'outputs':[]}
            if mode=='argv':raw['argv']=['bash','-lc',body]
            else:raw.update(code=body,language='bash')
            return json.dumps(raw)
        events,requests,state=await generate_until_ready_or_stopped(case,'audit-native-'+mode,response)
        durable=load_plan_state(case.root,'audit-native-'+mode)
        outcomes=[v.get('generate_outcome') for e in events for v in e.values() if isinstance(v,dict) and v.get('generate_outcome')]
        pending=durable['task_state'].get('pending_action') or {}
        evidence={'mode':mode,'routes':[next(iter(e)) for e in events],'outcomes':outcomes,
          'provider_calls':len(requests),'first_protocol_has_json':bool(requests and 'Return exactly one raw JSON' in requests[0]['_provider_request']),
          'helper_rule_says_block':bool(requests and 'Return ONE complete executable block' in requests[0]['_provider_request']),
          'pending_code':pending.get('code'),'pending_argv':pending.get('argv'),
          'events':durable['task_state'].get('candidate_repair_state',{}).get('events'),
          'loaded_source':{'file':str(Path(__import__('bio_agents').__file__)), 'sha256':hashlib.sha256(Path(__import__('bio_agents').__file__).read_bytes()).hexdigest()},
          'coverage':'real production Consultant/Planner/Plan approval/Generate/parser/candidate ledger; fixture model and CLI approval; stops before Execute'}
        save('incident-native-'+mode,evidence)
        if mode=='malformed_env':
            assert len(requests)==2 and len(evidence['events'])==1
            assert outcomes[-1]['payload']['error_code']=='generate_exception'
        elif mode=='repair':
            assert len(requests)==2,outcomes
            rows=list(evidence['events'].values())
            assert len(rows)==2 and rows[0]['candidate_fingerprint']!=rows[1]['candidate_fingerprint']
            assert rows[1]['termination_reason']=='repair_no_progress',rows
            assert rows[1]['policy_receipt']['remaining']==3
        else:
            assert outcomes[-1]['type']=='action_ready',outcomes
            executable='\n'.join(pending.get('argv') or [])
            if mode=='argv':assert '# BIOMIND_EFFECT_HELPER' in executable and 'def emit_effect_evidence' not in executable
            else:assert '# BIOMIND_EFFECT_HELPER' not in executable and 'def emit_effect_evidence' in executable
            # Mechanism witness: run the fresh approved candidate argv as a registered
            # tiny process. This is not the production action Execute gate.
            assert pending['argv'][:2]==['bash','-lc']
            # Never run a login shell: only the exact known synthetic body is
            # exercised in the already guarded non-login process.
            out,err,rc=await approved_tiny(case.root/'new-candidate-process.sh',pending['argv'][2])
            assert rc==(1 if mode=='argv' else 0),(out,err,rc)
            if mode=='argv':assert 'NameError' in err and not out
            else:assert extract_structured_effect_evidence(out)['produces_observations'][0]['value'] is False
            evidence['synthetic_candidate_process']={'rc':rc,'stdout':out,'stderr':err,'gate':'separate executor component, not full action approval; native login argv replaced by guarded non-login execution of identical synthetic body'}
            save('incident-native-'+mode,evidence)
    finally:await case.asyncTearDown()


@pytest.mark.parametrize('mode',['argv','code','repair','malformed_env'])
def test_current_native_generate_and_progress_witness(mode):
    asyncio.run(native_scenario(mode))

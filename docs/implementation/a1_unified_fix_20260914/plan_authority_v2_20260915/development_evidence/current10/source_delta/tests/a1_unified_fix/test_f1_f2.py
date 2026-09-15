"""Current protocol and carrier FIX oracles; synthetic execution only."""
import asyncio
from copy import deepcopy
import hashlib
import json
import shlex

import pytest
from bio_agents import BioAnalystAgent
from agent_core.step_effects import (
    declared_effect_emission_contract, materialize_effect_template,
    materialize_effect_action, generate_output_examples,
    structured_effect_parse_receipt, extract_structured_effect_evidence,
)
from tests.test_preflight_recovery_audit import approved_tiny, save, PYTHON


def template():
    step = {'step_id': 'synthetic-check', 'operation': 'check_contig_compatibility',
            'produces_observations': [{'identity': 'renamed_result', 'evidence_contract': {'required_fields': ['value']}}]}
    return step, declared_effect_emission_contract({'steps': [step]}, step['step_id'])


def source():
    return "# BIOMIND_EFFECT_HELPER\nimport sys\nobserved = len(sys.argv[1:]) == 99\nprint(emit_effect_evidence({'produces_observations': {'renamed_result': {'value': observed, 'items': []}}}))\n"


@pytest.mark.parametrize('mode', ['python', 'bash', 'code', 'both', 'legacy_python', 'legacy_bash'])
def test_actual_carrier_tiny_process(mode, tmp_path):
    step, frozen = template()
    body = source()
    args = ['中文 空格', 'single\'double"']
    shell_body = shlex.quote(PYTHON) + ' - ' + shlex.join(args) + " <<'SYNTHETIC'\n" + body + 'SYNTHETIC\n'
    if mode in ('legacy_python', 'legacy_bash'):
        from agent_core.controller_audit import parse_executable_script_block, adapt_script_block_to_action
        language = 'python' if mode == 'legacy_python' else 'shell'
        block = parse_executable_script_block('<'+language+'>\n'+(body if language=='python' else shell_body)+'</'+language+'>')
        from dataclasses import replace
        block = replace(block, body=materialize_effect_template(block.body, frozen, language=block.language))
        # The old Python adapter's executable selector is a tested external
        # environment boundary; execute its exact expanded body in known Python.
        action = {'type': 'execute', 'argv': [PYTHON, '-I', '-S', '-c', block.body, *args]} if language=='python' else {'type':'execute','argv':['bash','--noprofile','--norc','-c',block.body]}
    else:
        raw = {'action_type': 'execute', 'operation': step['operation'], 'env': {}, 'outputs': []}
        if mode == 'python': raw['argv'] = [PYTHON, '-I', '-S', '-c', body, *args]
        elif mode == 'bash': raw['argv'] = ['bash', '--noprofile', '--norc', '-o', 'errexit', '-c', shell_body, 'synthetic-zero', *args]
        elif mode == 'code': raw.update(code=body, language='python')
        else: raw.update(code=shell_body, language='bash', argv=['bash','--noprofile','--norc','-c',shell_body,'synthetic-zero',*args])
        parsed = BioAnalystAgent._structured_action_from_object_result(raw)
        assert not parsed.error
        action = materialize_effect_action(parsed.action, frozen)
        assert materialize_effect_action(action, frozen) == action
        if not action['argv']: action['argv'] = [PYTHON,'-I','-S','-c',action['code'],*args]
        if mode == 'both': assert action['argv'][4] == action['code']
    assert '# BIOMIND_EFFECT_HELPER' not in ' '.join(action['argv'])
    out, err, rc = asyncio.run(approved_tiny(tmp_path/'new-action.sh', 'exec '+shlex.join(action['argv'])))
    assert rc == 0 and not err, (rc,err)
    evidence = extract_structured_effect_evidence(out)
    assert evidence['produces_observations'][0]['value'] is False
    assert evidence['produces_observations'][0]['items'] == []
    receipt = structured_effect_parse_receipt(out, step)
    save('f2-carrier-'+mode, {'level':'COMPONENT_FIX','action':action,'stdout':out,'stderr':err,'rc':rc,'parser_receipt':receipt,'template_sha256':frozen['template_sha256'],
        'formal_approval':'covered in F6 graph, not this serializer/carrier component','substitution':'only new synthetic computation and known interpreter'})


@pytest.mark.parametrize('bad', ['duplicate', 'shell_scope', 'nested_c', 'mismatch', 'digest', 'unknown_identity', 'flat_facts'])
def test_rejects_incomplete_or_conflicting_carrier(bad, tmp_path):
    step, frozen = template(); body = source()
    if bad in ('flat_facts','unknown_identity'):
        facts = {'renamed_result': {'value': False}} if bad=='flat_facts' else {'produces_observations': {'foreign': {'value': False}}}
        program = materialize_effect_template('# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence('+repr(facts)+'))\n',frozen)
        out,err,rc = asyncio.run(approved_tiny(tmp_path/'invalid.sh','exec '+shlex.join([PYTHON,'-I','-S','-c',program])))
        assert rc == 1 and not out
        assert ('EFFECT_FIELD_UNDECLARED' if bad=='flat_facts' else 'EFFECT_FACT_IDENTITIES_MISMATCH') in err
        return
    if bad=='duplicate': body += '\n# BIOMIND_EFFECT_HELPER\n'
    action = {'type':'execute','code':body,'language':'python','argv':[]}
    if bad=='shell_scope': action['language']='bash'
    if bad=='nested_c': action.update(code='',argv=['bash','-c',shlex.join([PYTHON,'-c',body])])
    if bad=='mismatch': action['argv']=[PYTHON,'-c','print(123)']
    if bad=='digest': frozen={**frozen,'template_sha256':'0'*64}
    with pytest.raises(ValueError): materialize_effect_action(action,frozen)


def test_protocol_examples_exact_parser_and_negative_action_shapes():
    step, frozen = template()
    public = generate_output_examples(step,script_protocol=False,template=frozen)
    for name in ('action_example','blocked_example'):
        parsed=BioAnalystAgent._extract_structured_action_result(json.dumps(public[name],ensure_ascii=False))
        assert not parsed.error,parsed.error
    assert public['action_example']['action_type']=='blocked'
    assert 'current_action_capability' in public['action_example']['reason']
    assert set(public['facts_example'])=={'produces_observations'}
    assert set(public['facts_example']['produces_observations'])=={'renamed_result'}
    wrapped=BioAnalystAgent._extract_structured_action_result(json.dumps({'action_json':public['action_example']}))
    assert wrapped.error['field_path']=='action_type'
    truncated=BioAnalystAgent._extract_structured_action_result(json.dumps(public['action_example'])[:-2])
    assert truncated.error['stage']=='json_decode'
    mixed=BioAnalystAgent._extract_structured_action_result(json.dumps(public['action_example'])+'\n'+json.dumps(public['blocked_example']))
    assert mixed.error
    assert not any('Return ONE complete executable block' in rule for rule in frozen['rules'])


def test_native_argv_generate_materializes_before_action_ready():
    async def scenario():
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        from tests.ger_fixture_support import configure_protocol,generate_until_ready_or_stopped
        from agent_core.plan_tracker import load_plan_state
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        case.SKILL_ID='genomics/snpeff-annotation'
        await case.asyncSetUp()
        try:
            capability_id='genomics.snpeff_annotation.check_contig_compatibility'
            configure_protocol(case,execution_capability_id=capability_id)
            case._configure_one_step_no_output_plan(execution_capability_id=capability_id);original=case._planner_payload
            def plan():
                result=original();result['steps'][0].update(execution_capability_id='genomics.snpeff_annotation.check_contig_compatibility',operation='check_contig_compatibility',produces_observations=[{'identity':'contigs_compatible','evidence_contract':{'required_fields':['value']}}]);return result
            case._planner_payload=plan
            script=shlex.quote(PYTHON)+" - <<'PY'\n# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence({'produces_observations': {'contigs_compatible': {'value': False}}}))\nPY\n"
            def candidate(payload,ordinal):
                assert ordinal==1
                return json.dumps({'action_type':'execute','operation':'check_contig_compatibility','argv':['bash','--noprofile','--norc','-c',script],'env':{},'outputs':[]})
            events,requests,state=await generate_until_ready_or_stopped(case,'f2-native-ready',candidate)
            durable=load_plan_state(case.root,'f2-native-ready');pending=durable['task_state']['pending_action']
            assert 'def emit_effect_evidence' in pending['argv'][4]
            assert '# BIOMIND_EFFECT_HELPER' not in pending['argv'][4]
            wire=requests[0]['_provider_request'];assert 'current_generate_output_protocol' in wire
            assert 'Return ONE complete executable block' not in wire
            save('f1-f2-native-ready',{'level':'GRAPH_TO_PREAPPROVAL_FIX','requests':requests,'events':events,'pending':pending})
        finally: await case.asyncTearDown()
    asyncio.run(scenario())

"""Approved observation template to unmodified stdout and formal parser."""
import json
import subprocess
import sys
from pathlib import Path
import pytest
from agent_core.step_effects import (declared_effect_emission_contract, emit_declared_effects,
    structured_effect_parse_receipt, extract_structured_effect_evidence, match_step_effect_evidence)


def declaration():
    return {'step_id':'step-direct','step_effect':{'schema_version':1,
        'produces_observations':[{'identity':'validation_observation',
            'evidence_contract':{'required_fields':['value']}}],
        'produces_artifacts':[], 'explicit_side_effects':[]}}


def run_fixture(path):
    """Only this exact test-created path is allowed by the offline bootstrap."""
    allowed=getattr(sys.modules['__main__'],'OFFLINE_ALLOWED_SCRIPT_PATHS',None)
    if allowed is not None:allowed.add(str(path))
    try:
        return subprocess.run(['/bin/bash','--noprofile','--norc',str(path)],
            env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'},capture_output=True,timeout=5)
    finally:
        if allowed is not None:allowed.discard(str(path))


@pytest.mark.parametrize('value',[False,"UNKNOWN",{'inputs':[{'exists':False}], 'tools':{'fixture':'UNKNOWN'}},True])
def test_observation_template_preserves_measured_value_without_files(tmp_path,value):
    current=declaration()
    contract=declared_effect_emission_contract({'steps':[current]},'step-direct')
    assert contract['declarations']['produces_observations'][0]['identity']=='validation_observation'
    facts={'produces_observations':{'validation_observation':{'value':value}}}
    path=tmp_path/'tiny.sh'
    path.write_text("/usr/bin/python3 <<'PY'\n"+contract['python_template']+
        '\nimport json\nfacts=json.loads('+repr(json.dumps(facts))+')\nprint(emit_effect_evidence(facts))\nPY\n')
    result=run_fixture(path)
    assert result.returncode==0,result.stderr
    raw=result.stdout.decode()
    parsed=structured_effect_parse_receipt(raw,current)
    assert parsed['status']=='parsed' and parsed['emission_count']==1
    evidence=extract_structured_effect_evidence(raw)
    assert evidence['produces_observations'][0]['value']==value
    assert not evidence.get('produces_artifacts')
    assert not any(match_step_effect_evidence(current,[evidence])['missing_effects'].values())
    assert raw.rstrip()==emit_declared_effects(current,facts)


@pytest.mark.parametrize('facts',[{}, {'produces_observations':{'validation_observation':{}}},
    {'produces_observations':{'wrong':{'value':True}}}])
def test_observation_template_cannot_invent_facts(facts):
    with pytest.raises(ValueError):emit_declared_effects(declaration(),facts)


def test_echo_and_rc_zero_cannot_satisfy_observation():
    raw='all inputs checked, PASS\n'
    receipt=structured_effect_parse_receipt(raw,declaration())
    assert receipt['emission_count']==0
    match=match_step_effect_evidence(declaration(),[extract_structured_effect_evidence(raw)])
    assert match['missing_effects']['produces_observations'][0]['identity']=='validation_observation'


@pytest.mark.parametrize('raw',[
    'BIOMIND_EFFECT_EVIDENCE_JSON={invalid',
    'BIOMIND_EFFECT_EVIDENCE_JSON={"produces_observations":[{"identity":"wrong","value":true}]}',
])
def test_parser_keeps_malformed_identity_and_required_field_checks(raw):
    assert structured_effect_parse_receipt(raw,declaration())['status']=='rejected'


def test_missing_or_null_value_cannot_satisfy_formal_contract():
    for value in ({}, {'value':None}):
        raw='BIOMIND_EFFECT_EVIDENCE_JSON='+json.dumps({'produces_observations':[{'identity':'validation_observation',**value}]})
        matched=match_step_effect_evidence(declaration(),[extract_structured_effect_evidence(raw)])
        assert matched['missing_effects']['produces_observations']

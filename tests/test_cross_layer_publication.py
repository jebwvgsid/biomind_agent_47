"""S1 positive acceptance and shared completion counterexamples (F01–F07)."""
import asyncio
from copy import deepcopy
import hashlib
import json
import shlex
import pytest
from agent_core.step_effects import (emit_declared_effects,declared_effect_emission_contract,
    materialize_effect_template,effect_template_provider_view,structured_effect_parse_receipt,
    extract_structured_effect_evidence,match_step_effect_evidence)
from tests.test_cross_layer_audit import declaration
from tests.test_preflight_recovery_audit import approved_tiny,save,PYTHON


def template(effect):
    return declared_effect_emission_contract({'steps':[{'step_id':'new-step',**effect}]},'new-step')


def test_F01_F02_publication_and_completion_are_distinct(tmp_path):
    effect=declaration();facts={'explicit_side_effects':{'create_reference_indexes':{
        'reference_resource_ref':'approved-ref','index_artifacts':[],'status':'blocked'}}}
    with pytest.raises(ValueError,match='EFFECT_FACT_IDENTITIES_MISMATCH'):
        emit_declared_effects(effect,facts)  # Existing success/adoption default.
    line=emit_declared_effects(effect,facts,publication=True)
    parsed=extract_structured_effect_evidence(line)
    assert structured_effect_parse_receipt(line,effect)['status']=='parsed'
    assert not parsed['produces_artifacts'] and parsed['explicit_side_effects'][0]['index_artifacts']==[]
    assert not match_step_effect_evidence(effect,[parsed])['complete']
    assert list(tmp_path.iterdir())==[]
    assert emit_declared_effects(effect,{},publication=True)==''
    empty=tmp_path/'实际 空目录';empty.mkdir()
    facts['produces_artifacts']={'synthetic-bundle':{'path':str(empty),'status':'blocked'}}
    assert not match_step_effect_evidence(effect,[extract_structured_effect_evidence(
        emit_declared_effects(effect,facts,publication=True))])['complete']
    save('F01-publication',{'line':line,'parsed':parsed,'scientific_commit':0,'fake_artifacts':0})


@pytest.mark.parametrize('variant',['foreign','override','null','missing','path','category','mixed'])
def test_F03_invalid_supplied_fact_fails_atomically(variant):
    facts={'explicit_side_effects':{'create_reference_indexes':{
        'reference_resource_ref':'approved-ref','index_artifacts':[],'status':'blocked'}}}
    side=facts['explicit_side_effects']['create_reference_indexes']
    if variant=='foreign':facts['explicit_side_effects']['foreign']=side.copy()
    if variant=='override':side['identity']='foreign'
    if variant=='null':side['index_artifacts']=None
    if variant=='missing':side.pop('index_artifacts')
    if variant=='path':facts['produces_artifacts']={'synthetic-bundle':{'status':'blocked'}}
    if variant=='category':facts['unknown']={}
    if variant=='mixed':facts['produces_observations']={'foreign':{'value':True}}
    with pytest.raises(ValueError,match='EFFECT_'):emit_declared_effects(declaration(),facts,publication=True)


def test_F06_F07_frozen_helper_executes_without_nested_json(tmp_path):
    ref=tmp_path/'新 参考名字.fasta';ref.write_text('>synthetic\nACGT\n')
    t=template(declaration());public=effect_template_provider_view(t)
    assert 'python_template' not in public and public['marker']=='# BIOMIND_EFFECT_HELPER'
    body=shlex.quote(PYTHON)+' - '+shlex.quote(str(ref))+" synthetic_dictionary <<'PY'\n"+'''# BIOMIND_EFFECT_HELPER
import sys
facts={"explicit_side_effects":{"create_reference_indexes":{
    "reference_resource_ref":sys.argv[1],"index_artifacts":[],
    "status":"blocked","missing_commands":sys.argv[2:]}}}
print(emit_effect_evidence(facts))
raise SystemExit(7)
PY
'''
    frozen=materialize_effect_template(body,t)
    assert '# BIOMIND_EFFECT_HELPER' not in frozen and frozen!=body
    out,err,rc=asyncio.run(approved_tiny(tmp_path/'approved-synthetic.sh',frozen))
    parsed=extract_structured_effect_evidence(out)
    assert rc==7 and not err and not parsed['produces_artifacts']
    assert parsed['explicit_side_effects'][0]['reference_resource_ref']==str(ref)
    changed=deepcopy(t);changed['python_template']+='\n# changed'
    with pytest.raises(ValueError,match='DIGEST_MISMATCH'):materialize_effect_template(body,changed)
    save('F06-F07-frozen',{'provider_template':public,'candidate':body,'approved':frozen,
        'approved_sha256':hashlib.sha256(frozen.encode()).hexdigest(),'stdout':out,'stderr':err,'rc':rc})

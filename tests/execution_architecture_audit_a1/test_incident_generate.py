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

from agent_core.plan_bindings import binding_input_progress_facts
from agent_core.step_effects import declared_effect_emission_contract, effect_template_provider_view, materialize_effect_template, extract_structured_effect_evidence
from agent_core.workflow.candidate_repair import candidate_repair_fingerprint
from tests.test_preflight_recovery_audit import approved_tiny, save, PYTHON




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





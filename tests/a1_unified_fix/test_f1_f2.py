"""Current protocol and carrier FIX oracles; synthetic execution only."""
import asyncio
from copy import deepcopy
import hashlib
import json
import shlex

import pytest
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





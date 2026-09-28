"""Compatibility must not turn an executable obligation into an unchecked claim."""
import asyncio
from copy import deepcopy

import pytest

from agent_core.plan_tracker import load_plan_state
from agent_core.work_validation import validation_diagnostics
from tests.contracts_support import contract_fixture
from tests.react_v3.support import actual_micro_runner
from tests.test_context_continuation_v1 import install_sequence


@pytest.mark.parametrize('rule', [
    {'kind':'custom','description':'Check the result','checks':[{'predicate':'exists'}]},
    {'kind':'custom','description':'Check the result','execution_env':{'A':'B'}},
    {'kind':'custom','description':''},
    {'kind':'invented_verifier','description':'Check the result'},
])
def test_legacy_description_compatibility_never_discards_machine_fields(rule):
    # Detached admission still cannot silently reinterpret these executable rules.
    original=deepcopy(rule)
    issues,_=validation_diagnostics([{'validation':rule}],check_field_policy=False)
    assert any(i['code']=='VALIDATION_KIND_UNSUPPORTED' for i in issues)
    assert rule==original


def test_explicit_cooler_uri_cannot_contradict_the_claimed_resolution(tmp_path):
    from agent_core.work_validation import validate_output
    path=str(tmp_path/'leaf.mcool')+'::/resolutions/10000'
    state={'plan_document':{'inputs':[{'sample':'leaf','path':path}],
        'parameters':{'resolution_bp':40000}}}
    table=tmp_path/'preflight.tsv'
    table.write_text('sample\tmatrix\tresolution_bp\tbalanced\tstatus\tmessage\n'
        'leaf\t'+path+'\t40000\tyes\tsuccess\twrong group despite claimed resolution\n')
    with pytest.raises(ValueError,match='URI resolution conflicts'):
        validate_output(state,{'role':'tad_preflight'},table)


def test_truncated_preflight_row_is_a_content_failure_not_a_harness_error(tmp_path):
    from agent_core.work_validation import validate_output
    path=str(tmp_path/'leaf.mcool')
    state={'plan_document':{'inputs':[{'sample':'leaf','path':path}],
        'parameters':{'resolution_bp':40000}}}
    table=tmp_path/'preflight.tsv'
    table.write_text('sample\tmatrix\tresolution_bp\tstatus\tmessage\tbalanced\n'
        'leaf\t'+path+'::/resolutions/40000\t40000\tsuccess\ttruncated\n')
    with pytest.raises(ValueError,match='failed or unusable'):
        validate_output(state,{'role':'tad_preflight'},table)







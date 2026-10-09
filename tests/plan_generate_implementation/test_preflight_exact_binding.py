"""Both established attempt evidence and explicit Plan evidence keep exact paths."""
import copy
import pytest
from agent_core.action_runtime import (build_execution_policy_manifest,
    resolve_preflight_evidence_binding, registered_preflight_output, ActionRuntimeContractError)


def policy(tmp_path, row=None):
    return build_execution_policy_manifest(action_identity={'action_id': 'a', 'plan_id': 'p', 'step_id': 'input_check'},
        working_directory=str(tmp_path), language='Bash', evidence_root=str(tmp_path/'attempts'),
        skill_resources=[{'handle': 'genomics/wgs-short-read-mapping#scripts/inspect_wgs_inputs.py'}],
        registered_preflight_evidence=row)


def test_no_registered_plan_output_keeps_attempt_policy(tmp_path):
    manifest = policy(tmp_path)
    bound = resolve_preflight_evidence_binding(manifest, approved_root=tmp_path/'attempts',
        attempt_id='attempt_current', action_environment={})
    assert bound['canonical_path'] == str(tmp_path/'attempts/attempt_current/preflight')
    assert not (tmp_path/'attempts/attempt_current/preflight').exists()
    assert registered_preflight_output({'controller_output_bindings': []}) is None


def test_registered_plan_output_passed_exactly_and_not_replaced(tmp_path):
    exact = tmp_path/'user-results/input_preflight_evidence'
    action = {'controller_output_bindings': [{'semantic_handle': 'plan.input_preflight_evidence',
        'expected_path': str(exact), 'artifact_id': 'registered-id'}]}
    manifest = policy(tmp_path, registered_preflight_output(action))
    bound = resolve_preflight_evidence_binding(manifest, approved_root=tmp_path/'attempts',
        attempt_id='attempt_current', action_environment={})
    assert bound['canonical_path'] == str(exact)
    assert bound['purpose'] == 'formal_plan_evidence'
    altered = copy.deepcopy(manifest)
    altered['evidence_binding']['canonical_path'] = str(tmp_path/'wrong')
    with pytest.raises(ActionRuntimeContractError):
        resolve_preflight_evidence_binding(altered, approved_root=tmp_path/'attempts',
            attempt_id='attempt_current', action_environment={})
    exact.mkdir(parents=True)
    with pytest.raises(ActionRuntimeContractError, match='PREFLIGHT_EVIDENCE_ALREADY_EXISTS'):
        resolve_preflight_evidence_binding(manifest, approved_root=tmp_path/'attempts',
            attempt_id='attempt_current', action_environment={})

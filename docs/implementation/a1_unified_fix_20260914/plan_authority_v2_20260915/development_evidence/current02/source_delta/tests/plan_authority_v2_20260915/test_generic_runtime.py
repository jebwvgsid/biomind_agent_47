"""Focused component checks; full graph exercises are in test_graph.py."""
from copy import deepcopy

import pytest

from agent_core.skill_manager import SkillManager
from agent_core.action_runtime import resolve_action_runtime_bindings, ActionRuntimeContractError, capability_contract_hash
from agent_core.step_effects import compile_step_effect


GENERIC = 'framework.generic.execute'


def manager():
    # Explicit empty catalog component boundary; no filesystem discovery or
    # scientific runtime is constructed by these tests.
    obj = SkillManager.__new__(SkillManager)
    obj.normalize_selected_skills = lambda values, **kwargs: list(values)
    obj.get_skill_meta = lambda sid: {}
    return obj


def generic_step():
    return {'step_id': 'independent-check-17', 'action': 'execute', 'tool': 'python',
        'execution_capability_id': GENERIC, 'operation': 'execute', 'skill_ids': [],
        'inputs': [], 'outputs': [],
        'produces_observations': [{'observation_id': 'input_readable',
            'evidence_contract': {'value_type': 'boolean', 'required_fields': ['value']}}]}


def test_generic_explicit_registry_and_runtime_without_domain_skill():
    obj = manager()
    registry = obj.get_execution_capability_registry([])
    assert GENERIC in registry
    step = generic_step()
    assert obj.validate_plan_execution_capabilities({'steps': [step]}, []) == []
    bound = obj.bind_plan_execution_capability_contracts({'steps': [step]}, [])
    assert bound['steps'][0]['capability_contract_hash'] == capability_contract_hash(registry[GENERIC])
    actual = resolve_action_runtime_bindings(selected_skill_id='', execution_capability_id=GENERIC,
        approved_capability_contract_hash=bound['steps'][0]['capability_contract_hash'], capability_registry=registry,
        ready_runtime_binding_registry={}, current_action_identity={'plan_id': 'new-component-plan', 'step_id': step['step_id']})
    assert actual['effective_runtime_bindings'] == {}
    compiled = compile_step_effect(step, registry)
    assert not compiled['issues'], compiled['issues']
    assert compiled['complete'] and not compiled['effect']['produces_artifacts']


@pytest.mark.parametrize('field,value,code', [
    ('execution_capability_id', 'nonexistent.domain.execute', 'UNKNOWN_EXECUTION_CAPABILITY'),
    ('execution_capability_id', '', 'ACTION_CAPABILITY_SCOPE_MISSING'),
    ('tool', 'imaginary_runner', 'GENERIC_EXECUTION_TOOL_UNSUPPORTED'),
])
def test_no_missing_unknown_or_unsupported_route_fallback(field, value, code):
    step = generic_step()
    step[field] = value
    issues = manager().validate_plan_execution_capabilities({'steps': [step]}, [])
    assert [issue['code'] for issue in issues] == [code]


def test_generic_contract_drift_still_rejects():
    registry = manager().get_execution_capability_registry([])
    approved = capability_contract_hash(registry[GENERIC])
    modified = deepcopy(registry)
    modified[GENERIC]['supported_tools'] = ['bash']
    with pytest.raises(ActionRuntimeContractError) as caught:
        resolve_action_runtime_bindings(selected_skill_id='', execution_capability_id=GENERIC,
            approved_capability_contract_hash=approved, capability_registry=modified,
            ready_runtime_binding_registry={}, current_action_identity={'step_id': 'new-step'})
    assert caught.value.code == 'RUNTIME_BINDING_SNAPSHOT_DRIFT'

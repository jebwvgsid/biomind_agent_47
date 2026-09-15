"""Public-contract components; run only in the established isolated export.

These component inputs are synthetic. Actual next-provider-message evidence is
separate in test_public_graph.py; no private reasoning or original session loads.
"""
from copy import deepcopy
import json
import re

import pytest

from agent_core.action_runtime import generic_execution_capability
from agent_core.node_context import (
    ControllerBinding, SemanticHandleBinding, SemanticHandleError, SemanticHandleMap,
    _llm_safe_step_projection, _planning_failure_focus,
    _project_step_semantic_references, _select_current_step_closure,
    approved_current_step_artifact_usage, resolve_plan_proposal_handles,
)
from agent_core.plan_bindings import planner_resource_requirements
from agent_core.planner_context import provider_correction_feedback
from prompts.manager import PromptManager


def _legacy_issue():
    association = {
        "step_id": "deleted_step", "capability_id": "example.prepare",
        "role": "artifact.workspace", "task_id": "private-task",
        "unrelated": {"step_id": "private-nested-step", "approval_id": "private-approval"},
    }
    return {
        "code": "PLAN_RESOURCE_ROLE_SCOPE_INVALID",
        "validator_id": "plan_resource_bindings",
        "field_path": "$.proposed_artifacts[0].role_associations[0]",
        "actual": deepcopy(association),
        "expected": {"capability_id": None, "role_association": {"step_id": "declared_step"}},
        "machine_evidence": {"actual": deepcopy(association), "step_lookup_found": False},
        "not_evaluated": ["step_effects", "dataflow"],
        "approval_id": "private-approval",
    }


def test_resource_reference_projection_preserves_invalid_value_only_at_public_paths():
    issue = _legacy_issue()
    before = deepcopy(issue)
    result = provider_correction_feedback({"validation_issues": [issue]})["validation_issues"][0]
    assert issue == before
    assert result["actual"]["step_id"] == "deleted_step"
    assert result["machine_evidence"]["actual"]["step_id"] == "deleted_step"
    assert result["expected"]["capability_id"] is None
    assert result["expected"]["role_association"]["step_id"] == "declared_step"
    assert result["machine_evidence"]["step_lookup_found"] is False
    assert result["not_evaluated"] == ["step_effects", "dataflow"]
    assert "step_id" not in result["actual"]["unrelated"]
    assert "private-" not in json.dumps(result)


def test_other_validator_does_not_gain_step_identity_exception():
    issue = _legacy_issue()
    issue["validator_id"] = "unrelated_source"
    result = provider_correction_feedback({"validation_issues": [issue]})["validation_issues"][0]
    assert "step_id" not in result["actual"]
    assert "step_id" not in result["machine_evidence"]["actual"]
    assert "step_id" not in result["expected"]["role_association"]


def test_real_capability_validator_exposes_invalid_and_available_ids_only_at_its_public_locus():
    from agent_core.plan_tracker import normalize_validation_issue
    from agent_core.skill_manager import SkillManager
    manager = object.__new__(SkillManager)
    # Only the registry loading dependency is substituted; the formal public
    # generic contract, capability validator, normalizer and projector are real.
    capability = generic_execution_capability()
    manager.get_execution_capability_registry = lambda selected: {capability['capability_id']: capability}
    issues = manager.validate_plan_execution_capabilities({'steps': [{
        'step_id': 'write_572', 'action': 'execute', 'skill_ids': [],
        'execution_capability_id': 'framework.unregistered.operation', 'tool': 'python'}]}, [])
    assert len(issues) == 1
    issue = normalize_validation_issue(issues[0])
    projected = provider_correction_feedback({'validation_issues': [issue]})['validation_issues'][0]
    assert projected['received_id'] == 'framework.unregistered.operation'
    assert projected['available_capability_ids'] == ['framework.generic.execute']
    for change in ({'validator_id': 'unrelated_validator'}, {'field_path': '$.private.execution_capability_id'}):
        bad_source = {**issue, **change}
        hidden = provider_correction_feedback({'validation_issues': [bad_source]})['validation_issues'][0]
        assert 'received_id' not in hidden and 'available_capability_ids' not in hidden


def test_skill_bad_value_is_retained_without_exposing_nested_control_metadata():
    from agent_core.plan_tracker import validation_issue
    issue = validation_issue('PLAN_STEP_SKILL_UNKNOWN', owner='planner',
        validator_id='plan_skill_bindings', field_path='$.steps[0].skill_ids[0]',
        actual_value='unknown_skill', selected_skill_id='selected/catalog-id',
        machine_evidence={'task_id': 'private-task'})
    result = provider_correction_feedback({'validation_issues': [issue]})['validation_issues'][0]
    assert result['actual_value'] == 'unknown_skill'
    assert result['selected_skill_id'] == 'selected/catalog-id'
    assert 'private-task' not in json.dumps(result)


def test_retired_association_diagnostic_preserves_each_explicit_bad_reference():
    issue = _legacy_issue()
    issue.update(code="PLAN_RESOURCE_FIELD_RETIRED",
                 field_path="$.proposed_artifacts[0].role_associations",
                 actual=[{"step_id": "deleted_step", "capability_id": "example.prepare", "role": "artifact.workspace"}])
    result = provider_correction_feedback({"validation_issues": [issue]})["validation_issues"][0]
    assert result["actual"][0]["step_id"] == "deleted_step"


def test_resource_requirements_v3_and_scoped_legacy_shape_survive_projection():
    requirement = planner_resource_requirements({}, {})
    result = provider_correction_feedback({"requirements": [requirement]})["requirements"][0]
    assert result["protocol"] == "plan_resource_protocol_v3"
    assert set(result["step_resource_uses"]["item"]) == {"resource_ref", "access", "role"}
    assert "role_associations" not in result["new_resource"]
    assert set(result["legacy_resource_contract_v2"]["role_association_item"]) == {"step_id", "capability_id", "role"}
    old = {"protocol": "plan_resource_protocol_v2", "new_resource": {
        "role_associations": {"item": {"step_id": "declared step", "capability_id": "selected capability", "role": "exact role"}}}}
    projected = provider_correction_feedback({"requirements": [old]})["requirements"][0]
    assert projected["new_resource"]["role_associations"]["item"]["step_id"] == "declared step"
    old["protocol"] = "unrelated_protocol"
    projected = provider_correction_feedback({"requirements": [old]})["requirements"][0]
    assert "step_id" not in projected["new_resource"]["role_associations"]["item"]


def test_planning_focus_uses_same_public_diagnostic_projection():
    issue = _legacy_issue()
    focus = _planning_failure_focus({"validation_issues": [issue]}, SemanticHandleMap())
    assert focus["violations"] == provider_correction_feedback({"validation_issues": [issue]})["validation_issues"]
    assert focus["violations"][0]["actual"]["step_id"] == "deleted_step"


def _handles():
    return SemanticHandleMap([
        SemanticHandleBinding("input.record", "artifact", "record-stable", direction="input", allowed_usage=("read",)),
        SemanticHandleBinding("skill.example", "skill", "example/skill", allowed_usage=("reference",)),
    ])


def test_single_use_handle_roundtrip_is_exact_and_preserves_prose():
    handles = _handles()
    binding = ControllerBinding("synthetic-invocation", "planner", "task", 0, "task-semantic", "task-record", semantic_handle_map=handles.to_dict())
    candidate = {"metadata": {"resource_contract_version": 3}, "steps": [{
        "step_id": "custom_inspection", "operation": "execute",
        "title": "record-stable is prose, not a reference",
        "resource_uses": [{"resource_ref": "input.record", "access": "read"},
                          {"resource_ref": "plan.future", "access": "read"}], "outputs": [],
    }]}
    resolved = resolve_plan_proposal_handles(candidate, binding)
    step = resolved["steps"][0]
    assert step["resource_uses"][0]["resource_ref"] == "record-stable"
    assert step["resource_uses"][1]["resource_ref"] == "plan.future"
    assert "inputs" not in step
    _project_step_semantic_references([step], handles)
    assert step == candidate["steps"][0]
    bad = deepcopy(candidate)
    bad["steps"][0]["resource_uses"][0]["resource_ref"] = "skill.example"
    with pytest.raises(SemanticHandleError):
        resolve_plan_proposal_handles(bad, binding)


def test_approved_read_view_consumes_single_use_without_mutating_frozen_source():
    snapshot = {"steps": [{"step_id": "inspect", "resource_uses": [
        {"resource_ref": "record-stable", "access": "read"},
        {"resource_ref": "workspace-stable", "access": "mutate"}], "outputs": []}]}
    before = deepcopy(snapshot)
    result = approved_current_step_artifact_usage(snapshot, "inspect")
    assert set(result) == {"record-stable"}
    assert result["record-stable"]["relation"] == "consume"
    assert snapshot == before and "inputs" not in snapshot["steps"][0]


@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_empty_resource_boundary_does_not_reopen_legacy_full_closure(explicit):
    raw = {"step_id": "inspect", "title": "inspect", "outputs": []}
    if explicit:
        raw["resource_uses"] = []
    projected = _llm_safe_step_projection(raw)
    projected["handle"] = "step.inspect"
    if explicit:
        assert projected["resource_uses"] == []
    result = _select_current_step_closure(node_type="generate", raw_steps=[raw],
        step_rows=[projected], current_step_id="inspect", constraints=[],
        inputs=[{"handle": "input.unrelated"}], outputs=[], raw_outputs=[])
    assert bool(result[1]) is (not explicit)
    assert result[-1] == ("current-step-closure-v2" if explicit else "legacy-unbound-step-full-closure-v1")


def test_public_prompt_exposes_complete_generic_single_source_example():
    system = PromptManager.get_planner_v2_system_prompt()
    raw = re.search(r'<generic_plan_example schema_version="1">\n(.*?)\n</generic_plan_example>', system, re.S).group(1)
    example = json.loads(raw)
    assert example["metadata"]["resource_contract_version"] == 3
    generic = generic_execution_capability()
    for step in example["steps"]:
        assert step["execution_capability_id"] == generic["capability_id"]
        assert step["operation"] == generic["operation"]
        assert step["tool"] in generic["supported_tools"]
        assert step["step_id"] != step["operation"]
        assert "resource_uses" in step and "inputs" not in step
        assert step["skill_ids"] == []
    assert all("role_associations" not in x for x in example["proposed_artifacts"])
    read = example["steps"][1]
    assert read["outputs"] == [] and read["resource_uses"][0]["access"] == "read"
    assert "value" not in read["produces_observations"][0]
    assert '<selected-capability-id>' not in system
    assert "canonical-selected-skill-id" in system
    assert "resource_contract_version=3" in system

"""Explicit Plan necessity through real readers, compiler and completion gates.

All inputs and provenance are synthetic; no original state loader is imported.
Managed node/CommitStep end-to-end coverage is provided by the companion suite.
"""
from copy import deepcopy
import json

import pytest

from agent_core.completion_gate import CompletionGate, _expected_artifacts_from_final_plan
from agent_core.plan_contract import (
    parse_planner_outcome_envelope, plan_result_contracts, plan_uses_explicit_results,
    prepare_allocation_intent, promote_allocation_intent,
    structured_plan_candidate_diagnostics, planner_candidate_body_from_state,
)
from agent_core.plan_tracker import ensure_provenance_state
from agent_core.step_effects import compile_plan_dataflow, extract_structured_effect_evidence


def candidate(*, required=None, output=True, marker=True):
    contract = {"object_kind": "file", "lifecycle": "plan_local",
                "materialization_policy_id": "managed_intermediate_v1"}
    if required is not None:
        contract["required"] = required
    if marker:
        contract["path_policy"] = "model_selectable"
    return {
        "steps": [{"step_id": "inspect", "title": "Read synthetic inputs",
                   "action": "execute", "purpose": "probe", "tool": "fixture",
                   "inputs": [], "outputs": ["plan.result"] if output else [],
                   "dependencies": [], "success_criteria": ["Never write forbidden/report.md"],
                   "skill_ids": [], "workflow_stage_ids": [], "batch_id": "", "risk_level": "low",
                   "produces_observations": ["checked"]}],
        "proposed_artifacts": [{"local_ref": "plan.result", "semantic_view": {
            "role": "result", "description": "Synthetic result", "format": "txt"},
            "artifact_contract": contract}] if output else [],
        "validation_checklist": [{"check_id": "check", "description": "Read all declared input files"}],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2",
                     **({"result_contract_version": 1} if marker else {})},
    }


def state_with_observation(tmp_path, *, value=False, evidence=True, required=None, with_file=False):
    plan = candidate(output=with_file, required=required)
    state = {"schema_version": 10, "workflow_routing_schema_version": 2,
        "task_id": "synthetic-results", "plan_id": "plan-results", "plan_version": "pv1",
        "current_step_id": "inspect", "current_task": {"task_spec": None, "task_revision": 0},
        "task_state": {"status": "running", "workflow_routing_schema_version": 2},
        "plan_document": deepcopy(plan), "proposed_artifacts": deepcopy(plan["proposed_artifacts"]),
        "steps": [{**deepcopy(plan["steps"][0]), "status": "verification_pending",
                   "step_effect": {"schema_version": 1, "produces_artifacts": ["plan.result"] if with_file else [],
                       "produces_observations": [{"observation_id": "checked", "evidence_contract": {
                           "required_fields": ["value", "coverage"]}}]},
                   "output_artifact_ids": []}],
        "provenance_state": {"schema_version": 2, "task_id": "synthetic-results",
            "actions": {}, "attempts": {"attempt-1": {"attempt_id": "attempt-1", "action_id": "action-1",
                "plan_id": "plan-results", "plan_version": "pv1", "step_id": "inspect",
                "returncode": 0, "status": "VALIDATION_PASSED", "command_role": "analysis_execution"}},
            "artifacts": {}, "artifact_heads": {}, "validations": {"validation-1": {
                "validation_id": "validation-1", "step_id": "inspect", "attempt_id": "attempt-1",
                "action_id": "action-1", "status": "VALID", "effect_results": {
                    "produces_observations": [{"observation_id": "checked", "value": value,
                        "coverage": "all declared files", "receipt_id": "read-receipt"}] if evidence else []}}},
            "manifests": {}, "stages": {}, "attempt_counters": {}, "migration_log": []}}
    if with_file:
        state["plan_artifacts"] = {"plan.result": {
            "logical_artifact_id": "plan.result", "canonical_path": str(tmp_path / "optional.txt"),
            "object_kind": "file", "lifecycle": "plan_local", "materialization_policy_id": "managed_intermediate_v1",
            "required": bool(required), "path_policy": "model_selectable", "validation_contract_id": "file_exists"}}
    return state


@pytest.mark.parametrize("required", [None, False, True])
def test_strict_reader_and_allocation_preserve_explicit_required(tmp_path, required):
    plan = candidate(required=required)
    before = json.dumps(plan, sort_keys=True)
    assert structured_plan_candidate_diagnostics(plan) == []
    wire = parse_planner_outcome_envelope(json.dumps({"outcome_type": "PLAN_CANDIDATE", **plan}))
    actual = wire.plan_candidate_body()
    intent = prepare_allocation_intent(actual, candidate_scope_id="scope", planning_epoch_id="epoch")
    mapping, ledger = promote_allocation_intent(intent, task_id="synthetic", managed_root=str(tmp_path))
    row = ledger[mapping["plan.result"]]
    assert row["required"] is bool(required)
    assert row["path_policy"] == "model_selectable"
    assert json.dumps(plan, sort_keys=True) == before


def test_legacy_missing_marker_preserves_allocation_and_approved_payload(tmp_path):
    plan = candidate(marker=False)
    before = deepcopy(plan)
    intent = prepare_allocation_intent(plan, candidate_scope_id="scope", planning_epoch_id="epoch")
    _, ledger = promote_allocation_intent(intent, task_id="synthetic", managed_root=str(tmp_path))
    assert "result_contract_version" not in intent
    assert "required" not in next(iter(ledger.values()))
    assert plan_result_contracts(plan)["plan.result"]["required"] is True
    assert plan == before
    assert not plan_uses_explicit_results(plan)


@pytest.mark.parametrize("field,value", [("required", "false"), ("path_policy", "guess_latest")])
def test_strict_new_result_field_feedback(field, value):
    plan = candidate()
    plan["proposed_artifacts"][0]["artifact_contract"][field] = value
    assert any(row["field_path"].endswith("." + field) for row in structured_plan_candidate_diagnostics(plan))


def test_materialization_roundtrip_retains_required_path_policy():
    plan = candidate(output=False)
    plan["task_output_materializations"] = [{"output_ref": "output.report", "path": "report.md",
        "required": True, "path_policy": "managed_exact", "description": "Explicit requested report"}]
    assert structured_plan_candidate_diagnostics(plan) == []
    state = {"steps": plan["steps"], "plan_document": plan,
             "task_output_materializations": deepcopy(plan["task_output_materializations"])}
    assert planner_candidate_body_from_state(state)["task_output_materializations"] == plan["task_output_materializations"]


def test_optional_hard_downstream_gets_specific_feedback_not_promotion():
    plan = candidate(required=False)
    consumer = deepcopy(plan["steps"][0])
    consumer.update(step_id="consume", inputs=["plan.result"], outputs=[], dependencies=["inspect"])
    plan["steps"].append(consumer)
    compiled, _, issues = compile_plan_dataflow(plan, {}, candidate_scope_id="scope", allow_legacy_untyped=False)
    assert any(row["code"] == "PLAN_OPTIONAL_RESULT_REQUIRED_BY_CONSUMER" for row in issues)
    assert plan_result_contracts(compiled)["plan.result"]["required"] is False
    plan["proposed_artifacts"][0]["artifact_contract"]["required"] = True
    _, _, corrected = compile_plan_dataflow(plan, {}, candidate_scope_id="scope", allow_legacy_untyped=False)
    assert not any(row["code"] == "PLAN_OPTIONAL_RESULT_REQUIRED_BY_CONSUMER" for row in corrected)


@pytest.mark.parametrize("negative", [False, True])
def test_actual_multifile_read_and_structured_observation_has_no_file_obligation(tmp_path, negative):
    inputs = [tmp_path / "first.tsv", tmp_path / "second.tsv"]
    inputs[0].write_text("sample\tvalue\na\t1\n")
    inputs[1].write_text("sample\tvalue\nb\t2\n" if not negative else "sample\nb\n")
    measured = {path.name: "value" in path.read_text().splitlines()[0].split("\t") for path in inputs}
    raw = "BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps({"schema_version": 1,
        "produces_observations": [{"observation_id": "checked", "value": measured,
            "coverage": "all declared files", "receipt_id": "read-receipt"}]})
    parsed = extract_structured_effect_evidence(raw)
    state = state_with_observation(tmp_path)
    state["provenance_state"]["validations"]["validation-1"]["effect_results"] = parsed
    gate = CompletionGate.validate_current_step(tmp_path, "synthetic-results", state)
    assert gate.passed, gate.to_dict()
    assert gate.checked_artifacts == []
    state["steps"][0]["status"] = "passed"
    final = CompletionGate.validate_final(tmp_path, "synthetic-results", state, require_execution_evidence=True)
    assert final.passed, final.to_dict()
    assert state["steps"][0]["outputs"] == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["first.tsv", "second.tsv"]
    assert _expected_artifacts_from_final_plan(state, "Never write forbidden/report.md") == []
    assert measured["second.tsv"] is not negative


@pytest.mark.parametrize("failure", ["no_observation", "partial", "read_failure"])
def test_read_failure_or_incomplete_evidence_never_finishes(tmp_path, failure):
    state = state_with_observation(tmp_path, evidence=failure != "no_observation")
    row = state["provenance_state"]["validations"]["validation-1"]["effect_results"]["produces_observations"]
    if failure == "partial":
        row[0].pop("coverage")
    if failure == "read_failure":
        state["provenance_state"]["attempts"]["attempt-1"]["returncode"] = 1
    assert not CompletionGate.validate_current_step(tmp_path, "synthetic-results", state).passed
    state["steps"][0]["status"] = "passed"
    assert not CompletionGate.validate_final(tmp_path, "synthetic-results", state).passed


@pytest.mark.parametrize("required", [False, True])
def test_required_missing_blocks_optional_missing_does_not(tmp_path, required):
    state = state_with_observation(tmp_path, required=required, with_file=True)
    gate = CompletionGate.validate_current_step(tmp_path, "synthetic-results", state)
    assert gate.passed is not required, gate.to_dict()
    state["steps"][0]["status"] = "passed"
    assert CompletionGate.validate_final(tmp_path, "synthetic-results", state).passed is not required
    assert not (tmp_path / "optional.txt").exists()


def test_optional_absence_does_not_hide_process_failure(tmp_path):
    state = state_with_observation(tmp_path, required=False, with_file=True)
    state["provenance_state"]["attempts"]["attempt-1"]["returncode"] = 1
    assert not CompletionGate.validate_current_step(tmp_path, "synthetic-results", state).passed


@pytest.mark.parametrize("validator,passed", [("file_exists", True), ("nonempty_file", False)])
def test_zero_byte_result_respects_explicit_file_validation_contract(tmp_path, validator, passed):
    state = state_with_observation(tmp_path, required=True, with_file=True)
    state["plan_artifacts"]["plan.result"]["validation_contract_id"] = validator
    output = tmp_path / "optional.txt"
    output.write_bytes(b"")
    provenance = ensure_provenance_state(state)
    artifact_id = state["steps"][0]["output_artifact_ids"][0]
    row = provenance["artifacts"][provenance["artifact_heads"][artifact_id]]
    row.update(status="valid", validator=validator, validation_status="valid",
               producer_attempt_id="attempt-1", validation_id="validation-1")
    gate = CompletionGate.validate_current_step(tmp_path, "synthetic-results", state)
    assert gate.passed is passed, gate.to_dict()


@pytest.mark.parametrize("policy,passed", [("managed_exact", False), ("model_selectable", True)])
def test_registered_different_path_cannot_override_exact_approved_result(tmp_path, policy, passed):
    state = state_with_observation(tmp_path, required=True, with_file=True)
    state["plan_artifacts"]["plan.result"]["path_policy"] = policy
    actual = tmp_path / "actual.txt"
    actual.write_text("synthetic content\n")
    provenance = ensure_provenance_state(state)
    artifact_id = state["steps"][0]["output_artifact_ids"][0]
    row = provenance["artifacts"][provenance["artifact_heads"][artifact_id]]
    row.update(status="valid", validator="file_exists", validation_status="valid", path=str(actual),
               producer_attempt_id="attempt-1", validation_id="validation-1")
    gate = CompletionGate.validate_current_step(tmp_path, "synthetic-results", state)
    assert gate.passed is passed, gate.to_dict()


@pytest.mark.parametrize("path", ["../input.txt", "/external/report.md", "", "."])
def test_exact_plan_local_path_cannot_change_allocation_root(path, tmp_path):
    plan = candidate(required=True)
    plan['proposed_artifacts'][0]['artifact_contract'].update(path_policy='managed_exact', path=path)
    assert any(row['field_path'].endswith('.path') for row in structured_plan_candidate_diagnostics(plan))
    with pytest.raises(ValueError, match='PLAN_RESULT_PATH_INVALID'):
        prepare_allocation_intent(plan, candidate_scope_id='scope', planning_epoch_id='epoch')
    assert not list(tmp_path.iterdir())


def test_exact_plan_local_path_is_allocated_only_below_existing_managed_root(tmp_path):
    plan = candidate(required=True)
    plan['proposed_artifacts'][0]['artifact_contract'].update(path_policy='managed_exact', path='report.md')
    intent = prepare_allocation_intent(plan, candidate_scope_id='scope', planning_epoch_id='epoch')
    _, ledger = promote_allocation_intent(intent, task_id='synthetic', managed_root=str(tmp_path))
    row = next(iter(ledger.values()))
    assert row['canonical_path'] == str(tmp_path / 'report.md')
    assert row['required'] is True and row['path_policy'] == 'managed_exact'
    assert not list(tmp_path.iterdir())


def test_explicit_empty_outputs_and_plan_necessity_survive_tracker_adapters():
    from agent_core.plan_tracker import normalize_plan_step, normalize_plan_output_count_criteria, validate_output_contracts
    step = {'step_id': 'inspect', 'outputs': [], 'output_contracts': ['forbidden/report.md'],
            'success_criteria': ['Do not write forbidden/report.md']}
    assert normalize_plan_step(step)['outputs'] == []
    plan = candidate(output=False)
    old_task_outputs = [{'artifact_id': 'old-report', 'required': True, 'path': 'old-report.md'}]
    assert validate_output_contracts(plan, old_task_outputs) == []
    assert normalize_plan_output_count_criteria(plan, old_task_outputs) == plan


def test_materialization_normalizer_preserves_plan_required_default_and_exact_policy(tmp_path):
    from agent_core.plan_tracker import normalize_task_output_materializations
    plan = candidate(output=False)
    plan['task_output_materializations'] = [{'output_ref': 'output.report', 'path': 'report.md',
        'path_policy': 'managed_exact', 'validator': 'file_exists', 'description': 'Explicit named delivery'}]
    spec = {'output_contracts': [
        {'output_id': 'report', 'artifact_id': 'result-report', 'semantic_handle': 'output.report',
         'artifact_kind': 'file', 'required': True},
        {'output_id': 'root', 'role': 'output_dir', 'artifact_kind': 'directory', 'path': str(tmp_path)}]}
    normalized, issues = normalize_task_output_materializations(plan, spec)
    assert issues == []
    row = normalized['task_output_materializations'][0]
    assert row['required'] is False
    assert row['path_policy'] == 'managed_exact'
    assert row['validator'] == 'file_exists'
    assert row['canonical_path'] == str(tmp_path / 'report.md')


@pytest.mark.parametrize('diagnostic,role,passed', [(False, 'probe', True), (True, 'probe', False), (True, 'primary', False)])
def test_completion_eligibility_follows_admission_not_purpose_label(tmp_path, diagnostic, role, passed):
    state = state_with_observation(tmp_path)
    state['approval_status'] = 'approved'
    attempt = state['provenance_state']['attempts']['attempt-1']
    attempt['command_role'] = role
    state['provenance_state']['validations']['validation-1']['command_role'] = role
    normalized = {'action_id': 'action-1', 'step_id': 'inspect'}
    if diagnostic:
        normalized['recovery_execution_envelope'] = {'recovery_strategy': 'RUN_DIAGNOSTIC_PROBE'}
    state['provenance_state']['actions']['action-1'] = {
        'action_id': 'action-1', 'step_id': 'inspect', 'plan_id': 'plan-results',
        'plan_version': 'pv1', 'status': 'bound', 'normalized_action': normalized}
    gate = CompletionGate.validate_current_step(tmp_path, 'synthetic-results', state)
    assert gate.passed is passed, gate.to_dict()


def test_admitted_recovery_diagnostic_receipt_cannot_be_relabelled_primary(tmp_path):
    state = state_with_observation(tmp_path)
    state['approval_status'] = 'approved'
    state['provenance_state']['attempts']['attempt-1']['command_role'] = 'primary'
    state['provenance_state']['validations']['validation-1']['command_role'] = 'primary'
    state['provenance_state']['actions']['action-1'] = {
        'action_id': 'action-1', 'step_id': 'inspect', 'plan_id': 'plan-results',
        'plan_version': 'pv1', 'status': 'bound', 'normalized_action': {
            'action_id': 'action-1', 'step_id': 'inspect', 'recovery_decision_id': 'diagnostic-1'}}
    state['task_state'] = {'typed_recovery_receipts': {'diagnostic-1': {
        'status': 'admitted', 'directive': {'action': 'run_diagnostic_probe'}}}}
    assert not CompletionGate.validate_current_step(tmp_path, 'synthetic-results', state).passed


def test_task_output_binding_uses_approved_plan_result_declaration(tmp_path):
    from agent_core.managed_artifacts import build_managed_artifact_bindings
    plan = candidate(output=False)
    plan['task_output_materializations'] = [{
        'output_ref': 'output.report', 'path': str(tmp_path / 'report.md'),
        'canonical_path': str(tmp_path / 'report.md'), 'required': False,
        'path_policy': 'model_selectable', 'validator': 'file_exists'}]
    spec = {'output_contracts': [
        {'output_id': 'report', 'artifact_id': 'result-report', 'semantic_handle': 'output.report',
         'artifact_kind': 'file', 'path': str(tmp_path / 'legacy.md'), 'required': True,
         'path_policy': 'managed_exact', 'validator': 'non_empty_file'},
        {'output_id': 'root', 'role': 'output_dir', 'artifact_kind': 'directory',
         'required': True, 'path': str(tmp_path)}]}
    bindings = build_managed_artifact_bindings(spec, plan_state=plan)
    report = bindings['output.report']
    assert report.required is False
    assert report.path_policy == 'model_selectable'
    assert report.canonical_path == str(tmp_path / 'report.md')
    assert report.validation_contract_id == 'file_exists'
    assert report.metadata['result_contract_version'] == 1
    assert bindings['output.output_dir'].canonical_path == str(tmp_path)
    assert bindings['output.output_dir'].required is True

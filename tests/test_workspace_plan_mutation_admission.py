"""Plan-owned workspace effects must reach the runtime attester before launch."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_core.action_runtime import capability_contract_hash
from agent_core.step_effects import compile_step_effect
from agent_core.workspace_attestation import (
    BOUNDED_SNAPSHOT_COLLECTOR,
    WorkspaceAttestationError,
    finalize_mutation_attestation,
    prepare_mutation_intents,
    project_current_action_workspace_bindings,
    start_mutation_attestation,
    validate_workspace_action_admission,
)


def _case(tmp_path):
    evidence = {
        "schema_version": 1,
        "collector": BOUNDED_SNAPSHOT_COLLECTOR,
        "postconditions": [{"kind": "file_nonempty", "relative_path": "staged.fa"}],
    }
    mutation = {
        "workspace_ref": "plan.workspace",
        "mutation": "create_and_stage",
        "requires_available": False,
        "available_after_success": True,
        "establishes_state": "prepared",
        "evidence_contract": evidence,
    }
    capability = {"capability_id": "fixture.execute", "operation": "execute"}
    step = {
        "step_id": "prepare", "action": "execute", "operation": "execute",
        "execution_capability_id": "fixture.execute", "outputs": [],
        "step_effect": {"workspace_mutations": [mutation]},
    }
    resource = {
        "binding_id": "workspace-binding", "binding_revision": 1,
        "canonical_resource_id": "workspace-1", "resource_kind": "mutable_workspace",
        "aliases": ["plan.workspace", "workspace-1"],
    }
    ledger = {
        "canonical_path": str(tmp_path / "workspace"),
        "canonical_resource_id": "workspace-1", "resource_kind": "mutable_workspace",
        "artifact_binding_revision": 1,
    }
    state = {
        "plan_resource_bindings": {alias: resource for alias in resource["aliases"]},
        "plan_artifacts": {"plan.workspace": ledger},
    }
    snapshot = {
        "steps": [deepcopy(step)],
        "plan_resource_bindings": deepcopy(state["plan_resource_bindings"]),
        "plan_local_artifacts": deepcopy(state["plan_artifacts"]),
    }
    return state, snapshot, step, capability


def _project(state, snapshot, step, capability):
    return project_current_action_workspace_bindings(
        plan_state=state, approved_plan_snapshot=snapshot,
        current_step=step, capability_contract=capability,
    )


def test_plan_only_mutation_without_evidence_requires_repair_before_approval(tmp_path):
    _, _, step, capability = _case(tmp_path)
    step["step_effect"]["workspace_mutations"][0].pop("evidence_contract")
    # The original failure selected an artifact-producing capability that had
    # no mutation field, yet declared create_and_stage with no output handles.
    capability["produces_artifacts"] = ["generated_artifact"]
    result = compile_step_effect(step, {capability["capability_id"]: capability})
    issue = next(row for row in result["issues"]
                 if row["code"] == "PLAN_WORKSPACE_MUTATION_EVIDENCE_UNSUPPORTED")
    assert issue["disposition"] == "REPAIR_REQUIRED"
    assert issue["step_id"] == "prepare"
    assert result["receipt"]["status"] == "repair_required"
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize("bad_evidence", [
    {}, {"schema_version": 1, "collector": "model_claim", "postconditions": [{}]},
    {"schema_version": 1, "collector": BOUNDED_SNAPSHOT_COLLECTOR, "postconditions": []},
    {"schema_version": 1, "collector": BOUNDED_SNAPSHOT_COLLECTOR,
     "postconditions": [{"kind": "file_nonempty", "relative_path": "../outside"}]},
    {"schema_version": 1, "collector": BOUNDED_SNAPSHOT_COLLECTOR,
     "postconditions": [{"kind": "unknown_verifier", "relative_path": "data"}]},
])
def test_unsupported_inline_evidence_is_rejected_before_launch(tmp_path, bad_evidence):
    state, snapshot, step, capability = _case(tmp_path)
    step["step_effect"]["workspace_mutations"][0]["evidence_contract"] = bad_evidence
    snapshot["steps"] = [deepcopy(step)]
    result = compile_step_effect(step, {capability["capability_id"]: capability})
    assert any(row["code"] == "PLAN_WORKSPACE_MUTATION_EVIDENCE_UNSUPPORTED"
               for row in result["issues"])
    rows = _project(state, snapshot, step, capability)
    admission = validate_workspace_action_admission(
        {}, capability_contract=capability, workspace_bindings=rows, approved_step=step,
    )
    assert admission.status == "contract_error"
    assert admission.mutation_intents == ()


def test_approved_inline_contract_reaches_real_runtime_receipt(tmp_path):
    state, snapshot, step, capability = _case(tmp_path)
    original_hash = capability_contract_hash(capability)
    compiled = compile_step_effect(
        step, {capability["capability_id"]: capability},
        resource_bindings=state["plan_resource_bindings"],
    )
    assert compiled["issues"] == []
    step = compiled["step"]
    snapshot["steps"] = [deepcopy(step)]
    rows = _project(state, snapshot, step, capability)
    admission = validate_workspace_action_admission(
        {}, capability_contract=capability, workspace_bindings=rows, approved_step=step,
    )
    assert admission.status == "passed"
    assert len(admission.mutation_intents) == 1
    assert capability_contract_hash(capability) == original_hash
    assert "workspace_mutations" not in capability
    intents = prepare_mutation_intents(
        admission, task_id="task", plan_id="plan", plan_revision=1,
        step_id=step["step_id"], action_id="action", action_digest="digest", action={},
    )
    journal = start_mutation_attestation(intents, attempt_id="attempt")
    workspace = Path(rows[0]["formal_concrete_path"])
    workspace.mkdir()
    (workspace / "staged.fa").write_text(">chr1\nACGT\n")
    completed = finalize_mutation_attestation(journal, process_exit_code=0)
    assert len(completed["receipts"]) == 1
    assert completed["receipts"][0]["attested"] is True
    assert completed["receipts"][0]["attestation_status"] == "mutated_and_validated"


def test_read_only_observation_needs_no_outputs_or_mutation_receipts(tmp_path):
    state, snapshot, step, capability = _case(tmp_path)
    step["step_effect"] = {
        "workspace_mutations": [], "produces_observations": ["inputs_verified"],
    }
    snapshot["steps"] = [deepcopy(step)]
    compiled = compile_step_effect(step, {capability["capability_id"]: capability})
    assert compiled["issues"] == []
    assert compiled["step"]["outputs"] == []
    rows = _project(state, snapshot, step, capability)
    assert rows == []
    admission = validate_workspace_action_admission(
        {}, capability_contract=capability, workspace_bindings=rows, approved_step=step,
    )
    assert admission.status == "not_applicable"
    assert admission.mutation_intents == ()


@pytest.mark.parametrize("closed_mutations", [[], [{
    "workspace_ref": "another.workspace", "mutation": "create_and_stage",
}]])
def test_inline_evidence_cannot_expand_closed_capability_scope(tmp_path, closed_mutations):
    state, snapshot, step, capability = _case(tmp_path)
    capability["workspace_mutations"] = closed_mutations
    compiled = compile_step_effect(step, {capability["capability_id"]: capability})
    assert any(row.get("violation_code") == "STEP_EFFECT_CAPABILITY_MISMATCH"
               for row in compiled["issues"])
    with pytest.raises(WorkspaceAttestationError):
        _project(state, snapshot, step, capability)
    admission = validate_workspace_action_admission(
        {}, capability_contract=capability, workspace_bindings=[], approved_step=step,
    )
    assert admission.status == "contract_error"


def test_projection_uses_frozen_step_instead_of_mutated_current_step(tmp_path):
    state, snapshot, step, capability = _case(tmp_path)
    step["step_effect"]["workspace_mutations"] = []
    rows = _project(state, snapshot, step, capability)
    assert len(rows) == 1
    assert rows[0]["operation_scope"]["mutation"] == "create_and_stage"


def test_generated_action_cannot_supply_missing_plan_evidence(tmp_path):
    state, snapshot, step, capability = _case(tmp_path)
    proposed_evidence = deepcopy(step["step_effect"])
    step["step_effect"]["workspace_mutations"][0].pop("evidence_contract")
    snapshot["steps"] = [deepcopy(step)]
    rows = _project(state, snapshot, step, capability)
    admission = validate_workspace_action_admission(
        {"step_effect": proposed_evidence, **proposed_evidence},
        capability_contract=capability, workspace_bindings=rows, approved_step=step,
    )
    assert admission.status == "contract_error"
    assert admission.mutation_intents == ()


def test_projected_evidence_must_match_the_approved_inline_contract(tmp_path):
    state, snapshot, step, capability = _case(tmp_path)
    rows = _project(state, snapshot, step, capability)
    rows[0]["evidence_contract"]["postconditions"][0]["relative_path"] = "unapproved.fa"
    admission = validate_workspace_action_admission(
        {}, capability_contract=capability, workspace_bindings=rows, approved_step=step,
    )
    assert admission.status == "contract_error"
    assert admission.violations[0]["code"] == "WORKSPACE_EVIDENCE_CONTRACT_SCOPE_MISMATCH"
    assert admission.mutation_intents == ()

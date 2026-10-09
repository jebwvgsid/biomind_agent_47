"""Promote Plan-only mutable resource identities together with their ledger."""
from copy import deepcopy

import agent_core.plan_tracker as plan_tracker
from agent_core.plan_bindings import compile_plan_resource_bindings
from agent_core.plan_contract import prepare_allocation_intent
from agent_core.step_effects import compile_plan_dataflow
from agent_core.workspace_attestation import project_current_action_workspace_bindings


def test_v2_compiled_mutation_only_resource_is_promoted_before_projection(tmp_path, monkeypatch):
    capability = {"capability_id": "fixture.execute", "operation": "execute"}
    plan = {
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2",
                     "resource_contract_version": 2},
        "proposed_artifacts": [{
            "local_ref": "plan.workspace",
            "semantic_view": {"role": "workspace", "format": "directory"},
            "artifact_contract": {
                "object_kind": "directory", "resource_kind": "mutable_workspace",
                "lifecycle": "plan_local", "materialization_policy_id": "managed_intermediate_v1",
            },
        }],
        "steps": [{
            "step_id": "prepare", "action": "execute", "operation": "execute",
            "execution_capability_id": capability["capability_id"],
            "inputs": [], "outputs": [], "dependencies": [],
            "workspace_mutations": [{
                "workspace_ref": "plan.workspace", "mutation": "create_and_stage",
                "requires_available": False, "available_after_success": True,
                "establishes_state": "prepared",
                "evidence_contract": {
                    "schema_version": 1, "collector": "bounded_filesystem_snapshot_v1",
                    "postconditions": [{"kind": "file_nonempty", "relative_path": "staged.fa"}],
                },
            }],
        }],
    }
    capabilities = {capability["capability_id"]: capability}
    state, _, issues = compile_plan_dataflow(plan, capabilities, candidate_scope_id="scope-mutation")
    assert issues == []
    candidate_id = state["steps"][0]["step_effect"]["workspace_mutations"][0]["identity"]
    assert candidate_id.startswith("candidate:")
    state.update({
        "task_id": "mutation-allocation",
        "plan_resource_bindings": compile_plan_resource_bindings(
            plan, candidate_scope_id="scope-mutation", authoritative_handles={},
        ),
        "allocation_intent": prepare_allocation_intent(
            plan, candidate_scope_id="scope-mutation", planning_epoch_id="epoch-mutation",
        ),
        "plan_commit_requested": False,
    })
    monkeypatch.setattr(plan_tracker, "migrate_plan_state", lambda value, *_args, **_kwargs: (value, []))
    monkeypatch.setattr(plan_tracker, "semantic_plan_hash", lambda _value: "semantic-plan-hash")
    monkeypatch.setattr(plan_tracker, "refresh_state_contract_v2", lambda value: value)
    plan_tracker._prepare_atomic_plan_objects(
        path=tmp_path / "task" / "plan" / "plan_state.json", state=state,
        current_value={}, store=object(), current_generation=0,
        step_effect_capability_registry=capabilities,
    )
    allocated = state["artifact_allocation_mapping"]["local_to_authoritative"]["plan.workspace"]
    step = state["steps"][0]
    assert step["outputs"] == []
    assert step["step_effect"]["workspace_mutations"][0]["identity"] == allocated
    assert state["plan_resource_bindings"][allocated]["canonical_resource_id"] == allocated
    assert state["plan_resource_bindings"]["plan.workspace"]["canonical_resource_id"] == allocated
    assert candidate_id not in state["plan_resource_bindings"]
    assert state["plan_artifacts"][allocated]["canonical_resource_id"] == allocated
    assert state["compiled_dataflow"]["edges"][0]["canonical_resource_id"] == allocated
    assert state["plan_compilation_receipt"]["status"] == "compiled"
    assert state["plan_dataflow_receipt"]["status"] == "compiled"
    snapshot = {
        "steps": [deepcopy(step)],
        "plan_local_artifacts": deepcopy(state["plan_artifacts"]),
    }
    rows = project_current_action_workspace_bindings(
        plan_state=state, approved_plan_snapshot=snapshot,
        current_step=step, capability_contract=capability,
    )
    assert len(rows) == 1
    assert rows[0]["canonical_resource_id"] == allocated
    assert rows[0]["formal_concrete_path"] == state["plan_artifacts"][allocated]["canonical_path"]

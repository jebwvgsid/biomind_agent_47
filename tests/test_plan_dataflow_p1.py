from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import agent_core.plan_tracker as plan_tracker
from agent_core.plan_bindings import compile_plan_resource_bindings
from agent_core.plan_contract import (
    prepare_allocation_intent,
    promote_allocation_intent,
    rewrite_candidate_local_refs,
    validate_planner_candidate_deterministically,
)
from agent_core.plan_tracker import (
    _rewrite_plan_resource_bindings_after_allocation,
)
from agent_core.skill_manager import SkillManager
from agent_core.step_effects import (
    compile_plan_dataflow,
    validate_plan_dataflow_receipt,
)
from prompts.manager import PromptManager


def _resource(
    local_ref: str,
    *,
    canonical_resource_id: str = "",
    resource_kind: str = "immutable_artifact",
    availability_origin: str = "plan_local",
) -> dict:
    row = {
        "local_ref": local_ref,
        "semantic_view": {
            "role": local_ref.split(".", 1)[-1],
            "format": "directory" if resource_kind == "mutable_workspace" else "text",
            "description": "explicit P1 fixture resource",
        },
        "artifact_contract": {
            "object_kind": "directory" if resource_kind == "mutable_workspace" else "file",
            "lifecycle": "plan_local",
            "materialization_policy_id": "managed_intermediate_v1",
            "resource_kind": resource_kind,
        },
    }
    if canonical_resource_id:
        row["resource_binding"] = {
            "canonical_resource_id": canonical_resource_id,
            "resource_kind": resource_kind,
            "availability_origin": availability_origin,
            "contract_revision": "fixture-contract-v1",
        }
    return row


def _mutation(
    resource_ref: str,
    *,
    mutation: str,
    requires_available: bool,
    available_after_success: bool = True,
    requires_state: str = "",
    establishes_state: str = "",
) -> dict:
    row = {
        "workspace_ref": resource_ref,
        "mutation": mutation,
        "requires_available": requires_available,
        "available_after_success": available_after_success,
        "contract_source": "fixture.selected_capability",
        "contract_revision": "fixture-contract-v1",
        "evidence_contract": {
            "schema_version": 1,
            "collector": "bounded_filesystem_snapshot_v1",
            "postconditions": [{"kind": "directory_exists", "relative_path": ""}],
        },
    }
    if requires_state:
        row["requires_state"] = requires_state
    if establishes_state:
        row["establishes_state"] = establishes_state
    return row


def _step(
    step_id: str,
    *,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    dependencies: list[str] | None = None,
    workspace_mutations: list[object] | None = None,
    observations: list[str] | None = None,
    capability_id: str = "",
    operation: str = "",
    title: str = "P1 dataflow step",
) -> dict:
    return {
        "step_id": step_id,
        "title": title,
        "action": "execute",
        "purpose": "analysis",
        "tool": "fixture",
        "inputs": list(inputs or []),
        "outputs": list(outputs or []),
        "dependencies": list(dependencies or []),
        "success_criteria": ["typed effect evidence is accepted"],
        "skill_ids": ["fixture/skill"],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
        "execution_capability_id": capability_id,
        "operation": operation,
        "produces_bindings": [],
        "produces_observations": list(observations or []),
        "workspace_mutations": list(workspace_mutations or []),
        "explicit_side_effects": [],
    }


def _plan(steps: list[dict], resources: list[dict]) -> dict:
    return {
        "steps": steps,
        "proposed_artifacts": resources,
        "validation_checklist": [{
            "check_id": "p1-dataflow",
            "description": "validate explicit resource effects",
        }],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
            "candidate_revision": 7,
        },
    }


def _snpeff_plan() -> tuple[dict, dict]:
    workspace = "plan.snpeff_data_dir"
    capability_workspace = "artifact.snpeff_data_dir"
    prepare_mutation = _mutation(
        workspace,
        mutation="create_or_update",
        requires_available=False,
        establishes_state="database_layout_prepared",
    )
    build_mutation = _mutation(
        workspace,
        mutation="update",
        requires_available=True,
        requires_state="database_layout_prepared",
        establishes_state="snpeff_database_ready",
    )
    plan = _plan(
        [
            _step(
                "prepare_database",
                workspace_mutations=[prepare_mutation],
                capability_id="fixture.prepare",
                operation="prepare_database",
            ),
            _step(
                "build_database",
                inputs=[workspace],
                dependencies=["prepare_database"],
                workspace_mutations=[build_mutation],
                capability_id="fixture.build",
                operation="build_database",
            ),
            _step(
                "annotate_variants",
                inputs=[workspace],
                outputs=["plan.annotated_vcf"],
                dependencies=["build_database"],
                capability_id="fixture.annotate",
                operation="annotate_variants",
            ),
        ],
        [
            _resource(
                workspace,
                canonical_resource_id=capability_workspace,
                resource_kind="mutable_workspace",
            ),
            _resource("plan.annotated_vcf"),
        ],
    )
    registry = {
        "fixture.prepare": {
            "capability_id": "fixture.prepare",
            "selected_skill_id": "fixture/skill",
            "operation": "prepare_database",
            "produces_artifacts": [],
            "produces_bindings": [],
            "produces_observations": [],
            "workspace_mutations": [{
                **prepare_mutation,
                "workspace_ref": capability_workspace,
            }],
            "explicit_side_effects": [],
        },
        "fixture.build": {
            "capability_id": "fixture.build",
            "selected_skill_id": "fixture/skill",
            "operation": "build_database",
            "produces_artifacts": [],
            "produces_bindings": [],
            "produces_observations": [],
            "workspace_mutations": [{
                **build_mutation,
                "workspace_ref": capability_workspace,
            }],
            "explicit_side_effects": [],
        },
        "fixture.annotate": {
            "capability_id": "fixture.annotate",
            "selected_skill_id": "fixture/skill",
            "operation": "annotate_variants",
            "produces_artifacts": ["artifact.annotated_vcf"],
            "produces_bindings": [],
            "produces_observations": [],
            "workspace_mutations": [],
            "explicit_side_effects": [],
        },
    }
    return plan, registry


def _codes(issues: list[dict]) -> set[str]:
    return {str(issue.get("code") or "") for issue in issues}


def test_plan_01_snpeff_workspace_uses_one_compiled_mutable_chain() -> None:
    plan, registry = _snpeff_plan()
    compiled, receipt, issues = compile_plan_dataflow(
        plan,
        registry,
        candidate_scope_id="scope-snpeff",
    )

    assert issues == []
    assert receipt["review_compatible"] is True
    workspace_edges = [
        edge for edge in compiled["compiled_dataflow"]["edges"]
        if edge["canonical_resource_id"] == "artifact.snpeff_data_dir"
    ]
    assert [edge["effect_kind"] for edge in workspace_edges] == [
        "MUTATE", "MUTATE", "READ", "READ"
    ]
    assert all(
        "plan.snpeff_data_dir" not in step["outputs"]
        for step in compiled["steps"]
    )


def test_legacy_flow_wrapper_delegates_to_compiled_workspace_facts() -> None:
    plan, _ = _snpeff_plan()
    _, _, issues = validate_planner_candidate_deterministically(
        plan,
        candidate_scope_id="scope-wrapper",
        authoritative_handles={},
        availability_origins={},
    )
    assert issues == []


def test_plan_03_external_mutable_origin_is_independent_of_resource_kind() -> None:
    workspace_mutation = _mutation(
        "input.workspace",
        mutation="update",
        requires_available=True,
        establishes_state="updated",
    )
    plan = _plan([
        _step(
            "update_external_workspace",
            inputs=["input.workspace", "input.reference"],
            workspace_mutations=[workspace_mutation],
        )
    ], [])
    authoritative_handles = {
        "input.workspace": "resource-external-workspace",
        "input.reference": "resource-external-reference",
    }
    authoritative_bindings = {
        "input.workspace": {
            "resource_kind": "mutable_workspace",
            "availability_origin": "external",
            "initially_available": True,
        },
        "input.reference": {
            "resource_kind": "immutable_artifact",
            "availability_origin": "external",
            "initially_available": True,
        },
    }
    _, _, issues = compile_plan_dataflow(
        plan,
        {},
        candidate_scope_id="scope-external",
        authoritative_handles=authoritative_handles,
        authoritative_bindings=authoritative_bindings,
    )
    assert issues == []


def test_plan_04_missing_source_has_complete_typed_source_analysis() -> None:
    plan = _plan(
        [_step("consume", inputs=["plan.index"], observations=["checked"])],
        [_resource("plan.index")],
    )
    _, _, issues = compile_plan_dataflow(
        plan, {}, candidate_scope_id="scope-missing"
    )
    issue = next(
        issue for issue in issues if issue["code"] == "PLAN_ARTIFACT_UNAVAILABLE"
    )
    assert issue["issue_id"].startswith("plan-dataflow-issue-")
    assert issue["candidate_revision"] == 7
    assert issue["authority"] == "blocking"
    assert issue["consumer_step"] == "consume"
    assert issue["dependency_ancestors"] == []
    assert issue["recognized_candidate_sources"] == []
    assert issue["actual_facts"]["source_analysis_status"] == "complete_no_source"
    assert issue["canonical_resource_id"].startswith("candidate:scope-missing:")


def test_plan_05_nonancestor_mutator_cannot_supply_read() -> None:
    workspace = "plan.workspace"
    plan = _plan(
        [
            _step(
                "create_other_branch",
                workspace_mutations=[_mutation(
                    workspace,
                    mutation="create",
                    requires_available=False,
                )],
            ),
            _step(
                "consume",
                inputs=[workspace],
                outputs=["plan.result"],
            ),
        ],
        [
            _resource(
                workspace,
                canonical_resource_id="artifact.workspace",
                resource_kind="mutable_workspace",
            ),
            _resource("plan.result"),
        ],
    )
    _, _, issues = compile_plan_dataflow(
        plan, {}, candidate_scope_id="scope-nonancestor"
    )
    issue = next(
        issue for issue in issues if issue["code"] == "PLAN_RESOURCE_UNAVAILABLE"
    )
    assert issue["consumer_step"] == "consume"
    assert issue["rejected_sources"][0]["reason"] == "source_step_is_not_an_ancestor"


def test_plan_05_same_step_mutation_does_not_satisfy_prior_read() -> None:
    workspace = "plan.workspace"
    plan = _plan(
        [_step(
            "read_then_create",
            inputs=[workspace],
            workspace_mutations=[_mutation(
                workspace,
                mutation="create",
                requires_available=False,
            )],
        )],
        [_resource(
            workspace,
            canonical_resource_id="artifact.workspace",
            resource_kind="mutable_workspace",
        )],
    )
    _, _, issues = compile_plan_dataflow(
        plan, {}, candidate_scope_id="scope-same-step"
    )
    issue = next(
        issue for issue in issues
        if issue["code"] == "PLAN_RESOURCE_READ_BEFORE_MUTATION"
    )
    assert issue["accepted_sources"] == []
    assert issue["rejected_sources"][0]["reason"] == (
        "same_step_effect_occurs_after_read_precondition"
    )


def test_plan_06_multiple_ordered_mutators_do_not_use_immutable_cardinality() -> None:
    plan, registry = _snpeff_plan()
    _, _, issues = compile_plan_dataflow(
        plan, registry, candidate_scope_id="scope-multi-mutate"
    )
    assert "PLAN_ARTIFACT_PRODUCER_CARDINALITY" not in _codes(issues)
    assert issues == []


def test_plan_06_unordered_mutators_are_rejected_as_ambiguous_state() -> None:
    workspace = "plan.workspace"
    plan = _plan(
        [
            _step(
                "mutate_a",
                workspace_mutations=[_mutation(
                    workspace,
                    mutation="create_or_update_a",
                    requires_available=False,
                )],
            ),
            _step(
                "mutate_b",
                workspace_mutations=[_mutation(
                    workspace,
                    mutation="create_or_update_b",
                    requires_available=False,
                )],
            ),
        ],
        [_resource(
            workspace,
            canonical_resource_id="artifact.workspace",
            resource_kind="mutable_workspace",
        )],
    )

    _, _, issues = compile_plan_dataflow(
        plan, {}, candidate_scope_id="scope-unordered-mutations"
    )
    issue = next(
        issue for issue in issues
        if issue["code"] == "PLAN_MUTATION_ORDER_AMBIGUOUS"
    )
    assert issue["actual_facts"]["unordered_mutator_step_ids"] == [
        "mutate_a", "mutate_b"
    ]
    assert issue["source_effect_kinds"] == ["MUTATE"]


def test_plan_07_aliases_unify_only_with_explicit_binding() -> None:
    explicit = _plan([], [
        _resource(
            "plan.database",
            canonical_resource_id="artifact.database",
            resource_kind="mutable_workspace",
        )
    ])
    bound = compile_plan_resource_bindings(
        explicit,
        candidate_scope_id="scope-bound",
        authoritative_handles={},
    )
    assert bound["plan.database"]["canonical_resource_id"] == "artifact.database"
    assert bound["artifact.database"]["canonical_resource_id"] == "artifact.database"

    unbound = compile_plan_resource_bindings(
        _plan([], [_resource("plan.database")]),
        candidate_scope_id="scope-unbound",
        authoritative_handles={},
    )
    assert "artifact.database" not in unbound
    assert unbound["plan.database"]["canonical_resource_id"] != "artifact.database"


def test_plan_07_allocation_preserves_explicit_workspace_identity_binding() -> None:
    workspace = "plan.workspace"
    plan = _plan(
        [_step(
            "create_workspace",
            workspace_mutations=[_mutation(
                workspace,
                mutation="create",
                requires_available=False,
            )],
        )],
        [_resource(
            workspace,
            canonical_resource_id="artifact.workspace",
            resource_kind="mutable_workspace",
        )],
    )
    intent = prepare_allocation_intent(
        plan,
        candidate_scope_id="scope-allocation",
        planning_epoch_id="epoch-allocation",
    )
    mapping, allocated_bindings = promote_allocation_intent(
        intent,
        task_id="task-allocation",
        managed_root="/managed/plan_artifacts",
    )
    rewritten = rewrite_candidate_local_refs(plan, mapping)
    allocated_id = mapping[workspace]
    original_registry = compile_plan_resource_bindings(
        plan,
        candidate_scope_id="scope-allocation",
        authoritative_handles={},
    )
    allocated_registry = _rewrite_plan_resource_bindings_after_allocation(
        original_registry,
        mapping,
    )

    assert rewritten["steps"][0]["workspace_mutations"][0][
        "workspace_ref"
    ] == allocated_id
    explicit_binding = rewritten["proposed_artifacts"][0]["resource_binding"]
    assert explicit_binding["canonical_resource_id"] == "artifact.workspace"
    assert {workspace, allocated_id}.issubset(set(explicit_binding["aliases"]))
    assert allocated_bindings[allocated_id]["resource_kind"] == "mutable_workspace"
    assert allocated_registry[allocated_id]["canonical_resource_id"] == (
        "artifact.workspace"
    )
    assert original_registry[workspace]["canonical_resource_id"] == (
        allocated_registry[allocated_id]["canonical_resource_id"]
    )
    assert allocated_id in allocated_registry[allocated_id]["aliases"]

    post_allocation = deepcopy(rewritten)
    post_allocation.pop("proposed_artifacts", None)
    compiled, post_allocation_receipt, issues = compile_plan_dataflow(
        post_allocation,
        {},
        resource_bindings=allocated_registry,
        compile_effect_contracts=False,
    )
    assert issues == []
    assert validate_plan_dataflow_receipt(
        post_allocation_receipt,
        compiled,
        allocated_registry,
    )["valid"] is True
    mutation_edge = next(
        edge for edge in compiled["compiled_dataflow"]["edges"]
        if edge["effect_kind"] == "MUTATE"
    )
    assert mutation_edge["declared_resource_ref"] == allocated_id
    assert mutation_edge["canonical_resource_id"] == "artifact.workspace"


def test_mutation_without_explicit_transition_is_contract_gap_not_missing_resource() -> None:
    workspace = "plan.workspace"
    plan = _plan(
        [_step("mutate", workspace_mutations=[workspace])],
        [_resource(
            workspace,
            canonical_resource_id="artifact.workspace",
            resource_kind="mutable_workspace",
        )],
    )
    _, _, issues = compile_plan_dataflow(
        plan, {}, candidate_scope_id="scope-transition-gap"
    )
    assert "PLAN_MUTATION_TRANSITION_UNSPECIFIED" in _codes(issues)
    assert "PLAN_RESOURCE_UNAVAILABLE" not in _codes(issues)
    assert "PLAN_ARTIFACT_UNAVAILABLE" not in _codes(issues)


def test_plan_08_immutable_cardinality_and_stale_dataflow_receipt_stay_hard() -> None:
    plan = _plan(
        [
            _step("produce_a", outputs=["plan.result"]),
            _step("produce_b", outputs=["plan.result"]),
        ],
        [_resource("plan.result")],
    )
    bindings = compile_plan_resource_bindings(
        plan,
        candidate_scope_id="scope-hard",
        authoritative_handles={},
    )
    compiled, receipt, issues = compile_plan_dataflow(
        plan, {}, resource_bindings=bindings
    )
    assert "PLAN_ARTIFACT_PRODUCER_CARDINALITY" in _codes(issues)
    assert validate_plan_dataflow_receipt(receipt, compiled, bindings)["valid"] is True

    stale = deepcopy(compiled)
    stale["steps"][1]["dependencies"] = ["produce_a"]
    validation = validate_plan_dataflow_receipt(receipt, stale, bindings)
    assert validation["valid"] is False
    assert "PLAN_DATAFLOW_RECEIPT_SOURCE_STALE" in _codes(validation["issues"])


def test_plan_08_initial_immutable_resource_cannot_be_current_plan_output() -> None:
    plan = _plan(
        [_step("replace_input", outputs=["input.source"])],
        [],
    )
    _, _, issues = compile_plan_dataflow(
        plan,
        {},
        authoritative_handles={"input.source": "artifact-source"},
        availability_origins={"artifact-source": "verified_task_input"},
        authoritative_bindings={
            "input.source": {
                "resource_kind": "immutable_artifact",
                "availability_origin": "verified_task_input",
            }
        },
    )

    issue = next(
        issue for issue in issues
        if issue["code"] == "PLAN_ARTIFACT_ORIGIN_CONFLICT"
    )
    assert issue["canonical_resource_id"] == "artifact-source"
    assert issue["accepted_sources"][0]["effect_kind"] == "INITIAL"
    assert issue["rejected_sources"][0]["effect_kind"] == "PRODUCE"

    _, _, wrapped_issues = validate_planner_candidate_deterministically(
        plan,
        candidate_scope_id="scope-origin-conflict-wrapper",
        authoritative_handles={"input.source": "artifact-source"},
        availability_origins={"artifact-source": "verified_task_input"},
        authoritative_bindings={
            "input.source": {
                "resource_kind": "immutable_artifact",
                "availability_origin": "verified_task_input",
            }
        },
    )
    wrapped = next(
        issue for issue in wrapped_issues
        if issue["code"] == "PLAN_ARTIFACT_ORIGIN_CONFLICT"
    )
    assert wrapped["field_path"] == "$.steps[0].outputs[0]"
    assert wrapped["canonical_resource_id"] == "artifact-source"
    assert wrapped["recognized_candidate_sources"] == issue[
        "recognized_candidate_sources"
    ]


def test_plan_09_title_only_change_does_not_change_dataflow_gate() -> None:
    plan, registry = _snpeff_plan()
    first, _, first_issues = compile_plan_dataflow(
        plan, registry, candidate_scope_id="scope-title"
    )
    renamed = deepcopy(plan)
    renamed["steps"][1]["title"] = "A completely different prose title"
    second, _, second_issues = compile_plan_dataflow(
        renamed, registry, candidate_scope_id="scope-title"
    )

    assert _codes(first_issues) == _codes(second_issues) == set()
    assert first["compiled_dataflow"] == second["compiled_dataflow"]


def test_plan_10_planner_prompt_exposes_exact_binding_and_typed_mutation() -> None:
    prompt = PromptManager.get_planner_v2_system_prompt()

    assert "resource_contract_version" in prompt
    assert "resource_ref" in prompt
    assert "omit resource_binding" in prompt.lower()
    assert "capability" in prompt and "scoped" in prompt
    for field in (
        "resource_kind",
        "contract_revision",
        "mutation",
        "requires_available",
        "requires_state",
        "available_after_success",
        "establishes_state",
    ):
        assert f'"{field}"' in prompt
    assert "Never infer aliases from basename, suffix, path" in prompt
    assert "Mutable workspaces belong in workspace_mutations, never in outputs" in prompt
    assert "list order is not a dependency" in prompt


def test_plan_11_real_snpeff_skill_declares_ordered_workspace_transitions() -> None:
    manager = SkillManager(str(Path(__file__).resolve().parents[1] / "skills"))
    registry = manager.get_execution_capability_registry([
        "genomics/snpeff-annotation"
    ])
    prepare = registry["genomics.snpeff_annotation.prepare_database"]
    build = registry["genomics.snpeff_annotation.build_database"]
    prepare_transition = prepare["workspace_mutations"][0]
    build_transition = build["workspace_mutations"][0]

    assert prepare_transition == {
        "workspace_ref": "artifact.snpeff_data_dir",
        "mutation": "prepare_database",
        "requires_available": False,
        "available_after_success": True,
        "establishes_state": "database_layout_prepared",
        "contract_source": (
            "genomics.snpeff_annotation.prepare_database.workspace_mutations"
        ),
        "contract_revision": "snpeff-workspace-transition-v1",
    }
    assert build_transition == {
        "workspace_ref": "artifact.snpeff_data_dir",
        "mutation": "build_database",
        "requires_available": True,
        "requires_state": "database_layout_prepared",
        "available_after_success": True,
        "establishes_state": "snpeff_database_ready",
        "contract_source": (
            "genomics.snpeff_annotation.build_database.workspace_mutations"
        ),
        "contract_revision": "snpeff-workspace-transition-v1",
    }


def test_plan_12_atomic_allocation_reseals_from_same_resource_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = "plan.workspace"
    plan = _plan(
        [_step(
            "prepare",
            workspace_mutations=[_mutation(
                workspace,
                mutation="create",
                requires_available=False,
                establishes_state="prepared",
            )],
        )],
        [_resource(
            workspace,
            canonical_resource_id="artifact.workspace",
            resource_kind="mutable_workspace",
        )],
    )
    scope = "scope-atomic-allocation"
    registry = compile_plan_resource_bindings(
        plan,
        candidate_scope_id=scope,
        authoritative_handles={},
    )
    state = deepcopy(plan)
    state.update({
        "task_id": "task-atomic-allocation",
        "allocation_intent": prepare_allocation_intent(
            plan,
            candidate_scope_id=scope,
            planning_epoch_id="epoch-atomic-allocation",
        ),
        "plan_resource_bindings": registry,
        "plan_commit_requested": False,
    })
    monkeypatch.setattr(
        plan_tracker,
        "migrate_plan_state",
        lambda value, *_args, **_kwargs: (value, []),
    )
    monkeypatch.setattr(
        plan_tracker,
        "semantic_plan_hash",
        lambda _value: "semantic-plan-hash",
    )
    monkeypatch.setattr(
        plan_tracker,
        "refresh_state_contract_v2",
        lambda value: value,
    )

    plan_tracker._prepare_atomic_plan_objects(
        path=(tmp_path / "task" / "plan" / "plan_state.json"),
        state=state,
        current_value={},
        store=object(),
        current_generation=0,
        step_effect_capability_registry={},
    )

    allocated = state["artifact_allocation_mapping"][
        "local_to_authoritative"
    ][workspace]
    assert state["plan_resource_bindings"][allocated][
        "canonical_resource_id"
    ] == "artifact.workspace"
    assert state["plan_compilation_receipt"]["status"] == "compiled"
    assert state["plan_dataflow_receipt"]["status"] == "compiled"
    assert state["compiled_dataflow"]["edges"][0][
        "canonical_resource_id"
    ] == "artifact.workspace"


def test_plan_13_atomic_allocation_seals_empty_resource_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A typed observation-only Plan still owns a sealed dataflow fact."""

    plan = _plan(
        [_step("observe", observations=["approved_command_succeeded"])],
        [],
    )
    state = deepcopy(plan)
    state.update({
        "task_id": "task-empty-resource-allocation",
        "allocation_intent": prepare_allocation_intent(
            plan,
            candidate_scope_id="scope-empty-resource-allocation",
            planning_epoch_id="epoch-empty-resource-allocation",
        ),
        "plan_resource_bindings": {},
        "plan_commit_requested": False,
    })
    monkeypatch.setattr(
        plan_tracker,
        "migrate_plan_state",
        lambda value, *_args, **_kwargs: (value, []),
    )
    monkeypatch.setattr(
        plan_tracker,
        "semantic_plan_hash",
        lambda _value: "semantic-plan-hash",
    )
    monkeypatch.setattr(
        plan_tracker,
        "refresh_state_contract_v2",
        lambda value: value,
    )

    plan_tracker._prepare_atomic_plan_objects(
        path=(tmp_path / "task" / "plan" / "plan_state.json"),
        state=state,
        current_value={},
        store=object(),
        current_generation=0,
        step_effect_capability_registry={},
    )

    assert state["plan_resource_bindings"] == {}
    assert state["compiled_dataflow"]["edges"] == []
    assert state["plan_dataflow_receipt"]["status"] == "compiled"
    assert validate_plan_dataflow_receipt(
        state["plan_dataflow_receipt"], state, {}
    )["valid"] is True

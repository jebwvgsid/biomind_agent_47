from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_core.plan_contract import (
    structured_plan_candidate_diagnostics,
    validate_planner_candidate_deterministically,
)
from agent_core.plan_tracker import (
    bind_plan_to_task_revision,
    load_plan_state,
    migrate_plan_state,
    normalize_validation_issue,
    record_planning_failure,
    save_plan_state,
)
from agent_core.planning_state_facts import new_planner_loop_state
from agent_core.skill_manager import SkillManager
from agent_core.step_effects import (
    compile_plan_step_effects,
    match_step_effect_evidence,
    validate_plan_compilation_receipt,
)


def _step(**updates):
    value = {
        "step_id": "check",
        "title": "Check the declared condition",
        "action": "execute",
        "purpose": "validation",
        "tool": "declared",
        "execution_capability_id": "skill.check",
        "operation": "check",
        "inputs": ["input.vcf", "input.fa"],
        "outputs": [],
        "dependencies": [],
        "success_criteria": ["The check exits successfully"],
        "skill_ids": ["skill"],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
    }
    value.update(updates)
    return value


def _plan(step):
    return {
        "task_id": "effect-task",
        "plan_id": "plan-effect-task",
        "plan_version": "plan_v1",
        "plan_revision": 1,
        "steps": [step],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }


def _capability(**updates):
    value = {
        "capability_id": "skill.check",
        "selected_skill_id": "skill",
        "operation": "check",
        "produces_artifacts": [],
        "produces_bindings": [],
        "produces_observations": ["condition_true"],
        "workspace_mutations": [],
        "explicit_side_effects": [],
    }
    value.update(updates)
    return {value["capability_id"]: value}


def _compile(step, registry):
    compiled, receipt, issues = compile_plan_step_effects(
        _plan(step), registry, allow_legacy_untyped=False
    )
    return compiled, receipt, issues


def test_input_with_observation_only_is_legal_without_artifact_output():
    compiled, receipt, issues = _compile(
        _step(produces_observations=["condition_true"]), _capability()
    )
    assert issues == []
    assert compiled["steps"][0]["outputs"] == []
    assert compiled["steps"][0]["step_effect"]["produces_observations"] == [
        "condition_true"
    ]
    assert receipt["effect_complete"] is True


def test_healthcheck_observation_only_is_legal():
    registry = _capability(
        capability_id="runtime.java21",
        operation="check_java_runtime",
        produces_observations=["java21_available"],
    )
    compiled, _, issues = _compile(
        _step(
            execution_capability_id="runtime.java21",
            operation="check_java_runtime",
            inputs=[],
            produces_observations=["java21_available"],
        ),
        registry,
    )
    assert issues == []
    assert compiled["steps"][0]["outputs"] == []


def test_binding_only_and_workspace_mutation_steps_are_legal():
    for field, value in (
        ("produces_bindings", ["genome_version"]),
        (
            "workspace_mutations",
            [{"workspace_ref": "snpeff_data_dir", "mutation": "readwrite"}],
        ),
    ):
        contract = _capability(
            produces_observations=[],
            **{field: value},
        )
        compiled, receipt, issues = _compile(_step(**{field: value}), contract)
        assert issues == []
        assert compiled["steps"][0]["outputs"] == []
        assert receipt["effect_complete"] is True


def test_unique_capability_effect_is_compiler_normalized_with_receipt():
    compiled, receipt, issues = _compile(_step(), _capability())
    assert issues == []
    assert compiled["steps"][0]["step_effect"]["produces_observations"] == [
        "condition_true"
    ]
    assert any(
        row["code"] == "NORMALIZE_STEP_EFFECT"
        for row in receipt["normalizations"]
    )


def test_real_skill_operation_contract_is_the_generic_compiler_source():
    manager = SkillManager(str(Path(__file__).resolve().parents[1] / "skills"))
    registry = manager.get_execution_capability_registry([
        "genomics/snpeff-annotation"
    ])
    capability_id = (
        "genomics.snpeff_annotation.check_contig_compatibility"
    )
    compiled, receipt, issues = _compile(
        _step(
            execution_capability_id=capability_id,
            operation="check_contig_compatibility",
            skill_ids=["genomics/snpeff-annotation"],
        ),
        registry,
    )

    assert issues == []
    assert compiled["steps"][0]["outputs"] == []
    assert compiled["steps"][0]["step_effect"]["produces_observations"] == [
        "contigs_compatible"
    ]
    assert receipt["status"] == "compiled"
    assert any(
        row["source"].endswith("operation.check_contig_compatibility")
        for row in receipt["normalizations"]
        if row["code"] == "NORMALIZE_STEP_EFFECT"
    )


def test_unique_operation_can_supply_missing_capability_identity():
    compiled, receipt, issues = _compile(
        _step(execution_capability_id=""), _capability()
    )
    assert issues == []
    assert compiled["steps"][0]["execution_capability_id"] == "skill.check"
    assert any(
        row["code"] == "NORMALIZE_EXECUTION_CAPABILITY"
        for row in receipt["normalizations"]
    )


def test_ambiguous_or_empty_effect_returns_route_free_repair_evidence():
    ambiguous_registry = {
        "skill.a": {
            **next(iter(_capability().values())),
            "capability_id": "skill.a",
            "produces_observations": ["a"],
        },
        "skill.b": {
            **next(iter(_capability().values())),
            "capability_id": "skill.b",
            "produces_observations": ["b"],
        },
    }
    for step, registry in (
        (_step(execution_capability_id=""), ambiguous_registry),
        (_step(execution_capability_id="", operation=""), {}),
    ):
        _, receipt, issues = _compile(step, registry)
        assert receipt["effect_complete"] is False
        assert any(row["code"] == "PLAN_STEP_EFFECT_UNSPECIFIED" for row in issues)
        assert all(row["disposition"] == "REPAIR_REQUIRED" for row in issues)
        assert all("route" not in row and "next_step" not in row for row in issues)


def test_ambiguous_effect_evidence_survives_durable_planner_reentry():
    _, _, issues = _compile(
        _step(execution_capability_id="", operation=""),
        {},
    )
    issue = next(
        row for row in issues if row["code"] == "PLAN_STEP_EFFECT_UNSPECIFIED"
    )
    durable = record_planning_failure(
        {"task_state": {}},
        validation_issues=[issue],
        planning_epoch_id="epoch",
        task_spec_revision_value=1,
        validation_source="step_effect_compiler",
        candidate=_plan(_step(execution_capability_id="", operation="")),
    )
    persisted = durable["task_state"]["planning_failure"]["validation_issues"][0]
    assert persisted["possible_effect_kinds"] == [
        "artifact",
        "binding",
        "observation",
        "workspace_mutation",
        "explicit_side_effect",
    ]
    assert persisted["expected"] == (
        "at least one explicit or uniquely inferred StepEffect"
    )
    assert persisted["actual"] == "no interpretable effect"
    assert persisted["source"] == "step_effect_compiler"


def test_capability_artifact_effect_requires_planner_artifact_binding():
    registry = _capability(
        produces_observations=[], produces_artifacts=["artifact.result"]
    )
    compiled, _, issues = _compile(_step(), registry)
    assert compiled["steps"][0]["outputs"] == []
    assert any(row["code"] == "PLAN_STEP_ARTIFACT_EFFECT_UNBOUND" for row in issues)


def test_answer_only_finish_step_has_protocol_owned_effect():
    compiled, receipt, issues = _compile(
        _step(
            action="finish",
            execution_capability_id="",
            operation="",
            inputs=[],
            skill_ids=[],
        ),
        {},
    )
    assert issues == []
    assert receipt["effect_complete"] is True
    assert compiled["steps"][0]["step_effect"]["explicit_side_effects"]


def test_compilation_receipt_allows_review_of_outputless_step():
    compiled, receipt, issues = _compile(_step(), _capability())
    assert issues == []
    compiled["plan_compilation_receipt"] = receipt
    review = validate_plan_compilation_receipt(
        receipt, compiled, _capability(), require_complete=True,
        allow_legacy_missing=False,
    )
    assert review["valid"] is True
    assert review["effect_complete"] is True


def test_compilation_receipt_survives_persistence_normalization():
    with TemporaryDirectory() as tmpdir:
        plan, _ = migrate_plan_state(_plan(_step()), "effect-task", tmpdir)
        bind_plan_to_task_revision(plan, 1)
        compiled, receipt, issues = compile_plan_step_effects(
            plan,
            _capability(),
            allow_legacy_untyped=False,
        )
        assert issues == []
        compiled["plan_compilation_receipt"] = receipt

        save_plan_state(tmpdir, "effect-task", compiled)
        persisted = load_plan_state(tmpdir, "effect-task")
        review = validate_plan_compilation_receipt(
            persisted.get("plan_compilation_receipt"),
            persisted,
            _capability(),
            require_complete=True,
            allow_legacy_missing=False,
        )

    assert review["valid"] is True
    assert review["issues"] == []


def test_observation_evidence_matches_without_fake_file():
    effect = {"produces_observations": ["condition_true"]}
    matched = match_step_effect_evidence(
        effect,
        {
            "effect_evidence": {
                "produces_observations": [
                    {"observation_id": "condition_true", "value": True, "status": "observed"}
                ]
            }
        },
    )
    assert matched["complete"] is True
    assert matched["missing_effects"]["produces_artifacts"] == []


def test_wire_accepts_optional_effect_fields_and_empty_outputs():
    candidate = {
        "steps": [_step(workspace_mutations=[{"workspace_ref": "db", "mutation": "readwrite"}])],
        "validation_checklist": [],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"},
    }
    assert structured_plan_candidate_diagnostics(candidate) == []


def test_volatile_scope_does_not_fake_planner_progress():
    candidate = _plan(_step())
    issue = {
        "code": "PLAN_ARTIFACT_UNAVAILABLE",
        "field_path": "$.steps[0].inputs[0]",
        "affected_artifact": {
            "kind": "candidate_local",
            "identity": "plan.input",
            "candidate_scope_id": "scope-a",
        },
    }
    first = record_planning_failure(
        {"task_state": {}},
        validation_issues=[issue],
        planning_epoch_id="epoch",
        task_spec_revision_value=1,
        validation_source="test",
        candidate=candidate,
    )
    second_issue = deepcopy(issue)
    second_issue["affected_artifact"]["candidate_scope_id"] = "scope-b"
    second = record_planning_failure(
        first,
        validation_issues=[second_issue],
        planning_epoch_id="epoch",
        task_spec_revision_value=1,
        validation_source="test",
        candidate=candidate,
    )
    failure = second["task_state"]["planning_failure"]
    assert failure["repeat_count"] == 1
    assert "candidate_scope_id" not in str(failure["validation_issues"])


def test_nested_candidate_route_poison_does_not_fake_semantic_progress():
    issue = {
        "code": "PLAN_STEP_EFFECT_UNSPECIFIED",
        "disposition": "REPAIR_REQUIRED",
        "step_id": "check",
        "affected_step_ids": ["check"],
        "affected_fields": ["produces_observations"],
        "field_path": "$.steps[0].produces_observations",
    }
    first_candidate = _plan(_step(produces_observations=[{
        "observation_id": "contigs_compatible",
        "target_route": "generate",
        "request_id": "request-a",
    }]))
    first = record_planning_failure(
        {"task_state": {}},
        validation_issues=[issue],
        planning_epoch_id="epoch",
        task_spec_revision_value=1,
        validation_source="step_effect_compiler",
        candidate=first_candidate,
    )
    second_candidate = deepcopy(first_candidate)
    second_candidate["steps"][0]["produces_observations"][0].update({
        "target_route": "finish",
        "request_id": "request-b",
    })
    second = record_planning_failure(
        first,
        validation_issues=[issue],
        planning_epoch_id="epoch",
        task_spec_revision_value=1,
        validation_source="step_effect_compiler",
        candidate=second_candidate,
    )

    failure = second["task_state"]["planning_failure"]
    assert failure["repeat_count"] == 1
    assert failure["correction_progress"] == "no_structural_progress"
    assert "target_route" not in str(failure["previous_candidate"])
    assert "request_id" not in str(failure["previous_candidate"])
    assert "contigs_compatible" in str(failure["previous_candidate"])




def test_protocol_invalid_dag_candidates_remain_route_free_planner_repairs():
    base = _step(inputs=[], execution_capability_id="", operation="")
    cases = (
        (
            [deepcopy(base), {**deepcopy(base), "title": "Duplicate"}],
            "PLAN_STEP_ID_DUPLICATE",
        ),
        (
            [{**deepcopy(base), "dependencies": ["missing"]}],
            "PLAN_DEPENDENCY_UNKNOWN",
        ),
        (
            [
                {**deepcopy(base), "step_id": "a", "dependencies": ["b"]},
                {**deepcopy(base), "step_id": "b", "dependencies": ["a"]},
            ],
            "PLAN_DEPENDENCY_CYCLE",
        ),
    )
    for steps, expected_code in cases:
        candidate = {
            "steps": steps,
            "validation_checklist": [],
            "metadata": {
                "schema_version": 2,
                "planner_contract": "structured_plan_v2",
            },
        }
        _, _, issues = validate_planner_candidate_deterministically(
            candidate,
            candidate_scope_id="scope",
            authoritative_handles={},
            availability_origins={},
        )
        assert any(issue["code"] == expected_code for issue in issues)
        assert all(issue["disposition"] == "PROTOCOL_INVALID" for issue in issues)
        assert all("route" not in str(issue).lower() for issue in issues)

    duplicate_issue = next(
        issue for issue in validate_planner_candidate_deterministically(
            {
                "steps": cases[0][0],
                "validation_checklist": [],
                "metadata": {
                    "schema_version": 2,
                    "planner_contract": "structured_plan_v2",
                },
            },
            candidate_scope_id="scope",
            authoritative_handles={},
            availability_origins={},
        )[2]
        if issue["code"] == "PLAN_STEP_ID_DUPLICATE"
    )
    assert duplicate_issue["affected_step_ids"] == ["check"]
    assert duplicate_issue["machine_evidence"]["duplicate_step_indexes"] == {
        "check": [0, 1]
    }


def test_real_effect_correction_counts_as_semantic_progress():
    issue = normalize_validation_issue({
        "code": "PLAN_STEP_EFFECT_UNSPECIFIED",
        "disposition": "REPAIR_REQUIRED",
        "step_id": "check",
        "field_path": "$.steps[0].step_effect",
        "affected_fields": ["produces_observations"],
    })
    first_candidate = _plan(_step(execution_capability_id="", operation=""))
    first = record_planning_failure(
        {"task_state": {}},
        validation_issues=[issue],
        planning_epoch_id="epoch",
        task_spec_revision_value=1,
        validation_source="step_effect_compiler",
        candidate=first_candidate,
    )
    corrected_candidate = deepcopy(first_candidate)
    corrected_candidate["steps"][0]["produces_observations"] = [
        "contigs_compatible"
    ]
    second = record_planning_failure(
        first,
        validation_issues=[issue],
        planning_epoch_id="epoch",
        task_spec_revision_value=1,
        validation_source="step_effect_compiler",
        candidate=corrected_candidate,
    )
    failure = second["task_state"]["planning_failure"]
    assert failure["repeat_count"] == 0
    assert failure["correction_progress"] == "affected_structure_changed"

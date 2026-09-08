from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from agent_core.plan_tracker import normalize_plan_step, normalize_validation_issue
from agent_core.step_effects import (
    compile_plan_step_effects,
    validate_plan_compilation_receipt,
    validate_step_effect_receipt,
)


def _plan(step: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": "adversarial-effect-task",
        "plan_id": "plan-adversarial-effect-task",
        "plan_version": "plan_v1",
        "plan_revision": 1,
        "steps": [deepcopy(dict(step))],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }


def _step(**updates: Any) -> dict[str, Any]:
    step = {
        "step_id": "inspect-runtime",
        "title": "Inspect runtime semantics",
        "action": "execute",
        "purpose": "validation",
        "tool": "declared",
        "execution_capability_id": "skill.inspect-runtime",
        "operation": "inspect_runtime",
        "inputs": [],
        "outputs": [],
        "dependencies": [],
        "success_criteria": ["The declared runtime semantics are observed"],
        "skill_ids": ["skill"],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
    }
    step.update(updates)
    return step


def _rich_effect() -> dict[str, list[Any]]:
    return {
        "produces_bindings": [
            {
                "binding_id": "runtime.java_bin",
                "value_type": "absolute_path",
                "evidence_contract": {"source": "stdout", "required": True},
            }
        ],
        "produces_observations": [
            {
                "observation_id": "runtime.java_version",
                "value_type": "string",
                "evidence_contract": {"source": "stdout", "required": True},
            }
        ],
        "workspace_mutations": [
            {
                "workspace_ref": "runtime.cache",
                "mutation": "readwrite",
                "evidence_contract": {
                    "required_fields": ["changed", "receipt"],
                },
            }
        ],
        "explicit_side_effects": [
            {
                "effect_id": "runtime.probe_recorded",
                "evidence_contract": {"receipt_required": True},
            }
        ],
    }


def _capability(effect: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "skill.inspect-runtime": {
            "capability_id": "skill.inspect-runtime",
            "selected_skill_id": "skill",
            "operation": "inspect_runtime",
            "produces_artifacts": [],
            **deepcopy(dict(effect or _rich_effect())),
        }
    }


def _issue_codes(validation: Mapping[str, Any]) -> set[str]:
    return {
        str(issue.get("code") or "")
        for issue in validation.get("issues", [])
        if isinstance(issue, Mapping)
    }


def _recursive_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {
            *(str(key) for key in value),
            *(
                nested
                for item in value.values()
                for nested in _recursive_keys(item)
            ),
        }
    if isinstance(value, (list, tuple)):
        return {
            nested
            for item in value
            for nested in _recursive_keys(item)
        }
    return set()


def test_protocol_invalid_complete_receipt_is_not_reviewable() -> None:
    malformed_effect = {
        "produces_observations": [
            {"value": True, "evidence_contract": {"source": "stdout"}}
        ]
    }
    step = _step(
        action="finish",
        execution_capability_id="",
        operation="",
        skill_ids=[],
        **malformed_effect,
    )
    compiled, receipt, issues = compile_plan_step_effects(
        _plan(step),
        {},
        allow_legacy_untyped=False,
    )

    assert any(issue["disposition"] == "PROTOCOL_INVALID" for issue in issues)
    assert receipt["effect_complete"] is True
    assert receipt["status"] == "protocol_invalid"
    assert receipt["review_compatible"] is False

    validation = validate_plan_compilation_receipt(
        receipt,
        compiled,
        {},
        require_complete=True,
        allow_legacy_missing=False,
    )
    assert validation["valid"] is False
    assert "PLAN_STEP_EFFECT_RECEIPT_NOT_REVIEWABLE" in _issue_codes(validation)


def test_malformed_plan_and_step_receipt_identities_are_structured_invalid() -> None:
    effect = {"produces_observations": ["runtime.java_version"]}
    registry = _capability(
        {
            "produces_bindings": [],
            **effect,
            "workspace_mutations": [],
            "explicit_side_effects": [],
        }
    )
    compiled, receipt, issues = compile_plan_step_effects(
        _plan(_step(**effect)),
        registry,
        allow_legacy_untyped=False,
    )
    assert issues == []

    malformed_plan_receipt = deepcopy(receipt)
    malformed_plan_receipt["identity"] = "not-an-identity-object"
    plan_validation = validate_plan_compilation_receipt(
        malformed_plan_receipt,
        compiled,
        registry,
        require_complete=True,
        allow_legacy_missing=False,
    )
    assert plan_validation["valid"] is False
    assert "PLAN_STEP_EFFECT_RECEIPT_IDENTITY_INVALID" in _issue_codes(
        plan_validation
    )

    malformed_step_receipt = deepcopy(receipt["step_receipts"][0])
    malformed_step_receipt["identity"] = ["not", "an", "identity", "object"]
    step_validation = validate_step_effect_receipt(
        malformed_step_receipt,
        compiled["steps"][0],
        capability_registry=registry,
        plan_identity=receipt["identity"],
        step_index=0,
    )
    assert step_validation["valid"] is False
    assert "STEP_EFFECT_RECEIPT_IDENTITY_INVALID" in _issue_codes(
        step_validation
    )


def test_explicit_builtin_finish_side_effect_compiles() -> None:
    step = _step(
        action="finish",
        execution_capability_id="",
        operation="",
        skill_ids=[],
        explicit_side_effects=["task_response"],
    )
    compiled, receipt, issues = compile_plan_step_effects(
        _plan(step),
        {},
        allow_legacy_untyped=False,
    )

    assert issues == []
    assert receipt["status"] == "compiled"
    assert receipt["effect_complete"] is True
    assert compiled["steps"][0]["step_effect"]["explicit_side_effects"] == [
        "task_response"
    ]


def test_plan_normalization_preserves_rich_effects_and_receipt_validity() -> None:
    effect = _rich_effect()
    registry = _capability(effect)
    source_step = normalize_plan_step(_step(**effect), index=1)
    compiled, receipt, issues = compile_plan_step_effects(
        _plan(source_step),
        registry,
        allow_legacy_untyped=False,
    )
    assert issues == []

    before = deepcopy(compiled["steps"][0]["step_effect"])
    compiled["steps"][0] = normalize_plan_step(compiled["steps"][0], index=1)

    assert compiled["steps"][0]["step_effect"] == before
    for field in (
        "produces_bindings",
        "produces_observations",
        "workspace_mutations",
        "explicit_side_effects",
    ):
        assert compiled["steps"][0][field] == before[field]

    validation = validate_plan_compilation_receipt(
        receipt,
        compiled,
        registry,
        require_complete=True,
        allow_legacy_missing=False,
    )
    assert validation["valid"] is True


def test_validation_issue_scrubs_nested_routes_and_volatile_identities() -> None:
    route_keys = {
        "route",
        "next_step",
        "next_route",
        "target_route",
        "goto",
        "proposed_route",
        "effective_route",
        "dispatch_route",
        "route_resolution",
        "planner_full_replan",
        "required_next_action",
        "node_recommended_next_step",
        "pending_recovery_action",
        "resume_route_hint",
        "resolved_resume_node",
        "resume_step",
        "terminal_route",
        "graph_node",
        "graph_node_name",
        "source_graph_node",
        "destination_graph_node",
        "controller_transition",
        "controller_transition_metadata",
        "command_goto",
        "route_event_id",
    }
    volatile_keys = {
        "candidate_scope_id",
        "request_id",
        "turn_id",
        "trace_id",
        "receipt_id",
        "invocation_id",
        "submission_id",
        "observation_id",
        "timestamp",
        "created_at",
        "updated_at",
        "observed_at",
    }

    def poisoned(suffix: str) -> dict[str, Any]:
        return {
            "code": "PLAN_STEP_EFFECT_UNSPECIFIED",
            "disposition": "REPAIR_REQUIRED",
            "owner": "planner",
            "step_id": "inspect-runtime",
            "field_path": "$.steps[0].step_effect",
            "message": "The step needs one unambiguous semantic effect.",
            **{key: f"poison-{suffix}" for key in route_keys},
            **{key: f"volatile-{suffix}" for key in volatile_keys},
            "machine_evidence": {
                "semantic_fact": "effect contract is ambiguous",
                **{key: f"nested-poison-{suffix}" for key in route_keys},
                "TARGET_ROUTE": f"case-poison-{suffix}",
                **{key: f"nested-volatile-{suffix}" for key in volatile_keys},
                "recovery_decision": {
                    "target_route": f"recovery-{suffix}",
                    "next_route": f"planner-{suffix}",
                    "semantic_reason": "same ambiguity",
                },
                "nested": [
                    {
                        "route_resolution": {
                            "dispatch_route": f"dispatch-{suffix}",
                        },
                        "resume_route_hint": f"resume-{suffix}",
                        "semantic_detail": "same detail",
                    }
                ],
            },
        }

    first = normalize_validation_issue(poisoned("a"))
    second = normalize_validation_issue(poisoned("b"))

    assert first["fingerprint"] == second["fingerprint"]
    assert first == second
    remaining_keys = _recursive_keys(first)
    assert remaining_keys.isdisjoint(route_keys)
    assert "TARGET_ROUTE" not in remaining_keys
    assert remaining_keys.isdisjoint(volatile_keys)
    assert first["machine_evidence"]["semantic_fact"] == (
        "effect contract is ambiguous"
    )
    assert first["machine_evidence"]["recovery_decision"] == {
        "semantic_reason": "same ambiguity"
    }

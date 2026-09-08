"""Full compiler and durable normalization boundaries with synthetic contracts.

These tests declare their own capabilities. They grant no mutable permission to
WGS Skills and do not claim provider/graph or scientific execution coverage.
"""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from agent_core.action_runtime import normalize_capability_contract
from agent_core.plan_tracker import (
    build_plan_state, commit_consultant_candidate_state, load_plan_state,
    normalize_plan_contracts, save_plan_state,
)
from agent_core.step_effects import compile_plan_dataflow, match_step_effect_evidence
from agent_core.task_spec import create_task_spec


SKILL = "acceptance/declared-capability"
SCOPE = "explicit-compiler-acceptance"


def _step(step_id, *, dependencies=(), mutations=(), capability="", effects=()):
    return {"step_id": step_id, "title": "Explicit synthetic operation", "action": "execute",
        "purpose": "analysis", "tool": "fixture", "inputs": [], "outputs": [],
        "dependencies": list(dependencies), "success_criteria": ["Declared typed evidence is present"],
        "skill_ids": [SKILL], "workflow_stage_ids": [], "batch_id": "", "risk_level": "low",
        "execution_capability_id": capability, "operation": step_id,
        "workspace_mutations": list(mutations), "explicit_side_effects": list(effects)}


def _plan(steps, resources=()):
    return {"steps": steps, "proposed_artifacts": list(resources),
        "validation_checklist": [{"check_id": "typed-effects", "description": "Validate the declared evidence"}],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2", "resource_contract_version": 2}}


def _workspace_case():
    mutations = [
        {"workspace_ref": "plan.workspace", "mutation": "initialize", "requires_available": False,
         "available_after_success": True, "establishes_state": "initialized"},
        {"workspace_ref": "plan.workspace", "mutation": "use", "requires_available": True,
         "requires_state": "initialized", "available_after_success": True, "establishes_state": "used"},
    ]
    for mutation in mutations:
        mutation.update(contract_source="synthetic.explicit-lifecycle", contract_revision="synthetic-v1")
    steps, registry = [], {}
    for index, name in enumerate(("initialize", "use")):
        capability = "acceptance." + name
        steps.append(_step(name, dependencies=["initialize"] if index else [],
            mutations=[deepcopy(mutations[index])], capability=capability))
        registry[capability] = normalize_capability_contract({
            "capability_id": capability, "selected_skill_id": SKILL, "operation": name,
            "contract_source": "synthetic.explicit-lifecycle", "contract_revision": "synthetic-v1",
            "produces_artifacts": [], "produces_bindings": [], "produces_observations": [],
            "workspace_mutations": [deepcopy(mutations[index])], "explicit_side_effects": [],
        })
    resource = {"local_ref": "plan.workspace", "semantic_view": {"role": "workspace", "format": "directory"},
        "artifact_contract": {"object_kind": "directory", "resource_kind": "mutable_workspace",
            "lifecycle": "plan_local", "materialization_policy_id": "managed_intermediate_v1"}}
    return _plan(steps, [resource]), registry


def _compile(plan, registry):
    return compile_plan_dataflow(plan, registry, candidate_scope_id=SCOPE,
        authoritative_handles={}, compile_effect_contracts=True, allow_legacy_untyped=False)


def _evidence(name, value, tmp_path):
    base = Path(os.environ["BIOMIND_OFFLINE_RESULTS"]).parent if os.environ.get("BIOMIND_OFFLINE_RESULTS") else tmp_path
    directory = base / "planner-effect-boundary-evidence"
    directory.mkdir(exist_ok=True)
    (directory / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def test_declared_mutable_initialize_use_runs_full_effect_and_dataflow_compiler_without_disk(tmp_path):
    plan, registry = _workspace_case()
    assert not list(tmp_path.iterdir())
    compiled, receipt, issues = _compile(plan, registry)
    _evidence("mutable-full-compiler", {"candidate": plan, "synthetic_registry": registry,
        "compiled": compiled, "receipt": receipt, "issues": issues}, tmp_path)
    assert issues == []
    assert receipt["review_compatible"] is True
    assert all(step["step_effect"]["workspace_mutations"] for step in compiled["steps"])
    edges = compiled["compiled_dataflow"]["edges"]
    assert [edge["effect_kind"] for edge in edges] == ["MUTATE", "MUTATE"]
    assert len({edge["canonical_resource_id"] for edge in edges}) == 1
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize("change,expected", [
    ("dependency", "PLAN_MUTATION_ORDER_AMBIGUOUS"),
    ("same_step", "PLAN_MUTATION_PRECONDITION_UNSATISFIED"),
    ("kind", "PLAN_RESOURCE_KIND_MISMATCH"),
    ("state", "PLAN_MUTATION_PRECONDITION_UNSATISFIED"),
    ("cycle", "PLAN_DEPENDENCY_CYCLE"),
    ("undeclared_effect", "STEP_EFFECT_CAPABILITY_MISMATCH"),
])
def test_full_compiler_keeps_existing_mutation_and_capability_guards(tmp_path, change, expected):
    plan, registry = _workspace_case()
    if change == "dependency":
        plan["steps"][1]["dependencies"] = []
    elif change == "same_step":
        for declaration in (plan["steps"][0]["workspace_mutations"][0], registry["acceptance.initialize"]["workspace_mutations"][0]):
            declaration.update(requires_available=True, requires_state="initialized")
    elif change == "kind":
        plan["proposed_artifacts"][0]["artifact_contract"]["resource_kind"] = "immutable_artifact"
    elif change == "state":
        # A declared capability can demand a state no prior step establishes.
        # Both declarations agree, so the dataflow guard must decide this case.
        plan["steps"][1]["workspace_mutations"][0]["requires_state"] = "never_established"
        registry["acceptance.use"]["workspace_mutations"][0]["requires_state"] = "never_established"
    elif change == "cycle":
        plan["steps"][0]["dependencies"] = ["use"]
    else:
        plan["steps"][1]["explicit_side_effects"] = [{"effect_id": "not_authorized", "evidence_contract": {"required_fields": ["receipt"]}}]
    compiled, receipt, issues = _compile(plan, registry)
    _evidence("mutable-rejected-" + change, {"candidate": plan, "synthetic_registry": registry,
        "compiled": compiled, "receipt": receipt, "issues": issues}, tmp_path)
    assert expected in {issue.get("violation_code") or issue.get("code") for issue in issues}, issues
    assert receipt["review_compatible"] is False


def test_inline_effect_survives_raw_normalization_real_plan_store_reload_and_complete_evidence(tmp_path):
    task_id = "offline-inline-effect-boundary"
    effect = {"effect_id": "explicit_scoped_effect", "evidence_contract": {"required_fields": ["receipt"]}}
    capability = "acceptance.inline"
    registry = {capability: normalize_capability_contract({
        "capability_id": capability, "selected_skill_id": SKILL,
        "contract_source": "synthetic.inline-capability", "contract_revision": "synthetic-inline-v1",
    })}
    candidate = _plan([_step("inline", capability=capability, effects=[effect])])
    raw = json.dumps(candidate, ensure_ascii=False)
    task = create_task_spec("Retain a declared typed side effect without executing it", task_id=task_id)
    initial = commit_consultant_candidate_state(tmp_path, task_id, {"task_spec": task, "selected_skills": [SKILL]})
    parsed = build_plan_state(task_id, raw, strict=True)
    parsed["state_generation"] = initial["state_generation"]
    parsed["current_task"] = deepcopy(initial["current_task"])
    parsed["selected_skill_ids"] = [SKILL]
    normalized, audit, normalization_issues = normalize_plan_contracts(parsed, task_spec=task)
    assert normalization_issues == [], normalization_issues
    assert normalized["steps"][0]["explicit_side_effects"] == [effect]
    compiled, receipt, issues = _compile(normalized, registry)
    assert issues == [], issues
    save_plan_state(tmp_path, task_id, compiled, step_effect_capability_registry=registry)
    reloaded = load_plan_state(tmp_path, task_id)
    assert not reloaded.get("load_error"), reloaded.get("load_error")
    assert reloaded["steps"][0]["explicit_side_effects"] == compiled["steps"][0]["explicit_side_effects"]
    accepted_effect = reloaded["steps"][0]["step_effect"]
    missing = match_step_effect_evidence(accepted_effect, {"explicit_side_effects": [{"effect_id": effect["effect_id"]}]})
    complete_evidence = {"explicit_side_effects": [{"effect_id": effect["effect_id"], "evidence": {"receipt": "synthetic-observation"}}]}
    complete = match_step_effect_evidence(accepted_effect, complete_evidence)
    _evidence("inline-normalization-store-reload", {"raw": raw, "candidate": candidate, "normalized": normalized,
        "normalization_audit": audit, "compiled": compiled, "receipt": receipt, "reloaded": reloaded,
        "missing_evidence_result": missing, "complete_synthetic_evidence": complete_evidence, "complete_evidence_result": complete}, tmp_path)
    assert missing["complete"] is False
    assert complete["complete"] is True
    assert not reloaded.get("current_approval", {}).get("status") == "approved"
    assert not reloaded.get("provenance_state", {}).get("attempts")

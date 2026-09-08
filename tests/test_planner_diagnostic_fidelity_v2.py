"""Planner diagnostic identity and durable machine-evidence regressions.

These are compiler/store tests. Provider-bound feedback is covered by the
Planner integration suite, not implied by these local assertions.
"""
from __future__ import annotations

from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest

from agent_core.plan_tracker import (
    _planning_failure_machine_issue,
    derive_plan_validation_state,
    load_plan_state,
    normalize_validation_issue,
    record_planning_failure,
    save_plan_state,
)
from agent_core.step_effects import compile_plan_dataflow, compile_plan_step_effects


def _step(step_id, *, outputs=(), dependencies=(), mutations=(), side_effects=()):
    return {
        "step_id": step_id,
        "title": "Declared fixture operation",
        "action": "execute",
        "purpose": "analysis",
        "tool": "fixture",
        "inputs": [],
        "outputs": list(outputs),
        "dependencies": list(dependencies),
        "success_criteria": ["The declared effects have accepted evidence"],
        "skill_ids": [],
        "workflow_stage_ids": [],
        "risk_level": "low",
        "workspace_mutations": list(mutations),
        "explicit_side_effects": list(side_effects),
    }


def _resource(local_ref, kind="immutable_artifact"):
    result = {
        "local_ref": local_ref,
        "semantic_view": {"description": "Declared fixture resource"},
        "artifact_contract": {
            "object_kind": "directory" if kind == "mutable_workspace" else "file",
            "resource_kind": kind,
            "lifecycle": "plan_local",
            "materialization_policy_id": "managed_intermediate_v1",
        },
    }
    if kind == "mutable_workspace":
        result["resource_binding"] = {
            "canonical_resource_id": "fixture.workspace",
            "resource_kind": kind,
            "availability_origin": "plan_local",
            "contract_revision": "fixture-v1",
        }
    return result


def _plan(steps, resources):
    return {
        "steps": steps,
        "proposed_artifacts": resources,
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }


def _failure(state, issues, **extra):
    return record_planning_failure(
        state,
        validation_issues=issues,
        planning_epoch_id="diagnostic-epoch",
        task_spec_revision_value=1,
        validation_source="actual_compiler_results",
        **extra,
    )


class PlannerDiagnosticFidelityTests(unittest.TestCase):
    def test_two_resources_same_code_and_path_survive_real_compiler_deduplication(self):
        plan = _plan(
            [_step("first", outputs=["plan.a", "plan.b"]),
             _step("second", outputs=["plan.a", "plan.b"])],
            [_resource("plan.a"), _resource("plan.b")],
        )
        compiled, _, issues = compile_plan_dataflow(
            plan, {}, candidate_scope_id="diagnostic-scope"
        )
        conflicts = [i for i in issues if i["code"] == "PLAN_ARTIFACT_PRODUCER_CARDINALITY"]
        self.assertEqual(len(conflicts), 2)
        self.assertEqual({i["field_path"] for i in conflicts}, {"$.steps"})
        validated = derive_plan_validation_state(compiled, conflicts + deepcopy(conflicts))
        self.assertEqual(len(validated["validation_issues"]), 2)
        machine = [_planning_failure_machine_issue(i) for i in validated["validation_issues"]]
        self.assertEqual(len({i["canonical_resource_id"] for i in machine}), 2)
        self.assertEqual(len({i["issue_id"] for i in machine}), 2)
        self.assertTrue(all(i["producer_step_ids"] == ["first", "second"] for i in machine))

    def test_five_unordered_pairs_remain_independent(self):
        mutation = {
            "workspace_ref": "plan.workspace",
            "mutation": "initialize",
            "requires_available": False,
            "available_after_success": True,
            "contract_source": "fixture.lifecycle",
            "contract_revision": "fixture-v1",
        }
        plan = _plan(
            [_step("a", mutations=[mutation]),
             _step("b", mutations=[mutation], dependencies=["a"]),
             _step("c", mutations=[mutation]),
             _step("d", mutations=[mutation])],
            [_resource("plan.workspace", "mutable_workspace")],
        )
        compiled, _, issues = compile_plan_dataflow(
            plan, {}, candidate_scope_id="diagnostic-scope"
        )
        conflicts = [i for i in issues if i["code"] == "PLAN_MUTATION_ORDER_AMBIGUOUS"]
        self.assertEqual(len(conflicts), 5)
        validated = derive_plan_validation_state(compiled, conflicts + deepcopy(conflicts))
        machine = [_planning_failure_machine_issue(i) for i in validated["validation_issues"]]
        self.assertEqual(len(machine), 5)
        self.assertEqual(
            {tuple(i["actual_facts"]["unordered_mutator_step_ids"]) for i in machine},
            {("a", "c"), ("a", "d"), ("b", "c"), ("b", "d"), ("c", "d")},
        )
        self.assertTrue(all(len(i["rejected_sources"]) == 2 for i in machine))

    def test_same_steps_but_different_edges_do_not_merge(self):
        base = {
            "code": "PLAN_MUTATION_ORDER_AMBIGUOUS", "field_path": "$.steps",
            "message": "Mutations require a declared order",
            "canonical_resource_id": "workspace-id", "affected_step_ids": ["a", "b"],
        }
        issues = [
            {**base, "conflicting_edges": [{"field_path": f"$.steps[0].mutations[{i}]"}]}
            for i in range(2)
        ]
        state = derive_plan_validation_state({}, issues + deepcopy(issues))
        self.assertEqual(len(state["validation_issues"]), 2)

    def test_issue_identity_is_preserved_without_changing_cross_turn_progress(self):
        base = {"code": "PLAN_FIXTURE", "field_path": "$.steps", "actual": []}
        first = {**base, "issue_id": "compiler-issue-a"}
        second = {**base, "issue_id": "compiler-issue-b"}
        validated = derive_plan_validation_state({}, [first, second, deepcopy(first)])
        self.assertEqual(len(validated["validation_issues"]), 2)
        one = _failure({"task_state": {}}, [first])
        two = _failure(one, [second])
        self.assertEqual(two["task_state"]["planning_failure"]["repeat_count"], 1)

    def test_real_effect_subcode_is_preserved(self):
        plan = _plan([_step("prepare", side_effects=["create_reference_indexes"])], [])
        _, _, issues = compile_plan_step_effects(plan, {}, allow_legacy_untyped=False)
        issue = next(i for i in issues if i.get("violation_code") == "EXPLICIT_SIDE_EFFECT_UNSUPPORTED")
        for value in (normalize_validation_issue(issue), _planning_failure_machine_issue(issue)):
            self.assertEqual(value["code"], "STEP_EFFECT_PROTOCOL_INVALID")
            self.assertEqual(value["violation_code"], "EXPLICIT_SIDE_EFFECT_UNSUPPORTED")
            self.assertEqual(value["unsupported_effect_ids"], ["create_reference_indexes"])

    def test_machine_abi_preserves_observed_empty_values_and_contract_source(self):
        issue = {
            "code": "STEP_EFFECT_PROTOCOL_INVALID",
            "violation_code": "EXPLICIT_SIDE_EFFECT_UNSUPPORTED",
            "field_path": "$.steps[0].explicit_side_effects",
            "step_id": "prepare", "unsupported_effect_ids": ["index"],
            "actual": [], "expected": {"supported": ["index"]},
            "constraint_source": "selected-capability", "contract_revision": "7",
            "machine_evidence": {"target_route": "forbidden", "actual_value": None},
        }
        normalized = normalize_validation_issue(issue)
        self.assertEqual(normalized, normalize_validation_issue(normalized))
        machine = _planning_failure_machine_issue(normalized)
        for key in ("code", "violation_code", "unsupported_effect_ids", "field_path",
                    "step_id", "actual", "expected", "constraint_source", "contract_revision"):
            self.assertEqual(machine[key], issue[key])
        self.assertNotIn("target_route", machine["machine_evidence"])
        self.assertIsNone(machine["machine_evidence"]["actual_value"])
        self.assertEqual(machine, _planning_failure_machine_issue(machine))

    def test_full_diagnostics_and_distinct_requirements_survive_real_store_reload(self):
        with TemporaryDirectory(prefix="biomind-diagnostic-fidelity-") as data_path:
            state = load_plan_state(data_path, "diagnostic-task")
            issue = {
                "code": "PLAN_RESOURCE_ALIAS_INVALID", "field_path": "$.proposed_artifacts[0]",
                "canonical_resource_id": "execution-capability-id", "actual": "wrong-domain",
                "expected": "exact resource authority", "violation_code": "RESOURCE_DOMAIN_MISMATCH",
                "constraint_source": "approved-resource-catalog", "contract_revision": "3",
            }
            requirements = [{
                "requirement_id": "ordered-mutations", "source": "public-plan-schema",
                "contract": "shared mutable workspace writes have dependency order",
            }]
            state = _failure(state, [issue], requirements=requirements)
            before = deepcopy(state["task_state"]["planning_failure"])
            save_plan_state(data_path, "diagnostic-task", state)
            loaded = load_plan_state(data_path, "diagnostic-task")
            self.assertNotIn("load_error", loaded)
            after = loaded["task_state"]["planning_failure"]
            self.assertEqual(after["validation_issues"], before["validation_issues"])
            self.assertEqual(after["actual_issues"], before["validation_issues"])
            self.assertEqual(after["requirements"], requirements)
            self.assertEqual(after["attempt"], 1)
            self.assertEqual(len(after["actual_issues"]), 1)
            self.assertNotIn("PLAN_MUTATION_ORDER_AMBIGUOUS", str(after["actual_issues"]))

    def test_twenty_first_step_repair_is_progress_while_display_remains_bounded(self):
        candidate = _plan([_step(f"step-{i}") for i in range(1, 22)], [])
        issue = {
            "code": "PLAN_MUTATION_PRECONDITION_UNSATISFIED",
            "field_path": "$.steps[20]", "affected_step_ids": ["step-21"],
            "affected_fields": ["dependencies"], "actual": "no reachable source",
        }
        first = _failure({"task_state": {}}, [issue], candidate=candidate)
        changed = deepcopy(candidate)
        changed["steps"][20]["dependencies"] = ["step-20"]
        second = _failure(first, [issue], candidate=changed)
        failure = second["task_state"]["planning_failure"]
        self.assertEqual(len(failure["previous_candidate"]["steps"]), 16)
        self.assertEqual(failure["attempt"], 2)
        self.assertEqual(failure["repeat_count"], 0)
        self.assertEqual(failure["correction_progress"], "affected_structure_changed")
        repeated = _failure(second, [issue], candidate=changed)
        self.assertEqual(repeated["task_state"]["planning_failure"]["repeat_count"], 1)

    def test_resource_identity_repair_is_progress_with_affected_steps(self):
        candidate = _plan([_step("write", outputs=["plan.a"])], [_resource("plan.a")])
        candidate["proposed_artifacts"][0]["resource_binding"] = {
            "canonical_resource_id": "wrong-shared-identity",
            "contract_revision": "1",
        }
        issue = {
            "code": "PLAN_ARTIFACT_PRODUCER_CARDINALITY", "field_path": "$.steps",
            "affected_step_ids": ["write"], "affected_fields": ["outputs"],
        }
        first = _failure({"task_state": {}}, [issue], candidate=candidate)
        displayed = first["task_state"]["planning_failure"]["previous_candidate"]
        self.assertEqual(displayed["proposed_artifacts"][0]["resource_binding"],
                         candidate["proposed_artifacts"][0]["resource_binding"])
        changed = deepcopy(candidate)
        changed["proposed_artifacts"][0].pop("resource_binding")
        second = _failure(first, [issue], candidate=changed)
        self.assertEqual(second["task_state"]["planning_failure"]["repeat_count"], 0)
        self.assertEqual(second["task_state"]["planning_failure"]["correction_progress"],
                         "affected_structure_changed")


if __name__ == "__main__":
    unittest.main()

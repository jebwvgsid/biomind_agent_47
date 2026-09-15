from copy import deepcopy
import unittest

from agent_core.plan_bindings import compile_plan_resource_bindings, PlanBindingError
from agent_core.plan_contract import prepare_allocation_intent, promote_allocation_intent
from agent_core.task_spec import normalize_output_binding
from agent_core.node_context import build_node_context
from agent_core.step_effects import compile_plan_dataflow


def candidate(kind="immutable_artifact"):
    return {"metadata": {"resource_contract_version": 2}, "proposed_artifacts": [{
        "local_ref": "plan.new", "semantic_view": {"role": "result", "format": "directory"},
        "artifact_contract": {"object_kind": "directory", "resource_kind": kind, "lifecycle": "plan_local", "materialization_policy_id": "managed_intermediate_v1"},
    }], "steps": []}


def bindings(c, **kw):
    return compile_plan_resource_bindings(c, candidate_scope_id="current", authoritative_handles=kw.pop("authoritative_handles", {}), **kw)


class PlannerResourceProtocolV2Tests(unittest.TestCase):
    def test_new_identity_is_scoped_without_alias_and_directory_stays_immutable(self):
        c = candidate()
        row = bindings(c)["plan.new"]
        self.assertEqual(row["canonical_resource_id"], "candidate:current:plan.new")
        self.assertEqual(row["resource_kind"], "immutable_artifact")
        self.assertFalse(row["initially_available"])
        self.assertNotIn("resource_binding", c["proposed_artifacts"][0])
        other = compile_plan_resource_bindings(c, candidate_scope_id="other", authoritative_handles={})
        self.assertNotEqual(row["canonical_resource_id"], other["plan.new"]["canonical_resource_id"])

    def test_existing_reference_copies_current_authority_and_rejects_stale_revision_scope_alias(self):
        c = candidate()
        c["proposed_artifacts"][0]["resource_binding"] = {"resource_ref": "input.current"}
        authority = {"input.current": {"metadata": {"contract_revision": "r7", "resource_kind": "immutable_artifact"}}}
        kw = {"authoritative_handles": {"input.current": "existing-id"}, "authoritative_bindings": authority}
        row = bindings(c, **kw)["plan.new"]
        self.assertEqual((row["canonical_resource_id"], row["contract_revision"]), ("existing-id", "r7"))
        for bad in ({"contract_revision": "r6"}, {"candidate_scope_id": "old"}, {"aliases": ["invented"]}, {"resource_kind": "mutable_workspace"}):
            rejected = deepcopy(c)
            rejected["proposed_artifacts"][0]["resource_binding"].update(bad)
            with self.subTest(bad=bad), self.assertRaises(PlanBindingError):
                bindings(rejected, **kw)

    def test_capability_domain_is_not_resource_authority_and_all_fields_reported(self):
        c = candidate()
        c["proposed_artifacts"][0]["resource_binding"] = {"canonical_resource_id": "opaque-ability"}
        second = deepcopy(c["proposed_artifacts"][0]); second["local_ref"] = "plan.other"
        c["proposed_artifacts"].append(second)
        with self.assertRaises(PlanBindingError) as caught:
            bindings(c, capability_ids=["opaque-ability"])
        issues = caught.exception.to_dict()["validation_issues"]
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(x["code"] == "PLAN_RESOURCE_REFERENCE_WRONG_DOMAIN" for x in issues))
        self.assertEqual(len({x["field_path"] for x in issues}), 2)

    def test_legacy_reader_keeps_its_explicit_binding_but_live_gate_rejects_it(self):
        c = candidate(); c["metadata"] = {}
        c["proposed_artifacts"][0]["resource_binding"] = {"canonical_resource_id": "legacy", "resource_kind": "immutable_artifact"}
        self.assertEqual(bindings(c)["plan.new"]["canonical_resource_id"], "legacy")
        with self.assertRaises(PlanBindingError):
            bindings(c, require_declared_references=True)

    def test_conflicting_authoritative_revisions_are_not_merged(self):
        with self.assertRaises(PlanBindingError):
            bindings(candidate(), authoritative_handles={"input.a": "same", "input.b": "same"}, authoritative_bindings={
                "input.a": {"metadata": {"contract_revision": "a"}}, "input.b": {"metadata": {"contract_revision": "b"}},
            })

    def test_explicit_workspace_intent_supports_future_state_without_disk(self):
        c = candidate("mutable_workspace")
        def step(sid, dependencies, before, after, requires=True):
            return {"step_id": sid, "inputs": [], "outputs": [], "dependencies": dependencies,
                    "workspace_mutations": [{"workspace_ref": "plan.new", "mutation": "update", "requires_available": requires,
                                             "requires_state": before, "available_after_success": True, "establishes_state": after}]}
        c["steps"] = [step("init", [], "", "ready", False), step("use", ["init"], "ready", "used")]
        _, _, issues = compile_plan_dataflow(c, candidate_scope_id="current", authoritative_handles={}, compile_effect_contracts=False)
        self.assertEqual(issues, [])
        for mutation, expected in (("dependency", "PLAN_MUTATION_ORDER_AMBIGUOUS"), ("state", "PLAN_MUTATION_PRECONDITION_UNSATISFIED"), ("kind", "PLAN_RESOURCE_KIND_MISMATCH"), ("same_step", "PLAN_MUTATION_PRECONDITION_UNSATISFIED"), ("cycle", "PLAN_DEPENDENCY_CYCLE")):
            x = deepcopy(c)
            if mutation == "dependency": x["steps"][1]["dependencies"] = []
            elif mutation == "state": x["steps"][1]["workspace_mutations"][0]["requires_state"] = "never_established"
            elif mutation == "kind": x["proposed_artifacts"][0]["artifact_contract"]["resource_kind"] = "immutable_artifact"
            elif mutation == "same_step": x["steps"][0]["workspace_mutations"][0].update(requires_available=True, requires_state="ready")
            else: x["steps"][0]["dependencies"] = ["use"]
            _, _, rejected = compile_plan_dataflow(x, candidate_scope_id="current", authoritative_handles={}, compile_effect_contracts=False)
            self.assertIn(expected, {i["code"] for i in rejected}, mutation)

    def test_task_lifecycle_declaration_reaches_provider_and_authoritative_binding(self):
        output = normalize_output_binding({"role": "workspace", "artifact_kind": "directory", "resource_kind": "mutable_workspace", "contract_revision": "declared-v1"})
        ctx = build_node_context(node_type="planner", task_spec={"schema_version": 1, "task_id": "scope", "output_contracts": [output]}, plan_state={}, state_generation=0)
        rows = [x for x in ctx.binding.semantic_handle_map.values() if x.get("direction") == "output"]
        self.assertEqual(rows[0]["metadata"]["resource_kind"], "mutable_workspace")
        self.assertIn("mutable_workspace", str(ctx.llm_payload.to_provider_dict()))
        plain = normalize_output_binding({"role": "workspace", "artifact_kind": "directory"})
        self.assertNotIn("resource_kind", plain)
        with self.assertRaises(ValueError):
            normalize_output_binding({"role": "file", "artifact_kind": "file", "resource_kind": "mutable_workspace"})

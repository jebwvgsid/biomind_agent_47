"""INPUT-FIX leaf acceptance for explicit instances and scalar resource paths.

The separate production test owns Consultant/provider/store/graph integration.
These tests exercise the actual canonicalization, mutation and reader bindings
with synthetic files under a temporary directory.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core.action_runtime import project_artifacts_for_provider, serialize_action_capability_instructions
from agent_core.approved_resource_locator import approved_resource_locator, open_approved_resource
from agent_core.managed_artifacts import build_managed_artifact_bindings, concrete_artifact_provider_view
from agent_core.node_context import build_node_context
from agent_core.task_spec import (
    InputBindingContractError,
    apply_task_spec_mutation_proposal,
    canonical_current_task_spec,
    merge_task_spec,
    normalize_input_binding,
    render_task_spec,
    task_semantic_hash,
    validate_input_binding_declaration,
)


def _proposal(*operations):
    return {
        "proposal_type": "task_spec_mutation", "schema_version": 1,
        "operations": list(operations), "unresolved_ambiguities": [],
        "proposal_evidence_refs": [],
    }


def _upsert(row):
    return {"op": "upsert_input", "input": deepcopy(row), "supporting_evidence": []}


class InputInstanceContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="input-instance-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rows = []
        # Basenames intentionally provide no sample/mate clues: grouping must
        # come from the explicit instance, not from a filename heuristic.
        for group in ("S1", "S2"):
            for member in ("R1", "R2"):
                path = self.root / group / member / "reads.fq"
                path.parent.mkdir(parents=True)
                path.write_text(f"synthetic {group}/{member}", encoding="utf-8")
                self.rows.append({
                    "role": "paired_fastq", "path": str(path), "required": True,
                    "input_instance": {"schema_version": 1, "group_id": group, "member_id": member},
                })

    def _apply(self, source, *operations):
        return apply_task_spec_mutation_proposal(
            source, _proposal(*operations), task_id="input-instance-task",
            base_dir=self.root,
        )

    def _spec(self):
        return self._apply(None, {"op": "set_goal", "value": "Use both explicitly paired samples"}, *map(_upsert, self.rows))

    def test_four_scalar_members_and_same_operations_are_idempotent(self):
        spec = self._spec()
        self.assertEqual(len(spec["inputs"]), 4)
        self.assertEqual(len({row["semantic_handle"] for row in spec["inputs"]}), 4)
        repeated = self._apply(spec, *map(_upsert, self.rows))
        self.assertEqual(repeated, spec)
        reloaded = canonical_current_task_spec(json.loads(json.dumps(spec)))
        self.assertEqual(reloaded["inputs"], spec["inputs"])
        self.assertEqual(task_semantic_hash(reloaded), task_semantic_hash(spec))
        reordered = canonical_current_task_spec({**spec, "inputs": list(reversed(spec["inputs"]))})
        self.assertEqual(
            {row["canonical_path"]: row["semantic_handle"] for row in reordered["inputs"]},
            {row["canonical_path"]: row["semantic_handle"] for row in spec["inputs"]},
        )

    def test_one_member_correction_preserves_other_sample_and_stable_handle(self):
        spec = self._spec()
        old = deepcopy(spec["inputs"])
        corrected = deepcopy(self.rows[2])
        corrected["path"] = str(self.root / "replacement.fq")
        Path(corrected["path"]).write_text("synthetic replacement", encoding="utf-8")
        result = self._apply(spec, _upsert(corrected))
        self.assertEqual(result["inputs"][:2], old[:2])
        self.assertEqual(result["inputs"][3], old[3])
        self.assertEqual(result["inputs"][2]["semantic_handle"], old[2]["semantic_handle"])
        self.assertNotEqual(result["inputs"][2]["artifact_id"], old[2]["artifact_id"])
        self.assertEqual(result["revision"], spec["revision"] + 1)
        self.assertEqual(self._apply(result, _upsert(corrected)), result)

    def test_invalid_list_does_not_partially_apply_an_earlier_operation(self):
        spec = self._spec()
        before = deepcopy(spec)
        invalid = {"role": "paired_fastq", "path": [self.rows[0]["path"], self.rows[1]["path"]]}
        with self.assertRaises(InputBindingContractError) as caught:
            self._apply(spec, {"op": "set_goal", "value": "must not persist"}, _upsert(invalid))
        self.assertEqual(caught.exception.code, "INPUT_BINDING_PATH_TYPE_INVALID")
        self.assertEqual(spec, before)
        self.assertEqual(len(spec["inputs"]), 4)

    def test_scalar_types_are_rejected_before_path_resolution_with_exact_pointer(self):
        for field in ("path", "canonical_path", "resolved_path"):
            for invalid in (["a", "b"], {"R1": "a"}, 7, False):
                with self.subTest(field=field, actual=type(invalid).__name__):
                    with patch("agent_core.task_spec._resolved_binding_path", side_effect=AssertionError("must not resolve")):
                        with self.assertRaises(InputBindingContractError) as caught:
                            normalize_input_binding({"role": "paired_fastq", field: invalid})
                    self.assertEqual(caught.exception.field_path, f"$.{field}")
                    self.assertEqual(caught.exception.actual["value"], invalid)
        with self.assertRaises(InputBindingContractError) as caught:
            validate_input_binding_declaration(
                {"path": ["a", "b"]}, field_path="$.task_spec_operations[2].input",
            )
        self.assertEqual(caught.exception.to_diagnostic()["field_path"], "$.task_spec_operations[2].input.path")
        self.assertEqual(caught.exception.to_dict(), caught.exception.to_diagnostic())

    def test_versioned_instance_has_closed_explicit_semantic_members(self):
        for instance, code in (
            ({}, "INPUT_INSTANCE_INVALID"),
            ({"schema_version": 2, "group_id": "S1", "member_id": "R1"}, "INPUT_INSTANCE_SCHEMA_UNSUPPORTED"),
            ({"schema_version": True, "group_id": "S1", "member_id": "R1"}, "INPUT_INSTANCE_SCHEMA_UNSUPPORTED"),
            ({"schema_version": 1, "group_id": [], "member_id": "R1"}, "INPUT_INSTANCE_INVALID"),
            ({"schema_version": 1, "group_id": "S1", "member_id": " "}, "INPUT_INSTANCE_INVALID"),
            ({"schema_version": 1, "group_id": "S1", "member_id": "R1", "guess": True}, "INPUT_INSTANCE_INVALID"),
        ):
            with self.subTest(instance=instance):
                with self.assertRaises(InputBindingContractError) as caught:
                    normalize_input_binding({**self.rows[0], "input_instance": instance})
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(InputBindingContractError) as caught:
            normalize_input_binding({"role": "paired_fastq", "input_instance": self.rows[0]["input_instance"]})
        self.assertEqual(caught.exception.code, "INPUT_BINDING_PATH_MISSING")

    def test_multi_instance_role_requires_selector_for_upsert_and_remove(self):
        spec = self._spec()
        for operation in (
            _upsert({"role": "paired_fastq", "path": self.rows[0]["path"]}),
            {"op": "remove_input", "role": "paired_fastq"},
        ):
            with self.assertRaises(InputBindingContractError) as caught:
                self._apply(spec, operation)
            self.assertEqual(caught.exception.code, "INPUT_INSTANCE_SELECTOR_REQUIRED")
        result = self._apply(spec, {
            "op": "remove_input", "role": "paired_fastq",
            "input_instance": deepcopy(self.rows[2]["input_instance"]),
        })
        self.assertEqual(len(result["inputs"]), 3)
        self.assertEqual(result["inputs"][:2], spec["inputs"][:2])
        self.assertEqual(self._apply(result, {
            "op": "remove_input", "role": "paired_fastq",
            "input_instance": deepcopy(self.rows[2]["input_instance"]),
        }), result)

    def test_legacy_scalar_remains_compatible_without_implicit_collection_conversion(self):
        legacy = {"role": "reference_fasta", "path": str(self.root / "reference.fa")}
        first = self._apply(None, {"op": "set_goal", "value": "Use reference"}, _upsert(legacy))
        self.assertEqual(first["inputs"][0]["semantic_handle"], "input.reference_fasta")
        self.assertNotIn("input_instance", first["inputs"][0])
        changed = self._apply(first, _upsert({**legacy, "path": str(self.root / "changed.fa")}))
        self.assertEqual(len(changed["inputs"]), 1)
        self.assertTrue(changed["inputs"][0]["canonical_path"].endswith("changed.fa"))
        self.assertEqual(self._apply(changed, {"op": "remove_input", "role": "reference_fasta"})["inputs"], [])
        # Historical stringified values stay opaque strings; never eval or
        # silently rewrite a historical session into a new collection schema.
        opaque = normalize_input_binding({"role": "paired_fastq", "path": "['a', 'b']"})
        self.assertNotIn("input_instance", opaque)
        self.assertEqual(opaque["canonical_path"], "['a', 'b']")

    def test_merge_keys_and_alias_dedupe_preserve_explicit_instance_scope(self):
        spec = self._spec()
        self.assertEqual(merge_task_spec(spec, {"inputs": self.rows}, source="consultant_inferred", reason="replay"), spec)
        conflict = deepcopy(spec)
        conflict["inputs"].append({**deepcopy(spec["inputs"][0]), "canonical_path": str(self.root / "other.fq")})
        with self.assertRaises(InputBindingContractError) as caught:
            canonical_current_task_spec(conflict)
        self.assertEqual(caught.exception.code, "INPUT_INSTANCE_CONFLICT")
        repeated = canonical_current_task_spec({**spec, "inputs": [*spec["inputs"], deepcopy(spec["inputs"][0])]})
        self.assertEqual(len(repeated["inputs"]), 4)

    def test_grouped_declaration_requires_explicit_removal_of_legacy_singleton(self):
        legacy = {key: deepcopy(value) for key, value in self.rows[0].items() if key != "input_instance"}
        spec = self._apply(None, {"op": "set_goal", "value": "Use explicitly paired samples"}, _upsert(legacy))
        before = deepcopy(spec)
        with self.assertRaises(InputBindingContractError) as caught:
            self._apply(spec, _upsert(self.rows[0]))
        self.assertEqual(caught.exception.code, "INPUT_INSTANCE_LEGACY_ROLE_CONFLICT")
        self.assertEqual(spec, before)
        replaced = self._apply(spec, {"op": "remove_input", "role": "paired_fastq"}, *map(_upsert, self.rows))
        self.assertEqual(len(replaced["inputs"]), 4)
        self.assertTrue(all("input_instance" in row for row in replaced["inputs"]))

    def test_task_projection_and_real_managed_reader_keep_both_pairings(self):
        spec = self._spec()
        context = build_node_context(node_type="planner", task_spec=spec)
        payload = context.llm_payload.to_provider_dict()
        projected = payload["semantic_history"]["task_spec"]["inputs"]
        self.assertEqual(len(projected), 4)
        expected = {(row["input_instance"]["group_id"], row["input_instance"]["member_id"]) for row in self.rows}
        self.assertEqual({(row["input_instance"]["group_id"], row["input_instance"]["member_id"]) for row in projected}, expected)
        canonical_prompt = render_task_spec(spec, max_chars=100000)
        self.assertIn('"input_instance"', canonical_prompt)
        self.assertEqual(json.loads(canonical_prompt)["inputs"][0]["input_instance"]["group_id"], "S1")
        bindings = build_managed_artifact_bindings(spec)
        self.assertEqual(len(bindings), 4)
        concrete = concrete_artifact_provider_view(bindings)
        self.assertEqual(len(concrete), 4)
        concrete = json.loads(serialize_action_capability_instructions({
            "current_action_artifact_bindings": project_artifacts_for_provider(concrete),
        }))["current_action_artifact_bindings"]
        self.assertEqual(len(concrete), 4)
        observed = {}
        for row in concrete:
            instance = row["input_instance"]
            locator = approved_resource_locator({"canonical_path": row["path"]})
            with open_approved_resource(locator["path"]) as reader:
                observed[(instance["group_id"], instance["member_id"])] = reader.read().decode("utf-8")
        self.assertEqual(set(observed), expected)
        self.assertEqual(observed[("S1", "R1")], "synthetic S1/R1")
        self.assertEqual(observed[("S2", "R2")], "synthetic S2/R2")


if __name__ == "__main__":
    unittest.main()

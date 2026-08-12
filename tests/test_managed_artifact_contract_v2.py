from __future__ import annotations

import unittest

from agent_core.managed_artifacts import (
    build_managed_artifact_bindings,
    classify_output_mismatch,
    managed_artifact_llm_view,
    materialize_managed_action,
    validate_artifact_relocation,
    validate_materialized_artifacts,
    validate_observed_artifacts,
    validate_plan_managed_artifacts,
)


def _task(path: str = "/output/aa.fa", *, relocatable: bool = False) -> dict:
    return {
        "inputs": [
            {
                "input_id": "input-uuid",
                "role": "source_fasta",
                "path": "/data/source with spaces;safe.fa",
                "format": "fasta",
            }
        ],
        "output_contracts": [
            {
                "output_id": "output-uuid",
                "role": "final_fasta",
                "path": path,
                "format": "fasta",
                "required": True,
                "producer_step_id": "step-1",
                "relocatable": relocatable,
                "validation_contract_id": "fasta-output-v1",
            }
        ],
    }


class ManagedArtifactMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bindings = build_managed_artifact_bindings(_task())

    def test_llm_view_has_handle_and_placeholder_but_no_path_or_uuid(self) -> None:
        view = managed_artifact_llm_view(self.bindings)
        rendered = str(view)
        self.assertIn("output.final_fasta", rendered)
        self.assertIn("{output:final_fasta}", rendered)
        self.assertNotIn("/output/aa.fa", rendered)
        self.assertNotIn("output-uuid", rendered)

    def test_structured_action_materializes_exact_paths_as_single_argv_values(self) -> None:
        proposal = {
            "action_kind": "command",
            "argv": [
                "tool",
                "{input:source_fasta}",
                "-o",
                "{output:final_fasta}",
            ],
            "outputs": ["output.final_fasta"],
        }
        materialized, violations = materialize_managed_action(proposal, self.bindings)
        self.assertEqual(violations, [])
        self.assertEqual(materialized["argv"][1], "/data/source with spaces;safe.fa")
        self.assertEqual(materialized["argv"][3], "/output/aa.fa")
        self.assertEqual(
            materialized["materialized_artifacts"]["output.final_fasta"],
            "/output/aa.fa",
        )

    def test_operation_handle_is_resolved_only_by_controller(self) -> None:
        proposal = {
            "argv": [
                "{operation:operation.C1.O1}",
                "{input:source_fasta}",
                "-o",
                "{output:final_fasta}",
            ],
            "outputs": ["output.final_fasta"],
        }
        materialized, violations = materialize_managed_action(
            proposal,
            self.bindings,
            operation_bindings={"operation.C1.O1": "/pinned/skill/run.py"},
        )
        self.assertEqual(violations, [])
        self.assertEqual(materialized["argv"][0], "/pinned/skill/run.py")
        self.assertEqual(
            materialized["materialized_operations"],
            {"operation.C1.O1": "/pinned/skill/run.py"},
        )

    def test_unknown_or_embedded_operation_handle_fails_before_execution(self) -> None:
        _, unknown = materialize_managed_action(
            {"argv": ["{operation:operation.C9.O9}"]},
            self.bindings,
            operation_bindings={},
        )
        self.assertEqual(unknown[0].code, "MANAGED_OPERATION_HANDLE_UNKNOWN")
        _, embedded = materialize_managed_action(
            {"argv": ["python={operation:operation.C1.O1}"]},
            self.bindings,
            operation_bindings={"operation.C1.O1": "/pinned/run.py"},
        )
        self.assertEqual(embedded[0].code, "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED")

    def test_unknown_duplicate_and_wrong_direction_fail_before_execution(self) -> None:
        _, unknown = materialize_managed_action(
            {"argv": ["tool", "{output:unknown}"], "outputs": ["output.unknown"]},
            self.bindings,
        )
        self.assertIn("MANAGED_ARTIFACT_HANDLE_UNKNOWN", {item.code for item in unknown})

        _, duplicate = materialize_managed_action(
            {
                "argv": ["tool", "{output:final_fasta}"],
                "outputs": ["output.final_fasta", "output.final_fasta"],
            },
            self.bindings,
        )
        self.assertIn("MANAGED_ARTIFACT_HANDLE_DUPLICATE", {item.code for item in duplicate})

        _, wrong = materialize_managed_action(
            {"argv": ["tool"], "outputs": ["input.source_fasta"]},
            self.bindings,
        )
        self.assertIn("MANAGED_ARTIFACT_DIRECTION_MISMATCH", {item.code for item in wrong})

    def test_embedded_or_free_shell_placeholder_is_rejected(self) -> None:
        _, embedded = materialize_managed_action(
            {"argv": ["tool", "--out={output:final_fasta}"]},
            self.bindings,
        )
        self.assertEqual(embedded[0].code, "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED")
        _, shell = materialize_managed_action(
            {"code": "tool -o {output:final_fasta}"},
            self.bindings,
        )
        self.assertIn("MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL", {item.code for item in shell})

    def test_new_literal_is_rejected_and_legacy_exact_is_audited(self) -> None:
        proposal = {
            "argv": ["tool", "-o", "/output/aa.fa"],
            "outputs": ["output.final_fasta"],
        }
        _, violations = materialize_managed_action(proposal, self.bindings)
        self.assertIn("MANAGED_ARTIFACT_HANDLE_REQUIRED", {item.code for item in violations})
        materialized, legacy_violations = materialize_managed_action(
            proposal,
            self.bindings,
            allow_legacy_exact_literals=True,
        )
        self.assertEqual(legacy_violations, [])
        self.assertEqual(
            materialized["compatibility_hits"][0]["code"],
            "LEGACY_MANAGED_LITERAL_EXACT",
        )

    def test_declared_output_without_placeholder_is_action_mismatch(self) -> None:
        _, violations = materialize_managed_action(
            {"argv": ["tool"], "outputs": ["output.final_fasta"]},
            self.bindings,
        )
        match = next(item for item in violations if item.code == "ACTION_OUTPUT_BINDING_MISMATCH")
        self.assertEqual(match.owner, "GENERATED_ACTION")
        self.assertEqual(match.route, "generate")


class ArtifactMismatchOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bindings = build_managed_artifact_bindings(_task())
        self.output = self.bindings["output.final_fasta"]

    def test_aa_bb_is_owned_by_first_divergent_layer(self) -> None:
        cases = (
            ({"plan_path": "/output/bb.fa"}, "plan_binding", "planner_repair"),
            ({"proposed_path": "/output/bb.fa"}, "GENERATED_ACTION", "generate"),
            ({"materialized_path": "/output/bb.fa"}, "controller_projection", "internal_block"),
            ({"observed_path": "/output/bb.fa"}, "observed_artifact_mismatch", "recovery"),
        )
        for kwargs, owner, route in cases:
            with self.subTest(owner=owner):
                violation = classify_output_mismatch(task_binding=self.output, **kwargs)
                self.assertIsNotNone(violation)
                self.assertEqual(violation.owner, owner)
                self.assertEqual(violation.route, route)
                self.assertEqual(violation.required_filename, "aa.fa")

    def test_plan_redefinition_is_planner_owned(self) -> None:
        plan = {
            "steps": [
                {
                    "step_id": "step-1",
                    "bound_outputs": [
                        {
                            "semantic_handle": "output.final_fasta",
                            "path": "/output/bb.fa",
                        }
                    ],
                }
            ]
        }
        violations = validate_plan_managed_artifacts(plan, self.bindings)
        self.assertEqual(violations[0].code, "PLAN_OUTPUT_BINDING_MISMATCH")
        self.assertEqual(violations[0].owner, "plan_binding")

    def test_controller_wrong_materialization_is_internal(self) -> None:
        violations = validate_materialized_artifacts(
            {"materialized_artifacts": {"output.final_fasta": "/output/bb.fa"}},
            self.bindings,
        )
        self.assertEqual(violations[0].owner, "controller_projection")
        self.assertEqual(violations[0].route, "internal_block")

    def test_observed_wrong_path_is_attempt_scoped_recovery(self) -> None:
        violations = validate_observed_artifacts(
            [
                {
                    "semantic_handle": "output.final_fasta",
                    "path": "/output/bb.fa",
                    "attempt_id": "attempt-1",
                    "content_valid": True,
                }
            ],
            self.bindings,
            attempt_id="attempt-1",
        )
        self.assertEqual(violations[0].code, "OBSERVED_ARTIFACT_PATH_MISMATCH")
        self.assertEqual(violations[0].owner, "observed_artifact_mismatch")


class ArtifactRelocationTests(unittest.TestCase):
    def test_all_six_conditions_are_required(self) -> None:
        binding = build_managed_artifact_bindings(_task(relocatable=True))["output.final_fasta"]
        self.assertEqual(
            validate_artifact_relocation(
                binding=binding,
                source_path="/output/bb.fa",
                source_attempt_id="attempt-1",
                current_attempt_id="attempt-1",
                content_valid=True,
                intended_candidate_count=1,
                preserves_semantics=True,
                companion_set_complete=True,
            ),
            [],
        )
        violation = validate_artifact_relocation(
            binding=binding,
            source_path="/output/bb.fa",
            source_attempt_id="old-attempt",
            current_attempt_id="attempt-1",
            content_valid=False,
            intended_candidate_count=2,
            preserves_semantics=False,
            companion_set_complete=False,
        )[0]
        self.assertIn("source_not_from_current_attempt", violation.message)
        self.assertIn("content_validation_failed", violation.message)
        self.assertIn("candidate_not_unique", violation.message)

    def test_contract_must_explicitly_allow_relocation(self) -> None:
        binding = build_managed_artifact_bindings(_task(relocatable=False))["output.final_fasta"]
        violation = validate_artifact_relocation(
            binding=binding,
            source_path="/output/bb.fa",
            source_attempt_id="attempt-1",
            current_attempt_id="attempt-1",
            content_valid=True,
            intended_candidate_count=1,
            preserves_semantics=True,
            companion_set_complete=True,
        )[0]
        self.assertIn("contract_does_not_allow_relocation", violation.message)


if __name__ == "__main__":
    unittest.main()

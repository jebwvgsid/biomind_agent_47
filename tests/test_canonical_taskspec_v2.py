from __future__ import annotations

import unittest

from agent_core.task_spec import (
    TASK_SPEC_SCHEMA_VERSION,
    apply_task_spec_mutation_proposal,
    apply_user_feedback,
    constraint_mutation_proposal,
    create_task_spec,
    ensure_task_spec,
)


class CanonicalTaskSpecV2Tests(unittest.TestCase):
    def test_new_task_has_no_historical_or_equal_aliases(self) -> None:
        spec = create_task_spec(
            "Use /data/sample.vcf and write annotated.vcf to /output",
            task_id="task-a",
            raw_submission_ref="object:sha256:" + "a" * 64,
        )
        self.assertEqual(spec["schema_version"], TASK_SPEC_SCHEMA_VERSION)
        for key in (
            "request_envelope", "user_directives", "change_log", "facts",
            "artifact_bindings", "expected_outputs", "effective_user_facts",
        ):
            self.assertNotIn(key, spec)
        self.assertEqual(spec["raw_submission_ref"], "object:sha256:" + "a" * 64)

    def test_artifacts_have_one_canonical_managed_contract(self) -> None:
        spec = create_task_spec(
            "Input /data/sample.vcf; expected output annotated.vcf",
            task_id="task-a",
        )
        for direction, rows in (
            ("input", spec["inputs"]),
            ("output", spec["output_contracts"]),
        ):
            for row in rows:
                self.assertTrue(row["artifact_id"].startswith("artifact-"))
                self.assertTrue(row["semantic_handle"].startswith(direction + "."))
                self.assertEqual(row["direction"], direction)
                self.assertIn(row["path_policy"], {"managed_exact", "model_selectable"})
                self.assertNotIn("resolved_path", row)
                self.assertNotIn("path_or_name", row)

    def test_legacy_aliases_are_read_once_and_not_rewritten(self) -> None:
        migrated = ensure_task_spec({
            "schema_version": 4,
            "task_id": "old",
            "revision": 2,
            "goal": "run",
            "expected_outputs": [{"role": "report", "filename": "report.tsv"}],
            "facts": [{"fact_kind": "artifact", "locator": "/old/input.vcf"}],
            "change_log": [{"revision": 1}],
        })
        self.assertIn("output_contracts", migrated)
        self.assertNotIn("expected_outputs", migrated)
        self.assertNotIn("facts", migrated)
        self.assertNotIn("change_log", migrated)

    def test_provenance_only_feedback_does_not_force_revision(self) -> None:
        spec = create_task_spec("run", task_id="task-a")
        updated = apply_user_feedback(
            spec,
            "acknowledged",
            kind="plan_feedback",
            change_type="plan_feedback",
            source_message_id="message-2",
        )
        self.assertEqual(updated["revision"], spec["revision"])
        self.assertIn("message-2", updated["source_refs"])

    def test_consultant_constraint_delta_uses_open_text_mutation_protocol(self) -> None:
        spec = create_task_spec("annotate variants", task_id="task-a")
        proposal = constraint_mutation_proposal([{
            "semantic_key": "required_behavior.database_source",
            "operator": "eq",
            "value": "custom_genome",
            "strength": "hard",
            "source_ref": "U1",
        }])

        updated = apply_task_spec_mutation_proposal(spec, proposal)

        self.assertEqual(updated["revision"], spec["revision"] + 1)
        self.assertEqual(
            updated["requirements"][0]["value"],
            "custom_genome",
        )
        self.assertNotIn("semantic_key", updated["requirements"][0])
        self.assertNotIn("constraint_id", updated["requirements"][0])
        replay = apply_task_spec_mutation_proposal(updated, proposal)
        self.assertEqual(replay["revision"], updated["revision"])

    def test_consultant_mutation_cannot_return_identity_or_merge_patch(self) -> None:
        spec = create_task_spec("annotate variants", task_id="task-a")
        proposal = constraint_mutation_proposal([])
        proposal["task_semantic_hash"] = "model-controlled"
        with self.assertRaisesRegex(ValueError, "TASKSPEC_MUTATION_STRUCTURE_INVALID"):
            apply_task_spec_mutation_proposal(spec, proposal)

        proposal = constraint_mutation_proposal([])
        proposal["operations"] = [{
            "op": "replace_task_spec",
            "value": {"goal": "override"},
            "supporting_evidence": [],
        }]
        with self.assertRaisesRegex(ValueError, "TASKSPEC_MUTATION_OPERATION_FORBIDDEN"):
            apply_task_spec_mutation_proposal(spec, proposal)


if __name__ == "__main__":
    unittest.main()

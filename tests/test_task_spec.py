import json
import tempfile
import unittest
from pathlib import Path

from agent_core.task_spec import (
    TASK_SPEC_SCHEMA_VERSION,
    apply_user_feedback,
    classify_user_feedback_change,
    create_task_spec,
    ensure_task_spec,
    merge_task_spec_input_audit,
    merge_compiled_constraints,
    merge_task_spec,
    project_task_spec_to_renderer,
    render_task_spec,
    evaluate_requirements,
    task_spec_constraints,
    task_spec_revision,
    validate_compiled_constraints,
    semantic_constraints_from_task_spec,
)


class TaskSpecTests(unittest.TestCase):
    def test_initial_request_has_one_canonical_goal_and_source_reference(self):
        spec = create_task_spec("run annotation; do not guess the genome")

        self.assertEqual(spec["revision"], 1)
        self.assertEqual(spec["goal"], "run annotation; do not guess the genome")
        self.assertEqual(len(spec["source_refs"]), 1)
        self.assertNotIn("user_directives", spec)

    def test_user_feedback_appends_and_increments_revision(self):
        spec = create_task_spec("run DE")
        updated = apply_user_feedback(
            spec,
            "use stimulated as target",
            kind="ask_user_answer",
            question="Which group is target?",
        )

        self.assertEqual(task_spec_revision(updated), 2)
        self.assertEqual(updated["goal"], "run DE")
        self.assertEqual(updated["confirmed_decisions"][-1]["answer"], "use stimulated as target")
        self.assertIn("use stimulated as target", render_task_spec(updated))

    def test_plan_feedback_does_not_increment_task_revision(self):
        spec = create_task_spec("run DE")
        updated = apply_user_feedback(
            spec,
            "the plan is too verbose",
            kind="plan_review_feedback",
            change_type="plan_feedback",
        )

        self.assertEqual(task_spec_revision(updated), 1)
        self.assertEqual(updated["confirmed_decisions"], spec["confirmed_decisions"])
        self.assertGreater(len(updated["source_refs"]), len(spec["source_refs"]))
        self.assertEqual(task_spec_constraints(updated), [])

    def test_requirement_feedback_classifier_distinguishes_plan_style(self):
        self.assertEqual(
            classify_user_feedback_change("plan_review_feedback", "do not run loop calling"),
            "requirement_change",
        )
        self.assertEqual(
            classify_user_feedback_change("plan_review_feedback", "the plan is too verbose"),
            "plan_feedback",
        )

    def test_rendered_task_spec_is_always_valid_json(self):
        spec = create_task_spec("分析" * 5000)
        rendered = render_task_spec(spec, max_chars=1200)

        self.assertIsInstance(json.loads(rendered), dict)

    def test_merge_updates_input_record_without_duplicate(self):
        spec = create_task_spec("annotate variants")
        first = merge_task_spec(
            spec,
            {"inputs": [{"path": "/data/a.vcf", "role": "variants", "status": "declared"}]},
            source="consultant",
            reason="input_audit",
        )
        second = merge_task_spec(
            first,
            {"inputs": [{"path": "/data/a.vcf", "role": "variants", "status": "verified"}]},
            source="consultant",
            reason="input_verified",
        )

        self.assertEqual(len(second["inputs"]), 1)
        self.assertEqual(second["inputs"][0]["status"], "verified")

    def test_ensure_handles_legacy_missing_spec(self):
        spec = ensure_task_spec(None, "legacy goal")
        self.assertEqual(spec["goal"], "legacy goal")
        self.assertEqual(spec["revision"], 1)

    def test_later_user_feedback_is_a_current_decision_until_compiled(self):
        spec = apply_user_feedback(
            create_task_spec("run analysis"),
            "do not replace the custom reference",
            kind="plan_review_feedback",
        )

        self.assertEqual(task_spec_constraints(spec), [])
        self.assertEqual(spec["confirmed_decisions"][-1]["answer"], "do not replace the custom reference")

    def test_required_path_is_rechecked_instead_of_trusting_present_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "input.vcf"
            path.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
            requirement = {
                "requirement_id": "input_vcf",
                "path": str(path),
                "level": "required",
                "present": True,
            }
            self.assertFalse(evaluate_requirements([requirement])["blocked"])
            path.unlink()
            result = evaluate_requirements([requirement])

        self.assertTrue(result["blocked"])
        self.assertEqual(result["missing_required"], ["input_vcf"])

    def test_user_supplied_inputs_become_required_requirements(self):
        spec = merge_task_spec(
            create_task_spec("annotate variants"),
            {"inputs": [{"path": "/data/input.vcf", "role": "variants"}]},
            source="user_feedback",
            reason="explicit_input",
        )

        constraints = semantic_constraints_from_task_spec(spec)
        self.assertEqual(constraints[0]["level"], "required")
        self.assertEqual(constraints[0]["semantic_key"], "required_input.variant_vcf")
        self.assertIn("requirements", json.loads(render_task_spec(spec)))

    def test_request_extracts_explicit_inputs_requirements_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data = root / "data"
            data.mkdir()
            for name in ("all.filtered.snp.vcf", "genome.fasta", "genome.gtf"):
                (data / name).write_text("x\n", encoding="utf-8")
            request = (
                "Use data/all.filtered.snp.vcf, data/genome.fasta and data/genome.gtf "
                "for a custom genome with -gtf22; do not guess a species database.\n"
                "Output directory: results/strict\n"
                "Expected outputs:\n- annotated.vcf\n- report.md"
            )

            spec = create_task_spec(request, base_dir=root, project_root=root)
            mocked_consultant_output = [
                {
                    "constraint_id": "format",
                    "semantic_key": "required_behavior.annotation_format",
                    "operator": "eq",
                    "value": "gtf22",
                    "strength": "hard",
                    "confidence": 0.99,
                    "source_text": "with -gtf22",
                },
                {
                    "constraint_id": "database",
                    "semantic_key": "required_behavior.database_source",
                    "operator": "eq",
                    "value": "custom_genome",
                    "strength": "hard",
                    "confidence": 0.99,
                    "source_text": "custom genome",
                },
                {
                    "constraint_id": "no-guess",
                    "semantic_key": "forbidden_behavior.species_database_guess",
                    "operator": "forbid",
                    "value": True,
                    "strength": "hard",
                    "confidence": 0.99,
                    "source_text": "do not guess a species database",
                },
            ]
            validated = validate_compiled_constraints(mocked_consultant_output)
            self.assertTrue(validated["valid"], validated["errors"])
            spec = merge_compiled_constraints(
                spec,
                validated["constraints"],
                base_dir=root,
                project_root=root,
                metrics=validated["metrics"],
            )

        inputs = {item["role"]: item for item in spec["inputs"]}
        requirements = {item["requirement_id"]: item for item in spec["requirements"]}
        outputs = {item["role"]: item for item in spec["output_contracts"]}
        self.assertEqual(set(inputs), {"variant_vcf", "reference_fasta", "annotation_gtf"})
        self.assertTrue(all(item["source"] == "user_explicit" for item in inputs.values()))
        self.assertEqual(requirements["format"]["value"], "gtf22")
        self.assertEqual(requirements["database"]["value"], "custom_genome")
        self.assertTrue(requirements["no-guess"]["value"])
        self.assertTrue(outputs["output_dir"]["canonical_path"].endswith("results/strict"))
        self.assertIn("annotated_vcf", outputs)

    def test_task_spec_round_trip_preserves_structured_bindings(self):
        spec = create_task_spec(
            "Use /data/input.vcf and output report.md",
            base_dir="/project",
        )

        restored = ensure_task_spec(json.loads(json.dumps(spec)))

        self.assertEqual(restored["inputs"], spec["inputs"])
        self.assertEqual(restored["output_contracts"], spec["output_contracts"])
        self.assertEqual(restored["requirements"], spec["requirements"])

    def test_inferred_input_cannot_override_user_explicit_binding(self):
        spec = merge_task_spec(
            create_task_spec("annotate"),
            {"inputs": [{"role": "variant_vcf", "path": "/user/input.vcf", "source": "user_explicit"}]},
            source="user_explicit",
            reason="explicit_input",
        )

        updated = merge_task_spec(
            spec,
            {"inputs": [{"role": "variant_vcf", "path": "/inferred/other.vcf", "source": "planner_derived"}]},
            source="planner_derived",
            reason="planner_inference",
        )

        preferred = [item for item in updated["inputs"] if item["source"] == "user_explicit"]
        self.assertEqual(preferred[0]["canonical_path"], "/user/input.vcf")

    def test_multiple_explicit_bindings_are_preserved_for_conflict_resolution(self):
        spec = create_task_spec("Use /data/a.vcf and /data/b.vcf")

        self.assertEqual(
            [item["canonical_path"] for item in spec["inputs"]],
            ["/data/a.vcf", "/data/b.vcf"],
        )

    def test_formal_input_audit_writes_verification_into_task_spec(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "input.vcf"
            path.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
            spec = create_task_spec(f"Use {path}")
            updated = merge_task_spec_input_audit(
                spec,
                {"resolved_inputs": [{
                    "role": "variant_file",
                    "path": str(path),
                    "source": "explicit",
                    "verified": True,
                    "verification_evidence": ["exists", "non_empty", "readable"],
                }]},
            )

        binding = updated["inputs"][0]
        self.assertEqual(binding["role"], "variant_vcf")
        self.assertTrue(binding["verified"])
        self.assertIn("non_empty", binding["verification_evidence"])

    def test_repeated_formal_input_audit_is_idempotent(self):
        audit = {"checked_at": "2026-07-30T12:00:00", "resolved_inputs": [{
            "role": "variant_file",
            "path": "/data/input.vcf",
            "source": "explicit",
            "verified": True,
        }]}
        once = merge_task_spec_input_audit(create_task_spec("annotate"), audit)
        twice = merge_task_spec_input_audit(once, audit)

        self.assertEqual(twice["inputs"], once["inputs"])
        self.assertEqual(twice["requirements"], once["requirements"])
        self.assertNotIn("change_log", twice)

    def test_generic_renderer_projection_uses_roles_and_reports_missing(self):
        spec = merge_task_spec(
            create_task_spec("custom annotation"),
            {
                "inputs": [
                    {"role": "variant_vcf", "path": "/data/input.vcf", "source": "user_explicit"},
                    {"role": "reference_fasta", "path": "/data/genome.fa", "source": "user_explicit"},
                    {"role": "annotation_gtf", "path": "/data/genome.gtf", "source": "user_explicit"},
                ],
                "requirements": [{"requirement_id": "database_source", "role": "database_source", "value": "custom_genome", "source": "user_explicit"}],
                "expected_outputs": [{"role": "output_dir", "path": "/out/strict", "source": "user_explicit"}],
            },
            source="user_explicit",
            reason="request_extraction",
        )
        schema = {
            "arguments": {
                "vcf": {"flag": "--vcf", "required": True, "aliases": ["variant_file"]},
                "reference_fasta": {"flag": "--reference-fasta", "required": True},
                "annotation_gtf": {"flag": "--annotation-gtf"},
                "genome_version": {"flag": "--genome-version", "required": True},
                "outdir": {"flag": "--outdir", "top_level": True},
            },
            "binding_rules": {
                "genome_version": {
                    "sources": [{
                        "kind": "stable_task_identifier",
                        "role": "database_source",
                        "prefix": "biomind",
                        "rule_id": "test.custom_database.task_id.v1",
                        "when": {
                            "role": "database_source",
                            "operator": "eq",
                            "value": "custom_genome",
                        },
                    }],
                },
            },
        }

        projected = project_task_spec_to_renderer(spec, schema, task_id="task-a")
        missing = project_task_spec_to_renderer(
            {**spec, "inputs": [item for item in spec["inputs"] if item["role"] != "reference_fasta"]},
            schema,
            task_id="task-a",
        )

        self.assertEqual(projected["arguments"]["vcf"], "/data/input.vcf")
        self.assertEqual(projected["arguments"]["annotation_gtf"], "/data/genome.gtf")
        self.assertEqual(projected["outdir"], "/out/strict")
        self.assertEqual(projected["missing_bindings"], [])
        self.assertIn("reference_fasta", missing["missing_bindings"])

    def test_legacy_task_spec_migrates_from_goal_without_overwriting_values(self):
        legacy = {
            "schema_version": 2,
            "revision": 1,
            "goal": "Use /data/input.vcf with -gtf22 for a custom genome; output report.md",
            "inputs": [],
            "requirements": [],
        }

        migrated = ensure_task_spec(legacy)

        self.assertEqual(migrated["schema_version"], TASK_SPEC_SCHEMA_VERSION)
        self.assertTrue(migrated["inputs"])
        self.assertTrue(semantic_constraints_from_task_spec(migrated))
        self.assertNotIn("migrations_applied", migrated)
        self.assertNotIn("expected_outputs", migrated)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from agent_core.task_spec import (
    TASK_SPEC_SCHEMA_VERSION,
    apply_task_spec_mutation_proposal,
    apply_user_feedback,
    classify_user_feedback_change,
    create_task_spec,
    constraint_mutation_proposal,
    ensure_task_spec,
    merge_task_spec_input_audit,
    merge_compiled_constraints,
    merge_task_spec,
    project_task_spec_to_renderer,
    render_task_spec,
    evaluate_requirements,
    evaluate_failure_policies,
    semantic_install_policy,
    task_spec_constraints,
    task_record_hash,
    task_spec_revision,
    task_semantic_hash,
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
                "semantic_key": "required_input.variant_vcf",
                "path": str(path),
                "value": str(path),
                "level": "required",
                "present": True,
            }
            self.assertFalse(evaluate_requirements([requirement])["blocked"])
            path.unlink()
            result = evaluate_requirements([requirement])

        self.assertTrue(result["blocked"])
        self.assertEqual(result["missing_required"], ["input_vcf"])

    def test_user_supplied_inputs_remain_structured_bindings(self):
        spec = merge_task_spec(
            create_task_spec("annotate variants"),
            {"inputs": [{"path": "/data/input.vcf", "role": "variants"}]},
            source="user_feedback",
            reason="explicit_input",
        )

        constraints = semantic_constraints_from_task_spec(spec)
        self.assertEqual(constraints, [])
        self.assertEqual(spec["inputs"][0]["role"], "variant_vcf")
        self.assertEqual(spec["inputs"][0]["canonical_path"], "/data/input.vcf")
        self.assertIn("requirements", json.loads(render_task_spec(spec)))

    def test_user_feedback_artifact_update_does_not_duplicate_requirement(self):
        spec = create_task_spec("annotate variants", task_id="task-artifact-update")

        updated = apply_user_feedback(
            spec,
            "Use /data/input.vcf as the variant input.",
            kind="consultant_answer",
            source_message_id="U2",
            message_origin="user",
        )

        self.assertEqual(updated["inputs"][0]["canonical_path"], "/data/input.vcf")
        self.assertEqual(updated["requirements"], [])
        self.assertEqual(semantic_constraints_from_task_spec(updated), [])

    def test_open_world_requirements_preserve_text_without_synthesized_ontology(self):
        texts = [
            "不得使用 liftOver 后的 VCF 作为分析输入。",
            "Do not rename contigs before annotation.",
            "禁止使用 demo/toy/test/hidden/expected-answer 文件作为分析输入。",
        ]
        validated = validate_compiled_constraints([
            {"value": text, "strength": "required", "source_text": text}
            for text in texts
        ])
        self.assertTrue(validated["valid"], validated["errors"])

        spec = merge_compiled_constraints(
            create_task_spec("annotate variants"),
            validated["constraints"],
        )

        self.assertEqual([item["value"] for item in spec["requirements"]], texts)
        self.assertEqual(
            [item["requirement_id"] for item in spec["requirements"]],
            ["requirement-1", "requirement-2", "requirement-3"],
        )
        forbidden = {
            "semantic_key", "category", "operator", "enforcement_scope",
            "constraint_id", "semantic_claims", "handles",
        }
        self.assertTrue(all(not forbidden.intersection(item) for item in spec["requirements"]))
        self.assertEqual(semantic_constraints_from_task_spec(spec), [])

    def test_sequential_open_requirement_batches_append_and_explicit_update_stays_in_place(self):
        spec = create_task_spec("annotate variants")
        first = validate_compiled_constraints([{
            "value": "Keep all ANN fields.",
            "strength": "required",
            "source_text": "Keep all ANN fields.",
        }])
        spec = merge_compiled_constraints(spec, first["constraints"])
        second = validate_compiled_constraints([{
            "value": "Do not rename contigs.",
            "strength": "hard",
            "source_text": "Do not rename contigs.",
        }])
        spec = merge_compiled_constraints(spec, second["constraints"])

        self.assertEqual(
            [item["requirement_id"] for item in spec["requirements"]],
            ["requirement-1", "requirement-2"],
        )
        self.assertEqual(
            [item["value"] for item in spec["requirements"]],
            ["Keep all ANN fields.", "Do not rename contigs."],
        )

        replayed = merge_compiled_constraints(spec, second["constraints"])
        self.assertEqual(replayed["revision"], spec["revision"])
        self.assertEqual(replayed["requirements"], spec["requirements"])

        update = validate_compiled_constraints([{
            "requirement_id": "requirement-1",
            "value": "Keep every ANN field.",
            "strength": "required",
            "source_text": "Keep every ANN field.",
        }])
        spec = merge_compiled_constraints(spec, update["constraints"])
        self.assertEqual(
            [item["value"] for item in spec["requirements"]],
            ["Keep every ANN field.", "Do not rename contigs."],
        )

    def test_open_requirement_rejects_invalid_attention_shape(self):
        invalid = validate_compiled_constraints([{
            "value": "Keep all ANN fields.",
            "strength": "absolute",
            "level": "mandatory_forever",
            "source_text": "Keep all ANN fields.",
        }])

        self.assertFalse(invalid["valid"])
        self.assertEqual(
            {item["field"] for item in invalid["errors"]},
            {"strength", "level"},
        )

    def test_fresh_typed_looking_generic_mutation_cannot_bypass_open_text_admission(self):
        spec = create_task_spec("annotate variants", task_id="task-open-admission")
        proposal = constraint_mutation_proposal([{
            "constraint_id": "invented-demo-policy",
            "semantic_key": "forbidden_behavior.demo_hidden_answer",
            "category": "forbidden_behavior",
            "operator": "forbid",
            "enforcement_scope": "plan",
            "value": "禁止使用 demo/toy/test/hidden answer 文件作为分析输入。",
            "strength": "hard",
            "confidence": 1.0,
            "source_ref": "U1",
        }])

        updated = apply_task_spec_mutation_proposal(spec, proposal)

        requirement = updated["requirements"][0]
        self.assertEqual(
            requirement["value"],
            "禁止使用 demo/toy/test/hidden answer 文件作为分析输入。",
        )
        self.assertEqual(requirement["requirement_id"], "requirement-1")
        self.assertFalse({
            "semantic_key", "category", "operator", "enforcement_scope",
            "constraint_id",
        }.intersection(requirement))
        self.assertEqual(semantic_constraints_from_task_spec(updated), [])

    def test_typed_runtime_and_failure_policies_remain_deterministic(self):
        rows = [
            {
                "constraint_id": "no-install",
                "semantic_key": "forbidden_behavior.runtime_install",
                "operator": "forbid",
                "value": True,
                "strength": "hard",
                "confidence": 1.0,
                "source_text": "do not install runtimes",
            },
            {
                "constraint_id": "missing-java",
                "semantic_key": "failure_policy.java_missing",
                "operator": "on_condition",
                "value": "block",
                "condition": "java_missing",
                "strength": "hard",
                "confidence": 1.0,
                "source_text": "block if Java is missing",
            },
        ]
        validated = validate_compiled_constraints(rows)
        self.assertTrue(validated["valid"], validated["errors"])
        spec = merge_compiled_constraints(create_task_spec("annotate"), validated["constraints"])

        self.assertFalse(semantic_install_policy(spec, dependency_key="java")["install_allowed"])
        self.assertTrue(evaluate_failure_policies(spec, {"java_missing": True})["blocked"])
        self.assertFalse(evaluate_failure_policies(spec, {"java_missing": False})["blocked"])

    def test_requirement_text_changes_semantic_hash_but_provenance_does_not(self):
        validated = validate_compiled_constraints([{
            "value": "Do not rename contigs before annotation.",
            "strength": "required",
            "source_text": "Do not rename contigs before annotation.",
        }])
        spec = merge_compiled_constraints(create_task_spec("annotate"), validated["constraints"])
        semantic_before = task_semantic_hash(spec)
        record_before = task_record_hash(spec)

        provenance_only = json.loads(json.dumps(spec))
        provenance_only["requirements"][0]["provenance"].append({
            "source_type": "user_explicit",
            "source_id": "msg-2",
        })
        self.assertEqual(task_semantic_hash(provenance_only), semantic_before)
        self.assertNotEqual(task_record_hash(provenance_only), record_before)

        changed = json.loads(json.dumps(spec))
        changed["requirements"][0]["value"] = "Rename contigs before annotation."
        self.assertNotEqual(task_semantic_hash(changed), semantic_before)

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
                "parameters": {"database_source": "custom_genome"},
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
            "requirements": [{
                "requirement_id": "legacy-format",
                "constraint_id": "legacy-format",
                "semantic_key": "required_behavior.annotation_format",
                "operator": "eq",
                "value": "gtf22",
                "strength": "hard",
                "confidence": 1.0,
                "source_text": "with -gtf22",
            }],
        }

        migrated = ensure_task_spec(legacy)

        self.assertEqual(migrated["schema_version"], TASK_SPEC_SCHEMA_VERSION)
        self.assertTrue(migrated["inputs"])
        self.assertEqual(
            semantic_constraints_from_task_spec(migrated)[0]["semantic_key"],
            "required_behavior.annotation_format",
        )
        self.assertNotIn("migrations_applied", migrated)
        self.assertNotIn("expected_outputs", migrated)


if __name__ == "__main__":
    unittest.main()

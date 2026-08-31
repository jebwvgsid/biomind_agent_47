import copy
import json
import unittest

from agent_core import plan_tracker as plan_tracker_module
from agent_core.plan_tracker import (
    PlanStructureError,
    approve_plan_state,
    bind_renderer_invocation_contracts,
    bind_plan_to_task_revision,
    build_plan_state,
    derive_plan_validation_state,
    migrate_plan_state,
    normalize_artifact_contract,
    normalize_plan_contracts,
    normalize_validation_issue,
    parse_plan_steps,
    plan_contract_violations,
    resolve_plan_semantic_claims,
    validate_plan_for_generate,
    validation_issue,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import (
    apply_authorized_task_spec_constraint_repair,
    apply_declarative_task_spec_rules,
    create_task_spec,
    format_planner_task_contract_context,
    merge_compiled_constraints,
    normalize_requirement,
    project_task_spec_to_renderer,
    semantic_provenance_catalog,
    semantic_constraints_from_task_spec,
    validate_compiled_constraints,
)


def structured_plan(*, input_value="/tmp/input.vcf"):
    return json.dumps({
        "steps": [{
            "step_id": "step-1",
            "title": "Run annotation",
            "action": "execute",
            "purpose": "analysis",
            "tool": "renderer",
            "inputs": [input_value],
            "outputs": ["annotated.vcf"],
            "dependencies": [],
            "success_criteria": ["annotated VCF exists and is non-empty"],
            "skill_ids": ["genomics/snpeff-annotation"],
            "workflow_stage_ids": [],
            "batch_id": "",
            "risk_level": "medium",
        }],
        "validation_checklist": [{"check_id": "check-1", "description": "validate outputs"}],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"},
    })


class StructuredPlanContractTests(unittest.TestCase):
    def test_01_exact_json_is_accepted(self):
        self.assertEqual(len(build_plan_state("t", structured_plan(), strict=True)["steps"]), 1)

    def test_02_leading_prose_is_rejected(self):
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", "Here is the plan\n" + structured_plan(), strict=True)

    def test_03_h1_markdown_is_rejected(self):
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", "# Plan\n## Execute", strict=True)

    def test_04_empty_steps_are_rejected(self):
        payload = json.loads(structured_plan())
        payload["steps"] = []
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_05_missing_metadata_is_rejected(self):
        payload = json.loads(structured_plan())
        payload.pop("metadata")
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_06_missing_step_field_is_rejected(self):
        payload = json.loads(structured_plan())
        payload["steps"][0].pop("success_criteria")
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_07_inline_claims_are_rejected(self):
        payload = json.loads(structured_plan())
        payload["steps"][0]["semantic_claims"] = []
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_08_single_json_fence_is_accepted(self):
        state = build_plan_state("t", "```json\n" + structured_plan() + "\n```", strict=True)
        self.assertEqual(state["plan_structure_status"], "valid")

    def test_09_mixed_fence_is_rejected(self):
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", "```json\n{}\n``` trailing", strict=True)

    def test_10_legacy_parser_remains_display_compatibility_only(self):
        self.assertEqual(len(parse_plan_steps("## Run\n## Validate")), 2)

    def test_10a_unexpected_top_level_field_is_rejected(self):
        payload = json.loads(structured_plan())
        payload["next_step"] = "generate"
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_10b_explicit_empty_skill_ids_remain_declared(self):
        payload = json.loads(structured_plan())
        payload["steps"][0]["skill_ids"] = []

        state = build_plan_state("t", json.dumps(payload), strict=True)

        self.assertTrue(state["steps"][0]["skills_declared"])

    def test_10b_every_prompt_declared_step_field_is_required(self):
        payload = json.loads(structured_plan())
        payload["steps"][0].pop("tool")
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_10c_step_array_types_are_enforced(self):
        payload = json.loads(structured_plan())
        payload["steps"][0]["inputs"] = "/tmp/input.vcf"
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_10d_metadata_contract_version_is_enforced(self):
        payload = json.loads(structured_plan())
        payload["metadata"]["schema_version"] = 3
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)


class TaskSpecRoleAndProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = SkillManager("skills")
        cls.renderer = cls.manager.get_renderer_record("genomics/snpeff-annotation")

    def custom_spec(self):
        spec = create_task_spec(
            "Use /tmp/input.vcf, /tmp/reference.fasta, and /tmp/annotation.gtf with -gtf22. "
            "Output directory: /tmp/out",
            task_id="task-custom",
            project_root="/tmp",
        )
        return apply_declarative_task_spec_rules(spec, [self.renderer])

    def test_11_semantic_key_owns_role(self):
        row = normalize_requirement({
            "requirement_id": "gtf22",
            "role": "gtf22",
            "semantic_key": "required_behavior.annotation_format",
            "value": "gtf22",
        })
        self.assertEqual(row["role"], "annotation_format")

    def test_12_requirement_identity_is_separate_from_role(self):
        row = normalize_requirement({
            "requirement_id": "row-7",
            "semantic_key": "required_behavior.annotation_format",
            "value": "gtf22",
        })
        self.assertEqual(row["requirement_id"], "row-7")
        self.assertEqual(row["role"], "annotation_format")

    def test_13_skill_rules_do_not_create_generic_semantic_rows(self):
        first = self.custom_spec()
        second = self.custom_spec()
        self.assertEqual(first["parameters"], second["parameters"])
        self.assertEqual(semantic_constraints_from_task_spec(first), [])

    def test_14_skill_rule_materializes_custom_database_parameter(self):
        spec = self.custom_spec()
        self.assertEqual(spec["parameters"]["database_source"], "custom_genome")
        self.assertFalse(spec["requirements"])

    def test_15_skill_rule_requires_typed_annotation_input(self):
        spec = create_task_spec("Use /tmp/input.vcf and /tmp/reference.fasta", task_id="t", project_root="/tmp")
        resolved = apply_declarative_task_spec_rules(spec, [self.renderer])
        self.assertNotIn("database_source", resolved["parameters"])

    def test_16_skill_rule_is_idempotent(self):
        once = self.custom_spec()
        twice = apply_declarative_task_spec_rules(once, [self.renderer])
        self.assertEqual(twice["parameters"], once["parameters"])
        self.assertFalse(twice["requirements"])

    def test_17_custom_projection_is_complete(self):
        result = project_task_spec_to_renderer(self.custom_spec(), self.renderer["invocation"], task_id="task-custom")
        self.assertEqual(result["status"], "COMPLETE")

    def test_18_custom_identifier_is_stable(self):
        first = project_task_spec_to_renderer(self.custom_spec(), self.renderer["invocation"], task_id="task-custom")
        second = project_task_spec_to_renderer(self.custom_spec(), self.renderer["invocation"], task_id="task-custom")
        self.assertEqual(first["arguments"]["genome_version"], second["arguments"]["genome_version"])

    def test_19_derived_identifier_has_controller_provenance(self):
        result = project_task_spec_to_renderer(self.custom_spec(), self.renderer["invocation"], task_id="task-custom")
        self.assertEqual(
            result["binding_sources"]["genome_version"]["source"],
            "controller_derived_from_skill_contract",
        )

    def test_20_missing_required_input_is_incomplete(self):
        result = project_task_spec_to_renderer(create_task_spec("output directory /tmp/out"), self.renderer["invocation"], task_id="t")
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("vcf", result["missing_bindings"])

    def test_21_missing_input_has_external_owner(self):
        result = project_task_spec_to_renderer(create_task_spec("output directory /tmp/out"), self.renderer["invocation"], task_id="t")
        details = {x["argument"]: x for x in result["missing_binding_details"]}
        self.assertEqual(details["vcf"]["owner"], "external_input")

    def test_22_empty_renderer_is_not_applicable(self):
        self.assertEqual(project_task_spec_to_renderer({}, {}, task_id="t")["status"], "NOT_APPLICABLE")

    def test_23_undeclared_derivation_does_not_run(self):
        schema = copy.deepcopy(self.renderer["invocation"])
        schema.pop("binding_rules")
        result = project_task_spec_to_renderer(self.custom_spec(), schema, task_id="task-custom")
        self.assertIn("genome_version", result["missing_bindings"])

    def test_24_structured_parameter_cross_skill_binding_rule(self):
        spec = create_task_spec("generic task")
        spec["parameters"] = {"cohort": "A"}
        schema = {
            "arguments": {"workspace_key": {"required": True}},
            "binding_rules": {
                "workspace_key": {"sources": [{"kind": "requirement", "role": "cohort", "rule_id": "generic.v1"}]}
            },
        }
        result = project_task_spec_to_renderer(spec, schema, task_id="generic")
        self.assertEqual(result["arguments"]["workspace_key"], "A")

    @staticmethod
    def _authorized_repair_payload(spec, *, semantic_key="required_behavior.mode", value="strict"):
        catalog = semantic_provenance_catalog(spec)
        source = next(item for item in catalog if item.get("source_id"))
        return catalog, {
            "base_task_spec_revision": spec["revision"],
            "constraints": [{
                "constraint_id": "repair:mode",
                "semantic_key": semantic_key,
                "operator": "eq",
                "value": value,
                "strength": "hard",
                "confidence": 1.0,
                "provenance": [{
                    "source_id": source["source_id"],
                    "source_type": source["source_type"],
                }],
            }],
            "needs_user_input": False,
            "question": "",
        }

    def test_24a_authorized_taskspec_repair_changes_only_existing_legacy_slot(self):
        initial = validate_compiled_constraints([{
            "constraint_id": "legacy:mode",
            "semantic_key": "required_behavior.mode",
            "operator": "eq",
            "value": "permissive",
            "strength": "hard",
            "confidence": 1.0,
            "source_text": "legacy mode",
        }])
        spec = merge_compiled_constraints(
            create_task_spec("Use strict mode", task_id="repair"),
            initial["constraints"],
        )
        catalog, payload = self._authorized_repair_payload(spec)
        repaired = apply_authorized_task_spec_constraint_repair(
            spec,
            payload,
            allowed_semantic_slots=["mode"],
            provenance_catalog=catalog,
        )
        mode = next(item for item in repaired["requirements"] if item["semantic_key"] == "required_behavior.mode")
        self.assertEqual(mode["value"], "strict")
        self.assertEqual(repaired["revision"], spec["revision"] + 1)

    def test_24b_taskspec_repair_rejects_forbidden_slot(self):
        spec = create_task_spec("Use strict mode", task_id="repair")
        catalog, payload = self._authorized_repair_payload(
            spec,
            semantic_key="required_behavior.reference_identity",
            value="invented",
        )
        with self.assertRaisesRegex(ValueError, "MUTATION_FORBIDDEN"):
            apply_authorized_task_spec_constraint_repair(
                spec,
                payload,
                allowed_semantic_slots=["mode"],
                provenance_catalog=catalog,
            )

    def test_24c_taskspec_repair_rejects_stale_revision(self):
        spec = create_task_spec("Use strict mode", task_id="repair")
        catalog, payload = self._authorized_repair_payload(spec)
        payload["base_task_spec_revision"] = 0
        with self.assertRaisesRegex(ValueError, "STALE_REVISION"):
            apply_authorized_task_spec_constraint_repair(
                spec,
                payload,
                allowed_semantic_slots=["mode"],
                provenance_catalog=catalog,
            )

    def test_24d_taskspec_repair_no_op_is_rejected(self):
        initial = validate_compiled_constraints([{
            "constraint_id": "legacy:mode",
            "semantic_key": "required_behavior.mode",
            "operator": "eq",
            "value": "permissive",
            "strength": "hard",
            "confidence": 1.0,
            "source_text": "legacy mode",
        }])
        spec = merge_compiled_constraints(
            create_task_spec("Use strict mode", task_id="repair"),
            initial["constraints"],
        )
        catalog, payload = self._authorized_repair_payload(spec)
        repaired = apply_authorized_task_spec_constraint_repair(
            spec,
            payload,
            allowed_semantic_slots=["mode"],
            provenance_catalog=catalog,
        )
        next_catalog, next_payload = self._authorized_repair_payload(repaired)
        with self.assertRaisesRegex(ValueError, "NO_OP"):
            apply_authorized_task_spec_constraint_repair(
                repaired,
                next_payload,
                allowed_semantic_slots=["mode"],
                provenance_catalog=next_catalog,
            )

    def test_24e_taskspec_repair_cannot_create_fresh_generic_ontology(self):
        spec = create_task_spec("Use strict mode", task_id="repair")
        catalog, payload = self._authorized_repair_payload(spec)
        with self.assertRaisesRegex(ValueError, "GENERIC_ONTOLOGY_RETIRED"):
            apply_authorized_task_spec_constraint_repair(
                spec,
                payload,
                allowed_semantic_slots=["mode"],
                provenance_catalog=catalog,
            )


class ValidationOwnershipAndGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manager = SkillManager("skills")
        cls.renderer = manager.get_renderer_record("genomics/snpeff-annotation")

    def test_25_issue_has_all_routing_fields(self):
        issue = validation_issue("x", owner="planner", repair_target="plan", field_path="plan.x", message="bad")
        for key in ("code", "severity", "owner", "repair_target", "field_path", "message", "fingerprint"):
            self.assertIn(key, issue)

    def test_26_issue_fingerprint_is_stable(self):
        a = validation_issue("x", owner="planner", repair_target="plan", field_path="plan.x", message="bad")
        b = validation_issue("x", owner="planner", repair_target="plan", field_path="plan.x", message="bad")
        self.assertEqual(a["fingerprint"], b["fingerprint"])

    def test_27_legacy_plan_field_maps_to_planner(self):
        issue = normalize_validation_issue({"step_id": "step-1", "field": "action", "reason": "missing"})
        self.assertEqual(issue["owner"], "planner")

    def test_28_invalid_semantic_claim_is_explicit(self):
        state = build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok")
        state["steps"][0]["semantic_claims"] = [{"semantic_key": "bad"}]
        state["steps"][0].pop("semantic_claims_validation", None)
        self.assertIn("SEMANTIC_CLAIMS_INVALID", {x.get("violation_code") for x in plan_contract_violations(state)})

    def test_29_fresh_plan_rejects_constraint_reference_fields(self):
        payload = json.loads(structured_plan())
        payload["steps"][0]["semantic_constraint_ids"] = ["missing"]
        with self.assertRaises(PlanStructureError):
            build_plan_state("t", json.dumps(payload), strict=True)

    def test_30_legacy_constraint_reference_resolves_once(self):
        compiled = validate_compiled_constraints([{
            "constraint_id": "legacy-format",
            "semantic_key": "required_behavior.annotation_format",
            "operator": "eq",
            "value": "gtf22",
            "strength": "hard",
            "confidence": 1.0,
            "source_ref": "legacy",
        }])
        spec = merge_compiled_constraints(create_task_spec("annotate", task_id="t"), compiled["constraints"])
        constraint = semantic_constraints_from_task_spec(spec)[0]
        state = build_plan_state("t", structured_plan(), strict=True)
        state["compatibility"] = {"source_schema_version": 9}
        state["steps"][0]["semantic_constraint_ids"] = [constraint["constraint_id"]]
        resolved, issues = resolve_plan_semantic_claims(state, spec)
        self.assertFalse(issues)
        self.assertEqual(resolved["steps"][0]["semantic_claims"][0]["constraint_id"], constraint["constraint_id"])

    def test_30a_anonymous_hard_directive_needs_no_plan_binding(self):
        compiled = validate_compiled_constraints([{
            "value": "Do not invent annotation records",
            "strength": "required",
            "source_ref": "test",
        }])
        spec = merge_compiled_constraints(
            create_task_spec("Annotate variants", task_id="t"),
            compiled["constraints"],
        )
        state = build_plan_state("t", structured_plan(), strict=True)
        state["task_spec"] = spec

        violations = plan_contract_violations(state)

        self.assertNotIn(
            "SEMANTIC_CONSTRAINT_UNCOVERED",
            {item.get("violation_code") for item in violations},
        )

    def test_30b_planner_context_projects_open_text_without_claim_id(self):
        compiled = validate_compiled_constraints([{
            "value": "Record tool failures truthfully",
            "strength": "required",
            "source_ref": "test",
        }])
        spec = merge_compiled_constraints(
            create_task_spec("Annotate variants", task_id="t"),
            compiled["constraints"],
        )

        context = format_planner_task_contract_context(spec)
        contract = json.loads(
            context.split("\n", 1)[1].split("\n[CANONICAL REQUIRED OUTPUTS]", 1)[0]
        )

        self.assertEqual(
            contract["highlighted_constraints"][0]["text"],
            "Record tool failures truthfully",
        )
        self.assertEqual(
            set(contract["highlighted_constraints"][0]),
            {"text", "importance", "source_refs"},
        )
        self.assertNotIn("semantic_constraints", contract)
        self.assertNotIn("global_policies", contract)

    def test_31_incomplete_projection_is_persisted(self):
        state = build_plan_state("t", structured_plan(), strict=True)
        state.setdefault("task_state", {})["execution_contract"] = "legacy_renderer_v1"
        normalized, _, issues = normalize_plan_contracts(
            state,
            renderer_records=[self.renderer],
            task_spec=create_task_spec("output directory /tmp/out"),
        )
        self.assertEqual(normalized["renderer_projections"][0]["status"], "INCOMPLETE")
        self.assertTrue(issues)

    def test_32_incomplete_projection_has_no_invocation(self):
        state = build_plan_state("t", structured_plan(), strict=True)
        state.setdefault("task_state", {})["execution_contract"] = "legacy_renderer_v1"
        normalized, _, _ = normalize_plan_contracts(
            state,
            renderer_records=[self.renderer],
            task_spec=create_task_spec("output directory /tmp/out"),
        )
        self.assertNotIn("renderer_invocation_contract", normalized["steps"][0])

    def test_33_blocking_issue_closes_approval_request(self):
        state = derive_plan_validation_state(
            build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok"),
            [validation_issue("X", owner="planner", repair_target="plan", field_path="plan", message="bad")],
        )
        self.assertFalse(state["can_request_approval"])

    def test_34_approval_rejects_blocking_issue(self):
        state = derive_plan_validation_state(
            build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok"),
            [validation_issue("X", owner="planner", repair_target="plan", field_path="plan", message="bad")],
        )
        bind_plan_to_task_revision(state, 1)
        with self.assertRaises(ValueError):
            approve_plan_state(state, 1)

    def test_35_approval_opens_generate_gate(self):
        state = derive_plan_validation_state(
            build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok"),
            [],
        )
        bind_plan_to_task_revision(state, 1)
        approve_plan_state(state, 1)
        self.assertTrue(validate_plan_for_generate(state, 1)["valid"])

    def test_36_generate_reports_issue_owner(self):
        state = derive_plan_validation_state(
            build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok"),
            [validation_issue("X", owner="task_spec", repair_target="task_spec.x", field_path="x", message="bad")],
        )
        bind_plan_to_task_revision(state, 1)
        result = validate_plan_for_generate(state, 1)
        self.assertEqual(result["repair_owner"], "task_spec")

    def test_37_migration_invalidates_legacy_approval(self):
        legacy = {
            "task_id": "t",
            "migration_version": 6,
            "plan_status": "approved",
            "steps": [{"step_id": "step-1", "title": "Run | Action: EXECUTE | Skills: [] | Done: ok"}],
        }
        migrated, _ = migrate_plan_state(legacy, "t")
        self.assertEqual(migrated["plan_status"], "pending_review")
        self.assertTrue(migrated["validation_recompute_required"])

    def test_38_artifact_extension_does_not_define_role(self):
        artifact = normalize_artifact_contract("annotated.vcf")
        self.assertEqual(artifact["role"], "")

    def test_39_legacy_role_labels_do_not_define_step_usage(self):
        state = build_plan_state(
            "t",
            "- [ ] Step 1: Validate annotated.vcf | Action: EXECUTE | Inputs: [annotated.vcf] | Skills: [] | Done: ok",
        )
        state["current_task"] = {
            "task_spec": {
                "output_contracts": [{"role": "annotated_vcf", "filename": "annotated.vcf", "required": True}]
            }
        }
        self.assertNotIn(
            "ARTIFACT_ROLE_CONFLICT",
            {x.get("violation_code") for x in plan_contract_violations(state)},
        )


class RepairAndConvergenceTests(unittest.TestCase):
    def state(self):
        state = build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok")
        return derive_plan_validation_state(
            state,
            [validation_issue("PLAN_X", owner="planner", repair_target="plan.step-1", field_path="plan.step-1", message="bad")],
        )

    def test_40_to_43_semantic_plan_patch_surface_is_retired(self):
        for name in (
            "PLAN_PATCH_OPERATION",
            "parse_plan_patch",
            "apply_plan_patch",
            "build_plan_repair_request",
        ):
            self.assertFalse(hasattr(plan_tracker_module, name), name)

    def test_44_a_to_a_is_non_convergent(self):
        state = self.state()
        repeated = derive_plan_validation_state(state, state["validation_issues"])
        self.assertEqual(repeated["repair_convergence"]["cycle_type"], "A_TO_A")

    def test_44a_rollback_bookkeeping_does_not_create_a_to_a(self):
        state = self.state()

        restored = derive_plan_validation_state(
            state,
            state["validation_issues"],
            record_convergence=False,
        )

        self.assertEqual(restored["repair_convergence"]["status"], "converging")
        self.assertEqual(restored["repair_convergence"]["cycle_type"], "")
        self.assertEqual(
            restored["validation_convergence_history"],
            state["validation_convergence_history"],
        )

    def test_45_a_to_b_to_a_is_detected(self):
        state = self.state()
        issue_b = validation_issue("PLAN_Y", owner="planner", repair_target="plan.step-1", field_path="plan.step-1", message="other")
        state_b = derive_plan_validation_state(state, [issue_b])
        state_a = derive_plan_validation_state(state_b, state["validation_issues"])
        self.assertEqual(state_a["repair_convergence"]["cycle_type"], "A_TO_B_TO_A")

    def test_46_hard_repair_ceiling_blocks(self):
        state = build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok")
        for index in range(4):
            issue = validation_issue(f"PLAN_{index}", owner="planner", repair_target="plan", field_path="plan", message=str(index))
            state = derive_plan_validation_state(state, [issue], max_repair_attempts=3)
        self.assertEqual(state["repair_convergence"]["cycle_type"], "REPAIR_CEILING")

    def test_47_selected_non_applicable_renderer_is_internal_block(self):
        state = build_plan_state("t", structured_plan(), strict=True)
        state["steps"][0].update({
            "step_type": "renderer_composite",
            "selected_skill_id": "genomics/snpeff-annotation",
            "renderer_id": "mock.empty",
        })
        bound = bind_renderer_invocation_contracts(
            state,
            [{
                "skill_id": "genomics/snpeff-annotation",
                "renderer_id": "mock.empty",
                "invocation": {},
            }],
            create_task_spec("task"),
        )
        issues = bound["renderer_projection_issues"]
        self.assertEqual(bound["renderer_projections"][0]["status"], "NOT_APPLICABLE")
        self.assertEqual(issues[0]["code"], "RENDERER_NOT_APPLICABLE")
        self.assertEqual(issues[0]["owner"], "internal")

    def test_48_non_complete_projection_closes_approval_even_without_cached_issue(self):
        state = build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok")
        state.setdefault("task_state", {})["execution_contract"] = "legacy_renderer_v1"
        state["renderer_projections"] = [{
            "status": "NOT_APPLICABLE",
            "plan_step_id": "step-1",
            "renderer_id": "mock.empty",
        }]
        state = derive_plan_validation_state(state, [])
        bind_plan_to_task_revision(state, 1)
        self.assertFalse(state["can_request_approval"])
        with self.assertRaises(ValueError):
            approve_plan_state(state, 1)

    def test_49_task_spec_change_is_progress_and_valid_state_converges(self):
        issue = validation_issue(
            "TASKSPEC_REQUIRED_BEHAVIOR_MISSING",
            owner="task_spec",
            repair_target="task_spec.requirements[role=mode]",
            field_path="task_spec.requirements",
            message="mode missing",
        )
        state = build_plan_state("t", "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Done: ok")
        state["current_task"] = {"task_spec": create_task_spec("task")}
        state = derive_plan_validation_state(state, [issue])
        repaired_spec = copy.deepcopy(state["current_task"]["task_spec"])
        repaired_spec["requirements"].append(normalize_requirement({
            "semantic_key": "required_behavior.mode",
            "role": "mode",
            "value": "strict",
            "level": "required",
        }))
        state["current_task"]["task_spec"] = repaired_spec
        state = derive_plan_validation_state(state, [issue])
        self.assertEqual(state["repair_convergence"]["status"], "converging")
        state = derive_plan_validation_state(state, [])
        self.assertEqual(state["repair_convergence"]["status"], "valid")
        self.assertTrue(state["can_request_approval"])


class IncidentReplayTests(unittest.TestCase):
    def test_50_session_1786283343_custom_database_projection_converges(self):
        manager = SkillManager("skills")
        renderer = manager.get_renderer_record("genomics/snpeff-annotation")
        spec = create_task_spec(
            "Use /tmp/input.vcf, /tmp/reference.fasta, and /tmp/annotation.gtf with -gtf22. "
            "Output directory: /tmp/out",
            task_id="session_1786283343",
            project_root="/tmp",
        )
        spec = apply_declarative_task_spec_rules(spec, [renderer])
        state = build_plan_state(
            "session_1786283343",
            structured_plan(),
            strict=True,
        )
        state.setdefault("task_state", {})["execution_contract"] = "legacy_renderer_v1"
        normalized, _, issues = normalize_plan_contracts(
            state,
            renderer_records=[renderer],
            output_contracts=[],
            task_spec=spec,
        )
        invocation = normalized["steps"][0]["renderer_invocation_contract"]
        self.assertFalse(issues)
        self.assertEqual(normalized["renderer_projections"][0]["status"], "COMPLETE")
        self.assertEqual(invocation["arguments"]["genome_version"], "biomind_session_1786283343")
        self.assertNotIn("annotated.vcf", normalized["steps"][0]["inputs"])


if __name__ == "__main__":
    unittest.main()

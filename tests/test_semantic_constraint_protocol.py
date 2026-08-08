import json
import unittest
from pathlib import Path

from agent_core.knowledge_conflicts import (
    ClaimRegistry,
    KnowledgeConflictDetector,
    semantic_claim_set_hash,
)
from agent_core.plan_tracker import (
    approval_semantic_delta,
    approve_plan_state,
    bind_renderer_invocation_contracts,
    build_plan_state,
    parse_plan_steps,
)
from agent_core.task_spec import (
    canonical_constraint_hash,
    create_task_spec,
    evaluate_failure_policies,
    merge_compiled_constraints,
    merge_task_spec,
    project_task_spec_to_renderer,
    semantic_install_policy,
    validate_compiled_constraints,
)
from bio_agents import BioAnalystAgent


FIXTURES = Path(__file__).parent / "fixtures"


def compiled_row(
    key,
    value,
    *,
    operator="eq",
    strength="hard",
    confidence=0.95,
    source_text="mocked Consultant extraction",
    **extra,
):
    return {
        "constraint_id": "mock-" + key.replace(".", "-"),
        "semantic_key": key,
        "operator": operator,
        "value": value,
        "strength": strength,
        "confidence": confidence,
        "source_text": source_text,
        **extra,
    }


def register_typed(registry, source_type, key, value, *, operator="eq", strength="required"):
    return registry.register(
        f"{key} {operator} {value}",
        source_type=source_type,
        strength=strength,
        scope="task" if source_type == "task_contract" else "stage",
        scope_id="task" if source_type == "task_contract" else "step-1",
        attributes={
            "semantic_key": key,
            "fact_key": key,
            "operator": operator,
            "value": value,
        },
    )


class SemanticConstraintProtocolTests(unittest.TestCase):
    def test_mocked_consultant_language_variants_share_schema_invariants(self):
        variants = [
            "必须使用 genome.gtf",
            "gtf 就用我给的那个",
            "注释文件别换",
            "那个 genome.gtf 就行",
            "gtf必需用我给de，别换",
        ]
        for utterance in variants:
            with self.subTest(utterance=utterance):
                mocked_output = [compiled_row(
                    "required_input.annotation_gtf",
                    "/formal/genome.gtf",
                    confidence=0.82 if "de" in utterance else 0.98,
                    source_text=utterance,
                )]
                validated = validate_compiled_constraints(mocked_output)
                self.assertTrue(validated["valid"], validated["errors"])
                item = validated["constraints"][0]
                self.assertEqual(item["semantic_key"], "required_input.annotation_gtf")
                self.assertEqual(item["value"], "/formal/genome.gtf")
                self.assertEqual(item["strength"], "hard")
                self.assertIsInstance(item["confidence"], float)
                self.assertTrue(item["source_text"])

    def test_conditional_preference_and_ambiguity_remain_distinct(self):
        rows = [
            compiled_row(
                "failure_policy.java_missing",
                "block",
                operator="on_condition",
                strength="conditional",
                condition="java_missing",
                source_text="如果 Java 没有，就 blocked",
            ),
            compiled_row(
                "preference.database_source",
                "custom_genome",
                operator="prefer",
                strength="preference",
                confidence=0.75,
                source_text="最好使用 custom genome",
            ),
            compiled_row(
                "required_behavior.reference_choice",
                "unmapped",
                strength="ambiguous",
                confidence=0.3,
                ambiguity="UNMAPPED",
                needs_clarification=True,
                source_text="还是那个吧",
            ),
        ]
        result = validate_compiled_constraints(rows)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["metrics"]["conditional_count"], 1)
        self.assertEqual(result["metrics"]["preference_count"], 1)
        self.assertEqual(result["metrics"]["ambiguous_count"], 1)
        self.assertEqual(result["constraints"][0]["operator"], "on_condition")

    def test_invalid_consultant_payload_requires_structured_retry(self):
        result = validate_compiled_constraints("the complete raw prompt is a hard constraint")
        self.assertFalse(result["valid"])
        self.assertEqual(result["metrics"]["schema_validation"], "failed")
        self.assertEqual(result["constraints"], [])

    def test_consultant_json_payload_preserves_semantic_ir(self):
        payload = {
            "action_type": "finish",
            "selected_skills": ["genomics/snpeff-annotation"],
            "summary": "ready",
            "semantic_constraints": [compiled_row("required_input.annotation_gtf", "/formal/genome.gtf")],
        }
        wire = "<action_json>" + json.dumps(payload) + "</action_json>"
        self.assertEqual(BioAnalystAgent._consultant_json_payload(wire), payload)
        self.assertTrue(BioAnalystAgent._consultant_command_from_json(wire).startswith("FINISH"))

    def test_task_spec_consumes_compiled_contract_without_promoting_raw_prompt(self):
        raw = "If Java is missing block; use /formal/genome.gtf and do not guess a database."
        spec = create_task_spec(raw)
        rows = [
            compiled_row("required_input.annotation_gtf", "/formal/genome.gtf"),
            compiled_row("failure_policy.java_missing", "block", operator="on_condition", strength="conditional"),
            compiled_row("forbidden_behavior.species_database_guess", True, operator="forbid"),
        ]
        validation = validate_compiled_constraints(rows)
        spec = merge_compiled_constraints(spec, validation["constraints"], metrics=validation["metrics"])
        self.assertEqual(spec["user_directives"][0]["text"], raw)
        self.assertEqual(spec["semantic_compilation"]["schema_validation"], "passed")
        self.assertNotIn(raw, json.dumps(spec["requirements"], ensure_ascii=False))

    def test_session_1786157669_replay_goes_from_11_false_conflicts_to_zero(self):
        snapshot = json.loads(
            (FIXTURES / "session_1786157669_conflict_replay.json").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["session_id"], "session_1786157669")
        self.assertEqual(snapshot["audited_claim_count"], 37)
        self.assertEqual(snapshot["audited_before_user_plan_conflicts"], 11)
        self.assertEqual(len(snapshot["legacy_plan_propositions"]), 11)

        registry = ClaimRegistry()
        registry.register(
            snapshot["raw_prompt_provenance"],
            source_type="user_instruction",
            strength="hard_constraint",
            scope="task",
            scope_id=snapshot["session_id"],
            attributes={"provenance_only": True},
        )
        for index, proposition in enumerate(snapshot["legacy_plan_propositions"], 1):
            registry.register(
                proposition,
                source_type="plan",
                strength="required",
                scope="stage",
                scope_id=f"step-{index}",
            )
        after = [
            item for item in KnowledgeConflictDetector.detect(registry)
            if item.conflict_type == "USER_PLAN_CONFLICT"
        ]
        self.assertEqual(after, [])

        validation = validate_compiled_constraints(snapshot["semantic_constraints"])
        self.assertTrue(validation["valid"], validation["errors"])
        spec = merge_compiled_constraints(
            create_task_spec(snapshot["raw_prompt_provenance"], task_id=snapshot["session_id"]),
            validation["constraints"],
            metrics=validation["metrics"],
        )
        plan_state = {
            "task_id": snapshot["session_id"],
            "plan_version": "plan_v1",
            "task_spec": spec,
            "selected_skill_ids": ["genomics/snpeff-annotation"],
            "steps": [{
                "step_id": "step-1",
                "inputs": snapshot["legacy_plan_propositions"][1:4],
                "outputs": snapshot["legacy_plan_propositions"][4:],
                "skill_ids": ["genomics/snpeff-annotation"],
                "renderer_invocation_contract": snapshot["renderer_invocation"],
            }],
        }
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = "/nonexistent-read-only-replay"
        production_registry = agent._knowledge_claim_registry(
            snapshot["session_id"],
            {
                "task_spec": spec,
                "selected_skills": ["genomics/snpeff-annotation"],
                # This legacy field contained the complete raw prompt in the
                # audited session and must now be ignored by the comparator.
                "user_constraints": [snapshot["raw_prompt_provenance"]],
            },
            plan_state,
        )
        production_after = [
            item for item in KnowledgeConflictDetector.detect(production_registry)
            if item.conflict_type == "USER_PLAN_CONFLICT"
        ]
        self.assertEqual(production_after, [])

    def test_true_same_slot_conflicts_remain_blocking(self):
        cases = [
            ("required_input.reference_fasta", "/A.fa", "/B.fa", "eq", "eq"),
            ("required_behavior.database_source", "custom_genome", "preset", "eq", "eq"),
            ("required_behavior.annotation_format", "gtf22", "gff3", "eq", "eq"),
            ("forbidden_behavior.species_database_guess", True, True, "forbid", "eq"),
            ("forbidden_behavior.demo_or_toy_input", True, True, "forbid", "eq"),
        ]
        for key, expected, proposed, user_op, plan_op in cases:
            with self.subTest(key=key):
                registry = ClaimRegistry()
                register_typed(registry, "task_contract", key, expected, operator=user_op, strength="hard_constraint")
                register_typed(registry, "plan", key, proposed, operator=plan_op)
                conflicts = KnowledgeConflictDetector.detect(registry)
                self.assertEqual(len(conflicts), 1)
                self.assertEqual(conflicts[0].conflict_type, "USER_PLAN_CONFLICT")
                self.assertTrue(conflicts[0].blocking)

    def test_preference_mismatch_is_advisory_not_hard(self):
        registry = ClaimRegistry()
        register_typed(
            registry,
            "task_contract",
            "preference.database_source",
            "custom_genome",
            operator="prefer",
            strength="preferred",
        )
        register_typed(registry, "plan", "preference.database_source", "preset")
        conflict = KnowledgeConflictDetector.detect(registry)[0]
        self.assertEqual(conflict.conflict_type, "USER_PLAN_CONFLICT")
        self.assertFalse(conflict.blocking)

    def test_recovery_cannot_change_a_hard_semantic_slot(self):
        registry = ClaimRegistry()
        register_typed(
            registry,
            "task_contract",
            "required_behavior.annotation_format",
            "gtf22",
            strength="hard_constraint",
        )
        register_typed(registry, "recovery_proposal", "required_behavior.annotation_format", "gff3")
        conflict = KnowledgeConflictDetector.detect(registry)[0]
        self.assertEqual(conflict.conflict_type, "USER_RECOVERY_CONFLICT")
        self.assertTrue(conflict.blocking)

    def test_failure_policy_and_install_policy_are_deterministic(self):
        validation = validate_compiled_constraints([
            compiled_row(
                "failure_policy.java_missing",
                "block",
                operator="on_condition",
                strength="conditional",
                condition="java_missing",
            ),
        ])
        spec = merge_compiled_constraints(create_task_spec("runtime policy provenance"), validation["constraints"])
        self.assertTrue(evaluate_failure_policies(spec, {"java_missing": True})["blocked"])
        self.assertFalse(evaluate_failure_policies(spec, {"java_missing": False})["blocked"])
        self.assertFalse(semantic_install_policy(spec, dependency_key="java")["install_allowed"])

    def test_renderer_projection_emits_typed_behavior_claim(self):
        validation = validate_compiled_constraints([
            compiled_row("required_input.annotation_gtf", "/formal/genome.gtf"),
            compiled_row("required_behavior.annotation_format", "gtf22"),
        ])
        spec = merge_compiled_constraints(create_task_spec("provenance"), validation["constraints"])
        projection = project_task_spec_to_renderer(
            spec,
            {
                "arguments": {"annotation_gtf": {"required": True}},
                "routed_arguments": {
                    "annotation_file": {
                        "selector": "annotation_format",
                        "routes": {"gtf22": "annotation_gtf"},
                    }
                },
            },
        )
        self.assertTrue(projection["complete"], projection)
        self.assertIn(
            "required_behavior.annotation_format",
            {item["semantic_key"] for item in projection["semantic_claims"]},
        )

    def test_compiled_output_directory_binds_as_directory_not_filename(self):
        validation = validate_compiled_constraints([
            compiled_row("required_output.output_dir", "/formal/output")
        ])
        spec = merge_compiled_constraints(create_task_spec("provenance"), validation["constraints"])
        projection = project_task_spec_to_renderer(
            spec,
            {"arguments": {"output_dir": {"required": True}}},
        )
        self.assertTrue(projection["complete"], projection)
        self.assertEqual(projection["outdir"], "/formal/output")
        self.assertEqual(
            next(item for item in spec["output_contracts"] if item["role"] == "output_dir")["path"],
            "/formal/output",
        )

    def test_renderer_binding_removes_plan_parser_garbage(self):
        plan = """## Execution Checklist
- [ ] [step-1] Run annotation
  - Action: EXECUTE
  - Purpose: analysis
  - Skills: [genomics/snpeff-annotation]
  - Inputs:
    - **
    - ** none
    - VCF
    - FASTA
    - GTF 文件
    - Java 路径
  - Outputs:
    - annotated.vcf
  - Depends on: none
  - Success criteria: annotated VCF exists
  - Stage: none
  - Batch: none
"""
        parsed = parse_plan_steps(plan)
        self.assertNotIn("**", parsed[0]["inputs"])
        self.assertNotIn("** none", parsed[0]["inputs"])
        self.assertFalse({"VCF", "FASTA", "GTF 文件", "Java 路径"} & set(parsed[0]["inputs"]))
        parsed[0].update({
            "step_type": "renderer_composite",
            "selected_skill_id": "genomics/snpeff-annotation",
            "renderer_id": "genomics.snpeff_annotation",
        })
        spec = merge_task_spec(
            create_task_spec("provenance"),
            {
                "inputs": [
                    {"role": "variant_vcf", "path": "/formal/input.vcf"},
                    {"role": "reference_fasta", "path": "/formal/genome.fasta"},
                    {"role": "annotation_gtf", "path": "/formal/genome.gtf"},
                ],
                "requirements": [{
                    "requirement_id": "database_source",
                    "role": "database_source",
                    "value": "custom_genome",
                    "level": "required",
                }],
            },
            source="consultant_inferred",
            reason="test",
        )
        state = {"task_id": "task", "steps": parsed}
        records = [{
            "skill_id": "genomics/snpeff-annotation",
            "renderer_id": "genomics.snpeff_annotation",
            "invocation": {
                "arguments": {
                    "vcf": {"required": True},
                    "reference_fasta": {"required": True},
                    "annotation_gtf": {"required": True},
                    "genome_version": {"required": True},
                }
            },
        }]
        bound = bind_renderer_invocation_contracts(state, records, spec)
        self.assertEqual(
            bound["steps"][0]["inputs"],
            ["/formal/input.vcf", "/formal/genome.fasta", "/formal/genome.gtf"],
        )
        self.assertFalse({"VCF", "FASTA", "GTF 文件", "Java 路径"} & set(bound["steps"][0]["inputs"]))

    def test_inline_semantic_claim_json_stops_before_later_bracket_fields(self):
        claim = compiled_row("required_input.variant_vcf", "/formal/input.vcf")
        plan = (
            "## Execution Checklist\n"
            "- [ ] Step 1: annotate | Action: EXECUTE | "
            "Semantic claims: " + json.dumps([claim], ensure_ascii=False) + " | "
            "Depends on: [none] | Outputs: [out.vcf] | Done: output exists\n"
        )
        parsed = parse_plan_steps(plan)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(parsed[0]["semantic_claims"]), 1)
        self.assertEqual(
            parsed[0]["semantic_claims"][0]["semantic_key"],
            "required_input.variant_vcf",
        )
        self.assertEqual(parsed[0]["semantic_claims"][0]["value"], "/formal/input.vcf")
        self.assertEqual(parsed[0]["outputs"], ["out.vcf"])

    def test_approved_no_delta_reuses_semantic_snapshot(self):
        validation = validate_compiled_constraints([
            compiled_row("required_input.variant_vcf", "/formal/input.vcf")
        ])
        spec = merge_compiled_constraints(create_task_spec("provenance"), validation["constraints"])
        state = build_plan_state(
            "task",
            "- [ ] Step 1: run | Action: EXECUTE | Inputs: /formal/input.vcf | Outputs: out.vcf | Done: output exists",
        )
        state["task_spec"] = spec
        state["canonical_constraint_hash"] = canonical_constraint_hash(spec)
        registry = ClaimRegistry()
        register_typed(registry, "task_contract", "required_input.variant_vcf", "/formal/input.vcf", strength="hard_constraint")
        register_typed(registry, "plan", "required_input.variant_vcf", "/formal/input.vcf")
        state["knowledge_claim_set_hash"] = semantic_claim_set_hash(registry)
        approve_plan_state(state, 1)
        unchanged = approval_semantic_delta(
            state,
            current_constraint_hash=canonical_constraint_hash(spec),
            current_claim_set_hash=semantic_claim_set_hash(registry),
        )
        changed = approval_semantic_delta(
            state,
            current_constraint_hash=canonical_constraint_hash(spec),
            current_claim_set_hash="post-approval-new-fact",
        )
        self.assertFalse(unchanged["changed"], unchanged)
        self.assertTrue(changed["changed"])
        self.assertIn("semantic_claim_set_changed", changed["reasons"])


if __name__ == "__main__":
    unittest.main()

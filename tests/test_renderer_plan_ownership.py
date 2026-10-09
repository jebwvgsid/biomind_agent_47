import unittest
from pathlib import Path

from agent_core.plan_tracker import (
    build_plan_state,
    normalize_plan_contracts,
    validate_output_contracts,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import (
    create_task_spec,
    effective_output_contracts,
    ensure_task_spec,
    merge_compiled_constraints,
    validate_compiled_constraints,
)


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


class RendererPlanOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.skills = SkillManager(str(SKILLS_ROOT))
        self.renderer = self.skills.get_renderer_record("genomics/snpeff-annotation")

    def test_output_contracts_are_authoritative_over_legacy_expected_outputs(self):
        spec = ensure_task_spec({
            "goal": "annotate",
            "output_contracts": [{
                "filename": "annotated.vcf",
                "required": True,
                "source_type": "benchmark_contract",
                "source_ref": "task.outputs[0]",
            }],
            "expected_outputs": ["legacy-only.txt"],
        })

        contracts = effective_output_contracts(spec)

        self.assertEqual([item["required_basename"] for item in contracts], ["annotated.vcf"])
        self.assertEqual(contracts[0]["source_type"], "benchmark_contract")
        self.assertEqual(contracts[0]["source_ref"], "task.outputs[0]")

    def test_legacy_renderer_binding_does_not_invent_generate_owned_identity(self):
        state = build_plan_state(
            "task-renderer",
            "- [ ] Step 1: Run annotation | Action: EXECUTE | Skills: [genomics/snpeff-annotation] "
            "| Outputs: [annotated.vcf] | Done: annotated.vcf exists\n"
            "- [ ] Step 2: Write renderer reports | Action: EXECUTE | Skills: [] "
            "| Outputs: [report.md, reproduce.sh, stage_status.tsv] | Done: reports exist\n"
            "- [ ] Step 3: Write gene summary | Action: EXECUTE | Skills: [] "
            "| Outputs: [ann_gene_summary.tsv] | Done: ann_gene_summary.tsv exists\n",
        )
        state.setdefault("task_state", {})["execution_contract"] = "legacy_renderer_v1"
        contracts = effective_output_contracts({
            "output_contracts": [
                {"filename": name, "required": True, "source_type": "user_explicit"}
                for name in ("annotated.vcf", "report.md", "reproduce.sh", "stage_status.tsv", "ann_gene_summary.tsv")
            ]
        })

        task_spec = create_task_spec(
            "Use /tmp/input.vcf with a custom genome /tmp/reference.fasta and /tmp/annotation.gtf using -gtf22. "
            "Output directory: /tmp/renderer-output",
            task_id="task-renderer",
            project_root="/tmp",
        )
        compiled = validate_compiled_constraints([
            {"constraint_id": "renderer-db", "semantic_key": "required_behavior.database_source", "operator": "eq", "value": "custom_genome", "strength": "hard", "confidence": 0.99, "source_ref": "mock_consultant"},
            {"constraint_id": "renderer-format", "semantic_key": "required_behavior.annotation_format", "operator": "eq", "value": "gtf22", "strength": "hard", "confidence": 0.99, "source_ref": "mock_consultant"},
        ])
        task_spec = merge_compiled_constraints(
            task_spec,
            compiled["constraints"],
            metrics=compiled["metrics"],
        )

        normalized, audit, violations = normalize_plan_contracts(
            state,
            renderer_records=[self.renderer],
            output_contracts=contracts,
            task_spec=task_spec,
        )

        violation_codes = [item["violation_code"] for item in violations]
        self.assertEqual(
            violation_codes.count("SEMANTIC_CONSTRAINT_UNCOVERED"),
            2,
        )
        self.assertEqual(
            violation_codes.count("RENDERER_MISSING_REQUIRED_BINDING"),
            1,
        )
        self.assertEqual([step["step_id"] for step in normalized["steps"]], ["step-1", "step-2", "step-3"])
        anchor = normalized["steps"][0]
        self.assertTrue(anchor["renderer_composite"])
        self.assertEqual(anchor["renderer_id"], "genomics.snpeff_annotation")
        self.assertEqual(anchor["selected_skill_id"], "genomics/snpeff-annotation")
        self.assertNotIn("renderer_invocation_contract", anchor)
        self.assertEqual(
            anchor["renderer_projection"]["missing_bindings"],
            ["genome_version"],
        )
        self.assertNotIn(
            "genome_version",
            anchor["renderer_projection"]["arguments"],
        )
        self.assertEqual(audit[0]["removed_step_ids"], [])

    def test_duplicate_output_producer_is_blocking(self):
        state = build_plan_state(
            "task-duplicate",
            "- [ ] Step 1: First | Action: EXECUTE | Skills: [] | Outputs: [report.md] | Done: report exists\n"
            "- [ ] Step 2: Second | Action: EXECUTE | Skills: [] | Outputs: [/tmp/out/report.md] | Done: report exists\n",
        )
        state.setdefault("task_state", {})["execution_contract"] = "legacy_renderer_v1"
        contracts = effective_output_contracts({
            "output_contracts": [
                {"role": "output_dir", "path": "/tmp/out", "required": True},
                {"filename": "report.md", "required": True, "source_type": "system_required"},
            ],
        })

        violations = validate_output_contracts(state, contracts)

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["violation_code"], "OUTPUT_PRODUCER_AMBIGUOUS")
        self.assertEqual(violations[0]["affected_step_ids"], ["step-1", "step-2"])

    def test_renderer_capability_without_invocation_is_not_a_producer(self):
        state = build_plan_state(
            "task-capability-only",
            "- [ ] Step 1: Renderer capability | Action: EXECUTE | Skills: [genomics/snpeff-annotation] "
            "| Outputs: [annotated.vcf] | Done: annotated.vcf exists\n",
        )
        state["steps"][0].update({"step_type": "renderer_composite", "renderer_id": "snpeff"})
        violations = validate_output_contracts(
            state,
            effective_output_contracts({"output_contracts": [{"filename": "annotated.vcf", "required": True}]}),
        )
        self.assertEqual(violations[0]["violation_code"], "REQUIRED_OUTPUT_UNCOVERED")

    def test_optional_output_does_not_trigger_coverage_repair(self):
        state = build_plan_state(
            "task-optional",
            "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | Outputs: [required.tsv] | Done: required.tsv exists\n",
        )
        state.setdefault("task_state", {})["execution_contract"] = "legacy_renderer_v1"
        contracts = effective_output_contracts({
            "output_contracts": [
                {"filename": "required.tsv", "required": True},
                {"filename": "optional.tsv", "required": False},
            ],
        })

        self.assertFalse(validate_output_contracts(state, contracts))


if __name__ == "__main__":
    unittest.main()

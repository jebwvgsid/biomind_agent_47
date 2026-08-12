import copy
import json
import unittest

from agent_core.plan_tracker import (
    approve_plan_state,
    build_approval_projection,
    build_approval_scope,
    build_plan_state,
    current_plan_step_with_index,
    normalize_plan_contracts,
    plan_approval_is_current,
    validate_action_approval_lineage,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import (
    create_task_spec,
    effective_output_contracts,
    ensure_task_spec,
    merge_compiled_constraints,
    semantic_constraints_from_task_spec,
    validate_compiled_constraints,
)


class PlanContractCanaryTests(unittest.TestCase):
    TASK_ID = "canary-snpeff-plan-contract-20260807"

    def setUp(self):
        self.task_spec = create_task_spec(
            "Annotate /tmp/all.filtered.snp.vcf using custom genome /tmp/genome.fasta and "
            "/tmp/genome.gtf with -gtf22. Output directory: /tmp/snpeff-canary. "
            "Output annotated.vcf, ann_gene_summary.tsv, report.md and reproduce.sh.",
            task_id=self.TASK_ID,
            project_root="/tmp",
        )
        compiled = validate_compiled_constraints([
            {"constraint_id": "canary-db", "semantic_key": "required_behavior.database_source", "operator": "eq", "value": "custom_genome", "strength": "hard", "confidence": 0.99, "source_ref": "mock_consultant"},
            {"constraint_id": "canary-format", "semantic_key": "required_behavior.annotation_format", "operator": "eq", "value": "gtf22", "strength": "hard", "confidence": 0.99, "source_ref": "mock_consultant"},
        ])
        self.task_spec = merge_compiled_constraints(
            self.task_spec,
            compiled["constraints"],
            metrics=compiled["metrics"],
        )
        self.task_spec["output_contracts"] = [
            {
                "output_id": "output_dir",
                "role": "output_dir",
                "artifact_kind": "directory",
                "path": "/tmp/snpeff-canary",
                "required": True,
            },
            *[
                {
                    "output_id": output_id,
                    "role": output_id,
                    "artifact_kind": "file",
                    "path": f"/tmp/snpeff-canary/{filename}",
                    "required": True,
                }
                for output_id, filename in (
                    ("annotated_vcf", "annotated.vcf"),
                    ("ann_summary", "ann_gene_summary.tsv"),
                    ("report", "report.md"),
                    ("reproduce_script", "reproduce.sh"),
                )
            ],
        ]
        self.task_spec = ensure_task_spec(self.task_spec)
        self.renderer = SkillManager("skills").get_renderer_record("genomics/snpeff-annotation")
        self.outputs = effective_output_contracts(self.task_spec)

    def _candidate(self, outputs, *, first_action="execute"):
        constraint_ids = [
            str(item.get("constraint_id") or "")
            for item in semantic_constraints_from_task_spec(self.task_spec)
            if str(item.get("constraint_id") or "")
        ]
        return {
            "steps": [
                {
                    "step_id": "step-1",
                    "title": "Validate inputs",
                    "action": first_action,
                    "purpose": "validation",
                    "tool": "managed-shell",
                    "inputs": [],
                    "outputs": [],
                    "dependencies": [],
                    "success_criteria": ["inputs exist and are non-empty"],
                    "skill_ids": [],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                    "semantic_constraint_ids": [],
                },
                {
                    "step_id": "step-2",
                    "title": "Run annotation and produce required artifacts",
                    "action": "execute",
                    "purpose": "analysis",
                    "tool": "snpEff",
                    "inputs": [],
                    "outputs": list(outputs),
                    "dependencies": ["step-1"],
                    "success_criteria": ["all declared outputs exist and are non-empty"],
                    "skill_ids": ["genomics/snpeff-annotation"],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                    "semantic_constraint_ids": constraint_ids,
                },
            ],
            "semantic_claims": [],
            "validation_checklist": [{
                "check_id": "required-outputs",
                "description": "validate all required task outputs",
            }],
            "metadata": {
                "schema_version": 1,
                "planner_contract": "structured_plan_v1",
            },
        }

    def test_canary_a_to_e_contract_lifecycle(self):
        first = build_plan_state(
            self.TASK_ID,
            json.dumps(self._candidate(["annotated_vcf"], first_action="search")),
            strict=True,
        )
        first["task_spec"] = self.task_spec
        first["selected_skill_ids"] = ["genomics/snpeff-annotation"]
        _, _, first_violations = normalize_plan_contracts(
            first,
            renderer_records=[self.renderer],
            output_contracts=self.outputs,
            task_spec=self.task_spec,
        )
        self.assertTrue(any(item.get("violation_code") == "INVALID_ACTION_FOR_STEP_PURPOSE" for item in first_violations))
        self.assertTrue(any(item.get("violation_code") == "REQUIRED_OUTPUT_UNCOVERED" for item in first_violations))

        repaired = build_plan_state(
            self.TASK_ID,
            json.dumps(self._candidate([
                "annotated_vcf",
                "ann_summary",
                "report",
                "reproduce_script",
            ])),
            strict=True,
        )
        repaired["task_spec"] = self.task_spec
        repaired["selected_skill_ids"] = ["genomics/snpeff-annotation"]
        repaired, _, repaired_violations = normalize_plan_contracts(
            repaired,
            renderer_records=[self.renderer],
            output_contracts=self.outputs,
            task_spec=self.task_spec,
        )
        self.assertFalse(repaired_violations)
        self.assertFalse(any(
            step.get("renderer_invocation_contract") or step.get("renderer_composite")
            for step in repaired["steps"]
        ))

        projection = build_approval_projection(repaired)
        approve_plan_state(repaired, 1, approval_projection=projection)
        self.assertTrue(plan_approval_is_current(repaired, 1))
        self.assertEqual(repaired.get("execution_cursor_step_id", ""), "")
        self.assertEqual(current_plan_step_with_index(repaired)[1]["step_id"], "step-1")

        mutated = copy.deepcopy(repaired)
        mutated["steps"][1]["outputs"].append("undeclared_output")
        self.assertFalse(plan_approval_is_current(mutated, 1))

        scope = build_approval_scope(repaired)
        action = {
            "step_id": "step-1",
            "task_revision": 1,
            "plan_version": repaired["plan_version"],
            "generated_from_plan_hash": scope["plan_hash"],
            "generated_from_approval_scope_hash": scope["approval_scope_hash"],
        }
        self.assertEqual(validate_action_approval_lineage(repaired, action, 1), "")


if __name__ == "__main__":
    unittest.main()

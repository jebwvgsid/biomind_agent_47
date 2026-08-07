import copy
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
from agent_core.task_spec import create_task_spec, effective_output_contracts


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
        self.renderer = SkillManager("skills").get_renderer_record("genomics/snpeff-annotation")
        self.outputs = effective_output_contracts(self.task_spec)

    def test_canary_a_to_e_contract_lifecycle(self):
        first = build_plan_state(
            self.TASK_ID,
            "- [ ] Step 1: Validate inputs | Action: SEARCH | Purpose: validation | Skills: [] "
            "| Done: input files exist and are non-empty\n"
            "- [ ] Step 2: Run annotation | Action: EXECUTE | Skills: [genomics/snpeff-annotation] "
            "| Outputs: [annotated.vcf] | Done: annotated.vcf exists\n",
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
        self.assertFalse(any(item.get("violation_code") == "REQUIRED_OUTPUT_UNCOVERED" for item in first_violations))

        repaired = build_plan_state(
            self.TASK_ID,
            "- [ ] Step 1: Validate inputs | Action: EXECUTE | Purpose: validation | Skills: [] "
            "| Done: input files exist and are non-empty\n"
            "- [ ] Step 2: Run renderer-backed annotation | Action: EXECUTE | Purpose: analysis "
            "| Skills: [genomics/snpeff-annotation] "
            "| Outputs: [annotated.vcf, ann_gene_summary.tsv, report.md, reproduce.sh] "
            "| Done: all required outputs exist and are non-empty\n",
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
        renderer_step = next(step for step in repaired["steps"] if step.get("renderer_invocation_contract"))
        self.assertTrue(renderer_step["renderer_invocation_contract"]["invocation_id"])

        projection = build_approval_projection(repaired)
        approve_plan_state(repaired, 1, approval_projection=projection)
        self.assertTrue(plan_approval_is_current(repaired, 1))
        self.assertEqual(repaired.get("execution_cursor_step_id", ""), "")
        self.assertEqual(current_plan_step_with_index(repaired)[1]["step_id"], "step-1")

        mutated = copy.deepcopy(repaired)
        mutated["steps"][1]["outputs"].append("undeclared.txt")
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

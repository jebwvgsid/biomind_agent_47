from __future__ import annotations

import json
import unittest

from agent_core.completion_gate import _expected_artifacts_from_final_plan
from agent_core.plan_tracker import (
    build_plan_state,
    step_artifact_contracts,
)


class SuccessCriteriaArtifactVersioningTests(unittest.TestCase):
    def test_strict_v2_does_not_project_negative_examples_as_artifacts(self) -> None:
        candidate = {
            "steps": [{
                "step_id": "annotate",
                "title": "Annotate the declared VCF",
                "action": "execute",
                "purpose": "analysis",
                "tool": "declared_capability",
                "inputs": [],
                "outputs": [],
                "dependencies": [],
                "success_criteria": ["未使用 tests/demo_ref.fa 或 demo.vcf"],
                "skill_ids": ["genomics/snpeff-annotation"],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
                "semantic_constraint_ids": [],
            }],
            "semantic_claims": [],
            "validation_checklist": [{
                "check_id": "check-input-provenance",
                "description": "verify declared input provenance",
            }],
            "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
        }

        state = build_plan_state("strict-v2", json.dumps(candidate), strict=True)
        step = state["steps"][0]

        self.assertEqual(step["artifacts"], [])
        self.assertEqual(step_artifact_contracts(step), [])
        expected = _expected_artifacts_from_final_plan(state, "")
        self.assertNotIn("tests/demo_ref.fa", expected)
        self.assertNotIn("demo.vcf", expected)

    def test_explicit_legacy_adapter_keeps_done_path_extraction(self) -> None:
        plan_text = (
            "- [ ] Step 1: Write report | Action: EXECUTE | "
            "Skills: [] | Done: report.md exists\n"
        )
        state = build_plan_state("legacy-plan", plan_text, strict=False)
        step = state["steps"][0]

        self.assertLess(int(state["schema_version"]), 8)
        self.assertIn("report.md", [item["path"] for item in step["artifacts"]])
        self.assertIn(
            "report.md",
            [
                item["path"]
                for item in step_artifact_contracts(
                    step,
                    allow_legacy_text_fallback=True,
                )
            ],
        )
        self.assertIn("report.md", _expected_artifacts_from_final_plan(state, plan_text))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest

from agent_core import plan_tracker as plan_tracker_module
from agent_core.plan_contract import (
    planner_candidate_body_from_state,
    validate_planner_candidate_deterministically,
)
from agent_core.plan_tracker import (
    build_plan_state,
    derive_plan_validation_state,
    validation_issue,
)
from agent_core.skill_manager import SkillManager
from bio_agents import BioAnalystAgent


KNOWN_SKILL = "genomics/snpeff-annotation"


def _candidate(
    *,
    dependency: str = "step-1",
    step_two_inputs: list[str] | None = None,
) -> dict:
    return {
        "steps": [
            {
                "step_id": "step-1",
                "title": "Prepare report.md",
                "action": "execute",
                "purpose": "analysis",
                "tool": "tool-a",
                "inputs": [],
                "outputs": [],
                "dependencies": [],
                "success_criteria": ["report.md is described without rewriting its name"],
                "skill_ids": [KNOWN_SKILL],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
            },
            {
                "step_id": "step-2",
                "title": "Use report.md",
                "action": "execute",
                "purpose": "analysis",
                "tool": "tool-b",
                "inputs": list(step_two_inputs or []),
                "outputs": [],
                "dependencies": [dependency],
                "success_criteria": ["report.md remains a literal prose basename"],
                "skill_ids": [KNOWN_SKILL],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
            },
        ],
        "validation_checklist": [
            {"check_id": "check-plan", "description": "validate plan contracts"}
        ],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }


def _state(**kwargs) -> dict:
    return build_plan_state(
        "planner-corrective-repair",
        json.dumps(_candidate(**kwargs)),
        strict=True,
    )


class LexicalAuthorityRetirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manager = SkillManager("skills")

    def assert_not_blocked_by_prose(self, prose: str) -> None:
        self.assertEqual(self.manager.validate_plan(prose, [KNOWN_SKILL]), [])

    def test_negated_placeholder_is_not_authoritative(self) -> None:
        self.assert_not_blocked_by_prose(
            "No fake or placeholder ANN annotations are written."
        )

    def test_positive_placeholder_is_not_a_keyword_rule(self) -> None:
        self.assert_not_blocked_by_prose(
            "Write placeholder ANN annotations when annotation fails."
        )

    def test_other_retired_phrases_are_not_authoritative(self) -> None:
        self.assert_not_blocked_by_prose(
            "Use an abstracted command template while documenting the operation."
        )

    def test_script_name_regex_is_not_authoritative(self) -> None:
        prose = "Discuss fictional_pipeline.py in the rationale."
        self.assert_not_blocked_by_prose(prose)
        telemetry = self.manager.plan_review_telemetry(prose, [KNOWN_SKILL])
        self.assertFalse(telemetry["authoritative"])
        self.assertEqual(
            telemetry["undocumented_script_mentions"],
            ["fictional_pipeline.py"],
        )

    def test_retained_step_skill_rule_is_typed_and_exact(self) -> None:
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.skill_manager = self.manager
        issues = agent._plan_step_skill_violations(
            {
                "steps": [
                    {
                        "step_id": "step-4",
                        "skills_declared": False,
                        "skill_ids": [],
                    }
                ]
            },
            [KNOWN_SKILL],
        )

        self.assertEqual(issues[0]["code"], "PLAN_STEP_SKILLS_UNDECLARED")
        self.assertEqual(issues[0]["validator_id"], "plan_skill_bindings")
        self.assertEqual(issues[0]["step_id"], "step-4")
        self.assertEqual(issues[0]["field_path"], "$.steps[0].skill_ids")
        self.assertTrue(issues[0]["fingerprint"])


class PlanPatchRetirementTests(unittest.TestCase):
    def test_semantic_patch_and_repair_projection_are_absent(self) -> None:
        for name in (
            "PLAN_PATCH_OPERATION",
            "build_plan_repair_request",
            "parse_plan_patch",
            "apply_plan_patch",
        ):
            self.assertFalse(hasattr(plan_tracker_module, name), name)


class SharedDeterministicRevalidationTests(unittest.TestCase):
    def validate(self, candidate: dict):
        return validate_planner_candidate_deterministically(
            candidate,
            candidate_scope_id="candidate-scope-test",
            authoritative_handles={},
            availability_origins={},
        )

    def test_legacy_wire_slots_are_retained_during_state_revalidation(self) -> None:
        legacy = _candidate()
        legacy["semantic_claims"] = []
        legacy["metadata"] = {
            "schema_version": 1,
            "planner_contract": "structured_plan_v1",
        }
        for step in legacy["steps"]:
            step["semantic_constraint_ids"] = []
        state = build_plan_state(
            "legacy-repair",
            json.dumps(legacy),
            strict=True,
        )

        projected = planner_candidate_body_from_state(state)
        _keys, _usage, issues = self.validate(projected)

        self.assertIn("semantic_claims", projected)
        self.assertIn("semantic_constraint_ids", projected["steps"][0])
        self.assertEqual(issues, [])


class ExactIssueConvergenceTests(unittest.TestCase):
    def test_same_code_at_different_location_is_progress(self) -> None:
        issue_a = validation_issue(
            "VALIDATION_ERROR",
            owner="planner",
            step_id="step-1",
            field_path="$.steps[0].inputs[0]",
            message="issue A",
        )
        issue_b = validation_issue(
            "VALIDATION_ERROR",
            owner="planner",
            step_id="step-2",
            field_path="$.steps[1].outputs[0]",
            message="issue B",
        )
        first = derive_plan_validation_state(_state(), [issue_a])
        second = derive_plan_validation_state(first, [issue_b])
        self.assertEqual(second["repair_convergence"]["status"], "converging")

    def test_same_exact_issue_is_non_convergent_not_internal_conflict(self) -> None:
        issue = validation_issue(
            "VALIDATION_ERROR",
            owner="planner",
            step_id="step-2",
            field_path="$.steps[1].dependencies[0]",
            message="same exact issue",
        )
        first = derive_plan_validation_state(_state(), [issue])
        repeated = derive_plan_validation_state(first, [issue])

        self.assertEqual(
            repeated["repair_convergence"]["status"],
            "blocked_non_convergent",
        )
        self.assertEqual(repeated["repair_convergence"]["cycle_type"], "A_TO_A")


if __name__ == "__main__":
    unittest.main()

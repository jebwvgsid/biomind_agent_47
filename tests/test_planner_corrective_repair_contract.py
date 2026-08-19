from __future__ import annotations

import json
import unittest

from agent_core.node_context import (
    build_node_context,
    resolve_plan_proposal_handles,
)
from agent_core.plan_contract import (
    planner_candidate_body_from_state,
    validate_planner_candidate_deterministically,
)
from agent_core.plan_tracker import (
    PLAN_PATCH_OPERATION,
    apply_plan_patch,
    build_plan_repair_request,
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


def _task_spec() -> dict:
    return {
        "schema_version": 4,
        "task_id": "planner-corrective-repair",
        "revision": 1,
        "goal": "Create report.md without rewriting prose references.",
        "inputs": [
            {
                "input_id": "input-vcf-id",
                "semantic_handle": "input.variant_vcf",
                "role": "variant_vcf",
                "path": "/data/input.vcf",
                "required": True,
            }
        ],
        "output_contracts": [
            {
                "output_id": "report.md",
                "semantic_handle": "output.report",
                "role": "report",
                "path": "/work/report.md",
                "required": True,
            }
        ],
    }


def _projection_plan() -> dict:
    plan = _candidate()
    plan["steps"][0]["inputs"] = ["input-vcf-id"]
    plan["steps"][0]["outputs"] = ["report.md"]
    plan["steps"][1]["inputs"] = ["report.md"]
    for step in plan["steps"]:
        step["skills_declared"] = True
    return plan


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


class ExactRepairScopeTests(unittest.TestCase):
    def test_structured_issue_survives_repair_projection(self) -> None:
        state = _state()
        issue = validation_issue(
            "PLAN_DEPENDENCY_UNKNOWN",
            owner="planner",
            step_id="step-2",
            field_path="$.steps[1].dependencies[0]",
            repair_target="$.steps[1].dependencies[0]",
            message="Dependency does not identify an existing plan step.",
            validator_id="plan_dag",
            bounded_excerpt="step.step-1",
        )

        request = build_plan_repair_request(state, [issue])

        self.assertEqual(request["allowed_step_ids"], ["step-2"])
        self.assertEqual(
            [step["step_id"] for step in request["affected_steps"]],
            ["step-2"],
        )
        projected = request["violations"][0]
        for field in (
            "code",
            "validator_id",
            "step_id",
            "field_path",
            "message",
            "fingerprint",
            "bounded_excerpt",
            "repair_target",
        ):
            self.assertEqual(projected[field], issue[field])

    def test_missing_location_does_not_authorize_every_step(self) -> None:
        state = _state()
        issue = validation_issue(
            "PLAN_GLOBAL_UNKNOWN",
            owner="planner",
            message="No unique repair location is known.",
            validator_id="plan_global",
        )
        request = build_plan_repair_request(state, [issue])
        self.assertEqual(request["allowed_step_ids"], [])

        patch = {
            "operation": PLAN_PATCH_OPERATION,
            "base_plan_version": state["plan_revision"],
            "base_plan_hash": state["semantic_plan_hash"],
            "replace_steps": [
                {"step_id": "step-1", "new_step": {"title": "unauthorized"}}
            ],
        }
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            apply_plan_patch(state, patch, allowed_step_ids=[])


class SemanticProjectionRoundTripTests(unittest.TestCase):
    def context(self):
        plan = _projection_plan()
        return build_node_context(
            node_type="planner_repair",
            task_spec=_task_spec(),
            plan_state=plan,
            approved_plan_snapshot=plan,
            violations=[
                validation_issue(
                    "PLAN_DEPENDENCY_UNKNOWN",
                    owner="planner",
                    step_id="step-2",
                    field_path="$.steps[1].dependencies[0]",
                    message="bad dependency",
                    validator_id="plan_dag",
                )
            ],
        )

    def test_previous_plan_projection_is_idempotent_and_preserves_prose(self) -> None:
        payload = self.context().llm_payload.to_provider_dict()
        steps = payload["current_focus"]["candidate_plan"]["steps"]

        self.assertEqual(steps[1]["dependencies"], ["step.step-1"])
        self.assertNotIn("step.step.step-1", json.dumps(payload))
        self.assertEqual(steps[0]["title"], "Prepare report.md")
        self.assertEqual(
            steps[0]["success_criteria"],
            ["report.md is described without rewriting its name"],
        )
        self.assertEqual(steps[0]["outputs"], ["output.report"])
        self.assertEqual(steps[0]["skill_ids"], [KNOWN_SKILL])

    def test_dependency_and_artifact_handles_resolve_to_canonical_ids(self) -> None:
        context = self.context()
        resolved = resolve_plan_proposal_handles(
            {
                "operation": PLAN_PATCH_OPERATION,
                "replace_steps": [
                    {
                        "step_handle": "step.step-2",
                        "new_step": {
                            "dependencies": ["step.step-1"],
                            "inputs": ["output.report"],
                            "outputs": [],
                            "skill_ids": [KNOWN_SKILL],
                        },
                    }
                ],
            },
            context.binding,
        )
        replacement = resolved["replace_steps"][0]

        self.assertEqual(replacement["step_id"], "step-2")
        self.assertEqual(replacement["new_step"]["dependencies"], ["step-1"])
        self.assertEqual(replacement["new_step"]["inputs"], ["report.md"])
        self.assertEqual(replacement["new_step"]["skill_ids"], [KNOWN_SKILL])


class SharedDeterministicRevalidationTests(unittest.TestCase):
    def validate(self, candidate: dict):
        return validate_planner_candidate_deterministically(
            candidate,
            candidate_scope_id="candidate-scope-test",
            authoritative_handles={},
            availability_origins={},
        )

    def patch(self, state: dict, new_step: dict) -> dict:
        patch = {
            "operation": PLAN_PATCH_OPERATION,
            "base_plan_version": state["plan_revision"],
            "base_plan_hash": state["semantic_plan_hash"],
            "replace_steps": [{"step_id": "step-2", "new_step": new_step}],
        }
        return apply_plan_patch(
            state,
            patch,
            allowed_step_ids=["step-2"],
        )[0]

    def test_patch_with_projected_dependency_is_rejected_by_shared_dag(self) -> None:
        repaired = self.patch(_state(), {"dependencies": ["step.step-1"]})
        repaired_wire = planner_candidate_body_from_state(repaired)
        _keys, _usage, issues = self.validate(repaired_wire)

        self.assertEqual(issues[0]["code"], "PLAN_DEPENDENCY_UNKNOWN")
        self.assertEqual(issues[0]["validator_id"], "plan_dag")
        self.assertEqual(issues[0]["step_id"], "step-2")
        self.assertEqual(issues[0]["field_path"], "$.steps[1].dependencies[0]")

    def test_patch_and_initial_candidate_share_artifact_ref_validation(self) -> None:
        malformed_initial = _candidate(step_two_inputs=["missing-artifact"])
        _keys, _usage, initial_issues = self.validate(malformed_initial)

        repaired = self.patch(
            _state(),
            {"inputs": ["missing-artifact"]},
        )
        repaired_wire = planner_candidate_body_from_state(repaired)
        _keys, _usage, repaired_issues = self.validate(repaired_wire)

        self.assertEqual(initial_issues[0]["code"], "ARTIFACT_REFERENCE_UNKNOWN")
        self.assertEqual(repaired_issues[0]["code"], initial_issues[0]["code"])
        self.assertEqual(
            repaired_issues[0]["field_path"],
            "$.steps[1].inputs[0]",
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

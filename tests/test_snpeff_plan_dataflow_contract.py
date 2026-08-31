from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.plan_contract import (
    PlannerCandidateError,
    candidate_artifact_ref_keys,
    prepare_allocation_intent,
    promote_allocation_intent,
    rewrite_candidate_local_refs,
    structured_plan_candidate_diagnostics,
    validate_candidate_graph_and_flow,
)
from agent_core.plan_tracker import render_plan_review_content
from prompts.manager import PromptManager


SKILL_ID = "genomics/snpeff-annotation"


def _skill_document() -> str:
    return Path("skills/genomics/snpeff-annotation/SKILL.md").read_text(encoding="utf-8")


def _step(
    step_id: str,
    *,
    inputs: list[str],
    outputs: list[str],
    dependencies: list[str],
) -> dict:
    return {
        "step_id": step_id,
        "title": "structured capability step",
        "action": "execute",
        "purpose": "analysis",
        "tool": "declared_capability",
        "inputs": inputs,
        "outputs": outputs,
        "dependencies": dependencies,
        "success_criteria": ["declared evidence is valid"],
        "skill_ids": [SKILL_ID],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
    }


def _candidate(
    steps: list[dict],
    *,
    proposed_artifacts: list[dict] | None = None,
) -> dict:
    candidate = {
        "steps": list(steps),
        "validation_checklist": [{
            "check_id": "check-dataflow",
            "description": "verify typed artifact and dependency contracts",
        }],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }
    if proposed_artifacts is not None:
        candidate["proposed_artifacts"] = list(proposed_artifacts)
    return candidate


def _plan_local_directory(
    local_ref: str = "plan.snpeff_database_dir",
    *,
    policy_id: str = "managed_intermediate_v1",
) -> dict:
    return {
        "local_ref": local_ref,
        "semantic_view": {
            "role": "snpeff_database_dir",
            "format": "directory",
            "description": "custom snpEff database",
        },
        "artifact_contract": {
            "object_kind": "directory",
            "lifecycle": "plan_local",
            "materialization_policy_id": policy_id,
        },
    }


class SnpEffPlanDataflowContractTests(unittest.TestCase):
    def test_same_skill_ordering_only_dependency_is_structurally_valid(self) -> None:
        candidate = _candidate([
            _step("validate-inputs", inputs=[], outputs=[], dependencies=[]),
            _step(
                "build-db",
                inputs=[],
                outputs=[],
                dependencies=["validate-inputs"],
            ),
        ])
        self.assertEqual(structured_plan_candidate_diagnostics(candidate), [])
        ref_keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id="candidate-scope-ordering-only",
            authoritative_handles={},
        )

        self.assertEqual(
            validate_candidate_graph_and_flow(
                candidate,
                ref_keys=ref_keys,
                usage=usage,
                availability_origins={},
            ),
            [],
        )

    def test_split_plan_reuses_one_allocated_directory_identity(self) -> None:
        candidate = _candidate(
            [
                _step("build-db", inputs=[], outputs=["plan.snpeff_database_dir"], dependencies=[]),
                _step("annotate", inputs=["plan.snpeff_database_dir"], outputs=[], dependencies=["build-db"]),
            ],
            proposed_artifacts=[_plan_local_directory()],
        )
        scope = "candidate-scope-snpeff"
        ref_keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id=scope,
            authoritative_handles={},
        )
        self.assertEqual(
            validate_candidate_graph_and_flow(
                candidate,
                ref_keys=ref_keys,
                usage=usage,
                availability_origins={},
            ),
            [],
        )
        intent = prepare_allocation_intent(
            candidate,
            candidate_scope_id=scope,
            planning_epoch_id="planning-epoch-snpeff",
        )
        with tempfile.TemporaryDirectory() as root:
            mapping, bindings = promote_allocation_intent(
                intent,
                task_id="snpeff-plan",
                managed_root=str(Path(root) / "plan_artifacts"),
            )
        rewritten = rewrite_candidate_local_refs(candidate, mapping)
        producer_id = rewritten["steps"][0]["outputs"][0]
        consumer_id = rewritten["steps"][1]["inputs"][0]

        self.assertEqual(producer_id, consumer_id)
        self.assertEqual(producer_id, mapping["plan.snpeff_database_dir"])
        self.assertEqual(bindings[producer_id]["object_kind"], "directory")
        self.assertEqual(bindings[producer_id]["lifecycle"], "plan_local")

    def test_unknown_artifact_reference_fails_closed(self) -> None:
        candidate = _candidate([
            _step(
                "annotate",
                inputs=["undeclared_database_dir"],
                outputs=[],
                dependencies=[],
            ),
        ])

        with self.assertRaises(PlannerCandidateError) as caught:
            candidate_artifact_ref_keys(
                candidate,
                candidate_scope_id="candidate-scope-unknown-artifact",
                authoritative_handles={},
            )

        self.assertEqual(caught.exception.code, "ARTIFACT_REFERENCE_UNKNOWN")

    def test_declared_artifact_without_producer_fails_closed(self) -> None:
        candidate = _candidate(
            [_step(
                "annotate",
                inputs=["plan.snpeff_database_dir"],
                outputs=[],
                dependencies=[],
            )],
            proposed_artifacts=[_plan_local_directory()],
        )
        ref_keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id="candidate-scope-missing-producer",
            authoritative_handles={},
        )

        issues = validate_candidate_graph_and_flow(
            candidate,
            ref_keys=ref_keys,
            usage=usage,
            availability_origins={},
        )

        self.assertIn(
            "PLAN_ARTIFACT_UNAVAILABLE",
            {str(issue.get("code") or "") for issue in issues},
        )

    def test_multiple_artifact_producers_fail_closed(self) -> None:
        candidate = _candidate(
            [
                _step(
                    "build-db-a",
                    inputs=[],
                    outputs=["plan.snpeff_database_dir"],
                    dependencies=[],
                ),
                _step(
                    "build-db-b",
                    inputs=[],
                    outputs=["plan.snpeff_database_dir"],
                    dependencies=[],
                ),
                _step(
                    "annotate",
                    inputs=["plan.snpeff_database_dir"],
                    outputs=[],
                    dependencies=["build-db-a", "build-db-b"],
                ),
            ],
            proposed_artifacts=[_plan_local_directory()],
        )
        ref_keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id="candidate-scope-multiple-producers",
            authoritative_handles={},
        )

        issues = validate_candidate_graph_and_flow(
            candidate,
            ref_keys=ref_keys,
            usage=usage,
            availability_origins={},
        )

        self.assertIn(
            "PLAN_ARTIFACT_PRODUCER_CARDINALITY",
            {str(issue.get("code") or "") for issue in issues},
        )

    def test_unknown_and_cyclic_dependencies_fail_closed(self) -> None:
        cases = {
            "unknown": (
                _candidate([
                    _step("prepare", inputs=[], outputs=[], dependencies=[]),
                    _step(
                        "annotate",
                        inputs=[],
                        outputs=[],
                        dependencies=["missing-step"],
                    ),
                ]),
                "PLAN_DEPENDENCY_UNKNOWN",
            ),
            "cycle": (
                _candidate([
                    _step(
                        "prepare",
                        inputs=[],
                        outputs=[],
                        dependencies=["annotate"],
                    ),
                    _step(
                        "annotate",
                        inputs=[],
                        outputs=[],
                        dependencies=["prepare"],
                    ),
                ]),
                "PLAN_DEPENDENCY_CYCLE",
            ),
        }

        for name, (candidate, expected_code) in cases.items():
            with self.subTest(name=name):
                ref_keys, usage = candidate_artifact_ref_keys(
                    candidate,
                    candidate_scope_id=f"candidate-scope-{name}-dependency",
                    authoritative_handles={},
                )
                issues = validate_candidate_graph_and_flow(
                    candidate,
                    ref_keys=ref_keys,
                    usage=usage,
                    availability_origins={},
                )
                self.assertIn(
                    expected_code,
                    {str(issue.get("code") or "") for issue in issues},
                )

    def test_invalid_materialization_policy_fails_closed(self) -> None:
        candidate = _candidate(
            [_step(
                "build-db",
                inputs=[],
                outputs=["plan.snpeff_database_dir"],
                dependencies=[],
            )],
            proposed_artifacts=[_plan_local_directory(
                policy_id="unregistered_materialization_policy",
            )],
        )

        with self.assertRaises(PlannerCandidateError) as caught:
            prepare_allocation_intent(
                candidate,
                candidate_scope_id="candidate-scope-invalid-policy",
                planning_epoch_id="planning-epoch-invalid-policy",
            )

        self.assertEqual(caught.exception.code, "ARTIFACT_POLICY_UNREGISTERED")

    def test_planner_prompt_and_plan_review_show_explicit_dataflow_rules(self) -> None:
        prompt = PromptManager.get_planner_v2_system_prompt()
        self.assertIn("dependencies only order steps", prompt)
        self.assertIn("produced by one\nreachable dependency", prompt)
        self.assertIn("plan.<task-specific-local-handle>", prompt)

        review = render_plan_review_content({
            "task_id": "review",
            "plan_id": "plan-review",
            "plan_version": "plan_v1",
            "steps": [_step("only-step", inputs=[], outputs=[], dependencies=[])],
        })
        self.assertIn("Cross-step Artifact Review", review)
        self.assertIn("mktemp, shell variables, cwd assumptions, or prose", review)

    def test_skill_contract_uses_controller_runtime_variable(self) -> None:
        document = _skill_document()
        planning_contract = document.split("## Planning Contract", 1)[1].split("##", 1)[0]
        self.assertIn('"$SNPEFF_JAR"', planning_contract)
        self.assertIn("Do not emit\n`{runtime:snpeff_jar}`", planning_contract)


if __name__ == "__main__":
    unittest.main()

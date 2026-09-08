from __future__ import annotations

import unittest
from pathlib import Path

from agent_core.plan_contract import (
    PlannerCandidateError,
    candidate_artifact_ref_keys,
    prepare_allocation_intent,
    structured_plan_candidate_diagnostics,
    validate_candidate_graph_and_flow,
)
from agent_core.step_effects import compile_plan_dataflow
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

    def test_split_plan_uses_mutable_workspace_without_immutable_output(self) -> None:
        workspace = _plan_local_directory("plan.snpeff_data_dir")
        workspace["artifact_contract"]["resource_kind"] = "mutable_workspace"
        workspace["resource_binding"] = {
            "canonical_resource_id": "artifact.snpeff_data_dir",
            "resource_kind": "mutable_workspace",
            "availability_origin": "plan_local",
            "contract_revision": "snpeff-workspace-v1",
        }
        build = _step("build-db", inputs=[], outputs=[], dependencies=[])
        build["workspace_mutations"] = [{
            "workspace_ref": "plan.snpeff_data_dir",
            "mutation": "create",
            "requires_available": False,
            "available_after_success": True,
            "establishes_state": "snpeff_database_ready",
            "contract_revision": "snpeff-workspace-v1",
        }]
        candidate = _candidate(
            [
                build,
                _step(
                    "annotate",
                    inputs=["plan.snpeff_data_dir"],
                    outputs=[],
                    dependencies=["build-db"],
                ),
            ],
            proposed_artifacts=[workspace],
        )
        scope = "candidate-scope-snpeff"
        compiled, _, issues = compile_plan_dataflow(
            candidate,
            candidate_scope_id=scope,
            authoritative_handles={},
            compile_effect_contracts=False,
        )
        self.assertEqual(issues, [])
        self.assertNotIn("plan.snpeff_data_dir", compiled["steps"][0]["outputs"])
        workspace_edges = [
            row for row in compiled["compiled_dataflow"]["edges"]
            if row["canonical_resource_id"] == "artifact.snpeff_data_dir"
        ]
        self.assertEqual(
            [row["effect_kind"] for row in workspace_edges],
            ["MUTATE", "READ"],
        )

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

    def test_multiple_immutable_artifact_producers_fail_closed(self) -> None:
        candidate = _candidate(
            [
                _step(
                    "write-report-a",
                    inputs=[],
                    outputs=["plan.immutable_report"],
                    dependencies=[],
                ),
                _step(
                    "write-report-b",
                    inputs=[],
                    outputs=["plan.immutable_report"],
                    dependencies=[],
                ),
                _step(
                    "consume-report",
                    inputs=["plan.immutable_report"],
                    outputs=[],
                    dependencies=["write-report-a", "write-report-b"],
                ),
            ],
            proposed_artifacts=[_plan_local_directory("plan.immutable_report")],
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

    def test_skill_contract_separates_runtime_paths_from_semantic_bindings(self) -> None:
        document = _skill_document()
        planning_contract = document.split("## Planning Contract", 1)[1].split("##", 1)[0]
        self.assertIn("Do not emit `$SNPEFF_JAR`", planning_contract)
        self.assertIn("produced or\nconsumed bindings", planning_contract)
        self.assertIn("binding_contract:", document)
        self.assertIn("produces_bindings: [genome_version, snpeff_db]", document)
        self.assertIn("consumes_bindings: [genome_version, snpeff_db]", document)


if __name__ == "__main__":
    unittest.main()

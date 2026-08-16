from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.plan_contract import (
    candidate_artifact_ref_keys,
    prepare_allocation_intent,
    promote_allocation_intent,
    rewrite_candidate_local_refs,
    validate_candidate_graph_and_flow,
)
from agent_core.plan_tracker import render_plan_review_content
from agent_core.skill_planning_contracts import (
    validate_selected_skill_planning_contracts,
)
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
        "semantic_constraint_ids": [],
    }


class SnpEffPlanDataflowContractTests(unittest.TestCase):
    def test_split_plan_without_shared_artifact_requests_planner_repair(self) -> None:
        plan = {
            "steps": [
                _step("build-db", inputs=["reference_fasta", "reference_gtf"], outputs=[], dependencies=[]),
                _step("annotate", inputs=["input_vcf"], outputs=["annotated_vcf"], dependencies=["build-db"]),
                _step("summarize", inputs=["annotated_vcf"], outputs=["summary"], dependencies=["annotate"]),
            ]
        }

        violations = validate_selected_skill_planning_contracts(
            plan,
            {SKILL_ID: _skill_document()},
        )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["code"], "PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED")
        self.assertEqual(violations[0]["owner"], "PLANNER")
        self.assertEqual(violations[0]["producer_step_id"], "build-db")
        self.assertEqual(violations[0]["consumer_step_id"], "annotate")
        self.assertIn("plan-local directory artifact", violations[0]["message"])

    def test_split_plan_reuses_one_allocated_directory_identity(self) -> None:
        candidate = {
            "outcome_type": "PLAN_CANDIDATE",
            "proposed_artifacts": [{
                "local_ref": "snpeff_database_dir",
                "semantic_view": {
                    "role": "snpeff_database_dir",
                    "format": "directory",
                    "description": "custom snpEff database",
                },
                "artifact_contract": {
                    "object_kind": "directory",
                    "lifecycle": "plan_local",
                    "materialization_policy_id": "managed_intermediate_v1",
                },
            }],
            "steps": [
                _step("build-db", inputs=[], outputs=["snpeff_database_dir"], dependencies=[]),
                _step("annotate", inputs=["snpeff_database_dir"], outputs=[], dependencies=["build-db"]),
            ],
            "semantic_claims": [],
            "validation_checklist": [{
                "check_id": "check-dataflow",
                "description": "verify explicit cross-step artifact dataflow",
            }],
            "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
        }
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
        self.assertEqual(producer_id, mapping["snpeff_database_dir"])
        self.assertEqual(bindings[producer_id]["object_kind"], "directory")
        self.assertEqual(bindings[producer_id]["lifecycle"], "plan_local")
        self.assertEqual(
            validate_selected_skill_planning_contracts(
                rewritten,
                {SKILL_ID: _skill_document()},
            ),
            [],
        )

    def test_composite_plan_needs_no_cross_step_artifact(self) -> None:
        plan = {"steps": [_step("build-and-annotate", inputs=[], outputs=["annotated_vcf"], dependencies=[])]}
        self.assertEqual(
            validate_selected_skill_planning_contracts(
                plan,
                {SKILL_ID: _skill_document()},
            ),
            [],
        )

    def test_planner_prompt_and_plan_review_show_explicit_dataflow_rules(self) -> None:
        prompt = PromptManager.get_planner_v2_prompt({"task": {}, "current_step": {}})
        self.assertIn("Dependencies only express ordering", prompt)
        self.assertIn("producer's `outputs` and the consumer's `inputs`", prompt)
        self.assertIn("`database_dir`", prompt)

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

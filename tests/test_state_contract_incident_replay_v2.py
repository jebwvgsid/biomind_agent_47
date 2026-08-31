from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_core.managed_artifacts import (
    build_managed_artifact_bindings,
    materialize_managed_action,
    validate_materialized_artifacts,
)
from agent_core.node_context import (
    build_node_context,
    control_metadata_leaks,
    resolve_plan_proposal_handles,
)
from agent_core.plan_tracker import (
    build_plan_state,
    load_plan_state,
    plan_contract_violations,
)
from agent_core.task_spec import (
    create_task_spec,
    merge_task_spec,
    semantic_constraints_from_task_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_FIXTURE = (
    PROJECT_ROOT
    / "benchmarks"
    / "biomind_agent_benchmark"
    / "runs"
    / "task_outputs"
    / "session_1786335039"
    / "plan_state.json"
)


def _managed_task() -> dict:
    return merge_task_spec(
        create_task_spec("Produce the required final FASTA", task_id="managed-aa"),
        {
            "inputs": [{
                "artifact_id": "artifact-source-fasta",
                "semantic_handle": "input.source_fasta",
                "role": "source_fasta",
                "canonical_path": "/data/source.fa",
                "format": "fasta",
                "required": True,
            }],
            "output_contracts": [{
                "artifact_id": "artifact-final-fasta",
                "semantic_handle": "output.final_fasta",
                "role": "final_fasta",
                "canonical_path": "/output/aa.fa",
                "format": "fasta",
                "required": True,
                "producer_step_id": "step-1",
                "validation_contract_id": "fasta-output-v1",
            }],
        },
        source="user_explicit",
        reason="managed_artifact_fixture",
    )


class ManagedArtifactEndToEndReplayTests(unittest.TestCase):
    def test_step_field_usage_allows_output_alias_as_later_input(self) -> None:
        task = merge_task_spec(
            create_task_spec("Build a reproducible report", task_id="case-a"),
            {
                "inputs": [{
                    "artifact_id": "artifact-source",
                    "semantic_handle": "input.source",
                    "role": "source",
                    "canonical_path": "/data/source.vcf",
                    "required": True,
                }],
                "output_contracts": [
                    {
                        "artifact_id": "artifact-reproduce-script",
                        "semantic_handle": "output.reproduce_script",
                        "role": "reproduce_script",
                        "canonical_path": "/out/reproduce.sh",
                        "required": True,
                    },
                    {
                        "artifact_id": "artifact-report",
                        "semantic_handle": "output.report",
                        "role": "report",
                        "canonical_path": "/out/report.md",
                        "required": True,
                    },
                ],
            },
            source="user_explicit",
            reason="plan_contract_case_a",
        )
        context = build_node_context(node_type="planner", task_spec=task)
        proposal = {
            "steps": [
                {
                    "step_id": "step-1", "title": "Write reproduce script",
                    "action": "execute", "purpose": "preparation", "tool": "python",
                    "inputs": ["input.source"], "outputs": ["output.reproduce_script"],
                    "dependencies": [], "success_criteria": ["script is non-empty"],
                    "skill_ids": [], "workflow_stage_ids": [], "batch_id": "",
                    "risk_level": "low", "semantic_constraint_ids": [],
                },
                {
                    "step_id": "step-2", "title": "Run script and write report",
                    "action": "execute", "purpose": "analysis", "tool": "bash",
                    "inputs": ["output.reproduce_script"], "outputs": ["output.report"],
                    "dependencies": ["step-1"], "success_criteria": ["report is non-empty"],
                    "skill_ids": [], "workflow_stage_ids": [], "batch_id": "",
                    "risk_level": "low", "semantic_constraint_ids": [],
                },
            ],
            "semantic_claims": [],
            "validation_checklist": [{"check_id": "check", "description": "outputs exist"}],
            "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
        }

        resolved = resolve_plan_proposal_handles(proposal, context.binding)
        self.assertEqual(
            resolved["steps"][0]["outputs"],
            ["artifact-reproduce-script"],
        )
        self.assertEqual(
            resolved["steps"][1]["inputs"],
            ["artifact-reproduce-script"],
        )
        plan = build_plan_state("case-a", json.dumps(resolved), strict=True)
        plan["current_task"] = {"task_spec": task, "task_revision": task["revision"]}
        plan["generated_from_task_revision"] = task["revision"]
        self.assertFalse(any(
            item.get("violation_code") == "ARTIFACT_ROLE_CONFLICT"
            for item in plan_contract_violations(plan)
        ))

    def test_plan_handle_generate_handle_and_controller_materialization_converge(self) -> None:
        task = _managed_task()
        planner_context = build_node_context(node_type="planner", task_spec=task)
        planner_prompt = planner_context.llm_payload.to_json()
        self.assertIn("output.final_fasta", planner_prompt)
        self.assertNotIn("/output/aa.fa", planner_prompt)

        proposal = {
            "steps": [{
                "step_id": "step-1",
                "title": "Produce final FASTA",
                "action": "execute",
                "purpose": "analysis",
                "tool": "tool",
                "inputs": ["input.source_fasta"],
                "outputs": ["output.final_fasta"],
                "dependencies": [],
                "success_criteria": ["output.final_fasta is a non-empty FASTA"],
                "skill_ids": [],
                "workflow_stage_ids": [],
                "batch_id": "",
                "risk_level": "low",
                "semantic_constraint_ids": [],
            }],
            "semantic_claims": [],
            "validation_checklist": [{
                "check_id": "check-output",
                "description": "validate output.final_fasta",
            }],
            "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
        }
        resolved = resolve_plan_proposal_handles(proposal, planner_context.binding)
        self.assertEqual(resolved["steps"][0]["inputs"], ["artifact-source-fasta"])
        self.assertEqual(resolved["steps"][0]["outputs"], ["artifact-final-fasta"])

        plan = build_plan_state("managed-aa", json.dumps(resolved), strict=True)
        plan["current_task"] = {"task_spec": task, "task_revision": task["revision"]}
        plan["generated_from_task_revision"] = task["revision"]
        self.assertFalse(
            any(
                item.get("code") in {
                    "UNKNOWN_INPUT_ARTIFACT_REFERENCE",
                    "UNKNOWN_OUTPUT_ARTIFACT_REFERENCE",
                    "MISSING_REQUIRED_OUTPUT_CONTRACT",
                }
                for item in plan_contract_violations(plan)
            )
        )

        generate_context = build_node_context(
            node_type="generate",
            task_spec=task,
            plan_state=plan,
            current_step_id="step-1",
        )
        generate_prompt = generate_context.llm_payload.to_json()
        self.assertIn("output.final_fasta", generate_prompt)
        self.assertIn("{output:final_fasta}", generate_prompt)
        self.assertNotIn("/output/aa.fa", generate_prompt)
        self.assertEqual(control_metadata_leaks(generate_context.llm_payload.to_dict()), [])

        bindings = build_managed_artifact_bindings(task)
        action, violations = materialize_managed_action(
            {
                "action_kind": "command",
                "argv": [
                    "tool",
                    "{input:source_fasta}",
                    "-o",
                    "{output:final_fasta}",
                ],
                "outputs": ["output.final_fasta"],
            },
            bindings,
        )
        self.assertEqual(violations, [])
        self.assertEqual(action["argv"], ["tool", "/data/source.fa", "-o", "/output/aa.fa"])
        self.assertEqual(validate_materialized_artifacts(action, bindings), [])


@unittest.skipUnless(SESSION_FIXTURE.exists(), "session_1786335039 fixture not installed")
class Session1786335039ReplayTests(unittest.TestCase):
    def test_legacy_state_migrates_once_and_recovery_keeps_required_semantics(self) -> None:
        legacy = json.loads(SESSION_FIXTURE.read_text(encoding="utf-8"))
        legacy_task = legacy.get("task_spec") or (legacy.get("task_state") or {}).get("task_spec")
        baseline_task_chars = len(json.dumps(legacy_task, ensure_ascii=False, sort_keys=True))

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "task_outputs" / "session_1786335039" / "plan_state.json"
            target.parent.mkdir(parents=True)
            target.write_bytes(SESSION_FIXTURE.read_bytes())

            migrated = load_plan_state(root, "session_1786335039")
            task = migrated["current_task"]["task_spec"]
            first_serialized = json.dumps(migrated, ensure_ascii=False, sort_keys=True)
            reloaded = load_plan_state(root, "session_1786335039")

            self.assertEqual(first_serialized, json.dumps(reloaded, ensure_ascii=False, sort_keys=True))
            self.assertNotIn("task_spec", migrated)
            self.assertNotIn("task_spec", migrated.get("task_state") or {})
            self.assertNotIn("request_envelope", task)
            self.assertNotIn("expected_outputs", task)
            self.assertNotIn("change_log", task)
            self.assertTrue(str(task.get("raw_submission_ref") or "").startswith("object:sha256:"))
            self.assertLess(
                len(json.dumps(task, ensure_ascii=False, sort_keys=True)),
                baseline_task_chars,
            )

            current_step_id = str(migrated.get("current_step_id") or "")
            recovery = build_node_context(
                node_type="recovery",
                task_spec=task,
                plan_state=migrated,
                current_step_id=current_step_id,
                evidence=[{
                    "evidence_id": "incident-execution-log",
                    "kind": "execution_log",
                    "excerpt": "snpEff custom database build failed",
                }],
                max_payload_chars=800_000,
            )
            payload = recovery.llm_payload.to_dict()
            rendered = recovery.llm_payload.to_json()
            constraint_handles = {
                str(item.get("handle") or "")
                for item in (payload.get("task") or {}).get("constraints", [])
            }
            required_keys = {
                str(item.get("semantic_key") or "")
                for item in semantic_constraints_from_task_spec(task)
                if str(item.get("semantic_key") or "").startswith("forbidden_behavior.")
            }
            required_keys.update({
                "required_input.variant_vcf",
                "required_input.reference_fasta",
                "required_input.annotation_gtf",
            })
            self.assertTrue(
                {"constraint." + key for key in required_keys}.issubset(constraint_handles)
            )
            self.assertIn("input.variant_vcf", rendered)
            self.assertIn("input.reference_fasta", rendered)
            self.assertIn("input.annotation_gtf", rendered)
            self.assertIn('"handle": "E1"', rendered)
            self.assertNotIn("/public/home/liusiqi", rendered)
            self.assertEqual(control_metadata_leaks(payload), [])
            self.assertLess(len(rendered), baseline_task_chars)


if __name__ == "__main__":
    unittest.main()

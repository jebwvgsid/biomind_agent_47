from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_core import plan_tracker as plan_tracker_module
from agent_core.completion_gate import CompletionGate
from agent_core.managed_artifacts import (
    build_managed_artifact_bindings,
    materialize_managed_action,
    validate_generate_output_coverage,
)
from agent_core.plan_contract import (
    planning_epoch_matches,
    start_planning_epoch,
)
from agent_core.plan_tracker import (
    build_plan_state,
    derive_plan_validation_state,
    ensure_provenance_state,
    normalize_plan_contracts,
    normalize_task_output_materializations,
    validation_issue,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import (
    effective_output_contracts,
    ensure_task_spec,
    validate_task_spec_output_contracts,
)


def _task_spec(output_dir: str = "/tmp/generate-only-v2") -> dict:
    return ensure_task_spec({
        "task_id": "generate-only-v2",
        "goal": "annotate variants",
        "output_contracts": [
            {
                "output_id": "output_dir",
                "role": "output_dir",
                "artifact_kind": "directory",
                "path": output_dir,
                "required": True,
            },
            *[
                {
                    "output_id": output_id,
                    "role": output_id,
                    "artifact_kind": "file",
                    "root_ref": "output_dir",
                    "path_policy": "planner_materialized",
                    "required": True,
                }
                for output_id in (
                    "annotated_vcf",
                    "ann_summary",
                    "report",
                    "reproduce_script",
                )
            ],
        ],
    })


def _strict_plan() -> dict:
    candidate = {
        "steps": [{
            "step_id": "step-1",
            "title": "Annotate and report",
            "action": "execute",
            "purpose": "analysis",
            "tool": "snpEff",
            "inputs": [],
            "outputs": [
                "annotated_vcf",
                "ann_summary",
                "report",
                "reproduce_script",
            ],
            "dependencies": [],
            "success_criteria": ["all declared outputs pass validation"],
            "skill_ids": ["genomics/snpeff-annotation"],
            "workflow_stage_ids": [],
            "batch_id": "",
            "risk_level": "low",
            "semantic_constraint_ids": [],
        }],
        "semantic_claims": [],
        "validation_checklist": [{
            "check_id": "outputs",
            "description": "validate all task outputs",
        }],
        "metadata": {
            "schema_version": 1,
            "planner_contract": "structured_plan_v1",
        },
        "task_output_materializations": [
            {"output_ref": "annotated_vcf", "path": "annotated.vcf"},
            {"output_ref": "ann_summary", "path": "ann_summary.tsv"},
            {"output_ref": "report", "path": "report.md"},
            {"output_ref": "reproduce_script", "path": "reproduce.sh"},
        ],
    }
    return build_plan_state("generate-only-v2", json.dumps(candidate), strict=True)


class GenerateOnlyOutputContractV2Tests(unittest.TestCase):
    def test_pathless_required_file_keeps_file_semantics(self):
        contracts = effective_output_contracts(_task_spec())
        report = next(row for row in contracts if row["output_id"] == "report")

        self.assertEqual(report["artifact_kind"], "file")
        self.assertEqual(report["root_ref"], "output_dir")
        self.assertEqual(report["path_policy"], "planner_materialized")
        self.assertNotEqual(report.get("format"), "directory")

    def test_finish_rejects_typed_output_dir_that_is_not_canonicalized(self):
        candidate = ensure_task_spec({
            "task_id": "missing-output-dir-binding",
            "goal": "annotate",
            "requirements": [{
                "requirement_id": "c-output-dir",
                "semantic_key": "required_output.output_dir",
                "operator": "eq",
                "value": "/tmp/explicit-result",
                "strength": "hard",
                "source_ref": "U1",
            }],
            "output_contracts": [{
                "output_id": "report",
                "role": "report",
                "artifact_kind": "file",
                "required": True,
            }],
        })

        codes = {row["code"] for row in validate_task_spec_output_contracts(candidate)}

        self.assertIn("TASK_OUTPUT_DIR_NOT_CANONICALIZED", codes)
        self.assertIn("TASK_OUTPUT_BINDING_INCOMPLETE", codes)

    def test_finish_accepts_typed_output_dir_and_planner_materialization_policy(self):
        self.assertEqual(validate_task_spec_output_contracts(_task_spec()), [])

    def test_planner_materializes_unspecified_filename_under_output_dir(self):
        output_dir = "/tmp/planner-materialized-output"
        state = {"task_output_materializations": [
            {"output_ref": "annotated_vcf", "path": "annotated.vcf"},
            {"output_ref": "ann_summary", "path": "ann_summary.tsv"},
            {"output_ref": "report", "path": "report.md"},
            {"output_ref": "reproduce_script", "path": "reproduce.sh"},
        ]}

        normalized, issues = normalize_task_output_materializations(
            state,
            _task_spec(output_dir),
        )

        self.assertEqual(issues, [])
        report_materialization = next(
            row for row in normalized["task_output_materializations"]
            if row["output_id"] == "report"
        )
        self.assertEqual(
            report_materialization["canonical_path"],
            f"{output_dir}/report.md",
        )
        report = next(
            row for row in effective_output_contracts(_task_spec(output_dir))
            if row["output_id"] == "report"
        )
        self.assertFalse(report.get("filename"))
        self.assertEqual(report["path_policy"], "planner_materialized")

    def test_v2_normalization_never_routes_through_or_rewrites_for_renderer(self):
        state = _strict_plan()
        state["task_spec"] = _task_spec()
        before = list(state["steps"][0]["outputs"])
        fake_renderer = {
            "skill_id": "genomics/snpeff-annotation",
            "owned_outputs": [{"output_id": "annotated_vcf"}],
            "output_bindings": {"annotated_vcf": "output_vcf"},
        }

        normalized, audits, _violations = normalize_plan_contracts(
            state,
            renderer_records=[fake_renderer],
            output_contracts=effective_output_contracts(_task_spec()),
            task_spec=_task_spec(),
        )

        self.assertEqual(normalized["steps"][0]["outputs"], before)
        self.assertFalse(normalized["steps"][0].get("renderer_composite", False))
        self.assertFalse(any(row.get("renderer_skill_id") for row in audits))

    def test_generate_coverage_is_owned_by_generated_action(self):
        task_spec = _task_spec()
        plan_state = _strict_plan()
        plan_state["task_spec"] = task_spec
        bindings = build_managed_artifact_bindings(task_spec, plan_state=plan_state)
        action, materialization_issues = materialize_managed_action({
            "action_type": "execute",
            "argv": ["run-annotation", "{output:annotated_vcf}", "{output:report}", "{output:reproduce_script}"],
            "outputs": ["output.annotated_vcf", "output.report", "output.reproduce_script"],
            "produced_outputs": [
                {"output_id": "annotated_vcf", "path": "{output:annotated_vcf}"},
                {"output_id": "report", "path": "{output:report}"},
                {"output_id": "reproduce_script", "path": "{output:reproduce_script}"},
            ],
        }, bindings)
        self.assertFalse(materialization_issues)

        issues = validate_generate_output_coverage(plan_state, action, bindings)

        self.assertEqual([row.code for row in issues], ["GENERATE_OUTPUT_COVERAGE_INCOMPLETE"])
        self.assertEqual(issues[0].owner, "GENERATED_ACTION")
        self.assertEqual(issues[0].route, "generate")
        self.assertIn("ann_summary", issues[0].message)

    def test_llm_authored_produced_output_identity_is_ignored(self):
        task_spec = _task_spec()
        plan_state = _strict_plan()
        plan_state["task_spec"] = task_spec
        bindings = build_managed_artifact_bindings(task_spec, plan_state=plan_state)

        _action, issues = materialize_managed_action({
            "action_type": "execute",
            "argv": ["write-report", "{output:report}"],
            "produced_outputs": [{
                "output_id": "output.report",
                "path": "/tmp/wrong-name.md",
            }],
        }, bindings)

        self.assertEqual(issues, [])
        self.assertNotIn("produced_outputs", _action)
        self.assertEqual(_action["referenced_output_handles"], ["output.report"])

    def test_renderer_declared_skills_are_generate_owned_in_v2(self):
        manager = SkillManager(str(Path(__file__).resolve().parents[1] / "skills"))

        capabilities, _operations = manager.get_semantic_execution_capabilities([
            "genomics/snpeff-annotation",
            "genomics/wgs-variant-calling",
        ])

        self.assertEqual(
            {row["skill_id"]: row["action_mode"] for row in capabilities},
            {
                "genomics/snpeff-annotation": "managed_generate",
                "genomics/wgs-variant-calling": "managed_generate",
            },
        )
        rendered = str(capabilities)
        self.assertNotIn("renderer_arguments", rendered)
        self.assertNotIn("renderer_id", rendered)
        snpeff = next(row for row in capabilities if row["skill_id"] == "genomics/snpeff-annotation")
        self.assertEqual(
            [row["output_id"] for row in snpeff["output_capabilities"]],
            ["annotated_vcf", "ann_summary", "report", "reproduce_script"],
        )

    def test_materialization_only_plan_patch_protocol_is_retired(self):
        self.assertFalse(hasattr(plan_tracker_module, "apply_plan_patch"))

    def test_planner_cannot_rename_an_exact_user_output(self):
        task_spec = ensure_task_spec({
            "task_id": "exact-user-path",
            "goal": "write report",
            "output_contracts": [
                {
                    "output_id": "output_dir",
                    "role": "output_dir",
                    "artifact_kind": "directory",
                    "path": "/tmp/exact-user-path",
                    "required": True,
                },
                {
                    "output_id": "report",
                    "role": "report",
                    "artifact_kind": "file",
                    "path": "/tmp/exact-user-path/report.md",
                    "required": True,
                },
            ],
        })
        state = {"task_output_materializations": [
            {"output_ref": "report", "path": "renamed-report.md"},
        ]}

        _normalized, issues = normalize_task_output_materializations(state, task_spec)

        self.assertEqual([row["code"] for row in issues], ["USER_OUTPUT_PATH_NOT_PRESERVED"])
        issue = issues[0]
        self.assertEqual(
            issue["field_path"],
            "$.task_output_materializations[0].path",
        )
        self.assertEqual(issue["affected_output"]["output_id"], "report")
        self.assertEqual(
            issue["expected_identity"]["required_basename"],
            "report.md",
        )
        self.assertEqual(
            issue["observed_identity"]["candidate_path"],
            "renamed-report.md",
        )
        self.assertIn("machine_evidence", issue)

    def test_provenance_keeps_output_id_separate_from_version_identity(self):
        task_spec = _task_spec()
        state = _strict_plan()
        state["task_spec"] = task_spec
        state, _audits, violations = normalize_plan_contracts(
            state,
            renderer_records=[],
            output_contracts=effective_output_contracts(task_spec),
            task_spec=task_spec,
        )
        self.assertFalse(violations)

        provenance = ensure_provenance_state(state)
        records = list(provenance["artifacts"].values())

        self.assertEqual(
            {row["output_id"] for row in records},
            {"annotated_vcf", "ann_summary", "report", "reproduce_script"},
        )
        self.assertTrue(all(row["artifact_id"] != row["output_id"] for row in records))

    def test_planning_epoch_is_not_reset_by_a_rejected_candidate_identity(self):
        epoch = start_planning_epoch(
            task_revision=3,
            task_semantic_identity="task-sha",
            base_plan_revision=2,
            base_plan_snapshot_id="committed-snapshot",
            base_plan_semantic_identity="committed-plan-sha",
            planner_feedback_revision=7,
        )

        self.assertTrue(planning_epoch_matches(
            epoch,
            task_semantic_identity="task-sha",
            base_plan_snapshot_id="committed-snapshot",
            base_plan_semantic_identity="committed-plan-sha",
            planner_feedback_revision=7,
        ))
        self.assertFalse(planning_epoch_matches(
            epoch,
            task_semantic_identity="task-sha",
            base_plan_snapshot_id="rejected-candidate-snapshot",
            base_plan_semantic_identity="rejected-candidate-sha",
            planner_feedback_revision=7,
        ))
        self.assertFalse(planning_epoch_matches(
            epoch,
            task_semantic_identity="task-sha",
            base_plan_snapshot_id="committed-snapshot",
            base_plan_semantic_identity="committed-plan-sha",
            planner_feedback_revision=8,
        ))

    def test_repeated_candidate_uses_exact_issue_convergence_not_internal_conflict(self):
        issue_a = validation_issue(
            "VALIDATION_ERROR",
            owner="planner",
            step_id="step-1",
            field_path="$.steps[0].inputs[0]",
            message="first exact issue",
        )
        issue_b = validation_issue(
            "VALIDATION_ERROR",
            owner="planner",
            step_id="step-4",
            field_path="$.steps[3].inputs[0]",
            message="different issue with the same generic code",
        )
        first = derive_plan_validation_state(_strict_plan(), [issue_a])
        repeated = derive_plan_validation_state(first, [issue_a])
        changed = derive_plan_validation_state(first, [issue_b])

        self.assertEqual(
            repeated["repair_convergence"]["status"],
            "blocked_non_convergent",
        )
        self.assertEqual(repeated["repair_convergence"]["cycle_type"], "A_TO_A")
        self.assertEqual(changed["repair_convergence"]["status"], "converging")

    def test_final_completion_uses_task_and_plan_outputs_and_ignores_renderer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "results"
            output_dir.mkdir()
            task_spec = _task_spec(str(output_dir))
            state = _strict_plan()
            state["task_spec"] = task_spec
            state, _audits, violations = normalize_plan_contracts(
                state,
                renderer_records=[],
                output_contracts=effective_output_contracts(task_spec),
                task_spec=task_spec,
            )
            self.assertFalse(violations)
            for row in state["task_output_materializations"]:
                path = Path(row["canonical_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"generated {row['output_id']}\n", encoding="utf-8")
            for step in state["steps"]:
                step["status"] = "done"
            state["created_at"] = "2000-01-01T00:00:00+00:00"
            state["steps"][0]["renderer_execution"] = {
                "manifest_path": str(output_dir / "missing-renderer-manifest.json"),
                "remaining_action_ids": ["legacy-action"],
            }

            result = CompletionGate.validate_final(tmpdir, state["task_id"], state)

        self.assertTrue(result.passed, result.feedback)
        self.assertEqual(len(result.checked_artifacts), 4)

    def test_v2_completion_does_not_invent_a_generic_report_requirement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_spec = ensure_task_spec({
                "task_id": "no-generic-report",
                "goal": "perform a managed task with no file output",
                "output_contracts": [{
                    "output_id": "output_dir",
                    "role": "output_dir",
                    "artifact_kind": "directory",
                    "path": tmpdir,
                    "required": True,
                }],
            })
            candidate = {
                "steps": [{
                    "step_id": "step-1",
                    "title": "Complete managed operation",
                    "action": "execute",
                    "purpose": "analysis",
                    "tool": "managed-shell",
                    "inputs": [],
                    "outputs": [],
                    "dependencies": [],
                    "success_criteria": ["operation completes"],
                    "skill_ids": [],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                    "semantic_constraint_ids": [],
                }],
                "semantic_claims": [],
                "validation_checklist": [{
                    "check_id": "operation",
                    "description": "operation completed",
                }],
                "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
            }
            state = build_plan_state("no-generic-report", json.dumps(candidate), strict=True)
            state["task_spec"] = task_spec
            state, _audits, violations = normalize_plan_contracts(
                state,
                renderer_records=[],
                output_contracts=effective_output_contracts(task_spec),
                task_spec=task_spec,
            )
            self.assertFalse(violations)
            state["steps"][0]["status"] = "done"

            result = CompletionGate.validate_final(tmpdir, state["task_id"], state)

        self.assertTrue(result.passed, result.feedback)
        self.assertEqual(result.checked_artifacts, [])


if __name__ == "__main__":
    unittest.main()

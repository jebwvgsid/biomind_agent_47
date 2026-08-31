from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_core.completion_gate import CompletionGate
from agent_core.managed_artifacts import (
    PHYSICAL_HARD,
    PHYSICAL_IMPLEMENTATION_DETAIL,
    build_approved_visible_artifact_set,
    build_managed_artifact_bindings,
    capture_managed_output_snapshot,
    discover_current_attempt_outputs,
    materialize_managed_action,
    normalize_bash_positional_argv,
    physical_constraint_origin,
    reconcile_runtime_outputs,
    validate_generate_output_coverage,
)
from agent_core.node_context import build_node_context
from agent_core.plan_tracker import (
    build_plan_state,
    ensure_provenance_state,
    record_provenance_event,
    record_runtime_output_failure,
)


ATTEMPT = "attempt-current"


def _contract(
    expected_path: str,
    *,
    origin: str = PHYSICAL_IMPLEMENTATION_DETAIL,
    accepted: tuple[str, ...] = (),
) -> dict:
    return {
        "artifact_id": "logical-reference",
        "semantic_handle": "output.reference_fasta",
        "expected_path": expected_path,
        "artifact_kind": "file",
        "format": "fasta",
        "validation_contract_id": "nonempty-file-v1",
        "required": True,
        "constraint_origin": origin,
        "accepted_representations": list(accepted),
    }


def _candidate(path: str, *, attempt_id: str = ATTEMPT, representation: str = "plain") -> dict:
    return {
        "path": path,
        "attempt_id": attempt_id,
        "current_attempt_owned": True,
        "artifact_kind": "file",
        "format": "fasta",
        "representation": representation,
        "content_valid": True,
        "size": 10,
        "mtime_ns": 1,
    }


class GenerateContractSimplificationTests(unittest.TestCase):
    def test_exact_semantic_handle_derives_coverage_without_duplicate_identity(self) -> None:
        task = {
            "inputs": [],
            "output_contracts": [{
                "artifact_id": "artifact-internal-1",
                "output_id": "canonical-output-2",
                "role": "reference_fasta",
                "path": "/tmp/reference.fa",
                "format": "fasta",
                "required": True,
            }],
        }
        plan = {
            "current_step_id": "step-1",
            "steps": [{"step_id": "step-1", "outputs": ["artifact-internal-1"]}],
        }
        bindings = build_managed_artifact_bindings(task, plan_state=plan)

        action, issues = materialize_managed_action({
            "type": "execute",
            "argv": ["tool", "{output:reference_fasta}"],
            "produced_outputs": [{
                "output_id": "output.reference_fasta",
                "path": "/wrong/model/path.fa",
            }],
        }, bindings)

        self.assertEqual(issues, [])
        self.assertEqual(validate_generate_output_coverage(plan, action, bindings), [])
        self.assertNotIn("produced_outputs", action)
        self.assertEqual(action["referenced_output_handles"], ["output.reference_fasta"])
        self.assertEqual(bindings["output.reference_fasta"].artifact_id, "artifact-internal-1")
        self.assertEqual(
            bindings["output.reference_fasta"].metadata["output_id"],
            "canonical-output-2",
        )

    def test_exact_visible_handle_without_binding_is_internal_protocol(self) -> None:
        visible = {
            "logical-reference": {
                "artifact_id": "logical-reference",
                "direction": "output",
                "aliases": ["output.reference_fasta"],
            }
        }
        _, issues = materialize_managed_action(
            {"argv": ["tool", "{output:reference_fasta}"]},
            {},
            approved_visible_artifacts=visible,
        )
        self.assertEqual(issues[0].code, "PLAN_ARTIFACT_BINDING_PROJECTION_MISSING")
        self.assertEqual(issues[0].owner, "INTERNAL_PROTOCOL")


class RuntimeOutputReconciliationTests(unittest.TestCase):
    def test_exact_semantic_text_format_is_not_missing_when_detector_is_coarse(self) -> None:
        contract = _contract("/managed/settings.conf", origin=PHYSICAL_HARD)
        contract["format"] = "config"
        result = reconcile_runtime_outputs(
            [contract],
            [{**_candidate("/managed/settings.conf"), "format": "text"}],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["bindings"][0]["format_compatibility"], "unproven")
        self.assertNotIn(
            "OUTPUT_MISSING",
            {item["code"] for item in result["violations"]},
        )

    def test_absent_current_attempt_artifact_remains_output_missing(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["violations"][0]["code"], "OUTPUT_MISSING")

    def test_exact_provably_wrong_format_is_mismatch_not_missing(self) -> None:
        contract = _contract("/managed/result.json", origin=PHYSICAL_HARD)
        contract["format"] = "json"
        result = reconcile_runtime_outputs(
            [contract],
            [{**_candidate("/managed/result.json"), "format": "vcf"}],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["status"], "recovery")
        self.assertEqual(
            result["violations"][0]["code"],
            "OUTPUT_REALIZATION_MISMATCH",
        )
        self.assertNotEqual(result["violations"][0]["code"], "OUTPUT_MISSING")

    def test_exact_invalid_content_is_invalid_not_missing(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [{**_candidate("/managed/reference.fa"), "content_valid": False}],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(
            result["violations"][0]["code"],
            "OBSERVED_ARTIFACT_CONTENT_INVALID",
        )

    def test_exact_markdown_path_accepts_plain_text_representation(self) -> None:
        contract = _contract("/managed/report.md", origin=PHYSICAL_HARD)
        contract["format"] = "markdown"
        result = reconcile_runtime_outputs(
            [contract],
            [{**_candidate("/managed/report.md"), "format": "text"}],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["bindings"][0]["realization"], "expected")

    def test_soft_fasta_gzip_realization_rebinds_when_downstream_accepts_it(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa", accepted=("gzip",))],
            [_candidate("/managed/reference.fa.gz", representation="gzip")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["bindings"][0]["realization"], "reconciled")
        self.assertEqual(result["bindings"][0]["observed_path"], "/managed/reference.fa.gz")

    def test_hard_name_mismatch_requires_normalization(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa", origin=PHYSICAL_HARD, accepted=("gzip",))],
            [_candidate("/managed/reference.fa.gz", representation="gzip")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["status"], "recovery")
        self.assertEqual(result["violations"][0]["code"], "OUTPUT_REALIZATION_MISMATCH")
        self.assertEqual(result["violations"][0]["constraint_origin"], PHYSICAL_HARD)

    def test_successful_tool_with_name_change_is_not_tool_failed(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa", accepted=("gzip",))],
            [_candidate("/managed/reference.fa.gz", representation="gzip")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertNotIn(
            "TOOL_EXECUTION_FAILED",
            {item["code"] for item in result["violations"]},
        )

    def test_nonzero_exit_never_adopts_partial_candidate(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [_candidate("/managed/reference.fa")],
            attempt_id=ATTEMPT,
            exit_code=2,
        )
        self.assertEqual(result["bindings"], [])
        self.assertEqual(result["violations"][0]["code"], "TOOL_EXECUTION_FAILED")

    def test_multiple_candidates_are_ambiguous_without_filename_fuzzy_matching(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [_candidate("/managed/A"), _candidate("/managed/B")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["violations"][0]["code"], "AMBIGUOUS_OUTPUT_REALIZATION")

    def test_stale_previous_attempt_file_cannot_be_claimed(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [_candidate("/managed/reference.fa", attempt_id="attempt-old")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["violations"][0]["code"], "OUTPUT_MISSING")

    def test_downstream_incompatible_compression_requires_recovery(self) -> None:
        result = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [_candidate("/managed/reference.fa.gz", representation="gzip")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(result["violations"][0]["code"], "OUTPUT_REALIZATION_MISMATCH")
        self.assertFalse(result["violations"][0]["downstream_compatible"])

    def test_changed_runtime_evidence_changes_convergence_fingerprint(self) -> None:
        first = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        same = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa")],
            [],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        changed = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa", accepted=("gzip",))],
            [_candidate("/managed/reference.fa.gz", representation="gzip")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        self.assertEqual(first["fingerprint"], same["fingerprint"])
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])

    def test_runtime_mismatch_persists_typed_failure_without_flattening(self) -> None:
        state: dict = {"failure": {}, "task_state": {}, "steps": []}
        reconciliation = reconcile_runtime_outputs(
            [_contract("/managed/reference.fa", origin=PHYSICAL_HARD)],
            [_candidate("/managed/reference.fa.gz", representation="gzip")],
            attempt_id=ATTEMPT,
            exit_code=0,
        )
        failure = record_runtime_output_failure(
            state,
            reconciliation,
            step_id="step-1",
            attempt_id=ATTEMPT,
        )
        self.assertEqual(failure["error_code"], "OUTPUT_REALIZATION_MISMATCH")
        self.assertEqual(failure["owner"], "ARTIFACT_CONTRACT")
        self.assertEqual(failure["stage"], "post_execution_reconciliation")
        self.assertEqual(failure["logical_artifact"], "logical-reference")
        self.assertNotIn("TASK_BLOCKED", json.dumps(failure))

    def test_planner_materialized_name_is_soft_but_user_exact_name_is_hard(self) -> None:
        soft = physical_constraint_origin({
            "path_policy": "planner_materialized",
            "source_type": "user_explicit",
            "canonical_path": "/result/planner-name.fa",
        })
        hard = physical_constraint_origin({
            "path_policy": "managed_exact",
            "source_type": "user_explicit",
            "canonical_path": "/result/user-name.fa",
        })
        self.assertEqual(soft, PHYSICAL_IMPLEMENTATION_DETAIL)
        self.assertEqual(hard, PHYSICAL_HARD)

    def test_bash_lc_positional_arguments_receive_zero_sentinel(self) -> None:
        argv = normalize_bash_positional_argv([
            "bash", "-lc", 'printf "%s|%s|%s|%s" "$0" "$1" "$2" "$3"',
            "db", "fasta", "gtf",
        ])
        self.assertEqual(argv[3], "_")
        completed = subprocess.run(argv, check=True, capture_output=True, text=True)
        self.assertTrue(completed.stdout.endswith("_|db|fasta|gtf"), completed.stdout)


class RuntimeReconciliationIntegrationTests(unittest.TestCase):
    def test_plan_generate_execute_reconcile_downstream_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            expected = root / "reference.fa"
            observed = root / "reference.actual"
            task = {
                "task_id": "reconcile-integration",
                "inputs": [],
                "output_contracts": [{
                    "artifact_id": "logical-reference",
                    "output_id": "controller-output-id",
                    "role": "reference_fasta",
                    "artifact_kind": "file",
                    "format": "fasta",
                    "path_policy": "planner_materialized",
                    "required": True,
                    "source_type": "user_explicit",
                    "accepted_representations": ["gzip"],
                }],
            }
            candidate = {
                "steps": [{
                    "step_id": "step-1",
                    "title": "produce logical reference",
                    "action": "execute",
                    "purpose": "analysis",
                    "tool": "test-tool",
                    "inputs": [],
                    "outputs": ["logical-reference"],
                    "dependencies": [],
                    "success_criteria": ["logical reference is valid"],
                    "skill_ids": [],
                    "workflow_stage_ids": [],
                    "batch_id": "",
                    "risk_level": "low",
                    "semantic_constraint_ids": [],
                }],
                "semantic_claims": [],
                "validation_checklist": [],
                "metadata": {"schema_version": 1, "planner_contract": "structured_plan_v1"},
                "task_output_materializations": [{
                    "output_ref": "logical-reference",
                    "path": expected.name,
                }],
            }
            state = build_plan_state(
                "reconcile-integration",
                json.dumps(candidate),
                strict=True,
            )
            state["current_task"] = {"task_spec": task}
            state["task_spec"] = task
            state["schema_version"] = 9
            state["compatibility"] = {
                "source_schema_version": 9,
                "read_aliases_enabled": False,
            }
            state["current_step_id"] = "step-1"
            state["steps"][0]["status"] = "running"
            state["task_output_materializations"] = [{
                "output_ref": "logical-reference",
                "output_id": "controller-output-id",
                "path": expected.name,
                "canonical_path": str(expected),
                "origin": "planner",
            }]
            ensure_provenance_state(state)

            context = build_node_context(
                node_type="generate",
                task_spec=task,
                plan_state=state,
                current_step_id="step-1",
            )
            self.assertIn(
                "output.reference_fasta",
                {item["handle"] for item in context.llm_payload.to_dict()["task"]["outputs"]},
            )

            bindings = build_managed_artifact_bindings(task, plan_state=state)
            action, issues = materialize_managed_action({
                "type": "execute",
                "argv": ["implicit-fasta-writer"],
                "outputs": ["output.reference_fasta"],
            }, bindings)
            self.assertEqual(issues, [])
            self.assertEqual(validate_generate_output_coverage(state, action, bindings), [])
            contracts = action["controller_output_bindings"]

            before = capture_managed_output_snapshot(contracts)
            with gzip.open(observed, "wt", encoding="utf-8") as handle:
                handle.write(">chr1\nACGT\n")
            after = capture_managed_output_snapshot(contracts)
            candidates = discover_current_attempt_outputs(before, after, attempt_id=ATTEMPT)
            reconciliation = reconcile_runtime_outputs(
                contracts,
                candidates,
                attempt_id=ATTEMPT,
                exit_code=0,
            )
            self.assertEqual(reconciliation["status"], "accepted")

            action["action_id"] = "action-integration"
            action["step_id"] = "step-1"
            record_provenance_event(state, "action_bound", {"action": action})
            attempt = record_provenance_event(state, "attempt_started", {
                "action_id": "action-integration",
                "attempt_id": ATTEMPT,
                "step_id": "step-1",
            })
            provenance_result = record_provenance_event(state, "execution_finished", {
                "attempt_id": attempt["attempt_id"],
                "execution_record": {
                    "execution_id": "execution-integration",
                    "returncode": 0,
                    "command_role": "primary",
                    "purpose": "analysis",
                    "produced_artifacts": [str(observed)],
                    "reconciled_outputs": reconciliation["bindings"],
                },
            })
            record_provenance_event(state, "validation_recorded", {
                "attempt_id": ATTEMPT,
                "validation": {"validation_id": "validation-integration", "status": "valid"},
            })

            self.assertTrue(
                provenance_result["artifact_version_ids"],
                json.dumps({
                    "step": state["steps"][0],
                    "reconciliation": reconciliation,
                    "provenance": state["provenance_state"],
                }, default=str, sort_keys=True),
            )
            step_evidence = CompletionGate.validate_step_evidence(state, "step-1")
            self.assertTrue(
                step_evidence.completion_satisfied,
                json.dumps({
                    "evidence": step_evidence.to_dict(),
                    "artifacts": state["provenance_state"]["artifacts"],
                }, default=str, sort_keys=True),
            )
            gate = CompletionGate.validate_current_step(root, "reconcile-integration", state)
            self.assertTrue(gate.passed, gate.to_dict())
            version_id = provenance_result["artifact_version_ids"][0]
            self.assertEqual(
                state["provenance_state"]["artifacts"][version_id]["path"],
                str(observed),
            )

            downstream_snapshot = {
                "plan_snapshot_id": "snapshot-downstream",
                "steps": [{
                    "step_id": "step-2",
                    "inputs": ["logical-reference"],
                    "outputs": [],
                }],
                "resolved_artifact_refs": [{
                    "step_id": "step-2",
                    "relation": "consume",
                    "field": "inputs",
                    "artifact_ref": {"kind": "authoritative", "identity": "logical-reference"},
                }],
            }
            visible = build_approved_visible_artifact_set(
                task,
                plan_state=state,
                approved_plan_snapshot=downstream_snapshot,
                current_step_id="step-2",
            )
            downstream = build_managed_artifact_bindings(
                task,
                plan_state=state,
                approved_plan_snapshot=downstream_snapshot,
                current_step_id="step-2",
                visible_artifacts=visible,
            )
            self.assertEqual(
                downstream["input.reference_fasta"].canonical_path,
                str(observed),
            )


if __name__ == "__main__":
    unittest.main()

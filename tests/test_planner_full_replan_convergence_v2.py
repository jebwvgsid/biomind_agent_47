from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy

from agent_core import plan_tracker as plan_tracker_module
from agent_core.node_context import (
    build_node_context,
    resolve_plan_proposal_handles,
)
from agent_core.plan_contract import (
    PlannerEnvelopeError,
    parse_planner_outcome_envelope,
    planning_epoch_matches,
    prepare_allocation_intent,
    start_planning_epoch,
    validate_planner_candidate_deterministically,
)
from agent_core.plan_tracker import (
    build_plan_state,
    carry_forward_plan_lifecycle,
    current_planning_failure,
    full_replan_transition_issues,
    load_plan_state,
    normalize_plan_contracts,
    record_planning_failure,
    resolve_planning_failure,
    save_plan_state,
    supersede_planning_failure,
    terminalize_planning_failure,
    validate_output_contracts,
)
from agent_core.task_spec import (
    create_task_spec,
    merge_task_spec,
    task_semantic_hash,
)
from prompts.manager import PromptManager


def _task() -> dict:
    return merge_task_spec(
        create_task_spec("Annotate the admitted VCF", task_id="planner-v2"),
        {
            "inputs": [{
                "artifact_id": "artifact-input-vcf",
                "input_id": "variant_vcf",
                "semantic_handle": "input.variant_vcf",
                "role": "variant_vcf",
                "format": "vcf",
                "path": "/data/input.vcf",
                "required": True,
            }],
            "output_contracts": [{
                "artifact_id": "artifact-annotated-vcf",
                "output_id": "annotated_vcf",
                "semantic_handle": "output.annotated_vcf",
                "role": "annotated_vcf",
                "artifact_kind": "file",
                "format": "vcf.gz",
                "path_policy": "planner_materialized",
                "root_ref": "output_dir",
                "producer_owner": "planner",
                "required_basename": "annotated.vcf.gz",
                "alternative_group": "primary_annotation",
                "required": True,
                "description": "annotated variant file",
            }],
        },
        source="user_explicit",
        reason="planner_v2_test",
    )


def _step(*, title: str = "Annotate", output: str = "artifact-annotated-vcf") -> dict:
    return {
        "step_id": "annotate",
        "title": title,
        "action": "execute",
        "purpose": "analysis",
        "tool": "genomics/snpeff-annotation",
        "inputs": ["artifact-input-vcf"],
        "outputs": [output],
        "dependencies": [],
        "success_criteria": ["task-specific result is valid"],
        "skill_ids": ["genomics/snpeff-annotation"],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
    }


def _candidate(*, proposed_artifacts=None) -> dict:
    value = {
        "steps": [{
            "step_id": "annotate",
            "title": "Annotate",
            "action": "execute",
            "purpose": "analysis",
            "tool": "genomics/snpeff-annotation",
            "inputs": ["input.variant_vcf"],
            "outputs": ["output.annotated_vcf"],
            "dependencies": [],
            "success_criteria": ["task-specific result is valid"],
            "skill_ids": ["genomics/snpeff-annotation"],
            "workflow_stage_ids": [],
            "batch_id": "",
            "risk_level": "low",
        }],
        "validation_checklist": [{
            "check_id": "check-task",
            "description": "validate task-specific output",
        }],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }
    if proposed_artifacts is not None:
        value["proposed_artifacts"] = proposed_artifacts
    return value


class PlannerFullReplanConvergenceV2Tests(unittest.TestCase):
    def test_retired_semantic_patch_api_and_prompt_are_absent(self):
        for name in (
            "build_plan_repair_request",
            "parse_plan_patch",
            "apply_plan_patch",
            "stamp_plan_patch_control_binding",
        ):
            self.assertFalse(hasattr(plan_tracker_module, name), name)
        self.assertFalse(hasattr(PromptManager, "get_plan_repair_prompt"))
        self.assertFalse(hasattr(PromptManager, "get_plan_repair_v2_prompt"))

    def test_rejected_candidate_remains_in_the_same_active_epoch(self):
        epoch = start_planning_epoch(
            task_revision=7,
            task_semantic_identity="task-semantic-7",
            base_plan_revision=2,
            base_plan_snapshot_id="snapshot-2",
            base_plan_semantic_identity="plan-semantic-2",
        )
        failed = record_planning_failure(
            {"task_state": {"planning_epoch": epoch}},
            validation_issues=[{
                "code": "REQUIRED_OUTPUT_UNCOVERED",
                "field_path": "$.steps[0].outputs",
            }],
            planning_epoch_id=epoch["planning_epoch_id"],
            task_spec_revision_value=7,
            validation_source="plan_contract_validation",
        )
        persisted_epoch = failed["task_state"]["planning_epoch"]
        self.assertEqual(persisted_epoch["status"], "active")
        self.assertTrue(planning_epoch_matches(
            persisted_epoch,
            task_semantic_identity="task-semantic-7",
            base_plan_snapshot_id="snapshot-2",
            base_plan_semantic_identity="plan-semantic-2",
        ))

    def test_failure_is_durable_and_next_human_request_is_not_initial_request(self):
        task = _task()
        base = {
            "task_id": "planner-v2",
            "task_state": {},
            "selected_skill_ids": ["stale/legacy-capability"],
            "steps": [],
        }
        initial = build_node_context(
            node_type="planner",
            task_spec=task,
            plan_state=base,
            conversation_turns=[{"role": "user", "content": "Annotate this VCF."}],
            consultant_handoff={
                "summary": "Use the selected annotation capability.",
                "input_files": [{"role": "stale_input"}],
                "required_outputs": [{"role": "stale_output"}],
                "selected_skills": ["stale/consultant-capability"],
                "runtime_requirements": ["stale-runtime-requirement"],
            },
            runtime={
                "runtime_ready": True,
                "available_capabilities": ["genomics/snpeff-annotation"],
                "resolved_executable": "/usr/bin/java",
                "timestamp": "must-not-project",
                "healthcheck_history": ["must-not-project"],
            },
        )
        initial_request = PromptManager.get_planner_v2_human_prompt(
            initial.llm_payload.to_provider_dict()
        )
        failed = record_planning_failure(
            base,
            validation_issues=[{
                "code": "REQUIRED_OUTPUT_UNCOVERED",
                "message": "Required output has no typed producer.",
                "field_path": "$.steps[0].outputs",
                "affected_step_ids": ["annotate"],
                "affected_fields": ["outputs"],
                "violated_invariant": "required_outputs_have_one_producer",
                "required_postcondition": "Every required output has one producer.",
                "affected_artifact": {
                    "kind": "authoritative",
                    "identity": "artifact-annotated-vcf",
                },
                "expected_identity": {
                    "kind": "authoritative",
                    "identity": "artifact-annotated-vcf",
                },
                "affected_output": {"output_id": "annotated_vcf"},
                "machine_evidence": {"producer_step_ids": []},
            }],
            planning_epoch_id="epoch-1",
            task_spec_revision_value=int(task["revision"]),
            validation_source="plan_contract_validation",
            candidate=_candidate(),
            previous_correction={
                "status": "rejected",
                "outcome_code": "REQUIRED_OUTPUT_UNCOVERED",
            },
        )
        retry = build_node_context(
            node_type="planner",
            task_spec=task,
            plan_state=failed,
            conversation_turns=[{"role": "user", "content": "Annotate this VCF."}],
            consultant_handoff={
                "summary": "Use the selected annotation capability.",
                "input_files": [{"role": "stale_input"}],
                "required_outputs": [{"role": "stale_output"}],
                "selected_skills": ["stale/consultant-capability"],
                "runtime_requirements": ["stale-runtime-requirement"],
            },
            runtime={
                "runtime_ready": True,
                "available_capabilities": ["genomics/snpeff-annotation"],
            },
        )
        retry_payload = retry.llm_payload.to_provider_dict()
        retry_request = PromptManager.get_planner_v2_human_prompt(retry_payload)
        durable_issue = current_planning_failure(failed)["validation_issues"][0]

        self.assertNotEqual(initial_request, retry_request)
        self.assertEqual(
            durable_issue["message"],
            "Required output has no typed producer.",
        )
        self.assertNotIn("owner", durable_issue)
        self.assertNotIn("route", durable_issue)
        self.assertEqual(
            retry_payload["current_focus"]["planning"]["mode"],
            "correct_previous_plan",
        )
        self.assertTrue(
            retry_payload["current_focus"]["planning"]["instruction"][
                "return_complete_plan"
            ]
        )
        self.assertEqual(
            retry_payload["current_focus"]["planning"]["violations"][0]["code"],
            "REQUIRED_OUTPUT_UNCOVERED",
        )
        correction = retry_payload["current_focus"]["planning"]
        self.assertEqual(
            correction["previous_correction"],
            {
                "status": "rejected",
                "outcome_code": "REQUIRED_OUTPUT_UNCOVERED",
            },
        )
        self.assertEqual(
            correction["violations"][0]["required_postcondition"],
            "Every required output has one producer.",
        )
        self.assertEqual(
            correction["affected_subgraph"]["affected_fields"],
            ["outputs"],
        )
        self.assertEqual(
            correction["affected_subgraph"]["step_keys"],
            ["annotate"],
        )
        self.assertFalse(correction["convergence"]["no_structural_progress"])
        self.assertNotIn("failure_fingerprint", retry_request)
        self.assertNotIn("candidate_correction_signature", retry_request)
        self.assertNotIn("artifact-annotated-vcf", retry_request)
        self.assertEqual(
            retry_payload["current_focus"]["planning"]["violations"][0]
            ["affected_artifact"]["identity"],
            "output.annotated_vcf",
        )
        self.assertNotIn("healthcheck_history", initial_request)
        self.assertNotIn("must-not-project", initial_request)
        self.assertEqual(
            retry_payload["semantic_history"]["conversation"][0]["content"],
            "Annotate this VCF.",
        )
        self.assertIn("consultant_handoff", retry_payload["semantic_history"])
        self.assertEqual(
            retry_payload["semantic_history"]["consultant_handoff"],
            {"summary": "Use the selected annotation capability."},
        )
        self.assertEqual(
            retry_payload["semantic_history"]["task_spec"]["goal"],
            "Annotate the admitted VCF",
        )
        self.assertEqual(
            retry_payload["semantic_history"]["selected_capabilities"],
            ["genomics/snpeff-annotation"],
        )
        self.assertEqual(
            initial.llm_payload.to_provider_dict()["semantic_history"]["runtime"],
            {"runtime_ready": True, "resolved_executable": "/usr/bin/java"},
        )
        self.assertNotIn(
            "runtime",
            initial.llm_payload.to_provider_dict()["current_focus"],
        )

    def test_failure_lifecycle_resolve_and_task_revision_supersede(self):
        state = record_planning_failure(
            {
                "current_plan": {
                    "plan_snapshot_id": "snapshot-7",
                    "plan_revision": 3,
                },
                "task_state": {},
            },
            validation_issues=[{"code": "PLAN_STRUCTURE_INVALID", "field_path": "$.steps"}],
            planning_epoch_id="epoch-1",
            task_spec_revision_value=7,
            validation_source="planner_candidate_admission",
            candidate=_candidate(),
        )
        self.assertEqual(current_planning_failure(state)["status"], "unresolved")
        self.assertEqual(current_planning_failure(state)["plan_snapshot_id"], "snapshot-7")
        repeated = record_planning_failure(
            state,
            validation_issues=[{"code": "PLAN_STRUCTURE_INVALID", "field_path": "$.steps"}],
            planning_epoch_id="epoch-1",
            task_spec_revision_value=7,
            validation_source="planner_candidate_admission",
            candidate=_candidate(),
        )
        self.assertEqual(current_planning_failure(repeated)["attempt"], 2)
        self.assertEqual(current_planning_failure(repeated)["repeat_count"], 1)
        prose_changed_candidate = _candidate()
        prose_changed_candidate["steps"][0]["title"] = "Different prose only"
        prose_changed = record_planning_failure(
            repeated,
            validation_issues=[{
                "code": "PLAN_STRUCTURE_INVALID",
                "field_path": "$.steps",
            }],
            planning_epoch_id="epoch-1",
            task_spec_revision_value=7,
            validation_source="planner_candidate_admission",
            candidate=prose_changed_candidate,
        )
        self.assertEqual(
            current_planning_failure(prose_changed)["repeat_count"],
            2,
        )
        rebound = record_planning_failure(
            prose_changed,
            validation_issues=[{"code": "PLAN_STRUCTURE_INVALID", "field_path": "$.steps"}],
            planning_epoch_id="epoch-2",
            task_spec_revision_value=7,
            validation_source="planner_candidate_admission",
        )
        self.assertEqual(current_planning_failure(rebound)["attempt"], 1)
        self.assertEqual(current_planning_failure(rebound)["repeat_count"], 0)
        resolved = resolve_planning_failure(state)
        self.assertEqual(
            resolved["task_state"]["planning_failure"]["status"], "resolved"
        )
        superseded = supersede_planning_failure(state, task_spec_revision_value=8)
        self.assertEqual(
            superseded["task_state"]["planning_failure"]["status"],
            "superseded",
        )

    def test_no_progress_uses_validator_affected_structure_not_superficial_edits(self):
        issue = {
            "code": "PLAN_DEPENDENCY_CYCLE",
            "message": "Plan dependencies contain a cycle.",
            "field_path": "$.steps",
            "affected_step_ids": ["report"],
            "affected_fields": ["dependencies"],
            "violated_invariant": "dependency_graph_is_acyclic",
            "required_postcondition": "The returned dependency graph contains no cycle.",
            "machine_evidence": {
                "cycle": ["report", "status", "reproduce", "report"],
                "validator_id": "plan_dag",
            },
        }
        candidate = {
            "steps": [
                {"step_id": "report", "inputs": [], "outputs": [], "dependencies": ["status"]},
                {"step_id": "reproduce", "inputs": [], "outputs": [], "dependencies": ["report"]},
                {"step_id": "status", "inputs": [], "outputs": [], "dependencies": ["reproduce"]},
            ],
        }
        first = record_planning_failure(
            {"task_state": {}},
            validation_issues=[issue],
            planning_epoch_id="epoch-cycle",
            task_spec_revision_value=1,
            validation_source="planner_candidate_admission",
            candidate=candidate,
            previous_correction={
                "status": "rejected",
                "outcome_code": "PLAN_DEPENDENCY_CYCLE",
            },
        )
        superficial = deepcopy(candidate)
        superficial["steps"][1]["inputs"] = ["output.report"]
        repeated = record_planning_failure(
            first,
            validation_issues=[issue],
            planning_epoch_id="epoch-cycle",
            task_spec_revision_value=1,
            validation_source="planner_candidate_admission",
            candidate=superficial,
            previous_correction={
                "status": "rejected",
                "outcome_code": "PLAN_DEPENDENCY_CYCLE",
            },
        )
        repeated_failure = current_planning_failure(repeated)
        self.assertEqual(repeated_failure["repeat_count"], 1)
        self.assertEqual(
            repeated_failure["correction_progress"],
            "no_structural_progress",
        )
        retry_context = build_node_context(
            node_type="planner",
            task_spec=_task(),
            plan_state=repeated,
            conversation_turns=[{"role": "user", "content": "Annotate this VCF."}],
        )
        correction = retry_context.llm_payload.to_provider_dict()[
            "current_focus"
        ]["planning"]
        self.assertTrue(correction["convergence"]["no_structural_progress"])
        self.assertTrue(
            correction["instruction"]["change_affected_structure_if_no_progress"]
        )
        self.assertEqual(
            correction["affected_subgraph"]["step_keys"],
            ["report", "reproduce", "status"],
        )
        self.assertEqual(
            correction["affected_subgraph"]["affected_fields"],
            ["dependencies"],
        )

        structural = deepcopy(superficial)
        structural["steps"][2]["dependencies"] = []
        progressed = record_planning_failure(
            repeated,
            validation_issues=[issue],
            planning_epoch_id="epoch-cycle",
            task_spec_revision_value=1,
            validation_source="planner_candidate_admission",
            candidate=structural,
        )
        progressed_failure = current_planning_failure(progressed)
        self.assertEqual(progressed_failure["repeat_count"], 0)
        self.assertEqual(
            progressed_failure["correction_progress"],
            "affected_structure_changed",
        )

    def test_dag_validator_emits_generic_correction_contract(self):
        candidate = _candidate()
        template = deepcopy(candidate["steps"][0])
        candidate["steps"] = []
        for step_id, dependency in (
            ("report", "status"),
            ("reproduce", "report"),
            ("status", "reproduce"),
        ):
            step = deepcopy(template)
            step.update({
                "step_id": step_id,
                "inputs": [],
                "outputs": [],
                "dependencies": [dependency],
            })
            candidate["steps"].append(step)
        _, _, issues = validate_planner_candidate_deterministically(
            candidate,
            candidate_scope_id="candidate-cycle",
            authoritative_handles={},
            availability_origins={},
        )
        cycle = next(
            issue for issue in issues
            if issue.get("code") == "PLAN_DEPENDENCY_CYCLE"
        )
        self.assertEqual(cycle["affected_fields"], ["dependencies"])
        self.assertEqual(cycle["violated_invariant"], "dependency_graph_is_acyclic")
        self.assertEqual(
            cycle["required_postcondition"],
            "The returned dependency graph contains no cycle.",
        )

    def test_terminalization_preserves_last_real_failure_and_repeat_evidence(self):
        issue = {
            "code": "PLAN_DEPENDENCY_CYCLE",
            "message": "Plan dependencies contain a cycle.",
            "field_path": "$.steps",
            "affected_step_ids": ["report"],
            "affected_fields": ["dependencies"],
        }
        candidate = {"steps": [{"step_id": "report", "dependencies": ["report"]}]}
        first = record_planning_failure(
            {"task_state": {}},
            validation_issues=[issue],
            planning_epoch_id="epoch-terminal",
            task_spec_revision_value=1,
            validation_source="planner_candidate_admission",
            candidate=candidate,
        )
        repeated = record_planning_failure(
            first,
            validation_issues=[issue],
            planning_epoch_id="epoch-terminal",
            task_spec_revision_value=1,
            validation_source="planner_candidate_admission",
            candidate=candidate,
        )
        terminal = terminalize_planning_failure(
            repeated,
            planning_epoch_id="epoch-terminal",
            task_spec_revision_value=1,
            terminal_diagnostic={
                "error_code": "PLANNING_EPOCH_EXHAUSTED",
                "machine_evidence": {
                    "planner_calls_total": 4,
                    "max_planner_calls_per_epoch": 4,
                },
            },
        )
        failure = terminal["task_state"]["planning_failure"]
        self.assertEqual(failure["status"], "terminal")
        self.assertEqual(failure["validation_source"], "planner_candidate_admission")
        self.assertEqual(failure["validation_issues"][0]["code"], "PLAN_DEPENDENCY_CYCLE")
        self.assertEqual(failure["repeat_count"], 1)
        self.assertEqual(
            failure["terminal_diagnostic"]["machine_evidence"]["planner_calls_total"],
            4,
        )

    def test_empty_validator_result_still_fails_closed_with_machine_issue(self):
        state = record_planning_failure(
            {"task_state": {}},
            validation_issues=[],
            planning_epoch_id="epoch-empty-diagnostic",
            task_spec_revision_value=7,
            validation_source="plan_contract_validation",
        )
        failure = current_planning_failure(
            state,
            task_spec_revision_value=7,
        )
        self.assertEqual(failure["status"], "unresolved")
        self.assertEqual(len(failure["validation_issues"]), 1)
        issue = failure["validation_issues"][0]
        self.assertEqual(issue["code"], "PLAN_CANDIDATE_INVALID")
        self.assertEqual(issue["field_path"], "$")
        self.assertEqual(
            issue["machine_evidence"]["validation_source"],
            "plan_contract_validation",
        )
        self.assertNotIn("message", issue)
        self.assertNotIn("owner", issue)
        self.assertNotIn("route", issue)

    def test_json_error_preserves_parser_location_and_excerpt(self):
        with self.assertRaises(PlannerEnvelopeError) as raised:
            parse_planner_outcome_envelope('{"outcome_type":"PLAN_CANDIDATE",\n"steps": [}')
        diagnostic = raised.exception.to_diagnostic()
        self.assertEqual(diagnostic["code"], "PLANNER_ENVELOPE_JSON_INVALID")
        self.assertGreaterEqual(diagnostic["line"], 1)
        self.assertGreaterEqual(diagnostic["column"], 1)
        self.assertIn("parser_message", diagnostic)
        self.assertIn("response_excerpt", diagnostic)

    def test_task_output_cannot_be_shadowed_by_plan_local_artifact(self):
        task = _task()
        # Make handle, role, stable ID, and output_id distinct so this proves
        # collision detection consumes typed TaskSpec metadata rather than a
        # lucky string match against the model-visible handle.
        task["output_contracts"][0]["semantic_handle"] = "output.primary_result"
        task["output_contracts"][0]["role"] = "annotation_result"
        context = build_node_context(
            node_type="planner",
            task_spec=task,
            plan_state={"task_state": {}, "steps": []},
        )
        candidate = _candidate(proposed_artifacts=[{
            "local_ref": "plan.annotated_vcf",
            "semantic_view": {
                "role": "annotated_vcf",
                "format": "vcf.gz",
                "description": "invalid shadow",
            },
            "artifact_contract": {
                "object_kind": "file",
                "lifecycle": "plan_local",
                "materialization_policy_id": "managed_intermediate_v1",
            },
        }])
        bindings = {
            str(handle): dict(value)
            for handle, value in context.binding.semantic_handle_map.items()
            if value.get("object_type") == "artifact"
        }
        handles = {
            handle: str(value.get("stable_identity") or "")
            for handle, value in bindings.items()
        }
        availability = {
            str(value.get("stable_identity") or ""): (
                "verified_task_input"
                if value.get("direction") == "input"
                else "not_yet_available"
            )
            for value in bindings.values()
        }
        _, _, issues = validate_planner_candidate_deterministically(
            candidate,
            candidate_scope_id="candidate-scope-1",
            authoritative_handles=handles,
            authoritative_bindings=bindings,
            availability_origins=availability,
        )
        self.assertEqual(issues[0]["code"], "TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL")
        self.assertEqual(
            issues[0]["field_path"],
            "$.proposed_artifacts[0].local_ref",
        )
        self.assertIn("affected_artifact", issues[0])
        self.assertEqual(
            issues[0]["affected_output"]["output_id"],
            "annotated_vcf",
        )
        self.assertIn("expected_identity", issues[0])
        self.assertIn("observed_identity", issues[0])
        self.assertIn("machine_evidence", issues[0])
        self.assertEqual(
            issues[0]["expected_identity"]["identity"],
            "artifact-annotated-vcf",
        )
        self.assertEqual(
            issues[0]["observed_identity"]["kind"], "candidate_local"
        )

    def test_plan_local_artifact_must_use_closed_plan_namespace(self):
        candidate = _candidate(proposed_artifacts=[{
            "local_ref": "scratch",
            "semantic_view": {"role": "scratch"},
            "artifact_contract": {
                "object_kind": "file",
                "lifecycle": "plan_local",
                "materialization_policy_id": "managed_intermediate_v1",
            },
        }])
        _, _, issues = validate_planner_candidate_deterministically(
            candidate,
            candidate_scope_id="candidate-scope-closed-namespace",
            authoritative_handles={},
            authoritative_bindings={},
            availability_origins={},
        )
        self.assertEqual(issues[0]["code"], "LOCAL_REF_INVALID")
        self.assertEqual(
            issues[0]["machine_evidence"]["required_namespace"],
            "plan.*",
        )

    def test_schema_diagnostic_preserves_expected_and_received_types(self):
        candidate = _candidate()
        candidate["steps"][0]["inputs"] = "input.variant_vcf"
        _, _, issues = validate_planner_candidate_deterministically(
            candidate,
            candidate_scope_id="candidate-scope-schema",
            authoritative_handles={},
            authoritative_bindings={},
            availability_origins={},
        )
        issue = issues[0]
        self.assertEqual(issue["code"], "PLAN_STRUCTURE_INVALID")
        self.assertEqual(issue["field_path"], "$.steps[0].inputs")
        self.assertEqual(issue["expected_type"], "array")
        self.assertEqual(issue["received_type"], "string")
        self.assertEqual(issue["received_excerpt"], '"input.variant_vcf"')

    def test_task_output_projection_contains_complete_contract(self):
        task = _task()
        task["output_contracts"][0].pop("alternative_group")
        task["output_contracts"][0].pop("description")
        payload = build_node_context(
            node_type="planner",
            task_spec=task,
            plan_state={"task_state": {}, "steps": []},
        ).llm_payload.to_provider_dict()
        output = payload["semantic_history"]["task_spec"]["outputs"][0]
        for field in (
            "handle", "output_id", "role", "artifact_kind", "format", "required",
            "path_policy", "root_ref", "producer_owner", "required_basename",
            "alternative_group", "description",
        ):
            self.assertIn(field, output)
        self.assertEqual(output["alternative_group"], "")
        self.assertEqual(output["description"], "")

    def test_approved_plan_local_identity_uses_plan_namespace_and_survives_replan(self):
        plan_artifact_id = "plan-artifact-internal-123"
        ledger = {
            plan_artifact_id: {
                "logical_artifact_id": plan_artifact_id,
                "local_ref": "plan.snpeff_db",
                "canonical_path": "/managed/plan-artifact-internal-123",
                "object_kind": "directory",
                "lifecycle": "plan_local",
                "materialization_policy_id": "managed_intermediate_v1",
                "semantic_view": {
                    "role": "snpeff_db",
                    "format": "directory",
                    "description": "prepared annotation database",
                },
            },
        }
        approved = {
            "steps": [{
                **_step(output=plan_artifact_id),
                "status": "done",
            }],
            "plan_local_artifacts": ledger,
        }
        context = build_node_context(
            node_type="planner",
            task_spec=_task(),
            plan_state={"task_state": {}, "steps": approved["steps"]},
            approved_plan_snapshot=approved,
        )
        payload = context.llm_payload.to_provider_dict()
        serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertIn("plan.snpeff_db", serialised)
        self.assertNotIn(plan_artifact_id, serialised)
        self.assertEqual(
            payload["semantic_history"]["approved_plan"]["steps"][0]["outputs"],
            ["plan.snpeff_db"],
        )

        pending_context = build_node_context(
            node_type="planner",
            task_spec=_task(),
            plan_state={"task_state": {}, "steps": approved["steps"]},
            plan_snapshot=approved,
            plan_snapshot_status="pending",
        ).llm_payload.to_provider_dict()
        self.assertEqual(
            pending_context["semantic_history"]["plan_snapshot"]["snapshot_status"],
            "pending",
        )
        self.assertNotIn("approved_plan", pending_context["semantic_history"])

        carried = carry_forward_plan_lifecycle(
            {"task_id": "planner-v2", "steps": approved["steps"], "plan_artifacts": ledger},
            {"task_id": "planner-v2", "steps": [_step(output=plan_artifact_id)]},
        )
        self.assertEqual(carried["plan_artifacts"], ledger)

    def test_typed_output_producer_is_stable_before_and_after_local_allocation(self):
        task = _task()
        output_contracts = deepcopy(task["output_contracts"])
        with tempfile.TemporaryDirectory() as root:
            save_plan_state(root, "planner-v2", {
                "task_id": "planner-v2",
                "task_spec": task,
                "current_task": {"task_spec": task},
                "task_state": {},
                "steps": [],
                "state_generation": 0,
            })
            baseline = load_plan_state(root, "planner-v2")
            context = build_node_context(
                node_type="planner",
                task_spec=task,
                plan_state=baseline,
            )
            candidate = _candidate(proposed_artifacts=[{
                "local_ref": "plan.snpeff_db",
                "semantic_view": {
                    "role": "snpeff_db",
                    "format": "directory",
                },
                "artifact_contract": {
                    "object_kind": "directory",
                    "lifecycle": "plan_local",
                    "materialization_policy_id": "managed_intermediate_v1",
                },
            }])
            candidate["steps"] = [
                {
                    **candidate["steps"][0],
                    "step_id": "prepare-db",
                    "title": "Prepare database",
                    "inputs": [],
                    "outputs": ["plan.snpeff_db"],
                },
                {
                    **candidate["steps"][0],
                    "step_id": "annotate",
                    "inputs": ["input.variant_vcf", "plan.snpeff_db"],
                    "outputs": ["output.annotated_vcf"],
                    "dependencies": ["prepare-db"],
                },
            ]
            bindings = {
                str(handle): dict(value)
                for handle, value in context.binding.semantic_handle_map.items()
                if value.get("object_type") == "artifact"
            }
            handles = {
                handle: str(value.get("stable_identity") or "")
                for handle, value in bindings.items()
            }
            availability = {
                str(value.get("stable_identity") or ""): (
                    "verified_task_input"
                    if value.get("direction") == "input"
                    else "not_yet_available"
                )
                for value in bindings.values()
            }
            _, usage, issues = validate_planner_candidate_deterministically(
                candidate,
                candidate_scope_id=context.binding.candidate_scope_id,
                authoritative_handles=handles,
                authoritative_bindings=bindings,
                availability_origins=availability,
            )
            self.assertEqual(issues, [])
            resolved = resolve_plan_proposal_handles(
                candidate,
                context.binding,
            )
            parsed = build_plan_state(
                "planner-v2",
                json.dumps(resolved),
                strict=True,
            )
            pending = deepcopy(baseline)
            pending["steps"] = parsed["steps"]
            pending["plan_document"] = parsed["plan_document"]
            pending["proposed_artifacts"] = parsed["proposed_artifacts"]
            pending["artifact_usage"] = usage
            pending["allocation_intent"] = prepare_allocation_intent(
                candidate,
                candidate_scope_id=context.binding.candidate_scope_id,
                planning_epoch_id="planning-epoch-allocation",
            )
            pending["generated_from_task_revision"] = task["revision"]
            pending["base_task_semantic_hash"] = task_semantic_hash(task)
            pending["validation_issues"] = []
            pending["validation_recompute_required"] = False
            pending["plan_commit_requested"] = True

            self.assertEqual(
                validate_output_contracts(pending, output_contracts),
                [],
            )
            save_plan_state(root, "planner-v2", pending)
            reloaded = load_plan_state(root, "planner-v2")
            allocation = reloaded["artifact_allocation_mapping"][
                "local_to_authoritative"
            ]
            self.assertIn("plan.snpeff_db", allocation)
            self.assertNotIn(
                "plan.snpeff_db",
                json.dumps(reloaded["artifact_usage"], sort_keys=True),
            )
            self.assertEqual(validate_output_contracts(reloaded, output_contracts), [])
            reviewed, _, review_issues = normalize_plan_contracts(
                reloaded,
                renderer_records=[],
                output_contracts=output_contracts,
                task_spec=task,
            )
            producer_codes = {
                str(issue.get("code") or issue.get("violation_code") or "")
                for issue in review_issues
                if isinstance(issue, dict)
            }
            self.assertNotIn("REQUIRED_OUTPUT_UNCOVERED", producer_codes)
            self.assertNotIn("OUTPUT_PRODUCER_AMBIGUOUS", producer_codes)
            self.assertEqual(validate_output_contracts(reviewed, output_contracts), [])

    def test_full_replan_ignores_prose_but_protects_machine_history(self):
        previous = {
            "steps": [{
                **_step(title="Old wording"),
                "status": "done",
                "evidence": [{"event": "execute_success"}],
            }],
            "artifact_usage": [{
                "step_id": "annotate",
                "relation": "produce",
                "artifact_ref": {
                    "kind": "authoritative",
                    "identity": "artifact-annotated-vcf",
                },
            }],
        }
        prose_only = deepcopy(previous)
        prose_only["steps"][0]["title"] = "New wording"
        prose_only["steps"][0]["success_criteria"] = ["Different prose"]
        prose_only["steps"][0].pop("status", None)
        prose_only["steps"][0].pop("evidence", None)
        self.assertEqual(full_replan_transition_issues(previous, prose_only), [])

        changed = deepcopy(prose_only)
        changed["steps"][0]["outputs"] = ["different-output"]
        changed["artifact_usage"][0]["artifact_ref"]["identity"] = "different-output"
        issues = full_replan_transition_issues(previous, changed)
        self.assertEqual(
            issues[0]["code"], "COMPLETED_PLAN_STEP_IDENTITY_CHANGED"
        )

    def test_prompt_is_split_and_provider_visible_example_is_retired(self):
        system = PromptManager.get_planner_v2_system_prompt()
        human = PromptManager.get_planner_v2_human_prompt({
            "semantic_history": {"task_spec": {"goal": "task"}},
            "current_focus": {"planning": {"mode": "create_complete_plan"}},
        })
        self.assertIn("PLAN_CANDIDATE", system)
        self.assertIn("plan.<task-specific-local-handle>", system)
        self.assertNotIn("Create the intermediate summary", system + human)
        self.assertNotIn("intermediate_summary", system + human)
        self.assertEqual(
            set(json.loads(human)), {"semantic_history", "current_focus"}
        )


if __name__ == "__main__":
    unittest.main()

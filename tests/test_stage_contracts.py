import json
import unittest
from dataclasses import FrozenInstanceError

from agent_core.workflow.stage_contracts import (
    ConsultantSeed,
    ConsultationHandoff,
    FailurePacket,
    freeze,
    PlannerSeed,
    PlanSpecRef,
    RecoveryDirective,
    RecoverySeed,
    SelectedSkillBundle,
    SelectedSkillRef,
    StageContractError,
    StepSeed,
    TaskSpecRef,
    thaw,
)


def task_ref(**updates):
    value = {
        "schema_version": 1,
        "task_id": "task-1",
        "task_revision": 3,
        "task_semantic_hash": "sha256:task-semantic",
        "task_record_hash": "sha256:task-record",
        "task_spec_ref": "object:sha256:task",
    }
    value.update(updates)
    return value


def skill_ref(**updates):
    value = {
        "schema_version": 1,
        "skill_id": "genomics/snpeff-annotation",
        "snapshot_id": "skill-snapshot-1",
        "content_hash": "sha256:skill",
        "scope": "task",
        "source_path": "skills/genomics/snpeff-annotation/SKILL.md",
        "description": "Annotate variants with a selected, immutable Skill snapshot.",
        "heading_refs": ["usage", "outputs"],
    }
    value.update(updates)
    return value


def skill_bundle(**updates):
    value = {
        "schema_version": 1,
        "bundle_id": "skill-bundle-1",
        "task_revision": 3,
        "skills": [skill_ref()],
        "selection_summary": "One task-scoped annotation Skill.",
    }
    value.update(updates)
    return value


def consultation_handoff(**updates):
    value = {
        "schema_version": 1,
        "handoff_id": "consultation-handoff-1",
        "task_spec_ref": task_ref(),
        "selected_skill_bundle": skill_bundle(),
        "summary": "Inputs and annotation outputs are resolved.",
        "capability_decisions": ["Use the selected annotation capability."],
        "unresolved_questions": [],
        "evidence_refs": ["evidence:consultation"],
    }
    value.update(updates)
    return value


def planner_seed(**updates):
    value = {
        "schema_version": 1,
        "seed_id": "planner-seed-1",
        "task_id": "task-1",
        "consultation_handoff": consultation_handoff(),
        "artifact_manifest": [
            {
                "artifact_id": "input-vcf",
                "role": "variants",
                "format": "vcf",
                "path": "/workspace/input.vcf",
                "availability": "available",
                "required": True,
            }
        ],
        "planning_constraints": ["Preserve input coordinates."],
        "evidence_refs": ["evidence:planner-seed"],
    }
    value.update(updates)
    return value


def plan_ref(**updates):
    value = {
        "schema_version": 1,
        "task_id": "task-1",
        "plan_id": "plan-1",
        "plan_version": "v3",
        "plan_revision": 3,
        "plan_semantic_hash": "sha256:plan-semantic",
        "plan_record_hash": "sha256:plan-record",
        "plan_snapshot_ref": "object:sha256:plan",
    }
    value.update(updates)
    return value


def current_step(**updates):
    value = {
        "step_id": "step-2",
        "title": "Annotate variants",
        "action": "execute",
        "inputs": ["artifact:normalized-vcf"],
        "outputs": ["artifact:annotated-vcf"],
        "dependencies": ["step-1"],
        "success_criteria": ["Annotated VCF exists and is parseable."],
        "skill_ids": ["genomics/snpeff-annotation"],
        "metadata": {"risk": "bounded"},
    }
    value.update(updates)
    return value


def dependency_outcomes():
    return [
        {
            "step_id": "step-1",
            "status": "completed",
            "summary": "Input VCF was normalized.",
            "outcome_ref": "object:sha256:step-1-outcome",
            "evidence_refs": ["evidence:step-1"],
            "artifact_refs": ["artifact:normalized-vcf"],
            "binding_refs": ["binding:normalized-vcf"],
        }
    ]


def step_seed(**updates):
    value = {
        "schema_version": 1,
        "seed_id": "step-seed-2",
        "task_spec_ref": task_ref(),
        "plan_spec_ref": plan_ref(),
        "selected_skill_bundle": skill_bundle(),
        "current_step": current_step(),
        "dependency_outcomes": dependency_outcomes(),
        "available_artifacts": [
            {
                "artifact_id": "normalized-vcf",
                "role": "variants",
                "format": "vcf",
                "content_ref": "object:sha256:normalized-vcf",
                "source_step_id": "step-1",
                "availability": "available",
            }
        ],
        "accepted_bindings": [
            {
                "binding_id": "binding:normalized-vcf",
                "name": "input_vcf",
                "value_ref": "object:sha256:normalized-vcf",
                "value_type": "artifact",
                "producer_step_id": "step-1",
            }
        ],
        "workspace": "/workspace",
    }
    value.update(updates)
    return value


def failure_packet(**updates):
    value = {
        "schema_version": 1,
        "failure_id": "failure-1",
        "task_id": "task-1",
        "step_id": "step-2",
        "failure_type": "runtime_error",
        "failure_owner": "runtime",
        "error_code": "DATABASE_NOT_FOUND",
        "summary": "The selected database was not available.",
        "retryable": True,
        "evidence_refs": ["evidence:failed-attempt"],
        "plan_spec_ref": plan_ref(),
        "attempt_ref": "attempt:step-2:1",
        "action_ref": "action:step-2:1",
        "failure_fingerprint": "sha256:failure",
        "diagnostic_facts": [
            {
                "fact_id": "fact-1",
                "kind": "missing_dependency",
                "summary": "Database directory is absent.",
                "evidence_ref": "evidence:failed-attempt",
                "confidence": 1.0,
            }
        ],
    }
    value.update(updates)
    return value


def recovery_seed(**updates):
    value = {
        "schema_version": 1,
        "seed_id": "recovery-seed-1",
        "task_spec_ref": task_ref(),
        "plan_spec_ref": plan_ref(),
        "selected_skill_bundle": skill_bundle(),
        "current_step": current_step(),
        "failure_packet": failure_packet(),
        "dependency_outcomes": dependency_outcomes(),
        "prior_directive_refs": ["directive:previous"],
        "evidence_refs": ["evidence:failed-attempt"],
    }
    value.update(updates)
    return value


def consultant_seed(**updates):
    value = {
        "schema_version": 1,
        "seed_id": "consultant-seed-1",
        "task_id": "task-1",
        "request_summary": "Annotate the supplied VCF.",
        "input_manifest": [
            {
                "input_id": "input-vcf",
                "role": "variants",
                "path": "/workspace/input.vcf",
                "format": "vcf",
                "required": True,
                "metadata": {"source": "user"},
            }
        ],
        "task_spec_ref": task_ref(),
        "constraints": ["Do not alter variant coordinates."],
        "evidence_refs": ["evidence:user-request"],
        "skill_catalog_ref": "object:sha256:skill-catalog",
    }
    value.update(updates)
    return value


def recovery_directive(action="retry_step", **updates):
    value = {
        "schema_version": 1,
        "directive_id": f"directive-{action}",
        "task_id": "task-1",
        "action": action,
        "summary": f"Semantic recovery decision: {action}.",
        "reason_code": "RECOVERY_DECISION",
        "evidence_refs": ["evidence:failed-attempt"],
    }
    if action in {"retry_step", "revalidate"}:
        value["target_step_id"] = "step-2"
    if action in {"retry_step", "revise_plan", "reconsult"}:
        value["instruction"] = "Repair only the failed semantic scope."
    if action == "ask_user":
        value.update({
            "question": "Which reference database should be used?",
            "requested_slots": ["reference_database"],
        })
    value.update(updates)
    return value


CONTRACT_FIXTURES = (
    (TaskSpecRef, task_ref),
    (SelectedSkillRef, skill_ref),
    (SelectedSkillBundle, skill_bundle),
    (ConsultantSeed, consultant_seed),
    (ConsultationHandoff, consultation_handoff),
    (PlannerSeed, planner_seed),
    (PlanSpecRef, plan_ref),
    (StepSeed, step_seed),
    (FailurePacket, failure_packet),
    (RecoverySeed, recovery_seed),
    (RecoveryDirective, recovery_directive),
)


class StageContractTests(unittest.TestCase):
    def test_every_contract_round_trips_as_plain_json(self):
        for contract, fixture in CONTRACT_FIXTURES:
            with self.subTest(contract=contract.__name__):
                parsed = contract.from_mapping(fixture())
                wire = parsed.to_dict()
                self.assertEqual(contract.from_mapping(wire).to_dict(), wire)
                self.assertIs(contract.freeze(parsed), parsed)
                self.assertEqual(contract.freeze(wire).thaw(), wire)
                self.assertEqual(json.loads(json.dumps(wire, allow_nan=False)), wire)

    def test_generic_freeze_and_thaw_are_recursive_and_detached(self):
        source = {"nested": [{"value": 1}]}
        frozen = freeze(source)
        source["nested"][0]["value"] = 2
        self.assertEqual(frozen["nested"][0]["value"], 1)
        with self.assertRaises(TypeError):
            frozen["nested"][0]["value"] = 3
        mutable = thaw(frozen)
        mutable["nested"][0]["value"] = 4
        self.assertEqual(frozen["nested"][0]["value"], 1)

    def test_contract_and_nested_json_are_immutable(self):
        parsed = StepSeed.from_mapping(step_seed())
        with self.assertRaises(FrozenInstanceError):
            parsed.seed_id = "changed"
        with self.assertRaises(TypeError):
            parsed.current_step["step_id"] = "changed"
        with self.assertRaises(TypeError):
            parsed.current_step["metadata"]["risk"] = "changed"
        with self.assertRaises(TypeError):
            parsed.dependency_outcomes[0]["summary"] = "changed"

    def test_unknown_top_level_fields_fail_for_every_contract(self):
        for contract, fixture in CONTRACT_FIXTURES:
            value = fixture()
            value["unexpected"] = True
            with self.subTest(contract=contract.__name__):
                with self.assertRaisesRegex(StageContractError, "STAGE_FIELD_UNKNOWN"):
                    contract.from_mapping(value)

    def test_schema_version_is_required_and_exact_for_every_contract(self):
        for contract, fixture in CONTRACT_FIXTURES:
            for mutation in (
                {"schema_version": 2},
                {"schema_version": True},
                {"schema_version": 1.0},
                {"schema_version": "1"},
            ):
                value = fixture()
                value.update(mutation)
                with self.subTest(contract=contract.__name__, mutation=mutation):
                    with self.assertRaisesRegex(StageContractError, "STAGE_SCHEMA_UNSUPPORTED"):
                        contract.from_mapping(value)

    def test_context_and_route_leakage_keys_are_rejected_recursively(self):
        poison = (
            "messages",
            "prior-transcript",
            "raw_transcript",
            "full_skill_doc",
            "loaded_skill_documents",
            "graph_node",
            "goto",
            "controller_transition",
            "next_route",
        )
        for key in poison:
            value = step_seed()
            value["current_step"]["metadata"] = {"nested": {key: "planner"}}
            expected = (
                "STAGE_ROUTE_FIELD_FORBIDDEN"
                if key in {"graph_node", "goto", "controller_transition", "next_route"}
                else "STAGE_CONTEXT_LEAK_FORBIDDEN"
            )
            with self.subTest(key=key):
                with self.assertRaisesRegex(StageContractError, expected):
                    StepSeed.from_mapping(value)

    def test_selected_skill_bundle_is_manifest_only(self):
        for key in ("document", "exact_text", "full_skill_document", "raw_content"):
            value = skill_bundle()
            value["skills"][0][key] = "large Skill body"
            with self.subTest(key=key):
                with self.assertRaisesRegex(StageContractError, "STAGE_CONTEXT_LEAK_FORBIDDEN"):
                    SelectedSkillBundle.from_mapping(value)

    def test_selected_skill_bundle_allows_an_explicit_empty_manifest(self):
        value = skill_bundle()
        value["skills"] = []
        parsed = SelectedSkillBundle.from_mapping(value)
        self.assertEqual(parsed.skills, ())
        self.assertEqual(parsed.to_dict()["skills"], [])

    def test_step_seed_accepts_compact_dependency_outcomes_but_not_transcripts(self):
        parsed = StepSeed.from_mapping(step_seed())
        self.assertEqual(parsed.dependency_outcomes[0]["step_id"], "step-1")
        self.assertEqual(parsed.dependency_outcomes[0]["summary"], "Input VCF was normalized.")
        self.assertNotIn("transcript", parsed.to_dict()["dependency_outcomes"][0])

        for forbidden in (
            {"prior_transcript": ["tool call"]},
            {"stdout": "unbounded output"},
            {"details": "not part of the compact whitelist"},
        ):
            value = step_seed()
            value["dependency_outcomes"][0].update(forbidden)
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(StageContractError):
                    StepSeed.from_mapping(value)

    def test_step_and_recovery_scope_identities_must_match(self):
        value = step_seed(plan_spec_ref=plan_ref(task_id="other-task"))
        with self.assertRaisesRegex(StageContractError, "STAGE_SCOPE_MISMATCH"):
            StepSeed.from_mapping(value)

        value = recovery_seed()
        value["failure_packet"]["step_id"] = "different-step"
        with self.assertRaisesRegex(StageContractError, "STAGE_SCOPE_MISMATCH"):
            RecoverySeed.from_mapping(value)

    def test_skill_bundle_revision_is_bound_to_task_revision(self):
        value = consultation_handoff()
        value["selected_skill_bundle"]["task_revision"] = 2
        with self.assertRaisesRegex(StageContractError, "STAGE_SCOPE_MISMATCH"):
            ConsultationHandoff.from_mapping(value)

    def test_all_recovery_actions_are_semantic_and_round_trip(self):
        for action in sorted(RecoveryDirective.ALLOWED_ACTIONS):
            with self.subTest(action=action):
                parsed = RecoveryDirective.from_mapping(recovery_directive(action))
                self.assertEqual(parsed.action, action)
                wire = parsed.to_dict()
                self.assertNotIn("graph_node", wire)
                self.assertNotIn("goto", wire)

    def test_recovery_directive_rejects_node_names_as_actions(self):
        for action in ("generate", "planner", "consultant", "supervisor"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(StageContractError, "RECOVERY_ACTION_UNKNOWN"):
                    RecoveryDirective.from_mapping(recovery_directive(action))

    def test_recovery_action_specific_fields_are_enforced(self):
        cases = (
            recovery_directive("retry_step", target_step_id=""),
            recovery_directive("revise_plan", instruction=""),
            recovery_directive("ask_user", question=""),
            recovery_directive("ask_user", requested_slots=[]),
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(StageContractError, "STAGE_FIELD_REQUIRED"):
                    RecoveryDirective.from_mapping(value)

    def test_scientific_prose_may_name_stages_without_becoming_route_metadata(self):
        value = recovery_directive(
            "revise_plan",
            summary="Planner-like reasoning is needed before Generate retries.",
        )
        parsed = RecoveryDirective.from_mapping(value)
        self.assertIn("Generate", parsed.summary)


if __name__ == "__main__":
    unittest.main()

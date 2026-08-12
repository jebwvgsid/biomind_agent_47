from __future__ import annotations

import tempfile
import unittest

from agent_core.consultation_state import (
    build_consultant_candidate,
    consultant_no_progress_decision,
    evaluate_consultant_ask,
    new_consultation_state,
)
from agent_core.plan_tracker import (
    commit_consultant_candidate_state,
    initialize_consultant_bootstrap_state,
    load_plan_state,
)
from agent_core.skill_manager import SkillManager
from agent_core.state_contract_store import StateContractStore
from agent_core.task_spec import (
    TASK_SPEC_MUTATION_SCHEMA_VERSION,
    apply_task_spec_mutation_proposal,
    project_task_spec_to_renderer,
    task_spec_is_initialized,
)


def mutation(*operations: dict) -> dict:
    return {
        "proposal_type": "task_spec_mutation",
        "schema_version": TASK_SPEC_MUTATION_SCHEMA_VERSION,
        "operations": list(operations),
        "unresolved_ambiguities": [],
        "proposal_evidence_refs": [],
    }


def op(name: str, **fields: object) -> dict:
    return {
        "op": name,
        **fields,
        "reason": "consultant_interpretation",
        "supporting_evidence": ["U1"],
    }


class ConsultantFirstCanonicalStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skills = SkillManager("skills")

    def _conversation(self, root: str, task_id: str) -> tuple[StateContractStore, dict]:
        store = StateContractStore(root, task_id)
        raw_ref = store.put_object("raw_user_submission", {"text": "VCF=A FASTA=B GTF=C"})
        index, _ = store.append_conversation_turn(
            {},
            role="user",
            content="VCF=A FASTA=B GTF=C",
            content_ref=raw_ref,
            turn_id="U1",
        )
        return store, index

    def test_01_bootstrap_is_null_until_consultant_candidate_commits_revision_one(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store, evidence = self._conversation(root, "task-bootstrap")
            raw_ref = store.read_conversation_evidence(evidence)[0]["content_ref"]
            initial = initialize_consultant_bootstrap_state(
                root,
                "task-bootstrap",
                raw_submission_ref=raw_ref,
                conversation_evidence=evidence,
            )
            self.assertIsNone(initial["current_task"]["task_spec"])
            self.assertEqual(initial["current_task"]["task_revision"], 0)

            candidate = build_consultant_candidate(
                None,
                new_consultation_state("consult-1", "task-bootstrap"),
                mutation(
                    op("set_goal", value="Annotate the supplied VCF"),
                    op("upsert_input", input={"role": "variant_vcf", "path": "/data/A.vcf"}),
                ),
                selected_skills=["genomics/snpeff-annotation"],
                task_id="task-bootstrap",
                raw_submission_ref=raw_ref,
                renderer_records=[self.skills.get_renderer_record("genomics/snpeff-annotation")],
            )
            committed = commit_consultant_candidate_state(root, "task-bootstrap", candidate)
            self.assertTrue(task_spec_is_initialized(committed["current_task"]["task_spec"]))
            self.assertEqual(committed["current_task"]["task_revision"], 1)

    def test_02_bootstrap_does_not_make_filename_parser_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = StateContractStore(root, "task-natural-language")
            text = "Do not use fake.fa; use the reference I described as the second assembly file."
            raw_ref = store.put_object("raw_user_submission", {"text": text})
            evidence, _ = store.append_conversation_turn(
                {}, role="user", content=text, content_ref=raw_ref, turn_id="U1"
            )
            state = initialize_consultant_bootstrap_state(
                root,
                "task-natural-language",
                raw_submission_ref=raw_ref,
                conversation_evidence=evidence,
            )
            self.assertIsNone(state["current_task"]["task_spec"])

    def test_03_resume_reconstructs_all_user_and_question_turns_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store, evidence = self._conversation(root, "task-resume")
            evidence, _ = store.append_conversation_turn(
                evidence,
                role="consultant_question",
                content="Which release?",
                question_id="Q1",
                turn_id="Q1",
            )
            evidence, _ = store.append_conversation_turn(
                evidence,
                role="user",
                content="Use release 2; keep all earlier paths.",
                question_id="Q1",
                turn_id="U2",
            )
            turns = store.read_conversation_evidence(evidence)
            self.assertEqual([item["turn_id"] for item in turns], ["U1", "Q1", "U2"])
            self.assertIn("VCF=A", turns[0]["content"])
            self.assertIn("keep all earlier paths", turns[2]["content"])

    def test_04_original_evidence_can_repair_a_taskspec_omission(self) -> None:
        base = apply_task_spec_mutation_proposal(
            None,
            mutation(op("set_goal", value="Annotate variants")),
            task_id="task-repair",
        )
        repaired = apply_task_spec_mutation_proposal(
            base,
            mutation(op("upsert_constraint", constraint={
                "constraint_id": "chromosome-naming",
                "semantic_key": "required_behavior.chromosome_naming",
                "operator": "require",
                "value": "validate_and_resolve_mismatch",
                "strength": "hard",
                "confidence": 1.0,
                "source_ref": "U1",
            })),
            task_id="task-repair",
        )
        self.assertEqual(repaired["revision"], 2)
        self.assertEqual(
            repaired["requirements"][0]["semantic_key"],
            "required_behavior.chromosome_naming",
        )

    def test_05_user_correction_replaces_current_input_without_erasing_history(self) -> None:
        first = apply_task_spec_mutation_proposal(
            None,
            mutation(
                op("set_goal", value="Annotate"),
                op("upsert_input", input={"role": "reference_fasta", "path": "/data/B.fa"}),
            ),
            task_id="task-correction",
        )
        corrected = apply_task_spec_mutation_proposal(
            first,
            mutation(op("upsert_input", input={"role": "reference_fasta", "path": "/data/B2.fa"})),
            task_id="task-correction",
        )
        paths = [row["canonical_path"] for row in corrected["inputs"] if row["role"] == "reference_fasta"]
        self.assertEqual(paths, ["/data/B2.fa"])

    def test_06_mixed_answer_can_confirm_decision_and_replace_input(self) -> None:
        base = apply_task_spec_mutation_proposal(
            None,
            mutation(op("set_goal", value="Annotate")),
            task_id="task-mixed",
        )
        updated = apply_task_spec_mutation_proposal(
            base,
            mutation(
                op("confirm_decision", decision={
                    "semantic_key": "annotation.database_name",
                    "value": "maize_custom",
                    "source_ref": "U2",
                }),
                op("upsert_input", input={"role": "reference_fasta", "path": "/data/B2.fa"}),
            ),
            task_id="task-mixed",
        )
        self.assertEqual(updated["confirmed_decisions"][0]["value"], "maize_custom")
        self.assertEqual(updated["inputs"][0]["canonical_path"], "/data/B2.fa")

    def test_07_controller_owned_ask_is_rejected_and_resolved(self) -> None:
        capabilities, _ = self.skills.get_semantic_execution_capabilities([
            "genomics/snpeff-annotation"
        ])
        result = evaluate_consultant_ask(
            {
                "action_type": "ask",
                "question": "Please provide genome_version",
                "requested_slots": ["genome_version"],
            },
            capabilities=capabilities,
            resolved_bindings={"genome_version": "biomind_session_123"},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["code"], "INVALID_ASK_CONTROLLER_OWNED_SLOT")
        self.assertEqual(result["resolved_slots"]["genome_version"], "biomind_session_123")

    def test_08_candidate_finish_applies_skill_and_mutation_before_completion(self) -> None:
        consultation = new_consultation_state("consult-8", "task-finish")
        candidate = build_consultant_candidate(
            None,
            consultation,
            mutation(
                op("set_goal", value="Annotate"),
                op("upsert_input", input={"role": "variant_vcf", "path": "/data/A.vcf"}),
            ),
            selected_skills=["genomics/snpeff-annotation"],
            task_id="task-finish",
            renderer_records=[self.skills.get_renderer_record("genomics/snpeff-annotation")],
            input_audit={"relevant": True, "status": "resolved", "resolved": ["A.vcf"]},
        )
        routing = candidate["consultation_state"]["requirements"]["skill-routing"]
        self.assertEqual(routing["status"], "satisfied")
        self.assertNotIn("skill-routing", candidate["consultation_state"]["unresolved_requirement_ids"])

    def test_09_valid_partial_progress_survives_pending_user_question(self) -> None:
        pending = {
            "question_id": "Q9",
            "question": "Which true user-owned release should be used?",
            "requested_slots": ["annotation.reference_release"],
            "owner": "user",
        }
        candidate = build_consultant_candidate(
            None,
            new_consultation_state("consult-9", "task-partial"),
            mutation(op("set_goal", value="Annotate")),
            selected_skills=["genomics/snpeff-annotation"],
            task_id="task-partial",
            pending_question=pending,
        )
        self.assertEqual(candidate["selected_skills"], ["genomics/snpeff-annotation"])
        self.assertEqual(candidate["task_spec"]["goal"], "Annotate")
        self.assertEqual(candidate["consultation_state"]["pending_question"], pending)
        self.assertEqual(candidate["consultation_state"]["completion_decision"], "needs_user")

    def test_10_equivalent_retry_without_progress_is_blocked(self) -> None:
        previous = {
            "task_revision": 2,
            "conversation_turn_count": 3,
            "feedback": [{"code": "SAME"}],
            "route": "consultant",
            "retry_pending": True,
        }
        decision = consultant_no_progress_decision(
            previous,
            task_revision=2,
            conversation_turn_count=3,
            feedback=[{"code": "SAME"}],
            route="consultant",
        )
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["code"], "CONSULTANT_NO_PROGRESS")

    def test_11_snpeff_binding_is_complete_without_user_version_or_name(self) -> None:
        record = self.skills.get_renderer_record("genomics/snpeff-annotation")
        candidate = build_consultant_candidate(
            None,
            new_consultation_state("consult-11", "session_11"),
            mutation(
                op("set_goal", value="Annotate"),
                op("upsert_input", input={"role": "variant_vcf", "path": "/data/A.vcf"}),
                op("upsert_input", input={"role": "reference_fasta", "path": "/data/B.fa"}),
                op("upsert_input", input={"role": "annotation_gtf", "path": "/data/C.gtf"}),
            ),
            selected_skills=["genomics/snpeff-annotation"],
            task_id="session_11",
            renderer_records=[record],
        )
        projection = project_task_spec_to_renderer(
            candidate["task_spec"], record["invocation"], task_id="session_11"
        )
        self.assertEqual(projection["status"], "COMPLETE")
        self.assertEqual(projection["arguments"]["genome_version"], "biomind_session_11")
        for field in ("genome_version", "genome_name", "reference_fasta", "annotation_gtf"):
            self.assertNotIn(field, projection["missing_bindings"])

    def test_12_multiple_loads_preserve_semantic_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store, evidence = self._conversation(root, "task-repeat-resume")
            raw_ref = store.read_conversation_evidence(evidence)[0]["content_ref"]
            initialize_consultant_bootstrap_state(
                root,
                "task-repeat-resume",
                raw_submission_ref=raw_ref,
                conversation_evidence=evidence,
            )
            candidate = build_consultant_candidate(
                None,
                new_consultation_state("consult-12", "task-repeat-resume"),
                mutation(op("set_goal", value="Stable goal")),
                selected_skills=["genomics/snpeff-annotation"],
                task_id="task-repeat-resume",
            )
            commit_consultant_candidate_state(root, "task-repeat-resume", candidate)
            snapshots = [load_plan_state(root, "task-repeat-resume") for _ in range(3)]
            semantic = [
                (
                    item["current_task"]["task_spec"],
                    item["task_state"]["selected_skills"],
                    item["task_state"]["consultation_state"].get("pending_question"),
                    item["task_state"].get("resolved_bindings"),
                )
                for item in snapshots
            ]
            self.assertEqual(semantic[0], semantic[1])
            self.assertEqual(semantic[1], semantic[2])


if __name__ == "__main__":
    unittest.main()

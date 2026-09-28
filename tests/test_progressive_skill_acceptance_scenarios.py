from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent_core.node_context import build_node_context
from agent_core.skill_access import (
    PROPOSE_SKILL_BINDING,
    REQUEST_SKILL_CONTEXT,
    SEARCH_SKILLS,
    SkillAccessService,
    activate_reviewed_binding,
    apply_confirmed_task_skill_change,
    apply_plan_impact_review,
    canonical_loaded_skill_documents,
    mark_continuation_resumed,
    new_skill_access_state,
    parse_typed_skill_decision,
    project_skill_context,
    review_binding_proposal,
    stale_node_result,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import canonical_current_task_spec
from prompts.manager import PromptManager


SNPEFF_SKILL = "genomics/snpeff-annotation"
ANNOVAR_SKILL = "genomics/annovar-annotation"


def _write_skill(root: Path, skill_id: str, text: str) -> None:
    path = root.joinpath(*skill_id.split("/"))
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")


def _task_spec(*, revision: int, selected_skill: str) -> dict:
    return canonical_current_task_spec({
        "task_id": "task-progressive-skill-acceptance",
        "revision": revision,
        "goal": "Annotate the admitted VCF with the user-selected method.",
        "inputs": [
            {"input_id": "variant-vcf", "role": "variant_vcf", "required": True},
            {"input_id": "reference-fasta", "role": "reference_fasta", "required": True},
            {"input_id": "annotation-gtf", "role": "annotation_gtf", "required": True},
        ],
        "parameters": {},
        "output_contracts": [{
            "output_id": "annotated-vcf",
            "role": "annotated_vcf",
            "required": True,
        }],
        "confirmed_decisions": [],
        "unresolved_questions": [],
        "requirements": [],
        "capability_intent": {"selected_skill_ids": [selected_skill]},
    })


def _runtime(*, caller: str = "consultant", invocation: str = "call-1") -> dict:
    return {
        "caller_node": caller,
        "caller_invocation_id": invocation,
        "task_revision": 12,
        "plan_revision": 7,
        "authoritative_user_turn_revision": 4,
        "current_step_id": "step-build-db",
        "attempt_id": "attempt-1",
    }


class ProgressiveSkillAcceptanceScenarios(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.skills_root = self.root / "skills"
        _write_skill(
            self.skills_root,
            SNPEFF_SKILL,
            """---
name: snpEff Annotation Fixture
description: local snpEff annotation and custom database construction
metadata:
  aliases: [snpeff]
  inputs: [variant_vcf, reference_fasta, annotation_gtf]
---
# Custom database construction
Place the reference FASTA at data/genomes/{genome}/sequences.fa.
Place the annotation GTF at data/genomes/{genome}/genes.gtf.

# Optional CDS/protein validation
When CDS and protein inputs are absent, use -noCheckCds and -noCheckProtein.
Do not ask the user for optional CDS or protein files.
""",
        )
        _write_skill(
            self.skills_root,
            ANNOVAR_SKILL,
            """---
name: ANNOVAR Annotation Fixture
description: local ANNOVAR table annotation capability
metadata:
  aliases: [annovar]
  inputs: [variant_vcf]
---
# Table annotation
Use table_annovar for the admitted VCF and declared local databases.
""",
        )
        _write_skill(
            self.skills_root,
            "genomics/annovar-annotation-alternative",
            """---
name: ANNOVAR Alternative Fixture
description: alternative local ANNOVAR annotation capability
metadata:
  aliases: [annovar]
  inputs: [variant_vcf]
---
# Protocol
Use a separately reviewed ANNOVAR protocol.
""",
        )
        self.manager = SkillManager(str(self.skills_root))
        self.service = SkillAccessService(self.manager)




    def test_explicit_snpeff_to_annovar_switch_replans_and_stales_old_generate(self) -> None:
        state, _snpeff_snapshot, _ = self.service.ensure_snapshot(
            new_skill_access_state(),
            SNPEFF_SKILL,
            scope="task",
            status="active",
            selected_at_task_revision=12,
        )
        runtime = _runtime()
        state, search_result = self.service.search_skills(
            state,
            {
                "decision": SEARCH_SKILLS,
                "query": "ANNOVAR table annotation",
                "capability_gap": "The user explicitly changed the method.",
                "desired_outcome": "Use the requested local ANNOVAR capability.",
                "requested_scope": "task",
                "relation_hint": "replace",
            },
            runtime_envelope=runtime,
        )
        annovar = next(
            item for item in search_result["candidates"]
            if item["skill_id"] == ANNOVAR_SKILL
        )
        state, excerpt_result = self.service.serve_context_request(
            state,
            {
                "decision": REQUEST_SKILL_CONTEXT,
                "reason": "Review the exact replacement procedure.",
                "requests": [{
                    "skill_id": ANNOVAR_SKILL,
                    "operation": "section",
                    "heading": "Table annotation",
                }],
            },
            runtime_envelope=runtime,
        )
        excerpt_ref = excerpt_result["results"][0]["excerpt_id"]
        state, proposal = self.service.propose_binding(
            state,
            {
                "decision": PROPOSE_SKILL_BINDING,
                "candidate_skill_id": ANNOVAR_SKILL,
                "candidate_snapshot_id": annovar["candidate_snapshot_id"],
                "requested_scope": "task",
                "relation": "replace",
                "justification": "The user explicitly said to use ANNOVAR.",
                "evidence_refs": ["user-turn-4", excerpt_ref],
            },
            runtime_envelope=runtime,
        )
        state, reviewed = review_binding_proposal(
            state,
            proposal["proposal_id"],
            {
                "decision": "ROUTE_TASK_LEVEL_CHANGE",
                "proposal_id": proposal["proposal_id"],
                "candidate_skill_id": ANNOVAR_SKILL,
                "relation": "replace",
                "reason": "The authoritative user turn explicitly changes method.",
                "task_fields_affected": ["capability_intent.selected_skill_ids"],
            },
        )
        self.assertEqual(reviewed["status"], "approved_for_task_commit")

        changed_task = apply_confirmed_task_skill_change(
            _task_spec(revision=12, selected_skill=SNPEFF_SKILL),
            [ANNOVAR_SKILL],
            source_ref="user-turn-4",
        )
        self.assertEqual(changed_task["revision"], 13)
        self.assertEqual(
            changed_task["capability_intent"]["selected_skill_ids"],
            [ANNOVAR_SKILL],
        )

        state, active_ids = activate_reviewed_binding(
            state,
            proposal["proposal_id"],
            task_revision=13,
        )
        self.assertEqual(active_ids, [ANNOVAR_SKILL])
        annovar_ref = next(
            deepcopy(dict(item))
            for item in state["selected_skill_refs"]
            if item["skill_id"] == ANNOVAR_SKILL
            and item["status"] == "active"
        )

        old_plan = {
            "plan_revision": 7,
            "plan_version": "plan_v7",
            "current_step_id": "step-prepare-snpeff",
            "steps": [
                {
                    "step_id": "step-validate-inputs",
                    "title": "Validate inputs",
                    "status": "passed",
                    "evidence_refs": ["evidence-inputs-valid"],
                },
                {"step_id": "step-prepare-snpeff", "status": "running"},
                {"step_id": "step-build-snpeff-db", "status": "pending"},
                {"step_id": "step-annotate-snpeff", "status": "pending"},
            ],
            "superseded_steps": [],
            "approval_status": "approved",
            "can_generate": True,
        }
        impact = {
            "decision": "REPLAN_SUFFIX",
            "base_task_revision": 12,
            "new_task_revision": 13,
            "base_plan_revision": 7,
            "carry_forward": [{
                "step_id": "step-validate-inputs",
                "evidence_refs": ["evidence-inputs-valid"],
            }],
            "supersede": [
                "step-prepare-snpeff",
                "step-build-snpeff-db",
                "step-annotate-snpeff",
            ],
            "replacement_suffix": [
                {"step_id": "step-prepare-annovar", "status": "pending"},
                {
                    "step_id": "step-annotate-annovar",
                    "status": "pending",
                    "skill_refs": [annovar_ref],
                    "skill_ids": [ANNOVAR_SKILL],
                },
            ],
            "reason": "The committed method change invalidates the old suffix.",
        }
        replanned = apply_plan_impact_review(
            old_plan,
            impact,
            base_task_revision=12,
            new_task_revision=13,
            valid_skill_snapshot_ids={annovar["candidate_snapshot_id"]},
            valid_skill_refs=[annovar_ref],
        )
        self.assertEqual(replanned["plan_revision"], 8)
        self.assertEqual(
            [step["step_id"] for step in replanned["steps"]],
            ["step-validate-inputs", "step-prepare-annovar", "step-annotate-annovar"],
        )
        self.assertEqual(
            {step["step_id"] for step in replanned["superseded_steps"]},
            {
                "step-prepare-snpeff",
                "step-build-snpeff-db",
                "step-annotate-snpeff",
            },
        )

        ref_status = {
            item["skill_id"]: item["status"]
            for item in state["selected_skill_refs"]
            if item["skill_id"] in {SNPEFF_SKILL, ANNOVAR_SKILL}
        }
        self.assertEqual(ref_status[SNPEFF_SKILL], "superseded")
        self.assertEqual(ref_status[ANNOVAR_SKILL], "active")

        stale = stale_node_result(
            request_task_revision=12,
            request_plan_revision=7,
            current_task_revision=13,
            current_plan_revision=8,
            caller_node="generate",
            caller_invocation_id="old-snpeff-generate",
        )
        self.assertEqual(stale["decision_result"], "STALE_NODE_RESULT")
        self.assertFalse(stale["execute"])
        self.assertFalse(stale["counts_as_failure"])
        self.assertFalse(stale["consumes_execution_retry"])

    def test_ambiguous_annovar_mention_asks_once_without_replacing_skill(self) -> None:
        state, _snapshot, _ = self.service.ensure_snapshot(
            new_skill_access_state(),
            SNPEFF_SKILL,
            scope="task",
            status="active",
            selected_at_task_revision=12,
        )
        runtime = _runtime(invocation="ambiguous-call")
        state, search_result = self.service.search_skills(
            state,
            {
                "decision": SEARCH_SKILLS,
                "query": "ANNOVAR annotation",
                "capability_gap": "The user mentioned an alternative without choosing it.",
                "desired_outcome": "Determine whether comparison or replacement is intended.",
                "requested_scope": "unknown",
                "relation_hint": "unknown",
            },
            runtime_envelope=runtime,
        )
        self.assertGreaterEqual(len(search_result["candidates"]), 2)
        annovar = next(
            item for item in search_result["candidates"]
            if item["skill_id"] == ANNOVAR_SKILL
        )
        state, excerpt_result = self.service.serve_context_request(
            state,
            {
                "decision": REQUEST_SKILL_CONTEXT,
                "reason": "Read one candidate before asking the semantic question.",
                "requests": [{
                    "skill_id": ANNOVAR_SKILL,
                    "operation": "section",
                    "heading": "Table annotation",
                }],
            },
            runtime_envelope=runtime,
        )
        state, proposal = self.service.propose_binding(
            state,
            {
                "decision": PROPOSE_SKILL_BINDING,
                "candidate_skill_id": ANNOVAR_SKILL,
                "candidate_snapshot_id": annovar["candidate_snapshot_id"],
                "requested_scope": "task",
                "relation": "replace",
                "justification": "The mention could mean replacement or comparison.",
                "evidence_refs": [
                    "user-turn-4",
                    excerpt_result["results"][0]["excerpt_id"],
                ],
            },
            runtime_envelope=runtime,
        )
        state, waiting = review_binding_proposal(
            state,
            proposal["proposal_id"],
            {
                "decision": "NEED_USER_INPUT",
                "proposal_id": proposal["proposal_id"],
                "candidate_skill_id": ANNOVAR_SKILL,
                "reason": "The user did not choose replacement versus comparison.",
                "question": "Should ANNOVAR replace snpEff, or should both methods be compared?",
                "requested_slots": ["annotation_method_intent"],
            },
        )
        self.assertEqual(waiting["status"], "waiting_user")
        self.assertEqual(
            waiting["review"]["requested_slots"],
            ["annotation_method_intent"],
        )
        self.assertEqual(waiting["review"]["question"].count("?"), 1)
        refs = state["selected_skill_refs"]
        self.assertEqual(
            [item["skill_id"] for item in refs if item["status"] == "active"],
            [SNPEFF_SKILL],
        )
        self.assertTrue(any(
            item["skill_id"] == ANNOVAR_SKILL and item["status"] == "candidate"
            for item in refs
        ))




if __name__ == "__main__":
    unittest.main()

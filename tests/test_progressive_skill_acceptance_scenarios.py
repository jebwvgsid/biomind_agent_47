from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

import bio_agents
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
from bio_agents import BioAnalystAgent
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

    async def test_snpeff_first_generate_reads_exact_rules_before_action(self) -> None:
        task_spec = _task_spec(revision=12, selected_skill=SNPEFF_SKILL)
        durable = {
            "schema_version": 1,
            "task_id": "task-progressive-skill-acceptance",
            "task_spec": deepcopy(task_spec),
            "plan_id": "plan-acceptance",
            "plan_version": "plan_v7",
            "plan_revision": 7,
            "current_step_id": "step-build-db",
            "conversation_evidence": {"user_turn_count": 4},
            "steps": [{
                "step_id": "step-build-db",
                "title": "Build the custom annotation database",
                "status": "running",
                "inputs": ["variant-vcf", "reference-fasta", "annotation-gtf"],
                "outputs": ["annotated-vcf"],
                "success_criteria": ["The admitted VCF is annotated"],
            }],
            "task_state": {
                "selected_skills": [SNPEFF_SKILL],
                "generate_failure_count": 0,
                "execution_retry_count": 0,
            },
        }
        node_context = build_node_context(
            node_type="generate",
            task_spec=task_spec,
            plan_state=durable,
            current_step_id="step-build-db",
            authoritative_user_turn_revision=4,
        )
        first_provider_text = json.dumps({
            "decision": REQUEST_SKILL_CONTEXT,
            "reason": "Need the exact database layout and optional-input rules.",
            "requests": [
                {
                    "skill_id": SNPEFF_SKILL,
                    "operation": "section",
                    "heading": "Custom database construction",
                },
                {
                    "skill_id": SNPEFF_SKILL,
                    "operation": "section",
                    "heading": "Optional CDS/protein validation",
                },
            ],
        })
        decision = parse_typed_skill_decision(first_provider_text)
        self.assertIsNotNone(decision)

        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = str(self.root)
        agent.skill_access_service = self.service
        store = {"value": deepcopy(durable)}

        def fake_load(*_args, **_kwargs):
            return deepcopy(store["value"])

        def fake_save(_data_path, _task_id, state, **_kwargs):
            store["value"] = deepcopy(state)
            return self.root / "plan_state.json"

        with (
            patch.object(bio_agents, "load_plan_state", side_effect=fake_load),
            patch.object(bio_agents, "save_plan_state", side_effect=fake_save),
        ):
            first_update = await agent._handle_skill_node_decision(
                task_id=durable["task_id"],
                caller_node="generate",
                graph_node="generate",
                decision=decision,
                node_context=node_context,
                response=AIMessage(content=first_provider_text),
                selected_skill_ids=[SNPEFF_SKILL],
            )

        self.assertEqual(first_update["next_step"], "generate")
        self.assertEqual(first_update["generate_result"], "SKILL_CONTINUATION")
        self.assertEqual(first_update["pending_action"], {})
        self.assertEqual(first_update["transition_guard"], "skill_same_node_continuation")
        self.assertNotIn("execute", first_update)
        self.assertNotIn("pending_recovery_action", first_update)
        self.assertNotIn("ask_user", first_update)
        self.assertEqual(
            store["value"]["task_state"]["generate_failure_count"], 0
        )
        self.assertEqual(
            store["value"]["task_state"]["execution_retry_count"], 0
        )
        self.assertEqual(store["value"]["steps"][0]["status"], "running")

        projection = agent._skill_context_projection(
            store["value"],
            caller_node="generate",
            current_step_id="step-build-db",
            continuation_id=first_update["skill_continuation_id"],
        )
        exact_text = "\n".join(
            str(item.get("exact_text") or "")
            for result in projection["skill_tool_results"]
            for item in result.get("results") or []
        )
        self.assertIn("data/genomes/{genome}/sequences.fa", exact_text)
        self.assertIn("data/genomes/{genome}/genes.gtf", exact_text)
        self.assertIn("-noCheckCds", exact_text)
        self.assertIn("-noCheckProtein", exact_text)
        self.assertIn("Do not ask the user", exact_text)

        def deterministic_second_generate(context: str) -> str:
            required = (
                "data/genomes/{genome}/sequences.fa",
                "data/genomes/{genome}/genes.gtf",
                "-noCheckCds",
                "-noCheckProtein",
            )
            if not all(rule in context for rule in required):
                raise AssertionError("exact Skill rules were not supplied")
            return (
                "mkdir -p data/genomes/$GENOME && "
                "cp $FASTA data/genomes/$GENOME/sequences.fa && "
                "cp $GTF data/genomes/$GENOME/genes.gtf && "
                "snpEff build -gtf22 -noCheckCds -noCheckProtein $GENOME"
            )

        second_action = deterministic_second_generate(exact_text)
        self.assertIn("sequences.fa", second_action)
        self.assertIn("genes.gtf", second_action)
        self.assertIn("-noCheckCds", second_action)
        self.assertIn("-noCheckProtein", second_action)

        access = store["value"]["task_state"]["skill_access_state"]
        recovery_projection = project_skill_context(
            access,
            caller_node="recovery",
            current_step_id="step-build-db",
        )
        corrective_projection = project_skill_context(
            access,
            caller_node="corrective_generate",
            current_step_id="step-build-db",
        )
        for inherited in (recovery_projection, corrective_projection):
            inherited_text = "\n".join(
                item["exact_text"]
                for item in inherited["relevant_skill_excerpts"]
            )
            self.assertIn("-noCheckCds", inherited_text)
            self.assertIn("-noCheckProtein", inherited_text)

    async def test_consultant_typed_context_commits_once_and_changes_next_prompt(self) -> None:
        task_spec = _task_spec(revision=12, selected_skill=SNPEFF_SKILL)
        durable = {
            "schema_version": 1,
            "task_id": "task-consultant-context-continuation",
            "task_spec": deepcopy(task_spec),
            "plan_revision": 7,
            "conversation_evidence": {"user_turn_count": 4},
            "steps": [],
            "task_state": {
                "selected_skills": [],
                "consultation_state": {
                    "skill_discovery": {
                        "candidate_skill_ids": [SNPEFF_SKILL],
                    },
                },
            },
        }
        decision = {
            "decision": REQUEST_SKILL_CONTEXT,
            "reason": "Need exact selected Skill text before deciding",
            "requests": [{
                "skill_id": SNPEFF_SKILL,
                "operation": "full",
            }],
        }
        provider_text = json.dumps(decision)
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = str(self.root)
        agent.skill_access_service = self.service
        store = {"value": deepcopy(durable)}

        def fake_load(*_args, **_kwargs):
            return deepcopy(store["value"])

        def fake_save(_data_path, _task_id, state, **_kwargs):
            store["value"] = deepcopy(state)
            return self.root / "plan_state.json"

        context = build_node_context(
            node_type="consultant",
            task_spec=task_spec,
            plan_state=durable,
            authoritative_user_turn_revision=4,
        )
        empty_prompt = PromptManager.get_consultant_v2_prompt(
            context.llm_payload.to_dict(),
            capability_context="{}",
            consultation_context="{}",
        )
        source_path = self.skills_root / "genomics" / "snpeff-annotation" / "SKILL.md"
        original_read_bytes = Path.read_bytes

        with (
            patch.object(bio_agents, "load_plan_state", side_effect=fake_load),
            patch.object(bio_agents, "save_plan_state", side_effect=fake_save),
            patch.object(
                Path,
                "read_bytes",
                autospec=True,
                side_effect=lambda path: original_read_bytes(path),
            ) as read_bytes,
        ):
            first = await agent._handle_skill_node_decision(
                task_id=durable["task_id"],
                caller_node="consultant",
                graph_node="consultant",
                decision=decision,
                node_context=context,
                response=AIMessage(content=provider_text),
            )
            second = await agent._handle_skill_node_decision(
                task_id=durable["task_id"],
                caller_node="consultant",
                graph_node="consultant",
                decision=decision,
                node_context=context,
                response=AIMessage(content=provider_text),
            )

        skill_reads = [
            call for call in read_bytes.call_args_list
            if call.args and Path(call.args[0]) == source_path
        ]
        self.assertEqual(len(skill_reads), 1)
        self.assertFalse(first["skill_decision_result"]["cache_hit"])
        self.assertTrue(second["skill_decision_result"]["cache_hit"])

        access = store["value"]["task_state"]["skill_access_state"]
        documents = canonical_loaded_skill_documents(access)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["skill_id"], SNPEFF_SKILL)
        self.assertEqual(
            documents[0]["content_hash"],
            hashlib.sha256(documents[0]["document"].encode()).hexdigest(),
        )
        discovery, derived_documents = (
            agent._derive_consultant_discovery_from_skill_access(
                durable["task_state"]["consultation_state"]["skill_discovery"],
                access,
                task_revision=12,
            )
        )
        self.assertEqual(len(discovery["loaded_skill_ids"]), 1)
        self.assertEqual(len(discovery["loaded_skill_docs"]), 1)
        self.assertEqual(len(derived_documents), 1)

        continuation_id = second["skill_continuation_id"]
        projection = agent._skill_context_projection(
            store["value"],
            caller_node="consultant",
            current_step_id="",
            continuation_id=continuation_id,
        )
        next_context = build_node_context(
            node_type="consultant",
            task_spec=task_spec,
            plan_state=store["value"],
            authoritative_user_turn_revision=4,
            skill_context=projection,
            continuation_id=continuation_id,
        )
        next_prompt = PromptManager.get_consultant_v2_prompt(
            next_context.llm_payload.to_dict(),
            capability_context="{}",
            consultation_context="{}",
        )
        self.assertIn("SKILL_CONTEXT_RESULT", next_prompt)
        self.assertIn("Custom database construction", next_prompt)
        self.assertNotEqual(
            hashlib.sha256(empty_prompt.encode()).hexdigest(),
            hashlib.sha256(next_prompt.encode()).hexdigest(),
        )

        resumed = mark_continuation_resumed(
            access,
            continuation_id,
            resumed_invocation_id="consultant-api-retry",
        )
        retry_projection = project_skill_context(
            resumed,
            caller_node="consultant",
            continuation_id=continuation_id,
        )
        self.assertEqual(len(retry_projection["skill_tool_results"]), 1)

        typed_signature = {
            key: documents[0][key]
            for key in ("skill_id", "snapshot_id", "content_hash", "document")
        }
        store["value"] = deepcopy(durable)
        with (
            patch.object(bio_agents, "load_plan_state", side_effect=fake_load),
            patch.object(bio_agents, "save_plan_state", side_effect=fake_save),
        ):
            legacy_documents, _legacy_projection = (
                agent._load_and_persist_skill_documents(
                    durable["task_id"],
                    [SNPEFF_SKILL],
                    task_revision=12,
                    request_id="legacy-load-skill-doc",
                )
            )
        legacy_access = store["value"]["task_state"]["skill_access_state"]
        self.assertEqual(
            typed_signature,
            {
                key: canonical_loaded_skill_documents(legacy_access)[0][key]
                for key in ("skill_id", "snapshot_id", "content_hash", "document")
            },
        )
        self.assertEqual(
            legacy_documents[0]["snapshot_id"],
            typed_signature["snapshot_id"],
        )

    async def test_generate_search_returns_to_same_node_without_activation(self) -> None:
        task_spec = _task_spec(revision=12, selected_skill=SNPEFF_SKILL)
        durable = {
            "schema_version": 1,
            "task_id": "task-search-continuation",
            "task_spec": deepcopy(task_spec),
            "plan_version": "plan_v7",
            "plan_revision": 7,
            "current_step_id": "step-build-db",
            "conversation_evidence": {"user_turn_count": 4},
            "steps": [{"step_id": "step-build-db", "status": "running"}],
            "task_state": {"selected_skills": [SNPEFF_SKILL]},
        }
        context = build_node_context(
            node_type="generate",
            task_spec=task_spec,
            plan_state=durable,
            current_step_id="step-build-db",
            authoritative_user_turn_revision=4,
        )
        provider_text = json.dumps({
            "decision": SEARCH_SKILLS,
            "query": "ANNOVAR table annotation",
            "capability_gap": "The selected Skill does not cover the requested comparison.",
            "desired_outcome": "Find local ANNOVAR annotation capability.",
            "requested_scope": "task",
            "relation_hint": "replace",
        })
        decision = parse_typed_skill_decision(provider_text)
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = str(self.root)
        agent.skill_access_service = self.service
        store = {"value": deepcopy(durable)}

        def fake_load(*_args, **_kwargs):
            return deepcopy(store["value"])

        def fake_save(_data_path, _task_id, state, **_kwargs):
            store["value"] = deepcopy(state)
            return self.root / "plan_state.json"

        with (
            patch.object(bio_agents, "load_plan_state", side_effect=fake_load),
            patch.object(bio_agents, "save_plan_state", side_effect=fake_save),
        ):
            update = await agent._handle_skill_node_decision(
                task_id=durable["task_id"],
                caller_node="generate",
                graph_node="generate",
                decision=decision,
                node_context=context,
                response=AIMessage(content=provider_text),
                selected_skill_ids=[SNPEFF_SKILL],
            )

        self.assertEqual(update["next_step"], "generate")
        self.assertEqual(update["pending_action"], {})
        self.assertEqual(
            update["skill_decision_result"]["status"], "MATCHES_FOUND"
        )
        self.assertEqual(store["value"]["task_spec"], durable["task_spec"])
        self.assertEqual(store["value"]["plan_revision"], 7)
        refs = store["value"]["task_state"]["skill_access_state"][
            "selected_skill_refs"
        ]
        self.assertEqual(
            [item["skill_id"] for item in refs if item["status"] == "active"],
            [SNPEFF_SKILL],
        )
        self.assertTrue(any(
            item["skill_id"] == ANNOVAR_SKILL and item["status"] == "candidate"
            for item in refs
        ))

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

    def test_post_bootstrap_taskspec_change_creates_restricted_plan_impact(self) -> None:
        prior_task = _task_spec(revision=12, selected_skill=SNPEFF_SKILL)
        changed_task = deepcopy(prior_task)
        changed_task["revision"] = 13
        changed_task["goal"] = "Annotate and compare the admitted variants."
        active_ref = {
            "skill_id": SNPEFF_SKILL,
            "snapshot_id": "snapshot-snpeff",
            "source_path": "skills/genomics/snpeff-annotation/SKILL.md",
            "content_hash": "sha256:fixture-snpeff",
            "scope": "task",
            "status": "active",
            "selected_at_task_revision": 12,
        }
        access = new_skill_access_state()
        access["selected_skill_refs"] = [deepcopy(active_ref)]
        plan_steps = [{"step_id": "step-build-db", "status": "running"}]
        durable = {
            "task_id": "task-generic-impact",
            "current_task": {
                "task_spec": deepcopy(prior_task),
                "task_revision": 12,
            },
            "plan_revision": 7,
            "plan_version": "plan_v7",
            "current_step_id": "step-build-db",
            "state_generation": 5,
            "conversation_evidence": {"user_turn_count": 4},
            "steps": deepcopy(plan_steps),
            "current_plan": {
                "plan_spec": {"steps": deepcopy(plan_steps)},
                "plan_revision": 7,
                "plan_semantic_hash": "sha256:fixture-plan-semantic",
                "plan_record_hash": "sha256:fixture-plan-record",
            },
            "selected_skill_ids": [SNPEFF_SKILL],
            "selected_skill_refs": [deepcopy(active_ref)],
            "skill_access_state": deepcopy(access),
            "task_state": {
                "selected_skills": [SNPEFF_SKILL],
                "selected_skill_refs": [deepcopy(active_ref)],
                "skill_access_state": deepcopy(access),
            },
        }
        captured = {}

        def fake_commit(_data_path, _task_id, candidate):
            captured.update(deepcopy(dict(candidate)))
            task_state = {
                **deepcopy(durable["task_state"]),
                "selected_skills": list(candidate.get("selected_skills") or []),
                "selected_skill_refs": deepcopy(list(
                    candidate.get("selected_skill_refs") or []
                )),
                "skill_access_state": deepcopy(dict(
                    candidate.get("skill_access_state") or {}
                )),
                "consultation_state": deepcopy(dict(
                    candidate.get("consultation_state") or {}
                )),
                "resolved_bindings": deepcopy(dict(
                    candidate.get("resolved_bindings") or {}
                )),
                "pending_question": deepcopy(dict(
                    candidate.get("pending_question") or {}
                )),
                "pending_plan_impact_review": deepcopy(dict(
                    candidate.get("pending_plan_impact_review") or {}
                )),
            }
            return {
                **deepcopy(durable),
                "current_task": {
                    "task_spec": deepcopy(candidate["task_spec"]),
                    "task_revision": 13,
                },
                "task_state": task_state,
                "state_generation": 6,
            }

        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = str(self.root)
        with (
            patch.object(
                bio_agents, "load_plan_state", return_value=deepcopy(durable)
            ),
            patch.object(
                bio_agents,
                "commit_consultant_candidate_state",
                side_effect=fake_commit,
            ),
        ):
            update = agent._commit_consultant_update(
                durable["task_id"],
                {
                    "task_spec": changed_task,
                    "selected_skills": [SNPEFF_SKILL],
                    "next_step": "planner",
                },
            )

        pending = captured["pending_plan_impact_review"]
        self.assertEqual(pending["impact_kind"], "taskspec_semantic_change")
        self.assertEqual(pending["base_task_revision"], 12)
        self.assertEqual(pending["new_task_revision"], 13)
        self.assertEqual(pending["base_plan_revision"], 7)
        self.assertEqual(update["next_step"], "planner")
        self.assertEqual(
            update["pending_plan_impact_review"]["proposal_id"],
            pending["proposal_id"],
        )

    async def test_step_scoped_impact_is_stale_not_rebased_after_step_change(self) -> None:
        task_spec = _task_spec(revision=12, selected_skill=SNPEFF_SKILL)
        access = new_skill_access_state()
        access["continuations"]["continuation-old"] = {
            "continuation_id": "continuation-old",
            "caller_node": "generate",
            "status": "served",
        }
        pending = {
            "mode": "plan_impact_review",
            "proposal_id": "proposal-old-step",
            "base_task_revision": 12,
            "new_task_revision": 12,
            "base_plan_revision": 7,
            "base_user_turn_revision": 4,
            "skill_change": {
                "proposal": {
                    "proposal_id": "proposal-old-step",
                    "requested_scope": "step",
                    "relation": "supplement",
                    "base_step_id": "step-old",
                    "target_step_id": "step-old",
                },
            },
            "status": "pending",
        }
        durable = {
            "task_id": "task-step-impact-stale",
            "task_spec": task_spec,
            "plan_revision": 8,
            "plan_version": "plan_v8",
            "current_step_id": "step-successor",
            "conversation_evidence": {"user_turn_count": 4},
            "steps": [{"step_id": "step-successor", "status": "running"}],
            "task_state": {
                "skill_access_state": access,
                "skill_continuation_id": "continuation-old",
                "pending_plan_impact_review": pending,
            },
        }
        store = {"value": deepcopy(durable)}

        def fake_load(*_args, **_kwargs):
            return deepcopy(store["value"])

        def fake_save(_data_path, _task_id, value, **_kwargs):
            store["value"] = deepcopy(value)
            return self.root / "plan_state.json"

        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = str(self.root)
        with (
            patch.object(bio_agents, "load_plan_state", side_effect=fake_load),
            patch.object(bio_agents, "save_plan_state", side_effect=fake_save),
        ):
            update = await agent._run_planner_plan_impact_review(
                {},
                task_id=durable["task_id"],
                durable=durable,
                pending=pending,
            )

        self.assertEqual(
            update["stale_node_result"]["decision_result"],
            "STALE_NODE_RESULT",
        )
        self.assertIn(
            "step_id",
            json.loads(update["messages"][0].content)[
                "changed_preconditions"
            ],
        )
        self.assertEqual(update["next_step"], "planner")
        self.assertNotIn(
            "pending_plan_impact_review",
            store["value"]["task_state"],
        )
        continuation = store["value"]["task_state"]["skill_access_state"][
            "continuations"
        ]["continuation-old"]
        self.assertEqual(continuation["status"], "stale")


if __name__ == "__main__":
    unittest.main()

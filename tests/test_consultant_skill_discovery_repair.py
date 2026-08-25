import unittest
from pathlib import Path

from agent_core.consultation_state import (
    consultant_no_progress_decision,
    evaluate_consultant_ask,
)
from agent_core.formal_artifact_resolver import resolve_formal_upstream_artifacts
from agent_core.skill_discovery import (
    CAPABILITY_GAP,
    CANDIDATES_FOUND,
    EXPANDING_DISCOVERY,
    READING_CANDIDATES,
    REASONING_ESCALATION,
    SEARCHING,
    SELECTED,
    build_discovery_escalation_context,
    capability_gap_allowed,
    discovery_input_roles,
    discovery_next_actions,
    new_skill_discovery_state,
    record_catalog_expansion,
    record_loaded_skill_doc,
    record_reasoning_escalation,
    record_selected_skills,
    record_skill_search,
    reject_premature_finish,
)
from agent_core.skill_manager import SkillManager
from prompts.manager import PromptManager


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT.parent / "benchmarks" / "01.测试" / "04.重测序" / "Part2.vairant_annotation" / "data"


class ConsultantSkillDiscoveryRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = SkillManager(str(ROOT / "skills"))

    def test_a_vague_chinese_variant_request_ranks_and_selects_snpeff_without_ask(self):
        paths = [
            BENCHMARK_ROOT / "all.filtered.snp.vcf",
            BENCHMARK_ROOT / "genome.fasta",
            BENCHMARK_ROOT / "genome.gtf",
        ]
        self.assertTrue(all(path.is_file() for path in paths))
        request = (
            "这个 VCF 帮我看看突变影响哪些基因和功能。\n"
            + "\n".join(str(path) for path in paths)
        )
        audit = resolve_formal_upstream_artifacts(
            request,
            work_dir=ROOT,
            project_root=ROOT.parent,
        )
        roles = discovery_input_roles(input_audit=audit)
        self.assertTrue({"variant_vcf", "reference_fasta", "annotation_gtf"} <= set(roles))
        cards = self.manager.rank_skill_candidates(
            request,
            input_roles=roles,
            max_results=8,
        )
        self.assertEqual(cards[0]["skill_id"], "genomics/snpeff-annotation")
        self.assertIn("input role matched: variant_vcf", cards[0]["reasons"])

        state = record_skill_search(
            new_skill_discovery_state(),
            query="existing VCF functional effect annotation",
            candidate_cards=cards,
            input_roles=roles,
            task_hypothesis="functional annotation of an existing VCF",
        )
        document = self.manager.load_skill("genomics/snpeff-annotation")
        state = record_loaded_skill_doc(
            state,
            skill_id="genomics/snpeff-annotation",
            document=document,
        )
        state = record_selected_skills(state, ["genomics/snpeff-annotation"])
        self.assertEqual(state["routing_status"], SELECTED)
        self.assertFalse(state["capability_gap"])
        self.assertNotIn("ASK_USER", discovery_next_actions(state))

    def test_b_first_empty_search_requires_reformulation_not_terminal_or_ask(self):
        state = record_skill_search(
            new_skill_discovery_state(),
            query="ambiguous analysis phrase",
            candidate_cards=[],
        )
        self.assertEqual(state["routing_status"], SEARCHING)
        self.assertEqual(discovery_next_actions(state), ["SEARCH_SKILLS"])
        self.assertFalse(state["capability_gap"])
        self.assertFalse(capability_gap_allowed(state))

    def test_c_two_empty_searches_can_expand_genomics_catalog_and_find_snpeff(self):
        state = record_skill_search(
            new_skill_discovery_state(), query="query one", candidate_cards=[]
        )
        state = record_skill_search(state, query="query two", candidate_cards=[])
        self.assertEqual(state["routing_status"], EXPANDING_DISCOVERY)
        cards = self.manager.list_skill_cards("genomics", max_results=64)
        state = record_catalog_expansion(
            state,
            category="genomics",
            candidate_cards=cards,
        )
        self.assertEqual(state["routing_status"], CANDIDATES_FOUND)
        self.assertIn("genomics/snpeff-annotation", state["candidate_skill_ids"])
        document = self.manager.load_skill("genomics/snpeff-annotation")
        state = record_loaded_skill_doc(
            state,
            skill_id="genomics/snpeff-annotation",
            document=document,
        )
        state = record_selected_skills(state, ["genomics/snpeff-annotation"])
        self.assertEqual(state["routing_status"], SELECTED)

    def test_d_multiple_candidates_require_document_evidence_before_selection(self):
        cards = [
            {"skill_id": "candidate/a", "score": 10, "reasons": ["semantic match"]},
            {"skill_id": "candidate/b", "score": 10, "reasons": ["semantic match"]},
            {"skill_id": "candidate/c", "score": 9, "reasons": ["input match"]},
        ]
        state = record_skill_search(
            new_skill_discovery_state(), query="task", candidate_cards=cards
        )
        self.assertEqual(discovery_next_actions(state), ["LOAD_SKILL_DOC"])
        state = record_loaded_skill_doc(
            state,
            skill_id="candidate/b",
            document="documented compatible input/output contract",
        )
        self.assertEqual(state["routing_status"], READING_CANDIDATES)
        self.assertIn("SELECT_SKILL", discovery_next_actions(state))
        state = record_selected_skills(state, ["candidate/b"])
        self.assertEqual(state["routing_status"], SELECTED)

    def test_e_only_required_user_owned_scientific_decision_can_ask(self):
        capability = {
            "required_bindings": [
                {
                    "name": "analysis_goal",
                    "required": True,
                    "required_for_execution": True,
                    "owner": "user",
                    "user_input_required": True,
                },
                {
                    "name": "skill-routing",
                    "required": True,
                    "required_for_execution": True,
                    "owner": "controller",
                    "user_input_required": False,
                },
            ]
        }
        allowed = evaluate_consultant_ask(
            {"requested_slots": ["analysis_goal"]},
            capabilities=[capability],
        )
        forbidden = evaluate_consultant_ask(
            {"requested_slots": ["skill-routing"]},
            capabilities=[capability],
        )
        self.assertTrue(allowed["allowed"])
        self.assertEqual(forbidden["code"], "INVALID_ASK_CONTROLLER_OWNED_SLOT")

    def test_f_internal_retrieval_failure_rejects_finish_without_asking_user(self):
        state = record_skill_search(
            new_skill_discovery_state(), query="first", candidate_cards=[]
        )
        state, guidance = reject_premature_finish(state)
        self.assertEqual(guidance["owner"], "consultant_internal")
        self.assertEqual(guidance["reason"], "skill discovery not exhausted")
        self.assertNotIn("ASK_USER", guidance["allowed_or_required_next_actions"])
        self.assertFalse(state["capability_gap"])

    def test_g_reasoning_escalation_is_bounded_and_context_is_narrow(self):
        state = record_skill_search(
            new_skill_discovery_state(), query="first", candidate_cards=[]
        )
        state = record_skill_search(state, query="second", candidate_cards=[])
        state = record_catalog_expansion(state, category="genomics", candidate_cards=[])
        state = record_reasoning_escalation(state, trigger="normal discovery exhausted")
        self.assertEqual(state["routing_status"], REASONING_ESCALATION)
        self.assertEqual(state["reasoning_level"], "escalated")
        self.assertEqual(state["reasoning_escalation_count"], 1)
        context = build_discovery_escalation_context(
            raw_user_request="analyse these variants",
            task_spec={"goal": "variant analysis", "inputs": []},
            input_inventory={"status": "resolved", "resolved_inputs": []},
            discovery_state=state,
        )
        self.assertEqual(context["context_type"], "ConsultantDiscoveryEscalationContext")
        self.assertNotIn("messages", context)
        self.assertNotIn("session_history", context)
        prompt = PromptManager.get_consultant_discovery_escalation_prompt(context)
        self.assertIn("narrow reasoning invocation", prompt)
        self.assertNotIn("Planner/Generate/Execute history", context)

    def test_h_identical_discovery_state_still_triggers_no_progress(self):
        state = record_skill_search(
            new_skill_discovery_state(), query="same", candidate_cards=[]
        )
        first = consultant_no_progress_decision(
            None,
            task_revision=1,
            conversation_turn_count=1,
            feedback=[{"code": "DISCOVERY_REQUIRED"}],
            route="consultant",
            discovery_state=state,
        )
        prior = dict(first["guard"])
        prior["retry_pending"] = True
        second = consultant_no_progress_decision(
            prior,
            task_revision=1,
            conversation_turn_count=1,
            feedback=[{"code": "DISCOVERY_REQUIRED"}],
            route="consultant",
            discovery_state=state,
        )
        self.assertFalse(second["allow"])
        self.assertEqual(second["code"], "CONSULTANT_NO_PROGRESS")

    def test_i_new_query_and_candidate_are_progress_without_task_revision_change(self):
        first_state = record_skill_search(
            new_skill_discovery_state(), query="query A", candidate_cards=[]
        )
        first = consultant_no_progress_decision(
            None,
            task_revision=1,
            conversation_turn_count=1,
            feedback=[],
            route="consultant",
            discovery_state=first_state,
        )
        prior = dict(first["guard"])
        prior["retry_pending"] = True
        second_state = record_skill_search(
            first_state,
            query="query B",
            candidate_cards=[{"skill_id": "genomics/snpeff-annotation", "score": 20}],
        )
        second = consultant_no_progress_decision(
            prior,
            task_revision=1,
            conversation_turn_count=1,
            feedback=[],
            route="consultant",
            discovery_state=second_state,
        )
        self.assertTrue(second["allow"])

    def test_j_runtime_prompt_does_not_make_generic_fields_mechanically_required(self):
        prompt = PromptManager.get_consultant_v2_prompt(
            {"evidence": [], "task": {}, "feedback": {}},
            capability_context="{}",
        )
        self.assertIn("Generic fields", prompt)
        self.assertIn("such as contrast, resolution", prompt)
        self.assertIn("required unless the current scientific task", prompt)
        self.assertIn("INTERNAL_SKILL_DISCOVERY_UNCERTAINTY", prompt)
        self.assertIn('"action_type":"search_skills"', prompt)


if __name__ == "__main__":
    unittest.main()

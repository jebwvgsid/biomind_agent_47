import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.context_budget import RequiredContextOverflow
from agent_core.task_spec import (
    create_task_spec,
    format_effective_user_facts_context,
    merge_compiled_constraints,
    validate_compiled_constraints,
)
from bio_agents import BioAnalystAgent
from config.config import (
    CONSULTANT_CANDIDATE_SKILL_LIMIT,
    CONSULTANT_HISTORY_TOKEN_BUDGET,
    CONSULTANT_SKILL_TOKEN_BUDGET,
)
from prompts.manager import PromptManager
from agent_core.workflow_manager import WorkflowManager
from agent_core.skill_manager import SkillManager


class ConsultantContextBudgetTests(unittest.TestCase):
    def test_nominal_pools_are_100k_tokens_each(self):
        self.assertEqual(CONSULTANT_HISTORY_TOKEN_BUDGET, 100_000)
        self.assertEqual(CONSULTANT_SKILL_TOKEN_BUDGET, 100_000)
        self.assertGreaterEqual(CONSULTANT_CANDIDATE_SKILL_LIMIT, 64)

        budget = BioAnalystAgent._consultant_effective_pool_budgets(200_000)
        self.assertEqual(budget["nominal_total_tokens"], 200_000)
        self.assertEqual(budget["effective_input_tokens"], 191_808)
        self.assertEqual(budget["reserved_headroom_tokens"], 8_192)

        larger_context_budget = BioAnalystAgent._consultant_effective_pool_budgets(300_000)
        self.assertEqual(larger_context_budget["effective_input_tokens"], 200_000)
        self.assertEqual(larger_context_budget["history_effective_tokens"], 100_000)
        self.assertEqual(larger_context_budget["skill_effective_tokens"], 100_000)
        self.assertGreaterEqual(larger_context_budget["reserved_headroom_tokens"], 8_192)

    def test_original_human_and_loaded_skill_doc_remain_fully_visible(self):
        human = "用户明确要求使用 genomics/snpeff-annotation。" + "甲" * 10_000
        skill_doc = (
            '<loaded_skill_doc skill_id="genomics/snpeff-annotation">\n'
            + "complete skill guidance\n" * 1_000
            + "</loaded_skill_doc>"
        )
        messages = [
            HumanMessage(content=human),
            AIMessage(content="LOAD_SKILL_DOC genomics/snpeff-annotation"),
            SystemMessage(content=skill_doc),
        ]

        visible = BioAnalystAgent._consultant_visible_messages(messages)

        self.assertEqual(visible[0].content, human)
        self.assertEqual(visible[-1].content, skill_doc)

    def test_history_and_skill_components_share_their_pool(self):
        human = "authoritative current request " + "H" * 2_000
        task_spec = "[EFFECTIVE USER FACTS]\n" + "T" * 4_000
        preloaded = "<preloaded_skill_docs>\n" + "S" * 5_000 + "\n</preloaded_skill_docs>"
        catalog = "catalog entry\n" * 2_000
        memory = "old memory paragraph\n\n" * 1_000

        history, skills, visible, report = BioAnalystAgent._pack_consultant_context(
            [HumanMessage(content=human)],
            history_components={"task_spec": task_spec, "memory_context": memory},
            skill_components={"preloaded_skill_docs": preloaded, "skill_catalog": catalog},
            history_max_chars=12_000,
            skill_max_chars=10_000,
        )

        self.assertEqual(history["task_spec"], task_spec)
        self.assertEqual(visible[0].content, human)
        self.assertEqual(skills["preloaded_skill_docs"], preloaded)
        self.assertLessEqual(report["history"]["packed_chars"], 12_000)
        self.assertLessEqual(report["skills"]["packed_chars"], 10_000)
        self.assertLess(len(history["memory_context"]), len(memory))
        self.assertLess(len(skills["skill_catalog"]), len(catalog))

    def test_required_user_authority_overflow_is_explicit(self):
        with self.assertRaises(RequiredContextOverflow):
            BioAnalystAgent._pack_consultant_context(
                [HumanMessage(content="U" * 4_000)],
                history_components={"task_spec": "T" * 4_000},
                skill_components={},
                history_max_chars=6_000,
                skill_max_chars=6_000,
            )

    def test_token_pool_does_not_treat_chinese_as_four_chars_per_token(self):
        history, _, visible, report = BioAnalystAgent._pack_consultant_context(
            [HumanMessage(content="必须保留的用户请求")],
            history_components={
                "task_spec": "[EFFECTIVE USER FACTS]\n{}",
                "memory_context": "汉" * 20_000,
            },
            skill_components={},
            history_max_chars=80_000,
            skill_max_chars=8_000,
            history_max_tokens=2_000,
            skill_max_tokens=2_000,
        )

        self.assertEqual(visible[0].content, "必须保留的用户请求")
        self.assertLessEqual(report["history"]["estimated_tokens"], 2_000)
        self.assertLess(len(history["memory_context"]), 20_000)

        with self.assertRaises(RequiredContextOverflow):
            BioAnalystAgent._pack_consultant_context(
                [HumanMessage(content="中" * 3_000)],
                history_components={"task_spec": "{}"},
                skill_components={},
                history_max_chars=20_000,
                skill_max_chars=2_000,
                history_max_tokens=1_000,
                skill_max_tokens=1_000,
            )

    def test_typed_constraint_projection_is_not_misreported_as_missing_user_data(self):
        spec = create_task_spec(
            "Use genomics/snpeff-annotation with -gtf22 and write annotated.vcf",
            task_id="consultant-budget-task",
        )
        validated = validate_compiled_constraints([{
            "constraint_id": "require-gtf22",
            "semantic_key": "required_behavior.snpeff_gtf22",
            "operator": "eq",
            "value": True,
            "strength": "hard",
            "confidence": 1.0,
            "source_text": "必须使用 -gtf22",
        }])
        self.assertTrue(validated["valid"], validated["errors"])
        spec = merge_compiled_constraints(spec, validated["constraints"], metrics=validated["metrics"])

        rendered = format_effective_user_facts_context(
            spec,
            audience="consultant",
            max_chars=400_000,
            include_task_goal=False,
        )

        self.assertIn("required_behavior.snpeff_gtf22", rendered)
        self.assertIn("必须使用 -gtf22", rendered)
        self.assertNotIn("Use genomics/snpeff-annotation with -gtf22", rendered)
        self.assertNotIn("CONTEXT_BUDGET_UNSATISFIED", rendered)

    def test_static_prompt_is_compact_and_has_the_ask_and_preload_gates(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertLessEqual(len(prompt), 6_000)
        self.assertIn("The current Human message is the natural-language authority", prompt)
        self.assertIn("Do not ask the user to confirm file existence", prompt)
        self.assertIn("<preloaded_skill_docs>", prompt)
        self.assertIn("never request the same skill document again", prompt)

    def test_memory_retrieval_excludes_current_task_and_dedupes_current_request(self):
        class _Archive:
            def search(self, *args, **kwargs):
                self.limit = kwargs["limit"]
                return [
                    SimpleNamespace(task_id="current"),
                    SimpleNamespace(task_id="older"),
                ]

            @staticmethod
            def format_hits(hits, title):
                return title + ":" + ",".join(hit.task_id for hit in hits)

        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.session_archive = _Archive()
        context = agent._archive_prompt_context(
            None,
            "annotate variants",
            project_id="project",
            limit=4,
            exclude_task_id="current",
        )
        request = "R" * 140
        replay = f"prefix\n{request}\nsuffix"

        self.assertIn("older", context)
        self.assertNotIn("current", context)
        self.assertEqual(agent.session_archive.limit, 12)
        deduped = agent._dedupe_current_query_from_retrieval(replay, request)
        self.assertNotIn(request, deduped)
        self.assertIn("lossless Human message", deduped)

    def test_generic_benchmark_language_does_not_inject_an_unrelated_workflow(self):
        manager = WorkflowManager("workflows")
        query = (
            "Use genomics/snpeff-annotation on all.filtered.snp.vcf with a custom genome. "
            "Generate report.md and stage_status.tsv; do not fabricate analysis outputs."
        )

        self.assertEqual(manager.candidate_workflow_ids_for_prompt(query, max_results=2), [])

    def test_loaded_skill_header_excludes_python_cache_artifacts(self):
        loaded = SkillManager("skills").load_skill("genomics/snpeff-annotation")

        self.assertIn("scripts/render_snpeff_annotation_scripts.py", loaded)
        self.assertNotIn("__pycache__", loaded)
        self.assertNotIn(".pyc", loaded)


if __name__ == "__main__":
    unittest.main()

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
from config.config import (
    CONSULTANT_CANDIDATE_SKILL_LIMIT,
    CONSULTANT_HISTORY_TOKEN_BUDGET,
    CONSULTANT_SKILL_TOKEN_BUDGET,
)
from prompts.manager import PromptManager
from agent_core.workflow_manager import WorkflowManager
from agent_core.skill_manager import SkillManager


class ConsultantContextBudgetTests(unittest.TestCase):





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

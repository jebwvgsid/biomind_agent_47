import json
import unittest

from agent_core.context_budget import (
    ContextBlock,
    ContextBudgetManager,
    RequiredContextOverflow,
    compress_json,
    compress_markdown_sections,
    compress_paragraphs,
    compress_tagged_text,
)
from prompts.manager import PromptManager


class ContextBudgetTests(unittest.TestCase):
    def test_optional_blocks_are_removed_before_pinned_constraints(self):
        task_spec = json.dumps({"must_not_do": ["不得使用 --nobalance"]}, ensure_ascii=False)
        result = ContextBudgetManager().pack(
            [
                ContextBlock("task", "task_spec", task_spec, 100, pinned=True, droppable=False, compress_fn=compress_json),
                ContextBlock("rag", "rag", "old retrieval\n\n" * 1000, 10, compress_fn=compress_paragraphs),
            ],
            500,
        )

        self.assertEqual(json.loads(result.contents["task"])["must_not_do"], ["不得使用 --nobalance"])
        self.assertIn(
            next(block for block in result.report["blocks"] if block["id"] == "rag")["action"],
            {"compressed", "dropped"},
        )

    def test_required_overflow_is_explicit(self):
        with self.assertRaises(RequiredContextOverflow) as raised:
            ContextBudgetManager().pack(
                [ContextBlock("required", "task_spec", "x" * 2000, 100, pinned=True, droppable=False)],
                100,
            )

        self.assertTrue(raised.exception.report["overflow"])
        self.assertIn("required", raised.exception.report["required_blocks"])

    def test_markdown_compression_preserves_outer_xml_wrapper(self):
        body = "<active_skills_context>\n" + "\n\n".join(
            f"## Section {index}\n" + ("line\n" * 30) for index in range(20)
        ) + "\n</active_skills_context>"
        compressed = compress_markdown_sections(body, 1200)

        self.assertTrue(compressed.startswith("<active_skills_context>"))
        self.assertTrue(compressed.endswith("</active_skills_context>"))

    def test_observation_compression_preserves_message_contract(self):
        observation = "<observation>\n" + ("normal line\n" * 1000) + "ERROR: failed here\n" + ("tail\n" * 100) + "</observation>"
        compressed = compress_tagged_text(observation, 1200)

        self.assertTrue(compressed.startswith("<observation>"))
        self.assertTrue(compressed.endswith("</observation>"))
        self.assertIn("ERROR: failed here", compressed)

    def test_generate_prompt_uses_block_budget_without_final_slice(self):
        task_spec = json.dumps(
            {
                "revision": 4,
                "goal": "run current step",
                "must_not_do": ["不得使用 --nobalance"],
                "output_contracts": ["任一样本失败时返回非零退出码"],
            },
            ensure_ascii=False,
        )
        skill = "<active_skills_context>\n" + "\n\n".join(
            f"## Runtime section {index}\n" + ("command detail\n" * 100) for index in range(20)
        ) + "\n</active_skills_context>"
        prompt, report = PromptManager.build_ephemeral_context_result(
            task_spec_context=task_spec,
            current_plan="<current_step_context>\nstep_id: step-4\naction: execute\nsuccess_criteria: output exists\n</current_step_context>",
            plan_progress="step-4 running",
            loaded_skills_instructions=skill,
            rag_context="irrelevant old rag\n\n" * 10000,
            archive_context="old execution detail\n\n" * 10000,
            current_action_context="task_revision: 4\nstep_id: step-4\naction: EXECUTE\n只执行当前步骤",
            max_chars=26000,
        )

        self.assertLessEqual(len(prompt), 26000)
        self.assertIn("不得使用 --nobalance", prompt)
        self.assertIn("任一样本失败时返回非零退出码", prompt)
        self.assertIn("只执行当前步骤", prompt)
        self.assertEqual(prompt.count("<active_skills_context>"), prompt.count("</active_skills_context>"))
        self.assertGreater(prompt.index("<optional_retrieved_context>"), prompt.index("<current_action>"))
        self.assertFalse(report["overflow"])
        self.assertNotIn("...[truncated]", prompt)


if __name__ == "__main__":
    unittest.main()

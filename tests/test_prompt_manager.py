import unittest

from prompts.manager import AIMessage, PromptManager


class _FakePackageManager:
    work_dir = "/work/project"

    def list_installed_tools(self):
        return "samtools: ready"

    def get_shim_dir_str(self):
        return "/work/shims"


class PromptManagerTests(unittest.TestCase):
    def test_stable_prompt_excludes_ephemeral_context(self):
        stable = PromptManager.build_stable_system_prompt(
            package_manager=_FakePackageManager(),
            work_dir="/work/project",
            core_memory="CORE_FACT: project_alpha.gff3 is at /data/project_alpha.gff3",
            agents_md_context="AGENTS_RULE: keep edits scoped",
            current_time="2026-05-02 10:00:00",
        )

        self.assertIn("CORE_FACT", stable)
        self.assertIn("AGENTS_RULE", stable)
        self.assertNotIn("RAG_HIT", stable)
        self.assertNotIn("TASK_MEMORY_HIT", stable)
        self.assertNotIn("ACTIVE_SKILL_DOC", stable)
        self.assertNotIn("supervisor_context", stable)
        self.assertNotIn("current_plan", stable)

    def test_ephemeral_context_contains_dynamic_sections(self):
        ephemeral = PromptManager.build_ephemeral_context(
            rag_context="RAG_HIT",
            history=[AIMessage(content="<observation>❌ failed</observation>")],
            loaded_skills_instructions="ACTIVE_SKILL_DOC",
            long_term_memory="LONG_TERM_HIT",
            task_memory="TASK_MEMORY_HIT",
            related_memory="RELATED_MEMORY_HIT",
            wiki_context="WIKI_HIT",
            archive_context="ARCHIVE_HIT",
            supervisor_context="<supervisor_context>WARN</supervisor_context>",
            current_plan="Run one checked step.",
        )

        self.assertIn("<ephemeral_context>", ephemeral)
        self.assertIn("RAG_HIT", ephemeral)
        self.assertIn("ACTIVE_SKILL_DOC", ephemeral)
        self.assertIn("TASK_MEMORY_HIT", ephemeral)
        self.assertIn("WIKI_HIT", ephemeral)
        self.assertIn("ARCHIVE_HIT", ephemeral)
        self.assertIn("WARN", ephemeral)
        self.assertIn("<recovery_mode>", ephemeral)
        self.assertIn("Run one checked step.", ephemeral)


if __name__ == "__main__":
    unittest.main()

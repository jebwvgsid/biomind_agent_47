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
        self.assertIn("Wiki claims with evidence", stable)
        self.assertNotIn("RAG_HIT", stable)
        self.assertNotIn("TASK_MEMORY_HIT", stable)
        self.assertNotIn("ACTIVE_SKILL_DOC", stable)
        self.assertNotIn("supervisor_context", stable)
        self.assertNotIn("current_plan", stable)

    def test_ephemeral_context_contains_dynamic_sections(self):
        ephemeral = PromptManager.build_ephemeral_context(
            rag_context="RAG_HIT",
            history=[AIMessage(content="<observation>❌ failed</observation>")],
            dynamic_tool_context=PromptManager._build_tool_context(_FakePackageManager(), "/work/project"),
            loaded_skills_instructions="ACTIVE_SKILL_DOC",
            long_term_memory="LONG_TERM_HIT",
            task_memory="TASK_MEMORY_HIT",
            related_memory="RELATED_MEMORY_HIT",
            wiki_context="WIKI_HIT",
            wiki_digest="WIKI_DIGEST_HIT",
            archive_context="ARCHIVE_HIT",
            supervisor_context="<supervisor_context>WARN</supervisor_context>",
            current_plan="Run one checked step.",
            plan_progress="- [>] Step 1: Run one checked step.",
        )

        self.assertIn("<ephemeral_context>", ephemeral)
        self.assertIn("<latest_environment_context>", ephemeral)
        self.assertIn("samtools: ready", ephemeral)
        self.assertIn("RAG_HIT", ephemeral)
        self.assertIn("ACTIVE_SKILL_DOC", ephemeral)
        self.assertIn("TASK_MEMORY_HIT", ephemeral)
        self.assertIn("step-local runtime context", ephemeral)
        self.assertIn("broad memory is not reloaded on every Generate turn", ephemeral)
        self.assertIn("WIKI_HIT", ephemeral)
        self.assertIn("<wiki_digest>", ephemeral)
        self.assertIn("WIKI_DIGEST_HIT", ephemeral)
        self.assertIn("Wiki evidence", ephemeral)
        self.assertIn("ARCHIVE_HIT", ephemeral)
        self.assertIn("WARN", ephemeral)
        self.assertIn("<recovery_mode>", ephemeral)
        self.assertIn("Run one checked step.", ephemeral)
        self.assertIn("<plan_progress>", ephemeral)
        self.assertIn("- [>] Step 1: Run one checked step.", ephemeral)
        self.assertIn("Follow the first running or pending item", ephemeral)
        self.assertIn("<activate_batch batch_id=\"N\" />", ephemeral)
        self.assertIn("Only follow skills inside <active_skills>", ephemeral)

    def test_consultant_prompt_contains_wiki_memory_context(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
            installed_tools_str="samtools: ready",
            work_dir="/work/project",
            max_skills=2,
            wiki_memory_context="WIKI_MEMORY_CONTEXT",
        )

        self.assertIn("[CONSULTANT MEMORY RETRIEVAL CONTEXT]", prompt)
        self.assertIn("WIKI_MEMORY_CONTEXT", prompt)

    def test_consultant_prompt_contains_workflow_first_protocol(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
            workflow_catalog="workflow_id: scrna_standard_analysis",
        )

        self.assertIn("<workflow_first_protocol>", prompt)
        self.assertIn("LOAD_WORKFLOW_TEMPLATE workflow_id", prompt)
        self.assertIn("WORKFLOW_STAGES", prompt)
        self.assertIn("ACTIVE_SKILLS", prompt)

    def test_consultant_prompt_restricts_execute_to_read_only_preflight(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("safe, read-only", prompt)
        self.assertIn("must never create, modify, index, normalize, balance", prompt)
        self.assertIn("cooler balance", prompt)
        self.assertIn("--preflight-only", prompt)
        self.assertIn("blocked/input-invalid handoff", prompt)

    def test_detailed_planner_prompt_is_centralized(self):
        prompt = PromptManager.get_detailed_planner_prompt(
            user_msg="Run QC",
            consultant_summary="[SELECTED_SKILLS: scrna/qc/filter]",
            installed_tools_str="R: ready",
            skills_summary="scrna/qc/filter",
            selected_skills_text="scrna/qc/filter",
            active_skill_docs="Use scripts/qc_filter_seurat.R",
        )

        self.assertIn("You are the Planner for BioAnalyst V4.", prompt)
        self.assertIn("Run QC", prompt)
        self.assertIn("Consultant Summary", prompt)
        self.assertIn("[PLANNING MEMORY CONTEXT]", prompt)
        self.assertIn("Use scripts/qc_filter_seurat.R", prompt)
        self.assertIn("Do NOT generate bash scripts", prompt)
        self.assertIn("- Cautions:", prompt)
        self.assertIn("## Execution Checklist", prompt)
        self.assertIn("- [ ] Step N:", prompt)
        self.assertIn("Action: [SEARCH/EXECUTE/INSTALL/ACTIVATE_BATCH/FINISH]", prompt)

    def test_detailed_planner_prompt_contains_active_batch_policy(self):
        prompt = PromptManager.get_detailed_planner_prompt(
            user_msg="Run full scRNA workflow",
            workflow_handoff="workflow_id: scrna_standard_analysis",
            active_batch_id="1",
            active_skills_text="scrna/ingest/data-download, scrna/qc/filter",
            future_skills_text="scrna/preprocess/basic",
            candidate_skills_text="None",
        )

        self.assertIn("[ACTIVE BATCH]", prompt)
        self.assertIn("ACTIVE_SKILLS max = 3", prompt)
        self.assertIn("Every ACTIVE_SKILL must be linked to an active workflow stage", prompt)
        self.assertIn("Candidate skills must never be loaded into generate", prompt)

    def test_installer_handover_prompt_prefers_conda_for_r_packages(self):
        prompt = PromptManager.get_installer_handover_prompt(
            tool_name="crop_breeding_crop_gs_rrblup_gblup_r",
            target_env="crop_gp_pro",
            env_instruction="Target environment: crop_gp_pro",
        )

        self.assertIn("r-<package>", prompt)
        self.assertIn("rrBLUP` -> `r-rrblup", prompt)
        self.assertIn("install.packages", prompt)
        self.assertIn("cat('OK\\n')", prompt)


if __name__ == "__main__":
    unittest.main()

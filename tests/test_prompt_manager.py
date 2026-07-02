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
        self.assertIn("<finish_only_policy>", ephemeral)
        self.assertIn("contains `Action: FINISH`, output exactly one `<solution>...</solution>`", ephemeral)
        self.assertIn("Never copy, symlink, move, or duplicate artifacts solely to satisfy", ephemeral)
        self.assertIn("<completion_gate_policy>", ephemeral)
        self.assertIn("missing promised artifacts", ephemeral)
        self.assertIn("do not run another `sed`, `grep`, `head`, `cat`, or source-code inspection command", ephemeral)
        self.assertIn("blocked outputs (`stage_status.tsv`, `report.md`, `reproduce.sh`)", ephemeral)

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

    def test_consultant_prompt_contains_formal_upstream_audit_policy(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
            input_resolution_context="[INPUT_RESOLUTION_AUDIT]\nneeds_user:true\n[/INPUT_RESOLUTION_AUDIT]",
        )

        self.assertIn("[INPUT RESOLUTION AUDIT]", prompt)
        self.assertIn("[INPUT_RESOLUTION_AUDIT]", prompt)
        self.assertIn("先找再问", prompt)
        self.assertIn("[FORMAL_UPSTREAM_CANDIDATES]", prompt)
        self.assertIn("For differential TAD, do not assume sample order", prompt)

    def test_consultant_prompt_requires_project_background_clarification(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="Run biological comparison",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("<background_clarification_protocol>", prompt)
        self.assertIn("applies throughout Consultant reasoning, not as a fixed early or late step", prompt)
        self.assertIn("before or after shallow skill discovery", prompt)
        self.assertIn("If reading a skill reveals required biological context or parameters", prompt)
        self.assertIn("Do not treat file names, sample names, benchmark IDs, directory names", prompt)

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
        self.assertIn("only when it is a clear semantic match", prompt)
        self.assertIn("Do not load an adjacent or broader workflow", prompt)
        self.assertIn("a TAD workflow is not a valid workflow for ordinary A/B compartment calling", prompt)
        self.assertIn("skip workflow handoff entirely and use the normal `[SELECTED_SKILLS: ...]`", prompt)
        self.assertIn("Do not include `[WORKFLOW_DECISION]`, `[WORKFLOW_STAGES]`, `workflow_id: none`", prompt)
        self.assertIn("Only when a real workflow template was loaded and clearly matches the task", prompt)

    def test_consultant_prompt_requires_strict_command_response_contract(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("<consultant_response_contract>", prompt)
        self.assertIn("Every Consultant reply must start with exactly one supported command token", prompt)
        self.assertIn("Never echo an observation as", prompt)
        self.assertIn("Do not output prose, markdown summaries, copied stdout/stderr", prompt)
        self.assertIn("If a command failed, do not paste the traceback back", prompt)

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
        self.assertIn("Do not turn CLI tools into Python/R imports", prompt)
        self.assertIn("bedtools --version", prompt)
        self.assertIn("never replace it with `import pybedtools`", prompt)

    def test_consultant_prompt_loads_skill_doc_before_uncertain_runtime_checks(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="Run tool with strict runtime",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("If environment availability, missing dependency, install policy, package type, or healthcheck details are uncertain", prompt)
        self.assertIn("load the selected skill's `SKILL.md` before declaring a dependency missing", prompt)
        self.assertIn("use that exact healthcheck or a strict subset of it", prompt)
        self.assertIn("Do not synthesize an import list from `runtime.packages`", prompt)
        self.assertIn("Do not infer extra packages or installation actions from a failed import", prompt)

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
        self.assertIn("Action: [SEARCH/EXECUTE/INSTALL/ACTIVATE_BATCH/ASK_USER/FINISH]", prompt)
        self.assertIn("single `Action: FINISH` item", prompt)
        self.assertIn("Never plan to copy, symlink, move, or duplicate artifacts solely to satisfy", prompt)

    def test_detailed_planner_prompt_requires_ask_user_for_unresolved_audit(self):
        prompt = PromptManager.get_detailed_planner_prompt(
            user_msg="Run differential TAD",
            consultant_summary="[INPUT_RESOLUTION_AUDIT]\nneeds_user:true\n[/INPUT_RESOLUTION_AUDIT]\n[QUESTIONS_FOR_USER]\n- confirm target/control\n[/QUESTIONS_FOR_USER]",
        )

        self.assertIn("needs_user:true", prompt)
        self.assertIn("first executable checklist item MUST be `Action: ASK_USER`", prompt)
        self.assertIn("target/control direction and bidirectional mode are required", prompt)

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

import json
import unittest

from prompts.manager import AIMessage, PromptManager


class _FakePackageManager:
    work_dir = "/work/project"

    def list_installed_tools(self):
        return "samtools: ready"

    def get_shim_dir_str(self):
        return "/work/shims"


class PromptManagerTests(unittest.TestCase):
    def test_recovery_prompts_separate_judgment_from_repair_generation(self):
        judge = PromptManager.get_recovery_judge_prompt({"execution": {"returncode": 1}})
        repair = PromptManager.get_repair_generator_prompt({
            "decision": {"recommended_action": "PATCH_SCRIPT"},
        })

        self.assertIn("Never include shell, Python, R, patches, commands", judge)
        self.assertIn('"schema_version": 2', judge)
        self.assertIn("You cannot finish the task", judge)
        self.assertIn("smallest current-step repair", repair)
        self.assertIn("Do not change the plan", repair)

    def test_recovery_prompts_keep_current_step_context_bounded(self):
        huge = "X" * 50_000
        diagnostic = {
            "schema_version": 2,
            "generated_at": "2026-07-30T00:00:00Z",
            "task": {"original_user_request": huge},
            "plan": {"success_criteria": [huge] * 10},
            "execution": {"stdout_tail": huge, "stderr_tail": huge},
            "artifact_evidence": {"filesystem_snapshot": [{"preview": huge}] * 10},
            "reviews": {"completion_gate_reason": huge},
            "recovery_history": {"attempts": [{"detail": huge}] * 10},
            "skill_policy": {"relevant_context": huge},
            "limits": {"max_context_chars": 26_000},
        }

        judge = PromptManager.get_recovery_judge_prompt(diagnostic)
        repair = PromptManager.get_repair_generator_prompt({
            "diagnostic_context": diagnostic,
            "decision": {"recommended_action": "PATCH_SCRIPT"},
            "current_step": huge,
            "forbidden_actions": [huge] * 40,
        })

        self.assertLessEqual(len(judge), 32_000)
        self.assertLessEqual(len(repair), 8_000)
        self.assertNotIn("generated_at", repair)

    def test_recovery_context_is_packed_separately_from_supervisor_feedback(self):
        supervisor = "<supervisor_context>\n" + ("WARN evidence\n" * 1200) + "</supervisor_context>"
        recovery = "<recovery_next_action>\n" + ("repair evidence\n" * 1600) + "</recovery_next_action>"
        prompt, report = PromptManager.build_ephemeral_context_result(
            task_spec_context=json.dumps({"goal": "repair current step", "must_not_do": ["do not guess"]}),
            current_plan="step-2: repair one generated script",
            plan_progress="step-2 running",
            supervisor_context=supervisor,
            recovery_context=recovery,
            loaded_skills_instructions="<active_skills_context>\n" + ("runtime rule\n" * 1000) + "</active_skills_context>",
            current_action_context="step_id: step-2\naction: EXECUTE",
            max_chars=26_000,
        )

        self.assertLessEqual(len(prompt), 26_000)
        self.assertEqual(prompt.count("<supervisor_context>"), 1)
        self.assertEqual(prompt.count("</recovery_next_action>"), 1)
        blocks = {item["id"]: item for item in report["blocks"]}
        self.assertIn(blocks["supervisor"]["action"], {"compressed", "kept"})
        self.assertIn(blocks["recovery"]["action"], {"compressed", "kept"})

    def test_effective_task_spec_precedes_optional_generate_context(self):
        prompt = PromptManager.build_ephemeral_context(
            task_spec_context='{"revision": 3, "goal": "current goal"}',
            current_plan="Step 2",
            plan_progress="Step 2 running",
            rag_context="optional old retrieval",
        )

        self.assertIn("<effective_task_spec>", prompt)
        self.assertLess(prompt.index("<effective_task_spec>"), prompt.index("<knowledge_base>"))
        self.assertLess(prompt.index("<current_plan>"), prompt.index("<knowledge_base>"))

    def test_consultant_prompt_is_compact_and_domain_neutral(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertLessEqual(len(prompt), 6000)
        self.assertIn("<workflow_first_protocol>", prompt)
        self.assertIn("<dynamic_audit_protocol>", prompt)
        self.assertIn("before a tool-specific runtime probe", prompt)
        self.assertIn("Required probes preserve non-zero status", prompt)
        self.assertNotIn("cooler balance", prompt)
        self.assertNotIn("hicFindTADs", prompt)

    def test_stable_prompt_excludes_ephemeral_context(self):
        stable = PromptManager.build_stable_system_prompt(
            package_manager=_FakePackageManager(),
            work_dir="/work/project",
            core_memory="CORE_FACT: project_alpha.gff3 is at /data/project_alpha.gff3",
            agents_md_context="AGENTS_RULE: keep edits scoped",
            user_preferences_context="USER_PREF: answer in Chinese",
            current_time="2026-05-02 10:00:00",
        )

        self.assertIn("CORE_FACT", stable)
        self.assertIn("AGENTS_RULE", stable)
        self.assertIn("USER_PREF: answer in Chinese", stable)
        self.assertIn("<user_preferences_context>", stable)
        self.assertIn("Wiki claims with evidence", stable)
        self.assertNotIn("RAG_HIT", stable)
        self.assertNotIn("TASK_MEMORY_HIT", stable)
        self.assertNotIn("ACTIVE_SKILL_DOC", stable)
        self.assertNotIn("supervisor_context", stable)
        self.assertNotIn("current_plan", stable)
        self.assertNotIn("samtools: ready", stable)
        self.assertIn("step-local refreshed tool list is authoritative", stable)

    def test_consultant_and_planner_prompts_include_user_preferences(self):
        consultant = PromptManager.get_consultant_prompt(
            intent="test",
            kb_list_desc="none",
            skills_summary="none",
            rag_history=[],
            user_preferences_context="USER_PREF: use concise Chinese",
        )
        planner = PromptManager.get_detailed_planner_prompt(
            user_msg="do analysis",
            user_preferences_context="USER_PREF: use concise Chinese",
        )

        self.assertIn("[USER PREFERENCES]", consultant)
        self.assertIn("USER_PREF: use concise Chinese", consultant)
        self.assertIn("[USER PREFERENCES]", planner)
        self.assertIn("USER_PREF: use concise Chinese", planner)

    def test_planner_does_not_route_local_file_validation_to_kb_search(self):
        planner = PromptManager.get_detailed_planner_prompt(user_msg="validate input files")

        self.assertIn("`Action: SEARCH` is knowledge-base retrieval only", planner)
        self.assertIn("`Action: EXECUTE` with `Purpose: validation`", planner)

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

    def test_structured_recovery_takes_precedence_over_legacy_template(self):
        ephemeral = PromptManager.build_ephemeral_context(
            history=[AIMessage(content="<observation>❌ failed</observation>")],
            current_plan="<current_step_context>{}</current_step_context>",
            recovery_context="<recovery_context>{}</recovery_context>",
        )

        self.assertNotIn("<recovery_mode>", ephemeral)
        self.assertEqual(ephemeral.count("<recovery_context>"), 1)
        self.assertIn("Follow the first running or pending item", ephemeral)
        self.assertIn("ACTIVE_SKILLS is derived by the controller", ephemeral)
        self.assertNotIn("<activate_batch batch_id=\"N\" />", ephemeral)
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
        self.assertIn("<local_input_discovery_protocol>", prompt)
        self.assertIn("bounded local/sibling/formal discovery", prompt)
        self.assertIn("For comparative/directional work", prompt)

    def test_consultant_prompt_requires_project_background_clarification(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="Run biological comparison",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("<background_clarification_protocol>", prompt)
        self.assertIn("This applies throughout reasoning, not at a fixed step", prompt)
        self.assertIn("Before or after shallow skill discovery or loading a skill", prompt)
        self.assertIn("If reading a skill reveals required biological context or parameters", prompt)
        self.assertIn("File names, sample names, benchmark IDs, and upstream artifacts are not project background", prompt)

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
        self.assertIn("only after that check", prompt)
        self.assertIn("do not load an adjacent or broader workflow", prompt)
        self.assertIn("task type, triggers, inputs, outputs, and stages clearly match", prompt)
        self.assertIn("skip workflow handoff and use [SELECTED_SKILLS: ...]", prompt)
        self.assertIn("never emit workflow blocks or `workflow_id: none`", prompt)

    def test_consultant_prompt_prefers_json_response_contract(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("<consultant_response_contract>", prompt)
        self.assertIn("Output exactly one `<action_json>...</action_json>` block", prompt)
        self.assertIn("using a supported action above", prompt)
        self.assertIn("Legacy commands remain accepted for compatibility", prompt)
        self.assertIn("never echo it", prompt)
        self.assertIn("No prose, copied stdout/stderr, traceback", prompt)

    def test_consultant_prompt_restricts_execute_to_read_only_preflight(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="General Inquiry",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("safe, read-only", prompt)
        self.assertIn("must never create, modify, index, normalize, balance", prompt)
        self.assertIn("skill-declared healthcheck", prompt)
        self.assertIn("do not invent stages", prompt)
        self.assertIn("blocked", prompt)
        self.assertNotIn("cooler balance", prompt)

    def test_consultant_prompt_loads_skill_doc_before_uncertain_runtime_checks(self):
        prompt = PromptManager.get_consultant_prompt(
            intent="Run tool with strict runtime",
            kb_list_desc="SOP, TOOLS, ERRORS",
            skills_summary="SKILL_TREE",
            rag_history=[],
        )

        self.assertIn("If environment, dependency, install policy, package type, or healthcheck details are uncertain", prompt)
        self.assertIn("load the selected skill's `SKILL.md` before declaring it missing", prompt)
        self.assertIn("Use the skill-declared healthcheck/runtime evidence exactly or as a strict subset", prompt)
        self.assertIn("do not infer imports, packages, commands, or installation actions", prompt)

    def test_detailed_planner_prompt_is_centralized(self):
        prompt = PromptManager.get_detailed_planner_prompt(
            user_msg="Run QC",
            consultant_summary="[SELECTED_SKILLS: scrna/qc/filter]",
            installed_tools_str="UNRELATED_GLOBAL_TOOL_SHOULD_NOT_RENDER",
            runtime_audit_context=(
                "[RUNTIME_AUDIT]\n"
                "skill: scrna/qc/filter\nstatus: ready\ninstall_required: false\n"
                "[/RUNTIME_AUDIT]"
            ),
            skills_summary="scrna/qc/filter",
            selected_skills_text="scrna/qc/filter",
            active_skill_docs="Use scripts/qc_filter_seurat.R",
        )

        self.assertIn("You are the Planner for BioAnalyst V4.", prompt)
        self.assertIn("Run QC", prompt)
        self.assertIn("Consultant Summary", prompt)
        self.assertIn("[CURRENT TASK RUNTIME AUDIT]", prompt)
        self.assertIn("status: ready", prompt)
        self.assertNotIn("UNRELATED_GLOBAL_TOOL_SHOULD_NOT_RENDER", prompt)
        self.assertNotIn("Installed Tools:", prompt)
        self.assertIn("[PLANNING MEMORY CONTEXT]", prompt)
        self.assertIn("Use scripts/qc_filter_seurat.R", prompt)
        self.assertIn("Do NOT generate bash scripts", prompt)
        self.assertIn("- Cautions:", prompt)
        self.assertIn("## Execution Checklist", prompt)
        self.assertIn("- [ ] Step N:", prompt)
        self.assertIn("Action: [SEARCH/EXECUTE/INSTALL/ASK_USER/FINISH]", prompt)
        self.assertIn("Skills: [canonical_skill_id,...]", prompt)
        self.assertIn("single `Action: FINISH` item", prompt)
        self.assertIn("Never plan to copy, symlink, move, or duplicate artifacts solely to satisfy", prompt)

    def test_skill_context_is_near_the_end_of_routing_prompts(self):
        consultant = PromptManager.get_consultant_prompt(
            intent="route",
            kb_list_desc="SOP",
            skills_summary="SKILL_CANDIDATE",
            workflow_catalog="WORKFLOW_CANDIDATE",
            installed_tools_str="RUNTIME_CANDIDATE",
            rag_history=[],
        )
        planner = PromptManager.get_detailed_planner_prompt(
            user_msg="run analysis",
            skills_summary="SKILL_POOL",
            selected_skills_text="skill/id",
            active_skill_docs="ACTIVE_SKILL_DOC",
        )

        self.assertGreater(consultant.rfind("SKILL_CANDIDATE"), consultant.find("<dynamic_audit_protocol>"))
        self.assertGreater(planner.rfind("SKILL_POOL"), planner.find("## Section 4: Validation Checklist"))
        self.assertLess(planner.find("ACTIVE_SKILL_DOC"), planner.find("<final_skill_boundary>"))
        self.assertIn("Consultant owns skill discovery", planner)

    def test_detailed_planner_prompt_requires_ask_user_for_unresolved_audit(self):
        prompt = PromptManager.get_detailed_planner_prompt(
            user_msg="Run differential TAD",
            consultant_summary="[INPUT_RESOLUTION_AUDIT]\nneeds_user:true\n[/INPUT_RESOLUTION_AUDIT]\n[QUESTIONS_FOR_USER]\n- confirm target/control\n[/QUESTIONS_FOR_USER]",
        )

        self.assertIn("needs_user:true", prompt)
        self.assertIn("first executable checklist item MUST be `Action: ASK_USER`", prompt)
        self.assertIn("For any comparative or directional analysis", prompt)
        self.assertIn("reference/query direction, and one-way/bidirectional mode", prompt)

    def test_detailed_planner_prompt_contains_step_skill_policy(self):
        prompt = PromptManager.get_detailed_planner_prompt(
            user_msg="Run full scRNA workflow",
            workflow_handoff="workflow_id: scrna_standard_analysis",
            active_batch_id="1",
            active_skills_text="scrna/ingest/data-download, scrna/qc/filter",
            future_skills_text="scrna/preprocess/basic",
            candidate_skills_text="None",
        )

        self.assertNotIn("[ACTIVE BATCH]", prompt)
        self.assertIn("Each step may declare at most 3 skills", prompt)
        self.assertIn("bind it to the matching coarse workflow stage through `Stage:`", prompt)
        self.assertIn("SELECTED_SKILLS is the full allowed skill pool", prompt)
        self.assertIn("Candidate skills must never be loaded into generate", prompt)
        self.assertIn("Do not create `ACTIVATE_BATCH` steps", prompt)
        self.assertIn("tool-specific preflight", prompt)
        self.assertIn("must carry the selected skill", prompt)

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

import datetime
import json
import logging
from typing import Dict, List, Optional, Tuple

from agent_core.context_budget import (
    ContextBlock,
    ContextBudgetManager,
    RequiredContextOverflow,
    compress_json,
    compress_lines,
    compress_markdown_sections,
    compress_paragraphs,
    compress_tagged_text,
)
from agent_core.diagnostics import DiagnosticContext

logger = logging.getLogger("BioMind.PromptManager")

try:
    from langchain_core.messages import BaseMessage, AIMessage
except ImportError:
    class BaseMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = ""):
            self.content = content

    class AIMessage(BaseMessage):  # type: ignore[no-redef]
        pass

# 导入上面拆分好的模板
from .templates import (
    MAIN_SKELETON,
    SYSTEM_HEADER,
    PRIME_DIRECTIVES,
    TOOL_CONTEXT_TEMPLATE,
    INTERACTION_PROTOCOL,
    RECOVERY_TEMPLATE,
    RECOVERY_JUDGE_PROMPT,
    REPAIR_GENERATOR_PROMPT,
    MEMORY_CONTEXT_TEMPLATE,
    CORE_MEMORY_CONTEXT_TEMPLATE,
    AGENTS_CONTEXT_TEMPLATE,
    USER_PREFERENCES_CONTEXT_TEMPLATE,
    EPHEMERAL_CONTEXT_TEMPLATE,
    TOOL_EXTRACTION_TEMPLATE,
    ERROR_ANALYSIS_TEMPLATE,
    TASK_FAILURE_REPORT_TEMPLATE,
    INSTALLER_HANDOVER_TEMPLATE,
    CONSULTANT_PROMPT,
    INSTALLER_SYSTEM_PROMPT,
    PLANNER_PROMPT,
)

class PromptManager:
    """
    工业级动态 Prompt 生成器 (逻辑层)
    负责数据获取与模板组装
    """

    @staticmethod
    def get_tool_extraction_prompt(user_msg: str) -> str:
        """生成工具信息提取 Prompt"""
        return TOOL_EXTRACTION_TEMPLATE.format(user_msg=user_msg)

    @staticmethod
    def get_error_analysis_prompt(error_context: str) -> str:
        """生成错误归因分析 Prompt"""
        return ERROR_ANALYSIS_TEMPLATE.format(error_context=error_context)

    @staticmethod
    def get_failure_report(analysis_text: str) -> str:
        """生成最终失败报告"""
        return TASK_FAILURE_REPORT_TEMPLATE.format(analysis=analysis_text)

    @staticmethod
    def _diagnostic_prompt_view(value: object, max_chars: int) -> object:
        """Project persisted or in-memory DiagnosticContext through one bounded path."""
        if not isinstance(value, dict):
            return value
        fields = DiagnosticContext.__dataclass_fields__
        payload = {key: value.get(key) for key in fields if key in value}
        if not payload:
            return value
        return DiagnosticContext(**payload).prompt_view(max_chars=max_chars)

    @staticmethod
    def _build_tool_context(package_manager, work_dir: str) -> str:
        """Build the full inventory context for Installer/Recovery only."""
        if package_manager:
            tools_list = package_manager.list_installed_tools()
            shim_path = package_manager.get_shim_dir_str()
            source_install_path = getattr(package_manager, "source_root", getattr(package_manager, "root_dir", "N/A"))
        else:
            tools_list = "None"
            shim_path = "N/A"
            source_install_path = "N/A"

        return TOOL_CONTEXT_TEMPLATE.format(
            tools_list=tools_list,
            shim_path=shim_path,
            source_path=source_install_path,
            work_dir=work_dir,
        )

    @staticmethod
    def build_scoped_runtime_context(
        runtime_audit_context: str,
        package_manager,
        work_dir: str,
    ) -> str:
        """Render step-local runtime evidence without exposing global history."""
        shim_path = package_manager.get_shim_dir_str() if package_manager else "N/A"
        return (
            "<environment_context>\n"
            "<runtime_audit>\n"
            f"{runtime_audit_context or 'No selected-skill runtime audit is available.'}\n"
            "</runtime_audit>\n"
            "<path_configuration>\n"
            f"- Shim Path: `{shim_path}` (Already in PATH)\n"
            f"- Working Directory: `{work_dir}`\n"
            "</path_configuration>\n"
            "<runtime_policy>\n"
            "- Use only this selected-skill audit to decide whether the runtime is ready.\n"
            "- Never infer readiness from an old global tool inventory or discovery hint.\n"
            "- Delegate installation only when install_required is true.\n"
            "- If resolved_runtime_paths are present, reuse those exact verified paths. "
            "Never reconstruct them from the project, benchmark, or working-directory root.\n"
            "</runtime_policy>\n"
            "</environment_context>"
        )

    @staticmethod
    def build_stable_system_prompt(
        package_manager,
        work_dir: str,
        core_memory: str = "",
        agents_md_context: str = "",
        user_preferences_context: str = "",
        current_time: Optional[str] = None,
    ) -> str:
        current_time = current_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = SYSTEM_HEADER.format(
            current_time=current_time,
            work_dir=work_dir,
        )
        if package_manager:
            shim_path = package_manager.get_shim_dir_str()
            source_install_path = getattr(
                package_manager,
                "source_root",
                getattr(package_manager, "root_dir", "N/A"),
            )
        else:
            shim_path = "N/A"
            source_install_path = "N/A"
        tool_context = TOOL_CONTEXT_TEMPLATE.format(
            tools_list=(
                "Deferred to <latest_environment_context>. "
                "The step-local refreshed tool list is authoritative."
            ),
            shim_path=shim_path,
            source_path=source_install_path,
            work_dir=work_dir,
        )
        core_memory_context = CORE_MEMORY_CONTEXT_TEMPLATE.format(
            core_memory=core_memory or "No frozen core memory snapshot loaded.",
        )
        agents_context = AGENTS_CONTEXT_TEMPLATE.format(
            agents_md_context=agents_md_context or "No AGENTS.md instructions discovered for this session.",
        )
        user_preferences = USER_PREFERENCES_CONTEXT_TEMPLATE.format(
            user_preferences_context=user_preferences_context or "No user preferences file loaded for this session.",
        )

        return MAIN_SKELETON.format(
            header=header,
            directives=PRIME_DIRECTIVES,
            tool_context=tool_context,
            core_memory_context=core_memory_context,
            user_preferences_context=user_preferences,
            agents_md_context=agents_context,
            protocol=INTERACTION_PROTOCOL,
        )

    @staticmethod
    def build_ephemeral_context(
        task_spec_context: str = "",
        rag_context: Optional[str] = None,
        history: Optional[List[BaseMessage]] = None,
        dynamic_tool_context: str = "",
        loaded_skills_instructions: str = "",
        long_term_memory: str = "",
        task_memory: str = "",
        related_memory: str = "",
        memory_packet_context: str = "",
        wiki_context: str = "",
        wiki_digest: str = "",
        archive_context: str = "",
        supervisor_context: str = "",
        recovery_context: str = "",
        current_plan: str = "No plan.",
        plan_progress: str = "No tracked plan steps.",
        current_action_context: str = "",
        max_chars: int = 26000,
    ) -> str:
        text, _ = PromptManager.build_ephemeral_context_result(
            task_spec_context=task_spec_context,
            rag_context=rag_context,
            history=history,
            dynamic_tool_context=dynamic_tool_context,
            loaded_skills_instructions=loaded_skills_instructions,
            long_term_memory=long_term_memory,
            task_memory=task_memory,
            related_memory=related_memory,
            memory_packet_context=memory_packet_context,
            wiki_context=wiki_context,
            wiki_digest=wiki_digest,
            archive_context=archive_context,
            supervisor_context=supervisor_context,
            recovery_context=recovery_context,
            current_plan=current_plan,
            plan_progress=plan_progress,
            current_action_context=current_action_context,
            max_chars=max_chars,
        )
        return text

    @staticmethod
    def build_ephemeral_context_result(
        task_spec_context: str = "",
        rag_context: Optional[str] = None,
        history: Optional[List[BaseMessage]] = None,
        dynamic_tool_context: str = "",
        loaded_skills_instructions: str = "",
        long_term_memory: str = "",
        task_memory: str = "",
        related_memory: str = "",
        memory_packet_context: str = "",
        wiki_context: str = "",
        wiki_digest: str = "",
        archive_context: str = "",
        supervisor_context: str = "",
        recovery_context: str = "",
        current_plan: str = "No plan.",
        plan_progress: str = "No tracked plan steps.",
        current_action_context: str = "",
        max_chars: int = 26000,
    ) -> Tuple[str, Dict]:
        """Build Generate context from typed blocks without slicing the result."""
        special_instr = ""
        if history and not recovery_context:
            last_content = str(getattr(history[-1], "content", history[-1]))
            if "<observation>" in last_content and "❌" in last_content:
                special_instr = RECOVERY_TEMPLATE
                logger.warning(
                    "Deprecated RECOVERY_TEMPLATE injected for a legacy state without structured RecoveryContext."
                )

        defaults = {
            "task_spec": task_spec_context or "No structured task spec is available.",
            "current_plan": current_plan or "No plan.",
            "plan_progress": plan_progress or "No tracked plan steps.",
            "supervisor": supervisor_context or "",
            "recovery": recovery_context or "",
            "special": special_instr,
            "environment": dynamic_tool_context or "No refreshed environment context available for this step.",
            "skill": loaded_skills_instructions or "No active skill docs loaded for this step.",
            "long_term": long_term_memory or "No extra long-term compatibility memory relevant to this step.",
            "task_memory": task_memory or "No TaskSummary report injected for this step.",
            "related_memory": related_memory or "No related SessionArchive report/cache context retrieved for this step.",
            "memory_packet": memory_packet_context or "No consultant-curated memory packet for this step.",
            "wiki": wiki_context or "No wiki memory retrieved for this step.",
            "wiki_digest": wiki_digest or "No compiled wiki digest available.",
            "archive": archive_context or "No session archive context retrieved for this step.",
            "rag": rag_context or "No external knowledge retrieved for this step.",
            "current_action": current_action_context or "Generate exactly one action for the current plan step.",
        }

        empty_template = EPHEMERAL_CONTEXT_TEMPLATE.format(
            task_spec_context="",
            dynamic_tool_context="",
            loaded_skills_instructions="",
            memory_context="",
            wiki_digest="",
            archive_context="",
            rag_context="",
            supervisor_context="",
            recovery_context="",
            special_instructions="",
            current_plan="",
            plan_progress="",
            current_action_context="",
        )
        empty_memory = MEMORY_CONTEXT_TEMPLATE.format(
            long_term_memory="",
            task_memory="",
            related_memory="",
            memory_packet_context="",
            wiki_context="",
        )
        rag_wrapper_chars = len("<retrieved_docs>\n\n</retrieved_docs>")
        dynamic_budget = max_chars - len(empty_template) - len(empty_memory) - rag_wrapper_chars
        if dynamic_budget <= 0:
            raise RequiredContextOverflow(
                "Static Generate policy exceeds the configured ephemeral context budget.",
                report={
                    "budget_chars": max_chars,
                    "static_chars": len(empty_template) + len(empty_memory),
                    "overflow": True,
                },
            )

        blocks = [
            ContextBlock("task_spec", "task_spec", defaults["task_spec"], 100, pinned=True, droppable=False, min_chars=1200, compress_fn=compress_json),
            ContextBlock("current_plan", "plan_step", defaults["current_plan"], 100, pinned=True, droppable=False),
            ContextBlock("plan_progress", "plan_progress", defaults["plan_progress"], 88, min_chars=300, compress_fn=compress_lines),
            ContextBlock(
                "supervisor", "supervisor", defaults["supervisor"], 100,
                pinned=bool(supervisor_context), droppable=not bool(supervisor_context),
                min_chars=900, compress_fn=compress_tagged_text,
            ),
            ContextBlock(
                "recovery", "recovery", defaults["recovery"], 100,
                pinned=bool(recovery_context), droppable=not bool(recovery_context),
                min_chars=3000, compress_fn=compress_tagged_text,
            ),
            ContextBlock("special", "recovery", defaults["special"], 100, pinned=bool(special_instr), droppable=not bool(special_instr)),
            ContextBlock("environment", "environment", defaults["environment"], 82, droppable=False, min_chars=600, compress_fn=compress_lines),
            ContextBlock("skill", "skill", defaults["skill"], 92, droppable=False, min_chars=2500, source_ref="active_skills", compress_fn=compress_markdown_sections),
            ContextBlock("long_term", "memory", defaults["long_term"], 25, min_chars=300, compress_fn=compress_paragraphs),
            ContextBlock("task_memory", "memory", defaults["task_memory"], 30, min_chars=300, compress_fn=compress_paragraphs),
            ContextBlock("related_memory", "memory", defaults["related_memory"], 20, min_chars=300, compress_fn=compress_paragraphs),
            ContextBlock("memory_packet", "memory", defaults["memory_packet"], 45, min_chars=500, compress_fn=compress_paragraphs),
            ContextBlock("wiki", "wiki", defaults["wiki"], 15, min_chars=300, compress_fn=compress_paragraphs),
            ContextBlock("wiki_digest", "wiki", defaults["wiki_digest"], 18, min_chars=300, compress_fn=compress_lines),
            ContextBlock("archive", "archive", defaults["archive"], 22, min_chars=300, source_ref="session_archive", compress_fn=compress_paragraphs),
            ContextBlock("rag", "rag", defaults["rag"], 28, min_chars=500, source_ref="rag_history", compress_fn=compress_paragraphs),
            ContextBlock("current_action", "action_contract", defaults["current_action"], 100, pinned=True, droppable=False),
        ]
        packed = ContextBudgetManager().pack(blocks, dynamic_budget)
        content = packed.contents
        final_rag_context = (
            f"<retrieved_docs>\n{content['rag']}\n</retrieved_docs>"
            if content.get("rag")
            else ""
        )

        memory_context = MEMORY_CONTEXT_TEMPLATE.format(
            long_term_memory=content.get("long_term", ""),
            task_memory=content.get("task_memory", ""),
            related_memory=content.get("related_memory", ""),
            memory_packet_context=content.get("memory_packet", ""),
            wiki_context=content.get("wiki", ""),
        )
        rendered = EPHEMERAL_CONTEXT_TEMPLATE.format(
            task_spec_context=content.get("task_spec", ""),
            dynamic_tool_context=content.get("environment", ""),
            loaded_skills_instructions=content.get("skill", ""),
            memory_context=memory_context,
            wiki_digest=content.get("wiki_digest", ""),
            archive_context=content.get("archive", ""),
            rag_context=final_rag_context,
            supervisor_context=content.get("supervisor", ""),
            recovery_context=content.get("recovery", ""),
            special_instructions=content.get("special", ""),
            current_plan=content.get("current_plan", ""),
            plan_progress=content.get("plan_progress", ""),
            current_action_context=content.get("current_action", ""),
        )
        if len(rendered) > max_chars:
            report = dict(packed.report)
            report.update({"overflow": True, "rendered_chars": len(rendered), "budget_chars": max_chars})
            raise RequiredContextOverflow(
                "Rendered Generate context exceeds budget after block packing.",
                report=report,
            )
        packed.report.update({
            "static_chars": len(rendered) - sum(len(value) for value in content.values()),
            "rendered_chars": len(rendered),
        })
        return rendered, packed.report

    @staticmethod
    def build_system_prompt(
        package_manager,
        work_dir: str,
        rag_context: Optional[str] = None,
        history: Optional[List[BaseMessage]] = None,
        loaded_skills_instructions: str = "",
        long_term_memory: str = "",
        task_memory: str = "",
        related_memory: str = "",
        core_memory: str = "",
        agents_md_context: str = "",
        user_preferences_context: str = "",
    ) -> str:
        """Backward-compatible stable system prompt builder.

        Dynamic arguments are accepted for old callers but intentionally not
        injected into the stable prefix. Use build_ephemeral_context() for
        RAG, task memory, active skills, and recovery context.
        """
        return PromptManager.build_stable_system_prompt(
            package_manager=package_manager,
            work_dir=work_dir,
            core_memory=core_memory,
            agents_md_context=agents_md_context,
            user_preferences_context=user_preferences_context,
        )

    # ====================================================
    # 2. 安装智能体 System Prompt (岗位说明书)
    # ====================================================
    @staticmethod
    def get_installer_system_prompt(
        tool_name: str,       # 新增: 传入工具名
        work_dir: str,
        source_path: str,     # 新增: 源码目录
        conda_root: str       # 新增: Conda/Shim目录
    ) -> str:
        """
        生成通用的安装工身份设定。
        """
        return INSTALLER_SYSTEM_PROMPT.format(
            tool_name=tool_name,
            work_dir=work_dir,
            source_path=source_path,
            conda_root=conda_root
        )

    # ====================================================
    # 3. 安装智能体 Handover Prompt (具体工单)
    # ====================================================
    @staticmethod
    def get_installer_handover_prompt(
        tool_name: str, 
        target_env: str, 
        env_instruction: str, 
        user_context: str = "No context provided",  # <--- 新增参数
        rag_context: str = "" 
    ) -> str:
        """
        生成移交给安装子智能体的具体任务指令。
        """
        rag_text = rag_context if rag_context else "No specific installation guide available."

        # 调用新的模板
        return INSTALLER_HANDOVER_TEMPLATE.format(
            user_context=user_context,       # <--- 填入 user_context
            rag_context=rag_text,
            tool_name=tool_name,
            target_env=target_env,
            env_instruction=env_instruction
        )
    


    @staticmethod
    def get_consultant_prompt(
        intent: str,
        kb_list_desc: str,
        skills_summary: str,
        rag_history: list,
        installed_tools_str: str = "None",
        work_dir: str = ".",
        max_skills: int = 1,
        wiki_memory_context: str = "",
        input_resolution_context: str = "",
        workflow_catalog: str = "",
        user_preferences_context: str = "",
        task_spec_context: str = "",
        runtime_evidence_context: str = "",
    ) -> str:
        rag_history_str = "\n".join(rag_history) if rag_history else "No previous context."
        return CONSULTANT_PROMPT.format(
            intent=intent,
            kb_list_desc=kb_list_desc,
            skills_summary=skills_summary,
            rag_history_str=rag_history_str,
            runtime_evidence_context=(
                runtime_evidence_context
                or installed_tools_str
                or "No selected-skill runtime evidence is available."
            ),
            work_dir=work_dir,
            max_skills=max_skills,
            wiki_memory_context=wiki_memory_context or "No compiled or retrieved wiki memory for this consultation.",
            input_resolution_context=input_resolution_context or "No formal upstream artifact audit was triggered for this consultation.",
            workflow_catalog=workflow_catalog or "No workflow templates available.",
            user_preferences_context=user_preferences_context or "No user preferences file loaded for this consultation.",
            task_spec_context=task_spec_context or "No structured task spec is available.",
        )

    @staticmethod
    def get_planner_prompt(user_msg: str, runtime_audit_context: str = "", skills_summary: str = "None") -> str:
        """
        生成规划智能体 (Planner) 的 Prompt
        """
        return PLANNER_PROMPT.format(
            user_msg=user_msg,
            runtime_audit_context=runtime_audit_context or "No selected-skill runtime audit is available.",
            skills_summary=skills_summary
        )

    @staticmethod
    def get_detailed_planner_prompt(
        user_msg: str,
        task_spec_context: str = "",
        consultant_summary: str = "",
        planner_wiki_context: str = "",
        runtime_audit_context: str = "",
        installed_tools_str: str = "None",
        rag_knowledge: str = "",
        skills_summary: str = "None",
        selected_skills_text: str = "None",
        active_skill_docs: str = "",
        skill_reference_index: str = "",
        skill_execution_context: str = "",
        workflow_handoff: str = "",
        active_batch_id: str = "",
        active_skills_text: str = "None",
        future_skills_text: str = "None",
        required_skills_all_text: str = "None",
        candidate_skills_text: str = "None",
        user_preferences_context: str = "",
    ) -> str:
        """Build the concrete planner prompt used by the main graph planner node."""
        return f"""You are the Planner for BioAnalyst V4.

[USER PREFERENCES]
{user_preferences_context or "No user preferences file loaded for this plan."}

User Goal: {user_msg}

{task_spec_context or "No structured task spec is available. Use the user goal without inventing missing requirements."}

[CONSULT HANDOFF]
{consultant_summary or "No Consultant handoff was provided."}

[PLANNING MEMORY CONTEXT]
{planner_wiki_context if planner_wiki_context else "No consultant memory packet or retrieved planning memory for this plan."}

[RUNTIME AUDIT]
{runtime_audit_context or "No selected-skill runtime audit is available. Planner must schedule targeted runtime diagnosis; it must not infer availability from a global tool inventory."}

[KNOWLEDGE BASE CONTEXT]
{rag_knowledge[:2000] if rag_knowledge else "None"}

[WORKFLOW STATE]
{workflow_handoff if workflow_handoff else "No workflow handoff. Treat SELECTED SKILLS as the allowed skill pool, not as active skills."}

==========================================================
TASK: Generate a DETAILED PLAN with 4 sections:
==========================================================

You are only planning. Do NOT generate bash scripts, Python scripts, or runnable code blocks.
Do NOT invent commands for skills. If a matching skill is present in ACTIVE SKILL DOCS,
plan to use its documented script path and documented arguments exactly.
Use only [CURRENT TASK RUNTIME AUDIT] for runtime scheduling:
- `status: ready`: proceed with the verified runtime and do not add installation.
- `install_required: true`: add INSTALL unless the user explicitly forbids installation.
- `status: unknown` or `status: healthcheck_failed`: add a targeted diagnosis, repair, or registration step; do not assume the runtime is usable and do not jump directly to installation.
- Controlled rg/discovery hints are evidence-only and can never establish `ready` without PackageManager evidence plus a passing healthcheck.
If the user request, benchmark instruction, Consultant Summary, or resume
context explicitly says missing environments/dependencies should be `blocked`,
`do not install`, or equivalent, do NOT plan INSTALL. Plan a blocked FINISH
after documenting the failed runtime check.
Every step must also include explicit `Assumptions`, `Required constraints`,
`Input contract`, `Output contract`, `Environment contract`, `Depends on`,
`Allowed fallbacks`, `Forbidden fallbacks`, and `Success criteria`. These are
machine-auditable facts, not filler prose. Mark uncertain items as unverified
assumptions. Runtime or artifact evidence can invalidate an assumption and
trigger a scoped revision, but cannot silently weaken a user hard constraint
or change the scientific target.

## Section 1: Requirement Analysis
- Input files, reference genomes, parameters needed
- Tools required (use the current selected-skill runtime audit above)
- Estimated runtime and resource needs (RAM, CPU, disk)

## Section 2: Step-by-Step Execution Plan
For EACH step, provide:
  Step N: [Action Name]
    - Tool: [tool name + version if known]
    - Execution guidance: [plain-language instruction; include an exact command only when it is copied from ACTIVE SKILL DOCS and filled with user-provided paths]
    - Input: [expected input files/data]
    - Output: [expected output files/format]
    - Success criteria: [how to verify this step worked]
    - Cautions: [historical lessons, path reuse rules, failure guards, or "none"]
    - Risk: [LOW/MEDIUM/HIGH - and why]
    - Assumptions: [unverified facts only]
    - Required constraints: [must preserve]
    - Input contract: [format/path/compatibility]
    - Output contract: [files/format/non-empty/validation]
    - Environment contract: [environment/executable/version]
    - Depends on: [step IDs]
    - Allowed fallbacks: [explicitly permitted alternatives]
    - Forbidden fallbacks: [methods/inputs/options that must not be substituted]

## Execution Checklist
After the detailed plan, include a machine-trackable checklist.
Each executable step MUST be one checkbox line in this exact style:
- [ ] Step N: [short action title] | Goal: [goal] | Action: [SEARCH/EXECUTE/INSTALL/ASK_USER/FINISH] | Purpose: [analysis/validation/probe/record_failure/install/user_input] | Skills: [canonical_skill_id,...] | Stage: [workflow_stage_id or none] | Batch: [batch_id or none] | Done: [completion criterion]
Keep checkbox lines short. Do not put long explanations or multi-line details inside checkbox items.
The checklist order MUST match Section 2 and should be executable one step at a time.
`Skills:` is mandatory on every checklist item. Use `Skills: []` only for an intentional no-skill control, user-question, or finalization step; never omit the field.
`Purpose:` describes what a successful command proves and is distinct from `Action:` routing. Use `validation` for a step whose completion criterion is verification itself; use `probe` only when its evidence cannot complete the current business step.
`Action: SEARCH` is knowledge-base retrieval only. It can never prove that a local file, directory, executable, or artifact exists, is non-empty, readable, writable, or compatible. Plan such read-only filesystem checks as `Action: EXECUTE` with `Purpose: validation` so they produce real execution evidence.

## Section 4: Validation Checklist
- What final outputs to verify?
- QC metrics to check (mapping rate, coverage, etc.)?
- Common failure modes to watch for?

RULES:
- Never infer availability from a global historical tool list. Use only the current selected-skill runtime audit.
- User/benchmark install policy overrides skill defaults: if missing runtime must be blocked or installation is forbidden, do not add INSTALL even when a skill declares `install_tool`.
- Do not infer runtime absence from `which tool` in the current shell when a skill declares `default_env` or `healthcheck`; use `[RUNTIME_AUDIT]`, the skill healthcheck, registered envs, or registered shims.
- For workflow tasks, use the workflow as a minimum coverage scaffold, not a ceiling. Plan every user-requested stage/output, including custom stages beyond the workflow.
- Consultant decomposes coarse workflow stages; Planner owns execution scheduling. The plan must decide which skill(s) are active for each executable step or batch.
- SELECTED_SKILLS is the full allowed skill pool for the task. Each checklist item's `Skills:` field is the sole step-to-skill schedule; the controller derives ACTIVE_SKILLS from the currently running step.
- Each step may declare at most 3 skills. If a stage needs more, split it into multiple executable checklist steps.
- Every declared step skill must belong to SELECTED_SKILLS. For workflow tasks, also bind it to the matching coarse workflow stage through `Stage:`.
- Do not create `ACTIVATE_BATCH` steps and do not output global `[ACTIVE_SKILLS]` or `[FUTURE_SKILLS]` blocks. Batch switching is an internal controller transition derived from the current step's `Batch:` field.
- Later-stage skills remain only in later checklist items. They do not become active until their step starts.
- Candidate skills must never be loaded into generate.
- When several selected skills are alternative tools for the same analysis stage, plan the documented primary/default tool first. Do not activate optional backup or cross-validation tools unless the user explicitly requested them, the primary tool failed, or the active skill doc requires a multi-tool stage.
- If a stage has no selected skill, use `Skills: []` and state why it is a no-skill control step or blocked decision.
- A tool-specific preflight, dependency/version check, runtime healthcheck, or environment verification must carry the selected skill whose runtime contract is being checked. `Skills: []` is valid only when the step does not invoke, locate, validate, install, or repair a selected tool/runtime.
- You may only select skill IDs listed in AVAILABLE SKILLS. Do not translate, rename, or invent skill IDs.
- Future or custom stages requested by the user must appear as later checklist steps, ASK_USER decisions, stage_blocked rows, or explicit custom-stage steps.
- If MANDATORY SKILL EXECUTION CARDS are present, they override your prior knowledge and all plan wording.
- If an execution card declares `renderer_id`, represent that renderer-owned workflow as one composite `Action: EXECUTE` checklist step with logical inputs, outputs, and final success criteria. Do not create separate Plan steps for its internal preflight/build/call/summarize scripts, do not name those scripts, and do not add a second report step when the manifest declares reporting artifacts. The registered manifest owns internal action order and progress.
- If Consultant Summary, resume context, or verified execution history says the task is already complete and all required artifacts are present, make the entire checklist a single `Action: FINISH` item. Do not add SEARCH, EXECUTE, INSTALL, or compatibility-repair steps after a completed task.
- Never plan to copy, symlink, move, or duplicate artifacts solely to satisfy a validation gate, completion gate, or gate-checked path. Use the verified real artifact path; if a gate disagrees, the gate/validation context must be repaired, not the scientific outputs.
- If ACTIVE SKILL DOCS are present, you MUST read them and plan the selected stage with their real script path(s), manifest format, and documented outputs. Do not replace a selected skill with a custom R/Python/bash script unless the skill doc explicitly says no script exists for that stage.
- Custom scripts are allowed only for user-requested custom stages that are not covered by the current ACTIVE_SKILLS or later planned workflow stages, or for final report/status assembly when no selected report skill is active.
- Convert relevant planning memory into step-local constraints and cautions. Do not rely on Generate to retrieve broad Wiki/TaskSummary context again.
- Never write "Command template" for a selected skill. Use "Execution guidance".
- Never use placeholder wrapper commands or undocumented script names.
- **PATH STRICTNESS**: You MUST use the EXACT absolute paths provided in the Consultant Summary.
- **NO GUESSING**: Do NOT guess file names or naming patterns. Use verified paths from Consultant or plan a bounded discovery step.
- If a required path is missing from the summary, your plan MUST include a bounded read-only discovery step before ASK or blocked.
- If a user-provided input appears to be one member of a conventional input set, plan sibling discovery before declaring missing inputs. Examples of general patterns include paired FASTQ mates, sample replicates/groups sharing a prefix, sidecar indexes, sample sheets, metadata, references, and annotation files required by the selected skill.
- If a selected skill needs reference/annotation/metadata and Consultant did not verify a path, plan a bounded search in the explicit input directory, nearby data/ref/annotation/metadata directories, same-project benchmark roots, and verified upstream outputs. Do not invent a path; use a unique verified match or ASK on ambiguity.
- If Consultant Summary contains `[INPUT_RESOLUTION_AUDIT]` with `needs_user:true`, or `[QUESTIONS_FOR_USER]` contains anything other than `none`, the first executable checklist item MUST be `Action: ASK_USER`. Do not plan data execution, blocked report generation, or `stage_status.tsv` missing-input rows before that answer.
- If `[FORMAL_UPSTREAM_CANDIDATES]` lists verified candidates, plan to verify and reuse those exact formal paths; if candidates are multiple or ambiguous, plan to ask the user to choose.
- For any comparative or directional analysis, target/control, case/control, treatment/control, reference/query direction, and one-way/bidirectional mode are scientific choices. If they are not explicit in Consultant Summary, plan `ASK_USER` before execution when the choice affects interpretation.
- Skill name is not a runnable shell command unless the skill body explicitly defines such a command.
- Do not include a "Script Skeleton" section.

[SELECTED SKILL CONTRACT]
[AVAILABLE SKILLS]
{skills_summary}

[SELECTED SKILLS]
{selected_skills_text}

[ACTIVE SKILL DOCS]
{active_skill_docs if active_skill_docs else "No plan step is active during planning. Use only the shallow Consultant-selected skill catalog above; Generate will load full docs for the running step."}
{skill_reference_index if skill_reference_index else ""}
{skill_execution_context}

<final_skill_boundary>
Consultant owns skill discovery and SELECTED_SKILLS is the full allowed pool. Planner schedules that pool only through each checklist item's mandatory `Skills:` field. The controller, not Planner prose, derives ACTIVE_SKILLS for the current step. Never load or invent skills outside the context above.
</final_skill_boundary>
"""

    @staticmethod
    def get_recovery_judge_prompt(diagnostic_context: dict) -> str:
        context = dict(diagnostic_context or {})
        context = PromptManager._diagnostic_prompt_view(context, 26_000)
        payload = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
        return RECOVERY_JUDGE_PROMPT.format(diagnostic_context=payload)

    @staticmethod
    def get_repair_generator_prompt(recovery_context: dict) -> str:
        context = dict(recovery_context or {})
        diagnostic = context.get("diagnostic_context")
        context["diagnostic_context"] = PromptManager._diagnostic_prompt_view(diagnostic, 3_000)
        decision = context.get("decision")
        if isinstance(decision, dict):
            context["decision"] = {
                key: decision.get(key)
                for key in (
                    "recommended_action", "failure_owner", "failure_type", "reason",
                    "root_cause", "repair_strategy", "evidence", "missing_evidence",
                )
                if decision.get(key) not in (None, "", [], {})
            }
            for key in ("evidence", "missing_evidence"):
                if isinstance(context["decision"].get(key), list):
                    context["decision"][key] = [
                        str(item or "")[:240]
                        for item in context["decision"][key][-6:]
                    ]
        if isinstance(context.get("current_step"), str):
            current_step = context["current_step"]
            context["current_step"] = (
                current_step if len(current_step) <= 1_200 else current_step[:1_200] + "...[truncated]"
            )
        if isinstance(context.get("forbidden_actions"), list):
            context["forbidden_actions"] = [
                str(item or "")[:160]
                for item in context["forbidden_actions"][-6:]
            ]
        payload = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
        return REPAIR_GENERATOR_PROMPT.format(recovery_context=payload)

    @staticmethod
    def get_plan_repair_prompt(
        repair_request: dict,
        *,
        effective_facts_context: str = "",
        selected_skill_context: str = "",
        renderer_route: str = "",
    ) -> str:
        """Build a compact, version-bound prompt for repairing existing steps."""
        payload = json.dumps(repair_request, ensure_ascii=False, separators=(",", ":"), default=str)
        return f"""You are repairing an existing BioMind plan, not creating a new plan.

The local plan store is authoritative. Modify only step IDs in `allowed_step_ids`.
Preserve immutable constraints, step IDs, completed/running status, and evidence.
Correct only the supplied validation violations and required dependency contracts.
Do not add or delete workflow stages. Do not return Markdown or a full plan.
Repair scope: schema/format/missing fields/references only.
Do not change effective user facts, selected skills, scientific intent, or the renderer route.

{effective_facts_context or "[EFFECTIVE USER FACTS]\nNo structured effective facts were available.\n[USER SOURCE REFERENCES]\n[]"}

[SELECTED SKILL CONTRACT]
{selected_skill_context or "No selected-skill context was provided."}

[RENDERER ROUTE]
{renderer_route or "No renderer route applies."}

Return exactly one JSON object:
{{
  "operation": "replace_steps",
  "base_plan_version": <same integer as request>,
  "base_plan_hash": "<same hash as request>",
  "replace_steps": [
    {{"step_id": "step-N", "new_step": {{
      "step_id": "step-N",
      "title": "...",
      "action": "search|execute|install|ask_user|finish",
      "purpose": "analysis|validation|probe|record_failure|install|user_input",
      "tool": "...",
      "inputs": [],
      "outputs": [],
      "dependencies": [],
      "success_criteria": [],
      "skill_ids": [],
      "risk_level": "low|medium|high"
    }}}}
  ],
  "reason": "brief repair reason"
}}

<plan_repair_request>
{payload}
</plan_repair_request>
"""

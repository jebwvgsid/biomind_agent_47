import datetime
from typing import List, Optional

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
    MEMORY_CONTEXT_TEMPLATE,
    CORE_MEMORY_CONTEXT_TEMPLATE,
    AGENTS_CONTEXT_TEMPLATE,
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
    def _build_tool_context(package_manager, work_dir: str) -> str:
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
    def build_stable_system_prompt(
        package_manager,
        work_dir: str,
        core_memory: str = "",
        agents_md_context: str = "",
        current_time: Optional[str] = None,
    ) -> str:
        current_time = current_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = SYSTEM_HEADER.format(
            current_time=current_time,
            work_dir=work_dir,
        )
        tool_context = PromptManager._build_tool_context(package_manager, work_dir)
        core_memory_context = CORE_MEMORY_CONTEXT_TEMPLATE.format(
            core_memory=core_memory or "No frozen core memory snapshot loaded.",
        )
        agents_context = AGENTS_CONTEXT_TEMPLATE.format(
            agents_md_context=agents_md_context or "No AGENTS.md instructions discovered for this session.",
        )

        return MAIN_SKELETON.format(
            header=header,
            directives=PRIME_DIRECTIVES,
            tool_context=tool_context,
            core_memory_context=core_memory_context,
            agents_md_context=agents_context,
            protocol=INTERACTION_PROTOCOL,
        )

    @staticmethod
    def build_ephemeral_context(
        rag_context: Optional[str] = None,
        history: Optional[List[BaseMessage]] = None,
        dynamic_tool_context: str = "",
        loaded_skills_instructions: str = "",
        long_term_memory: str = "",
        task_memory: str = "",
        related_memory: str = "",
        wiki_context: str = "",
        wiki_digest: str = "",
        archive_context: str = "",
        supervisor_context: str = "",
        current_plan: str = "No plan.",
        plan_progress: str = "No tracked plan steps.",
    ) -> str:
        final_rag_context = "No external knowledge retrieved for this step."
        if rag_context:
            final_rag_context = f"<retrieved_docs>\n{rag_context}\n</retrieved_docs>"

        memory_context = MEMORY_CONTEXT_TEMPLATE.format(
            long_term_memory=long_term_memory or "No long-term Markdown memory relevant to this step.",
            task_memory=task_memory or "No task Markdown memory for this step.",
            related_memory=related_memory or "No related Markdown memory retrieved for this step.",
            wiki_context=wiki_context or "No wiki memory retrieved for this step.",
        )

        special_instr = ""
        if history:
            last_content = str(getattr(history[-1], "content", history[-1]))
            if "<observation>" in last_content and "❌" in last_content:
                special_instr = RECOVERY_TEMPLATE

        return EPHEMERAL_CONTEXT_TEMPLATE.format(
            dynamic_tool_context=dynamic_tool_context or "No refreshed environment context available for this step.",
            loaded_skills_instructions=loaded_skills_instructions or "No active skill docs loaded for this step.",
            memory_context=memory_context,
            wiki_digest=wiki_digest or "No compiled wiki digest available.",
            archive_context=archive_context or "No session archive context retrieved for this step.",
            rag_context=final_rag_context,
            supervisor_context=supervisor_context or "",
            special_instructions=special_instr,
            current_plan=current_plan or "No plan.",
            plan_progress=plan_progress or "No tracked plan steps.",
        )

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
        workflow_catalog: str = "",
    ) -> str:
        rag_history_str = "\n".join(rag_history) if rag_history else "No previous context."
        return CONSULTANT_PROMPT.format(
            intent=intent,
            kb_list_desc=kb_list_desc,
            skills_summary=skills_summary,
            rag_history_str=rag_history_str,
            installed_tools_str=installed_tools_str,
            work_dir=work_dir,
            max_skills=max_skills,
            wiki_memory_context=wiki_memory_context or "No compiled or retrieved wiki memory for this consultation.",
            workflow_catalog=workflow_catalog or "No workflow templates available.",
        )

    @staticmethod
    def get_planner_prompt(user_msg: str, installed_tools_str: str, skills_summary: str = "None") -> str:
        """
        生成规划智能体 (Planner) 的 Prompt
        """
        return PLANNER_PROMPT.format(
            user_msg=user_msg,
            installed_tools_str=installed_tools_str,
            skills_summary=skills_summary
        )

    @staticmethod
    def get_detailed_planner_prompt(
        user_msg: str,
        consultant_summary: str = "",
        planner_wiki_context: str = "",
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
        candidate_skills_text: str = "None",
    ) -> str:
        """Build the concrete planner prompt used by the main graph planner node."""
        return f"""You are the Planner for BioAnalyst V4.

User Goal: {user_msg}
{f"Consultant Summary: {consultant_summary}" if consultant_summary else ""}

[WIKI MEMORY CONTEXT]
{planner_wiki_context if planner_wiki_context else "No compiled or retrieved wiki memory for this plan."}

[ENVIRONMENT]
Installed Tools:
{installed_tools_str}

[KNOWLEDGE BASE CONTEXT]
{rag_knowledge[:2000] if rag_knowledge else "None"}

[AVAILABLE SKILLS]
{skills_summary}

[SELECTED SKILLS]
{selected_skills_text}

[WORKFLOW HANDOFF]
{workflow_handoff if workflow_handoff else "No workflow handoff. Use legacy selected skills if present."}

[ACTIVE BATCH]
active_batch_id: {active_batch_id or "none"}
active_skills: {active_skills_text}
future_skills: {future_skills_text}
candidate_skills: {candidate_skills_text}

[ACTIVE SKILL DOCS]
{active_skill_docs if active_skill_docs else "No skill selected. If you choose a skill, use only IDs from AVAILABLE SKILLS."}
{skill_reference_index if skill_reference_index else ""}
{skill_execution_context}

==========================================================
TASK: Generate a DETAILED PLAN with 4 sections:
==========================================================

You are only planning. Do NOT generate bash scripts, Python scripts, or runnable code blocks.
Do NOT invent commands for skills. If a matching skill is present in ACTIVE SKILL DOCS,
plan to use its documented script path and documented arguments exactly.

## Section 1: Requirement Analysis
- Input files, reference genomes, parameters needed
- Tools required (check installed list above)
- Estimated runtime and resource needs (RAM, CPU, disk)

## Section 2: Step-by-Step Execution Plan
For EACH step, provide:
  Step N: [Action Name]
    - Tool: [tool name + version if known]
    - Execution guidance: [plain-language instruction; include an exact command only when it is copied from ACTIVE SKILL DOCS and filled with user-provided paths]
    - Input: [expected input files/data]
    - Output: [expected output files/format]
    - Success criteria: [how to verify this step worked]
    - Risk: [LOW/MEDIUM/HIGH - and why]

## Execution Checklist
After the detailed plan, include a machine-trackable checklist.
Each executable step MUST be one checkbox line in this exact style:
- [ ] Step N: [short action title] | Goal: [goal] | Action: [SEARCH/EXECUTE/INSTALL/ACTIVATE_BATCH/FINISH] | Done: [completion criterion]
Keep checkbox lines short. Do not put long explanations or multi-line details inside checkbox items.
The checklist order MUST match Section 2 and should be executable one step at a time.

## Section 4: Validation Checklist
- What final outputs to verify?
- QC metrics to check (mapping rate, coverage, etc.)?
- Common failure modes to watch for?

RULES:
- If a tool is in Installed Tools, use it directly. DO NOT add install steps.
- For workflow tasks, plan the full workflow but activate only the current execution batch.
- ACTIVE_SKILLS max = 3.
- Every ACTIVE_SKILL must be linked to an active workflow stage.
- Future-stage skills must be placed under FUTURE_SKILLS, not ACTIVE_SKILLS.
- Candidate skills must never be loaded into generate.
- If a stage has no selected skill, it must be marked no_skill_needed or blocked in the workflow handoff.
- If using legacy non-workflow skills, append: [SELECTED_SKILLS: skill_id_1, skill_id_2]
- If using workflow skills, append [ACTIVE_SKILLS] and [FUTURE_SKILLS] blocks.
- You may only select skill IDs listed in AVAILABLE SKILLS. Do not translate, rename, or invent skill IDs.
- ACTIVE SKILLS are already finalized for the current batch. Do not add extra candidate skills beyond that set.
- If MANDATORY SKILL EXECUTION CARDS are present, they override your prior knowledge and all plan wording.
- If ACTIVE SKILL DOCS are present, you MUST read them and use their real script path(s). Never describe the skill as an abstract module or pre-built global command.
- Never write "Command template" for a selected skill. Use "Execution guidance".
- Never use placeholder wrapper commands or undocumented script names.
- **PATH STRICTNESS**: You MUST use the EXACT absolute paths provided in the Consultant Summary.
- **NO GUESSING**: Do NOT guess file names or naming patterns (e.g., do not assume '.CG.bw' if the summary says '_CG.Chr10.bw').
- If a required path is missing from the summary, your plan MUST include a step to find it first.
- Skill name is not a runnable shell command unless the skill body explicitly defines such a command.
- Do not include a "Script Skeleton" section.
"""

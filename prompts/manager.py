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
        loaded_skills_instructions: str = "",
        long_term_memory: str = "",
        task_memory: str = "",
        related_memory: str = "",
        wiki_context: str = "",
        archive_context: str = "",
        supervisor_context: str = "",
        current_plan: str = "No plan.",
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
            loaded_skills_instructions=loaded_skills_instructions or "No active skill docs loaded for this step.",
            memory_context=memory_context,
            archive_context=archive_context or "No session archive context retrieved for this step.",
            rag_context=final_rag_context,
            supervisor_context=supervisor_context or "",
            special_instructions=special_instr,
            current_plan=current_plan or "No plan.",
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

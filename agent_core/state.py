from typing import Annotated, List, Optional, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

# 主智能体状态
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    next_step: Optional[str]
    task_id: Optional[str]
    session_id: Optional[str]
    project_id: Optional[str]
    stable_system_prompt: Optional[str]
    core_memory_loaded_at: Optional[str]
    current_plan: Optional[str]
    selected_skills: List[str]
    selected_skills_raw: List[str]
    skill_selection_source: Optional[str]
    retry_count: int
    intent: Optional[str]
    artifacts: List[str]
    requirements_summary: str  # 存储咨询阶段确认的参数（如基因组大小、物种等）
    rag_history: List[str]     # 存储咨询阶段查到的所有知识片段
    supervisor_verdict: Optional[str]

# 安装子智能体状态
class InstallerState(TypedDict):
    tool_name: str
    messages: Annotated[List[BaseMessage], add_messages]
    next_step: Optional[str]
    final_result: Optional[str]

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
    plan_progress: Optional[str]
    selected_skills: List[str]
    selected_skills_raw: List[str]
    skill_selection_source: Optional[str]
    workflow_id: Optional[str]
    workflow_stages: List[dict]
    workflow_state: dict
    workflow_handoff: dict
    pending_action: dict
    node_recommended_next_step: Optional[str]
    pending_human_question: dict
    runtime_status: dict
    active_batch_id: Optional[str]
    active_skills: List[str]
    future_skills: List[str]
    required_skills_all: List[str]
    candidate_skills: List[str]
    retry_count: int
    task_mode: Optional[str]
    retrieval_mode: Optional[str]
    use_scientific_review: bool
    validation_level: Optional[str]
    intent: Optional[str]
    artifacts: List[str]
    requirements_summary: str  # 存储咨询阶段确认的参数（如基因组大小、物种等）
    rag_history: List[str]     # 存储咨询阶段查到的所有知识片段
    memory_packet_context: str  # Consultant 阶段筛选整理后的跨阶段记忆包
    wiki_memory_context: str    # Consultant 阶段一次性检索到的 Wiki 上下文
    supervisor_verdict: Optional[str]

# 安装子智能体状态
class InstallerState(TypedDict):
    tool_name: str
    messages: Annotated[List[BaseMessage], add_messages]
    next_step: Optional[str]
    final_result: Optional[str]

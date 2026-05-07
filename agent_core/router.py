import logging
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger("BioMind.Router")

async def determine_intent(llm: ChatOpenAI, user_query: str) -> str:
    """
    智能意图路由 (SOP / TOOLS / ERRORS / CHAT / ALL)
    """

    router_prompt = f"""
You are an intent classifier. Classify the USER QUERY into:

1. **SOP**: User asks **HOW TO RUN** a tool, about workflows, or data analysis.
   - Examples: "How to run samtools sort?", "Analysis pipeline for RNA-seq".

2. **TOOLS**: User explicitly asks to **EXECUTE an installation or configuration**.
   - Triggers: "Install", "Setup", "Register", "Configure", "Add to path".
   - Examples: "Install bwa", "Register samtools", "Create env".

3. **INFO**: User asks about **TOOL CONCEPTS** or **INSTALLATION GUIDES** (without asking to execute).
   - Examples: "How do I install gatk? (asking for guide)", "What is fastqc?", "Where is bwa installed?".

4. **ERRORS**: Error logs or fix requests.
5. **CHAT**: Greetings.

USER QUERY: "{user_query}"

OUTPUT ONLY one word: SOP, TOOLS, INFO, ERRORS, or CHAT.
"""

    try:
        response = await llm.ainvoke([HumanMessage(content=router_prompt)])
        intent = response.content.strip().upper()
        
        # 清洗结果
        for valid in ["SOP", "TOOLS", "INFO", "ERRORS", "CHAT", "ALL"]:
            if valid in intent:
                return valid
        return "ALL" # 无法判断时，默认查库以防万一
        
    except Exception as e:
        logger.warning(f"Router failed: {e}")
        return "ALL"
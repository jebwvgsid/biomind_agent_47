import json
import logging
import re
from typing import Any, Dict

try:
    from langchain_core.messages import HumanMessage
except ModuleNotFoundError:  # pragma: no cover - lightweight test fallback
    class HumanMessage:  # type: ignore[no-redef]
        def __init__(self, content: str):
            self.content = content

logger = logging.getLogger("BioMind.Router")

TASK_MODES = {"chat", "readonly", "simple_execute", "managed"}
RETRIEVAL_MODES = {"none", "kb", "skill_docs"}


def _default_decision(reason: str = "router fallback") -> Dict[str, Any]:
    return {
        "task_mode": "managed",
        "retrieval_mode": "kb",
        "confidence": 0.0,
        "reason": reason,
    }


def _extract_json_object(text: str) -> Dict[str, Any]:
    payload = str(text or "").strip()
    if not payload:
        raise ValueError("empty router response")

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", payload, re.DOTALL | re.IGNORECASE)
    if fence_match:
        payload = fence_match.group(1).strip()
    else:
        object_match = re.search(r"\{.*\}", payload, re.DOTALL)
        if object_match:
            payload = object_match.group(0).strip()

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("router response JSON is not an object")
    return data


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def normalize_task_mode_decision(
    data: Dict[str, Any],
    *,
    confidence_threshold: float = 0.65,
) -> Dict[str, Any]:
    mode = str(data.get("task_mode") or "").strip().lower()
    retrieval = str(data.get("retrieval_mode") or "").strip().lower()
    confidence = _coerce_confidence(data.get("confidence"))
    reason = str(data.get("reason") or "").strip()

    if mode not in TASK_MODES:
        mode = "managed"
        confidence = 0.0
        reason = reason or "invalid task_mode"

    if retrieval not in RETRIEVAL_MODES:
        retrieval = "none" if mode in {"readonly", "simple_execute"} else "kb"

    if confidence < confidence_threshold and mode != "managed":
        reason = (reason + "; " if reason else "") + "low confidence, escalated to managed"
        mode = "managed"
        retrieval = "kb"

    if mode in {"readonly", "simple_execute"}:
        retrieval = "none"
    elif mode == "managed" and retrieval == "none":
        retrieval = "kb"

    return {
        "task_mode": mode,
        "retrieval_mode": retrieval,
        "confidence": confidence,
        "reason": reason[:300] if reason else "classified by fast router",
    }


def build_task_mode_router_prompt(user_query: str) -> str:
    return f"""You are BioMind's fast router. Classify the USER REQUEST into exactly one route.

Routes:

chat:
- Conceptual explanation, definition, parameter meaning, advice, or discussion.
- Does not require reading local files or running commands.
- May use lightweight retrieval from project knowledge base or skill documentation.

readonly:
- User asks to inspect existing local state only, such as listing directories, reading file snippets,
  checking paths, versions, dates, or command help.
- No file modification, no installation, no workflow, no scientific result validation.

simple_execute:
- User asks to run one explicit existing command or script.
- No installation, no multi-step workflow, no ambiguous analysis goal, no scientific result validation.

managed:
- Multi-step task, unclear goal, bioinformatics/data analysis workflow, installation/environment changes,
  debugging/fixing environments, output generation, scientific validation, comparisons, plotting,
  QC, or anything that should use consultant/planner/supervisor.
- If uncertain, choose managed.

Return JSON only. Do not answer the user and do not include markdown.

Schema:
{{
  "task_mode": "chat|readonly|simple_execute|managed",
  "retrieval_mode": "none|kb|skill_docs",
  "confidence": 0.0,
  "reason": "short reason"
}}

Retrieval guidance:
- chat can use "kb" for general/project knowledge or "skill_docs" for questions about BioMind skills/workflows.
- readonly and simple_execute must use "none".
- managed usually uses "kb".

USER REQUEST:
{user_query}
"""


async def classify_task_mode(
    llm: Any,
    user_query: str,
    *,
    confidence_threshold: float = 0.65,
) -> Dict[str, Any]:
    try:
        response = await llm.ainvoke([HumanMessage(content=build_task_mode_router_prompt(user_query))])
        data = _extract_json_object(str(response.content))
        return normalize_task_mode_decision(data, confidence_threshold=confidence_threshold)
    except Exception as exc:
        logger.warning("Task-mode router failed: %s", exc)
        return _default_decision(f"router failed: {type(exc).__name__}")


async def determine_intent(llm: Any, user_query: str) -> str:
    """Backward-compatible legacy intent entrypoint.

    The main graph now uses classify_task_mode(). This function keeps the old
    SOP/TOOLS/INFO/ERRORS/CHAT contract for any older callers.
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
        intent = str(response.content or "").strip().upper()
        for valid in ["SOP", "TOOLS", "INFO", "ERRORS", "CHAT", "ALL"]:
            if valid in intent:
                return valid
        return "ALL"
    except Exception as exc:
        logger.warning("Legacy router failed: %s", exc)
        return "ALL"

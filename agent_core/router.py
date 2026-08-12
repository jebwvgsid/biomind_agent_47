import json
import logging
import re
from typing import Any, Dict, Mapping

try:
    from langchain_core.messages import HumanMessage
except ModuleNotFoundError:  # pragma: no cover - lightweight test fallback
    class HumanMessage:  # type: ignore[no-redef]
        def __init__(self, content: str):
            self.content = content

from agent_core.context_lineage import ainvoke_with_snapshot

logger = logging.getLogger("BioMind.Router")

TASK_MODES = {"chat", "readonly", "simple_execute", "managed"}
RETRIEVAL_MODES = {"none", "kb", "skill_docs"}
REQUEST_INTENT_SCHEMA_VERSION = 1
REQUEST_KINDS = {"question", "plan", "execute"}
EXECUTION_SCOPES = {"none", "readonly", "simple", "managed"}


def _non_executable_decision(reason: str, *, status: str) -> Dict[str, Any]:
    """Fail closed without granting TaskSpec authority and request consultation."""
    return {
        "schema_version": REQUEST_INTENT_SCHEMA_VERSION,
        "request_kind": "question",
        "execution_scope": "none",
        "managed_intent_explicit": False,
        "task_mode": "chat",
        "retrieval_mode": "none",
        "confidence": 0.0,
        "admission_status": status,
        "consultation_required": True,
        "reason": str(reason or "router fallback")[:300],
    }


def _default_decision(reason: str = "router fallback") -> Dict[str, Any]:
    return _non_executable_decision(reason, status="router_fallback_non_executable")


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


def normalize_request_intent_decision(
    data: Mapping[str, Any],
    *,
    confidence_threshold: float = 0.65,
) -> Dict[str, Any]:
    """Validate an LLM semantic decision without reclassifying its prose.

    The Controller consumes only closed typed fields.  Invalid, incoherent, or
    low-confidence decisions fail closed and require Consultant clarification;
    they are never escalated to an executable TaskSpec.
    """
    try:
        schema_version = int(data.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    request_kind = str(data.get("request_kind") or "").strip().lower()
    execution_scope = str(data.get("execution_scope") or "").strip().lower()
    managed_explicit = data.get("managed_intent_explicit")
    retrieval = str(data.get("retrieval_mode") or "").strip().lower()
    confidence = _coerce_confidence(data.get("confidence"))
    reason = str(data.get("reason") or "").strip()

    if schema_version != REQUEST_INTENT_SCHEMA_VERSION:
        return _non_executable_decision(
            reason or "unsupported request-intent schema",
            status="invalid_schema_non_executable",
        )
    if request_kind not in REQUEST_KINDS:
        return _non_executable_decision(
            reason or "invalid request_kind",
            status="invalid_request_kind_non_executable",
        )
    if execution_scope not in EXECUTION_SCOPES or not isinstance(managed_explicit, bool):
        return _non_executable_decision(
            reason or "invalid execution admission fields",
            status="invalid_execution_scope_non_executable",
        )

    if retrieval not in RETRIEVAL_MODES:
        return _non_executable_decision(
            reason or "invalid retrieval_mode",
            status="invalid_retrieval_mode_non_executable",
        )

    coherent = (
        request_kind in {"question", "plan"}
        and execution_scope == "none"
        and managed_explicit is False
    ) or (
        request_kind == "execute"
        and execution_scope in {"readonly", "simple"}
        and managed_explicit is False
    ) or (
        request_kind == "execute"
        and execution_scope == "managed"
        and managed_explicit is True
    )
    if not coherent:
        return _non_executable_decision(
            reason or "incoherent request-intent decision",
            status="incoherent_intent_non_executable",
        )

    if confidence < confidence_threshold:
        return _non_executable_decision(
            (reason + "; " if reason else "") + "low-confidence intent; execution not admitted",
            status="low_confidence_non_executable",
        )

    if request_kind in {"question", "plan"}:
        mode = "chat"
    elif execution_scope == "readonly":
        mode = "readonly"
    elif execution_scope == "simple":
        mode = "simple_execute"
    else:
        mode = "managed"

    if mode in {"readonly", "simple_execute"}:
        retrieval = "none"
    elif mode == "managed" and retrieval == "none":
        retrieval = "kb"

    return {
        "schema_version": REQUEST_INTENT_SCHEMA_VERSION,
        "request_kind": request_kind,
        "execution_scope": execution_scope,
        "managed_intent_explicit": managed_explicit,
        "task_mode": mode,
        "retrieval_mode": retrieval,
        "confidence": confidence,
        "admission_status": (
            "managed_execution_admitted"
            if mode == "managed"
            else "lightweight_execution_admitted"
            if mode in {"readonly", "simple_execute"}
            else "non_executable_admitted"
        ),
        "consultation_required": False,
        "reason": reason[:300] if reason else "classified by fast router",
    }


def normalize_task_mode_decision(
    data: Dict[str, Any],
    *,
    confidence_threshold: float = 0.65,
) -> Dict[str, Any]:
    """Backward-compatible adapter for callers that already hold typed modes.

    New LLM responses use ``normalize_request_intent_decision``.  This adapter
    converts a legacy structured enum, never user prose, into the new closed
    decision and therefore cannot introduce keyword-based execution intent.
    """
    if "request_kind" in data or "execution_scope" in data:
        return normalize_request_intent_decision(
            data,
            confidence_threshold=confidence_threshold,
        )
    legacy_mode = str(data.get("task_mode") or "").strip().lower()
    legacy_projection = {
        "chat": ("question", "none", False),
        "readonly": ("execute", "readonly", False),
        "simple_execute": ("execute", "simple", False),
        "managed": ("execute", "managed", True),
    }.get(legacy_mode)
    if legacy_projection is None:
        return _non_executable_decision(
            str(data.get("reason") or "invalid legacy task_mode"),
            status="invalid_legacy_mode_non_executable",
        )
    request_kind, execution_scope, managed_explicit = legacy_projection
    return normalize_request_intent_decision(
        {
            "schema_version": REQUEST_INTENT_SCHEMA_VERSION,
            "request_kind": request_kind,
            "execution_scope": execution_scope,
            "managed_intent_explicit": managed_explicit,
            "retrieval_mode": str(data.get("retrieval_mode") or "none"),
            "confidence": data.get("confidence"),
            "reason": data.get("reason"),
        },
        confidence_threshold=confidence_threshold,
    )


def managed_execution_admitted(value: Mapping[str, Any] | None) -> bool:
    decision = dict(value or {})
    try:
        schema_version = int(decision.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        schema_version == REQUEST_INTENT_SCHEMA_VERSION
        and decision.get("request_kind") == "execute"
        and decision.get("execution_scope") == "managed"
        and decision.get("managed_intent_explicit") is True
        and decision.get("task_mode") == "managed"
        and decision.get("admission_status") == "managed_execution_admitted"
    )


def build_task_mode_router_prompt(user_query: str) -> str:
    return f"""You are BioMind's semantic request-intent classifier.

Understand the user's intended interaction and return exactly one typed decision.
Do not classify by matching isolated words or phrases. Resolve the meaning of the
complete request, including whether the user is asking a question, asking for an
advisory plan, or explicitly authorizing BioMind to perform actions.

Request kinds:

- question: answer or explain; do not perform local actions.
- plan: provide a proposed approach for the user to review; do not perform it.
- execute: the user is asking BioMind to perform an action, not merely explain it.

Execution scopes, valid only when request_kind is execute:

- readonly: one bounded inspection with no mutation.
- simple: one explicit existing command or script, without workflow expansion.
- managed: a managed multi-step workflow with Consultant, Planner, review, and execution.

Set managed_intent_explicit=true only when the complete request explicitly asks
BioMind to perform the managed work. A question about a workflow and a request for
a workflow proposal are not execution authorization. If meaning is uncertain,
return a low confidence; the Controller will keep it non-executable.

Return JSON only. Do not answer the user and do not include markdown.

Schema:
{{
  "schema_version": 1,
  "request_kind": "question|plan|execute",
  "execution_scope": "none|readonly|simple|managed",
  "managed_intent_explicit": false,
  "retrieval_mode": "none|kb|skill_docs",
  "confidence": 0.0,
  "reason": "short reason"
}}

Retrieval guidance:
- question and plan may use kb or skill_docs.
- readonly and simple execution use none.
- managed execution normally uses kb.

USER REQUEST:
{user_query}
"""


async def classify_task_mode(
    llm: Any,
    user_query: str,
    *,
    confidence_threshold: float = 0.65,
    data_path: str | None = None,
    task_id: str = "",
    trace_metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    try:
        messages = [HumanMessage(content=build_task_mode_router_prompt(user_query))]
        response, snapshot = await ainvoke_with_snapshot(
            llm,
            messages,
            data_path=data_path,
            task_id=task_id,
            metadata={
                "node_name": "triage",
                "call_kind": "task_router",
                **dict(trace_metadata or {}),
            },
        )
        data = _extract_json_object(str(response.content))
        decision = normalize_request_intent_decision(
            data,
            confidence_threshold=confidence_threshold,
        )
        if snapshot:
            decision["llm_call_ref"] = {
                key: snapshot.get(key)
                for key in (
                    "call_id", "snapshot_ref", "request_hash", "response_hash",
                    "task_revision", "plan_revision", "step_id", "attempt_id", "call_kind",
                )
            }
        return decision
    except Exception as exc:
        logger.warning("Task-mode router failed: %s", exc)
        return _default_decision(f"router failed: {type(exc).__name__}")


async def determine_intent(
    llm: Any,
    user_query: str,
    *,
    data_path: str | None = None,
    task_id: str = "",
) -> str:
    """Backward-compatible legacy intent entrypoint.

    The main graph now uses ``classify_task_mode()``.  Older callers still
    receive the historical enum, but it is projected from the same typed LLM
    decision.  This adapter never parses user prose or searches model prose
    for action keywords.
    """
    decision = await classify_task_mode(
        llm,
        user_query,
        data_path=data_path,
        task_id=task_id,
        trace_metadata={"legacy_projection": True},
    )
    request_kind = str(decision.get("request_kind") or "question")
    execution_scope = str(decision.get("execution_scope") or "none")
    if request_kind == "plan":
        return "SOP"
    if request_kind == "execute":
        return "TOOLS" if execution_scope in {"simple", "managed"} else "INFO"
    return "INFO"

"""Typed, session-local access to immutable Skill source snapshots.

The service in this module is deliberately domain agnostic.  It verifies
catalog identity, paths, immutable bytes, revisions and continuation routing;
LLM nodes remain responsible for understanding the exact Skill text that is
returned to them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Sequence

from config.config import (
    MAX_SKILL_BINDING_PROPOSALS_PER_STEP,
    MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION,
    MAX_SKILL_SEARCH_ROUNDS_PER_STEP,
    SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND,
    SKILL_EXCERPT_MAX_CHARS,
    SKILL_FULL_READ_MAX_CHARS,
)


SKILL_ACCESS_SCHEMA_VERSION = 1

REQUEST_SKILL_CONTEXT = "REQUEST_SKILL_CONTEXT"
SEARCH_SKILLS = "SEARCH_SKILLS"
PROPOSE_SKILL_BINDING = "PROPOSE_SKILL_BINDING"

SKILL_CONTEXT_RESULT = "SKILL_CONTEXT_RESULT"
SKILL_SEARCH_RESULT = "SKILL_SEARCH_RESULT"

SKILL_CONTEXT_OPERATIONS = {
    "headings", "section", "search", "range", "resource", "full",
}
SKILL_SCOPES = {"task", "step"}
SKILL_REF_STATUSES = {"active", "superseded", "candidate"}
SEARCH_REQUEST_SCOPES = {"current_step", "task", "unknown"}
SKILL_RELATIONS = {"supplement", "replace"}
SEARCH_RELATION_HINTS = {*SKILL_RELATIONS, "unknown"}

SKILL_RESOLUTION_DECISIONS = {
    "APPROVE_STEP_SUPPLEMENT",
    "ROUTE_TASK_LEVEL_CHANGE",
    "REJECT_SKILL_PROPOSAL",
    "NEED_USER_INPUT",
    "SEARCH_MORE",
}
PLAN_IMPACT_DECISIONS = {
    "BIND_AND_RESUME",
    "PATCH_CURRENT_STEP",
    "REPLAN_SUFFIX",
    "FULL_REPLAN",
    "NEED_USER_INPUT",
    "REJECT_PROPOSAL",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _digest(value: Any) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else _json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(prefix: str, value: Any, length: int = 20) -> str:
    return f"{prefix}-{_digest(value)[:length]}"


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip() for value in values or [] if str(value).strip()
    ))


def _normalize_query(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


class SkillAccessError(ValueError):
    """Deterministic Skill access or routing protocol failure."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        classification: str = "INTERNAL_PROTOCOL",
    ) -> None:
        self.code = str(code)
        self.detail = str(detail)
        self.classification = (
            "ACTION_FORMAT_REPAIR"
            if classification == "INTERNAL_PROTOCOL"
            and self.code == "SKILL_DECISION_SCHEMA_INVALID"
            else str(classification)
        )
        super().__init__(
            f"{self.classification} / {self.code}"
            + (f": {self.detail}" if self.detail else "")
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "classification": self.classification,
            "code": self.code,
            "detail": self.detail,
        }


def new_skill_access_state() -> Dict[str, Any]:
    """Return the sole durable state owner for progressive Skill access."""
    return {
        "schema_version": SKILL_ACCESS_SCHEMA_VERSION,
        "selected_skill_refs": [],
        "snapshots": {},
        "loaded_skill_documents": {},
        "resource_snapshots": {},
        "excerpts": {},
        "excerpt_cache": {},
        "searches": {},
        "search_cache": {},
        "continuations": {},
        "binding_proposals": {},
        "skill_tool_results": {},
        # Lightweight model-visible coverage and residency records.  Bodies
        # remain single-sourced in snapshots/resource_snapshots/excerpts.
        "provider_visible_materials": {},
        "material_visibility_sequence": 0,
        "request_occurrences": {},
        "context_rounds": {},
        "search_occurrences": {},
        "search_rounds": {},
        "proposal_occurrences": {},
        "events": [],
    }


def normalize_skill_access_state(value: Mapping[str, Any] | None) -> Dict[str, Any]:
    state = new_skill_access_state()
    state.update(deepcopy(dict(value or {})))
    state["schema_version"] = SKILL_ACCESS_SCHEMA_VERSION
    for field in (
        "snapshots", "loaded_skill_documents", "resource_snapshots", "excerpts", "excerpt_cache",
        "searches", "search_cache", "continuations", "binding_proposals",
        "skill_tool_results", "provider_visible_materials",
        "request_occurrences", "search_occurrences",
        "proposal_occurrences", "context_rounds", "search_rounds",
    ):
        state[field] = deepcopy(dict(state.get(field) or {}))
    state["selected_skill_refs"] = [
        deepcopy(dict(item))
        for item in state.get("selected_skill_refs") or []
        if isinstance(item, Mapping)
    ]
    state["events"] = [
        deepcopy(dict(item))
        for item in state.get("events") or []
        if isinstance(item, Mapping)
    ][-500:]
    try:
        state["material_visibility_sequence"] = max(
            0, int(state.get("material_visibility_sequence") or 0)
        )
    except (TypeError, ValueError) as exc:
        raise SkillAccessError(
            "SKILL_VISIBLE_MATERIAL_SEQUENCE_INVALID",
            type(state.get("material_visibility_sequence")).__name__,
        ) from exc
    return state


def _record_event(
    state: MutableMapping[str, Any],
    event: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    row = {
        "event": str(event),
        "recorded_at": _now(),
        **deepcopy(dict(payload or {})),
    }
    state.setdefault("events", []).append(row)
    state["events"] = list(state["events"])[-500:]


def record_skill_access_event(
    value: Mapping[str, Any] | None,
    event: str,
    payload: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    state = normalize_skill_access_state(value)
    _record_event(state, event, payload)
    return state


def _runtime_envelope(value: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "caller_node", "caller_invocation_id", "task_revision",
        "plan_revision", "current_step_id",
        "authoritative_user_turn_revision",
    }
    missing = sorted(
        key for key in required if value.get(key) in (None, "")
        and key not in {"current_step_id"}
    )
    if missing:
        raise SkillAccessError(
            "SKILL_RUNTIME_ENVELOPE_INVALID",
            f"missing fields: {', '.join(missing)}",
        )
    caller = str(value.get("caller_node") or "").strip().lower()
    if caller not in {
        "consultant", "planner", "planner_repair", "generate",
        "corrective_generate", "recovery",
    }:
        raise SkillAccessError(
            "SKILL_RUNTIME_ENVELOPE_INVALID",
            f"unsupported caller_node: {caller or '<missing>'}",
        )
    return {
        "caller_node": caller,
        "caller_invocation_id": str(value.get("caller_invocation_id") or ""),
        "task_revision": max(0, int(value.get("task_revision") or 0)),
        "plan_revision": max(0, int(value.get("plan_revision") or 0)),
        "authoritative_user_turn_revision": max(
            0, int(value.get("authoritative_user_turn_revision") or 0)
        ),
        "current_step_id": str(value.get("current_step_id") or ""),
        "attempt_id": str(value.get("attempt_id") or ""),
    }


def _new_continuation(
    state: MutableMapping[str, Any],
    *,
    envelope: Mapping[str, Any],
    request_kind: str,
    request_hash: str,
) -> Dict[str, Any]:
    runtime = _runtime_envelope(envelope)
    identity = {
        **runtime,
        "request_kind": str(request_kind),
        "request_hash": str(request_hash),
        "nonce": uuid.uuid4().hex,
    }
    continuation_id = _stable_id("continuation", identity)
    continuation = {
        "continuation_id": continuation_id,
        **runtime,
        "request_kind": str(request_kind),
        "request_hash": str(request_hash),
        "status": "pending",
        "created_at": _now(),
    }
    state.setdefault("continuations", {})[continuation_id] = continuation
    return continuation


def close_stale_continuations(
    value: Mapping[str, Any] | None,
    *,
    task_revision: int,
    plan_revision: int,
    current_step_id: str,
    authoritative_user_turn_revision: Optional[int] = None,
    reason: str = "revision_changed",
) -> Dict[str, Any]:
    """Close continuations whose revision-bound caller can no longer resume."""
    state = normalize_skill_access_state(value)
    for continuation in state["continuations"].values():
        if not isinstance(continuation, MutableMapping):
            continue
        if str(continuation.get("status") or "") in {"stale", "closed"}:
            continue
        changed = (
            int(continuation.get("task_revision") or 0) != int(task_revision or 0)
            or int(continuation.get("plan_revision") or 0) != int(plan_revision or 0)
            or (
                authoritative_user_turn_revision is not None
                and int(
                    continuation.get("authoritative_user_turn_revision") or 0
                ) != int(authoritative_user_turn_revision or 0)
            )
            or (
                str(continuation.get("current_step_id") or "")
                and str(continuation.get("current_step_id") or "")
                != str(current_step_id or "")
            )
        )
        if changed:
            continuation["status"] = "stale"
            continuation["closed_at"] = _now()
            continuation["close_reason"] = str(reason)
    return state


def mark_continuation_resumed(
    value: Mapping[str, Any] | None,
    continuation_id: str,
    *,
    resumed_invocation_id: str,
) -> Dict[str, Any]:
    state = normalize_skill_access_state(value)
    continuation = state["continuations"].get(str(continuation_id or ""))
    if not isinstance(continuation, MutableMapping):
        raise SkillAccessError(
            "SKILL_CONTINUATION_UNAVAILABLE",
            str(continuation_id or ""),
        )
    if str(continuation.get("status") or "") not in {"served", "rejected"}:
        raise SkillAccessError(
            "SKILL_CONTINUATION_NOT_RESUMABLE",
            f"{continuation_id}: {continuation.get('status')}",
        )
    continuation["status"] = "resumed"
    continuation["resumed_at"] = _now()
    continuation["resumed_invocation_id"] = str(resumed_invocation_id or "")
    _record_event(state, "node_continuation_resumed", {
        "continuation_id": str(continuation_id),
        "caller_node": continuation.get("caller_node"),
    })
    return state


def invalidate_skill_continuations(
    value: Mapping[str, Any] | None,
    *,
    reason: str,
) -> Dict[str, Any]:
    """Close every resumable continuation after a new authority boundary."""
    state = normalize_skill_access_state(value)
    closed_ids = []
    for continuation_id, continuation in state["continuations"].items():
        if not isinstance(continuation, MutableMapping):
            continue
        if str(continuation.get("status") or "") in {
            "closed", "stale", "rejected",
        }:
            continue
        continuation["status"] = "stale"
        continuation["closed_at"] = _now()
        continuation["close_reason"] = str(reason or "authority_changed")
        closed_ids.append(str(continuation_id))
    if closed_ids:
        _record_event(state, "skill_continuations_invalidated", {
            "continuation_ids": closed_ids,
            "reason": str(reason or "authority_changed"),
        })
    return state


def complete_skill_continuation(
    value: Mapping[str, Any] | None,
    continuation_id: str,
    *,
    caller_node: str,
) -> Dict[str, Any]:
    """Record that the resumed caller consumed the served Skill result."""
    state = normalize_skill_access_state(value)
    continuation = state["continuations"].get(str(continuation_id or ""))
    if not isinstance(continuation, MutableMapping):
        return state
    expected = str(continuation.get("caller_node") or "")
    if expected and expected != str(caller_node or ""):
        raise SkillAccessError(
            "SKILL_CONTINUATION_CALLER_MISMATCH",
            f"expected={expected}; actual={caller_node}",
        )
    continuation["status"] = "closed"
    continuation["closed_at"] = _now()
    continuation["close_reason"] = "caller_result_accepted"
    if str(continuation.get("request_kind") or "") == REQUEST_SKILL_CONTEXT:
        result = dict(
            state["skill_tool_results"].get(str(continuation_id or "")) or {}
        )
        _record_event(state, "SKILL_CONTEXT_ACKNOWLEDGED", {
            "request_id": str(
                continuation.get("request_id") or result.get("request_id") or ""
            ),
            "continuation_id": str(continuation_id or ""),
            "skill_ids": _unique(
                item.get("skill_id")
                for item in result.get("results") or []
                if isinstance(item, Mapping)
            ),
            "state_revision": int(continuation.get("task_revision") or 0),
        })
    _record_event(state, "node_continuation_resumed", {
        "continuation_id": str(continuation_id or ""),
        "caller_node": str(caller_node or ""),
    })
    _record_event(state, "node_continuation_completed", {
        "continuation_id": str(continuation_id or ""),
        "caller_node": str(caller_node or ""),
    })
    if str(continuation.get("request_kind") or "") == REQUEST_SKILL_CONTEXT:
        descriptors = [
            {"kind": "excerpt", "excerpt_id": item.get("excerpt_id")}
            for item in result.get("results") or []
            if isinstance(item, Mapping) and item.get("excerpt_id")
        ]
        if descriptors:
            state = record_provider_visible_materials(
                state,
                descriptors,
                phase=str(caller_node or ""),
                invocation_id=str(
                    continuation.get("resumed_invocation_id")
                    or continuation.get("caller_invocation_id")
                    or ""
                ),
                event_id=str(continuation_id or ""),
            )
    return state


def _heading_index(text: str) -> list[Dict[str, Any]]:
    lines = str(text).splitlines()
    headings: list[Dict[str, Any]] = []
    in_fence = False
    for index, line in enumerate(lines, start=1):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        headings.append({
            "heading": match.group(2).strip(),
            "level": len(match.group(1)),
            "start_line": index,
            "end_line": len(lines),
        })
    for position, heading in enumerate(headings):
        for later in headings[position + 1:]:
            if int(later["level"]) <= int(heading["level"]):
                heading["end_line"] = int(later["start_line"]) - 1
                break
    return headings


def _source_slice(text: str, start_line: int, end_line: int) -> str:
    lines = str(text).splitlines(keepends=True)
    if not lines:
        return ""
    start = max(1, int(start_line))
    end = min(len(lines), int(end_line))
    if start > end:
        raise SkillAccessError(
            "SKILL_CONTEXT_RANGE_INVALID",
            f"start_line={start}, end_line={end}",
        )
    selected = "".join(lines[start - 1:end])
    # A source range is line-oriented: exclude only the record separator that
    # follows the final selected line, while preserving every other byte.
    if selected.endswith("\r\n"):
        return selected[:-2]
    if selected.endswith(("\n", "\r")):
        return selected[:-1]
    return selected


def validate_skill_decision(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly validate the three typed node decision schemas."""
    decision = deepcopy(dict(value or {}))
    kind = str(decision.get("decision") or "").strip().upper()
    decision["decision"] = kind
    if kind == REQUEST_SKILL_CONTEXT:
        allowed = {"decision", "reason", "requests"}
        requests = decision.get("requests")
        if (
            not str(decision.get("reason") or "").strip()
            or not isinstance(requests, list)
            or not requests
        ):
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID",
                "REQUEST_SKILL_CONTEXT requires reason and non-empty requests",
            )
        if len(requests) > int(SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND):
            raise SkillAccessError(
                "SKILL_CONTEXT_REQUEST_BUDGET_EXCEEDED",
                f"requests={len(requests)}",
            )
        normalized_requests = []
        for raw in requests:
            if not isinstance(raw, Mapping):
                raise SkillAccessError(
                    "SKILL_DECISION_SCHEMA_INVALID",
                    "each Skill context request must be an object",
                )
            request = deepcopy(dict(raw))
            operation = str(request.get("operation") or "").strip().lower()
            skill_id = str(request.get("skill_id") or "").strip()
            fields = {
                "headings": {"skill_id", "operation"},
                "section": {"skill_id", "operation", "heading"},
                "search": {"skill_id", "operation", "query"},
                "range": {"skill_id", "operation", "start_line", "end_line"},
                "resource": {
                    "skill_id", "operation", "resource_path", "start_line", "end_line",
                },
                "full": {"skill_id", "operation"},
            }
            if operation not in SKILL_CONTEXT_OPERATIONS or not skill_id:
                raise SkillAccessError(
                    "SKILL_DECISION_SCHEMA_INVALID",
                    f"invalid context operation or skill_id: {operation!r}",
                )
            unexpected = sorted(set(request) - fields[operation])
            required = {
                "section": {"heading"},
                "search": {"query"},
                "range": {"start_line", "end_line"},
                "resource": {"resource_path"},
            }.get(operation, set())
            missing = sorted(
                field for field in required
                if request.get(field) in (None, "")
            )
            if unexpected or missing:
                raise SkillAccessError(
                    "SKILL_DECISION_SCHEMA_INVALID",
                    f"operation={operation}; unexpected={unexpected}; missing={missing}",
                )
            if operation in {"range", "resource"}:
                numeric_fields = [
                    field for field in ("start_line", "end_line")
                    if field in request and request.get(field) not in (None, "")
                ]
                try:
                    numeric = {
                        field: int(request[field]) for field in numeric_fields
                    }
                except (TypeError, ValueError) as exc:
                    raise SkillAccessError(
                        "SKILL_DECISION_SCHEMA_INVALID",
                        f"operation={operation}; line range must be integer",
                    ) from exc
                if (
                    any(value < 1 for value in numeric.values())
                    or (
                        "start_line" in numeric
                        and "end_line" in numeric
                        and numeric["end_line"] < numeric["start_line"]
                    )
                ):
                    raise SkillAccessError(
                        "SKILL_DECISION_SCHEMA_INVALID",
                        f"operation={operation}; invalid line range",
                    )
                request.update(numeric)
            request["operation"] = operation
            request["skill_id"] = skill_id
            normalized_requests.append(request)
        decision["requests"] = normalized_requests
    elif kind == SEARCH_SKILLS:
        allowed = {
            "decision", "query", "capability_gap", "desired_outcome",
            "requested_scope", "relation_hint", "queries", "top_k",
            "exclude_skill_ids",
        }
        scope = str(decision.get("requested_scope") or "unknown").strip().lower()
        relation = str(decision.get("relation_hint") or "unknown").strip().lower()
        raw_queries = decision.get("queries")
        legacy_query = str(decision.get("query") or "").strip()
        normalized_queries: list[Dict[str, str]] = []
        if raw_queries is not None:
            if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= 3:
                raise SkillAccessError(
                    "SKILL_DECISION_SCHEMA_INVALID",
                    "SEARCH_SKILLS queries must contain 1-3 objects",
                )
            seen_query_ids = set()
            for index, raw_query in enumerate(raw_queries, start=1):
                item = (
                    dict(raw_query)
                    if isinstance(raw_query, Mapping)
                    else {"text": str(raw_query)}
                )
                unexpected_query_fields = sorted(
                    set(item) - {"query_id", "text", "purpose"}
                )
                text = str(item.get("text") or "").strip()
                query_id = str(item.get("query_id") or f"q{index}").strip()
                if unexpected_query_fields or not text or not query_id or query_id in seen_query_ids:
                    raise SkillAccessError(
                        "SKILL_DECISION_SCHEMA_INVALID",
                        "SEARCH_SKILLS query requires unique query_id, text, and optional purpose",
                    )
                seen_query_ids.add(query_id)
                normalized_queries.append({
                    "query_id": query_id,
                    "text": text,
                    "purpose": str(item.get("purpose") or "")[:300],
                })
            decision["queries"] = normalized_queries
        elif not (
            legacy_query
            and str(decision.get("capability_gap") or "").strip()
            and str(decision.get("desired_outcome") or "").strip()
        ):
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID",
                "SEARCH_SKILLS requires queries or legacy query/capability_gap/desired_outcome",
            )
        if raw_queries is not None and legacy_query:
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID",
                "SEARCH_SKILLS must use either queries or query, not both",
            )
        try:
            top_k = int(decision.get("top_k") or 5)
        except (TypeError, ValueError) as exc:
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID", "SEARCH_SKILLS top_k must be an integer"
            ) from exc
        if not 1 <= top_k <= 8:
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID", "SEARCH_SKILLS top_k must be between 1 and 8"
            )
        excluded = decision.get("exclude_skill_ids") or []
        if not isinstance(excluded, list) or any(
            not isinstance(item, str) or not item.strip() for item in excluded
        ):
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID",
                "SEARCH_SKILLS exclude_skill_ids must be a string list",
            )
        decision["top_k"] = top_k
        decision["exclude_skill_ids"] = _unique(excluded)
        if scope not in SEARCH_REQUEST_SCOPES or relation not in SEARCH_RELATION_HINTS:
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID",
                "invalid requested_scope or relation_hint",
            )
        decision["requested_scope"] = scope
        decision["relation_hint"] = relation
    elif kind == PROPOSE_SKILL_BINDING:
        allowed = {
            "decision", "candidate_skill_id", "candidate_snapshot_id",
            "requested_scope", "relation", "target_step_id", "justification",
            "evidence_refs",
        }
        scope = str(decision.get("requested_scope") or "").strip().lower()
        if scope == "current_step":
            scope = "step"
        relation = str(decision.get("relation") or "").strip().lower()
        evidence = decision.get("evidence_refs")
        justification = str(decision.get("justification") or "").strip()
        normalized_evidence = _unique(evidence if isinstance(evidence, list) else [])
        if (
            not str(decision.get("candidate_skill_id") or "").strip()
            or not str(decision.get("candidate_snapshot_id") or "").strip()
            or scope not in SKILL_SCOPES
            or relation not in SKILL_RELATIONS
            or not isinstance(evidence, list)
            or not normalized_evidence
            or not justification
            or (scope == "step" and not str(decision.get("target_step_id") or "").strip())
        ):
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID",
                "invalid PROPOSE_SKILL_BINDING payload",
            )
        decision["requested_scope"] = scope
        decision["relation"] = relation
        decision["justification"] = justification
        decision["evidence_refs"] = normalized_evidence
    else:
        raise SkillAccessError(
            "SKILL_DECISION_SCHEMA_INVALID",
            f"unsupported decision: {kind or '<missing>'}",
        )
    unexpected = sorted(set(decision) - allowed)
    if unexpected:
        raise SkillAccessError(
            "SKILL_DECISION_SCHEMA_INVALID",
            f"unexpected fields for {kind}: {unexpected}",
        )
    return decision


def parse_typed_skill_decision(text: str) -> Optional[Dict[str, Any]]:
    """Parse one complete top-level typed JSON object.

    A typed decision is a control-plane response, so an object embedded in
    prose, nested below another object, or followed by trailing text must not
    be promoted into a state command.  A single whole-output JSON fence is
    accepted for provider compatibility.
    """
    payload = str(text or "").strip()
    if not payload:
        return None
    fenced = re.fullmatch(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        payload,
        flags=re.IGNORECASE,
    )
    candidate = fenced.group(1).strip() if fenced else payload
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    if str(decoded.get("decision") or "").strip().upper() not in {
        REQUEST_SKILL_CONTEXT, SEARCH_SKILLS, PROPOSE_SKILL_BINDING,
    }:
        return None
    return validate_skill_decision(decoded)


def parse_strict_json_object(
    text: str,
    *,
    error_code: str,
) -> Dict[str, Any]:
    """Parse exactly one whole-output JSON object or one whole JSON fence."""
    payload = str(text or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        payload,
        flags=re.IGNORECASE,
    )
    candidate = fenced.group(1).strip() if fenced else payload
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SkillAccessError(
            str(error_code), "response must be one complete JSON object"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise SkillAccessError(
            str(error_code), "response must be a top-level JSON object"
        )
    return deepcopy(dict(decoded))


class SkillAccessService:
    """Create immutable snapshots and serve typed access/search requests."""

    def __init__(self, skill_manager: Any) -> None:
        if skill_manager is None:
            raise SkillAccessError(
                "SKILL_CONTEXT_UNAVAILABLE", "SkillManager is not configured"
            )
        self.skill_manager = skill_manager
        self.skills_root = Path(skill_manager.skills_root).resolve()

    def catalog_revision(self) -> str:
        status = getattr(self.skill_manager, "catalog_status", None)
        if callable(status):
            typed = dict(status() or {})
            if typed.get("catalog_revision"):
                return str(typed["catalog_revision"])
        catalog = []
        for raw in list(getattr(self.skill_manager, "discovered_skills", []) or []):
            if not isinstance(raw, Mapping):
                continue
            catalog.append({
                "skill_id": str(raw.get("_skill_id") or ""),
                "source_path": str(raw.get("_skill_md") or ""),
                "name": str(raw.get("name") or ""),
                "description": str(raw.get("description") or ""),
                "version": str(raw.get("version") or ""),
            })
        return _stable_id("skill-catalog", catalog, 16)

    def _catalog_status(self) -> Dict[str, Any]:
        status = getattr(self.skill_manager, "catalog_status", None)
        if callable(status):
            typed = deepcopy(dict(status() or {}))
            return {
                "catalog_revision": str(
                    typed.get("catalog_revision")
                    or _stable_id("skill-catalog", typed, 16)
                ),
                "catalog_complete": bool(typed.get("catalog_complete", True)),
                "catalog_errors": deepcopy(list(typed.get("catalog_errors") or [])),
            }
        return {
            "catalog_revision": self.catalog_revision(),
            "catalog_complete": bool(
                getattr(self.skill_manager, "catalog_complete", True)
            ),
            "catalog_errors": deepcopy(list(
                getattr(self.skill_manager, "catalog_errors", []) or []
            )),
        }

    def _registered_source(self, skill_id: str) -> tuple[str, Path, Path]:
        meta = dict(self.skill_manager.get_skill_meta(str(skill_id or "")) or {})
        canonical = str(meta.get("_skill_id") or "").strip()
        if not canonical:
            raise SkillAccessError(
                "SKILL_CONTEXT_UNAVAILABLE",
                f"unregistered skill_id: {skill_id}",
            )
        source = Path(str(meta.get("_skill_md") or ""))
        skill_dir = Path(str(meta.get("_skill_dir") or source.parent))
        if not source.is_absolute() or not skill_dir.is_absolute():
            raise SkillAccessError(
                "SKILL_RESOURCE_OUT_OF_SCOPE", "catalog path must be absolute"
            )
        try:
            resolved_source = source.resolve(strict=True)
            resolved_dir = skill_dir.resolve(strict=True)
            resolved_source.relative_to(self.skills_root)
            resolved_dir.relative_to(self.skills_root)
        except (OSError, ValueError) as exc:
            raise SkillAccessError(
                "SKILL_RESOURCE_OUT_OF_SCOPE", str(source)
            ) from exc
        if resolved_source.name != "SKILL.md" or resolved_source.parent != resolved_dir:
            raise SkillAccessError(
                "SKILL_RESOURCE_OUT_OF_SCOPE", str(resolved_source)
            )
        return canonical, resolved_source, resolved_dir

    def _source_path(self, source: Path) -> str:
        relative = source.relative_to(self.skills_root).as_posix()
        return f"{self.skills_root.name}/{relative}"

    @staticmethod
    def _verified_snapshot(state: Mapping[str, Any], snapshot_id: str) -> Dict[str, Any]:
        snapshot = deepcopy(dict((state.get("snapshots") or {}).get(snapshot_id) or {}))
        if not snapshot:
            raise SkillAccessError(
                "SKILL_CONTEXT_UNAVAILABLE", f"snapshot_id={snapshot_id}"
            )
        exact_text = snapshot.get("exact_text")
        if not isinstance(exact_text, str):
            raise SkillAccessError(
                "SKILL_CONTEXT_UNAVAILABLE", "snapshot content copy is missing"
            )
        observed = _digest(exact_text)
        if observed != str(snapshot.get("content_hash") or ""):
            raise SkillAccessError(
                "SKILL_SNAPSHOT_HASH_MISMATCH", f"snapshot_id={snapshot_id}"
            )
        declared_id = str(snapshot.get("snapshot_id") or "")
        catalog_revision = str(snapshot.get("catalog_revision") or "")
        expected_id = _stable_id("skill-snapshot", {
            "skill_id": str(snapshot.get("skill_id") or ""),
            "content_hash": observed,
            "catalog_revision": catalog_revision,
        })
        source_path = str(snapshot.get("source_path") or "").replace("\\", "/")
        source_parts = Path(source_path).parts
        if (
            declared_id != str(snapshot_id)
            or declared_id != expected_id
            or not str(snapshot.get("skill_id") or "")
            or not catalog_revision
            or not source_path
            or Path(source_path).is_absolute()
            or ".." in source_parts
            or not source_path.endswith("/SKILL.md")
            or not source_path.endswith(
                f"/{str(snapshot.get('skill_id') or '').strip('/')}/SKILL.md"
            )
            or int(snapshot.get("line_count") or 0) != len(exact_text.splitlines())
            or list(snapshot.get("heading_index") or []) != _heading_index(exact_text)
        ):
            raise SkillAccessError(
                "SKILL_SNAPSHOT_LINKAGE_INVALID", f"snapshot_id={snapshot_id}"
            )
        return snapshot

    @staticmethod
    def _verified_excerpt(
        state: Mapping[str, Any],
        excerpt_id: str,
    ) -> Dict[str, Any]:
        excerpt = deepcopy(dict((state.get("excerpts") or {}).get(excerpt_id) or {}))
        if not excerpt or str(excerpt.get("excerpt_id") or "") != str(excerpt_id):
            raise SkillAccessError(
                "SKILL_EXCERPT_LINKAGE_INVALID", f"excerpt_id={excerpt_id}"
            )
        snapshot = SkillAccessService._verified_snapshot(
            state, str(excerpt.get("snapshot_id") or "")
        )
        exact_text = excerpt.get("exact_text")
        if (
            not isinstance(exact_text, str)
            or _digest(exact_text) != str(excerpt.get("content_hash") or "")
            or str(excerpt.get("skill_id") or "")
            != str(snapshot.get("skill_id") or "")
        ):
            raise SkillAccessError(
                "SKILL_EXCERPT_LINKAGE_INVALID", f"excerpt_id={excerpt_id}"
            )
        operation = str(excerpt.get("operation") or "")
        if operation not in {"section", "search", "range", "resource", "full"}:
            raise SkillAccessError(
                "SKILL_EXCERPT_LINKAGE_INVALID", f"operation={operation}"
            )
        request_spec = excerpt.get("request_spec")
        if (
            not isinstance(request_spec, Mapping)
            or str(request_spec.get("skill_id") or "")
            != str(snapshot.get("skill_id") or "")
            or str(request_spec.get("operation") or "") != operation
        ):
            raise SkillAccessError(
                "SKILL_EXCERPT_LINKAGE_INVALID", f"request={excerpt_id}"
            )
        expected_excerpt_id = _stable_id("skill-excerpt", {
            "snapshot_id": snapshot.get("snapshot_id"),
            "request": dict(request_spec),
            "content_hash": _digest(exact_text),
        })
        expected_cache_key = SkillAccessService._excerpt_cache_key(
            snapshot, request_spec
        )
        if (
            expected_excerpt_id != str(excerpt_id)
            or str((state.get("excerpt_cache") or {}).get(expected_cache_key) or "")
            != str(excerpt_id)
        ):
            raise SkillAccessError(
                "SKILL_EXCERPT_LINKAGE_INVALID", f"excerpt_id={excerpt_id}"
            )
        start_line = int(excerpt.get("start_line") or 0)
        end_line = int(excerpt.get("end_line") or 0)
        source_text = str(snapshot.get("exact_text") or "")
        if operation == "resource":
            resource_id = str(excerpt.get("resource_snapshot_id") or "")
            resources = [
                (str(key), dict(item))
                for key, item in (state.get("resource_snapshots") or {}).items()
                if isinstance(item, Mapping)
                and str(item.get("resource_snapshot_id") or "") == resource_id
                and str(item.get("snapshot_id") or "")
                == str(snapshot.get("snapshot_id") or "")
                and str(item.get("resource_path") or "").replace("\\", "/")
                == str(excerpt.get("resource_path") or "").replace("\\", "/")
            ]
            if len(resources) != 1:
                raise SkillAccessError(
                    "SKILL_EXCERPT_LINKAGE_INVALID", f"resource={resource_id}"
                )
            resource_key, resource = resources[0]
            resource_text = resource.get("exact_text")
            resource_path = str(resource.get("resource_path") or "").replace("\\", "/")
            resource_hash = _digest(str(resource_text or ""))
            expected_resource_key = _digest({
                "snapshot_id": snapshot.get("snapshot_id"),
                "resource_path": resource_path,
            })
            if (
                not isinstance(resource_text, str)
                or resource_key != expected_resource_key
                or str(resource.get("snapshot_id") or "")
                != str(snapshot.get("snapshot_id") or "")
                or str(resource.get("skill_id") or "")
                != str(snapshot.get("skill_id") or "")
                or not resource_path
                or Path(resource_path).is_absolute()
                or ".." in Path(resource_path).parts
                or resource_hash != str(resource.get("content_hash") or "")
                or resource_id != f"skill-resource-{resource_hash[:16]}"
                or int(resource.get("line_count") or 0)
                != len(resource_text.splitlines())
                or str(excerpt.get("resource_path") or "").replace("\\", "/")
                != resource_path
                or str(excerpt.get("resource_content_hash") or "")
                != resource_hash
            ):
                raise SkillAccessError(
                    "SKILL_EXCERPT_LINKAGE_INVALID", f"resource={resource_id}"
                )
            source_text = resource_text
        line_count = len(source_text.splitlines())
        if start_line < 1 or end_line < start_line or end_line > max(1, line_count):
            raise SkillAccessError(
                "SKILL_EXCERPT_LINKAGE_INVALID", f"range={start_line}-{end_line}"
            )
        expected_text = (
            source_text
            if operation == "full"
            else _source_slice(source_text, start_line, end_line)
        )
        if exact_text != expected_text:
            raise SkillAccessError(
                "SKILL_EXCERPT_LINKAGE_INVALID", f"excerpt_id={excerpt_id}"
            )
        if operation == "section":
            heading = str(excerpt.get("heading") or "")
            heading_matches = [
                item for item in snapshot.get("heading_index") or []
                if str(item.get("heading") or "") == heading
                and int(item.get("start_line") or 0) == start_line
                and int(item.get("end_line") or 0) == end_line
            ]
            if len(heading_matches) != 1:
                raise SkillAccessError(
                    "SKILL_EXCERPT_LINKAGE_INVALID", f"heading={heading!r}"
                )
        return excerpt

    def ensure_snapshot(
        self,
        value: Mapping[str, Any] | None,
        skill_id: str,
        *,
        scope: str = "task",
        status: str = "candidate",
        selected_at_task_revision: int = 0,
        target_step_id: str = "",
        catalog_revision: str = "",
    ) -> tuple[Dict[str, Any], Dict[str, Any], bool]:
        state = normalize_skill_access_state(value)
        scope = str(scope or "task").strip().lower()
        status = str(status or "candidate").strip().lower()
        target_step_id = str(target_step_id or "").strip()
        if scope not in SKILL_SCOPES or status not in SKILL_REF_STATUSES:
            raise SkillAccessError(
                "SKILL_REFERENCE_INVALID", f"scope={scope}, status={status}"
            )
        if scope == "step" and not target_step_id:
            raise SkillAccessError(
                "SKILL_REFERENCE_INVALID", "step scope requires target_step_id"
            )
        canonical, source, _ = self._registered_source(skill_id)
        effective_catalog_revision = str(
            catalog_revision or self.catalog_revision()
        )
        matching_scope_refs = [
            item for item in state["selected_skill_refs"]
            if str(item.get("skill_id") or "") == canonical
            and str(item.get("snapshot_id") or "")
            and str(item.get("scope") or "task") == scope
            and (
                scope == "task"
                or str(item.get("target_step_id") or "") == target_step_id
            )
        ]
        if status == "active" and matching_scope_refs:
            preferred = next((
                item for item in matching_scope_refs
                if str(item.get("status") or "") == "active"
            ), None) or next((
                item for item in matching_scope_refs
                if str(item.get("status") or "") == "candidate"
                and not str(item.get("candidate_disposition") or "")
            ), None) or matching_scope_refs[-1]
            snapshot = self._verified_snapshot(
                state, str(preferred.get("snapshot_id") or "")
            )
            preferred["status"] = "active"
            preferred["selected_at_task_revision"] = int(
                selected_at_task_revision
                or preferred.get("selected_at_task_revision")
                or 0
            )
            for field in (
                "candidate_disposition", "stale_reason", "rejection_reason",
            ):
                preferred.pop(field, None)
            return state, snapshot, False
        if status == "candidate":
            for candidate_ref in matching_scope_refs:
                snapshot = self._verified_snapshot(
                    state, str(candidate_ref.get("snapshot_id") or "")
                )
                if str(snapshot.get("catalog_revision") or "") != (
                    effective_catalog_revision
                ):
                    continue
                # Searching for an already-active snapshot never downgrades
                # the binding. A prior candidate disposition is cleared only
                # by this fresh catalog-bound search.
                if str(candidate_ref.get("status") or "") == "candidate":
                    for field in (
                        "candidate_disposition",
                        "stale_reason",
                        "rejection_reason",
                    ):
                        candidate_ref.pop(field, None)
                return state, snapshot, False
        try:
            content = source.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillAccessError(
                "SKILL_CONTEXT_UNAVAILABLE", f"{canonical}: {type(exc).__name__}"
            ) from exc
        content_hash = _digest(content)
        catalog_meta = dict(self.skill_manager.get_skill_meta(canonical) or {})
        catalog_content_hash = str(catalog_meta.get("_skill_content_hash") or "")
        if catalog_content_hash and content_hash != catalog_content_hash:
            raise SkillAccessError(
                "SKILL_CATALOG_REVISION_MISMATCH",
                f"Skill source changed after discovery: {canonical}",
            )
        snapshot_id = _stable_id("skill-snapshot", {
            "skill_id": canonical,
            "content_hash": content_hash,
            "catalog_revision": effective_catalog_revision,
        })
        created = snapshot_id not in state["snapshots"]
        if created:
            snapshot = {
                "snapshot_id": snapshot_id,
                "skill_id": canonical,
                "source_path": self._source_path(source),
                "content_hash": content_hash,
                "created_at": _now(),
                "catalog_revision": effective_catalog_revision,
                "heading_index": _heading_index(content),
                "line_count": len(content.splitlines()),
                "exact_text": content,
            }
            state["snapshots"][snapshot_id] = snapshot
        else:
            snapshot = self._verified_snapshot(state, snapshot_id)
        existing_ref = next((
            item for item in matching_scope_refs
            if str(item.get("snapshot_id") or "") == snapshot_id
        ), None)
        if existing_ref is not None:
            if status == "active":
                existing_ref["status"] = "active"
                existing_ref["selected_at_task_revision"] = int(
                    selected_at_task_revision
                    or existing_ref.get("selected_at_task_revision")
                    or 0
                )
            elif str(existing_ref.get("status") or "") == "candidate":
                for field in (
                    "candidate_disposition",
                    "stale_reason",
                    "rejection_reason",
                ):
                    existing_ref.pop(field, None)
            return state, deepcopy(snapshot), False
        state["selected_skill_refs"].append(self._snapshot_ref(
            snapshot,
            scope=scope,
            status=status,
            selected_at_task_revision=selected_at_task_revision,
            target_step_id=target_step_id,
        ))
        if created:
            _record_event(state, "skill_snapshot_created", {
                "skill_id": canonical,
                "snapshot_id": snapshot_id,
                "content_hash": content_hash,
                "status": status,
            })
        return state, deepcopy(snapshot), created

    @staticmethod
    def _snapshot_ref(
        snapshot: Mapping[str, Any],
        *,
        scope: str,
        status: str,
        selected_at_task_revision: int,
        target_step_id: str,
    ) -> Dict[str, Any]:
        ref = {
            "skill_id": str(snapshot.get("skill_id") or ""),
            "source_path": str(snapshot.get("source_path") or ""),
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "content_hash": str(snapshot.get("content_hash") or ""),
            "scope": str(scope),
            "status": str(status),
            "selected_at_task_revision": max(
                0, int(selected_at_task_revision or 0)
            ),
        }
        if scope == "step":
            ref["target_step_id"] = str(target_step_id)
        return ref

    def ensure_selected_skill_refs(
        self,
        value: Mapping[str, Any] | None,
        skill_ids: Sequence[Any],
        *,
        task_revision: int,
        scope: str = "task",
        target_step_id: str = "",
    ) -> Dict[str, Any]:
        state = normalize_skill_access_state(value)
        selected: list[str] = []
        for raw in skill_ids or []:
            state, snapshot, _ = self.ensure_snapshot(
                state,
                str(raw),
                scope=scope,
                status="active",
                selected_at_task_revision=task_revision,
                target_step_id=target_step_id,
            )
            selected.append(str(snapshot["skill_id"]))
        for ref in state["selected_skill_refs"]:
            if (
                str(ref.get("scope") or "") == scope
                and str(ref.get("status") or "") == "active"
                and str(ref.get("skill_id") or "") not in selected
            ):
                ref["status"] = "superseded"
                ref["superseded_at_task_revision"] = int(task_revision or 0)
                ref["superseded_reason"] = "consultant_selected_skill_set_changed"
        return state

    def _resource_snapshot(
        self,
        state: MutableMapping[str, Any],
        snapshot: Mapping[str, Any],
        resource_path: str,
    ) -> Dict[str, Any]:
        raw = str(resource_path or "").strip().replace("\\", "/")
        path = Path(raw)
        if not raw or path.is_absolute() or ".." in path.parts:
            raise SkillAccessError(
                "SKILL_RESOURCE_OUT_OF_SCOPE", raw or "<missing>"
            )
        _, _, skill_dir = self._registered_source(str(snapshot.get("skill_id") or ""))
        try:
            resolved = (skill_dir / path).resolve(strict=True)
            resolved.relative_to(skill_dir)
        except (OSError, ValueError) as exc:
            raise SkillAccessError("SKILL_RESOURCE_OUT_OF_SCOPE", raw) from exc
        if not resolved.is_file() or resolved == skill_dir / "SKILL.md":
            raise SkillAccessError("SKILL_RESOURCE_OUT_OF_SCOPE", raw)
        key = _digest({
            "snapshot_id": snapshot.get("snapshot_id"),
            "resource_path": path.as_posix(),
        })
        cached = deepcopy(dict(state["resource_snapshots"].get(key) or {}))
        if cached:
            if _digest(str(cached.get("exact_text") or "")) != str(
                cached.get("content_hash") or ""
            ):
                raise SkillAccessError(
                    "SKILL_SNAPSHOT_HASH_MISMATCH", f"resource={raw}"
                )
            return cached
        try:
            content = resolved.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillAccessError(
                "SKILL_CONTEXT_UNAVAILABLE", f"resource={raw}: {type(exc).__name__}"
            ) from exc
        cached = {
            "resource_snapshot_id": f"skill-resource-{_digest(content)[:16]}",
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "skill_id": str(snapshot.get("skill_id") or ""),
            "resource_path": path.as_posix(),
            "content_hash": _digest(content),
            "line_count": len(content.splitlines()),
            "exact_text": content,
            "created_at": _now(),
        }
        state["resource_snapshots"][key] = cached
        return deepcopy(cached)

    @staticmethod
    def _snapshot_for_skill(
        state: Mapping[str, Any],
        skill_id: str,
    ) -> Dict[str, Any]:
        refs = [
            dict(item) for item in state.get("selected_skill_refs") or []
            if str(item.get("skill_id") or "") == str(skill_id or "")
            and str(item.get("status") or "") in {"active", "candidate"}
        ]
        if not refs:
            raise SkillAccessError(
                "SKILL_CONTEXT_UNAVAILABLE",
                f"Skill is not active or candidate: {skill_id}",
            )
        return SkillAccessService._verified_snapshot(
            state, str(refs[-1].get("snapshot_id") or "")
        )

    def load_skill_documents(
        self,
        value: Mapping[str, Any] | None,
        skill_ids: Sequence[Any],
        *,
        task_revision: int,
        request_id: str = "",
        max_new_chars: Optional[int] = None,
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Validate and commit canonical immutable Skill documents once."""
        state = normalize_skill_access_state(value)
        documents: List[Dict[str, Any]] = []
        for requested_id in _unique(skill_ids):
            active = any(
                str(item.get("skill_id") or "") == requested_id
                and str(item.get("status") or "") == "active"
                for item in state["selected_skill_refs"]
            )
            state, snapshot, _created = self.ensure_snapshot(
                state,
                requested_id,
                scope="task",
                status="active" if active else "candidate",
                selected_at_task_revision=(task_revision if active else 0),
            )
            skill_id = str(snapshot.get("skill_id") or requested_id)
            prior = dict(
                state["loaded_skill_documents"].get(skill_id) or {}
            )
            cache_hit = bool(
                prior
                and str(prior.get("snapshot_id") or "")
                == str(snapshot.get("snapshot_id") or "")
                and str(prior.get("content_hash") or "")
                == str(snapshot.get("content_hash") or "")
            )
            record = {
                "skill_id": skill_id,
                "snapshot_id": str(snapshot.get("snapshot_id") or ""),
                "source_path": str(snapshot.get("source_path") or ""),
                "content_hash": str(snapshot.get("content_hash") or ""),
                "loaded_at_task_revision": max(0, int(task_revision or 0)),
            }
            state["loaded_skill_documents"][skill_id] = record
            meta = dict(self.skill_manager.get_skill_meta(skill_id) or {})
            documents.append({
                **record,
                "document": str(snapshot.get("exact_text") or ""),
                "document_digest": record["content_hash"],
                "safe_summary": str(
                    meta.get("summary") or meta.get("description") or ""
                ),
                "cache_hit": cache_hit,
            })
            _record_event(
                state,
                "SKILL_CONTEXT_CACHE_HIT" if cache_hit else "SKILL_CONTEXT_LOADED",
                {
                    "request_id": str(request_id or ""),
                    "skill_id": skill_id,
                    "snapshot_id": record["snapshot_id"],
                    "content_hash": record["content_hash"],
                    "state_revision": record["loaded_at_task_revision"],
                },
            )
        new_chars = sum(
            len(str(item.get("document") or ""))
            for item in documents
            if not bool(item.get("cache_hit"))
        )
        if max_new_chars is not None and new_chars > int(max_new_chars):
            raise SkillAccessError(
                "SKILL_DOC_BATCH_CHAR_BUDGET_EXCEEDED",
                f"aggregate_chars={new_chars}; aggregate_limit={int(max_new_chars)}",
            )
        return state, documents

    @staticmethod
    def _excerpt_cache_key(snapshot: Mapping[str, Any], request: Mapping[str, Any]) -> str:
        return _digest({
            "snapshot_id": snapshot.get("snapshot_id"),
            "operation": request.get("operation"),
            "heading": request.get("heading"),
            "query": _normalize_query(request.get("query")),
            "start_line": request.get("start_line"),
            "end_line": request.get("end_line"),
            "resource_path": str(request.get("resource_path") or "").replace("\\", "/"),
        })

    def _store_excerpt(
        self,
        state: MutableMapping[str, Any],
        *,
        snapshot: Mapping[str, Any],
        request: Mapping[str, Any],
        request_id: str,
        exact_text: str,
        start_line: int,
        end_line: int,
        extra: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        cache_key = self._excerpt_cache_key(snapshot, request)
        cached_id = str(state["excerpt_cache"].get(cache_key) or "")
        if cached_id:
            cached = deepcopy(dict(state["excerpts"].get(cached_id) or {}))
            if cached:
                if _digest(str(cached.get("exact_text") or "")) != str(
                    cached.get("content_hash") or ""
                ):
                    raise SkillAccessError(
                        "SKILL_SNAPSHOT_HASH_MISMATCH", f"excerpt_id={cached_id}"
                    )
                cached["cache_hit"] = True
                return cached
        excerpt_id = _stable_id("skill-excerpt", {
            "snapshot_id": snapshot.get("snapshot_id"),
            "request": request,
            "content_hash": _digest(exact_text),
        })
        excerpt = {
            "excerpt_id": excerpt_id,
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "skill_id": str(snapshot.get("skill_id") or ""),
            "operation": str(request.get("operation") or ""),
            "start_line": int(start_line),
            "end_line": int(end_line),
            "content_hash": _digest(exact_text),
            "exact_text": exact_text,
            "request_id": str(request_id),
            "request_spec": deepcopy(dict(request)),
            **deepcopy(dict(extra or {})),
        }
        state["excerpts"][excerpt_id] = excerpt
        state["excerpt_cache"][cache_key] = excerpt_id
        return deepcopy(excerpt)

    def _serve_one(
        self,
        state: MutableMapping[str, Any],
        request: Mapping[str, Any],
        request_id: str,
    ) -> Dict[str, Any]:
        snapshot = self._snapshot_for_skill(state, str(request.get("skill_id") or ""))
        content = str(snapshot.get("exact_text") or "")
        line_count = int(snapshot.get("line_count") or len(content.splitlines()))
        operation = str(request.get("operation") or "")
        if operation == "headings":
            return {
                "skill_id": snapshot["skill_id"],
                "snapshot_id": snapshot["snapshot_id"],
                "operation": operation,
                "heading_index": deepcopy(list(snapshot.get("heading_index") or [])),
            }
        if operation == "full":
            if len(content) > int(SKILL_FULL_READ_MAX_CHARS):
                return {
                    "skill_id": snapshot["skill_id"],
                    "snapshot_id": snapshot["snapshot_id"],
                    "operation": operation,
                    "status": "REQUIRES_NARROWING",
                    "heading_index": deepcopy(list(snapshot.get("heading_index") or [])),
                    "message": "Request one or more sections, searches, or ranges.",
                }
            start_line, end_line, exact_text = 1, max(1, line_count), content
            extra: Dict[str, Any] = {}
        elif operation == "section":
            wanted = str(request.get("heading") or "").strip().casefold()
            matches = [
                item for item in snapshot.get("heading_index") or []
                if str(item.get("heading") or "").strip().casefold() == wanted
            ]
            if len(matches) != 1:
                raise SkillAccessError(
                    "SKILL_CONTEXT_SECTION_UNAVAILABLE",
                    f"heading={request.get('heading')!r}; matches={len(matches)}",
                )
            heading = matches[0]
            start_line = int(heading["start_line"])
            end_line = int(heading["end_line"])
            exact_text = _source_slice(content, start_line, end_line)
            extra = {"heading": str(heading["heading"])}
        elif operation == "range":
            start_line = int(request.get("start_line") or 0)
            end_line = int(request.get("end_line") or 0)
            if start_line < 1 or end_line > line_count:
                raise SkillAccessError(
                    "SKILL_CONTEXT_RANGE_INVALID",
                    f"range={start_line}-{end_line}; line_count={line_count}",
                )
            exact_text = _source_slice(content, start_line, end_line)
            extra = {}
        elif operation == "search":
            query = _normalize_query(request.get("query"))
            terms = _unique(query.split())
            source_lines = content.splitlines()
            hits = [
                index
                for index, line in enumerate(source_lines, start=1)
                if (query and query in line.casefold())
                or any(term in line.casefold() for term in terms)
            ]
            if not hits:
                return {
                    "skill_id": snapshot["skill_id"],
                    "snapshot_id": snapshot["snapshot_id"],
                    "operation": operation,
                    "query": str(request.get("query") or ""),
                    "status": "NO_MATCH",
                    "matched_ranges": [],
                }
            start_line = max(1, min(hits) - 2)
            end_line = min(line_count, max(hits) + 2)
            exact_text = _source_slice(content, start_line, end_line)
            extra = {
                "query": str(request.get("query") or ""),
                "matched_ranges": [{"start_line": start_line, "end_line": end_line}],
            }
        elif operation == "resource":
            resource = self._resource_snapshot(
                state, snapshot, str(request.get("resource_path") or "")
            )
            resource_text = str(resource.get("exact_text") or "")
            resource_lines = int(resource.get("line_count") or len(resource_text.splitlines()))
            start_line = int(request.get("start_line") or 1)
            end_line = int(request.get("end_line") or resource_lines or 1)
            if start_line < 1 or end_line > max(1, resource_lines):
                raise SkillAccessError(
                    "SKILL_CONTEXT_RANGE_INVALID",
                    f"resource range={start_line}-{end_line}; line_count={resource_lines}",
                )
            exact_text = _source_slice(resource_text, start_line, end_line)
            extra = {
                "resource_path": str(resource["resource_path"]),
                "resource_snapshot_id": str(resource["resource_snapshot_id"]),
                "resource_content_hash": str(resource["content_hash"]),
            }
        else:  # pragma: no cover - validate_skill_decision owns this boundary.
            raise SkillAccessError("SKILL_DECISION_SCHEMA_INVALID", operation)
        if (
            operation != "full"
            and len(exact_text) > int(SKILL_EXCERPT_MAX_CHARS)
        ):
            return {
                "skill_id": snapshot["skill_id"],
                "snapshot_id": snapshot["snapshot_id"],
                "operation": operation,
                "status": "REQUIRES_NARROWING",
                "start_line": start_line,
                "end_line": end_line,
                "message": "The requested excerpt exceeds SKILL_EXCERPT_MAX_CHARS.",
                "heading_index": deepcopy(list(snapshot.get("heading_index") or [])),
            }
        return self._store_excerpt(
            state,
            snapshot=snapshot,
            request=request,
            request_id=request_id,
            exact_text=exact_text,
            start_line=start_line,
            end_line=end_line,
            extra=extra,
        )

    def serve_context_request(
        self,
        value: Mapping[str, Any] | None,
        decision: Mapping[str, Any],
        *,
        runtime_envelope: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        state = normalize_skill_access_state(value)
        request = validate_skill_decision(decision)
        if request["decision"] != REQUEST_SKILL_CONTEXT:
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID", "expected REQUEST_SKILL_CONTEXT"
            )
        runtime = _runtime_envelope(runtime_envelope)
        request_hash = _digest(request)
        occurrence_key = _digest({**runtime, "request_hash": request_hash})
        occurrences = int(state["request_occurrences"].get(occurrence_key) or 0) + 1
        state["request_occurrences"][occurrence_key] = occurrences
        round_key = _digest({
            "caller_node": runtime["caller_node"],
            "caller_invocation_id": runtime["caller_invocation_id"],
            "task_revision": runtime["task_revision"],
            "plan_revision": runtime["plan_revision"],
            "current_step_id": runtime["current_step_id"],
        })
        rounds = int(state["context_rounds"].get(round_key) or 0) + 1
        state["context_rounds"][round_key] = rounds
        continuation = _new_continuation(
            state,
            envelope=runtime,
            request_kind=REQUEST_SKILL_CONTEXT,
            request_hash=request_hash,
        )
        request_id = _stable_id("skill-context-request", {
            "continuation_id": continuation["continuation_id"],
            "request_hash": request_hash,
        })
        _record_event(state, "skill_context_requested", {
            "request_id": request_id,
            "continuation_id": continuation["continuation_id"],
            "caller_node": runtime["caller_node"],
            "request_count": len(request["requests"]),
        })
        requested_skill_ids = _unique(
            item.get("skill_id") for item in request["requests"]
        )
        _record_event(state, "SKILL_CONTEXT_REQUESTED", {
            "request_id": request_id,
            "continuation_id": continuation["continuation_id"],
            "skill_ids": requested_skill_ids,
            "state_revision": runtime["task_revision"],
        })
        if rounds > int(MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION):
            result = {
                "decision_result": SKILL_CONTEXT_RESULT,
                "request_id": request_id,
                "continuation_id": continuation["continuation_id"],
                "status": "SKILL_CONTEXT_NO_PROGRESS",
                "classification": "INTERNAL_NODE_PROTOCOL",
                "code": "SKILL_CONTEXT_NO_PROGRESS",
                "results": [],
                "repeated_request_hash": request_hash,
                "rounds": rounds,
            }
        else:
            state, loaded_documents = self.load_skill_documents(
                state,
                requested_skill_ids,
                task_revision=runtime["task_revision"],
                request_id=request_id,
            )
            results = [
                self._serve_one(state, item, request_id)
                for item in request["requests"]
            ]
            for item in results:
                excerpt_id = str(item.get("excerpt_id") or "")
                excerpt = state["excerpts"].get(excerpt_id)
                if not isinstance(excerpt, MutableMapping):
                    continue
                usage = {
                    "caller_node": runtime["caller_node"],
                    "caller_invocation_id": runtime["caller_invocation_id"],
                    "task_revision": runtime["task_revision"],
                    "plan_revision": runtime["plan_revision"],
                    "authoritative_user_turn_revision": runtime[
                        "authoritative_user_turn_revision"
                    ],
                    "step_id": runtime["current_step_id"],
                }
                usages = [
                    deepcopy(dict(row))
                    for row in excerpt.get("usage_scopes") or []
                    if isinstance(row, Mapping)
                ]
                if usage not in usages:
                    usages.append(usage)
                excerpt["usage_scopes"] = usages[-24:]
            status = (
                "REQUIRES_NARROWING"
                if any(str(item.get("status") or "") == "REQUIRES_NARROWING" for item in results)
                else "COMPLETE"
            )
            result = {
                "decision_result": SKILL_CONTEXT_RESULT,
                "request_id": request_id,
                "continuation_id": continuation["continuation_id"],
                "status": status,
                "results": results,
                "cache_hit": bool(loaded_documents) and all(
                    bool(item.get("cache_hit")) for item in loaded_documents
                ),
            }
        continuation = state["continuations"].get(
            str(continuation.get("continuation_id") or "")
        )
        if not isinstance(continuation, MutableMapping):
            raise SkillAccessError(
                "SKILL_CONTINUATION_UNAVAILABLE", request_id
            )
        continuation["status"] = "served"
        continuation["request_id"] = request_id
        continuation["result_status"] = result["status"]
        state["skill_tool_results"][continuation["continuation_id"]] = deepcopy(result)
        _record_event(state, "skill_context_served", {
            "request_id": request_id,
            "continuation_id": continuation["continuation_id"],
            "status": result["status"],
            "excerpt_ids": [
                item.get("excerpt_id") for item in result.get("results") or []
                if item.get("excerpt_id")
            ],
        })
        return state, result

    def search_skills(
        self,
        value: Mapping[str, Any] | None,
        decision: Mapping[str, Any],
        *,
        runtime_envelope: Mapping[str, Any],
        input_roles: Sequence[str] = (),
        max_results: int = 12,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        state = normalize_skill_access_state(value)
        request = validate_skill_decision(decision)
        if request["decision"] != SEARCH_SKILLS:
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID", "expected SEARCH_SKILLS"
            )
        runtime = _runtime_envelope(runtime_envelope)
        query_specs = list(request.get("queries") or []) or [{
            "query_id": "q1",
            "text": str(request.get("query") or ""),
            "purpose": str(request.get("desired_outcome") or ""),
        }]
        query_text = " | ".join(
            str(item.get("text") or "") for item in query_specs
        )
        catalog_status = self._catalog_status()
        catalog_revision = str(catalog_status["catalog_revision"])
        cache_identity = {
            "caller_node": runtime["caller_node"],
            "capability_gap": _normalize_query(request.get("capability_gap")),
            "queries": [
                {
                    "query_id": str(item.get("query_id") or ""),
                    "text": _normalize_query(item.get("text")),
                }
                for item in query_specs
            ],
            "requested_scope": str(request.get("requested_scope") or "unknown"),
            "input_roles": sorted(_unique(input_roles)),
            "max_results": max(1, min(int(max_results), 12)),
            "top_k": int(request.get("top_k") or 5),
            "exclude_skill_ids": sorted(_unique(
                request.get("exclude_skill_ids") or []
            )),
            "catalog_revision": catalog_revision,
            "task_revision": runtime["task_revision"],
            "plan_revision": runtime["plan_revision"],
            "authoritative_user_turn_revision": runtime[
                "authoritative_user_turn_revision"
            ],
            "current_step_id": runtime["current_step_id"],
        }
        cache_key = _digest(cache_identity)
        occurrences = int(state["search_occurrences"].get(cache_key) or 0) + 1
        state["search_occurrences"][cache_key] = occurrences
        round_key = _digest({
            "task_revision": runtime["task_revision"],
            "plan_revision": runtime["plan_revision"],
            "authoritative_user_turn_revision": runtime[
                "authoritative_user_turn_revision"
            ],
            "current_step_id": runtime["current_step_id"],
        })
        rounds = int(state["search_rounds"].get(round_key) or 0) + 1
        state["search_rounds"][round_key] = rounds
        continuation = _new_continuation(
            state,
            envelope=runtime,
            request_kind=SEARCH_SKILLS,
            request_hash=cache_key,
        )
        search_id = _stable_id("skill-search", {
            "continuation_id": continuation["continuation_id"],
            "cache_key": cache_key,
        })
        _record_event(state, "skill_search_requested", {
            "search_id": search_id,
            "continuation_id": continuation["continuation_id"],
            "caller_node": runtime["caller_node"],
            "query": query_text,
            "queries": deepcopy(query_specs),
        })
        cached_search_id = str(state["search_cache"].get(cache_key) or "")
        cached = deepcopy(dict(state["searches"].get(cached_search_id) or {}))
        if not bool(catalog_status["catalog_complete"]):
            result = {
                "decision_result": SKILL_SEARCH_RESULT,
                "search_id": search_id,
                "continuation_id": continuation["continuation_id"],
                "catalog_revision": catalog_revision,
                "catalog_complete": False,
                "status": "CATALOG_INCOMPLETE",
                "classification": "INTERNAL_PROTOCOL",
                "code": "SKILL_CATALOG_INCOMPLETE",
                "query": query_text,
                "queries": deepcopy(query_specs),
                "candidates": [],
                "provider_errors": deepcopy(list(
                    catalog_status.get("catalog_errors") or []
                )),
            }
        elif rounds > int(MAX_SKILL_SEARCH_ROUNDS_PER_STEP):
            result = {
                "decision_result": SKILL_SEARCH_RESULT,
                "search_id": search_id,
                "continuation_id": continuation["continuation_id"],
                "catalog_revision": catalog_revision,
                "catalog_complete": True,
                "status": "SKILL_SEARCH_NO_PROGRESS",
                "classification": "INTERNAL_NODE_PROTOCOL",
                "code": "SKILL_SEARCH_NO_PROGRESS",
                "query": query_text,
                "queries": deepcopy(query_specs),
                "candidates": deepcopy(list(cached.get("candidates") or [])),
                "repeated_search_key": cache_key,
                "rounds": rounds,
            }
        elif cached:
            result = {
                **cached,
                "search_id": search_id,
                "continuation_id": continuation["continuation_id"],
                "cache_hit": True,
            }
        else:
            try:
                if request.get("queries"):
                    search_batch = self.skill_manager.search_skill_queries(
                        query_specs,
                        input_roles=[str(item) for item in input_roles or []],
                        top_k=int(request.get("top_k") or 5),
                        total_limit=max(1, min(int(max_results), 12)),
                        exclude_skill_ids=request.get("exclude_skill_ids") or [],
                    )
                    cards = list(search_batch.get("candidates") or [])
                else:
                    cards = self.skill_manager.rank_skill_candidates(
                        str(request.get("query") or ""),
                        input_roles=[str(item) for item in input_roles or []],
                        max_results=max(1, int(max_results)),
                        source_query_id="q1",
                    )
                candidates = []
                scope = (
                    "step"
                    if request.get("requested_scope") == "current_step"
                    else "task"
                )
                working_state = normalize_skill_access_state(state)
                for card in cards or []:
                    skill_id = str(card.get("skill_id") or "")
                    working_state, snapshot, _ = self.ensure_snapshot(
                        working_state,
                        skill_id,
                        scope=scope,
                        status="candidate",
                        selected_at_task_revision=runtime["task_revision"],
                        target_step_id=(
                            runtime["current_step_id"] if scope == "step" else ""
                        ),
                        catalog_revision=catalog_revision,
                    )
                    if str(snapshot.get("catalog_revision") or "") != catalog_revision:
                        raise SkillAccessError(
                            "SKILL_CATALOG_REVISION_MISMATCH",
                            f"snapshot={snapshot.get('catalog_revision')}; search={catalog_revision}",
                        )
                    candidates.append({
                        "skill_id": str(snapshot["skill_id"]),
                        "name": str(card.get("title") or ""),
                        "summary": str(card.get("summary") or "")[:800],
                        "description": str(card.get("summary") or "")[:800],
                        "domain": str(card.get("domain") or card.get("category") or ""),
                        "tags": _unique(card.get("tags") or []),
                        "source_path": str(snapshot["source_path"]),
                        "candidate_snapshot_id": str(snapshot["snapshot_id"]),
                        "content_hash": str(snapshot["content_hash"]),
                        "document_digest": str(card.get("document_digest") or snapshot["content_hash"]),
                        "document_loaded": False,
                        "declares_executable_runtime": bool(
                            card.get("declares_executable_runtime")
                        ),
                        "source_query_id": str(card.get("source_query_id") or "q1"),
                        "matched_metadata_fields": _unique(
                            card.get("matched_metadata_fields") or []
                        ),
                        "deterministic_score": int(
                            card.get("deterministic_score") or card.get("score") or 0
                        ),
                        "match_evidence": _unique(card.get("reasons") or []),
                    })
                final_catalog_status = self._catalog_status()
                final_revision = str(final_catalog_status["catalog_revision"])
                if (
                    not bool(final_catalog_status["catalog_complete"])
                    or final_revision != catalog_revision
                ):
                    result = {
                        "decision_result": SKILL_SEARCH_RESULT,
                        "search_id": search_id,
                        "continuation_id": continuation["continuation_id"],
                        "catalog_revision": catalog_revision,
                        "observed_catalog_revision": final_revision,
                        "catalog_complete": False,
                        "status": "CATALOG_INCOMPLETE",
                        "classification": "INTERNAL_PROTOCOL",
                        "code": (
                            "SKILL_CATALOG_REVISION_CHANGED"
                            if final_revision != catalog_revision
                            else "SKILL_CATALOG_INCOMPLETE"
                        ),
                        "query": query_text,
                        "queries": deepcopy(query_specs),
                        "candidates": [],
                        "provider_errors": deepcopy(list(
                            final_catalog_status.get("catalog_errors") or []
                        )),
                    }
                else:
                    state = working_state
                    result = {
                        "decision_result": SKILL_SEARCH_RESULT,
                        "search_id": search_id,
                        "continuation_id": continuation["continuation_id"],
                        "catalog_revision": catalog_revision,
                        "catalog_complete": True,
                        "status": "MATCHES_FOUND" if candidates else "NO_MATCH",
                        "query": query_text,
                        "queries": deepcopy(query_specs),
                        "candidates": candidates,
                        "cache_hit": False,
                    }
                    if not candidates:
                        result.update({
                            "classification": "CAPABILITY_GAP",
                            "code": "SKILL_NOT_FOUND",
                        })
            except SkillAccessError as exc:
                latest = self._catalog_status()
                incomplete = (
                    not bool(latest["catalog_complete"])
                    or str(latest["catalog_revision"]) != catalog_revision
                    or exc.code == "SKILL_CATALOG_REVISION_MISMATCH"
                )
                result = {
                    "decision_result": SKILL_SEARCH_RESULT,
                    "search_id": search_id,
                    "continuation_id": continuation["continuation_id"],
                    "catalog_revision": catalog_revision,
                    "catalog_complete": False if incomplete else True,
                    "status": "CATALOG_INCOMPLETE" if incomplete else "SEARCH_ERROR",
                    "classification": "INTERNAL_PROTOCOL",
                    "code": (
                        "SKILL_CATALOG_REVISION_CHANGED"
                        if incomplete
                        else "SKILL_SEARCH_CANDIDATE_SNAPSHOT_ERROR"
                    ),
                    "query": query_text,
                    "queries": deepcopy(query_specs),
                    "candidates": [],
                    "provider_errors": deepcopy(list(
                        latest.get("catalog_errors") or []
                    )),
                    "cause": exc.to_dict(),
                }
            except Exception as exc:
                latest = self._catalog_status()
                incomplete = (
                    not bool(latest["catalog_complete"])
                    or str(latest["catalog_revision"]) != catalog_revision
                )
                result = {
                    "decision_result": SKILL_SEARCH_RESULT,
                    "search_id": search_id,
                    "continuation_id": continuation["continuation_id"],
                    "catalog_revision": catalog_revision,
                    "catalog_complete": False if incomplete else True,
                    "status": "CATALOG_INCOMPLETE" if incomplete else "SEARCH_ERROR",
                    "classification": "INTERNAL_PROTOCOL",
                    "code": (
                        "SKILL_CATALOG_REVISION_CHANGED"
                        if incomplete
                        else "SKILL_SEARCH_ERROR"
                    ),
                    "query": str(request.get("query") or ""),
                    "candidates": [],
                    "provider_errors": deepcopy(list(
                        latest.get("catalog_errors") or []
                    )),
                    "error": f"{type(exc).__name__}: {exc}",
                }
        continuation = state["continuations"][continuation["continuation_id"]]
        continuation["status"] = "served"
        continuation["search_id"] = search_id
        continuation["result_status"] = result["status"]
        state["searches"][search_id] = deepcopy(result)
        if result["status"] in {
            "MATCHES_FOUND", "NO_MATCH", "CATALOG_INCOMPLETE", "SEARCH_ERROR",
        }:
            state["search_cache"].setdefault(cache_key, search_id)
        state["skill_tool_results"][continuation["continuation_id"]] = deepcopy(result)
        _record_event(state, "skill_search_completed", {
            "search_id": search_id,
            "continuation_id": continuation["continuation_id"],
            "status": result["status"],
            "candidate_skill_ids": [
                item.get("skill_id") for item in result.get("candidates") or []
            ],
        })
        return state, result

    def propose_binding(
        self,
        value: Mapping[str, Any] | None,
        decision: Mapping[str, Any],
        *,
        runtime_envelope: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        state = normalize_skill_access_state(value)
        proposal = validate_skill_decision(decision)
        if proposal["decision"] != PROPOSE_SKILL_BINDING:
            raise SkillAccessError(
                "SKILL_DECISION_SCHEMA_INVALID", "expected PROPOSE_SKILL_BINDING"
            )
        runtime = _runtime_envelope(runtime_envelope)
        permission_matrix = {
            "generate": {("step", "supplement"), ("task", "supplement"), ("task", "replace")},
            "corrective_generate": {("step", "supplement"), ("task", "supplement"), ("task", "replace")},
            "recovery": {("step", "supplement"), ("task", "supplement"), ("task", "replace")},
            "planner": {("step", "supplement")},
            "planner_repair": {("step", "supplement")},
            "consultant": {("step", "supplement"), ("task", "supplement"), ("task", "replace")},
        }
        requested_relation = (
            str(proposal["requested_scope"]), str(proposal["relation"])
        )
        if requested_relation not in permission_matrix[runtime["caller_node"]]:
            raise SkillAccessError(
                "SKILL_BINDING_CALLER_PERMISSION_DENIED",
                f"caller={runtime['caller_node']}; scope={requested_relation[0]}; relation={requested_relation[1]}",
            )
        if (
            proposal["requested_scope"] == "step"
            and str(proposal.get("target_step_id") or "")
            != runtime["current_step_id"]
        ):
            raise SkillAccessError(
                "SKILL_BINDING_TARGET_STEP_MISMATCH",
                f"proposal={proposal.get('target_step_id')}; current={runtime['current_step_id']}",
            )
        snapshot = self._verified_snapshot(
            state, str(proposal.get("candidate_snapshot_id") or "")
        )
        if str(snapshot.get("skill_id") or "") != str(
            proposal.get("candidate_skill_id") or ""
        ):
            raise SkillAccessError(
                "SKILL_BINDING_SNAPSHOT_MISMATCH",
                str(proposal.get("candidate_snapshot_id") or ""),
            )
        candidate_ref = next((
            item for item in state["selected_skill_refs"]
            if str(item.get("snapshot_id") or "") == str(snapshot["snapshot_id"])
            and str(item.get("status") or "") == "candidate"
            and not str(item.get("candidate_disposition") or "")
        ), None)
        if candidate_ref is None:
            raise SkillAccessError(
                "SKILL_BINDING_CANDIDATE_UNAVAILABLE",
                str(proposal.get("candidate_skill_id") or ""),
            )
        candidate_scope = str(candidate_ref.get("scope") or "")
        candidate_target_step = str(candidate_ref.get("target_step_id") or "")
        if (
            candidate_scope not in SKILL_SCOPES
            or (candidate_scope == "step" and not candidate_target_step)
            or any(
                str(candidate_ref.get(field) or "")
                != str(snapshot.get(field) or "")
                for field in (
                    "skill_id", "snapshot_id", "source_path", "content_hash",
                )
            )
        ):
            raise SkillAccessError(
                "SKILL_REFERENCE_LINKAGE_INVALID",
                f"candidate={proposal.get('candidate_skill_id')}",
            )
        matching_excerpt_ids = []
        for evidence_ref in proposal.get("evidence_refs") or []:
            if str(evidence_ref) not in state["excerpts"]:
                continue
            excerpt = self._verified_excerpt(state, str(evidence_ref))
            if str(excerpt.get("snapshot_id") or "") != str(snapshot["snapshot_id"]):
                continue
            usages = [
                dict(item)
                for item in excerpt.get("usage_scopes") or []
                if isinstance(item, Mapping)
            ]
            if any(
                str(usage.get("caller_node") or "") == runtime["caller_node"]
                and str(usage.get("caller_invocation_id") or "")
                == runtime["caller_invocation_id"]
                and int(usage.get("task_revision") or 0) == runtime["task_revision"]
                and int(usage.get("plan_revision") or 0) == runtime["plan_revision"]
                and int(
                    usage.get("authoritative_user_turn_revision") or 0
                ) == runtime["authoritative_user_turn_revision"]
                and str(usage.get("step_id") or "") == runtime["current_step_id"]
                for usage in usages
            ):
                matching_excerpt_ids.append(str(evidence_ref))
        if not matching_excerpt_ids:
            raise SkillAccessError(
                "SKILL_BINDING_EVIDENCE_REQUIRED",
                "at least one candidate excerpt must be served to the same logical invocation and step",
            )
        counter_key = runtime["current_step_id"] or "task"
        count = int(state["proposal_occurrences"].get(counter_key) or 0) + 1
        state["proposal_occurrences"][counter_key] = count
        if count > int(MAX_SKILL_BINDING_PROPOSALS_PER_STEP):
            raise SkillAccessError(
                "SKILL_BINDING_NO_PROGRESS",
                f"step_id={counter_key}; proposals={count}",
            )
        proposal_id = _stable_id("skill-proposal", {
            **proposal,
            "runtime": runtime,
            "ordinal": count,
        })
        route = (
            "planner_plan_impact_review"
            if runtime["caller_node"] in {"planner", "planner_repair"}
            and proposal["requested_scope"] == "step"
            and proposal["relation"] == "supplement"
            else "consultant_skill_resolution"
        )
        record = {
            "proposal_id": proposal_id,
            **deepcopy(proposal),
            "caller_node": runtime["caller_node"],
            "caller_invocation_id": runtime["caller_invocation_id"],
            "base_task_revision": runtime["task_revision"],
            "base_plan_revision": runtime["plan_revision"],
            "base_user_turn_revision": runtime[
                "authoritative_user_turn_revision"
            ],
            "base_step_id": runtime["current_step_id"],
            "status": (
                "approved_for_plan_impact"
                if route == "planner_plan_impact_review"
                else "pending_review"
            ),
            "review_route": route,
            "created_at": _now(),
            "verified_excerpt_refs": matching_excerpt_ids,
            "candidate_ref_scope": candidate_scope,
            "candidate_ref_target_step_id": candidate_target_step,
        }
        state["binding_proposals"][proposal_id] = record
        _record_event(state, "skill_binding_proposed", {
            "proposal_id": proposal_id,
            "candidate_skill_id": proposal["candidate_skill_id"],
            "requested_scope": proposal["requested_scope"],
            "relation": proposal["relation"],
            "review_route": route,
        })
        if route == "planner_plan_impact_review":
            _record_event(state, "skill_binding_reviewed", {
                "proposal_id": proposal_id,
                "decision": "PLANNER_STEP_SUPPLEMENT_PROPOSAL",
                "status": "approved_for_plan_impact",
            })
        return state, deepcopy(record)


def canonical_loaded_skill_documents(
    value: Mapping[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Return verified documents from the canonical Skill access store."""
    state = normalize_skill_access_state(value)
    documents: List[Dict[str, Any]] = []
    for skill_id, raw in sorted(
        state["loaded_skill_documents"].items(),
        key=lambda item: str(item[0]),
    ):
        if not isinstance(raw, Mapping):
            raise SkillAccessError(
                "SKILL_LOADED_DOCUMENT_INVALID", f"skill_id={skill_id}"
            )
        record = dict(raw)
        snapshot = SkillAccessService._verified_snapshot(
            state, str(record.get("snapshot_id") or "")
        )
        if (
            str(record.get("skill_id") or "") != str(skill_id)
            or str(snapshot.get("skill_id") or "") != str(skill_id)
            or str(record.get("content_hash") or "")
            != str(snapshot.get("content_hash") or "")
            or str(record.get("source_path") or "")
            != str(snapshot.get("source_path") or "")
        ):
            raise SkillAccessError(
                "SKILL_LOADED_DOCUMENT_INVALID", f"skill_id={skill_id}"
            )
        documents.append({
            "skill_id": str(skill_id),
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "source_path": str(snapshot.get("source_path") or ""),
            "content_hash": str(snapshot.get("content_hash") or ""),
            "document_digest": str(snapshot.get("content_hash") or ""),
            "loaded_at_task_revision": int(
                record.get("loaded_at_task_revision") or 0
            ),
            "document": str(snapshot.get("exact_text") or ""),
        })
    return documents


def _material_record_from_descriptor(
    state: Mapping[str, Any],
    descriptor: Mapping[str, Any],
) -> tuple[Dict[str, Any], str]:
    """Resolve one visibility descriptor to immutable provenance and body.

    The returned record intentionally excludes the body.  The second return
    value is used only for hash verification and phase assembly.
    """

    kind = str(descriptor.get("kind") or "").strip().lower()
    if kind == "skill_document":
        snapshot = SkillAccessService._verified_snapshot(
            state, str(descriptor.get("snapshot_id") or "")
        )
        skill_id = str(snapshot.get("skill_id") or "")
        loaded = dict((state.get("loaded_skill_documents") or {}).get(skill_id) or {})
        if (
            str(loaded.get("snapshot_id") or "") != str(snapshot.get("snapshot_id") or "")
            or str(loaded.get("content_hash") or "") != str(snapshot.get("content_hash") or "")
        ):
            raise SkillAccessError(
                "SKILL_VISIBLE_MATERIAL_NOT_LOADED", f"skill_id={skill_id}"
            )
        body = str(snapshot.get("exact_text") or "")
        identity = {
            "source_kind": "skill_document",
            "skill_id": skill_id,
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "source_path": str(snapshot.get("source_path") or ""),
            "content_hash": str(snapshot.get("content_hash") or ""),
            "coverage": {"kind": "full"},
        }
        source_ref = f"skill-snapshot:{identity['snapshot_id']}"
    elif kind == "excerpt":
        excerpt = SkillAccessService._verified_excerpt(
            state, str(descriptor.get("excerpt_id") or "")
        )
        body = str(excerpt.get("exact_text") or "")
        resource_snapshot_id = str(excerpt.get("resource_snapshot_id") or "")
        identity = {
            "source_kind": (
                "skill_resource" if resource_snapshot_id else "skill_document_excerpt"
            ),
            "skill_id": str(excerpt.get("skill_id") or ""),
            "snapshot_id": str(excerpt.get("snapshot_id") or ""),
            "excerpt_id": str(excerpt.get("excerpt_id") or ""),
            "content_hash": str(excerpt.get("content_hash") or ""),
            "coverage": {
                "kind": (
                    "full"
                    if str(excerpt.get("operation") or "") == "full"
                    else "range"
                ),
                "start_line": int(excerpt.get("start_line") or 1),
                "end_line": int(excerpt.get("end_line") or 1),
            },
        }
        if resource_snapshot_id:
            identity.update({
                "resource_snapshot_id": resource_snapshot_id,
                "resource_path": str(excerpt.get("resource_path") or ""),
                "resource_content_hash": str(
                    excerpt.get("resource_content_hash") or ""
                ),
            })
        source_ref = f"skill-excerpt:{identity['excerpt_id']}"
    else:
        raise SkillAccessError(
            "SKILL_VISIBLE_MATERIAL_KIND_INVALID", kind or "<missing>"
        )

    supplied_hash = str(descriptor.get("content_hash") or "")
    if supplied_hash and supplied_hash != str(identity.get("content_hash") or ""):
        raise SkillAccessError(
            "SKILL_VISIBLE_MATERIAL_HASH_MISMATCH", source_ref
        )
    identity["source_ref"] = source_ref
    material_id = _stable_id("skill-material", identity)
    return {
        "schema_version": 1,
        "material_id": material_id,
        **identity,
    }, body


def resolve_skill_material_descriptor(
    value: Mapping[str, Any] | None,
    descriptor: Mapping[str, Any],
) -> Dict[str, Any]:
    """Resolve one exact, already-served Skill material for context ownership.

    This is the public identity boundary used when a phase-local Skill result
    must remain attributable during a later context compaction.  It performs
    the same immutable snapshot/excerpt linkage and hash checks as visibility
    recording; it does not itself assert provider visibility or mutate state.
    """

    state = normalize_skill_access_state(value)
    record, body = _material_record_from_descriptor(state, descriptor)
    return {**deepcopy(record), "body": body, "residency": "exact"}


def record_provider_visible_materials(
    value: Mapping[str, Any] | None,
    descriptors: Sequence[Mapping[str, Any]],
    *,
    phase: str,
    invocation_id: str,
    event_id: str,
) -> Dict[str, Any]:
    """Commit exact material coverage once a provider-visible request exists.

    This is an auditable proxy for visibility, not a claim about model
    cognition.  Replaying the same event is idempotent; a later genuine event
    is retained without copying the source body.
    """

    state = normalize_skill_access_state(value)
    records = state["provider_visible_materials"]
    for descriptor in descriptors or ():
        if not isinstance(descriptor, Mapping):
            raise SkillAccessError(
                "SKILL_VISIBLE_MATERIAL_DESCRIPTOR_INVALID",
                type(descriptor).__name__,
            )
        record, _body = _material_record_from_descriptor(state, descriptor)
        material_id = str(record["material_id"])
        prior = deepcopy(dict(records.get(material_id) or {}))
        if prior:
            immutable_fields = (
                "skill_id", "snapshot_id", "excerpt_id", "source_ref",
                "content_hash", "coverage", "resource_snapshot_id",
                "resource_content_hash", "resource_path", "source_path",
            )
            if any(prior.get(field) != record.get(field) for field in immutable_fields):
                raise SkillAccessError(
                    "SKILL_VISIBLE_MATERIAL_IDENTITY_CONFLICT", material_id
                )
        else:
            state["material_visibility_sequence"] += 1
            prior = {
                **record,
                "first_seen_sequence": state["material_visibility_sequence"],
                "residency": "exact",
                "visibility_events": [],
            }
        visibility = {
            "phase": str(phase or ""),
            "invocation_id": str(invocation_id or ""),
            "event_id": str(event_id or ""),
            "selection_refs": [
                {
                    "status": str(ref.get("status") or ""),
                    "scope": str(ref.get("scope") or "task"),
                    "target_step_id": str(ref.get("target_step_id") or ""),
                    "selected_at_task_revision": int(
                        ref.get("selected_at_task_revision") or 0
                    ),
                }
                for ref in state["selected_skill_refs"]
                if isinstance(ref, Mapping)
                and str(ref.get("snapshot_id") or "")
                == str(record.get("snapshot_id") or "")
            ],
        }
        events = [
            deepcopy(dict(item))
            for item in prior.get("visibility_events") or []
            if isinstance(item, Mapping)
        ]
        if visibility not in events:
            events.append(visibility)
        prior["visibility_events"] = events[-32:]
        records[material_id] = prior
    state["provider_visible_materials"] = records
    return state


def selected_skill_materials_for_carry(
    value: Mapping[str, Any] | None,
    *,
    current_step_id: str = "",
    residency_state: Mapping[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Resolve selected, actually-visible materials for a new model phase.

    Selection is a mechanical identity/scope filter.  No filename, extension,
    heading, age, or task-language relevance heuristic participates.
    """

    state = normalize_skill_access_state(value)
    active_refs = {
        str(ref.get("snapshot_id") or ""): deepcopy(dict(ref))
        for ref in state["selected_skill_refs"]
        if isinstance(ref, Mapping)
        and str(ref.get("status") or "") == "active"
        and (
            str(ref.get("scope") or "task") == "task"
            or (
                str(ref.get("scope") or "") == "step"
                and str(ref.get("target_step_id") or "") == str(current_step_id or "")
            )
        )
    }
    overrides = {
        str(key): deepcopy(dict(item))
        for key, item in dict(residency_state or {}).items()
        if isinstance(item, Mapping)
    }
    candidates: list[tuple[Dict[str, Any], str]] = []
    for raw in state["provider_visible_materials"].values():
        if not isinstance(raw, Mapping):
            continue
        record = deepcopy(dict(raw))
        active_ref = active_refs.get(str(record.get("snapshot_id") or ""))
        if not active_ref:
            continue
        descriptor = (
            {"kind": "excerpt", "excerpt_id": record.get("excerpt_id")}
            if record.get("excerpt_id")
            else {
                "kind": "skill_document",
                "snapshot_id": record.get("snapshot_id"),
            }
        )
        verified, body = _material_record_from_descriptor(state, descriptor)
        if any(
            verified.get(field) != record.get(field)
            for field in (
                "material_id", "skill_id", "snapshot_id", "excerpt_id",
                "source_ref", "content_hash", "coverage",
                "resource_snapshot_id", "resource_content_hash",
            )
        ):
            raise SkillAccessError(
                "SKILL_VISIBLE_MATERIAL_LINKAGE_INVALID",
                str(record.get("material_id") or ""),
            )
        override = overrides.get(str(record.get("material_id") or "")) or {}
        residency = str(override.get("residency") or record.get("residency") or "exact")
        if residency not in {"exact", "summarized", "externalized"}:
            raise SkillAccessError(
                "SKILL_MATERIAL_RESIDENCY_INVALID", residency
            )
        public = {
            key: deepcopy(item)
            for key, item in record.items()
            if key not in {"visibility_events"}
        }
        public["residency"] = residency
        public["selected_scope"] = str(active_ref.get("scope") or "task")
        public["selected_target_step_id"] = str(
            active_ref.get("target_step_id") or ""
        )
        public["selected_at_task_revision"] = int(
            active_ref.get("selected_at_task_revision") or 0
        )
        if residency == "exact":
            public["body"] = body
        else:
            if (
                str(override.get("content_hash") or record.get("content_hash") or "")
                != str(record.get("content_hash") or "")
                or not str(override.get("summary") or "").strip()
                or not str(override.get("retrieval_ref") or record.get("source_ref") or "").strip()
            ):
                raise SkillAccessError(
                    "SKILL_MATERIAL_RECOVERY_STATE_INVALID",
                    str(record.get("material_id") or ""),
                )
            public.update({
                "summary": str(override.get("summary") or ""),
                "missing_details": str(override.get("missing_details") or ""),
                "retrieval_ref": str(
                    override.get("retrieval_ref") or record.get("source_ref") or ""
                ),
                "compaction_request_id": str(
                    override.get("compaction_request_id") or ""
                ),
            })
        candidates.append((public, body))

    # A visible full SKILL.md mechanically covers its own earlier line excerpts.
    # Resource files remain distinct even when their text happens to match.
    full_snapshots = {
        str(item.get("snapshot_id") or "")
        for item, _ in candidates
        if str(item.get("source_kind") or "") == "skill_document"
        and str((item.get("coverage") or {}).get("kind") or "") == "full"
        and str(item.get("residency") or "") == "exact"
    }
    result = [
        item
        for item, _body in candidates
        if not (
            str(item.get("source_kind") or "") == "skill_document_excerpt"
            and str(item.get("snapshot_id") or "") in full_snapshots
            and str(item.get("residency") or "") == "exact"
        )
    ]
    result.sort(key=lambda item: (
        int(item.get("first_seen_sequence") or 0),
        str(item.get("material_id") or ""),
    ))
    return result


def project_skill_context(
    value: Mapping[str, Any] | None,
    *,
    caller_node: str,
    current_step_id: str = "",
    continuation_id: str = "",
    include_candidate_snapshot_ids: Iterable[str] = (),
    include_excerpt_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    """Project active context plus explicitly authorised candidate evidence.

    Candidate visibility is opt-in and snapshot/excerpt bounded.  It is used by
    restricted semantic review modes such as Planner plan-impact review; a
    normal node projection still sees active refs only.
    """
    state = normalize_skill_access_state(value)
    caller = str(caller_node or "").strip().lower()
    allowed_candidate_snapshots = {
        str(item) for item in include_candidate_snapshot_ids or [] if str(item)
    }
    allowed_excerpt_ids = {
        str(item) for item in include_excerpt_ids or [] if str(item)
    }
    visible_refs = []
    for item in state["selected_skill_refs"]:
        status = str(item.get("status") or "")
        scope = str(item.get("scope") or "")
        target_step_id = str(item.get("target_step_id") or "")
        if (
            status not in SKILL_REF_STATUSES
            or scope not in SKILL_SCOPES
            or (scope == "step" and not target_step_id)
        ):
            raise SkillAccessError(
                "SKILL_REFERENCE_INVALID",
                f"skill_id={item.get('skill_id')}; scope={scope}",
            )
        snapshot = SkillAccessService._verified_snapshot(
            state, str(item.get("snapshot_id") or "")
        )
        if any(
            str(item.get(field) or "") != str(snapshot.get(field) or "")
            for field in ("skill_id", "snapshot_id", "source_path", "content_hash")
        ):
            raise SkillAccessError(
                "SKILL_REFERENCE_LINKAGE_INVALID",
                f"skill_id={item.get('skill_id')}",
            )
        visible_candidate = (
            status == "candidate"
            and str(item.get("snapshot_id") or "")
            in allowed_candidate_snapshots
            and not str(item.get("candidate_disposition") or "")
        )
        if status != "active" and not visible_candidate:
            continue
        if scope == "step" and target_step_id != str(current_step_id or ""):
            continue
        visible_refs.append({
            key: deepcopy(item.get(key))
            for key in (
                "skill_id", "snapshot_id", "source_path", "content_hash",
                "scope", "status", "selected_at_task_revision", "target_step_id",
            )
            if item.get(key) not in (None, "")
        })
    visible_snapshot_ids = {
        str(item.get("snapshot_id") or "") for item in visible_refs
    }
    headings = []
    for snapshot_id in sorted(visible_snapshot_ids):
        snapshot = SkillAccessService._verified_snapshot(state, snapshot_id)
        headings.append({
            "skill_id": snapshot.get("skill_id"),
            "snapshot_id": snapshot_id,
            "heading_index": deepcopy(list(snapshot.get("heading_index") or [])),
        })
    current_result = (
        state["skill_tool_results"].get(str(continuation_id))
        if continuation_id
        else None
    )
    if isinstance(current_result, Mapping):
        for result_item in current_result.get("results") or []:
            if not isinstance(result_item, Mapping) or not result_item.get("excerpt_id"):
                continue
            stored = SkillAccessService._verified_excerpt(
                state, str(result_item.get("excerpt_id") or "")
            )
            for field in (
                "snapshot_id", "skill_id", "operation", "content_hash", "exact_text",
                "start_line", "end_line", "resource_snapshot_id",
                "resource_content_hash", "resource_path",
            ):
                if field in result_item and result_item.get(field) != stored.get(field):
                    raise SkillAccessError(
                        "SKILL_EXCERPT_LINKAGE_INVALID",
                        f"excerpt_id={result_item.get('excerpt_id')}",
                    )
    current_excerpt_ids = {
        str(item.get("excerpt_id") or "")
        for item in (
            (current_result or {}).get("results") or []
            if isinstance(current_result, Mapping)
            else []
        )
        if isinstance(item, Mapping) and item.get("excerpt_id")
    }
    excerpts = []
    for excerpt in state["excerpts"].values():
        if not isinstance(excerpt, Mapping):
            continue
        snapshot_id = str(excerpt.get("snapshot_id") or "")
        if snapshot_id not in visible_snapshot_ids:
            continue
        excerpt = SkillAccessService._verified_excerpt(
            state, str(excerpt.get("excerpt_id") or "")
        )
        if (
            snapshot_id in allowed_candidate_snapshots
            and snapshot_id not in {
                str(item.get("snapshot_id") or "")
                for item in visible_refs
                if str(item.get("status") or "") == "active"
            }
            and str(excerpt.get("excerpt_id") or "")
            not in allowed_excerpt_ids
        ):
            continue
        if str(excerpt.get("excerpt_id") or "") in current_excerpt_ids:
            # The current tool result already carries this exact text.  Avoid
            # serialising a second copy into relevant_skill_excerpts.
            continue
        usages = [
            dict(item)
            for item in excerpt.get("usage_scopes") or []
            if isinstance(item, Mapping)
        ]
        if current_step_id and usages and not any(
            str(item.get("step_id") or "") in {"", str(current_step_id)}
            for item in usages
        ):
            continue
        excerpts.append(deepcopy(dict(excerpt)))
    excerpts.sort(key=lambda item: str(item.get("excerpt_id") or ""))
    tool_results = []
    if continuation_id:
        result = state["skill_tool_results"].get(str(continuation_id))
        continuation = state["continuations"].get(str(continuation_id)) or {}
        if (
            isinstance(result, Mapping)
            and str(continuation.get("caller_node") or "") == caller
            and str(continuation.get("status") or "") in {"served", "resumed"}
        ):
            tool_results.append(deepcopy(dict(result)))
    return {
        "selected_skill_refs": visible_refs,
        "available_skill_headings": headings,
        "relevant_skill_excerpts": excerpts,
        "skill_tool_results": tool_results,
    }


def validate_skill_resolution_decision(value: Mapping[str, Any]) -> Dict[str, Any]:
    decision = deepcopy(dict(value or {}))
    kind = str(decision.get("decision") or "").strip().upper()
    if kind not in SKILL_RESOLUTION_DECISIONS:
        raise SkillAccessError(
            "SKILL_RESOLUTION_SCHEMA_INVALID", f"decision={kind or '<missing>'}"
        )
    allowed = {
        "decision", "proposal_id", "candidate_skill_id", "relation", "reason",
        "task_fields_affected", "question", "requested_slots", "query",
    }
    unexpected = sorted(set(decision) - allowed)
    if unexpected:
        raise SkillAccessError(
            "SKILL_RESOLUTION_SCHEMA_INVALID", f"unexpected={unexpected}"
        )
    required = {"proposal_id", "candidate_skill_id", "reason"}
    required.update({
        "APPROVE_STEP_SUPPLEMENT": {"relation"},
        "ROUTE_TASK_LEVEL_CHANGE": {"relation", "task_fields_affected"},
        "REJECT_SKILL_PROPOSAL": set(),
        "NEED_USER_INPUT": {"question", "requested_slots"},
        "SEARCH_MORE": {"query"},
    }[kind])
    missing = sorted(
        field for field in required if decision.get(field) in (None, "", [])
    )
    if missing:
        raise SkillAccessError(
            "SKILL_RESOLUTION_SCHEMA_INVALID", f"missing={missing}"
        )
    relation = str(decision.get("relation") or "").strip().lower()
    if relation and relation not in SKILL_RELATIONS:
        raise SkillAccessError(
            "SKILL_RESOLUTION_SCHEMA_INVALID", f"relation={relation}"
        )
    if relation:
        decision["relation"] = relation
    decision["decision"] = kind
    return decision


def build_skill_resolution_payload(
    *,
    task_snapshot: Mapping[str, Any],
    current_plan: Mapping[str, Any],
    active_skill_refs: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    proposal: Mapping[str, Any],
    user_evidence: Sequence[Mapping[str, Any] | str] = (),
) -> Dict[str, Any]:
    """Build the restricted Consultant skill_resolution input envelope."""
    step_id = str(proposal.get("target_step_id") or proposal.get("base_step_id") or "")
    current_step = next((
        dict(item) for item in current_plan.get("steps", []) or []
        if isinstance(item, Mapping) and str(item.get("step_id") or "") == step_id
    ), {})
    return {
        "mode": "skill_resolution",
        "task_snapshot": {
            "task_revision": int(task_snapshot.get("revision") or 0),
            "user_goal": str(task_snapshot.get("goal") or ""),
            "capability_intent": deepcopy(dict(task_snapshot.get("capability_intent") or {})),
        },
        "current_plan": {
            "plan_revision": int(current_plan.get("plan_revision") or 0),
            "current_step_id": step_id,
            "current_step_objective": str(
                current_step.get("purpose") or current_step.get("title") or ""
            ),
        },
        "active_skills": [
            str(item.get("skill_id") or "")
            for item in active_skill_refs or []
            if str(item.get("status") or "active") == "active"
        ],
        "candidate": {
            "skill_id": str(candidate.get("skill_id") or ""),
            "snapshot_id": str(
                candidate.get("snapshot_id") or candidate.get("candidate_snapshot_id") or ""
            ),
            "source_path": str(candidate.get("source_path") or ""),
            "content_hash": str(candidate.get("content_hash") or ""),
            "heading_index": deepcopy(list(candidate.get("heading_index") or [])),
            "relevant_excerpt_refs": _unique(candidate.get("relevant_excerpt_refs") or []),
            "latest_skill_tool_result": deepcopy(dict(
                candidate.get("latest_skill_tool_result") or {}
            )),
        },
        "proposal": {
            "proposal_id": str(proposal.get("proposal_id") or ""),
            "relation": str(proposal.get("relation") or ""),
            "requested_scope": str(proposal.get("requested_scope") or ""),
            "justification": str(proposal.get("justification") or ""),
        },
        "user_evidence": [deepcopy(item) for item in user_evidence or []],
        "relevant_skill_excerpts": [
            deepcopy(dict(item))
            for item in candidate.get("relevant_skill_excerpts") or []
            if isinstance(item, Mapping)
        ],
    }


def review_binding_proposal(
    value: Mapping[str, Any] | None,
    proposal_id: str,
    review: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    state = normalize_skill_access_state(value)
    proposal = state["binding_proposals"].get(str(proposal_id or ""))
    if not isinstance(proposal, MutableMapping):
        raise SkillAccessError(
            "SKILL_BINDING_PROPOSAL_UNAVAILABLE", str(proposal_id or "")
        )
    decision = validate_skill_resolution_decision(review)
    if decision.get("proposal_id") and str(decision["proposal_id"]) != str(proposal_id):
        raise SkillAccessError(
            "SKILL_BINDING_PROPOSAL_MISMATCH", str(proposal_id)
        )
    if str(decision.get("candidate_skill_id") or "") != str(
        proposal.get("candidate_skill_id") or ""
    ):
        raise SkillAccessError(
            "SKILL_BINDING_PROPOSAL_MISMATCH", "candidate_skill_id"
        )
    if (
        decision["decision"] == "APPROVE_STEP_SUPPLEMENT"
        and (
            str(proposal.get("requested_scope") or "") != "step"
            or str(decision.get("relation") or "") != "supplement"
        )
    ):
        raise SkillAccessError(
            "SKILL_RESOLUTION_SCOPE_INVALID",
            "APPROVE_STEP_SUPPLEMENT requires a step supplement proposal",
        )
    if decision["decision"] == "ROUTE_TASK_LEVEL_CHANGE":
        proposal.setdefault("proposed_requested_scope", proposal.get("requested_scope"))
        proposal.setdefault("proposed_relation", proposal.get("relation"))
        proposal["requested_scope"] = "task"
        proposal["relation"] = str(decision.get("relation") or "")
    elif decision["decision"] == "APPROVE_STEP_SUPPLEMENT":
        proposal.setdefault("proposed_relation", proposal.get("relation"))
        proposal["relation"] = "supplement"
    proposal["review"] = deepcopy(decision)
    proposal["reviewed_at"] = _now()
    proposal["status"] = {
        "APPROVE_STEP_SUPPLEMENT": "approved_for_plan_impact",
        "ROUTE_TASK_LEVEL_CHANGE": "approved_for_task_commit",
        "REJECT_SKILL_PROPOSAL": "rejected",
        "NEED_USER_INPUT": "waiting_user",
        "SEARCH_MORE": "search_more",
    }[decision["decision"]]
    _record_event(state, "skill_binding_reviewed", {
        "proposal_id": str(proposal_id),
        "decision": decision["decision"],
        "status": proposal["status"],
    })
    return state, deepcopy(dict(proposal))


def activate_reviewed_binding(
    value: Mapping[str, Any] | None,
    proposal_id: str,
    *,
    task_revision: int,
) -> tuple[Dict[str, Any], list[str]]:
    """Apply an approved binding without making a semantic applicability choice."""
    state = normalize_skill_access_state(value)
    proposal = state["binding_proposals"].get(str(proposal_id or ""))
    if not isinstance(proposal, MutableMapping) or str(proposal.get("status") or "") not in {
        "approved_for_plan_impact", "approved_for_task_commit",
    }:
        raise SkillAccessError(
            "SKILL_BINDING_REVIEW_REQUIRED", str(proposal_id or "")
        )
    candidate_id = str(proposal.get("candidate_skill_id") or "")
    candidate_snapshot = str(proposal.get("candidate_snapshot_id") or "")
    candidate_ref_scope = str(proposal.get("candidate_ref_scope") or "task")
    candidate_ref_target = str(
        proposal.get("candidate_ref_target_step_id") or ""
    )
    candidate_refs = [
        ref for ref in state["selected_skill_refs"]
        if str(ref.get("skill_id") or "") == candidate_id
        and str(ref.get("snapshot_id") or "") == candidate_snapshot
        and str(ref.get("status") or "") == "candidate"
        and str(ref.get("scope") or "task") == candidate_ref_scope
        and (
            candidate_ref_scope == "task"
            or str(ref.get("target_step_id") or "") == candidate_ref_target
        )
    ]
    if len(candidate_refs) != 1:
        raise SkillAccessError(
            "SKILL_BINDING_CANDIDATE_UNAVAILABLE",
            f"candidate_refs={len(candidate_refs)}",
        )
    replacement_targets: list[MutableMapping[str, Any]] = []
    if str(proposal.get("relation") or "") == "replace":
        replacement_targets = [
            ref for ref in state["selected_skill_refs"]
            if isinstance(ref, MutableMapping)
            and str(ref.get("status") or "") == "active"
            and str(ref.get("scope") or "task")
            == str(proposal.get("requested_scope") or "task")
            and (
                str(proposal.get("requested_scope") or "task") == "task"
                or str(ref.get("target_step_id") or "")
                == str(
                    proposal.get("target_step_id")
                    or proposal.get("base_step_id")
                    or ""
                )
            )
        ]
        if len(replacement_targets) != 1:
            raise SkillAccessError(
                "SKILL_REPLACEMENT_TARGET_AMBIGUOUS",
                f"active_targets={len(replacement_targets)}",
            )
    replacement_target = replacement_targets[0] if replacement_targets else None
    active_ids: list[str] = []
    for ref in state["selected_skill_refs"]:
        is_candidate = (
            str(ref.get("skill_id") or "") == candidate_id
            and str(ref.get("snapshot_id") or "") == candidate_snapshot
            and str(ref.get("status") or "") == "candidate"
            and str(ref.get("scope") or "task") == candidate_ref_scope
            and (
                candidate_ref_scope == "task"
                or str(ref.get("target_step_id") or "") == candidate_ref_target
            )
        )
        if is_candidate:
            ref["status"] = "active"
            ref["scope"] = str(proposal.get("requested_scope") or "task")
            ref["selected_at_task_revision"] = int(task_revision or 0)
            if ref["scope"] == "step":
                ref["target_step_id"] = str(
                    proposal.get("target_step_id")
                    or proposal.get("base_step_id")
                    or ""
                )
            else:
                ref.pop("target_step_id", None)
        elif ref is replacement_target:
            ref["status"] = "superseded"
            ref["superseded_by"] = candidate_id
            ref["superseded_at_task_revision"] = int(task_revision or 0)
            ref["reason_ref"] = next(iter(proposal.get("evidence_refs") or []), "")
        if str(ref.get("status") or "") == "active":
            active_ids.append(str(ref.get("skill_id") or ""))
    proposal["status"] = "activated_pending_plan_impact"
    proposal["activated_at_task_revision"] = int(task_revision or 0)
    return state, _unique(active_ids)


def apply_confirmed_task_skill_change(
    task_spec: Mapping[str, Any],
    active_skill_ids: Sequence[Any],
    *,
    source_ref: str,
) -> Dict[str, Any]:
    """Advance TaskSpec semantics after Consultant authorises a task binding.

    The function records only generic capability identity.  It does not infer
    methods, inputs, flags or workflow rules from Skill prose.
    """
    from agent_core.task_spec import (  # Local import avoids module cycles.
        canonical_current_task_spec,
        task_semantic_hash,
        task_spec_revision,
    )

    current = canonical_current_task_spec(task_spec)
    before = task_semantic_hash(current)
    candidate = deepcopy(current)
    capability_intent = deepcopy(dict(candidate.get("capability_intent") or {}))
    capability_intent["selected_skill_ids"] = _unique(active_skill_ids)
    if source_ref:
        capability_intent["selection_source_ref"] = str(source_ref)
        candidate["source_refs"] = sorted({
            *[str(item) for item in candidate.get("source_refs") or [] if str(item)],
            str(source_ref),
        })
    candidate["capability_intent"] = capability_intent
    candidate = canonical_current_task_spec(candidate)
    candidate["revision"] = task_spec_revision(current) + (
        1 if task_semantic_hash(candidate) != before else 0
    )
    return candidate


def validate_plan_impact_decision(
    value: Mapping[str, Any],
    *,
    base_task_revision: int,
    new_task_revision: Optional[int] = None,
    base_plan_revision: int,
) -> Dict[str, Any]:
    decision = deepcopy(dict(value or {}))
    kind = str(decision.get("decision") or "").strip().upper()
    if kind not in PLAN_IMPACT_DECISIONS:
        raise SkillAccessError(
            "PLAN_IMPACT_SCHEMA_INVALID", f"decision={kind or '<missing>'}"
        )
    common_required = {
        "decision", "base_task_revision", "new_task_revision",
        "base_plan_revision", "reason",
    }
    missing_common = sorted(
        field for field in common_required
        if field not in decision or decision.get(field) in (None, "")
    )
    if missing_common:
        raise SkillAccessError(
            "PLAN_IMPACT_SCHEMA_INVALID",
            f"missing={missing_common}",
        )
    expected_new_task_revision = int(
        base_task_revision
        if new_task_revision is None
        else new_task_revision
    )
    try:
        supplied_base_task = int(decision["base_task_revision"])
        supplied_new_task = int(decision["new_task_revision"])
        supplied_plan = int(decision["base_plan_revision"])
    except (TypeError, ValueError) as exc:
        raise SkillAccessError(
            "PLAN_IMPACT_SCHEMA_INVALID", "revision fields must be integers"
        ) from exc
    if (
        supplied_base_task != int(base_task_revision)
        or supplied_new_task != expected_new_task_revision
        or supplied_plan != int(base_plan_revision)
    ):
        raise SkillAccessError(
            "PLAN_IMPACT_REVISION_MISMATCH",
            "base_task="
            f"{supplied_base_task}/{base_task_revision}; "
            f"new_task={supplied_new_task}/{expected_new_task_revision}; "
            f"plan={supplied_plan}/{base_plan_revision}",
        )
    allowed_by_kind = {
        "BIND_AND_RESUME": common_required | {"skill_refs"},
        "PATCH_CURRENT_STEP": common_required | {"replacement_step"},
        "REPLAN_SUFFIX": common_required | {"carry_forward", "supersede", "replacement_suffix"},
        "FULL_REPLAN": common_required | {"replacement_steps"},
        "NEED_USER_INPUT": common_required | {"question", "requested_slots"},
        "REJECT_PROPOSAL": common_required,
    }
    unexpected = sorted(set(decision) - allowed_by_kind[kind])
    if unexpected:
        raise SkillAccessError(
            "PLAN_IMPACT_SCHEMA_INVALID", f"unexpected={unexpected}"
        )
    required_by_kind = {
        "BIND_AND_RESUME": {"skill_refs"},
        "PATCH_CURRENT_STEP": {"replacement_step"},
        "REPLAN_SUFFIX": {"carry_forward", "supersede", "replacement_suffix"},
        "FULL_REPLAN": {"replacement_steps"},
        "NEED_USER_INPUT": {"question", "requested_slots"},
        "REJECT_PROPOSAL": set(),
    }
    missing = sorted(
        field for field in required_by_kind[kind]
        if decision.get(field) in (None, "", [])
    )
    if missing:
        raise SkillAccessError(
            "PLAN_IMPACT_SCHEMA_INVALID", f"missing={missing}"
        )
    list_fields = {
        "skill_refs", "carry_forward", "supersede", "replacement_suffix",
        "replacement_steps", "requested_slots",
    }
    for field in list_fields.intersection(decision):
        if not isinstance(decision.get(field), list):
            raise SkillAccessError(
                "PLAN_IMPACT_SCHEMA_INVALID", f"{field} must be an array"
            )
    for field in (
        "skill_refs", "carry_forward", "replacement_suffix",
        "replacement_steps",
    ):
        rows = decision.get(field)
        if rows is not None and any(
            not isinstance(item, Mapping) for item in rows
        ):
            raise SkillAccessError(
                "PLAN_IMPACT_SCHEMA_INVALID",
                f"{field} entries must be objects",
            )
    if "replacement_step" in decision and not isinstance(
        decision.get("replacement_step"), Mapping
    ):
        raise SkillAccessError(
            "PLAN_IMPACT_SCHEMA_INVALID",
            "replacement_step must be an object",
        )
    for field in ("replacement_step",):
        row = decision.get(field)
        if isinstance(row, Mapping) and not str(row.get("step_id") or ""):
            raise SkillAccessError(
                "PLAN_IMPACT_SCHEMA_INVALID",
                f"{field}.step_id is required",
            )
    for field in ("replacement_suffix", "replacement_steps"):
        rows = decision.get(field) or []
        if any(not str(item.get("step_id") or "") for item in rows):
            raise SkillAccessError(
                "PLAN_IMPACT_SCHEMA_INVALID",
                f"{field} entries require step_id",
            )
    if "carry_forward" in decision:
        for row in decision.get("carry_forward") or []:
            if (
                not str(row.get("step_id") or "")
                or not isinstance(row.get("evidence_refs"), list)
                or not _unique(row.get("evidence_refs") or [])
            ):
                raise SkillAccessError(
                    "PLAN_IMPACT_SCHEMA_INVALID",
                    "carry_forward entries require step_id and evidence_refs",
                )
    if "supersede" in decision:
        supersede = decision.get("supersede") or []
        if (
            any(not isinstance(item, str) or not item for item in supersede)
            or len(_unique(supersede)) != len(supersede)
        ):
            raise SkillAccessError(
                "PLAN_IMPACT_SCHEMA_INVALID",
                "supersede entries must be unique non-empty strings",
            )
    if "requested_slots" in decision and any(
        not isinstance(item, str) or not item
        for item in decision.get("requested_slots") or []
    ):
        raise SkillAccessError(
            "PLAN_IMPACT_SCHEMA_INVALID",
            "requested_slots entries must be non-empty strings",
        )
    decision["decision"] = kind
    decision["base_task_revision"] = supplied_base_task
    decision["new_task_revision"] = supplied_new_task
    decision["base_plan_revision"] = supplied_plan
    return decision


def validate_plan_impact_proposal_compatibility(
    decision: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    current_step_id: str,
) -> Dict[str, Any]:
    """Validate structural proposal/impact compatibility without semantics."""
    reviewed = deepcopy(dict(decision or {}))
    proposal_row = deepcopy(dict(proposal or {}))
    kind = str(reviewed.get("decision") or "").strip().upper()
    scope = str(proposal_row.get("requested_scope") or "").strip().lower()
    if scope == "current_step":
        scope = "step"
    relation = str(proposal_row.get("relation") or "").strip().lower()
    if scope not in SKILL_SCOPES or relation not in SKILL_RELATIONS:
        raise SkillAccessError(
            "PLAN_IMPACT_PROPOSAL_SHAPE_INVALID",
            f"scope={scope or '<missing>'}; relation={relation or '<missing>'}",
        )
    target_step_id = str(
        proposal_row.get("target_step_id")
        or proposal_row.get("base_step_id")
        or ""
    )
    if scope == "step" and (
        not target_step_id
        or target_step_id != str(current_step_id or "")
    ):
        raise SkillAccessError(
            "PLAN_IMPACT_PROPOSAL_STEP_MISMATCH",
            f"proposal={target_step_id or '<missing>'}; current={current_step_id or '<missing>'}",
        )
    if kind == "BIND_AND_RESUME" and not (
        scope == "step" and relation == "supplement"
    ):
        raise SkillAccessError(
            "PLAN_IMPACT_BIND_SCOPE_INVALID",
            f"scope={scope}; relation={relation}",
        )
    return reviewed


def _plan_step_map(plan: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for raw in plan.get("steps") or []:
        if not isinstance(raw, Mapping):
            raise SkillAccessError("PLAN_IMPACT_STRUCTURE_INVALID", "step must be an object")
        step = deepcopy(dict(raw))
        step_id = str(step.get("step_id") or "")
        if not step_id or step_id in result:
            raise SkillAccessError(
                "PLAN_IMPACT_STRUCTURE_INVALID", f"duplicate/missing step_id={step_id!r}"
            )
        result[step_id] = step
    return result


def _step_skill_snapshot_ids(step: Mapping[str, Any]) -> list[str]:
    return _unique(
        item.get("snapshot_id")
        for item in step.get("skill_refs") or []
        if isinstance(item, Mapping)
    )


def apply_plan_impact_review(
    plan_state: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    base_task_revision: Optional[int] = None,
    new_task_revision: int,
    valid_skill_snapshot_ids: Iterable[str],
    valid_skill_refs: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Deterministically apply one Planner-owned plan-impact decision."""
    previous = deepcopy(dict(plan_state or {}))
    base_plan_revision = int(previous.get("plan_revision") or 0)
    reviewed = validate_plan_impact_decision(
        decision,
        base_task_revision=int(
            new_task_revision
            if base_task_revision is None
            else base_task_revision
        ),
        new_task_revision=int(new_task_revision),
        base_plan_revision=base_plan_revision,
    )
    kind = reviewed["decision"]
    if kind in {"NEED_USER_INPUT", "REJECT_PROPOSAL"}:
        return {
            **previous,
            "plan_impact_review": deepcopy(reviewed),
            "plan_impact_status": kind.lower(),
        }
    steps = _plan_step_map(previous)
    current_step_id = str(previous.get("current_step_id") or "")
    if current_step_id not in steps:
        current_step_id = next((
            step_id for step_id, step in steps.items()
            if str(step.get("status") or "") in {
                "running", "ready", "pending", "repairing", "retry_pending",
            }
        ), "")
    valid_snapshots = {str(item) for item in valid_skill_snapshot_ids or [] if str(item)}
    canonical_skill_refs = [
        deepcopy(dict(item))
        for item in valid_skill_refs or []
        if isinstance(item, Mapping)
        and str(item.get("status") or "active") == "active"
    ]

    def check_snapshots(candidate_steps: Sequence[Mapping[str, Any]]) -> None:
        invalid = sorted({
            snapshot_id
            for step in candidate_steps
            for snapshot_id in _step_skill_snapshot_ids(step)
            if snapshot_id not in valid_snapshots
        })
        if invalid:
            raise SkillAccessError(
                "PLAN_IMPACT_SKILL_SNAPSHOT_INVALID", ", ".join(invalid)
            )
        if not canonical_skill_refs:
            return
        for step in candidate_steps:
            step_id = str(step.get("step_id") or "")
            raw_skill_refs = step.get("skill_refs") or []
            ref_skill_ids = _unique(
                item.get("skill_id")
                for item in raw_skill_refs
                if isinstance(item, Mapping)
            )
            if "skill_refs" in step and (
                _unique(step.get("skill_ids") or []) != ref_skill_ids
            ):
                raise SkillAccessError(
                    "PLAN_IMPACT_SKILL_REFERENCE_INVALID",
                    f"step_id={step_id}; skill_ids do not match skill_refs",
                )
            for raw_ref in raw_skill_refs:
                if not isinstance(raw_ref, Mapping):
                    raise SkillAccessError(
                        "PLAN_IMPACT_SKILL_REFERENCE_INVALID", step_id
                    )
                linked = any(
                    all(
                        str(raw_ref.get(field) or "")
                        == str(canonical.get(field) or "")
                        for field in (
                            "skill_id", "snapshot_id", "source_path",
                            "content_hash", "scope", "status",
                            "target_step_id",
                        )
                    )
                    and (
                        str(canonical.get("scope") or "task") == "task"
                        or str(canonical.get("target_step_id") or "")
                        == step_id
                    )
                    for canonical in canonical_skill_refs
                )
                if not linked:
                    raise SkillAccessError(
                        "PLAN_IMPACT_SKILL_REFERENCE_INVALID",
                        f"step_id={step_id}; snapshot_id={raw_ref.get('snapshot_id')}",
                    )

    def fresh_replacement(step: Mapping[str, Any]) -> Dict[str, Any]:
        replacement = deepcopy(dict(step))
        replacement["status"] = "pending"
        for field in (
            "execution_evidence", "validation_evidence",
            "completion_evidence", "evidence_refs", "attempts", "result",
            "started_at", "completed_at", "finished_at", "passed_at",
        ):
            replacement.pop(field, None)
        return replacement

    def durable_step_evidence_refs(step: Mapping[str, Any]) -> set[str]:
        refs: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    if str(key) in {
                        "evidence_id", "evidence_ref", "artifact_id",
                        "artifact_ref", "path", "sha256",
                    } and str(nested):
                        refs.add(str(nested))
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)
            elif isinstance(value, str) and value:
                refs.add(value)

        for field in (
            "evidence_refs", "execution_evidence", "validation_evidence",
            "completion_evidence", "artifacts", "artifact_refs",
        ):
            collect(step.get(field))
        return refs

    superseded_history = [
        deepcopy(dict(item))
        for item in previous.get("superseded_steps") or []
        if isinstance(item, Mapping)
    ]
    if kind == "BIND_AND_RESUME":
        if current_step_id not in steps:
            raise SkillAccessError("PLAN_IMPACT_CURRENT_STEP_INVALID", current_step_id)
        old_step = deepcopy(steps[current_step_id])
        skill_refs = [
            deepcopy(dict(item))
            for item in reviewed.get("skill_refs") or []
            if isinstance(item, Mapping)
        ]
        if len(skill_refs) != len(reviewed.get("skill_refs") or []):
            raise SkillAccessError(
                "PLAN_IMPACT_SKILL_REFERENCE_INVALID",
                current_step_id,
            )
        identity_fields = (
            "skill_id", "snapshot_id", "source_path", "content_hash",
            "scope", "target_step_id",
        )
        old_ref_keys = {
            tuple(str(item.get(field) or "") for field in identity_fields)
            for item in old_step.get("skill_refs") or []
            if isinstance(item, Mapping)
        }
        new_ref_keys = {
            tuple(str(item.get(field) or "") for field in identity_fields)
            for item in skill_refs
        }
        old_skill_ids = set(_unique(old_step.get("skill_ids") or []))
        new_skill_ids = set(_unique(
            item.get("skill_id") for item in skill_refs
        ))
        if not old_ref_keys.issubset(new_ref_keys) or not old_skill_ids.issubset(
            new_skill_ids
        ):
            raise SkillAccessError(
                "PLAN_IMPACT_BIND_NOT_ADDITIVE",
                current_step_id,
            )
        check_snapshots([{
            **steps[current_step_id],
            "skill_refs": skill_refs,
            "skill_ids": _unique(
                item.get("skill_id") for item in skill_refs
            ),
        }])
        steps[current_step_id]["skill_refs"] = skill_refs
        steps[current_step_id]["skill_ids"] = _unique(
            item.get("skill_id") for item in skill_refs
        )
        next_steps = list(steps.values())
    elif kind == "PATCH_CURRENT_STEP":
        if current_step_id not in steps:
            raise SkillAccessError("PLAN_IMPACT_CURRENT_STEP_INVALID", current_step_id)
        old = deepcopy(steps[current_step_id])
        if str(old.get("status") or "") in {"done", "passed", "completed", "skipped"}:
            raise SkillAccessError(
                "PLAN_IMPACT_PASSED_STEP_PATCH_FORBIDDEN", current_step_id
            )
        replacement = deepcopy(dict(reviewed.get("replacement_step") or {}))
        replacement.setdefault("step_id", current_step_id)
        if str(replacement.get("step_id") or "") != current_step_id:
            raise SkillAccessError(
                "PLAN_IMPACT_CURRENT_STEP_ID_CHANGED", current_step_id
            )
        for field in ("inputs", "outputs", "success_criteria"):
            if list(replacement.get(field) or []) != list(old.get(field) or []):
                raise SkillAccessError(
                    "PLAN_IMPACT_PATCH_CONTRACT_CHANGED", field
                )
        old["status"] = "superseded"
        old["superseded_at_plan_revision"] = base_plan_revision + 1
        superseded_history.append(old)
        replacement["step_revision"] = int(old.get("step_revision") or 1) + 1
        replacement["status"] = "pending"
        check_snapshots([replacement])
        next_steps = [
            replacement if step_id == current_step_id else step
            for step_id, step in steps.items()
        ]
    elif kind == "REPLAN_SUFFIX":
        carry_rows = [
            deepcopy(dict(item))
            for item in reviewed.get("carry_forward") or []
            if isinstance(item, Mapping)
        ]
        carry_ids = _unique(item.get("step_id") for item in carry_rows)
        supersede_ids = _unique(reviewed.get("supersede") or [])
        ordered_step_ids = list(steps)
        if carry_ids != ordered_step_ids[:len(carry_ids)]:
            raise SkillAccessError(
                "PLAN_IMPACT_CARRY_FORWARD_NOT_CONTIGUOUS_PREFIX",
                f"carry={carry_ids}; order={ordered_step_ids}",
            )
        if set(carry_ids).intersection(supersede_ids):
            raise SkillAccessError(
                "PLAN_IMPACT_STEP_CLASSIFICATION_CONFLICT",
                ", ".join(sorted(set(carry_ids).intersection(supersede_ids))),
            )
        if set(carry_ids).union(supersede_ids) != set(steps):
            raise SkillAccessError(
                "PLAN_IMPACT_STEP_CLASSIFICATION_INCOMPLETE",
                f"classified={sorted(set(carry_ids).union(supersede_ids))}; existing={sorted(steps)}",
            )
        for row in carry_rows:
            step_id = str(row.get("step_id") or "")
            status = str((steps.get(step_id) or {}).get("status") or "")
            if status not in {"done", "passed", "completed", "skipped"}:
                raise SkillAccessError(
                    "PLAN_IMPACT_CARRY_FORWARD_NOT_PASSED", step_id
                )
            if not _unique(row.get("evidence_refs") or []):
                raise SkillAccessError(
                    "PLAN_IMPACT_CARRY_FORWARD_EVIDENCE_MISSING", step_id
                )
            unknown_evidence = sorted(
                set(_unique(row.get("evidence_refs") or []))
                - durable_step_evidence_refs(steps.get(step_id) or {})
            )
            if unknown_evidence:
                raise SkillAccessError(
                    "PLAN_IMPACT_CARRY_FORWARD_EVIDENCE_INVALID",
                    f"step_id={step_id}; refs={unknown_evidence}",
                )
        replacements = [
            fresh_replacement(item)
            for item in reviewed.get("replacement_suffix") or []
            if isinstance(item, Mapping)
        ]
        replacement_ids = _unique(item.get("step_id") for item in replacements)
        if len(replacement_ids) != len(replacements) or set(replacement_ids).intersection(steps):
            raise SkillAccessError(
                "PLAN_IMPACT_REPLACEMENT_STEP_ID_INVALID", ", ".join(replacement_ids)
            )
        check_snapshots(replacements)
        for step_id in supersede_ids:
            old = deepcopy(steps[step_id])
            old["status"] = "superseded"
            old["superseded_at_plan_revision"] = base_plan_revision + 1
            superseded_history.append(old)
        next_steps = [deepcopy(steps[step_id]) for step_id in carry_ids] + replacements
    else:  # FULL_REPLAN
        replacements = [
            fresh_replacement(item)
            for item in reviewed.get("replacement_steps") or []
            if isinstance(item, Mapping)
        ]
        replacement_map = _plan_step_map({"steps": replacements})
        if not replacement_map:
            raise SkillAccessError(
                "PLAN_IMPACT_STRUCTURE_INVALID", "FULL_REPLAN replacement_steps is empty"
            )
        check_snapshots(replacements)
        for old in steps.values():
            historical = deepcopy(old)
            historical["status"] = "superseded"
            historical["superseded_at_plan_revision"] = base_plan_revision + 1
            superseded_history.append(historical)
        next_steps = replacements

    result = deepcopy(previous)
    result["steps"] = next_steps
    result["superseded_steps"] = superseded_history
    result["parent_plan_version"] = str(
        previous.get("plan_version") or f"plan_v{base_plan_revision}"
    )
    result["plan_revision"] = base_plan_revision + 1
    result["plan_version"] = f"plan_v{result['plan_revision']}"
    result["current_effective_plan_version"] = result["plan_version"]
    result["generated_from_task_revision"] = int(new_task_revision)
    result["plan_status"] = "pending_review"
    result["approval_status"] = "awaiting_review"
    result["approved_plan_hash"] = ""
    result["approved_plan_revision"] = 0
    result["approved_task_revision"] = 0
    result["approval_record"] = {}
    result["approval_projection"] = {}
    result["approval_scope"] = {}
    result["can_generate"] = False
    result["current_step_id"] = next((
        str(step.get("step_id") or "")
        for step in next_steps
        if str(step.get("status") or "pending") not in {
            "done", "passed", "completed", "skipped", "superseded",
        }
    ), "")
    result["plan_impact_review"] = deepcopy(reviewed)
    result["plan_impact_status"] = "reviewed"
    return result


def stale_node_result(
    *,
    request_task_revision: int,
    request_plan_revision: int,
    current_task_revision: int,
    current_plan_revision: int,
    caller_node: str,
    caller_invocation_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the typed discard record for a late node result, if stale."""
    changed = []
    if int(request_task_revision) != int(current_task_revision):
        changed.append("TASK_REVISION_MISMATCH")
    if int(request_plan_revision) != int(current_plan_revision):
        changed.append("PLAN_REVISION_MISMATCH")
    if not changed:
        return None
    return {
        "decision_result": "STALE_NODE_RESULT",
        "status": "discarded",
        "caller_node": str(caller_node or ""),
        "caller_invocation_id": str(caller_invocation_id or ""),
        "changed_preconditions": changed,
        "counts_as_failure": False,
        "consumes_execution_retry": False,
        "execute": False,
    }

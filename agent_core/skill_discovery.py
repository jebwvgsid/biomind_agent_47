"""Deterministic state and budgets for Consultant-owned skill discovery.

The Controller records discovery evidence and bounds retries.  It deliberately
does not decide which scientific capability should be selected; that remains a
Consultant decision made from candidate cards and loaded skill documents.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, Mapping, Sequence

from agent_core.task_spec import canonical_input_role


DISCOVERY_SCHEMA_VERSION = 1

UNRESOLVED = "UNRESOLVED"
SEARCHING = "SEARCHING"
CANDIDATES_FOUND = "CANDIDATES_FOUND"
READING_CANDIDATES = "READING_CANDIDATES"
SELECTED = "SELECTED"
EXPANDING_DISCOVERY = "EXPANDING_DISCOVERY"
REASONING_ESCALATION = "REASONING_ESCALATION"
NEED_USER_CLARIFICATION = "NEED_USER_CLARIFICATION"
CAPABILITY_GAP = "CAPABILITY_GAP"

DEFAULT_DISCOVERY_BUDGETS = {
    "normal_searches": 2,
    "catalog_expansions": 1,
    "loaded_skill_docs": 5,
    "reasoning_escalations": 1,
}


def _unique(values: Sequence[Any]) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip() for value in values or [] if str(value).strip()
    ))


def new_skill_discovery_state(
    *,
    budgets: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    limits = dict(DEFAULT_DISCOVERY_BUDGETS)
    for key, value in dict(budgets or {}).items():
        if key in limits:
            limits[key] = max(1, int(value))
    return {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "routing_status": UNRESOLVED,
        "reasoning_level": "normal",
        "task_hypothesis": "",
        "input_roles": [],
        "search_queries_attempted": [],
        "search_history": [],
        "candidate_skill_ids": [],
        "candidate_evidence": {},
        "loaded_skill_ids": [],
        "loaded_skill_docs": {},
        "rejected_candidates": [],
        "normal_search_count": 0,
        "catalog_expansion_count": 0,
        "reasoning_escalation_count": 0,
        "premature_finish_count": 0,
        "capability_gap": False,
        "capability_gap_reason": "",
        "budgets": limits,
    }


def normalize_skill_discovery_state(
    state: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    normalized = new_skill_discovery_state(
        budgets=dict((state or {}).get("budgets") or {})
    )
    normalized.update(deepcopy(dict(state or {})))
    normalized["schema_version"] = DISCOVERY_SCHEMA_VERSION
    normalized["search_queries_attempted"] = _unique(
        normalized.get("search_queries_attempted") or []
    )
    normalized["candidate_skill_ids"] = _unique(
        normalized.get("candidate_skill_ids") or []
    )
    normalized["loaded_skill_ids"] = _unique(
        normalized.get("loaded_skill_ids") or []
    )
    normalized["input_roles"] = _unique(normalized.get("input_roles") or [])
    normalized["search_history"] = [
        deepcopy(dict(item)) for item in normalized.get("search_history") or []
        if isinstance(item, Mapping)
    ][-12:]
    normalized["rejected_candidates"] = [
        deepcopy(dict(item)) for item in normalized.get("rejected_candidates") or []
        if isinstance(item, Mapping)
    ][-12:]
    return normalized


def discovery_input_roles(
    *,
    input_audit: Mapping[str, Any] | None = None,
    task_spec: Mapping[str, Any] | None = None,
) -> list[str]:
    """Project verified/bound inputs into generic ranking evidence."""
    roles: list[str] = []
    for item in list(dict(input_audit or {}).get("resolved_inputs") or []):
        if not isinstance(item, Mapping):
            continue
        path = item.get("path") or item.get("resolved_path") or ""
        roles.append(canonical_input_role(str(item.get("role") or ""), str(path)))
    for item in list(dict(task_spec or {}).get("inputs") or []):
        if not isinstance(item, Mapping):
            continue
        path = item.get("path") or item.get("resolved_path") or ""
        roles.append(canonical_input_role(str(item.get("role") or ""), str(path)))
    return _unique(role for role in roles if role and role != "input_file")


def _candidate_projection(cards: Sequence[Mapping[str, Any]]) -> tuple[list[str], Dict[str, Any]]:
    ids: list[str] = []
    evidence: Dict[str, Any] = {}
    for raw in cards or []:
        card = dict(raw or {})
        skill_id = str(card.get("skill_id") or "").strip()
        if not skill_id:
            continue
        ids.append(skill_id)
        evidence[skill_id] = {
            "score": int(card.get("score") or 0),
            "reasons": _unique(card.get("reasons") or []),
            "category": str(card.get("category") or ""),
            "summary": str(card.get("summary") or "")[:600],
        }
    return _unique(ids), evidence


def record_skill_search(
    state: Mapping[str, Any] | None,
    *,
    query: str,
    candidate_cards: Sequence[Mapping[str, Any]],
    input_roles: Sequence[str] = (),
    task_hypothesis: str = "",
    source: str = "semantic_query",
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    query_text = str(query or "").strip()
    prior_queries = list(updated.get("search_queries_attempted") or [])
    is_new_query = bool(query_text and query_text not in prior_queries)
    if is_new_query:
        prior_queries.append(query_text)
        updated["normal_search_count"] = int(updated.get("normal_search_count") or 0) + 1
    candidate_ids, evidence = _candidate_projection(candidate_cards)
    updated.update({
        "search_queries_attempted": prior_queries,
        "candidate_skill_ids": candidate_ids,
        "candidate_evidence": evidence,
        "input_roles": _unique(input_roles),
        "task_hypothesis": str(task_hypothesis or updated.get("task_hypothesis") or "").strip(),
        "capability_gap": False,
        "capability_gap_reason": "",
    })
    updated["search_history"] = [
        *list(updated.get("search_history") or []),
        {
            "source": str(source or "semantic_query"),
            "query": query_text,
            "input_roles": _unique(input_roles),
            "candidate_ids": candidate_ids,
            "candidate_evidence": evidence,
        },
    ][-12:]
    if candidate_ids:
        updated["routing_status"] = CANDIDATES_FOUND
    elif int(updated.get("normal_search_count") or 0) < int(
        updated["budgets"]["normal_searches"]
    ):
        updated["routing_status"] = SEARCHING
    else:
        updated["routing_status"] = EXPANDING_DISCOVERY
    return updated


def record_catalog_expansion(
    state: Mapping[str, Any] | None,
    *,
    category: str,
    candidate_cards: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    category_text = str(category or "all").strip() or "all"
    seen_categories = _unique(updated.get("expanded_categories") or [])
    if category_text not in seen_categories:
        seen_categories.append(category_text)
        updated["catalog_expansion_count"] = int(
            updated.get("catalog_expansion_count") or 0
        ) + 1
    candidate_ids, evidence = _candidate_projection(candidate_cards)
    updated["expanded_categories"] = seen_categories
    updated["search_history"] = [
        *list(updated.get("search_history") or []),
        {
            "source": "catalog_expansion",
            "category": category_text,
            "candidate_ids": candidate_ids,
            "candidate_evidence": evidence,
        },
    ][-12:]
    if candidate_ids:
        updated["candidate_skill_ids"] = candidate_ids
        updated["candidate_evidence"] = evidence
        updated["routing_status"] = CANDIDATES_FOUND
    else:
        updated["routing_status"] = EXPANDING_DISCOVERY
    return updated


def record_loaded_skill_doc(
    state: Mapping[str, Any] | None,
    *,
    skill_id: str,
    path: str = "",
    document: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    skill = str(skill_id or "").strip()
    if not skill:
        return updated
    loaded = _unique([*updated.get("loaded_skill_ids", []), skill])
    docs = deepcopy(dict(updated.get("loaded_skill_docs") or {}))
    docs[skill] = {
        "path": str(path or ""),
        "sha256": hashlib.sha256(str(document or "").encode("utf-8")).hexdigest(),
        "summary": str(summary or "")[:600],
    }
    updated.update({
        "loaded_skill_ids": loaded,
        "loaded_skill_docs": docs,
        "routing_status": READING_CANDIDATES,
    })
    return updated


def record_rejected_candidate(
    state: Mapping[str, Any] | None,
    *,
    skill_id: str,
    reason: str,
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    skill = str(skill_id or "").strip()
    if not skill:
        return updated
    rows = [
        item for item in updated.get("rejected_candidates") or []
        if str(item.get("skill_id") or "") != skill
    ]
    rows.append({"skill_id": skill, "reason": str(reason or "")[:800]})
    updated["rejected_candidates"] = rows[-12:]
    remaining = [
        item for item in updated.get("candidate_skill_ids") or []
        if item not in {row["skill_id"] for row in rows}
    ]
    updated["routing_status"] = CANDIDATES_FOUND if remaining else EXPANDING_DISCOVERY
    return updated


def record_selected_skills(
    state: Mapping[str, Any] | None,
    selected_skill_ids: Sequence[str],
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    selected = _unique(selected_skill_ids)
    updated["selected_skill_ids"] = selected
    if selected:
        updated["routing_status"] = SELECTED
        updated["capability_gap"] = False
        updated["capability_gap_reason"] = ""
    return updated


def normal_discovery_exhausted(state: Mapping[str, Any] | None) -> bool:
    current = normalize_skill_discovery_state(state)
    limits = current["budgets"]
    return bool(
        int(current.get("normal_search_count") or 0) >= int(limits["normal_searches"])
        and int(current.get("catalog_expansion_count") or 0) >= int(limits["catalog_expansions"])
    )


def record_reasoning_escalation(
    state: Mapping[str, Any] | None,
    *,
    trigger: str,
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    if not normal_discovery_exhausted(updated):
        raise ValueError("normal skill discovery must be exhausted before reasoning escalation")
    if int(updated.get("reasoning_escalation_count") or 0) >= int(
        updated["budgets"]["reasoning_escalations"]
    ):
        raise ValueError("reasoning escalation budget is exhausted")
    updated.update({
        "routing_status": REASONING_ESCALATION,
        "reasoning_level": "escalated",
        "reasoning_escalation_count": int(updated.get("reasoning_escalation_count") or 0) + 1,
        "reasoning_escalation_trigger": str(trigger or "normal_discovery_exhausted")[:800],
    })
    return updated


def capability_gap_allowed(state: Mapping[str, Any] | None) -> bool:
    current = normalize_skill_discovery_state(state)
    viable = [
        skill_id for skill_id in current.get("candidate_skill_ids") or []
        if skill_id not in {
            str(item.get("skill_id") or "")
            for item in current.get("rejected_candidates") or []
        }
    ]
    return bool(
        normal_discovery_exhausted(current)
        and current.get("reasoning_level") == "escalated"
        and int(current.get("reasoning_escalation_count") or 0) > 0
        and not viable
    )


def record_capability_gap(
    state: Mapping[str, Any] | None,
    *,
    reason: str,
) -> Dict[str, Any]:
    if not capability_gap_allowed(state):
        raise ValueError("capability gap is not allowed before discovery is exhausted")
    updated = normalize_skill_discovery_state(state)
    updated.update({
        "routing_status": CAPABILITY_GAP,
        "capability_gap": True,
        "capability_gap_reason": str(reason or "No compatible documented skill was found.")[:1600],
    })
    return updated


def discovery_next_actions(state: Mapping[str, Any] | None) -> list[str]:
    current = normalize_skill_discovery_state(state)
    candidates = list(current.get("candidate_skill_ids") or [])
    loaded = set(current.get("loaded_skill_ids") or [])
    rejected = {
        str(item.get("skill_id") or "")
        for item in current.get("rejected_candidates") or []
    }
    viable = [item for item in candidates if item not in rejected]
    if current.get("routing_status") == SELECTED:
        return ["FINISH"]
    if viable:
        unloaded = [item for item in viable if item not in loaded]
        loaded_viable = [item for item in viable if item in loaded]
        actions: list[str] = []
        if loaded_viable:
            actions.extend(["SELECT_SKILL", "REJECT_SKILL"])
        if unloaded and len(loaded) < int(current["budgets"]["loaded_skill_docs"]):
            actions.append("LOAD_SKILL_DOC")
        return actions or ["REASONING_ESCALATION"]
    limits = current["budgets"]
    if int(current.get("normal_search_count") or 0) < int(limits["normal_searches"]):
        return ["SEARCH_SKILLS"]
    if int(current.get("catalog_expansion_count") or 0) < int(limits["catalog_expansions"]):
        return ["LIST_SKILL_CATEGORIES", "LIST_SKILLS"]
    if current.get("reasoning_level") != "escalated":
        return ["REASONING_ESCALATION"]
    return ["SEARCH_SKILLS", "LOAD_SKILL_DOC", "ASK_USER", "CAPABILITY_GAP"]


def reject_premature_finish(
    state: Mapping[str, Any] | None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    updated = normalize_skill_discovery_state(state)
    updated["premature_finish_count"] = int(updated.get("premature_finish_count") or 0) + 1
    actions = discovery_next_actions(updated)
    if actions == ["SEARCH_SKILLS"]:
        updated["routing_status"] = SEARCHING
    elif "LIST_SKILLS" in actions:
        updated["routing_status"] = EXPANDING_DISCOVERY
    elif actions == ["REASONING_ESCALATION"]:
        updated["routing_status"] = REASONING_ESCALATION
    guidance = {
        "finish_rejected": True,
        "unresolved": ["skill-routing"],
        "owner": "consultant_internal",
        "reason": "skill discovery not exhausted",
        "allowed_or_required_next_actions": actions,
    }
    updated["recovery_directive"] = {
        "code": "DISCOVERY_REQUIRED",
        "allowed_or_required_next_actions": list(actions),
    }
    return updated, guidance


def skill_discovery_progress_projection(
    state: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Return only substantive evidence; counters for rejected FINISH are noise."""
    current = normalize_skill_discovery_state(state)
    return {
        "routing_status": current.get("routing_status"),
        "reasoning_level": current.get("reasoning_level"),
        "task_hypothesis": current.get("task_hypothesis"),
        "input_roles": list(current.get("input_roles") or []),
        "search_queries_attempted": list(current.get("search_queries_attempted") or []),
        "search_history": deepcopy(list(current.get("search_history") or [])),
        "candidate_skill_ids": list(current.get("candidate_skill_ids") or []),
        "candidate_evidence": deepcopy(dict(current.get("candidate_evidence") or {})),
        "loaded_skill_ids": list(current.get("loaded_skill_ids") or []),
        "loaded_skill_docs": deepcopy(dict(current.get("loaded_skill_docs") or {})),
        "rejected_candidates": deepcopy(list(current.get("rejected_candidates") or [])),
        "normal_search_count": int(current.get("normal_search_count") or 0),
        "catalog_expansion_count": int(current.get("catalog_expansion_count") or 0),
        "reasoning_escalation_count": int(current.get("reasoning_escalation_count") or 0),
        "capability_gap": bool(current.get("capability_gap")),
        "recovery_directive": deepcopy(dict(current.get("recovery_directive") or {})),
    }


def skill_discovery_progress_digest(state: Mapping[str, Any] | None) -> str:
    projection = skill_discovery_progress_projection(state)
    return hashlib.sha256(
        json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_discovery_escalation_context(
    *,
    raw_user_request: str,
    task_spec: Mapping[str, Any] | None,
    input_inventory: Mapping[str, Any] | None,
    discovery_state: Mapping[str, Any] | None,
    candidate_cards: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Build the narrow payload for a thinking-only discovery invocation."""
    current = normalize_skill_discovery_state(discovery_state)
    spec = dict(task_spec or {})
    inventory = dict(input_inventory or {})
    relevant_spec = {
        key: deepcopy(spec.get(key))
        for key in (
            "goal", "inputs", "requirements", "confirmed_decisions",
            "unresolved_questions", "output_contracts",
        )
        if spec.get(key) not in (None, "", [], {})
    }
    compact_inventory = {
        "status": inventory.get("status"),
        "resolved_inputs": [
            {
                "role": item.get("role"),
                "path": item.get("path") or item.get("resolved_path"),
                "verified": item.get("verified"),
            }
            for item in inventory.get("resolved_inputs") or []
            if isinstance(item, Mapping)
        ][:24],
        "missing_roles": list(inventory.get("missing_roles") or []),
        "ambiguous_roles": list(inventory.get("ambiguous_roles") or []),
    }
    return {
        "context_type": "ConsultantDiscoveryEscalationContext",
        "schema_version": 1,
        "raw_user_request": str(raw_user_request or ""),
        "task_hypothesis": str(current.get("task_hypothesis") or ""),
        "current_task_spec": relevant_spec,
        "input_inventory": compact_inventory,
        "input_roles": list(current.get("input_roles") or []),
        "skill_routing_status": current.get("routing_status"),
        "search_queries_attempted": list(current.get("search_queries_attempted") or []),
        "search_history": [
            {
                key: deepcopy(item.get(key))
                for key in ("source", "query", "category", "candidate_ids")
                if item.get(key) not in (None, "", [], {})
            }
            for item in list(current.get("search_history") or [])[-8:]
            if isinstance(item, Mapping)
        ],
        "candidate_skills": [
            deepcopy(dict(item)) for item in candidate_cards or []
        ][:12],
        "loaded_skill_docs": deepcopy(dict(current.get("loaded_skill_docs") or {})),
        "rejected_candidates": deepcopy(list(current.get("rejected_candidates") or [])),
        "current_unresolved_items": ["skill-routing"],
        "allowed_next_actions": discovery_next_actions(current),
    }

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


DISCOVERY_SCHEMA_VERSION = 2

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
    "loaded_skill_docs": 8,
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
        "observed_skill_mentions": [],
        "input_roles": [],
        "search_queries_attempted": [],
        "search_history": [],
        "catalog_digest": "",
        "last_search_fingerprint": "",
        "repeated_search_count": 0,
        "candidate_skill_ids": [],
        "candidate_evidence": {},
        "candidate_skill_refs": [],
        "loaded_skill_ids": [],
        "loaded_skill_docs": {},
        "loaded_skill_refs": [],
        "pending_document_injection_ids": [],
        "injected_document_digests": {},
        "selected_skill_refs": [],
        "excluded_skill_refs": [],
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
    normalized["pending_document_injection_ids"] = _unique(
        normalized.get("pending_document_injection_ids") or []
    )
    normalized["injected_document_digests"] = {
        str(key): str(value)
        for key, value in dict(
            normalized.get("injected_document_digests") or {}
        ).items()
        if str(key) and str(value)
    }
    normalized["input_roles"] = _unique(normalized.get("input_roles") or [])
    normalized["observed_skill_mentions"] = _unique(
        normalized.get("observed_skill_mentions") or []
    )
    normalized["search_history"] = [
        deepcopy(dict(item)) for item in normalized.get("search_history") or []
        if isinstance(item, Mapping)
    ][-12:]
    normalized["rejected_candidates"] = [
        deepcopy(dict(item)) for item in normalized.get("rejected_candidates") or []
        if isinstance(item, Mapping)
    ][-12:]
    for field in (
        "candidate_skill_refs", "loaded_skill_refs",
        "selected_skill_refs", "excluded_skill_refs",
    ):
        normalized[field] = [
            deepcopy(dict(item))
            for item in normalized.get(field) or []
            if isinstance(item, Mapping)
        ][-12:]
    # Read-old compatibility: the legacy selected ID projection remains
    # derived from typed Consultant decisions.
    if normalized.get("selected_skill_refs"):
        normalized["selected_skill_ids"] = _unique(
            item.get("skill_id") for item in normalized["selected_skill_refs"]
        )
    else:
        normalized["selected_skill_ids"] = _unique(
            normalized.get("selected_skill_ids") or []
        )
    return normalized


def record_observed_skill_mentions(
    state: Mapping[str, Any] | None,
    skill_ids: Sequence[str],
) -> Dict[str, Any]:
    """Persist literal prompt mentions as evidence, never as selection."""
    updated = normalize_skill_discovery_state(state)
    updated["observed_skill_mentions"] = _unique([
        *updated.get("observed_skill_mentions", []),
        *skill_ids,
    ])
    return updated


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
            "score": int(card.get("deterministic_score") or card.get("score") or 0),
            "reasons": _unique(card.get("reasons") or []),
            "category": str(card.get("category") or ""),
            "summary": str(card.get("summary") or "")[:600],
            "matched_metadata_fields": _unique(
                card.get("matched_metadata_fields") or []
            ),
            "document_digest": str(card.get("document_digest") or ""),
            "declares_executable_runtime": bool(
                card.get("declares_executable_runtime")
            ),
            "source_query_id": str(card.get("source_query_id") or ""),
        }
    return _unique(ids), evidence


def _candidate_refs(cards: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    refs: list[Dict[str, Any]] = []
    for raw in cards or []:
        card = dict(raw or {})
        skill_id = str(card.get("skill_id") or "").strip()
        if not skill_id:
            continue
        refs.append({
            "skill_id": skill_id,
            "name": str(card.get("name") or card.get("title") or ""),
            "summary": str(card.get("summary") or card.get("description") or "")[:800],
            "domain": str(card.get("domain") or card.get("category") or ""),
            "tags": _unique(card.get("tags") or []),
            "matched_metadata_fields": _unique(
                card.get("matched_metadata_fields") or []
            ),
            "reasons": _unique(card.get("reasons") or []),
            "deterministic_score": int(
                card.get("deterministic_score") or card.get("score") or 0
            ),
            "document_digest": str(card.get("document_digest") or ""),
            "document_loaded": bool(card.get("document_loaded")),
            "declares_executable_runtime": bool(
                card.get("declares_executable_runtime")
            ),
            "source_query_id": str(card.get("source_query_id") or ""),
            "status": "candidate",
        })
    refs.sort(key=lambda item: (-item["deterministic_score"], item["skill_id"]))
    return refs


def _normalized_query_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = " ".join(text.replace("-", " ").replace("_", " ").split())
    return text


def record_skill_search_batch(
    state: Mapping[str, Any] | None,
    *,
    queries: Sequence[Mapping[str, Any] | str],
    candidate_cards: Sequence[Mapping[str, Any]],
    catalog_digest: str,
    input_roles: Sequence[str] = (),
    task_hypothesis: str = "",
    source: str = "consultant_query",
    total_limit: int = 12,
) -> Dict[str, Any]:
    """Record one Consultant-owned multi-hypothesis retrieval round."""
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    normalized_queries: list[Dict[str, str]] = []
    for index, raw in enumerate(queries or [], start=1):
        item = dict(raw) if isinstance(raw, Mapping) else {"text": str(raw)}
        text = str(item.get("text") or item.get("query") or "").strip()
        if not text:
            continue
        normalized_queries.append({
            "query_id": str(item.get("query_id") or f"q{index}"),
            "text": text,
            "normalized_text": _normalized_query_text(text),
            "purpose": str(item.get("purpose") or "")[:300],
        })
    if not 1 <= len(normalized_queries) <= 3:
        raise ValueError("SEARCH_SKILLS requires 1-3 non-empty queries")
    identity = {
        "queries": [
            {"query_id": item["query_id"], "text": item["normalized_text"]}
            for item in normalized_queries
        ],
        "catalog_digest": str(catalog_digest or ""),
        "input_roles": sorted(_unique(input_roles)),
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    repeated = fingerprint == str(updated.get("last_search_fingerprint") or "")
    if repeated:
        updated["repeated_search_count"] = int(
            updated.get("repeated_search_count") or 0
        ) + 1
    else:
        updated["normal_search_count"] = int(
            updated.get("normal_search_count") or 0
        ) + 1
        updated["repeated_search_count"] = 0
    attempted = list(updated.get("search_queries_attempted") or [])
    for item in normalized_queries:
        if item["normalized_text"] not in attempted:
            attempted.append(item["normalized_text"])

    new_refs = _candidate_refs(candidate_cards)
    refs_by_id = {
        str(item.get("skill_id") or ""): deepcopy(dict(item))
        for item in updated.get("candidate_skill_refs") or []
        if str(item.get("skill_id") or "")
    }
    for ref in new_refs:
        current = refs_by_id.get(ref["skill_id"])
        if current is None or int(ref.get("deterministic_score") or 0) > int(
            current.get("deterministic_score") or 0
        ):
            refs_by_id[ref["skill_id"]] = ref
    merged_refs = sorted(
        refs_by_id.values(),
        key=lambda item: (
            -int(item.get("deterministic_score") or 0),
            str(item.get("skill_id") or ""),
        ),
    )[: max(1, min(int(total_limit or 12), 12))]
    candidate_ids = [str(item["skill_id"]) for item in merged_refs]
    evidence = {
        str(item["skill_id"]): {
            key: deepcopy(item.get(key))
            for key in (
                "deterministic_score", "matched_metadata_fields", "reasons", "summary",
                "document_digest", "declares_executable_runtime", "source_query_id",
            )
        }
        for item in merged_refs
    }
    updated.update({
        "search_queries_attempted": attempted,
        "last_search_fingerprint": fingerprint,
        "catalog_digest": str(catalog_digest or ""),
        "candidate_skill_ids": candidate_ids,
        "candidate_evidence": evidence,
        "candidate_skill_refs": merged_refs,
        "input_roles": _unique(input_roles),
        "task_hypothesis": str(
            task_hypothesis or updated.get("task_hypothesis") or ""
        ).strip(),
        "capability_gap": False,
        "capability_gap_reason": "",
    })
    if not repeated:
        updated["search_history"] = [
            *list(updated.get("search_history") or []),
            {
                "source": str(source or "consultant_query"),
                "queries": normalized_queries,
                "catalog_digest": str(catalog_digest or ""),
                "candidate_ids": [
                    str(item.get("skill_id") or "") for item in new_refs
                ],
                "repeated": False,
                "fingerprint": fingerprint,
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


def record_skill_search(
    state: Mapping[str, Any] | None,
    *,
    query: str,
    candidate_cards: Sequence[Mapping[str, Any]],
    input_roles: Sequence[str] = (),
    task_hypothesis: str = "",
    source: str = "semantic_query",
) -> Dict[str, Any]:
    return record_skill_search_batch(
        state,
        queries=[{"query_id": "q1", "text": str(query or "")}],
        candidate_cards=candidate_cards,
        catalog_digest="",
        input_roles=input_roles,
        task_hypothesis=task_hypothesis,
        source=source,
    )


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
    candidate_refs = _candidate_refs(candidate_cards)
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
        updated["candidate_skill_refs"] = candidate_refs
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
    loaded_at_revision: int = 0,
    document_digest: str = "",
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    skill = str(skill_id or "").strip()
    if not skill:
        return updated
    loaded = _unique([*updated.get("loaded_skill_ids", []), skill])
    docs = deepcopy(dict(updated.get("loaded_skill_docs") or {}))
    prior_digest = str(
        (docs.get(skill) or {}).get("document_digest")
        or (docs.get(skill) or {}).get("sha256")
        or ""
    )
    document_digest = str(document_digest or "").strip() or hashlib.sha256(
        str(document or "").encode("utf-8")
    ).hexdigest()
    docs[skill] = {
        "path": str(path or ""),
        "source_path": str(path or ""),
        "sha256": document_digest,
        "document_digest": document_digest,
        "loaded_at_revision": max(0, int(loaded_at_revision or 0)),
        "summary": str(summary or "")[:600],
        "safe_summary": str(summary or "")[:600],
        "full_doc_cache_reference": f"skill-doc:{skill}:{document_digest[:16]}",
    }
    loaded_refs = [
        deepcopy(dict(item))
        for item in updated.get("loaded_skill_refs") or []
        if str(item.get("skill_id") or "") != skill
    ]
    loaded_refs.append({
        "skill_id": skill,
        "document_digest": document_digest,
        "loaded_at_revision": max(0, int(loaded_at_revision or 0)),
        "source_path": str(path or ""),
        "safe_summary": str(summary or "")[:600],
        "full_doc_cache_reference": f"skill-doc:{skill}:{document_digest[:16]}",
        "status": "loaded",
    })
    candidate_refs = []
    for item in updated.get("candidate_skill_refs") or []:
        ref = deepcopy(dict(item))
        if str(ref.get("skill_id") or "") == skill:
            ref["document_loaded"] = True
            ref["document_digest"] = document_digest
        candidate_refs.append(ref)
    updated.update({
        "loaded_skill_ids": loaded,
        "loaded_skill_docs": docs,
        "loaded_skill_refs": loaded_refs[-12:],
        "candidate_skill_refs": candidate_refs,
        "routing_status": READING_CANDIDATES,
    })
    if document_digest != prior_digest:
        updated["pending_document_injection_ids"] = _unique([
            *updated.get("pending_document_injection_ids", []), skill,
        ])
    return updated


def record_loaded_skill_docs(
    state: Mapping[str, Any] | None,
    documents: Sequence[Mapping[str, Any]],
    *,
    loaded_at_revision: int = 0,
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    for item in documents or []:
        updated = record_loaded_skill_doc(
            updated,
            skill_id=str(item.get("skill_id") or ""),
            path=str(item.get("source_path") or item.get("path") or ""),
            document=str(item.get("document") or ""),
            summary=str(item.get("safe_summary") or item.get("summary") or ""),
            loaded_at_revision=loaded_at_revision,
            document_digest=str(item.get("document_digest") or ""),
        )
    return updated


def record_rejected_candidate(
    state: Mapping[str, Any] | None,
    *,
    skill_id: str,
    reason: str,
    reason_code: str = "not_required",
    decision_revision: int = 0,
    source: str = "consultant",
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
    row = {
        "skill_id": skill,
        "reason_code": str(reason_code or "not_required"),
        "reason": str(reason or "")[:800],
        "decision_revision": max(0, int(decision_revision or 0)),
        "source": str(source or "consultant"),
        "status": "excluded",
    }
    rows.append(row)
    updated["rejected_candidates"] = rows[-12:]
    excluded = [
        deepcopy(dict(item))
        for item in updated.get("excluded_skill_refs") or []
        if str(item.get("skill_id") or "") != skill
    ]
    excluded.append(deepcopy(row))
    updated["excluded_skill_refs"] = excluded[-12:]
    remaining = [
        item for item in updated.get("candidate_skill_ids") or []
        if item not in {row["skill_id"] for row in rows}
    ]
    updated["routing_status"] = CANDIDATES_FOUND if remaining else EXPANDING_DISCOVERY
    return updated


def record_excluded_skills(
    state: Mapping[str, Any] | None,
    decisions: Sequence[Mapping[str, Any]],
    *,
    decision_revision: int = 0,
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    for item in decisions or []:
        updated = record_rejected_candidate(
            updated,
            skill_id=str(item.get("skill_id") or ""),
            reason=str(item.get("reason") or ""),
            reason_code=str(item.get("reason_code") or "not_required"),
            decision_revision=int(
                item.get("decision_revision") or decision_revision or 0
            ),
            source=str(item.get("source") or "consultant"),
        )
    return updated


def record_selected_skills(
    state: Mapping[str, Any] | None,
    selected_skill_ids: Sequence[str],
    *,
    decisions: Sequence[Mapping[str, Any]] = (),
    selection_revision: int = 0,
    reason: str = "",
) -> Dict[str, Any]:
    updated = normalize_skill_discovery_state(state)
    updated.pop("recovery_directive", None)
    selected = _unique(selected_skill_ids)
    excluded_ids = {
        str(item.get("skill_id") or "")
        for item in updated.get("excluded_skill_refs") or []
    }
    conflict = sorted(set(selected).intersection(excluded_ids))
    if conflict:
        raise ValueError(
            "selected and excluded Skill sets overlap: " + ", ".join(conflict)
        )
    decision_by_id = {
        str(item.get("skill_id") or ""): dict(item)
        for item in decisions or []
        if isinstance(item, Mapping) and str(item.get("skill_id") or "")
    }
    loaded_docs = dict(updated.get("loaded_skill_docs") or {})
    selected_refs: list[Dict[str, Any]] = []
    for index, skill_id in enumerate(selected):
        decision = decision_by_id.get(skill_id, {})
        doc = dict(loaded_docs.get(skill_id) or {})
        selected_refs.append({
            "skill_id": skill_id,
            "role": str(decision.get("role") or ("primary" if index == 0 else "supporting")),
            "reason": str(decision.get("reason") or reason or "")[:800],
            "document_digest": str(
                decision.get("document_digest")
                or doc.get("document_digest")
                or doc.get("sha256")
                or ""
            ),
            "selection_revision": max(0, int(
                decision.get("selection_revision") or selection_revision or 0
            )),
            "source": "consultant",
            "status": "selected",
        })
    updated["selected_skill_ids"] = selected
    updated["selected_skill_refs"] = selected_refs
    if selected:
        updated["routing_status"] = SELECTED
        updated["capability_gap"] = False
        updated["capability_gap_reason"] = ""
    return updated


def selection_document_errors(
    state: Mapping[str, Any] | None,
    selected_skill_ids: Sequence[str],
    *,
    current_document_digests: Mapping[str, str] | None = None,
) -> list[Dict[str, Any]]:
    """Return typed Consultant-owned load-before-select violations."""
    current = normalize_skill_discovery_state(state)
    docs = dict(current.get("loaded_skill_docs") or {})
    expected_digests = dict(current_document_digests or {})
    errors: list[Dict[str, Any]] = []
    for skill_id in _unique(selected_skill_ids):
        doc = dict(docs.get(skill_id) or {})
        digest = str(doc.get("document_digest") or doc.get("sha256") or "")
        if not digest:
            errors.append({
                "code": "SKILL_DOC_REQUIRED",
                "owner": "consultant",
                "field": f"selected_skill_refs[{skill_id}].document_digest",
                "skill_id": skill_id,
                "current_value": "",
                "expected_structural_condition": (
                    "the selected Skill document is loaded at the current digest"
                ),
                "evidence": {"loaded_skill_ids": list(current.get("loaded_skill_ids") or [])},
                "allowed_action": "load_skill_doc",
            })
        elif expected_digests.get(skill_id) and digest != str(
            expected_digests[skill_id]
        ):
            errors.append({
                "code": "SKILL_DOC_STALE",
                "owner": "consultant",
                "field": f"selected_skill_refs[{skill_id}].document_digest",
                "skill_id": skill_id,
                "current_value": digest,
                "expected_structural_condition": (
                    "the loaded document digest equals the current catalog digest"
                ),
                "evidence": {
                    "catalog_document_digest": str(expected_digests[skill_id]),
                },
                "allowed_action": "load_skill_doc",
            })
    return errors


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
        # Selection is substantive progress, not a request for a ceremonial
        # acknowledgement round.  The Controller evaluates completion now;
        # only unresolved task semantics require another Consultant response.
        return ["COMPLETE_TASK_SEMANTICS"]
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
        "observed_skill_mentions": list(
            current.get("observed_skill_mentions") or []
        ),
        "input_roles": list(current.get("input_roles") or []),
        "search_queries_attempted": list(current.get("search_queries_attempted") or []),
        "search_history": deepcopy(list(current.get("search_history") or [])),
        "candidate_skill_ids": list(current.get("candidate_skill_ids") or []),
        "candidate_evidence": deepcopy(dict(current.get("candidate_evidence") or {})),
        "candidate_skill_refs": deepcopy(list(
            current.get("candidate_skill_refs") or []
        )),
        "loaded_skill_ids": list(current.get("loaded_skill_ids") or []),
        "loaded_skill_docs": deepcopy(dict(current.get("loaded_skill_docs") or {})),
        "loaded_skill_refs": deepcopy(list(current.get("loaded_skill_refs") or [])),
        "selected_skill_refs": deepcopy(list(current.get("selected_skill_refs") or [])),
        "excluded_skill_refs": deepcopy(list(current.get("excluded_skill_refs") or [])),
        "rejected_candidates": deepcopy(list(current.get("rejected_candidates") or [])),
        "catalog_digest": str(current.get("catalog_digest") or ""),
        "last_search_fingerprint": str(
            current.get("last_search_fingerprint") or ""
        ),
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
        "observed_skill_mentions": list(
            current.get("observed_skill_mentions") or []
        ),
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
        "selected_skill_refs": deepcopy(list(current.get("selected_skill_refs") or [])),
        "excluded_skill_refs": deepcopy(list(current.get("excluded_skill_refs") or [])),
        "rejected_candidates": deepcopy(list(current.get("rejected_candidates") or [])),
        "current_unresolved_items": ["skill-routing"],
        "allowed_next_actions": discovery_next_actions(current),
    }

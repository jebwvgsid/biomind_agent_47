"""Canonical Consultant Skill-binding phase and no-progress contracts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from enum import Enum
from typing import Any, Dict, Mapping, Sequence

from agent_core.plan_tracker import (
    canonical_task_spec_for_state,
    has_materialized_plan,
)
from agent_core.skill_access import normalize_skill_access_state
from agent_core.task_spec import (
    task_semantic_hash,
    task_spec_is_initialized,
    task_spec_revision,
)


class ConsultantBindingPhase(str, Enum):
    INITIAL_TASK_AND_SKILL_BOOTSTRAP = "INITIAL_TASK_AND_SKILL_BOOTSTRAP"
    PREPLAN_INITIAL_SKILL_BIND = "PREPLAN_INITIAL_SKILL_BIND"
    EXISTING_BINDING_CHANGE = "EXISTING_BINDING_CHANGE"
    INCONSISTENT_CANONICAL_STATE = "INCONSISTENT_CANONICAL_STATE"


class ConsultantEntryDisposition(str, Enum):
    CONTINUE_BOOTSTRAP = "CONTINUE_BOOTSTRAP"
    CONTINUE_RECOVERABLE_TRANSITION = "CONTINUE_RECOVERABLE_TRANSITION"
    FAIL_CANONICAL_INCONSISTENCY = "FAIL_CANONICAL_INCONSISTENCY"


_VOLATILE_PROGRESS_KEYS = {
    "created_at",
    "updated_at",
    "recorded_at",
    "served_at",
    "completed_at",
    "last_accessed_at",
}


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _stable_json(value).encode("utf-8")
    ).hexdigest()


def _semantic_progress_projection(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_progress_projection(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_PROGRESS_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_progress_projection(item) for item in value]
    return deepcopy(value)


def active_task_skill_refs(
    plan_state: Mapping[str, Any] | None,
    *,
    access_state: Mapping[str, Any] | None = None,
) -> list[Dict[str, Any]]:
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    access = normalize_skill_access_state(
        access_state
        or task_state.get("skill_access_state")
        or state.get("skill_access_state")
        or {"selected_skill_refs": state.get("selected_skill_refs") or []}
    )
    return [
        deepcopy(dict(ref))
        for ref in access.get("selected_skill_refs") or []
        if isinstance(ref, Mapping)
        and str(ref.get("status") or "") == "active"
        and str(ref.get("scope") or "task") == "task"
    ]


def consultant_planning_readiness(
    plan_state: Mapping[str, Any] | None,
    *,
    require_agent_initiated: bool = False,
) -> Dict[str, Any]:
    """Classify the durable, route-free Consultant-to-Planner handoff.

    A successful Consultant commit has two live representations: the current
    canonical TaskSpec plus an accepted handoff and ready consultation
    lifecycle, or the versioned FINALIZE receipt used by older live paths.
    Neither representation needs (or is allowed to use) a destination field.
    """

    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    current_task = dict(state.get("current_task") or {})
    task_spec = canonical_task_spec_for_state(state)
    consultation = dict(task_state.get("consultation_state") or {})
    accepted = dict(task_state.get("accepted_consultant_handoff") or {})
    selected = list(dict.fromkeys(
        str(value)
        for value in (
            task_state.get("selected_skills")
            or state.get("selected_skill_ids")
            or []
        )
        if str(value)
    ))
    accepted_selected = list(dict.fromkeys(
        str(value)
        for value in accepted.get("selected_skills") or []
        if str(value)
    ))
    revision = int(
        current_task.get("task_revision")
        or task_spec_revision(task_spec)
        or 0
    )
    pending_question = (
        task_state.get("pending_question")
        or consultation.get("pending_question")
        or consultation.get("pending_human_question")
        or {}
    )
    protocol = dict(consultation.get("finalize_protocol") or {})
    compile_outcome = dict(protocol.get("compile_outcome") or {})
    compiled = bool(
        int(protocol.get("schema_version") or 0) == 4
        and str(protocol.get("finalize_status") or "").lower() == "compiled"
        and str(compile_outcome.get("status") or "").lower() == "compiled"
    )
    lifecycle_ready = bool(
        str(consultation.get("phase") or "").lower() == "ready_to_plan"
        and str(consultation.get("consultation_status") or "").lower()
        in {"ready", "completed"}
        and str(consultation.get("completion_decision") or "").lower()
        in {"ready_to_finalize", "ready_to_plan", "completed"}
    )
    selection_consistent = bool(
        not accepted_selected or accepted_selected == selected
    )
    agent_handoff = bool(
        accepted.get("agent_initiated") is True
        and str(accepted.get("handoff_kind") or "") == "execution_planning"
        and int(accepted.get("handoff_protocol_version") or 0) >= 1
    )
    accepted_ready = bool(
        accepted
        and lifecycle_ready
        and selection_consistent
        and (agent_handoff or not require_agent_initiated)
    )
    ready = bool(
        task_spec_is_initialized(task_spec)
        and revision > 0
        and not pending_question
        and (
            accepted_ready
            or (compiled and not require_agent_initiated)
        )
    )
    if ready:
        reason_code = (
            "accepted_consultant_handoff_ready"
            if accepted_ready
            else "versioned_finalize_compiled"
        )
    elif pending_question:
        reason_code = "consultant_question_pending"
    elif not task_spec_is_initialized(task_spec):
        reason_code = "canonical_task_spec_missing"
    elif revision <= 0:
        reason_code = "canonical_task_revision_missing"
    elif accepted and not selection_consistent:
        reason_code = "accepted_handoff_skill_selection_mismatch"
    elif require_agent_initiated and not agent_handoff:
        reason_code = "agent_initiated_handoff_missing"
    else:
        reason_code = "consultant_handoff_not_committed"
    return {
        "ready": ready,
        "reason_code": reason_code,
        "task_revision": revision,
        "contract_status": (
            "compiled" if compiled else "canonical_task_ready" if accepted_ready else "incomplete"
        ),
        "phase": str(consultation.get("phase") or ""),
        "consultation_status": str(consultation.get("consultation_status") or ""),
        "completion_decision": str(consultation.get("completion_decision") or ""),
        "accepted_handoff": bool(accepted),
        "agent_initiated_handoff": agent_handoff,
        "selected_skill_ids": selected,
        # Business-evidence requirements remain available to the Agent as
        # observations.  They are intentionally not a global planning gate.
        "unmet_mandatory_requirement_ids": [],
    }


def classify_consultant_binding_phase(
    plan_state: Mapping[str, Any] | None,
    *,
    access_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Classify binding authority using canonical material state only."""
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    task_spec = canonical_task_spec_for_state(state)
    task_exists = task_spec_is_initialized(task_spec)
    active_refs = active_task_skill_refs(state, access_state=access_state)
    active_ids = list(dict.fromkeys(
        str(ref.get("skill_id") or "")
        for ref in active_refs
        if str(ref.get("skill_id") or "")
    ))
    compatibility_ids = list(dict.fromkeys(
        str(value)
        for value in (
            task_state.get("selected_skills")
            or state.get("selected_skill_ids")
            or []
        )
        if str(value)
    ))
    committed_binding_exists = bool(active_refs or compatibility_ids)
    materialized_plan = has_materialized_plan(state)
    canonical_plan_pointer_present = bool(state.get("current_plan"))
    binding_projection_inconsistent = bool(
        active_ids != compatibility_ids
        and (active_ids or compatibility_ids)
    )

    if (
        (not task_exists and (
            committed_binding_exists
            or materialized_plan
        ))
        or binding_projection_inconsistent
    ):
        phase = ConsultantBindingPhase.INCONSISTENT_CANONICAL_STATE
    elif not task_exists:
        phase = ConsultantBindingPhase.INITIAL_TASK_AND_SKILL_BOOTSTRAP
    elif not committed_binding_exists and not materialized_plan:
        phase = ConsultantBindingPhase.PREPLAN_INITIAL_SKILL_BIND
    else:
        phase = ConsultantBindingPhase.EXISTING_BINDING_CHANGE

    return {
        "phase": phase.value,
        "canonical_task_exists": task_exists,
        "active_skill_refs": active_refs,
        "active_skill_ids": active_ids,
        "compatibility_selected_skill_ids": compatibility_ids,
        "committed_skill_binding_exists": committed_binding_exists,
        "materialized_plan_exists": materialized_plan,
        "canonical_plan_pointer_present": canonical_plan_pointer_present,
        "binding_projection_inconsistent": binding_projection_inconsistent,
    }


def consultant_entry_disposition(
    plan_state: Mapping[str, Any] | None,
    *,
    consultation_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Classify Consultant entry from durable canonical facts and transitions."""
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    canonical_task = canonical_task_spec_for_state(state)
    task_exists = task_spec_is_initialized(canonical_task)
    consultation = dict(
        consultation_state
        or task_state.get("consultation_state")
        or {}
    )
    phase = str(consultation.get("phase") or "")
    binding_phase = classify_consultant_binding_phase(state)
    accepted_handoff = dict(
        task_state.get("accepted_consultant_handoff") or {}
    )
    pending_transition = dict(
        task_state.get("consultant_pending_transition") or {}
    )
    conflict_reasons = []
    if not task_exists and accepted_handoff:
        conflict_reasons.append(
            "accepted_consultant_handoff_without_canonical_task"
        )
    if binding_phase["phase"] == (
        ConsultantBindingPhase.INCONSISTENT_CANONICAL_STATE.value
    ):
        conflict_reasons.append("canonical_binding_partition_inconsistent")
    if conflict_reasons:
        return {
            "disposition": (
                ConsultantEntryDisposition.FAIL_CANONICAL_INCONSISTENCY.value
            ),
            "classification": "INTERNAL_PROTOCOL",
            "code": "CONSULTANT_CANONICAL_STATE_INCONSISTENT",
            "detail": (
                "Accepted Consultant/Planner state conflicts with the durable "
                "canonical TaskSpec or selected-Skill binding."
            ),
            "terminal_route": "finish",
            "consultation_phase": phase,
            "binding_phase": binding_phase,
            "reasons": conflict_reasons,
        }

    recoverable_transition = bool(
        pending_transition
        and pending_transition.get("recoverable") is True
        and pending_transition.get("terminal") is False
        and str(pending_transition.get("owner") or "") == "consultant"
        and str(pending_transition.get("next_route") or "") == "consultant"
    )
    if recoverable_transition:
        return {
            "disposition": (
                ConsultantEntryDisposition.CONTINUE_RECOVERABLE_TRANSITION.value
            ),
            "terminal_route": "",
            "consultation_phase": phase,
            "binding_phase": binding_phase,
            "pending_transition": deepcopy(pending_transition),
            "required_action": str(
                pending_transition.get("required_action") or ""
            ),
        }
    return {
        "disposition": ConsultantEntryDisposition.CONTINUE_BOOTSTRAP.value,
        "terminal_route": "",
        "consultation_phase": phase,
        "binding_phase": binding_phase,
        "pending_transition": {},
        "required_action": "",
    }


def consultant_uninitialized_entry_diagnostic(
    plan_state: Mapping[str, Any] | None,
    *,
    consultation_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return only a terminal canonical-conflict diagnostic for compatibility."""
    disposition = consultant_entry_disposition(
        plan_state,
        consultation_state=consultation_state,
    )
    if disposition.get("disposition") != (
        ConsultantEntryDisposition.FAIL_CANONICAL_INCONSISTENCY.value
    ):
        return {}
    return disposition


def consultant_validation_no_progress_fingerprint(
    plan_state: Mapping[str, Any] | None,
    *,
    rejection_code: str,
    skill_id: str,
    candidate_action_type: str,
    consultation_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Fingerprint a validation retry against the current canonical progress."""
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    consultation = dict(
        consultation_state
        or task_state.get("consultation_state")
        or {}
    )
    discovery = dict(consultation.get("skill_discovery") or {})
    canonical_task = canonical_task_spec_for_state(state)
    access = normalize_skill_access_state(
        task_state.get("skill_access_state")
        or state.get("skill_access_state")
    )
    canonical_progress = {
        "task_revision": (
            task_spec_revision(canonical_task)
            if task_spec_is_initialized(canonical_task)
            else 0
        ),
        "task_semantic_hash": (
            task_semantic_hash(canonical_task)
            if task_spec_is_initialized(canonical_task)
            else ""
        ),
        "accepted_consultant_handoff": _semantic_progress_projection(
            task_state.get("accepted_consultant_handoff") or {}
        ),
        "loaded_skill_ids": sorted({
            str(value)
            for value in discovery.get("loaded_skill_ids") or []
            if str(value)
        }),
        "skill_access": _semantic_progress_projection({
            key: access.get(key) or {}
            for key in (
                "selected_skill_refs",
                "snapshots",
                "resource_snapshots",
                "excerpts",
            )
        }),
        "active_evidence": _semantic_progress_projection({
            str(key): value
            for key, value in dict(
                consultation.get("requirements") or {}
            ).items()
            if isinstance(value, Mapping)
            and bool(value.get("active", True))
        }),
        "model_visible_repair_context": _semantic_progress_projection({
            "last_feedback": list(
                consultation.get("last_feedback") or []
            )[-1:],
            "recovery_directive": discovery.get("recovery_directive") or {},
        }),
    }
    canonical_progress_version = _digest(canonical_progress)
    payload = {
        "rejection_code": str(rejection_code or ""),
        "skill_id": str(skill_id or ""),
        "candidate_action_type": str(candidate_action_type or ""),
        "canonical_progress_version": canonical_progress_version,
    }
    return {
        "fingerprint": _digest(payload),
        **payload,
        "canonical_progress": canonical_progress,
    }


def legacy_uninitialized_task_reset_decision(
    plan_state: Mapping[str, Any] | None,
    *,
    consultation_state: Mapping[str, Any] | None = None,
    contract_repair_present: bool = False,
) -> Dict[str, Any]:
    """Bound legacy reset and explicitly carry the session no-progress ledger."""
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    consultation = dict(
        consultation_state
        or task_state.get("consultation_state")
        or {}
    )
    phase = str(consultation.get("phase") or "")
    diagnostic = consultant_uninitialized_entry_diagnostic(
        state,
        consultation_state=consultation,
    )
    eligible_legacy_shape = bool(
        contract_repair_present
        or isinstance(consultation.get("finalize_protocol"), Mapping)
        or phase in {"finalize", "ready_to_finalize"}
    )
    return {
        "allowed": bool(
            not task_spec_is_initialized(canonical_task_spec_for_state(state))
            and not diagnostic
            and eligible_legacy_shape
        ),
        "consultation_phase": phase,
        "diagnostic": diagnostic,
        "preserved_consultant_no_progress_ledger": deepcopy(dict(
            task_state.get("consultant_no_progress_ledger") or {}
        )),
    }


def consultant_commit_no_progress_fingerprint(
    plan_state: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    active_skill_refs: Sequence[Mapping[str, Any]],
    effective_route: str,
    rejection_reason: Mapping[str, Any] | str,
) -> Dict[str, Any]:
    """Build a semantic fingerprint for one rejected Consultant self-route."""
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    conversation = dict(state.get("conversation_evidence") or {})
    canonical_task = canonical_task_spec_for_state(state)
    proposed_task = candidate.get("task_spec")
    access = normalize_skill_access_state(
        task_state.get("skill_access_state")
        or state.get("skill_access_state")
    )
    consultation = dict(candidate.get("consultation_state") or {})
    discovery = dict(consultation.get("skill_discovery") or {})
    current_plan = dict(state.get("current_plan") or {})
    candidate_projection = {
        "task_semantic_hash": (
            task_semantic_hash(proposed_task)
            if task_spec_is_initialized(proposed_task)
            else ""
        ),
        "selected_skill_ids": sorted({
            str(value)
            for value in (
                candidate.get("selected_skills")
                or candidate.get("selected_skill_ids")
                or []
            )
            if str(value)
        }),
        "resolved_bindings": _semantic_progress_projection(
            candidate.get("resolved_bindings") or {}
        ),
        "pending_question": _semantic_progress_projection(
            candidate.get("pending_question")
            or candidate.get("pending_human_question")
            or {}
        ),
    }
    progress_projection = {
        "skill_access": {
            key: _semantic_progress_projection(access.get(key) or {})
            for key in (
                "selected_skill_refs",
                "snapshots",
                "resource_snapshots",
                "excerpts",
                "searches",
                "skill_tool_results",
            )
        },
        "discovery": _semantic_progress_projection({
            key: discovery.get(key)
            for key in (
                "routing_status",
                "reasoning_level",
                "search_queries_attempted",
                "search_history",
                "candidate_skill_ids",
                "loaded_skill_ids",
                "rejected_candidates",
                "normal_search_count",
                "catalog_expansion_count",
                "reasoning_escalation_count",
            )
        }),
        "active_evidence": _semantic_progress_projection({
            str(key): value
            for key, value in dict(
                consultation.get("requirements") or {}
            ).items()
            if isinstance(value, Mapping)
            and bool(value.get("active", True))
        }),
        "model_visible_repair_context": _semantic_progress_projection({
            "last_feedback": list(
                consultation.get("last_feedback") or []
            )[-1:],
            "recovery_directive": discovery.get("recovery_directive") or {},
        }),
    }
    payload = {
        "user_turn_revision": int(
            conversation.get("user_turn_count")
            if conversation.get("user_turn_count") is not None
            else conversation.get("turn_count")
            or 0
        ),
        "canonical_task_spec_hash": (
            task_semantic_hash(canonical_task)
            if task_spec_is_initialized(canonical_task)
            else ""
        ),
        "active_skill_refs_hash": _digest(
            _semantic_progress_projection(list(active_skill_refs or []))
        ),
        "materialized_plan_hash": (
            str(current_plan.get("plan_semantic_hash") or "")
            or _digest({
                "plan_snapshot_ref": str(
                    current_plan.get("plan_snapshot_ref") or ""
                ),
                "plan_spec": _semantic_progress_projection(
                    current_plan.get("plan_spec") or {}
                ),
                "plan_record_hash": str(
                    current_plan.get("plan_record_hash") or ""
                ),
            })
            if has_materialized_plan(state)
            else ""
        ),
        "candidate_hash": _digest(candidate_projection),
        "effective_route": str(effective_route or ""),
        "rejection_reason": _semantic_progress_projection(rejection_reason),
        "skill_progress_hash": _digest(progress_projection),
        "active_evidence_hash": _digest(
            progress_projection.get("active_evidence") or {}
        ),
        "model_visible_repair_context_hash": _digest(
            progress_projection.get("model_visible_repair_context") or {}
        ),
    }
    return {"fingerprint": _digest(payload), **payload}


def advance_consultant_commit_no_progress(
    previous: Mapping[str, Any] | None,
    transition: Mapping[str, Any],
    *,
    limit: int,
) -> Dict[str, Any]:
    """Count only identical rejected Consultant self-routes."""
    prior = dict(previous or {})
    current = dict(transition or {})
    normalized_limit = max(2, int(limit or 0))
    identical = bool(
        str(prior.get("fingerprint") or "")
        and str(prior.get("fingerprint") or "")
        == str(current.get("fingerprint") or "")
    )
    count = int(prior.get("identical_transition_count") or 0) + 1 if identical else 1
    return {
        **deepcopy(current),
        "identical_transition_count": count,
        "limit": normalized_limit,
        "progress_observed": not identical,
        "terminal": count >= normalized_limit,
    }

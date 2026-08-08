"""Deterministic Consultant evidence ledger and completion state machine.

This module deliberately knows nothing about individual tools or command keywords.
It turns structured input/runtime facts and bounded read-only observations into a
small controller-owned state that can be checkpointed by LangGraph.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


CONSULTATION_PHASES = {
    "consulting",
    "needs_evidence",
    "evaluate",
    "ready_to_finalize",
    "finalize",
    "needs_user",
    "blocked",
}
EVIDENCE_STATUSES = {"unresolved", "satisfied", "failed", "blocked", "waived"}
TERMINAL_REQUIREMENT_STATUSES = {"satisfied", "waived"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_.:-]+", "-", str(value or "").strip().lower()).strip("-") or "item"


def _requirement(
    requirement_id: str,
    purpose: str,
    *,
    mandatory: bool = True,
    status: str = "unresolved",
    satisfied_by: Optional[Sequence[str]] = None,
    provenance: Optional[Mapping[str, Any]] = None,
    freshness: str = "current_consultation",
) -> Dict[str, Any]:
    if status not in EVIDENCE_STATUSES:
        raise ValueError(f"Unsupported evidence status: {status}")
    return {
        "id": _safe_id(requirement_id),
        "purpose": str(purpose or "").strip(),
        "mandatory": bool(mandatory),
        "status": status,
        "satisfied_by": list(satisfied_by or []),
        "evidence_provenance": dict(provenance or {}),
        "freshness": str(freshness or "current_consultation"),
        "updated_at": _now(),
    }


def new_consultation_state(
    consultation_id: str,
    task_id: str,
    *,
    max_total_execute: int = 4,
    max_no_progress: int = 2,
) -> Dict[str, Any]:
    """Create an empty, serializable consultation controller state."""
    return {
        "schema_version": 1,
        "consultation_id": str(consultation_id),
        "task_id": str(task_id),
        "phase": "consulting",
        "consultation_status": "active",
        "iteration": 0,
        "requirements": {},
        "actions": {},
        "evidence": [],
        "execute_call_count": 0,
        "duplicate_reuse_count": 0,
        "no_progress_count": 0,
        "max_total_execute": max(1, int(max_total_execute)),
        "max_no_progress": max(1, int(max_no_progress)),
        "completion_decision": "needs_evidence",
        "completion_reason": "requirements_not_seeded",
        "created_at": _now(),
        "updated_at": _now(),
    }


def add_requirement(state: Mapping[str, Any], requirement: Mapping[str, Any]) -> Dict[str, Any]:
    updated = deepcopy(dict(state))
    req = dict(requirement)
    requirement_id = _safe_id(str(req.get("id") or ""))
    if not requirement_id or not str(req.get("purpose") or "").strip():
        raise ValueError("Evidence requirement needs id and purpose")
    req["id"] = requirement_id
    req.setdefault("mandatory", True)
    req.setdefault("status", "unresolved")
    req.setdefault("satisfied_by", [])
    req.setdefault("evidence_provenance", {})
    req.setdefault("freshness", "current_consultation")
    req["updated_at"] = _now()
    updated.setdefault("requirements", {})[requirement_id] = req
    updated["updated_at"] = _now()
    return updated


def seed_structured_evidence(
    state: Mapping[str, Any],
    *,
    input_audit: Optional[Mapping[str, Any]] = None,
    candidate_skill_ids: Optional[Sequence[str]] = None,
    routing_candidate_ids: Optional[Sequence[str]] = None,
    runtime_evidence: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Seed controller requirements without interpreting tool names or commands.

    A structured formal-input audit is evidence in its own right. Candidate skills
    create one generic runtime-readiness requirement each unless current structured
    runtime evidence already proves readiness.
    """
    updated = deepcopy(dict(state))
    requirements = updated.setdefault("requirements", {})
    audit = dict(input_audit or {})
    if audit and bool(audit.get("relevant")) and "formal-inputs" not in requirements:
        unresolved_value = audit.get("unresolved")
        unresolved_items = audit.get("missing") or audit.get("missing_required_inputs") or audit.get("unresolved_reasons") or []
        unresolved = list(unresolved_items) if isinstance(unresolved_items, (list, tuple, set)) else [str(unresolved_items)]
        is_unresolved = bool(unresolved_value) or bool(unresolved)
        status_text = str(audit.get("status") or "").strip().lower()
        resolved_items = (
            audit.get("resolved")
            or audit.get("resolved_inputs")
            or audit.get("artifacts")
            or audit.get("matches")
            or []
        )
        has_resolution = bool(resolved_items) or status_text in {"resolved", "passed", "complete", "satisfied", "success"}
        status = "satisfied" if has_resolution and not is_unresolved else "unresolved"
        requirements["formal-inputs"] = _requirement(
            "formal-inputs",
            "Establish the required formal upstream inputs from the structured input audit.",
            status=status,
            satisfied_by=["structured_input_audit"] if status == "satisfied" else [],
            provenance={"kind": "structured_input_audit", "digest": _digest(audit)},
            freshness="task_revision",
        )

    runtime = dict(runtime_evidence or {})
    routing_ids = list(dict.fromkeys(
        str(item).strip() for item in (routing_candidate_ids or []) if str(item).strip()
    ))
    if "skill-routing" not in requirements:
        requirements["skill-routing"] = _requirement(
            "skill-routing",
            "Resolve a canonical skill or loaded workflow route for Planner.",
            status="satisfied" if routing_ids else "unresolved",
            satisfied_by=["canonical_metadata_candidate"] if routing_ids else [],
            provenance={"kind": "canonical_metadata", "candidate_ids": routing_ids},
            freshness="task_revision",
        )
    elif routing_ids and requirements["skill-routing"].get("status") != "satisfied":
        requirements["skill-routing"].update({
            "status": "satisfied",
            "satisfied_by": ["canonical_metadata_candidate"],
            "evidence_provenance": {"kind": "canonical_metadata", "candidate_ids": routing_ids},
            "updated_at": _now(),
        })
    for skill_id in dict.fromkeys(str(item).strip() for item in (candidate_skill_ids or []) if str(item).strip()):
        requirement_id = f"runtime:{_safe_id(skill_id)}"
        if requirement_id in requirements:
            continue
        item = runtime.get(skill_id) if isinstance(runtime.get(skill_id), Mapping) else {}
        item_status = str((item or {}).get("status") or "").strip().lower()
        ready = item_status in {"ready", "verified", "passed", "satisfied", "success"}
        requirements[requirement_id] = _requirement(
            requirement_id,
            f"Establish current runtime readiness for canonical skill {skill_id} using declared evidence.",
            status="satisfied" if ready else "unresolved",
            satisfied_by=["structured_runtime_evidence"] if ready else [],
            provenance={"kind": "structured_runtime_evidence", "skill_id": skill_id, "digest": _digest(item)} if item else {"kind": "candidate_skill", "skill_id": skill_id},
            freshness="current_runtime",
        )
    updated["updated_at"] = _now()
    return evaluate_consultation_completion(updated)


def canonical_action_fingerprint(
    command: str,
    *,
    cwd: str,
    requirement_ids: Sequence[str],
    environment: Optional[Mapping[str, Any]] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> str:
    """Fingerprint a proposed action from semantic execution identity fields."""
    normalized_command = re.sub(r"[ \t]+", " ", str(command or "").strip())
    payload = {
        "command": normalized_command,
        "cwd": os.path.realpath(str(cwd or ".")),
        "environment": dict(environment or {}),
        "context": dict(context or {}),
    }
    return _digest(payload)


def evaluate_consultation_completion(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Single deterministic completion evaluator used after every state change."""
    updated = deepcopy(dict(state))
    requirements = list((updated.get("requirements") or {}).values())
    mandatory = [item for item in requirements if bool(item.get("mandatory", True))]
    blocked = [item for item in mandatory if str(item.get("status")) == "blocked"]
    failed = [item for item in mandatory if str(item.get("status")) == "failed"]
    unresolved = [item for item in mandatory if str(item.get("status")) not in TERMINAL_REQUIREMENT_STATUSES]
    phase = str(updated.get("phase") or "consulting")

    if blocked:
        decision, reason, next_phase, status = "blocked", "mandatory_evidence_blocked", "blocked", "blocked"
    elif int(updated.get("no_progress_count") or 0) >= int(updated.get("max_no_progress") or 2):
        decision, reason, next_phase, status = "needs_user", "no_progress_limit_reached", "needs_user", "needs_user"
    elif int(updated.get("execute_call_count") or 0) >= int(updated.get("max_total_execute") or 4) and unresolved:
        decision, reason, next_phase, status = "blocked", "emergency_execute_ceiling_reached", "blocked", "blocked"
    elif not requirements:
        decision, reason, next_phase, status = "needs_evidence", "requirements_not_seeded", "consulting", "active"
    elif unresolved:
        decision = "needs_evidence"
        reason = "mandatory_evidence_failed" if failed else "mandatory_evidence_unresolved"
        next_phase, status = "needs_evidence", "active"
    else:
        decision, reason, status = "ready_to_finalize", "all_mandatory_evidence_satisfied", "ready"
        next_phase = "finalize" if phase in {"ready_to_finalize", "finalize"} else "ready_to_finalize"

    updated.update({
        "phase": next_phase,
        "consultation_status": status,
        "completion_decision": decision,
        "completion_reason": reason,
        "unresolved_requirement_ids": [str(item.get("id")) for item in unresolved],
        "updated_at": _now(),
    })
    return updated


def begin_finalize(state: Mapping[str, Any]) -> Dict[str, Any]:
    updated = evaluate_consultation_completion(state)
    if updated.get("completion_decision") != "ready_to_finalize":
        raise ValueError("Consultation cannot enter FINALIZE with unresolved mandatory evidence")
    updated["phase"] = "finalize"
    updated["updated_at"] = _now()
    return updated


def preflight_execute(
    state: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    cwd: str,
    environment: Optional[Mapping[str, Any]] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate evidence purpose and deduplicate before confirmation/spawn."""
    updated = evaluate_consultation_completion(state)
    if updated.get("phase") in {"ready_to_finalize", "finalize"}:
        return {"decision": "finalize_execute_forbidden", "state": begin_finalize(updated)}
    if updated.get("completion_decision") in {"blocked", "needs_user"}:
        return {"decision": str(updated.get("completion_decision")), "state": updated}

    requirement_ids = [
        _safe_id(str(item)) for item in (proposal.get("requirement_ids") or []) if str(item).strip()
    ]
    purpose = str(proposal.get("purpose") or "").strip()
    expected = proposal.get("expected_evidence")
    if not requirement_ids or not purpose or not expected:
        return {"decision": "invalid_proposal", "reason": "requirement_ids, purpose, and expected_evidence are required", "state": updated}
    requirements = updated.get("requirements") or {}
    unknown = [item for item in requirement_ids if item not in requirements]
    if unknown:
        return {"decision": "invalid_requirement", "unknown_requirement_ids": unknown, "state": updated}
    if all(str(requirements[item].get("status")) in TERMINAL_REQUIREMENT_STATUSES for item in requirement_ids):
        return {"decision": "evidence_already_satisfied", "state": updated, "requirement_ids": requirement_ids}

    fingerprint = canonical_action_fingerprint(
        str(proposal.get("command") or proposal.get("code") or ""),
        cwd=cwd,
        requirement_ids=requirement_ids,
        environment=environment,
        context=context,
    )
    prior = (updated.get("actions") or {}).get(fingerprint)
    if isinstance(prior, Mapping) and str(prior.get("status")) == "succeeded":
        reused = deepcopy(updated)
        reused["duplicate_reuse_count"] = int(reused.get("duplicate_reuse_count") or 0) + 1
        reused["last_action_fingerprint"] = fingerprint
        reused["updated_at"] = _now()
        return {
            "decision": "duplicate_action_reused",
            "fingerprint": fingerprint,
            "observation": str(prior.get("observation") or ""),
            "state": evaluate_consultation_completion(reused),
        }
    if isinstance(prior, Mapping) and str(prior.get("status")) == "failed":
        reused = deepcopy(updated)
        reused["duplicate_reuse_count"] = int(reused.get("duplicate_reuse_count") or 0) + 1
        reused["no_progress_count"] = int(reused.get("no_progress_count") or 0) + 1
        reused["last_action_fingerprint"] = fingerprint
        reused["updated_at"] = _now()
        return {
            "decision": "duplicate_failed_action_suppressed",
            "fingerprint": fingerprint,
            "observation": str(prior.get("observation") or ""),
            "state": evaluate_consultation_completion(reused),
        }
    return {"decision": "allow", "fingerprint": fingerprint, "requirement_ids": requirement_ids, "state": updated}


def record_execution_observation(
    state: Mapping[str, Any],
    *,
    fingerprint: str,
    proposal: Mapping[str, Any],
    returncode: int,
    observation: str,
    provenance: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Commit one observation and evaluate whether it produced evidence progress."""
    updated = deepcopy(dict(state))
    requirements = updated.setdefault("requirements", {})
    requirement_ids = [_safe_id(str(item)) for item in (proposal.get("requirement_ids") or []) if str(item).strip()]
    changed: list[str] = []
    success = int(returncode) == 0
    for requirement_id in requirement_ids:
        requirement = requirements.get(requirement_id)
        if not isinstance(requirement, dict):
            continue
        previous = str(requirement.get("status") or "unresolved")
        requirement["status"] = "satisfied" if success else "failed"
        requirement.setdefault("satisfied_by", [])
        if success and fingerprint not in requirement["satisfied_by"]:
            requirement["satisfied_by"].append(fingerprint)
        requirement["evidence_provenance"] = dict(provenance or {})
        requirement["updated_at"] = _now()
        if previous != requirement["status"]:
            changed.append(requirement_id)

    action = {
        "fingerprint": fingerprint,
        "status": "succeeded" if success else "failed",
        "requirement_ids": requirement_ids,
        "purpose": str(proposal.get("purpose") or ""),
        "expected_evidence": proposal.get("expected_evidence"),
        "observation": str(observation or ""),
        "returncode": int(returncode),
        "provenance": dict(provenance or {}),
        "recorded_at": _now(),
    }
    updated.setdefault("actions", {})[fingerprint] = action
    updated.setdefault("evidence", []).append({
        "evidence_id": f"evidence:{fingerprint[:16]}",
        "requirement_ids": requirement_ids,
        "status": action["status"],
        "provenance": dict(provenance or {}),
        "recorded_at": action["recorded_at"],
    })
    updated["execute_call_count"] = int(updated.get("execute_call_count") or 0) + 1
    updated["no_progress_count"] = 0 if changed else int(updated.get("no_progress_count") or 0) + 1
    updated["last_progress_requirement_ids"] = changed
    updated["last_action_fingerprint"] = fingerprint
    updated["phase"] = "evaluate"
    updated["updated_at"] = _now()
    return evaluate_consultation_completion(updated)


def consultation_prompt_context(state: Mapping[str, Any]) -> str:
    """Render only the compact controller contract required by the Consultant."""
    payload = {
        "consultation_id": state.get("consultation_id"),
        "phase": state.get("phase"),
        "completion_decision": state.get("completion_decision"),
        "completion_reason": state.get("completion_reason"),
        "execute_call_count": state.get("execute_call_count", 0),
        "no_progress_count": state.get("no_progress_count", 0),
        "requirements": list((state.get("requirements") or {}).values()),
        "allowed_actions": ["finish", "ask"] if state.get("phase") == "finalize" else [
            "execute", "search", "load_workflow_template", "load_skill_doc", "load_reference", "load_script", "ask", "finish"
        ],
    }
    return "<consultation_controller>\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n</consultation_controller>"

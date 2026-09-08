"""Route-free Planner local-agent lifecycle and review receipt primitives.

The module owns protocol state only.  It never chooses a graph node and never
interprets scientific task meaning.  Planner actions and PlanReview validators
produce observations; the workflow adapter maps the resulting lifecycle to a
typed outcome and the graph owns the edge.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Sequence


PLANNER_LOOP_SCHEMA_VERSION = 1
PLANNER_REVIEW_PROTOCOL_VERSION = 1
DEFAULT_PLANNER_NO_PROGRESS_LIMIT = 2
_ROUTING_METADATA_KEYS = {
    "route", "next_step", "next_route", "target_route", "goto",
    "proposed_route", "effective_route", "dispatch_route",
    "route_resolution", "planner_full_replan", "required_next_action",
    "node_recommended_next_step", "pending_recovery_action",
    "resume_route_hint", "resolved_resume_node", "resume_step",
    "terminal_route", "graph_node", "graph_node_name", "source_graph_node",
    "destination_graph_node", "controller_transition",
    "controller_transition_metadata", "command_goto", "route_event_id",
}
_VOLATILE_PROGRESS_KEYS = {
    "submission_id", "observation_id", "receipt_id", "request_id", "turn_id",
    "invocation_id", "candidate_scope_id", "timestamp", "created_at",
    "updated_at", "observed_at", "reviewed_at",
}


def _semantic_protocol_value(value: Any) -> Any:
    """Strip graph authority and per-attempt IDs from semantic observations."""
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_protocol_value(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _ROUTING_METADATA_KEYS
            and str(key).strip().lower() not in _VOLATILE_PROGRESS_KEYS
        }
    if isinstance(value, list):
        return [_semantic_protocol_value(item) for item in value]
    return deepcopy(value)


class PlannerDisposition(str, Enum):
    CONTINUE = "continue"
    CONSULTATION_REQUIRED = "consultation_required"
    REVIEW_READY = "review_ready"
    REVIEW_ACCEPTED = "review_accepted"
    TERMINAL = "terminal"
    INVALID = "invalid"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def planner_loop_for_state(plan_state: Mapping[str, Any] | None) -> Dict[str, Any]:
    task_state = dict((plan_state or {}).get("task_state") or {})
    return deepcopy(dict(task_state.get("planner_loop") or {}))


def new_planner_loop_state(
    task_id: str,
    *,
    max_identical_no_progress: int = DEFAULT_PLANNER_NO_PROGRESS_LIMIT,
) -> Dict[str, Any]:
    return {
        "schema_version": PLANNER_LOOP_SCHEMA_VERSION,
        "task_id": str(task_id or "default"),
        "phase": "reasoning",
        "status": "active",
        "agent_disposition": PlannerDisposition.CONTINUE.value,
        "observations": [],
        "progress": {
            "last_fingerprint": "",
            "identical_no_progress_count": 0,
            "max_identical_no_progress": max(
                1, int(max_identical_no_progress or DEFAULT_PLANNER_NO_PROGRESS_LIMIT)
            ),
        },
        "updated_at": _now(),
    }


def _validated_loop(
    state: Mapping[str, Any] | None,
    *,
    task_id: str = "default",
) -> Dict[str, Any]:
    current = deepcopy(dict(state or {}))
    if not current:
        return new_planner_loop_state(task_id)
    if int(current.get("schema_version") or 0) != PLANNER_LOOP_SCHEMA_VERSION:
        raise ValueError("PLANNER_LOOP_SCHEMA_UNSUPPORTED")
    current.setdefault("task_id", str(task_id or "default"))
    current.setdefault("observations", [])
    current.setdefault("progress", {})
    return current


def record_planner_observation(
    state: Mapping[str, Any] | None,
    *,
    action_type: str,
    action: Mapping[str, Any] | None,
    observation: Mapping[str, Any] | None,
    candidate_fingerprint: str = "",
    semantic_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Record one local action result without deriving a workflow transition."""

    current = _validated_loop(state)
    action_name = str(action_type or "").strip().lower()
    if not action_name:
        raise ValueError("PLANNER_ACTION_TYPE_REQUIRED")
    action_value = deepcopy(dict(action or {}))
    observation_value = deepcopy(dict(observation or {}))
    semantic_state_value = deepcopy(dict(semantic_state or {}))
    fingerprint = _digest({
        "action_type": action_name,
        "action": _semantic_protocol_value(action_value),
        "observation": _semantic_protocol_value(observation_value),
        "candidate_fingerprint": str(candidate_fingerprint or ""),
        "semantic_state": _semantic_protocol_value(semantic_state_value),
    })
    progress = dict(current.get("progress") or {})
    prior = str(progress.get("last_fingerprint") or "")
    identical = int(progress.get("identical_no_progress_count") or 0)
    identical = identical + 1 if prior == fingerprint else 0
    limit = max(
        1,
        int(
            progress.get("max_identical_no_progress")
            or DEFAULT_PLANNER_NO_PROGRESS_LIMIT
        ),
    )
    progress.update({
        "last_fingerprint": fingerprint,
        "identical_no_progress_count": identical,
        "max_identical_no_progress": limit,
    })
    receipt = {
        "observation_id": "planner-observation-" + uuid.uuid4().hex[:20],
        "action_type": action_name,
        "action": action_value,
        "observation": observation_value,
        "candidate_fingerprint": str(candidate_fingerprint or ""),
        "semantic_state": semantic_state_value,
        "fingerprint": fingerprint,
        "observed_at": _now(),
    }
    observations = [
        deepcopy(dict(item))
        for item in current.get("observations") or []
        if isinstance(item, Mapping)
    ]
    observations.append(receipt)
    current.update({
        "phase": "observing",
        "status": "active",
        "agent_disposition": PlannerDisposition.CONTINUE.value,
        "observations": observations[-12:],
        "progress": progress,
        "last_observation": receipt,
        "updated_at": _now(),
    })
    if identical >= limit:
        diagnostic = {
            "source_node": "planner",
            "error_code": "PLANNER_AGENT_NO_PROGRESS",
            "detail": (
                "The same Planner action, observation, candidate, and semantic "
                "state repeated without durable progress."
            ),
            "semantic_state": {
                "phase": current["phase"],
                "status": current["status"],
                "action_type": action_name,
                "identical_no_progress_count": identical,
                "max_identical_no_progress": limit,
            },
        }
        current.update({
            "phase": "invalid",
            "status": "invalid",
            "agent_disposition": PlannerDisposition.INVALID.value,
            "terminal_diagnostic": diagnostic,
        })
    return current


def _plan_identity(state: Mapping[str, Any]) -> Dict[str, Any]:
    current = dict(state.get("current_plan") or {})
    steps = [
        deepcopy(dict(item))
        for item in state.get("steps") or []
        if isinstance(item, Mapping)
    ]
    semantic_hash = str(
        state.get("semantic_plan_hash")
        or state.get("plan_semantic_hash")
        or current.get("plan_semantic_hash")
        or _digest({"steps": steps})
    )
    return {
        "task_id": str(state.get("task_id") or "default"),
        "plan_id": str(state.get("plan_id") or current.get("plan_id") or ""),
        "plan_version": str(
            state.get("plan_version") or current.get("plan_version") or ""
        ),
        "plan_revision": int(
            state.get("plan_revision") or current.get("plan_revision") or 0
        ),
        "plan_semantic_hash": semantic_hash,
        "plan_snapshot_ref": str(
            current.get("plan_snapshot_ref") or state.get("plan_snapshot_ref") or ""
        ),
    }


def _receipt_matches_plan(
    receipt: Mapping[str, Any] | None,
    state: Mapping[str, Any],
) -> bool:
    value = dict(receipt or {})
    identity = _plan_identity(state)
    return bool(
        str(value.get("submission_id") or "")
        and value.get("agent_initiated") is True
        and str(value.get("action_type") or "") == "submit_plan_for_review"
        and int(value.get("protocol_version") or 0)
        == PLANNER_REVIEW_PROTOCOL_VERSION
        and str(value.get("task_id") or "") == identity["task_id"]
        and str(value.get("plan_id") or "") == identity["plan_id"]
        and str(value.get("plan_version") or "") == identity["plan_version"]
        and int(value.get("plan_revision") or 0) == identity["plan_revision"]
        and str(value.get("plan_semantic_hash") or "")
        == identity["plan_semantic_hash"]
    )


def record_plan_review_submission(
    plan_state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Attach an Agent-initiated, plan-identity-bound review submission."""

    updated = deepcopy(dict(plan_state or {}))
    identity = _plan_identity(updated)
    if not updated.get("steps") or identity["plan_revision"] <= 0:
        raise ValueError("PLAN_REVIEW_SUBMISSION_CANDIDATE_INVALID")
    seed = {**identity, "nonce": uuid.uuid4().hex}
    receipt = {
        "schema_version": 1,
        "protocol_version": PLANNER_REVIEW_PROTOCOL_VERSION,
        "submission_id": "plan-submission-" + hashlib.sha256(
            _canonical(seed).encode("utf-8")
        ).hexdigest()[:20],
        "action_type": "submit_plan_for_review",
        "agent_initiated": True,
        "status": "pending_review",
        **identity,
        "submitted_at": _now(),
    }
    task_state = dict(updated.get("task_state") or {})
    loop = _validated_loop(
        task_state.get("planner_loop"), task_id=identity["task_id"]
    )
    loop.update({
        "phase": "submitted_for_review",
        "status": "review_pending",
        "agent_disposition": PlannerDisposition.REVIEW_READY.value,
        "submission_receipt": deepcopy(receipt),
        "updated_at": _now(),
    })
    task_state["planner_loop"] = loop
    task_state["plan_review_submission"] = deepcopy(receipt)
    task_state.pop("accepted_plan_review", None)
    updated["task_state"] = task_state
    return updated


def plan_review_submission_readiness(
    plan_state: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    receipt = dict(task_state.get("plan_review_submission") or {})
    identity_matches = _receipt_matches_plan(receipt, state)
    ready = bool(
        identity_matches
        and str(receipt.get("status") or "") == "pending_review"
    )
    return {
        "ready": ready,
        "identity_matches": identity_matches,
        "reason_code": (
            "PLAN_REVIEW_SUBMISSION_COMMITTED"
            if ready
            else "PLAN_REVIEW_SUBMISSION_MISSING_OR_STALE"
        ),
        "submission_id": str(receipt.get("submission_id") or ""),
        "receipt": receipt,
    }


def review_evidence_from_issues(
    issues: Sequence[Mapping[str, Any]] | None,
) -> list[Dict[str, Any]]:
    """Project compact, machine-addressable review facts with no route hint."""

    evidence: list[Dict[str, Any]] = []
    for raw in issues or []:
        if not isinstance(raw, Mapping):
            continue
        issue = dict(raw)
        artifact = dict(issue.get("affected_artifact") or {})
        machine = dict(issue.get("machine_evidence") or {})
        consumers = [
            str(value)
            for value in (
                [issue.get("consumer_step_id") or machine.get("consumer_step_id")]
                if (issue.get("consumer_step_id") or machine.get("consumer_step_id"))
                else issue.get("affected_step_ids") or []
            )
            if str(value or "")
        ]
        row = {
            "error_code": str(issue.get("code") or "PLAN_REVIEW_INVALID"),
            "json_path": str(
                issue.get("field_path") or issue.get("repair_target") or "$"
            ),
            "expected": deepcopy(
                issue.get("expected_identity")
                or issue.get("expected_contract")
                or machine.get("expected_contract")
                or issue.get("expected_type")
                or issue.get("required_postcondition")
                or "valid plan contract"
            ),
            "actual": deepcopy(
                issue.get("observed_identity")
                or issue.get("actual_value")
                or machine.get("actual_value")
                or issue.get("received_type")
                or issue.get("machine_evidence")
                or "invalid or missing"
            ),
            "constraint_source": str(
                issue.get("constraint_source")
                or issue.get("source_ref")
                or issue.get("validator_id")
                or machine.get("validator_id")
                or "plan_contract"
            ),
            "message": str(issue.get("message") or issue.get("code") or ""),
            "owner": str(issue.get("owner") or "planner"),
            "resource": str(
                artifact.get("identity")
                or issue.get("artifact_id")
                or issue.get("affected_output")
                or ""
            ),
            "consumers": consumers,
            "candidate_producers": [
                str(value)
                for value in (
                    issue.get("producer_step_ids")
                    or machine.get("producer_step_ids")
                    or []
                )
                if str(value)
            ],
            "required_postcondition": str(
                issue.get("required_postcondition") or ""
            ),
            "violated_invariant": str(issue.get("violated_invariant") or ""),
        }
        evidence.append(_semantic_protocol_value(row))
    return evidence


def record_plan_review_rejection(
    plan_state: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]] | None,
) -> Dict[str, Any]:
    updated = deepcopy(dict(plan_state or {}))
    task_state = dict(updated.get("task_state") or {})
    receipt = dict(task_state.get("plan_review_submission") or {})
    evidence = review_evidence_from_issues(issues)
    if receipt:
        receipt.update({
            "status": "repair_required",
            "reviewed_at": _now(),
            "review_evidence": deepcopy(evidence),
        })
        task_state["plan_review_submission"] = receipt
    loop = record_planner_observation(
        task_state.get("planner_loop"),
        action_type="plan_review",
        action={"review": "compiled_plan"},
        observation={"accepted": False, "review_evidence": evidence},
        candidate_fingerprint=str(receipt.get("plan_semantic_hash") or ""),
        semantic_state=_plan_identity(updated),
    )
    loop.update({"phase": "review_observed", "status": "active"})
    task_state["planner_loop"] = loop
    task_state.pop("accepted_plan_review", None)
    updated["task_state"] = task_state
    return updated


def record_plan_review_acceptance(
    plan_state: Mapping[str, Any],
) -> Dict[str, Any]:
    updated = deepcopy(dict(plan_state or {}))
    task_state = dict(updated.get("task_state") or {})
    submission = dict(task_state.get("plan_review_submission") or {})
    approval = dict(updated.get("approval_record") or {})
    if not _receipt_matches_plan(submission, updated):
        raise ValueError("PLAN_REVIEW_SUBMISSION_MISSING_OR_STALE")
    if str(approval.get("status") or "") not in {"approved", "accepted"}:
        raise ValueError("PLAN_REVIEW_APPROVAL_RECEIPT_INVALID")
    accepted = {
        "schema_version": 1,
        "protocol_version": PLANNER_REVIEW_PROTOCOL_VERSION,
        "review_receipt_id": "plan-review-" + uuid.uuid4().hex[:20],
        "status": "accepted",
        "submission_id": str(submission.get("submission_id") or ""),
        "approval_id": str(approval.get("approval_id") or ""),
        "review_event_id": str(approval.get("review_event_id") or ""),
        **_plan_identity(updated),
        "accepted_at": _now(),
    }
    submission.update({
        "status": "accepted",
        "review_receipt_id": accepted["review_receipt_id"],
        "approval_id": accepted["approval_id"],
        "reviewed_at": accepted["accepted_at"],
    })
    loop = _validated_loop(
        task_state.get("planner_loop"), task_id=accepted["task_id"]
    )
    loop.update({
        "phase": "review_accepted",
        "status": "completed",
        "agent_disposition": PlannerDisposition.REVIEW_ACCEPTED.value,
        "accepted_review_receipt": deepcopy(accepted),
        "updated_at": _now(),
    })
    task_state["plan_review_submission"] = submission
    task_state["accepted_plan_review"] = accepted
    task_state["planner_loop"] = loop
    updated["task_state"] = task_state
    return updated


def plan_review_acceptance_readiness(
    plan_state: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    submission = dict(task_state.get("plan_review_submission") or {})
    accepted = dict(task_state.get("accepted_plan_review") or {})
    approval = dict(state.get("approval_record") or {})
    ready = bool(
        _receipt_matches_plan(submission, state)
        and str(submission.get("status") or "") == "accepted"
        and str(accepted.get("status") or "") == "accepted"
        and str(accepted.get("submission_id") or "")
        == str(submission.get("submission_id") or "")
        and str(accepted.get("review_receipt_id") or "")
        == str(submission.get("review_receipt_id") or "")
        and str(accepted.get("approval_id") or "")
        == str(approval.get("approval_id") or "")
        and str(approval.get("status") or "") in {"approved", "accepted"}
    )
    return {
        "ready": ready,
        "reason_code": (
            "PLAN_REVIEW_ACCEPTANCE_COMMITTED"
            if ready
            else "PLAN_REVIEW_ACCEPTANCE_MISSING_OR_STALE"
        ),
        "review_receipt_id": str(accepted.get("review_receipt_id") or ""),
        "submission_id": str(submission.get("submission_id") or ""),
        "receipt": accepted,
    }


def classify_planner_lifecycle(
    plan_state: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Classify Planner protocol facts without interpreting validation owners."""

    state = dict(plan_state or {})
    task_state = dict(state.get("task_state") or {})
    loop = planner_loop_for_state(state)
    diagnostic = dict(loop.get("terminal_diagnostic") or {})
    if diagnostic:
        return {
            "disposition": PlannerDisposition.INVALID.value,
            "reason_code": str(
                diagnostic.get("error_code") or "PLANNER_LOOP_INVALID"
            ),
            "detail": str(diagnostic.get("detail") or "")[:800],
            "semantic_state": {
                "phase": str(loop.get("phase") or ""),
                "status": str(loop.get("status") or ""),
            },
        }
    reconciliation = dict(task_state.get("planner_reconciliation") or {})
    if str(reconciliation.get("status") or "") in {
        "pending_consultant", "pending_consultant_classification"
    }:
        return {
            "disposition": PlannerDisposition.CONSULTATION_REQUIRED.value,
            "reason_code": "TASKSPEC_RECONCILIATION_REQUIRED",
        }
    accepted = plan_review_acceptance_readiness(state)
    if accepted["ready"]:
        return {
            "disposition": PlannerDisposition.REVIEW_ACCEPTED.value,
            "reason_code": accepted["reason_code"],
            "review_receipt_id": accepted["review_receipt_id"],
        }
    submission = plan_review_submission_readiness(state)
    if submission["ready"]:
        return {
            "disposition": PlannerDisposition.REVIEW_READY.value,
            "reason_code": submission["reason_code"],
            "submission_id": submission["submission_id"],
        }
    failure = dict(task_state.get("planning_failure") or {})
    planning_epoch = dict(task_state.get("planning_epoch") or {})
    observations = [
        dict(item)
        for item in loop.get("observations") or []
        if isinstance(item, Mapping)
    ]
    last_observation = dict(loop.get("last_observation") or {})
    if not last_observation and observations:
        last_observation = observations[-1]
    observation_fingerprint = str(last_observation.get("fingerprint") or "")
    planner_calls_total = int(
        planning_epoch.get("planner_calls_total") or 0
    )
    progress_facts: Dict[str, Any] = {}
    if observation_fingerprint:
        progress_facts["planner_observation"] = {
            "semantic_fingerprint": observation_fingerprint,
        }
    if str(failure.get("status") or "") in {
        "active", "pending", "unresolved", "repairing"
    }:
        progress_facts["planning_failure"] = {
            "issue_fingerprint": str(
                failure.get("issue_fingerprint") or ""
            ),
            "candidate_correction_signature": str(
                failure.get("candidate_correction_signature") or ""
            ),
            "validation_source": str(
                failure.get("validation_source") or ""
            ),
        }
    if state.get("steps"):
        progress_facts["plan_candidate"] = {
            "plan_semantic_hash": str(
                _plan_identity(state).get("plan_semantic_hash") or ""
            )
        }
    if progress_facts:
        progress_receipt_id = "planner-progress:" + _digest(progress_facts)
        return {
            "disposition": PlannerDisposition.CONTINUE.value,
            "reason_code": str(
                failure.get("validation_source")
                or (
                    "PLAN_SUBMISSION_REQUIRED"
                    if state.get("steps")
                    else "PLANNER_LOCAL_LOOP_ACTIVE"
                )
            ),
            "attempt_ordinal": max(
                1,
                planner_calls_total,
                len(observations),
            ),
            "progress_receipt_id": progress_receipt_id,
            "progress_kind": "+".join(sorted(progress_facts)),
        }
    return {
        "disposition": PlannerDisposition.INVALID.value,
        "reason_code": "PLANNER_PROGRESS_RECEIPT_MISSING",
        "detail": (
            "Planner returned without a durable planning epoch, local-loop "
            "observation, candidate Plan, review submission, or reconciliation "
            "receipt. A schema-2 self-loop cannot be inferred from an empty "
            "planning state."
        ),
        "semantic_state": {
            "phase": str(loop.get("phase") or ""),
            "status": str(loop.get("status") or ""),
        },
    }

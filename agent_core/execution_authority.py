"""Completion eligibility from approved execution lineage, never script words.

This predicate grants no filesystem permission and does not replace approval
or evidence validation. It lets those existing authorities distinguish a
formal Plan check from a temporary Recovery diagnostic with the same purpose.
"""
from typing import Any, Mapping


def is_recovery_diagnostic_execution(plan_state: Mapping[str, Any], evidence: Mapping[str, Any],
                                     *, action: Mapping[str, Any] | None = None) -> bool:
    provenance = dict(plan_state.get("provenance_state") or {})
    attempt = dict((provenance.get("attempts") or {}).get(str(evidence.get("attempt_id") or "")) or {})
    bound = dict((provenance.get("actions") or {}).get(str(evidence.get("action_id") or attempt.get("action_id") or "")) or {})
    normalized = dict(bound.get("normalized_action") or {})
    for value in (dict(action or {}), normalized, evidence):
        envelope = dict(value.get("recovery_execution_envelope") or {})
        if (envelope.get("recovery_strategy") or envelope.get("strategy")) == "RUN_DIAGNOSTIC_PROBE" or envelope.get("diagnostic_request") or value.get("diagnostic_request"):
            return True
        decision = str(value.get("recovery_decision_id") or value.get("decision_id") or "")
        receipt = (plan_state.get("task_state") or {}).get("typed_recovery_receipts", {}).get(decision, {})
        if (receipt.get("directive") or {}).get("action") == "run_diagnostic_probe":
            return True
    return False


def is_approved_step_execution(plan_state: Mapping[str, Any], evidence: Mapping[str, Any],
                               *, action: Mapping[str, Any] | None = None) -> bool:
    provenance = dict(plan_state.get("provenance_state") or {})
    attempt = dict((provenance.get("attempts") or {}).get(str(evidence.get("attempt_id") or "")) or {})
    action_id = str(evidence.get("action_id") or attempt.get("action_id") or "")
    bound = dict((provenance.get("actions") or {}).get(action_id) or {})
    if not bound or str(bound.get("status") or "") == "revoked" or bound.get("revoked_at"):
        return False
    normalized = dict(bound.get("normalized_action") or {})
    candidate = dict(action or normalized)
    if is_recovery_diagnostic_execution(plan_state, evidence, action=candidate):
        return False
    for value in (candidate, normalized, evidence):
        envelope = dict(value.get("recovery_execution_envelope") or {})
        if envelope and str(envelope.get("recovery_strategy") or envelope.get("strategy") or "") == "RUN_DIAGNOSTIC_PROBE":
            return False
        if value.get("diagnostic_request") or envelope.get("diagnostic_request"):
            return False
    decision = str(candidate.get("recovery_decision_id") or candidate.get("decision_id") or "")
    if decision:
        receipt = (plan_state.get("task_state") or {}).get("typed_recovery_receipts", {}).get(decision, {})
        if (receipt.get("directive") or {}).get("action") == "run_diagnostic_probe":
            return False
    step_id = str(evidence.get("step_id") or attempt.get("step_id") or bound.get("step_id") or "")
    if not step_id or str(bound.get("step_id") or "") != step_id:
        return False
    steps = plan_state.get("steps") or (plan_state.get("current_plan") or {}).get("plan_spec", {}).get("steps") or []
    if not any(isinstance(row, Mapping) and str(row.get("step_id") or "") == step_id for row in steps):
        return False
    for key in ("plan_id", "plan_version"):
        expected = str(plan_state.get(key) or "")
        if not expected or str(bound.get(key) or "") != expected:
            return False
        for value in (attempt, evidence):
            if value.get(key) and str(value[key]) != expected:
                return False
    approval = dict(plan_state.get("current_approval") or {})
    approved = (str(approval.get("status") or "") == "approved"
                or str(plan_state.get("approval_status") or "") == "approved")
    if not approved:
        return False
    expected_snapshot = str((plan_state.get("current_plan") or {}).get("plan_snapshot_id") or "")
    observed_snapshot = str(candidate.get("plan_snapshot_id") or candidate.get("approved_plan_snapshot_id") or "")
    if observed_snapshot and expected_snapshot and observed_snapshot != expected_snapshot:
        return False
    return True

"""Scoped Planner response facts and one remediation inside the existing epoch.

An occurrence is an accounted call result.  Its identity is independent from
failure meaning and from the normalized remediation that may follow it.
All writes use PlanState's existing CAS; an unsettled call is never refunded.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping


class PlannerCallError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail

    def to_diagnostic(self) -> dict[str, Any]:
        return {"error_code": self.code, "error_owner": "planner", "detail": self.detail}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _epoch(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict((state.get("task_state") or {}).get("planning_epoch") or {})


def _check_scope(state: Mapping[str, Any], scope: Mapping[str, Any]) -> None:
    current = state.get("current_task") or {}
    revision = int(current.get("task_revision") or state.get("task_revision") or 0)
    if (str(state.get("task_id")) != str(scope.get("task_id"))
        or revision != int(scope.get("task_revision") or 0)
        or str(_epoch(state).get("planning_epoch_id")) != str(scope.get("planning_epoch_id"))):
        raise PlannerCallError("PLANNER_CALL_STALE_SCOPE", "Planner result/claim does not match the current task revision and planning epoch.")


def unsettled_call(state: Mapping[str, Any]) -> dict[str, Any]:
    claim = dict(_epoch(state).get("call_claim") or {})
    return claim if claim.get("status") in {"dispatched", "response_received"} else {}


def remediation_signature(kind: str, policy: Mapping[str, Any]) -> str:
    # Only operational policy facts count. Source text, timestamps, call IDs,
    # random extras and trace-observation status cannot create a new strategy.
    return "planner-remediation:" + _digest({
        "version": 1, "kind": kind,
        "output_key": policy.get("wire_output_parameter_key"),
        "output_limit": policy.get("wire_output_parameter_value", policy.get("requested_output_limit")),
        "policy_version": policy.get("output_policy_version"),
        "representation": "compact_complete_json_v1",
    })


def select_length_remediation(state: Mapping[str, Any], initial_policy: Mapping[str, Any],
                              recovery_policy: Mapping[str, Any]) -> dict[str, Any]:
    epoch = _epoch(state)
    recovery = dict(epoch.get("length_recovery") or {})
    used = int(recovery.get("corrective_calls_used") or 0)
    local_limit = min(1, max(0, int(initial_policy.get("max_length_corrective_calls_per_epoch", 1))))
    if used >= local_limit:
        return {"status": "unavailable", "reason_code": "PLANNER_LENGTH_RECOVERY_EXHAUSTED",
                "detail": "The epoch's bounded length remediation has already been consumed; no further automatic regeneration is authorized."}
    total_limit = int(((state.get("task_state") or {}).get("budgets") or {}).get("max_planner_calls_per_epoch", 4))
    if int(epoch.get("planner_calls_total") or 0) >= total_limit:
        return {"status": "unavailable", "reason_code": "PLANNER_LENGTH_RECOVERY_UNAVAILABLE",
                "detail": "The existing Planner call budget has no remaining slot for a length remediation."}
    old = initial_policy.get("requested_output_limit")
    new = recovery_policy.get("requested_output_limit")
    increase = (isinstance(new, int) and (not isinstance(old, int) or new > old)
                and bool(recovery_policy.get("wire_output_parameter_key"))
                and recovery_policy.get("apply_output_binding") is True
                and recovery_policy.get("status") != "unavailable")
    kind = "increase_output_limit" if increase else "compact_json"
    policy = deepcopy(dict(recovery_policy if increase else initial_policy))
    signature = remediation_signature(kind, policy)
    prior = list(recovery.get("history") or [])
    if recovery.get("compact_attempted") or any(x.get("signature") == signature for x in prior):
        return {"status": "unavailable", "reason_code": "PLANNER_LENGTH_RECOVERY_UNAVAILABLE",
                "detail": "No untried effective length remediation remains in the current epoch."}
    return {"status": "available", "kind": kind, "signature": signature,
            "output_policy": policy, "representation": "compact_complete_json_v1",
            "source_occurrence_id": str((((state.get("task_state") or {}).get("planning_failure") or {}).get("occurrence_receipt") or {}).get("id") or "")}


def planner_call_limit(state: Mapping[str, Any]) -> int:
    return min(4, max(0, int(((state.get("task_state") or {}).get("budgets") or {}).get("max_planner_calls_per_epoch", 4))))


def claim_planner_call(data_path: str, task_id: str, expected_state: Mapping[str, Any],
                       scope: Mapping[str, Any], output_policy: Mapping[str, Any],
                       remediation: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    from agent_core.plan_tracker import load_plan_state, save_plan_state

    state = deepcopy(dict(expected_state))
    _check_scope(state, scope)
    epoch = _epoch(state)
    if epoch.get("status") not in {"active", "candidate_rejected"}:
        raise PlannerCallError("PLANNER_CALL_EPOCH_CLOSED", "The planning epoch is not active; it cannot authorize another call.")
    if unsettled_call(state):
        raise PlannerCallError("PLANNER_CALL_RESULT_UNKNOWN", "A prior claimed Planner call has no accepted durable result. Its budget is not refunded and the call will not be dispatched again.")
    if int(epoch.get("planner_calls_total") or 0) >= planner_call_limit(state):
        raise PlannerCallError("PLANNING_EPOCH_EXHAUSTED", "The hard total Planner request budget, including the first request, is exhausted; no fifth dispatch is authorized.")
    task_limit = ((state.get("task_state") or {}).get("budgets") or {}).get("max_planner_output_tokens")
    if task_limit is not None:
        requested = output_policy.get("requested_output_limit")
        if not isinstance(requested, int) or requested > int(task_limit):
            raise PlannerCallError("PLANNER_OUTPUT_TASK_LIMIT_UNENFORCEABLE", "The effective output policy does not satisfy the current durable task output limit.")
    remediation = deepcopy(dict(remediation or {}))
    if remediation:
        failure = dict((state.get("task_state") or {}).get("planning_failure") or {})
        authorized = dict(failure.get("remediation_authorization") or {})
        recovery = deepcopy(dict(epoch.get("length_recovery") or {}))
        if (authorized.get("status") != "available"
            or not authorized.get("source_occurrence_id")
            or authorized.get("source_occurrence_id") != (failure.get("occurrence_receipt") or {}).get("id")
            or authorized.get("signature") != remediation.get("signature")
            or remediation.get("signature") != remediation_signature(str(remediation.get("kind")), output_policy)
            or int(recovery.get("corrective_calls_used") or 0) >= 1
            or int(epoch.get("planner_calls_total") or 0) >= int(((state.get("task_state") or {}).get("budgets") or {}).get("max_planner_calls_per_epoch", 4))):
            raise PlannerCallError("PLANNER_LENGTH_RECOVERY_UNAVAILABLE", "The durable length remediation authorization is absent, consumed, or does not match the effective policy.")
        recovery["schema_version"] = 1
        recovery["corrective_calls_used"] = int(recovery.get("corrective_calls_used") or 0) + 1
        recovery["compact_attempted"] = True
        epoch["length_recovery"] = recovery
    ordinal = int(epoch.get("planner_calls_total") or 0) + 1
    claim = {"schema_version": 1, "scope": deepcopy(dict(scope)), "ordinal": ordinal,
             "output_policy": deepcopy(dict(output_policy)), "remediation": remediation,
             "status": "dispatched"}
    claim["claim_id"] = "planner-call:" + _digest({"scope": claim["scope"], "ordinal": ordinal})
    if remediation:
        applied = {**deepcopy(remediation), "claim_id": claim["claim_id"], "status": "dispatched", "ordinal": ordinal}
        epoch["length_recovery"].setdefault("history", []).append(applied)
    epoch["planner_calls_total"] = ordinal
    epoch["call_claim"] = claim
    state.setdefault("task_state", {})["planning_epoch"] = epoch
    # This CAS is the sole authorization/charge for this logical call. A racing
    # save raises the existing conflict; callers cannot turn it into a retry.
    save_plan_state(data_path, task_id, state)
    durable = load_plan_state(data_path, task_id)
    return durable, deepcopy(claim)


def response_integrity(response: Any, *, scope: Mapping[str, Any], claim: Mapping[str, Any],
                       policy: Mapping[str, Any], parser_diagnostic: Mapping[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(getattr(response, "response_metadata", {}) or {})
    attempt = dict(metadata.get("biomind_provider_attempt") or {})
    # These are SDK/system metadata of this returned response, never text
    # parsed out of the candidate or the last observer of another task.
    finish = str(attempt.get("finish_reason") or metadata.get("finish_reason") or "UNKNOWN")
    usage = attempt.get("provider_usage") or getattr(response, "usage_metadata", None)
    usage_status = str(attempt.get("usage_status") or ("provider_reported" if isinstance(getattr(response, "usage_metadata", None), Mapping) else "UNKNOWN"))
    raw = str(getattr(response, "content", "") or "")
    return {
        "schema_version": 1, "scope": deepcopy(dict(scope)),
        "claim_id": str(claim.get("claim_id") or ""),
        "call_id": str(attempt.get("call_id") or ""),
        "provider_attempt_id": str(attempt.get("attempt_id") or attempt.get("provider_attempt_id") or ""),
        "provider_request_id": str(attempt.get("provider_request_id") or getattr(response, "id", "") or ""),
        "raw_digest": hashlib.sha256(raw.encode()).hexdigest(), "raw_ref": "",
        "finish_reason": finish,
        "reported_usage": deepcopy(usage) if isinstance(usage, Mapping) else {"status": "UNKNOWN"},
        "usage_source": usage_status if isinstance(usage, Mapping) else "UNKNOWN",
        "usage_status": usage_status,
        "transport_status": str(attempt.get("state") or "UNKNOWN"),
        "content_status": "TRUNCATED" if finish == "length" else "NORMAL_END" if finish == "stop" else "UNKNOWN",
        "parser_diagnostic": deepcopy(dict(parser_diagnostic or {"status": "not_run"})),
        "resolved_output_policy": deepcopy(dict(metadata.get("planner_output_policy") or policy)),
        "attempted_remediation": deepcopy(dict(claim.get("remediation") or {})),
    }


def complete_planner_claim(state: dict[str, Any], claim: Mapping[str, Any], integrity: Mapping[str, Any],
                           *, outcome: str) -> None:
    if not claim:
        return
    _check_scope(state, claim["scope"])
    if dict(integrity.get("scope") or {}) != dict(claim["scope"]):
        raise PlannerCallError("PLANNER_RESPONSE_SCOPE_MISMATCH", "The response evidence is not bound to the claimed invocation and candidate scope.")
    epoch = _epoch(state)
    current = dict(epoch.get("call_claim") or {})
    if current.get("claim_id") != claim.get("claim_id"):
        raise PlannerCallError("PLANNER_CALL_STALE_CLAIM", "A different call owns the current durable Planner claim.")
    if current.get("status") == "result_recorded":
        if current.get("raw_digest") == integrity.get("raw_digest"):
            return  # Same accepted result; no new occurrence or charge.
        raise PlannerCallError("PLANNER_CALL_RESULT_CONFLICT", "A recorded call result cannot be replaced by different response bytes.")
    current.update({"status": "result_recorded", "outcome": outcome,
                    "raw_digest": integrity.get("raw_digest"), "response_integrity": deepcopy(dict(integrity))})
    epoch["call_claim"] = current
    for item in (epoch.get("length_recovery") or {}).get("history", []):
        if item.get("claim_id") == claim.get("claim_id"):
            item.update({"status": "result_recorded", "outcome": outcome, "finish_reason": integrity.get("finish_reason")})
    state.setdefault("task_state", {})["planning_epoch"] = epoch


def attach_rejection_facts(state: dict[str, Any], integrity: Mapping[str, Any],
                            claim: Mapping[str, Any], remediation: Mapping[str, Any] | None = None) -> None:
    from agent_core.plan_tracker import terminalize_planning_failure

    failure = state.setdefault("task_state", {}).setdefault("planning_failure", {})
    complete_planner_claim(state, claim, integrity, outcome="candidate_rejected")
    occurrence = {"schema_version": 1, "scope": deepcopy(dict(integrity.get("scope") or {})),
                  "claim_id": str(claim.get("claim_id") or ""), "ordinal": int(claim.get("ordinal") or failure.get("attempt") or 0),
                  "call_id": str(integrity.get("call_id") or ""),
                  "failure_attempt": int(failure.get("attempt") or 0)}
    occurrence["id"] = "planner-occurrence:" + _digest(occurrence)
    failure["occurrence_receipt"] = occurrence
    failure["response_integrity"] = deepcopy(dict(integrity))
    failure["semantic_failure_fingerprint"] = _digest({
        "issue_fingerprint": failure.get("issue_fingerprint"),
        "candidate_correction_signature": failure.get("candidate_correction_signature"),
        "finish_reason": integrity.get("finish_reason", "UNKNOWN"),
    })
    failure["effective_remediation_signature"] = str((claim.get("remediation") or {}).get("signature") or "")
    if integrity.get("finish_reason") == "length":
        recovery = state["task_state"]["planning_epoch"].setdefault("length_recovery", {"schema_version": 1})
        recovery.setdefault("first_truncation", deepcopy(dict(integrity)))
    if int(failure.get("attempt") or 0) == 1 and (failure.get("primary_failure") or {}).get("status") == "observed":
        failure["primary_failure"]["response_integrity"] = deepcopy(dict(integrity))
        failure["primary_failure"]["occurrence_receipt"] = deepcopy(occurrence)
    if remediation is not None:
        failure["remediation_authorization"] = deepcopy(dict(remediation))
        if failure["remediation_authorization"].get("status") == "available":
            failure["remediation_authorization"]["source_occurrence_id"] = occurrence["id"]
    terminal = None
    if integrity.get("finish_reason") == "length" and (remediation or {}).get("status") != "available":
        terminal = {"error_code": str((remediation or {}).get("reason_code") or "PLANNER_LENGTH_RECOVERY_UNAVAILABLE"),
                    "detail": str((remediation or {}).get("detail") or "No authorized effective length remediation is available."), "error_owner": "planner"}
    elif int(failure.get("repeat_count") or 0) >= 1 and (remediation or {}).get("status") != "available":
        terminal = {"error_code": "PLANNER_REPEATED_FAILURE", "error_owner": "planner",
                    "detail": "A new accounted Planner call repeated the same semantic rejection without an unused effective remediation. This is repeated failure, not replay of the previous request."}
    if terminal is None and int(_epoch(state).get("planner_calls_total") or 0) >= planner_call_limit(state):
        terminal = {"error_code": "PLANNING_EPOCH_EXHAUSTED", "error_owner": "planner",
                    "detail": "The last allowed Planner response was rejected. The budget is exhausted; no further request is authorized."}
    if terminal:
        updated = terminalize_planning_failure(state,
            planning_epoch_id=str(failure.get("planning_epoch_id") or ""),
            task_spec_revision_value=int(failure.get("task_spec_revision") or 0), terminal_diagnostic=terminal)
        state.clear()
        state.update(updated)


def length_feedback(integrity: Mapping[str, Any], authorization: Mapping[str, Any]) -> dict[str, Any]:
    policy = dict(integrity.get("resolved_output_policy") or {})
    return {
        "finish_reason": str(integrity.get("finish_reason") or "UNKNOWN"),
        "reported_usage": deepcopy(integrity.get("reported_usage") or {"status": "UNKNOWN"}),
        "previous_request_output_limit": policy.get("wire_output_parameter_value", "UNKNOWN"),
        "previous_wire_observation_status": policy.get("wire_observation_status", "UNKNOWN"),
        "selected_remediation": {k: deepcopy(authorization[k]) for k in ("status", "kind", "reason_code", "detail") if k in authorization},
        "next_local_output_limit": (authorization.get("output_policy") or {}).get("requested_output_limit", "UNKNOWN"),
        "instructions": "The preceding generation ended with finish_reason=length and was not accepted as a reliable complete candidate. Generate one complete candidate from the beginning; do not continue or splice the fragment. Keep every required sample, stage, dependency, constraint, validation and deliverable. Use compact JSON: shorten explanatory prose/whitespace and omit only fields the current protocol explicitly makes optional. A local output setting does not guarantee server acceptance or successful generation.",
    }

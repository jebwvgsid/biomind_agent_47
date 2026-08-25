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

from agent_core.task_spec import (
    apply_declarative_task_spec_rules,
    apply_task_spec_mutation_proposal,
    project_task_spec_to_renderer,
    task_semantic_hash,
    task_spec_is_initialized,
    task_spec_revision,
    validate_task_spec_output_contracts,
)
from agent_core.skill_discovery import skill_discovery_progress_projection


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

FINALIZE_PROTOCOL_SCHEMA_VERSION = 3
FINALIZE_PROTOCOL_V4_SCHEMA_VERSION = 4
FINALIZE_PROTOCOL_STATUSES = {
    "ready",
    "invoking",
    "repair_pending",
    "needs_user",
    "compiled",
    "blocked_internal_contract",
    "blocked_internal_provider",
}
FINALIZE_PROVIDER_STATUSES = {
    "not_called",
    "in_flight",
    "succeeded",
    "transient_failed",
    "fatal_failed",
    "outcome_unknown",
}
FINALIZE_RESULT_PROTOCOL_STATUSES = {
    "not_validated",
    "valid",
    "wire_invalid",
    "field_invalid",
    "needs_user",
    "blocked",
}
FINALIZE_CALL_KINDS = {"initial", "transport_retry", "wire_repair", "field_repair"}
FINALIZE_V4_CALL_KINDS = {
    "decision_batch",
    "length_repair",
    "wire_repair",
    "field_repair",
    "transport_retry",
}
FINALIZE_V4_PROVIDER_STATUSES = {
    "not_called",
    "in_flight",
    "succeeded",
    "transient_failed",
    "fatal_failed",
    "outcome_unknown",
}
FINALIZE_V4_COMPLETION_INTEGRITIES = {
    "not_called",
    "in_flight",
    "complete",
    "truncated",
    "content_filtered",
    "missing",
    "unknown",
}
FINALIZE_ERROR_OWNERS = {
    "controller_model_protocol",
    "controller",
    "controller_storage",
    "provider",
    "controller_provider",
    "user_semantic",
}
_FINALIZE_PROVIDER_TERMINAL_OWNERS = {"provider", "controller_provider"}
_RAW_RESPONSE_KEYS = {
    "raw_response",
    "raw_message",
    "assistant_content",
    "response_content",
    "exact_response",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_.:-]+", "-", str(value or "").strip().lower()).strip("-") or "item"


def _contains_raw_response(value: Any) -> bool:
    """Reject exact assistant content at the graph-state boundary."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in _RAW_RESPONSE_KEYS:
                return True
            if _contains_raw_response(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_raw_response(item) for item in value)
    return False


def _new_finalize_repair(
    *,
    max_wire_repair_attempts: int = 2,
    max_field_repair_attempts: int = 2,
) -> Dict[str, Any]:
    return {
        "repair_kind": "none",
        "error_code": "",
        "wire_repair_attempt": 0,
        "max_wire_repair_attempts": max(0, int(max_wire_repair_attempts)),
        "field_repair_attempt": 0,
        "max_field_repair_attempts": max(0, int(max_field_repair_attempts)),
        "initial_canonical_request_hash": "",
        "original_response_artifact_ref": "",
        "latest_response_artifact_ref": "",
        "latest_response_excerpt": "",
        "response_excerpt_truncated": False,
        "original_response_content_hash": "",
        "latest_response_content_hash": "",
        "original_payload": None,
        "base_payload": None,
        "payload_hash": "",
        "validation_errors": [],
        "last_patch_protocol_errors": [],
        "errors_by_field": {},
        "allowed_fields_by_index": {},
        "allowed_payload_fields": [],
    }


def new_finalize_protocol(
    finalize_context_id: str,
    base_contract_hash: str,
    *,
    wire_schema_version: int = 2,
    max_wire_repair_attempts: int = 2,
    max_field_repair_attempts: int = 2,
) -> Dict[str, Any]:
    """Build the schema-v3 durable finalize protocol state.

    The exact provider response is deliberately absent. Only a protected
    artifact reference, an exact content hash, and a bounded redacted excerpt
    may enter graph state.
    """
    context_id = str(finalize_context_id or "").strip()
    contract_hash = str(base_contract_hash or "").strip()
    if not context_id or not contract_hash:
        raise ValueError("finalize_context_id and base_contract_hash are required")
    now = _now()
    return {
        "schema_version": FINALIZE_PROTOCOL_SCHEMA_VERSION,
        "status": "ready",
        "finalize_context_id": context_id,
        "wire_schema_version": int(wire_schema_version),
        "base_contract_hash": contract_hash,
        "provider_status": "not_called",
        "protocol_status": "not_validated",
        "terminal_error_code": None,
        "terminal_owner": None,
        "pending_ambiguity_id": None,
        "superseded_by_task_spec_revision": None,
        "active_canonical_request_hash": "",
        "attempts": [],
        "repair": _new_finalize_repair(
            max_wire_repair_attempts=max_wire_repair_attempts,
            max_field_repair_attempts=max_field_repair_attempts,
        ),
        "created_at": now,
        "updated_at": now,
    }


def initialize_finalize_protocol(
    state: Mapping[str, Any],
    *,
    finalize_context_id: str,
    base_contract_hash: str,
    wire_schema_version: int = 2,
    max_wire_repair_attempts: int = 2,
    max_field_repair_attempts: int = 2,
    replace: bool = False,
) -> Dict[str, Any]:
    """Initialize the sole nested protocol owner using copy-on-write semantics."""
    updated = deepcopy(dict(state))
    current = updated.get("finalize_protocol")
    if isinstance(current, Mapping) and current:
        same_binding = (
            str(current.get("finalize_context_id") or "") == str(finalize_context_id or "")
            and str(current.get("base_contract_hash") or "") == str(base_contract_hash or "")
            and int(current.get("wire_schema_version") or 0) == int(wire_schema_version)
        )
        if same_binding:
            return updated
        if not replace:
            raise ValueError("An active finalize protocol cannot be rebound in place")
    updated["finalize_protocol"] = new_finalize_protocol(
        finalize_context_id,
        base_contract_hash,
        wire_schema_version=wire_schema_version,
        max_wire_repair_attempts=max_wire_repair_attempts,
        max_field_repair_attempts=max_field_repair_attempts,
    )
    # A fresh context never inherits a question or either repair budget.
    updated["pending_human_question"] = None
    updated.pop("consultant_contract_repair", None)
    updated["updated_at"] = _now()
    return updated


def _active_finalize_protocol(state: Mapping[str, Any]) -> Dict[str, Any]:
    protocol = state.get("finalize_protocol")
    if not isinstance(protocol, Mapping) or not protocol:
        raise ValueError("consultation_state.finalize_protocol is not initialized")
    if int(protocol.get("schema_version") or 0) != FINALIZE_PROTOCOL_SCHEMA_VERSION:
        raise ValueError("finalize protocol must be migrated to schema version 3")
    return deepcopy(dict(protocol))


def _attempt_index(protocol: Mapping[str, Any], attempt_id: str) -> int:
    matches = [
        index
        for index, attempt in enumerate(protocol.get("attempts") or [])
        if str((attempt or {}).get("attempt_id") or "") == str(attempt_id or "")
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one finalize attempt for {attempt_id!r}")
    return matches[0]


def _sync_finalize_summaries(protocol: Dict[str, Any]) -> None:
    attempts = list(protocol.get("attempts") or [])
    if not attempts:
        protocol["provider_status"] = "not_called"
        protocol["protocol_status"] = "not_validated"
        return
    latest = attempts[-1]
    protocol["provider_status"] = str(latest.get("provider_status") or "not_called")
    protocol["protocol_status"] = str(latest.get("protocol_status") or "not_validated")


def _set_finalize_block(
    protocol: Dict[str, Any],
    *,
    error_code: str,
    error_owner: str,
    provider_block: Optional[bool] = None,
) -> None:
    owner = str(error_owner or "controller")
    if owner not in FINALIZE_ERROR_OWNERS:
        raise ValueError(f"Unsupported finalize error owner: {owner}")
    is_provider = owner in _FINALIZE_PROVIDER_TERMINAL_OWNERS if provider_block is None else bool(provider_block)
    protocol["status"] = "blocked_internal_provider" if is_provider else "blocked_internal_contract"
    protocol["terminal_error_code"] = str(error_code or "FINALIZE_REPAIR_EXHAUSTED")
    protocol["terminal_owner"] = owner
    protocol["pending_ambiguity_id"] = None
    if not is_provider and protocol.get("attempts"):
        protocol["attempts"][-1]["protocol_status"] = "blocked"
        protocol["attempts"][-1]["error_code"] = protocol["terminal_error_code"]
        protocol["attempts"][-1]["error_owner"] = owner
    _sync_finalize_summaries(protocol)


def _validate_finalize_binding(protocol: Mapping[str, Any], decision: Mapping[str, Any]) -> None:
    supplied_context = str(decision.get("finalize_context_id") or "")
    supplied_hash = str(decision.get("base_contract_hash") or "")
    if supplied_context and supplied_context != str(protocol.get("finalize_context_id") or ""):
        raise ValueError("Finalize decision context ID does not match the active context")
    if supplied_hash and supplied_hash != str(protocol.get("base_contract_hash") or ""):
        raise ValueError("Finalize decision base contract hash does not match the active context")


def _record_request_transition(protocol: Dict[str, Any], decision: Mapping[str, Any]) -> None:
    attempt_id = str(decision.get("attempt_id") or "").strip()
    call_id = str(decision.get("call_id") or "").strip()
    call_kind = str(decision.get("call_kind") or "initial").strip()
    request_hash = str(
        decision.get("canonical_request_hash")
        or decision.get("request_semantic_hash")
        or ""
    ).strip()
    if not attempt_id or not call_id or not request_hash:
        raise ValueError("request_started requires attempt_id, call_id, and canonical_request_hash")
    if call_kind not in FINALIZE_CALL_KINDS:
        raise ValueError(f"Unsupported finalize call kind: {call_kind}")
    attempts = protocol.setdefault("attempts", [])
    if any(str(item.get("attempt_id") or "") == attempt_id for item in attempts):
        raise ValueError(f"Duplicate finalize attempt ID: {attempt_id}")
    if any(str(item.get("call_id") or "") == call_id for item in attempts):
        raise ValueError(f"Duplicate finalize call ID: {call_id}")

    previous_same_hash = [
        item for item in attempts
        if str(item.get("canonical_request_hash") or "") == request_hash
    ]
    safe_transport_retry = (
        call_kind == "transport_retry"
        and len([item for item in previous_same_hash if item.get("call_kind") == "transport_retry"]) < 1
        and bool(previous_same_hash)
        and str(previous_same_hash[-1].get("provider_status") or "") == "transient_failed"
        and bool(previous_same_hash[-1].get("retry_safe"))
        and not str(previous_same_hash[-1].get("response_artifact_ref") or "")
    )
    if previous_same_hash and not safe_transport_retry:
        attempts.append({
            "attempt_id": attempt_id,
            "call_id": call_id,
            "call_kind": call_kind,
            "canonical_request_hash": request_hash,
            "provider_status": "not_called",
            "response_content_hash": "",
            "response_artifact_ref": "",
            "protocol_status": "blocked",
            "error_code": "FINALIZE_IDENTICAL_REQUEST_BLOCKED",
            "error_owner": "controller",
            "started_at": _now(),
            "completed_at": _now(),
        })
        protocol["active_canonical_request_hash"] = request_hash
        _set_finalize_block(
            protocol,
            error_code="FINALIZE_IDENTICAL_REQUEST_BLOCKED",
            error_owner="controller",
        )
        return

    repair = protocol["repair"]
    if call_kind == "wire_repair":
        next_attempt = int(repair.get("wire_repair_attempt") or 0) + 1
        if next_attempt > int(repair.get("max_wire_repair_attempts") or 0):
            _set_finalize_block(
                protocol,
                error_code="FINALIZE_REPAIR_EXHAUSTED",
                error_owner="controller",
            )
            return
        repair["wire_repair_attempt"] = next_attempt
        repair["repair_kind"] = "wire_format"
    elif call_kind == "field_repair":
        next_attempt = int(repair.get("field_repair_attempt") or 0) + 1
        if next_attempt > int(repair.get("max_field_repair_attempts") or 0):
            _set_finalize_block(
                protocol,
                error_code="FINALIZE_REPAIR_EXHAUSTED",
                error_owner="controller",
            )
            return
        repair["field_repair_attempt"] = next_attempt
        repair["repair_kind"] = "field_patch"
    elif call_kind == "initial" and not repair.get("initial_canonical_request_hash"):
        repair["initial_canonical_request_hash"] = request_hash

    attempts.append({
        "attempt_id": attempt_id,
        "call_id": call_id,
        "call_kind": call_kind,
        "canonical_request_hash": request_hash,
        "provider_status": "in_flight",
        "response_content_hash": "",
        "response_artifact_ref": "",
        "protocol_status": "not_validated",
        "error_code": "",
        "error_owner": "",
        "started_at": str(decision.get("started_at") or _now()),
        "completed_at": "",
    })
    protocol["active_canonical_request_hash"] = request_hash
    protocol["status"] = "invoking"
    protocol["terminal_error_code"] = None
    protocol["terminal_owner"] = None
    protocol["pending_ambiguity_id"] = None
    _sync_finalize_summaries(protocol)


def _record_response_transition(protocol: Dict[str, Any], decision: Mapping[str, Any]) -> None:
    attempt_id = str(decision.get("attempt_id") or "")
    artifact_ref = str(decision.get("response_artifact_ref") or "").strip()
    content_hash = str(decision.get("response_content_hash") or "").strip()
    if not artifact_ref or not content_hash:
        raise ValueError("provider_response requires protected artifact ref and exact content hash")
    index = _attempt_index(protocol, attempt_id)
    attempt = protocol["attempts"][index]
    previous_content_hashes = {
        str(item.get("response_content_hash") or "")
        for item in protocol.get("attempts", [])[:index]
        if str(item.get("response_content_hash") or "")
    }
    if decision.get("call_id") and str(decision.get("call_id")) != str(attempt.get("call_id")):
        raise ValueError("Finalize response call ID does not match its request intent")
    if decision.get("canonical_request_hash") and (
        str(decision.get("canonical_request_hash")) != str(attempt.get("canonical_request_hash"))
    ):
        raise ValueError("Finalize response canonical request hash does not match its request intent")
    prior_ref = str(attempt.get("response_artifact_ref") or "")
    prior_hash = str(attempt.get("response_content_hash") or "")
    if prior_ref or prior_hash:
        if prior_ref == artifact_ref and prior_hash == content_hash:
            return
        raise ValueError("A finalize attempt cannot be rebound to a different response artifact")
    if str(attempt.get("provider_status") or "") not in {"in_flight", "outcome_unknown"}:
        raise ValueError("Only an in-flight finalize attempt may record a response")

    protocol_status = str(decision.get("protocol_status") or "not_validated")
    if protocol_status not in FINALIZE_RESULT_PROTOCOL_STATUSES:
        raise ValueError(f"Unsupported finalize protocol status: {protocol_status}")
    attempt.update({
        "provider_status": "succeeded",
        "response_content_hash": content_hash,
        "response_artifact_ref": artifact_ref,
        "protocol_status": protocol_status,
        "error_code": str(decision.get("error_code") or ""),
        "error_owner": str(decision.get("error_owner") or ""),
        "completed_at": str(decision.get("completed_at") or _now()),
    })
    repair = protocol["repair"]
    if not repair.get("original_response_artifact_ref"):
        repair["original_response_artifact_ref"] = artifact_ref
        repair["original_response_content_hash"] = content_hash
    elif (
        not repair.get("original_response_content_hash")
        or not repair.get("original_response_artifact_ref")
    ):
        raise ValueError("Original finalize response binding is incomplete")
    repair["latest_response_artifact_ref"] = artifact_ref
    repair["latest_response_content_hash"] = content_hash
    repair["latest_response_excerpt"] = str(decision.get("response_excerpt") or "")
    repair["response_excerpt_truncated"] = bool(decision.get("response_excerpt_truncated", False))
    if (
        str(attempt.get("call_kind") or "") in {"wire_repair", "field_repair"}
        and content_hash in previous_content_hashes
    ):
        _set_finalize_block(
            protocol,
            error_code="FINALIZE_IDENTICAL_RESPONSE_LOOP",
            error_owner="controller_model_protocol",
        )
        return
    _sync_finalize_summaries(protocol)


def _record_provider_failure_transition(protocol: Dict[str, Any], decision: Mapping[str, Any]) -> None:
    attempt_id = str(decision.get("attempt_id") or "")
    index = _attempt_index(protocol, attempt_id)
    attempt = protocol["attempts"][index]
    if attempt.get("response_artifact_ref"):
        raise ValueError("A provider failure cannot overwrite a completed response artifact")
    provider_status = str(decision.get("provider_status") or "")
    if provider_status not in {"transient_failed", "fatal_failed", "outcome_unknown"}:
        raise ValueError("Provider failure status must be transient_failed, fatal_failed, or outcome_unknown")
    retry_safe = bool(decision.get("retry_safe", False))
    if provider_status == "transient_failed" and not retry_safe:
        raise ValueError("A transient provider retry requires adapter-certified retry_safe=true")
    error_code = str(decision.get("error_code") or {
        "transient_failed": "FINALIZE_PROVIDER_TRANSIENT",
        "fatal_failed": "FINALIZE_PROVIDER_FATAL",
        "outcome_unknown": "FINALIZE_PROVIDER_OUTCOME_UNKNOWN",
    }[provider_status])
    owner = str(decision.get("error_owner") or (
        "controller_provider" if provider_status == "outcome_unknown" else "provider"
    ))
    if owner not in FINALIZE_ERROR_OWNERS:
        raise ValueError(f"Unsupported finalize error owner: {owner}")
    attempt.update({
        "provider_status": provider_status,
        "protocol_status": "not_validated",
        "error_code": error_code,
        "error_owner": owner,
        "retry_safe": retry_safe,
        "completed_at": str(decision.get("completed_at") or _now()),
    })
    if provider_status == "transient_failed":
        protocol["status"] = (
            "repair_pending"
            if protocol.get("repair", {}).get("repair_kind") in {"wire_format", "field_patch"}
            else "ready"
        )
    else:
        _set_finalize_block(
            protocol,
            error_code=error_code,
            error_owner=owner,
            provider_block=True,
        )
    _sync_finalize_summaries(protocol)


def _record_response_persistence_failure_transition(
    protocol: Dict[str, Any],
    decision: Mapping[str, Any],
) -> None:
    """Record provider success whose protected response could not be committed.

    This is a controller-storage terminal state, not an unknown/fatal provider
    outcome.  Keeping ``protocol_status=not_validated`` is important: no exact
    artifact exists from which protocol validation could truthfully proceed.
    """
    attempt_id = str(decision.get("attempt_id") or "")
    index = _attempt_index(protocol, attempt_id)
    attempt = protocol["attempts"][index]
    if attempt.get("response_artifact_ref"):
        raise ValueError("Response persistence failure cannot overwrite a protected response")
    if str(attempt.get("provider_status") or "") not in {"in_flight", "succeeded"}:
        raise ValueError("Response persistence failure requires an invoked provider attempt")
    attempt.update({
        "provider_status": "succeeded",
        "protocol_status": "not_validated",
        "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
        "error_owner": "controller_storage",
        "retry_safe": False,
        "completed_at": str(decision.get("completed_at") or _now()),
    })
    protocol["status"] = "blocked_internal_contract"
    protocol["terminal_error_code"] = "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
    protocol["terminal_owner"] = "controller_storage"
    protocol["pending_ambiguity_id"] = None
    _sync_finalize_summaries(protocol)


def _record_partial_response_transition(
    protocol: Dict[str, Any],
    decision: Mapping[str, Any],
) -> None:
    """Project a delivered legacy response that is not safe to parse/merge."""
    attempt_id = str(decision.get("attempt_id") or "")
    index = _attempt_index(protocol, attempt_id)
    attempt = protocol["attempts"][index]
    if str(attempt.get("provider_status") or "") != "in_flight":
        raise ValueError("Only an in-flight legacy attempt may receive a partial response")
    integrity = str(decision.get("completion_integrity") or "")
    finish_reason = str(decision.get("finish_reason") or "")
    if integrity not in {"truncated", "content_filtered", "unknown"}:
        raise ValueError("Unsupported partial completion integrity")
    if integrity == "truncated" and finish_reason not in {
        "length", "insufficient_system_resource",
    }:
        raise ValueError("Truncated completion requires a typed finish reason")
    partial_ref = str(decision.get("partial_response_artifact_ref") or "")
    partial_hash = str(decision.get("partial_response_artifact_hash") or "")
    if not partial_ref or not partial_hash:
        raise ValueError("Partial response artifact ref/hash is required")
    attempt.update({
        "provider_status": "succeeded",
        "completion_integrity": integrity,
        "protocol_status": "not_validated",
        "finish_reason": finish_reason,
        "usage": deepcopy(dict(decision.get("usage") or {})),
        "partial_response_artifact_ref": partial_ref,
        "partial_response_artifact_hash": partial_hash,
        "response_artifact_ref": "",
        "response_content_hash": "",
        "error_code": (
            "FINALIZE_RESPONSE_TRUNCATED"
            if integrity == "truncated"
            else "FINALIZE_RESPONSE_CONTENT_FILTERED"
            if integrity == "content_filtered"
            else "FINALIZE_PROVIDER_RESOURCE_UNKNOWN"
        ),
        "error_owner": "controller_model_protocol",
        "retry_safe": False,
    })
    protocol["attempts"][index] = attempt
    protocol["status"] = "blocked_internal_contract"
    protocol["terminal_error_code"] = str(attempt["error_code"])
    protocol["terminal_owner"] = str(attempt["error_owner"])
    protocol["completion_integrity"] = integrity
    protocol["pending_ambiguity_id"] = None
    _sync_finalize_summaries(protocol)


def _record_validation_transition(protocol: Dict[str, Any], decision: Mapping[str, Any]) -> None:
    attempt_id = str(decision.get("attempt_id") or "")
    index = _attempt_index(protocol, attempt_id)
    attempt = protocol["attempts"][index]
    if str(attempt.get("provider_status") or "") != "succeeded" or not attempt.get("response_artifact_ref"):
        raise ValueError("Protocol validation requires a protected successful response artifact")
    protocol_status = str(decision.get("protocol_status") or "")
    if protocol_status not in FINALIZE_RESULT_PROTOCOL_STATUSES - {"not_validated"}:
        raise ValueError(f"Unsupported validation result: {protocol_status}")
    error_code = str(decision.get("error_code") or "")
    owner = str(decision.get("error_owner") or ("controller_model_protocol" if error_code else ""))
    if owner and owner not in FINALIZE_ERROR_OWNERS:
        raise ValueError(f"Unsupported finalize error owner: {owner}")
    attempt.update({
        "protocol_status": protocol_status,
        "error_code": error_code,
        "error_owner": owner,
    })
    repair = protocol["repair"]
    repair["error_code"] = error_code
    preserve_patch_targets = bool(decision.get("preserve_patch_targets", False))
    if preserve_patch_targets:
        if repair.get("repair_kind") != "field_patch":
            raise ValueError("Patch-target preservation is valid only during field repair")
        repair["last_patch_protocol_errors"] = deepcopy(list(
            decision.get("patch_protocol_errors")
            or decision.get("validation_errors")
            or decision.get("errors")
            or []
        ))
    else:
        repair["validation_errors"] = deepcopy(list(decision.get("validation_errors") or decision.get("errors") or []))
        repair["last_patch_protocol_errors"] = []
        repair["errors_by_field"] = deepcopy(dict(decision.get("errors_by_field") or {}))
        repair["allowed_fields_by_index"] = deepcopy(dict(decision.get("allowed_fields_by_index") or {}))
        repair["allowed_payload_fields"] = deepcopy(list(decision.get("allowed_payload_fields") or []))
    if "parsed_payload" in decision and decision.get("parsed_payload") is not None:
        parsed_payload = decision.get("parsed_payload")
        if not isinstance(parsed_payload, Mapping):
            raise ValueError("parsed_payload must be a JSON object")
        parsed_copy = deepcopy(dict(parsed_payload))
        if repair.get("original_payload") is None:
            repair["original_payload"] = parsed_copy
        repair["base_payload"] = parsed_copy
        repair["payload_hash"] = str(decision.get("payload_hash") or _digest(parsed_copy))

    if protocol_status == "valid":
        protocol["status"] = "invoking"
        repair["repair_kind"] = "none"
    elif protocol_status == "wire_invalid":
        protocol["status"] = "repair_pending"
        repair["repair_kind"] = "wire_format"
    elif protocol_status == "field_invalid":
        protocol["status"] = "repair_pending"
        repair["repair_kind"] = "field_patch"
    elif protocol_status == "needs_user":
        protocol["status"] = "needs_user"
    elif protocol_status == "blocked":
        _set_finalize_block(
            protocol,
            error_code=error_code or "FINALIZE_REPAIR_EXHAUSTED",
            error_owner=owner or "controller",
        )
    _sync_finalize_summaries(protocol)


def commit_finalize_decision(
    state: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> Dict[str, Any]:
    """Sole atomic API for mutating an initialized finalize protocol.

    The protocol module may propose a transition, but only this function
    validates the active binding, updates ordered attempts, consumes budgets,
    derives summary statuses, and commits terminal/user routing state.
    """
    if _contains_raw_response(decision):
        raise ValueError("Exact/raw assistant response content must remain in the protected artifact store")
    updated = deepcopy(dict(state))
    protocol = _active_finalize_protocol(updated)
    proposal = dict(decision or {})
    transition = str(
        proposal.get("transition")
        or proposal.get("decision_type")
        or proposal.get("kind")
        or ""
    ).strip()
    if not transition:
        raise ValueError("Finalize decision requires a transition")
    _validate_finalize_binding(protocol, proposal)
    terminal = str(protocol.get("status") or "") in {
        "compiled",
        "blocked_internal_contract",
        "blocked_internal_provider",
    }
    checkpoint_override = (
        transition == "checkpoint_integrity_failure"
        and str(proposal.get("error_code") or "") == "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
        and str(proposal.get("error_owner") or "") == "controller_storage"
    )
    if terminal and transition != "replace_context" and not checkpoint_override:
        raise ValueError("A terminal finalize protocol cannot accept another transition")

    if transition in {"request_started", "record_request", "begin_attempt"}:
        _record_request_transition(protocol, proposal)
    elif transition in {"provider_response", "record_response"}:
        _record_response_transition(protocol, proposal)
    elif transition in {"provider_failure", "record_failure"}:
        _record_provider_failure_transition(protocol, proposal)
    elif transition in {"response_persistence_failure", "record_storage_failure"}:
        _record_response_persistence_failure_transition(protocol, proposal)
    elif transition in {"provider_partial_response", "record_partial_response"}:
        _record_partial_response_transition(protocol, proposal)
    elif transition in {"protocol_validation", "record_validation"}:
        _record_validation_transition(protocol, proposal)
    elif transition in {"compiled", "compile"}:
        if protocol.get("attempts"):
            latest = protocol["attempts"][-1]
            if str(latest.get("provider_status") or "") != "succeeded":
                raise ValueError("A final contract requires a provider-successful response")
            latest["protocol_status"] = "valid"
            latest["error_code"] = ""
            latest["error_owner"] = ""
        protocol["status"] = "compiled"
        protocol["terminal_error_code"] = None
        protocol["terminal_owner"] = None
        protocol["pending_ambiguity_id"] = None
        protocol["repair"]["repair_kind"] = "none"
        updated["pending_human_question"] = None
        _sync_finalize_summaries(protocol)
    elif transition in {"needs_user", "semantic_ambiguity"}:
        ambiguity_ids = list(proposal.get("ambiguity_ids") or [])
        ambiguity_id = str(proposal.get("ambiguity_id") or (ambiguity_ids[0] if len(ambiguity_ids) == 1 else ""))
        allowed = {
            str(item)
            for item in (
                proposal.get("context_unresolved_semantic_ids")
                or proposal.get("allowed_ambiguity_ids")
                or []
            )
            if str(item)
        }
        if len(ambiguity_ids) > 1 or not ambiguity_id or ambiguity_id not in allowed:
            raise ValueError("needs_user requires exactly one ambiguity already present in the active context")
        protocol["status"] = "needs_user"
        protocol["pending_ambiguity_id"] = ambiguity_id
        protocol["terminal_error_code"] = None
        protocol["terminal_owner"] = None
        if protocol.get("attempts"):
            protocol["attempts"][-1]["protocol_status"] = "needs_user"
            protocol["attempts"][-1]["error_code"] = "FINALIZE_SEMANTIC_AMBIGUITY"
            protocol["attempts"][-1]["error_owner"] = "user_semantic"
        updated["pending_human_question"] = deepcopy(proposal.get("pending_human_question"))
        updated["phase"] = "needs_user"
        _sync_finalize_summaries(protocol)
    elif transition in {"blocked_internal", "block"}:
        _set_finalize_block(
            protocol,
            error_code=str(proposal.get("error_code") or "FINALIZE_REPAIR_EXHAUSTED"),
            error_owner=str(proposal.get("error_owner") or "controller"),
            provider_block=proposal.get("provider_block"),
        )
        updated["pending_human_question"] = None
        updated["phase"] = "blocked"
    elif transition == "checkpoint_integrity_failure":
        _set_finalize_block(
            protocol,
            error_code="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            error_owner="controller_storage",
            provider_block=False,
        )
        updated["pending_human_question"] = None
        updated["phase"] = "blocked"
    elif transition in {"supersede", "context_superseded"}:
        revision = proposal.get("task_spec_revision")
        if revision in (None, ""):
            raise ValueError("Superseding finalize context requires the new TaskSpec revision")
        protocol["superseded_by_task_spec_revision"] = revision
        protocol["status"] = "needs_user"
        protocol["pending_ambiguity_id"] = str(proposal.get("ambiguity_id") or "") or None
        updated["pending_human_question"] = deepcopy(proposal.get("pending_human_question"))
        updated["phase"] = "needs_user"
    elif transition in {"ambiguity_answer_applied", "user_answer_applied"}:
        revision = proposal.get("task_spec_revision")
        ambiguity_id = str(proposal.get("ambiguity_id") or "").strip()
        if revision in (None, "") or not ambiguity_id:
            raise ValueError(
                "An ambiguity answer requires its ID and answered TaskSpec revision"
            )
        if (
            str(protocol.get("status") or "") != "needs_user"
            or str(protocol.get("pending_ambiguity_id") or "") != ambiguity_id
        ):
            raise ValueError(
                "An ambiguity answer must target the protocol's exact pending ambiguity"
            )
        protocol["superseded_by_task_spec_revision"] = revision
        protocol["status"] = "ready"
        protocol["pending_ambiguity_id"] = None
        updated["pending_human_question"] = None
        updated["phase"] = "finalize"
    elif transition == "replace_context":
        if (
            str(protocol.get("status") or "") != "ready"
            or protocol.get("superseded_by_task_spec_revision") in (None, "")
        ):
            raise ValueError(
                "A replacement finalize context requires an applied ambiguity answer"
            )
        replacement = new_finalize_protocol(
            str(proposal.get("new_finalize_context_id") or ""),
            str(proposal.get("new_base_contract_hash") or ""),
            wire_schema_version=int(proposal.get("new_wire_schema_version") or 2),
            max_wire_repair_attempts=int(proposal.get("max_wire_repair_attempts") or 2),
            max_field_repair_attempts=int(proposal.get("max_field_repair_attempts") or 2),
        )
        if replacement["finalize_context_id"] == protocol.get("finalize_context_id"):
            raise ValueError("A user-answer transition must create a new finalize context")
        protocol = replacement
        updated["pending_human_question"] = None
        updated["phase"] = "finalize"
    else:
        raise ValueError(f"Unsupported finalize transition: {transition}")

    protocol["updated_at"] = _now()
    if protocol.get("status") not in FINALIZE_PROTOCOL_STATUSES:
        raise ValueError(f"Unsupported finalize protocol status: {protocol.get('status')}")
    _sync_finalize_summaries(protocol)
    if protocol.get("provider_status") not in FINALIZE_PROVIDER_STATUSES:
        raise ValueError(f"Unsupported provider status: {protocol.get('provider_status')}")
    if protocol.get("protocol_status") not in FINALIZE_RESULT_PROTOCOL_STATUSES:
        raise ValueError(f"Unsupported protocol status: {protocol.get('protocol_status')}")
    if protocol.get("status") in {
        "blocked_internal_contract",
        "blocked_internal_provider",
    }:
        updated["phase"] = "blocked"
        updated["consultation_status"] = "blocked"
        updated["completion_decision"] = "blocked"
        updated["completion_reason"] = str(
            protocol.get("terminal_error_code") or "finalize_internal_error"
        )
        updated["pending_human_question"] = None
    elif protocol.get("status") == "needs_user":
        updated["phase"] = "needs_user"
        updated["consultation_status"] = "needs_user"
        updated["completion_decision"] = "needs_user"
        updated["completion_reason"] = "validated_semantic_ambiguity"
    elif protocol.get("status") == "compiled":
        updated["phase"] = "finalize"
        updated["consultation_status"] = "completed"
        updated["completion_decision"] = "ready_to_finalize"
        updated["completion_reason"] = "formal_contract_compiled_v2"
        updated["pending_human_question"] = None
    else:
        updated["phase"] = "finalize"
        updated["pending_human_question"] = None
    updated["finalize_protocol"] = protocol
    # The legacy top-level object is never independently committed.
    updated.pop("consultant_contract_repair", None)
    updated["updated_at"] = _now()
    return updated


def finalize_repair_projection(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a read-only legacy projection derived from the nested owner."""
    protocol = state.get("finalize_protocol")
    if not isinstance(protocol, Mapping):
        return {}
    repair = deepcopy(dict(protocol.get("repair") or {}))
    if not repair:
        return {}
    repair["schema_version"] = FINALIZE_PROTOCOL_SCHEMA_VERSION
    repair["finalize_context_id"] = str(protocol.get("finalize_context_id") or "")
    repair["base_contract_hash"] = str(protocol.get("base_contract_hash") or "")
    repair["repair_attempt"] = (
        int(repair.get("wire_repair_attempt") or 0)
        if repair.get("repair_kind") == "wire_format"
        else int(repair.get("field_repair_attempt") or 0)
    )
    return repair


def emit_finalize_repair_projection(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy state and emit the deprecated projection for legacy readers only."""
    updated = deepcopy(dict(state))
    projection = finalize_repair_projection(updated)
    updated["consultant_contract_repair"] = projection
    return updated


def migrate_finalize_protocol_state(
    state: Mapping[str, Any],
    *,
    finalize_context_id: str = "",
    base_contract_hash: str = "",
    wire_schema_version: int = 2,
) -> Dict[str, Any]:
    """Migrate one legacy repair checkpoint without creating parallel owners."""
    updated = deepcopy(dict(state))
    nested = updated.get("finalize_protocol")
    legacy = updated.get("consultant_contract_repair")
    if isinstance(nested, Mapping) and nested:
        if int(nested.get("schema_version") or 0) != FINALIZE_PROTOCOL_SCHEMA_VERSION:
            raise ValueError("Unsupported nested finalize protocol schema")
        if isinstance(legacy, Mapping) and legacy:
            projection = finalize_repair_projection(updated)
            comparable = {
                key: value
                for key, value in projection.items()
                if key in legacy
            }
            if any(_digest(value) != _digest(legacy.get(key)) for key, value in comparable.items()):
                updated.setdefault("migration_diagnostics", []).append({
                    "error_code": "FINALIZE_MIGRATION_PROJECTION_CONFLICT",
                    "nested_state_won": True,
                })
        updated.pop("consultant_contract_repair", None)
        return updated
    if not isinstance(legacy, Mapping) or not legacy:
        updated.pop("consultant_contract_repair", None)
        return updated

    context_id = str(finalize_context_id or legacy.get("finalize_context_id") or "")
    contract_hash = str(base_contract_hash or legacy.get("base_contract_hash") or "")
    if not context_id or not contract_hash:
        # Historical repair data cannot be rebound to a fabricated context.
        protocol = {
            **new_finalize_protocol(
                "unrecoverable-historical-context",
                "unrecoverable-historical-contract",
                wire_schema_version=wire_schema_version,
            ),
            "finalize_context_id": "",
            "base_contract_hash": "",
        }
        _set_finalize_block(
            protocol,
            error_code="FINALIZE_CONTEXT_BINDING_UNRECOVERABLE",
            error_owner="controller_storage",
        )
        updated["finalize_protocol"] = protocol
        updated["pending_human_question"] = None
        updated.pop("consultant_contract_repair", None)
        return updated

    protocol = new_finalize_protocol(
        context_id,
        contract_hash,
        wire_schema_version=int(legacy.get("wire_schema_version") or wire_schema_version),
        max_wire_repair_attempts=int(legacy.get("max_wire_repair_attempts") or 2),
        max_field_repair_attempts=int(
            legacy.get("max_field_repair_attempts")
            or legacy.get("max_repair_attempts")
            or 2
        ),
    )
    repair = protocol["repair"]
    original_payload = legacy.get("original_payload")
    base_payload = legacy.get("base_payload") or legacy.get("latest_payload")
    parsed = isinstance(original_payload, Mapping) or isinstance(base_payload, Mapping)
    repair.update({
        "repair_kind": "field_patch" if parsed else "wire_format",
        "error_code": str(legacy.get("error_code") or "FINALIZE_SCHEMA_INVALID"),
        "wire_repair_attempt": int(legacy.get("wire_repair_attempt") or (0 if parsed else legacy.get("repair_attempt") or 0)),
        "field_repair_attempt": int(legacy.get("field_repair_attempt") or (legacy.get("repair_attempt") or 0 if parsed else 0)),
        "initial_canonical_request_hash": str(legacy.get("initial_canonical_request_hash") or ""),
        "original_response_artifact_ref": str(legacy.get("original_response_artifact_ref") or ""),
        "latest_response_artifact_ref": str(legacy.get("latest_response_artifact_ref") or ""),
        "latest_response_excerpt": str(legacy.get("latest_response_excerpt") or ""),
        "response_excerpt_truncated": bool(legacy.get("response_excerpt_truncated", False)),
        "original_response_content_hash": str(legacy.get("original_response_content_hash") or ""),
        "latest_response_content_hash": str(legacy.get("latest_response_content_hash") or ""),
        "original_payload": deepcopy(dict(original_payload)) if isinstance(original_payload, Mapping) else None,
        "base_payload": deepcopy(dict(base_payload)) if isinstance(base_payload, Mapping) else (
            deepcopy(dict(original_payload)) if isinstance(original_payload, Mapping) else None
        ),
        "payload_hash": str(legacy.get("payload_hash") or legacy.get("base_payload_hash") or ""),
        "validation_errors": deepcopy(list(legacy.get("validation_errors") or legacy.get("errors") or [])),
        "last_patch_protocol_errors": deepcopy(list(legacy.get("last_patch_protocol_errors") or [])),
        "errors_by_field": deepcopy(dict(legacy.get("errors_by_field") or {})),
        "allowed_fields_by_index": deepcopy(dict(legacy.get("allowed_fields_by_index") or {})),
        "allowed_payload_fields": deepcopy(list(legacy.get("allowed_payload_fields") or [])),
    })
    protocol["status"] = "repair_pending"
    protocol["protocol_status"] = "field_invalid" if parsed else "wire_invalid"
    protocol["updated_at"] = _now()
    updated["finalize_protocol"] = protocol
    updated.pop("consultant_contract_repair", None)
    updated["updated_at"] = _now()
    return updated


# ---------------------------------------------------------------------------
# FINALIZE protocol schema v4 / wire schema v3
# ---------------------------------------------------------------------------

def _v4_ref_pair(value: Mapping[str, Any], prefix: str) -> bool:
    """Return whether a durable ref/hash pair is complete.

    Mutable graph state only carries pointers.  The authoritative bytes live in
    ``ContextLineageStore``; accepting a half-populated pair would make a
    checkpoint look recoverable when it is not.
    """
    return bool(str(value.get(f"{prefix}_ref") or "").strip()) and bool(
        str(value.get(f"{prefix}_hash") or "").strip()
    )


def new_finalize_protocol_v4(
    finalize_context_id: str,
    base_contract_hash: str,
    *,
    replay_scope_id: str,
    source_task_spec_revision: Any,
    parent_finalize_context_id: str = "",
    migration_reason: str = "",
    source_protocol_schema_version: Optional[int] = None,
    source_wire_schema_version: Optional[int] = None,
) -> Dict[str, Any]:
    """Create an empty schema-v4/wire-v3 protocol owner.

    This function intentionally does not migrate an existing attempt in place.
    A caller that resumes legacy work must create an explicitly linked child
    context after applying the migration matrix in the output-capacity spec.
    """
    context_id = str(finalize_context_id or "").strip()
    contract_hash = str(base_contract_hash or "").strip()
    replay_scope = str(replay_scope_id or "").strip()
    if not context_id or not contract_hash or not replay_scope:
        raise ValueError(
            "schema-v4 finalize requires context, base-contract, and replay-scope IDs"
        )
    now = _now()
    return {
        "schema_version": FINALIZE_PROTOCOL_V4_SCHEMA_VERSION,
        "wire_schema_version": 3,
        "replay_scope_id": replay_scope,
        "finalize_context_id": context_id,
        "base_contract_hash": contract_hash,
        "finalize_status": "collecting",
        "compile_mode": None,
        "provider_status": "not_called",
        "completion_integrity": "not_called",
        "protocol_status": "not_validated",
        "coverage_artifact_ref": "",
        "coverage_artifact_hash": "",
        "candidate_catalog_ref": "",
        "candidate_catalog_hash": "",
        "partition_capacity_basis_ref": "",
        "partition_capacity_basis_hash": "",
        "work_budget_ref": "",
        "work_budget_hash": "",
        "work_preflight_ref": "",
        "work_preflight_hash": "",
        "reservation_ledger_ref": "",
        "reservation_ledger_hash": "",
        "parent_finalize_context_id": str(parent_finalize_context_id or ""),
        "migration_reason": str(migration_reason or ""),
        "source_protocol_schema_version": source_protocol_schema_version,
        "source_wire_schema_version": source_wire_schema_version,
        "migration_artifact_ref": "",
        "migration_artifact_hash": "",
        "pending_ambiguity_id": "",
        "source_task_spec_revision": deepcopy(source_task_spec_revision),
        "supersession_link_ref": "",
        "supersession_link_hash": "",
        "terminal_error_code": "",
        "terminal_owner": "",
        "terminal_diagnostic": {},
        "compile_outcome": {
            "status": "not_started",
            "mode": None,
            "protocol_status": "not_validated",
            "pending_ambiguity_id": "",
            "coverage_artifact_ref": "",
            "coverage_artifact_hash": "",
            "compiled_contract_ref": "",
            "compiled_contract_hash": "",
            "committed_at": "",
        },
        "aggregate": {
            "status": "not_started",
            "expected_resolution_ids": [],
            "resolved_resolution_ids": [],
            "pending_resolution_ids": [],
            "created_batches": 0,
            "used_provider_calls": 0,
            "used_completion_tokens": 0,
            "used_estimated_cost_microusd": 0,
        },
        "batches": [],
        "attempts": [],
        "reservation_projections": [],
        "created_at": now,
        "updated_at": now,
    }


def initialize_finalize_protocol_v4(
    state: Mapping[str, Any],
    *,
    finalize_context_id: str,
    base_contract_hash: str,
    replay_scope_id: str,
    source_task_spec_revision: Any,
    parent_finalize_context_id: str = "",
    migration_reason: str = "",
    source_protocol_schema_version: Optional[int] = None,
    source_wire_schema_version: Optional[int] = None,
    replace: bool = False,
) -> Dict[str, Any]:
    updated = deepcopy(dict(state))
    existing = updated.get("finalize_protocol")
    if isinstance(existing, Mapping) and existing:
        same = (
            int(existing.get("schema_version") or 0) == 4
            and int(existing.get("wire_schema_version") or 0) == 3
            and str(existing.get("finalize_context_id") or "")
            == str(finalize_context_id or "")
            and str(existing.get("base_contract_hash") or "")
            == str(base_contract_hash or "")
            and str(existing.get("replay_scope_id") or "")
            == str(replay_scope_id or "")
        )
        if same:
            validate_finalize_protocol_v4(existing)
            return updated
        if not replace:
            raise ValueError("An active finalize protocol cannot be rebound in place")
    updated["finalize_protocol"] = new_finalize_protocol_v4(
        finalize_context_id,
        base_contract_hash,
        replay_scope_id=replay_scope_id,
        source_task_spec_revision=source_task_spec_revision,
        parent_finalize_context_id=parent_finalize_context_id,
        migration_reason=migration_reason,
        source_protocol_schema_version=source_protocol_schema_version,
        source_wire_schema_version=source_wire_schema_version,
    )
    updated["pending_human_question"] = None
    updated.pop("consultant_contract_repair", None)
    updated["updated_at"] = _now()
    return updated


def finalize_protocol_runner(protocol: Optional[Mapping[str, Any]]) -> str:
    """Select a frozen runner without parsing a payload under the wrong ABI."""
    if not isinstance(protocol, Mapping) or not protocol:
        return "initialize_v4"
    schema = int(protocol.get("schema_version") or 0)
    wire = int(protocol.get("wire_schema_version") or 0)
    if (schema, wire) == (FINALIZE_PROTOCOL_SCHEMA_VERSION, 2):
        return "legacy_v2"
    if (schema, wire) == (FINALIZE_PROTOCOL_V4_SCHEMA_VERSION, 3):
        return "v4"
    raise ValueError(
        f"Unsupported FINALIZE protocol/wire pair: schema={schema}, wire={wire}"
    )


def legacy_finalize_migration_eligibility(
    protocol: Mapping[str, Any],
    *,
    explicit_resume: bool = False,
    task_spec_revised: bool = False,
) -> Dict[str, Any]:
    """Apply the state-specific migration matrix without mutating the parent."""
    runner = finalize_protocol_runner(protocol)
    if runner == "v4":
        return {"eligible": False, "reason": "already_v4", "runner": runner}
    status = str(protocol.get("status") or "ready")
    attempts = [item for item in protocol.get("attempts", []) or [] if isinstance(item, Mapping)]
    if status == "ready" and not attempts:
        return {"eligible": True, "reason": "ready_without_provider_intent", "runner": runner}
    if status == "needs_user" and task_spec_revised:
        return {"eligible": True, "reason": "task_spec_revision_after_user_answer", "runner": runner}
    if status in {"blocked_internal_contract", "blocked_internal_provider"} and explicit_resume:
        return {"eligible": True, "reason": "explicit_terminal_resume", "runner": runner}
    return {"eligible": False, "reason": f"legacy_state_pinned:{status}", "runner": runner}


def _v4_attempt(protocol: Mapping[str, Any], attempt_id: str) -> tuple[int, Dict[str, Any]]:
    matches = [
        (index, deepcopy(dict(item)))
        for index, item in enumerate(protocol.get("attempts", []) or [])
        if isinstance(item, Mapping)
        and str(item.get("attempt_id") or "") == str(attempt_id or "")
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one schema-v4 attempt for {attempt_id!r}")
    return matches[0]


def _sync_finalize_v4_summaries(protocol: Dict[str, Any]) -> None:
    outcome = dict(protocol.get("compile_outcome") or {})
    outcome_status = str(outcome.get("status") or "not_started")
    attempts = [dict(item) for item in protocol.get("attempts", []) or []]
    if outcome_status == "compiled":
        protocol["finalize_status"] = "compiled"
        protocol["protocol_status"] = "valid"
        if not attempts:
            protocol["provider_status"] = "not_called"
            protocol["completion_integrity"] = "not_called"
    elif outcome_status == "needs_user":
        protocol["finalize_status"] = "needs_user"
        protocol["protocol_status"] = "needs_user"
    elif outcome_status == "blocked":
        protocol["finalize_status"] = "blocked"
        protocol["protocol_status"] = "blocked"
        if not attempts:
            protocol["provider_status"] = "not_called"
            protocol["completion_integrity"] = "not_called"
    elif attempts:
        latest = attempts[-1]
        protocol["provider_status"] = str(latest.get("provider_status") or "not_called")
        protocol["completion_integrity"] = str(
            latest.get("completion_integrity") or "not_called"
        )
        protocol["protocol_status"] = str(latest.get("protocol_status") or "not_validated")


def validate_finalize_protocol_v4(protocol: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate schema-v4 enums and the cross-product invariants in the spec."""
    errors: list[str] = []
    if int(protocol.get("schema_version") or 0) != 4:
        errors.append("schema_version must be 4")
    if int(protocol.get("wire_schema_version") or 0) != 3:
        errors.append("wire_schema_version must be 3")
    for field in ("replay_scope_id", "finalize_context_id", "base_contract_hash"):
        if not str(protocol.get(field) or "").strip():
            errors.append(f"{field} is required")
    if str(protocol.get("provider_status") or "") not in FINALIZE_V4_PROVIDER_STATUSES:
        errors.append("provider_status is invalid")
    if str(protocol.get("completion_integrity") or "") not in FINALIZE_V4_COMPLETION_INTEGRITIES:
        errors.append("completion_integrity is invalid")
    if _contains_raw_response(protocol):
        errors.append("graph state may not contain raw provider content")

    legal_protocol = {
        ("not_called", "not_called"): {"not_validated", "blocked", "valid"},
        ("in_flight", "in_flight"): {"not_validated"},
        ("succeeded", "complete"): {
            "not_validated", "wire_invalid", "field_invalid", "needs_user", "valid",
        },
        ("succeeded", "truncated"): {"not_validated", "blocked"},
        ("succeeded", "content_filtered"): {"not_validated", "blocked"},
        ("succeeded", "unknown"): {"not_validated", "blocked"},
        ("transient_failed", "missing"): {"not_validated", "blocked"},
        ("fatal_failed", "missing"): {"not_validated", "blocked"},
        ("outcome_unknown", "missing"): {"not_validated", "blocked"},
        ("outcome_unknown", "unknown"): {"not_validated", "blocked"},
    }
    for attempt in protocol.get("attempts", []) or []:
        if not isinstance(attempt, Mapping):
            errors.append("attempt entries must be objects")
            continue
        triple = (
            str(attempt.get("provider_status") or ""),
            str(attempt.get("completion_integrity") or ""),
        )
        pstatus = str(attempt.get("protocol_status") or "")
        if pstatus not in legal_protocol.get(triple, set()):
            errors.append(f"illegal attempt status cross-product: {triple}/{pstatus}")
        if triple == ("succeeded", "truncated"):
            if not _v4_ref_pair(attempt, "partial_response_artifact"):
                errors.append("truncated attempt requires partial artifact ref/hash")
            if str(attempt.get("finish_reason") or "") != "length":
                errors.append("truncated attempt requires finish_reason=length")
    outcome = dict(protocol.get("compile_outcome") or {})
    if str(outcome.get("status") or "") == "compiled":
        if str(outcome.get("protocol_status") or "") != "valid":
            errors.append("compiled outcome must be protocol-valid")
        if not _v4_ref_pair(outcome, "coverage_artifact"):
            errors.append("compiled outcome requires coverage artifact")
        if not _v4_ref_pair(outcome, "compiled_contract"):
            errors.append("compiled outcome requires compiled contract")
    if errors:
        raise ValueError("; ".join(errors))
    return deepcopy(dict(protocol))


def commit_finalize_v4_transition(
    state: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> Dict[str, Any]:
    """Atomically project one schema-v4 transition into graph state.

    Artifacts must already be durable.  This function accepts only refs/hashes
    and therefore cannot accidentally checkpoint a partial/raw completion.
    """
    if _contains_raw_response(decision):
        raise ValueError("FINALIZE v4 decisions cannot contain raw provider content")
    updated = deepcopy(dict(state))
    protocol = deepcopy(dict(updated.get("finalize_protocol") or {}))
    if finalize_protocol_runner(protocol) != "v4":
        raise ValueError("schema-v4 transition requires a schema-v4/wire-v3 owner")
    transition = str(decision.get("transition") or "").strip()
    now = _now()

    if transition == "coverage_ready":
        if str(protocol.get("finalize_status") or "") != "collecting":
            raise ValueError("coverage can only be committed from collecting")
        for prefix in ("coverage_artifact",):
            if not _v4_ref_pair(decision, prefix):
                raise ValueError(f"{prefix} ref/hash is required")
        protocol["coverage_artifact_ref"] = str(decision["coverage_artifact_ref"])
        protocol["coverage_artifact_hash"] = str(decision["coverage_artifact_hash"])
        for prefix in (
            "candidate_catalog", "partition_capacity_basis", "work_budget",
            "work_preflight", "reservation_ledger",
        ):
            if _v4_ref_pair(decision, prefix):
                protocol[f"{prefix}_ref"] = str(decision[f"{prefix}_ref"])
                protocol[f"{prefix}_hash"] = str(decision[f"{prefix}_hash"])
        expected = list(dict.fromkeys(str(item) for item in decision.get("expected_resolution_ids", []) or []))
        protocol["compile_mode"] = (
            "model_batched" if expected else "controller_deterministic"
        )
        protocol["finalize_status"] = "resolving" if expected else "collecting"
        protocol["aggregate"] = {
            **dict(protocol.get("aggregate") or {}),
            "status": "resolving" if expected else "ready_to_compile",
            "expected_resolution_ids": expected,
            "resolved_resolution_ids": [],
            "pending_resolution_ids": expected,
        }
    elif transition == "batch_created":
        batch = deepcopy(dict(decision.get("batch") or {}))
        if not str(batch.get("batch_id") or "") or not str(batch.get("batch_hash") or ""):
            raise ValueError("batch ID/hash is required")
        if any(str(item.get("batch_id") or "") == str(batch["batch_id"]) for item in protocol["batches"]):
            raise ValueError("duplicate batch ID")
        batch.setdefault("status", "pending")
        batch.setdefault("superseded_by_batch_ids", [])
        protocol["batches"].append(batch)
        protocol["aggregate"]["created_batches"] = len(protocol["batches"])
    elif transition == "request_started":
        attempt = deepcopy(dict(decision.get("attempt") or {}))
        if str(attempt.get("call_kind") or "") not in FINALIZE_V4_CALL_KINDS:
            raise ValueError("invalid schema-v4 call kind")
        required = (
            "attempt_id", "call_id", "decision_batch_id", "decision_batch_hash",
            "request_intent_ref", "request_intent_hash", "protocol_request_hash",
            "wire_request_hash", "capacity_profile_ref", "capacity_profile_hash",
            "response_schema_ref", "response_schema_hash",
            "effective_wire_settings_ref", "effective_wire_settings_hash",
        )
        if any(not str(attempt.get(key) or "").strip() for key in required):
            raise ValueError("request_started is missing a durable identity/ref")
        attempt.update({
            "provider_status": "in_flight",
            "completion_integrity": "in_flight",
            "protocol_status": "not_validated",
            "finish_reason": "",
            "usage": {},
        })
        protocol["attempts"].append(attempt)
        for batch in protocol["batches"]:
            if str(batch.get("batch_id") or "") == str(attempt["decision_batch_id"]):
                batch["status"] = "in_flight"
                batch["active_attempt_id"] = str(attempt["attempt_id"])
        protocol["aggregate"]["used_provider_calls"] = int(
            protocol["aggregate"].get("used_provider_calls") or 0
        ) + 1
        protocol["finalize_status"] = "resolving"
    elif transition in {"response_complete", "response_truncated", "response_blocked"}:
        attempt_id = str(decision.get("attempt_id") or "")
        index, attempt = _v4_attempt(protocol, attempt_id)
        if str(attempt.get("provider_status") or "") != "in_flight":
            raise ValueError("only an in-flight attempt may receive a response")
        attempt["provider_status"] = "succeeded"
        attempt["finish_reason"] = str(decision.get("finish_reason") or "")
        attempt["usage"] = deepcopy(dict(decision.get("usage") or {}))
        completion_tokens = int(attempt["usage"].get("completion_tokens") or 0)
        protocol["aggregate"]["used_completion_tokens"] = int(
            protocol["aggregate"].get("used_completion_tokens") or 0
        ) + completion_tokens
        if transition == "response_complete":
            if not _v4_ref_pair(decision, "response_artifact"):
                raise ValueError("complete response requires artifact ref/hash")
            response_protocol_status = str(
                decision.get("protocol_status") or "not_validated"
            )
            attempt.update({
                "completion_integrity": "complete",
                "protocol_status": response_protocol_status,
                "response_artifact_ref": str(decision["response_artifact_ref"]),
                "response_artifact_hash": str(decision["response_artifact_hash"]),
            })
            if response_protocol_status in {"wire_invalid", "field_invalid"}:
                for batch in protocol["batches"]:
                    if str(batch.get("batch_id") or "") == str(
                        attempt.get("decision_batch_id") or ""
                    ):
                        batch["status"] = "protocol_invalid"
                        batch["protocol_status"] = response_protocol_status
        else:
            if not _v4_ref_pair(decision, "partial_response_artifact"):
                raise ValueError("non-committable response requires partial artifact ref/hash")
            integrity = "truncated" if transition == "response_truncated" else str(
                decision.get("completion_integrity") or "unknown"
            )
            attempt.update({
                "completion_integrity": integrity,
                "protocol_status": "not_validated",
                "partial_response_artifact_ref": str(decision["partial_response_artifact_ref"]),
                "partial_response_artifact_hash": str(decision["partial_response_artifact_hash"]),
                "retry_disposition": "reconcile_first",
            })
            for batch in protocol["batches"]:
                if str(batch.get("batch_id") or "") == str(attempt.get("decision_batch_id") or ""):
                    batch["status"] = "truncated" if integrity == "truncated" else "blocked"
                    batch["partial_response_artifact_ref"] = str(decision["partial_response_artifact_ref"])
                    batch["partial_response_artifact_hash"] = str(decision["partial_response_artifact_hash"])
        protocol["attempts"][index] = attempt
    elif transition == "provider_failed":
        attempt_id = str(decision.get("attempt_id") or "")
        index, attempt = _v4_attempt(protocol, attempt_id)
        if str(attempt.get("provider_status") or "") != "in_flight":
            raise ValueError("only an in-flight attempt may fail")
        provider_status = str(
            decision.get("provider_status") or "fatal_failed"
        )
        if provider_status not in {
            "transient_failed", "fatal_failed", "outcome_unknown"
        }:
            raise ValueError("provider_failed requires a terminal provider status")
        attempt.update({
            "provider_status": provider_status,
            "completion_integrity": (
                "unknown" if provider_status == "outcome_unknown" else "missing"
            ),
            "protocol_status": "not_validated",
            "finish_reason": "",
            "usage": {},
            "error_code": str(decision.get("error_code") or ""),
            "error_owner": str(decision.get("error_owner") or "provider"),
            "retry_safe": bool(decision.get("retry_safe", False)),
        })
        for batch in protocol["batches"]:
            if str(batch.get("batch_id") or "") == str(
                attempt.get("decision_batch_id") or ""
            ):
                batch["status"] = "provider_failed"
        protocol["attempts"][index] = attempt
    elif transition == "reservation_projected":
        if not _v4_ref_pair(decision, "reservation_ledger"):
            raise ValueError("reservation ledger projection requires ref/hash")
        projection = {
            "reservation_id": str(decision.get("reservation_id") or ""),
            "reservation_hash": str(decision.get("reservation_hash") or ""),
            "reservation_ref": str(decision.get("reservation_ref") or ""),
            "attempt_lineage_hash": str(
                decision.get("attempt_lineage_hash") or ""
            ),
            "wire_request_hash": str(decision.get("wire_request_hash") or ""),
            "status": str(decision.get("status") or ""),
            "record_revision": int(decision.get("record_revision") or 0),
        }
        if not all(str(projection.get(field) or "") for field in (
            "reservation_id", "reservation_hash", "reservation_ref",
            "attempt_lineage_hash", "wire_request_hash", "status",
        )):
            raise ValueError("reservation projection identity is incomplete")
        protocol["reservation_ledger_ref"] = str(
            decision["reservation_ledger_ref"]
        )
        protocol["reservation_ledger_hash"] = str(
            decision["reservation_ledger_hash"]
        )
        prior = [
            deepcopy(dict(item))
            for item in protocol.get("reservation_projections") or []
            if isinstance(item, Mapping)
            and str(item.get("reservation_id") or "")
            != projection["reservation_id"]
        ]
        protocol["reservation_projections"] = [*prior, projection]
    elif transition == "batch_resolved":
        batch_id = str(decision.get("batch_id") or "")
        resolved = list(dict.fromkeys(str(item) for item in decision.get("resolved_resolution_ids", []) or []))
        found = False
        for batch in protocol["batches"]:
            if str(batch.get("batch_id") or "") != batch_id:
                continue
            found = True
            if str(batch.get("status") or "") != "in_flight":
                raise ValueError("only an in-flight batch may resolve")
            if not _v4_ref_pair(decision, "decision_artifact"):
                raise ValueError("resolved batch requires decision artifact")
            batch.update({
                "status": "resolved",
                "decision_artifact_ref": str(decision["decision_artifact_ref"]),
                "decision_artifact_hash": str(decision["decision_artifact_hash"]),
            })
        if not found:
            raise ValueError("unknown batch")
        expected = list(protocol["aggregate"].get("expected_resolution_ids") or [])
        current = list(protocol["aggregate"].get("resolved_resolution_ids") or [])
        combined = list(dict.fromkeys([*current, *resolved]))
        if not set(combined).issubset(set(expected)):
            raise ValueError("resolved IDs are outside the aggregate")
        protocol["aggregate"]["resolved_resolution_ids"] = combined
        protocol["aggregate"]["pending_resolution_ids"] = [
            item for item in expected if item not in set(combined)
        ]
        protocol["aggregate"]["status"] = (
            "ready_to_compile"
            if set(combined) == set(expected)
            else "resolving"
        )
    elif transition == "batch_superseded":
        parent_id = str(decision.get("parent_batch_id") or "")
        child_ids = list(dict.fromkeys(str(item) for item in decision.get("child_batch_ids", []) or []))
        if not child_ids:
            raise ValueError("supersession requires child batches")
        known = {str(item.get("batch_id") or "") for item in protocol["batches"]}
        if not set(child_ids).issubset(known):
            raise ValueError("supersession references unknown child")
        for batch in protocol["batches"]:
            if str(batch.get("batch_id") or "") == parent_id:
                if str(batch.get("status") or "") not in {"truncated", "protocol_invalid"}:
                    raise ValueError("only truncated/invalid parent may be superseded")
                batch["status"] = "superseded"
                batch["superseded_by_batch_ids"] = child_ids
    elif transition == "compiled":
        if not _v4_ref_pair(decision, "coverage_artifact") or not _v4_ref_pair(
            decision, "compiled_contract"
        ):
            raise ValueError("compiled transition requires coverage and contract artifacts")
        expected = set(protocol["aggregate"].get("expected_resolution_ids") or [])
        resolved = set(protocol["aggregate"].get("resolved_resolution_ids") or [])
        if expected != resolved:
            raise ValueError("cannot compile a partial batch aggregate")
        mode = str(decision.get("compile_mode") or protocol.get("compile_mode") or "")
        if mode not in {"controller_deterministic", "model_batched"}:
            raise ValueError("invalid compile mode")
        if mode == "controller_deterministic" and protocol.get("attempts"):
            raise ValueError("deterministic compilation cannot contain provider attempts")
        protocol["compile_mode"] = mode
        protocol["compile_outcome"] = {
            "status": "compiled",
            "mode": mode,
            "protocol_status": "valid",
            "pending_ambiguity_id": "",
            "coverage_artifact_ref": str(decision["coverage_artifact_ref"]),
            "coverage_artifact_hash": str(decision["coverage_artifact_hash"]),
            "compiled_contract_ref": str(decision["compiled_contract_ref"]),
            "compiled_contract_hash": str(decision["compiled_contract_hash"]),
            "committed_at": now,
        }
        protocol["aggregate"]["status"] = "compiled"
    elif transition == "needs_user":
        ambiguity_id = str(decision.get("pending_ambiguity_id") or "").strip()
        if not ambiguity_id:
            raise ValueError("needs_user requires one validated ambiguity ID")
        protocol["pending_ambiguity_id"] = ambiguity_id
        protocol["compile_outcome"] = {
            **dict(protocol.get("compile_outcome") or {}),
            "status": "needs_user",
            "mode": protocol.get("compile_mode"),
            "protocol_status": "needs_user",
            "pending_ambiguity_id": ambiguity_id,
            "committed_at": now,
        }
        protocol["aggregate"]["status"] = "needs_user"
    elif transition == "blocked":
        code = str(decision.get("error_code") or "FINALIZE_REPAIR_EXHAUSTED")
        owner = str(decision.get("error_owner") or "controller")
        protocol["terminal_error_code"] = code
        protocol["terminal_owner"] = owner
        protocol["terminal_diagnostic"] = deepcopy(dict(decision.get("terminal_diagnostic") or {}))
        protocol["pending_ambiguity_id"] = ""
        protocol["compile_outcome"] = {
            **dict(protocol.get("compile_outcome") or {}),
            "status": "blocked",
            "mode": protocol.get("compile_mode"),
            "protocol_status": "blocked",
            "pending_ambiguity_id": "",
            "committed_at": now,
        }
        protocol["aggregate"]["status"] = "blocked"
    else:
        raise ValueError(f"Unsupported schema-v4 transition: {transition}")

    protocol["updated_at"] = now
    _sync_finalize_v4_summaries(protocol)
    validate_finalize_protocol_v4(protocol)
    updated["finalize_protocol"] = protocol
    updated["pending_human_question"] = None if transition != "needs_user" else updated.get("pending_human_question")
    updated["updated_at"] = now
    return updated


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
        "finalize_protocol": None,
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
        item = runtime.get(skill_id) if isinstance(runtime.get(skill_id), Mapping) else {}
        item_status = str((item or {}).get("status") or "").strip().lower()
        ready = item_status in {"ready", "verified", "passed", "satisfied", "success"}
        if requirement_id in requirements:
            existing = requirements[requirement_id]
            if ready and str(existing.get("status") or "") not in TERMINAL_REQUIREMENT_STATUSES:
                existing.update({
                    "status": "satisfied",
                    "satisfied_by": ["structured_runtime_evidence"],
                    "evidence_provenance": {
                        "kind": "structured_runtime_evidence",
                        "skill_id": skill_id,
                        "digest": _digest(item),
                    },
                    "freshness": "current_runtime",
                    "updated_at": _now(),
                })
            continue
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


def build_consultant_candidate(
    current_task_spec: Mapping[str, Any] | None,
    consultation_state: Mapping[str, Any],
    task_spec_proposal: Mapping[str, Any],
    *,
    selected_skills: Sequence[str],
    task_id: str,
    raw_submission_ref: str = "",
    source_refs: Sequence[str] = (),
    renderer_records: Sequence[Mapping[str, Any]] = (),
    input_audit: Optional[Mapping[str, Any]] = None,
    runtime_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    pending_question: Optional[Mapping[str, Any]] = None,
    project_root: str = "",
    legacy_renderer_mode: bool = False,
) -> Dict[str, Any]:
    """Build the complete candidate before evaluating Consultant FINISH.

    The function is deliberately pure: no graph/durable state is modified.
    The caller may commit valid full or partial progress through the existing
    PlanTracker CAS boundary.
    """
    base_revision = task_spec_revision(current_task_spec) if current_task_spec else 0
    base_hash = task_semantic_hash(current_task_spec or {})
    task_spec = apply_task_spec_mutation_proposal(
        current_task_spec,
        task_spec_proposal,
        task_id=task_id,
        raw_submission_ref=raw_submission_ref,
        source_refs=source_refs,
        project_root=project_root or None,
    )
    records = [dict(item) for item in renderer_records if isinstance(item, Mapping)]
    if legacy_renderer_mode and records:
        task_spec = apply_declarative_task_spec_rules(task_spec, records)
    if task_semantic_hash(task_spec) != base_hash:
        task_spec["revision"] = base_revision + 1

    selected = list(dict.fromkeys(
        str(item).strip() for item in selected_skills if str(item).strip()
    ))
    consultation = seed_structured_evidence(
        consultation_state,
        input_audit=input_audit,
        routing_candidate_ids=selected,
        runtime_evidence=runtime_evidence,
    )
    consultation["resolved_selected_skill_ids"] = list(selected)
    resolved_bindings: Dict[str, Any] = {}
    for record in records if legacy_renderer_mode else []:
        skill_id = str(record.get("skill_id") or "")
        invocation = record.get("invocation") if isinstance(record.get("invocation"), Mapping) else {}
        projection = project_task_spec_to_renderer(
            task_spec,
            invocation,
            task_id=task_id,
        )
        resolved_bindings[skill_id or str(record.get("renderer_id") or "renderer")] = projection

    consultation = evaluate_consultation_completion(consultation)
    pending = deepcopy(dict(pending_question or {}))
    if pending:
        consultation.update({
            "pending_question": pending,
            "pending_human_question": pending,
            "phase": "needs_user",
            "consultation_status": "needs_user",
            "completion_decision": "needs_user",
            "completion_reason": "validated_user_owned_semantic_decision",
        })
    else:
        consultation["pending_question"] = None
    consultation["updated_at"] = _now()
    return {
        "task_spec": task_spec,
        "selected_skills": selected,
        "resolved_bindings": resolved_bindings,
        "pending_question": pending,
        "output_contract_violations": validate_task_spec_output_contracts(task_spec),
        "consultation_state": consultation,
        "completion": {
            "decision": consultation.get("completion_decision"),
            "reason": consultation.get("completion_reason"),
            "unresolved_requirement_ids": list(
                consultation.get("unresolved_requirement_ids") or []
            ),
        },
    }


def evaluate_consultant_candidate_route(
    candidate: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Apply the one completion route gate shared by FINISH and SELECT_SKILL.

    This function does not mutate the candidate.  In particular, a selected
    skill is insufficient by itself: a canonical TaskSpec, output contract,
    runtime/controller evidence, and absence of a pending user question are all
    still required before Planner routing is legal.
    """

    current = dict(candidate or {})
    violations = [
        dict(item) for item in current.get("output_contract_violations") or []
        if isinstance(item, Mapping)
    ]
    if violations:
        first = violations[0]
        return {
            "route": "consultant",
            "code": str(first.get("code") or "TASK_OUTPUT_BINDING_INCOMPLETE"),
            "reason": str(first.get("message") or "Repair the typed TaskSpec output contract."),
        }
    if not task_spec_is_initialized(current.get("task_spec")):
        return {
            "route": "consultant",
            "code": "CANONICAL_TASKSPEC_REQUIRED",
            "reason": "Skill selection is valid, but the canonical TaskSpec is not complete.",
        }
    pending = current.get("pending_question")
    consultation = dict(current.get("consultation_state") or {})
    pending = pending or consultation.get("pending_question") or consultation.get("pending_human_question")
    if isinstance(pending, Mapping) and pending:
        return {
            "route": "consultant",
            "code": "PENDING_USER_QUESTION",
            "reason": "A validated user-owned question remains pending.",
        }
    completion = dict(current.get("completion") or {})
    if str(completion.get("decision") or "") != "ready_to_finalize":
        unresolved = list(completion.get("unresolved_requirement_ids") or [])
        return {
            "route": "consultant",
            "code": "FINISH_CANDIDATE_INCOMPLETE",
            "reason": (
                "Candidate completion still has unresolved Controller/runtime evidence: "
                + (", ".join(str(item) for item in unresolved) or "unknown")
            ),
            "unresolved_requirement_ids": unresolved,
        }
    return {
        "route": "planner",
        "code": "CONSULTANT_CANDIDATE_COMPLETE",
        "reason": "The shared deterministic Consultant completion contract passed.",
    }


def evaluate_consultant_ask(
    action: Mapping[str, Any],
    *,
    capabilities: Sequence[Mapping[str, Any]],
    resolved_bindings: Optional[Mapping[str, Any]] = None,
    task_spec: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Allow only unresolved, required, user-owned semantic questions."""
    requested = list(dict.fromkeys(
        str(item).strip() for item in action.get("requested_slots", []) or []
        if str(item).strip()
    ))
    if not requested:
        return {
            "allowed": False,
            "code": "ASK_REQUESTED_SLOTS_REQUIRED",
            "resolved_slots": {},
        }

    arguments: Dict[str, Dict[str, Any]] = {}
    for capability in capabilities or []:
        if not isinstance(capability, Mapping):
            continue
        for item in [
            *list(capability.get("required_bindings", []) or []),
            # Read-only compatibility for persisted legacy capability cards.
            *list(capability.get("renderer_arguments", []) or []),
        ]:
            if isinstance(item, Mapping) and str(item.get("name") or ""):
                arguments[str(item["name"])] = dict(item)

    flat_resolved: Dict[str, Any] = {}
    for key, value in dict(resolved_bindings or {}).items():
        if isinstance(value, Mapping) and isinstance(value.get("arguments"), Mapping):
            flat_resolved.update(dict(value["arguments"]))
        else:
            flat_resolved[str(key)] = value
    known_slots = set(flat_resolved)
    current = dict(task_spec or {})
    known_slots.update(
        str(item.get("role") or "")
        for item in [*(current.get("inputs") or []), *(current.get("output_contracts") or [])]
        if isinstance(item, Mapping) and str(item.get("role") or "")
    )
    known_slots.update(
        str(item.get("semantic_key") or "")
        for item in [*(current.get("requirements") or []), *(current.get("confirmed_decisions") or [])]
        if isinstance(item, Mapping) and str(item.get("semantic_key") or "")
    )

    resolved_requested = {
        slot: flat_resolved.get(slot) or flat_resolved.get(slot.rsplit(".", 1)[-1])
        for slot in requested
        if flat_resolved.get(slot) not in (None, "")
        or flat_resolved.get(slot.rsplit(".", 1)[-1]) not in (None, "")
    }
    for slot in requested:
        name = slot.rsplit(".", 1)[-1]
        meta = arguments.get(slot) or arguments.get(name)
        if slot in known_slots or name in known_slots or slot in resolved_requested:
            code = (
                "INVALID_ASK_CONTROLLER_OWNED_SLOT"
                if meta and str(meta.get("owner") or "") == "controller"
                else "INVALID_ASK_ALREADY_KNOWN_SLOT"
            )
            return {"allowed": False, "code": code, "resolved_slots": resolved_requested}
        if meta is None:
            return {"allowed": False, "code": "INVALID_ASK_UNKNOWN_SLOT", "resolved_slots": resolved_requested}
        owner = str(meta.get("owner") or "user")
        if owner == "controller":
            return {"allowed": False, "code": "INVALID_ASK_CONTROLLER_OWNED_SLOT", "resolved_slots": resolved_requested}
        if owner == "runtime":
            return {"allowed": False, "code": "INVALID_ASK_RUNTIME_OWNED_SLOT", "resolved_slots": resolved_requested}
        if not bool(meta.get("required_for_execution", meta.get("required"))):
            return {"allowed": False, "code": "INVALID_ASK_OPTIONAL_SLOT", "resolved_slots": resolved_requested}
        if not bool(meta.get("user_input_required", owner == "user")):
            return {"allowed": False, "code": "INVALID_ASK_NOT_USER_REQUIRED", "resolved_slots": resolved_requested}
    return {"allowed": True, "code": "ASK_USER_SLOT_UNRESOLVED", "resolved_slots": {}}


def consultant_no_progress_decision(
    previous: Mapping[str, Any] | None,
    *,
    task_revision: int,
    conversation_turn_count: int,
    feedback: Sequence[Mapping[str, Any]],
    route: str,
    discovery_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Bound a retry only when semantic and discovery evidence are unchanged."""
    prior = dict(previous or {})
    current = {
        "task_revision": int(task_revision or 0),
        "conversation_turn_count": int(conversation_turn_count or 0),
        "feedback": deepcopy(list(feedback or [])),
        "route": str(route or "consultant"),
        "skill_discovery": skill_discovery_progress_projection(discovery_state),
    }
    equivalent = all(prior.get(key) == value for key, value in current.items())
    if equivalent and bool(prior.get("retry_pending")):
        return {"allow": False, "code": "CONSULTANT_NO_PROGRESS", "guard": current}
    return {"allow": True, "code": "ALLOW", "guard": current}


def begin_finalize(
    state: Mapping[str, Any],
    *,
    finalize_context_id: str = "",
    base_contract_hash: str = "",
    wire_schema_version: int = 2,
    max_wire_repair_attempts: int = 2,
    max_field_repair_attempts: int = 2,
) -> Dict[str, Any]:
    updated = evaluate_consultation_completion(state)
    if updated.get("completion_decision") != "ready_to_finalize":
        raise ValueError("Consultation cannot enter FINALIZE with unresolved mandatory evidence")
    updated["phase"] = "finalize"
    if finalize_context_id or base_contract_hash:
        if not finalize_context_id or not base_contract_hash:
            raise ValueError("Finalize protocol initialization requires both context ID and base hash")
        updated = initialize_finalize_protocol(
            updated,
            finalize_context_id=finalize_context_id,
            base_contract_hash=base_contract_hash,
            wire_schema_version=wire_schema_version,
            max_wire_repair_attempts=max_wire_repair_attempts,
            max_field_repair_attempts=max_field_repair_attempts,
        )
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

"""Exact Planner correction records in the existing immutable state store."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping, Sequence

from langchain_core.messages import AIMessage, SystemMessage, messages_from_dict, messages_to_dict
from agent_core.state_contract_store import StateContractStore
from agent_core.task_spec import task_semantic_hash


def correction_feedback(state: Mapping[str, Any]) -> dict[str, Any]:
    task = dict(state.get("task_state") or {})
    failure = dict(task.get("planning_failure") or {})
    epoch = dict(task.get("planning_epoch") or {})
    result = {
        "code": "PLANNER_CANDIDATE_REJECTED",
        "validation_issues": deepcopy(failure.get("validation_issues") or []),
        "requirements": deepcopy(failure.get("requirements") or []),
        "correction_budget": {
            "planner_calls_total": int(epoch.get("planner_calls_total") or 0),
            "next_candidate_ordinal": int(epoch.get("planner_calls_total") or 0) + 1,
            "nominal_call_limit": int((task.get("budgets") or {}).get("max_planner_calls_per_epoch", 4)),
            "limit_condition": "hard total, including the first request; fourth valid response may be accepted; no fifth request",
            "remaining_requests": max(0, min(4, int((task.get("budgets") or {}).get("max_planner_calls_per_epoch", 4))) - int(epoch.get("planner_calls_total") or 0)),
            "active_stop_available": True,
            "cycle_detected": bool(failure.get("cycle_detected")),
            "repeat_count": int(failure.get("repeat_count") or 0),
        },
    }
    integrity = dict(failure.get("response_integrity") or {})
    if integrity:
        from agent_core.planner_recovery import length_feedback
        result["response_completion"] = {
            "finish_reason": str(integrity.get("finish_reason") or "UNKNOWN"),
            "content_status": str(integrity.get("content_status") or "UNKNOWN"),
            "reported_usage": deepcopy(integrity.get("reported_usage") or {"status": "UNKNOWN"}),
            "parser_diagnostic": deepcopy(integrity.get("parser_diagnostic") or {"status": "not_run"}),
        }
        if integrity.get("finish_reason") == "length":
            result["length_recovery"] = length_feedback(integrity, failure.get("remediation_authorization") or {})
    return result


def save_correction_context(data_path: str, task_id: str, state: dict[str, Any], *,
                            task_spec: Mapping[str, Any], raw: str,
                            candidate: Mapping[str, Any], provider_messages: Sequence[Any],
                            fixed_prefix_count: int, capabilities: Mapping[str, Any],
                            response_metadata: Mapping[str, Any] | None = None,
                            call_claim: Mapping[str, Any] | None = None,
                            remediation: Mapping[str, Any] | None = None,
                            consumption_review_baselines: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    integrity = deepcopy(dict(response_metadata or {}))
    if integrity:
        from agent_core.planner_recovery import attach_rejection_facts
        # One controlled immutable response object provides a non-circular raw
        # reference. It is not an admission receipt or an Evidence Registry.
        integrity["raw_ref"] = StateContractStore(data_path, task_id).put_object(
            "planner_response_evidence", {"schema_version": 1, "raw_candidate": raw,
                                          "response_integrity": deepcopy(integrity)})
        attach_rejection_facts(state, integrity, call_claim or {}, remediation)
    feedback = correction_feedback(state)
    complete = [*provider_messages, AIMessage(content=raw), SystemMessage(content=json.dumps(feedback, ensure_ascii=False, sort_keys=True))]
    record = {
        "schema_version": 2 if integrity else 1, "task_id": task_id,
        "planning_epoch_id": (state.get("task_state") or {}).get("planning_epoch", {}).get("planning_epoch_id", ""),
        "task_semantic_hash": task_semantic_hash(task_spec),
        "capability_digest": hashlib.sha256(json.dumps(capabilities, sort_keys=True).encode()).hexdigest(),
        "raw_candidate": raw, "candidate": deepcopy(dict(candidate)),
        "feedback": feedback, "messages": messages_to_dict(complete),
        "fixed_prefix_count": fixed_prefix_count,
    }
    baselines = [deepcopy(dict(row)) for row in consumption_review_baselines]
    read_issues = [deepcopy(row) for row in feedback["validation_issues"] if row.get("code") == "PLAN_ARTIFACT_UNAVAILABLE"]
    if read_issues and not any(row.get("raw_candidate") == raw for row in baselines):
        baselines.append({"raw_candidate": raw, "validation_issues": read_issues})
    if baselines:
        record["consumption_review_baselines"] = baselines
    if integrity:
        failure = (state.get("task_state") or {}).get("planning_failure") or {}
        record.update({"response_integrity": integrity,
                       "occurrence_receipt": deepcopy(failure.get("occurrence_receipt") or {}),
                       "remediation_authorization": deepcopy(failure.get("remediation_authorization") or {})})
    ref = StateContractStore(data_path, task_id).put_object("planner_correction_context", record)
    failure = state.setdefault("task_state", {}).setdefault("planning_failure", {})
    failure["context_ref"] = ref
    failure["candidate_source"] = {
        "context_ref": ref, "raw_candidate_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "planning_epoch_id": record["planning_epoch_id"],
        "candidate_ordinal": int((state.get("task_state") or {}).get("planning_epoch", {}).get("planner_calls_total") or 0),
    }
    primary = failure.get("primary_failure") or {}
    if primary.get("status") == "observed" and int(failure.get("attempt") or 0) == 1:
        failure["primary_context_ref"] = ref
        primary["candidate_source"] = deepcopy(failure["candidate_source"])
    return feedback


def load_correction_context(data_path: str, task_id: str, state: Mapping[str, Any], *,
                            task_spec: Mapping[str, Any], capabilities: Mapping[str, Any]) -> dict[str, Any] | None:
    task = state.get("task_state") or {}
    failure = task.get("planning_failure") or {}
    if failure.get("status") != "unresolved" or not failure.get("context_ref"):
        return None
    record = StateContractStore(data_path, task_id).read_object(failure["context_ref"], expected_type="planner_correction_context")
    if (record.get("task_id") != task_id
        or record.get("planning_epoch_id") != (task.get("planning_epoch") or {}).get("planning_epoch_id")
        or record.get("task_semantic_hash") != task_semantic_hash(task_spec)
        or record.get("capability_digest") != hashlib.sha256(json.dumps(capabilities, sort_keys=True).encode()).hexdigest()):
        return None  # A changed semantic/contract basis requires the normal new epoch.
    if int(record.get("schema_version") or 1) < 2:
        # Compatibility projection only. Never backfill a historical object
        # from the current provider or another task's last response.
        record = deepcopy(record)
        record["response_integrity"] = {"schema_version": 1, "finish_reason": "UNKNOWN",
                                        "content_status": "UNKNOWN", "reported_usage": {"status": "UNKNOWN"},
                                        "source": "legacy_metadata_not_persisted"}
    return record


def restore_correction_messages(context: Any, record: Mapping[str, Any]) -> list[Any]:
    messages = messages_from_dict(record["messages"])
    # Direct producer callers may not have phase-assigned message IDs. These
    # deterministic transport IDs do not change any provider-visible content.
    for index, message in enumerate(messages):
        if not message.id:
            identity = hashlib.sha256((str(record.get("planning_epoch_id")) + ":" + str(index) + ":" + str(message.content)).encode()).hexdigest()
            messages[index] = message.model_copy(update={"id": "planner-context:" + identity})
    if context is not None and not context.messages and context.system_message is None:
        count = int(record["fixed_prefix_count"])
        # A fresh phase has new transport-channel IDs. Rebind only those local
        # IDs before initializing its fixed channels; provider-visible payload
        # and the saved immutable context remain byte-for-byte unchanged.
        if getattr(context, "epoch_id", None):
            messages[0] = messages[0].model_copy(update={"id": f"{context.epoch_id}:system"})
            messages[1] = messages[1].model_copy(update={"id": f"{context.epoch_id}:seed"})
        context.system_message, context.seed_message = messages[:2]
        context.inherited_messages = messages[2:count]
        context.messages = messages[count:]
    return messages

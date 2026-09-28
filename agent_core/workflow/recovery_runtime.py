"""Recovery resource receipts and historical revalidation facts.

Pi owns model/tool dispatch.  The retained helpers account for bounded resources
and interpret saved evidence; no legacy agent, recovery loop or graph adapter is
exposed here.  Existing record formats and resource-limit exceptions are kept.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from copy import deepcopy
from typing import Any, Mapping

RECOVERY_RESOURCES = ContextVar("biomind_recovery_resources", default=None)
RECOVERY_POLICY_RECEIPT = ContextVar("biomind_recovery_policy_receipt", default=None)
RECOVERY_DISPATCH_BUDGET = ContextVar("biomind_recovery_dispatch_budget", default=None)


@dataclass(frozen=True)
class RecoveryResourcePolicy:
    """Explicit candidate, disabled unless selected at the production entry."""
    name: str = "phase_e_candidate_v2"
    soft_tokens: int = 300_000
    hard_tokens: int = 450_000
    soft_seconds: float = 120
    hard_seconds: float = 180
    max_output_tokens: int = 50_000
    receive_char_limit: int = 1_048_576
    public_char_limit: int = 1200
    decision_seconds: float = 30

    def __post_init__(self):
        if not (0 < self.soft_tokens < self.hard_tokens and 0 < self.soft_seconds < self.hard_seconds
                and 0 < self.decision_seconds <= self.hard_seconds and self.max_output_tokens > 0
                and self.receive_char_limit > 0 and self.public_char_limit > 0):
            raise ValueError("RECOVERY_RESOURCE_POLICY_INVALID")


def configured_recovery_policy():
    name = os.environ.get("BIOMIND_RECOVERY_RESOURCE_POLICY", "disabled")
    if name == "disabled":
        return None
    if name != "phase_e_candidate_v2":
        raise ValueError("RECOVERY_RESOURCE_POLICY_UNKNOWN")
    return RecoveryResourcePolicy()


def resolve_recovery_budget_policy(policy, *, clients=(), resource_policy=None):
    """Choose one explicit local output cap before Recovery request admission."""
    from agent_core.output_policy_facts import output_policy_projection
    requested = policy.requested_output_limit_tokens
    limits = [requested if requested is not None else policy.output_reserve_tokens]
    if policy.known_provider_output_limit_tokens is not None:
        limits.append(policy.known_provider_output_limit_tokens)
    if resource_policy is not None:
        limits.append(resource_policy.max_output_tokens)
    for client in clients:
        resolved = output_policy_projection(client)
        if resolved["configuration_ambiguity"] or resolved["client_parameter"] not in {"UNKNOWN", "max_tokens"}:
            raise ValueError("RECOVERY_OUTPUT_PARAMETER_CONFLICT")
        value = resolved["client_resolved_output_limit"]
        # Local inherited defaults do not establish provider capability.
        if requested is None and type(value) is int and value > 0:
            limits.append(value)
    if min(limits) <= 0:
        raise ValueError("RECOVERY_OUTPUT_LIMIT_REQUIRED")
    return replace(policy, output_reserve_tokens=min(limits))


def configured_recovery_budget_policy():
    """Standalone defaults without loading application credentials."""
    from agent_core.workflow.context_compaction import ContextBudgetPolicy
    from agent_core.llm_safety import MODEL_OUTPUT_LIMIT_TOKENS
    requested = min(MODEL_OUTPUT_LIMIT_TOKENS, int(os.getenv("BIOMIND_RECOVERY_OUTPUT_LIMIT_TOKENS", str(MODEL_OUTPUT_LIMIT_TOKENS))))
    raw_cap = os.getenv("BIOMIND_RECOVERY_PROVIDER_OUTPUT_LIMIT_TOKENS", "").strip()
    from agent_core.workflow.context_compaction import configured_context_policy
    return replace(configured_context_policy(output_reserve_tokens=requested),
        requested_output_limit_tokens=requested,
        known_provider_output_limit_tokens=int(raw_cap) if raw_cap else None,
        output_capability_source="configured_provider_limit" if raw_cap else "not_verified")


@contextmanager
def recovery_dispatch_budget(policy, estimator, *, stage, tools=(), inherited_count=0):
    """Carry the admitted local policy through the existing final adapter."""
    token = RECOVERY_DISPATCH_BUDGET.set({"policy": policy, "estimator": estimator,
        "stage": stage, "tools": list(tools), "inherited_count": inherited_count})
    try:
        yield
    finally:
        RECOVERY_DISPATCH_BUDGET.reset(token)


def recovery_dispatch_receipt(messages, *, stage, client=None):
    """Recheck the complete final wire and explicit cap without vendor claims."""
    scope = RECOVERY_DISPATCH_BUDGET.get()
    if scope is None or scope["stage"] != stage:
        return None
    from agent_core.output_policy_facts import output_policy_projection
    from agent_core.workflow.context_compaction import measure_provider_input, measure_compactor_wire_tokens, ContextCompactionCapacityError
    from agent_core.workflow.scoped_context import provider_visible_payload
    policy, estimator = scope["policy"], scope["estimator"]
    resolved = output_policy_projection(client)
    if resolved["configuration_ambiguity"] or resolved["client_parameter"] not in {"UNKNOWN", "max_tokens"}:
        raise ValueError("RECOVERY_OUTPUT_PARAMETER_CONFLICT")
    explicit = resolved["client_resolved_output_limit"]
    if policy.requested_output_limit_tokens is None and type(explicit) is int and explicit < policy.output_reserve_tokens:
        raise ValueError("RECOVERY_OUTPUT_POLICY_CHANGED_AFTER_ADMISSION")
    tools = list(scope["tools"])
    # The bound client is the source of actual dispatched tool definitions.
    current, seen = client, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        kwargs = getattr(current, "kwargs", None)
        if isinstance(kwargs, Mapping) and "tools" in kwargs:
            tools = list(kwargs["tools"] or ())
            break
        current = getattr(current, "bound", None)
    if stage == "context_compaction":
        tokens = measure_compactor_wire_tokens(messages, policy=policy, estimator=estimator)
        safe = min(policy.safe_input_tokens, policy.compressor_safe_input_tokens or policy.safe_input_tokens)
        components = {"complete_compactor_wire": tokens}
    else:
        wire = [provider_visible_payload(message) for message in messages]
        inherited = int(scope["inherited_count"])
        snapshot = measure_provider_input({
            "system": wire[0] if wire else {}, "frozen_inputs": wire[1] if len(wire) > 1 else {},
            "inherited_materials": wire[2:2 + inherited], "local_messages": wire[2 + inherited:],
            "tool_schema": tools, "pending_results": [], "provider_wrappers": {},
        }, policy=policy, estimator=estimator)
        tokens, safe = snapshot.estimated_input_tokens, policy.safe_input_tokens
        components = snapshot.component_token_counts
    receipt = {**policy.to_dict(), "failure_stage": "recovery_compactor_admission" if stage == "context_compaction" else "recovery_provider_admission",
        "estimated_input_tokens": tokens, "effective_output_limit_tokens": policy.output_reserve_tokens,
        "component_token_counts": dict(components), "safe_input_tokens": safe,
        "provider_message_count": len(messages), "tool_definition_count": len(tools),
        "estimator_name": estimator.name, "estimator_version": estimator.version,
        "exact_or_estimated": estimator.exact_or_estimated, "remote_effective_limit": "UNKNOWN",
        "output_cap_adjustment": ("explicit_provider_or_resource_limit" if
            policy.requested_output_limit_tokens is not None and policy.output_reserve_tokens < policy.requested_output_limit_tokens
            else "none"), "wire_output_parameter": "max_tokens", "wire_output_limit": policy.output_reserve_tokens}
    if tokens > safe:
        raise ContextCompactionCapacityError("RECOVERY_PROVIDER_INPUT_UNSAFE", json.dumps(receipt, sort_keys=True), feedback=receipt)
    return receipt


def recovery_policy_receipt(policy, *, explicit=False):
    return {"enabled": policy is not None,
            "name": policy.name if policy else "disabled",
            "thresholds": asdict(policy) if policy else {},
            "source": "node_argument" if explicit else (
                "environment_after_configuration_merge" if "BIOMIND_RECOVERY_RESOURCE_POLICY" in os.environ else "default"),
            "upstream_environment_origin": "NOT_RECORDED" if not explicit and "BIOMIND_RECOVERY_RESOURCE_POLICY" in os.environ else "not_applicable",
            "scope": "causal_incident", "accounting": "processing_tokens_not_invoice"}


class RecoveryResourceLimit(ValueError):
    code = "RECOVERY_RESOURCE_LIMIT"


def recovery_resource_begin(store, incident_id, policy):
    owner = uuid.uuid4().hex
    lease = "recovery-resources-" + incident_id
    store.acquire_finalize_provider_lease(lease)
    def begin(row):
        if not row and store.recovery_unaccounted_attempts():
            raise RecoveryResourceLimit("Prior transport usage has no parent ledger; no fresh budget is inferred")
        if row and row.get("policy") != asdict(policy):
            raise RecoveryResourceLimit("Existing causal ledger uses a different policy")
        row.setdefault("policy", asdict(policy))
        row.setdefault("attempts", {})
        row.setdefault("business", {})
        row.setdefault("elapsed_seconds", 0.0)
        if row.get("active"):
            # A prior process interval cannot be measured with our monotonic
            # clock. Retain its bounded outstanding reservation, not downtime.
            row["elapsed_seconds"] += row["active"]["reserved_seconds"]
            row["interrupted_time_unknown"] = True
        row["active"] = {"owner": owner, "reserved_seconds": max(0, policy.hard_seconds - row["elapsed_seconds"])}
    try:
        row = store.recovery_resource_update(incident_id, begin)
    except BaseException:
        store.release_finalize_provider_lease(lease)
        raise
    scope = {"store": store, "incident": incident_id, "policy": policy, "owner": owner,
             "started": time.monotonic(), "base_seconds": row["elapsed_seconds"], "row": row, "lease": lease}
    return scope, RECOVERY_RESOURCES.set(scope)


def recovery_resource_update(update=lambda row: None, *, locked=False):
    scope = RECOVERY_RESOURCES.get()
    if scope is None:
        return {}
    row = scope["store"].recovery_resource_update(scope["incident"], update, locked=locked)
    scope["row"] = row
    return row


def recovery_business_checkpoint(values):
    # Resource persistence contains counters/identities only. Full observations
    # remain in the existing phase archive; this is not another history store.
    values = {key: value for key, value in values.items() if key in {
        "turn_count", "tool_call_count", "transport_attempt_count", "format_correction_count", "output_resubmission_count",
        "no_progress_rounds", "tool_request_counts", "progress_signatures", "observation_chars",
        "compaction_maintenance", "compaction_bypass_remaining", "compaction_failures"}}
    def checkpoint(row):
        prior = row["business"]
        counters = {key: max(int(prior.get(key) or 0), int(values.get(key) or 0))
                    for key in ("turn_count", "tool_call_count", "transport_attempt_count", "format_correction_count", "output_resubmission_count")}
        prior.update(deepcopy(values))
        prior.update(counters)
    return recovery_resource_update(checkpoint)


def recovery_resource_end(scope, token):
    def finish(row):
        if (row.get("active") or {}).get("owner") != scope["owner"]:
            return
        row["elapsed_seconds"] = scope["base_seconds"] + time.monotonic() - scope["started"]
        row["active"] = None
    try:
        recovery_resource_update(finish)
    finally:
        RECOVERY_RESOURCES.reset(token)
        scope["store"].release_finalize_provider_lease(scope["lease"])


def recovery_seconds_remaining():
    scope = RECOVERY_RESOURCES.get()
    if scope is None:
        return float("inf")
    return max(0.0, scope["policy"].hard_seconds - scope["base_seconds"] - (time.monotonic() - scope["started"]))


def recovery_dispatch_seconds():
    scope = RECOVERY_RESOURCES.get()
    remaining = recovery_seconds_remaining()
    if scope is not None and not scope["row"].get("closing"):
        remaining = max(0.0, remaining - scope["policy"].decision_seconds)
    return remaining


def recovery_token_occupancy(row):
    return sum(sum(attempt.get("usage", {}).get(k, estimate) for k, estimate in attempt["reservation"].items())
               for attempt in row.get("attempts", {}).values())


def recovery_request_estimate(messages, *, tools=()):
    from agent_core.workflow.context_compaction import ReferenceTokenEstimator
    body = json.dumps({"messages": [m if isinstance(m, Mapping) else m.model_dump() for m in messages],
                       "tools": list(tools)}, ensure_ascii=False, default=str)
    estimate = ReferenceTokenEstimator().estimate(body)
    return estimate.tokens + estimate.uncertainty_tokens + 2048


def recovery_reserve(attempt_id, *, input_tokens, output_tokens, stage, locked=False, invocation_id="", wire_fingerprint="", context_epoch_id=""):
    scope = RECOVERY_RESOURCES.get()
    if scope is None:
        return
    def reserve(row):
        if attempt_id in row["attempts"]:
            return
        if row.get("terminal_reason") or recovery_seconds_remaining() <= 0:
            raise RecoveryResourceLimit("Causal investigation is closed or out of time")
        if recovery_token_occupancy(row) + input_tokens + output_tokens > scope["policy"].hard_tokens:
            raise RecoveryResourceLimit("Full physical request exceeds remaining causal tokens")
        row["attempts"][attempt_id] = {"stage": stage, "ordinal": len(row["attempts"])+1, "status": "reserved_unknown",
            "invocation_id": invocation_id, "wire_fingerprint": wire_fingerprint, "context_epoch_id": context_epoch_id,
            "reservation": {"input_tokens": input_tokens, "output_tokens": output_tokens}, "usage": {}}
    recovery_resource_update(reserve, locked=locked)


def recovery_settle(attempt_id, usage=None, *, not_dispatched=False):
    """Idempotent physical accounting, including late usage; never admits output."""
    if RECOVERY_RESOURCES.get() is None:
        return
    usage = dict(usage or {})
    def settle(row):
        attempt = row["attempts"].get(attempt_id)
        if not attempt:
            raise RecoveryResourceLimit("Physical receipt has no durable reservation")
        if not_dispatched:
            attempt.update(status="not_dispatched", reservation={"input_tokens": 0, "output_tokens": 0})
            return
        for name, alias in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
            value = usage.get(name, usage.get(alias))
            if type(value) is int and value >= 0:
                attempt["usage"][name] = value
        attempt["status"] = "settled" if len(attempt["usage"]) == 2 else "usage_unknown"
        if recovery_token_occupancy(row) > row["policy"]["hard_tokens"]:
            row["overreported_usage"] = True
    recovery_resource_update(settle)


def recovery_replay_account(claim, *, input_tokens, output_tokens, stage):
    """Adopt a historical transport receipt without issuing or charging a replay.

    Missing prior usage remains reserved UNKNOWN; it is never silently zero.
    """
    from langchain_core.messages import messages_from_dict
    response = messages_from_dict([claim["response"]])[0]
    def adopt(row):
        for attempt in claim["attempts"]:
            row["attempts"].setdefault(attempt["attempt_id"], {"stage": stage, "ordinal": len(row["attempts"])+1,
                "status": "historical_usage_unknown", "usage": {},
                "reservation": {"input_tokens": input_tokens, "output_tokens": output_tokens}})
    recovery_resource_update(adopt)
    recovery_settle(claim["attempts"][-1]["attempt_id"], getattr(response, "usage_metadata", None)
        or (getattr(response, "response_metadata", {}) or {}).get("token_usage"))


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:24]


def _full_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical(value).encode("utf-8")
    ).hexdigest()


def revalidation_fingerprint(projection: Any) -> str:
    """Verifier inputs, not report IDs, read handles or model wording."""
    payload = projection.source.get("payload") or {}
    report = payload.get("completion_report") or {}
    if not report.get("validation_digest"):
        return ""
    return _full_hash({
        "attempt": projection.recovery_seed.failure_packet.attempt_ref,
        "validation_digest": report["validation_digest"],
        "gate": payload.get("completion_gate"),
        "disposition": report.get("effective_disposition"),
        "inspection_facts": meaningful_inspection_facts(
            (report.get("effect_presence") or {}).get("revalidation_inspection_facts") or []),
    })


def meaningful_inspection_facts(rows):
    """Scoped observations, ignoring receipt identities and repeated failures.

    Callers supply the existing scope-checked projection, never free-form model
    claims. A changed factual/coverage result can advance a local check only.
    """
    volatile = {"event_id", "receipt_id", "request_id", "tool_call_id", "message_id",
                "observation_id", "timestamp", "created_at", "updated_at", "duration_seconds",
                "observed_at", "age_seconds", "timing", "activity_id",
                "content_sha256", "observation_ref", "source_message_id", "decision_ref",
                "receipt_sha256", "archive_ref", "result_chars", "max_result_chars",
                "receipt_ref", "command_receipt_ref", "log_refs"}
    def facts(value):
        if isinstance(value, Mapping):
            return {k: facts(v) for k, v in value.items() if k not in volatile}
        if isinstance(value, (list, tuple)):
            return [facts(v) for v in value]
        return value
    result = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        observation = row.get("observation", row)
        if not isinstance(observation, Mapping) or observation.get("error"):
            continue
        body = observation.get("result", observation)
        if not isinstance(body, Mapping) or body.get("status") in {
            "failed", "error", "not_dispatched", "REQUIRES_NARROWING",
            "not_run", "sandbox_error", "cancelled", "timed_out",
            "NO_MATCH", "RECOVERY_OBSERVATION_BUDGET_EXCEEDED",
            "body_not_projected_budget_requires_targeted_read"}:
            continue
        if "content" in body and not str(body["content"] or "").strip():
            continue  # A new empty range is not new evidence.
        if not set(body) - volatile - {"status", "start_line", "end_line", "cursor", "truncated"}:
            continue  # Coverage labels without returned facts do not advance.
        if observation.get("tool") in {"exec.command", "inspect.paths"}:
            observation = {k: v for k, v in observation.items() if k != "source_ref"}
        result.add(_canonical(facts(observation)))
    return sorted(result)


def revalidation_stop_reason(projection: Any, durable: Mapping[str, Any], limit: int) -> str:
    packet = projection.recovery_seed.failure_packet
    task = durable.get("task_state") or {}
    prior = (task.get("typed_recovery_progress") or {}).get(packet.incident_id) or {}
    if prior.get("action") != "revalidate":
        return ""
    fingerprint = revalidation_fingerprint(projection)
    if fingerprint and fingerprint == prior.get("revalidation_fingerprint"):
        return "RECOVERY_NO_PROGRESS"
    if int((task.get("typed_recovery_budget") or {}).get(packet.incident_id) or 0) >= limit:
        return "RECOVERY_BUDGET_EXHAUSTED"
    return ""

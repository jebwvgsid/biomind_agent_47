"""Deterministic admission runtime for route-blind Recovery directives.

The model-facing RecoveryAgent owns diagnosis and emits one strict
``RecoveryDirective``.  This module never infers a disposition from failure
text.  It validates the directive against the causal packet, preserves the
existing budget/CAS/idempotency boundary, and maps the semantic action into a
typed ``RecoveryOutcome`` for the graph's fixed conditional edges.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
import asyncio
import inspect
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from copy import deepcopy
from typing import Any, Callable, Mapping

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import load_plan_state, save_plan_state, bind_recovery_generation_authority
from agent_core.workflow.outcomes import (
    RecoveryOutcome,
    WORKFLOW_ROUTING_SCHEMA_TYPED,
    workflow_routing_schema_version,
)
from agent_core.workflow.recovery_projection import (
    append_causal_failure_lifecycle_transition,
    has_causal_failure_lifecycle_transition,
    project_recovery_seed,
)
from agent_core.workflow.stage_contracts import (
    RecoveryDirective,
    StageContractError,
)


Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]


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


RECOVERY_RESOURCES = ContextVar("biomind_recovery_resources", default=None)
RECOVERY_POLICY_RECEIPT = ContextVar("biomind_recovery_policy_receipt", default=None)
RECOVERY_DISPATCH_BUDGET = ContextVar("biomind_recovery_dispatch_budget", default=None)


def resolve_recovery_budget_policy(policy, *, clients=(), resource_policy=None):
    """Choose one explicit local output cap before Recovery request admission."""
    from agent_core.provider_attempt import output_policy_projection
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
    return ContextBudgetPolicy(output_reserve_tokens=requested,
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
    from agent_core.provider_attempt import output_policy_projection
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


async def recovery_direct_invoke(call, messages, *, stage, call_context=None, call_kwargs=None):
    """Standalone adapter uses the same physical journal as the production SDK.

    The production compactor already calls _ainvoke_llm; its caller must not
    charge this adapter as well. No child aggregate is charged here.
    """
    scope = RECOVERY_RESOURCES.get()
    kwargs = dict(call_kwargs or {})
    client = getattr(call, "__self__", None)
    receipt = recovery_dispatch_receipt(messages, stage=stage, client=client)
    if receipt:
        if hasattr(client, "bind"):
            call = client.bind(max_tokens=receipt["effective_output_limit_tokens"]).ainvoke
        else:
            signature = inspect.signature(call)
            if "max_tokens" in signature.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                kwargs["max_tokens"] = receipt["effective_output_limit_tokens"]
            else:
                from agent_core.workflow.context_compaction import ContextCompactionError
                raise ContextCompactionError("RECOVERY_OUTPUT_CAP_UNSUPPORTED",
                    "The provider callable cannot receive an explicit max_tokens policy", feedback=receipt)
    if scope is None:
        result = call(messages, **kwargs)
        return await result if inspect.isawaitable(result) else result
    from agent_core.provider_attempt import ProviderAttemptEnvelope, ProviderTimeoutPolicy, run_provider_attempt
    from langchain_core.messages import message_to_dict, messages_from_dict
    from agent_core.workflow.recovery_agent import recovery_tool_schemas
    policy = scope["policy"]
    context = dict(call_context or {})
    from agent_core.workflow.recovery_agent import _wire_fingerprint
    wire_hash = _wire_fingerprint(messages)
    epoch = str(context.get("context_epoch_id") or "")
    invocation = "recovery-direct-" + hashlib.sha256((scope["incident"]+stage+epoch+wire_hash).encode()).hexdigest()[:24]
    attempt_id = invocation + "-" + uuid.uuid4().hex[:8]
    estimate = recovery_request_estimate(messages, tools=recovery_tool_schemas() if stage == "recovery" else ())
    output = receipt["effective_output_limit_tokens"] if receipt else policy.max_output_tokens if stage == "recovery" else 50000
    reserve = lambda aid, locked=False: recovery_reserve(aid, input_tokens=estimate,
        output_tokens=output, stage=stage, locked=locked, invocation_id=invocation,
        wire_fingerprint=wire_hash, context_epoch_id=epoch)
    claim = scope["store"].recovery_transport_claim(invocation, wire_hash, attempt_id,
        maximum=int(context.get("maximum_transport_attempts") or 1), total_seconds=recovery_seconds_remaining(), reserve=reserve)
    if claim["status"] == "completed":
        recovery_replay_account(claim, input_tokens=estimate, output_tokens=output, stage=stage)
        return messages_from_dict([claim["response"]])[0]
    seconds = recovery_dispatch_seconds()
    envelope = ProviderAttemptEnvelope(node_name=stage, step_id=str(context.get("step_id") or ""),
        semantic_invocation_id=invocation, attempt_id=attempt_id, attempt_ordinal=1,
        parent_attempt_id="", call_id=attempt_id, node_context_hash="", request_hash=wire_hash,
        provider="standalone", model="explicit_adapter", thinking_policy={},
        timeout_policy=ProviderTimeoutPolicy(seconds, seconds, seconds, min(1, seconds)),
        retry_budget_before=0, retry_budget_after=0, sdk_max_retries=0,
        output_policy={"max_tokens": output, "recovery_dispatch_budget": receipt,
            **({"receive_char_limit": policy.receive_char_limit} if stage == "recovery" else {})})
    async def invoke(observer):
        options = dict(kwargs)
        signature = inspect.signature(call)
        flexible = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        if "config" in signature.parameters or flexible:
            options["config"] = {"callbacks": [observer]}
        if (stage == "recovery" or receipt) and ("max_tokens" in signature.parameters or flexible):
            options["max_tokens"] = output
        response = call(messages, **options)
        return await response if inspect.isawaitable(response) else response
    try:
        response, record = await run_provider_attempt(invoke, envelope)
    except BaseException as exc:
        record = getattr(exc, "record", None) or getattr(exc, "provider_attempt_record", None)
        recovery_settle(attempt_id, getattr(record, "provider_usage", {}))
        # Cancel/unknown remains fenced by the existing in-flight journal.
        if not isinstance(exc, asyncio.CancelledError):
            scope["store"].recovery_transport_finish(invocation, attempt_id,
                failure={"code": str(getattr(exc, "error_code", type(exc).__name__))},
                retry=bool(getattr(exc, "retryable", False)))
        raise
    recovery_settle(attempt_id, record.provider_usage)
    scope["store"].recovery_transport_finish(invocation, attempt_id, response=message_to_dict(response))
    return response


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


def _decision_wire(wire):
    from langchain_core.messages import HumanMessage
    return [*wire, HumanMessage(content="<recovery_resource_closure>调查预算接近边界。仅用当前证据提交一个合法决定；不再调用调查工具。无法解决时提交 blocked，明确尚未解决的事实。</recovery_resource_closure>")]


def recovery_replay_wire(wire, *, context_epoch_id=""):
    """Defer completed/UNKNOWN consumption to the existing transport owner.

    Only a recorded immutable wire can bypass a *new dispatch* budget check.
    No response/directive is admitted here, including after late cancellation.
    """
    from agent_core.workflow.recovery_agent import _wire_fingerprint
    scope = RECOVERY_RESOURCES.get()
    if scope is None:
        return None
    for candidate in (wire, _decision_wire(wire)):
        fingerprint = _wire_fingerprint(candidate)
        for attempt in scope["row"]["attempts"].values():
            if (attempt.get("stage") == "recovery" and attempt.get("context_epoch_id", "") == context_epoch_id
                    and attempt.get("invocation_id") and attempt.get("wire_fingerprint") == fingerprint):
                claim = scope["store"].recovery_transport_read(attempt["invocation_id"])
                if claim.get("status") in {"completed", "in_flight", "terminal"}:
                    return candidate
    return None


def recovery_closure_wire(wire, *, turns, tools, max_turns, max_tools, commit=True):
    from agent_core.workflow.recovery_agent import recovery_tool_schemas
    scope = RECOVERY_RESOURCES.get()
    if scope is None:
        return wire
    row = recovery_resource_update()
    policy = scope["policy"]
    decision = _decision_wire(wire)
    cost = recovery_request_estimate(decision, tools=recovery_tool_schemas()) + policy.max_output_tokens
    remaining = recovery_seconds_remaining()
    occupied = recovery_token_occupancy(row)
    if row.get("terminal_reason") or remaining < policy.decision_seconds or occupied + cost > policy.hard_tokens:
        raise RecoveryResourceLimit("No room for a complete decision request")
    close = (row.get("closing") or occupied >= policy.soft_tokens
        or remaining <= policy.hard_seconds - policy.soft_seconds
        or occupied + cost * 2 + policy.max_output_tokens > policy.hard_tokens
        or remaining < 2 * policy.decision_seconds or turns >= max_turns - 1 or tools >= max_tools - 1)
    if close:
        def mark(row):
            if row.get("decision_sent"):
                raise RecoveryResourceLimit("The single reserved decision turn did not resolve the investigation")
            row.update(closing=True, decision_sent=True,
                decision_reservation={"estimated_input_tokens": cost-policy.max_output_tokens,
                    "output_tokens": policy.max_output_tokens, "seconds": min(remaining, policy.decision_seconds), "turns": 1})
        if commit:
            recovery_resource_update(mark)
        return decision
    return wire


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
                "content_sha256", "observation_ref", "source_message_id", "decision_ref",
                "receipt_sha256", "archive_ref", "result_chars", "max_result_chars"}
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
            "NO_MATCH", "RECOVERY_OBSERVATION_BUDGET_EXCEEDED",
            "body_not_projected_budget_requires_targeted_read"}:
            continue
        if "content" in body and not str(body["content"] or "").strip():
            continue  # A new empty range is not new evidence.
        if not set(body) - volatile - {"status", "start_line", "end_line", "cursor", "truncated"}:
            continue  # Coverage labels without returned facts do not advance.
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


class RecoveryRuntime:
    """Admit one semantic directive and persist one route-blind receipt."""

    BUDGETED_ACTIONS = frozenset({"retry_step", "revise_plan", "run_diagnostic_probe", "organize_result", "revalidate"})

    def __init__(
        self,
        *,
        data_path: str,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
        repair_limit: int = 2,
    ) -> None:
        self.data_path = str(data_path)
        self.loader = loader
        self.saver = saver
        self.repair_limit = max(1, int(repair_limit))

    @staticmethod
    def _result(outcome: RecoveryOutcome) -> dict[str, Any]:
        return {"recovery_outcome": outcome.to_dict()}

    @staticmethod
    def _internal(
        scope: Mapping[str, Any], code: str, detail: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return RecoveryRuntime._result(RecoveryOutcome(
            schema_version=1,
            type="internal_protocol_error",
            summary="Recovery failed closed on an invalid internal protocol.",
            scope=dict(scope),
            payload={
                "error_code": str(code),
                "source_scope": dict(scope),
                "identity_conflict": deepcopy(dict(detail or {})),
            },
        ))

    @staticmethod
    def _scope(task_id: str, durable: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "step_id": str(durable.get("current_step_id") or ""),
            "state_generation": int(durable.get("state_generation") or 0),
        }

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("TYPED_RECOVERY_TASK_ID_REQUIRED")
        durable = self.loader(self.data_path, task_id)
        scope = self._scope(task_id, durable)
        observed_schema = workflow_routing_schema_version(durable, state)
        if observed_schema != WORKFLOW_ROUTING_SCHEMA_TYPED:
            return self._internal(scope, "WORKFLOW_ROUTING_SCHEMA_NOT_TYPED", {
                "observed_schema": observed_schema,
            })

        try:
            projection = project_recovery_seed(state, durable)
        except StageContractError as exc:
            return self._internal(scope, exc.code, {"detail": exc.detail})
        if projection is None:
            return self._internal(scope, "RECOVERY_CAUSAL_PACKET_MISSING")
        seed = projection.recovery_seed
        packet = seed.failure_packet
        if packet.action_ref:
            scope["action_id"] = packet.action_ref
        if packet.attempt_ref:
            scope["attempt_id"] = packet.attempt_ref

        raw_directive = state.get("recovery_directive")
        if not isinstance(raw_directive, Mapping):
            return self._internal(scope, "RECOVERY_DIRECTIVE_MISSING")
        try:
            directive = RecoveryDirective.from_mapping(raw_directive)
        except StageContractError as exc:
            return self._internal(scope, exc.code, {"detail": exc.detail})

        if directive.task_id != packet.task_id:
            return self._internal(scope, "RECOVERY_DIRECTIVE_TASK_MISMATCH", {
                "directive_task_id": directive.task_id,
                "packet_task_id": packet.task_id,
            })
        if directive.target_step_id and directive.target_step_id != packet.step_id:
            return self._internal(scope, "RECOVERY_DIRECTIVE_STEP_MISMATCH", {
                "directive_step_id": directive.target_step_id,
                "packet_step_id": packet.step_id,
            })

        inspected_refs = {
            str(item) for item in state.get("recovery_inspected_refs") or ()
            if str(item)
        }
        declared_refs = {
            packet.source_outcome_ref,
            *packet.evidence_refs,
            *packet.inspection_refs,
            *seed.evidence_refs,
            *inspected_refs,
        }
        undeclared = sorted(set(directive.evidence_refs) - declared_refs)
        if undeclared:
            return self._internal(
                scope,
                "RECOVERY_DIRECTIVE_EVIDENCE_SCOPE_INVALID",
                {"undeclared_evidence_refs": undeclared},
            )

        source = dict(projection.source or {})
        source_payload = (
            dict(source.get("payload") or {})
            if projection.source_kind.endswith("outcome")
            else dict(source)
        )
        source_scope = (
            dict(source.get("scope") or {})
            if projection.source_kind.endswith("outcome")
            else {}
        )
        source_fingerprint = projection.source_fingerprint
        failure_fingerprint = str(
            packet.failure_fingerprint or source_fingerprint
        )
        source_incident_id = str(
            source_payload.get("incident_id") or ""
        ).strip()
        if (
            packet.incident_id
            and source_incident_id
            and packet.incident_id != source_incident_id
        ):
            return self._internal(scope, "RECOVERY_INCIDENT_ID_MISMATCH", {
                "packet_incident_id": packet.incident_id,
                "source_incident_id": source_incident_id,
            })
        # New schema-1 packets make the immutable FailurePacket authoritative.
        # Legacy packets retain the prior source-payload/failure-id fallback.
        incident_id = str(
            packet.incident_id or source_incident_id or packet.failure_id
        )
        failure_packet_ref = str(
            state.get("active_failure_packet_ref")
            or (durable.get("task_state") or {}).get(
                "active_failure_packet_ref"
            )
            or ""
        )
        if packet.incident_id:
            scope["incident_id"] = packet.incident_id
            scope["failure_packet_version"] = packet.packet_version
        decision_id = directive.directive_id
        directive_wire = directive.to_dict()
        directive_digest = _full_hash(directive_wire)

        task_state = dict(durable.get("task_state") or {})
        receipts = dict(task_state.get("typed_recovery_receipts") or {})
        prior = receipts.get(decision_id)
        if isinstance(prior, Mapping):
            if (
                str(prior.get("source_fingerprint") or "") != source_fingerprint
                or str(prior.get("directive_digest") or "") != directive_digest
            ):
                return self._internal(
                    scope,
                    "RECOVERY_DECISION_IDENTITY_CONFLICT",
                    {
                        "decision_id": decision_id,
                        "stored_source_fingerprint": str(prior.get("source_fingerprint") or ""),
                        "observed_source_fingerprint": source_fingerprint,
                        "stored_directive_digest": str(prior.get("directive_digest") or ""),
                        "observed_directive_digest": directive_digest,
                    },
                )
            return self._result(
                RecoveryOutcome.from_mapping(prior.get("outcome") or {})
            )

        ledger = dict(task_state.get("typed_recovery_budget") or {})
        result_resolution_request = {}
        if directive.result_selections:
            from agent_core.workflow.result_resolution import admit_result_resolution
            try:
                result_resolution_request = admit_result_resolution(
                    seed=seed, durable=durable, directive=directive, state=state)
            except StageContractError as exc:
                return self._internal(scope, exc.code, {"detail": exc.detail,
                    "command_started": False, "directive_id": decision_id})
        consumed_before = int(ledger.get(incident_id) or 0)
        consumes_budget = directive.action in self.BUDGETED_ACTIONS
        budget_receipt = {
            "incident_id": incident_id,
            "ordinal": consumed_before + (1 if consumes_budget else 0),
            "limit": self.repair_limit,
            "remaining": max(
                0,
                self.repair_limit - consumed_before - (1 if consumes_budget else 0),
            ),
            "consumed": consumes_budget,
            "source_fingerprint": source_fingerprint,
            **({
                "failure_packet_ref": failure_packet_ref,
                "failure_packet_version": packet.packet_version,
            } if packet.incident_id else {}),
        }

        progress = dict(task_state.get("typed_recovery_progress") or {})
        prior_progress = dict(progress.get(incident_id) or {})
        semantic_progress = {
            "action": directive.action,
            "summary": directive.summary,
            "reason_code": directive.reason_code,
            "instruction": directive.instruction,
            "question": directive.question,
            "diagnostic_operation": directive.diagnostic_operation, "diagnostic_inputs": list(directive.diagnostic_inputs),
            **({"result_selections": directive.to_dict()["result_selections"]}
               if directive.result_selections else {}),
        }
        prior_evidence = {
            str(item) for item in prior_progress.get("evidence_refs") or ()
            if str(item)
        }
        same_diagnosis = (
            str(prior_progress.get("semantic_digest") or "")
            == _full_hash(semantic_progress)
        )
        has_new_evidence = bool(set(directive.evidence_refs) - prior_evidence)

        outcome_type: str
        payload: dict[str, Any]
        if consumes_budget and consumed_before >= self.repair_limit:
            outcome_type = "blocked"
            payload = {
                "reason_code": "RECOVERY_BUDGET_EXHAUSTED",
                "blocked_evidence": {
                    "decision_id": decision_id,
                    "incident_id": incident_id,
                    "failure_fingerprint": failure_fingerprint,
                },
                "source_scope": scope,
                "budget_receipt": {
                    **budget_receipt,
                    "consumed": False,
                    "ordinal": consumed_before,
                },
            }
        elif (revalidation_stop_reason(projection, durable, self.repair_limit)
              or (same_diagnosis and not has_new_evidence)):
            outcome_type = "blocked"
            payload = {
                "reason_code": "RECOVERY_NO_PROGRESS",
                "blocked_evidence": {
                    "decision_id": decision_id,
                    "incident_id": incident_id,
                    "failure_fingerprint": failure_fingerprint,
                },
                "source_scope": scope,
                "budget_receipt": {
                    **budget_receipt,
                    "consumed": False,
                    "ordinal": consumed_before,
                },
            }
        elif directive.action in {"retry_step", "run_diagnostic_probe", "organize_result"}:
            outcome_type = "candidate_repair_required"
            payload = {
                "diagnosis": directive.summary,
                "failure_owner": packet.failure_owner,
                "strategy": "semantic_directive:" + directive.action,
                "repair_instruction": directive.instruction,
                "source_scope": scope,
                "decision_id": decision_id,
                "incident_id": incident_id,
                "failure_fingerprint": failure_fingerprint,
                "budget_receipt": budget_receipt,
            }
        elif directive.action == "revise_plan":
            outcome_type = "plan_revision_required"
            payload = {
                "diagnosis": directive.summary,
                "contract_violations": [directive.instruction],
                "source_scope": scope,
                "decision_id": decision_id,
                "budget_receipt": budget_receipt,
            }
        elif directive.action == "reconsult":
            outcome_type = "reconsultation_required"
            payload = {
                "diagnosis": directive.summary,
                "missing_facts": [directive.instruction],
                "source_scope": scope,
                "decision_id": decision_id,
            }
        elif directive.action == "revalidate":
            if not directive.evidence_refs:
                return self._internal(
                    scope,
                    "REVALIDATION_REQUIRES_NEW_EVIDENCE",
                    {"directive_id": decision_id},
                )
            outcome_type = "revalidation_required"
            payload = {
                "diagnosis": directive.summary,
                "new_evidence_refs": list(directive.evidence_refs),
                "prior_validation_id": str(
                    source_scope.get("validation_id")
                    or source_payload.get("prior_validation_id")
                    or ""
                ),
                "source_scope": scope,
                "decision_id": decision_id,
            }
        elif directive.action == "ask_user":
            outcome_type = "user_input_required"
            payload = {
                "diagnosis": directive.summary,
                "question_id": "question-" + _hash({
                    "directive_id": decision_id,
                    "slots": list(directive.requested_slots),
                }),
                "question": directive.question,
                "owner": "INPUT_DATA",
                "requires_user_input": True,
                "source_scope": scope,
                "decision_id": decision_id,
            }
        elif directive.action == "blocked":
            outcome_type = "blocked"
            payload = {
                "reason_code": directive.reason_code,
                "blocked_evidence": {
                    "diagnosis": directive.summary,
                    "unresolved_evidence": list(directive.evidence_refs),
                },
                "source_scope": scope,
                "budget_receipt": {**budget_receipt, "consumed": False},
            }
        else:  # RecoveryDirective is closed, but fail closed if it ever expands.
            return self._internal(scope, "RECOVERY_ACTION_UNMAPPED", {
                "action": directive.action,
            })

        summaries = {
            "candidate_repair_required": "A bounded changed candidate is required.",
            "plan_revision_required": "The approved Plan contract requires revision.",
            "reconsultation_required": "Authoritative task facts require reconsultation.",
            "revalidation_required": "New diagnostic evidence requires deterministic revalidation.",
            "user_input_required": "Recovery requires an input-owned user fact.",
            "blocked": "Recovery reached a durable terminal blocker.",
        }
        outcome = RecoveryOutcome(
            schema_version=1,
            type=outcome_type,
            summary=summaries[outcome_type],
            scope=scope,
            payload=payload,
            evidence_refs=directive.evidence_refs,
        )

        if consumes_budget and outcome_type in {
            "candidate_repair_required", "plan_revision_required", "revalidation_required"
        }:
            ledger[incident_id] = consumed_before + 1
            task_state["typed_recovery_budget"] = ledger
        if outcome_type != "blocked":
            progress[incident_id] = {
                "action": directive.action,
                "revalidation_fingerprint": revalidation_fingerprint(projection),
                "semantic_digest": _full_hash(semantic_progress),
                "evidence_refs": sorted(prior_evidence | set(directive.evidence_refs)),
                "directive_id": decision_id,
            }
            task_state["typed_recovery_progress"] = progress

        candidate = deepcopy(durable)
        candidate_task = dict(candidate.get("task_state") or {})
        for key in ("typed_recovery_budget", "typed_recovery_progress"):
            if key in task_state:
                candidate_task[key] = deepcopy(task_state[key])
        candidate_receipts = dict(candidate_task.get("typed_recovery_receipts") or {})
        candidate_receipts[decision_id] = {
            "schema_version": 1,
            "source_fingerprint": source_fingerprint,
            "directive_digest": directive_digest,
            "directive": directive_wire,
            "reception": {**deepcopy(state.get("recovery_reception") or {}),
                "admission": {"status": "admitted", "outcome_type": outcome_type,
                    "command_started": False, "directive_id": decision_id}},
            "failure_packet_ref": failure_packet_ref,
            **({
                "incident_id": packet.incident_id,
                "failure_packet_version": packet.packet_version,
                "supersedes_packet_ref": packet.supersedes_packet_ref,
            } if packet.incident_id else {}),
            "recovery_archive_ref": str(state.get("recovery_archive_ref") or ""),
            "causal_observation_receipts": deepcopy(state.get("recovery_causal_observation_receipts") or []),
            "business_resume_state": deepcopy(state.get("recovery_business_resume_state") or {}),
            "outcome_digest": _full_hash(outcome.to_dict()),
            "outcome": outcome.to_dict(),
            **({"result_resolution_request": result_resolution_request}
               if result_resolution_request else {}),
        }
        if outcome_type == "plan_revision_required":
            # Point to the existing admitted body; this is not a second Plan
            # or a new semantic directive. Planner reads it until scope moves.
            candidate_task["planner_revision_request"] = {
                "decision_id": decision_id,
                **{key: str(durable.get(key) or "") for key in
                   ("task_id", "plan_id", "plan_version", "current_step_id")},
            }
        if outcome_type == "candidate_repair_required":
            diagnostic_request = {}
            if directive.action == "run_diagnostic_probe":
                from agent_core.workflow.diagnostic_admission import admit_diagnostic_request
                try:
                    diagnostic_request = admit_diagnostic_request(seed=seed, durable=durable, directive=directive, data_path=self.data_path)
                except StageContractError as exc:
                    if exc.code in {"ARTIFACT_PERMISSION_DENIED", "ARTIFACT_NOT_FOUND", "DIAGNOSTIC_OPERATION_UNSUPPORTED", "DIAGNOSTIC_INPUT_BUDGET_EXCEEDED"}:
                        return self._result(RecoveryOutcome(schema_version=1, type="blocked", scope=scope,
                            summary="The requested diagnostic could not be admitted: " + exc.detail,
                            payload={"reason_code": exc.code, "source_scope": scope,
                                "blocked_evidence": {"decision_id": decision_id, "incident_id": incident_id,
                                    "failure_fingerprint": failure_fingerprint},
                                "failure_stage": "recovery.diagnostic_admission",
                                "detail": exc.detail, "operation": directive.diagnostic_operation,
                                "requested_refs": list(directive.diagnostic_inputs), "command_started": False,
                                "budget_receipt": {**budget_receipt, "consumed": False}}))
                    raise
            from pathlib import Path
            from agent_core.recovery import build_scoped_recovery_execution_envelope
            envelope = build_scoped_recovery_execution_envelope(
                decision_id=decision_id, source_scope=scope,
                strategy="RUN_DIAGNOSTIC_PROBE" if directive.action == "run_diagnostic_probe" else "PATCH_SCRIPT",
                recovery_context_id=seed.seed_id, step_id=packet.step_id,
                task_root=Path(self.data_path) / "task_outputs" / task_id,
                objective=directive.instruction,
                allowed_input_roots=(
                    [item["observed_path"] for item in result_resolution_request["selections"]]
                    if directive.action == "organize_result" else
                    [item["path"] for item in diagnostic_request.get("inputs", [])]),
            )
            if diagnostic_request:
                from dataclasses import replace
                from agent_core.recovery import RecoveryExecutionEnvelope
                envelope = replace(envelope, scratch_capability=replace(envelope.scratch_capability, **diagnostic_request["limits"]), immutable_hash="")
                envelope = RecoveryExecutionEnvelope.from_mapping(envelope.to_dict())
                candidate_receipts[decision_id]["diagnostic_request"] = diagnostic_request
            candidate_task["recovery_state"] = bind_recovery_generation_authority(
                candidate, candidate_task.get("recovery_state") or {},
                decision_id=decision_id, envelope=envelope.to_dict(), source_attempt_id=packet.attempt_ref,
            )
            candidate_receipts[decision_id].update(
                execution_envelope=envelope.to_dict(), execution_envelope_digest=envelope.immutable_hash,
                generation_authority=deepcopy(candidate_task["recovery_state"]["active_recovery"]),
                business_resume_state=deepcopy(state.get("recovery_business_resume_state") or {}),
                causal_observation_receipts=deepcopy(state.get("recovery_causal_observation_receipts") or []),
            )
        candidate_task["typed_recovery_receipts"] = candidate_receipts
        packet_ref = str(
            state.get("active_failure_packet_ref")
            or candidate_task.get("active_failure_packet_ref")
            or ""
        )
        if packet_ref:
            consumed_packets = dict(
                candidate_task.get("consumed_failure_packet_refs") or {}
            )
            consumed_packets[packet_ref] = {
                "decision_id": decision_id,
                "source_fingerprint": source_fingerprint,
                "directive_digest": directive_digest,
                **({
                    "incident_id": packet.incident_id,
                    "failure_packet_version": packet.packet_version,
                    "supersedes_packet_ref": packet.supersedes_packet_ref,
                } if packet.incident_id else {}),
            }
            candidate_task["consumed_failure_packet_refs"] = consumed_packets
            try:
                append_causal_failure_lifecycle_transition(
                    candidate_task,
                    packet_ref=packet_ref,
                    packet=packet,
                    transition="resolved",
                    reason=f"recovery_outcome_committed:{outcome_type}",
                    related_ref=decision_id,
                )
            except StageContractError as exc:
                return self._internal(scope, exc.code, {"detail": exc.detail})
            if str(candidate_task.get("active_failure_packet_ref") or "") == packet_ref:
                candidate_task["active_failure_packet_ref"] = ""
        candidate["task_state"] = candidate_task

        try:
            self.saver(self.data_path, task_id, candidate)
        except StateCASConflict:
            refreshed = self.loader(self.data_path, task_id)
            refreshed_receipt = dict(
                (refreshed.get("task_state") or {}).get("typed_recovery_receipts")
                or {}
            ).get(decision_id)
            if isinstance(refreshed_receipt, Mapping):
                if (
                    str(refreshed_receipt.get("source_fingerprint") or "")
                    == source_fingerprint
                    and str(refreshed_receipt.get("directive_digest") or "")
                    == directive_digest
                    and has_causal_failure_lifecycle_transition(
                        refreshed.get("task_state") or {},
                        packet_ref=failure_packet_ref,
                        transition="resolved",
                    )
                ):
                    return self._result(RecoveryOutcome.from_mapping(
                        refreshed_receipt.get("outcome") or {}
                    ))
            return self._internal(scope, "RECOVERY_RECEIPT_CAS_CONFLICT", {
                "expected_state_generation": int(durable.get("state_generation") or 0),
                "current_state_generation": int(refreshed.get("state_generation") or 0),
            })
        return self._result(outcome)


# Import compatibility for existing schema-2 wiring while callers migrate to
# the more accurate name.  This is not the schema-1 Recovery implementation.
TypedRecovery = RecoveryRuntime


def recovery_harness_error(exc: Exception, state: Mapping[str, Any]) -> RecoveryOutcome:
    return RecoveryOutcome(
        schema_version=1,
        type="harness_error",
        summary="Typed Recovery boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "TYPED_RECOVERY_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "detail": str(exc)[:800],
        },
    )


__all__ = ["RecoveryRuntime", "TypedRecovery", "recovery_harness_error"]

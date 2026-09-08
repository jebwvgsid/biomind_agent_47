"""Planner-only output policy and provider-compatible, credential-free projection.

Profiles are reviewed declarations, not live endpoint observations. A resolved
policy never claims an HTTP body or server limit has been observed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

INITIAL_OUTPUT_TOKEN_LIMIT = 16_384
LENGTH_RECOVERY_OUTPUT_TOKEN_LIMIT = 32_768
MAX_LENGTH_CORRECTIVE_CALLS_PER_EPOCH = 1
OUTPUT_POLICY_VERSION = "planner-output-policy-v1"
MODEL_PARAMETER_HASH_VERSION = "planner-model-parameters-v2"
TOKEN_FIELDS = ("max_tokens", "max_completion_tokens")
CAPABILITY_VERSION = "deepseek-v4-chat-completions-20260908"
CAPABILITY_SOURCE = "https://api-docs.deepseek.com/api/create-chat-completion/"
CEILING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
# The docs say 384K. A conservative decimal ceiling avoids assuming K=1024.
OFFICIAL_DOCUMENTED_OUTPUT_CEILING = 384_000
OFFICIAL_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"})


class PlannerOutputPolicyError(ValueError):
    def __init__(self, code: str, *, field_path: str = "$.planner_output_policy", actual: Any = None, expected: Any = None) -> None:
        self.code, self.field_path = code, field_path
        self.actual, self.expected = actual, expected
        super().__init__(code)

    def to_diagnostic(self) -> dict[str, Any]:
        return {"code": self.code, "field_path": self.field_path, "actual": deepcopy(self.actual), "expected": deepcopy(self.expected), "message": self.code, "owner": "planner_output_policy"}


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _positive(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_LIMIT_INVALID", field_path=field, actual={"type": type(value).__name__}, expected="positive integer")
    return value


def _endpoint_identity(endpoint: str) -> str:
    parsed = urlsplit(str(endpoint or ""))
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "unrecognized_endpoint"
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return "unrecognized_endpoint"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "unrecognized_endpoint"
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}{parsed.path.rstrip('/')}"


def _official_profile(model: str, endpoint: str) -> dict[str, Any]:
    if model not in OFFICIAL_MODELS or _endpoint_identity(endpoint) not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        return {}
    return {"source": CAPABILITY_SOURCE, "version": CAPABILITY_VERSION, "output_parameter": "max_tokens", "output_ceiling": OFFICIAL_DOCUMENTED_OUTPUT_CEILING, "output_ceiling_label": "384K", "output_ceiling_source": CEILING_SOURCE, "context_limit": 1_000_000, "evidence_level": "documented_official_profile_not_deployment_verified"}


def _layers(llm: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """Only inspect positive parameter fields, never model_dump/client secrets."""
    result: list[tuple[str, Mapping[str, Any]]] = []
    bound = getattr(llm, "bound", None)
    if bound is not None and bound is not llm:
        result.extend(_layers(bound))
    else:
        result.append(("client", {k: getattr(llm, k, None) for k in (*TOKEN_FIELDS, "extra_body")}))
        values = getattr(llm, "model_kwargs", None)
        if isinstance(values, Mapping):
            result.append(("client.model_kwargs", {k: values[k] for k in (*TOKEN_FIELDS, "extra_body") if k in values}))
    values = getattr(llm, "kwargs", None)
    if isinstance(values, Mapping):
        result.append(("binding", {k: values[k] for k in (*TOKEN_FIELDS, "extra_body") if k in values}))
    return result


def planner_client_output_settings(llm: Any) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for label, layer in _layers(llm):
        for location, values in ((label, layer), (label + ".extra_body", layer.get("extra_body") or {})):
            if not isinstance(values, Mapping):
                continue
            for key in TOKEN_FIELDS:
                if values.get(key) is not None:
                    entries.append({"source": location, "key": key, "value": _positive(values[key], location + "." + key)})
    if len({item["value"] for item in entries}) > 1:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_PARAMETER_CONFLICT", actual=entries, expected="one unambiguous configured output limit")
    return {"entries": entries, "limit": entries[0]["value"] if entries else None}


def resolve_planner_output_policy(*, model: str, endpoint: str, provider: str = "", settings: Mapping[str, Any] | None = None, task_output_token_limit: int | None = None, recovery: bool = False, legacy_epoch: bool = False, existing_output_settings: Mapping[str, Any] | None = None, application_context_limit: int = 200_000, existing_input_reserve: int = 8192) -> dict[str, Any]:
    config = dict(settings or {})
    allowed = {"initial_output_token_limit", "length_recovery_output_token_limit", "max_length_corrective_calls_per_epoch", "provider_capability", "source", "version", "task_output_token_limit"}
    if set(config) - allowed:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_POLICY_FIELD_UNSUPPORTED", actual=sorted(set(config) - allowed), expected=sorted(allowed))
    initial = _positive(config.get("initial_output_token_limit", INITIAL_OUTPUT_TOKEN_LIMIT), "$.planner_output_policy.initial_output_token_limit")
    corrective = _positive(config.get("length_recovery_output_token_limit", LENGTH_RECOVERY_OUTPUT_TOKEN_LIMIT), "$.planner_output_policy.length_recovery_output_token_limit")
    count = config.get("max_length_corrective_calls_per_epoch", MAX_LENGTH_CORRECTIVE_CALLS_PER_EPOCH)
    if type(count) is not int or count not in (0, 1):
        raise PlannerOutputPolicyError("PLANNER_LENGTH_CORRECTION_LIMIT_INVALID", actual=count, expected="0 or 1 within existing Planner budgets")
    if corrective < initial:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_POLICY_ORDER_INVALID", actual={"initial": initial, "recovery": corrective}, expected="recovery >= initial")
    app_limit = _positive(application_context_limit, "$.application_context_limit")
    if type(existing_input_reserve) is not int or existing_input_reserve < 0:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_LIMIT_INVALID", field_path="$.existing_input_reserve", expected="nonnegative integer")
    reserve = existing_input_reserve
    profile = _official_profile(str(model), endpoint) if provider in {"openai", "deepseek"} else {}
    declared = config.get("provider_capability")
    if declared is not None:
        if not isinstance(declared, Mapping):
            raise PlannerOutputPolicyError("PLANNER_PROVIDER_CAPABILITY_INVALID", expected="scoped capability declaration")
        required = {"model", "endpoint", "source", "version", "output_parameter", "output_ceiling"}
        if not required.issubset(declared) or any(not str(declared.get(key) or "").strip() for key in ("source", "version")):
            raise PlannerOutputPolicyError("PLANNER_PROVIDER_CAPABILITY_INVALID", expected=sorted(required))
        if str(declared["model"]) != str(model) or _endpoint_identity(str(declared["endpoint"])) != _endpoint_identity(endpoint) or _endpoint_identity(endpoint) == "unrecognized_endpoint":
            raise PlannerOutputPolicyError("PLANNER_PROVIDER_CAPABILITY_SCOPE_MISMATCH", expected="exact endpoint and model match")
        if declared["output_parameter"] not in TOKEN_FIELDS:
            raise PlannerOutputPolicyError("PLANNER_OUTPUT_PARAMETER_UNSUPPORTED", actual=declared["output_parameter"], expected=list(TOKEN_FIELDS))
        custom = {key: deepcopy(declared[key]) for key in ("source", "version", "output_parameter")}
        custom["output_ceiling"] = _positive(declared["output_ceiling"], "$.provider_capability.output_ceiling")
        custom["context_limit"] = _positive(declared["context_limit"], "$.provider_capability.context_limit") if declared.get("context_limit") is not None else None
        custom["evidence_level"] = "explicit_scoped_deployment_declaration_not_service_verified"
        if profile and custom["output_parameter"] != profile["output_parameter"]:
            raise PlannerOutputPolicyError("PLANNER_OUTPUT_PARAMETER_CONFLICT", actual=custom["output_parameter"], expected=profile["output_parameter"])
        if profile:
            custom["output_ceiling"] = min(custom["output_ceiling"], profile["output_ceiling"])
            custom["context_limit"] = min(custom.get("context_limit") or profile["context_limit"], profile["context_limit"])
        profile = custom
    existing = dict(existing_output_settings or {})
    existing_limit = existing.get("limit")
    if existing_limit is not None:
        existing_limit = _positive(existing_limit, "$.existing_output_limit")
    restrictions = []
    for label, value in (("existing_client_limit", existing_limit), ("node_task_limit", config.get("task_output_token_limit")), ("task_output_token_limit", task_output_token_limit)):
        if value is not None:
            restrictions.append({"source": label, "value": _positive(value, "$." + label)})
    cap = min((row["value"] for row in restrictions), default=None)
    source = str(config.get("source") or ("planner_node_policy_override" if config else "planner_engineering_defaults"))
    active = bool(profile) and not legacy_epoch
    if active:
        ceiling = profile["output_ceiling"]
        configured = corrective if recovery else initial
        if configured > ceiling:
            raise PlannerOutputPolicyError("PLANNER_OUTPUT_CAPABILITY_LIMIT_EXCEEDED", actual=configured, expected={"maximum": ceiling, "source": profile["source"]})
        selected = min(configured, cap) if cap is not None else configured
        resolved_initial = min(initial, cap) if cap is not None else initial
        resolved_corrective = min(corrective, cap) if cap is not None else corrective
        reason = "TASK_OR_EXISTING_LIMIT_APPLIED" if selected != configured else "EXPLICIT_OUTPUT_POLICY_RESOLVED"
    else:
        task_caps = [row["value"] for row in restrictions if row["source"] != "existing_client_limit"]
        if task_caps and (existing_limit is None or existing_limit > min(task_caps)):
            # There is no declared compatible parameter with which to enforce
            # the stricter task cap. Preserve UNKNOWN, but never silently send
            # a request that may exceed an explicit task restriction.
            raise PlannerOutputPolicyError(
                "PLANNER_OUTPUT_TASK_LIMIT_UNENFORCEABLE",
                actual={"existing_output_limit": existing_limit, "task_output_limit": min(task_caps), "provider_capability_known": bool(profile), "legacy_epoch": bool(legacy_epoch)},
                expected="a scoped, declared provider parameter policy enforcing the task limit",
            )
        selected = existing_limit
        resolved_initial = resolved_corrective = selected
        reason = "LEGACY_EPOCH_POLICY_PRESERVED" if legacy_epoch else "PROVIDER_CAPABILITY_UNKNOWN"
        source = "existing_client_configuration" if selected is not None else "unknown_existing_provider_default"
    reserve = max(reserve, selected or 0)
    parameter = profile.get("output_parameter") if active else None
    result = {"schema_version": 1, "output_policy_source": source, "output_policy_version": str(config.get("version") or OUTPUT_POLICY_VERSION), "provider_capability_source": profile.get("source") or "UNKNOWN", "provider_capability_version": profile.get("version") or "UNKNOWN", "provider_capability_evidence_level": profile.get("evidence_level") or "UNKNOWN", "provider_documented_output_ceiling": profile.get("output_ceiling"), "provider_documented_output_ceiling_label": profile.get("output_ceiling_label"), "provider_documented_context_limit": profile.get("context_limit"), "requested_output_limit": selected, "wire_output_parameter_key": parameter, "wire_output_parameter_value": selected if active else None, "wire_observation_status": "resolved_not_wire_observed", "server_effective_output_limit": None, "application_context_limit": app_limit, "input_reserve_used": reserve, "initial_output_token_limit": resolved_initial, "length_recovery_output_token_limit": resolved_corrective, "max_length_corrective_calls_per_epoch": count, "can_increase": bool(active and resolved_corrective > resolved_initial), "apply_output_binding": active, "policy_reason": reason, "recovery": bool(recovery), "legacy_epoch": bool(legacy_epoch), "restrictions": restrictions, "model_parameter_hash_version": MODEL_PARAMETER_HASH_VERSION}
    # The same effective decision is stable across invocation identities.
    result["output_policy_hash"] = _digest({key: result[key] for key in (
        "schema_version", "output_policy_version", "provider_capability_version",
        "requested_output_limit", "wire_output_parameter_key", "wire_output_parameter_value",
        "application_context_limit", "input_reserve_used", "initial_output_token_limit",
        "length_recovery_output_token_limit", "max_length_corrective_calls_per_epoch",
        "apply_output_binding", "legacy_epoch",
    )})
    return result


def planner_output_parameter_projection(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(policy.get(key)) for key in (
        "output_policy_hash", "output_policy_version", "wire_output_parameter_key",
        "wire_output_parameter_value", "input_reserve_used",
    )}


def _clean_token_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in values.items() if key not in TOKEN_FIELDS}
    if isinstance(result.get("extra_body"), Mapping):
        result["extra_body"] = {key: value for key, value in result["extra_body"].items() if key not in TOKEN_FIELDS}
    return result


def bind_planner_output_policy(llm: Any, policy: Mapping[str, Any]) -> Any:
    """Clone only parameter layers; shared clients and other nodes are untouched."""
    if not policy.get("apply_output_binding"):
        return llm
    planner_client_output_settings(llm)  # Reject ambiguous existing authorities first.
    parameter = policy.get("wire_output_parameter_key")
    value = _positive(policy.get("wire_output_parameter_value"), "$.wire_output_parameter_value")
    if parameter not in TOKEN_FIELDS:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_PARAMETER_UNSUPPORTED", actual=parameter, expected=list(TOKEN_FIELDS))
    extra: dict[str, Any] = {}
    for _, layer in _layers(llm):
        if isinstance(layer.get("extra_body"), Mapping):
            extra.update({key: deepcopy(item) for key, item in layer["extra_body"].items() if key not in TOKEN_FIELDS})

    def clean(current: Any) -> Any:
        bound = getattr(current, "bound", None)
        if bound is not None and bound is not current:
            if not hasattr(current, "model_copy"):
                raise PlannerOutputPolicyError("PLANNER_OUTPUT_BINDING_UNSUPPORTED", expected="copyable bound model")
            return current.model_copy(update={"bound": clean(bound), "kwargs": _clean_token_fields(dict(getattr(current, "kwargs", {}) or {}))})
        if hasattr(current, "model_copy"):
            return current.model_copy(update={"max_tokens": None, "model_kwargs": _clean_token_fields(dict(getattr(current, "model_kwargs", {}) or {})), "extra_body": extra})
        if planner_client_output_settings(current).get("limit") is not None:
            raise PlannerOutputPolicyError("PLANNER_OUTPUT_BINDING_UNSUPPORTED", expected="copyable configured model")
        return current

    normalized = clean(llm)
    if not hasattr(normalized, "bind"):
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_BINDING_UNSUPPORTED", expected="provider bind interface")
    # SDK extra_body merges after ChatOpenAI's max_tokens -> max_completion_tokens
    # rewrite. All other token aliases were removed from cloned layers above.
    return normalized.bind(extra_body={**extra, parameter: value})


def planner_context_budget_policy(base: Any, policy: Mapping[str, Any]) -> Any:
    updates = {"output_reserve_tokens": max(int(base.output_reserve_tokens), int(policy.get("input_reserve_used") or 0)), "application_context_limit_tokens": min(int(base.application_context_limit_tokens), int(policy["application_context_limit"]))}
    declared = policy.get("provider_documented_context_limit")
    existing = base.known_provider_context_limit_tokens
    if declared is not None:
        updates["known_provider_context_limit_tokens"] = min(int(declared), int(existing)) if existing is not None else int(declared)
    try:
        return replace(base, **updates)
    except ValueError as exc:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_CONTEXT_CAPACITY_INSUFFICIENT", actual={"input_reserve_used": updates["output_reserve_tokens"], "application_context_limit": updates["application_context_limit_tokens"]}, expected="positive safe input capacity after output reserve") from exc


def planner_input_admission(messages: Sequence[Any], policy: Mapping[str, Any], *, base_policy: Any, estimator: Any = None) -> dict[str, Any]:
    from agent_core.workflow.context_compaction import measure_provider_input
    from agent_core.workflow.scoped_context import provider_visible_payload
    budget = measure_provider_input({"system": provider_visible_payload(messages[0]) if messages else {}, "frozen_inputs": provider_visible_payload(messages[1]) if len(messages) > 1 else {}, "local_messages": [provider_visible_payload(message) for message in messages[2:]], "provider_wrappers": {"message_count": len(messages), "message_protocol": "langchain-provider-visible-v1"}}, policy=planner_context_budget_policy(base_policy, policy), estimator=estimator)
    if budget.hard_block:
        raise PlannerOutputPolicyError("PLANNER_OUTPUT_CONTEXT_CAPACITY_INSUFFICIENT", actual=budget.to_dict(), expected="input estimate <= safe_input_tokens with this output reserve")
    return budget.to_dict()

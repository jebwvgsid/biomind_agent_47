"""Context budgets, source records and deterministic validation/activation facts.

Pi owns live context compaction and model calls.  The former asynchronous
compactor, hierarchical follow-up/retry loop and archive/activate orchestrator
have been retired.  Pure token/source calculations and explicit epoch CAS
validation retain their existing formats for stored domain evidence.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import sys
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace, fields
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from prompts.context_compaction import (
    build_context_compaction_messages,
    build_context_segment_map_messages,
    build_context_summary_merge_messages,
)


CONTEXT_COMPACTION_SCHEMA_VERSION = 1
CONTEXT_BUDGET_POLICY_VERSION = "schema2-context-budget-v1"
DEFAULT_APPLICATION_CONTEXT_LIMIT_TOKENS = 312_144
DEFAULT_INPUT_SOFT_LIMIT_TOKENS = 256_000
DEFAULT_COMPACTION_TRIGGER_RATIO = 1.0
DEFAULT_COMPACTION_TARGET_RATIO = 0.65
DEFAULT_COMPACTION_MAX_ATTEMPTS = 2
DEFAULT_HIERARCHICAL_MAX_LEVELS = 16
DEFAULT_HIERARCHICAL_MAX_MODEL_CALLS = 256
_HIERARCHICAL_RETRY_RESERVE_FEEDBACK = {
    "previous_attempt": 1,
    "error_code": "HIERARCHICAL_VALIDATION_ERROR",
    "detail": "x" * 256,
}

STANDARD_CONTEXT_COMPONENTS = (
    "system",
    "tool_schema",
    "frozen_inputs",
    "inherited_materials",
    "local_messages",
    "pending_results",
    "provider_wrappers",
)

_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_FORBIDDEN_MUTATION_KEYS = frozenset({
    "route",
    "next_step",
    "proposed_route",
    "state",
    "state_update",
    "task_state",
    "plan_state",
    "durable_state",
    "working_update",
    "parent_update",
    "pending_action",
    "required_next_action",
})


class ContextCompactionError(ValueError):
    """Fail-closed context compaction contract error."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        feedback: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code or "CONTEXT_COMPACTION_ERROR")
        self.detail = str(detail or "")
        self.feedback = deepcopy(dict(feedback or {}))
        super().__init__(
            self.code if not self.detail else f"{self.code}: {self.detail}"
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"error_code": self.code}
        if self.detail:
            value["detail"] = self.detail
        if self.feedback:
            value["capacity_feedback"] = deepcopy(self.feedback)
        return value


class ContextCompactionCapacityError(ContextCompactionError):
    """A model-selected exact set still exceeds safe provider capacity."""


class ContextCompactionAttemptsExhausted(ContextCompactionError):
    """The fixed source epoch did not yield a valid plan in bounded attempts."""

    def __init__(self, attempts: Sequence["CompactionAttemptRecord"]) -> None:
        self.attempts = tuple(attempts)
        super().__init__(
            "CONTEXT_COMPACTION_ATTEMPTS_EXHAUSTED",
            f"attempts={len(self.attempts)}",
            feedback={
                "attempts": [attempt.to_dict() for attempt in self.attempts],
            },
        )


class ContextEpochCASConflict(ContextCompactionError):
    """The source epoch changed before the prepared epoch could be activated."""


def _freeze_json(value: Any, *, field_name: str = "value") -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContextCompactionError(
                    "CONTEXT_JSON_KEY_INVALID", f"{field_name}: {key!r}"
                )
            frozen[key] = _freeze_json(item, field_name=f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, field_name=f"{field_name}[]") for item in value
        )
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ContextCompactionError(
        "CONTEXT_JSON_VALUE_INVALID",
        f"{field_name}: {type(value).__name__}",
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _thaw_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContextCompactionError(
            "CONTEXT_JSON_VALUE_INVALID", f"{type(exc).__name__}: {exc}"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _normalized_digest(value: str) -> str:
    result = str(value or "").strip().lower()
    if result.startswith("sha256:"):
        result = result[7:]
    return result


def _require_text(value: Any, field_name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContextCompactionError(
            "CONTEXT_FIELD_REQUIRED", field_name
        )
    return result


def _require_hash(value: Any, field_name: str) -> str:
    result = _require_text(value, field_name)
    if _SHA256_RE.fullmatch(result) is None:
        raise ContextCompactionError(
            "CONTEXT_HASH_INVALID", f"{field_name}={result!r}"
        )
    return result


def _require_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ContextCompactionError(
            "CONTEXT_INTEGER_INVALID", f"{field_name}=bool"
        )
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContextCompactionError(
            "CONTEXT_INTEGER_INVALID", field_name
        ) from exc
    if result < 0:
        raise ContextCompactionError(
            "CONTEXT_INTEGER_INVALID", f"{field_name}={result}"
        )
    return result


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContextCompactionError("CONTEXT_BOOLEAN_INVALID", field_name)
    return value


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(
        value, Sequence
    ):
        raise ContextCompactionError(
            "CONTEXT_SEQUENCE_INVALID", field_name
        )
    return tuple(value)


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: set[str] | frozenset[str], field_name: str
) -> None:
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise ContextCompactionError(
            "CONTEXT_SCHEMA_UNKNOWN_FIELD",
            f"{field_name}: {', '.join(unknown)}",
        )


def _reject_mutation_fields(value: Any, *, path: str = "plan") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_MUTATION_KEYS:
                raise ContextCompactionError(
                    "COMPACTION_STATE_MUTATION_FIELD_FORBIDDEN",
                    f"{path}.{key}",
                )
            _reject_mutation_fields(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_mutation_fields(item, path=f"{path}[{index}]")


def _normalized_scope(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        raw: Mapping[str, Any] = {"kind": value}
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise ContextCompactionError("SOURCE_UNIT_SCOPE_INVALID")
    frozen = _freeze_json(raw, field_name="scope")
    if not frozen:
        raise ContextCompactionError("SOURCE_UNIT_SCOPE_INVALID", "empty")
    _reject_mutation_fields(frozen, path="scope")
    return frozen


def _normalized_coverage(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextCompactionError("SOURCE_UNIT_COVERAGE_INVALID")
    coverage = dict(value)
    kind = str(coverage.get("kind") or "").strip().lower()
    if kind not in {"full", "range"}:
        raise ContextCompactionError(
            "SOURCE_UNIT_COVERAGE_INVALID", f"kind={kind or '<missing>'}"
        )
    coverage["kind"] = kind
    if kind == "range":
        valid = False
        range_shapes = (
            ("start_offset", "end_offset", 0, False),
            ("start_token", "end_token", 0, False),
            ("start_line", "end_line", 1, True),
            ("start", "end", 0, False),
        )
        for start_key, end_key, minimum, inclusive in range_shapes:
            if start_key not in coverage and end_key not in coverage:
                continue
            if start_key not in coverage or end_key not in coverage:
                raise ContextCompactionError(
                    "SOURCE_UNIT_COVERAGE_INVALID",
                    f"range requires {start_key} and {end_key}",
                )
            start = _require_nonnegative_int(coverage[start_key], start_key)
            end = _require_nonnegative_int(coverage[end_key], end_key)
            if start < minimum or end < start + (0 if inclusive else 1):
                raise ContextCompactionError(
                    "SOURCE_UNIT_COVERAGE_INVALID",
                    f"{start_key}={start}; {end_key}={end}",
                )
            coverage[start_key] = start
            coverage[end_key] = end
            valid = True
            break
        if not valid:
            raise ContextCompactionError(
                "SOURCE_UNIT_COVERAGE_INVALID", "range bounds missing"
            )
        if "source_length" in coverage:
            source_length = _require_nonnegative_int(
                coverage["source_length"], "source_length"
            )
            coverage["source_length"] = source_length
            end_value = next(
                int(coverage[key])
                for key in ("end_offset", "end_token", "end", "end_line")
                if key in coverage
            )
            if end_value > source_length:
                raise ContextCompactionError(
                    "SOURCE_UNIT_COVERAGE_INVALID", "range exceeds source_length"
                )
        if "source_content_hash" in coverage:
            coverage["source_content_hash"] = _require_hash(
                coverage["source_content_hash"], "coverage.source_content_hash"
            )
    return _freeze_json(coverage, field_name="coverage")


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    estimator_name: str
    estimator_version: str
    exact_or_estimated: str
    uncertainty_tokens: int = 0
    overhead_min_tokens: int = 0
    overhead_max_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tokens", _require_nonnegative_int(self.tokens, "tokens")
        )
        object.__setattr__(
            self,
            "uncertainty_tokens",
            _require_nonnegative_int(
                self.uncertainty_tokens, "uncertainty_tokens"
            ),
        )
        object.__setattr__(
            self,
            "overhead_min_tokens",
            _require_nonnegative_int(
                self.overhead_min_tokens, "overhead_min_tokens"
            ),
        )
        object.__setattr__(
            self,
            "overhead_max_tokens",
            _require_nonnegative_int(
                self.overhead_max_tokens, "overhead_max_tokens"
            ),
        )
        if self.overhead_max_tokens < self.overhead_min_tokens:
            raise ContextCompactionError("TOKEN_ESTIMATOR_OVERHEAD_INVALID")
        if str(self.exact_or_estimated) not in {"exact", "estimated"}:
            raise ContextCompactionError("TOKEN_ESTIMATOR_QUALITY_INVALID")
        _require_text(self.estimator_name, "estimator_name")
        _require_text(self.estimator_version, "estimator_version")


class TokenEstimator(Protocol):
    """Provider/model/version-bound estimator protocol."""

    name: str
    version: str
    exact_or_estimated: str

    def estimate(self, text: str) -> TokenEstimate:
        ...


class ReferenceTokenEstimator:
    """Offline cl100k reference with an explicit mixed-script fallback.

    The result remains ``estimated`` even when cl100k is available because the
    active remote provider may use a different tokenizer.  Integration can
    inject a provider/model/version-matched ``TokenEstimator`` when available.
    """

    exact_or_estimated = "estimated"

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoder: Any = None
        self._encoding_name = str(encoding_name or "cl100k_base")
        try:
            import tiktoken  # type: ignore

            self._encoder = tiktoken.get_encoding(self._encoding_name)
            try:
                package_version = importlib.metadata.version("tiktoken")
            except importlib.metadata.PackageNotFoundError:
                package_version = "unknown"
            self.name = f"{self._encoding_name}_reference"
            self.version = f"tiktoken-{package_version}"
        except Exception:
            self.name = "mixed_script_reference_fallback"
            self.version = "ascii3-cjk1.25-other2-v1"

    def estimate(self, text: str) -> TokenEstimate:
        payload = str(text or "")
        if not payload:
            tokens = 0
            uncertainty = 0
        elif self._encoder is not None:
            tokens = len(self._encoder.encode(payload, disallowed_special=()))
            uncertainty = max(64, math.ceil(tokens * 0.10))
        else:
            ascii_chars = 0
            cjk_chars = 0
            other_chars = 0
            for char in payload:
                codepoint = ord(char)
                if codepoint < 128:
                    ascii_chars += 1
                elif (
                    0x3400 <= codepoint <= 0x9FFF
                    or 0xF900 <= codepoint <= 0xFAFF
                    or 0x3040 <= codepoint <= 0x30FF
                    or 0xAC00 <= codepoint <= 0xD7AF
                ):
                    cjk_chars += 1
                else:
                    other_chars += 1
            tokens = max(
                1,
                math.ceil(ascii_chars / 3)
                + math.ceil(cjk_chars * 1.25)
                + math.ceil(other_chars / 2),
            )
            uncertainty = max(64, math.ceil(tokens * 0.25))
        return TokenEstimate(
            tokens=tokens,
            estimator_name=self.name,
            estimator_version=self.version,
            exact_or_estimated=self.exact_or_estimated,
            uncertainty_tokens=uncertainty,
            overhead_min_tokens=0,
            overhead_max_tokens=uncertainty,
        )


@dataclass(frozen=True)
class ContextBudgetPolicy:
    schema_version: int = CONTEXT_COMPACTION_SCHEMA_VERSION
    policy_version: str = CONTEXT_BUDGET_POLICY_VERSION
    application_context_limit_tokens: int = (
        DEFAULT_APPLICATION_CONTEXT_LIMIT_TOKENS
    )
    input_soft_limit_tokens: int = field(default_factory=lambda: int(
        os.getenv("BIOMIND_INPUT_SOFT_LIMIT_TOKENS", str(DEFAULT_INPUT_SOFT_LIMIT_TOKENS))))
    context_capability_source: str = "application_budget_only_provider_not_verified"
    known_provider_context_limit_tokens: int | None = None
    known_provider_input_limit_tokens: int | None = None
    input_capability_source: str = "not_verified"
    output_reserve_tokens: int = 50_000
    # Explicit Recovery policy overrides inherited local client defaults;
    # provider capability is a separate fact, not inferred from max_tokens.
    requested_output_limit_tokens: int | None = None
    known_provider_output_limit_tokens: int | None = None
    output_capability_source: str = ""
    estimation_margin_tokens: int = 4_096
    safety_headroom_tokens: int = 2_048
    trigger_ratio: float = DEFAULT_COMPACTION_TRIGGER_RATIO
    target_ratio: float = DEFAULT_COMPACTION_TARGET_RATIO
    compressor_safe_input_tokens: int | None = None
    compressor_output_reserve_tokens: int | None = None
    compressor_prompt_overhead_tokens: int = 0

    def __post_init__(self) -> None:
        schema_version = _require_nonnegative_int(
            self.schema_version, "schema_version"
        )
        if schema_version != CONTEXT_COMPACTION_SCHEMA_VERSION:
            raise ContextCompactionError("CONTEXT_BUDGET_SCHEMA_UNSUPPORTED")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(
            self, "policy_version", _require_text(self.policy_version, "policy_version")
        )
        application = _require_nonnegative_int(
            self.application_context_limit_tokens,
            "application_context_limit_tokens",
        )
        if application <= 0:
            raise ContextCompactionError("CONTEXT_LIMIT_INVALID")
        object.__setattr__(
            self, "application_context_limit_tokens", application
        )
        provider = self.known_provider_context_limit_tokens
        if provider is not None:
            provider = _require_nonnegative_int(
                provider, "known_provider_context_limit_tokens"
            )
            if provider <= 0:
                raise ContextCompactionError("CONTEXT_LIMIT_INVALID")
            object.__setattr__(
                self, "known_provider_context_limit_tokens", provider
            )
        for field_name in (
            "input_soft_limit_tokens",
            "output_reserve_tokens",
            "estimation_margin_tokens",
            "safety_headroom_tokens",
            "compressor_prompt_overhead_tokens",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_nonnegative_int(getattr(self, field_name), field_name),
            )
        for field_name in ("requested_output_limit_tokens", "known_provider_output_limit_tokens",
                           "known_provider_input_limit_tokens", "compressor_output_reserve_tokens"):
            value = getattr(self, field_name)
            if value is not None:
                value = _require_nonnegative_int(value, field_name)
                if value <= 0:
                    raise ContextCompactionError("CONTEXT_OUTPUT_LIMIT_INVALID", field_name)
                object.__setattr__(self, field_name, value)
        if self.compressor_safe_input_tokens is not None:
            compressor_safe = _require_nonnegative_int(
                self.compressor_safe_input_tokens,
                "compressor_safe_input_tokens",
            )
            if compressor_safe <= 0:
                raise ContextCompactionError("COMPRESSOR_LIMIT_INVALID")
            object.__setattr__(
                self, "compressor_safe_input_tokens", compressor_safe
            )
        try:
            target_ratio = float(self.target_ratio)
            trigger_ratio = float(self.trigger_ratio)
        except (TypeError, ValueError) as exc:
            raise ContextCompactionError("CONTEXT_BUDGET_RATIO_INVALID") from exc
        if not 0 < target_ratio <= trigger_ratio <= 1:
            raise ContextCompactionError("CONTEXT_BUDGET_RATIO_INVALID")
        object.__setattr__(self, "target_ratio", target_ratio)
        object.__setattr__(self, "trigger_ratio", trigger_ratio)
        if self.safe_input_tokens <= 0 or self.trigger_tokens <= 0:
            raise ContextCompactionError(
                "CONTEXT_BUDGET_NO_SAFE_INPUT_CAPACITY"
            )

    @property
    def effective_context_limit_tokens(self) -> int:
        provider = self.known_provider_context_limit_tokens
        return min(
            int(self.application_context_limit_tokens),
            int(provider) if provider is not None else int(
                self.application_context_limit_tokens
            ),
        )

    @property
    def safe_input_tokens(self) -> int:
        # A total window includes generated output. An independent input
        # limit does not: subtract output once, only from the total window.
        input_capacity = self.effective_context_limit_tokens - int(self.output_reserve_tokens)
        if self.known_provider_input_limit_tokens is not None:
            input_capacity = min(input_capacity, self.known_provider_input_limit_tokens)
        return max(0, input_capacity - int(self.estimation_margin_tokens))

    @property
    def trigger_tokens(self) -> int:
        candidates = [
            math.floor(
                float(self.trigger_ratio)
                * min(self.input_soft_limit_tokens, self.effective_context_limit_tokens)
            ),
            self.safe_input_tokens - int(self.safety_headroom_tokens),
        ]
        # The compactor has its own bounded/segmented request path. Its
        # smaller window is not a second soft limit on a business node.
        return max(0, min(candidates))

    @property
    def target_tokens(self) -> int:
        return min(
            self.safe_input_tokens,
            math.floor(
                float(self.target_ratio)
                * min(self.input_soft_limit_tokens, self.safe_input_tokens)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "application_context_limit_tokens": (
                self.application_context_limit_tokens
            ),
            "known_provider_context_limit_tokens": (
                self.known_provider_context_limit_tokens
            ),
            "known_provider_input_limit_tokens": self.known_provider_input_limit_tokens,
            "input_capability_source": self.input_capability_source,
            "input_soft_limit_tokens": self.input_soft_limit_tokens,
            "context_capability_source": self.context_capability_source,
            "effective_input_limit_tokens": self.trigger_tokens,
            "compression_constraint": ("input_soft_target" if self.trigger_tokens >= self.input_soft_limit_tokens
                else "effective_window_or_explicit_earlier_trigger"),
            "effective_context_limit_tokens": (
                self.effective_context_limit_tokens
            ),
            "output_reserve_tokens": self.output_reserve_tokens,
            "requested_output_limit_tokens": self.requested_output_limit_tokens,
            "known_provider_output_limit_tokens": self.known_provider_output_limit_tokens,
            "output_capability_source": self.output_capability_source,
            "estimation_margin_tokens": self.estimation_margin_tokens,
            "safety_headroom_tokens": self.safety_headroom_tokens,
            "safe_input_tokens": self.safe_input_tokens,
            "trigger_ratio": self.trigger_ratio,
            "trigger_tokens": self.trigger_tokens,
            "target_ratio": self.target_ratio,
            "target_tokens": self.target_tokens,
            "compressor_safe_input_tokens": self.compressor_safe_input_tokens,
            "compressor_output_reserve_tokens": self.compressor_output_reserve_tokens,
            "compressor_prompt_overhead_tokens": (
                self.compressor_prompt_overhead_tokens
            ),
        }


def configured_context_policy(*, output_reserve_tokens=None):
    """One configuration source, without importing credentials/startup effects.

    The application limit is a TOTAL budget, not a provider capability claim.
    Unknown remote capability remains unknown; explicit adapter limits win.
    """
    loaded = sys.modules.get("config.config")
    def setting(name, env, default):
        return int(getattr(loaded, name, os.getenv(env, default)))
    return ContextBudgetPolicy(
        application_context_limit_tokens=setting("SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS",
            "BIOMIND_SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS", DEFAULT_APPLICATION_CONTEXT_LIMIT_TOKENS),
        input_soft_limit_tokens=setting("SCHEMA2_INPUT_SOFT_LIMIT_TOKENS",
            "BIOMIND_INPUT_SOFT_LIMIT_TOKENS", DEFAULT_INPUT_SOFT_LIMIT_TOKENS),
        output_reserve_tokens=int(output_reserve_tokens if output_reserve_tokens is not None else
            setting("SCHEMA2_CONTEXT_OUTPUT_RESERVE_TOKENS", "BIOMIND_CONTEXT_OUTPUT_RESERVE_TOKENS", 50_000)),
        estimation_margin_tokens=setting("SCHEMA2_CONTEXT_ESTIMATION_MARGIN_TOKENS",
            "BIOMIND_SCHEMA2_CONTEXT_ESTIMATION_MARGIN_TOKENS", 4096),
        safety_headroom_tokens=setting("SCHEMA2_CONTEXT_SAFETY_HEADROOM_TOKENS",
            "BIOMIND_SCHEMA2_CONTEXT_SAFETY_HEADROOM_TOKENS", 2048))


def model_context_policy(policy, model, *, resolve_output=True):
    """Resolve capability facts only from the actual adapter, never its name."""
    from agent_core.output_policy_facts import output_policy_projection
    updates = {}
    if resolve_output:
        output = output_policy_projection(model).get("client_resolved_output_limit")
        if type(output) is int and output > 0:
            updates["output_reserve_tokens"] = output
    limits, input_limits, output_limits = [], [], []
    sources, input_sources, output_sources = [], [], []
    seen, current = set(), model
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        profile = getattr(current, "profile", None)
        for prefix, facts in (("adapter.", current), ("adapter.profile.", profile)):
            for keys, values, origins in (
                (("context_window", "max_context_tokens"), limits, sources),
                (("max_input_tokens",), input_limits, input_sources),
                # max_tokens is a client option, not a provider capability.
                (("max_output_tokens",), output_limits, output_sources),
            ):
                for key in keys:
                    value = facts.get(key) if isinstance(facts, Mapping) else getattr(facts, key, None)
                    if type(value) is int and value > 0:
                        values.append(value)
                        origins.append(prefix + key)
        current = getattr(current, "bound", None)
    for values, origins, field_name, source_name in (
        (limits, sources, "known_provider_context_limit_tokens", "context_capability_source"),
        (input_limits, input_sources, "known_provider_input_limit_tokens", "input_capability_source"),
        (output_limits, output_sources, "known_provider_output_limit_tokens", "output_capability_source"),
    ):
        if values:
            if getattr(policy, field_name) is not None:
                values.append(getattr(policy, field_name))
            updates[field_name] = min(values)
            updates[source_name] = ",".join(origins)
    if updates.get("known_provider_output_limit_tokens"):
        updates["output_reserve_tokens"] = min(
            updates.get("output_reserve_tokens", policy.output_reserve_tokens),
            updates["known_provider_output_limit_tokens"])
    return replace(policy, **updates)


def model_tool_schema(model):
    current, seen = model, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current)); kwargs = getattr(current, "kwargs", None)
        if isinstance(kwargs, Mapping) and "tools" in kwargs:
            return deepcopy(list(kwargs["tools"] or ()))
        current = getattr(current, "bound", None)
    return []


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    policy: ContextBudgetPolicy
    estimated_input_tokens: int
    component_token_counts: Mapping[str, int]
    estimator_name: str
    estimator_version: str
    exact_or_estimated: str
    uncertainty_tokens: int
    overhead_min_tokens: int
    overhead_max_tokens: int
    cached_input_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "estimated_input_tokens",
            _require_nonnegative_int(
                self.estimated_input_tokens, "estimated_input_tokens"
            ),
        )
        counts = {
            str(key): _require_nonnegative_int(value, f"component.{key}")
            for key, value in self.component_token_counts.items()
        }
        if sum(counts.values()) != self.estimated_input_tokens:
            raise ContextCompactionError(
                "CONTEXT_COMPONENT_ACCOUNTING_MISMATCH"
            )
        object.__setattr__(self, "component_token_counts", MappingProxyType(counts))
        for field_name in (
            "uncertainty_tokens", "overhead_min_tokens", "overhead_max_tokens"
        ):
            object.__setattr__(
                self,
                field_name,
                _require_nonnegative_int(getattr(self, field_name), field_name),
            )
        if self.overhead_max_tokens < self.overhead_min_tokens:
            raise ContextCompactionError("TOKEN_ESTIMATOR_OVERHEAD_INVALID")
        if str(self.exact_or_estimated) not in {"exact", "estimated"}:
            raise ContextCompactionError("TOKEN_ESTIMATOR_QUALITY_INVALID")
        _require_text(self.estimator_name, "estimator_name")
        _require_text(self.estimator_version, "estimator_version")
        if self.cached_input_tokens is not None:
            object.__setattr__(
                self,
                "cached_input_tokens",
                _require_nonnegative_int(
                    self.cached_input_tokens, "cached_input_tokens"
                ),
            )

    @property
    def should_compact(self) -> bool:
        return self.estimated_input_tokens >= self.policy.trigger_tokens

    @property
    def hard_block(self) -> bool:
        return self.estimated_input_tokens > self.policy.safe_input_tokens

    @property
    def remaining_safe_tokens(self) -> int:
        return self.policy.safe_input_tokens - self.estimated_input_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
            **self.policy.to_dict(),
            "estimated_input_tokens": self.estimated_input_tokens,
            "component_token_counts": dict(self.component_token_counts),
            "cached_input_tokens": self.cached_input_tokens,
            "cached_tokens_count_toward_input": True,
            "estimator_name": self.estimator_name,
            "estimator_version": self.estimator_version,
            "exact_or_estimated": self.exact_or_estimated,
            "uncertainty_tokens": self.uncertainty_tokens,
            "overhead_min_tokens": self.overhead_min_tokens,
            "overhead_max_tokens": self.overhead_max_tokens,
            "should_compact": self.should_compact,
            "hard_block": self.hard_block,
            "remaining_safe_tokens": self.remaining_safe_tokens,
        }


def _component_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return _canonical_json(value)


def measure_provider_input(
    component_payloads: Mapping[str, Any],
    *,
    policy: ContextBudgetPolicy | None = None,
    estimator: TokenEstimator | None = None,
    cached_input_tokens: int | None = None,
) -> ContextBudgetSnapshot:
    """Account one actual provider input without subtracting cached tokens."""

    if not isinstance(component_payloads, Mapping):
        raise ContextCompactionError("CONTEXT_COMPONENTS_INVALID")
    active_policy = policy or ContextBudgetPolicy()
    active_estimator = estimator or ReferenceTokenEstimator()
    names = list(STANDARD_CONTEXT_COMPONENTS)
    names.extend(
        sorted(str(key) for key in component_payloads if str(key) not in names)
    )
    counts: dict[str, int] = {}
    estimates: list[TokenEstimate] = []
    for name in names:
        estimate = active_estimator.estimate(
            _component_text(component_payloads.get(name))
        )
        if not isinstance(estimate, TokenEstimate):
            raise ContextCompactionError(
                "TOKEN_ESTIMATOR_RESULT_INVALID", type(estimate).__name__
            )
        counts[name] = estimate.tokens
        estimates.append(estimate)
    labels = {
        (
            item.estimator_name,
            item.estimator_version,
            item.exact_or_estimated,
        )
        for item in estimates
    }
    if len(labels) != 1:
        raise ContextCompactionError("TOKEN_ESTIMATOR_LABEL_DRIFT")
    label = next(iter(labels))
    return ContextBudgetSnapshot(
        policy=active_policy,
        estimated_input_tokens=sum(counts.values()),
        component_token_counts=counts,
        estimator_name=label[0],
        estimator_version=label[1],
        exact_or_estimated=label[2],
        uncertainty_tokens=sum(item.uncertainty_tokens for item in estimates),
        overhead_min_tokens=sum(item.overhead_min_tokens for item in estimates),
        overhead_max_tokens=sum(item.overhead_max_tokens for item in estimates),
        cached_input_tokens=cached_input_tokens,
    )


@dataclass(frozen=True)
class SourceUnit:
    unit_id: str
    kind: str
    source_ref: str
    body_ref: str
    content_hash: str
    coverage: Mapping[str, Any]
    scope: Mapping[str, Any]
    token_count: int
    body_length_chars: int
    selected: bool = False
    skill_id: str = ""
    protocol_group_id: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit_id", _require_text(self.unit_id, "unit_id"))
        object.__setattr__(self, "kind", _require_text(self.kind, "kind"))
        object.__setattr__(
            self, "source_ref", _require_text(self.source_ref, "source_ref")
        )
        object.__setattr__(
            self,
            "body_ref",
            _require_text(self.body_ref or self.source_ref, "body_ref"),
        )
        object.__setattr__(
            self,
            "content_hash",
            _require_hash(self.content_hash, "content_hash"),
        )
        object.__setattr__(self, "coverage", _normalized_coverage(self.coverage))
        object.__setattr__(self, "scope", _normalized_scope(self.scope))
        object.__setattr__(
            self,
            "token_count",
            _require_nonnegative_int(self.token_count, "token_count"),
        )
        object.__setattr__(
            self,
            "body_length_chars",
            _require_nonnegative_int(
                self.body_length_chars, "body_length_chars"
            ),
        )
        object.__setattr__(
            self, "selected", _require_bool(self.selected, "selected")
        )
        object.__setattr__(self, "skill_id", str(self.skill_id or "").strip())
        object.__setattr__(
            self,
            "protocol_group_id",
            str(self.protocol_group_id or "").strip(),
        )
        object.__setattr__(
            self,
            "provenance",
            _freeze_json(self.provenance or {}, field_name="provenance"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceUnit":
        if not isinstance(value, Mapping):
            raise ContextCompactionError("SOURCE_UNIT_INVALID")
        allowed = {
            "unit_id", "kind", "source_ref", "body_ref", "content_hash",
            "coverage", "scope", "token_count", "body_length_chars",
            "selected", "skill_id", "protocol_group_id", "provenance",
        }
        _reject_unknown_keys(value, allowed, "source_unit")
        return cls(
            unit_id=value.get("unit_id", ""),
            kind=value.get("kind", ""),
            source_ref=value.get("source_ref", ""),
            body_ref=value.get("body_ref") or value.get("source_ref", ""),
            content_hash=value.get("content_hash", ""),
            coverage=value.get("coverage") or {},
            scope=value.get("scope") or {},
            token_count=value.get("token_count", 0),
            body_length_chars=value.get("body_length_chars", 0),
            selected=value.get("selected", False),
            skill_id=value.get("skill_id", ""),
            protocol_group_id=value.get("protocol_group_id", ""),
            provenance=value.get("provenance") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "source_ref": self.source_ref,
            "body_ref": self.body_ref,
            "content_hash": self.content_hash,
            "coverage": _thaw_json(self.coverage),
            "scope": _thaw_json(self.scope),
            "token_count": self.token_count,
            "body_length_chars": self.body_length_chars,
            "selected": self.selected,
            "skill_id": self.skill_id,
            "protocol_group_id": self.protocol_group_id,
            "provenance": _thaw_json(self.provenance),
        }


@dataclass(frozen=True)
class SourceUnitManifest:
    source_epoch_id: str
    history_version: int
    selected_revision: int
    units: tuple[SourceUnit, ...]
    protocol_groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    schema_version: int = CONTEXT_COMPACTION_SCHEMA_VERSION
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        if int(self.schema_version) != CONTEXT_COMPACTION_SCHEMA_VERSION:
            raise ContextCompactionError("SOURCE_MANIFEST_SCHEMA_UNSUPPORTED")
        object.__setattr__(
            self,
            "source_epoch_id",
            _require_text(self.source_epoch_id, "source_epoch_id"),
        )
        object.__setattr__(
            self,
            "history_version",
            _require_nonnegative_int(self.history_version, "history_version"),
        )
        object.__setattr__(
            self,
            "selected_revision",
            _require_nonnegative_int(
                self.selected_revision, "selected_revision"
            ),
        )
        units = tuple(
            item if isinstance(item, SourceUnit) else SourceUnit.from_mapping(item)
            for item in self.units
        )
        unit_ids = [item.unit_id for item in units]
        if len(set(unit_ids)) != len(unit_ids):
            raise ContextCompactionError("SOURCE_UNIT_ID_DUPLICATE")
        object.__setattr__(self, "units", units)
        derived: dict[str, list[str]] = {}
        for unit in units:
            scoped_epoch = str(unit.scope.get("epoch_id") or "")
            if scoped_epoch and scoped_epoch != self.source_epoch_id:
                raise ContextCompactionError(
                    "SOURCE_UNIT_SCOPE_MISMATCH",
                    f"unit={unit.unit_id}; epoch={scoped_epoch}",
                )
            scoped_revision = unit.scope.get("selected_revision")
            if scoped_revision is not None and _require_nonnegative_int(
                scoped_revision, "scope.selected_revision"
            ) != self.selected_revision:
                raise ContextCompactionError(
                    "SOURCE_UNIT_SCOPE_MISMATCH",
                    f"unit={unit.unit_id}; selected_revision={scoped_revision}",
                )
            scoped_history = unit.scope.get("history_version")
            if scoped_history is not None and _require_nonnegative_int(
                scoped_history, "scope.history_version"
            ) != self.history_version:
                raise ContextCompactionError(
                    "SOURCE_UNIT_SCOPE_MISMATCH",
                    f"unit={unit.unit_id}; history_version={scoped_history}",
                )
            if unit.protocol_group_id:
                derived.setdefault(unit.protocol_group_id, []).append(unit.unit_id)
        supplied = {
            str(group_id): tuple(str(item) for item in members)
            for group_id, members in dict(self.protocol_groups or {}).items()
        }
        if supplied:
            if supplied != {
                key: tuple(value) for key, value in derived.items()
            }:
                raise ContextCompactionError("SOURCE_PROTOCOL_GROUP_MISMATCH")
        groups = {
            key: tuple(value) for key, value in derived.items()
        }
        object.__setattr__(self, "protocol_groups", MappingProxyType(groups))
        computed = _digest(self._identity_payload())
        if self.manifest_hash and (
            _normalized_digest(_require_hash(
                self.manifest_hash, "manifest_hash"
            ))
            != computed
        ):
            raise ContextCompactionError("SOURCE_MANIFEST_HASH_MISMATCH")
        object.__setattr__(self, "manifest_hash", computed)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_epoch_id": self.source_epoch_id,
            "history_version": self.history_version,
            "selected_revision": self.selected_revision,
            "units": [item.to_dict() for item in self.units],
            "protocol_groups": {
                key: list(value) for key, value in self.protocol_groups.items()
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_payload(), "manifest_hash": self.manifest_hash}

    def by_id(self) -> dict[str, SourceUnit]:
        return {item.unit_id: item for item in self.units}


def build_source_unit_manifest(
    *,
    source_epoch_id: str,
    history_version: int,
    selected_revision: int,
    units: Sequence[SourceUnit | Mapping[str, Any]],
) -> SourceUnitManifest:
    """Freeze a complete, body-free map of context source units."""

    return SourceUnitManifest(
        source_epoch_id=source_epoch_id,
        history_version=history_version,
        selected_revision=selected_revision,
        units=tuple(
            item if isinstance(item, SourceUnit) else SourceUnit.from_mapping(item)
            for item in units
        ),
    )


BodyResolver = Mapping[str, str] | Callable[[SourceUnit], str]


def resolve_source_body(unit: SourceUnit, resolver: BodyResolver) -> str:
    """Resolve and hash-check one immutable source body."""

    if callable(resolver):
        body = resolver(unit)
    elif isinstance(resolver, Mapping):
        found = False
        body: Any = None
        for key in (unit.body_ref, unit.unit_id, unit.source_ref):
            if key in resolver:
                body = resolver[key]
                found = True
                break
        if not found:
            raise ContextCompactionError(
                "SOURCE_BODY_UNAVAILABLE", unit.body_ref
            )
    else:
        raise ContextCompactionError("SOURCE_BODY_RESOLVER_INVALID")
    if not isinstance(body, str):
        raise ContextCompactionError(
            "SOURCE_BODY_INVALID", f"unit={unit.unit_id}"
        )
    if _text_digest(body) != _normalized_digest(unit.content_hash):
        raise ContextCompactionError(
            "SOURCE_BODY_HASH_MISMATCH", f"unit={unit.unit_id}"
        )
    if len(body) != unit.body_length_chars:
        raise ContextCompactionError(
            "SOURCE_BODY_LENGTH_MISMATCH", f"unit={unit.unit_id}"
        )
    return body


@dataclass(frozen=True)
class LosslessSegmentResult:
    source_unit_id: str
    source_content_hash: str
    segments: tuple[SourceUnit, ...]
    bodies: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "bodies", MappingProxyType(dict(self.bodies)))

    def reconstruct(self) -> str:
        return "".join(self.bodies[item.body_ref] for item in self.segments)


def segment_source_unit_losslessly(
    unit: SourceUnit,
    body: str,
    *,
    max_segment_tokens: int,
    estimator: TokenEstimator | None = None,
) -> LosslessSegmentResult:
    """Split one oversized body by reversible code-point ranges.

    Boundaries are chosen only from capacity, never from headings, keywords,
    filenames, or perceived importance.  Concatenating segment bodies is
    byte-for-byte equivalent to the original Python text value.
    """

    if resolve_source_body(unit, {unit.body_ref: body}) != body:
        raise AssertionError("unreachable")
    limit = _require_nonnegative_int(max_segment_tokens, "max_segment_tokens")
    if limit <= 0:
        raise ContextCompactionError("SOURCE_SEGMENT_LIMIT_INVALID")
    active_estimator = estimator or ReferenceTokenEstimator()
    if active_estimator.estimate(body).tokens <= limit:
        return LosslessSegmentResult(
            source_unit_id=unit.unit_id,
            source_content_hash=unit.content_hash,
            segments=(unit,),
            bodies={unit.body_ref: body},
        )

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(body):
        low = start + 1
        high = len(body)
        best = start
        while low <= high:
            midpoint = (low + high) // 2
            tokens = active_estimator.estimate(body[start:midpoint]).tokens
            if tokens <= limit:
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best == start:
            raise ContextCompactionError(
                "SOURCE_SEGMENT_UNIT_TOO_LARGE",
                f"offset={start}; limit={limit}",
            )
        ranges.append((start, best))
        start = best

    segments: list[SourceUnit] = []
    bodies: dict[str, str] = {}
    for ordinal, (start, end) in enumerate(ranges, start=1):
        segment_body = body[start:end]
        segment_ref = (
            f"{unit.body_ref}#codepoint={start}:{end}"
        )
        segment_id = (
            f"{unit.unit_id}:range:{ordinal:06d}:{start}:{end}"
        )
        segment = SourceUnit(
            unit_id=segment_id,
            kind=unit.kind,
            source_ref=unit.source_ref,
            body_ref=segment_ref,
            content_hash=_text_digest(segment_body),
            coverage={
                "kind": "range",
                "range_unit": "unicode_codepoint",
                "start_offset": start,
                "end_offset": end,
                "source_length": len(body),
                "source_content_hash": unit.content_hash,
            },
            scope=unit.scope,
            token_count=active_estimator.estimate(segment_body).tokens,
            body_length_chars=len(segment_body),
            selected=unit.selected,
            skill_id=unit.skill_id,
            protocol_group_id=unit.protocol_group_id,
            provenance={
                **_thaw_json(unit.provenance),
                "parent_unit_id": unit.unit_id,
                "segmentation_policy": "capacity_only_codepoint_range_v1",
            },
        )
        segments.append(segment)
        bodies[segment_ref] = segment_body
    result = LosslessSegmentResult(
        source_unit_id=unit.unit_id,
        source_content_hash=unit.content_hash,
        segments=tuple(segments),
        bodies=bodies,
    )
    if result.reconstruct() != body:
        raise ContextCompactionError("SOURCE_SEGMENT_COVERAGE_INCOMPLETE")
    expected_start = 0
    for segment in result.segments:
        coverage = segment.coverage
        if int(coverage["start_offset"]) != expected_start:
            raise ContextCompactionError("SOURCE_SEGMENT_COVERAGE_INCOMPLETE")
        expected_start = int(coverage["end_offset"])
    if expected_start != len(body):
        raise ContextCompactionError("SOURCE_SEGMENT_COVERAGE_INCOMPLETE")
    return result


@dataclass(frozen=True)
class CompactionRequest:
    compaction_request_id: str
    source_manifest: SourceUnitManifest
    budget_policy: ContextBudgetPolicy = field(default_factory=ContextBudgetPolicy)
    fixed_components: Mapping[str, Any] = field(default_factory=dict)
    cached_input_tokens: int | None = None
    prior_summary: str = ""
    prior_retrieval_state: tuple[Mapping[str, Any], ...] = ()
    source_archive_ref: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "compaction_request_id",
            _require_text(
                self.compaction_request_id, "compaction_request_id"
            ),
        )
        if not isinstance(self.source_manifest, SourceUnitManifest):
            raise ContextCompactionError("SOURCE_MANIFEST_INVALID")
        if not isinstance(self.budget_policy, ContextBudgetPolicy):
            raise ContextCompactionError("CONTEXT_BUDGET_POLICY_INVALID")
        object.__setattr__(
            self,
            "fixed_components",
            _freeze_json(
                self.fixed_components or {}, field_name="fixed_components"
            ),
        )
        if self.cached_input_tokens is not None:
            object.__setattr__(
                self,
                "cached_input_tokens",
                _require_nonnegative_int(
                    self.cached_input_tokens, "cached_input_tokens"
                ),
            )
        object.__setattr__(self, "prior_summary", str(self.prior_summary or ""))
        object.__setattr__(
            self,
            "prior_retrieval_state",
            tuple(
                _freeze_json(item, field_name="prior_retrieval_state")
                for item in self.prior_retrieval_state
            ),
        )
        object.__setattr__(
            self, "source_archive_ref", str(self.source_archive_ref or "").strip()
        )

    @property
    def source_epoch_id(self) -> str:
        return self.source_manifest.source_epoch_id

    @property
    def source_manifest_hash(self) -> str:
        return self.source_manifest.manifest_hash

    @property
    def history_version(self) -> int:
        return self.source_manifest.history_version

    @property
    def selected_revision(self) -> int:
        return self.source_manifest.selected_revision

    def to_prompt_payload(self, resolver: BodyResolver) -> dict[str, Any]:
        units = []
        for unit in self.source_manifest.units:
            units.append({**unit.to_dict(), "body": resolve_source_body(unit, resolver)})
        return {
            "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
            "compaction_request_id": self.compaction_request_id,
            "source_epoch_id": self.source_epoch_id,
            "source_manifest_hash": self.source_manifest_hash,
            "history_version": self.history_version,
            "selected_revision": self.selected_revision,
            "budget": self.budget_policy.to_dict(),
            # Fixed provider components are part of the exact request the
            # compactor is trying to make fit.  They are read-only context,
            # not selectable source units: the model may account for them
            # while choosing retain/summary dispositions, but cannot remove
            # or rewrite them.
            "fixed_components": _thaw_json(self.fixed_components),
            "source_manifest": self.source_manifest.to_dict(),
            "source_units": units,
            "prior_summary": self.prior_summary,
            "prior_retrieval_state": [
                _thaw_json(item) for item in self.prior_retrieval_state
            ],
        }


def new_compaction_request(
    manifest: SourceUnitManifest,
    *,
    budget_policy: ContextBudgetPolicy | None = None,
    fixed_components: Mapping[str, Any] | None = None,
    cached_input_tokens: int | None = None,
    prior_summary: str = "",
    prior_retrieval_state: Sequence[Mapping[str, Any]] = (),
) -> CompactionRequest:
    policy = budget_policy or ContextBudgetPolicy()
    request_id = "cmp-" + _digest({
        "source_manifest_hash": manifest.manifest_hash,
        "policy": policy.to_dict(),
    })[:24]
    return CompactionRequest(
        compaction_request_id=request_id,
        source_manifest=manifest,
        budget_policy=policy,
        fixed_components=fixed_components or {},
        cached_input_tokens=cached_input_tokens,
        prior_summary=prior_summary,
        prior_retrieval_state=tuple(prior_retrieval_state),
    )


@dataclass(frozen=True)
class MaterialRetrievalState:
    source_unit_id: str
    skill_id: str
    source_ref: str
    content_hash: str
    coverage: Mapping[str, Any]
    residency: str
    reason: str
    summary: str
    missing_details: str
    retrieval_ref: str
    scope: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_unit_id",
            _require_text(self.source_unit_id, "source_unit_id"),
        )
        object.__setattr__(self, "skill_id", str(self.skill_id or "").strip())
        object.__setattr__(
            self, "source_ref", _require_text(self.source_ref, "source_ref")
        )
        object.__setattr__(
            self,
            "content_hash",
            _require_hash(self.content_hash, "content_hash"),
        )
        object.__setattr__(self, "coverage", _normalized_coverage(self.coverage))
        residency = str(self.residency or "").strip().lower()
        if residency not in {"summarized", "externalized"}:
            raise ContextCompactionError("MATERIAL_RESIDENCY_INVALID", residency)
        object.__setattr__(self, "residency", residency)
        for field_name in (
            "reason", "summary", "missing_details", "retrieval_ref"
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.scope is not None:
            object.__setattr__(self, "scope", _normalized_scope(self.scope))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MaterialRetrievalState":
        allowed = {item.name for item in fields(cls)}
        _reject_unknown_keys(value, allowed, "material_retrieval_state")
        return cls(
            source_unit_id=value.get("source_unit_id", ""),
            skill_id=value.get("skill_id", ""),
            source_ref=value.get("source_ref", ""),
            content_hash=value.get("content_hash", ""),
            coverage=value.get("coverage") or {},
            residency=value.get("residency", ""),
            reason=value.get("reason", ""),
            summary=value.get("summary", ""),
            missing_details=value.get("missing_details", ""),
            retrieval_ref=value.get("retrieval_ref", ""),
            scope=value.get("scope"),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "source_unit_id": self.source_unit_id,
            "skill_id": self.skill_id,
            "source_ref": self.source_ref,
            "content_hash": self.content_hash,
            "coverage": _thaw_json(self.coverage),
            "residency": self.residency,
            "reason": self.reason,
            "summary": self.summary,
            "missing_details": self.missing_details,
            "retrieval_ref": self.retrieval_ref,
        }
        if self.scope is not None:
            value["scope"] = _thaw_json(self.scope)
        return value


@dataclass(frozen=True)
class CompactionPlan:
    compaction_request_id: str
    source_epoch_id: str
    source_manifest_hash: str
    mode: str
    retain_exact_unit_ids: tuple[str, ...]
    summary_source_unit_ids: tuple[str, ...]
    summary: str
    material_retrieval_state: tuple[MaterialRetrievalState, ...]
    unresolved_items: tuple[Any, ...]
    selection_rationale: str
    schema_version: int = CONTEXT_COMPACTION_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CompactionPlan":
        if not isinstance(value, Mapping):
            raise ContextCompactionError("COMPACTION_PLAN_INVALID")
        _reject_mutation_fields(value)
        allowed = {item.name for item in fields(cls)}
        _reject_unknown_keys(value, allowed, "compaction_plan")
        schema_version = _require_nonnegative_int(
            value.get("schema_version"), "schema_version"
        )
        if schema_version != CONTEXT_COMPACTION_SCHEMA_VERSION:
            raise ContextCompactionError("COMPACTION_PLAN_SCHEMA_UNSUPPORTED")
        mode = str(value.get("mode") or "").strip().lower()
        if mode not in {"normal", "capacity_pressure"}:
            raise ContextCompactionError("COMPACTION_MODE_INVALID", mode)
        retain = tuple(
            _require_text(item, "retain_exact_unit_ids[]")
            for item in _sequence(
                value.get("retain_exact_unit_ids"),
                "retain_exact_unit_ids",
            )
        )
        summarized = tuple(
            _require_text(item, "summary_source_unit_ids[]")
            for item in _sequence(
                value.get("summary_source_unit_ids"),
                "summary_source_unit_ids",
            )
        )
        if len(set(retain)) != len(retain) or len(set(summarized)) != len(summarized):
            raise ContextCompactionError("COMPACTION_UNIT_ID_DUPLICATE")
        retrieval = tuple(
            MaterialRetrievalState.from_mapping(item)
            for item in _sequence(
                value.get("material_retrieval_state"),
                "material_retrieval_state",
            )
            if isinstance(item, Mapping)
        )
        raw_retrieval = _sequence(
            value.get("material_retrieval_state"),
            "material_retrieval_state",
        )
        if len(retrieval) != len(raw_retrieval):
            raise ContextCompactionError("MATERIAL_RECOVERY_ENTRY_INVALID")
        unresolved = tuple(
            _freeze_json(item, field_name="unresolved_items")
            for item in _sequence(value.get("unresolved_items"), "unresolved_items")
        )
        return cls(
            schema_version=schema_version,
            compaction_request_id=_require_text(
                value.get("compaction_request_id"), "compaction_request_id"
            ),
            source_epoch_id=_require_text(
                value.get("source_epoch_id"), "source_epoch_id"
            ),
            source_manifest_hash=_require_hash(
                value.get("source_manifest_hash"), "source_manifest_hash"
            ),
            mode=mode,
            retain_exact_unit_ids=retain,
            summary_source_unit_ids=summarized,
            summary=str(value.get("summary") or ""),
            material_retrieval_state=retrieval,
            unresolved_items=unresolved,
            selection_rationale=str(value.get("selection_rationale") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "compaction_request_id": self.compaction_request_id,
            "source_epoch_id": self.source_epoch_id,
            "source_manifest_hash": self.source_manifest_hash,
            "mode": self.mode,
            "retain_exact_unit_ids": list(self.retain_exact_unit_ids),
            "summary_source_unit_ids": list(self.summary_source_unit_ids),
            "summary": self.summary,
            "material_retrieval_state": [
                item.to_dict() for item in self.material_retrieval_state
            ],
            "unresolved_items": [
                _thaw_json(item) for item in self.unresolved_items
            ],
            "selection_rationale": self.selection_rationale,
        }


def compaction_response_schema() -> dict[str, Any]:
    """Prompt and strict parsers share the dataclass field owners."""
    material = {item.name: {"type": "object" if item.name in {"coverage", "scope"} else "string"}
                for item in fields(MaterialRetrievalState)}
    material["residency"]["enum"] = ["summarized", "externalized"]
    props = {item.name: {"type": "string"} for item in fields(CompactionPlan)}
    props.update(schema_version={"const": 1}, mode={"enum": ["normal", "capacity_pressure"]},
        retain_exact_unit_ids={"type": "array", "items": {"type": "string"}},
        summary_source_unit_ids={"type": "array", "items": {"type": "string"}},
        unresolved_items={"type": "array"}, material_retrieval_state={"type": "array", "items": {
            "type": "object", "additionalProperties": False, "properties": material,
            "required": [key for key in material if key not in {"scope", "skill_id"}]}})
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


def parse_compaction_plan(value: Any) -> CompactionPlan:
    """Parse strict JSON or a provider response carrying strict JSON."""

    raw = getattr(value, "content", value)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContextCompactionError(
                "COMPACTION_PLAN_JSON_INVALID",
                f"line={exc.lineno}; column={exc.colno}",
            ) from exc
    if not isinstance(raw, Mapping):
        raise ContextCompactionError(
            "COMPACTION_PLAN_INVALID", type(raw).__name__
        )
    return CompactionPlan.from_mapping(raw)


def validate_compaction_plan(
    plan: CompactionPlan | Mapping[str, Any],
    request: CompactionRequest,
) -> CompactionPlan:
    """Mechanically validate identity, disposition, protocol, and recovery."""

    typed = (
        CompactionPlan.from_mapping(plan.to_dict())
        if isinstance(plan, CompactionPlan)
        else CompactionPlan.from_mapping(plan)
    )
    manifest = request.source_manifest
    if typed.compaction_request_id != request.compaction_request_id:
        raise ContextCompactionError("COMPACTION_REQUEST_ID_MISMATCH")
    if typed.source_epoch_id != manifest.source_epoch_id:
        raise ContextCompactionError("COMPACTION_SOURCE_EPOCH_MISMATCH")
    if _normalized_digest(typed.source_manifest_hash) != manifest.manifest_hash:
        raise ContextCompactionError("COMPACTION_SOURCE_MANIFEST_MISMATCH")

    units = manifest.by_id()
    retain = set(typed.retain_exact_unit_ids)
    summarized = set(typed.summary_source_unit_ids)
    unknown = sorted((retain | summarized) - set(units))
    if unknown:
        raise ContextCompactionError(
            "COMPACTION_SOURCE_UNIT_UNKNOWN", ",".join(unknown)
        )
    overlap = sorted(retain & summarized)
    if overlap:
        raise ContextCompactionError(
            "COMPACTION_SOURCE_UNIT_MULTIPLE_DESTINATIONS", ",".join(overlap)
        )
    missing = sorted(set(units) - (retain | summarized))
    if missing:
        raise ContextCompactionError(
            "COMPACTION_SOURCE_UNIT_DESTINATION_MISSING", ",".join(missing)
        )
    if summarized and not typed.summary.strip():
        raise ContextCompactionError("COMPACTION_SUMMARY_REQUIRED")

    for group_id, members in manifest.protocol_groups.items():
        dispositions = {
            "retain" if unit_id in retain else "summary"
            for unit_id in members
        }
        if len(dispositions) != 1:
            raise ContextCompactionError(
                "COMPACTION_PROTOCOL_GROUP_SPLIT", group_id
            )

    selected_summaries = {
        unit_id for unit_id in summarized if units[unit_id].selected
    }
    if selected_summaries and typed.mode != "capacity_pressure":
        raise ContextCompactionError(
            "COMPACTION_SELECTED_SUMMARY_REQUIRES_CAPACITY_PRESSURE",
            ",".join(sorted(selected_summaries)),
        )

    recovery_by_id: dict[str, MaterialRetrievalState] = {}
    for recovery in typed.material_retrieval_state:
        if recovery.source_unit_id in recovery_by_id:
            raise ContextCompactionError("MATERIAL_RECOVERY_ENTRY_DUPLICATE")
        recovery_by_id[recovery.source_unit_id] = recovery
        unit = units.get(recovery.source_unit_id)
        if unit is None or unit.unit_id not in summarized:
            raise ContextCompactionError(
                "MATERIAL_RECOVERY_SOURCE_INVALID", recovery.source_unit_id
            )
        if recovery.skill_id != unit.skill_id:
            raise ContextCompactionError(
                "MATERIAL_RECOVERY_SKILL_MISMATCH", unit.unit_id
            )
        if recovery.source_ref != unit.source_ref:
            raise ContextCompactionError(
                "MATERIAL_RECOVERY_REF_MISMATCH", unit.unit_id
            )
        if _normalized_digest(recovery.content_hash) != _normalized_digest(
            unit.content_hash
        ):
            raise ContextCompactionError(
                "MATERIAL_RECOVERY_HASH_MISMATCH", unit.unit_id
            )
        if _thaw_json(recovery.coverage) != _thaw_json(unit.coverage):
            raise ContextCompactionError(
                "MATERIAL_RECOVERY_COVERAGE_MISMATCH", unit.unit_id
            )
        if recovery.scope is not None and (
            _thaw_json(recovery.scope) != _thaw_json(unit.scope)
        ):
            raise ContextCompactionError(
                "MATERIAL_RECOVERY_SCOPE_MISMATCH", unit.unit_id
            )
        allowed_retrieval_refs = {
            unit.source_ref,
            unit.body_ref,
            str(unit.provenance.get("retrieval_ref") or ""),
        }
        allowed_retrieval_refs.discard("")
        if recovery.retrieval_ref not in allowed_retrieval_refs:
            raise ContextCompactionError(
                "MATERIAL_RECOVERY_RETRIEVAL_REF_MISMATCH", unit.unit_id
            )

    missing_recovery = sorted(selected_summaries - set(recovery_by_id))
    if missing_recovery:
        raise ContextCompactionError(
            "COMPACTION_SELECTED_RECOVERY_STATE_REQUIRED",
            ",".join(missing_recovery),
        )
    # The model chooses sets, not provider message order.  Canonical source
    # order is runtime authority and prevents a valid set from reordering
    # assistant/tool/result history.
    ordered_retain = tuple(
        unit.unit_id for unit in manifest.units if unit.unit_id in retain
    )
    ordered_summary = tuple(
        unit.unit_id for unit in manifest.units if unit.unit_id in summarized
    )
    recovery_order = {unit.unit_id: index for index, unit in enumerate(manifest.units)}
    ordered_recovery = tuple(sorted(
        typed.material_retrieval_state,
        key=lambda item: recovery_order[item.source_unit_id],
    ))
    return replace(
        typed,
        retain_exact_unit_ids=ordered_retain,
        summary_source_unit_ids=ordered_summary,
        material_retrieval_state=ordered_recovery,
    )


@dataclass(frozen=True)
class RetainedSourceMaterial:
    unit: SourceUnit
    body: str

    def __post_init__(self) -> None:
        if resolve_source_body(self.unit, {self.unit.body_ref: self.body}) != self.body:
            raise AssertionError("unreachable")

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        value = self.unit.to_dict()
        if include_body:
            value["body"] = self.body
        return value


@dataclass(frozen=True)
class ContextEpochRecord:
    epoch_id: str
    predecessor_epoch_id: str
    source_epoch_id: str
    source_manifest_hash: str
    history_version: int
    selected_revision: int
    compaction_request_id: str
    compaction_plan_hash: str
    source_archive_ref: str
    boundary_reason: str = "context_compaction"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
            "epoch_id": self.epoch_id,
            "predecessor_epoch_id": self.predecessor_epoch_id,
            "source_epoch_id": self.source_epoch_id,
            "source_manifest_hash": self.source_manifest_hash,
            "history_version": self.history_version,
            "selected_revision": self.selected_revision,
            "compaction_request_id": self.compaction_request_id,
            "compaction_plan_hash": self.compaction_plan_hash,
            "source_archive_ref": self.source_archive_ref,
            "boundary_reason": self.boundary_reason,
        }


@dataclass(frozen=True)
class PreparedContextEpoch:
    record: ContextEpochRecord
    plan: CompactionPlan
    retained_materials: tuple[RetainedSourceMaterial, ...]
    summary: str
    retrieval_state: tuple[MaterialRetrievalState, ...]
    budget: ContextBudgetSnapshot
    hierarchical_compaction: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hierarchical_compaction",
            _freeze_json(
                self.hierarchical_compaction or {},
                field_name="hierarchical_compaction",
            ),
        )

    def to_archive_payload(self) -> dict[str, Any]:
        """Return references/hashes only; exact bodies stay single-sourced."""

        return {
            "record": self.record.to_dict(),
            "plan": self.plan.to_dict(),
            "retained_materials": [
                item.to_dict(include_body=False) for item in self.retained_materials
            ],
            "summary": self.summary,
            "retrieval_state": [item.to_dict() for item in self.retrieval_state],
            "budget": self.budget.to_dict(),
            "hierarchical_compaction": _thaw_json(
                self.hierarchical_compaction
            ),
        }

    def provider_materials(self) -> list[dict[str, Any]]:
        """Resolve the exact new-epoch inherited region for wire assembly."""

        return [
            {
                "kind": "retained_exact_source",
                **item.to_dict(include_body=True),
            }
            for item in self.retained_materials
        ] + ([{
            "kind": "compaction_summary",
            "source_epoch_id": self.record.source_epoch_id,
            "content": self.summary,
            "material_retrieval_state": [
                item.to_dict() for item in self.retrieval_state
            ],
            "authority": "lossy_working_memory_only",
        }] if self.summary else [])


def _assembled_components(
    request: CompactionRequest,
    retained: Sequence[RetainedSourceMaterial],
    plan: CompactionPlan,
) -> dict[str, Any]:
    components = {
        str(key): _thaw_json(value)
        for key, value in request.fixed_components.items()
    }
    components["inherited_materials"] = [
        {
            "unit_id": item.unit.unit_id,
            "source_ref": item.unit.source_ref,
            "content_hash": item.unit.content_hash,
            "coverage": _thaw_json(item.unit.coverage),
            "body": item.body,
        }
        for item in retained
    ]
    if plan.summary:
        components["inherited_materials"].append({
            "kind": "compaction_summary",
            "content": plan.summary,
            "material_retrieval_state": [
                item.to_dict() for item in plan.material_retrieval_state
            ],
            "authority": "lossy_working_memory_only",
        })
    return components


def assemble_compacted_epoch(
    plan: CompactionPlan | Mapping[str, Any],
    request: CompactionRequest,
    resolver: BodyResolver,
    *,
    estimator: TokenEstimator | None = None,
) -> PreparedContextEpoch:
    """Move retained bytes mechanically and verify the resulting safe budget."""

    typed = validate_compaction_plan(plan, request)
    units = request.source_manifest.by_id()
    retained = tuple(
        RetainedSourceMaterial(
            unit=units[unit_id],
            body=resolve_source_body(units[unit_id], resolver),
        )
        for unit_id in typed.retain_exact_unit_ids
    )
    budget = measure_provider_input(
        _assembled_components(request, retained, typed),
        policy=request.budget_policy,
        estimator=estimator,
        cached_input_tokens=request.cached_input_tokens,
    )
    if budget.hard_block:
        feedback = {
            "reason": "retained_context_exceeds_safe_input",
            "estimated_input_tokens": budget.estimated_input_tokens,
            "safe_input_tokens": budget.policy.safe_input_tokens,
            "over_by_tokens": (
                budget.estimated_input_tokens - budget.policy.safe_input_tokens
            ),
            "target_tokens": budget.policy.target_tokens,
            "retain_exact_unit_ids": list(typed.retain_exact_unit_ids),
            "summary_source_unit_ids": list(typed.summary_source_unit_ids),
            "component_token_counts": dict(budget.component_token_counts),
            "estimator_name": budget.estimator_name,
            "estimator_version": budget.estimator_version,
            "exact_or_estimated": budget.exact_or_estimated,
        }
        raise ContextCompactionCapacityError(
            "COMPACTION_RETAINED_CONTEXT_OVER_BUDGET",
            f"over_by_tokens={feedback['over_by_tokens']}",
            feedback=feedback,
        )
    plan_hash = _digest(typed.to_dict())
    epoch_id = "context-epoch-" + _digest({
        "source_epoch_id": request.source_epoch_id,
        "source_manifest_hash": request.source_manifest_hash,
        "compaction_request_id": request.compaction_request_id,
        "plan_hash": plan_hash,
    })[:24]
    return PreparedContextEpoch(
        record=ContextEpochRecord(
            epoch_id=epoch_id,
            predecessor_epoch_id=request.source_epoch_id,
            source_epoch_id=request.source_epoch_id,
            source_manifest_hash=request.source_manifest_hash,
            history_version=request.history_version,
            selected_revision=request.selected_revision,
            compaction_request_id=request.compaction_request_id,
            compaction_plan_hash=plan_hash,
            source_archive_ref=request.source_archive_ref,
        ),
        plan=typed,
        retained_materials=retained,
        summary=typed.summary,
        retrieval_state=typed.material_retrieval_state,
        budget=budget,
    )


def measure_uncompacted_request(
    request: CompactionRequest,
    resolver: BodyResolver,
    *,
    estimator: TokenEstimator | None = None,
) -> ContextBudgetSnapshot:
    """Measure the complete current epoch, including every exact source unit."""

    pseudo_plan = CompactionPlan(
        schema_version=CONTEXT_COMPACTION_SCHEMA_VERSION,
        compaction_request_id=request.compaction_request_id,
        source_epoch_id=request.source_epoch_id,
        source_manifest_hash=request.source_manifest_hash,
        mode="normal",
        retain_exact_unit_ids=tuple(
            item.unit_id for item in request.source_manifest.units
        ),
        summary_source_unit_ids=(),
        summary="",
        material_retrieval_state=(),
        unresolved_items=(),
        selection_rationale="no compaction measurement",
    )
    retained = tuple(
        RetainedSourceMaterial(unit=item, body=resolve_source_body(item, resolver))
        for item in request.source_manifest.units
    )
    return measure_provider_input(
        _assembled_components(request, retained, pseudo_plan),
        policy=request.budget_policy,
        estimator=estimator,
        cached_input_tokens=request.cached_input_tokens,
    )


@dataclass(frozen=True)
class CompactionAttemptRecord:
    attempt_ordinal: int
    status: str
    error_code: str = ""
    detail: str = ""
    capacity_feedback: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capacity_feedback",
            _freeze_json(
                self.capacity_feedback or {}, field_name="capacity_feedback"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_ordinal": self.attempt_ordinal,
            "status": self.status,
            "error_code": self.error_code,
            "detail": self.detail,
            "capacity_feedback": _thaw_json(self.capacity_feedback),
        }


@dataclass(frozen=True)
class PreparedCompaction:
    epoch: PreparedContextEpoch
    attempts: tuple[CompactionAttemptRecord, ...]
    initial_budget: ContextBudgetSnapshot
    hierarchy: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hierarchy",
            _freeze_json(self.hierarchy or {}, field_name="hierarchy"),
        )






def _compactor_wire_safe_tokens(policy: ContextBudgetPolicy) -> int:
    """Return the actual total-input ceiling for one compactor invocation."""

    # The compactor is an independent model node with the shared configured
    # input target, even when a caller sets an earlier business trigger.
    return min(configured_context_policy().input_soft_limit_tokens, int(
        policy.compressor_safe_input_tokens
        if policy.compressor_safe_input_tokens is not None
        else policy.safe_input_tokens
    ))


def measure_compactor_wire_tokens(
    messages: Sequence[Mapping[str, str]],
    *,
    policy: ContextBudgetPolicy,
    estimator: TokenEstimator | None = None,
) -> int:
    """Measure one complete compactor wire, including configured overhead.

    Cached tokens are deliberately irrelevant here.  The canonical mapping
    includes roles and message boundaries; provider-specific wrappers that the
    estimator cannot see remain represented by the central policy overhead.
    """

    active_estimator = estimator or ReferenceTokenEstimator()
    payload = _canonical_json([dict(item) for item in messages])
    return (
        active_estimator.estimate(payload).tokens
        + int(policy.compressor_prompt_overhead_tokens)
    )


def _ensure_compactor_wire_safe(
    messages: Sequence[Mapping[str, str]],
    *,
    request: CompactionRequest,
    estimator: TokenEstimator,
    operation: str,
) -> int:
    estimated = measure_compactor_wire_tokens(
        messages,
        policy=request.budget_policy,
        estimator=estimator,
    )
    safe = _compactor_wire_safe_tokens(request.budget_policy)
    if estimated > safe:
        raise ContextCompactionCapacityError(
            "COMPACTOR_PROVIDER_INPUT_UNSAFE",
            f"operation={operation}; estimated={estimated}; safe={safe}",
            feedback={
                "operation": operation,
                "estimated_input_tokens": estimated,
                "compressor_safe_input_tokens": safe,
                "over_by_tokens": estimated - safe,
            },
        )
    return estimated


@dataclass(frozen=True)
class HierarchicalSummaryNode:
    """One lossy node whose immutable leaf coverage is runtime-verified."""

    summary_id: str
    summary_hash: str
    summary: str
    level: int
    input_summary_ids: tuple[str, ...]
    coverage_spans: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "summary_id", _require_text(self.summary_id, "summary_id")
        )
        summary = _require_text(self.summary, "summary")
        object.__setattr__(self, "summary", summary)
        supplied = _normalized_digest(
            _require_hash(self.summary_hash, "summary_hash")
        )
        if supplied != _text_digest(summary):
            raise ContextCompactionError("HIERARCHICAL_SUMMARY_HASH_MISMATCH")
        object.__setattr__(self, "summary_hash", supplied)
        object.__setattr__(
            self, "level", _require_nonnegative_int(self.level, "level")
        )
        inputs = tuple(
            _require_text(item, "input_summary_ids[]")
            for item in self.input_summary_ids
        )
        if not inputs or len(set(inputs)) != len(inputs):
            raise ContextCompactionError("HIERARCHICAL_INPUT_IDS_INVALID")
        object.__setattr__(self, "input_summary_ids", inputs)
        spans = tuple(
            _freeze_json(item, field_name="coverage_spans[]")
            for item in self.coverage_spans
        )
        if not spans:
            raise ContextCompactionError("HIERARCHICAL_COVERAGE_REQUIRED")
        object.__setattr__(self, "coverage_spans", spans)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "summary_id": self.summary_id,
            "summary_hash": self.summary_hash,
            "summary": self.summary,
            "level": self.level,
            "input_summary_ids": list(self.input_summary_ids),
            "coverage_spans": [
                _thaw_json(item) for item in self.coverage_spans
            ],
        }

    def to_trace_dict(self) -> dict[str, Any]:
        value = self.to_prompt_dict()
        value.pop("summary")
        value["summary_length_chars"] = len(self.summary)
        return value


def _source_span(parent: SourceUnit, segment: SourceUnit) -> dict[str, Any]:
    coverage = _thaw_json(segment.coverage)
    if str(coverage.get("kind") or "") == "full" or (
        segment.body_length_chars == parent.body_length_chars
        and _normalized_digest(segment.content_hash) == _normalized_digest(parent.content_hash)
    ):
        # An unsplit registered excerpt may retain source line-range coverage.
        # Its identical body is the full parent unit in local codepoint space.
        start = 0
        end = parent.body_length_chars
    else:
        try:
            start = int(coverage.get("start_offset"))
            end = int(coverage.get("end_offset"))
        except (TypeError, ValueError) as exc:
            raise ContextCompactionError("HIERARCHICAL_SEGMENT_COVERAGE_INVALID", segment.unit_id) from exc
        if (
            int(coverage.get("source_length") or -1)
            != parent.body_length_chars
            or _normalized_digest(
                str(coverage.get("source_content_hash") or "")
            )
            != _normalized_digest(parent.content_hash)
        ):
            raise ContextCompactionError(
                "HIERARCHICAL_SEGMENT_PARENT_IDENTITY_MISMATCH",
                segment.unit_id,
            )
    if start < 0 or end <= start or end > parent.body_length_chars:
        raise ContextCompactionError(
            "HIERARCHICAL_SEGMENT_COVERAGE_INVALID", segment.unit_id
        )
    return {
        "parent_source_unit_id": parent.unit_id,
        "source_ref": parent.source_ref,
        "source_content_hash": parent.content_hash,
        "start_offset": start,
        "end_offset": end,
        "source_length": parent.body_length_chars,
        "range_unit": "unicode_codepoint",
    }


def _coalesced_coverage_spans(
    nodes: Sequence[HierarchicalSummaryNode],
) -> tuple[Mapping[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        for raw in node.coverage_spans:
            span = deepcopy(_thaw_json(raw))
            required = {
                "parent_source_unit_id", "source_ref", "source_content_hash",
                "start_offset", "end_offset", "source_length", "range_unit",
            }
            if set(span) != required:
                raise ContextCompactionError(
                    "HIERARCHICAL_COVERAGE_SCHEMA_INVALID"
                )
            start = _require_nonnegative_int(span["start_offset"], "start_offset")
            end = _require_nonnegative_int(span["end_offset"], "end_offset")
            length = _require_nonnegative_int(
                span["source_length"], "source_length"
            )
            if end <= start or end > length:
                raise ContextCompactionError(
                    "HIERARCHICAL_COVERAGE_RANGE_INVALID"
                )
            span["start_offset"] = start
            span["end_offset"] = end
            span["source_length"] = length
            span["source_content_hash"] = _require_hash(
                span["source_content_hash"], "source_content_hash"
            )
            if str(span["range_unit"]) != "unicode_codepoint":
                raise ContextCompactionError(
                    "HIERARCHICAL_COVERAGE_UNIT_INVALID"
                )
            if result:
                previous = result[-1]
                same_source = all(
                    previous[key] == span[key]
                    for key in (
                        "parent_source_unit_id", "source_ref",
                        "source_content_hash", "source_length", "range_unit",
                    )
                )
                if same_source and previous["end_offset"] == start:
                    previous["end_offset"] = end
                    continue
                if same_source and start < previous["end_offset"]:
                    raise ContextCompactionError(
                        "HIERARCHICAL_COVERAGE_OVERLAP"
                    )
            result.append(span)
    return tuple(_freeze_json(item, field_name="coverage_span") for item in result)


def _verify_complete_hierarchical_coverage(
    nodes: Sequence[HierarchicalSummaryNode],
    manifest: SourceUnitManifest,
    exact_units: Sequence[SourceUnit] = (),
) -> tuple[Mapping[str, Any], ...]:
    spans = _coalesced_coverage_spans(nodes)
    by_parent: dict[str, list[Mapping[str, Any]]] = {}
    for span in spans:
        by_parent.setdefault(str(span["parent_source_unit_id"]), []).append(span)
    units = manifest.by_id()
    exact_ids = {unit.unit_id for unit in exact_units}
    if set(by_parent) & exact_ids or set(by_parent) | exact_ids != set(units):
        raise ContextCompactionError(
            "HIERARCHICAL_SOURCE_COVERAGE_INCOMPLETE"
        )
    for unit in manifest.units:
        if unit.unit_id in exact_ids:
            continue
        cursor = 0
        for span in by_parent[unit.unit_id]:
            if (
                int(span["start_offset"]) != cursor
                or str(span["source_ref"]) != unit.source_ref
                or _normalized_digest(str(span["source_content_hash"]))
                != _normalized_digest(unit.content_hash)
                or int(span["source_length"]) != unit.body_length_chars
            ):
                raise ContextCompactionError(
                    "HIERARCHICAL_SOURCE_COVERAGE_INCOMPLETE", unit.unit_id
                )
            cursor = int(span["end_offset"])
        if cursor != unit.body_length_chars:
            raise ContextCompactionError(
                "HIERARCHICAL_SOURCE_COVERAGE_INCOMPLETE", unit.unit_id
            )
    return spans


def _response_mapping(value: Any, *, operation: str) -> Mapping[str, Any]:
    raw = getattr(value, "content", value)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContextCompactionError(
                "HIERARCHICAL_RESPONSE_JSON_INVALID",
                f"operation={operation}; line={exc.lineno}; column={exc.colno}",
            ) from exc
    if not isinstance(raw, Mapping):
        raise ContextCompactionError(
            "HIERARCHICAL_RESPONSE_INVALID", operation
        )
    _reject_mutation_fields(raw, path=operation)
    return raw


def _segment_map_payload(
    request: CompactionRequest,
    parent: SourceUnit,
    segment: SourceUnit,
    body: str,
) -> dict[str, Any]:
    return {
        "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
        "operation": "map_source_segment",
        "compaction_request_id": request.compaction_request_id,
        "source_epoch_id": request.source_epoch_id,
        "source_manifest_hash": request.source_manifest_hash,
        "parent_source_unit": parent.to_dict(),
        "segment_source_unit": {**segment.to_dict(), "body": body},
    }


def _parse_segment_map_response(
    value: Any,
    *,
    request: CompactionRequest,
    parent: SourceUnit,
    segment: SourceUnit,
) -> tuple[str, str]:
    raw = _response_mapping(value, operation="map_source_segment")
    allowed = {
        "schema_version", "operation", "compaction_request_id",
        "source_epoch_id", "source_manifest_hash", "segment_unit_id",
        "parent_source_unit_id", "parent_source_content_hash",
        "segment_content_hash", "coverage", "summary", "missing_details",
    }
    _reject_unknown_keys(raw, allowed, "segment_map_response")
    expected = {
        "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
        "operation": "map_source_segment",
        "compaction_request_id": request.compaction_request_id,
        "source_epoch_id": request.source_epoch_id,
        "source_manifest_hash": request.source_manifest_hash,
        "segment_unit_id": segment.unit_id,
        "parent_source_unit_id": parent.unit_id,
        "parent_source_content_hash": parent.content_hash,
        "segment_content_hash": segment.content_hash,
        "coverage": _thaw_json(segment.coverage),
    }
    observed = {key: _thaw_json(raw.get(key)) for key in expected}
    observed["schema_version"] = _require_nonnegative_int(
        observed["schema_version"], "schema_version"
    )
    for key in (
        "source_manifest_hash", "parent_source_content_hash",
        "segment_content_hash",
    ):
        observed[key] = _normalized_digest(
            _require_hash(observed[key], key)
        )
        expected[key] = _normalized_digest(str(expected[key]))
    if observed != expected:
        raise ContextCompactionError(
            "HIERARCHICAL_MAP_IDENTITY_MISMATCH", segment.unit_id
        )
    return (
        _require_text(raw.get("summary"), "summary"),
        _require_text(raw.get("missing_details"), "missing_details"),
    )


def _merge_payload(
    request: CompactionRequest,
    nodes: Sequence[HierarchicalSummaryNode],
    *,
    level: int,
) -> tuple[str, dict[str, Any]]:
    merge_request_id = "hmerge-" + _digest({
        "compaction_request_id": request.compaction_request_id,
        "level": level,
        "inputs": [item.summary_id for item in nodes],
        "hashes": [item.summary_hash for item in nodes],
    })[:24]
    coverage = _coalesced_coverage_spans(nodes)
    return merge_request_id, {
        "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
        "operation": "merge_segment_summaries",
        "compaction_request_id": request.compaction_request_id,
        "source_epoch_id": request.source_epoch_id,
        "source_manifest_hash": request.source_manifest_hash,
        "merge_request_id": merge_request_id,
        "input_summary_ids": [item.summary_id for item in nodes],
        "input_summary_hashes": [item.summary_hash for item in nodes],
        "coverage_spans": [_thaw_json(item) for item in coverage],
        "input_summaries": [item.to_prompt_dict() for item in nodes],
    }


def _parse_merge_response(
    value: Any,
    *,
    request: CompactionRequest,
    merge_request_id: str,
    nodes: Sequence[HierarchicalSummaryNode],
) -> str:
    raw = _response_mapping(value, operation="merge_segment_summaries")
    allowed = {
        "schema_version", "operation", "compaction_request_id",
        "source_epoch_id", "source_manifest_hash", "merge_request_id",
        "input_summary_ids", "input_summary_hashes", "coverage_spans",
        "summary",
    }
    _reject_unknown_keys(raw, allowed, "summary_merge_response")
    expected = {
        "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
        "operation": "merge_segment_summaries",
        "compaction_request_id": request.compaction_request_id,
        "source_epoch_id": request.source_epoch_id,
        "source_manifest_hash": request.source_manifest_hash,
        "merge_request_id": merge_request_id,
        "input_summary_ids": [item.summary_id for item in nodes],
        "input_summary_hashes": [item.summary_hash for item in nodes],
        "coverage_spans": [
            _thaw_json(item) for item in _coalesced_coverage_spans(nodes)
        ],
    }
    observed = {key: _thaw_json(raw.get(key)) for key in expected}
    observed["schema_version"] = _require_nonnegative_int(
        observed["schema_version"], "schema_version"
    )
    observed["source_manifest_hash"] = _normalized_digest(
        _require_hash(observed["source_manifest_hash"], "source_manifest_hash")
    )
    expected["source_manifest_hash"] = _normalized_digest(
        str(expected["source_manifest_hash"])
    )
    observed["input_summary_hashes"] = [
        _normalized_digest(_require_hash(item, "input_summary_hashes[]"))
        for item in _sequence(
            observed["input_summary_hashes"], "input_summary_hashes"
        )
    ]
    if observed != expected:
        raise ContextCompactionError(
            "HIERARCHICAL_MERGE_IDENTITY_MISMATCH", merge_request_id
        )
    return _require_text(raw.get("summary"), "summary")


def _hierarchical_selector_payload(
    request: CompactionRequest,
    nodes: Sequence[HierarchicalSummaryNode],
    exact_materials: Sequence[RetainedSourceMaterial] = (),
) -> dict[str, Any]:
    coverage = _verify_complete_hierarchical_coverage(
        nodes, request.source_manifest, [item.unit for item in exact_materials]
    )
    return {
        "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
        "input_mode": "hierarchical_lossy_coverage",
        "compaction_request_id": request.compaction_request_id,
        "source_epoch_id": request.source_epoch_id,
        "source_manifest_hash": request.source_manifest_hash,
        "history_version": request.history_version,
        "selected_revision": request.selected_revision,
        "budget": request.budget_policy.to_dict(),
        "fixed_components": _thaw_json(request.fixed_components),
        "source_manifest": request.source_manifest.to_dict(),
        "hierarchical_summary_nodes": [
            item.to_prompt_dict() for item in nodes
        ],
        "model_visible_source_coverage": [
            _thaw_json(item) for item in coverage
        ],
        "protected_exact_source_units": [item.to_dict(include_body=True) for item in exact_materials],
        "must_retain_exact_unit_ids": [item.unit.unit_id for item in exact_materials],
        "prior_summary": request.prior_summary,
        "prior_retrieval_state": [
            _thaw_json(item) for item in request.prior_retrieval_state
        ],
        "source_archive_ref": request.source_archive_ref,
    }


def _fit_source_segments_for_map(
    request: CompactionRequest,
    unit: SourceUnit,
    body: str,
    *,
    estimator: TokenEstimator,
) -> LosslessSegmentResult:
    """Find the largest capacity-only segment size whose real map wire fits."""

    safe = _compactor_wire_safe_tokens(request.budget_policy)
    safe -= min(512, safe // 20)
    upper = max(1, estimator.estimate(body).tokens)
    low = 1
    best: LosslessSegmentResult | None = None
    while low <= upper:
        midpoint = (low + upper) // 2
        candidate = segment_source_unit_losslessly(
            unit, body, max_segment_tokens=midpoint, estimator=estimator
        )
        fits = True
        for segment in candidate.segments:
            segment_payload = _segment_map_payload(
                request, unit, segment, candidate.bodies[segment.body_ref]
            )
            for attempt_ordinal, validation_feedback in (
                (1, {}),
                (2, _HIERARCHICAL_RETRY_RESERVE_FEEDBACK),
            ):
                messages = build_context_segment_map_messages(
                    segment_payload,
                    attempt_ordinal=attempt_ordinal,
                    validation_feedback=validation_feedback,
                )
                if measure_compactor_wire_tokens(
                    messages, policy=request.budget_policy, estimator=estimator
                ) > safe:
                    fits = False
                    break
            if not fits:
                break
        if fits:
            best = candidate
            low = midpoint + 1
        else:
            upper = midpoint - 1
    if best is None:
        raise ContextCompactionCapacityError(
            "HIERARCHICAL_SEGMENT_MAP_ENVELOPE_UNSAFE",
            f"unit={unit.unit_id}; compressor_safe_input_tokens={safe}",
            feedback={
                "source_unit_id": unit.unit_id,
                "compressor_safe_input_tokens": safe,
                "reason": "map envelope leaves no capacity for one source codepoint",
            },
        )
    if best.reconstruct() != body:
        raise ContextCompactionError(
            "HIERARCHICAL_SEGMENT_RECONSTRUCTION_FAILED", unit.unit_id
        )
    return best




def _protected_selector_messages(request, exact_materials):
    """Conservative final selector shell, before any paid map operation.

    Include the same fixed region, manifest and exact bodies as the final
    selector, plus the existing retry allowance. Summary nodes are measured
    again after map/reduce assembly; their size is not guessed here.
    """
    return build_context_compaction_messages({
        "schema_version": CONTEXT_COMPACTION_SCHEMA_VERSION,
        "input_mode": "hierarchical_lossy_coverage",
        "compaction_request_id": request.compaction_request_id,
        "source_epoch_id": request.source_epoch_id,
        "source_manifest_hash": request.source_manifest_hash,
        "history_version": request.history_version,
        "selected_revision": request.selected_revision,
        "budget": request.budget_policy.to_dict(),
        "fixed_components": _thaw_json(request.fixed_components),
        "source_manifest": request.source_manifest.to_dict(),
        "protected_exact_source_units": [item.to_dict(include_body=True) for item in exact_materials],
        "must_retain_exact_unit_ids": [item.unit.unit_id for item in exact_materials],
        "prior_summary": request.prior_summary,
        "prior_retrieval_state": [_thaw_json(item) for item in request.prior_retrieval_state],
        "source_archive_ref": request.source_archive_ref,
    }, attempt_ordinal=2, capacity_feedback=_HIERARCHICAL_RETRY_RESERVE_FEEDBACK)






@dataclass(frozen=True)
class CompactionDecision:
    triggered: bool
    reason: str
    initial_budget: ContextBudgetSnapshot
    preparation: PreparedCompaction | None = None




@dataclass(frozen=True)
class ActiveEpochPointer:
    state_generation: int
    epoch_id: str
    epoch_archive_ref: str
    history_version: int
    selected_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state_generation",
            _require_nonnegative_int(self.state_generation, "state_generation"),
        )
        object.__setattr__(self, "epoch_id", _require_text(self.epoch_id, "epoch_id"))
        object.__setattr__(
            self,
            "history_version",
            _require_nonnegative_int(self.history_version, "history_version"),
        )
        object.__setattr__(
            self,
            "selected_revision",
            _require_nonnegative_int(
                self.selected_revision, "selected_revision"
            ),
        )
        object.__setattr__(
            self, "epoch_archive_ref", str(self.epoch_archive_ref or "").strip()
        )


@dataclass(frozen=True)
class EpochActivationReceipt:
    previous: ActiveEpochPointer
    active: ActiveEpochPointer
    candidate_epoch_archive_ref: str
    already_active: bool = False


def activate_epoch_cas(
    current: ActiveEpochPointer,
    *,
    expected_state_generation: int,
    expected_epoch_id: str,
    expected_history_version: int,
    expected_selected_revision: int,
    candidate: PreparedContextEpoch,
    candidate_epoch_archive_ref: str,
) -> EpochActivationReceipt:
    """Pure CAS: return a new immutable pointer or leave ``current`` intact."""

    expected = {
        "state_generation": _require_nonnegative_int(
            expected_state_generation, "expected_state_generation"
        ),
        "epoch_id": _require_text(expected_epoch_id, "expected_epoch_id"),
        "history_version": _require_nonnegative_int(
            expected_history_version, "expected_history_version"
        ),
        "selected_revision": _require_nonnegative_int(
            expected_selected_revision, "expected_selected_revision"
        ),
    }
    observed = {
        "state_generation": current.state_generation,
        "epoch_id": current.epoch_id,
        "history_version": current.history_version,
        "selected_revision": current.selected_revision,
    }
    archive_ref = _require_text(
        candidate_epoch_archive_ref, "candidate_epoch_archive_ref"
    )
    if (
        current.epoch_id == candidate.record.epoch_id
        and current.epoch_archive_ref == archive_ref
        and current.state_generation == expected["state_generation"] + 1
        and current.history_version == expected["history_version"]
        and current.selected_revision == expected["selected_revision"]
    ):
        # Deterministic replay after a crash immediately following the real
        # PlanState CAS.  No second activation or business side effect is needed.
        return EpochActivationReceipt(
            previous=current,
            active=current,
            candidate_epoch_archive_ref=archive_ref,
            already_active=True,
        )
    if observed != expected:
        raise ContextEpochCASConflict(
            "CONTEXT_EPOCH_CAS_CONFLICT",
            f"expected={_canonical_json(expected)}; observed={_canonical_json(observed)}",
        )
    if (
        candidate.record.source_epoch_id != expected["epoch_id"]
        or candidate.record.history_version != expected["history_version"]
        or candidate.record.selected_revision != expected["selected_revision"]
    ):
        raise ContextEpochCASConflict(
            "CONTEXT_EPOCH_CANDIDATE_STALE",
            candidate.record.epoch_id,
        )
    active = ActiveEpochPointer(
        state_generation=current.state_generation + 1,
        epoch_id=candidate.record.epoch_id,
        epoch_archive_ref=archive_ref,
        history_version=current.history_version,
        selected_revision=current.selected_revision,
    )
    return EpochActivationReceipt(
        previous=current,
        active=active,
        candidate_epoch_archive_ref=archive_ref,
    )








__all__ = ['ActiveEpochPointer',
 'CompactionAttemptRecord',
 'CompactionDecision',
 'CompactionPlan',
 'CompactionRequest',
 'ContextBudgetPolicy',
 'ContextBudgetSnapshot',
 'ContextCompactionAttemptsExhausted',
 'ContextCompactionCapacityError',
 'ContextCompactionError',
 'ContextEpochCASConflict',
 'ContextEpochRecord',
 'DEFAULT_APPLICATION_CONTEXT_LIMIT_TOKENS',
 'DEFAULT_COMPACTION_MAX_ATTEMPTS',
 'DEFAULT_HIERARCHICAL_MAX_LEVELS',
 'DEFAULT_HIERARCHICAL_MAX_MODEL_CALLS',
 'EpochActivationReceipt',
 'LosslessSegmentResult',
 'HierarchicalSummaryNode',
 'MaterialRetrievalState',
 'PreparedCompaction',
 'PreparedContextEpoch',
 'ReferenceTokenEstimator',
 'RetainedSourceMaterial',
 'SourceUnit',
 'SourceUnitManifest',
 'TokenEstimate',
 'TokenEstimator',
 'activate_epoch_cas',
 'assemble_compacted_epoch',
 'build_source_unit_manifest',
 'measure_provider_input',
 'measure_compactor_wire_tokens',
 'measure_uncompacted_request',
 'new_compaction_request',
 'parse_compaction_plan',
 'resolve_source_body',
 'segment_source_unit_losslessly',
 'validate_compaction_plan']

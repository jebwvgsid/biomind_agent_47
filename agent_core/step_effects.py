"""Route-blind StepEffect compilation and completion evidence primitives.

``outputs`` remains the immutable-artifact data-flow contract.  This module
adds the orthogonal answer to "what does this step accomplish?": a step may
produce artifacts, bindings, or observations, mutate a declared workspace, or
perform a capability-declared side effect.

The compiler is deliberately semantic-mechanism-only:

* it never classifies task prose or special-cases a Skill;
* it can fill omitted effects only from one selected capability contract;
* ambiguity is returned as structured evidence, never as a graph destination;
* compilation receipts are deterministic and bound to the Plan, step, and
  capability identities they certify; and
* evidence matching checks declared identities without requiring a file for a
  non-artifact effect.

Historical Plans can still be read.  Artifact outputs are projected into a
StepEffect automatically, while explicitly marked legacy Plans may retain an
untyped, empty effect at their legacy execution boundary.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


STEP_EFFECT_SCHEMA_VERSION = 1
STEP_EFFECT_COMPILER_VERSION = 1
STEP_EFFECT_RECEIPT_SCHEMA_VERSION = 1
STEP_EFFECT_COMPILER_ID = "biomind.step_effect_compiler.v1"
PLAN_DATAFLOW_COMPILER_VERSION = 1
PLAN_DATAFLOW_RECEIPT_SCHEMA_VERSION = 1
PLAN_DATAFLOW_COMPILER_ID = "biomind.plan_dataflow_compiler.v1"
STRUCTURED_EFFECT_EVIDENCE_PREFIX = "BIOMIND_EFFECT_EVIDENCE_JSON="
STRUCTURED_EFFECT_EMISSION_VERSION = 1

CANONICAL_STEP_EFFECT_FIELDS: Tuple[str, ...] = (
    "produces_artifacts",
    "produces_bindings",
    "produces_observations",
    "workspace_mutations",
    "explicit_side_effects",
)

EFFECT_KIND_BY_FIELD = {
    "produces_artifacts": "artifact",
    "produces_bindings": "binding",
    "produces_observations": "observation",
    "workspace_mutations": "workspace_mutation",
    "explicit_side_effects": "explicit_side_effect",
}

_EFFECT_ALIASES = {
    "produces_artifacts": ("produces_artifacts", "artifacts"),
    "produces_bindings": ("produces_bindings", "bindings"),
    "produces_observations": ("produces_observations", "observations"),
    "workspace_mutations": (
        "workspace_mutations",
        "mutates_workspaces",
        "workspaces",
    ),
    "explicit_side_effects": (
        "explicit_side_effects",
        "side_effects",
        "explicit_side_effect",
    ),
}

_IDENTITY_KEYS = {
    "produces_artifacts": (
        "identity",
        "artifact_id",
        "object_ref",
        "local_ref",
        "output_id",
        "path",
        "name",
        "id",
    ),
    "produces_bindings": ("identity", "binding_id", "name", "id"),
    "produces_observations": (
        "identity",
        "observation_id",
        "result_id",
        "name",
        "id",
    ),
    "workspace_mutations": (
        "identity",
        "workspace_ref",
        "workspace_id",
        "artifact_id",
        "object_ref",
        "path",
        "name",
        "id",
    ),
    "explicit_side_effects": (
        "identity",
        "effect_id",
        "side_effect_id",
        "name",
        "id",
    ),
}

_WRAPPER_FIELDS = {"step_effect", "effects"}
_ROUTING_KEYS = {
    "route",
    "next_step",
    "goto",
    "proposed_route",
    "effective_route",
    "dispatch_route",
    "route_resolution",
    "node_recommended_next_step",
    "required_next_action",
    "pending_recovery_action",
    "resume_route_hint",
    "target_route",
    "next_route",
    "planner_full_replan",
    "resolved_resume_node",
    "resume_step",
    "terminal_route",
    "graph_node",
    "graph_node_name",
    "source_graph_node",
    "destination_graph_node",
    "controller_transition",
    "controller_transition_metadata",
    "command_goto",
    "route_event_id",
}
_FAILED_EVIDENCE_STATUSES = {
    "absent",
    "blocked",
    "failed",
    "invalid",
    "missing",
    "rejected",
    "unverified",
}
_VOLATILE_STEP_KEYS = {
    "attempt",
    "attempts",
    "created_at",
    "evidence",
    "execution_provenance",
    "last_error",
    "progress",
    "runtime_state",
    "status",
    "updated_at",
}
_PLAN_PROJECTION_FIELDS = (
    "task_id",
    "plan_id",
    "plan_version",
    "plan_revision",
    "proposed_artifacts",
    "task_output_materializations",
    "metadata",
)
_BUILTIN_ACTION_SIDE_EFFECTS = {
    "ask_user": "user_input_requested",
    "install": "runtime_installation",
    "finish": "task_response",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _json_value(value: Any, *, field_path: str) -> Any:
    """Return deterministic JSON data or raise a typed protocol error."""

    if value is None or isinstance(value, (str, bool, int)):
        return deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StepEffectProtocolError(
                "STEP_EFFECT_VALUE_INVALID",
                field_path,
                "Effect metadata must not contain NaN or Infinity.",
                actual_type="number",
            )
        return value
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise StepEffectProtocolError(
                    "STEP_EFFECT_VALUE_INVALID",
                    field_path,
                    "Effect metadata object keys must be non-empty strings.",
                    actual_type=type(raw_key).__name__,
                )
            result[raw_key] = _json_value(
                raw_value, field_path=f"{field_path}.{raw_key}"
            )
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, field_path=f"{field_path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        normalized = [
            _json_value(item, field_path=f"{field_path}[]") for item in value
        ]
        return sorted(normalized, key=_canonical_json)
    raise StepEffectProtocolError(
        "STEP_EFFECT_VALUE_INVALID",
        field_path,
        "Effect metadata must be JSON-compatible.",
        actual_type=type(value).__name__,
    )


def _route_free(value: Any) -> Any:
    """Recursively remove legacy control-plane vocabulary from diagnostics."""

    if isinstance(value, Mapping):
        return {
            str(key): _route_free(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _ROUTING_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_route_free(item) for item in value]
    return deepcopy(value)


class StepEffectProtocolError(ValueError):
    """A candidate effect representation is structurally invalid.

    This exception describes the candidate only.  It carries no workflow edge
    or task-terminal semantics.
    """

    def __init__(
        self,
        code: str,
        field_path: str,
        message: str,
        **evidence: Any,
    ) -> None:
        self.code = str(code or "STEP_EFFECT_PROTOCOL_INVALID")
        self.field_path = str(field_path or "$")
        self.message = str(message or "StepEffect protocol is invalid.")
        self.evidence = _route_free(evidence)
        super().__init__(f"{self.code}: {self.field_path}: {self.message}")

    def to_issue(self, *, step_id: str = "") -> Dict[str, Any]:
        return protocol_invalid_issue(
            step_id=step_id,
            field_path=self.field_path,
            message=self.message,
            violation_code=self.code,
            **self.evidence,
        )


def _issue(
    code: str,
    *,
    disposition: str,
    step_id: str = "",
    field_path: str = "",
    message: str = "",
    source: str = "step_effect_compiler",
    **evidence: Any,
) -> Dict[str, Any]:
    """Build route-free structured evidence for Planner or internal review."""

    body: Dict[str, Any] = {
        "schema_version": 1,
        "code": str(code or "STEP_EFFECT_INVALID"),
        "disposition": str(disposition or "PROTOCOL_INVALID"),
        "severity": "error",
        "field_path": str(field_path or "$"),
        "message": str(message or "StepEffect validation failed."),
        "source": str(source or "step_effect_compiler"),
    }
    if step_id:
        body["step_id"] = str(step_id)
    for key, value in _route_free(evidence).items():
        if value not in (None, "", [], {}):
            body[str(key)] = deepcopy(value)
    return body


def protocol_invalid_issue(
    *,
    step_id: str = "",
    field_path: str = "",
    message: str = "StepEffect protocol is invalid.",
    violation_code: str = "STEP_EFFECT_PROTOCOL_INVALID",
    **evidence: Any,
) -> Dict[str, Any]:
    """Return a candidate-invalid, route-free issue."""

    return _issue(
        "STEP_EFFECT_PROTOCOL_INVALID",
        disposition="PROTOCOL_INVALID",
        step_id=step_id,
        field_path=field_path,
        message=message,
        violation_code=str(violation_code or "STEP_EFFECT_PROTOCOL_INVALID"),
        **evidence,
    )


def unspecified_effect_issue(
    *,
    step_id: str = "",
    field_path: str = "",
    source: str = "step_effect_compiler",
    candidate_sources: Optional[Sequence[str]] = None,
    message: str = "",
) -> Dict[str, Any]:
    """Return semantic-incompleteness evidence without selecting a next node."""

    return _issue(
        "PLAN_STEP_EFFECT_UNSPECIFIED",
        disposition="REPAIR_REQUIRED",
        step_id=step_id,
        field_path=field_path or "$.step_effect",
        message=message
        or (
            "The step declares no observable or durable effect, and its "
            "capability contract does not provide one unique interpretation."
        ),
        source=source,
        possible_effect_kinds=[
            "artifact",
            "binding",
            "observation",
            "workspace_mutation",
            "explicit_side_effect",
        ],
        candidate_sources=list(candidate_sources or []),
        expected="at least one explicit or uniquely inferred StepEffect",
        actual="no interpretable effect",
    )


def _step_protocol_issue(
    exc: StepEffectProtocolError,
    *,
    step_id: str,
    field_root: str,
) -> Dict[str, Any]:
    """Rebase a standalone effect pointer onto its position in the Plan."""

    suffix = exc.field_path[1:] if exc.field_path.startswith("$") else ""
    field_path = f"{field_root}{suffix}" if suffix else field_root
    return protocol_invalid_issue(
        step_id=step_id,
        field_path=field_path,
        message=exc.message,
        violation_code=exc.code,
        **exc.evidence,
    )


def _identity_for_entry(field: str, value: Mapping[str, Any]) -> str:
    for key in _IDENTITY_KEYS[field]:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _identity_text(value: Any, *, field: str) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return _identity_for_entry(field, value)
    return ""


def _canonical_entry(value: Any, *, field: str, field_path: str) -> Any:
    if isinstance(value, str):
        identity = value.strip()
        if not identity or re.search(r"[\x00-\x1f\x7f]", identity):
            raise StepEffectProtocolError(
                "STEP_EFFECT_IDENTITY_INVALID",
                field_path,
                "Effect identity must be a non-empty string without control characters.",
                actual_type="string",
            )
        return identity
    if not isinstance(value, Mapping):
        raise StepEffectProtocolError(
            "STEP_EFFECT_ENTRY_INVALID",
            field_path,
            "Each effect must be a string identity or an object with an identity.",
            actual_type=type(value).__name__,
        )
    normalized = _json_value(value, field_path=field_path)
    identity = _identity_for_entry(field, normalized)
    if not identity or re.search(r"[\x00-\x1f\x7f]", identity):
        raise StepEffectProtocolError(
            "STEP_EFFECT_IDENTITY_INVALID",
            field_path,
            "Effect object lacks a valid identity.",
            accepted_identity_fields=list(_IDENTITY_KEYS[field]),
        )
    result: Dict[str, Any] = {"identity": identity}
    identity_aliases = set(_IDENTITY_KEYS[field])
    for key, item in normalized.items():
        if key not in identity_aliases:
            result[key] = deepcopy(item)
    return result if len(result) > 1 else identity


def _merge_same_identity(
    prior: Any,
    current: Any,
    *,
    field: str,
    field_path: str,
) -> Any:
    if _canonical_json(prior) == _canonical_json(current):
        return deepcopy(prior)
    prior_map = {"identity": prior} if isinstance(prior, str) else dict(prior)
    current_map = {"identity": current} if isinstance(current, str) else dict(current)
    conflict = [
        key
        for key in set(prior_map) & set(current_map)
        if _canonical_json(prior_map[key]) != _canonical_json(current_map[key])
    ]
    if conflict:
        raise StepEffectProtocolError(
            "STEP_EFFECT_IDENTITY_CONFLICT",
            field_path,
            "The same effect identity has conflicting metadata.",
            effect_kind=EFFECT_KIND_BY_FIELD[field],
            identity=_identity_text(prior, field=field),
            conflicting_fields=sorted(conflict),
        )
    merged = {**prior_map, **current_map}
    return merged if len(merged) > 1 else str(merged["identity"])


def _canonical_entries(value: Any, *, field: str, field_path: str) -> list[Any]:
    if value is None:
        return []
    if field == "explicit_side_effects" and isinstance(value, bool):
        raise StepEffectProtocolError(
            "UNTYPED_EXPLICIT_SIDE_EFFECT",
            field_path,
            "A boolean side-effect flag cannot satisfy StepEffect completeness.",
            expected="registered side-effect identity and evidence contract",
            actual_type="boolean",
        )
    raw_values: Sequence[Any]
    if isinstance(value, (str, Mapping)):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_values = list(value)
    else:
        raise StepEffectProtocolError(
            "STEP_EFFECT_LIST_INVALID",
            field_path,
            "Effect field must be an array of identities or effect objects.",
            actual_type=type(value).__name__,
        )
    by_identity: Dict[str, Any] = {}
    for index, item in enumerate(raw_values):
        entry = _canonical_entry(
            item,
            field=field,
            field_path=f"{field_path}[{index}]",
        )
        identity = _identity_text(entry, field=field)
        if identity in by_identity:
            by_identity[identity] = _merge_same_identity(
                by_identity[identity],
                entry,
                field=field,
                field_path=f"{field_path}[{index}]",
            )
        else:
            by_identity[identity] = entry
    return sorted(by_identity.values(), key=_canonical_json)


def _looks_like_step(value: Mapping[str, Any]) -> bool:
    return bool(
        set(value)
        & {
            "step_id",
            "action",
            "inputs",
            "outputs",
            "dependencies",
            "execution_capability_id",
        }
    )


def _effect_sources(value: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    sources: list[Mapping[str, Any]] = []
    wrapped = False
    for wrapper in _WRAPPER_FIELDS:
        if wrapper not in value:
            continue
        wrapped = True
        raw = value.get(wrapper)
        if isinstance(raw, StepEffect):
            sources.append(raw.to_dict())
        elif isinstance(raw, Mapping):
            sources.append(raw)
        elif raw not in (None, ""):
            raise StepEffectProtocolError(
                "STEP_EFFECT_OBJECT_INVALID",
                f"$.{wrapper}",
                "StepEffect wrapper must be an object.",
                actual_type=type(raw).__name__,
            )
    sources.append(value)
    return sources, wrapped


def _field_presence(value: Mapping[str, Any]) -> Dict[str, bool]:
    result = {field: False for field in CANONICAL_STEP_EFFECT_FIELDS}
    sources, _ = _effect_sources(value)
    is_step = _looks_like_step(value)
    for source_index, source in enumerate(sources):
        nested = source_index < len(sources) - 1
        for field, aliases in _EFFECT_ALIASES.items():
            usable = aliases
            if is_step and not nested and field == "produces_artifacts":
                usable = ("produces_artifacts",)
            if any(alias in source for alias in usable):
                result[field] = True
    if is_step and "outputs" in value:
        result["produces_artifacts"] = True
    return result


def canonical_step_effect(value: Any) -> Dict[str, Any]:
    """Return one canonical StepEffect.

    The reader accepts the short ``artifacts/bindings/observations`` aliases
    and flat Planner fields.  The writer emits only the canonical
    ``produces_*`` names.  For a Plan step, ``outputs`` is projected to
    ``produces_artifacts``; a normalized step's broad ``artifacts`` collection
    is intentionally not treated as produced output.
    """

    if isinstance(value, StepEffect):
        return value.to_dict()
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise StepEffectProtocolError(
            "STEP_EFFECT_OBJECT_INVALID",
            "$",
            "StepEffect must be represented by an object.",
            actual_type=type(value).__name__,
        )
    source_value = dict(value)
    sources, _ = _effect_sources(source_value)
    is_step = _looks_like_step(source_value)
    canonical: Dict[str, Any] = {"schema_version": STEP_EFFECT_SCHEMA_VERSION}
    for field, aliases in _EFFECT_ALIASES.items():
        combined: list[Any] = []
        for source_index, source in enumerate(sources):
            nested = source_index < len(sources) - 1
            schema_version = source.get("schema_version") if nested else None
            if schema_version not in (None, "", STEP_EFFECT_SCHEMA_VERSION):
                raise StepEffectProtocolError(
                    "STEP_EFFECT_SCHEMA_UNSUPPORTED",
                    "$.step_effect.schema_version",
                    "Unsupported StepEffect schema version.",
                    expected=STEP_EFFECT_SCHEMA_VERSION,
                    actual=schema_version,
                )
            usable_aliases = aliases
            if is_step and not nested and field == "produces_artifacts":
                # ``step.artifacts`` may include inputs or legacy projections;
                # only outputs/produces_artifacts are production declarations.
                usable_aliases = ("produces_artifacts",)
            for alias in usable_aliases:
                if alias not in source:
                    continue
                combined.extend(
                    _canonical_entries(
                        source.get(alias),
                        field=field,
                        field_path=(
                            f"$.step_effect.{alias}" if nested else f"$.{alias}"
                        ),
                    )
                )
        if is_step and field == "produces_artifacts" and "outputs" in source_value:
            combined.extend(
                _canonical_entries(
                    source_value.get("outputs"),
                    field=field,
                    field_path="$.outputs",
                )
            )
        canonical[field] = _canonical_entries(
            combined,
            field=field,
            field_path=f"$.step_effect.{field}",
        )
    return canonical


@dataclass(frozen=True)
class StepEffect:
    """Immutable Python representation of the canonical wire object."""

    produces_artifacts: Tuple[Any, ...] = ()
    produces_bindings: Tuple[Any, ...] = ()
    produces_observations: Tuple[Any, ...] = ()
    workspace_mutations: Tuple[Any, ...] = ()
    explicit_side_effects: Tuple[Any, ...] = ()
    schema_version: int = STEP_EFFECT_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, value: Any) -> "StepEffect":
        canonical = canonical_step_effect(value)
        return cls(
            produces_artifacts=tuple(canonical["produces_artifacts"]),
            produces_bindings=tuple(canonical["produces_bindings"]),
            produces_observations=tuple(canonical["produces_observations"]),
            workspace_mutations=tuple(canonical["workspace_mutations"]),
            explicit_side_effects=tuple(canonical["explicit_side_effects"]),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **{
                field: deepcopy(list(getattr(self, field)))
                for field in CANONICAL_STEP_EFFECT_FIELDS
            },
        }

    @property
    def complete(self) -> bool:
        return any(getattr(self, field) for field in CANONICAL_STEP_EFFECT_FIELDS)


def step_effect_complete(value: Any) -> bool:
    """Return whether a step has at least one interpretable declared effect."""

    effect = canonical_step_effect(value)
    return any(effect[field] for field in CANONICAL_STEP_EFFECT_FIELDS)


def _entry_map(values: Iterable[Any], *, field: str) -> Dict[str, Any]:
    return {
        _identity_text(value, field=field): deepcopy(value)
        for value in values
        if _identity_text(value, field=field)
    }


def _canonical_resource_id(
    identity: str,
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]],
) -> str:
    """Resolve one exact alias; deliberately perform no fuzzy matching."""

    raw = dict((resource_bindings or {}).get(str(identity or "")) or {})
    return str(raw.get("canonical_resource_id") or identity or "")


def _canonicalize_workspace_effects(
    effect: Mapping[str, Any],
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]],
) -> Dict[str, Any]:
    """Rewrite workspace aliases to the exact identity proven by a binding."""

    result = deepcopy(dict(effect))
    canonical: list[Any] = []
    for raw in result.get("workspace_mutations", []) or []:
        declared = _identity_text(raw, field="workspace_mutations")
        identity = _canonical_resource_id(declared, resource_bindings)
        if isinstance(raw, Mapping):
            row = deepcopy(dict(raw))
            row["identity"] = identity
            for key in _IDENTITY_KEYS["workspace_mutations"]:
                if key != "identity":
                    row.pop(key, None)
            if declared != identity:
                row.setdefault("declared_resource_ref", declared)
            canonical.append(row)
        elif declared != identity:
            canonical.append({
                "identity": identity,
                "declared_resource_ref": declared,
            })
        else:
            canonical.append(identity)
    result["workspace_mutations"] = _canonical_entries(
        canonical,
        field="workspace_mutations",
        field_path="$.step_effect.workspace_mutations",
    )
    return result


def _merge_effects(
    explicit: Mapping[str, Any],
    inferred: Mapping[str, Any],
    *,
    source: str,
    step_id: str,
) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
    result: Dict[str, Any] = {"schema_version": STEP_EFFECT_SCHEMA_VERSION}
    normalizations: list[Dict[str, Any]] = []
    for field in CANONICAL_STEP_EFFECT_FIELDS:
        explicit_map = _entry_map(explicit.get(field) or [], field=field)
        inferred_map = _entry_map(inferred.get(field) or [], field=field)
        combined = dict(explicit_map)
        for identity, entry in inferred_map.items():
            if identity in combined:
                combined[identity] = _merge_same_identity(
                    combined[identity],
                    entry,
                    field=field,
                    field_path=f"$.step_effect.{field}",
                )
                continue
            combined[identity] = deepcopy(entry)
            added_effect: Dict[str, Any] = {
                "kind": EFFECT_KIND_BY_FIELD[field],
                "identity": identity,
            }
            if isinstance(entry, Mapping):
                details = {
                    key: deepcopy(value)
                    for key, value in entry.items()
                    if key != "identity"
                }
                if details:
                    added_effect["details"] = details
            normalizations.append({
                "schema_version": 1,
                "code": "NORMALIZE_STEP_EFFECT",
                "disposition": "NORMALIZED",
                "step_id": str(step_id or ""),
                "added_effect": added_effect,
                "source": str(source or "capability_contract"),
            })
        result[field] = sorted(combined.values(), key=_canonical_json)
    return result, normalizations


def _contract_source(contract: Mapping[str, Any], fallback: str = "") -> str:
    explicit = str(
        contract.get("contract_source")
        or contract.get("source_ref")
        or contract.get("contract_ref")
        or ""
    ).strip()
    if explicit:
        return explicit
    skill_id = str(contract.get("selected_skill_id") or contract.get("skill_id") or "").strip()
    operation = str(contract.get("operation") or contract.get("operation_id") or "").strip()
    capability_id = str(
        contract.get("capability_id")
        or contract.get("execution_capability_id")
        or fallback
        or ""
    ).strip()
    parts = ["skill"]
    if skill_id:
        parts.append(skill_id)
    if operation:
        parts.extend(("operation", operation))
    elif capability_id:
        parts.extend(("capability", capability_id))
    return ".".join(parts)


def _has_effect_contract(value: Mapping[str, Any]) -> bool:
    if any(field in value for field in CANONICAL_STEP_EFFECT_FIELDS):
        return True
    if any(alias in value for aliases in _EFFECT_ALIASES.values() for alias in aliases):
        return True
    return any(isinstance(value.get(wrapper), Mapping) for wrapper in _WRAPPER_FIELDS)


def _flatten_capability_registry(value: Any) -> list[Dict[str, Any]]:
    """Accept the current registry plus common list/nested registry projections."""

    rows: list[Dict[str, Any]] = []

    def visit(
        raw: Any,
        *,
        key_hint: str = "",
        source_hint: str = "registry",
        inherited: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if isinstance(raw, (list, tuple)):
            for index, item in enumerate(raw):
                visit(
                    item,
                    source_hint=f"{source_hint}[{index}]",
                    inherited=inherited,
                )
            return
        if not isinstance(raw, Mapping):
            return
        current = dict(raw)
        inherited_values = {
            key: deepcopy(value)
            for key, value in dict(inherited or {}).items()
            if key in {"selected_skill_id", "skill_id", "capability_id"}
        }
        row = {**inherited_values, **current}
        if key_hint:
            if not row.get("capability_id") and "." in key_hint:
                row["capability_id"] = key_hint
            elif not row.get("operation"):
                row["operation"] = key_hint
        if (
            row.get("capability_id")
            or row.get("execution_capability_id")
            or row.get("operation")
            or _has_effect_contract(row)
        ) and _has_effect_contract(row):
            row["_step_effect_source"] = _contract_source(row, source_hint)
            rows.append(row)
        for container_key in (
            "execution_capabilities",
            "capabilities",
            "operation_contracts",
            "operations",
        ):
            nested = current.get(container_key)
            if isinstance(nested, Mapping):
                for nested_key, item in nested.items():
                    visit(
                        item,
                        key_hint=str(nested_key),
                        source_hint=f"{source_hint}.{container_key}.{nested_key}",
                        inherited=row,
                    )
            elif isinstance(nested, (list, tuple)):
                visit(
                    nested,
                    source_hint=f"{source_hint}.{container_key}",
                    inherited=row,
                )

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {
                "execution_capabilities",
                "capabilities",
                "operation_contracts",
                "operations",
            }:
                visit(item, source_hint=f"registry.{key}")
            else:
                visit(item, key_hint=str(key), source_hint=f"registry.{key}")
    else:
        visit(value)
    unique: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        public = {key: value for key, value in row.items() if not key.startswith("_")}
        key = _digest(public)
        unique.setdefault(key, row)
    return [unique[key] for key in sorted(unique)]


def _candidate_contracts(
    step: Mapping[str, Any],
    capability_registry: Any,
) -> list[Dict[str, Any]]:
    rows = _flatten_capability_registry(capability_registry)
    capability_id = str(
        step.get("execution_capability_id") or step.get("capability_id") or ""
    ).strip()
    operation = str(step.get("operation") or step.get("operation_id") or "").strip()
    action = str(step.get("action") or "").strip().lower()
    skill_ids = {
        str(item).strip()
        for item in step.get("skill_ids", []) or []
        if str(item).strip()
    }
    if not capability_id and not operation and action in {
        "search",
        *_BUILTIN_ACTION_SIDE_EFFECTS,
    }:
        # These are closed Planner action protocols, not Skill capabilities.
        return []
    if capability_id:
        rows = [
            row
            for row in rows
            if str(
                row.get("capability_id")
                or row.get("execution_capability_id")
                or ""
            ).strip()
            == capability_id
        ]
    elif operation:
        rows = [
            row
            for row in rows
            if str(row.get("operation") or row.get("operation_id") or "").strip()
            == operation
        ]
    if skill_ids:
        scoped = [
            row
            for row in rows
            if not str(row.get("selected_skill_id") or row.get("skill_id") or "").strip()
            or str(row.get("selected_skill_id") or row.get("skill_id") or "").strip()
            in skill_ids
        ]
        if scoped:
            rows = scoped
    return rows


def _builtin_action_effect(step: Mapping[str, Any]) -> tuple[Dict[str, Any], str] | None:
    """Return the closed deterministic effect of a non-capability Plan action."""

    if str(step.get("execution_capability_id") or step.get("capability_id") or "").strip():
        return None
    action = str(step.get("action") or "").strip().lower()
    effect = canonical_step_effect({})
    if action == "search":
        step_id = str(step.get("step_id") or step.get("id") or "").strip()
        if not step_id:
            return None
        effect["produces_observations"] = [f"{step_id}.search_result"]
    elif action in _BUILTIN_ACTION_SIDE_EFFECTS:
        effect["explicit_side_effects"] = [_BUILTIN_ACTION_SIDE_EFFECTS[action]]
    else:
        return None
    return effect, f"planner.action_protocol.{action}"


def _normalization_receipt(
    code: str,
    *,
    step_id: str,
    source: str,
    **change: Any,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "code": str(code),
        "disposition": "NORMALIZED",
        "step_id": str(step_id or ""),
        **_route_free(change),
        "source": str(source or "capability_contract"),
    }


def _canonical_normalization_history(value: Any) -> list[Dict[str, Any]]:
    """Validate and stably deduplicate compiler-owned normalization audit rows."""

    if not isinstance(value, (list, tuple)):
        return []
    accepted_codes = {
        "NORMALIZE_STEP_EFFECT",
        "NORMALIZE_EXECUTION_CAPABILITY",
        "NORMALIZE_STEP_OPERATION",
    }
    unique: Dict[str, Dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        row = _route_free(_json_value(raw, field_path="$.step_effect_normalizations[]"))
        code = str(row.get("code") or "")
        step_id = str(row.get("step_id") or "")
        source = str(row.get("source") or "")
        if code not in accepted_codes or not step_id or not source:
            continue
        row["schema_version"] = 1
        row["disposition"] = "NORMALIZED"
        key = _digest({
            "code": code,
            "step_id": step_id,
            "source": source,
            "added_effect": row.get("added_effect"),
            "execution_capability_id": row.get("execution_capability_id"),
            "operation": row.get("operation"),
        })
        unique.setdefault(key, row)
    return [unique[key] for key in sorted(unique)]


def _merge_normalization_history(*groups: Any) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for group in groups:
        if isinstance(group, (list, tuple)):
            rows.extend(deepcopy(list(group)))
    return _canonical_normalization_history(rows)


def _contract_effect_groups(
    contracts: Sequence[Mapping[str, Any]],
    *,
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[tuple[Dict[str, Any], list[Dict[str, Any]]]]:
    groups: Dict[str, tuple[Dict[str, Any], list[Dict[str, Any]]]] = {}
    for raw in contracts:
        contract = dict(raw)
        if not _has_effect_contract(contract):
            continue
        effect = _canonicalize_workspace_effects(
            canonical_step_effect(contract), resource_bindings
        )
        key = _digest(effect)
        if key not in groups:
            groups[key] = (effect, [])
        groups[key][1].append(contract)
    return [groups[key] for key in sorted(groups)]


def _unsupported_explicit_effects(
    explicit: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, list[str]]:
    """Return identities outside an explicitly closed capability effect field."""

    unsupported: Dict[str, list[str]] = {}
    presence = _field_presence(contract)
    explicit_effect = _canonicalize_workspace_effects(explicit, resource_bindings)
    contract_effect = _canonicalize_workspace_effects(
        canonical_step_effect(contract), resource_bindings
    )
    for field in CANONICAL_STEP_EFFECT_FIELDS:
        if field == "produces_artifacts" or not presence[field]:
            # Artifact outputs use Plan-local identities, while a capability may
            # name semantic artifact binding IDs.  The artifact flow validator
            # owns that mapping and immutable-producer safety.
            continue
        explicit_ids = set(_entry_map(explicit_effect.get(field) or [], field=field))
        contract_ids = set(_entry_map(contract_effect.get(field) or [], field=field))
        difference = sorted(explicit_ids - contract_ids)
        if difference:
            unsupported[field] = difference
    return unsupported


def _legacy_plan(value: Mapping[str, Any]) -> bool:
    metadata = dict(value.get("metadata") or {})
    compatibility = dict(value.get("compatibility") or {})
    contract_name = str(metadata.get("planner_contract") or "").strip().lower()
    if contract_name.endswith("_v1"):
        return True
    try:
        if int(metadata.get("schema_version") or 0) == 1:
            return True
    except (TypeError, ValueError):
        pass
    if compatibility.get("legacy_step_effects") is True:
        return True
    if str(compatibility.get("step_effect_mode") or "").lower() in {
        "legacy",
        "legacy_untyped",
    }:
        return True
    return False


def _semantic_step_projection(step: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        str(key): _json_value(value, field_path=f"$.{key}")
        for key, value in sorted(step.items())
        if key not in _VOLATILE_STEP_KEYS and not str(key).endswith("_receipt")
    }


def _plan_projection(plan_state: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "steps": [
            _semantic_step_projection(step)
            for step in plan_state.get("steps", []) or []
            if isinstance(step, Mapping)
        ]
    }
    for field in _PLAN_PROJECTION_FIELDS:
        if field in plan_state:
            result[field] = _json_value(plan_state.get(field), field_path=f"$.{field}")
    return result


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _plan_identity(plan_state: Mapping[str, Any]) -> Dict[str, Any]:
    current = dict(plan_state.get("current_plan") or {})
    return {
        "task_id": str(plan_state.get("task_id") or "default"),
        "plan_id": str(plan_state.get("plan_id") or current.get("plan_id") or ""),
        "plan_version": str(
            plan_state.get("plan_version") or current.get("plan_version") or ""
        ),
        "plan_revision": _safe_int(
            plan_state.get("plan_revision") or current.get("plan_revision")
        ),
        "declared_plan_semantic_hash": str(
            plan_state.get("semantic_plan_hash")
            or plan_state.get("plan_semantic_hash")
            or current.get("plan_semantic_hash")
            or ""
        ),
    }


def _seal_receipt(value: Mapping[str, Any], *, prefix: str) -> Dict[str, Any]:
    body = deepcopy(dict(value))
    body.pop("receipt_id", None)
    body.pop("receipt_digest", None)
    digest = _digest(body)
    return {
        **body,
        "receipt_digest": digest,
        "receipt_id": f"{prefix}:" + digest.removeprefix("sha256:")[:24],
    }


def _receipt_seal_valid(value: Mapping[str, Any], *, prefix: str) -> bool:
    received = dict(value or {})
    expected = _seal_receipt(received, prefix=prefix)
    return bool(
        str(received.get("receipt_id") or "") == expected["receipt_id"]
        and str(received.get("receipt_digest") or "") == expected["receipt_digest"]
    )


def compile_step_effect(
    step: Mapping[str, Any],
    capability_registry: Any,
    *,
    plan_identity: Optional[Mapping[str, Any]] = None,
    step_index: int = 0,
    allow_legacy_untyped: bool = False,
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compile one Plan step into a canonical StepEffect.

    The returned mapping has ``step``, ``effect``, ``receipt``, ``issues``, and
    ``complete`` keys.  It does not mutate the caller's object.
    """

    if not isinstance(step, Mapping):
        issue = protocol_invalid_issue(
            field_path=f"$.steps[{step_index}]",
            message="Plan step must be an object before StepEffect compilation.",
            violation_code="PLAN_STEP_OBJECT_INVALID",
            actual_type=type(step).__name__,
        )
        return {
            "step": deepcopy(step),
            "effect": canonical_step_effect({}),
            "receipt": {},
            "issues": [issue],
            "complete": False,
        }

    source_step = deepcopy(dict(step))
    compiled_step = deepcopy(source_step)
    step_id = str(source_step.get("step_id") or source_step.get("id") or "")
    field_root = f"$.steps[{step_index}]"
    issues: list[Dict[str, Any]] = []
    normalizations: list[Dict[str, Any]] = []
    try:
        explicit_effect = _canonicalize_workspace_effects(
            canonical_step_effect(source_step), resource_bindings
        )
    except StepEffectProtocolError as exc:
        issues.append(_step_protocol_issue(
            exc,
            step_id=step_id,
            field_root=field_root,
        ))
        explicit_effect = canonical_step_effect({})
    try:
        output_artifacts = _canonical_entries(
            source_step.get("outputs") or [],
            field="produces_artifacts",
            field_path=f"{field_root}.outputs",
        )
        output_ids = set(_entry_map(output_artifacts, field="produces_artifacts"))
        declared_artifact_ids = set(_entry_map(
            explicit_effect["produces_artifacts"],
            field="produces_artifacts",
        ))
        unbound_artifact_effects = sorted(declared_artifact_ids - output_ids)
        if unbound_artifact_effects:
            issues.append(_issue(
                "PLAN_STEP_ARTIFACT_EFFECT_UNBOUND",
                disposition="REPAIR_REQUIRED",
                step_id=step_id,
                field_path=f"{field_root}.outputs",
                message=(
                    "Artifact effects must be bound through step.outputs so "
                    "immutable artifact flow remains authoritative."
                ),
                expected="one valid step.outputs handle per produced artifact",
                actual="artifact effect identities exist outside step.outputs",
                unbound_artifact_effects=unbound_artifact_effects,
            ))
        # ``outputs`` is the sole concrete produced-artifact authority.
        explicit_effect["produces_artifacts"] = output_artifacts
    except StepEffectProtocolError as exc:
        issues.append(_step_protocol_issue(
            exc,
            step_id=step_id,
            field_root=field_root,
        ))
        explicit_effect["produces_artifacts"] = []

    contracts = _candidate_contracts(source_step, capability_registry)
    capability_id = str(
        source_step.get("execution_capability_id")
        or source_step.get("capability_id")
        or ""
    ).strip()
    operation = str(
        source_step.get("operation") or source_step.get("operation_id") or ""
    ).strip()
    if contracts and operation:
        operation_mismatches = [
            str(item.get("operation") or item.get("operation_id") or "").strip()
            for item in contracts
            if str(item.get("operation") or item.get("operation_id") or "").strip()
            and str(item.get("operation") or item.get("operation_id") or "").strip()
            != operation
        ]
        if operation_mismatches:
            issues.append(protocol_invalid_issue(
                step_id=step_id,
                field_path=f"{field_root}.operation",
                message="Step operation does not match its selected capability contract.",
                violation_code="STEP_EFFECT_OPERATION_CAPABILITY_MISMATCH",
                expected=sorted(set(operation_mismatches)),
                actual=operation,
                capability_id=capability_id,
            ))
            contracts = []

    groups: list[tuple[Dict[str, Any], list[Dict[str, Any]]]] = []
    try:
        groups = _contract_effect_groups(
            contracts, resource_bindings=resource_bindings
        )
    except StepEffectProtocolError as exc:
        issues.append(_step_protocol_issue(
            exc,
            step_id=step_id,
            field_root=field_root,
        ))

    selected_effect: Optional[Dict[str, Any]] = None
    selected_contracts: list[Dict[str, Any]] = []
    if len(groups) == 1:
        selected_effect, selected_contracts = groups[0]
    elif len(groups) > 1:
        sources = sorted({
            str(item.get("_step_effect_source") or _contract_source(item))
            for _, group_contracts in groups
            for item in group_contracts
        })
        issues.append(unspecified_effect_issue(
            step_id=step_id,
            field_path=f"{field_root}.step_effect",
            candidate_sources=sources,
            message=(
                "More than one capability-owned StepEffect interpretation "
                "matches the step; Planner clarification is required."
            ),
        ))

    if selected_effect is not None and selected_contracts:
        source_refs = sorted({
            str(item.get("_step_effect_source") or _contract_source(item))
            for item in selected_contracts
        })
        source_ref = source_refs[0] if len(source_refs) == 1 else " + ".join(source_refs)
        if len(selected_contracts) == 1:
            selected_contract = selected_contracts[0]
            selected_capability_id = str(
                selected_contract.get("capability_id")
                or selected_contract.get("execution_capability_id")
                or ""
            ).strip()
            selected_operation = str(
                selected_contract.get("operation")
                or selected_contract.get("operation_id")
                or ""
            ).strip()
            if not capability_id and selected_capability_id:
                capability_id = selected_capability_id
                compiled_step["execution_capability_id"] = capability_id
                normalizations.append(_normalization_receipt(
                    "NORMALIZE_EXECUTION_CAPABILITY",
                    step_id=step_id,
                    source=source_ref,
                    execution_capability_id=capability_id,
                ))
            if not operation and selected_operation:
                operation = selected_operation
                compiled_step["operation"] = operation
                normalizations.append(_normalization_receipt(
                    "NORMALIZE_STEP_OPERATION",
                    step_id=step_id,
                    source=source_ref,
                    operation=operation,
                ))
        unsupported: Dict[str, list[str]] = {}
        # Semantically identical contracts have the same canonical effect; use
        # their first public contract for presence/closed-field checks.
        selected_contract = selected_contracts[0]
        try:
            unsupported = _unsupported_explicit_effects(
                explicit_effect,
                selected_contract,
                resource_bindings=resource_bindings,
            )
        except StepEffectProtocolError as exc:
            issues.append(_step_protocol_issue(
                exc,
                step_id=step_id,
                field_root=field_root,
            ))
        if unsupported:
            issues.append(protocol_invalid_issue(
                step_id=step_id,
                field_path=f"{field_root}.step_effect",
                message=(
                    "Planner-declared effects are outside the selected "
                    "capability's closed effect contract."
                ),
                violation_code="STEP_EFFECT_CAPABILITY_MISMATCH",
                unsupported_effects=unsupported,
                capability_id=capability_id,
                actual={field: deepcopy(explicit_effect[field]) for field in unsupported},
                expected={field: deepcopy(selected_effect[field]) for field in unsupported},
                contract_source=source_ref,
                contract_revision=str(selected_contract.get("contract_revision") or ""),
            ))
        try:
            inferred_effect = deepcopy(selected_effect)
            capability_artifacts = list(
                inferred_effect.get("produces_artifacts") or []
            )
            # Plan outputs always own concrete produced-artifact identities.
            # A capability artifact declaration proves the required effect
            # kind, but cannot invent or bypass the Plan artifact-flow handle.
            inferred_effect["produces_artifacts"] = []
            if capability_artifacts and not explicit_effect["produces_artifacts"]:
                issues.append(_issue(
                    "PLAN_STEP_ARTIFACT_EFFECT_UNBOUND",
                    disposition="REPAIR_REQUIRED",
                    step_id=step_id,
                    field_path=f"{field_root}.outputs",
                    message=(
                        "The selected capability produces an artifact, but "
                        "the step binds no immutable artifact output handle."
                    ),
                    source=source_ref,
                    expected="a valid step.outputs artifact handle",
                    actual="outputs is empty",
                    capability_artifact_effects=capability_artifacts,
                ))
            compiled_effect, effect_normalizations = _merge_effects(
                explicit_effect,
                inferred_effect,
                source=source_ref,
                step_id=step_id,
            )
            normalizations.extend(effect_normalizations)
        except StepEffectProtocolError as exc:
            issues.append(_step_protocol_issue(
                exc,
                step_id=step_id,
                field_root=field_root,
            ))
            compiled_effect = deepcopy(explicit_effect)
    else:
        compiled_effect = deepcopy(explicit_effect)
        action_inference = _builtin_action_effect(source_step)
        if action_inference is not None:
            action_effect, action_source = action_inference
            try:
                compiled_effect, action_normalizations = _merge_effects(
                    explicit_effect,
                    action_effect,
                    source=action_source,
                    step_id=step_id,
                )
                normalizations.extend(action_normalizations)
            except StepEffectProtocolError as exc:
                issues.append(_step_protocol_issue(
                    exc,
                    step_id=step_id,
                    field_root=field_root,
                ))

    if explicit_effect.get("explicit_side_effects") and not selected_contracts:
        builtin_side_effect_ids = set()
        if action_inference is not None:
            builtin_side_effect_ids = set(_entry_map(
                action_inference[0].get("explicit_side_effects") or [],
                field="explicit_side_effects",
            ))
        unsupported_side_effects = [
            _identity_text(item, field="explicit_side_effects")
            for item in explicit_effect["explicit_side_effects"]
            if _identity_text(item, field="explicit_side_effects")
            not in builtin_side_effect_ids
            and not (
                isinstance(item, Mapping)
                and item.get("evidence_contract") not in (None, "", [], {})
            )
        ]
        if unsupported_side_effects:
            issues.append(protocol_invalid_issue(
                step_id=step_id,
                field_path=f"{field_root}.explicit_side_effects",
                message=(
                    "Explicit side effects require capability support or an "
                    "inline typed evidence contract."
                ),
                violation_code="EXPLICIT_SIDE_EFFECT_UNSUPPORTED",
                unsupported_effect_ids=unsupported_side_effects,
                capability_id=capability_id,
                actual=deepcopy(explicit_effect["explicit_side_effects"]),
                expected={
                    "supported_forms": [
                        "selected capability declares this side-effect identity",
                        "inline side-effect object with a typed evidence_contract",
                    ],
                },
                contract_source="agent_core.step_effects.compile_step_effect",
                contract_revision=f"step-effect-schema-{STEP_EFFECT_SCHEMA_VERSION}",
            ))

    complete = step_effect_complete(compiled_effect)
    if not complete and not allow_legacy_untyped and not any(
        issue.get("code") in {
            "PLAN_STEP_EFFECT_UNSPECIFIED",
            "PLAN_STEP_ARTIFACT_EFFECT_UNBOUND",
        }
        for issue in issues
    ):
        sources = sorted({
            str(item.get("_step_effect_source") or _contract_source(item))
            for item in contracts
        })
        issues.append(unspecified_effect_issue(
            step_id=step_id,
            field_path=f"{field_root}.step_effect",
            candidate_sources=sources,
        ))

    compiled_step["step_effect"] = deepcopy(compiled_effect)
    # Preserve the current Planner wire shape.  Artifacts remain in ``outputs``;
    # the other four declarations are safe additive fields.
    for field in CANONICAL_STEP_EFFECT_FIELDS:
        if field != "produces_artifacts":
            compiled_step[field] = deepcopy(compiled_effect[field])

    protocol_invalid = any(
        str(item.get("disposition") or "") == "PROTOCOL_INVALID" for item in issues
    )
    repair_required = any(
        str(item.get("disposition") or "") == "REPAIR_REQUIRED" for item in issues
    )
    status = (
        "protocol_invalid"
        if protocol_invalid
        else "repair_required"
        if repair_required
        else "legacy_compatible"
        if allow_legacy_untyped and not complete
        else "compiled"
    )
    selected_public = [
        {key: deepcopy(value) for key, value in item.items() if not key.startswith("_")}
        for item in selected_contracts
    ]
    receipt_body = {
        "schema_version": STEP_EFFECT_RECEIPT_SCHEMA_VERSION,
        "compiler_id": STEP_EFFECT_COMPILER_ID,
        "compiler_version": STEP_EFFECT_COMPILER_VERSION,
        "scope": "step",
        "status": status,
        "identity": {
            **deepcopy(dict(plan_identity or {})),
            "step_id": step_id,
            "step_index": int(step_index),
            "execution_capability_id": capability_id,
            "operation": operation,
        },
        "source_step_digest": _digest(_semantic_step_projection(source_step)),
        "compiled_step_digest": _digest(_semantic_step_projection(compiled_step)),
        "step_effect_digest": _digest(compiled_effect),
        "capability_contract_digest": (
            _digest(selected_public) if selected_public else ""
        ),
        "effect_complete": bool(complete),
        "legacy_compatible": bool(allow_legacy_untyped and not complete),
        "normalizations": deepcopy(normalizations),
        "issue_dispositions": sorted({
            str(item.get("disposition") or "") for item in issues
            if str(item.get("disposition") or "")
        }),
    }
    receipt = _seal_receipt(receipt_body, prefix="step-effect")
    return {
        "step": compiled_step,
        "effect": compiled_effect,
        "receipt": receipt,
        "issues": deepcopy(issues),
        "complete": bool(complete),
        "normalizations": deepcopy(normalizations),
    }


def compile_plan_step_effects(
    plan_state: Mapping[str, Any],
    capability_registry: Any,
    *,
    allow_legacy_untyped: Optional[bool] = None,
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any], list[Dict[str, Any]]]:
    """Compile every Plan step and return ``(state, receipt, issues)``.

    The function is pure and deterministic.  It neither persists the receipt
    nor chooses what workflow node consumes a repairable issue.
    """

    if not isinstance(plan_state, Mapping):
        issue = protocol_invalid_issue(
            field_path="$",
            message="Plan must be an object before StepEffect compilation.",
            violation_code="PLAN_OBJECT_INVALID",
            actual_type=type(plan_state).__name__,
        )
        return {}, {}, [issue]
    source_plan = deepcopy(dict(plan_state))
    compiled_plan = deepcopy(source_plan)
    raw_steps = source_plan.get("steps")
    if not isinstance(raw_steps, list):
        issue = protocol_invalid_issue(
            field_path="$.steps",
            message="Plan steps must be an array before StepEffect compilation.",
            violation_code="PLAN_STEPS_INVALID",
            actual_type=type(raw_steps).__name__,
        )
        return compiled_plan, {}, [issue]
    legacy = (
        _legacy_plan(source_plan)
        if allow_legacy_untyped is None
        else bool(allow_legacy_untyped)
    )
    identity = _plan_identity(source_plan)
    compiled_steps: list[Any] = []
    step_receipts: list[Dict[str, Any]] = []
    issues: list[Dict[str, Any]] = []
    normalizations: list[Dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            issue = protocol_invalid_issue(
                field_path=f"$.steps[{index}]",
                message="Plan step must be an object.",
                violation_code="PLAN_STEP_OBJECT_INVALID",
                actual_type=type(raw_step).__name__,
            )
            compiled_steps.append(deepcopy(raw_step))
            issues.append(issue)
            continue
        result = compile_step_effect(
            raw_step,
            capability_registry,
            plan_identity=identity,
            step_index=index,
            allow_legacy_untyped=legacy,
            resource_bindings=resource_bindings,
        )
        compiled_steps.append(result["step"])
        step_receipts.append(result["receipt"])
        issues.extend(result["issues"])
        normalizations.extend(result["normalizations"])
    compiled_plan["steps"] = compiled_steps
    normalizations = _merge_normalization_history(
        source_plan.get("step_effect_normalizations"),
        normalizations,
    )
    # This append-only semantic audit survives subsequent PlanRevision
    # recompilation even after the inferred fields have become explicit.
    compiled_plan["step_effect_normalizations"] = deepcopy(normalizations)

    protocol_invalid = any(
        str(item.get("disposition") or "") == "PROTOCOL_INVALID" for item in issues
    )
    repair_required = any(
        str(item.get("disposition") or "") == "REPAIR_REQUIRED" for item in issues
    )
    all_effects_complete = len(step_receipts) == len(raw_steps) and all(
        bool(item.get("effect_complete"))
        for item in step_receipts
    )
    all_review_compatible = (
        not issues
        and len(step_receipts) == len(raw_steps)
        and all(
            bool(item.get("effect_complete")) or bool(item.get("legacy_compatible"))
            for item in step_receipts
        )
    )
    status = (
        "protocol_invalid"
        if protocol_invalid
        else "repair_required"
        if repair_required
        else "legacy_compatible"
        if legacy and any(item.get("legacy_compatible") for item in step_receipts)
        else "compiled"
    )
    registry_rows = [
        {key: deepcopy(value) for key, value in row.items() if not key.startswith("_")}
        for row in _flatten_capability_registry(capability_registry)
    ]
    receipt_body = {
        "schema_version": STEP_EFFECT_RECEIPT_SCHEMA_VERSION,
        "compiler_id": STEP_EFFECT_COMPILER_ID,
        "compiler_version": STEP_EFFECT_COMPILER_VERSION,
        "scope": "plan",
        "status": status,
        "identity": identity,
        "source_plan_digest": _digest(_plan_projection(source_plan)),
        "compiled_plan_digest": _digest(_plan_projection(compiled_plan)),
        "capability_registry_digest": _digest(registry_rows),
        "resource_binding_digest": (
            _digest(resource_bindings) if resource_bindings else ""
        ),
        "effect_complete": bool(all_effects_complete),
        "review_compatible": bool(all_review_compatible),
        "legacy_compatible": bool(legacy and all_review_compatible),
        "step_receipts": step_receipts,
        "normalizations": normalizations,
        "issue_count": len(issues),
        "issue_dispositions": sorted({
            str(item.get("disposition") or "") for item in issues
            if str(item.get("disposition") or "")
        }),
        "issues_digest": _digest(issues),
    }
    receipt = _seal_receipt(receipt_body, prefix="plan-step-effects")
    return compiled_plan, receipt, issues


def _dataflow_semantic_projection(plan_state: Mapping[str, Any]) -> Dict[str, Any]:
    """Project only explicit identities/effects that can change dataflow."""

    return {
        "steps": [
            {
                key: _json_value(step.get(key), field_path=f"$.steps[{index}].{key}")
                for key in (
                    "step_id",
                    "inputs",
                    "outputs",
                    "dependencies",
                    "execution_capability_id",
                    "operation",
                    "workspace_mutations",
                    "step_effect",
                )
                if key in step
            }
            for index, step in enumerate(plan_state.get("steps", []) or [])
            if isinstance(step, Mapping)
        ],
        "proposed_artifacts": [
            {
                key: _json_value(item.get(key), field_path=f"$.proposed_artifacts.{key}")
                for key in (
                    "local_ref",
                    "artifact_contract",
                    "resource_binding",
                )
                if key in item
            }
            for item in plan_state.get("proposed_artifacts", []) or []
            if isinstance(item, Mapping)
        ],
    }


def _dataflow_issue(
    code: str,
    *,
    plan_state: Mapping[str, Any],
    resource_ref: str = "",
    resource_binding: Optional[Mapping[str, Any]] = None,
    consumer_step: str = "",
    dependency_ancestors: Optional[Sequence[str]] = None,
    recognized_sources: Optional[Sequence[Mapping[str, Any]]] = None,
    accepted_sources: Optional[Sequence[Mapping[str, Any]]] = None,
    rejected_sources: Optional[Sequence[Mapping[str, Any]]] = None,
    source_effect_kinds: Optional[Sequence[str]] = None,
    field_path: str = "$.steps",
    affected_fields: Optional[Sequence[str]] = None,
    actual_facts: Any = None,
    required_condition: Any = None,
    message: str = "Compiled Plan dataflow is invalid.",
    explicit_contract_source: str = "plan_and_selected_capability_contract",
    contract_revision: str = "",
    **evidence: Any,
) -> Dict[str, Any]:
    """Build one complete, deterministic, route-free dataflow issue."""

    binding = deepcopy(dict(resource_binding or {}))
    candidate_revision = _safe_int(
        plan_state.get("plan_revision")
        or dict(plan_state.get("metadata") or {}).get("candidate_revision")
        or 0
    )
    body: Dict[str, Any] = {
        "schema_version": 1,
        "candidate_revision": candidate_revision,
        "validator_id": PLAN_DATAFLOW_COMPILER_ID,
        "code": str(code),
        "severity": "error",
        "disposition": "PROTOCOL_INVALID",
        "authority": "blocking",
        "blocking": True,
        "explicit_contract_source": str(explicit_contract_source),
        "contract_revision": str(
            contract_revision
            or binding.get("contract_revision")
            or "unknown"
        ),
        "resource_id": str(resource_ref or "unknown"),
        "canonical_resource_id": str(
            binding.get("canonical_resource_id") or "unknown"
        ),
        "binding_evidence": binding or {"status": "unknown"},
        "consumer_step": str(consumer_step or ""),
        "consumer_step_id": str(consumer_step or ""),
        "dependency_ancestors": sorted({
            str(value) for value in dependency_ancestors or [] if str(value)
        }),
        "recognized_candidate_sources": deepcopy(list(recognized_sources or [])),
        "source_effect_kinds": sorted({
            str(value) for value in source_effect_kinds or [] if str(value)
        }),
        "accepted_sources": deepcopy(list(accepted_sources or [])),
        "rejected_sources": deepcopy(list(rejected_sources or [])),
        "affected_fields": [str(value) for value in affected_fields or []],
        "actual_facts": deepcopy(actual_facts),
        "required_condition": deepcopy(required_condition),
        "field_path": str(field_path or "$.steps"),
        "message": str(message),
        "source": "compiled_plan_dataflow",
    }
    body.update({
        str(key): deepcopy(value)
        for key, value in _route_free(evidence).items()
        if value not in (None, "", [], {})
    })
    issue_body = {
        key: value for key, value in body.items() if key != "issue_id"
    }
    body["issue_id"] = "plan-dataflow-issue-" + _digest(
        issue_body
    ).removeprefix("sha256:")[:20]
    return body


def _dependency_analysis(
    plan_state: Mapping[str, Any],
) -> tuple[Dict[str, set[str]], Dict[str, set[str]], list[Dict[str, Any]]]:
    """Return exact dependency and ancestor sets plus structural issues."""

    raw_steps = [
        dict(item)
        for item in plan_state.get("steps", []) or []
        if isinstance(item, Mapping)
    ]
    step_ids = [str(step.get("step_id") or "") for step in raw_steps]
    issues: list[Dict[str, Any]] = []
    if any(not step_id for step_id in step_ids):
        issues.append(_dataflow_issue(
            "PLAN_STEP_ID_MISSING",
            plan_state=plan_state,
            field_path="$.steps",
            affected_fields=["step_id"],
            actual_facts={"step_ids": step_ids},
            required_condition="every step has one non-empty step_id",
            message="Every Plan step must have one non-empty step_id.",
        ))
        return {}, {}, issues
    duplicate_ids = sorted({
        step_id for step_id in step_ids if step_ids.count(step_id) > 1
    })
    if duplicate_ids:
        issues.append(_dataflow_issue(
            "PLAN_STEP_ID_DUPLICATE",
            plan_state=plan_state,
            field_path="$.steps",
            affected_fields=["step_id"],
            actual_facts={"duplicate_step_ids": duplicate_ids},
            required_condition="step identities are unique",
            message="Plan step identities must be unique.",
            affected_step_ids=duplicate_ids,
        ))
        return {}, {}, issues

    known = set(step_ids)
    dependencies: Dict[str, set[str]] = {}
    for index, step in enumerate(raw_steps):
        step_id = step_ids[index]
        declared = {
            str(value) for value in step.get("dependencies", []) or [] if str(value)
        }
        dependencies[step_id] = declared
        for dependency in sorted(declared):
            if dependency not in known:
                issues.append(_dataflow_issue(
                    "PLAN_DEPENDENCY_UNKNOWN",
                    plan_state=plan_state,
                    consumer_step=step_id,
                    field_path=f"$.steps[{index}].dependencies",
                    affected_fields=["dependencies"],
                    actual_facts={"dependency_step_id": dependency},
                    required_condition="every dependency names an existing step",
                    message="Plan dependency does not name an existing step.",
                    dependency_step_id=dependency,
                    affected_step_ids=[step_id],
                ))
            elif dependency == step_id:
                issues.append(_dataflow_issue(
                    "PLAN_DEPENDENCY_SELF",
                    plan_state=plan_state,
                    consumer_step=step_id,
                    field_path=f"$.steps[{index}].dependencies",
                    affected_fields=["dependencies"],
                    actual_facts={"dependency_step_id": dependency},
                    required_condition="a step does not depend on itself",
                    message="A Plan step may not depend on itself.",
                    affected_step_ids=[step_id],
                ))

    cycle_keys: set[str] = set()
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            start = visiting.index(step_id)
            cycle = [*visiting[start:], step_id]
            key = _canonical_json(cycle)
            if key not in cycle_keys:
                cycle_keys.add(key)
                issues.append(_dataflow_issue(
                    "PLAN_DEPENDENCY_CYCLE",
                    plan_state=plan_state,
                    consumer_step=step_id,
                    field_path="$.steps",
                    affected_fields=["dependencies"],
                    actual_facts={"cycle": cycle},
                    required_condition="dependency graph is acyclic",
                    message="Plan dependencies contain a cycle.",
                    affected_step_ids=cycle,
                ))
            return
        if step_id in visited:
            return
        visiting.append(step_id)
        for dependency in sorted(dependencies.get(step_id, set())):
            if dependency in known:
                visit(dependency)
        visiting.pop()
        visited.add(step_id)

    for step_id in step_ids:
        visit(step_id)

    ancestors: Dict[str, set[str]] = {}
    for step_id in step_ids:
        reached: set[str] = set()
        pending = list(dependencies.get(step_id, set()))
        while pending:
            dependency = pending.pop()
            if dependency in reached or dependency not in known:
                continue
            reached.add(dependency)
            pending.extend(dependencies.get(dependency, set()))
        ancestors[step_id] = reached
    return dependencies, ancestors, issues


def _binding_for_ref(
    resource_ref: str,
    resource_bindings: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Resolve exactly one registered alias; no basename/suffix fallback."""

    return deepcopy(dict(resource_bindings.get(str(resource_ref or "")) or {}))


def _mutation_transition(entry: Any) -> Dict[str, Any]:
    row = deepcopy(dict(entry)) if isinstance(entry, Mapping) else {}
    requires_state = str(
        row.get("requires_state") or row.get("required_state") or ""
    ).strip()
    establishes_state = str(
        row.get("establishes_state") or row.get("state_after_success") or ""
    ).strip()
    requires_marker = next(
        (
            row.get(key)
            for key in ("requires_available", "requires_availability")
            if key in row
        ),
        None,
    )
    after_marker = next(
        (
            row.get(key)
            for key in (
                "available_after_success",
                "establishes_availability",
            )
            if key in row
        ),
        None,
    )
    requires_explicit = isinstance(requires_marker, bool) or bool(requires_state)
    after_explicit = isinstance(after_marker, bool) or bool(establishes_state)
    return {
        "requires_available": (
            bool(requires_marker) if isinstance(requires_marker, bool)
            else bool(requires_state)
        ),
        "requires_state": requires_state,
        "available_after_success": (
            bool(after_marker) if isinstance(after_marker, bool)
            else bool(establishes_state)
        ),
        "establishes_state": establishes_state,
        "transition_explicit": bool(requires_explicit and after_explicit),
        "mutation": str(row.get("mutation") or row.get("operation") or ""),
        "contract_source": str(
            row.get("contract_source")
            or row.get("source_ref")
            or "selected_capability.workspace_mutations"
        ),
        "contract_revision": str(row.get("contract_revision") or ""),
    }


def _source_view(edge: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: deepcopy(edge.get(key))
        for key in (
            "step_id",
            "effect_kind",
            "canonical_resource_id",
            "establishes_state",
            "field_path",
        )
        if edge.get(key) not in (None, "", [], {})
    }


def _source_analysis(
    *,
    consumer_step: str,
    binding: Mapping[str, Any],
    ancestors: Mapping[str, set[str]],
    source_edges: Sequence[Mapping[str, Any]],
    required_state: str = "",
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Classify explicit sources against dependency reachability."""

    recognized: list[Dict[str, Any]] = []
    accepted: list[Dict[str, Any]] = []
    rejected: list[Dict[str, Any]] = []
    if binding.get("initially_available") is True:
        initial = {
            "effect_kind": "INITIAL",
            "canonical_resource_id": str(binding.get("canonical_resource_id") or ""),
            "availability_origin": str(binding.get("availability_origin") or ""),
            "establishes_state": str(binding.get("initial_state") or ""),
        }
        recognized.append(deepcopy(initial))
        if required_state and str(initial.get("establishes_state") or "") != required_state:
            rejected.append({
                **initial,
                "reason": "initial_state_does_not_satisfy_required_state",
                "required_state": required_state,
            })
        else:
            accepted.append(initial)

    reachable_edges: list[Mapping[str, Any]] = []
    for edge in source_edges:
        view = _source_view(edge)
        recognized.append(deepcopy(view))
        producer = str(edge.get("step_id") or "")
        if producer == consumer_step:
            rejected.append({
                **view,
                "reason": "same_step_effect_occurs_after_read_precondition",
            })
        elif producer not in ancestors.get(consumer_step, set()):
            rejected.append({**view, "reason": "source_step_is_not_an_ancestor"})
        else:
            reachable_edges.append(edge)

    # For an ordered mutable chain, only maximal reachable source steps are
    # current heads.  Incomparable branches remain visible; absence of an
    # explicit conflict/locking contract is not converted into a proven error.
    maximal: list[Mapping[str, Any]] = []
    for edge in reachable_edges:
        producer = str(edge.get("step_id") or "")
        if any(
            producer in ancestors.get(str(other.get("step_id") or ""), set())
            for other in reachable_edges
            if other is not edge
        ):
            continue
        maximal.append(edge)
    for edge in maximal:
        view = _source_view(edge)
        state = str(edge.get("establishes_state") or "")
        if required_state and state != required_state:
            rejected.append({
                **view,
                "reason": "source_state_does_not_satisfy_required_state",
                "required_state": required_state,
            })
        else:
            accepted.append(view)
    return recognized, accepted, rejected


def compile_plan_dataflow(
    plan_state: Mapping[str, Any],
    capability_registry: Any = None,
    *,
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
    candidate_scope_id: str = "",
    authoritative_handles: Optional[Mapping[str, str]] = None,
    availability_origins: Optional[Mapping[str, str]] = None,
    authoritative_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
    compile_effect_contracts: bool = True,
    validate_dependencies: bool = True,
    allow_legacy_untyped: bool = False,
) -> tuple[Dict[str, Any], Dict[str, Any], list[Dict[str, Any]]]:
    """Compile the sole schema-2 READ/PRODUCE/MUTATE availability view.

    Resource aliases are resolved only through ``resource_bindings`` (or the
    exact binding compiler inputs).  READ preconditions are evaluated before a
    same-step MUTATE, mutable updates use dependency reachability rather than
    immutable producer cardinality, and a mutation without explicit before/
    after availability semantics yields a contract issue instead of pretending
    that the resource does not exist.
    """

    if not isinstance(plan_state, Mapping):
        issue = _dataflow_issue(
            "PLAN_OBJECT_INVALID",
            plan_state={},
            field_path="$",
            actual_facts={"type": type(plan_state).__name__},
            required_condition="Plan is an object",
            message="Plan must be an object before dataflow compilation.",
        )
        return {}, {}, [issue]

    source_plan = deepcopy(dict(plan_state))
    binding_issues: list[Dict[str, Any]] = []
    compiled_bindings: Dict[str, Dict[str, Any]]
    if resource_bindings is None:
        try:
            from agent_core.plan_bindings import (
                PlanBindingError,
                compile_plan_resource_bindings,
            )

            compiled_bindings = compile_plan_resource_bindings(
                source_plan,
                candidate_scope_id=str(candidate_scope_id or "candidate-scope"),
                authoritative_handles=dict(authoritative_handles or {}),
                availability_origins=dict(availability_origins or {}),
                authoritative_bindings=dict(authoritative_bindings or {}),
            )
        except PlanBindingError as exc:
            diagnostic = (
                exc.to_dict() if hasattr(exc, "to_dict") else {"detail": str(exc)}
            )
            binding_issues.append(_dataflow_issue(
                str(diagnostic.get("code") or "PLAN_RESOURCE_BINDING_INVALID"),
                plan_state=source_plan,
                field_path=str(diagnostic.get("field_path") or "$.proposed_artifacts"),
                actual_facts=diagnostic,
                required_condition="all resource aliases have one explicit canonical identity",
                message="Plan resource binding compilation failed.",
                explicit_contract_source="plan.resource_binding",
            ))
            compiled_bindings = {}
    else:
        compiled_bindings = {
            str(alias): deepcopy(dict(row))
            for alias, row in resource_bindings.items()
            if isinstance(row, Mapping)
        }

    effect_issues: list[Dict[str, Any]] = []
    effect_receipt: Dict[str, Any] = {}
    if compile_effect_contracts:
        compiled_plan, effect_receipt, effect_issues = compile_plan_step_effects(
            source_plan,
            capability_registry or {},
            allow_legacy_untyped=allow_legacy_untyped,
            resource_bindings=compiled_bindings,
        )
    else:
        compiled_plan = deepcopy(source_plan)
        for index, raw_step in enumerate(compiled_plan.get("steps", []) or []):
            if not isinstance(raw_step, dict):
                continue
            try:
                raw_step["step_effect"] = _canonicalize_workspace_effects(
                    canonical_step_effect(raw_step), compiled_bindings
                )
            except StepEffectProtocolError as exc:
                effect_issues.append(_step_protocol_issue(
                    exc,
                    step_id=str(raw_step.get("step_id") or ""),
                    field_root=f"$.steps[{index}]",
                ))

    if validate_dependencies:
        _, ancestors, dependency_issues = _dependency_analysis(compiled_plan)
    else:
        steps = [
            dict(item)
            for item in compiled_plan.get("steps", []) or []
            if isinstance(item, Mapping)
        ]
        dependencies = {
            str(step.get("step_id") or ""): {
                str(value)
                for value in step.get("dependencies", []) or []
                if str(value)
            }
            for step in steps
        }
        ancestors = {}
        for step_id in dependencies:
            reached: set[str] = set()
            pending = list(dependencies.get(step_id, set()))
            while pending:
                dependency = pending.pop()
                if dependency in reached or dependency not in dependencies:
                    continue
                reached.add(dependency)
                pending.extend(dependencies.get(dependency, set()))
            ancestors[step_id] = reached
        dependency_issues = []

    edges: list[Dict[str, Any]] = []
    dataflow_issues: list[Dict[str, Any]] = [*binding_issues, *dependency_issues]
    from .plan_contract import plan_result_contracts, plan_uses_explicit_results
    result_contracts = (
        plan_result_contracts(source_plan)
        if plan_uses_explicit_results(source_plan) else {}
    )
    for step_index, raw_step in enumerate(compiled_plan.get("steps", []) or []):
        if not isinstance(raw_step, Mapping):
            continue
        step = dict(raw_step)
        step_id = str(step.get("step_id") or "")
        for field, effect_kind in (("inputs", "READ"), ("outputs", "PRODUCE")):
            for ref_index, raw_ref in enumerate(step.get(field, []) or []):
                resource_ref = str(raw_ref or "")
                declared_result = result_contracts.get(resource_ref)
                if effect_kind == "READ" and declared_result is not None and not declared_result["required"]:
                    dataflow_issues.append(_dataflow_issue(
                        "PLAN_OPTIONAL_RESULT_REQUIRED_BY_CONSUMER",
                        plan_state=compiled_plan,
                        resource_ref=resource_ref,
                        consumer_step=step_id,
                        field_path=f"$.steps[{step_index}].inputs[{ref_index}]",
                        affected_fields=["inputs", "proposed_artifacts", "task_output_materializations"],
                        actual_facts={"required": False, "consumer_step_id": step_id},
                        required_condition="A hard input requires a necessary producer result or a different explicit dependency.",
                        message="Optional output is consumed as a mandatory input; Planner must correct necessity or the dependency without silent promotion.",
                        affected_step_ids=[step_id],
                    ))
                binding = _binding_for_ref(resource_ref, compiled_bindings)
                field_path = f"$.steps[{step_index}].{field}[{ref_index}]"
                if not binding:
                    dataflow_issues.append(_dataflow_issue(
                        "PLAN_RESOURCE_BINDING_MISSING",
                        plan_state=compiled_plan,
                        resource_ref=resource_ref,
                        consumer_step=step_id,
                        dependency_ancestors=sorted(ancestors.get(step_id, set())),
                        field_path=field_path,
                        affected_fields=[field],
                        actual_facts={"declared_resource_ref": resource_ref},
                        required_condition="an exact registered resource handle or explicit alias binding",
                        message="Plan resource reference has no exact identity binding.",
                        affected_step_ids=[step_id],
                    ))
                    continue
                edge = {
                    "step_id": step_id,
                    "step_index": step_index,
                    "effect_kind": effect_kind,
                    "declared_resource_ref": resource_ref,
                    "canonical_resource_id": str(
                        binding.get("canonical_resource_id") or ""
                    ),
                    "resource_kind": str(binding.get("resource_kind") or ""),
                    "binding_id": str(binding.get("binding_id") or ""),
                    "field_path": field_path,
                    "establishes_state": "available" if effect_kind == "PRODUCE" else "",
                }
                edges.append(edge)
                if (
                    effect_kind == "PRODUCE"
                    and edge["resource_kind"] != "immutable_artifact"
                ):
                    dataflow_issues.append(_dataflow_issue(
                        "PLAN_MUTABLE_RESOURCE_DECLARED_AS_OUTPUT",
                        plan_state=compiled_plan,
                        resource_ref=resource_ref,
                        resource_binding=binding,
                        consumer_step=step_id,
                        dependency_ancestors=sorted(ancestors.get(step_id, set())),
                        recognized_sources=[_source_view(edge)],
                        source_effect_kinds=["PRODUCE"],
                        field_path=field_path,
                        affected_fields=[field, "workspace_mutations"],
                        actual_facts={"resource_kind": edge["resource_kind"]},
                        required_condition="mutable_workspace is changed only by MUTATE",
                        message="Mutable workspace may not be declared as an immutable output.",
                        affected_step_ids=[step_id],
                    ))

        effect = dict(step.get("step_effect") or {})
        for mutation_index, raw_mutation in enumerate(
            effect.get("workspace_mutations", []) or []
        ):
            resource_ref = _identity_text(
                raw_mutation, field="workspace_mutations"
            )
            declared_resource_ref = (
                str(raw_mutation.get("declared_resource_ref") or resource_ref)
                if isinstance(raw_mutation, Mapping)
                else resource_ref
            )
            binding = _binding_for_ref(resource_ref, compiled_bindings)
            field_path = (
                f"$.steps[{step_index}].step_effect.workspace_mutations[{mutation_index}]"
            )
            if not binding:
                dataflow_issues.append(_dataflow_issue(
                    "PLAN_RESOURCE_BINDING_MISSING",
                    plan_state=compiled_plan,
                    resource_ref=resource_ref,
                    consumer_step=step_id,
                    dependency_ancestors=sorted(ancestors.get(step_id, set())),
                    field_path=field_path,
                    affected_fields=["workspace_mutations"],
                    actual_facts={"declared_resource_ref": resource_ref},
                    required_condition="an exact registered mutable workspace binding",
                    message="Workspace mutation has no exact resource binding.",
                    affected_step_ids=[step_id],
                ))
                continue
            transition = _mutation_transition(raw_mutation)
            edge = {
                "step_id": step_id,
                "step_index": step_index,
                "effect_kind": "MUTATE",
                "declared_resource_ref": declared_resource_ref,
                "canonical_resource_id": str(
                    binding.get("canonical_resource_id") or ""
                ),
                "resource_kind": str(binding.get("resource_kind") or ""),
                "binding_id": str(binding.get("binding_id") or ""),
                "field_path": field_path,
                **transition,
            }
            edges.append(edge)
            if edge["resource_kind"] != "mutable_workspace":
                dataflow_issues.append(_dataflow_issue(
                    "PLAN_RESOURCE_KIND_MISMATCH",
                    plan_state=compiled_plan,
                    resource_ref=resource_ref,
                    resource_binding=binding,
                    consumer_step=step_id,
                    dependency_ancestors=sorted(ancestors.get(step_id, set())),
                    recognized_sources=[_source_view(edge)],
                    source_effect_kinds=["MUTATE"],
                    field_path=field_path,
                    affected_fields=["workspace_mutations", "resource_binding"],
                    actual_facts={"resource_kind": edge["resource_kind"]},
                    required_condition="workspace mutation targets resource_kind=mutable_workspace",
                    message="MUTATE may only target an explicitly typed mutable workspace.",
                    affected_step_ids=[step_id],
                ))
            if not transition["transition_explicit"]:
                dataflow_issues.append(_dataflow_issue(
                    "PLAN_MUTATION_TRANSITION_UNSPECIFIED",
                    plan_state=compiled_plan,
                    resource_ref=resource_ref,
                    resource_binding=binding,
                    consumer_step=step_id,
                    dependency_ancestors=sorted(ancestors.get(step_id, set())),
                    recognized_sources=[_source_view(edge)],
                    source_effect_kinds=["MUTATE"],
                    field_path=field_path,
                    affected_fields=["workspace_mutations"],
                    actual_facts={
                        "requires_available": transition["requires_available"],
                        "available_after_success": transition["available_after_success"],
                        "transition_explicit": False,
                    },
                    required_condition=(
                        "explicit requires_available/requires_state and "
                        "available_after_success/establishes_state"
                    ),
                    message=(
                        "Workspace mutation lacks explicit before/after availability semantics."
                    ),
                    explicit_contract_source=transition["contract_source"],
                    contract_revision=transition["contract_revision"],
                    affected_step_ids=[step_id],
                ))

    immutable_producers: Dict[str, list[Dict[str, Any]]] = {}
    sources_by_resource: Dict[str, list[Dict[str, Any]]] = {}
    for edge in edges:
        identity = str(edge.get("canonical_resource_id") or "")
        if (
            edge.get("effect_kind") == "PRODUCE"
            and edge.get("resource_kind") == "immutable_artifact"
        ):
            immutable_producers.setdefault(identity, []).append(edge)
            sources_by_resource.setdefault(identity, []).append(edge)
        elif (
            edge.get("effect_kind") == "MUTATE"
            and edge.get("available_after_success") is True
            and edge.get("resource_kind") == "mutable_workspace"
        ):
            sources_by_resource.setdefault(identity, []).append(edge)

    # An admitted immutable input/resume version already has one authoritative
    # origin.  Declaring any current-Plan PRODUCE for that same identity would
    # be in-place replacement, not creation of a new immutable artifact.
    for identity, producer_edges in sorted(immutable_producers.items()):
        binding = _binding_for_ref(identity, compiled_bindings)
        if binding.get("initially_available") is not True:
            continue
        for producer_edge in producer_edges:
            dataflow_issues.append(_dataflow_issue(
                "PLAN_ARTIFACT_ORIGIN_CONFLICT",
                plan_state=compiled_plan,
                resource_ref=str(
                    producer_edge.get("declared_resource_ref") or identity
                ),
                resource_binding=binding,
                consumer_step=str(producer_edge.get("step_id") or ""),
                dependency_ancestors=sorted(ancestors.get(
                    str(producer_edge.get("step_id") or ""), set()
                )),
                recognized_sources=[
                    {
                        "effect_kind": "INITIAL",
                        "canonical_resource_id": identity,
                        "availability_origin": str(
                            binding.get("availability_origin") or ""
                        ),
                    },
                    _source_view(producer_edge),
                ],
                accepted_sources=[{
                    "effect_kind": "INITIAL",
                    "canonical_resource_id": identity,
                    "availability_origin": str(
                        binding.get("availability_origin") or ""
                    ),
                }],
                rejected_sources=[{
                    **_source_view(producer_edge),
                    "reason": "immutable_resource_already_has_authoritative_origin",
                }],
                source_effect_kinds=["INITIAL", "PRODUCE"],
                field_path=str(producer_edge.get("field_path") or "$.steps"),
                affected_fields=["outputs", "resource_binding"],
                actual_facts={
                    "availability_origin": str(
                        binding.get("availability_origin") or ""
                    ),
                    "initially_available": True,
                    "producer_step_id": str(producer_edge.get("step_id") or ""),
                },
                required_condition=(
                    "an immutable resource has exactly one origin; write a new "
                    "resource identity instead of replacing an admitted input"
                ),
                message=(
                    "An initially available immutable resource cannot also be "
                    "produced by the current Plan."
                ),
                explicit_contract_source="resource_binding.availability_origin",
                affected_step_ids=[str(producer_edge.get("step_id") or "")],
                producer_step_ids=[str(producer_edge.get("step_id") or "")],
            ))

    for identity, producer_edges in sorted(immutable_producers.items()):
        if len(producer_edges) <= 1:
            continue
        binding = _binding_for_ref(identity, compiled_bindings)
        dataflow_issues.append(_dataflow_issue(
            "PLAN_ARTIFACT_PRODUCER_CARDINALITY",
            plan_state=compiled_plan,
            resource_ref=identity,
            resource_binding=binding,
            recognized_sources=[_source_view(edge) for edge in producer_edges],
            source_effect_kinds=["PRODUCE"],
            field_path="$.steps",
            affected_fields=["outputs"],
            actual_facts={
                "producer_step_ids": [edge["step_id"] for edge in producer_edges]
            },
            required_condition="one immutable producer",
            message="Immutable artifact must have exactly one producing step.",
            producer_step_ids=[edge["step_id"] for edge in producer_edges],
            affected_step_ids=[edge["step_id"] for edge in producer_edges],
        ))

    mutable_mutators: Dict[str, list[Dict[str, Any]]] = {}
    for edge in edges:
        if (
            edge.get("effect_kind") == "MUTATE"
            and edge.get("resource_kind") == "mutable_workspace"
        ):
            mutable_mutators.setdefault(
                str(edge.get("canonical_resource_id") or ""), []
            ).append(edge)
    for identity, mutation_edges in sorted(mutable_mutators.items()):
        ordered_edges = sorted(mutation_edges, key=lambda item: (
            int(item.get("step_index") or 0),
            str(item.get("step_id") or ""),
            str(item.get("field_path") or ""),
        ))
        for left_index, left in enumerate(ordered_edges):
            left_step = str(left.get("step_id") or "")
            for right in ordered_edges[left_index + 1:]:
                right_step = str(right.get("step_id") or "")
                if not left_step or not right_step or left_step == right_step:
                    continue
                if (
                    left_step in ancestors.get(right_step, set())
                    or right_step in ancestors.get(left_step, set())
                ):
                    continue
                binding = _binding_for_ref(identity, compiled_bindings)
                sources = [_source_view(left), _source_view(right)]
                dataflow_issues.append(_dataflow_issue(
                    "PLAN_MUTATION_ORDER_AMBIGUOUS",
                    plan_state=compiled_plan,
                    resource_ref=identity,
                    resource_binding=binding,
                    recognized_sources=sources,
                    rejected_sources=[
                        {
                            **source,
                            "reason": "mutation_steps_are_not_dependency_ordered",
                        }
                        for source in sources
                    ],
                    source_effect_kinds=["MUTATE"],
                    field_path="$.steps",
                    affected_fields=["workspace_mutations", "dependencies"],
                    actual_facts={
                        "unordered_mutator_step_ids": [left_step, right_step],
                    },
                    required_condition=(
                        "all steps mutating one mutable workspace are ordered "
                        "by explicit dependency reachability"
                    ),
                    message=(
                        "Mutable workspace mutations must have an explicit "
                        "dependency order."
                    ),
                    affected_step_ids=[left_step, right_step],
                ))

    for edge in edges:
        if edge.get("effect_kind") not in {"READ", "MUTATE"}:
            continue
        if edge.get("effect_kind") == "MUTATE" and not edge.get("requires_available"):
            continue
        identity = str(edge.get("canonical_resource_id") or "")
        binding = _binding_for_ref(identity, compiled_bindings)
        required_state = str(edge.get("requires_state") or "")
        recognized, accepted, rejected = _source_analysis(
            consumer_step=str(edge.get("step_id") or ""),
            binding=binding,
            ancestors=ancestors,
            source_edges=sources_by_resource.get(identity, []),
            required_state=required_state,
        )
        if accepted:
            continue

        same_step_sources = [
            source for source in rejected
            if source.get("reason") == "same_step_effect_occurs_after_read_precondition"
        ]
        if edge.get("effect_kind") == "MUTATE":
            code = "PLAN_MUTATION_PRECONDITION_UNSATISFIED"
            message = "Mutable workspace precondition has no explicit reachable source."
            affected = ["workspace_mutations", "dependencies"]
        elif same_step_sources and any(
            source.get("effect_kind") == "PRODUCE" for source in same_step_sources
        ):
            code = "PLAN_ARTIFACT_ORIGIN_CONFLICT"
            message = "A same-step PRODUCE cannot satisfy the step's earlier READ precondition."
            affected = ["inputs", "outputs", "dependencies"]
        elif same_step_sources and any(
            source.get("effect_kind") == "MUTATE" for source in same_step_sources
        ):
            code = "PLAN_RESOURCE_READ_BEFORE_MUTATION"
            message = "A same-step mutation cannot satisfy the step's earlier READ precondition."
            affected = ["inputs", "workspace_mutations", "dependencies"]
        elif binding.get("resource_kind") == "immutable_artifact":
            code = "PLAN_ARTIFACT_UNAVAILABLE"
            message = "Consumed immutable artifact has no explicit reachable source."
            affected = ["inputs", "outputs", "dependencies"]
        else:
            code = "PLAN_RESOURCE_UNAVAILABLE"
            message = "Consumed resource has no explicit reachable availability source."
            affected = ["inputs", "workspace_mutations", "dependencies"]
        dataflow_issues.append(_dataflow_issue(
            code,
            plan_state=compiled_plan,
            resource_ref=str(edge.get("declared_resource_ref") or identity),
            resource_binding=binding,
            consumer_step=str(edge.get("step_id") or ""),
            dependency_ancestors=sorted(
                ancestors.get(str(edge.get("step_id") or ""), set())
            ),
            recognized_sources=recognized,
            accepted_sources=accepted,
            rejected_sources=rejected,
            source_effect_kinds=[
                str(source.get("effect_kind") or "") for source in recognized
            ],
            field_path=str(edge.get("field_path") or "$.steps"),
            affected_fields=affected,
            actual_facts={
                "accepted_source_count": len(accepted),
                "source_analysis_status": (
                    "complete_no_source" if not recognized else "complete_sources_rejected"
                ),
            },
            required_condition=(
                {"available_before_step": True, "required_state": required_state}
                if required_state
                else {"available_before_step": True}
            ),
            message=message,
            explicit_contract_source=str(
                edge.get("contract_source")
                or "plan.inputs_and_dependencies"
            ),
            contract_revision=str(edge.get("contract_revision") or ""),
            producer_step_ids=[
                str(source.get("step_id") or "")
                for source in recognized
                if str(source.get("step_id") or "")
            ],
            affected_step_ids=[str(edge.get("step_id") or "")],
        ))

    resources_by_identity: Dict[str, Dict[str, Any]] = {}
    for binding in compiled_bindings.values():
        identity = str(binding.get("canonical_resource_id") or "")
        if identity:
            resources_by_identity[identity] = deepcopy(dict(binding))
    dataflow = {
        "schema_version": 1,
        "compiler_id": PLAN_DATAFLOW_COMPILER_ID,
        "resources": [
            resources_by_identity[key] for key in sorted(resources_by_identity)
        ],
        "edges": sorted(edges, key=lambda item: (
            int(item.get("step_index") or 0),
            str(item.get("effect_kind") or ""),
            str(item.get("canonical_resource_id") or ""),
            str(item.get("field_path") or ""),
        )),
        "dependency_ancestors": {
            step_id: sorted(values) for step_id, values in sorted(ancestors.items())
        },
    }
    compiled_plan["compiled_dataflow"] = deepcopy(dataflow)
    issues = [*effect_issues, *dataflow_issues]
    receipt_body = {
        "schema_version": PLAN_DATAFLOW_RECEIPT_SCHEMA_VERSION,
        "compiler_id": PLAN_DATAFLOW_COMPILER_ID,
        "compiler_version": PLAN_DATAFLOW_COMPILER_VERSION,
        "scope": "plan_dataflow",
        "status": "compiled" if not issues else "invalid",
        "review_compatible": not issues,
        "semantic_source_digest": _digest(
            _dataflow_semantic_projection(compiled_plan)
        ),
        "resource_binding_digest": _digest(compiled_bindings),
        "compiled_dataflow_digest": _digest(dataflow),
        "effect_receipt_id": str(effect_receipt.get("receipt_id") or ""),
        "issue_count": len(issues),
        "issues_digest": _digest(issues),
    }
    receipt = _seal_receipt(receipt_body, prefix="plan-dataflow")
    return compiled_plan, receipt, issues


def validate_plan_dataflow_receipt(
    receipt: Mapping[str, Any] | None,
    compiled_plan: Mapping[str, Any],
    resource_bindings: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate the sealed dataflow fact without reinterpreting Plan prose."""

    value = dict(receipt or {})
    issues: list[Dict[str, Any]] = []
    if not value or not _receipt_seal_valid(value, prefix="plan-dataflow"):
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_SEAL_INVALID", scope="plan_dataflow"
        ))
    if str(value.get("compiler_id") or "") != PLAN_DATAFLOW_COMPILER_ID:
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_COMPILER_INVALID", scope="plan_dataflow"
        ))
    if _safe_int(value.get("schema_version")) != PLAN_DATAFLOW_RECEIPT_SCHEMA_VERSION:
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_SCHEMA_INVALID", scope="plan_dataflow"
        ))
    if _safe_int(value.get("compiler_version")) != PLAN_DATAFLOW_COMPILER_VERSION:
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_VERSION_INVALID", scope="plan_dataflow"
        ))
    if str(value.get("scope") or "") != "plan_dataflow":
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_SCOPE_INVALID", scope="plan_dataflow"
        ))
    expected_source = _digest(_dataflow_semantic_projection(compiled_plan))
    if str(value.get("semantic_source_digest") or "") != expected_source:
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_SOURCE_STALE", scope="plan_dataflow"
        ))
    expected_bindings = _digest(resource_bindings)
    if str(value.get("resource_binding_digest") or "") != expected_bindings:
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_BINDINGS_STALE", scope="plan_dataflow"
        ))
    expected_dataflow = _digest(dict(compiled_plan.get("compiled_dataflow") or {}))
    if str(value.get("compiled_dataflow_digest") or "") != expected_dataflow:
        issues.append(_receipt_issue(
            "PLAN_DATAFLOW_RECEIPT_DATAFLOW_STALE", scope="plan_dataflow"
        ))
    return {
        "valid": not issues,
        "receipt_id": str(value.get("receipt_id") or ""),
        "issues": issues,
    }


def _receipt_issue(code: str, *, scope: str, **evidence: Any) -> Dict[str, Any]:
    return _issue(
        code,
        disposition="INTERNAL_INVALID",
        field_path=f"$.{scope}_receipt",
        message="StepEffect compilation receipt is missing, stale, or invalid.",
        source="step_effect_receipt_validator",
        **evidence,
    )


def validate_step_effect_receipt(
    receipt: Mapping[str, Any] | None,
    step: Mapping[str, Any],
    *,
    capability_registry: Any = None,
    plan_identity: Optional[Mapping[str, Any]] = None,
    step_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate a sealed step receipt against its compiled step identity."""

    value = dict(receipt or {})
    issues: list[Dict[str, Any]] = []
    if not value or not _receipt_seal_valid(value, prefix="step-effect"):
        issues.append(_receipt_issue("STEP_EFFECT_RECEIPT_SEAL_INVALID", scope="step"))
    if str(value.get("compiler_id") or "") != STEP_EFFECT_COMPILER_ID:
        issues.append(_receipt_issue("STEP_EFFECT_RECEIPT_COMPILER_INVALID", scope="step"))
    if str(value.get("scope") or "") != "step":
        issues.append(_receipt_issue("STEP_EFFECT_RECEIPT_SCOPE_INVALID", scope="step"))
    raw_identity = value.get("identity")
    if not isinstance(raw_identity, Mapping):
        issues.append(_receipt_issue(
            "STEP_EFFECT_RECEIPT_IDENTITY_INVALID",
            scope="step",
            received_type=type(raw_identity).__name__,
        ))
        identity: Dict[str, Any] = {}
    else:
        identity = dict(raw_identity)
    step_id = str(step.get("step_id") or step.get("id") or "")
    if str(identity.get("step_id") or "") != step_id:
        issues.append(_receipt_issue(
            "STEP_EFFECT_RECEIPT_IDENTITY_MISMATCH",
            scope="step",
            identity_field="step_id",
        ))
    if step_index is not None and _safe_int(identity.get("step_index")) != int(step_index):
        issues.append(_receipt_issue(
            "STEP_EFFECT_RECEIPT_IDENTITY_MISMATCH",
            scope="step",
            identity_field="step_index",
        ))
    for key, expected in dict(plan_identity or {}).items():
        # The durable Plan semantic hash may be recomputed after compilation
        # adds StepEffect fields.  The compiled-step digest below is the
        # authority for this receipt; IDs/revision remain exact identity keys.
        if key == "declared_plan_semantic_hash":
            continue
        if identity.get(key) != expected:
            issues.append(_receipt_issue(
                "STEP_EFFECT_RECEIPT_IDENTITY_MISMATCH",
                scope="step",
                identity_field=str(key),
            ))
    try:
        effect = canonical_step_effect(step)
        if str(value.get("step_effect_digest") or "") != _digest(effect):
            issues.append(_receipt_issue(
                "STEP_EFFECT_RECEIPT_EFFECT_STALE",
                scope="step",
            ))
        if str(value.get("compiled_step_digest") or "") != _digest(
            _semantic_step_projection(step)
        ):
            issues.append(_receipt_issue(
                "STEP_EFFECT_RECEIPT_STEP_STALE",
                scope="step",
            ))
        if bool(value.get("effect_complete")) != step_effect_complete(effect):
            issues.append(_receipt_issue(
                "STEP_EFFECT_RECEIPT_COMPLETENESS_MISMATCH",
                scope="step",
            ))
    except StepEffectProtocolError:
        issues.append(_receipt_issue(
            "STEP_EFFECT_RECEIPT_STEP_PROTOCOL_INVALID",
            scope="step",
        ))
    if capability_registry is not None:
        selected = _candidate_contracts(step, capability_registry)
        try:
            groups = _contract_effect_groups(selected)
        except StepEffectProtocolError:
            groups = []
        selected_public = [
            {key: deepcopy(item) for key, item in row.items() if not key.startswith("_")}
            for _, rows in groups
            for row in rows
        ]
        expected_digest = _digest(selected_public) if selected_public else ""
        if str(value.get("capability_contract_digest") or "") != expected_digest:
            issues.append(_receipt_issue(
                "STEP_EFFECT_RECEIPT_CAPABILITY_STALE",
                scope="step",
            ))
    return {
        "valid": not issues,
        "code": (
            "STEP_EFFECT_COMPILATION_RECEIPT_VALID"
            if not issues
            else "STEP_EFFECT_COMPILATION_RECEIPT_INVALID"
        ),
        "receipt_id": str(value.get("receipt_id") or ""),
        "effect_complete": bool(value.get("effect_complete")),
        "legacy_compatible": bool(value.get("legacy_compatible")),
        "issues": issues,
    }


def _coerce_plan_receipt(
    receipt: Mapping[str, Any] | None,
    plan_state: Mapping[str, Any],
) -> Dict[str, Any]:
    if isinstance(receipt, Mapping) and receipt.get("scope") == "plan":
        return dict(receipt)
    for container in (
        receipt,
        plan_state.get("plan_validation_state"),
        plan_state.get("validation_receipt"),
    ):
        if not isinstance(container, Mapping):
            continue
        for key in (
            "step_effect_compilation_receipt",
            "step_effect_compilation",
            "plan_compilation_receipt",
        ):
            candidate = container.get(key)
            if isinstance(candidate, Mapping):
                return dict(candidate)
    return {}


def validate_plan_compilation_receipt(
    receipt: Mapping[str, Any] | None,
    plan_state: Mapping[str, Any],
    capability_registry: Any = None,
    *,
    require_complete: bool = False,
    allow_legacy_missing: bool = True,
) -> Dict[str, Any]:
    """Validate a plan receipt without reinterpreting no-artifact steps.

    ``valid`` describes receipt integrity.  ``effect_complete`` separately
    describes whether the compiler accepted every current-schema step.  A
    sealed repair-required receipt can therefore be valid without being review
    acceptable.
    """

    value = _coerce_plan_receipt(receipt, plan_state)
    if not value and allow_legacy_missing and _legacy_plan(plan_state):
        return {
            "valid": True,
            "code": "LEGACY_PLAN_WITHOUT_STEP_EFFECT_RECEIPT",
            "receipt_id": "",
            "effect_complete": False,
            "legacy_compatible": True,
            "issues": [],
        }
    issues: list[Dict[str, Any]] = []
    if not value or not _receipt_seal_valid(value, prefix="plan-step-effects"):
        issues.append(_receipt_issue("PLAN_STEP_EFFECT_RECEIPT_SEAL_INVALID", scope="plan"))
    if str(value.get("compiler_id") or "") != STEP_EFFECT_COMPILER_ID:
        issues.append(_receipt_issue("PLAN_STEP_EFFECT_RECEIPT_COMPILER_INVALID", scope="plan"))
    if str(value.get("scope") or "") != "plan":
        issues.append(_receipt_issue("PLAN_STEP_EFFECT_RECEIPT_SCOPE_INVALID", scope="plan"))
    expected_identity = _plan_identity(plan_state)
    raw_received_identity = value.get("identity")
    if not isinstance(raw_received_identity, Mapping):
        issues.append(_receipt_issue(
            "PLAN_STEP_EFFECT_RECEIPT_IDENTITY_INVALID",
            scope="plan",
            received_type=type(raw_received_identity).__name__,
        ))
        received_identity: Dict[str, Any] = {}
    else:
        received_identity = dict(raw_received_identity)
    for key in ("task_id", "plan_id", "plan_version", "plan_revision"):
        if received_identity.get(key) != expected_identity.get(key):
            issues.append(_receipt_issue(
                "PLAN_STEP_EFFECT_RECEIPT_IDENTITY_MISMATCH",
                scope="plan",
                identity_field=key,
            ))
    # ``compiled_plan_digest`` below binds the actual compiled semantics.  A
    # separately stored Plan semantic hash is derived data and may legitimately
    # be refreshed after StepEffect fields are installed.
    try:
        if str(value.get("compiled_plan_digest") or "") != _digest(
            _plan_projection(plan_state)
        ):
            issues.append(_receipt_issue(
                "PLAN_STEP_EFFECT_RECEIPT_PLAN_STALE",
                scope="plan",
            ))
    except StepEffectProtocolError:
        issues.append(_receipt_issue(
            "PLAN_STEP_EFFECT_RECEIPT_PLAN_PROTOCOL_INVALID",
            scope="plan",
        ))
    if capability_registry is not None:
        registry_rows = [
            {key: deepcopy(item) for key, item in row.items() if not key.startswith("_")}
            for row in _flatten_capability_registry(capability_registry)
        ]
        if str(value.get("capability_registry_digest") or "") != _digest(registry_rows):
            issues.append(_receipt_issue(
                "PLAN_STEP_EFFECT_RECEIPT_CAPABILITY_STALE",
                scope="plan",
            ))
    try:
        state_normalizations = _canonical_normalization_history(
            plan_state.get("step_effect_normalizations")
        )
        receipt_normalizations = _canonical_normalization_history(
            value.get("normalizations")
        )
        if state_normalizations != receipt_normalizations:
            issues.append(_receipt_issue(
                "PLAN_STEP_EFFECT_NORMALIZATION_AUDIT_STALE",
                scope="plan",
            ))
    except StepEffectProtocolError:
        issues.append(_receipt_issue(
            "PLAN_STEP_EFFECT_NORMALIZATION_AUDIT_INVALID",
            scope="plan",
        ))
    step_receipts = value.get("step_receipts")
    plan_steps = [
        item for item in plan_state.get("steps", []) or [] if isinstance(item, Mapping)
    ]
    if not isinstance(step_receipts, list) or len(step_receipts) != len(plan_steps):
        issues.append(_receipt_issue(
            "PLAN_STEP_EFFECT_RECEIPT_STEP_SET_MISMATCH",
            scope="plan",
        ))
    else:
        for index, (step_receipt, step) in enumerate(zip(step_receipts, plan_steps)):
            validation = validate_step_effect_receipt(
                step_receipt if isinstance(step_receipt, Mapping) else None,
                step,
                capability_registry=capability_registry,
                plan_identity=expected_identity,
                step_index=index,
            )
            issues.extend(validation["issues"])
    if require_complete and not bool(value.get("effect_complete")):
        issues.append(_receipt_issue(
            "PLAN_STEP_EFFECT_RECEIPT_INCOMPLETE",
            scope="plan",
        ))
    if require_complete and (
        str(value.get("status") or "") != "compiled"
        or not bool(value.get("review_compatible"))
        or value.get("issue_count") != 0
        or value.get("issue_dispositions") not in (None, [], ())
    ):
        issues.append(_receipt_issue(
            "PLAN_STEP_EFFECT_RECEIPT_NOT_REVIEWABLE",
            scope="plan",
            receipt_status=str(value.get("status") or "missing"),
            issue_count=value.get("issue_count"),
            issue_dispositions=value.get("issue_dispositions"),
        ))
    return {
        "valid": not issues,
        "code": (
            "PLAN_STEP_EFFECT_COMPILATION_RECEIPT_VALID"
            if not issues
            else "PLAN_STEP_EFFECT_COMPILATION_RECEIPT_INVALID"
        ),
        "receipt_id": str(value.get("receipt_id") or ""),
        "effect_complete": bool(value.get("effect_complete")),
        "review_compatible": bool(value.get("review_compatible")),
        "legacy_compatible": bool(value.get("legacy_compatible")),
        "status": str(value.get("status") or ""),
        "issues": issues,
    }


def _evidence_entries(value: Any, *, field: str, field_path: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        if _identity_for_entry(field, value):
            raw_values: Sequence[Any] = [value]
        else:
            # Runtime bindings and observations commonly use identity -> value.
            raw_values = [
                {
                    "identity": str(key),
                    "evidence": _json_value(item, field_path=f"{field_path}.{key}"),
                }
                for key, item in value.items()
                if str(key).strip()
            ]
    elif isinstance(value, (str, list, tuple, set, frozenset)):
        raw_values = [value] if isinstance(value, str) else list(value)
    else:
        return []
    result: list[Any] = []
    for index, item in enumerate(raw_values):
        try:
            result.append(_canonical_entry(
                item,
                field=field,
                field_path=f"{field_path}[{index}]",
            ))
        except StepEffectProtocolError:
            continue
    return result


def canonical_execution_effect_evidence(value: Any) -> Dict[str, Any]:
    """Project flexible execution/Supervisor evidence into effect identities."""

    result: Dict[str, Any] = {
        "schema_version": STEP_EFFECT_SCHEMA_VERSION,
        **{field: [] for field in CANONICAL_STEP_EFFECT_FIELDS},
    }
    containers: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        containers.append(value)
        for wrapper in ("step_effect_evidence", "effect_evidence", "effects"):
            nested = value.get(wrapper)
            if isinstance(nested, Mapping):
                containers.append(nested)
        # A single typed evidence row may be returned directly.
        kind = str(value.get("kind") or value.get("effect_kind") or "").strip().lower()
        kind_field = {
            "artifact": "produces_artifacts",
            "binding": "produces_bindings",
            "observation": "produces_observations",
            "result": "produces_observations",
            "workspace_mutation": "workspace_mutations",
            "side_effect": "explicit_side_effects",
            "explicit_side_effect": "explicit_side_effects",
        }.get(kind)
        if kind_field:
            result[kind_field].extend(
                _evidence_entries(value, field=kind_field, field_path="$")
            )
        else:
            for field in CANONICAL_STEP_EFFECT_FIELDS:
                if _identity_for_entry(field, value):
                    result[field].extend(
                        _evidence_entries(value, field=field, field_path="$")
                    )
                    break
    elif isinstance(value, (list, tuple)):
        for item in value:
            projected = canonical_execution_effect_evidence(item)
            for field in CANONICAL_STEP_EFFECT_FIELDS:
                result[field].extend(projected[field])
    for container_index, container in enumerate(containers):
        for field, aliases in _EFFECT_ALIASES.items():
            evidence_aliases = aliases
            if field == "produces_artifacts":
                evidence_aliases = (*aliases, "outputs", "produced_artifacts")
            elif field == "produces_bindings":
                evidence_aliases = (
                    *aliases,
                    "produced_bindings",
                    "accepted_bindings",
                    "accepted_plan_bindings",
                )
            elif field == "produces_observations":
                evidence_aliases = (*aliases, "results", "produced_observations")
            elif field == "workspace_mutations":
                evidence_aliases = (*aliases, "mutated_workspaces")
            elif field == "explicit_side_effects":
                evidence_aliases = (*aliases, "completed_side_effects")
            for alias in evidence_aliases:
                if alias not in container:
                    continue
                result[field].extend(_evidence_entries(
                    container.get(alias),
                    field=field,
                    field_path=f"$.evidence[{container_index}].{alias}",
                ))
    for field in CANONICAL_STEP_EFFECT_FIELDS:
        by_identity: Dict[str, Any] = {}
        for entry in result[field]:
            identity = _identity_text(entry, field=field)
            if not identity:
                continue
            current = by_identity.get(identity)
            if current is None or (
                isinstance(entry, Mapping) and not isinstance(current, Mapping)
            ):
                by_identity[identity] = deepcopy(entry)
        result[field] = sorted(by_identity.values(), key=_canonical_json)
    return result


def observation_emission_contract(effect: Any) -> Dict[str, Any]:
    """One public definition shared by Generate, emitters and parser feedback.

    The explicit approved identity is the model's choice. Invocation/provenance
    identities belong to Execute's receipt, never to stdout supplied by code.
    """
    declared = canonical_step_effect(effect)
    identities = [_identity_text(row, field="produces_observations")
                  for row in declared["produces_observations"]]
    return {
        "schema_version": STRUCTURED_EFFECT_EMISSION_VERSION,
        "prefix": STRUCTURED_EFFECT_EVIDENCE_PREFIX,
        "approved_observation_ids": identities,
        "examples": [emit_observation(identity, {"sample_result": "replace with actual value"})
                     for identity in identities],
        "rules": ["Select an explicit approved observation identity; do not infer an identity from value-only stdout.",
                  "Negative observations are evidence, not automatic completion; approved predicates still apply.",
                  "Execute binds task/plan/step/action/attempt and persists the parser receipt.",
                  "Reader errors and sampling limits do not establish absence or full scientific validity."],
    }


def emit_observation(identity: str, value: Any) -> str:
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("OBSERVATION_IDENTITY_REQUIRED")
    return STRUCTURED_EFFECT_EVIDENCE_PREFIX + json.dumps({
        "schema_version": STRUCTURED_EFFECT_EMISSION_VERSION,
        "produces_observations": [{"identity": identity, "value": value}],
    }, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _emit_declared_rows(declarations, facts, prefix):
    """Standalone serializer kernel: no filesystem, inference or execution."""
    def reject(code, path, actual, expected):
        raise ValueError(json.dumps({"code": code, "field_path": path,
                                     "actual": actual, "expected": expected}, sort_keys=True))

    if not isinstance(facts, dict):
        reject("EFFECT_FACTS_REQUIRED", "$.facts", type(facts).__name__, "object")
    unknown = sorted(set(facts) - set(declarations))
    if unknown:
        reject("EFFECT_FIELD_UNDECLARED", "$.facts", unknown, sorted(declarations))
    payload = {"schema_version": 1}
    for field, rows in declarations.items():
        supplied = facts.get(field, {})
        if not isinstance(supplied, dict):
            reject("EFFECT_FACTS_REQUIRED", "$.facts." + field, type(supplied).__name__, "identity-keyed object")
        expected = [row["identity"] for row in rows]
        if set(supplied) != set(expected):
            reject("EFFECT_FACT_IDENTITIES_MISMATCH", "$.facts." + field, sorted(supplied), expected)
        emitted = []
        for row in rows:
            identity = row["identity"]
            value = supplied[identity]
            path = "$.facts." + field + "." + identity
            if not isinstance(value, dict) or not value:
                reject("EFFECT_FACTS_REQUIRED", path, value, "nonempty actual fact object")
            if any(key in value for key in ("identity", "artifact_id", "effect_id", "kind", "effect_kind")):
                reject("EFFECT_IDENTITY_OVERRIDE_FORBIDDEN", path, sorted(value), "facts without identity overrides")
            missing = [key for key in row["required_fields"] if value.get(key) is None]
            if field == "produces_artifacts" and not (isinstance(value.get("path"), str) and value["path"].strip()):
                missing.append("path")
            if missing:
                reject("EFFECT_FACT_FIELDS_MISSING", path, sorted(value), sorted(set(missing)))
            emitted.append({"identity": identity, **value})
        if emitted:
            payload[field] = emitted
    if len(payload) == 1:
        return ""  # no supported declaration: fabricate nothing
    return prefix + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _declared_emission_rows(effect: Any) -> Dict[str, Any]:
    declared = canonical_step_effect(effect)
    return {
        field: [{"identity": _identity_text(row, field=field),
                 "required_fields": list((row.get("evidence_contract") or {}).get("required_fields") or [])
                 if isinstance(row, Mapping) else []}
                for row in declared[field]]
        for field in ("produces_artifacts", "explicit_side_effects", "produces_observations")
    }


def emit_declared_effects(effect: Any, facts: Dict[str, Any]) -> str:
    """Serialize caller-observed facts under exact declarations, never verify them.

    This helper grants no completion authority. Execute/parser/verification and
    CommitStep still validate identities, provenance, paths and actual results.
    """
    return _emit_declared_rows(_declared_emission_rows(effect), facts, STRUCTURED_EFFECT_EVIDENCE_PREFIX)


def declared_effect_emission_contract(approved_snapshot: Mapping[str, Any], step_id: str) -> Dict[str, Any]:
    """Render exact Python from the snapshot already checked by the caller.

    No implicit state load, no first-output selection, no fallback to a mutable
    step. The snippet uses only Python's json module in the approved script.
    """
    from agent_core.phase_timing import phase

    with phase("generate.effect_template"):
        if not approved_snapshot:
            return {"available": False, "reason": "approved_snapshot_missing", "python_template": ""}
        steps = [row for row in approved_snapshot.get("steps", [])
                 if isinstance(row, Mapping) and row.get("step_id") == step_id]
        if len(steps) != 1:
            raise ValueError("APPROVED_EMISSION_STEP_NOT_UNIQUE")
        declarations = _declared_emission_rows(steps[0])
        # Use the same tested serializer body for the standalone script. No
        # scientific command, placeholder success, or module-path assumption.
        code = "import json\n\n" + inspect.getsource(_emit_declared_rows)
        code += "\n_DECLARED_EFFECT_ROWS = json.loads(" + repr(json.dumps(declarations, ensure_ascii=False)) + ")\n"
        code += "\ndef emit_effect_evidence(actual_facts):\n"
        code += "    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, " + repr(STRUCTURED_EFFECT_EVIDENCE_PREFIX) + ")\n"
        return {"available": True, "schema_version": STRUCTURED_EFFECT_EMISSION_VERSION,
                "step_id": step_id,
                "declarations": declarations, "python_template": code,
                "rules": [
                    "Use this exact serializer in the approved script; do not handwrite machine identities or canonical groups.",
                    "Supply identity-keyed actual_facts only after performing the real checks or operations. Report failed/unknown observations faithfully; claim a successful operation only when it actually succeeded.",
                    "Artifact facts require the actual path. Side-effect and observation facts require every declared field; observations carry the actual value, including failure or unknown. Declarations alone are not evidence.",
                    "Historical failed scripts/stdout are diagnostic evidence; their failed machine emission must not be reused. This current template takes precedence.",
                    "No emission or rc=0 alone grants completion; Execute, verification and CommitStep remain authoritative.",
                ]}


def assert_declared_emission_current(template: Mapping[str, Any], approved_snapshot: Mapping[str, Any], step_id: str) -> None:
    """Reject a frozen template if the transport's fresh approved step changed.

    Consume the caller's existing snapshot; do not load, regenerate messages,
    reinterpret identities, or introduce a new retry/approval path.
    """
    if not approved_snapshot and not template.get("available"):
        return
    rows = [row for row in approved_snapshot.get("steps", [])
            if isinstance(row, Mapping) and row.get("step_id") == step_id]
    current = _declared_emission_rows(rows[0]) if len(rows) == 1 else None
    if template.get("step_id") != step_id or current != template.get("declarations"):
        raise ValueError(json.dumps({"code": "APPROVED_EFFECT_EMISSION_TEMPLATE_STALE",
            "field_path": "$.approved_effect_emission_template.declarations",
            "actual": template.get("declarations"), "expected": current}, sort_keys=True))


def structured_effect_parse_receipt(text: Any, effect: Any) -> Dict[str, Any]:
    """Preserve malformed/foreign claims without guessing an effect identity."""
    raw = str(text or "")
    declared = canonical_step_effect(effect)
    issues: list[Dict[str, Any]] = []
    count = 0
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip().startswith(STRUCTURED_EFFECT_EVIDENCE_PREFIX):
            continue
        count += 1
        field_path = f"stdout.lines[{number}]"
        try:
            payload = json.loads(line.strip()[len(STRUCTURED_EFFECT_EVIDENCE_PREFIX):])
        except ValueError:
            issues.append({"code": "EFFECT_EMISSION_JSON_INVALID", "field_path": field_path})
            continue
        if isinstance(payload, Mapping) and "schema_version" in payload and (
                type(payload["schema_version"]) is not int or payload["schema_version"] != STRUCTURED_EFFECT_EMISSION_VERSION):
            issues.append({"code": "EFFECT_EMISSION_VERSION_UNSUPPORTED", "field_path": field_path + ".schema_version"})
            continue
        projected = canonical_execution_effect_evidence(payload)
        if not any(projected[field] for field in CANONICAL_STEP_EFFECT_FIELDS):
            issues.append({"code": "EFFECT_EMISSION_IDENTITY_MISSING", "field_path": field_path,
                           "expected": "canonical effect field and explicit approved identity"})
        for field in CANONICAL_STEP_EFFECT_FIELDS:
            expected = {_identity_text(row, field=field) for row in declared[field]}
            for row in projected[field]:
                identity = _identity_text(row, field=field)
                if identity not in expected:
                    issues.append({"code": "EFFECT_EMISSION_IDENTITY_UNDECLARED", "field_path": field_path + "." + field,
                                   "actual": identity, "expected": sorted(expected)})
    return {"schema_version": 1, "parser_id": "biomind.structured_effect_emission.v1",
            "raw_stdout_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "emission_count": count, "status": "rejected" if issues else "parsed",
            "issues": issues, "emission_contract": observation_emission_contract(declared)}


def extract_structured_effect_evidence(text: Any) -> Dict[str, Any]:
    """Read typed non-file results emitted by one executed command.

    Commands may write ordinary human-readable stdout and additionally emit
    one or more lines shaped as ``BIOMIND_EFFECT_EVIDENCE_JSON=<json>``.  Only
    canonical effect fields survive this parser; arbitrary stdout never becomes
    completion evidence.
    """

    collected: Dict[str, list[Any]] = {
        field: [] for field in CANONICAL_STEP_EFFECT_FIELDS
    }
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(STRUCTURED_EFFECT_EVIDENCE_PREFIX):
            continue
        encoded = line[len(STRUCTURED_EFFECT_EVIDENCE_PREFIX):].strip()
        if not encoded:
            continue
        try:
            payload = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, (Mapping, list, tuple)):
            continue
        if isinstance(payload, Mapping) and "schema_version" in payload and (
                type(payload["schema_version"]) is not int or payload["schema_version"] != STRUCTURED_EFFECT_EMISSION_VERSION):
            continue
        projected = canonical_execution_effect_evidence(payload)
        for field in CANONICAL_STEP_EFFECT_FIELDS:
            collected[field].extend(projected.get(field) or [])
    result: Dict[str, Any] = {"schema_version": STEP_EFFECT_SCHEMA_VERSION}
    for field in CANONICAL_STEP_EFFECT_FIELDS:
        by_identity: Dict[str, Any] = {}
        for item in collected[field]:
            identity = _identity_text(item, field=field)
            if identity:
                by_identity[identity] = deepcopy(item)
        result[field] = sorted(by_identity.values(), key=_canonical_json)
    return result


def _evidence_accepted(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return True
    if value.get("verified") is False or value.get("accepted") is False:
        return False
    status = str(value.get("status") or "").strip().lower()
    return status not in _FAILED_EVIDENCE_STATUSES


def _evidence_contract_satisfied(declared: Any, observed: Any) -> bool:
    if not isinstance(declared, Mapping):
        return True
    contract = declared.get("evidence_contract")
    if not isinstance(contract, Mapping):
        return True
    required_fields = contract.get("required_fields") or []
    if not isinstance(required_fields, (list, tuple)):
        return False
    if not isinstance(observed, Mapping):
        return not required_fields
    evidence_body = observed.get("evidence")
    evidence_mapping = evidence_body if isinstance(evidence_body, Mapping) else observed
    return all(
        isinstance(field, str)
        and field
        and field in evidence_mapping
        and evidence_mapping.get(field) is not None
        for field in required_fields
    )


def match_step_effect_evidence(
    effect_or_step: Any,
    execution_evidence: Any,
    *,
    allow_legacy_untyped: bool = False,
) -> Dict[str, Any]:
    """Match declared StepEffect identities to structured execution evidence.

    A present observation counts even when its value is ``false``; scientific
    success criteria, not this structural matcher, interpret that value.
    """

    try:
        declared = canonical_step_effect(effect_or_step)
    except StepEffectProtocolError as exc:
        return {
            "complete": False,
            "declared_effect_complete": False,
            "matched_effects": {},
            "missing_effects": {},
            "issues": [exc.to_issue()],
        }
    observed = canonical_execution_effect_evidence(execution_evidence)
    matched: Dict[str, list[Any]] = {}
    missing: Dict[str, list[Any]] = {}
    for field in CANONICAL_STEP_EFFECT_FIELDS:
        observed_map = _entry_map(observed[field], field=field)
        matched[field] = []
        missing[field] = []
        for requirement in declared[field]:
            identity = _identity_text(requirement, field=field)
            candidate = observed_map.get(identity)
            if (
                candidate is not None
                and _evidence_accepted(candidate)
                and _evidence_contract_satisfied(requirement, candidate)
            ):
                matched[field].append(deepcopy(candidate))
            else:
                missing[field].append(deepcopy(requirement))
    declared_complete = step_effect_complete(declared)
    complete = bool(
        (declared_complete or allow_legacy_untyped)
        and not any(missing[field] for field in CANONICAL_STEP_EFFECT_FIELDS)
    )
    issues: list[Dict[str, Any]] = []
    if not declared_complete and not allow_legacy_untyped:
        issues.append(unspecified_effect_issue())
    for field in CANONICAL_STEP_EFFECT_FIELDS:
        if not missing[field]:
            continue
        issues.append(_issue(
            "STEP_EFFECT_EVIDENCE_MISSING",
            disposition="REPAIR_REQUIRED",
            field_path=f"$.step_effect.{field}",
            message="Execution evidence does not satisfy the declared StepEffect.",
            source="step_effect_evidence_matcher",
            effect_kind=EFFECT_KIND_BY_FIELD[field],
            missing_identities=[
                _identity_text(item, field=field) for item in missing[field]
            ],
            expected="structured evidence for every declared effect identity",
            actual="one or more effect identities were not evidenced",
        ))
    return {
        "schema_version": 1,
        "complete": complete,
        "declared_effect_complete": declared_complete,
        "legacy_compatible": bool(allow_legacy_untyped and not declared_complete),
        "declared_effect": declared,
        "observed_effect_evidence": observed,
        "matched_effects": matched,
        "missing_effects": missing,
        "issues": issues,
    }


def step_effect_evidence_matches(
    effect_or_step: Any,
    execution_evidence: Any,
    *,
    allow_legacy_untyped: bool = False,
) -> bool:
    """Boolean convenience wrapper for completion gates."""

    return bool(match_step_effect_evidence(
        effect_or_step,
        execution_evidence,
        allow_legacy_untyped=allow_legacy_untyped,
    )["complete"])


# Integration-friendly aliases.  They intentionally preserve the route-blind
# return contracts above rather than adding workflow-specific behavior.
compile_plan_effects = compile_plan_step_effects
validate_compilation_receipt = validate_plan_compilation_receipt
match_execution_evidence = match_step_effect_evidence


__all__ = [
    "CANONICAL_STEP_EFFECT_FIELDS",
    "EFFECT_KIND_BY_FIELD",
    "PLAN_DATAFLOW_COMPILER_ID",
    "PLAN_DATAFLOW_COMPILER_VERSION",
    "PLAN_DATAFLOW_RECEIPT_SCHEMA_VERSION",
    "STEP_EFFECT_COMPILER_ID",
    "STEP_EFFECT_COMPILER_VERSION",
    "STEP_EFFECT_RECEIPT_SCHEMA_VERSION",
    "STEP_EFFECT_SCHEMA_VERSION",
    "STRUCTURED_EFFECT_EVIDENCE_PREFIX",
    "StepEffect",
    "StepEffectProtocolError",
    "canonical_execution_effect_evidence",
    "canonical_step_effect",
    "compile_plan_effects",
    "compile_plan_dataflow",
    "compile_plan_step_effects",
    "compile_step_effect",
    "extract_structured_effect_evidence",
    "match_execution_evidence",
    "match_step_effect_evidence",
    "protocol_invalid_issue",
    "step_effect_complete",
    "step_effect_evidence_matches",
    "unspecified_effect_issue",
    "validate_compilation_receipt",
    "validate_plan_dataflow_receipt",
    "validate_plan_compilation_receipt",
    "validate_step_effect_receipt",
]

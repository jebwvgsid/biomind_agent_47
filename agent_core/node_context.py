"""Control-plane/LLM-plane boundary for BioMind node invocations.

Only :class:`LLMPayload` is serialised into an LLM message.  Controller
identity, concurrency guards, immutable refs and trace fingerprints remain in
the accompanying :class:`ControllerBinding` / :class:`ProjectionTrace`.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from agent_core.plan_tracker import plan_record_hash, semantic_plan_hash
from agent_core.task_spec import (
    semantic_constraints_from_task_spec,
    task_record_hash,
    task_semantic_hash,
)
from agent_core.plan_contract import new_candidate_scope_id

NODE_CONTEXT_SCHEMA_VERSION = 2

NODE_STALENESS_DOMAINS = {
    "consultant": "consultant-semantic-mutation-v1",
    "planner": "planner-v1",
    "planner_repair": "planner-repair-v1",
    "generate": "generate-v1",
    "recovery": "recovery-v1",
    "supervisor": "supervisor-review-v1",
    "plan_review": "plan-review-v1",
}

# Keys that carry identity/integrity/concurrency metadata rather than domain
# meaning.  The boundary rejects them recursively instead of silently deleting
# them, so a newly introduced leak fails before a model call.
_CONTROL_KEY_EXACT = {
    "invocation_id",
    "state_generation",
    "generation",
    "task_revision",
    "task_record_hash",
    "task_semantic_hash",
    "plan_revision",
    "plan_record_hash",
    "plan_semantic_hash",
    "approval_id",
    "approval_ref",
    "approval_scope_hash",
    "projection_semantic_hash",
    "node_context_hash",
    "cas_token",
    "content_ref",
    "object_ref",
    "semantic_handle_map_ref",
    "failure_fingerprint",
    "violation_set_identity",
    "context_id",
    "task_id",
    "plan_id",
    "step_id",
    "action_id",
    "attempt_id",
    "execution_id",
    "manifest_id",
    "validation_id",
}
_CONTROL_KEY_SUFFIXES = (
    "_hash",
    "_generation",
    "_cas",
    "_cas_token",
    "_object_ref",
    "_content_ref",
)
_CONTROL_VALUE_PATTERNS = (
    re.compile(r"^sha256:[0-9a-f]{32,}$", re.IGNORECASE),
    re.compile(r"^(?:object|audit|evidence):sha256:[0-9a-f]{16,}$", re.IGNORECASE),
    re.compile(r"^cas:[A-Za-z0-9._:-]+$"),
)
_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_PLACEHOLDER_RE = re.compile(r"^\{(input|output):([A-Za-z][A-Za-z0-9_.-]*)\}$")


class NodeContextError(ValueError):
    """Base class for deterministic node-context protocol failures."""


class ControlMetadataLeakError(NodeContextError):
    code = "LLM_CONTROL_METADATA_LEAK"


class ContextCapacityExceeded(NodeContextError):
    code = "CONTEXT_CAPACITY_EXCEEDED"


class SemanticHandleError(NodeContextError):
    code = "SEMANTIC_HANDLE_INVALID"


class InvocationProtocolError(NodeContextError):
    code = "INVOCATION_PROTOCOL_ERROR"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _slug(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text).strip("_.-")
    return (text or fallback)[:80]


def _without_control_metadata(value: Any) -> Any:
    """Make semantic rows safe without converting them into a summary."""
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in _CONTROL_KEY_EXACT or lowered.endswith(_CONTROL_KEY_SUFFIXES):
                continue
            if lowered in {
                "source", "source_id", "source_ref", "source_refs", "source_type",
                "provenance", "audit_ref", "audit_refs", "evidence_ref", "evidence_refs",
                "created_at", "updated_at", "recorded_at", "timestamp",
            }:
                continue
            if item in (None, "", [], {}):
                continue
            result[key] = _without_control_metadata(item)
        return result
    if isinstance(value, list):
        return [_without_control_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_without_control_metadata(item) for item in value]
    return deepcopy(value)


def control_metadata_leaks(value: Any, path: str = "$LLMPayload") -> List[str]:
    """Return precise paths at which control-plane metadata entered a payload."""
    leaks: List[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            child = f"{path}.{key}"
            if lowered in _CONTROL_KEY_EXACT or lowered.endswith(_CONTROL_KEY_SUFFIXES):
                leaks.append(child)
            leaks.extend(control_metadata_leaks(item, child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            leaks.extend(control_metadata_leaks(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        stripped = value.strip()
        if any(pattern.match(stripped) for pattern in _CONTROL_VALUE_PATTERNS):
            leaks.append(path)
    return sorted(set(leaks))


def assert_llm_payload_clean(value: Any) -> None:
    leaks = control_metadata_leaks(value)
    if leaks:
        raise ControlMetadataLeakError(
            f"LLM_CONTROL_METADATA_LEAK: prohibited control metadata at {', '.join(leaks[:20])}"
        )


@dataclass(frozen=True)
class SemanticHandleBinding:
    semantic_handle: str
    object_type: str
    stable_identity: str
    direction: str = ""
    allowed_usage: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _HANDLE_RE.match(self.semantic_handle):
            raise SemanticHandleError(f"SEMANTIC_HANDLE_INVALID: {self.semantic_handle!r}")
        if not self.stable_identity:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_IDENTITY_MISSING: {self.semantic_handle}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "semantic_handle": self.semantic_handle,
            "object_type": self.object_type,
            "stable_identity": self.stable_identity,
            "direction": self.direction,
            "allowed_usage": list(self.allowed_usage),
            "metadata": deepcopy(dict(self.metadata)),
        }


class SemanticHandleMap:
    """Invocation binding that resolves every model-visible handle uniquely."""

    def __init__(self, bindings: Iterable[SemanticHandleBinding] = ()) -> None:
        self._bindings: Dict[str, SemanticHandleBinding] = {}
        for binding in bindings:
            self.add(binding)

    def add(self, binding: SemanticHandleBinding) -> None:
        existing = self._bindings.get(binding.semantic_handle)
        if existing is not None and existing != binding:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_AMBIGUOUS: {binding.semantic_handle} maps to "
                f"{existing.stable_identity!r} and {binding.stable_identity!r}"
            )
        self._bindings[binding.semantic_handle] = binding

    def resolve(
        self,
        handle: str,
        *,
        object_type: str = "",
        direction: str = "",
        usage: str = "",
    ) -> SemanticHandleBinding:
        binding = self._bindings.get(str(handle or ""))
        if binding is None:
            raise SemanticHandleError(f"SEMANTIC_HANDLE_UNKNOWN: {handle!r}")
        if object_type and binding.object_type != object_type:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_TYPE_MISMATCH: {handle} is {binding.object_type}, expected {object_type}"
            )
        if direction and binding.direction != direction:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_DIRECTION_MISMATCH: {handle} is {binding.direction}, expected {direction}"
            )
        if usage and binding.allowed_usage and usage not in binding.allowed_usage:
            raise SemanticHandleError(
                f"SEMANTIC_HANDLE_USAGE_FORBIDDEN: {handle} cannot be used for {usage}"
            )
        return binding

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return {key: value.to_dict() for key, value in sorted(self._bindings.items())}

    def __contains__(self, handle: object) -> bool:
        return str(handle) in self._bindings

    def __len__(self) -> int:
        return len(self._bindings)


@dataclass(frozen=True)
class ControllerBinding:
    invocation_id: str
    node_type: str
    staleness_domain: str
    state_generation: int
    task_semantic_hash: str
    task_record_hash: str
    plan_semantic_hash: str = ""
    plan_record_hash: str = ""
    approval_ref: str = ""
    approval_applicability: str = ""
    step_id: str = ""
    execution_epoch: str = ""
    attempt_id: str = ""
    failure_fingerprint: str = ""
    violation_set_identity: str = ""
    action_identity: str = ""
    evidence_identity: str = ""
    candidate_scope_id: str = ""
    touched_entity_versions: Mapping[str, str] = field(default_factory=dict)
    semantic_handle_map: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    created_at_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "node_type": self.node_type,
            "staleness_domain": self.staleness_domain,
            "state_generation": self.state_generation,
            "task_semantic_hash": self.task_semantic_hash,
            "task_record_hash": self.task_record_hash,
            "plan_semantic_hash": self.plan_semantic_hash,
            "plan_record_hash": self.plan_record_hash,
            "approval_ref": self.approval_ref,
            "approval_applicability": self.approval_applicability,
            "step_id": self.step_id,
            "execution_epoch": self.execution_epoch,
            "attempt_id": self.attempt_id,
            "failure_fingerprint": self.failure_fingerprint,
            "violation_set_identity": self.violation_set_identity,
            "action_identity": self.action_identity,
            "evidence_identity": self.evidence_identity,
            "candidate_scope_id": self.candidate_scope_id,
            "touched_entity_versions": dict(self.touched_entity_versions),
            "semantic_handle_map": deepcopy(dict(self.semantic_handle_map)),
        }


@dataclass(frozen=True)
class ProjectionTrace:
    projection_type: str
    projection_version: str
    selection_policy: str
    included_constraint_handles: Tuple[str, ...]
    included_artifact_handles: Tuple[str, ...]
    excluded_handles_with_reason: Mapping[str, str]
    projection_semantic_hash: str
    node_context_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "projection_type": self.projection_type,
            "projection_version": self.projection_version,
            "selection_policy": self.selection_policy,
            "included_constraint_handles": list(self.included_constraint_handles),
            "included_artifact_handles": list(self.included_artifact_handles),
            "excluded_handles_with_reason": dict(self.excluded_handles_with_reason),
            "projection_semantic_hash": self.projection_semantic_hash,
            "node_context_hash": self.node_context_hash,
        }


@dataclass(frozen=True)
class LLMPayload:
    """Non-authoritative semantic data that may be serialised for the model."""

    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        assert_llm_payload_clean(self.value)

    def to_dict(self) -> Dict[str, Any]:
        return deepcopy(dict(self.value))

    def to_json(self, *, indent: Optional[int] = None) -> str:
        return json.dumps(self.value, ensure_ascii=False, sort_keys=True, indent=indent, default=str)


@dataclass(frozen=True)
class NodeContext:
    binding: ControllerBinding
    llm_payload: LLMPayload
    projection_trace: Optional[ProjectionTrace] = None


def _unique_handle(prefix: str, seed: Any, used: MutableMapping[str, int]) -> str:
    base = f"{prefix}.{_slug(seed, fallback='item')}"
    count = used.get(base, 0) + 1
    used[base] = count
    return base if count == 1 else f"{base}.{count}"


def _declared_or_unique_handle(
    prefix: str,
    declared: Any,
    seed: Any,
    used: MutableMapping[str, int],
) -> str:
    value = str(declared or "").strip()
    if value.startswith(prefix + ".") and _HANDLE_RE.match(value):
        count = used.get(value, 0) + 1
        used[value] = count
        return value if count == 1 else f"{value}.{count}"
    return _unique_handle(prefix, seed, used)


def _semantic_artifact_rows(
    task_spec: Mapping[str, Any],
    handle_map: SemanticHandleMap,
    used: MutableMapping[str, int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    for index, raw in enumerate(task_spec.get("inputs", []) or [], start=1):
        if not isinstance(raw, Mapping):
            continue
        role = raw.get("role") or raw.get("name") or raw.get("input_id") or f"input_{index}"
        handle = _declared_or_unique_handle(
            "input", raw.get("semantic_handle"), role, used
        )
        stable_id = str(raw.get("artifact_id") or raw.get("input_id") or raw.get("fact_id") or role)
        handle_map.add(SemanticHandleBinding(
            handle,
            "artifact",
            stable_id,
            direction="input",
            allowed_usage=("read", "materialize_input"),
            metadata={
                "index": index,
                "role": str(role),
                "canonical_path": str(
                    raw.get("canonical_path")
                    or raw.get("resolved_path")
                    or raw.get("path")
                    or ""
                ),
            },
        ))
        row = {
            "handle": handle,
            "role": str(role),
            "format": raw.get("format") or raw.get("type") or "",
            "required": bool(raw.get("required", True)),
            "verification_status": (
                "verified"
                if bool(raw.get("verified"))
                else str(raw.get("status") or "declared")
            ),
        }
        if raw.get("description"):
            row["description"] = str(raw.get("description"))
        # A basename is useful semantic context; the verified absolute path is
        # materialised by the Controller and stays out of the model context.
        path = str(raw.get("canonical_path") or raw.get("resolved_path") or raw.get("path") or "")
        if path:
            row["basename"] = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        inputs.append(row)

    raw_outputs = task_spec.get("output_contracts", []) or []
    for index, raw in enumerate(raw_outputs, start=1):
        if not isinstance(raw, Mapping):
            continue
        role = raw.get("role") or raw.get("name") or raw.get("output_id") or f"output_{index}"
        handle = _declared_or_unique_handle(
            "output", raw.get("semantic_handle"), role, used
        )
        stable_id = str(raw.get("artifact_id") or raw.get("output_id") or raw.get("fact_id") or role)
        handle_map.add(SemanticHandleBinding(
            handle,
            "artifact",
            stable_id,
            direction="output",
            allowed_usage=("write", "declare_output", "materialize_output"),
            metadata={
                "index": index,
                "role": str(role),
                "canonical_path": str(
                    raw.get("canonical_path")
                    or raw.get("path")
                    or raw.get("filename")
                    or ""
                ),
            },
        ))
        row = {
            "handle": handle,
            "role": str(role),
            "format": raw.get("format") or raw.get("type") or "",
            "required": bool(raw.get("required", True)),
            "placeholder": "{output:" + handle.removeprefix("output.") + "}",
        }
        if raw.get("description"):
            row["description"] = str(raw.get("description"))
        path = str(raw.get("canonical_path") or raw.get("path") or raw.get("filename") or "")
        if path:
            row["required_basename"] = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        outputs.append(row)
    return inputs, outputs


def _constraint_rows(
    task_spec: Mapping[str, Any],
    handle_map: SemanticHandleMap,
    used: MutableMapping[str, int],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    artifact_handles: Dict[Tuple[str, str], str] = {}
    artifact_basenames: Dict[str, str] = {}
    for artifact_handle, binding in handle_map.to_dict().items():
        if binding.get("object_type") != "artifact":
            continue
        metadata = binding.get("metadata") or {}
        role = str(metadata.get("role") or "")
        direction = str(binding.get("direction") or "")
        if role and direction:
            artifact_handles[(direction, role)] = artifact_handle
        path = str(metadata.get("canonical_path") or "")
        if path:
            artifact_basenames[artifact_handle] = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    constraints = semantic_constraints_from_task_spec(task_spec)
    ordered = sorted(
        (dict(item) for item in constraints if isinstance(item, Mapping)),
        key=lambda item: (
            str(item.get("semantic_key") or ""),
            str(item.get("constraint_id") or item.get("requirement_id") or item.get("id") or ""),
            json.dumps(item.get("value"), ensure_ascii=False, sort_keys=True, default=str),
        ),
    )
    for index, raw in enumerate(ordered, start=1):
        semantic_key = raw.get("semantic_key") or raw.get("key") or f"constraint_{index}"
        handle = _unique_handle("constraint", semantic_key, used)
        stable_id = str(
            raw.get("constraint_id")
            or raw.get("requirement_id")
            or raw.get("id")
            or f"{semantic_key}:{index}"
        )
        handle_map.add(SemanticHandleBinding(
            handle,
            "constraint",
            stable_id,
            allowed_usage=("reference", "satisfy", "report_violation"),
            metadata={"semantic_key": str(semantic_key)},
        ))
        row = _without_control_metadata(raw)
        for key in ("constraint_id", "requirement_id", "id"):
            row.pop(key, None)
        category, _, role = str(semantic_key).partition(".")
        direction = (
            "input" if category == "required_input"
            else "output" if category == "required_output"
            else ""
        )
        artifact_handle = artifact_handles.get((direction, role)) if direction else ""
        if artifact_handle:
            # Exact artifact locations belong to the ControllerBinding.  The
            # model sees the semantic edge and a basename only.
            for key in (
                "value", "path", "canonical_path", "resolved_path", "filename",
                "source_path", "target_path",
            ):
                row.pop(key, None)
            row["artifact_handle"] = artifact_handle
            basename = artifact_basenames.get(artifact_handle, "")
            if basename:
                row["required_basename" if direction == "output" else "basename"] = basename
        row["handle"] = handle
        rows.append(row)
    return rows


def _step_rows(
    plan_state: Mapping[str, Any],
    handle_map: SemanticHandleMap,
    used: MutableMapping[str, int],
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    rows: List[Dict[str, Any]] = []
    by_id: Dict[str, str] = {}
    for index, raw in enumerate(plan_state.get("steps", []) or [], start=1):
        if not isinstance(raw, Mapping):
            continue
        step_id = str(raw.get("step_id") or f"step-{index}")
        handle = _declared_or_unique_handle(
            "step",
            raw.get("semantic_handle"),
            step_id,
            used,
        )
        by_id[step_id] = handle
        handle_map.add(SemanticHandleBinding(
            handle,
            "plan_step",
            step_id,
            allowed_usage=("reference", "replace", "execute", "review"),
            metadata={"index": index},
        ))
        semantic_fields = {
            "title", "action", "purpose", "tool", "inputs", "input_ids",
            "input_artifacts", "input_refs", "input_contracts", "outputs",
            "output_ids", "output_artifacts", "output_refs", "output_contracts",
            "constraints", "constraint_ids", "semantic_constraint_ids",
            "requirement_ids", "dependencies", "depends_on", "success_criteria",
            "risk_level", "step_type", "skills_declared", "command_intent",
            "artifacts",
        }
        row = {
            str(key): _without_control_metadata(value)
            for key, value in raw.items()
            if str(key) in semantic_fields and value not in (None, "", [], {})
        }
        if isinstance(row.get("artifacts"), list):
            row["artifacts"] = [
                {
                    str(key): deepcopy(value)
                    for key, value in artifact.items()
                    if str(key) in {
                        "role", "artifact_type", "required", "validator",
                        "path", "format", "description", "producer_step_id",
                    }
                    and value not in (None, "", [], {})
                }
                for artifact in row["artifacts"]
                if isinstance(artifact, Mapping)
            ]
        row["handle"] = handle
        rows.append(row)
    for row, raw in zip(rows, [x for x in plan_state.get("steps", []) or [] if isinstance(x, Mapping)]):
        deps = raw.get("dependencies") or raw.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        row["dependencies"] = [by_id.get(str(dep), str(dep)) for dep in deps]
    return rows, by_id


def _project_step_semantic_references(
    rows: Sequence[MutableMapping[str, Any]],
    handle_map: SemanticHandleMap,
) -> None:
    """Replace control-plane entity identities in Plan rows with handles.

    PlanSpec pins stable Task/Plan identities so that deterministic code can
    validate them.  Those identities are not model vocabulary.  The model sees
    the invocation-local semantic handles and the Controller resolves them on
    return.
    """
    stable_to_handle: Dict[str, str] = {}
    for handle, binding in handle_map.to_dict().items():
        stable = str(binding.get("stable_identity") or "")
        if stable:
            stable_to_handle[stable] = handle
        metadata = binding.get("metadata") or {}
        if isinstance(metadata, Mapping):
            semantic_key = str(metadata.get("semantic_key") or "")
            if semantic_key:
                stable_to_handle.setdefault(semantic_key, handle)
            canonical_path = str(metadata.get("canonical_path") or "")
            if canonical_path:
                stable_to_handle.setdefault(canonical_path, handle)

    def project(value: Any) -> Any:
        if isinstance(value, str):
            if value in stable_to_handle:
                return stable_to_handle[value]
            projected = value
            for stable, handle in sorted(
                stable_to_handle.items(),
                key=lambda item: len(item[0]),
                reverse=True,
            ):
                stable_suffix = stable.lstrip("./")
                if stable_suffix and not stable.startswith("sha256:"):
                    def replace_path_token(match: re.Match[str]) -> str:
                        token = match.group(0)
                        if token == stable or token.endswith("/" + stable_suffix):
                            return handle
                        return token

                    # Legacy PlanSpec rows may contain an absolute resolution
                    # of a canonical relative artifact. Replacing only the
                    # relative suffix would leak the machine-specific prefix
                    # (``/root/.../input.foo``), so replace the complete token.
                    projected = re.sub(
                        r"[^\s\[\](),，;；'\"`]+",
                        replace_path_token,
                        projected,
                    )
                if stable and stable in projected:
                    projected = projected.replace(stable, handle)
            return projected
        if isinstance(value, list):
            return [project(item) for item in value]
        if isinstance(value, tuple):
            return [project(item) for item in value]
        if isinstance(value, Mapping):
            return {str(key): project(item) for key, item in value.items()}
        return deepcopy(value)

    for row in rows:
        for key in tuple(row):
            row[key] = project(row[key])


def _visible_handles(value: Any, known: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if value in known:
            found.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            found.update(_visible_handles(item, known))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_visible_handles(item, known))
    return found


def _select_current_step_closure(
    *,
    node_type: str,
    raw_steps: Sequence[Mapping[str, Any]],
    step_rows: Sequence[Mapping[str, Any]],
    current_step_id: str,
    constraints: Sequence[Mapping[str, Any]],
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    raw_outputs: Sequence[Mapping[str, Any]],
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, Any],
    List[Dict[str, Any]],
    Dict[str, str],
    str,
]:
    """Select the deterministic semantic dependency closure for one step.

    The closure is identity/ref driven.  It never uses character budgets or
    natural-language similarity, so a large required closure fails capacity
    checks instead of silently losing the tail of the task contract.
    """
    known_constraint_handles = {
        str(row.get("handle") or "") for row in constraints if row.get("handle")
    }
    known_artifact_handles = {
        str(row.get("handle") or "") for row in [*inputs, *outputs] if row.get("handle")
    }
    known_step_handles = {
        str(row.get("handle") or "") for row in step_rows if row.get("handle")
    }
    selected_index = -1
    for index, raw in enumerate(raw_steps):
        if current_step_id and str(raw.get("step_id") or "") == str(current_step_id):
            selected_index = index
            break
    if selected_index < 0 and step_rows:
        selected_index = 0
    current = deepcopy(dict(step_rows[selected_index])) if selected_index >= 0 else {}

    by_step_handle = {
        str(row.get("handle") or ""): index
        for index, row in enumerate(step_rows)
        if row.get("handle")
    }
    dependency_indexes: List[int] = []
    pending = list(_visible_handles(current.get("dependencies", []), known_step_handles))
    seen_steps: set[str] = set()
    while pending:
        handle = pending.pop(0)
        if handle in seen_steps or handle not in by_step_handle:
            continue
        seen_steps.add(handle)
        index = by_step_handle[handle]
        dependency_indexes.append(index)
        pending.extend(
            sorted(
                _visible_handles(step_rows[index].get("dependencies", []), known_step_handles)
                - seen_steps
            )
        )

    referenced_constraints = _visible_handles(current, known_constraint_handles)
    referenced_artifacts = _visible_handles(current, known_artifact_handles)
    dependency_contracts: List[Dict[str, Any]] = []
    for index in dependency_indexes:
        dependency = deepcopy(dict(step_rows[index]))
        # Only explicitly declared dependency outputs can satisfy the current
        # step. Handle spelling is identity-only and never supplies usage.
        dependency_outputs: set[str] = set()
        for field_name in (
            "outputs", "output_refs", "output_artifacts",
            "output_artifact_handles", "bound_outputs",
        ):
            dependency_outputs.update(
                _visible_handles(dependency.get(field_name, []), known_artifact_handles)
            )
        referenced_artifacts.update(dependency_outputs)
        referenced_constraints.update(
            _visible_handles(dependency, known_constraint_handles)
        )
        dependency_contracts.append({
            key: deepcopy(value)
            for key, value in dependency.items()
            if key in {
                "handle", "title", "action", "outputs", "output_refs",
                "output_artifacts", "output_artifact_handles", "success_criteria",
            }
            and value not in (None, "", [], {})
        })

    for row in constraints:
        handle = str(row.get("handle") or "")
        semantic_key = str(row.get("semantic_key") or "")
        category = semantic_key.partition(".")[0]
        strength = str(row.get("strength") or "").lower()
        scope = str(row.get("enforcement_scope") or "").lower()
        if (
            row.get("task_global") is True
            or scope == "runtime_global"
            or (
                strength in {"hard", "required"}
                and category not in {"required_input", "required_output", "required_skill"}
            )
        ):
            referenced_constraints.add(handle)

    # Artifact requirements selected through the step's constraint refs are
    # explicit semantic edges too.  This also upgrades legacy plans whose
    # steps referenced constraint identities but stored literal paths.
    for row in constraints:
        if str(row.get("handle") or "") in referenced_constraints:
            referenced_artifacts.update(
                _visible_handles(row, known_artifact_handles)
            )

    # Producer ownership is an explicit edge even when a legacy step omitted
    # its output handle list.
    if selected_index >= 0:
        selected_stable_id = str(raw_steps[selected_index].get("step_id") or "")
        for row, raw in zip(outputs, raw_outputs):
            if str(raw.get("producer_step_id") or "") == selected_stable_id:
                referenced_artifacts.add(str(row.get("handle") or ""))

    has_explicit_edges = bool(referenced_constraints or referenced_artifacts)
    policy = "current-step-closure-v2"
    if current and not has_explicit_edges:
        # Read-only compatibility for plans created before semantic references
        # were mandatory.  New PlanSpec validation rejects this shape.
        referenced_constraints = set(known_constraint_handles)
        referenced_artifacts = set(known_artifact_handles)
        policy = "legacy-unbound-step-full-closure-v1"

    selected_constraints = [
        deepcopy(dict(row)) for row in constraints
        if str(row.get("handle") or "") in referenced_constraints
    ]
    selected_inputs = [
        deepcopy(dict(row)) for row in inputs
        if str(row.get("handle") or "") in referenced_artifacts
    ]
    selected_outputs = [
        deepcopy(dict(row)) for row in outputs
        if str(row.get("handle") or "") in referenced_artifacts
    ]
    excluded: Dict[str, str] = {}
    included = referenced_constraints | referenced_artifacts
    for handle in sorted(known_constraint_handles | known_artifact_handles):
        if handle not in included:
            excluded[handle] = f"outside_{node_type}_current_step_dependency_closure"
    return (
        selected_constraints,
        selected_inputs,
        selected_outputs,
        current,
        dependency_contracts,
        excluded,
        policy,
    )


def _evidence_rows(
    evidence: Sequence[Mapping[str, Any]],
    handle_map: SemanticHandleMap,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    user_index = 0
    generic_index = 0
    for index, raw in enumerate(evidence, start=1):
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or raw.get("type") or "evidence")
        if kind == "user_message":
            user_index += 1
            handle = f"U{user_index}"
        else:
            generic_index += 1
            handle = f"E{generic_index}"
        stable_id = str(raw.get("evidence_id") or raw.get("id") or raw.get("ref") or f"evidence:{index}")
        handle_map.add(SemanticHandleBinding(
            handle,
            "evidence",
            stable_id,
            allowed_usage=("cite", "diagnose", "review"),
            metadata={
                "offset": raw.get("offset"),
                "truncated": bool(raw.get("truncated", False)),
                "kind": kind,
            },
        ))
        excerpt = raw.get("excerpt") or raw.get("text") or raw.get("summary") or ""
        rows.append({
            "handle": handle,
            "kind": kind,
            "excerpt": str(excerpt),
            "truncated": bool(raw.get("truncated", False)),
        })
    return rows


def _normalize_hash(value: str) -> str:
    text = str(value or "")
    return text if not text or text.startswith("sha256:") else f"sha256:{text}"


def build_node_context(
    *,
    node_type: str,
    task_spec: Mapping[str, Any],
    plan_state: Optional[Mapping[str, Any]] = None,
    state_generation: int = 0,
    current_step_id: str = "",
    runtime: Optional[Mapping[str, Any]] = None,
    feedback: Optional[Mapping[str, Any]] = None,
    evidence: Sequence[Mapping[str, Any]] = (),
    violations: Sequence[Mapping[str, Any]] = (),
    allowed_operations: Sequence[str] = (),
    approval_ref: str = "",
    approval_applicability: str = "",
    execution_epoch: Any = "",
    attempt_id: str = "",
    failure_fingerprint: str = "",
    violation_set_identity: str = "",
    action_identity: str = "",
    evidence_identity: str = "",
    touched_entity_versions: Optional[Mapping[str, str]] = None,
    trace_enabled: bool = False,
    max_payload_chars: Optional[int] = None,
    invocation_id: str = "",
    candidate_scope_id: str = "",
) -> NodeContext:
    """Build the three-object invocation protocol from authoritative state.

    No semantic list is shortened to satisfy ``max_payload_chars``.  If the
    complete required view does not fit, the call fails explicitly.
    """
    normalized_node = str(node_type or "").strip().lower()
    if normalized_node not in NODE_STALENESS_DOMAINS:
        raise NodeContextError(f"NODE_TYPE_UNSUPPORTED: {node_type!r}")
    plan = dict(plan_state or {})
    handles = SemanticHandleMap()
    used: Dict[str, int] = {}
    inputs, outputs = _semantic_artifact_rows(task_spec, handles, used)
    constraints = _constraint_rows(task_spec, handles, used)
    steps, step_handles = _step_rows(plan, handles, used)
    _project_step_semantic_references(steps, handles)
    evidence_rows = _evidence_rows(evidence, handles)

    selected_step: Dict[str, Any] = {}
    dependency_contracts: List[Dict[str, Any]] = []
    excluded_handles: Dict[str, str] = {}
    selection_policy = "required-task-semantics-v2"
    if normalized_node in {"generate", "recovery", "supervisor"}:
        raw_steps = [
            dict(item) for item in plan.get("steps", []) or []
            if isinstance(item, Mapping)
        ]
        raw_outputs = [
            dict(item) for item in task_spec.get("output_contracts", []) or []
            if isinstance(item, Mapping)
        ]
        (
            constraints,
            inputs,
            outputs,
            selected_step,
            dependency_contracts,
            excluded_handles,
            selection_policy,
        ) = _select_current_step_closure(
            node_type=normalized_node,
            raw_steps=raw_steps,
            step_rows=steps,
            current_step_id=current_step_id,
            constraints=constraints,
            inputs=inputs,
            outputs=outputs,
            raw_outputs=raw_outputs,
        )

    goal_view: Dict[str, Any] = {"goal": str(task_spec.get("goal") or "")}
    _project_step_semantic_references([goal_view], handles)
    task_view: Dict[str, Any] = {
        "goal": str(goal_view.get("goal") or ""),
        "constraints": constraints,
        "inputs": inputs,
        "outputs": outputs,
    }
    for key in ("confirmed_decisions", "unresolved_questions"):
        values = task_spec.get(key, []) or []
        if values:
            semantic_values = {key: _without_control_metadata(values)}
            _project_step_semantic_references([semantic_values], handles)
            task_view[key] = semantic_values[key]

    payload: Dict[str, Any] = {"task": task_view}
    if normalized_node in {"planner_repair", "plan_review"}:
        payload["plan"] = {"steps": steps}
    if normalized_node in {"generate", "recovery", "supervisor"}:
        payload["current_step"] = selected_step
        if dependency_contracts:
            payload["dependency_contracts"] = dependency_contracts
    if runtime:
        payload["runtime"] = _without_control_metadata(runtime)
    if feedback:
        payload["feedback"] = _without_control_metadata(feedback)
    if violations:
        visible_violations: List[Dict[str, Any]] = []
        for index, raw in enumerate(violations, start=1):
            row = _without_control_metadata(raw)
            row.pop("violation_id", None)
            row.setdefault("violation_handle", f"V{index}")
            visible_violations.append(row)
        payload["violations"] = visible_violations
    if evidence_rows:
        payload["evidence"] = evidence_rows
    if allowed_operations:
        payload["allowed_operations"] = [str(value) for value in allowed_operations]

    llm_payload = LLMPayload(payload)
    payload_chars = len(llm_payload.to_json())
    if max_payload_chars is not None and payload_chars > int(max_payload_chars):
        report = payload_observability_report(payload)
        raise ContextCapacityExceeded(
            "CONTEXT_CAPACITY_EXCEEDED: "
            f"required payload is {payload_chars} characters, capacity is {int(max_payload_chars)}; "
            f"top_sections={report['top_sections']}"
        )

    task_sem_hash = task_semantic_hash(task_spec)
    task_rec_hash = task_record_hash(task_spec)
    plan_sem_hash = _normalize_hash(semantic_plan_hash(plan)) if plan else ""
    plan_rec_hash = plan_record_hash(plan) if plan else ""
    binding = ControllerBinding(
        invocation_id=invocation_id or f"inv-{uuid.uuid4()}",
        node_type=normalized_node,
        staleness_domain=NODE_STALENESS_DOMAINS[normalized_node],
        state_generation=max(0, int(state_generation or 0)),
        task_semantic_hash=task_sem_hash,
        task_record_hash=task_rec_hash,
        plan_semantic_hash=plan_sem_hash,
        plan_record_hash=plan_rec_hash,
        approval_ref=str(approval_ref or ""),
        approval_applicability=str(approval_applicability or ""),
        step_id=str(current_step_id or ""),
        execution_epoch=str(execution_epoch if execution_epoch is not None else ""),
        attempt_id=str(attempt_id or ""),
        failure_fingerprint=str(failure_fingerprint or ""),
        violation_set_identity=str(
            violation_set_identity
            or (_sha256([_without_control_metadata(item) for item in violations]) if violations else "")
        ),
        action_identity=str(action_identity or ""),
        evidence_identity=str(evidence_identity or ""),
        candidate_scope_id=(
            str(candidate_scope_id or new_candidate_scope_id())
            if normalized_node in {"planner", "planner_repair"}
            else ""
        ),
        touched_entity_versions=dict(touched_entity_versions or {}),
        semantic_handle_map=handles.to_dict(),
    )

    trace: Optional[ProjectionTrace] = None
    if trace_enabled:
        constraint_handles = tuple(row["handle"] for row in constraints)
        artifact_handles = tuple(row["handle"] for row in [*inputs, *outputs])
        projection_identity = {
            "projection_type": f"{normalized_node}_task_view",
            "projection_version": f"{normalized_node}-v2",
            "selection_policy": selection_policy,
            "task_semantic_hash": task_sem_hash,
            "plan_semantic_hash": plan_sem_hash,
            "included_constraint_handles": constraint_handles,
            "included_artifact_handles": artifact_handles,
            "semantic_payload": payload,
        }
        trace = ProjectionTrace(
            projection_type=f"{normalized_node}_task_view",
            projection_version=f"{normalized_node}-v2",
            selection_policy=str(projection_identity["selection_policy"]),
            included_constraint_handles=constraint_handles,
            included_artifact_handles=artifact_handles,
            excluded_handles_with_reason=excluded_handles,
            projection_semantic_hash=_sha256(projection_identity),
            node_context_hash=_sha256({
                "projection": projection_identity,
                "binding": binding.to_dict(),
            }),
        )
    return NodeContext(binding=binding, llm_payload=llm_payload, projection_trace=trace)


@dataclass(frozen=True)
class StalenessDecision:
    usable: bool
    code: str
    changed_preconditions: Tuple[str, ...] = ()
    ignored_record_changes: Tuple[str, ...] = ()
    stale_signature: str = ""


_NODE_RELEVANT_PRECONDITIONS = {
    "consultant": ("task_semantic_hash", "touched_entity_versions"),
    "planner": ("task_semantic_hash", "plan_semantic_hash"),
    "planner_repair": (
        "task_semantic_hash", "plan_semantic_hash", "violation_set_identity",
    ),
    "generate": (
        "task_semantic_hash", "plan_semantic_hash", "approval_applicability",
        "approval_ref", "step_id", "execution_epoch",
    ),
    "recovery": (
        "task_semantic_hash", "plan_semantic_hash", "failure_fingerprint", "attempt_id",
    ),
    "supervisor": (
        "step_id", "action_identity", "attempt_id", "evidence_identity", "task_semantic_hash",
    ),
    "plan_review": ("task_semantic_hash", "plan_semantic_hash"),
}


def evaluate_node_staleness(
    binding: ControllerBinding,
    current: Mapping[str, Any],
) -> StalenessDecision:
    """Compare only the correctness domain of the bound node invocation."""
    relevant = _NODE_RELEVANT_PRECONDITIONS[binding.node_type]
    changed: List[str] = []
    for field_name in relevant:
        before = getattr(binding, field_name)
        after = current.get(field_name, before)
        if field_name == "touched_entity_versions":
            after_map = dict(after or {})
            for entity, version in dict(before or {}).items():
                if str(after_map.get(entity, version)) != str(version):
                    changed.append(f"touched_entity_versions.{entity}")
        elif str(after if after is not None else "") != str(before if before is not None else ""):
            changed.append(field_name)

    ignored = []
    for field_name in ("state_generation", "task_record_hash", "plan_record_hash"):
        before = getattr(binding, field_name)
        if field_name in current and str(current.get(field_name)) != str(before):
            ignored.append(field_name)
    if not changed:
        return StalenessDecision(
            usable=True,
            code="REVALIDATE_ON_CURRENT_RECORD" if ignored else "CURRENT",
            ignored_record_changes=tuple(sorted(ignored)),
        )
    code = {
        "consultant": "STALE_TASK_SEMANTICS",
        "planner": "STALE_TASK_SEMANTICS",
        "planner_repair": "STALE_PLAN_REPAIR_BASE",
        "generate": "STALE_GENERATE_CONTEXT",
        "recovery": "STALE_RECOVERY_CONTEXT",
        "supervisor": "STALE_SUPERVISOR_CONTEXT",
        "plan_review": "STALE_REVIEW_CONTEXT",
    }[binding.node_type]
    signature_payload = {
        "node_type": binding.node_type,
        "domain": binding.staleness_domain,
        "changed": sorted(changed),
        "before": {name: getattr(binding, name.split(".", 1)[0]) for name in relevant},
        "after": {name: current.get(name) for name in relevant if name in current},
        "reason": code,
    }
    return StalenessDecision(
        usable=False,
        code=code,
        changed_preconditions=tuple(sorted(changed)),
        ignored_record_changes=tuple(sorted(ignored)),
        stale_signature=_sha256(signature_payload),
    )


@dataclass
class _InvocationEntry:
    binding: ControllerBinding
    stale_signature: str
    status: str = "active"


class InvocationRegistry:
    """Bounded response registry with duplicate/late and stale deduplication."""

    def __init__(self, *, max_entries: int = 512, max_stale_signatures: int = 512) -> None:
        if max_entries < 1 or max_stale_signatures < 1:
            raise ValueError("registry bounds must be positive")
        self.max_entries = int(max_entries)
        self.max_stale_signatures = int(max_stale_signatures)
        self._entries: "OrderedDict[str, _InvocationEntry]" = OrderedDict()
        self._active_signatures: Dict[str, str] = {}
        self._stale_signatures: "OrderedDict[str, None]" = OrderedDict()

    def register(self, binding: ControllerBinding, *, stale_signature: str = "") -> None:
        invocation_id = binding.invocation_id
        if invocation_id in self._entries:
            raise InvocationProtocolError(f"INVOCATION_DUPLICATE: {invocation_id}")
        if stale_signature:
            active = self._active_signatures.get(stale_signature)
            if active:
                raise InvocationProtocolError(
                    f"EQUIVALENT_INVOCATION_ALREADY_ACTIVE: {stale_signature} ({active})"
                )
            if stale_signature in self._stale_signatures:
                raise InvocationProtocolError(f"STALE_SIGNATURE_ALREADY_HANDLED: {stale_signature}")
            self._active_signatures[stale_signature] = invocation_id
        self._entries[invocation_id] = _InvocationEntry(binding, stale_signature)
        self._entries.move_to_end(invocation_id)
        self._trim_entries()

    def binding_for(self, invocation_id: str) -> ControllerBinding:
        entry = self._entries.get(str(invocation_id or ""))
        if entry is None:
            raise InvocationProtocolError(f"INVOCATION_UNKNOWN: {invocation_id}")
        if entry.status != "active":
            raise InvocationProtocolError(
                f"INVOCATION_NOT_ACTIVE: {invocation_id} ({entry.status})"
            )
        return entry.binding

    def consume(self, invocation_id: str) -> ControllerBinding:
        binding = self.binding_for(invocation_id)
        entry = self._entries[invocation_id]
        entry.status = "consumed"
        if entry.stale_signature:
            self._active_signatures.pop(entry.stale_signature, None)
        self._entries.move_to_end(invocation_id)
        return binding

    def terminate(self, invocation_id: str, *, status: str = "failed") -> None:
        entry = self._entries.get(str(invocation_id or ""))
        if entry is None:
            raise InvocationProtocolError(f"INVOCATION_UNKNOWN: {invocation_id}")
        if entry.status != "active":
            raise InvocationProtocolError(
                f"INVOCATION_NOT_ACTIVE: {invocation_id} ({entry.status})"
            )
        entry.status = str(status or "failed")
        if entry.stale_signature:
            self._active_signatures.pop(entry.stale_signature, None)
        self._entries.move_to_end(invocation_id)

    def mark_stale_signature_handled(self, signature: str) -> bool:
        key = str(signature or "")
        if not key:
            return False
        if key in self._stale_signatures:
            self._stale_signatures.move_to_end(key)
            return False
        self._stale_signatures[key] = None
        while len(self._stale_signatures) > self.max_stale_signatures:
            self._stale_signatures.popitem(last=False)
        return True

    def _trim_entries(self) -> None:
        while len(self._entries) > self.max_entries:
            removable = next(
                (key for key, value in self._entries.items() if value.status != "active"),
                None,
            )
            if removable is None:
                # Never evict an active binding: doing so would make a valid
                # response unverifiable.  The caller must apply backpressure.
                newest = next(reversed(self._entries))
                entry = self._entries.pop(newest)
                if entry.stale_signature:
                    self._active_signatures.pop(entry.stale_signature, None)
                raise InvocationProtocolError("INVOCATION_REGISTRY_CAPACITY_EXCEEDED")
            self._entries.pop(removable)

    @property
    def size(self) -> int:
        return len(self._entries)


def payload_observability_report(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Phase-0 metrics for prompt size and control-metadata leakage."""
    section_chars = {
        str(key): len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
        for key, value in payload.items()
    }
    top_sections = sorted(section_chars.items(), key=lambda item: (-item[1], item[0]))
    serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    counts = {"constraints": 0, "inputs": 0, "outputs": 0, "evidence": 0}
    task = payload.get("task") if isinstance(payload.get("task"), Mapping) else {}
    for key in ("constraints", "inputs", "outputs"):
        value = task.get(key, []) if isinstance(task, Mapping) else []
        counts[key] = len(value) if isinstance(value, list) else 0
    counts["evidence"] = len(payload.get("evidence", []) or [])
    return {
        "total_chars": len(serialised),
        "section_chars": section_chars,
        "top_sections": top_sections,
        "included_counts": counts,
        "control_metadata_leaks": control_metadata_leaks(payload),
    }


def resolve_response_handles(
    binding: ControllerBinding,
    handles: Iterable[str],
    *,
    object_type: str = "",
    direction: str = "",
    usage: str = "",
) -> List[Dict[str, Any]]:
    mapping = SemanticHandleMap(
        SemanticHandleBinding(
            semantic_handle=str(item.get("semantic_handle") or key),
            object_type=str(item.get("object_type") or ""),
            stable_identity=str(item.get("stable_identity") or ""),
            direction=str(item.get("direction") or ""),
            allowed_usage=tuple(item.get("allowed_usage") or ()),
            metadata=dict(item.get("metadata") or {}),
        )
        for key, item in binding.semantic_handle_map.items()
        if isinstance(item, Mapping)
    )
    return [
        mapping.resolve(
            str(handle),
            object_type=object_type,
            direction=direction,
            usage=usage,
        ).to_dict()
        for handle in handles
    ]


def resolve_plan_proposal_handles(
    value: Mapping[str, Any],
    binding: ControllerBinding,
) -> Dict[str, Any]:
    """Resolve declared plan reference fields without touching free text."""
    proposal = deepcopy(dict(value or {}))
    handle_map = SemanticHandleMap(
        SemanticHandleBinding(
            semantic_handle=str(item.get("semantic_handle") or key),
            object_type=str(item.get("object_type") or ""),
            stable_identity=str(item.get("stable_identity") or ""),
            direction=str(item.get("direction") or ""),
            allowed_usage=tuple(item.get("allowed_usage") or ()),
            metadata=dict(item.get("metadata") or {}),
        )
        for key, item in binding.semantic_handle_map.items()
        if isinstance(item, Mapping)
    )

    def resolve_list(raw: Any, *, object_type: str = "") -> Any:
        """Resolve identity only; the containing Plan field owns step usage."""
        if not isinstance(raw, list):
            return raw
        resolved: List[Any] = []
        for item in raw:
            text = str(item or "")
            if text in handle_map:
                resolved.append(handle_map.resolve(
                    text,
                    object_type=object_type,
                ).stable_identity)
            else:
                resolved.append(deepcopy(item))
        return resolved

    steps = proposal.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, MutableMapping):
                continue
            step["semantic_constraint_ids"] = resolve_list(
                step.get("semantic_constraint_ids"), object_type="constraint",
            )
            step["inputs"] = resolve_list(
                step.get("inputs"), object_type="artifact",
            )
            step["outputs"] = resolve_list(
                step.get("outputs"), object_type="artifact",
            )
    materializations = proposal.get("task_output_materializations")
    if isinstance(materializations, list):
        for row in materializations:
            if not isinstance(row, MutableMapping):
                continue
            output_ref = str(row.get("output_ref") or "")
            if output_ref in handle_map:
                row["output_ref"] = handle_map.resolve(
                    output_ref,
                    object_type="artifact",
                    direction="output",
                ).stable_identity
    replacements = proposal.get("replace_steps")
    if isinstance(replacements, list):
        for replacement in replacements:
            if not isinstance(replacement, MutableMapping):
                continue
            step_handle = str(replacement.get("step_handle") or "")
            if step_handle:
                replacement["step_id"] = handle_map.resolve(
                    step_handle, object_type="plan_step",
                ).stable_identity
                replacement.pop("step_handle", None)
            new_step = replacement.get("new_step")
            if isinstance(new_step, MutableMapping):
                new_step["semantic_constraint_ids"] = resolve_list(
                    new_step.get("semantic_constraint_ids"), object_type="constraint",
                )
                new_step["inputs"] = resolve_list(
                    new_step.get("inputs"), object_type="artifact",
                )
                new_step["outputs"] = resolve_list(
                    new_step.get("outputs"), object_type="artifact",
                )
    return proposal


def parse_managed_placeholder(value: str) -> Tuple[str, str]:
    match = _PLACEHOLDER_RE.fullmatch(str(value or ""))
    if not match:
        raise SemanticHandleError(f"MANAGED_PLACEHOLDER_INVALID: {value!r}")
    direction, name = match.groups()
    return direction, f"{direction}.{name}"

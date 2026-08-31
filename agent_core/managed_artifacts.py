"""Deterministic managed-artifact contracts and action materialisation.

The model selects semantic handles.  This module is the only boundary that
turns those handles into authoritative paths.  Typed argv/env fields are the
validation authority; raw source is preserved and may only produce diagnostic
signals because this module deliberately does not parse shell semantics.
"""

from __future__ import annotations

import os
import re
import hashlib
import json
import gzip
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MANAGED_ARTIFACT_SCHEMA_VERSION = 2
MANAGED_EXACT = "managed_exact"
MODEL_SELECTABLE = "model_selectable"
PLANNER_MATERIALIZED = "planner_materialized"
PHYSICAL_HARD = "HARD"
PHYSICAL_CONTRACT_BOUND = "CONTRACT_BOUND"
PHYSICAL_IMPLEMENTATION_DETAIL = "IMPLEMENTATION_DETAIL"
_PLACEHOLDER_RE = re.compile(r"^\{(input|output):([A-Za-z][A-Za-z0-9_.-]*)\}$")
_OPERATION_PLACEHOLDER_RE = re.compile(r"^\{operation:([A-Za-z][A-Za-z0-9_.-]*)\}$")
_RUNTIME_PLACEHOLDER_RE = re.compile(r"\{runtime:[A-Za-z][A-Za-z0-9_.-]*\}")
_MANAGED_PLACEHOLDER_RE = re.compile(
    r"\{(?:input|output|operation):[A-Za-z][A-Za-z0-9_.-]*\}"
)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ManagedArtifactError(ValueError):
    code = "MANAGED_ARTIFACT_ERROR"


@dataclass(frozen=True)
class ArtifactViolation:
    code: str
    owner: str
    route: str
    message: str
    artifact_handle: str = ""
    expected: str = ""
    proposed: str = ""
    required_filename: str = ""
    field_path: str = ""
    compatibility_hit: bool = False
    subowner: str = ""
    failure_stage: str = ""
    repair_hint: Mapping[str, str] = field(default_factory=dict)
    offending_placeholders: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        value = {
            key: value
            for key, value in {
                "code": self.code,
                "owner": self.owner,
                "route": self.route,
                "message": self.message,
                "artifact_handle": self.artifact_handle,
                "expected": self.expected,
                "proposed": self.proposed,
                "required_filename": self.required_filename,
                "field_path": self.field_path,
                "compatibility_hit": self.compatibility_hit,
                "subowner": self.subowner,
                "failure_stage": self.failure_stage,
            }.items()
            if value not in (None, "", False)
        }
        if self.repair_hint:
            value["repair_hint"] = dict(self.repair_hint)
        if self.offending_placeholders:
            value["offending_placeholders"] = list(self.offending_placeholders)
        return value


@dataclass(frozen=True)
class ManagedArtifactBinding:
    artifact_id: str
    semantic_handle: str
    direction: str
    format: str
    path_policy: str
    canonical_path: str
    required_basename: str
    producer_owner: str = ""
    producer_step_id: str = ""
    relocatable: bool = False
    validation_contract_id: str = ""
    required: bool = True
    aliases: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    artifact_kind: str = ""
    source_authority: str = "task_spec"
    snapshot_identity: str = ""
    runtime_available: bool = True
    constraint_origin: str = PHYSICAL_IMPLEMENTATION_DETAIL
    accepted_representations: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.direction not in {"input", "output"}:
            raise ManagedArtifactError(
                f"MANAGED_ARTIFACT_DIRECTION_INVALID: {self.semantic_handle}={self.direction!r}"
            )
        expected_prefix = self.direction + "."
        if not self.semantic_handle.startswith(expected_prefix):
            raise ManagedArtifactError(
                f"MANAGED_ARTIFACT_HANDLE_DIRECTION_MISMATCH: {self.semantic_handle}"
            )
        if self.path_policy == MANAGED_EXACT and not self.canonical_path:
            raise ManagedArtifactError(
                f"MANAGED_ARTIFACT_PATH_MISSING: {self.semantic_handle}"
            )

    @property
    def placeholder(self) -> str:
        return "{" + self.direction + ":" + self.semantic_handle.split(".", 1)[1] + "}"

    def to_dict(self, *, include_control_path: bool = True) -> Dict[str, Any]:
        value = {
            "artifact_id": self.artifact_id,
            "semantic_handle": self.semantic_handle,
            "direction": self.direction,
            "format": self.format,
            "path_policy": self.path_policy,
            "required_basename": self.required_basename,
            "producer_owner": self.producer_owner,
            "producer_step_id": self.producer_step_id,
            "relocatable": self.relocatable,
            "validation_contract_id": self.validation_contract_id,
            "required": self.required,
            "aliases": list(self.aliases),
            "metadata": deepcopy(dict(self.metadata)),
            "artifact_kind": self.artifact_kind,
            "source_authority": self.source_authority,
            "snapshot_identity": self.snapshot_identity,
            "runtime_available": self.runtime_available,
            "constraint_origin": self.constraint_origin,
            "accepted_representations": list(self.accepted_representations),
        }
        if include_control_path:
            value["canonical_path"] = self.canonical_path
        else:
            value["placeholder"] = self.placeholder
        return value


def _slug(value: Any, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_.-]+", "_", str(value or "").strip().lower()).strip("_.-")
    return text or fallback


def _canonical_path(raw: Mapping[str, Any]) -> str:
    value = str(
        raw.get("canonical_path")
        or raw.get("resolved_path")
        or raw.get("path")
        or raw.get("filename")
        or ""
    ).strip()
    if not value:
        return ""
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except OSError:
        return os.path.abspath(os.path.expanduser(value))


def physical_constraint_origin(
    raw: Mapping[str, Any],
    *,
    materialization_origin: str = "",
    source_authority: str = "",
) -> str:
    """Classify physical path strictness from Controller-owned provenance.

    Logical requiredness does not make a Planner-selected basename a hard
    physical contract.  Only an explicit user path or a typed tool/workflow
    contract does that; approved plan-local paths remain implementation
    expectations unless their durable metadata says otherwise.
    """
    explicit = str(raw.get("constraint_origin") or "").strip().upper()
    if explicit in {
        PHYSICAL_HARD,
        PHYSICAL_CONTRACT_BOUND,
        PHYSICAL_IMPLEMENTATION_DETAIL,
    }:
        return explicit
    origin = str(materialization_origin or raw.get("materialization_origin") or "").strip().lower()
    declared_policy = str(
        raw.get("declared_path_policy") or raw.get("path_policy") or ""
    ).strip().lower()
    authority = str(source_authority or raw.get("source_authority") or "").strip().lower()
    source_type = str(
        raw.get("source_type") or raw.get("source") or raw.get("source_authority") or ""
    ).strip().lower()
    if origin == "planner" or declared_policy == PLANNER_MATERIALIZED:
        return PHYSICAL_IMPLEMENTATION_DETAIL
    if authority == "approved_plan_snapshot":
        return PHYSICAL_IMPLEMENTATION_DETAIL
    if source_type.startswith(("skill", "workflow", "tool_contract", "contract")):
        return PHYSICAL_CONTRACT_BOUND
    has_explicit_path = any(
        str(raw.get(key) or "").strip()
        for key in ("canonical_path", "path", "filename", "required_basename")
    )
    if source_type in {"user", "user_explicit", "task_user"} and has_explicit_path:
        return PHYSICAL_HARD
    return PHYSICAL_IMPLEMENTATION_DETAIL


def _accepted_representations(raw: Mapping[str, Any]) -> Tuple[str, ...]:
    metadata = dict(raw.get("metadata") or {})
    values = (
        raw.get("accepted_representations")
        or raw.get("downstream_accepted_representations")
        or metadata.get("accepted_representations")
        or metadata.get("downstream_accepted_representations")
        or []
    )
    if isinstance(values, str):
        values = [values]
    return tuple(dict.fromkeys(
        str(value).strip().lower()
        for value in values or []
        if str(value or "").strip()
    ))


def _binding_from_row(
    raw: Mapping[str, Any],
    *,
    direction: str,
    index: int,
) -> ManagedArtifactBinding:
    identity_key = "input_id" if direction == "input" else "output_id"
    role = raw.get("role") or raw.get("name") or raw.get(identity_key) or f"{direction}_{index}"
    semantic_handle = str(raw.get("semantic_handle") or f"{direction}.{_slug(role, f'{direction}_{index}')}")
    path = _canonical_path(raw)
    path_policy = str(raw.get("path_policy") or (MANAGED_EXACT if path else MODEL_SELECTABLE))
    artifact_id = str(raw.get("artifact_id") or raw.get(identity_key) or semantic_handle)
    required_basename = str(raw.get("required_basename") or (Path(path).name if path else ""))
    validator = str(
        raw.get("validation_contract_id")
        or raw.get("validator")
        or (f"{raw.get('format')}-output-v1" if raw.get("format") and direction == "output" else "")
    )
    aliases = tuple(
        str(value)
        for value in raw.get("aliases", []) or []
        if str(value or "").strip()
    )
    return ManagedArtifactBinding(
        artifact_id=artifact_id,
        semantic_handle=semantic_handle,
        direction=direction,
        format=str(raw.get("format") or raw.get("type") or ""),
        path_policy=path_policy,
        canonical_path=path,
        required_basename=required_basename,
        producer_owner=str(raw.get("producer_owner") or raw.get("producer_hint") or ""),
        producer_step_id=str(raw.get("producer_step_id") or ""),
        relocatable=bool(raw.get("relocatable", False)),
        validation_contract_id=validator,
        required=bool(raw.get("required", True)),
        aliases=aliases,
        metadata={
            "source_index": index,
            "role": str(role),
            "output_id": str(raw.get("output_id") or "") if direction == "output" else "",
            "source_type": str(raw.get("source_type") or raw.get("source") or ""),
            "source_ref": str(raw.get("source_ref") or raw.get("source_id") or ""),
            "declared_path_policy": str(raw.get("declared_path_policy") or raw.get("path_policy") or ""),
            "materialization_origin": str(raw.get("materialization_origin") or ""),
            "user_visible": bool(raw.get("user_visible", False)),
        },
        artifact_kind=str(raw.get("artifact_kind") or raw.get("object_kind") or ""),
        source_authority=str(raw.get("source_authority") or "task_spec"),
        snapshot_identity=str(raw.get("snapshot_identity") or ""),
        runtime_available=bool(raw.get("runtime_available", True)),
        constraint_origin=physical_constraint_origin(raw),
        accepted_representations=_accepted_representations(raw),
    )


_STEP_SUCCESS = {"done", "completed", "passed", "skipped"}
_ARTIFACT_VALID = {"valid", "passed", "pass"}


def _runtime_plan_local_available(
    plan_state: Mapping[str, Any],
    artifact_id: str,
    producer_step_id: str,
) -> bool:
    producer = next(
        (
            dict(step)
            for step in plan_state.get("steps", []) or []
            if isinstance(step, Mapping)
            and str(step.get("step_id") or "") == str(producer_step_id or "")
        ),
        {},
    )
    if str(producer.get("status") or "").strip().lower() not in _STEP_SUCCESS:
        return False
    provenance = dict(plan_state.get("provenance_state") or {})
    artifacts = dict(provenance.get("artifacts") or {})
    heads = dict(provenance.get("artifact_heads") or {})
    for head in heads.values():
        record = dict(artifacts.get(str(head)) or {})
        if str(record.get("logical_artifact_id") or "") != str(artifact_id):
            continue
        if (
            str(record.get("status") or "").strip().lower() == "valid"
            and str(record.get("validation_status") or "").strip().lower()
            in _ARTIFACT_VALID
        ):
            return True
    return False


def _validated_runtime_realization(
    plan_state: Mapping[str, Any],
    logical_artifact_id: str,
) -> str:
    """Return the validated physical head for one stable logical artifact."""
    provenance = dict(plan_state.get("provenance_state") or {})
    artifacts = dict(provenance.get("artifacts") or {})
    for head in dict(provenance.get("artifact_heads") or {}).values():
        record = dict(artifacts.get(str(head)) or {})
        if str(record.get("logical_artifact_id") or "") != str(logical_artifact_id or ""):
            continue
        if (
            str(record.get("status") or "").strip().lower() == "valid"
            and str(record.get("validation_status") or "").strip().lower()
            in _ARTIFACT_VALID
            and str(record.get("path") or "").strip()
        ):
            return _canonical_path({"path": record.get("path")})
    return ""


def build_approved_visible_artifact_set(
    task_spec: Mapping[str, Any],
    *,
    plan_state: Optional[Mapping[str, Any]] = None,
    approved_plan_snapshot: Optional[Mapping[str, Any]] = None,
    current_step_id: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Compile the authoritative artifact visibility set for one invocation.

    Plans without plan-local artifacts retain the pre-v1.2.1 TaskSpec-wide
    binding behaviour.  Once a plan-local ledger exists, the immutable
    snapshot's typed usage edges scope both task and plan-local artifacts.
    """
    snapshot = dict(approved_plan_snapshot or {})
    plan_local = dict(
        snapshot.get("plan_local_artifacts")
        or snapshot.get("plan_artifacts")
        or {}
    )
    task_rows: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for direction, field_name, identity_key in (
        ("input", "inputs", "input_id"),
        ("output", "output_contracts", "output_id"),
    ):
        for raw in task_spec.get(field_name, []) or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            identity = str(
                row.get("artifact_id")
                or row.get(identity_key)
                or row.get("semantic_handle")
                or ""
            )
            for ref in (
                identity,
                row.get("artifact_id"),
                row.get(identity_key),
                row.get("semantic_handle"),
            ):
                if str(ref or ""):
                    task_rows[str(ref)] = (direction, row)

    # Compatibility: older approved plans have no plan-local ledger and keep
    # the original TaskSpec binding universe.
    has_typed_usage = bool(
        snapshot.get("resolved_artifact_refs")
        or snapshot.get("artifact_usage")
    )
    if not plan_local and not has_typed_usage:
        visible: Dict[str, Dict[str, Any]] = {}
        for identity, (direction, row) in task_rows.items():
            canonical_identity = str(
                row.get("artifact_id")
                or row.get("input_id" if direction == "input" else "output_id")
                or row.get("semantic_handle")
                or identity
            )
            visible[canonical_identity] = {
                "artifact_id": canonical_identity,
                "direction": direction,
                "source_authority": "task_spec",
                "row": deepcopy(row),
                "runtime_available": True,
                "aliases": sorted({
                    str(value)
                    for value in (
                        identity,
                        row.get("artifact_id"),
                        row.get("input_id"),
                        row.get("output_id"),
                        row.get("semantic_handle"),
                    )
                    if str(value or "")
                }),
            }
        return visible

    from agent_core.node_context import approved_current_step_artifact_usage

    usage = approved_current_step_artifact_usage(snapshot, current_step_id)
    visible = {}
    snapshot_identity = str(
        snapshot.get("plan_snapshot_id")
        or snapshot.get("plan_semantic_hash")
        or ""
    )
    for artifact_id, invocation in sorted(usage.items()):
        direction = str(invocation.get("direction") or "")
        if artifact_id in plan_local:
            ledger_entry = deepcopy(dict(plan_local[artifact_id] or {}))
            producer_step_id = str(invocation.get("producer_step_id") or "")
            runtime_available = (
                True
                if direction == "output"
                else _runtime_plan_local_available(
                    dict(plan_state or {}), artifact_id, producer_step_id
                )
            )
            visible[artifact_id] = {
                "artifact_id": artifact_id,
                "direction": direction,
                "source_authority": "approved_plan_snapshot",
                "snapshot_identity": snapshot_identity,
                "producer_step_id": producer_step_id,
                "row": ledger_entry,
                "runtime_available": runtime_available,
                "aliases": [artifact_id, f"{direction}.{artifact_id}"],
            }
            continue
        task_match = task_rows.get(artifact_id)
        if task_match is None:
            continue
        declared_direction, row = task_match
        if declared_direction != direction and not (
            declared_direction == "output" and direction == "input"
        ):
            continue
        canonical_identity = str(
            row.get("artifact_id")
            or row.get("input_id" if declared_direction == "input" else "output_id")
            or artifact_id
        )
        role = str(
            row.get("role")
            or row.get("name")
            or row.get("input_id" if declared_direction == "input" else "output_id")
            or canonical_identity
        )
        visible[canonical_identity] = {
            "artifact_id": canonical_identity,
            "direction": direction,
            "declared_direction": declared_direction,
            "source_authority": "task_spec",
            "snapshot_identity": snapshot_identity,
            "row": deepcopy(row),
            "runtime_available": True,
            "aliases": sorted({
                artifact_id,
                canonical_identity,
                str(row.get("semantic_handle") or ""),
                f"{direction}.{_slug(role, canonical_identity)}",
            } - {""}),
        }
    return visible


def managed_action_fingerprint(action: Mapping[str, Any]) -> str:
    """Hash normalized execution-relevant structured action fields."""
    normalized = {
        "type": str(action.get("type") or action.get("action_type") or "").strip().lower(),
        "argv": [str(item) for item in action.get("argv", []) or []],
        "env": {
            str(key): str(value)
            for key, value in sorted(dict(action.get("env") or {}).items())
        },
        "cwd": str(action.get("cwd") or ""),
        "outputs": [str(item) for item in action.get("outputs", []) or []],
    }
    return hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    ).hexdigest()


def build_managed_artifact_bindings(
    task_spec: Mapping[str, Any],
    *,
    plan_state: Optional[Mapping[str, Any]] = None,
    approved_plan_snapshot: Optional[Mapping[str, Any]] = None,
    current_step_id: str = "",
    visible_artifacts: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, ManagedArtifactBinding]:
    """Compile the approved current-step artifact set into managed bindings."""
    result: Dict[str, ManagedArtifactBinding] = {}
    visible = {
        str(key): deepcopy(dict(value))
        for key, value in dict(
            visible_artifacts
            or build_approved_visible_artifact_set(
                task_spec,
                plan_state=plan_state,
                approved_plan_snapshot=approved_plan_snapshot,
                current_step_id=current_step_id,
            )
        ).items()
        if isinstance(value, Mapping)
    }
    plan_local = dict(
        (approved_plan_snapshot or {}).get("plan_local_artifacts")
        or (approved_plan_snapshot or {}).get("plan_artifacts")
        or {}
    )
    scoped_to_approved_step = bool(
        plan_local
        or (approved_plan_snapshot or {}).get("resolved_artifact_refs")
        or (approved_plan_snapshot or {}).get("artifact_usage")
    )
    visible_by_alias: Dict[str, Dict[str, Any]] = {}
    for identity, row in visible.items():
        visible_by_alias[identity] = row
        for alias in row.get("aliases", []) or []:
            if str(alias or ""):
                visible_by_alias[str(alias)] = row
    output_root = ""
    for raw in task_spec.get("output_contracts", []) or []:
        if not isinstance(raw, Mapping) or str(raw.get("role") or "") != "output_dir":
            continue
        output_root = _canonical_path(raw)
        if output_root:
            break
    materializations: Dict[str, Mapping[str, Any]] = {}
    for row in (plan_state or {}).get("task_output_materializations", []) or []:
        if not isinstance(row, Mapping):
            continue
        for ref in (row.get("output_ref"), row.get("output_id")):
            if str(ref or ""):
                materializations[str(ref)] = row
    for declared_direction, field_name in (("input", "inputs"), ("output", "output_contracts")):
        for index, raw in enumerate(task_spec.get(field_name, []) or [], start=1):
            if not isinstance(raw, Mapping):
                continue
            if scoped_to_approved_step and not any(
                str(ref or "") in visible_by_alias
                for ref in (
                    raw.get("artifact_id"),
                    raw.get("input_id" if declared_direction == "input" else "output_id"),
                    raw.get("semantic_handle"),
                )
            ):
                continue
            binding_row = dict(raw)
            visible_row = next(
                (
                    visible_by_alias[str(ref)]
                    for ref in (
                        raw.get("artifact_id"),
                        raw.get("input_id" if declared_direction == "input" else "output_id"),
                        raw.get("semantic_handle"),
                    )
                    if str(ref or "") in visible_by_alias
                ),
                {},
            )
            direction = str(visible_row.get("direction") or declared_direction)
            if visible_row:
                binding_row.update({
                    "source_authority": str(
                        visible_row.get("source_authority") or "task_spec"
                    ),
                    "snapshot_identity": str(
                        visible_row.get("snapshot_identity") or ""
                    ),
                    "runtime_available": bool(
                        visible_row.get("runtime_available", True)
                    ),
                })
                if direction != declared_direction:
                    role = str(
                        binding_row.get("role")
                        or binding_row.get("name")
                        or binding_row.get("output_id")
                        or binding_row.get("artifact_id")
                        or f"artifact_{index}"
                    )
                    binding_row["semantic_handle"] = (
                        f"{direction}.{_slug(role, f'artifact_{index}')}"
                    )
            if declared_direction == "output" and not _canonical_path(binding_row):
                materialized = next((
                    materializations.get(str(ref))
                    for ref in (
                        binding_row.get("artifact_id"),
                        binding_row.get("output_id"),
                        binding_row.get("semantic_handle"),
                    )
                    if materializations.get(str(ref)) is not None
                ), None)
                if materialized is not None:
                    binding_row.update({
                        "canonical_path": str(materialized.get("canonical_path") or materialized.get("path") or ""),
                        "filename": Path(str(materialized.get("path") or "")).name,
                        "path_policy": MANAGED_EXACT,
                        "declared_path_policy": str(raw.get("path_policy") or ""),
                        "materialization_origin": str(materialized.get("origin") or ""),
                    })
            logical_identity = str(
                binding_row.get("artifact_id")
                or binding_row.get("input_id" if declared_direction == "input" else "output_id")
                or ""
            )
            if direction == "input":
                realized_path = _validated_runtime_realization(
                    dict(plan_state or {}), logical_identity
                )
                if realized_path:
                    binding_row["canonical_path"] = realized_path
                    binding_row["runtime_available"] = True
            binding = _binding_from_row(binding_row, direction=direction, index=index)
            if (
                direction == "output"
                and binding.path_policy == MANAGED_EXACT
                and binding.canonical_path
                and not Path(binding.canonical_path).is_absolute()
                and output_root
                and binding.semantic_handle != "output.output_dir"
            ):
                joined = str((Path(output_root) / binding.canonical_path).resolve(strict=False))
                binding = ManagedArtifactBinding(
                    **{
                        **binding.to_dict(include_control_path=True),
                        "canonical_path": joined,
                        "required_basename": Path(joined).name,
                        "aliases": tuple(binding.aliases),
                        "accepted_representations": tuple(binding.accepted_representations),
                    }
                )
            existing = result.get(binding.semantic_handle)
            if existing is not None and existing != binding:
                raise ManagedArtifactError(
                    f"MANAGED_ARTIFACT_HANDLE_AMBIGUOUS: {binding.semantic_handle}"
                )
            result[binding.semantic_handle] = binding

    for artifact_id, visible_row in sorted(visible.items()):
        if str(visible_row.get("source_authority") or "") != "approved_plan_snapshot":
            continue
        direction = str(visible_row.get("direction") or "")
        ledger_entry = dict(visible_row.get("row") or {})
        semantic_view = dict(ledger_entry.get("semantic_view") or {})
        canonical_path = _canonical_path(ledger_entry)
        if direction == "input":
            canonical_path = (
                _validated_runtime_realization(dict(plan_state or {}), artifact_id)
                or canonical_path
            )
        artifact_kind = str(ledger_entry.get("object_kind") or "")
        validation_contract_id = str(
            ledger_entry.get("validation_contract_id")
            or (
                "directory_exists"
                if artifact_kind == "directory"
                else "file_exists"
            )
        )
        binding = ManagedArtifactBinding(
            artifact_id=artifact_id,
            semantic_handle=f"{direction}.{artifact_id}",
            direction=direction,
            format=str(semantic_view.get("format") or ""),
            path_policy=MANAGED_EXACT,
            canonical_path=canonical_path,
            required_basename=Path(canonical_path).name if canonical_path else "",
            producer_owner="approved_plan",
            producer_step_id=str(visible_row.get("producer_step_id") or ""),
            relocatable=False,
            validation_contract_id=validation_contract_id,
            required=True,
            aliases=tuple(
                str(value)
                for value in visible_row.get("aliases", []) or []
                if str(value or "")
            ),
            metadata={
                "role": str(semantic_view.get("role") or artifact_id),
                "output_id": artifact_id if direction == "output" else "",
                "availability_origin": str(
                    ledger_entry.get("availability_origin") or ""
                ),
                "source_type": str(ledger_entry.get("source_type") or ""),
                "declared_path_policy": str(ledger_entry.get("path_policy") or MANAGED_EXACT),
            },
            artifact_kind=artifact_kind,
            source_authority="approved_plan_snapshot",
            snapshot_identity=str(visible_row.get("snapshot_identity") or ""),
            runtime_available=bool(visible_row.get("runtime_available", True)),
            constraint_origin=physical_constraint_origin(
                ledger_entry,
                source_authority="approved_plan_snapshot",
            ),
            accepted_representations=_accepted_representations({
                **ledger_entry,
                **semantic_view,
            }),
        )
        result[binding.semantic_handle] = binding
    return result


def managed_artifact_llm_view(
    bindings: Mapping[str, ManagedArtifactBinding],
) -> List[Dict[str, Any]]:
    """Return semantic contracts/placeholders without authoritative paths or UUIDs."""
    return [
        {
            key: value
            for key, value in binding.to_dict(include_control_path=False).items()
            if key not in {
                "artifact_id", "producer_step_id", "metadata", "aliases",
                "snapshot_identity", "source_authority", "constraint_origin",
                "accepted_representations",
            }
        }
        for _, binding in sorted(bindings.items())
    ]


def _parse_placeholder(value: str) -> Optional[Tuple[str, str]]:
    match = _PLACEHOLDER_RE.fullmatch(str(value or ""))
    if not match:
        return None
    direction, name = match.groups()
    return direction, f"{direction}.{name}"


def normalize_bash_positional_argv(argv: Sequence[Any]) -> List[str]:
    """Insert a Bash ``$0`` sentinel only when positional indexing requires it."""
    values = [str(item) for item in argv or []]
    if len(values) < 4 or Path(values[0]).name != "bash" or values[1] not in {"-c", "-lc"}:
        return values
    script = values[2]
    indexes = [int(item) for item in re.findall(r"(?<!\\)\$\{?([1-9][0-9]*)\}?", script)]
    if not indexes:
        return values
    # Every argument after SCRIPT is shifted once by Bash because the first
    # becomes $0.  If the highest referenced positional parameter is not
    # reachable, the caller omitted the sentinel.
    if max(indexes) >= len(values) - 3:
        return [*values[:3], "_", *values[3:]]
    return values


def _literal_mentions_path(value: str, path: str) -> bool:
    if not value or not path:
        return False
    return str(value) == path or str(value) == Path(path).name


def _violation(
    *,
    code: str,
    owner: str,
    route: str,
    message: str,
    binding: Optional[ManagedArtifactBinding] = None,
    expected: str = "",
    proposed: str = "",
    field_path: str = "",
    compatibility_hit: bool = False,
    subowner: str = "",
    failure_stage: str = "",
    repair_hint: Optional[Mapping[str, str]] = None,
    offending_placeholders: Sequence[str] = (),
) -> ArtifactViolation:
    return ArtifactViolation(
        code=code,
        owner=owner,
        route=route,
        message=message,
        artifact_handle=binding.semantic_handle if binding else "",
        expected=expected or (binding.canonical_path if binding else ""),
        proposed=proposed,
        required_filename=binding.required_basename if binding else "",
        field_path=field_path,
        compatibility_hit=compatibility_hit,
        subowner=subowner,
        failure_stage=failure_stage,
        repair_hint=dict(repair_hint or {}),
        offending_placeholders=tuple(str(item) for item in offending_placeholders if str(item)),
    )


def validate_plan_managed_artifacts(
    plan_state: Mapping[str, Any],
    bindings: Mapping[str, ManagedArtifactBinding],
) -> List[ArtifactViolation]:
    """Validate managed identity/path bindings without inferring step usage."""
    violations: List[ArtifactViolation] = []
    output_by_basename = {
        value.required_basename: value
        for value in bindings.values()
        if value.direction == "output" and value.required_basename
    }
    for step_index, step in enumerate(plan_state.get("steps", []) or []):
        if not isinstance(step, Mapping):
            continue
        handles = step.get("output_artifact_handles") or []
        if isinstance(handles, str):
            handles = [handles]
        for item_index, handle in enumerate(handles):
            binding = bindings.get(str(handle))
            if binding is None:
                violations.append(_violation(
                    code="PLAN_ARTIFACT_HANDLE_INVALID",
                    owner="planner",
                    route="planner",
                    message=f"Plan output handle {handle!r} is unknown.",
                    proposed=str(handle),
                    field_path=f"steps[{step_index}].output_artifact_handles[{item_index}]",
                ))
        for field_name in ("outputs", "bound_outputs", "artifacts"):
            raw_values = step.get(field_name, []) or []
            if isinstance(raw_values, (str, Mapping)):
                raw_values = [raw_values]
            for item_index, raw in enumerate(raw_values):
                if isinstance(raw, Mapping):
                    proposed_path = _canonical_path(raw)
                    proposed_handle = str(raw.get("semantic_handle") or raw.get("handle") or "")
                else:
                    proposed_path = _canonical_path({"path": raw})
                    proposed_handle = str(raw or "") if str(raw or "") in bindings else ""
                if proposed_handle:
                    binding = bindings.get(proposed_handle)
                    if binding is None:
                        violations.append(_violation(
                            code="PLAN_ARTIFACT_HANDLE_INVALID",
                            owner="planner",
                            route="planner",
                            message=f"Plan output handle {proposed_handle!r} is unknown.",
                            proposed=proposed_handle,
                            field_path=f"steps[{step_index}].{field_name}[{item_index}]",
                        ))
                    elif proposed_path and proposed_path != binding.canonical_path:
                        violations.append(_violation(
                            code="PLAN_OUTPUT_BINDING_MISMATCH",
                            owner="planner",
                            route="planner",
                            message=(
                                f"Plan redefines {binding.semantic_handle} as {proposed_path}; "
                                "it must reference the Task artifact handle without redefining its path."
                            ),
                            binding=binding,
                            proposed=proposed_path,
                            field_path=f"steps[{step_index}].{field_name}[{item_index}]",
                        ))
                elif proposed_path:
                    basename = Path(proposed_path).name
                    exact = next(
                        (item for item in bindings.values() if item.canonical_path == proposed_path),
                        None,
                    )
                    mismatch = output_by_basename.get(basename)
                    if exact is None and mismatch is None and any(
                        item.direction == "output" and item.required and item.producer_step_id in {"", str(step.get("step_id") or "")}
                        for item in bindings.values()
                    ):
                        # A literal plan output may be a new intermediate.  It
                        # is only a managed mismatch when the row declares the
                        # managed handle/artifact identity explicitly.
                        continue
    return violations


def materialize_managed_action(
    action: Mapping[str, Any],
    bindings: Mapping[str, ManagedArtifactBinding],
    *,
    allow_legacy_exact_literals: bool = False,
    operation_bindings: Optional[Mapping[str, str]] = None,
    approved_visible_artifacts: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[Dict[str, Any], List[ArtifactViolation]]:
    """Resolve typed placeholders in argv/env and validate declared handles.

    Embedded placeholders are rejected in typed fields.  Raw script/source
    text is not a machine-semantic field and is therefore diagnostic-only.  A
    path containing spaces or shell metacharacters remains one argv value.
    """
    proposal = deepcopy(dict(action or {}))
    materialized = deepcopy(proposal)
    script_protocol = str(proposal.get("script_adapter") or "") == "script_block_v1"
    controller_artifact_environment = [
        dict(item)
        for item in proposal.get("controller_artifact_environment", []) or []
        if isinstance(item, Mapping)
    ]
    script_env_by_placeholder = {
        str(item.get("placeholder") or ""): str(item.get("env_var") or "")
        for item in controller_artifact_environment
        if str(item.get("placeholder") or "")
        and _ENV_KEY_RE.fullmatch(str(item.get("env_var") or ""))
    }
    violations: List[ArtifactViolation] = []
    used_handles: List[str] = []
    compatibility_hits: List[Dict[str, str]] = []
    source_diagnostics: List[Dict[str, Any]] = []
    managed_operations = {
        str(key): str(value)
        for key, value in dict(operation_bindings or {}).items()
        if str(key) and str(value)
    }
    used_operations: List[str] = []
    visible_by_alias: Dict[str, Mapping[str, Any]] = {}
    for identity, row in dict(approved_visible_artifacts or {}).items():
        if not isinstance(row, Mapping):
            continue
        visible_by_alias[str(identity)] = row
        for alias in row.get("aliases", []) or []:
            if str(alias or ""):
                visible_by_alias[str(alias)] = row

    def missing_binding_violation(
        reference: str,
        direction: str,
        *,
        field_path: str,
        message: str,
    ) -> ArtifactViolation:
        visible = visible_by_alias.get(str(reference or ""))
        if visible is not None and str(visible.get("direction") or "") == direction:
            return _violation(
                code="PLAN_ARTIFACT_BINDING_PROJECTION_MISSING",
                owner="INTERNAL_PROTOCOL",
                subowner="controller_projection",
                route="internal_reproject",
                message=(
                    "Approved current-step artifact is absent from the managed "
                    f"binding projection: {reference!r}."
                ),
                proposed=reference,
                field_path=field_path,
                failure_stage="pre_action_contract",
            )
        return _violation(
            code=(
                "MANAGED_ARTIFACT_HANDLE_UNKNOWN"
            ),
            owner="GENERATED_ACTION",
            route="generate",
            message=message,
            proposed=reference,
            field_path=field_path,
            failure_stage="pre_action_contract",
        )

    # ``produced_outputs`` used to ask the model to repeat Controller-owned
    # canonical IDs and paths.  It is deliberately ignored at this boundary;
    # output coverage is derived from typed argv/env references.  ``outputs``
    # remains an optional semantic-handle-only escape hatch for tools whose
    # outputs are implicit and therefore absent from argv/env.
    materialized.pop("produced_outputs", None)
    declared_outputs = proposal.get("outputs", []) or []
    if isinstance(declared_outputs, str):
        declared_outputs = [declared_outputs]
    seen_outputs: set[str] = set()
    for index, handle_value in enumerate(declared_outputs):
        handle = str(handle_value or "")
        if handle in seen_outputs:
            violations.append(_violation(
                code="MANAGED_ARTIFACT_HANDLE_DUPLICATE",
                owner="GENERATED_ACTION",
                route="generate",
                message=f"Output handle {handle!r} is declared more than once.",
                binding=bindings.get(handle),
                proposed=handle,
                field_path=f"outputs[{index}]",
            ))
            continue
        seen_outputs.add(handle)
        binding = bindings.get(handle)
        if binding is None:
            violations.append(missing_binding_violation(
                handle,
                "output",
                field_path=f"outputs[{index}]",
                message=f"Output handle {handle!r} is unknown.",
            ))
        elif binding.direction != "output":
            violations.append(_violation(
                code="MANAGED_ARTIFACT_DIRECTION_MISMATCH",
                owner="GENERATED_ACTION",
                route="generate",
                message=f"{handle!r} is an input and cannot be declared as an output.",
                binding=binding,
                proposed=handle,
                field_path=f"outputs[{index}]",
            ))

    def materialize_value(value: Any, field_path: str) -> Any:
        if not isinstance(value, str):
            return deepcopy(value)
        runtime_placeholders = _RUNTIME_PLACEHOLDER_RE.findall(value)
        if runtime_placeholders:
            violations.append(_violation(
                code="MANAGED_RUNTIME_PLACEHOLDER_UNSUPPORTED",
                owner="GENERATED_ACTION",
                route="generate",
                message=(
                    "Runtime resources are not managed artifact placeholders. "
                    "Use the verified runtime environment variable supplied by Controller, "
                    "for example $SNPEFF_JAR."
                ),
                proposed=value,
                field_path=field_path,
                failure_stage="pre_action_contract",
                repair_hint={
                    "strategy": "move_embedded_managed_placeholders_to_env",
                    "rule": (
                        "Each managed placeholder must be the complete value of one env entry. "
                        "Replace the embedded placeholder in the shell script with the corresponding $ENV_NAME."
                    ),
                    "runtime_rule": (
                        "Do not use {runtime:*}; use Controller-provided runtime environment variables."
                    ),
                },
                offending_placeholders=runtime_placeholders,
            ))
            return value
        operation_match = _OPERATION_PLACEHOLDER_RE.fullmatch(value)
        if operation_match:
            handle = operation_match.group(1)
            resolved = managed_operations.get(handle)
            if not resolved:
                violations.append(_violation(
                    code="MANAGED_OPERATION_HANDLE_UNKNOWN",
                    owner="GENERATED_ACTION",
                    route="generate",
                    message=f"Operation handle {handle!r} is not available in the pinned capability contract.",
                    proposed=value,
                    field_path=field_path,
                ))
                return value
            used_operations.append(handle)
            return resolved
        parsed = _parse_placeholder(value)
        if parsed:
            direction, handle = parsed
            binding = bindings.get(handle)
            if binding is None:
                violations.append(missing_binding_violation(
                    handle,
                    direction,
                    field_path=field_path,
                    message=f"Placeholder {value!r} does not resolve to a managed artifact.",
                ))
                return value
            if binding.direction != direction:
                violations.append(_violation(
                    code="MANAGED_ARTIFACT_DIRECTION_MISMATCH",
                    owner="GENERATED_ACTION",
                    route="generate",
                    message=f"Placeholder {value!r} uses the wrong direction.",
                    binding=binding,
                    proposed=value,
                    field_path=field_path,
                ))
                return value
            if binding.direction == "input" and not binding.runtime_available:
                violations.append(_violation(
                    code="PLAN_ARTIFACT_DEPENDENCY_UNAVAILABLE",
                    owner="DEPENDENCY",
                    route="recovery",
                    message=(
                        f"Approved dependency artifact {binding.artifact_id!r} is not "
                        "runtime-valid for consumption."
                    ),
                    binding=binding,
                    proposed=value,
                    field_path=field_path,
                    failure_stage="pre_action_contract",
                ))
                return value
            if not binding.canonical_path:
                violations.append(_violation(
                    code="PLAN_ARTIFACT_BINDING_PROJECTION_MISSING",
                    owner="INTERNAL_PROTOCOL",
                    subowner="controller_projection",
                    route="internal_reproject",
                    message=f"Controller has no path for {handle}.",
                    binding=binding,
                    proposed=value,
                    field_path=field_path,
                    failure_stage="pre_action_contract",
                ))
                return value
            used_handles.append(handle)
            return binding.canonical_path
        embedded_placeholders = _MANAGED_PLACEHOLDER_RE.findall(value)
        if embedded_placeholders:
            script_env_names = sorted({
                script_env_by_placeholder[item]
                for item in embedded_placeholders
                if item in script_env_by_placeholder
            })
            violations.append(_violation(
                code="MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
                owner="GENERATED_ACTION",
                route="generate",
                message=(
                    (
                        "Controller-owned managed bindings must not appear in an executable "
                        "script; use the corresponding Controller environment variables "
                        f"directly ({', '.join(script_env_names)})."
                    )
                    if script_protocol and script_env_names
                    else (
                        "Managed placeholders must occupy a complete argv/env value; "
                        "embedded shell/text substitution is forbidden."
                    )
                ),
                proposed=value,
                field_path=field_path,
                failure_stage="pre_action_contract",
                repair_hint={
                    "protocol": "script_block_v1" if script_protocol else "semantic_action_v2",
                    "strategy": (
                        "replace_controller_placeholder_with_env_reference"
                        if script_protocol
                        else "move_embedded_managed_placeholders_to_env"
                    ),
                    "rule": (
                        (
                            "Use the listed Controller environment variables directly and "
                            "remove managed bindings or default fallbacks from the script."
                        )
                        if script_protocol
                        else (
                            "Each managed placeholder must be the complete value of one env entry. "
                            "Replace the embedded placeholder in the shell script with the corresponding $ENV_NAME."
                        )
                    ),
                    "runtime_rule": (
                        "Do not use {runtime:*}; use Controller-provided runtime environment variables."
                    ),
                    **(
                        {"env_vars": ",".join(script_env_names)}
                        if script_protocol and script_env_names
                        else {}
                    ),
                },
                offending_placeholders=embedded_placeholders,
            ))
            return value
        for binding in bindings.values():
            if binding.path_policy != MANAGED_EXACT or not _literal_mentions_path(value, binding.canonical_path):
                continue
            if allow_legacy_exact_literals and value == binding.canonical_path:
                compatibility_hits.append({
                    "code": "LEGACY_MANAGED_LITERAL_EXACT",
                    "artifact_handle": binding.semantic_handle,
                    "field_path": field_path,
                })
                used_handles.append(binding.semantic_handle)
                return value
            violations.append(_violation(
                code="MANAGED_ARTIFACT_HANDLE_REQUIRED",
                owner="GENERATED_ACTION",
                route="generate",
                message=(
                    f"Use {binding.placeholder} for {binding.semantic_handle}; "
                    "Generate may not emit its authoritative path as a literal."
                ),
                binding=binding,
                proposed=value,
                field_path=field_path,
            ))
        return value

    def record_source_diagnostic(
        *,
        code: str,
        field_path: str,
        observed: str,
        artifact_handle: str = "",
    ) -> None:
        """Record a high-signal lexical occurrence without granting authority."""
        source_text = str(proposal.get(field_path) or "")
        offset = source_text.find(observed)
        line = source_text.count("\n", 0, max(offset, 0)) + 1 if offset >= 0 else 0
        source_diagnostics.append({
            "code": code,
            "severity": "HIGH",
            "source": "managed_artifact_free_source_scan",
            "field_path": field_path,
            "location": f"{field_path}:line:{line}" if line else field_path,
            "artifact_handle": artifact_handle,
            "observed": observed,
            "message": (
                "A managed-artifact token or literal occurs in raw source text; "
                "the occurrence is non-authoritative because Controller has no "
                "structured command semantics for this field."
            ),
            "authoritative": False,
            "effect": "diagnostic_only",
        })

    argv = proposal.get("argv")
    if argv is not None:
        if not isinstance(argv, list) or not argv:
            violations.append(_violation(
                code="ACTION_ARGV_INVALID",
                owner="GENERATED_ACTION",
                route="generate",
                message="Structured action argv must be a non-empty array.",
                field_path="argv",
            ))
        else:
            materialized_argv: List[Any] = []
            for index, value in enumerate(argv):
                # In the script-block adapter argv[2] is a byte-for-byte copy
                # of ``code``, not a typed command argument.  Treating it as a
                # semantic scalar would merely run the raw-source regex twice.
                if script_protocol and index == 2 and str(value) == str(proposal.get("code") or ""):
                    materialized_argv.append(deepcopy(value))
                else:
                    materialized_argv.append(materialize_value(value, f"argv[{index}]"))
            materialized["argv"] = normalize_bash_positional_argv(materialized_argv)
    env = proposal.get("env")
    if env is not None:
        if not isinstance(env, Mapping):
            violations.append(_violation(
                code="ACTION_ENV_INVALID",
                owner="GENERATED_ACTION",
                route="generate",
                message="Structured action env must be an object.",
                field_path="env",
            ))
        else:
            materialized_env: Dict[str, str] = {}
            for key, value in env.items():
                field_path = f"env.{key}"
                if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
                    violations.append(_violation(
                        code="ACTION_ENV_KEY_INVALID",
                        owner="GENERATED_ACTION",
                        route="generate",
                        message=(
                            "Structured action env keys must match "
                            "^[A-Za-z_][A-Za-z0-9_]*$."
                        ),
                        proposed=str(key),
                        field_path=field_path,
                        failure_stage="pre_action_contract",
                    ))
                    continue
                if not isinstance(value, str):
                    violations.append(_violation(
                        code="ACTION_ENV_VALUE_INVALID",
                        owner="GENERATED_ACTION",
                        route="generate",
                        message="Structured action env values must be strings.",
                        field_path=field_path,
                        failure_stage="pre_action_contract",
                    ))
                    continue
                materialized_env[key] = materialize_value(value, field_path)
            materialized["env"] = materialized_env

    # Raw source/prose has no deterministic argv semantics.  Keep lexical
    # matches visible for audit, but never turn them into ArtifactViolation.
    for source_field in ("code", "description"):
        source_text = str(proposal.get(source_field) or "")
        if not source_text:
            continue
        for placeholder in sorted(set(_MANAGED_PLACEHOLDER_RE.findall(source_text))):
            record_source_diagnostic(
                code="MANAGED_PLACEHOLDER_IN_FREE_SOURCE",
                field_path=source_field,
                observed=placeholder,
            )
        for runtime_placeholder in sorted(set(_RUNTIME_PLACEHOLDER_RE.findall(source_text))):
            record_source_diagnostic(
                code="RUNTIME_PLACEHOLDER_IN_FREE_SOURCE",
                field_path=source_field,
                observed=runtime_placeholder,
            )
        for binding in bindings.values():
            if binding.path_policy != MANAGED_EXACT:
                continue
            observed = ""
            if binding.canonical_path and binding.canonical_path in source_text:
                observed = binding.canonical_path
            elif binding.required_basename and re.search(
                rf"(?:(?<![\w.-])|(?<=:-)|(?<=:=)|(?<=:\+)|(?<=:\?))"
                rf"{re.escape(binding.required_basename)}(?![\w.-])",
                source_text,
            ):
                observed = binding.required_basename
            if observed:
                record_source_diagnostic(
                    code="MANAGED_LITERAL_IN_FREE_SOURCE",
                    field_path=source_field,
                    observed=observed,
                    artifact_handle=binding.semantic_handle,
                )

    referenced_output_handles = sorted({
        handle
        for handle in [*used_handles, *seen_outputs]
        if handle in bindings and bindings[handle].direction == "output"
    })
    materialized["managed_artifact_handles"] = sorted(set(used_handles))
    materialized["referenced_output_handles"] = referenced_output_handles
    materialized["materialized_artifacts"] = {
        handle: bindings[handle].canonical_path
        for handle in sorted(set(used_handles))
        if handle in bindings
    }
    materialized["controller_output_bindings"] = [
        {
            "artifact_id": bindings[handle].artifact_id,
            "semantic_handle": bindings[handle].semantic_handle,
            "expected_path": bindings[handle].canonical_path,
            "artifact_kind": bindings[handle].artifact_kind,
            "format": bindings[handle].format,
            "validation_contract_id": bindings[handle].validation_contract_id,
            "required": bindings[handle].required,
            "constraint_origin": bindings[handle].constraint_origin,
            "accepted_representations": list(bindings[handle].accepted_representations),
            "source_authority": bindings[handle].source_authority,
        }
        for handle in referenced_output_handles
    ]
    materialized["materialized_operations"] = {
        handle: managed_operations[handle]
        for handle in sorted(set(used_operations))
    }
    if compatibility_hits:
        materialized["compatibility_hits"] = compatibility_hits
    if source_diagnostics:
        materialized["source_diagnostics"] = source_diagnostics
    return materialized, violations


def validate_generate_output_coverage(
    plan_state: Mapping[str, Any],
    materialized_action: Mapping[str, Any],
    bindings: Mapping[str, ManagedArtifactBinding],
) -> List[ArtifactViolation]:
    """Derive required-output coverage from typed action references."""
    steps = [row for row in plan_state.get("steps", []) or [] if isinstance(row, Mapping)]
    current_step_id = str(plan_state.get("current_step_id") or "")
    step = next(
        (row for row in steps if str(row.get("step_id") or "") == current_step_id),
        steps[0] if steps else {},
    )
    by_ref: Dict[str, ManagedArtifactBinding] = {}
    for binding in bindings.values():
        if binding.direction != "output":
            continue
        for ref in (
            binding.artifact_id,
            binding.semantic_handle,
            binding.metadata.get("output_id"),
        ):
            if str(ref or ""):
                by_ref[str(ref)] = binding
    required: Dict[str, ManagedArtifactBinding] = {}
    for raw_ref in step.get("outputs", []) or []:
        if isinstance(raw_ref, Mapping):
            ref = str(raw_ref.get("artifact_id") or raw_ref.get("output_id") or "")
        else:
            ref = str(raw_ref or "")
        binding = by_ref.get(ref)
        if binding is not None:
            required[binding.semantic_handle] = binding
    covered = {
        str(handle)
        for handle in materialized_action.get("referenced_output_handles", []) or []
        if str(handle)
    }
    missing = sorted(set(required) - covered)
    if not missing:
        return []
    return [ArtifactViolation(
        code="GENERATE_OUTPUT_COVERAGE_INCOMPLETE",
        owner="GENERATED_ACTION",
        route="generate",
        message=(
            "Generate does not reference the approved current-step outputs: "
            + ",".join(missing)
        ),
        field_path="argv/env/outputs",
    )]


def validate_materialized_artifacts(
    materialized_action: Mapping[str, Any],
    bindings: Mapping[str, ManagedArtifactBinding],
) -> List[ArtifactViolation]:
    """Validate Controller/Renderer output after semantic handle resolution."""
    violations: List[ArtifactViolation] = []
    actual = materialized_action.get("materialized_artifacts") or {}
    if not isinstance(actual, Mapping):
        return [_violation(
            code="CONTROLLER_ARTIFACT_MATERIALIZATION_INVALID",
            owner="controller_projection",
            route="internal_block",
            message="Controller materialized_artifacts must be an object.",
            field_path="materialized_artifacts",
        )]
    for handle, proposed in actual.items():
        binding = bindings.get(str(handle))
        if binding is None:
            violations.append(_violation(
                code="CONTROLLER_ARTIFACT_HANDLE_UNKNOWN",
                owner="controller_projection",
                route="internal_block",
                message=f"Controller emitted unknown artifact handle {handle!r}.",
                proposed=str(proposed),
                field_path=f"materialized_artifacts.{handle}",
            ))
        elif str(proposed) != binding.canonical_path:
            violations.append(_violation(
                code="CONTROLLER_OUTPUT_BINDING_MISMATCH",
                owner="controller_projection",
                route="internal_block",
                message=(
                    f"Controller materialized {handle} as {proposed}; expected {binding.canonical_path}."
                ),
                binding=binding,
                proposed=str(proposed),
                field_path=f"materialized_artifacts.{handle}",
            ))
    return violations


def _observed_file_metadata(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False, "content_valid": False}
    artifact_kind = "directory" if path.is_dir() else "file" if path.is_file() else "other"
    value: Dict[str, Any] = {
        "path": str(path.resolve(strict=False)),
        "exists": True,
        "artifact_kind": artifact_kind,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
        "content_valid": bool(path.is_dir() or (path.is_file() and stat.st_size > 0)),
        "format": "directory" if path.is_dir() else "",
        "representation": "directory" if path.is_dir() else "plain",
    }
    if not path.is_file() or stat.st_size <= 0:
        return value
    try:
        with path.open("rb") as handle:
            prefix = handle.read(65536)
        value["content_prefix_sha256"] = hashlib.sha256(prefix).hexdigest()
        if prefix.startswith(b"\x1f\x8b"):
            value["representation"] = "gzip"
            try:
                with gzip.open(path, "rb") as handle:
                    prefix = handle.read(65536)
            except (OSError, EOFError, gzip.BadGzipFile):
                value["content_valid"] = False
                return value
        stripped = prefix.lstrip()
        if stripped.startswith(b">"):
            value["format"] = "fasta"
        elif stripped.startswith(b"##fileformat=VCF") or b"\n#CHROM\t" in prefix:
            value["format"] = "vcf"
        elif stripped[:1] in {b"{", b"["}:
            try:
                json.loads(prefix.decode("utf-8", errors="strict"))
                value["format"] = "json"
            except (UnicodeDecodeError, json.JSONDecodeError):
                value["format"] = "text"
        else:
            try:
                prefix.decode("utf-8", errors="strict")
                value["format"] = "text"
            except UnicodeDecodeError:
                value["format"] = "binary"
    except OSError:
        value["content_valid"] = False
    return value


def _managed_output_roots(
    output_contracts: Sequence[Mapping[str, Any]],
) -> List[Path]:
    roots: List[Path] = []
    for contract in output_contracts or []:
        if not isinstance(contract, Mapping):
            continue
        raw_path = str(contract.get("expected_path") or contract.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser().resolve(strict=False)
        root = path.parent
        if str(root) == "/":
            continue
        if root not in roots:
            roots.append(root)
    return roots[:16]


def capture_managed_output_snapshot(
    output_contracts: Sequence[Mapping[str, Any]],
    *,
    max_entries: int = 1024,
    max_depth: int = 5,
) -> Dict[str, Dict[str, Any]]:
    """Capture a bounded snapshot under only the action's managed roots."""
    snapshot: Dict[str, Dict[str, Any]] = {}
    for root in _managed_output_roots(output_contracts):
        stack: List[Tuple[Path, int]] = [(root, 0)]
        while stack and len(snapshot) < max_entries:
            path, depth = stack.pop()
            if path.is_symlink():
                continue
            if path.exists():
                metadata = _observed_file_metadata(path)
                snapshot[metadata["path"]] = metadata
            if depth >= max_depth or not path.is_dir():
                continue
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name, reverse=True)
            except OSError:
                continue
            stack.extend((child, depth + 1) for child in children)
    return snapshot


def discover_current_attempt_outputs(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    *,
    attempt_id: str,
) -> List[Dict[str, Any]]:
    """Return only new or changed physical artifacts from the current attempt."""
    observed: List[Dict[str, Any]] = []
    for path, raw in sorted(dict(after or {}).items()):
        current = dict(raw or {})
        previous = dict((before or {}).get(path) or {})
        if previous and all(
            previous.get(key) == current.get(key)
            for key in (
                "artifact_kind", "size", "mtime_ns", "ctime_ns",
                "content_valid", "content_prefix_sha256",
            )
        ):
            continue
        current.update({
            "attempt_id": str(attempt_id or ""),
            "current_attempt_owned": True,
        })
        observed.append(current)
    return observed


def _path_within(path: str, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _format_compatibility(expected: str, observed: str, artifact_kind: str) -> str:
    """Relate a semantic format contract to the bounded detector result.

    ``text`` is a detector class, not proof of a more specific semantic format
    such as GTF, TSV, or a configuration language.  Preserve that uncertainty
    instead of collapsing it into incompatibility.
    """
    expected_value = str(expected or "").strip().lower()
    observed_value = str(observed or "").strip().lower()
    if artifact_kind == "directory":
        return (
            "compatible"
            if expected_value in {"", "directory", "dir"}
            else "incompatible"
        )
    if expected_value in {"", "file", "text", "data", "output"}:
        return "compatible"
    if expected_value == observed_value:
        return "compatible"
    # Markdown is a textual representation; a bounded content sniff cannot
    # reliably distinguish prose-only Markdown from plain UTF-8 text.  This is
    # format compatibility, not filename/extension equivalence.
    if observed_value == "text" and expected_value in {
        "markdown", "md", "plaintext", "plain_text", "report",
    }:
        return "compatible"
    if observed_value == "text":
        # A bounded UTF-8 sniff proves only the detector class.  It cannot by
        # itself disprove a more specific semantic text contract; leave that to
        # the declared content validator.
        return "unproven"
    if not observed_value:
        return "unproven"
    return "incompatible"


def reconcile_runtime_outputs(
    output_contracts: Sequence[Mapping[str, Any]],
    observed_candidates: Sequence[Mapping[str, Any]],
    *,
    attempt_id: str,
    exit_code: int,
) -> Dict[str, Any]:
    """Reconcile logical outputs to attempt-owned physical realizations."""
    contracts = [dict(item) for item in output_contracts or [] if isinstance(item, Mapping)]
    candidates = [
        dict(item) for item in observed_candidates or []
        if isinstance(item, Mapping)
        and str(item.get("attempt_id") or "") == str(attempt_id or "")
        and bool(item.get("current_attempt_owned", True))
    ]
    result: Dict[str, Any] = {
        "status": "accepted",
        "attempt_id": str(attempt_id or ""),
        "exit_code": int(exit_code),
        "bindings": [],
        "violations": [],
        "observed_candidates": candidates,
    }
    if not contracts:
        result["status"] = "not_required"
        result["fingerprint"] = _runtime_reconciliation_fingerprint(result)
        return result
    if int(exit_code) != 0:
        result["status"] = "recovery"
        result["violations"].append({
            "code": "TOOL_EXECUTION_FAILED",
            "owner": "RUNTIME",
            "route": "recovery",
            "message": "The command exited non-zero; current-attempt files may be partial and were not adopted.",
            "observed_candidates": [str(item.get("path") or "") for item in candidates],
        })
        result["fingerprint"] = _runtime_reconciliation_fingerprint(result)
        return result

    used_paths: set[str] = set()
    for contract in contracts:
        handle = str(contract.get("semantic_handle") or "")
        logical_id = str(contract.get("artifact_id") or handle)
        expected_path = _canonical_path({"path": contract.get("expected_path") or contract.get("path")})
        expected_kind = str(contract.get("artifact_kind") or "file").strip().lower()
        expected_format = str(contract.get("format") or "").strip().lower()
        constraint_origin = str(
            contract.get("constraint_origin") or PHYSICAL_IMPLEMENTATION_DETAIL
        ).strip().upper()
        root = Path(expected_path).parent.resolve(strict=False) if expected_path else None
        in_scope = [
            item for item in candidates
            if str(item.get("path") or "") not in used_paths
            and (not root or _path_within(str(item.get("path") or ""), root))
        ]
        exact = next(
            (item for item in in_scope if _canonical_path({"path": item.get("path")}) == expected_path),
            None,
        )
        if exact is not None:
            observed_path = _canonical_path({"path": exact.get("path")})
            observed_kind = str(exact.get("artifact_kind") or "").strip().lower()
            format_compatibility = _format_compatibility(
                expected_format,
                str(exact.get("format") or ""),
                expected_kind,
            )
            if observed_kind != expected_kind:
                result["violations"].append({
                    "code": "OUTPUT_REALIZATION_MISMATCH",
                    "owner": "ARTIFACT_CONTRACT",
                    "route": "recovery",
                    "message": f"The expected path for {handle or logical_id} has artifact kind {observed_kind!r}, not {expected_kind!r}.",
                    "logical_artifact": logical_id,
                    "expected_realization": expected_path,
                    "observed_candidates": [observed_path],
                    "constraint_origin": constraint_origin,
                    "expected_artifact_kind": expected_kind,
                    "observed_artifact_kind": observed_kind,
                })
                continue
            if not bool(exact.get("content_valid", False)):
                result["violations"].append({
                    "code": "OBSERVED_ARTIFACT_CONTENT_INVALID",
                    "owner": "ARTIFACT_CONTRACT",
                    "route": "recovery",
                    "message": f"The current-attempt artifact for {handle or logical_id} exists but did not pass its content validation contract.",
                    "logical_artifact": logical_id,
                    "expected_realization": expected_path,
                    "observed_candidates": [observed_path],
                    "constraint_origin": constraint_origin,
                    "validation_contract_id": str(contract.get("validation_contract_id") or ""),
                })
                continue
            if format_compatibility == "incompatible":
                result["violations"].append({
                    "code": "OUTPUT_REALIZATION_MISMATCH",
                    "owner": "ARTIFACT_CONTRACT",
                    "route": "recovery",
                    "message": f"The current-attempt artifact for {handle or logical_id} has a provably incompatible detected format.",
                    "logical_artifact": logical_id,
                    "expected_realization": expected_path,
                    "observed_candidates": [observed_path],
                    "constraint_origin": constraint_origin,
                    "expected_format": expected_format,
                    "observed_format": str(exact.get("format") or ""),
                    "format_compatibility": format_compatibility,
                })
                continue
            used_paths.add(str(exact.get("path") or ""))
            result["bindings"].append({
                "artifact_id": logical_id,
                "semantic_handle": handle,
                "expected_path": expected_path,
                "observed_path": observed_path,
                "attempt_id": str(attempt_id or ""),
                "constraint_origin": constraint_origin,
                "realization": "expected",
                "format": str(exact.get("format") or expected_format),
                "representation": str(exact.get("representation") or ""),
                "content_valid": True,
                "format_compatibility": format_compatibility,
            })
            continue
        physical_candidates = [
            item for item in in_scope
            if str(item.get("artifact_kind") or "").strip().lower() == expected_kind
        ]
        eligible = []
        for item in physical_candidates:
            relation = _format_compatibility(
                expected_format,
                str(item.get("format") or ""),
                expected_kind,
            )
            if bool(item.get("content_valid", False)) and relation != "incompatible":
                candidate = dict(item)
                candidate["format_compatibility"] = relation
                eligible.append(candidate)
        if not eligible:
            if physical_candidates:
                content_invalid = all(
                    not bool(item.get("content_valid", False))
                    for item in physical_candidates
                )
                result["violations"].append({
                    "code": (
                        "OBSERVED_ARTIFACT_CONTENT_INVALID"
                        if content_invalid
                        else "OUTPUT_REALIZATION_MISMATCH"
                    ),
                    "owner": "ARTIFACT_CONTRACT",
                    "route": "recovery",
                    "message": (
                        f"Current-attempt realizations for {handle or logical_id} exist but did not pass the content contract."
                        if content_invalid
                        else f"Current-attempt realizations for {handle or logical_id} exist but have a provably incompatible detected format."
                    ),
                    "logical_artifact": logical_id,
                    "expected_realization": expected_path,
                    "constraint_origin": constraint_origin,
                    "observed_candidates": [
                        str(item.get("path") or "") for item in physical_candidates
                    ],
                    "expected_format": expected_format,
                    "observed_formats": [
                        str(item.get("format") or "") for item in physical_candidates
                    ],
                    "validation_contract_id": str(contract.get("validation_contract_id") or ""),
                })
                continue
            result["violations"].append({
                "code": "OUTPUT_MISSING",
                "owner": "ARTIFACT_CONTRACT",
                "route": "recovery",
                "message": f"No current-attempt physical realization was found for {handle or logical_id}.",
                "logical_artifact": logical_id,
                "expected_realization": expected_path,
                "constraint_origin": constraint_origin,
                "observed_candidates": [str(item.get("path") or "") for item in candidates],
            })
            continue
        if len(eligible) != 1:
            result["violations"].append({
                "code": "AMBIGUOUS_OUTPUT_REALIZATION",
                "owner": "ARTIFACT_CONTRACT",
                "route": "recovery",
                "message": f"Multiple current-attempt realizations satisfy {handle or logical_id}; Controller will not guess.",
                "logical_artifact": logical_id,
                "expected_realization": expected_path,
                "constraint_origin": constraint_origin,
                "observed_candidates": [str(item.get("path") or "") for item in eligible],
            })
            continue
        candidate = eligible[0]
        observed_path = _canonical_path({"path": candidate.get("path")})
        if constraint_origin in {PHYSICAL_HARD, PHYSICAL_CONTRACT_BOUND}:
            result["violations"].append({
                "code": "OUTPUT_REALIZATION_MISMATCH",
                "owner": "ARTIFACT_CONTRACT",
                "route": "recovery",
                "message": f"The physical realization for {handle or logical_id} is contract-bound and requires normalization.",
                "logical_artifact": logical_id,
                "expected_realization": expected_path,
                "observed_candidates": [observed_path],
                "constraint_origin": constraint_origin,
            })
            continue
        representation = str(candidate.get("representation") or "plain").strip().lower()
        accepted_representations = {
            str(item).strip().lower()
            for item in contract.get("accepted_representations", []) or []
            if str(item or "").strip()
        }
        downstream_compatible = (
            representation in {"", "plain", "directory"}
            or representation in accepted_representations
        )
        if not downstream_compatible:
            result["violations"].append({
                "code": "OUTPUT_REALIZATION_MISMATCH",
                "owner": "ARTIFACT_CONTRACT",
                "route": "recovery",
                "message": f"Observed representation {representation!r} is not accepted by the downstream contract.",
                "logical_artifact": logical_id,
                "expected_realization": expected_path,
                "observed_candidates": [observed_path],
                "constraint_origin": constraint_origin,
                "downstream_compatible": False,
            })
            continue
        used_paths.add(str(candidate.get("path") or ""))
        result["bindings"].append({
            "artifact_id": logical_id,
            "semantic_handle": handle,
            "expected_path": expected_path,
            "observed_path": observed_path,
            "attempt_id": str(attempt_id or ""),
            "constraint_origin": constraint_origin,
            "realization": "reconciled",
            "format": str(candidate.get("format") or expected_format),
            "representation": representation,
            "content_valid": True,
            "format_compatibility": str(candidate.get("format_compatibility") or ""),
            "downstream_compatible": True,
        })
    if result["violations"]:
        result["status"] = "recovery"
    result["fingerprint"] = _runtime_reconciliation_fingerprint(result)
    return result


def _runtime_reconciliation_fingerprint(result: Mapping[str, Any]) -> str:
    observed = [
        {
            key: item.get(key)
            for key in (
                "path", "artifact_kind", "format", "representation",
                "content_valid", "size", "content_prefix_sha256",
            )
        }
        for item in result.get("observed_candidates", []) or []
        if isinstance(item, Mapping)
    ]
    return hashlib.sha256(json.dumps(
        {
            "status": result.get("status"),
            "exit_code": result.get("exit_code"),
            "bindings": result.get("bindings", []),
            "violations": result.get("violations", []),
            "observed": observed,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="replace")).hexdigest()


def validate_observed_artifacts(
    observed: Sequence[Mapping[str, Any]],
    bindings: Mapping[str, ManagedArtifactBinding],
    *,
    attempt_id: str,
) -> List[ArtifactViolation]:
    """Compare attempt-scoped observed artifacts with the declared contract."""
    by_handle: Dict[str, List[Mapping[str, Any]]] = {}
    for row in observed:
        if not isinstance(row, Mapping):
            continue
        by_handle.setdefault(str(row.get("semantic_handle") or ""), []).append(row)
    violations: List[ArtifactViolation] = []
    for handle, binding in bindings.items():
        if binding.direction != "output" or not binding.required:
            continue
        rows = by_handle.get(handle, [])
        eligible = [row for row in rows if str(row.get("attempt_id") or "") == str(attempt_id)]
        if len(eligible) != 1:
            violations.append(_violation(
                code="OBSERVED_ARTIFACT_MISSING" if not eligible else "OBSERVED_ARTIFACT_AMBIGUOUS",
                owner="observed_artifact_mismatch",
                route="recovery",
                message=(
                    f"Expected exactly one current-attempt artifact for {handle}; found {len(eligible)}."
                ),
                binding=binding,
                proposed=", ".join(str(row.get("path") or "") for row in eligible),
                field_path="observed_artifacts",
            ))
            continue
        row = eligible[0]
        observed_path = _canonical_path({"path": row.get("path")})
        if observed_path != binding.canonical_path:
            violations.append(_violation(
                code="OBSERVED_ARTIFACT_PATH_MISMATCH",
                owner="observed_artifact_mismatch",
                route="recovery",
                message=(
                    f"The materialized contract required {binding.canonical_path}, but the current attempt "
                    f"produced {observed_path}."
                ),
                binding=binding,
                proposed=observed_path,
                field_path="observed_artifacts",
            ))
        if not bool(row.get("content_valid", row.get("valid", False))):
            violations.append(_violation(
                code="OBSERVED_ARTIFACT_CONTENT_INVALID",
                owner="observed_artifact_mismatch",
                route="recovery",
                message=f"Observed artifact {handle} did not pass its validation contract.",
                binding=binding,
                proposed=observed_path,
                field_path="observed_artifacts",
            ))
    return violations


def validate_artifact_relocation(
    *,
    binding: ManagedArtifactBinding,
    source_path: str,
    source_attempt_id: str,
    current_attempt_id: str,
    content_valid: bool,
    intended_candidate_count: int,
    preserves_semantics: bool,
    companion_set_complete: bool,
) -> List[ArtifactViolation]:
    """Enforce all six relocation preconditions without moving any file."""
    reasons: List[str] = []
    if str(source_attempt_id or "") != str(current_attempt_id or ""):
        reasons.append("source_not_from_current_attempt")
    if not content_valid:
        reasons.append("content_validation_failed")
    if int(intended_candidate_count or 0) != 1:
        reasons.append("candidate_not_unique")
    if not preserves_semantics:
        reasons.append("scientific_semantics_or_format_changed")
    if not binding.relocatable:
        reasons.append("contract_does_not_allow_relocation")
    if not companion_set_complete:
        reasons.append("compression_or_index_companion_set_incomplete")
    if not reasons:
        return []
    return [_violation(
        code="ARTIFACT_RELOCATION_FORBIDDEN",
        owner="observed_artifact_mismatch",
        route="recovery",
        message="Relocation is forbidden: " + ", ".join(reasons),
        binding=binding,
        proposed=str(source_path or ""),
        field_path="artifact_relocation",
    )]


def classify_output_mismatch(
    *,
    task_binding: ManagedArtifactBinding,
    plan_path: str = "",
    proposed_path: str = "",
    materialized_path: str = "",
    observed_path: str = "",
) -> Optional[ArtifactViolation]:
    """Classify aa/bb-style mismatches at the first divergent owner boundary."""
    expected = task_binding.canonical_path
    if plan_path and _canonical_path({"path": plan_path}) != expected:
        return _violation(
            code="PLAN_OUTPUT_BINDING_MISMATCH",
            owner="planner",
            route="planner",
            message="Plan redefined the task-owned output path.",
            binding=task_binding,
            proposed=_canonical_path({"path": plan_path}),
        )
    if proposed_path and _canonical_path({"path": proposed_path}) != expected:
        return _violation(
            code="ACTION_OUTPUT_BINDING_MISMATCH",
            owner="GENERATED_ACTION",
            route="generate",
            message="Generate proposed an output different from the managed artifact contract.",
            binding=task_binding,
            proposed=_canonical_path({"path": proposed_path}),
        )
    if materialized_path and _canonical_path({"path": materialized_path}) != expected:
        return _violation(
            code="CONTROLLER_OUTPUT_BINDING_MISMATCH",
            owner="controller_projection",
            route="internal_block",
            message="Controller/Renderer materialized the correct handle to the wrong path.",
            binding=task_binding,
            proposed=_canonical_path({"path": materialized_path}),
        )
    if observed_path and _canonical_path({"path": observed_path}) != expected:
        return _violation(
            code="OBSERVED_ARTIFACT_PATH_MISMATCH",
            owner="observed_artifact_mismatch",
            route="recovery",
            message="The tool produced a path different from the correctly materialized contract.",
            binding=task_binding,
            proposed=_canonical_path({"path": observed_path}),
        )
    return None

"""Deterministic managed-artifact contracts and action materialisation.

The model selects semantic handles.  This module is the only boundary that
turns those handles into authoritative paths, and it performs no shell text
substitution: placeholders must occupy a complete argv/env value.
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MANAGED_ARTIFACT_SCHEMA_VERSION = 2
MANAGED_EXACT = "managed_exact"
MODEL_SELECTABLE = "model_selectable"
_PLACEHOLDER_RE = re.compile(r"^\{(input|output):([A-Za-z][A-Za-z0-9_.-]*)\}$")
_OPERATION_PLACEHOLDER_RE = re.compile(r"^\{operation:([A-Za-z][A-Za-z0-9_.-]*)\}$")


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

    def to_dict(self) -> Dict[str, Any]:
        return {
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
            }.items()
            if value not in (None, "", False)
        }


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
        },
    )


def build_managed_artifact_bindings(
    task_spec: Mapping[str, Any],
) -> Dict[str, ManagedArtifactBinding]:
    """Compile the canonical Task artifact contract into unique handles."""
    result: Dict[str, ManagedArtifactBinding] = {}
    output_root = ""
    for raw in task_spec.get("output_contracts", []) or []:
        if not isinstance(raw, Mapping) or str(raw.get("role") or "") != "output_dir":
            continue
        output_root = _canonical_path(raw)
        if output_root:
            break
    for direction, field_name in (("input", "inputs"), ("output", "output_contracts")):
        for index, raw in enumerate(task_spec.get(field_name, []) or [], start=1):
            if not isinstance(raw, Mapping):
                continue
            binding = _binding_from_row(raw, direction=direction, index=index)
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
                    }
                )
            existing = result.get(binding.semantic_handle)
            if existing is not None and existing != binding:
                raise ManagedArtifactError(
                    f"MANAGED_ARTIFACT_HANDLE_AMBIGUOUS: {binding.semantic_handle}"
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
            if key not in {"artifact_id", "producer_step_id", "metadata", "aliases"}
        }
        for _, binding in sorted(bindings.items())
    ]


def _parse_placeholder(value: str) -> Optional[Tuple[str, str]]:
    match = _PLACEHOLDER_RE.fullmatch(str(value or ""))
    if not match:
        return None
    direction, name = match.groups()
    return direction, f"{direction}.{name}"


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
    )


def validate_plan_managed_artifacts(
    plan_state: Mapping[str, Any],
    bindings: Mapping[str, ManagedArtifactBinding],
) -> List[ArtifactViolation]:
    """Reject a Plan that copies/redefines a task-owned managed path."""
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
            if binding is None or binding.direction != "output":
                violations.append(_violation(
                    code="PLAN_ARTIFACT_HANDLE_INVALID",
                    owner="plan_binding",
                    route="planner_repair",
                    message=f"Plan output handle {handle!r} is unknown or not an output.",
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
                    proposed_handle = str(raw or "") if str(raw or "").startswith("output.") else ""
                if proposed_handle:
                    binding = bindings.get(proposed_handle)
                    if binding is None or binding.direction != "output":
                        violations.append(_violation(
                            code="PLAN_ARTIFACT_HANDLE_INVALID",
                            owner="plan_binding",
                            route="planner_repair",
                            message=f"Plan output handle {proposed_handle!r} is invalid.",
                            proposed=proposed_handle,
                            field_path=f"steps[{step_index}].{field_name}[{item_index}]",
                        ))
                    elif proposed_path and proposed_path != binding.canonical_path:
                        violations.append(_violation(
                            code="PLAN_OUTPUT_BINDING_MISMATCH",
                            owner="plan_binding",
                            route="planner_repair",
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
) -> Tuple[Dict[str, Any], List[ArtifactViolation]]:
    """Resolve typed placeholders in argv/env and validate declared handles.

    Embedded placeholders and free shell replacement are rejected.  A path
    containing spaces or shell metacharacters remains one argv value.
    """
    proposal = deepcopy(dict(action or {}))
    materialized = deepcopy(proposal)
    violations: List[ArtifactViolation] = []
    used_handles: List[str] = []
    compatibility_hits: List[Dict[str, str]] = []
    managed_operations = {
        str(key): str(value)
        for key, value in dict(operation_bindings or {}).items()
        if str(key) and str(value)
    }
    used_operations: List[str] = []

    declared_outputs = proposal.get("outputs", []) or []
    if isinstance(declared_outputs, str):
        declared_outputs = [declared_outputs]
    seen_outputs: set[str] = set()
    for index, handle_value in enumerate(declared_outputs):
        handle = str(handle_value or "")
        if handle in seen_outputs:
            violations.append(_violation(
                code="MANAGED_ARTIFACT_HANDLE_DUPLICATE",
                owner="action_generation",
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
            violations.append(_violation(
                code="MANAGED_ARTIFACT_HANDLE_UNKNOWN",
                owner="action_generation",
                route="generate",
                message=f"Output handle {handle!r} is unknown.",
                proposed=handle,
                field_path=f"outputs[{index}]",
            ))
        elif binding.direction != "output":
            violations.append(_violation(
                code="MANAGED_ARTIFACT_DIRECTION_MISMATCH",
                owner="action_generation",
                route="generate",
                message=f"{handle!r} is an input and cannot be declared as an output.",
                binding=binding,
                proposed=handle,
                field_path=f"outputs[{index}]",
            ))

    def materialize_value(value: Any, field_path: str) -> Any:
        if not isinstance(value, str):
            return deepcopy(value)
        operation_match = _OPERATION_PLACEHOLDER_RE.fullmatch(value)
        if operation_match:
            handle = operation_match.group(1)
            resolved = managed_operations.get(handle)
            if not resolved:
                violations.append(_violation(
                    code="MANAGED_OPERATION_HANDLE_UNKNOWN",
                    owner="action_generation",
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
                violations.append(_violation(
                    code="MANAGED_ARTIFACT_HANDLE_UNKNOWN",
                    owner="action_generation",
                    route="generate",
                    message=f"Placeholder {value!r} does not resolve to a managed artifact.",
                    proposed=value,
                    field_path=field_path,
                ))
                return value
            if binding.direction != direction:
                violations.append(_violation(
                    code="MANAGED_ARTIFACT_DIRECTION_MISMATCH",
                    owner="action_generation",
                    route="generate",
                    message=f"Placeholder {value!r} uses the wrong direction.",
                    binding=binding,
                    proposed=value,
                    field_path=field_path,
                ))
                return value
            if not binding.canonical_path:
                violations.append(_violation(
                    code="MANAGED_ARTIFACT_PATH_MISSING",
                    owner="controller_projection",
                    route="internal_block",
                    message=f"Controller has no path for {handle}.",
                    binding=binding,
                    proposed=value,
                    field_path=field_path,
                ))
                return value
            used_handles.append(handle)
            return binding.canonical_path
        if "{input:" in value or "{output:" in value or "{operation:" in value:
            violations.append(_violation(
                code="MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
                owner="action_generation",
                route="generate",
                message=(
                    "Managed placeholders must occupy a complete argv/env value; "
                    "embedded shell/text substitution is forbidden."
                ),
                proposed=value,
                field_path=field_path,
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
                owner="action_generation",
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

    argv = proposal.get("argv")
    if argv is not None:
        if not isinstance(argv, list) or not argv:
            violations.append(_violation(
                code="ACTION_ARGV_INVALID",
                owner="action_generation",
                route="generate",
                message="Structured action argv must be a non-empty array.",
                field_path="argv",
            ))
        else:
            materialized["argv"] = [
                materialize_value(value, f"argv[{index}]")
                for index, value in enumerate(argv)
            ]
    env = proposal.get("env")
    if env is not None:
        if not isinstance(env, Mapping):
            violations.append(_violation(
                code="ACTION_ENV_INVALID",
                owner="action_generation",
                route="generate",
                message="Structured action env must be an object.",
                field_path="env",
            ))
        else:
            materialized["env"] = {
                str(key): materialize_value(value, f"env.{key}")
                for key, value in env.items()
            }

    # New managed actions are structured.  Free shell code cannot be safely
    # materialised because quoting semantics depend on the shell/parser.
    code = str(proposal.get("code") or "")
    if code:
        if "{operation:" in code:
            violations.append(_violation(
                code="MANAGED_OPERATION_PLACEHOLDER_IN_FREE_SHELL",
                owner="action_generation",
                route="generate",
                message="Managed operation placeholders are supported only as complete structured argv values.",
                proposed=code,
                field_path="code",
            ))
        for binding in bindings.values():
            literal = binding.canonical_path
            basename = binding.required_basename
            if binding.path_policy != MANAGED_EXACT:
                continue
            if binding.placeholder in code:
                violations.append(_violation(
                    code="MANAGED_ARTIFACT_PLACEHOLDER_IN_FREE_SHELL",
                    owner="action_generation",
                    route="generate",
                    message="Managed placeholders are supported only in structured argv/env fields.",
                    binding=binding,
                    proposed=binding.placeholder,
                    field_path="code",
                ))
            elif (literal and literal in code) or (basename and re.search(rf"(?<![\w.-]){re.escape(basename)}(?![\w.-])", code)):
                if allow_legacy_exact_literals and literal and literal in code:
                    compatibility_hits.append({
                        "code": "LEGACY_MANAGED_LITERAL_EXACT",
                        "artifact_handle": binding.semantic_handle,
                        "field_path": "code",
                    })
                    used_handles.append(binding.semantic_handle)
                else:
                    proposed_path = literal if literal and literal in code else basename
                    violations.append(_violation(
                        code="ACTION_OUTPUT_BINDING_MISMATCH" if binding.direction == "output" else "MANAGED_ARTIFACT_HANDLE_REQUIRED",
                        owner="action_generation",
                        route="generate",
                        message=(
                            f"The action uses literal {proposed_path!r}; use {binding.placeholder} "
                            f"for managed artifact {binding.semantic_handle}."
                        ),
                        binding=binding,
                        proposed=proposed_path,
                        field_path="code",
                    ))

    for handle in sorted(seen_outputs):
        binding = bindings.get(handle)
        if binding is not None and binding.required and handle not in used_handles:
            violations.append(_violation(
                code="ACTION_OUTPUT_BINDING_MISMATCH",
                owner="action_generation",
                route="generate",
                message=(
                    f"Declared output {handle} is not materialised in argv/env; "
                    f"use {binding.placeholder}."
                ),
                binding=binding,
                proposed="missing_placeholder",
                field_path="outputs",
            ))
    materialized["managed_artifact_handles"] = sorted(set(used_handles))
    materialized["materialized_artifacts"] = {
        handle: bindings[handle].canonical_path
        for handle in sorted(set(used_handles))
        if handle in bindings
    }
    materialized["materialized_operations"] = {
        handle: managed_operations[handle]
        for handle in sorted(set(used_operations))
    }
    if compatibility_hits:
        materialized["compatibility_hits"] = compatibility_hits
    return materialized, violations


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
            owner="plan_binding",
            route="planner_repair",
            message="Plan redefined the task-owned output path.",
            binding=task_binding,
            proposed=_canonical_path({"path": plan_path}),
        )
    if proposed_path and _canonical_path({"path": proposed_path}) != expected:
        return _violation(
            code="ACTION_OUTPUT_BINDING_MISMATCH",
            owner="action_generation",
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

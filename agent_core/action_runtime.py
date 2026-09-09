"""Capability-owned runtime and execution provenance contracts.

This module is the only authority for turning a selected Skill capability into
the environment of one executable action.  Runtime discovery may populate the
registry, but neither Planner prose nor generated source text can enlarge the
capability's binding scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


RUNTIME_MANIFEST_SCHEMA_VERSION = 1
ARTIFACT_MANIFEST_SCHEMA_VERSION = 1
EXECUTION_POLICY_MANIFEST_SCHEMA_VERSION = 1
ACTION_APPROVAL_IDENTITY_SCHEMA_VERSION = 1

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_BINDING_ID_RE = re.compile(r"^(?:runtime|artifact)\.[a-z0-9][a-z0-9_.-]{0,126}$")

# The internal resolution returned by ``resolve_action_runtime_bindings`` is an
# authorization object.  Its contract hash and manifest identities must remain
# Controller-only.  Provider projections are built independently from this
# explicit semantic allowlist; they are never produced by copying the internal
# object and deleting a few known keys.
LLM_CAPABILITY_FIELD_ALLOWLIST = (
    "capability_id",
    "selected_skill_id",
    "contract_source",
    "contract_revision",
    "required_runtime_binding_ids",
    "optional_runtime_binding_ids",
    "required_artifact_binding_ids",
    "operation",
    "consumes_artifacts",
    "produces_artifacts",
    "produces_bindings",
    "produces_observations",
    "workspace_mutations",
    "explicit_side_effects",
    "execution_guidance",
)

_OPTIONAL_CAPABILITY_EFFECT_LIST_FIELDS = (
    "consumes_artifacts",
    "produces_artifacts",
    "produces_bindings",
    "produces_observations",
    "workspace_mutations",
    "explicit_side_effects",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def stable_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def value_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class ActionRuntimeContractError(ValueError):
    """Typed fail-closed boundary for action runtime authority."""

    def __init__(self, code: str, **diagnostic: Any) -> None:
        self.code = str(code or "RUNTIME_BINDING_MANIFEST_INVALID")
        self.diagnostic = deepcopy(diagnostic)
        super().__init__(f"{self.code}: {_canonical_json(self.diagnostic)}")

    def to_diagnostic(self) -> Dict[str, Any]:
        return {"code": self.code, **deepcopy(self.diagnostic)}


def canonical_skill_namespace(skill_id: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", ".", str(skill_id or "").strip().lower())
    return text.strip(".") or "skill"


def default_execution_capability_id(skill_id: Any) -> str:
    return f"{canonical_skill_namespace(skill_id)}.execute"


def _string_ids(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else []
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _typed_effect_entries(value: Any) -> list[Any]:
    """Preserve typed effect rows while keeping string-only hashes stable."""
    values = value if isinstance(value, (list, tuple, set)) else []
    by_identity: Dict[str, Any] = {}
    for item in values:
        if isinstance(item, Mapping):
            row: Any = deepcopy(dict(item))
        elif str(item).strip():
            row = str(item).strip()
        else:
            continue
        by_identity[_canonical_json(row)] = row
    return [by_identity[key] for key in sorted(by_identity)]


def _workspace_mutations(value: Any) -> list[Any]:
    """Normalize mutable workspace declarations without treating them as files."""
    values = value if isinstance(value, (list, tuple, set)) else []
    rows: list[Any] = []
    for item in values:
        if isinstance(item, Mapping):
            source = deepcopy(dict(item))
            workspace_ref = str(
                source.get("workspace_ref")
                or source.get("id")
                or source.get("identity")
                or ""
            ).strip()
            if not workspace_ref:
                continue
            source.update({
                "workspace_ref": workspace_ref,
                "mutation": str(
                    source.get("mutation") or source.get("mode") or "readwrite"
                ).strip().lower(),
            })
            for alias in ("id", "identity", "mode"):
                source.pop(alias, None)
            rows.append(source)
        elif str(item).strip():
            rows.append(str(item).strip())
    return sorted(rows, key=lambda item: _canonical_json(item))


def normalize_capability_contract(
    value: Mapping[str, Any],
    *,
    selected_skill_id: str = "",
) -> Dict[str, Any]:
    capability_id = str(
        value.get("capability_id") or value.get("execution_capability_id") or ""
    ).strip()
    if not capability_id:
        raise ActionRuntimeContractError(
            "ACTION_CAPABILITY_SCOPE_MISSING",
            selected_skill_id=str(selected_skill_id or ""),
            json_pointer="$.execution_capability_id",
        )
    required_runtime = _string_ids(value.get("required_runtime_binding_ids"))
    optional_runtime = _string_ids(value.get("optional_runtime_binding_ids"))
    required_artifacts = _string_ids(value.get("required_artifact_binding_ids"))
    invalid_runtime = [
        item for item in [*required_runtime, *optional_runtime]
        if not _BINDING_ID_RE.fullmatch(item) or not item.startswith("runtime.")
    ]
    invalid_artifacts = [
        item for item in required_artifacts
        if not _BINDING_ID_RE.fullmatch(item) or not item.startswith("artifact.")
    ]
    overlap = sorted(set(required_runtime) & set(optional_runtime))
    if invalid_runtime or invalid_artifacts or overlap:
        raise ActionRuntimeContractError(
            "UNDECLARED_RUNTIME_BINDING",
            execution_capability_id=capability_id,
            selected_skill_id=str(selected_skill_id or value.get("selected_skill_id") or ""),
            invalid_runtime_binding_ids=invalid_runtime,
            invalid_artifact_binding_ids=invalid_artifacts,
            conflicting_binding_ids=overlap,
        )
    guidance = [
        str(item).strip()
        for item in value.get("execution_guidance", []) or []
        if str(item).strip()
    ]
    normalized = {
        "schema_version": 1,
        "capability_id": capability_id,
        "selected_skill_id": str(
            selected_skill_id or value.get("selected_skill_id") or ""
        ).strip(),
        "required_runtime_binding_ids": required_runtime,
        "optional_runtime_binding_ids": optional_runtime,
        "required_artifact_binding_ids": required_artifacts,
        "execution_guidance": guidance,
    }
    # Effect semantics are an additive contract extension.  Preserve the exact
    # normalized shape (and therefore the contract hash) of every legacy
    # capability that did not declare them.  An explicitly declared empty list
    # remains visible internally so producers can distinguish "declared none"
    # from the absence of an effect contract.
    if "operation" in value:
        normalized["operation"] = str(value.get("operation") or "").strip()
    # The declaration's source and revision travel with its semantic contract.
    # Keep this additive: absent fields on legacy capabilities must not change
    # an already approved capability hash merely because the reader upgraded.
    for field in ("contract_source", "contract_revision"):
        if field in value:
            normalized[field] = str(value.get(field) or "").strip()
    for field in _OPTIONAL_CAPABILITY_EFFECT_LIST_FIELDS:
        if field in value:
            normalized[field] = (
                _workspace_mutations(value.get(field))
                if field == "workspace_mutations"
                else _string_ids(value.get(field))
                if field == "consumes_artifacts"
                else _typed_effect_entries(value.get(field))
            )
    if "binding_applicability" in value:
        raw_applicability = value.get("binding_applicability")
        if not isinstance(raw_applicability, Mapping):
            raise ActionRuntimeContractError(
                "ACTION_CAPABILITY_BINDING_APPLICABILITY_INVALID",
                execution_capability_id=capability_id,
                selected_skill_id=str(
                    selected_skill_id or value.get("selected_skill_id") or ""
                ),
            )
        applicability = dict(raw_applicability)
        try:
            applicability_schema_version = int(
                applicability.get("schema_version") or 1
            )
            binding_contract_schema_version = int(
                applicability.get("binding_contract_schema_version") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ActionRuntimeContractError(
                "ACTION_CAPABILITY_BINDING_APPLICABILITY_INVALID",
                execution_capability_id=capability_id,
                selected_skill_id=str(
                    selected_skill_id or value.get("selected_skill_id") or ""
                ),
            ) from exc
        normalized["binding_applicability"] = {
            "schema_version": applicability_schema_version,
            "applicable": applicability.get("applicable") is True,
            "binding_contract_schema_version": binding_contract_schema_version,
            "contract_source": str(
                applicability.get("contract_source")
                or "generate.binding_contract"
            ),
            "consumes_bindings": _string_ids(
                applicability.get("consumes_bindings")
            ),
            "produces_bindings": _string_ids(
                applicability.get("produces_bindings")
            ),
        }
    return normalized


def capability_contract_hash(value: Mapping[str, Any]) -> str:
    return stable_digest(normalize_capability_contract(value))


def _runtime_binding_identity(resolved_name: str) -> tuple[str, str, str]:
    """Map one verified audit name to one canonical binding and env name."""
    name = str(resolved_name or "").strip().upper()
    if name.startswith("RESOLVED_"):
        name = name.removeprefix("RESOLVED_")
    special = {
        "ENV_PREFIX": ("runtime.environment.prefix", "ENV_PREFIX", "directory_path"),
        "CONDA_PREFIX": ("runtime.environment.prefix", "ENV_PREFIX", "directory_path"),
        "JAVA_HOME": ("runtime.java.home", "JAVA_HOME", "directory_path"),
        "JAVA_BIN": ("runtime.java.bin", "JAVA_BIN", "executable_path"),
        "SNPEFF_JAR": ("runtime.snpeff.jar", "SNPEFF_JAR", "jar_path"),
        "SNPEFF_BIN": ("runtime.snpeff.bin", "SNPEFF_BIN", "executable_path"),
    }
    if name in special:
        return special[name]
    suffixes = (
        ("_BIN", "bin", "executable_path"),
        ("_JAR", "jar", "jar_path"),
        ("_HOME", "home", "directory_path"),
        ("_LIBRARY_DIR", "library_dir", "directory_path"),
        ("_LIB_DIR", "library_dir", "directory_path"),
        ("_PREFIX", "prefix", "directory_path"),
    )
    for suffix, kind, value_kind in suffixes:
        if name.endswith(suffix) and len(name) > len(suffix):
            owner = name[: -len(suffix)].lower().replace("_", ".")
            return f"runtime.{owner}.{kind}", name, value_kind
    owner = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".") or "unknown"
    return f"runtime.{owner}", name, "path"


def _runtime_audit_id(audit: Mapping[str, Any]) -> str:
    explicit = str(audit.get("runtime_audit_id") or audit.get("audit_id") or "").strip()
    if explicit:
        return explicit
    projection = {
        "skill_id": str(audit.get("skill_id") or audit.get("skill") or ""),
        "checked_env": str(audit.get("checked_env") or audit.get("env") or ""),
        "status": str(audit.get("status") or ""),
        "resolved_runtime_paths": dict(audit.get("resolved_runtime_paths") or {}),
    }
    return "runtime-audit-" + stable_digest(projection).removeprefix("sha256:")[:20]


def _registry_record(
    *,
    binding_id: str,
    env_name: str,
    value: str,
    value_kind: str,
    audit: Mapping[str, Any],
) -> Dict[str, Any]:
    sensitivity = str(audit.get("sensitivity") or "internal_path")
    if re.search(r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION)", env_name):
        sensitivity = "secret"
    return {
        "binding_id": binding_id,
        "env_name": env_name,
        "value": value,
        "value_kind": value_kind,
        "source": "verified_runtime_audit",
        "source_runtime_audit_id": _runtime_audit_id(audit),
        "source_skill_id": str(audit.get("skill_id") or audit.get("skill") or ""),
        "status": "ready",
        "sensitivity": sensitivity,
        "value_digest": value_digest(value),
    }


def build_runtime_binding_registry(state_or_audits: Any) -> Dict[str, Dict[str, Any]]:
    """Build a canonical registry without exporting compatibility aliases."""
    evaluations = {}
    if isinstance(state_or_audits, Mapping):
        audits = state_or_audits.get("runtime_audit", []) or []
        evaluations = state_or_audits.get("runtime_binding_evaluations") or {}
        task_state = state_or_audits.get("task_state") or {}
        if "runtime_evidence" in task_state:
            from agent_core.runtime_evidence import project_runtime_audits, evaluate_runtime_evidence
            task_id = state_or_audits.get("task_id")
            audits = project_runtime_audits(task_state, task_id=task_id)
            evaluations = evaluate_runtime_evidence(task_state, task_id=task_id)
    else:
        audits = state_or_audits or []
    registry: Dict[str, Dict[str, Any]] = {}
    for raw in audits:
        if not isinstance(raw, Mapping):
            continue
        audit = dict(raw)
        if str(audit.get("status") or "").strip().lower() != "ready":
            continue
        resolved = dict(audit.get("resolved_runtime_paths") or {})
        for raw_name, raw_value in sorted(resolved.items()):
            name = str(raw_name or "").strip().upper()
            value = str(raw_value or "").strip()
            if not value or not _ENV_NAME_RE.fullmatch(name):
                continue
            binding_id, env_name, kind = _runtime_binding_identity(name)
            row = _registry_record(
                binding_id=binding_id,
                env_name=env_name,
                value=value,
                value_kind=kind,
                audit=audit,
            )
            previous = registry.get(binding_id)
            if previous and previous.get("value_digest") != row["value_digest"]:
                registry[binding_id] = {
                    **row,
                    "status": "ambiguous",
                    "conflicting_source_runtime_audit_ids": sorted({
                        str(previous.get("source_runtime_audit_id") or ""),
                        str(row.get("source_runtime_audit_id") or ""),
                    }),
                }
            else:
                registry[binding_id] = row

        java_home = str(
            resolved.get("RESOLVED_JAVA_HOME") or resolved.get("JAVA_HOME") or ""
        ).strip()
        java_bin = str(
            resolved.get("RESOLVED_JAVA_BIN") or resolved.get("JAVA_BIN") or ""
        ).strip()
        if not java_bin and java_home:
            candidate = Path(java_home) / "bin" / "java"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                java_bin = str(candidate.resolve())
        if java_bin and "runtime.java.bin" not in registry:
            registry["runtime.java.bin"] = _registry_record(
                binding_id="runtime.java.bin",
                env_name="JAVA_BIN",
                value=java_bin,
                value_kind="executable_path",
                audit=audit,
            )
    # A derived path never overrules an explicit negative or conflict about
    # that fact. Apply this rule to every materialized binding, independent of
    # the tool-specific derivations already supported above.
    for binding_id, evaluation in evaluations.items():
        availability = evaluation.get("availability")
        if availability in {"UNAVAILABLE", "AMBIGUOUS"} or (
            availability == "UNKNOWN" and evaluation.get("evidence_ids")
        ):
            registry[binding_id] = {
                "binding_id": binding_id,
                "status": availability.lower(),
                "runtime_evidence": deepcopy(evaluation),
            }
    return {key: registry[key] for key in sorted(registry)}


def _manifest_hash(payload: Mapping[str, Any]) -> str:
    value = {key: deepcopy(item) for key, item in payload.items() if key != "manifest_hash"}
    return stable_digest(value)


def validate_manifest_hash(manifest: Mapping[str, Any]) -> None:
    expected = str(manifest.get("manifest_hash") or "")
    actual = _manifest_hash(manifest)
    if not expected or expected != actual:
        raise ActionRuntimeContractError(
            "RUNTIME_BINDING_MANIFEST_INVALID",
            expected_manifest_hash=expected,
            observed_manifest_hash=actual,
        )

def resolve_action_runtime_bindings(
    *,
    selected_skill_id: str,
    execution_capability_id: str,
    approved_capability_contract_hash: str,
    capability_registry: Mapping[str, Mapping[str, Any]],
    ready_runtime_binding_registry: Mapping[str, Mapping[str, Any]],
    current_action_identity: Mapping[str, Any],
    requested_optional_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Resolve exactly one approved capability; never fall back to task scope."""
    capability_id = str(execution_capability_id or "").strip()
    available_ids = sorted(str(item) for item in capability_registry)
    if not capability_id:
        raise ActionRuntimeContractError(
            "ACTION_CAPABILITY_SCOPE_MISSING",
            json_pointer="$.execution_capability_id",
            selected_skill_id=str(selected_skill_id or ""),
            available_capability_ids=available_ids,
        )
    raw_contract = capability_registry.get(capability_id)
    if not isinstance(raw_contract, Mapping):
        raise ActionRuntimeContractError(
            "UNKNOWN_EXECUTION_CAPABILITY",
            json_pointer="$.execution_capability_id",
            received_id=capability_id,
            available_capability_ids=available_ids,
            selected_skill_id=str(selected_skill_id or ""),
        )
    contract = normalize_capability_contract(
        raw_contract,
        selected_skill_id=str(raw_contract.get("selected_skill_id") or selected_skill_id),
    )
    contract_skill = str(contract.get("selected_skill_id") or "")
    if contract_skill and contract_skill != str(selected_skill_id or ""):
        raise ActionRuntimeContractError(
            "UNKNOWN_EXECUTION_CAPABILITY",
            json_pointer="$.execution_capability_id",
            received_id=capability_id,
            available_capability_ids=[
                item for item in available_ids
                if str((capability_registry.get(item) or {}).get("selected_skill_id") or "")
                == str(selected_skill_id or "")
            ],
            selected_skill_id=str(selected_skill_id or ""),
        )
    observed_contract_hash = capability_contract_hash(contract)
    if (
        approved_capability_contract_hash
        and str(approved_capability_contract_hash) != observed_contract_hash
    ):
        raise ActionRuntimeContractError(
            "RUNTIME_BINDING_SNAPSHOT_DRIFT",
            execution_capability_id=capability_id,
            selected_skill_id=str(selected_skill_id or ""),
            drift_field="capability_contract_hash",
            approved_hash=str(approved_capability_contract_hash),
            observed_hash=observed_contract_hash,
        )
    optional = _string_ids(requested_optional_ids)
    allowed_optional = set(contract["optional_runtime_binding_ids"])
    undeclared = sorted(set(optional) - allowed_optional)
    if undeclared:
        raise ActionRuntimeContractError(
            "UNDECLARED_RUNTIME_BINDING",
            execution_capability_id=capability_id,
            selected_skill_id=str(selected_skill_id or ""),
            received_binding_ids=undeclared,
            available_optional_runtime_binding_ids=sorted(allowed_optional),
        )
    required = list(contract["required_runtime_binding_ids"])
    resolved_ids = [*required, *[item for item in optional if item not in required]]
    records: list[Dict[str, Any]] = []
    effective: Dict[str, str] = {}
    for binding_id in resolved_ids:
        raw_record = ready_runtime_binding_registry.get(binding_id)
        record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
        if str(record.get("status") or "") != "ready" or not str(record.get("value") or ""):
            raise ActionRuntimeContractError(
                "REQUIRED_RUNTIME_BINDING_UNAVAILABLE",
                execution_capability_id=capability_id,
                selected_skill_id=str(selected_skill_id or ""),
                binding_id=binding_id,
                required=binding_id in required,
                failure_scope="runtime_fact",
                availability=("UNAVAILABLE" if record.get("status") == "unavailable" else
                              "AMBIGUOUS" if record.get("status") == "ambiguous" else "UNKNOWN"),
                available_runtime_binding_ids=sorted(
                    key for key, value in ready_runtime_binding_registry.items()
                    if str((value or {}).get("status") or "") == "ready"
                ),
            )
        source_skill = str(record.get("source_skill_id") or "")
        if source_skill and source_skill != str(selected_skill_id or ""):
            raise ActionRuntimeContractError(
                "REQUIRED_RUNTIME_BINDING_UNAVAILABLE",
                execution_capability_id=capability_id,
                selected_skill_id=str(selected_skill_id or ""),
                binding_id=binding_id,
                source_skill_id=source_skill,
                reason="binding belongs to another Skill runtime audit",
            )
        env_name = str(record.get("env_name") or "")
        if not _ENV_NAME_RE.fullmatch(env_name):
            raise ActionRuntimeContractError(
                "RUNTIME_BINDING_MANIFEST_INVALID",
                binding_id=binding_id,
                invalid_env_name=env_name,
            )
        effective[env_name] = str(record["value"])
        records.append({
            key: deepcopy(record.get(key))
            for key in (
                "binding_id", "env_name", "source", "source_runtime_audit_id",
                "source_skill_id", "value_kind", "sensitivity", "value_digest", "status",
            )
        })
    identity = {str(key): deepcopy(value) for key, value in current_action_identity.items()}
    manifest: Dict[str, Any] = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "action_id": str(identity.get("action_id") or ""),
        "plan_id": str(identity.get("plan_id") or ""),
        "step_id": str(identity.get("step_id") or ""),
        "selected_skill_id": str(selected_skill_id or ""),
        "execution_capability_id": capability_id,
        "capability_contract_hash": observed_contract_hash,
        "scope": "current_action",
        "bindings": records,
    }
    manifest["manifest_hash"] = _manifest_hash(manifest)
    return {
        "capability_contract": contract,
        "capability_contract_hash": observed_contract_hash,
        "effective_runtime_bindings": dict(sorted(effective.items())),
        "runtime_binding_records": records,
        "runtime_binding_manifest": manifest,
        "runtime_binding_manifest_hash": manifest["manifest_hash"],
    }


def validate_runtime_paths_prelaunch(resolution: Mapping[str, Any]) -> None:
    """Recheck the accepted values without rebuilding approval or evidence."""
    values = resolution.get("effective_runtime_bindings") or {}
    for record in resolution.get("runtime_binding_records") or []:
        value = str(values.get(str(record.get("env_name") or "")) or "")
        kind = str(record.get("value_kind") or "path")
        path = Path(value)
        try:
            valid = path.is_absolute() and path.exists() and os.access(path, os.R_OK)
            if kind == "directory_path":
                valid = valid and path.is_dir() and os.access(path, os.X_OK)
            elif kind == "executable_path":
                valid = valid and path.is_file() and os.access(path, os.X_OK)
            elif kind in {"file_path", "jar_path"}:
                valid = valid and path.is_file() and path.stat().st_size > 0
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise ActionRuntimeContractError(
                "RUNTIME_BINDING_PRELAUNCH_PATH_INVALID",
                binding_id=str(record.get("binding_id") or ""),
                value_kind=kind, execution_started=False,
                approved_value_digest=str(record.get("value_digest") or ""),
            )


def project_action_runtime_for_provider(resolution: Mapping[str, Any]) -> Dict[str, Any]:
    internal_contract = resolution.get("capability_contract") or {}
    contract = (
        internal_contract if isinstance(internal_contract, Mapping) else {}
    )
    capability_view: Dict[str, Any] = {}
    for field in LLM_CAPABILITY_FIELD_ALLOWLIST:
        value = contract.get(field)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, set)):
            capability_view[field] = [
                deepcopy(dict(item)) if isinstance(item, Mapping) else str(item)
                for item in value
                if isinstance(item, Mapping) or str(item).strip()
            ]
        else:
            capability_view[field] = str(value)
    concrete_values = dict(resolution.get("effective_runtime_bindings") or {})
    rows = []
    for raw in resolution.get("runtime_binding_records", []) or []:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        env_name = str(row.get("env_name") or "")
        value = str(concrete_values.get(env_name) or "")
        if not env_name or not value:
            continue
        rows.append({
            "binding_id": str(row.get("binding_id") or ""),
            "value": value,
            "value_kind": str(row.get("value_kind") or "runtime_value"),
            "availability": str(row.get("status") or "unavailable"),
            "exists": bool(Path(value).exists()) if os.path.isabs(value) else True,
            "executable": bool(os.path.isfile(value) and os.access(value, os.X_OK))
            if os.path.isabs(value) else False,
        })
    return {
        "current_action_capability": capability_view,
        "current_action_runtime_bindings": rows,
    }


def artifact_binding_id(semantic_handle: Any) -> str:
    handle = str(semantic_handle or "").strip().lower()
    handle = re.sub(r"^(?:input|output|plan)\.", "", handle)
    handle = re.sub(r"[^a-z0-9_.-]+", "_", handle).strip("._-") or "artifact"
    return "artifact." + handle


def build_artifact_env_manifest(
    artifact_environment: Sequence[Mapping[str, Any]],
    *,
    action_identity: Mapping[str, Any],
    selected_skill_id: str,
    execution_capability_id: str,
    capability_contract_hash_value: str,
) -> Dict[str, Any]:
    bindings: list[Dict[str, Any]] = []
    for raw in artifact_environment:
        if not isinstance(raw, Mapping):
            continue
        env_name = str(raw.get("env_var") or raw.get("env_name") or "")
        value = str(raw.get("placeholder") or raw.get("value") or "")
        if not _ENV_NAME_RE.fullmatch(env_name) or not value:
            continue
        bindings.append({
            "binding_id": artifact_binding_id(raw.get("semantic_handle")),
            "semantic_handle": str(raw.get("semantic_handle") or ""),
            "env_name": env_name,
            "direction": str(raw.get("direction") or "input"),
            "source": "managed_artifact_binding",
            "value_kind": str(raw.get("artifact_kind") or "path"),
            "sensitivity": "internal_path",
            "value_digest": value_digest(value),
            "status": "ready",
            "scope": "current_action",
        })
    identity = dict(action_identity or {})
    manifest: Dict[str, Any] = {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "action_id": str(identity.get("action_id") or ""),
        "plan_id": str(identity.get("plan_id") or ""),
        "step_id": str(identity.get("step_id") or ""),
        "selected_skill_id": str(selected_skill_id or ""),
        "execution_capability_id": str(execution_capability_id or ""),
        "capability_contract_hash": str(capability_contract_hash_value or ""),
        "scope": "current_action",
        "bindings": sorted(bindings, key=lambda row: (row["binding_id"], row["env_name"])),
    }
    manifest["manifest_hash"] = _manifest_hash(manifest)
    return manifest


def artifact_environment_values(
    artifact_environment: Sequence[Mapping[str, Any]],
) -> Dict[str, str]:
    """Legacy explicit-adapter helper; default Execute never calls it."""
    values: Dict[str, str] = {}
    for raw in artifact_environment:
        if not isinstance(raw, Mapping):
            continue
        env_name = str(raw.get("env_var") or raw.get("env_name") or "")
        value = str(raw.get("placeholder") or raw.get("value") or "")
        if _ENV_NAME_RE.fullmatch(env_name) and value:
            values[env_name] = value
    return dict(sorted(values.items()))


def build_action_execution_environment(
    base_environment: Mapping[str, Any],
    *,
    runtime_bindings: Mapping[str, Any],
    artifact_bindings: Mapping[str, Any],
    controller_bindings: Optional[Mapping[str, Any]] = None,
    managed_runtime_env_names: Iterable[str] = (),
) -> Dict[str, str]:
    """Legacy explicit-adapter environment builder; not the default Execute path.

    The ambient process environment remains the base execution environment, but
    every env name owned by the runtime registry is removed before the exact
    current-action bindings are applied.  This prevents a launcher or an older
    action from granting undeclared runtime capability through inheritance.
    """
    effective = {
        str(key): str(value)
        for key, value in base_environment.items()
        if str(key)
    }
    for raw_name in managed_runtime_env_names:
        name = str(raw_name or "").strip()
        if _ENV_NAME_RE.fullmatch(name):
            effective.pop(name, None)
    for bindings in (
        runtime_bindings,
        artifact_bindings,
        controller_bindings or {},
    ):
        for raw_name, raw_value in bindings.items():
            name = str(raw_name or "").strip()
            if _ENV_NAME_RE.fullmatch(name):
                effective[name] = str(raw_value)
    return effective


def build_exact_action_environment(
    base_environment: Mapping[str, Any],
    *,
    action_environment: Optional[Mapping[str, Any]] = None,
    controller_identity: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Build the subprocess environment without runtime/artifact materialization.

    Ordinary ambient variables are preserved. Only environment entries already
    present in the validated action and non-scientific Controller identity are
    added. Capability registries and artifact bindings are intentionally not
    accepted by this API, which prevents accidental reintroduction of the old
    injection chain.
    """
    effective = {
        str(key): str(value)
        for key, value in base_environment.items()
        if str(key)
    }
    for bindings in (action_environment or {}, controller_identity or {}):
        for raw_name, raw_value in bindings.items():
            name = str(raw_name or "").strip()
            if _ENV_NAME_RE.fullmatch(name):
                effective[name] = str(raw_value)
    return effective


def project_artifacts_for_provider(
    artifact_environment: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    allowed = (
        "semantic_handle", "role", "direction", "required", "format",
        "artifact_kind", "path", "expected_path", "exists", "readable",
        "parent_exists", "parent_writable", "input_instance",
    )
    return [
        {key: deepcopy(row.get(key)) for key in allowed if row.get(key) not in (None, "")}
        for row in artifact_environment
        if isinstance(row, Mapping) and str(row.get("semantic_handle") or "")
    ]


def project_action_capability_context_for_provider(
    resolution: Mapping[str, Any],
    artifact_environment: Sequence[Mapping[str, Any]] = (),
    workspace_bindings: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Project concrete current-action resources without control metadata."""
    from agent_core.workspace_attestation import provider_workspace_view

    return {
        **project_action_runtime_for_provider(resolution),
        "current_action_artifact_bindings": project_artifacts_for_provider(
            artifact_environment
        ),
        # Mutable resources are not outputs.  Keep their canonical identity,
        # allocator path, revision and evidence contract in a separate section.
        "current_action_workspace_bindings": provider_workspace_view(
            workspace_bindings
        ),
    }


def serialize_action_capability_instructions(
    provider_context: Mapping[str, Any],
    *,
    script_protocol: str = "",
) -> str:
    """Serialize only the already allowlisted LLM capability view.

    This deliberately reconstructs the three provider-facing sections rather
    than accepting an internal runtime resolution.  Stable JSON makes prompt
    audits and offline replays deterministic.
    """
    capability = provider_context.get("current_action_capability") or {}
    runtime_rows = provider_context.get("current_action_runtime_bindings") or []
    artifact_rows = provider_context.get("current_action_artifact_bindings") or []
    workspace_rows = provider_context.get("current_action_workspace_bindings") or []
    payload: Dict[str, Any] = {
        "current_action_capability": {
            field: deepcopy(capability.get(field))
            for field in LLM_CAPABILITY_FIELD_ALLOWLIST
            if isinstance(capability, Mapping)
            and capability.get(field) not in (None, "", [], {})
        },
        "current_action_runtime_bindings": [
            {
                field: deepcopy(row.get(field))
                for field in (
                    "binding_id", "value", "value_kind", "availability",
                    "exists", "executable",
                )
                if row.get(field) not in (None, "")
            }
            for row in runtime_rows
            if isinstance(row, Mapping)
        ],
        "current_action_artifact_bindings": [
            {
                field: deepcopy(row.get(field))
                for field in (
                    "semantic_handle", "role", "direction", "required", "format",
                    "artifact_kind", "path", "expected_path", "exists", "readable",
                    "parent_exists", "parent_writable", "input_instance",
                )
                if row.get(field) not in (None, "")
            }
            for row in artifact_rows
            if isinstance(row, Mapping)
        ],
        "current_action_workspace_bindings": [
            deepcopy(dict(row))
            for row in workspace_rows
            if isinstance(row, Mapping)
            and str(row.get("canonical_resource_id") or "")
            and str(row.get("formal_concrete_path") or "")
        ],
    }
    if str(script_protocol or "").strip():
        payload["script_protocol"] = str(script_protocol).strip()
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_execution_policy_manifest(
    *,
    action_identity: Mapping[str, Any],
    working_directory: str,
    language: str,
    policy: Optional[Mapping[str, Any]] = None,
    skill_resources: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    policy_value = {
        "stdin": "devnull",
        "process_group": "isolated",
        "shell_errexit": False,
        "shell_pipefail": True,
        **dict(policy or {}),
    }
    identity = dict(action_identity or {})
    manifest: Dict[str, Any] = {
        "schema_version": EXECUTION_POLICY_MANIFEST_SCHEMA_VERSION,
        "action_id": str(identity.get("action_id") or ""),
        "plan_id": str(identity.get("plan_id") or ""),
        "step_id": str(identity.get("step_id") or ""),
        "working_directory": str(Path(working_directory).expanduser().resolve()),
        **({"skill_resources": deepcopy(list(skill_resources))} if skill_resources else {}),
        "language": str(language or "Bash"),
        "scope": "current_action",
        "policy": policy_value,
    }
    manifest["manifest_hash"] = _manifest_hash(manifest)
    return manifest


def build_action_approval_identity(
    *,
    generated_script_sha256: str,
    capability_contract_hash_value: str,
    runtime_env_manifest_hash: str,
    artifact_env_manifest_hash: str,
    execution_policy_manifest_hash: str,
    working_directory: str,
) -> Dict[str, Any]:
    identity = {
        "schema_version": ACTION_APPROVAL_IDENTITY_SCHEMA_VERSION,
        "generated_script_sha256": str(generated_script_sha256 or ""),
        "capability_contract_hash": str(capability_contract_hash_value or ""),
        "runtime_env_manifest_hash": str(runtime_env_manifest_hash or ""),
        "artifact_env_manifest_hash": str(artifact_env_manifest_hash or ""),
        "execution_policy_manifest_hash": str(execution_policy_manifest_hash or ""),
        "working_directory": str(Path(working_directory).expanduser().resolve()),
    }
    identity["approval_identity_hash"] = stable_digest(identity)
    return identity


def validate_action_approval_identity(
    approved: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> None:
    fields = (
        "generated_script_sha256", "capability_contract_hash",
        "runtime_env_manifest_hash", "artifact_env_manifest_hash",
        "execution_policy_manifest_hash", "working_directory",
    )
    drift = {
        field: {"approved": str(approved.get(field) or ""), "observed": str(observed.get(field) or "")}
        for field in fields
        if str(approved.get(field) or "") != str(observed.get(field) or "")
    }
    approved_hash = str(approved.get("approval_identity_hash") or "")
    observed_hash = str(observed.get("approval_identity_hash") or "")
    if not approved_hash or approved_hash != observed_hash or drift:
        code = (
            "RUNTIME_BINDING_SNAPSHOT_DRIFT"
            if "runtime_env_manifest_hash" in drift
            else "ACTION_APPROVAL_IDENTITY_DRIFT"
        )
        raise ActionRuntimeContractError(
            code,
            changed_fields=sorted(drift),
            drift=drift,
            approved_identity_hash=approved_hash,
            observed_identity_hash=observed_hash,
        )


def assert_current_action_runtime_scope(action: Mapping[str, Any]) -> None:
    required = (
        "execution_capability_id",
        "capability_contract_hash",
        "runtime_binding_manifest_hash",
    )
    missing = [field for field in required if not str(action.get(field) or "")]
    if missing:
        raise ActionRuntimeContractError(
            "LEGACY_RUNTIME_SCOPE_REQUIRES_REGENERATION",
            action_id=str(action.get("action_id") or ""),
            step_id=str(action.get("step_id") or ""),
            missing_fields=missing,
            required_action="regenerate_and_review_current_action",
        )

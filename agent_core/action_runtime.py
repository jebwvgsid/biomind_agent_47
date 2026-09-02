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
    return {
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
    if isinstance(state_or_audits, Mapping):
        audits = state_or_audits.get("runtime_audit", []) or []
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


def project_action_runtime_for_provider(resolution: Mapping[str, Any]) -> Dict[str, Any]:
    contract = dict(resolution.get("capability_contract") or {})
    rows = [
        {
            "binding_id": str(row.get("binding_id") or ""),
            "shell_reference": "$" + str(row.get("env_name") or ""),
            "availability": str(row.get("status") or "unavailable"),
            "source_kind": "verified_runtime",
        }
        for row in resolution.get("runtime_binding_records", []) or []
        if isinstance(row, Mapping) and str(row.get("env_name") or "")
    ]
    return {
        "current_action_capability": {
            "capability_id": str(contract.get("capability_id") or ""),
            "contract_hash": str(resolution.get("capability_contract_hash") or ""),
        },
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
    """Build one subprocess environment without leaking another action's runtime.

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


def project_artifacts_for_provider(
    artifact_environment: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    return [
        {
            "binding_id": artifact_binding_id(row.get("semantic_handle")),
            "shell_reference": "$" + str(row.get("env_var") or ""),
            "direction": str(row.get("direction") or "input"),
            "availability": "ready",
        }
        for row in artifact_environment
        if isinstance(row, Mapping) and _ENV_NAME_RE.fullmatch(str(row.get("env_var") or ""))
    ]


def build_execution_policy_manifest(
    *,
    action_identity: Mapping[str, Any],
    working_directory: str,
    language: str,
    policy: Optional[Mapping[str, Any]] = None,
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

"""Formal command execution and historical capability runtime contracts.

Ordinary work uses explicit parameters, frozen sources and the shared runner.
The versioned capability helpers below remain available to historical actions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


RUNTIME_MANIFEST_SCHEMA_VERSION = 1
ARTIFACT_MANIFEST_SCHEMA_VERSION = 1
EXECUTION_POLICY_MANIFEST_SCHEMA_VERSION = 1
ACTION_APPROVAL_IDENTITY_SCHEMA_VERSION = 1
GENERIC_EXECUTION_CAPABILITY_ID = "framework.generic.execute"
GENERIC_EXECUTION_TOOLS = frozenset({"bash", "python"})


def generic_execution_capability() -> Dict[str, Any]:
    """The existing controlled executor, explicitly selected without a Skill.

    This contract grants no ambient resources or runtime bindings. The approved
    step owns resource use and observable effects; action approval and the
    existing execution/evidence boundaries still apply. Unknown IDs never use
    this route implicitly.
    """
    return normalize_capability_contract({
        "capability_id": GENERIC_EXECUTION_CAPABILITY_ID,
        "selected_skill_id": "",
        "operation": "execute",
        "contract_source": "framework.controlled_executor",
        "contract_revision": "generic-execution-v1",
        "supported_tools": sorted(GENERIC_EXECUTION_TOOLS),
        "required_runtime_binding_ids": [],
        "optional_runtime_binding_ids": [],
        "required_artifact_binding_ids": [],
        "execution_guidance": [
            "Explicit generic execution through the existing code approval and executor; select tool bash or python.",
            "Declare exact resource uses and actual observable results in the Plan. A read-only check may have outputs=[] and typed observations.",
            "No domain helper or runtime availability is implied. Use approved resources only; missing dependencies require evidence, correction or formal blocked/stop.",
            "This route does not provide an arbitrary-system-call OS sandbox.",
        ],
    })

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
    "supported_tools",
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


class PrelaunchAdmissionRejected(ActionRuntimeContractError):
    """Host preparation rejection certified inside the exclusive, pre-delegation scope."""


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
    if "supported_tools" in value:
        normalized["supported_tools"] = _string_ids(value["supported_tools"])
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
        if applicability.get("artifact_binding_requirements"):
            normalized["binding_applicability"]["artifact_binding_requirements"] = deepcopy(
                dict(applicability["artifact_binding_requirements"])
            )
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
        "source": "registered_runtime" if audit.get("runtime_source") == "registered_runtime" else "verified_runtime_audit",
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
    # A derived path never overrules an explicit fact outcome. Expired/future
    # observations have no eligible evidence_ids, but remain declarations of
    # this fact; they cannot be renewed by a directory-derived filename.
    for binding_id, evaluation in evaluations.items():
        availability = evaluation.get("availability")
        if availability in {"UNAVAILABLE", "AMBIGUOUS"} or (
            availability == "UNKNOWN" and (
                evaluation.get("evidence_ids")
                or evaluation.get("reason_code") == "evidence_expired_or_future"
            )
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


def freeze_execution_environment(
    base_environment: Mapping[str, Any],
    action_environment: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Freeze safe launch defaults into the existing validated action.env.

    Ambient credentials and arbitrary launcher variables are not copied. The
    explicit action bindings are already part of the reviewed action contract.
    Reusing this return value for subsequent argv/cwd/env preparation makes a
    later launcher export irrelevant to this accepted action.
    """
    effective = {
        "PATH": str(base_environment.get("PATH") or "/usr/bin:/bin"),
        "LC_ALL": str(base_environment.get("LC_ALL") or "C.UTF-8"),
    }
    if base_environment.get("TMPDIR"):
        effective["TMPDIR"] = str(base_environment["TMPDIR"])
    for raw_name, raw_value in (action_environment or {}).items():
        name = str(raw_name)
        value = str(raw_value)
        if not _ENV_NAME_RE.fullmatch(name) or "\x00" in value:
            raise ActionRuntimeContractError("ACTION_ENV_SNAPSHOT_INVALID", env_name=name)
        effective[name] = value
    prefix = str((action_environment or {}).get("ENV_PREFIX") or (action_environment or {}).get("CONDA_PREFIX") or "")
    if prefix:
        if not Path(prefix).is_absolute():
            raise ActionRuntimeContractError("ACTION_ENV_SNAPSHOT_PATH_INVALID")
        effective["CONDA_PREFIX"] = prefix
        target_bin=str(Path(prefix) / 'bin')
        effective['PATH']=os.pathsep.join([target_bin,*[p for p in effective['PATH'].split(os.pathsep) if p!=target_bin]])
    # PATH/TMPDIR are explicit locations; empty/relative PATH components would
    # reintroduce a dependence on a later process working directory.
    if any(not part or not Path(part).is_absolute() for part in effective["PATH"].split(os.pathsep)):
        raise ActionRuntimeContractError("ACTION_ENV_SNAPSHOT_PATH_INVALID")
    if effective.get("TMPDIR") and not Path(effective["TMPDIR"]).is_absolute():
        raise ActionRuntimeContractError("ACTION_ENV_SNAPSHOT_TMPDIR_INVALID")
    if any("\x00" in value for value in effective.values()):
        raise ActionRuntimeContractError("ACTION_ENV_SNAPSHOT_INVALID")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", effective["LC_ALL"]):
        raise ActionRuntimeContractError("ACTION_ENV_SNAPSHOT_LOCALE_INVALID")
    return dict(sorted(effective.items()))


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


def resolve_execution_runner(language: str, environment: Mapping[str, Any]) -> str:
    """Select the existing runner once, using the reviewed launch environment."""
    import shutil
    import sys
    if language == "Python":
        prefix = str(environment.get("ENV_PREFIX") or environment.get("CONDA_PREFIX") or "")
        if prefix:
            runner = Path(prefix) / "bin" / "python"
            if not runner.is_absolute() or not runner.is_file() or not os.access(runner, os.X_OK):
                raise ActionRuntimeContractError("EXECUTION_RUNNER_UNAVAILABLE", runner=str(runner))
            return str(runner)
        return str(Path(sys.executable).absolute())
    name = "bash" if language == "Bash" else "Rscript"
    selected = shutil.which(name, path=str(environment.get("PATH") or "/usr/bin:/bin"))
    if not selected:
        raise ActionRuntimeContractError("EXECUTION_RUNNER_UNAVAILABLE", runner=name)
    return str(Path(selected).absolute())


def build_execution_policy_manifest(
    *,
    action_identity: Mapping[str, Any],
    working_directory: str,
    language: str,
    policy: Optional[Mapping[str, Any]] = None,
    skill_resources: Sequence[Mapping[str, Any]] = (),
    evidence_root: str = "",
    runner_executable: str = "",
    registered_preflight_evidence: Optional[Mapping[str, Any]] = None,
    output_write_guard: Optional[Mapping[str, Any]] = None,
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
    if runner_executable:
        if not Path(runner_executable).is_absolute():
            raise ActionRuntimeContractError("EXECUTION_RUNNER_PATH_INVALID")
        manifest["runner_executable"] = runner_executable
    if output_write_guard is not None:
        manifest["output_write_guard"] = deepcopy(dict(output_write_guard))
    if evidence_root and any(row.get("handle") in {
        "genomics/wgs-short-read-mapping#scripts/inspect_fastq_batch.py",
        "genomics/wgs-short-read-mapping#scripts/inspect_wgs_inputs.py",
    } for row in skill_resources):
        from agent_core.approved_resource_locator import approved_resource_locator
        root = approved_resource_locator({"canonical_path": evidence_root})["path"]
        manifest["evidence_binding"] = {"environment_variable": "BIOMIND_PREFLIGHT_EVIDENCE_DIR",
            "approved_root": root, "leaf_rule": "current_attempt/preflight",
            "creator": "helper", "purpose": "internal_execution_evidence"}
        if registered_preflight_evidence:
            row = dict(registered_preflight_evidence)
            if row.get("semantic_handle") != "plan.input_preflight_evidence" or not row.get("expected_path"):
                raise ActionRuntimeContractError("PREFLIGHT_REGISTERED_BINDING_INVALID")
            exact = approved_resource_locator({"canonical_path": row["expected_path"]})["path"]
            manifest["evidence_binding"].update(leaf_rule="registered_exact_path",
                canonical_path=exact, semantic_handle="plan.input_preflight_evidence",
                authority="controller_output_bindings", purpose="formal_plan_evidence")
    manifest["manifest_hash"] = _manifest_hash(manifest)
    return manifest


def registered_preflight_output(action):
    """Read the exact host-bound output; no guessing by filename or artifact text."""
    if action.get('execution_protocol_version') == 3:
        return None  # Optional legacy helper binding only; actual preflight is task work.
    rows = [row for row in action.get("controller_output_bindings", [])
            if isinstance(row, Mapping) and row.get("semantic_handle") == "plan.input_preflight_evidence"]
    if len(rows) > 1:
        raise ActionRuntimeContractError("PREFLIGHT_REGISTERED_BINDING_AMBIGUOUS")
    return dict(rows[0]) if rows else None


def _output_path_state(path: Path) -> Dict[str, Any]:
    """Metadata only; do not read scientific files to prepare an approval."""
    parents = []
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ActionRuntimeContractError("OUTPUT_PARENT_UNSAFE", path=str(parent))
        parents.append([str(parent), info.st_dev, info.st_ino])
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "parents": parents}
    if not stat.S_ISREG(info.st_mode):
        raise ActionRuntimeContractError("OUTPUT_TARGET_NOT_REGULAR", path=str(path))
    return {"exists": True, "parents": parents, "device": info.st_dev,
            "inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "mode": info.st_mode}


def capture_output_write_guard(contracts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Freeze declared file targets before candidate persistence and review.

    Workspace directory mutations retain their existing attestation contract.
    This guards declared outputs; it is not an OS sandbox for arbitrary code.
    """
    rows = []
    seen = set()
    for contract in contracts:
        if str(contract.get("artifact_kind") or "file") == "directory":
            continue
        raw = str(contract.get("expected_path") or "")
        path = Path(os.path.abspath(raw))
        if not raw or not Path(raw).is_absolute():
            raise ActionRuntimeContractError("OUTPUT_TARGET_PATH_NOT_CONCRETE", path=raw)
        if str(path) in seen:
            continue
        seen.add(str(path))
        before = _output_path_state(path)
        rows.append({"path": str(path), "artifact_id": str(contract.get("artifact_id") or ""),
                     "before": before, "backup_path": str(path.parent / (".biomind-backup-" + uuid.uuid4().hex))
                     if before["exists"] else ""})
    value = {"schema_version": 1, "policy": "preserve_existing_files", "targets": rows}
    value["guard_hash"] = stable_digest(value)
    return value


def validate_output_write_guard(guard: Mapping[str, Any], contracts=None) -> None:
    payload = {k: v for k, v in guard.items() if k != "guard_hash"}
    if not guard.get("guard_hash") or stable_digest(payload) != guard["guard_hash"]:
        raise ActionRuntimeContractError("OUTPUT_WRITE_GUARD_INVALID")
    if contracts is not None:
        expected = {os.path.abspath(str(c.get("expected_path") or "")) for c in contracts
                    if str(c.get("artifact_kind") or "file") != "directory"}
        if expected != {r["path"] for r in guard["targets"]}:
            raise ActionRuntimeContractError("OUTPUT_WRITE_SCOPE_CHANGED")
    for row in guard["targets"]:
        backup = str(row.get("backup_path") or "")
        if row["before"]["exists"] and (
                Path(backup).parent != Path(row["path"]).parent
                or not re.fullmatch(r"\.biomind-backup-[0-9a-f]{32}", Path(backup).name)):
            raise ActionRuntimeContractError("OUTPUT_BACKUP_PATH_INVALID", path=backup)
        if _output_path_state(Path(row["path"])) != row["before"]:
            raise ActionRuntimeContractError("OUTPUT_TARGET_CHANGED_SINCE_REVIEW", path=row["path"],
                required_action="inspect_current_target_and_request_fresh_approval")


def _reviewed_parent_fd(row):
    """Walk reviewed directory identities without following symlinks."""
    expected = {path: (device, inode) for path, device, inode in row["before"]["parents"]}
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path("/")
    try:
        for name in Path(row["path"]).parent.parts[1:]:
            info = os.fstat(descriptor)
            if expected.get(str(current)) != (info.st_dev, info.st_ino):
                raise ActionRuntimeContractError("OUTPUT_PARENT_CHANGED", path=str(current))
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child; current /= name
        info = os.fstat(descriptor)
        if expected.get(str(current)) != (info.st_dev, info.st_ino):
            raise ActionRuntimeContractError("OUTPUT_PARENT_CHANGED", path=str(current))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def preserve_approved_output_files(guard: Mapping[str, Any], *, decision: str,
                                   approval_identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy the exact reviewed old bytes exclusively; never rename/delete them.

    Called only after human approval, then rechecked immediately before launch.
    If copy or validation fails the scientific command is not dispatched.
    """
    validate_output_write_guard(guard)
    existing = [r for r in guard["targets"] if r["before"]["exists"]]
    if existing and decision != "approve_backup_replace":
        raise ActionRuntimeContractError("OUTPUT_REPLACEMENT_NOT_APPROVED",
            paths=[r["path"] for r in existing])
    receipt = {"schema_version": 1, "guard_hash": guard["guard_hash"], "decision": decision,
               "approval_identity_hash": approval_identity.get("approval_identity_hash", ""),
               "backups": []}
    for row in existing:
        digest = hashlib.sha256()
        parent_fd = _reviewed_parent_fd(row)
        try:
            fd = os.open(Path(row["path"]).name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != tuple(
                        row["before"][k] for k in ("device", "inode", "size", "mtime_ns", "ctime_ns")):
                    raise ActionRuntimeContractError("OUTPUT_TARGET_CHANGED_SINCE_REVIEW", path=row["path"])
                try:
                    backup_fd = os.open(Path(row["backup_path"]).name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                except FileExistsError:
                    # A crash may leave a completed backup before its receipt
                    # is saved. Reuse only this guard's exact backup, after a
                    # full comparison to the still-unchanged reviewed source.
                    # Partial/tampered copies remain untouched and cannot admit
                    # execution. This does not upgrade any scientific evidence.
                    backup_fd = os.open(Path(row["backup_path"]).name,
                        os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    with os.fdopen(backup_fd, "rb") as backup:
                        info = os.fstat(backup.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size != before.st_size:
                            raise ActionRuntimeContractError("OUTPUT_BACKUP_CHANGED")
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            if backup.read(len(chunk)) != chunk:
                                raise ActionRuntimeContractError("OUTPUT_BACKUP_CHANGED")
                            digest.update(chunk)
                        if backup.read(1):
                            raise ActionRuntimeContractError("OUTPUT_BACKUP_CHANGED")
                else:
                    with os.fdopen(backup_fd, "wb") as backup:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            backup.write(chunk)
                            digest.update(chunk)
                        backup.flush()
                        os.fsync(backup.fileno())
                os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        validate_output_write_guard(guard)
        receipt["backups"].append({"original_path": row["path"], "backup_path": row["backup_path"],
                                   "size": row["before"]["size"], "sha256": digest.hexdigest()})
    return receipt


def resolve_preflight_evidence_binding(manifest, *, approved_root, attempt_id, action_environment):
    """Resolve the approved policy to the existing attempt; never create a leaf.

    This is execution-policy materialization, not a user path or Plan binder.
    The original body and approval digest remain unchanged.
    """
    binding = manifest.get("evidence_binding")
    if not binding:
        return {}
    validate_manifest_hash(manifest)
    from agent_core.approved_resource_locator import approved_resource_locator
    root = approved_resource_locator({"canonical_path": str(approved_root)})["path"]
    expected = {"environment_variable": "BIOMIND_PREFLIGHT_EVIDENCE_DIR",
        "approved_root": root, "leaf_rule": "current_attempt/preflight",
        "creator": "helper", "purpose": "internal_execution_evidence"}
    registered = binding.get("leaf_rule") == "registered_exact_path"
    if registered:
        exact = approved_resource_locator({"canonical_path": binding.get("canonical_path", "")})["path"]
        expected.update(leaf_rule="registered_exact_path", canonical_path=exact,
            semantic_handle="plan.input_preflight_evidence", authority="controller_output_bindings",
            purpose="formal_plan_evidence")
    if binding != expected:
        raise ActionRuntimeContractError("PREFLIGHT_EVIDENCE_BINDING_DRIFT")
    if binding["environment_variable"] in action_environment:
        raise ActionRuntimeContractError("PREFLIGHT_EVIDENCE_ENV_RESERVED")
    if not str(attempt_id).startswith("attempt_") or Path(attempt_id).name != attempt_id:
        raise ActionRuntimeContractError("PREFLIGHT_ATTEMPT_ID_INVALID")
    leaf = Path(binding["canonical_path"]) if registered else Path(root) / attempt_id / "preflight"
    checked = approved_resource_locator({"canonical_path": str(leaf)})["path"]
    if Path(checked).exists():
        raise ActionRuntimeContractError("PREFLIGHT_EVIDENCE_ALREADY_EXISTS")
    return {**binding, "attempt_id": attempt_id, "canonical_path": checked,
            "action_id": manifest["action_id"], "policy_manifest_hash": manifest["manifest_hash"]}


def build_action_approval_identity(
    *,
    generated_script_sha256: str,
    capability_contract_hash_value: str,
    runtime_env_manifest_hash: str,
    artifact_env_manifest_hash: str,
    execution_policy_manifest_hash: str,
    working_directory: str,
    execution_environment: Optional[Mapping[str, Any]] = None,
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
    if execution_environment is not None:
        identity["execution_environment_hash"] = stable_digest(dict(sorted(
            (str(key), str(value)) for key, value in execution_environment.items())))
    identity["approval_identity_hash"] = stable_digest(identity)
    return identity


def validate_action_approval_identity(
    approved: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> None:
    fields = (
        "generated_script_sha256", "capability_contract_hash",
        "runtime_env_manifest_hash", "artifact_env_manifest_hash",
        "execution_policy_manifest_hash", "working_directory", "execution_environment_hash",
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


def registered_execution_environment(state: Mapping[str, Any], explicit: Mapping[str, Any], *, runtime_binding=None) -> Dict[str, str]:
    """Materialize accepted registration for this step, without a fresh probe.

    An explicit reviewed environment stays explicit. Ambiguous task bindings do
    not fall back silently to the host Python interpreter.
    """
    environment = {str(k): str(v) for k, v in explicit.items()}
    task = state.get("task_state") or {}
    if runtime_binding:
        from agent_core.runtime_evidence import registered_execution_binding
        registered_execution_binding(state,binding_ref=runtime_binding.get('binding_ref',''),runtime_scope=runtime_binding.get('scope',''))
        # Domain already resolved the shared launch, including runner-only
        # records. Reapplying it here would double-wrap command and PATH.
        return environment
    explicit_prefix = environment.get("ENV_PREFIX") or environment.get("CONDA_PREFIX")
    if explicit_prefix:
        # Prior dependency errors remain observations. They do not mandate a
        # diagnosis before the model can choose another authorized command.
        return environment
    if task.get('pi_request_id'):
        from agent_core.runtime_evidence import registered_execution_binding
        binding=registered_execution_binding(state)
        if binding:
            from agent_core.runtime_launch import prepare_runtime_launch
            # This compatibility projection handles only environment facts.
            # Domain prepares the command/runner once before permission hashing.
            descriptor=dict(binding['launch']);descriptor.pop('env_runner',None);descriptor.pop('runner_argv',None)
            launch=prepare_runtime_launch(descriptor,['/bin/true'],base_env={'PATH':os.environ.get('PATH','/usr/bin:/bin')},extra_env=environment)
            return launch['env']
        return environment
    if not task.get("runtime_evidence"):
        return environment
    from agent_core.runtime_evidence import project_registered_runtime_scopes
    step = next((item for item in state.get("steps") or []
                 if item.get("step_id") == state.get("current_step_id")), {})
    selected = list(step.get("skill_ids") or [])
    for key in ("selected_skill_id", "skill_id"):
        if step.get(key):
            selected.append(str(step[key]))
    audits, evaluations = project_registered_runtime_scopes(task, task_id=state.get("task_id"), skill_ids=selected)
    registered = [row for row in audits if row.get("runtime_source") == "registered_runtime"]
    prefixes = {str(row["resolved_runtime_paths"].get("RESOLVED_ENV_PREFIX") or
                    row["resolved_runtime_paths"].get("ENV_PREFIX") or "") for row in registered} - {""}
    if any((value.get("runtime.environment.prefix") or {}).get("availability") in {"AMBIGUOUS", "UNAVAILABLE"}
           for value in evaluations) or len(prefixes) > 1:
        raise ActionRuntimeContractError("REGISTERED_EXECUTION_ENVIRONMENT_UNRESOLVED")
    if len(prefixes) == 1:
        environment["ENV_PREFIX"] = next(iter(prefixes))
    return environment


async def execute_formal_command(service, arguments, *, task_id, call_id, stage='generate',
                                 runtime_observation=None, cancel_event=None, original_deadline=None):
    """Command-tool adapter to the SAME formal runner and PlanTracker lineage.

    Identity is the persisted tool intent, never the content hash. Holding its
    file lock prevents two restored callers launching it together. A persisted
    exit receipt is replayed even if the tool-message checkpoint was lost.
    """
    import asyncio
    import fcntl
    import shlex
    import socket
    import time
    from agent_core.plan_tracker import (load_plan_state, save_plan_state,
        record_provenance_event, classify_attempt_resume, canonical_task_spec_for_state, resolve_work_output)
    from agent_core.request_state import request_scope, current_finish
    from execution.executor import run_command, get_last_execution_metadata
    from execution.script_validation import check_executable_action, resolve_direct_script_argv, explicit_script_diagnostic

    if not isinstance(arguments, Mapping) or set(arguments) - {
        'cmd', 'argv', 'workdir', 'env', 'timeout_ms', 'max_output_chars', 'script_path', 'helper_paths', 'expected_sha256', 'legacy_action', 'script_args', 'interpreter', 'write_paths', 'delete_paths', 'read_paths', '_biomind_probe', '_runtime_binding'}:
        raise ValueError('EXEC_COMMAND_ARGUMENTS_INVALID')
    if not call_id:
        raise ValueError('EXECUTION_INTENT_ID_MISSING')
    durable = load_plan_state(service.data_path, task_id)
    observation = runtime_observation if runtime_observation is not None else {}
    observation['launch_delegated'] = False
    observation['preparation_settlement'] = {'safe_to_release':True,
        'phase':'permission_admission','effects':[],
        'boundary':'before_preparation_and_launch'}
    def check_cancelled():
        current = load_plan_state(service.data_path, task_id)
        if ((cancel_event is not None and cancel_event.is_set())
                or (current.get('task_state') or {}).get('status') == 'cancelled'):
            raise asyncio.CancelledError()
    check_cancelled()
    scope = request_scope(durable)
    review = (durable.get('task_state') or {}).get('result_review') or {}
    if review.get('request_scope') == scope:
        raise ValueError('RESULT_REVIEW_FORBIDS_EXECUTION')
    intent_id = hashlib.sha256(json.dumps([scope, call_id], sort_keys=True).encode()).hexdigest()[:24]
    action_id, attempt_id = 'action-' + intent_id, 'attempt-' + intent_id
    attempt_dir = Path(service.data_path).absolute() / 'task_outputs' / task_id / 'attempts' / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = attempt_dir / 'exit_status.json'
    args_hash = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    lock = (attempt_dir / 'intent.lock').open('a+')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            from agent_core.execution_permissions import (request_permission, command_payload,
                                                          PermissionPending, derive_permission_call_id)
            waiting = request_permission(service.data_path, task_id,
                derive_permission_call_id(call_id, 'command'),
                command_payload(service, arguments, task_id, call_id=call_id))
            if waiting['status'] == 'pending':
                raise PermissionPending(waiting)
            return {'status': 'running', 'attempt_id': attempt_id, 'exit_code': None,
                'receipt_ref': str(receipt_path), 'replayed': True}
        # A receipt is the launch fact; missing chat text cannot invalidate it.
        if receipt_path.is_file():
            result = json.loads(receipt_path.read_text())
            if result.get('arguments_hash') != args_hash:
                raise ValueError('EXECUTION_INTENT_ARGUMENTS_CHANGED')
            _record_formal_receipt(service.data_path, task_id, result)
            return {**result, 'replayed': True}
        prior = (durable.get('provenance_state') or {}).get('attempts', {}).get(attempt_id)
        if prior:
            original = (durable.get('provenance_state') or {}).get('actions', {}).get(action_id, {}).get('normalized_action') or {}
            if original.get('arguments_hash') != args_hash:
                raise ValueError('EXECUTION_INTENT_ARGUMENTS_CHANGED')
            resume = classify_attempt_resume(durable, step_id=prior.get('step_id'), action_id=action_id)
            return {'status': 'running' if resume.get('route') == 'monitor_running_attempt' else 'reconciliation_required',
                'attempt_id': attempt_id, 'exit_code': None, 'resume': resume, 'replayed': True}
        for previous_attempt in (durable.get('provenance_state') or {}).get('attempts', {}).values():
            if (previous_attempt.get('status') in {'STARTING', 'RUNNING', 'UNKNOWN_AFTER_CRASH'}):
                raise ValueError('PRIOR_PROCESS_REQUIRES_MONITOR_OR_RECONCILIATION')

        # Only reached after exclusive intent ownership, replay and prior-run checks.
        # This is host control-flow evidence, not an execution_started default.
        observation['preparation_settlement'] = {
            'safe_to_release': True, 'phase': 'scoped_preparation',
            'effects': [{'kind': 'attempt_control_files', 'path': str(attempt_dir),
                        'retained': True, 'rolled_back': False}],
            'boundary': 'exclusive_intent_before_starting_and_delegation'}
        from agent_core.execution_permissions import (authorize_command, claim_permission,
            validate_permit, execution_context, command_payload, request_allows_command, digest as permission_digest)
        permission = await authorize_command(service, arguments, task_id=task_id,
            call_id=call_id, cancel_event=cancel_event)
        if permission['payload'].get('access_projection') is not None:
            from agent_core.command_execution import formal_protection_capability
            formal_protection_capability(permission['payload']['access_projection'])
        durable = load_plan_state(service.data_path, task_id)
        check_cancelled()
        if current_finish(durable):
            raise PrelaunchAdmissionRejected('CURRENT_REQUEST_ALREADY_ENDED', phase='dynamic_admission')
        if observation.get('expected_request_scope') is not None and observation['expected_request_scope'] != scope:
            raise PrelaunchAdmissionRejected('FORMAL_REQUEST_SCOPE_CHANGED', phase='dynamic_admission')
        admission = (durable.get('task_state') or {}).get('intent_admission') or {}
        if not request_allows_command(durable,arguments) or admission.get('execution_scope') not in {'simple', 'managed'}:
            raise PrelaunchAdmissionRejected('CURRENT_REQUEST_HAS_NO_EXECUTION_AUTHORIZATION', phase='dynamic_admission')
        approval = durable.get('current_approval') or {}
        requested_review_done = approval.get('status') == 'approved' and approval.get('plan_snapshot_ref') == durable.get('plan_snapshot_ref')
        if admission.get('review_before_execution') and not requested_review_done:
            raise PrelaunchAdmissionRejected('USER_REQUESTED_REVIEW_BEFORE_EXECUTION', phase='dynamic_admission')
        project = Path(service.project_for_task(task_id)).absolute()
        cwd = Path(arguments.get('workdir') or project).expanduser()
        if not cwd.is_absolute():
            cwd = project / cwd
        try:
            cwd = cwd.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            raise PrelaunchAdmissionRejected('EXECUTION_CWD_NOT_DIRECTORY', phase='dynamic_admission')
        if not cwd.is_dir():
            raise PrelaunchAdmissionRejected('EXECUTION_CWD_NOT_DIRECTORY', phase='dynamic_admission')
        from agent_core.work_outputs import file_fingerprint
        from agent_core.work_plan import action_targets, authority_identity
        output_rows = action_targets(durable, arguments, project,command_service=service,task_id=task_id)
        step = {}
        draft = ((durable.get('task_state') or {}).get('generate_context_episode') or {}).get('draft_file') or {}
        draft_scope = draft.get('scope') or {}
        current_draft = (draft.get('status') == 'validated' and draft.get('content_ref')
            and all(draft_scope.get(k) == v for k,v in scope.items()))
        selected_source, selected_sha = None, None
        if current_draft:
            if Path(str(draft.get('path') or '')).suffix.lower() in {'.py','.sh','.r'}:
                selected_source, selected_sha = draft['path'], draft.get('sha256')
            elif Path(str(draft.get('path') or '')).suffix.lower() == '.json':
                from agent_core.state_contract_store import StateContractStore
                saved = StateContractStore(service.data_path,task_id).read_object(draft['content_ref'], expected_type='draft_candidate_content')
                selected = json.loads(saved['content'])
                if isinstance(selected,dict) and selected.get('script_path'):
                    selected_source, selected_sha = selected['script_path'], selected.get('expected_sha256')
        legacy = arguments.get('legacy_action') or {}
        host_generated_source = None
        actual_arguments = dict(arguments)
        actual_arguments.pop('_runtime_binding',None)
        # Resource references belong to the permission projection, never to
        # the strict executable carrier/argv contract.
        actual_arguments.pop('read_paths', None)
        if legacy:
            from agent_core.step_effects import executable_carrier
            carrier = executable_carrier(legacy)
            checked = check_executable_action(legacy)
            if checked.get('required') and checked.get('check_status') != 'PASS':
                return {'status': 'not_run', 'syntax_check': checked, 'execution_started': False}
            if legacy.get('script_path') and not carrier.get('argv') and not carrier.get('script_available'):
                actual_arguments.update({k:v for k,v in legacy.items() if k in {'script_path','script_args','interpreter','helper_paths','expected_sha256'}})
            elif carrier.get('argv'):
                actual_arguments['argv'] = list(carrier['argv'])
                for field in ('script_path', 'helper_paths', 'expected_sha256'):
                    if field in legacy:
                        actual_arguments[field] = legacy[field]
            elif carrier.get('script_available'):
                language = carrier['language']
                interpreter, suffix = {'bash': ('bash', 'sh'), 'shell': ('bash', 'sh'), 'python': ('python', 'py'), 'r': ('Rscript', 'R')}[language]
                if selected_source and selected_sha == hashlib.sha256(carrier['body'].encode()).hexdigest():
                    actual_arguments.update(script_path=selected_source, script_args=[], interpreter=interpreter, expected_sha256=selected_sha)
                else:
                    inline = attempt_dir / ('inline.' + suffix)
                    observation['preparation_settlement']['effects'].append({'kind':'inline_source', 'path':str(inline), 'retained':True})
                    _output_path_state(inline)
                    if inline.exists() and inline.read_text() != carrier['body']:
                        raise ValueError('INLINE_SOURCE_VERSION_CHANGED')
                    if not inline.exists():
                        with inline.open('x') as stream:
                            stream.write(carrier['body'])
                    host_generated_source = inline
                    actual_arguments.update(script_path=str(inline), script_args=[], interpreter=interpreter)
            else:
                raise ValueError('EXECUTABLE_CARRIER_MISSING')
        env = freeze_execution_environment(os.environ, registered_execution_environment(durable, actual_arguments.get('env') or {},runtime_binding=arguments.get('_runtime_binding')))
        diagnostic = explicit_script_diagnostic({k:v for k,v in actual_arguments.items() if k != 'legacy_action'})
        if diagnostic:
            raise PrelaunchAdmissionRejected(diagnostic['error'], phase='carrier_admission',
                reason=diagnostic['reason'], instruction=diagnostic['instruction'])
        argv, command = actual_arguments.get('argv'), actual_arguments.get('cmd')
        source = actual_arguments.get('script_path')
        if selected_source and not source:
            raise PrelaunchAdmissionRejected('SELECTED_CANDIDATE_REQUIRES_SOURCE', phase='source_identity',
                instruction='Execute the selected draft using script_path + script_args; omitting source does not remove its version obligation.')
        tokens = list(argv) if argv is not None else None
        script_index = None
        if source:
            if argv is not None or command is not None:
                resolved = resolve_direct_script_argv(actual_arguments)  # legacy only
                if resolved.get('code'):
                    raise PrelaunchAdmissionRejected(resolved['code'], phase='carrier_admission', reason=resolved['reason'], instruction=resolved['instruction'])
                tokens, script_index = resolved['argv'], resolved['script_index']
                if (cwd / tokens[script_index]).resolve() != (cwd / source).resolve():
                    raise PrelaunchAdmissionRejected('EXPLICIT_SCRIPT_ARGV_MISMATCH', phase='source_identity')
            else:
                language = {'.py':'python','.sh':'bash','.r':'r'}.get(Path(source).suffix.lower())
                if language is None:
                    raise PrelaunchAdmissionRejected('CONTROLLED_SCRIPT_LANGUAGE_REQUIRED', phase='source_identity')
                default_runner = {'python':'python','bash':'bash','r':'Rscript'}[language]
                runner = actual_arguments.get('interpreter') or default_runner
                # Match a host-supported file runner, then resolve that exact
                # runner in the selected environment. Never fall back to sys.executable.
                runner_kind = ('python' if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(runner).name)
                    else 'bash' if Path(runner).name in {'bash','sh'} else 'r' if Path(runner).name == 'Rscript' else None)
                if runner_kind != language:
                    raise PrelaunchAdmissionRejected('CONTROLLED_SCRIPT_RUNNER_MISMATCH', phase='source_identity')
                tokens = [runner, *(['-u'] if language == 'python' else []), str(source), *actual_arguments.get('script_args', [])]
                script_index = 2 if language == 'python' else 1
            import shutil
            runner = tokens[0]
            selected_runner = str((cwd / runner).absolute()) if '/' in runner else shutil.which(runner, path=env['PATH'])
            if not selected_runner or not Path(selected_runner).is_file() or not os.access(selected_runner, os.X_OK):
                raise PrelaunchAdmissionRejected('EXECUTION_RUNNER_UNAVAILABLE', phase='source_identity')
            tokens[0] = selected_runner
            if selected_source and (cwd / source).resolve() != (cwd / selected_source).resolve():
                raise PrelaunchAdmissionRejected('SELECTED_CANDIDATE_SOURCE_MISMATCH', phase='source_identity')
        original_command = command if command is not None else shlex.join(tokens)
        command = original_command
        frozen_files = []
        syntax = {'required': False, 'check_status': 'NOT_APPLICABLE', 'reason': 'native argv or complex shell command'}
        if not source and argv is None:
            syntax = check_executable_action({'type': 'execute', 'argv': argv} if argv is not None else
                {'type': 'execute', 'language': 'bash', 'code': command})
            if syntax.get('required') and syntax.get('check_status') != 'PASS':
                return {'status': 'not_run', 'syntax_check': syntax, 'execution_started': False}
        if source:
            lexical_source = cwd / source
            try:
                source = (cwd / source).resolve(strict=True)
            except FileNotFoundError:
                raise PrelaunchAdmissionRejected('EXPLICIT_SCRIPT_MISSING', phase='source_identity')
            if script_index is None:
                raise PrelaunchAdmissionRejected('EXPLICIT_SCRIPT_REQUIRES_DIRECT_INTERPRETER_ARGV', phase='carrier_admission')
            from agent_core.workspace_files import WorkspaceFileService
            from agent_core.execution_permissions import open_authorized_source
            source_files = WorkspaceFileService(service, draft_root=(
                attempt_dir if host_generated_source == lexical_source else
                Path(selected_source).parent if selected_source and source == Path(selected_source)
                    and source.is_relative_to(Path(service.data_path) / 'task_outputs' / task_id)
                else None))
            with open_authorized_source(source_files, lexical_source, task_id, call_id) as stream:
                content = stream.read()
            sha = hashlib.sha256(content).hexdigest()
            approved_source_hash = permission['payload'].get('source_hashes', {}).get(str(lexical_source))
            if approved_source_hash and sha != approved_source_hash:
                raise PrelaunchAdmissionRejected('APPROVED_SOURCE_CHANGED', phase='source_version')
            expected = actual_arguments.get('expected_sha256') or (draft.get('sha256') if str(source) == draft.get('path') else '')
            if (expected and expected != sha) or (selected_sha and selected_sha != sha):
                raise PrelaunchAdmissionRejected('SCRIPT_VERSION_CHANGED', phase='source_version', instruction='Read and explicitly select the current file; retain expected_sha256.')
            language = {'.sh': 'bash', '.py': 'python', '.r': 'r'}.get(source.suffix.lower())
            if language:
                syntax = check_executable_action({'type': 'execute', 'language': language, 'code': content.decode()})
                if syntax.get('required') and syntax.get('check_status') != 'PASS':
                    return {'status': 'not_run', 'syntax_check': syntax, 'execution_started': False, 'path': str(source)}
            layout = attempt_dir / 'source'
            if layout.is_symlink():
                raise ValueError('FROZEN_SOURCE_LAYOUT_CHANGED')
            observation['preparation_settlement']['effects'].append({'kind':'frozen_source_layout', 'path':str(layout), 'retained':True})
            layout.mkdir(exist_ok=True)
            selected_files = [source]
            for helper in actual_arguments.get('helper_paths') or []:
                path = source.parent / helper
                if '..' in Path(helper).parts or not path.is_relative_to(source.parent):
                    raise PrelaunchAdmissionRejected('HELPER_OUTSIDE_EXPLICIT_SCRIPT_LAYOUT', phase='dynamic_admission')
                selected_files.append(path)
            for path in dict.fromkeys(selected_files):
                if path == source:
                    body = content
                else:
                    with open_authorized_source(source_files, path, task_id, call_id) as stream:
                        body = stream.read()
                    approved_helper_hash = permission['payload'].get('source_hashes', {}).get(str(path))
                    if approved_helper_hash and hashlib.sha256(body).hexdigest() != approved_helper_hash:
                        raise PrelaunchAdmissionRejected('APPROVED_HELPER_CHANGED', phase='source_version')
                target = layout / path.relative_to(source.parent)
                _output_path_state(target)  # Refuse a symlink in the frozen layout.
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # A previous admission/backup failure may leave frozen
                    # sources without any dispatch. Reuse exact immutable bytes
                    # only; never replace that intent's frozen source on retry.
                    if stat.S_IMODE(target.stat().st_mode) != 0o500 or target.read_bytes() != body:
                        raise ValueError('FROZEN_SOURCE_VERSION_CHANGED')
                else:
                    with target.open('xb') as stream:
                        stream.write(body)
                    target.chmod(0o500)
                frozen_files.append({'original_path': str(path), 'path': str(target), 'sha256': hashlib.sha256(body).hexdigest()})
            tokens = list(tokens)
            tokens[script_index] = str(layout / source.name)
            command = shlex.join(tokens)
        # Snapshot known files without scanning an entire project or inferring
        # dependencies from arbitrary shell text.
        spec = canonical_task_spec_for_state(durable) or {}
        paths = []
        step = {}
        from agent_core.work_outputs import file_fingerprint
        output_rows = action_targets(durable, arguments, project,command_service=service,task_id=task_id)
        paths.extend(row['canonical_path'] for row in output_rows)
        def observe():
            result = {}
            for raw in paths:
                if not raw:
                    continue
                path = Path(raw)
                try:
                    info = path.stat()
                    result[str(path)] = {'exists': True, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
                        'kind': 'directory' if path.is_dir() else 'file'}
                    if path.is_file():
                        # Keep the original bounded strength. Later acceptance
                        # cannot upgrade a historical prefix to a full hash.
                        result[str(path)].update(file_fingerprint(path))
                except OSError as exc:
                    result[str(path)] = {'exists': False, 'error': type(exc).__name__}
            return result
        check_cancelled()
        # Claim before any mutation of a user target, not just before spawn.
        if permission_digest(command_payload(service, arguments, task_id, call_id=call_id)) != permission['action_hash']:
            raise ActionRuntimeContractError('PERMISSION_ACTION_CHANGED_BEFORE_CLAIM')
        permit = claim_permission(service.data_path, task_id, permission, attempt_id)
        durable = load_plan_state(service.data_path, task_id)
        if (durable.get('task_state') or {}).get('report_preservation_authorization'):
            from agent_core.workflow.report_preservation import execute_report_preservation
            def preservation_scope(current):
                return (request_scope(current) == scope and not current_finish(current)
                        and (current.get('task_state') or {}).get('status') != 'cancelled'
                        and not (cancel_event is not None and cancel_event.is_set()))
            observation['preparation_settlement'].update(safe_to_release=False, phase='report_preservation')
            preserved = execute_report_preservation(service.data_path, task_id, scope_guard=preservation_scope)
            observation['preparation_settlement']['effects'].append({'kind':'report_preservation', 'receipts':deepcopy(preserved), 'retained':True})
            if all(row.get('status') == 'completed' for row in preserved):
                observation['preparation_settlement'].update(safe_to_release=True, phase='scoped_preparation')
            durable = load_plan_state(service.data_path, task_id)
            check_cancelled()
        from agent_core.work_output_protection import prepare_output_protection, output_file_identities
        def output_scope_guard():
            validate_permit(permit)
            check_cancelled()
            latest = load_plan_state(service.data_path, task_id)
            if (request_scope(latest) != scope or current_finish(latest)
                    or authority_identity(latest) != authority_identity(durable)):
                raise ActionRuntimeContractError('OUTPUT_WRITE_SCOPE_CHANGED')
        output_guard, output_protection = prepare_output_protection(durable, output_rows,
            data_path=service.data_path, attempt_dir=attempt_dir, scope_guard=output_scope_guard,
            preparation_observation=observation['preparation_settlement'])
        observation['preparation_settlement']['effects'].append({'kind':'output_protection', 'receipt':deepcopy(output_protection), 'retained':True})
        before = observe()
        from agent_core.revision_evidence import observed_inputs, method_context
        input_before = {}
        from agent_core.input_manifest import manifest_snapshot
        manifest_before = None
        action = {'type': 'execute', 'execution_protocol_version': 3, 'action_id': action_id,
            'arguments_hash': args_hash, 'arguments': deepcopy(dict(arguments)), 'request_scope': scope, 'tool_call_id': call_id,
            'step_id': '', 'plan_version': None,
            'cmd': command, 'original_cmd': original_command, 'argv': tokens, 'cwd': str(cwd), 'env': env,
            'execution_carrier': 'argv' if tokens is not None else 'shell',
            'shell': None if tokens is not None else '/bin/sh', 'shell_login': False,
            'frozen_files': frozen_files, 'syntax_check': syntax,
            'dependency_scope': 'explicit script and helper paths only; complex shell dependencies not inferred'}
        if arguments.get('_runtime_binding'):
            action['runtime_binding']=deepcopy(arguments['_runtime_binding'])
        # Complete write admission before persisting STARTING or charging a
        # dispatch/repair. No await separates this check from the actual runner.
        # A rejected target must not leave a phantom active process to reconcile.
        output_scope_guard()
        validate_output_write_guard(output_guard)
        action['action_hash'] = hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()
        observation['preparation_settlement'].update(safe_to_release=False, phase='starting_publication')
        record_provenance_event(durable, 'action_bound', {'action': action})
        record_provenance_event(durable, 'attempt_created', {'action_id': action_id, 'attempt_id': attempt_id,
            'step_id': action['step_id'], 'stdout_path': str(attempt_dir/'stdout.log'),
            'stderr_path': str(attempt_dir/'stderr.log'), 'exit_status_path': str(receipt_path)})
        record_provenance_event(durable, 'attempt_state_changed', {'attempt_id': attempt_id, 'status': 'STARTING'})
        task = durable.setdefault('task_state', {})
        budgets = task.setdefault('budgets', {})
        budgets['primary_execution_dispatches'] = int(budgets.get('primary_execution_dispatches') or 0)+1
        save_plan_state(service.data_path, task_id, durable)
        async def lifecycle(status, details):
            observation.update(details)
            if status == 'HEARTBEAT':
                status = 'RUNNING'
            if status == 'PROCESS_CANCELLED':
                status = 'PROCESS_EXITED' if details.get('process_reaped') else 'UNKNOWN_AFTER_CRASH'
            state = load_plan_state(service.data_path, task_id)
            record_provenance_event(state, 'attempt_state_changed', {'attempt_id': attempt_id, 'status': status, **details})
            save_plan_state(service.data_path, task_id, state)
        try:
            check_cancelled()
            from agent_core.command_execution import FormalProtectionError
            from agent_core.cli_tool_activity import runtime_event
            runtime_event('preparing', {'cmd': command, 'argv': tokens, 'cwd': str(cwd),
                'environment_names': sorted(env), 'action_id': action_id,
                'execution_attempt_id': attempt_id})
            observation['launch_delegated'] = True
            with execution_context(permit, tokens if tokens is not None else command, cwd=cwd, env=env):
                stdout, stderr, rc = await run_command(tokens if tokens is not None else command, cwd=cwd, env=env,
                    runtime_observation=observation, cancel_event=cancel_event, original_deadline=original_deadline,
                    timeout=float(arguments['timeout_ms'])/1000 if arguments.get('timeout_ms') else None,
                    max_output_bytes=65536 if arguments.get('_biomind_probe') is True else None,
                    short_check=arguments.get('_biomind_probe') is True,
                    lifecycle_callback=lifecycle, profile=False,
                    capture_log_paths={'stdout': str(attempt_dir/'stdout.log'), 'stderr': str(attempt_dir/'stderr.log')},
                    display_context={'task_id': task_id, 'node': stage, 'execution_attempt_id': attempt_id})
        except FormalProtectionError as exc:
            # The mount adapter raises only before create_subprocess. Retain
            # prior preparation effects but do not leave a phantom STARTING
            # process or manufacture a command exit code.
            state = load_plan_state(service.data_path, task_id)
            record_provenance_event(state, 'attempt_state_changed', {'attempt_id': attempt_id,
                'status': 'FAILED', 'execution_started': False, 'error': str(exc),
                'capability_missing': True})
            save_plan_state(service.data_path, task_id, state)
            observation.update(launch_delegated=False, process_alive=False, cleanup_confirmed=True)
            observation['preparation_settlement'].update(safe_to_release=True,
                phase='protected_spawn_preflight')
            raise
        except asyncio.CancelledError:
            state = load_plan_state(service.data_path, task_id)
            attempt = state['provenance_state']['attempts'][attempt_id]
            cancellation = dict(observation) or get_last_execution_metadata()
            attempt['status'] = 'PROCESS_EXITED' if cancellation.get('process_reaped') else 'UNKNOWN_AFTER_CRASH'
            attempt['returncode'] = cancellation.get('process_returncode')
            attempt['cancellation_requested'] = True
            attempt['cancellation_receipt'] = cancellation
            save_plan_state(service.data_path, task_id, state)
            # Cancellation is a request fact; preserve an observed natural rc=0.
            # A missing exit or incomplete drain is not a consumable terminal.
            complete = bool(cancellation.get('cleanup_confirmed')) or observation.get('launch_delegated') is False
            cancelled_result = {'status': 'cancelled' if complete else 'reconciliation_unknown',
                'terminal': complete, 'execution_protocol_version': 3,
                'execution_started': True if cancellation.get('process_started_at') else (None if observation.get('launch_delegated') else False),
                'attempt_id': attempt_id, 'action_id': action_id, 'task_id': task_id,
                'step_id': action['step_id'], 'request_scope': scope, 'arguments_hash': args_hash,
                'exit_code': cancellation.get('process_returncode'),
                'returncode': cancellation.get('process_returncode'),
                'receipt_ref': str(receipt_path), 'runtime_metadata': cancellation,
                'stdout_path': str(attempt_dir/'stdout.log'), 'stderr_path': str(attempt_dir/'stderr.log'),
                'scientific_validation': 'not_assessed', 'cleanup_confirmed': complete}
            if arguments.get('_runtime_binding'):
                cancelled_result['runtime_binding']=deepcopy(arguments['_runtime_binding'])
            from agent_core.command_inspection_owner import _once
            if complete and not receipt_path.exists():
                _once(receipt_path, cancelled_result)
            observation['cancelled_result'] = cancelled_result
            raise
        # A changed/unreadable manifest after execution is evidence of a gap,
        # not grounds to discard the real process exit receipt.
        manifest_after, manifest_error, input_after = None, None, {}
        try:
            manifest_after = None
            input_after = {}
        except (ValueError, OSError) as exc:
            manifest_error = str(exc)
        output_after, output_snapshot_error = {}, None
        try: output_after = observe()
        except (ValueError, OSError) as exc: output_snapshot_error = str(exc)
        result = {'status': 'exited', 'terminal': True, 'execution_protocol_version': 3, 'execution_started': True,
            'attempt_id': attempt_id, 'action_id': action_id, 'step_id': action['step_id'],
            'task_id': task_id, 'plan_id': durable.get('plan_id'), 'plan_version': durable.get('plan_version'), 'execution_id': 'exec-' + attempt_id, 'arguments_hash': args_hash, 'request_scope': scope,
            'cmd': command, 'original_cmd': original_command, 'argv': tokens, 'cwd': str(cwd), 'env': env,
            'execution_carrier': action['execution_carrier'], 'shell': action['shell'], 'shell_login': action['shell_login'],
            'hostname': socket.gethostname(), 'exit_code': rc, 'returncode': rc,
            'stdout': stdout, 'stderr': stderr, 'stdout_path': str(attempt_dir/'stdout.log'),
            'stderr_path': str(attempt_dir/'stderr.log'), 'receipt_ref': str(receipt_path),
            'frozen_files': frozen_files, 'syntax_check': syntax, 'dependency_scope': action['dependency_scope'],
            'before_files': before, 'after_files': output_after, 'output_snapshot_error': output_snapshot_error, 'scientific_validation': 'not_assessed',
            'output_write_guard': output_guard, 'output_write_protection': output_protection,
            'output_file_identities': output_file_identities(output_guard),
            'execution_repair': deepcopy(action.get('execution_repair') or {}),
            'authority_identity': authority_identity(durable),
            'before_input_files': input_before, 'after_input_files': input_after,
            'input_manifest_before': manifest_before, 'input_manifest_after': manifest_after,
            'input_manifest_error': manifest_error,
            'review_method_context': {},
            # Preserve only the method/validation context needed for later
            # acceptance. Host status, evidence and recursive execution_history
            # are not part of the method and must not be copied into receipts.
            'review_step_context': {key: deepcopy(step[key]) for key in (
                'description','title','tool','purpose','action','skill_ids',
                'workflow_stage_ids','execution_capability_id','operation','validation') if key in step},
            'finished_at': time.time(), 'runtime_metadata': dict(observation) or get_last_execution_metadata()}
        result['cleanup_confirmed'] = bool(result['runtime_metadata'].get('cleanup_confirmed', True))
        if arguments.get('_runtime_binding'):
            result['runtime_binding']=deepcopy(arguments['_runtime_binding'])
        from agent_core.command_inspection_owner import _once
        _once(receipt_path, result)
        _record_formal_receipt(service.data_path, task_id, result)
        return result
    except Exception as exc:
        from agent_core.execution_permissions import PermissionPending, PermissionDenied
        if isinstance(exc, (PermissionPending, PermissionDenied)):
            raise
        settlement = observation.get('preparation_settlement') or {}
        if (runtime_observation is not None and settlement.get('safe_to_release') is True
                and observation.get('launch_delegated') is False):
            # Classify by this owned preparation scope, never by exception names.
            import traceback
            issue = deepcopy(getattr(exc, 'issue', None) or getattr(exc, 'diagnostic', None) or {})
            code = getattr(exc, 'code', None) or issue.pop('code', None) or type(exc).__name__
            protection_phase = issue.pop('phase', None)
            if isinstance(protection_phase, str):
                issue['protection_phase'] = protection_phase
            raise PrelaunchAdmissionRejected(code, phase=settlement['phase'],
                original_error=type(exc).__name__ + ': ' + str(exc),
                original_traceback=traceback.format_exc(), **issue) from exc
        raise
    finally:
        lock.close()


def _record_formal_receipt(data_path, task_id, result):
    """Reconcile a durable process exit exactly once, including crash replay."""
    from agent_core.plan_tracker import load_plan_state, save_plan_state, record_provenance_event, StateCASConflict
    from agent_core.request_state import request_scope
    for _ in range(3):
        state = load_plan_state(data_path, task_id)
        if ((state.get('task_state') or {}).get('status') == 'cancelled'
                or result.get('status') == 'cancelled' or result.get('terminal') is False):
            return
        attempt_id = result['attempt_id']
        attempt = state['provenance_state']['attempts'][attempt_id]
        if result.get('request_scope') and result['request_scope'] != request_scope(state):
            # Retain the old attempt's real exit, without publishing evidence,
            # failures, budget charges or step transitions into a new request.
            if attempt.get('command_receipt') == result['receipt_ref']:
                return
            if attempt.get('status') in {'STARTING', 'RUNNING', 'UNKNOWN_AFTER_CRASH'}:
                record_provenance_event(state, 'attempt_state_changed', {
                    'attempt_id': attempt_id, 'status': 'PROCESS_EXITED', 'returncode': result['exit_code'],
                    'exit_status_path': result['receipt_ref']})
                attempt = state['provenance_state']['attempts'][attempt_id]
            attempt['command_receipt'] = result['receipt_ref']
            attempt['command_receipt_sha256'] = hashlib.sha256(Path(result['receipt_ref']).read_bytes()).hexdigest()
            try:
                save_plan_state(data_path, task_id, state)
                return
            except StateCASConflict:
                continue
        if attempt.get('command_receipt') == result['receipt_ref']:
            return
        record_provenance_event(state, 'execution_finished', {'attempt_id': attempt_id, 'execution_record': result})
        attempt = state['provenance_state']['attempts'][attempt_id]
        attempt['command_receipt'] = result['receipt_ref']
        attempt['command_receipt_sha256'] = __import__('hashlib').sha256(Path(result['receipt_ref']).read_bytes()).hexdigest()
        state['execution_evidence'] = deepcopy(result)
        from agent_core.registered_runtime import record_execution_invalidation
        record_execution_invalidation(state, result)
        if result['exit_code'] != 0:
            budgets = state.setdefault('task_state', {}).setdefault('budgets', {})
            budgets['primary_execution_failures'] = int(budgets.get('primary_execution_failures') or 0) + 1
            attempt['failure_source'] = {'attempt_id': attempt_id, 'returncode': result['exit_code'], 'receipt_ref': result['receipt_ref']}
        try:
            save_plan_state(data_path, task_id, state)
            return
        except StateCASConflict:
            continue
    raise ValueError('EXECUTION_RECEIPT_STATE_CONFLICT')

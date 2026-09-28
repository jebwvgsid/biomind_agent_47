"""Formal mutable-workspace bindings and runtime-attested mutation evidence.

This module intentionally has no scientific-name heuristics.  It moves an
already approved Plan resource identity/path into Generate, validates the
selected capability's explicit evidence contract before launch, and records a
bounded filesystem observation after execution.  Executed source and model
output are claims, never receipt authorities.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


WORKSPACE_BINDING_SCHEMA_VERSION = 1
MUTATION_INTENT_SCHEMA_VERSION = 1
MUTATION_RECEIPT_SCHEMA_VERSION = 1
MUTATION_INTENT_REGISTRATION_SCHEMA_VERSION = 1
RUNTIME_ATTESTER_PRODUCER = "biomind.runtime_attester.v1"
WORKSPACE_MUTATION_INTENT_RECORDED_EVENT = (
    "workspace_mutation_intent_recorded"
)
RUNTIME_ACCESS_EVIDENCE_SCHEMA_VERSION = 1
RUNTIME_ACCESS_EVIDENCE_PRODUCER = (
    "biomind.controller.runtime_access_observer.v1"
)
BOUNDED_SNAPSHOT_COLLECTOR = "bounded_filesystem_snapshot_v1"

_BINDING_TOKEN_RE = re.compile(r"\{binding:([A-Za-z][A-Za-z0-9_.-]{0,126})\}")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _positive_int(value: Any, *, code: str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkspaceAttestationError(
            code,
            field=field,
            observed_value=value,
        ) from exc
    if parsed < 1:
        raise WorkspaceAttestationError(
            code,
            field=field,
            observed_value=value,
        )
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class WorkspaceAttestationError(ValueError):
    """Typed, route-free workspace contract failure."""

    def __init__(self, code: str, **diagnostic: Any) -> None:
        self.code = str(code or "WORKSPACE_ATTESTATION_INVALID")
        self.diagnostic = deepcopy(diagnostic)
        super().__init__(f"{self.code}: {_canonical(self.diagnostic)}")

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, **deepcopy(self.diagnostic)}


@dataclass(frozen=True)
class VerifiedMutationJournalReference:
    """A scope-bound, integrity-checked reference to one mutation journal.

    ``journal_digest`` authenticates the currently loaded file version.
    ``registration_digest`` authenticates the immutable registration
    projection and therefore remains stable when the same journal is later
    finalized or reconciled in place.
    """

    schema_version: int
    event_type: str
    producer: str
    task_id: str
    plan_id: str
    plan_revision: int
    step_id: str
    action_id: str
    action_digest: str
    attempt_id: str
    journal_path: str
    journal_digest: str
    registration_digest: str
    journal_status: str
    journal_finished_at: str
    intent_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["intent_ids"] = list(self.intent_ids)
        return result


@dataclass(frozen=True)
class WorkspaceAdmission:
    status: str
    effective_workspace_bindings: Tuple[Dict[str, Any], ...] = ()
    mutation_intents: Tuple[Dict[str, Any], ...] = ()
    violations: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "effective_workspace_bindings": [
                deepcopy(item) for item in self.effective_workspace_bindings
            ],
            "mutation_intents": [deepcopy(item) for item in self.mutation_intents],
            "violations": [deepcopy(item) for item in self.violations],
        }


def _workspace_identity(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(
            value.get("identity")
            or value.get("workspace_ref")
            or value.get("canonical_resource_id")
            or value.get("declared_resource_ref")
            or ""
        ).strip()
    return str(value or "").strip()


def _mutation_rows(value: Any) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    for raw in value if isinstance(value, (list, tuple)) else []:
        if isinstance(raw, Mapping):
            row = deepcopy(dict(raw))
        elif str(raw or "").strip():
            row = {"workspace_ref": str(raw).strip(), "mutation": "readwrite"}
        else:
            continue
        identity = _workspace_identity(row)
        if identity:
            row.setdefault("workspace_ref", identity)
            result.append(row)
    return result


def _binding_for_identity(
    resource_bindings: Mapping[str, Any], identity: str
) -> Dict[str, Any]:
    direct = resource_bindings.get(identity)
    if isinstance(direct, Mapping):
        return deepcopy(dict(direct))
    matches: Dict[str, Dict[str, Any]] = {}
    for raw in resource_bindings.values():
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        aliases = {str(item) for item in row.get("aliases", []) or [] if str(item)}
        canonical = str(row.get("canonical_resource_id") or "")
        if identity == canonical or identity in aliases:
            key = str(row.get("binding_id") or _digest(row))
            matches[key] = row
    if len(matches) > 1:
        raise WorkspaceAttestationError(
            "WORKSPACE_RESOURCE_BINDING_AMBIGUOUS",
            workspace_ref=identity,
            binding_ids=sorted(matches),
        )
    return deepcopy(next(iter(matches.values()))) if matches else {}


def _step_workspace_mutations(step: Mapping[str, Any]) -> list[Dict[str, Any]]:
    effect = dict(step.get("step_effect") or {})
    # An explicit empty canonical declaration must not revive stale wire fields.
    return _mutation_rows(
        effect["workspace_mutations"] if "workspace_mutations" in effect
        else step.get("workspace_mutations") or []
    )


def resolve_action_workspace_mutations(
    *,
    capability_contract: Mapping[str, Any],
    approved_step: Optional[Mapping[str, Any]] = None,
    resource_bindings: Optional[Mapping[str, Any]] = None,
) -> list[Dict[str, Any]]:
    """Resolve capability and approved Plan effects without changing the capability.

    An omitted capability field allows an explicitly declared Plan contract;
    an explicit field (including []) remains closed. Generated action fields
    never participate in this authority projection.
    """
    capability_rows = _mutation_rows(capability_contract.get("workspace_mutations") or [])
    if approved_step is None:
        return capability_rows
    bindings = resource_bindings or {}

    def identity(row: Mapping[str, Any]) -> str:
        ref = _workspace_identity(row)
        return str(_binding_for_identity(bindings, ref).get("canonical_resource_id") or ref)

    step_rows = _step_workspace_mutations(approved_step)
    by_identity = {identity(row): row for row in step_rows}
    if len(by_identity) != len(step_rows):
        raise WorkspaceAttestationError("WORKSPACE_MUTATION_SCOPE_AMBIGUOUS")
    resolved: list[Dict[str, Any]] = []
    capability_ids = set()
    for mutation in capability_rows:
        canonical_id = identity(mutation)
        capability_ids.add(canonical_id)
        approved = by_identity.get(canonical_id)
        if approved is None:
            raise WorkspaceAttestationError(
                "WORKSPACE_EFFECT_NOT_APPROVED_FOR_STEP",
                workspace_ref=_workspace_identity(mutation),
                canonical_resource_id=canonical_id,
                step_id=str(approved_step.get("step_id") or ""),
            )
        if str(approved.get("mutation") or "readwrite") != str(mutation.get("mutation") or "readwrite"):
            raise WorkspaceAttestationError(
                "WORKSPACE_MUTATION_SCOPE_MISMATCH", canonical_resource_id=canonical_id,
                capability_mutation=str(mutation.get("mutation") or "readwrite"),
                approved_step_mutation=str(approved.get("mutation") or "readwrite"),
            )
        if (approved.get("evidence_contract") is not None
                and approved.get("evidence_contract") != mutation.get("evidence_contract")):
            raise WorkspaceAttestationError(
                "WORKSPACE_EVIDENCE_CONTRACT_SCOPE_MISMATCH",
                canonical_resource_id=canonical_id,
            )
        resolved.append(mutation)
    for mutation in step_rows:
        canonical_id = identity(mutation)
        if canonical_id in capability_ids:
            continue
        if "workspace_mutations" in capability_contract:
            raise WorkspaceAttestationError(
                "WORKSPACE_MUTATION_CAPABILITY_MISMATCH",
                canonical_resource_id=canonical_id,
                capability_id=str(capability_contract.get("capability_id") or ""),
            )
        resolved.append(mutation)
    return resolved


def workspace_evidence_contract_supported(value: Any) -> bool:
    """Whether the runtime collector can evaluate this declared contract."""
    if not isinstance(value, Mapping):
        return False
    try:
        version = int(value.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    conditions = value.get("postconditions")
    if (version != 1 or value.get("collector") != BOUNDED_SNAPSHOT_COLLECTOR
            or not isinstance(conditions, list) or not conditions):
        return False
    for condition in conditions:
        if not isinstance(condition, Mapping) or condition.get("kind") not in {
            "directory_exists", "file_exists", "file_nonempty",
        }:
            return False
        relative = condition.get("relative_path", "")
        if not isinstance(relative, str) or "\x00" in relative:
            return False
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            return False
    return True


def _resolved_layout(layout: Mapping[str, Any], root: str) -> Dict[str, Any]:
    """Resolve only explicit layout references; leave dynamic binding tokens intact."""

    result = {str(key): deepcopy(value) for key, value in dict(layout or {}).items()}
    resolved: Dict[str, Any] = {}
    for _ in range(max(1, len(result) + 1)):
        changed = False
        for key, raw in result.items():
            if not isinstance(raw, str):
                resolved[key] = deepcopy(raw)
                continue
            value = raw.replace("<snpeff_db artifact>", root)
            for prior_key, prior_value in resolved.items():
                if isinstance(prior_value, str):
                    value = value.replace(f"<{prior_key}>", prior_value)
            if resolved.get(key) != value:
                resolved[key] = value
                changed = True
        if not changed:
            break
    return resolved


def _path_state(path_text: str) -> Dict[str, Any]:
    path = Path(path_text)
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "kind": "missing", "symlink": False}
    except OSError as exc:
        return {
            "exists": None,
            "kind": "unknown",
            "symlink": None,
            "error": type(exc).__name__,
        }
    is_link = path.is_symlink()
    return {
        "exists": True,
        "kind": (
            "symlink" if is_link else "directory" if path.is_dir() else "file"
        ),
        "symlink": is_link,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "inode": int(stat.st_ino),
    }


def project_current_action_workspace_bindings(
    *,
    plan_state: Mapping[str, Any],
    approved_plan_snapshot: Mapping[str, Any],
    current_step: Mapping[str, Any],
    capability_contract: Mapping[str, Any],
    binding_rule: Optional[Mapping[str, Any]] = None,
    derived_artifact_layout: Optional[Mapping[str, Any]] = None,
) -> list[Dict[str, Any]]:
    """Project exact mutable resources required by the approved current action.

    Mutation declarations authorize read/write effects.  Artifact-backed
    binding requirements can additionally project a read-only mutable resource
    (for example an already built database used by an annotation action).
    Mutation declarations come from the selected capability and approved Plan;
    neither is inferred from source text or a path basename.
    """

    from agent_core.plan_bindings import resource_bindings_for_step
    if approved_plan_snapshot.get("resource_role_protocol_version") not in (None, 1):
        raise WorkspaceAttestationError("WORKSPACE_ROLE_PROTOCOL_UNSUPPORTED")
    if approved_plan_snapshot.get("steps") or approved_plan_snapshot.get("resource_role_protocol_version") == 1:
        approved_step = next((row for row in approved_plan_snapshot.get("steps", [])
            if row.get("step_id") == current_step.get("step_id")), {})
        if not approved_step or approved_step.get("execution_capability_id") != capability_contract.get("capability_id"):
            raise WorkspaceAttestationError("WORKSPACE_ROLE_APPROVAL_SCOPE_MISMATCH")
        current_step = approved_step
    if approved_plan_snapshot.get("resource_role_protocol_version") == 1:
        resource_bindings = resource_bindings_for_step(approved_plan_snapshot.get("plan_resource_bindings") or {}, current_step)
    else:
        resource_bindings = dict(approved_plan_snapshot.get("plan_resource_bindings")
                                 or plan_state.get("plan_resource_bindings") or {})
    capability_mutations = resolve_action_workspace_mutations(
        capability_contract=capability_contract,
        approved_step=current_step,
        resource_bindings=resource_bindings,
    )
    projection_requirements = list(capability_mutations)
    projected_identities = {
        str((resource_bindings.get(_workspace_identity(item)) or {}).get("canonical_resource_id") or _workspace_identity(item)) for item in capability_mutations
    }
    raw_artifact_requirements = dict(
        (binding_rule or {}).get("artifact_binding_requirements") or {}
    )
    explicit_capability_artifacts = {
        str((resource_bindings.get(str(item)) or {}).get("canonical_resource_id") or item)
        for item in [
            *(capability_contract.get("required_artifact_binding_ids") or []),
            *(capability_contract.get("consumes_artifacts") or []),
        ]
        if str(item)
    }
    for binding_name, raw_requirement in raw_artifact_requirements.items():
        if not isinstance(raw_requirement, Mapping):
            continue
        canonical_id = str(
            raw_requirement.get("canonical_resource_id") or ""
        ).strip()
        if (
            str(raw_requirement.get("resource_kind") or "")
            != "mutable_workspace"
            or not canonical_id
            or canonical_id in projected_identities
            or canonical_id not in explicit_capability_artifacts
        ):
            continue
        projection_requirements.append({
            "workspace_ref": canonical_id,
            "mutation": "read",
            "contract_source": (
                "generate.binding_contract.artifact_binding_requirements."
                + str(binding_name)
            ),
            "contract_revision": str(
                (binding_rule or {}).get("contract_revision") or "1"
            ),
        })
        projected_identities.add(canonical_id)
    if not projection_requirements:
        return []
    step_mutations = _step_workspace_mutations(current_step)
    ledger = {
        str(key): dict(value)
        for key, value in dict(
            approved_plan_snapshot.get("plan_local_artifacts")
            or plan_state.get("plan_artifacts")
            or {}
        ).items()
        if isinstance(value, Mapping)
    }
    results: list[Dict[str, Any]] = []
    for mutation in projection_requirements:
        raw_ref = _workspace_identity(mutation)
        resource_binding = _binding_for_identity(resource_bindings, raw_ref)
        canonical_id = str(
            resource_binding.get("canonical_resource_id") or raw_ref
        ).strip()
        expected_mutation = str(mutation.get("mutation") or "readwrite")
        if expected_mutation == "read":
            approved_mutation: Dict[str, Any] = {
                "declared_resource_ref": canonical_id,
                "mutation": "read",
            }
        else:
            matching_step_rows = [
                row
                for row in step_mutations
                if _workspace_identity(row) in {raw_ref, canonical_id}
                or str(row.get("declared_resource_ref") or "") == raw_ref
            ]
            if not matching_step_rows:
                raise WorkspaceAttestationError(
                    "WORKSPACE_EFFECT_NOT_APPROVED_FOR_STEP",
                    workspace_ref=raw_ref,
                    canonical_resource_id=canonical_id,
                    step_id=str(current_step.get("step_id") or ""),
                )
            approved_mutation = matching_step_rows[0]
            if (
                str(approved_mutation.get("mutation") or "readwrite")
                != expected_mutation
            ):
                raise WorkspaceAttestationError(
                    "WORKSPACE_MUTATION_SCOPE_MISMATCH",
                    canonical_resource_id=canonical_id,
                    capability_mutation=expected_mutation,
                    approved_step_mutation=str(
                        approved_mutation.get("mutation") or ""
                    ),
                )
        candidates = {
            logical_id: row
            for logical_id, row in ledger.items()
            if logical_id in {
                raw_ref,
                canonical_id,
                str(approved_mutation.get("declared_resource_ref") or ""),
            }
            or str(row.get("canonical_resource_id") or "") == canonical_id
            or raw_ref
            in {
                str(item)
                for item in dict(row.get("resource_binding") or {}).get("aliases", [])
            }
        }
        if len(candidates) != 1:
            raise WorkspaceAttestationError(
                "FORMAL_WORKSPACE_BINDING_UNAVAILABLE"
                if not candidates
                else "FORMAL_WORKSPACE_BINDING_AMBIGUOUS",
                workspace_ref=raw_ref,
                canonical_resource_id=canonical_id,
                matching_plan_handles=sorted(candidates),
            )
        plan_handle, ledger_row = next(iter(candidates.items()))
        path = str(ledger_row.get("canonical_path") or "").strip()
        resource_kind = str(
            ledger_row.get("resource_kind")
            or resource_binding.get("resource_kind")
            or ""
        )
        if resource_kind != "mutable_workspace" or not path or not Path(path).is_absolute():
            raise WorkspaceAttestationError(
                "FORMAL_WORKSPACE_BINDING_INVALID",
                workspace_ref=raw_ref,
                canonical_resource_id=canonical_id,
                plan_handle=plan_handle,
                resource_kind=resource_kind,
                formal_concrete_path=path,
            )
        binding_revision = _positive_int(
            ledger_row.get("artifact_binding_revision")
            or resource_binding.get("binding_revision"),
            code="FORMAL_WORKSPACE_BINDING_REVISION_MISSING",
            field="binding_revision",
        )
        raw_evidence_contract = mutation.get("evidence_contract")
        evidence_contract = (
            deepcopy(dict(raw_evidence_contract))
            if isinstance(raw_evidence_contract, Mapping) else {}
        )
        row = {
            "schema_version": WORKSPACE_BINDING_SCHEMA_VERSION,
            "workspace_ref": raw_ref,
            "canonical_resource_id": canonical_id,
            "plan_handle": plan_handle,
            "binding_id": str(resource_binding.get("binding_id") or ""),
            "binding_revision": binding_revision,
            "resource_kind": resource_kind,
            "formal_concrete_path": str(Path(path)),
            "access_scope": (
                "read_only" if expected_mutation == "read" else "read_write"
            ),
            "operation_scope": {
                "operation": str(capability_contract.get("operation") or ""),
                "mutation": expected_mutation,
            },
            "requires_available": mutation.get("requires_available"),
            "requires_state": str(mutation.get("requires_state") or ""),
            "available_after_success": mutation.get("available_after_success"),
            "establishes_state": str(mutation.get("establishes_state") or ""),
            "contract_source": str(mutation.get("contract_source") or ""),
            "contract_revision": str(
                mutation.get("contract_revision")
                or resource_binding.get("contract_revision")
                or ""
            ),
            "evidence_contract": evidence_contract,
            "binding_value_roles": deepcopy(dict((binding_rule or {}).get("binding_value_roles") or {})),
            "current_state": _path_state(path),
            "resolved_derived_layout": _resolved_layout(
                derived_artifact_layout or {}, path
            ),
        }
        row["binding_digest"] = _digest(
            {key: value for key, value in row.items() if key != "current_state"}
        )
        results.append(row)
    return sorted(results, key=lambda row: row["canonical_resource_id"])


def provider_workspace_view(rows: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    allowed = (
        "workspace_ref",
        "canonical_resource_id",
        "plan_handle",
        "binding_revision",
        "resource_kind",
        "formal_concrete_path",
        "access_scope",
        "operation_scope",
        "requires_available",
        "requires_state",
        "available_after_success",
        "establishes_state",
        "contract_source",
        "contract_revision",
        "evidence_contract",
        "binding_value_roles",
        "current_state",
        "resolved_derived_layout",
    )
    return [
        {key: deepcopy(row.get(key)) for key in allowed if key in row}
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("canonical_resource_id") or "")
        and str(row.get("formal_concrete_path") or "")
    ]


def validate_workspace_action_admission(
    action: Mapping[str, Any],
    *,
    capability_contract: Mapping[str, Any],
    workspace_bindings: Sequence[Mapping[str, Any]],
    approved_step: Optional[Mapping[str, Any]] = None,
) -> WorkspaceAdmission:
    """Validate intent/evidence capability without asking for a future receipt."""

    resource_bindings = {
        str(row.get(field)): row
        for row in workspace_bindings if isinstance(row, Mapping)
        for field in ("workspace_ref", "canonical_resource_id", "plan_handle")
        if row.get(field)
    }
    try:
        mutations = resolve_action_workspace_mutations(
            capability_contract=capability_contract, approved_step=approved_step,
            resource_bindings=resource_bindings,
        )
    except WorkspaceAttestationError as exc:
        return WorkspaceAdmission("contract_error", violations=({
            **exc.to_dict(), "owner": "INTERNAL_PROTOCOL",
        },))
    if not mutations:
        if action.get("formal_workspace_bindings") not in (None, [], {}):
            return WorkspaceAdmission(
                "candidate_rejected",
                violations=({
                    "code": "ACTION_WORKSPACE_BINDING_UNAUTHORIZED",
                    "owner": "GENERATED_ACTION",
                    "field_path": "formal_workspace_bindings",
                },),
            )
        return WorkspaceAdmission("not_applicable")
    if action.get("formal_workspace_bindings") not in (None, [], {}):
        return WorkspaceAdmission(
            "candidate_rejected",
            violations=({
                "code": "FORMAL_WORKSPACE_BINDING_CONTROLLER_OWNED",
                "owner": "GENERATED_ACTION",
                "message": "Generate must consume, not author, formal workspace authority.",
                "field_path": "formal_workspace_bindings",
            },),
        )
    rows = [deepcopy(dict(item)) for item in workspace_bindings if isinstance(item, Mapping)]
    violations: list[Dict[str, Any]] = []
    intents: list[Dict[str, Any]] = []
    for mutation in mutations:
        identity = _workspace_identity(mutation)
        candidates = [
            row for row in rows
            if str(row.get("canonical_resource_id") or "") == identity
            or str(row.get("workspace_ref") or "") == identity
        ]
        if len(candidates) != 1:
            violations.append({
                "code": "FORMAL_WORKSPACE_BINDING_UNAVAILABLE",
                "owner": "INTERNAL_PROTOCOL",
                "workspace_ref": identity,
                "matching_binding_count": len(candidates),
            })
            continue
        binding = candidates[0]
        if str(binding.get("resource_kind") or "") != "mutable_workspace":
            violations.append({
                "code": "FORMAL_WORKSPACE_BINDING_KIND_INVALID",
                "owner": "INTERNAL_PROTOCOL",
                "canonical_resource_id": str(
                    binding.get("canonical_resource_id") or ""
                ),
                "resource_kind": str(binding.get("resource_kind") or ""),
            })
            continue
        raw_evidence_contract = binding.get("evidence_contract")
        evidence_contract = (
            dict(raw_evidence_contract)
            if isinstance(raw_evidence_contract, Mapping) else {}
        )
        if not workspace_evidence_contract_supported(evidence_contract):
            violations.append({
                "code": "WORKSPACE_EVIDENCE_CAPABILITY_MISSING",
                "owner": "INTERNAL_PROTOCOL",
                "canonical_resource_id": str(binding.get("canonical_resource_id") or ""),
                "contract_source": str(binding.get("contract_source") or ""),
            })
            continue
        if (approved_step is not None
                and evidence_contract != mutation.get("evidence_contract")):
            violations.append({
                "code": "WORKSPACE_EVIDENCE_CONTRACT_SCOPE_MISMATCH",
                "owner": "INTERNAL_PROTOCOL",
                "canonical_resource_id": str(binding.get("canonical_resource_id") or ""),
            })
            continue
        intent = {
            "schema_version": MUTATION_INTENT_SCHEMA_VERSION,
            "producer": RUNTIME_ATTESTER_PRODUCER,
            "canonical_resource_id": str(binding.get("canonical_resource_id") or ""),
            "resource_kind": "mutable_workspace",
            "plan_handle": str(binding.get("plan_handle") or ""),
            "binding_revision": _positive_int(
                binding.get("binding_revision"),
                code="FORMAL_WORKSPACE_BINDING_REVISION_MISSING",
                field="binding_revision",
            ),
            "formal_concrete_path": str(binding.get("formal_concrete_path") or ""),
            "operation": str(capability_contract.get("operation") or ""),
            "mutation": str(mutation.get("mutation") or "readwrite"),
            "effect_contract_hash": _digest(mutation),
            "evidence_contract": deepcopy(evidence_contract),
            "binding_value_roles": deepcopy(binding.get("binding_value_roles") or {}),
            "allow_idempotent_revalidation": bool(
                evidence_contract.get("allow_idempotent_revalidation") is True
            ),
        }
        intent["intent_contract_hash"] = _digest(intent)
        intents.append(intent)
    return WorkspaceAdmission(
        "contract_error" if any(v.get("owner") == "INTERNAL_PROTOCOL" for v in violations)
        else "candidate_rejected" if violations else "passed",
        tuple(rows),
        tuple(intents),
        tuple(violations),
    )


def _artifact_ref_path(value: Any, rows: Sequence[Mapping[str, Any]]) -> str:
    if not isinstance(value, Mapping):
        raise WorkspaceAttestationError(
            "ARTIFACT_BACKED_BINDING_REF_INVALID", observed_type=type(value).__name__
        )
    canonical = str(value.get("canonical_resource_id") or "").strip()
    resource_kind = str(value.get("resource_kind") or "").strip()
    if not canonical or resource_kind != "mutable_workspace":
        raise WorkspaceAttestationError(
            "ARTIFACT_BACKED_BINDING_REF_INVALID",
            canonical_resource_id=canonical,
            resource_kind=resource_kind,
            expected_resource_kind="mutable_workspace",
        )
    revision = _positive_int(
        value.get("binding_revision"),
        code="ARTIFACT_BACKED_BINDING_REF_INVALID",
        field="binding_revision",
    )
    matches = [
        row for row in rows
        if str(row.get("canonical_resource_id") or "") == canonical
        and int(row.get("binding_revision") or 0) == revision
        and str(row.get("resource_kind") or "") == resource_kind
    ]
    if len(matches) != 1:
        raise WorkspaceAttestationError(
            "ARTIFACT_BACKED_BINDING_REF_STALE",
            canonical_resource_id=canonical,
            binding_revision=revision,
            matching_binding_count=len(matches),
        )
    binding = matches[0]
    identity_mismatches = {
        field: {
            "expected": str(binding.get(field) or ""),
            "observed": str(value.get(field) or ""),
        }
        for field in ("plan_handle", "binding_id")
        if value.get(field) not in (None, "")
        and str(value.get(field) or "") != str(binding.get(field) or "")
    }
    path = str(binding.get("formal_concrete_path") or "").strip()
    if identity_mismatches or not path or not Path(path).is_absolute():
        raise WorkspaceAttestationError(
            "ARTIFACT_BACKED_BINDING_REF_STALE",
            canonical_resource_id=canonical,
            binding_revision=revision,
            identity_mismatches=identity_mismatches,
            formal_concrete_path=path,
        )
    return path


def resolve_artifact_backed_binding_value(
    value: Any,
    *,
    value_type: str,
    workspace_bindings: Sequence[Mapping[str, Any]],
) -> Any:
    if str(value_type or "") != "artifact_ref":
        return deepcopy(value)
    return _artifact_ref_path(value, workspace_bindings)


def _safe_relative_path(root: Path, relative: str) -> Path:
    candidate = Path(str(relative or ""))
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise WorkspaceAttestationError(
            "WORKSPACE_POSTCONDITION_PATH_INVALID", relative_path=str(relative)
        )
    resolved_root = root.resolve(strict=False)
    resolved = (resolved_root / candidate).resolve(strict=False)
    if os.path.commonpath([str(resolved_root), str(resolved)]) != str(resolved_root):
        raise WorkspaceAttestationError(
            "WORKSPACE_POSTCONDITION_PATH_ESCAPE", relative_path=str(relative)
        )
    return resolved


def _binding_candidates(action: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for name, raw in dict(action.get("produced_bindings") or {}).items():
        if isinstance(raw, Mapping) and "value" in raw:
            result[str(name)] = deepcopy(dict(raw))
    return result


def _render_relative_template(template: str, action: Mapping[str, Any], accepted_bindings=None, binding_value_roles=None) -> str:
    from agent_core.plan_bindings import binding_value_violation
    candidates = _binding_candidates(action)
    for name, value in dict(accepted_bindings or {}).items():
        accepted = {"type": "string", "value": value}
        if name in candidates and candidates[name] != accepted:
            raise WorkspaceAttestationError("WORKSPACE_POSTCONDITION_BINDING_CONFLICT", binding_name=name)
        candidates[name] = accepted

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        candidate = candidates.get(name)
        if not candidate or str(candidate.get("type") or "") != "string":
            raise WorkspaceAttestationError(
                "WORKSPACE_POSTCONDITION_BINDING_UNAVAILABLE", binding_name=name
            )
        value = candidate.get("value")
        violation = binding_value_violation(value, (binding_value_roles or {}).get(name))
        if violation:
            raise WorkspaceAttestationError(violation, binding_name=name)
        if not isinstance(value, str) or not value or "/" in value or "\\" in value:
            raise WorkspaceAttestationError(
                "WORKSPACE_POSTCONDITION_BINDING_INVALID",
                binding_name=name,
                observed_value=value,
            )
        return value

    return _BINDING_TOKEN_RE.sub(replace, str(template or ""))


def prepare_mutation_intents(
    admission: WorkspaceAdmission,
    *,
    task_id: str,
    plan_id: str,
    plan_revision: int,
    step_id: str,
    action_id: str,
    action_digest: str,
    action: Mapping[str, Any],
    accepted_bindings: Optional[Mapping[str, str]] = None,
) -> list[Dict[str, Any]]:
    # Accepted scalars must be supplied by the caller's current, scoped binder;
    # never trust similarly named model action metadata or parse shell env.
    if admission.status not in {"passed", "not_applicable"}:
        raise WorkspaceAttestationError(
            "WORKSPACE_ADMISSION_NOT_PASSED", admission_status=admission.status
        )
    results: list[Dict[str, Any]] = []
    for raw in admission.mutation_intents:
        intent = deepcopy(dict(raw))
        root = Path(str(intent.get("formal_concrete_path") or ""))
        resolved_conditions = []
        for index, condition_raw in enumerate(
            dict(intent.get("evidence_contract") or {}).get("postconditions") or []
        ):
            if not isinstance(condition_raw, Mapping):
                raise WorkspaceAttestationError(
                    "WORKSPACE_POSTCONDITION_INVALID", postcondition_index=index
                )
            condition = deepcopy(dict(condition_raw))
            relative = _render_relative_template(
                str(condition.get("relative_path") or ""), action, accepted_bindings,
                intent.get("binding_value_roles")
            )
            target = root if not relative else _safe_relative_path(root, relative)
            condition.update({
                "postcondition_index": index,
                "relative_path": relative,
                "formal_workspace_root": str(root),
                "formal_target_path": str(target),
            })
            resolved_conditions.append(condition)
        intent.update({
            "task_id": str(task_id),
            "plan_id": str(plan_id),
            "plan_revision": int(plan_revision),
            "step_id": str(step_id),
            "action_id": str(action_id),
            "action_digest": str(action_digest),
            "resolved_postconditions": resolved_conditions,
            "status": "prepared",
        })
        intent["intent_id"] = "mutation-intent-" + _digest(intent).split(":", 1)[1][:24]
        results.append(intent)
    return results


def capture_mutation_failure_logs(journal: Mapping[str, Any], *, capability_id: str) -> list[Dict[str, Any]]:
    """Snapshot the selected snpEff build's documented log, never arbitrary paths.

    The frozen mutation's predictor postcondition determines the database
    directory. Only build.log beside it, observed by this attempt's attester,
    is eligible. Reads require its exact captured hash and reject symlinks.
    A missing/unreadable log does not change the scientific outcome.
    """
    from agent_core.approved_resource_locator import open_approved_resource
    from agent_core.context_lineage import redact_text
    if capability_id != "genomics.snpeff_annotation.build_database":
        return []
    logs = []
    for intent in journal.get("intents", []) or []:
        if (intent.get("operation") != "build_database"
                or intent.get("mutation") != "build_database"
                or intent.get("status") in {"prepared", "started"}):
            continue
        root = Path(str(intent.get("formal_concrete_path") or ""))
        after = intent.get("after_evidence") or {}
        if not after.get("reliable") or str(root) != after.get("root"):
            continue
        for condition in intent.get("resolved_postconditions", []) or []:
            relative = Path(str(condition.get("relative_path") or ""))
            if (condition.get("kind") != "file_nonempty" or len(relative.parts) != 3
                    or relative.parts[0] != "data" or relative.name != "snpEffectPredictor.bin"
                    or relative.is_absolute() or ".." in relative.parts):
                continue
            log_relative = str(relative.with_name("build.log"))
            entry = next((row for row in after.get("entries", []) if row.get("relative_path") == log_relative), {})
            if entry.get("kind") != "file" or entry.get("symlink") or not entry.get("content_hash"):
                continue
            path = root / log_relative
            row = {"source": "runtime_attester_snapshot", "relative_path": log_relative,
                **{key: intent.get(key) for key in ("task_id", "plan_id", "plan_revision", "step_id", "action_id", "attempt_id", "intent_id")},
                "source_sha256": entry["content_hash"], "status": "unavailable"}
            try:
                # Ancestor checks precede opening, including workspace parents.
                if any(p.is_symlink() for p in reversed((path, *path.parents))):
                    raise ValueError("FAILURE_LOG_SYMLINK_DENIED")
                with open_approved_resource(path) as stream:
                    body = stream.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ValueError("FAILURE_LOG_SIZE_LIMIT")
                if "sha256:" + hashlib.sha256(body).hexdigest() != entry["content_hash"]:
                    raise ValueError("FAILURE_LOG_SNAPSHOT_CHANGED")
                text = redact_text(body.decode("utf-8"))
                row.update(status="captured", size_bytes=len(body),
                    text_sha256="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                    redacted=text.encode() != body,
                    # One physical line may exceed reader limits; reversible
                    # chunks keep the immutable view pageable without loss.
                    text_chunks=[text[i:i + 2000] for i in range(0, len(text), 2000)],
                    representation="join text_chunks; UTF-8 log snapshot, not completion evidence")
            except (ValueError, OSError, UnicodeError) as exc:
                row["unavailable_reason"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            logs.append(row)
    return logs


def _bounded_file_hash(path: Path, *, limit: int) -> str:
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > limit:
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return ""


def capture_bounded_workspace_snapshot(
    path_text: str,
    *,
    max_entries: int = 512,
    max_depth: int = 8,
    hash_file_limit: int = 1024 * 1024,
) -> Dict[str, Any]:
    root = Path(path_text)
    root_state = _path_state(str(root))
    entries: list[Dict[str, Any]] = []
    truncated = False
    walk_errors: list[str] = []
    if root_state.get("exists") is True and root_state.get("symlink") is True:
        result = {
            "root": str(root),
            "root_state": root_state,
            "entries": [],
            "truncated": False,
            "reliable": False,
            "unknown_reason": "workspace_root_is_symlink",
        }
        result["snapshot_digest"] = _digest(result)
        return result
    if root_state.get("kind") == "directory":
        try:
            def record_walk_error(exc: OSError) -> None:
                walk_errors.append(type(exc).__name__)

            for current, dirnames, filenames in os.walk(
                root,
                followlinks=False,
                onerror=record_walk_error,
            ):
                current_path = Path(current)
                depth = len(current_path.relative_to(root).parts)
                dirnames[:] = sorted(dirnames)
                filenames = sorted(filenames)
                if depth >= max_depth:
                    if dirnames:
                        truncated = True
                    dirnames[:] = []
                for name in [*dirnames, *filenames]:
                    target = current_path / name
                    relative = str(target.relative_to(root))
                    state = _path_state(str(target))
                    entry = {"relative_path": relative, **state}
                    if state.get("kind") == "file":
                        file_hash = _bounded_file_hash(target, limit=hash_file_limit)
                        if file_hash:
                            entry["content_hash"] = file_hash
                    entries.append(entry)
                    if len(entries) >= max_entries:
                        truncated = True
                        dirnames[:] = []
                        break
                if len(entries) >= max_entries:
                    break
        except OSError as exc:
            walk_errors.append(type(exc).__name__)
        if walk_errors:
            root_state["walk_error"] = walk_errors[0]
    elif root_state.get("kind") == "file":
        file_hash = _bounded_file_hash(root, limit=hash_file_limit)
        if file_hash:
            root_state["content_hash"] = file_hash
    result = {
        "root": str(root),
        "root_state": root_state,
        "entries": entries,
        "truncated": truncated,
        "limits": {
            "max_entries": int(max_entries),
            "max_depth": int(max_depth),
            "hash_file_limit": int(hash_file_limit),
        },
        "reliable": root_state.get("exists") in {True, False}
        and root_state.get("symlink") is not True
        and not truncated
        and not walk_errors,
    }
    if truncated:
        result["unknown_reason"] = "snapshot_bounds_exceeded"
    elif walk_errors:
        result["unknown_reason"] = "workspace_walk_failed"
    result["snapshot_digest"] = _digest(result)
    return result


def start_mutation_attestation(
    intents: Sequence[Mapping[str, Any]], *, attempt_id: str
) -> Dict[str, Any]:
    rows = []
    for raw in intents:
        intent = deepcopy(dict(raw))
        intent["attempt_id"] = str(attempt_id)
        intent["before_evidence"] = capture_bounded_workspace_snapshot(
            str(intent.get("formal_concrete_path") or "")
        )
        intent["status"] = "attempt_started"
        rows.append(intent)
    journal = {
        "schema_version": MUTATION_INTENT_SCHEMA_VERSION,
        "producer": RUNTIME_ATTESTER_PRODUCER,
        "attempt_id": str(attempt_id),
        "status": "attempt_started" if rows else "not_applicable",
        "started_at": _now(),
        "intents": rows,
        "receipts": [],
    }
    journal["journal_digest"] = _digest(
        {key: value for key, value in journal.items() if key != "journal_digest"}
    )
    return journal


def _evaluate_postcondition(condition: Mapping[str, Any]) -> Dict[str, Any]:
    kind = str(condition.get("kind") or "")
    root = Path(str(condition.get("formal_workspace_root") or ""))
    path = Path(str(condition.get("formal_target_path") or ""))
    relative = Path(str(condition.get("relative_path") or ""))
    path_safety_error = ""
    try:
        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        if (
            not root.is_absolute()
            or os.path.commonpath([str(resolved_root), str(resolved_path)])
            != str(resolved_root)
        ):
            path_safety_error = "postcondition_path_escape"
        else:
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    path_safety_error = "postcondition_path_contains_symlink"
                    break
    except (OSError, ValueError):
        path_safety_error = "postcondition_path_unresolvable"
    if path_safety_error:
        return {
            **deepcopy(dict(condition)),
            "status": "unknown",
            "reason": path_safety_error,
            "observed_state": _path_state(str(path)),
        }
    state = _path_state(str(path))
    if kind == "directory_exists":
        satisfied = state.get("kind") == "directory" and state.get("symlink") is False
    elif kind == "file_exists":
        satisfied = state.get("kind") == "file" and state.get("symlink") is False
    elif kind == "file_nonempty":
        satisfied = (
            state.get("kind") == "file"
            and state.get("symlink") is False
            and int(state.get("size") or 0) > 0
        )
    else:
        return {
            **deepcopy(dict(condition)),
            "status": "unknown",
            "reason": "unsupported_postcondition_kind",
            "observed_state": state,
        }
    return {
        **deepcopy(dict(condition)),
        "status": "satisfied" if satisfied else "unsatisfied",
        "observed_state": state,
    }


_RUNTIME_ACCESS_SCOPE_FIELDS = (
    "task_id",
    "plan_id",
    "plan_revision",
    "step_id",
    "action_id",
    "attempt_id",
    "action_digest",
    "intent_id",
    "canonical_resource_id",
    "resource_kind",
    "plan_handle",
    "binding_revision",
    "formal_concrete_path",
    "operation",
    "mutation",
    "effect_contract_hash",
)


def record_runtime_access_evidence(
    intent: Mapping[str, Any],
    *,
    observed_workspace_path: str,
    accessed_postcondition_paths: Sequence[str],
) -> Dict[str, Any]:
    """Mint Controller-only evidence from a trusted executor access observer.

    This is deliberately a separate input to finalization.  Generated action
    fields, stdout and model claims never flow through this constructor.  The
    caller must supply the workspace root and postcondition paths actually
    observed during this exact attempt; the attester will independently bind
    them to the immutable intent and require exact formal-scope coverage.
    """

    body = {
        "schema_version": RUNTIME_ACCESS_EVIDENCE_SCHEMA_VERSION,
        "producer": RUNTIME_ACCESS_EVIDENCE_PRODUCER,
        "evidence_kind": "attempt_scoped_postcondition_access",
        **{
            field: deepcopy(intent.get(field))
            for field in _RUNTIME_ACCESS_SCOPE_FIELDS
        },
        "access_observed": True,
        "observed_workspace_path": (
            str(Path(str(observed_workspace_path)))
            if str(observed_workspace_path or "").strip()
            else ""
        ),
        "accessed_postcondition_paths": sorted({
            str(Path(str(path)))
            for path in accessed_postcondition_paths
            if str(path or "").strip()
        }),
        "observed_at": _now(),
    }
    body["evidence_id"] = (
        "runtime-access-evidence-" + _digest(body).split(":", 1)[1][:24]
    )
    body["immutable_hash"] = _digest(body)
    return body


def _validate_runtime_access_evidence(
    evidence_rows: Sequence[Mapping[str, Any]],
    *,
    intent: Mapping[str, Any],
) -> Dict[str, Any]:
    intent_id = str(intent.get("intent_id") or "")
    matches = [
        deepcopy(dict(item))
        for item in evidence_rows or ()
        if isinstance(item, Mapping)
        and str(item.get("intent_id") or "") == intent_id
    ]
    if not matches:
        return {
            "valid": False,
            "code": "RUNTIME_ACCESS_EVIDENCE_MISSING",
            "evidence": {},
        }
    if len(matches) != 1:
        return {
            "valid": False,
            "code": "RUNTIME_ACCESS_EVIDENCE_AMBIGUOUS",
            "matching_evidence_count": len(matches),
            "evidence": {},
        }
    row = matches[0]
    expected_evidence_id = "runtime-access-evidence-" + _digest({
        key: value
        for key, value in row.items()
        if key not in {"evidence_id", "immutable_hash"}
    }).split(":", 1)[1][:24]
    expected_hash = _digest({
        key: value for key, value in row.items() if key != "immutable_hash"
    })
    scope_mismatches = {
        field: {"expected": intent.get(field), "observed": row.get(field)}
        for field in _RUNTIME_ACCESS_SCOPE_FIELDS
        if row.get(field) != intent.get(field)
    }
    expected_workspace_path = str(
        Path(str(intent.get("formal_concrete_path") or ""))
    ) if str(intent.get("formal_concrete_path") or "").strip() else ""
    observed_workspace_path = str(
        Path(str(row.get("observed_workspace_path") or ""))
    ) if str(row.get("observed_workspace_path") or "").strip() else ""
    workspace_path_valid = bool(
        expected_workspace_path
        and observed_workspace_path == expected_workspace_path
    )
    if not workspace_path_valid:
        scope_mismatches["observed_workspace_path"] = {
            "expected": expected_workspace_path,
            "observed": observed_workspace_path,
        }
    expected_paths = sorted({
        str(Path(str(item.get("formal_target_path") or "")))
        for item in intent.get("resolved_postconditions", []) or []
        if isinstance(item, Mapping)
        and str(item.get("formal_target_path") or "").strip()
    })
    observed_raw = row.get("accessed_postcondition_paths")
    observed_paths = (
        sorted({str(Path(str(item))) for item in observed_raw if str(item)})
        if isinstance(observed_raw, (list, tuple))
        else []
    )
    path_coverage_valid = bool(expected_paths) and observed_paths == expected_paths
    valid = bool(
        row.get("schema_version") == RUNTIME_ACCESS_EVIDENCE_SCHEMA_VERSION
        and row.get("producer") == RUNTIME_ACCESS_EVIDENCE_PRODUCER
        and row.get("evidence_kind")
        == "attempt_scoped_postcondition_access"
        and row.get("access_observed") is True
        and row.get("immutable_hash") == expected_hash
        and row.get("evidence_id") == expected_evidence_id
        and not scope_mismatches
        and workspace_path_valid
        and path_coverage_valid
    )
    return {
        "valid": valid,
        "code": (
            "RUNTIME_ACCESS_EVIDENCE_VALID"
            if valid
            else "RUNTIME_ACCESS_EVIDENCE_INVALID"
        ),
        "producer": row.get("producer"),
        "evidence_id": row.get("evidence_id"),
        "evidence_id_valid": row.get("evidence_id") == expected_evidence_id,
        "immutable_hash_valid": row.get("immutable_hash") == expected_hash,
        "scope_mismatches": scope_mismatches,
        "expected_workspace_path": expected_workspace_path,
        "observed_workspace_path": observed_workspace_path,
        "workspace_path_valid": workspace_path_valid,
        "expected_postcondition_paths": expected_paths,
        "observed_postcondition_paths": observed_paths,
        "path_coverage_valid": path_coverage_valid,
        "evidence": row,
    }


def finalize_mutation_attestation(
    journal: Mapping[str, Any], *, process_exit_code: Optional[int],
    interruption_reason: str = "",
    runtime_access_evidence: Sequence[Mapping[str, Any]] = (),
    controller_revalidation_evidence: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    from agent_core.workspace_revalidation import validate_controller_workspace_revalidation

    result = deepcopy(dict(journal))
    receipts: list[Dict[str, Any]] = []
    finalized_intents: list[Dict[str, Any]] = []
    for raw in result.get("intents", []) or []:
        intent = deepcopy(dict(raw))
        after = capture_bounded_workspace_snapshot(
            str(intent.get("formal_concrete_path") or "")
        )
        checks = [
            _evaluate_postcondition(item)
            for item in intent.get("resolved_postconditions", []) or []
            if isinstance(item, Mapping)
        ]
        postconditions_satisfied = bool(checks) and all(
            item.get("status") == "satisfied" for item in checks
        )
        before = dict(intent.get("before_evidence") or {})
        reliable = bool(before.get("reliable") and after.get("reliable"))
        changed = str(before.get("snapshot_digest") or "") != str(
            after.get("snapshot_digest") or ""
        )
        idempotent = bool(intent.get("allow_idempotent_revalidation") is True)
        access_validation = _validate_runtime_access_evidence(
            runtime_access_evidence,
            intent=intent,
        )
        revalidation = validate_controller_workspace_revalidation(
            controller_revalidation_evidence,
            intent=intent,
            after_snapshot=after,
        )
        attested_effect = ""
        if process_exit_code is None:
            status = "unknown"
            attested = False
        elif int(process_exit_code) != 0:
            status = "process_failed"
            attested = False
        elif not reliable:
            status = "unknown"
            attested = False
        elif not postconditions_satisfied:
            status = "postcondition_unsatisfied"
            attested = False
        elif (
            revalidation.get("code") != "WORKSPACE_REVALIDATION_EVIDENCE_MISSING"
            and revalidation.get("valid") is not True
        ):
            # An observed change after Controller verification cannot be
            # relabelled as a successful mutation by the already exited script.
            status = {
                "WORKSPACE_REVALIDATION_CONTRACT_MISSING": "idempotent_revalidation_contract_missing",
                "WORKSPACE_REVALIDATION_CONTRACT_INVALID": "idempotent_revalidation_contract_invalid",
                "WORKSPACE_REVALIDATION_FAILED": "idempotent_revalidation_failed",
                "WORKSPACE_REVALIDATION_LIMIT_EXCEEDED": "idempotent_revalidation_failed",
            }.get(str(revalidation.get("code") or ""), "idempotent_revalidation_evidence_invalid")
            attested = False
        elif changed:
            status = "mutated_and_validated"
            attested = True
            attested_effect = "mutation"
        elif idempotent and (
            access_validation.get("valid") is True
            or revalidation.get("valid") is True
        ):
            status = "revalidated_unchanged"
            attested = True
            attested_effect = "revalidation/adoption"
        elif idempotent:
            # Exit zero plus old files is not proof that this attempt accessed or
            # validated the formal workspace.  Only Controller-owned,
            # attempt-scoped access evidence can authorize unchanged adoption.
            status = (
                "idempotent_revalidation_evidence_missing"
                if access_validation.get("code")
                == "RUNTIME_ACCESS_EVIDENCE_MISSING"
                else "idempotent_revalidation_evidence_invalid"
            )
            attested = False
        else:
            status = "unchanged_not_attributable"
            attested = False
        receipt_body = {
            "schema_version": MUTATION_RECEIPT_SCHEMA_VERSION,
            "producer": RUNTIME_ATTESTER_PRODUCER,
            "task_id": str(intent.get("task_id") or ""),
            "plan_id": str(intent.get("plan_id") or ""),
            "plan_revision": int(intent.get("plan_revision") or 0),
            "step_id": str(intent.get("step_id") or ""),
            "action_id": str(intent.get("action_id") or ""),
            "attempt_id": str(intent.get("attempt_id") or ""),
            "action_digest": str(intent.get("action_digest") or ""),
            "intent_id": str(intent.get("intent_id") or ""),
            "canonical_resource_id": str(intent.get("canonical_resource_id") or ""),
            "resource_kind": str(intent.get("resource_kind") or ""),
            "workspace_ref": str(intent.get("canonical_resource_id") or ""),
            "plan_handle": str(intent.get("plan_handle") or ""),
            "binding_revision": int(intent.get("binding_revision") or 0),
            "formal_concrete_path": str(intent.get("formal_concrete_path") or ""),
            "operation": str(intent.get("operation") or ""),
            "mutation": str(intent.get("mutation") or ""),
            "effect_contract_hash": str(intent.get("effect_contract_hash") or ""),
            "before_evidence": before,
            "after_evidence": after,
            "postcondition_results": checks,
            "changed_during_attempt": changed,
            "effect": attested_effect,
            "attestation_status": status,
            "attested": attested,
            "postcondition_satisfied": bool(postconditions_satisfied),
            "process_exit_code": process_exit_code,
            "unknown_reason": (
                str(interruption_reason or "execution_outcome_unavailable")
                if process_exit_code is None
                else ""
            ),
            "runtime_access_evidence": deepcopy(
                access_validation.get("evidence") or {}
            ),
            "runtime_access_evidence_validation": {
                key: deepcopy(value)
                for key, value in access_validation.items()
                if key != "evidence"
            },
            "controller_revalidation_evidence": deepcopy(
                revalidation.get("evidence") or {}
            ),
            "controller_revalidation_evidence_validation": {
                key: deepcopy(value)
                for key, value in revalidation.items()
                if key != "evidence"
            },
            "finished_at": _now(),
        }
        receipt_body["receipt_id"] = (
            "workspace-receipt-" + _digest(receipt_body).split(":", 1)[1][:24]
        )
        receipt_body["immutable_hash"] = _digest(receipt_body)
        receipts.append(receipt_body)
        intent.update({
            "after_evidence": after,
            "postcondition_results": checks,
            "status": status,
            "receipt_id": receipt_body["receipt_id"],
        })
        finalized_intents.append(intent)
    result.update({
        "intents": finalized_intents,
        "receipts": receipts,
        "status": (
            "completed"
            if all(item.get("attested") for item in receipts)
            else "reconciliation_required"
            if receipts
            else "not_applicable"
        ),
        "finished_at": _now(),
    })
    result["journal_digest"] = _digest(
        {key: value for key, value in result.items() if key != "journal_digest"}
    )
    return result


def validate_runtime_mutation_receipt(
    receipt: Mapping[str, Any], *, intent: Mapping[str, Any]
) -> Dict[str, Any]:
    row = deepcopy(dict(receipt or {}))
    expected_hash = _digest({
        key: value for key, value in row.items() if key != "immutable_hash"
    })
    scope_fields = (
        "task_id",
        "plan_id",
        "plan_revision",
        "step_id",
        "action_id",
        "attempt_id",
        "action_digest",
        "intent_id",
        "canonical_resource_id",
        "resource_kind",
        "plan_handle",
        "binding_revision",
        "formal_concrete_path",
        "operation",
        "mutation",
        "effect_contract_hash",
    )
    mismatches = {
        field: {"expected": intent.get(field), "observed": row.get(field)}
        for field in scope_fields
        if row.get(field) != intent.get(field)
    }
    valid = bool(
        row.get("schema_version") == MUTATION_RECEIPT_SCHEMA_VERSION
        and
        row.get("producer") == RUNTIME_ATTESTER_PRODUCER
        and row.get("attested") is True
        and row.get("immutable_hash") == expected_hash
        and not mismatches
    )
    return {
        "valid": valid,
        "code": "WORKSPACE_MUTATION_RECEIPT_VALID"
        if valid
        else "WORKSPACE_MUTATION_RECEIPT_INVALID",
        "mismatches": mismatches,
        "producer": row.get("producer"),
        "immutable_hash_valid": row.get("immutable_hash") == expected_hash,
    }


def persist_mutation_journal(path: Path, journal: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        journal, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return path


def load_mutation_journal(path: Path) -> Dict[str, Any]:
    """Reload one durable journal and fail closed on truncation or tampering."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RELOAD_FAILED",
            journal_path=str(path),
            error_type=type(exc).__name__,
        ) from exc
    if not isinstance(raw, Mapping):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INVALID",
            journal_path=str(path),
            observed_type=type(raw).__name__,
        )
    journal = deepcopy(dict(raw))
    expected_digest = _digest({
        key: value for key, value in journal.items() if key != "journal_digest"
    })
    if (
        journal.get("schema_version") != MUTATION_INTENT_SCHEMA_VERSION
        or journal.get("producer") != RUNTIME_ATTESTER_PRODUCER
        or journal.get("journal_digest") != expected_digest
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTEGRITY_INVALID",
            journal_path=str(path),
            producer=journal.get("producer"),
            schema_version=journal.get("schema_version"),
            journal_digest_valid=(
                journal.get("journal_digest") == expected_digest
            ),
        )
    return journal


def _required_registration_text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_REFERENCE_FIELD_INVALID",
            field=field,
            observed_type=type(value).__name__,
        )
    return value


def _registration_intent_ids(value: Any, *, field: str) -> Tuple[str, ...]:
    if type(value) not in {list, tuple}:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_REFERENCE_FIELD_INVALID",
            field=field,
            observed_type=type(value).__name__,
        )
    intent_ids = tuple(value)
    if any(type(item) is not str or not item.strip() for item in intent_ids):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTENT_IDS_INVALID",
            field=field,
            observed_types=[type(item).__name__ for item in intent_ids],
        )
    if len(set(intent_ids)) != len(intent_ids):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTENT_IDS_INVALID",
            field=field,
            reason="duplicate_intent_id",
        )
    return intent_ids


def _verified_bounded_snapshot(
    value: Any,
    *,
    journal_path: str,
    intent_id: str,
    field: str,
    expected_root: str,
    require_reliable: bool,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_SNAPSHOT_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            field=field,
            reason="snapshot_type_invalid",
            observed_type=type(value).__name__,
        )
    snapshot = deepcopy(dict(value))
    observed_root = snapshot.get("root")
    if type(observed_root) is not str or observed_root != expected_root:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_SNAPSHOT_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            field=f"{field}.root",
            expected_root=expected_root,
            observed_root=observed_root,
        )
    if not isinstance(snapshot.get("root_state"), Mapping):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_SNAPSHOT_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            field=f"{field}.root_state",
            reason="root_state_type_invalid",
        )
    entries = snapshot.get("entries")
    if type(entries) is not list or any(
        not isinstance(item, Mapping) for item in entries
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_SNAPSHOT_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            field=f"{field}.entries",
            reason="entries_type_invalid",
        )
    if type(snapshot.get("truncated")) is not bool or type(
        snapshot.get("reliable")
    ) is not bool:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_SNAPSHOT_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            field=field,
            reason="snapshot_flags_invalid",
        )
    snapshot_digest = snapshot.get("snapshot_digest")
    expected_snapshot_digest = _digest({
        key: item
        for key, item in snapshot.items()
        if key != "snapshot_digest"
    })
    if (
        type(snapshot_digest) is not str
        or snapshot_digest != expected_snapshot_digest
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_SNAPSHOT_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            field=f"{field}.snapshot_digest",
            reason="snapshot_digest_invalid",
        )
    if require_reliable and snapshot.get("reliable") is not True:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_SNAPSHOT_UNRELIABLE",
            journal_path=journal_path,
            intent_id=intent_id,
            field=field,
            unknown_reason=str(snapshot.get("unknown_reason") or ""),
        )
    return snapshot


def _verified_finalized_receipt(
    value: Any,
    *,
    journal_path: str,
    intent: Mapping[str, Any],
    receipt_index: int,
) -> Dict[str, Any]:
    intent_id = str(intent.get("intent_id") or "")
    if not isinstance(value, Mapping):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RECEIPT_INTEGRITY_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            receipt_index=receipt_index,
            reason="receipt_type_invalid",
        )
    receipt = deepcopy(dict(value))
    if (
        type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != MUTATION_RECEIPT_SCHEMA_VERSION
        or type(receipt.get("producer")) is not str
        or receipt.get("producer") != RUNTIME_ATTESTER_PRODUCER
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RECEIPT_INTEGRITY_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            receipt_index=receipt_index,
            reason="receipt_schema_or_producer_invalid",
        )
    receipt_without_hash = {
        key: item for key, item in receipt.items() if key != "immutable_hash"
    }
    if (
        type(receipt.get("immutable_hash")) is not str
        or receipt.get("immutable_hash") != _digest(receipt_without_hash)
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RECEIPT_INTEGRITY_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            receipt_index=receipt_index,
            reason="receipt_immutable_hash_invalid",
        )
    receipt_body = {
        key: item
        for key, item in receipt.items()
        if key not in {"receipt_id", "immutable_hash"}
    }
    expected_receipt_id = (
        "workspace-receipt-" + _digest(receipt_body).split(":", 1)[1][:24]
    )
    if (
        type(receipt.get("receipt_id")) is not str
        or receipt.get("receipt_id") != expected_receipt_id
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RECEIPT_INTEGRITY_INVALID",
            journal_path=journal_path,
            intent_id=intent_id,
            receipt_index=receipt_index,
            reason="receipt_id_invalid",
        )
    raw_binding_revision = intent.get("binding_revision")
    if raw_binding_revision is None or (
        type(raw_binding_revision) is str and raw_binding_revision == ""
    ):
        expected_binding_revision = 0
    elif type(raw_binding_revision) is int:
        expected_binding_revision = raw_binding_revision
    else:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RECEIPT_SCOPE_MISMATCH",
            journal_path=journal_path,
            intent_id=intent_id,
            receipt_index=receipt_index,
            field="binding_revision",
            reason="intent_scope_type_invalid",
            observed_type=type(raw_binding_revision).__name__,
        )
    expected_scope = {
        "task_id": str(intent.get("task_id") or ""),
        "plan_id": str(intent.get("plan_id") or ""),
        "plan_revision": int(intent.get("plan_revision") or 0),
        "step_id": str(intent.get("step_id") or ""),
        "action_id": str(intent.get("action_id") or ""),
        "attempt_id": str(intent.get("attempt_id") or ""),
        "action_digest": str(intent.get("action_digest") or ""),
        "intent_id": str(intent.get("intent_id") or ""),
        "canonical_resource_id": str(
            intent.get("canonical_resource_id") or ""
        ),
        "resource_kind": str(intent.get("resource_kind") or ""),
        "workspace_ref": str(intent.get("canonical_resource_id") or ""),
        "plan_handle": str(intent.get("plan_handle") or ""),
        "binding_revision": expected_binding_revision,
        "formal_concrete_path": str(
            intent.get("formal_concrete_path") or ""
        ),
        "operation": str(intent.get("operation") or ""),
        "mutation": str(intent.get("mutation") or ""),
        "effect_contract_hash": str(
            intent.get("effect_contract_hash") or ""
        ),
    }
    mismatches = {
        field: {
            "expected": deepcopy(expected_value),
            "observed": deepcopy(receipt.get(field)),
        }
        for field, expected_value in expected_scope.items()
        if type(receipt.get(field)) is not type(expected_value)
        or receipt.get(field) != expected_value
    }
    if mismatches:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RECEIPT_SCOPE_MISMATCH",
            journal_path=journal_path,
            intent_id=intent_id,
            receipt_index=receipt_index,
            mismatches=mismatches,
        )
    return receipt


def _mutation_registration_projection(
    journal: Mapping[str, Any],
    *,
    journal_path: str,
    task_id: str,
    plan_id: str,
    plan_revision: int,
    step_id: str,
    action_id: str,
    action_digest: str,
    attempt_id: str,
) -> Dict[str, Any]:
    """Return the immutable portion registered before command dispatch.

    A journal is finalized in place.  Only fields that the existing
    finalization/reconciliation functions own are removed here.  Unknown new
    fields remain part of the digest and therefore fail closed until their
    lifecycle semantics are explicitly defined.
    """

    immutable_journal = deepcopy(dict(journal))
    for field in ("journal_digest", "status", "receipts", "finished_at"):
        immutable_journal.pop(field, None)
    immutable_intents: list[Dict[str, Any]] = []
    for raw in immutable_journal.get("intents", []):
        intent = deepcopy(dict(raw))
        for field in (
            "status",
            "after_evidence",
            "postcondition_results",
            "receipt_id",
        ):
            intent.pop(field, None)
        immutable_intents.append(intent)
    immutable_journal["intents"] = immutable_intents
    return {
        "schema_version": MUTATION_INTENT_REGISTRATION_SCHEMA_VERSION,
        "event_type": WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
        "task_id": task_id,
        "plan_id": plan_id,
        "plan_revision": plan_revision,
        "step_id": step_id,
        "action_id": action_id,
        "action_digest": action_digest,
        "attempt_id": attempt_id,
        "journal_path": journal_path,
        "immutable_journal": immutable_journal,
    }


def load_verified_mutation_journal_reference(
    journal_path: str | Path,
    *,
    expected_attempt_dir: str | Path,
    expected_task_id: str,
    expected_plan_id: str,
    expected_plan_revision: int,
    expected_step_id: str,
    expected_action_id: str,
    expected_action_digest: str,
    expected_attempt_id: str,
    expected_journal_digest: str,
    expected_intent_ids: Sequence[str],
) -> VerifiedMutationJournalReference:
    """Load and bind a mutation journal to one trusted provenance attempt.

    Expected scope values come from PlanTracker's current attempt/action, not
    from the event payload or model output.  The function intentionally does
    not coerce malformed values into the formal schema.
    """

    task_id = _required_registration_text(
        expected_task_id, field="expected_task_id"
    )
    plan_id = _required_registration_text(
        expected_plan_id, field="expected_plan_id"
    )
    step_id = _required_registration_text(
        expected_step_id, field="expected_step_id"
    )
    action_id = _required_registration_text(
        expected_action_id, field="expected_action_id"
    )
    attempt_id = _required_registration_text(
        expected_attempt_id, field="expected_attempt_id"
    )
    expected_digest = _required_registration_text(
        expected_journal_digest, field="expected_journal_digest"
    )
    if (
        not expected_digest.startswith("sha256:")
        or len(expected_digest) != len("sha256:") + 64
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_REFERENCE_FIELD_INVALID",
            field="expected_journal_digest",
            reason="invalid_sha256_digest",
        )
    action_digest = _required_registration_text(
        expected_action_digest, field="expected_action_digest"
    )
    if (
        type(expected_plan_revision) is not int
        or expected_plan_revision < 1
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_REFERENCE_FIELD_INVALID",
            field="expected_plan_revision",
            observed_type=type(expected_plan_revision).__name__,
            observed_value=expected_plan_revision,
        )
    expected_ids = _registration_intent_ids(
        expected_intent_ids, field="expected_intent_ids"
    )
    if not isinstance(journal_path, (str, os.PathLike)):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_REFERENCE_FIELD_INVALID",
            field="journal_path",
            observed_type=type(journal_path).__name__,
        )
    if not isinstance(expected_attempt_dir, (str, os.PathLike)):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_REFERENCE_FIELD_INVALID",
            field="expected_attempt_dir",
            observed_type=type(expected_attempt_dir).__name__,
        )
    raw_path = Path(journal_path)
    raw_attempt_dir = Path(expected_attempt_dir)
    if not raw_path.is_absolute() or not raw_attempt_dir.is_absolute():
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_PATH_SCOPE_MISMATCH",
            journal_path=str(raw_path),
            expected_attempt_dir=str(raw_attempt_dir),
            reason="path_or_attempt_dir_not_absolute",
        )
    try:
        canonical_path = raw_path.resolve(strict=True)
        canonical_attempt_dir = raw_attempt_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RELOAD_FAILED",
            journal_path=str(raw_path),
            error_type=type(exc).__name__,
        ) from exc
    if (
        raw_path != canonical_path
        or raw_attempt_dir != canonical_attempt_dir
        or not canonical_path.is_file()
        or not canonical_attempt_dir.is_dir()
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_PATH_SCOPE_MISMATCH",
            journal_path=str(raw_path),
            canonical_path=str(canonical_path),
            expected_attempt_dir=str(raw_attempt_dir),
            reason="path_not_canonical_regular_file",
        )
    path_scope_valid = bool(
        canonical_path.name == "mutation_intent.json"
        and canonical_path == canonical_attempt_dir / "mutation_intent.json"
        and canonical_attempt_dir.name == attempt_id
        and canonical_attempt_dir.parent.name == "attempts"
        and canonical_attempt_dir.parent.parent.name == task_id
        and canonical_attempt_dir.parent.parent.parent.name == "task_outputs"
    )
    if not path_scope_valid:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_PATH_SCOPE_MISMATCH",
            journal_path=str(canonical_path),
            expected_task_id=task_id,
            expected_attempt_id=attempt_id,
        )

    journal = load_mutation_journal(canonical_path)
    if type(journal.get("schema_version")) is not int:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTEGRITY_INVALID",
            journal_path=str(canonical_path),
            reason="schema_version_type_invalid",
        )
    if (
        type(journal.get("producer")) is not str
        or journal.get("producer") != RUNTIME_ATTESTER_PRODUCER
    ):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTEGRITY_INVALID",
            journal_path=str(canonical_path),
            reason="producer_invalid",
        )
    observed_attempt_id = journal.get("attempt_id")
    if type(observed_attempt_id) is not str or observed_attempt_id != attempt_id:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_ATTEMPT_SCOPE_MISMATCH",
            journal_path=str(canonical_path),
            expected_attempt_id=attempt_id,
            observed_attempt_id=observed_attempt_id,
        )
    observed_digest = journal.get("journal_digest")
    if type(observed_digest) is not str or observed_digest != expected_digest:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_DIGEST_MISMATCH",
            journal_path=str(canonical_path),
            expected_journal_digest=expected_digest,
            observed_journal_digest=observed_digest,
        )
    status = journal.get("status")
    if type(status) is not str or status not in {
        "attempt_started",
        "not_applicable",
        "completed",
        "reconciliation_required",
    }:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_LIFECYCLE_INVALID",
            journal_path=str(canonical_path),
            observed_status=status,
        )
    started_at = journal.get("started_at")
    if type(started_at) is not str or not started_at.strip():
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_LIFECYCLE_INVALID",
            journal_path=str(canonical_path),
            field="started_at",
        )
    finished_at = journal.get("finished_at", "")
    if type(finished_at) is not str:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_LIFECYCLE_INVALID",
            journal_path=str(canonical_path),
            field="finished_at",
            observed_type=type(finished_at).__name__,
        )
    intents = journal.get("intents")
    receipts = journal.get("receipts")
    if type(intents) is not list or type(receipts) is not list:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTEGRITY_INVALID",
            journal_path=str(canonical_path),
            reason="intents_or_receipts_type_invalid",
        )
    empty_lifecycle_valid = bool(
        not intents
        and (
            (not finished_at and status == "not_applicable")
            or (finished_at and status == "completed")
        )
    )
    nonempty_lifecycle_valid = bool(
        intents
        and (
            (not finished_at and status == "attempt_started")
            or (
                finished_at
                and status in {"completed", "reconciliation_required"}
            )
        )
    )
    if not (empty_lifecycle_valid or nonempty_lifecycle_valid):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_LIFECYCLE_INVALID",
            journal_path=str(canonical_path),
            observed_status=status,
            intent_count=len(intents),
        )
    if not finished_at and "finished_at" in journal:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_LIFECYCLE_INVALID",
            journal_path=str(canonical_path),
            observed_status=status,
            reason="prelaunch_has_finished_at",
        )
    if not intents and receipts:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTEGRITY_INVALID",
            journal_path=str(canonical_path),
            reason="receipt_without_intent",
        )
    if intents and not finished_at and receipts:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_LIFECYCLE_INVALID",
            journal_path=str(canonical_path),
            observed_status=status,
            reason="attempt_started_has_receipts",
            receipt_count=len(receipts),
        )
    if intents and finished_at and len(receipts) != len(intents):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_RECEIPT_CARDINALITY_INVALID",
            journal_path=str(canonical_path),
            intent_count=len(intents),
            receipt_count=len(receipts),
        )

    observed_ids: list[str] = []
    verified_intents: list[Dict[str, Any]] = []
    verified_before_evidence: list[Dict[str, Any]] = []
    expected_scope = {
        "task_id": task_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "action_id": action_id,
        "attempt_id": attempt_id,
    }
    for index, raw_intent in enumerate(intents):
        if not isinstance(raw_intent, Mapping):
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_INTEGRITY_INVALID",
                journal_path=str(canonical_path),
                intent_index=index,
                reason="intent_type_invalid",
            )
        intent = dict(raw_intent)
        intent_id = intent.get("intent_id")
        if type(intent_id) is not str or not intent_id.strip():
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_INTENT_IDS_INVALID",
                journal_path=str(canonical_path),
                intent_index=index,
            )
        prepared_intent = deepcopy(intent)
        for field in (
            "intent_id",
            "attempt_id",
            "before_evidence",
            "after_evidence",
            "postcondition_results",
            "receipt_id",
        ):
            prepared_intent.pop(field, None)
        prepared_intent["status"] = "prepared"
        expected_intent_id = (
            "mutation-intent-"
            + _digest(prepared_intent).split(":", 1)[1][:24]
        )
        if intent_id != expected_intent_id:
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_INTENT_ID_INTEGRITY_INVALID",
                journal_path=str(canonical_path),
                intent_index=index,
                observed_intent_id=intent_id,
                expected_intent_id=expected_intent_id,
            )
        observed_ids.append(intent_id)
        for field, expected_value in expected_scope.items():
            observed_value = intent.get(field)
            if type(observed_value) is not str or observed_value != expected_value:
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_SCOPE_MISMATCH",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    field=field,
                    expected_value=expected_value,
                    observed_value=observed_value,
                )
        observed_revision = intent.get("plan_revision")
        if (
            type(observed_revision) is not int
            or observed_revision != expected_plan_revision
        ):
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_SCOPE_MISMATCH",
                journal_path=str(canonical_path),
                intent_id=intent_id,
                field="plan_revision",
                expected_value=expected_plan_revision,
                observed_value=observed_revision,
            )
        observed_action_digest = intent.get("action_digest")
        if (
            type(observed_action_digest) is not str
            or not observed_action_digest.strip()
            or observed_action_digest != action_digest
        ):
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_SCOPE_MISMATCH",
                journal_path=str(canonical_path),
                intent_id=intent_id,
                field="action_digest",
                expected_value=action_digest,
                observed_value=observed_action_digest,
            )
        formal_concrete_path = intent.get("formal_concrete_path")
        if (
            type(formal_concrete_path) is not str
            or not formal_concrete_path.strip()
        ):
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_SNAPSHOT_INVALID",
                journal_path=str(canonical_path),
                intent_id=intent_id,
                field="formal_concrete_path",
                reason="formal_workspace_root_invalid",
            )
        before_evidence = _verified_bounded_snapshot(
            intent.get("before_evidence"),
            journal_path=str(canonical_path),
            intent_id=intent_id,
            field="before_evidence",
            expected_root=formal_concrete_path,
            require_reliable=True,
        )
        if not finished_at:
            finalized_only_fields = [
                field
                for field in (
                    "after_evidence",
                    "postcondition_results",
                    "receipt_id",
                )
                if field in intent
            ]
            if (
                intent.get("status") != "attempt_started"
                or finalized_only_fields
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_LIFECYCLE_INVALID",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    observed_intent_status=intent.get("status"),
                    forbidden_fields=finalized_only_fields,
                )
        verified_intents.append(intent)
        verified_before_evidence.append(before_evidence)
    observed_ids_tuple = _registration_intent_ids(
        observed_ids, field="journal.intent_ids"
    )
    if observed_ids_tuple != expected_ids:
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_INTENT_IDS_MISMATCH",
            journal_path=str(canonical_path),
            expected_intent_ids=list(expected_ids),
            observed_intent_ids=list(observed_ids_tuple),
        )

    if intents and finished_at:
        finalized_statuses = {
            "unknown",
            "process_failed",
            "postcondition_unsatisfied",
            "mutated_and_validated",
            "revalidated_unchanged",
            "idempotent_revalidation_evidence_missing",
            "idempotent_revalidation_evidence_invalid",
            "idempotent_revalidation_contract_missing",
            "idempotent_revalidation_contract_invalid",
            "idempotent_revalidation_failed",
            "unchanged_not_attributable",
        }
        verified_receipts: list[Dict[str, Any]] = []
        for index, (intent, receipt_raw) in enumerate(
            zip(verified_intents, receipts)
        ):
            intent_id = str(intent["intent_id"])
            intent_status = intent.get("status")
            if (
                type(intent_status) is not str
                or intent_status not in finalized_statuses
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_LIFECYCLE_INVALID",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    observed_intent_status=intent_status,
                )
            after_evidence = _verified_bounded_snapshot(
                intent.get("after_evidence"),
                journal_path=str(canonical_path),
                intent_id=intent_id,
                field="after_evidence",
                expected_root=str(intent["formal_concrete_path"]),
                require_reliable=False,
            )
            postcondition_results = intent.get("postcondition_results")
            if type(postcondition_results) is not list or any(
                not isinstance(item, Mapping)
                for item in postcondition_results
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_LIFECYCLE_INVALID",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    field="postcondition_results",
                    reason="postcondition_results_type_invalid",
                )
            receipt = _verified_finalized_receipt(
                receipt_raw,
                journal_path=str(canonical_path),
                intent=intent,
                receipt_index=index,
            )
            receipt_id = intent.get("receipt_id")
            if (
                type(receipt_id) is not str
                or receipt_id != receipt.get("receipt_id")
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_RECEIPT_SCOPE_MISMATCH",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    receipt_index=index,
                    field="receipt_id",
                    expected_value=receipt_id,
                    observed_value=receipt.get("receipt_id"),
                )
            if (
                receipt.get("before_evidence")
                != verified_before_evidence[index]
                or receipt.get("after_evidence") != after_evidence
                or receipt.get("postcondition_results")
                != postcondition_results
                or receipt.get("attestation_status") != intent_status
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_RECEIPT_SCOPE_MISMATCH",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    receipt_index=index,
                    reason="receipt_evidence_or_status_mismatch",
                )
            if type(receipt.get("attested")) is not bool:
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_RECEIPT_INTEGRITY_INVALID",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    receipt_index=index,
                    reason="receipt_attested_type_invalid",
                )
            expected_attested = intent_status in {
                "mutated_and_validated",
                "revalidated_unchanged",
            }
            if receipt.get("attested") is not expected_attested:
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_LIFECYCLE_INVALID",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    receipt_index=index,
                    observed_intent_status=intent_status,
                    observed_attested=receipt.get("attested"),
                    expected_attested=expected_attested,
                )
            if (
                type(receipt.get("finished_at")) is not str
                or not receipt.get("finished_at", "").strip()
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_RECEIPT_INTEGRITY_INVALID",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    receipt_index=index,
                    reason="receipt_finished_at_invalid",
                )
            if receipt.get("attested") is True and (
                verified_before_evidence[index].get("reliable") is not True
                or after_evidence.get("reliable") is not True
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_JOURNAL_SNAPSHOT_UNRELIABLE",
                    journal_path=str(canonical_path),
                    intent_id=intent_id,
                    field="attested_receipt_evidence",
                )
            verified_receipts.append(receipt)
        all_attested = all(
            receipt.get("attested") is True
            for receipt in verified_receipts
        )
        expected_final_status = (
            "completed" if all_attested else "reconciliation_required"
        )
        if status != expected_final_status:
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_LIFECYCLE_INVALID",
                journal_path=str(canonical_path),
                observed_status=status,
                expected_status=expected_final_status,
            )

    projection = _mutation_registration_projection(
        journal,
        journal_path=str(canonical_path),
        task_id=task_id,
        plan_id=plan_id,
        plan_revision=expected_plan_revision,
        step_id=step_id,
        action_id=action_id,
        action_digest=action_digest,
        attempt_id=attempt_id,
    )
    return VerifiedMutationJournalReference(
        schema_version=MUTATION_INTENT_REGISTRATION_SCHEMA_VERSION,
        event_type=WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
        producer=RUNTIME_ATTESTER_PRODUCER,
        task_id=task_id,
        plan_id=plan_id,
        plan_revision=expected_plan_revision,
        step_id=step_id,
        action_id=action_id,
        action_digest=action_digest,
        attempt_id=attempt_id,
        journal_path=str(canonical_path),
        journal_digest=observed_digest,
        registration_digest=_digest(projection),
        journal_status=status,
        journal_finished_at=finished_at,
        intent_ids=observed_ids_tuple,
    )


def reconcile_started_mutation_journal(
    path: Path,
    *,
    reason: str,
    expected_attempt_id: str = "",
) -> Dict[str, Any]:
    """Close an orphaned started journal as unknown without replaying its action.

    The exact attempt file is the durable source.  Reconciliation is idempotent:
    finalized journals are returned unchanged, while only ``attempt_started``
    journals receive one fail-closed terminal observation.
    """

    journal = load_mutation_journal(path)
    observed_attempt_id = str(journal.get("attempt_id") or "")
    if expected_attempt_id and observed_attempt_id != str(expected_attempt_id):
        raise WorkspaceAttestationError(
            "MUTATION_JOURNAL_ATTEMPT_SCOPE_MISMATCH",
            journal_path=str(path),
            expected_attempt_id=str(expected_attempt_id),
            observed_attempt_id=observed_attempt_id,
        )
    if str(journal.get("status") or "") != "attempt_started":
        return journal
    reconciled = finalize_mutation_attestation(
        journal,
        process_exit_code=None,
        interruption_reason=str(reason or "controller_interrupted"),
    )
    persist_mutation_journal(path, reconciled)
    return load_mutation_journal(path)

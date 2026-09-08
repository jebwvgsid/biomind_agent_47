"""Opaque, plan-scoped value transport between successful actions.

The managed binder owns identity, persistence, scope and exact copying. It never
derives a value from executable source and never interprets domain meaning.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,126}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_VALUE_TYPES = {"string", "integer", "number", "boolean", "json", "artifact_ref"}
_RESOURCE_KINDS = {"immutable_artifact", "mutable_workspace"}
_INITIALLY_AVAILABLE_ORIGINS = {
    "external",
    "verified_task_input",
    "admitted_resume_version",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class PlanBindingError(ValueError):
    def __init__(self, code: str, **diagnostic: Any) -> None:
        self.code = str(code)
        self.diagnostic = deepcopy(diagnostic)
        super().__init__(f"{self.code}: {_canonical(self.diagnostic)}")

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, **deepcopy(self.diagnostic)}


@dataclass(frozen=True)
class OperationBindingAdmission:
    """Route-free result of formal operation and partial binding admission."""

    status: str
    operation: str
    capability_id: str
    binding_applicable: bool
    authority: str
    contract_source: str
    contract_revision: str
    violations: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "violations": [deepcopy(item) for item in self.violations],
        }


@dataclass(frozen=True)
class PlanResourceBinding:
    """One explicit alias-to-resource identity used by Plan admission.

    This is intentionally separate from :class:`PlanBinding`.  ``PlanBinding``
    transports an accepted runtime value; this record only proves that exact
    Plan/capability handles denote one resource.  It never unifies names by
    basename, suffix, role prose, or filesystem path.
    """

    binding_id: str
    canonical_resource_id: str
    resource_kind: str
    availability_origin: str
    initially_available: bool
    aliases: Tuple[str, ...]
    binding_source: str
    contract_revision: str = ""
    candidate_scope_id: str = ""
    initial_state: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "aliases": list(self.aliases),
        }


def _resource_binding_id(value: Mapping[str, Any]) -> str:
    body = {
        key: deepcopy(item)
        for key, item in value.items()
        if key != "binding_id"
    }
    return "plan-resource-binding-" + hashlib.sha256(
        _canonical(body).encode("utf-8")
    ).hexdigest()[:20]


def _resource_kind(value: Any, *, field_path: str) -> str:
    kind = str(value or "immutable_artifact").strip().lower()
    if kind not in _RESOURCE_KINDS:
        raise PlanBindingError(
            "PLAN_RESOURCE_KIND_INVALID",
            field_path=field_path,
            received_resource_kind=kind,
            allowed_resource_kinds=sorted(_RESOURCE_KINDS),
        )
    return kind


def _exact_aliases(*groups: Any) -> Tuple[str, ...]:
    aliases = {
        str(item).strip()
        for group in groups
        for item in (
            group
            if isinstance(group, (list, tuple, set, frozenset))
            else [group]
        )
        if str(item or "").strip()
    }
    return tuple(sorted(aliases))


def compile_plan_resource_bindings(
    candidate: Mapping[str, Any],
    *,
    candidate_scope_id: str,
    authoritative_handles: Mapping[str, str],
    availability_origins: Optional[Mapping[str, str]] = None,
    authoritative_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Compile exact Plan handles into one canonical resource registry.

    ``proposed_artifacts[*].resource_binding`` is the additive schema-2 seam
    for a Plan-local handle that explicitly binds a capability resource ID.
    Omitting it preserves the existing candidate-local immutable artifact
    identity.  In particular, ``plan.foo`` and ``artifact.foo`` remain distinct
    unless one explicit binding lists both exact identities.

    The returned mapping is indexed by every accepted exact alias.  Every alias
    for one resource carries the same canonical record.
    """

    origins = {
        str(key): str(value or "")
        for key, value in dict(availability_origins or {}).items()
    }
    authoritative = {
        str(key): dict(value)
        for key, value in dict(authoritative_bindings or {}).items()
        if isinstance(value, Mapping)
    }
    records: list[Dict[str, Any]] = []

    for handle, raw_identity in sorted(authoritative_handles.items()):
        identity = str(raw_identity or "").strip()
        exact_handle = str(handle or "").strip()
        if not exact_handle or not identity:
            raise PlanBindingError(
                "PLAN_RESOURCE_IDENTITY_MISSING",
                field_path="$.authoritative_handles",
                handle=exact_handle,
            )
        source = authoritative.get(exact_handle, {})
        metadata = dict(source.get("metadata") or {})
        origin = str(
            origins.get(identity)
            or source.get("availability_origin")
            or metadata.get("availability_origin")
            or "not_yet_available"
        ).strip().lower()
        initially_available = bool(
            source.get("initially_available") is True
            or metadata.get("initially_available") is True
            or origin in _INITIALLY_AVAILABLE_ORIGINS
        )
        row = {
            "canonical_resource_id": identity,
            "resource_kind": _resource_kind(
                source.get("resource_kind")
                or metadata.get("resource_kind")
                or "immutable_artifact",
                field_path=f"$.authoritative_bindings.{exact_handle}.resource_kind",
            ),
            "availability_origin": origin,
            "initially_available": initially_available,
            "aliases": list(_exact_aliases(
                exact_handle,
                identity,
                source.get("resource_aliases") or metadata.get("resource_aliases") or [],
            )),
            "binding_source": str(
                source.get("binding_source") or "authoritative_handle"
            ),
            "contract_revision": str(
                source.get("contract_revision")
                or metadata.get("contract_revision")
                or ""
            ),
            "candidate_scope_id": "",
            "initial_state": str(
                source.get("initial_state") or metadata.get("initial_state") or ""
            ),
        }
        row["binding_id"] = _resource_binding_id(row)
        records.append(row)

    for index, raw in enumerate(candidate.get("proposed_artifacts", []) or []):
        if not isinstance(raw, Mapping):
            continue
        local_ref = str(raw.get("local_ref") or "").strip()
        if not local_ref:
            continue
        explicit = raw.get("resource_binding")
        if explicit not in (None, {}) and not isinstance(explicit, Mapping):
            raise PlanBindingError(
                "PLAN_RESOURCE_BINDING_INVALID",
                field_path=f"$.proposed_artifacts[{index}].resource_binding",
                expected_type="object",
                received_type=type(explicit).__name__,
            )
        binding = dict(explicit or {})
        contract = dict(raw.get("artifact_contract") or {})
        if binding:
            identity = str(
                binding.get("canonical_resource_id")
                or binding.get("resource_id")
                or ""
            ).strip()
            if not identity:
                raise PlanBindingError(
                    "PLAN_RESOURCE_IDENTITY_MISSING",
                    field_path=(
                        f"$.proposed_artifacts[{index}].resource_binding."
                        "canonical_resource_id"
                    ),
                    handle=local_ref,
                )
            kind = _resource_kind(
                binding.get("resource_kind")
                or contract.get("resource_kind")
                or "",
                field_path=(
                    f"$.proposed_artifacts[{index}].resource_binding.resource_kind"
                ),
            )
            origin = str(
                binding.get("availability_origin") or "plan_local"
            ).strip().lower()
            aliases = _exact_aliases(
                local_ref,
                identity,
                binding.get("aliases") or [],
            )
            binding_source = str(
                binding.get("binding_source") or "planner_explicit_resource_binding"
            )
            contract_revision = str(binding.get("contract_revision") or "")
            initial_state = str(binding.get("initial_state") or "")
            initially_available = bool(
                binding.get("initially_available") is True
                or origin in _INITIALLY_AVAILABLE_ORIGINS
            )
        else:
            # Existing proposed_artifacts retain their candidate-local identity.
            # The scope prefix makes accidental cross-candidate unification
            # impossible while keeping the Planner-facing handle unchanged.
            identity = f"candidate:{candidate_scope_id}:{local_ref}"
            kind = "immutable_artifact"
            origin = "plan_local"
            aliases = _exact_aliases(local_ref, identity)
            binding_source = "candidate_local_artifact"
            contract_revision = ""
            initial_state = ""
            initially_available = False
        row = {
            "canonical_resource_id": identity,
            "resource_kind": kind,
            "availability_origin": origin,
            "initially_available": initially_available,
            "aliases": list(aliases),
            "binding_source": binding_source,
            "contract_revision": contract_revision,
            "candidate_scope_id": str(candidate_scope_id or ""),
            "initial_state": initial_state,
        }
        row["binding_id"] = _resource_binding_id(row)
        records.append(row)

    by_alias: Dict[str, Dict[str, Any]] = {}
    by_identity: Dict[str, Dict[str, Any]] = {}
    for raw in records:
        row = deepcopy(raw)
        identity = str(row["canonical_resource_id"])
        prior_identity = by_identity.get(identity)
        if prior_identity and any(
            prior_identity.get(field) != row.get(field)
            for field in (
                "resource_kind",
                "availability_origin",
                "initially_available",
                "initial_state",
            )
        ):
            raise PlanBindingError(
                "PLAN_RESOURCE_BINDING_CONFLICT",
                canonical_resource_id=identity,
                first_binding=prior_identity,
                second_binding=row,
            )
        if prior_identity:
            merged_aliases = _exact_aliases(
                prior_identity.get("aliases") or [], row.get("aliases") or []
            )
            prior_identity["aliases"] = list(merged_aliases)
            prior_identity["binding_id"] = _resource_binding_id(prior_identity)
            row = prior_identity
        else:
            by_identity[identity] = row
        for alias in row.get("aliases") or []:
            prior_alias = by_alias.get(str(alias))
            if (
                prior_alias
                and str(prior_alias.get("canonical_resource_id") or "") != identity
            ):
                raise PlanBindingError(
                    "PLAN_RESOURCE_ALIAS_CONFLICT",
                    alias=str(alias),
                    first_canonical_resource_id=str(
                        prior_alias.get("canonical_resource_id") or ""
                    ),
                    second_canonical_resource_id=identity,
                )
            by_alias[str(alias)] = row
        # The canonical ID is always an exact alias, never a guessed one.
        by_alias[identity] = row
    return {alias: deepcopy(by_alias[alias]) for alias in sorted(by_alias)}


@dataclass(frozen=True)
class PlanBinding:
    binding_id: str
    name: str
    value: Any
    value_type: str
    producer_plan_id: str
    producer_plan_version: str
    producer_plan_revision: int
    producer_step_id: str
    producer_action_id: str
    producer_execution_attempt_id: str
    status: str = "accepted"
    revision: int = 1
    value_hash: str = ""
    accepted_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlanBinding":
        data = dict(value or {})
        binding = cls(**{
            name: deepcopy(data.get(name))
            for name in cls.__dataclass_fields__
            if name in data
        })
        expected_hash = hashlib.sha256(_canonical(binding.value).encode("utf-8")).hexdigest()
        if binding.value_hash != expected_hash:
            raise PlanBindingError(
                "PLAN_BINDING_VALUE_HASH_MISMATCH",
                binding_id=binding.binding_id,
            )
        return binding


def _candidate_map(action: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = action.get("produced_bindings") or {}
    if not isinstance(raw, Mapping):
        raise PlanBindingError(
            "PLAN_BINDING_CANDIDATE_INVALID",
            field="produced_bindings",
        )
    candidates: Dict[str, Dict[str, Any]] = {}
    for raw_name, raw_value in raw.items():
        name = str(raw_name or "").strip()
        if not _NAME_RE.fullmatch(name) or not isinstance(raw_value, Mapping):
            raise PlanBindingError(
                "PLAN_BINDING_CANDIDATE_INVALID",
                binding_name=name,
            )
        value = dict(raw_value)
        value_type = str(value.get("type") or "").strip().lower()
        if value_type not in _VALUE_TYPES or "value" not in value:
            raise PlanBindingError(
                "PLAN_BINDING_CANDIDATE_INVALID",
                binding_name=name,
                value_type=value_type,
            )
        candidate_value = value["value"]
        shape_valid = {
            "string": isinstance(candidate_value, str),
            "integer": isinstance(candidate_value, int)
            and not isinstance(candidate_value, bool),
            "number": isinstance(candidate_value, (int, float))
            and not isinstance(candidate_value, bool),
            "boolean": isinstance(candidate_value, bool),
            "json": isinstance(candidate_value, (dict, list)),
            "artifact_ref": isinstance(candidate_value, Mapping),
        }[value_type]
        if not shape_valid:
            raise PlanBindingError(
                "PLAN_BINDING_CANDIDATE_VALUE_SHAPE_INVALID",
                binding_name=name,
                value_type=value_type,
                observed_python_type=type(candidate_value).__name__,
            )
        candidates[name] = {
            "name": name,
            "value_type": value_type,
            "value": deepcopy(candidate_value),
        }
    return candidates


def accept_action_binding_candidates(
    state: Dict[str, Any],
    action: Mapping[str, Any],
    execution_record: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> Sequence[PlanBinding]:
    """Promote explicit candidates only after process and output validation."""
    candidates = _candidate_map(action)
    if not candidates:
        return ()
    returncode = execution_record.get("returncode", execution_record.get("exit_code"))
    validation_status = str(validation.get("status") or "").strip().lower()
    validation_passed = bool(validation.get("passed")) or validation_status in {
        "valid", "valid_with_warnings", "passed", "success", "succeeded",
    }
    if returncode != 0 or not validation_passed:
        return ()

    task_state = dict(state.get("task_state") or {})
    history = list(task_state.get("plan_binding_history") or [])
    accepted = dict(task_state.get("accepted_plan_bindings") or {})
    result = []
    for name, candidate in sorted(candidates.items()):
        prior_id = str(accepted.get(name) or "")
        prior = next(
            (
                dict(item) for item in reversed(history)
                if isinstance(item, Mapping)
                and str(item.get("binding_id") or "") == prior_id
            ),
            {},
        )
        candidate_hash = hashlib.sha256(
            _canonical(candidate["value"]).encode("utf-8")
        ).hexdigest()
        if prior and str(prior.get("status") or "") == "accepted":
            if (
                str(prior.get("value_hash") or "") != candidate_hash
                or int(prior.get("producer_plan_revision") or 0)
                != int(state.get("plan_revision") or 0)
            ):
                raise PlanBindingError(
                    "PLAN_BINDING_IMMUTABLE",
                    binding_name=name,
                    accepted_binding_id=prior_id,
                )
            result.append(PlanBinding.from_mapping(prior))
            continue
        revision = 1 + max(
            [
                int(item.get("revision") or 0)
                for item in history
                if isinstance(item, Mapping)
                and str(item.get("name") or "") == name
            ]
            or [0]
        )
        identity = {
            "name": name,
            "value_hash": candidate_hash,
            "value_type": candidate["value_type"],
            "plan_id": str(state.get("plan_id") or ""),
            "plan_version": str(state.get("plan_version") or ""),
            "plan_revision": int(state.get("plan_revision") or 0),
            "step_id": str(execution_record.get("step_id") or state.get("current_step_id") or ""),
            "action_id": str(execution_record.get("action_id") or action.get("action_id") or ""),
            "attempt_id": str(execution_record.get("attempt_id") or execution_record.get("execution_id") or ""),
            "revision": revision,
        }
        binding_id = "plan-binding-" + hashlib.sha256(
            _canonical(identity).encode("utf-8")
        ).hexdigest()[:20]
        binding = PlanBinding(
            binding_id=binding_id,
            name=name,
            value=deepcopy(candidate["value"]),
            value_type=candidate["value_type"],
            producer_plan_id=identity["plan_id"],
            producer_plan_version=identity["plan_version"],
            producer_plan_revision=identity["plan_revision"],
            producer_step_id=identity["step_id"],
            producer_action_id=identity["action_id"],
            producer_execution_attempt_id=identity["attempt_id"],
            revision=revision,
            value_hash=candidate_hash,
            accepted_at=_now(),
        )
        history.append(binding.to_dict())
        accepted[name] = binding_id
        result.append(binding)
    task_state["plan_binding_history"] = history
    task_state["accepted_plan_bindings"] = accepted
    state["task_state"] = task_state
    return tuple(result)


def accepted_plan_binding(
    state: Mapping[str, Any], name: str
) -> Optional[PlanBinding]:
    task_state = dict(state.get("task_state") or {})
    binding_id = str((task_state.get("accepted_plan_bindings") or {}).get(name) or "")
    for raw in reversed(list(task_state.get("plan_binding_history") or [])):
        if not isinstance(raw, Mapping) or str(raw.get("binding_id") or "") != binding_id:
            continue
        binding = PlanBinding.from_mapping(raw)
        if binding.status != "accepted":
            return None
        if (
            binding.producer_plan_id != str(state.get("plan_id") or "")
            or binding.producer_plan_version != str(state.get("plan_version") or "")
            or binding.producer_plan_revision != int(state.get("plan_revision") or 0)
        ):
            return None
        return binding
    return None


def resolve_action_binding_environment(
    state: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    binding_rule: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve declared refs to exact values without examining executable text."""
    consumed = action.get("consumed_bindings") or {}
    binding_env = action.get("binding_env") or {}
    if not isinstance(consumed, Mapping) or not isinstance(binding_env, Mapping):
        raise PlanBindingError("PLAN_BINDING_DECLARATION_INVALID")
    resolved: Dict[str, PlanBinding] = {}
    expected_types = dict((binding_rule or {}).get("binding_types") or {})
    for raw_name, raw_ref in consumed.items():
        name = str(raw_name or "")
        ref = str((raw_ref or {}).get("ref") or "") if isinstance(raw_ref, Mapping) else ""
        if ref != f"plan_binding:{name}":
            raise PlanBindingError(
                "BINDING_REFERENCE_MISMATCH",
                binding_name=name,
                declared_ref=ref,
                expected_ref=f"plan_binding:{name}",
            )
        binding = accepted_plan_binding(state, name)
        if binding is None:
            raise PlanBindingError(
                "PLAN_BINDING_UNAVAILABLE",
                binding_name=name,
            )
        expected_type = str(expected_types.get(name) or "")
        if expected_type and binding.value_type != expected_type:
            raise PlanBindingError(
                "PLAN_BINDING_VALUE_TYPE_MISMATCH",
                binding_name=name,
                expected_type=expected_type,
                observed_type=binding.value_type,
                binding_id=binding.binding_id,
            )
        resolved[name] = binding
    candidates = _candidate_map(action)
    environment: Dict[str, str] = {}
    records = []
    for raw_env, raw_spec in binding_env.items():
        env_name = str(raw_env or "")
        if not _ENV_RE.fullmatch(env_name) or not isinstance(raw_spec, Mapping):
            raise PlanBindingError(
                "PLAN_BINDING_ENV_INVALID",
                env_name=env_name,
            )
        source = str(raw_spec.get("source") or "")
        name = str(raw_spec.get("name") or "")
        if source == "accepted_binding":
            binding = resolved.get(name)
            if binding is None:
                raise PlanBindingError(
                    "BINDING_REFERENCE_MISMATCH",
                    binding_name=name,
                    env_name=env_name,
                )
            value = binding.value
            binding_id = binding.binding_id
        elif source == "produced_binding":
            if name not in candidates:
                raise PlanBindingError(
                    "BINDING_REFERENCE_MISMATCH",
                    binding_name=name,
                    env_name=env_name,
                )
            value = candidates[name]["value"]
            binding_id = "candidate:" + name
        else:
            raise PlanBindingError(
                "PLAN_BINDING_ENV_SOURCE_INVALID",
                env_name=env_name,
                source=source,
            )
        bound_value = deepcopy(value)
        if (
            source == "accepted_binding"
            and binding is not None
            and binding.value_type == "artifact_ref"
        ) or (
            source == "produced_binding"
            and candidates[name]["value_type"] == "artifact_ref"
        ):
            from agent_core.workspace_attestation import (
                resolve_artifact_backed_binding_value,
            )

            value = resolve_artifact_backed_binding_value(
                value,
                value_type="artifact_ref",
                workspace_bindings=list(
                    action.get("formal_workspace_bindings") or []
                ),
            )
        if isinstance(value, (dict, list)):
            env_value = _canonical(value)
        elif isinstance(value, bool):
            env_value = "true" if value else "false"
        else:
            env_value = str(value)
        if env_name in environment and environment[env_name] != env_value:
            raise PlanBindingError("PLAN_BINDING_ENV_COLLISION", env_name=env_name)
        environment[env_name] = env_value
        records.append({
            "env_name": env_name,
            "binding_name": name,
            "binding_id": binding_id,
            "value_type": (
                binding.value_type
                if source == "accepted_binding" and binding is not None
                else candidates[name]["value_type"]
            ),
            "value_hash": hashlib.sha256(
                _canonical(bound_value).encode("utf-8")
            ).hexdigest(),
        })
    return {"environment": environment, "records": records}


def _validate_binding_value_contract(
    action: Mapping[str, Any], declared_rule: Mapping[str, Any]
) -> list[Dict[str, Any]]:
    """Validate candidate value types and artifact-backed identities.

    This is part of full Generate admission.  It is intentionally driven by
    the applicable binding sub-contract, never by whether the candidate chose
    to include ``binding_env``.
    """

    violations: list[Dict[str, Any]] = []
    try:
        candidates = _candidate_map(action)
    except PlanBindingError as exc:
        return [{
            "error_code": exc.code,
            **deepcopy(exc.diagnostic),
            "disposition": "REPAIR_REQUIRED",
        }]
    raw_types = declared_rule.get("binding_types") or {}
    raw_artifact_refs = declared_rule.get("artifact_binding_requirements") or {}
    if not isinstance(raw_types, Mapping) or not isinstance(raw_artifact_refs, Mapping):
        return [{
            "error_code": "BINDING_SUBCONTRACT_VALUE_SCHEMA_INVALID",
            "disposition": "PROTOCOL_INVALID",
        }]
    expected_names = {
        str(item)
        for item in [
            *(declared_rule.get("produces_bindings") or []),
            *(declared_rule.get("consumes_bindings") or []),
        ]
        if str(item)
    }
    unknown_type_names = sorted(str(name) for name in raw_types if str(name) not in expected_names)
    if unknown_type_names:
        return [{
            "error_code": "BINDING_SUBCONTRACT_VALUE_SCHEMA_INVALID",
            "unknown_binding_type_names": unknown_type_names,
            "disposition": "PROTOCOL_INVALID",
        }]
    for name in sorted(candidates):
        expected_type = str(raw_types.get(name) or "").strip()
        if expected_type and expected_type not in _VALUE_TYPES:
            violations.append({
                "error_code": "BINDING_SUBCONTRACT_VALUE_SCHEMA_INVALID",
                "binding_name": name,
                "expected_type": expected_type,
                "disposition": "PROTOCOL_INVALID",
            })
            continue
        actual_type = str(candidates[name].get("value_type") or "")
        if expected_type and actual_type != expected_type:
            violations.append({
                "error_code": "BINDING_VALUE_TYPE_MISMATCH",
                "binding_name": name,
                "expected_type": expected_type,
                "observed_type": actual_type,
                "disposition": "REPAIR_REQUIRED",
            })
            continue
        requirement = raw_artifact_refs.get(name)
        if expected_type == "artifact_ref" or requirement is not None:
            if not isinstance(requirement, Mapping):
                violations.append({
                    "error_code": "BINDING_SUBCONTRACT_VALUE_SCHEMA_INVALID",
                    "binding_name": name,
                    "disposition": "PROTOCOL_INVALID",
                })
                continue
            value = candidates[name].get("value")
            if not isinstance(value, Mapping):
                # The generic shape check above normally catches this first.
                continue
            expected_resource = str(requirement.get("canonical_resource_id") or "")
            expected_kind = str(requirement.get("resource_kind") or "")
            observed_resource = str(value.get("canonical_resource_id") or "")
            observed_kind = str(value.get("resource_kind") or "")
            revision = value.get("binding_revision")
            if (
                not expected_resource
                or expected_kind != "mutable_workspace"
                or observed_resource != expected_resource
                or observed_kind != expected_kind
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 1
            ):
                violations.append({
                    "error_code": "ARTIFACT_BACKED_BINDING_REFERENCE_INVALID",
                    "binding_name": name,
                    "expected": {
                        "canonical_resource_id": expected_resource,
                        "resource_kind": expected_kind,
                        "binding_revision": "positive integer",
                    },
                    "observed": deepcopy(value),
                    "disposition": "REPAIR_REQUIRED",
                })
    return violations


def _validate_required_binding_environment(
    action: Mapping[str, Any], declared_rule: Mapping[str, Any]
) -> list[Dict[str, Any]]:
    raw_required = declared_rule.get("required_binding_env") or {}
    if not isinstance(raw_required, Mapping):
        return [{
            "error_code": "BINDING_SUBCONTRACT_ENV_SCHEMA_INVALID",
            "disposition": "PROTOCOL_INVALID",
        }]
    actual = action.get("binding_env") or {}
    if not isinstance(actual, Mapping):
        return [{
            "error_code": "PLAN_BINDING_DECLARATION_INVALID",
            "field": "binding_env",
            "disposition": "REPAIR_REQUIRED",
        }]
    violations: list[Dict[str, Any]] = []
    for raw_env, raw_spec in raw_required.items():
        env_name = str(raw_env or "")
        if not _ENV_RE.fullmatch(env_name) or not isinstance(raw_spec, Mapping):
            violations.append({
                "error_code": "BINDING_SUBCONTRACT_ENV_SCHEMA_INVALID",
                "env_name": env_name,
                "disposition": "PROTOCOL_INVALID",
            })
            continue
        expected = {
            "source": str(raw_spec.get("source") or ""),
            "name": str(raw_spec.get("name") or ""),
        }
        observed_raw = actual.get(env_name)
        observed = (
            {
                "source": str(observed_raw.get("source") or ""),
                "name": str(observed_raw.get("name") or ""),
            }
            if isinstance(observed_raw, Mapping)
            else None
        )
        if observed != expected:
            violations.append({
                "error_code": "BINDING_ENV_REQUIRED"
                if observed_raw is None
                else "BINDING_ENV_CONTRACT_MISMATCH",
                "env_name": env_name,
                "expected": expected,
                "observed": deepcopy(observed_raw),
                "disposition": "REPAIR_REQUIRED",
            })
    return violations


def invalidate_producer_bindings(
    state: Dict[str, Any], *, producer_step_id: str, reason: str
) -> int:
    """Mark producer history stale and clear active refs; never mutate values."""
    task_state = dict(state.get("task_state") or {})
    history = list(task_state.get("plan_binding_history") or [])
    accepted = dict(task_state.get("accepted_plan_bindings") or {})
    changed = 0
    for index, raw in enumerate(history):
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if (
            str(item.get("producer_step_id") or "") == str(producer_step_id or "")
            and str(item.get("status") or "") == "accepted"
        ):
            item.update({"status": "stale", "stale_reason": str(reason), "stale_at": _now()})
            history[index] = item
            if str(accepted.get(str(item.get("name") or "")) or "") == str(item.get("binding_id") or ""):
                accepted.pop(str(item.get("name") or ""), None)
            changed += 1
    task_state["plan_binding_history"] = history
    task_state["accepted_plan_bindings"] = accepted
    state["task_state"] = task_state
    return changed


def project_accepted_plan_bindings(state: Mapping[str, Any]) -> Sequence[Dict[str, Any]]:
    task_state = dict(state.get("task_state") or {})
    return tuple(
        binding.to_dict()
        for name in sorted((task_state.get("accepted_plan_bindings") or {}).keys())
        for binding in [accepted_plan_binding(state, str(name))]
        if binding is not None
    )


def validate_declared_binding_operation(
    action: Mapping[str, Any], contract: Mapping[str, Any]
) -> Sequence[Dict[str, Any]]:
    """Validate one *applicable* entry in a partial binding sub-contract.

    Operation existence is deliberately outside this helper.  Absence from the
    binding table means that this local validator has no rule to apply; callers
    must first authorize the operation against its canonical capability.
    """
    operation = str(action.get("operation") or "")
    operations = dict(contract.get("operations") or {})
    if not operation or operation not in operations:
        return ()
    declared = dict(operations[operation] or {})
    expected_consumes = {
        str(item) for item in declared.get("consumes_bindings") or [] if str(item)
    }
    expected_produces = {
        str(item) for item in declared.get("produces_bindings") or [] if str(item)
    }
    actual_consumes = {
        str(item) for item in dict(action.get("consumed_bindings") or {})
    }
    actual_produces = {
        str(item) for item in dict(action.get("produced_bindings") or {})
    }
    violations = []
    for name in sorted(expected_consumes - actual_consumes):
        violations.append({
            "error_code": "BINDING_REFERENCE_MISMATCH",
            "operation": operation,
            "binding_name": name,
            "expected_ref": f"plan_binding:{name}",
            "disposition": "REPAIR_REQUIRED",
        })
    for name in sorted(expected_produces - actual_produces):
        violations.append({
            "error_code": "BINDING_PRODUCTION_MISSING",
            "operation": operation,
            "binding_name": name,
            "disposition": "REPAIR_REQUIRED",
        })
    for name in sorted(actual_consumes - expected_consumes):
        violations.append({
            "error_code": "BINDING_REFERENCE_UNDECLARED",
            "operation": operation,
            "binding_name": name,
            "disposition": "REPAIR_REQUIRED",
        })
    for name in sorted(actual_produces - expected_produces):
        violations.append({
            "error_code": "BINDING_PRODUCTION_UNDECLARED",
            "operation": operation,
            "binding_name": name,
            "disposition": "REPAIR_REQUIRED",
        })
    consumed = dict(action.get("consumed_bindings") or {})
    for name in sorted(expected_consumes & actual_consumes):
        raw_ref = consumed.get(name)
        declared_ref = (
            str(raw_ref.get("ref") or "")
            if isinstance(raw_ref, Mapping)
            else ""
        )
        expected_ref = f"plan_binding:{name}"
        if declared_ref != expected_ref:
            violations.append({
                "error_code": "BINDING_REFERENCE_MISMATCH",
                "operation": operation,
                "binding_name": name,
                "declared_ref": declared_ref,
                "expected_ref": expected_ref,
                "disposition": "REPAIR_REQUIRED",
            })
    return tuple(violations)


def validate_operation_binding_admission(
    action: Mapping[str, Any],
    *,
    capability_contract: Mapping[str, Any],
    capability_registry: Mapping[str, Mapping[str, Any]],
    binding_contract: Mapping[str, Any],
) -> OperationBindingAdmission:
    """Authorize an operation, then apply its optional binding sub-contract.

    The execution-capability registry is the closed operation authority.  The
    binding table is a partial mapping over that authority and can never grant
    a new operation merely by naming it.
    """

    capability = deepcopy(dict(capability_contract or {}))
    capability_id = str(capability.get("capability_id") or "").strip()
    selected_skill_id = str(capability.get("selected_skill_id") or "").strip()
    expected_operation = str(capability.get("operation") or "").strip()
    actual_operation = str(action.get("operation") or "").strip()
    declared_capability_id = str(
        action.get("execution_capability_id") or ""
    ).strip()
    formal_source = (
        f"generate.execution_capabilities[{capability_id}]"
        if capability_id
        else "generate.execution_capabilities"
    )

    formal_rows = {
        str(raw.get("capability_id") or registry_id): deepcopy(dict(raw))
        for registry_id, raw in dict(capability_registry or {}).items()
        if isinstance(raw, Mapping)
        and (
            not selected_skill_id
            or str(raw.get("selected_skill_id") or "") == selected_skill_id
        )
    }
    canonical_capability = formal_rows.get(capability_id)
    if not capability_id or canonical_capability is None:
        issue = {
            "code": "EXECUTION_CAPABILITY_AUTHORITY_MISSING",
            "owner": "INTERNAL_PROTOCOL",
            "message": "The selected capability is absent from the canonical Skill registry.",
            "capability_id": capability_id,
            "contract_source": formal_source,
        }
        return OperationBindingAdmission(
            "contract_error", actual_operation, capability_id, False,
            "execution_capability_registry", formal_source, "1", (issue,),
        )
    if canonical_capability != capability:
        issue = {
            "code": "EXECUTION_CAPABILITY_CONTRACT_VERSION_CONFLICT",
            "owner": "INTERNAL_PROTOCOL",
            "message": "Generate admission did not receive the canonical selected capability contract.",
            "capability_id": capability_id,
            "contract_source": formal_source,
        }
        return OperationBindingAdmission(
            "contract_error", actual_operation, capability_id, False,
            "execution_capability_registry", formal_source, "1", (issue,),
        )
    if declared_capability_id and declared_capability_id != capability_id:
        issue = {
            "code": "ACTION_EXECUTION_CAPABILITY_SCOPE_MISMATCH",
            "owner": "GENERATED_ACTION",
            "message": "The action names a capability other than the approved current-step capability.",
            "field_path": "execution_capability_id",
            "expected_capability_id": capability_id,
            "received_capability_id": declared_capability_id,
            "contract_source": formal_source,
        }
        return OperationBindingAdmission(
            "candidate_rejected", actual_operation, capability_id, False,
            "execution_capability_registry", formal_source, "1", (issue,),
        )
    if not expected_operation:
        if actual_operation or any(
            action.get(field) not in (None, {}, [])
            for field in ("produced_bindings", "consumed_bindings", "binding_env")
        ):
            issue = {
                "code": "ACTION_OPERATION_UNAUTHORIZED",
                "owner": "GENERATED_ACTION",
                "message": "The selected capability does not authorize an operation identity.",
                "operation": actual_operation,
                "capability_id": capability_id,
                "field_path": "operation",
                "contract_source": formal_source,
            }
            return OperationBindingAdmission(
                "candidate_rejected", actual_operation, capability_id, False,
                "execution_capability_registry", formal_source, "1", (issue,),
            )
        return OperationBindingAdmission(
            "not_applicable", "", capability_id, False,
            "execution_capability_registry", formal_source, "1", (),
        )
    if not actual_operation:
        issue = {
            "code": "ACTION_OPERATION_MISSING",
            "owner": "GENERATED_ACTION",
            "message": "The action omitted the operation required by its selected capability.",
            "expected_operation": expected_operation,
            "capability_id": capability_id,
            "field_path": "operation",
            "contract_source": formal_source,
        }
        return OperationBindingAdmission(
            "candidate_rejected", "", capability_id, False,
            "execution_capability_registry", formal_source, "1", (issue,),
        )
    if actual_operation != expected_operation:
        issue = {
            "code": "ACTION_OPERATION_SCOPE_MISMATCH",
            "owner": "GENERATED_ACTION",
            "message": "The action operation is not authorized by the selected approved capability.",
            "operation": actual_operation,
            "expected_operation": expected_operation,
            "capability_id": capability_id,
            "field_path": "operation",
            "contract_source": formal_source,
        }
        return OperationBindingAdmission(
            "candidate_rejected", actual_operation, capability_id, False,
            "execution_capability_registry", formal_source, "1", (issue,),
        )

    applicability = (
        deepcopy(dict(capability.get("binding_applicability") or {}))
        if isinstance(capability.get("binding_applicability"), Mapping)
        else {}
    )
    capability_requires_binding = bool(
        applicability.get("applicable") is True
        or capability.get("produces_bindings")
    )
    binding = deepcopy(dict(binding_contract or {}))
    binding_source = "generate.binding_contract"
    if not binding:
        has_candidate_binding_facts = any(
            action.get(field) not in (None, {}, [])
            for field in ("produced_bindings", "consumed_bindings", "binding_env")
        )
        if capability_requires_binding:
            issue = {
                "code": "BINDING_SUBCONTRACT_MISSING",
                "owner": "INTERNAL_PROTOCOL",
                "message": "Explicit binding semantics are present but the Skill binding sub-contract is missing.",
                "operation": actual_operation,
                "capability_id": capability_id,
                "contract_source": binding_source,
            }
            return OperationBindingAdmission(
                "contract_error", actual_operation, capability_id, False,
                "execution_capability_registry", binding_source, "missing", (issue,),
            )
        if has_candidate_binding_facts:
            issue = {
                "code": "ACTION_BINDING_UNAUTHORIZED",
                "owner": "GENERATED_ACTION",
                "message": "The action declares binding fields without a binding sub-contract for its formal capability.",
                "operation": actual_operation,
                "capability_id": capability_id,
                "field_path": "consumed_bindings|produced_bindings|binding_env",
                "contract_source": formal_source,
            }
            return OperationBindingAdmission(
                "candidate_rejected", actual_operation, capability_id, False,
                "execution_capability_registry", formal_source, "1", (issue,),
            )
        return OperationBindingAdmission(
            "not_applicable", actual_operation, capability_id, False,
            "execution_capability_registry", binding_source, "absent", (),
        )

    try:
        binding_schema = int(binding.get("schema_version") or 0)
    except (TypeError, ValueError):
        binding_schema = 0
    if binding_schema != 1 or not isinstance(binding.get("operations"), Mapping):
        issue = {
            "code": "BINDING_SUBCONTRACT_VERSION_UNSUPPORTED",
            "owner": "INTERNAL_PROTOCOL",
            "message": "The Skill binding sub-contract schema is missing, malformed, or unsupported.",
            "operation": actual_operation,
            "capability_id": capability_id,
            "observed_schema_version": binding.get("schema_version"),
            "contract_source": binding_source,
        }
        return OperationBindingAdmission(
            "contract_error", actual_operation, capability_id, False,
            "execution_capability_registry", binding_source,
            str(binding.get("schema_version") or "missing"), (issue,),
        )

    formal_operation_to_capabilities: Dict[str, list[str]] = {}
    for formal_id, raw in formal_rows.items():
        operation = str(raw.get("operation") or "").strip()
        if operation:
            formal_operation_to_capabilities.setdefault(operation, []).append(formal_id)
    unknown_binding_operations = sorted(
        str(item) for item in dict(binding.get("operations") or {})
        if str(item) not in formal_operation_to_capabilities
    )
    if unknown_binding_operations:
        issue = {
            "code": "BINDING_SUBCONTRACT_OPERATION_UNKNOWN",
            "owner": "INTERNAL_PROTOCOL",
            "message": "The binding sub-contract names an operation absent from formal capability authority.",
            "unknown_operations": unknown_binding_operations,
            "selected_skill_id": selected_skill_id,
            "contract_source": binding_source,
        }
        return OperationBindingAdmission(
            "contract_error", actual_operation, capability_id, False,
            "execution_capability_registry", binding_source, str(binding_schema), (issue,),
        )

    operations = dict(binding.get("operations") or {})
    if actual_operation not in operations:
        has_binding_facts = any(
            action.get(field) not in (None, {}, [])
            for field in ("produced_bindings", "consumed_bindings", "binding_env")
        )
        if capability_requires_binding:
            issue = {
                "code": "BINDING_SUBCONTRACT_OPERATION_MISSING",
                "owner": "INTERNAL_PROTOCOL",
                "message": "The formal capability exposes binding semantics but has no applicable binding rule.",
                "operation": actual_operation,
                "capability_id": capability_id,
                "contract_source": binding_source,
            }
            return OperationBindingAdmission(
                "contract_error", actual_operation, capability_id, False,
                "execution_capability_registry", binding_source, str(binding_schema), (issue,),
            )
        if has_binding_facts:
            issue = {
                "code": "ACTION_BINDING_NOT_APPLICABLE",
                "owner": "GENERATED_ACTION",
                "message": "The formal operation has no applicable binding rule, but the candidate declares binding fields.",
                "operation": actual_operation,
                "capability_id": capability_id,
                "field_path": "consumed_bindings|produced_bindings|binding_env",
                "contract_source": binding_source,
            }
            return OperationBindingAdmission(
                "candidate_rejected", actual_operation, capability_id, False,
                "execution_capability_registry", binding_source, str(binding_schema), (issue,),
            )
        return OperationBindingAdmission(
            "not_applicable", actual_operation, capability_id, False,
            "execution_capability_registry", binding_source, str(binding_schema), (),
        )
    if not isinstance(operations.get(actual_operation), Mapping):
        issue = {
            "code": "BINDING_SUBCONTRACT_OPERATION_INVALID",
            "owner": "INTERNAL_PROTOCOL",
            "message": "The applicable binding operation declaration is not an object.",
            "operation": actual_operation,
            "capability_id": capability_id,
            "contract_source": binding_source,
        }
        return OperationBindingAdmission(
            "contract_error", actual_operation, capability_id, True,
            "execution_capability_registry", binding_source, str(binding_schema), (issue,),
        )

    declared_rule = dict(operations.get(actual_operation) or {})
    malformed_binding_fields = []
    for field in ("consumes_bindings", "produces_bindings"):
        raw_names = declared_rule.get(field)
        if (
            not isinstance(raw_names, list)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in raw_names
            )
            or len({item.strip() for item in raw_names}) != len(raw_names)
        ):
            malformed_binding_fields.append(field)
    if malformed_binding_fields:
        issue = {
            "code": "BINDING_SUBCONTRACT_OPERATION_SCHEMA_INVALID",
            "owner": "INTERNAL_PROTOCOL",
            "message": (
                "The applicable binding operation must declare unique, "
                "non-empty string arrays for consumes_bindings and "
                "produces_bindings."
            ),
            "operation": actual_operation,
            "capability_id": capability_id,
            "invalid_fields": malformed_binding_fields,
            "contract_source": binding_source,
        }
        return OperationBindingAdmission(
            "contract_error", actual_operation, capability_id, True,
            "execution_capability_registry", binding_source,
            str(binding_schema), (issue,),
        )

    if applicability:
        try:
            descriptor_schema = int(applicability.get("schema_version") or 0)
            descriptor_contract_schema = int(
                applicability.get("binding_contract_schema_version") or 0
            )
        except (TypeError, ValueError):
            descriptor_schema = 0
            descriptor_contract_schema = 0
        expected_consumes = sorted({
            str(item)
            for item in applicability.get("consumes_bindings") or []
            if str(item)
        })
        expected_produces = sorted({
            str(item)
            for item in applicability.get("produces_bindings") or []
            if str(item)
        })
        observed_consumes = sorted({
            str(item)
            for item in declared_rule.get("consumes_bindings") or []
            if str(item)
        })
        observed_produces = sorted({
            str(item)
            for item in declared_rule.get("produces_bindings") or []
            if str(item)
        })
        if (
            descriptor_schema != 1
            or applicability.get("applicable") is not True
            or descriptor_contract_schema != binding_schema
            or expected_consumes != observed_consumes
            or expected_produces != observed_produces
        ):
            issue = {
                "code": "BINDING_SUBCONTRACT_APPLICABILITY_CONFLICT",
                "owner": "INTERNAL_PROTOCOL",
                "message": (
                    "The capability-owned binding applicability descriptor "
                    "conflicts with the current binding sub-contract."
                ),
                "operation": actual_operation,
                "capability_id": capability_id,
                "contract_source": binding_source,
                "expected": {
                    "schema_version": descriptor_contract_schema,
                    "consumes_bindings": expected_consumes,
                    "produces_bindings": expected_produces,
                },
                "observed": {
                    "schema_version": binding_schema,
                    "consumes_bindings": observed_consumes,
                    "produces_bindings": observed_produces,
                },
            }
            return OperationBindingAdmission(
                "contract_error", actual_operation, capability_id, True,
                "execution_capability_registry", binding_source,
                str(binding_schema), (issue,),
            )

    raw_binding_violations = [
        *validate_declared_binding_operation(action, binding),
        *_validate_binding_value_contract(action, declared_rule),
        *_validate_required_binding_environment(action, declared_rule),
    ]
    binding_violations = tuple(
        {
            "code": str(item.get("error_code") or "BINDING_CONTRACT_MISMATCH"),
            "owner": (
                "INTERNAL_PROTOCOL"
                if str(item.get("disposition") or "") == "PROTOCOL_INVALID"
                else "GENERATED_ACTION"
            ),
            "message": (
                "The applicable Skill binding sub-contract is internally invalid."
                if str(item.get("disposition") or "") == "PROTOCOL_INVALID"
                else "Generated action does not satisfy the applicable Skill binding rule."
            ),
            "contract_source": binding_source,
            **deepcopy(dict(item)),
        }
        for item in raw_binding_violations
    )
    return OperationBindingAdmission(
        "contract_error"
        if any(item.get("owner") == "INTERNAL_PROTOCOL" for item in binding_violations)
        else "candidate_rejected" if binding_violations else "passed",
        actual_operation,
        capability_id,
        True,
        "execution_capability_registry",
        binding_source,
        str(binding_schema),
        binding_violations,
    )


PUBLIC_BINDING_PROTOCOL = "public_handle_v2"


def public_binding_requirements(frozen, *, operation, workspace_bindings):
    """Provider description of the one frozen input contract; no authority."""
    contract = dict(frozen.get("binding_contract") or {})
    rule = dict((contract.get("operations") or {}).get(operation) or {})
    types = dict(rule.get("binding_types") or {})
    artifacts = dict(rule.get("artifact_binding_requirements") or {})
    return {"protocol": str(frozen.get("protocol") or "legacy_full_v1"),
        "operation": operation,
        "produced_bindings": {name: {"type": types.get(name),
            "value": {"handle": "one exact allowed handle"} if types.get(name) == "artifact_ref" else "model semantic value",
            "allowed_handles": [row["plan_handle"] for row in workspace_bindings
                if row.get("canonical_resource_id") == dict(artifacts.get(name) or {}).get("canonical_resource_id")
                and row.get("resource_kind") == dict(artifacts.get(name) or {}).get("resource_kind")]}
            for name in rule.get("produces_bindings", [])},
        "consumed_bindings": {name: {"ref": "plan_binding:" + name} for name in rule.get("consumes_bindings", [])},
        "binding_env": {"owner": "system", "declared_mapping": deepcopy(rule.get("required_binding_env") or {}),
            "compatibility": "If supplied, every mapping must equal this declaration."},
        "legacy_full_v1": "Complete formal objects remain strict; identity/kind/revision and binding_env must already be correct. Do not mix handle and formal values."}


def normalize_public_action_bindings(action, *, frozen, scope, workspace_bindings, raw_public_candidate=None):
    """Route-free boundary called only with approved invocation/catalog facts.

    This does not admit the action. The original operation, workspace, value,
    env, approval and prelaunch validators must still run on the result.
    """
    raw = deepcopy(dict(action))
    if not frozen:
        if raw.get("binding_input_protocol") not in (None, "legacy_full_v1"):
            raise PlanBindingError("BINDING_INPUT_PROTOCOL_UNSUPPORTED", field_path="binding_input_protocol")
        return raw, {}
    if frozen.get("protocol") != PUBLIC_BINDING_PROTOCOL:
        raise PlanBindingError("BINDING_INPUT_PROTOCOL_UNSUPPORTED", field_path="binding_input_protocol")
    public = raw_public_candidate if isinstance(raw_public_candidate, Mapping) else raw
    # Public output cannot replace the fixed invocation authority, including
    # fields the action parser deliberately omits from the executable envelope.
    expected_scope = dict(scope)
    expected_scope["execution_capability_id"] = frozen.get("capability_id")
    for key in ("task_id", "task_revision", "plan_id", "plan_version", "plan_revision", "step_id", "execution_capability_id"):
        if key in public and public[key] != expected_scope.get(key):
            raise PlanBindingError("BINDING_PUBLIC_SCOPE_MISMATCH", field_path=key)
    declared = raw.get("binding_input_protocol", PUBLIC_BINDING_PROTOCOL)
    if declared not in (PUBLIC_BINDING_PROTOCOL, "legacy_full_v1"):
        raise PlanBindingError("BINDING_INPUT_PROTOCOL_UNSUPPORTED", field_path="binding_input_protocol")
    contract = dict(frozen.get("binding_contract") or {})
    # Use the approved capability's operation, never a model-selected rule.
    operation = str(frozen.get("operation") or "")
    rule = dict((contract.get("operations") or {}).get(operation) or {})
    values = raw.get("produced_bindings") or {}
    modes = set()
    if isinstance(values, Mapping):
        for value in values.values():
            if isinstance(value, Mapping) and value.get("type") == "artifact_ref":
                ref = value.get("value")
                if isinstance(ref, Mapping):
                    modes.add("handle" if "handle" in ref else "formal")
    if len(modes) > 1 or (declared == "legacy_full_v1" and "handle" in modes):
        raise PlanBindingError("BINDING_INPUT_PROTOCOL_MIXED", field_path="produced_bindings")
    legacy = declared == "legacy_full_v1" or modes == {"formal"}
    normalized = deepcopy(raw)
    if not legacy:
        for name, candidate in (values.items() if isinstance(values, Mapping) else []):
            if not isinstance(candidate, Mapping) or candidate.get("type") != "artifact_ref":
                continue  # Original validator owns shape/type violations.
            ref = candidate.get("value")
            if not isinstance(ref, Mapping) or "handle" not in ref:
                continue
            if set(ref) != {"handle"} or not isinstance(ref["handle"], str):
                raise PlanBindingError("BINDING_INPUT_PROTOCOL_MIXED", field_path="produced_bindings."+name+".value")
            requirement = dict((rule.get("artifact_binding_requirements") or {}).get(name) or {})
            matches = [row for row in workspace_bindings if row.get("plan_handle") == ref["handle"]
                and row.get("canonical_resource_id") == requirement.get("canonical_resource_id")
                and row.get("resource_kind") == requirement.get("resource_kind")
                and str((row.get("operation_scope") or {}).get("operation") or "") == operation]
            if len(matches) != 1:
                raise PlanBindingError("BINDING_HANDLE_UNKNOWN" if not matches else "BINDING_HANDLE_AMBIGUOUS",
                    field_path="produced_bindings."+name+".value.handle", binding_name=name, handle=ref["handle"])
            row = matches[0]
            normalized["produced_bindings"][name]["value"] = {key: deepcopy(row[key]) for key in
                ("canonical_resource_id", "resource_kind", "binding_revision", "plan_handle", "binding_id")}
        supplied = raw.get("binding_env") or {}
        required = dict(rule.get("required_binding_env") or {})
        if not isinstance(supplied, Mapping) or any(key not in required or value != required[key] for key,value in supplied.items()):
            raise PlanBindingError("BINDING_ENV_CONTRACT_MISMATCH", field_path="binding_env")
        normalized["binding_env"] = deepcopy(required)
    digest = lambda value: hashlib.sha256(_canonical(value).encode()).hexdigest()
    receipt = {"schema_version": 1, "producer": "managed_binding_normalizer", "protocol": "legacy_full_v1" if legacy else PUBLIC_BINDING_PROTOCOL,
        "scope": deepcopy(dict(scope)), "contract_digest": digest(frozen),
        "catalog_digests": [row["binding_digest"] for row in workspace_bindings],
        "raw_digest": digest(raw), "normalized_digest": digest(normalized),
        "raw_candidate": raw, "normalized_candidate": deepcopy(normalized)}
    if raw_public_candidate is not None:
        # Preserve the public JSON object separately from the parser's action
        # envelope. Existing v1 receipts retain their exact representation.
        receipt.update(schema_version=2, raw_public_candidate=deepcopy(raw_public_candidate),
            raw_public_digest=digest(raw_public_candidate), raw_representation="parsed_action_before_binding")
    receipt["receipt_id"] = "binding-normalization-" + digest(receipt)
    return normalized, receipt


def binding_input_progress_facts(action, requirements):
    """Facts actually checkable at the public boundary, not text differences."""
    if not isinstance(action, Mapping):
        return set()
    facts = set()
    if action.get('operation') == requirements.get('operation'):
        facts.add('operation')
    produced = action.get('produced_bindings') or {}
    if isinstance(produced, Mapping):
        for name, expected in requirements.get('produced_bindings', {}).items():
            row = produced.get(name)
            if not isinstance(row, Mapping) or row.get('type') != expected.get('type'):
                continue
            value = row.get('value')
            kind = expected.get('type')
            valid = (kind == 'string' and isinstance(value, str) and bool(value.strip())
                or kind == 'integer' and type(value) is int
                or kind == 'number' and type(value) in (int, float)
                or kind == 'boolean' and type(value) is bool
                or kind == 'artifact_ref' and isinstance(value, Mapping) and set(value) == {'handle'}
                and value.get('handle') in expected.get('allowed_handles', []))
            if valid:
                facts.add('produced_bindings.' + name)
    consumed = action.get('consumed_bindings') or {}
    if isinstance(consumed, Mapping):
        for name, expected in requirements.get('consumed_bindings', {}).items():
            if consumed.get(name) == expected:
                facts.add('consumed_bindings.' + name)
    supplied = action.get('binding_env') or {}
    expected = requirements.get('binding_env', {}).get('declared_mapping', {})
    if isinstance(supplied, Mapping) and all(k in expected and v == expected[k] for k,v in supplied.items()):
        facts.add('binding_env')
    return facts

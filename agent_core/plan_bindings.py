"""Opaque, plan-scoped value transport between successful actions.

The Controller owns identity, persistence, scope and exact copying.  It never
derives a value from executable source and never interprets domain meaning.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,126}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_VALUE_TYPES = {"string", "integer", "number", "boolean", "json", "artifact_ref"}


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
        candidates[name] = {
            "name": name,
            "value_type": value_type,
            "value": deepcopy(value["value"]),
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
    state: Mapping[str, Any], action: Mapping[str, Any]
) -> Dict[str, Any]:
    """Resolve declared refs to exact values without examining executable text."""
    consumed = action.get("consumed_bindings") or {}
    binding_env = action.get("binding_env") or {}
    if not isinstance(consumed, Mapping) or not isinstance(binding_env, Mapping):
        raise PlanBindingError("PLAN_BINDING_DECLARATION_INVALID")
    resolved: Dict[str, PlanBinding] = {}
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
            "value_hash": hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest(),
        })
    return {"environment": environment, "records": records}


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
    """Validate typed operation metadata against a Skill declaration only."""
    operation = str(action.get("operation") or "")
    operations = dict(contract.get("operations") or {})
    if not operation or operation not in operations:
        return ({
            "error_code": "SKILL_OPERATION_UNDECLARED",
            "operation": operation,
            "route": "corrective_generate",
        },)
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
            "route": "corrective_generate",
        })
    for name in sorted(expected_produces - actual_produces):
        violations.append({
            "error_code": "BINDING_PRODUCTION_MISSING",
            "operation": operation,
            "binding_name": name,
            "route": "corrective_generate",
        })
    return tuple(violations)

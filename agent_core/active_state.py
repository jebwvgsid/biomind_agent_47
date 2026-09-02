"""Typed active-state boundaries for step-local execution and Generate dispatch."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional


class ActiveStateInvariantError(ValueError):
    """A persisted active pointer contradicts the current plan scope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)


class GenerationMode(str, Enum):
    NORMAL = "NORMAL"
    RECOVERY = "RECOVERY"


@dataclass(frozen=True)
class GenerationProvenance:
    """Controller-owned provenance for exactly one Generate dispatch."""

    mode: str
    source_route: str
    step_id: str
    plan_revision: int
    recovery_id: Optional[str] = None
    recovery_envelope_id: Optional[str] = None
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normal_generation_provenance(
    plan_state: Mapping[str, Any],
    *,
    source_route: str,
) -> GenerationProvenance:
    return GenerationProvenance(
        mode=GenerationMode.NORMAL.value,
        source_route=str(source_route or "controller"),
        step_id=str(plan_state.get("current_step_id") or ""),
        plan_revision=int(plan_state.get("plan_revision") or 0),
    )


def generation_provenance_for_dispatch(
    plan_state: Mapping[str, Any],
    *,
    source_route: str,
    execution_envelope: Optional[Mapping[str, Any]] = None,
) -> GenerationProvenance:
    """Derive Generate mode only from the typed active recovery owner.

    Historical ``recovery_decision`` fields are intentionally ignored.  A
    recovery envelope is required only when the durable active scope requests
    a recovery Generate dispatch.
    """
    task_state = dict(plan_state.get("task_state") or {})
    recovery_state = dict(task_state.get("recovery_state") or {})
    active = dict(recovery_state.get("active_recovery") or {})
    if not active:
        return normal_generation_provenance(
            plan_state,
            source_route=source_route,
        )

    status = str(active.get("status") or "").strip().lower()
    target_route = str(active.get("target_route") or "").strip().lower()
    if status not in {"pending", "dispatched"} or target_route != "generate":
        return normal_generation_provenance(
            plan_state,
            source_route=source_route,
        )

    recovery_id = str(active.get("recovery_id") or "").strip()
    step_id = str(active.get("step_id") or "").strip()
    current_step_id = str(plan_state.get("current_step_id") or "").strip()
    plan_revision = int(plan_state.get("plan_revision") or 0)
    active_plan_revision = int(active.get("plan_revision") or 0)
    if not recovery_id or not step_id or active_plan_revision <= 0:
        raise ActiveStateInvariantError(
            "ACTIVE_RECOVERY_IDENTITY_MISSING",
            "recovery_id, step_id, and plan_revision are required",
        )
    if step_id != current_step_id or active_plan_revision != plan_revision:
        raise ActiveStateInvariantError(
            "ACTIVE_RECOVERY_SCOPE_MISMATCH",
            (
                f"active={step_id}@{active_plan_revision}, "
                f"current={current_step_id}@{plan_revision}"
            ),
        )

    envelope = dict(
        execution_envelope
        or recovery_state.get("active_recovery_envelope")
        or {}
    )
    if not envelope:
        raise ActiveStateInvariantError(
            "RECOVERY_EXECUTION_ENVELOPE_MISSING",
            f"active recovery {recovery_id} has no execution envelope",
        )
    envelope_id = str(envelope.get("immutable_hash") or "").strip()
    expected_envelope_id = str(active.get("envelope_id") or "").strip()
    envelope_recovery_id = str(
        envelope.get("recovery_id")
        or envelope.get("recovery_decision_id")
        or ""
    ).strip()
    envelope_step_id = str(envelope.get("step_id") or "").strip()
    if envelope_recovery_id != recovery_id:
        raise ActiveStateInvariantError(
            "RECOVERY_EXECUTION_ENVELOPE_MISMATCH",
            "envelope recovery identity does not match active recovery",
        )
    if envelope_step_id != current_step_id:
        raise ActiveStateInvariantError(
            "RECOVERY_EXECUTION_ENVELOPE_MISMATCH",
            "envelope step does not match the current step",
        )
    if not envelope_id or not expected_envelope_id or envelope_id != expected_envelope_id:
        raise ActiveStateInvariantError(
            "RECOVERY_EXECUTION_ENVELOPE_MISMATCH",
            "envelope hash does not match the active recovery envelope_id",
        )
    return GenerationProvenance(
        mode=GenerationMode.RECOVERY.value,
        source_route=str(source_route or "recovery"),
        step_id=current_step_id,
        plan_revision=plan_revision,
        recovery_id=recovery_id,
        recovery_envelope_id=envelope_id,
    )


def scoped_execution_evidence(
    plan_state: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    active_reference: Optional[Mapping[str, Any]] = None,
    attempt_id: str = "",
    action_id: str = "",
) -> Dict[str, Any]:
    """Return only evidence belonging to the current Plan/Step/attempt scope."""
    current_step_id = str(plan_state.get("current_step_id") or "").strip()
    if not current_step_id:
        return {}
    task_id = str(plan_state.get("task_id") or "").strip()
    plan_id = str(plan_state.get("plan_id") or "").strip()
    plan_version = str(plan_state.get("plan_version") or "").strip()
    plan_revision = int(plan_state.get("plan_revision") or 0)
    active = dict(active_reference or {})
    required_attempt = str(
        attempt_id
        or active.get("attempt_id")
        or active.get("execution_id")
        or ""
    ).strip()
    required_action = str(action_id or active.get("action_id") or "").strip()

    for raw in reversed(list(records or [])):
        if not isinstance(raw, Mapping):
            continue
        record = dict(raw)
        contract = dict(record.get("action_contract") or {})
        record_task_id = str(record.get("task_id") or "").strip()
        record_plan_id = str(
            record.get("plan_id") or contract.get("plan_id") or ""
        ).strip()
        record_plan_version = str(
            record.get("plan_version") or contract.get("plan_version") or ""
        ).strip()
        try:
            record_plan_revision = int(
                record.get("plan_revision")
                or contract.get("plan_revision")
                or 0
            )
        except (TypeError, ValueError):
            continue
        record_step_id = str(
            record.get("step_id") or contract.get("step_id") or ""
        ).strip()
        record_attempt = str(
            record.get("attempt_id") or record.get("execution_id") or ""
        ).strip()
        record_action = str(
            record.get("action_id") or contract.get("action_id") or ""
        ).strip()
        if record_step_id != current_step_id:
            continue
        if record_task_id and task_id and record_task_id != task_id:
            continue
        if record_plan_id and plan_id and record_plan_id != plan_id:
            continue
        if record_plan_version and plan_version and record_plan_version != plan_version:
            continue
        if record_plan_revision and plan_revision and record_plan_revision != plan_revision:
            continue
        if required_attempt and record_attempt != required_attempt:
            continue
        if required_action and record_action != required_action:
            continue
        return deepcopy(record)
    return {}

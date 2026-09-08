"""Durable activation transaction for archived model-context epochs.

The compactor owns immutable source/candidate archives.  This module owns the
small controller-side CAS that makes exactly one candidate active and carries
selected Skill residency into the next model phase.  It deliberately does not
persist provider message bodies or grant the compactor business-state authority.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Callable, Dict, Mapping, Optional

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import load_plan_state, save_plan_state


SelectionRevisionReader = Callable[[Mapping[str, Any]], int]


def persist_context_epoch_activation(
    *,
    data_path: str,
    task_id: str,
    phase: str,
    invocation_id: str,
    invocation_ordinal: int,
    source_epoch_id: str,
    selected_revision: int,
    compaction_state: Mapping[str, Any],
    compaction_telemetry: Mapping[str, Any],
    current_selection_revision: SelectionRevisionReader,
) -> Dict[str, Any]:
    """CAS-activate one already-archived context candidate.

    Archive creation and candidate validation must have completed before this
    function is called.  Lost acknowledgements are idempotent; older phase
    invocations and stale selection revisions fail closed.
    """

    phase_name = str(phase or "").strip().lower()
    if phase_name not in {"consultant", "planner", "supervisor"}:
        raise StateCASConflict(
            f"PHASE_CONTEXT_ACTIVATION_SCOPE_INVALID: {phase_name}"
        )
    state_value = deepcopy(dict(compaction_state or {}))
    telemetry = deepcopy(dict(compaction_telemetry or {}))
    pointer = deepcopy(dict(state_value.get("active_pointer") or {}))
    source_archive_ref = str(
        state_value.get("source_archive_ref")
        or telemetry.get("source_archive_ref")
        or ""
    )
    candidate_archive_ref = str(
        state_value.get("candidate_epoch_archive_ref")
        or telemetry.get("candidate_epoch_archive_ref")
        or ""
    )
    effective_revision = max(0, int(selected_revision or 0))
    if (
        not str(pointer.get("epoch_id") or "")
        or not source_archive_ref
        or not candidate_archive_ref
        or str(pointer.get("epoch_archive_ref") or "")
        != candidate_archive_ref
        or int(pointer.get("selected_revision") or 0)
        != effective_revision
        or telemetry.get("compaction_committed") is not True
    ):
        raise StateCASConflict("PHASE_CONTEXT_ACTIVATION_RECORD_INVALID")

    raw_residency = dict(state_value.get("material_residency") or {})
    residency = {
        str(material_id): deepcopy(dict(record))
        for material_id, record in raw_residency.items()
        if isinstance(record, Mapping)
    }
    if len(residency) != len(raw_residency) or any(
        "body" in record
        or str(record.get("material_id") or material_id) != material_id
        or str(record.get("residency") or "")
        not in {"exact", "summarized", "externalized"}
        for material_id, record in residency.items()
    ):
        raise StateCASConflict("PHASE_CONTEXT_MATERIAL_RESIDENCY_INVALID")

    active_record = {
        "schema_version": 1,
        "phase": phase_name,
        "invocation_id": str(invocation_id or ""),
        "invocation_ordinal": max(0, int(invocation_ordinal or 0)),
        "source_epoch_id": str(source_epoch_id or ""),
        "selected_revision": effective_revision,
        "active_pointer": pointer,
        "source_archive_ref": source_archive_ref,
        "candidate_epoch_archive_ref": candidate_archive_ref,
        "material_residency_hash": hashlib.sha256(
            json.dumps(
                residency,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
    }

    last_conflict: Optional[StateCASConflict] = None
    for _ in range(3):
        durable = load_plan_state(data_path, task_id)
        if max(0, int(current_selection_revision(durable) or 0)) != (
            effective_revision
        ):
            raise StateCASConflict("PHASE_CONTEXT_SELECTION_REVISION_STALE")
        task_state = dict(durable.get("task_state") or {})
        activations = dict(task_state.get("phase_context_compaction") or {})
        existing = (
            dict(activations.get(phase_name) or {})
            if isinstance(activations.get(phase_name), Mapping)
            else {}
        )
        residency_by_phase = dict(
            task_state.get("skill_material_residency") or {}
        )
        existing_residency = (
            dict(residency_by_phase.get(phase_name) or {})
            if isinstance(residency_by_phase.get(phase_name), Mapping)
            else {}
        )
        if existing == active_record and existing_residency == residency:
            return durable
        if existing:
            existing_pointer = dict(existing.get("active_pointer") or {})
            existing_order = (
                int(existing.get("invocation_ordinal") or 0),
                int(existing_pointer.get("state_generation") or 0),
                int(existing_pointer.get("history_version") or 0),
            )
            candidate_order = (
                active_record["invocation_ordinal"],
                int(pointer.get("state_generation") or 0),
                int(pointer.get("history_version") or 0),
            )
            if candidate_order < existing_order or (
                candidate_order == existing_order and existing != active_record
            ):
                raise StateCASConflict("PHASE_CONTEXT_ACTIVATION_STALE")

        activations[phase_name] = deepcopy(active_record)
        activations["latest"] = deepcopy(active_record)
        residency_by_phase[phase_name] = deepcopy(residency)
        residency_by_phase["latest"] = deepcopy(residency)
        task_state["phase_context_compaction"] = activations
        task_state["skill_material_residency"] = residency_by_phase
        durable["task_state"] = task_state
        try:
            save_plan_state(data_path, task_id, durable)
            return load_plan_state(data_path, task_id)
        except StateCASConflict as exc:
            last_conflict = exc
    raise last_conflict or StateCASConflict(
        "PHASE_CONTEXT_ACTIVATION_TRANSACTION_STALE"
    )


__all__ = [
    "SelectionRevisionReader",
    "persist_context_epoch_activation",
]

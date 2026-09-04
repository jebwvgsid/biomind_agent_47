"""Scope-checked, explicit inspection of Step archives by Step or Recovery.

This module is a read service, not a context builder.  Its return value is never
merged into a phase seed automatically.  Callers receive only ``summary`` by
default and must explicitly request ``section="transcript"`` (with pagination)
to see a local transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent_core.workflow.phase_archive import (
    PHASE_ARCHIVE_REF_PREFIX,
    PhaseArchiveRecord,
    PhaseArchiveStore,
)
from agent_core.workflow.stage_contracts import FailurePacket, StageContractError


ReferenceResolver = Callable[[str], Mapping[str, Any] | None]


class PhaseInspectionError(ValueError):
    """Fail-closed inspection or reference-scope violation."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "PHASE_INSPECTION_ERROR")
        self.detail = str(detail or "")
        super().__init__(
            self.code if not self.detail else f"{self.code}: {self.detail}"
        )


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PhaseInspectionError("PHASE_INSPECTION_FIELD_REQUIRED", field)
    return text


def _mapping_at(value: Any, *path: str) -> Mapping[str, Any]:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _archive_step_id(record: PhaseArchiveRecord) -> str:
    """Resolve an explicit Step identity without guessing from filenames."""

    metadata = record.metadata
    seed = _mapping_at(metadata, "seed_contract")
    candidates = {
        str(value).strip()
        for value in (
            metadata.get("step_id") if isinstance(metadata, Mapping) else None,
            _mapping_at(metadata, "current_step").get("step_id"),
            _mapping_at(seed, "current_step").get("step_id"),
            _mapping_at(seed, "failure_packet").get("step_id"),
        )
        if str(value or "").strip()
    }
    if not candidates:
        raise PhaseInspectionError(
            "PHASE_ARCHIVE_STEP_SCOPE_MISSING",
            f"archive {record.archive_ref} has no explicit step_id metadata",
        )
    if len(candidates) != 1:
        raise PhaseInspectionError(
            "PHASE_ARCHIVE_STEP_SCOPE_CONFLICT",
            f"archive {record.archive_ref} declares {sorted(candidates)!r}",
        )
    return next(iter(candidates))


def _validate_embedded_task_scope(record: PhaseArchiveRecord) -> None:
    metadata = record.metadata
    seed = _mapping_at(metadata, "seed_contract")
    candidates = {
        str(value).strip()
        for value in (
            metadata.get("task_id") if isinstance(metadata, Mapping) else None,
            seed.get("task_id") if isinstance(seed, Mapping) else None,
            _mapping_at(seed, "task_spec_ref").get("task_id"),
            _mapping_at(seed, "plan_spec_ref").get("task_id"),
            _mapping_at(seed, "failure_packet").get("task_id"),
        )
        if str(value or "").strip()
    }
    if any(value != record.task_id for value in candidates):
        raise PhaseInspectionError(
            "PHASE_ARCHIVE_TASK_SCOPE_CONFLICT",
            f"archive task={record.task_id!r}, embedded={sorted(candidates)!r}",
        )


@dataclass(frozen=True)
class _FailureReference:
    value: str
    field: str
    step_scoped: bool = True


def _failure_references(packet: FailurePacket) -> tuple[_FailureReference, ...]:
    references: list[_FailureReference] = []
    for index, value in enumerate(packet.evidence_refs):
        references.append(_FailureReference(value, f"evidence_refs[{index}]"))
    if packet.attempt_ref:
        references.append(_FailureReference(packet.attempt_ref, "attempt_ref"))
    if packet.action_ref:
        references.append(_FailureReference(packet.action_ref, "action_ref"))
    for index, fact in enumerate(packet.diagnostic_facts):
        value = str(fact.get("evidence_ref") or "").strip()
        if value:
            references.append(
                _FailureReference(
                    value, f"diagnostic_facts[{index}].evidence_ref"
                )
            )
    if packet.plan_spec_ref is not None and packet.plan_spec_ref.plan_snapshot_ref:
        references.append(
            _FailureReference(
                packet.plan_spec_ref.plan_snapshot_ref,
                "plan_spec_ref.plan_snapshot_ref",
                step_scoped=False,
            )
        )
    return tuple(references)


class PhaseInspectionService:
    """Read verified archive sections through a task/phase/step scope gate."""

    def __init__(
        self,
        store: PhaseArchiveStore,
        *,
        reference_resolver: ReferenceResolver | None = None,
    ) -> None:
        if not isinstance(store, PhaseArchiveStore):
            raise PhaseInspectionError(
                "PHASE_ARCHIVE_STORE_INVALID", type(store).__name__
            )
        self.store = store
        self.reference_resolver = reference_resolver

    @staticmethod
    def _normalize_section(
        section: str | None, offset: int, limit: int | None
    ) -> str:
        name = "summary" if section is None else str(section).strip().lower()
        if not name:
            name = "summary"
        if name != "transcript" and (offset != 0 or limit is not None):
            raise PhaseInspectionError(
                "PHASE_INSPECTION_PAGINATION_WITHOUT_TRANSCRIPT"
            )
        return name

    def _read_scoped_archive(
        self,
        *,
        archive_ref: str,
        task_id: str,
        phase: str,
        step_id: str,
    ) -> PhaseArchiveRecord:
        expected_task = _required_text(task_id, "task_id")
        expected_phase = _required_text(phase, "phase")
        expected_step = _required_text(step_id, "step_id")
        record = self.store.read(_required_text(archive_ref, "archive_ref"))
        _validate_embedded_task_scope(record)
        observed_step = _archive_step_id(record)
        mismatches: list[str] = []
        if record.task_id != expected_task:
            mismatches.append(
                f"task_id expected={expected_task!r} observed={record.task_id!r}"
            )
        if record.phase != expected_phase:
            mismatches.append(
                f"phase expected={expected_phase!r} observed={record.phase!r}"
            )
        if observed_step != expected_step:
            mismatches.append(
                f"step_id expected={expected_step!r} observed={observed_step!r}"
            )
        if mismatches:
            raise PhaseInspectionError(
                "PHASE_INSPECTION_SCOPE_MISMATCH", "; ".join(mismatches)
            )
        return record

    def _inspect_record(
        self,
        record: PhaseArchiveRecord,
        *,
        section: str | None,
        offset: int,
        limit: int | None,
    ) -> dict[str, Any]:
        section_name = self._normalize_section(section, offset, limit)
        view = self.store.inspect(
            record.archive_ref,
            section=section_name,
            offset=offset,
            limit=limit,
        )
        # Scope is included as an audit receipt.  No seed, prompt, or automatic
        # context-injection field is returned by this service.
        return {**view, "step_id": _archive_step_id(record)}

    def inspect_step(
        self,
        *,
        archive_ref: str,
        task_id: str,
        phase: str,
        step_id: str,
        section: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Inspect one Step archive; summary is the only implicit section."""

        record = self._read_scoped_archive(
            archive_ref=archive_ref,
            task_id=task_id,
            phase=phase,
            step_id=step_id,
        )
        return self._inspect_record(
            record, section=section, offset=offset, limit=limit
        )

    def _resolve_failure_reference(
        self,
        reference: _FailureReference,
        *,
        task_id: str,
        step_id: str,
    ) -> None:
        value = _required_text(reference.value, reference.field)
        if value.startswith(PHASE_ARCHIVE_REF_PREFIX):
            record = self.store.read(value)
            _validate_embedded_task_scope(record)
            if record.task_id != task_id:
                raise PhaseInspectionError(
                    "FAILURE_REFERENCE_SCOPE_MISMATCH",
                    f"{reference.field}.task_id={record.task_id!r}",
                )
            if reference.step_scoped and _archive_step_id(record) != step_id:
                raise PhaseInspectionError(
                    "FAILURE_REFERENCE_SCOPE_MISMATCH",
                    f"{reference.field}.step_id={_archive_step_id(record)!r}",
                )
            return

        if self.reference_resolver is None:
            raise PhaseInspectionError(
                "FAILURE_REFERENCE_UNVERIFIED",
                f"{reference.field}={value!r} has no resolver",
            )
        try:
            resolved = self.reference_resolver(value)
        except Exception as exc:
            raise PhaseInspectionError(
                "FAILURE_REFERENCE_RESOLUTION_FAILED",
                f"{reference.field}={value!r}: {type(exc).__name__}: {exc}",
            ) from exc
        if not isinstance(resolved, Mapping):
            raise PhaseInspectionError(
                "FAILURE_REFERENCE_UNVERIFIED",
                f"{reference.field}={value!r} did not resolve",
            )
        resolved_task = str(resolved.get("task_id") or "").strip()
        resolved_step = str(resolved.get("step_id") or "").strip()
        if resolved_task != task_id or (
            reference.step_scoped and resolved_step != step_id
        ):
            raise PhaseInspectionError(
                "FAILURE_REFERENCE_SCOPE_MISMATCH",
                (
                    f"{reference.field}={value!r} resolved task={resolved_task!r} "
                    f"step={resolved_step!r}"
                ),
            )

    def validate_failure_packet_references(
        self,
        failure_packet: FailurePacket | Mapping[str, Any],
        *,
        task_id: str,
        step_id: str,
    ) -> FailurePacket:
        """Validate the packet and every direct/nested ``*_ref`` it carries."""

        expected_task = _required_text(task_id, "task_id")
        expected_step = _required_text(step_id, "step_id")
        try:
            packet = (
                failure_packet
                if isinstance(failure_packet, FailurePacket)
                else FailurePacket.from_mapping(failure_packet)
            )
        except StageContractError as exc:
            raise PhaseInspectionError(
                "FAILURE_PACKET_INVALID", f"{exc.code}: {exc.detail}"
            ) from exc
        if packet.task_id != expected_task or packet.step_id != expected_step:
            raise PhaseInspectionError(
                "FAILURE_PACKET_SCOPE_MISMATCH",
                (
                    f"expected task={expected_task!r} step={expected_step!r}; "
                    f"observed task={packet.task_id!r} step={packet.step_id!r}"
                ),
            )
        for reference in _failure_references(packet):
            self._resolve_failure_reference(
                reference, task_id=expected_task, step_id=expected_step
            )
        return packet

    def inspect_for_recovery(
        self,
        *,
        failure_packet: FailurePacket | Mapping[str, Any],
        archive_ref: str,
        task_id: str,
        phase: str,
        step_id: str,
        section: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Inspect an explicitly referenced Step archive for Recovery."""

        packet = self.validate_failure_packet_references(
            failure_packet, task_id=task_id, step_id=step_id
        )
        declared = {reference.value for reference in _failure_references(packet)}
        if archive_ref not in declared:
            raise PhaseInspectionError(
                "FAILURE_ARCHIVE_REF_NOT_DECLARED", str(archive_ref or "")
            )
        return self.inspect_step(
            archive_ref=archive_ref,
            task_id=task_id,
            phase=phase,
            step_id=step_id,
            section=section,
            offset=offset,
            limit=limit,
        )


__all__ = [
    "PhaseInspectionError",
    "PhaseInspectionService",
    "ReferenceResolver",
]

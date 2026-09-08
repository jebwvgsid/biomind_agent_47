"""Production adapter from scoped LangChain messages to schema-2 compaction.

The semantic compactor remains a read-only selector.  This adapter owns the
mechanical work around it: constructing exact source units, archiving the
source request before the model call, restoring retained provider messages,
and carrying only an explicit lossy summary/retrieval state into the next
request baseline.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import inspect
import json
from typing import Any, Awaitable, Callable, Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_core.workflow.context_compaction import (
    ActiveEpochPointer,
    ContextBudgetPolicy,
    ContextCompactionAttemptsExhausted,
    ContextCompactionError,
    ReferenceTokenEstimator,
    SourceUnit,
    build_source_unit_manifest,
    measure_provider_input,
    measure_uncompacted_request,
    new_compaction_request,
    run_compaction_transaction,
)
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.scoped_context import provider_visible_payload


CompactorInvoke = Callable[..., Awaitable[BaseMessage] | BaseMessage]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


def _material_record_list(value: Any) -> list[dict[str, Any]]:
    """Normalize internal, already-verified material identity metadata.

    The metadata is never projected to the provider wire.  Full identity is
    required only when a selected material becomes summarized; exact-only
    compatibility descriptors may remain partial until then.
    """

    raw_items = (
        [value]
        if isinstance(value, Mapping)
        else list(value or ())
        if isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        else []
    )
    identity_fields = {
        "schema_version", "kind", "material_id", "skill_id", "snapshot_id",
        "excerpt_id", "source_ref", "content_hash", "coverage",
        "resource_snapshot_id", "resource_content_hash", "resource_path",
        "source_path", "source_kind", "selected_scope",
        "selected_target_step_id", "selected_at_task_revision",
        "first_seen_sequence", "residency", "retrieval_ref",
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ContextCompactionError(
                "SKILL_MATERIAL_RECORD_INVALID", type(raw).__name__
            )
        record = {
            str(key): _plain_json(item)
            for key, item in raw.items()
            if str(key) in identity_fields
        }
        material_id = str(record.get("material_id") or "").strip()
        if material_id and material_id in seen:
            continue
        if material_id:
            seen.add(material_id)
        result.append(record)
    return result


def _message_material_records(message: BaseMessage) -> list[dict[str, Any]]:
    additional = getattr(message, "additional_kwargs", None)
    if not isinstance(additional, Mapping):
        return []
    return _material_record_list(
        additional.get("schema2_skill_material_records")
    )


def _complete_material_identity(
    record: Mapping[str, Any],
    *,
    source_unit: SourceUnit,
) -> dict[str, Any]:
    material_id = str(record.get("material_id") or "").strip()
    skill_id = str(record.get("skill_id") or "").strip()
    source_ref = str(record.get("source_ref") or "").strip()
    content_hash = str(record.get("content_hash") or "").strip().lower()
    coverage = _plain_json(record.get("coverage"))
    if (
        not material_id
        or not skill_id
        or not source_ref
        or len(content_hash.removeprefix("sha256:")) != 64
        or any(
            char not in "0123456789abcdef"
            for char in content_hash.removeprefix("sha256:")
        )
        or not isinstance(coverage, Mapping)
        or str(coverage.get("kind") or "") not in {"full", "range"}
    ):
        raise ContextCompactionError(
            "COMPACTED_SKILL_MATERIAL_IDENTITY_INCOMPLETE",
            f"source_unit_id={source_unit.unit_id}; material_id={material_id}",
        )
    retrieval_ref = str(
        record.get("retrieval_ref") or source_ref
    ).strip()
    if not retrieval_ref:
        raise ContextCompactionError(
            "COMPACTED_SKILL_MATERIAL_IDENTITY_INCOMPLETE", material_id
        )
    return {
        "material_id": material_id,
        "skill_id": skill_id,
        "source_ref": source_ref,
        "content_hash": content_hash.removeprefix("sha256:"),
        "coverage": coverage,
        "retrieval_ref": retrieval_ref,
    }


def _message_record(message: BaseMessage) -> dict[str, Any]:
    return {
        "message_id": str(getattr(message, "id", None) or ""),
        "provider_payload": provider_visible_payload(message),
    }


def _message_from_record(record: Mapping[str, Any]) -> BaseMessage:
    payload = deepcopy(dict(record.get("provider_payload") or {}))
    message_id = str(record.get("message_id") or "")
    kind = str(payload.pop("type", "") or payload.pop("role", "")).lower()
    content = payload.pop("content", "")
    name = str(payload.pop("name", "") or "") or None
    if kind in {"system"}:
        return SystemMessage(id=message_id or None, content=content, name=name)
    if kind in {"ai", "assistant"}:
        return AIMessage(
            id=message_id or None,
            content=content,
            name=name,
            tool_calls=deepcopy(payload.pop("tool_calls", [])),
        )
    if kind == "tool":
        tool_call_id = str(payload.pop("tool_call_id", "") or "")
        if not tool_call_id:
            raise ContextCompactionError(
                "COMPACTED_TOOL_CALL_ID_MISSING", message_id
            )
        return ToolMessage(
            id=message_id or None,
            content=content,
            name=name,
            tool_call_id=tool_call_id,
        )
    if kind not in {"human", "user"}:
        raise ContextCompactionError(
            "COMPACTED_MESSAGE_TYPE_INVALID", kind or "<missing>"
        )
    return HumanMessage(id=message_id or None, content=content, name=name)


def _compactor_messages(
    messages: Sequence[Mapping[str, str]],
) -> list[BaseMessage]:
    result: list[BaseMessage] = []
    for item in messages:
        role = str(item.get("role") or "").lower()
        content = str(item.get("content") or "")
        if role == "system":
            result.append(SystemMessage(content=content))
        elif role in {"human", "user"}:
            result.append(HumanMessage(content=content))
        else:
            raise ContextCompactionError(
                "COMPACTOR_MESSAGE_ROLE_INVALID", role
            )
    return result


def _protocol_groups(messages: Sequence[BaseMessage]) -> dict[int, str]:
    """Bind one assistant tool-call message and all matching results together."""

    by_call_id: dict[str, int] = {}
    groups: dict[int, str] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage):
            continue
        calls = list(getattr(message, "tool_calls", None) or ())
        if not calls:
            continue
        group = "provider-tool-group-" + _hash_text(
            _canonical([str(item.get("id") or "") for item in calls])
        )[:20]
        groups[index] = group
        for call in calls:
            call_id = str(call.get("id") or "")
            if call_id:
                by_call_id[call_id] = index
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage):
            continue
        owner = by_call_id.get(str(message.tool_call_id or ""))
        if owner is None:
            raise ContextCompactionError(
                "COMPACTION_ORPHAN_TOOL_RESULT", str(message.tool_call_id or "")
            )
        groups[index] = groups[owner]
    return groups


def _pointer_from_state(
    value: Mapping[str, Any] | None,
    *,
    default_epoch_id: str,
    history_version: int,
    selected_revision: int,
) -> ActiveEpochPointer:
    source = dict(value or {})
    pointer = dict(source.get("active_pointer") or {})
    return ActiveEpochPointer(
        state_generation=int(pointer.get("state_generation") or 0),
        epoch_id=str(pointer.get("epoch_id") or default_epoch_id),
        epoch_archive_ref=str(pointer.get("epoch_archive_ref") or ""),
        # Appending a genuine event advances history, but not context epoch.
        history_version=int(history_version),
        selected_revision=int(selected_revision),
    )


@dataclass(frozen=True)
class PhaseCompactionResult:
    messages: tuple[BaseMessage, ...]
    state: Mapping[str, Any]
    telemetry: Mapping[str, Any]
    triggered: bool


class PhaseContextCompactor:
    """Compact one scoped phase request without mutating task/plan authority."""

    def __init__(
        self,
        *,
        phase_store: PhaseArchiveStore,
        compactor_invoke: CompactorInvoke,
        policy: ContextBudgetPolicy | None = None,
    ) -> None:
        self.phase_store = phase_store
        self.compactor_invoke = compactor_invoke
        self.policy = policy or ContextBudgetPolicy()
        self.estimator = ReferenceTokenEstimator()

    async def prepare(
        self,
        *,
        task_id: str,
        phase: str,
        invocation_id: str,
        source_epoch_id: str,
        system_message: BaseMessage,
        seed_message: BaseMessage,
        inherited_messages: Sequence[BaseMessage],
        local_messages: Sequence[BaseMessage],
        pending_messages: Sequence[BaseMessage] = (),
        material_descriptors: Sequence[Mapping[str, Any]] = (),
        prior_state: Mapping[str, Any] | None = None,
        selected_revision: int = 0,
    ) -> PhaseCompactionResult:
        prior = deepcopy(dict(prior_state or {}))
        prior_baseline = [
            _message_from_record(item)
            for item in prior.get("baseline_messages") or ()
            if isinstance(item, Mapping)
        ]
        prior_offset = max(0, int(prior.get("local_history_offset") or 0))
        local = list(local_messages)
        pending = list(pending_messages)
        if prior_baseline:
            # The old exact source remains archived.  Only the compacted
            # baseline and events appended after its source boundary are live.
            source_messages = [
                system_message,
                seed_message,
                *prior_baseline,
                *local[prior_offset:],
                *pending,
            ]
        else:
            source_messages = [
                system_message,
                seed_message,
                *list(inherited_messages),
                *local,
                *pending,
            ]

        descriptor_by_message: dict[str, list[dict[str, Any]]] = {}
        for key, item in dict(
            prior.get("material_descriptors_by_message") or {}
        ).items():
            records = _material_record_list(item)
            if records:
                descriptor_by_message[str(key)] = records
        for message, descriptor in zip(
            inherited_messages, material_descriptors
        ):
            message_id = str(getattr(message, "id", None) or "")
            records = _material_record_list(descriptor)
            if message_id and records:
                descriptor_by_message[message_id] = records
        for message in [*local, *pending]:
            message_id = str(getattr(message, "id", None) or "")
            records = _message_material_records(message)
            if message_id and records:
                combined = [
                    *descriptor_by_message.get(message_id, []), *records
                ]
                descriptor_by_message[message_id] = _material_record_list(
                    combined
                )
        material_revisions = [
            int(record.get("selected_at_task_revision") or 0)
            for records in descriptor_by_message.values()
            for record in records
        ]
        effective_selected_revision = max(
            [int(selected_revision or 0), *material_revisions]
        )
        pointer = _pointer_from_state(
            prior,
            default_epoch_id=source_epoch_id,
            history_version=len(local) + len(pending),
            selected_revision=effective_selected_revision,
        )
        groups = _protocol_groups(source_messages[2:])
        bodies: dict[str, str] = {}
        units: list[SourceUnit] = []
        for position, message in enumerate(source_messages[2:]):
            record = _message_record(message)
            body = _canonical(record)
            message_id = str(record.get("message_id") or f"index-{position:06d}")
            descriptors = descriptor_by_message.get(message_id) or []
            descriptor = descriptors[0] if descriptors else {}
            material_ids = [
                str(item.get("material_id") or "")
                for item in descriptors
                if str(item.get("material_id") or "")
            ]
            source_ref = str(
                descriptor.get("source_ref")
                or f"phase-message:{pointer.epoch_id}:{message_id}"
            )
            unit_id = (
                f"unit:{pointer.epoch_id}:{position:06d}:"
                + _hash_text(message_id + "\n" + body)[:20]
            )
            body_ref = f"{source_ref}#provider-message={position}"
            bodies[body_ref] = body
            units.append(SourceUnit(
                unit_id=unit_id,
                kind=(
                    "selected_skill_material"
                    if descriptor else "provider_message"
                ),
                source_ref=source_ref,
                body_ref=body_ref,
                content_hash=_hash_text(body),
                coverage={"kind": "full"},
                scope={
                    "kind": "phase_context",
                    "task_id": str(task_id),
                    "phase": str(phase),
                    "epoch_id": pointer.epoch_id,
                    "history_version": pointer.history_version,
                    "selected_revision": pointer.selected_revision,
                },
                token_count=self.estimator.estimate(body).tokens,
                body_length_chars=len(body),
                selected=bool(descriptor),
                skill_id=(
                    str(descriptor.get("skill_id") or "")
                    if len({
                        str(item.get("skill_id") or "")
                        for item in descriptors
                    }) <= 1
                    else "multiple-selected-materials"
                ),
                protocol_group_id=str(groups.get(position) or ""),
                provenance={
                    "message_id": message_id,
                    "material_id": material_ids[0] if material_ids else "",
                    "material_ids": material_ids,
                    "material_records": descriptors,
                    "original_position": position,
                    "retrieval_ref": source_ref,
                },
            ))

        manifest = build_source_unit_manifest(
            source_epoch_id=pointer.epoch_id,
            history_version=pointer.history_version,
            selected_revision=pointer.selected_revision,
            units=units,
        )
        request = new_compaction_request(
            manifest,
            budget_policy=self.policy,
            fixed_components={
                "system": provider_visible_payload(system_message),
                "frozen_inputs": provider_visible_payload(seed_message),
                "tool_schema": [],
                "provider_wrappers": {
                    "message_count": len(source_messages),
                    "message_protocol": "langchain-provider-visible-v1",
                },
            },
            prior_summary=str(prior.get("summary") or ""),
            prior_retrieval_state=list(prior.get("retrieval_state") or ()),
        )
        budget = measure_uncompacted_request(
            request, bodies, estimator=self.estimator
        )
        if not budget.should_compact:
            return PhaseCompactionResult(
                messages=tuple(source_messages),
                state=prior,
                telemetry=budget.to_dict(),
                triggered=False,
            )

        source_transcript = [
            {**provider_visible_payload(message), "message_id": str(message.id or "")}
            for message in source_messages
        ]

        def archive_source(payload: Mapping[str, Any]) -> str:
            digest = _hash_text(_canonical(payload))
            record = self.phase_store.archive(
                task_id=str(task_id),
                phase=f"{phase}-context-source",
                invocation_id=(
                    f"{invocation_id}:{request.compaction_request_id}:source:{digest[:16]}"
                ),
                summary=deepcopy(dict(payload)),
                transcript=source_transcript,
                metadata={
                    "source_epoch_id": pointer.epoch_id,
                    "history_version": pointer.history_version,
                    "selected_revision": pointer.selected_revision,
                    "complete_provider_source": True,
                },
            )
            return record.archive_ref

        def archive_candidate(payload: Mapping[str, Any]) -> str:
            digest = _hash_text(_canonical(payload))
            record = self.phase_store.archive(
                task_id=str(task_id),
                phase=f"{phase}-context-epoch",
                invocation_id=(
                    f"{invocation_id}:{request.compaction_request_id}:candidate:{digest[:16]}"
                ),
                summary=deepcopy(dict(payload)),
                metadata={
                    "source_epoch_id": pointer.epoch_id,
                    "history_version": pointer.history_version,
                    "selected_revision": pointer.selected_revision,
                    "retained_bodies_single_sourced": True,
                },
            )
            return record.archive_ref

        async def invoke_compactor(
            wire: Sequence[Mapping[str, str]],
        ) -> BaseMessage:
            compactor_messages = _compactor_messages(wire)
            try:
                signature = inspect.signature(self.compactor_invoke)
                accepts_context = "task_id" in signature.parameters or any(
                    item.kind is inspect.Parameter.VAR_KEYWORD
                    for item in signature.parameters.values()
                )
            except (TypeError, ValueError):
                accepts_context = False
            value = (
                self.compactor_invoke(
                    compactor_messages,
                    task_id=str(task_id),
                    phase=str(phase),
                    source_epoch_id=pointer.epoch_id,
                    compaction_request_id=request.compaction_request_id,
                )
                if accepts_context
                else self.compactor_invoke(compactor_messages)
            )
            if hasattr(value, "__await__"):
                value = await value
            if not isinstance(value, BaseMessage):
                raise ContextCompactionError(
                    "COMPACTOR_RESPONSE_INVALID", type(value).__name__
                )
            return value

        try:
            prepared, receipt = await run_compaction_transaction(
                request,
                bodies,
                invoke_compactor,
                archive_source=archive_source,
                archive_candidate=archive_candidate,
                read_current=lambda: pointer,
                estimator=self.estimator,
            )
        except Exception as exc:
            # Compaction is an optimization while the already assembled
            # business request remains below the hard provider boundary.  A
            # model/format/archive failure must not discard that intact
            # request or mutate the active epoch.  Once the source request is
            # no longer safely submit-able, however, fail closed with the
            # original typed failure (and its bounded-attempt evidence).
            if budget.hard_block:
                raise
            failure = (
                exc.to_dict()
                if isinstance(exc, ContextCompactionError)
                else {
                    "error_code": "CONTEXT_COMPACTION_TRANSACTION_FAILED",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            attempts = (
                [item.to_dict() for item in exc.attempts]
                if isinstance(exc, ContextCompactionAttemptsExhausted)
                else []
            )
            return PhaseCompactionResult(
                messages=tuple(source_messages),
                state=prior,
                telemetry={
                    **budget.to_dict(),
                    "compaction_triggered": True,
                    "compaction_committed": False,
                    "compaction_failure": failure,
                    "compactor_attempt_count": len(attempts),
                    "attempts": attempts,
                    "fallback": "safe_uncompacted_request",
                },
                triggered=True,
            )
        retained_messages = [
            _message_from_record(json.loads(item.body))
            for item in prepared.epoch.retained_materials
        ]
        if prepared.epoch.summary:
            retained_messages.append(HumanMessage(
                id=f"{receipt.active.epoch_id}:compaction-summary",
                name="context_compaction_summary",
                content=(
                    '<context_compaction_summary authority="lossy_working_memory_only">\n'
                    + prepared.epoch.summary
                    + "\n<material_retrieval_state>\n"
                    + _canonical([
                        item.to_dict() for item in prepared.epoch.retrieval_state
                    ])
                    + "\n</material_retrieval_state>\n"
                    + "</context_compaction_summary>"
                ),
            ))

        residency: dict[str, dict[str, Any]] = {
            str(key): deepcopy(dict(value))
            for key, value in dict(prior.get("material_residency") or {}).items()
            if isinstance(value, Mapping)
        }
        by_id = manifest.by_id()
        retained_ids = set(prepared.epoch.plan.retain_exact_unit_ids)
        for source_unit_id in retained_ids:
            source = by_id[source_unit_id]
            raw_records = source.provenance.get("material_records") or ()
            for raw_record in raw_records:
                if not isinstance(raw_record, Mapping):
                    raise ContextCompactionError(
                        "COMPACTED_SKILL_MATERIAL_IDENTITY_INCOMPLETE",
                        source_unit_id,
                    )
                try:
                    identity = _complete_material_identity(
                        raw_record, source_unit=source
                    )
                except ContextCompactionError:
                    # Old callers may carry a descriptor sufficient to mark a
                    # provider message selected but not a complete downstream
                    # material identity.  Exact retention needs no override;
                    # a prior lossy override, however, must never be cleared
                    # without a fully verified original identity.
                    partial_id = str(raw_record.get("material_id") or "")
                    if partial_id and partial_id in residency:
                        raise
                    continue
                residency[identity["material_id"]] = {
                    **identity,
                    "residency": "exact",
                    "summary": "",
                    "missing_details": "",
                    "compaction_request_id": request.compaction_request_id,
                    "provider_source_unit_identity": {
                        "source_unit_id": source.unit_id,
                        "source_ref": source.source_ref,
                        "body_ref": source.body_ref,
                        "content_hash": source.content_hash,
                        "coverage": deepcopy(source.to_dict()["coverage"]),
                    },
                }
        for item in prepared.epoch.retrieval_state:
            source = by_id[item.source_unit_id]
            raw_records = source.provenance.get("material_records") or ()
            for raw_record in raw_records:
                if not isinstance(raw_record, Mapping):
                    raise ContextCompactionError(
                        "COMPACTED_SKILL_MATERIAL_IDENTITY_INCOMPLETE",
                        source.unit_id,
                    )
                identity = _complete_material_identity(
                    raw_record, source_unit=source
                )
                residency[identity["material_id"]] = {
                    **identity,
                    "residency": item.residency,
                    "reason": item.reason,
                    "summary": item.summary,
                    "missing_details": item.missing_details,
                    "compaction_request_id": request.compaction_request_id,
                    "provider_source_unit_identity": {
                        "source_unit_id": source.unit_id,
                        "source_ref": source.source_ref,
                        "body_ref": source.body_ref,
                        "content_hash": source.content_hash,
                        "coverage": deepcopy(source.to_dict()["coverage"]),
                        "model_retrieval_ref": item.retrieval_ref,
                    },
                }
        active_pointer = {
            "state_generation": receipt.active.state_generation,
            "epoch_id": receipt.active.epoch_id,
            "epoch_archive_ref": receipt.active.epoch_archive_ref,
            "history_version": receipt.active.history_version,
            "selected_revision": receipt.active.selected_revision,
        }
        hierarchy_telemetry = deepcopy(
            prepared.epoch.to_archive_payload().get(
                "hierarchical_compaction"
            ) or {}
        )
        next_state = {
            "schema_version": 1,
            "effective_selected_revision": pointer.selected_revision,
            "active_pointer": active_pointer,
            "baseline_messages": [_message_record(item) for item in retained_messages],
            "local_history_offset": len(local) + len(pending),
            "material_descriptors_by_message": descriptor_by_message,
            "summary": prepared.epoch.summary,
            "retrieval_state": [
                item.to_dict() for item in prepared.epoch.retrieval_state
            ],
            "material_residency": residency,
            "source_archive_ref": prepared.epoch.record.source_archive_ref,
            "candidate_epoch_archive_ref": receipt.candidate_epoch_archive_ref,
            "attempts": [item.to_dict() for item in prepared.attempts],
            "budget": prepared.epoch.budget.to_dict(),
            "hierarchical_compaction": hierarchy_telemetry,
        }
        final_messages = [system_message, seed_message, *retained_messages]
        final_budget = measure_provider_input(
            {
                "system": provider_visible_payload(system_message),
                "frozen_inputs": provider_visible_payload(seed_message),
                "local_messages": [
                    provider_visible_payload(message)
                    for message in retained_messages
                ],
                "provider_wrappers": {
                    "message_count": len(final_messages),
                    "message_protocol": "langchain-provider-visible-v1",
                },
            },
            policy=self.policy,
            estimator=self.estimator,
        )
        if final_budget.hard_block:
            # This is a final-wire guard, not a Python content selector.  The
            # compactor already had its bounded opportunity to revise the
            # exact retain set; silently deleting retained units here would
            # violate the semantic-authority contract.
            raise ContextCompactionError(
                "COMPACTED_FINAL_PROVIDER_INPUT_UNSAFE",
                (
                    f"estimated_input_tokens={final_budget.estimated_input_tokens}; "
                    f"safe_input_tokens={final_budget.policy.safe_input_tokens}"
                ),
                feedback={"final_provider_budget": final_budget.to_dict()},
            )
        return PhaseCompactionResult(
            messages=tuple(final_messages),
            state=next_state,
            telemetry={
                **prepared.epoch.budget.to_dict(),
                "initial_budget": prepared.initial_budget.to_dict(),
                "final_provider_budget": final_budget.to_dict(),
                "compaction_triggered": True,
                "compaction_committed": True,
                "compactor_attempt_count": len(prepared.attempts),
                "source_archive_ref": prepared.epoch.record.source_archive_ref,
                "candidate_epoch_archive_ref": receipt.candidate_epoch_archive_ref,
                "material_residency": deepcopy(residency),
                "effective_selected_revision": pointer.selected_revision,
                "hierarchical_compaction": hierarchy_telemetry,
            },
            triggered=True,
        )


__all__ = [
    "CompactorInvoke",
    "PhaseCompactionResult",
    "PhaseContextCompactor",
]

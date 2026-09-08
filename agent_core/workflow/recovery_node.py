"""Schema-2 Recovery node: causal Seed -> local agent -> deterministic runtime."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from langgraph.errors import GraphBubbleUp

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.skill_access import (
    SkillAccessService,
    selected_skill_materials_for_carry,
)
from agent_core.workflow.context_compaction import (
    CompactionRequest,
    ContextBudgetPolicy,
    ContextCompactionError,
    ContextEpochCASConflict,
    ReferenceTokenEstimator,
    SourceUnit,
    TokenEstimator,
    build_source_unit_manifest,
    new_compaction_request,
    prepare_compaction,
)
from agent_core.workflow.outcomes import RecoveryOutcome
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_agent import (
    RECOVERY_SYSTEM_PROMPT_VERSION,
    RecoveryAgentError,
    RecoveryAgentLoop,
    RecoveryAgentLoopConfig,
    recovery_seed_digest,
    recovery_tool_schemas,
)
from agent_core.workflow.recovery_context import (
    RecoveryObservationStore,
    archive_recovery_compaction_epoch,
    archive_recovery_episode,
)
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_runtime import RecoveryRuntime
from agent_core.workflow.recovery_tools import (
    RecoveryLocalToolDispatcher,
    build_recovery_inspection_registry,
)
from agent_core.workflow.stage_contracts import StageContractError
from agent_core.workflow.scoped_context import provider_visible_payload


Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]
InheritedMaterialsFactory = Callable[..., Sequence[Any]]
logger = logging.getLogger(__name__)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _message_id(message: BaseMessage, *, fallback: str) -> str:
    return str(getattr(message, "id", None) or fallback).strip()


def _render_material_message(
    raw: Mapping[str, Any],
    *,
    epoch_id: str,
    ordinal: int,
) -> tuple[HumanMessage, dict[str, Any]]:
    item = deepcopy(dict(raw))
    material_id = str(
        item.get("material_id")
        or item.get("source_ref")
        or f"material-{ordinal:04d}"
    ).strip()
    residency = str(item.get("residency") or "exact").strip().lower()
    if residency not in {"exact", "summarized", "externalized"}:
        raise ValueError(f"RECOVERY_INHERITED_MATERIAL_INVALID: {material_id}")
    metadata = {
        key: deepcopy(value)
        for key, value in item.items()
        if key not in {"body", "content", "summary", "missing_details"}
        and value not in (None, "", [], {})
    }
    if residency == "exact":
        if "body" not in item and "content" not in item:
            raise ValueError(
                f"RECOVERY_INHERITED_MATERIAL_BODY_MISSING: {material_id}"
            )
        body = str(item.get("body", item.get("content", "")))
        declared_hash = str(item.get("content_hash") or "").lower()
        if declared_hash.startswith("sha256:"):
            declared_hash = declared_hash.removeprefix("sha256:")
        if declared_hash and declared_hash != _sha256_text(body):
            raise ValueError(
                f"RECOVERY_INHERITED_MATERIAL_HASH_MISMATCH: {material_id}"
            )
        content = (
            '<inherited_skill_material schema_version="1">\n'
            + _canonical(metadata)
            + "\n---BEGIN EXACT SOURCE BODY---\n"
            + body
            + "\n---END EXACT SOURCE BODY---\n"
            + "</inherited_skill_material>"
        )
    else:
        summary = str(item.get("summary") or "").strip()
        retrieval_ref = str(item.get("retrieval_ref") or "").strip()
        if not summary or not retrieval_ref:
            raise ValueError(
                f"RECOVERY_INHERITED_MATERIAL_RECOVERY_MISSING: {material_id}"
            )
        content = (
            '<inherited_skill_material_state schema_version="1">\n'
            + _canonical(metadata)
            + "\n<summary>\n"
            + summary
            + "\n</summary>\n<missing_details>\n"
            + str(item.get("missing_details") or "")
            + "\n</missing_details>\n"
            + "</inherited_skill_material_state>"
        )
    if item.get("material_kind") == "recovery_evidence":
        content = content.replace("inherited_skill_material", "inherited_recovery_evidence")
    message = HumanMessage(
        id=(
            f"{epoch_id}:inherited:"
            + hashlib.sha256(material_id.encode("utf-8")).hexdigest()[:24]
        ),
        name="inherited_recovery_evidence" if item.get("material_kind") == "recovery_evidence" else "inherited_skill_material",
        content=content,
    )
    descriptor = {
        key: deepcopy(value)
        for key, value in item.items()
        if key not in {"body", "content"}
    }
    descriptor.update({
        "material_id": material_id,
        "residency": residency,
        "selected": bool(item.get("selected", True)),
    })
    if residency == "exact":
        # Internal resolver input only.  It is removed from source-unit
        # provenance and candidate archives, so the immutable Skill body keeps
        # a single source of truth while its original hash remains verifiable.
        descriptor["_source_body"] = body
    return message, descriptor


def _normalize_inherited_materials(
    values: Sequence[Any],
    *,
    epoch_id: str,
) -> tuple[list[HumanMessage], dict[str, dict[str, Any]]]:
    messages: list[HumanMessage] = []
    descriptors: dict[str, dict[str, Any]] = {}
    for ordinal, raw in enumerate(values or (), start=1):
        descriptor: dict[str, Any]
        if isinstance(raw, BaseMessage):
            if not isinstance(raw, HumanMessage):
                raise ValueError("RECOVERY_INHERITED_MATERIAL_ROLE_INVALID")
            message = raw
            descriptor = {
                "material_id": _message_id(
                    message, fallback=f"material-{ordinal:04d}"
                ),
                "residency": "exact",
                "selected": True,
            }
        elif isinstance(raw, Mapping) and isinstance(
            raw.get("message"), BaseMessage
        ):
            message = raw["message"]
            if not isinstance(message, HumanMessage):
                raise ValueError("RECOVERY_INHERITED_MATERIAL_ROLE_INVALID")
            descriptor = {
                key: deepcopy(value)
                for key, value in raw.items()
                if key != "message"
            }
            descriptor.setdefault(
                "material_id",
                _message_id(message, fallback=f"material-{ordinal:04d}"),
            )
            descriptor.setdefault("residency", "exact")
            descriptor.setdefault("selected", True)
        elif isinstance(raw, Mapping):
            message, descriptor = _render_material_message(
                raw, epoch_id=epoch_id, ordinal=ordinal
            )
        else:
            raise ValueError(
                "RECOVERY_INHERITED_MATERIAL_INVALID: "
                + type(raw).__name__
            )
        message_id = _message_id(
            message, fallback=f"{epoch_id}:inherited:{ordinal:04d}"
        )
        if not getattr(message, "id", None):
            message = message.model_copy(update={"id": message_id})
        if message_id in descriptors:
            raise ValueError(
                f"RECOVERY_INHERITED_MATERIAL_DUPLICATE: {message_id}"
            )
        messages.append(message)
        descriptors[message_id] = descriptor
    return messages, descriptors


def _protocol_groups(messages: Sequence[BaseMessage]) -> dict[str, str]:
    """Bind an assistant tool request and all matching results atomically."""

    by_call_id: dict[str, str] = {}
    by_message_id: dict[str, str] = {}
    for ordinal, message in enumerate(messages, start=1):
        if not isinstance(message, AIMessage):
            continue
        calls = list(getattr(message, "tool_calls", None) or ())
        if not calls:
            continue
        message_id = _message_id(message, fallback=f"assistant-{ordinal}")
        group_id = "recovery-tool-protocol:" + _sha256_text(message_id)[:24]
        by_message_id[message_id] = group_id
        for call in calls:
            if isinstance(call, Mapping) and str(call.get("id") or ""):
                by_call_id[str(call["id"])] = group_id
    for ordinal, message in enumerate(messages, start=1):
        call_id = str(getattr(message, "tool_call_id", None) or "")
        if call_id and call_id in by_call_id:
            by_message_id[_message_id(
                message, fallback=f"tool-{ordinal}"
            )] = by_call_id[call_id]
    return by_message_id


def _build_compaction_request(
    *,
    seed: Any,
    episode: Mapping[str, Any],
    inherited_messages: Sequence[BaseMessage],
    local_messages: Sequence[BaseMessage],
    inherited_descriptors: Mapping[str, Mapping[str, Any]],
    source_epoch_id: str,
    history_version: int,
    selected_revision: int,
    policy: ContextBudgetPolicy,
    estimator: TokenEstimator,
) -> tuple[CompactionRequest, dict[str, str], dict[str, BaseMessage]]:
    units: list[SourceUnit] = []
    bodies: dict[str, str] = {}
    originals: dict[str, BaseMessage] = {}
    protocol = _protocol_groups(local_messages)
    combined = [
        ("inherited", message) for message in inherited_messages
    ] + [("local", message) for message in local_messages]
    for ordinal, (region, message) in enumerate(combined, start=1):
        message_id = _message_id(message, fallback=f"message-{ordinal:04d}")
        payload = provider_visible_payload(message)
        provider_message_body = _canonical(payload)
        unit_id = (
            f"recovery-source-{ordinal:06d}-"
            + _sha256_text(message_id)[:20]
        )
        body_ref = f"{source_epoch_id}:body:{unit_id}"
        descriptor = json.loads(_canonical(dict(
            inherited_descriptors.get(message_id) or {}
        )))
        selected = bool(region == "inherited" and descriptor.get("selected"))
        source_ref = str(
            descriptor.get("source_ref")
            or descriptor.get("material_id")
            or f"recovery-message:{message_id}"
        )
        source_body = descriptor.get("_source_body")
        if selected and isinstance(source_body, str):
            body = source_body
            source_hash = str(descriptor.get("content_hash") or "")
            source_hash = source_hash.removeprefix("sha256:")
            if not source_hash:
                source_hash = _sha256_text(body)
            coverage = deepcopy(dict(descriptor.get("coverage") or {
                "kind": "full"
            }))
        else:
            body = provider_message_body
            source_hash = _sha256_text(body)
            coverage = {"kind": "full"}
        unit = SourceUnit(
            unit_id=unit_id,
            kind=(
                "selected_skill_material"
                if selected else "recovery_local_message"
            ),
            source_ref=source_ref,
            body_ref=body_ref,
            content_hash=source_hash,
            coverage=coverage,
            scope={
                "kind": "recovery_context_epoch",
                "epoch_id": source_epoch_id,
                "history_version": history_version,
                "selected_revision": selected_revision,
                "failure_id": seed.failure_packet.failure_id,
            },
            token_count=estimator.estimate(body).tokens,
            body_length_chars=len(body),
            selected=selected,
            skill_id=str(descriptor.get("skill_id") or ""),
            protocol_group_id=(
                protocol.get(message_id, "") if region == "local" else ""
            ),
            provenance={
                "region": region,
                "message_id": message_id,
                "message_type": str(getattr(message, "type", "")),
                "retrieval_ref": str(
                    descriptor.get("retrieval_ref")
                    or descriptor.get("source_ref")
                    or source_ref
                ),
                "material_descriptor": {
                    key: deepcopy(value)
                    for key, value in descriptor.items()
                    if key not in {
                        "body", "content", "summary", "_source_body"
                    }
                },
            },
        )
        units.append(unit)
        bodies[body_ref] = body
        originals[unit_id] = message
    if not units:
        raise ContextCompactionError(
            "RECOVERY_COMPACTION_SOURCE_EMPTY", source_epoch_id
        )
    manifest = build_source_unit_manifest(
        source_epoch_id=source_epoch_id,
        history_version=history_version,
        selected_revision=selected_revision,
        units=units,
    )
    provider_episode = list(episode.get("_provider_episode") or ())
    fixed = {
        "system": provider_episode[0] if provider_episode else {},
        "tool_schema": recovery_tool_schemas(),
        "frozen_inputs": provider_episode[1] if len(provider_episode) > 1 else {},
        "pending_results": [],
        "provider_wrappers": {},
    }
    request = new_compaction_request(
        manifest,
        budget_policy=policy,
        fixed_components=fixed,
    )
    return request, bodies, originals


def _next_epoch_messages(
    *,
    prepared: Any,
    originals: Mapping[str, BaseMessage],
    source_units: Mapping[str, SourceUnit],
    inherited_descriptors: Mapping[str, Mapping[str, Any]],
) -> tuple[list[HumanMessage], list[BaseMessage], dict[str, dict[str, Any]]]:
    epoch = prepared.epoch
    retained = set(epoch.plan.retain_exact_unit_ids)
    next_inherited: list[HumanMessage] = []
    next_local: list[BaseMessage] = []
    descriptors: dict[str, dict[str, Any]] = {}
    for material in epoch.retained_materials:
        unit = material.unit
        original = originals[unit.unit_id]
        region = str(unit.provenance.get("region") or "local")
        if region == "inherited":
            if not isinstance(original, HumanMessage):
                raise ContextCompactionError(
                    "RECOVERY_RETAINED_INHERITED_ROLE_INVALID", unit.unit_id
                )
            next_inherited.append(original)
            message_id = str(unit.provenance.get("message_id") or "")
            descriptor = deepcopy(dict(
                inherited_descriptors.get(message_id)
                or unit.provenance.get("material_descriptor")
                or {}
            ))
            descriptor.setdefault("selected", bool(unit.selected))
            descriptors[_message_id(original, fallback=unit.unit_id)] = descriptor
        else:
            next_local.append(original)

    for ordinal, recovery in enumerate(epoch.retrieval_state, start=1):
        unit = source_units.get(recovery.source_unit_id)
        if unit is None:
            raise ContextCompactionError(
                "RECOVERY_COMPACTION_SOURCE_UNIT_MISSING",
                recovery.source_unit_id,
            )
        message_id = str(unit.provenance.get("message_id") or "")
        descriptor = deepcopy(dict(
            inherited_descriptors.get(message_id)
            or unit.provenance.get("material_descriptor")
            or {}
        ))
        material_id = str(
            descriptor.get("material_id")
            or f"compacted:{recovery.source_unit_id}"
        )
        raw = {
            **recovery.to_dict(),
            # The compactor validates the provider-visible source unit.  A
            # previously summarized Skill wrapper nevertheless keeps the
            # immutable identity of its original material across epochs.
            "source_ref": str(
                descriptor.get("source_ref") or recovery.source_ref
            ),
            "content_hash": str(
                descriptor.get("content_hash") or recovery.content_hash
            ),
            "coverage": deepcopy(dict(
                descriptor.get("coverage") or recovery.coverage
            )),
        }
        content = (
            '<inherited_skill_material_state schema_version="1">\n'
            + _canonical({
                **raw,
                "material_id": material_id,
                "authority": "lossy_working_memory_only",
            })
            + "\n</inherited_skill_material_state>"
        )
        message = HumanMessage(
            id=f"{epoch.record.epoch_id}:selected-state:{ordinal:04d}",
            name="inherited_skill_material",
            content=content,
        )
        next_inherited.append(message)
        descriptors[str(message.id)] = {
            "material_id": material_id,
            "skill_id": str(descriptor.get("skill_id") or recovery.skill_id),
            "source_ref": raw["source_ref"],
            "content_hash": raw["content_hash"],
            "coverage": deepcopy(raw["coverage"]),
            "retrieval_ref": recovery.retrieval_ref,
            "residency": recovery.residency,
            "summary": recovery.summary,
            "missing_details": recovery.missing_details,
            "compaction_request_id": epoch.record.compaction_request_id,
            "selected": bool(unit.selected),
        }
    if epoch.summary:
        summary_message = HumanMessage(
            id=f"{epoch.record.epoch_id}:working-summary",
            name="recovery_context_summary",
            content=(
                '<recovery_context_summary schema_version="1" '
                'authority="lossy_working_memory_only">\n'
                + str(epoch.summary)
                + "\n</recovery_context_summary>"
            ),
        )
        next_inherited.append(summary_message)
        descriptors[str(summary_message.id)] = {
            "material_id": f"summary:{epoch.record.epoch_id}",
            "residency": "summarized",
            "selected": False,
        }
    if not retained and not next_inherited:
        raise ContextCompactionError(
            "RECOVERY_COMPACTED_CONTEXT_EMPTY", epoch.record.epoch_id
        )
    return next_inherited, next_local, descriptors


def _recovery_identity(seed: Any) -> dict[str, Any]:
    packet = seed.failure_packet
    return {
        "seed_id": str(seed.seed_id),
        "failure_id": str(packet.failure_id),
        "step_id": str(packet.step_id),
        "attempt_ref": str(packet.attempt_ref or ""),
        "action_ref": str(packet.action_ref or ""),
        "source_outcome_ref": str(packet.source_outcome_ref),
    }


def _source_descriptor(
    unit: SourceUnit,
    inherited_descriptors: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    message_id = str(unit.provenance.get("message_id") or "")
    return deepcopy(dict(
        inherited_descriptors.get(message_id)
        or unit.provenance.get("material_descriptor")
        or {}
    ))


def _material_residency_for_epoch(
    *,
    prepared: Any,
    request: CompactionRequest,
    inherited_descriptors: Mapping[str, Mapping[str, Any]],
    prior: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Project selected source dispositions under original material IDs."""

    residency = {
        str(material_id): deepcopy(dict(value))
        for material_id, value in dict(prior or {}).items()
        if str(material_id) and isinstance(value, Mapping)
    }
    retained = set(prepared.epoch.plan.retain_exact_unit_ids)
    retrieval = {
        item.source_unit_id: item
        for item in prepared.epoch.retrieval_state
    }
    for unit in request.source_manifest.units:
        if not unit.selected:
            continue
        descriptor = _source_descriptor(unit, inherited_descriptors)
        material_id = str(
            descriptor.get("material_id") or unit.source_ref
        ).strip()
        if not material_id:
            raise ContextCompactionError(
                "RECOVERY_MATERIAL_ID_MISSING", unit.unit_id
            )
        content_hash = str(
            descriptor.get("content_hash") or unit.content_hash
        )
        coverage = deepcopy(dict(
            descriptor.get("coverage") or unit.coverage
        ))
        recovery = retrieval.get(unit.unit_id)
        if recovery is not None:
            residency[material_id] = {
                "residency": recovery.residency,
                "content_hash": content_hash,
                "coverage": coverage,
                "summary": recovery.summary,
                "missing_details": recovery.missing_details,
                "retrieval_ref": recovery.retrieval_ref,
                "compaction_request_id": (
                    prepared.epoch.record.compaction_request_id
                ),
            }
            continue
        if unit.unit_id not in retained:
            raise ContextCompactionError(
                "RECOVERY_SELECTED_MATERIAL_DISPOSITION_MISSING",
                unit.unit_id,
            )
        prior_row = deepcopy(dict(residency.get(material_id) or {}))
        prior_residency = str(
            descriptor.get("residency")
            or prior_row.get("residency")
            or "exact"
        ).strip().lower()
        if prior_residency == "exact":
            residency[material_id] = {
                "residency": "exact",
                "content_hash": content_hash,
                "coverage": coverage,
            }
            continue
        if prior_residency not in {"summarized", "externalized"}:
            raise ContextCompactionError(
                "RECOVERY_MATERIAL_RESIDENCY_INVALID", prior_residency
            )
        summary = str(
            descriptor.get("summary") or prior_row.get("summary") or ""
        ).strip()
        retrieval_ref = str(
            descriptor.get("retrieval_ref")
            or prior_row.get("retrieval_ref")
            or ""
        ).strip()
        compaction_request_id = str(
            descriptor.get("compaction_request_id")
            or prior_row.get("compaction_request_id")
            or ""
        ).strip()
        if not summary or not retrieval_ref or not compaction_request_id:
            raise ContextCompactionError(
                "RECOVERY_MATERIAL_RECOVERY_STATE_INVALID", material_id
            )
        residency[material_id] = {
            "residency": prior_residency,
            "content_hash": content_hash,
            "coverage": coverage,
            "summary": summary,
            "missing_details": str(
                descriptor.get("missing_details")
                or prior_row.get("missing_details")
                or ""
            ),
            "retrieval_ref": retrieval_ref,
            "compaction_request_id": compaction_request_id,
        }
    return residency


def _message_from_provider_payload(
    payload: Mapping[str, Any],
    *,
    message_id: str,
) -> BaseMessage:
    value = deepcopy(dict(payload))
    kind = str(value.pop("type", "") or value.pop("role", "")).lower()
    content = value.pop("content", "")
    name = str(value.pop("name", "") or "") or None
    if kind == "system":
        return SystemMessage(id=message_id or None, content=content, name=name)
    if kind in {"ai", "assistant"}:
        return AIMessage(
            id=message_id or None,
            content=content,
            name=name,
            tool_calls=deepcopy(value.pop("tool_calls", [])),
        )
    if kind == "tool":
        tool_call_id = str(value.pop("tool_call_id", "") or "")
        if not tool_call_id:
            raise ContextCompactionError(
                "RECOVERY_RESTORED_TOOL_CALL_ID_MISSING", message_id
            )
        return ToolMessage(
            id=message_id or None,
            content=content,
            name=name,
            tool_call_id=tool_call_id,
        )
    if kind not in {"human", "user"}:
        raise ContextCompactionError(
            "RECOVERY_RESTORED_MESSAGE_TYPE_INVALID", kind or "<missing>"
        )
    return HumanMessage(id=message_id or None, content=content, name=name)


def _candidate_provider_baseline_manifest(
    *,
    prepared: Any,
    inherited_messages: Sequence[BaseMessage],
    local_messages: Sequence[BaseMessage],
    inherited_descriptors: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    retained_by_message_id = {
        str(item.unit.provenance.get("message_id") or ""): item.unit.unit_id
        for item in prepared.epoch.retained_materials
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for region, messages in (
        ("inherited", inherited_messages),
        ("local", local_messages),
    ):
        for ordinal, message in enumerate(messages, start=1):
            message_id = _message_id(
                message, fallback=f"{region}-{ordinal:04d}"
            )
            if message_id in seen:
                raise ContextCompactionError(
                    "RECOVERY_CANDIDATE_MESSAGE_DUPLICATE", message_id
                )
            seen.add(message_id)
            descriptor = {
                key: deepcopy(value)
                for key, value in dict(
                    inherited_descriptors.get(message_id) or {}
                ).items()
                if key not in {"body", "content", "_source_body"}
            }
            source_unit_id = retained_by_message_id.get(message_id, "")
            row: dict[str, Any] = {
                "region": region,
                "message_id": message_id,
                "material_descriptor": descriptor,
            }
            if source_unit_id:
                row.update({
                    "kind": "retained_source_unit",
                    "source_unit_id": source_unit_id,
                })
            else:
                if (
                    bool(descriptor.get("selected"))
                    and str(descriptor.get("residency") or "exact")
                    == "exact"
                ):
                    raise ContextCompactionError(
                        "RECOVERY_EXACT_SOURCE_INLINE_FORBIDDEN", message_id
                    )
                row.update({
                    "kind": "inline_lossy_message",
                    "provider_payload": provider_visible_payload(message),
                })
            rows.append(row)
    return rows


def _business_resume_state(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = dict(value or {})
    scalar_fields = (
        "turn_count",
        "tool_call_count",
        "no_progress_rounds",
        "observation_chars",
        "transport_attempt_count",
    )
    result = {
        key: int(source.get(key) or 0)
        for key in scalar_fields
    }
    result.update({
        "tool_request_counts": deepcopy(dict(
            source.get("tool_request_counts") or {}
        )),
        "progress_signatures": list(
            source.get("progress_signatures") or ()
        ),
        "inspected_refs": list(source.get("inspected_refs") or ()),
        "soft_limit_reached": bool(source.get("soft_limit_reached")),
        "context_budget_checks": deepcopy(list(
            source.get("context_budget_checks") or ()
        )),
        "provider_request_audit": deepcopy(list(
            source.get("provider_request_audit") or ()
        )),
        "provider_usage": deepcopy(list(
            source.get("provider_usage") or ()
        )),
        "compaction_bypass_remaining": 0,
        "compaction_failures": deepcopy(list(
            source.get("compaction_failures") or ()
        )),
        # Prior observations remain authoritative in the linked source
        # archive.  The active epoch does not duplicate their result bodies.
        "observation_receipts": [],
    })
    return result


class Schema2RecoveryNode:
    """Own one fresh phase-local episode and admit only its final directive."""

    def __init__(
        self,
        *,
        data_path: str,
        model: Any,
        phase_store: PhaseArchiveStore | None = None,
        skill_service: SkillAccessService | None = None,
        evidence_reader: RecoveryEvidenceReader | None = None,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
        repair_limit: int = 2,
        loop_config: RecoveryAgentLoopConfig | None = None,
        inherited_materials_factory: InheritedMaterialsFactory | None = None,
        compactor: Any | None = None,
        context_budget_policy: ContextBudgetPolicy | None = None,
        token_estimator: TokenEstimator | None = None,
        context_compaction_enabled: bool = True,
        provider_invoke: Callable[..., Any] | None = None,
        event_sink: Callable[[str, Mapping[str, Any]], Any] | None = None,
        runtime_recovery: Callable | None = None,
    ) -> None:
        self.data_path = str(data_path)
        self.model = model
        self.phase_store = phase_store or PhaseArchiveStore(self.data_path)
        self.skill_service = skill_service
        self.evidence_reader = evidence_reader or RecoveryEvidenceReader(self.data_path)
        self.loader = loader
        self.saver = saver
        self.runtime = RecoveryRuntime(
            data_path=self.data_path,
            loader=loader,
            saver=saver,
            repair_limit=repair_limit,
        )
        self.loop_config = loop_config or RecoveryAgentLoopConfig()
        self.runtime_recovery = runtime_recovery
        self.inherited_materials_factory = inherited_materials_factory
        self.compactor = compactor
        self.context_budget_policy = context_budget_policy or ContextBudgetPolicy()
        self.token_estimator = token_estimator or ReferenceTokenEstimator()
        self.context_compaction_enabled = bool(context_compaction_enabled)
        self.provider_invoke = provider_invoke
        self.event_sink = event_sink

    async def _emit_event(
        self,
        event: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self.event_sink is None:
            return
        try:
            result = self.event_sink(str(event), deepcopy(dict(payload or {})))
            if inspect.isawaitable(result):
                await result
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            raise RecoveryAgentError(
                "RECOVERY_EVENT_PERSISTENCE_FAILED",
                f"{event}: {type(exc).__name__}",
            ) from exc

    @staticmethod
    def _persisted_activation_matches(
        durable: Mapping[str, Any],
        *,
        expected_generation: int,
        active_record: Mapping[str, Any],
        material_residency: Mapping[str, Any],
    ) -> bool:
        if int(durable.get("state_generation") or 0) != expected_generation:
            return False
        task_state = dict(durable.get("task_state") or {})
        if dict(task_state.get("recovery_context_compaction") or {}) != dict(
            active_record
        ):
            return False
        by_phase = dict(task_state.get("skill_material_residency") or {})
        return (
            dict(by_phase.get("recovery") or {})
            == dict(material_residency)
            and dict(by_phase.get("latest") or {})
            == dict(material_residency)
        )

    @staticmethod
    def _assert_recovery_identity(
        parent_state: Mapping[str, Any],
        durable: Mapping[str, Any],
        seed: Any,
    ) -> None:
        try:
            projection = project_recovery_seed(parent_state, durable)
        except Exception as exc:
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_IDENTITY_STALE",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        if projection is None:
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_IDENTITY_STALE",
                "causal failure projection is no longer available",
            )
        expected = _recovery_identity(seed)
        observed = _recovery_identity(projection.recovery_seed)
        if observed != expected:
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_IDENTITY_STALE",
                f"expected={_canonical(expected)}; observed={_canonical(observed)}",
            )

    def _persist_context_epoch_activation(
        self,
        *,
        task_id: str,
        parent_state: Mapping[str, Any],
        durable: Mapping[str, Any],
        seed: Any,
        prepared: Any,
        candidate_epoch_archive_ref: str,
        source_archive_ref: str,
        source_epoch_id: str,
        history_version: int,
        material_residency: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """CAS-persist one archived Recovery epoch before local activation."""

        expected_generation = int(durable.get("state_generation") or 0)
        selected_revision = int(prepared.epoch.record.selected_revision)
        identity = _recovery_identity(seed)
        next_pointer = {
            "state_generation": expected_generation + 1,
            "epoch_id": str(prepared.epoch.record.epoch_id),
            "epoch_archive_ref": str(candidate_epoch_archive_ref),
            # The source snapshot is history_version N; activating its
            # successor advances the durable Recovery history once.
            "history_version": int(history_version) + 1,
            "selected_revision": selected_revision,
        }
        active_record = {
            "schema_version": 1,
            "phase": "recovery",
            "identity": identity,
            "active_pointer": next_pointer,
            "predecessor_epoch_id": str(source_epoch_id),
            "source_archive_ref": str(source_archive_ref),
            "candidate_epoch_archive_ref": str(candidate_epoch_archive_ref),
            "source_manifest_hash": str(
                prepared.epoch.record.source_manifest_hash
            ),
            "compaction_request_id": str(
                prepared.epoch.record.compaction_request_id
            ),
        }
        record = prepared.epoch.record
        if (
            str(record.source_epoch_id) != str(source_epoch_id)
            or int(record.history_version) != int(history_version)
            or int(record.selected_revision) != selected_revision
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_EPOCH_CANDIDATE_STALE",
                str(record.epoch_id),
            )

        current = self.loader(self.data_path, task_id)
        self._assert_recovery_identity(parent_state, current, seed)
        # A saver may commit successfully and then lose its acknowledgement.
        # Exact replay of that same archived candidate is already complete and
        # must not consume another PlanState generation.
        if self._persisted_activation_matches(
            current,
            expected_generation=expected_generation + 1,
            active_record=active_record,
            material_residency=material_residency,
        ):
            return current, True
        current_generation = int(current.get("state_generation") or 0)
        if current_generation != expected_generation:
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_EPOCH_CAS_CONFLICT",
                (
                    f"expected_generation={expected_generation}; "
                    f"current_generation={current_generation}"
                ),
            )

        current_task = dict(current.get("task_state") or {})
        existing = dict(
            current_task.get("recovery_context_compaction") or {}
        )
        if existing and dict(existing.get("identity") or {}) == identity:
            pointer = dict(existing.get("active_pointer") or {})
            observed_source = {
                "state_generation": int(
                    pointer.get("state_generation") or 0
                ),
                "epoch_id": str(pointer.get("epoch_id") or ""),
                "history_version": int(
                    pointer.get("history_version") or 0
                ),
                "selected_revision": int(
                    pointer.get("selected_revision") or 0
                ),
            }
            expected_source = {
                "state_generation": expected_generation,
                "epoch_id": str(source_epoch_id),
                "history_version": int(history_version),
                "selected_revision": selected_revision,
            }
            if observed_source != expected_source:
                raise ContextEpochCASConflict(
                    "RECOVERY_CONTEXT_EPOCH_CAS_CONFLICT",
                    (
                        f"expected={_canonical(expected_source)}; "
                        f"observed={_canonical(observed_source)}"
                    ),
                )
        elif int(history_version) != 0:
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_EPOCH_CAS_CONFLICT",
                "non-initial source does not match the durable Recovery epoch",
            )

        candidate = deepcopy(current)
        candidate_task = dict(candidate.get("task_state") or {})
        candidate_task["recovery_context_compaction"] = deepcopy(
            active_record
        )
        by_phase = dict(
            candidate_task.get("skill_material_residency") or {}
        )
        residency = deepcopy(dict(material_residency))
        by_phase["recovery"] = residency
        by_phase["latest"] = deepcopy(residency)
        candidate_task["skill_material_residency"] = by_phase
        candidate["task_state"] = candidate_task
        try:
            self.saver(self.data_path, task_id, candidate)
        except StateCASConflict as exc:
            refreshed = self.loader(self.data_path, task_id)
            self._assert_recovery_identity(parent_state, refreshed, seed)
            if self._persisted_activation_matches(
                refreshed,
                expected_generation=expected_generation + 1,
                active_record=active_record,
                material_residency=material_residency,
            ):
                return refreshed, True
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_EPOCH_CAS_CONFLICT",
                str(exc),
            ) from exc

        refreshed = self.loader(self.data_path, task_id)
        self._assert_recovery_identity(parent_state, refreshed, seed)
        if not self._persisted_activation_matches(
            refreshed,
            expected_generation=expected_generation + 1,
            active_record=active_record,
            material_residency=material_residency,
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_CONTEXT_EPOCH_COMMIT_UNACKNOWLEDGED",
                str(record.epoch_id),
            )
        return refreshed, False

    def _initial_inherited_materials(
        self,
        *,
        seed: Any,
        durable: Mapping[str, Any],
    ) -> Sequence[Any]:
        if self.inherited_materials_factory is not None:
            result = self.inherited_materials_factory(seed, durable)
            if inspect.isawaitable(result):
                raise ValueError(
                    "RECOVERY_INHERITED_MATERIAL_FACTORY_ASYNC_UNSUPPORTED"
                )
            return list(result or ())
        access = deepcopy(dict(
            (durable.get("task_state") or {}).get("skill_access_state")
            or durable.get("skill_access_state")
            or {}
        ))
        residency_by_phase = dict(
            (durable.get("task_state") or {}).get(
                "skill_material_residency"
            )
            or {}
        )
        residency = (
            residency_by_phase.get("latest")
            if isinstance(residency_by_phase.get("latest"), Mapping)
            else residency_by_phase.get("recovery")
            if isinstance(residency_by_phase.get("recovery"), Mapping)
            else {}
        )
        materials = list(selected_skill_materials_for_carry(
            access, current_step_id=seed.failure_packet.step_id, residency_state=residency,
        ))
        from agent_core.workflow.inspection_evidence import InspectionObjectReader, inspection_scope, INSPECTION_BACKEND
        reader = InspectionObjectReader(self.data_path, scope=inspection_scope(seed.failure_packet, seed.seed_id))
        index = dict((durable.get("task_state") or {}).get("inspection_ref_registry") or {})
        # Same scoped reader as evidence.read. Source identities and hashes are
        # retained in the provider message and the existing phase archive.
        remaining = 96_000
        for ref in seed.failure_packet.inspection_refs:
            entry = index.get(ref) or {}
            kind = (entry.get("scope") or {}).get("object_kind")
            if entry.get("backend") != INSPECTION_BACKEND or kind not in {
                "execution", "script", "action", "task_context", "plan", "completion_report", "parser_receipt", "diagnostic",
            }:
                continue
            item = {"material_id": ref, "source_ref": ref, "retrieval_ref": ref,
                    "material_kind": "recovery_evidence", "object_kind": kind, "selected": True,
                    "source_digest": entry.get("digest"), "coverage": "complete immutable object"}
            try:
                body = _canonical(reader.read_body(entry, entry.get("locator") or {}))
                if len(body) > remaining:
                    raise ValueError("RECOVERY_INHERITED_BODY_CAPACITY; retrieve with evidence.read")
                remaining -= len(body)
                item.update(residency="exact", body=body, content_hash=_sha256_text(body))
            except (ValueError, OSError) as exc:
                item.update(residency="externalized", summary="Body not resident; explicit scoped reader remains authoritative.",
                            missing_details=str(getattr(exc, "code", "") or type(exc).__name__) + ": " + str(exc)[:300])
            materials.append(item)
        return materials

    def _restore_active_context(
        self,
        *,
        parent_state: Mapping[str, Any],
        durable: Mapping[str, Any],
        seed: Any,
    ) -> dict[str, Any] | None:
        """Rebuild a post-CAS active epoch without replaying its old tools."""

        task_state = dict(durable.get("task_state") or {})
        raw_active = task_state.get("recovery_context_compaction")
        if not isinstance(raw_active, Mapping) or not raw_active:
            return None
        active = deepcopy(dict(raw_active))
        expected_identity = _recovery_identity(seed)
        if dict(active.get("identity") or {}) != expected_identity:
            # A completed or superseded failure may leave an archived pointer;
            # a different causal Seed starts a genuinely new Recovery episode.
            return None
        self._assert_recovery_identity(parent_state, durable, seed)
        pointer = dict(active.get("active_pointer") or {})
        current_generation = int(durable.get("state_generation") or 0)
        activation_generation = int(pointer.get("state_generation") or 0)
        if activation_generation != current_generation:
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_GENERATION_STALE",
                (
                    f"activation_generation={activation_generation}; "
                    f"current_generation={current_generation}"
                ),
            )
        epoch_id = str(pointer.get("epoch_id") or "")
        archive_ref = str(pointer.get("epoch_archive_ref") or "")
        history_version = int(pointer.get("history_version") or 0)
        selected_revision = int(pointer.get("selected_revision") or 0)
        if (
            not epoch_id
            or not archive_ref
            or history_version < 1
            or selected_revision != int(seed.task_spec_ref.task_revision)
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_POINTER_INVALID", epoch_id
            )
        by_phase = dict(task_state.get("skill_material_residency") or {})
        recovery_residency = by_phase.get("recovery")
        latest_residency = by_phase.get("latest")
        if (
            not isinstance(recovery_residency, Mapping)
            or not isinstance(latest_residency, Mapping)
            or dict(recovery_residency) != dict(latest_residency)
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_RESIDENCY_STALE", epoch_id
            )

        candidate = self.phase_store.read(archive_ref)
        # PhaseArchiveRecord recursively freezes JSON mappings.  Restore from
        # its public thawed representation so nested descriptors can be copied
        # without leaking MappingProxyType objects into live loop state.
        candidate_data = candidate.to_dict()
        candidate_metadata = dict(candidate_data.get("metadata") or {})
        if (
            candidate.phase != "recovery-context-epoch"
            or candidate.task_id != seed.failure_packet.task_id
            or str(candidate_metadata.get("active_context_epoch_id") or "")
            != epoch_id
            or str(candidate_metadata.get("source_failure_id") or "")
            != seed.failure_packet.failure_id
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_ARCHIVE_INVALID", archive_ref
            )
        summary = dict(candidate_data.get("summary") or {})
        prepared = dict(summary.get("prepared_context_epoch") or {})
        record = dict(prepared.get("record") or {})
        if (
            str(record.get("epoch_id") or "") != epoch_id
            or str(record.get("compaction_request_id") or "")
            != str(active.get("compaction_request_id") or "")
            or str(record.get("source_manifest_hash") or "")
            != str(active.get("source_manifest_hash") or "")
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_RECORD_INVALID", epoch_id
            )
        source_archive_ref = str(
            summary.get("source_archive_ref")
            or active.get("source_archive_ref")
            or ""
        )
        if (
            not source_archive_ref
            or source_archive_ref
            != str(active.get("source_archive_ref") or "")
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_SOURCE_REF_INVALID", epoch_id
            )
        source = self.phase_store.read(source_archive_ref)
        source_data = source.to_dict()
        source_summary = dict(source_data.get("summary") or {})
        source_metadata = dict(source_data.get("metadata") or {})
        if (
            source.phase != "recovery"
            or source.task_id != seed.failure_packet.task_id
            or str(source_summary.get("failure_id") or "")
            != seed.failure_packet.failure_id
            or str(source_summary.get("terminal_state") or "")
            != "compaction_boundary"
            or str(source_metadata.get("prompt_version") or "")
            != RECOVERY_SYSTEM_PROMPT_VERSION
            or str(source_metadata.get("frozen_seed_digest") or "")
            != recovery_seed_digest(seed)
        ):
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_SOURCE_ARCHIVE_INVALID",
                source_archive_ref,
            )

        retained_rows = {
            str(item.get("unit_id") or ""): dict(item)
            for item in prepared.get("retained_materials") or ()
            if isinstance(item, Mapping) and str(item.get("unit_id") or "")
        }
        baseline = list(summary.get("provider_baseline_manifest") or ())
        if not baseline:
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_BASELINE_MISSING", epoch_id
            )
        source_transcript = list(source_data.get("transcript") or ())
        inherited: list[HumanMessage] = []
        local: list[BaseMessage] = []
        descriptors: dict[str, dict[str, Any]] = {}
        consumed_retained: set[str] = set()
        seen_messages: set[str] = set()
        for raw in baseline:
            if not isinstance(raw, Mapping):
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_BASELINE_INVALID", epoch_id
                )
            row = dict(raw)
            region = str(row.get("region") or "")
            message_id = str(row.get("message_id") or "")
            kind = str(row.get("kind") or "")
            if (
                region not in {"inherited", "local"}
                or not message_id
                or message_id in seen_messages
            ):
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_BASELINE_INVALID", message_id
                )
            seen_messages.add(message_id)
            if kind == "retained_source_unit":
                unit_id = str(row.get("source_unit_id") or "")
                unit = retained_rows.get(unit_id)
                if unit is None or unit_id in consumed_retained:
                    raise ContextEpochCASConflict(
                        "RECOVERY_ACTIVE_CONTEXT_RETAINED_REF_INVALID",
                        unit_id,
                    )
                consumed_retained.add(unit_id)
                provenance = dict(unit.get("provenance") or {})
                if (
                    str(provenance.get("region") or "") != region
                    or str(provenance.get("message_id") or "")
                    != message_id
                ):
                    raise ContextEpochCASConflict(
                        "RECOVERY_ACTIVE_CONTEXT_RETAINED_REF_INVALID",
                        unit_id,
                    )
                parts = unit_id.split("-", 3)
                if (
                    len(parts) != 4
                    or parts[:2] != ["recovery", "source"]
                    or not parts[2].isdigit()
                ):
                    raise ContextEpochCASConflict(
                        "RECOVERY_ACTIVE_CONTEXT_RETAINED_REF_INVALID",
                        unit_id,
                    )
                transcript_index = int(parts[2]) + 1
                if transcript_index >= len(source_transcript):
                    raise ContextEpochCASConflict(
                        "RECOVERY_ACTIVE_CONTEXT_SOURCE_TRANSCRIPT_INVALID",
                        unit_id,
                    )
                payload = source_transcript[transcript_index]
            elif kind == "inline_lossy_message":
                payload = row.get("provider_payload")
            else:
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_BASELINE_KIND_INVALID", kind
                )
            if not isinstance(payload, Mapping):
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_MESSAGE_INVALID", message_id
                )
            try:
                message = _message_from_provider_payload(
                    payload, message_id=message_id
                )
            except ContextCompactionError as exc:
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_MESSAGE_INVALID", exc.code
                ) from exc
            if provider_visible_payload(message) != dict(payload):
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_MESSAGE_DRIFT", message_id
                )
            if region == "inherited":
                if not isinstance(message, HumanMessage):
                    raise ContextEpochCASConflict(
                        "RECOVERY_ACTIVE_CONTEXT_INHERITED_ROLE_INVALID",
                        message_id,
                    )
                inherited.append(message)
                descriptors[message_id] = deepcopy(dict(
                    row.get("material_descriptor") or {}
                ))
            else:
                local.append(message)
        if consumed_retained != set(retained_rows):
            raise ContextEpochCASConflict(
                "RECOVERY_ACTIVE_CONTEXT_RETAINED_REF_INCOMPLETE", epoch_id
            )

        # Recover internal exact-body handles from the immutable Skill store;
        # they are never copied into the candidate archive or PlanState.
        fresh_messages, fresh_descriptors = _normalize_inherited_materials(
            self._initial_inherited_materials(seed=seed, durable=durable),
            epoch_id=epoch_id,
        )
        del fresh_messages
        fresh_by_material = {
            str(item.get("material_id") or ""): item
            for item in fresh_descriptors.values()
            if str(item.get("material_id") or "")
        }
        for message_id, descriptor in list(descriptors.items()):
            material_id = str(descriptor.get("material_id") or "")
            fresh = fresh_by_material.get(material_id)
            if fresh is None:
                continue
            if (
                descriptor.get("content_hash")
                and fresh.get("content_hash")
                and str(descriptor.get("content_hash"))
                != str(fresh.get("content_hash"))
            ):
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_MATERIAL_IDENTITY_DRIFT",
                    material_id,
                )
            merged = deepcopy(dict(fresh))
            merged.update(deepcopy(descriptor))
            if "_source_body" in fresh:
                merged["_source_body"] = fresh["_source_body"]
            descriptors[message_id] = merged

        resume_state = _business_resume_state(
            summary.get("business_resume_state")
            if isinstance(summary.get("business_resume_state"), Mapping)
            else {}
        )
        counters = dict(source_summary.get("business_budget_counters") or {})
        for field in (
            "turn_count",
            "tool_call_count",
            "no_progress_rounds",
            "transport_attempt_count",
        ):
            if int(resume_state.get(field) or 0) != int(
                counters.get(field) or 0
            ):
                raise ContextEpochCASConflict(
                    "RECOVERY_ACTIVE_CONTEXT_COUNTER_DRIFT", field
                )
        return {
            "context_epoch_id": epoch_id,
            "inherited_messages": inherited,
            "local_messages": local,
            "inherited_descriptors": descriptors,
            "budget_state": resume_state,
            "linked_previous_archive_ref": archive_ref,
            "overflow_epoch": int(candidate_metadata.get("overflow_epoch") or 0),
        }

    async def _invoke_compactor(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        observability: list[dict[str, Any]],
        task_id: str,
        seed_id: str,
        failure_id: str,
        source_epoch_id: str,
        compaction_request_id: str,
    ) -> Any:
        target = self.compactor if self.compactor is not None else self.model
        call = getattr(target, "ainvoke", None)
        if call is None and callable(target):
            call = target
        if call is None:
            raise ContextCompactionError("RECOVERY_COMPACTOR_INVALID")
        record: dict[str, Any] = {
            "request_ordinal": len(observability) + 1,
            "wire_fingerprint": _sha256_text(_canonical(list(messages))),
            "provider_message_count": len(messages),
        }
        event_context = {
            "task_id": str(task_id),
            "seed_id": str(seed_id),
            "failure_id": str(failure_id),
            "source_epoch_id": str(source_epoch_id),
            "compaction_request_id": str(compaction_request_id),
            "request_ordinal": record["request_ordinal"],
            "wire_fingerprint": record["wire_fingerprint"],
        }
        event_prefix = (
            f"recovery:{seed_id}:{source_epoch_id}:compaction:"
            f"{compaction_request_id}:{record['request_ordinal']}"
        )
        await self._emit_event(
            "recovery_compaction_started",
            {
                **event_context,
                "event_id": f"{event_prefix}:started",
            },
        )
        try:
            call_kwargs: dict[str, Any] = {}
            if self.compactor is not None:
                try:
                    signature = inspect.signature(call)
                    accepts_kwargs = any(
                        item.kind is inspect.Parameter.VAR_KEYWORD
                        for item in signature.parameters.values()
                    )
                    if accepts_kwargs or {
                        "task_id", "phase", "source_epoch_id",
                        "compaction_request_id",
                    }.issubset(signature.parameters):
                        call_kwargs = {
                            "task_id": str(task_id),
                            "phase": "recovery",
                            "source_epoch_id": str(source_epoch_id),
                            "compaction_request_id": str(compaction_request_id),
                        }
                except (TypeError, ValueError):
                    call_kwargs = {}
            response = call(messages, **call_kwargs)
            if inspect.isawaitable(response):
                response = await response
        except (asyncio.CancelledError, KeyboardInterrupt):
            record["status"] = "cancelled"
            observability.append(record)
            await self._emit_event(
                "recovery_compaction_cancelled",
                {
                    **event_context,
                    "event_id": f"{event_prefix}:cancelled",
                    "status": "cancelled",
                },
            )
            raise
        except Exception as exc:
            record.update({
                "status": "transport_failed",
                "error_type": type(exc).__name__,
            })
            observability.append(record)
            await self._emit_event(
                "recovery_compaction_failed",
                {
                    **event_context,
                    "event_id": f"{event_prefix}:failed",
                    "status": "transport_failed",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        content = getattr(response, "content", response)
        candidate = content
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                candidate = None
        if isinstance(candidate, Mapping) and (
            "directive_id" in candidate or "action" in candidate
        ):
            record["status"] = "rejected_directive_shaped_output"
            observability.append(record)
            await self._emit_event(
                "recovery_compaction_failed",
                {
                    **event_context,
                    "event_id": f"{event_prefix}:rejected",
                    "status": "rejected_directive_shaped_output",
                    "error_code": "COMPACTOR_DIRECTIVE_FORBIDDEN",
                },
            )
            raise ContextCompactionError(
                "COMPACTOR_DIRECTIVE_FORBIDDEN",
                "Recovery directives are not a compactor output type.",
            )
        usage = json.loads(_canonical(dict(
            getattr(response, "usage_metadata", None) or {}
        )))
        response_metadata = json.loads(_canonical(dict(
            getattr(response, "response_metadata", None) or {}
        )))
        token_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        record["status"] = "completed"
        if usage:
            record["usage_metadata"] = usage
        if isinstance(token_usage, Mapping):
            record["provider_token_usage"] = deepcopy(dict(token_usage))
        observability.append(record)
        await self._emit_event(
            "recovery_compaction_completed",
            {
                **event_context,
                "event_id": f"{event_prefix}:completed",
                "status": "completed",
                "usage_metadata": deepcopy(usage),
                "provider_token_usage": (
                    deepcopy(dict(token_usage))
                    if isinstance(token_usage, Mapping)
                    else {}
                ),
            },
        )
        return response

    @staticmethod
    def _internal(
        *,
        task_id: str,
        durable: Mapping[str, Any],
        code: str,
        detail: str = "",
        recovery_archive_ref: str = "",
    ) -> dict[str, Any]:
        scope = {
            "task_id": task_id,
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "step_id": str(durable.get("current_step_id") or ""),
            "state_generation": int(durable.get("state_generation") or 0),
        }
        return {"recovery_outcome": RecoveryOutcome(
            schema_version=1,
            type="internal_protocol_error",
            summary="Recovery phase failed closed before directive admission.",
            scope=scope,
            payload={
                "error_code": str(code),
                "source_scope": scope,
                "identity_conflict": {
                    "detail": str(detail)[:800],
                    **(
                        {"recovery_archive_ref": recovery_archive_ref}
                        if recovery_archive_ref else {}
                    ),
                },
            },
        ).to_dict()}

    def _inspection_stop_result(
        self, *, projection: Any, durable: Mapping[str, Any],
        episode: Mapping[str, Any], archive_ref: str = "",
    ) -> dict[str, Any] | None:
        """Freeze terminal episode facts; the outer owns durable/report/summary I/O."""
        if projection is None:
            return None
        from agent_core.workflow.inspection_stop import (
            build_inspection_stop, finalize_inspection_stop, inspection_stop_outcome,
        )
        stop = build_inspection_stop(
            seed=projection.recovery_seed, durable=durable,
            source=projection.source, episode=episode, archive_ref=archive_ref,
        )
        # Classify the terminal event itself. Policy exhaustion is unresolved,
        # independent of which legal producer initiated the investigation.
        if (episode.get("terminal_error") and str(episode["terminal_error"].get("code") or "") not in {
            "RECOVERY_NO_PROGRESS_LIMIT", "RECOVERY_TOOL_CALL_LIMIT", "RECOVERY_MODEL_TURN_LIMIT",
            "RECOVERY_BUDGET_EXHAUSTED", "RECOVERY_NO_PROGRESS",
        } and projection.failure_packet.failure_type != "inspection_required"):
            result = self._internal(task_id=stop["scope"]["task_id"], durable=durable,
                code=stop["stop_reason"]["error_code"], detail=str(episode["terminal_error"].get("detail") or ""),
                recovery_archive_ref=archive_ref)
            result["recovery_outcome"]["payload"]["inspection_stop"] = stop
            return result
        return {
            "recovery_outcome": inspection_stop_outcome(stop).to_dict(),
            "messages": [AIMessage(content=str(stop.get("explanation") or ""))],
        }

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("SCHEMA2_RECOVERY_TASK_ID_REQUIRED")
        if state.get("pending_runtime_evidence_request"):
            if self.runtime_recovery is None:
                return self._internal(task_id=task_id, durable={},
                                      code="RUNTIME_RECOVERY_HANDLER_MISSING")
            return await self.runtime_recovery(state)
        durable = self.loader(self.data_path, task_id)
        from agent_core.workflow.inspection_stop import (
            current_inspection_stop, finalize_inspection_stop, inspection_stop_outcome,
        )
        from agent_core.workflow.managed_stop import current_managed_stop
        managed = current_managed_stop(durable)
        if managed:
            return {"recovery_outcome": RecoveryOutcome(schema_version=1, type="blocked",
                summary=managed["explanation"], scope=managed["scope"], payload={"reason_code": managed["stop_reason"]["error_code"],
                    "blocked_evidence": [], "source_scope": managed["scope"], "budget_receipt": managed["stop_reason"]}).to_dict()}
        prior_stop = current_inspection_stop(durable)
        if prior_stop:
            # A matching durable stop is an exit receipt, not a new episode.
            if prior_stop.get("reports_status") != "finalized":
                prior_stop = finalize_inspection_stop(
                    self.data_path, prior_stop, loader=self.loader, saver=self.saver,
                )
            return {
                "recovery_outcome": inspection_stop_outcome(prior_stop).to_dict(),
                "messages": [AIMessage(content=str(prior_stop.get("explanation") or ""))],
            }
        projection = None
        episode: dict[str, Any] = {}

        def fail(*, task_id, durable, code, detail="", recovery_archive_ref=""):
            held_episode = deepcopy(episode)
            held_error = dict(held_episode.get("terminal_error") or {})
            if held_error:
                # Archive/finalization failure must not replace a known stop.
                held_episode.setdefault("secondary_failures", []).append({
                    "stage": "recovery.finalization", "error_code": str(code),
                })
            else:
                held_episode["terminal_error"] = {"code": str(code), "detail": str(detail)}
            stopped = self._inspection_stop_result(
                projection=projection, durable=durable, episode=held_episode,
                archive_ref=recovery_archive_ref,
            )
            if stopped is not None:
                return stopped
            return self._internal(
                task_id=task_id, durable=durable, code=code, detail=detail,
                recovery_archive_ref=recovery_archive_ref,
            )

        try:
            projection = project_recovery_seed(state, durable)
            if projection is None:
                return fail(
                    task_id=task_id,
                    durable=durable,
                    code="RECOVERY_CAUSAL_PACKET_MISSING",
                )
            seed = projection.recovery_seed
            registry = build_recovery_inspection_registry(
                seed, durable, phase_store=self.phase_store
            )
            dispatcher = RecoveryLocalToolDispatcher(
                seed=seed,
                durable=durable,
                registry=registry,
                phase_store=self.phase_store,
                skill_service=self.skill_service,
                evidence_reader=self.evidence_reader,
            )
            context_epoch_id = f"{seed.seed_id}:context-epoch:0"
            inherited_messages, inherited_descriptors = (
                _normalize_inherited_materials(
                    self._initial_inherited_materials(
                        seed=seed, durable=durable
                    ),
                    epoch_id=context_epoch_id,
                )
            )
            restored_context = self._restore_active_context(
                parent_state=state,
                durable=durable,
                seed=seed,
            )
        except StageContractError as exc:
            return fail(
                task_id=task_id, durable=durable, code=exc.code, detail=exc.detail
            )
        except Exception as exc:
            return fail(
                task_id=task_id,
                durable=durable,
                code=str(getattr(exc, "code", None) or "RECOVERY_AGENT_PHASE_FAILED"),
                detail=str(getattr(exc, "detail", None) or exc),
            )

        if restored_context is not None:
            context_epoch_id = str(restored_context["context_epoch_id"])
            inherited_messages = list(
                restored_context["inherited_messages"]
            )
            local_messages = list(restored_context["local_messages"])
            inherited_descriptors = deepcopy(dict(
                restored_context["inherited_descriptors"]
            ))
            budget_state = deepcopy(dict(restored_context["budget_state"]))
            linked_previous_archive_ref = str(
                restored_context["linked_previous_archive_ref"]
            )
            overflow_epoch = int(restored_context["overflow_epoch"])
        else:
            local_messages = []
            budget_state = {}
            linked_previous_archive_ref = ""
            overflow_epoch = 0
        try:
            await self._emit_event(
                "recovery_started",
                {
                    "event_id": f"recovery:{seed.seed_id}:started",
                    "task_id": task_id,
                    "step_id": seed.failure_packet.step_id,
                    "seed_id": seed.seed_id,
                    "failure_id": seed.failure_packet.failure_id,
                    "context_epoch_id": context_epoch_id,
                    "overflow_epoch": overflow_epoch,
                },
            )
        except (asyncio.CancelledError, GraphBubbleUp):
            raise
        except Exception as exc:
            return fail(
                task_id=task_id, durable=durable,
                code=str(getattr(exc, "code", None) or "RECOVERY_EVENT_PERSISTENCE_FAILED"),
                detail=str(getattr(exc, "detail", None) or exc),
                recovery_archive_ref=linked_previous_archive_ref,
            )
        archive = None

        while True:
            try:
                episode = await RecoveryAgentLoop(
                    model=self.model,
                    dispatcher=dispatcher,
                    config=self.loop_config,
                    observation_store=RecoveryObservationStore(self.phase_store),
                    context_budget_policy=self.context_budget_policy,
                    token_estimator=self.token_estimator,
                    provider_invoke=self.provider_invoke,
                    event_sink=self.event_sink,
                ).ainvoke(
                    seed=seed,
                    durable=durable,
                    inspection_manifest=registry.provider_manifest(),
                    inherited_messages=inherited_messages,
                    local_messages=local_messages,
                    budget_state=budget_state,
                    context_epoch_id=context_epoch_id,
                    overflow_epoch=overflow_epoch,
                    linked_previous_archive_ref=linked_previous_archive_ref,
                )
            except StageContractError as exc:
                return fail(
                    task_id=task_id,
                    durable=durable,
                    code=exc.code,
                    detail=exc.detail,
                    recovery_archive_ref=linked_previous_archive_ref,
                )
            except Exception as exc:
                logger.exception(
                    "Recovery agent phase failed closed: task=%s seed=%s",
                    task_id,
                    seed.seed_id,
                )
                return fail(
                    task_id=task_id,
                    durable=durable,
                    code=str(
                        getattr(exc, "code", None)
                        or "RECOVERY_AGENT_PHASE_FAILED"
                    ),
                    detail=str(getattr(exc, "detail", None) or exc),
                    recovery_archive_ref=linked_previous_archive_ref,
                )

            try:
                archive = archive_recovery_episode(
                    phase_store=self.phase_store,
                    seed=seed,
                    episode=episode,
                    overflow_epoch=overflow_epoch,
                    linked_previous_archive_ref=linked_previous_archive_ref,
                )
            except Exception as exc:
                return fail(
                    task_id=task_id,
                    durable=durable,
                    code=str(
                        getattr(exc, "code", None)
                        or "RECOVERY_PHASE_ARCHIVE_FAILED"
                    ),
                    detail=str(getattr(exc, "detail", None) or exc),
                    recovery_archive_ref=linked_previous_archive_ref,
                )

            boundary = dict(episode.get("compaction_required") or {})
            if not boundary:
                break
            if overflow_epoch >= self.loop_config.max_overflow_epochs:
                return fail(
                    task_id=task_id,
                    durable=durable,
                    code="RECOVERY_OVERFLOW_EPOCH_LIMIT",
                    detail=(
                        f"epoch={overflow_epoch}; "
                        f"limit={self.loop_config.max_overflow_epochs}"
                    ),
                    recovery_archive_ref=archive.archive_ref,
                )

            if not self.context_compaction_enabled:
                boundary_budget = dict(boundary.get("budget") or {})
                resumed = deepcopy(dict(
                    episode.get("_resume_state") or {}
                ))
                prior_failures = deepcopy(list(
                    resumed.get("compaction_failures") or ()
                ))
                if bool(boundary_budget.get("hard_block")):
                    return fail(
                        task_id=task_id,
                        durable=durable,
                        code=(
                            "RECOVERY_CONTEXT_COMPACTION_DISABLED_HARD_BLOCK"
                        ),
                        detail=(
                            "The next provider request exceeds the safe token "
                            "boundary while context compaction is disabled."
                        ),
                        recovery_archive_ref=archive.archive_ref,
                    )
                already_used_fallback = any(
                    str(
                        (dict(item.get("failure") or {}) if isinstance(
                            item, Mapping
                        ) else {}).get("error_code")
                        or ""
                    )
                    == "RECOVERY_CONTEXT_COMPACTION_DISABLED"
                    for item in prior_failures
                )
                if already_used_fallback:
                    return fail(
                        task_id=task_id,
                        durable=durable,
                        code=(
                            "RECOVERY_CONTEXT_COMPACTION_DISABLED_AFTER_FALLBACK"
                        ),
                        detail=(
                            "The single safe uncompacted fallback was already "
                            "consumed."
                        ),
                        recovery_archive_ref=archive.archive_ref,
                    )
                prior_failures.append({
                    "context_epoch_id": context_epoch_id,
                    "source_archive_ref": archive.archive_ref,
                    "failure": {
                        "error_code": (
                            "RECOVERY_CONTEXT_COMPACTION_DISABLED"
                        ),
                        "detail": (
                            "Context compaction is disabled; one token-safe "
                            "uncompacted provider request is permitted."
                        ),
                    },
                    "attempts": [],
                    "compactor_observability": [],
                    "fallback": "safe_uncompacted_request_once",
                })
                resumed["compaction_failures"] = prior_failures
                resumed["compaction_bypass_remaining"] = 1
                budget_state = resumed
                inherited_messages = list(
                    episode.get("_inherited_messages") or ()
                )
                local_messages = list(
                    episode.get("_local_messages") or ()
                )
                linked_previous_archive_ref = archive.archive_ref
                continue

            compactor_observability: list[dict[str, Any]] = []
            candidate_archive = None
            try:
                request, bodies, originals = _build_compaction_request(
                    seed=seed,
                    episode=episode,
                    inherited_messages=list(
                        episode.get("_inherited_messages") or ()
                    ),
                    local_messages=list(episode.get("_local_messages") or ()),
                    inherited_descriptors=inherited_descriptors,
                    source_epoch_id=context_epoch_id,
                    history_version=overflow_epoch,
                    selected_revision=int(seed.task_spec_ref.task_revision),
                    policy=self.context_budget_policy,
                    estimator=self.token_estimator,
                )
                archived_request = replace(
                    request, source_archive_ref=archive.archive_ref
                )

                async def invoke_compactor(
                    messages: Sequence[Mapping[str, str]],
                ) -> Any:
                    return await self._invoke_compactor(
                        messages,
                        observability=compactor_observability,
                        task_id=task_id,
                        seed_id=seed.seed_id,
                        failure_id=seed.failure_packet.failure_id,
                        source_epoch_id=context_epoch_id,
                        compaction_request_id=archived_request.compaction_request_id,
                    )

                prepared = await prepare_compaction(
                    archived_request,
                    bodies,
                    invoke_compactor,
                    estimator=self.token_estimator,
                    max_attempts=2,
                )
                (
                    candidate_inherited,
                    candidate_local,
                    candidate_descriptors,
                ) = _next_epoch_messages(
                    prepared=prepared,
                    originals=originals,
                    source_units=request.source_manifest.by_id(),
                    inherited_descriptors=inherited_descriptors,
                )
                task_state = dict(durable.get("task_state") or {})
                residency_by_phase = dict(
                    task_state.get("skill_material_residency") or {}
                )
                prior_residency = (
                    residency_by_phase.get("latest")
                    if isinstance(
                        residency_by_phase.get("latest"), Mapping
                    )
                    else residency_by_phase.get("recovery")
                    if isinstance(
                        residency_by_phase.get("recovery"), Mapping
                    )
                    else {}
                )
                material_residency = _material_residency_for_epoch(
                    prepared=prepared,
                    request=request,
                    inherited_descriptors=inherited_descriptors,
                    prior=prior_residency,
                )
                provider_baseline_manifest = (
                    _candidate_provider_baseline_manifest(
                        prepared=prepared,
                        inherited_messages=candidate_inherited,
                        local_messages=candidate_local,
                        inherited_descriptors=candidate_descriptors,
                    )
                )
                business_resume_state = _business_resume_state(
                    episode.get("_resume_state")
                    if isinstance(episode.get("_resume_state"), Mapping)
                    else {}
                )
                # This immutable write is the final operation before the local
                # PlanState CAS.  Any failure leaves the old archived episode,
                # durable active pointer, and in-memory context untouched.
                candidate_archive = archive_recovery_compaction_epoch(
                    phase_store=self.phase_store,
                    seed=seed,
                    prepared=prepared,
                    source_archive_ref=archive.archive_ref,
                    overflow_epoch=overflow_epoch + 1,
                    compactor_observability=compactor_observability,
                    provider_baseline_manifest=provider_baseline_manifest,
                    business_resume_state=business_resume_state,
                )
                committed_durable, _already_active = (
                    self._persist_context_epoch_activation(
                        task_id=task_id,
                        parent_state=state,
                        durable=durable,
                        seed=seed,
                        prepared=prepared,
                        candidate_epoch_archive_ref=(
                            candidate_archive.archive_ref
                        ),
                        source_archive_ref=archive.archive_ref,
                        source_epoch_id=context_epoch_id,
                        history_version=overflow_epoch,
                        material_residency=material_residency,
                    )
                )
            except Exception as exc:
                if isinstance(exc, ContextEpochCASConflict):
                    return fail(
                        task_id=task_id,
                        durable=durable,
                        code=exc.code,
                        detail=(
                            f"{exc.detail}; source_archive_ref="
                            f"{archive.archive_ref}"
                        ),
                        # The orphan candidate is immutable and links back to
                        # the still-active source archive, making a late CAS
                        # conflict inspectable without activating the epoch.
                        recovery_archive_ref=(
                            candidate_archive.archive_ref
                            if candidate_archive is not None
                            else archive.archive_ref
                        ),
                    )
                boundary_budget = dict(boundary.get("budget") or {})
                if not bool(boundary_budget.get("hard_block")):
                    failure = (
                        exc.to_dict()
                        if isinstance(exc, ContextCompactionError)
                        else {
                            "error_code": "RECOVERY_CONTEXT_COMPACTION_FAILED",
                            "detail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    attempts = [
                        item.to_dict()
                        for item in getattr(exc, "attempts", ())
                    ]
                    resumed = deepcopy(dict(
                        episode.get("_resume_state") or {}
                    ))
                    failures = deepcopy(list(
                        resumed.get("compaction_failures") or ()
                    ))
                    failures.append({
                        "context_epoch_id": context_epoch_id,
                        "source_archive_ref": archive.archive_ref,
                        "failure": failure,
                        "attempts": attempts,
                        "compactor_observability": deepcopy(
                            compactor_observability
                        ),
                        "fallback": "safe_uncompacted_request",
                    })
                    resumed["compaction_failures"] = failures
                    # Permit exactly one still-safe provider dispatch.  Any
                    # subsequent round is measured again and can establish a
                    # fresh boundary; a hard-blocked request is never sent.
                    resumed["compaction_bypass_remaining"] = 1
                    budget_state = resumed
                    inherited_messages = list(
                        episode.get("_inherited_messages") or ()
                    )
                    local_messages = list(
                        episode.get("_local_messages") or ()
                    )
                    linked_previous_archive_ref = archive.archive_ref
                    continue
                return fail(
                    task_id=task_id,
                    durable=durable,
                    code=str(
                        getattr(exc, "code", None)
                        or "RECOVERY_CONTEXT_COMPACTION_FAILED"
                    ),
                    detail=(
                        _canonical(exc.to_dict())
                        if isinstance(exc, ContextCompactionError)
                        else str(getattr(exc, "detail", None) or exc)
                    ),
                    recovery_archive_ref=archive.archive_ref,
                )

            durable = committed_durable
            inherited_messages = candidate_inherited
            local_messages = candidate_local
            inherited_descriptors = candidate_descriptors
            budget_state = deepcopy(dict(
                episode.get("_resume_state") or {}
            ))
            overflow_epoch += 1
            context_epoch_id = prepared.epoch.record.epoch_id
            linked_previous_archive_ref = candidate_archive.archive_ref

        error = dict(episode.get("terminal_error") or {})
        directive = dict(episode.get("directive") or {})
        if error or not directive:
            stopped = self._inspection_stop_result(
                projection=projection, durable=durable, episode=episode,
                archive_ref=(archive.archive_ref if archive else ""),
            )
            if stopped is not None:
                return stopped
            return fail(
                task_id=task_id,
                durable=durable,
                code=str(error.get("code") or "RECOVERY_DIRECTIVE_MISSING"),
                detail=str(error.get("detail") or ""),
                recovery_archive_ref=(archive.archive_ref if archive else ""),
            )
        admitted_state = {
            **deepcopy(dict(state)),
            "recovery_directive": directive,
            "recovery_inspected_refs": list(
                (episode.get("audit") or {}).get("inspected_refs") or ()
            ),
            "recovery_archive_ref": archive.archive_ref,
        }
        try:
            await self._emit_event(
                "recovery_directive_admitted",
                {
                    "event_id": f"recovery:{seed.seed_id}:directive:admitted",
                    "task_id": task_id,
                    "step_id": seed.failure_packet.step_id,
                    "seed_id": seed.seed_id,
                    "failure_id": seed.failure_packet.failure_id,
                    "context_epoch_id": context_epoch_id,
                    "overflow_epoch": overflow_epoch,
                    "directive_id": str(directive.get("directive_id") or ""),
                    "action": str(directive.get("action") or ""),
                    "recovery_archive_ref": archive.archive_ref,
                },
            )
        except (asyncio.CancelledError, GraphBubbleUp):
            raise
        except Exception as exc:
            return fail(
                task_id=task_id, durable=durable,
                code=str(getattr(exc, "code", None) or "RECOVERY_EVENT_PERSISTENCE_FAILED"),
                detail=str(getattr(exc, "detail", None) or exc),
                recovery_archive_ref=archive.archive_ref,
            )
        # The provider episode stays local.  Only the semantic directive and
        # bounded inspected-ref receipt reach the deterministic runtime.
        try:
            applied = await self.runtime.run(admitted_state)
        except (asyncio.CancelledError, GraphBubbleUp):
            raise
        except Exception as exc:
            return fail(
                task_id=task_id, durable=durable,
                code=str(getattr(exc, "code", None) or "RECOVERY_DIRECTIVE_APPLY_FAILED"),
                detail=str(getattr(exc, "detail", None) or exc),
                recovery_archive_ref=archive.archive_ref,
            )
        result = dict(applied.get("recovery_outcome") or {})
        if result.get("type") in {"blocked", "internal_protocol_error"}:
            payload = dict(result.get("payload") or {})
            stopped = self._inspection_stop_result(
                projection=projection, durable=durable,
                episode={**episode, "terminal_error": {
                    "code": str(payload.get("error_code") or payload.get("reason_code")
                                or directive.get("reason_code") or "INSPECTION_UNRESOLVED"),
                    "detail": str(directive.get("summary") or result.get("summary") or ""),
                }},
                archive_ref=archive.archive_ref,
            )
            if stopped is not None:
                return stopped
        return applied


__all__ = ["Schema2RecoveryNode"]

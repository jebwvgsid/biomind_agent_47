"""Generate handoff and per-step private reasoning-episode contracts.

The production Generate node historically rebuilt one large Human message for
every invocation.  This module provides the state-free primitives needed to
give Generate an explicit private context owner without coupling that owner to
the workflow graph implementation.  A Generate request has four regions::

    stable System
    frozen per-step Generate Seed
    selected inherited material messages
    append-only local candidate/validator events

The immutable records below deliberately contain only provider-visible message
fields.  Transport retries therefore reuse one frozen request, while business
transitions are represented explicitly instead of being inferred from only a
``step_id``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_core.workflow.outcomes import RecoveryOutcome
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.workflow.stage_contracts import (
    FailurePacket,
    GenerateSeed,
    RecoveryDirective,
    StageContractError,
)


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


class GenerateEpisodeError(ValueError):
    """Typed fail-closed violation of the Generate episode contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "GENERATE_EPISODE_ERROR")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


class GenerateEpisodeTransition(str, Enum):
    """The only supported causal transitions for a Generate private context."""

    INITIAL = "initial"
    TRANSPORT_RETRY = "transport_retry"
    SAME_STEP_VALIDATOR_APPEND = "same_step_validator_append"
    NEXT_STEP = "next_step"
    RECOVERY_RETURN = "recovery_return"
    AUTHORITY_CHANGE = "authority_change"
    CAPACITY_EPOCH = "capacity_epoch"


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise GenerateEpisodeError("GENERATE_EPISODE_IDENTITY_INVALID", field)
    return text


@dataclass(frozen=True)
class GenerateEpisodeBasis:
    """Authoritative identity underlying one per-step reasoning episode.

    ``execution_identity`` names the execution/correction attempt whose action
    is being generated.  Candidate-format repair rounds do not change it.  A
    Recovery return must use a verified ``GenerateSeed.seed_id`` as
    ``recovery_seed_id`` and a fresh execution identity.
    """

    schema_version: int
    task_id: str
    task_revision: str
    plan_id: str
    plan_revision: str
    selected_skill_revision: str
    step_id: str
    execution_identity: str
    recovery_seed_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise GenerateEpisodeError(
                "GENERATE_EPISODE_SCHEMA_UNSUPPORTED", str(self.schema_version)
            )
        for field in (
            "task_id",
            "task_revision",
            "plan_id",
            "plan_revision",
            "selected_skill_revision",
            "step_id",
            "execution_identity",
        ):
            object.__setattr__(
                self,
                field,
                _required_text(getattr(self, field), field=f"basis.{field}"),
            )
        object.__setattr__(self, "recovery_seed_id", str(self.recovery_seed_id or "").strip())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GenerateEpisodeBasis":
        allowed = {
            "schema_version",
            "task_id",
            "task_revision",
            "plan_id",
            "plan_revision",
            "selected_skill_revision",
            "step_id",
            "execution_identity",
            "recovery_seed_id",
        }
        unknown = set(value) - allowed
        if unknown:
            raise GenerateEpisodeError(
                "GENERATE_EPISODE_IDENTITY_INVALID",
                "unknown fields: " + ", ".join(sorted(str(item) for item in unknown)),
            )
        return cls(
            schema_version=int(value.get("schema_version") or 0),
            task_id=value.get("task_id", ""),
            task_revision=value.get("task_revision", ""),
            plan_id=value.get("plan_id", ""),
            plan_revision=value.get("plan_revision", ""),
            selected_skill_revision=value.get("selected_skill_revision", ""),
            step_id=value.get("step_id", ""),
            execution_identity=value.get("execution_identity", ""),
            recovery_seed_id=value.get("recovery_seed_id", ""),
        )

    @property
    def authority_key(self) -> tuple[str, ...]:
        """External TaskSpec/Plan/selection authority, excluding step progress."""

        return (
            self.task_id,
            self.task_revision,
            self.plan_id,
            self.plan_revision,
            self.selected_skill_revision,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "selected_skill_revision": self.selected_skill_revision,
            "step_id": self.step_id,
            "execution_identity": self.execution_identity,
        }
        if self.recovery_seed_id:
            result["recovery_seed_id"] = self.recovery_seed_id
        return result


@dataclass(frozen=True)
class FrozenGenerateMessage:
    """Immutable canonical snapshot of one provider-visible message."""

    message_id: str
    payload_json: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "message_id",
            _required_text(self.message_id, field="message.message_id"),
        )
        try:
            payload = json.loads(self.payload_json)
        except Exception as exc:
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID", "payload_json is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID", "message payload must be an object"
            )
        canonical = _canonical(payload)
        if canonical != self.payload_json:
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID", "message payload is not canonical"
            )
        if str(payload.get("type") or "") not in {
            "system",
            "human",
            "ai",
            "tool",
            "chat",
        }:
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID",
                f"unsupported message type {payload.get('type')!r}",
            )

    @classmethod
    def from_message(
        cls,
        message: BaseMessage,
        *,
        message_id: str | None = None,
    ) -> "FrozenGenerateMessage":
        if not isinstance(message, BaseMessage):
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID", type(message).__name__
            )
        resolved_id = str(message_id or getattr(message, "id", None) or "").strip()
        if not resolved_id:
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_ID_REQUIRED", type(message).__name__
            )
        try:
            payload = provider_visible_payload(message)
        except Exception as exc:
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID", f"{type(exc).__name__}: {exc}"
            ) from exc
        return cls(message_id=resolved_id, payload_json=_canonical(payload))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FrozenGenerateMessage":
        if not isinstance(value, Mapping):
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID", type(value).__name__
            )
        unknown = set(value) - {"message_id", "payload"}
        if unknown or not isinstance(value.get("payload"), Mapping):
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID",
                "persisted message must contain only message_id and payload",
            )
        return cls(
            message_id=value.get("message_id", ""),
            payload_json=_canonical(dict(value.get("payload") or {})),
        )

    @property
    def payload(self) -> dict[str, Any]:
        return dict(json.loads(self.payload_json))

    @property
    def fingerprint(self) -> str:
        return _digest(self.payload)

    def to_message(self) -> BaseMessage:
        payload = self.payload
        kind = str(payload.pop("type"))
        content = payload.pop("content", "")
        role = str(payload.pop("role", "") or "")
        name = str(payload.pop("name", "") or "")
        tool_calls = payload.pop("tool_calls", None)
        tool_call_id = str(payload.pop("tool_call_id", "") or "")
        if payload:
            raise GenerateEpisodeError(
                "GENERATE_MESSAGE_INVALID",
                "unsupported payload fields: " + ", ".join(sorted(payload)),
            )
        common: dict[str, Any] = {"content": content, "id": self.message_id}
        if name:
            common["name"] = name
        if kind == "system":
            return SystemMessage(**common)
        if kind == "human":
            return HumanMessage(**common)
        if kind == "ai":
            if tool_calls:
                common["tool_calls"] = tool_calls
            return AIMessage(**common)
        if kind == "tool":
            if not tool_call_id:
                raise GenerateEpisodeError(
                    "GENERATE_MESSAGE_INVALID", "ToolMessage requires tool_call_id"
                )
            return ToolMessage(tool_call_id=tool_call_id, **common)
        if kind == "chat" and role:
            return ChatMessage(role=role, **common)
        raise GenerateEpisodeError(
            "GENERATE_MESSAGE_INVALID", f"cannot reconstruct {kind!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {"message_id": self.message_id, "payload": self.payload}


@dataclass(frozen=True)
class GenerateEpisodeEvent:
    """One causally unique local event in an append-only Generate episode."""

    event_id: str
    event_kind: str
    message: FrozenGenerateMessage

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_id", _required_text(self.event_id, field="event.event_id")
        )
        object.__setattr__(
            self,
            "event_kind",
            _required_text(self.event_kind, field="event.event_kind"),
        )
        if not isinstance(self.message, FrozenGenerateMessage):
            raise GenerateEpisodeError(
                "GENERATE_EVENT_INVALID", "message is not frozen"
            )

    @classmethod
    def from_message(
        cls,
        *,
        event_id: str,
        event_kind: str,
        message: BaseMessage,
    ) -> "GenerateEpisodeEvent":
        stable_id = "generate-event-" + _digest({
            "event_id": _required_text(event_id, field="event.event_id"),
            "event_kind": _required_text(event_kind, field="event.event_kind"),
        }).removeprefix("sha256:")[:24]
        return cls(
            event_id=event_id,
            event_kind=event_kind,
            message=FrozenGenerateMessage.from_message(
                message, message_id=stable_id
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "message": self.message.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GenerateEpisodeEvent":
        if not isinstance(value, Mapping):
            raise GenerateEpisodeError(
                "GENERATE_EVENT_INVALID", type(value).__name__
            )
        unknown = set(value) - {"event_id", "event_kind", "message"}
        if unknown or not isinstance(value.get("message"), Mapping):
            raise GenerateEpisodeError(
                "GENERATE_EVENT_INVALID", "persisted event shape"
            )
        return cls(
            event_id=value.get("event_id", ""),
            event_kind=value.get("event_kind", ""),
            message=FrozenGenerateMessage.from_mapping(
                dict(value.get("message") or {})
            ),
        )


@dataclass(frozen=True)
class GenerateReasoningEpisode:
    """Immutable context snapshot for one Generate reasoning episode/epoch."""

    schema_version: int
    basis: GenerateEpisodeBasis
    reasoning_episode_id: str
    context_epoch: int
    transition: GenerateEpisodeTransition
    system_message: FrozenGenerateMessage
    seed_message: FrozenGenerateMessage
    inherited_materials: tuple[FrozenGenerateMessage, ...]
    local_events: tuple[GenerateEpisodeEvent, ...]
    previous_reasoning_episode_id: str = ""
    previous_context_archive_ref: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise GenerateEpisodeError(
                "GENERATE_EPISODE_SCHEMA_UNSUPPORTED", str(self.schema_version)
            )
        if not isinstance(self.basis, GenerateEpisodeBasis):
            raise GenerateEpisodeError(
                "GENERATE_EPISODE_IDENTITY_INVALID", "basis"
            )
        object.__setattr__(
            self,
            "reasoning_episode_id",
            _required_text(
                self.reasoning_episode_id, field="reasoning_episode_id"
            ),
        )
        if isinstance(self.context_epoch, bool) or int(self.context_epoch) < 0:
            raise GenerateEpisodeError(
                "GENERATE_CONTEXT_EPOCH_INVALID", str(self.context_epoch)
            )
        object.__setattr__(self, "context_epoch", int(self.context_epoch))
        try:
            transition = GenerateEpisodeTransition(self.transition)
        except Exception as exc:
            raise GenerateEpisodeError(
                "GENERATE_TRANSITION_INVALID", str(self.transition)
            ) from exc
        object.__setattr__(self, "transition", transition)
        if self.system_message.payload.get("type") != "system":
            raise GenerateEpisodeError(
                "GENERATE_SYSTEM_INVALID", "SystemMessage required"
            )
        if self.seed_message.payload.get("type") != "human":
            raise GenerateEpisodeError(
                "GENERATE_SEED_INVALID", "HumanMessage required"
            )
        if any(item.payload.get("type") != "human" for item in self.inherited_materials):
            raise GenerateEpisodeError(
                "GENERATE_INHERITED_MATERIAL_INVALID",
                "selected materials must use HumanMessage source containers",
            )
        ids: list[str] = [
            self.system_message.message_id,
            self.seed_message.message_id,
            *(item.message_id for item in self.inherited_materials),
            *(item.message.message_id for item in self.local_events),
        ]
        if len(ids) != len(set(ids)):
            raise GenerateEpisodeError(
                "GENERATE_PROVIDER_MESSAGE_ID_DUPLICATE", self.reasoning_episode_id
            )
        event_ids = [item.event_id for item in self.local_events]
        if len(event_ids) != len(set(event_ids)):
            raise GenerateEpisodeError(
                "GENERATE_EVENT_ID_DUPLICATE", self.reasoning_episode_id
            )

    @property
    def context_epoch_id(self) -> str:
        return f"{self.reasoning_episode_id}:epoch:{self.context_epoch}"

    def frozen_messages(self) -> tuple[FrozenGenerateMessage, ...]:
        return (
            self.system_message,
            self.seed_message,
            *self.inherited_materials,
            *(event.message for event in self.local_events),
        )

    def provider_messages(self) -> list[BaseMessage]:
        return [item.to_message() for item in self.frozen_messages()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "basis": self.basis.to_dict(),
            "reasoning_episode_id": self.reasoning_episode_id,
            "context_epoch": self.context_epoch,
            "context_epoch_id": self.context_epoch_id,
            "transition": self.transition.value,
            "system_message": self.system_message.to_dict(),
            "seed_message": self.seed_message.to_dict(),
            "inherited_materials": [item.to_dict() for item in self.inherited_materials],
            "local_events": [item.to_dict() for item in self.local_events],
            "previous_reasoning_episode_id": self.previous_reasoning_episode_id,
            "previous_context_archive_ref": self.previous_context_archive_ref,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GenerateReasoningEpisode":
        if not isinstance(value, Mapping):
            raise GenerateEpisodeError(
                "GENERATE_EPISODE_INVALID", type(value).__name__
            )
        allowed = {
            "schema_version", "basis", "reasoning_episode_id",
            "context_epoch", "context_epoch_id", "transition",
            "system_message", "seed_message", "inherited_materials",
            "local_events", "previous_reasoning_episode_id",
            "previous_context_archive_ref",
        }
        unknown = set(value) - allowed
        if unknown:
            raise GenerateEpisodeError(
                "GENERATE_EPISODE_INVALID",
                "unknown fields: " + ", ".join(sorted(str(item) for item in unknown)),
            )
        episode = cls(
            schema_version=int(value.get("schema_version") or 0),
            basis=GenerateEpisodeBasis.from_mapping(
                dict(value.get("basis") or {})
            ),
            reasoning_episode_id=value.get("reasoning_episode_id", ""),
            context_epoch=int(value.get("context_epoch") or 0),
            transition=GenerateEpisodeTransition(
                str(value.get("transition") or "")
            ),
            system_message=FrozenGenerateMessage.from_mapping(
                dict(value.get("system_message") or {})
            ),
            seed_message=FrozenGenerateMessage.from_mapping(
                dict(value.get("seed_message") or {})
            ),
            inherited_materials=tuple(
                FrozenGenerateMessage.from_mapping(item)
                for item in value.get("inherited_materials", ()) or ()
                if isinstance(item, Mapping)
            ),
            local_events=tuple(
                GenerateEpisodeEvent.from_mapping(item)
                for item in value.get("local_events", ()) or ()
                if isinstance(item, Mapping)
            ),
            previous_reasoning_episode_id=value.get(
                "previous_reasoning_episode_id", ""
            ),
            previous_context_archive_ref=value.get(
                "previous_context_archive_ref", ""
            ),
        )
        supplied_epoch_id = str(value.get("context_epoch_id") or "")
        if supplied_epoch_id and supplied_epoch_id != episode.context_epoch_id:
            raise GenerateEpisodeError(
                "GENERATE_CONTEXT_EPOCH_INVALID", supplied_epoch_id
            )
        return episode


@dataclass(frozen=True)
class GenerateProviderRequest:
    """A request snapshot reusable verbatim for network/stream retries."""

    reasoning_episode_id: str
    context_epoch: int
    messages: tuple[FrozenGenerateMessage, ...]
    request_digest: str

    def __post_init__(self) -> None:
        expected = _digest({
            "reasoning_episode_id": self.reasoning_episode_id,
            "context_epoch": self.context_epoch,
            "messages": [item.to_dict() for item in self.messages],
        })
        if self.request_digest != expected:
            raise GenerateEpisodeError(
                "GENERATE_FROZEN_REQUEST_INVALID", "digest mismatch"
            )

    def provider_messages(self) -> list[BaseMessage]:
        return [item.to_message() for item in self.messages]

    def provider_payloads(self) -> list[dict[str, Any]]:
        return [item.payload for item in self.messages]


def _content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        return _canonical(value)
    raise GenerateEpisodeError(
        "GENERATE_MESSAGE_INVALID", f"unsupported content {type(value).__name__}"
    )


def _fixed_message(
    value: FrozenGenerateMessage | BaseMessage | Any,
    *,
    kind: str,
    stable_id: str,
) -> FrozenGenerateMessage:
    if isinstance(value, FrozenGenerateMessage):
        result = value
    elif isinstance(value, BaseMessage):
        result = FrozenGenerateMessage.from_message(value, message_id=stable_id)
    elif kind == "system":
        result = FrozenGenerateMessage.from_message(
            SystemMessage(content=_content(value), id=stable_id)
        )
    else:
        result = FrozenGenerateMessage.from_message(
            HumanMessage(content=_content(value), id=stable_id)
        )
    if result.payload.get("type") != kind:
        raise GenerateEpisodeError(
            "GENERATE_FIXED_MESSAGE_INVALID", f"expected {kind}"
        )
    return result


def _inherited_materials(
    values: Sequence[FrozenGenerateMessage | BaseMessage],
) -> tuple[FrozenGenerateMessage, ...]:
    result: list[FrozenGenerateMessage] = []
    for index, value in enumerate(values):
        if isinstance(value, FrozenGenerateMessage):
            item = value
        elif isinstance(value, BaseMessage):
            if not str(getattr(value, "id", None) or "").strip():
                raise GenerateEpisodeError(
                    "GENERATE_INHERITED_MATERIAL_ID_REQUIRED", str(index)
                )
            item = FrozenGenerateMessage.from_message(value)
        else:
            raise GenerateEpisodeError(
                "GENERATE_INHERITED_MATERIAL_INVALID", type(value).__name__
            )
        if item.payload.get("type") != "human":
            raise GenerateEpisodeError(
                "GENERATE_INHERITED_MATERIAL_INVALID", str(index)
            )
        result.append(item)
    return tuple(result)


def _reasoning_episode_id(
    basis: GenerateEpisodeBasis,
    transition: GenerateEpisodeTransition,
    previous_reasoning_episode_id: str,
) -> str:
    identity = {
        "basis": basis.to_dict(),
        "transition": transition.value,
        "previous_reasoning_episode_id": previous_reasoning_episode_id,
    }
    return "generate-episode-" + _digest(identity).removeprefix("sha256:")[:24]


def _new_episode(
    *,
    basis: GenerateEpisodeBasis,
    transition: GenerateEpisodeTransition,
    system_message: FrozenGenerateMessage | BaseMessage | Any,
    seed_message: FrozenGenerateMessage | BaseMessage | Any,
    inherited_materials: Sequence[FrozenGenerateMessage | BaseMessage],
    previous_reasoning_episode_id: str = "",
) -> GenerateReasoningEpisode:
    episode_id = _reasoning_episode_id(
        basis, transition, previous_reasoning_episode_id
    )
    system = _fixed_message(
        system_message,
        kind="system",
        stable_id="generate-system-" + _digest(_content(
            system_message.payload if isinstance(system_message, FrozenGenerateMessage)
            else getattr(system_message, "content", system_message)
        )).removeprefix("sha256:")[:24],
    )
    seed = _fixed_message(
        seed_message,
        kind="human",
        stable_id=f"generate-seed-{episode_id.removeprefix('generate-episode-')}",
    )
    return GenerateReasoningEpisode(
        schema_version=1,
        basis=basis,
        reasoning_episode_id=episode_id,
        context_epoch=0,
        transition=transition,
        system_message=system,
        seed_message=seed,
        inherited_materials=_inherited_materials(inherited_materials),
        local_events=(),
        previous_reasoning_episode_id=previous_reasoning_episode_id,
    )


def start_generate_reasoning_episode(
    *,
    basis: GenerateEpisodeBasis,
    system_message: FrozenGenerateMessage | BaseMessage | Any,
    seed_message: FrozenGenerateMessage | BaseMessage | Any,
    inherited_materials: Sequence[FrozenGenerateMessage | BaseMessage] = (),
) -> GenerateReasoningEpisode:
    """Start the first private episode for a normal approved-plan step."""

    if basis.recovery_seed_id:
        raise GenerateEpisodeError(
            "GENERATE_RECOVERY_SEED_UNEXPECTED", basis.recovery_seed_id
        )
    return _new_episode(
        basis=basis,
        transition=GenerateEpisodeTransition.INITIAL,
        system_message=system_message,
        seed_message=seed_message,
        inherited_materials=inherited_materials,
    )


def append_generate_episode_events(
    episode: GenerateReasoningEpisode,
    *,
    observed_basis: GenerateEpisodeBasis,
    events: Sequence[GenerateEpisodeEvent],
) -> GenerateReasoningEpisode:
    """Append same-attempt candidate/validator facts idempotently.

    Comparing the complete basis, rather than merely ``step_id``, prevents a
    stale candidate from being repaired after Plan, TaskSpec, selected Skill,
    or execution-attempt authority has changed.
    """

    if observed_basis != episode.basis:
        raise GenerateEpisodeError(
            "GENERATE_EPISODE_BASIS_CHANGED",
            "start an authority/recovery/step transition before appending",
        )
    if not events:
        return episode
    by_id = {item.event_id: item for item in episode.local_events}
    result = list(episode.local_events)
    for item in events:
        if not isinstance(item, GenerateEpisodeEvent):
            raise GenerateEpisodeError(
                "GENERATE_EVENT_INVALID", type(item).__name__
            )
        prior = by_id.get(item.event_id)
        if prior is None:
            result.append(item)
            by_id[item.event_id] = item
            continue
        if prior != item:
            raise GenerateEpisodeError(
                "GENERATE_EVENT_ID_CONFLICT", item.event_id
            )
        # Exact event replay is an idempotent no-op.  Equal content under a new
        # event ID remains a separate historical fact.
    return replace(
        episode,
        transition=GenerateEpisodeTransition.SAME_STEP_VALIDATOR_APPEND,
        local_events=tuple(result),
    )


def freeze_generate_provider_request(
    episode: GenerateReasoningEpisode,
) -> GenerateProviderRequest:
    messages = episode.frozen_messages()
    digest = _digest({
        "reasoning_episode_id": episode.reasoning_episode_id,
        "context_epoch": episode.context_epoch,
        "messages": [item.to_dict() for item in messages],
    })
    return GenerateProviderRequest(
        reasoning_episode_id=episode.reasoning_episode_id,
        context_epoch=episode.context_epoch,
        messages=messages,
        request_digest=digest,
    )


def retry_generate_provider_request(
    frozen_request: GenerateProviderRequest,
) -> GenerateProviderRequest:
    """Return the same immutable request for a pure transport/stream retry."""

    if not isinstance(frozen_request, GenerateProviderRequest):
        raise GenerateEpisodeError(
            "GENERATE_FROZEN_REQUEST_INVALID", type(frozen_request).__name__
        )
    return frozen_request


def start_next_generate_step(
    episode: GenerateReasoningEpisode,
    *,
    basis: GenerateEpisodeBasis,
    seed_message: FrozenGenerateMessage | BaseMessage | Any,
    inherited_materials: Sequence[FrozenGenerateMessage | BaseMessage] | None = None,
    system_message: FrozenGenerateMessage | BaseMessage | Any | None = None,
) -> GenerateReasoningEpisode:
    """Start a fresh episode after deterministic advancement to another step."""

    if basis.task_id != episode.basis.task_id:
        raise GenerateEpisodeError(
            "GENERATE_TASK_SCOPE_MISMATCH", basis.task_id
        )
    if basis.authority_key != episode.basis.authority_key:
        raise GenerateEpisodeError(
            "GENERATE_NEXT_STEP_AUTHORITY_CHANGED",
            "use start_generate_after_authority_change",
        )
    if basis.step_id == episode.basis.step_id:
        raise GenerateEpisodeError(
            "GENERATE_NEXT_STEP_INVALID", basis.step_id
        )
    if basis.recovery_seed_id:
        raise GenerateEpisodeError(
            "GENERATE_RECOVERY_SEED_UNEXPECTED", basis.recovery_seed_id
        )
    return _new_episode(
        basis=basis,
        transition=GenerateEpisodeTransition.NEXT_STEP,
        system_message=episode.system_message if system_message is None else system_message,
        seed_message=seed_message,
        inherited_materials=(
            episode.inherited_materials
            if inherited_materials is None
            else inherited_materials
        ),
        previous_reasoning_episode_id=episode.reasoning_episode_id,
    )


def start_generate_after_authority_change(
    episode: GenerateReasoningEpisode,
    *,
    basis: GenerateEpisodeBasis,
    seed_message: FrozenGenerateMessage | BaseMessage | Any,
    inherited_materials: Sequence[FrozenGenerateMessage | BaseMessage],
    system_message: FrozenGenerateMessage | BaseMessage | Any | None = None,
) -> GenerateReasoningEpisode:
    """Start a fresh episode when TaskSpec/Plan/selection authority changes."""

    if basis.task_id != episode.basis.task_id:
        raise GenerateEpisodeError(
            "GENERATE_TASK_SCOPE_MISMATCH",
            "cross-task material carry is forbidden",
        )
    if basis.authority_key == episode.basis.authority_key:
        raise GenerateEpisodeError(
            "GENERATE_AUTHORITY_CHANGE_MISSING", basis.task_id
        )
    if basis.recovery_seed_id:
        raise GenerateEpisodeError(
            "GENERATE_RECOVERY_SEED_UNEXPECTED", basis.recovery_seed_id
        )
    return _new_episode(
        basis=basis,
        transition=GenerateEpisodeTransition.AUTHORITY_CHANGE,
        system_message=episode.system_message if system_message is None else system_message,
        seed_message=seed_message,
        inherited_materials=inherited_materials,
        previous_reasoning_episode_id=episode.reasoning_episode_id,
    )


_RECOVERY_TRANSCRIPT_KEYS = frozenset({
    "messages",
    "recovery_transcript",
    "recovery_outcome",
    "recovery_seed",
    "recovery_tool_messages",
    "recovery_candidate_hypotheses",
    "recovery_validator_feedback",
    "failure_packet",
    "failure_source",
})

_CONTENT_ADDRESSED_REFERENCE = re.compile(
    r"(?:^|:)sha256:[0-9a-f]{32,}$",
    re.IGNORECASE,
)


def _provider_safe_reference(value: Any, *, label: str) -> str:
    """Project a verified internal content ref as a non-authoritative handle."""

    text = str(value or "").strip()
    if text and _CONTENT_ADDRESSED_REFERENCE.search(text):
        return f"verified:{label}"
    return text


def _provider_generate_seed_view(seed: GenerateSeed) -> dict[str, Any]:
    """Keep corrective facts while withholding Controller content digests."""

    value = seed.to_dict()
    value["failure_packet_ref"] = _provider_safe_reference(
        value.get("failure_packet_ref"),
        label="current_failure_packet",
    )
    value["source_outcome_ref"] = _provider_safe_reference(
        value.get("source_outcome_ref"),
        label="current_source_outcome",
    )
    value["recovery_archive_ref"] = _provider_safe_reference(
        value.get("recovery_archive_ref"),
        label="current_recovery_archive",
    )
    value["evidence_refs"] = [
        _provider_safe_reference(item, label=f"evidence_{index}")
        for index, item in enumerate(value.get("evidence_refs") or (), start=1)
    ]
    directive = dict(value.get("recovery_directive") or {})
    directive["evidence_refs"] = [
        _provider_safe_reference(
            item,
            label=f"directive_evidence_{index}",
        )
        for index, item in enumerate(
            directive.get("evidence_refs") or (),
            start=1,
        )
    ]
    value["recovery_directive"] = directive
    return value


def _assert_transcript_free(value: Any, *, path: str = "node_context") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            label = str(key)
            if label in _RECOVERY_TRANSCRIPT_KEYS:
                raise GenerateEpisodeError(
                    "GENERATE_RECOVERY_TRANSCRIPT_FORBIDDEN", f"{path}.{label}"
                )
            _assert_transcript_free(child, path=f"{path}.{label}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_transcript_free(child, path=f"{path}[{index}]")


def start_generate_after_recovery(
    episode: GenerateReasoningEpisode,
    *,
    basis: GenerateEpisodeBasis,
    verified_generate_seed: GenerateSeed | Mapping[str, Any],
    node_context: Mapping[str, Any],
    inherited_materials: Sequence[FrozenGenerateMessage | BaseMessage],
    system_message: FrozenGenerateMessage | BaseMessage | Any | None = None,
) -> GenerateReasoningEpisode:
    """Start corrective Generate from a verified seed, never Recovery chat."""

    seed = (
        verified_generate_seed
        if isinstance(verified_generate_seed, GenerateSeed)
        else GenerateSeed.from_mapping(verified_generate_seed)
    )
    if basis.task_id != episode.basis.task_id or basis.task_id != seed.task_id:
        raise GenerateEpisodeError(
            "GENERATE_TASK_SCOPE_MISMATCH", basis.task_id
        )
    if basis.step_id != seed.step_id:
        raise GenerateEpisodeError(
            "GENERATE_RECOVERY_SEED_SCOPE_MISMATCH", basis.step_id
        )
    if basis.recovery_seed_id != seed.seed_id:
        raise GenerateEpisodeError(
            "GENERATE_RECOVERY_SEED_ID_MISMATCH", basis.recovery_seed_id
        )
    _assert_transcript_free(node_context)
    seed_body = {
        "schema_version": 1,
        "kind": "verified_corrective_generate_seed",
        "generate_seed": _provider_generate_seed_view(seed),
        "node_context": dict(node_context),
    }
    return _new_episode(
        basis=basis,
        transition=GenerateEpisodeTransition.RECOVERY_RETURN,
        system_message=episode.system_message if system_message is None else system_message,
        seed_message=seed_body,
        inherited_materials=inherited_materials,
        previous_reasoning_episode_id=episode.reasoning_episode_id,
    )


def advance_generate_capacity_epoch(
    episode: GenerateReasoningEpisode,
    *,
    observed_basis: GenerateEpisodeBasis,
    previous_context_archive_ref: str,
    inherited_materials: Sequence[FrozenGenerateMessage | BaseMessage],
    compacted_local_events: Sequence[GenerateEpisodeEvent],
) -> GenerateReasoningEpisode:
    """Install an archived compaction result without changing business identity."""

    if observed_basis != episode.basis:
        raise GenerateEpisodeError(
            "GENERATE_EPISODE_BASIS_CHANGED", "capacity transition"
        )
    archive_ref = _required_text(
        previous_context_archive_ref, field="previous_context_archive_ref"
    )
    for item in compacted_local_events:
        if not isinstance(item, GenerateEpisodeEvent):
            raise GenerateEpisodeError(
                "GENERATE_EVENT_INVALID", type(item).__name__
            )
    return GenerateReasoningEpisode(
        schema_version=episode.schema_version,
        basis=episode.basis,
        reasoning_episode_id=episode.reasoning_episode_id,
        context_epoch=episode.context_epoch + 1,
        transition=GenerateEpisodeTransition.CAPACITY_EPOCH,
        system_message=episode.system_message,
        seed_message=episode.seed_message,
        inherited_materials=_inherited_materials(inherited_materials),
        local_events=tuple(compacted_local_events),
        previous_reasoning_episode_id=episode.previous_reasoning_episode_id,
        previous_context_archive_ref=archive_ref,
    )


def project_corrective_generate_seed(
    recovery_outcome: RecoveryOutcome | Mapping[str, Any],
    durable: Mapping[str, Any],
) -> GenerateSeed:
    """Resolve one admitted directive receipt into a transcript-free seed."""

    outcome = (
        recovery_outcome
        if isinstance(recovery_outcome, RecoveryOutcome)
        else RecoveryOutcome.from_mapping(recovery_outcome)
    )
    if outcome.type != "candidate_repair_required":
        raise StageContractError(
            "GENERATE_RECOVERY_OUTCOME_INVALID", outcome.type
        )
    payload = dict(outcome.to_dict().get("payload") or {})
    decision_id = str(payload.get("decision_id") or "")
    task_state = dict(durable.get("task_state") or {})
    receipt = dict(
        (task_state.get("typed_recovery_receipts") or {}).get(decision_id) or {}
    )
    if not receipt:
        raise StageContractError(
            "GENERATE_RECOVERY_RECEIPT_MISSING", decision_id
        )
    if str(receipt.get("outcome_digest") or "") != _digest(outcome.to_dict()):
        raise StageContractError(
            "GENERATE_RECOVERY_OUTCOME_MISMATCH", decision_id
        )
    directive = RecoveryDirective.from_mapping(receipt.get("directive") or {})
    if directive.directive_id != decision_id:
        raise StageContractError(
            "GENERATE_RECOVERY_DIRECTIVE_MISMATCH", decision_id
        )
    if str(receipt.get("directive_digest") or "") != _digest(directive.to_dict()):
        raise StageContractError(
            "GENERATE_RECOVERY_DIRECTIVE_MISMATCH", decision_id
        )
    packet_ref = str(receipt.get("failure_packet_ref") or "")
    record = dict(
        (task_state.get("causal_failure_packets") or {}).get(packet_ref) or {}
    )
    packet = FailurePacket.from_mapping(record.get("failure_packet") or {})
    archive_ref = str(receipt.get("recovery_archive_ref") or "")
    if not archive_ref:
        raise StageContractError(
            "GENERATE_RECOVERY_ARCHIVE_REF_MISSING", decision_id
        )
    if directive.task_id != packet.task_id:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "corrective Generate task identity"
        )
    if str(durable.get("current_step_id") or "") != packet.step_id:
        raise StageContractError(
            "GENERATE_RECOVERY_PACKET_STALE", packet.step_id
        )
    evidence_refs = tuple(dict.fromkeys((
        *directive.evidence_refs,
        *outcome.evidence_refs,
    )))
    identity = {
        "task_id": packet.task_id,
        "step_id": packet.step_id,
        "failure_id": packet.failure_id,
        "failure_packet_ref": packet_ref,
        "source_outcome_ref": packet.source_outcome_ref,
        "directive": directive.to_dict(),
        "evidence_refs": list(evidence_refs),
        "recovery_archive_ref": archive_ref,
    }
    return GenerateSeed(
        schema_version=1,
        seed_id="generate-seed-" + _digest(identity).removeprefix("sha256:")[:24],
        task_id=packet.task_id,
        step_id=packet.step_id,
        failure_id=packet.failure_id,
        failure_packet_ref=packet_ref,
        source_outcome_ref=packet.source_outcome_ref,
        recovery_directive=directive,
        evidence_refs=evidence_refs,
        recovery_archive_ref=archive_ref,
    )


__all__ = [
    "FrozenGenerateMessage",
    "GenerateEpisodeBasis",
    "GenerateEpisodeError",
    "GenerateEpisodeEvent",
    "GenerateEpisodeTransition",
    "GenerateProviderRequest",
    "GenerateReasoningEpisode",
    "advance_generate_capacity_epoch",
    "append_generate_episode_events",
    "freeze_generate_provider_request",
    "project_corrective_generate_seed",
    "retry_generate_provider_request",
    "start_generate_after_authority_change",
    "start_generate_after_recovery",
    "start_generate_reasoning_episode",
    "start_next_generate_step",
]

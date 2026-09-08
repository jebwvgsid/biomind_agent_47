"""Per-invocation private LangGraph loops for model-using workflow phases.

The production graph owns semantic routing, while this module owns only the
lifetime of one local agent transcript.  A parent state snapshot is projected
into a private subgraph; the subgraph may take several reasoning/tool turns,
but only its route-free update, typed outcome, archive reference, and bounded
telemetry are returned to the parent.

The domain node remains responsible for constructing its exact prompt.  During
a scoped invocation it can obtain :class:`PhaseProviderContext` from the
temporary state and use it to turn a rebuilt prompt into a stable
``System + immutable Seed + append-only suffix`` provider request.
"""

from copy import deepcopy
import asyncio
import hashlib
import inspect
import json
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
    TypedDict,
)

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from agent_core.node_context import assert_llm_payload_clean
from agent_core.provider_message_safety import assert_v2_llm_messages_clean
from agent_core.workflow.outcomes import StrictOutcome, route_blind_state_projection
from agent_core.workflow.phase_archive import PhaseArchiveRecord, PhaseArchiveStore
from agent_core.workflow.scoped_context import (
    AppendOnlyProviderMessages,
    FixedProviderMessage,
    ScopedContextError,
    build_provider_messages,
    fixed_provider_message,
    provider_visible_fingerprint,
    provider_visible_payload,
)
from agent_core.workflow.stage_contracts import freeze as freeze_stage_value
from agent_core.workflow.stage_contracts import thaw as thaw_stage_value


PHASE_CONTEXT_RUNTIME_KEY = "_biomind_phase_provider_context"
PHASE_LOOP_SCHEMA_VERSION = 1
MAX_PHASE_SEED_CONTRACT_CHARS = 32_000


_PHASE_INPUT_FIELDS = (
    "input_id", "role", "path", "format", "required", "availability",
)
_PHASE_OUTPUT_FIELDS = (
    "output_id", "role", "path", "format", "required", "path_policy",
    "root_ref",
)
_PHASE_HANDOFF_TEXT_FIELDS = (
    "summary", "requirements_summary", "goal_interpretation",
    "database_strategy",
)
_PHASE_HANDOFF_SEQUENCE_FIELDS = (
    "capability_decisions", "runtime_requirements", "resolved_ambiguities",
    "unresolved_questions",
)


def _allowlisted_rows(value: Any, fields: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {
            key: deepcopy(row[key])
            for key in fields
            if row.get(key) not in (None, "", [], {})
        }
        for row in value or ()
        if isinstance(row, Mapping)
    ]


def _semantic_skill_selection(value: Any) -> list[Any]:
    """Project Skill semantics without source/archive identity."""

    result: list[Any] = []
    if isinstance(value, (str, Mapping)):
        candidates = (value,)
    else:
        candidates = value or ()
    for raw in candidates:
        if isinstance(raw, str):
            if raw.strip():
                result.append(raw)
            continue
        if not isinstance(raw, Mapping):
            continue
        row = {
            key: deepcopy(raw[key])
            for key in ("skill_id", "scope")
            if raw.get(key) not in (None, "", [], {})
        }
        if row:
            result.append(row)
    return result


def project_phase_seed_for_provider(
    seed_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the semantic subset of a private phase seed.

    The full seed remains private and continues to bind invocation identity and
    archive provenance.  This projection is the only seed body serialized into
    a provider-visible message.
    """

    source = dict(seed_contract or {})
    projected: dict[str, Any] = {"schema_version": 1}
    request_summary = str(source.get("request_summary") or "").strip()
    if request_summary:
        projected["request_summary"] = request_summary

    input_manifest = _allowlisted_rows(
        source.get("input_manifest"), _PHASE_INPUT_FIELDS
    )
    if input_manifest:
        projected["input_manifest"] = input_manifest
    output_manifest = _allowlisted_rows(
        source.get("output_manifest"), _PHASE_OUTPUT_FIELDS
    )
    if output_manifest:
        projected["output_manifest"] = output_manifest

    bundle = source.get("selected_skill_bundle")
    if isinstance(bundle, Mapping):
        skills = []
        for raw in bundle.get("skills") or ():
            if not isinstance(raw, Mapping):
                continue
            row = {
                key: deepcopy(raw[key])
                for key in ("skill_id", "scope")
                if raw.get(key) not in (None, "", [], {})
            }
            if row:
                skills.append(row)
        projected["selected_skill_manifest"] = {
            "manifest_only": True,
            "skills": skills,
        }

    handoff = source.get("consultation_handoff")
    if isinstance(handoff, Mapping):
        semantic_handoff: dict[str, Any] = {}
        for key in _PHASE_HANDOFF_TEXT_FIELDS:
            value = handoff.get(key)
            if value not in (None, "", [], {}):
                semantic_handoff[key] = deepcopy(value)
        input_files = _allowlisted_rows(
            handoff.get("input_files"), _PHASE_INPUT_FIELDS
        )
        if input_files:
            semantic_handoff["input_files"] = input_files
        required_outputs = _allowlisted_rows(
            handoff.get("required_outputs"), _PHASE_OUTPUT_FIELDS
        )
        if required_outputs:
            semantic_handoff["required_outputs"] = required_outputs
        selected_skills = _semantic_skill_selection(
            handoff.get("selected_skills")
        )
        if selected_skills:
            semantic_handoff["selected_skills"] = selected_skills
        for key in _PHASE_HANDOFF_SEQUENCE_FIELDS:
            value = handoff.get(key)
            if isinstance(value, (list, tuple)) and value:
                semantic_handoff[key] = deepcopy(list(value))
        if semantic_handoff:
            projected["consultation_handoff"] = semantic_handoff

    # Unlike an open-ended recursive sanitizer, this assertion makes a newly
    # introduced nested control field fail closed instead of silently deleting
    # a potentially meaningful semantic value.
    assert_llm_payload_clean(projected)
    return projected


class PhaseLoopError(ValueError):
    """Fail-closed local-loop protocol violation."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "PHASE_LOOP_ERROR")
        self.detail = str(detail or "")
        super().__init__(
            self.code if not self.detail else f"{self.code}: {self.detail}"
        )


class PhaseEpochBoundaryRequired(RuntimeError):
    """Signal that a changed System policy requires a fresh local epoch."""

    def __init__(self, *, prior_digest: str, next_digest: str) -> None:
        self.prior_digest = str(prior_digest or "")
        self.next_digest = str(next_digest or "")
        super().__init__(
            "PHASE_SYSTEM_POLICY_CHANGED: "
            f"prior={self.prior_digest} next={self.next_digest}"
        )


def _digest_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _message_copy(message: BaseMessage, *, message_id: str) -> BaseMessage:
    if hasattr(message, "model_copy"):
        return message.model_copy(deep=True, update={"id": message_id})
    return message.copy(deep=True, update={"id": message_id})


def _as_provider_observation(message: BaseMessage, *, message_id: str) -> BaseMessage:
    """Normalize legacy mid-loop System messages into ordinary observations."""

    if isinstance(message, RemoveMessage):
        raise PhaseLoopError(
            "PHASE_TRANSCRIPT_DELETE_FORBIDDEN",
            "a private append-only epoch cannot contain RemoveMessage",
        )
    if isinstance(message, SystemMessage):
        return HumanMessage(
            id=message_id,
            content=deepcopy(message.content),
            name=str(getattr(message, "name", None) or "runtime_observation"),
            additional_kwargs=deepcopy(
                dict(getattr(message, "additional_kwargs", {}) or {})
            ),
            response_metadata=deepcopy(
                dict(getattr(message, "response_metadata", {}) or {})
            ),
        )
    return _message_copy(message, message_id=message_id)


class PhaseProviderContext:
    """Ephemeral bridge used only while one domain turn is executing.

    The object is injected into a temporary state passed to the domain node and
    is never written to a LangGraph/checkpoint channel.  It captures the exact
    provider response so the local transcript keeps assistant tool calls and
    provider-visible fields intact even when a legacy adapter later returns a
    normalized ``AIMessage``.  It accepts only the semantic provider seed; the
    private identity/archive seed never crosses into this provider-side object.
    """

    def __init__(
        self,
        *,
        task_id: str = "",
        phase_name: str,
        invocation_id: str,
        epoch_ordinal: int,
        provider_seed_contract: Mapping[str, Any],
        system_message: BaseMessage | None,
        seed_message: BaseMessage | None,
        messages: Sequence[BaseMessage],
        inherited_messages: Sequence[BaseMessage] = (),
        inherited_material_descriptors: Sequence[Mapping[str, Any]] = (),
        inherited_material_records: Sequence[Mapping[str, Any]] = (),
        compaction_handler: Callable[..., Any] | None = None,
        compaction_activation_recorder: Callable[..., Any] | None = None,
        compaction_state: Mapping[str, Any] | None = None,
        selected_revision: int = 0,
        invocation_ordinal: int = 0,
    ) -> None:
        self.task_id = str(task_id or "default")
        self.phase_name = str(phase_name)
        self.invocation_id = str(invocation_id)
        self.epoch_ordinal = int(epoch_ordinal)
        self.epoch_id = f"{self.invocation_id}:e{self.epoch_ordinal}"
        if not isinstance(provider_seed_contract, Mapping):
            raise PhaseLoopError(
                "PHASE_PROVIDER_SEED_INVALID",
                f"received {type(provider_seed_contract).__name__}",
            )
        self.provider_seed_contract = deepcopy(dict(provider_seed_contract))
        self.system_message = system_message
        self.seed_message = seed_message
        self.inherited_messages = list(inherited_messages)
        self.messages = list(messages)
        self.bootstrap_system: BaseMessage | None = None
        self.bootstrap_seed: BaseMessage | None = None
        self.assistant_response: BaseMessage | None = None
        self.provider_messages: list[BaseMessage] = []
        self.pending_provider_events: list[BaseMessage] = []
        self.visible_material_descriptors: list[dict[str, Any]] = []
        self.inherited_material_descriptors = [
            deepcopy(dict(item))
            for item in inherited_material_descriptors or ()
            if isinstance(item, Mapping)
        ]
        self.note_provider_visible_materials(
            self.inherited_material_descriptors
        )
        self.inherited_material_records = [
            deepcopy(dict(item))
            for item in inherited_material_records or ()
            if isinstance(item, Mapping)
        ]
        self.compaction_handler = compaction_handler
        self.compaction_activation_recorder = compaction_activation_recorder
        self.compaction_state = deepcopy(dict(compaction_state or {}))
        self.compaction_telemetry: dict[str, Any] = {}
        self.compaction_triggered = False
        self.selected_revision = max(0, int(selected_revision or 0))
        self.invocation_ordinal = max(0, int(invocation_ordinal or 0))

    def note_provider_visible_materials(
        self, descriptors: Sequence[Mapping[str, Any]]
    ) -> None:
        """Bind canonical material descriptors to this prepared request.

        The domain builder calls this only for bodies it actually places in
        the final wire.  The outer phase owner records the descriptors after a
        successful assistant response, avoiding the false inference that a
        stored snapshot was necessarily sent to the provider.
        """

        for raw in descriptors or ():
            if not isinstance(raw, Mapping):
                raise PhaseLoopError(
                    "PHASE_VISIBLE_MATERIAL_DESCRIPTOR_INVALID",
                    type(raw).__name__,
                )
            item = deepcopy(dict(raw))
            if item not in self.visible_material_descriptors:
                self.visible_material_descriptors.append(item)

    def queue_provider_event(
        self,
        *,
        event_id: str,
        content: str,
        name: str = "phase_event",
    ) -> None:
        """Queue one causal event for the next request and durable suffix.

        ``event_id`` is business identity, not a content hash: replaying the
        same event is idempotent, while two genuine events with equal text are
        retained separately.  The event is committed by the local graph only
        after the turn returns, so request preparation itself has no durable
        side effect.
        """

        identity = str(event_id or "").strip()
        if not identity:
            raise PhaseLoopError("PHASE_EVENT_ID_REQUIRED")
        message_id = (
            f"{self.epoch_id}:event:"
            + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        )
        candidate = HumanMessage(
            id=message_id,
            name=str(name or "phase_event"),
            content=str(content or ""),
            additional_kwargs={"phase_event_id": identity},
        )
        for prior in [*self.messages, *self.pending_provider_events]:
            if str(getattr(prior, "id", None) or "") != message_id:
                continue
            if provider_visible_payload(prior) != provider_visible_payload(candidate):
                raise PhaseLoopError(
                    "PHASE_EVENT_ID_CONFLICT", f"event_id={identity}"
                )
            return
        self.pending_provider_events.append(candidate)

    @staticmethod
    def split_prompt(
        full_prompt: str,
        *,
        dynamic_markers: Sequence[str],
    ) -> tuple[str, str]:
        value = str(full_prompt or "")
        matches = [
            (value.find(marker), marker)
            for marker in dynamic_markers
            if marker and value.find(marker) >= 0
        ]
        if not matches:
            return value, ""
        offset, _marker = min(matches, key=lambda item: item[0])
        return value[:offset].rstrip(), value[offset:]

    def prepare_split_prompt(
        self,
        full_prompt: str,
        *,
        dynamic_markers: Sequence[str],
    ) -> list[BaseMessage]:
        system_prompt, seed_payload = self.split_prompt(
            full_prompt, dynamic_markers=dynamic_markers
        )
        return self.prepare_messages(system_prompt, seed_payload)

    async def prepare_split_prompt_with_compaction(
        self,
        full_prompt: str,
        *,
        dynamic_markers: Sequence[str],
    ) -> list[BaseMessage]:
        system_prompt, seed_payload = self.split_prompt(
            full_prompt, dynamic_markers=dynamic_markers
        )
        return await self.prepare_messages_with_compaction(
            system_prompt, seed_payload
        )

    def prepare_messages(
        self,
        system_prompt: str,
        seed_payload: str,
    ) -> list[BaseMessage]:
        candidate_system = SystemMessage(
            id=f"{self.epoch_id}:system",
            content=str(system_prompt or ""),
        )
        contract_json = json.dumps(
            self.provider_seed_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(contract_json) > MAX_PHASE_SEED_CONTRACT_CHARS:
            raise PhaseLoopError(
                "PHASE_SEED_CONTRACT_TOO_LARGE",
                f"chars={len(contract_json)} limit={MAX_PHASE_SEED_CONTRACT_CHARS}",
            )
        seed_content = (
            '<phase_seed_contract schema_version="1">\n'
            + contract_json
            + "\n</phase_seed_contract>"
        )
        if str(seed_payload or ""):
            seed_content += "\n" + str(seed_payload)
        candidate_seed = HumanMessage(
            id=f"{self.epoch_id}:seed",
            content=seed_content,
        )
        if self.system_message is None:
            self.bootstrap_system = candidate_system
            active_system = candidate_system
        else:
            try:
                active_system = fixed_provider_message(
                    self.system_message, candidate_system
                )
            except ScopedContextError as exc:
                if exc.code != "FIXED_MESSAGE_CHANGED":
                    raise
                raise PhaseEpochBoundaryRequired(
                    prior_digest=provider_visible_fingerprint(self.system_message),
                    next_digest=provider_visible_fingerprint(candidate_system),
                ) from exc

        if self.seed_message is None:
            self.bootstrap_seed = candidate_seed
            active_seed = candidate_seed
        else:
            # Invocation-specific facts are deliberately frozen at the first
            # provider call.  Later domain prompt rebuilds are observations,
            # not permission to rewrite the seed and invalidate its prefix.
            active_seed = self.seed_message

        self.provider_messages = build_provider_messages({
            "system_message": active_system,
            "seed_message": active_seed,
            "inherited_messages": self.inherited_messages,
            "messages": [*self.messages, *self.pending_provider_events],
        })
        assert_v2_llm_messages_clean(
            self.provider_messages,
            stage=f"{self.phase_name}.phase_provider_seed",
        )
        return list(self.provider_messages)

    async def prepare_messages_with_compaction(
        self,
        system_prompt: str,
        seed_payload: str,
    ) -> list[BaseMessage]:
        """Prepare the real wire and compact it at the private phase boundary.

        The synchronous method remains available for legacy/non-schema-2
        callers and tests.  Production scoped phases use this method so the
        compactor sees the complete provider request before dispatch.
        """

        original = self.prepare_messages(system_prompt, seed_payload)
        if self.compaction_handler is None:
            return original
        active_system = self.bootstrap_system or self.system_message
        active_seed = self.bootstrap_seed or self.seed_message
        if not isinstance(active_system, BaseMessage) or not isinstance(
            active_seed, BaseMessage
        ):
            raise PhaseLoopError("PHASE_COMPACTION_FIXED_PREFIX_MISSING")
        prepared = self.compaction_handler(
            task_id=self.task_id,
            phase=self.phase_name,
            invocation_id=self.invocation_id,
            source_epoch_id=self.epoch_id,
            system_message=active_system,
            seed_message=active_seed,
            inherited_messages=list(self.inherited_messages),
            local_messages=list(self.messages),
            pending_messages=list(self.pending_provider_events),
            material_descriptors=deepcopy(self.inherited_material_records),
            prior_state=deepcopy(self.compaction_state),
            selected_revision=self.selected_revision,
        )
        if inspect.isawaitable(prepared):
            prepared = await prepared
        messages = list(getattr(prepared, "messages", ()) or ())
        if len(messages) < 2 or not all(
            isinstance(message, BaseMessage) for message in messages
        ):
            raise PhaseLoopError(
                "PHASE_COMPACTION_RESULT_INVALID", type(prepared).__name__
            )
        # Static System and frozen Seed are protocol authority, never model
        # selected content.  Reject any adapter that changes either one.
        fixed_provider_message(active_system, messages[0])
        fixed_provider_message(active_seed, messages[1])
        assert_v2_llm_messages_clean(
            messages,
            stage=f"{self.phase_name}.phase_provider_compacted",
        )
        next_compaction_state = deepcopy(
            dict(getattr(prepared, "state", {}) or {})
        )
        next_compaction_telemetry = deepcopy(
            dict(getattr(prepared, "telemetry", {}) or {})
        )
        next_compaction_triggered = bool(
            getattr(prepared, "triggered", False)
        )
        if (
            self.compaction_activation_recorder is not None
            and next_compaction_triggered
            and next_compaction_telemetry.get("compaction_committed") is True
        ):
            # The immutable candidate archive is already durable at this
            # point.  Persist its active pointer and material residency before
            # exposing the compacted wire to the business provider.  A CAS
            # failure therefore leaves the previous epoch active and prevents
            # a request from running against an uncommitted working baseline.
            recorded = self.compaction_activation_recorder(
                task_id=self.task_id,
                phase=self.phase_name,
                invocation_id=self.invocation_id,
                invocation_ordinal=self.invocation_ordinal,
                source_epoch_id=self.epoch_id,
                selected_revision=max(
                    int(
                        next_compaction_state.get(
                            "effective_selected_revision"
                        )
                        or (
                            next_compaction_state.get("active_pointer") or {}
                        ).get("selected_revision")
                        or self.selected_revision
                    ),
                    0,
                ),
                compaction_state=deepcopy(next_compaction_state),
                compaction_telemetry=deepcopy(next_compaction_telemetry),
            )
            if inspect.isawaitable(recorded):
                await recorded
        # Publish the new working baseline only after its durable activation
        # record has committed.  If the recorder raises, callers retain the
        # prior context fields and cannot observe or dispatch an uncommitted
        # candidate wire.
        self.provider_messages = messages
        self.compaction_state = next_compaction_state
        self.compaction_telemetry = next_compaction_telemetry
        self.compaction_triggered = next_compaction_triggered
        # A capacity-pressure plan may replace an inherited exact source with
        # a lossy summary.  Visibility receipts must describe the final wire,
        # not the pre-compaction candidate request.  Descriptors added later
        # by the phase (for example a document embedded in its frozen Seed)
        # remain valid because fixed components cannot be model-disposed.
        final_ids = {
            str(getattr(message, "id", None) or "")
            for message in messages
            if str(getattr(message, "id", None) or "")
        }
        retained_inherited_descriptors = [
            descriptor
            for message, descriptor in zip(
                self.inherited_messages,
                self.inherited_material_descriptors,
            )
            if str(getattr(message, "id", None) or "") in final_ids
        ]
        phase_local_descriptors = [
            descriptor
            for descriptor in self.visible_material_descriptors
            if descriptor not in self.inherited_material_descriptors
        ]
        self.visible_material_descriptors = []
        self.note_provider_visible_materials([
            *retained_inherited_descriptors,
            *phase_local_descriptors,
        ])
        return list(self.provider_messages)

    def record_assistant_response(self, message: BaseMessage) -> None:
        if not isinstance(message, BaseMessage):
            raise PhaseLoopError(
                "PHASE_ASSISTANT_RESPONSE_INVALID",
                f"received {type(message).__name__}",
            )
        self.assistant_response = deepcopy(message)

    def audit(self) -> dict[str, Any]:
        return {
            "provider_call_recorded": bool(self.provider_messages),
            "provider_message_count": len(self.provider_messages),
            "system_fingerprint": (
                provider_visible_fingerprint(
                    self.bootstrap_system or self.system_message
                )
                if (self.bootstrap_system or self.system_message) is not None
                else ""
            ),
            "seed_fingerprint": (
                provider_visible_fingerprint(self.bootstrap_seed or self.seed_message)
                if (self.bootstrap_seed or self.seed_message) is not None
                else ""
            ),
            "provider_seed_contract_sha256": _digest_json(
                self.provider_seed_contract
            ),
            "inherited_message_count": len(self.inherited_messages),
            "pending_event_count": len(self.pending_provider_events),
            "visible_material_descriptors": deepcopy(
                self.visible_material_descriptors
            ),
            "provider_request_fingerprint": _digest_json([
                provider_visible_payload(message)
                for message in self.provider_messages
            ]),
            "context_compaction_triggered": self.compaction_triggered,
            "context_compaction": deepcopy(self.compaction_telemetry),
        }


def phase_provider_context(state: Mapping[str, Any]) -> PhaseProviderContext | None:
    value = state.get(PHASE_CONTEXT_RUNTIME_KEY)
    return value if isinstance(value, PhaseProviderContext) else None


def manifest_only_skill_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project Skill identity/headings without passively injecting excerpts.

    Exact selected material is carried in the dedicated inherited input region,
    not duplicated inside the dynamic NodeContext/Seed projection.  New reads
    still arrive as append-only local events.
    """

    source = dict(value or {})
    return {
        "selected_skill_refs": deepcopy(list(
            source.get("selected_skill_refs") or ()
        )),
        "available_skill_headings": deepcopy(list(
            source.get("available_skill_headings") or ()
        )),
        "relevant_skill_excerpts": [],
        "skill_tool_results": [],
    }


def _normalize_turn_messages(
    raw_messages: Sequence[BaseMessage],
    *,
    context: PhaseProviderContext,
    existing_count: int,
) -> list[BaseMessage]:
    pending = list(context.pending_provider_events)
    source: list[BaseMessage] = []
    recorded = context.assistant_response
    if recorded is not None:
        source.append(recorded)

    for message in raw_messages:
        if not isinstance(message, BaseMessage):
            raise PhaseLoopError(
                "PHASE_TURN_MESSAGE_INVALID",
                f"received {type(message).__name__}",
            )
        if recorded is not None and isinstance(message, AIMessage):
            # The exact provider object above is the authoritative assistant
            # wire; legacy normalized AI projections are not appended twice.
            continue
        source.append(message)

    normalized: list[BaseMessage] = [deepcopy(message) for message in pending]
    for offset, message in enumerate(source):
        message_id = (
            f"{context.epoch_id}:message:"
            f"{existing_count + len(pending) + offset:06d}"
        )
        normalized.append(
            _as_provider_observation(message, message_id=message_id)
        )
    return normalized


class _PhaseLoopInput(TypedDict):
    phase_name: str
    invocation_id: str
    invocation_ordinal: int
    epoch_ordinal: int
    seed_contract: dict[str, Any]
    provider_seed_contract: dict[str, Any]
    inherited_messages: list[BaseMessage]
    inherited_material_descriptors: list[dict[str, Any]]
    inherited_material_records: list[dict[str, Any]]
    selected_revision: int
    parent_snapshot: dict[str, Any]
    compaction_state: dict[str, Any]


class _PhaseLoopOutput(TypedDict, total=False):
    parent_update: dict[str, Any]
    phase_audit: dict[str, Any]
    transcript_ref: str
    epoch_boundary: bool


class _PhaseLoopState(TypedDict, total=False):
    phase_name: str
    invocation_id: str
    invocation_ordinal: int
    epoch_ordinal: int
    seed_contract: dict[str, Any]
    provider_seed_contract: dict[str, Any]
    inherited_messages: list[BaseMessage]
    inherited_material_descriptors: list[dict[str, Any]]
    inherited_material_records: list[dict[str, Any]]
    selected_revision: int
    parent_snapshot: dict[str, Any]
    working_update: dict[str, Any]
    system_message: FixedProviderMessage
    seed_message: FixedProviderMessage
    messages: AppendOnlyProviderMessages
    latest_outcome: dict[str, Any]
    turn_count: int
    provider_call_count: int
    parent_update: dict[str, Any]
    phase_audit: dict[str, Any]
    transcript_ref: str
    epoch_boundary: bool
    compaction_state: dict[str, Any]
    provider_transcript_messages: list[BaseMessage]


TurnHandler = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
SeedFactory = Callable[[Mapping[str, Any]], Mapping[str, Any]]
SeedProjector = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ParentProjector = Callable[[Mapping[str, Any]], Mapping[str, Any]]
InheritedMaterialFactory = Callable[
    [Mapping[str, Any]], Sequence[Mapping[str, Any]]
]
SelectedRevisionFactory = Callable[[Mapping[str, Any]], int]
MaterialVisibilityRecorder = Callable[..., Mapping[str, Any] | None]
PhaseCompactionHandler = Callable[..., Any]
PhaseCompactionActivationRecorder = Callable[..., Mapping[str, Any] | None]
ArchiveWriter = Callable[..., str]


def inherited_skill_material_messages(
    materials: Sequence[Mapping[str, Any]],
    *,
    epoch_id: str,
) -> list[HumanMessage]:
    """Render verified carry records as ordinary, frozen source messages."""

    result: list[HumanMessage] = []
    seen: set[str] = set()
    for index, raw in enumerate(materials or ()):
        if not isinstance(raw, Mapping):
            raise PhaseLoopError(
                "PHASE_INHERITED_MATERIAL_INVALID",
                f"materials[{index}] is {type(raw).__name__}",
            )
        item = deepcopy(dict(raw))
        material_id = str(item.get("material_id") or "").strip()
        residency = str(item.get("residency") or "exact").strip()
        if not material_id or residency not in {
            "exact", "summarized", "externalized",
        }:
            raise PhaseLoopError(
                "PHASE_INHERITED_MATERIAL_INVALID",
                f"materials[{index}] lacks valid identity/residency",
            )
        if material_id in seen:
            continue
        seen.add(material_id)
        metadata = {
            key: deepcopy(value)
            for key, value in item.items()
            if key not in {"body", "summary", "missing_details"}
            and value not in (None, "", [], {})
        }
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if residency == "exact":
            if "body" not in item:
                raise PhaseLoopError(
                    "PHASE_INHERITED_MATERIAL_BODY_MISSING", material_id
                )
            body = str(item.get("body") or "")
            content = (
                '<inherited_skill_material schema_version="1">\n'
                + metadata_json
                + "\n---BEGIN EXACT SOURCE BODY---\n"
                + body
                + "\n---END EXACT SOURCE BODY---\n"
                + "</inherited_skill_material>"
            )
        else:
            summary = str(item.get("summary") or "").strip()
            retrieval_ref = str(item.get("retrieval_ref") or "").strip()
            if not summary or not retrieval_ref:
                raise PhaseLoopError(
                    "PHASE_INHERITED_MATERIAL_RECOVERY_MISSING", material_id
                )
            content = (
                '<inherited_skill_material_state schema_version="1">\n'
                + metadata_json
                + "\n<summary>\n"
                + summary
                + "\n</summary>\n<missing_details>\n"
                + str(item.get("missing_details") or "")
                + "\n</missing_details>\n"
                + "</inherited_skill_material_state>"
            )
        result.append(HumanMessage(
            id=f"{epoch_id}:inherited:{material_id}",
            name="inherited_skill_material",
            content=content,
        ))
    return result


def archive_writer_for_store(store: PhaseArchiveStore) -> ArchiveWriter:
    """Adapt the neutral immutable archive store to the local-loop callback."""

    if not isinstance(store, PhaseArchiveStore):
        raise PhaseLoopError(
            "PHASE_ARCHIVE_STORE_INVALID", type(store).__name__
        )

    def write_archive(**value: Any) -> str:
        outcome = dict(value.get("outcome") or {})
        payload = dict(outcome.get("payload") or {})
        logical_invocation_id = str(value.get("invocation_id") or "unknown")
        epoch_ordinal = int(value.get("epoch_ordinal") or 1)
        archive_attempt_digest = _digest_json({
            "phase_invocation_id": logical_invocation_id,
            "epoch_ordinal": epoch_ordinal,
            "seed_contract": dict(value.get("seed_contract") or {}),
            "transcript": list(value.get("transcript") or ()),
            "outcome": outcome,
            "reason": str(value.get("reason") or "phase_outcome"),
        })
        artifact_refs = [
            str(item)
            for item in (
                payload.get("artifact_refs")
                or payload.get("artifacts")
                or ()
            )
            if str(item)
        ]
        evidence_refs = [
            str(item)
            for item in outcome.get("evidence_refs") or ()
            if str(item)
        ]
        record = PhaseArchiveRecord(
            task_id=str(value.get("task_id") or "default"),
            phase=str(value.get("phase") or "unknown"),
            invocation_id=(
                f"{logical_invocation_id}:e{epoch_ordinal}:"
                f"a{archive_attempt_digest[:16]}"
            ),
            summary={
                "reason": str(value.get("reason") or "phase_outcome"),
                "outcome_type": str(outcome.get("type") or ""),
                "summary": str(outcome.get("summary") or ""),
            },
            artifacts=artifact_refs,
            evidence=evidence_refs,
            transcript=list(value.get("transcript") or ()),
            metadata={
                "phase_invocation_id": logical_invocation_id,
                "epoch_ordinal": epoch_ordinal,
                "archive_attempt_digest": archive_attempt_digest,
                "seed_contract": dict(value.get("seed_contract") or {}),
            },
        )
        return store.write(record).archive_ref

    return write_archive


def _harness_outcome(
    contract: type[StrictOutcome],
    *,
    task_id: str,
    code: str,
    detail: str,
    exception_class: str = "PhaseLoopError",
) -> dict[str, Any]:
    return contract(
        schema_version=1,
        type="harness_error",
        summary="The private phase loop failed closed.",
        scope={"task_id": str(task_id or "default")},
        payload={
            "error_code": str(code),
            "exception_class": str(exception_class or "PhaseLoopError"),
            "detail": str(detail)[:800],
        },
    ).to_dict()


def _build_epoch_graph(
    *,
    phase_name: str,
    turn_handler: TurnHandler,
    outcome_field: str,
    outcome_contract: type[StrictOutcome],
    continue_types: frozenset[str],
    max_turns: int,
    archive_writer: ArchiveWriter | None,
    material_visibility_recorder: MaterialVisibilityRecorder | None,
    compaction_handler: PhaseCompactionHandler | None,
    compaction_activation_recorder: PhaseCompactionActivationRecorder | None,
):
    async def run_turn(state: _PhaseLoopState) -> dict[str, Any]:
        parent_snapshot = dict(state.get("parent_snapshot") or {})
        working_update = dict(state.get("working_update") or {})
        working = {**parent_snapshot, **working_update}
        existing_messages = list(state.get("messages") or [])
        seed_message = state.get("seed_message")
        domain_messages = (
            [seed_message, *existing_messages]
            if isinstance(seed_message, BaseMessage)
            else existing_messages
        )
        working["messages"] = domain_messages
        raw_provider_seed = state.get("provider_seed_contract")
        if not isinstance(raw_provider_seed, Mapping):
            raise PhaseLoopError(
                "PHASE_PROVIDER_SEED_INVALID",
                f"received {type(raw_provider_seed).__name__}",
            )
        context = PhaseProviderContext(
            task_id=str(parent_snapshot.get("task_id") or "default"),
            phase_name=phase_name,
            invocation_id=str(state.get("invocation_id") or ""),
            invocation_ordinal=int(state.get("invocation_ordinal") or 0),
            epoch_ordinal=int(state.get("epoch_ordinal") or 1),
            provider_seed_contract=raw_provider_seed,
            system_message=(
                state.get("system_message")
                if isinstance(state.get("system_message"), BaseMessage)
                else None
            ),
            seed_message=(
                seed_message if isinstance(seed_message, BaseMessage) else None
            ),
            inherited_messages=[
                message
                for message in state.get("inherited_messages") or []
                if isinstance(message, BaseMessage)
            ],
            inherited_material_descriptors=[
                dict(item)
                for item in state.get("inherited_material_descriptors") or []
                if isinstance(item, Mapping)
            ],
            inherited_material_records=[
                dict(item)
                for item in state.get("inherited_material_records") or []
                if isinstance(item, Mapping)
            ],
            compaction_handler=compaction_handler,
            compaction_activation_recorder=compaction_activation_recorder,
            compaction_state=dict(state.get("compaction_state") or {}),
            selected_revision=max((
                int(state.get("selected_revision") or 0),
                *[
                    int(item.get("selected_at_task_revision") or 0)
                    for item in state.get("inherited_material_records") or []
                    if isinstance(item, Mapping)
                ],
            )),
            messages=existing_messages,
        )
        working[PHASE_CONTEXT_RUNTIME_KEY] = context
        try:
            raw = await turn_handler(working)
        except PhaseEpochBoundaryRequired as exc:
            return {
                "parent_update": working_update,
                "phase_audit": {
                    "schema_version": PHASE_LOOP_SCHEMA_VERSION,
                    "phase": phase_name,
                    "invocation_id": str(state.get("invocation_id") or ""),
                    "epoch_ordinal": int(state.get("epoch_ordinal") or 1),
                    "turn_count": int(state.get("turn_count") or 0),
                    "provider_call_count": int(
                        state.get("provider_call_count") or 0
                    ),
                    "boundary_reason": "system_policy_changed",
                    "prior_system_fingerprint": exc.prior_digest,
                    "next_system_fingerprint": exc.next_digest,
                },
                "epoch_boundary": True,
            }
        if not isinstance(raw, Mapping):
            raise PhaseLoopError(
                "PHASE_TURN_RESULT_INVALID",
                f"{phase_name} returned {type(raw).__name__}",
            )
        result = dict(raw)
        turn_audit = context.audit()
        if (
            material_visibility_recorder is not None
            and context.assistant_response is not None
            and context.visible_material_descriptors
        ):
            recorded = material_visibility_recorder(
                phase=phase_name,
                invocation_id=str(state.get("invocation_id") or ""),
                epoch_id=context.epoch_id,
                request_fingerprint=str(
                    turn_audit.get("provider_request_fingerprint") or ""
                ),
                descriptors=deepcopy(context.visible_material_descriptors),
                parent_state=deepcopy(parent_snapshot),
            )
            if inspect.isawaitable(recorded):
                recorded = await recorded
            if isinstance(recorded, Mapping):
                result.update(deepcopy(dict(recorded)))
        raw_outcome = result.get(outcome_field)
        outcome = (
            raw_outcome
            if isinstance(raw_outcome, outcome_contract)
            else outcome_contract.from_mapping(raw_outcome or {})
        )
        result[outcome_field] = outcome.to_dict()
        raw_messages = list(result.pop("messages", []) or [])
        result.pop(PHASE_CONTEXT_RUNTIME_KEY, None)
        next_working = {**working_update, **result}
        additions = _normalize_turn_messages(
            raw_messages,
            context=context,
            existing_count=len(existing_messages),
        )
        provider_transcript = list(context.provider_messages)
        provider_ids = {
            str(getattr(message, "id", None) or "")
            for message in provider_transcript
            if str(getattr(message, "id", None) or "")
        }
        for addition in additions:
            message_id = str(getattr(addition, "id", None) or "")
            if message_id and message_id in provider_ids:
                continue
            provider_transcript.append(addition)
            if message_id:
                provider_ids.add(message_id)
        update: dict[str, Any] = {
            "working_update": next_working,
            "latest_outcome": outcome.to_dict(),
            "messages": additions,
            "turn_count": int(state.get("turn_count") or 0) + 1,
            "provider_call_count": int(state.get("provider_call_count") or 0)
            + (1 if context.provider_messages else 0),
            "phase_audit": {
                **dict(state.get("phase_audit") or {}),
                "last_provider_request": turn_audit,
            },
            "epoch_boundary": False,
            "compaction_state": deepcopy(context.compaction_state),
            "provider_transcript_messages": provider_transcript,
        }
        if context.bootstrap_system is not None:
            update["system_message"] = context.bootstrap_system
        if context.bootstrap_seed is not None:
            update["seed_message"] = context.bootstrap_seed
        return update

    async def route_after_turn(state: _PhaseLoopState) -> str:
        if state.get("epoch_boundary"):
            return "finish"
        outcome = outcome_contract.from_mapping(state.get("latest_outcome") or {})
        if outcome.type in continue_types:
            if int(state.get("turn_count") or 0) >= max_turns:
                return "turn_limit"
            return "turn"
        return "finish"

    async def turn_limit(state: _PhaseLoopState) -> dict[str, Any]:
        """Produce the bounded failure on the graph's async execution path.

        LangGraph runs synchronous nodes in the event loop's default executor.
        Keeping this tiny terminal-adjacent node async avoids leaving an idle
        executor worker behind when an isolated event loop is closed after the
        local-turn limit fires.
        """
        parent = dict(state.get("parent_snapshot") or {})
        working = dict(state.get("working_update") or {})
        outcome = _harness_outcome(
            outcome_contract,
            task_id=str(parent.get("task_id") or "default"),
            code="PHASE_LOCAL_TURN_LIMIT",
            detail=f"{phase_name} exceeded {max_turns} local turns",
        )
        working[outcome_field] = outcome
        return {"working_update": working, "latest_outcome": outcome}

    async def finish(state: _PhaseLoopState) -> dict[str, Any]:
        """Seal one epoch without crossing an async-to-thread executor boundary.

        Archive writers perform small synchronous durability operations.  Keeping
        this LangGraph node async ensures those operations finish on the graph's
        event-loop path; some LangGraph 1.0.x cyclic async executions otherwise
        wait indefinitely while finalizing a synchronous terminal node.
        """
        transcript: list[dict[str, Any]] = []
        actual_transcript = list(state.get("provider_transcript_messages") or [])
        if not actual_transcript:
            actual_transcript = [
                state.get("system_message"),
                state.get("seed_message"),
                *list(state.get("inherited_messages") or []),
                *list(state.get("messages") or []),
            ]
        for message in actual_transcript:
            if isinstance(message, BaseMessage):
                archived = provider_visible_payload(message)
                archived["message_id"] = str(
                    getattr(message, "id", None) or ""
                )
                archived["content_sha256"] = hashlib.sha256(
                    str(getattr(message, "content", "") or "").encode(
                        "utf-8", errors="replace"
                    )
                ).hexdigest()
                transcript.append(archived)
        latest = dict(state.get("latest_outcome") or {})
        audit = {
            **dict(state.get("phase_audit") or {}),
            "schema_version": PHASE_LOOP_SCHEMA_VERSION,
            "phase": phase_name,
            "invocation_id": str(state.get("invocation_id") or ""),
            "epoch_ordinal": int(state.get("epoch_ordinal") or 1),
            "turn_count": int(state.get("turn_count") or 0),
            "provider_call_count": int(state.get("provider_call_count") or 0),
            "provider_message_count": len(transcript),
            "inherited_message_count": len(
                list(state.get("inherited_messages") or [])
            ),
            "seed_contract_sha256": _digest_json(
                state.get("seed_contract") or {}
            ),
            "provider_seed_contract_sha256": _digest_json(
                state.get("provider_seed_contract") or {}
            ),
            "outcome_type": str(latest.get("type") or ""),
            "epoch_boundary": bool(state.get("epoch_boundary")),
        }
        ref = ""
        if archive_writer is not None:
            ref = str(archive_writer(
                task_id=str(
                    (state.get("parent_snapshot") or {}).get("task_id")
                    or "default"
                ),
                phase=phase_name,
                invocation_id=str(state.get("invocation_id") or ""),
                epoch_ordinal=int(state.get("epoch_ordinal") or 1),
                seed_contract=deepcopy(dict(state.get("seed_contract") or {})),
                transcript=deepcopy(transcript),
                outcome=deepcopy(latest),
                reason=(
                    "system_policy_changed"
                    if state.get("epoch_boundary")
                    else "phase_outcome"
                ),
            ) or "")
        return {
            "parent_update": dict(state.get("working_update") or {}),
            "phase_audit": audit,
            "transcript_ref": ref,
            "epoch_boundary": bool(state.get("epoch_boundary")),
        }

    graph = StateGraph(
        _PhaseLoopState,
        input_schema=_PhaseLoopInput,
        output_schema=_PhaseLoopOutput,
    )
    graph.add_node("turn", run_turn)
    graph.add_node("turn_limit", turn_limit)
    graph.add_node("finish", finish)
    graph.add_edge(START, "turn")
    graph.add_conditional_edges(
        "turn", route_after_turn, ["turn", "turn_limit", "finish"]
    )
    graph.add_edge("turn_limit", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=False)


def build_scoped_phase_node(
    *,
    phase_name: str,
    turn_handler: TurnHandler,
    outcome_field: str,
    outcome_contract: type[StrictOutcome],
    continue_types: Sequence[str],
    seed_factory: SeedFactory,
    provider_seed_projector: SeedProjector,
    inherited_material_factory: InheritedMaterialFactory | None = None,
    selected_revision_factory: SelectedRevisionFactory | None = None,
    material_visibility_recorder: MaterialVisibilityRecorder | None = None,
    compaction_handler: PhaseCompactionHandler | None = None,
    compaction_activation_recorder: PhaseCompactionActivationRecorder | None = None,
    parent_projector: ParentProjector | None = None,
    max_turns: int = 32,
    max_epochs: int = 4,
    archive_writer: ArchiveWriter | None = None,
) -> TurnHandler:
    """Wrap a one-turn typed handler in a private per-invocation subgraph."""

    phase = str(phase_name or "").strip()
    if not phase:
        raise PhaseLoopError("PHASE_NAME_REQUIRED")
    max_turns = max(1, int(max_turns))
    max_epochs = max(1, int(max_epochs))
    continuing = frozenset(str(item) for item in continue_types)
    epoch_graph = _build_epoch_graph(
        phase_name=phase,
        turn_handler=turn_handler,
        outcome_field=outcome_field,
        outcome_contract=outcome_contract,
        continue_types=continuing,
        max_turns=max_turns,
        archive_writer=archive_writer,
        material_visibility_recorder=material_visibility_recorder,
        compaction_handler=compaction_handler,
        compaction_activation_recorder=compaction_activation_recorder,
    )

    async def scoped_node(
        state: Mapping[str, Any], config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        route_blind_parent = route_blind_state_projection(state)
        route_blind_parent.pop("messages", None)
        if parent_projector is None:
            parent = route_blind_parent
        else:
            projected = parent_projector(route_blind_parent)
            if not isinstance(projected, Mapping):
                raise PhaseLoopError(
                    "PHASE_PARENT_PROJECTION_INVALID",
                    f"received {type(projected).__name__}",
                )
            parent = dict(projected)
            parent.pop("messages", None)
        ordinals = dict(parent.get("phase_invocation_ordinals") or {})
        invocation_ordinal = int(ordinals.get(phase) or 0) + 1
        seed_contract = thaw_stage_value(
            freeze_stage_value(dict(seed_factory(parent) or {}))
        )
        # The projector is deliberately untrusted with respect to the private
        # identity contract.  It receives a copy so an accidental in-place
        # mutation cannot alter invocation or archive identity.
        try:
            raw_provider_seed = provider_seed_projector(deepcopy(seed_contract))
            if not isinstance(raw_provider_seed, Mapping):
                raise PhaseLoopError(
                    "PHASE_PROVIDER_SEED_INVALID",
                    f"received {type(raw_provider_seed).__name__}",
                )
            provider_seed_contract = thaw_stage_value(
                freeze_stage_value(dict(raw_provider_seed))
            )
            # Reject an unsafe custom projector before entering the first local
            # turn.  Complete provider messages are checked again at dispatch.
            assert_llm_payload_clean(provider_seed_contract)
        except GraphBubbleUp:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = str(
                getattr(exc, "error_code", None)
                or getattr(exc, "code", None)
                or "PHASE_PROVIDER_SEED_PROJECTION_FAILED"
            )
            return {
                outcome_field: _harness_outcome(
                    outcome_contract,
                    task_id=str(parent.get("task_id") or "default"),
                    code=error_code,
                    detail=f"{type(exc).__name__}: {exc}",
                    exception_class=type(exc).__name__,
                ),
            }
        identity = {
            "schema_version": PHASE_LOOP_SCHEMA_VERSION,
            "task_id": str(parent.get("task_id") or "default"),
            "phase": phase,
            "invocation_ordinal": invocation_ordinal,
            "seed_contract": seed_contract,
        }
        invocation_id = f"{phase}-" + _digest_json(identity)[:24]
        try:
            inherited_materials = list(
                inherited_material_factory(parent)
                if inherited_material_factory is not None
                else ()
            )
        except GraphBubbleUp:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = str(
                getattr(exc, "error_code", None)
                or getattr(exc, "code", None)
                or "PHASE_INHERITED_MATERIAL_PROJECTION_FAILED"
            )
            return {
                outcome_field: _harness_outcome(
                    outcome_contract,
                    task_id=str(parent.get("task_id") or "default"),
                    code=error_code,
                    detail=f"{type(exc).__name__}: {exc}",
                    exception_class=type(exc).__name__,
                ),
            }
        try:
            selected_revision = max((
                int(
                    selected_revision_factory(parent)
                    if selected_revision_factory is not None
                    else 0
                ),
                *[
                    int(item.get("selected_at_task_revision") or 0)
                    for item in inherited_materials
                    if isinstance(item, Mapping)
                ],
            ))
        except GraphBubbleUp:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = str(
                getattr(exc, "error_code", None)
                or getattr(exc, "code", None)
                or "PHASE_SELECTION_REVISION_PROJECTION_FAILED"
            )
            return {
                outcome_field: _harness_outcome(
                    outcome_contract,
                    task_id=str(parent.get("task_id") or "default"),
                    code=error_code,
                    detail=f"{type(exc).__name__}: {exc}",
                    exception_class=type(exc).__name__,
                ),
            }
        combined_update: dict[str, Any] = {}
        epoch_audits: list[dict[str, Any]] = []
        transcript_refs: list[str] = []
        final_result: dict[str, Any] = {}

        for epoch_ordinal in range(1, max_epochs + 1):
            epoch_id = f"{invocation_id}:e{epoch_ordinal}"
            inherited_messages = inherited_skill_material_messages(
                inherited_materials,
                epoch_id=epoch_id,
            )
            child_config = dict(config or {})
            child_config["recursion_limit"] = max(
                int(child_config.get("recursion_limit") or 0),
                max_turns * 2 + 8,
            )
            result = await epoch_graph.ainvoke(
                {
                    "phase_name": phase,
                    "invocation_id": invocation_id,
                    "invocation_ordinal": invocation_ordinal,
                    "epoch_ordinal": epoch_ordinal,
                    "seed_contract": seed_contract,
                    "provider_seed_contract": provider_seed_contract,
                    "inherited_messages": inherited_messages,
                    "inherited_material_descriptors": [
                        (
                            {
                                "kind": "excerpt",
                                "excerpt_id": str(item.get("excerpt_id") or ""),
                                "content_hash": str(item.get("content_hash") or ""),
                            }
                            if str(item.get("excerpt_id") or "")
                            else {
                                "kind": "skill_document",
                                "snapshot_id": str(item.get("snapshot_id") or ""),
                                "content_hash": str(item.get("content_hash") or ""),
                            }
                        )
                        for item in inherited_materials
                        if isinstance(item, Mapping)
                        and str(item.get("residency") or "exact") == "exact"
                    ],
                    "inherited_material_records": [
                        {
                            key: deepcopy(value)
                            for key, value in dict(item).items()
                            if key != "body"
                        }
                        for item in inherited_materials
                        if isinstance(item, Mapping)
                    ],
                    "selected_revision": selected_revision,
                    "parent_snapshot": {**parent, **combined_update},
                    "compaction_state": {},
                },
                config=child_config,
            )
            final_result = dict(result or {})
            combined_update.update(dict(final_result.get("parent_update") or {}))
            if isinstance(final_result.get("phase_audit"), Mapping):
                epoch_audits.append(dict(final_result["phase_audit"]))
            if str(final_result.get("transcript_ref") or ""):
                transcript_refs.append(str(final_result["transcript_ref"]))
            if not final_result.get("epoch_boundary"):
                break
        else:
            combined_update[outcome_field] = _harness_outcome(
                outcome_contract,
                task_id=str(parent.get("task_id") or "default"),
                code="PHASE_LOCAL_EPOCH_LIMIT",
                detail=f"{phase} exceeded {max_epochs} System-policy epochs",
            )

        ordinals[phase] = invocation_ordinal
        prior_audit = dict(parent.get("phase_context_audit") or {})
        prior_audit[phase] = {
            "schema_version": PHASE_LOOP_SCHEMA_VERSION,
            "invocation_id": invocation_id,
            "invocation_ordinal": invocation_ordinal,
            "epoch_count": len(epoch_audits),
            "epochs": epoch_audits,
            "transcript_refs": transcript_refs,
            "transcript_in_parent_state": False,
        }
        combined_update["phase_invocation_ordinals"] = ordinals
        combined_update["phase_context_audit"] = prior_audit
        prior_refs = dict(parent.get("phase_archive_refs") or {})
        if transcript_refs:
            prior_refs[phase] = transcript_refs[-1]
        combined_update["phase_archive_refs"] = prior_refs
        combined_update.pop("messages", None)
        combined_update.pop(PHASE_CONTEXT_RUNTIME_KEY, None)
        return combined_update

    scoped_node.__name__ = f"scoped_{phase}_phase"
    return scoped_node


__all__ = [
    "PHASE_CONTEXT_RUNTIME_KEY",
    "PHASE_LOOP_SCHEMA_VERSION",
    "ParentProjector",
    "SeedProjector",
    "InheritedMaterialFactory",
    "SelectedRevisionFactory",
    "MaterialVisibilityRecorder",
    "PhaseCompactionHandler",
    "PhaseEpochBoundaryRequired",
    "PhaseLoopError",
    "PhaseProviderContext",
    "archive_writer_for_store",
    "build_scoped_phase_node",
    "inherited_skill_material_messages",
    "manifest_only_skill_context",
    "phase_provider_context",
    "project_phase_seed_for_provider",
]

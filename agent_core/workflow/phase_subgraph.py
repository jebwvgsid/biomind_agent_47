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
        phase_name: str,
        invocation_id: str,
        epoch_ordinal: int,
        provider_seed_contract: Mapping[str, Any],
        system_message: BaseMessage | None,
        seed_message: BaseMessage | None,
        messages: Sequence[BaseMessage],
    ) -> None:
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
        self.messages = list(messages)
        self.bootstrap_system: BaseMessage | None = None
        self.bootstrap_seed: BaseMessage | None = None
        self.assistant_response: BaseMessage | None = None
        self.provider_messages: list[BaseMessage] = []

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
            "messages": self.messages,
        })
        if self.seed_message is not None and str(seed_payload or "").strip():
            # The immutable seed is a provider-prefix contract, not a reason to
            # hide later local-agent observations.  Rebuilt Planner correction
            # context is appended as a turn-local observation so validation
            # evidence, the corrected candidate, and Skill effect contracts are
            # visible without mutating the original seed.
            initial_seed = str(getattr(active_seed, "content", "") or "")
            latest_payload = str(seed_payload).strip()
            if latest_payload not in initial_seed:
                self.provider_messages.append(HumanMessage(
                    id=f"{self.epoch_id}:runtime-observation",
                    content=(
                        '<phase_runtime_observation schema_version="1">\n'
                        + latest_payload
                        + "\n</phase_runtime_observation>"
                    ),
                ))
        assert_v2_llm_messages_clean(
            self.provider_messages,
            stage=f"{self.phase_name}.phase_provider_seed",
        )
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
        }


def phase_provider_context(state: Mapping[str, Any]) -> PhaseProviderContext | None:
    value = state.get(PHASE_CONTEXT_RUNTIME_KEY)
    return value if isinstance(value, PhaseProviderContext) else None


def manifest_only_skill_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project Skill identity/headings without passively injecting excerpts.

    A scoped Planner receives exact Skill text only as the append-only result of
    an explicit Skill inspection action.  Previously loaded excerpts and tool
    results remain durable domain state, but are not copied into a fresh phase
    seed merely because another node loaded them earlier.
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

    normalized: list[BaseMessage] = []
    for offset, message in enumerate(source):
        message_id = f"{context.epoch_id}:message:{existing_count + offset:06d}"
        normalized.append(
            _as_provider_observation(message, message_id=message_id)
        )
    return normalized


class _PhaseLoopInput(TypedDict):
    phase_name: str
    invocation_id: str
    epoch_ordinal: int
    seed_contract: dict[str, Any]
    provider_seed_contract: dict[str, Any]
    parent_snapshot: dict[str, Any]


class _PhaseLoopOutput(TypedDict, total=False):
    parent_update: dict[str, Any]
    phase_audit: dict[str, Any]
    transcript_ref: str
    epoch_boundary: bool


class _PhaseLoopState(TypedDict, total=False):
    phase_name: str
    invocation_id: str
    epoch_ordinal: int
    seed_contract: dict[str, Any]
    provider_seed_contract: dict[str, Any]
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


TurnHandler = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
SeedFactory = Callable[[Mapping[str, Any]], Mapping[str, Any]]
SeedProjector = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ParentProjector = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ArchiveWriter = Callable[..., str]


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
            phase_name=phase_name,
            invocation_id=str(state.get("invocation_id") or ""),
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
        update: dict[str, Any] = {
            "working_update": next_working,
            "latest_outcome": outcome.to_dict(),
            "messages": additions,
            "turn_count": int(state.get("turn_count") or 0) + 1,
            "provider_call_count": int(state.get("provider_call_count") or 0)
            + (1 if context.provider_messages else 0),
            "epoch_boundary": False,
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
        for message in (
            state.get("system_message"),
            state.get("seed_message"),
            *list(state.get("messages") or []),
        ):
            if isinstance(message, BaseMessage):
                transcript.append(provider_visible_payload(message))
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
        combined_update: dict[str, Any] = {}
        epoch_audits: list[dict[str, Any]] = []
        transcript_refs: list[str] = []
        final_result: dict[str, Any] = {}

        for epoch_ordinal in range(1, max_epochs + 1):
            child_config = dict(config or {})
            child_config["recursion_limit"] = max(
                int(child_config.get("recursion_limit") or 0),
                max_turns * 2 + 8,
            )
            result = await epoch_graph.ainvoke(
                {
                    "phase_name": phase,
                    "invocation_id": invocation_id,
                    "epoch_ordinal": epoch_ordinal,
                    "seed_contract": seed_contract,
                    "provider_seed_contract": provider_seed_contract,
                    "parent_snapshot": {**parent, **combined_update},
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
    "PhaseEpochBoundaryRequired",
    "PhaseLoopError",
    "PhaseProviderContext",
    "archive_writer_for_store",
    "build_scoped_phase_node",
    "manifest_only_skill_context",
    "phase_provider_context",
    "project_phase_seed_for_provider",
]

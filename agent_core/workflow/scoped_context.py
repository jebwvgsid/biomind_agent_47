"""Private, append-only message context for one agent phase.

The parent workflow should pass a phase seed into a subgraph and receive only a
typed outcome back.  The subgraph's model transcript stays on the private
``messages`` channel.  This keeps Consultant, Planner, and individual
StepWorker histories from becoming one global conversation while preserving a
stable provider prefix inside each local loop.

Parallel tool nodes must not write directly to ``messages``.  Collect their
``ToolMessage`` objects on a fan-in channel, then call
``append_parallel_tool_messages`` once in a join node.  An append-only reducer
cannot repair nondeterministic parallel arrival order after it has committed a
message.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Any, Mapping, Sequence, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    convert_to_messages,
)


class ScopedContextError(ValueError):
    """Fail-closed violation of the private phase-context contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "SCOPED_CONTEXT_ERROR")
        self.detail = str(detail or "")
        super().__init__(
            self.code if not self.detail else f"{self.code}: {self.detail}"
        )


def _json_value(value: Any, *, path: str) -> Any:
    """Return a deterministic JSON value or reject unsupported wire content."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ScopedContextError(
                "PROVIDER_VISIBLE_PAYLOAD_INVALID", f"{path} is non-finite"
            )
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item, path=f"{path}.{key}")
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ScopedContextError(
        "PROVIDER_VISIBLE_PAYLOAD_INVALID",
        f"{path} has unsupported type {type(value).__name__}",
    )


def _coerce_messages(value: Any, *, field: str) -> list[BaseMessage]:
    if value is None:
        return []
    if isinstance(value, (BaseMessage, Mapping)):
        raw = [value]
    else:
        try:
            raw = list(value)
        except TypeError as exc:
            raise ScopedContextError(
                "MESSAGE_VALUE_INVALID",
                f"{field} must be a message or a sequence of messages",
            ) from exc
    try:
        messages = list(convert_to_messages(raw))
    except Exception as exc:
        raise ScopedContextError(
            "MESSAGE_VALUE_INVALID", f"{field}: {type(exc).__name__}: {exc}"
        ) from exc
    if not all(isinstance(message, BaseMessage) for message in messages):
        raise ScopedContextError(
            "MESSAGE_VALUE_INVALID", f"{field} contains a non-message value"
        )
    return messages


def _message_id(message: BaseMessage, *, field: str) -> str:
    message_id = str(getattr(message, "id", None) or "").strip()
    if not message_id:
        raise ScopedContextError(
            "MESSAGE_ID_REQUIRED", f"{field} message has no stable id"
        )
    return message_id


def _tool_calls(message: BaseMessage) -> Any:
    normalized = getattr(message, "tool_calls", None)
    if normalized:
        return normalized
    additional = getattr(message, "additional_kwargs", None)
    if isinstance(additional, Mapping) and additional.get("tool_calls"):
        return additional["tool_calls"]
    return None


def provider_visible_payload(message: BaseMessage | Mapping[str, Any]) -> dict[str, Any]:
    """Project exactly the fields that can affect the provider message wire.

    Message identity is checked separately by the reducer.  Usage metadata,
    response metadata, latency, timestamps, cache counters, tracing fields, and
    all other bookkeeping are deliberately excluded.
    """

    values = _coerce_messages(message, field="provider_visible_payload")
    if len(values) != 1:
        raise ScopedContextError(
            "MESSAGE_VALUE_INVALID", "provider_visible_payload requires one message"
        )
    current = values[0]
    payload: dict[str, Any] = {
        "type": str(getattr(current, "type", "") or ""),
        "content": _json_value(current.content, path="content"),
    }
    role = str(getattr(current, "role", None) or "").strip()
    if role:
        payload["role"] = role
    name = str(getattr(current, "name", None) or "").strip()
    if name:
        payload["name"] = name
    calls = _tool_calls(current)
    if calls:
        payload["tool_calls"] = _json_value(calls, path="tool_calls")
    # These native protocol fields affect replay and must be budgeted. They
    # are never public CLI explanation text; typed reasoning blocks in content
    # are already preserved above.
    native = getattr(current, "additional_kwargs", {}) or {}
    protocol_fields = {key: _json_value(native[key], path="additional_kwargs." + key)
        for key in ("reasoning_content", "reasoning", "thinking", "signature", "redacted_thinking", "function_call")
        if key in native}
    if protocol_fields:
        payload["additional_kwargs"] = protocol_fields
    tool_call_id = str(getattr(current, "tool_call_id", None) or "").strip()
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    return payload


def provider_visible_fingerprint(
    message: BaseMessage | Mapping[str, Any],
) -> str:
    encoded = json.dumps(
        provider_visible_payload(message),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fixed_provider_message(
    left: BaseMessage | Mapping[str, Any] | None,
    right: BaseMessage | Mapping[str, Any] | None,
) -> BaseMessage:
    """Reducer for the immutable system and seed channels."""

    old = _coerce_messages(left, field="fixed.left")
    new = _coerce_messages(right, field="fixed.right")
    if len(new) != 1:
        raise ScopedContextError(
            "FIXED_MESSAGE_INVALID", "a fixed channel update must contain one message"
        )
    incoming = new[0]
    incoming_id = _message_id(incoming, field="fixed.right")
    if not old:
        return incoming
    if len(old) != 1:
        raise ScopedContextError(
            "FIXED_MESSAGE_INVALID", "a fixed channel contains multiple messages"
        )
    existing = old[0]
    existing_id = _message_id(existing, field="fixed.left")
    if (
        existing_id != incoming_id
        or provider_visible_payload(existing) != provider_visible_payload(incoming)
    ):
        raise ScopedContextError(
            "FIXED_MESSAGE_CHANGED",
            f"fixed message {existing_id!r} cannot be replaced by {incoming_id!r}",
        )
    # Preserve the first object and its original telemetry.  A retry that only
    # changes local metadata is an idempotent no-op, not an in-place update.
    return existing


def strict_append_messages(
    left: Sequence[BaseMessage] | None,
    right: Sequence[BaseMessage] | BaseMessage | Mapping[str, Any] | None,
) -> list[BaseMessage]:
    """Append unique message IDs and reject any attempt to rewrite history."""

    existing = _coerce_messages(left, field="messages.left")
    incoming = _coerce_messages(right, field="messages.right")
    result = list(existing)
    by_id: dict[str, BaseMessage] = {}

    for index, message in enumerate(existing):
        message_id = _message_id(message, field=f"messages.left[{index}]")
        if message_id in by_id:
            raise ScopedContextError(
                "APPEND_ONLY_STATE_CORRUPT",
                f"existing transcript contains duplicate id {message_id!r}",
            )
        by_id[message_id] = message

    for index, message in enumerate(incoming):
        message_id = _message_id(message, field=f"messages.right[{index}]")
        prior = by_id.get(message_id)
        if prior is None:
            result.append(message)
            by_id[message_id] = message
            continue
        if provider_visible_payload(prior) != provider_visible_payload(message):
            raise ScopedContextError(
                "APPEND_ONLY_MESSAGE_CONFLICT",
                f"message id {message_id!r} was reused with a different provider payload",
            )
        # Same ID and same provider-visible payload is replay/idempotency.  Do
        # not replace the original object merely because telemetry changed.

    return result


def order_parallel_tool_messages(
    assistant_message: AIMessage | Mapping[str, Any],
    tool_messages: Sequence[ToolMessage | Mapping[str, Any]],
    *,
    require_complete: bool = True,
) -> list[ToolMessage]:
    """Order a fan-in batch by the assistant's original ``tool_calls`` list."""

    assistant_values = _coerce_messages(
        assistant_message, field="parallel_tools.assistant"
    )
    if len(assistant_values) != 1 or not isinstance(assistant_values[0], AIMessage):
        raise ScopedContextError(
            "PARALLEL_TOOL_ASSISTANT_INVALID", "one AIMessage is required"
        )
    calls = _tool_calls(assistant_values[0]) or []
    call_ids: list[str] = []
    for index, call in enumerate(calls):
        if not isinstance(call, Mapping):
            raise ScopedContextError(
                "PARALLEL_TOOL_CALL_INVALID", f"tool_calls[{index}] is not an object"
            )
        call_id = str(call.get("id") or "").strip()
        if not call_id:
            raise ScopedContextError(
                "PARALLEL_TOOL_CALL_INVALID", f"tool_calls[{index}] has no id"
            )
        if call_id in call_ids:
            raise ScopedContextError(
                "PARALLEL_TOOL_CALL_INVALID", f"duplicate tool call id {call_id!r}"
            )
        call_ids.append(call_id)
    if not call_ids:
        raise ScopedContextError(
            "PARALLEL_TOOL_CALL_INVALID", "assistant has no tool calls"
        )

    results = _coerce_messages(tool_messages, field="parallel_tools.results")
    by_call_id: dict[str, ToolMessage] = {}
    for index, result in enumerate(results):
        if not isinstance(result, ToolMessage):
            raise ScopedContextError(
                "PARALLEL_TOOL_RESULT_INVALID",
                f"parallel_tools.results[{index}] is not a ToolMessage",
            )
        _message_id(result, field=f"parallel_tools.results[{index}]")
        call_id = str(result.tool_call_id or "").strip()
        if call_id not in call_ids:
            raise ScopedContextError(
                "PARALLEL_TOOL_RESULT_UNKNOWN",
                f"tool result references unknown call id {call_id!r}",
            )
        if call_id in by_call_id:
            raise ScopedContextError(
                "PARALLEL_TOOL_RESULT_DUPLICATE",
                f"multiple tool results reference call id {call_id!r}",
            )
        by_call_id[call_id] = result

    missing = [call_id for call_id in call_ids if call_id not in by_call_id]
    if require_complete and missing:
        raise ScopedContextError(
            "PARALLEL_TOOL_RESULT_INCOMPLETE",
            "missing result(s) for " + ", ".join(missing),
        )
    return [by_call_id[call_id] for call_id in call_ids if call_id in by_call_id]


def append_parallel_tool_messages(
    transcript: Sequence[BaseMessage] | None,
    assistant_message: AIMessage | Mapping[str, Any],
    tool_messages: Sequence[ToolMessage | Mapping[str, Any]],
    *,
    require_complete: bool = True,
) -> list[BaseMessage]:
    """Sort one completed parallel tool batch and append it atomically."""

    ordered = order_parallel_tool_messages(
        assistant_message, tool_messages, require_complete=require_complete
    )
    return strict_append_messages(transcript, ordered)


def build_provider_messages(state: Mapping[str, Any]) -> list[BaseMessage]:
    """Build the cache-stable provider wire for one context epoch.

    Inherited source material is a frozen phase-entry region.  It is kept
    separate from the semantic Seed so exact Skill/resource bodies are not
    copied into TaskSpec or handoff payloads, and separate from ``messages``
    so later local events remain strictly append-only.
    """

    systems = _coerce_messages(state.get("system_message"), field="system_message")
    seeds = _coerce_messages(state.get("seed_message"), field="seed_message")
    if len(systems) != 1 or not isinstance(systems[0], SystemMessage):
        raise ScopedContextError(
            "PHASE_SYSTEM_INVALID", "phase context requires one SystemMessage"
        )
    if len(seeds) != 1 or not isinstance(seeds[0], HumanMessage):
        raise ScopedContextError(
            "PHASE_SEED_INVALID", "phase context requires one HumanMessage seed"
        )
    inherited = _coerce_messages(
        state.get("inherited_messages"), field="inherited_messages"
    )
    if any(not isinstance(message, HumanMessage) for message in inherited):
        raise ScopedContextError(
            "PHASE_INHERITED_MATERIAL_INVALID",
            "inherited material must use ordinary HumanMessage source containers",
        )
    local = _coerce_messages(state.get("messages"), field="messages")
    wire = [systems[0], seeds[0], *inherited, *local]
    seen: set[str] = set()
    for index, message in enumerate(wire):
        message_id = _message_id(message, field=f"provider_messages[{index}]")
        if message_id in seen:
            raise ScopedContextError(
                "PROVIDER_MESSAGE_ID_DUPLICATE",
                f"provider message sequence repeats id {message_id!r}",
            )
        seen.add(message_id)
    return wire


def assert_stable_provider_prefix(
    previous_state: Mapping[str, Any], current_state: Mapping[str, Any]
) -> bool:
    """Fail unless the old provider wire is a visible prefix of the new wire."""

    previous = [provider_visible_payload(item) for item in build_provider_messages(previous_state)]
    current = [provider_visible_payload(item) for item in build_provider_messages(current_state)]
    if len(current) < len(previous):
        raise ScopedContextError(
            "PROVIDER_PREFIX_TRUNCATED",
            f"provider wire shrank from {len(previous)} to {len(current)} messages",
        )
    for index, payload in enumerate(previous):
        if payload != current[index]:
            raise ScopedContextError(
                "PROVIDER_PREFIX_CHANGED", f"provider-visible message {index} changed"
            )
    return True


FixedProviderMessage = Annotated[BaseMessage, fixed_provider_message]
AppendOnlyProviderMessages = Annotated[list[BaseMessage], strict_append_messages]


class PhaseContextInput(TypedDict):
    """Only the canonical seed crosses from a parent graph into a phase."""

    phase_id: str
    system_message: FixedProviderMessage
    seed_message: FixedProviderMessage
    inherited_messages: list[BaseMessage]


class PhaseContextOutput(TypedDict, total=False):
    """Only a typed result/reference crosses back to the parent graph."""

    phase_outcome: dict[str, Any]
    transcript_ref: str


class PhaseContextState(TypedDict, total=False):
    """Overall subgraph state; ``messages`` is private to the phase graph."""

    phase_id: str
    system_message: FixedProviderMessage
    seed_message: FixedProviderMessage
    inherited_messages: list[BaseMessage]
    messages: AppendOnlyProviderMessages
    phase_outcome: dict[str, Any]
    transcript_ref: str


__all__ = [
    "AppendOnlyProviderMessages",
    "FixedProviderMessage",
    "PhaseContextInput",
    "PhaseContextOutput",
    "PhaseContextState",
    "ScopedContextError",
    "append_parallel_tool_messages",
    "assert_stable_provider_prefix",
    "build_provider_messages",
    "fixed_provider_message",
    "order_parallel_tool_messages",
    "provider_visible_fingerprint",
    "provider_visible_payload",
    "strict_append_messages",
]

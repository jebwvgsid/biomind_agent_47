"""Compatibility adapter for DeepSeek reasoning chunks over ChatOpenAI.

DeepSeek's chat-completions stream places thinking text in the non-standard
``delta.reasoning_content`` field.  The pinned ``langchain-openai`` adapter
drops that field while converting an SDK chunk to ``AIMessageChunk``.  Keep the
field in ``additional_kwargs`` so callbacks can observe stream activity and the
provider watchdog does not mistake active thinking for a silent connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


def _reasoning_content_from_chunk(chunk: Mapping[str, Any]) -> Any:
    choices = chunk.get("choices") or []
    if not choices:
        nested = chunk.get("chunk") or {}
        if isinstance(nested, Mapping):
            choices = nested.get("choices") or []
    if not choices or not isinstance(choices[0], Mapping):
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, Mapping) or "reasoning_content" not in delta:
        return None
    return delta.get("reasoning_content")


class DeepSeekReasoningChatOpenAI(ChatOpenAI):
    """Keep DeepSeek's native thinking on receive and tool-round replay.

    This opt-in adapter handles the DeepSeek chat-completions protocol only.
    Reasoning stays protocol data and is never copied into public content.
    """

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None,
                             **kwargs: Any) -> dict:
        # Convert once: preserving the original message correspondence is
        # essential when adding native fields to the SDK's final payload.
        prompt = self._convert_input(input_)
        messages = prompt.to_messages()
        explicit_limits = [kwargs[key] for key in ("max_tokens", "max_completion_tokens") if key in kwargs]
        if len(explicit_limits) == 2 and explicit_limits[0] != explicit_limits[1]:
            raise ValueError("DEEPSEEK_OUTPUT_PARAMETER_CONFLICT")
        payload = super()._get_request_payload(prompt, stop=stop, **kwargs)
        wire_messages = payload.get("messages")
        if not isinstance(wire_messages, list):
            return payload  # Responses API retains its own SDK contract.

        # Current ChatOpenAI renames max_tokens to max_completion_tokens.
        # DeepSeek's chat-completions API specifies max_tokens, so restore
        # that field at the actual SDK wire boundary, not just bind kwargs.
        if "max_completion_tokens" in payload:
            if "max_tokens" in payload and payload["max_tokens"] != payload["max_completion_tokens"]:
                raise ValueError("DEEPSEEK_OUTPUT_PARAMETER_CONFLICT")
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        if not payload.get("tools"):
            return payload
        if len(wire_messages) != len(messages):
            raise ValueError("DEEPSEEK_NATIVE_MESSAGE_ALIGNMENT_INVALID")
        for original, wire in zip(messages, wire_messages):
            if not isinstance(original, AIMessage):
                continue
            if "reasoning_content" not in original.additional_kwargs:
                continue
            value = original.additional_kwargs["reasoning_content"]
            if value is not None and not isinstance(value, str):
                raise ValueError("DEEPSEEK_REASONING_CONTENT_INVALID")
            if wire.get("role") != "assistant":
                raise ValueError("DEEPSEEK_NATIVE_MESSAGE_ALIGNMENT_INVALID")
            wire["reasoning_content"] = value
        return payload

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None

        reasoning_content = _reasoning_content_from_chunk(chunk)
        if reasoning_content is None:
            return generation_chunk

        message = generation_chunk.message
        additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
        additional_kwargs["reasoning_content"] = reasoning_content
        message.additional_kwargs = additional_kwargs
        return generation_chunk


def enable_deepseek_reasoning_stream(model: Any) -> Any:
    """Enable the adapter in place while preserving callbacks and SDK clients.

    ``init_chat_model`` returns the concrete ``ChatOpenAI`` instance in the
    supported runtime.  Switching to a field-compatible subclass preserves the
    already-created clients and also leaves test doubles or future providers
    untouched.
    """

    if isinstance(model, DeepSeekReasoningChatOpenAI):
        return model
    if type(model) is ChatOpenAI:
        model.__class__ = DeepSeekReasoningChatOpenAI
    return model


def deepseek_reasoning_stream_enabled(model: Any) -> bool:
    """Return whether the model uses the DeepSeek reasoning compatibility path."""

    return isinstance(model, DeepSeekReasoningChatOpenAI)

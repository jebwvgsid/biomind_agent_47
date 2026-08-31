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
    """ChatOpenAI variant that retains DeepSeek's streamed thinking field."""

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

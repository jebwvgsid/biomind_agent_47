"""Complete-message control-metadata safety for provider dispatch."""

from __future__ import annotations

import re
from typing import Any, Sequence

from agent_core.node_context import ControlMetadataLeakError


_V2_MESSAGE_CONTROL_RULES = (
    (
        "CONTROL_OBJECT_DIGEST",
        re.compile(
            r"\b(?:object|audit|evidence):sha256:[0-9a-f]{16,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "RAW_SHA256_DIGEST",
        re.compile(r"\bsha256:[0-9a-f]{32,}\b", re.IGNORECASE),
    ),
    (
        "TASK_PLAN_SEMANTIC_HASH_KEY",
        re.compile(
            r'["\'](?:task|plan)_(?:semantic|record)_hash["\']\s*:',
            re.IGNORECASE,
        ),
    ),
    (
        "STATE_GENERATION_KEY",
        re.compile(r'["\']state_generation["\']\s*:', re.IGNORECASE),
    ),
    (
        "CONTROLLER_IDENTITY_KEY",
        re.compile(
            r'["\'](?:approval_ref|cas_token|node_context_hash|projection_semantic_hash)["\']\s*:',
            re.IGNORECASE,
        ),
    ),
)


def assert_v2_llm_messages_clean(
    messages: Sequence[Any],
    *,
    stage: str = "llm.provider_dispatch",
) -> None:
    """Reject complete provider messages with typed, value-free evidence."""
    offending_paths = []
    matched_rule_ids = []
    for index, message in enumerate(messages):
        content = str(getattr(message, "content", message) or "")
        for rule_id, pattern in _V2_MESSAGE_CONTROL_RULES:
            if pattern.search(content):
                offending_paths.append(f"$messages[{index}].content")
                matched_rule_ids.append(rule_id)
    if matched_rule_ids:
        raise ControlMetadataLeakError(
            stage=stage,
            offending_paths=offending_paths,
            matched_rule_ids=matched_rule_ids,
        )

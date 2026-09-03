"""Shared, guarded assembly for every Recovery provider message."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from langchain_core.messages import BaseMessage, HumanMessage

from agent_core.provider_message_safety import assert_v2_llm_messages_clean
from agent_core.recovery_evidence import (
    execute_recovery_evidence_request,
    parse_evidence_request,
)
from prompts.manager import PromptManager


MAX_RECOVERY_EVIDENCE_FOLLOWUPS = 1

# The existing typed Skill continuation handlers remain compatibility-only for
# now.  Recovery's public prompt intentionally advertises only read/glob until
# R1 and R2 can share one formally specified Skill protocol end to end.


@dataclass(frozen=True)
class RecoveryEvidenceFollowup:
    request: Dict[str, Any]
    result: Dict[str, Any]
    messages: tuple[BaseMessage, ...]


def guarded_recovery_provider_messages(
    llm_payload: Mapping[str, Any],
    *,
    stage: str,
    evidence_result: Optional[Mapping[str, Any]] = None,
) -> list[BaseMessage]:
    """Build and validate one complete Recovery request before dispatch."""
    prompt = PromptManager.get_recovery_v2_prompt(llm_payload)
    if evidence_result is not None:
        prompt = (
            prompt
            + "\n\n<evidence_result>\n"
            + json.dumps(dict(evidence_result), ensure_ascii=False, indent=2)
            + "\n</evidence_result>\n"
            + "Return the final RecoveryDecision JSON now. "
            + "Do not request more evidence."
        )
    messages: list[BaseMessage] = [HumanMessage(content=prompt)]
    assert_v2_llm_messages_clean(messages, stage=stage)
    return messages


def build_recovery_evidence_followup(
    llm_payload: Mapping[str, Any],
    response_text: Any,
    *,
    data_path: str,
    task_id: str,
    stage: str,
) -> Optional[RecoveryEvidenceFollowup]:
    """Parse, execute and guard one bounded Recovery evidence continuation."""
    request = parse_evidence_request(response_text)
    if not request:
        return None
    result = execute_recovery_evidence_request(
        data_path=data_path,
        task_id=task_id,
        request=request,
    )
    messages = guarded_recovery_provider_messages(
        llm_payload,
        stage=stage,
        evidence_result=result,
    )
    return RecoveryEvidenceFollowup(
        request=dict(request),
        result=dict(result),
        messages=tuple(messages),
    )

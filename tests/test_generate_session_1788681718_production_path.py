"""Offline production-path regression for session_1788681718 Generate admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent_core.plan_tracker import (
    advance_plan_after_verified_step,
    approve_plan_state,
    bind_plan_to_task_revision,
    build_plan_state,
    canonical_task_spec_for_state,
    commit_recovery_bundle,
    load_plan_state,
    plan_approval_is_current,
    record_provenance_event,
    rollover_recovery_generation_claim_after_candidate_rejection,
    save_plan_state,
    semantic_plan_hash,
    update_recovery_generation_claim,
    validate_plan_for_generate,
)
from agent_core.recovery import (
    RecoveryAction,
    RecoveryCommitBundle,
    RecoveryDecision,
    RecoverySourceScope,
    RecoveryStateStore,
    build_recovery_execution_envelope,
    finalize_recovery_decision,
)
from agent_core.skill_access import (
    SkillAccessService,
    new_skill_access_state,
    record_provider_visible_materials,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import create_task_spec, merge_task_spec
from agent_core.workflow.context_compaction import ContextBudgetPolicy
from agent_core.workflow.outcomes import ExecuteOutcome
from agent_core.workflow.recovery_projection import (
    CausalFailurePacketBinder,
    bind_causal_failure_node,
)
from agent_core.workflow.scoped_context import provider_visible_payload


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "session_1788681718_generate_candidate.json"
)
PLAN_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "session_1788681718_approved_plan.json"
)
SKILL_ID = "genomics/snpeff-annotation"
CAPABILITY_ID = "genomics.snpeff_annotation.check_contig_compatibility"


class _FrozenResponseProvider:
    """LangChain-shaped fake that returns only explicitly queued responses."""

    def __init__(self, responses: Sequence[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def bind(self, **_kwargs: Any) -> "_FrozenResponseProvider":
        return self

    def model_copy(self, update: Any = None) -> "_FrozenResponseProvider":
        del update
        return self

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        **_kwargs: Any,
    ) -> AIMessage:
        self.calls.append([provider_visible_payload(item) for item in messages])
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise AssertionError("unexpected provider call")
        return AIMessage(content=self.responses[index])




if __name__ == "__main__":
    unittest.main()

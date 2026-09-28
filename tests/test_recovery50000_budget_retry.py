"""Real Recovery graph, final application adapter, archive and durable reentry.
Only external provider responses are scripted. No live config/API is read.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from agent_core.context_lineage import ContextLineageStore
from agent_core.workflow.context_compaction import ContextBudgetPolicy
from agent_core.workflow.recovery_runtime import configured_recovery_budget_policy, resolve_recovery_budget_policy
from agent_core.workflow.scoped_context import provider_visible_payload


def cap(**kwargs):
    return ContextBudgetPolicy(output_reserve_tokens=50000, requested_output_limit_tokens=50000,
        output_capability_source="not_verified", **kwargs)


def truncated(reason="length", *, complete_prefix=False):
    calls = [{"id": "never-dispatch", "type": "function", "function": {
        "name": "failure_inspect", "arguments": '{"ref_id":"never"}' if complete_prefix else '{"ref_id":'}}]
    return AIMessage(content="<analysis>incomplete private draft", additional_kwargs={"tool_calls": calls},
        response_metadata={"finish_reason": reason})












def test_explicit_provider_limit_is_visible_and_inherited_local_cap_is_not_capability():
    class Legacy: max_tokens=8192
    actual = resolve_recovery_budget_policy(cap(known_provider_output_limit_tokens=32768), clients=[Legacy()])
    assert actual.output_reserve_tokens == 32768
    assert actual.requested_output_limit_tokens == 50000
    assert actual.known_provider_output_limit_tokens == 32768
    assert resolve_recovery_budget_policy(cap(), clients=[Legacy()]).output_reserve_tokens == 50000
    assert configured_recovery_budget_policy().requested_output_limit_tokens == 50000
    assert 118554 < actual.safe_input_tokens
    assert cap().safe_input_tokens < 170000



















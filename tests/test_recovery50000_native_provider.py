"""Installed SDK payloads, without any network call or real configuration.

These tests reach ChatOpenAI's actual request serializer. A captured ainvoke
argument alone cannot prove that native fields survive the SDK boundary.
"""
import asyncio
from copy import deepcopy
import json
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from agent_core.deepseek_stream import DeepSeekReasoningChatOpenAI, enable_deepseek_reasoning_stream
from agent_core.workflow.recovery_agent import recovery_boundary_messages_from_events
from agent_core.workflow.scoped_context import provider_visible_payload


TOOLS = [{"type": "function", "function": {"name": "evidence_read",
    "description": "Read a declared evidence ref", "parameters": {"type": "object", "properties": {}}}}]


def _messages():
    return [SystemMessage(content="stable"), HumanMessage(content="inspect"),
        AIMessage(content="public-only", additional_kwargs={"reasoning_content": "native-first",
            "signature": "not-a-deepseek-chat-field", "arbitrary_extra": "not-forwarded"}),
        HumanMessage(content="follow up"),
        AIMessage(content="", additional_kwargs={"reasoning_content": "native-tool-turn"},
            tool_calls=[{"id": "read-1", "name": "evidence_read", "args": {}}]),
        ToolMessage(content="actual observation", tool_call_id="read-1")]


@pytest.fixture
def sdk():
    client = ChatOpenAI(model="deepseek-v4-flash", api_key="offline-literal-dummy",
        base_url="https://offline.invalid/v1", streaming=True, max_retries=0,
        max_tokens=8192, use_responses_api=False)
    yield client
    client.root_client.close()
    asyncio.run(client.root_async_client.close())


def test_installed_sdk_payload_carries_all_native_turns_and_wire_50000(sdk, tmp_path):
    messages = _messages()
    before = [m.model_dump() for m in messages]
    model = enable_deepseek_reasoning_stream(sdk)
    payload = model._get_request_payload(messages, tools=deepcopy(TOOLS), max_tokens=50000)
    assert payload["max_tokens"] == 50000 and "max_completion_tokens" not in payload
    assert payload["messages"][2]["reasoning_content"] == "native-first"
    assert payload["messages"][4]["reasoning_content"] == "native-tool-turn"
    assert payload["messages"][4]["tool_calls"][0]["id"] == "read-1"
    assert payload["messages"][5]["tool_call_id"] == "read-1"
    assert payload["messages"][2]["content"] == "public-only"
    assert "signature" not in payload["messages"][2] and "arbitrary_extra" not in payload["messages"][2]
    assert all("reasoning_content" not in row for index, row in enumerate(payload["messages"]) if index not in {2, 4})
    assert before == [m.model_dump() for m in messages]
    (tmp_path / "native_sdk_payload.json").write_text(json.dumps(payload, indent=2))


def test_other_provider_and_no_tools_keep_sdk_native_contract(sdk):
    original = sdk._get_request_payload(_messages(), tools=deepcopy(TOOLS), max_tokens=50000)
    assert "reasoning_content" not in original["messages"][2]
    assert original["max_completion_tokens"] == 50000 and "max_tokens" not in original
    adapted = enable_deepseek_reasoning_stream(sdk)
    no_tools = adapted._get_request_payload(_messages(), max_tokens=50000)
    assert "reasoning_content" not in no_tools["messages"][2]
    assert no_tools["max_tokens"] == 50000


def test_responses_api_keeps_its_own_sdk_output_parameter(sdk):
    sdk.use_responses_api = True
    original = sdk._get_request_payload([HumanMessage(content="isolated input")], max_tokens=50000)
    adapted = enable_deepseek_reasoning_stream(sdk)
    payload = adapted._get_request_payload([HumanMessage(content="isolated input")], max_tokens=50000)
    assert "messages" not in payload and "input" in payload
    # This patch does not redefine another API's SDK precedence rules.
    assert payload == original and "max_output_tokens" in payload and "max_tokens" not in payload


def test_large_native_field_is_in_both_wire_and_complete_input_budget(sdk):
    from agent_core.workflow.context_compaction import ContextBudgetPolicy, ReferenceTokenEstimator, measure_provider_input
    native = "native reasoning synthetic; " * 8000
    message = AIMessage(content="short public text", additional_kwargs={"reasoning_content": native})
    wire = enable_deepseek_reasoning_stream(sdk)._get_request_payload([message], tools=TOOLS, max_tokens=50000)
    assert wire["messages"][0]["reasoning_content"] == native
    estimator = ReferenceTokenEstimator()
    policy = ContextBudgetPolicy(output_reserve_tokens=50000)
    components = {"system": {}, "frozen_inputs": {}, "inherited_materials": [],
        "local_messages": [provider_visible_payload(message)], "tool_schema": TOOLS,
        "pending_results": [], "provider_wrappers": {}}
    full = measure_provider_input(components, policy=policy, estimator=estimator)
    components["local_messages"] = [provider_visible_payload(AIMessage(content="short public text"))]
    small = measure_provider_input(components, policy=policy, estimator=estimator)
    assert full.estimated_input_tokens > small.estimated_input_tokens + 10000


def test_wire_rejects_conflicting_explicit_output_fields(sdk):
    adapted = enable_deepseek_reasoning_stream(sdk)
    with pytest.raises(ValueError, match="DEEPSEEK_OUTPUT_PARAMETER_CONFLICT"):
        adapted._get_request_payload(_messages(), tools=TOOLS, max_tokens=50000, max_completion_tokens=8192)
    compatible = adapted._get_request_payload(_messages(), tools=TOOLS, max_tokens=50000, max_completion_tokens=50000)
    assert compatible["max_tokens"] == 50000 and "max_completion_tokens" not in compatible


@pytest.mark.parametrize("native", ["", None])
def test_empty_native_value_retained_without_inventing_missing_reasoning(sdk, native):
    adapted = enable_deepseek_reasoning_stream(sdk)
    messages = [AIMessage(content="a", additional_kwargs={"reasoning_content": native}), AIMessage(content="b")]
    wire = adapted._get_request_payload(messages, tools=TOOLS)["messages"]
    assert "reasoning_content" in wire[0] and wire[0]["reasoning_content"] is native
    assert "reasoning_content" not in wire[1]


def test_invalid_native_object_is_rejected_without_serializing_unrelated_fields(sdk):
    adapted = enable_deepseek_reasoning_stream(sdk)
    with pytest.raises(ValueError, match="DEEPSEEK_REASONING_CONTENT_INVALID"):
        adapted._get_request_payload([AIMessage(content="", additional_kwargs={"reasoning_content": {"invalid": 1}})], tools=TOOLS)


def test_durable_replay_native_payload_reaches_actual_sdk(sdk):
    adapted = enable_deepseek_reasoning_stream(sdk)
    original = _messages()[4]
    payload = provider_visible_payload(original)
    restored = recovery_boundary_messages_from_events([{
        "event": "recovery_assistant_message_committed", "message_id": "stable-native",
        "message": json.loads(json.dumps(payload)),
    }])[0]
    wire = adapted._get_request_payload([HumanMessage(content="read"), restored,
        ToolMessage(content="observed", tool_call_id="read-1")], tools=TOOLS, max_tokens=50000)
    assert wire["messages"][1]["reasoning_content"] == "native-tool-turn"
    assert wire["messages"][1]["tool_calls"][0]["id"] == wire["messages"][2]["tool_call_id"]


def test_actual_bio_constructor_installs_adapter_on_recovery_factory(tmp_path):
    import bio_agents
    class ConstructorBoundary(Exception):
        pass
    # Run the real constructor through every production client factory and
    # binding, stopping before unrelated result/graph facilities are created.
    agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    with patch.object(bio_agents, "LLM_API_KEY", "offline-literal-dummy"), \
         patch.object(bio_agents, "LLM_BASE_URL", "https://offline.invalid/v1"), \
         patch.object(bio_agents, "PackageManager", return_value=None), \
         patch.object(bio_agents, "ResultValidator", side_effect=ConstructorBoundary):
        with pytest.raises(ConstructorBoundary):
            agent.__init__(data_path=str(tmp_path), llm_model="deepseek-v4-flash", model_provider="openai", enable_persistence=False)
    try:
        assert agent.llm_provider_is_deepseek is True
        assert type(agent.recovery_transport_llm) is DeepSeekReasoningChatOpenAI
        bound = agent.recovery_llm
        while hasattr(bound, "bound"):
            bound = bound.bound
        assert type(bound) is DeepSeekReasoningChatOpenAI
        wire = bound._get_request_payload(_messages(), tools=TOOLS, max_tokens=50000)
        assert wire["max_tokens"] == 50000 and "max_completion_tokens" not in wire
        assert wire["messages"][4]["reasoning_content"] == "native-tool-turn"
        (tmp_path / "production_factory_payload.json").write_text(json.dumps(wire, indent=2))
    finally:
        seen = set()
        for name in ("llm", "planner_llm", "generate_llm", "recovery_transport_llm"):
            client = getattr(agent, name, None)
            while hasattr(client, "bound"):
                client = client.bound
            if client is not None and id(client) not in seen:
                seen.add(id(client))
                client.root_client.close()
                asyncio.run(client.root_async_client.close())

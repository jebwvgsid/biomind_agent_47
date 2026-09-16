"""Phase D current-boundary witnesses; ONLY run in the A1 guarded export.

These tests intentionally document the present absence of analysis-tag display
filtering and of a generation-time content cap. Passing is not repair acceptance.
Provider objects are local substitutes; production reception/display/watchdogs
are real. No subprocess, network or scientific work is used by these cases.
"""
import asyncio
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, message_chunk_to_message

from agent_core.cli_display import DisplayModel, bind_terminal, emit_public
from agent_core.provider_attempt import (
    CLEANUP_CONFIRMED, PROVIDER_CALLER_CANCELLED, PROVIDER_TOTAL_TIMEOUT,
    ProviderAttemptFailure, ProviderStreamObserver, ProviderTimeoutPolicy,
    output_policy_projection, run_provider_attempt,
)
from agent_core.workflow.recovery_agent import RecoveryAgentLoopConfig
from tests.test_recovery_reception import native, run_loop
from tests.test_provider_attempt_reliability import _ScenarioLLM, _envelope


def _recovery_envelope(policy):
    return replace(_envelope(policy), node_name="recovery")


def _inspection(content):
    # An intentionally unknown evidence ref exercises the real read-only tool
    # dispatcher error, not an executed command or a submitted directive.
    return AIMessage(content=content, tool_calls=[{
        "id": "inspect-d4", "name": "failure_inspect",
        "args": {"ref_id": "unknown-evidence-ref-d4"},
    }])


def test_long_prose_without_directive_still_charges_turns(tmp_path):
    content = "离线边界见证。" * 5000
    result, requests, events = run_loop(tmp_path, [AIMessage(content=content)] * 2)
    assert len(requests) == result["audit"]["turn_count"] == 2
    assert result["audit"]["format_correction_count"] == 1
    assert result["audit"]["tool_call_count"] == 0
    assert not result["directive"] and result["terminal_error"]
    public = [e["explanation"] for e in events if e["event"] == "recovery_response_received"]
    assert public == [content, content]  # current display boundary has no short-copy cap


@pytest.mark.parametrize("finish", ["length", "cancelled"])
def test_noncomplete_native_tool_is_never_dispatched(tmp_path, finish):
    response = _inspection("<analysis>partial response")
    response.response_metadata = {"finish_reason": finish}
    result, requests, events = run_loop(tmp_path, [response])
    assert len(requests) == 1 and result["audit"]["tool_call_count"] == 0
    assert not result["directive"]
    assert result["terminal_error"]["code"] == "RECOVERY_RESPONSE_INCOMPLETE"
    assert not any(e["event"] == "recovery_tool_observed" for e in events)


def test_full_receive_size_limit_precedes_tool_dispatch(tmp_path):
    response = _inspection("x" * 128001)
    result, requests, _ = run_loop(tmp_path, [response], RecoveryAgentLoopConfig(max_llm_turns=1))
    assert len(requests) == 1 and result["audit"]["tool_call_count"] == 0
    assert result["terminal_error"]["code"] == "RECOVERY_RESPONSE_SIZE_LIMIT"
    assert result["reception"]["explanations"][0]["text"] == response.content


@pytest.mark.parametrize("chunks", [
    ["<ana", "lysis>private-looking-text", "</anal", "ysis>公开说明"],
    ["<anal", "ysis>unclosed-private-looking-text"],
    ['代码字面示例：\n```python\ns = "<ana', 'lysis>literal</analysis>"\n```'],
])
def test_current_tag_boundary_preserves_raw_and_public_copy(tmp_path, chunks):
    message = AIMessageChunk(content="")
    for text in chunks:
        message = message + AIMessageChunk(content=text)
    raw = message_chunk_to_message(message).content
    response = _inspection(raw)
    result, requests, events = run_loop(tmp_path, [response, native()])
    observed = [e for e in events if e["event"] == "recovery_response_received"]
    assert observed[0]["explanation"] == raw
    assert result["reception"]["explanations"][0]["text"] == raw
    assert result["audit"]["tool_call_count"] == 1  # inspection error consumes budget
    model = DisplayModel()
    previous = bind_terminal(model)
    try:
        ticket = emit_public(observed[0]["explanation"], task_id="d4-offline")
        assert ticket.text == raw and model.history[-1] == raw
    finally:
        bind_terminal(previous)
    # Current string-tag leakage is a finding. Literal code is preserved. A
    # future display-only sanitizer must hide the former without changing raw.


def test_native_reasoning_block_is_not_public_content(tmp_path):
    response = _inspection([
        {"type": "reasoning", "text": "native-reasoning-offline-sentinel"},
        {"type": "text", "text": "已读取有限证据。"},
    ])
    result, _, events = run_loop(tmp_path, [response, native()])
    observed = [e for e in events if e["event"] == "recovery_response_received"]
    assert observed[0]["explanation"] == "已读取有限证据。"
    assert "native-reasoning-offline-sentinel" in str(result["reception"]["explanations"])


def test_continuous_chunks_still_hit_total_deadline():
    async def run():
        llm = _ScenarioLLM(["active_forever"])
        with pytest.raises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
                _recovery_envelope(ProviderTimeoutPolicy(0.02, 0.1, 0.035, 0.02)),
            )
        assert caught.value.error_code == PROVIDER_TOTAL_TIMEOUT
        assert caught.value.record.partial_payload_disposition == "discarded_before_parse"
        assert caught.value.record.cleanup_result == CLEANUP_CONFIRMED
        assert all(t.done() for t in llm.tasks)
    asyncio.run(run())


def test_missing_provider_usage_stays_unknown():
    async def run():
        _, record = await run_provider_attempt(
            lambda observer: _ScenarioLLM(["success"]).ainvoke([], {"callbacks": [observer]}),
            _recovery_envelope(ProviderTimeoutPolicy(0.02, 0.02, 0.08, 0.02)),
        )
        assert record.usage_status == "UNKNOWN" and record.provider_usage == {}
    asyncio.run(run())


def test_cancel_drains_provider_and_old_chunks_are_fenced():
    async def run():
        llm, events = _ScenarioLLM(["no_chunks"]), []
        task = asyncio.create_task(run_provider_attempt(
            lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
            _recovery_envelope(ProviderTimeoutPolicy(1, 1, 2, 0.05)),
            progress_callback=lambda kind, payload: events.append((kind, payload)),
        ))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        record = caught.value.provider_attempt_record
        assert record.failure_type == PROVIDER_CALLER_CANCELLED
        assert record.cleanup_result == CLEANUP_CONFIRMED and not record.retryable
        assert sum(kind == "attempt_cancelled" for kind, _ in events) == 1
        assert all(t.done() for t in llm.tasks)
        old = ProviderStreamObserver("old", asyncio.get_running_loop())
        old.fence()
        old.on_llm_new_token("late")
        assert old.snapshot()["content_chunk_count"] == 0
        assert old.snapshot()["late_chunk_count"] == 1
    asyncio.run(run())


def test_output_projection_does_not_claim_remote_enforcement():
    class LocalClient:
        max_tokens = 1500
    result = output_policy_projection(LocalClient(), requested={"max_tokens": 1500})
    assert result["remote_effective_limit"] == "UNKNOWN"
    assert result["transport_parameter_confirmation"] == "UNKNOWN"


def test_complete_late_response_after_cancel_is_consumed_not_accepted():
    async def run():
        llm, events = _ScenarioLLM(["ignore_cancel"]), []
        task = asyncio.create_task(run_provider_attempt(
            lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
            _recovery_envelope(ProviderTimeoutPolicy(1, 1, 2, 0.1)),
            progress_callback=lambda kind, payload: events.append((kind, payload)),
        ))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        llm.release.set()  # substitute client swallows cancel and completes late
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        record = caught.value.provider_attempt_record
        assert record.state == "CANCELLED" and record.cleanup_result == CLEANUP_CONFIRMED
        assert not any(kind == "attempt_completed" for kind, _ in events)
        assert sum(kind == "attempt_cancelled" for kind, _ in events) == 1
        assert all(t.done() for t in llm.tasks)
    asyncio.run(run())

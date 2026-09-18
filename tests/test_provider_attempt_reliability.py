from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx
import openai
from langchain_core.messages import AIMessage, HumanMessage

from agent_core.context_lineage import ContextLineageStore
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.provider_attempt import (
    CLEANUP_CONFIRMED,
    EMPTY_FINAL_CONTENT,
    GENERATE_PROVIDER_RETRY_EXHAUSTED,
    GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR,
    PROVIDER_CANONICAL_CONTEXT_CHANGED,
    PROVIDER_CALLER_CANCELLED,
    PROVIDER_CANCEL_CLEANUP_FAILED,
    PROVIDER_CLIENT_CANCELLED,
    PROVIDER_CONNECT_TIMEOUT,
    PROVIDER_FIRST_CHUNK_TIMEOUT,
    PROVIDER_HTTP_ERROR,
    PROVIDER_OUTCOME_UNKNOWN,
    PROVIDER_CONNECTION_FAILURE,
    PROVIDER_SDK_ERROR,
    PROVIDER_STREAM_READ_INTERRUPTED,
    PROVIDER_STREAM_IDLE_TIMEOUT,
    PROVIDER_TOTAL_TIMEOUT,
    ProviderAttemptEnvelope,
    ProviderAttemptFailure,
    ProviderStreamObserver,
    ProviderTimeoutPolicy,
    classify_provider_exception,
    exception_diagnostic,
    run_provider_attempt,
)
from bio_agents import BioAnalystAgent, generate_client_kwargs
from bio_agents import recovery_client_kwargs


class _ChunkMessage:
    def __init__(
        self,
        *,
        content: str = "",
        reasoning: str = "",
        tool_call_chunks=None,
    ):
        self.content = content
        self.additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}
        self.response_metadata = {}
        self.tool_call_chunks = list(tool_call_chunks or [])


class _Chunk:
    def __init__(
        self,
        *,
        content: str = "",
        reasoning: str = "",
        tool_call_chunks=None,
    ):
        self.message = _ChunkMessage(
            content=content,
            reasoning=reasoning,
            tool_call_chunks=tool_call_chunks,
        )
        self.generation_info = {}


class _ScenarioLLM:
    def __init__(self, scenarios):
        self.scenarios = list(scenarios)
        self.calls = 0
        self.cancelled = 0
        self.tasks = []
        self.callback_batches = []
        self.release = asyncio.Event()

    async def ainvoke(self, messages, config=None, **provider_kwargs):
        self.calls += 1
        scenario = self.scenarios[min(self.calls - 1, len(self.scenarios) - 1)]
        callbacks = list((config or {}).get("callbacks") or [])
        self.tasks.append(asyncio.current_task())
        self.callback_batches.append(callbacks)

        def content(value="x"):
            for callback in callbacks:
                callback.on_llm_new_token(value, chunk=_Chunk(content=value))

        def reasoning(value="thinking"):
            for callback in callbacks:
                callback.on_llm_new_token("", chunk=_Chunk(reasoning=value))

        def tool_argument(value='{"ref_id":'):
            for callback in callbacks:
                callback.on_llm_new_token(
                    "",
                    chunk=_Chunk(tool_call_chunks=[{
                        "id": "partial-tool",
                        "name": "failure_inspect",
                        "args": value,
                    }]),
                )

        def empty_heartbeat():
            for callback in callbacks:
                callback.on_llm_new_token("", chunk=_Chunk())

        if isinstance(scenario, BaseException):
            raise scenario

        if scenario == "success":
            return AIMessage(content="completed", response_metadata={'finish_reason': 'stop'})
        if scenario == "malformed":
            return AIMessage(content="not a structured Generate action", response_metadata={'finish_reason': 'stop'})
        if scenario == "empty":
            return AIMessage(content="", response_metadata={'finish_reason': 'stop'})
        if scenario == "reasoning_empty":
            reasoning()
            return AIMessage(content="", response_metadata={'finish_reason': 'stop'})
        if scenario == "partial_empty":
            content("<execute>discard-me")
            return AIMessage(content="", response_metadata={'finish_reason': 'stop'})
        if scenario == "reasoning_read_error":
            reasoning("discarded-reasoning")
            request = httpx.Request("POST", "https://provider.invalid/v1/chat")
            raise httpx.ReadError("stream read interrupted", request=request)
        if scenario == "partial_read_error":
            content("<execute>discard-me")
            request = httpx.Request("POST", "https://provider.invalid/v1/chat")
            raise httpx.ReadError("stream read interrupted", request=request)
        if scenario == "tool_partial_read_error":
            tool_argument()
            request = httpx.Request("POST", "https://provider.invalid/v1/chat")
            raise httpx.ReadError("stream read interrupted", request=request)
        if scenario == "cancel":
            raise asyncio.CancelledError()
        if scenario == "no_chunks":
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1
        if scenario == "ignore_cancel":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                await self.release.wait()
                return AIMessage(content="late completion", response_metadata={'finish_reason': 'stop'})
        if scenario == "partial_then_stall":
            content("<execute>echo partial")
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1
        if scenario in {
            "reasoning_active", "content_active", "tool_active",
            "empty_heartbeat", "active_forever",
        }:
            count = 6 if scenario != "active_forever" else 100
            try:
                for _ in range(count):
                    if scenario == "reasoning_active":
                        reasoning()
                    elif scenario == "tool_active":
                        tool_argument()
                    elif scenario == "empty_heartbeat":
                        empty_heartbeat()
                    else:
                        content()
                    await asyncio.sleep(0.004)
            finally:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    self.cancelled += 1
            return AIMessage(content="completed", response_metadata={'finish_reason': 'stop'})
        raise AssertionError(f"unknown scenario: {scenario}")


def _envelope(policy: ProviderTimeoutPolicy, ordinal: int = 1) -> ProviderAttemptEnvelope:
    return ProviderAttemptEnvelope(
        node_name="generate",
        step_id="step-build-db",
        semantic_invocation_id="semantic-fixed",
        attempt_id=f"attempt-{ordinal}",
        attempt_ordinal=ordinal,
        parent_attempt_id="" if ordinal == 1 else "attempt-1",
        call_id=f"call-{ordinal}",
        node_context_hash="node-context-fixed",
        request_hash="request-fixed",
        provider="fake",
        model="fake-model",
        thinking_policy={"thinking_enabled": True},
        timeout_policy=policy,
        retry_budget_before=1 if ordinal == 1 else 0,
        retry_budget_after=0,
        sdk_max_retries=0,
    )


def _agent(llm, *, data_path="", attempts=2):
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = data_path
    agent.llm = llm
    agent.llm_provider_is_deepseek = False
    agent.llm_provider_id = "fake"
    agent.llm_model_id = "fake-model"
    agent.llm_timeout_seconds = 0.12
    agent.llm_readonly_timeout_seconds = 0.12
    agent.llm_total_timeout_seconds = 0.12
    agent.llm_first_chunk_timeout_seconds = 0.02
    agent.llm_stream_idle_timeout_seconds = 0.02
    agent.llm_cancel_cleanup_timeout_seconds = 0.02
    agent.generate_provider_max_attempts = attempts
    agent.generate_provider_retry_base_seconds = 0.5
    agent.generate_provider_retry_cap_seconds = 8.0
    agent.generate_llm_sdk_max_retries = 0
    agent.llm_max_retries = 2
    agent.llm_context_limit_tokens = 200_000
    agent._provider_attempt_history = {}
    agent._latest_llm_call_refs = {}
    agent._provider_retry_random = lambda: 1.0

    async def no_wait(_delay):
        return None

    agent._provider_retry_sleep = no_wait
    return agent


def _http_status_error(status: int, *, retry_after: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError(
        f"provider returned HTTP {status}",
        request=request,
        response=response,
    )


class ProviderAttemptReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_a1_first_attempt_timeout_then_retry_success(self):
        llm = _ScenarioLLM(["no_chunks", "success"])
        agent = _agent(llm)
        response = await agent._ainvoke_llm(
            [HumanMessage(content="same request")],
            stage="generate",
            task_id="a1",
        )
        self.assertEqual(response.content, "completed")
        self.assertEqual(llm.calls, 2)
        self.assertEqual(len(agent._provider_attempt_history["a1"]), 2)

    async def test_a2_two_timeouts_are_terminal_without_third_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = _ScenarioLLM(["no_chunks", "no_chunks"])
            agent = _agent(llm, data_path=tmpdir)
            with self.assertRaises(ProviderAttemptFailure) as caught:
                await agent._ainvoke_llm(
                    [HumanMessage(content="same request")],
                    stage="generate",
                    task_id="a2",
                )
            self.assertEqual(
                caught.exception.error_code,
                GENERATE_PROVIDER_RETRY_EXHAUSTED,
            )
            self.assertEqual(llm.calls, 2)
            agent._mark_generate_provider_blocked("a2", caught.exception)
            blocked = json.loads(
                (
                    Path(tmpdir)
                    / "task_outputs"
                    / "a2"
                    / "blocked_status.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["error_owner"], "PROVIDER")
            self.assertEqual(blocked["failure_stage"], "GENERATE_PROVIDER")
            self.assertEqual(
                blocked["error_code"],
                GENERATE_PROVIDER_RETRY_EXHAUSTED,
            )

    async def test_a3_first_chunk_timeout_is_typed(self):
        llm = _ScenarioLLM(["no_chunks"])
        policy = ProviderTimeoutPolicy(0.01, 0.03, 0.08, 0.02)
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
                _envelope(policy),
            )
        self.assertEqual(caught.exception.error_code, PROVIDER_FIRST_CHUNK_TIMEOUT)
        self.assertEqual(caught.exception.record.cleanup_result, CLEANUP_CONFIRMED)

    async def test_a4_reasoning_chunks_refresh_idle_watchdog(self):
        llm = _ScenarioLLM(["reasoning_active"])
        response, record = await run_provider_attempt(
            lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
            _envelope(ProviderTimeoutPolicy(0.02, 0.012, 0.08, 0.02)),
        )
        self.assertEqual(response.content, "completed")
        self.assertGreaterEqual(record.reasoning_chunk_count, 6)
        self.assertEqual(record.content_chunk_count, 0)

    async def test_a5_hidden_content_chunks_refresh_idle_watchdog(self):
        llm = _ScenarioLLM(["content_active"])
        response, record = await run_provider_attempt(
            lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
            _envelope(ProviderTimeoutPolicy(0.02, 0.012, 0.08, 0.02)),
        )
        self.assertEqual(response.content, "completed")
        self.assertGreaterEqual(record.content_chunk_count, 6)
        self.assertTrue(record.last_any_chunk_at)

    async def test_a5b_tool_argument_only_chunks_refresh_idle_watchdog(self):
        llm = _ScenarioLLM(["tool_active"])
        response, record = await run_provider_attempt(
            lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
            _envelope(ProviderTimeoutPolicy(0.02, 0.012, 0.08, 0.02)),
        )
        self.assertEqual(response.content, "completed")
        self.assertGreaterEqual(record.tool_call_fragment_count, 6)
        self.assertEqual(record.content_chunk_count, 0)
        self.assertEqual(record.reasoning_chunk_count, 0)

    async def test_a5c_empty_transport_heartbeats_do_not_reset_watchdog(self):
        llm = _ScenarioLLM(["empty_heartbeat"])
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
                _envelope(ProviderTimeoutPolicy(0.012, 0.012, 0.08, 0.02)),
            )
        self.assertEqual(
            caught.exception.error_code,
            PROVIDER_FIRST_CHUNK_TIMEOUT,
        )
        self.assertEqual(caught.exception.record.total_chunk_count, 0)
        self.assertFalse(caught.exception.record.stream_started)

    async def test_a5d_missing_provider_usage_is_unknown_not_zero(self):
        response, record = await run_provider_attempt(
            lambda observer: _ScenarioLLM(["success"]).ainvoke(
                [], {"callbacks": [observer]}
            ),
            _envelope(ProviderTimeoutPolicy(0.02, 0.02, 0.08, 0.02)),
        )
        self.assertEqual(response.content, "completed")
        self.assertEqual(record.usage_status, "UNKNOWN")
        self.assertEqual(record.provider_usage, {})
        self.assertEqual(record.usage_reported_at, "")

    async def test_a6_midstream_stall_is_idle_timeout(self):
        llm = _ScenarioLLM(["partial_then_stall"])
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
                _envelope(ProviderTimeoutPolicy(0.02, 0.01, 0.08, 0.02)),
            )
        self.assertEqual(caught.exception.error_code, PROVIDER_STREAM_IDLE_TIMEOUT)
        self.assertEqual(caught.exception.record.content_chunk_count, 1)

    async def test_a7_active_stream_still_hits_total_deadline(self):
        llm = _ScenarioLLM(["active_forever"])
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
                _envelope(ProviderTimeoutPolicy(0.02, 0.1, 0.035, 0.02)),
            )
        self.assertEqual(caught.exception.error_code, PROVIDER_TOTAL_TIMEOUT)

    async def test_a8_timeout_cancellation_is_awaited_and_drained(self):
        llm = _ScenarioLLM(["no_chunks"])
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
                _envelope(ProviderTimeoutPolicy(0.01, 0.02, 0.05, 0.02)),
            )
        self.assertEqual(caught.exception.record.cleanup_result, CLEANUP_CONFIRMED)
        self.assertEqual(llm.cancelled, 1)
        self.assertTrue(all(task.done() for task in llm.tasks))

    async def test_a8b_external_cancellation_emits_terminal_record_and_drains(self):
        llm = _ScenarioLLM(["no_chunks"])
        events = []
        attempt = asyncio.create_task(run_provider_attempt(
            lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
            _envelope(ProviderTimeoutPolicy(1.0, 1.0, 2.0, 0.05)),
            progress_callback=lambda event, payload: events.append(
                (event, dict(payload))
            ),
        ))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        attempt.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await attempt

        record = getattr(caught.exception, "provider_attempt_record")
        self.assertEqual(record.failure_type, PROVIDER_CALLER_CANCELLED)
        self.assertEqual(record.state, "CANCELLED")
        self.assertEqual(record.cancellation_source, "caller")
        self.assertFalse(record.retryable)
        self.assertEqual(record.cleanup_result, CLEANUP_CONFIRMED)
        self.assertTrue(record.attempt_completed_at)
        self.assertEqual(events[-1][0], "attempt_cancelled")
        self.assertEqual(events[-1][1]["failure_type"], PROVIDER_CALLER_CANCELLED)
        self.assertTrue(all(task.done() for task in llm.tasks))

    async def test_a8c_provider_self_cancellation_is_typed_transport_failure(self):
        llm = _ScenarioLLM(["cancel"])
        events = []
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                lambda observer: llm.ainvoke([], {"callbacks": [observer]}),
                _envelope(ProviderTimeoutPolicy(1.0, 1.0, 2.0, 0.05)),
                progress_callback=lambda event, payload: events.append(
                    (event, dict(payload))
                ),
            )
        self.assertEqual(caught.exception.error_code, PROVIDER_CLIENT_CANCELLED)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(events[-1][0], "attempt_failed")

    async def test_a9_cleanup_unknown_never_retries(self):
        llm = _ScenarioLLM(["ignore_cancel", "success"])
        agent = _agent(llm)
        agent.llm_cancel_cleanup_timeout_seconds = 0.005
        try:
            with self.assertRaises(ProviderAttemptFailure) as caught:
                await agent._ainvoke_llm(
                    [HumanMessage(content="same request")],
                    stage="generate",
                    task_id="a9",
                )
            self.assertEqual(caught.exception.error_code, PROVIDER_OUTCOME_UNKNOWN)
            self.assertEqual(llm.calls, 1)
        finally:
            llm.release.set()
            await asyncio.sleep(0)
            await asyncio.gather(*llm.tasks, return_exceptions=True)

    async def test_a10_late_old_chunk_is_fenced_from_active_attempt(self):
        loop = asyncio.get_running_loop()
        old = ProviderStreamObserver("old", loop)
        active = ProviderStreamObserver("active", loop)
        old.on_llm_new_token("old", chunk=_Chunk(content="old"))
        old.fence()
        active.on_llm_new_token("new", chunk=_Chunk(content="new"))
        old.on_llm_new_token("late", chunk=_Chunk(content="late"))
        self.assertEqual(old.snapshot()["content_chunk_count"], 1)
        self.assertEqual(old.snapshot()["late_chunk_count"], 1)
        self.assertEqual(active.snapshot()["content_chunk_count"], 1)

    async def test_a11_partial_response_is_diagnostic_never_executable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = _ScenarioLLM(["partial_then_stall"])
            agent = _agent(llm, data_path=tmpdir, attempts=1)
            with self.assertRaises(ProviderAttemptFailure):
                await agent._ainvoke_llm(
                    [HumanMessage(content="produce action")],
                    stage="generate",
                    task_id="a11",
                    trace_metadata={"step_id": "step-build-db"},
                )
            snapshots = ContextLineageStore(tmpdir, "a11").list_snapshots()
            self.assertEqual(len(snapshots), 1)
            diagnostic_path = Path(snapshots[0]["snapshot_ref"]).parent / "partial_response_diagnostic.json"
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["classification"], "DIAGNOSTIC_ONLY")
            self.assertFalse(diagnostic["complete"])
            self.assertFalse(diagnostic["executable"])
            self.assertNotIn("pending_action", diagnostic)

    async def test_a12_provider_error_taxonomy_remains_distinct(self):
        class HTTPError(RuntimeError):
            status_code = 503

        self.assertEqual(
            classify_provider_exception(RuntimeError("ConnectTimeout"))[0],
            PROVIDER_CONNECTION_FAILURE,
        )
        self.assertEqual(classify_provider_exception(HTTPError("busy"))[0], PROVIDER_HTTP_ERROR)
        self.assertEqual(classify_provider_exception(ValueError("sdk exploded"))[0], PROVIDER_SDK_ERROR)
        self.assertEqual(len({
            PROVIDER_CONNECT_TIMEOUT,
            PROVIDER_CONNECTION_FAILURE,
            PROVIDER_STREAM_READ_INTERRUPTED,
            PROVIDER_FIRST_CHUNK_TIMEOUT,
            PROVIDER_STREAM_IDLE_TIMEOUT,
            PROVIDER_TOTAL_TIMEOUT,
            PROVIDER_HTTP_ERROR,
            PROVIDER_SDK_ERROR,
            PROVIDER_CANCEL_CLEANUP_FAILED,
            PROVIDER_OUTCOME_UNKNOWN,
        }), 10)

    async def test_a13_completed_malformed_response_is_not_transport_retry(self):
        llm = _ScenarioLLM(["malformed", "success"])
        agent = _agent(llm)
        response = await agent._ainvoke_llm(
            [HumanMessage(content="produce action")],
            stage="generate",
            task_id="a13",
        )
        self.assertEqual(response.content, "not a structured Generate action")
        self.assertEqual(llm.calls, 1)

    async def test_non_generate_sdk_exception_contract_is_unchanged(self):
        class RaisingLLM:
            async def ainvoke(self, messages, config=None, **provider_kwargs):
                raise ValueError("existing planner sdk error")

        agent = _agent(RaisingLLM())
        with self.assertRaisesRegex(ValueError, "existing planner sdk error"):
            await agent._ainvoke_llm(
                [HumanMessage(content="plan")],
                stage="planner",
                task_id="planner-compat",
            )

    async def test_a14_retry_keeps_semantic_and_context_hashes(self):
        llm = _ScenarioLLM(["no_chunks", "success"])
        agent = _agent(llm)
        await agent._ainvoke_llm(
            [HumanMessage(content="same immutable request")],
            stage="generate",
            task_id="a14",
            trace_metadata={"node_context_hash": "node-context-fixed"},
        )
        first, second = agent._provider_attempt_history["a14"]
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["semantic_invocation_id"], second["semantic_invocation_id"])
        self.assertEqual(first["node_context_hash"], second["node_context_hash"])
        self.assertEqual(first["request_hash"], second["request_hash"])

    async def test_a15_canonical_change_fences_stale_retry(self):
        llm = _ScenarioLLM(["no_chunks", "success"])
        agent = _agent(llm)
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await agent._ainvoke_llm(
                [HumanMessage(content="same request")],
                stage="generate",
                task_id="a15",
                retry_guard=lambda: False,
            )
        self.assertEqual(caught.exception.error_code, PROVIDER_CANONICAL_CONTEXT_CHANGED)
        self.assertEqual(llm.calls, 1)

    async def test_p1_reasoning_then_read_error_retries_once_and_commits_only_final(self):
        llm = _ScenarioLLM(["reasoning_read_error", "success"])
        agent = _agent(llm)
        response = await agent._ainvoke_llm(
            [HumanMessage(content="produce one action")],
            stage="generate",
            task_id="p1",
        )
        self.assertEqual(response.content, "completed")
        self.assertEqual(llm.calls, 2)
        first, second = agent._provider_attempt_history["p1"]
        self.assertEqual(first["failure_type"], PROVIDER_STREAM_READ_INTERRUPTED)
        self.assertEqual(first["reasoning_callback_fragment_count"], 1)
        self.assertEqual(first["partial_payload_disposition"], "discarded_before_parse")
        self.assertEqual(second["state"], "COMPLETED")

    async def test_p2_partial_content_is_not_concatenated_into_retry_success(self):
        llm = _ScenarioLLM(["partial_read_error", "success"])
        agent = _agent(llm)
        response = await agent._ainvoke_llm(
            [HumanMessage(content="produce one action")],
            stage="generate",
            task_id="p2",
        )
        self.assertEqual(response.content, "completed")
        self.assertNotIn("discard-me", str(response.content))
        first, second = agent._provider_attempt_history["p2"]
        self.assertEqual(first["content_callback_fragment_count"], 1)
        self.assertEqual(second["content_callback_fragment_count"], 0)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])

    async def test_p3_two_read_errors_exhaust_budget_without_action_or_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "p3"
            plan = load_plan_state(tmpdir, task_id)
            plan["steps"] = [
                {"step_id": "done", "status": "done", "description": "done"},
                {"step_id": "current", "status": "running", "description": "current"},
                {"step_id": "later", "status": "pending", "description": "later"},
            ]
            plan["current_step_id"] = "current"
            save_plan_state(tmpdir, task_id, plan)
            llm = _ScenarioLLM(["reasoning_read_error", "partial_read_error"])
            agent = _agent(llm, data_path=tmpdir)
            with self.assertRaises(ProviderAttemptFailure) as caught:
                await agent._ainvoke_llm(
                    [HumanMessage(content="produce one action")],
                    stage="generate",
                    task_id=task_id,
                )
            self.assertEqual(caught.exception.error_code, GENERATE_PROVIDER_RETRY_EXHAUSTED)
            self.assertEqual(llm.calls, 2)
            agent._mark_generate_provider_blocked(task_id, caught.exception)
            blocked_plan = load_plan_state(tmpdir, task_id)
            self.assertEqual(
                [step["status"] for step in blocked_plan["steps"]],
                ["done", "blocked", "skipped_upstream_blocked"],
            )
            self.assertEqual(blocked_plan["current_step_id"], "current")
            failure = blocked_plan["failure"]
            self.assertEqual(failure["attempt_count"], 2)
            self.assertEqual(failure["configured_max_attempts"], 2)
            self.assertTrue(failure["last_attempt_retryable"])
            self.assertTrue(failure["retry_budget_exhausted"])
            self.assertEqual(failure["pending_action"], {})
            self.assertFalse(failure["generated_action_committed"])
            self.assertFalse(failure["execution_started"])
            snapshots = ContextLineageStore(tmpdir, task_id).list_snapshots()
            self.assertEqual([row["status"] for row in snapshots], ["failed", "failed"])
            self.assertTrue(all(
                row["annotations"]["provider_attempt"]["partial_payload_disposition"]
                == "discarded_before_parse"
                for row in snapshots
            ))

    async def test_p4_wrapped_openai_connection_error_uses_cause_chain(self):
        request = httpx.Request("POST", "https://provider.invalid/v1/chat")
        inner = httpx.ReadError("read interrupted", request=request)
        outer = openai.APIConnectionError(request=request)
        outer.__cause__ = inner
        llm = _ScenarioLLM([outer, "success"])
        agent = _agent(llm)
        response = await agent._ainvoke_llm(
            [HumanMessage(content="same request")],
            stage="generate",
            task_id="p4",
        )
        self.assertEqual(response.content, "completed")
        self.assertEqual(llm.calls, 2)
        first = agent._provider_attempt_history["p4"][0]
        self.assertEqual(first["failure_type"], PROVIDER_CONNECTION_FAILURE)
        self.assertTrue(first["exception_cause_chain"])

    async def test_p5_context_only_read_error_cycle_is_bounded_and_retryable(self):
        request = httpx.Request("POST", "https://provider.invalid/v1/chat")
        outer = RuntimeError("outer wrapper")
        inner = httpx.ReadError("read interrupted", request=request)
        outer.__context__ = inner
        inner.__context__ = outer
        code, retryable = classify_provider_exception(outer)
        diagnostic = exception_diagnostic(outer)
        self.assertEqual(code, PROVIDER_CONNECTION_FAILURE)
        self.assertTrue(retryable)
        self.assertLessEqual(len(diagnostic["exception_chain"]), 2)
        self.assertTrue(diagnostic["exception_chain_cycle_safe"])
        self.assertTrue(diagnostic["exception_context_chain"])

    async def test_p6_http_400_is_one_send_terminal_request_error(self):
        llm = _ScenarioLLM([_http_status_error(400), "success"])
        agent = _agent(llm)
        with self.assertRaises(ProviderAttemptFailure) as caught:
            await agent._ainvoke_llm(
                [HumanMessage(content="same request")],
                stage="generate",
                task_id="p6",
            )
        self.assertEqual(
            caught.exception.error_code,
            GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR,
        )
        self.assertEqual(llm.calls, 1)
        self.assertFalse(caught.exception.record.retry_budget_exhausted)

    async def test_p7_http_401_and_403_are_one_send_terminal_errors(self):
        for status in (401, 403):
            with self.subTest(status=status):
                llm = _ScenarioLLM([_http_status_error(status), "success"])
                agent = _agent(llm)
                with self.assertRaises(ProviderAttemptFailure) as caught:
                    await agent._ainvoke_llm(
                        [HumanMessage(content="same request")],
                        stage="generate",
                        task_id=f"p7-{status}",
                    )
                self.assertEqual(
                    caught.exception.error_code,
                    GENERATE_PROVIDER_TERMINAL_REQUEST_ERROR,
                )
                self.assertEqual(llm.calls, 1)

    async def test_p8_http_429_honors_retry_after_with_mocked_sleep(self):
        delays = []

        async def record_sleep(delay):
            delays.append(delay)

        llm = _ScenarioLLM([_http_status_error(429, retry_after="2"), "success"])
        agent = _agent(llm)
        agent._provider_retry_sleep = record_sleep
        response = await agent._ainvoke_llm(
            [HumanMessage(content="same request")],
            stage="generate",
            task_id="p8",
        )
        self.assertEqual(response.content, "completed")
        self.assertEqual(llm.calls, 2)
        self.assertEqual(delays, [2.0])
        self.assertEqual(
            agent._provider_attempt_history["p8"][0]["retry_delay_seconds"],
            2.0,
        )

    async def test_p9_gateway_errors_are_bounded_and_generate_sdk_retry_is_zero(self):
        shared = {"max_retries": 3, "streaming": True}
        generated = generate_client_kwargs(shared)
        self.assertEqual(generated["max_retries"], 0)
        self.assertFalse(generated["cache"])
        self.assertEqual(shared["max_retries"], 3)
        for status in (502, 503, 504):
            with self.subTest(status=status):
                llm = _ScenarioLLM([
                    _http_status_error(status),
                    _http_status_error(status),
                    "success",
                ])
                agent = _agent(llm)
                with self.assertRaises(ProviderAttemptFailure) as caught:
                    await agent._ainvoke_llm(
                        [HumanMessage(content="same request")],
                        stage="generate",
                        task_id=f"p9-{status}",
                    )
                self.assertEqual(caught.exception.error_code, GENERATE_PROVIDER_RETRY_EXHAUSTED)
                self.assertEqual(llm.calls, 2)
                self.assertTrue(all(
                    row["sdk_max_retries"] == 0
                    for row in agent._provider_attempt_history[f"p9-{status}"]
                ))

    async def test_p10_provider_self_cancel_is_bounded_and_retryable(self):
        llm = _ScenarioLLM(["cancel", "success"])
        agent = _agent(llm)
        response = await agent._ainvoke_llm(
            [HumanMessage(content="same request")],
            stage="generate",
            task_id="p10",
        )
        self.assertEqual(response.content, "completed")
        self.assertEqual(llm.calls, 2)
        self.assertEqual(
            agent._provider_attempt_history["p10"][0]["failure_type"],
            PROVIDER_CLIENT_CANCELLED,
        )

    async def test_p10b_external_cancel_closes_context_manifest_as_cancelled(self):
        with tempfile.TemporaryDirectory(prefix="provider-cancel-trace-") as root:
            llm = _ScenarioLLM(["no_chunks"])
            agent = _agent(llm, data_path=root, attempts=1)
            request = asyncio.create_task(agent._ainvoke_llm(
                [HumanMessage(content="same request")],
                stage="planner",
                task_id="p10b",
            ))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request

            snapshots = ContextLineageStore(root, "p10b").list_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["status"], "cancelled")
            attempt = snapshots[0]["annotations"]["provider_attempt"]
            self.assertEqual(attempt["failure_type"], PROVIDER_CALLER_CANCELLED)
            self.assertEqual(attempt["state"], "CANCELLED")
            self.assertEqual(llm.calls, 1)

    async def test_recovery_adapter_uses_exact_wire_and_persists_attempt_events(self):
        class CaptureLLM:
            def __init__(self):
                self.requests = []

            async def ainvoke(self, messages, config=None, **provider_kwargs):
                self.requests.append(list(messages))
                return AIMessage(content="recovery response", response_metadata={'finish_reason': 'stop'})

        with tempfile.TemporaryDirectory(prefix="recovery-provider-adapter-") as root:
            llm = CaptureLLM()
            agent = _agent(llm, data_path=root, attempts=1)
            # This test checks dispatch/receipt semantics, not a 120 ms preparation deadline.
            agent.llm_timeout_seconds = agent.llm_total_timeout_seconds = 2.0
            agent.recovery_llm_policy = {}
            messages = [
                HumanMessage(content="frozen recovery seed"),
                AIMessage(content="append-only local event"),
            ]
            response = await agent._schema2_recovery_provider_invoke(
                messages,
                model=llm,
                call_context={
                    "task_id": "recovery-adapter",
                    "step_id": "step-1",
                    "seed_id": "seed-1",
                    "failure_id": "failure-1",
                    "context_epoch_id": "epoch-1",
                    "overflow_epoch": 0,
                    "turn_ordinal": 1,
                    "request_ordinal": 1,
                    "wire_fingerprint": "wire-1",
                },
            )

            self.assertEqual(response.content, "recovery response")
            self.assertEqual(llm.requests[0], messages)
            snapshots = ContextLineageStore(
                root, "recovery-adapter"
            ).list_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["call_kind"], "recovery_turn")
            self.assertEqual(snapshots[0]["status"], "completed")
            events_path = (
                Path(root)
                / "task_outputs"
                / "recovery-adapter"
                / "context_trace"
                / "events.jsonl"
            )
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            kinds = [item["event_kind"] for item in events]
            self.assertIn("recovery_provider_attempt_started", kinds)
            self.assertIn("recovery_provider_attempt_completed", kinds)

    async def test_recovery_truncated_tool_arguments_are_partial_not_executable(self):
        with tempfile.TemporaryDirectory(prefix="recovery-tool-partial-") as root:
            llm = _ScenarioLLM(["tool_partial_read_error"])
            agent = _agent(llm, data_path=root, attempts=1)
            # This test checks dispatch/receipt semantics, not a 120 ms preparation deadline.
            agent.llm_timeout_seconds = agent.llm_total_timeout_seconds = 2.0
            agent.recovery_llm_policy = {"sdk_max_retries": 0}
            with self.assertRaises(httpx.ReadError):
                await agent._schema2_recovery_provider_invoke(
                    [HumanMessage(content="frozen recovery seed")],
                    model=llm,
                    call_context={
                        "task_id": "recovery-tool-partial",
                        "step_id": "step-1",
                        "seed_id": "seed-tool-partial",
                        "failure_id": "failure-tool-partial",
                        "context_epoch_id": "epoch-tool-partial",
                        "overflow_epoch": 0,
                        "turn_ordinal": 1,
                        "request_ordinal": 1,
                        "wire_fingerprint": "wire-tool-partial",
                    },
                )

            lineage = ContextLineageStore(root, "recovery-tool-partial")
            manifest = lineage.list_snapshots()[0]
            attempt = (manifest.get("annotations") or {}).get(
                "provider_attempt"
            ) or {}
            events = [
                json.loads(line)
                for line in (lineage.root / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            terminal = next(
                row for row in events
                if row["event_kind"] == "recovery_provider_attempt_failed"
            )["payload"]

        self.assertEqual(llm.calls, 1)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(attempt["tool_call_fragment_count"], 1)
        self.assertEqual(
            attempt["partial_payload_disposition"], "discarded_before_parse"
        )
        self.assertFalse(attempt["stream_completed"])
        self.assertEqual(terminal["tool_call_fragment_count"], 1)
        self.assertEqual(
            terminal["partial_payload_disposition"], "discarded_before_parse"
        )

    async def test_recovery_manifest_and_attempt_event_precede_provider_entry(self):
        class InspectingLLM:
            def __init__(self, root):
                self.root = root
                self.calls = 0

            async def ainvoke(self, _messages, config=None, **provider_kwargs):
                self.calls += 1
                store = ContextLineageStore(self.root, "recovery-order")
                snapshots = store.list_snapshots()
                self.assertions = {
                    "manifest_started": (
                        len(snapshots) == 1
                        and snapshots[0]["status"] == "started"
                    ),
                    "sdk_max_retries": snapshots[0].get("sdk_max_retries"),
                }
                events_path = store.root / "events.jsonl"
                events = [
                    json.loads(line)
                    for line in events_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                started = [
                    row for row in events
                    if row["event_kind"] == "recovery_provider_attempt_started"
                ]
                self.assertions["attempt_event_exists"] = len(started) == 1
                self.assertions["attempt_sdk_max_retries"] = (
                    started[0]["payload"]["sdk_max_retries"]
                    if started else None
                )
                return AIMessage(content="recovery response", response_metadata={'finish_reason': 'stop'})

        with tempfile.TemporaryDirectory(prefix="recovery-order-") as root:
            llm = InspectingLLM(root)
            agent = _agent(llm, data_path=root, attempts=1)
            # This test checks dispatch/receipt semantics, not a 120 ms preparation deadline.
            agent.llm_timeout_seconds = agent.llm_total_timeout_seconds = 2.0
            agent.recovery_llm_policy = {"sdk_max_retries": 0}
            await agent._schema2_recovery_provider_invoke(
                [HumanMessage(content="frozen recovery seed")],
                model=llm,
                call_context={
                    "task_id": "recovery-order",
                    "step_id": "step-1",
                    "seed_id": "seed-order",
                    "failure_id": "failure-order",
                    "context_epoch_id": "epoch-order",
                    "overflow_epoch": 0,
                    "turn_ordinal": 1,
                    "request_ordinal": 1,
                    "wire_fingerprint": "wire-order",
                },
            )

        self.assertEqual(llm.calls, 1)
        self.assertTrue(llm.assertions["manifest_started"])
        self.assertTrue(llm.assertions["attempt_event_exists"])
        self.assertEqual(llm.assertions["sdk_max_retries"], 0)
        self.assertEqual(llm.assertions["attempt_sdk_max_retries"], 0)

    async def test_recovery_attempt_event_failure_prevents_provider_dispatch(self):
        class NeverCalledLLM:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, _messages, config=None, **provider_kwargs):
                self.calls += 1
                return AIMessage(content="must not run", response_metadata={'finish_reason': 'stop'})

        with tempfile.TemporaryDirectory(prefix="recovery-event-fail-") as root:
            llm = NeverCalledLLM()
            agent = _agent(llm, data_path=root, attempts=1)
            agent.recovery_llm_policy = {"sdk_max_retries": 0}

            def fail_event(_event, _payload):
                raise OSError("event store unavailable")

            agent._schema2_recovery_event_sink = fail_event
            with self.assertRaises(OSError):
                await agent._schema2_recovery_provider_invoke(
                    [HumanMessage(content="frozen recovery seed")],
                    model=llm,
                    call_context={
                        "task_id": "recovery-event-fail",
                        "step_id": "step-1",
                        "seed_id": "seed-event-fail",
                        "failure_id": "failure-event-fail",
                        "context_epoch_id": "epoch-event-fail",
                        "overflow_epoch": 0,
                        "turn_ordinal": 1,
                        "request_ordinal": 1,
                        "wire_fingerprint": "wire-event-fail",
                    },
                )
        self.assertEqual(llm.calls, 0)

    async def test_recovery_compactor_uses_zero_retry_transport_and_durable_start(self):
        class InspectingCompactorLLM:
            def __init__(self, root):
                self.root = root
                self.calls = 0
                self.assertions = {}

            async def ainvoke(self, _messages, config=None, **provider_kwargs):
                self.calls += 1
                attempt_id = str(
                    ((config or {}).get("metadata") or {}).get(
                        "provider_attempt_id"
                    )
                    or ""
                )
                store = ContextLineageStore(self.root, "recovery-compactor-order")
                manifest = next(
                    item for item in store.list_snapshots()
                    if str(item.get("attempt_id") or "") == attempt_id
                )
                events = [
                    json.loads(line)
                    for line in (store.root / "events.jsonl").read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if line.strip()
                ]
                started = next(
                    row for row in events
                    if row["event_kind"]
                    == "recovery_compactor_provider_attempt_started"
                    and str((row.get("payload") or {}).get("attempt_id") or "")
                    == attempt_id
                )
                self.assertions = {
                    "manifest_status": manifest.get("status"),
                    "manifest_sdk_max_retries": manifest.get("sdk_max_retries"),
                    "attempt_sdk_max_retries": started["payload"].get(
                        "sdk_max_retries"
                    ),
                }
                return AIMessage(content='{"summary":"bounded"}', response_metadata={'finish_reason': 'stop'})

        with tempfile.TemporaryDirectory(prefix="recovery-compactor-order-") as root:
            llm = InspectingCompactorLLM(root)
            agent = _agent(object(), data_path=root, attempts=1)
            agent.recovery_transport_llm = llm
            response = await agent._schema2_context_compactor_invoke(
                [HumanMessage(content="compact this recovery epoch")],
                task_id="recovery-compactor-order",
                phase="recovery",
                source_epoch_id="epoch-source",
                compaction_request_id="compaction-order",
            )
            lineage = ContextLineageStore(root, "recovery-compactor-order")
            completed = next(
                row for row in [
                    json.loads(line)
                    for line in (lineage.root / "events.jsonl").read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if line.strip()
                ]
                if row["event_kind"]
                == "recovery_compactor_provider_attempt_completed"
            )["payload"]

        self.assertEqual(response.content, '{"summary":"bounded"}')
        self.assertEqual(llm.calls, 1)
        self.assertEqual(llm.assertions, {
            "manifest_status": "started",
            "manifest_sdk_max_retries": 0,
            "attempt_sdk_max_retries": 0,
        })
        self.assertEqual(completed["state"], "COMPLETED")
        self.assertEqual(completed["usage_status"], "UNKNOWN")
        self.assertEqual(completed["provider_usage"], {})

    def test_recovery_client_kwargs_disable_sdk_retries_without_mutation(self):
        shared = {"max_retries": 2, "streaming": True, "cache": True}
        recovery = recovery_client_kwargs(shared)
        self.assertEqual(recovery["max_retries"], 0)
        self.assertFalse(recovery["cache"])
        self.assertEqual(shared["max_retries"], 2)
        self.assertTrue(shared["cache"])

    async def test_p11_reasoning_only_empty_response_uses_provider_budget(self):
        llm = _ScenarioLLM(["reasoning_empty", "success"])
        agent = _agent(llm)
        agent._record_action_format_failure = Mock()
        response = await agent._ainvoke_llm(
            [HumanMessage(content="same request")],
            stage="generate",
            task_id="p11",
        )
        self.assertEqual(response.content, "completed")
        self.assertEqual(llm.calls, 2)
        first = agent._provider_attempt_history["p11"][0]
        self.assertEqual(first["failure_type"], EMPTY_FINAL_CONTENT)
        self.assertEqual(first["reasoning_callback_fragment_count"], 1)
        agent._record_action_format_failure.assert_not_called()

    async def test_p12_mixed_read_and_empty_failures_never_dispatch_a_third_request(self):
        for scenarios in (
            ["reasoning_read_error", "empty", "success"],
            ["empty", "partial_read_error", "success"],
        ):
            with self.subTest(scenarios=scenarios[:2]):
                llm = _ScenarioLLM(scenarios)
                agent = _agent(llm)
                with self.assertRaises(ProviderAttemptFailure) as caught:
                    await agent._ainvoke_llm(
                        [HumanMessage(content="same request")],
                        stage="generate",
                        task_id=f"p12-{scenarios[0]}",
                    )
                self.assertEqual(caught.exception.error_code, GENERATE_PROVIDER_RETRY_EXHAUSTED)
                self.assertEqual(llm.calls, 2)

    async def test_p13_partial_empty_attempt_is_failed_not_successfully_cached(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = _ScenarioLLM(["partial_empty", "success"])
            agent = _agent(llm, data_path=tmpdir)
            response = await agent._ainvoke_llm(
                [HumanMessage(content="same request")],
                stage="generate",
                task_id="p13",
            )
            self.assertEqual(response.content, "completed")
            snapshots = ContextLineageStore(tmpdir, "p13").list_snapshots()
            self.assertEqual([row["status"] for row in snapshots], ["failed", "completed"])
            failed_payload = json.loads(
                Path(snapshots[0]["response_payload_ref"]).read_text(encoding="utf-8")
            )
            self.assertEqual(failed_payload["role"], "error")
            self.assertEqual(
                snapshots[0]["annotations"]["provider_attempt"]["failure_type"],
                EMPTY_FINAL_CONTENT,
            )

    async def test_p14_attempt_ids_buffers_and_lineage_are_independent(self):
        llm = _ScenarioLLM(["partial_read_error", "success"])
        agent = _agent(llm)
        await agent._ainvoke_llm(
            [HumanMessage(content="immutable prompt")],
            stage="generate",
            task_id="p14",
            trace_metadata={"node_context_hash": "fixed-context"},
        )
        first, second = agent._provider_attempt_history["p14"]
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(second["parent_attempt_id"], first["attempt_id"])
        self.assertEqual(first["semantic_invocation_id"], second["semantic_invocation_id"])
        self.assertEqual(first["request_hash"], second["request_hash"])
        self.assertEqual(first["prompt_hash"], second["prompt_hash"])
        self.assertEqual(first["model_parameter_hash"], second["model_parameter_hash"])
        self.assertEqual(first["content_callback_fragment_count"], 1)
        self.assertEqual(second["content_callback_fragment_count"], 0)
        self.assertEqual(first["partial_payload_disposition"], "discarded_before_parse")
        self.assertIsNot(llm.tasks[0], llm.tasks[1])
        self.assertIsNot(llm.callback_batches[0], llm.callback_batches[1])
        self.assertIsNot(
            llm.callback_batches[0][0],
            llm.callback_batches[1][0],
        )

    async def test_p15_action_binding_consumer_runs_once_after_retry_success(self):
        llm = _ScenarioLLM(["partial_read_error", "success"])
        agent = _agent(llm)
        agent._bind_pending_action = Mock(return_value={"action_id": "one"})
        execution_spy = Mock()
        response = await agent._ainvoke_llm(
            [HumanMessage(content="one action")],
            stage="generate",
            task_id="p15",
        )
        parsed_action = {"type": "execute", "script": str(response.content)}
        agent._bind_pending_action(parsed_action, {}, "p15")
        agent._bind_pending_action.assert_called_once()
        execution_spy.assert_not_called()
        self.assertEqual(llm.calls, 2)

    async def test_p16_prior_success_is_not_counted_for_current_terminal_401(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "p16"
            messages = [HumanMessage(content="same semantic request")]
            agent = _agent(_ScenarioLLM(["success"]), data_path=tmpdir)
            await agent._ainvoke_llm(messages, stage="generate", task_id=task_id)
            terminal_llm = _ScenarioLLM([_http_status_error(401)])
            agent.llm = terminal_llm
            with self.assertRaises(ProviderAttemptFailure) as caught:
                await agent._ainvoke_llm(messages, stage="generate", task_id=task_id)
            agent._mark_generate_provider_blocked(task_id, caught.exception)
            failure = load_plan_state(tmpdir, task_id)["failure"]
            self.assertEqual(failure["attempt_count"], 1)
            self.assertEqual(failure["configured_max_attempts"], 2)
            self.assertFalse(failure["last_attempt_retryable"])
            self.assertFalse(failure["retry_budget_exhausted"])
            self.assertEqual(
                failure["terminal_failure_reason"],
                "generate_provider_terminal_request_error",
            )
            self.assertEqual(terminal_llm.calls, 1)

    async def test_p17_exception_diagnostics_are_bounded_and_redacted(self):
        class DiagnosticError(RuntimeError):
            status_code = 429

            def __init__(self):
                super().__init__(
                    "Authorization: Bearer unit-test-credential "
                    "https://user:pass@provider.invalid/path?signature=unit-test-signature#fragment",
                    "Cookie: session=unit-test-cookie",
                    {
                        "Authorization": "Bearer unit-test-mapping-credential",
                        "HTTPS_PROXY": "https://proxy-user:proxy-pass@proxy.invalid",
                    },
                )
                self.response = type("Response", (), {
                    "status_code": 429,
                    "headers": {
                        "Retry-After": "1",
                        "X-Request-Id": "safe-request-id",
                        "Authorization": "Bearer unit-test-header-credential",
                        "Cookie": "session=unit-test-cookie",
                    },
                })()

        diagnostic = exception_diagnostic(DiagnosticError())
        encoded = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "unit-test-credential",
            "unit-test-signature",
            "unit-test-header-credential",
            "unit-test-mapping-credential",
            "unit-test-cookie",
            "user:pass@",
            "proxy-user:proxy-pass@",
            "#fragment",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(diagnostic["http_response_headers"]["Retry-After"], "1")
        self.assertEqual(
            diagnostic["provider_request_id_from_headers"],
            "safe-request-id",
        )
        self.assertNotIn("Authorization", diagnostic["http_response_headers"])
        self.assertTrue(diagnostic["exception_mro"])
        self.assertIn("exception_repr", diagnostic)

    async def test_p18_generate_sdk_retry_policy_violation_fails_before_dispatch(self):
        llm = _ScenarioLLM(["success"])
        agent = _agent(llm)
        with self.assertRaisesRegex(
            RuntimeError,
            "GENERATE_SDK_RETRY_POLICY_VIOLATION",
        ):
            await agent._ainvoke_llm(
                [HumanMessage(content="same request")],
                stage="generate",
                task_id="p18",
                llm_policy={"sdk_max_retries": 1},
            )
        self.assertEqual(llm.calls, 0)


if __name__ == "__main__":
    unittest.main()

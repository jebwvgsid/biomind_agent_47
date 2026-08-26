from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import AIMessage, HumanMessage

from agent_core.context_lineage import ContextLineageStore
from agent_core.provider_attempt import (
    CLEANUP_CONFIRMED,
    PROVIDER_CANONICAL_CONTEXT_CHANGED,
    PROVIDER_CANCEL_CLEANUP_FAILED,
    PROVIDER_CONNECT_TIMEOUT,
    PROVIDER_FIRST_CHUNK_TIMEOUT,
    PROVIDER_HTTP_ERROR,
    PROVIDER_OUTCOME_UNKNOWN,
    PROVIDER_SDK_ERROR,
    PROVIDER_STREAM_IDLE_TIMEOUT,
    PROVIDER_TOTAL_TIMEOUT,
    ProviderAttemptEnvelope,
    ProviderAttemptFailure,
    ProviderStreamObserver,
    ProviderTimeoutPolicy,
    classify_provider_exception,
    run_provider_attempt,
)
from bio_agents import BioAnalystAgent


class _ChunkMessage:
    def __init__(self, *, content: str = "", reasoning: str = ""):
        self.content = content
        self.additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}
        self.response_metadata = {}


class _Chunk:
    def __init__(self, *, content: str = "", reasoning: str = ""):
        self.message = _ChunkMessage(content=content, reasoning=reasoning)
        self.generation_info = {}


class _ScenarioLLM:
    def __init__(self, scenarios):
        self.scenarios = list(scenarios)
        self.calls = 0
        self.cancelled = 0
        self.tasks = []
        self.release = asyncio.Event()

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        scenario = self.scenarios[min(self.calls - 1, len(self.scenarios) - 1)]
        callbacks = list((config or {}).get("callbacks") or [])
        self.tasks.append(asyncio.current_task())

        def content(value="x"):
            for callback in callbacks:
                callback.on_llm_new_token(value, chunk=_Chunk(content=value))

        def reasoning(value="thinking"):
            for callback in callbacks:
                callback.on_llm_new_token("", chunk=_Chunk(reasoning=value))

        if scenario == "success":
            return AIMessage(content="completed")
        if scenario == "malformed":
            return AIMessage(content="not a structured Generate action")
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
                return AIMessage(content="late completion")
        if scenario == "partial_then_stall":
            content("<execute>echo partial")
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1
        if scenario in {"reasoning_active", "content_active", "active_forever"}:
            count = 6 if scenario != "active_forever" else 100
            try:
                for _ in range(count):
                    reasoning() if scenario == "reasoning_active" else content()
                    await asyncio.sleep(0.004)
            finally:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    self.cancelled += 1
            return AIMessage(content="completed")
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
        sdk_max_retries=2,
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
    agent.llm_max_retries = 2
    agent.llm_context_limit_tokens = 200_000
    agent._provider_attempt_history = {}
    agent._latest_llm_call_refs = {}
    return agent


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
            self.assertEqual(caught.exception.error_code, PROVIDER_FIRST_CHUNK_TIMEOUT)
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
            self.assertEqual(blocked["error_code"], PROVIDER_FIRST_CHUNK_TIMEOUT)

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
            PROVIDER_CONNECT_TIMEOUT,
        )
        self.assertEqual(classify_provider_exception(HTTPError("busy"))[0], PROVIDER_HTTP_ERROR)
        self.assertEqual(classify_provider_exception(ValueError("sdk exploded"))[0], PROVIDER_SDK_ERROR)
        self.assertEqual(len({
            PROVIDER_CONNECT_TIMEOUT,
            PROVIDER_FIRST_CHUNK_TIMEOUT,
            PROVIDER_STREAM_IDLE_TIMEOUT,
            PROVIDER_TOTAL_TIMEOUT,
            PROVIDER_HTTP_ERROR,
            PROVIDER_SDK_ERROR,
            PROVIDER_CANCEL_CLEANUP_FAILED,
            PROVIDER_OUTCOME_UNKNOWN,
        }), 8)

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
            async def ainvoke(self, messages, config=None):
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


if __name__ == "__main__":
    unittest.main()

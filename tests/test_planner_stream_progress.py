"""OBS-FIX: real provider observer/watchdog with isolated synthetic streams.

Graph/Planner renderer integration belongs to the companion production tests.
These tests call the actual provider-attempt driver; no network is used.
"""
import asyncio
from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.provider_attempt import (
    PROVIDER_CALLER_CANCELLED,
    PROVIDER_ATTEMPT_START_PERSISTENCE_FAILED,
    PROVIDER_FIRST_CHUNK_TIMEOUT,
    PROVIDER_STREAM_IDLE_TIMEOUT,
    PROVIDER_TOTAL_TIMEOUT,
    ProviderAttemptEnvelope,
    ProviderAttemptFailure,
    ProviderTimeoutPolicy,
    run_provider_attempt,
)


def _envelope(*, first=1.0, idle=1.0, total=3.0):
    return ProviderAttemptEnvelope(
        node_name="planner", step_id="", semantic_invocation_id="planner-progress-test",
        attempt_id="attempt-observe", attempt_ordinal=1, parent_attempt_id="",
        call_id="call-observe", node_context_hash="context-fixed", request_hash="request-fixed",
        provider="offline", model="recorded", thinking_policy={},
        timeout_policy=ProviderTimeoutPolicy(first, idle, total, 0.2),
        retry_budget_before=0, retry_budget_after=0, sdk_max_retries=0,
    )


def _payload(observer, *, content="", reasoning=""):
    observer.on_llm_new_token(content, chunk=SimpleNamespace(
        message=SimpleNamespace(
            content=content,
            additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
            response_metadata={}, tool_call_chunks=[],
        ),
        generation_info={},
    ))


def _response(content="complete candidate"):
    return SimpleNamespace(content=content, response_metadata={"finish_reason": "stop"})


class PlannerStreamProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiphase_stream_reports_real_counts_and_open_payload_gap(self):
        events = []
        snapshots = []

        async def invoke(observer):
            await asyncio.sleep(0.04)
            _payload(observer, content="abc")
            snapshots.append(observer.snapshot())
            await asyncio.sleep(0.09)
            _payload(observer, reasoning="private-reasoning-body")
            _payload(observer, content="defgh")
            snapshots.append(observer.snapshot())
            await asyncio.sleep(0.065)
            return _response("abcdefgh")

        response, record = await run_provider_attempt(
            invoke, _envelope(), observation_interval_seconds=0.02,
            progress_callback=lambda event, value: events.append((event, deepcopy(value))),
        )
        progress = [row for event, row in events if event == "attempt_progress"]
        self.assertGreaterEqual(len(progress), 5)
        self.assertEqual(events[0][0], "attempt_started")
        self.assertEqual(events[-1][0], "attempt_completed")
        waiting = [row for row in progress if row["payload_idle_seconds"] is None]
        self.assertTrue(waiting)
        self.assertTrue(all(row["content_char_count"] == 0 for row in waiting))
        gap = [row for row in progress if row["content_char_count"] == 3]
        self.assertGreaterEqual(len(gap), 2)
        self.assertLess(gap[0]["payload_idle_seconds"], gap[-1]["payload_idle_seconds"])
        self.assertEqual(record.content_char_count, 8)
        self.assertEqual(record.reasoning_char_count, len("private-reasoning-body"))
        self.assertEqual(record.observed_payload_callback_count, 3)
        self.assertEqual(record.max_payload_gap_seconds, snapshots[-1]["max_payload_gap_seconds"])
        self.assertGreaterEqual(record.max_payload_gap_seconds, 0.075)
        self.assertNotIn("private-reasoning-body", json.dumps(events))
        self.assertEqual(response.content, "abcdefgh")
        self.assertTrue(record.stream_completed)
        self.assertEqual(record.envelope.request_hash, "request-fixed")
        self.assertTrue(all(row["total_elapsed_seconds"] is not None for row in progress))

    async def test_heartbeat_does_not_refresh_idle_or_change_provider_counts(self):
        async def run(interval):
            observers = []
            events = []

            async def invoke(observer):
                observers.append(observer)
                _payload(observer, content="one payload")
                await asyncio.Event().wait()

            with self.assertRaises(ProviderAttemptFailure) as caught:
                await run_provider_attempt(
                    invoke, _envelope(idle=0.15), observation_interval_seconds=interval,
                    progress_callback=lambda event, value: events.append((event, deepcopy(value))),
                )
            return caught.exception, observers[0].snapshot(), events

        enabled, enabled_snapshot, events = await run(0.02)
        disabled, disabled_snapshot, disabled_events = await run(0)
        self.assertEqual(enabled.error_code, PROVIDER_STREAM_IDLE_TIMEOUT)
        self.assertEqual(disabled.error_code, enabled.error_code)
        self.assertGreaterEqual(len([event for event, _ in events if event == "attempt_progress"]), 3)
        self.assertNotIn("attempt_progress", [event for event, _ in disabled_events])
        for snapshot in (enabled_snapshot, disabled_snapshot):
            self.assertEqual(snapshot["version"], 1)
            self.assertEqual(snapshot["observed_payload_callback_count"], 1)
            self.assertEqual(snapshot["content_char_count"], len("one payload"))
        self.assertEqual(enabled.record.max_payload_gap_seconds, 0)
        self.assertGreaterEqual(enabled.record.payload_idle_seconds, 0.15)
        self.assertEqual([event for event, _ in events][-2:], ["timeout_cleanup_started", "attempt_failed"])

    async def test_empty_transport_and_ui_heartbeats_do_not_establish_first_payload(self):
        events = []

        async def invoke(observer):
            while True:
                _payload(observer)
                await asyncio.sleep(0.012)

        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                invoke, _envelope(first=0.13, idle=0.04),
                observation_interval_seconds=0.015,
                progress_callback=lambda event, value: events.append((event, deepcopy(value))),
            )
        self.assertEqual(caught.exception.error_code, PROVIDER_FIRST_CHUNK_TIMEOUT)
        self.assertEqual(caught.exception.record.total_chunk_count, 0)
        self.assertIsNone(caught.exception.record.payload_idle_seconds)
        self.assertNotIn("stream_active", [event for event, _ in events])
        self.assertTrue(any(event == "attempt_progress" for event, _ in events))

    async def test_active_stream_total_deadline_is_unchanged_and_summary_is_bounded(self):
        async def invoke(observer):
            while True:
                _payload(observer, content="x")
                await asyncio.sleep(0.012)

        with self.assertRaises(ProviderAttemptFailure) as caught:
            await run_provider_attempt(
                invoke, _envelope(first=0.5, idle=0.3, total=0.36),
                observation_interval_seconds=0.005,
            )
        record = caught.exception.record
        self.assertEqual(caught.exception.error_code, PROVIDER_TOTAL_TIMEOUT)
        self.assertGreaterEqual(record.content_chunk_count, 5)
        self.assertEqual(len(record.activity_summary), 32)
        self.assertEqual(record.activity_summary[-1]["content_char_count"], record.content_char_count)
        self.assertGreaterEqual(record.elapsed_seconds, 0.36)

    async def test_ui_failure_does_not_turn_success_into_failure_or_change_payload(self):
        events = []

        def failing_ui(event, payload):
            events.append(event)
            raise OSError("injected terminal writer failure")

        async def invoke(observer):
            _payload(observer, content="same")
            await asyncio.sleep(0.05)
            return _response("same")

        response, record = await run_provider_attempt(
            invoke, _envelope(), observation_interval_seconds=0.015,
            progress_callback=failing_ui,
        )
        self.assertEqual(response.content, "same")
        self.assertEqual(record.state, "COMPLETED")
        self.assertIn("attempt_progress", events)
        self.assertEqual(events[-1], "attempt_completed")
        self.assertEqual(record.observed_payload_callback_count, 1)

    async def test_cancellation_propagates_and_fences_late_payload_and_ui(self):
        events = []
        observers = []
        active = asyncio.Event()

        async def invoke(observer):
            observers.append(observer)
            _payload(observer, content="partial")
            active.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(run_provider_attempt(
            invoke, _envelope(), observation_interval_seconds=0.015,
            progress_callback=lambda event, value: events.append((event, deepcopy(value))),
        ))
        await active.wait()
        await asyncio.sleep(0.045)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        record = caught.exception.provider_attempt_record
        self.assertEqual(record.failure_type, PROVIDER_CALLER_CANCELLED)
        self.assertEqual(record.partial_payload_disposition, "discarded_after_caller_cancellation")
        self.assertEqual(events[-1][0], "attempt_cancelled")
        count = len(events)
        _payload(observers[0], content="late")
        await asyncio.sleep(0.035)
        self.assertEqual(len(events), count)
        self.assertEqual(observers[0].snapshot()["content_char_count"], len("partial"))
        self.assertEqual(observers[0].snapshot()["late_chunk_count"], 1)

    async def test_logger_io_failure_is_observational_but_dispatch_receipt_stays_required(self):
        invoked = []

        async def invoke(observer):
            invoked.append(True)
            _payload(observer, content="ready")
            await asyncio.sleep(0.025)
            return _response("ready")

        with patch("agent_core.provider_attempt.logger.info", side_effect=OSError("log writer unavailable")):
            response, record = await run_provider_attempt(
                invoke, _envelope(), observation_interval_seconds=0.005,
            )
        self.assertEqual(response.content, "ready")
        self.assertEqual(record.state, "COMPLETED")
        invoked.clear()

        def dispatch_receipt_failure(event, payload):
            if event == "attempt_started":
                raise OSError("required durable dispatch receipt unavailable")

        with patch("agent_core.provider_attempt.logger.info", side_effect=OSError("log writer unavailable")):
            with self.assertRaises(ProviderAttemptFailure) as caught:
                await run_provider_attempt(
                    invoke, _envelope(), observation_interval_seconds=0.005,
                    progress_callback=dispatch_receipt_failure, progress_start_fail_closed=True,
                )
        self.assertEqual(caught.exception.error_code, PROVIDER_ATTEMPT_START_PERSISTENCE_FAILED)
        self.assertEqual(invoked, [])

    async def test_completed_attempt_has_no_late_periodic_event(self):
        events = []

        async def invoke(observer):
            _payload(observer, content="ready")
            return _response("ready")

        _, record = await run_provider_attempt(
            invoke, _envelope(), observation_interval_seconds=0.01,
            progress_callback=lambda event, value: events.append((event, deepcopy(value))),
        )
        count = len(events)
        await asyncio.sleep(0.035)
        self.assertEqual(len(events), count)
        self.assertEqual(events[-1][0], "attempt_completed")
        self.assertEqual(record.content_char_count, 5)


if __name__ == "__main__":
    unittest.main()

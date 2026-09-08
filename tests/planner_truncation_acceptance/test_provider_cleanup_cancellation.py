"""Deterministically cancel the caller during its activity-waiter drain.

The real provider-attempt driver, watchdog and cleanup run unchanged. Only
the synthetic provider and observer wait use synchronization barriers, so a
passing test cannot depend on moving the caller cancellation away from the
previously vulnerable suspension.
"""
import asyncio
from copy import deepcopy

import pytest

from agent_core.provider_attempt import (
    PROVIDER_CALLER_CANCELLED, ProviderStreamObserver, run_provider_attempt,
)
from tests.test_planner_stream_progress import _envelope, _payload, _response


@pytest.mark.parametrize("cancel_caller", [True, False])
def test_activity_cleanup_distinguishes_caller_cancellation_from_child_cleanup(monkeypatch, cancel_caller):
    async def scenario():
        drain_entered = asyncio.Event()
        release_drain = asyncio.Event()
        provider_can_finish = asyncio.Event()
        entered = []
        events = []
        original_wait = ProviderStreamObserver.wait_for_activity

        async def gated_wait(observer, version):
            if entered:
                return await original_wait(observer, version)
            entered.append(asyncio.current_task())
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # run_provider_attempt cancelled this child and now awaits
                # its cleanup. Hold exactly that suspension for the issuer.
                drain_entered.set()
                await release_drain.wait()
                raise

        monkeypatch.setattr(ProviderStreamObserver, "wait_for_activity", gated_wait)

        async def invoke(observer):
            _payload(observer, content="partial candidate")
            await provider_can_finish.wait()
            return _response("synthetic complete response")

        caller = asyncio.create_task(run_provider_attempt(
            invoke, _envelope(), observation_interval_seconds=0.01,
            progress_callback=lambda event, value: events.append((event, deepcopy(value))),
        ))
        try:
            await asyncio.wait_for(drain_entered.wait(), timeout=1)
            assert len(entered) == 1 and entered[0].cancelling() == 1
            assert not entered[0].done() and caller.cancelling() == 0
            if cancel_caller:
                assert caller.cancel("trusted synthetic issuer at activity drain")
            release_drain.set()
            # On the old implementation the swallowed caller cancellation
            # admits this response and returns success, rather than relying
            # on a later timeout to make the test fail.
            provider_can_finish.set()
            if cancel_caller:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await caller
                record = caught.value.provider_attempt_record
                assert caller.cancelled()
                assert record.failure_type == PROVIDER_CALLER_CANCELLED
                assert record.partial_payload_disposition == "discarded_after_caller_cancellation"
                assert record.content_char_count == len("partial candidate")
                assert record.retryable is False
                assert [name for name, _ in events].count("attempt_cancelled") == 1
                assert not any(name in {"attempt_completed", "timeout_cleanup_started", "attempt_failed"} for name, _ in events)
            else:
                response, record = await caller
                assert response.content == "synthetic complete response"
                assert record.state == "COMPLETED"
                assert [name for name, _ in events].count("attempt_completed") == 1
                assert not any(name == "attempt_cancelled" for name, _ in events)
        finally:
            release_drain.set()
            provider_can_finish.set()
            if not caller.done():
                caller.cancel()
                try:
                    await caller
                except BaseException:
                    pass

    asyncio.run(scenario())

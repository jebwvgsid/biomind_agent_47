"""C01: cancellation origin through real go, schema-2 graph and outer loop.

The external provider emits a partial candidate and cancels the actual outer
Task. No graph, go, binder, store or cancellation implementation is replaced.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from agent_core.plan_tracker import load_plan_state, task_status
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.planner_fix_acceptance.test_planner_recorded_workflow import (
    Harness, RecordedModel, evidence,
)


class PartialCancellationModel(RecordedModel):
    partial = '{"schema_version": 2, "steps": [{"step_id": "partial_not_a_plan"'

    def __init__(self, initiator):
        super().__init__([])
        self.initiator = initiator
        self.agent = None
        self.task_id = ''
        self.issue_evidence = {
            'kind': 'isolated_actual_cancellation_issuer',
            'control_event_id': 'offline-cancel-' + initiator,
        }
        self.cancel_accepted = None
        self.late_cancel_accepted = None

    async def ainvoke(self, messages, **kwargs):
        self.calls.append([provider_visible_payload(m) for m in messages])
        assert len(self.calls) == 1, 'Cancellation must not trigger corrective generation'
        chunk = SimpleNamespace(
            message=SimpleNamespace(content=self.partial, additional_kwargs={},
                                    response_metadata={}, tool_call_chunks=[]),
            generation_info={},
        )
        callbacks = list((kwargs.get('config') or {}).get('callbacks') or [])
        assert callbacks, 'The real provider stream observer must be connected'
        for callback in callbacks:
            callback.on_llm_new_token(self.partial, chunk=chunk)
        await asyncio.sleep(.025)
        assert self.cancel_target is not None
        if self.initiator in {'unknown', 'unknown_then_late_user'}:
            # Misleading exception text is deliberately not trusted evidence.
            self.cancel_accepted = self.cancel_target.cancel(
                'Task cancelled by user misleading text',
            )
            if self.initiator == 'unknown_then_late_user':
                # Same event-loop callback, with no await between requests:
                # a later known owner cannot relabel the earlier unknown signal.
                assert self.cancel_target.cancelling() > 0
                self.late_cancel_accepted = self.agent.cancel_managed_run(
                    self.cancel_target,
                    task_id=self.task_id,
                    initiator_kind='user',
                    owner='trusted-control-owner',
                    reason='Late user request after independently unknown cancellation',
                    source_evidence={
                        'kind': 'late_user_cancellation_event',
                        'control_event_id': 'offline-cancel-late-user',
                    },
                )
        else:
            self.cancel_accepted = self.agent.cancel_managed_run(
                self.cancel_target,
                task_id=self.task_id,
                initiator_kind=self.initiator,
                owner='trusted-control-owner',
                reason='Isolated ' + self.initiator + ' cancellation requested',
                source_evidence=self.issue_evidence,
            )
        await asyncio.Future()


@pytest.mark.parametrize('initiator', [
    'user', 'parent_task', 'deadline', 'shutdown', 'unknown',
    'unknown_then_late_user',
])
def test_actual_outer_partial_planner_cancellation_origin_and_no_summary(initiator):
    expected_initiator = 'unknown' if initiator == 'unknown_then_late_user' else initiator
    async def scenario():
        with tempfile.TemporaryDirectory(prefix='planner-cancel-' + initiator + '-') as tmp:
            root = Path(tmp)
            task = 'offline-planner-cancel-' + initiator
            model = PartialCancellationModel(initiator)
            harness = Harness(root, model)
            agent = harness.agent
            model.agent, model.task_id = agent, task
            summary = RecordedModel([])
            agent.terminal_summary_llm = summary
            agent.terminal_summary_policy = {
                'enabled': True, 'provider_available': True,
                'requests_allowed': True, 'remaining_tokens': 100000,
            }
            progress = []
            agent.planner_progress_enabled = True
            agent.planner_progress_interval_seconds = .01
            agent.planner_progress_callback = lambda event, payload: progress.append({
                'event': event, 'payload': deepcopy(dict(payload)),
            })
            finalizer_calls = []
            real_finalizer = agent._finalize_managed_terminal

            async def observe_finalizer(*args, **kwargs):
                # Observe the real method without replacing its behavior.
                finalizer_calls.append({'args': args, 'kwargs': kwargs})
                return await real_finalizer(*args, **kwargs)

            agent._finalize_managed_terminal = observe_finalizer
            try:
                initial = harness.bootstrap(task)
                # Runtime registry contents are synthetic in the guarded
                # bootstrap. go, including its startup registry sync, stays real.
                caller = asyncio.create_task(agent._run_agent_interrupt_loop(
                    initial['task_spec']['goal'], task, resume_initial=True,
                ))
                model.cancel_target = caller
                try:
                    result = await caller
                except asyncio.CancelledError:
                    propagated = True
                else:
                    evidence('C01-not-propagated-' + initiator, {
                        'result': result, 'provider_calls': model.calls,
                        'durable': load_plan_state(root, task), 'events': progress,
                    })
                    raise AssertionError('The real outer swallowed cancellation')
                durable = load_plan_state(root, task)
                task_state = durable['task_state']
                receipt = task_state['cancellation_receipt']
                events_path = root / 'task_outputs' / task / 'context_trace' / 'events.jsonl'
                lineage = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                metrics = agent.profiler.snapshot(task)
                outcome = {
                    'initiator': initiator, 'exception_propagated': propagated,
                    'cancel_accepted': model.cancel_accepted,
                    'late_cancel_accepted': model.late_cancel_accepted,
                    'expected_initiator': expected_initiator,
                    'provider_calls': model.calls, 'partial_payload': model.partial,
                    'progress_events': progress, 'durable': durable,
                    'lineage': lineage, 'profiler': metrics,
                    'summary_calls': summary.calls,
                    'outer_finalizer_calls': finalizer_calls, 'source': __file__,
                }
                evidence('C01-' + initiator, outcome)
                assert caller.cancelled() and model.cancel_accepted is True
                if initiator == 'unknown_then_late_user':
                    assert model.late_cancel_accepted is True
                assert task_status(durable) == 'cancelled'
                assert len(model.calls) == 1 and summary.calls == []
                assert finalizer_calls == [], 'Cancellation must bypass failure finalization'
                assert not durable.get('steps') and not durable.get('execution_attempts')
                assert durable.get('approval_status') != 'approved'
                assert not list(root.rglob('execution_result.json'))
                assert not task_state.get('managed_stop')
                assert task_state['planning_epoch']['planner_calls_total'] == 1
                assert receipt['schema_version'] == 2
                assert receipt['initiator_kind'] == expected_initiator
                assert receipt['scope']['task_id'] == task
                assert receipt['scope']['planning_epoch_id'] == task_state['planning_epoch']['planning_epoch_id']
                assert receipt['active_provider_ref'], 'Cancellation must retain actual provider reference'
                assert receipt['command_started'] is False
                if expected_initiator == 'unknown':
                    assert receipt['owner'] == '' and receipt['source_evidence'] == {}
                    assert 'unknown' in receipt['reason']
                    assert 'misleading text' not in receipt['reason']
                else:
                    assert receipt['owner'] == 'trusted-control-owner'
                    assert receipt['source_evidence'] == model.issue_evidence
                    assert receipt['request_scope'] == receipt['scope']
                cancellation_events = [row for row in lineage
                                       if row['event_kind'] in {'task_cancelled', 'task_user_cancelled'}]
                expected_event = 'task_user_cancelled' if expected_initiator == 'user' else 'task_cancelled'
                assert len(cancellation_events) == 1
                assert cancellation_events[0]['event_kind'] == expected_event
                assert cancellation_events[0]['payload']['initiator_kind'] == expected_initiator
                diagnostics = metrics['final_result']['terminal_diagnostic']
                assert diagnostics['stage'] == ('user_cancellation' if expected_initiator == 'user' else 'cancellation')
                assert diagnostics['initiator_kind'] == expected_initiator
                cancelled_attempts = [row['payload'] for row in progress if row['event'] == 'attempt_cancelled']
                assert len(cancelled_attempts) == 1
                assert cancelled_attempts[0]['failure_type'] == 'PROVIDER_CALLER_CANCELLED'
                assert cancelled_attempts[0]['content_char_count'] == len(model.partial)
                assert not any(row['event'] == 'candidate_rejected' for row in progress)
            finally:
                await harness.close()
    asyncio.run(scenario())

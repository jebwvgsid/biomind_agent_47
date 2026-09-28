"""Thin request-scoped observation scheduling on the existing command service.

This module owns no subprocess, timeout, log collector or independent message
store. The service owns resources; tool_loop owns the durable local history.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time


class CommandObservationCoordinator:
    def __init__(self, service, *, task_id, request_scope, loop_key, stage):
        self.service, self.task_id = service, task_id
        self.scope, self.loop_key, self.stage = dict(request_scope), loop_key, stage
        intervals = getattr(service, 'observation_review_intervals', (30., 60.))
        self.intervals = tuple(max(.001, float(v)) for v in intervals) or (30., 60.)
        self.tick_seconds = max(.001, float(getattr(service, 'observation_tick_seconds', 1.)))
        # Repeated quiet windows coalesce locally up to a bounded model-control review.
        self.model_review_seconds = max(.001, float(getattr(service, 'observation_model_review_seconds', 180.)))

    def assert_request(self):
        from agent_core.plan_tracker import load_plan_state
        from agent_core.request_state import request_scope
        state = load_plan_state(self.service.data_path, self.task_id)
        task = state.get('task_state') or {}
        current = asyncio.current_task()
        if task.get('status') == 'cancelled' or self.service.request_is_cancelled(
                self.task_id, self.scope) or (current is not None and current.cancelling()):
            self.service.cancel_request(self.task_id, self.scope, reason='request_cancelled')
            raise asyncio.CancelledError('REQUEST_CANCELLED')
        if request_scope(state) != self.scope:
            raise RuntimeError('TOOL_CONTEXT_STALE')
        return state

    def check_work_admission(self, name, call_id):
        state = self.assert_request()
        loops = ((state.get('task_state') or {}).get('tool_loops') or {})
        for key, loop in loops.items():
            # A handoff closes its phase loop, not the downstream Plan/Generate
            # work in the same user request. Committed finish is checked by the
            # request's existing managed-stop gate before entering any loop.
            if key != self.loop_key or loop.get('request_scope') != self.scope:
                continue
            for identity, intent in (loop.get('intents') or {}).items():
                if intent.get('admitted') and not intent.get('ending_withdrawn') and intent.get('tool') in {'finish_task', 'submit_planning_handoff'}:
                    if name not in {'finish_task', 'submit_planning_handoff', 'wait_command', 'stop_command'}:
                        raise ValueError('REQUEST_CLOSING_NO_NEW_WORK')
        return state

    def withdraw_uncommitted_ending(self):
        """A confirmed stop is a local correction, never a repeated handoff commit."""
        from agent_core.plan_tracker import save_plan_state
        state = self.assert_request()
        changed = False
        for key, loop in ((state.get('task_state') or {}).get('tool_loops') or {}).items():
            if key != self.loop_key or loop.get('request_scope') != self.scope:
                continue
            for intent in (loop.get('intents') or {}).values():
                if ((intent.get('result') or {}).get('ending_intent_pending')
                        and not intent.get('ending_withdrawn')):
                    intent['ending_withdrawn'] = True
                    changed = True
        if changed:
            save_plan_state(self.service.data_path, self.task_id, state)
            self.service._closing_requests.discard((str(self.task_id), self.service._scope_key(self.scope)))
        return changed

    def _event(self, snapshot):
        value = dict(snapshot)
        value.pop('cmd', None)
        value.pop('timing', None)
        for stream in ('stdout', 'stderr'):
            if stream + '_delta' in value:
                value.pop(stream, None)
        ref = str(value.get('run_ref') or value.get('activity_id') or '')
        # Final receipts are immutable. Observations identify a host revision,
        # not an elapsed-time tick; retries reuse one frozen message.
        version = value.get('observation_seq', value.get('receipt_ref', value.get('status')))
        key = json.dumps([self.scope, ref, version], sort_keys=True, ensure_ascii=False)
        value['event_id'] = 'runtime:' + hashlib.sha256(key.encode()).hexdigest()
        value['source'] = 'host_command_observation'
        value['untrusted_output'] = True
        return value

    async def settle(self, *, cancel=False, reason='request_boundary'):
        if not cancel: self.assert_request()
        result = await self.service.drain_request(self.task_id, self.scope,
            cancel=cancel, reason=reason)
        if not cancel: self.assert_request()
        return result

    async def before_return(self, saved):
        if not self.service.active_runs(self.task_id, self.scope):
            return {}
        started = time.monotonic()
        result = await self.settle()
        return {'wait_seconds':time.monotonic()-started, 'pending_runs': not result.get('all_terminal'),
                'observations':[self._event(r) for r in result.get('runs') or []]}

    async def before_provider(self, saved, batch):
        self.assert_request()
        if not batch or not all(row.get('tool') == 'wait_command' and row.get('admitted')
                               and row.get('result', {}).get('terminal') is False
                               and row.get('result', {}).get('request_executed') is not False
                               and row.get('result', {}).get('run_ref') for row in batch):
            return {}
        # The first exec->running always reaches the model. Coalescing starts
        # only after the complete batch contains maintenance and nothing else.
        refs = {row['result']['run_ref']:dict(row['result']) for row in batch}
        # Host sampling has not shown these bytes to the model. Keep the last
        # delivered cursor until one coalesced observation is durably appended;
        # never advance over discarded intermediate snapshots.
        delivered_cursors = {ref: row.get('next_cursor') for ref, row in refs.items()}
        reviews = int(saved.get('silent_reviews') or 0)
        limit = getattr(self.service, 'command_max_decisions', None)
        exhausted = limit is not None and int(saved.get('decision_calls') or 0) >= limit
        if exhausted:
            return {}  # Tool loop finalizer owns cleanup; never wait forever here.
        review_at = time.monotonic() + (self.intervals[0] if reviews == 0 else self.model_review_seconds)
        started = time.monotonic()
        observations = {}
        while refs:
            self.assert_request()
            changed = False
            for ref, prior in list(refs.items()):
                # A bounded wait belongs to the service. Cancelling this waiter
                # does not dispose its resources or move its original deadline.
                remaining = review_at-time.monotonic()
                quantum = int(max(0, min(self.tick_seconds*1000, max(0, remaining)*1000)))
                current = await self.service.wait_command({
                    'run_ref':ref, 'after_cursor':delivered_cursors[ref], 'max_wait_ms':quantum},
                    task_id=self.task_id, request_scope=self.scope)
                self.assert_request()
                observations[ref] = self._event(current)
                if current.get('terminal') is True or current.get('status') in {
                        'reconciliation_unknown', 'cleanup_unconfirmed', 'not_run'}:
                    refs.pop(ref, None)
                    changed = True
                else:
                    phase_changed = current.get('progress_phase') != prior.get('progress_phase')
                    stage_changed = bool(re.search(r'(?m)^BIOMIND_STAGE\s+(?:begin|end)\b',
                        str(current.get('stdout_delta') or '')))
                    error_changed = bool(current.get('error') and current.get('error') != prior.get('error'))
                    changed |= phase_changed or stage_changed or error_changed
                    refs[ref] = current
            if changed or not refs:
                return {'observations':list(observations.values()), 'wait_seconds':time.monotonic()-started}
            if time.monotonic() >= review_at:
                return {'observations':list(observations.values()), 'wait_seconds':time.monotonic()-started,
                        'silent_review':True}
            # Output may wake the service wait early. Never turn high output
            # volume into a tight host loop or a provider decision per line.
            await asyncio.sleep(min(.05, self.tick_seconds, max(0., review_at-time.monotonic())))

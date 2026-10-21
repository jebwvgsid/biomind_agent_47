"""Observation of the existing formal command adapter, never a second runner.

The task below retains execute_formal_command's intent lock until its original
executor returns. Only this command-tool path opts into early yield; external
step-worker backends keep their existing scheduler and resume contracts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_core.command_inspection_owner import InspectionOwnerMixin, _InspectionRun, _canonical, _once


def get_formal_observer(service):
    observer = getattr(service, 'formal_observer', None)
    if observer is None:
        observer = FormalCommandObserver(service)
        service.formal_observer = observer
    return observer


class FormalCommandObserver(InspectionOwnerMixin):
    def __init__(self, service):
        self.service = service
        self._init_owners(service.first_yield_ms, service.cleanup_timeout_seconds)

    def __getattr__(self, name):
        return getattr(self.service, name)

    @staticmethod
    def _deadline_timestamp(value):
        if not isinstance(value, str):
            raise ValueError('TASK_DEADLINE_ISO_REQUIRED')
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if date.tzinfo is None:
                raise ValueError()
            stamp = date.timestamp()
            if not math.isfinite(stamp):
                raise ValueError()
            return stamp
        except (ValueError, OverflowError):
            raise ValueError('TASK_DEADLINE_ISO_REQUIRED') from None

    @staticmethod
    def _iso(stamp):
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()

    def task_deadline(self, task_id, scope):
        from agent_core.plan_tracker import load_plan_state
        task = load_plan_state(self.data_path, task_id).get('task_state', {})
        row = task.get('pi_execution_budget') or {}
        if row.get('request_key') != (scope or {}).get('request_key'):
            return None
        return row.get('original_deadline_at')

    def tighten_task_deadline(self, task_id, scope, value):
        """An absolute request budget survives new calls, replay and reconnect."""
        from agent_core.pending_input import mutate
        from agent_core.request_state import request_scope
        stamp = self._deadline_timestamp(value)
        def update(state):
            if request_scope(state) != scope:
                raise ValueError('TOOL_CONTEXT_STALE')
            task = state.setdefault('task_state', {})
            row = task.get('pi_execution_budget') or {}
            if row.get('request_key') == scope.get('request_key') and row.get('original_deadline_at'):
                stamp_before = self._deadline_timestamp(row['original_deadline_at'])
                effective = min(stamp, stamp_before)
            else:
                effective = stamp
            task['pi_execution_budget'] = {'request_key': scope.get('request_key'),
                'original_deadline_at': self._iso(effective)}
            return effective
        effective = mutate(self.data_path, task_id, update)
        for owner in self._select_owners(task_id, scope):
            if owner.result and owner.result.get('terminal'):
                continue
            deadline = time.monotonic() + effective - time.time()
            if owner.deadline_at is None or deadline < owner.deadline:
                owner.deadline = deadline
                owner.deadline_at = self._iso(effective)
                owner.metrics['hard_deadline_monotonic'] = deadline
        return self._iso(effective)

    @staticmethod
    def validate_monitoring(value):
        result = dict(value or {})
        for key in ('next_check_seconds', 'review_interval_seconds', 'eta_seconds'):
            if key in result and (type(result[key]) not in (int, float) or not math.isfinite(result[key])
                                  or not 0.01 <= result[key] <= (31536000 if key == 'eta_seconds' else 86400)):
                raise ValueError('RUN_MONITOR_INTERVAL_INVALID:' + key)
        return result

    def _monitor_row(self, task_id, run_ref):
        from agent_core.plan_tracker import load_plan_state
        return (load_plan_state(self.data_path, task_id).get('task_state', {}).get('pi_run_monitor') or {}).get(run_ref, {})

    def _save_monitor(self, task_id, run_ref, row):
        from agent_core.pending_input import mutate
        def update(state):
            state.setdefault('task_state', {}).setdefault('pi_run_monitor', {})[run_ref] = row
        mutate(self.data_path, task_id, update)

    def _init_monitor(self, owner, config):
        if config is None:
            return
        config = self.validate_monitoring(config)
        if self._monitor_row(owner.task_id, owner.run_ref):
            return
        self._save_monitor(owner.task_id, owner.run_ref, {
            'scope_key': owner.scope_key, 'run_ref': owner.run_ref,
            'first_check_seconds': config.get('next_check_seconds', 180),
            'review_interval_seconds': config.get('review_interval_seconds', 180),
            'eta_seconds': config.get('eta_seconds'), 'next_check_at': None, 'eta_at': None,
            'last_check_at': None, 'log_cursor': None, 'events': [], 'checked_terminal': False,
            'check_sequence': 0, 'snapshots': {}, 'receipt_sequence': 0,
            'original_deadline_at': owner.deadline_at, 'created_at': self._iso(time.time())})

    def _schedule_started_monitor(self, owner, row):
        """Expose the actual-start schedule before the first background sample."""
        started = owner.metrics.get('process_started_epoch') if owner is not None else None
        if not row or started is None or row.get('process_started_at') is not None:
            return False
        row['process_started_at'] = self._iso(started)
        first = started + row['first_check_seconds']
        row['next_check_at'] = self._iso(min(first, self._deadline_timestamp(row['next_check_at']))) if row.get('next_check_at') else self._iso(first)
        if row.get('eta_seconds') is not None:
            row['eta_at'] = self._iso(started + row['eta_seconds'])
        return True

    def prepare_check(self, run_ref, task_id, scope, result, *, next_check_seconds=None, eta_seconds=None, review=True):
        """Issue bounded snapshot evidence, without consuming events or logs."""
        values = {k: v for k, v in {'next_check_seconds': next_check_seconds, 'eta_seconds': eta_seconds}.items() if v is not None}
        self.validate_monitoring(values)
        row = self._monitor_row(task_id, run_ref)
        if not row or row.get('scope_key') != self._scope_key(scope):
            return result
        now = time.time()
        if eta_seconds is not None:
            row['eta_at'] = self._iso(now + eta_seconds)
            row['eta_notified'] = False
        # A prestart read must not stand in for the first running check.
        running = bool(row.get('process_started_at'))
        if next_check_seconds is not None and running:
            row['next_check_at'] = self._iso(now + next_check_seconds)
        row['receipt_sequence'] = row.get('receipt_sequence', 0) + 1
        snapshot = {'sequence': row['receipt_sequence'], 'event_ids': [e['event_id'] for e in row['events']] if review else [],
            'next_cursor': result.get('next_cursor'), 'terminal': bool(result.get('terminal')),
            'review': review, 'running': running, 'read_at': self._iso(now),
            'next_check_seconds': next_check_seconds, 'received': False, 'judged': False}
        snapshot_id = uuid.uuid4().hex
        snapshots = row.setdefault('snapshots', {})
        snapshots[snapshot_id] = snapshot
        # A stale acknowledgement cannot consume new events. Retain a small
        # window in the existing task record, not another job database.
        row['snapshots'] = dict(list(snapshots.items())[-16:])
        self._save_monitor(task_id, run_ref, row)
        return {**result, **{k: row.get(k) for k in ('next_check_at', 'eta_at', 'last_check_at')},
                'check_receipt': {'run_ref': run_ref, 'snapshot_id': snapshot_id, 'scope_key': row['scope_key'],
                                  'event_ids': snapshot['event_ids'], 'review': review},
                'task_deadline_at': self.task_deadline(task_id, scope)}

    def acknowledge_check(self, run_ref, task_id, scope, receipt, *, phase, entry_id, tool_call_id, decision_entry_id=None):
        """Host-only idempotent ACK of actual Pi entries; never scientific PASS."""
        row = self._monitor_row(task_id, run_ref)
        if not row or row.get('scope_key') != self._scope_key(scope):
            raise ValueError('RUN_CHECK_SCOPE_MISMATCH')
        snapshot = row.get('snapshots', {}).get(receipt.get('snapshot_id'))
        if (not snapshot or receipt.get('run_ref') != run_ref or receipt.get('scope_key') != row['scope_key']
                or receipt.get('event_ids') != snapshot['event_ids'] or not entry_id or not tool_call_id
                or phase not in {'received', 'judged'}):
            raise ValueError('RUN_CHECK_RECEIPT_INVALID')
        if snapshot.get('entry_id') and (snapshot['entry_id'] != entry_id or snapshot['tool_call_id'] != tool_call_id):
            raise ValueError('RUN_CHECK_ENTRY_MISMATCH')
        if phase == 'received' and not snapshot['received']:
            snapshot.update(received=True, entry_id=entry_id, tool_call_id=tool_call_id, received_at=self._iso(time.time()))
            if snapshot['sequence'] > row.get('received_sequence', 0):
                row['received_sequence'] = snapshot['sequence']
                # Cursor snapshots are monotone only for forward reads. A
                # diagnostic read of old bytes must never move Main backwards.
                owner, _ = self._lookup(run_ref, task_id, scope)
                cursor = snapshot.get('next_cursor')
                if owner is not None and cursor:
                    old = self._offsets(owner, row.get('log_cursor'))
                    new = self._offsets(owner, cursor)
                    row['log_cursor'] = self._cursor(owner, {k: max(old[k], new[k]) for k in old})
            row['result_received_at'] = snapshot['received_at']
            if snapshot['review']:
                row['judgment_pending'] = True
        if phase == 'judged' and not snapshot['judged']:
            if not snapshot['received'] or not decision_entry_id:
                raise ValueError('RUN_CHECK_DECISION_WITHOUT_RECEIPT')
            snapshot.update(judged=True, decision_entry_id=decision_entry_id, judged_at=self._iso(time.time()))
            covered = set(snapshot['event_ids'])
            row['events'] = [e for e in row['events'] if e['event_id'] not in covered]
            if snapshot['review'] and (snapshot['running'] or snapshot['terminal']):
                row['last_check_at'] = snapshot['judged_at']
                row['check_sequence'] = row.get('check_sequence', 0) + 1
                if snapshot['terminal']:
                    row['checked_terminal'] = True
                    row['next_check_at'] = None
                elif not row.get('checked_terminal'):
                    row['next_check_at'] = self._iso(time.time() + (snapshot['next_check_seconds'] or row['review_interval_seconds']))
            row['judgment_pending'] = any(s['received'] and s['review'] and not s['judged'] for s in row['snapshots'].values())
            row['decision_entry_id'] = decision_entry_id
        self._save_monitor(task_id, run_ref, row)
        return {'status': 'acknowledged', 'phase': phase, 'run_ref': run_ref,
                'snapshot_id': receipt['snapshot_id'], 'scientific_validation': 'not_assessed'}

    def monitor_due(self, task_id, scope):
        """Low-cost state sampling. Reading never consumes a semantic event."""
        from agent_core.plan_tracker import load_plan_state
        rows = load_plan_state(self.data_path, task_id).get('task_state', {}).get('pi_run_monitor') or {}
        events, active = [], []
        now = time.time()
        for ref, stored in rows.items():
            if stored.get('scope_key') != self._scope_key(scope):
                continue
            row = dict(stored)
            row['events'] = list(row.get('events', []))
            owner, saved = self._lookup(ref, task_id, scope)
            result = owner.result or {} if owner is not None else saved or {}
            metrics = owner.metrics if owner is not None else {}
            terminal = bool(result.get('terminal'))
            owner_unavailable = owner is None or bool(owner.task is not None and owner.task.done() and not terminal)
            status = result.get('status', metrics.get('phase', 'unknown'))
            self._schedule_started_monitor(owner, row)
            summary = {'run_ref': ref, 'status': status, 'terminal': terminal,
                'hostname': socket.gethostname(), 'execution_backend': 'local_owned_process',
                'exit_code': result.get('exit_code'), 'error': result.get('error'),
                'owner_available': not owner_unavailable, 'process_alive': metrics.get('process_alive'),
                'activity': metrics.get('cpu_activity', 'unknown'),
                'cleanup_confirmed': result.get('cleanup_confirmed', False),
                'original_deadline_at': owner.deadline_at if owner is not None else result.get('original_deadline_at'),
                'task_deadline_at': self.task_deadline(task_id, scope),
                'next_check_at': row.get('next_check_at'), 'eta_at': row.get('eta_at'),
                'last_check_at': row.get('last_check_at'), 'log_cursor': row.get('log_cursor'),
                'result_received_at': row.get('result_received_at'), 'judgment_pending': row.get('judgment_pending', False),
                'log_refs': {n: str(self._task_dir(task_id) / ref / (n + '.log')) for n in ('stdout', 'stderr')},
                'scientific_validation': 'not_assessed',
                'diagnostic_due': metrics.get('diagnostic_due')}
            reason = None
            if terminal and not row.get('checked_terminal'):
                reason = 'terminal'
                row['events'] = [event for event in row['events'] if event['reason'] == 'terminal']
            elif not terminal and not row.get('events'):
                if owner_unavailable and (not row.get('last_check_at') or row.get('next_check_at') and now >= self._deadline_timestamp(row['next_check_at'])):
                    reason = 'owner_unavailable'
                elif row.get('eta_at') and not row.get('eta_notified') and now >= self._deadline_timestamp(row['eta_at']):
                    reason = 'eta_due'
                    row['eta_notified'] = True
                elif row.get('next_check_at') and now >= self._deadline_timestamp(row['next_check_at']):
                    reason = 'review_due' if row.get('last_check_at') else 'first_check'
            if reason and not any(e['reason'] == reason for e in row['events']):
                event_id = hashlib.sha256(_canonical([ref, reason, row.get('last_check_at'), row.get('eta_at')]).encode()).hexdigest()[:24]
                row['events'].append({'event_id': event_id, 'run_ref': ref, 'reason': reason,
                    'check_key': ref + (':terminal' if terminal else ':review:' + str(row.get('check_sequence', 0))),
                    'observed_at': self._iso(now)})
            if row != stored:
                self._save_monitor(task_id, ref, row)
            events.extend({**summary, **event} for event in row['events'])
            if not terminal:
                active.append(summary)
        return {'events': events, 'active_runs': active, 'sampled_at': self._iso(now)}

    def _task_dir(self, task_id):
        # Reuse validated task naming without routing formal evidence to inspections.
        return self.service._task_dir(task_id).parent / 'attempts'

    def _identity(self, task_id, call_id, scope=None):
        from agent_core.plan_tracker import load_plan_state
        from agent_core.request_state import request_scope
        if scope is None: scope = request_scope(load_plan_state(self.data_path, task_id))
        digest = hashlib.sha256(json.dumps([scope, call_id], sort_keys=True).encode()).hexdigest()[:24]
        return 'attempt-' + digest

    def has_intent(self, task_id='', request_scope=None, call_id=''):
        return bool(call_id and (self._task_dir(task_id) / self._identity(task_id, call_id, request_scope) / 'observation_intent.json').is_file())

    def reconcile_call(self, *, task_id, request_scope, call_id):
        ref = self._identity(task_id, call_id, request_scope)
        if not (self._task_dir(task_id) / ref / 'observation_intent.json').is_file(): return None
        owner, saved = self._lookup(ref, task_id, request_scope)
        return self._snapshot(owner) if owner is not None else saved

    def _read_terminal(self, directory, record):
        """One conservative reader for replay and serial admission after restart."""
        ref = directory.name
        unknown = self._unknown(ref, record)
        try:
            if record.get('run_ref') != ref or not record.get('task_id') or not record.get('scope_key'):
                raise ValueError('FORMAL_INTENT_IDENTITY_INVALID')
            paths = [directory / n for n in ('exit_status.json', 'observation_cancelled.json', 'observation_result.json')
                     if (directory / n).is_file()]
            if not paths:
                return None, unknown
            results = [json.loads(p.read_text()) for p in paths]
            for result in results:
                if not isinstance(result, dict) or result.get('terminal') is not True or result.get('cleanup_confirmed') is not True:
                    raise ValueError('FORMAL_TERMINAL_INCOMPLETE')
                if result.get('task_id') != record['task_id'] or result.get('attempt_id') != ref:
                    raise ValueError('FORMAL_TERMINAL_IDENTITY_MISMATCH')
                if result.get('status') in {'not_run', 'stage_blocked'}:
                    payload = {k: v for k, v in result.items() if k != 'terminal_sha256'}
                    if (result.get('observation_schema_version') != 1
                            or result.get('terminal_sha256') != hashlib.sha256(_canonical(payload).encode()).hexdigest()
                            or result.get('arguments_sha256') != record.get('arguments_sha256')
                            or result.get('scope_key') != record['scope_key']
                            or result.get('call_id') != record['call_id']
                            or result.get('execution_started') is not False or result.get('exit_code') is not None
                            or result.get('host_boundary') != 'before_launch_delegation'):
                        raise ValueError('FORMAL_NOT_RUN_EVIDENCE_INVALID')
                    if len(results) != 1:
                        raise ValueError('FORMAL_TERMINAL_CONFLICT')
                else:
                    if (result.get('status') not in {'exited', 'cancelled'}
                            or self._scope_key(result.get('request_scope')) != record.get('runtime_scope_key', record['scope_key'])
                            or (record.get('arguments_hash') and result.get('arguments_hash') != record['arguments_hash'])):
                        raise ValueError('FORMAL_TERMINAL_SCOPE_OR_ARGUMENTS_MISMATCH')
            if len(results) > 1 and any(r != results[0] for r in results[1:]):
                raise ValueError('FORMAL_TERMINAL_CONFLICT')
            return results[0], None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return None, {**unknown, 'error': 'FORMAL_TERMINAL_EVIDENCE_INVALID', 'detail': str(exc)}

    def _orphan_snapshots(self, task_id, request_scope):
        scope = self._scope_key(request_scope)
        rows = []
        for path in self._task_dir(task_id).glob('attempt-*/observation_intent.json'):
            try:
                record = json.loads(path.read_text())
                ref = record['run_ref']
                if ref in self._owners or (request_scope is not None and record.get('scope_key') != scope):
                    continue
                _, unknown = self._read_terminal(path.parent, record)
                if unknown:
                    rows.append(unknown)
            except (OSError, ValueError, KeyError):
                rows.append(self._unknown(path.parent.name, {}))
        return rows

    @staticmethod
    def _unknown(ref, record):
        return {'tool': 'exec_command', 'status': 'reconciliation_unknown', 'terminal': False,
                'run_ref': ref, 'attempt_id': ref, 'exit_code': None,
                'owner_available': False, 'cleanup_confirmed': False,
                'error': 'FORMAL_OWNER_LOST_NO_RELAUNCH',
                'original_deadline_at': record.get('original_deadline_at')}

    def _lookup(self, run_ref, task_id, request_scope, *, management=False):
        if not isinstance(run_ref, str) or not re.fullmatch(r'attempt-[a-f0-9]{24}', run_ref):
            raise ValueError('FORMAL_RUN_REF_INVALID')
        scope = self._scope_key(request_scope)
        owner = self._owners.get(run_ref)
        if owner is not None:
            if owner.task_id != str(task_id) or owner.scope_key != scope:
                raise ValueError('FORMAL_RUN_SCOPE_MISMATCH')
            return owner, None
        directory = self._task_dir(task_id) / run_ref
        intent = directory / 'observation_intent.json'
        if not intent.is_file():
            raise ValueError('FORMAL_RUN_UNKNOWN')
        try:
            record = json.loads(intent.read_text())
        except (OSError, ValueError):
            return None, self._unknown(run_ref, {})
        if record.get('scope_key') != scope or record.get('task_id') != str(task_id):
            raise ValueError('FORMAL_RUN_SCOPE_MISMATCH')
        task_deadline = self.task_deadline(task_id, request_scope)
        if task_deadline and (not record.get('original_deadline_at')
                or self._deadline_timestamp(task_deadline) < self._deadline_timestamp(record['original_deadline_at'])):
            record['original_deadline_at'] = task_deadline
        result, unknown = self._read_terminal(directory, record)
        if unknown:
            return None, unknown
        duration = float((result.get('runtime_metadata') or {}).get('elapsed_seconds', 0))
        now = time.monotonic()
        owner = _InspectionRun(str(task_id), scope, record['call_id'],
            {'max_output_chars': record.get('max_output_chars', 12000)}, result.get('stage', ''),
            run_ref, directory, now-duration, now, record.get('original_deadline_at'), record['cursor_key'])
        owner.result = {**result, 'replayed': True, 'terminal': result.get('terminal', True), 'run_ref': run_ref,
                        'duration_seconds': duration, 'cleanup_confirmed': result.get('cleanup_confirmed', True)}
        owner.metrics = {'restored_terminal': True, 'phase': result['status'], 'process_alive': False}
        self._owners[run_ref] = owner
        return owner, None

    async def start(self, arguments, *, task_id='', stage='', request_scope=None, call_id='', yield_time_ms=None, monitoring=None):
        from execution.runtime_contract import EXECUTION_RUNTIME_POLICY
        from execution.executor import _env_float
        from agent_core.plan_tracker import load_plan_state
        from agent_core.request_state import request_scope as current_scope
        if not isinstance(arguments, dict) or not call_id:
            return self._refusal('FORMAL_INTENT_ARGUMENTS_INVALID')
        timeout_ms = arguments.get('timeout_ms')
        if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms <= 0):
            return self._refusal('FORMAL_TIMEOUT_INVALID', execution_started=False)
        if monitoring is not None:
            self.validate_monitoring(monitoring)
        started_epoch, started = time.time(), time.monotonic()
        scope = self._scope_key(request_scope)
        ref = self._identity(task_id, call_id, request_scope)
        directory = self._task_dir(task_id) / ref
        intent_path = directory / 'observation_intent.json'
        digest = hashlib.sha256(_canonical(arguments).encode()).hexdigest()
        # Wait before allocating a process owner/deadline. The original tool
        # call remains active; Generate never polls an approval as a command.
        native = getattr(self.service, '_execution_profile', 'protected') == 'native'
        if not intent_path.is_file() and not native:
            from agent_core.execution_permissions import authorize_command
            await authorize_command(self.service, arguments, task_id=task_id, call_id=call_id)
            started_epoch, started = time.time(), time.monotonic()
        with self._owner_lock:
            if intent_path.is_file():
                try:
                    record = json.loads(intent_path.read_text())
                except (OSError, ValueError):
                    return self._unknown(ref, {})
                if record.get('arguments_sha256') != digest:
                    return self._refusal('FORMAL_INTENT_CONFLICT', run_ref=ref)
                owner, saved = self._lookup(ref, task_id, request_scope)
                if owner is None:
                    return saved
            else:
                if self.request_is_cancelled(task_id, request_scope):
                    return self._refusal('REQUEST_CANCELLED')
                if (str(task_id), scope) in self._closing_requests:
                    return self._refusal('REQUEST_CLOSING')
                if self.active_runs(task_id, None):
                    return self._refusal('PRIOR_INSPECTION_REQUIRES_RECONCILIATION')
                from execution.script_validation import explicit_script_diagnostic
                diagnostic = None if native else explicit_script_diagnostic(arguments)
                if diagnostic:
                    return diagnostic
                # Actual targets are independent of editable notes. The same
                # check runs again under exclusive owned preparation.
                from agent_core.work_plan import action_targets
                try:
                    if not native:
                        action_targets(load_plan_state(self.data_path, task_id), arguments, self.service.project_for_task(task_id),command_service=self.service,task_id=task_id)
                except ValueError as exc:
                    return {**self._refusal(str(exc)), 'execution_started': False, 'phase': 'action_admission'}
                policy = EXECUTION_RUNTIME_POLICY
                seconds = (float(arguments['timeout_ms'])/1000 if arguments.get('timeout_ms') else
                           _env_float(policy.wall_timeout_env, policy.wall_timeout_default_seconds))
                deadline = started + seconds if seconds > 0 else float('inf')
                deadline_at = self._iso(started_epoch + seconds) if seconds > 0 else None
                task_deadline_at = self.task_deadline(task_id, request_scope)
                if task_deadline_at:
                    sampled_wall, sampled_mono = time.time(), time.monotonic()
                    task_remaining = self._deadline_timestamp(task_deadline_at) - sampled_wall
                    if task_remaining <= 0:
                        return self._refusal('TASK_HARD_DEADLINE_EXPIRED', execution_started=False,
                                             original_deadline_at=task_deadline_at)
                    if sampled_mono + task_remaining < deadline:
                        deadline, deadline_at = sampled_mono + task_remaining, task_deadline_at
                owner = _InspectionRun(str(task_id), scope, str(call_id), dict(arguments), stage,
                    ref, directory, started, deadline, deadline_at, uuid.uuid4().hex)
                owner.metrics = {'phase': 'preparing', 'last_output_monotonic': started,
                                 'expected_request_scope': current_scope(load_plan_state(self.data_path, task_id)),
                                 'process_alive': None, 'formal_wall_timeout_disabled': deadline_at is None,
                                 'hard_deadline_monotonic': deadline if deadline_at else None,
                                 'managed_semantic_monitoring': monitoring is not None}
                record = {'task_id': str(task_id), 'scope_key': scope, 'call_id': str(call_id),
                    'runtime_scope_key': self._scope_key(current_scope(load_plan_state(self.data_path, task_id))),
                    'run_ref': ref, 'arguments_sha256': digest, 'original_deadline_at': deadline_at,
                    'arguments_hash': hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                    'cursor_key': owner.cursor_key, 'max_output_chars': arguments.get('max_output_chars', 12000),
                    'monitoring': monitoring, 'task_deadline_at': task_deadline_at,
                    'created_at': datetime.now(timezone.utc).isoformat()}
                try:
                    _once(intent_path, record)
                except OSError as exc:
                    return self._refusal('FORMAL_INTENT_PERSIST_FAILED', detail=str(exc))
                self._owners[ref] = owner
                self._init_monitor(owner, monitoring)
                owner.task = asyncio.create_task(self._drive_formal(owner), name='formal-observer-' + ref)
        wait = self.first_yield_ms if yield_time_ms is None else max(0, min(120000, int(yield_time_ms)))
        await self._wait_owner(owner, wait/1000)
        return self._snapshot(owner)

    async def _drive_formal(self, owner):
        from agent_core.action_runtime import execute_formal_command, PrelaunchAdmissionRejected
        if getattr(self.service, '_execution_profile', 'protected') == 'native':
            from agent_core.native_command import execute_native_command as execute_formal_command
        from agent_core.execution_permissions import PermissionDenied
        owner.metrics['launch_delegated'] = False
        try:
            result = await execute_formal_command(self.service, owner.arguments, task_id=owner.task_id,
                call_id=owner.call_id, stage=owner.stage, runtime_observation=owner.metrics,
                cancel_event=owner.cancelled,
                original_deadline=owner.deadline if owner.deadline_at else None)
            terminal = (result.get('status') not in {'running', 'reconciliation_required', 'reconciliation_unknown'}
                        and result.get('cleanup_confirmed') is not False)
            if not terminal:
                result = {**result, 'status': 'reconciliation_unknown',
                          'error': result.get('error') or 'FORMAL_EXECUTOR_OWNER_NOT_ATTACHED'}
            if result.get('status') == 'not_run' and result.get('execution_started') is False and owner.metrics.get('launch_delegated') is False:
                self._persist_not_run(owner, result)
                return
            saved_result = {**result, 'terminal': terminal, 'run_ref': owner.run_ref,
                'duration_seconds': time.monotonic()-owner.started,
                'cleanup_confirmed': result.get('cleanup_confirmed', terminal)}
            if terminal and not (owner.directory/'exit_status.json').is_file():
                saved_result['receipt_ref'] = str(owner.directory/'observation_result.json')
                _once(owner.directory/'observation_result.json', saved_result)
            owner.result = saved_result
        except PermissionDenied as exc:
            self._persist_not_run(owner, {'error':str(exc)})
        except PrelaunchAdmissionRejected as exc:
            if (owner.metrics.get('launch_delegated') is False
                    and (owner.metrics.get('preparation_settlement') or {}).get('safe_to_release') is True):
                self._persist_not_run(owner, {'error': exc.code, **exc.diagnostic})
            else:
                owner.result = {**self._unknown(owner.run_ref, {}), 'error': str(exc),
                    'diagnostic': exc.diagnostic,
                    'preparation_effects': owner.metrics.get('preparation_settlement')}
        except asyncio.CancelledError:
            receipt = owner.directory / 'exit_status.json'
            if receipt.is_file():
                owner.result = {**json.loads(receipt.read_text()), 'run_ref': owner.run_ref,
                                'duration_seconds': time.monotonic()-owner.started}
            elif (owner.metrics.get('launch_delegated') is False
                    and (owner.metrics.get('preparation_settlement') or {}).get('safe_to_release') is True):
                owner.result = {'status': 'cancelled', 'terminal': True, 'execution_started': False,
                    'run_ref': owner.run_ref, 'attempt_id': owner.run_ref, 'exit_code': None,
                    'task_id': owner.task_id, 'request_scope': self._request_scope(owner),
                    'arguments_hash': hashlib.sha256(json.dumps(owner.arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                    'cleanup_confirmed': True, 'duration_seconds': time.monotonic()-owner.started,
                    'preparation_effects': owner.metrics.get('preparation_settlement'),
                    'receipt_ref': str(owner.directory/'observation_cancelled.json')}
                try:
                    _once(owner.directory/'observation_cancelled.json', owner.result)
                except OSError as exc:
                    owner.result.update(terminal=False, status='reconciliation_unknown', evidence_complete=False, persistence_error=str(exc))
            else:
                owner.result = {**self._unknown(owner.run_ref, {}), 'owner_available': True}
        except Exception as exc:
            import traceback
            owner.result = {**self._unknown(owner.run_ref, {}), 'owner_available': True,
                            'exit_code': owner.metrics.get('process_returncode', owner.metrics.get('process_exit_code')),
                            'cleanup_confirmed': bool(owner.metrics.get('cleanup_confirmed')),
                            'evidence_complete': False,
                            'execution_started': False if owner.metrics.get('launch_delegated') is False else (True if owner.metrics.get('process_started_at') else None),
                            'phase': owner.metrics.get('phase'),
                            'preparation_effects': {'directory': str(owner.directory), 'retained': True, 'rolled_back': False,
                                'settlement': owner.metrics.get('preparation_settlement')},
                            'error': type(exc).__name__ + ': ' + str(exc), 'traceback': traceback.format_exc()}
            try:
                _once(owner.directory/'observation_error.json', owner.result)
            except OSError as storage_error:
                owner.result['persistence_error'] = str(storage_error)
        finally:
            if owner.result and owner.result.get('terminal') and owner.result.get('cleanup_confirmed'):
                owner.metrics['cleanup_unconfirmed'] = False
                owner.metrics.pop('cleanup_reason', None)
            owner.metrics['phase'] = (owner.result or {}).get('status', 'reconciliation_unknown')
            from agent_core.cli_tool_activity import runtime_event
            runtime_event(owner.metrics['phase'], owner.result or {})
            from agent_core.cli_tool_activity import register_owner_logs
            register_owner_logs(owner)

    def _request_scope(self, owner):
        from agent_core.plan_tracker import load_plan_state
        from agent_core.request_state import request_scope
        scope = request_scope(load_plan_state(self.data_path, owner.task_id))
        return scope if self._scope_key(scope) == owner.scope_key else None

    def _persist_not_run(self, owner, diagnostic):
        settlement = owner.metrics.get('preparation_settlement') or {}
        if settlement.get('safe_to_release') is not True or owner.metrics.get('launch_delegated') is not False:
            owner.result = {**self._unknown(owner.run_ref, {}), **diagnostic,
                            'status':'reconciliation_unknown', 'terminal':False,
                            'cleanup_confirmed':False, 'preparation_effects':settlement}
            return
        path = owner.directory / 'observation_result.json'
        capability_missing = diagnostic.get('status') == 'capability_missing'
        result = {**diagnostic, 'status': 'stage_blocked' if capability_missing else 'not_run', 'terminal': True,
                  'execution_started': False, 'exit_code': None, 'cleanup_confirmed': True,
                  'task_id': owner.task_id, 'attempt_id': owner.run_ref, 'run_ref': owner.run_ref,
                  'scope_key': owner.scope_key, 'call_id': owner.call_id,
                  'arguments_sha256': hashlib.sha256(_canonical(owner.arguments).encode()).hexdigest(),
                  'observation_schema_version': 1, 'host_boundary': 'before_launch_delegation',
                  'phase': diagnostic.get('phase', 'syntax_admission'), 'receipt_ref': str(path),
                  'preparation_effects': {'intent': str(owner.directory/'observation_intent.json'),
                      'directory': str(owner.directory), 'retained': True, 'rolled_back': False,
                      'settlement': settlement},
                  'duration_seconds': time.monotonic()-owner.started}
        if capability_missing:
            result['capability_missing'] = True
            result['affected_capability'] = 'formal_input_read_only_execution'
        result['terminal_sha256'] = hashlib.sha256(_canonical(result).encode()).hexdigest()
        try:
            _once(path, result)
        except OSError as exc:
            owner.result = {**self._unknown(owner.run_ref, {}), 'execution_started': False,
                            'phase': result['phase'], 'error': result.get('error'),
                            'persistence_error': str(exc), 'evidence_complete': False,
                            'preparation_effects': result['preparation_effects']}
            return
        owner.result = result

    def _snapshot(self, owner, cursor=None):
        result = super()._snapshot(owner, cursor)
        result['observation_only'] = False
        result['attempt_id'] = owner.run_ref
        result['scientific_validation'] = 'not_assessed'
        limit = owner.arguments.get('max_output_chars', 12000)
        if type(limit) is not int or not 256 <= limit <= 64000:
            limit = 12000
        for name in ('stdout', 'stderr'):
            value = result.get(name, '')
            if isinstance(value, str) and len(value) > limit:
                result[name] = value[:limit//2] + value[-(limit-limit//2):]
                result.setdefault('truncated', {})[name] = True
                result.setdefault('output_projection', {})[name] = {'kind': 'head_and_tail', 'full_source_ref': result['log_refs'][name]}

        result['activity'] = owner.metrics.get('cpu_activity', 'unknown')
        result['diagnostic_due'] = owner.metrics.get('diagnostic_due')
        result['termination_reason'] = owner.metrics.get('termination_reason')
        result['task_deadline_at'] = self.task_deadline(owner.task_id, self._request_scope(owner))
        row = self._monitor_row(owner.task_id, owner.run_ref)
        if self._schedule_started_monitor(owner, row):
            self._save_monitor(owner.task_id, owner.run_ref, row)
        for key in ('next_check_at', 'eta_at', 'last_check_at'):
            if row:
                result[key] = row.get(key)
        if owner.deadline_at is None:
            result['remaining_seconds'] = None
            result['original_deadline_at'] = None
            result['wall_timeout_disabled'] = True
        return result

    async def drain_request(self, task_id='', request_scope=None, *, cancel=False, reason='', timeout_seconds=None):
        # Wall=0 is disabled. A finite close observation limit reports unknown;
        # it never invents a command deadline or kills an otherwise active job.
        if timeout_seconds is None and any(o.deadline_at is None for o in self._select_owners(task_id, request_scope)):
            timeout_seconds = self.cleanup_timeout_seconds
        return await super().drain_request(task_id, request_scope, cancel=cancel, reason=reason,
                                            timeout_seconds=timeout_seconds)

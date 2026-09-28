"""Request-owned inspection lifetimes; shell/sandbox execution stays in its service.

A tool waiter never owns the worker. Persistent intent is written before creating
that worker, and a missing owner never grants permission to repeat a launch.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import hmac
import json
import os
import re
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _once(path, value):
    """Atomically publish without replacing a previous fact (including on replay)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class _InspectionRun:
    task_id: str
    scope_key: str
    call_id: str
    arguments: dict
    stage: str
    run_ref: str
    directory: Path
    started: float
    deadline: float | None
    deadline_at: str | None
    cursor_key: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: Any = None
    result: dict | None = None
    sequence: int = 0
    observation_signature: Any = None
    metrics: dict = field(default_factory=dict)


class InspectionOwnerMixin:
    def _init_owners(self, first_yield_ms, cleanup_timeout_seconds):
        self.first_yield_ms = max(0, min(120000, int(first_yield_ms)))
        self.cleanup_timeout_seconds = max(.05, float(cleanup_timeout_seconds))
        self._owners = {}
        self._intent_owners = {}
        self._owner_lock = threading.RLock()
        self._cancelled_requests = set()
        self._cancelled_request_runs = {}
        self._closing_requests = set()
        self._closed = False

    @staticmethod
    def _scope_key(request_scope):
        return hashlib.sha256(_canonical(request_scope).encode()).hexdigest()

    def _request_cancel_file(self, task_id, scope_key):
        return self._task_dir(task_id) / 'request_lifecycle' / ('cancel-' + scope_key + '.json')

    def request_is_cancelled(self, task_id='', request_scope=None):
        key = self._scope_key(request_scope)
        with self._owner_lock:
            return (self._closed or (str(task_id), '*') in self._cancelled_requests
                    or (str(task_id), key) in self._cancelled_requests
                    or self._request_cancel_file(task_id, '*').exists()
                    or self._request_cancel_file(task_id, key).exists())

    def mark_request_closing(self, task_id='', request_scope=None, call_id=''):
        """Optional launch fence; the tool-loop checkpoint remains the closure authority."""
        with self._owner_lock:
            self._closing_requests.add((str(task_id), self._scope_key(request_scope)))
        return {'status': 'closing', 'call_id': call_id}

    def cancel_request(self, task_id='', request_scope=None, reason=''):
        """Fence first, signal owned workers, then attempt durable cancellation."""
        scope = '*' if request_scope is None else self._scope_key(request_scope)
        with self._owner_lock:
            # Select legitimate continued owners before installing the fence.
            matched = self._select_owners(task_id, request_scope)
            self._cancelled_requests.add((str(task_id), scope))
            self._cancelled_request_runs.setdefault((str(task_id), scope), set()).update(o.run_ref for o in matched)
            for owner in matched:
                owner.cancelled.set()
            fact = {'status': 'cancel_requested', 'task_id': str(task_id),
                    'scope_key': scope, 'reason': reason,
                    'requested_at': datetime.now(timezone.utc).isoformat(),
                    'run_refs': [o.run_ref for o in matched], 'cleanup_confirmed': False}
            try:
                path = self._request_cancel_file(task_id, scope)
                if not path.exists():
                    _once(path, fact)
                fact['receipt_ref'] = str(path)
            except OSError as exc:
                fact['persistence_error'] = type(exc).__name__ + ': ' + str(exc)
        return fact

    def _select_owners(self, task_id, request_scope):
        scope = self._scope_key(request_scope)
        return [o for o in self._owners.values() if o.task_id == str(task_id)
                and (request_scope is None or o.scope_key == scope
                     or o.run_ref in self._cancelled_request_runs.get((str(task_id), scope), set())
                     or self._continued_management(o.directory, task_id, request_scope))]

    def _orphan_snapshots(self, task_id, request_scope):
        scope = self._scope_key(request_scope)
        records = []
        for path in (self._task_dir(task_id) / 'intents').glob('*.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if request_scope is not None and record.get('scope_key') != scope:
                    continue
                ref = record.get('run_ref', '')
                if ref in self._owners or not re.fullmatch(r'inspection-[0-9a-f]{32}', ref):
                    continue
                directory = self._task_dir(task_id) / ref.removeprefix('inspection-')
                if not (directory / 'receipt.json').exists():
                    records.append({'tool': 'exec_command', 'run_ref': ref, 'terminal': False,
                                    'status': 'reconciliation_unknown', 'exit_code': None,
                                    'error': 'INSPECTION_OWNER_LOST_NO_RELAUNCH',
                                    'original_deadline_at': record.get('original_deadline_at'),
                                    'cleanup_confirmed': False, 'owner_available': False})
            except (OSError, ValueError):
                records.append({'tool': 'exec_command', 'run_ref': 'unknown', 'terminal': False,
                                'status': 'reconciliation_unknown', 'exit_code': None,
                                'error': 'INSPECTION_INTENT_UNREADABLE', 'intent_ref': str(path),
                                'cleanup_confirmed': False, 'owner_available': False})
        return records

    def active_runs(self, task_id='', request_scope=None):
        with self._owner_lock:
            return ([self._snapshot(o) for o in self._select_owners(task_id, request_scope)
                     if o.result is None or not o.result.get('terminal')]
                    + self._orphan_snapshots(task_id, request_scope))

    @staticmethod
    def _refusal(error, **facts):
        return {'tool': 'exec_command', 'observation_only': True, 'status': 'not_run',
                'terminal': True, 'exit_code': None, 'stdout': '', 'stderr': '', 'error': error, **facts}

    def _lookup(self, run_ref, task_id, request_scope, *, management=False):
        if not isinstance(run_ref, str) or not re.fullmatch(r'inspection-[0-9a-f]{32}', run_ref):
            raise ValueError('INSPECTION_RUN_REF_INVALID')
        owner = self._owners.get(run_ref)
        scope = self._scope_key(request_scope)
        if owner is not None:
            if owner.task_id != str(task_id):
                raise ValueError('INSPECTION_RUN_SCOPE_MISMATCH')
            if owner.scope_key != scope and not (management and self._continued_management(owner.directory, task_id, request_scope)):
                raise ValueError('INSPECTION_RUN_SCOPE_MISMATCH')
            return owner, None
        directory = self._task_dir(task_id) / run_ref.removeprefix('inspection-')
        path = directory / 'intent.json'
        if not path.is_file():
            raise ValueError('INSPECTION_RUN_UNKNOWN')
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('task_id') != str(task_id):
            raise ValueError('INSPECTION_RUN_SCOPE_MISMATCH')
        if record.get('scope_key') != scope and not (management and self._continued_management(directory, task_id, request_scope)):
            raise ValueError('INSPECTION_RUN_SCOPE_MISMATCH')
        receipt = directory / 'receipt.json'
        if receipt.is_file():
            result = json.loads(receipt.read_text(encoding='utf-8'))
            # Terminal replay can still drain a previously truncated cursor.
            # Monotonic time is reconstructed only for a completed projection;
            # it is never used to restart or grant time to a lost worker.
            duration = float(result.get('duration_seconds', 0))
            now = time.monotonic()
            owner = _InspectionRun(str(task_id), scope, record.get('call_id', ''),
                                   {'max_output_chars': record.get('max_output_chars', 12000)},
                                   result.get('stage', ''), run_ref, directory,
                                   now-duration, now if record.get('original_deadline_at') is not None else None, record.get('original_deadline_at'),
                                   record.get('cursor_key', ''), result=result)
            owner.metrics = {'phase': result.get('status'), 'process_alive': False,
                             'restored_terminal': True}
            self._owners[run_ref] = owner
            return owner, None
        return None, {'tool': 'exec_command', 'status': 'reconciliation_unknown',
                      'terminal': False, 'run_ref': run_ref, 'exit_code': None,
                      'error': 'INSPECTION_OWNER_LOST_NO_RELAUNCH',
                      'original_deadline_at': record.get('original_deadline_at'),
                      'intent_ref': str(path), 'cleanup_confirmed': False,
                      'owner_available': False}

    def _continued_management(self, directory, task_id, request_scope):
        """Only the current host-validated continuation can manage an earlier run."""
        from agent_core.plan_tracker import load_plan_state
        from agent_core.request_state import request_scope as current_scope
        state = load_plan_state(self.data_path, task_id)
        if ((state.get('task_state') or {}).get('status') == 'cancelled'
                or current_scope(state) != request_scope or self.request_is_cancelled(task_id, request_scope)):
            return False
        if not (directory / 'intent.json').is_file():
            return False
        record = json.loads((directory / 'intent.json').read_text(encoding='utf-8'))
        original = record.get('request_scope')
        if not isinstance(original, dict) or not isinstance(request_scope, dict):
            return False
        return (original.get('task_id') == request_scope.get('task_id') == str(task_id)
                and original.get('request_key') == request_scope.get('request_key')
                and int(request_scope.get('user_turn_revision', 0)) >= int(original.get('user_turn_revision', 0))
                and not self.request_is_cancelled(task_id, original))

    def has_intent(self, task_id='', request_scope=None, call_id=''):
        if not call_id:
            return False
        identity = hashlib.sha256(_canonical([str(task_id), self._scope_key(request_scope), str(call_id)]).encode()).hexdigest()
        return (self._task_dir(task_id) / 'intents' / (identity + '.json')).is_file()

    def reconcile_call(self, *, task_id, request_scope, call_id):
        """Observe the original owner after a transport error; never launch work."""
        identity = hashlib.sha256(_canonical([str(task_id), self._scope_key(request_scope), str(call_id)]).encode()).hexdigest()
        path = self._task_dir(task_id) / 'intents' / (identity + '.json')
        if not path.is_file(): return None
        record = json.loads(path.read_text(encoding='utf-8'))
        owner, saved = self._lookup(record['run_ref'], task_id, request_scope)
        return self._snapshot(owner) if owner is not None else saved

    async def start(self, arguments, *, task_id='', stage='', request_scope=None,
                    call_id='', yield_time_ms=None):
        """Reserve intent/owner synchronously, then wait only for the first projection."""
        started = time.monotonic()
        scope = self._scope_key(request_scope)
        call_id = str(call_id or ('direct-' + uuid.uuid4().hex))
        if not isinstance(arguments, Mapping):
            return self._refusal('Invalid exec_command arguments')
        try:
            arguments = json.loads(_canonical(dict(arguments)))
            digest = hashlib.sha256(_canonical(arguments).encode()).hexdigest()
        except (ValueError, TypeError):
            return self._refusal('Invalid exec_command arguments')
        identity = hashlib.sha256(_canonical([str(task_id), scope, call_id]).encode()).hexdigest()
        mapping = self._task_dir(task_id) / 'intents' / (identity + '.json')
        with self._owner_lock:
            # Replay is observation, so remains legal after cancellation/closing.
            if mapping.is_file():
                try:
                    record = json.loads(mapping.read_text(encoding='utf-8'))
                    if record.get('arguments_sha256') != digest:
                        return self._refusal('INSPECTION_INTENT_CONFLICT', run_ref=record.get('run_ref'))
                    owner, saved = self._lookup(record['run_ref'], task_id, request_scope)
                except (OSError, ValueError, KeyError) as exc:
                    return self._refusal('INSPECTION_INTENT_UNREADABLE', detail=str(exc))
                if owner is None:
                    return saved
            else:
                if self.request_is_cancelled(task_id, request_scope):
                    return self._refusal('REQUEST_CANCELLED')
                if (str(task_id), scope) in self._closing_requests:
                    return self._refusal('REQUEST_CLOSING')
                timeout_ms = arguments.get('timeout_ms')
                if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms <= 0):
                    return self._refusal('timeout_ms must be a positive integer or null')
                run_ref = 'inspection-' + uuid.uuid4().hex
                directory = self._task_dir(task_id) / run_ref.removeprefix('inspection-')
                deadline_at = (datetime.fromtimestamp(time.time() + timeout_ms / 1000, timezone.utc).isoformat()
                               if timeout_ms is not None else None)
                owner = _InspectionRun(str(task_id), scope, call_id, arguments, stage, run_ref,
                                       directory, started, started + timeout_ms / 1000 if timeout_ms is not None else None,
                                       deadline_at, uuid.uuid4().hex)
                owner.metrics = {'scans': [], 'scan_seconds': 0., 'directories': 0, 'entries': 0,
                                 'prepare_seconds': 0., 'command_seconds': 0., 'spawn_seconds': 0.,
                                 'deadline_monotonic': owner.deadline, 'phase': 'preparing',
                                 'cleanup_timeout_seconds': self.cleanup_timeout_seconds,
                                 'last_output_monotonic': started, 'process_alive': None}
                record = {'schema_version': 1, 'task_id': str(task_id), 'scope_key': scope,
                          'call_id': call_id, 'run_ref': run_ref, 'arguments_sha256': digest,
                          'request_scope': request_scope,
                          'original_deadline_at': deadline_at, 'hostname': socket.gethostname(),
                          'created_at': datetime.now(timezone.utc).isoformat(),
                          'status': 'launch_reserved', 'cursor_key': owner.cursor_key,
                          'max_output_chars': arguments.get('max_output_chars', 12000)}
                try:
                    directory.mkdir(parents=True, exist_ok=False)
                    _once(directory / 'intent.json', record)
                    _once(mapping, record)
                except FileExistsError:
                    # Another service won the same reservation. It owns any spawn.
                    return self._refusal('INSPECTION_INTENT_CONCURRENT_RETRY')
                except OSError as exc:
                    return self._refusal('INSPECTION_INTENT_PERSIST_FAILED', detail=str(exc))
                self._owners[run_ref] = owner
                self._intent_owners[identity] = owner
                owner.task = asyncio.create_task(self._drive_owner(owner), name=run_ref)
        wait_ms = self.first_yield_ms if yield_time_ms is None else max(0, min(120000, int(yield_time_ms)))
        await self._wait_owner(owner, wait_ms / 1000)
        return self._snapshot(owner)

    async def _wait_owner(self, owner, seconds):
        # asyncio.wait does not cancel its input if this waiter is cancelled.
        stop = time.monotonic() + max(0, seconds)
        while owner.task is not None and not owner.task.done():
            if owner.metrics.get('cleanup_unconfirmed'):
                break
            remaining = stop - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.wait({owner.task}, timeout=min(.05, remaining))

    async def _drive_owner(self, owner):
        try:
            result = await self._run_owned(owner)
            if owner.metrics.get('stop_requested'):
                result['stop_reason'] = owner.metrics.get('stop_reason', '')
                result['stop_requested'] = True
                if result.get('status') == 'cancelled':
                    result['status'] = 'stopped'
            result.update(run_ref=owner.run_ref, terminal=True,
                          original_deadline_at=owner.deadline_at,
                          cleanup_confirmed=True,
                          cleanup_scope='owned sandbox worker and captured pipes; no remote assertion')
            # Preserve the actual rc even when cancellation wins request consumption.
            _once(owner.directory / 'receipt.json', result)
            owner.result = result
            owner.metrics['cleanup_unconfirmed'] = False
            owner.metrics.pop('cleanup_reason', None)
            owner.metrics['phase'] = result['status']
            self._emit_owner(owner, result['status'], '本地检查收尾完成')
            if (owner.directory / 'cleanup_observation.json').exists():
                _once(owner.directory / 'cleanup_confirmed.json', {
                    'run_ref': owner.run_ref, 'cleanup_confirmed': True,
                    'receipt_ref': result['receipt_ref'],
                    'observed_at': datetime.now(timezone.utc).isoformat()})
        except BaseException as exc:
            # Keep the owner, including any strong worker references retained by
            # its coroutine; cancellation of this task is not thread-exit proof.
            returned = locals().get('result') or {}
            actual_rc = returned.get('exit_code', owner.metrics.get('process_exit_code'))
            cleanup_known = bool(returned.get('cleanup_confirmed'))
            if cleanup_known:
                owner.metrics['cleanup_unconfirmed'] = False
                owner.metrics.pop('cleanup_reason', None)
            owner.result = {**returned, 'tool': 'exec_command', 'status': 'reconciliation_unknown',
                            'terminal': False, 'run_ref': owner.run_ref, 'exit_code': actual_rc,
                            'stdout': returned.get('stdout', ''), 'stderr': returned.get('stderr', ''),
                            'evidence_complete': False, 'observed_result_status': returned.get('status'),
                            'error': 'INSPECTION_OWNER_RESULT_UNCONFIRMED',
                            'detail': type(exc).__name__ + ': ' + str(exc),
                            'traceback': traceback.format_exc(),
                            'cleanup_confirmed': cleanup_known, 'owner_available': True,
                            'original_deadline_at': owner.deadline_at}
            try:
                _once(owner.directory / 'owner_error.json', owner.result)
            except OSError as storage_error:
                owner.result['persistence_error'] = str(storage_error)

    def _cursor(self, owner, offsets):
        body = _canonical([owner.run_ref, offsets['stdout'], offsets['stderr']]).encode()
        signature = hmac.new(owner.cursor_key.encode(), body, hashlib.sha256).hexdigest()[:24]
        return base64.urlsafe_b64encode(body).decode().rstrip('=') + '.' + signature

    def _offsets(self, owner, cursor):
        if cursor in (None, ''):
            return {'stdout': 0, 'stderr': 0}
        try:
            encoded, signature = cursor.split('.')
            body = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
            expected = hmac.new(owner.cursor_key.encode(), body, hashlib.sha256).hexdigest()[:24]
            run_ref, stdout, stderr = json.loads(body)
            if (not hmac.compare_digest(signature, expected) or run_ref != owner.run_ref
                    or type(stdout) is not int or type(stderr) is not int or min(stdout, stderr) < 0):
                raise ValueError
            return {'stdout': stdout, 'stderr': stderr}
        except (AttributeError, ValueError, TypeError, UnicodeError):
            raise ValueError('INSPECTION_CURSOR_INVALID') from None

    def _snapshot(self, owner, cursor=None):
        offsets = self._offsets(owner, cursor)
        # Only the bounded model projection is read; the worker drains independently.
        limit = owner.arguments.get('max_output_chars', 12000)
        if type(limit) is not int or not 256 <= limit <= 64000:
            limit = 12000
        deltas, remaining, ranges = {}, {}, {}
        for name in ('stdout', 'stderr'):
            path = owner.directory / (name + '.log')
            try:
                size = path.stat().st_size
                if offsets[name] > size:
                    raise ValueError('INSPECTION_CURSOR_BEYOND_LOG')
                with path.open('rb') as handle:
                    handle.seek(offsets[name])
                    raw = handle.read(limit)
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                text = decoder.decode(raw, final=bool(owner.result and owner.result.get('terminal') and offsets[name] + len(raw) >= size))
                buffered = len(decoder.getstate()[0])
                consumed = len(raw) - buffered
                ranges[name] = [offsets[name], offsets[name] + consumed]
                offsets[name] += consumed
                deltas[name], remaining[name] = text, max(0, size - offsets[name])
            except FileNotFoundError:
                if offsets[name]:
                    raise ValueError('INSPECTION_CURSOR_LOG_MISSING')
                deltas[name], remaining[name], ranges[name] = '', 0, [0, 0]
            except OSError as exc:
                deltas[name], remaining[name], ranges[name] = '', None, [offsets[name], offsets[name]]
                owner.metrics.setdefault('snapshot_storage_errors', {})[name] = str(exc)
        now = time.monotonic()
        if owner.result and owner.result.get('terminal'):
            now = owner.started + float(owner.result.get('duration_seconds', now-owner.started))
        phase = owner.metrics.get('phase', 'preparing')
        status = owner.result.get('status') if owner.result else ('reconciliation_unknown' if owner.metrics.get('cleanup_unconfirmed') else phase)
        signature = (status, tuple(owner.metrics.get('stream_bytes_live', {}).items()), owner.cancelled.is_set(),
                     owner.metrics.get('cleanup_unconfirmed', False), bool(owner.result))
        if signature != owner.observation_signature:
            owner.sequence += 1
            owner.observation_signature = signature
        result = dict(owner.result or {})
        result.setdefault('stdout', '')
        result.setdefault('stderr', '')
        result.update(tool='exec_command', observation_only=True, run_ref=owner.run_ref,
                      terminal=bool(owner.result and owner.result.get('terminal')), status=status,
                      observation_seq=owner.sequence, next_cursor=self._cursor(owner, offsets),
                      stdout_delta=deltas['stdout'], stderr_delta=deltas['stderr'],
                      delta_byte_ranges=ranges, unread_log_bytes=remaining,
                      original_deadline_at=owner.deadline_at,
                      elapsed_seconds=round(now - owner.started, 3),
                      remaining_seconds=(round(max(0, owner.deadline - now), 3) if owner.deadline_at is not None else None),
                      output_idle_seconds=round(max(0, now - owner.metrics.get('last_output_monotonic', owner.started)), 3),
                      activity='unknown', process_alive=owner.metrics.get('process_alive'),
                      cancel_requested=owner.cancelled.is_set(), owner_available=True,
                      hostname=socket.gethostname(),
                      log_refs={n: str(owner.directory / (n + '.log')) for n in ('stdout', 'stderr')})
        if owner.metrics.get('restored_terminal'):
            result['output_idle_seconds'] = None
            result['remaining_seconds'] = 0 if owner.deadline_at is not None else None
        if owner.result is None:
            result['exit_code'] = owner.metrics.get('process_exit_code')
            result['cleanup_confirmed'] = False
        if owner.metrics.get('cleanup_unconfirmed'):
            result['error'] = 'INSPECTION_CLEANUP_UNCONFIRMED'
            result['cleanup_reason'] = owner.metrics.get('cleanup_reason')
            try:
                path = owner.directory / 'cleanup_observation.json'
                if not path.exists():
                    _once(path, result)
                result['cleanup_receipt_ref'] = str(path)
            except OSError as exc:
                result['persistence_error'] = str(exc)
        return result

    def _management_error(self, tool, error, arguments, task_id, request_scope):
        """A rejected observation/control request says nothing about process exit."""
        result = {'tool': tool, 'status': 'not_run', 'request_executed': False,
                  'terminal': False, 'run_status': 'unknown', 'run_terminal': None,
                  'error': error, 'observation_only': True}
        if isinstance(arguments, Mapping):
            result['run_ref'] = arguments.get('run_ref')
            try:
                owner, saved = self._lookup(arguments.get('run_ref'), task_id, request_scope, management=True)
                snapshot = self._snapshot(owner) if owner is not None else saved
                result.update(run_status=snapshot.get('status', 'unknown'),
                              run_terminal=snapshot.get('terminal'),
                              original_deadline_at=snapshot.get('original_deadline_at'))
            except (ValueError, OSError, KeyError):
                pass
        return result

    async def wait_command(self, arguments, *, task_id='', request_scope=None):
        if (not isinstance(arguments, Mapping) or set(arguments) - {'run_ref', 'after_cursor', 'max_wait_ms'}
                or type(arguments.get('max_wait_ms', 1000)) is not int
                or not 0 <= arguments.get('max_wait_ms', 1000) <= 60000):
            result = self._management_error('wait_command', 'INSPECTION_WAIT_ARGUMENTS_INVALID',
                                            arguments, task_id, request_scope)
            result['invalid_fields'] = sorted(set(arguments) - {'run_ref', 'after_cursor', 'max_wait_ms'}) if isinstance(arguments, Mapping) else []
            result['instruction'] = 'Copy the previous reply next_cursor unchanged into after_cursor. max_wait_ms must be an integer in [0, 60000]. Correct this wait; do not restart the command.'
            return result
        try:
            owner, saved = self._lookup(arguments.get('run_ref'), task_id, request_scope, management=True)
            if owner is None:
                return {**saved, 'tool': 'wait_command'}
            self._offsets(owner, arguments.get('after_cursor'))
            await self._wait_owner(owner, arguments.get('max_wait_ms', 1000) / 1000)
            return {**self._snapshot(owner, arguments.get('after_cursor')), 'tool': 'wait_command'}
        except (ValueError, OSError, KeyError) as exc:
            return self._management_error('wait_command', str(exc), arguments, task_id, request_scope)

    async def stop_command(self, arguments, *, task_id='', request_scope=None):
        """Signal one live owner; never kill by a supplied PID or cancel the task."""
        if (not isinstance(arguments, Mapping) or set(arguments) - {'run_ref', 'reason'}
                or not isinstance(arguments.get('reason', ''), str) or len(arguments.get('reason', '')) > 1000):
            return self._management_error('stop_command', 'INSPECTION_STOP_ARGUMENTS_INVALID', arguments, task_id, request_scope)
        try:
            from agent_core.plan_tracker import load_plan_state
            from agent_core.request_state import request_scope as current_scope
            durable = load_plan_state(self.data_path, task_id)
            if (durable.get('task_state') or {}).get('status') == 'cancelled' or self.request_is_cancelled(task_id, request_scope):
                raise ValueError('REQUEST_CANCELLED')
            if durable.get('task_id') and current_scope(durable) != request_scope:
                raise ValueError('TOOL_CONTEXT_STALE')
            owner, saved = self._lookup(arguments.get('run_ref'), task_id, request_scope, management=True)
            if owner is None:
                return {**saved, 'tool': 'stop_command', 'stop_requested': False}
            # The dispatcher validates the current provider/request identity.
            # The owner controls its Popen/group while still holding that identity;
            # an absent/reconstructed owner never authorizes PID-based cleanup.
            if not (owner.result and owner.result.get('terminal')):
                if owner.task is None or owner.task.done():
                    return {**self._snapshot(owner), 'tool': 'stop_command',
                            'stop_requested': False, 'error': 'INSPECTION_OWNER_UNAVAILABLE'}
                path = owner.directory / 'stop_request.json'
                if not path.exists():
                    _once(path, {'run_ref': owner.run_ref, 'reason': arguments.get('reason', ''),
                                 'scope_key': owner.scope_key, 'requested_at': datetime.now(timezone.utc).isoformat()})
                owner.metrics.setdefault('stop_reason', arguments.get('reason', ''))
                owner.metrics['stop_requested'] = True
                owner.cancelled.set()
                await self._wait_owner(owner, self.cleanup_timeout_seconds)
            return {**self._snapshot(owner), 'tool': 'stop_command',
                    'stop_requested': bool(owner.metrics.get('stop_requested')),
                    'task_cancelled': self.request_is_cancelled(task_id, request_scope),
                    'instruction': 'This stops only the inspection. Its partial output does not prove missing dependencies or scientific failure.'}
        except (ValueError, OSError, KeyError) as exc:
            return self._management_error('stop_command', str(exc), arguments, task_id, request_scope)

    async def drain_request(self, task_id='', request_scope=None, *, cancel=False, reason='', timeout_seconds=None):
        with self._owner_lock:
            owners = self._select_owners(task_id, request_scope)
        if cancel:
            self.cancel_request(task_id, request_scope, reason)
        duration = timeout_seconds
        if duration is None:
            duration = self.cleanup_timeout_seconds if cancel else 0
        until = time.monotonic() + max(0, duration)
        for owner in owners:
            await self._wait_owner(owner, max(0, until - time.monotonic()))
        for owner in owners:
            if (cancel and owner.result is None and owner.task is not None and not owner.task.done()
                    and time.monotonic() >= until):
                owner.metrics['cleanup_unconfirmed'] = True
                owner.metrics['cleanup_reason'] = 'request_drain_limit_reached'
        snapshots = [self._snapshot(o) for o in owners] + self._orphan_snapshots(task_id, request_scope)
        unresolved = [s['run_ref'] for s in snapshots if not s.get('terminal')]
        return {'status': 'complete' if not unresolved else 'cleanup_unconfirmed' if cancel else 'running',
                'terminal': not unresolved, 'all_terminal': not unresolved,
                'cleanup_confirmed': all(s.get('cleanup_confirmed', False) for s in snapshots),
                'unresolved': unresolved, 'runs': snapshots}

    async def shutdown(self, *, timeout_seconds=None, reason='runtime_shutdown'):
        with self._owner_lock:
            self._closed = True
            task_ids = {o.task_id for o in self._owners.values()}
        for task_id in task_ids:
            self.cancel_request(task_id, reason=reason)
        duration = self.cleanup_timeout_seconds if timeout_seconds is None else timeout_seconds
        until = time.monotonic() + max(0, duration)
        results = [await self.drain_request(t, timeout_seconds=max(0, until - time.monotonic())) for t in task_ids]
        return {'status': 'closed' if all(r['cleanup_confirmed'] for r in results) else 'cleanup_unconfirmed',
                'cleanup_confirmed': all(r['cleanup_confirmed'] for r in results), 'requests': results}

    async def run(self, arguments, *, task_id='', stage=''):
        """Compatibility: cancellation of this direct call cancels its own command."""
        call_id = 'compat-' + uuid.uuid4().hex
        scope = {'compat_call_id': call_id}
        try:
            result = await self.start(arguments, task_id=task_id, stage=stage,
                                      request_scope=scope, call_id=call_id, yield_time_ms=0)
            while not result.get('terminal') and result.get('status') != 'reconciliation_unknown':
                result = await self.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 60000},
                                                 task_id=task_id, request_scope=scope)
            return result
        except asyncio.CancelledError:
            self.cancel_request(task_id, scope, 'direct_run_waiter_cancelled')
            await self.drain_request(task_id, scope, timeout_seconds=self.cleanup_timeout_seconds)
            raise

    def _emit_owner(self, owner, phase, text):
        from agent_core.cli_display import emit
        now = time.monotonic()
        # Owned task context retains the initiating tool even after early return
        # and a later wait_command/model request. No new timer or process owner.
        from agent_core.cli_tool_activity import current, clean
        observer = current()
        if observer is not None:
            observer.event(phase, {**dict(owner.result or {}),
                'run_ref': owner.run_ref, 'elapsed_seconds': round(now-owner.started, 3),
                'output_idle_seconds': round(now-owner.metrics.get('last_output_monotonic', owner.started), 3),
                'cancel_requested': owner.cancelled.is_set(),
                'terminal': bool(owner.result and owner.result.get('terminal'))})
            from agent_core.cli_tool_activity import register_owner_logs
            register_owner_logs(owner)
        emit('activity', f'{owner.stage or "本地"} · {text} · {now-owner.started:.1f}s',
             purpose='status', task_id=owner.task_id, node=owner.stage,
             call_id=owner.run_ref, tool_call_id=owner.call_id, run_ref=owner.run_ref,
             event=phase, progress_phase=phase, status=phase, source='runtime_fact',
             activity_kind='command_inspection', command_name=clean(owner.arguments.get('cmd') or '本地检查'),
             timeline_bound=observer is not None,
             elapsed_seconds=round(now-owner.started, 3),
             output_idle_seconds=round(now-owner.metrics.get('last_output_monotonic', owner.started), 3),
             remaining_seconds=(round(max(0, owner.deadline-now), 3) if owner.deadline_at is not None else None),
             cancel_requested=owner.cancelled.is_set(), terminal=bool(owner.result and owner.result.get('terminal')),
             hostname=socket.gethostname())

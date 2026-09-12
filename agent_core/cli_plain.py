"""Ordered plain display and input, including mandatory approval with UI off.

Only display copies and one ephemeral input request live here. Approval parsing,
authorization, raw process capture and business persistence stay with callers.
"""
import asyncio
import concurrent.futures
from collections import deque
from copy import deepcopy
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time

from agent_core.cli_display import DisplayModel, DisplayTicket, safe_document, safe_text


class PlainTerminal:
    def __init__(self, output=None, *, enabled=True, verbosity='compact',
                 presentation_timeout=5.0):
        self.mode = 'plain' if enabled else 'off'
        self.model = DisplayModel(verbosity=verbosity)
        self.output = output if output is not None else sys.stdout
        self.closed = False
        self.sink_broken = False
        self.last_ticket = None
        self.history_cursor = 0
        self.last_stage = None
        self.context = ('', 'interactive_prompt', {})
        self.review = None
        self.pending = None
        self.approval_events = deque(maxlen=256)
        self.presentation_timeout = max(0.01, float(presentation_timeout))
        self._sequence = 0
        self._closed_input = threading.Event()
        self._read_ahead = b''
        self.queue = queue.Queue()
        # Off suppresses ordinary display, never the required human channel.
        self.writer = threading.Thread(target=self._write_loop,
                                       name='biomind-cli-output', daemon=True)
        self.writer.start()

    def prepare_review(self, view):
        from agent_core.human_io import HumanInputUnavailable
        with self.model.lock:
            if self.pending is not None:
                raise HumanInputUnavailable('another terminal input is pending')
            self.review = view
            self.model.details.append(safe_document(view.body))

    def set_context(self, task_id, stage, payload):
        from agent_core.human_io import HumanInputUnavailable
        value = (str(task_id), str(stage), deepcopy(dict(payload or {})))
        with self.model.lock:
            if self.pending is not None and value != self.context:
                raise HumanInputUnavailable('cannot replace a pending input scope')
            self.context = value

    def _event(self, request, phase, **extra):
        event = {
            'phase': phase, 'monotonic': time.monotonic(),
            'request_id': request['id'], 'task_id': request['context'][0],
            'stage': request['context'][1], 'review_hash': request['review_hash'],
            'action_id': request['action_id'], **extra,
        }
        self.approval_events.append(event)
        logging.getLogger(__name__).info('approval_ui %s', json.dumps(event))

    def receive(self, kind, text='', **data):
        with self.model.lock:
            ticket = self.model.receive(kind, text, **data)
            if ticket is None:
                return None
            if kind == 'activity' and data.get('purpose') == 'preflight_progress':
                # Plain has no live status area. Each actual item boundary is
                # a compact line, still fenced by any pending human approval.
                ticket.presentation = 'history'
                self.model.items.append(ticket)
                self.model.history.append(ticket.text)
            if (kind == 'stage' and ticket.source == 'runtime_fact'
                    and ticket.presentation == 'status' and ticket.text.strip()):
                stage = (ticket.scope.get('task_id', ''), ticket.scope.get('node', ''), ticket.text)
                if stage != self.last_stage:
                    self.last_stage = stage
                    ticket.presentation = 'history'
                    self.model.items.append(ticket)
                    self.model.history.append(ticket.text)
            if (kind == 'tool_tail' and data.get('display_delta')
                    and (self.model.verbosity == 'verbose' or data.get('purpose') == 'key_output')):
                ticket.text = safe_text(data['display_delta']).rstrip('\n')
                self.model.items.append(ticket)
                self.model.history.append(ticket.text)
                ticket.presentation = 'history'
            self._queue_history()
            if self.closed or self.sink_broken or self.mode == 'off':
                ticket.finish(False, 'display_closed' if self.closed else 'display_unavailable')
            return ticket

    def _queue_history(self):
        with self.model.lock:
            items = self.model.items[self.history_cursor:]
            self.history_cursor = len(self.model.items)
            for ticket in items:
                if ticket.presentation != 'history':
                    continue
                if self.closed or self.sink_broken:
                    ticket.finish(False, 'display_unavailable')
                elif self.pending is not None:
                    if ticket.kind in {'error', 'cancel'}:
                        self.queue.put_nowait(('urgent', ticket, self.pending))
                        self.last_ticket = ticket
                    else:
                        # Keep the exact display record in history/details;
                        # no stdout pipe or diagnostic logger is paused.
                        ticket.finish(False, 'pending_input_detail_only')
                elif self.mode == 'off':
                    ticket.finish(False, 'display_unavailable')
                else:
                    self.last_ticket = ticket
                    self.queue.put_nowait(('history', ticket, None))

    def finish_tools(self):
        self.model.finalize_tools()
        self._queue_history()

    def _control(self, request, text, *, phase='presentation'):
        ticket = DisplayTicket(request['id'], 'runtime_fact', 'approval', text,
                               {}, {'enqueued': time.monotonic()})
        with self.model.lock:
            self.last_ticket = ticket
            self.queue.put_nowait((phase, ticket, request))
        return ticket

    def _write_loop(self):
        while True:
            entry = self.queue.get()
            if entry is None:
                return
            kind, ticket, request = entry
            if self.sink_broken:
                ticket.finish(False, 'sink_closed')
                continue
            if request is not None:
                with self.model.lock:
                    if self.pending is not request or request['closed']:
                        ticket.finish(False, 'stale_input_request')
                        continue
                    if kind == 'presentation':
                        self._event(request, 'presentation_started')
            try:
                text = ticket.text
                if kind == 'urgent':
                    text = ('\n' + text + '\n提示已恢复；已键入内容仍保留，'
                            '当前对象未变，仍待决定。\n' + request['prompt'])
                elif kind == 'history':
                    text += '\n'
                self.output.write(text)
                ticket.times['write'] = time.monotonic()
                self.output.flush()
                ticket.times['flush'] = time.monotonic()
                if request is not None:
                    with self.model.lock:
                        if self.pending is request and not request['closed']:
                            if kind == 'presentation':
                                self._event(request, 'presentation_written')
                ticket.finish(True)
            except Exception:
                self.sink_broken = True
                ticket.finish(False, 'writer_failed')

    def _await_written(self, ticket):
        from agent_core.human_io import HumanInputUnavailable
        try:
            success = ticket.confirmation.result(timeout=self.presentation_timeout)
        except concurrent.futures.TimeoutError:
            raise HumanInputUnavailable('approval presentation did not complete') from None
        if not success:
            raise HumanInputUnavailable('approval output channel is unavailable')

    @staticmethod
    def _waiting_text(stage):
        if stage == 'code_review':
            return '等待代码审批 · 当前候选尚未执行'
        if stage == 'plan_review':
            return '等待计划审批 · 当前计划尚未批准'
        return '等待用户输入'

    def _presentation(self, request, *, details=False, notice=''):
        view = request['review']
        parts = []
        if notice:
            parts.append(safe_document(notice))
        if view is not None:
            parts.append(safe_document(view.summary).replace('/details 或 F2', '/details'))
            if details or self.model.verbosity == 'verbose':
                parts.extend([safe_text(view.title), safe_document(view.body)])
            if details:
                parts.append('详情结束；仍待审批。查看详情不构成批准。')
            else:
                parts.append('/details 查看完整对象；查看详情不构成批准。')
        parts.append(self._waiting_text(request['context'][1]))
        return '\n'.join(parts) + '\n' + request['prompt']

    def show_details(self, text=None, title=''):
        with self.model.lock:
            request = self.pending
            if request is not None:
                if (text is not None and request['review'] is not None
                        and safe_document(text) != safe_document(request['review'].body)):
                    from agent_core.human_io import HumanInputUnavailable
                    raise HumanInputUnavailable('cannot replace a pending approval detail')
                if text is None:
                    body = self._presentation(request, details=True)
                else:
                    body = (safe_document((title + '\n' if title else '') + str(text))
                            + '\n仍待审批；查看详情不构成批准。\n' + request['prompt'])
                return self._control(request, body, phase='details')
        body = text if text is not None else '\n\n'.join(self.model.details)
        return self.receive('message', (title + '\n' if title else '') + body,
                            purpose='review_view')

    def _read_line(self, timeout, cancelled=None):
        from agent_core.human_io import (HumanInputEOF, HumanInputTimeout,
                                         HumanInputDecodeError, decode_terminal_bytes)
        def check_cancelled():
            if self._closed_input.is_set() or (cancelled is not None and cancelled()):
                raise HumanInputEOF('plain input cancelled')

        check_cancelled()
        try:
            fd = sys.stdin.fileno()
        except (AttributeError, OSError, ValueError):
            fd = None  # In-memory test/file-like input has no OS wait handle.
        if fd is not None:
            import select
            deadline = time.monotonic() + timeout if timeout else None
            tty = sys.stdin.isatty()
            while True:
                check_cancelled()
                if not tty and b'\n' in self._read_ahead:
                    line, self._read_ahead = self._read_ahead.split(b'\n', 1)
                    return decode_terminal_bytes(line)
                remaining = max(0, deadline - time.monotonic()) if deadline else None
                if deadline is not None and remaining <= 0:
                    raise HumanInputTimeout('plain input timed out')
                try:
                    # This is a cancellation wake-up interval, not a human
                    # deadline; timeout=0 remains an unlimited human wait.
                    ready = select.select([fd], [], [], min(0.1, remaining)
                                          if remaining is not None else 0.1)[0]
                    if not ready:
                        continue
                    raw = os.read(fd, 65536)
                except (OSError, ValueError):
                    raise HumanInputEOF('plain input channel is unavailable') from None
                check_cancelled()
                if not raw:
                    if self._read_ahead:
                        value, self._read_ahead = self._read_ahead, b''
                        return decode_terminal_bytes(value)
                    raise HumanInputEOF('plain input closed')
                if tty:
                    # Canonical TTY read returns one line/EOF-delimited record.
                    # No TextIOWrapper prefetch can hide pasted follow-up lines.
                    return decode_terminal_bytes(raw).rstrip('\n')
                self._read_ahead += raw
        try:
            value = sys.stdin.readline()
        except UnicodeError:
            raise HumanInputDecodeError('plain input could not be decoded') from None
        except (OSError, ValueError):
            raise HumanInputEOF('plain input channel is unavailable') from None
        if value == '':
            raise HumanInputEOF('plain input closed')
        return value[:-1] if value.endswith('\n') else value

    def read(self, prompt, timeout=0, multiline=False, cancelled=None):
        from agent_core.human_io import HumanInputEOF, HumanInputTimeout, HumanInputUnavailable
        with self.model.lock:
            if self.pending is not None:
                raise HumanInputUnavailable('another terminal input is pending')
            if self.closed or self.sink_broken:
                raise HumanInputEOF('plain terminal is unavailable')
            self._sequence += 1
            context = deepcopy(self.context)
            view = self.review if context[1] in {'plan_review', 'code_review'} else None
            # A later legacy confirmation must not inherit this displayed
            # candidate merely because its caller did not set a new context.
            self.review = None
            payload = context[2]
            review_body = str(view.body) if view is not None else str(payload.get('content', ''))
            request = {'id': self._sequence, 'context': context, 'review': view,
                       'review_hash': hashlib.sha256(review_body.encode()).hexdigest(),
                       'action_id': str(payload.get('action_id') or
                                        (payload.get('provenance') or {}).get('action_id') or ''),
                       'prompt': safe_document(prompt), 'closed': False}
            # This assignment fences new ordinary display messages while the
            # already queued finite batch remains ahead of the presentation.
            self.pending = request
            self._event(request, 'request_created')
        try:
            self._await_written(self._control(request, self._presentation(request)))
            self._event(request, 'input_ready')
            deadline = time.monotonic() + timeout if timeout else None
            lines = []
            while True:
                remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0
                if deadline is not None and remaining <= 0:
                    raise HumanInputTimeout('plain input timed out')
                value = self._read_line(remaining, cancelled=cancelled)
                # Existing bounded paste collection, with no global stdin
                # clearing or newline replay into a second approval.
                from agent_core.human_io import read_pending_paste_lines
                continuation = read_pending_paste_lines(
                    grace_seconds=0.08, max_lines=2000, owned_read=True)
                if continuation:
                    value = '\n'.join([value, *continuation])
                if value == '/details' and not multiline:
                    self._await_written(self._control(
                        request, self._presentation(request, details=True), phase='details'))
                    continue
                if multiline:
                    values = value.split('\n')
                    if values[-1] != '/end':
                        lines.extend(values)
                        self._await_written(self._control(request, '... ', phase='continuation'))
                        continue
                    lines.extend(values[:-1])
                    value = '\n'.join(lines)
                self._event(request, 'decision_received')
                return value
        finally:
            with self.model.lock:
                request['closed'] = True
                if self.pending is request:
                    self.pending = None
                self.model.release_priority()

    async def drain(self, timeout=0.05):
        self.finish_tools()
        if self.last_ticket is None:
            return True
        try:
            return await asyncio.wait_for(asyncio.shield(
                asyncio.wrap_future(self.last_ticket.confirmation)), timeout)
        except asyncio.TimeoutError:
            return (self.last_ticket.confirmation.done()
                    and bool(self.last_ticket.confirmation.result()))

    def close(self):
        self.finish_tools()
        with self.model.lock:
            self.closed = True
            self._closed_input.set()
            self.queue.put_nowait(None)

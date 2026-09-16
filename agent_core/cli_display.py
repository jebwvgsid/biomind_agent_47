"""Display copies only; never parse, authorize or persist business state."""
import re
import asyncio
import concurrent.futures
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import count

_ESCAPE = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\[[0-?]*[ -/]*[@-~]|[@-_])|\x9d[^\x9c]*(?:\x9c|$)')
_SECRET = re.compile(r'''(?ix)
    (\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret)\b["']?\s*[:=]\s*)
    (?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)
''')
_BEARER = re.compile(r'(?i)(\bbearer\s+)[^\s,;"\'}]+')
_API_KEY = re.compile(r'\bsk-[A-Za-z0-9_-]{8,}\b')
_OMITTED = '输出片段已省略'


def safe_text(value):
    # Only bounded complete messages/lines enter this function. Never inspect
    # credentials or the environment to construct a secret matching table.
    if len(str(value)) > 65536:
        return _OMITTED
    text = _ESCAPE.sub('', str(value))
    text = ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159))
    text = _SECRET.sub(lambda match: match[1] + '[REDACTED]', text)
    text = _BEARER.sub(lambda match: match[1] + '[REDACTED]', text)
    return _API_KEY.sub('[REDACTED]', text)


def safe_document(value):
    """Preserve long multiline review copies; each complete line is sanitized.

    This is never returned to approval hashing, execution or a model parser.
    An oversized single line keeps the existing explicit omission behavior.
    """
    return ''.join(safe_text(line) for line in str(value).splitlines(keepends=True))

import codecs
import logging
import sys
import threading
import time
from collections import deque

_terminal = None
_last_public = ContextVar('biomind_display_public_ticket', default=None)
_sequence = count(1)
_SCOPE_FIELDS = ('task_id', 'node', 'call_id', 'provider_attempt_id', 'provider_attempt',
                 'execution_attempt_id', 'execution_attempt', 'action_id', 'pid',
                 'source_message_id', 'epoch', 'tool_request_id', 'tool_call_id', 'stream',
                 'step_id', 'seed_id', 'activity_id', 'stop_id')
_CRITICAL = {'model_public', 'message', 'error', 'result', 'cancel', 'approval',
             'tool_start', 'tool_end'}
_REQUIRED = {'model_public', 'error', 'result', 'cancel', 'approval'}
_DETAIL_PURPOSES = {'details', 'approval_detail'}
_DIAGNOSTIC_PURPOSES = {'telemetry', 'diagnostic'}


def display_verbosity(value):
    """A display preference, independent of business configuration or budgets."""
    return 'verbose' if str(value).lower() == 'verbose' else 'compact'


@dataclass
class DisplayTicket:
    item_id: object
    source: str
    kind: str
    text: str
    scope: dict
    times: dict = field(default_factory=dict)
    confirmation: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    degradation: str = ''
    purpose: str = ''
    presentation: str = 'details'
    status: str = ''

    def finish(self, success, reason=''):
        if not success:
            self.degradation = reason
            self.times.setdefault('degraded', time.monotonic())
        if not self.confirmation.done():
            self.confirmation.set_result(success)


def emit_public(text, **scope):
    """Publish only an explicitly selected, complete public model string."""
    _last_public.set(None)
    if not isinstance(text, str) or not text.strip():
        return None
    owner = _terminal
    if owner is None:
        return None
    try:
        scope.pop('source', None)
        ticket = owner.receive('model_public', text, source='model_public',
                               public_ready_at=time.monotonic(), **scope)
        if isinstance(ticket, DisplayTicket):
            _last_public.set(ticket)
            return ticket
    except Exception:
        logging.getLogger(__name__).debug('Public display unavailable')
    return None


async def flush_public(timeout=0.05):
    """Await this coroutine's last item, with no business retry or authority."""
    ticket = _last_public.get()
    _last_public.set(None)
    if ticket is None:
        return True
    deadline = time.monotonic() + max(0, timeout)
    try:
        return bool(await asyncio.wait_for(asyncio.shield(
            asyncio.wrap_future(ticket.confirmation)), max(0, timeout)))
    except asyncio.TimeoutError:
        # Completion can race the event loop's timeout callback. The actual
        # writer receipt, not callback scheduling, decides timely delivery.
        if (ticket.confirmation.done() and ticket.times.get('flush', float('inf')) <= deadline):
            return bool(ticket.confirmation.result())
        ticket.degradation = 'confirmation_timeout'
        ticket.times.setdefault('degraded', time.monotonic())
        return False
    except Exception:
        ticket.degradation = 'confirmation_unavailable'
        return False


async def publish_public(text, *, timeout=0.05, **scope):
    emit_public(text, **scope)
    return await flush_public(timeout)


def terminal():
    return _terminal


def interactive_terminal():
    """A plain/off display sink does not replace existing input/signal policy."""
    return _terminal if getattr(_terminal, 'mode', None) == 'tty' else None


def input_terminal():
    """Return the same owner that presents this terminal's human request.

    Unlike the Application-only accessor, plain/off can coordinate an actual
    approval read without becoming a TTY or changing signal/runtime policy.
    """
    return _terminal if callable(getattr(_terminal, 'read', None)) else None


def bind_terminal(value):
    global _terminal
    old, _terminal = _terminal, value
    return old


def emit(kind, text='', **data):
    """Best effort notification; renderer failures cannot enter recovery."""
    owner = _terminal
    if owner is None:
        return False
    try:
        owner.receive(kind, text, **data)
    except Exception:
        logging.getLogger(__name__).debug('Display notification failed')
    return True


def display_print(*values, sep=' ', end='\n', file=None, flush=False,
                  source='runtime_fact', purpose=''):
    # Explicitly used at legacy display call sites, never patched into builtins.
    text = sep.join(str(v) for v in values)
    if file is not None and file not in (sys.stdout, sys.stderr):
        print(*values, sep=sep, end=end, file=file, flush=flush)
    elif not emit('message', text, source=source, purpose=purpose):
        try:
            print(safe_text(text), end=end, file=file, flush=flush)
        except BrokenPipeError:
            # A display-only call cannot turn a broken pipe into model recovery.
            pass


class DisplayModel:
    def __init__(self, *, verbosity='compact'):
        self.verbosity = display_verbosity(verbosity)
        self.history = []
        self.items = []
        self.details = deque(maxlen=100)
        self.active = '等待输入'
        self.stage = ''
        self.last_activity = time.monotonic()
        self.started = self.last_activity
        self.lock = threading.RLock()
        self.seen = set()
        self.updates = 0
        self.tool_tail = ''
        self.tool_tails = {}
        self.active_scope = None
        self.known_scopes = set()
        self.fenced_scopes = set()
        self.active_execution_scope = None
        self.known_execution_scopes = set()
        self.priority = ''
        self.pending_tool_group = None
        self.tool_receipts = set()
        self.activities = {}
        self.activity_details = {}
        self.public_messages = {}
        self.terminals = {}
        self.completed_steps = {}
        self.run_state = 'idle'

    def _lifecycle(self, kind, text, data, scope):
        """Update display identities only; all original events stay in details."""
        now = time.monotonic()
        if kind == 'details' and data.get('activity_id'):
            identity = (str(data.get('task_id') or ''), str(data['activity_id']))
            self.activity_details.setdefault(identity, []).append(text)
        def ticket(label, body, identity):
            return DisplayTicket(identity, data.get('source', 'runtime_fact'), label, body,
                                 scope, {'ready': now, 'enqueued': now})
        def append(row):
            row.presentation = 'history'
            self.items.append(row)
            self.history.append(row.text)
            return row
        if kind in {'terminal_state', 'cancel', 'result'}:
            for pending in self.public_messages.values():
                item = pending.get('ticket')
                if not pending['complete'] and item is not None:
                    item.text = pending['text'] + '\n[公开消息未完整结束；以上为已接收部分]'
                    item.kind, item.presentation = 'model_public', 'history'
                    pending['complete'] = True
                    self.history[pending['history_index']] = item.text
        if kind == 'step_committed':
            identity = (data.get('task_id'), data.get('commit_id') or data.get('item_id'))
            if identity in self.completed_steps:
                return True, None
            row = ticket('message', text, identity)
            self.completed_steps[identity] = row
            if not self.priority:
                self.active = text
            return True, append(row)
        if kind == 'terminal_state':
            from .cli_lifecycle import terminal_text, delivery_text, delivery_identity
            facts = data.get('terminal_facts') or {}
            identity = (data.get('task_id'), facts.get('stop_id') or data.get('stop_id') or 'terminal')
            status = data.get('status') or 'unknown'
            self.run_state = status
            self.active = '任务已停止' if status != 'completed' else '任务正式完成'
            self.priority = 'terminal'
            self.tool_tail = ''
            self.details.append(safe_document(__import__('json').dumps(facts, ensure_ascii=False, indent=2)))
            body = safe_document(terminal_text(status, facts))
            delivery = delivery_identity(facts)
            prior = self.terminals.get(identity)
            if prior:
                prior['ticket'].text = body
                self.history[prior['history_index']] = body
                if delivery == prior['delivery']:
                    return True, None
                prior['delivery'] = delivery
                return True, append(ticket('terminal_delivery_update', '交付更新：' + delivery_text(facts), identity))
            row = ticket('result', body, identity)
            self.terminals[identity] = {'ticket': row, 'delivery': delivery, 'history_index': len(self.history)}
            return True, append(row)
        message_id = data.get('source_message_id') or data.get('item_id')
        if kind in {'model_delta', 'model_public'} and message_id and data.get('source') == 'model_public':
            identity = tuple(str(data.get(k) or '') for k in ('task_id', 'node', 'call_id', 'provider_attempt_id')) + (str(message_id),)
            row = self.public_messages.setdefault(identity, {'text': '', 'complete': False})
            self._may_activate(kind, data)
            if row['complete']:
                if kind == 'model_public' and row['text'] != text:
                    self.details.append('同一消息出现不同完成正文；原始回执保留，已完成显示不重放。\n' + text)
                return True, None
            if kind == 'model_delta':
                row['text'] += text
                if 'ticket' not in row:
                    self.finalize_tools()
                    row['ticket'] = ticket('model_delta', row['text'], identity)
                    row['ticket'].presentation = 'pending'
                    row['history_index'] = len(self.history)
                    self.items.append(row['ticket'])
                    self.history.append(row['text'])
                row['ticket'].text = row['text']
                self.history[row['history_index']] = row['text']
                return True, row['ticket']
            if row['text'] and row['text'] != text:
                self.details.append('流式预览与完成正文不同；以下最终显示采用 completed。\n' + row['text'])
            row.update(text=text, complete=True)
            self.finalize_tools()
            if 'ticket' in row:
                complete = row['ticket']
                complete.text, complete.kind, complete.presentation = text, 'model_public', 'history'
                self.history[row['history_index']] = text
                return True, complete
            return True, append(ticket('model_public', text, identity))
        activity_id = data.get('activity_id')
        if kind in {'tool_start', 'tool_tail', 'tool_end', 'activity_check'} and activity_id:
            identity = (str(data.get('task_id') or ''), str(activity_id))
            row = self.activities.get(identity)
            if row is None:
                row = {'ticket': ticket(kind, text, identity), 'started': False, 'ended': False,
                       'history_index': None, 'events': []}
                self.activities[identity] = row
            row['events'].append({'kind': kind, 'text': text, 'status': data.get('status'), 'event_id': data.get('event_id') or data.get('item_id')})
            self.details.append(text)
            if kind == 'tool_start':
                row['started'] = True
                if row['ended']:
                    return True, None  # A late/replayed start never reopens a finished activity.
            if kind == 'tool_tail':
                row['output'] = text
                stream = tuple(str(data.get(k, '')) for k in ('task_id', 'action_id', 'execution_attempt_id',
                    'execution_attempt', 'pid', 'tool_request_id', 'tool_call_id', 'stream'))
                self.tool_tails[stream] = text[-4096:]
                if len(self.tool_tails) > 32:
                    del self.tool_tails[next(iter(self.tool_tails))]
                if not row['ended'] and self._may_activate(kind, data):
                    self.tool_tail = text[-4096:]
                tail = ticket(kind, text, identity)
                tail.presentation = 'status'
                return True, tail
            entry = row['ticket']
            previous_status = entry.status
            entry.text, entry.status, entry.kind = text, str(data.get('status') or ''), kind
            if self._may_activate(kind, data):
                self.active = text
                self.last_activity = now
                if not self.priority:
                    self.run_state = 'running'
            if kind in {'tool_end', 'activity_check'}:
                row['ended'] = True
                if row['history_index'] is None:
                    row['history_index'] = len(self.history)
                    return True, append(entry)
                self.history[row['history_index']] = text
                # Append-only scrollback may already contain rc0. Surface a
                # newly failed formal check once while keeping one activity.
                if entry.status == 'failed' and previous_status != 'failed' and entry.times.get('write'):
                    update = ticket('activity_check', text, identity)
                    update.status = 'failed'
                    return True, append(update)
                return True, None
            start = ticket('tool_start', text, identity)
            start.presentation = 'status'
            self.items.append(start)  # Immutable start receipt; one mutable activity above.
            return True, start
        return False, None

    @staticmethod
    def _group_scope(data):
        return tuple(str(data.get(key, '')) for key in
                     ('task_id', 'node', 'call_id', 'epoch', 'action_id'))

    @staticmethod
    def _receipt_identity(data):
        identity = tuple(str(data.get(key, '')) for key in
                         ('tool_call_id', 'tool_request_id', 'execution_attempt_id',
                          'execution_attempt', 'pid'))
        return (DisplayModel._group_scope(data), identity) if any(identity) else None

    @staticmethod
    def _group_text(counts):
        labels = {'read': ('读取', '项'), 'search': ('搜索', '次'),
                  'process': ('运行', '个脚本'), 'other': ('工具返回', '次'),
                  'empty': ('空结果', '次')}
        text = ' · '.join(f'{labels[k][0]} {n} {labels[k][1]}' for k, n in counts.items())
        return text + (' · 进程已退出，正式核验中' if counts.get('process') else '')

    def finalize_tools(self):
        """Close an observed contiguous group, without timers or business facts."""
        with self.lock:
            group, self.pending_tool_group = self.pending_tool_group, None
            if group is None:
                return None
            text = self._group_text(group['counts'])
            ticket = DisplayTicket(next(_sequence), 'runtime_fact', 'tool_group', text,
                                   group['scope'], {'ready': group['ready'], 'enqueued': time.monotonic()},
                                   purpose='tool_fact', presentation='history')
            self.items.append(ticket)
            self.history.append(text)
            return ticket

    @staticmethod
    def _activity_scope(data):
        return tuple(str(data.get(key, '')) for key in
                     ('task_id', 'node', 'call_id', 'provider_attempt_id', 'provider_attempt'))

    def _may_activate(self, kind, data):
        scope = self._activity_scope(data)
        has_scope = any(scope)
        event = data.get('event', '')
        if event in {'attempt_fenced', 'fenced'}:
            self.fenced_scopes.add(scope)
            return False
        if scope in self.fenced_scopes:
            return False
        new_task = bool(scope[0]) and self.active_scope and bool(self.active_scope[0]) and scope[0] != self.active_scope[0]
        if self.priority in {'terminal', 'cancel'} and not new_task and kind in {'model_public', 'model_delta'}:
            return False
        if self.priority and not new_task and kind in {'stage', 'activity', 'tool_start', 'tool_end', 'tool_tail', 'activity_check'}:
            return False
        execution = tuple(str(data.get(key, '')) for key in
                          ('task_id', 'action_id', 'execution_attempt_id', 'execution_attempt',
                           'pid', 'tool_request_id', 'tool_call_id'))
        if kind in {'tool_start', 'tool_end', 'tool_tail'} and any(execution[1:]):
            if execution != self.active_execution_scope:
                if execution in self.known_execution_scopes:
                    return False
                if kind == 'tool_start' or self.active_execution_scope is None:
                    self.active_execution_scope = execution
                    self.known_execution_scopes.add(execution)
                else:
                    # An unobserved/late exit remains a receipt in history;
                    # it cannot impersonate the current execution activity.
                    return False
        if has_scope and scope != self.active_scope:
            if scope in self.known_scopes:
                return False
            # A previously unseen call is a new local scope. A provider start
            # is explicit; full public receipts also establish their call.
            self.known_scopes.add(scope)
            self.active_scope = scope
            self.priority = ''
        return True

    def receive(self, kind, text='', **data):
        with self.lock:
            self.updates += 1
            text = safe_document(text) if data.get('purpose') in _DETAIL_PURPOSES | {'review_view'} else safe_text(text)
            source = data.get('source', 'diagnostic' if kind == 'details' else 'runtime_fact')
            if source not in {'model_public', 'runtime_fact', 'diagnostic'}:
                source = 'diagnostic'
            scope = {key: data[key] for key in _SCOPE_FIELDS if data.get(key) is not None}
            identity = data.get('item_id') or data.get('key') or None
            event_identity = data.get('event_id') or identity
            key = (tuple((k, str(v)) for k, v in scope.items()), kind, str(event_identity)) if event_identity is not None else None
            if kind in {'model_delta', 'terminal_state', 'activity_check'} and not data.get('event_id'):
                key = None
            if key and key in self.seen:
                return None
            if key:
                self.seen.add(key)
            handled, lifecycle_ticket = self._lifecycle(kind, text, data, scope)
            if handled:
                return lifecycle_ticket
            if kind == 'activity' and data.get('event') == 'recovery_started' and not self.priority:
                self.run_state = 'recovering'
            if kind == 'cancel':
                self.run_state = 'cancelling'
            if kind == 'result' and data.get('status'):
                self.run_state = str(data['status'])
                self.tool_tail = ''
            receipt = self._receipt_identity(data) if kind == 'tool_end' else None
            ticket = DisplayTicket(identity if identity is not None else next(_sequence),
                                   source, kind, text, scope,
                                   {'ready': data.get('public_ready_at', time.monotonic()),
                                    'enqueued': time.monotonic()})
            ticket.purpose = str(data.get('purpose', ''))
            ticket.status = str(data.get('status', ''))
            is_diagnostic = source == 'diagnostic' or ticket.purpose in _DIAGNOSTIC_PURPOSES
            status_only = ticket.purpose == 'status'
            detail_only = (kind == 'details' or is_diagnostic or ticket.purpose in _DETAIL_PURPOSES)
            if (detail_only and kind not in _REQUIRED and not status_only
                    and data.get('event') not in {'attempt_fenced', 'fenced'}):
                # A counter/parse receipt with only a task and node is not a
                # new invocation. It must not make the actual call look stale.
                active = False
            else:
                active = self._may_activate(kind, data)
            category = data.get('tool_category', 'other')
            category = category if category in {'read', 'search', 'process'} else 'other'
            if data.get('status') in {'empty', 'no_match'}:
                category = 'empty'
            returned = (kind == 'tool_end' and receipt is not None
                        and data.get('status') in {'returned', 'success', 'empty', 'no_match'}
                        and data.get('tool_success', True) is not False
                        and not data.get('partial') and not data.get('unknown')
                        and not data.get('not_dispatched')
                        and (data.get('tool_category') != 'process' or
                             (data.get('returncode') == 0 and data.get('command_started') is not False)))
            if returned:
                # Dedupe the successful receipt only. A previous unknown or
                # failure does not swallow a later authoritative completion.
                if receipt in self.tool_receipts:
                    return None
                self.tool_receipts.add(receipt)
            grouped = (returned and bool(data.get('task_id')) and active and not self.priority and source == 'runtime_fact'
                       and not is_diagnostic and self.verbosity == 'compact')
            if self.pending_tool_group is not None:
                boundary = (kind in _REQUIRED or (kind == 'message' and not is_diagnostic)
                            or (kind == 'tool_end' and not grouped)
                            or (kind == 'tool_start' and self._group_scope(data) != self.pending_tool_group['key'])
                            or (kind == 'stage' and active)
                            or data.get('event') in {'attempt_started', 'route_changed'})
                if grouped and self._group_scope(data) != self.pending_tool_group['key']:
                    boundary = True
                if boundary:
                    self.finalize_tools()
            if (kind in {'stage', 'activity', 'tool_start', 'tool_end'} or status_only) and active and (not is_diagnostic or status_only):
                if kind == 'stage':
                    self.stage = text
                    self.started = time.monotonic()
                self.active = text
                self.last_activity = time.monotonic()
                ticket.presentation = 'status'
            if kind == 'tool_tail':
                group = tuple(str(data.get(k, '')) for k in ('task_id', 'action_id', 'execution_attempt_id',
                              'execution_attempt', 'pid', 'tool_request_id', 'tool_call_id', 'stream'))
                self.tool_tails[group] = text[-4096:]
                if len(self.tool_tails) > 32:
                    del self.tool_tails[next(iter(self.tool_tails))]
                if active:
                    self.tool_tail = text[-4096:]
                    self.last_activity = time.monotonic()
                ticket.presentation = 'status'
            elif kind in _REQUIRED and ticket.purpose not in _DETAIL_PURPOSES:
                # Severity and explicit public provenance win over a mistaken
                # telemetry tag. Ordinary diagnostic messages never become AI.
                self.details.append(text[:65536])
                self.history.append(text)
                self.items.append(ticket)
                ticket.presentation = 'history'
                if kind in {'result', 'cancel', 'approval'} and active:
                    self.priority = kind if kind in {'cancel', 'approval'} else ''
                    self.active = text.splitlines()[0] if text else kind
                    self.last_activity = time.monotonic()
            elif ticket.purpose in _DETAIL_PURPOSES:
                self.details.append(text)
            elif kind == 'details' or is_diagnostic:
                self.details.append(text[:65536])
                if self.verbosity == 'verbose' and not status_only:
                    self.history.append(text)
                    self.items.append(ticket)
                    ticket.presentation = 'history'
            elif kind in _CRITICAL:
                self.details.append(text[:65536])
                self.history.append(text)
                self.items.append(ticket)
                ticket.presentation = 'status' if status_only else 'history'
                if kind == 'tool_start' and self.verbosity == 'compact':
                    ticket.presentation = 'status'
                elif grouped:
                    ticket.presentation = 'status'
                    if self.pending_tool_group is None:
                        self.pending_tool_group = {'key': self._group_scope(data), 'scope': scope,
                                                   'counts': {}, 'ready': ticket.times['ready']}
                    counts = self.pending_tool_group['counts']
                    counts[category] = counts.get(category, 0) + 1
                    self.active = self._group_text(counts)
                if kind in {'result', 'cancel', 'approval'} and active:
                    self.priority = kind if kind in {'cancel', 'approval'} else ''
                    self.active = text.splitlines()[0] if text else kind
                    self.last_activity = time.monotonic()
            elif kind not in {'stage', 'activity', 'tool_tail'}:
                self.details.append(f'{kind}: {text[:4096]}')
            return ticket

    def release_priority(self):
        with self.lock:
            self.priority = ''

    def details_text(self):
        """Complete activity copies remain available after telemetry rotates."""
        with self.lock:
            rows = list(self.details)
            for identity, activity in self.activities.items():
                rows.append('活动 ' + str(identity) + '\n' + '\n'.join(
                    event['kind'] + ': ' + event['text'] for event in activity['events']))
            for identity, details in self.activity_details.items():
                rows.append('活动详情 ' + str(identity) + '\n' + '\n'.join(details))
            return '\n\n'.join(rows)

    def status(self):
        with self.lock:
            if self.verbosity == 'compact':
                return self.active
            return f'{self.stage} · {self.active} · 距上次观测 {int(time.monotonic()-self.last_activity)} 秒'


class StreamDisplayCopy:
    """Incremental display decoder; raw bytes belong exclusively to the executor.

    Pending partial lines are bounded. Machine-marker lines are hidden from the
    display; oversized lines are represented by a neutral activity notice.
    """
    def __init__(self, name, **scope):
        self.name = name
        self.scope = scope
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''
        self.tail = ''
        self.last_emitted = ''
        self.discard_line = False

    def feed(self, chunk, final=False):
        new_lines = []
        decoded = self.decoder.decode(chunk, final)
        fragments = decoded.splitlines(keepends=True)
        if final and not fragments:
            fragments = ['']
        for fragment in fragments:
            ended = fragment.endswith(('\n', '\r'))
            if not self.discard_line:
                self.pending += fragment
                if len(self.pending) > 4096:
                    self.pending = ''
                    self.discard_line = True
            if ended or final:
                text = self.pending
                from execution.preflight_progress import parse_progress, progress_text
                preflight = parse_progress(text) if self.name == 'stderr' else None
                if preflight is not None:
                    emit('activity', progress_text(preflight), purpose='preflight_progress', **self.scope)
                # Only display filtering; never returned to a parser or writer.
                if not self.discard_line and not any(marker in text.lower() for marker in ('biomind_', '<observation', '<effect', '<validation_result')):
                    shown = safe_text(text)
                    self.tail = (self.tail + shown)[-4096:]
                    new_lines.append(shown)
                elif self.discard_line:
                    self.tail = (self.tail + _OMITTED + '\n')[-4096:]
                    new_lines.append(_OMITTED + '\n')
                self.pending = ''
                self.discard_line = False
        if self.tail and self.tail != self.last_emitted:
            emit('tool_tail', self.tail, **{**self.scope, 'stream': self.name, 'display_delta': ''.join(new_lines)})
            self.last_emitted = self.tail

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
                 'source_message_id', 'epoch', 'tool_request_id', 'tool_call_id', 'stream')
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
        if self.priority and not new_task and kind in {'stage', 'activity', 'tool_start', 'tool_end', 'tool_tail'}:
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
            key = (tuple((k, str(v)) for k, v in scope.items()), str(identity)) if identity is not None else None
            if key and key in self.seen:
                return None
            if key:
                self.seen.add(key)
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

"""Display copies only; never parse, authorize or persist business state."""
import re

_ESCAPE = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])')


def safe_text(value):
    text = _ESCAPE.sub('', str(value))
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159))

import codecs
import logging
import sys
import threading
import time
from collections import deque

_terminal = None


def terminal():
    return _terminal


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
        logging.getLogger(__name__).debug('Display notification failed', exc_info=True)
    return True


def display_print(*values, sep=' ', end='\n', file=None, flush=False):
    # Explicitly used at legacy display call sites, never patched into builtins.
    text = sep.join(str(v) for v in values)
    if file is not None and file not in (sys.stdout, sys.stderr):
        print(*values, sep=sep, end=end, file=file, flush=flush)
    elif not emit('message', text):
        print(safe_text(text), end=end, file=file, flush=flush)


class DisplayModel:
    def __init__(self):
        self.history = []
        self.details = deque(maxlen=100)
        self.active = '等待输入'
        self.stage = ''
        self.last_activity = time.monotonic()
        self.started = self.last_activity
        self.lock = threading.RLock()
        self.seen = set()
        self.updates = 0
        self.tool_tail = ''

    def receive(self, kind, text='', **data):
        with self.lock:
            self.updates += 1
            text = safe_text(text)
            key = data.get('key')
            if key and key in self.seen:
                return
            if key:
                self.seen.add(key)
            if kind in {'stage', 'activity', 'tool_start', 'tool_end'}:
                if kind == 'stage':
                    self.stage = text
                    self.started = time.monotonic()
                self.active = text
                self.last_activity = time.monotonic()
            elif kind == 'tool_tail':
                self.tool_tail = text[-4096:]
                self.last_activity = time.monotonic()
            elif kind == 'details':
                self.details.append(text[:65536])
            elif kind in {'message', 'error', 'result', 'cancel', 'approval'}:
                self.details.append(text[:65536])
                lines = text.splitlines()
                shown = '\n'.join(lines[:8])[:1500]
                if shown != text:
                    shown += '\n… /details 查看显示详情；原文见原日志/批准对象'
                self.history.append(shown)
                if kind in {'result', 'cancel', 'approval'}:
                    self.active = shown.splitlines()[0] if shown else kind
                    self.last_activity = time.monotonic()
            else:
                self.details.append(f'{kind}: {text[:4096]}')

    def status(self):
        with self.lock:
            return f'{self.stage} · {self.active} · 距上次观测 {int(time.monotonic()-self.last_activity)} 秒'


class StreamDisplayCopy:
    """Incremental display decoder; raw bytes belong exclusively to the executor.

    Pending partial lines are bounded. Machine-marker lines are hidden from the
    display; oversized lines are represented by a neutral activity notice.
    """
    def __init__(self, name):
        self.name = name
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''
        self.tail = ''
        self.discard_line = False

    def feed(self, chunk, final=False):
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
                    self.tail = (self.tail + safe_text(text))[-4096:]
                self.pending = ''
                self.discard_line = False
        emit('tool_tail', self.tail, stream=self.name)

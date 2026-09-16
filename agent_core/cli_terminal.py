"""Python terminal owner. The business coroutine keeps its own loop in one thread.

No state loaders, parser calls, authorization or retry decisions live here.
The worker isolates existing blocking runtime code from terminal key processing.
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import re
import sys
import threading
import time
import hashlib
from collections import deque

from agent_core.cli_display import DisplayModel, bind_terminal, safe_text, safe_document, display_verbosity
from agent_core.cli_logging import set_notice_sink
from agent_core.cli_plain import PlainTerminal


def plain_input(prompt, timeout=0):
    """Canonical plain input without readline's ANSI/bracketed-paste modes."""
    from agent_core.human_io import HumanInputEOF, HumanInputTimeout
    import select
    try:
        sys.stdout.write(safe_text(prompt))
        sys.stdout.flush()
    except BrokenPipeError:
        raise HumanInputEOF('plain output closed') from None
    if timeout and not select.select([sys.stdin], [], [], timeout)[0]:
        raise HumanInputTimeout('plain input timed out')
    value = sys.stdin.readline()
    if value == '':
        raise HumanInputEOF('plain input closed')
    return value[:-1] if value.endswith('\n') else value




class _PlainInputHandoff(Exception):
    """Internal worker handoff, never an approval result."""


class Terminal:
    def __init__(self, *, verbosity='compact'):
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.layout import Layout, HSplit, Window, ConditionalContainer
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.widgets import TextArea
        self.loop = asyncio.get_running_loop()
        self.mode = 'tty'
        self.model = DisplayModel(verbosity=verbosity)
        self.pending = None
        self.input_ready = False
        self.presentation = None
        self.presentation_timeout = 5.0
        self.approval_events = deque(maxlen=256)
        self._request_serial = 0
        self.plain_owner = None
        self.writer_unavailable = False
        self.context = ('', 'interactive_prompt', {})
        self.draft = ''
        self.prompt_text = ''
        self.paste_mode = False
        self._blank_paste_request = None
        self.modal = False
        self.modal_history_cursor = 0
        self.review_document = ''
        self.review_title = ''
        self.review_prompt = ''
        self.review_kind = ''
        self.review_hash = ''
        self.cancel_requested = False
        self.cancel_callback = None
        self.force_callback = None
        self.frames = 0
        self.history_cursor = 0
        self.history_flush = None
        self.closed = False
        self.sink_broken = False
        self._notify_lock = threading.Lock()
        self._notify_pending = False
        self.fallback = False
        self.buffer = Buffer(multiline=True, completer=WordCompleter(['/help', '/status', '/details', '/logs', '/paste', '/exit', '/stop']), complete_while_typing=False)
        self.input_control = BufferControl(buffer=self.buffer)
        self.details_view = TextArea(read_only=True, scrollbar=True, wrap_lines=True,
                                     height=Dimension(min=3, preferred=12, max=24))
        keys = KeyBindings()

        @keys.add(Keys.BracketedPaste)
        def paste(event):
            data = event.data.replace('\r\n', '\n').replace('\r', '\n')
            if not data.strip() and self.pending is not None:
                self._blank_paste_request = self.pending
            event.current_buffer.insert_text(data)

        @keys.add('enter')
        def submit(event):
            self.submit()

        @keys.add('c-j')
        def newline(event):
            self.buffer.insert_text('\n')

        @keys.add('c-c')
        def cancel(event):
            self.request_cancel()

        @keys.add('c-d')
        def leave(event):
            if self.cancel_requested:
                self.receive('cancel', '强制离开：provider、子进程清理及状态保存仍可能未确认。')
                self.app.exit(result='force')
            elif self.pending is not None and self.context[1] == 'interactive_prompt':
                self._resolve('/exit')
            elif self.pending is not None:
                from agent_core.human_io import HumanInputEOF
                self._fail_read(HumanInputEOF('approval input closed'))

        @keys.add('escape')
        def close_details(event):
            self.close_details()

        @keys.add('f2')
        def details(event):
            self.show_details()

        @keys.add('c-o')
        def transcript(event):
            self.show_history()

        @keys.add('pageup', filter=Condition(lambda: not self.modal and not self._plan_body_visible()))
        def browse(event):
            self.show_history()

        @keys.add('pageup', filter=Condition(self._plan_body_visible))
        def previous_plan_page(event):
            self.details_view.buffer.cursor_up(count=max(1, min(12, self.app.output.get_size().rows - 8)))
            self.app.invalidate()

        @keys.add('pagedown', filter=Condition(self._plan_body_visible))
        def next_plan_page(event):
            self.details_view.buffer.cursor_down(count=max(1, min(12, self.app.output.get_size().rows - 8)))
            self.app.invalidate()

        layout = HSplit([
            ConditionalContainer(self.details_view, Condition(lambda: self.modal or self._plan_body_visible())),
            Window(FormattedTextControl(self.status_fragments), height=1, wrap_lines=False),
            ConditionalContainer(Window(FormattedTextControl(lambda: '\n'.join(self.model.tool_tail.splitlines()[-2:])), height=2, wrap_lines=True), Condition(lambda: self.model.verbosity == 'verbose' and bool(self.model.tool_tail))),
            ConditionalContainer(Window(FormattedTextControl(self.prompt_fragments), height=Dimension(min=1, max=3), wrap_lines=True), Condition(lambda: bool(self.prompt_text) and not self.modal)),
            ConditionalContainer(Window(self.input_control, height=Dimension(min=1, max=6), wrap_lines=True), Condition(lambda: not self.modal)),
            Window(FormattedTextControl(self.input_hint), height=1, wrap_lines=False),
        ])
        self.app = Application(layout=Layout(layout, self.input_control), key_bindings=keys,
                               full_screen=False, min_redraw_interval=0.1, erase_when_done=True,
                               mouse_support=True,
                               after_render=lambda app: self._rendered())

    @staticmethod
    def fit_columns(text, width):
        from prompt_toolkit.utils import get_cwidth
        text = safe_text(text).split('\n', 1)[0]
        if get_cwidth(text) <= max(0, width):
            return text
        budget = max(0, width - 1)
        result, used = [], 0
        for char in text:
            columns = max(0, get_cwidth(char))
            if used + columns > budget:
                break
            result.append(char)
            used += columns
        return ''.join(result) + ('…' if width > 0 else '')

    def status_line(self):
        text = self.model.status()
        if self.pending is not None and self.context[1] in {'plan_review', 'code_review'}:
            label = '代码' if self.context[1] == 'code_review' else '计划'
            text = f'等待{label}审批 · 当前候选尚未执行'
        if self.modal:
            count = sum(item.presentation == 'history' for item in self.model.items[self.modal_history_cursor:])
            text = ('仍待审批' if self.pending is not None else '浏览中') + (f' · {count} 条新消息' if count else '') + ' · Esc 返回；查看不代表批准'
        return self.fit_columns(text, self.app.output.get_size().columns)

    def input_hint(self):
        text = ('↑↓ / PgUp PgDn 浏览 · Esc 返回；Enter 不批准' if self.modal else
                'Enter 提交 · Ctrl+J 换行 · F2 详情 · Ctrl+O 历史 · Ctrl+C 取消')
        if not self.modal and self.pending is not None and self.context[1] in {'plan_review', 'code_review'}:
            text = 'Enter/y 批准 · n 拒绝 · q 退出 · F2 详情'
            if self.context[2].get('output_conflicts'):
                text = 'backup 备份后替换 · n 拒绝 · q 退出 · F2 详情；Enter 不批准'
            if self._plan_body_visible():
                text = 'PgUp/PgDn 计划正文 · ' + text
        return self.fit_columns(text, self.app.output.get_size().columns)

    def _approval_style(self):
        return ('ansiyellow bold' if self.pending is not None
                and self.context[1] in {'plan_review', 'code_review'} else '')

    def status_fragments(self):
        return [(self._approval_style(), self.status_line())]

    def prompt_fragments(self):
        return [(self._approval_style(), self.prompt_text)]

    def _rendered(self):
        self.frames += 1
        request = self.presentation
        if (request is not None and self.pending is request['future']
                and request.get('batch_complete') and not self.input_ready
                and not self.app._running_in_terminal and not self.fallback):
            self._approval_event('frame_rendered', request)
            self.input_ready = True
            request['ready'].set()
            self._approval_event('input_ready', request)

    def _approval_event(self, phase, request):
        event = {'phase': phase, 'monotonic': time.monotonic(),
                 **{key: request[key] for key in ('request_id', 'task_id', 'stage', 'review_hash', 'action_id')}}
        self.approval_events.append(event)
        logging.getLogger(__name__).info('approval_ui %s', json.dumps(event, ensure_ascii=False))

    def prepare_review(self, view):
        # Display copy only. Approval hashing/parsing continues to use the
        # original backend payload, never this sanitized document.
        if self.pending is not None and not self.pending.done():
            from agent_core.human_io import HumanInputUnavailable
            raise HumanInputUnavailable('cannot replace a pending approval view')
        if self.plain_owner is not None:
            self.plain_owner.prepare_review(view)
        self.review_document = safe_document(view.body)
        self.review_display = view
        self.review_title = safe_text(view.title)
        self.review_prompt = ''
        self.review_kind = view.kind
        if view.kind == 'plan':
            self.details_view.text = self.review_document
            self.details_view.buffer.cursor_position = 0
        self.review_hash = hashlib.sha256(view.body.encode('utf-8')).hexdigest()
        if self.plain_owner is None:
            self.receive('approval', view.summary, source='runtime_fact')

    def _plan_body_visible(self):
        return (not self.modal and self.review_kind == 'plan'
                and self.context[1] == 'plan_review' and self.pending is not None)

    @staticmethod
    def item_attributes(item):
        from prompt_toolkit.styles import DEFAULT_ATTRS
        if item.kind == 'approval':
            return DEFAULT_ATTRS._replace(color='ansiyellow', bold=True)
        if item.kind in {'tool_start', 'tool_end', 'tool_group'}:
            return DEFAULT_ATTRS._replace(color='ansicyan')
        if item.source == 'diagnostic':
            return DEFAULT_ATTRS._replace(dim=True)
        return DEFAULT_ATTRS

    @classmethod
    def item_fragments(cls, item):
        """Style display copies only; receipt status and original text stay intact."""
        from prompt_toolkit.styles import DEFAULT_ATTRS
        normal = DEFAULT_ATTRS
        marker = None
        if item.kind == 'error' or (item.kind in {'tool_end', 'activity_check'} and item.status == 'failed'):
            marker = ('× ', normal._replace(color='ansired', bold=True))
        elif item.kind == 'cancel' or (item.kind == 'tool_end' and item.status in {'cancelled', 'partial', 'unknown', 'not_dispatched'}):
            marker = ('! ', normal._replace(color='ansiyellow', bold=True))
        elif item.kind == 'tool_end' and item.status == 'returned':
            # Tool return, not Plan completion; the unchanged body retains that distinction.
            marker = ('✓ ', normal._replace(color='ansigreen', bold=True))
        fragments = [marker] if marker else []
        attrs = normal if item.kind in {'error', 'cancel'} or (marker and item.status != 'returned') else cls.item_attributes(item)
        if item.kind in {'tool_start', 'tool_end', 'tool_group'}:
            # Paths are dimmed only in the display copy. Never parse them as bindings.
            parts = re.split(r'((?<!\S)(?:/|\.{1,2}/)\S+)', item.text + '\n')
            fragments.extend((part, normal._replace(dim=True) if index % 2 else attrs)
                             for index, part in enumerate(parts) if part)
        else:
            fragments.append((item.text + '\n', attrs))
        if item.kind == 'model_public':
            fragments.append(('\n', normal))
        return fragments

    def _write_item(self, output, item):
        if self.fallback:
            output.write(item.text + '\n')
            return
        # ANSI palette entries follow the host theme. The default foreground
        # remains untouched for public paragraphs, and each item resets itself.
        try:
            for text, attributes in self.item_fragments(item):
                output.set_attributes(attributes, self.app.color_depth)
                output.write(text)
        finally:
            output.reset_attributes()

    def receive(self, kind, text='', **data):
        if self.plain_owner is not None:
            return self.plain_owner.receive(kind, text, **data)
        ticket = self.model.receive(kind, text, **data)
        if kind in {'terminal_state', 'result', 'cancel'} and self.pending is None:
            self.prompt_text = ('正在取消；等待清理与状态回执' if kind == 'cancel' else
                                '任务已完成；等待输入接口' if data.get('status') == 'completed' else
                                '任务已停止；等待输入接口')
        if ticket is None:
            return None
        if self.closed or self.sink_broken:
            ticket.finish(False, 'display_closed')
            return ticket
        with self._notify_lock:
            if not self._notify_pending:
                self._notify_pending = True
                self.loop.call_soon_threadsafe(self._changed)
        return ticket

    def _changed(self):
        with self._notify_lock:
            self._notify_pending = False
        if self.closed or self.sink_broken or self.plain_owner is not None:
            return
        if self.modal and not self.fallback:
            self.app.invalidate()
            return
        if (len(self.model.items) > self.history_cursor and self.history_flush is None
                and self.model.items[self.history_cursor].presentation != 'pending'):
            self.history_flush = self.loop.create_task(self._flush_history())
            return
        if not self.fallback:
            self.app.invalidate()

    async def _flush_history(self, until=None):
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.application.current import set_app
        if self.plain_owner is not None:
            return
        try:
            if self.modal and not self.fallback:
                return
            # A key paragraph is queued immediately, without the frame timer.
            with self.model.lock:
                first_index = self.history_cursor
                end = len(self.model.items) if until is None else min(until, len(self.model.items))
                end = next((i for i in range(self.history_cursor, end)
                            if self.model.items[i].presentation == 'pending'), end)
                items = self.model.items[self.history_cursor:end]
                self.history_cursor = end
            if items:
                def append():
                    output = sys.stdout if self.fallback else self.app.output
                    for index, item in enumerate(items):
                        if self.modal and not self.fallback:
                            # A viewer can open while an earlier write is in
                            # flight. Stop before the next item; an already
                            # started OS write itself cannot be recalled.
                            with self.model.lock:
                                self.history_cursor = min(self.history_cursor, first_index + index)
                            return
                        if item.presentation != 'history':
                            continue
                        request = self.presentation
                        if (request is not None and self.pending is request['future']
                                and first_index + index >= request['boundary']
                                and item.kind not in {'error', 'cancel'}):
                            # Keep the full classified history/diagnostic copy,
                            # while the current approval owns the input area.
                            item.finish(False, 'held_during_approval')
                            continue
                        if self.sink_broken:
                            item.finish(False, 'sink_closed')
                            continue
                        try:
                            self._write_item(output, item)
                            item.times['write'] = time.monotonic()
                            output.flush()
                            item.times['flush'] = time.monotonic()
                            item.finish(True)
                        except Exception:
                            self.sink_broken = True
                            item.finish(False, 'writer_failed')
                if self.fallback:
                    await asyncio.to_thread(append)
                else:
                    # Calls arrive from the runtime thread's ContextVars, which
                    # do not own prompt_toolkit's Application context.
                    with set_app(self.app):
                        await run_in_terminal(append, in_executor=True)
        except Exception:
            for item in locals().get('items', []):
                item.finish(False, 'renderer_unavailable')
        finally:
            self.history_flush = None
            if not self.closed and not self.sink_broken and not self.modal and len(self.model.items) > self.history_cursor:
                self._changed()

    def finish_tools(self):
        self.model.finalize_tools()
        self.loop.call_soon_threadsafe(self._changed)

    def set_context(self, task_id, stage, payload):
        from copy import deepcopy
        from agent_core.human_io import HumanInputUnavailable
        if self.pending is not None and not self.pending.done():
            raise HumanInputUnavailable('another terminal input request is already pending')
        self.context = (str(task_id), str(stage), deepcopy(dict(payload or {})))
        if self.plain_owner is not None:
            self.plain_owner.set_context(task_id, stage, payload)

    def read(self, prompt, timeout=0, multiline=False):
        from agent_core.human_io import HumanInputEOF, HumanInputTimeout, _readline_input_with_timeout
        if self.fallback:
            if self.plain_owner is None:
                raise HumanInputEOF('plain input handoff is not ready')
            return self.plain_owner.read(prompt, timeout=timeout, multiline=multiline)
        future = concurrent.futures.Future()
        def open_prompt():
            from agent_core.human_io import HumanInputUnavailable
            if future.done():
                return
            # Application can detach after the worker's initial mode check
            # but before this callback runs. Do not orphan a new TTY Future.
            if self.fallback:
                future.set_exception(_PlainInputHandoff())
                return
            if self.closed or self.pending is not None:
                future.set_exception(HumanInputUnavailable('terminal closed or another input request is pending'))
                return
            self.pending = future
            self.input_ready = False
            self._request_serial += 1
            payload = self.context[2]
            provenance = payload.get('provenance') or {}
            self.presentation = request = {
                'future': future, 'ready': asyncio.Event(), 'request_id': self._request_serial,
                'task_id': self.context[0], 'stage': self.context[1],
                'review_hash': getattr(self, 'review_hash', ''),
                'action_id': str(payload.get('action_id') or provenance.get('action_id') or ''),
                'boundary': len(self.model.items), 'batch_complete': False,
            }
            self._approval_event('request_created', request)
            self.paste_mode = multiline
            self.prompt_text = safe_text(prompt)
            if self.context[1] != 'interactive_prompt':
                self.review_prompt = safe_document(prompt)
                self.model.details.append('当前输入提示：\n' + self.review_prompt)
                if self.modal and self.review_prompt not in self.details_view.text:
                    cursor = self.details_view.buffer.cursor_position
                    self.details_view.text += '\n\n当前输入提示：\n' + self.review_prompt
                    self.details_view.buffer.cursor_position = cursor
                self.draft = self.buffer.text
                self.buffer.reset()
                self.receive('activity', '等待用户决定 · F2 查看完整对象', purpose='status')
                with self.model.lock:
                    self.model.priority = 'approval'
                self.model.details.append(safe_document(json.dumps(self.context[2], ensure_ascii=False, indent=2)))
            else:
                self.cancel_requested = False
                self.model.release_priority()
                self.model.receive('activity', '等待输入；最近结果与取消确认见历史')
                self.buffer.text = self.draft or self.buffer.text
                self.draft = ''
            self.loop.create_task(self._present_input(request))
        self.loop.call_soon_threadsafe(open_prompt)
        try:
            return future.result(timeout=timeout or None)
        except _PlainInputHandoff:
            from agent_core.human_io import HumanInputUnavailable
            if self.plain_owner is None:
                raise HumanInputUnavailable('plain input handoff is unavailable') from None
            return self.plain_owner.read(prompt, timeout=timeout, multiline=multiline)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise HumanInputTimeout('terminal input timed out')
        finally:
            def cleanup():
                if self.pending is future:
                    self.pending = None
                    self.input_ready = False
                    self.presentation = None
            self.loop.call_soon_threadsafe(cleanup)

    async def _present_input(self, request):
        from agent_core.human_io import HumanInputUnavailable
        self._approval_event('presentation_started', request)
        async def present():
            previous = self.history_flush
            if previous is not None:
                await asyncio.shield(previous)
            if self.history_cursor < request['boundary']:
                await self._flush_history(until=request['boundary'])
            if self.pending is not request['future']:
                return
            if self.sink_broken:
                raise HumanInputUnavailable('approval output channel is unavailable')
            request['batch_complete'] = True
            self.app.invalidate()
            await request['ready'].wait()
        try:
            await asyncio.wait_for(present(), timeout=self.presentation_timeout)
        except (Exception, asyncio.CancelledError) as exc:
            if self.fallback:
                return
            if self.pending is not request['future'] or request['future'].done():
                return
            if self.app.is_running:
                # The original Future survives Application teardown. The
                # existing entry wrapper hands it to one plain owner next.
                self.app.exit(result='plain')
            else:
                self._fail_read(HumanInputUnavailable('approval presentation unavailable: ' + type(exc).__name__),
                                expected=request['future'])

    def _fail_read(self, exc, expected=None):
        future = self.pending
        if future is None or (expected is not None and future is not expected):
            return
        self.pending = None
        self.input_ready = False
        self.presentation = None
        self.model.release_priority()
        if not future.done():
            future.set_exception(exc)
        self.app.invalidate()

    def _resolve(self, text, expected=None, *, cancelling=False):
        future = self.pending
        if (future is not None and not future.done()
                and (expected is None or future is expected)
                and (self.input_ready or cancelling)):
            request = self.presentation
            self.pending = None
            self.input_ready = False
            self.presentation = None
            if request is not None:
                self._approval_event('decision_received', request)
            self.model.release_priority()
            if self.context[1] == 'interactive_prompt':
                self.buffer.append_to_history()
            self.buffer.reset()
            # Display prediction only; return the unchanged input to the
            # existing command classifier, which retains all authority.
            command = str(text).replace('\r\n', '\n').replace('\r', '\n').strip().lower()
            if command in {'/exit', '/quit', 'exit', 'quit', 'q'}:
                self.model.run_state = 'exiting'
                self.model.active = '正在退出'
                self.prompt_text = '正在退出；等待清理回执'
            elif cancelling:
                self.model.run_state = 'cancelling'
                self.prompt_text = '正在取消；等待清理与状态回执'
            else:
                self.model.run_state = 'running'
                self.prompt_text = '运行中可编辑本地草稿；尚未提交'
            future.set_result(text)

    def submit(self):
        if self.modal:
            return
        value = self.buffer.text
        if (self.context[1] in {'plan_review', 'code_review'} and self.pending is not None
                and self._blank_paste_request is self.pending and not value.strip()):
            self._blank_paste_request = None
            self.model.history.append('未提交的空白粘贴草稿：' + repr(value))
            self.buffer.reset()
            self.prompt_text = self.prompt_text.split('\n空白粘贴', 1)[0] + '\n空白粘贴未作为批准；请另行提交决定。'
            self.app.invalidate()
            return
        if value == '/details'  and not self.paste_mode:
            # This command is UI-only even inside approval. Keep the exact
            # pending Future, context and local draft; no backend read cycle.
            self.buffer.reset()
            self.show_details()
            return
        # UI commands only on single-line top-level input, never inside a paste
        # or approval response. No command-like content in multiline text runs.
        if self.context[1] == 'interactive_prompt' and '\n' not in value and not self.paste_mode:
            if value in {'/status', '/details', '/logs', '/help'}:
                if value == '/status':
                    self.receive('message', self.model.status())
                elif value == '/details':
                    self.show_details()
                elif value == '/logs':
                    paths = [h.baseFilename for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
                    self.receive('message', '诊断日志：\n' + '\n'.join(paths or ['未配置文件日志']))
                    # Raw diagnostics may contain unclassified request or
                    # reasoning objects. Secret masking cannot turn them into
                    # public content; only already classified copies appear.
                    self.show_details()
                else:
                    self.receive('message', '/status 状态 · /details 显示详情 · /logs 日志位置\nCtrl+J 换行，bracketed paste 整段接收；/paste 后以 /end 结束。\n运行中草稿不自动提交；F2 详情，Esc 返回。\nCtrl+C 请求取消；取消中再次 Ctrl+C 提示，Ctrl+D 强制离开且清理未确认。\nBIOMIND_UI=plain 禁用动态界面；LOG_LEVEL=DEBUG 诊断仅进入文件。\nBIOMIND_CLI_VERBOSITY=compact|verbose（默认 compact，独立于 BIOMIND_UI）。\n/tasks /new /resume /memory /wiki /task /cot /debug /exit 和 !cmd 保留原语义。')
                self.buffer.reset()
                return
        if self.pending is None:
            self.receive('activity', '本地草稿尚未提交；等待当前运行返回输入接口')
            return
        if self.paste_mode:
            if value.split('\n')[-1] != '/end':
                self.buffer.insert_text('\n')
                return
            value = '\n'.join(value.split('\n')[:-1])
        self.receive('message', '› ' + value)
        self._resolve(value)

    def _on_owner_loop(self, call):
        try:
            on_owner = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            on_owner = False
        if on_owner:
            call()
        else:
            self.loop.call_soon_threadsafe(call)

    def _open_details(self, text, title=''):
        def show():
            if self.closed:
                return
            self.details_view.text = safe_document((title + '\n\n' if title else '') + text)
            self.details_view.buffer.cursor_position = 0
            self.modal_history_cursor = len(self.model.items)
            self.modal = True
            self.app.layout.focus(self.details_view)
            self.app.invalidate()
        self._on_owner_loop(show)

    def show_details(self, text=None, title=''):
        if self.plain_owner is not None:
            return self.plain_owner.show_details(text=text, title=title)
        if text is not None:
            # A review remains available after later telemetry fills the
            # bounded diagnostics deque. It is only a sanitized display copy.
            document = safe_document(str(text))
            if (self.pending is not None and not self.pending.done()
                    and self.review_document and document != self.review_document):
                from agent_core.human_io import HumanInputUnavailable
                raise HumanInputUnavailable('cannot replace a pending approval detail')
            if document != self.review_document:
                self.review_prompt = ''
                self.review_hash = ''
            self.review_document = document
            self.review_title = safe_text(title)
            text = document + ('\n\n当前输入提示：\n' + self.review_prompt if self.review_prompt else '')
        else:
            with self.model.lock:
                tails = '\n\n'.join(str(group) + '\n' + tail for group, tail in self.model.tool_tails.items())
                text = self.model.details_text() + '\n\n工具显示尾部：\n' + tails
            if self.review_document:
                prompt = '\n\n当前输入提示：\n' + self.review_prompt if self.review_prompt else ''
                text = self.review_document + prompt + '\n\n最近诊断：\n' + text
                title = self.review_title or title
            if self.context[1] != 'interactive_prompt':
                text += '\n\n当前待输入对象（完整显示副本；后端仍核对授权）：\n' + json.dumps(self.context[2], ensure_ascii=False, indent=2)
        if self.review_document and self.review_hash:
            text += '\n\n审批显示正文 SHA-256: ' + self.review_hash
        self._open_details(str(text), title)

    def show_history(self):
        with self.model.lock:
            text = '\n\n'.join(item.text for item in self.model.items if item.presentation == 'history')
        self._open_details(text, '对话历史')

    def close_details(self):
        def close():
            self.modal = False
            if self._plan_body_visible():
                self.details_view.text = self.review_document
                self.details_view.buffer.cursor_position = 0
            self.app.layout.focus(self.input_control)
            self.buffer.cancel_completion()
            self._changed()
            self.app.invalidate()
        self._on_owner_loop(close)

    def request_cancel(self):
        if self.fallback and self.pending is None:
            if self.cancel_callback is not None:
                self.cancel_callback()
            return
        if self.cancel_requested:
            self.receive('cancel', '取消仍待确认；Ctrl+D 强制离开，清理未确认。')
            return
        if self.pending is not None:
            # Existing human-input cancellation path retains its semantics.
            if self.context[1] == 'interactive_prompt':
                self.buffer.reset()
                self.receive('message', '已清空输入；/exit 退出。')
            else:
                self._resolve('q', cancelling=True)
            return
        self.cancel_requested = True
        self.receive('cancel', '已请求取消，正在等待执行端确认；provider/子进程/保存均未确认。')
        if self.cancel_callback is not None:
            self.cancel_callback()

    def cancel_read(self):
        from agent_core.human_io import HumanInputEOF
        def close():
            self._fail_read(HumanInputEOF('input cancelled'))
        self.loop.call_soon_threadsafe(close)

    async def use_plain(self):
        """Transfer the existing pending read after Application detached stdin."""
        from agent_core.cli_plain import PlainTerminal
        from agent_core.cli_review import ReviewDisplay
        if self.history_flush is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.history_flush), self.presentation_timeout)
            except (Exception, asyncio.CancelledError):
                # A concurrent writer with unknown completion cannot safely be
                # replaced. Preserve the undecided request as unavailable.
                from agent_core.human_io import HumanInputUnavailable
                self.sink_broken = True
                self.writer_unavailable = True
                self.fallback = True
                self._fail_read(HumanInputUnavailable('previous terminal writer did not finish'))
                logging.getLogger(__name__).error('approval output unavailable; previous writer cleanup unconfirmed')
                return
        self.fallback = True
        plain = PlainTerminal(verbosity=self.model.verbosity)
        self.plain_owner = plain
        plain.model = self.model
        plain.history_cursor = self.history_cursor
        plain.set_context(*self.context)
        if self.review_document:
            plain.prepare_review(getattr(self, 'review_display', None) or
                ReviewDisplay(self.review_title, self.review_document,
                    self.review_title + '\n界面已降级；仍需决定。/details 查看完整对象。'))
        future = self.pending
        request = self.presentation
        if request is not None:
            request['ready'].set()
        if future is not None and not future.done():
            try:
                response = await asyncio.to_thread(plain.read, self.prompt_text,
                                                   multiline=self.paste_mode,
                                                   cancelled=lambda: self.pending is not future or future.done() or self.closed)
                if self.pending is future and not future.done():
                    if request is not None:
                        self._approval_event('decision_received', request)
                    self.pending = None
                    self.presentation = None
                    self.input_ready = False
                    future.set_result(response)
            except BaseException as exc:
                if self.pending is future:
                    self._fail_read(exc, expected=future)

    def handoff(self, call):
        from prompt_toolkit.application import run_in_terminal
        async def invoke():
            return await run_in_terminal(call, in_executor=True)
        return asyncio.run_coroutine_threadsafe(invoke(), self.loop).result()

    async def tick(self):
        while not self.closed:
            await asyncio.sleep(1)
            if self.pending is None:
                self.app.invalidate()


async def run_terminal(runtime):
    """Real CLI entry wrapper; runtime is the original async main function."""
    mode = os.getenv('BIOMIND_UI', '').lower()
    verbosity = display_verbosity(os.getenv('BIOMIND_CLI_VERBOSITY', 'compact'))
    async def run_plain():
        owner = PlainTerminal(enabled=mode != 'off', verbosity=verbosity)
        previous = bind_terminal(owner)
        previous_sink = set_notice_sink(lambda text: owner.receive('error', text))
        completed = False
        try:
            # Entry eligibility and all input/approval behavior remain with the
            # original runtime. This owner only gives initial plain an outlet.
            await runtime()
            completed = True
        finally:
            try:
                if completed:
                    await owner.drain()
            finally:
                owner.close()
                bind_terminal(previous)
                set_notice_sink(previous_sink)
    if mode in {'plain', 'off'} or os.getenv('TERM') == 'dumb' or not (sys.stdin.isatty() and sys.stdout.isatty()):
        await run_plain()
        return
    try:
        owner = Terminal(verbosity=verbosity)
    except ImportError:
        sys.stderr.write('动态界面依赖缺失；使用 plain 模式。安装 requirements-cli.txt 可启用。\n')
        await run_plain()
        return
    previous = bind_terminal(owner)
    previous_sink = set_notice_sink(lambda text: owner.receive('error', text))
    done = concurrent.futures.Future()
    def worker():
        try:
            asyncio.run(runtime())
            done.set_result(None)
        except BaseException as exc:
            done.set_exception(exc)
        finally:
            owner.loop.call_soon_threadsafe(lambda: owner.app.exit() if owner.app.is_running else None)
    thread = threading.Thread(target=worker, name='biomind-cli-runtime', daemon=True)
    timer = asyncio.create_task(owner.tick())
    previous_handler = owner.loop.get_exception_handler()
    def ui_exception(loop, context):
        logging.getLogger(__name__).error("终端绘制失败，降级 plain", exc_info=context.get("exception"))
        if owner.app.is_running:
            owner.app.exit(result="plain")
    owner.loop.set_exception_handler(ui_exception)
    try:
        try:
            result = await owner.app.run_async(pre_run=thread.start, set_exception_handler=False)
        except Exception:
            logging.getLogger(__name__).exception("终端异常，降级 plain")
            result = "plain"
        if result == "plain":
            # Application has restored terminal modes. Keep the same worker and
            # pending human request; do not cancel or recreate the business run.
            await owner.use_plain()
            while not done.done():
                await asyncio.sleep(0.05)
        if owner.history_flush is not None and not owner.writer_unavailable:
            await owner.history_flush
        owner.model.finalize_tools()
        if owner.plain_owner is not None:
            await owner.plain_owner.drain()
        elif not owner.writer_unavailable and owner.history_cursor < len(owner.model.items):
            # No interactive reader remains after Application returns. Flush
            # the final real tool group before restoring the display owner.
            owner.modal = False
            await owner._flush_history()
        if result == 'force':
            # Preserve existing process termination semantics after TTY restoration.
            sys.stderr.write('强制离开：任务整体清理未确认。\n')
            if owner.force_callback is not None:
                owner.force_callback()
            os._exit(130)
        if done.done():
            done.result()
    finally:
        owner.closed = True
        owner.prompt_text = ''
        owner.model.active = ''
        owner.model.tool_tail = ''
        owner.model.run_state = 'closed'
        owner.loop.set_exception_handler(previous_handler)
        owner.cancel_read()
        if owner.plain_owner is not None:
            owner.plain_owner.close()
        timer.cancel()
        bind_terminal(previous)
        set_notice_sink(previous_sink)

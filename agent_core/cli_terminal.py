"""Python terminal owner. The business coroutine keeps its own loop in one thread.

No state loaders, parser calls, authorization or retry decisions live here.
The worker isolates existing blocking runtime code from terminal key processing.
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import threading
import queue
import time

from agent_core.cli_display import DisplayModel, bind_terminal, safe_text, safe_document, display_verbosity
from agent_core.cli_logging import set_notice_sink


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


class PlainTerminal:
    """Explicit append-only owner; potentially blocking writes leave business alone."""
    def __init__(self, output=None, *, enabled=True, verbosity='compact'):
        self.mode = 'plain' if enabled else 'off'
        self.model = DisplayModel(verbosity=verbosity)
        self.output = output if output is not None else sys.stdout
        self.closed = False
        self.sink_broken = False
        self.last_ticket = None
        self.history_cursor = 0
        self.last_stage = None
        self.queue = queue.Queue()
        self.writer = threading.Thread(target=self._write_loop, name='biomind-cli-output', daemon=True)
        if enabled:
            self.writer.start()

    def receive(self, kind, text='', **data):
        with self.model.lock:
            ticket = self.model.receive(kind, text, **data)
            if ticket is None:
                return None
            if kind == 'stage' and ticket.source == 'runtime_fact' and ticket.presentation == 'status' and ticket.text.strip():
                stage = (ticket.scope.get('task_id', ''), ticket.scope.get('node', ''), ticket.text)
                if stage != self.last_stage:
                    # The model has already closed any preceding tool group.
                    # Only an accepted stage transition gains an append-only
                    # row; fenced activity and approval priorities stay silent.
                    self.last_stage = stage
                    ticket.presentation = 'history'
                    self.model.items.append(ticket)
                    self.model.history.append(ticket.text)
            if kind == 'tool_tail' and data.get('display_delta') and (self.model.verbosity == 'verbose' or data.get('purpose') == 'key_output'):
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
                if self.closed or self.sink_broken or self.mode == 'off':
                    ticket.finish(False, 'display_unavailable')
                else:
                    self.last_ticket = ticket
                    self.queue.put_nowait(ticket)

    def finish_tools(self):
        self.model.finalize_tools()
        self._queue_history()

    def show_details(self, text=None, title=''):
        body = text if text is not None else '\n\n'.join(self.model.details)
        return self.receive('message', (title + '\n' if title else '') + body, purpose='review_view')

    def _write_loop(self):
        while True:
            ticket = self.queue.get()
            if ticket is None:
                return
            if self.sink_broken:
                ticket.finish(False, 'sink_closed')
                continue
            try:
                self.output.write(ticket.text + '\n')
                ticket.times['write'] = time.monotonic()
                self.output.flush()
                ticket.times['flush'] = time.monotonic()
                ticket.finish(True)
            except Exception:
                # Never serialize arbitrary writer exceptions or retry a line
                # whose write might already have reached the sink.
                self.sink_broken = True
                ticket.finish(False, 'writer_failed')

    async def drain(self, timeout=0.05):
        self.finish_tools()
        if self.last_ticket is None:
            return True
        try:
            return await asyncio.wait_for(asyncio.shield(
                asyncio.wrap_future(self.last_ticket.confirmation)), timeout)
        except asyncio.TimeoutError:
            return self.last_ticket.confirmation.done() and bool(self.last_ticket.confirmation.result())

    def close(self):
        self.finish_tools()
        self.closed = True
        if self.mode != 'off':
            self.queue.put_nowait(None)


class Terminal:
    def __init__(self, *, verbosity='compact'):
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, HSplit, Window, ConditionalContainer
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.widgets import TextArea
        self.loop = asyncio.get_running_loop()
        self.mode = 'tty'
        self.model = DisplayModel(verbosity=verbosity)
        self.pending = None
        self.context = ('', 'interactive_prompt', {})
        self.draft = ''
        self.prompt_text = ''
        self.paste_mode = False
        self.modal = False
        self.modal_history_cursor = 0
        self.review_document = ''
        self.review_title = ''
        self.review_prompt = ''
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

        @keys.add('escape')
        def close_details(event):
            self.close_details()

        @keys.add('f2')
        def details(event):
            self.show_details()

        @keys.add('c-o')
        def transcript(event):
            self.show_history()

        @keys.add('pageup', filter=Condition(lambda: not self.modal))
        def browse(event):
            self.show_history()

        layout = HSplit([
            ConditionalContainer(self.details_view, Condition(lambda: self.modal)),
            Window(FormattedTextControl(self.status_line), height=1, wrap_lines=False),
            ConditionalContainer(Window(FormattedTextControl(lambda: '\n'.join(self.model.tool_tail.splitlines()[-2:])), height=2, wrap_lines=True), Condition(lambda: self.model.verbosity == 'verbose' and bool(self.model.tool_tail))),
            ConditionalContainer(Window(FormattedTextControl(lambda: self.prompt_text), height=Dimension(min=1, max=3), wrap_lines=True), Condition(lambda: bool(self.prompt_text) and not self.modal)),
            ConditionalContainer(Window(self.input_control, height=Dimension(min=1, max=6), wrap_lines=True), Condition(lambda: not self.modal)),
            Window(FormattedTextControl(self.input_hint), height=1, wrap_lines=False),
        ])
        self.app = Application(layout=Layout(layout, self.input_control), key_bindings=keys,
                               full_screen=False, min_redraw_interval=0.1,
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
        if self.modal:
            count = sum(item.presentation == 'history' for item in self.model.items[self.modal_history_cursor:])
            text = '浏览中' + (f' · {count} 条新消息' if count else '') + ' · Esc 返回输入'
        return self.fit_columns(text, self.app.output.get_size().columns)

    def input_hint(self):
        text = ('↑↓ / PgUp PgDn 浏览 · Esc 返回' if self.modal else
                'Enter 提交 · Ctrl+J 换行 · F2 详情 · Ctrl+O 历史 · Ctrl+C 取消')
        return self.fit_columns(text, self.app.output.get_size().columns)

    def _rendered(self):
        self.frames += 1

    @staticmethod
    def item_attributes(item):
        from prompt_toolkit.styles import DEFAULT_ATTRS
        if item.kind == 'error' or (item.kind == 'tool_end' and item.status == 'failed'):
            return DEFAULT_ATTRS._replace(color='ansired')
        if (item.kind in {'cancel', 'approval'} or
                (item.kind == 'tool_end' and item.status in {'cancelled', 'partial', 'unknown', 'not_dispatched'})):
            return DEFAULT_ATTRS._replace(color='ansiyellow')
        if item.kind in {'tool_start', 'tool_end', 'tool_group'} or item.source == 'diagnostic':
            return DEFAULT_ATTRS._replace(dim=True)
        return DEFAULT_ATTRS

    def _write_item(self, output, item):
        if self.fallback:
            output.write(item.text + '\n')
            return
        # ANSI palette entries follow the host theme. The default foreground
        # remains untouched for public paragraphs, and each item resets itself.
        output.set_attributes(self.item_attributes(item), self.app.color_depth)
        try:
            output.write(item.text + '\n')
        finally:
            output.reset_attributes()

    def receive(self, kind, text='', **data):
        ticket = self.model.receive(kind, text, **data)
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
        if self.closed or self.sink_broken:
            return
        if self.modal and not self.fallback:
            self.app.invalidate()
            return
        if len(self.model.items) > self.history_cursor and self.history_flush is None:
            self.history_flush = self.loop.create_task(self._flush_history())
            return
        if not self.fallback:
            self.app.invalidate()

    async def _flush_history(self):
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.application.current import set_app
        try:
            if self.modal and not self.fallback:
                return
            # A key paragraph is queued immediately, without the frame timer.
            with self.model.lock:
                first_index = self.history_cursor
                items = self.model.items[self.history_cursor:]
                self.history_cursor = len(self.model.items)
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
        self.context = (str(task_id), str(stage), deepcopy(dict(payload or {})))

    def read(self, prompt, timeout=0, multiline=False):
        from agent_core.human_io import HumanInputEOF, HumanInputTimeout, _readline_input_with_timeout
        if self.fallback:
            if not multiline:
                return plain_input(prompt, timeout)
            lines = []
            while True:
                line = plain_input(prompt if not lines else '... ', timeout)
                if line == '/end':
                    return '\n'.join(lines)
                lines.append(line)
        future = concurrent.futures.Future()
        def open_prompt():
            if self.closed:
                future.set_exception(HumanInputEOF('terminal closed'))
                return
            self.pending = future
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
            self.app.invalidate()
        self.loop.call_soon_threadsafe(open_prompt)
        try:
            return future.result(timeout=timeout or None)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise HumanInputTimeout('terminal input timed out')
        finally:
            def cleanup():
                if self.pending is future:
                    self.pending = None
            self.loop.call_soon_threadsafe(cleanup)

    def _resolve(self, text):
        future, self.pending = self.pending, None
        if future is not None and not future.done():
            self.model.release_priority()
            if self.context[1] == 'interactive_prompt':
                self.buffer.append_to_history()
            self.buffer.reset()
            self.prompt_text = '运行中可编辑本地草稿；尚未提交'
            future.set_result(text)

    def submit(self):
        if self.modal:
            return
        value = self.buffer.text
        if value == '/details' and not self.paste_mode:
            # This command is UI-only even inside approval. Keep the exact
            # pending Future, context and local draft; no backend read cycle.
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
        if text is not None:
            # A review remains available after later telemetry fills the
            # bounded diagnostics deque. It is only a sanitized display copy.
            document = safe_document(str(text))
            if document != self.review_document:
                self.review_prompt = ''
            self.review_document = document
            self.review_title = safe_text(title)
            text = document + ('\n\n当前输入提示：\n' + self.review_prompt if self.review_prompt else '')
        else:
            with self.model.lock:
                tails = '\n\n'.join(str(group) + '\n' + tail for group, tail in self.model.tool_tails.items())
                text = '\n\n'.join(self.model.details) + '\n\n工具显示尾部：\n' + tails
            if self.review_document:
                prompt = '\n\n当前输入提示：\n' + self.review_prompt if self.review_prompt else ''
                text = self.review_document + prompt + '\n\n最近诊断：\n' + text
                title = self.review_title or title
            if self.context[1] != 'interactive_prompt':
                text += '\n\n当前待输入对象（完整显示副本；后端仍核对授权）：\n' + json.dumps(self.context[2], ensure_ascii=False, indent=2)
        self._open_details(str(text), title)

    def show_history(self):
        with self.model.lock:
            text = '\n\n'.join(item.text for item in self.model.items if item.presentation == 'history')
        self._open_details(text, '对话历史')

    def close_details(self):
        def close():
            self.modal = False
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
                self._resolve('q')
            return
        self.cancel_requested = True
        self.receive('cancel', '已请求取消，正在等待执行端确认；provider/子进程/保存均未确认。')
        if self.cancel_callback is not None:
            self.cancel_callback()

    def cancel_read(self):
        from agent_core.human_io import HumanInputEOF
        def close():
            if self.pending is not None and not self.pending.done():
                self.pending.set_exception(HumanInputEOF('input cancelled'))
            self.pending = None
        self.loop.call_soon_threadsafe(close)

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
            owner.fallback = True
            sys.stderr.write("终端显示已降级 plain；原业务继续，未发起重试或取消。\n")
            if owner.pending is not None and not owner.pending.done():
                from agent_core.human_io import _readline_input_with_timeout, HumanInputUnavailable
                future = owner.pending
                try:
                    response = plain_input(owner.prompt_text)
                    future.set_result(response)
                except HumanInputUnavailable as exc:
                    future.set_exception(exc)
            while not done.done():
                await asyncio.sleep(0.05)
        if owner.history_flush is not None:
            await owner.history_flush
        owner.model.finalize_tools()
        if owner.history_cursor < len(owner.model.items):
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
        owner.loop.set_exception_handler(previous_handler)
        owner.cancel_read()
        timer.cancel()
        bind_terminal(previous)
        set_notice_sink(previous_sink)

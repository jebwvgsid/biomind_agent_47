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

from agent_core.cli_display import DisplayModel, bind_terminal, safe_text
from agent_core.cli_logging import set_notice_sink


def plain_input(prompt, timeout=0):
    """Canonical plain input without readline's ANSI/bracketed-paste modes."""
    from agent_core.human_io import HumanInputEOF, HumanInputTimeout
    import select
    sys.stdout.write(safe_text(prompt))
    sys.stdout.flush()
    if timeout and not select.select([sys.stdin], [], [], timeout)[0]:
        raise HumanInputTimeout('plain input timed out')
    value = sys.stdin.readline()
    if value == '':
        raise HumanInputEOF('plain input closed')
    return value[:-1] if value.endswith('\n') else value


class Terminal:
    def __init__(self):
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
        self.model = DisplayModel()
        self.pending = None
        self.context = ('', 'interactive_prompt', {})
        self.draft = ''
        self.prompt_text = ''
        self.paste_mode = False
        self.modal = False
        self.cancel_requested = False
        self.cancel_callback = None
        self.force_callback = None
        self.frames = 0
        self.history_cursor = 0
        self.history_flush = None
        self.closed = False
        self._notify_lock = threading.Lock()
        self._notify_pending = False
        self.fallback = False
        self.buffer = Buffer(multiline=True, completer=WordCompleter(['/help', '/status', '/details', '/logs', '/paste', '/exit', '/stop']), complete_while_typing=False)
        self.input_control = BufferControl(buffer=self.buffer)
        self.details_view = TextArea(read_only=True, scrollbar=True, wrap_lines=True)
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
            self.modal = False
            self.app.layout.focus(self.input_control)
            self.buffer.cancel_completion()

        @keys.add('f2')
        def details(event):
            self.show_details()

        layout = HSplit([
            Window(FormattedTextControl(lambda: 'BioMind · ' + safe_text(self.context[0]) + ' · ' + safe_text(os.getcwd())), height=1),
            ConditionalContainer(self.details_view, Condition(lambda: self.modal)),
            Window(FormattedTextControl(lambda: self.model.status()), height=2, wrap_lines=True),
            ConditionalContainer(Window(FormattedTextControl(lambda: '\n'.join(self.model.tool_tail.splitlines()[-3:])), height=3, wrap_lines=True), Condition(lambda: bool(self.model.tool_tail))),
            Window(FormattedTextControl(lambda: self.prompt_text), height=2, wrap_lines=True),
            Window(self.input_control, height=Dimension(min=1, max=6), wrap_lines=True),
            Window(FormattedTextControl('Enter 提交 · Ctrl+J 换行 · F2 详情 · Esc 返回 · Ctrl+C 请求取消'), height=1),
        ])
        self.app = Application(layout=Layout(layout, self.input_control), key_bindings=keys,
                               full_screen=False, min_redraw_interval=0.1,
                               after_render=lambda app: self._rendered())

    def _rendered(self):
        self.frames += 1

    def receive(self, kind, text='', **data):
        if self.closed:
            return
        self.model.receive(kind, text, **data)
        if self.fallback:
            if kind in {'message', 'error', 'result', 'cancel', 'approval', 'stage', 'tool_start', 'tool_end'}:
                sys.stdout.write(safe_text(text) + "\n")
                sys.stdout.flush()
            return
        with self._notify_lock:
            if not self._notify_pending:
                self._notify_pending = True
                self.loop.call_soon_threadsafe(self._changed)

    def _changed(self):
        with self._notify_lock:
            self._notify_pending = False
        if self.closed or self.fallback:
            return
        if len(self.model.history) > self.history_cursor and self.history_flush is None:
            self.history_flush = self.loop.create_task(self._flush_history())
        self.app.invalidate()

    async def _flush_history(self):
        from prompt_toolkit.application import run_in_terminal
        try:
            # Append stable history above the live inline application once.
            await asyncio.sleep(0.1)
            with self.model.lock:
                lines = self.model.history[self.history_cursor:]
                self.history_cursor = len(self.model.history)
            if lines:
                def append():
                    self.app.output.write(safe_text('\n'.join(lines)) + '\n')
                    self.app.output.flush()
                await run_in_terminal(append)
        finally:
            self.history_flush = None
            if not self.closed and not self.fallback and len(self.model.history) > self.history_cursor:
                self._changed()

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
                self.draft = self.buffer.text
                self.buffer.reset()
                self.receive('approval', '等待用户输入 · ' + self.context[1])
                self.model.details.append(json.dumps(self.context[2], ensure_ascii=False, indent=2))
            else:
                self.cancel_requested = False
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
            if self.context[1] == 'interactive_prompt':
                self.buffer.append_to_history()
            self.buffer.reset()
            self.prompt_text = '运行中可编辑本地草稿；尚未提交'
            future.set_result(text)

    def submit(self):
        if self.modal:
            return
        value = self.buffer.text
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
                    for path in paths:
                        try:
                            with open(path, 'rb') as handle:
                                handle.seek(0, 2)
                                handle.seek(max(0, handle.tell() - 4096))
                                self.model.details.append(safe_text(handle.read(4096).decode('utf-8', 'replace')))
                        except OSError:
                            self.model.details.append('日志尾部读取失败：' + path)
                    self.show_details()
                else:
                    self.receive('message', '/status 状态 · /details 显示详情 · /logs 日志位置\nCtrl+J 换行，bracketed paste 整段接收；/paste 后以 /end 结束。\n运行中草稿不自动提交；F2 详情，Esc 返回。\nCtrl+C 请求取消；取消中再次 Ctrl+C 提示，Ctrl+D 强制离开且清理未确认。\nBIOMIND_UI=plain 禁用动态界面；LOG_LEVEL=DEBUG 诊断仅进入文件。\n/tasks /new /resume /memory /wiki /task /cot /debug /exit 和 !cmd 保留原语义。')
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

    def show_details(self):
        text = '\n\n'.join(self.model.details) + '\n\n工具显示尾部：\n' + self.model.tool_tail
        if self.context[1] != 'interactive_prompt':
            text += '\n\n当前待输入对象（完整显示副本；后端仍核对授权）：\n' + json.dumps(self.context[2], ensure_ascii=False, indent=2)
        self.details_view.text = safe_text(text)
        self.modal = True
        self.app.layout.focus(self.details_view)
        self.app.invalidate()

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
    if os.getenv('BIOMIND_UI', '').lower() == 'plain' or os.getenv('TERM') == 'dumb' or not (sys.stdin.isatty() and sys.stdout.isatty()):
        await runtime()
        return
    try:
        owner = Terminal()
    except ImportError:
        sys.stderr.write('动态界面依赖缺失；使用 plain 模式。安装 requirements-cli.txt 可启用。\n')
        await runtime()
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

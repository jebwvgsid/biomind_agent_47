"""Plain ownership regressions; all inputs and output sinks are synthetic."""
import asyncio
import concurrent.futures
import io
import os
import queue
import threading
import time

import pytest

from agent_core.cli_plain import PlainTerminal
from agent_core.cli_review import review_display
from agent_core.human_io import HumanInputEOF, HumanInputUnavailable


class Lines:
    def __init__(self):
        self.values = queue.Queue()
        self.called = queue.Queue()

    def isatty(self):
        return False

    def readline(self):
        self.called.put(True)
        return self.values.get(timeout=3)


def reader(owner, result):
    try:
        result.put(('value', owner.read('Enter/y=run; n=reject > ')))
    except BaseException as exc:
        result.put(('error', exc))


def begin(monkeypatch, *, enabled=True, writer=None, verbosity='compact'):
    import agent_core.cli_plain as plain
    import agent_core.human_io as human
    stdin = Lines()
    monkeypatch.setattr(plain.sys, 'stdin', stdin)
    monkeypatch.setattr(human, 'read_pending_paste_lines', lambda **kwargs: [])
    owner = PlainTerminal(writer or io.StringIO(), enabled=enabled, verbosity=verbosity)
    owner.prepare_review(review_display('代码审批', '\n'.join(f'CODE-{i}' for i in range(450))))
    owner.set_context('synthetic-task', 'code_review', {'action_id': 'candidate-A'})
    result = queue.Queue()
    thread = threading.Thread(target=reader, args=(owner, result), daemon=True)
    return owner, stdin, result, thread


@pytest.mark.parametrize('enabled', [True, False])
def test_plain_pending_is_last_and_late_output_cannot_execute(monkeypatch, enabled):
    owner, stdin, result, thread = begin(monkeypatch, enabled=enabled)
    try:
        thread.start()
        stdin.called.get(timeout=2)
        for index in range(500):
            owner.receive('message', f'BACKGROUND-{index}')
        assert result.empty()
        shown = owner.output.getvalue()
        assert shown.endswith('Enter/y=run; n=reject > ')
        assert 'CODE-449' not in shown and 'BACKGROUND-' not in shown
        assert any('BACKGROUND-499' in row for row in owner.model.history)
        stdin.values.put('\n')
        assert result.get(timeout=2) == ('value', '')
        phases = [event['phase'] for event in owner.approval_events]
        assert phases == ['request_created', 'presentation_started',
                          'presentation_written', 'input_ready', 'decision_received']
        assert all(event['action_id'] == 'candidate-A' for event in owner.approval_events)
    finally:
        owner.close()


def test_plain_finite_prior_writer_batch_precedes_input(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class DelayedWriter(io.StringIO):
        def write(self, value):
            if value.startswith('PRIOR'):
                entered.set()
                assert release.wait(timeout=2)
            return super().write(value)

    owner, stdin, result, thread = begin(monkeypatch, writer=DelayedWriter(), verbosity='verbose')
    try:
        owner.receive('message', 'PRIOR\n' + '\n'.join(f'PLAN-{i}' for i in range(15)))
        assert entered.wait(timeout=2)
        thread.start()
        # This assertion uses the real blocked writer boundary, not a guessed
        # amount of time for rendering to finish.
        assert stdin.called.empty() and result.empty()
        release.set()
        stdin.called.get(timeout=2)
        text = owner.output.getvalue()
        assert text.index('PRIOR') < text.index('CODE-449') < text.rindex('Enter/y=run')
        stdin.values.put('n\n')
        assert result.get(timeout=2) == ('value', 'n')
    finally:
        release.set()
        owner.close()


def test_plain_details_is_same_request_and_does_not_decide(monkeypatch):
    owner, stdin, result, thread = begin(monkeypatch)
    try:
        thread.start()
        stdin.called.get(timeout=2)
        stdin.values.put('/details\n')
        stdin.called.get(timeout=2)
        assert result.empty()
        text = owner.output.getvalue()
        assert 'CODE-449' in text and '仍待审批' in text
        assert text.endswith('Enter/y=run; n=reject > ')
        assert len({event['request_id'] for event in owner.approval_events}) == 1
        assert not any(event['phase'] == 'decision_received' for event in owner.approval_events)
        stdin.values.put('q\n')
        assert result.get(timeout=2) == ('value', 'q')
    finally:
        owner.close()


def test_plain_second_reader_and_stale_context_are_rejected(monkeypatch):
    owner, stdin, result, thread = begin(monkeypatch)
    try:
        thread.start()
        stdin.called.get(timeout=2)
        with pytest.raises(HumanInputUnavailable):
            owner.read('OTHER > ')
        with pytest.raises(HumanInputUnavailable):
            owner.set_context('synthetic-task', 'code_review', {'action_id': 'candidate-B'})
        with pytest.raises(HumanInputUnavailable):
            owner.show_details(text='another candidate body')
        assert owner.pending['action_id'] == 'candidate-A'
        stdin.values.put('n\n')
        assert result.get(timeout=2) == ('value', 'n')
    finally:
        owner.close()


def test_plain_eof_is_not_empty_enter(monkeypatch):
    owner, stdin, result, thread = begin(monkeypatch, enabled=False)
    try:
        thread.start()
        stdin.called.get(timeout=2)
        stdin.values.put('')
        kind, error = result.get(timeout=2)
        assert kind == 'error' and isinstance(error, HumanInputEOF)
        assert not any(event['phase'] == 'decision_received' for event in owner.approval_events)
    finally:
        owner.close()


def test_plain_urgent_output_preserves_input_for_same_request(monkeypatch):
    owner, stdin, result, thread = begin(monkeypatch)
    try:
        thread.start()
        stdin.called.get(timeout=2)
        urgent = owner.receive('error', 'synthetic required warning')
        assert urgent.confirmation.result(timeout=2)
        assert result.empty()
        assert '已键入内容仍保留' in owner.output.getvalue()
        assert owner.output.getvalue().endswith('Enter/y=run; n=reject > ')
        stdin.values.put('preserved feedback\n')
        assert result.get(timeout=2) == ('value', 'preserved feedback')
    finally:
        owner.close()


def test_plain_writer_failure_never_enables_input(monkeypatch):
    class BrokenWriter(io.StringIO):
        def write(self, value):
            raise OSError('synthetic closed sink')

    owner, stdin, result, thread = begin(monkeypatch, writer=BrokenWriter())
    try:
        thread.start()
        kind, error = result.get(timeout=2)
        assert kind == 'error' and isinstance(error, HumanInputUnavailable)
        assert stdin.called.empty()
        assert not any(event['phase'] == 'input_ready' for event in owner.approval_events)
    finally:
        owner.close()


def test_plain_paste_keeps_details_text_inside_feedback(monkeypatch):
    import agent_core.human_io as human
    owner, stdin, result, thread = begin(monkeypatch)
    try:
        thread.start()
        stdin.called.get(timeout=2)
        stdin.values.put('\x1b[200~feedback\n')
        stdin.values.put('/details\n')
        stdin.values.put('y\x1b[201~\n')
        assert result.get(timeout=2) == ('value', 'feedback\n/details\ny')
        assert 'CODE-449' not in owner.output.getvalue()
    finally:
        owner.close()


def test_legacy_non_safe_enter_keeps_its_original_rule(monkeypatch):
    from agent_core.cli_display import bind_terminal
    from execution.confirmation import ConfirmationConfig, ConfirmationManager, ConfirmationMode
    owner, stdin, result, _ = begin(monkeypatch)
    old = bind_terminal(owner)
    manager = ConfirmationManager(ConfirmationConfig(mode=ConfirmationMode.ALWAYS,
                                                     timeout_seconds=0))
    def review():
        result.put(manager.request_confirmation('touch synthetic-review-only', 'Bash'))
    thread = threading.Thread(target=review, daemon=True)
    try:
        thread.start()
        stdin.called.get(timeout=2)
        assert owner.context[1] == 'legacy_confirmation'
        assert 'CODE-449' not in owner.output.getvalue()
        assert '[y/Enter]' not in owner.output.getvalue()
        stdin.values.put('\n')
        stdin.called.get(timeout=2)
        assert result.empty() and manager.approval_history == []
        stdin.values.put('n\n')
        assert result.get(timeout=2) == 'n'
        assert len(manager.approval_history) == 1
        assert manager.approval_history[0]['approved'] is False
    finally:
        bind_terminal(old)
        owner.close()


@pytest.mark.parametrize('cancel_kind', ['predicate', 'close'])
def test_plain_real_pipe_wait_is_cancellable_without_a_human_deadline(monkeypatch, cancel_kind):
    import agent_core.cli_plain as plain
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'r', encoding='utf-8')
    owner = PlainTerminal(io.StringIO())
    result = queue.Queue()
    cancelled = threading.Event()
    def read():
        try:
            result.put(owner.read('No deadline > ', timeout=0, cancelled=cancelled.is_set))
        except HumanInputUnavailable as exc:
            result.put(exc)
    try:
        monkeypatch.setattr(plain.sys, 'stdin', stream)
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        while not any(event['phase'] == 'input_ready' for event in owner.approval_events):
            assert time.monotonic() < deadline
            threading.Event().wait(0.005)
        assert result.empty()
        if cancel_kind == 'predicate':
            cancelled.set()
        else:
            owner.close()
        assert isinstance(result.get(timeout=1), HumanInputEOF)
    finally:
        owner.close()
        stream.close()
        os.close(write_fd)


def test_unfinished_tty_writer_cannot_create_a_competing_plain_writer():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    from agent_core.cli_terminal import Terminal
    async def scenario():
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            owner = Terminal()
            original = concurrent.futures.Future()
            owner.pending = original
            owner.history_flush = asyncio.get_running_loop().create_future()
            owner.presentation_timeout = 0.01
            try:
                await owner.use_plain()
                assert isinstance(original.exception(), HumanInputUnavailable)
                assert owner.writer_unavailable and owner.sink_broken
                assert owner.plain_owner is None and owner.pending is None
            finally:
                owner.history_flush.cancel()
                owner.closed = True
    asyncio.run(scenario())


def test_pending_tty_view_cannot_be_replaced_by_late_review_or_detail():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    from agent_core.cli_terminal import Terminal
    async def scenario():
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            owner = Terminal()
            original_view = review_display('代码审批', 'original candidate')
            owner.prepare_review(original_view)
            original_future = concurrent.futures.Future()
            owner.pending = original_future
            try:
                with pytest.raises(HumanInputUnavailable):
                    owner.prepare_review(review_display('代码审批', 'other candidate'))
                with pytest.raises(HumanInputUnavailable):
                    owner.show_details(text='other candidate')
                assert owner.review_document == 'original candidate'
                assert owner.pending is original_future and not original_future.done()
            finally:
                owner.pending = None
                owner.closed = True
                original_future.cancel()
    asyncio.run(scenario())


def test_queued_tty_read_moves_once_to_already_cancelled_plain_owner(monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    from agent_core.cli_terminal import Terminal
    async def scenario():
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            owner = Terminal()
            plain = PlainTerminal(io.StringIO())
            plain.close()
            queued = []
            captured = threading.Event()
            result = queue.Queue()
            loop = asyncio.get_running_loop()
            original_dispatch = loop.call_soon_threadsafe
            def dispatch(callback, *args, **kwargs):
                if getattr(callback, '__name__', '') == 'open_prompt':
                    queued.append(callback)
                    captured.set()
                    return None
                return original_dispatch(callback, *args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(loop, 'call_soon_threadsafe', dispatch)
                thread = threading.Thread(target=reader, args=(owner, result), daemon=True)
                thread.start()
                deadline = time.monotonic() + 2
                while not captured.is_set():
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.005)
                assert owner.pending is None and len(queued) == 1
                owner.fallback = True
                owner.plain_owner = plain
                queued.pop()()
                while result.empty():
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.005)
                kind, error = result.get_nowait()
                assert kind == 'error' and isinstance(error, HumanInputEOF)
                assert owner.pending is None and owner.presentation is None
                owner.closed = True
    asyncio.run(scenario())


@pytest.mark.parametrize('answer', ['y', 'n', 'eof'])
def test_real_run_terminal_transfers_original_request_after_renderer_failure(monkeypatch, tmp_path, answer):
    """A real Application and kernel PTY exercise the production fallback.

    Input is injected by this synthetic harness only after the plain owner's
    write acknowledgement. The old Application never receives a decision.
    """
    import fcntl
    import select
    import struct
    import termios
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_input
    from prompt_toolkit.output import create_output
    import agent_core.cli_terminal as terminal_module
    from agent_core.cli_display import terminal
    from agent_core.cli_review import show_review
    from agent_core.human_io import safe_input

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
    input_stream = os.fdopen(os.dup(slave), 'r', encoding='utf-8')
    output_stream = os.fdopen(os.dup(slave), 'w', encoding='utf-8', buffering=1)
    owners, original_requests, read_requests, outcomes = [], [], [], []
    transcript = bytearray()
    stop = threading.Event()
    master_closed = threading.Event()
    errors = []
    def diagnostic(label):
        with (tmp_path / 'fallback-events.txt').open('a', encoding='utf-8') as record:
            record.write(label + '\n')
    original_init = terminal_module.Terminal.__init__
    original_plain_read = PlainTerminal.read

    def initialize(owner, **kwargs):
        diagnostic('terminal_init_begin')
        original_init(owner, **kwargs)
        diagnostic('terminal_init_end')
        owners.append(owner)
        injected = False

        def fail_once(app):
            nonlocal injected
            if owner.pending is not None and owner.input_ready and not injected:
                injected = True
                diagnostic('renderer_failure_injected')
                original_requests.append(owner.pending)
                raise RuntimeError('synthetic approval renderer failure')
        owner.app.after_render += fail_once

    def observed_plain_read(owner, *args, **kwargs):
        diagnostic('plain_read_begin')
        assert owners[0].pending is original_requests[0]
        read_requests.append(owners[0].pending)
        return original_plain_read(owner, *args, **kwargs)

    def inject_and_drain():
        diagnostic('harness_begin')
        deadline = time.monotonic() + 12
        injected = False
        finished_seen = None
        stack_written = False
        try:
            while time.monotonic() < deadline and not stop.is_set():
                if select.select([master], [], [], 0.01)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    transcript.extend(chunk)
                    # Respond to CPR so Application readiness does not depend
                    # on prompt_toolkit's unrelated terminal-detection timeout.
                    if b'\x1b[6n' in chunk:
                        os.write(master, b'\x1b[1;1R')
                plain = owners[0].plain_owner if owners else None
                if (plain is not None and not injected
                        and any(event['phase'] == 'input_ready' for event in plain.approval_events)):
                    assert outcomes == []
                    assert len(read_requests) == 1
                    injected = True
                    diagnostic('plain_input_ready_inject_' + answer)
                    os.write(master, b'\x04' if answer == 'eof' else (answer + '\n').encode())
                if outcomes:
                    finished_seen = finished_seen or time.monotonic()
                    if time.monotonic() - finished_seen > 2 and not stack_written:
                        import faulthandler
                        with (tmp_path / 'fallback-stack.txt').open('w') as stack:
                            faulthandler.dump_traceback(file=stack, all_threads=True)
                        stack_written = True
            if not injected:
                owner = owners[0] if owners else None
                errors.append({'failure': 'plain input did not become ready',
                               'owner': bool(owner),
                               'pending': bool(owner and owner.pending),
                               'ready': bool(owner and owner.input_ready),
                               'fallback': bool(owner and owner.fallback),
                               'plain': bool(owner and owner.plain_owner),
                               'events': list(owner.approval_events) if owner else []})
                (tmp_path / 'fallback-failure.txt').write_text(repr(errors), encoding='utf-8')
                (tmp_path / 'fallback.raw').write_bytes(transcript)
                # Bound the synthetic test even if the production handoff is
                # stuck in a stdin reader. Closing the harness side produces
                # a real EOF; no fallback decision is invented.
                os.close(master)
                master_closed.set()
        except BaseException as exc:
            errors.append(repr(exc))
            diagnostic('harness_error_' + repr(exc))

    async def runtime():
        diagnostic('runtime_begin')
        owner = terminal()
        view = review_display('代码审批', '\n'.join(f'SYNTHETIC-CODE-{i}' for i in range(450)))
        show_review(view)
        owner.set_context('synthetic-fallback', 'code_review', {'action_id': 'same-candidate'})
        diagnostic('runtime_read_begin')
        try:
            outcomes.append(('value', safe_input('Fallback approval y/Enter=run; n=reject > ', timeout_seconds=0)))
        except HumanInputUnavailable as exc:
            outcomes.append(('error', type(exc).__name__))
        diagnostic('runtime_read_finished')

    async def run_entry():
        # Reuse the existing sandbox callback servicing fixture. The genuine
        # kernel PTY, Application, fallback and default executor still run;
        # only the sandbox's deferred selector wakeups receive a timer tick.
        from tests.test_cli_v2_display import service_writer_callbacks
        service_writer_callbacks(asyncio.get_running_loop())
        await asyncio.wait_for(terminal_module.run_terminal(runtime), timeout=15)
        diagnostic('run_terminal_coroutine_finished')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(terminal_module.sys, 'stdin', input_stream)
            patch.setattr(terminal_module.sys, 'stdout', output_stream)
            patch.setattr(terminal_module.Terminal, '__init__', initialize)
            patch.setattr(PlainTerminal, 'read', observed_plain_read)
            patch.setenv('BIOMIND_UI', '')
            patch.setenv('TERM', 'xterm-256color')
            harness = threading.Thread(target=inject_and_drain, daemon=True)
            harness.start()
            diagnostic('run_terminal_begin')
            # Each parameter owns a distinct PTY. Do not reuse the library's
            # default AppSession cached stdin from the previous closed PTY.
            with create_app_session(input=create_input(stdin=input_stream),
                                    output=create_output(stdout=output_stream)):
                asyncio.run(run_entry())
            diagnostic('run_terminal_finished')
            stop.set()
            harness.join(timeout=2)
        assert errors == []
        assert len(read_requests) == len(original_requests) == 1
        assert read_requests[0] is original_requests[0]
        if answer == 'eof':
            assert outcomes == [('error', 'HumanInputEOF')]
            assert not any(event['phase'] == 'decision_received'
                           for event in owners[0].plain_owner.approval_events)
        else:
            assert outcomes == [('value', answer)]
        assert b'Fallback approval' in transcript
    finally:
        stop.set()
        input_stream.close()
        output_stream.close()
        os.close(slave)
        if not master_closed.is_set():
            os.close(master)

"""Offline display contracts; synthetic text only, no business configuration."""
import asyncio
import io
import json
import threading
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_core.cli_display import (
    DisplayModel, StreamDisplayCopy, bind_terminal, display_print, emit,
    emit_public, flush_public, safe_text,
)
from agent_core.cli_terminal import PlainTerminal, Terminal, run_terminal


def service_cross_thread_callbacks():
    # Test-only service for restricted self-pipe wakeups, including asyncio's
    # executor shutdown. loop.close() discards the pending timer.
    loop = asyncio.get_running_loop()
    def pulse():
        loop.call_later(.001, pulse)
    pulse()


class Writer(io.StringIO):
    def __init__(self, gate=None, failure=False):
        super().__init__()
        self.gate = gate
        self.failure = failure
        self.calls = []

    def write(self, value):
        if self.gate is not None:
            self.gate.wait()
        self.calls.append(('write', time.monotonic(), value))
        if self.failure:
            raise BrokenPipeError('synthetic sink')
        return super().write(value)

    def flush(self):
        self.calls.append(('flush', time.monotonic(), ''))


def test_items_have_sources_and_only_identity_deduplicates():
    model = DisplayModel()
    prefix = '同一前缀' * 30
    model.receive('model_public', prefix + '甲', source='model_public', call_id='one', item_id='m')
    model.receive('model_public', prefix + '乙', source='model_public', call_id='one', item_id='n')
    model.receive('model_public', prefix + '甲', source='model_public', call_id='two', item_id='m')
    model.receive('model_public', prefix + '甲', source='model_public', call_id='two', item_id='m')
    model.receive('error', 'generated failure AIMessage', source='runtime_fact')
    model.receive('details', 'diagnostic copy', source='diagnostic')
    assert len(model.history) == 4
    assert [item.source for item in model.items] == ['model_public'] * 3 + ['runtime_fact']
    assert model.details[-1] == 'diagnostic copy'
    for _ in range(2):
        model.receive('model_public', '相同但没有item身份', source='model_public', item_id='')
    assert len(model.history) == 6


def test_late_provider_does_not_replace_scope_but_execution_receipt_survives():
    model = DisplayModel()
    old = dict(task_id='task', node='generate', call_id='old', provider_attempt_id='p1')
    new = {**old, 'call_id': 'new', 'provider_attempt_id': 'p2'}
    model.receive('activity', 'old started', event='attempt_started', **old)
    model.receive('activity', 'old fenced', event='attempt_fenced', **old)
    model.receive('activity', 'new started', event='attempt_started', **new)
    model.receive('activity', 'late token', **old)
    model.receive('tool_end', 'old execution cancelled', execution_attempt_id='exec1', **old)
    assert model.active == 'new started'
    assert model.history[-1] == 'old execution cancelled'
    assert model.items[-1].scope['execution_attempt_id'] == 'exec1'
    assert model.items[-1].scope['provider_attempt_id'] == 'p1'


@pytest.mark.parametrize('priority', ['approval', 'cancel'])
def test_approval_cancel_cannot_be_replaced_by_late_activity(priority):
    model = DisplayModel()
    model.receive('model_public', '说明', task_id='first')
    model.receive(priority, '等待确认', task_id='first')
    for kind in ['activity', 'stage', 'tool_start', 'tool_end', 'tool_tail']:
        model.receive(kind, '迟到活动', task_id='first')
    assert model.active == '等待确认'
    model.receive('activity', '下一任务', task_id='second', event='attempt_started')
    assert model.active == '下一任务'


def test_tool_tails_are_grouped_by_execution_and_stream():
    model = DisplayModel()
    for action, stream, text in [('a', 'stdout', 'a out'), ('a', 'stderr', 'a err'), ('b', 'stdout', 'b out')]:
        model.receive('tool_tail', text, task_id='t', action_id=action, execution_attempt_id='e', stream=stream)
    assert set(model.tool_tails.values()) == {'a out', 'a err', 'b out'}
    assert len(model.tool_tails) == 3
    model.receive('tool_start', 'a started', task_id='t', action_id='a', execution_attempt_id='e')
    model.receive('tool_start', 'b started', task_id='t', action_id='b', execution_attempt_id='e')
    model.receive('tool_end', 'a exited', task_id='t', action_id='a', execution_attempt_id='e')
    assert model.active == 'b started'
    assert model.history[-1] == 'a exited'


def test_bounded_complete_lines_mask_synthetic_secrets_across_chunks():
    raw = b'Authorization: Bearer fixture-bearer\napi_key="fixture key" password=fixture-password token=fixture-token\n\x1b]0;injected\x07ok\n'
    model = DisplayModel()
    previous = bind_terminal(model)
    try:
        copy = StreamDisplayCopy('stdout', task_id='synthetic', action_id='a', execution_attempt_id='e')
        for byte in raw:
            copy.feed(bytes([byte]))
        copy.feed(b'', final=True)
        for secret in ['fixture-bearer', 'fixture key', 'fixture-password', 'fixture-token', 'injected']:
            assert secret not in copy.tail
        assert '[REDACTED]' in copy.tail
        assert raw.startswith(b'Authorization: Bearer fixture-bearer')
        assert all(key[0] == 'synthetic' for key in model.tool_tails)
        copy.feed(b'x' * 5000 + b'\n')
        assert '输出片段已省略' in copy.tail
    finally:
        bind_terminal(previous)
    assert safe_text('sk-fixture123456789') == '[REDACTED]'
    assert safe_text('x' * 70000) == '输出片段已省略'


def test_public_normal_five_times_and_append_order():
    async def run():
        writer = Writer()
        owner = PlainTerminal(writer)
        previous = bind_terminal(owner)
        try:
            first = emit_public('看到了 fixture-A，接下来读取。', task_id='synthetic', call_id='c1')
            assert await flush_public()
            dispatch = time.monotonic()
            emit('tool_start', '读取开始', task_id='synthetic', tool_call_id='t1')
            emit('tool_end', '读取返回', task_id='synthetic', tool_call_id='t1')
            stream = StreamDisplayCopy('stdout', task_id='synthetic', tool_call_id='t1')
            stream.feed(b'fixture output\n')
            stream.feed(b'', final=True)
            second = emit_public('读取返回 fixture-B，接下来搜索。', task_id='synthetic', call_id='c2')
            assert await flush_public()
            assert first.times['ready'] <= first.times['enqueued'] <= first.times['write'] <= first.times['flush'] <= dispatch
            assert second.times['flush'] > dispatch
            assert writer.getvalue().splitlines() == ['看到了 fixture-A，接下来读取。', '读取开始', '读取返回', 'fixture output', '读取返回 fixture-B，接下来搜索。']
            print('PLAIN_NORMAL_FIVE_TIMES=' + json.dumps({**first.times, 'dispatch': dispatch, 'clock': 'time.monotonic', 'writer': 'PlainTerminal/Writer'}))
        finally:
            owner.close()
            bind_terminal(previous)
    asyncio.run(run())


def test_public_timeout_retains_later_order_and_cancellation_propagates():
    async def run():
        gate = threading.Event()
        writer = Writer(gate)
        owner = PlainTerminal(writer)
        previous = bind_terminal(owner)
        try:
            ticket = emit_public('已形成说明', call_id='timeout')
            assert not await flush_public(timeout=.001)
            dispatch = time.monotonic()
            assert 'write' not in ticket.times
            emit('tool_start', '随后真实派发', tool_call_id='t')
            async def cancelled_wait():
                emit_public('取消等待的显示', call_id='cancel')
                task = asyncio.current_task()
                asyncio.get_running_loop().call_soon(task.cancel)
                await flush_public(timeout=10)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.create_task(cancelled_wait())
            gate.set()
            assert await owner.drain(timeout=1)
            assert ticket.times['degraded'] <= dispatch <= ticket.times['write'] <= ticket.times['flush']
            assert writer.getvalue().splitlines()[:2] == ['已形成说明', '随后真实派发']
            print('PLAIN_TIMEOUT_FIVE_TIMES=' + json.dumps({**ticket.times, 'dispatch': dispatch, 'clock': 'time.monotonic', 'writer': 'PlainTerminal/Writer gated'}))
        finally:
            gate.set()
            owner.close()
            bind_terminal(previous)
    asyncio.run(run())


def test_broken_pipe_stops_writing_and_off_never_falls_back():
    async def run():
        writer = Writer(failure=True)
        owner = PlainTerminal(writer)
        previous = bind_terminal(owner)
        try:
            emit_public('first')
            assert not await flush_public()
            emit_public('second')
            assert not await flush_public()
            assert len(writer.calls) == 1
            owner.close()
            off_writer = Writer()
            owner = PlainTerminal(off_writer, enabled=False)
            bind_terminal(owner)
            emit_public('hidden')
            assert not await flush_public()
            display_print('also hidden')
            assert off_writer.getvalue() == ''
        finally:
            owner.close()
            bind_terminal(previous)
    asyncio.run(run())


def test_context_local_confirmation_has_no_cross_call_wait():
    async def run():
        model = DisplayModel()
        previous = bind_terminal(model)
        barrier = asyncio.Event()
        async def first():
            ticket = emit_public('one', call_id='c1')
            await barrier.wait()
            ticket.finish(True)
            assert await flush_public()
        async def second():
            ticket = emit_public('two', call_id='c2')
            barrier.set()
            assert not await flush_public(timeout=.001)
            assert not ticket.confirmation.done()
        try:
            await asyncio.gather(first(), second())
            assert await flush_public(timeout=0)
        finally:
            bind_terminal(previous)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['plain', 'off'])
def test_production_run_terminal_selects_explicit_initial_owner(monkeypatch, capsys, mode):
    monkeypatch.setenv('BIOMIND_UI', mode)
    async def runtime():
        emit_public('生产入口合成说明', call_id='runtime')
        await flush_public()
        emit('tool_end', 'rc=0，等待正式核验')
    asyncio.run(run_terminal(runtime))
    output = capsys.readouterr().out
    assert output == ('生产入口合成说明\nrc=0，等待正式核验\n' if mode == 'plain' else '')


class RecordingOutput(DummyOutput):
    def __init__(self, gate=None):
        self.writer = Writer()
        self.gate = gate

    def write(self, data):
        if data.startswith('TTY模型说明') and self.gate is not None:
            self.gate.wait()
        self.writer.write(data)

    def flush(self):
        self.writer.flush()


@pytest.mark.parametrize('timeout', [False, True])
def test_actual_terminal_application_confirms_write_flush_and_timeout(timeout):
    gate = threading.Event() if timeout else None
    output = RecordingOutput(gate)
    async def run():
        owner = Terminal()
        previous = bind_terminal(owner)
        # Restricted offline socketpair wakeups may not wake an asyncio loop.
        # This test-only timer services real future receipts; it never claims
        # a write/flush or imposes a business reading delay.
        service_cross_thread_callbacks()
        async def business():
            try:
                ticket = emit_public('TTY模型说明：准备读取合成文件。', call_id='tty')
                confirmed = await flush_public(timeout=.002 if timeout else 1)
                dispatch = time.monotonic()
                emit('tool_start', 'TTY真实读取派发', tool_call_id='tool')
                assert confirmed is (not timeout)
                if timeout:
                    assert 'write' not in ticket.times
                    gate.set()
                while owner.history_cursor < len(owner.model.items) or owner.history_flush is not None:
                    await asyncio.sleep(0)
                if timeout:
                    assert ticket.times['enqueued'] < dispatch <= ticket.times['write'] <= ticket.times['flush']
                else:
                    assert ticket.times['ready'] <= ticket.times['enqueued'] <= ticket.times['write'] <= ticket.times['flush'] <= dispatch
                transcript = output.writer.getvalue()
                assert transcript.index('TTY模型说明') < transcript.index('TTY真实读取派发')
                print('TTY_' + ('TIMEOUT' if timeout else 'NORMAL') + '_FIVE_TIMES=' + json.dumps({**ticket.times, 'dispatch': dispatch, 'clock': 'time.monotonic', 'writer': 'Terminal/Application/RecordingOutput'}))
            finally:
                if gate is not None:
                    gate.set()
                owner.app.exit()
        task = None
        def start():
            nonlocal task
            task = asyncio.create_task(business())
        try:
            await owner.app.run_async(pre_run=start, set_exception_handler=False)
            await task
        finally:
            owner.closed = True
            bind_terminal(previous)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        asyncio.run(run())


def test_terminal_fallback_appends_without_replaying_history(monkeypatch):
    output = RecordingOutput()
    plain = Writer()
    async def run():
        owner = Terminal()
        previous = bind_terminal(owner)
        service_cross_thread_callbacks()
        try:
            emit_public('before fallback')
            assert await flush_public(timeout=1)
            owner.fallback = True
            monkeypatch.setattr('sys.stdout', plain)
            emit_public('after fallback')
            assert await flush_public(timeout=1)
            assert plain.getvalue() == 'after fallback\n'
            assert 'before fallback' in output.writer.getvalue()
        finally:
            owner.closed = True
            bind_terminal(previous)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        asyncio.run(run())

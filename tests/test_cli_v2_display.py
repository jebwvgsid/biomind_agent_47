"""Synthetic V2 display checks; no provider, state loader or business entry."""
import asyncio
import io
import time

import pytest

from agent_core.cli_display import DisplayModel, bind_terminal, emit, emit_public, flush_public
from agent_core.cli_terminal import PlainTerminal, run_terminal


def service_writer_callbacks(loop):
    # This sandbox can defer selector wakeups from an output thread. Service
    # genuine callbacks with a test-only timer; receipts remain actual writes.
    def pulse():
        if not loop.is_closed():
            loop.call_later(.001, pulse)
    pulse()


@pytest.mark.parametrize('verbosity', ['compact', 'verbose'])
def test_v2_explicit_telemetry_routing_keeps_public_and_errors(verbosity):
    model = DisplayModel(verbosity=verbosity)
    for index in range(30):
        model.receive('message', f'planner chars={index}', purpose='telemetry')
        model.receive('message', f'compile elapsed={index}', source='diagnostic')
    model.receive('model_public', 'Planner 已返回不同的输入定义。', source='model_public')
    model.receive('model_public', 'A long public paragraph. ' * 30, source='model_public')
    model.receive('error', 'Planner timeout: exact cause unavailable', purpose='telemetry')
    model.receive('cancel', '取消尚未确认', purpose='telemetry')
    model.receive('approval', '批准前需查看完整脚本', purpose='telemetry')
    history = '\n'.join(ticket.text for ticket in model.items if ticket.presentation == 'history')
    assert ('planner chars=29' in history) == (verbosity == 'verbose')
    assert ('compile elapsed=29' in history) == (verbosity == 'verbose')
    assert 'Planner 已返回不同的输入定义。' in history
    assert 'A long public paragraph. ' * 30 in history
    assert 'Planner timeout: exact cause unavailable' in history
    assert '取消尚未确认' in history and '批准前需查看完整脚本' in history
    assert any('compile elapsed=29' in detail for detail in model.details)


@pytest.mark.parametrize('verbosity', ['compact', 'verbose'])
def test_v2_plain_owner_uses_same_selection_and_real_public_flush(verbosity):
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer = io.StringIO()
        owner = PlainTerminal(output=writer, verbosity=verbosity)
        previous = bind_terminal(owner)
        try:
            emit('message', 'hidden metric=41', purpose='telemetry')
            emit('details', 'full approval script', purpose='approval_detail')
            emit('activity', 'provider currently waiting', purpose='status')
            ticket = emit_public('已读取合成输入；接下来读取另一项。', task_id='fixture', call_id='one')
            assert await flush_public(timeout=.05)
            dispatch = time.monotonic()
            emit('error', 'fixture known timeout')
            assert await owner.drain(timeout=1)
            text = writer.getvalue()
            assert ('hidden metric=41' in text) == (verbosity == 'verbose')
            assert 'full approval script' not in text and 'provider currently waiting' not in text
            assert '已读取合成输入；接下来读取另一项。' in text
            assert 'fixture known timeout' in text
            assert ticket.times['ready'] <= ticket.times['enqueued'] <= ticket.times['write'] <= ticket.times['flush'] <= dispatch
            assert any('full approval script' == detail for detail in owner.model.details)
        finally:
            bind_terminal(previous)
            owner.close()
    asyncio.run(scenario())


def test_v2_details_and_verbose_redact_only_synthetic_secrets():
    model = DisplayModel(verbosity='verbose')
    model.receive('message', '\x1b[31mapi_key=synthetic-secret\x1b[0m', purpose='telemetry')
    assert model.history == ['api_key=[REDACTED]']
    assert list(model.details) == ['api_key=[REDACTED]']
    model.receive('message', 'Bearer synthetic-token', purpose='details')
    assert model.details[-1] == 'Bearer [REDACTED]'
    assert len(model.history) == 1


@pytest.mark.parametrize('mode,verbosity,shown', [('plain', 'compact', False), ('plain', 'verbose', True), ('off', 'verbose', False)])
def test_v2_actual_entry_selects_verbosity_without_changing_runtime(monkeypatch, mode, verbosity, shown):
    import agent_core.cli_terminal as terminal_module
    writer = io.StringIO()
    monkeypatch.setattr(terminal_module.sys, 'stdout', writer)
    monkeypatch.setenv('BIOMIND_UI', mode)
    monkeypatch.setenv('BIOMIND_CLI_VERBOSITY', verbosity)
    calls = []

    async def runtime():
        service_writer_callbacks(asyncio.get_running_loop())
        calls.append('original-runtime')
        emit('message', 'synthetic telemetry', purpose='telemetry')
        emit_public('公开说明')
        await flush_public()

    asyncio.run(run_terminal(runtime))
    assert calls == ['original-runtime']
    assert ('synthetic telemetry' in writer.getvalue()) == shown
    assert ('公开说明' in writer.getvalue()) == (mode != 'off')

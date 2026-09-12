"""Synthetic V2 display checks; no provider, state loader or business entry."""
import asyncio
import io
import logging
import time
from unittest.mock import patch

import pytest

from agent_core.cli_display import DisplayModel, bind_terminal, emit, emit_public, flush_public, safe_document
from agent_core.cli_terminal import PlainTerminal, Terminal, run_terminal


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


def completed_tool(model, identity, category='read', status='returned', **extra):
    scope = {'task_id': 'synthetic-task', 'node': 'Consultant', 'call_id': 'turn-one',
             'tool_call_id': identity, 'tool_category': category, **extra}
    model.receive('tool_start', identity + ' dispatched', **scope)
    return model.receive('tool_end', identity + ' actual return', status=status, **scope)


def visible(model):
    return [item.text for item in model.items if item.presentation == 'history']


def test_v2_groups_count_receipts_not_targets_and_stop_at_public():
    model = DisplayModel()
    completed_tool(model, 'read-one')
    completed_tool(model, 'read-two')
    completed_tool(model, 'search-one', category='search', status='empty')
    assert visible(model) == []
    assert model.active == '读取 2 项 · 空结果 1 次'
    assert model.receive('tool_end', 'duplicate with different text', task_id='synthetic-task',
        node='Consultant', call_id='turn-one', tool_call_id='search-one', tool_category='search', status='empty') is None
    model.receive('model_public', '这次没有匹配结果；下一步读取说明。', source='model_public')
    assert visible(model) == ['读取 2 项 · 空结果 1 次', '这次没有匹配结果；下一步读取说明。']
    completed_tool(model, 'read-three')
    model.finalize_tools()
    assert visible(model)[-1] == '读取 1 项'
    assert len([item for item in model.items if item.kind == 'tool_end']) == 4


def test_v2_scopeless_diagnostics_do_not_retire_current_model_tool_round():
    model = DisplayModel()
    scope = dict(task_id='synthetic-task', node='Consultant', call_id='turn-one')
    model.receive('model_public', '读取已准备。', source='model_public', **scope)
    current = model.active_scope
    for number in range(30):
        # Production Planner counters carry task/node without invocation IDs.
        model.receive('message', f'chars={number}', task_id='synthetic-task', node='Consultant', purpose='telemetry')
        model.receive('details', f'compile={number}', task_id='synthetic-task', node='Consultant', source='diagnostic')
    assert model.active_scope == current
    for identity in ['first', 'second', 'third']:
        completed_tool(model, identity)
    model.receive('model_public', '三个真实读取已返回。', source='model_public', **scope)
    assert visible(model) == ['读取已准备。', '读取 3 项', '三个真实读取已返回。']
    model.receive('approval', '原审批等待', **scope)
    model.receive('activity', 'missing-task late provider', node='Consultant', call_id='unseen-old-attempt')
    assert model.priority == 'approval' and model.active == '原审批等待'


@pytest.mark.parametrize('status', ['failed', 'unknown', 'partial', 'cancelled', 'not_dispatched'])
def test_v2_unsuccessful_receipt_is_visible_and_never_counted(status):
    model = DisplayModel()
    completed_tool(model, 'good')
    completed_tool(model, 'problem', status=status)
    completed_tool(model, 'later')
    model.finalize_tools()
    assert visible(model) == ['读取 1 项', 'problem actual return', '读取 1 项']


def test_v2_old_tool_return_keeps_receipt_without_replacing_approval():
    model = DisplayModel()
    completed_tool(model, 'old')
    model.receive('approval', '批准对象仍等待决定', task_id='synthetic-task', node='Consultant', call_id='turn-one')
    active = model.active
    completed_tool(model, 'late')
    assert model.active == active and model.priority == 'approval'
    assert visible(model) == ['读取 1 项', '批准对象仍等待决定', 'late actual return']
    model.release_priority()
    model.receive('tool_end', 'unknown receipt', status='unknown', task_id='synthetic-task', tool_call_id='pending')
    model.receive('tool_end', 'later real return', status='returned', task_id='synthetic-task', tool_call_id='pending')
    assert 'unknown receipt' in visible(model) and 'later real return' in visible(model)


def test_v2_process_group_only_asserts_exit_and_keeps_failure():
    model = DisplayModel()
    completed_tool(model, 'process-one', category='process', returncode=0)
    model.receive('result', '正式核验未通过：fixture effect missing')
    completed_tool(model, 'process-two', category='process', returncode=2)
    assert visible(model) == ['运行 1 个脚本 · 进程已退出，正式核验中',
                             '正式核验未通过：fixture effect missing', 'process-two actual return']
    assert all('成功' not in line for line in visible(model))
    completed_tool(model, 'process-not-started', category='process', returncode=0, command_started=False)
    model.finalize_tools()
    assert visible(model)[-1] == 'process-not-started actual return'


def test_v2_plain_final_group_and_long_review_are_available():
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer = io.StringIO()
        owner = PlainTerminal(output=writer)
        for number in range(3):
            scope = dict(task_id='fixture', node='Planner', tool_call_id=str(number), tool_category='read')
            owner.receive('tool_start', 'read start', **scope)
            owner.receive('tool_end', 'read returned', status='returned', **scope)
        assert await owner.drain(timeout=1)
        assert writer.getvalue() == '读取 3 项\n'
        body = '完整候选行\n' * 12000 + 'FINAL_CANDIDATE_SENTINEL\n'
        owner.show_details(text=body, title='完整代码')
        assert await owner.drain(timeout=1)
        assert 'FINAL_CANDIDATE_SENTINEL' in writer.getvalue()
        assert writer.getvalue().count('完整候选行') == 12000
        owner.close()
    asyncio.run(scenario())


def test_v2_long_documents_and_narrow_chinese_columns():
    from prompt_toolkit.utils import get_cwidth
    text = '中文长路径/资源/' * 100
    for width in [0, 1, 2, 5, 20, 80, 120]:
        assert get_cwidth(Terminal.fit_columns(text, width)) <= width
    document = '普通中文行\n' * 15000 + 'api_key=synthetic-secret\n最后一行\n'
    shown = safe_document(document)
    assert shown.endswith('api_key=[REDACTED]\n最后一行\n')
    assert shown.count('普通中文行') == 15000


def test_v2_actual_terminal_modal_keeps_pending_and_draft_until_close():
    from concurrent.futures import Future
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            owner = Terminal()
            owner.set_context('fixture', 'code_approval', {'body': 'exact candidate'})
            owner.pending = Future()
            pending = owner.pending
            owner.buffer.text = '未提交的中文草稿\n第二行'
            body = '完整审批代码行\n' * 12000 + 'END_OF_APPROVAL\n'
            await asyncio.to_thread(owner.show_details, body, '完整候选')
            await asyncio.sleep(.01)
            assert owner.modal and owner.details_view.text.endswith('END_OF_APPROVAL\n')
            owner.details_view.buffer.cursor_position = 123
            ticket = owner.receive('model_public', '浏览期间产生的新说明', source='model_public')
            await asyncio.sleep(.01)
            assert owner.history_cursor == 0 and owner.history_flush is None
            assert owner.details_view.buffer.cursor_position == 123
            assert not ticket.confirmation.done()
            owner.submit()
            assert owner.pending is pending and not pending.done()
            assert owner.buffer.text == '未提交的中文草稿\n第二行'
            owner.close_details()
            await asyncio.sleep(.03)
            assert not owner.modal and owner.pending is pending and not pending.done()
            assert owner.buffer.text == '未提交的中文草稿\n第二行'
            if owner.history_flush is not None:
                await owner.history_flush
            assert ticket.confirmation.done()
            for number in range(150):
                owner.receive('message', f'telemetry-{number}', purpose='telemetry')
            owner.buffer.text = '/details'
            owner.submit()
            assert owner.modal and owner.pending is pending and not pending.done()
            assert owner.buffer.text == ''  # consumed UI command, never backend feedback
            assert 'END_OF_APPROVAL' in owner.details_view.text
            assert owner.details_view.text.count('完整审批代码行') == 12000
            owner.close_details()
            owner.closed = True
    asyncio.run(scenario())


def test_v2_group_boundary_still_flushes_public_before_next_real_dispatch():
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer = io.StringIO()
        owner = PlainTerminal(output=writer)
        previous = bind_terminal(owner)
        try:
            for number in range(2):
                scope = dict(task_id='fixture', node='Planner', tool_call_id=str(number), tool_category='read')
                emit('tool_start', 'read start', **scope)
                emit('tool_end', 'read returned', status='returned', **scope)
            ticket = emit_public('两次读取已返回；接下来搜索。', task_id='fixture', node='Planner')
            assert await flush_public()
            actual_dispatch = time.monotonic()
            assert writer.getvalue() == '读取 2 项\n两次读取已返回；接下来搜索。\n'
            assert ticket.times['flush'] <= actual_dispatch
        finally:
            owner.close()
            bind_terminal(previous)
    asyncio.run(scenario())


def test_v2_twenty_column_approval_keeps_full_prompt_in_details():
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.data_structures import Size

    class NarrowOutput(DummyOutput):
        def get_size(self):
            return Size(rows=24, columns=20)

    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=NarrowOutput()):
            owner = Terminal()
            owner.set_context('fixture', 'code_review', {'content': 'original candidate'})
            owner.show_details(text='完整候选首行\nprint("fixture")\n', title='代码审批')
            prompt = '[y/Enter=run, n=reject, q=quit, e:<code>=edit, text=feedback；中文完整选择] > '
            application = asyncio.create_task(owner.app.run_async())
            response = asyncio.create_task(asyncio.to_thread(owner.read, prompt, 1))
            for _ in range(100):
                if owner.pending is not None:
                    break
                await asyncio.sleep(.001)
            pending = owner.pending
            assert pending is not None and not pending.done()
            assert owner.details_view.text.index('完整候选首行') < owner.details_view.text.index(prompt)
            assert prompt in owner.details_view.text
            owner.close_details()
            owner.show_details()
            assert prompt in owner.details_view.text and not pending.done()
            owner.close_details()
            for _ in range(200):
                if owner.input_ready:
                    break
                await asyncio.sleep(.001)
            assert owner.input_ready  # real Application frame, before accepting a decision
            owner._resolve('n')
            assert await response == 'n'
            owner.closed = True
            owner.app.exit()
            await application
    asyncio.run(scenario())


def test_v2_logs_lists_synthetic_handler_path_without_reading_body(tmp_path):
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def scenario():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            owner = Terminal(verbosity='verbose')
            path = tmp_path / 'unclassified-diagnostic.log'
            handler = logging.FileHandler(path, delay=True)
            owner.receive('details', 'classified synthetic diagnostic', purpose='details')
            owner.buffer.text = '/logs'
            try:
                with patch.object(logging.getLogger(), 'handlers', [handler]), patch('builtins.open',
                        side_effect=AssertionError('The display must not open raw diagnostics')) as opened:
                    owner.submit()
                    assert opened.call_count == 0
                assert owner.modal and str(path) in owner.details_view.text
                assert 'classified synthetic diagnostic' in owner.details_view.text
                assert owner.pending is None
                assert handler.stream is None
            finally:
                owner.closed = True
                handler.close()
    asyncio.run(scenario())


def test_v2_actual_output_attributes_reset_before_following_public():
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.styles import DEFAULT_ATTRS

    class AttributesOutput(DummyOutput):
        def __init__(self):
            self.current = DEFAULT_ATTRS
            self.writes = []
            self.resets = 0

        def set_attributes(self, attrs, color_depth):
            self.current = attrs

        def reset_attributes(self):
            self.current = DEFAULT_ATTRS
            self.resets += 1

        def write(self, text):
            self.writes.append((text, self.current))

    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        output = AttributesOutput()
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            owner = Terminal()
            owner.model.receive('tool_end', 'read result', status='returned')
            owner.model.receive('tool_end', 'actual failure', status='failed')
            owner.model.receive('approval', 'approval required')
            owner.model.receive('cancel', 'cancel requested')
            public = owner.model.receive('model_public', 'ordinary public paragraph', source='model_public')
            await owner._flush_history()
            by_text = dict(output.writes)
            assert by_text['read result\n'].color == 'ansicyan'
            assert by_text['read result\n'].dim is False
            assert by_text['✓ '].color == 'ansigreen'
            assert by_text['× '].color == 'ansired'
            assert by_text['actual failure\n'] == DEFAULT_ATTRS
            assert by_text['approval required\n'].color == 'ansiyellow'
            assert by_text['approval required\n'].bold is True
            assert by_text['! '].color == 'ansiyellow'
            assert by_text['cancel requested\n'] == DEFAULT_ATTRS
            assert by_text['ordinary public paragraph\n'] == DEFAULT_ATTRS
            assert output.current == DEFAULT_ATTRS and output.resets >= 5
            assert public.confirmation.result() is True
            owner.fallback = True
            writer = io.StringIO()
            owner._write_item(writer, public)
            assert writer.getvalue() == 'ordinary public paragraph\n'
            owner.closed = True
    asyncio.run(scenario())

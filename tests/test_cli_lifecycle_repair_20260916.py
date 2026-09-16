"""Real CLI owners, runtime sink and application writer; synthetic events only."""
import asyncio
from copy import deepcopy
import io
import json
import os
from pathlib import Path

import pytest

from agent_core.cli_display import emit, emit_public, flush_public, terminal
from agent_core.cli_terminal import run_terminal
from tests.test_cli_model_display import RecordingOutput
from tests.test_cli_v2_display import service_writer_callbacks


@pytest.mark.parametrize('mode', ['plain', 'tty'])
def test_recorded_session_events_replay_through_actual_sink(tmp_path, mode):
    import gzip
    import bio_agents
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_plain import PlainTerminal
    from tests.test_cli_generate_public_integration import run_actual_tty
    saved = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/cli_recovery_history_1789537621.json.gz').read_bytes()))
    rows = saved['events']; owners = []
    agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.data_path = str(tmp_path)  # Only a new display-event journal, no Plan.
    async def replay(owner):
        owners.append(owner)
        for repetition in range(2):
            for row in rows:
                agent._schema2_recovery_event_sink(row['event_kind'],
                    {**deepcopy(row['payload']), 'event_id': row['event_id']})
            await flush_public(timeout=1)
            if repetition == 0:
                before = (len(owner.model.items), len(owner.model.activities))
            else:
                assert (len(owner.model.items), len(owner.model.activities)) == before
        for activity in owner.model.activities.values():
            events = [r['kind'] for r in activity['events']]
            assert events[0] == 'tool_start' and 'tool_end' in events
        return before
    writer = io.StringIO()
    async def run():
        if mode == 'tty':
            result, owner, output = await run_actual_tty(replay, completion_budget=10)
            return output.text.getvalue()
        owner = PlainTerminal(writer); previous = bind_terminal(owner)
        try:
            await replay(owner); await owner.drain(timeout=2)
            return writer.getvalue()
        finally:
            owner.close(); bind_terminal(previous)
    transcript = asyncio.run(run()); owner = owners[0]
    starts = {('recovery:' + str(r['payload'].get('seed_id') or '') + ':' + str(r['payload'].get('context_epoch_id') or '') + ':' + r['payload']['tool_request_id'])
        for r in rows if r['event_kind'] == 'recovery_tool_started'}
    assert len(owner.model.activities) == len(starts) == 34
    assert 'ARTIFACT_LOCATOR_MISSING' in transcript
    assert not (tmp_path / 'task_outputs' / saved['session'] / 'plan_state.json').exists()
    evidence = {'mode': mode, 'source_sha256': saved['source_sha256'], 'saved_events': len(rows),
        'replay_passes': 2, 'unique_tool_activities': len(starts), 'public_messages':
        [{'text': t.text, 'scope': t.scope} for t in owner.model.items if t.kind == 'model_public'],
        'transcript': transcript, 'model_calls': 0, 'tool_dispatches': 0, 'task_restored': False}
    (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / f'historical-replay-{mode}.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2))


FACTS = {'stop_id': 'stop-current', 'origin': {'error_code': 'DELIVERY_CONTRACT_UNMET'},
         'stop_reason': {'error_code': 'RECOVERY_NO_PROGRESS'},
         'prior_completed_work': [{'step_id': str(i), 'commit_id': 'commit-' + str(i)} for i in range(6)],
         'inspection_failures': [{'error_code': 'ARTIFACT_LOCATOR_MISSING'}],
         'recovery_usage': {'scope': 'last_admitted_episode', 'turn_count': 10, 'tool_call_count': 11},
         'summary_llm_calls': 0, 'required_output_status': 'not_delivered'}


def activity_events():
    scope = {'task_id': 'task', 'node': 'execute', 'execution_attempt_id': 'attempt-one',
             'activity_id': 'execution:attempt-one'}
    return [('tool_start', '开始执行 · /tmp/中文 路径/检查.sh', {**scope, 'event_id': 'start'}),
        ('tool_tail', 'login greeting\nprivate protocol JSON ownership inode ' + 'a' * 64,
         {**scope, 'event_id': 'tail', 'stream': 'stdout'}),
        ('tool_end', '命令 rc=0；等待正式核验', {**scope, 'event_id': 'exit', 'status': 'returned'}),
        ('activity_check', '局部检查 VALID；尚未提交', {**scope, 'event_id': 'local', 'status': 'returned'}),
        ('activity_check', '正式核验 correction_required；Commit 尚未确认',
         {**scope, 'event_id': 'formal', 'status': 'failed'})]


@pytest.mark.parametrize('mode', ['plain', 'tty'])
@pytest.mark.parametrize('changed', [False, True])
def test_real_entry_messages_activity_replay_and_single_terminal(monkeypatch, mode, changed):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    import agent_core.cli_terminal as cli
    import bio_agents
    class Output(io.StringIO):
        def isatty(self): return mode == 'tty'
    writer, recording = Output(), RecordingOutput()
    monkeypatch.setattr(cli.sys, 'stdout', writer)
    monkeypatch.setattr(cli.sys, 'stdin', Output())
    monkeypatch.setenv('BIOMIND_UI', mode)
    monkeypatch.setenv('TERM', 'xterm-256color')
    owners, raw_requests = [], []
    async def runtime():
        owner = terminal(); owners.append(owner)
        text = '中文 <检查> /tmp/中文 路径\n' + 'long-command ' * 30
        final = text + ('完成回执修正' if changed else '')
        scope = {'task_id': 'task', 'node': 'recovery', 'call_id': 'call-one', 'source_message_id': 'message-one'}
        events = [('model_delta', text[:12], {**scope, 'event_id': 'd1', 'source': 'model_public'}),
                  ('model_delta', text[12:], {**scope, 'event_id': 'd2', 'source': 'model_public'})]
        for kind, body, data in events:
            emit(kind, body, **data)
        # Replay a delta by event identity before completion.
        emit(*events[0][:2], **events[0][2])
        # A tool event interleaves before completed; the public position is kept.
        start = activity_events()[0]
        emit(*start[:2], **start[2])
        emit_public(final, **scope); await flush_public(timeout=1)
        for event in activity_events()[1:3]:
            emit(*event[:2], **event[2])
        # rc0 really reaches the writer before the formal failure is available.
        if mode == 'plain': await owner.drain(timeout=1)
        else: await asyncio.sleep(.05)
        for event in activity_events()[3:]:
            emit(*event[:2], **event[2])
        for event in [*events, ('model_public', final, {**scope, 'source': 'model_public'}), *activity_events()]:
            emit(*event[:2], **event[2])
        emit_public(final, **{**scope, 'call_id': 'call-two'})
        await flush_public(timeout=1)
        for i in range(6):
            emit('step_committed', f'步骤 {i} 完成 · Commit 已确认', task_id='task', commit_id='commit-' + str(i))
        router = bio_agents.OutputRouter()
        def result(facts):
            return bio_agents.InteractiveRunResult(task_id='task', status='blocked', stage='recovery',
                reason='unresolved delivery', failure_code='RECOVERY_NO_PROGRESS', display_facts=facts)
        router.render_terminal_result(result(deepcopy(FACTS)))
        router.render_terminal_result(result(deepcopy(FACTS)))
        late = deepcopy(FACTS)
        late.update(report_receipts={'report.md': {'status': 'failed', 'reason': 'RUNTIME_REPORT_EXISTING_FILE_PROTECTED'}},
                    internal_diagnostic_delivery={'status': 'written'})
        router.render_terminal_result(result(late)); router.render_terminal_result(result(late))
        raw_requests.append(deepcopy(activity_events()))
        assert owner.model.priority == 'terminal' and owner.model.run_state == 'blocked'
        emit('activity', 'late provider heartbeat', task_id='task', node='recovery', call_id='call-two')
        assert owner.model.active == '任务已停止'
    async def run():
        service_writer_callbacks(asyncio.get_running_loop())
        await run_terminal(runtime)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=recording):
        asyncio.run(run())
    owner = owners[0]
    model = owner.model
    assert model.run_state == 'closed' and not model.active and not model.tool_tail
    if mode == 'tty':
        assert owner.prompt_text == '' and owner.app.erase_when_done
    assert len(model.activities) == 1
    activity = next(iter(model.activities.values()))
    assert activity['ticket'].text.startswith('正式核验') and len(activity['events']) == 5
    assert [r['kind'] for r in activity['events']][:3] == ['tool_start', 'tool_tail', 'tool_end']
    public = [r.text for r in model.items if r.kind == 'model_public']
    assert len(public) == 2 and public[0] == public[1] and '<检查>' in public[0]
    assert len(model.terminals) == 1 and len(model.completed_steps) == 6
    assert sum(r.kind == 'result' for r in model.items) == 1
    assert sum(r.kind == 'terminal_delivery_update' for r in model.items) == 1
    transcript = writer.getvalue() if mode == 'plain' else recording.writer.getvalue()
    assert transcript.count('任务已停止 · blocked') == 1
    assert '已有 6 步正式提交' in transcript and '10 次模型请求，11 次工具调用' in transcript
    assert '系统事实摘要；模型总结未运行' in transcript
    assert '正式核验 correction_required' in transcript
    assert '目标文件受保护，未覆盖' in transcript and '内部诊断归档=written' in transcript
    assert 'ownership' not in transcript and 'inode' not in transcript
    assert any('ownership' in r for r in model.details)
    assert bool(any('流式预览与完成正文不同' in r for r in model.details)) is changed
    assert raw_requests == [activity_events()]
    out = Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / f'lifecycle-{mode}-{changed}.json'
    out.write_text(json.dumps({'entry': 'run_terminal', 'mode': mode, 'provider_calls': 0,
        'transcript': transcript, 'canonical_history': model.history, 'activity_count': 1,
        'terminal_count': 1, 'closed': model.run_state}, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('result,error,expected', [
    ({'start_line': 1, 'end_line': 9, 'truncated': True, 'cursor': {'next_start_line': 10}}, None, '分页；可从第 10 行继续'),
    ({'truncated': True}, None, '续读位置未提供'),
    ({'status': 'REQUIRES_NARROWING'}, None, '查询范围过大'),
    ({}, {'code': 'ARTIFACT_PERMISSION_DENIED'}, '读取失败 ARTIFACT_PERMISSION_DENIED'),
    ({'status': 'NO_MATCH'}, None, '未找到匹配项')])
def test_production_recovery_sink_reports_actual_read_receipt(tmp_path, result, error, expected):
    import bio_agents
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_plain import PlainTerminal
    async def run():
        writer = io.StringIO(); owner = PlainTerminal(writer)
        old = bind_terminal(owner)
        try:
            agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent); agent.data_path = str(tmp_path)
            payload = {'task_id': 'task', 'seed_id': 'seed', 'tool_request_id': 'read-one', 'tool': 'artifact.read',
                'event_id': 'observation', 'observation': {'source_ref': 'artifact:summary', 'result': result, 'error': error}}
            agent._schema2_recovery_event_sink('recovery_tool_observation_committed', payload)
            await owner.drain(timeout=1)
            text = writer.getvalue(); assert expected in text and 'artifact:summary' in text
            if 'start_line' not in result: assert '返回第' not in text
            for index in range(105): owner.receive('details', 'later telemetry ' + str(index))
            # Use the production details entry after the short diagnostic ring
            # has rotated; the original observation still belongs to its call.
            owner.show_details()
            await owner.drain(timeout=1)
            assert 'artifact:summary' in writer.getvalue()[len(text):]
        finally:
            owner.close(); bind_terminal(old)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['plain', 'tty'])
def test_real_outer_stop_receipts_and_protected_report_reach_cli(tmp_path, monkeypatch, mode):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    import agent_core.cli_terminal as cli
    from tests.test_common_finalization_workflow import run_execution_case
    from agent_core.workflow.managed_stop import current_managed_stop
    from bio_agents import OutputRouter
    class Output(io.StringIO):
        def isatty(self): return mode == 'tty'
    writer, recording, owners = Output(), RecordingOutput(), []
    monkeypatch.setattr(cli.sys, 'stdout', writer)
    monkeypatch.setattr(cli.sys, 'stdin', Output())
    monkeypatch.setenv('BIOMIND_UI', mode)
    monkeypatch.setenv('TERM', 'xterm-256color')
    protected = []
    def inject(_agent, fixture):
        path = tmp_path / 'task_outputs' / fixture.task_id / 'report.md'
        path.write_text('Existing unowned report; must remain unchanged.\n')
        protected.append(path)
    async def runtime():
        owner = terminal(); owners.append(owner)
        f, a, graph, config, recovery, summary, result, _ = await run_execution_case(
            tmp_path, summary_policy={'enabled': False}, inject=inject)
        stop = current_managed_stop(f.reload())
        assert len(recovery.requests) == 3 and not summary.requests
        assert stop['summary_llm_calls'] == 0 and stop['summary_skip_reason'] == 'CONFIG_DISABLED'
        assert stop['report_receipts']['report.md']['reason'] == 'RUNTIME_REPORT_EXISTING_FILE_PROTECTED'
        assert stop['internal_diagnostic_delivery']['status'] == 'complete'
        assert protected[0].read_text() == 'Existing unowned report; must remain unchanged.\n'
        router = OutputRouter()
        router.render_terminal_result(result); router.render_terminal_result(result)
        await a._finalize_managed_terminal(f.task_id, graph.get_state(config))
        if mode == 'plain': await owner.drain(timeout=1)
        else: await asyncio.sleep(.02)
        assert len(owner.model.terminals) == 1 and not summary.requests
        # A separate stop with no counters must not invent a zero total.
        from dataclasses import replace
        unknown = replace(result, display_facts={'stop_id': 'separate-no-counters', 'summary_llm_calls': 0})
        router.render_terminal_result(unknown)
        (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / ('outer-stop-' + mode + '.json')).write_text(
            json.dumps({'entry': 'run_terminal -> _run_agent_interrupt_loop -> managed_stop -> OutputRouter',
                'stop': stop, 'summary_calls': len(summary.requests), 'recovery_calls': len(recovery.requests),
                'protected_report_preserved': True}, ensure_ascii=False, indent=2))
    async def run():
        service_writer_callbacks(asyncio.get_running_loop())
        await run_terminal(runtime)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=recording):
        asyncio.run(run())
    text = writer.getvalue() if mode == 'plain' else recording.writer.getvalue()
    assert text.count('任务已停止 · blocked') == 2  # two distinct stop identities
    assert '系统事实摘要；模型总结未运行' in text
    assert '目标文件受保护，未覆盖' in text and '内部诊断归档=complete' in text
    assert '调查累计次数：未知' in text and '0 次模型请求' not in text
    assert not owners[0].model.active and owners[0].model.run_state == 'closed'


@pytest.mark.parametrize('raw', ['/exit', '/exit\n', '/exit\r', '/exit\r\n', ' \t/exit \r\n', '\n /exit\n\n',
                                 '/quit\r\n', 'q\n', 'QUIT\n', 'exit\n'])
def test_actual_interactive_exit_forms_make_zero_model_calls(raw, monkeypatch):
    import bio_agents
    from unittest.mock import AsyncMock
    agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent._aprompt_user = AsyncMock(return_value=raw)
    agent._run_agent_interrupt_loop = AsyncMock(side_effect=AssertionError('exit cannot dispatch a task'))
    agent._ainvoke_node_llm = AsyncMock(side_effect=AssertionError('exit cannot call a model'))
    agent._ainvoke_llm = AsyncMock(side_effect=AssertionError('exit cannot call a provider'))
    asyncio.run(agent.interactive_mode())
    assert agent._aprompt_user.await_count == 1
    assert agent._run_agent_interrupt_loop.await_count == agent._ainvoke_llm.await_count == agent._ainvoke_node_llm.await_count == 0
    assert agent._last_cli_control_receipt['task_dispatched'] is False
    import hashlib
    name = hashlib.sha256(raw.encode()).hexdigest()[:12]
    (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / ('exit-' + name + '.json')).write_text(json.dumps({
        'entry': 'BioAnalystAgent.interactive_mode', 'raw': raw, 'model_calls': 0, 'graph_dispatches': 0,
        'receipt': agent._last_cli_control_receipt}, ensure_ascii=False, indent=2))

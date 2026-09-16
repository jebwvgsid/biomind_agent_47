"""Recorded-fact display through real CLI entries; no historical task replay."""
import asyncio
from copy import deepcopy
import io
import json
import os
from pathlib import Path

import pytest

from agent_core.cli_display import terminal
from agent_core.cli_terminal import run_terminal
from tests.test_cli_model_display import RecordingOutput
from tests.test_cli_v2_display import service_writer_callbacks


ISSUE = {'actual': ['ann_summary', 'output.ann_summary'],
         'code': 'EFFECT_FACT_IDENTITIES_MISMATCH', 'expected': ['ann_summary'],
         'field_path': '$.facts.produces_artifacts'}


def stopped_facts():
    # Minimal field-shaped fixture, copied from the saved exception structure.
    # No source paths, scripts, scientific inputs or original task state loaded.
    return {
        'stop_id': 'stop-synthetic',
        'origin': {'error_code': 'EXECUTION_FAILED', 'attempt_id': 'attempt-current', 'returncode': 1},
        'execution': {'attempt_id': 'attempt-current', 'step_id': 'summarize_ann',
                      'returncode': 1, 'executed': True, 'committed': False,
                      'stderr': 'Traceback (most recent call last):\nValueError: ' + json.dumps(ISSUE) + '\n'},
        'stages': [{'step_id': 'annotate_variants', 'title': 'VCF 注释', 'status': 'passed'},
                   {'step_id': 'summarize_ann', 'title': 'ANN 统计', 'status': 'running'}],
        'prior_completed_work': [{'step_id': 'annotate_variants', 'commit_id': 'commit-annotation', 'committed': True}],
        'stop_reason': {'error_code': 'RECOVERY_NO_PROGRESS_LIMIT', 'no_progress_rounds': 2},
        'recovery_usage': {'scope': 'current_episode', 'turn_count': 2, 'tool_call_count': 3},
        'inspection_failures': [{'error_code': 'ARTIFACT_LOCATOR_MISSING'}],
        'summary_llm_calls': 0, 'required_output_status': 'not_delivered',
    }


def actual_entry(monkeypatch, mode, runtime):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    import agent_core.cli_terminal as cli
    class Output(io.StringIO):
        def isatty(self):
            return mode == 'tty'
    writer, recording = Output(), RecordingOutput()
    monkeypatch.setattr(cli.sys, 'stdout', writer)
    monkeypatch.setattr(cli.sys, 'stdin', Output())
    monkeypatch.setenv('BIOMIND_UI', mode)
    monkeypatch.setenv('TERM', 'xterm-256color')
    async def run():
        service_writer_callbacks(asyncio.get_running_loop())
        await run_terminal(runtime)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=recording):
        asyncio.run(run())
    return writer.getvalue() if mode == 'plain' else recording.writer.getvalue()


@pytest.mark.parametrize('mode', ['plain', 'tty'])
@pytest.mark.parametrize('case', ['structured', 'unknown', 'wrong_attempt', 'mention_only',
                                  'truncated', 'same_identities', 'wrong_field', 'later_error'])
def test_stop_explanation_real_entry_and_replay(monkeypatch, mode, case):
    from bio_agents import InteractiveRunResult, OutputRouter
    facts = stopped_facts()
    if case == 'unknown':
        facts['execution']['stderr'] = 'RuntimeError: an unrelated failure\n'
    elif case == 'wrong_attempt':
        facts['execution']['attempt_id'] = 'attempt-other'
    elif case == 'mention_only':
        facts['execution']['stderr'] = 'checking EFFECT_FACT_IDENTITIES_MISMATCH in source\n'
    elif case == 'truncated':
        facts['execution']['stderr'] = 'ValueError: ' + json.dumps(ISSUE)[:-8]
    elif case == 'same_identities':
        facts['execution']['stderr'] = 'ValueError: ' + json.dumps({**ISSUE, 'actual': ['ann_summary']})
    elif case == 'wrong_field':
        facts['execution']['stderr'] = 'ValueError: ' + json.dumps({**ISSUE, 'field_path': '$.other'})
    elif case == 'later_error':
        facts['execution']['stderr'] += 'RuntimeError: failed for another reason\n'
    original = deepcopy(facts)
    owners = []
    async def runtime():
        owner = terminal(); owners.append(owner)
        router = OutputRouter()
        def result(value):
            return InteractiveRunResult(task_id='synthetic-task', status='blocked', stage='recovery',
                failure_code='RECOVERY_NO_PROGRESS_LIMIT', display_facts=value)
        router.render_terminal_result(result(facts))
        router.render_terminal_result(result(deepcopy(facts)))
        late = deepcopy(facts)
        late.update(report_receipts={'report.md': {'status': 'failed', 'reason': 'RUNTIME_REPORT_EXISTING_FILE_PROTECTED'}},
                    internal_diagnostic_delivery={'status': 'complete'})
        router.render_terminal_result(result(late))
        router.render_terminal_result(result(deepcopy(late)))
        if mode == 'plain':
            await owner.drain(timeout=1)
    transcript = actual_entry(monkeypatch, mode, runtime)
    model = owners[0].model
    assert facts == original
    assert transcript.count('任务已停止 · blocked') == 1
    assert '已提交步骤：VCF 注释' in transcript
    assert '当前步骤：ANN 统计（尚未正式提交）' in transcript
    assert '连续 2 轮未取得可记录进展' in transcript
    assert '未取得产物的可读位置（ARTIFACT_LOCATOR_MISSING）' in transcript
    assert '目标文件受保护，未覆盖' in transcript
    assert '系统事实摘要；模型总结未运行' in transcript
    assert len(model.terminals) == 1
    assert sum(row.kind == 'terminal_delivery_update' for row in model.items) == 1
    assert any('stderr' in detail for detail in model.details)
    if case == 'structured':
        assert '证据登记含未声明的标识 output.ann_summary' in transcript
        assert '声明标识：ann_summary' in transcript
        assert '下一步：修正当前步骤证据登记的标识' in transcript
    else:
        assert '具体原因尚未定位（EXECUTION_FAILED）' in transcript
        assert '证据登记含未声明的标识' not in transcript
    target = os.environ.get('BIOMIND_CLI_PROGRESS_ARTIFACTS')
    if target:
        (Path(target) / f'stop-explanation-{mode}-{case}.json').write_text(json.dumps({
            'mode': mode, 'case': case, 'entry': 'run_terminal -> OutputRouter',
            'transcript': transcript, 'terminal_count': len(model.terminals),
            'model_calls': 0, 'science_calls': 0, 'historical_task_loaded': False,
        }, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('mode', ['plain', 'tty'])
def test_no_progress_limit_real_finalization_skips_summary_and_replay(tmp_path, monkeypatch, mode):
    from bio_agents import OutputRouter
    from agent_core.workflow.managed_stop import current_managed_stop, projection
    from tests.test_common_finalization_workflow import run_execution_case
    owners = []
    async def runtime():
        owner = terminal(); owners.append(owner)
        fixture, agent, graph, config, recovery, summary, result, _ = await run_execution_case(
            tmp_path, summary_policy={'enabled': True})
        stop = current_managed_stop(fixture.reload())
        assert stop['stop_reason']['error_code'] == 'RECOVERY_NO_PROGRESS_LIMIT'
        assert stop['summary_skip_reason'] == 'RECOVERY_STOP_NO_MODEL'
        assert stop['summary_llm_calls'] == 0 and not summary.requests
        assert projection(stop)['terminal_diagnostic']['stages'] == stop['stages']
        router = OutputRouter()
        router.render_terminal_result(result)
        await agent._finalize_managed_terminal(fixture.task_id, graph.get_state(config))
        router.render_terminal_result(result)
        assert not summary.requests and len(recovery.requests) == 3
        assert not fixture.reload()['task_state'].get('typed_step_commit_receipts')
        if mode == 'plain':
            await owner.drain(timeout=1)
    transcript = actual_entry(monkeypatch, mode, runtime)
    assert '系统事实摘要；模型总结未运行' in transcript
    assert '自动纠错已停止（RECOVERY_NO_PROGRESS_LIMIT）' in transcript
    assert len(owners[0].model.terminals) == 1


@pytest.mark.parametrize('mode', ['plain', 'tty'])
@pytest.mark.parametrize('kind', ['diagnostic', 'formal', 'read_error'])
def test_diagnostic_read_limits_reach_real_tool_sink(tmp_path, monkeypatch, mode, kind):
    import bio_agents
    agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.data_path = str(tmp_path)
    result = {'path': '/synthetic/output.tsv', 'start_line': 1, 'end_line': 2}
    if kind != 'formal':
        result.update(diagnostic_only=True, source_uncertain=True)
    observation = {'source_ref': 'diagnostic:output', 'result': result,
                   'error': {'code': 'ARTIFACT_PERMISSION_DENIED'} if kind == 'read_error' else None}
    original = deepcopy(observation)
    owners = []
    async def runtime():
        owner = terminal(); owners.append(owner)
        payload = {'task_id': 'synthetic-task', 'seed_id': 'seed-synthetic',
                   'tool_request_id': 'diagnostic-read', 'tool': 'artifact.read',
                   'event_id': 'observation-one', 'observation': observation}
        for _ in range(2):
            agent._schema2_recovery_event_sink('recovery_tool_observation_committed', deepcopy(payload))
        if mode == 'plain':
            await owner.drain(timeout=1)
    transcript = actual_entry(monkeypatch, mode, runtime)
    assert observation == original and len(owners[0].model.activities) == 1
    if kind != 'formal':
        assert '仅诊断，文件来源尚未确认' in transcript
    else:
        assert '仅诊断' not in transcript and '来源尚未确认' not in transcript
    assert '正式产物已接纳' not in transcript
    if kind == 'read_error':
        assert '读取失败 ARTIFACT_PERMISSION_DENIED' in transcript
    else:
        assert '返回第 1–2 行' in transcript


@pytest.mark.parametrize('mode', ['plain', 'tty'])
@pytest.mark.parametrize('code,cause,next_step', [
    ('OUTPUT_TARGET_CHANGED_SINCE_REVIEW', '目标文件在审批后发生变化', '重新检查目标文件当前内容'),
    ('OUTPUT_REPLACEMENT_NOT_APPROVED', '替换现有输出文件尚未获得批准', '查看需要替换的文件和候选动作'),
    ('OUTPUT_BACKUP_OR_RECEIPT_FAILED', '旧输出备份或备份回执未确认成功', '查看备份与回执详情'),
    ('EXECUTION_FAILED', '具体原因尚未定位', '先查看当前步骤的执行与核验详情'),
])
def test_output_protection_reason_reaches_real_terminal(monkeypatch, mode, code, cause, next_step):
    from bio_agents import InteractiveRunResult, OutputRouter
    facts = stopped_facts()
    facts['origin'] = {'error_code': code,
                       'summary': 'Mention OUTPUT_REPLACEMENT_NOT_APPROVED; prose is not a typed reason.'}
    facts['execution']['stderr'] = ''
    facts['stop_reason'] = {'error_code': code}
    original = deepcopy(facts)
    async def runtime():
        owner = terminal()
        router = OutputRouter()
        result = InteractiveRunResult(task_id='synthetic-task', status='blocked',
            stage='execute', failure_code=code, display_facts=facts)
        router.render_terminal_result(result)
        router.render_terminal_result(result)
        if mode == 'plain':
            await owner.drain(timeout=1)
    transcript = actual_entry(monkeypatch, mode, runtime)
    assert facts == original and transcript.count('任务已停止 · blocked') == 1
    assert cause in transcript and '下一步：' + next_step in transcript
    if code == 'EXECUTION_FAILED':
        assert '替换现有输出文件尚未获得批准' not in transcript

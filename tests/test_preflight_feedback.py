"""O01–O08: actual executor, controlled tiny processes and new wrapper only.

Run solely through the allowlisted export and committed offline guard. No old
session, original dataset, model, API, or scientific command is used.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import shlex
import sys
import time

import pytest

from execution.preflight import run_checks, DEFAULT_ITEM_TIMEOUT_SECONDS
from execution.preflight_progress import PREFIX, parse_progress, without_progress
from execution.executor import run_command, get_last_execution_metadata
from agent_core.step_effects import emit_observation, extract_structured_effect_evidence
from agent_core.cli_display import bind_terminal, StreamDisplayCopy
from agent_core.cli_terminal import PlainTerminal
from tests.cli_model_progress_support import TimedWriter

ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / 'skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq_batch.py'


def tiny(root, label, body):
    path = root / (label + '.sh')
    path.write_text('#!/bin/bash\nset -e\n' + body + '\n')
    sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
    return {'label': label, 'argv': ['bash', str(path)], 'scope': 'synthetic_prefix'}


def artifact(name, value):
    (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str))


class Progress(io.StringIO):
    def __init__(self, event=None, display=None):
        super().__init__(); self.records = []; self.event = event; self.display = display

    def write(self, text):
        record = parse_progress(text)
        if record:
            self.records.append(record)
            if self.event and record['event'] == 'process_started':
                self.event.set()
        if self.display:
            self.display.feed(text.encode())
        return super().write(text)


def assess(row, raw_path):
    observed = extract_structured_effect_evidence(raw_path.read_text())['produces_observations']
    assert len(observed) == 1
    value = observed[0]['value']
    return {'passed': value['prefix_valid'] is True, 'value': value, 'scope': 'synthetic_prefix'}


@pytest.mark.parametrize('mode', ['compact', 'verbose', 'off'])
def test_four_real_checks_progress_visible_before_return_capture_stays_private(tmp_path, mode):
    async def run():
        release = tmp_path / 'release'
        started = asyncio.Event()
        writer = TimedWriter()
        owner = PlainTerminal(writer, enabled=mode != 'off', verbosity='verbose' if mode == 'verbose' else 'compact')
        previous = bind_terminal(owner)
        progress = Progress(started, StreamDisplayCopy('stderr', task_id='synthetic', action_id='preflight', execution_attempt_id='a1'))
        checks = []
        for index in range(4):
            evidence = emit_observation('child-' + str(index), {'prefix_valid': True, 'full_scientific_validation': False})
            body = ('while [ ! -f ' + shlex.quote(str(release)) + ' ]; do sleep 0.02; done\n' if index == 0 else '')
            body += "printf '%s\\n' " + shlex.quote(evidence)
            checks.append(tiny(tmp_path, 'S' + str(index), body))
        task = asyncio.create_task(run_checks(checks, evidence_dir=tmp_path / 'evidence',
            timeout_seconds=3, progress_stream=progress, env=ENV, assess=assess))
        try:
            await asyncio.wait_for(started.wait(), 3)
            assert not task.done()
            assert progress.records[-1]['event'] == 'process_started'
            assert progress.records[-1]['completed'] == 0
            assert 'S0' in owner.model.status()
            await owner.drain(timeout=1)
            if mode != 'off':
                assert '检查进程已启动 S0' in writer.getvalue()
            else:
                assert writer.getvalue() == ''
            release.touch()
            result = await task
            await owner.drain(timeout=1)
            assert result['returned'] == result['passed'] == 4
            assert [r['event'] for r in progress.records] == sum(
                [['item_started', 'process_started', 'item_returned'] for _ in range(4)], []) + ['summary']
            assert all(r['execution']['process_reaped'] for r in result['checks'])
            for row in result['checks']:
                raw = Path(row['raw']['stdout']).read_text()
                assert raw.count('BIOMIND_EFFECT_EVIDENCE_JSON=') == 1
                assert 'BIOMIND_EFFECT_EVIDENCE_JSON=' not in writer.getvalue()
            assert len([r for r in progress.records if r['event'] == 'process_started']) == 4
            artifact('preflight-four-' + mode + '.json', {'result': result, 'last_status': owner.model.status(),
                'display': writer.getvalue(), 'model_calls': 0, 'route': 'caller_existing_route',
                'source': 'run_checks → run_command → real shell processes; parent stderr → StreamDisplayCopy → production owner'})
        finally:
            release.touch()
            if not task.done(): task.cancel()
            bind_terminal(previous); owner.close()
    asyncio.run(run())


def test_local_timeout_keeps_partial_results_and_cleans_nested_child(tmp_path):
    async def run():
        child_pid = tmp_path / 'child.pid'
        first = tiny(tmp_path, 'first', 'printf done')
        blocked = tiny(tmp_path, 'blocked', "sleep 10 &\nkid=$!\nprintf %s \"$kid\" > " + shlex.quote(str(child_pid)) +
            "\ntrap 'kill \"$kid\" 2>/dev/null || true; wait \"$kid\" 2>/dev/null || true; exit 130' TERM INT\nwait \"$kid\"")
        last = tiny(tmp_path, 'never', 'exit 0')
        result = await run_checks([first, blocked, last], evidence_dir=tmp_path / 'evidence',
            timeout_seconds=0.2, progress_stream=Progress(), env=ENV)
        assert result['status'] == 'timeout' and result['returned'] == 1 and result['passed'] == 0
        assert result['unstarted'] == 1
        item = result['checks'][1]
        assert item['execution']['process_reaped'] is True
        assert item['execution']['group_reaped'] is True
        with pytest.raises(ProcessLookupError): os.kill(int(child_pid.read_text()), 0)
        assert Path(item['raw']['stdout']).exists() and Path(item['raw']['stderr']).exists()
        artifact('preflight-timeout.json', result)
    asyncio.run(run())


def test_cancel_current_check_reaps_nested_group_and_preserves_partial_raw(tmp_path):
    async def run():
        started = asyncio.Event(); progress = Progress(started)
        child_pid = tmp_path / 'child.pid'
        check = tiny(tmp_path, 'nested', "sleep 10 &\nkid=$!\nprintf %s \"$kid\" > " + shlex.quote(str(child_pid)) +
            "\ntrap 'kill \"$kid\" 2>/dev/null || true; wait \"$kid\" 2>/dev/null || true; exit 130' TERM INT\nprintf partial\\n\nwait \"$kid\"")
        task = asyncio.create_task(run_checks([check], evidence_dir=tmp_path / 'evidence',
            timeout_seconds=10, progress_stream=progress, env=ENV))
        await asyncio.wait_for(started.wait(), 3)
        deadline = time.monotonic() + 2
        while not child_pid.exists() and time.monotonic() < deadline: await asyncio.sleep(0.01)
        assert child_pid.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        result = json.loads((tmp_path / 'evidence/checks.json').read_text())
        assert result['status'] == 'cancelled' and result['passed'] == 0
        assert result['checks'][0]['execution']['process_reaped'] is True
        assert result['checks'][0]['execution']['group_reaped'] is True
        assert 'partial' in Path(result['checks'][0]['raw']['stdout']).read_text()
        with pytest.raises(ProcessLookupError): os.kill(int(child_pid.read_text()), 0)
        artifact('preflight-cancel.json', result)
    asyncio.run(run())


def test_large_simultaneous_streams_are_fully_archived_and_display_bounded(tmp_path):
    async def run():
        check = tiny(tmp_path, 'large', "(head -c 300000 /dev/zero | tr '\\0' x) &\n(head -c 350000 /dev/zero | tr '\\0' y >&2) &\nwait")
        progress = Progress()
        result = await run_checks([check], evidence_dir=tmp_path / 'evidence', timeout_seconds=5,
            progress_stream=progress, env=ENV)
        row = result['checks'][0]
        assert result['status'] == 'returned' and result['passed'] == 0
        assert Path(row['raw']['stdout']).read_bytes() == b'x' * 300000
        assert Path(row['raw']['stderr']).read_bytes() == b'y' * 350000
        assert len(progress.getvalue()) < 4096
        assert row['execution']['stdout_bytes'] == 300000
        assert row['execution']['stderr_bytes'] == 350000
        artifact('preflight-large-streams.json', result)
    asyncio.run(run())


def test_actual_new_batch_cli_merges_one_outer_observation_preserves_prefix_scope(tmp_path):
    async def run():
        command = [PYTHON, str(BATCH), '--observation-id', 'approved-batch', '--evidence-dir', str(tmp_path / 'batch-logs')]
        for index in range(4):
            path = tmp_path / (str(index) + '.fq')
            path.write_text('@read\nACGT\n+\nIIII\n' if index != 2 else 'not-fastq\n')
            command += ['--item', 'item-' + str(index), str(path)]
        check = tiny(tmp_path, 'batch', 'exec ' + ' '.join(shlex.quote(arg) for arg in command))
        stdout, stderr, rc = await run_command(' '.join(shlex.quote(arg) for arg in check['argv']),
            timeout=15, profile=False, env=ENV)
        assert rc == 0, stderr
        assert stdout.count('BIOMIND_EFFECT_EVIDENCE_JSON=') == 1
        observations = extract_structured_effect_evidence(stdout)['produces_observations']
        assert len(observations) == 1 and observations[0]['identity'] == 'approved-batch'
        value = observations[0]['value']
        assert value['returned'] == 4 and value['passed'] == 3
        assert value['full_file_validated'] is False
        assert value['checks'][2]['finding']['value']['prefix_valid'] is False
        assert all(row['finding']['value']['full_scientific_validation'] is False for row in value['checks'])
        assert len([line for line in stderr.splitlines() if parse_progress(line)]) == 13
        for row in value['checks']:
            assert Path(row['raw']['stdout']).read_text().count('BIOMIND_EFFECT_EVIDENCE_JSON=') == 1
        batch_receipt = json.loads((tmp_path / 'batch-logs/checks.json').read_text())
        assert batch_receipt['timeout_seconds'] == 60
        assert batch_receipt['timeout_source'] == 'short_inspection_default_60_seconds'
        artifact('preflight-batch-cli.json', {'rc': rc, 'stdout': stdout, 'stderr': stderr,
            'outer_executor': get_last_execution_metadata(), 'model_calls': 0})
    asyncio.run(run())


def test_progress_excluded_only_from_model_copies_and_preserved_in_raw(tmp_path):
    from agent_core.node_context import _without_control_metadata
    from agent_core.workflow.recovery_projection import _bounded_scalar
    progress = PREFIX + json.dumps({'version': 1, 'event': 'item_started', 'item': 'S1_R1',
        'index': 1, 'total': 4, 'completed': 0, 'passed': 0}) + '\n'
    marker = emit_observation('formal', {'value': 0}) + '\n'
    raw = 'real error\n' + progress + marker + 'last evidence\n'
    copied = _without_control_metadata({'stderr': raw})['stderr']
    assert PREFIX not in copied and marker in copied and 'real error' in copied
    assert PREFIX not in _bounded_scalar(raw)
    assert PREFIX in raw and raw.count(marker) == 1
    assert without_progress(PREFIX + 'invalid-json\n') == PREFIX + 'invalid-json\n'
    assert without_progress('print(' + repr(progress) + ')') == 'print(' + repr(progress) + ')'
    assert parse_progress(PREFIX + '{"version":1,"event":{}}') is None
    from agent_core.workflow.recovery_agent import _observation_envelope
    from types import SimpleNamespace
    seed = SimpleNamespace(failure_packet=SimpleNamespace(task_id='synthetic', step_id='step-1'),
                           plan_spec_ref=SimpleNamespace(plan_id='synthetic-plan'))
    source = {'execution': {'raw_stderr': raw, 'stdout': marker}}
    envelope = _observation_envelope(request_id='tool-1', tool='failure.inspect', arguments={},
                                    result=source, error={}, seed=seed)
    assert PREFIX not in json.dumps(envelope)
    assert source['execution']['raw_stderr'] == raw
    assert envelope['result']['execution']['stdout'] == marker


@pytest.mark.parametrize('failure', ['exit', 'exit124', 'parse'])
def test_failure_is_returned_without_scientific_pass_or_retry(tmp_path, failure):
    async def run():
        code = ('printf unreadable >&2; exit 17' if failure == 'exit' else
                'exit 124' if failure == 'exit124' else 'printf invalid-response')
        first = tiny(tmp_path, 'failed', code)
        next_check = tiny(tmp_path, 'must-not-run', 'exit 0')
        def invalid(_row, _path): raise ValueError('PREFIX_CHILD_OBSERVATION_INVALID')
        result = await run_checks([first, next_check], evidence_dir=tmp_path / 'evidence',
            timeout_seconds=2, env=ENV, progress_stream=Progress(), assess=invalid)
        assert result['status'] == 'incomplete' and result['passed'] == 0 and result['unstarted'] == 1
        assert result['checks'][0]['returncode'] == (17 if failure == 'exit' else 124 if failure == 'exit124' else 0)
        assert [row['event'] for row in result['events']] == [
            'item_started', 'process_started', 'item_returned', 'item_error', 'summary']
        assert len(result['checks']) == 1
    asyncio.run(run())


def test_outer_executor_cancel_reaches_wrapper_owned_nested_process_group(tmp_path):
    async def run():
        child_pid = tmp_path / 'child.pid'
        inner = tiny(tmp_path, 'inner', "sleep 10 &\nkid=$!\nprintf %s \"$kid\" > " + shlex.quote(str(child_pid)) +
            "\ntrap 'kill \"$kid\" 2>/dev/null || true; wait \"$kid\" 2>/dev/null || true; exit 130' TERM INT\nwait \"$kid\"")
        wrapper = tmp_path / 'synthetic-wrapper.py'
        wrapper.write_text('import sys,asyncio\nfrom pathlib import Path\nsys.path.insert(0,' + repr(str(ROOT)) + ')\n'
            'from execution.preflight import run_checks,with_signal_cleanup\n'
            'try:\n asyncio.run(with_signal_cleanup(run_checks(' + repr([inner]) + ', evidence_dir=' + repr(str(tmp_path / 'nested-logs')) + ', timeout_seconds=10)))\n'
            'except asyncio.CancelledError:\n raise SystemExit(130)\n')
        outer = tiny(tmp_path, 'outer', 'exec ' + shlex.quote(PYTHON) + ' ' + shlex.quote(str(wrapper)))
        task = asyncio.create_task(run_command(' '.join(shlex.quote(arg) for arg in outer['argv']),
            timeout=15, profile=False, env=ENV))
        deadline = time.monotonic() + 4
        while not child_pid.exists() and time.monotonic() < deadline: await asyncio.sleep(0.02)
        assert child_pid.exists(), 'The fixture did not reach a real nested spawn'
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert get_last_execution_metadata()['process_reaped'] is True
        evidence = tmp_path / 'nested-logs/checks.json'
        deadline = time.monotonic() + 3
        while not evidence.exists() and time.monotonic() < deadline: await asyncio.sleep(0.02)
        assert evidence.exists(), 'Wrapper cleanup did not persist its bounded receipt'
        result = json.loads(evidence.read_text())
        assert result['status'] == 'cancelled'
        assert result['checks'][0]['execution']['group_reaped'] is True
        with pytest.raises(ProcessLookupError): os.kill(int(child_pid.read_text()), 0)
        artifact('preflight-outer-cancel.json', {'wrapper': result, 'outer': get_last_execution_metadata()})
    asyncio.run(run())


def test_deadline_begins_at_call_not_candidate_construction_or_approval_wait(tmp_path):
    async def run():
        check = tiny(tmp_path, 'after-approval', 'printf actually-started')
        future_approval = asyncio.Event()
        result_path = tmp_path / 'evidence'
        async def approved_call():
            await future_approval.wait()
            return await run_checks([check], evidence_dir=result_path, timeout_seconds=0.15,
                                    progress_stream=Progress(), env=ENV)
        task = asyncio.create_task(approved_call())
        await asyncio.sleep(0.2)
        assert not result_path.exists() and not task.done()
        future_approval.set()
        result = await task
        assert result['status'] == 'returned'
        assert result['checks'][0]['execution']['elapsed_seconds'] < 0.15
        assert DEFAULT_ITEM_TIMEOUT_SECONDS == 60
    asyncio.run(run())


def test_actual_recovery_tool_message_excludes_progress_without_new_model_calls(tmp_path):
    from copy import deepcopy
    from langchain_core.messages import AIMessage
    from agent_core.workflow.recovery_agent import RecoveryAgentLoop
    from tests.test_recovery_agent_loop import _seed_fixture, _directive

    async def run():
        compared = []
        for mode in ('compact', 'verbose', 'off'):
            durable, seed = _seed_fixture()
            progress = PREFIX + json.dumps({'version': 1, 'event': 'item_started', 'item': 'S1_R1',
                'index': 1, 'total': 4, 'completed': 0, 'passed': 0}) + '\n'
            formal = emit_observation('formal', {'prefix_valid': False})
            original = {'status': 'ok', 'execution': {'raw_stderr': 'actual-error\n' + progress,
                        'stdout': formal}, 'source_ref': 'synthetic-execution'}
            saved = deepcopy(original)
            requests, tools_called = [], []
            owner = PlainTerminal(TimedWriter(), enabled=mode != 'off', verbosity='verbose' if mode == 'verbose' else 'compact')
            previous = bind_terminal(owner)

            async def provider(messages, **_kwargs):
                requests.append([{'type': message.type, 'content': message.content} for message in messages])
                for message in messages:
                    if message.type == 'tool':
                        assert PREFIX not in message.content
                        assert 'actual-error' in message.content and 'BIOMIND_EFFECT_EVIDENCE_JSON=' in message.content
                ordinal = len(requests)
                if ordinal <= 2:
                    return AIMessage(content='', tool_calls=[{'id': 'inspect-' + str(ordinal),
                        'name': 'failure_inspect' if ordinal == 1 else 'step_inspect', 'args': {}, 'type': 'tool_call'}])
                return AIMessage(content=json.dumps({'protocol': 'recovery-submission-v1',
                    'explanation': '已核对失败回执，提交当前步骤的既有恢复决定。', 'directive': json.loads(_directive())}))

            class Dispatcher:
                def dispatch_full(self, name, arguments):
                    tools_called.append(name)
                    return original

            try:
                result = await RecoveryAgentLoop(model=object(), dispatcher=Dispatcher(), bind_tools=False,
                    provider_invoke=provider).ainvoke(seed=seed, durable=durable)
                assert not result['terminal_error'], result
                assert len(requests) == 3 and len(tools_called) == 2
                assert original == saved
                compared.append({'directive': result['directive'], 'llm_turns': result['audit']['turn_count'],
                                 'tool_calls': tools_called, 'added_narration_calls': 0})
            finally:
                bind_terminal(previous); owner.close()
        assert compared[0] == compared[1] == compared[2]
        artifact('preflight-recovery-tool-messages.json', {'modes': compared,
            'assertion': 'Actual next provider requests exclude only progress; original dispatcher raw source unchanged'})
    asyncio.run(run())

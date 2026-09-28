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



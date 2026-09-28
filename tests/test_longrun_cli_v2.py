"""Synthetic CLI lifecycle witnesses; no model, Conda, science or credentials."""
import asyncio
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest


def _install_child_guard():
    def guard(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
                     'os.system', 'os.posix_spawn'}:
            raise RuntimeError('CLI_CHILD_EXTERNAL_DENIED ' + event)
        if event == 'open' and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).absolute()
            if ('/03.毕设/' in str(path) or 'config' in path.parts
                    or path.name.startswith('.env') or path.name in {'启动.sh', 'startup.py'}):
                raise RuntimeError('CLI_CHILD_PROTECTED_READ_DENIED')
            mode, flags = str(args[1] or ''), args[2] or 0
            if (any(char in mode for char in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
                if not str(path).startswith('/tmp/') and str(path) != '/dev/null':
                    raise RuntimeError('CLI_CHILD_WRITE_DENIED')
        if event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir'} and isinstance(args[0], (str, bytes)):
            if not str(Path(os.fsdecode(args[0])).absolute()).startswith('/tmp/'):
                raise RuntimeError('CLI_CHILD_MUTATION_DENIED')
    sys.addaudithook(guard)


if __name__ == '__main__':
    _install_child_guard()

from agent_core.cli_display import bind_terminal, emit, terminal
from agent_core.cli_lifecycle import archive_runtime_exception, expected_user_cancellation


def command_event(phase='running', **extra):
    return dict(activity_kind='command_inspection', purpose='status', source='runtime_fact',
        task_id='synthetic-task', node='consultant', call_id='inspection-one',
        tool_call_id='logical-command', run_ref='inspection-one', activity_id='inspection-one',
        progress_phase=phase, status=phase, terminal=phase in {'completed', 'cancelled'},
        command_name='synthetic probe', elapsed_seconds=2, output_idle_seconds=1,
        remaining_seconds=118, hostname='synthetic-host', **extra)


def skill_return():
    scope = dict(task_id='synthetic-task', node='consultant', call_id='provider-one',
                 activity_id='skill-one', tool_call_id='logical-skill')
    emit('tool_start', 'Skill 读取开始', **scope)
    emit('tool_end', 'Skill 读取已返回', status='returned', **scope)






def cancellation_facts():
    return dict(task_id='synthetic-task', request_scope={'task_id': 'synthetic-task', 'task_revision': 3},
        cancellation_receipt={'receipt_id': 'cancel-synthetic', 'task_id': 'synthetic-task',
            'initiator_kind': 'user', 'source_evidence': {'kind': 'cli_key', 'key': 'Ctrl+C'},
            'scope': {'task_id': 'synthetic-task', 'task_revision': 3}},
        owner_state={'task_id': 'synthetic-task', 'cancel_requested': True})


def test_complete_exception_chain_archived_without_classifying_unknown_error_as_cancel(tmp_path):
    try:
        try:
            raise asyncio.CancelledError('synthetic requested cancellation')
        except asyncio.CancelledError as cause:
            raise RuntimeError('synthetic cleanup failure api_key=do-not-display') from cause
    except RuntimeError as error:
        receipt = archive_runtime_exception(tmp_path / 'diagnostics', error,
            **cancellation_facts(), fallback_state={'mode': 'plain', 'password': 'do-not-display'},
            cleanup_refs={'inspection': 'synthetic-receipt'})
    assert receipt['status'] == 'saved'
    assert receipt['expected_user_cancellation'] is False
    saved = json.loads(Path(receipt['diagnostic_ref']).read_text())
    assert 'CancelledError' in saved['traceback'] and 'RuntimeError' in saved['traceback']
    assert 'direct cause' in saved['traceback']
    assert 'do-not-display' not in Path(receipt['diagnostic_ref']).read_text()
    assert saved['fallback_state']['mode'] == 'plain'
    assert saved['cleanup_refs']['inspection'] == 'synthetic-receipt'


def test_cancel_classification_requires_current_user_scope_and_owner(tmp_path):
    error = asyncio.CancelledError('synthetic')
    facts = cancellation_facts()
    assert expected_user_cancellation(error, **facts)
    facts['request_scope']['task_revision'] = 4
    assert not expected_user_cancellation(error, **facts)
    facts = cancellation_facts()
    facts['cancellation_receipt']['initiator_kind'] = 'unknown'
    assert not expected_user_cancellation(error, **facts)
    facts = cancellation_facts()
    facts['owner_state']['cancel_requested'] = False
    assert not expected_user_cancellation(error, **facts)
    receipt = archive_runtime_exception(tmp_path / 'diagnostics', error, **cancellation_facts())
    assert receipt['expected_user_cancellation'] and receipt['status'] == 'saved'


def test_archive_failure_is_reported_without_claiming_saved_or_cleaned(tmp_path):
    occupied = tmp_path / 'file'
    occupied.write_text('ordinary synthetic file')
    receipt = archive_runtime_exception(occupied / 'cannot-create', ValueError('unknown'),
                                       **cancellation_facts())
    assert receipt['status'] == 'failed' and not receipt['diagnostic_ref']
    assert not receipt['expected_user_cancellation']
    assert '不能确认' in receipt['display_text']




def test_probe_guidance_and_synthetic_failure_propagation(tmp_path):
    """A22: execute the documented shape, without a real environment or model."""
    import ast
    import shlex
    root = Path(__file__).resolve().parents[1]
    source = (root / 'agent_core/command_tools.py').read_text()
    instructions = next(ast.literal_eval(node.value) for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
            and target.id == 'COMMAND_TOOL_INSTRUCTIONS' for target in node.targets))
    manager = (root / 'prompts/manager.py').read_text()
    assert 'one verified absolute Conda entry and one' in instructions
    assert '--no-capture-output/--live-stream' in instructions
    assert 'BIOMIND_STAGE begin/end' in instructions and 'python -u' in instructions
    assert 'Ordinary shell defaults are unchanged' in instructions
    assert 'shutil.which only observes PATH discovery' in instructions
    assert 'No expensive environment probe is mandatory' in manager
    assert 'nonzero exits, avoid tail on live probe output' in manager
    execution_source = (root / 'agent_core/command_execution.py').read_text()
    assert "'/bin/bash', '--noprofile', '--norc', '-c', cmd" in execution_source

    count_file = tmp_path / 'entry-count'
    entry = tmp_path / 'synthetic environment entry.sh'
    entry.write_text('#!/bin/bash\nprintf "start\\n" >> ' + shlex.quote(str(count_file))
        + '\nexec "$@"\n')
    probe = tmp_path / 'synthetic probe.py'
    release = tmp_path / 'release-probe'
    probe.write_text('import sys, time\nfrom pathlib import Path\n'
        'print("BIOMIND_STAGE interpreter begin", flush=True)\n'
        'print("BIOMIND_STAGE interpreter end", flush=True)\n'
        'print("BIOMIND_STAGE synthetic_dependency begin", flush=True)\n'
        f'release = Path({str(release)!r})\n'
        'deadline = time.monotonic() + 2\n'
        'while not release.exists() and time.monotonic() < deadline:\n    time.sleep(.01)\n'
        'raise RuntimeError("synthetic dependency unavailable")\n')
    command = '/bin/bash --noprofile --norc ' + shlex.quote(str(entry)) + ' ' + shlex.quote(
        sys.executable) + ' -I -u ' + shlex.quote(str(probe))
    assert 'tail' not in command and 'pipefail' not in command
    process = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-c', command],
        cwd=tmp_path, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        visible_before_exit = [process.stdout.readline().rstrip('\n') for _ in range(3)]
        assert process.poll() is None
        release.write_text('continue synthetic failure')
        remainder, stderr = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    assert process.returncode != 0
    assert count_file.read_text().splitlines() == ['start']
    assert visible_before_exit == ['BIOMIND_STAGE interpreter begin',
        'BIOMIND_STAGE interpreter end', 'BIOMIND_STAGE synthetic_dependency begin']
    assert not remainder
    assert 'synthetic dependency unavailable' in stderr
    evidence = {'scope': 'synthetic documented probe shape; no real Conda/model/healthcheck',
        'environment_entry_count': 1, 'python_process_count': 1, 'returncode': process.returncode,
        'visible_before_exit': visible_before_exit, 'stderr': stderr, 'global_shell_policy_changed': False}
    (tmp_path / 'probe-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))




@pytest.mark.parametrize('mode', ['plain', 'tty'])
def test_real_plain_and_pty_host_exit_with_overlapping_skill_and_command(tmp_path, mode):
    report = tmp_path / ('cli-' + mode + '.json')
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-I', '-c',
        'import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")',
        str(root), str(Path(__file__).resolve()), mode, str(report)]
    environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8', 'TERM': 'xterm-256color',
                   'BIOMIND_UI': '' if mode == 'tty' else 'plain', 'PYTHONDONTWRITEBYTECODE': '1'}
    master = slave = None
    transcript = bytearray()
    if mode == 'tty':
        import fcntl
        import struct
        import termios
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 160, 0, 0))
    process = subprocess.Popen(command, cwd=str(root), env=environment,
        stdin=slave if slave is not None else subprocess.DEVNULL,
        stdout=slave if slave is not None else subprocess.PIPE,
        stderr=slave if slave is not None else subprocess.STDOUT)
    if slave is not None:
        os.close(slave)
    try:
        if master is None:
            output, _ = process.communicate(timeout=10)
            transcript.extend(output)
        else:
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    try:
                        output = os.read(master, 65536)
                    except OSError:
                        break
                    transcript.extend(output)
                    if b'\x1b[6n' in output:
                        os.write(master, b'\x1b[1;1R')
            process.wait(timeout=max(.1, deadline-time.monotonic()))
        assert process.returncode == 0, transcript.decode('utf-8', 'replace')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if master is not None:
            os.close(master)
        (tmp_path / ('cli-' + mode + '.raw')).write_bytes(transcript)
    observed = json.loads(report.read_text())
    assert observed['run_terminal_returned'] and observed['owner_mode'] == mode
    assert observed['tick_growth'] == [0, 0, 0]
    assert observed['command_after_skill']['progress_phase'] == 'running'
    history = '\n'.join(observed['history'])
    assert 'Skill 读取已返回' in history and '执行中' in history and '命令已取消' in history
    assert '无新输出' in history and '原期限剩余' in history
    assert observed['provider_calls'] == observed['command_spawns'] == 0
    if mode == 'tty':
        assert observed['cancel_callbacks'] == 1
        assert 'synthetic probe' in observed['status_line']
        assert '原期限剩余' in observed['clocks']
        assert '取消仍待确认' in history
    (tmp_path / ('cli-' + mode + '.raw')).write_bytes(transcript)



#!/usr/bin/env python3
"""Native Pi reload/new/quit with one explicitly approved stdlib fixture worker."""
import argparse
import codecs
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    options = parser.parse_args()
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[3]
    runtime = repo / 'integrations/pi_runtime'
    isolated = Path(tempfile.mkdtemp(prefix='biomind-native-lifecycle-'))
    project = isolated / '合成 作业项目'
    project.mkdir()
    agent = isolated / 'agent'
    agent.mkdir()
    (agent / 'settings.json').write_text(json.dumps({'enableAnalytics': False, 'enableInstallTelemetry': False,
        'retry': {'enabled': False}, 'compaction': {'enabled': False}, 'cacheWarming': 'off'}))
    registry = isolated / 'registry.json'
    registry.write_text(json.dumps({'environments': {'fixture': {'status': 'verified',
        'prefix': str(Path(sys.executable).parent.parent), 'language': 'python', 'packages': []}}, 'tools': {}}))
    preferences = isolated / 'preferences.md'
    preferences.write_text('合成UI生命周期测试；不用网络，不安装软件。')
    trace = evidence / 'provider-requests.jsonl'
    worker = ('import json,os,time\nfrom pathlib import Path\n'
        'p=Path("start-count");p.write_text(p.read_text()+"x" if p.exists() else "x")\n'
        'Path("worker-start.json").write_text(json.dumps({"pid":os.getpid(),"ppid":os.getppid()}))\n'
        'print("NATIVE_WORKER_STARTED 中文",flush=True)\n'
        'deadline=time.monotonic()+45\n'
        'while not Path("release-worker").exists() and time.monotonic()<deadline:time.sleep(.05)\n'
        'assert Path("release-worker").exists(),"fixture owner release deadline"\n'
        'Path("worker-finished.json").write_text(json.dumps({"pid":os.getpid(),"ppid":os.getppid()}))\n'
        'print("NATIVE_WORKER_FINISHED 中文",flush=True)\n')
    def text(value):
        return {'content': [{'type': 'text', 'text': value}]}
    def call(identity, name, args):
        return {'content': [{'type': 'toolCall', 'id': identity, 'name': name, 'arguments': args}]}
    fixture = isolated / 'fixture.json'
    fixture.write_text(json.dumps([{**text('NATIVE_LIFECYCLE_READY'), 'trace_path': str(trace)},
        call('lifecycle-handoff', 'handoff_generate', {'goal': '只启动一个已批准标准库假作业，测试原生会话生命周期'}),
        call('lifecycle-worker', 'run_command', {'argv': [sys.executable, '-I', '-u', '-c', worker],
            'workdir': str(project), 'env_name': 'fixture', 'description': 'NATIVE_LIFECYCLE_RUN 只运行临时标准库假作业'}),
        text('NATIVE_LIFECYCLE_LAUNCHED')], ensure_ascii=False))
    config = isolated / 'runtime.json'
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(agent), 'dataRoot': str(isolated / 'domain'),
        'providerFixture': str(fixture), 'registryPath': str(registry), 'preferencesPath': str(preferences),
        'memoryRoot': str(isolated / 'memory'), 'wikiRoot': str(isolated / 'wiki'), 'historyEnabled': False}, ensure_ascii=False))
    command = [sys.executable, str(repo / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project),
        '--pi-config', str(config), '--prompt', '只做合成生命周期准备咨询。']
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
    environment = {'PATH': str(runtime / '.runtime/node/bin') + ':/usr/bin:/bin', 'TERM': 'xterm-256color',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
        'NODE_OPTIONS': '--import=' + str(runtime / 'testing/offline-guard.mjs'),
        'BIOMIND_PI_TEST_ROOT': str(isolated), 'BIOMIND_PI_GUARD_TRACE': str(evidence / 'guard.jsonl')}
    child = subprocess.Popen(command, cwd=project, env=environment, stdin=slave, stdout=slave, stderr=slave,
        close_fds=True, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    decoded = ''
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    actions, checks = [], {}
    status, failure = 'FAIL', None
    worker_started = None
    run = None

    def pump(seconds):
        nonlocal decoded
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], min(.1, max(0, end-time.monotonic())))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                raw.extend(chunk)
                decoded += decoder.decode(chunk)
                assert len(raw) < 8 * 1024 * 1024, 'bounded PTY capture exceeded'

    def send(value, action):
        os.write(master, value.encode() if isinstance(value, str) else value)
        actions.append({'action': action, 'raw_offset': len(raw), 'time': time.monotonic()})

    def wait(predicate, label, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError('NATIVE_LIFECYCLE_TIMEOUT:' + label)
            pump(.1)
        actions.append({'observed': label, 'raw_offset': len(raw), 'time': time.monotonic()})

    def screen():
        return ANSI.sub('', decoded)

    def requests():
        return [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []

    def children(pid):
        p = Path(f'/proc/{pid}/task/{pid}/children')
        return [int(value) for value in p.read_text().split()] if p.exists() else []

    def owners():
        return sorted(pid for cli in children(child.pid) for pid in children(cli)
            if Path(f'/proc/{pid}/comm').read_text().strip().startswith('python'))

    def process_running(pid):
        path = Path(f'/proc/{pid}/stat')
        return path.exists() and path.read_text().rsplit(') ', 1)[1].split()[0] != 'Z'

    def session_ids():
        return sorted(json.loads(p.read_text().splitlines()[0])['id'] for p in agent.rglob('*.jsonl'))

    def complete_json(path):
        # The fixture and real executor publish files separately from writing
        # their bytes. Existence alone can observe an empty/partial JSON file.
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    try:
        wait(lambda: 'NATIVE_LIFECYCLE_READY' in screen(), 'native Consult', 60)
        send('启动一个合成短作业，等待我明确批准。\r', 'fixture user execution request')
        # Formal run_command strips presentation-only description before its
        # strict executor contract; observe the actual one-off approval selector.
        wait(lambda: '明确同意执行此提案' in screen(), 'native execution approval')
        assert not (project / 'start-count').exists()
        send(b'\x1b[B\r', 'fixture user explicitly accepts one execution')
        wait(lambda: 'NATIVE_LIFECYCLE_LAUNCHED' in screen(), 'model reports actual start')
        wait(lambda: complete_json(project / 'worker-start.json'), 'actual worker start')
        worker_started = complete_json(project / 'worker-start.json')
        assert (project / 'start-count').read_text() == 'x' and len(requests()) == 4
        result = next(message for message in requests()[-1]['context']['messages']
            if message['role'] == 'toolResult' and message['toolCallId'] == 'lifecycle-worker')
        assert not result.get('isError')
        run = json.loads(result['content'][0]['text'])
        assert run['run_ref'] and run['terminal'] is False and run['owner_available'] is True
        before_owners, before_sessions = owners(), session_ids()
        assert len(before_owners) == len(before_sessions) == 1, (before_owners, before_sessions)
        marker = len(decoded)
        send('/reload\r', 'native reload while original worker is active')
        wait(lambda: 'Reloaded keybindings, extensions' in ANSI.sub('', decoded[marker:]), 'native reload result')
        assert owners() == before_owners and session_ids() == before_sessions
        assert len(requests()) == 4 and not (project / 'worker-finished.json').exists()
        checks['reload_retains_original_domain_pid_and_session_without_model'] = True
        marker = len(decoded)
        send('/new\r', 'native new-session request while original worker is active')
        wait(lambda: '当前仍有科学作业' in ANSI.sub('', decoded[marker:]), 'native new-session blocked')
        assert owners() == before_owners and session_ids() == before_sessions and len(requests()) == 4
        checks['new_session_blocked_with_live_owner'] = True
        send(b'\x03', 'native quit first key')
        pump(.15)
        send(b'\x03', 'native quit second key')
        wait(lambda: child.poll() is not None, 'native process exits', 6)
        assert child.returncode == 0 and not (project / 'worker-finished.json').exists()
        assert process_running(before_owners[0]), 'original owner exited before worker terminal'
        checks['native_exit_does_not_cancel_original_owner'] = True
        (project / 'release-worker').write_text('fixture may finish after UI exit')
        wait(lambda: complete_json(project / 'worker-finished.json'), 'same worker completes after UI exit', 10)
        assert complete_json(project / 'worker-finished.json') == worker_started
        assert (project / 'start-count').read_text() == 'x'
        exits = []
        def terminal():
            exits[:] = list((isolated / 'domain').rglob('exit_status.json'))
            return bool(exits) and complete_json(exits[0]) is not None
        wait(terminal, 'original executor persists terminal result', 10)
        assert len(exits) == 1
        exit_record = json.loads(exits[0].read_text())
        assert exit_record.get('returncode', exit_record.get('exit_code')) == 0
        stdout = (exits[0].parent / 'stdout.log').read_text()
        assert stdout.count('NATIVE_WORKER_STARTED 中文') == stdout.count('NATIVE_WORKER_FINISHED 中文') == 1
        checks['same_worker_terminal_once_after_native_quit'] = True
        wait(lambda: not process_running(before_owners[0]), 'original owner exits after terminal cleanup', 10)
        checks['original_owner_closes_after_terminal'] = True
        (evidence / 'executor-terminal.json').write_text(json.dumps({'run': run, 'owner_pids': before_owners,
            'sessions_before_after': before_sessions, 'worker_identity': worker_started,
            'terminal': exit_record, 'stdout': stdout}, ensure_ascii=False, indent=2) + '\n')
        status = 'PASS'
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
    finally:
        # Releases only this finite stdlib fixture. Never cancels or kills an owner.
        (project / 'release-worker').write_text('fixture cleanup allows natural completion')
        if child.poll() is None:
            send(b'\x03', 'cleanup native quit first key'); pump(.15)
            send(b'\x03', 'cleanup native quit second key'); pump(2)
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=5)
        os.close(master)
        (evidence / 'terminal.raw').write_bytes(raw)
        (evidence / 'terminal.txt').write_text(screen())
        sessions = [{'path': str(p), 'entries': [json.loads(line) for line in p.read_text().splitlines() if line]}
            for p in agent.rglob('*.jsonl')]
        (evidence / 'fresh-sessions.json').write_text(json.dumps(sessions, ensure_ascii=False, indent=2) + '\n')
        report = {'status': status, 'failure': failure, 'checks': checks, 'actions': actions, 'command': command,
            'isolated_root': str(isolated), 'native_exit': child.returncode, 'provider_requests': len(requests()),
            'extension_sha256': hashlib.sha256((runtime / 'dist/extensions/biomind.js').read_bytes()).hexdigest(),
            'scientific_success': None, 'inputs': 'automatic fixture through real native UI',
            'limits': ['No real science or existing session', 'No crash/remote worker reattachment',
                       'No global reload registration-count claim beyond unchanged owner PID and zero model calls']}
        (evidence / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False))
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

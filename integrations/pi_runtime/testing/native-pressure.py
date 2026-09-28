#!/usr/bin/env python3
"""Native Pi + same production extension + real Python logs under increasing synthetic load.

Reuses native-pty.py's PTY/guard/provider approach. No sockets, model endpoint,
ambient sessions, credentials, installation or scientific dataset are used.
"""
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
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', required=True, type=Path)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    repository = Path(__file__).resolve().parents[3]
    runtime = repository / 'integrations/pi_runtime'
    isolated = Path(tempfile.mkdtemp(prefix='biomind-native-pressure-'))
    project = isolated / '合成项目'
    project.mkdir()
    agent = isolated / 'agent'
    agent.mkdir()
    (agent / 'settings.json').write_text(json.dumps({'retry': {'enabled': False}, 'compaction': {'enabled': False},
        'cacheWarming': 'off', 'enableAnalytics': False, 'enableInstallTelemetry': False}))
    registry = isolated / 'registry.json'
    registry.write_text(json.dumps({'environments': {'fixture': {'status': 'verified',
        'prefix': str(Path(sys.executable).parent.parent), 'language': 'python', 'packages': []}}, 'tools': {}}))
    preferences = isolated / 'preferences.md'
    preferences.write_text('仅合成日志压力验收。')
    trace = isolated / 'requests.jsonl'
    probe = isolated / 'pressure.jsonl'
    guard = isolated / 'guard.jsonl'
    helper = project / 'emit_logs.py'
    helper.write_text('''import os,sys,time
from pathlib import Path
if sys.argv[1]=='cancel':
 os.write(1,'CANCEL_LOG_BEGIN 中文🧬\\n'.encode());time.sleep(60)
else:
 data=Path(sys.argv[1]).read_bytes()
 for fd in (1,2):
  offset=0
  while offset<len(data):
   size=(1,2,3,4091)[offset%4]
   offset+=os.write(fd,data[offset:offset+size])
''')
    def message(value, **extra):
        return {'content': [{'type': 'text', 'text': value}], **extra}
    def tool(identity, name, arguments, resolve=False):
        return {'content': [{'type': 'toolCall', 'id': identity, 'name': name, 'arguments': arguments}],
            **({'resolve_last_run_ref': True} if resolve else {})}
    steps = []
    expected = []
    for index, lines in enumerate([100, 4000, 40000]):
        payload = ''.join(f'PRESSURE_{index}_{line:06d} 中文🧬e\u0301完整原日志\n' for line in range(lines)).encode()
        source = project / f'input-{index}.txt'
        source.write_bytes(payload)
        expected.append({'index': index, 'lines': lines, 'bytes_per_stream': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(), 'source': str(source)})
        steps += [tool(f'handoff-{index}', 'handoff_generate', {'goal': f'PRESSURE_{index} 仅运行合成日志'}),
            tool(f'run-{index}', 'run_command', {'argv': [sys.executable, '-I', '-u', str(helper), str(source)],
                'workdir': str(project), 'env_name': 'fixture', 'description': f'PRESSURE_APPROVAL_{index}'}),
            tool(f'wait-{index}', 'wait_run', {'run_ref': '$last_run_ref', 'max_wait_ms': 30000}, True),
            tool(f'logs-{index}', 'read_run_log', {'run_ref': '$last_run_ref'}, True),
            tool(f'page-{index}', 'read_run_log', {'run_ref': '$last_run_ref', 'after_cursor': '$last_cursor'}, True),
            message(f'PRESSURE_STAGE_{index}_DONE 中文完整显示。')]
    steps += [tool('handoff-cancel', 'handoff_generate', {'goal': '仅取消合成等待作业'}),
        tool('run-cancel', 'run_command', {'argv': [sys.executable, '-I', '-u', str(helper), 'cancel'],
            'workdir': str(project), 'env_name': 'fixture', 'description': 'PRESSURE_APPROVAL_CANCEL'}),
        tool('wait-cancel', 'wait_run', {'run_ref': '$last_run_ref', 'max_wait_ms': 250}, True),
        tool('cancel-job', 'cancel_run', {'run_ref': '$last_run_ref', 'reason': 'Explicit synthetic pressure cancellation'}, True),
        tool('cancel-terminal', 'wait_run', {'run_ref': '$last_run_ref', 'max_wait_ms': 30000}, True),
        message('PRESSURE_CANCEL_DONE 指定作业取消已观察。')]
    chunks = ['PRESSURE_ABORT_BEGIN\n'] + ['中文🧬取消与滚动\n'] * 60 + ['ABORT_UNEXPECTED_FINISH']
    steps += [message(''.join(chunks), chunks=chunks, delay_ms=100), message('PRESSURE_RECOVERED 中文🧬', expect_user_contains='恢复')]
    steps[0]['trace_path'] = str(trace)
    fixture = isolated / 'fixture.json'
    fixture.write_text(json.dumps(steps, ensure_ascii=False))
    config = isolated / 'config.json'
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(agent), 'dataRoot': str(isolated / 'domain'),
        'providerFixture': str(fixture), 'registryPath': str(registry), 'preferencesPath': str(preferences),
        'memoryRoot': str(isolated / 'memory'), 'wikiRoot': str(isolated / 'wiki'), 'historyEnabled': False}))
    command = [sys.executable, str(repository / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project),
        '--pi-config', str(config), '--prompt', '执行合成压力阶段0']
    environment = {'PATH': str(runtime / '.runtime/node/bin') + ':/usr/bin:/bin', 'TERM': 'xterm-256color',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '0',
        'NODE_OPTIONS': '--import=' + str(runtime / 'testing/offline-guard.mjs'), 'BIOMIND_PI_TEST_ROOT': str(isolated),
        'BIOMIND_PI_GUARD_TRACE': str(guard), 'BIOMIND_PI_PRESSURE_TRACE': str(probe)}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
    process = subprocess.Popen(command, cwd=project, env=environment, stdin=slave, stdout=slave, stderr=slave,
        close_fds=True, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    decoded = ''
    decoder = codecs.getincrementaldecoder('utf8')('strict')
    actions, stages, checks = [], [], {}
    def pump(seconds):
        nonlocal decoded
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not select.select([master], [], [], max(0, min(.1, end-time.monotonic())))[0]:
                continue
            try: chunk = os.read(master, 65536)
            except OSError: break
            if not chunk: break
            raw.extend(chunk)
            decoded += decoder.decode(chunk)
            if len(raw) > 16*1024*1024: raise RuntimeError('PTY_OUTPUT_LIMIT')
    def send(value, label):
        actions.append({'input': label, 'raw_offset': len(raw)})
        os.write(master, value if isinstance(value, bytes) else value.encode())
    def wait(marker, since=0):
        end = time.monotonic()+60
        while marker not in ANSI.sub('', decoded[since:]):
            if process.poll() is not None: raise RuntimeError(f'NATIVE_EXIT:{process.returncode}:{marker}')
            if time.monotonic() > end: raise RuntimeError('NATIVE_TIMEOUT:'+marker)
            pump(.1)
    def records(filename):
        return [json.loads(line) for line in filename.read_text().splitlines()] if filename.exists() else []
    def receipt(identity):
        for request in reversed(records(trace)):
            for item in request['context']['messages']:
                if item.get('role') == 'toolResult' and item.get('toolCallId') == identity:
                    assert not item.get('isError'), item
                    return json.loads(item['content'][0]['text'])
        raise AssertionError('missing actual tool receipt '+identity)
    def resize(rows, columns):
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
        for child in children: os.kill(int(child), signal.SIGWINCH)
        actions.append({'resize': [rows, columns], 'signalled_children': children})
    status, failure = 'FAIL', None
    try:
        for index in range(4):
            offset = len(decoded)
            if index: send(f'执行合成压力阶段{index}\r', f'stage {index}')
            wait('是否执行以下提案', since=offset)
            pump(.2)
            send(b'\x1b[B\r', 'explicitly accept only the displayed synthetic command')
            pump(.2)
            resize(28, 80 if index % 2 else 100)
            send(b'\x1b[5~', 'native PageUp under log pressure')
            pump(.15)
            send(b'\x1b[6~\x1b[F', 'native PageDown and End')
            wait(f'PRESSURE_STAGE_{index}_DONE' if index < 3 else 'PRESSURE_CANCEL_DONE', since=offset)
            pump(.25)
            renders = [item for item in records(probe) if item['type'] == 'render' and item.get('renderCount', 0) > 0]
            assert renders, 'test observer did not see actual production renderer'
            stages.append({'index': index, 'render_count': len(renders),
                'maximum_component': {key: max(item[key] for item in renders) for key in
                    ('textBytes', 'logicalLines', 'renderedLines', 'renderedBytes', 'cachedWidths')},
                'heap_samples': [item['heapUsed'] for item in records(probe) if item['type']=='agent_end']})
            if index < 3:
                terminal = receipt(f'wait-{index}')
                assert terminal.get('terminal') is True and terminal.get('exit_code', terminal.get('returncode')) == 0, terminal
                assert terminal['run_ref'] in ANSI.sub('', decoded), 'critical run_ref absent from actual native rendering'
                for stream in ('stdout', 'stderr'):
                    logfile = Path(terminal['log_refs'][stream])
                    payload = logfile.read_bytes()
                    assert len(payload) == expected[index]['bytes_per_stream']
                    assert hashlib.sha256(payload).hexdigest() == expected[index]['sha256']
                    shutil.copyfile(logfile, evidence / f'full-{index}-{stream}.log')
                page = receipt(f'logs-{index}')
                next_page = receipt(f'page-{index}')
                assert '\ufffd' not in json.dumps([page, next_page], ensure_ascii=False)
                if index > 0: assert next_page['next_cursor'] != page['next_cursor'], 'opaque cursor did not advance'
                assert len(json.dumps(page).encode()) < 256*1024, 'model received entire growing log'
                expected[index]['actual_log_refs'] = terminal['log_refs']
                expected[index]['page_bytes'] = len(json.dumps(page, ensure_ascii=False).encode())
                expected[index]['next_cursor'] = next_page['next_cursor']
            else:
                terminal = receipt('cancel-terminal')
                assert terminal.get('terminal') is True, terminal
                assert terminal.get('cancel_requested') or terminal.get('stop_requested') or terminal.get('status') in ('cancelled', 'stopped'), terminal
                checks['explicit_job_cancel_terminal'] = True
        send('合成中断流\r', 'start cancellable model stream')
        wait('PRESSURE_ABORT_BEGIN')
        pump(.2)
        resize(32, 90)
        send(b'\x1b', 'abort only current model stream')
        pump(.6)
        assert 'ABORT_UNEXPECTED_FINISH' not in ANSI.sub('', decoded)
        send('恢复\r', 'new Consult after model abort')
        wait('PRESSURE_RECOVERED')
        pump(.3)
        items = records(probe)
        limits = next(item for item in items if item['type']=='limits')
        for item in items:
            if item['type']=='render':
                for key in ('textBytes', 'logicalLines', 'renderedLines', 'renderedBytes', 'cachedWidths'):
                    assert item[key] <= limits['display'][key], (key, item)
            elif item['type']=='transport':
                for key in ('pendingRequests', 'queuedWrites', 'queuedBytes', 'diagnosticBytes'):
                    assert item[key] <= limits['transport'][key], (key, item)
        assert len(records(trace)) == len(steps), 'unexpected synthetic provider attempts'
        assert not [item for item in records(guard) if item['kind']=='network_attempt']
        assert not [item for item in records(guard) if item['kind']=='sensitive_file_attempt' and not item['allowed']]
        assert '\ufffd' not in decoded
        checks.update({'actual_bounded_renderer': True, 'full_python_stdout_stderr_exact': True,
            'model_pages_bounded_cursor_advances': True, 'unicode_split_utf8': True,
            'critical_run_ref_visible_and_terminal_exit_preserved': True,
            'resize_and_scroll_keys_no_corruption': True, 'model_abort_and_recovery': True,
            'external_requests': 0, 'prior_session_reads': 0})
        status = 'PASS'
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
    finally:
        if process.poll() is None:
            send(b'\x03', 'exit first Ctrl+C')
            pump(.15)
            send(b'\x03', 'exit second Ctrl+C')
            pump(2)
        if process.poll() is None:
            process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
        os.close(master)
        for source, name in [(trace, 'native-provider-requests.jsonl'), (probe, 'native-pressure-counts.jsonl'),
            (guard, 'native-guard.jsonl')]:
            if source.exists(): shutil.copyfile(source, evidence/name)
        (evidence/'native-terminal.raw').write_bytes(raw)
        (evidence/'native-terminal.txt').write_text(ANSI.sub('', decoded))
        files = ['dist/extensions/biomind.js', 'dist/tool-display.js', 'dist/domain-client.js', 'package-lock.json']
        result = {'status': status, 'failure': failure, 'command': command, 'isolated_root': str(isolated),
            'source_hashes': {name: hashlib.sha256((runtime/name).read_bytes()).hexdigest() for name in files},
            'native_exit': process.returncode, 'stages': stages, 'expected_full_logs': expected, 'actions': actions,
            'checks': checks, 'provider_request_count': len(records(trace)), 'expected_provider_request_count': len(steps),
            'scope': 'Per-tool display cache and transport retention; persistent Pi history and complete disk logs may grow.',
            'visual_scroll_position_assertion': 'NOT_RUN', 'real_ime_composition': 'NOT_RUN', 'scientific_success': None}
        (evidence/'native-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(result, ensure_ascii=False))
    return 0 if status=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

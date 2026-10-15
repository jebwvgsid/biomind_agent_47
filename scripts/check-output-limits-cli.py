"""Real native CLI/PTY and pinned bundle; scripted provider, isolated session."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import termios
import time

p = argparse.ArgumentParser()
p.add_argument('candidate', type=Path)
p.add_argument('evidence', type=Path)
p.add_argument('--node', required=True)
args = p.parse_args()
root = Path(__file__).resolve().parents[1]
candidate, evidence = args.candidate.resolve(), args.evidence.resolve()
config = json.loads((candidate / 'runtime.json').read_text())
project = candidate / 'cli-project'
project.mkdir(exist_ok=True)
body = '\n'.join(f'{i}: ' + ('MIDDLE_CLI_MARKER' if i == 600 else 'native🧬' * 12) for i in range(1200)) + '\nTAIL_CLI_MARKER'
fixture = project / 'fixed-output.txt'
fixture.write_text(body)
trace = evidence / 'cli-provider-mock.jsonl'
trace.write_text('')
provider = candidate / 'cli-provider.json'
provider.write_text(json.dumps([
    {'trace_path': str(trace), 'content': [{'type': 'toolCall', 'id': 'cli-fixed-bash', 'name': 'bash', 'arguments': {'command': f"cat '{fixture}'"}}]},
    {'content': [{'type': 'text', 'text': 'CLI_OUTPUT_LIMITS_DONE'}]},
], ensure_ascii=False))
runtime_config = candidate / 'cli-runtime.json'
runtime_config.write_text(json.dumps({**config, 'providerFixture': str(provider), 'sessionMode': 'persistent'}))
command = [config['python'], '-B', str(root / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project),
           '--pi-config', str(runtime_config), '--pi-node', args.node, '--prompt', 'Inspect only the fixed output fixture.']
(candidate / 'home').mkdir(exist_ok=True)
ui_trace = evidence / 'cli-session-lifecycle.jsonl'; ui_trace.write_text('')
env = {'PATH': str(Path(args.node).parent) + ':/usr/bin:/bin', 'HOME': str(candidate / 'home'), 'BIOMIND_PI_UI_TRACE': str(ui_trace), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
       'TERM': 'xterm-256color', 'PI_OFFLINE': '1', 'BIOMIND_DISABLE_DOTENV': '1', 'PYTHONDONTWRITEBYTECODE': '1',
       'BIOMIND_PI_NODE': args.node, 'BIOMIND_PI_PYTHON': config['python']}
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 140, 0, 0))
process = subprocess.Popen(command, cwd=project, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
os.close(slave)
raw = bytearray()

def pump():
    if select.select([master], [], [], .1)[0]:
        try:
            data = os.read(master, 65536)
        except OSError:
            return
        raw.extend(data)
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError('synthetic PTY output exceeded bound')

try:
    until = time.monotonic() + 90
    while b'CLI_OUTPUT_LIMITS_DONE' not in raw:
        pump()
        if process.poll() is not None or time.monotonic() > until:
            raise RuntimeError(f'native fixture did not complete; exit={process.poll()}')
    # Wait for the native turn to settle before sending the ordinary quit command.
    for _ in range(10):
        pump()
    for action in ['reload', 'new']:
        lifecycle_start = len(raw)
        os.write(master, ('/' + action + '\r').encode())
        until = time.monotonic() + 60
        while not any(json.loads(line).get('reason') == action for line in ui_trace.read_text().splitlines()):
            pump()
            if process.poll() is not None or time.monotonic() > until:
                raise RuntimeError('native lifecycle did not complete: ' + action)
        # session_start fires before the TUI finishes rebuilding its input widget.
        # Wait for its actual ready message before typing, otherwise input is lost.
        if action == 'reload':
            until = time.monotonic() + 60
            while b'Reloaded keybindings, extensions' not in raw[lifecycle_start:]:
                pump()
                if time.monotonic() > until: raise RuntimeError('TUI reload did not become ready')
        else:
            for _ in range(50): pump()
        os.write(master, b'Inspect only the fixed output fixture.\r')
        expected = 4 if action == 'reload' else 6
        until = time.monotonic() + 60
        while len(trace.read_text().splitlines()) < expected:
            pump()
            if process.poll() is not None or time.monotonic() > until:
                raise RuntimeError('native lifecycle provider did not complete: ' + action)
        for _ in range(10): pump()
    os.write(master, b'/quit\r')
    until = time.monotonic() + 20
    while process.poll() is None and time.monotonic() < until:
        pump()
    assert process.poll() == 0, 'native candidate did not quit cleanly'
    requests = [json.loads(row) for row in trace.read_text().splitlines()]
    assert len(requests) == 6
    for request in requests:
        encoded = json.dumps(request['context'], ensure_ascii=False)
        assert encoded.count('普通文本工具结果正文默认最多 300 行或 8192 字节') == 1
        assert encoded.count('大文件或长工具输出按需检索、分段读取或在计算端统计筛选') == 1
        assert body not in encoded
    results = [m for m in requests[-1]['context']['messages'] if m['role'] == 'toolResult']
    assert len(results) == 1 and not results[0]['isError']
    text = ''.join(b['text'] for b in results[0]['content'] if b['type'] == 'text')
    assert 'TAIL_CLI_MARKER' in text and 'MIDDLE_CLI_MARKER' not in text
    assert fixture.read_text() == body
    (evidence / 'cli-chain.json').write_text(json.dumps({'status': 'PASS', 'classification': 'REAL_NATIVE_CLI_PTY_MOCK_PROVIDER',
        'command': command, 'exitCode': process.returncode, 'requestCount': len(requests), 'lifecycle': ['startup', 'reload', 'new'], 'toolResultBytes': len(text.encode()),
        'toolResultCodePoints': len(text), 'project': str(project)}, ensure_ascii=False, indent=2) + '\n')
    print('PASS: real native CLI/PTY, pinned plugin bundle, reload + same-process new, six actual mock-provider requests.')
finally:
    (evidence / 'cli-pty.log').write_bytes(raw)
    # Only this isolated child belongs to the test; never touch a service or other task.
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    os.close(master)

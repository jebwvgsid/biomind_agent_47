#!/usr/bin/env python3
"""P07/P10/P11 real CLI witnesses using the existing native-pty probe primitives."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time

ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]')


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve(); evidence.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[3]
    runtime = repo / 'integrations/pi_runtime'
    root = Path(tempfile.mkdtemp(prefix='biomind-project-pty-'))
    home = root / 'home'; home.mkdir()
    agent = root / 'agent'; agent.mkdir()
    project = root / 'A 中文项目'; project.mkdir()
    results = project / 'results'; results.mkdir()
    (project / 'AGENTS.md').write_text('PROJECT_ROOT_P07\n')
    (agent / 'settings.json').write_text(json.dumps({'enableAnalytics': False, 'enableInstallTelemetry': False,
        'retry': {'enabled': False}, 'compaction': {'enabled': False}, 'cacheWarming': 'off'}))
    (agent / 'trust.json').write_text(json.dumps({str(project): True}))
    registry = root / 'registry.json'; registry.write_text('{"environments":{},"tools":{}}')
    preferences = root / 'preferences.md'; preferences.write_text('PROJECT_ENTRY_SYNTHETIC_PREFERENCES_ONLY\n')
    memory = root / 'memory'; (memory / 'wiki').mkdir(parents=True)
    with sqlite3.connect(memory / 'wiki/wiki_index.sqlite3') as db:
        db.execute('CREATE TABLE memory_project_roots(root TEXT,project_id TEXT,PRIMARY KEY(root,project_id))')
        db.execute('INSERT INTO memory_project_roots VALUES (?,?)', (str(project), 'existing-root-P07'))
    trace = evidence / 'provider.jsonl'
    fixture = root / 'provider.json'; fixture.write_text(json.dumps([{'content': [{'type': 'text', 'text': 'P07_ROOT_SELECTED'}], 'trace_path': str(trace)}]))
    config = root / 'runtime.json'
    cfg = {'python': sys.executable, 'agentDir': str(agent), 'sessionDir': str(root / 'sessions'),
        'providerFixture': str(fixture), 'registryPath': str(registry), 'memoryRoot': str(memory), 'historyEnabled': False,
        'preferencesPath': str(preferences), 'archivePath': str(root / 'archive.sqlite3')}
    config.write_text(json.dumps(cfg))
    env = {'PATH': str(runtime / '.runtime/node/bin') + ':/usr/bin:/bin', 'HOME': str(home), 'TERM': 'xterm-256color',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'BIOMIND_DISABLE_DOTENV': '1',
        'PI_CODING_AGENT_DIR': str(agent), 'NODE_OPTIONS': '--import=' + str(runtime / 'testing/offline-guard.mjs'),
        'BIOMIND_PI_TEST_ROOT': str(root), 'BIOMIND_PI_GUARD_TRACE': str(evidence / 'guard.jsonl')}
    checks = []; commands = []

    def run(label, command, cwd, action):
        raw = bytearray(); events = []; master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 140, 0, 0))
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave); commands.append({'scenario': label, 'argv': command, 'cwd': str(cwd)})

        def screen(): return ANSI.sub('', raw.decode(errors='replace'))
        def pump(seconds=.25):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if select.select([master], [], [], .05)[0]:
                    try: part = os.read(master, 65536)
                    except OSError: break
                    if not part: break
                    raw.extend(part)
        def send(keys, why):
            os.write(master, keys); pump(.35); events.append({'action': why, 'keys': repr(keys), 'bytes_after': len(raw)})
        def wait(marker, timeout=45):
            until = time.monotonic() + timeout
            while marker not in screen():
                if process.poll() is not None: raise RuntimeError(f'Exited {process.returncode} before {marker}')
                if time.monotonic() > until: raise TimeoutError(marker)
                pump()
        try:
            action(send, wait, process)
            process.wait(timeout=10); pump()
            checks.append({'id': label, 'status': 'PASS' if process.returncode == 0 else 'FAIL', 'exit_code': process.returncode})
        except Exception as error:
            checks.append({'id': label, 'status': 'FAIL', 'error': repr(error)})
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait()
            os.close(master)
            (evidence / (label + '.raw')).write_bytes(raw)
            (evidence / (label + '.txt')).write_text(screen())
            (evidence / (label + '-keys.json')).write_text(json.dumps(events, ensure_ascii=False, indent=2))

    base = [sys.executable, str(repo / 'bio_agents.py'), '--pi-config', str(config), '--pi-node', str(runtime / '.runtime/node/bin/node')]
    before = snapshot(root)
    def cancel(send, wait, process):
        wait('创建项目'); send(b'\x1b', 'cancel project menu')
    run('P11-menu-cancel', base + ['project'], results, cancel)
    checks.append({'id': 'P11-menu-no-files', 'status': 'PASS' if snapshot(root) == before else 'FAIL'})

    target = root / 'C 未创建 空项目'
    def cancel_create(send, wait, process):
        wait('创建项目')
        # A registered root and current results directory precede create.
        send(b'\x1b[B', 'next project'); send(b'\x1b[B', 'select create'); send(b'\r', 'open create path editor')
        wait('输入目录'); send(str(target).encode() + b'\r', 'enter synthetic target')
        wait('确认创建'); send(b'\x1b', 'cancel before mutation')
    run('P11-create-cancel', base + ['project'], results, cancel_create)
    checks.append({'id': 'P11-create-no-files', 'status': 'PASS' if snapshot(root) == before and not target.exists() else 'FAIL'})

    bad_memory = root / 'unreadable-source'; (bad_memory / 'wiki').mkdir(parents=True)
    (bad_memory / 'wiki/wiki_index.sqlite3').write_text('SYNTHETIC_INVALID_DB')
    bad_agent = root / 'partial-agent'; bad_agent.mkdir()
    (bad_agent / 'biomind-launcher.json').write_text(json.dumps({'recentProjectRoots': [str(root / '不可达 项目')]}))
    bad_config = root / 'partial-config.json'; bad_config.write_text(json.dumps({**cfg, 'agentDir': str(bad_agent), 'memoryRoot': str(bad_memory)}))
    partial_before = snapshot(root)
    def partial_menu(send, wait, process):
        wait('部分来源不可读'); wait('missing'); send(b'\x1b', 'cancel incomplete but usable directory menu')
    partial_base = [sys.executable, str(repo / 'bio_agents.py'), '--pi-config', str(bad_config), '--pi-node', str(runtime / '.runtime/node/bin/node')]
    run('P10-partial-list-menu', partial_base + ['project'], results, partial_menu)
    checks.append({'id': 'P10-read-only', 'status': 'PASS' if snapshot(root) == partial_before else 'FAIL'})

    def suggest(send, wait, process):
        wait('当前目录位于已知项目内'); send(b'\x1b[B', 'choose registered root'); send(b'\r', 'confirm root')
        wait('准备就绪'); send(b'P07 synthetic root witness\r', 'request after final project selection')
        wait('P07_ROOT_SELECTED'); send(b'/exit\r', 'normal exit')
    run('P07-root-suggestion', base, results, suggest)
    files = list((root / 'sessions').glob('*.jsonl'))
    headers = [json.loads(filename.read_text().splitlines()[0]) for filename in files]
    checks.append({'id': 'P07-final-session-cwd', 'status': 'PASS' if len(headers) == 1 and headers[0].get('cwd') == str(project) else 'FAIL', 'headers': headers})
    checks.append({'id': 'P07-no-results-registration', 'status': 'PASS' if not (agent / 'biomind-launcher.json').exists() else 'FAIL'})
    other = root / 'B other project'; other.mkdir()
    seed = root / 'seed-session.mjs'
    seed.write_text('import { SessionManager } from ' + json.dumps((runtime / 'node_modules/@earendil-works/pi-coding-agent/dist/index.js').as_uri()) + ';\n'
        + 'const m=SessionManager.create(' + json.dumps(str(other)) + ',' + json.dumps(str(root / 'sessions')) + ');\n'
        + 'm.appendMessage({role:"user",content:"CROSS_PROJECT_B_ONLY",timestamp:Date.now()});\n'
        + 'm.appendMessage({role:"assistant",content:[{type:"text",text:"B_SYNTHETIC_HISTORY"}],timestamp:Date.now(),api:"openai-completions",provider:"fixture",model:"fixture",stopReason:"stop",usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});\n'
        + 'm.appendSessionInfo("CROSS_PROJECT_B_ONLY");\n')
    subprocess.run([str(runtime / '.runtime/node/bin/node'), str(seed)], env=env, check=True, timeout=45)
    def cross_resume(send, wait, process):
        wait('准备就绪'); send(b'/resume\r', 'native resume selector')
        wait('Current Folder'); send(b'\t', 'native all-project scope'); send(b'CROSS_PROJECT_B_ONLY', 'filter B session')
        wait('CROSS_PROJECT_B_ONLY'); send(b'\r', 'attempt cross-project resume')
        wait('目标会话属于其它项目'); send(b'/project list\r', 'inspect retained project')
        wait('当前项目'); send(b'/exit\r', 'normal exit after denied hot switch')
    run('S07-cross-project-resume', base + ['--project-dir', str(project), '--continue'], project, cross_resume)
    result = {'checks': checks, 'commands': commands, 'synthetic_root': str(root), 'evidence_type': 'PRODUCT_TUI',
        'built_host_sha256': hashlib.sha256((runtime / 'dist/host.js').read_bytes()).hexdigest(),
        'project_launcher_sha256': hashlib.sha256((runtime / 'dist/project-launcher.js').read_bytes()).hexdigest()}
    (evidence / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(check['status'] == 'PASS' for check in checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())

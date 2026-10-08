#!/usr/bin/env python3
"""Offline PTY acceptance for the directory-independent BioMind entry menu.

All projects, Pi sessions, settings and provider responses are synthetic. The
existing offline guard denies network and real credential reads. Run against a
built runtime; this script never invokes the user's installed biomind wrapper.
"""
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
import struct
import subprocess
import sys
import tempfile
import termios
import time


ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]')
SCENARIOS = ('home-cancel', 'home-input-eio', 'cwd-first-direct', 'empty-project-back', 'history-back-navigation',
    'open-directory-back', 'create-project-back', 'all-history-back', 'all-history-cross-project',
    'open-directory-new-conversation', 'explicit-empty-project-direct', 'explicit-session-direct',
    'explicit-resume-picker', 'resume-nontty', 'resume-json')
NATIVE_SCENARIOS = {'all-history-cross-project', 'open-directory-new-conversation',
    'explicit-empty-project-direct', 'explicit-session-direct', 'cwd-first-direct'}


def rows(filename):
    return [json.loads(line) for line in filename.read_text().splitlines() if line.strip()] if filename.exists() else []


def snapshot(root):
    return {str(filename.relative_to(root)): hashlib.sha256(filename.read_bytes()).hexdigest()
            for filename in root.rglob('*') if filename.is_file() and filename.name != 'provider.json'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--scenario', action='append', choices=SCENARIOS,
        help='Run only this scenario; repeat to select several (default: all)')
    args = parser.parse_args()
    selected = set(args.scenario or SCENARIOS)
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if any(evidence.iterdir()):
        parser.error('Use an empty evidence directory')
    repo = Path(__file__).resolve().parents[3]
    runtime = repo / 'integrations/pi_runtime'
    node = runtime / '.runtime/node/bin/node'
    root = Path(tempfile.mkdtemp(prefix='biomind-startup-project-'))
    project_a, project_b, empty, other, launch = [root / name for name in
        ('A 中文项目', 'B 历史项目', 'C 空项目', 'D 另一个目录', 'Z 任意启动目录')]
    agent, home, sessions = [root / name for name in ('agent', 'home', 'sessions')]
    for directory in (project_a, project_b, empty, other, launch, agent, home, sessions, root / 'tmp'):
        directory.mkdir()
    (agent / 'settings.json').write_text(json.dumps({'enableAnalytics': False, 'enableInstallTelemetry': False,
        'retry': {'enabled': False}, 'compaction': {'enabled': False}, 'cacheWarming': 'off'}))
    (agent / 'trust.json').write_text(json.dumps({str(project): True for project in
        (project_a, project_b, empty, other, launch)}))
    (agent / 'biomind-launcher.json').write_text(json.dumps({'recentProjectRoots': [str(empty)]}))
    registry = root / 'registry.json'
    registry.write_text('{"environments":{},"tools":{}}')
    preferences = root / 'preferences.md'
    preferences.write_text('合成启动菜单验收；仅回答，不执行科研作业。\n')
    fixture = root / 'provider.json'
    fixture.write_text('[]')
    config = root / 'runtime.json'
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(agent), 'sessionDir': str(sessions),
        'providerFixture': str(fixture), 'registryPath': str(registry), 'preferencesPath': str(preferences),
        'memoryRoot': str(root / 'memory'), 'archivePath': str(root / 'archive.sqlite3'),
        'historyEnabled': False, 'memoryMode': 'disabled'}, ensure_ascii=False))
    env = {'PATH': str(runtime / '.runtime/node/bin') + ':/usr/bin:/bin', 'HOME': str(home),
        'TMPDIR': str(root / 'tmp'), 'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'PYTHONDONTWRITEBYTECODE': '1', 'BIOMIND_DISABLE_DOTENV': '1', 'PI_OFFLINE': '1',
        'PI_CODING_AGENT_DIR': str(agent), 'BIOMIND_PI_PYTHON': sys.executable,
        'BIOMIND_PI_EXPECT_EMBEDDED_SDK': '1',
        'NODE_OPTIONS': '--max-old-space-size=512 --import=' + str(runtime / 'testing/offline-guard.mjs'),
        'BIOMIND_PI_TEST_ROOT': str(root), 'BIOMIND_PI_GUARD_TRACE': str(evidence / 'guard.jsonl')}
    base = [sys.executable, str(repo / 'bio_agents.py'), '--pi-config', str(config), '--pi-node', str(node)]
    checks, commands = [], []
    # Inject only at this menu's owned stdin listener. This verifies cleanup;
    # it cannot establish the cause of the original terminal incident.
    eio_injector = root / 'inject-menu-eio.mjs'
    eio_injector.write_text("let sent=false;process.stdin.on('newListener',name=>{if(name==='error'&&!sent){sent=true;setTimeout(()=>{const e=new Error('read EIO SYNTHETIC_MENU_INPUT');e.code='EIO';e.syscall='read';process.stdin.emit('error',e);},500);}});\n")

    # Pi's public manager writes valid native history without starting BioMind or
    # submitting a model request. Read-only resume assertions need no task ledger.
    seed = root / 'seed.mjs'
    seed.write_text('import {SessionManager} from '
        + json.dumps((runtime / 'node_modules/@earendil-works/pi-coding-agent/dist/index.js').as_uri()) + ';\n'
        + 'const saved={};\n'
        + 'for(const [name,cwd] of ' + json.dumps([['ENTRY_A_HISTORY', str(project_a)], ['ENTRY_B_HISTORY', str(project_b)]]) + '){\n'
        + 'const m=SessionManager.create(cwd,' + json.dumps(str(sessions)) + ');\n'
        + 'm.appendMessage({role:"user",content:name+"_USER_CONTEXT",timestamp:Date.now()});\n'
        + 'm.appendMessage({role:"assistant",content:[{type:"text",text:name+"_RESTORED"}],timestamp:Date.now(),'
          'api:"openai-completions",provider:"fixture",model:"fixture",stopReason:"stop",'
          'usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});\n'
        + 'm.appendSessionInfo(name);saved[name]={id:m.getSessionId(),path:m.getSessionFile(),cwd};}\n'
        + 'console.log(JSON.stringify(saved));\n')
    seeded = subprocess.run([str(node), str(seed)], env=env, capture_output=True, text=True, timeout=90, check=True)
    saved = json.loads(seeded.stdout)
    (evidence / 'seeded-sessions.json').write_text(json.dumps(saved, ensure_ascii=False, indent=2))

    def check(identity, condition, **details):
        checks.append({'id': identity, 'status': 'PASS' if condition else 'FAIL', **details})
        if not condition:
            print('FAIL ' + json.dumps(checks[-1], ensure_ascii=False), file=sys.stderr, flush=True)

    def run(label, extra, cwd, action):
        if label not in selected:
            return None
        print('START ' + label, flush=True)
        provider_trace, ui_trace = evidence / (label + '-provider.jsonl'), evidence / (label + '-ui.jsonl')
        response = label.upper().replace('-', '_') + '_DONE'
        fixture.write_text(json.dumps([{'content': [{'type': 'text', 'text': response}], 'trace_path': str(provider_trace)}]))
        command = base + extra
        commands.append({'scenario': label, 'argv': command, 'cwd': str(cwd)})
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 45, 180, 0, 0))
        scenario_env = {**env, 'BIOMIND_PI_UI_TRACE': str(ui_trace)}
        if label == 'home-input-eio':
            scenario_env['NODE_OPTIONS'] += ' --import=' + str(eio_injector)
        process = subprocess.Popen(command, cwd=cwd, env=scenario_env,
            stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave)
        started = time.monotonic()
        raw, events, last_action_offset = bytearray(), [], 0

        def screen(fresh=False):
            return ANSI.sub('', raw[last_action_offset if fresh else 0:].decode(errors='replace'))

        def pump(seconds=.15):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if select.select([master], [], [], .05)[0]:
                    try:
                        part = os.read(master, 65536)
                    except OSError:
                        break
                    if not part:
                        break
                    raw.extend(part)
                    (evidence / (label + '.raw')).write_bytes(raw)
                    if len(raw) > 8 * 1024 * 1024:
                        raise RuntimeError('OUTPUT_LIMIT')

        def wait(marker, timeout=90):
            until = time.monotonic() + timeout
            # Only bytes produced since the last keystroke count. Earlier menu
            # renders cannot falsely satisfy assertions about returning to it.
            while marker not in screen(fresh=True):
                if process.poll() is not None:
                    raise RuntimeError(f'Exited {process.returncode} before {marker}')
                if time.monotonic() > until:
                    raise TimeoutError(marker)
                pump()
            events.append({'seen': marker, 'raw_offset': len(raw), 'elapsed_seconds': round(time.monotonic() - started, 3)})

        def send(keys, purpose):
            nonlocal last_action_offset
            last_action_offset = len(raw)
            os.write(master, keys.encode() if isinstance(keys, str) else keys)
            events.append({'action': purpose, 'raw_offset': last_action_offset,
                'elapsed_seconds': round(time.monotonic() - started, 3)})
            pump(.3)

        def choose(index, purpose):
            for _ in range(index):
                send(b'\x1b[B', 'move down')
            send(b'\r', purpose)

        def request():
            send(label + ' synthetic request\r', 'submit fixture request')
            wait(response)

        try:
            action(send, wait, choose, request, screen)
            process.wait(timeout=15)
            pump()
            check(label + '-process', process.returncode == (2 if label == 'home-input-eio' else 0), exit_code=process.returncode)
            if label == 'home-input-eio':
                flags = termios.tcgetattr(master)[3]
                check(label + '-terminal-restored', bool(flags & termios.ICANON) and bool(flags & termios.ECHO))
        except Exception as error:
            check(label + '-process', False, error=repr(error), exit_code=process.poll(),
                last_event=events[-1] if events else None)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            pump()
            os.close(master)
            (evidence / (label + '.raw')).write_bytes(raw)
            (evidence / (label + '.txt')).write_text(screen())
            (evidence / (label + '-actions.json')).write_text(json.dumps(events, ensure_ascii=False, indent=2))
        print('END ' + label + ': ' + checks[-1]['status'], flush=True)
        return {'screen': screen(), 'ui': rows(ui_trace), 'requests': rows(provider_trace)}

    def read_only(label, action, extra=None):
        if label not in selected:
            return None
        before = snapshot(root)
        result = run(label, extra or [], home if label != 'explicit-resume-picker' else launch, action)
        check(label + '-no-files', snapshot(root) == before)
        check(label + '-no-runtime-or-model', not result['ui'] and not result['requests'])
        return result

    def cancel(send, wait, choose, request, screen):
        wait('打开其他目录'); wait('浏览全部历史对话')
        for project in (project_a, project_b, empty, home):
            assert str(project) in screen(), 'Missing selectable project: ' + str(project)
        send(b'\x1b', 'cancel project menu from arbitrary cwd')

    read_only('home-cancel', cancel)

    def input_eio(send, wait, choose, request, screen):
        wait('BioMind 终端输入失败')
        assert 'SYNTHETIC_MENU_INPUT' in screen()

    read_only('home-input-eio', input_eio)

    def empty_back(send, wait, choose, request, screen):
        wait('浏览全部历史对话'); choose(2, 'choose empty registered project')
        wait('新建对话')
        assert '继续最近对话' not in screen(fresh=True), 'Empty project offered nonexistent recent history'
        send(b'\x1b', 'return from empty project actions')
        wait('浏览全部历史对话'); send(b'\x1b', 'cancel project menu')

    read_only('empty-project-back', empty_back)

    def history_back(send, wait, choose, request, screen):
        wait('浏览全部历史对话'); choose(0, 'choose project A')
        wait('选择历史对话'); choose(2, 'open native project history')
        wait('ENTRY_A_HISTORY'); send(b'\x1b', 'return from history to session actions')
        wait('选择历史对话'); send(b'\x1b', 'return from session actions to projects')
        wait('浏览全部历史对话'); send(b'\x1b', 'cancel project menu')

    read_only('history-back-navigation', history_back)

    def open_back(send, wait, choose, request, screen):
        wait('浏览全部历史对话'); choose(4, 'open directory input')
        wait('输入目录'); send(b'\x1b', 'cancel directory input')
        wait('浏览全部历史对话'); send(b'\x1b', 'cancel project menu')

    read_only('open-directory-back', open_back)

    def create_back(send, wait, choose, request, screen):
        wait('浏览全部历史对话'); choose(5, 'open create project input')
        wait('输入目录'); send(str(root / 'NOT_CREATED') + '\r', 'enter unused target')
        wait('确认创建'); send(b'\x1b', 'cancel project creation confirmation')
        wait('浏览全部历史对话'); send(b'\x1b', 'cancel project menu')

    read_only('create-project-back', create_back)

    def all_history_back(send, wait, choose, request, screen):
        wait('浏览全部历史对话'); choose(6, 'open global native history')
        wait('ENTRY_A_HISTORY'); wait('ENTRY_B_HISTORY')
        send(b'\x1b', 'return from all history to projects')
        wait('浏览全部历史对话'); send(b'\x1b', 'cancel project menu')

    read_only('all-history-back', all_history_back)

    def all_history_resume(send, wait, choose, request, screen):
        wait('浏览全部历史对话'); choose(6, 'open all projects history directly')
        wait('ENTRY_B_HISTORY')
        send('"ENTRY_B_HISTORY"', 'search native B history')
        send(b'\r', 'resume B from unrelated launch directory')
        wait('execution:'); wait('ENTRY_B_HISTORY_RESTORED')
        assert str(project_b) in screen(fresh=True), 'Resumed welcome did not bind project B'
        send('/exit\r', 'exit restored B session without model request')

    before = set(sessions.glob('*.jsonl'))
    result = run('all-history-cross-project', [], home, all_history_resume)
    if result is not None:
        ids = {entry['sessionId'] for entry in result['ui'] if entry.get('type') == 'session_start'}
        check('all-history-same-session', ids == {saved['ENTRY_B_HISTORY']['id']}, session_ids=sorted(ids))
        check('all-history-no-extra-session', set(sessions.glob('*.jsonl')) == before)
        check('all-history-no-model', not result['requests'])

    def open_new(send, wait, choose, request, screen):
        wait('浏览全部历史对话'); choose(4, 'open another directory')
        wait('输入目录'); send(str(other) + '\r', 'choose an existing unregistered directory')
        wait('新建对话'); choose(0, 'start a new conversation in that directory')
        wait('execution:native'); request(); send('/exit\r', 'exit new conversation')

    before = set(sessions.glob('*.jsonl'))
    result = run('open-directory-new-conversation', [], home, open_new)
    if result is not None:
        added = set(sessions.glob('*.jsonl')) - before
        headers = [json.loads(filename.read_text().splitlines()[0]) for filename in added]
        check('open-directory-selected-cwd', len(headers) == 1 and headers[0].get('cwd') == str(other), headers=headers)
        check('open-directory-clean-context', bool(result['requests']) and 'ENTRY_A_HISTORY' not in json.dumps(result['requests'])
            and 'ENTRY_B_HISTORY' not in json.dumps(result['requests']))
        check('open-directory-no-project-instructions', not (other / 'AGENTS.md').exists())

    def explicit_empty(send, wait, choose, request, screen):
        wait('execution:native')
        assert '浏览全部历史对话' not in screen() and '项目对话：' not in screen(), 'Explicit empty project opened a startup menu'
        send('/exit\r', 'exit directly selected empty project')

    run('explicit-empty-project-direct', ['--project-dir', str(empty)], launch, explicit_empty)
    run('cwd-first-direct', [], launch, explicit_empty)

    def explicit_session(send, wait, choose, request, screen):
        wait('execution:'); wait('ENTRY_A_HISTORY_RESTORED')
        assert '浏览全部历史对话' not in screen(), 'Explicit session opened project menu'
        send('/exit\r', 'exit explicit session')

    result = run('explicit-session-direct', ['--session', saved['ENTRY_A_HISTORY']['path']], launch, explicit_session)
    if result is not None:
        check('explicit-session-preserved', {entry['sessionId'] for entry in result['ui'] if entry.get('type') == 'session_start'}
            == {saved['ENTRY_A_HISTORY']['id']})

    def explicit_resume(send, wait, choose, request, screen):
        wait('Current Folder')
        assert '浏览全部历史对话' not in screen(), 'Explicit resume opened project menu'
        send(b'\t', 'native all-project scope'); wait('ENTRY_B_HISTORY')
        send(b'\x1b', 'cancel explicit native history selector')

    read_only('explicit-resume-picker', explicit_resume, ['--resume'])

    for label, extra in [('resume-nontty', ['--resume']), ('resume-json', ['--resume', '--pi-mode', 'json'])]:
        if label not in selected:
            continue
        command = base + extra
        commands.append({'scenario': label, 'argv': command, 'cwd': str(launch)})
        completed = subprocess.run(command, cwd=launch, env=env, input='', capture_output=True, text=True, timeout=30)
        (evidence / (label + '.txt')).write_text(completed.stdout + completed.stderr)
        check(label + '-still-rejected', completed.returncode == 2, exit_code=completed.returncode)

    guard = rows(evidence / 'guard.jsonl')
    check('offline-no-network-attempts', not any(row.get('kind') == 'network_attempt' for row in guard))
    check('offline-no-real-credentials', not any(row.get('kind') == 'sensitive_file_attempt' and not row.get('allowed') for row in guard))
    if selected & NATIVE_SCENARIOS:
        check('native-reuses-embedded-sdk', any(row.get('kind') == 'native_embedded_sdk_guard' for row in guard)
            and not any(row.get('kind') == 'duplicate_sdk_import' for row in guard))
    result = {'status': 'PASS' if all(row['status'] == 'PASS' for row in checks) else 'FAIL',
        'checks': checks, 'commands': commands, 'synthetic_root': str(root), 'evidence_type': 'PRODUCT_TUI',
        'selected_scenarios': [label for label in SCENARIOS if label in selected],
        'host_sha256': hashlib.sha256((runtime / 'dist/host.js').read_bytes()).hexdigest(),
        'project_launcher_sha256': hashlib.sha256((runtime / 'dist/project-launcher.js').read_bytes()).hexdigest()}
    (evidence / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Offline acceptance of the real BioMind startup history menus and Pi TUI.

Creates only synthetic sessions, settings and domain state in a fresh temporary
root. The existing offline guard denies network and real credential reads.
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


def rows(filename):
    return [json.loads(line) for line in filename.read_text().splitlines() if line.strip()] if filename.exists() else []


def snapshot(root):
    return {str(filename.relative_to(root)): hashlib.sha256(filename.read_bytes()).hexdigest()
            for filename in root.rglob('*') if filename.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if any(evidence.iterdir()):
        parser.error('Use an empty evidence directory')
    repo = Path(__file__).resolve().parents[3]
    runtime = repo / 'integrations/pi_runtime'
    node = runtime / '.runtime/node/bin/node'
    root = Path(tempfile.mkdtemp(prefix='biomind-startup-history-'))
    project_a, project_b, empty = [root / name for name in ('A 中文 项目', 'B 历史 项目', 'C 空项目')]
    agent, home, sessions = [root / name for name in ('agent', 'home', 'sessions')]
    for directory in (project_a, project_b, empty, agent, home, sessions, root / 'tmp'):
        directory.mkdir()
    (agent / 'settings.json').write_text(json.dumps({'enableAnalytics': False, 'enableInstallTelemetry': False,
        'retry': {'enabled': False}, 'compaction': {'enabled': False}, 'cacheWarming': 'off'}))
    (agent / 'trust.json').write_text(json.dumps({str(project): True for project in (project_a, project_b, empty)}))
    registry = root / 'registry.json'
    registry.write_text('{"environments":{},"tools":{}}')
    preferences = root / 'preferences.md'
    preferences.write_text('合成历史入口验收；仅回答，不执行科研作业。\n')
    fixture = root / 'provider.json'
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

    # Real BioMind turns establish both the native JSONL and its authoritative
    # request ledger. A hand-written plain Pi transcript would start a new task
    # boundary on its first BioMind request and cannot witness task continuation.
    saved = {}
    for project, name in [(project_a, 'HISTORY_A_OLDER'), (project_a, 'HISTORY_A_RECENT'), (project_b, 'HISTORY_B_ONLY')]:
        print('START seed-' + name, flush=True)
        fixture.write_text(json.dumps([{'content': [{'type': 'text', 'text': name + '_ASSISTANT_RESTORED'}],
            'trace_path': str(evidence / (name + '-seed-provider.jsonl'))}]))
        command = base + ['--project-dir', str(project), '--pi-mode', 'json', '--execution-profile', 'protected',
                          '--prompt', name + '_USER_CONTEXT']
        commands.append({'scenario': 'seed-' + name, 'argv': command, 'cwd': str(project)})
        process = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        raw, pending, completed = bytearray(), b'', None
        deadline = time.monotonic() + 90
        try:
            while completed is None:
                if time.monotonic() > deadline:
                    raise TimeoutError('BioMind seed result: ' + name)
                if select.select([process.stdout], [], [], .1)[0]:
                    part = os.read(process.stdout.fileno(), 65536)
                    if not part:
                        raise RuntimeError('Seed exited before result: ' + name)
                    raw.extend(part); pending += part
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        try:
                            frame = json.loads(line)
                        except ValueError:
                            continue
                        if frame.get('type') == 'result':
                            completed = frame
                elif process.poll() is not None:
                    raise RuntimeError(f'Seed exited {process.returncode}: {name}')
            assert completed['runtime_status'] == 'completed', completed
            assert completed['model_conclusion'] == name + '_ASSISTANT_RESTORED', completed
            process.stdin.write(b'{"type":"shutdown"}\n'); process.stdin.flush()
            remaining, _ = process.communicate(timeout=15)
            raw.extend(remaining)
            assert process.returncode == 0, process.returncode
            saved[name] = {'id': completed['sessionId'], 'path': completed['sessionFile'], 'cwd': str(project)}
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()
            (evidence / (name + '-seed.jsonl')).write_bytes(raw)
        print('END seed-' + name + ': PASS', flush=True)

    naming_script = root / 'name-sessions.mjs'
    naming_script.write_text('import fs from "node:fs";\nimport {SessionManager} from '
        + json.dumps((runtime / 'node_modules/@earendil-works/pi-coding-agent/dist/index.js').as_uri()) + ';\n'
        + 'const saved=' + json.dumps(saved) + '; let i=0;\n'
        + 'for(const [name,row] of Object.entries(saved)){const m=SessionManager.open(row.path);\n'
        + 'm.appendSessionInfo(name);const stamp=Date.now()/1000-300+(i++*100);fs.utimesSync(row.path,stamp,stamp);}\n')
    subprocess.run([str(node), str(naming_script)], env=env, capture_output=True, text=True, timeout=90, check=True)
    (evidence / 'seeded-sessions.json').write_text(json.dumps(saved, ensure_ascii=False, indent=2))

    def check(identity, condition, **details):
        checks.append({'id': identity, 'status': 'PASS' if condition else 'FAIL', **details})

    def run(label, extra, cwd, action, tool_steps=None):
        print('START ' + label, flush=True)
        provider_trace, ui_trace = evidence / (label + '-provider.jsonl'), evidence / (label + '-ui.jsonl')
        response = label.upper().replace('-', '_') + '_DONE'
        script = [*(tool_steps or []), {'content': [{'type': 'text', 'text': response}]}]
        script[0]['trace_path'] = str(provider_trace)
        fixture.write_text(json.dumps(script))
        scenario_env = {**env, 'BIOMIND_PI_UI_TRACE': str(ui_trace)}
        command = base + extra
        commands.append({'scenario': label, 'argv': command, 'cwd': str(cwd)})
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 160, 0, 0))
        process = subprocess.Popen(command, cwd=cwd, env=scenario_env, stdin=slave, stdout=slave,
                                   stderr=slave, close_fds=True, start_new_session=True)
        started = time.monotonic()
        os.close(slave)
        raw, events = bytearray(), []

        def screen():
            return ANSI.sub('', raw.decode(errors='replace'))

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
            while marker not in screen():
                if process.poll() is not None:
                    raise RuntimeError(f'Exited {process.returncode} before {marker}')
                if time.monotonic() > until:
                    raise TimeoutError(marker)
                pump()
            events.append({'seen': marker, 'raw_offset': len(raw), 'elapsed_seconds': round(time.monotonic() - started, 3)})

        def send(keys, purpose):
            os.write(master, keys.encode() if isinstance(keys, str) else keys)
            events.append({'action': purpose, 'raw_offset': len(raw), 'elapsed_seconds': round(time.monotonic() - started, 3)})
            pump(.25)

        def request():
            send(label + ' synthetic continuation\r', 'submit synthetic request')
            wait(response)

        try:
            action(send, wait, request, screen)
            process.wait(timeout=15)
            pump()
            check(label + '-process', process.returncode == 0, exit_code=process.returncode)
        except Exception as error:
            check(label + '-process', False, error=repr(error))
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

    def session_ids(result):
        return {entry['sessionId'] for entry in result['ui'] if entry.get('type') == 'session_start'}

    def verify_resume(label, result, name, before):
        selected = saved[name]
        check(label + '-same-session', session_ids(result) == {selected['id']}, session_ids=sorted(session_ids(result)))
        check(label + '-restored-transcript', name + '_ASSISTANT_RESTORED' in result['screen'])
        check(label + '-context-reused', name + '_USER_CONTEXT' in json.dumps(result['requests']))
        check(label + '-no-extra-session', set(sessions.glob('*.jsonl')) == before)
        check(label + '-retained-profile', 'execution:protected' in result['screen'])
        check(label + '-continued-original-file', label + ' synthetic continuation' in Path(selected['path']).read_text())

    # Cancel before services start: the only changing file is this harness's fixture.
    def cancel_menu(send, wait, request, screen):
        wait('选择历史对话'); send(b'\x1b', 'cancel startup conversation menu')

    before = snapshot(root)
    result = run('cancel-menu', ['--project-dir', str(project_a)], project_a, cancel_menu)
    after = snapshot(root)
    before.pop('provider.json', None); after.pop('provider.json', None)
    check('cancel-menu-no-files', before == after)
    check('cancel-menu-no-provider-or-runtime', not result['requests'] and not result['ui'])

    def cancel_picker(send, wait, request, screen):
        wait('HISTORY_A_RECENT'); send(b'\x1b', 'cancel native history selector')

    before = snapshot(root)
    result = run('cancel-picker', ['--project-dir', str(project_a), '--resume'], project_a, cancel_picker)
    after = snapshot(root)
    before.pop('provider.json', None); after.pop('provider.json', None)
    check('cancel-picker-no-files', before == after)
    check('cancel-picker-no-provider-or-runtime', not result['requests'] and not result['ui'])

    def continue_recent(send, wait, request, screen):
        wait('继续最近对话'); send(b'\x1b[B', 'choose continue recent'); send(b'\r', 'confirm continuation')
        wait('execution:protected'); wait('HISTORY_A_RECENT_ASSISTANT_RESTORED')
        request(); send('/exit\r', 'exit resumed conversation')

    before_files = set(sessions.glob('*.jsonl'))
    result = run('continue-recent', ['--project-dir', str(project_a)], project_a, continue_recent)
    verify_resume('continue-recent', result, 'HISTORY_A_RECENT', before_files)

    def project_history(send, wait, request, screen):
        wait('创建项目'); send(b'\r', 'choose first project A')
        wait('选择历史对话'); send(b'\x1b[B', 'move to continue'); send(b'\x1b[B', 'move to history')
        send(b'\r', 'open native history selector'); wait('HISTORY_A_OLDER')
        send('"HISTORY_A_OLDER"', 'search exact historical conversation name'); send(b'\r', 'resume selected history')
        wait('execution:protected'); wait('HISTORY_A_OLDER_ASSISTANT_RESTORED')
        request(); send('/exit\r', 'exit resumed conversation')

    before_files = set(sessions.glob('*.jsonl'))
    result = run('project-history', ['project'], project_a, project_history)
    verify_resume('project-history', result, 'HISTORY_A_OLDER', before_files)

    def cross_project(send, wait, request, screen):
        wait('Current Folder'); send(b'\t', 'show all projects'); wait('HISTORY_B_ONLY')
        send('"HISTORY_B_ONLY"', 'search exact B history name'); send(b'\r', 'resume B before domain initialization')
        wait('execution:protected'); wait('HISTORY_B_ONLY_ASSISTANT_RESTORED')
        request(); send('/project list\r', 'inspect selected project root')
        wait('当前项目'); send('/exit\r', 'exit B conversation')

    before_files = set(sessions.glob('*.jsonl'))
    result = run('cross-project', ['--resume'], empty, cross_project)
    verify_resume('cross-project', result, 'HISTORY_B_ONLY', before_files)
    selected_task = 'pi-' + hashlib.sha256(saved['HISTORY_B_ONLY']['id'].encode()).hexdigest()[:24]
    selected_state = project_b / '.biomind/pi-domain/task_outputs' / selected_task / 'plan_state.json'
    check('cross-project-root', str(project_b) in result['screen'] and selected_state.exists(), state_file=str(selected_state))

    def scoped_picker(send, wait, request, screen):
        wait('HISTORY_A_OLDER'); send(b'\t', 'attempt scope toggle with explicit A project')
        assert 'HISTORY_B_ONLY' not in screen(), 'Explicit project selector exposed B history'
        send('HISTORY_B_ONLY', 'search forbidden B history'); send(b'\x1b', 'cancel scoped selector')

    result = run('scoped-picker', ['--project-dir', str(project_a), '--resume'], project_a, scoped_picker)
    check('explicit-project-no-cross-session', 'HISTORY_B_ONLY_ASSISTANT_RESTORED' not in result['screen']
          and not result['ui'] and not result['requests'])

    def new_conversation(send, wait, request, screen):
        wait('新建对话'); send(b'\r', 'start a new persistent conversation')
        wait('execution:native'); request(); send('/exit\r', 'exit new conversation')

    before_files = set(sessions.glob('*.jsonl'))
    scratch = project_a / 'startup-tool-check.txt'
    tool_steps = [{'content': [{'type': 'toolCall', 'id': identity, 'name': name, 'arguments': arguments}]}
        for identity, name, arguments in [
            ('startup-write', 'write', {'path': str(scratch), 'content': 'STARTUP_FILE_OK'}),
            ('startup-read', 'read', {'path': str(scratch)}),
            ('startup-bash', 'bash', {'command': 'printf STARTUP_BASH_OK'}),
        ]]
    result = run('new-conversation', ['--project-dir', str(project_a)], project_a, new_conversation, tool_steps)
    check('new-conversation-new-id', len(session_ids(result)) == 1 and not session_ids(result).intersection(s['id'] for s in saved.values()))
    check('new-conversation-saved', len(set(sessions.glob('*.jsonl')) - before_files) == 1)
    check('new-conversation-clean-context', 'HISTORY_A_' not in json.dumps(result['requests']))
    check('new-conversation-default-profile', 'execution:native' in result['screen'])
    receipts = {message['toolCallId']: message for row in result['requests'] for message in row['context']['messages']
                if message.get('role') == 'toolResult'}
    check('bundled-extension-native-tools', set(receipts) == {'startup-write', 'startup-read', 'startup-bash'}
          and all(not receipt.get('isError') for receipt in receipts.values())
          and 'STARTUP_FILE_OK' in json.dumps(receipts.get('startup-read'))
          and 'STARTUP_BASH_OK' in json.dumps(receipts.get('startup-bash')))
    check('bundled-extension-file-effect', scratch.exists() and scratch.read_text() == 'STARTUP_FILE_OK')

    def no_history(send, wait, request, screen):
        wait('execution:native'); request(); send('/exit\r', 'exit first conversation in empty project')

    result = run('no-history', ['--project-dir', str(empty)], empty, no_history)
    check('no-history-direct-start', '选择历史对话' not in result['screen'] and len(session_ids(result)) == 1)
    check('no-history-clean-context', 'HISTORY_' not in json.dumps(result['requests']))

    def explicit_session(send, wait, request, screen):
        wait('execution:protected'); wait('HISTORY_A_OLDER_ASSISTANT_RESTORED')
        send('/exit\r', 'exit explicitly selected session')

    result = run('explicit-session', ['--project-dir', str(project_a), '--session', saved['HISTORY_A_OLDER']['path']], project_a, explicit_session)
    check('explicit-session-bypasses-menu', '选择历史对话' not in result['screen']
          and session_ids(result) == {saved['HISTORY_A_OLDER']['id']})

    for label, extra in [('resume-nontty', ['--resume']), ('resume-json', ['--resume', '--pi-mode', 'json'])]:
        command = base + extra
        commands.append({'scenario': label, 'argv': command, 'cwd': str(project_a)})
        try:
            completed = subprocess.run(command, cwd=project_a, env=env, input='', capture_output=True, text=True, timeout=30)
            (evidence / (label + '.txt')).write_text(completed.stdout + completed.stderr)
            check(label + '-rejected', completed.returncode == 2, exit_code=completed.returncode)
        except Exception as error:
            check(label + '-rejected', False, error=repr(error))

    guard = rows(evidence / 'guard.jsonl')
    check('native-reuses-embedded-sdk', any(row.get('kind') == 'native_embedded_sdk_guard' for row in guard)
          and not any(row.get('kind') == 'duplicate_sdk_import' for row in guard))
    check('offline-no-network-attempts', not any(row.get('kind') == 'network_attempt' for row in guard))
    check('offline-no-real-credentials', not any(row.get('kind') == 'sensitive_file_attempt' and not row.get('allowed') for row in guard))
    result = {'status': 'PASS' if all(row['status'] == 'PASS' for row in checks) else 'FAIL',
        'checks': checks, 'commands': commands, 'synthetic_root': str(root), 'evidence_type': 'PRODUCT_TUI',
        'host_sha256': hashlib.sha256((runtime / 'dist/host.js').read_bytes()).hexdigest(),
        'native_extension_sha256': hashlib.sha256((runtime / 'dist/biomind-extension.ts').read_bytes()).hexdigest(),
        'native_fixture_sha256': hashlib.sha256((runtime / 'dist/native-fixture.ts').read_bytes()).hexdigest()}
    (evidence / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Execute BioMind's saved-session command verbatim with isolated launch settings."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import tempfile
import termios
import time


def read_jsonl(filename):
    return [json.loads(line) for line in filename.read_text().splitlines() if line.strip()] if filename.exists() else []


def fixture_processes(root_pid):
    """Only descendants of this fixture; record a narrow noncredential projection."""
    queue, seen, found = [root_pid], set(), []
    keys = {'PI_CODING_AGENT_DIR', 'BIOMIND_PI_CONFIG_PATH', 'BIOMIND_PI_DATA_ROOT',
            'BIOMIND_PI_NODE', 'BIOMIND_PI_MODEL_RECORD', 'BIOMIND_PI_MODEL_SELECTION'}
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            queue.extend(int(value) for value in Path(f'/proc/{pid}/task/{pid}/children').read_text().split())
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().decode().rstrip('\0').split('\0')
            if not any(value.endswith(('/dist/bundle/cli.js', '/python_service.py')) for value in argv):
                continue
            environment = dict(value.split('=', 1) for value in
                Path(f'/proc/{pid}/environ').read_bytes().decode().split('\0') if '=' in value)
            found.append({'pid': pid, 'argv': argv, 'environment': {key: environment[key] for key in keys if key in environment}})
        except (OSError, ValueError):
            pass
    return found


def run_terminal(command, cwd, env, evidence, name):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 42, 220, 0, 0))
    before = termios.tcgetattr(slave)

    def controlling_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    # The exact generated string is passed to bash without prefixed/appended flags.
    process = subprocess.Popen(['/bin/bash', '-c', command], env=env, cwd=cwd,
        stdin=slave, stdout=slave, stderr=slave, close_fds=True, preexec_fn=controlling_terminal)
    raw, observed, started, ready_since = bytearray(), {}, time.monotonic(), None
    quit_sent = False
    try:
        while process.poll() is None and time.monotonic() - started < 50:
            if select.select([master], [], [], .05)[0]:
                try:
                    raw.extend(os.read(master, 65536))
                except OSError:
                    break
            for row in fixture_processes(process.pid):
                observed[row['pid']] = row
            if ready_since is None and any(label in raw.decode(errors='replace') for label in ('直接输入问题即可开始', '待输入')):
                ready_since = time.monotonic()
            if not quit_sent and ready_since is not None and time.monotonic() - ready_since > .5:
                quit_sent = True
                os.write(master, b'/quit\r')
        timed_out = process.poll() is None
        if timed_out:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        while select.select([master], [], [], .05)[0]:
            try:
                data = os.read(master, 65536)
                if not data:
                    break
                raw.extend(data)
            except OSError:
                break
        flags = termios.ICANON | termios.ECHO | termios.ISIG
        restored = before[3] & flags == termios.tcgetattr(slave)[3] & flags
    finally:
        os.close(master)
        os.close(slave)
    text = raw.decode(errors='replace')
    (evidence / f'{name}-terminal.txt').write_text(text)
    (evidence / f'{name}-processes.json').write_text(json.dumps(list(observed.values()), ensure_ascii=False, indent=2))
    plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
    commands = [line[line.index('biomind --project-dir'):].strip() for line in plain.splitlines() if 'biomind --project-dir' in line]
    return {'exit': process.returncode, 'elapsed_seconds': time.monotonic() - started, 'timed_out': timed_out,
            'ready': ready_since is not None, 'raw_restored': restored,
            'resume_command': commands[-1] if commands else None, 'processes': list(observed.values())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    runtime = Path(__file__).resolve().parents[1]
    repo = runtime.parents[1]
    node = runtime / '.runtime/node/bin/node'
    python = Path('/public/home/liusiqi/anaconda3/envs/biomind/bin/python')
    root = Path(tempfile.mkdtemp(prefix='biomind-recovery-context-'))
    home, control = root / 'home', root / 'launch-control'
    project = root / "中文 空格 ' $(unused) 项目"
    agent = root / "自定义 agent ' directory"
    data_root = root / "自定义 domain ' data"
    for directory in [home, control, project, agent]:
        directory.mkdir()
    (agent / 'settings.json').write_text(json.dumps({'quietStartup': True, 'enableAnalytics': False,
        'enableInstallTelemetry': False, 'cacheWarming': 'off', 'fullscreenExitOutput': 'resume-hint'}))
    binary = control / 'bin'
    binary.mkdir()
    (binary / 'biomind').symlink_to(repo / 'bin/biomind')
    # The executable path has shell punctuation, so this also exercises quoting.
    node_alias = control / "node ' selected"
    node_alias.symlink_to(node.resolve())
    record = control / "nondefault 模型 ' record.json"
    selection = {'schema': 'biomind-active-model-config-v2', 'provider': 'deepseek', 'model': 'deepseek-v4-flash',
        'api': 'openai-completions', 'thinking': 'off', 'endpoint_hostname': 'api.deepseek.com',
        'endpoint_base_url': 'https://api.deepseek.com/v1', 'origin': 'user-confirmed-active-product-config', 'confirmed_active': True}
    record.write_text(json.dumps(selection))
    preferences, registry = control / 'preferences.md', control / 'registry.json'
    preferences.write_text('Synthetic recovery acceptance preferences.\n')
    registry.write_text('{"environments":{},"tools":{}}')
    config = control / "runtime 配置 ' settings.json"
    config.write_text(json.dumps({'python': str(python), 'agentDir': str(agent), 'dataRoot': str(data_root),
        'continueSession': True, 'executionProfile': 'protected', 'registryPath': str(registry),
        'preferencesPath': str(preferences), 'memoryRoot': str(control / 'memory'),
        'archivePath': str(control / 'archive.sqlite3'), 'historyEnabled': False, 'memoryMode': 'disabled'}))
    # Reuse the strict test-only offline guard, resolving installed SDK sources
    # through candidate dependency symlinks. No production source is modified.
    guard = control / 'offline-guard.mjs'
    guard_source = (runtime / 'testing/offline-guard.mjs').read_text()
    guard_source = guard_source.replace('fileURLToPath(new URL(relative, import.meta.url))',
        '(name => { try { return realpath(name); } catch { return name; } })(fileURLToPath(new URL(relative, '
        + json.dumps((runtime / 'testing/offline-guard.mjs').as_uri()) + ')))')
    guard.write_text(guard_source)
    trace = evidence / 'offline-guard.jsonl'
    env = {'PATH': str(binary) + ':' + str(python.parent) + ':' + str(node.parent) + ':/usr/bin:/bin',
        'HOME': str(home), 'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'BIOMIND_DISABLE_DOTENV': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
        'PI_OFFLINE': '1', 'PI_SKIP_VERSION_CHECK': '1', 'NODE_OPTIONS': '--import=' + str(guard),
        'BIOMIND_PI_TEST_ROOT': str(root), 'BIOMIND_PI_GUARD_TRACE': str(trace), 'BIOMIND_PI_TRACE_SYNTHETIC_READS': '1'}
    forbidden = {'PI_CODING_AGENT_DIR', 'PI_CODING_AGENT_SESSION_DIR', 'BIOMIND_PI_CONFIG_PATH', 'BIOMIND_PI_NODE',
        'BIOMIND_PI_MODEL_RECORD', 'BIOMIND_PI_DATA_ROOT', 'BIOMIND_PI_MODEL_SELECTION', 'BIOMIND_PI_PYTHON'}
    assert not (forbidden & env.keys())
    # Seed data only; both tested launches receive the clean environment above.
    seed = control / 'seed.mjs'
    seed.write_text('import {SessionManager} from ' + json.dumps((runtime / 'node_modules/@earendil-works/pi-coding-agent/dist/index.js').as_uri()) + ';\n'
        + 'const m=SessionManager.create(' + json.dumps(str(project)) + ');\n'
        + 'm.appendMessage({role:"user",content:"SYNTHETIC saved input",timestamp:Date.now()});\n'
        + 'm.appendMessage({role:"assistant",content:[{type:"text",text:"SYNTHETIC saved response"}],timestamp:Date.now(),'
          'api:"openai-completions",provider:"deepseek",model:"deepseek-v4-flash",stopReason:"stop",'
          'usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});\n'
        + 'console.log(m.getSessionFile());\n')
    seeded = subprocess.run([str(node), str(seed)], cwd=project,
        env={**env, 'PI_CODING_AGENT_DIR': str(agent)}, capture_output=True, text=True, timeout=30, check=True)
    session = Path(seeded.stdout.strip())
    initial_entries = read_jsonl(session)
    identity = initial_entries[0]['id']
    initial_command = shlex.join(['biomind', '--project-dir', str(project), '--pi-config', str(config),
        '--pi-node', str(node_alias), '--pi-model-record', str(record)])
    initial = run_terminal(initial_command, project, env, evidence, 'initial')
    generated = initial['resume_command']
    report = {'root': str(root), 'candidate_repo': str(repo), 'initial_command': initial_command,
        'generated_command': generated, 'executed_command': generated, 'business_environment_omissions': sorted(forbidden),
        'test_environment': env, 'initial': initial, 'session': str(session), 'session_id': identity,
        'expected_agent_dir': str(agent), 'expected_data_root': str(data_root), 'expected_model_record': str(record),
        'expected_node': str(node.resolve()), 'real_TAD_touched': False}
    recovery = run_terminal(generated, control, env, evidence, 'recovery') if generated else None
    report['recovery'] = recovery
    task = 'pi-' + hashlib.sha256(identity.encode()).hexdigest()[:24]
    state_file = data_root / 'task_outputs' / task / 'plan_state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    report['domain_state_file'] = str(state_file)
    report['domain_profile_binding'] = state.get('task_state', {}).get('pi_execution_profile')
    final_entries = read_jsonl(session)
    report['message_counts'] = [sum(entry['type'] == 'message' for entry in entries) for entries in (initial_entries, final_entries)]
    report['default_agent_written'] = (home / '.pi/agent').exists()
    report['default_domain_written'] = (project / '.biomind/pi-domain').exists()
    events = read_jsonl(trace)
    report['network_attempts'] = [event for event in events if event.get('kind') == 'network_attempt']
    report['denied_credential_reads'] = [event for event in events if event.get('kind') == 'sensitive_file_attempt' and not event.get('allowed')]
    def context_ok(run):
        if not run or run['exit'] or not run['ready'] or not run['raw_restored']:
            return False
        native = [row for row in run['processes'] if any(value.endswith('/dist/bundle/cli.js') for value in row['argv'])]
        domain = [row for row in run['processes'] if any(value.endswith('/python_service.py') for value in row['argv'])]
        if not native or not domain:
            return False
        values = native[0]['environment']
        expected = {'PI_CODING_AGENT_DIR': str(agent), 'BIOMIND_PI_DATA_ROOT': str(data_root),
            'BIOMIND_PI_CONFIG_PATH': str(config), 'BIOMIND_PI_MODEL_RECORD': str(record), 'BIOMIND_PI_NODE': str(node.resolve())}
        if any(values.get(key) != value for key, value in expected.items()):
            return False
        if json.loads(values.get('BIOMIND_PI_MODEL_SELECTION', '{}')) != selection:
            return False
        argv = domain[0]['argv']
        return all(flag in argv and argv[argv.index(flag) + 1] == value for flag, value in
            [('--data-root', str(data_root)), ('--cwd', str(project)), ('--session-id', identity)])
    checks = {'initial_context': context_ok(initial), 'recovery_context': context_ok(recovery),
        'command_verbatim': bool(generated) and report['generated_command'] == report['executed_command'],
        'domain_binding': report['domain_profile_binding'] is not None and report['domain_profile_binding'].get('session_id') == identity,
        'no_replay': report['message_counts'] == [2, 2], 'same_session': final_entries[0]['id'] == identity,
        'no_default_directory_writes': not report['default_agent_written'] and not report['default_domain_written'],
        'offline': not report['network_attempts'] and not report['denied_credential_reads']}
    report['checks'] = checks
    report['status'] = 'PASS' if all(checks.values()) else 'FAIL'
    (evidence / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'status': report['status'], 'checks': checks, 'root': str(root), 'summary': str(evidence / 'summary.json')}, ensure_ascii=False))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

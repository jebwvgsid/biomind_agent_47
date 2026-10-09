#!/usr/bin/env python3
"""S08/S09/U06: saved trust, final domain roots and passive credential status.

Uses the existing Product JSON driver, POSIX relay, local provider and PTY
primitives. Every preference, HOME, auth/model store and archive is synthetic.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import select
import selectors
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parents[1]
spec = importlib.util.spec_from_file_location('memory_driver', RUNTIME / 'testing/project-memory-witness.py')
driver = importlib.util.module_from_spec(spec); spec.loader.exec_module(driver)
ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]')


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--command-credential', action='store_true', help='Optional passive command credential witness')
    parser.add_argument('--trust', choices=['both', 'trusted', 'untrusted'], default='both')
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve(); evidence.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='biomind-settings-scope-'))
    for name in ['home', 'agent-custom-B', 'agent-old-A', 'skills', 'A', 'B', 'sessions']:
        (root / name).mkdir()
    a, b, agent = root / 'A', root / 'B', root / 'agent-custom-B'
    (a / 'AGENTS.md').write_text('PROJECT_A_ONLY_MUST_NOT_ENTER\n')
    (b / 'AGENTS.md').write_text('PROJECT_B_ONLY_SCOPE\n')
    (root / 'pref-A.md').write_text('GLOBAL_PREF_A_MUST_NOT_ENTER\n')
    (root / 'pref-B.md').write_text('GLOBAL_PREF_B_SCOPE\n')
    (root / 'registry-B.json').write_text('{"environments":{},"tools":{}}')
    (b / '.pi').mkdir()
    (b / '.pi/settings.json').write_text(json.dumps({'quietStartup': True, 'enableSkillCommands': False, 'autocompleteMaxVisible': 3}))
    (agent / 'settings.json').write_text(json.dumps({'quietStartup': False, 'defaultProjectTrust': 'ask',
        'enableAnalytics': False, 'enableInstallTelemetry': False, 'retry': {'enabled': False}, 'cacheWarming': 'off'}))
    command_marker = root / 'CREDENTIAL_COMMAND_SHOULD_NOT_RUN'
    credential_command = '!' + shlex.quote(sys.executable) + ' -c ' + shlex.quote('from pathlib import Path;Path(' + repr(str(command_marker)) + ').write_text("SYNTHETIC_COMMAND_EXECUTED");print("synthetic-placeholder")')
    models = {'providers': {'scope-passive-fixture': {
        'baseUrl': 'https://invalid.example.invalid/v1', 'api': 'openai-completions', 'apiKey': credential_command,
        'models': [{'id': 'passive-only', 'name': 'Passive credential fixture', 'reasoning': False,
                    'input': ['text'], 'contextWindow': 32000, 'maxTokens': 512,
                    'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}]}}}
    if args.command_credential: (agent / 'models.json').write_text(json.dumps(models))
    checks = []; launches = []; command_log = []
    def artifact_hashes():
        return {name: hashlib.sha256((RUNTIME / name).read_bytes()).hexdigest()
                for name in ['dist/host.js', 'dist/extensions/biomind.js']}
    settings_hash = hashlib.sha256((b / '.pi/settings.json').read_bytes()).hexdigest()
    guard = evidence / 'guard.jsonl'
    base_env = {'PATH': str(RUNTIME / '.runtime/node/bin') + ':/usr/bin:/bin', 'HOME': str(root / 'home'),
        'XDG_CONFIG_HOME': str(root / 'home/.config'), 'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'PI_OFFLINE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'BIOMIND_DISABLE_DOTENV': '1',
        'BIOMIND_PI_SKILLS_ROOT': str(root / 'skills'), 'PI_CODING_AGENT_DIR': str(root / 'agent-old-A'),
        'BIOMIND_PI_DATA_ROOT': str(a / 'domain-old'), 'BIOMIND_PI_MEMORY_ROOT': str(a / 'memory-old'),
        'BIOMIND_PI_WIKI_ROOT': str(a / 'wiki-old'), 'BIOMIND_PI_ARCHIVE_PATH': str(a / 'archive-old.sqlite3'),
        'BIOMIND_PI_PREFERENCES_PATH': str(root / 'pref-A.md'), 'BIOMIND_PI_PROJECT_ROOT': str(a),
        'NODE_OPTIONS': '--import=' + str(RUNTIME / 'testing/offline-guard.mjs'),
        'BIOMIND_PI_TEST_ROOT': str(root), 'BIOMIND_PI_GUARD_TRACE': str(guard)}

    def setup(name):
        trace = evidence / (name + '-provider.jsonl')
        fixture = root / (name + '-fixture.json')
        fixture.write_text(json.dumps([{'content': [{'type': 'text', 'text': name + '_MODEL_OK'}],
                                       'trace_path': str(trace), 'expect_system_contains': 'GLOBAL_PREF_B_SCOPE'}]))
        launch_trace = evidence / (name + '-domain-launch.jsonl')
        wrapper = root / (name + '-domain-python')
        wrapper.write_text('#!/usr/bin/python3\nimport json,os,sys\n'
            + 'allowed={"--cwd","--data-root","--memory-root","--wiki-root","--preferences-path","--archive-path","--install-root"}\n'
            + 'values={a:sys.argv[i+1] for i,a in enumerate(sys.argv[:-1]) if a in allowed}\n'
            + 'with open(' + repr(str(launch_trace)) + ',"a") as f:f.write(json.dumps({"cwd":os.getcwd(),"flags":values})+"\\n")\n'
            + 'os.execv(' + repr(sys.executable) + ',[' + repr(sys.executable) + ',*sys.argv[1:]])\n')
        wrapper.chmod(0o755)
        config = root / (name + '-config.json')
        configuration = {'python': str(wrapper), 'providerFixture': str(fixture), 'agentDir': str(agent),
            'sessionDir': str(root / 'sessions'), 'dataRoot': './domain-B', 'memoryRoot': str(b / 'memory-B'),
            'preferencesPath': str(root / 'pref-B.md'), 'registryPath': str(root / 'registry-B.json'),
            'archivePath': str(b / 'archive-B.sqlite3'), 'historyEnabled': False, 'memoryMode': 'disabled'}
        config.write_text(json.dumps(configuration))
        (evidence / (name + '-startup-config.json')).write_text(json.dumps(configuration, indent=2) + '\n')
        argv = [sys.executable, '-B', str(REPO / 'bio_agents.py'), '--project-dir', str(b), '--pi-config', str(config),
                '--pi-node', str(RUNTIME / '.runtime/node/bin/node')]
        return argv, trace, launch_trace

    def json_run(name, trusted):
        argv, trace, launch_trace = setup(name)
        product = driver.Product.__new__(driver.Product)
        product.name, product.evidence, product.trace = name, evidence, trace
        product.argv = [sys.executable, '-B', str(RUNTIME / 'testing/posix-pipe-relay.py'), *argv, '--pi-mode', 'json']
        command_log.append({'name': name, 'argv': product.argv, 'cwd': str(a), 'artifact_sha256_before_launch': artifact_hashes()})
        product.process = subprocess.Popen(product.argv, cwd=a, env=base_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        product.selector = selectors.DefaultSelector()
        product.selector.register(product.process.stdout, selectors.EVENT_READ, 'stdout'); product.selector.register(product.process.stderr, selectors.EVENT_READ, 'stderr')
        product.frames, product.pending, product.stderr = [], b'', b''
        product.started, product.ready_ms = time.monotonic(), None
        try:
            ready = product.wait(lambda f: f.get('type') == 'ready')
            assert ready['projectTrusted'] is trusted, ready
            assert ready['cwd'] == str(b) and ready['sessionDir'] == str(root / 'sessions'), ready
            product.prompt(name, 'Synthetic settings and scope witness only'); product.result(name)
            payload = json.dumps(product.requests(), ensure_ascii=False)
            assert 'GLOBAL_PREF_B_SCOPE' in payload and 'PROJECT_B_ONLY_SCOPE' in payload
            assert 'GLOBAL_PREF_A_MUST_NOT_ENTER' not in payload and 'PROJECT_A_ONLY_MUST_NOT_ENTER' not in payload
            checks.append({'id': 'S08-' + name, 'status': 'PASS', 'projectTrusted': ready['projectTrusted'], 'agents_context_loaded': True})
        finally: product.close()
        launches.extend(json.loads(line) for line in launch_trace.read_text().splitlines())

    def tui_run(name, trusted):
        argv, trace, launch_trace = setup(name); raw = bytearray(); actions = []
        master, slave = pty.openpty(); fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 140, 0, 0))
        process = subprocess.Popen(argv, cwd=a, env=base_env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave); command_log.append({'name': name, 'argv': argv, 'cwd': str(a), 'artifact_sha256_at_launch': artifact_hashes()})
        def screen(): return ANSI.sub('', raw.decode(errors='replace'))
        def pump(seconds=.25):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if select.select([master], [], [], .05)[0]:
                    try: part = os.read(master, 65536)
                    except OSError: break
                    if not part: break
                    raw.extend(part)
        def wait(marker, since=0, timeout=150):
            until = time.monotonic() + timeout
            while marker not in screen()[since:]:
                if process.poll() is not None: raise RuntimeError('product exited ' + str(process.returncode))
                if time.monotonic() > until: raise TimeoutError(marker)
                pump()
        def send(keys, label): os.write(master, keys); pump(.35); actions.append({'label': label, 'keys': repr(keys)})
        try:
            wait('准备就绪'); pump(.7)
            startup = screen(); welcome = 'BioMind · /help 帮助 · /details 详情 · /exit 退出'
            assert (welcome not in startup) is trusted, {'trusted': trusted, 'welcome_visible': welcome in startup}
            checks.append({'id': 'S08-' + name, 'status': 'PASS', 'quietStartup_consumed': trusted, 'welcome_visible': welcome in startup})
            send(b'/status\r', 'passive product status'); wait('认证：原生 /login')
            assert not command_marker.exists(), 'passive status executed command credential'
            if args.command_credential:
                checks.append({'id': 'U06-passive-' + name, 'status': 'PASS', 'credential_command_executed': False})
            send(b'/biomind-memory status\r', 'actual domain memory scope'); wait('记忆根：' + str(b / 'memory-B'))
            send(b'Synthetic scope witness\r', 'local fixture model request'); wait(name + '_MODEL_OK')
            payload = trace.read_text()
            assert 'GLOBAL_PREF_B_SCOPE' in payload and 'PROJECT_B_ONLY_SCOPE' in payload
            assert 'GLOBAL_PREF_A_MUST_NOT_ENTER' not in payload and 'PROJECT_A_ONLY_MUST_NOT_ENTER' not in payload
            assert not command_marker.exists(), 'unselected command credential executed'
            send(b'/exit\r', 'normal exit'); process.wait(timeout=15); pump()
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait()
            os.close(master)
            (evidence / (name + '.raw')).write_bytes(raw); (evidence / (name + '.txt')).write_text(screen())
            (evidence / (name + '-keys.json')).write_text(json.dumps(actions, ensure_ascii=False, indent=2))
        launches.extend(json.loads(line) for line in launch_trace.read_text().splitlines())

    failure = None
    try:
        trust_values = [False, True] if args.trust == 'both' else [args.trust == 'trusted']
        for trusted in trust_values:
            (agent / 'trust.json').write_text(json.dumps({str(b): trusted}))
            trust_before = (agent / 'trust.json').read_bytes()
            label = 'trusted' if trusted else 'untrusted'
            json_run('json-' + label, trusted); tui_run('tui-' + label, trusted)
            assert (agent / 'trust.json').read_bytes() == trust_before, 'entry rewrote saved trust'
        for launch in launches:
            flags = launch['flags']
            assert launch['cwd'] == str(b) and flags['--cwd'] == str(b)
            assert flags['--data-root'] == str(b / 'domain-B') and flags['--memory-root'] == str(b / 'memory-B')
            assert '--wiki-root' not in flags, 'old A wiki env survived omitted B setting'
            assert flags['--preferences-path'] == str(root / 'pref-B.md') and flags['--archive-path'] == str(b / 'archive-B.sqlite3')
        assert len(launches) == 2 * len(trust_values), launches
        assert not (a / 'domain-old').exists() and not (a / 'memory-old').exists() and not (a / 'archive-old.sqlite3').exists()
        assert hashlib.sha256((b / '.pi/settings.json').read_bytes()).hexdigest() == settings_hash
        checks.append({'id': 'S09-final-project-domain-paths', 'status': 'PASS', 'actual_domain_launches': len(launches), 'old_A_paths_untouched': True})
    except Exception as error:
        failure = repr(error); checks.append({'id': 'witness-failure', 'status': 'FAIL', 'error': failure})
    report = {'checks': checks, 'failure': failure, 'commands': command_log, 'synthetic_root': str(root),
        'domain_launches': launches, 'settings_sha256': settings_hash,
        'host_sha256': hashlib.sha256((RUNTIME / 'dist/host.js').read_bytes()).hexdigest(),
        'extension_sha256': hashlib.sha256((RUNTIME / 'dist/extensions/biomind.js').read_bytes()).hexdigest(),
        'summary_fingerprint_timing': 'after run; per-command artifacts recorded at launch; JSON ready also contains actual extension_sha256',
        'command_credential_enabled': args.command_credential,
        'trust_cases': args.trust,
        'limits': 'Optional command credential provider is unselected; no claim of live credential retrieval or provider connectivity. JSON trust is its actual ready receipt; native settings consumption is visible quietStartup behavior.'}
    (evidence / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2)); return 0 if failure is None else 1


if __name__ == '__main__':
    raise SystemExit(main())

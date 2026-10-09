#!/usr/bin/env python3
"""Actual native product welcome witnesses using a reused VT cell emulator.

No model network, scientific job, real HOME, or product UI replacement. Snapshot
files are current viewport cells, not cumulative ANSI-stripped output.
"""
import argparse
import codecs
import fcntl
import hashlib
import importlib.util
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
import sys
import tempfile
import termios
import time
import traceback

HELPER = Path(__file__).with_name('welcome-screen.py')
spec = importlib.util.spec_from_file_location('welcome_screen', HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class Viewport(helper.Screen):
    """Thin observation adapter: OSC title isn't screen text; resize and cursor."""
    def __init__(self, cols, rows):
        super().__init__(cols, rows)
        self.transport_pending = ''
        self.cursor_visible = True
        self.ignored_metadata = []

    def feed(self, text):
        text = self.transport_pending + text
        self.transport_pending = ''
        out = ''
        while text:
            at = text.find('\x1b]')
            if at < 0:
                if text.endswith('\x1b'):
                    self.transport_pending = '\x1b'
                    text = text[:-1]
                out += text
                break
            out += text[:at]
            end = re.search(r'\x07|\x1b\\', text[at+2:])
            if end is None:
                self.transport_pending = text[at:]
                break
            stop = at + 2 + end.end()
            self.ignored_metadata.append(text[at:stop])
            text = text[stop:]
        for value in re.findall(r'\x1b\[\?25([hl])', out):
            self.cursor_visible = value == 'h'
        # ED3 clears scrollback only; the reused old emulator treats it as ED2.
        out = out.replace('\x1b[3J', '')
        return super().feed(out)

    def resize(self, cols, rows):
        blank = (' ', (39, False, False, 49, False, False))
        self.grid = [(row[:cols] + [blank] * max(0, cols-len(row))) for row in self.grid[:rows]]
        self.grid.extend([[blank] * cols for _ in range(rows-len(self.grid))])
        self.cols, self.rows = cols, rows
        self.x, self.y = min(cols-1, self.x), min(rows-1, self.y)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ScenarioComplete(Exception):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--baseline-only', action='store_true')
    parser.add_argument('--startup-only', action='store_true')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--unconfigured', action='store_true')
    parser.add_argument('--model-switch-only', action='store_true')
    parser.add_argument('--command-key-canary', action='store_true')
    parser.add_argument('--fail-status', action='store_true')
    parser.add_argument('--preferences-state', choices=['loaded', 'empty', 'missing'], default='loaded')
    parser.add_argument('--override', action='store_true')
    parser.add_argument('--cols', type=int, default=120)
    parser.add_argument('--rows', type=int, default=35)
    parser.add_argument('--fixture-root', type=Path)
    parser.add_argument('--resume-session', type=Path)
    parser.add_argument('--footer-session', action='store_true')
    parser.add_argument('--repository', type=Path, help='Explicit checkout to witness with isolated state')
    args = parser.parse_args()
    if args.command_key_canary:
        args.model_switch_only = True
    repository = args.repository.resolve() if args.repository else Path(__file__).resolve().parents[3]
    runtime = repository / 'integrations/pi_runtime'
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    assert not (evidence / 'result.json').exists(), 'Use a fresh evidence directory'
    isolated = args.fixture_root.resolve() if args.fixture_root else Path(tempfile.mkdtemp(prefix='biomind-welcome-'))
    isolated.mkdir(parents=True, exist_ok=True)
    project = isolated / '长中文项目🌿é-plant-multiomics'
    project.mkdir(exist_ok=True)
    agent, home = isolated / 'agent', isolated / 'home'
    agent.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    # Reuse installed executables without probing/downloading tools during the
    # synthetic startup. These links live only in this harness's agent directory.
    tool_bin = agent / 'bin'
    tool_bin.mkdir(exist_ok=True)
    for tool in ['fd', 'rg']:
        installed = runtime / '.biomind/pi-agent/bin' / tool
        assert installed.is_file(), 'Expected installed native tool: ' + str(installed)
        if not (tool_bin / tool).exists():
            (tool_bin / tool).symlink_to(installed)
    (agent / 'settings.json').write_text(json.dumps({'enableAnalytics': False,
        'enableInstallTelemetry': False, 'retry': {'enabled': False}, 'compaction': {'enabled': False},
        'cacheWarming': 'off', 'quietStartup': args.quiet}))
    key_canary = isolated / 'COMMAND_KEY_MUST_NOT_EXECUTE'
    if args.model_switch_only:
        (agent / 'models.json').write_text(json.dumps({'providers': {'welcome-display': {
            'baseUrl': 'http://127.0.0.1:1/v1', 'api': 'openai-completions',
            'apiKey': ('!touch ' + shlex.quote(str(key_canary))) if args.command_key_canary else 'synthetic-display-only',
            'models': [{'id': 'display-only', 'name': 'Welcome display-only synthetic model'}]}}}))
    prefs = isolated / 'preferences.md'
    prefs.write_text('WELCOME_GLOBAL_SYNTHETIC 简洁中文。')
    if args.preferences_state == 'empty':
        prefs.write_text('')
    if args.preferences_state == 'missing':
        prefs.unlink()
    (project / 'AGENTS.md').write_text('WELCOME_PROJECT_SYNTHETIC')
    if args.override:
        (project / 'AGENTS.override.md').write_text('WELCOME_PROJECT_OVERRIDE_SYNTHETIC')
    registry = isolated / 'registry.json'
    registry.write_text('{"environments":{},"tools":{}}')
    trace = evidence / 'provider.jsonl'
    resume_tag = 'WELCOME_TASK_' + evidence.name
    chunks = ['WELCOME_ACTIVE\n'] + ['本地假模型逐块显示，无科研作业。\n'] * 12 + ['WELCOME_DONE']
    cancel = ['WELCOME_CANCEL_ACTIVE\n'] + ['等待取消，无科研作业。\n'] * 60
    fixture = isolated / 'provider.json'
    fixture.write_text(json.dumps([
        {'content': [{'type': 'text', 'text': ''.join(chunks)}], 'chunks': chunks, 'delay_ms': 160, 'trace_path': str(trace),
            'expect_system_contains': 'WELCOME_PROJECT_UPDATED'},
        {'content': [{'type': 'text', 'text': ''.join(cancel)}], 'chunks': cancel, 'delay_ms': 200},
        {'error': 'WELCOME_SYNTHETIC_PROVIDER_ERROR'},
        {'content': [{'type': 'text', 'text': 'WELCOME_NEXT_DONE'}]},
    ], ensure_ascii=False))
    if args.footer_session:
        footer_chunks = ['FOOTER_ACTIVE\n'] + ['合成回复逐块显示，用于核对计时。\n'] * 10 + ['FOOTER_DONE：这是合成用量的本地回复。']
        fixture.write_text(json.dumps([{
            'content': [{'type': 'text', 'text': ''.join(footer_chunks)}],
            'chunks': footer_chunks, 'delay_ms': 450, 'trace_path': str(trace),
            'usage': {'input': 3200, 'output': 800, 'cacheRead': 1600, 'cacheWrite': 400, 'totalTokens': 6000,
                'cost': {'input': .004, 'output': .003, 'cacheRead': .001, 'cacheWrite': .001, 'total': .009}},
        }], ensure_ascii=False))
    config = isolated / 'runtime.json'
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(agent), 'dataRoot': str(isolated / 'domain'),
        'providerFixture': str(fixture), 'registryPath': str(registry), 'preferencesPath': str(prefs),
        'memoryRoot': str(isolated / 'memory'), 'wikiRoot': str(isolated / 'wiki'), 'historyEnabled': False,
        'sessionDir': str(isolated / 'sessions')}, ensure_ascii=False))
    if args.unconfigured:
        configuration = json.loads(config.read_text())
        configuration.pop('providerFixture')
        configuration['python'] = str(isolated / 'NO_DOMAIN_PYTHON')
        config.write_text(json.dumps(configuration, ensure_ascii=False))
    if args.resume_session:
        assert args.resume_session.resolve().is_relative_to(isolated), 'Only this harness synthetic session root is authorized'
        configuration = json.loads(config.read_text())
        configuration['session'] = str(args.resume_session.resolve())
        config.write_text(json.dumps(configuration, ensure_ascii=False))
    command = [sys.executable, str(repository / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project), '--pi-config', str(config)]
    if args.footer_session:
        command = [str(repository / 'bin/biomind'), '--engine', 'pi', '--project-dir', str(project), '--pi-config', str(config)]
    modules = [runtime / 'dist/product-ui.js', runtime / 'dist/extensions/biomind.js', runtime / 'dist/host.js',
        runtime / 'dist/biomind-extension.ts', runtime / 'dist/native-fixture.ts']
    if (runtime / 'dist/product-footer.js').exists():
        modules.append(runtime / 'dist/product-footer.js')
    if (runtime / 'dist/welcome-status.js').exists():
        modules.append(runtime / 'dist/welcome-status.js')
    module_observer = isolated / 'module-observer.mjs'
    module_observer.write_text("import {registerHooks,syncBuiltinESMExports} from 'node:module';\n"
        "import fs from 'node:fs'; import {createHash} from 'node:crypto';\n"
        "import path from 'node:path'; import {pathToFileURL,fileURLToPath} from 'node:url';\n"
        "const targets = new Set(" + json.dumps([p.as_uri() for p in modules]) + ");\n"
        "const record=(url,format,source,kind)=>fs.appendFileSync(" + json.dumps(str(evidence / 'loaded-modules.jsonl')) + ","
        "JSON.stringify({pid:process.pid,url,format,kind,source_sha256:source==null?null:"
        "createHash('sha256').update(source).digest('hex')})+'\\n');\n"
        # Jiti reads generated .ts entries through fs rather than Node's loader.
        "const readFileSync=fs.readFileSync.bind(fs); fs.readFileSync=(filename,...options)=>{"
        "const source=readFileSync(filename,...options); if(typeof filename==='string'||filename instanceof URL){"
        "const url=pathToFileURL(path.resolve(filename instanceof URL?fileURLToPath(filename):filename)).href;"
        "if(targets.has(url))record(url,'source',source,'read');}return source;}; syncBuiltinESMExports();\n"
        "registerHooks({load(url, context, nextLoad) { const result = nextLoad(url,context); "
        "if(targets.has(url))record(url,result.format,result.source,'load'); return result; }});\n")
    environment = {'HOME': str(home), 'PI_CODING_AGENT_DIR': str(agent),
        'PATH': str(runtime / '.runtime/node/bin') + ':' + str(tool_bin) + ':/usr/bin:/bin', 'TERM': 'xterm-256color',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '0',
        'PI_OFFLINE': '1', 'BIOMIND_PI_EXPECT_EMBEDDED_SDK': '1',
        'BIOMIND_PI_PYTHON': sys.executable,
        'BIOMIND_DISABLE_DOTENV': '1', 'NODE_OPTIONS': '--import=' + str(runtime / 'testing/offline-guard.mjs') + ' --import=' + str(module_observer),
        'BIOMIND_PI_TRACE_SYNTHETIC_READS': '1', 'BIOMIND_PI_TEST_ROOT': str(isolated),
        'BIOMIND_PI_GUARD_TRACE': str(evidence / 'guard.jsonl')}
    initial_hashes = {str(p): digest(p) for p in modules}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', args.rows, args.cols, 0, 0))
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=project, env=environment, stdin=slave, stdout=slave,
        stderr=slave, close_fds=True, start_new_session=True)
    os.close(slave)
    screen = Viewport(args.cols, args.rows)
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    raw, actions, snapshots, checks = bytearray(), [], [], {}
    status, failure = 'FAIL', None

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not select.select([master], [], [], min(.1, max(0, end-time.monotonic())))[0]:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            raw.extend(data)
            (evidence / 'terminal.raw').write_bytes(raw)
            for reply in screen.feed(decoder.decode(data)):
                os.write(master, reply.encode())
            if len(raw) > 8 * 1024 * 1024:
                raise RuntimeError('OUTPUT_LIMIT')

    def wait_for(marker, timeout=90):
        end = time.monotonic()+timeout
        while marker not in screen.text():
            if process.poll() is not None:
                raise RuntimeError('EXIT_BEFORE:' + marker + ':' + str(process.returncode))
            if time.monotonic() > end:
                raise RuntimeError('CURRENT_SCREEN_TIMEOUT:' + marker)
            pump(.1)

    def send(value, label):
        actions.append({'input': label, 'elapsed_s': time.monotonic()-started, 'raw_offset': len(raw)})
        os.write(master, value if isinstance(value, bytes) else value.encode())
        pump(.15)

    def snapshot(name):
        screen.snapshot(evidence, name)
        value = {'name': name, 'elapsed_s': time.monotonic()-started, 'raw_offset': len(raw),
            'cols': screen.cols, 'rows': screen.rows, 'cursor': {'x': min(screen.cols-1, screen.x), 'y': screen.y,
                'visible': screen.cursor_visible, 'wrap_pending': screen.x == screen.cols, 'emulator_next_x': screen.x},
            'product_modules': initial_hashes, 'unsupported_controls': list(dict.fromkeys(screen.unknown))}
        (evidence / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2))
        snapshots.append(value)
        return screen.text()

    def resize(cols, rows):
        actions.append({'resize': [cols, rows], 'elapsed_s': time.monotonic()-started, 'raw_offset': len(raw)})
        screen.resize(cols, rows)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        os.killpg(process.pid, signal.SIGWINCH)
        pump(.7)

    def request_count():
        return len(trace.read_text().splitlines()) if trace.exists() else 0

    try:
        wait_for('BioMind', 120)
        pump(1.5)
        startup = snapshot('01-startup')
        (evidence / 'startup-passive-observations.json').write_text(json.dumps({
            'provider_calls': request_count(), 'domain_data_exists': (isolated / 'domain').exists(),
            'memory_root_exists': (isolated / 'memory').exists(), 'wiki_root_exists': (isolated / 'wiki').exists(),
            'guard_events_at_snapshot': len((evidence / 'guard.jsonl').read_text().splitlines()),
            'snapshot_raw_offset': snapshots[-1]['raw_offset']}, indent=2))
        checks['no_startup_provider_call'] = request_count() == 0
        assert checks['no_startup_provider_call']
        if args.footer_session:
            def activity(view, phase):
                match = re.search(r'本轮' + phase + r' ((?:\d+:)?\d{2}:\d{2})', view)
                assert match, 'Current screen lacks activity clock: ' + phase
                parts = list(map(int, match.group(1).split(':')))
                return sum(value * 60 ** power for power, value in enumerate(reversed(parts)))

            def identity(view):
                row = next(line for line in view.splitlines() if line.startswith('会话'))
                match = re.search(r' · ([a-f0-9]{8})\s*$', row)
                assert match, 'Current screen must retain the session short ID: ' + row
                return row, match.group(1)

            row, original_id = identity(startup)
            assert '新对话' in row and '/32k' in startup
            assert ('常用操作' not in startup) if args.quiet else ('BioMind · 科研分析助手' in startup and '常用操作' in startup)
            assert 0 <= screen.y < screen.rows
            checks['native_footer_empty_session_no_request'] = True
            # A real native rename must update the label without adding a model request.
            send('/name 空会话命名\r', 'native session rename, no model call')
            wait_for('空会话命名')
            pump(.5)
            named = snapshot('footer-named-empty')
            assert '空会话命名' in identity(named)[0] and identity(named)[1] == original_id
            assert request_count() == 0
            checks['native_name_same_session_passive'] = True
            # A fresh session has a new identity and no inherited name/usage.
            send('/new\r', 'native new before first user task')
            pump(1)
            empty = snapshot('footer-new-empty')
            row, task_id = identity(empty)
            assert task_id != original_id and '新对话' in row
            if args.quiet:
                assert '常用操作' not in empty
            question = '整理科研结果与统计图'
            send(question + '\r', 'first accepted question supplies display-only preview')
            wait_for('FOOTER_ACTIVE')
            wait_for('本轮运行')
            running_start = activity(snapshot('footer-running-start'), '运行')
            pump(2.2)
            running_later = activity(snapshot('footer-running-later'), '运行')
            assert running_later >= running_start + 2, 'Running task clock must continue advancing'
            checks['activity_clock_advances_while_streaming'] = {'first_s': running_start, 'later_s': running_later}
            wait_for('FOOTER_DONE')
            wait_for('本次请求已结束')
            wait_for('本轮用时')
            pump(.5)
            done = snapshot('footer-first-question-compact')
            first_duration = activity(done, '用时')
            assert first_duration >= 4, 'Synthetic first stream must last at least four seconds'
            pump(2.2)
            frozen = snapshot('footer-first-question-frozen')
            assert activity(frozen, '用时') == first_duration and '本轮运行' not in frozen
            checks['activity_clock_freezes_after_settlement'] = {'duration_s': first_duration, 'idle_observation_s': 2.2}
            assert question in identity(done)[0] and identity(done)[1] == task_id
            for marker in ['↑3.2k', '↓800', 'R1.6k', 'W400', 'CH30.8%', '$0.009', '上下文 6k/32k']:
                assert marker in done, 'Native usage missing from current viewport: ' + marker
            assert '18.8%/32k' not in done
            assert '常用操作' not in done and request_count() == 1
            checks['native_usage_and_context_from_reported_fixture'] = True
            calls = request_count()
            send('/name 科研结果整理\r', 'native rename accepted conversation')
            pump(.6)
            assert '科研结果整理' in identity(snapshot('footer-named-compact'))[0]
            for cols, rows in [(80, 24), (42, 20), (120, 35)]:
                resize(cols, rows)
                view = snapshot('footer-working-%sx%s' % (cols, rows))
                assert identity(view)[1] == task_id and '科研结果' in identity(view)[0]
                assert '↑3.2k' in view and '↓800' in view
                assert 0 <= screen.x < cols and 0 <= screen.y < rows
            checks['current_session_and_native_footer_across_resize'] = True
            draft = '未提交草稿_KEEP'
            send(draft, 'native draft must not rename session')
            before = snapshot('footer-draft-before-model'); cursor = (screen.x, screen.y)
            send(b'\x0c', 'native model picker with draft')
            pump(.4); send(b'\x1b', 'cancel picker retaining draft'); pump(.4)
            after = snapshot('footer-draft-after-model')
            assert draft in before and draft in after and cursor == (screen.x, screen.y)
            assert identity(after)[1] == task_id and '科研结果整理' in identity(after)[0]
            send(b'\x03', 'clear unsubmitted draft')
            send('/reload\r', 'native reload keeps name, session and usage')
            pump(1.5)
            reloaded = snapshot('footer-reload')
            assert identity(reloaded)[1] == task_id and '↑3.2k' in reloaded
            assert '常用操作' not in reloaded and request_count() == calls
            send('/new\r', 'native new clears name and restores welcome policy')
            pump(1)
            new = snapshot('footer-final-new')
            assert identity(new)[1] != task_id and '新对话' in identity(new)[0]
            assert '↑3.2k' not in new
            assert ('常用操作' not in new) if args.quiet else ('常用操作' in new)
            for cols, rows in [(80, 24), (42, 20), (120, 35)]:
                resize(cols, rows); view = snapshot('footer-welcome-%sx%s' % (cols, rows))
                assert '新对话' in identity(view)[0] and '/32k' in view
                assert ('常用操作' not in view) if args.quiet else ('科研分析助手' in view)
            send('/resume\r', 'native resume selects named prior conversation')
            wait_for('Resume Session (Current Folder)')
            send('科研结果整理', 'filter named native sessions'); pump(.5)
            snapshot('footer-resume-picker')
            send('\r', 'native resume selection'); wait_for('Resumed session'); pump(.7)
            resumed = snapshot('footer-resumed')
            assert identity(resumed)[1] == task_id and '科研结果整理' in identity(resumed)[0]
            assert '↑3.2k' in resumed and request_count() == calls
            assert '本轮运行' not in resumed and '本轮用时' not in resumed
            assert not screen.unknown, 'Unsupported VT controls: ' + repr(screen.unknown)
            checks['native_draft_reload_new_resume_identity_and_usage'] = True
            # Reload/resume reconstructs the synthetic provider. Its first local
            # stream is deliberately long enough to witness native cancellation.
            send('验证恢复后新一轮计时与中断\r', 'new local task starts a fresh activity clock')
            wait_for('本轮运行')
            restarted = activity(snapshot('footer-restarted-running'), '运行')
            assert restarted < first_duration, 'A new task must not inherit the previous duration'
            pump(1.3)
            assert activity(snapshot('footer-before-cancel'), '运行') > restarted
            send(b'\x1b', 'cancel local synthetic stream using native Escape')
            wait_for('本轮用时')
            cancelled = activity(snapshot('footer-cancelled'), '用时')
            pump(1.3)
            assert activity(snapshot('footer-cancelled-frozen'), '用时') == cancelled
            assert request_count() == calls + 1
            checks['activity_clock_resets_and_freezes_on_native_cancel'] = {'restart_s': restarted, 'cancelled_s': cancelled}
            status = 'PASS'
        elif args.baseline_only:
            status = 'PASS'
        elif args.resume_session:
            assert '常用操作' not in startup
            before_messages = [json.loads(line) for line in args.resume_session.read_text().splitlines()]
            draft = '恢复会话_中文草稿_KEEP'
            send(draft, 'unsubmitted native editor draft on resumed conversation')
            bottom = snapshot('resume-draft-bottom')
            cursor = (screen.x, screen.y)
            assert draft in bottom
            send(b'\x1b[5~', 'native fullscreen PageUp transcript scroll')
            scrolled = snapshot('resume-draft-page-up')
            assert scrolled != bottom, 'Transcript did not visibly scroll'
            send(b'\x1b[6~', 'native fullscreen PageDown transcript scroll')
            send(b'\x1b[F', 'native fullscreen End returns to transcript bottom')
            restored = snapshot('resume-draft-restored-bottom')
            assert draft in restored and cursor == (screen.x, screen.y)
            assert '常用操作' not in restored and request_count() == 0
            after_messages = [json.loads(line) for line in args.resume_session.read_text().splitlines()]
            users = lambda items: [item for item in items if item.get('type') == 'message' and item.get('message', {}).get('role') == 'user']
            assert users(before_messages) == users(after_messages)
            checks['V05_resumed_native_scroll_draft_cursor_no_new_request'] = True
            status = 'PASS'
        else:
            assert not screen.unknown, 'Unsupported VT controls require review: ' + repr(screen.unknown)
            if args.quiet:
                assert '常用操作' not in startup and '科研分析助手' not in startup
                checks['quiet_hides_full_welcome'] = True
                send('/help\r', 'native help remains available in quiet mode')
                pump(.7)
                snapshot('02-quiet-help')
                send('/status\r', 'read effective quietStartup source through registered status')
                wait_for('quietStartup=true')
                snapshot('03-quiet-effective-source')
                checks['quiet_source_is_native_user_setting'] = 'Pi 用户设置' in screen.text()
                assert checks['quiet_source_is_native_user_setting']
                status = 'PASS'
            else:
                required = ['BioMind · 科研分析助手', '项目', '目录', '模型', '认证', '说明', '记忆', '常用操作',
                    '/login', '/model', '/project', '/instructions', '/biomind-memory status', '/new', '/resume',
                    '输入 / 搜索全部命令', '直接输入问题即可开始']
                assert all(item in startup for item in required), 'Welcome missing: ' + repr([v for v in required if v not in startup])
                checks['V01_current_viewport_full_welcome'] = True
                if args.model_switch_only:
                    for model_filter, expected in [('display-only', 'welcome-display/display-only'), ('scripted', 'biomind-fixture/scripted')]:
                        send('/model\r', 'open native model selector for actual model switch')
                        wait_for('Enter to select')
                        send(model_filter, 'filter native model list: ' + model_filter)
                        pump(.5)
                        snapshot('model-picker-' + model_filter)
                        send('\r', 'select actual model: ' + model_filter)
                        wait_for(expected)
                        selected = snapshot('model-selected-' + model_filter)
                        assert expected in next(line for line in selected.splitlines() if line.startswith('模型'))
                        assert '常用操作' in selected and request_count() == 0
                    checks['V09_actual_native_model_switch_and_back_no_requests'] = True
                    if args.command_key_canary:
                        assert not key_canary.exists(), 'Native passive auth/model inspection executed command key'
                        checks['V12_command_key_marker_absent_after_native_selection'] = True
                    status = 'PASS'
                    raise ScenarioComplete()
                if args.startup_only:
                    if args.unconfigured:
                        assert '尚未选择' in startup
                        send('/login\r', 'unconfigured welcome still opens native login')
                        wait_for('Select authentication method:')
                        snapshot('unconfigured-native-login')
                        send(b'\x1b', 'cancel native login without credentials')
                        pump(.5)
                        send('/model\r', 'unconfigured welcome still opens native model selector')
                        pump(.8)
                        snapshot('unconfigured-native-model-picker')
                        send(b'\x1b', 'cancel native model selector without request')
                        pump(.5)
                        send('未配置也能输入中文草稿', 'native editor remains usable without model')
                        pump(.2)
                        snapshot('unconfigured-native-draft')
                        assert request_count() == 0
                        if args.fail_status:
                            send(b'\x03', 'clear unsubmitted draft before explicit failed status read')
                            send('/biomind-memory status\r', 'explicit status against nonexistent synthetic domain Python')
                            wait_for('项目按需检索未知')
                            failed_view = snapshot('unconfigured-status-read-failed')
                            assert '状态读取失败' in failed_view and '常用操作' in failed_view
                            assert not (isolated / 'domain').exists()
                            checks['V08_actual_status_failure_unknown_does_not_block_input'] = True
                    if args.preferences_state == 'empty':
                        assert '空' in startup
                    if args.preferences_state == 'missing':
                        assert '缺失' in startup
                    if args.override:
                        assert 'override' in startup
                    checks['V08_startup_variant_truthful'] = True
                    status = 'PASS'
                    raise ScenarioComplete()
                for cols, rows in [(100, 30), (80, 24), (42, 20), (120, 35)]:
                    resize(cols, rows)
                    view = snapshot('resize-%sx%s' % (cols, rows))
                    assert '项目' in view and '模型' in view and '输入 / 搜索全部命令' in view
                    assert 0 <= screen.x < cols and 0 <= screen.y < rows
                checks['V07_resize_current_viewports'] = True
                draft = '第一行_中文未提交草稿\n第二行_路径🌿é_TRAIL'
                send('\x1b[200~' + draft + '\x1b[201~', 'paste native multiline Chinese draft, no submit')
                send(b'\x1b[D' * 6, 'place native cursor before final draft suffix')
                draft_before = snapshot('draft-before-model-menu')
                cursor_before = (screen.x, screen.y)
                assert all(line in draft_before for line in draft.split('\n'))
                send(b'\x0c', 'native Ctrl+L opens model selector while draft exists')
                pump(.5)
                snapshot('draft-model-menu')
                send(b'\x1b', 'cancel native model selector without submitting draft')
                pump(.5)
                restored = snapshot('draft-after-model-cancel')
                assert all(line in restored for line in draft.split('\n'))
                assert cursor_before == (screen.x, screen.y)
                assert '常用操作' in restored and request_count() == 0
                send('_CURSOR_', 'insert at retained native cursor to prove position')
                snapshot('draft-insert-at-retained-cursor')
                send(b'\x03', 'clear unsubmitted multiline draft through native editor')
                checks['V02_multiline_draft_native_menu_cancel_cursor_preserved'] = True
                send('/model\r', 'open native model picker from empty session')
                pump(.5)
                snapshot('menu-model')
                send(b'\x1b', 'cancel native model picker')
                pump(.5)
                assert '常用操作' in screen.text()
                send('/pro', 'native project command completion prefix')
                send(b'\t', 'native Tab completion')
                snapshot('completion-project')
                send(b'\x1b', 'native Esc completion dismissal')
                send(b'\x03', 'clear command without model request')
                send('/instructions global\r', 'open native instructions editor from empty session')
                wait_for('编辑说明')
                snapshot('menu-instructions')
                send(b'\x1b', 'cancel native instructions menu')
                pump(.4)
                assert '常用操作' in screen.text()
                send('/project\r', 'open registered native project menu from empty session')
                wait_for('项目操作（取消保留当前会话）')
                snapshot('menu-project')
                send(b'\x1b', 'cancel project menu retaining same empty session')
                pump(.5)
                assert '常用操作' in screen.text() and request_count() == 0
                checks['V03_native_menus_completions'] = True
                send('/biomind-memory history on\r', 'existing runtime memory switch while welcome remains empty')
                wait_for('项目按需检索开启')
                memory_view = snapshot('memory-runtime-on-welcome')
                assert '常用操作' in memory_view and request_count() == 0
                checks['V09_memory_runtime_updates_welcome'] = True
                send('/biomind-memory history off\r', 'restore retrieval off before unrelated lifecycle tasks')
                wait_for('项目按需检索关闭')
                send('/instructions project\r', 'edit actual loaded project instructions before first task')
                wait_for('编辑说明')
                send(b'\x01\x0b', 'native editor replace current one-line synthetic instruction')
                send('WELCOME_PROJECT_UPDATED\r', 'complete native instruction editor with new content')
                wait_for('确认保存说明')
                send('\r', 'confirm synthetic project instruction save')
                wait_for('现在刷新说明')
                send(b'\x1b', 'defer reload to witness pending welcome state')
                pump(.5)
                pending = snapshot('instructions-saved-pending-welcome')
                assert '待刷新' in pending and '常用操作' in pending
                assert (project / 'AGENTS.md').read_text() == 'WELCOME_PROJECT_UPDATED'
                send('/reload\r', 'native reload refreshes saved instructions while empty')
                wait_for('常用操作', 90)
                pump(.5)
                refreshed = snapshot('instructions-reloaded-full-welcome')
                assert '常用操作' in refreshed and '待刷新' not in next(line for line in refreshed.splitlines() if line.startswith('说明'))
                checks['V09_instruction_save_pending_reload_loaded'] = True
                send(resume_tag + ' 仅用本地假模型测试欢迎收缩，不运行科研。\r', 'first ordinary user task')
                wait_for('WELCOME_ACTIVE')
                active = snapshot('02-first-task-compact-active')
                assert '常用操作' not in active
                wait_for('WELCOME_DONE')
                wait_for('本次请求已结束')
                pump(.2)
                done = snapshot('03-first-task-compact-done')
                assert '常用操作' not in done
                checks['V04_first_task_and_completion_compact'] = True
                send('测试本地流取消。\r', 'start cancellable local fake stream')
                wait_for('WELCOME_CANCEL_ACTIVE')
                send(b'\x1b', 'cancel active request using native Escape')
                pump(.7)
                assert '常用操作' not in snapshot('04-cancelled-compact')
                send('测试本地假provider错误。\r', 'start local fake error request')
                wait_for('WELCOME_SYNTHETIC_PROVIDER_ERROR')
                assert '常用操作' not in snapshot('05-error-compact')
                checks['V04_cancel_error_remain_compact'] = True
                send('/reload\r', 'native reload of nonempty session')
                wait_for('BioMind · 长中文项目', 90)
                pump(.5)
                assert '常用操作' not in snapshot('06-reload-old-compact')
                send('/new\r', 'native new empty session')
                wait_for('常用操作')
                snapshot('07-new-full-welcome')
                checks['V05_reload_compact_new_full'] = True
                calls_before_resume = request_count()
                send('/resume\r', 'native resume current-project prior conversation')
                wait_for('Resume Session (Current Folder)')
                send(resume_tag, 'filter native resume picker for this exact fixture conversation')
                pump(.7)
                snapshot('08-resume-picker')
                send('\r', 'select original same-project conversation')
                wait_for('Resumed session')
                pump(.5)
                resumed = snapshot('09-resumed-old-compact')
                assert '常用操作' not in resumed and request_count() == calls_before_resume
                checks['V05_native_resume_old_compact_no_model_call'] = True
                status = 'PASS'
    except ScenarioComplete:
        pass
    except BaseException as error:
        failure = traceback.format_exc()
        snapshot('failure-current-screen')
    finally:
        send(b'\x03', 'exit first interrupt')
        pump(.2)
        send(b'\x03', 'exit second interrupt')
        end = time.monotonic()+8
        while process.poll() is None and time.monotonic()<end:
            pump(.1)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        pump(.1)
        os.close(master)
        (evidence / 'terminal.raw').write_bytes(raw)
        final_hashes = {str(p): digest(p) for p in modules}
        guards = [json.loads(v) for v in (evidence / 'guard.jsonl').read_text().splitlines()]
        denied = [v for v in guards if v.get('allowed') is False]
        if args.footer_session and status == 'PASS':
            observed = [json.loads(v) for v in (evidence / 'loaded-modules.jsonl').read_text().splitlines()]
            loaded_bundle_urls = {item['url'] for item in observed if item.get('kind') == 'read'}
            required_bundles = {str((runtime / 'dist' / name).as_uri()) for name in ['biomind-extension.ts', 'native-fixture.ts']}
            embedded_guard = any(v.get('kind') == 'native_embedded_sdk_guard' for v in guards)
            checks['native_bundles_observed_and_embedded_sdk_guard_active'] = required_bundles <= loaded_bundle_urls and embedded_guard
            if not checks['native_bundles_observed_and_embedded_sdk_guard_active']:
                status, failure = 'FAIL', 'Missing actual native bundle observation or embedded SDK guard'
        if denied or final_hashes != initial_hashes:
            status, failure = 'FAIL', 'Guard denial or product bytes changed during witness'
        result = {'status': status, 'failure': failure, 'exit_code': process.returncode,
            'command': command, 'cwd': str(project), 'synthetic_root': str(isolated), 'quietStartup': args.quiet,
            'elapsed_s': time.monotonic() - started,
            'quietStartup_source': 'synthetic agent/settings.json', 'checks': checks, 'actions': actions,
            'snapshots': snapshots, 'provider_calls': request_count(), 'guard_denials': denied,
            'command_key_canary': {'configured': args.command_key_canary, 'path': str(key_canary), 'exists': key_canary.exists()},
            'product_modules_before': initial_hashes, 'product_modules_after': final_hashes,
            'screen_helper': {'path': str(HELPER), 'sha256': digest(HELPER), 'type': 'existing VT cell emulator; current viewport'},
            'unsupported_controls': list(dict.fromkeys(screen.unknown))}
        (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({'status': status, 'failure': failure, 'exit_code': process.returncode,
            'evidence': str(evidence), 'checks': checks}, ensure_ascii=False))
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

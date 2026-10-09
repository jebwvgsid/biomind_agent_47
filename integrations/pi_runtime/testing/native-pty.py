#!/usr/bin/env python3
"""Real native Pi PTY acceptance probe; synthetic provider, fresh isolated data.

No legacy Agent, existing session, credential file or real package installation
is loaded. This is a terminal-byte probe, not a claim about a desktop IME.
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
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid


ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--positive-install', action='store_true')
    parser.add_argument('--negative-inputs', action='store_true')
    parser.add_argument('--draft-overlays', action='store_true')
    parser.add_argument('--session-switch', action='store_true')
    parser.add_argument('--instructions-ui', action='store_true')
    parser.add_argument('--unconfigured-ui', action='store_true')
    parser.add_argument('--plain-unconfigured', action='store_true', help='With --unconfigured-ui, omit even --pi-config')
    parser.add_argument('--project-draft-ui', action='store_true', help='Use native kill/yank to retain an unsubmitted draft across /project')
    parser.add_argument('--protection-ui', action='store_true', help='Approve one new synthetic hardlink-blocked command and inspect actual collapsed diagnostics')
    args = parser.parse_args()
    if sum((args.positive_install, args.negative_inputs, args.draft_overlays, args.session_switch, args.instructions_ui, args.unconfigured_ui, args.project_draft_ui, args.protection_ui)) > 1:
        parser.error('Choose only one dedicated UI scenario')
    repository = Path(__file__).resolve().parents[3]
    runtime = repository / 'integrations' / 'pi_runtime'
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    isolated = Path(tempfile.mkdtemp(prefix='biomind-native-pty-'))
    project = isolated / '合成 项目'
    project.mkdir()
    trace = evidence / 'native-provider-requests.jsonl'
    if trace.exists():
        raise RuntimeError('Use a fresh evidence directory; never append to prior session evidence')
    registry = isolated / 'registry.json'
    registry.write_text('{"environments":{},"tools":{}}')
    preferences = isolated / 'preferences.md'
    preferences.write_text('合成测试偏好：中文简洁。')
    agent = isolated / 'pi-agent'
    agent.mkdir()
    # Explicit new settings; no ambient provider/session lookup is needed.
    (agent / 'settings.json').write_text(json.dumps({'enableAnalytics': False, 'enableInstallTelemetry': False,
        'retry': {'enabled': False}, 'compaction': {'enabled': False}, 'cacheWarming': 'off'}))

    def text(value, **extra):
        return {'content': [{'type': 'text', 'text': value}], **extra}

    def call(identity, name, arguments):
        return {'content': [{'type': 'toolCall', 'id': identity, 'name': name, 'arguments': arguments}]}

    chunks = ['GEN_STREAM_BEGIN\n'] + [f'长流第{i:02d}行：中文内容、滚动与草稿保持。\n' for i in range(45)] + ['GEN_STREAM_END']
    cancelled = ['ABORT_STREAM_BEGIN\n'] + ['等待用户中断，未启动科学作业。\n'] * 50 + ['ABORT_UNEXPECTED_FINISH']
    draft = '草稿完整保留_空格 路径/合成.tsv'
    cursor_marker = '_CURSOR_'
    edited_draft = draft[:-6] + cursor_marker + draft[-6:]
    steps = [
        text('NATIVE_CONSULT_DONE 中文咨询直接结束。', trace_path=str(trace)),
        call('native-question', 'ask_user', {'question': 'NATIVE_QUESTION 请填写合成清单路径？'}),
        text('NATIVE_QUESTION_DONE 已收到真实终端回答。'),
        call('native-handoff', 'handoff_generate', {'goal': '仅进行合成界面与拒绝安装测试'}),
        call('native-install-deny', 'install_tool', {'cmd': 'printf synthetic > SHOULD_NOT_EXIST', 'env_name': 'synthetic',
            'description': 'NATIVE_INSTALL_DENIAL 合成安装提案，按默认拒绝', 'prefix': str(isolated / 'synthetic-env')}),
        text(''.join(chunks), chunks=chunks, delay_ms=150, expect_system_contains='Generate'),
        text('NATIVE_DRAFT_ACCEPTED 草稿在长流后完整提交。', expect_user_contains=edited_draft),
        text(''.join(cancelled), chunks=cancelled, delay_ms=200),
        text('NATIVE_RECOVERY_DONE 中断后新轮可用。', expect_user_contains='中断后恢复'),
        call('native-cancel-handoff', 'handoff_generate', {'goal': '只测试取消安装提案'}),
        call('native-install-cancel', 'install_tool', {'cmd': 'printf synthetic > CANCEL_SHOULD_NOT_EXIST', 'env_name': 'synthetic',
            'description': 'NATIVE_INSTALL_CANCEL 合成提案，按 Escape 取消', 'prefix': str(isolated / 'synthetic-env')}),
        text('NATIVE_APPROVAL_CANCEL_DONE 未执行安装。'),
        call('native-edit-handoff', 'handoff_generate', {'goal': '只测试编辑提案不自动批准'}),
        call('native-install-edit', 'install_tool', {'cmd': 'printf old > EDIT_OLD_SHOULD_NOT_EXIST', 'env_name': 'synthetic',
            'description': 'NATIVE_INSTALL_EDIT 编辑后仍须重新明确接受', 'prefix': str(isolated / 'synthetic-env')}),
        text('NATIVE_APPROVAL_EDIT_DONE 编辑提案已取消，无执行。'),
    ]
    accepted_marker = project / 'INSTALL_ACCEPTED_ONCE'
    accepted_prefix = project / '.synthetic-env'
    if args.protection_ui:
        source = project / 'protected-fixture'; source.write_text('synthetic-original')
        os.link(source, project / 'hardlink-fixture')
        steps = [steps[0],
            call('protection-handoff', 'handoff_generate', {'goal': '只验证合成硬链接保护诊断，不做科学分析'}),
            call('protection-command', 'run_command', {'cmd': 'touch PROTECTION_MUST_NOT_EXIST', 'workdir': str(project),
                'description': 'NATIVE_PROTECTION_APPROVAL 只测试保护拒绝'}),
            text('NATIVE_PROTECTION_DONE 这是合成回执观察，不表示科学成功。')]
    if args.positive_install:
        accepted_prefix.mkdir()
        steps = [steps[0],
            call('native-positive-handoff', 'handoff_generate', {'goal': '只运行已明确批准的标准库合成安装标记与验证'}),
            call('native-install-accept', 'install_tool', {
                'cmd': 'printf synthetic >> ' + shlex.quote(str(accepted_marker)),
                'env_name': 'synthetic-positive', 'prefix': str(accepted_prefix), 'packages': ['synthetic-only-no-package-manager'],
                'verification_commands': ['test -f ' + shlex.quote(str(accepted_marker)), 'test -d ' + shlex.quote(str(accepted_prefix))],
                'description': 'NATIVE_INSTALL_ACCEPT 仅写临时marker并验证临时prefix，不安装真实软件'}),
            text('NATIVE_APPROVAL_ACCEPT_DONE 合成执行与验证注册已结束。'),
        ]
    if args.negative_inputs:
        steps = [steps[0]]
        for label in ['WHITESPACE', 'N', 'PASTE']:
            steps.extend([
                call('negative-handoff-' + label, 'handoff_generate', {'goal': '只测试非授权输入 ' + label}),
                call('negative-install-' + label, 'install_tool', {
                    'cmd': 'printf NEVER > NEGATIVE_' + label + '_SHOULD_NOT_EXIST', 'env_name': 'synthetic',
                    'description': 'NATIVE_NEGATIVE_' + label + ' 合成负向授权输入', 'prefix': str(project / '.negative-env')}),
                text('NATIVE_NEGATIVE_DONE_' + label + ' 没有批准。'),
            ])
    overlay_drafts = {}
    if args.draft_overlays:
        def streamed_call(label, identity, name, arguments):
            prelude = [label + '_PRELUDE_BEGIN\n'] + ['先保留普通草稿及光标，再显示独立交互层。\n'] * 20
            return {'content': [{'type': 'text', 'text': ''.join(prelude)},
                {'type': 'toolCall', 'id': identity, 'name': name, 'arguments': arguments}],
                'chunks': prelude, 'delay_ms': 150}
        for label in ['QUESTION', 'APPROVAL']:
            value = label + '_普通草稿 空格/中文合成.tsv'
            overlay_drafts[label] = {'draft': value, 'expected': value[:-6] + '_KEPT_' + value[-6:]}
        steps = [steps[0],
            streamed_call('QUESTION', 'overlay-question', 'ask_user', {'question': 'OVERLAY_QUESTION 请输入独立回答，不覆盖普通草稿'}),
            text('OVERLAY_QUESTION_DONE 已收到独立回答。'),
            text('OVERLAY_QUESTION_DRAFT_ACCEPTED', expect_user_contains=overlay_drafts['QUESTION']['expected']),
            call('overlay-handoff', 'handoff_generate', {'goal': '只测试普通草稿在安装审批取消前后的保留'}),
            streamed_call('APPROVAL', 'overlay-install', 'install_tool', {
                'cmd': 'printf NEVER > OVERLAY_SHOULD_NOT_EXIST', 'env_name': 'synthetic',
                'description': 'OVERLAY_INSTALL_CANCEL 只显示合成安装提案并取消', 'prefix': str(project / '.overlay-env')}),
            text('OVERLAY_APPROVAL_DONE 安装已经取消。'),
            text('OVERLAY_APPROVAL_DRAFT_ACCEPTED', expect_user_contains=overlay_drafts['APPROVAL']['expected']),
        ]
    if args.session_switch:
        steps = [steps[0],
            call('idle-old-handoff', 'handoff_generate', {'goal': 'OLD_SESSION_CONTEXT_ONLY 仅切换角色，不执行作业'}),
            text('OLD_SESSION_GENERATE_DONE', expect_system_contains='Generate'),
        ]
    if args.instructions_ui:
        preferences.write_text('GLOBAL_PREF_A')
        (project / 'AGENTS.md').write_text('PROJECT_BASE_A')
        (project / 'AGENTS.override.md').write_text('PROJECT_OVERRIDE_A')
        steps = [text('NATIVE_CONSULT_DONE INSTRUCTIONS_REQUEST_OK', trace_path=str(trace))]
    if args.project_draft_ui:
        steps = [text('NATIVE_CONSULT_DONE PROJECT_DRAFT_READY', trace_path=str(trace))]
    (evidence / 'registry-before.json').write_text(registry.read_text())
    fixture = isolated / 'provider-fixture.json' 
    fixture.write_text(json.dumps(steps, ensure_ascii=False))
    config = isolated / 'runtime.json'
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(agent), 'dataRoot': str(isolated / 'domain'),
        'providerFixture': str(fixture), 'registryPath': str(registry), 'preferencesPath': str(preferences),
        'memoryRoot': str(isolated / 'memory'), 'wikiRoot': str(isolated / 'wiki'), 'historyEnabled': False}, ensure_ascii=False))
    session_directory = isolated / '自定义 会话'
    if args.instructions_ui:
        configuration = json.loads(config.read_text())
        configuration['sessionDir'] = str(session_directory)
        config.write_text(json.dumps(configuration, ensure_ascii=False))
    if args.unconfigured_ui:
        config.write_text(json.dumps({'python': str(isolated / 'NO_DOMAIN_PYTHON'), 'agentDir': str(agent)}))
        settings = json.loads((agent / 'settings.json').read_text())
        settings['quietStartup'] = True
        (agent / 'settings.json').write_text(json.dumps(settings))
    command = [sys.executable, str(repository / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project),
        '--pi-config', str(config), '--prompt', '仅做合成中文咨询，不执行分析。']
    if args.unconfigured_ui:
        command = command[:-2]
        if args.plain_unconfigured:
            command = command[:-2]
    master, slave = pty.openpty()
    extension_hash = hashlib.sha256((runtime / 'dist/extensions/biomind.js').read_bytes()).hexdigest()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
    synthetic_home = isolated / 'home'
    synthetic_home.mkdir()
    environment = {'HOME': str(synthetic_home), 'PI_CODING_AGENT_DIR': str(agent),
        'PATH': str(runtime / '.runtime' / 'node' / 'bin') + ':/usr/bin:/bin', 'TERM': 'xterm-256color',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '0',
        'NODE_OPTIONS': '--import=' + str(runtime / 'testing' / 'offline-guard.mjs'),
        'BIOMIND_PI_TRACE_SYNTHETIC_READS': '1' if args.instructions_ui else '0',
        'BIOMIND_PI_TEST_ROOT': str(isolated), 'BIOMIND_PI_GUARD_TRACE': str(evidence / 'native-guard.jsonl')}
    if args.plain_unconfigured:
        environment.update(BIOMIND_DISABLE_DOTENV='1', BIOMIND_PI_PYTHON=str(isolated / 'NO_DOMAIN_PYTHON'),
            BIOMIND_PI_PREFERENCES_PATH=str(preferences))
    process = subprocess.Popen(command, cwd=project, env=environment, stdin=slave, stdout=slave,
        stderr=slave, close_fds=True, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    decoded = ''
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    actions = []
    checks = {}
    status = 'FAIL'
    failure = None

    def pump(seconds):
        nonlocal decoded
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], max(0, min(.1, end - time.monotonic())))
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            raw.extend(chunk)
            decoded += decoder.decode(chunk)
            if len(raw) > 8 * 1024 * 1024:
                raise RuntimeError('PTY_OUTPUT_LIMIT')

    def screen():
        return ANSI.sub('', decoded)

    def wait_for(marker, timeout=30, since=0):
        end = time.monotonic() + timeout
        while marker not in ANSI.sub('', decoded[since:]):
            if process.poll() is not None:
                raise RuntimeError(f'NATIVE_EXIT_BEFORE:{marker}:code={process.returncode}')
            if time.monotonic() > end:
                raise RuntimeError('NATIVE_MARKER_TIMEOUT:' + marker)
            pump(.1)
        actions.append({'observed': marker, 'raw_offset': len(raw)})

    def send(value, label):
        actions.append({'input': label, 'raw_offset': len(raw)})
        os.write(master, value if isinstance(value, bytes) else value.encode())

    def requests():
        return [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []

    def receipt(identity):
        for request in reversed(requests()):
            for message in request['context'].get('messages', []):
                if message.get('role') == 'toolResult' and message.get('toolCallId') == identity:
                    assert not message.get('isError'), 'native tool failed instead of taking the UI decision'
                    return json.loads(message['content'][0]['text'])
        raise AssertionError('missing actual Pi tool result: ' + identity)

    try:
        if args.unconfigured_ui:
            wait_for('尚未选择模型', timeout=90)
            assert 'BioMind · /help 帮助' not in screen(), 'quietStartup did not suppress product welcome'
            checks['unconfigured_ordinary_entry_quiet_startup_no_domain_python'] = True
            auth_file = agent / 'auth.json'
            auth_before = auth_file.read_bytes() if auth_file.exists() else None
            start = len(decoded)
            send('/login\r', 'invoke actual native login without provider argument')
            wait_for('Select authentication method:', since=start)
            send(b'\x1b[B\r', 'choose native API-key provider list without entering a key')
            wait_for('Select provider to configure:', since=start)
            send(b'\x1b', 'cancel native authentication provider picker')
            pump(.3)
            send(b'\x1b', 'cancel parent native authentication method picker')
            pump(.3)
            start = len(decoded)
            send('/model\r', 'invoke actual native model selector')
            wait_for('Use /login', since=start)
            send(b'\x1b', 'cancel model selector without request')
            pump(.3)
            start = len(decoded)
            send('/help\r', 'native configured product help after selector cancellation')
            wait_for('BioMind 帮助', since=start)
            assert not trace.exists() and not (isolated / 'domain').exists()
            assert (auth_file.read_bytes() if auth_file.exists() else None) == auth_before, 'cancel changed native credential store'
            assert auth_before is None or not json.loads(auth_before), 'synthetic startup unexpectedly has credentials'
            checks['native_login_model_cancellation_no_auth_write_model_call'] = True
        else:
            wait_for('NATIVE_CONSULT_DONE', timeout=150 if args.project_draft_ui else 90 if args.instructions_ui else 60 if args.draft_overlays or args.session_switch else 30)
            checks['native_cli_chinese_render'] = True
            assert 'DOMAIN_' not in screen(), 'domain initialization failed before the native Consult'
            assert 'biomind-current' in json.dumps(requests()[0]['context']), 'BioMind business context was not attached'
            checks['native_consult_with_domain_context'] = True
        if args.unconfigured_ui:
            pass
        elif args.protection_ui:
            pump(.3)
            send('请求执行一次合成保护诊断，并等待明确接受。\r', 'request synthetic protected command')
            wait_for('NATIVE_PROTECTION_APPROVAL')
            send(b'\x1b[B\r', 'fixture selects explicit accept in real native UI')
            wait_for('NATIVE_PROTECTION_DONE', timeout=45)
            observed = receipt('protection-command')
            assert observed['status'] == 'stage_blocked', observed
            assert observed['reason'] == 'hardlink_in_writable_scope'
            assert observed['execution_started'] is False and observed['exit_code'] is None
            assert observed['count'] == 2 and observed['phase'] == 'protected_spawn_preflight'
            assert observed['receipt_ref'] and not (project / 'PROTECTION_MUST_NOT_EXIST').exists()
            assert source.read_text() == 'synthetic-original'
            for marker in ('执行被阻塞', 'hardlink_in_writable_scope', '本次目标命令未启动', '原始回执', '2 个路径条目'):
                assert marker in screen(), marker
            assert len(requests()) == len(steps)
            (evidence / 'protection-observation.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2))
            checks['real_domain_bwrap_preflight_model_and_collapsed_ui'] = True
        elif args.project_draft_ui:
            original_draft = "UNSUBMITTED_DRAFT_科研 路径/'quoted'_KEPT"
            before_sessions = {str(p): p.read_bytes() for p in agent.rglob('*.jsonl')}
            start = len(decoded)
            send(original_draft, 'type actual ordinary unsubmitted draft')
            wait_for(original_draft, since=start)
            send(b'\x01', 'native Ctrl+A moves to draft start')
            pump(.1)
            send(b'\x0b', 'native Ctrl+K keeps draft in editor kill ring while clearing input for command')
            pump(.3)
            start = len(decoded)
            send('/project\r', 'invoke real slash command using the ordinary native input owner')
            wait_for('项目操作（取消保留当前会话）', since=start)
            assert len(requests()) == 1
            send(b'\x1b', 'cancel native project picker')
            pump(.3)
            start = len(decoded)
            send(b'\x19', 'native Ctrl+Y restores original unsubmitted draft without provider submission')
            wait_for(original_draft, since=start)
            assert len(requests()) == 1
            after_sessions = {str(p): p.read_bytes() for p in agent.rglob('*.jsonl')}
            assert after_sessions == before_sessions
            assert all(json.loads(value.splitlines()[0])['cwd'] == str(project) for value in after_sessions.values())
            checks['project_cancel_preserves_native_kill_ring_draft_and_session_no_provider_call'] = True
            (evidence / 'project-draft-preservation.json').write_text(json.dumps({
                'draft': original_draft, 'restored_via': 'native Ctrl+A/Ctrl+K then /project Enter Escape then Ctrl+Y',
                'provider_calls_before_after': 1, 'session_files_identical': True, 'cwd': str(project),
                'evidence_type': 'PRODUCT_TUI', 'boundary': 'Slash commands share the native editor. This proves explicit native kill/yank recovery, not automatic parallel preservation of an unsaved editor buffer.'},
                ensure_ascii=False, indent=2) + '\n')
        elif args.instructions_ui:
            def issue_command(value, marker):
                start = len(decoded)
                started = time.monotonic()
                send(value + '\r', value)
                wait_for(marker, since=start)
                actions.append({'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                    'operation': value, 'observed_marker': marker})
                pump(.2)

            def enter_editor(kind):
                issue_command('/instructions ' + kind, '编辑说明（Enter 完成')

            def replace_and_save(value, refresh=False):
                start = len(decoded)
                send(b'\x01\x0b', 'clear single-line synthetic preference in native editor')
                send(value + '\r', 'submit actual native editor content (empty allowed)')
                wait_for('确认保存说明', since=start)
                send('\r', 'confirm actual save Yes')
                wait_for('现在刷新说明', since=start)
                send('\r' if refresh else b'\x1b', 'choose native reload' if refresh else 'defer reload without changing saved bytes')
                pump(1)

            issue_command('/instructions', '同目录 AGENTS.override.md 遮蔽 AGENTS.md')
            assert 'preferred_file' not in screen() or 'preferences.md' in screen()
            checks['actual_instructions_sources_and_override_status'] = True
            initial = preferences.read_bytes()
            enter_editor('global')
            send('CANCELLED_DRAFT', 'type into native preferences editor')
            send(b'\x1b', 'cancel native editor')
            pump(.3)
            assert preferences.read_bytes() == initial
            checks['native_editor_cancel_leaves_file_unchanged'] = True
            enter_editor('global')
            send(b'\x01\x0b', 'clear synthetic editor before conflict')
            send('UNSAVED_CONFLICT', 'keep a local editor draft')
            preferences.write_text('EXTERNAL_WRITER')
            start = len(decoded)
            send('\r', 'submit edited draft after external modification')
            wait_for('确认保存说明', since=start)
            send('\r', 'confirm save that must detect external conflict')
            wait_for('来源已被外部修改', since=start)
            assert preferences.read_text() == 'EXTERNAL_WRITER'
            start = len(decoded)
            enter_editor('global')
            wait_for('UNSAVED_CONFLICT', since=start)
            send(b'\x1b', 'cancel reopened preserved draft')
            pump(.3)
            checks['native_conflict_preserves_external_file_and_local_draft'] = True
            enter_editor('global')
            replace_and_save('')
            assert preferences.read_text() == ''
            checks['native_editor_empty_save_distinct_from_cancel'] = True
            enter_editor('global')
            replace_and_save('GLOBAL_PREF_B')
            assert preferences.read_text() == 'GLOBAL_PREF_B'
            enter_editor('project')
            replace_and_save('PROJECT_OVERRIDE_B')
            assert (project / 'AGENTS.override.md').read_text() == 'PROJECT_OVERRIDE_B'
            assert (project / 'AGENTS.md').read_text() == 'PROJECT_BASE_A'
            issue_command('/instructions', '修改后待刷新')
            issue_command('/reload', 'Reloaded')
            issue_command('说明刷新后的真实请求', 'NATIVE_CONSULT_DONE')
            latest = requests()[-1]['context']
            system = [m for m in latest['messages'] if m.get('role') == 'system']
            current = [m.get('sections', {}).get('biomind-current') for m in system]
            current = [value for value in current if value]
            native = [m.get('sections', {}).get('project_context') for m in system]
            native = [value for value in native if value]
            assert len(current) == 1 and 'GLOBAL_PREF_B' in current[0] and 'GLOBAL_PREF_A' not in current[0]
            assert native and 'PROJECT_OVERRIDE_B' in native[-1] and 'PROJECT_OVERRIDE_A' not in native[-1]
            assert 'PROJECT_BASE_A' not in native[-1]
            checks['native_save_reload_actual_provider_global_project_B_once'] = True
            old_sessions = set(session_directory.glob('*.jsonl'))
            assert old_sessions and not list(agent.rglob('*.jsonl'))
            issue_command('/new', 'New session started')
            issue_command('新会话使用自定义目录', 'NATIVE_CONSULT_DONE')
            new_sessions = set(session_directory.glob('*.jsonl'))
            assert len(new_sessions - old_sessions) == 1 and not list(agent.rglob('*.jsonl'))
            checks['native_new_retains_custom_session_directory'] = True
            issue_command('/resume', 'Resume Session (Current Folder)')
            send('仅做合成中文咨询', 'filter native current-project resume picker for original session')
            pump(.4)
            start = len(decoded)
            send('\r', 'select original session in the same project')
            wait_for('Resumed session', since=start)
            assert len(requests()) == 3, 'same-project resume itself called provider'
            checks['native_same_project_resume_uses_original_lifecycle'] = True
            project_b = isolated / "项目 B 'quoted'"
            project_b.mkdir()
            # Synthetic-only native JSONL fixture; never copy user history.
            records = [json.loads(line) for line in next(iter(old_sessions)).read_text().splitlines()]
            records[0]['cwd'] = str(project_b)
            records[0]['id'] = str(uuid.uuid4())
            records.append({'type': 'session_info', 'id': 'b-info', 'parentId': records[-1]['id'],
                'timestamp': records[-1]['timestamp'], 'name': 'CROSS_PROJECT_B_SENTINEL'})
            foreign = session_directory / 'synthetic-other-project.jsonl'
            foreign.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in records) + '\n')
            foreign_before = foreign.read_bytes()
            issue_command('/resume', 'Resume Session (Current Folder)')
            send(b'\t', 'native resume toggles all project folders')
            wait_for('Resume Session (All)')
            send('CROSS_PROJECT_B_SENTINEL', 'filter native all-project resume picker')
            pump(.4)
            start = len(decoded)
            send('\r', 'select foreign project through native resume')
            wait_for('目标会话属于其它项目', since=start)
            assert foreign.read_bytes() == foreign_before
            checks['native_cross_project_resume_requires_safe_restart'] = True
            issue_command('/biomind-memory status', '关闭只阻止新的检索')
            issue_command('/biomind-memory history on', '项目知识检索：开启（本次运行）')
            issue_command('/biomind-memory history off', '项目知识检索：关闭（本次运行）')
            before_config = json.loads(config.read_text())
            issue_command('/biomind-memory default on', '保存记忆启动默认值')
            send('\r', 'confirm memory default save')
            wait_for('启动默认值已保存')
            after_config = json.loads(config.read_text())
            assert after_config == {**before_config, 'historyEnabled': True}
            checks['native_memory_runtime_vs_disk_default_separate'] = True
            issue_command('/project', '项目操作（取消保留当前会话）')
            send(b'\x1b', 'cancel native project selector')
            pump(.2)
            start = len(decoded)
            send('/instructions g', 'type native command argument prefix')
            pump(.3)
            send(b'\t', 'complete native command argument with Tab')
            pump(.5)
            assert 'global' in ANSI.sub('', decoded[start:])
            send(b'\x1b', 'Esc closes native completion')
            pump(.3)  # Let Pi distinguish standalone Esc from the next control chord.
            send(b'\x01', 'move to start of completed command draft')
            pump(.1)
            send(b'\x0b', 'clear command draft without submitting a provider request')
            pump(.3)
            checks['native_tab_argument_completion_esc'] = True
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 42, 0, 0))
            os.killpg(process.pid, signal.SIGWINCH)
            pump(.4)
            issue_command('/status', '未验证连通性')
            checks['native_narrow_status_after_work'] = True
            settings = json.loads((agent / 'settings.json').read_text())
            settings['quietStartup'] = True
            (agent / 'settings.json').write_text(json.dumps(settings))
            # Host observes quietStartup at process start; restarting is covered
            # by the dedicated unconfigured/quiet probe, not this reload.
            (evidence / 'instructions-file-after.json').write_text(json.dumps({
                'preferences': preferences.read_text(), 'agents': (project / 'AGENTS.md').read_text(),
                'override': (project / 'AGENTS.override.md').read_text(), 'config_changed_keys': ['historyEnabled'],
                'effective_system_sections': {'biomind-current': current, 'project_context': native}}, ensure_ascii=False, indent=2))
        elif args.session_switch:
            def current_projection(request):
                sections = [m.get('sections', {}).get('biomind-current') for m in request['context']['messages']]
                section = next(value for value in reversed(sections) if value)
                return json.loads(section.rsplit('\n', 1)[-1])

            def fresh_sessions():
                return {json.loads(p.read_text().splitlines()[0])['id']: str(p) for p in agent.rglob('*.jsonl')}

            def domain_states():
                return [json.loads(p.read_text())['task_state'] for p in (isolated / 'domain').rglob('plan_state.json')]

            def owners():
                children_file = Path(f'/proc/{process.pid}/task/{process.pid}/children')
                cli_pids = children_file.read_text().split() if children_file.exists() else []
                pids = []
                for cli in cli_pids:
                    child_file = Path(f'/proc/{cli}/task/{cli}/children')
                    for identity in child_file.read_text().split() if child_file.exists() else []:
                        comm = Path(f'/proc/{identity}/comm')
                        if comm.exists() and comm.read_text().strip().startswith('python'):
                            pids.append(int(identity))
                return sorted(pids)

            pump(.3)
            send('OLD_SESSION_ONLY_USER 仅切换到Generate随后结束，不执行作业。\r', 'old session execution intent without dispatch')
            wait_for('OLD_SESSION_GENERATE_DONE')
            pump(.3)
            old_projection = current_projection(requests()[-1])
            old_sessions, old_owners = fresh_sessions(), owners()
            assert len(old_sessions) == len(old_owners) == 1 and len(requests()) == 3
            assert old_projection['role'] == 'Generate'
            old_id = next(iter(old_sessions))
            old_states = domain_states()
            assert len(old_states) == 1 and old_states[0]['pi_session_id'] == old_id
            marker = len(decoded)
            send('/new\r', 'native idle new-session command')
            wait_for('New session started', since=marker)
            pump(.3)
            assert len(requests()) == 3, 'session replacement itself called the provider'
            marker = len(decoded)
            send('NEW_SESSION_ONLY_USER 只做新的Consult回答。\r', 'first genuine prompt in new native session')
            # Native newSession reloads extensions/providers; the fixture's
            # provider cursor restarts. Identify this reply by new raw bytes and
            # actual new session/context, not an assumed continuing cursor.
            wait_for('NATIVE_CONSULT_DONE', since=marker)
            pump(.3)
            assert len(requests()) == 4
            assert [request['request'] for request in requests()] == [1, 2, 3, 1]
            new_projection = current_projection(requests()[-1])
            final_sessions, new_owners = fresh_sessions(), owners()
            new_ids = set(final_sessions) - set(old_sessions)
            assert len(final_sessions) == 2 and len(new_ids) == 1
            new_id = next(iter(new_ids))
            assert new_id != old_id and len(new_owners) == 1 and new_owners != old_owners
            for pid in old_owners:
                stat = Path(f'/proc/{pid}/stat')
                assert not stat.exists() or stat.read_text().rsplit(') ', 1)[1].split()[0] == 'Z'
            new_context = requests()[-1]['context']
            serialized = json.dumps(new_context, ensure_ascii=False)
            assert new_projection['role'] == 'Consult' and new_projection['request_id'] != old_projection['request_id']
            assert 'OLD_SESSION_' not in serialized and old_projection['request_id'] not in serialized
            tools = [tool['name'] for m in new_context['messages'] for tool in m.get('toolsAdded', [])]
            assert 'handoff_generate' in tools and 'run_command' not in tools and 'install_tool' not in tools
            final_states = domain_states()
            assert len(final_states) == 2 and {s['pi_session_id'] for s in final_states} == {old_id, new_id}
            new_state = next(s for s in final_states if s['pi_session_id'] == new_id)
            assert new_state['pi_request_id'] == new_projection['request_id']
            assert new_state['current_request_key'] != old_states[0]['current_request_key']
            (evidence / 'native-session-switch.json').write_text(json.dumps({
                'old_session_id': old_id, 'new_session_id': new_id, 'old_owner_pids': old_owners, 'new_owner_pids': new_owners,
                'old_projection': old_projection, 'new_projection': new_projection,
                'domain_scope_before': old_states, 'domain_scope_after': final_states,
                'new_context_tools': tools, 'provider_calls': len(requests()), 'fixture_request_indexes': [request['request'] for request in requests()], 'old_owner_closed': True,
                'input_source': 'automatic fixture through actual idle /new; no old user sessions restored'}, ensure_ascii=False, indent=2) + '\n')
            checks['native_idle_new_session_changes_session_and_domain_scope'] = True
            checks['new_consult_has_no_old_generate_context_or_execution_tools'] = True
            checks['old_owner_closed_one_new_owner_no_duplicate_provider_calls'] = True
        elif args.draft_overlays:
            for prefix, marker_name in [('!', 'NATIVE_BASH_SHOULD_NOT_EXIST'), ('!!', 'NATIVE_EXCLUDED_BASH_SHOULD_NOT_EXIST')]:
                pump(.3)
                bash_command = 'printf BYPASS > ' + marker_name
                start = len(decoded)
                send(prefix + bash_command + '\r', 'try real native ' + prefix + ' shortcut with harmless temporary marker')
                wait_for('Shell 快捷入口在 BioMind 模式不可用', since=start)
                pump(.3)
                assert len(requests()) == 1 and not (project / marker_name).exists()
            permission_rows = [row for filename in (isolated / 'domain').rglob('plan_state.json')
                for row in json.loads(filename.read_text()).get('task_state', {}).get('execution_permissions', {}).get('requests', {}).values()]
            assert not permission_rows, 'native shortcut opened an approval path'
            bash_messages = [entry['message'] for filename in agent.rglob('*.jsonl')
                for line in filename.read_text().splitlines() if line.strip()
                for entry in [json.loads(line)] if entry.get('message', {}).get('role') == 'bashExecution']
            assert len(bash_messages) == 2
            assert [m['command'] for m in bash_messages] == ['printf BYPASS > NATIVE_BASH_SHOULD_NOT_EXIST', 'printf BYPASS > NATIVE_EXCLUDED_BASH_SHOULD_NOT_EXIST']
            assert all(m['exitCode'] == 1 and '不可用' in m['output'] for m in bash_messages)
            assert not bash_messages[0].get('excludeFromContext') and bash_messages[1]['excludeFromContext'] is True
            (evidence / 'native-user-bash-denial.json').write_text(json.dumps({'actual_session_messages': bash_messages,
                'marker_exists': False, 'provider_calls_after_shortcut': len(requests()), 'permission_requests': permission_rows,
                'input_source': 'automatic fixture through actual native ! and !! shortcuts'}, ensure_ascii=False, indent=2) + '\n')
            checks['native_user_bash_denied_without_execution_or_approval'] = True
            exact_inputs = []
            for label in ['QUESTION', 'APPROVAL']:
                pump(.3)
                send('开始合成覆盖层 ' + label + '\r', 'request actual native overlay ' + label)
                wait_for(label + '_PRELUDE_BEGIN')
                before = len(requests())
                send(overlay_drafts[label]['draft'], 'type ordinary draft during streamed prelude ' + label)
                send(b'\x1b[D' * 6, 'place cursor six characters left BEFORE overlay ' + label)
                wait_for('OVERLAY_QUESTION' if label == 'QUESTION' else 'OVERLAY_INSTALL_CANCEL')
                pump(.3)
                assert len(requests()) == before, 'ordinary draft was submitted or overlay auto-resolved'
                if label == 'QUESTION':
                    send('/synthetic/独立回答.tsv\r', 'answer real ask_user input without submitting ordinary draft')
                else:
                    send(b'\x1b', 'cancel real approval selector without changing ordinary draft')
                wait_for('OVERLAY_' + label + '_DONE')
                pump(.3)
                if label == 'QUESTION':
                    assert receipt('overlay-question')['answer'] == '/synthetic/独立回答.tsv'
                else:
                    assert receipt('overlay-install')['status'] == 'cancelled'
                    assert not (project / 'OVERLAY_SHOULD_NOT_EXIST').exists()
                # No cursor relocation here: insertion must occur at the exact
                # position chosen before the independent native overlay opened.
                send('_KEPT_\r', 'edit restored pre-overlay cursor and submit ordinary draft ' + label)
                wait_for('OVERLAY_' + label + '_DRAFT_ACCEPTED')
                user = [m for m in requests()[-1]['context']['messages'] if m['role'] == 'user'][-1]
                actual = user['content'] if isinstance(user['content'], str) else ''.join(
                    item['text'] for item in user['content'] if item['type'] == 'text')
                assert actual == overlay_drafts[label]['expected'], ('draft or pre-overlay cursor changed', label, actual)
                exact_inputs.append({'overlay': label, 'expected': overlay_drafts[label]['expected'], 'actual': actual})
                checks['draft_and_pre_overlay_cursor_preserved_' + label.lower()] = True
            assert len(requests()) == len(steps)
            (evidence / 'draft-overlay-exact-inputs.json').write_text(json.dumps(exact_inputs, ensure_ascii=False, indent=2) + '\n')
        elif args.positive_install:
            pump(.3)
            send('执行一次合成安装标记，并等待我明确选择接受。\r', 'fixture user requests one synthetic installation proposal')
            wait_for('NATIVE_INSTALL_ACCEPT')
            before = len(requests())
            pump(.5)
            assert len(requests()) == before and not accepted_marker.exists(), 'installation dispatched before explicit UI approval'
            send(b'\x1b[B\r', 'fixture user selects explicit accept option')
            wait_for('NATIVE_APPROVAL_ACCEPT_DONE', timeout=45)
            accepted = receipt('native-install-accept')
            assert accepted['status'] == 'registered', accepted
            assert accepted_marker.read_text() == 'synthetic', 'fake installation must execute exactly once'
            registered = json.loads(registry.read_text())['environments']['synthetic-positive']
            assert registered['status'] == 'verified'
            assert Path(registered['prefix']).resolve() == accepted_prefix.resolve()
            assert len(requests()) == len(steps)
            checks['explicit_accept_dispatch_verify_register_once'] = True
        elif args.negative_inputs:
            previous = None
            for label in ['WHITESPACE', 'N', 'PASTE']:
                pump(.3)
                send('只测试非授权输入 ' + label + '\r', 'fixture negative approval scenario ' + label)
                wait_for('NATIVE_NEGATIVE_' + label)
                before = len(requests())
                pump(.3)
                if label == 'WHITESPACE':
                    send(b'   \r', 'spaces followed by Enter must not approve')
                elif label == 'N':
                    send(b'n\r', 'literal n followed by Enter must not approve')
                else:
                    assert previous is not None
                    stale = {'request_id': previous['request_id'], 'action_hash': previous['action_hash'], 'choice': 'y'}
                    pasted = '这只是普通反馈，不能作为批准。\n' + json.dumps(stale, ensure_ascii=False) + '\ny\n'
                    send('\x1b[200~' + pasted + '\x1b[201~', 'bracketed ordinary text and old proposal response pasted into current approval')
                    pump(.7)
                    assert len(requests()) == before, 'bracketed paste consumed as an approval/response'
                    assert not (project / ('NEGATIVE_' + label + '_SHOULD_NOT_EXIST')).exists()
                    send(b'\x1b', 'explicitly cancel after ignored pasted old response')
                wait_for('NATIVE_NEGATIVE_DONE_' + label)
                response = receipt('negative-install-' + label)
                assert response['status'] in {'denied', 'cancelled'}, response
                assert not (project / ('NEGATIVE_' + label + '_SHOULD_NOT_EXIST')).exists()
                previous = response['approval']
                checks['negative_' + label.lower() + '_not_approved'] = True
            assert len(requests()) == len(steps)
        else:
            pump(.3)
            send('合成问答\r', 'user question prompt')
            wait_for('NATIVE_QUESTION')
            before = len(requests())
            pump(.8)
            assert len(requests()) == before, 'question waiter caused a model call'
            send('/synthetic/真实终端回答.tsv\r', 'actual typed synthetic answer')
            wait_for('NATIVE_QUESTION_DONE')
            assert receipt('native-question')['answer'] == '/synthetic/真实终端回答.tsv'
            checks['question_wait_and_answer'] = True
            pump(.3)
            send('开始合成执行界面测试\r', 'execution prompt')
            wait_for('NATIVE_INSTALL_DENIAL')
            pump(.3)
            send(b'\r', 'empty Enter at approval defaults to deny')
            wait_for('GEN_STREAM_BEGIN')
            before = len(requests())
            send(b'\r', 'empty Enter during generation')
            pump(.3)
            send(draft, 'draft typed during generation without submitting')
            pump(.3)
            send(b'\x1b[5~', 'PageUp during long stream')
            pump(.3)
            send(b'\x1b[6~', 'PageDown during long stream')
            pump(.3)
            send(b'\x1b[F', 'End returns native viewport to live bottom')
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 28, 90, 0, 0))
            # Only signal the child native CLI created by this private host.
            children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
            for identity in children:
                os.kill(int(identity), signal.SIGWINCH)
            actions.append({'resize': {'rows': 28, 'columns': 90}, 'raw_offset': len(raw), 'signalled_children': children})
            wait_for('GEN_STREAM_END')
            pump(.4)
            assert len(requests()) == before, 'empty Enter or draft caused premature model request'
            assert not (project / 'SHOULD_NOT_EXIST').exists(), 'default approval unexpectedly executed'
            assert receipt('native-install-deny')['status'] == 'denied'
            checks['approval_default_denied'] = True
            checks['empty_enter_during_generation_no_request'] = True
            send(b'\x1b[D' * 6 + cursor_marker.encode(), 'move cursor six characters left and insert marker after streaming/resize')
            send(b'\r', 'submit retained and cursor-edited draft after stream')
            wait_for('NATIVE_DRAFT_ACCEPTED')
            latest_user = [message for message in requests()[-1]['context']['messages'] if message['role'] == 'user'][-1]
            actual_draft = latest_user['content'] if isinstance(latest_user['content'], str) else ''.join(
                block['text'] for block in latest_user['content'] if block['type'] == 'text')
            assert actual_draft == edited_draft, ('native draft or semantic cursor position changed', actual_draft)
            checks['draft_preserved_and_submitted'] = True
            checks['cursor_edit_after_stream_and_resize_exact'] = True
            checks['long_chinese_stream_page_keys_no_corruption'] = True
            checks['resize_during_stream_received'] = bool(children)
            pump(.3)
            send('合成中断流\r', 'start cancellable synthetic stream')
            wait_for('ABORT_STREAM_BEGIN')
            send(b'\x1b', 'Escape abort current model stream')
            pump(1)
            assert 'ABORT_UNEXPECTED_FINISH' not in screen()
            send('中断后恢复\r', 'new turn after abort')
            wait_for('NATIVE_RECOVERY_DONE')
            checks['escape_abort_and_recovery'] = True
            pump(.3)
            send('合成取消安装\r', 'new request for explicit approval cancellation')
            wait_for('NATIVE_INSTALL_CANCEL')
            pump(.3)
            send(b'\x1b', 'Escape cancels approval')
            wait_for('NATIVE_APPROVAL_CANCEL_DONE')
            assert not (project / 'CANCEL_SHOULD_NOT_EXIST').exists()
            assert receipt('native-install-cancel')['status'] == 'cancelled'
            checks['approval_escape_cancelled'] = True
            pump(.3)
            send('合成编辑安装\r', 'new request for edit-without-approval test')
            wait_for('NATIVE_INSTALL_EDIT')
            pump(.3)
            send(b'\x1b[B\x1b[B\r', 'select edit rather than accept')
            wait_for('输入修改后的命令')
            start = len(decoded)
            send('printf edited > EDIT_NEW_SHOULD_NOT_EXIST\r', 'edit proposal command without approval')
            wait_for('NATIVE_INSTALL_EDIT', since=start)
            before = len(requests())
            pump(.6)
            assert len(requests()) == before, 'editing resumed provider before a separate UI decision'
            assert not (project / 'EDIT_OLD_SHOULD_NOT_EXIST').exists()
            assert not (project / 'EDIT_NEW_SHOULD_NOT_EXIST').exists()
            send(b'\x1b', 'cancel the new edited proposal')
            wait_for('NATIVE_APPROVAL_EDIT_DONE')
            edited = receipt('native-install-edit')
            assert edited['status'] == 'cancelled'
            assert edited['approval']['payload']['code'] == 'printf edited > EDIT_NEW_SHOULD_NOT_EXIST'
            checks['edit_requires_separate_decision_and_cancelled'] = True
            pump(.4)
            assert len(requests()) == len(steps), f'unexpected provider call count {len(requests())}'
        status = 'PASS'
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
    finally:
        if process.poll() is None:
            send(b'\x03', 'Ctrl+C exit first key')
            pump(.15)
            send(b'\x03', 'Ctrl+C exit second key')
            pump(2)
        if process.poll() is None:
            # Only isolated synthetic tasks were admitted. Closing the UI
            # terminal never invokes exec.cancel or a process-group kill.
            os.close(master)
            master = -1
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if status == 'PASS' and args.protection_ui:
            if 'biomind --session' not in screen():
                status = 'FAIL'; failure = 'PRODUCT_RESUME_HINT_NOT_RENDERED'
            else:
                checks['product_exit_resume_hint'] = True
        if status == 'PASS' and args.instructions_ui:
            restart_command = command[:-2] + ['--pi-mode', 'json']
            restarted = subprocess.Popen(restart_command, cwd=project, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            restart_frames = []
            pending_bytes = b''
            try:
                def frame_until(kind, timeout=60):
                    nonlocal pending_bytes
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        while b'\n' in pending_bytes:
                            line, pending_bytes = pending_bytes.split(b'\n', 1)
                            frame = json.loads(line)
                            restart_frames.append(frame)
                            if frame.get('type') == kind:
                                return frame
                        ready, _, _ = select.select([restarted.stdout], [], [], .1)
                        if ready:
                            chunk = os.read(restarted.stdout.fileno(), 65536)
                            if not chunk:
                                raise AssertionError('fresh JSON process ended before ' + kind)
                            pending_bytes += chunk
                    raise AssertionError('fresh JSON frame timeout ' + kind)
                fresh_ready = frame_until('ready')
                restarted.stdin.write(b'{"type":"settings","id":"fresh-default"}\n')
                restarted.stdin.flush()
                default_result = frame_until('settings_result')
                assert default_result['status'] == 'ok' and default_result['history_enabled'] is True
                assert Path(fresh_ready['sessionDir']) == session_directory
                restarted.stdin.write(b'{"type":"shutdown"}\n')
                restarted.stdin.flush()
                restarted.wait(timeout=15)
                assert restarted.returncode == 0
                checks['fresh_product_json_consumes_saved_default_runtime_off_not_persisted'] = True
            except BaseException as error:
                status = 'FAIL'
                failure = 'SECOND_PROCESS:' + str(error)
            finally:
                if restarted.poll() is None:
                    restarted.terminate()
                    restarted.wait(timeout=10)
                (evidence / 'memory-default-restart.json').write_text(json.dumps({
                    'command': restart_command, 'exit_code': restarted.returncode, 'frames': restart_frames,
                    'stderr': restarted.stderr.read().decode('utf8', 'replace'), 'evidence_type': 'PRODUCT_JSON'},
                    ensure_ascii=False, indent=2) + '\n')
        if master >= 0:
            os.close(master)
        (evidence / 'registry-after.json').write_text(registry.read_text())
        if args.positive_install and accepted_marker.exists():
            (evidence / 'installation-marker.txt').write_text(accepted_marker.read_text())
        (evidence / 'native-terminal.raw').write_bytes(raw)
        (evidence / 'native-terminal.txt').write_text(screen())
        sessions = []
        # These paths were created by this probe; no prior session is restored.
        for filename in list(agent.rglob('*.jsonl')) + (list(session_directory.rglob('*.jsonl')) if session_directory.exists() else []):
            sessions.append({'path': str(filename), 'entries': [json.loads(line) for line in filename.read_text().splitlines() if line.strip()]})
        (evidence / 'native-fresh-sessions.json').write_text(json.dumps(sessions, ensure_ascii=False, indent=2))
        result = {'status': status, 'failure': failure, 'checks': checks, 'command': command, 'cwd': str(project),
            'isolated_root': str(isolated), 'native_exit': process.returncode, 'actions': actions,
            'extension_sha256': extension_hash, 'positive_install_fixture': args.positive_install, 'negative_input_fixture': args.negative_inputs,
            'draft_overlay_fixture': args.draft_overlays,
            'session_switch_fixture': args.session_switch,
            'instructions_ui_fixture': args.instructions_ui,
            'unconfigured_ui_fixture': args.unconfigured_ui,
            'plain_unconfigured_fixture': args.plain_unconfigured,
            'project_draft_ui_fixture': args.project_draft_ui,
            'protection_ui_fixture': args.protection_ui,
            'provider_request_count': len(requests()), 'expected_provider_request_count': 0 if args.unconfigured_ui else 3 if args.instructions_ui else 4 if args.session_switch else len(steps),
            'scientific_success': None, 'real_ime_composition': 'NOT_RUN',
            'visual_scroll_position_assertion': 'NOT_RUN', 'fullscreen_screen_pixels': 'NOT_RUN'}
        (evidence / 'native-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False))
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

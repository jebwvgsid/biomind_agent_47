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


ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--positive-install', action='store_true')
    parser.add_argument('--negative-inputs', action='store_true')
    parser.add_argument('--draft-overlays', action='store_true')
    parser.add_argument('--session-switch', action='store_true')
    args = parser.parse_args()
    if sum((args.positive_install, args.negative_inputs, args.draft_overlays, args.session_switch)) > 1:
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
    (evidence / 'registry-before.json').write_text(registry.read_text())
    fixture = isolated / 'provider-fixture.json' 
    fixture.write_text(json.dumps(steps, ensure_ascii=False))
    config = isolated / 'runtime.json'
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(agent), 'dataRoot': str(isolated / 'domain'),
        'providerFixture': str(fixture), 'registryPath': str(registry), 'preferencesPath': str(preferences),
        'memoryRoot': str(isolated / 'memory'), 'wikiRoot': str(isolated / 'wiki'), 'historyEnabled': False}, ensure_ascii=False))
    command = [sys.executable, str(repository / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project),
        '--pi-config', str(config), '--prompt', '仅做合成中文咨询，不执行分析。']
    master, slave = pty.openpty()
    extension_hash = hashlib.sha256((runtime / 'dist/extensions/biomind.js').read_bytes()).hexdigest()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
    environment = {'PATH': str(runtime / '.runtime' / 'node' / 'bin') + ':/usr/bin:/bin', 'TERM': 'xterm-256color',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '0',
        'NODE_OPTIONS': '--import=' + str(runtime / 'testing' / 'offline-guard.mjs'),
        'BIOMIND_PI_TEST_ROOT': str(isolated), 'BIOMIND_PI_GUARD_TRACE': str(evidence / 'native-guard.jsonl')}
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
        wait_for('NATIVE_CONSULT_DONE', timeout=60 if args.draft_overlays or args.session_switch else 30)
        checks['native_cli_chinese_render'] = True
        assert 'DOMAIN_' not in screen(), 'domain initialization failed before the native Consult'
        assert 'biomind-current' in json.dumps(requests()[0]['context']), 'BioMind business context was not attached'
        checks['native_consult_with_domain_context'] = True
        if args.session_switch:
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
        if master >= 0:
            os.close(master)
        (evidence / 'registry-after.json').write_text(registry.read_text())
        if args.positive_install and accepted_marker.exists():
            (evidence / 'installation-marker.txt').write_text(accepted_marker.read_text())
        (evidence / 'native-terminal.raw').write_bytes(raw)
        (evidence / 'native-terminal.txt').write_text(screen())
        sessions = []
        # These paths were created by this probe; no prior session is restored.
        for filename in agent.rglob('*.jsonl'):
            sessions.append({'path': str(filename), 'entries': [json.loads(line) for line in filename.read_text().splitlines() if line.strip()]})
        (evidence / 'native-fresh-sessions.json').write_text(json.dumps(sessions, ensure_ascii=False, indent=2))
        result = {'status': status, 'failure': failure, 'checks': checks, 'command': command, 'cwd': str(project),
            'isolated_root': str(isolated), 'native_exit': process.returncode, 'actions': actions,
            'extension_sha256': extension_hash, 'positive_install_fixture': args.positive_install, 'negative_input_fixture': args.negative_inputs,
            'draft_overlay_fixture': args.draft_overlays,
            'session_switch_fixture': args.session_switch,
            'provider_request_count': len(requests()), 'expected_provider_request_count': 4 if args.session_switch else len(steps),
            'scientific_success': None, 'real_ime_composition': 'NOT_RUN',
            'visual_scroll_position_assertion': 'NOT_RUN', 'fullscreen_screen_pixels': 'NOT_RUN'}
        (evidence / 'native-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False))
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

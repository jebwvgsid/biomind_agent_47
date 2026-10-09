#!/usr/bin/env python3
"""BioMind launcher -> published Pi bin -> bundled native /compact, offline only."""
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
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    runtime = repo / 'integrations/pi_runtime'
    out = args.evidence_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    base = Path(tempfile.mkdtemp(prefix='biomind-compaction-native-'))
    project, agent, home = [base / part for part in ('synthetic project', 'agent', 'home')]
    for directory in (project, agent, home, agent / 'bin'):
        directory.mkdir()
    for tool in ('fd', 'rg'):
        (agent / 'bin' / tool).symlink_to(runtime / '.biomind/pi-agent/bin' / tool)
    (agent / 'settings.json').write_text(json.dumps({
        'enableAnalytics': False, 'enableInstallTelemetry': False, 'cacheWarming': 'off',
        'retry': {'enabled': False}, 'compaction': {'enabled': False, 'reserveTokens': 512, 'keepRecentTokens': 128},
    }))
    registry, preferences = base / 'registry.json', base / 'preferences.txt'
    registry.write_text('{"environments":{},"tools":{}}')
    preferences.write_text('Synthetic offline compaction wiring witness only.')
    trace = out / 'provider-requests.jsonl'
    fixture = base / 'provider.json'
    def reply(value, **extra):
        return {'content': [{'type': 'text', 'text': value}], **extra}
    fixture.write_text(json.dumps([
        reply('NATIVE_ADDENDUM_READY: submitted job only; full execution remains unverified.', trace_path=str(trace)),
        reply('NATIVE_ADDENDUM_RECENT_READY: observations unchanged.'),
        reply('NATIVE_ADDENDUM_LATEST_READY: observations still unchanged.'),
        *[reply('SYNTHETIC_NATIVE_SUMMARY_TRANSPORT_ONLY: job submitted; no full completion or quality claim.') for _ in range(4)],
    ]))
    config = base / 'runtime.json'
    config.write_text(json.dumps({
        'python': sys.executable, 'agentDir': str(agent), 'dataRoot': str(base / 'domain'),
        'registryPath': str(registry), 'preferencesPath': str(preferences),
        'memoryRoot': str(base / 'memory'), 'wikiRoot': str(base / 'wiki'),
        'historyEnabled': False, 'providerFixture': str(fixture), 'executionProfile': 'native',
    }))
    focus = 'NATIVE_USER_FOCUS_V1 保留用户原话和仅提交状态'
    command = [sys.executable, str(repo / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project),
               '--pi-config', str(config), '--prompt', 'USER_QUOTE_NATIVE: 只分析样本 A、B；作业 synthetic-42 仅提交，环境未知。']
    environment = {
        'HOME': str(home), 'PI_CODING_AGENT_DIR': str(agent), 'PATH': str(runtime / '.runtime/node/bin') + ':/usr/bin:/bin',
        'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PI_OFFLINE': '1',
        'BIOMIND_DISABLE_DOTENV': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'NODE_OPTIONS': '--max-old-space-size=512 --import=' + str(runtime / 'testing/offline-guard.mjs'),
        'BIOMIND_PI_TEST_ROOT': str(base), 'BIOMIND_PI_GUARD_TRACE': str(out / 'guard.jsonl'),
        'BIOMIND_PI_EXPECT_EMBEDDED_SDK': '1',
    }
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 38, 132, 0, 0))
    child = subprocess.Popen(command, cwd=project, env=environment, stdin=slave, stdout=slave,
                             stderr=slave, close_fds=True, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    decoded = ''
    ansi = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]')
    report = {'status': 'FAIL', 'scope': 'Real BioMind launcher and published Pi native bundle; synthetic provider, no real model quality evaluation.',
              'command': command, 'isolated_root': str(base), 'checks': {}}

    def pump():
        nonlocal decoded
        ready, _, _ = select.select([master], [], [], .1)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            raw.extend(chunk)
            decoded += decoder.decode(chunk)
            assert len(raw) < 8 * 1024 * 1024

    def wait(predicate, label, timeout=60):
        until = time.monotonic() + timeout
        while not predicate():
            if child.poll() is not None:
                raise RuntimeError(f'Native process exited before {label}: {child.returncode}')
            if time.monotonic() > until:
                raise RuntimeError('Native witness timeout: ' + label)
            pump()

    def entries():
        result = []
        for filename in (agent / 'sessions').rglob('*.jsonl'):
            for line in filename.read_text().splitlines():
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # Native writer may still be publishing the final line.
        return result

    try:
        wait(lambda: 'NATIVE_ADDENDUM_READY' in ansi.sub('', decoded), 'initial ordinary reply')
        # A long current user turn forces native split-turn handling at its own cutoff.
        os.write(master, ('\x1b[200~' + 'RECENT_NATIVE_MATERIAL 仅保留已知状态，不推断全量完成。' * 150 + '\x1b[201~\r').encode())
        wait(lambda: 'NATIVE_ADDENDUM_RECENT_READY' in ansi.sub('', decoded), 'recent ordinary reply')
        os.write(master, ('\x1b[200~' + 'LATEST_NATIVE_MATERIAL 环境未知，保持原有样本范围。' * 150 + '\x1b[201~\r').encode())
        wait(lambda: 'NATIVE_ADDENDUM_LATEST_READY' in ansi.sub('', decoded), 'latest ordinary reply')
        os.write(master, ('/compact ' + focus + '\r').encode())
        wait(lambda: any(row.get('type') == 'compaction' for row in entries()), 'native persisted compaction')
        requests = [json.loads(line) for line in trace.read_text().splitlines()]
        summaries = [row for row in requests if any(message.get('role') == 'system'
            and 'You are a context summarization assistant.' in str(message.get('content'))
            for message in row['context']['messages'])]
        kinds = set()
        for row in summaries:
            text = '\n'.join(part['text'] for message in row['context']['messages'] if message.get('role') == 'user'
                for part in message.get('content', []) if part.get('type') == 'text')
            kinds.add('history' if '<conversation>' in text else 'prefix')
            assert text.count('<biomind_compaction_addendum_v1>') == 1
            assert text.count(focus) == 1
            assert 'BioMind 生信任务压缩增补 v1' in text
        assert kinds == {'history', 'prefix'}, kinds
        assert len(summaries) == 2, len(summaries)
        stored = [row for row in entries() if row.get('type') == 'compaction']
        assert len(stored) == 1
        assert stored[0].get('fromHook') is not True
        assert 'SYNTHETIC_NATIVE_SUMMARY_TRANSPORT_ONLY' in stored[0]['summary']
        (out / 'native-summary.json').write_text(json.dumps(stored[0], ensure_ascii=False, indent=2))
        guard = [json.loads(line) for line in (out / 'guard.jsonl').read_text().splitlines()]
        assert not [row for row in guard if row.get('kind') == 'network_attempt'
            or row.get('kind') == 'sensitive_file_attempt' and row.get('allowed') is not True], guard
        assert any(row.get('kind') == 'native_embedded_sdk_guard' for row in guard), guard
        report.update(status='PASS', checks={'native_compact_command': True, 'published_bundle_loaded': True,
            'history_and_split_prefix_requests': True, 'default_and_user_focus_once_each': True,
            'history_disabled_summary_kept': True, 'native_session_storage': True},
            summary_requests=len(summaries), summary_kinds=sorted(kinds),
            native_extension_sha256=hashlib.sha256((runtime / 'dist/biomind-extension.ts').read_bytes()).hexdigest())
    except Exception as error:
        report['error'] = repr(error)
        raise
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        os.close(master)
        (out / 'terminal.raw').write_bytes(raw)
        (out / 'terminal.txt').write_text(ansi.sub('', decoded))
        (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()

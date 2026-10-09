#!/usr/bin/env python3
"""Capture actual BioMind -> published Pi CLI requests before/after prompt edits.

The provider reply is synthetic. This witnesses assembly, not model judgment.
"""
import argparse
import codecs
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--phase', choices=['before', 'after'], required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    rt = repo / 'integrations/pi_runtime'
    base = args.root.resolve()
    out = base / args.phase
    out.mkdir(parents=True, exist_ok=False)
    project, agent, home = [base / name for name in ('project', 'agent', 'home')]
    for folder in (project, agent, home, agent / 'bin'):
        folder.mkdir(exist_ok=True)
    for name in ('fd', 'rg'):
        target = agent / 'bin' / name
        if not target.exists():
            target.symlink_to(rt / '.biomind/pi-agent/bin' / name)
    (agent / 'settings.json').write_text(json.dumps({'enableAnalytics': False, 'enableInstallTelemetry': False,
        'cacheWarming': 'off', 'retry': {'enabled': False}, 'compaction': {'enabled': False}}))
    registry, preference = base / 'registry.json', base / 'preferences.txt'
    registry.write_text('{"environments":{},"tools":{}}')
    preference.write_text('SYNTHETIC_STABLE_PREFERENCE: 回答简洁。')
    trace = out / 'provider-requests.jsonl'
    fixture, config = base / 'provider.json', base / 'runtime.json'
    fixture.write_text(json.dumps([{'trace_path': str(trace), 'content': [
        {'type': 'text', 'text': 'MAIN_PROMPT_ASSEMBLY_WITNESS_READY: synthetic reply, no model quality claim.'}]}]))
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(agent), 'dataRoot': str(base / 'domain'),
        'registryPath': str(registry), 'preferencesPath': str(preference), 'providerFixture': str(fixture),
        'memoryRoot': str(base / 'memory'), 'wikiRoot': str(base / 'wiki'), 'historyEnabled': False, 'executionProfile': 'native'}))
    command = [sys.executable, str(repo / 'bio_agents.py'), '--engine', 'pi', '--project-dir', str(project),
        '--pi-config', str(config), '--prompt', '仅讨论已有登记环境中的 base-R ORA 方案，不修改文件或执行分析。']
    environment = {'HOME': str(home), 'PI_CODING_AGENT_DIR': str(agent),
        'PATH': str(rt / '.runtime/node/bin') + ':/usr/bin:/bin', 'TERM': 'xterm-256color',
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PI_OFFLINE': '1', 'BIOMIND_DISABLE_DOTENV': '1',
        'PYTHONDONTWRITEBYTECODE': '1', 'BIOMIND_PI_TEST_ROOT': str(base),
        'NODE_OPTIONS': '--max-old-space-size=512 --import=' + str(rt / 'testing/offline-guard.mjs'),
        'BIOMIND_PI_GUARD_TRACE': str(out / 'guard.jsonl'), 'BIOMIND_PI_EXPECT_EMBEDDED_SDK': '1'}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 132, 0, 0))
    child = subprocess.Popen(command, cwd=project, env=environment, stdin=slave, stdout=slave,
        stderr=slave, close_fds=True, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    decoded = ''
    report = {'phase': args.phase, 'status': 'FAIL', 'model_quality': 'NOT_RUN', 'command': command}
    try:
        until = time.monotonic() + 90
        while 'MAIN_PROMPT_ASSEMBLY_WITNESS_READY' not in decoded:
            if time.monotonic() > until or child.poll() is not None:
                raise RuntimeError(f'Native prompt witness stopped before reply: {child.poll()}')
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                chunk = os.read(master, 65536)
                raw.extend(chunk)
                decoded += decoder.decode(chunk)
                assert len(raw) < 8 * 1024 * 1024
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        assert len(records) == 1
        messages = records[0]['context']['messages']
        current = [m['sections']['biomind-current'] for m in messages if m.get('role') == 'system'
            and isinstance(m.get('sections', {}).get('biomind-current'), str)]
        assert len(current) == 1
        assert current[0].count('当前 execution:native。') == 1
        assert '当前 execution:protected。' not in current[0]
        assert '当前阶段：记忆维护。' not in current[0]
        assert current[0].count('【任务推进】') == (1 if args.phase == 'after' else 0)
        sections = {k: v for m in messages if m.get('role') == 'system' for k, v in m.get('sections', {}).items()}
        assert sections['preamble'].startswith('You are an expert coding assistant operating inside pi,')
        assert all(name in sections for name in ('tools', 'rules', 'docs', 'skills', 'cwd'))
        guards = [json.loads(line) for line in (out / 'guard.jsonl').read_text().splitlines()]
        assert any(row['kind'] == 'native_embedded_sdk_guard' for row in guards)
        assert not any(row['kind'] == 'network_attempt' or row['kind'] == 'sensitive_file_attempt'
            and row.get('allowed') is not True for row in guards)
        report.update(status='PASS', requests=1, one_biomind_current=True, native_only=True,
            pi_base_sections_preserved=True, native_bundle_sha256=hashlib.sha256((rt / 'dist/biomind-extension.ts').read_bytes()).hexdigest())
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
        (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()

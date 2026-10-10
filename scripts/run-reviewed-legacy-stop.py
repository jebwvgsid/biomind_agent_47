#!/usr/bin/env python3
"""Versioned shared-directory wrapper: stop on the verified host, then audit."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import uuid


def main():
    bundle = Path(__file__).resolve().parent
    manifest = json.loads((bundle / 'STOP-BUNDLE.json').read_text())
    for item in manifest['files']:
        if hashlib.sha256((bundle / item['path']).read_bytes()).hexdigest() != item['sha256']:
            print(json.dumps({'status': 'BLOCKED', 'code': 'STOP_BUNDLE_CHANGED'}), flush=True)
            return 2
    if Path('/proc/1/comm').read_text().strip() not in ('systemd', 'init'):
        print(json.dumps({'status': 'BLOCKED', 'code': 'STOP_REAL_HOST_NAMESPACE_REQUIRED'}), flush=True)
        return 2
    plan_path = bundle / 'stop-plan.json'
    plan = json.loads(plan_path.read_text())
    assert re.fullmatch(r'[A-Za-z0-9_.-]+', plan['host'])
    evidence = Path(manifest['evidenceDirectory'])
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report = evidence / ('stop-' + stamp + '-' + uuid.uuid4().hex[:8] + '.json')
    command = [manifest['python'], '-B', str(bundle / 'stop-legacy-sessions.py'),
               '--plan', str(plan_path), '--apply', '--receipt', str(report)]
    if socket.gethostname() != plan['host']:
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=15', '-o', 'ConnectionAttempts=1', plan['host'], shlex.join(command)]
    print('Stopping reviewed old BioMind owners; checking process identities first...', flush=True)
    # The real-host stop command saves its receipt independently of SSH stdout.
    try:
        response = subprocess.run(command, capture_output=True, text=True, timeout=100)
        result = json.loads(response.stdout)
        if not isinstance(result, dict) or result.get('schema') != 'BioMindLegacySessionStopV1':
            raise ValueError()
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({'status': 'BLOCKED', 'code': 'STOP_REMOTE_REPLY_UNAVAILABLE',
                          'outcome': 'UNKNOWN_CHECK_RECEIPT', 'report': str(report)}, ensure_ascii=False), flush=True)
        return 2
    print(json.dumps({**result, 'report': str(report)}, ensure_ascii=False), flush=True)
    print('Running a new read-only maintenance preflight; stopping owners does not imply adoption.', flush=True)
    try:
        checked = subprocess.run([manifest['auditPython'], '-B', manifest['preflight']], timeout=180)
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({'status': 'BLOCKED', 'code': 'POST_STOP_AUDIT_UNAVAILABLE'}), flush=True)
        return 2
    return 0 if response.returncode == 0 and checked.returncode == 0 else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError):
        print(json.dumps({'status': 'BLOCKED', 'code': 'STOP_BUNDLE_UNAVAILABLE'}), flush=True)
        raise SystemExit(2)

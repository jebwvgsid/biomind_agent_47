#!/usr/bin/env python3
"""Read-only maintenance evidence. NEVER grants permission to adopt old scopes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys


def ancestors():
    result, pid = set(), os.getpid()
    while pid > 0 and pid not in result:
        result.add(pid)
        try:
            text = Path(f'/proc/{pid}/stat').read_text()
            pid = int(text[text.rfind(')') + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--agent-dir', required=True, type=Path)
    p.add_argument('--project-dir', required=True, type=Path)
    p.add_argument('--expected-host', required=True)
    p.add_argument('--frozen-entry', required=True, type=Path,
                   help='Operator-provided already-frozen official entry; only metadata/hash is inspected')
    args = p.parse_args()
    checks = {}
    hostname = socket.gethostname()
    checks['expectedHost'] = 'PASS' if hostname == args.expected_host else 'FAIL'
    checks['sshContextPresent'] = 'PASS' if os.environ.get('SSH_CONNECTION') else 'BLOCKED'
    try:
        init = Path('/proc/1/comm').read_text().strip()
        checks['hostPidNamespace'] = 'PASS' if init in ('systemd', 'init') else 'BLOCKED'
    except OSError:
        checks['hostPidNamespace'] = 'BLOCKED'
    excluded = ancestors()
    same_user, unreadable = [], 0
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal() or int(directory.name) in excluded:
            continue
        try:
            if directory.stat().st_uid == os.getuid():
                # No cmdline/environ/cwd reads: those can contain private task data.
                same_user.append(int(directory.name))
        except OSError:
            unreadable += 1
    checks['sameUidProcessOccupancy'] = 'PASS' if not same_user and not unreadable else 'BLOCKED'
    entry_sha = None
    try:
        entry = args.frozen_entry.resolve(strict=True)
        info = entry.stat()
        if entry.name not in ('biomind', 'bio_agents.py', 'launcher.sh') or not entry.is_file() or info.st_size > 65536:
            raise ValueError()
        entry_sha = hashlib.sha256(entry.read_bytes()).hexdigest()
        # A readable hash is identity evidence, not proof that an old launcher
        # can no longer be invoked through another path or existing shell.
        checks['oldAdmissionSealed'] = 'BLOCKED'
    except (OSError, ValueError):
        checks['oldAdmissionSealed'] = 'FAIL'
    jobs = None
    if shutil.which('squeue'):
        try:
            reply = subprocess.run(['squeue', '-h', '-u', pwd.getpwuid(os.getuid()).pw_name, '-o', '%i'],
                                   capture_output=True, text=True, timeout=15, check=True)
            jobs = len([row for row in reply.stdout.splitlines() if row.strip()])
            checks['slurmJobsExited'] = 'PASS' if jobs == 0 else 'BLOCKED'
        except (OSError, subprocess.SubprocessError):
            checks['slurmJobsExited'] = 'BLOCKED'
    else:
        checks['slurmJobsExited'] = 'NOT_RUN'
    # An empty local /proc and an exclusive new lock say nothing about legacy
    # workers on other login/compute hosts sharing this package storage.
    checks['allResourceHostsAndLegacyRegistriesVerified'] = 'BLOCKED'
    result = {'schema': 'BioMindPluginMaintenanceAuditV1', 'status': 'BLOCKED',
              'code': 'LEGACY_OCCUPANCY_UNVERIFIED', 'adoptionAuthorized': False,
              'host': hostname, 'agentDir': str(args.agent_dir.resolve()),
              'projectDir': str(args.project_dir.resolve()), 'frozenEntrySha256': entry_sha,
              'sameUidOtherProcesses': len(same_user), 'unreadableProcessEntries': unreadable,
              'activeSlurmJobs': jobs, 'checks': checks,
              'changedFiles': False, 'signalsSent': False, 'credentialsRead': False}
    print(json.dumps(result, ensure_ascii=False))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())

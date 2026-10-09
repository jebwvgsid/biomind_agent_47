#!/usr/bin/env python3
"""Read-only, bounded source pointers for this user's visible BioMind workers.

No environment, credentials, task text, or full command lines are emitted.
This is an observation, never authorization to mutate a legacy plugin scope.
"""
import argparse
import datetime
import json
import os
from pathlib import Path


def inspect(expected_host_pid=None):
    own = os.getuid()
    expected_visible = expected_host_pid is not None and Path(f'/proc/{expected_host_pid}/stat').is_file()
    rows = []
    entry_names = {'bio_agents.py', 'host.js', 'python_service.py', 'plugin-cli.js'}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != own:
                continue
            args = (proc / 'cmdline').read_bytes().split(b'\0')[:6]
            pointers = []
            for arg in args:
                value = os.fsdecode(arg)
                file = Path(value)
                if file.is_absolute() and file.name in entry_names and file.is_file():
                    pointers.append(str(file.resolve()))
            if not pointers:
                continue
            stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            rows.append({'pid': int(proc.name), 'ppid': int(stat[1]), 'startedTicks': stat[19],
                         'programs': pointers, 'interpreter': os.readlink(proc / 'exe'),
                         'cwd': os.readlink(proc / 'cwd')})
        except (OSError, ValueError, IndexError):
            continue  # exited while reading; never infer all owners have exited
    return {'schema': 'BioMindVisibleCoreProcessesV1',
            'atUTC': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'node': os.uname().nodename, 'uid': own, 'pidNamespace': os.readlink('/proc/self/ns/pid'),
            'expectedHostPidVisible': expected_visible, 'processes': rows,
            'scope': 'Only processes visible from this PID namespace on this node',
            'legacyMutationAuthorized': False,
            'limitations': ['Does not inspect other nodes, queued Slurm jobs, or prove all legacy launchers are frozen.',
                            'No visible PID is not proof of no active worker; consult the actual host and registry.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-host-pid', type=int)
    args = parser.parse_args()
    print(json.dumps(inspect(args.expected_host_pid), ensure_ascii=False, indent=2))

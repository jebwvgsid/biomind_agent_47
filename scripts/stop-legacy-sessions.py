#!/usr/bin/env python3
"""Stop explicitly reviewed old BioMind owners; never sweep a user/process group.

The stop plan is separate from plugin adoption approval. PID/start/exe/argv are
checked against an operator snapshot, then pinned with pidfd before any signal.
No transcript, credential, writer claim, package or launcher is modified here.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import time
import uuid


class Refused(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size <= 1048576, 'STOP_PRIVATE_PLAN_REQUIRED')
        return json.load(stream)


def receipt(path, value):
    target = Path(path)
    temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def host_namespace():
    return Path('/proc/1/comm').read_text().strip() in ('systemd', 'init')


def snapshot(pid):
    require(type(pid) is int and pid > 1 and pid != os.getpid(), 'STOP_INVALID_PID')
    directory = Path('/proc') / str(pid)
    require(directory.stat().st_uid == os.getuid(), 'STOP_PROCESS_OWNER_CHANGED')
    fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ProcessLookupError()
    command = (directory / 'cmdline').read_bytes()
    return {'pid': pid, 'startTicks': fields[19], 'ppid': int(fields[1]),
            'executable': os.readlink(directory / 'exe'),
            'commandSha256': hashlib.sha256(command).hexdigest(),
            'argv': [os.fsdecode(x) for x in command.split(b'\0') if x],
            'cwd': os.readlink(directory / 'cwd')}


def identity_matches(actual, expected):
    return all(actual[k] == expected[k] for k in ('pid', 'startTicks', 'executable', 'commandSha256'))


def program(row):
    args = row['argv'][1:]
    while args and args[0] in ('-B', '-u'):
        args = args[1:]
    if not args or args[0].startswith('-'):
        return None
    return str((Path(row['cwd']) / args[0]).resolve())


def role(row, plan):
    """Match the executed script position, never a prompt/argument substring."""
    script = program(row)
    exe = Path(row['executable']).name
    python = exe.startswith('python')
    node = exe == 'node'
    if python and script == plan['serviceManager']:
        return 'service'
    if node and script == plan['serviceLauncher']:
        return 'backend'
    if python and script == plan['pipeLauncher']:
        return 'relay'
    for root in plan['legacyMainRoots']:
        if node and script == str(Path(root) / 'integrations/pi_runtime/dist/host.js'):
            return 'main'
        if python and script == str(Path(root) / 'bio_agents.py'):
            return 'main'
        if python and script == str(Path(root) / 'integrations/pi_runtime/python_service.py'):
            return 'bridge'
    return None


def verify_service(plan, row=None):
    record = private_json(plan['serviceRecord'])
    expected = plan['serviceIdentity']
    require(all(record.get(k) == v for k, v in expected.items()), 'STOP_SERVICE_INSTANCE_CHANGED')
    if row is not None:
        require(row['pid'] == record['pid'] and row['startTicks'] == record['startTicks'],
                'STOP_SERVICE_OWNER_CHANGED')
        args = row['argv'][1:]
        require('run' in args and '--config' in args and '--instance' in args
                and args[args.index('--config') + 1] == plan['serviceConfig']
                and args[args.index('--instance') + 1] == record['instanceId'], 'STOP_SERVICE_COMMAND_CHANGED')
    return record


def gone(handle):
    return bool(select.select([handle['fd']], [], [], 0)[0])


def send(handle, signum, result):
    if gone(handle):
        return
    try:
        # pidfd prevents recycling races; recheck also rejects exec/role changes.
        require(identity_matches(snapshot(handle['row']['pid']), handle['row']), 'STOP_IDENTITY_CHANGED_BEFORE_SIGNAL')
        signal.pidfd_send_signal(handle['fd'], signum)
        result['signals'].append({'pid': handle['row']['pid'], 'role': handle['role'], 'signal': signum.name})
    except (ProcessLookupError, FileNotFoundError):
        return


def wait_for(handles, seconds):
    deadline = time.monotonic() + seconds
    while any(not gone(h) for h in handles) and time.monotonic() < deadline:
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def stop(plan, apply=False, service_wait=25, owner_wait=15):
    result = {'schema': 'BioMindLegacySessionStopV1', 'status': 'BLOCKED', 'signals': [],
              'targets': [], 'skipped': [], 'blockers': [], 'historyDeleted': False,
              'writerClaimsChanged': False, 'schedulerJobsCancelled': False,
              'entrypointsChanged': False, 'actualAdoption': False, 'formalSwitch': False}
    handles = []
    try:
        require(plan.get('schema') == 'BioMindLegacySessionStopPlanV1', 'STOP_INVALID_PLAN')
        require(plan.get('stopOldConversationsAuthorized') is True, 'STOP_AUTHORIZATION_REQUIRED')
        require(plan.get('host') == socket.gethostname() and plan.get('uid') == os.getuid(), 'STOP_HOST_OR_UID_MISMATCH')
        require(host_namespace(), 'STOP_REAL_HOST_NAMESPACE_REQUIRED')
        require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'STOP_PIDFD_REQUIRED')
        require(len(plan['targets']) <= 64 and len({x['pid'] for x in plan['targets']}) == len(plan['targets']), 'STOP_INVALID_TARGETS')
        verify_service(plan)
        for expected in plan['targets']:
            try:
                row = snapshot(expected['pid'])
                require(identity_matches(row, expected), 'STOP_REVIEWED_IDENTITY_CHANGED')
                kind = role(row, plan)
                if kind is None:
                    result['skipped'].append({'pid': row['pid'], 'reason': 'NOT_A_CONFIRMED_BIOMIND_OWNER'})
                    continue
                if kind == 'service':
                    verify_service(plan, row)
                fd = os.pidfd_open(row['pid'])
                handle = {'fd': fd, 'row': row, 'role': kind}
                handles.append(handle)
                require(identity_matches(snapshot(row['pid']), expected), 'STOP_IDENTITY_CHANGED_WHILE_PINNING')
                result['targets'].append({'pid': row['pid'], 'startTicks': row['startTicks'], 'role': kind})
            except (ProcessLookupError, FileNotFoundError):
                result['skipped'].append({'pid': expected['pid'], 'reason': 'ALREADY_EXITED'})
            except (Refused, OSError) as error:
                result['blockers'].append({'pid': expected['pid'], 'code': str(error) if isinstance(error, Refused) else 'STOP_PROCESS_UNREADABLE'})
        # Complete target validation before sending the first signal.
        require(not result['blockers'], 'STOP_TARGET_VALIDATION_FAILED')
        if not apply:
            result['status'] = 'DRY_RUN'
            return result
        verify_service(plan)
        # Stop listener first. Its normal shutdown sends the native shutdown
        # frame to server-owned sessions, allowing their history/lease flush.
        services = [h for h in handles if h['role'] == 'service']
        backends = [h for h in handles if h['role'] == 'backend']
        for h in services:
            send(h, signal.SIGTERM, result)
        wait_for(services + backends, service_wait if services else 0)
        for h in backends:
            send(h, signal.SIGTERM, result)
        wait_for(backends, owner_wait if not services else 2)
        owners = [h for h in handles if h['role'] not in ('service', 'backend')]
        for h in owners:
            send(h, signal.SIGTERM, result)
        wait_for(handles, owner_wait)
        if plan.get('forceAfterGrace') is True:
            for h in handles:
                send(h, signal.SIGKILL, result)
            wait_for(handles, 3)
        result['remaining'] = [{'pid': h['row']['pid'], 'role': h['role']} for h in handles if not gone(h)]
        require(not result['remaining'], 'STOP_TARGETS_STILL_RUNNING')
        verify_service(plan)  # detect a desktop auto-starting a replacement
        result['status'] = 'STOPPED_REVIEWED_TARGETS'
        # Scope is deliberately narrower than a full admission/host audit.
        result['maintenanceReady'] = False
        result['unreviewedProcessesRequireAudit'] = True
    except (Refused, OSError, ValueError, KeyError, IndexError) as error:
        result['code'] = str(error) if isinstance(error, Refused) else 'STOP_CHECK_FAILED'
        result['status'] = 'PARTIAL' if result['signals'] else 'BLOCKED'
    finally:
        for h in handles:
            with contextlib.suppress(OSError):
                os.close(h['fd'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--receipt')
    args = parser.parse_args()
    try:
        if args.receipt:
            # A lost SSH reply leaves an explicit UNKNOWN record, never PASS.
            receipt(args.receipt, {'schema': 'BioMindLegacySessionStopV1', 'status': 'IN_PROGRESS',
                                   'outcome': 'UNKNOWN_UNTIL_FINAL_RECEIPT', 'host': socket.gethostname(),
                                   'startedAt': time.time()})
        result = stop(private_json(args.plan), apply=args.apply)
    except (Refused, OSError, ValueError):
        result = {'status': 'BLOCKED', 'code': 'STOP_PLAN_UNAVAILABLE', 'signals': []}
    if args.receipt:
        try:
            receipt(args.receipt, {**result, 'host': socket.gethostname(), 'at': time.time()})
        except OSError:
            result['receiptError'] = 'STOP_RECEIPT_SAVE_FAILED'
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] in ('STOPPED_REVIEWED_TARGETS', 'DRY_RUN') else 2


if __name__ == '__main__':
    raise SystemExit(main())

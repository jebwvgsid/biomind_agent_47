"""Exact operator-reviewed OS helpers; never a name/EACCES or subtree exemption.

This only supplies identity evidence to the existing one-time adoption audit.
It does not approve records, mutate plans, stop processes or waive visible refs.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess

SCHEMA = 'BioMindSystemHelperReviewV1'
ROLES = ('ssh-login-helper', 'pam-session-helper', 'keyring-helper')
LIMIT = 2097152
IPC_LOCK = 1 << 14


class HelperReviewError(ValueError):
    pass


def fail(code):
    raise HelperReviewError(code)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def read(path):
    with Path(path).open('rb') as stream:
        value = stream.read(LIMIT + 1)
    if len(value) > LIMIT: fail('SYSTEM_HELPER_READ_LIMIT')
    return value


def role(command):
    first = os.fsdecode(command.split(b'\0', 1)[0])
    if first.startswith('sshd: '): return 'ssh-login-helper'
    return {'sshd': 'ssh-login-helper', '(sd-pam)': 'pam-session-helper',
            'systemd': 'system-service-manager', 'gnome-keyring-d': 'keyring-helper',
            'gnome-keyring-daemon': 'keyring-helper'}.get(Path(first).name, 'UNKNOWN')


def process(proc, pid):
    directory = proc / str(pid)
    info = read(directory / 'stat').decode().rsplit(')', 1)[1].split()
    values = dict(line.split(':', 1) for line in read(directory / 'status').decode().splitlines() if ':' in line)
    credentials = {}
    for key in ('Uid', 'Gid'):
        fields = values[key].split()
        if len(fields) != 4 or not all(value.isdecimal() for value in fields): fail('SYSTEM_HELPER_INVALID_CREDENTIALS')
        credentials[key] = list(map(int, fields))
    for key in ('CapPrm', 'CapEff', 'CapInh', 'CapAmb'):
        value = values.get(key, '').strip()
        if not re.fullmatch(r'[0-9a-fA-F]{1,16}', value): fail('SYSTEM_HELPER_INVALID_CAPABILITIES')
        credentials[key] = int(value, 16)
    credentials['NoNewPrivs'] = int(values['NoNewPrivs'])
    mapping = [list(map(int, line.split())) for line in read(directory / 'uid_map').decode().splitlines()]
    if mapping != [[0, 0, 4294967295]]: fail('SYSTEM_HELPER_INITIAL_UID_NAMESPACE_REQUIRED')
    groups = read(directory / 'cgroup')
    # Paths are kept in memory only to compare actual unit membership.
    group_paths = [line.split(':', 2)[2] for line in groups.decode().splitlines()]
    command = read(directory / 'cmdline')
    return {'pid': pid, 'ppid': int(info[1]), 'startTicks': info[19],
            'forkNoExec': bool(int(info[6]) & 0x40), 'commandSha256': sha(command),
            'roleHint': role(command), 'credentials': credentials, 'uidMap': mapping,
            'cgroupSha256': sha(groups)}, group_paths


def trusted_file(filename):
    path = Path(filename)
    if not path.is_absolute(): fail('SYSTEM_HELPER_TRUSTED_ABSOLUTE_PATH_REQUIRED')
    resolved = path.resolve(strict=True)
    for member in set((path, *path.parents, resolved, *resolved.parents)):
        info = member.stat()
        if info.st_uid != 0 or info.st_mode & 0o022 or os.access(member, os.W_OK):
            fail('SYSTEM_HELPER_SYSTEM_FILE_WRITABLE')
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode): fail('SYSTEM_HELPER_SYSTEM_FILE_INVALID')
    digest = hashlib.sha256()
    if info.st_size > 67108864: fail('SYSTEM_HELPER_SYSTEM_FILE_LIMIT')
    with resolved.open('rb') as stream:
        size = 0
        for block in iter(lambda: stream.read(1048576), b''):
            size += len(block)
            if size > 67108864: fail('SYSTEM_HELPER_SYSTEM_FILE_LIMIT')
            digest.update(block)
    return {'path': str(path), 'resolvedPath': str(resolved), 'device': info.st_dev,
            'inode': info.st_ino, 'sha256': digest.hexdigest()}


def command_output(executable, arguments):
    trusted_file(executable)
    value = subprocess.run([executable, *arguments], capture_output=True, timeout=10)
    if value.returncode or len(value.stdout) > LIMIT: fail('SYSTEM_HELPER_OS_QUERY_FAILED')
    return value.stdout.decode()


def unit(name, *, user=False):
    if not re.fullmatch(r'[A-Za-z0-9_.:@-]{1,192}\.service', name): fail('SYSTEM_HELPER_UNIT_INVALID')
    keys = 'MainPID,ControlPID,FragmentPath,ExecStart,ControlGroup,LoadState,ActiveState,DropInPaths,PAMName,Transient'
    output = command_output('/usr/bin/systemctl', (['--user'] if user else []) +
                            ['show', '--no-pager', name, '--property=' + keys])
    values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if values.get('LoadState') != 'loaded' or values.get('ActiveState') != 'active': fail('SYSTEM_HELPER_UNIT_NOT_ACTIVE')
    executable = re.search(r'(?:^|[;{]\s*)path=([^ ;}]+)', values.get('ExecStart', ''))
    if not executable: fail('SYSTEM_HELPER_UNIT_EXECUTABLE_UNKNOWN')
    path = executable.group(1)
    result = {'name': name, 'userManager': user, 'mainPid': int(values['MainPID']),
              'controlPid': int(values.get('ControlPID', '0')), 'controlGroup': values['ControlGroup'],
              'executable': trusted_file(path), 'pamName': values.get('PAMName', '')}
    fragments = [values.get('FragmentPath', ''), *values.get('DropInPaths', '').split()]
    if user:
        # Standard D-Bus transient units are owned by the user; that alone is
        # not executable provenance. A live bus owner and file capabilities
        # are independently required below for this one narrowly allowed case.
        if values.get('Transient') != 'yes': fail('SYSTEM_HELPER_TRANSIENT_UNIT_REQUIRED')
        result['unitFileFingerprints'] = []
        for filename in fragments:
            p = Path(filename)
            if (not p.is_absolute() or p.is_symlink() or not p.is_file()
                    or p.parent != Path(f'/run/user/{os.getuid()}/systemd/transient')
                    or p.name != name or p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o022):
                fail('SYSTEM_HELPER_TRANSIENT_UNIT_INVALID')
            result['unitFileFingerprints'].append({'path': filename, 'sha256': sha(read(p))})
    else:
        result['unitFileFingerprints'] = [trusted_file(filename) for filename in fragments]
    return result


def bus_owner():
    prefix = ['--user', 'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus']
    value = command_output('/usr/bin/busctl', [*prefix, 'GetNameOwner', 's', 'org.freedesktop.secrets'])
    match = re.fullmatch(r's "(:[0-9]+\.[0-9]+)"\s*', value)
    if not match: fail('SYSTEM_HELPER_SECRET_SERVICE_OWNER_UNKNOWN')
    owner = match.group(1)
    value = command_output('/usr/bin/busctl', [*prefix, 'GetConnectionUnixProcessID', 's', owner])
    match = re.fullmatch(r'u ([0-9]+)\s*', value)
    if not match: fail('SYSTEM_HELPER_SECRET_SERVICE_PID_UNKNOWN')
    return {'name': 'org.freedesktop.secrets', 'owner': owner, 'pid': int(match.group(1))}


def file_capabilities(path):
    data = os.getxattr(path, 'security.capability')
    if len(data) not in (20, 24): fail('SYSTEM_HELPER_FILE_CAPABILITIES_INVALID')
    values = struct.unpack('<' + 'I' * (len(data) // 4), data)
    revision = values[0] & 0xff000000
    if (values[0] not in (0x02000001, 0x03000001) or (revision == 0x02000000 and len(data) != 20)
            or values[1:5] != (IPC_LOCK, 0, 0, 0)):
        fail('SYSTEM_HELPER_FILE_CAPABILITIES_INVALID')
    if revision == 0x03000000 and (len(values) != 6 or values[5] != 0): fail('SYSTEM_HELPER_FILE_CAPABILITIES_INVALID')
    return {'sha256': sha(data), 'permitted': IPC_LOCK, 'effective': True, 'inheritable': 0}


def unit_contains(control_group, process_groups):
    return any(path == control_group or path.startswith(control_group.rstrip('/') + '/') for path in process_groups)


def snapshot(pid, kind, host, uid, proc=Path('/proc'), keyring_unit=None):
    if kind not in ROLES: fail('SYSTEM_HELPER_ROLE_UNSUPPORTED')
    boot = read(proc / 'sys/kernel/random/boot_id').decode().strip()
    current, groups = process(proc, pid)
    if current['roleHint'] != kind or current['credentials']['Uid'] != [uid] * 4:
        fail('SYSTEM_HELPER_ROLE_OR_UID_MISMATCH')
    if current['credentials']['CapInh'] or current['credentials']['CapAmb']:
        fail('SYSTEM_HELPER_UNEXPECTED_CAPABILITIES')
    parent, parent_groups = process(proc, current['ppid'])
    parents = [parent]
    if kind == 'ssh-login-helper':
        if parent['ppid'] <= 1: fail('SYSTEM_HELPER_SSH_PROVENANCE_MISMATCH')
        master, master_groups = process(proc, parent['ppid'])
        if master['ppid'] != 1: fail('SYSTEM_HELPER_SSH_PROVENANCE_MISMATCH')
        init, _ = process(proc, master['ppid'])
        parents += [master, init]
        service = unit('sshd.service')
        if (not current['forkNoExec'] or current['credentials']['CapPrm'] or current['credentials']['CapEff']
                or parent['roleHint'] != kind or master['roleHint'] != kind
                or parent['credentials']['Uid'] != [0] * 4 or master['credentials']['Uid'] != [0] * 4
                or init['pid'] != 1 or init['credentials']['Uid'] != [0] * 4
                or service['mainPid'] != master['pid'] or not unit_contains(service['controlGroup'], master_groups)
                or service['executable']['resolvedPath'] != '/usr/sbin/sshd'):
            fail('SYSTEM_HELPER_SSH_PROVENANCE_MISMATCH')
    else:
        if parent['ppid'] != 1: fail('SYSTEM_HELPER_SYSTEMD_PROVENANCE_MISMATCH')
        init, _ = process(proc, parent['ppid'])
        parents.append(init)
        service = unit(f'user@{uid}.service')
        actual_parent_executable = os.readlink(proc / str(parent['pid']) / 'exe')
        if (parent['roleHint'] != 'system-service-manager' or parent['credentials']['Uid'] != [uid] * 4
                or init['pid'] != 1 or init['credentials']['Uid'] != [0] * 4
                or service['mainPid'] != parent['pid'] or not unit_contains(service['controlGroup'], parent_groups)
                or actual_parent_executable != service['executable']['path']
                or service['executable']['resolvedPath'] not in ('/usr/lib/systemd/systemd', '/lib/systemd/systemd')):
            fail('SYSTEM_HELPER_SYSTEMD_PROVENANCE_MISMATCH')
        if kind == 'pam-session-helper' and (not current['forkNoExec'] or service['pamName'] != 'systemd-user'
                or current['cgroupSha256'] != parent['cgroupSha256']
                or current['credentials']['CapPrm'] or current['credentials']['CapEff']):
            fail('SYSTEM_HELPER_PAM_PROVENANCE_MISMATCH')
    result = {'host': host, 'bootId': boot, 'uid': uid, 'role': kind,
              'process': current, 'parents': parents, 'systemUnit': service}
    if kind == 'keyring-helper':
        if not isinstance(keyring_unit, str) or not re.fullmatch(r'dbus-:[0-9]+\.[0-9]+-org\.freedesktop\.secrets@[0-9]+\.service', keyring_unit):
            fail('SYSTEM_HELPER_KEYRING_UNIT_REQUIRED')
        live_owner = bus_owner()
        dedicated = unit(keyring_unit, user=True)
        executable = dedicated['executable']['resolvedPath']
        if (live_owner['pid'] != pid or dedicated['mainPid'] != pid or executable != '/usr/bin/gnome-keyring-daemon'
                or current['credentials']['CapPrm'] != IPC_LOCK or current['credentials']['CapEff'] != IPC_LOCK):
            fail('SYSTEM_HELPER_KEYRING_PROVENANCE_MISMATCH')
        result.update(secretService=live_owner, keyringUnit=dedicated, fileCapabilities=file_capabilities(executable))
    # Every process in the reviewed chain must retain its exact identity during capture.
    for item in [current, *parents]:
        latest, _ = process(proc, item['pid'])
        if latest != item: fail('SYSTEM_HELPER_IDENTITY_CHANGED')
    if read(proc / 'sys/kernel/random/boot_id').decode().strip() != boot: fail('SYSTEM_HELPER_BOOT_CHANGED')
    return result


def reviewed_snapshot(review, host, uid, proc=Path('/proc')):
    if (not isinstance(review, dict) or review.get('schema') != SCHEMA or review.get('operatorReviewed') is not True
            or review.get('host') != host or type(review.get('pid')) is not int or review['pid'] <= 1
            or review.get('role') not in ROLES or not re.fullmatch(r'[0-9a-f]{64}', review.get('identitySha256', ''))):
        fail('SYSTEM_HELPER_REVIEW_INVALID')
    evidence = review.get('operatorEvidence')
    if not isinstance(evidence, list) or not evidence or len(evidence) > 8: fail('SYSTEM_HELPER_OPERATOR_EVIDENCE_REQUIRED')
    for item in evidence:
        path = Path(item['path'])
        if (not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid()
                or path.stat().st_mode & 0o022 or sha(read(path)) != item['sha256']):
            fail('SYSTEM_HELPER_OPERATOR_EVIDENCE_CHANGED')
    result = snapshot(review['pid'], review['role'], host, uid, proc, review.get('keyringUnit'))
    if result['bootId'] != review.get('bootId') or fingerprint(result) != review['identitySha256']:
        fail('SYSTEM_HELPER_REVIEW_IDENTITY_MISMATCH')
    return result


def visible_references(pid, proc=Path('/proc')):
    """cwd has already failed once. Inspect other available fields once only."""
    directory = proc / str(pid)
    refs, unreadable = [], ['cwd']
    try:
        for fd in (directory / 'fd').iterdir():
            try: refs.append(('fd', os.readlink(fd)))
            except FileNotFoundError: pass
            except PermissionError: unreadable.append('fd-link')
    except PermissionError: unreadable.append('fd-directory')
    try:
        for line in read(directory / 'maps').decode().splitlines():
            parts = line.split(None, 5)
            if len(parts) == 6: refs.append(('maps', parts[5].removesuffix(' (deleted)')))
    except PermissionError: unreadable.append('maps')
    try:
        for item in read(directory / 'environ').split(b'\0'):
            _, sep, value = item.partition(b'=')
            if sep: refs.append(('environment', value.decode(errors='replace')))
    except PermissionError: unreadable.append('environ')
    return refs, sorted(set(unreadable))

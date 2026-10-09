"""Linux resource leases. No package catalog, credentials, or process killing.

flock follows the inherited open file description, not the short helper PID.
Callers close their descriptors; they must NEVER explicitly LOCK_UN a lease
which may have been inherited by a still-running native/domain child.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

SCHEMA = 'BioMindResourceGuardV1'


class GuardError(RuntimeError):
    pass


def canonical(value):
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise GuardError('RESOURCE_ROOT_ABSOLUTE_REQUIRED')
    return p.resolve()


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GuardError('RESOURCE_LOCK_DIRECTORY_UNSAFE')


def paths_for(root):
    root = canonical(root)
    key = hashlib.sha256(os.fsencode(root)).hexdigest()
    directory = root.parent / '.biomind-resource-locks'
    return {'root': str(root), 'lock': str(directory / (key + '.lock')),
            'gate': str(directory / (key + '.gate')),
            'repair': str(directory / (key + '.repair.json'))}


def plan(request):
    agent = canonical(request['agentDir'])
    cwd = canonical(request['cwd'])
    selected = request.get('scopes', ['user', 'project'])
    if not isinstance(selected, list) or any(s not in ('user', 'project') for s in selected):
        raise GuardError('INVALID_RESOURCE_SCOPE')
    scopes, resources = [], {}
    for scope in sorted(set(selected)):
        root = agent if scope == 'user' else canonical(cwd / '.pi')
        mark = root / '.biomind-resource-guard.json'
        trusted = False
        if mark.exists():
            info = mark.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
                try:
                    value = json.loads(mark.read_text())
                    trusted = value == {'schema': SCHEMA, 'root': str(root), 'origin': 'born-guarded'}
                except (OSError, ValueError):
                    pass
        row = {**paths_for(root), 'scope': scope, 'marker': str(mark),
               'legacyUnverified': root.exists() and not trusted, 'fresh': not root.exists()}
        scopes.append(row)
        names = ('npm', 'git', 'tmp/extensions') if scope == 'user' else ('npm', 'git')
        for name in names:
            p = paths_for(root / name)
            p['scope'] = scope
            resources[p['root']] = p
    return {'schema': SCHEMA, 'agentDir': str(agent), 'cwd': str(cwd), 'scopes': scopes,
            'resources': sorted(resources.values(), key=lambda r: r['root'])}


def prepare(request):
    result = plan(request)
    # Creating only coordination directories does not initialize or authorize an
    # existing scope. A new scope's marker is written only while its gate is held.
    for item in (result['resources'] if request.get('resources') else result['scopes']):
        private_dir(Path(item['lock']).parent)
    return result


def validate_fd(fd, filename, mode=None):
    fd = int(fd)
    actual = os.fstat(fd)
    expected = os.stat(filename, follow_symlinks=False)
    if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid()
            or actual.st_mode & 0o077 or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
        raise GuardError('RESOURCE_LEASE_FD_INVALID')
    if mode:
        # fdinfo is kernel evidence; an environment variable or PID file is not.
        try:
            rows = Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines()
        except OSError:
            raise GuardError('RESOURCE_LEASE_PROOF_UNAVAILABLE') from None
        wanted = 'WRITE' if mode == 'exclusive' else 'READ'
        if not any(r.startswith('lock:') and 'FLOCK' in r and wanted in r for r in rows):
            raise GuardError('RESOURCE_LEASE_NOT_HELD')


def lock_fds(request):
    rows = request['files']
    # Validate every descriptor before taking any locks. Parent owns all open
    # descriptions and closes all of them on partial acquisition failure.
    for row in rows:
        validate_fd(row['fd'], row['path'])
    for row in rows:
        mode = fcntl.LOCK_EX if row['mode'] == 'exclusive' else fcntl.LOCK_SH
        try:
            fcntl.flock(int(row['fd']), mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError('BUSY') from None
    return {'status': 'locked'}


def initialize(request):
    result = plan(request)
    gates = request['gates']
    for item in result['scopes']:
        row = next((r for r in gates if r['path'] == item['gate']), None)
        if not row:
            raise GuardError('RESOURCE_ADMISSION_GATE_REQUIRED')
        validate_fd(row['fd'], row['path'], 'exclusive')
        root = Path(item['root'])
        if not root.exists():
            root.mkdir(mode=0o700)
            value = {'schema': SCHEMA, 'root': str(root), 'origin': 'born-guarded'}
            fd = os.open(item['marker'], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
    return plan(request)


def adopt_inherited(environment=None):
    """Validate and retain inherited resource fds until this process exits."""
    text = (environment or os.environ).get('BIOMIND_PI_RESOURCE_FDS')
    if not text:
        return []  # old workers remain detectable as legacy/uncoordinated
    try:
        rows = json.loads(text)
        if not isinstance(rows, list) or not rows:
            raise ValueError()
        for row in rows:
            validate_fd(row['fd'], row['path'], row['mode'])
            os.set_inheritable(row['fd'], True)
        return [row['fd'] for row in rows]
    except (KeyError, ValueError, TypeError, OSError):
        raise GuardError('RESOURCE_LEASE_FD_INVALID') from None


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(131073))
        action = request.pop('action')
        if action == 'plan':
            result = plan(request)
        elif action == 'prepare':
            result = prepare(request)
        elif action == 'lock':
            result = lock_fds(request)
        elif action == 'initialize':
            result = initialize(request)
        elif action == 'verify':
            for row in request['files']:
                validate_fd(row['fd'], row['path'], row['mode'])
            result = {'status': 'verified'}
        else:
            raise GuardError('INVALID_RESOURCE_GUARD_ACTION')
        print(json.dumps(result))
        return 0
    except (GuardError, OSError, ValueError, KeyError, TypeError) as error:
        code = str(error) if isinstance(error, GuardError) else 'RESOURCE_GUARD_FAILED'
        print(json.dumps({'status': 'error', 'code': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

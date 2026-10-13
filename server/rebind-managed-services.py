#!/usr/bin/env python3
"""Rebind one explicitly stopped managed service without reading credentials.

Check-only by default. Never ensures, starts, stops or signals a service. Existing
credential files, sessions, registry and preferences remain at their old paths.
Only an operator-selected fixed release/backend and a new runtime file are used.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


CONFIG_FIELDS = {
    'schema', 'serviceId', 'serverId', 'mode', 'stateRoot', 'cwd', 'command',
    'startupTimeoutSeconds', 'environmentFile', 'environment', 'slurm',
}
ENV_FIELDS = {
    'BIOMIND_STUDIO_ROOT', 'NODE_ENV', 'PI_WEB_DIST_DIR', 'BIOMIND_LAUNCHER_PYTHON',
    'BIOMIND_ENTRYPOINT', 'BIOMIND_PIPE_LAUNCHER', 'BIOMIND_RUNTIME_CONFIG',
    'BIOMIND_PI_NODE', 'BIOMIND_PI_PYTHON', 'BIOMIND_PROJECT_ROOTS_JSON',
    'BIOMIND_DATA_ROOTS_JSON', 'PI_WEB_UPLOADS_DIR', 'BIOMIND_HISTORY_NODE',
    'BIOMIND_HISTORY_QUERY_SCRIPT', 'BIOMIND_SESSION_ROOT',
    'BIOMIND_INSTRUCTIONS_QUERY_SCRIPT', 'BIOMIND_MAX_OWNERS',
    'BIOMIND_PI_MODEL_RECORD', 'BIOMIND_DISABLE_DOTENV', 'PYTHONDONTWRITEBYTECODE',
    'PI_CODING_AGENT_DIR', 'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
    'XDG_STATE_HOME', 'PI_WEB_ACCESS_CACHE_ROOT', 'NEXT_TELEMETRY_DISABLED',
}
RUNTIME_FIELDS = {
    'python', 'agentDir', 'dataRoot', 'installRoot', 'provider', 'model',
    'thinkingLevel', 'registryPath', 'preferencesPath', 'memoryRoot', 'wikiRoot',
    'historyEnabled', 'archivePath', 'memoryMode', 'sessionMode', 'session',
    'continueSession', 'sessionDir', 'handshakeTimeoutMs', 'executionProfile',
}
CORE_FILES = (
    'bio_agents.py', 'agent_core/pi_entry.py',
    'integrations/pi_runtime/dist/host.js',
    'integrations/pi_runtime/dist/history-query.js',
    'integrations/pi_runtime/dist/instructions-query.js',
    'integrations/pi_runtime/dist/extensions/biomind.js',
    'integrations/pi_runtime/dist/biomind-extension.ts',
    'integrations/pi_runtime/testing/posix-pipe-relay.py',
)
BACKEND_FILES = (
    'package.json', 'lib/biomind-session-adapter.ts', '.next-pkg/BUILD_ID',
    '.next-pkg/server/app/api/agent/profile/route.js',
    '.next-pkg/server/app/api/agent/new/route.js',
    '.next-pkg/server/app/api/agent/[id]/route.js',
    '.next-pkg/server/app/api/agent/[id]/events/route.js',
)
SECRET_KEY = re.compile(r'(?i)(password|secret|token|credential|authorization|api[_-]?key)')


class Refused(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def absolute(value, *, exists=True, regular=False, directory=False, private=False):
    require(isinstance(value, (str, Path)), 'ABSOLUTE_PATH_REQUIRED')
    p = Path(value)
    require(p.is_absolute() and not any(c in str(p) for c in '\n\r\x00'), 'ABSOLUTE_PATH_REQUIRED')
    require(p.absolute() == p.resolve(strict=False), 'SYMLINK_OR_NONCANONICAL_PATH_REFUSED')
    if exists:
        s = p.stat()
        require(s.st_uid == os.getuid(), 'PATH_OWNER_MISMATCH')
        if regular:
            require(stat.S_ISREG(s.st_mode), 'REGULAR_FILE_REQUIRED')
        if directory:
            require(stat.S_ISDIR(s.st_mode), 'DIRECTORY_REQUIRED')
        require(not s.st_mode & 0o022, 'WRITABLE_BY_OTHERS_REFUSED')
        if private:
            require(not s.st_mode & 0o077, 'PRIVATE_FILE_REQUIRED')
    return p


def snapshot(path, *, private=False):
    require(isinstance(path, (str, Path)), 'NONSECRET_JSON_PATH_REQUIRED')
    candidate = Path(path)
    # Only nonsecret JSON metadata is ever decoded. environmentFile is handled
    # separately with stat calls, even if a configuration points runtime at it.
    require(candidate.suffix == '.json' and not candidate.name.startswith('.')
            and not SECRET_KEY.search(candidate.name)
            and candidate.name not in {'auth.json', 'models.json', 'runtime-environment.json'}
            and not any(part.startswith('.env') for part in candidate.parts),
            'NONSECRET_JSON_PATH_REQUIRED')
    p = absolute(path, regular=True, private=private)
    s = p.stat()
    require(s.st_size <= 2 * 1024 * 1024, 'JSON_TOO_LARGE')
    raw = p.read_bytes()
    after = p.stat()
    require((s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'CONCURRENT_FILE_CHANGE')
    obj = json.loads(raw)
    require(isinstance(obj, dict), 'JSON_OBJECT_REQUIRED')
    return {'path': p, 'raw': raw, 'value': obj, 'mode': stat.S_IMODE(s.st_mode),
            'identity': (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns), 'sha256': digest(raw)}


def unchanged(snap):
    p = snap['path']
    require(not p.is_symlink() and p.is_file(), 'COMPARE_AND_SWAP_FAILED')
    s = p.stat()
    require((s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) == snap['identity']
            and p.read_bytes() == snap['raw'], 'COMPARE_AND_SWAP_FAILED')


def reject_secrets(value):
    if isinstance(value, dict):
        for k, v in value.items():
            require(isinstance(k, str) and not SECRET_KEY.search(k), 'INLINE_SECRET_FIELD_REFUSED')
            reject_secrets(v)
    elif isinstance(value, list):
        for v in value:
            reject_secrets(v)


def validate_release(root):
    root = absolute(root, directory=True)
    release = snapshot(root / 'RELEASE.json')
    manifest = snapshot(root / 'integrations/pi_runtime/dist/build-manifest.json')
    r, m = release['value'], manifest['value']
    require(r.get('schema') == 'BioMindCoreReleaseV1', 'FIXED_RELEASE_REQUIRED')
    require(isinstance(r.get('releaseId'), str) and re.fullmatch('[0-9a-f]{64}', r['releaseId']), 'RELEASE_ID_REQUIRED')
    require(r['releaseId'] == m.get('buildId') and r.get('manifestSha256') == manifest['sha256'], 'RELEASE_MANIFEST_MISMATCH')
    require(r.get('sourceCommit') == m.get('sourceCommit'), 'RELEASE_SOURCE_MISMATCH')
    expected = {}
    for field, prefix in [('sources', ''), ('artifacts', 'integrations/pi_runtime/')]:
        rows = m.get(field)
        require(isinstance(rows, list), 'RELEASE_MANIFEST_INVALID')
        for row in rows:
            require(isinstance(row, dict) and isinstance(row.get('path'), str), 'RELEASE_MANIFEST_INVALID')
            rel = Path(prefix + row['path'])
            require(not rel.is_absolute() and '..' not in rel.parts, 'RELEASE_PATH_ESCAPE')
            expected[rel.as_posix()] = row.get('sha256')
    checked = []
    for name in CORE_FILES:
        p = absolute(root / name, regular=True)
        require(expected.get(name) == digest(p.read_bytes()), 'RELEASE_ENTRY_HASH_MISMATCH')
        checked.append(name)
    executables = {}
    for kind in ['node', 'python']:
        name = r.get(kind, {}).get('path')
        require(isinstance(name, str) and Path(name).is_absolute(), 'RELEASE_INTERPRETER_REQUIRED')
        p = Path(name)
        require(p.is_file() and os.access(p, os.X_OK), 'RELEASE_INTERPRETER_UNAVAILABLE')
        executables[kind] = str(p)
    return root, release, manifest, executables, checked


def fresh_output(path):
    p = absolute(path, exists=False)
    absolute(p.parent, directory=True)
    require(not os.path.lexists(p), 'OUTPUT_ALREADY_EXISTS')
    return p


def write_new(path, raw, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def atomic_replace(path, raw):
    temporary = path.with_name('.' + path.name + '.rebind-' + uuid.uuid4().hex)
    write_new(temporary, raw)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_create(path, raw):
    temporary = path.with_name('.' + path.name + '.rebind-' + uuid.uuid4().hex)
    write_new(temporary, raw)
    try:
        # link publishes complete bytes and refuses an existing destination.
        # Only this operation's private staging file is then removed.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(args):
    config = snapshot(args.service_config, private=True)
    c = config['value']
    reject_secrets(c)
    require(set(c) <= CONFIG_FIELDS and c.get('schema') == 'BioMindStudioServiceV1', 'SERVICE_CONFIG_SCHEMA_REFUSED')
    require(c.get('mode') in ('local', 'slurm'), 'SERVICE_MODE_REFUSED')
    for key in ['serviceId', 'serverId']:
        require(isinstance(c.get(key), str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', c[key]), 'SERVICE_IDENTITY_REQUIRED')
    env = c.get('environment')
    require(isinstance(env, dict) and set(env) <= ENV_FIELDS and
            all(isinstance(v, str) and '\x00' not in v for v in env.values()), 'ENVIRONMENT_ALLOWLIST_REFUSED')
    # Metadata-only: credentials are never opened, hashed, copied or decoded.
    credentials = absolute(c.get('environmentFile'), regular=True, private=True)
    state = absolute(c.get('stateRoot'), directory=True, private=True)
    require(state == config['path'].parent / 'state', 'CONFIGURATION_LAYOUT_CHANGE_REFUSED')
    require(credentials == config['path'].parent / 'runtime-environment.json', 'CREDENTIAL_PATH_LAYOUT_REFUSED')
    lifecycle = snapshot(state / 'service.json', private=True)
    require(lifecycle['value'].get('status') == 'stopped', 'SERVICE_NOT_STOPPED')
    require(all(lifecycle['value'].get(k) == c[k] for k in ['serviceId', 'serverId']), 'LIFECYCLE_IDENTITY_MISMATCH')
    runtime = snapshot(env.get('BIOMIND_RUNTIME_CONFIG'), private=True)
    reject_secrets(runtime['value'])
    require(set(runtime['value']) <= RUNTIME_FIELDS, 'RUNTIME_ALLOWLIST_REFUSED')
    agent = absolute(runtime['value'].get('agentDir'), directory=True)
    sessions = (absolute(runtime['value']['sessionDir'], directory=True)
                if runtime['value'].get('sessionDir') else absolute(agent / 'sessions', directory=True))
    require(absolute(env.get('BIOMIND_SESSION_ROOT'), directory=True) == sessions, 'SESSION_ROOT_MISMATCH')
    root, release, manifest, exe, checked = validate_release(args.release)
    backend = absolute(args.backend_root, directory=True)
    for name in BACKEND_FILES:
        absolute(backend / name, regular=True)
    adapter = (backend / 'lib/biomind-session-adapter.ts').read_bytes()
    require(b'liveSnapshot()' in adapter and b'displayPendingUI' in adapter, 'BACKEND_ADAPTER_REQUIRED')
    cmd = c.get('command')
    require(isinstance(cmd, list) and len(cmd) == 2 and all(isinstance(x, str) for x in cmd), 'MANAGED_COMMAND_REQUIRED')
    launcher = absolute(cmd[1], regular=True)
    require(launcher.name == 'next-service.mjs', 'MANAGED_LAUNCHER_REQUIRED')
    absolute(launcher.parent / 'server-service.py', regular=True)
    output = fresh_output(args.runtime_output)
    receipt = fresh_output(args.receipt)
    protected = [agent, sessions, root, backend, state]
    for p in [output, receipt]:
        require(not any(p.is_relative_to(q) for q in protected), 'OUTPUT_IN_PROTECTED_ROOT')
        require(p not in [config['path'], runtime['path'], credentials], 'OUTPUT_PATH_OVERLAP')
    require(output != receipt, 'OUTPUT_PATH_OVERLAP')
    require(output.suffix == '.json' and not SECRET_KEY.search(output.name) and not output.name.startswith('.'), 'NONSECRET_RUNTIME_OUTPUT_REQUIRED')
    n = copy.deepcopy(c)
    new_runtime = copy.deepcopy(runtime['value'])
    new_runtime.update(installRoot=str(root), python=exe['python'])
    rt = root / 'integrations/pi_runtime'
    n.update(cwd=str(backend), command=[exe['node'], str(launcher)])
    n['environment'].update({
        'BIOMIND_STUDIO_ROOT': str(backend), 'BIOMIND_ENTRYPOINT': str(root / 'bio_agents.py'),
        'BIOMIND_PIPE_LAUNCHER': str(rt / 'testing/posix-pipe-relay.py'),
        'BIOMIND_RUNTIME_CONFIG': str(output), 'BIOMIND_HISTORY_QUERY_SCRIPT': str(rt / 'dist/history-query.js'),
        'BIOMIND_INSTRUCTIONS_QUERY_SCRIPT': str(rt / 'dist/instructions-query.js'),
        'BIOMIND_PI_NODE': exe['node'], 'BIOMIND_HISTORY_NODE': exe['node'],
        'BIOMIND_PI_PYTHON': exe['python'], 'BIOMIND_LAUNCHER_PYTHON': exe['python'],
    })
    return {'config': config, 'runtime': runtime, 'lifecycle': lifecycle,
            'release': release, 'manifest': manifest, 'state': state, 'credentials': credentials,
            'runtime_output': output, 'receipt': receipt, 'new_config': n,
            'new_runtime': new_runtime, 'root': root, 'backend': backend,
            'checked_entries': checked}


def execute(args):
    plan = prepare(args)
    fds = []
    try:
        for p in [plan['config']['path'].parent / 'configure.lock',
                  plan['state'] / 'service.lock', plan['state'] / 'backend.lock']:
            absolute(p, regular=True, private=True)
            # Do not create a missing lock. Reject instead of modifying a live service.
            fd = os.open(p, os.O_RDWR | os.O_NOFOLLOW)
            fds.append(fd)
            actual, expected = os.fstat(fd), p.stat()
            require((actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino), 'LOCK_IDENTITY_CHANGED')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused('SERVICE_ACTIVE_CONFIGURATION_LOCKED') from None
        for key in ['config', 'runtime', 'lifecycle', 'release', 'manifest']:
            unchanged(plan[key])
        require(plan['lifecycle']['value']['status'] == 'stopped', 'SERVICE_NOT_STOPPED')
        require(not os.path.lexists(plan['runtime_output']) and not os.path.lexists(plan['receipt']), 'OUTPUT_ALREADY_EXISTS')
        result = {
            'schema': 'BioMindManagedServiceRebindV1',
            'status': 'APPLIED' if args.apply else 'CHECK_ONLY_READY',
            'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'serviceConfig': str(plan['config']['path']),
            'serviceId': plan['config']['value']['serviceId'],
            'serverId': plan['config']['value']['serverId'],
            'releaseRoot': str(plan['root']), 'releaseId': plan['release']['value']['releaseId'],
            'runtimeOutput': str(plan['runtime_output']), 'backendRoot': str(plan['backend']),
            'credentialContentsRead': False, 'credentialFileChanged': False,
            'serviceStarted': False, 'signalsSent': False, 'sessionsChanged': False,
            'registryPreferencesChanged': False, 'oldLifecycleStatus': 'stopped',
            'serviceConfigBeforeSha256': plan['config']['sha256'],
            'serviceConfigAfterSha256': digest(encoded(plan['new_config'])),
            'runtimeBeforeSha256': plan['runtime']['sha256'],
            'runtimeAfterSha256': digest(encoded(plan['new_runtime'])),
            'entryHashesChecked': plan['checked_entries'],
            'nextAction': 'Explicit managed ensure only after this APPLIED receipt; then verify new instance and child loaded_build. No restart was performed.',
        }
        if args.apply:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
            backup = plan['config']['path'].parent / ('rebind-backup-' + stamp)
            backup.mkdir(mode=0o700)
            for key in ['config', 'runtime', 'lifecycle']:
                write_new(backup / (key + '.json'), plan[key]['raw'])
            fsync_dir(backup)
            for key in ['config', 'runtime', 'lifecycle', 'release', 'manifest']:
                unchanged(plan[key])
            atomic_create(plan['runtime_output'], encoded(plan['new_runtime']))
            fsync_dir(plan['runtime_output'].parent)
            # Publishing config and retiring its verified stopped record are both
            # under service/backend/configure locks. A crash between them fails
            # closed as configuration-changed; it cannot start a second owner.
            unchanged(plan['config'])
            unchanged(plan['lifecycle'])
            atomic_replace(plan['config']['path'], encoded(plan['new_config']))
            os.replace(plan['lifecycle']['path'], backup / 'retired-lifecycle.json')
            fsync_dir(plan['config']['path'].parent)
            fsync_dir(plan['state'])
            fsync_dir(backup)
            result.update(backupDirectory=str(backup), stoppedLifecycleArchived=True,
                          rollback='With service stopped and the same three locks held, CAS current config against serviceConfigAfterSha256, then restore backup/config.json and backup/lifecycle.json. Preserve new runtime and all user data.')
        write_new(plan['receipt'], encoded(result))
        fsync_dir(plan['receipt'].parent)
        return result
    finally:
        for fd in reversed(fds):
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ['service-config', 'release', 'backend-root', 'runtime-output', 'receipt']:
        parser.add_argument('--' + option, required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except Refused as error:
        print(json.dumps({'status': 'BLOCKED', 'code': str(error), 'serviceStarted': False,
                          'signalsSent': False, 'credentialContentsRead': False}))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Never print config bytes or raw exceptions. If an IO failure occurs
        # during publication, inspect private backups before retrying. No force.
        print(json.dumps({'status': 'BLOCKED', 'code': 'REBIND_IO_OR_METADATA_ERROR',
                          'errorType': type(error).__name__, 'errno': getattr(error, 'errno', None),
                          'outcome': 'CHECK_CONFIG_AND_PRIVATE_BACKUP_BEFORE_RETRY',
                          'serviceStarted': False, 'signalsSent': False, 'credentialContentsRead': False}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

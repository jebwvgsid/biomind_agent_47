"""One-time, maintenance-window admission of existing Pi resource directories.

No package database, session store, process killing, or copying user settings.
Official old entrypoints are fenced before a fresh host/Slurm/writer audit.
New workers are excluded by the existing admission gates and resource flocks.
Directly bypassing a fenced launcher remains outside the supported boundary.
"""
from __future__ import annotations
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from agent_core import pi_plugin_guard as guard

SCHEMA = 'BioMindPluginAdoptionV1'


def fail(code):
    raise guard.GuardError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def save(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())


def absolute(value, final_link=False):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or p == Path('/'):
        fail('MAINTENANCE_ABSOLUTE_PATH_REQUIRED')
    return p.parent.resolve() / p.name if final_link else p.resolve()


def load_plan(filename):
    filename = absolute(filename)
    value = guard.private_json(filename)
    if value.get('schema') != SCHEMA or value.get('scopes') not in (['user'], ['project'], ['user', 'project']):
        fail('INVALID_ADOPTION_PLAN')
    for key in ('agentDir', 'cwd', 'stateDir'):
        value[key] = str(absolute(value[key]))
    if not Path(value['cwd']).is_dir(): fail('INVALID_ADOPTION_PROJECT')
    hosts = value.get('hosts', [])
    if not hosts or len(hosts) > 32 or len({h.get('name') for h in hosts}) != len(hosts):
        fail('ADOPTION_HOST_INVENTORY_REQUIRED')
    for h in hosts:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', h.get('name', '')) or (h.get('ssh') and not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', h['ssh'])):
            fail('INVALID_ADOPTION_HOST')
    if value.get('scheduler') not in ('slurm', 'none'): fail('ADOPTION_SCHEDULER_REQUIRED')
    if not value.get('legacyCodeRoots') or not value.get('entries') or not value.get('sessionRoots'):
        fail('ADOPTION_INVENTORY_REQUIRED')
    for key in ('legacyCodeRoots', 'sessionRoots'):
        value[key] = [str(absolute(p)) for p in value[key]]
    resources = guard.plan(value)
    if any(s['fresh'] for s in resources['scopes']): fail('ADOPTION_REQUIRES_EXISTING_SCOPE')
    protected = [Path(s['root']) for s in resources['scopes']] + [Path(p) for p in value['legacyCodeRoots']]
    state = Path(value['stateDir'])
    if any(state.is_relative_to(p) for p in protected): fail('ADOPTION_STATE_MUST_BE_EXTERNAL')
    entries = []
    for e in value['entries']:
        p = absolute(e['path'], final_link=True)
        if p == filename or p.is_relative_to(state) or state.is_relative_to(p): fail('ADOPTION_PATH_OVERLAP')
        if any(p.is_relative_to(root) for root in protected): fail('FENCE_MUST_NOT_REWRITE_BUSINESS_OR_RESOURCE_ROOT')
        if not re.fullmatch('[0-9a-f]{64}', e.get('expectedSha256', '')): fail('ENTRY_IDENTITY_REQUIRED')
        entries.append({**e, 'path': str(p)})
    if len({e['path'] for e in entries}) != len(entries): fail('DUPLICATE_ENTRY')
    if filename.is_relative_to(state) or any(filename.is_relative_to(p) for p in protected): fail('ADOPTION_PATH_OVERLAP')
    value['entries'] = entries
    value['_path'] = str(filename)
    value['_sha256'] = sha(filename.read_bytes())
    return value


def entry_snapshot(path):
    p = Path(path)
    info = p.lstat()
    if info.st_uid != os.getuid(): fail('ENTRY_OWNER_MISMATCH')
    if p.is_symlink():
        body = {'kind': 'symlink', 'target': os.readlink(p)}
    elif stat.S_ISREG(info.st_mode) and info.st_size <= 65536:
        data = p.read_bytes()
        if not data.startswith(b'#!') and p.suffix != '.py': fail('ENTRY_IS_NOT_A_LAUNCHER')
        body = {'kind': 'file', 'data': base64.b64encode(data).decode(), 'mode': stat.S_IMODE(info.st_mode)}
    else: fail('ENTRY_IS_NOT_A_LAUNCHER')
    return {**body, 'sha256': sha(json.dumps(body, sort_keys=True).encode())}


def fence_bytes(transaction_id):
    return (f'#!{sys.executable}\n# BioMind maintenance fence {transaction_id}\n'
            'import json, sys\nprint(json.dumps({"status":"maintenance","code":"BIOMIND_MAINTENANCE_WINDOW"}), flush=True)\nsys.exit(75)\n').encode()


def replacement(entry, plan):
    """Optional fixed-core CLI redirect. Old server manager stays fenced."""
    target = entry.get('replacement')
    if target is None: return None, []
    root = absolute(target['release'])
    runtime_config = absolute(target['runtimeConfig'])
    if runtime_config.name.startswith('.') or runtime_config.suffix != '.json' or re.search('auth|secret|credential|api.key|models', runtime_config.name, re.I):
        fail('NONSECRET_RUNTIME_CONFIG_REQUIRED')
    config = json.loads(runtime_config.read_text())
    if absolute(config['agentDir']) != Path(plan['agentDir']): fail('REPLACEMENT_AGENT_DIR_MISMATCH')
    release = json.loads((root / 'RELEASE.json').read_text())
    manifest = json.loads((root / 'integrations/pi_runtime/dist/build-manifest.json').read_text())
    if release['releaseId'] != manifest['buildId']: fail('RELEASE_IDENTITY_MISMATCH')
    # Only a release containing this adoption implementation is an accepted target.
    module = root / 'agent_core/pi_plugin_guard.py'
    if b'legacy-adopted' not in module.read_bytes(): fail('REPLACEMENT_CORE_NOT_ADOPTION_AWARE')
    python, node = release['python']['path'], release['node']['path']
    argv = [python, '-B', str(root / 'bio_agents.py')]
    suffix = ['--pi-config', str(runtime_config), '--pi-node', node]
    data = (f'#!{python}\nimport os, sys\nos.execv({python!r}, {argv!r} + sys.argv[1:] + {suffix!r})\n').encode()
    files = [root / 'RELEASE.json', root / 'bio_agents.py', module, runtime_config]
    return data, [{'path': str(p), 'sha256': sha(p.read_bytes())} for p in files]


@contextlib.contextmanager
def state_lock(plan):
    state = Path(plan['stateDir']); guard.private_dir(state)
    fd = os.open(state / 'operation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        guard.lock_fds({'files': [{'fd': fd, 'path': str(state / 'operation.lock'), 'mode': 'exclusive'}]})
        yield state
    finally: os.close(fd)


def transaction(plan, state):
    value = guard.private_json(state / 'transaction.json')
    if value.get('schema') != SCHEMA or value.get('planSha256') != plan['_sha256']:
        fail('ADOPTION_PLAN_CHANGED')
    return value


def verify_fences(value, closed=True):
    for entry in value['entries']:
        p = Path(entry['path'])
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 65536:
            fail('ADOPTION_ENTRY_CHANGED')
        allowed = [entry['closedSha256']] if closed else entry['acceptedSha256']
        if sha(p.read_bytes()) not in allowed: fail('ADOPTION_ENTRY_CHANGED')
        for binding in entry['bindings']:
            p = Path(binding['path'])
            if p.is_symlink() or not p.is_file() or p.stat().st_size > 1048576 or sha(p.read_bytes()) != binding['sha256']:
                fail('ADOPTION_BINDING_CHANGED')


def fence(plan):
    if not all(plan.get(k) is True for k in ('entryInventoryConfirmed', 'hostInventoryConfirmed', 'maintenanceApproved')):
        fail('MAINTENANCE_REVIEW_REQUIRED')
    with state_lock(plan) as state:
        journal = state / 'transaction.json'
        if journal.exists():
            value = transaction(plan, state)
            if value['state'] not in ('fencing', 'fenced'): fail('ADOPTION_STATE_REQUIRES_RECOVERY')
        else:
            identifier = uuid.uuid4().hex
            entries = []
            # Validate and preserve EVERY entry before modifying the first one.
            for entry in plan['entries']:
                original = entry_snapshot(entry['path'])
                if original['sha256'] != entry['expectedSha256']: fail('ADOPTION_ENTRY_CHANGED')
                target, bindings = replacement(entry, plan)
                closed = fence_bytes(identifier)
                entries.append({'path': entry['path'], 'original': original,
                    'closed': base64.b64encode(closed).decode(), 'closedSha256': sha(closed),
                    'replacement': base64.b64encode(target).decode() if target else None,
                    'acceptedSha256': [sha(closed)] + ([sha(target)] if target else []), 'bindings': bindings})
            value = {'schema': SCHEMA, 'id': identifier, 'state': 'fencing', 'planSha256': plan['_sha256'],
                     'roots': [s['root'] for s in guard.plan(plan)['scopes']], 'entries': entries, 'createdAt': time.time()}
            save(journal, value)
        for entry in value['entries']:
            p = Path(entry['path'])
            if not p.is_symlink() and p.is_file() and sha(p.read_bytes()) == entry['closedSha256']: continue
            if entry_snapshot(p)['sha256'] != entry['original']['sha256']: fail('ADOPTION_ENTRY_CHANGED')
            atomic_bytes(p, base64.b64decode(entry['closed']), 0o700)
        value['state'] = 'fenced'; save(journal, value)
        return {'status': 'fenced', 'id': value['id'], 'processesStopped': False, 'resourceFilesChanged': False}


def ancestors():
    values, pid = set(), os.getpid()
    while pid > 0 and pid not in values:
        values.add(pid)
        try: pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError): break
    return values


def audit_local(plan, require_host_namespace=True):
    """Read process/owner metadata only; never print argv, environ or history."""
    host = socket.gethostname()
    declared = next((h for h in plan['hosts'] if h['name'] == host), None)
    if declared is None: fail('ADOPTION_HOST_NOT_DECLARED')
    if require_host_namespace and Path('/proc/1/comm').read_text().strip() not in ('systemd', 'init'):
        fail('ADOPTION_HOST_PID_NAMESPACE_REQUIRED')
    resources = guard.plan(plan)
    roots = [str(p) for p in plan['legacyCodeRoots']] + [s['root'] for s in resources['scopes'] + resources['resources']]
    excluded = ancestors()
    blockers, unknown, unreadable, identities = [], [], [], {}
    allowed = declared.get('reviewedUnrelatedProcesses', [])
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal(): continue
        pid = int(directory.name)
        try:
            if directory.stat().st_uid != os.getuid(): continue
            info = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            if info[0] == 'Z': continue
            ticks = info[19]; identities[str(pid)] = ticks
            if pid in excluded: continue
            cmd = (directory / 'cmdline').read_bytes()
            cwd = os.readlink(directory / 'cwd')
            exe = os.readlink(directory / 'exe')
            references = [cwd] + [part.decode(errors='replace') for part in cmd.split(b'\0')[1:]]
            for fd in (directory / 'fd').iterdir():
                try: references.append(os.readlink(fd))
                except FileNotFoundError: pass
            related = any(any(ref == root or ref.startswith(root + '/') or root + '/' in ref for root in roots) for ref in references)
            pi_process = any(word in cmd for word in (b'bio_agents.py', b'python_service.py', b'pi-coding-agent', b'/dist/host.js', b'/host.ts', b'next-service.mjs', b'server-service.py'))
            row = {'pid': pid, 'startTicks': ticks, 'executable': exe, 'commandSha256': sha(cmd)}
            if related or pi_process:
                blockers.append({**row, 'reason': 'legacy-or-resource-reference'})
            elif Path(exe).name.startswith(('python', 'node', 'bun', 'deno')) and row not in allowed:
                unknown.append(row)
        except FileNotFoundError:
            if directory.exists(): unreadable.append(pid)
        except (OSError, ValueError, IndexError): unreadable.append(pid)
    writers = []
    for name in plan['sessionRoots']:
        root = Path(name)
        if not root.is_dir(): fail('ADOPTION_SESSION_ROOT_UNAVAILABLE')
        files = list(root.glob('**/*.biomind-writer.lock'))
        if len(files) > 10000: fail('ADOPTION_WRITER_SCAN_LIMIT')
        for filename in files:
            try:
                if filename.is_symlink() or filename.stat().st_size > 32768: raise ValueError()
                value = json.loads(filename.read_text())
                if value.get('schema') != 1 or not isinstance(value.get('pid'), int): raise ValueError()
                if value['hostname'] not in {h['name'] for h in plan['hosts']}: fail('ADOPTION_WRITER_HOST_UNDECLARED')
                if value['hostname'] == host and str(value['pid']) in identities:
                    writers.append({'pid': value['pid'], 'recordSha256': sha(os.fsencode(filename))})
            except FileNotFoundError: continue
            except (OSError, KeyError, TypeError, ValueError): fail('ADOPTION_WRITER_STATE_UNKNOWN')
    jobs = []
    if plan['scheduler'] == 'slurm':
        executable = shutil.which('squeue')
        if not executable: fail('ADOPTION_SCHEDULER_UNAVAILABLE')
        result = subprocess.run([executable, '-h', '-u', pwd.getpwuid(os.getuid()).pw_name, '-o', '%i'],
                                capture_output=True, text=True, timeout=20)
        if result.returncode: fail('ADOPTION_SCHEDULER_UNAVAILABLE')
        jobs = [line for line in result.stdout.splitlines() if line.strip()]
    return {'schema': SCHEMA, 'host': host, 'uid': os.getuid(), 'at': time.time(), 'planSha256': plan['_sha256'],
            'status': 'BUSY' if blockers or unknown or unreadable or writers or jobs else 'clear',
            'legacyProcesses': blockers, 'unreviewedProcesses': unknown, 'unreadablePids': unreadable,
            'activeWriterClaims': writers, 'activeSlurmJobs': len(jobs), 'signalsSent': False}


def audit_hosts(plan):
    reports = []
    for host in plan['hosts']:
        if host['name'] == socket.gethostname():
            report = audit_local(plan)
        else:
            if not host.get('ssh'): fail('ADOPTION_REMOTE_HOST_UNAVAILABLE')
            script = Path(__file__).resolve().parents[1] / 'scripts/plugin-maintenance.py'
            command = shlex.join([sys.executable, '-B', str(script), 'audit-local', '--plan', plan['_path']])
            reply = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                    '-o', 'ConnectTimeout=15', host['ssh'], command], capture_output=True, text=True, timeout=60)
            try: report = json.loads(reply.stdout)
            except ValueError: fail('ADOPTION_REMOTE_HOST_UNAVAILABLE')
            if reply.returncode not in (0, 3): fail('ADOPTION_REMOTE_HOST_UNAVAILABLE')
        if report.get('host') != host['name'] or report.get('uid') != os.getuid() or report.get('planSha256') != plan['_sha256'] or abs(time.time() - report.get('at', 0)) > 120:
            fail('ADOPTION_HOST_EVIDENCE_MISMATCH')
        reports.append(report)
    return reports


@contextlib.contextmanager
def all_leases(plan):
    fds = []
    try:
        before = guard.prepare(plan)
        for rows, field in [(before['scopes'], 'gate')]:
            files = []
            for row in sorted(rows, key=lambda r: r[field]):
                fd = os.open(row[field], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600); fds.append(fd)
                files.append({'fd': fd, 'path': row[field], 'mode': 'exclusive'})
            guard.lock_fds({'files': files})
        final = guard.prepare({**plan, 'resources': True})
        if before['resources'] != final['resources']: fail('RESOURCE_ROOT_CHANGED')
        files = []
        for row in final['resources']:
            fd = os.open(row['lock'], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600); fds.append(fd)
            files.append({'fd': fd, 'path': row['lock'], 'mode': 'exclusive'})
        guard.lock_fds({'files': files})
        yield final
    finally:
        for fd in fds: os.close(fd)


def metadata_snapshot(plan):
    result = {}
    # Hash declarations/trust only; do not open credentials, sessions or memory.
    for root in [Path(plan['agentDir']), Path(plan['cwd']) / '.pi']:
        for name in ('settings.json', 'trust.json'):
            p = root / name
            if p.is_symlink(): fail('ADOPTION_SETTINGS_ALIAS_UNVERIFIED')
            result[str(p)] = sha(p.read_bytes()) if p.is_file() else None
    return result


def adopt(plan, *, collect=audit_hosts, checkpoint=lambda stage: None):
    with state_lock(plan) as state, all_leases(plan) as resources:
        value = transaction(plan, state)
        if value['state'] == 'committed':
            verify_fences(value, closed=False)
            if any(s['legacyUnverified'] for s in guard.plan(plan)['scopes']): fail('ADOPTION_MARKER_CHANGED')
            return {'status': 'already-adopted', 'id': value['id'], 'changed': False}
        if value['state'] not in ('fenced', 'pending'): fail('ADOPTION_FENCE_REQUIRED')
        verify_fences(value)
        reports = collect(plan)
        if not reports or {r['host'] for r in reports} != {h['name'] for h in plan['hosts']}:
            fail('ADOPTION_HOST_EVIDENCE_MISMATCH')
        if any(r.get('uid') != os.getuid() or r.get('planSha256') != plan['_sha256'] or abs(time.time() - r.get('at', 0)) > 120 for r in reports):
            fail('ADOPTION_HOST_EVIDENCE_MISMATCH')
        save(state / 'last-audit.json', reports)
        if any(r['status'] != 'clear' for r in reports): fail('LEGACY_OCCUPANCY_BUSY')
        verify_fences(value)
        checkpoint('after-audit')
        if guard.plan(plan)['resources'] != resources['resources']: fail('RESOURCE_ROOT_CHANGED')
        current = metadata_snapshot(plan)
        if value['state'] == 'pending' and current != value['settingsBefore']: fail('ADOPTION_SETTINGS_CHANGED')
        if value['state'] == 'fenced':
            original = {}
            for row in resources['scopes']:
                mark = Path(row['marker'])
                if mark.exists():
                    if mark.is_symlink() or mark.stat().st_size > 65536: fail('ADOPTION_MARKER_UNSAFE')
                    original[str(mark)] = base64.b64encode(mark.read_bytes()).decode()
                else: original[str(mark)] = None
            value.update(state='pending', settingsBefore=current, previousMarkers=original)
            save(state / 'transaction.json', value)
        # A single transaction commit point makes partial multi-scope adoption
        # recoverable. Persistent pointers close admission after a crash.
        for row in resources['scopes']:
            pointer = Path(row['lock']).with_suffix('.adoption.json')
            save(pointer, {'id': value['id'], 'transaction': str(state / 'transaction.json')})
        checkpoint('pending')
        for row in resources['scopes']:
            mark = Path(row['marker'])
            ours = {'schema': guard.SCHEMA, 'root': row['root'], 'origin': 'legacy-adopted', 'transactionId': value['id']}
            previous = value['previousMarkers'][str(mark)]
            if mark.is_symlink(): fail('ADOPTION_MARKER_CHANGED')
            if mark.exists():
                raw = mark.read_bytes()
                if (previous is None or raw != base64.b64decode(previous)) and json.loads(raw) != ours:
                    fail('ADOPTION_MARKER_CHANGED')
            elif previous is not None: fail('ADOPTION_MARKER_CHANGED')
        for row in resources['scopes']:
            save(row['marker'], {'schema': guard.SCHEMA, 'root': row['root'], 'origin': 'legacy-adopted', 'transactionId': value['id']})
            checkpoint('marker')
        if metadata_snapshot(plan) != current: fail('ADOPTION_SETTINGS_CHANGED')
        verify_fences(value)
        checkpoint('before-commit')
        if metadata_snapshot(plan) != current: fail('ADOPTION_SETTINGS_CHANGED')
        if guard.plan(plan)['resources'] != resources['resources']: fail('RESOURCE_ROOT_CHANGED')
        verify_fences(value)
        value.update(state='committed', committedAt=time.time(), auditedHosts=[r['host'] for r in reports])
        save(state / 'transaction.json', value)
        return {'status': 'adopted', 'id': value['id'], 'changed': True, 'settingsChanged': False,
                'packageFilesChanged': False, 'sessionHistoryChanged': False, 'processesStopped': False}


def rollback(plan):
    with state_lock(plan) as state, all_leases(plan) as resources:
        value = transaction(plan, state)
        verify_fences(value, closed=False)
        if value['state'] == 'rolled-back': return {'status': 'rolled-back', 'changed': False}
        if value['state'] not in ('fenced', 'pending', 'committed'): fail('ADOPTION_STATE_REQUIRES_RECOVERY')
        for row in resources['scopes']:
            if value['state'] == 'fenced': continue
            p = Path(row['marker']); previous = value.get('previousMarkers', {}).get(str(p))
            if p.exists():
                current = guard.private_json(p)
                if current.get('transactionId') != value['id']:
                    if previous is None or p.read_bytes() != base64.b64decode(previous): fail('ADOPTION_MARKER_CHANGED')
                    continue
                if previous is None: p.unlink()
                else: atomic_bytes(p, base64.b64decode(previous))
        value['state'] = 'rolled-back'; save(state / 'transaction.json', value)
        return {'status': 'rolled-back', 'entriesRemainFenced': True, 'settingsOrPackagesRestored': False}


def entries_action(plan, activate=False):
    with state_lock(plan) as state, all_leases(plan):
        value = transaction(plan, state)
        if activate:
            if value['state'] != 'committed': fail('ADOPTION_COMMIT_REQUIRED')
            verify_fences(value, closed=False)
            for entry in value['entries']:
                if entry['replacement']:
                    atomic_bytes(entry['path'], base64.b64decode(entry['replacement']), 0o700)
            return {'status': 'activated', 'oldServersRemainFenced': True}
        if value['state'] not in ('fencing', 'fenced', 'rolled-back'): fail('ROLLBACK_ADOPTION_BEFORE_RESTORING_OLD_ENTRIES')
        for entry in value['entries']:
            p = Path(entry['path'])
            if entry_snapshot(p)['sha256'] == entry['original']['sha256']: continue
            if p.is_symlink() or sha(p.read_bytes()) not in entry['acceptedSha256']: fail('ADOPTION_ENTRY_CHANGED')
            old = entry['original']
            if old['kind'] == 'symlink':
                temporary = p.with_name('.' + p.name + '.' + uuid.uuid4().hex)
                temporary.symlink_to(old['target']); os.replace(temporary, p)
            else: atomic_bytes(p, base64.b64decode(old['data']), old['mode'])
        value['state'] = 'restored'; save(state / 'transaction.json', value)
        return {'status': 'restored', 'oldEntriesRestored': True, 'settingsOrPackagesRestored': False}

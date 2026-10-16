"""One-time, operator-confirmed shutdown of the reviewed pre-idle-stop service.

The old wrapper has no atomic HTTP idle admission. An explicit maintenance
window in which every known client sends no new requests is therefore required.
An empty running-session list alone never authorizes a signal. This module only
accepts an ownerless, exactly identified holder/backend pair, uses the holder's
existing SIGTERM handler, and waits for its genuine stopped record and lease.
"""
import base64
import contextlib
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import sys
import time
import urllib.request


# These are the reviewed pre-protocol implementations, not arbitrary process
# names. Supporting another legacy bootstrap requires reviewing its lifecycle.
LEGACY_HOLDER_SHA256 = 'bd5bb97f81c988cccf022d220bd38a3aab48240116ebbbff40dd533c07c062bf'
LEGACY_BOOTSTRAP_SHA256 = '628779a555907088fcbff69b84a20f09c38c55b0f9036b6029a1797ee4395dc5'


@contextlib.contextmanager
def maintenance_fence(core_root, plan_file, service):
    """Reuse the core's existing receipt and fence checks under its state lock."""
    root = service.absolute(core_root)
    release = json.loads((root / 'RELEASE.json').read_text())
    manifest = json.loads((root / 'integrations/pi_runtime/dist/build-manifest.json').read_text())
    service.require(re.fullmatch(r'[a-f0-9]{64}', release.get('releaseId', ''))
                    and release['releaseId'] == manifest.get('buildId'), 'MAINTENANCE_CORE_IDENTITY_MISMATCH')
    sys.path.insert(0, str(root))
    try:
        adoption = importlib.import_module('agent_core.pi_plugin_adoption')
        service.require(Path(adoption.__file__).resolve() == root / 'agent_core/pi_plugin_adoption.py',
                        'MAINTENANCE_CORE_IDENTITY_MISMATCH')
        plan = adoption.load_plan(plan_file)
        service.require(all(plan.get(key) is True for key in
                            ('maintenanceApproved', 'entryInventoryConfirmed', 'hostInventoryConfirmed')),
                        'MAINTENANCE_REVIEW_REQUIRED')
        service.require(any(host['name'] == socket.gethostname() for host in plan['hosts']),
                        'SERVICE_HOST_NOT_IN_MAINTENANCE')
        # stop must consume an existing receipt, never create a maintenance state.
        service.private(plan['stateDir'], True)
        service.private(Path(plan['stateDir']) / 'transaction.json')
        service.private(Path(plan['stateDir']) / 'operation.lock')
        with adoption.state_lock(plan) as state:
            transaction = adoption.transaction(plan, state)
            service.require(transaction.get('state') == 'fenced', 'MAINTENANCE_FENCE_REQUIRED')
            adoption.verify_fences(transaction)
            yield transaction, lambda: adoption.verify_fences(transaction)
    except Exception as error:
        if isinstance(error, service.Refused):
            raise
        raise service.Refused('MAINTENANCE_FENCE_UNVERIFIED') from None
    finally:
        sys.path.remove(str(root))


def host_process(pid, service):
    """Read only this exact process, including children of every native thread."""
    service.require(type(pid) is int and pid > 1, 'LEGACY_PROCESS_IDENTITY_UNCONFIRMED')
    directory = Path('/proc') / str(pid)
    before = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in (directory / 'status').read_text().splitlines() if ':' in line)
    service.require(before[0] != 'Z' and all(int(value) == os.getuid() for value in status['Uid'].split()),
                    'LEGACY_PROCESS_IDENTITY_UNCONFIRMED')
    service.require(os.readlink(directory / 'ns/pid') == os.readlink('/proc/self/ns/pid'),
                    'LEGACY_PROCESS_NAMESPACE_MISMATCH')
    raw = (directory / 'cmdline').read_bytes()
    service.require(0 < len(raw) <= 65536 and raw.endswith(b'\0'), 'LEGACY_PROCESS_IDENTITY_UNCONFIRMED')
    argv = [value.decode() for value in raw[:-1].split(b'\0')]
    executable = os.readlink(directory / 'exe')
    tasks = list((directory / 'task').iterdir())
    service.require(0 < len(tasks) <= 4096, 'LEGACY_PROCESS_TREE_UNVERIFIED')
    children = set()
    for task in tasks:
        try:
            children.update(int(value) for value in (task / 'children').read_text().split())
        except FileNotFoundError:
            # A vanished thread cannot retain a child; another live thread's
            # children and the second complete sample are checked again.
            service.require(not task.exists(), 'LEGACY_PROCESS_TREE_UNVERIFIED')
    after = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    service.require(before[19] == after[19] and before[1] == after[1]
                    and (directory / 'cmdline').read_bytes() == raw
                    and os.readlink(directory / 'exe') == executable, 'LEGACY_PROCESS_CHANGED')
    service.require(int(status['SigCgt'].strip(), 16) & (1 << (signal.SIGTERM - 1)),
                    'LEGACY_GRACEFUL_HANDLER_UNVERIFIED')
    return dict(pid=pid, startTicks=before[19], ppid=int(before[1]), executable=executable,
                argv=argv, commandSha256=hashlib.sha256(raw).hexdigest(), children=sorted(children))


def ownerless_pair(config, record, transaction, service):
    holder = host_process(record['pid'], service)
    service.require(holder['startTicks'] == record.get('startTicks'), 'LEGACY_PROCESS_CHANGED')
    argv = holder['argv']
    service.require(len(argv) == 7 and argv[2:] == ['run', '--config', config['configPath'], '--instance', record['instanceId']]
                    and Path(argv[0]).resolve() == Path(holder['executable']).resolve(),
                    'LEGACY_HOLDER_COMMAND_MISMATCH')
    entry = next((entry for entry in transaction['entries'] if entry['path'] == argv[1]), None)
    service.require(entry and entry['original'].get('kind') == 'file', 'LEGACY_HOLDER_NOT_FENCED')
    original = base64.b64decode(entry['original']['data'], validate=True)
    service.require(hashlib.sha256(original).hexdigest() == LEGACY_HOLDER_SHA256, 'LEGACY_HOLDER_NOT_REVIEWED')
    service.require(len(config['command']) == 2 and
                    hashlib.sha256(Path(config['command'][1]).read_bytes()).hexdigest() == LEGACY_BOOTSTRAP_SHA256,
                    'LEGACY_BOOTSTRAP_NOT_REVIEWED')
    service.require(len(holder['children']) == 1, 'LEGACY_PROCESS_TREE_NOT_OWNERLESS')
    backend = host_process(holder['children'][0], service)
    service.require(backend['ppid'] == holder['pid'] and backend['argv'] == config['command']
                    and Path(config['command'][0]).resolve() == Path(backend['executable']).resolve(),
                    'LEGACY_BACKEND_COMMAND_MISMATCH')
    # No owner, history helper, shell, scientific process or unknown descendant
    # is ever stopped by this compatibility path, even when /running is empty.
    service.require(not backend['children'], 'LEGACY_PROCESS_TREE_NOT_OWNERLESS')
    return holder, backend


def authenticated_idle(config, instance, endpoint, service):
    password = service.environment(config, instance).get('PI_WEB_PASSWORD')
    service.require(isinstance(password, str) and password, 'SERVICE_AUTH_REQUIRED')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise service.Refused('SERVICE_STOP_REDIRECT_REFUSED')

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(route):
        request = urllib.request.Request('http://127.0.0.1:' + str(endpoint['port']) + route,
            headers={'Authorization': 'Basic ' + base64.b64encode(('pi:' + password).encode()).decode()})
        with opener.open(request, timeout=5) as response:
            raw = response.read(65537)
            service.require(response.status == 200 and len(raw) <= 65536, 'LEGACY_IDLE_STATE_UNVERIFIED')
            return json.loads(raw)

    profile = read('/api/agent/profile')
    service.require(profile.get('profile') == 'biomind' and profile.get('serverId') == config['serverId'],
                    'BIOMIND_PROFILE_MISMATCH')
    running = read('/api/agent/running')
    service.require(running.get('runningSessionIds') == [], 'SERVICE_BUSY')


def pair_identity(pair):
    return tuple((row['pid'], row['startTicks'], row['ppid'], row['executable'], row['commandSha256']) for row in pair)


def pidfd_exited(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


def stop_legacy_idle(config, instance, core_root, plan_file, *, clients_quiescent=False,
                     idle_confirmed=False, timeout=45, service):
    service.require(clients_quiescent is True and idle_confirmed is True, 'LEGACY_MAINTENANCE_WINDOW_REQUIRED')
    service.require(config['mode'] == 'local', 'LEGACY_LOCAL_SERVICE_REQUIRED')
    service.require(instance and re.fullmatch(r'[a-f0-9]{32}', instance), 'EXPLICIT_INSTANCE_REQUIRED')
    service.require(Path('/proc/1/comm').read_text().strip() in ('systemd', 'init'), 'RUN_ON_REAL_SERVICE_HOST_REQUIRED')
    service.require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'PIDFD_REQUIRED')
    with maintenance_fence(core_root, plan_file, service) as (transaction, verify_fences):
        fds = []
        try:
            with service.locked(config, create=False):
                record = service.read_record(config)
                service.require(record and record.get('instanceId') == instance, 'SERVICE_INSTANCE_REPLACED')
                service.require(record.get('binding') == config['binding'], 'ACTIVE_CONFIG_BINDING_CHANGED')
                service.require(record.get('host') == socket.gethostname(), 'RUN_ON_SERVICE_HOST_REQUIRED')
                service.require(record.get('status') == 'ready' and record.get('holderStarted') is True
                                and service.process_alive(record), 'SERVICE_PROCESS_IDENTITY_UNCONFIRMED')
                endpoint = service.health(config, instance)
                service.require(endpoint and endpoint.get('idleStopProtocol') is None, 'USE_NATIVE_IDLE_STOP')
                service.require(service.backend_lease_held(config, create=False), 'LEGACY_BACKEND_LEASE_UNCONFIRMED')
                first = ownerless_pair(config, record, transaction, service)
                authenticated_idle(config, instance, endpoint, service)
                second = ownerless_pair(config, record, transaction, service)
                service.require(pair_identity(first) == pair_identity(second), 'LEGACY_PROCESS_CHANGED')
                for process in second:
                    fds.append(os.pidfd_open(process['pid'], 0))
                verify_fences()
                service.require(pair_identity(second) == pair_identity(ownerless_pair(config, record, transaction, service))
                                and not any(pidfd_exited(fd) for fd in fds), 'LEGACY_PROCESS_CHANGED')
                # One normal operator signal to the reviewed holder. It forwards
                # SIGTERM to its own backend, whose existing handler closes HTTP
                # admission and flushes. No process groups, SIGKILL or retry.
                signal.pidfd_send_signal(fds[0], signal.SIGTERM, None, 0)
            # The old holder must acquire service.lock to write its own receipt.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with service.locked(config, create=False):
                    current = service.read_record(config)
                    service.require(current and current.get('instanceId') == instance
                                    and current.get('binding') == config['binding'], 'SERVICE_INSTANCE_REPLACED')
                    if current.get('status') == 'stopped' and all(pidfd_exited(fd) for fd in fds):
                        service.require(current.get('exitCode') == 0, 'LEGACY_GRACEFUL_EXIT_FAILED')
                        service.require(not service.backend_lease_held(config, create=False), 'LEGACY_BACKEND_LEASE_UNCONFIRMED')
                        return service.public(config, current, status='stopped', signalsSent=True,
                            signal='SIGTERM', signalCount=1, legacyOperatorShutdown=True, nativeIdleShutdown=False,
                            ownerlessProcessTree=True, atomicIdleAdmission=False, operatorWindowRequired=True,
                            maintenanceTransactionId=transaction['id'])
                time.sleep(.1)
            raise service.Refused('SERVICE_STOP_UNCONFIRMED')
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            # No arbitrary process arguments, credential contents or HTTP bodies.
            raise service.Refused('LEGACY_STOP_EVIDENCE_OR_OUTCOME_UNCONFIRMED') from None
        finally:
            for fd in fds:
                os.close(fd)

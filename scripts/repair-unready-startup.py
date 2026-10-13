#!/usr/bin/env python3
"""Repair only the known never-ready PROFILE_TIMEOUT launcher, in host context.

No password/environment-file contents are read or changed. Dry-run is default.
The only signal is SIGTERM to a pidfd-bound, fully verified service holder.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
OLD_MANAGER_SHA256 = 'bd5bb97f81c988cccf022d220bd38a3aab48240116ebbbff40dd533c07c062bf'
OLD_LAUNCHER_SHA256 = '711afc3d2ed36016d276a595edfd710b630bebf693c91888ed5e244e568ff0f1'
NEW_LAUNCHER_SHA256 = '628779a555907088fcbff69b84a20f09c38c55b0f9036b6029a1797ee4395dc5'


class Refused(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def file_stat(path, private=True, directory=False):
    info = Path(path).lstat()
    require(not stat.S_ISLNK(info.st_mode) and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)), 'UNSAFE_FILE_TYPE')
    require(info.st_uid == os.getuid(), 'FOREIGN_FILE_OWNER')
    require(not stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022), 'UNSAFE_FILE_PERMISSIONS')
    return info


def read_file(path, private=True, limit=262144):
    before = file_stat(path, private)
    require(before.st_size <= limit, 'FILE_TOO_LARGE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        current = os.fstat(fd)
        require((before.st_dev, before.st_ino) == (current.st_dev, current.st_ino), 'FILE_CHANGED')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(limit + 1)
        require(len(data) <= limit, 'FILE_TOO_LARGE')
        return data
    finally:
        os.close(fd)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def metadata(path):
    info = file_stat(path)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode))


@contextlib.contextmanager
def lock_existing(path):
    file_stat(path)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('SERVICE_LOCK_BUSY')
        yield
    finally:
        os.close(fd)


class Host:
    def process(self, pid):
        root = Path('/proc') / str(pid)
        try:
            require(root.stat().st_uid == os.getuid(), 'FOREIGN_PROCESS_OWNER')
            fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
            argv = [item.decode('utf8') for item in (root / 'cmdline').read_bytes().split(b'\0') if item]
            children = set()
            for thread in (root / 'task').iterdir():
                children.update(int(value) for value in (thread / 'children').read_text().split())
            return dict(pid=pid, ticks=fields[19], parent=int(fields[1]), argv=argv, children=sorted(children))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            # Codex's tool PID namespace may not expose the host PID. Never
            # confuse that with evidence that the service has exited.
            raise Refused('PROCESS_NOT_VISIBLE_USE_HOST_SSH')

    def open_pid(self, pid):
        require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'PIDFD_SUPPORT_REQUIRED')
        try:
            return os.pidfd_open(pid)
        except ProcessLookupError:
            raise Refused('PROCESS_IDENTITY_CHANGED')
        except OSError:
            raise Refused('PIDFD_UNAVAILABLE_ON_THIS_HOST')

    def terminate(self, fd):
        signal.pidfd_send_signal(fd, signal.SIGTERM)

    def wait(self, fd, timeout):
        return bool(select.select([fd], [], [], timeout)[0])

    def close_pid(self, fd):
        os.close(fd)

    def ensure(self, manager, config):
        completed = subprocess.run([sys.executable, str(manager), 'ensure', '--config', str(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, timeout=45, check=False)
        require(completed.returncode == 0, 'ENSURE_FAILED')
        try:
            value = json.loads(completed.stdout)
        except (ValueError, TypeError):
            raise Refused('ENSURE_RESPONSE_INVALID')
        require(isinstance(value, dict) and value.get('schema') == 'BioMindStudioServiceV1', 'ENSURE_RESPONSE_INVALID')
        return value


def configuration(config_path, source_path):
    config_path = Path(os.path.abspath(config_path)); source_path = Path(os.path.abspath(source_path))
    require(config_path.name == 'service.json' and config_path.parent.name == 'private', 'UNEXPECTED_CONFIGURATION_PATH')
    file_stat(config_path.parent, directory=True)
    raw = read_file(config_path)
    config = json.loads(raw)
    require(config.get('schema') == 'BioMindStudioServiceV1' and config.get('mode') == 'local', 'LOCAL_SERVICE_REQUIRED')
    deployment = config_path.parent.parent
    launcher = deployment / 'service-v1' / 'next-service.mjs'
    manager = launcher.with_name('server-service.py')
    state = config_path.parent / 'state'
    environment_file = config_path.parent / 'runtime-environment.json'
    require(config.get('stateRoot') == str(state) and config.get('environmentFile') == str(environment_file), 'UNEXPECTED_CONFIGURATION_LAYOUT')
    command = config.get('command')
    require(isinstance(command, list) and len(command) == 2 and isinstance(command[0], str) and command[0].startswith('/') and command[1] == str(launcher), 'UNEXPECTED_LAUNCH_COMMAND')
    require(source_path.name == 'next-service.mjs', 'UNEXPECTED_SOURCE_PATH')
    file_stat(launcher.parent, directory=True); file_stat(state, directory=True)
    require(digest(read_file(manager)) == OLD_MANAGER_SHA256, 'MANAGER_VERSION_MISMATCH')
    require(digest(read_file(launcher)) == OLD_LAUNCHER_SHA256, 'INSTALLED_LAUNCHER_VERSION_MISMATCH')
    require(bool(NEW_LAUNCHER_SHA256), 'REPAIR_SOURCE_NOT_FROZEN')
    source = read_file(source_path, private=False)
    require(digest(source) == NEW_LAUNCHER_SHA256, 'REPAIR_SOURCE_VERSION_MISMATCH')
    return dict(config_path=config_path, config=config, config_hash=digest(raw), manager=manager, launcher=launcher,
                source_path=source_path, source=source, state=state, environment_file=environment_file,
                environment_metadata=metadata(environment_file))


def check_instance(context, host, expected=None):
    config, state = context['config'], context['state']
    require(digest(read_file(context['config_path'])) == context['config_hash'], 'CONFIGURATION_CHANGED')
    require(metadata(context['environment_file']) == context['environment_metadata'], 'WORKBENCH_CREDENTIAL_FILE_CHANGED')
    record = json.loads(read_file(state / 'service.json'))
    require(record.get('schema') == 'BioMindStudioServiceV1' and record.get('serviceId') == config.get('serviceId') and record.get('serverId') == config.get('serverId'), 'SERVICE_IDENTITY_MISMATCH')
    instance = record.get('instanceId')
    require(isinstance(instance, str) and len(instance) == 32 and all(c in '0123456789abcdef' for c in instance), 'INVALID_INSTANCE_ID')
    require(expected is None or instance == expected, 'SERVICE_INSTANCE_CHANGED')
    require(record.get('mode') == 'local' and record.get('status') in ('starting', 'slow-start') and record.get('holderStarted') is True, 'SERVICE_NOT_UNREADY_STARTUP')
    require('port' not in record and not (state / (instance + '.endpoint.json')).exists(), 'SERVICE_ALREADY_HAS_ENDPOINT')
    require(record.get('host') == socket.gethostname(), 'RUN_REPAIR_ON_SERVICE_HOST')
    require(type(record.get('pid')) is int and record['pid'] > 1 and isinstance(record.get('startTicks'), str), 'INVALID_HOLDER_IDENTITY')
    log = read_file(state / 'logs' / (instance + '.backend.log'), limit=16 * 1024 * 1024)
    require(re.search(rb'uncaughtException:\s*(?:Error:\s*)?PROFILE_TIMEOUT\b', log) and b'service_ready' not in log, 'KNOWN_STARTUP_FAILURE_NOT_CONFIRMED')
    holder = host.process(record['pid'])
    require(holder['ticks'] == record['startTicks'], 'HOLDER_START_TICKS_CHANGED')
    require(len(holder['argv']) == 7 and holder['argv'][1:] == [str(context['manager']), 'run', '--config', str(context['config_path']), '--instance', instance], 'HOLDER_COMMAND_MISMATCH')
    require(len(holder['children']) == 1, 'UNEXPECTED_HOLDER_CHILDREN')
    child = host.process(holder['children'][0])
    require(child['parent'] == holder['pid'] and child['argv'] == config['command'], 'BACKEND_COMMAND_MISMATCH')
    require(not child['children'], 'BACKEND_HAS_CHILDREN_REFUSE_TASK_INTERRUPTION')
    return record


def atomic_upgrade(context, instance):
    target = context['launcher']
    old = read_file(target)
    require(digest(old) == OLD_LAUNCHER_SHA256, 'INSTALLED_LAUNCHER_VERSION_MISMATCH')
    require(digest(read_file(context['source_path'], private=False)) == NEW_LAUNCHER_SHA256, 'REPAIR_SOURCE_VERSION_MISMATCH')
    backup = target.with_name(target.name + '.backup-unready-' + instance + '-' + str(time.time_ns()))
    temporary = target.with_name(target.name + '.repair-' + uuid.uuid4().hex)
    for filename, data in ((backup, old), (temporary, context['source'])):
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(os.dup(fd), 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
        finally:
            os.close(fd)
    try:
        require(digest(read_file(target)) == OLD_LAUNCHER_SHA256, 'INSTALLED_LAUNCHER_CHANGED')
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def repair(config_path, source_path, apply=False, host=None):
    host = host or Host()
    context = configuration(config_path, source_path)
    output = dict(schema='BioMindUnreadyStartupRepairV1', applied=False, status='verified', configUnchanged=True,
                  workbenchCredentialContentsRead=False, automaticTaskReplay=False, modelStarted=False)
    pidfd = None
    try:
        with lock_existing(context['state'] / 'service.lock'):
            record = check_instance(context, host)
            output.update(instanceId=record['instanceId'], serverId=record['serverId'], serviceId=record['serviceId'])
            if not apply:
                return output
            pidfd = host.open_pid(record['pid'])
            # Revalidate after pidfd binding, before either the patch or signal.
            check_instance(context, host, record['instanceId'])
            backup = atomic_upgrade(context, record['instanceId'])
            output.update(applied=True, launcherBackup=str(backup), status='launcher-upgraded')
            check_instance(context, host, record['instanceId'])
            # The holder forwards SIGTERM only to its owned backend. No killpg,
            # PID-name search, SIGKILL, task signal or scheduler operation exists.
            host.terminate(pidfd)
        # Do not hold service.lock while waiting: the holder needs it to record
        # its normal stopped state and release the backend lifetime lease.
        require(host.wait(pidfd, 30), 'HOLDER_STOP_TIMEOUT_NO_FORCE_KILL')
        with lock_existing(context['state'] / 'service.lock'):
            current = json.loads(read_file(context['state'] / 'service.json'))
            require(current.get('instanceId') == record['instanceId'], 'SERVICE_INSTANCE_CHANGED')
            require(current.get('status') in ('stopped', 'failed'), 'HOLDER_STOP_NOT_CONFIRMED')
            with lock_existing(context['state'] / 'backend.lock'):
                require(digest(read_file(context['config_path'])) == context['config_hash'], 'CONFIGURATION_CHANGED')
                require(metadata(context['environment_file']) == context['environment_metadata'], 'WORKBENCH_CREDENTIAL_FILE_CHANGED')
                require(digest(read_file(context['launcher'])) == NEW_LAUNCHER_SHA256, 'INSTALLED_LAUNCHER_CHANGED')
        value = host.ensure(context['manager'], context['config_path'])
        require(value.get('serverId') == record['serverId'] and value.get('serviceId') == record['serviceId'], 'ENSURE_IDENTITY_MISMATCH')
        output.update(status='restart-requested', serviceStatus=value.get('status'), nextInstanceId=value.get('instanceId'), reused=value.get('reused'))
        # ensure's starting/pending response is not a successful health check.
        return output
    except (Refused, OSError, ValueError, subprocess.TimeoutExpired) as error:
        # An I/O error after rename may still have installed the patch. Report
        # the actual file state instead of calling such a partial repair untouched.
        try:
            output['applied'] = digest(read_file(context['launcher'])) == NEW_LAUNCHER_SHA256
        except (Refused, OSError):
            output['launcherState'] = 'unknown'
        output.update(status='refused', code=str(error) if isinstance(error, Refused) else 'REPAIR_OPERATION_FAILED')
        return output
    finally:
        if pidfd is not None:
            host.close_pid(pidfd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'deployment/private/service.json'))
    parser.add_argument('--source', default=str(ROOT / 'server/next-service.mjs'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        output = repair(args.config, args.source, args.apply)
    except (Refused, OSError, ValueError, subprocess.TimeoutExpired) as error:
        output = dict(schema='BioMindUnreadyStartupRepairV1', applied=False, status='refused', code=str(error) if isinstance(error, Refused) else 'REPAIR_CHECK_FAILED',
                      workbenchCredentialContentsRead=False, automaticTaskReplay=False)
    print(json.dumps(output, ensure_ascii=False))
    return 2 if output['status'] == 'refused' else 0


if __name__ == '__main__':
    raise SystemExit(main())

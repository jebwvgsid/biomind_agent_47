#!/usr/bin/env python3
"""Persistent service discovery/startup for the existing BioMind Next backend.

The desktop calls ensure once, then discover while queued/preparing. No command
here cancels a job. Explicit stop uses authenticated native idle shutdown. Only the configured operator command
runs; this module never creates sessions, model agents, memory or project state.
The explicit stop-legacy-idle migration command uses the reviewed old holder's
normal operator SIGTERM only under a fenced, confirmed ownerless maintenance
window. It is never an automatic fallback from native idle stop.
"""
import argparse
import base64
import contextlib
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid

SCHEMA = 'BioMindStudioServiceV1'
HERE = Path(__file__).resolve().parent
ACTIVE_SLURM = {'PENDING', 'RUNNING', 'CONFIGURING', 'COMPLETING', 'SUSPENDED', 'RESIZING', 'REQUEUED', 'REQUEUE_FED', 'REQUEUE_HOLD', 'SIGNALING', 'STAGE_OUT'}
STARTING_SLURM = {'PENDING', 'CONFIGURING', 'REQUEUED', 'REQUEUE_FED', 'REQUEUE_HOLD'}


class Refused(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def absolute(value, exists=True):
    require(isinstance(value, str) and value.startswith('/') and '\x00' not in value and len(value) <= 4096, 'INVALID_ABSOLUTE_PATH')
    path = Path(value)
    require('..' not in path.parts and path != Path('/'), 'INVALID_ABSOLUTE_PATH')
    return path.resolve(strict=exists)


def private(path, directory=False):
    info = Path(path).lstat()
    require(not stat.S_ISLNK(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            'PRIVATE_STATE_PERMISSIONS_REQUIRED')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), 'INVALID_PRIVATE_STATE')


def atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as target:
            json.dump(value, target, ensure_ascii=False)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    path = absolute(str(path))
    config = json.loads(path.read_text())
    require(config.get('schema') == SCHEMA and config.get('mode') in ('local', 'slurm'), 'INVALID_SERVICE_CONFIG')
    require(re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', config.get('serverId', '')) is not None, 'INVALID_SERVER_ID')
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,48}', config.get('serviceId', '')) is not None, 'INVALID_SERVICE_ID')
    config['configPath'] = str(path)
    config['stateRoot'] = str(absolute(config.get('stateRoot'), exists=False))
    state = Path(config['stateRoot'])
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(state, True)
    (state / 'logs').mkdir(mode=0o700, exist_ok=True)
    private(state / 'logs', True)
    config['cwd'] = str(absolute(config.get('cwd', str(HERE))))
    require(Path(config['cwd']).is_dir(), 'INVALID_WORKING_DIRECTORY')
    command = config.get('command')
    require(isinstance(command, list) and 0 < len(command) <= 32 and
            all(isinstance(arg, str) and '\x00' not in arg and len(arg) <= 4096 for arg in command), 'INVALID_LAUNCH_COMMAND')
    require(command[0].startswith('/') and os.access(command[0], os.X_OK), 'LAUNCH_EXECUTABLE_UNAVAILABLE')
    environment = config.get('environment', {})
    require(isinstance(environment, dict) and len(environment) <= 128 and all(
        re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) and isinstance(value, str) and '\x00' not in value
        for key, value in environment.items()), 'INVALID_ENVIRONMENT')
    if 'environmentFile' in config:
        config['environmentFile'] = str(absolute(config['environmentFile']))
        private(config['environmentFile'])
    timeout = config.get('startupTimeoutSeconds', 180)
    require(type(timeout) is int and 5 <= timeout <= 3600, 'INVALID_STARTUP_TIMEOUT')
    config['startupTimeoutSeconds'] = timeout
    if config['mode'] == 'slurm':
        slurm = config.get('slurm', {})
        require(isinstance(slurm, dict), 'INVALID_SLURM_CONFIG')
        for key in ('partition', 'account', 'qos', 'constraint'):
            require(key not in slurm or isinstance(slurm[key], str) and re.fullmatch(r'[A-Za-z0-9_.,:+-]{1,128}', slurm[key]), 'INVALID_SLURM_OPTION')
        require('time' not in slurm or re.fullmatch(r'[0-9:-]{1,32}', str(slurm['time'])), 'INVALID_SLURM_TIME')
        require('cpus' not in slurm or type(slurm['cpus']) is int and 1 <= slurm['cpus'] <= 512, 'INVALID_SLURM_CPUS')
        require('memory' not in slurm or re.fullmatch(r'[1-9][0-9]*[KMGTP]?', str(slurm['memory']), re.I), 'INVALID_SLURM_MEMORY')
        for tool in ('sbatch', 'squeue'):
            value = slurm.get(tool, tool)
            require(isinstance(value, str) and value and '\x00' not in value and (value == tool or value.startswith('/')), 'INVALID_SLURM_EXECUTABLE')
    config['binding'] = hashlib.sha256(json.dumps({key: value for key, value in config.items() if key != 'binding'}, sort_keys=True).encode()).hexdigest()
    return config


@contextlib.contextmanager
def locked(config):
    path = Path(config['stateRoot']) / 'service.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private(path)
        # Bounded command latency even if a different caller is submitting.
        deadline = time.monotonic() + 25
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refused('SERVICE_LOCK_BUSY')
                time.sleep(.1)
        yield
    finally:
        os.close(fd)


def read_record(config):
    path = Path(config['stateRoot']) / 'service.json'
    if not path.exists():
        return None
    private(path)
    record = json.loads(path.read_text())
    require(record.get('schema') == SCHEMA and record.get('serviceId') == config['serviceId']
            and record.get('serverId') == config['serverId'], 'SERVICE_STATE_IDENTITY_MISMATCH')
    return record


def backend_lease(config):
    """Held by both the supervisor and child, including after supervisor loss."""
    path = Path(config['stateRoot']) / 'backend.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private(path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def backend_lease_held(config):
    try:
        fd = backend_lease(config)
    except BlockingIOError:
        return True
    os.close(fd)
    return False


def write_record(config, record):
    atomic(Path(config['stateRoot']) / 'service.json', record)


def ticks(pid):
    return Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]


def process_alive(record):
    try:
        info = Path('/proc/' + str(record['pid']) + '/stat').read_text().rsplit(')', 1)[1].split()
        return record.get('host') == socket.gethostname() and info[0] != 'Z' and info[19] == record.get('startTicks')
    except (OSError, KeyError, ValueError, ProcessLookupError):
        return False


def queue(config, record):
    slurm = config.get('slurm', {})
    command = [slurm.get('squeue', 'squeue'), '--noheader', '--format=%i|%T|%N|%j', '--user=' + str(os.getuid())]
    # A completed job ID can make squeue fail with "invalid job id". Query the
    # unique recorded name and still verify any returned ID before attaching.
    command += ['--name=' + record['jobName']]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {'unknown': True, 'code': 'SCHEDULER_UNAVAILABLE'}
    if result.returncode:
        return {'unknown': True, 'code': 'SCHEDULER_UNAVAILABLE'}
    rows = []
    for line in result.stdout.splitlines():
        cells = line.strip().split('|')
        if len(cells) != 4 or not re.fullmatch(r'[0-9]+', cells[0]):
            return {'unknown': True, 'code': 'SCHEDULER_RESPONSE_INVALID'}
        job_id, state, node, name = cells
        if name != record.get('jobName') or record.get('jobId') and job_id != record['jobId']:
            return {'unknown': True, 'code': 'JOB_IDENTITY_MISMATCH'}
        rows.append(dict(jobId=job_id, schedulerState=state, node=node))
    if len(rows) > 1:
        return {'unknown': True, 'code': 'DUPLICATE_SERVICE_JOBS'}
    return rows[0] if rows else None


def public(config, record=None, **changes):
    result = dict(schema=SCHEMA, serviceId=config['serviceId'], serverId=config['serverId'], status='stopped',
                  serviceMode=config['mode'], transport='jump' if config['mode'] == 'slurm' else 'direct',
                  user=getpass.getuser(), automaticReplay=False, desktopDetach=True)
    if record:
        for key in ('instanceId', 'jobId', 'jobName', 'host', 'port', 'status', 'code', 'schedulerState', 'startedAt', 'heartbeatAt'):
            if record.get(key) is not None:
                result[key] = record[key]
    result.update(changes)
    # The endpoint is always compute-node loopback. A desktop connects to host
    # through SSH ProxyJump, rather than forwarding from login to an exposed port.
    if result.get('host'):
        result['remoteAddress'] = '127.0.0.1'
        result['tunnelMode'] = 'ssh-to-service-host'
    return result


def inspect(config, record):
    if not record:
        return public(config)
    if record.get('binding') != config['binding']:
        return public(config, record, status='configuration-changed', code='ACTIVE_CONFIG_BINDING_CHANGED')
    if config['mode'] == 'slurm':
        observed = queue(config, record)
        if observed and observed.get('unknown'):
            return public(config, record, status='unknown', code=observed['code'])
        if not observed:
            if record.get('status') == 'submission-unknown':
                return public(config, record, status='unknown', code='SUBMISSION_STATUS_UNKNOWN')
            return public(config, record, status='stopped', code='SCHEDULER_JOB_ENDED')
        if observed['schedulerState'] not in ACTIVE_SLURM:
            return public(config, record, status='stopped', **observed)
        if observed['schedulerState'] in STARTING_SLURM:
            return public(config, record, status='pending', **observed)
        if observed['schedulerState'] != 'RUNNING':
            return public(config, record, status='starting', **observed)
        # Use the holder's successful local health probe only while fresh. A
        # login host cannot directly probe the private compute-node loopback.
        if time.time() - record.get('heartbeatAt', 0) > 15:
            return public(config, record, status='starting', code='HEALTH_CONFIRMATION_PENDING', **observed)
        return public(config, record, **observed)
    if record.get('host') and record['host'] != socket.gethostname():
        return public(config, record, status='unknown', code='RUN_ON_SERVICE_HOST_REQUIRED')
    if not process_alive(record):
        if record.get('status') == 'launching' and time.time() - record.get('startedAt', 0) < 30:
            return public(config, record, status='starting')
        return public(config, record, status='stopped', code='SERVICE_PROCESS_ENDED')
    if time.time() - record.get('heartbeatAt', 0) > 15:
        return public(config, record, status='starting', code='HEALTH_CONFIRMATION_PENDING')
    return public(config, record)


def discover(config):
    with locked(config):
        return inspect(config, read_record(config))


def submission_command(config, record):
    slurm = config.get('slurm', {})
    command = [slurm.get('sbatch', 'sbatch'), '--parsable', '--nodes=1', '--ntasks=1', '--export=NONE',
               '--job-name=' + record['jobName'], '--output=' + str(Path(config['stateRoot']) / 'logs' / '%j.out'),
               '--error=' + str(Path(config['stateRoot']) / 'logs' / '%j.err')]
    for key, option in (('partition', 'partition'), ('account', 'account'), ('qos', 'qos'), ('constraint', 'constraint'),
                        ('time', 'time'), ('cpus', 'cpus-per-task'), ('memory', 'mem')):
        if key in slurm:
            command.append('--' + option + '=' + str(slurm[key]))
    runner = [sys.executable, str(Path(__file__).resolve()), 'run', '--config', config['configPath'], '--instance', record['instanceId']]
    # sbatch --wrap invokes a POSIX shell. Quote each fixed/operator argument;
    # never interpolate raw paths, configuration strings or GUI shell fragments.
    return command + ['--wrap=exec ' + shlex.join(runner)]


def ensure(config):
    with locked(config):
        previous = read_record(config)
        existing = inspect(config, previous)
        if existing['status'] not in ('stopped', 'failed'):
            return {**existing, 'reused': True}
        if previous and config['mode'] == 'local' and process_alive(previous):
            return {**existing, 'reused': True}
        if backend_lease_held(config):
            return public(config, previous, status='unknown', code='EXISTING_BACKEND_LIFETIME_LOCKED', reused=True)
        # A failed health check or slow launch does not authorize a new job.
        if previous and config['mode'] == 'slurm' and existing['status'] == 'failed' and queue(config, previous):
            return {**existing, 'reused': True}
        instance = uuid.uuid4().hex
        record = dict(schema=SCHEMA, serviceId=config['serviceId'], serverId=config['serverId'],
                      instanceId=instance, binding=config['binding'], mode=config['mode'], startedAt=time.time(),
                      status='submission-unknown' if config['mode'] == 'slurm' else 'launching',
                      jobName='bm-' + config['serviceId'][:18] + '-' + instance[:12])
        # Persist intent before either operation, including a submission timeout.
        write_record(config, record)
        if config['mode'] == 'slurm':
            try:
                result = subprocess.run(submission_command(config, record), capture_output=True, text=True, timeout=20, check=False)
                if result.returncode:
                    # A nonzero scheduler reply may still follow an uncertain
                    # submission. Keep the intent; discovery must settle it.
                    record.update(status='submission-unknown', code='SLURM_SUBMISSION_UNCONFIRMED')
                else:
                    value = result.stdout.strip()
                    require(re.fullmatch(r'[0-9]+(?:;[A-Za-z0-9._-]+)?', value), 'SLURM_SUBMISSION_UNCONFIRMED')
                    record.update(jobId=value.split(';', 1)[0], status='pending')
            except (OSError, subprocess.TimeoutExpired, Refused):
                record.update(status='submission-unknown', code='SLURM_SUBMISSION_UNCONFIRMED')
        else:
            log_path = Path(config['stateRoot']) / 'logs' / (instance + '.holder.log')
            with log_path.open('xb') as log:
                os.chmod(log_path, 0o600)
                try:
                    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run', '--config', config['configPath'],
                        '--instance', instance], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        cwd=config['cwd'], start_new_session=True, close_fds=True)
                    record.update(pid=process.pid, startTicks=ticks(process.pid), host=socket.gethostname(), status='starting')
                except OSError:
                    record.update(status='failed', code='SERVICE_LAUNCH_FAILED')
        write_record(config, record)
        return public(config, record, status='unknown' if record['status'] == 'submission-unknown' else record['status'], reused=False)


def environment(config, instance):
    # Keep HOME/session/registry paths exactly as selected by the BioMind
    # installation. Do not create another agentDir, history or memory tree.
    env = dict(os.environ)
    if config.get('environmentFile'):
        source = json.loads(Path(config['environmentFile']).read_text())
        require(isinstance(source, dict) and all(isinstance(k, str) and isinstance(v, str) and '\x00' not in k + v
                and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) for k, v in source.items()), 'INVALID_ENVIRONMENT_FILE')
        env.update(source)
    env.update(config.get('environment', {}))
    env.update(PI_STUDIO_AGENT_PROFILE='biomind', BIOMIND_SERVER_ID=config['serverId'],
               BIOMIND_SERVICE_INSTANCE_ID=instance,
               BIOMIND_SERVICE_ENDPOINT_FILE=str(Path(config['stateRoot']) / (instance + '.endpoint.json')),
               NEXT_TELEMETRY_DISABLED='1')
    return env


def health(config, instance):
    endpoint_file = Path(config['stateRoot']) / (instance + '.endpoint.json')
    if not endpoint_file.exists():
        return None
    private(endpoint_file)
    endpoint = json.loads(endpoint_file.read_text())
    require(endpoint.get('schema') == 'BioMindStudioEndpointV1' and endpoint.get('instanceId') == instance
            and endpoint.get('serverId') == config['serverId'] and endpoint.get('address') == '127.0.0.1'
            and type(endpoint.get('port')) is int and 0 < endpoint['port'] <= 65535, 'ENDPOINT_IDENTITY_MISMATCH')
    # Avoid HTTP proxy environment variables for this private loopback probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:' + str(endpoint['port']) + '/_biomind_studio/health', timeout=3) as response:
        data = response.read(65537)
        require(response.status == 200 and len(data) <= 65536, 'SERVICE_NOT_READY')
        value = json.loads(data)
    require(value.get('schema') == 'BioMindStudioHealthV1' and value.get('instanceId') == instance
            and value.get('serverId') == config['serverId'] and value.get('profile') == 'biomind'
            and value.get('status') == 'ready', 'HEALTH_IDENTITY_MISMATCH')
    return {**endpoint, 'idleStopProtocol': value.get('idleStopProtocol')}


def stop(config, instance, timeout=45):
    """Ask this exact backend to drain idle owners; never signal or forge state."""
    require(instance and re.fullmatch(r'[a-f0-9]{32}', instance), 'EXPLICIT_INSTANCE_REQUIRED')
    with locked(config):
        record = read_record(config)
        require(record and record.get('instanceId') == instance, 'SERVICE_INSTANCE_REPLACED')
        require(record.get('binding') == config['binding'], 'ACTIVE_CONFIG_BINDING_CHANGED')
        require(record.get('host') == socket.gethostname(), 'RUN_ON_SERVICE_HOST_REQUIRED')
        require(process_alive(record), 'SERVICE_PROCESS_IDENTITY_UNCONFIRMED')
        endpoint = health(config, instance)
        require(endpoint and endpoint.get('idleStopProtocol') == 1, 'IDLE_STOP_PROTOCOL_UNAVAILABLE')
        password = environment(config, instance).get('PI_WEB_PASSWORD')
        require(isinstance(password, str) and password, 'SERVICE_AUTH_REQUIRED')
    # The holder needs service.lock while the backend drains. Do not hold it
    # across the HTTP request or while waiting for its genuine stopped record.
    request = urllib.request.Request('http://127.0.0.1:' + str(endpoint['port']) + '/_biomind_studio/stop-idle',
        data=b'', method='POST', headers={'Authorization': 'Basic ' + base64.b64encode(('pi:' + password).encode()).decode(),
                                       'X-BioMind-Instance': instance})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise Refused('SERVICE_STOP_REDIRECT_REFUSED')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(65537)
            require(response.status == 200 and len(raw) <= 65536, 'SERVICE_STOP_UNCONFIRMED')
            value = json.loads(raw)
    except urllib.error.HTTPError as error:
        raise Refused('SERVICE_BUSY' if error.code == 409 else 'SERVICE_STOP_AUTH_OR_PROTOCOL_REJECTED') from None
    require(value.get('schema') == 'BioMindIdleStopV1' and value.get('serverId') == config['serverId']
            and value.get('instanceId') == instance and value.get('status') == 'stopping', 'SERVICE_STOP_IDENTITY_MISMATCH')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with locked(config):
            current = read_record(config)
            require(current and current.get('instanceId') == instance, 'SERVICE_INSTANCE_REPLACED')
            if current.get('status') == 'stopped' and not process_alive(current) and not backend_lease_held(config):
                return public(config, current, status='stopped', signalsSent=False, nativeIdleShutdown=True)
        time.sleep(.1)
    raise Refused('SERVICE_STOP_UNCONFIRMED')


def run(config, instance):
    require(instance and re.fullmatch(r'[a-f0-9]{32}', instance), 'INVALID_INSTANCE')
    with locked(config):
        record = read_record(config)
        require(record and record.get('instanceId') == instance and record.get('binding') == config['binding'], 'SERVICE_INSTANCE_REPLACED')
        if config['mode'] == 'slurm':
            job_id = os.environ.get('SLURM_JOB_ID', '')
            require(re.fullmatch(r'[0-9]+', job_id) and (not record.get('jobId') or record['jobId'] == job_id), 'SLURM_JOB_REQUIRED')
            record['jobId'] = job_id
        if record.get('holderStarted'):
            # A second run command must never become a second backend owner.
            raise Refused('SERVICE_HOLDER_ALREADY_STARTED')
        record.update(pid=os.getpid(), startTicks=ticks(os.getpid()), host=socket.gethostname(),
                      status='starting', holderStarted=True, heartbeatAt=time.time())
        write_record(config, record)
    env = environment(config, instance)
    try:
        lease_fd = backend_lease(config)
    except BlockingIOError:
        raise Refused('EXISTING_BACKEND_LIFETIME_LOCKED')
    log_path = Path(config['stateRoot']) / 'logs' / (instance + '.backend.log')
    try:
        with log_path.open('xb') as log:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen(config['command'], cwd=config['cwd'], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, pass_fds=(lease_fd,))
    except OSError:
        os.close(lease_fd)
        with locked(config):
            record.update(status='failed', code='BACKEND_LAUNCH_FAILED', heartbeatAt=time.time())
            write_record(config, record)
        return 1
    stopping = False
    def terminate(_signum, _frame):
        nonlocal stopping
        stopping = True
        # Only forward actual scheduler/operator signals to this owned process.
        # SSH disconnect, polling timeout and desktop exit never reach this path.
        if process.poll() is None:
            process.terminate()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    started = time.monotonic()
    while process.poll() is None:
        status = 'starting' if time.monotonic() - started < config['startupTimeoutSeconds'] else 'slow-start'
        code = None
        try:
            endpoint = health(config, instance)
            if endpoint:
                record['port'] = endpoint['port']
                status = 'ready'
        except (OSError, ValueError, Refused):
            status = 'unhealthy' if record.get('port') else status
            code = 'HEALTH_CHECK_FAILED'
        with locked(config):
            latest = read_record(config)
            require(latest and latest.get('instanceId') == instance, 'SERVICE_INSTANCE_REPLACED')
            record.update(status='stopping' if stopping else status, heartbeatAt=time.time())
            record.pop('code', None)
            if code:
                record['code'] = code
            write_record(config, record)
        time.sleep(2)
    with locked(config):
        record.update(status='stopped' if stopping or process.returncode == 0 else 'failed', heartbeatAt=time.time(),
                      code='BACKEND_EXITED', exitCode=process.returncode)
        write_record(config, record)
    os.close(lease_fd)
    return process.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('ensure', 'discover', 'run', 'stop', 'stop-legacy-idle'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--instance')
    parser.add_argument('--maintenance-core')
    parser.add_argument('--maintenance-plan')
    parser.add_argument('--clients-quiescent', action='store_true',
                        help='Operator confirms every known target client sends no new requests during maintenance')
    parser.add_argument('--idle-confirmed', action='store_true',
                        help='Operator confirms all target scientific tasks have ended')
    args = parser.parse_args()
    try:
        config = load(args.config)
        if args.action == 'run':
            return run(config, args.instance)
        if args.action == 'stop-legacy-idle':
            from types import SimpleNamespace
            from legacy_idle_stop import stop_legacy_idle
            require(args.maintenance_core and args.maintenance_plan, 'MAINTENANCE_RECEIPT_REQUIRED')
            result = stop_legacy_idle(config, args.instance, args.maintenance_core, args.maintenance_plan,
                clients_quiescent=args.clients_quiescent, idle_confirmed=args.idle_confirmed,
                service=SimpleNamespace(**globals()))
        else:
            require(not any((args.maintenance_core, args.maintenance_plan, args.clients_quiescent, args.idle_confirmed)),
                    'LEGACY_OPTIONS_REQUIRE_EXPLICIT_COMMAND')
            result = stop(config, args.instance) if args.action == 'stop' else ensure(config) if args.action == 'ensure' else discover(config)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Refused as error:
        print(json.dumps(dict(status='error', code=str(error), automaticReplay=False)), flush=True)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        # Never return credential file contents, operator command diagnostics,
        # raw scheduler output or arbitrary exceptions to the Windows client.
        print(json.dumps(dict(status='error', code='SERVICE_CONFIGURATION_OR_STATE_INVALID', automaticReplay=False)), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

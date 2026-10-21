"""Reuse native idle-stop, locked core rebind and ensure; preserve Studio resources."""
from pathlib import Path
import argparse
import datetime
import json
import socket
import subprocess
import sys
import time

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Startup_Production_20261021'
E = O / 'evidence'
SERVICE = B / 'BioMind_Unified_Core_20261009/delivery/service/plugin-lifecycle/59f269bcd8027d40b8685feffd14a656176d5817/server/server-service.py'
CONFIG = B / 'BioMind_Studio_Workbench_20261009/deployment/private/service.json'


def run(label, args, timeout=180):
    result = subprocess.run([sys.executable, '-B', *map(str, args)], capture_output=True,
                            text=True, timeout=timeout)
    (E / (label + '.stdout')).write_text(result.stdout)
    (E / (label + '.stderr')).write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(label + '_REFUSED_SEE_EVIDENCE')
    return json.loads(result.stdout)


def host_preflight(config):
    # A fresh shared heartbeat is not a dead service merely because the current
    # execution sandbox cannot see its PID. Never retire that lifecycle record.
    path = Path(config['stateRoot']) / 'service.json'
    record = json.loads(path.read_text()) if path.exists() else {}
    if record.get('host') and record['host'] != socket.gethostname():
        raise RuntimeError('RUN_ON_ACTUAL_SERVICE_HOST')
    if record.get('status') not in (None, 'stopped', 'failed'):
        try:
            fields = Path('/proc/' + str(record['pid']) + '/stat').read_text().rsplit(')', 1)[1].split()
            visible = fields[0] != 'Z' and fields[19] == record.get('startTicks')
        except (OSError, KeyError):
            visible = False
        if not visible:
            raise RuntimeError('LIVE_SERVICE_PID_NOT_VERIFIABLE_IN_THIS_EXECUTION_ENVIRONMENT')
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.close()
    except OSError:
        raise RuntimeError('LOCAL_NETWORK_ACCESS_REQUIRED') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    build = json.loads((O / 'build-result.json').read_text())
    release = Path(build['root'])
    backend_release = json.loads((O / 'backend-result.json').read_text())
    assert backend_release['status'] == 'PASS'
    backend = Path(backend_release['root'])
    config = json.loads(CONFIG.read_text())
    runtime = json.loads(Path(config['environment']['BIOMIND_RUNTIME_CONFIG']).read_text())
    host_preflight(config)
    preflight_file = E / 'studio-host-preflight.json'
    if preflight_file.exists() and not (E / 'studio-host-preflight-before-unblocking.json').exists():
        (E / 'studio-host-preflight-before-unblocking.json').write_bytes(preflight_file.read_bytes())
    preflight_file.write_text(json.dumps({'status': 'PASS', 'host': socket.gethostname(),
        'checkedAt': datetime.datetime.now(datetime.timezone.utc).isoformat()}, indent=2) + '\n')
    receipt_file = O / 'studio-deployment.json'
    if receipt_file.exists() and json.loads(receipt_file.read_text()).get('status') == 'DEPLOYED':
        assert config['environment']['BIOMIND_ENTRYPOINT'] == str(release / 'bio_agents.py')
        assert config['cwd'] == str(backend)
        print(json.dumps({'status': 'ALREADY_DEPLOYED', 'build_id': build['releaseId']}))
        return
    current = run('studio-discover-before', [SERVICE, 'discover', '--config', CONFIG])
    assert current['status'] in ('ready', 'stopped'), 'STUDIO_NOT_READY_OR_STOPPED'
    delivery = B / 'BioMind_Unified_Core_20261009/delivery/startup-production-20261021' / build['releaseId'][:16]
    delivery.mkdir(parents=True, exist_ok=True)
    plan = {'status': 'PREPARED', 'release': str(release), 'build_id': build['releaseId'],
            'old_entrypoint': config['environment']['BIOMIND_ENTRYPOINT'],
            'old_backend': config['cwd'], 'backend': str(backend),
            'backend_source_commit': backend_release['sourceCommit'],
            'agentDir': runtime['agentDir'], 'sessionRoot': config['environment']['BIOMIND_SESSION_ROOT'],
            'runtime_output': str(delivery / 'workbench-shared-web-runtime.json'),
            'native_idle_stop_required': current['status'] == 'ready'}
    (O / 'studio-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False))
        return
    native_stopped = False
    if current['status'] == 'ready':
        stopped = run('studio-native-idle-stop', [SERVICE, 'stop', '--config', CONFIG,
                                                '--instance', current['instanceId']])
        assert stopped['status'] == 'stopped' and stopped.get('nativeIdleShutdown') is True
        native_stopped = True
    argv = [O / 'scripts/rebind-managed-services.py', '--service-config', CONFIG,
            '--release', release, '--backend-root', backend,
            '--runtime-output', plan['runtime_output']]
    try:
        run('studio-rebind-check', [*argv, '--receipt', O / 'studio-rebind-check.json'])
        run('studio-rebind', [*argv, '--receipt', O / 'studio-rebind.json', '--apply'])
    except BaseException:
        # Do not forge state or bypass locks. Existing ensure checks the binding.
        if native_stopped:
            run('studio-ensure-after-refusal', [SERVICE, 'ensure', '--config', CONFIG])
        raise
    run('studio-ensure', [SERVICE, 'ensure', '--config', CONFIG])
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = run('studio-discover-after', [SERVICE, 'discover', '--config', CONFIG])
        if current['status'] == 'ready':
            break
        if current['status'] in ('failed', 'stopped', 'configuration-changed', 'unknown'):
            raise RuntimeError('NEW_STUDIO_NOT_READY')
        time.sleep(2)
    assert current['status'] == 'ready', 'STUDIO_START_TIMEOUT'
    actual = json.loads(CONFIG.read_text())
    new_runtime = json.loads(Path(actual['environment']['BIOMIND_RUNTIME_CONFIG']).read_text())
    assert actual['environment']['BIOMIND_ENTRYPOINT'] == str(release / 'bio_agents.py')
    assert actual['cwd'] == str(backend)
    assert actual['environment']['BIOMIND_SESSION_ROOT'] == plan['sessionRoot']
    assert new_runtime['agentDir'] == plan['agentDir']
    receipt = {**plan, 'status': 'DEPLOYED', 'discovery': current,
               'source_commit': json.loads((release / 'RELEASE.json').read_text())['sourceCommit'],
               'native_idle_shutdown': native_stopped, 'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'user_tasks_cancelled': False, 'settings_changed': False,
               'rebind_receipt': str(O / 'studio-rebind.json')}
    receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': receipt['status'], 'instanceId': current['instanceId'],
                      'build_id': build['releaseId']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        result = {'status': 'BLOCKED', 'error_type': type(error).__name__,
                  'code': str(error) if isinstance(error, (RuntimeError, AssertionError)) else 'CHECK_SAVED_EVIDENCE',
                  'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        (E / 'studio-deploy-blocked.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(2)

"""Native idle-stop, guarded resource rebind, ensure; no signals or job changes."""
from pathlib import Path
import json
import subprocess
import sys
import time

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_20261020/shared-plugin'
S = B / 'BioMind_Production_20261020/scripts'
SERVICE = B / 'BioMind_Unified_Core_20261009/delivery/service/plugin-lifecycle/59f269bcd8027d40b8685feffd14a656176d5817/server/server-service.py'
CONFIG = B / 'BioMind_Studio_Workbench_20261009/deployment/private/service.json'

def run(label, argv, timeout=120):
    r = subprocess.run([sys.executable, '-B', *map(str, argv)], capture_output=True, text=True, timeout=timeout)
    (O / (label + '.stdout')).write_text(r.stdout)
    (O / (label + '.stderr')).write_text(r.stderr)
    if r.returncode:
        raise RuntimeError(label + ' refused; see saved logs')
    return json.loads(r.stdout)

def main():
    prep = json.loads((O / 'preparation.json').read_text())
    current = run('studio-discover-before', [SERVICE, 'discover', '--config', CONFIG])
    assert current['status'] == 'ready'
    stopped = run('studio-idle-stop', [SERVICE, 'stop', '--config', CONFIG, '--instance', current['instanceId']])
    assert stopped['status'] == 'stopped' and stopped['nativeIdleShutdown'] is True
    release = Path(prep['core'])
    output = B / 'BioMind_Unified_Core_20261009/delivery/submission-20261020/d0cf979998965759/workbench-shared-web-runtime.json'
    argv = [S / 'rebind-shared-agent.py', '--service-config', CONFIG, '--release', release,
            '--backend-root', B / 'BioMind_Unified_Core_20261009/studio/backend/pi-studio',
            '--runtime-output', output, '--new-agent-dir', prep['newAgentDir']]
    try:
        run('studio-rebind-check', [*argv, '--receipt', O / 'studio-rebind-check.json'])
        run('studio-rebind', [*argv, '--receipt', O / 'studio-rebind.json', '--apply'])
    except BaseException:
        # Restore availability with the current verified config; a refused rebind
        # does not publish it, and the inherited rebinder owns partial-state repair.
        run('studio-ensure-on-refusal', [SERVICE, 'ensure', '--config', CONFIG])
        raise
    run('studio-ensure', [SERVICE, 'ensure', '--config', CONFIG])
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        current = run('studio-discover-after', [SERVICE, 'discover', '--config', CONFIG])
        if current['status'] == 'ready':
            break
        if current['status'] in ('failed', 'stopped'):
            raise RuntimeError('Service did not become ready')
        time.sleep(2)
    assert current['status'] == 'ready'
    receipt = {'status': 'DEPLOYED', 'newAgentDir': prep['newAgentDir'],
               'sessionRoot': prep['sessionRoot'], 'discovery': current,
               'nativeIdleShutdown': True, 'signalsSent': False, 'slurmJobsModified': False}
    (O / 'studio-deployment.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': receipt['status'], 'instanceId': current['instanceId']}))

if __name__ == '__main__':
    main()

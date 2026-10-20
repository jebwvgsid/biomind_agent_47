"""Read-only checks against the actual managed service; never log auth."""
from pathlib import Path
import base64
import json
import subprocess
import sys
import urllib.parse
import urllib.request

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_20261020/shared-plugin'
CONFIG = B / 'BioMind_Studio_Workbench_20261009/deployment/private/service.json'
SERVICE = B / 'BioMind_Unified_Core_20261009/delivery/service/plugin-lifecycle/59f269bcd8027d40b8685feffd14a656176d5817/server/server-service.py'
PROJECT = B / 'benchmarks/02.测试26.9/05.作物育种与多组学/04.BM-REMAIN-CROP-ADVGP-010__strict'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Unexpected service redirect')

def main():
    discovery = json.loads(subprocess.check_output([sys.executable, '-B', str(SERVICE), 'discover', '--config', str(CONFIG)], text=True))
    assert discovery['status'] == 'ready'
    config = json.loads(CONFIG.read_text())
    password = json.loads(Path(config['environmentFile']).read_text())['PI_WEB_PASSWORD']
    auth = 'Basic ' + base64.b64encode(('pi:' + password).encode()).decode()
    del password
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    responses = {}
    for key, route in [('health', '/_biomind_studio/health'), ('profile', '/api/agent/profile'),
                       ('running', '/api/agent/running'),
                       ('plugins', '/api/plugins?' + urllib.parse.urlencode({'cwd': str(PROJECT)}))]:
        req = urllib.request.Request('http://127.0.0.1:' + str(discovery['port']) + route,
                                     headers={'Authorization': auth})
        with client.open(req, timeout=90) as response:
            responses[key] = json.load(response)
    del auth
    plugins = responses['plugins']
    # Native package protocol returns only public package/runtime metadata.
    assert plugins.get('schema') == 'BioMindPluginsV1' and plugins.get('status') == 'ok'
    context = plugins.get('context', {})
    packages = plugins.get('packages', [])
    prep = json.loads((O / 'preparation.json').read_text())
    expected = json.loads((B / 'BioMind_Production_20261020/build-result.json').read_text())
    checks = {
        'serviceReady': discovery['status'] == 'ready',
        'serverId': plugins.get('serverId') == discovery['serverId'],
        'agentDir': context.get('agentDir') == prep['newAgentDir'],
        'packagePresent': any('pi-web-access' in json.dumps(p) for p in packages),
        'historyRootPreserved': config['environment']['BIOMIND_SESSION_ROOT'] == prep['sessionRoot'],
        'runtimeAgentDir': json.loads(Path(config['environment']['BIOMIND_RUNTIME_CONFIG']).read_text())['agentDir'] == prep['newAgentDir'],
        'sameCore': str(Path(expected['root']) / 'bio_agents.py') == config['environment']['BIOMIND_ENTRYPOINT'],
    }
    result = {'status': 'PASS' if all(checks.values()) else 'FAIL', 'checks': checks,
              'discovery': discovery, 'context': context, 'packages': packages,
              'profile': responses['profile'], 'health': responses['health'],
              'running': responses['running'], 'credentialContentsLogged': False,
              'requestMode': 'read-only', 'ownerMainExecution': False}
    (O / 'studio-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'checks': checks, 'contextFields': list(context)}))
    raise SystemExit(result['status'] != 'PASS')

if __name__ == '__main__':
    main()

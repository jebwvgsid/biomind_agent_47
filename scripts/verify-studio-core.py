#!/usr/bin/env python3
"""Read-only authenticated server check; never starts owners, installs or prompts.

Run on the service host. A health response/Windows version alone is insufficient:
the existing /api/plugins list must report this exact core and resource context.
"""
import argparse
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core.pi_plugin_guard import private_json


def validate(expected, config, runtime, discovery, health, profile, packages):
    checks = {
        'serviceReady': discovery.get('status') == 'ready',
        'healthIdentity': health.get('status') == 'ready' and health.get('instanceId') == discovery.get('instanceId')
                          and health.get('serverId') == config['serverId'],
        'bioMindProfile': profile.get('profile') == 'biomind' and profile.get('serverId') == config['serverId'],
        'nativePackageProtocol': packages.get('schema') == 'BioMindPluginsV1' and packages.get('status') == 'ok'
                                 and packages.get('serverId') == config['serverId'],
    }
    context = packages.get('context') or {}
    checks.update({
        'coreBuild': context.get('buildId') == expected['releaseId'],
        'coreCommit': context.get('sourceCommit') == expected['sourceCommit'],
        'piPinned': context.get('piVersion') == '0.87.1',
        'mainEntrypoint': str(Path(context.get('releaseRoot', '/missing')) / 'bio_agents.py') == config['environment']['BIOMIND_ENTRYPOINT'],
        'agentDir': context.get('agentDir') == str(Path(runtime['agentDir']).resolve()),
    })
    return {'schema': 'BioMindConnectedCoreCheckV1', 'result': 'PASS' if all(checks.values()) else 'FAIL',
            'checks': checks, 'serverId': config['serverId'], 'instanceId': discovery.get('instanceId'),
            'core': {k: context.get(k) for k in ('buildId', 'sourceCommit', 'releaseRoot', 'piVersion', 'agentDir', 'cwd')},
            'ownerMainExecution': 'NOT_RUN', 'realModel': 'NOT_RUN', 'windowsInteraction': 'NOT_RUN',
            'readOnly': True, 'automaticReplay': False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise ValueError('UNEXPECTED_REDIRECT')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--project', required=True, type=Path)
    args = parser.parse_args()
    config = private_json(args.config)
    expected = json.loads((args.release / 'RELEASE.json').read_text())
    runtime = json.loads(Path(config['environment']['BIOMIND_RUNTIME_CONFIG']).read_text())
    result = subprocess.run([sys.executable, '-B', str(args.service), 'discover', '--config', str(args.config)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode: raise ValueError('DISCOVERY_FAILED')
    discovery = json.loads(result.stdout)
    if discovery.get('status') != 'ready': raise ValueError('SERVICE_NOT_READY')
    if discovery.get('host') != socket.gethostname(): raise ValueError('RUN_ON_ACTUAL_SERVICE_HOST')
    port = discovery.get('port')
    if type(port) is not int or not 1 <= port <= 65535: raise ValueError('INVALID_SERVICE_PORT')
    # Dedicated workbench credential, never API/model credentials or .env files.
    secrets = private_json(config['environmentFile'])
    if set(secrets) != {'PI_WEB_PASSWORD'}: raise ValueError('INVALID_WORKBENCH_CREDENTIAL_FILE')
    authorization = 'Basic ' + base64.b64encode(('pi:' + secrets['PI_WEB_PASSWORD']).encode()).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def get(route):
        request = urllib.request.Request('http://127.0.0.1:' + str(port) + route, headers={'Authorization': authorization})
        with opener.open(request, timeout=60) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024: raise ValueError('RESPONSE_TOO_LARGE')
            return json.loads(raw)
    receipt = validate(expected, config, runtime, discovery, get('/_biomind_studio/health'),
                       get('/api/agent/profile'), get('/api/plugins?' + urllib.parse.urlencode({'cwd': str(args.project.resolve())})))
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt['result'] == 'PASS' else 1


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({'schema': 'BioMindConnectedCoreCheckV1', 'result': 'BLOCKED', 'code': 'CONNECTED_CORE_NOT_VERIFIED',
                          'readOnly': True, 'realModel': 'NOT_RUN', 'windowsInteraction': 'NOT_RUN'}))
        raise SystemExit(2)

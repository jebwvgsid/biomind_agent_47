"""Verify Studio's fixed core and an actual Main owner, then remove the load-only session."""
from pathlib import Path
import base64
import json
import shutil
import sys
import time
import urllib.request

from deploy_studio import B, O, E, SERVICE, CONFIG, run, host_preflight


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('UNEXPECTED_REDIRECT')


def main():
    config = json.loads(CONFIG.read_text())
    host_preflight(config)
    build = json.loads((O / 'build-result.json').read_text())
    release = Path(build['root'])
    project = B / 'benchmarks/.biomind-production-load-check-20261021'
    roots = [Path(p).resolve() for p in json.loads(config['environment']['BIOMIND_PROJECT_ROOTS_JSON'])]
    assert any(project.resolve().is_relative_to(root) for root in roots), 'ACCEPTANCE_PROJECT_NOT_AUTHORIZED'
    project.mkdir(mode=0o700, exist_ok=False)
    connected = run('studio-connected-core', [release / 'scripts/verify-studio-core.py',
                    '--service', SERVICE, '--config', CONFIG, '--release', release, '--project', project])
    assert connected['result'] == 'PASS'
    discovery = run('studio-owner-discover', [SERVICE, 'discover', '--config', CONFIG])
    secret = json.loads(Path(config['environmentFile']).read_text())
    assert set(secret) == {'PI_WEB_PASSWORD'}
    auth = 'Basic ' + base64.b64encode(('pi:' + secret['PI_WEB_PASSWORD']).encode()).decode()
    del secret
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(route, value=None):
        headers = {'Authorization': auth}
        data = None
        if value is not None:
            data = json.dumps(value).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request('http://127.0.0.1:' + str(discovery['port']) + route,
                                     data=data, headers=headers)
        with client.open(req, timeout=120) as response:
            return json.load(response)

    running = request('/api/agent/running')
    assert running.get('runningSessionIds') == [], 'STUDIO_BUSY_SKIP_LOAD_ONLY_ACCEPTANCE'
    result = {'status': 'FAIL', 'modelRequests': 0, 'formalAnalysisRuns': 0,
              'connected': connected, 'testProject': str(project), 'testSessionRemoved': False}
    state = None
    created = False
    try:
        new = request('/api/agent/new', {'type': 'ensure_session', 'cwd': str(project),
                                        'serverId': config['serverId']})
        assert new.get('success') is True
        created = True
        result['new'] = new
        scope = {k: new[k] for k in ['serverId', 'sessionId', 'ownerGeneration', 'project']}
        route = '/api/agent/' + new['sessionId']
        state = request(route, {'type': 'get_state', **scope})['data']
        result['state'] = state
        assert state['loaded_build']['buildId'] == build['releaseId'], 'STUDIO_OWNER_BUILD_MISMATCH'
        assert state['loaded_build']['releaseRoot'] == str(release), 'STUDIO_OWNER_ROOT_MISMATCH'
        assert state['active'] is False
        tools = request(route, {'type': 'get_tools', **scope})['data']
        result['tools'] = tools
        names = {t['name'] for t in tools['data']['registered']}
        required = {'run_command', 'inspect_run', 'wait_run', 'cancel_run',
                    'fetch_content', 'web_search', 'get_search_content'}
        assert required <= names, 'STUDIO_TOOLS_MISSING'
        result['requiredTools'] = sorted(required)
        result['status'] = 'PASS'
    finally:
        if created:
            # Native stop refuses active work and closes owners without signals.
            stopped = run('studio-owner-test-idle-stop', [SERVICE, 'stop', '--config', CONFIG,
                                                         '--instance', discovery['instanceId']])
            assert stopped['nativeIdleShutdown'] is True
            if state and state.get('sessionFile'):
                source = Path(state['sessionFile'])
                runtime = json.loads(Path(config['environment']['BIOMIND_RUNTIME_CONFIG']).read_text())
                session_root = Path(runtime.get('sessionDir') or Path(runtime['agentDir']) / 'sessions').resolve()
                assert source.resolve().is_relative_to(session_root)
                if source.is_file():
                    with source.open() as stream:
                        header = json.loads(stream.readline())
                    assert header['id'] == result['new']['sessionId'] and header['cwd'] == str(project)
                    removed = E / 'removed-studio-test-session'
                    removed.mkdir(exist_ok=True)
                    target = removed / source.name
                    assert not target.exists()
                    manifest = {'source': str(source), 'target': str(target), 'sessionId': header['id'],
                                'project': str(project), 'firstMessage': None, 'modelRequests': 0}
                    (removed / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
                    shutil.move(source, target)
            result['testSessionRemoved'] = state is not None
            if result['testSessionRemoved']:
                archived_project = E / 'studio-load-project'
                assert not archived_project.exists()
                shutil.move(project, archived_project)
                result['archivedTestProject'] = str(archived_project)
            run('studio-after-test-ensure', [SERVICE, 'ensure', '--config', CONFIG])
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                latest = run('studio-after-test-discover', [SERVICE, 'discover', '--config', CONFIG])
                if latest['status'] == 'ready':
                    result['finalDiscovery'] = latest
                    break
                if latest['status'] in ('failed', 'stopped', 'unknown', 'configuration-changed'):
                    raise RuntimeError('STUDIO_NOT_READY_AFTER_TEST')
                time.sleep(2)
            assert result.get('finalDiscovery', {}).get('status') == 'ready'
        (O / 'studio-owner-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'build_id': build['releaseId'],
                      'testSessionRemoved': result['testSessionRemoved'], 'modelRequests': 0}))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Bounded real HTTP acceptance, with durable no-replay checkpoints.

Default is read-only identity verification. --sessions creates two native owners
only inside --isolation-root. --real-model and --real-search explicitly submit
one task each. An existing receipt can ONLY be opened with --resume, which never
repeats a POST. A failed/ambiguous POST is preserved for inspection, not retried.
The private service environment file supplies workbench authentication; values
are never logged. The checker does not load dotenv or provider auth files.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class CheckError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def private_json(path):
    path = Path(path)
    st = path.stat()
    if path.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise CheckError('PRIVATE_FILE_PERMISSIONS')
    return json.loads(path.read_text())


def under(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if path.resolve() != path or root.resolve() != root or root not in path.parents:
        raise CheckError('ISOLATION_PATH_OUTSIDE_ROOT_OR_SYMLINK')
    return str(path)


def validate_isolation(config, runtime, project, root):
    # Write-capable state must not refer to production, even through a symlink.
    for path in (runtime['agentDir'], project, config['stateRoot'], config['environmentFile'],
                 config['environment']['BIOMIND_RUNTIME_CONFIG']):
        under(path, root)
    for name in ('dataRoot', 'memoryRoot', 'wikiRoot', 'archivePath', 'registryPath', 'preferencesPath', 'sessionDir'):
        if runtime.get(name):
            path = Path(runtime[name])
            under(path if path.is_absolute() else Path(project) / path, root)
    if runtime.get('sessionMode') != 'persistent':
        raise CheckError('PERSISTENT_NATIVE_SESSIONS_REQUIRED')
    return True


def worker_identity(state, expected, release, agent_dir, project, session_id):
    build = state.get('loaded_build') or {}
    ownership = state.get('resourceOwnership') or {}
    checks = {
        'sessionId': state.get('sessionId') == session_id,
        'project': state.get('cwd') == project,
        'coreBuild': build.get('releaseId') == expected['releaseId'],
        'coreCommit': build.get('sourceCommit') == expected['sourceCommit'],
        'mainRoot': build.get('releaseRoot') == str(release),
        'jsonMain': build.get('entrypoint') == 'json',
        'piPinned': build.get('piVersion') == '0.87.1',
        'persistentSession': isinstance(state.get('sessionFile'), str) and bool(state['sessionFile']),
        'sessionInsideCandidateAgent': isinstance(state.get('sessionFile'), str)
          and Path(agent_dir) in Path(state['sessionFile']).parents,
    }
    return {'result': 'PASS' if all(checks.values()) else 'FAIL', 'checks': checks,
            'build': {k: build.get(k) for k in ('releaseId', 'sourceCommit', 'releaseRoot', 'piVersion', 'entrypoint')},
            'sessionId': session_id, 'ownerGeneration': state.get('ownerGeneration'),
            'sessionFile': state.get('sessionFile'), 'agentDir': agent_dir,
            'resourceOwnershipPresent': bool(ownership)}


def safe_code(error):
    if isinstance(error, CheckError):
        return error.code
    if isinstance(error, (socket.timeout, TimeoutError, subprocess.TimeoutExpired)):
        return 'TIMEOUT_OUTCOME_UNCONFIRMED'
    if isinstance(error, OSError) and error.errno in (1, 13):
        return 'SOCKET_OR_FILESYSTEM_PERMISSION_DENIED'
    return 'HTTP_OR_SERVICE_CHECK_FAILED'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CheckError('UNEXPECTED_REDIRECT')


class Client:
    def __init__(self, base, password, timeout=90):
        url = urllib.parse.urlsplit(base)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise CheckError('LOOPBACK_HTTP_BASE_REQUIRED')
        self.base, self.timeout = base.rstrip('/'), timeout
        self.auth = 'Basic ' + base64.b64encode(('pi:' + password).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, route, payload=None, timeout=None):
        if not route.startswith('/') or route.startswith('//'):
            raise CheckError('INVALID_ROUTE')
        req = urllib.request.Request(self.base + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': self.auth, 'Content-Type': 'application/json'})
        try:
            return self.opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as error:
            code = 'HTTP_' + str(error.code)
            try:
                body = json.loads(error.read(65536))
                if re.fullmatch('[A-Z_0-9]{1,100}', str(body.get('code', ''))):
                    code = str(body['code'])
            except (ValueError, OSError):
                pass
            raise CheckError(code) from None

    def json(self, route, payload=None):
        with self.open(route, payload) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise CheckError('RESPONSE_TOO_LARGE')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CheckError('OBJECT_RESPONSE_REQUIRED')
        return value


class Receipt:
    def __init__(self, path, resume=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if resume:
            self.data = private_json(self.path)
        else:
            # Claim the receipt before *any* mutation, preventing accidental rerun.
            with self.path.open('x') as handle:
                os.chmod(self.path, 0o600)
                json.dump({'schema': 'BioMindIsolatedHTTPAcceptanceV1'}, handle)
            self.data = {'schema': 'BioMindIsolatedHTTPAcceptanceV1', 'startedAt': time.time(),
                'result': 'BLOCKED', 'stages': {}, 'sessions': {}, 'automaticReplay': False,
                'windowsInteraction': 'NOT_RUN', 'productionChanged': False}
            self.save()

    def save(self):
        tmp = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def stage(self, name, value):
        self.data['stages'][name] = value
        self.save()


def scoped(session, project, server_id, command, **fields):
    return {'type': command, 'serverId': server_id, 'sessionId': session['id'],
            'ownerGeneration': session['ownerGeneration'], 'project': project, **fields}


def plugin_inventory(packages):
    rows = [{k: row.get(k) for k in ('source', 'scope', 'installedVersion', 'installed', 'declaration')}
            for row in packages.get('packages', [])]
    matches = [row for row in rows if 'pi-web-access' in str(row.get('source', ''))]
    valid = any(row.get('installed') is True and row.get('installedVersion') == '0.38.0' for row in matches)
    return {'result': 'PASS' if valid else 'FAIL', 'code': 'VERIFIED' if valid else 'PINNED_WEB_PLUGIN_NOT_INSTALLED',
            'packages': rows, 'workerLoaded': 'NOT_RUN'}


def tool_inventory(tools):
    required = {'web_search', 'fetch_content', 'get_search_content', 'source_check', 'web_enable'}
    registered = {v['name'] for v in tools.get('registered', []) if isinstance(v, dict) and isinstance(v.get('name'), str)}
    effective = set(tools.get('effective', []))
    return {'result': 'PASS' if required <= registered and 'web_search' in effective else 'FAIL',
            'registered': sorted(registered), 'effective': sorted(effective),
            'missingRegistered': sorted(required - registered), 'actualToolExecution': 'NOT_RUN'}


class Events:
    """Drain the real stream; retain only bounded, nonsecret proof fields."""
    def __init__(self, client, session_id, marker, deadline):
        self.client, self.session_id, self.marker = client, session_id, marker
        self.deadline, self.connected, self.done = deadline, threading.Event(), threading.Event()
        self.proof = {'eventCount': 0, 'toolCalls': [], 'assistantOutputTokens': 0, 'markerObserved': False}
        self.thread = threading.Thread(target=self.run, daemon=True)

    def observe(self, event):
        self.proof['eventCount'] += 1
        kind = event.get('type')
        if kind == 'connected':
            self.connected.set()
        if kind in ('message_end', 'message_update'):
            msg = event.get('message') or {}
            if msg.get('role') == 'assistant':
                usage = msg.get('usage') or {}
                self.proof['assistantOutputTokens'] = max(self.proof['assistantOutputTokens'], usage.get('output', 0) or 0)
                text = json.dumps(msg.get('content', []), ensure_ascii=False)
                self.proof['markerObserved'] |= self.marker in text
        if kind == 'tool_execution_start' and event.get('toolName') == 'web_search':
            self.proof['freshSearchMarkerObserved'] = self.marker in json.dumps(event.get('args', {}))
        if kind == 'tool_execution_end':
            result = event.get('result') or {}
            details = result.get('details') or {}
            serialized = json.dumps(result, sort_keys=True, ensure_ascii=False)
            # Origin only: never retain URL query credentials or private text.
            origins = set()
            for url in re.findall(r'https?://[^\s"<>\\]+', serialized):
                parsed = urllib.parse.urlsplit(url)
                if parsed.hostname:
                    origins.add(parsed.scheme + '://' + parsed.hostname)
            self.proof['toolCalls'].append({'name': event.get('toolName'), 'toolCallId': event.get('toolCallId'),
                'isError': event.get('isError') is True, 'resultSha256': hashlib.sha256(serialized.encode()).hexdigest(),
                'resultBytes': len(serialized.encode()), 'sourceOrigins': sorted(origins)[:30],
                'searchSucceeded': not details.get('error') and not details.get('cancelled')
                    and isinstance(details.get('successfulQueries'), int) and details['successfulQueries'] > 0
                    and isinstance(details.get('totalResults'), int) and details['totalResults'] > 0})
        if kind == 'extension_ui_request':
            self.proof['pendingUserInteraction'] = True
            self.done.set()
        if kind == 'biomind_result':
            self.proof['runtimeStatus'] = event.get('runtime_status')
            self.proof['requestId'] = event.get('requestId')
            self.proof['markerObserved'] |= self.marker in str(event.get('model_conclusion', ''))
            self.proof['completed'] = True
            self.done.set()

    def run(self):
        try:
            size = 0
            with self.client.open('/api/agent/' + urllib.parse.quote(self.session_id, safe='') + '/events?v2=1', timeout=self.deadline) as response:
                while not self.done.is_set() and size < 16 * 1024 * 1024:
                    line = response.readline(2 * 1024 * 1024 + 1)
                    size += len(line)
                    if not line:
                        break
                    if len(line) > 2 * 1024 * 1024:
                        raise CheckError('EVENT_FRAME_TOO_LARGE')
                    if line.startswith(b'data: '):
                        self.observe(json.loads(line[6:]))
            if not self.done.is_set():
                self.proof['streamCode'] = 'EVENT_STREAM_ENDED_OR_LIMIT'
        except (OSError, ValueError, CheckError) as error:
            self.proof['streamCode'] = safe_code(error)
        finally:
            self.done.set()


def prompt_once(client, receipt, stage, session, project, server_id, prompt, marker, deadline):
    events = Events(client, session['id'], marker, deadline)
    events.thread.start()
    if not events.connected.wait(min(30, deadline)):
        raise CheckError('EVENT_STREAM_NOT_CONNECTED_NO_TASK_SENT')
    message_id = 'accept-' + uuid.uuid4().hex
    # Durable pre-send checkpoint: uncertain delivery must never be replayed.
    receipt.stage(stage, {'result': 'BLOCKED', 'code': 'SUBMISSION_PENDING_OUTCOME_UNKNOWN',
        'sessionId': session['id'], 'clientMessageId': message_id, 'automaticReplay': False})
    client.json('/api/agent/' + urllib.parse.quote(session['id'], safe=''),
        scoped(session, project, server_id, 'prompt', message=prompt, clientMessageId=message_id))
    events.done.wait(deadline)
    proof = dict(events.proof)
    events.done.set()
    passed = proof.get('completed') and proof.get('runtimeStatus') == 'completed' and proof.get('assistantOutputTokens', 0) > 0
    if stage == 'realModel':
        passed = passed and proof.get('markerObserved') is True
    if stage == 'realSearch':
        passed = passed and proof.get('freshSearchMarkerObserved') is True and any(row['name'] == 'web_search' and not row['isError'] and row['searchSucceeded'] and row['sourceOrigins'] for row in proof['toolCalls'])
    result = {'result': 'PASS' if passed else 'BLOCKED', 'code': 'VERIFIED' if passed else 'TASK_OR_NETWORK_RESULT_NOT_VERIFIED',
        'sessionId': session['id'], 'clientMessageId': message_id, 'automaticReplay': False, **proof}
    receipt.stage(stage, result)
    return result


def run(args, receipt):
    config = private_json(args.config)
    runtime = json.loads(Path(config['environment']['BIOMIND_RUNTIME_CONFIG']).read_text())
    expected = json.loads((args.release / 'RELEASE.json').read_text())
    project = str(args.project.resolve())
    credential = private_json(config['environmentFile'])
    if not isinstance(credential.get('PI_WEB_PASSWORD'), str) or len(credential['PI_WEB_PASSWORD'].encode()) < 16:
        raise CheckError('INVALID_WORKBENCH_CREDENTIAL_FILE')
    if args.sessions or args.real_model or args.real_search:
        if not args.isolation_root:
            raise CheckError('ISOLATION_ROOT_REQUIRED_FOR_SESSION_CREATION')
        validate_isolation(config, runtime, project, args.isolation_root)
    found = subprocess.run([sys.executable, '-B', str(args.service), 'discover', '--config', str(args.config)],
        capture_output=True, text=True, timeout=30)
    if found.returncode:
        raise CheckError('SERVICE_DISCOVERY_FAILED')
    discovery = json.loads(found.stdout)
    receipt.stage('discovery', {k: discovery.get(k) for k in ('status', 'code', 'host', 'port', 'serverId', 'instanceId')})
    if discovery.get('status') != 'ready':
        raise CheckError('SERVICE_NOT_READY')
    if args.base_url is None and discovery.get('host') != socket.gethostname():
        raise CheckError('RUN_ON_SERVICE_HOST_OR_SUPPLY_LOCAL_TUNNEL_BASE_URL')
    client = Client(args.base_url or 'http://127.0.0.1:' + str(discovery['port']), credential['PI_WEB_PASSWORD'], args.timeout)
    spec = importlib.util.spec_from_file_location('verify_core', args.release / 'scripts/verify-studio-core.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    packages = client.json('/api/plugins?' + urllib.parse.urlencode({'cwd': project}))
    identity = verifier.validate(expected, config, runtime, discovery,
        client.json('/_biomind_studio/health'), client.json('/api/agent/profile'), packages)
    receipt.stage('httpIdentity', identity)
    if identity['result'] != 'PASS':
        raise CheckError('CONNECTED_CORE_IDENTITY_MISMATCH')
    receipt.data['serverId'] = config['serverId']
    receipt.data['instanceId'] = discovery['instanceId']
    receipt.data['project'] = project
    receipt.stage('nativePluginInventory', plugin_inventory(packages))
    if args.resume:
        # No task, session creation, resume_session, reload, or model POST here.
        states = {}
        for name, row in receipt.data['sessions'].items():
            value = client.json('/api/agent/' + urllib.parse.quote(row['id'], safe=''))
            state = value.get('state') or {}
            states[name] = {'running': value.get('running'), 'active': state.get('active'),
                'activeRequestId': state.get('activeRequestId'), 'sessionId': state.get('sessionId'),
                'ownerGeneration': state.get('ownerGeneration')}
        receipt.stage('readOnlyRecovery', {'result': 'PASS', 'states': states, 'tasksResent': 0})
        return
    if not (args.sessions or args.real_model or args.real_search):
        return
    selected = client.json('/api/cwd/validate', {'cwd': project})
    if selected.get('cwd') != project or selected.get('success') is not True:
        raise CheckError('PROJECT_SELECTION_MISMATCH')
    receipt.stage('projectSelection', {'result': 'PASS', 'project': project})
    for name in ('A', 'B'):
        receipt.stage('create' + name, {'result': 'BLOCKED', 'code': 'CREATION_PENDING_NO_AUTO_RETRY'})
        created = client.json('/api/agent/new', {'type': 'ensure_session', 'cwd': project, 'serverId': config['serverId']})
        if created.get('success') is not True or not created.get('sessionId') or not created.get('ownerGeneration'):
            raise CheckError('SESSION_CREATION_FAILED')
        row = {'id': created['sessionId'], 'ownerGeneration': created['ownerGeneration']}
        receipt.data['sessions'][name] = row
        receipt.save()
        state = client.json('/api/agent/' + urllib.parse.quote(row['id'], safe='')).get('state') or {}
        identity = worker_identity(state, expected, args.release.resolve(), runtime['agentDir'], project, row['id'])
        receipt.stage('create' + name, identity)
        if identity['result'] != 'PASS':
            raise CheckError('MAIN_WORKER_IDENTITY_MISMATCH')
    if receipt.data['sessions']['A']['id'] == receipt.data['sessions']['B']['id']:
        raise CheckError('SESSIONS_COLLIDED')
    checks = []
    for name in ('A', 'B', 'A'):
        row = receipt.data['sessions'][name]
        state = client.json('/api/agent/' + urllib.parse.quote(row['id'], safe='')).get('state') or {}
        checks.append(state.get('sessionId') == row['id'] and state.get('ownerGeneration') == row['ownerGeneration'] and state.get('cwd') == project)
    receipt.stage('httpABReadback', {'result': 'PASS' if all(checks) else 'FAIL', 'sequence': ['A', 'B', 'A'],
        'windowsInteraction': 'NOT_RUN', 'doesNotTestHistoricalRestart': True})
    for name in ('A', 'B'):
        row = receipt.data['sessions'][name]
        path = '/api/agent/' + urllib.parse.quote(row['id'], safe='')
        tool_response = client.json(path, scoped(row, project, config['serverId'], 'get_tools'))
        tools = (tool_response.get('data') or {}).get('data') or {}
        receipt.stage('tools' + name, tool_inventory(tools))
        models_response = client.json(path, scoped(row, project, config['serverId'], 'get_models'))
        models = (models_response.get('data') or {}).get('data') or {}
        receipt.stage('models' + name, {'result': 'PASS' if models.get('current') and models.get('models') else 'FAIL', 'current': models.get('current'),
            'models': [{k: v.get(k) for k in ('provider', 'id', 'authenticated')} for v in models.get('models', [])]})
    if args.real_model or args.real_search:
        # Real means a configured provider, never the offline/scripted fixture.
        if runtime.get('providerFixture') or runtime.get('offline') or runtime.get('offlineModel') or runtime.get('provider') in ('fixture', 'offline', 'test'):
            raise CheckError('REAL_PROVIDER_REQUIRED')
        models = receipt.data['stages']['modelsA']
        current = models.get('current') or {}
        if not any(row.get('authenticated') is True and row.get('provider') == current.get('provider') and row.get('id') == current.get('id') for row in models['models']):
            receipt.stage('realModel', {'result': 'BLOCKED', 'code': 'MODEL_CREDENTIAL_NOT_CONFIGURED'})
        else:
            marker = 'BIOMIND_ACCEPT_' + uuid.uuid4().hex
            prompt_once(client, receipt, 'realModel', receipt.data['sessions']['A'], project, config['serverId'],
                '请不要调用工具，只原样回复以下验证字符串：' + marker, marker, args.timeout)
    if args.real_search:
        if receipt.data['stages'].get('realModel', {}).get('result') != 'PASS':
            receipt.stage('realSearch', {'result': 'BLOCKED', 'code': 'REAL_MODEL_NOT_VERIFIED'})
        else:
            marker = 'network-check-' + uuid.uuid4().hex
            prompt_once(client, receipt, 'realSearch', receipt.data['sessions']['B'], project, config['serverId'],
                '请使用 pi-web-access 的 web_search 实际联网搜索 NCBI BLAST official documentation。'
                '本次查询标识 ' + marker + '；请将该标识附在搜索词中，勿使用旧缓存；只报告搜索结果与来源网址，不调用其他工具，不修改文件。',
                marker, args.timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('service', 'config', 'release', 'project', 'evidence'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--isolation-root', type=Path)
    parser.add_argument('--base-url')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--sessions', action='store_true')
    parser.add_argument('--real-model', action='store_true')
    parser.add_argument('--real-search', '--web-search', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 10 <= args.timeout <= 600:
        parser.error('--timeout must be between 10 and 600 seconds')
    receipt = None
    try:
        receipt = Receipt(args.evidence, args.resume)
        run(args, receipt)
        states = [row.get('result') for row in receipt.data['stages'].values() if 'result' in row]
        receipt.data['result'] = 'FAIL' if 'FAIL' in states else 'BLOCKED' if 'BLOCKED' in states else 'PASS'
    except (OSError, ValueError, KeyError, TypeError, CheckError, subprocess.SubprocessError) as error:
        code = 'RECEIPT_EXISTS_USE_RESUME_NO_TASK_REPLAY' if isinstance(error, FileExistsError) else safe_code(error)
        if receipt:
            receipt.data['result'] = 'BLOCKED'
            receipt.data['code'] = code
        else:
            print(json.dumps({'result': 'BLOCKED', 'code': code, 'automaticReplay': False}))
            return 2
    receipt.save()
    print(json.dumps({'result': receipt.data['result'], 'code': receipt.data.get('code'),
        'stages': {k: v.get('result', v.get('status')) for k, v in receipt.data['stages'].items()},
        'evidence': str(args.evidence), 'windowsInteraction': 'NOT_RUN', 'automaticReplay': False}, ensure_ascii=False))
    return 0 if receipt.data['result'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())

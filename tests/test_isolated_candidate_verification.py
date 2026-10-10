import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('isolated_verify', Path(__file__).resolve().parents[1] / 'scripts/verify-isolated-candidate.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class IsolatedVerification(unittest.TestCase):
    def owner_state(self, name='A'):
        return {'running': True, 'state': {
            'loaded_build': {'releaseId': 'release-a', 'sourceCommit': 'commit-a',
                             'releaseRoot': '/release/a', 'entrypoint': 'json', 'piVersion': '0.87.1'},
            'cwd': '/candidate/project', 'sessionId': name, 'ownerGeneration': 'generation-' + name,
            'sessionFile': '/candidate/agent/sessions/' + name + '.jsonl',
            'active': False, 'activeRequestId': None, 'controlBusy': None, 'pendingUI': [],
            'pendingDomainControlCount': 0, 'packagesQuiescing': False,
            'queue': {'pendingAssociationCount': 0, 'actualHasQueuedMessages': False}}}

    def old_receipt(self, path):
        receipt = verify.Receipt(path)
        receipt.data.update(serverId='server', project='/candidate/project', sessions={
            name: {'id': name, 'ownerGeneration': 'generation-' + name} for name in ('A', 'B')})
        receipt.save()
        return receipt

    def test_reuse_rejects_wrong_schema_scope_and_missing_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            old = self.old_receipt(Path(folder) / 'old.json')
            original = json.loads(old.path.read_text())
            for change, code in [({'schema': 'another'}, 'SESSION_RECEIPT_SCHEMA_MISMATCH'),
                    ({'serverId': 'production'}, 'SESSION_RECEIPT_SCOPE_MISMATCH'),
                    ({'project': '/production'}, 'SESSION_RECEIPT_SCOPE_MISMATCH'),
                    ({'sessions': {'A': original['sessions']['A']}}, 'SESSION_RECEIPT_OWNER_MISSING')]:
                with self.subTest(change=change):
                    old.data = {**original, **change}
                    old.save()
                    with self.assertRaises(verify.CheckError) as error:
                        verify.reused_session_rows(old.path, 'server', '/candidate/project')
                    self.assertEqual(error.exception.code, code)

    def test_reuse_checks_actual_build_generation_and_idle_without_post(self):
        args = ({'releaseId': 'release-a', 'sourceCommit': 'commit-a'}, Path('/release/a'),
                '/candidate/agent', '/candidate/project')
        row = {'id': 'A', 'ownerGeneration': 'generation-A'}
        mutations = [('ownerGeneration', 'new-owner', 'ORIGINAL_OWNER_GENERATION_CHANGED'),
            ('sessionId', 'replacement', 'MAIN_WORKER_IDENTITY_MISMATCH'),
            ('loaded_build', {}, 'MAIN_WORKER_IDENTITY_MISMATCH'),
            ('active', True, 'ORIGINAL_SESSION_NOT_IDLE'),
            ('queue', {'pendingAssociationCount': 1, 'actualHasQueuedMessages': True}, 'ORIGINAL_SESSION_NOT_IDLE'),
            ('queue', {}, 'ORIGINAL_SESSION_NOT_IDLE'),
            ('pendingUI', [{'id': 'question'}], 'ORIGINAL_SESSION_NOT_IDLE'),
            ('controlBusy', 'reload', 'ORIGINAL_SESSION_NOT_IDLE')]
        for field, value, code in mutations:
            state = self.owner_state()
            state['state'][field] = value
            calls = []
            def fetch(route, payload=None):
                calls.append((route, payload))
                return state
            with self.subTest(field=field), self.assertRaises(verify.CheckError) as error:
                verify.check_existing_owner(SimpleNamespace(json=fetch), row, *args)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(calls, [('/api/agent/A', None)])
        state = {'running': False, 'resumable': True}
        with self.assertRaises(verify.CheckError) as error:
            verify.check_existing_owner(SimpleNamespace(json=lambda *_: state), row, *args)
        self.assertEqual(error.exception.code, 'ORIGINAL_SESSION_NOT_RUNNING_NO_AUTO_RESUME')

    def test_reuse_preserves_original_ids_and_does_not_create_resume_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            old = self.old_receipt(Path(folder) / 'old.json')
            current = verify.Receipt(Path(folder) / 'new.json')
            calls = []
            def fetch(route, payload=None):
                calls.append((route, payload))
                self.assertIsNone(payload)
                return self.owner_state(route.rsplit('/', 1)[1])
            verify.reuse_sessions(SimpleNamespace(json=fetch), current, old.path, 'server', '/candidate/project',
                {'releaseId': 'release-a', 'sourceCommit': 'commit-a'}, Path('/release/a'), '/candidate/agent')
            self.assertEqual(current.data['sessions'], old.data['sessions'])
            self.assertEqual(calls, [('/api/agent/A', None), ('/api/agent/B', None)])
            self.assertFalse(current.data['stages']['reuseA']['reloaded'])
            self.assertEqual(current.data['stages']['reuseB']['ownerGeneration'], 'generation-B')

    def test_native_probe_keeps_auth_cache_false_and_never_records_key(self):
        models = {'current': {'provider': 'deepseek', 'id': 'test'},
                  'models': [{'provider': 'deepseek', 'id': 'test', 'authenticated': False}]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            agent = root / 'agent'
            agent.mkdir()
            auth = agent / 'auth.json'
            auth.write_text(json.dumps({'deepseek': {'type': 'api_key', 'key': 'test-only-private-value'}}))
            auth.chmod(0o600)
            proof = verify.native_credential_probe({'agentDir': str(agent)}, root, models)
            self.assertTrue(proof['nativeCredentialPresent'])
            self.assertFalse(proof['cachedAuthenticated'])
            self.assertFalse(models['models'][0]['authenticated'])
            self.assertEqual(proof['result'], 'NOT_RUN')
            self.assertEqual(proof['authenticity'], 'not_yet_verified')
            self.assertNotIn('test-only-private-value', json.dumps(proof))
            for key in ('', ' ', '!echo do-not-execute'):
                auth.write_text(json.dumps({'deepseek': {'type': 'api_key', 'key': key}}))
                self.assertFalse(verify.native_credential_probe({'agentDir': str(agent)}, root, models)['nativeCredentialPresent'])
            with self.assertRaises(verify.CheckError) as error:
                verify.native_credential_probe({'agentDir': str(agent)}, root, {'current': {'provider': 'other'}})
            self.assertEqual(error.exception.code, 'NATIVE_CREDENTIAL_PROBE_PROVIDER_UNSUPPORTED')

    def test_full_reuse_requires_explicit_probe_and_posts_only_one_model_task(self):
        for probe in (False, True):
            with self.subTest(probe=probe), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                agent, project, release = (root / name for name in ('agent', 'project', 'release'))
                agent.mkdir()
                project.mkdir()
                (release / 'scripts').mkdir(parents=True)
                (release / 'RELEASE.json').write_text(json.dumps({'releaseId': 'release-a', 'sourceCommit': 'commit-a'}))
                (release / 'scripts/verify-studio-core.py').write_text('def validate(*args):\n    return {"result": "PASS"}\n')
                runtime = root / 'runtime.json'
                runtime.write_text(json.dumps({'agentDir': str(agent), 'sessionMode': 'persistent', 'provider': 'deepseek'}))
                def write_private(path, data):
                    path.write_text(json.dumps(data))
                    path.chmod(0o600)
                config, env = root / 'config.json', root / 'environment.json'
                write_private(config, {'serverId': 'server', 'stateRoot': str(root / 'state'),
                    'environmentFile': str(env), 'environment': {'BIOMIND_RUNTIME_CONFIG': str(runtime)}})
                write_private(env, {'PI_WEB_PASSWORD': 'test-only-workbench-value'})
                write_private(agent / 'auth.json', {'deepseek': {'type': 'api_key', 'key': 'test-only-native-value'}})
                old = self.old_receipt(root / 'old.json')
                old.data['project'] = str(project)
                old.save()
                receipt = verify.Receipt(root / 'new.json')
                args = SimpleNamespace(config=config, release=release, project=project, service=root / 'service.py',
                    isolation_root=root, base_url='http://127.0.0.1:1', timeout=10,
                    sessions=False, real_model=True, real_search=False, resume=False,
                    reuse_sessions_from=old.path, probe_native_credential=probe)
                calls = []
                def http(route, payload=None):
                    calls.append((route, payload))
                    if route.startswith('/api/plugins?'):
                        return {'packages': [{'source': 'npm:pi-web-access@0.38.0', 'installed': True, 'installedVersion': '0.38.0'}]}
                    if route in ('/_biomind_studio/health', '/api/agent/profile'):
                        return {}
                    self.assertIn(route, ('/api/agent/A', '/api/agent/B'))
                    if payload is None:
                        value = self.owner_state(route.rsplit('/', 1)[1])
                        value['state']['cwd'] = str(project)
                        value['state']['sessionFile'] = str(agent / 'sessions' / (value['state']['sessionId'] + '.jsonl'))
                        value['state']['loaded_build']['releaseRoot'] = str(release)
                        return value
                    self.assertIn(payload['type'], ('get_tools', 'get_models'))
                    self.assertEqual(payload['ownerGeneration'], 'generation-' + payload['sessionId'])
                    if payload['type'] == 'get_tools':
                        names = ['web_search', 'fetch_content', 'get_search_content', 'source_check', 'web_enable']
                        return {'data': {'data': {'registered': [{'name': n} for n in names], 'effective': names}}}
                    return {'data': {'data': {'current': {'provider': 'deepseek', 'id': 'test'},
                        'models': [{'provider': 'deepseek', 'id': 'test', 'authenticated': False}]}}}
                submitted = []
                def prompt(client, target, stage, row, *_):
                    submitted.append(row.copy())
                    result = {'result': 'PASS'}
                    target.stage(stage, result)
                    return result
                discovery = SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'ready', 'host': 'host',
                    'port': 1, 'serverId': 'server', 'instanceId': 'instance'}))
                with patch.object(verify.subprocess, 'run', return_value=discovery), \
                     patch.object(verify, 'Client', return_value=SimpleNamespace(json=http)), \
                     patch.object(verify, 'prompt_once', side_effect=prompt):
                    verify.run(args, receipt)
                self.assertEqual(receipt.data['sessions'], old.data['sessions'])
                self.assertEqual(submitted, [old.data['sessions']['A']] if probe else [])
                self.assertEqual(receipt.data['stages']['modelsA']['models'][0]['authenticated'], False)
                if probe:
                    proof = receipt.data['stages']['authenticationProbe']
                    self.assertEqual(proof['result'], 'PASS')
                    self.assertFalse(proof['cachedAuthenticated'])
                    self.assertEqual(proof['authenticity'], 'verified_by_real_model')
                else:
                    self.assertEqual(receipt.data['stages']['realModel']['code'], 'MODEL_CREDENTIAL_NOT_CONFIGURED')
                    self.assertNotIn('authenticationProbe', receipt.data['stages'])
                self.assertNotIn('test-only-native-value', receipt.path.read_text())
                self.assertFalse(any(route == '/api/agent/new' or (payload and payload['type'] in ('reload', 'resume_session')) for route, payload in calls))

    def test_pending_receipt_prevents_duplicate_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            receipt = verify.Receipt(path)
            receipt.stage('realModel', {'result': 'BLOCKED', 'code': 'SUBMISSION_PENDING_OUTCOME_UNKNOWN', 'clientMessageId': 'same'})
            with self.assertRaises(FileExistsError):
                verify.Receipt(path)
            loaded = verify.Receipt(path, resume=True)
            self.assertEqual(loaded.data['stages']['realModel']['clientMessageId'], 'same')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_isolation_rejects_production_and_symlink_and_shared_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'candidate'
            root.mkdir()
            production = Path(folder) / 'production'
            production.mkdir()
            (root / 'link').symlink_to(production, target_is_directory=True)
            config = {'stateRoot': str(root / 'state'), 'environmentFile': str(root / 'credential.json'),
                      'environment': {'BIOMIND_RUNTIME_CONFIG': str(root / 'runtime.json')}}
            runtime = {'agentDir': str(root / 'agent'), 'sessionMode': 'persistent', 'dataRoot': '.biomind/domain'}
            self.assertTrue(verify.validate_isolation(config, runtime, str(root / 'project'), root))
            for field, value in [('agentDir', str(production)), ('agentDir', str(root / 'link')), ('memoryRoot', str(production))]:
                with self.subTest(field=field, value=value):
                    changed = {**runtime, field: value}
                    with self.assertRaises(verify.CheckError):
                        verify.validate_isolation(config, changed, str(root / 'project'), root)

    def test_worker_uses_actual_loaded_build_not_package_context(self):
        expected = {'releaseId': 'release-a', 'sourceCommit': 'commit-a'}
        state = {'loaded_build': {**expected, 'releaseRoot': '/release/a', 'entrypoint': 'json', 'piVersion': '0.87.1'},
                 'cwd': '/candidate/project', 'sessionId': 'A', 'ownerGeneration': 'generation',
                 'sessionFile': '/candidate/agent/sessions/a.jsonl'}
        args = (expected, Path('/release/a'), '/candidate/agent', '/candidate/project', 'A')
        self.assertEqual(verify.worker_identity(state, *args)['result'], 'PASS')
        for key, value in [('releaseRoot', '/S2'), ('releaseId', 'old'), ('entrypoint', 'native'), ('piVersion', '0.88.0')]:
            old = {**state, 'loaded_build': {**state['loaded_build'], key: value}}
            self.assertEqual(verify.worker_identity(old, *args)['result'], 'FAIL')
        self.assertEqual(verify.worker_identity({**state, 'sessionFile': '/production/a.jsonl'}, *args)['result'], 'FAIL')

    def test_credentials_never_follow_redirect_or_leave_loopback(self):
        for base in ('http://remote:8080', 'http://127.0.0.1@remote/', 'http://localhost/path', 'file:///tmp', 'http://localhost?key=secret'):
            with self.subTest(base=base), self.assertRaises(verify.CheckError):
                verify.Client(base, 'private-workbench-credential')
        with self.assertRaises(verify.CheckError):
            verify.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_tool_evidence_removes_query_and_content(self):
        events = verify.Events(None, 'A', 'marker', 10)
        events.observe({'type': 'tool_execution_end', 'toolName': 'web_search', 'toolCallId': 'tool-a',
                        'result': {'content': [{'text': 'https://example.org/search?token=secret private prompt'}]}})
        serialized = json.dumps(events.proof)
        self.assertNotIn('secret', serialized)
        self.assertNotIn('private prompt', serialized)
        self.assertEqual(events.proof['toolCalls'][0]['sourceOrigins'], ['https://example.org'])
        self.assertFalse(events.proof['toolCalls'][0]['searchSucceeded'])

    def test_search_error_with_help_url_is_not_a_successful_search(self):
        events = verify.Events(None, 'A', 'marker', 10)
        events.observe({'type': 'tool_execution_end', 'toolName': 'web_search',
                        'result': {'content': [{'text': 'See https://example.org/help'}],
                                   'details': {'error': 'Missing provider key', 'successfulQueries': 0, 'totalResults': 0}}})
        self.assertFalse(events.proof['toolCalls'][0]['searchSucceeded'])
        events.observe({'type': 'tool_execution_end', 'toolName': 'web_search',
                        'result': {'content': [{'text': 'Source https://ncbi.nlm.nih.gov'}],
                                   'details': {'successfulQueries': 1, 'totalResults': 1}}})
        self.assertTrue(events.proof['toolCalls'][1]['searchSucceeded'])

    def test_sse_requires_completion_not_merely_registration(self):
        events = verify.Events(None, 'A', 'marker', 10)
        events.observe({'type': 'connected'})
        events.observe({'type': 'message_end', 'message': {'role': 'assistant', 'usage': {'output': 3}, 'content': [{'type': 'text', 'text': 'marker'}]}})
        self.assertFalse(events.done.is_set())
        events.observe({'type': 'biomind_result', 'runtime_status': 'completed', 'requestId': 'request-a'})
        self.assertTrue(events.done.is_set())
        self.assertEqual(events.proof['assistantOutputTokens'], 3)
        self.assertTrue(events.proof['markerObserved'])

    def test_empty_or_wrong_version_package_inventory_never_passes(self):
        self.assertEqual(verify.plugin_inventory({'packages': []})['result'], 'FAIL')
        row = {'source': 'npm:pi-web-access@0.38.0', 'installed': True, 'installedVersion': '0.37.0'}
        self.assertEqual(verify.plugin_inventory({'packages': [row]})['result'], 'FAIL')
        row['installedVersion'] = '0.38.0'
        self.assertEqual(verify.plugin_inventory({'packages': [row]})['result'], 'PASS')
        self.assertEqual(verify.tool_inventory({})['result'], 'FAIL')
        names = ['web_search', 'fetch_content', 'get_search_content', 'source_check', 'web_enable']
        self.assertEqual(verify.tool_inventory({'registered': [{'name': n} for n in names], 'effective': []})['result'], 'FAIL')
        self.assertEqual(verify.tool_inventory({'registered': [{'name': n} for n in names], 'effective': names})['result'], 'PASS')

    def test_post_failure_is_never_retried_and_preserves_message_id(self):
        class BrokenClient:
            calls = 0
            def json(self, *_args, **_kwargs):
                self.calls += 1
                raise verify.CheckError('NETWORK_LOST_AFTER_WRITE')
        class Stream:
            def start(self):
                pass
        class FakeEvents:
            def __init__(self, *_args):
                self.connected = __import__('threading').Event()
                self.connected.set()
                self.thread = Stream()
        with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'Events', FakeEvents):
            receipt = verify.Receipt(Path(folder) / 'receipt.json')
            client = BrokenClient()
            with self.assertRaises(verify.CheckError):
                verify.prompt_once(client, receipt, 'realModel', {'id': 'A', 'ownerGeneration': 'g'}, '/candidate', 'server', 'hello', 'marker', 10)
            self.assertEqual(client.calls, 1)
            saved = json.loads(receipt.path.read_text())
            self.assertEqual(saved['stages']['realModel']['code'], 'SUBMISSION_PENDING_OUTCOME_UNKNOWN')
            self.assertTrue(saved['stages']['realModel']['clientMessageId'].startswith('accept-'))


if __name__ == '__main__':
    unittest.main()

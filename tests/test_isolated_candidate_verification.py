import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('isolated_verify', Path(__file__).resolve().parents[1] / 'scripts/verify-isolated-candidate.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class IsolatedVerification(unittest.TestCase):
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

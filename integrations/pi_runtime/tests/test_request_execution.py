"""Legacy input-inspection metadata must not override explicit request lifecycle."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_external_access_integration import DomainService, DomainError, REPO
from agent_core.pending_input import mutate


class RequestExecution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-request-execution-')
        self.root = Path(self.tmp.name)
        registry = self.root / 'registry.json'
        registry.write_text('{"environments":{},"tools":{}}')
        preferences = self.root / 'preferences.md'
        preferences.write_text('fixture')
        self.s = DomainService(cwd=self.root, data_root=self.root / 'data', install_root=REPO,
            session_id='request-execution', registry_path=registry, preferences_path=preferences,
            skills_root=self.root / 'skills', execution_profile='native')
        await self.s.call('request.begin', {'request_id': 'test', 'role': 'main'}, host_token=self.s.host_token)

    async def asyncTearDown(self):
        await self.s.formal.shutdown()
        await self.s.disconnected()
        self.tmp.cleanup()

    async def run_command(self, call_id):
        result = await self.s.call('exec.start', {'call_id': call_id,
            'arguments': {'cmd': 'printf inspected', 'timeout_ms': 10000}})
        for _ in range(100):
            if result.get('terminal') or not result.get('run_ref'):
                return result
            result = await self.s.call('exec.wait', {'run_ref': result['run_ref'], 'max_wait_ms': 100})
        self.fail(str(result))

    async def test_legacy_input_metadata_does_not_block_but_explicit_pause_does(self):
        scope = self.s.scope()
        def legacy_metadata(state):
            state['task_state']['scientific_input'] = {'scope': scope, 'status': 'blocked',
                'reason_code': 'Q_SAMPLE_MAPPING_UNKNOWN'}
        mutate(self.s.data_root, self.s.task_id, legacy_metadata)
        result = await self.run_command('legacy-metadata')
        self.assertEqual(result.get('exit_code'), 0, result)
        self.assertEqual(result['stdout'], 'inspected')
        self.assertFalse((self.root / 'output').exists())
        await self.s.call('request.pause', {'reason': 'user_paused'}, host_token=self.s.host_token)
        with self.assertRaisesRegex(DomainError, 'REQUEST_PAUSED'):
            await self.run_command('paused')
        await self.s.call('request.resume', {}, host_token=self.s.host_token)
        result = await self.run_command('resumed')
        self.assertEqual(result.get('exit_code'), 0, result)
        self.assertEqual(result['stdout'], 'inspected')


if __name__ == '__main__':
    unittest.main(verbosity=2)

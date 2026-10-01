"""Directory traversal must reuse file authority before returning any names."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parents[1]
sys.path[:0] = [str(RUNTIME), str(REPO)]
from domain_adapter.service import DomainService


class DiscoveryAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='biomind-discovery-authority-')
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.external = self.root / 'external'
        self.external.mkdir()
        (self.external / 'outside.txt').write_text('SYNTHETIC_OUTSIDE')
        registry = self.root / 'registry.json'
        registry.write_text('{"environments":{},"tools":{}}')
        self.service = DomainService(cwd=self.project, data_root=self.project / '.biomind' / 'domain',
                                     install_root=REPO, session_id='discovery-fixture', registry_path=registry)
        await self.host('request.begin', {'request_id': 'request-1', 'role': 'consult'})

    async def asyncTearDown(self):
        await self.service.formal.shutdown()
        self.temp.cleanup()

    async def host(self, method, params):
        return await self.service.call(method, params, host_token=self.service.host_token)

    async def list(self, filename=None):
        return await self.host('files.list', {'path': str(filename or self.project), 'call_id': 'listing'})

    async def decide(self, result, choice):
        row = result['approval']
        return await self.host('approval.decide', {'request_id': row['request_id'],
            'action_hash': row['action_hash'], 'request_scope': row['request_scope'], 'choice': choice})

    async def test_metadata_only_excludes_private_links_and_special_files(self):
        for name in ('visible.txt', '.hidden.txt', '.env.fixture', 'auth.json'):
            (self.project / name).write_text('SYNTHETIC_CONTENT')
        (self.project / 'nested').mkdir()
        (self.project / '.git').mkdir()
        (self.project / 'alias').symlink_to(self.external, target_is_directory=True)
        os.link(self.project / 'visible.txt', self.project / 'hard.txt')
        os.mkfifo(self.project / 'pipe')
        result = await self.list()
        names = {row['name'] for row in result['entries']}
        self.assertEqual(names, {'.hidden.txt', 'nested'})
        self.assertNotIn('SYNTHETIC_CONTENT', str(result))
        self.assertFalse(result['scan_limited'])

    async def test_exact_file_grant_does_not_authorize_parent_listing(self):
        pending = await self.host('files.read_authorize', {'path': str(self.external / 'outside.txt'), 'call_id': 'file'})
        await self.decide(pending, 'y')
        parent = await self.list(self.external)
        self.assertEqual(parent['status'], 'waiting_approval')
        self.assertNotIn('entries', parent)
        await self.decide(parent, 'y')
        self.assertEqual((await self.list(self.external))['entries'], [{'name': 'outside.txt', 'kind': 'file'}])

    async def test_denied_listing_cannot_be_retried_through_another_tool(self):
        pending = await self.list(self.external)
        await self.decide(pending, 'n')
        for method in ('files.list', 'files.read_authorize'):
            with self.assertRaisesRegex(ValueError, 'REQUEST_PAUSED'):
                await self.host(method, {'path': str(self.external), 'call_id': 'alternate'})

    async def test_replaced_directory_grant_requires_fresh_authority(self):
        pending = await self.list(self.external)
        await self.decide(pending, 'y')
        self.external.rename(self.root / 'old-external')
        self.external.mkdir()
        (self.external / 'replacement.txt').write_text('SYNTHETIC_REPLACEMENT')
        with self.assertRaisesRegex(ValueError, 'PERMISSION_ACTION_CHANGED'):
            await self.list(self.external)

    async def test_host_boundary_and_explicit_incomplete_snapshot(self):
        with self.assertRaisesRegex(ValueError, 'HOST'):
            await self.service.call('files.list', {'path': str(self.external), 'trusted_skill_dirs': [str(self.external)]})
        for index in range(3):
            (self.project / f'file-{index}').write_text('synthetic')
        with patch('domain_adapter.discovery.time.monotonic', side_effect=[0, 0, 6]):
            result = await self.list()
        self.assertTrue(result['scan_limited'])


if __name__ == '__main__':
    unittest.main()

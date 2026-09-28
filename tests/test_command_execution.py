from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.command_execution import CommandExecutionService, CommandInspectionError, ProjectContext


class ProjectContextTests(unittest.TestCase):
    def test_unicode_marker_explicit_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '项目 with spaces'
            child = root / 'runs'
            child.mkdir(parents=True)
            (root / '.biomind-project').touch()
            self.assertEqual(ProjectContext.discover(startup_dir=child, environ={}).project_dir, root)
            self.assertEqual(ProjectContext.discover('.', startup_dir=child,
                environ={'BIOMIND_PROJECT_DIR': str(root)}).project_dir, child)
            self.assertEqual(ProjectContext.discover(startup_dir=child,
                environ={'BIOMIND_PROJECT_DIR': str(root)}).source, 'configured')

    def test_saved_project_missing_requires_explicit_rebind(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BIOMIND_PROJECT_DIR': ''}):
            old = Path(directory) / 'old'; old.mkdir()
            new = Path(directory) / 'new'; new.mkdir()
            service = CommandExecutionService(data_path=directory, startup_dir=old)
            self.assertEqual(service.project_for_task('task'), old)
            old.rename(new / 'moved')
            resumed = CommandExecutionService(data_path=directory, startup_dir=new)
            with self.assertRaisesRegex(CommandInspectionError, 'PROJECT_REBIND_REQUIRED'):
                resumed.project_for_task('task')
            rebound = CommandExecutionService(data_path=directory, project_dir=new)
            self.assertEqual(rebound.project_for_task('task'), new)




class ShellInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='biomind-command-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CommandExecutionService(data_path=self.root, project_dir=self.root)
        # A small isolated fixture; real host discovery is exercised separately.
        self.service.install_dir = self.root
        self.service.prefixes = set()
        self.service.path = ['/usr/bin', '/bin']
        check = await self.service.run({'cmd': 'true', 'timeout_ms': 3000}, task_id='availability')
        if check['status'] != 'completed' or check['exit_code'] != 0:
            self.skipTest(f"Read-only sandbox unavailable: {check.get('error', check['stderr'])}")

    async def test_pipeline_python_unicode_cwd_and_failure(self):
        subdir = self.root / '数据 空格'; subdir.mkdir()
        (subdir / 'input.txt').write_text('alpha\nbeta\nbeta2\n', encoding='utf-8')
        result = await self.service.run({'cmd': "sed -n '2,3p' input.txt | /usr/bin/python3 -c 'import sys; print(len(sys.stdin.readlines()))'",
                                         'workdir': '数据 空格'}, task_id='pipeline', stage='consultant')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual(result['stdout'].strip(), '2')
        self.assertEqual(result['cwd'], str(subdir))
        self.assertTrue(Path(result['receipt_ref']).is_file())
        failed = await self.service.run({'cmd': 'echo missing >&2; exit 7'}, task_id='pipeline')
        self.assertEqual(failed['exit_code'], 7)
        self.assertIn('missing', failed['stderr'])

    async def test_readonly_secrets_network_and_environment(self):
        (self.root / '.env.api_keys').write_text('TEST_SECRET=do-not-disclose', encoding='utf-8')
        with patch.dict(os.environ, {'TEST_SECRET': 'do-not-disclose'}):
            result = await self.service.run({'cmd': 'cat .env.api_keys; echo "${TEST_SECRET-unset}"; touch forbidden.txt'}, task_id='boundary')
        self.assertNotEqual(result['exit_code'], 0)
        self.assertNotIn('do-not-disclose', result['stdout'] + result['stderr'])
        self.assertIn('unset', result['stdout'])
        self.assertFalse((self.root / 'forbidden.txt').exists())
        network = await self.service.run({'cmd': "/usr/bin/python3 -c 'import socket; socket.socket()'"}, task_id='boundary')
        self.assertNotEqual(network['exit_code'], 0)
        self.assertIn('Operation not permitted', network['stderr'])
        scratch = await self.service.run({'cmd': 'echo okay > /tmp/private-test; cat /tmp/private-test'}, task_id='boundary')
        self.assertEqual(scratch['stdout'].strip(), 'okay')

    async def test_symlink_cannot_escape_roots(self):
        with tempfile.TemporaryDirectory(prefix='outside-command-') as outside:
            (Path(outside) / 'private.txt').write_text('outside-private', encoding='utf-8')
            (self.root / 'outside').symlink_to(outside, target_is_directory=True)
            result = await self.service.run({'cmd': 'cat outside/private.txt'}, task_id='escape')
            self.assertNotEqual(result['exit_code'], 0)
            self.assertNotIn('outside-private', result['stdout'])
            rejected = await self.service.run({'cmd': 'pwd', 'workdir': str(self.root / 'outside')}, task_id='escape')
            self.assertEqual(rejected['status'], 'not_run')

    async def test_sensitive_symlinks_are_excluded_without_hiding_the_project(self):
        (self.root / 'credential-value.json').write_text('hidden-value', encoding='utf-8')
        (self.root / '.env').symlink_to('credential-value.json')
        (self.root / 'public.txt').write_text('visible-value', encoding='utf-8')
        result = await self.service.run({'cmd': 'cat public.txt; cat .env; cat credential-value.json'}, task_id='secret-link')
        self.assertIn('visible-value', result['stdout'])
        self.assertNotIn('hidden-value', result['stdout'])
        self.assertEqual(result['status'], 'completed')

    async def test_existing_force_stop_cleans_inspection_process(self):
        from execution.executor import terminate_active_processes
        from agent_core.process_registry import ACTIVE_PROCESS_GROUPS
        task = asyncio.create_task(self.service.run({'cmd': 'sleep 60 & wait'}, task_id='force-stop'))
        for _ in range(100):
            if ACTIVE_PROCESS_GROUPS:
                break
            await asyncio.sleep(.02)
        self.assertTrue(ACTIVE_PROCESS_GROUPS)
        self.assertGreaterEqual(terminate_active_processes(), 1)
        result = await asyncio.wait_for(task, 5)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertFalse(ACTIVE_PROCESS_GROUPS)

    async def test_timeout_cleans_background_children_and_cancellation(self):
        result = await asyncio.wait_for(self.service.run({'cmd': 'sleep 60 & wait', 'timeout_ms': 150}, task_id='timeout'), 5)
        self.assertTrue(result['timed_out'])
        self.assertIsNotNone(result['exit_code'])
        task = asyncio.create_task(self.service.run({'cmd': 'sleep 60 & wait'}, task_id='cancel'))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        receipts = list((self.root / 'task_outputs' / 'cancel' / 'command_inspection').glob('*/receipt.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'cancelled')

    async def test_output_is_bounded_and_can_be_read_in_followup(self):
        result = await self.service.run({'cmd': "/usr/bin/python3 -c 'print(\"x\" * 20000)'", 'max_output_chars': 256}, task_id='output')
        self.assertEqual(len(result['stdout']), 256)
        self.assertTrue(result['truncated']['stdout'])
        self.assertFalse(result['log_truncated']['stdout'])
        followup = await self.service.run({'cmd': 'wc -c < ' + shlex.quote(result['log_refs']['stdout'])}, task_id='output')
        self.assertEqual(int(followup['stdout']), 20001)




if __name__ == '__main__':
    unittest.main()

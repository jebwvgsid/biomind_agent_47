from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_core.command_execution import CommandExecutionService
from agent_core.workspace_files import WorkspaceFileService


def tool(name, args, call_id='call-1'):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': call_id}])


class Model:
    def bind_tools(self, schemas):
        self.schemas = schemas
        return self

    def bind(self, **kwargs):
        return self

    def model_copy(self, **kwargs):
        return self


class WorkspaceFixture:
    def setup_workspace(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='biomind-workspace-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / '项目 with spaces'
        self.project.mkdir()
        with patch('agent_core.command_execution._runtime_locations', return_value=(set(), '', ['/usr/bin', '/bin'])):
            self.commands = CommandExecutionService(data_path=self.root / 'records', project_dir=self.project)
        self.commands.install_dir = self.project
        self.files = WorkspaceFileService(self.commands)


class WorkspaceFileTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        self.setup_workspace()

    def test_new_script_is_saved_with_receipt_and_never_executed(self):
        path = self.project / '脚本' / 'run_bash.sh'
        content = '#!/bin/bash\ntouch should-not-exist\nsbatch job.sh\n'
        result = self.files.write({'path': str(path), 'content': content, 'expected_sha256': ''}, task_id='write')
        self.assertEqual(path.read_text(), content)
        self.assertFalse((self.project / 'should-not-exist').exists())
        self.assertFalse(result['executed'])
        self.assertEqual(json.loads(Path(result['receipt_ref']).read_text()), result)
        self.assertEqual((Path(result['receipt_ref']).parent / 'after.txt').read_text(), content)
        self.assertEqual(self.files.verify([result], task_id='write'), [result])

    def test_edit_requires_current_hash_preserves_mode_and_records_previous_content(self):
        target = self.project / 'run.sh'
        target.write_text('old\n'); target.chmod(0o750)
        read = self.files.read({'path': 'run.sh'}, task_id='edit')
        with self.assertRaisesRegex(ValueError, 'FILE_CHANGED'):
            self.files.write({'path': 'run.sh', 'content': 'wrong', 'expected_sha256': ''}, task_id='edit')
        target.write_text('concurrent edit\n')
        with self.assertRaisesRegex(ValueError, 'FILE_CHANGED'):
            self.files.write({'path': 'run.sh', 'content': 'wrong', 'expected_sha256': read['sha256']}, task_id='edit')
        read = self.files.read({'path': 'run.sh'}, task_id='edit')
        written = self.files.write({'path': 'run.sh', 'content': 'new\n', 'expected_sha256': read['sha256']}, task_id='edit')
        self.assertEqual(target.stat().st_mode & 0o777, 0o750)
        self.assertEqual((Path(written['receipt_ref']).parent / 'before.txt').read_text(), 'concurrent edit\n')

    def test_paths_cannot_escape_follow_links_or_access_private_files(self):
        outside = self.root / 'outside'; outside.mkdir()
        (outside / 'secret.txt').write_text('outside secret')
        (self.project / 'link').symlink_to(outside, target_is_directory=True)
        (self.project / 'linked.txt').symlink_to(outside / 'secret.txt')
        (self.project / '.env.api_keys').write_text('fixture-only')
        os.link(outside / 'secret.txt', self.project / 'hardlink.txt')
        for path in ['../outside/secret.txt', str(outside / 'secret.txt'), 'link/secret.txt',
                     'linked.txt', 'hardlink.txt', '.env.api_keys', '.git/config', '.biomind/state']:
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.files.write({'path': path, 'content': 'bad', 'expected_sha256': ''}, task_id='paths')
        self.assertEqual((outside / 'secret.txt').read_text(), 'outside secret')
        with self.assertRaises(ValueError):
            self.files.read({'path': '.env.api_keys'}, task_id='paths')

    def test_runtime_receipts_and_explicit_denied_paths_are_protected(self):
        self.commands.deny_paths = (self.project / 'protected',)
        with self.assertRaisesRegex(ValueError, 'PROTECTED_RUNTIME_PATH'):
            self.files.write({'path': 'protected/file', 'content': 'bad', 'expected_sha256': ''}, task_id='deny')
        # When task storage lives inside the project it still cannot be forged.
        self.commands.data_path = self.project
        with self.assertRaisesRegex(ValueError, 'PROTECTED_RUNTIME_PATH'):
            self.files.write({'path': 'task_outputs/t/plan_state.json', 'content': '{}', 'expected_sha256': ''}, task_id='deny')

    def test_bounded_reads_and_delivery_detect_external_changes(self):
        result = self.files.write({'path': 'notes.md', 'content': 'one\ntwo\nthree\n', 'expected_sha256': ''}, task_id='read')
        part = self.files.read({'path': 'notes.md', 'start_line': 2, 'max_lines': 1}, task_id='read')
        self.assertEqual(part['content'], 'two\n')
        self.assertEqual(part['sha256'], result['sha256'])
        self.assertEqual(part['next_line'], 3)
        (self.project / 'notes.md').write_text('external change')
        with self.assertRaisesRegex(ValueError, 'DELIVERED_FILE_CHANGED'):
            self.files.verify([result], task_id='read')

    def test_oversized_binary_and_nonregular_files_are_rejected(self):
        for content in ['x' * (128 * 1024 + 1), 'a\0b']:
            with self.assertRaisesRegex(ValueError, 'TEXT_WRITE'):
                self.files.write({'path': 'file', 'content': content, 'expected_sha256': ''}, task_id='size')
        os.mkfifo(self.project / 'pipe')
        with self.assertRaisesRegex(ValueError, 'REGULAR_SINGLE_LINK'):
            self.files.read({'path': 'pipe'}, task_id='size')


class WorkspaceLoopTests(WorkspaceFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_workspace()
        self.model = Model()











if __name__ == '__main__':
    unittest.main()

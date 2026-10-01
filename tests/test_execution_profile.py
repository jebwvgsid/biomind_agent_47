"""Frozen host metadata in the existing task store; no provider/Domain needed."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from agent_core.execution_profile import bind_host_profile, require_host_profile, release_host_profile


class ExecutionProfile(unittest.TestCase):
    def test_new_binding_and_live_host_migration_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            s=SimpleNamespace(data_root=Path(directory),task_id='synthetic',session_id='s',cwd=Path(directory),
                execution_profile='protected',active_jobs=lambda:[],formal=SimpleNamespace(active_runs=lambda *_:[]))
            self.assertEqual(bind_host_profile(s,'protected')['profile'],'protected')
            self.assertEqual(require_host_profile(s)['profile'],'protected')
            with self.assertRaisesRegex(ValueError,'RESTART_REQUIRED'):bind_host_profile(s,'native')
            with self.assertRaisesRegex(ValueError,'MIGRATION_BUSY'):bind_host_profile(s,'native',migrate=True)
            release_host_profile(s)
            self.assertEqual(bind_host_profile(s,'native',migrate=True)['profile'],'native')
            with self.assertRaisesRegex(ValueError,'CHANGED_RESTART_REQUIRED'):require_host_profile(s)

    def test_legacy_and_unrecognized_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            s=SimpleNamespace(data_root=Path(directory),task_id='legacy',session_id='s',cwd=Path(directory),
                execution_profile='native',active_jobs=lambda:[],formal=SimpleNamespace(active_runs=lambda *_:[]))
            with self.assertRaisesRegex(ValueError,'RESTART_REQUIRED'):bind_host_profile(s,'native',existing=True)
            self.assertEqual(bind_host_profile(s,'protected',existing=True)['profile'],'protected')


if __name__=='__main__':unittest.main()

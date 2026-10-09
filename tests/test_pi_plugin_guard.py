import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'agent_core/pi_plugin_guard.py'
spec = importlib.util.spec_from_file_location('guard_under_test', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ResourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.request = {'agentDir': str(self.root / 'agent'), 'cwd': str(self.project)}
        self.fds = []

    def tearDown(self):
        for fd in self.fds:
            os.close(fd)
        self.tmp.cleanup()

    def helper(self, request, fds=()):
        p = subprocess.run([sys.executable, '-B', str(SCRIPT)], input=json.dumps(request),
                           text=True, capture_output=True, pass_fds=tuple(fds))
        return p.returncode, json.loads(p.stdout)

    def opened(self, name):
        fd = os.open(name, os.O_CREAT | os.O_RDWR, 0o600)
        self.fds.append(fd)
        return fd

    def test_plan_readonly_does_not_create_scope_or_lock(self):
        result = guard.plan(self.request)
        self.assertFalse((self.root / 'agent').exists())
        self.assertFalse((self.root / '.biomind-resource-locks').exists())
        self.assertTrue(all(row['fresh'] for row in result['scopes']))

    def test_new_scope_initializes_only_with_actual_gate_lease(self):
        result = guard.prepare(self.request)
        self.assertFalse((self.root / 'agent').exists())
        gates = [{'fd': self.opened(row['gate']), 'path': row['gate'], 'mode': 'exclusive'} for row in result['scopes']]
        rc, _ = self.helper({'action': 'lock', 'files': gates}, [r['fd'] for r in gates])
        self.assertEqual(rc, 0)
        rc, final = self.helper({'action': 'initialize', **self.request, 'gates': gates}, [r['fd'] for r in gates])
        self.assertEqual(rc, 0)
        self.assertTrue(all(not row['legacyUnverified'] for row in final['scopes']))

    def test_existing_scope_is_not_claimed_even_if_empty(self):
        (self.root / 'agent').mkdir()
        self.assertTrue(next(s for s in guard.plan(self.request)['scopes'] if s['scope'] == 'user')['legacyUnverified'])

    def test_pidfile_or_environment_is_not_lock_proof(self):
        filename = self.root / 'fake.lock'
        fd = self.opened(filename)
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_LEASE_NOT_HELD'):
            guard.validate_fd(fd, str(filename), 'shared')

    def test_shared_helper_exit_retains_flock_in_parent_descriptor(self):
        filename = self.root / 'use.lock'
        fd = self.opened(filename)
        request = {'action': 'lock', 'files': [{'fd': fd, 'path': str(filename), 'mode': 'shared'}]}
        self.assertEqual(self.helper(request, [fd])[0], 0)
        guard.validate_fd(fd, str(filename), 'shared')
        independent = self.opened(filename)
        rc, reply = self.helper({'action': 'lock', 'files': [{'fd': independent, 'path': str(filename), 'mode': 'exclusive'}]}, [independent])
        self.assertEqual(rc, 2)
        self.assertEqual(reply['code'], 'BUSY')

    def test_symlinked_install_root_has_same_canonical_lock_identity(self):
        agent = self.root / 'agent'
        agent.mkdir()
        shared = self.root / 'shared-npm'
        shared.mkdir()
        (agent / 'npm').symlink_to(shared, target_is_directory=True)
        npm = next(r for r in guard.plan(self.request)['resources'] if r['root'] == str(shared))
        self.assertEqual(npm['lock'], guard.paths_for(shared)['lock'])

    def test_unsafe_lock_directory_and_replaced_descriptor_refused(self):
        directory = self.root / 'unsafe'
        directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_LOCK_DIRECTORY_UNSAFE'):
            guard.private_dir(directory)
        first, second = self.root / 'first', self.root / 'second'
        fd = self.opened(first)
        second.write_text('')
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_LEASE_FD_INVALID'):
            guard.validate_fd(fd, str(second))


if __name__ == '__main__':
    unittest.main()

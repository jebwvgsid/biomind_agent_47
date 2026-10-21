import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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

    def test_wait_acquires_after_short_contention_without_releasing_the_lease(self):
        filename = self.root / 'admission.gate'
        contender = self.opened(filename)
        holder = os.open(filename, os.O_RDWR)
        real_flock = guard.fcntl.flock
        real_flock(holder, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        contended = threading.Event()
        released = threading.Event()

        def release_after_contention():
            if contended.wait(2):
                os.close(holder)
                released.set()

        def observe_contention(fd, operation):
            try:
                return real_flock(fd, operation)
            except BlockingIOError:
                if fd == contender:
                    contended.set()
                raise

        releaser = threading.Thread(target=release_after_contention)
        releaser.start()
        try:
            with mock.patch.object(guard.fcntl, 'flock', side_effect=observe_contention):
                result = guard.lock_fds({'wait_ms': 1000, 'files': [
                    {'fd': contender, 'path': str(filename), 'mode': 'shared'},
                ]})
            self.assertEqual(result, {'status': 'locked'})
            self.assertTrue(released.wait(2))
            guard.validate_fd(contender, str(filename), 'shared')
        finally:
            contended.set()
            releaser.join(2)
            if not released.is_set():
                os.close(holder)

    def test_wait_expires_with_busy_on_a_real_contended_lock(self):
        filename = self.root / 'held.gate'
        holder, contender = self.opened(filename), self.opened(filename)
        guard.fcntl.flock(holder, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        started = time.monotonic()
        with self.assertRaisesRegex(guard.GuardError, '^BUSY$'):
            guard.lock_fds({'wait_ms': 30, 'files': [
                {'fd': contender, 'path': str(filename), 'mode': 'shared'},
            ]})
        self.assertGreaterEqual(time.monotonic() - started, 0.025)
        guard.validate_fd(holder, str(filename), 'exclusive')

    def test_wait_deadline_is_shared_across_fds_and_partial_leases_stay_held(self):
        first, second = self.root / 'first.gate', self.root / 'second.gate'
        first_holder = os.open(first, os.O_CREAT | os.O_RDWR, 0o600)
        second_holder = self.opened(second)
        first_contender, second_contender = self.opened(first), self.opened(second)
        guard.fcntl.flock(first_holder, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        guard.fcntl.flock(second_holder, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        elapsed = 0.0
        first_released = False

        def advance(seconds):
            nonlocal elapsed, first_released
            elapsed += seconds
            if elapsed >= 0.2 and not first_released:
                os.close(first_holder)
                first_released = True

        try:
            # Keep real kernel contention, but make the deadline deterministic:
            # the first gate consumes 200ms of the one 300ms request budget.
            with mock.patch.object(guard.time, 'monotonic', side_effect=lambda: elapsed), \
                    mock.patch.object(guard.time, 'sleep', side_effect=advance):
                with self.assertRaisesRegex(guard.GuardError, '^BUSY$'):
                    guard.lock_fds({'wait_ms': 300, 'files': [
                        {'fd': first_contender, 'path': str(first), 'mode': 'shared'},
                        {'fd': second_contender, 'path': str(second), 'mode': 'shared'},
                    ]})
            self.assertTrue(first_released)
            self.assertAlmostEqual(elapsed, 0.3)
            guard.validate_fd(first_contender, str(first), 'shared')
            guard.validate_fd(second_holder, str(second), 'exclusive')
            independent = self.opened(first)
            with self.assertRaises(BlockingIOError):
                guard.fcntl.flock(independent, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        finally:
            if not first_released:
                os.close(first_holder)

    def test_wait_refuses_retry_when_lock_released_after_deadline(self):
        filename = self.root / 'late-release.gate'
        contender = self.opened(filename)
        holder = os.open(filename, os.O_RDWR)
        guard.fcntl.flock(holder, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        elapsed = 0.0
        released = False

        def release_after_deadline(seconds):
            nonlocal elapsed, released
            # Simulate scheduler delay beyond the requested final sleep. The
            # real lock is available on wakeup, but the wait budget is spent.
            elapsed += seconds + 0.001
            os.close(holder)
            released = True

        try:
            with mock.patch.object(guard.time, 'monotonic', side_effect=lambda: elapsed), \
                    mock.patch.object(guard.time, 'sleep', side_effect=release_after_deadline):
                with self.assertRaisesRegex(guard.GuardError, '^BUSY$'):
                    guard.lock_fds({'wait_ms': 10, 'files': [
                        {'fd': contender, 'path': str(filename), 'mode': 'shared'},
                    ]})
            self.assertTrue(released)
            self.assertGreater(elapsed, 0.01)
            independent = self.opened(filename)
            guard.fcntl.flock(independent, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
            guard.validate_fd(independent, str(filename), 'exclusive')
        finally:
            if not released:
                os.close(holder)

    def test_wait_deadline_also_blocks_first_attempt_on_the_next_free_fd(self):
        first, second = self.root / 'first-free.gate', self.root / 'second-free.gate'
        first_fd, second_fd = self.opened(first), self.opened(second)
        real_flock = guard.fcntl.flock
        elapsed = 0.0
        attempts = []

        def acquire_then_delay(fd, operation):
            nonlocal elapsed
            attempts.append(fd)
            result = real_flock(fd, operation)
            if fd == first_fd:
                elapsed = 0.011  # Scheduling passes the one 10ms deadline.
            return result

        with mock.patch.object(guard.time, 'monotonic', side_effect=lambda: elapsed), \
                mock.patch.object(guard.time, 'sleep') as sleep, \
                mock.patch.object(guard.fcntl, 'flock', side_effect=acquire_then_delay):
            with self.assertRaisesRegex(guard.GuardError, '^BUSY$'):
                guard.lock_fds({'wait_ms': 10, 'files': [
                    {'fd': first_fd, 'path': str(first), 'mode': 'shared'},
                    {'fd': second_fd, 'path': str(second), 'mode': 'shared'},
                ]})
            sleep.assert_not_called()
        self.assertEqual(attempts, [first_fd])
        guard.validate_fd(first_fd, str(first), 'shared')
        independent = self.opened(second)
        real_flock(independent, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        guard.validate_fd(independent, str(second), 'exclusive')

    def test_default_contention_returns_busy_without_waiting(self):
        filename = self.root / 'resource.lock'
        holder, contender = self.opened(filename), self.opened(filename)
        guard.fcntl.flock(holder, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        with mock.patch.object(guard.time, 'sleep') as sleep:
            with self.assertRaisesRegex(guard.GuardError, '^BUSY$'):
                guard.lock_fds({'files': [
                    {'fd': contender, 'path': str(filename), 'mode': 'shared'},
                ]})
            sleep.assert_not_called()

    def test_invalid_wait_values_are_rejected_before_locking(self):
        for value in (True, False, -1, 5001, 1.5, '1', None):
            with self.subTest(value=value):
                rc, reply = self.helper({'action': 'lock', 'wait_ms': value, 'files': []})
                self.assertEqual(rc, 2)
                self.assertEqual(reply['code'], 'INVALID_RESOURCE_LOCK_WAIT')
        for value in (0, 5000):
            with self.subTest(value=value):
                self.assertEqual(self.helper({'action': 'lock', 'wait_ms': value, 'files': []}),
                                 (0, {'status': 'locked'}))

    def test_wait_still_validates_every_fd_before_acquiring_any_lock(self):
        filename = self.root / 'valid.gate'
        fd = self.opened(filename)
        mismatched = self.root / 'mismatched.gate'
        independent = self.opened(mismatched)
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_LEASE_FD_INVALID'):
            guard.lock_fds({'wait_ms': 5000, 'files': [
                {'fd': fd, 'path': str(filename), 'mode': 'shared'},
                {'fd': independent, 'path': str(filename), 'mode': 'shared'},
            ]})
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_LEASE_NOT_HELD'):
            guard.validate_fd(fd, str(filename), 'shared')

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

"""Fixed manager redirects preserve Windows paths and existing adoption rollback."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from agent_core import pi_plugin_adoption as adoption
from agent_core import pi_plugin_guard as guard


class ServiceActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commit = 'a' * 40
        self.service = self.root / 'releases' / self.commit / 'server'
        self.service.mkdir(parents=True)
        self.names = ('server-service.py', 'next-service.mjs', 'configure-service.py', 'preflight.py',
                      'rebind-managed-services.py', 'rollback-managed-service-rebind.py', 'legacy_idle_stop.py')
        for name in self.names:
            (self.service / name).write_text('# fixed service fixture\n')
        (self.service / 'server-service.py').write_text(
            'import json, os, sys\nprint(json.dumps({"args": sys.argv[1:], "selection": os.getenv("TEST_SELECTION")}))\n'
            'sys.exit(int(os.getenv("TEST_EXIT", "0")))\n')
        self.manifest_path = self.service.parent / 'SERVICE-RELEASE.json'
        self.manifest = {'status': 'PASS', 'sourceCommit': self.commit, 'root': str(self.service),
                         'files': [{'path': name, 'sha256': adoption.sha((self.service / name).read_bytes())}
                                   for name in self.names]}
        self.write_manifest()
        self.entry = self.root / 'old-service' / 'server-service.py'
        self.entry.parent.mkdir()
        self.original = b'#!/usr/bin/env python3\nprint("original manager")\n'
        self.entry.write_bytes(self.original)
        self.entry.chmod(0o750)
        self.target = {'kind': 'service-manager', 'serviceRoot': str(self.service), 'python': sys.executable}
        for name in ('agent', 'project', 'legacy', 'sessions'):
            (self.root / name).mkdir()
        self.settings = self.root / 'agent/settings.json'
        self.settings.write_text('{"packages":[],"userPreference":"preserved"}\n')
        self.plan_file = self.root / 'plan.json'
        self.raw = {'schema': adoption.SCHEMA, 'agentDir': str(self.root / 'agent'),
                    'cwd': str(self.root / 'project'), 'scopes': ['user'],
                    'stateDir': str(self.root / 'transaction'), 'hosts': [{'name': socket.gethostname()}],
                    'scheduler': 'none', 'legacyCodeRoots': [str(self.root / 'legacy')],
                    'sessionRoots': [str(self.root / 'sessions')], 'maintenanceApproved': True,
                    'entryInventoryConfirmed': True, 'hostInventoryConfirmed': True,
                    'entries': [{'path': str(self.entry), 'expectedSha256': adoption.entry_snapshot(self.entry)['sha256'],
                                 'replacement': self.target}]}

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def plan(self):
        adoption.save(self.plan_file, self.raw)
        return adoption.load_plan(self.plan_file)

    def replacement(self):
        return adoption.replacement(self.raw['entries'][0], {})

    def invoke(self, args=(), **env):
        return subprocess.run([sys.executable, '-B', str(self.entry), *args], capture_output=True, text=True,
                              env={**os.environ, **env})

    def install_fixture_redirect(self):
        data, bindings = self.replacement()
        self.entry.write_bytes(data)
        return bindings

    def clear(self, plan):
        return [{'host': socket.gethostname(), 'uid': os.getuid(), 'at': time.time(),
                 'planSha256': plan['_sha256'], 'status': 'clear'}]

    def test_exact_argv_environment_and_exit_status_forwarded(self):
        bindings = self.install_fixture_redirect()
        args = ['ensure', '--config', '/unchanged/path with spaces/service.json', '--json', 'literal $() `text`']
        result = self.invoke(args, TEST_SELECTION='unchanged', TEST_EXIT='23')
        self.assertEqual(result.returncode, 23)
        self.assertEqual(json.loads(result.stdout), {'args': args, 'selection': 'unchanged'})
        self.assertEqual(len(bindings), len(self.names) + 1)
        self.assertIn(str(self.service / 'legacy_idle_stop.py'), [item['path'] for item in bindings])

    def test_only_explicit_kind_and_existing_manager_entry_are_supported(self):
        self.raw['entries'][0]['path'] = str(self.root / 'arbitrary.py')
        with self.assertRaisesRegex(guard.GuardError, 'INVALID_SERVICE_MANAGER_REPLACEMENT'):
            self.replacement()
        self.raw['entries'][0]['path'] = str(self.entry)
        self.target['entrypoint'] = 'arbitrary.py'
        with self.assertRaisesRegex(guard.GuardError, 'INVALID_SERVICE_MANAGER_REPLACEMENT'):
            self.replacement()
        del self.target['entrypoint']
        self.target['kind'] = 'arbitrary'
        with self.assertRaisesRegex(guard.GuardError, 'INVALID_ENTRY_REPLACEMENT_KIND'):
            self.replacement()

    def test_missing_or_wrong_release_identity_refuses_fence_before_entry_write(self):
        plan = self.plan()
        self.manifest_path.unlink()
        with self.assertRaisesRegex(guard.GuardError, 'SERVICE_RELEASE_MANIFEST_REQUIRED'):
            adoption.fence(plan)
        self.assertEqual(self.entry.read_bytes(), self.original)
        for key, wrong in [('status', 'FAIL'), ('sourceCommit', 'b' * 40), ('root', '/other/server')]:
            original = self.manifest[key]
            self.manifest[key] = wrong
            self.write_manifest()
            with self.assertRaisesRegex(guard.GuardError, 'SERVICE_RELEASE_IDENTITY_MISMATCH'):
                self.replacement()
            self.manifest[key] = original

    def test_every_manifest_file_is_verified_including_legacy_stop_module(self):
        for name in self.names:
            with self.subTest(name=name):
                path = self.service / name
                original = path.read_bytes()
                path.write_bytes(original + b'# modified\n')
                with self.assertRaisesRegex(guard.GuardError, 'SERVICE_RELEASE_FILE_CHANGED'):
                    self.replacement()
                path.write_bytes(original)

    def test_unsafe_duplicate_missing_or_unlisted_module_refused(self):
        original = list(self.manifest['files'])
        for entries in [original + [original[0]], original[:-1],
                        [{'path': '../arbitrary.py', 'sha256': 'a' * 64}]]:
            self.manifest['files'] = entries
            self.write_manifest()
            with self.assertRaises(guard.GuardError):
                self.replacement()
        self.manifest['files'] = original
        self.write_manifest()
        (self.service / 'unexpected.py').write_text('pass\n')
        with self.assertRaisesRegex(guard.GuardError, 'SERVICE_RELEASE_UNLISTED_FILE'):
            self.replacement()

    def test_post_generation_manifest_or_module_tamper_refused_at_invocation(self):
        self.install_fixture_redirect()
        original = self.manifest_path.read_bytes()
        self.manifest_path.write_bytes(original + b'\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.manifest_path.write_bytes(original)
        (self.service / 'legacy_idle_stop.py').write_text('# different\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_service_activate_keeps_marker_trusted_and_rollback_restores_exact_entry(self):
        plan = self.plan()
        adoption.fence(plan)
        adoption.adopt(plan, collect=self.clear)
        result = adoption.entries_action(plan, activate=True)
        self.assertEqual(result['serviceManagerRedirects'], [str(self.entry)])
        self.assertFalse(result['oldServersRemainFenced'])
        self.assertEqual(self.invoke(['status', '--json']).returncode, 0)
        self.assertTrue(all(not row['legacyUnverified'] for row in guard.plan(plan)['scopes']))
        self.settings.write_text('{"packages":[],"userPreference":"new independent change"}\n')
        adoption.rollback(plan)
        self.assertEqual(self.invoke().returncode, 75)
        self.assertEqual(adoption.rollback(plan), {'status': 'rolled-back', 'changed': False, 'entriesRemainFenced': True})
        adoption.entries_action(plan)
        self.assertEqual(self.entry.read_bytes(), self.original)
        self.assertEqual(self.entry.stat().st_mode & 0o777, 0o750)
        self.assertIn('new independent change', self.settings.read_text())

    def test_tamper_after_fence_blocks_activation_and_invalidates_marker(self):
        plan = self.plan()
        adoption.fence(plan)
        adoption.adopt(plan, collect=self.clear)
        (self.service / 'legacy_idle_stop.py').write_text('# changed\n')
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_BINDING_CHANGED'):
            adoption.entries_action(plan, activate=True)
        self.assertTrue(all(row['legacyUnverified'] for row in guard.plan(plan)['scopes']))
        self.assertEqual(self.invoke().returncode, 75)

    def test_rollback_checks_all_entries_and_saved_fences_before_any_entry_write(self):
        second = self.root / 'other-launcher'
        second.write_text('#!/usr/bin/env python3\nprint("other")\n')
        self.raw['entries'].append({'path': str(second), 'expectedSha256': adoption.entry_snapshot(second)['sha256']})
        plan = self.plan()
        adoption.fence(plan)
        adoption.adopt(plan, collect=self.clear)
        adoption.entries_action(plan, activate=True)
        activated = self.entry.read_bytes()
        original_second_fence = second.read_bytes()
        second.write_text('#!/usr/bin/env python3\nprint("third party changed")\n')
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_ENTRY_CHANGED'):
            adoption.rollback(plan)
        self.assertEqual(self.entry.read_bytes(), activated)
        second.write_bytes(original_second_fence)
        state = Path(plan['stateDir'])
        receipt = adoption.transaction(plan, state)
        receipt['entries'][1]['closed'] = 'bm90IHRoZSBvcmlnaW5hbCBmZW5jZQ=='
        adoption.save(state / 'transaction.json', receipt)
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_FENCE_RECORD_INVALID'):
            adoption.rollback(plan)
        self.assertEqual(self.entry.read_bytes(), activated)

    def test_rollback_refences_activated_cli_and_manager_then_restores_both(self):
        release = self.root / 'core'
        (release / 'agent_core').mkdir(parents=True)
        (release / 'integrations/pi_runtime/dist').mkdir(parents=True)
        (release / 'agent_core/pi_plugin_guard.py').write_text('# legacy-adopted\n')
        (release / 'RELEASE.json').write_text(json.dumps({'releaseId': 'fixed', 'python': {'path': sys.executable},
                                                       'node': {'path': '/fixed/node'}}))
        (release / 'integrations/pi_runtime/dist/build-manifest.json').write_text('{"buildId":"fixed"}')
        (release / 'bio_agents.py').write_text('print("fixed cli")\n')
        config = self.root / 'runtime.json'
        config.write_text(json.dumps({'agentDir': str(self.root / 'agent')}))
        cli = self.root / 'biomind'
        cli_original = b'#!/usr/bin/env python3\nprint("old cli")\n'
        cli.write_bytes(cli_original)
        self.raw['entries'].append({'path': str(cli), 'expectedSha256': adoption.entry_snapshot(cli)['sha256'],
                                   'replacement': {'release': str(release), 'runtimeConfig': str(config)}})
        plan = self.plan()
        adoption.fence(plan)
        adoption.adopt(plan, collect=self.clear)
        adoption.entries_action(plan, activate=True)
        self.assertEqual(subprocess.run([sys.executable, '-B', str(cli)], capture_output=True, text=True).stdout, 'fixed cli\n')
        self.assertEqual(self.invoke().returncode, 0)
        self.assertTrue(all(not row['legacyUnverified'] for row in guard.plan(plan)['scopes']))
        adoption.rollback(plan)
        for entry in (cli, self.entry):
            result = subprocess.run([sys.executable, '-B', str(entry)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 75)
            self.assertIn('BIOMIND_MAINTENANCE_WINDOW', result.stdout)
        self.assertFalse(adoption.rollback(plan)['changed'])
        adoption.entries_action(plan)
        self.assertEqual(cli.read_bytes(), cli_original)
        self.assertEqual(self.entry.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()

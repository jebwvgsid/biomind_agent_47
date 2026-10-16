#!/usr/bin/env python3
"""One stopped transaction changes core and bootstrap; no intermediate launch."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


fixture = load('rebind_fixture', Path(__file__).with_name('test_rebind_managed_services.py'))
mod = fixture.mod
rollback = load('rebind_rollback', Path(__file__).resolve().parents[1] / 'server/rollback-managed-service-rebind.py')


class FixedServiceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.RebindTests('test_default_check_leaves_production_bytes_and_state_untouched')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.service = self.f.root / ('c' * 40) / 'server'
        files = []
        for name in mod.SERVICE_FILES:
            raw = ('synthetic fixed service: ' + name + '\n').encode()
            fixture.put(self.service / name, raw, mode=0o400)
            files.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest()})
        self.manifest = self.service.parent / 'SERVICE-RELEASE.json'
        self.release_data = {'status': 'PASS', 'sourceCommit': 'c' * 40,
                             'root': str(self.service), 'files': files}
        fixture.put(self.manifest, self.release_data, mode=0o400)
        self.f.args.service_root = self.service
        self.f.args.apply = True

    def assert_no_write(self, before):
        self.assertEqual(self.f.tracked(), before)
        self.assertFalse(self.f.args.runtime_output.exists())
        self.assertFalse(self.f.args.receipt.exists())
        self.assertFalse(list(self.f.private.glob('rebind-backup-*')))

    def test_combined_rebind_and_exact_rollback_preserve_original_binding(self):
        f = self.f
        original_config, original_lifecycle = f.config.read_bytes(), f.record.read_bytes()
        original_runtime = f.runtime.read_bytes()
        result = mod.execute(f.args)
        self.assertEqual(result['status'], 'APPLIED')
        self.assertFalse(result['serviceStarted'])
        self.assertFalse(result['signalsSent'])
        self.assertFalse(f.record.exists())
        current = json.loads(f.config.read_text())
        self.assertEqual(current['command'], [str(f.node), str(self.service / 'next-service.mjs')])
        self.assertEqual(current['environment']['BIOMIND_ENTRYPOINT'], str(f.release / 'bio_agents.py'))
        self.assertEqual(current['environmentFile'], f.config_data['environmentFile'])
        self.assertEqual(current['serverId'], f.config_data['serverId'])
        self.assertEqual(result['serviceRelease']['sourceCommit'], 'c' * 40)
        self.assertEqual(result['serviceRelease']['root'], str(self.service))
        self.assertEqual(result['serviceRelease']['launcherBefore'], str(f.manager / 'next-service.mjs'))
        self.assertEqual(result['serviceRelease']['launcherAfter'], current['command'][1])
        # A subsequently stopped instance is archived, then the old genuine
        # record and exact bootstrap/core/config binding are restored together.
        fixture.put(f.record, {'status': 'stopped', 'serviceId': 'service-test',
                              'serverId': 'server-test', 'instanceId': 'new-instance'})
        restored = rollback.execute(argparse.Namespace(receipt=f.args.receipt,
            output_receipt=f.root / 'rollback.json', apply=True))
        self.assertEqual(restored['status'], 'ROLLED_BACK')
        self.assertEqual(f.config.read_bytes(), original_config)
        self.assertEqual(f.record.read_bytes(), original_lifecycle)
        self.assertEqual(f.runtime.read_bytes(), original_runtime)
        self.assertTrue(f.args.runtime_output.is_file())
        self.assertEqual(f.credential.stat().st_mode & 0o777, 0)

    def test_ready_instance_is_refused_before_writes(self):
        fixture.put(self.f.record, {'status': 'ready', 'serviceId': 'service-test', 'serverId': 'server-test'})
        before = self.f.tracked()
        with self.assertRaisesRegex(mod.Refused, 'SERVICE_NOT_STOPPED'):
            mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_each_existing_service_lock_still_blocks_combined_rebind(self):
        for lock in self.f.locks:
            with self.subTest(lock=lock.name):
                before = self.f.tracked()
                fd = os.open(lock, os.O_RDWR)
                try:
                    mod.fcntl.flock(fd, mod.fcntl.LOCK_EX | mod.fcntl.LOCK_NB)
                    with self.assertRaisesRegex(mod.Refused, 'SERVICE_ACTIVE_CONFIGURATION_LOCKED'):
                        mod.execute(self.f.args)
                finally:
                    os.close(fd)
                self.assert_no_write(before)

    def test_unfrozen_worktree_directory_is_refused(self):
        self.f.args.service_root = self.f.manager
        before = self.f.tracked()
        with self.assertRaises(FileNotFoundError):
            mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_directory_and_manifest_commit_must_match(self):
        self.release_data['sourceCommit'] = 'd' * 40
        self.manifest.chmod(0o600)
        fixture.put(self.manifest, self.release_data)
        before = self.f.tracked()
        with self.assertRaisesRegex(mod.Refused, 'FIXED_SERVICE_RELEASE_REQUIRED'):
            mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_manifest_root_must_equal_selected_directory(self):
        self.release_data['root'] = str(self.f.manager)
        self.manifest.chmod(0o600)
        fixture.put(self.manifest, self.release_data)
        before = self.f.tracked()
        with self.assertRaisesRegex(mod.Refused, 'FIXED_SERVICE_RELEASE_REQUIRED'):
            mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_incomplete_or_duplicate_manifest_is_refused(self):
        self.release_data['files'][-1] = self.release_data['files'][0]
        self.manifest.chmod(0o600)
        fixture.put(self.manifest, self.release_data)
        before = self.f.tracked()
        with self.assertRaisesRegex(mod.Refused, 'SERVICE_RELEASE_FILES_INVALID'):
            mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_tampered_bootstrap_is_refused(self):
        (self.service / 'next-service.mjs').chmod(0o600)
        fixture.put(self.service / 'next-service.mjs', b'TAMPERED\n')
        before = self.f.tracked()
        with self.assertRaisesRegex(mod.Refused, 'SERVICE_RELEASE_HASH_MISMATCH'):
            mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_service_symlink_is_refused(self):
        alias = self.f.root / 'service-alias'
        alias.symlink_to(self.service, target_is_directory=True)
        self.f.args.service_root = alias
        before = self.f.tracked()
        with self.assertRaisesRegex(mod.Refused, 'SYMLINK_OR_NONCANONICAL_PATH_REFUSED'):
            mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_script_concurrently_changed_before_locks_is_refused(self):
        before = self.f.tracked()
        prepare = mod.prepare
        def concurrent(args):
            result = prepare(args)
            (self.service / 'server-service.py').chmod(0o600)
            fixture.put(self.service / 'server-service.py', b'CONCURRENT\n')
            return result
        with patch.object(mod, 'prepare', concurrent):
            with self.assertRaisesRegex(mod.Refused, 'COMPARE_AND_SWAP_FAILED'):
                mod.execute(self.f.args)
        self.assert_no_write(before)

    def test_later_user_configuration_change_prevents_destructive_rollback(self):
        f = self.f
        mod.execute(f.args)
        value = json.loads(f.config.read_text())
        value['environment']['BIOMIND_MAX_OWNERS'] = '4'
        fixture.put(f.config, value)
        before = f.config.read_bytes()
        with self.assertRaisesRegex(rollback.r.Refused, 'CURRENT_CONFIG_CHANGED'):
            rollback.execute(argparse.Namespace(receipt=f.args.receipt,
                output_receipt=f.root / 'rollback.json', apply=True))
        self.assertEqual(f.config.read_bytes(), before)
        self.assertFalse((f.root / 'rollback.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)

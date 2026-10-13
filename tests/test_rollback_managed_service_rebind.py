#!/usr/bin/env python3
"""Receipt-based rollback tests on retained synthetic fixtures only."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import unittest

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(file) if Path(file).is_absolute() else Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

fixture = load('fixture_rebind', 'test_rebind_managed_services.py')
rollback = load('rollback_rebind', Path(__file__).resolve().parents[1] / 'server' / 'rollback-managed-service-rebind.py')


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.RebindTests('test_default_check_leaves_production_bytes_and_state_untouched')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        f = self.fixture
        self.original = f.config.read_bytes()
        self.lifecycle = f.record.read_bytes()
        f.args.apply = True
        self.applied = fixture.mod.execute(f.args)
        self.args = argparse.Namespace(receipt=f.args.receipt,
            output_receipt=f.root / 'rollback-receipt.json', apply=False)

    def test_default_check_leaves_config_and_absent_state_unchanged(self):
        f = self.fixture
        before = f.config.read_bytes()
        result = rollback.execute(self.args)
        self.assertEqual(result['status'], 'CHECK_ONLY_READY')
        self.assertEqual(f.config.read_bytes(), before)
        self.assertFalse(f.record.exists())
        self.assertFalse(list(f.private.glob('rollback-backup-*')))

    def test_apply_exact_restore_retains_new_runtime_and_rebind_backups(self):
        f = self.fixture
        runtime = f.args.runtime_output.read_bytes()
        self.args.apply = True
        result = rollback.execute(self.args)
        self.assertEqual(result['status'], 'ROLLED_BACK')
        self.assertEqual(f.config.read_bytes(), self.original)
        self.assertEqual(f.record.read_bytes(), self.lifecycle)
        self.assertEqual(f.args.runtime_output.read_bytes(), runtime)
        self.assertTrue(Path(self.applied['backupDirectory']).is_dir())
        self.assertFalse(result['serviceStarted'])

    def test_busy_lock_refuses_without_new_output(self):
        f = self.fixture
        self.args.apply = True
        fd = os.open(f.state / 'backend.lock', os.O_RDWR)
        try:
            rollback.r.fcntl.flock(fd, rollback.r.fcntl.LOCK_EX | rollback.r.fcntl.LOCK_NB)
            with self.assertRaisesRegex(rollback.r.Refused, 'SERVICE_ACTIVE_CONFIGURATION_LOCKED'):
                rollback.execute(self.args)
        finally:
            os.close(fd)
        self.assertFalse(self.args.output_receipt.exists())
        self.assertFalse(list(f.private.glob('rollback-backup-*')))

    def test_ready_record_refuses(self):
        f = self.fixture
        fixture.put(f.record, {'status': 'ready', 'serviceId': 'service-test', 'serverId': 'server-test'})
        with self.assertRaisesRegex(rollback.r.Refused, 'SERVICE_NOT_STOPPED'):
            rollback.execute(self.args)

    def test_changed_config_refuses(self):
        f = self.fixture
        f.config.write_bytes(f.config.read_bytes() + b'\n')
        with self.assertRaisesRegex(rollback.r.Refused, 'CURRENT_CONFIG_CHANGED'):
            rollback.execute(self.args)

    def test_new_stopped_record_is_archived_before_old_restored(self):
        f = self.fixture
        fixture.put(f.record, {'status': 'stopped', 'serviceId': 'service-test',
                              'serverId': 'server-test', 'instanceId': 'new-fixture-instance'})
        before = f.record.read_bytes()
        self.args.apply = True
        result = rollback.execute(self.args)
        self.assertEqual((Path(result['rollbackBackupDirectory']) / 'retired-lifecycle.json').read_bytes(), before)
        self.assertEqual(f.record.read_bytes(), self.lifecycle)


if __name__ == '__main__':
    unittest.main(verbosity=2)

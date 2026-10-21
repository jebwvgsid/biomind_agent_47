#!/usr/bin/env python3
"""Offline synthetic fixtures only; never touches an installed service."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent / 'rebind-managed-services.py'
spec = importlib.util.spec_from_file_location('rebind_managed', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def put(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = value if isinstance(value, bytes) else json.dumps(value, indent=2).encode()
    path.write_bytes(raw)
    path.chmod(mode)


class RebindTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='biomind-rebind-' + self._testMethodName + '-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / 'private'
        self.private.mkdir(mode=0o700)
        self.state = self.private / 'state'
        self.state.mkdir(mode=0o700)
        self.agent = self.root / 'agent'
        (self.agent / 'sessions').mkdir(parents=True, mode=0o700)
        self.backend = self.root / 'backend'
        self.release = self.root / 'release'
        self.manager = self.root / 'service-v1'
        self.runtime = self.root / 'old-runtime.json'
        self.config = self.private / 'service.json'
        self.credential = self.private / 'runtime-environment.json'
        self.record = self.state / 'service.json'
        self.node = self.root / 'synthetic-node'
        self.python = self.root / 'synthetic-python'
        for p in [self.node, self.python]:
            put(p, b'#!/bin/sh\nexit 99\n', mode=0o700)
        # These interpreters must never execute; the script only stat-checks them.
        for name in ['next-service.mjs', 'server-service.py']:
            put(self.manager / name, b'// synthetic fixture, not executable service\n')
        for name in mod.BACKEND_FILES:
            put(self.backend / name, b'liveSnapshot() displayPendingUI\n')
        sources, artifacts = [], []
        for name in mod.CORE_FILES:
            content = ('synthetic: ' + name + '\n').encode()
            put(self.release / name, content, mode=0o400)
            row = {'path': name, 'sha256': hashlib.sha256(content).hexdigest()}
            if '/dist/' in name:
                row['path'] = name.removeprefix('integrations/pi_runtime/')
                artifacts.append(row)
            else:
                sources.append(row)
        manifest = {'buildId': 'a' * 64, 'sourceCommit': 'b' * 40,
                    'sources': sources, 'artifacts': artifacts}
        mf = self.release / 'integrations/pi_runtime/dist/build-manifest.json'
        put(mf, manifest, mode=0o400)
        put(self.release / 'RELEASE.json', {
            'schema': 'BioMindCoreReleaseV1', 'releaseId': 'a' * 64,
            'sourceCommit': 'b' * 40, 'manifestSha256': hashlib.sha256(mf.read_bytes()).hexdigest(),
            'node': {'path': str(self.node)}, 'python': {'path': str(self.python)},
        }, mode=0o400)
        self.runtime_data = {
            'installRoot': str(self.root / 'old-core'), 'agentDir': str(self.agent),
            'dataRoot': '.biomind/pi-domain', 'registryPath': str(self.root / 'registry.json'),
            'preferencesPath': str(self.root / 'preferences.md'),
            'memoryRoot': str(self.root / 'memory'), 'wikiRoot': str(self.root / 'wiki'),
            'provider': 'fixture-public-metadata', 'model': 'no-model-executed',
            'historyEnabled': False,
        }
        put(self.runtime, self.runtime_data)
        for name in ['registry.json', 'preferences.md']:
            put(self.root / name, b'KEEP-USER-DATA\n')
        put(self.credential, b'OPAQUE SYNTHETIC BYTES NOT VALID JSON\n', mode=0o000)
        self.config_data = {
            'schema': 'BioMindStudioServiceV1', 'serviceId': 'service-test',
            'serverId': 'server-test', 'mode': 'local', 'stateRoot': str(self.state),
            'cwd': str(self.root / 'old-backend'),
            'command': [str(self.node), str(self.manager / 'next-service.mjs')],
            'environmentFile': str(self.credential), 'startupTimeoutSeconds': 180,
            'environment': {
                'BIOMIND_RUNTIME_CONFIG': str(self.runtime),
                'BIOMIND_ENTRYPOINT': str(self.root / 'old-core/bio_agents.py'),
                'BIOMIND_SESSION_ROOT': str(self.agent / 'sessions'),
                'BIOMIND_PROJECT_ROOTS_JSON': '["/synthetic/project"]',
                'BIOMIND_DATA_ROOTS_JSON': '["/synthetic/data"]',
                'PI_WEB_UPLOADS_DIR': '/synthetic/uploads',
                'BIOMIND_MAX_OWNERS': '8',
            }, 'slurm': {},
        }
        put(self.config, self.config_data)
        put(self.record, {'status': 'stopped', 'serviceId': 'service-test',
                          'serverId': 'server-test', 'instanceId': 'old-fixture-instance',
                          'binding': 'old-binding', 'pid': 987654})
        self.locks = [self.private / 'configure.lock', self.state / 'service.lock',
                      self.state / 'backend.lock']
        for p in self.locks:
            put(p, b'')
        self.args = argparse.Namespace(service_config=self.config, release=self.release,
            backend_root=self.backend, runtime_output=self.root / 'new-runtime.json',
            receipt=self.root / 'receipt.json', apply=False)

    def tracked(self):
        paths = [self.config, self.runtime, self.record,
                 self.root / 'registry.json', self.root / 'preferences.md', *self.locks]
        return {str(p): p.read_bytes() for p in paths if p.exists()}

    def test_default_check_leaves_production_bytes_and_state_untouched(self):
        before = self.tracked()
        result = mod.execute(self.args)
        self.assertEqual(result['status'], 'CHECK_ONLY_READY')
        self.assertEqual(before, self.tracked())
        self.assertFalse(self.args.runtime_output.exists())
        self.assertFalse(list(self.private.glob('rebind-backup-*')))
        self.assertTrue(self.args.receipt.exists())

    def test_apply_preserves_data_credentials_identity_and_exact_backups(self):
        before = self.tracked()
        self.args.apply = True
        original_read_bytes, original_read_text = Path.read_bytes, Path.read_text
        def guarded_bytes(p):
            if p == self.credential:
                raise AssertionError('credential read attempted')
            return original_read_bytes(p)
        def guarded_text(p, *a, **kw):
            if p == self.credential:
                raise AssertionError('credential read attempted')
            return original_read_text(p, *a, **kw)
        with patch.object(Path, 'read_bytes', guarded_bytes), patch.object(Path, 'read_text', guarded_text):
            result = mod.execute(self.args)
        self.assertEqual(result['status'], 'APPLIED')
        self.assertFalse(result['serviceStarted'])
        self.assertFalse(self.record.exists())
        backup = Path(result['backupDirectory'])
        for name, source in [('config', self.config), ('runtime', self.runtime), ('lifecycle', self.record)]:
            self.assertEqual((backup / (name + '.json')).read_bytes(), before[str(source)])
        self.assertEqual((backup / 'retired-lifecycle.json').read_bytes(), before[str(self.record)])
        new = json.loads(self.config.read_text())
        for field in ['serverId', 'serviceId', 'stateRoot', 'environmentFile', 'mode', 'slurm']:
            self.assertEqual(new[field], self.config_data[field])
        runtime = json.loads(self.args.runtime_output.read_text())
        self.assertEqual(runtime['installRoot'], str(self.release))
        self.assertEqual(runtime['python'], str(self.python))
        for key in set(self.runtime_data) - {'installRoot', 'python'}:
            self.assertEqual(runtime[key], self.runtime_data[key])
        self.assertEqual(new['environment']['BIOMIND_SESSION_ROOT'], str(self.agent / 'sessions'))
        self.assertEqual(self.runtime.read_bytes(), before[str(self.runtime)])
        self.assertEqual((self.root / 'registry.json').read_bytes(), before[str(self.root / 'registry.json')])
        self.assertEqual(self.credential.stat().st_mode & 0o777, 0)
        self.assertEqual(self.args.runtime_output.stat().st_mode & 0o777, 0o600)

    def test_each_busy_lock_rejects_without_any_new_file(self):
        self.args.apply = True
        for lock in self.locks:
            before = self.tracked()
            fd = os.open(lock, os.O_RDWR)
            try:
                mod.fcntl.flock(fd, mod.fcntl.LOCK_EX | mod.fcntl.LOCK_NB)
                with self.assertRaisesRegex(mod.Refused, 'SERVICE_ACTIVE_CONFIGURATION_LOCKED'):
                    mod.execute(self.args)
            finally:
                os.close(fd)
            self.assertEqual(before, self.tracked())
            self.assertFalse(self.args.runtime_output.exists())
            self.assertFalse(self.args.receipt.exists())
            self.assertFalse(list(self.private.glob('rebind-backup-*')))

    def test_ready_service_refused_without_lock_or_config_changes(self):
        put(self.record, {'status': 'ready', 'serviceId': 'service-test', 'serverId': 'server-test'})
        before = self.tracked()
        self.args.apply = True
        with self.assertRaisesRegex(mod.Refused, 'SERVICE_NOT_STOPPED'):
            mod.execute(self.args)
        self.assertEqual(before, self.tracked())
        self.assertFalse(self.args.receipt.exists())

    def test_inline_password_refused_before_any_write(self):
        self.config_data['environment']['PI_WEB_PASSWORD'] = 'SYNTHETIC-NOT-A-REAL-SECRET'
        put(self.config, self.config_data)
        before = self.tracked()
        with self.assertRaisesRegex(mod.Refused, 'INLINE_SECRET_FIELD_REFUSED'):
            mod.execute(self.args)
        self.assertEqual(before, self.tracked())

    def test_unknown_environment_refused(self):
        self.config_data['environment']['UNREVIEWED_SETTING'] = 'value'
        put(self.config, self.config_data)
        with self.assertRaisesRegex(mod.Refused, 'ENVIRONMENT_ALLOWLIST_REFUSED'):
            mod.execute(self.args)

    def test_runtime_pointing_at_credentials_refused_before_read(self):
        self.config_data['environment']['BIOMIND_RUNTIME_CONFIG'] = str(self.credential)
        put(self.config, self.config_data)
        with self.assertRaisesRegex(mod.Refused, 'NONSECRET_JSON_PATH_REQUIRED'):
            mod.execute(self.args)
        self.assertFalse(self.args.runtime_output.exists())

    def test_failed_lifecycle_retirement_keeps_stopped_record_and_backup(self):
        original = os.replace
        def fail_retirement(source, destination):
            if Path(source) == self.record:
                raise OSError('synthetic disk publication failure')
            return original(source, destination)
        self.args.apply = True
        old = self.record.read_bytes()
        with patch.object(os, 'replace', fail_retirement):
            with self.assertRaises(OSError):
                mod.execute(self.args)
        self.assertEqual(self.record.read_bytes(), old)
        backups = list(self.private.glob('rebind-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'lifecycle.json').read_bytes(), old)
        self.assertEqual(json.loads(self.config.read_text())['environment']['BIOMIND_RUNTIME_CONFIG'], str(self.args.runtime_output))
        self.assertFalse(self.args.receipt.exists())

    def test_runtime_output_existing_refused_without_overwrite(self):
        put(self.args.runtime_output, b'EXISTING\n')
        with self.assertRaisesRegex(mod.Refused, 'OUTPUT_ALREADY_EXISTS'):
            mod.execute(self.args)
        self.assertEqual(self.args.runtime_output.read_bytes(), b'EXISTING\n')

    def test_symlink_runtime_output_refused(self):
        self.args.runtime_output.symlink_to(self.runtime)
        with self.assertRaisesRegex(mod.Refused, 'SYMLINK_OR_NONCANONICAL_PATH_REFUSED'):
            mod.execute(self.args)

    def test_manifest_tampered_entry_refused(self):
        (self.release / 'bio_agents.py').chmod(0o600)
        put(self.release / 'bio_agents.py', b'TAMPERED\n')
        with self.assertRaisesRegex(mod.Refused, 'RELEASE_ENTRY_HASH_MISMATCH'):
            mod.execute(self.args)

    def test_runtime_cas_change_refused_before_backup_or_mutation(self):
        original = mod.prepare
        def concurrent(args):
            result = original(args)
            self.runtime.write_bytes(self.runtime.read_bytes() + b'\n')
            return result
        self.args.apply = True
        before_config = self.config.read_bytes()
        with patch.object(mod, 'prepare', concurrent):
            with self.assertRaisesRegex(mod.Refused, 'COMPARE_AND_SWAP_FAILED'):
                mod.execute(self.args)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertFalse(list(self.private.glob('rebind-backup-*')))
        self.assertFalse(self.args.runtime_output.exists())

    def test_missing_lock_refused_without_creating_it(self):
        # Only remove this test's own empty fixture lock; no real service path.
        self.locks[2].unlink()
        with self.assertRaises(FileNotFoundError):
            mod.execute(self.args)
        self.assertFalse(self.locks[2].exists())
        self.assertFalse(self.args.receipt.exists())

    def test_scope_mismatch_refused(self):
        self.config_data['environment']['BIOMIND_SESSION_ROOT'] = str(self.agent)
        put(self.config, self.config_data)
        with self.assertRaisesRegex(mod.Refused, 'SESSION_ROOT_MISMATCH'):
            mod.execute(self.args)

    def shared_sessions(self):
        alias = self.agent / 'sessions'
        alias.rmdir()  # This test's newly created empty directory only.
        target = self.root / 'shared-sessions'
        target.mkdir(mode=0o700)
        alias.symlink_to(target, target_is_directory=True)
        self.config_data['environment']['BIOMIND_SESSION_ROOT'] = str(target)
        put(self.config, self.config_data)
        return alias, target

    def test_shared_session_alias_preserved_on_apply(self):
        alias, target = self.shared_sessions()
        put(target / 'existing-session.jsonl', b'EXISTING USER SESSION\n')
        identity = alias.lstat().st_ino
        self.args.apply = True
        result = mod.execute(self.args)
        self.assertEqual(result['status'], 'APPLIED')
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.lstat().st_ino, identity)
        self.assertEqual(alias.resolve(), target)
        self.assertEqual((target / 'existing-session.jsonl').read_bytes(), b'EXISTING USER SESSION\n')
        runtime = json.loads(self.args.runtime_output.read_text())
        self.assertEqual(runtime['agentDir'], str(self.agent))
        self.assertNotIn('sessionDir', runtime)

    def test_shared_alias_wrong_target_refused(self):
        alias, target = self.shared_sessions()
        wrong = self.root / 'wrong-sessions'
        wrong.mkdir(mode=0o700)
        self.config_data['environment']['BIOMIND_SESSION_ROOT'] = str(wrong)
        put(self.config, self.config_data)
        with self.assertRaisesRegex(mod.Refused, 'SESSION_ROOT_MISMATCH'):
            mod.execute(self.args)
        self.assertFalse(self.args.runtime_output.exists())

    def test_shared_alias_replacement_refused_under_locks(self):
        alias, target = self.shared_sessions()
        original_prepare = mod.prepare
        def replaced(args):
            result = original_prepare(args)
            alias.rename(self.root / 'original-alias')
            alias.symlink_to(target, target_is_directory=True)
            return result
        self.args.apply = True
        before = self.config.read_bytes()
        with patch.object(mod, 'prepare', replaced):
            with self.assertRaisesRegex(mod.Refused, 'SESSION_BINDING_CHANGED'):
                mod.execute(self.args)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.args.runtime_output.exists())
        self.assertFalse(list(self.private.glob('rebind-backup-*')))



if __name__ == '__main__':
    unittest.main(verbosity=2)

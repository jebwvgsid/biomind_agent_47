#!/usr/bin/env python3
"""Offline recovery guards. All process inspection/signals/restarts are fakes."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('repair', Path(__file__).with_name('repair-unready-startup.py'))
repair = importlib.util.module_from_spec(spec); spec.loader.exec_module(repair)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / 'deployment/private'; self.installed = self.root / 'deployment/service-v1'
        self.state = self.private / 'state'; self.logs = self.state / 'logs'; self.source = self.root / 'server/next-service.mjs'
        for path in (self.private, self.installed, self.logs, self.source.parent): path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.manager = self.installed / 'server-service.py'; self.launcher = self.installed / 'next-service.mjs'
        self.write(self.manager, b'original known manager'); self.write(self.launcher, b'original broken launcher'); self.write(self.source, b'new tested launcher')
        self.secret = self.private / 'runtime-environment.json'; self.write(self.secret, b'SYNTHETIC_PASSWORD_MUST_NEVER_BE_READ')
        self.secret_meta = repair.metadata(self.secret)
        self.config_path = self.private / 'service.json'
        self.config = dict(schema='BioMindStudioServiceV1', mode='local', serverId='server-1', serviceId='studio',
            command=['/synthetic/node', str(self.launcher)], stateRoot=str(self.state), environmentFile=str(self.secret))
        self.write_json(self.config_path, self.config); self.original_config = self.config_path.read_bytes()
        self.record = dict(schema='BioMindStudioServiceV1', mode='local', serverId='server-1', serviceId='studio',
            instanceId='a' * 32, status='slow-start', holderStarted=True, host=socket.gethostname(), pid=111, startTicks='12345')
        self.write_json(self.state / 'service.json', self.record)
        self.write(self.state / 'service.lock', b''); self.write(self.state / 'backend.lock', b'')
        self.log = self.logs / ('a' * 32 + '.backend.log'); self.write(self.log, b'uncaughtException: Error: PROFILE_TIMEOUT\n')
        for name, file in [('OLD_MANAGER_SHA256', self.manager), ('OLD_LAUNCHER_SHA256', self.launcher), ('NEW_LAUNCHER_SHA256', self.source)]:
            patcher = patch.object(repair, name, hashlib.sha256(file.read_bytes()).hexdigest()); patcher.start(); self.addCleanup(patcher.stop)
        fixture = self
        class FakeHost:
            def __init__(self):
                self.signals = []; self.restarts = []; self.closed = []; self.inspections = 0; self.timeout = False; self.before_terminate = None
                self.processes = {111: dict(pid=111, parent=1, ticks='12345', children=[222], argv=['/synthetic/python', str(fixture.manager), 'run', '--config', str(fixture.config_path), '--instance', 'a' * 32]),
                                  222: dict(pid=222, parent=111, ticks='45678', children=[], argv=fixture.config['command'])}
            def process(self, pid):
                self.inspections += 1
                if pid not in self.processes: raise repair.Refused('PROCESS_NOT_VISIBLE_USE_HOST_SSH')
                return copy.deepcopy(self.processes[pid])
            def open_pid(self, pid): self.bound = pid; return 999
            def terminate(self, fd):
                self.signals.append((fd, self.bound))
                if self.before_terminate: self.before_terminate()
            def wait(self, fd, timeout):
                # The actual holder also needs this lock before it can exit.
                with repair.lock_existing(fixture.state / 'service.lock'): pass
                if self.timeout: return False
                value = json.loads((fixture.state / 'service.json').read_text()); value['status'] = 'stopped'; fixture.write_json(fixture.state / 'service.json', value)
                return True
            def close_pid(self, fd): self.closed.append(fd)
            def ensure(self, manager, config):
                self.restarts.append((manager, config))
                return dict(schema='BioMindStudioServiceV1', serverId='server-1', serviceId='studio', status='starting', instanceId='b' * 32, reused=False)
        self.host = FakeHost()
        real_read_file = repair.read_file
        def guarded_read(path, *args, **kwargs):
            if Path(path) == self.secret: raise AssertionError('credential contents were read')
            return real_read_file(path, *args, **kwargs)
        guard = patch.object(repair, 'read_file', guarded_read); guard.start(); self.addCleanup(guard.stop)

    def write(self, path, data):
        path.write_bytes(data); path.chmod(0o600)

    def write_json(self, path, value):
        self.write(path, json.dumps(value).encode())

    def run_repair(self, apply=False):
        return repair.repair(self.config_path, self.source, apply, self.host)

    def assert_refused(self, code):
        value = self.run_repair(True)
        self.assertEqual(value['status'], 'refused'); self.assertEqual(value['code'], code)
        self.assertEqual(self.host.signals, []); self.assertEqual(self.host.restarts, [])
        self.assertEqual(self.launcher.read_bytes(), b'original broken launcher')

    def test_dry_run_never_writes_signals_or_starts(self):
        value = self.run_repair(); self.assertEqual(value['status'], 'verified'); self.assertFalse(value['applied'])
        self.assertEqual(self.host.signals, []); self.assertEqual(self.host.restarts, []); self.assertEqual(list(self.installed.glob('*.backup-*')), [])

    def test_verified_repair_only_upgrades_launcher_and_restarts_same_config(self):
        value = self.run_repair(True)
        self.assertEqual(value['status'], 'restart-requested'); self.assertEqual(value['serviceStatus'], 'starting')
        self.assertTrue(value['applied']); self.assertEqual(self.launcher.read_bytes(), b'new tested launcher')
        self.assertEqual(Path(value['launcherBackup']).read_bytes(), b'original broken launcher')
        self.assertEqual(self.config_path.read_bytes(), self.original_config); self.assertEqual(repair.metadata(self.secret), self.secret_meta)
        self.assertEqual(self.host.signals, [(999, 111)]); self.assertEqual(self.host.closed, [999]); self.assertEqual(self.host.restarts, [(self.manager, self.config_path)])
        self.assertGreaterEqual(self.host.inspections, 6)

    def test_healthy_or_published_endpoint_is_never_stopped(self):
        for mutate in ('ready', 'port', 'endpoint'):
            with self.subTest(mutate=mutate):
                value = copy.deepcopy(self.record)
                if mutate == 'ready': value['status'] = 'ready'
                if mutate == 'port': value['port'] = 32123
                if mutate == 'endpoint': self.write_json(self.state / ('a' * 32 + '.endpoint.json'), {'status': 'ready'})
                self.write_json(self.state / 'service.json', value)
                self.assert_refused('SERVICE_NOT_UNREADY_STARTUP' if mutate == 'ready' else 'SERVICE_ALREADY_HAS_ENDPOINT')
                (self.state / ('a' * 32 + '.endpoint.json')).unlink(missing_ok=True)

    def test_missing_pid_is_not_treated_as_dead_or_restarted(self):
        del self.host.processes[111]; self.assert_refused('PROCESS_NOT_VISIBLE_USE_HOST_SSH')

    def test_pid_reuse_and_foreign_command_are_refused(self):
        self.host.processes[111]['ticks'] = 'other'; self.assert_refused('HOLDER_START_TICKS_CHANGED')
        self.host.processes[111]['ticks'] = '12345'; self.host.processes[111]['argv'][1] = '/other/server-service.py'; self.assert_refused('HOLDER_COMMAND_MISMATCH')

    def test_native_owner_child_prevents_any_patch_or_signal(self):
        self.host.processes[222]['children'] = [333]; self.assert_refused('BACKEND_HAS_CHILDREN_REFUSE_TASK_INTERRUPTION')

    def test_timeout_text_without_original_uncaught_failure_is_insufficient(self):
        self.write(self.log, b'PROFILE_TIMEOUT retry may continue\n'); self.assert_refused('KNOWN_STARTUP_FAILURE_NOT_CONFIRMED')
        self.write(self.log, b'uncaughtException: Error: PROFILE_TIMEOUT\nservice_ready\n'); self.assert_refused('KNOWN_STARTUP_FAILURE_NOT_CONFIRMED')

    def test_changed_candidate_or_installed_source_refuses_before_any_signal(self):
        self.write(self.source, b'changed candidate')
        with self.assertRaisesRegex(repair.Refused, 'REPAIR_SOURCE_VERSION_MISMATCH'): self.run_repair(True)
        self.assertEqual(self.host.signals, []); self.assertEqual(self.launcher.read_bytes(), b'original broken launcher')

    def test_stop_timeout_never_escalates_to_force_kill_or_starts_another_service(self):
        self.host.timeout = True; value = self.run_repair(True)
        self.assertEqual(value['code'], 'HOLDER_STOP_TIMEOUT_NO_FORCE_KILL'); self.assertTrue(value['applied'])
        self.assertEqual(self.host.signals, [(999, 111)]); self.assertEqual(self.host.restarts, [])

    def test_new_endpoint_before_signal_is_rechecked_and_blocks_signal(self):
        original = repair.atomic_upgrade
        def race(context, instance):
            value = original(context, instance); self.write_json(self.state / ('a' * 32 + '.endpoint.json'), {'status': 'ready'}); return value
        with patch.object(repair, 'atomic_upgrade', race): value = self.run_repair(True)
        self.assertEqual(value['code'], 'SERVICE_ALREADY_HAS_ENDPOINT'); self.assertTrue(value['applied'])
        self.assertEqual(self.host.signals, []); self.assertEqual(self.host.restarts, [])

    def test_surviving_backend_lease_prevents_duplicate_restart(self):
        with repair.lock_existing(self.state / 'backend.lock'):
            value = self.run_repair(True)
        self.assertEqual(value['code'], 'SERVICE_LOCK_BUSY'); self.assertTrue(value['applied'])
        self.assertEqual(self.host.signals, [(999, 111)]); self.assertEqual(self.host.restarts, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)

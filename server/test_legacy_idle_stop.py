"""Legacy migration boundaries; real pidfd/holder test uses simulated HTTP only."""
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


service = module('legacy_lifecycle_test_service', 'server-service.py')
legacy = module('legacy_lifecycle_test_compat', 'legacy_idle_stop.py')


class LegacyStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='biomind-legacy-stop-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_file = self.root / 'service.json'
        self.raw = dict(schema=service.SCHEMA, serviceId='legacy-test', serverId='biomind-legacy-test',
                        mode='local', stateRoot=str(self.root / 'state'), cwd=str(self.root),
                        command=[sys.executable, str(self.root / 'bootstrap.py')])
        self.config_file.write_text(json.dumps(self.raw))
        self.config = service.load(self.config_file)
        self.instance = 'a' * 32
        self.record = dict(schema=service.SCHEMA, serviceId=self.config['serviceId'],
            serverId=self.config['serverId'], instanceId=self.instance, host=socket.gethostname(),
            binding=self.config['binding'], status='ready', holderStarted=True, pid=12345, startTicks='200')
        service.write_record(self.config, self.record)
        self.transaction = dict(id='fixture-transaction', entries=[])
        self.holder = dict(pid=12345, startTicks='200', ppid=1, executable='/python', commandSha256='a',
                           children=[12346], argv=[])
        self.backend = dict(pid=12346, startTicks='201', ppid=12345, executable='/node', commandSha256='b',
                            children=[], argv=[])

    @contextlib.contextmanager
    def fence(self, *args):
        yield self.transaction, lambda: None

    def invoke(self, **kwargs):
        return legacy.stop_legacy_idle(self.config, self.instance, '/fixed/core', '/frozen/plan.json',
            clients_quiescent=True, idle_confirmed=True, timeout=.01, service=service, **kwargs)

    @contextlib.contextmanager
    def isolated_checks(self):
        read_text = Path.read_text
        close = os.close

        def read(path, *args, **kwargs):
            return 'systemd\n' if str(path) == '/proc/1/comm' else read_text(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read), \
             patch.object(legacy, 'maintenance_fence', self.fence), \
             patch.object(service, 'process_alive', return_value=True), \
             patch.object(service, 'health', return_value={'port': 1234, 'idleStopProtocol': None}), \
             patch.object(service, 'backend_lease_held', return_value=True), \
             patch.object(legacy, 'ownerless_pair', return_value=(self.holder, self.backend)), \
             patch.object(legacy, 'authenticated_idle'), \
             patch.object(legacy.os, 'pidfd_open', side_effect=[91, 92]), \
             patch.object(legacy, 'pidfd_exited', return_value=False), \
             patch.object(legacy.os, 'close', side_effect=lambda fd: None if fd in (91, 92) else close(fd)), \
             patch.object(legacy.signal, 'pidfd_send_signal') as sent:
            yield sent

    def test_explicit_window_and_idle_confirmation_are_both_required(self):
        for clients, idle in [(False, True), (True, False), (False, False)]:
            with self.subTest(clients=clients, idle=idle), patch.object(legacy, 'maintenance_fence') as receipt:
                with self.assertRaisesRegex(service.Refused, 'LEGACY_MAINTENANCE_WINDOW_REQUIRED'):
                    legacy.stop_legacy_idle(self.config, self.instance, '/core', '/plan',
                        clients_quiescent=clients, idle_confirmed=idle, service=service)
                receipt.assert_not_called()

    def test_restricted_container_is_rejected_without_signals(self):
        with patch.object(Path, 'read_text', return_value='python\n'), patch.object(legacy, 'maintenance_fence') as receipt:
            with self.assertRaisesRegex(service.Refused, 'RUN_ON_REAL_SERVICE_HOST_REQUIRED'):
                self.invoke()
            receipt.assert_not_called()

    def test_slurm_is_not_automatically_included(self):
        self.config['mode'] = 'slurm'
        with self.assertRaisesRegex(service.Refused, 'LEGACY_LOCAL_SERVICE_REQUIRED'):
            self.invoke()

    def test_wrong_instance_binding_host_and_state_never_signal(self):
        cases = [('instanceId', 'b' * 32, 'SERVICE_INSTANCE_REPLACED'),
                 ('binding', 'different', 'ACTIVE_CONFIG_BINDING_CHANGED'),
                 ('host', 'foreign', 'RUN_ON_SERVICE_HOST_REQUIRED'),
                 ('status', 'starting', 'SERVICE_PROCESS_IDENTITY_UNCONFIRMED'),
                 ('holderStarted', False, 'SERVICE_PROCESS_IDENTITY_UNCONFIRMED')]
        for key, value, code in cases:
            with self.subTest(key=key), self.isolated_checks() as sent:
                service.write_record(self.config, {**self.record, key: value})
                with self.assertRaisesRegex(service.Refused, code):
                    self.invoke()
                sent.assert_not_called()

    def test_native_protocol_never_falls_back_to_a_signal(self):
        with self.isolated_checks() as sent, patch.object(service, 'health', return_value={'port': 1234, 'idleStopProtocol': 1}):
            with self.assertRaisesRegex(service.Refused, 'USE_NATIVE_IDLE_STOP'):
                self.invoke()
            sent.assert_not_called()

    def test_authentication_or_process_tree_refusal_never_signals(self):
        for target in ('authenticated_idle', 'ownerless_pair'):
            with self.subTest(target=target), self.isolated_checks() as sent, \
                 patch.object(legacy, target, side_effect=service.Refused('FIXTURE_REFUSED')):
                with self.assertRaisesRegex(service.Refused, 'FIXTURE_REFUSED'):
                    self.invoke()
                sent.assert_not_called()
                self.assertEqual(service.read_record(self.config)['status'], 'ready')

    def test_pid_identity_change_or_exit_never_signals(self):
        with self.isolated_checks() as sent, patch.object(legacy, 'ownerless_pair',
            side_effect=[(self.holder, self.backend), ({**self.holder, 'startTicks': '999'}, self.backend)]):
            with self.assertRaisesRegex(service.Refused, 'LEGACY_PROCESS_CHANGED'):
                self.invoke()
            sent.assert_not_called()
        with self.isolated_checks() as sent, patch.object(legacy, 'pidfd_exited', return_value=True):
            with self.assertRaisesRegex(service.Refused, 'LEGACY_PROCESS_CHANGED'):
                self.invoke()
            sent.assert_not_called()

    def test_timeout_sends_once_and_never_forges_or_retries(self):
        with self.isolated_checks() as sent:
            with self.assertRaisesRegex(service.Refused, 'SERVICE_STOP_UNCONFIRMED'):
                self.invoke()
            sent.assert_called_once_with(91, signal.SIGTERM, None, 0)
            self.assertEqual(service.read_record(self.config), self.record)

    def test_nonzero_exit_and_lingering_lease_are_not_success(self):
        for exit_code, lease, code in [(1, False, 'LEGACY_GRACEFUL_EXIT_FAILED'),
                                       (0, True, 'LEGACY_BACKEND_LEASE_UNCONFIRMED')]:
            with self.subTest(exit_code=exit_code, lease=lease), self.isolated_checks() as sent:
                service.write_record(self.config, self.record)
                def stop(*args):
                    service.write_record(self.config, {**self.record, 'status': 'stopped', 'exitCode': exit_code})
                sent.side_effect = stop
                with patch.object(legacy, 'pidfd_exited', side_effect=[False, False, True, True]), \
                     patch.object(service, 'backend_lease_held', side_effect=[True, lease]):
                    with self.assertRaisesRegex(service.Refused, code):
                        self.invoke()
                self.assertEqual(sent.call_count, 1)

    def test_only_reviewed_fenced_holder_and_reviewed_bootstrap_are_accepted(self):
        manager = self.root / 'manager.py'
        bootstrap = self.root / 'bootstrap.py'
        original = b'# reviewed fixture holder\n'
        bootstrap.write_bytes(b'# reviewed fixture bootstrap\n')
        holder = {**self.holder, 'executable': str(Path(sys.executable).resolve()),
            'argv': [sys.executable, str(manager), 'run', '--config', self.config['configPath'], '--instance', self.instance]}
        backend = {**self.backend, 'executable': str(Path(sys.executable).resolve()), 'argv': self.config['command']}
        transaction = {'entries': [{'path': str(manager), 'original': {'kind': 'file', 'data': base64.b64encode(original).decode()}}]}
        with patch.object(legacy, 'host_process', side_effect=lambda pid, _: holder if pid == holder['pid'] else backend), \
             patch.object(legacy, 'LEGACY_HOLDER_SHA256', hashlib.sha256(original).hexdigest()), \
             patch.object(legacy, 'LEGACY_BOOTSTRAP_SHA256', hashlib.sha256(bootstrap.read_bytes()).hexdigest()):
            self.assertEqual(legacy.ownerless_pair(self.config, self.record, transaction, service), (holder, backend))
            with self.assertRaisesRegex(service.Refused, 'LEGACY_HOLDER_NOT_FENCED'):
                legacy.ownerless_pair(self.config, self.record, {'entries': []}, service)
            backend['children'] = [777]
            with self.assertRaisesRegex(service.Refused, 'LEGACY_PROCESS_TREE_NOT_OWNERLESS'):
                legacy.ownerless_pair(self.config, self.record, transaction, service)
            backend['children'] = []
            bootstrap.write_text('changed')
            with self.assertRaisesRegex(service.Refused, 'LEGACY_BOOTSTRAP_NOT_REVIEWED'):
                legacy.ownerless_pair(self.config, self.record, transaction, service)

    def test_authentication_uses_existing_password_without_redirect_or_body_export(self):
        def response(value):
            result = Mock(status=200)
            result.read.return_value = json.dumps(value).encode()
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(return_value=False)
            return result
        opener = Mock()
        opener.open.side_effect = [response({'profile': 'biomind', 'serverId': self.config['serverId']}),
                                   response({'runningSessionIds': []})]
        with patch.object(service, 'environment', return_value={'PI_WEB_PASSWORD': 'fixture-password'}), \
             patch.object(legacy.urllib.request, 'build_opener', return_value=opener):
            legacy.authenticated_idle(self.config, self.instance, {'port': 1234}, service)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual([request.full_url for request in requests],
                         ['http://127.0.0.1:1234/api/agent/profile', 'http://127.0.0.1:1234/api/agent/running'])
        self.assertTrue(all(request.get_method() == 'GET' and request.get_header('Authorization').startswith('Basic ') for request in requests))

    def test_maintenance_receipt_reuses_core_lock_and_fence_validation(self):
        core = self.root / 'fixed-core'
        (core / 'integrations/pi_runtime/dist').mkdir(parents=True)
        (core / 'RELEASE.json').write_text(json.dumps({'releaseId': 'd' * 64}))
        (core / 'integrations/pi_runtime/dist/build-manifest.json').write_text(json.dumps({'buildId': 'd' * 64}))
        state = self.root / 'maintenance'
        state.mkdir(mode=0o700)
        receipt = state / 'transaction.json'
        receipt.write_text('{}')
        receipt.chmod(0o600)
        plan = dict(maintenanceApproved=True, entryInventoryConfirmed=True, hostInventoryConfirmed=True,
                    hosts=[{'name': socket.gethostname()}], stateDir=str(state))
        transaction = {'id': 'fenced-id', 'state': 'fenced'}
        active = []
        @contextlib.contextmanager
        def lock(_plan):
            active.append(True)
            try:
                yield state
            finally:
                active.pop()
        adoption = Mock(__file__=str(core / 'agent_core/pi_plugin_adoption.py'))
        adoption.load_plan.return_value = plan
        adoption.state_lock.side_effect = lock
        adoption.transaction.return_value = transaction
        adoption.verify_fences.side_effect = lambda value: self.assertEqual(active, [True])
        with patch.object(legacy.importlib, 'import_module', return_value=adoption):
            with legacy.maintenance_fence(str(core), '/private/plan.json', service) as (value, recheck):
                self.assertIs(value, transaction)
                recheck()
            self.assertEqual(active, [])
            self.assertEqual(adoption.verify_fences.call_count, 2)
            transaction['state'] = 'adopted'
            with self.assertRaisesRegex(service.Refused, 'MAINTENANCE_FENCE_REQUIRED'):
                with legacy.maintenance_fence(str(core), '/private/plan.json', service):
                    self.fail('Non-fenced transaction must not authorize legacy stop')
            transaction['state'] = 'fenced'
            adoption.verify_fences.side_effect = ValueError('fixture binding changed')
            with self.assertRaisesRegex(service.Refused, 'MAINTENANCE_FENCE_UNVERIFIED'):
                with legacy.maintenance_fence(str(core), '/private/plan.json', service):
                    self.fail('Changed fence must not authorize legacy stop')

    def test_host_process_includes_children_started_by_nonmain_thread(self):
        script = self.root / 'thread-child.py'
        script.write_text('import signal,subprocess,sys,threading,time\n'
            'signal.signal(signal.SIGTERM,lambda *args: exit(0))\n'
            'def worker():\n'
            ' p=subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.readline()"],stdin=subprocess.PIPE)\n'
            ' print(p.pid,flush=True)\n'
            ' try: sys.stdin.readline()\n'
            ' finally: p.communicate(b"\\n",timeout=3)\n'
            't=threading.Thread(target=worker);t.start();t.join()\n')
        process = subprocess.Popen([sys.executable, '-B', str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            child_pid = int(process.stdout.readline())
            facts = legacy.host_process(process.pid, service)
            self.assertIn(child_pid, facts['children'])
            self.assertEqual(facts['pid'], process.pid)
        finally:
            process.communicate(b'\n', timeout=5)

    @unittest.skipUnless(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'Linux pidfd required')
    def test_real_holder_pidfd_graceful_stop_writes_receipt_and_releases_lease(self):
        # No production process or network request: the real supervisor and its
        # child run only these private fixture scripts. HTTP is simulated.
        manager = self.root / 'manager.py'
        bootstrap = self.root / 'bootstrap.py'
        ready = self.root / 'backend-ready'
        bootstrap.write_text('import signal,time\nfrom pathlib import Path\n'
                            'signal.signal(signal.SIGTERM, lambda *args: exit(0))\n'
                            'Path(' + repr(str(ready)) + ').touch()\nwhile True: time.sleep(.01)\n')
        manager.write_text('import importlib.util\ns=importlib.util.spec_from_file_location("holder",'
            + repr(str(Path(service.__file__).resolve())) + ');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n'
            'm.health=lambda c,i:{"port":1234}\nraise SystemExit(m.main())\n')
        original = manager.read_bytes()
        # ensure gets a real run-holder, actual PID/startTicks and backend lease.
        (Path(self.config['stateRoot']) / 'service.json').unlink()
        launched = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            launched.append(process)
            return process
        with patch.object(service, '__file__', str(manager)), patch.object(service.subprocess, 'Popen', side_effect=spawn):
            started = service.ensure(self.config)
        self.instance = started['instanceId']
        holder_pid = launched[0].pid
        # Cleanup only the exact fixture holder if a test assertion fails.
        def cleanup():
            current = service.read_record(self.config)
            if current and current.get('pid') == holder_pid and service.process_alive(current):
                fd = os.pidfd_open(holder_pid)
                try:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                finally:
                    os.close(fd)
            launched[0].wait(timeout=10)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = service.read_record(self.config)
            if row.get('status') == 'ready' and ready.exists():
                break
            time.sleep(.02)
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['pid'], holder_pid)
        manager.write_text('# fenced fixture launcher\n')
        fence_hash = hashlib.sha256(manager.read_bytes()).hexdigest()
        self.transaction['entries'] = [{'path': str(manager), 'original': {'kind': 'file', 'data': base64.b64encode(original).decode()}}]
        @contextlib.contextmanager
        def receipt(*args):
            def verify():
                self.assertEqual(hashlib.sha256(manager.read_bytes()).hexdigest(), fence_hash)
            verify()
            yield self.transaction, verify
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            return 'systemd\n' if str(path) == '/proc/1/comm' else read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), patch.object(legacy, 'maintenance_fence', receipt), \
             patch.object(legacy, 'LEGACY_HOLDER_SHA256', hashlib.sha256(original).hexdigest()), \
             patch.object(legacy, 'LEGACY_BOOTSTRAP_SHA256', hashlib.sha256(bootstrap.read_bytes()).hexdigest()), \
             patch.object(service, 'health', return_value={'port': 1234}), patch.object(legacy, 'authenticated_idle'):
            result = legacy.stop_legacy_idle(self.config, self.instance, '/fixture', '/fixture',
                clients_quiescent=True, idle_confirmed=True, timeout=10, service=service)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['signalCount'], 1)
        self.assertFalse(result['atomicIdleAdmission'])
        self.assertFalse(result['nativeIdleShutdown'])
        row = service.read_record(self.config)
        self.assertEqual(row['exitCode'], 0)
        self.assertFalse(service.process_alive(row))
        self.assertFalse(service.backend_lease_held(self.config))
        print(json.dumps({'status': 'PASS', 'evidenceClass': 'ISOLATED_REAL_PIDFD_LIFECYCLE_HTTP_SIMULATED',
                          'signalCount': 1, 'exitCode': row['exitCode'], 'atomicIdleAdmission': False}))


if __name__ == '__main__':
    unittest.main(verbosity=2)

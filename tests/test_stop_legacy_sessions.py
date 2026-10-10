"""Actual disposable children; no production PID or resource is signalled."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'scripts/stop-legacy-sessions.py'
spec = importlib.util.spec_from_file_location('legacy_stop', MODULE)
stopper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stopper)


class StopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / 'legacy'
        self.main.mkdir()
        self.record = self.root / 'service.json'
        self.identity = {'host': socket.gethostname(), 'pid': 99999999,
                         'startTicks': '1', 'instanceId': 'test-instance'}
        self.record.write_text(json.dumps(self.identity))
        self.record.chmod(0o600)
        self.plan = {'schema': 'BioMindLegacySessionStopPlanV1', 'host': socket.gethostname(),
                     'uid': os.getuid(), 'stopOldConversationsAuthorized': True,
                     'forceAfterGrace': False, 'targets': [], 'legacyMainRoots': [str(self.main)],
                     'serviceManager': str(self.root / 'server-service.py'),
                     'serviceLauncher': str(self.root / 'next-service.mjs'),
                     'pipeLauncher': str(self.root / 'relay.py'), 'serviceRecord': str(self.record),
                     'serviceIdentity': self.identity, 'serviceConfig': str(self.root / 'config.json')}
        self.hist = self.main / 'history.jsonl'
        self.hist.write_text('retained history sentinel\n')
        self.claim = self.main / 'history.jsonl.biomind-writer.lock'
        self.claim.write_text('retained claim sentinel\n')
        self.namespace = patch.object(stopper, 'host_namespace', return_value=True)
        self.namespace.start()
        self.addCleanup(self.namespace.stop)

    def child(self, script=None, ignore=False):
        path = script or self.main / 'bio_agents.py'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('import signal,time\n' +
                        ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore else '') +
                        'print("READY", flush=True)\ntime.sleep(60)\n')
        process = subprocess.Popen([sys.executable, '-B', str(path)], stdout=subprocess.PIPE)
        self.assertEqual(process.stdout.readline(), b'READY\n')
        def clean():
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
        self.addCleanup(clean)
        row = stopper.snapshot(process.pid)
        self.plan['targets'].append({k: row[k] for k in ('pid', 'startTicks', 'executable', 'commandSha256')})
        return process

    def apply(self):
        return stopper.stop(self.plan, apply=True, service_wait=0.05, owner_wait=0.05)

    def test_actual_owner_stopped_with_pidfd_history_and_claim_untouched(self):
        process = self.child()
        result = self.apply()
        self.assertEqual(result['status'], 'STOPPED_REVIEWED_TARGETS')
        self.assertEqual(result['signals'][0]['signal'], 'SIGTERM')
        self.assertIsNotNone(process.poll())
        self.assertEqual(self.hist.read_text(), 'retained history sentinel\n')
        self.assertEqual(self.claim.read_text(), 'retained claim sentinel\n')
        self.assertFalse(result['actualAdoption'])
        self.assertFalse(result['maintenanceReady'])

    def test_changed_identity_rejects_before_any_other_valid_target_signalled(self):
        process = self.child()
        other = self.child(self.main / 'integrations/pi_runtime/python_service.py')
        self.plan['targets'][1]['startTicks'] = 'not-the-reviewed-start'
        result = self.apply()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['signals'], [])
        self.assertIsNone(process.poll())
        self.assertIsNone(other.poll())

    def test_stale_command_hash_is_not_an_authorization(self):
        process = self.child()
        self.plan['targets'][0]['commandSha256'] = '0' * 64
        self.assertEqual(self.apply()['code'], 'STOP_TARGET_VALIDATION_FAILED')
        self.assertIsNone(process.poll())

    def test_unrelated_script_with_biomind_in_contents_is_never_killed(self):
        process = self.child(self.root / 'sshd.py')
        result = self.apply()
        self.assertEqual(result['signals'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'NOT_A_CONFIRMED_BIOMIND_OWNER')
        self.assertIsNone(process.poll())
        row = stopper.snapshot(process.pid)
        row['argv'] = [sys.executable, '-c', 'pass', str(self.main / 'bio_agents.py')]
        self.assertIsNone(stopper.role(row, self.plan))

    def test_dry_run_sends_no_signals(self):
        process = self.child()
        result = stopper.stop(self.plan)
        self.assertEqual(result['status'], 'DRY_RUN')
        self.assertEqual(result['signals'], [])
        self.assertIsNone(process.poll())

    def test_host_and_namespace_are_required(self):
        process = self.child()
        self.plan['host'] = 'not-this-host'
        self.assertEqual(self.apply()['code'], 'STOP_HOST_OR_UID_MISMATCH')
        self.plan['host'] = socket.gethostname()
        with patch.object(stopper, 'host_namespace', return_value=False):
            self.assertEqual(self.apply()['code'], 'STOP_REAL_HOST_NAMESPACE_REQUIRED')
        self.assertIsNone(process.poll())

    def test_new_service_instance_blocks_old_stop_plan(self):
        process = self.child()
        self.record.write_text(json.dumps({**self.identity, 'instanceId': 'new'}))
        self.assertEqual(self.apply()['code'], 'STOP_SERVICE_INSTANCE_CHANGED')
        self.assertIsNone(process.poll())

    def test_force_only_after_grace_for_verified_process(self):
        process = self.child(ignore=True)
        self.plan['forceAfterGrace'] = True
        result = self.apply()
        self.assertEqual(result['status'], 'STOPPED_REVIEWED_TARGETS')
        self.assertEqual([s['signal'] for s in result['signals']], ['SIGTERM', 'SIGKILL'])
        self.assertIsNotNone(process.poll())

    def test_no_force_preserves_survivor_and_reports_partial(self):
        process = self.child(ignore=True)
        result = self.apply()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['code'], 'STOP_TARGETS_STILL_RUNNING')
        self.assertIsNone(process.poll())

    def test_exited_owner_is_idempotent(self):
        process = self.child()
        process.terminate()
        process.wait()
        result = self.apply()
        self.assertEqual(result['status'], 'STOPPED_REVIEWED_TARGETS')
        self.assertEqual(result['signals'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'ALREADY_EXITED')

    def test_service_command_must_bind_exact_config_and_instance(self):
        process = self.child(Path(self.plan['serviceManager']))
        row = stopper.snapshot(process.pid)
        self.identity.update(pid=process.pid, startTicks=row['startTicks'])
        self.record.write_text(json.dumps(self.identity))
        result = self.apply()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['signals'], [])
        self.assertIsNone(process.poll())

    def test_shutdown_order_service_before_main(self):
        owner = self.child()
        script = Path(self.plan['serviceManager'])
        finished = self.root / 'service-stopped'
        script.write_text('import signal,sys,time\nfrom pathlib import Path\n' +
                         'def stop(*args):\n    Path(' + repr(str(finished)) + ').touch()\n    sys.exit(0)\n' +
                         'signal.signal(signal.SIGTERM, stop)\nprint("READY", flush=True)\ntime.sleep(60)\n')
        service = subprocess.Popen([sys.executable, str(script), 'run', '--config', self.plan['serviceConfig'],
                                    '--instance', self.identity['instanceId']], stdout=subprocess.PIPE)
        self.assertEqual(service.stdout.readline(), b'READY\n')
        def clean():
            if service.poll() is None:
                service.kill()
            service.wait()
            service.stdout.close()
        self.addCleanup(clean)
        row = stopper.snapshot(service.pid)
        self.identity.update(pid=service.pid, startTicks=row['startTicks'])
        self.record.write_text(json.dumps(self.identity))
        self.plan['targets'].append({k: row[k] for k in ('pid', 'startTicks', 'executable', 'commandSha256')})
        result = stopper.stop(self.plan, apply=True, service_wait=2, owner_wait=.2)
        self.assertEqual(result['status'], 'STOPPED_REVIEWED_TARGETS')
        self.assertTrue(finished.exists())
        self.assertEqual([s['role'] for s in result['signals']], ['service', 'main'])
        self.assertIsNotNone(owner.poll())

    def test_unsupported_python_cannot_send_signals(self):
        process = self.child()
        with patch.object(stopper.os, 'pidfd_open', create=True) as opened:
            # Missing send interface occurs in this site's conda Python build.
            saved = stopper.signal.pidfd_send_signal
            del stopper.signal.pidfd_send_signal
            try:
                result = self.apply()
            finally:
                stopper.signal.pidfd_send_signal = saved
        self.assertEqual(result['code'], 'STOP_PIDFD_REQUIRED')
        opened.assert_not_called()
        self.assertIsNone(process.poll())


if __name__ == '__main__':
    unittest.main()

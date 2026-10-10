"""Host coverage, sanitized SSH diagnosis, and fail-closed adoption regression."""
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

from agent_core import pi_plugin_adoption as adoption
from agent_core import pi_plugin_guard as guard


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('agent', 'project', 'legacy', 'sessions'):
            (self.root / name).mkdir()
        self.entry = self.root / 'biomind'
        self.entry.write_text('#!/bin/sh\nexit 0\n')
        self.file = self.root / 'plan.json'
        self.local = socket.gethostname()
        self.raw = {
            'schema': adoption.SCHEMA, 'agentDir': str(self.root / 'agent'),
            'cwd': str(self.root / 'project'), 'scopes': ['user'],
            'stateDir': str(self.root / 'state'),
            'hosts': [{'name': self.local}, {'name': 'remote-test', 'ssh': 'remote-test'}],
            'legacyCodeRoots': [str(self.root / 'legacy')],
            'sessionRoots': [str(self.root / 'sessions')], 'scheduler': 'none',
            'entries': [{'path': str(self.entry), 'expectedSha256': adoption.entry_snapshot(self.entry)['sha256']}],
            'maintenanceApproved': True, 'hostInventoryConfirmed': True, 'entryInventoryConfirmed': True,
        }
        self.load()

    def load(self):
        adoption.save(self.file, self.raw)
        self.plan = adoption.load_plan(self.file)

    def report(self, host=None, **kwargs):
        return {'schema': adoption.SCHEMA, 'host': host or self.local, 'uid': os.getuid(),
                'at': time.time(), 'planSha256': self.plan['_sha256'], 'status': 'clear',
                'legacyProcesses': [], 'unreviewedProcesses': [], 'unreadablePids': [],
                'activeWriterClaims': [], 'activeSlurmJobs': 0, 'slurmJobs': [], 'signalsSent': False,
                'checks': {'hostNamespace': 'PASS', 'processes': 'PASS', 'writers': 'PASS', 'scheduler': 'NOT_APPLICABLE'}, **kwargs}

    def remote(self, report=None, code=0, stderr=''):
        return subprocess.CompletedProcess([], code, json.dumps(report or self.report('remote-test')), stderr)

    def incomplete(self, reply, local=None):
        with patch.object(adoption, 'audit_local', return_value=local or self.report()), \
                patch.object(adoption.subprocess, 'run', return_value=reply), \
                self.assertRaises(adoption.AuditFailure) as caught:
            adoption.audit_hosts(self.plan)
        result = caught.exception.result
        self.assertFalse(result['coverageComplete'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertIn('remote-test', result['incompleteHosts'])
        return result

    def test_partial_failure_retains_busy_local_and_continues_remaining_hosts(self):
        self.raw['hosts'].append({'name': 'remote-third', 'ssh': 'remote-third'})
        self.load()
        local = self.report(status='BUSY', legacyProcesses=[{'pid': 101}])
        responses = [self.remote(code=255, stderr='Host key verification failed. PRIVATE_BANNER'),
                     self.remote(self.report('remote-third', status='BUSY'), code=3)]
        with patch.object(adoption, 'audit_local', return_value=local), \
                patch.object(adoption.subprocess, 'run', side_effect=responses) as run, \
                self.assertRaises(adoption.AuditFailure) as caught:
            adoption.audit_hosts(self.plan)
        result = caught.exception.result
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result['checkedHosts'], [self.local, 'remote-third'])
        self.assertEqual(result['reports'][0]['legacyProcesses'], [{'pid': 101}])
        self.assertEqual(result['reports'][1]['code'], 'ADOPTION_SSH_HOST_KEY_UNVERIFIED')
        self.assertNotIn('PRIVATE_BANNER', json.dumps(result))
        self.assertIn('StrictHostKeyChecking=yes', run.call_args.args[0])
        self.assertIn('BatchMode=yes', run.call_args.args[0])

    def test_ssh_reasons_are_categorical_no_raw_credentials_or_banner(self):
        for stderr, code in [
            ('Permission denied (publickey)', 'AUTH_FAILED'),
            ('Could not resolve hostname remote', 'NAME_UNRESOLVED'),
            ('Connection timed out', 'CONNECT_TIMEOUT'),
            ('REMOTE HOST IDENTIFICATION HAS CHANGED', 'HOST_KEY_CHANGED'),
            ('Connection refused', 'CONNECTION_REFUSED'),
            ('No route to host', 'NETWORK_UNREACHABLE'),
            ('socket: Operation not permitted', 'SOCKET_DENIED'),
        ]:
            with self.subTest(code=code):
                result = self.incomplete(self.remote(code=255, stderr=stderr + ' DO_NOT_PRINT_ME'))
                self.assertEqual(result['reports'][1]['code'], 'ADOPTION_SSH_' + code)
                self.assertNotIn('DO_NOT_PRINT_ME', json.dumps(result))

    def test_remote_scheduler_failure_preserves_process_findings(self):
        report = self.report('remote-test', status='BLOCKED', stage='scheduler',
                             code='ADOPTION_SCHEDULER_UNAVAILABLE', legacyProcesses=[{'pid': 109}])
        result = self.incomplete(self.remote(report, code=2))
        self.assertEqual(result['reports'][1], report)

    def test_missing_remote_plan_error_is_explained_but_is_not_host_evidence(self):
        reply = self.remote({'status': 'BLOCKED', 'code': 'RESOURCE_ADOPTION_RECORD_UNSAFE'}, code=2)
        result = self.incomplete(reply)
        self.assertEqual(result['reports'][1]['code'], 'RESOURCE_ADOPTION_RECORD_UNSAFE')
        self.assertEqual(result['reports'][1]['stage'], 'remote-audit')
        self.assertNotIn('uid', result['reports'][1])

    def test_malformed_stale_wrong_host_and_mismatched_exit_never_pass(self):
        responses = [subprocess.CompletedProcess([], 0, 'banner\n{}', ''),
                     subprocess.CompletedProcess([], 0, '[]', ''),
                     self.remote(self.report('wrong-host')), self.remote(self.report('remote-test', uid=-1)),
                     self.remote(self.report('remote-test', at=0)),
                     self.remote(self.report('remote-test', at=float('nan'))),
                     self.remote(self.report('remote-test', at='now')),
                     self.remote(self.report('remote-test', planSha256='different')),
                     self.remote(self.report('remote-test', checks={'processes': 'PASS'})),
                     self.remote(self.report('remote-test', legacyProcesses=[{'pid': 1}])),
                     self.remote(self.report('remote-test', activeSlurmJobs=1)),
                     self.remote(code=3), self.remote(code=1)]
        for response in responses:
            with self.subTest(response=response): self.incomplete(response)

    def test_timeout_does_not_leak_partial_ssh_output(self):
        with patch.object(adoption, 'audit_local', return_value=self.report()), \
                patch.object(adoption.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                    'secret command', 60, output='secret output', stderr='secret error')), \
                self.assertRaises(adoption.AuditFailure) as caught:
            adoption.audit_hosts(self.plan)
        value = caught.exception.result
        self.assertEqual(value['reports'][1]['code'], 'ADOPTION_AUDIT_TIMEOUT')
        self.assertNotIn('secret', json.dumps(value))

    def test_all_hosts_clear_is_not_inventory_approval(self):
        self.plan['hostInventoryConfirmed'] = False
        with patch.object(adoption, 'audit_local', return_value=self.report()), \
                patch.object(adoption.subprocess, 'run', return_value=self.remote()):
            reports = adoption.audit_hosts(self.plan)
        summary = adoption.audit_summary(self.plan, reports)
        self.assertTrue(summary['coverageComplete'])
        self.assertFalse(summary['inventoryConfirmed'])
        self.assertEqual(summary['status'], 'clear')

    def test_local_namespace_failure_still_checks_remote_host(self):
        local = self.report(status='BLOCKED', code='ADOPTION_HOST_PID_NAMESPACE_REQUIRED')
        with patch.object(adoption, 'audit_local', return_value=local), \
                patch.object(adoption.subprocess, 'run', return_value=self.remote()) as run, \
                self.assertRaises(adoption.AuditFailure) as caught:
            adoption.audit_hosts(self.plan)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(caught.exception.result['incompleteHosts'], [self.local])
        self.assertEqual(caught.exception.result['checkedHosts'], ['remote-test'])

    def test_scheduler_unavailable_keeps_actual_unlocked_legacy_process_evidence(self):
        worker = self.root / 'legacy/old.py'
        worker.write_text('import sys\nprint("ready", flush=True)\nsys.stdin.readline()\n')
        process = subprocess.Popen([sys.executable, '-B', str(worker)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.plan['scheduler'] = 'slurm'
            with patch.object(adoption.shutil, 'which', return_value=None):
                report = adoption.audit_local(self.plan, require_host_namespace=False)
            self.assertEqual(report['code'], 'ADOPTION_SCHEDULER_EXECUTABLE_UNAVAILABLE')
            self.assertEqual(report['status'], 'BLOCKED')
            self.assertEqual(report['checks']['scheduler'], 'BLOCKED')
            self.assertIsNone(report['activeSlurmJobs'])
            self.assertIn(process.pid, [r['pid'] for r in report['legacyProcesses']])
            self.assertIsNone(process.poll())
        finally:
            process.communicate('\n', timeout=10)

    def test_writer_unknown_host_is_visible_and_blocks_audit(self):
        record = self.root / 'sessions/a.biomind-writer.lock'
        record.write_text(json.dumps({'schema': 1, 'pid': 123, 'hostname': 'compute-unknown'}))
        report = adoption.audit_local(self.plan, require_host_namespace=False)
        self.assertEqual(report['code'], 'ADOPTION_WRITER_HOST_UNDECLARED')
        self.assertEqual(report['observedWriterHosts'], ['compute-unknown'])
        self.assertEqual(report['checks']['scheduler'], 'NOT_RUN')
        self.assertTrue(record.exists())

    def test_slurm_inflight_job_reports_assigned_nodes_without_names_or_commands(self):
        self.plan['scheduler'] = 'slurm'
        reply = subprocess.CompletedProcess([], 0, '123|RUNNING|compute[01-02]\n124|PENDING|\n', '')
        with patch.object(adoption.shutil, 'which', return_value=sys.executable), \
                patch.object(adoption.subprocess, 'run', return_value=reply):
            report = adoption.audit_local(self.plan, require_host_namespace=False)
        self.assertEqual(report['activeSlurmJobs'], 2)
        self.assertEqual(report['slurmJobs'][0], {'jobId': '123', 'state': 'RUNNING', 'nodeList': 'compute[01-02]'})
        self.assertNotEqual(report['status'], 'clear')

    def test_incomplete_audit_stops_adoption_before_markers_or_settings_writes(self):
        settings = self.root / 'agent/settings.json'
        settings.write_text('{"packages":["npm:keep@^1"]}')
        adoption.fence(self.plan)
        with patch.object(adoption, 'audit_local', return_value=self.report()), \
                patch.object(adoption.subprocess, 'run', return_value=self.remote(code=255, stderr='Permission denied')), \
                self.assertRaises(adoption.AuditFailure):
            adoption.adopt(self.plan)
        self.assertEqual(settings.read_text(), '{"packages":["npm:keep@^1"]}')
        self.assertEqual(adoption.transaction(self.plan, self.root / 'state')['state'], 'fenced')
        self.assertFalse((self.root / 'agent/.biomind-resource-guard.json').exists())
        # After connection repair the same fenced transaction can be retried;
        # real production still requires an audit, not a stored prior report.
        with patch.object(adoption, 'audit_local', return_value=self.report()), \
                patch.object(adoption.subprocess, 'run', return_value=self.remote()):
            result = adoption.adopt(self.plan)
        self.assertEqual(result['status'], 'adopted')
        self.assertEqual(settings.read_text(), '{"packages":["npm:keep@^1"]}')

    def test_actual_cli_missing_plan_has_audit_specific_diagnosis(self):
        cli = Path(__file__).resolve().parents[1] / 'scripts/plugin-maintenance.py'
        response = subprocess.run([sys.executable, '-B', str(cli), 'audit-local', '--plan',
                                   str(self.root / 'missing.json')], capture_output=True, text=True, timeout=15)
        self.assertEqual(response.returncode, 2)
        result = json.loads(response.stdout)
        self.assertEqual(result['code'], 'ADOPTION_AUDIT_PATH_UNAVAILABLE')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertNotIn('Traceback', response.stdout + response.stderr)
        self.assertNotIn('repair resumes', result['recovery'])

    def test_explicit_scheduler_path_works_without_login_shell_path(self):
        self.plan.update(scheduler='slurm', schedulerExecutable=sys.executable)
        with patch.object(adoption.shutil, 'which', side_effect=AssertionError('Must not depend on PATH')), \
                patch.object(adoption.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            report = adoption.audit_local(self.plan, require_host_namespace=False)
        self.assertEqual(report['checks']['scheduler'], 'PASS')
        self.assertEqual(report['schedulerPathSource'], 'plan')
        self.assertEqual(report['schedulerExecutable'], sys.executable)
        self.assertEqual(run.call_args.args[0][0], sys.executable)

    def test_missing_configured_scheduler_must_not_fall_back_or_claim_no_jobs(self):
        self.plan.update(scheduler='slurm', schedulerExecutable=str(self.root / 'missing-squeue'))
        with patch.object(adoption.subprocess, 'run', side_effect=AssertionError('Missing executable')):
            report = adoption.audit_local(self.plan, require_host_namespace=False)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertEqual(report['code'], 'ADOPTION_SCHEDULER_EXECUTABLE_UNAVAILABLE')
        self.assertIsNone(report['activeSlurmJobs'])

    def test_scheduler_query_failure_differs_from_missing_executable(self):
        self.plan.update(scheduler='slurm', schedulerExecutable=sys.executable)
        with patch.object(adoption.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'PRIVATE')):
            report = adoption.audit_local(self.plan, require_host_namespace=False)
        self.assertEqual(report['code'], 'ADOPTION_SCHEDULER_QUERY_FAILED')
        self.assertEqual(report['schedulerExitCode'], 1)
        self.assertIsNone(report['activeSlurmJobs'])
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_scheduler_path_requires_absolute_private_plan_declaration(self):
        self.raw['schedulerExecutable'] = 'squeue'
        adoption.save(self.file, self.raw)
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_SCHEDULER_ABSOLUTE_PATH_REQUIRED'):
            adoption.load_plan(self.file)

    def test_unreadable_process_records_failed_field_and_errno_remains_blocked(self):
        worker = self.root / 'legacy/denied.py'
        worker.write_text('import sys\nprint("ready", flush=True)\nsys.stdin.readline()\n')
        process = subprocess.Popen([sys.executable, '-B', str(worker)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        original = os.readlink
        def readlink(path, *args, **kwargs):
            if str(path) == f'/proc/{process.pid}/cwd': raise PermissionError(13, 'PRIVATE')
            return original(path, *args, **kwargs)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with patch.object(adoption.os, 'readlink', side_effect=readlink):
                report = adoption.audit_local(self.plan, require_host_namespace=False)
            detail = next(row for row in report['unreadableProcesses'] if row['pid'] == process.pid)
            self.assertEqual(detail['field'], 'cwd')
            self.assertEqual(detail['errno'], 13)
            self.assertIn('startTicks', detail)
            self.assertEqual(report['code'], 'ADOPTION_PROCESS_VISIBILITY_INCOMPLETE')
            self.assertIsNone(process.poll())
            self.assertNotIn('PRIVATE', json.dumps(report))
        finally: process.communicate('\n', timeout=10)

    def test_script_identity_hint_never_outputs_command_arguments(self):
        hint = adoption.process_hint(b'/usr/bin/node\0/home/user/.editor/server.js\0--token\0PRIVATE_VALUE\0', '/tmp', '/usr/bin/node')
        self.assertEqual(hint['entryProgram'], '/home/user/.editor/server.js')
        self.assertNotIn('PRIVATE_VALUE', json.dumps(hint))
        inline = adoption.process_hint(b'python\0-c\0PRIVATE_CODE\0', '/tmp', '/usr/bin/python')
        self.assertNotIn('entryProgram', inline)
        self.assertNotIn('PRIVATE_CODE', json.dumps(inline))
        self.assertEqual(adoption.process_hint(b'sshd: user@notty\0')['roleHint'], 'ssh-login-helper')

    def test_reference_evidence_distinguishes_resource_and_code_without_filenames(self):
        refs = adoption.reference_evidence([('cwd', '/legacy'), ('argv', '/legacy/a.py'),
                ('fd', '/legacy/agent/private-history.jsonl')], ['/legacy'], ['/legacy/agent'])
        self.assertIn({'source':'cwd','kind':'code','root':'/legacy'}, refs)
        self.assertIn({'source':'fd','kind':'resource','root':'/legacy/agent'}, refs)
        self.assertNotIn('private-history', json.dumps(refs))

    def test_shell_cwd_reference_is_explained_but_not_automatically_exempted(self):
        process = subprocess.Popen(['/usr/bin/bash','--noprofile','--norc','-c','read unused'],
                                   cwd=self.root/'legacy',stdin=subprocess.PIPE)
        try:
            report = adoption.audit_local(self.plan, require_host_namespace=False)
            row = next(r for r in report['processEvidence'] if r['pid']==process.pid)
            self.assertEqual(row['roleHint'],'shell')
            self.assertIn({'source':'cwd','kind':'code','root':str(self.root/'legacy')},row['references'])
            self.assertIn(process.pid,[r['pid'] for r in report['legacyProcesses']])
            self.assertNotEqual(report['status'],'clear')
            self.assertIsNone(process.poll())
        finally: process.communicate(b'\n',timeout=10)


if __name__ == '__main__': unittest.main()

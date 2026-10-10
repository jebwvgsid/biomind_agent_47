"""Isolated maintenance transactions, real processes/flocks; no production writes."""
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

ROOT = Path(__file__).resolve().parents[1]


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agent = self.root / 'agent'
        self.project = self.root / 'project'
        self.legacy = self.root / 'legacy'
        self.sessions = self.root / 'sessions'
        for p in (self.agent, self.project / '.pi', self.legacy, self.sessions): p.mkdir(parents=True)
        self.launcher_target = self.legacy / 'launch.py'
        self.launcher_target.write_text('#!/usr/bin/env python3\nprint("original")\n')
        self.entry = self.root / 'biomind'
        self.entry.symlink_to(self.launcher_target)
        self.settings = self.agent / 'settings.json'
        self.settings.write_text(json.dumps({'packages': [{'source': 'npm:test@^1', 'extensions': ['!disabled.ts', 'tools.ts'], 'skills': []}], 'extensions': ['./local.ts']}))
        (self.agent / 'trust.json').write_text('{"version":1,"projects":{}}')
        (self.project / '.pi/settings.json').write_text('{"packages":["../local-development"]}')
        (self.agent / 'npm').mkdir()
        (self.agent / 'npm/package.json').write_text('{"dependencies":{"test":"^1"}}')
        self.before = self.content()
        self.plan_file = self.root / 'plan.json'
        self.raw = {'schema': adoption.SCHEMA, 'agentDir': str(self.agent), 'cwd': str(self.project),
                    'scopes': ['user', 'project'], 'stateDir': str(self.root / 'maintenance'),
                    'hosts': [{'name': socket.gethostname()}], 'scheduler': 'none',
                    'legacyCodeRoots': [str(self.legacy)], 'sessionRoots': [str(self.sessions)],
                    'entries': [{'path': str(self.entry), 'expectedSha256': adoption.entry_snapshot(self.entry)['sha256']}],
                    'entryInventoryConfirmed': True, 'hostInventoryConfirmed': True, 'maintenanceApproved': True}
        adoption.save(self.plan_file, self.raw)
        self.plan = adoption.load_plan(self.plan_file)

    def tearDown(self): self.temp.cleanup()

    def content(self):
        return {str(p): p.read_bytes() for base in (self.agent, self.project, self.legacy, self.sessions)
                for p in base.rglob('*') if p.is_file() and '.biomind-resource' not in str(p)}

    def clear(self, plan):
        # Inject only occupancy evidence for this disposable fixture. The CLI has
        # no audit bypass and always enforces host namespace and actual Slurm.
        return [{'host': socket.gethostname(), 'uid': os.getuid(), 'at': time.time(),
                 'planSha256': plan['_sha256'], 'status': 'clear'}]

    def initialize(self):
        resources = guard.prepare(self.plan)
        gates = []
        try:
            for row in resources['scopes']:
                gates.append({'fd': os.open(row['gate'], os.O_RDWR | os.O_CREAT, 0o600), 'path': row['gate'], 'mode': 'exclusive'})
            guard.lock_fds({'files': gates})
            return guard.initialize({**self.plan, 'gates': gates})
        finally:
            for row in gates: os.close(row['fd'])

    def test_old_process_without_any_guard_blocks_before_resource_write(self):
        script = self.legacy / 'old-worker.py'
        script.write_text('import sys\nprint("ready",flush=True)\nsys.stdin.readline()\n')
        process = subprocess.Popen([sys.executable, '-B', str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            before = self.content()
            adoption.fence(self.plan)
            report = adoption.audit_local(self.plan, require_host_namespace=False)
            self.assertIn(process.pid, [p['pid'] for p in report['legacyProcesses']])
            with self.assertRaisesRegex(guard.GuardError, 'LEGACY_OCCUPANCY_BUSY'):
                adoption.adopt(self.plan, collect=lambda _: [report])
            self.assertIsNone(process.poll())
            self.assertEqual(before, self.content())
            self.assertTrue(all(not Path(s['marker']).exists() for s in guard.plan(self.plan)['scopes']))
            blocked = subprocess.run([str(self.entry)], capture_output=True, text=True)
            self.assertEqual(blocked.returncode, 75)
            self.assertIn('BIOMIND_MAINTENANCE_WINDOW', blocked.stdout)
        finally:
            process.communicate('\n', timeout=10)

    def test_adopt_preserves_settings_filters_trust_versions_and_packages(self):
        adoption.fence(self.plan)
        self.assertEqual(adoption.adopt(self.plan, collect=self.clear)['status'], 'adopted')
        self.assertEqual(self.before, self.content())
        self.assertTrue(all(not s['legacyUnverified'] for s in self.initialize()['scopes']))
        self.assertEqual(adoption.adopt(self.plan, collect=self.clear)['status'], 'already-adopted')
        self.entry.write_text('#!/usr/bin/env python3\nprint("unexpected old entry")\n')
        self.assertTrue(all(s['legacyUnverified'] for s in guard.plan(self.plan)['scopes']))

    def test_new_arrival_cannot_acquire_admission_gate_during_audit(self):
        adoption.fence(self.plan)
        def checkpoint(stage):
            if stage != 'after-audit': return
            gate = guard.plan(self.plan)['scopes'][0]['gate']
            code = ('import os,sys;sys.path.insert(0,sys.argv[1]);from agent_core import pi_plugin_guard as g;'
                    'p=sys.argv[2];fd=os.open(p,os.O_RDWR);g.lock_fds({"files":[{"fd":fd,"path":p,"mode":"exclusive"}]})')
            result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT), gate], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('BUSY', result.stderr)
        adoption.adopt(self.plan, collect=self.clear, checkpoint=checkpoint)

    def test_existing_shared_worker_blocks_adoption_before_marker_write(self):
        adoption.fence(self.plan)
        row = guard.prepare({**self.plan, 'resources': True})['resources'][0]
        fd = os.open(row['lock'], os.O_RDWR | os.O_CREAT, 0o600)
        try:
            guard.lock_fds({'files': [{'fd': fd, 'path': row['lock'], 'mode': 'shared'}]})
            with self.assertRaisesRegex(guard.GuardError, 'BUSY'):
                adoption.adopt(self.plan, collect=self.clear)
            self.assertEqual(self.before, self.content())
        finally: os.close(fd)
        self.assertEqual(adoption.adopt(self.plan, collect=self.clear)['status'], 'adopted')

    def test_interrupted_multiscope_commit_blocks_new_worker_then_repairs(self):
        adoption.fence(self.plan)
        def crash(stage):
            if stage == 'marker': raise OSError('injected interruption after first marker')
        with self.assertRaises(OSError): adoption.adopt(self.plan, collect=self.clear, checkpoint=crash)
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_ADOPTION_RECOVERY_REQUIRED'): self.initialize()
        self.assertEqual(self.before, self.content())
        adoption.adopt(self.plan, collect=self.clear)
        self.assertTrue(all(not s['legacyUnverified'] for s in self.initialize()['scopes']))

    def test_commit_save_failure_recovers_without_permanent_busy(self):
        adoption.fence(self.plan)
        original_save = adoption.save
        def failed_save(path, value):
            if isinstance(value, dict) and value.get('state') == 'committed': raise OSError('simulated disk error')
            return original_save(path, value)
        with patch.object(adoption, 'save', failed_save), self.assertRaises(OSError):
            adoption.adopt(self.plan, collect=self.clear)
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_ADOPTION_RECOVERY_REQUIRED'): self.initialize()
        adoption.adopt(self.plan, collect=self.clear)
        self.assertEqual(self.before, self.content())

    def test_settings_change_is_not_overwritten_by_repair_or_rollback(self):
        adoption.fence(self.plan)
        def changed(stage):
            if stage == 'before-commit': self.settings.write_text('{"packages":["user-edited"]}')
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_SETTINGS_CHANGED'):
            adoption.adopt(self.plan, collect=self.clear, checkpoint=changed)
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_SETTINGS_CHANGED'):
            adoption.adopt(self.plan, collect=self.clear)
        adoption.rollback(self.plan)
        adoption.entries_action(self.plan)
        self.assertEqual(self.settings.read_text(), '{"packages":["user-edited"]}')
        self.assertTrue(self.entry.is_symlink())
        self.assertEqual(self.entry.resolve(), self.launcher_target)
        self.assertTrue(all(s['legacyUnverified'] and not s['adoptionPending'] for s in self.initialize()['scopes']))

    def test_repair_refuses_concurrent_marker_replacement(self):
        adoption.fence(self.plan)
        def crash(stage):
            if stage == 'marker': raise OSError('stop')
        with self.assertRaises(OSError): adoption.adopt(self.plan, collect=self.clear, checkpoint=crash)
        marker = Path(guard.plan(self.plan)['scopes'][0]['marker'])
        adoption.save(marker, {'unexpected': True})
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_MARKER_CHANGED'):
            adoption.adopt(self.plan, collect=self.clear)
        with self.assertRaisesRegex(guard.GuardError, 'ADOPTION_MARKER_CHANGED'): adoption.rollback(self.plan)

    def test_fence_failure_is_recoverable_and_never_changes_target_source(self):
        original = self.launcher_target.read_bytes()
        real_write = adoption.atomic_bytes
        def fail_entry(path, data, mode=0o600):
            if Path(path) == self.entry: raise OSError('injected entry save failure')
            return real_write(path, data, mode)
        with patch.object(adoption, 'atomic_bytes', fail_entry), self.assertRaises(OSError): adoption.fence(self.plan)
        self.assertTrue(self.entry.is_symlink())
        adoption.fence(self.plan)
        adoption.entries_action(self.plan)
        self.assertTrue(self.entry.is_symlink())
        self.assertEqual(original, self.launcher_target.read_bytes())

    def test_fenced_only_rollback_leaves_existing_marker_unchanged(self):
        marker = self.agent / '.biomind-resource-guard.json'
        adoption.save(marker, {'schema': guard.SCHEMA, 'root': str(self.agent), 'origin': 'born-guarded'})
        before = marker.read_bytes()
        adoption.fence(self.plan)
        adoption.rollback(self.plan)
        adoption.entries_action(self.plan)
        self.assertEqual(before, marker.read_bytes())

    def test_unknown_host_or_stale_evidence_never_adopts(self):
        adoption.fence(self.plan)
        for wrong in ({'host': 'undeclared'}, {'at': 0}, {'uid': -1}, {'status': 'BUSY'}):
            with self.assertRaises(guard.GuardError):
                adoption.adopt(self.plan, collect=lambda p: [{**self.clear(p)[0], **wrong}])
        self.assertTrue(all(not Path(s['marker']).exists() for s in guard.plan(self.plan)['scopes']))

    def test_resource_symlink_changed_after_audit_refused(self):
        adoption.fence(self.plan)
        def replaced(stage):
            if stage == 'after-audit':
                (self.agent / 'npm').rename(self.agent / 'npm-old')
                other = self.root / 'other-npm'; other.mkdir()
                (self.agent / 'npm').symlink_to(other)
        with self.assertRaisesRegex(guard.GuardError, 'RESOURCE_ROOT_CHANGED'):
            adoption.adopt(self.plan, collect=self.clear, checkpoint=replaced)

    def test_private_plan_review_and_no_cli_namespace_bypass(self):
        self.raw['maintenanceApproved'] = False
        adoption.save(self.plan_file, self.raw)
        with self.assertRaisesRegex(guard.GuardError, 'MAINTENANCE_REVIEW_REQUIRED'):
            adoption.fence(adoption.load_plan(self.plan_file))
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/plugin-maintenance.py'), 'audit-local',
                                 '--plan', str(self.plan_file), '--skip-host-check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.entry.is_symlink())


if __name__ == '__main__': unittest.main()

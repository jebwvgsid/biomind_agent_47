"""Synthetic proc and OS-service fixtures; no real host processes are inspected."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from agent_core import pi_plugin_adoption as adoption
from agent_core import pi_system_helpers as helpers


class SystemHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.uid = os.getuid()
        self.boot = '93c77b83-3db9-4279-b39b-57637ea378c6'
        (self.proc / 'sys/kernel/random').mkdir(parents=True)
        (self.proc / 'sys/kernel/random/boot_id').write_text(self.boot)
        self.user_group = f'/user.slice/user-{self.uid}.slice/user@{self.uid}.service'
        self.make_process(1, 0, '/usr/lib/systemd/systemd', uid=0, group='/')
        self.make_process(11, 1, '/usr/sbin/sshd', uid=0, group='/system.slice/sshd.service')
        self.make_process(12, 11, 'sshd: user [priv]', uid=0)
        self.make_process(13, 12, 'sshd: user@notty')
        self.make_process(21, 1, '/usr/lib/systemd/systemd', group=self.user_group)
        (self.proc / '21/exe').symlink_to('/usr/lib/systemd/systemd')
        self.make_process(22, 21, '(sd-pam)', group=self.user_group)
        self.make_process(23, 21, '/usr/bin/gnome-keyring-daemon', group=self.user_group,
                          caps=helpers.IPC_LOCK, fork=False)
        self.keyring_unit = 'dbus-:1.2-org.freedesktop.secrets@0.service'
        self.units = {
            'sshd.service': self.unit_value('sshd.service', 11, '/usr/sbin/sshd', '/system.slice/sshd.service'),
            f'user@{self.uid}.service': self.unit_value(f'user@{self.uid}.service', 21, '/usr/lib/systemd/systemd', self.user_group),
            self.keyring_unit: self.unit_value(self.keyring_unit, 23, '/usr/bin/gnome-keyring-daemon', self.user_group, user=True),
        }
        self.evidence = self.root / 'operator-evidence.json'
        self.evidence.write_text('{"status":"PASS","provenance":"synthetic fixture"}')
        self.evidence.chmod(0o600)
        self.patches = [patch.object(helpers, 'unit', side_effect=lambda name, **_: copy.deepcopy(self.units[name])),
                        patch.object(helpers, 'bus_owner', return_value={'name': 'org.freedesktop.secrets', 'owner': ':1.3', 'pid': 23}),
                        patch.object(helpers.os, 'getxattr', return_value=bytes.fromhex('0100000200400000000000000000000000000000'))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def make_process(self, pid, ppid, command, uid=None, caps=0, group='/session.scope', fork=True):
        uid = self.uid if uid is None else uid
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ['S', str(ppid)] + ['0'] * 17 + [str(pid * 100)]
        fields[6] = '64' if fork else '0'
        (directory / 'stat').write_text(f'{pid} (PRIVATE NAME) ' + ' '.join(fields))
        (directory / 'cmdline').write_bytes(command.encode() + b'\0')
        (directory / 'status').write_text(f'Uid:\t{uid} {uid} {uid} {uid}\nGid:\t{uid} {uid} {uid} {uid}\n'
                                          f'CapPrm:\t{caps:016x}\nCapEff:\t{caps:016x}\n'
                                          'CapInh:\t0000000000000000\nCapAmb:\t0000000000000000\nNoNewPrivs:\t0\n')
        (directory / 'uid_map').write_text('0 0 4294967295\n')
        (directory / 'cgroup').write_text('0::' + group + '\n')
        (directory / 'fd').mkdir(exist_ok=True)
        (directory / 'maps').write_text('')
        (directory / 'environ').write_bytes(b'PRIVATE_TOKEN=DO_NOT_EXPORT\0')
        return directory

    def unit_value(self, name, pid, executable, group, user=False):
        return {'name': name, 'userManager': user, 'mainPid': pid, 'controlPid': 0,
                'controlGroup': group, 'pamName': 'systemd-user' if name.startswith('user@') else '',
                'executable': {'path': executable, 'resolvedPath': executable, 'sha256': 'a' * 64},
                'unitFileFingerprints': [{'path': '/fixture/unit', 'sha256': 'b' * 64}]}

    def capture(self, pid=13, kind='ssh-login-helper'):
        return helpers.snapshot(pid, kind, 'test-host', self.uid, self.proc,
                                self.keyring_unit if kind == 'keyring-helper' else None)

    def review(self, pid=13, kind='ssh-login-helper'):
        value = {'schema': helpers.SCHEMA, 'operatorReviewed': True, 'host': 'test-host',
                 'bootId': self.boot, 'pid': pid, 'role': kind,
                 'identitySha256': helpers.fingerprint(self.capture(pid, kind)),
                 'operatorEvidence': [{'path': str(self.evidence), 'sha256': helpers.sha(self.evidence.read_bytes())}]}
        if kind == 'keyring-helper': value['keyringUnit'] = self.keyring_unit
        return value

    def test_three_positive_roles_require_exact_review_and_fresh_identity(self):
        for pid, kind in ((13, 'ssh-login-helper'), (22, 'pam-session-helper'), (23, 'keyring-helper')):
            with self.subTest(role=kind):
                value = helpers.reviewed_snapshot(self.review(pid, kind), 'test-host', self.uid, self.proc)
                self.assertEqual(value['process']['pid'], pid)
                self.assertEqual(value['role'], kind)
                self.assertNotIn('DO_NOT_EXPORT', json.dumps(value))

    def test_role_name_and_permission_hint_cannot_supply_review(self):
        review = self.review()
        for field, wrong in [('operatorReviewed', False), ('identitySha256', '0' * 64), ('host', 'other'),
                             ('bootId', 'different'), ('operatorEvidence', [])]:
            with self.subTest(field=field), self.assertRaises(helpers.HelperReviewError):
                helpers.reviewed_snapshot({**review, field: wrong}, 'test-host', self.uid, self.proc)

    def test_operator_evidence_changed_or_symlinked_is_refused(self):
        review = self.review()
        self.evidence.write_text('{"changed":true}')
        with self.assertRaisesRegex(helpers.HelperReviewError, 'EVIDENCE_CHANGED'):
            helpers.reviewed_snapshot(review, 'test-host', self.uid, self.proc)
        self.evidence.unlink()
        self.evidence.symlink_to(self.proc / '13/status')
        with self.assertRaisesRegex(helpers.HelperReviewError, 'EVIDENCE_CHANGED'):
            helpers.reviewed_snapshot(review, 'test-host', self.uid, self.proc)

    def test_pid_command_credentials_cgroup_and_parent_drift_refused(self):
        for path, replacement in [('13/cmdline', b'sshd: changed@notty\0'),
                                  ('13/cgroup', b'0::/different.scope\n'),
                                  ('12/cmdline', b'sshd: different-parent\0'),
                                  ('12/stat', b'12 (name) S 1 ' + b'0 ' * 17 + b'1200')]:
            review = self.review()
            file = self.proc / path
            before = file.read_bytes()
            file.write_bytes(replacement)
            with self.subTest(path=path), self.assertRaises(helpers.HelperReviewError):
                helpers.reviewed_snapshot(review, 'test-host', self.uid, self.proc)
            file.write_bytes(before)

    def test_pam_must_be_unexecuted_systemd_fork_with_pam_configuration(self):
        path = self.proc / '22/stat'
        before = path.read_bytes()
        fields = path.read_text().rsplit(')', 1)[1].split()
        fields[6] = '0'
        path.write_text('22 (name) ' + ' '.join(fields))
        with self.assertRaisesRegex(helpers.HelperReviewError, 'PAM_PROVENANCE'):
            self.capture(22, 'pam-session-helper')
        path.write_bytes(before)
        self.units[f'user@{self.uid}.service']['pamName'] = ''
        with self.assertRaisesRegex(helpers.HelperReviewError, 'PAM_PROVENANCE'):
            self.capture(22, 'pam-session-helper')

    def test_ssh_unit_must_bind_actual_root_master(self):
        self.units['sshd.service']['mainPid'] = 99
        with self.assertRaisesRegex(helpers.HelperReviewError, 'SSH_PROVENANCE'):
            self.capture()

    def test_exact_main_pid_may_reside_in_systemd_init_subscope(self):
        for pid in (21, 22):
            (self.proc / str(pid) / 'cgroup').write_text('0::' + self.user_group + '/init.scope\n')
        self.assertEqual(self.capture(22, 'pam-session-helper')['process']['pid'], 22)
        self.assertFalse(helpers.unit_contains(self.user_group, [self.user_group + '-unrelated/init.scope']))

    def test_keyring_transient_unit_needs_bus_owner_and_exact_capability(self):
        with patch.object(helpers, 'bus_owner', return_value={'pid': 99}):
            with self.assertRaisesRegex(helpers.HelperReviewError, 'KEYRING_PROVENANCE'):
                self.capture(23, 'keyring-helper')
        with patch.object(helpers.os, 'getxattr', return_value=struct.pack('<IIIII', 0x02000001, helpers.IPC_LOCK | 1, 0, 0, 0)):
            with self.assertRaisesRegex(helpers.HelperReviewError, 'CAPABILITIES_INVALID'):
                self.capture(23, 'keyring-helper')
        with self.assertRaisesRegex(helpers.HelperReviewError, 'KEYRING_UNIT_REQUIRED'):
            helpers.snapshot(23, 'keyring-helper', 'test-host', self.uid, self.proc, 'arbitrary.service')

    def test_nested_uid_namespace_and_user_writable_provenance_are_refused(self):
        (self.proc / '13/uid_map').write_text(f'0 {self.uid} 1\n')
        with self.assertRaisesRegex(helpers.HelperReviewError, 'INITIAL_UID_NAMESPACE'):
            self.capture()
        with self.assertRaisesRegex(helpers.HelperReviewError, 'SYSTEM_FILE_WRITABLE'):
            helpers.trusted_file(self.evidence)

    def audit_fixture(self, reviews, pids=(13,)):
        sessions = self.root / 'sessions'
        sessions.mkdir(exist_ok=True)
        target = self.root / 'agent'
        target.mkdir(exist_ok=True)
        plan = {'hosts': [{'name': 'test-host', 'reviewedSystemHelpers': reviews}], '_sha256': 'f' * 64,
                'legacyCodeRoots': [], 'sessionRoots': [str(sessions)], 'scheduler': 'none'}
        original_path, original_iterdir, original_readlink = Path, Path.iterdir, os.readlink
        original_review, original_refs, original_process = helpers.reviewed_snapshot, helpers.visible_references, helpers.process
        cwd_attempts = []
        def path(value):
            value = Path(value)
            return self.proc / value.relative_to('/proc') if value.is_relative_to('/proc') else value
        def iterdir(value):
            return iter(self.proc / str(pid) for pid in pids) if value == self.proc else original_iterdir(value)
        def readlink(value):
            if Path(value).name == 'cwd':
                cwd_attempts.append(str(value))
                raise PermissionError(13, 'DO_NOT_LEAK')
            return original_readlink(value)
        with patch.object(adoption, 'Path', side_effect=path), patch.object(Path, 'iterdir', iterdir), \
                patch.object(adoption.socket, 'gethostname', return_value='test-host'), \
                patch.object(adoption, 'ancestors', return_value=set()), \
                patch.object(adoption.guard, 'plan', return_value={'scopes': [{'root': str(target)}], 'resources': []}), \
                patch.object(os, 'readlink', readlink), \
                patch.object(helpers, 'reviewed_snapshot', side_effect=lambda r, h, u: original_review(r, h, u, self.proc)), \
                patch.object(helpers, 'visible_references', side_effect=lambda pid: original_refs(pid, self.proc)), \
                patch.object(helpers, 'process', side_effect=lambda p, pid: original_process(self.proc if p == original_path('/proc') else p, pid)):
            result = adoption.audit_local(plan, require_host_namespace=False)
        return result, cwd_attempts

    def test_audit_records_exact_helper_proof_without_retrying_failed_cwd(self):
        result, attempts = self.audit_fixture([self.review()])
        self.assertEqual(result['status'], 'clear', result)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result['reviewedSystemHelpers'][0]['unreadableResourceFields'], ['cwd'])
        self.assertFalse(result['reviewedSystemHelpers'][0]['unreadableResourcesRead'])
        self.assertEqual(result['reviewedSystemHelpers'][0]['reviewScope'], 'THIS_EXACT_SYSTEM_HELPER_ONLY')
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(result))

    def test_unknown_unreadable_child_does_not_inherit_parent_review(self):
        self.make_process(14, 13, '/usr/bin/python3')
        result, _ = self.audit_fixture([self.review()], (13, 14))
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['unreadablePids'], [14])
        self.assertEqual(len(result['reviewedSystemHelpers']), 1)

    def test_visible_argv_resource_wins_before_opaque_field_or_review(self):
        review = self.review()
        (self.proc / '13/cmdline').write_bytes(b'sshd: user@notty\0' + os.fsencode(str(self.root / 'agent/file')) + b'\0')
        result, attempts = self.audit_fixture([review])
        self.assertEqual(result['status'], 'BUSY')
        self.assertEqual(attempts, [])
        self.assertEqual(result['reviewedSystemHelpers'], [])
        self.assertEqual(result['legacyProcesses'][0]['reason'], 'resource-reference')

    def test_visible_fd_maps_and_environment_resource_always_win(self):
        review = self.review()
        for source in ('fd', 'maps', 'environ'):
            target = str(self.root / 'agent/file')
            file = self.proc / '13' / ('fd/8' if source == 'fd' else source)
            old = None if source == 'fd' else file.read_bytes()
            if source == 'fd': file.symlink_to(target)
            elif source == 'maps': file.write_text('000-111 r--p 0 00:00 1 ' + target + '\n')
            else: file.write_bytes(b'PI_CODING_AGENT_DIR=' + os.fsencode(target) + b'\0')
            with self.subTest(source=source):
                result, _ = self.audit_fixture([review])
                self.assertEqual(result['status'], 'BUSY', result)
                self.assertEqual(result['reviewedSystemHelpers'], [])
            if old is None: file.unlink()
            else: file.write_bytes(old)

    def test_invalid_or_missing_review_keeps_original_unreadable_blocker(self):
        review = self.review()
        for rows in ([], [{**review, 'identitySha256': '0' * 64}]):
            result, attempts = self.audit_fixture(rows)
            self.assertEqual(result['status'], 'BLOCKED')
            self.assertEqual(result['unreadablePids'], [13])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(result['reviewedSystemHelpers'], [])


if __name__ == '__main__':
    unittest.main()

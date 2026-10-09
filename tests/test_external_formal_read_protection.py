"""Synthetic, real bwrap probes. No providers, dependency installs or real inputs.

Tests of mount construction are intentionally distinct from Domain UI approval
tests; simulated host decisions are never evidence of human TUI/remote approval.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_core.command_execution import (
    CommandExecutionService, FormalProtectionError, formal_access_projection,
    formal_protected_spawn, formal_protection_capability,
)


class FormalReadProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-formal-ro-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.external = self.root / 'external'
        self.external.mkdir()
        self.source = self.external / 'input.txt'
        self.source.write_text('original\n')
        self.service = CommandExecutionService(data_path=self.project / '.biomind' / 'data',
            project_dir=self.project, runtime_locations=(set(), '', ['/usr/bin', '/bin']))
        self.service.install_dir = self.root / 'installation'
        self.service.install_dir.mkdir()
        info = self.source.stat()
        self.read = {'grant_id': 'synthetic-mount-fixture', 'root': str(self.source),
            'anchor': [info.st_dev, info.st_ino], 'anchor_type': 'file', 'recursive': False}
        self.projection = formal_access_projection(self.service, {}, 'test',
            read_access={'reads': [self.read], 'policy_revision': 'test-only'})
        try: formal_protection_capability(self.projection)
        except FormalProtectionError as exc: self.skipTest(str(exc))
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def grant(self, target):
        from agent_core import execution_permissions as ep
        ep._mutate(self.service.data_path,'test',lambda s:s.setdefault('task_state',{}).update(pi_session_id='mount-fixture'))
        payload=ep.path_access_payload(self.service,'test',[{'path':str(target),'access':'read_write','target_kind':'directory' if target.is_dir() else 'file','recursive':target.is_dir()}])
        row=ep.request_permission(self.service.data_path,'test','grant-'+str(target),payload)
        ep.record_human_decision(self.service.data_path,'test',ep._Decision(row['request_id'],row['action_hash'],'y',ep._SEAL))

    def run_protected(self, command, *, projection=None, extra_env=None):
        observed = {}
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(self.project), **(extra_env or {})}
        with formal_protected_spawn(projection or self.projection, command,
                cwd=self.project, env=environment, observation=observed) as (argv, fds):
            if extra_env:
                for value in extra_env.values():
                    self.assertNotIn(value, '\x00'.join(argv))
            result = subprocess.run(argv, cwd='/', env={}, pass_fds=fds,
                close_fds=True, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        return result, observed

    def test_shell_and_argv_read_output_and_network_environment_preserved(self):
        source = str(self.source)
        command = f'cat "{source}" > result.txt; printf "$BIO_SYNTHETIC" >> result.txt; echo done'
        result, observed = self.run_protected(command, extra_env={'BIO_SYNTHETIC': 'environment'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / 'result.txt').read_text(), 'original\nenvironment')
        code = 'import pathlib; print(pathlib.Path(' + repr(source) + ').read_text().strip())'
        result, _ = self.run_protected(['/usr/bin/python3', '-u', '-c', code])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'original')
        # The outer test host may already deny networking; the formal wrapper
        # must preserve that result rather than impose inspection's seccomp.
        network = ['/usr/bin/python3', '-c', 'import socket; socket.socket().close()']
        baseline = subprocess.run(network, capture_output=True)
        inside, _ = self.run_protected(network)
        self.assertEqual(inside.returncode, baseline.returncode, inside.stderr)
        self.assertEqual(observed['filesystem_protection']['policy'], 'formal-input-ro-v1')

    def test_all_source_mutations_and_subprocess_blocked(self):
        code = '''import os,pathlib,subprocess,json
p=pathlib.Path(SOURCE); out={}
operations={'write':lambda:p.write_text('bad'),'truncate':lambda:os.truncate(p,0),
 'unlink':p.unlink,'rename':lambda:p.rename(p.with_name('moved')),
 'new_hardlink':lambda:os.link(p,pathlib.Path('new-alias'))}
for name, action in operations.items():
 try: action(); out[name]='ESCAPED'
 except OSError as e: out[name]=type(e).__name__
out['child']=subprocess.run(['/bin/sh','-c','printf bad > '+str(p)],capture_output=True).returncode
pathlib.Path('output.json').write_text(json.dumps(out)); print(json.dumps(out))
'''.replace('SOURCE', repr(str(self.source)))
        script = self.project / 'science.py'; script.write_text(code)
        result, _ = self.run_protected(['/usr/bin/python3', '-u', str(script)])
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertNotIn('ESCAPED', observed.values())
        self.assertNotEqual(observed['child'], 0)
        self.assertTrue((self.project / 'output.json').is_file())

    def test_symlink_unknown_dynamic_input_control_state_and_fd_aliases(self):
        (self.project / 'alias').symlink_to(self.source)
        (self.root / 'unknown').write_text('unapproved')
        code = '''import pathlib,os,json
out={}
for name,path in [('symlink','alias'),('control',CONTROL),('unknown',UNKNOWN)]:
 try:
  if name=='unknown': pathlib.Path(path).read_text()
  else: pathlib.Path(path).write_text('bad')
  out[name]='ESCAPED'
 except OSError as e: out[name]=type(e).__name__
out['fds']={n:os.readlink('/proc/self/fd/'+n) for n in os.listdir('/proc/self/fd') if n.isdigit() and int(n)>2 and os.path.exists('/proc/self/fd/'+n)}
print(json.dumps(out))
'''.replace('CONTROL', repr(str(self.service.data_path / 'state'))).replace('UNKNOWN', repr(str(self.root / 'unknown')))
        fd = os.open(self.source, os.O_RDWR)
        os.set_inheritable(fd, True)
        try: result, _ = self.run_protected(['/usr/bin/python3', '-u', '-c', code])
        finally: os.close(fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertNotIn('ESCAPED', observed.values())
        self.assertEqual(observed['fds'], {})

    def test_hardlink_overlap_anchor_swap_and_missing_capability_refused(self):
        alias = self.project / 'hardlink'
        os.link(self.source, alias)
        with self.assertRaisesRegex(FormalProtectionError, 'hardlink_in_writable_scope'):
            self.run_protected('true')
        alias.unlink()
        self.grant(self.external)
        with self.assertRaisesRegex(FormalProtectionError, 'input_output_overlap'):
            formal_access_projection(self.service, {'write_paths': [str(self.external)]}, 'test',
                read_access={'reads': [self.read]})
        self.external.rename(self.root / 'original-external')
        self.external.mkdir()
        self.source.write_text('replacement')
        try:
            with self.assertRaisesRegex(FormalProtectionError, 'read_anchor_changed_at_mount'):
                with formal_protected_spawn(self.projection, 'true', cwd=self.project): pass
        finally:
            self.source.unlink(); self.external.rmdir(); (self.root / 'original-external').rename(self.external)
        projection = {**self.projection, 'bwrap_path': str(self.root / 'missing-bwrap')}
        with self.assertRaisesRegex(FormalProtectionError, 'bubblewrap_missing'):
            with formal_protected_spawn(projection, 'touch SHOULD_NOT_EXIST', cwd=self.project): pass
        self.assertFalse((self.project / 'SHOULD_NOT_EXIST').exists())

    def test_environment_parameter_does_not_authorize_unknown_data_root(self):
        for key in ('ENV_PREFIX', 'CONDA_PREFIX'):
            with self.assertRaisesRegex(FormalProtectionError, 'unregistered_environment_prefix'):
                formal_access_projection(self.service, {'env': {key: str(self.external)}},
                    'test', read_access={'reads': []})

    def test_project_nested_in_runtime_keeps_outputs_writable(self):
        self.service.install_dir = self.root
        projection = formal_access_projection(self.service, {}, 'test',
            read_access={'reads': [self.read], 'policy_revision': 'test-only'})
        result, _ = self.run_protected('echo nested > nested.txt', projection=projection)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / 'nested.txt').read_text(), 'nested\n')

    def test_external_write_file_is_not_promoted_to_parent_scope(self):
        output = self.root / 'output'; output.mkdir()
        exact = output / 'one.txt'; exact.write_text('before')
        sibling = output / 'other.txt'; sibling.write_text('unchanged')
        self.grant(exact)
        projection = formal_access_projection(self.service, {'write_paths': [str(exact)]},
            'test', read_access={'reads': [self.read]})
        self.assertIn(str(exact), projection['write_roots'])
        self.assertNotIn(str(output), projection['write_roots'])
        command = 'printf after > '+str(exact)+'; printf bad > '+str(sibling)
        result, _ = self.run_protected(command, projection=projection)
        self.assertEqual(exact.read_text(), 'after')
        self.assertEqual(sibling.read_text(), 'unchanged')
        with self.assertRaisesRegex(FormalProtectionError, 'external_write_permission_required'):
            formal_access_projection(self.service, {'write_paths': [str(output/'new.txt')]},
                'test', read_access={'reads': [self.read]})

    def test_recursive_directory_formal_protection_is_explicitly_unavailable(self):
        info=self.external.stat()
        read={**self.read, 'root':str(self.external), 'anchor':[info.st_dev,info.st_ino],
            'anchor_type':'directory', 'recursive':True}
        with self.assertRaisesRegex(FormalProtectionError,'recursive_read_privacy_filter_unavailable'):
            formal_access_projection(self.service,{},'test',read_access={'reads':[read]})


if __name__ == '__main__': unittest.main(verbosity=2)

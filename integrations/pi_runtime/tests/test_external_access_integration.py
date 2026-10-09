"""Actual domain/formal integration, synthetic inputs and simulated host choices only."""
import asyncio
import importlib.abc
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parents[1]
sys.path[:0] = [str(RUNTIME), str(REPO)]

class NoProvider(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'config' or fullname.startswith('config.') or fullname in {
                'bio_agents', 'agent_core.provider_attempt', 'agent_core.tool_loop', 'dotenv'}:
            raise ImportError('SYNTHETIC_TEST_FORBIDS_PROVIDER:' + fullname)
sys.meta_path.insert(0, NoProvider())

from domain_adapter.service import DomainService, DomainError
from agent_core import execution_permissions as ep
from agent_core.command_execution import FormalProtectionError

class ExternalAccessIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-access-integration-')
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'; self.project.mkdir()
        self.inputs = self.root / 'inputs'; self.inputs.mkdir()
        self.source = self.inputs / 'source.txt'; self.source.write_text('original')
        registry = self.root / 'registry.json'; registry.write_text('{"environments":{},"tools":{}}')
        preferences = self.root / 'preferences.md'; preferences.write_text('synthetic')
        skills = self.root / 'skills'; skills.mkdir()
        self.s = DomainService(cwd=self.project, data_root=self.project / '.biomind' / 'domain',
            install_root=REPO, session_id='synthetic-integration', registry_path=registry,
            skills_root=skills, preferences_path=preferences)
        await self.s.call('request.begin', {'request_id':'analysis', 'role':'generate'}, host_token=self.s.host_token)

    async def asyncTearDown(self):
        for event in self.s._install_cancels.values(): event.set()
        await self.s.formal.shutdown()
        if self.s.active_jobs(): await asyncio.gather(*self.s.active_jobs(), return_exceptions=True)
        self.assertEqual(self.source.read_text(), 'original')
        evidence = os.environ.get('BIOMIND_ACCESS_TEST_EVIDENCE')
        if evidence:
            import hashlib
            out = Path(evidence) / self._testMethodName; out.mkdir(parents=True,exist_ok=True)
            # Persist synthetic receipts before TemporaryDirectory cleanup.
            # Environments may contain host values; never archive their values.
            def clean(value):
                if isinstance(value,dict):
                    return {k:clean(v) for k,v in value.items() if k.lower() not in
                        {'env','environment','execution_env','host_token','token','api_key'}}
                if isinstance(value,list): return [clean(v) for v in value]
                return value
            for path in self.s.data_root.rglob('*.json'):
                try: value = clean(json.loads(path.read_text()))
                except (OSError,ValueError): continue
                dest = out/'receipts'/path.relative_to(self.s.data_root)
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_text(json.dumps(value,ensure_ascii=False,indent=2))
            (out/'identity-and-source.json').write_text(json.dumps({'project':str(self.project),
                'session_id':self.s.session_id,'task_id':self.s.task_id,'request_scope':self.s.scope(),
                'source_sha256':hashlib.sha256(self.source.read_bytes()).hexdigest(),
                'source_unchanged':True,'approval_input':'SIMULATED_HOST_ONLY',
                'environment_values':'OMITTED_FROM_ARCHIVED_RECEIPTS'},indent=2))
        self.tmp.cleanup()

    async def approve(self, pending, choice='y'):
        a = pending['approval']
        return await self.s.call('approval.decide', {**{k:a[k] for k in ('request_id','action_hash','request_scope')},
            'choice':choice}, host_token=self.s.host_token)

    async def grant(self):
        p = await self.s.call('files.read_authorize', {'path':str(self.source),'call_id':'source'}, host_token=self.s.host_token)
        await self.approve(p, 'y')

    async def terminal(self, value):
        for _ in range(100):
            if value.get('terminal'): return value
            value = await self.s.call('exec.wait', {'run_ref':value['run_ref'],'max_wait_ms':100})
        self.fail('synthetic command did not finish')

    async def start(self, arguments, call='command'):
        params = {'call_id':call,'arguments':arguments}
        pending = await self.s.call('exec.start', params)
        self.assertEqual(pending['status'], 'waiting_approval', pending)
        await self.approve(pending)
        return params, await self.terminal(await self.s.call('exec.start', params))

    async def test_revoke_requires_complete_cas_and_model_authority_is_rejected(self):
        await self.grant()
        status = await self.s.call('permissions.status', {}, host_token=self.s.host_token)
        valid = {'grant_id':status['grants'][0]['grant_id'], 'request_scope':status['request_scope'],
                 'policy_revision':status['policy_revision']}
        for missing in valid:
            with self.assertRaisesRegex(DomainError, 'CAS_REQUIRED'):
                await self.s.call('permissions.revoke', {k:v for k,v in valid.items() if k!=missing}, host_token=self.s.host_token)
        with self.assertRaisesRegex(DomainError, 'HOST_CHANNEL_REQUIRED'):
            await self.s.call('permissions.revoke', valid)
        for field in ('access_projection','read_access','grants','mounts','approved'):
            with self.assertRaisesRegex(DomainError, 'AUTHORITY_FIELD_FORBIDDEN'):
                await self.s.call('exec.start', {'call_id':'forged','arguments':{'cmd':'true',field:{}}})
        self.assertEqual(len(ep.active_read_grants(self.s.data_root,self.s.task_id)), 1)

    async def test_manifest_150_samples_approval_stays_compact_and_directory_execution_runs(self):
        for i in range(150): (self.inputs / f'{i}.txt').write_text(str(i))
        manifest = self.project / 'samples.json'
        manifest.write_text(json.dumps([str(self.inputs / f'{i}.txt') for i in range(150)]))
        code = 'import json,pathlib; p=json.loads(pathlib.Path("samples.json").read_text()); pathlib.Path("total").write_text(str(sum(int(pathlib.Path(x).read_text()) for x in p)))'
        args = {'call_id':'manifest','arguments':{'argv':[sys.executable,'-I','-u','-c',code], 'read_paths':[str(self.inputs)]}}
        read = await self.s.call('exec.start',args)
        self.assertEqual(read['approval']['payload']['kind'],'file_read')
        self.assertLess(len(json.dumps(read['approval'])), 6000)
        await self.approve(read)
        command = await self.s.call('exec.start',args)
        self.assertEqual(command['status'],'waiting_approval',command)
        self.assertLess(len(json.dumps(command['approval'])),6000)
        await self.approve(command)
        result=await self.terminal(await self.s.call('exec.start',args))
        self.assertEqual(result['exit_code'],0,result)
        self.assertEqual((self.project/'total').read_text(),str(sum(range(150))))
        stat = await self.s.call('files.read_authorize',{'path':str(self.inputs/'149.txt'),'call_id':'retained'},host_token=self.s.host_token)
        self.assertEqual(stat['status'],'completed')

    async def test_parent_directory_approval_runs_two_exact_files_and_does_not_poison_pwd(self):
        other=self.inputs/'helper.py';other.write_text('companion')
        sibling=self.inputs/'unrequested.txt';sibling.write_text('not mounted')
        p=await self.s.call('files.read_authorize',{'path':str(self.source),'call_id':'parent-grant'},host_token=self.s.host_token)
        await self.approve(p,'scope')
        code=('from pathlib import Path;import json;'
              f'print(json.dumps([Path({str(self.source)!r}).read_text(),'
              f'Path({str(other)!r}).read_text(),Path({str(sibling)!r}).exists()]));'
              f'Path({str(self.source)!r}).write_text("must fail")')
        args,result=await self.start({'argv':[sys.executable,'-I','-c',code],
            'read_paths':[str(self.source),str(other)]},call='two-files-from-parent')
        self.assertNotEqual(result['exit_code'],0)
        self.assertEqual(json.loads(result['stdout']),['original','companion',False])
        self.assertIn('Read-only file system',result['stderr'])
        self.assertEqual(self.source.read_text(),'original')
        _,pwd=await self.start({'argv':['pwd']},call='unrelated-pwd')
        self.assertEqual(pwd['exit_code'],0)
        self.assertEqual(pwd['stdout'].strip(),str(self.project))
        state=self.s._state();requests=state['task_state']['execution_permissions']['requests'].values()
        reads=[r for r in requests if r['payload']['kind']=='file_read']
        self.assertEqual(sum(r.get('source')=='human' for r in reads),1)
        self.assertTrue(all(r['status']=='allowed' for r in reads))

    async def test_dynamic_missing_input_preserves_effects_and_does_not_reexecute(self):
        await self.grant()
        unknown = self.root / 'unapproved'; unknown.write_text('outside')
        code = f'import pathlib; p=pathlib.Path("effect"); p.write_text(p.read_text()+"x" if p.exists() else "x"); pathlib.Path({str(unknown)!r}).read_text()'
        args, result = await self.start({'argv':[sys.executable,'-I','-c',code]})
        self.assertNotEqual(result['exit_code'],0)
        self.assertEqual((self.project/'effect').read_text(),'x')
        again = await self.s.call('exec.start',args)
        self.assertEqual(again['run_ref'],result['run_ref'])
        self.assertEqual((self.project/'effect').read_text(),'x')
        self.assertTrue(Path(result['receipt_ref']).is_file())

    async def test_install_and_verification_use_same_read_only_projection(self):
        await self.grant()
        source = shlex.quote(str(self.source)); prefix = self.project/'new-env'
        command = f'mkdir {shlex.quote(str(prefix))}; cat {source} > install-read; if printf bad > {source}; then exit 31; fi; printf main > install-effect'
        verify = f'cat {source} > verify-read; if rm {source}; then exit 32; fi; test -d {shlex.quote(str(prefix))}'
        params = {'call_id':'install','arguments':{'cmd':command,'prefix':str(prefix),
            'env_name':'synthetic-no-dependencies','verification_commands':[verify], 'packages':[]}}
        pending = await self.s.call('install.propose',params); await self.approve(pending)
        result = await self.s.call('install.execute',params)
        self.assertEqual(result['status'],'registered',result)
        self.assertEqual(result['verification'][0]['exit_code'],0)
        for name in ('install-read','verify-read'): self.assertEqual((self.project/name).read_text(),'original')
        self.assertTrue((await self.s.call('install.execute',params))['replayed'])

    async def test_missing_execution_capability_keeps_file_grant_and_never_spawns(self):
        await self.grant()
        params = {'call_id':'missing','arguments':{'cmd':'touch SHOULD_NOT_EXIST'}}
        p = await self.s.call('exec.start',params); await self.approve(p)
        self.assertNotIn('runtime_roots',p['approval']['payload']['access_projection'])
        self.assertNotIn('deny_paths',p['approval']['payload']['access_projection'])
        with patch('agent_core.command_execution.formal_protection_capability',
                   side_effect=FormalProtectionError('synthetic_namespace_unavailable')):
            result = await self.terminal(await self.s.call('exec.start',params))
        self.assertEqual(result['status'],'stage_blocked',result)
        self.assertFalse(result['execution_started'])
        self.assertFalse((self.project/'SHOULD_NOT_EXIST').exists())
        stat = await self.s.call('files.read_authorize', {'path':str(self.source),'call_id':'still-readable'}, host_token=self.s.host_token)
        self.assertEqual(stat['status'],'completed')

    async def test_install_revocation_before_verification_preserves_real_effects(self):
        await self.grant()
        prefix=self.project/'interrupted-env'
        params={'call_id':'install-revoke','arguments':{'cmd':f'mkdir {shlex.quote(str(prefix))}; printf once > effect; touch ready; sleep .4',
            'env_name':'synthetic-revoked','prefix':str(prefix),
            'verification_commands':[f'cat {shlex.quote(str(self.source))} > SHOULD_NOT_EXIST']}}
        pending=await self.s.call('install.propose',params); await self.approve(pending)
        task=asyncio.create_task(self.s.call('install.execute',params))
        for _ in range(200):
            if (self.project/'ready').exists(): break
            await asyncio.sleep(.01)
        self.assertTrue((self.project/'ready').exists())
        status=await self.s.call('permissions.status',{},host_token=self.s.host_token)
        await self.s.call('permissions.revoke',{'grant_id':status['grants'][0]['grant_id'],
            'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},host_token=self.s.host_token)
        result=await task
        self.assertEqual(result['status'],'verification_blocked',result)
        self.assertEqual(result['exit_code'],0)
        self.assertFalse(result['verification'][0]['execution_started'])
        self.assertEqual((self.project/'effect').read_text(),'once')
        self.assertFalse((self.project/'SHOULD_NOT_EXIST').exists())
        self.assertEqual(ep.permission_state(self.s.data_root,self.s.task_id,pending['approval']['request_id'])['result'],result)

    async def test_revocation_blocks_future_launch_but_existing_owner_uses_cancel(self):
        await self.grant()
        params = {'call_id':'long','arguments':{'argv':[sys.executable,'-I','-u','-c','import time; print("running",flush=True); time.sleep(5)']}}
        p = await self.s.call('exec.start',params); await self.approve(p)
        running = await self.s.call('exec.start',params)
        status = await self.s.call('permissions.status',{},host_token=self.s.host_token)
        await self.s.call('permissions.revoke',{'grant_id':status['grants'][0]['grant_id'],
            'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},host_token=self.s.host_token)
        current = await self.s.call('exec.status',{'run_ref':running['run_ref']})
        self.assertFalse(current.get('terminal'))
        with self.assertRaises(ep.PermissionDenied): await self.s.call('exec.start',params)
        stopped = await self.s.call('exec.cancel',{'run_ref':running['run_ref']})
        self.assertTrue(stopped.get('terminal')); self.assertTrue(stopped.get('cleanup_confirmed'))

if __name__ == '__main__': unittest.main(verbosity=2)

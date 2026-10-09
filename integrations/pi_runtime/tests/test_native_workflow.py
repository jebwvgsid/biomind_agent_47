"""Real Domain/local process tests. Synthetic files only; no model or installs."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
from test_external_access_integration import DomainService, DomainError, REPO
from agent_core.execution_profile import release_host_profile
from agent_core.pending_input import mutate


class NativeWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-native-')
        self.root = Path(self.tmp.name)
        self.A, self.B, self.O = [self.root/n for n in ['A','B 中文 space','O']]
        for p in [self.A,self.B,self.O]: p.mkdir()
        self.registry=self.root/'registry.json'; self.registry.write_text('{"environments":{},"tools":{}}')
        self.prefs=self.root/'preferences.md'; self.prefs.write_text('synthetic')
        self.kw=dict(cwd=self.A, data_root=self.A/'.biomind/domain',install_root=REPO,
                     session_id='native-fixture',registry_path=self.registry,preferences_path=self.prefs,
                     skills_root=self.root/'skills')
        self.s=DomainService(**self.kw,execution_profile='native')
        await self.call('request.begin',{'request_id':'r1','role':'consult'},host=True)

    async def asyncTearDown(self):
        await self.s.formal.shutdown()
        if self.s.active_jobs(): await asyncio.gather(*self.s.active_jobs(),return_exceptions=True)
        evidence=os.environ.get('BIOMIND_NATIVE_TEST_EVIDENCE')
        if evidence:
            def clean(v):
                if isinstance(v,dict): return {k:clean(x) for k,x in v.items() if k not in {'env','host_token','token'}}
                if isinstance(v,list): return [clean(x) for x in v]
                return v
            target=Path(evidence)/self._testMethodName;target.mkdir(parents=True,exist_ok=True)
            for source in self.s.data_root.rglob('*.json'):
                dest=target/source.relative_to(self.s.data_root);dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_text(json.dumps(clean(json.loads(source.read_text())),ensure_ascii=False,indent=2))
        self.tmp.cleanup()

    async def call(self,m,p=None,host=False):
        return await self.s.call(m,p or {},host_token=self.s.host_token if host else None)

    async def terminal(self,r):
        for _ in range(150):
            if r.get('terminal'): return r
            r=await self.call('exec.wait',{'run_ref':r['run_ref'],'max_wait_ms':100})
        self.fail(str(r))

    async def run_code(self,code,call='run',**args):
        p={'call_id':call,'arguments':{'argv':[sys.executable,'-I','-B','-c',code],'workdir':str(self.B),**args}}
        return p,await self.terminal(await self.call('exec.start',p))

    def assert_no_permissions(self):
        self.assertEqual(self.s._state()['task_state'].get('execution_permissions',{}).get('requests',{}),{})

    async def test_T13_native_external_cwd_output_no_contract_no_bwrap(self):
        (self.B/'input').write_text('real input')
        self.s.commands.bwrap='/synthetic-no-bwrap'
        with patch('agent_core.command_execution.formal_protected_spawn',side_effect=AssertionError('native used bwrap')):
            _,r=await self.run_code(f"from pathlib import Path;print(Path.cwd());Path({str(self.O/'result')!r}).write_text(Path('input').read_text())")
        self.assertEqual(r.get('exit_code'),0,r)
        self.assertEqual((self.O/'result').read_text(),'real input')
        self.assertTrue(Path(r['receipt_ref']).is_file())
        self.assertIn(str(self.B),Path(r['log_refs']['stdout']).read_text())
        self.assertEqual(self.s.cwd,self.A);self.assert_no_permissions()
        record=await self.call('exec.status',{'run_ref':r['run_ref'],'record':'command'})
        self.assertIn(str(self.B),str(record['execution_record']))

    async def test_T14_replay_and_new_connection_one_effect(self):
        p,r=await self.run_code("from pathlib import Path;p=Path('counter');p.write_text(p.read_text()+'x' if p.exists() else 'x')")
        self.assertEqual(r.get('exit_code'),0,r)
        again=await self.call('exec.start',p);self.assertEqual(again['run_ref'],r['run_ref'])
        await self.s.disconnected()
        self.s=DomainService(**self.kw,execution_profile='native',session_existing=True)
        again=await self.call('exec.start',p);self.assertEqual(again.get('exit_code'),0,again)
        self.assertEqual((self.B/'counter').read_text(),'x');self.assert_no_permissions()

    async def test_T15_unknown_receipt_never_relaunches(self):
        p,r=await self.run_code("from pathlib import Path;p=Path('counter');p.write_text(p.read_text()+'x' if p.exists() else 'x')")
        self.assertEqual(r.get('exit_code'),0,r)
        Path(r['receipt_ref']).unlink()  # Crash fixture: intent exists, terminal evidence lost.
        self.s=DomainService(**self.kw,execution_profile='native',session_existing=True)
        again=await self.call('exec.start',p)
        self.assertEqual(again['status'],'reconciliation_unknown',again)
        self.assertEqual((self.B/'counter').read_text(),'x')
        self.assertTrue((await self.call('session.lifecycle'))['active_jobs'])

    async def test_T16_cancel_cleans_descendant_and_replay(self):
        code="import subprocess,time,pathlib;p=subprocess.Popen(['sleep','30']);pathlib.Path('child.pid').write_text(str(p.pid));time.sleep(30)"
        p={'call_id':'long','arguments':{'argv':[sys.executable,'-I','-B','-c',code],'workdir':str(self.B)}}
        r=await self.call('exec.start',p)
        for _ in range(100):
            if (self.B/'child.pid').exists():break
            await asyncio.sleep(.02)
        child=int((self.B/'child.pid').read_text())
        await self.call('exec.cancel',{'run_ref':r['run_ref'],'reason':'synthetic cancellation'})
        r=await self.terminal(r);self.assertEqual(r['status'],'cancelled',r);self.assertTrue(r['cleanup_confirmed'])
        proc=Path(f'/proc/{child}/stat')
        self.assertTrue(not proc.exists() or proc.read_text().split(')')[1].split()[0]=='Z')
        self.assertEqual((await self.call('exec.start',p))['status'],'cancelled')

    async def test_T17_T19_migration_pending_not_replayed_and_requires_new_request(self):
        await self.s.disconnected()
        self.s=DomainService(**{**self.kw,'session_id':'old'},execution_profile='protected')
        await self.call('request.begin',{'request_id':'old-request','role':'consult'},True)
        (self.B/'file').write_text('input')
        r=await self.call('files.read_authorize',{'call_id':'old-read','path':str(self.B/'file')},True)
        self.assertEqual(r['status'],'waiting_approval')
        # Preserve a genuine denied decision and another cancelled row too.
        row_id=r['approval']['request_id']
        def fixtures(state):
            rows=state['task_state']['execution_permissions']['requests']
            rows['prior-denied']={**rows[row_id],'request_id':'prior-denied','status':'denied'}
            rows['prior-cancelled']={**rows[row_id],'request_id':'prior-cancelled','status':'cancelled'}
        mutate(self.s.data_root,self.s.task_id,fixtures)
        with self.assertRaisesRegex(ValueError,'MIGRATION_BUSY'):
            DomainService(**{**self.kw,'session_id':'old'},execution_profile='native',migrate_profile=True)
        release_host_profile(self.s)  # Host exited while waiting, no replay.
        self.s=DomainService(**{**self.kw,'session_id':'old'},execution_profile='native',migrate_profile=True)
        rows=self.s._state()['task_state']['execution_permissions']['requests']
        self.assertEqual(rows[row_id]['status'],'cancelled')
        self.assertEqual(rows['prior-denied']['status'],'denied')
        self.assertEqual(rows['prior-cancelled']['status'],'cancelled')
        with self.assertRaisesRegex(DomainError,'MIGRATION_REQUIRES_NEW_REQUEST'):
            await self.call('request.resume',{},True)
        with self.assertRaisesRegex(DomainError,'REQUEST_PAUSED'):
            await self.call('exec.start',{'call_id':'never','arguments':{'cmd':'touch replayed'}})
        self.assertFalse((self.A/'replayed').exists())
        await self.call('request.begin',{'request_id':'explicit-new','role':'consult'},True)
        _,r=await self.run_code('print("new request")');self.assertEqual(r.get('exit_code'),0,r)

    async def test_T18_legacy_and_profile_mismatch(self):
        old={**self.kw,'session_id':'legacy'}
        with self.assertRaisesRegex(ValueError,'RESTART_REQUIRED'):
            DomainService(**old,execution_profile='native',session_existing=True)
        service=DomainService(**old,session_existing=True)
        self.assertEqual(service.execution_profile,'protected')
        release_host_profile(service)

    async def test_T19_unknown_attempt_blocks_profile_migration(self):
        p,r=await self.run_code('print("finished")');self.assertEqual(r.get('exit_code'),0,r)
        Path(r['receipt_ref']).unlink();release_host_profile(self.s)
        with self.assertRaisesRegex(ValueError,'MIGRATION_BUSY'):
            DomainService(**self.kw,execution_profile='protected',migrate_profile=True)

    async def test_T21_authority_rejected_before_native_or_protected(self):
        for field in ['approved','source','execution_profile','disable_sandbox','_biomind_probe','access_projection']:
            with self.assertRaisesRegex(DomainError,'AUTHORITY_FIELD'):
                await self.call('exec.start',{'call_id':'spoof','arguments':{'cmd':'true',field:'native'}})
        self.assert_no_permissions()

    async def test_OS_failure_has_no_approval_and_missing_env_no_fallback(self):
        _,r=await self.run_code('print("never")',workdir=str(self.B/'missing'))
        self.assertNotEqual(r.get('exit_code'),0,r);self.assert_no_permissions()
        r=await self.call('exec.start',{'call_id':'environment','arguments':{'cmd':'true','env_name':'not-registered'}})
        self.assertEqual(r['error'],'REGISTERED_ENVIRONMENT_UNAVAILABLE')

    async def test_T27_zero_exit_does_not_validate_missing_required_result(self):
        from agent_core.work_validation import validate_output
        _,r=await self.run_code('print("process succeeded without result")')
        self.assertEqual(r.get('exit_code'),0,r)
        self.assertEqual(r['scientific_validation'],'not_assessed')
        result=self.O/'required.bed'
        with self.assertRaises(FileNotFoundError):validate_output(self.s._state(),{'format':'bed'},result)
        result.write_text('')
        with self.assertRaisesRegex(ValueError,'no BED intervals'):validate_output(self.s._state(),{'format':'bed'},result)
        result.write_text('chr1\t10\t20\n')
        self.assertEqual(validate_output(self.s._state(),{'format':'bed'},result)['interval_count'],1)

    async def test_native_install_confirmation_once_external_prefix_no_directory_grant(self):
        import shlex
        prefix=self.B/'synthetic-empty-prefix'
        p={'call_id':'synthetic-install','arguments':{'cmd':'mkdir '+shlex.quote(str(prefix)),
            'env_name':'synthetic-no-software','prefix':str(prefix),'verification_commands':['test -d '+shlex.quote(str(prefix))]}}
        pending=await self.call('install.propose',p)
        self.assertEqual(pending['status'],'waiting_approval',pending)
        self.assertEqual(pending['approval']['payload']['kind'],'installation')
        self.assertNotIn('access_projection',pending['approval']['payload'])
        a=pending['approval']
        await self.call('approval.decide',{**{k:a[k] for k in ['request_id','action_hash','request_scope']},'choice':'y'},True)
        result=await self.call('install.execute',p)
        self.assertEqual(result['status'],'registered',result)
        self.assertTrue((await self.call('install.execute',p))['replayed'])
        book=self.s._state()['task_state']['execution_permissions'];self.assertEqual(len(book['requests']),1);self.assertFalse(book['grants'])


if __name__ == '__main__':unittest.main()

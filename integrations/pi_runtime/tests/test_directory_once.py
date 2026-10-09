"""Directory decision integration: real Domain/CAS/file I/O and bubblewrap, no model/API."""
import asyncio
import json
import unittest
from pathlib import Path
from test_path_permissions import PathPermissions as Fixture
from agent_core import execution_permissions as ep


class DirectoryOnce(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=Fixture.asyncSetUp
    asyncTearDown=Fixture.asyncTearDown
    call=Fixture.call
    approve=Fixture.approve

    async def once(self,method,params,host=False):
        result=await self.call(method,params,host)
        if result.get('status')=='waiting_approval':
            await self.approve(result,'directory_session')
            result=await self.call(method,params,host)
        self.assertNotEqual(result.get('status'),'waiting_approval',result)
        return result

    async def grant(self,path,access='read'):
        self.index+=1
        return await self.once('permissions.propose',{'call_id':f'dir-{self.index}',
            'paths':[{'path':str(path),'access':access,'target_kind':'directory','recursive':True}],
            'purpose':'synthetic directory once'})

    async def run_code(self,code,**arguments):
        self.index+=1
        result=await self.call('exec.start',{'call_id':f'run-{self.index}',
            'arguments':{'argv':['/usr/bin/python3','-I','-B','-c',code],**arguments}})
        self.assertNotEqual(result.get('status'),'waiting_approval',result)
        for _ in range(200):
            if result.get('terminal') or not result.get('run_ref'):break
            result=await self.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
        return result

    def decisions(self):
        return [e for e in self.events if e['method']=='approval.decide']

    async def test_U01_U03_three_inputs_and_new_descendants(self):
        for name in ['a.vcf','b.fasta','c.gtf']:(self.B/name).write_text(name)
        for name in ['a.vcf','b.fasta','c.gtf']:
            result=await self.once('files.read_authorize',{'path':str(self.B/name),'call_id':name},True)
            self.assertEqual(result['status'],'completed')
            result=await self.call('files.read',{'path':str(self.B/name),'call_id':'read-'+name})
            self.assertIn(name,str(result))
        (self.B/'new').mkdir();(self.B/'new/index').write_text('new-index')
        result=await self.call('files.read',{'path':str(self.B/'new/index'),'call_id':'new-file'})
        self.assertIn('new-index',str(result));self.assertEqual(len(self.decisions()),1)
        status=await self.call('permissions.status',host=True)
        self.assertEqual(len(status['grants']),1);grant=status['grants'][0]
        self.assertEqual((grant['root'],grant['lifetime'],grant['access']),(str(self.B),'session','read'))
        self.assertTrue(grant['ordinary_operations_active'])

    async def test_U02_all_pending_reassessed_atomically(self):
        paths=[self.B/'one',self.B/'two',self.B/'three'];paths[-1].write_text('three')
        pending=await asyncio.gather(*(self.call('files.read_authorize',{'path':str(p),'call_id':p.name},True) for p in paths))
        self.assertTrue(all(r['status']=='waiting_approval' for r in pending))
        await self.approve(pending[0],'directory_session')
        book=self.s._state()['task_state']['execution_permissions']
        self.assertEqual(len(book['grants']),1)
        for p,r in zip(paths,pending):
            self.assertEqual(book['requests'][r['approval']['request_id']]['status'],'allowed')
            fresh=await self.call('permissions.status',{'approval_id':r['approval']['request_id']},True)
            self.assertEqual(fresh['status'],'allowed')
            self.assertEqual((await self.call('files.stat',{'path':str(p),'call_id':p.name}))['status'],'completed')
        before=json.dumps(book['grants'],sort_keys=True)
        await self.approve(pending[0],'directory_session')
        self.assertEqual(before,json.dumps(self.s._state()['task_state']['execution_permissions']['grants'],sort_keys=True))

    async def test_U04_RW_file_and_sandbox_without_auto_command(self):
        await self.grant(self.B,'read_write')
        for name in ['report','new/sub/result']:
            r=await self.call('files.write',{'path':str(self.B/name),'content':'ok','call_id':name})
            self.assertEqual(r['status'],'completed',r)
        r=await self.run_code("from pathlib import Path;assert Path('report').read_text()=='ok';Path('test-result').write_text('PASS')",workdir=str(self.B),write_paths=[str(self.B)])
        self.assertEqual(r.get('exit_code'),0,r);self.assertEqual((self.B/'test-result').read_text(),'PASS')
        self.assertEqual(len(self.decisions()),1)
        rows=self.s._state()['task_state']['execution_permissions']['requests'].values()
        actions=[r for r in rows if r['payload']['kind'] in {'command','file_write'}]
        self.assertTrue(all(r['source']=='policy' for r in actions))
        self.assertEqual(len({r['action_hash'] for r in actions}),len(actions))

    async def test_U05_U06_directory_companions_RO_and_project_output(self):
        await self.grant(self.B)
        (self.B/'indices').mkdir();(self.B/'indices/late').write_text('companion')
        code=f"from pathlib import Path;assert Path('indices/late').read_text()=='companion';assert not Path({str(self.C/'secret')!r}).exists();Path({str(self.A/'result')!r}).write_text(Path('two').read_text())"
        r=await self.run_code(code,workdir=str(self.B));self.assertEqual(r.get('exit_code'),0,r)
        r=await self.run_code(f"from pathlib import Path;assert Path({str(self.B/'indices/late')!r}).read_text()=='companion'",read_paths=[str(self.B/'one')])
        self.assertEqual(r.get('exit_code'),0,r)
        r=await self.run_code("from pathlib import Path;Path('one').write_text('bad')",workdir=str(self.B))
        self.assertEqual(r.get('exit_code'),1,r);self.assertEqual((self.B/'one').read_text(),'original')
        self.assertEqual(len(self.decisions()),1)
        upgrade=await self.call('files.write',{'path':str(self.B/'one'),'content':'bad','call_id':'upgrade'})
        self.assertEqual(upgrade['status'],'waiting_approval');self.assertEqual(upgrade['approval']['payload']['directory_targets'][0]['access'],'read_write')

    async def test_U07_unrelated_old_grant_still_needs_action_review(self):
        await self.grant(self.B)
        p={'call_id':'old-C','paths':[{'path':str(self.C),'access':'read_write','target_kind':'directory','recursive':True}]}
        r=await self.call('permissions.propose',p);await self.approve(r,'session')
        r=await self.call('files.write',{'path':str(self.C/'new'),'content':'no-auto','call_id':'unrelated'})
        self.assertEqual(r['status'],'waiting_approval');self.assertEqual(r['approval']['payload']['kind'],'file_write')
        self.assertFalse((self.C/'new').exists())

    async def test_U07_combined_proposal_only_adds_C_and_pins_existing_B(self):
        await self.grant(self.B)
        p={'call_id':'B-and-C','paths':[{'path':str(path),'access':'read','target_kind':'directory','recursive':True} for path in [self.B,self.C]]}
        r=await self.call('permissions.propose',p)
        self.assertEqual([t['root'] for t in r['approval']['payload']['directory_targets']],[str(self.C)])
        self.assertEqual([t['root'] for t in r['approval']['payload']['covered_directory_grants']],[str(self.B)])
        await self.approve(r,'directory_session')
        self.assertEqual((await self.call('permissions.propose',p))['status'],'completed')
        grants=(await self.call('permissions.status',host=True))['grants']
        self.assertEqual([g['root'] for g in grants],[str(self.B),str(self.C)])

    async def test_U10_displayed_covered_scope_cannot_be_resurrected(self):
        await self.grant(self.B)
        p={'call_id':'B-and-C-revoke','paths':[{'path':str(path),'access':'read','target_kind':'directory','recursive':True} for path in [self.B,self.C]]}
        r=await self.call('permissions.propose',p)
        status=await self.call('permissions.status',host=True)
        await self.call('permissions.revoke',{'grant_id':status['grants'][0]['grant_id'],'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},True)
        decision=await self.approve(r,'directory_session')
        self.assertEqual(decision['status'],'cancelled')
        self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,self.B))
        self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,self.C))

    async def test_U08_U09_lifetime_and_legacy_exact_choice(self):
        p={'path':str(self.B/'one'),'call_id':'legacy'}
        r=await self.call('files.read_authorize',p,True);await self.approve(r,'session')
        r=await self.call('files.read_authorize',{'path':str(self.B/'two'),'call_id':'next'},True)
        self.assertEqual(r['status'],'waiting_approval')
        await self.approve(r,'directory_session')
        for role,request in [('consult','r1'),('generate','r1'),('consult','r2')]:
            await self.call('request.begin',{'request_id':request,'role':role},True)
            r=await self.call('files.read_authorize',{'path':str(self.B/'two'),'call_id':'reused-'+role+request},True)
            self.assertEqual(r['status'],'completed')
        grants=(await self.call('permissions.status',host=True))['grants']
        self.assertEqual(grants[0]['target_kind'],'file');self.assertFalse(grants[0]['ordinary_operations_active'])
        self.assertTrue(grants[1]['ordinary_operations_active'])
        await self.call('request.begin',{'request_id':'r2','role':'generate'},True)
        r=await self.run_code(f"from pathlib import Path;assert Path({str(self.B/'two')!r}).read_text()=='original'",read_paths=[str(self.B/'one')])
        self.assertEqual(r.get('exit_code'),0,r)

    async def test_U10_manual_cancel_revoke_and_root_identity(self):
        await self.grant(self.B,'read_write')
        await self.call('permissions.mode',{'mode':'manual'},True)
        status=await self.call('permissions.status',host=True)
        self.assertTrue(status['grants'][0]['active']);self.assertFalse(status['grants'][0]['ordinary_operations_active'])
        r=await self.call('files.write',{'path':str(self.B/'manual'),'content':'bad','call_id':'manual'})
        self.assertEqual(r['status'],'waiting_approval')
        await self.approve(r,'cancel')
        with self.assertRaisesRegex(ValueError,'REQUEST_PAUSED'):
            await self.call('files.write',{'path':str(self.B/'manual'),'content':'bad','call_id':'different'})
        await self.call('request.resume',{},True)
        self.assertFalse((await self.call('permissions.status',host=True))['grants'][0]['ordinary_operations_active'])
        await self.call('permissions.revoke',{'grant_id':status['grants'][0]['grant_id'],'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},True)
        self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,self.B))
        await self.grant(self.O,'read_write');self.O.rename(self.root/'O-old');self.O.mkdir()
        self.assertFalse(ep.path_grants(self.s.commands,self.s.task_id,self.O))

    async def test_U11_nested_runtime_and_controls(self):
        await self.grant(self.B,'read_write')
        # Use A's actual configured runtime for a grant on the project itself.
        await self.grant(self.A,'read_write')
        with self.assertRaisesRegex(ValueError,'PROTECTED_RUNTIME_PATH'):
            await self.call('files.write',{'path':str(self.R/'original'),'content':'bad','call_id':'runtime'})
        r=await self.run_code(f"from pathlib import Path;Path({str(self.R/'original')!r}).write_text('bad')")
        self.assertEqual(r.get('exit_code'),1,r);self.assertEqual((self.R/'original').read_text(),'original')
        control=self.A/'.biomind';control.mkdir(exist_ok=True);(control/'host').write_text('control')
        with self.assertRaisesRegex(ValueError,'PRIVATE_FILE_EXCLUDED|PROTECTED_CONTROL_PATH'):
            await self.call('files.write',{'path':str(control/'host'),'content':'bad','call_id':'control'})
        r=await self.run_code(f"from pathlib import Path;assert not Path({str(control/'host')!r}).exists();Path({str(control/'host')!r}).write_text('bad')")
        self.assertEqual(r.get('exit_code'),1,r);self.assertEqual((control/'host').read_text(),'control')

    async def test_U10_scoped_tokens_manual_cancel_and_unrelated_revoke(self):
        await self.grant(self.B,'read_write');await self.grant(self.C)
        def pending(name):
            payload=ep.file_payload(self.s.files,'write_file',{'path':str(self.B/name),'content':'not executed','expected_sha256':''},self.s.task_id)
            row=ep.request_permission(self.s.data_root,self.s.task_id,name,payload)
            self.assertEqual(row['source'],'policy');return row
        row=pending('prepared-token')
        status=await self.call('permissions.status',host=True);unrelated=next(g for g in status['grants'] if g['root']==str(self.C))
        await self.call('permissions.revoke',{'grant_id':unrelated['grant_id'],'request_scope':status['request_scope'],'policy_revision':status['policy_revision']},True)
        self.assertEqual(ep.permission_state(self.s.data_root,self.s.task_id,row['request_id'])['status'],'allowed')
        await self.call('permissions.mode',{'mode':'manual'},True)
        with self.assertRaises(ep.PermissionDenied):ep.claim_permission(self.s.data_root,self.s.task_id,row,'stale-manual')
        await self.grant(self.B,'read_write')
        row=pending('before-cancel')
        new=await self.call('permissions.propose',{'call_id':'cancel-C','paths':[{'path':str(self.C),'access':'read','target_kind':'directory','recursive':True}]})
        await self.approve(new,'cancel')
        self.assertFalse(ep.scoped_automatic_grants(self.s._state()))
        with self.assertRaises(ep.PermissionDenied):ep.claim_permission(self.s.data_root,self.s.task_id,row,'stale-cancel')
        self.assertFalse((self.B/'prepared-token').exists());self.assertFalse((self.B/'before-cancel').exists())


del Fixture
if __name__=='__main__':unittest.main()

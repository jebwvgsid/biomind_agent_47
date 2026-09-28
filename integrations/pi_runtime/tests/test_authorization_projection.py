"""Actual permission authority; observational refs never broaden file/host access."""
import json
import unittest

import test_python_domain as fixture
from agent_core.pending_input import mutate


class AuthorizationProjectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.DomainTests.asyncSetUp
    asyncTearDown = fixture.DomainTests.asyncTearDown
    service = fixture.DomainTests.service
    begin = fixture.DomainTests.begin
    approve = fixture.DomainTests.approve

    async def proposal(self, name):
        args={'call_id':name,'path':str(self.root/(name+'.txt')),'content':'synthetic public output'}
        return args,await self.s.call('files.write',args)

    async def test_actual_human_reference_is_read_only_and_internal_state_stays_protected(self):
        args,pending=await self.proposal('allowed')
        self.assertEqual(self.s.context_snapshot()['authorization_refs'],[])
        await self.approve(pending)
        await self.s.call('files.write',args)
        state_path=self.s.data_root/'task_outputs'/self.s.task_id/'plan_state.json'
        before=state_path.read_bytes()
        actual=self.s.context_snapshot()['authorization_refs']
        row=self.s._state()['task_state']['execution_permissions']['requests'][pending['approval']['request_id']]
        self.assertTrue(row.get('claim'))
        self.assertEqual(actual,[{key:row[key] for key in ('request_id','action_hash','request_scope','status','source')}])
        self.assertEqual(state_path.read_bytes(),before)
        for forbidden in ('payload','permit','host_token','claim','environment','token'):
            self.assertNotIn('"'+forbidden+'"',json.dumps(actual))
        with self.assertRaisesRegex(ValueError,'PROTECTED_RUNTIME_PATH'):
            await self.s.call('files.read',{'path':str(state_path)})
        with self.assertRaisesRegex(ValueError,'HOST_CHANNEL_REQUIRED'):
            await self.s.call('approval.decide',{**actual[0],'choice':'y'})

    async def test_request_expiration_cancellation_and_policy_change_hide_refs(self):
        _,pending=await self.proposal('scope-old');await self.approve(pending)
        self.assertEqual(len(self.s.context_snapshot()['authorization_refs']),1)
        await self.begin(self.s,'second','generate')
        self.assertEqual(self.s.context_snapshot()['authorization_refs'],[])
        _,pending=await self.proposal('scope-current');await self.approve(pending)
        fixture.permissions.cancel_permission(self.s.data_root,self.s.task_id,pending['approval']['request_id'])
        self.assertEqual(self.s.context_snapshot()['authorization_refs'],[])
        _,pending=await self.proposal('policy');await self.approve(pending)
        mutate(self.s.data_root,self.s.task_id,lambda state:state['task_state']['execution_permissions'].update(policy_revision='expired-policy'))
        self.assertEqual(self.s.context_snapshot()['authorization_refs'],[])

    async def test_scope_derived_rule_and_denial_are_not_human_refs_and_request_cancel_expires(self):
        args,pending=await self.proposal('grant');await self.approve(pending,'scope')
        await self.s.call('files.write',args)
        rule_args={'call_id':'rule','path':str(self.root/'rule.txt'),'content':'rule-authorized'}
        self.assertEqual((await self.s.call('files.write',rule_args))['status'],'completed')
        rows=self.s._state()['task_state']['execution_permissions']['requests'].values()
        self.assertTrue(any(row.get('source')=='rule' and row['status']=='allowed' for row in rows))
        self.assertEqual(len(self.s.context_snapshot()['authorization_refs']),1)
        # Another request has no inherited directory grant.
        await self.begin(self.s,'third','generate')
        _,denied=await self.proposal('denied');await self.approve(denied,'n')
        self.assertEqual(self.s.context_snapshot()['authorization_refs'],[])
        _,allowed=await self.proposal('cancel');await self.approve(allowed)
        self.assertEqual(len(self.s.context_snapshot()['authorization_refs']),1)
        await self.s.call('request.cancel',{},host_token=self.s.host_token)
        self.assertEqual(self.s.context_snapshot()['authorization_refs'],[])


if __name__=='__main__':unittest.main()

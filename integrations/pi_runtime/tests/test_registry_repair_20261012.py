"""Actual domain, registry, probes and process receipts; synthetic executables."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_environment_binding_20261002 import EnvironmentBinding

class RegistryRepair(EnvironmentBinding):
    async def test_verified_registration_idempotent_multiple_environments_and_fresh_query(self):
        for name,version in [('one','1.0'),('two','2.0')]:
            prefix=self.tool(name,tool='repairtool')
            entry=prefix/'bin/repairtool'
            entry.write_text('#!/bin/sh\nprintf "REPAIRTOOL '+version+'\\n"\nexit 7\n')
            located=await self.s.call('environments.locate',{'requirements':{'prefix':str(prefix),'tools':['repairtool']}})
            self.assertEqual(located['status'],'location_hints',located)
            run=await self.terminal(await self.s.call('exec.probe',{'call_id':'probe-'+name,'arguments':{'argv':[str(entry),'--help'],'workdir':str(self.root),'timeout_ms':1000}},host_token=self.s.host_token))
            self.assertEqual(run['exit_code'],7,run)
            args={'call_id':'register-'+name,'env_name':name,'prefix':str(prefix),'tools':['repairtool'],'versions':{'repairtool':version},
                  'verification_runs':[{'tool':'repairtool','run_ref':run['run_ref'],'expected_output':'REPAIRTOOL '+version,'exit_codes':[7]}]}
            registered=await self.s.call('environments.register',args)
            self.assertEqual(registered['status'],'registered',registered)
            self.assertEqual(registered['record']['evidence']['strength'],'historically_observed')
            before=self.registry.read_bytes()
            again=await self.s.call('environments.register',args)
            self.assertEqual(again['operation'],'unchanged',again)
            self.assertEqual(before,self.registry.read_bytes())
            bad={**args,'verification_runs':[{**args['verification_runs'][0],'expected_output':'not-present'}]}
            with self.assertRaisesRegex(ValueError,'NOT_VERIFIED'):await self.s.call('environments.register',bad)
            self.assertEqual(before,self.registry.read_bytes())
        registry=json.loads(self.registry.read_text())
        self.assertEqual(registry['tools']['repairtool']['env'],'one')
        self.assertEqual(set(registry['environments']),{'one','two'})
        self.s=self.service('new-session');await self.begin('new-request')
        for name,version in [('one','1.0'),('two','2.0')]:
            selected=await self.select(env_name=name,requirements={'tools':['repairtool'],'versions':{'repairtool':version}})
            self.assertEqual(selected['availability'],'AVAILABLE',selected)
            self.assertEqual(selected['selected_candidate']['env'],name)
        ambiguous=await self.select(requirements={'tools':['repairtool']})
        self.assertEqual(ambiguous['availability'],'UNKNOWN',ambiguous)

    async def test_location_budget_survives_distinct_queries_and_replays(self):
        for i in range(4):
            result=await self.s.call('environments.locate',{'requirements':{'prefix':str(self.root/str(i)),'tools':['repairtool']}})
            self.assertIn(result['status'],['location_hints','not_found'])
        repeated=await self.s.call('environments.locate',{'requirements':{'prefix':str(self.root/'0'),'tools':['repairtool']}})
        self.assertTrue(repeated['replayed'])
        with self.assertRaisesRegex(ValueError,'TOTAL_BUDGET_EXHAUSTED'):
            await self.s.call('environments.locate',{'requirements':{'prefix':str(self.root/'5'),'tools':['repairtool']}})

    async def test_forged_or_failed_probe_cannot_register_verified_software(self):
        prefix=self.tool('bad',tool='repairtool')
        entry=prefix/'bin/repairtool'
        run=await self.terminal(await self.s.call('exec.probe',{'call_id':'echo','arguments':{'argv':['/bin/echo','REPAIRTOOL'],'timeout_ms':1000}},host_token=self.s.host_token))
        args={'call_id':'invalid','env_name':'bad','prefix':str(prefix),'tools':['repairtool'],
              'verification_runs':[{'tool':'repairtool','run_ref':run['run_ref'],'expected_output':'REPAIRTOOL'}]}
        with self.assertRaisesRegex(ValueError,'EXACT_ENTRY'):await self.s.call('environments.register',args)
        self.assertFalse(json.loads(self.registry.read_text())['environments'])
        self.registry.write_text('{broken')
        unavailable=await self.select(env_name='bad')
        self.assertEqual(unavailable['availability'],'UNKNOWN');self.assertIn('CORRUPT',unavailable['reason_code'])

for name in vars(EnvironmentBinding):
    if name.startswith('test_'):setattr(RegistryRepair,name,None)
if __name__=='__main__':unittest.main(verbosity=2)

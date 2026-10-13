"""Deterministic real checker/observer/ledger tests; no model or GWAS claims."""
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_environment_binding_20261002 import EnvironmentBinding
from domain_adapter.scientific_inputs import current, close_block


class ScientificStop(EnvironmentBinding):
    def fixture(self):
        (self.root/'mini.vcf').write_text('##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tA\tB\n')
        (self.root/'trait.tsv').write_text('A\t1\nB\t2\n')
        (self.root/'mini.Q').write_text('0.2 0.8\n0.7 0.3\n')
        (self.root/'mini.tfam').write_text('A A 0 0 0 -9\nB B 0 0 0 -9\n')
        return 'GWAS EMMAX mini.vcf trait.tsv mini.Q'

    async def check(self, text):
        return await self.s.call('request.preflight', {'user_text':text}, host_token=self.s.host_token)

    async def test_block_cache_resume_restart_new_request(self):
        prompt = self.fixture(); row = await self.check(prompt)
        self.assertEqual(row['reason_code'], 'Q_SAMPLE_MAPPING_UNKNOWN', row)
        self.assertEqual(row['delivery_status'], 'complete', row)
        self.assertLess(row['closed_at']-row['accepted_at'], 30)
        self.assertTrue(all((self.root/'output'/name).is_file() for name in ['report.md','stage_status.tsv','reproduce.sh']))
        for i in range(2):
            refused = await self.s.call('exec.start', {'call_id':f'bypass-{i}', 'arguments':{'cmd':'touch SENTINEL','timeout_ms':3600000}})
            self.assertEqual(refused['status'], 'blocked'); self.assertFalse(refused['execution_started'])
        self.assertFalse((self.root/'SENTINEL').exists())
        await self.s.call('request.resume', {}, host_token=self.s.host_token)
        self.assertEqual((await self.check(prompt))['run_ref'], row['run_ref'])
        await self.s.disconnected(); self.s = self.service('one')
        self.assertEqual((await self.s.call('request.status',{},host_token=self.s.host_token))['scientific_input']['status'], 'blocked')
        await self.begin('new-independent')
        self.assertEqual((await self.s.call('request.dispatch',{'tool':'bash'},host_token=self.s.host_token))['status'], 'allowed')

    async def test_valid_mapping_releases_same_request_only_after_recheck(self):
        prompt=self.fixture(); blocked=await self.check(prompt)
        mapping={'schema_version':1,'q_sha256':hashlib.sha256((self.root/'mini.Q').read_bytes()).hexdigest(),
                 'tfam_sha256':hashlib.sha256((self.root/'mini.tfam').read_bytes()).hexdigest(),
                 'basis':'Fixture generator explicitly writes B then A',
                 'rows':[{'q_row':1,'sample_id':'B'},{'q_row':2,'sample_id':'A'}]}
        (self.root/'map.json').write_text(json.dumps(mapping))
        good=await self.check(prompt+' mini.tfam map.json')
        self.assertEqual(good['status'],'validated',good); self.assertNotEqual(good['run_ref'],blocked['run_ref'])
        run=await self.terminal(await self.s.call('exec.start',{'call_id':'valid-long-budget','arguments':{'cmd':'sleep 0.1; echo VALID','timeout_ms':120000}}))
        self.assertEqual(run['exit_code'],0,run)
        (self.root/'mini.Q').write_text('0.3 0.7\n0.4 0.6\n')
        refused=await self.s.call('exec.start',{'call_id':'changed-input','arguments':{'cmd':'touch SENTINEL'}})
        self.assertEqual(refused['status'],'blocked',refused);self.assertFalse((self.root/'SENTINEL').exists())

    async def test_untrusted_stdout_is_not_a_control_receipt(self):
        self.assertEqual((await self.check('只讨论 '+self.fixture()))['status'],'not_applicable')
        self.assertFalse((self.root/'output').exists())
        run=await self.terminal(await self.s.call('exec.start',{'call_id':'ordinary-text','arguments':{'cmd':"echo Q_SAMPLE_MAPPING_UNKNOWN"}}))
        self.assertEqual(run['exit_code'],0,run)
        self.assertEqual(current(self.s),{})
        self.assertEqual((await self.s.call('request.dispatch',{'tool':'bash'},host_token=self.s.host_token))['status'],'allowed')
        with self.assertRaisesRegex(ValueError,'HOST_CHANNEL_REQUIRED'):
            await self.s.call('request.preflight',{'user_text':self.fixture()})

    async def test_cleanup_targets_current_owned_process_only(self):
        other=self.service('other');await other.call('request.begin',{'request_id':'other'},host_token=other.host_token)
        independent=await other.formal.start({'cmd':'sleep 10','timeout_ms':15000},task_id=other.task_id,request_scope=other.scope(),stage='fixture',call_id='independent',yield_time_ms=30)
        owned={}
        async def crossing_admission(service,row):
            nonlocal owned
            owned=await service.formal.start({'cmd':'sleep 10 & wait','timeout_ms':15000},task_id=service.task_id,request_scope=service.scope(),stage='fixture',call_id='owned',yield_time_ms=30)
            self.assertFalse(owned.get('terminal'),owned)
            return await close_block(service,row)
        with patch('domain_adapter.scientific_inputs.close_block',side_effect=crossing_admission):
            result=await self.check(self.fixture())
        self.assertEqual(result['cleanup']['status'],'confirmed',result)
        self.assertEqual(result['cleanup']['runs'][0]['run_ref'],owned['run_ref'])
        self.assertTrue(other.formal.active_runs(other.task_id,other.scope()),independent)

for name in vars(EnvironmentBinding):
    if name.startswith('test_'):setattr(ScientificStop,name,None)
del EnvironmentBinding
if __name__=='__main__':
    import unittest
    unittest.main(verbosity=2)

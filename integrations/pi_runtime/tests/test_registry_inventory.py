"""Existing successful registration plus passive path inventory; no process runs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

RUNTIME = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RUNTIME), str(RUNTIME.parents[1])]
from domain_adapter.registry import RegistryView, registry_path, registered_scientific_tool
from agent_core.registered_runtime import registered_runtime_reuse


class RegistryInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='biomind-registry-inventory-')
        self.root = Path(self.temp.name)
        self.prefix = self.root/'envs'/'existing'; (self.prefix/'bin').mkdir(parents=True)
        binary = self.prefix/'bin'/'bcftools'; binary.write_text('fixture, never executed\n'); binary.chmod(0o700)
        self.inventory = self.root/'environments.txt'; self.inventory.write_text(str(self.prefix)+'\n')
        self.registry = self.root/'registry.json'
        self.record = {'status':'success', 'packages':['bcftools'], 'last_verified_at':'historical'}
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.registry.write_text(json.dumps({'environments':{'existing':self.record},'tools':{}}))

    def selected(self):
        return registered_runtime_reuse(RegistryView(self.registry,self.inventory),
            {'tools':['bcftools']},skill_record={},target_env='existing')

    def test_existing_success_inventory_matches_product_selection_without_reverification(self):
        before = self.registry.read_bytes()
        result = self.selected(); tool = registered_scientific_tool(self.registry,self.inventory)
        self.assertEqual(result['availability'],'AVAILABLE')
        self.assertEqual(tool['prefix'],result['selected_candidate']['prefix'])
        self.assertEqual(tool['source_ref'],result['selected_candidate']['source_ref'])
        self.assertEqual(tool['healthcheck_status'],'not_run')
        self.assertEqual(tool['last_verified_at'],'historical')
        self.assertEqual(self.registry.read_bytes(),before)

    def test_inventory_cannot_promote_registered_or_broken(self):
        for status in ('registered','broken','disabled'):
            self.record['status']=status;self.write()
            self.assertIsNone(registered_scientific_tool(self.registry,self.inventory))
            self.assertEqual(self.selected()['availability'],'UNKNOWN')

    def test_duplicate_existing_names_are_ambiguous(self):
        second=self.root/'other'/'existing';second.mkdir(parents=True)
        self.inventory.write_text(str(self.prefix)+'\n'+str(second)+'\n')
        self.assertEqual(self.selected()['reason_code'],'REGISTERED_RUNTIME_LOCATION_AMBIGUOUS')
        self.assertIsNone(registered_scientific_tool(self.registry,self.inventory))

    def test_missing_inventory_does_not_guess_an_environment(self):
        self.inventory.unlink()
        self.assertEqual(self.selected()['reason_code'],'REGISTERED_RUNTIME_LOCATION_UNRESOLVED')
        self.assertIsNone(registered_scientific_tool(self.registry,self.inventory))

    def test_explicit_prefix_wins_and_invalid_prefix_never_falls_back(self):
        self.record['prefix']=str(self.prefix);self.write();self.inventory.unlink()
        self.assertEqual(self.selected()['availability'],'AVAILABLE')
        self.inventory.write_text(str(self.prefix)+'\n')
        for bad in ('relative/path',str(self.root/'nonexistent')):
            self.record['prefix']=bad;self.write()
            self.assertEqual(self.selected()['availability'],'UNKNOWN')

    def test_resolved_path_metadata_remains_supported(self):
        self.record['resolved_runtime_paths']={'RESOLVED_ENV_PREFIX':str(self.prefix)};self.write()
        self.inventory.unlink()
        self.assertEqual(self.selected()['availability'],'AVAILABLE')

    def test_declared_tool_and_executable_both_required(self):
        self.record['packages']=['different'];self.write()
        self.assertIsNone(registered_scientific_tool(self.registry,self.inventory))
        self.record['packages']=['bcftools'];self.write();(self.prefix/'bin'/'bcftools').unlink()
        self.assertIsNone(registered_scientific_tool(self.registry,self.inventory))

    def test_registry_source_precedence_matches_product(self):
        env={'BIO_SOFTWARE_ROOT':'/software','BIOMIND_PI_REGISTRY_PATH':'/configured/registry.json'}
        self.assertEqual(registry_path('/install','/explicit/registry.json',environment=env),Path('/explicit/registry.json'))
        self.assertEqual(registry_path('/install',environment=env),Path('/configured/registry.json'))
        self.assertEqual(registry_path('/install',environment={'BIO_SOFTWARE_ROOT':'/software'}),Path('/software/software_registry.json'))
        self.assertEqual(registry_path('/install',environment={}),Path('/install/software_registry.json'))

    def test_product_records_passive_evidence_and_retains_concrete_invalidation(self):
        from domain_adapter.service import DomainService
        from agent_core.pending_input import mutate
        service=DomainService(cwd=self.root,data_root=self.root/'data',install_root=RUNTIME.parents[1],
            session_id='passive-inventory',registry_path=self.registry,environment_inventory=self.inventory)
        before=self.registry.read_bytes()
        selected=service.select_environment({'tools':['bcftools']},env_name='existing')
        self.assertEqual(selected['availability'],'AVAILABLE')
        evidence=service._state()['task_state']['runtime_evidence']['records']
        self.assertEqual(evidence[0]['producer'],'registered_runtime')
        self.assertEqual(evidence[0]['inspection']['status'],'not_run')
        def invalidate(state):
            state['task_state']['runtime_invalidations']={'registered-environment:existing':
                {'prefix':str(self.prefix),'reason_code':'REGISTERED_RUNTIME_DEPENDENCY_FAILED'}}
        mutate(service.data_root,service.task_id,invalidate)
        invalid=service.select_environment({'tools':['bcftools']},env_name='existing')
        self.assertEqual(invalid['availability'],'UNKNOWN')
        self.assertEqual(invalid['decision'],'execution_dependency_failed')
        self.assertEqual(self.registry.read_bytes(),before)
        self.assertFalse(service.active_jobs())


if __name__=='__main__':unittest.main(verbosity=2)

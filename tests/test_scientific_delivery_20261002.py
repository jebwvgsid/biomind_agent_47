"""C delivery gates: existing validators and real harmless Domain subprocesses.

The explicit stop-condition wrappers below are scripted user-intent fixtures,
not an evaluation of a natural model deciding when to stop. No R, fgsea, Hi-C
matrix loader or scientific executable runs.
"""
import importlib.abc
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class NoProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'config' or fullname.startswith('config.') or fullname == 'dotenv' or fullname.startswith('dotenv.'):
            raise ImportError('offline fixture forbids configuration import: ' + fullname)
        return None


sys.meta_path.insert(0, NoProviders())
from agent_core.work_validation import validate_output, result_table_facts
from integrations.pi_runtime.domain_adapter.service import DomainService


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-delivery-c-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root/'leaf.mcool'
        self.input.write_text('synthetic metadata only; never load as matrix')
        self.state = {'plan_document': {'inputs': [{'sample': 'leaf', 'path': str(self.input)}],
                                      'parameters': {'resolution_bp': 40000}}}

    def test_J04_missing_required_output_is_not_validated(self):
        with self.assertRaises(FileNotFoundError):
            validate_output(self.state, {'artifact_id': 'required_result', 'required': True,
                                        'format': 'tsv'}, self.root/'not-created.tsv')

    def test_J04_wrong_sample_and_wrong_exact_input_path_are_rejected(self):
        output = self.root/'samples.tsv'
        for sample, source in [('other-sample', str(self.input)), ('leaf', str(self.root/'wrong.mcool'))]:
            output.write_text('sample\tmatrix\n' + sample + '\t' + source + '\n')
            with self.subTest(sample=sample, source=source), self.assertRaises(ValueError):
                validate_output(self.state, {'role': 'matrix_sample_sheet'}, output)

    def test_N16_reused_or_partial_or_failed_rows_cannot_be_success(self):
        for sample, status, code in [('leaf', 'reused', '0'), ('leaf', 'read_existing', '0'),
                                     ('other', 'success', '0'), ('leaf', 'failed', '7')]:
            with self.subTest(sample=sample, status=status), self.assertRaises(ValueError):
                result_table_facts(self.state, [('saved-old-run', [{'sample': sample, 'status': status,
                    'return_code': code, 'resolution_bp': '40000'}])])
        with self.assertRaises(ValueError):
            result_table_facts(self.state, [('empty-wrapper-success', [])])

    def test_J04_legal_empty_table_respects_declared_contract(self):
        output = self.root/'no-enrichment.tsv'
        output.write_text('pathway\tpadj\n')
        with self.assertRaisesRegex(ValueError, 'empty'):
            validate_output(self.state, {'format': 'tsv'}, output)
        facts = validate_output(self.state, {'format': 'tsv', 'validation': {'allow_empty': True}}, output)
        self.assertEqual(facts, {'kind': 'table', 'rows': 0})

    def test_E12_preflight_rejects_failure_and_wrong_resolution(self):
        output = self.root/'preflight.tsv'
        for status, balanced, resolution in [('failed', 'no', '40000'), ('success', 'yes', '10000')]:
            output.write_text('sample\tmatrix\tresolution_bp\tbalanced\tstatus\tmessage\n'
                + f'leaf\t{self.input}::/resolutions/{resolution}\t{resolution}\t{balanced}\t{status}\tfixture\n')
            with self.subTest(status=status, resolution=resolution), self.assertRaises(ValueError):
                validate_output(self.state, {'role': 'tad_preflight'}, output)


class RealExecutionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-scientific-stop-c-')
        self.root = Path(self.tmp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        self.registry = self.root/'registry.json'
        self.registry.write_text('{"environments":{},"tools":{}}')
        inventory = self.root/'inventory.txt'
        inventory.write_text('')
        self.service = DomainService(cwd=self.project, data_root=self.root/'state', install_root=ROOT,
            session_id='scientific-stop', registry_path=self.registry, environment_inventory=inventory,
            preferences_path=self.root/'preferences.md', skills_root=self.root/'skills', execution_profile='native')
        await self.service.call('request.begin', {'request_id': 'synthetic-user-stop', 'role': 'main'},
                                host_token=self.service.host_token)

    async def asyncTearDown(self):
        await self.service.formal.shutdown()
        await self.service.disconnected()
        self.tmp.cleanup()

    async def execute(self, call_id, arguments):
        value = await self.service.call('exec.start', {'call_id': call_id, 'arguments': arguments})
        for _ in range(100):
            if value.get('terminal'):
                return value
            self.assertTrue(value.get('run_ref'), value)
            value = await self.service.call('exec.wait', {'run_ref': value['run_ref'], 'max_wait_ms': 100})
        self.fail('synthetic command never reached terminal: ' + str(value))

    async def stop_case(self, name, check_tool, exit_code):
        prefix = self.root/name
        (prefix/'bin').mkdir(parents=True)
        check_count = self.project/(name+'-checks')
        science_count = self.project/(name+'-science')
        check = prefix/'bin'/check_tool
        check.write_text('#!/bin/sh\nprintf x >> ' + shlex.quote(str(check_count)) + '\n'
            + 'printf "synthetic explicit condition failed\\n" >&2\nexit ' + str(exit_code) + '\n')
        check.chmod(0o755)
        science = prefix/'bin'/'synthetic-science'
        science.write_text('#!/bin/sh\nprintf x >> ' + shlex.quote(str(science_count)) + '\n')
        science.chmod(0o755)
        registered = await self.service.call('environments.register', {'call_id': 'register-'+name,
            'env_name': name, 'prefix': str(prefix), 'tools': [check_tool, 'synthetic-science'],
            'source_note': 'synthetic user declaration; no healthcheck'})
        self.assertEqual(registered['status'], 'registered', registered)
        selected = await self.service.call('environments.select', {'env_name': name})
        self.assertEqual(selected['availability'], 'AVAILABLE', selected)
        self.assertEqual(selected['selected_candidate']['audit']['healthcheck_status'], 'not_run')
        # User explicitly asked to prove the condition before analysis. This
        # wrapper preserves that stop condition and the upstream exit status.
        wrapper = (shlex.quote(str(check)) + '; condition_rc=$?; '
                   'if [ "$condition_rc" -ne 0 ]; then exit "$condition_rc"; fi; '
                   + shlex.quote(str(science)))
        result = await self.execute('check-'+name, {'argv': ['/bin/bash', '-c', wrapper],
            'binding_ref': selected['binding_ref'], 'workdir': str(self.project)})
        self.assertEqual(result['exit_code'], exit_code, result)
        self.assertEqual(check_count.read_text(), 'x')
        self.assertFalse(science_count.exists(), 'failed explicit condition must have zero scientific starts')
        self.assertEqual(result.get('scientific_validation'), 'not_assessed')
        return result

    async def test_E12_user_R_fgsea_condition_failure_has_zero_analysis_starts(self):
        await self.stop_case('rna-fixture', 'Rscript', 7)

    async def test_E12_user_TAD_preflight_failure_has_zero_analysis_starts(self):
        await self.stop_case('tad-fixture', 'tad-preflight', 2)

    async def test_J02_native_pipeline_semantics_and_explicit_upstream_exit(self):
        native = await self.execute('native-pipeline', {'cmd': 'false | tail -n 1', 'workdir': str(self.project)})
        self.assertEqual(native['exit_code'], 0, 'native shell pipeline semantics stay unchanged')
        self.assertEqual(native.get('scientific_validation'), 'not_assessed')
        checked = await self.execute('preserved-upstream', {'argv': ['/bin/bash', '-c',
            'false > upstream.log 2>&1; upstream_rc=$?; cat upstream.log; exit "$upstream_rc"'],
            'workdir': str(self.project)})
        self.assertEqual(checked['exit_code'], 1)

    async def test_N16_rc_zero_without_required_file_cannot_pass_output_validation(self):
        result = await self.execute('empty-success-wrapper', {'argv': ['/bin/true'], 'workdir': str(self.project)})
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result.get('scientific_validation'), 'not_assessed')
        with self.assertRaises(FileNotFoundError):
            validate_output({}, {'format': 'tsv', 'required': True}, self.project/'required.tsv')


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Pure-standard-library software fixtures; no environment/config/GWAS imports.

Run: python -B tests/test_gwas_input_contracts_s3.py
All input/output is synthetic and confined to TemporaryDirectory; real helper main runs locally.
"""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/population-genetics/gwas-vcf-prep-qc/scripts'
# Local pure module imports are also usable under the offline managed bootstrap.
sys.path.insert(0, str(SCRIPTS))
from input_contract import InputError, table, vcf_header
from inspect_inputs import inspect_inputs
import sort_phenotype


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InputContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gwas-s3-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tfam = self.write('target.tfam', 'F1 S1 0 0 0 -9\nF2 S2 0 0 0 -9\n')
        self.phenotype = self.write('phenotype.tsv', 'S1\t11\nS2\t22\n')
        self.q = self.write('q.tsv', '0.1 0.2 0.7\n0.4 0.5 0.1\n')
        self.vcf = self.write('input.vcf', '##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n1\t1\t.\tA\tC\t.\tPASS\t.\tGT\t0/0\t0/1\n')
        self.out = self.root / 'output.tsv'

    def write(self, name, value):
        path = self.root / name
        path.write_text(value)
        return path

    def invoke(self, argv):
        # Execute main directly, so bootstrap's subprocess denylist need not be weakened.
        # Main is the same entrypoint used by the script, with synthetic sys.argv only.
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        stdout, stderr = StringIO(), StringIO()
        with patch.object(sys, 'argv', ['sort_phenotype.py', *map(str, argv)]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = sort_phenotype.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def sort(self, *extra, phenotype=None):
        return self.invoke(['--tfam', self.tfam, '--phenotype', phenotype or self.phenotype, '--trait', '2', '--out-emmax', self.out, *extra])

    def mapping(self, *, ids=('S2', 'S1')):
        value = {'schema_version': 1, 'q_sha256': digest(self.q), 'tfam_sha256': digest(self.tfam),
            'basis': 'Synthetic fixture generated with explicit Q row identities S2 then S1',
            'rows': [{'q_row': index, 'sample_id': sid} for index, sid in enumerate(ids, 1)]}
        return self.write('map.json', json.dumps(value))

    def covar(self, mapping=None, *extra):
        return self.invoke(['--tfam', self.tfam, '--phenotype', self.q, '--trait', '1,2,3',
            '--input-kind', 'q_covariates', '--out-emmax-covar', self.out,
            *(['--q-row-map', mapping] if mapping else []), *extra])

    def assertBlocked(self, result, code):
        self.assertEqual(result[0], 2, result)
        self.assertEqual(json.loads(result[2])['error']['code'], code)

    def test_no_header_215_preserves_first_sample(self):
        self.tfam.write_text(''.join(f'F{i} S{i:03} 0 0 0 -9\n' for i in range(215)))
        self.phenotype.write_text(''.join(f'S{i:03}\t{i + 1}\n' for i in range(215)))
        result = self.sort('--header', 'absent', '--sample-column', '1')
        self.assertEqual(result[0], 0, result)
        rows = self.out.read_text().splitlines()
        self.assertEqual(len(rows), 215)
        self.assertEqual(rows[0], 'F0\tS000\t1')
        self.assertEqual(rows[-1], 'F214\tS214\t215')
        self.assertEqual(table(self.phenotype)['row_count'], 215)

    def test_first_sample_named_id_is_data_beside_numeric_trait(self):
        self.tfam.write_text('F1 id\nF2 S2\n')
        self.phenotype.write_text('id\t11\nS2\t22\n')
        self.assertEqual(self.sort()[0], 0)
        self.assertEqual(self.out.read_text(), 'F1\tid\t11\nF2\tS2\t22\n')
        self.assertFalse(table(self.phenotype)["header_present"])
        self.phenotype.write_text('IID\t2\nS1\t11\nS2\t22\n')
        self.assertFalse(table(self.phenotype)["header_present"])
        self.assertEqual(table(self.phenotype, header_mode="present")["row_count"], 2)

    def test_sample_column_does_not_authorize_numeric_q_identity(self):
        self.tfam.write_text('F1 101\nF2 102\n')
        self.q.write_text('101 0.1 0.9\n102 0.4 0.6\n')
        self.assertBlocked(self.covar(None, '--sample-column', '1', '--trait', '2,3'), 'Q_SAMPLE_MAPPING_UNKNOWN')
        mapping = self.mapping(ids=('101', '102'))
        self.assertEqual(self.covar(mapping, '--sample-column', '1', '--trait', '2,3')[0], 0)
        self.assertEqual(self.out.read_text(), 'F1\t101\t1\t0.1\t0.9\nF2\t102\t1\t0.4\t0.6\n')

    def test_explicit_header_and_named_column(self):
        self.phenotype.write_text('accession\ttrait\nS2\t22\nS1\t11\n')
        result = self.sort('--header', 'present', '--sample-column', 'accession', '--trait', 'trait')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])['alignment']['alignment'], 'reorder_required')
        self.assertEqual(self.out.read_text(), 'F1\tS1\t11\nF2\tS2\t22\n')

    def test_duplicate_id_preserves_existing_output(self):
        self.phenotype.write_text('S1\t11\nS1\t22\n')
        self.out.write_text('existing output\n')
        result = self.sort()
        self.assertBlocked(result, 'SAMPLE_ID_DUPLICATE')
        self.assertEqual(json.loads(result[2])['error']['duplicate_ids'], ['S1'])
        self.assertEqual(self.out.read_text(), 'existing output\n')

    def test_missing_and_extra_ids_reported_without_fid_fallback(self):
        self.phenotype.write_text('F1\t11\nS2\t22\n')
        result = self.sort()
        self.assertBlocked(result, 'SAMPLE_SET_MISMATCH')
        error = json.loads(result[2])['error']
        self.assertEqual(error['missing_ids'], ['S1'])
        self.assertEqual(error['extra_ids'], ['F1'])
        self.assertFalse(self.out.exists())

    def test_missing_allowed_only_explicitly_and_not_all_missing(self):
        self.phenotype.write_text('S2\t22\n')
        self.assertBlocked(self.sort(), 'SAMPLE_SET_MISMATCH')
        self.assertEqual(self.sort('--allow-missing', 'true')[0], 0)
        self.assertEqual(self.out.read_text(), 'F1\tS1\tNA\nF2\tS2\t22\n')
        self.phenotype.write_text('S1\tNA\nS2\tNA\n')
        self.assertBlocked(self.sort('--allow-missing', 'true'), 'TRAIT_ALL_MISSING')

    def test_invalid_columns_are_never_guessed(self):
        for trait in ['0', '-1', '3', 'TRAIT']:
            with self.subTest(trait=trait):
                self.assertBlocked(self.sort('--trait', trait), 'TRAIT_COLUMN_INVALID')
        self.assertBlocked(self.sort('--trait', '1'), 'TRAIT_IS_SAMPLE_COLUMN')
        self.assertBlocked(self.sort('--sample-column', '3'), 'SAMPLE_COLUMN_INVALID')

    def test_wholly_numeric_sample_ids_need_explicit_column(self):
        self.phenotype.write_text('101 11\n102 22\n')
        self.tfam.write_text('F1 101\nF2 102\n')
        self.assertBlocked(self.sort(), 'SAMPLE_ID_COLUMN_UNRESOLVED')
        self.assertEqual(self.sort('--sample-column', '1')[0], 0)

    def test_empty_duplicate_missing_and_ragged_ids_fail(self):
        cases = [('', 'INPUT_TABLE_EMPTY'), ('S1\t11\nS2\t22\tx\n', 'INPUT_TABLE_RAGGED'),
            ('\t11\nS2\t22\n', 'SAMPLE_ID_MISSING'), ('IID\tIID\nS1\t11\n', 'INPUT_HEADER_DUPLICATE')]
        for content, code in cases:
            with self.subTest(code=code):
                self.phenotype.write_text(content)
                self.assertBlocked(self.sort(), code)
        self.tfam.write_text('F1 S1\nF2 S1\n')
        self.phenotype.write_text('S1 11\nS2 22\n')
        self.assertBlocked(self.sort(), 'SAMPLE_ID_DUPLICATE')

    def test_numeric_q_equal_counts_remains_unknown_inspection_succeeds(self):
        result = inspect_inputs(self.vcf, self.phenotype, self.q)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['q_matrix']['row_count'], result['vcf']['sample_count'])
        self.assertEqual(result['q_mapping']['status'], 'unknown')
        self.assertEqual(result['q_matrix']['sample_column'], None)
        self.assertEqual(result['blocked_operations'], ['q_covariate_preparation', 'association_using_q'])
        self.assertFalse(result['scientific_completion'])
        self.assertEqual(result['phenotype']['ids'][0], 'S1')
        self.assertFalse(self.out.exists())

    def test_unknown_q_blocks_output_and_preserves_old_bytes(self):
        self.out.write_text('keep\n')
        self.assertBlocked(self.covar(), 'Q_SAMPLE_MAPPING_UNKNOWN')
        self.assertEqual(self.out.read_text(), 'keep\n')

    def test_explicit_row_assignments_reorder_and_lock_versions(self):
        mapping = self.mapping()
        result = self.covar(mapping)
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.out.read_text(), 'F1\tS1\t1\t0.4\t0.5\t0.1\nF2\tS2\t1\t0.1\t0.2\t0.7\n')
        facts = json.loads(result[1])['q_mapping']
        self.assertEqual(facts['q_sha256'], digest(self.q))
        self.assertEqual(facts['tfam_sha256'], digest(self.tfam))
        self.assertEqual(facts['mapping_sha256'], digest(mapping))
        self.assertEqual(facts['comparison']['alignment'], 'reorder_required')
        observed = inspect_inputs(self.vcf, self.phenotype, self.q, row_map=mapping, tfam_path=self.tfam)
        self.assertEqual(observed['q_mapping']['status'], 'explicit_mapping_validated')
        self.assertEqual(observed['blocked_operations'], [])

    def test_mapping_path_or_count_alone_is_insufficient(self):
        for body in ['S1\nS2\n', json.dumps({'sample_list': str(self.tfam), 'row_count': 2})]:
            with self.subTest(body=body):
                self.assertBlocked(self.covar(self.write('map.json', body)), 'Q_ROW_MAP_INVALID')

    def test_map_wrong_file_versions_rejected(self):
        mapping = self.mapping()
        self.q.write_text(self.q.read_text() + '\n')
        self.assertBlocked(self.covar(mapping), 'Q_ROW_MAP_VERSION_MISMATCH')
        mapping = self.mapping()
        self.tfam.write_text('F2 S2 0 0 0 -9\nF1 S1 0 0 0 -9\n')
        self.assertBlocked(self.covar(mapping), 'Q_ROW_MAP_VERSION_MISMATCH')

    def test_map_duplicate_missing_and_wrong_order_rejected(self):
        cases = [('rows', [{'q_row': 1, 'sample_id': 'S1'}], 'Q_ROW_MAP_COVERAGE_INVALID'),
            ('rows', [{'q_row': 2, 'sample_id': 'S2'}, {'q_row': 1, 'sample_id': 'S1'}], 'Q_ROW_MAP_ROW_ORDER_INVALID'),
            ('rows', [{'q_row': 1, 'sample_id': 'S1'}, {'q_row': 2, 'sample_id': 'S1'}], 'SAMPLE_ID_DUPLICATE'),
            ('rows', [{'q_row': 1, 'sample_id': 'S1'}, {'q_row': 2, 'sample_id': 'S3'}], 'Q_ROW_MAP_SAMPLE_MISMATCH'),
            ('basis', '', 'Q_ROW_MAP_BASIS_MISSING')]
        for key, value, code in cases:
            with self.subTest(code=code):
                mapping = self.mapping()
                content = json.loads(mapping.read_text()); content[key] = value
                mapping.write_text(json.dumps(content))
                self.assertBlocked(self.covar(mapping), code)

    def test_q_header_labels_do_not_become_sample_ids(self):
        self.q.write_text('Q1 Q2 Q3\n0.1 0.2 0.7\n0.4 0.5 0.1\n')
        observed = inspect_inputs(self.vcf, self.phenotype, self.q)
        self.assertEqual(observed['q_matrix']['row_count'], 2)
        self.assertEqual(observed['q_mapping']['status'], 'unknown')
        self.assertEqual(self.covar(self.mapping())[0], 0)

    def test_labeled_q_order_can_be_validated_and_reordered(self):
        self.q.write_text('IID Q1 Q2\nS2 0.2 0.8\nS1 0.3 0.7\n')
        result = self.invoke(['--tfam', self.tfam, '--phenotype', self.q, '--trait', 'Q1,Q2',
            '--input-kind', 'q_covariates', '--out-emmax-covar', self.out])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.out.read_text(), 'F1\tS1\t1\t0.3\t0.7\nF2\tS2\t1\t0.2\t0.8\n')
        result = inspect_inputs(self.vcf, self.phenotype, self.q)
        self.assertEqual(result['q_mapping']['comparison']['alignment'], 'reorder_required')

    def test_nonfinite_q_is_not_a_sample_id_column(self):
        for value in ["inf", "-inf", "NaN", "1e999"]:
            with self.subTest(value=value):
                self.q.write_text(f"{value} 0.2 0.8\n0.3 0.5 0.2\n")
                self.assertEqual(table(self.q, require_ids=False)["sample_column"], None)
                self.assertBlocked(self.covar(), "Q_SAMPLE_MAPPING_UNKNOWN")
                self.assertBlocked(self.covar(self.mapping()), "Q_COVARIATE_VALUE_INVALID")

    def test_q_requires_finite_values_and_complete_rows(self):
        self.q.write_text('0.1 0.2 nan\n0.4 0.5 0.1\n')
        self.assertBlocked(self.covar(self.mapping()), 'Q_COVARIATE_VALUE_INVALID')
        self.assertBlocked(self.covar(self.mapping(), '--allow-missing', 'true'), 'Q_MISSING_ROWS_NOT_ALLOWED')

    def test_output_cannot_overwrite_input_or_share_destinations(self):
        original = self.phenotype.read_bytes()
        self.assertBlocked(self.sort('--out-emmax', self.phenotype), 'OUTPUT_PATH_CONFLICT')
        self.assertEqual(self.phenotype.read_bytes(), original)
        self.assertBlocked(self.sort('--out-tassel', self.out), 'OUTPUT_PATH_CONFLICT')

    def test_gzip_vcf_header_scoped_and_bounded(self):
        compressed = self.root / 'input.vcf.gz'
        with gzip.open(compressed, 'wb') as stream:
            stream.write(self.vcf.read_bytes())
        observed = vcf_header(compressed)
        self.assertEqual(observed['sample_ids'], ['S1', 'S2'])
        self.assertEqual(observed['source']['identity_scope'], 'header_only')
        self.assertFalse(observed['whole_vcf_validated'])
        with self.assertRaises(InputError) as caught:
            vcf_header(compressed, limit=20)
        self.assertEqual(caught.exception.facts['code'], 'VCF_HEADER_LIMIT')

    def test_vcf_duplicate_or_absent_samples_rejected(self):
        self.vcf.write_text(self.vcf.read_text().replace('\tS2\n', '\tS1\n'))
        with self.assertRaises(InputError) as caught:
            vcf_header(self.vcf)
        self.assertEqual(caught.exception.facts['code'], 'SAMPLE_ID_DUPLICATE')
        self.vcf.write_text('##fileformat=VCFv4.2\n1\t1\tA\n')
        with self.assertRaises(InputError) as caught:
            vcf_header(self.vcf)
        self.assertEqual(caught.exception.facts['code'], 'VCF_SAMPLE_HEADER_MISSING')

    def test_explicit_map_target_must_match_inspected_vcf_order(self):
        self.tfam.write_text('F2 S2\nF1 S1\n')
        with self.assertRaises(InputError) as caught:
            inspect_inputs(self.vcf, self.phenotype, self.q, row_map=self.mapping(), tfam_path=self.tfam)
        self.assertEqual(caught.exception.facts['code'], 'Q_ROW_MAP_TARGET_ORDER_MISMATCH')


if __name__ == '__main__':
    unittest.main(verbosity=2)

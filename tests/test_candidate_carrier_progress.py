"""Carrier changes preserve syntax applicability without manufacturing PASS."""
import unittest
from unittest.mock import patch
from agent_core.workflow.candidate_repair import candidate_progress_assessment
from execution.script_validation import check_executable_action


def assess(candidate, check, previous=()):
    return candidate_progress_assessment(candidate=candidate,
        representation_targets=['execute.argv', 'execute.env_object'] + (
            ['executable_syntax'] if check['check_status'] == 'PASS' else []),
        binding_facts=[], violations=[{'field_path': 'outputs'}],
        prior_rows=previous, contract_ref={}, check_records=[check])


class CandidateCarrierProgressTests(unittest.TestCase):
    def test_code_inline_registered_file_and_native_keep_true_syntax_records(self):
        candidates = [
            {'action_type': 'execute', 'code': 'printf ok', 'language': 'bash'},
            {'action_type': 'execute', 'argv': ['/bin/bash', '-c', 'printf ok']},
            {'action_type': 'execute', 'argv': ['/bin/bash', '/fixture/accepted.sh']},
            {'action_type': 'execute', 'argv': ['/usr/bin/printf', 'ok']},
        ]
        rows = []
        for number, candidate in enumerate(candidates):
            if number == 2:
                # Registered-file admission owns the verified bytes. This is
                # its trusted syntax receipt; comparison never reads a path.
                check = {'check_id': 'executable_syntax', 'check_status': 'PASS',
                         'source': '/fixture/accepted.sh', 'body_sha256': 'fixture-content-hash'}
            else:
                with patch('execution.script_validation.check_script_body', return_value={'status': 'valid'}):
                    check = check_executable_action(candidate)
            result = assess(candidate, check, rows)
            self.assertEqual(result['failed_checks'], [])
            self.assertEqual(result['unknown_targets'], [])
            self.assertEqual(result['check_records']['executable_syntax'], check)
            self.assertEqual('executable_syntax' in result['representation_targets'], number != 3)
            self.assertEqual(result['not_applicable_targets'], ['executable_syntax'] if number == 3 else [])
            self.assertFalse(result['repeat_proven'])
            rows.append({'repair_ordinal': number, 'progress_assessment': result})
        self.assertEqual(rows[0]['progress_assessment']['check_records']['executable_syntax']['check_status'], 'PASS')
        self.assertEqual(rows[-1]['progress_assessment']['check_records']['executable_syntax']['check_status'], 'NOT_APPLICABLE')

    def test_inapplicable_receipt_cannot_hide_missing_syntax_for_inline_body(self):
        result = assess({'code': 'broken(', 'language': 'python'},
            {'check_id': 'executable_syntax', 'check_status': 'NOT_APPLICABLE'})
        self.assertIn('executable_syntax', result['unknown_targets'])
        self.assertEqual(result['not_applicable_targets'], [])
        self.assertNotIn('executable_syntax', result['representation_targets'])

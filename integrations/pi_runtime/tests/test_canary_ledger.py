"""Send reservations use synthetic metadata; no provider/network or real quota."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from canary_ledger import execute, LedgerError


class CanaryLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='biomind-canary-budget-')
        self.path = Path(self.tmp.name) / 'budget.sqlite'
        self.task = 'synthetic-budget-test'
        self.call('init')

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, action, scenario='R1', **info):
        return execute(self.path, dict(action=action, taskId=self.task, scenario=scenario, info=info))

    def reserve(self, index, scenario='R1', provider=None):
        return self.call('reserve', scenario, attemptId='physical-' + str(index),
                         providerAttemptId=provider or 'provider-' + str(index),
                         operation='compaction' if index % 2 else 'main',
                         provider='synthetic', model='fixture', api='openai-completions',
                         transport='http', destination={'origin': 'https://secret:secret@fixture.invalid', 'pathname': '/never-log-secret'})

    def test_six_then_other_scenarios_and_eighteen_total(self):
        for i in range(6):
            self.reserve(i)
        with self.assertRaisesRegex(LedgerError, 'SCENARIO_BUDGET'):
            self.reserve(6)
        for i in range(6, 12):
            self.reserve(i, 'R2')
        for i in range(12, 18):
            self.reserve(i, 'R3')
        with self.assertRaisesRegex(LedgerError, 'TOTAL_BUDGET'):
            self.reserve(18, 'R3')
        summary = self.call('summary')
        self.assertEqual(summary['counts'], dict(R1=6, R2=6, R3=6))
        self.assertEqual(summary['reserved_sends'], 18)
        self.assertEqual(summary['unknown_usage_sends'], 18)
        self.assertNotIn('secret', json.dumps(summary))
        self.assertNotIn('pathname', json.dumps(summary))

    def test_concurrent_six_limit_is_atomic(self):
        def attempt(i):
            try:
                self.reserve(i)
                return 'reserved'
            except LedgerError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(24)))
        self.assertEqual(results.count('reserved'), 6)
        self.assertEqual(results.count('CANARY_SCENARIO_BUDGET_EXHAUSTED'), 18)
        self.assertEqual(self.call('summary')['reserved_sends'], 6)

    def test_cancel_and_crash_unknown_do_not_refund(self):
        self.reserve(0)
        self.call('send_result', attemptId='physical-0', outcome='aborted')
        # Another reserved send deliberately never receives a completion.
        for i in range(1, 6):
            self.reserve(i)
        self.call('init')
        with self.assertRaisesRegex(LedgerError, 'SCENARIO_BUDGET'):
            self.reserve(6)
        result = self.call('summary')
        self.assertEqual(result['reserved_sends'], 6)
        self.assertEqual(result['unknown_usage_sends'], 6)
        self.assertFalse(result['usage_complete'])

    def test_process_restart_reuses_budget_and_conflicting_task_fails(self):
        self.reserve(0)
        helper = Path(__file__).resolve().parents[1] / 'canary_ledger.py'
        request = dict(action='reserve', taskId=self.task, scenario='R1',
                       info=dict(attemptId='child', providerAttemptId='child-provider'))
        result = subprocess.run([sys.executable, '-B', str(helper), str(self.path)],
                                input=json.dumps(request), capture_output=True, text=True,
                                env={'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin'}, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['summary']['reserved_sends'], 2)
        self.assertEqual(self.call('summary')['reserved_sends'], 2)
        with self.assertRaisesRegex(LedgerError, 'IDENTITY'):
            execute(self.path, dict(action='init', taskId='a-new-task-cannot-reset'))
        self.assertEqual(self.call('summary')['reserved_sends'], 2)

    def test_duplicate_send_id_never_allows_free_dispatch(self):
        self.reserve(0)
        with self.assertRaisesRegex(LedgerError, 'DUPLICATE_PHYSICAL_SEND'):
            self.reserve(0)
        self.assertEqual(self.call('summary')['reserved_sends'], 1)

    def test_failed_usage_and_terminal_usage_count_once(self):
        self.reserve(0, provider='logical-provider')
        self.reserve(1, provider='logical-provider')
        usage = dict(input=1, output=1, cacheRead=0, cacheWrite=0, totalTokens=2)
        self.call('send_result', attemptId='physical-0', outcome='response', status=503, usage=usage)
        self.call('send_result', attemptId='physical-1', outcome='response', status=200, usage=usage)
        terminal = dict(providerAttemptId='logical-provider', physicalAttemptIds=['physical-0', 'physical-1'],
                        usage=usage, stopReason='stop')
        self.call('provider_result', **terminal)
        self.call('provider_result', **terminal)  # repeated observer ACK is idempotent
        summary = self.call('summary')
        self.assertEqual(summary['known_reported_usage']['totalTokens'], 4)
        self.assertEqual(summary['unknown_usage_sends'], 0)
        self.assertEqual(len(summary['provider_results']), 1)
        with self.assertRaisesRegex(LedgerError, 'ATTRIBUTION_MISMATCH'):
            self.call('provider_result', **{**terminal, 'physicalAttemptIds': ['physical-1']})

    def test_missing_usage_remains_unknown_and_invalid_cannot_reset(self):
        self.reserve(0)
        self.call('send_result', attemptId='physical-0', outcome='error')
        result = self.call('provider_result', providerAttemptId='provider-0', physicalAttemptIds=['physical-0'], usage=None)
        self.assertIsNone(result['attempts'][0]['usage'])
        self.assertFalse(result['usage_complete'])
        for action in ('reset', 'delete', 'refund'):
            with self.assertRaisesRegex(LedgerError, 'UNKNOWN_ACTION'):
                self.call(action)
        self.assertEqual(self.call('summary')['reserved_sends'], 1)

    def test_corrupt_or_uninitialized_ledger_fails_closed(self):
        with self.assertRaisesRegex(LedgerError, 'NOT_INITIALIZED'):
            execute(Path(self.tmp.name) / 'missing.sqlite', dict(action='reserve', scenario='R1'))
        bad = Path(self.tmp.name) / 'corrupt.sqlite'
        bad.write_text('not a database')
        with self.assertRaises(Exception):
            execute(bad, dict(action='init'))
        self.assertEqual(bad.read_text(), 'not a database')


if __name__ == '__main__':
    unittest.main(verbosity=2)

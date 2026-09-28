"""Bounded repair regressions with real consumers and existing offline guards.

Controlled model responses are fixtures, never provider/cache measurements.
No approval is answered and no scientific command or remote API is run.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from tests.contracts_support import contract_fixture, isolated_host


@pytest.fixture(autouse=True)
def guarded_host_and_witness_directory():
    (Path(os.environ['TMPDIR']).parent / 'witnesses').mkdir(exist_ok=True)
    with isolated_host():
        yield


def record(name, value):
    target = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('regression-' + name + '.json')
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))




@pytest.mark.parametrize('case', [
    'test_E04_reworded_consultation_cannot_reset_no_progress',
    'test_C04_C05_C08_revision_evidence_negative_and_commit_guards',
    'test_A07_E02_advice_authority_and_idempotent_delivery',
    'test_F05_raw_usage_snapshots_are_not_added',
    'test_F05_duplicate_stream_usage_remains_raw_and_normalizes_last_snapshot',
    'test_F05_unknown_usage_extensions_obey_existing_redaction',
])
def test_existing_context_continuation_regression(case):
    from tests import test_context_continuation_v1 as original
    getattr(original, case)()




def test_existing_shared_budget_accounting_and_migration():
    from tests.generate_repair.test_correctness import test_B03_B05_shared_accounting_and_migration
    test_B03_B05_shared_accounting_and_migration()











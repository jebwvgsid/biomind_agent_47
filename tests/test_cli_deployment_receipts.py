"""Validate actual process lifetime receipts without assuming zero heartbeats."""
from copy import deepcopy

import pytest

from tests.replay_approval_visibility_pty import process_lifecycle_is_complete


def receipt(phase, **values):
    return {'args': [phase, {'pid': 17, 'pgid': 17, **values}], 'kwargs': {}}


@pytest.mark.parametrize('heartbeats', [0, 1, 3])
def test_one_process_lifetime_preserves_all_heartbeats(heartbeats):
    rows = [receipt('RUNNING'), *[receipt('HEARTBEAT') for _ in range(heartbeats)],
            receipt('PROCESS_EXITED', returncode=0)]
    original = deepcopy(rows)
    assert process_lifecycle_is_complete(rows)
    assert rows == original


@pytest.mark.parametrize('rows', [
    [], [receipt('RUNNING')], [receipt('PROCESS_EXITED', returncode=0)],
    [receipt('RUNNING'), receipt('RUNNING'), receipt('PROCESS_EXITED', returncode=0)],
    [receipt('RUNNING'), receipt('PROCESS_EXITED', returncode=0), receipt('PROCESS_EXITED', returncode=0)],
    [receipt('RUNNING'), receipt('PROCESS_EXITED', returncode=0), receipt('HEARTBEAT')],
    [receipt('RUNNING'), receipt('HEARTBEAT', pid=18), receipt('PROCESS_EXITED', returncode=0)],
    [receipt('RUNNING'), receipt('OTHER'), receipt('PROCESS_EXITED', returncode=0)],
    [receipt('RUNNING'), receipt('PROCESS_EXITED', returncode=1)],
])
def test_incomplete_duplicate_foreign_or_failed_process_receipts_are_rejected(rows):
    assert not process_lifecycle_is_complete(rows)

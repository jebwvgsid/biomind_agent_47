"""Intentional signal ONLY in a new explicitly flagged guarded child."""
import os
import signal

import pytest


def test_synthetic_fatal_outlet():
    if os.getenv('BIOMIND_OFFLINE_FATAL_SELFTEST') != '1':
        pytest.skip('explicit isolated fatal-outlet validation only')
    assert os.getenv('BIOMIND_OFFLINE_FATAL_FILE')
    os.kill(os.getpid(), signal.SIGSEGV)
    raise AssertionError('Synthetic fatal signal unexpectedly returned')

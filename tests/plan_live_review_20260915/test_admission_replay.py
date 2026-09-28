"""Read-only supplemental Plan replay; fixed external replies, real admission.

Only the guarded exporter may run this module. Historical business replies are
data; no historical state is loaded and no Plan is approved or executed.
"""
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from tests.a1_unified_fix.test_f6_graph import SID

FIXTURES = Path(__file__).parent / 'fixtures'


def raw_round(n):
    return (FIXTURES / f'live_round_{n}.txt').read_text()


def live_minimum():
    value = json.loads(raw_round(3))
    value['proposed_artifacts'][0]['role_associations'][0]['step_id'] = 'runtime_healthcheck'
    return value


def store(name, value):
    root = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    body = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode()
    path = root / ('plan-review-' + name + '.json.gz')
    path.write_bytes(gzip.compress(body, mtime=0))
    return {'path': str(path), 'raw_sha256': hashlib.sha256(body).hexdigest()}







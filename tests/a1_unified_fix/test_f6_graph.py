"""F6: six distinct snpEff capabilities through the actual production graph.

External model responses and a new scientific-algorithm executable are explicit
stand-ins. Plan/approval/parser/helper/process/evidence/attester/Reviewer parser/
gate/Commit/FinalAudit are production implementations; no success-state seeding.
Only run with this change's guarded export_and_run launcher.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

SID = 'genomics/snpeff-annotation'
OPS = ['check_contig_compatibility', 'prepare_database', 'build_database',
       'annotate_variants', 'summarize_ann', 'finalize_outputs']
PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def save(name, value, *, full=False):
    root = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    body = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode()
    path = root / ('f6-' + name + ('.json.gz' if full else '.json'))
    path.write_bytes(gzip.compress(body, mtime=0) if full else body)
    return {'path': str(path), 'sha256': digest(path.read_bytes()), 'raw_sha256': digest(body)}







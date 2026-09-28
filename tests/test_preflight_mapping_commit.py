"""Actual mapping registry -> approval -> real mixed readers -> formal CommitStep."""
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from tests.test_preflight_recovery_audit import PYTHON, save
from tests.cli_model_progress_support import TimedWriter

SID = 'genomics/wgs-short-read-mapping'
RESOURCES = [SID+'#scripts/'+name for name in ['inspect_wgs_inputs.py','inspect_fastq.py','inspect_fasta_header.py','inspect_fastq_batch.py']]











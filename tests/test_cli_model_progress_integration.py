"""Target assertions at real production receive/dispatch/display boundaries.

Only run via cli_model_progress_support.py on its allowlisted /tmp export.
No original task or database is available to this test process.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time

import pytest
from langchain_core.messages import AIMessage

from tests.cli_model_progress_support import TimedWriter


def artifact(name, value):
    directory = Path(os.environ["BIOMIND_CLI_PROGRESS_ARTIFACTS"])
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))





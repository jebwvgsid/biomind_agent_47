"""Exact stopped-step ownership and current diagnostic facts, no live session."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from agent_core.cli_lifecycle import delivery_text, terminal_text
from agent_core.plan_tracker import build_plan_state, load_plan_state, save_plan_state
from agent_core.terminal_facts import _project_current_stop_facts, current_managed_stop, projection












def test_delivery_paths_require_current_successful_receipt(tmp_path):
    protected, current = str(tmp_path / "report.md"), str(tmp_path / "terminal" / "report.md")
    facts = {"required_output_status": "conflict", "report_paths": {"report.md": protected},
        "report_receipts": {"report.md": {"status": "failed", "reason": "RUNTIME_REPORT_EXISTING_FILE_PROTECTED"}},
        "internal_diagnostic_delivery": {"status": "complete", "paths": {"report.md": current},
            "receipts": {"report.md": {"status": "created", "sha256": "real-publication-hash"}}}}
    text = delivery_text(facts)
    assert "目标文件受保护，未覆盖" in text and current in text
    assert "本次报告 report.md：" not in text
    facts["internal_diagnostic_delivery"]["receipts"]["report.md"]["status"] = "failed"
    assert current not in delivery_text(facts)





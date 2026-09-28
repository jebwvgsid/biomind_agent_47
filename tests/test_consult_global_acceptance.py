"""Correct-behaviour acceptance via production Consult and persistent state."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_core.plan_tracker import load_plan_state
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as fixture
from tests.test_longrun_integration_v2 import gated_command, install_provider, tool_payloads, bootstrap, finish, record, facts, register_command
from agent_core.process_registry import ACTIVE_PROCESS_GROUPS




def test_balance_default_never_writes_input_and_explicit_opt_in_targets_exact_uri(tmp_path, monkeypatch):
    from tests.test_tad_calling_exit import _load_call_tads_module
    module = _load_call_tads_module()
    args = module.parse_args(['--sample','synthetic','input.mcool','--resolution-bp','40000','--out-root',str(tmp_path)])
    assert args.auto_balance is False
    row={'sample':'synthetic','matrix':'input.mcool','effective_resolution_bp':40000}
    monkeypatch.setattr(module,'check_cooler_balance',lambda *a:(False,"weight column 'weight' not found"))
    calls=[]
    monkeypatch.setattr(module,'run_cooler_balance',lambda uri,log:calls.append(uri) or 9)
    monkeypatch.setattr(module,'run_command',lambda *a,**k:pytest.fail('scientific command forbidden'))
    status,*_=module.process_sample(row,args)
    assert status['status']=='failed' and not calls
    args.auto_balance=True
    module.process_sample(row,args)
    assert calls==['input.mcool::/resolutions/40000']

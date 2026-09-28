"""OUT-01 acceptance at the real SDK/HTTP seam; all responses synthetic."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
import pytest
from agent_core.planner_output_policy import (
    PlannerOutputPolicyError, bind_planner_output_policy,
    planner_client_output_settings, planner_context_budget_policy,
    planner_input_admission, resolve_planner_output_policy,
)
from agent_core.workflow.context_compaction import ContextBudgetPolicy


def policy(**overrides):
    return resolve_planner_output_policy(model='deepseek-v4-flash', endpoint='https://api.deepseek.com/v1', provider='openai', **overrides)






















def test_t05_legacy_epoch_is_not_reinterpreted_as_new_output_policy():
    resolved=policy(legacy_epoch=True,recovery=True)
    assert resolved['apply_output_binding'] is False
    assert resolved['requested_output_limit'] is None
    assert resolved['max_length_corrective_calls_per_epoch'] == 1
    assert resolved['policy_reason']=='LEGACY_EPOCH_POLICY_PRESERVED'



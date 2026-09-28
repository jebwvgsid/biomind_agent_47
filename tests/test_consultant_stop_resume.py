"""Synthetic checkpoint only: production interactive continue -> go -> real graph."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import patch
from langchain_core.messages import AIMessage
import pytest










def test_current_stop_scope_is_not_cleared_for_other_owners():
    from agent_core.terminal_facts import current_managed_stop, _scope
    from agent_core.workflow.inspection_stop import _authority
    durable = {'task_id': 'synthetic', 'task_state': {'status': 'blocked'}}
    stop = {'producer': 'managed_run_stop_v1', 'persistence_status': 'committed',
            'scope': _scope(durable), 'authority': _authority(durable), 'stop_id': 'synthetic-recovery',
            'boundary': {'node': 'recovery'}}
    durable['task_state']['managed_run_stop'] = stop
    durable['task_state']['consultant_invocation'] = {'invocation_id': 'different-source-entry'}
    assert current_managed_stop(durable) == stop





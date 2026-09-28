"""Real Consultant source, shared Skill service, typed adapter and phase loop."""
import asyncio
from copy import deepcopy
import json
import hashlib
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage

SEARCH = {'decision': 'SEARCH_SKILLS', 'query': 'local BLAST sequence search',
          'capability_gap': 'Need documented sequence search',
          'desired_outcome': 'Find a local sequence search workflow',
          'requested_scope': 'task', 'relation_hint': 'unknown'}




def test_stale_skill_return_does_not_invent_observation():
    from agent_core.consultation_state import close_consultant_skill_turn, new_consultation_state
    state = new_consultation_state("synthetic", "synthetic")
    result = close_consultant_skill_turn(state, SEARCH, {'stale_node_result': {'decision_result': 'STALE_NODE_RESULT'}})
    assert result['consultation_state'] == state
    assert not result['consultation_state'].get('tool_observations')






def test_identical_actual_observation_still_reaches_no_progress_guard():
    from agent_core.consultation_state import close_consultant_skill_turn, new_consultation_state, classify_consultation_lifecycle
    state = new_consultation_state('synthetic', 'synthetic')
    for _ in range(3):
        state = close_consultant_skill_turn(state, SEARCH, {'skill_decision_result': {
            'decision_result': 'SKILL_SEARCH_NO_PROGRESS', 'results': []}})['consultation_state']
    assert classify_consultation_lifecycle(state)['reason_code'] == 'CONSULTANT_AGENT_NO_PROGRESS'



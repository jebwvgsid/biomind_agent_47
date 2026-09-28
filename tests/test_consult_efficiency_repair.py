"""Session 1789916737 carrier failures, soft repair and real Consult handoff."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_tracker import load_plan_state
from agent_core.skill_tools import skill_tool_schemas
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as fixture


def binding():
    return SimpleNamespace(semantic_handle_map={'U1': {
        'semantic_handle': 'U1', 'object_type': 'evidence',
        'stable_identity': 'user-turn-1', 'allowed_usage': ['cite']}})








def test_native_handoff_does_not_require_internal_mutation_or_evidence_protocol():
    tools = {schema['function']['name']: schema['function'] for schema in skill_tool_schemas('consultant')}
    assert not {'select_skill', 'update_taskspec'} & tools.keys()
    fields = tools['submit_planning_handoff']['parameters']['properties']
    assert 'output_dir' in fields
    assert not {'task_spec_operations', 'supporting_evidence'} & fields.keys()
    assert not {'root_ref', 'path_policy'} & fields['outputs']['items']['properties'].keys()







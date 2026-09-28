"""R18/R19 actual provider-wire captures and exact duplicate Skill projection."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage, SystemMessage
from agent_core.consult_handoff import deduplicate_loaded_skill_documents
from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.test_consult_v2_roundtrip import native
from tests.test_longrun_integration_v2 import tool_payloads, gated_command, bootstrap
from tests.test_registered_runtime_reuse import setup_registry
from tests import test_schema2_integration_a_production as fixture

CAPTURE = Path(os.environ.get('BIOMIND_FINAL_PROMPT_CAPTURE', '/tmp/biomind-consult-final-capture'))


def save_capture(label, messages, tools):
    CAPTURE.mkdir(parents=True, exist_ok=True)
    payload = {'label': label, 'messages': [{**provider_visible_payload(m), 'role': m.type} for m in messages], 'tools': deepcopy(tools)}
    (CAPTURE / (label + '.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def assert_tool_groups(messages):
    pending = set()
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            assert not pending, 'new tool group preceded prior results'
            pending = {row['id'] for row in message.tool_calls}
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in pending, 'orphan tool result'
            pending.remove(message.tool_call_id)
    assert not pending




def test_R19_exact_projection_preserves_partial_and_nonidentical_materials():
    full = {'skill_id': 'fixture', 'body': 'EXACT BODY', 'residency': 'exact',
        'coverage': {'kind': 'full'}, 'source_ref': 'skill-snapshot:read', 'content_hash': 'same'}
    later_partial = {**full, 'body': 'EXACT', 'coverage': {'kind': 'range'}}
    documents = {'fixture': 'EXACT BODY', 'unread': 'UNREAD BODY'}
    before = deepcopy(documents)
    projected = deduplicate_loaded_skill_documents(documents, [full, later_partial])
    assert projected['fixture']['provided_in'] == 'inherited_skill_material'
    assert projected['unread'] == 'UNREAD BODY' and documents == before
    for altered in [{**full, 'residency': 'externalized'}, {**full, 'residency': 'summarized'}, later_partial,
                    {**full, 'body': 'DIFFERENT BODY'}, {**full, 'coverage': {'kind': 'range'}}]:
        assert deduplicate_loaded_skill_documents(documents, [altered]) == documents


def test_R19_historical_actual_151515_seed_exact_body_projection():
    path = CAPTURE / 'historical_fixture.json'
    if not path.exists():
        pytest.skip('historical request fixture requires safe message-only extraction before guarded test')
    historical = json.loads(path.read_text())
    assert len(historical['seed']) == 151515
    before = deepcopy(historical['capability_context'])
    after = deepcopy(before)
    after['loaded_skill_documents'] = deduplicate_loaded_skill_documents(before['loaded_skill_documents'], historical['inherited_materials'])
    assert isinstance(after['loaded_skill_documents']['3d-dna/tad-calling'], dict)
    assert after['loaded_skill_documents']['3d-dna/tad-boundary-change'] == before['loaded_skill_documents']['3d-dna/tad-boundary-change']
    old_cap = json.dumps(before, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    new_cap = json.dumps(after, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    assert old_cap in historical['seed']
    after_seed = historical['seed'].replace(old_cap, new_cap, 1)
    assert len(after_seed) < len(historical['seed']) - 20000
    assert after_seed.replace(new_cap, old_cap, 1) == historical['seed'], 'all nonduplicate facts byte-identical'
    assert historical['payload']['evidence']
    assert '改原文件名字加后缀日期，继续执行' in historical['seed']
    assert '改原文件名字加后缀日期，继续执行' in after_seed
    result = {'historical_seed_chars': len(historical['seed']), 'after_exact_projection_chars': len(after_seed),
        'removed_chars': len(historical['seed']) - len(after_seed), 'source_sha256': historical['source_sha256'],
        'source_path': historical['source_path'], 'nonduplicate_seed_bytes_unchanged': True,
        'all_other_messages_unchanged': True, 'projection_only_not_replayed_cancelled_task': True,
        'remaining_relevant_skill_excerpt_preserved': True}
    (CAPTURE / 'historical-dedup-proof.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))

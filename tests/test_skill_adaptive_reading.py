"""Host context allowance, exact section pages, and stage receipt separation."""
import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_core.context_budget import skill_read_char_budget
from agent_core.workflow.context_compaction import ReferenceTokenEstimator
from agent_core.workflow.stage_contracts import freeze, StageContractError
from tests.test_consult_v2_pages import reader, assert_exact
from tests.test_consult_v2_roundtrip import native
from tests import test_schema2_integration_a_production as fixture


def document(size=20835):
    prefix = '---\nname: procedure\ndescription: exact sections\n---\n# Procedure\n'
    section = '## Input\n' + ('矩阵 sample value "quoted" \\path\n' * 120)
    body = prefix + section + '## Method\n' + ('method line\n' * 2000)
    return body[:size]


def test_medium_document_is_full_when_allowance_permits(tmp_path):
    service, state, docs, source, _ = reader(tmp_path, document())
    state, complete = service.load_skill_documents(state, ['local/procedure'], task_revision=0, max_new_chars=32000)
    page = complete[0]['provider_excerpt']
    assert page['exact_text'] == source and not page['has_more']
    assert page['status'] == 'COMPLETE' and page['body_delivered']
    assert complete[0]['cache_hit']  # Snapshot cached does not mean previously fully read.
    assert docs[0]['provider_excerpt']['has_more']


def test_long_document_pages_end_at_sections_and_preserve_all_bytes(tmp_path):
    source = '---\nname: procedure\ndescription: sections\n---\n' + ''.join(
        f'## Stage {i}\n' + ('数据 line "quoted" \\path\n' * 80) for i in range(12))
    service, state, docs, source, _ = reader(tmp_path, source)
    page = docs[0]['provider_excerpt']; pages = []
    while True:
        assert_exact(page, source); pages.append(page['exact_text'])
        assert len(json.dumps(page, ensure_ascii=False)) <= page['request_spec']['max_chars']
        if not page['has_more']:
            break
        assert source[page['source_end_char']:].startswith('## Stage ')
        assert not page['ends_mid_line']
        page = service._serve_one(state, page['next_request'], 'next')
    assert ''.join(pages) == source


@pytest.mark.parametrize('allowance', [0, 1500, 5000, 32000])
def test_batch_host_allowance_cannot_be_overridden_or_claim_unread_complete(tmp_path, allowance):
    service, state, _, source, _ = reader(tmp_path, document())
    requests = [{'skill_id': 'local/procedure', 'operation': 'full', 'max_chars': 999999}]
    pages = service._serve_batch(state, requests, 'batch', allowance)
    page = pages[0]
    if allowance < 2200:
        assert page['status'] == 'REQUIRES_NARROWING' and not page['body_delivered']
        assert not page.get('exact_text') and page['has_more']
    else:
        assert_exact(page, source)
        assert len(json.dumps(pages, ensure_ascii=False)) < allowance
        assert page['has_more'] == (allowance == 5000)


def test_multiple_documents_share_wire_budget(tmp_path):
    service, state, _, _, package = reader(tmp_path, document(3000))
    other = package.parent / 'long'; other.mkdir()
    (other / 'SKILL.md').write_text(document(40000).replace('name: procedure', 'name: long'))
    from agent_core.skill_manager import SkillManager
    service.skill_manager = SkillManager(str(package.parent.parent))
    state, docs = service.load_skill_documents(state, ['local/procedure', 'local/long'], task_revision=0, max_new_chars=32000)
    pages = [d['provider_excerpt'] for d in docs]
    assert not pages[0]['has_more'] and pages[1]['has_more']
    assert len(json.dumps(pages, ensure_ascii=False)) < 32000


def test_allowance_counts_history_native_arguments_schemas_and_output_reserve():
    estimator = ReferenceTokenEstimator()
    def budget(messages, schemas=(), limit=100000):
        return skill_read_char_budget(messages, schemas, safe_input_tokens=limit, estimator=estimator)
    initial = [HumanMessage(content='Read exact method')]
    history = initial + [AIMessage(content='', tool_calls=[{'id': 'a', 'name': 'read', 'args': {'source': '长文本' * 3000}}])]
    assert budget(history) < budget(initial)
    assert budget(history, [{'description': 'schema ' * 3000}]) < budget(history)
    assert budget(history, limit=10) == 0





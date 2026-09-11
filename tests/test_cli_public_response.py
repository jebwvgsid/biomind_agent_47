"""Transport/display separation against the actual business script parser.

Synthetic fixtures only. Run inside the allowlisted offline export; no provider,
state restoration, original database, or original task data is used here.
"""
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from agent_core.public_response import project_public_json


@pytest.mark.parametrize('field', [
    '"public_message":"先读取合成文件，再查看返回结果。",',
    '"public_message":null,',
    '"public_message":"",',
    '"public_message":"   ",',
    '"public_message":41,',
    '"public_message":true,',
    '"public_message":["not","public"],',
    '"public_message":{"text":"not public"},',
])
def test_optional_public_field_never_changes_action_bytes(field):
    business = '{"type" : "inspect", "path":"/tmp/synthetic.txt", "extra":{"public_message":"business data"}}'
    raw = '{' + field + business[1:]
    projected = project_public_json(raw)
    assert projected.error == ''
    assert projected.payload == business
    assert hashlib.sha256(projected.payload.encode()).digest() == hashlib.sha256(business.encode()).digest()
    assert json.loads(projected.payload)['extra']['public_message'] == 'business data'
    if field.startswith('"public_message":"先'):
        assert projected.public_message == '先读取合成文件，再查看返回结果。'
    else:
        assert not projected.public_message
    assert raw != projected.payload


@pytest.mark.parametrize('raw,business', [
    ('{"public_message":"说明","decision":"SEARCH_SKILLS","query":"fixture"}',
     '{"decision":"SEARCH_SKILLS","query":"fixture"}'),
    ('{"decision":"SEARCH_SKILLS","public_message":"说明","query":"fixture"}',
     '{"decision":"SEARCH_SKILLS","query":"fixture"}'),
    ('{"decision":"SEARCH_SKILLS","query":"fixture","public_message":"说明"}',
     '{"decision":"SEARCH_SKILLS","query":"fixture"}'),
    ('\r\n{"public_message":"说明","decision" : "SEARCH_SKILLS", "query" : "fixture"}\r\n',
     '\r\n{"decision" : "SEARCH_SKILLS", "query" : "fixture"}\r\n'),
])
def test_field_order_is_not_a_business_constraint(raw, business):
    projected = project_public_json(raw)
    assert projected.error == ''
    assert projected.payload == business
    assert projected.public_message == '说明'


@pytest.mark.parametrize('business', [
    ' {"type":"answer","answer":"旧回答"}\n',
    '{"plan":{"public_message":"literal business field","steps":[]}}',
    '{"text":"braces { } commas , escaped \\\"public_message\\\": and slash \\\\"}',
    '{"unknown_business_key":[{"public_message":"must survive"}]}',
])
def test_legacy_json_without_top_public_is_byte_identical(business):
    json.loads(business)
    projected = project_public_json(business)
    assert projected.payload == business
    assert not projected.public_message
    assert projected.error == ''


def test_json_strings_cannot_impersonate_top_level_boundaries():
    business = json.dumps({'type': 'answer', 'answer': '},{"public_message":"quoted data"}',
                           'extra': ['中文', {'x': '\\"},['}]}, ensure_ascii=False)
    public = '看到包含逗号、括号和引号的文本，接下来保留原数据。'
    raw = '{"public_message":' + json.dumps(public, ensure_ascii=False) + ',' + business[1:]
    projected = project_public_json(raw)
    assert projected.payload == business
    assert projected.public_message == public


@pytest.mark.parametrize('raw', [
    '{"public_message":"不得从坏JSON抢救","type":',
    '{"public_message":"trailing", "type":"answer"} trailing',
    '{"public_message":"one"}{"type":"answer"}',
    'prefix {"public_message":"invalid","type":"answer"}',
    '```json\n{"public_message":"fenced", "type":"answer"}\n```',
])
def test_broken_whole_json_does_not_publish_or_repair_business(raw):
    projected = project_public_json(raw)
    assert projected.payload == raw
    assert not projected.public_message
    with pytest.raises(json.JSONDecodeError):
        json.loads(projected.payload)


def test_projection_does_not_mutate_and_does_not_normalize_business():
    raw = '{"public_message":"准备核对","type":17,"unknown":{"public_message":"keep"}}'
    projected = project_public_json(raw)
    assert json.loads(projected.payload) == {'type': 17, 'unknown': {'public_message': 'keep'}}
    with pytest.raises(FrozenInstanceError):
        projected.payload = 'mutation'
    assert raw == '{"public_message":"准备核对","type":17,"unknown":{"public_message":"keep"}}'


def test_current_consultant_planner_prompts_enable_public_field():
    from prompts.manager import PromptManager
    for prompt in [PromptManager.get_consultant_v2_prompt({}), PromptManager.get_planner_v2_system_prompt(),
                   PromptManager.get_consultant_discovery_escalation_prompt({})]:
        assert 'public_message' in prompt


def test_duplicate_public_fields_do_not_publish_an_ambiguous_message():
    raw = '{"public_message":"one","type":"answer","public_message":"two","answer":"business"}'
    projected = project_public_json(raw)
    assert not projected.public_message
    assert json.loads(projected.payload) == {'type': 'answer', 'answer': 'business'}


def test_non_json_constants_do_not_enable_public_projection():
    for raw in ['{"public_message":"text","value":NaN}', '{"public_message":"text","value":Infinity}']:
        projected = project_public_json(raw)
        assert projected.payload == raw
        assert not projected.public_message


def test_original_consultant_and_planner_parsers_keep_business_rejection():
    from bio_agents import BioAnalystAgent
    from agent_core.plan_contract import parse_planner_outcome_envelope, PlannerEnvelopeError
    valid = '{"action_type":"inspect_resource","resource":"/tmp/synthetic.txt"}'
    invalid = '{"action_type":"unknown_business_action","unknown_business_key":7}'
    for business in (valid, invalid):
        for public in (None, "", 17, [], "准备读取"):
            wire = '{"public_message":' + json.dumps(public) + ',' + business[1:]
            projected = project_public_json(wire)
            assert BioAnalystAgent._consultant_json_payload(projected.payload) == json.loads(business)
            assert BioAnalystAgent._consultant_command_from_json(projected.payload) == BioAnalystAgent._consultant_command_from_json(business)
    for business in ('{"outcome_type":"UNKNOWN","steps":[]}', '{"steps":[]}'):
        projected = project_public_json('{"public_message":"候选仍待校验",' + business[1:])
        errors = []
        for raw in (business, projected.payload):
            with pytest.raises(PlannerEnvelopeError) as caught:
                parse_planner_outcome_envelope(raw, allow_legacy_plan=False)
            errors.append(caught.value.to_diagnostic())
        assert errors[0] == errors[1]
    broken = '{"public_message":"不会公开","outcome_type":'
    with pytest.raises(PlannerEnvelopeError) as caught:
        parse_planner_outcome_envelope(project_public_json(broken).payload)
    assert caught.value.to_diagnostic()['code'] == 'PLANNER_ENVELOPE_JSON_INVALID'

"""Transport/display separation against the actual business script parser.

Synthetic fixtures only. Run inside the allowlisted offline export; no provider,
state restoration, original database, or original task data is used here.
"""
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from agent_core.public_response import project_public_json, project_generate_response
from agent_core.controller_audit import ScriptBlockError, parse_executable_script_block


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


SCRIPT_PAYLOADS = [
    '<shell>\n  printf \'fixture\\n\'  \n</shell>\n',
    '\t<shell>\r\nprintf \'CRLF\\r\\n\'\r\n</shell>\r\n',
    '<shell>\ncat <<\'FIXTURE\'\n中文 "quotes" \\slashes\\\n<xml>literal data</xml>\nFIXTURE\n</shell>',
    '<python>\ntext = "中文\\n\\\\quote\\\""\nfor value in [text]:\n    print(value)\n\n</python>',
    '<r>\nx <- "中文\\n"\nfor (i in 1:2) {\n  cat(x)\n}\n</r>',
    '<shell skill_resources="fixture-a;fixture-b">\nprintf \'fixture\\n\'\n</shell>',
]


@pytest.mark.parametrize('payload', SCRIPT_PAYLOADS)
def test_generate_wrapper_preserves_real_parser_body_utf8_and_hash(tmp_path, payload):
    raw = json.dumps({'response_type': 'biomind_generate_display_v1',
                      'public_message': '候选已形成，接下来先检查语法，再提交原审批。',
                      'payload': payload}, ensure_ascii=False)
    projected = project_generate_response(raw, script_protocol=True)
    assert projected.error == ''
    assert projected.payload == payload
    assert projected.public_message == '候选已形成，接下来先检查语法，再提交原审批。'
    old = parse_executable_script_block(payload)
    new = parse_executable_script_block(projected.payload)
    assert new == old
    old_bytes, new_bytes = old.body.encode('utf-8'), new.body.encode('utf-8')
    old_path, new_path = tmp_path / 'old-candidate', tmp_path / 'new-candidate'
    old_path.write_bytes(old_bytes)
    new_path.write_bytes(new_bytes)
    assert old_path.read_bytes() == new_path.read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest() == hashlib.sha256(new_bytes).hexdigest()
    assert hashlib.sha256(raw.encode()).hexdigest() != hashlib.sha256(new_bytes).hexdigest()
    legacy = project_generate_response(payload, script_protocol=True)
    assert legacy.payload == payload and not legacy.public_message and legacy.error == ''


@pytest.mark.parametrize('public', [None, '', ' ', 0, [], {'unexpected': 'object'}])
def test_generate_bad_optional_display_still_unwraps_legal_payload(public):
    payload = '<shell>\nprintf fixture\n</shell>'
    raw = json.dumps({'response_type': 'biomind_generate_display_v1', 'public_message': public, 'payload': payload})
    projected = project_generate_response(raw, script_protocol=True)
    assert projected.error == '' and projected.payload == payload
    assert not projected.public_message


@pytest.mark.parametrize('value', [None, '', 3, [], {}, True])
def test_generate_recognized_invalid_payload_is_not_guessed(value):
    raw = json.dumps({'response_type': 'biomind_generate_display_v1', 'payload': value})
    projected = project_generate_response(raw, script_protocol=True)
    assert projected.error
    assert projected.payload == raw


@pytest.mark.parametrize('extra', [{'script': '<shell>echo conflict</shell>'}, {'type': 'execute'}, {'alternate_payload': 'conflict'}])
def test_generate_conflicting_transport_fields_are_rejected(extra):
    raw = json.dumps({'response_type': 'biomind_generate_display_v1', 'payload': '<shell>echo fixture</shell>', **extra})
    projected = project_generate_response(raw, script_protocol=True)
    assert projected.error and projected.payload == raw


def test_generate_missing_payload_is_an_explicit_error():
    raw = '{"response_type":"biomind_generate_display_v1","public_message":"候选形成"}'
    assert project_generate_response(raw, script_protocol=True).error


@pytest.mark.parametrize('payload', [
    '<shell><script>printf fixture</script></shell>',
    '<script>printf fixture</script>',
    'prose <shell>printf fixture</shell>',
    '<shell>printf fixture</shell><python>print("fixture")</python>',
    '<shell>\n   \n</shell>',
])
def test_generate_unwrap_does_not_relax_existing_script_rejection(payload):
    raw = json.dumps({'response_type': 'biomind_generate_display_v1', 'payload': payload})
    projected = project_generate_response(raw, script_protocol=True)
    with pytest.raises(ScriptBlockError) as old:
        parse_executable_script_block(payload)
    assert projected.payload == payload
    assert projected.error == str(old.value)
    assert not projected.public_message


@pytest.mark.parametrize('payload', [
    '{"decision":"SEARCH_SKILLS","query":"fixture"}',
    '{"type":"blocked","reason":"fixture stopping reason"}',
    '<solution>这是合成结束说明。</solution>',
    '这是不带脚本块的合成最终回答。',
])
def test_generate_script_envelope_cannot_enter_other_early_return_branches(payload):
    raw = json.dumps({'response_type': 'biomind_generate_display_v1',
                      'public_message': '不能把其它响应冒充脚本载荷。', 'payload': payload}, ensure_ascii=False)
    projected = project_generate_response(raw, script_protocol=True)
    with pytest.raises(ScriptBlockError) as original:
        parse_executable_script_block(payload)
    assert projected.error == str(original.value)
    assert projected.payload == payload
    assert projected.public_message == '不能把其它响应冒充脚本载荷。'


@pytest.mark.parametrize('payload', [
    '<shell><script>printf fixture</script></shell>',
    '{"decision":"SEARCH_SKILLS","query":"fixture"}',
])
def test_invalid_known_payload_identity_is_independent_of_public_words(payload):
    projections = [project_generate_response(json.dumps({
        'response_type': 'biomind_generate_display_v1', 'public_message': public,
        'payload': payload}, ensure_ascii=False), script_protocol=True)
        for public in ('第一条公开说明。', '第二条不同的公开说明。')]
    with pytest.raises(ScriptBlockError) as original:
        parse_executable_script_block(payload)
    assert [item.public_message for item in projections] == ['第一条公开说明。', '第二条不同的公开说明。']
    assert all(item.error == str(original.value) for item in projections)
    assert all(item.payload.encode('utf-8') == payload.encode('utf-8') for item in projections)
    assert len({hashlib.sha256(item.payload.encode('utf-8')).hexdigest() for item in projections}) == 1


def test_generate_envelope_is_gated_and_unknown_versions_stay_business_input():
    payload = '<shell>printf fixture</shell>'
    current = json.dumps({'response_type': 'biomind_generate_display_v1', 'payload': payload})
    gated = project_generate_response(current, script_protocol=False)
    assert gated.payload != payload
    unknown = json.dumps({'response_type': 'biomind_generate_display_v9', 'payload': payload})
    projected = project_generate_response(unknown, script_protocol=True)
    assert projected.payload == unknown
    with pytest.raises(ScriptBlockError):
        parse_executable_script_block(projected.payload)


@pytest.mark.parametrize('decision', [
    {'decision': 'REQUEST_SKILL_CONTEXT', 'requests': [{'operation': 'section', 'heading': 'fixture'}]},
    {'decision': 'SEARCH_SKILLS', 'query': 'fixture'},
    {'type': 'blocked', 'reason': 'synthetic unresolved constraint'},
    {'type': 'execute', 'action': {'command': 'fixture'}},
])
@pytest.mark.parametrize('script_protocol', [False, True])
def test_generate_native_json_and_early_decision_fields_are_separated(decision, script_protocol):
    business = json.dumps(decision, separators=(',', ':'), ensure_ascii=False)
    raw = '{"public_message":"根据当前合成结果调整下一步。",' + business[1:]
    projected = project_generate_response(raw, script_protocol=script_protocol)
    assert projected.error == '' and projected.payload == business
    assert projected.public_message == '根据当前合成结果调整下一步。'


def test_active_v2_prompts_enable_public_transport_at_actual_prompt_functions():
    from prompts.manager import PromptManager
    consultant = PromptManager.get_consultant_v2_prompt({})
    planner = PromptManager.get_planner_v2_system_prompt()
    generate_native = PromptManager.get_generate_v2_prompt({})
    generate_script = PromptManager.get_generate_v2_prompt({
        'semantic_history': {},
        'current_focus': {'runtime': {'generate_output_contract': 'script_block_v1'}},
    })
    for prompt in [consultant, planner, generate_native, generate_script]:
        assert 'public_message' in prompt
    assert 'biomind_generate_display_v1' in generate_script
    assert '"payload"' in generate_script
    assert 'For the executable alternative, do not return prose, Markdown fences, JSON,' not in generate_script
    example = next(line for line in generate_script.splitlines()
                   if line.startswith('{"response_type":"biomind_generate_display_v1"'))
    decoded = json.loads(example)
    expected = "<shell>\nprintf 'fixture\\n'\n</shell>"
    assert decoded['payload'] == expected
    assert parse_executable_script_block(decoded['payload']) == parse_executable_script_block(expected)


@pytest.mark.parametrize('metadata', [
    {'finish_reason': 'length'}, {'finish_reason': 'max_tokens'}, {'finish_reason': 'content_filter'},
    {'completion_integrity': 'truncated'}, {'completion_integrity': 'interrupted'},
    {'completion_integrity': 'incomplete'}, {'completion_integrity': 'content_filtered'},
])
def test_generate_valid_json_cannot_override_negative_provider_integrity(metadata):
    raw = json.dumps({'response_type': 'biomind_generate_display_v1',
                      'public_message': '不可提前公开不完整响应',
                      'payload': '<shell>printf fixture</shell>'})
    projected = project_generate_response(raw, script_protocol=True, response_metadata=metadata)
    assert projected.error and projected.payload == raw
    assert not projected.public_message


def test_generate_duplicate_public_is_display_only_degradation():
    raw = '{"response_type":"biomind_generate_display_v1","public_message":"one","payload":"<shell>printf fixture</shell>","public_message":"two"}'
    projected = project_generate_response(raw, script_protocol=True)
    assert projected.error == ''
    assert projected.payload == '<shell>printf fixture</shell>'
    assert not projected.public_message


@pytest.mark.parametrize('raw', [
    '{"response_type":"biomind_generate_display_v1","payload":"<shell>printf fixture</shell>","payload":"<shell>printf second</shell>"}',
    '{"response_type":"biomind_generate_display_v1","response_type":"biomind_generate_display_v1","payload":"<shell>printf fixture</shell>"}',
])
def test_generate_duplicate_business_transport_is_rejected(raw):
    projected = project_generate_response(raw, script_protocol=True)
    assert projected.error and projected.payload == raw



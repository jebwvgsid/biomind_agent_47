"""Original wires are parsed only. No original script/helper is executed."""
import ast
import gzip
import json
from pathlib import Path

import pytest

from agent_core.public_response import project_generate_response
from agent_core.skill_resource_binding import parse_generate_script_candidate
from agent_core.controller_audit import ScriptBlockError


def archived_candidates():
    return json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/preflight_1789734892_candidates.json.gz').read_bytes()))


@pytest.mark.parametrize('number,accepted', [('0016', False), ('0026', False), ('0027', True), ('0028', True)])
def test_four_original_rejected_candidates(number, accepted):
    row = next(r for r in archived_candidates() if r['trace'].startswith('generate-' + number))
    wire = row['response']['content']
    result = project_generate_response(wire, script_protocol=True, strict=False)
    if accepted:
        assert not result.error
        block = parse_generate_script_candidate(result.payload, None, {}, step_id='preflight_inputs', cwd='/tmp', strict=False)
        assert block.body.strip() and block.language == 'bash'
        # This proves carrier reception only; resource admission, approval and
        # scientific correctness remain separate gates.
        assert block.body in json.loads(wire.strip().split('\n', 1)[1].rsplit('\n', 1)[0])['payload']
    else:
        assert 'AMBIGUOUS_WRAPPER' in result.error
        assert result.diagnostic['field_path'] == '$'
        assert not result.public_message


def test_archive_requests_had_same_complete_protocol_before_fix():
    rows = archived_candidates()
    assert {r['message_count'] for r in rows} == {8, 12, 14, 18}
    protocol = [r['protocol_messages'] for r in rows]
    assert all(len(r) == 1 for r in protocol)
    assert all(r[0]['content'] == protocol[0][0]['content'] for r in protocol)
    assert 'skill_script' in protocol[0][0]['content']


@pytest.mark.parametrize('label', ['', 'json'])
@pytest.mark.parametrize('payload', [
    {'response_type': 'biomind_generate_display_v1', 'payload': '<python>print("offline")</python>'},
    {'type': 'blocked', 'reason': 'Evidence missing'},
    {'type': 'skill_script', 'handle': 'fixture/skill#scripts/a.py', 'arguments': []},
])
def test_whole_json_fence_matches_raw_projection(label, payload):
    raw = json.dumps(payload)
    assert project_generate_response(' \n```' + label + '\n' + raw + '\n```\n', script_protocol=True) == project_generate_response(raw, script_protocol=True)


@pytest.mark.parametrize('wire', [
    'prose\n```json\n{"type":"blocked","reason":"x"}\n```',
    '```json\n{"type":"blocked","reason":"x"}\n```\nextra',
    '```json\n{} {}\n```',
    '```json\n{}\n```\n```json\n{}\n```',
    '```json\n{"payload": NaN}\n```',
    '```\n{}\n```\n<shell>echo second</shell>',
])
def test_ambiguous_json_never_selectively_extracts_action(wire):
    value = project_generate_response(wire, script_protocol=True, strict=False)
    assert value.error and value.payload == wire


def test_nested_helper_feedback_is_field_specific_and_does_not_move_action():
    helper = {'type': 'skill_script', 'handle': 'fixture/skill#scripts/a.py', 'arguments': ['--evidence-dir', {'runtime_binding':'BIOMIND_PREFLIGHT_EVIDENCE_DIR'}]}
    raw = json.dumps({'response_type':'biomind_generate_display_v1', 'payload': json.dumps(helper)})
    value = project_generate_response(raw, script_protocol=True)
    assert value.diagnostic['field_path'] == '$.payload'
    assert value.diagnostic['example'] == helper
    assert 'top-level skill_script' in value.diagnostic['correct_branch']
    assert value.error and json.loads(value.payload) == helper


def test_duplicate_members_and_negative_provider_completion_still_fail():
    duplicate = '{"response_type":"biomind_generate_display_v1","payload":"<shell>true</shell>","payload":"<shell>false</shell>"}'
    assert project_generate_response('```json\n' + duplicate + '\n```', script_protocol=True).error
    assert project_generate_response('```json\n{}\n```', script_protocol=True, response_metadata={'finish_reason':'length'}).error.startswith('GENERATE_RESPONSE_INCOMPLETE')


def test_explicit_empty_selection_survives_script_adapter():
    from agent_core.controller_audit import adapt_script_block_to_action
    for tag in ['shell', 'python']:
        block = parse_generate_script_candidate(f'<{tag} skill_resources="">print("synthetic")</{tag}>', None, {}, step_id='p', cwd='/tmp')
        action = adapt_script_block_to_action(block)
        assert action['skill_resource_refs'] == []
    ordinary = parse_generate_script_candidate('<python>print("synthetic")</python>', None, {}, step_id='p', cwd='/tmp')
    assert 'skill_resource_refs' not in adapt_script_block_to_action(ordinary)


def test_object_payload_gets_the_same_top_level_helper_example():
    helper={'type':'skill_script','handle':'fixture/skill#scripts/a.py','arguments':[]}
    result=project_generate_response(json.dumps({'response_type':'biomind_generate_display_v1','payload':helper}),script_protocol=True)
    assert result.error and result.diagnostic['field_path']=='$.payload'
    assert result.diagnostic['example']==helper and 'top-level skill_script' in result.diagnostic['correct_branch']

"""Debug parsing changes transport only, and never selects competing code."""

import hashlib
import json

import pytest

from agent_core.controller_audit import ScriptBlockError, parse_executable_script_block
from agent_core.public_response import project_generate_response


BODY = "\n# 中文 with XML data\ncat <<'DATA'\n<shell>literal data</shell>\nDATA\nprintf '%s\\n' 'done'\n"


@pytest.mark.parametrize("carrier", [
    '<shell language="bash">{body}</shell>',
    "<shell language='sh' skill_resources='fixture-a;fixture-b'>{body}</shell>",
    '已生成脚本。\n<shell language="bash"><![CDATA[{body}]]></shell>\n请查看。',
    '已生成脚本。\n```xml\n<shell language="bash">{body}</shell>\n```\n请查看。',
])
def test_relaxed_mode_preserves_executable_bytes(carrier):
    raw = carrier.format(body=BODY)
    with pytest.raises(ScriptBlockError):
        parse_executable_script_block(raw)
    block = parse_executable_script_block(raw, strict=False)
    assert block.language == "bash"
    assert block.body == BODY
    assert hashlib.sha256(block.body.encode()).hexdigest() == hashlib.sha256(BODY.encode()).hexdigest()
    if "skill_resources" in carrier:
        assert block.skill_resource_refs == ("fixture-a", "fixture-b")


@pytest.mark.parametrize("tag,language,body", [
    ("python", "python3", "print('fixture')"),
    ("r", "Rscript", "print('fixture')"),
])
def test_other_matching_language_attributes(tag, language, body):
    block = parse_executable_script_block(f'<{tag} language="{language}">{body}</{tag}>', strict=False)
    assert block.tag == tag and block.body == body


@pytest.mark.parametrize("raw", [
    '<shell language="python">true</shell>',
    '<shell language="bash" language="bash">true</shell>',
    '<shell unexpected="value">true</shell>',
    '<shell language="bash">true',
    '<shell>true</shell><shell>false</shell>',
    '<shell>true</shell>\n另一种选择：\n<shell>false</shell>',
    '<shell>true</shell>\n<python>print("other")</python>',
    '<shell><![CDATA[true]]></shell><shell><![CDATA[false]]></shell>',
    '<shell>true</shell>\n```bash\nfalse\n```',
    '```bash\nfalse\n```\n<shell>true</shell>',
    '<script>true</script><shell>false</shell>',
    '<shell><script>true</script></shell>',
    '<shell>\n  \n</shell>',
    '<shell><![CDATA[]]></shell>',
])
def test_relaxed_mode_still_requires_one_unambiguous_script(raw):
    with pytest.raises(ScriptBlockError):
        parse_executable_script_block(raw, strict=False)


def test_display_transport_uses_same_mode_and_keeps_payload_bytes():
    payload = '说明\n<shell language="bash"><![CDATA[' + BODY + ']]></shell>'
    raw = json.dumps({"response_type": "biomind_generate_display_v1", "public_message": "候选生成", "payload": payload})
    assert project_generate_response(raw, script_protocol=True).error
    projected = project_generate_response(raw, script_protocol=True, strict=False)
    assert not projected.error and projected.payload == payload
    assert projected.public_message == "候选生成"
    assert parse_executable_script_block(projected.payload, strict=False).body == BODY


def test_explained_blocked_is_terminal_json_only_when_relaxed():
    payload = '{"type":"blocked","reason":"missing dependency"}'
    raw = "当前无法继续，缺少依赖。\n\n" + payload
    assert project_generate_response(raw, script_protocol=True).payload == raw
    projected = project_generate_response(raw, script_protocol=True, strict=False)
    assert projected.payload == payload
    assert projected.public_message == "当前无法继续，缺少依赖。"
    assert not projected.error


def test_blocked_explanation_may_quote_a_script_carrier_name():
    raw = 'The `<shell>` carrier was rejected.\n\n{"type":"blocked","reason":"repair exhausted"}'
    projected = project_generate_response(raw, script_protocol=True, strict=False)
    assert json.loads(projected.payload)["type"] == "blocked"


def test_existing_script_data_is_unchanged_in_relaxed_mode():
    raw = "<shell>" + BODY + "</shell>"
    assert parse_executable_script_block(raw, strict=False) == parse_executable_script_block(raw)


@pytest.mark.parametrize("raw", [
    '说明\n{"type":"execute","action":{"command":"true"}}',
    '说明\n{"type":"blocked","reason":"missing"}\n后续其它内容',
    '```python\n{"type":"blocked","reason":"example"}\n```',
    '<shell>cat <<EOF\n{"type":"blocked","reason":"data"}\nEOF\n</shell>',
    '<shell>true</shell>\n{"type":"blocked","reason":"other outcome"}',
    'example = {\n"type": "blocked"\n}\n{"type":"blocked","reason":"nested"}',
    '说明\n{"type":"blocked","reason":',
])
def test_explanation_cannot_authorize_executable_or_embedded_json(raw):
    projected = project_generate_response(raw, script_protocol=True, strict=False)
    assert projected.payload == raw


def test_relaxed_response_still_rejects_provider_truncation():
    raw = '<shell language="bash">true</shell>'
    projected = project_generate_response(raw, script_protocol=True, strict=False,
                                         response_metadata={"finish_reason": "length"})
    assert "GENERATE_RESPONSE_INCOMPLETE" in projected.error

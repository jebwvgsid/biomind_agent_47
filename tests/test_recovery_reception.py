import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.workflow.recovery_reception import (
    PROTOCOL, SUBMIT_TOOL, ReceptionError, decode_text, strict_json, validate_candidate,
)
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder
from tests.test_reliability_evidence_publication import _artifact_case, _source, _reopen


def candidate(task="publication-fixture", step="step.inspect"):
    return dict(schema_version=1, directive_id="decision-reception", task_id=task,
        action="retry_step", target_step_id=step, summary="脚本位置待核验；环境尚未检查。",
        reason_code="SCRIPT_BUG", instruction="Resolve selected Skill resource before regenerating and verifying.")


def native(value=None, content="诊断已收到，修复尚未执行。"):
    return AIMessage(content=content, tool_calls=[dict(id="submit-1", name=SUBMIT_TOOL, args=value or candidate())])


@pytest.mark.parametrize("transport", ["bare", "envelope", "block"])
def test_explicit_transport_keeps_explanation_without_repair(transport):
    explanation = '中文 "quoted" / English\n```bash\nprintf example\n```\n'
    raw = json.dumps(candidate(), ensure_ascii=False)
    wire = raw if transport == "bare" else json.dumps(dict(protocol=PROTOCOL, explanation=explanation, directive=candidate())) if transport == "envelope" else explanation + "```" + PROTOCOL + "\n" + raw + "\n```\n"
    value, body, receipt = decode_text(wire)
    assert validate_candidate(value).action == "retry_step"
    assert body == ("" if transport == "bare" else explanation)
    assert receipt["transport"]


def test_uncleaned_actual_tad_response_extracts_but_needs_resubmission():
    p = Path(__file__).parent / "fixtures/tad_recovery_reception/recovery_turn-0009-e0298b577b1e.json"
    raw = p.read_bytes()
    content = json.loads(raw)["content"]
    assert content.startswith("The skill material and search confirm")
    with pytest.raises(ReceptionError) as raised:
        decode_text(content)
    assert raised.value.code == "RECOVERY_CANONICAL_RESUBMISSION_REQUIRED"
    assert raised.value.candidate["schema_status"] == "valid"
    assert raw == p.read_bytes()


@pytest.mark.parametrize("wire,code", [
    ('{"action":"retry_step","action":"blocked"}', "RECOVERY_JSON_DUPLICATE_KEY"),
    ('{"x":NaN}', "RECOVERY_JSON_NONFINITE_NUMBER"),
    ('{"x":1e999}', "RECOVERY_JSON_NONFINITE_NUMBER"),
    ('[' * 33 + '0' + ']' * 33, "RECOVERY_RESPONSE_DEPTH_LIMIT"),
    (' ' * 128001, "RECOVERY_RESPONSE_SIZE_LIMIT"),
    ('{"action":', "RECOVERY_DIRECTIVE_JSON_INVALID"),
])
def test_command_json_is_bounded_and_never_repaired(wire, code):
    with pytest.raises(ReceptionError) as raised:
        strict_json(wire)
    assert raised.value.code == code


@pytest.mark.parametrize("change", [{"evidence_refs": "not-a-list"}, {"schema_version": True}, {"instruction": {}}, {"action": "goto"}, {"unexpected": 1}])
def test_decoded_json_still_requires_original_contract(change):
    with pytest.raises(ReceptionError):
        validate_candidate({**candidate(), **change})










def test_two_ordinary_command_blocks_are_ambiguous():
    with pytest.raises(ReceptionError, match="AMBIGUOUS"):
        decode_text(("```json\n" + json.dumps(candidate()) + "\n```\n") * 2)



def test_explicit_block_offsets_and_explanation_are_lossless():
    before='\n\n  中文说明\n';after='\n  尾部说明  \n\n'
    block='```'+PROTOCOL+'\n'+json.dumps(candidate())+'\n```\n'
    raw,explanation,receipt=decode_text(before+block+after)
    assert explanation==before+after
    assert receipt['start']==len(before) and receipt['end']==len(before+block)











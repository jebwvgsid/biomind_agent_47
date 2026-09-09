import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.workflow.recovery_agent import RecoveryAgentLoop, RecoveryAgentLoopConfig, recovery_tool_schemas
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


def run_loop(tmp_path, replies, config=None, counters=None):
    state, _, _ = _artifact_case(tmp_path)
    source = _source(state, "execute_outcome", "execution_failed")
    CausalFailurePacketBinder(data_path=str(tmp_path)).bind(state, "execute_outcome", source)
    durable, seed, registry, dispatcher = _reopen(tmp_path, state["task_id"])
    requests, events = [], []
    class Model:
        def bind_tools(self, schemas):
            assert any(r["function"]["name"] == SUBMIT_TOOL for r in schemas)
            return self
        async def ainvoke(self, messages):
            requests.append(deepcopy(messages))
            return replies[len(requests)-1]
    loop = RecoveryAgentLoop(model=Model(), dispatcher=dispatcher, config=config,
        event_sink=lambda event,payload: events.append(dict(event=event, **payload)))
    result = asyncio.run(loop.ainvoke(seed=seed, durable=durable,
        inspection_manifest=registry.provider_manifest(), budget_state=counters))
    return result, requests, events


@pytest.mark.parametrize("first", ["prose", "schema", "scope", "mixed", "two_submits", "ordinary_fence"])
def test_one_feedback_round_preserves_explanation_and_pairs_tools(tmp_path, first):
    explanation = "已识别脚本路径问题；仍需要可信绑定。"
    messages = {
        "prose": AIMessage(content=explanation),
        "schema": native({**candidate(), "evidence_refs": "bad"}, explanation),
        "scope": native({**candidate(), "target_step_id": "foreign"}, explanation),
        "mixed": AIMessage(content=explanation, tool_calls=[*native().tool_calls, dict(id="inspect-first", name="failure_inspect", args={"ref_id":"not-used"})]),
        "two_submits": AIMessage(content=explanation, tool_calls=[*native().tool_calls, dict(id="submit-2", name=SUBMIT_TOOL, args=candidate())]),
        "ordinary_fence": AIMessage(content=explanation + "\n```json\n" + json.dumps(candidate()) + "\n```"),
    }
    result, requests, events = run_loop(tmp_path, [messages[first], native()])
    assert len(requests) == 2 and result["directive"]["action"] == "retry_step"
    assert result["audit"]["format_correction_count"] == 1
    assert result["audit"]["tool_call_count"] == 0
    assert result["audit"]["no_progress_rounds"] == 0
    assert any(explanation in str(m.content) for m in requests[1])
    feedback = [m for m in requests[1] if '"format_corrections_remaining"' in str(m.content)]
    assert feedback and all('"command_started":false' in m.content for m in feedback)
    ids = {c["id"] for c in messages[first].tool_calls}
    assert ids == {m.tool_call_id for m in feedback if hasattr(m, "tool_call_id")}
    assert any(e["event"] == "recovery_response_received" and e["explanation"] for e in events)


def test_native_empty_content_uses_summary_once(tmp_path):
    result, requests, events = run_loop(tmp_path, [native(content="")])
    assert len(requests) == 1 and result["directive"]
    assert result["reception"]["explanations"][0]["text"] == candidate()["summary"]


@pytest.mark.parametrize("mode", ["exhausted", "turn_limit", "length", "no_progress"])
def test_failed_reception_cannot_reset_or_exceed_original_limits(tmp_path, mode):
    response = AIMessage(content="说明保留；尚未正式提交。", response_metadata={"finish_reason":"length"} if mode == "length" else {})
    config = RecoveryAgentLoopConfig(max_llm_turns=1 if mode == "turn_limit" else 12)
    counters = {"format_correction_count":1} if mode == "exhausted" else {"no_progress_rounds":2} if mode == "no_progress" else {}
    result, requests, _ = run_loop(tmp_path, [response], config, counters)
    assert len(requests) == (0 if mode == "no_progress" else 1) and not result["directive"]
    assert result["terminal_error"]
    if mode != "no_progress":
        assert result["reception"]["explanations"]
    assert result["audit"]["tool_call_count"] == 0


def test_two_ordinary_command_blocks_are_ambiguous():
    with pytest.raises(ReceptionError, match="AMBIGUOUS"):
        decode_text(("```json\n" + json.dumps(candidate()) + "\n```\n") * 2)

@pytest.mark.parametrize('args,code', [('{"action":"blocked","action":"retry_step"}', 'RECOVERY_JSON_DUPLICATE_KEY'), ('{"x":NaN}', 'RECOVERY_JSON_NONFINITE_NUMBER'), ('{"x":', 'RECOVERY_DIRECTIVE_JSON_INVALID'), ('{"x":'+'['*33+'0'+']'*33+'}','RECOVERY_RESPONSE_DEPTH_LIMIT')])
def test_raw_native_arguments_survive_sdk_normalization_and_feedback(tmp_path,args,code):
    first=AIMessage(content='解释不能随参数错误丢失。',additional_kwargs={'tool_calls':[{'id':'bad-args','type':'function','function':{'name':SUBMIT_TOOL,'arguments':args}}]})
    result,requests,events=run_loop(tmp_path,[first,native()])
    assert len(requests)==2 and result['directive']
    assert any(code in str(m.content) for m in requests[1])
    assert any(getattr(m,'tool_call_id','')=='bad-args' for m in requests[1])
    assert result['reception']['explanations'][0]['text']==first.content
    assert any(e.get('message',{}).get('tool_calls',[{}])[0].get('function',{}).get('arguments')==args for e in events if e['event']=='recovery_assistant_message_committed')


def test_explicit_block_offsets_and_explanation_are_lossless():
    before='\n\n  中文说明\n';after='\n  尾部说明  \n\n'
    block='```'+PROTOCOL+'\n'+json.dumps(candidate())+'\n```\n'
    raw,explanation,receipt=decode_text(before+block+after)
    assert explanation==before+after
    assert receipt['start']==len(before) and receipt['end']==len(before+block)


def test_native_submission_does_not_execute_json_example_in_prose(tmp_path):
    explanation='Example only:\n```json\n{"action":"blocked"}\n```'
    result,requests,_=run_loop(tmp_path,[native(content=explanation)])
    assert len(requests)==1 and result['directive']['action']=='retry_step'
    assert result['reception']['explanations'][0]['text']==explanation


def test_two_formal_transports_reject_even_with_pretty_reordered_envelope(tmp_path):
    content=json.dumps({'explanation':'说明','directive':candidate(),'protocol':PROTOCOL},indent=2)
    result,requests,_=run_loop(tmp_path,[native(content=content),native()])
    assert len(requests)==2 and result['directive']
    assert 'RECOVERY_SUBMISSION_AMBIGUOUS' in str(requests[1][-1].content)


def test_compacted_resume_keeps_format_and_total_budget(tmp_path):
    result,requests,_=run_loop(tmp_path/'first',[AIMessage(content='说明一'),AIMessage(content='说明二')])
    assert len(requests)==2 and result['terminal_error']['format_correction_exhausted']
    resumed,requests,_=run_loop(tmp_path/'second',[AIMessage(content='说明三')],counters=result['_resume_state'])
    assert len(requests)==1 and not resumed['directive']
    assert resumed['audit']['turn_count']==3 and resumed['audit']['format_correction_count']==1
    assert len(resumed['reception']['explanations'])==3


def test_native_nonfinite_normalized_args_preserve_explanation(tmp_path):
    first=native({**candidate(),'schema_version':float('nan')})
    result,requests,_=run_loop(tmp_path,[first,native()])
    assert len(requests)==2 and result['directive']
    assert 'RECOVERY_JSON_NONFINITE_NUMBER' in str(requests[1][-1].content)


def test_boundary_checkpoint_restores_feedback_and_raw_invalid_arguments(tmp_path):
    from agent_core.workflow.recovery_agent import recovery_boundary_messages_from_events
    from agent_core.workflow.scoped_context import provider_visible_payload
    first=AIMessage(content='解释原文',additional_kwargs={'tool_calls':[{'id':'invalid','type':'function','function':{'name':SUBMIT_TOOL,'arguments':'{"x":'}}]})
    _,requests,events=run_loop(tmp_path,[first,native()])
    boundaries=[]
    for e in events:
        if e['event']=='recovery_model_turn_started' and e.get('turn_ordinal')==2:break
        boundaries.append(e)
    restored=recovery_boundary_messages_from_events(boundaries)
    suffix=[provider_visible_payload(m) for m in requests[1][2:]]
    assert [provider_visible_payload(m) for m in restored]==suffix

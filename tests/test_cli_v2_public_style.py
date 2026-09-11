"""UX3 public style is a display preference, never a new response validator.

Run through the allowlisted offline exporter/guard. Only synthetic Recovery
seed/provider/reader fixtures and fresh lineage roots are used here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json

import pytest
from langchain_core.messages import AIMessage

from agent_core.context_lineage import ContextLineageStore
from agent_core.public_response import project_public_json
from agent_core.workflow.recovery_agent import RECOVERY_SYSTEM_PROMPT
from agent_core.workflow.recovery_reception import PROTOCOL, SUBMIT_TOOL
from prompts.manager import PUBLIC_MESSAGE_POLICY, PromptManager
from tests.test_cli_model_progress_integration import artifact
from tests.test_cli_model_recovery_tools import _recovery_case
from tests.test_cli_v2_display import service_writer_callbacks
from tests.test_recovery_agent_loop import _directive


SUMMARY = "现有回执表明本次命令未完成；我将提交当前候选的修正建议。"
LONG_PUBLIC = ("我已看到本次执行返回的公开事实，仍需核对输入和约束；以下说明不是执行批准。" * 12)


def directive(summary=SUMMARY):
    return {**json.loads(_directive()), "summary": summary}


def native(candidate=None, *, content=LONG_PUBLIC):
    return AIMessage(id="synthetic-style-provider-response", content=content,
        tool_calls=[{"id": "synthetic-style-submit", "name": SUBMIT_TOOL,
                     "args": deepcopy(candidate if candidate is not None else directive()),
                     "type": "tool_call"}], response_metadata={"finish_reason": "tool_calls"})


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def execute(tmp_path, replies, **kwargs):
    async def run():
        service_writer_callbacks(asyncio.get_running_loop())
        return await _recovery_case(tmp_path, replies, **kwargs)
    return asyncio.run(run())


def publics(owner):
    return [item.text for item in owner.model.items if item.kind == "model_public"]


def assistant_checkpoints(root):
    path = ContextLineageStore(str(root), "task-recovery").root / "events.jsonl"
    return [record["payload"] for record in map(json.loads, path.read_text().splitlines())
            if record["event_kind"] == "recovery_assistant_message_committed"]


def test_ux3_formal_prompts_offer_language_and_length_preferences_without_new_schema():
    assert "user's language" in PUBLIC_MESSAGE_POLICY
    assert "wording is not a new validation requirement" in PUBLIC_MESSAGE_POLICY
    assert "one to three" in PUBLIC_MESSAGE_POLICY
    assert "display style preferences, never new validation or retry requirements" in RECOVERY_SYSTEM_PROMPT
    assert "proposed next action" in RECOVERY_SYSTEM_PROMPT
    assert "proposal until actual admission" in RECOVERY_SYSTEM_PROMPT
    for prompt in (PromptManager.get_consultant_v2_prompt({}),
                   PromptManager.get_planner_v2_system_prompt(),
                   PromptManager.get_consultant_discovery_escalation_prompt({})):
        assert PUBLIC_MESSAGE_POLICY in prompt
    long_english = "The observed result has a limit. A correction is still a proposal. " * 16
    business = '{"type" : "answer", "answer" : "unchanged business"}'
    response = '{"public_message":' + json.dumps(long_english) + ',' + business[1:]
    projection = project_public_json(response)
    assert projection.payload == business and projection.public_message == long_english
    assert projection.error == ""


@pytest.mark.parametrize("content", [LONG_PUBLIC, ""])
def test_ux3_valid_native_summary_uses_existing_value_and_preserves_raw_receipts(tmp_path, content):
    reply = native(content=content)
    original = reply.model_dump()
    result, requests, receptions, dispatches, _, owner, writer = execute(tmp_path, [reply])
    assert len(requests) == 1 and dispatches == []
    assert result["audit"]["format_correction_count"] == 0
    assert result["audit"]["transport_attempt_count"] == 1
    assert result["directive"] == directive() and not result["terminal_error"]
    assert publics(owner) == [SUMMARY]
    assert receptions[0]["status"] == "candidate_validated"
    assert receptions[0]["transport"] == "native_tool"
    assert receptions[0]["explanation"] == SUMMARY
    assert receptions[0]["directive_adopted"] is False
    assert receptions[0]["command_started"] is False
    if content:
        assert content in owner.model.details
        assert content not in writer.getvalue()
        assert result["reception"]["explanations"] == [{
            "source_message_id": receptions[0]["source_message_id"],
            "source": "assistant.content", "text": content}]
    else:
        assert result["reception"]["explanations"][0]["source"] == "directive.summary"
    records = assistant_checkpoints(tmp_path)
    assert len(records) == 1
    assert records[0]["message"]["content"] == content
    assert records[0]["message"]["tool_calls"][0]["args"] == directive()
    assert receptions[0]["response_sha256"] == sha(records[0]["message"])
    assert any(message.content == content and message.tool_calls[0]["args"] == directive()
               for message in result["_local_messages"] if isinstance(message, AIMessage))
    assert any(message.get("content") == content and message.get("tool_calls")
               for message in result["_provider_episode"] if message.get("type") == "ai")
    assert reply.model_dump() == original


def test_ux3_long_english_summary_does_not_trigger_retry_or_change_business(tmp_path):
    summary = ("The execution record describes an incomplete result. The proposed repair still requires existing admission. " * 12).strip()
    candidate = directive(summary)
    reply = native(candidate)
    on = execute(tmp_path / "on", [reply])
    off = execute(tmp_path / "off", [reply], enabled=False)
    for result, requests, receptions, dispatches, _, owner, _writer in (on, off):
        assert len(requests) == 1 and dispatches == []
        assert result["directive"] == candidate and not result["terminal_error"]
        assert result["audit"]["format_correction_count"] == 0
        assert receptions[0]["explanation"] == summary
        assert publics(owner) == [summary]
        assert LONG_PUBLIC in owner.model.details
    assert off[6].getvalue() == ""
    assert on[0]["reception"] == off[0]["reception"]
    assert on[0]["directive"] == off[0]["directive"]
    artifact("cli-v2-public-style-native.json", {
        "fixture": "synthetic native submission; language/length are deliberately nonpreferred",
        "provider_calls": len(on[1]), "real_tool_calls": len(on[3]),
        "format_corrections": on[0]["audit"]["format_correction_count"],
        "directive_unchanged": on[0]["directive"] == candidate,
        "main_public_equals_existing_summary": publics(on[5]) == [summary],
        "original_public_body_in_details": LONG_PUBLIC in on[5].model.details,
        "original_body_sha256": hashlib.sha256(LONG_PUBLIC.encode()).hexdigest(),
        "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
        "display_on_off_reception_equal": on[0]["reception"] == off[0]["reception"],
        "authority": "candidate validated; no directive execution or admission claimed by this loop-only fixture"})


@pytest.mark.parametrize("first_content", [LONG_PUBLIC, ""])
def test_ux3_investigating_keeps_original_public_selection_and_history(tmp_path, first_content):
    first = AIMessage(content=first_content, tool_calls=[{
        "id": "style-actual-inspection", "name": "failure_inspect", "args": {}, "type": "tool_call"}])
    result, requests, receptions, dispatches, _, owner, _writer = execute(tmp_path, [first, native()])
    assert len(requests) == 2 and len(dispatches) == 1
    assert result["audit"]["format_correction_count"] == 0
    assert receptions[0]["status"] == "investigating"
    assert receptions[0]["explanation"] == first_content
    assert publics(owner) == ([first_content] if first_content else []) + [SUMMARY]
    assert any(isinstance(item, AIMessage) and item.content == first_content
               and item.tool_calls[0]["id"] == "style-actual-inspection" for item in requests[1])
    assert result["directive"] == directive()


@pytest.mark.parametrize("invalid_summary", [None, ""])
def test_ux3_invalid_or_missing_summary_uses_existing_feedback_and_public_prose(tmp_path, invalid_summary):
    invalid = directive()
    if invalid_summary is None:
        invalid.pop("summary")
    else:
        invalid["summary"] = invalid_summary
    result, requests, receptions, dispatches, _, owner, _writer = execute(tmp_path, [native(invalid), native()])
    assert len(requests) == 2 and dispatches == []
    assert result["audit"]["format_correction_count"] == 1
    assert receptions[0]["status"] == "not_accepted"
    assert receptions[0]["explanation"] == LONG_PUBLIC
    assert publics(owner) == [LONG_PUBLIC, SUMMARY]
    assert result["directive"] == directive()
    original = [item for item in requests[1] if isinstance(item, AIMessage)][0]
    assert original.content == LONG_PUBLIC and original.tool_calls[0]["args"] == invalid
    assert result["reception"]["explanations"][0]["text"] == LONG_PUBLIC


def test_ux3_text_envelope_still_prefers_its_explicit_explanation(tmp_path):
    explanation = "这是文本信封的公开说明；本轮继续沿用其既有选择。"
    raw = json.dumps({"protocol": PROTOCOL, "explanation": explanation,
                      "directive": directive()}, ensure_ascii=False)
    result, requests, receptions, dispatches, _, owner, _writer = execute(tmp_path, [AIMessage(content=raw)])
    assert len(requests) == 1 and dispatches == []
    assert result["audit"]["format_correction_count"] == 0
    assert publics(owner) == [explanation]
    assert receptions[0]["explanation"] == explanation
    assert result["reception"]["explanations"][0]["text"] == raw
    assert result["directive"] == directive()

"""Planner rejection -> saved context -> next provider request, without a model."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, messages_to_dict

from agent_core.node_context import ControlMetadataLeakError
from agent_core.planner_context import (
    provider_correction_feedback, restore_correction_messages, save_correction_context,
)
from agent_core.provider_message_safety import assert_v2_llm_messages_clean
from agent_core.state_contract_store import StateContractStore
from agent_core.terminal_facts import explanation


REF = "object:sha256:" + "a" * 64


def feedback():
    return {
        "code": "PLANNER_CANDIDATE_REJECTED",
        "validation_issues": [{
            "code": "REQUIRED_OUTPUT_UNCOVERED",
            "message": "Required output `filtered_vcf` has no logical producer.",
            "repair_target": "outputs", "step_id": "route_and_plan",
            "source_ref": REF,
            "affected_output": {"handle": "output.filtered_vcf", "output_id": "filtered_vcf"},
            "machine_evidence": {"producer_step_ids": [], "source_ref": REF,
                                 "approval_ref": "approval-private", "node_context_hash": "private",
                                 "expected": "logical producer", "actual": None},
        }],
        "requirements": [{"message": "Produce filtered VCF and statistics.", "source_ref": REF}],
        "correction_budget": {"planner_calls_total": 1, "next_candidate_ordinal": 2},
    }


def test_projection_keeps_repair_meaning_and_local_audit():
    original = feedback(); before = deepcopy(original)
    projected = provider_correction_feedback(original)
    assert original == before
    issue = projected["validation_issues"][0]
    assert issue["step_key"] == "route_and_plan"
    assert issue["affected_output"]["handle"] == "output.filtered_vcf"
    assert issue["machine_evidence"] == {"producer_step_ids": [], "expected": "logical producer", "actual": None}
    assert projected["correction_budget"] == original["correction_budget"]
    assert projected["requirements"][0]["message"] == original["requirements"][0]["message"]
    assert provider_correction_feedback(projected) == projected
    assert_v2_llm_messages_clean([SystemMessage(content=json.dumps(projected))])


def test_nested_diagnostic_refs_and_length_feedback_are_projected():
    original = feedback()
    original["validation_issues"][0]["message"] += " Source: " + REF
    original["response_completion"] = {"finish_reason": "length", "reported_usage": {"output_tokens": 8192},
                                       "parser_diagnostic": {"message": "at " + REF}}
    original["length_recovery"] = {"selected_remediation": {"kind": "compact_json", "output_policy_hash": "sha256:" + "b" * 64}}
    projected = provider_correction_feedback(original)
    assert REF in projected["validation_issues"][0]["message"]
    assert "source_ref" not in projected["validation_issues"][0]
    assert projected["response_completion"]["reported_usage"]["output_tokens"] == 8192
    assert projected["length_recovery"]["selected_remediation"] == {"kind": "compact_json"}


def test_saved_correction_can_dispatch_twice_and_preserves_raw_evidence(tmp_path):
    state = {"task_state": {"planning_epoch": {"planning_epoch_id": "epoch", "planner_calls_total": 1},
                            "planning_failure": {"validation_issues": feedback()["validation_issues"]}}}
    prefix = [SystemMessage(content="Return a corrected plan."), HumanMessage(content="Create filtered VCF.")]
    raw = '{"candidate": "unchanged bytes"}'
    for _ in range(2):
        projected = save_correction_context(str(tmp_path), "task", state, task_spec={"goal": "Create VCF"},
            raw=raw, candidate={}, provider_messages=prefix, fixed_prefix_count=2, capabilities={})
        ref = state["task_state"]["planning_failure"]["context_ref"]
        record = StateContractStore(str(tmp_path), "task").read_object(ref, expected_type="planner_correction_context")
        assert record["feedback"]["validation_issues"][0]["source_ref"] == REF
        assert record["raw_candidate"] == raw
        assert record["provider_feedback"] == projected
        restored = restore_correction_messages(None, record)
        assert restored[-2].content == raw
        assert_v2_llm_messages_clean(restored)
        prefix = restored


def test_legacy_record_projects_only_feedback_and_preserves_history():
    raw = '{"plan": "original response"}'
    messages = [SystemMessage(content="Plan"), HumanMessage(content="Task"), AIMessage(content=raw),
                SystemMessage(content=json.dumps(feedback()))]
    record = {"messages": messages_to_dict(messages), "fixed_prefix_count": 2, "planning_epoch_id": "epoch"}
    before = deepcopy(record)
    context = SimpleNamespace(messages=[], system_message=None, epoch_id="new-epoch")
    restored = restore_correction_messages(context, record)
    assert record == before
    assert [m.content for m in restored[:3]] == [m.content for m in messages[:3]]
    assert json.loads(restored[-1].content)["validation_issues"][0]["step_key"] == "route_and_plan"
    assert context.messages[-1].content == restored[-1].content
    assert_v2_llm_messages_clean(restored)


@pytest.mark.parametrize("message", [HumanMessage(content=REF), AIMessage(content=REF), SystemMessage(content=REF)])
def test_unrelated_messages_preserve_hashes_without_blocking(message):
    record = {"messages": messages_to_dict([message]), "fixed_prefix_count": 0}
    restored = restore_correction_messages(None, record)
    assert restored[0].content == REF
    assert_v2_llm_messages_clean(restored)


@pytest.mark.parametrize("planning", [False, True])
def test_failed_task_is_distinct_from_completed_summary(planning):
    stop = {"status": "failed", "stage": "planner", "summary_status": "COMPLETED",
            "origin": {"error_code": "FRONT_HALF_NODE_HARNESS_ERROR"}}
    if planning:
        stop["planning"] = {"no_approved_plan": True}
    before = deepcopy(stop)
    text = explanation(stop)
    assert "任务状态：FAILED" in text
    assert "停止总结生成状态：COMPLETED" in text
    assert "不代表任务成功" in text
    assert stop == before

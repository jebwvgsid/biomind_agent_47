"""Explanation boundary checks, supplementary to actual graph/outer acceptance.

Use the guarded offline candidate bootstrap. These tests do not claim graph
coverage: test_workflow exercises the real summary caller and report writer.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import bio_agents
from agent_core.workflow import managed_stop


def _receipt(call_id, finish, limit=16384):
    return {"schema_version": 1, "call_id": call_id, "provider_attempt_id": call_id + ":1",
        "finish_reason": finish, "transport_status": "COMPLETED",
        "content_status": "TRUNCATED" if finish == "length" else "NORMAL_END",
        "reported_usage": {"output_tokens": 8192},
        "resolved_output_policy": {"requested_output_limit": limit,
            "wire_output_parameter_key": "max_tokens", "wire_output_parameter_value": limit,
            "wire_observation_status": "resolved_not_wire_observed", "output_policy_source": "isolated_fixture"},
        "parser_diagnostic": {"status": "not_run" if finish == "length" else "failed"}}


def _planning(*, first="length", latest="length"):
    issue = {"code": "PLANNER_OUTPUT_TRUNCATED" if latest == "length" else "PLANNER_ENVELOPE_TRANSPORT_INVALID",
        "message": "recorded provider boundary" if latest == "length" else "synthetic parser rejection",
        "field_path": "$", "expected_type": "single_json_object", "received_type": "incomplete_json",
        "diagnostic_source": "isolated_fixture", "validator_id": "parse_plan_envelope"}
    primary = {"validation_issues": [{**issue, "code": "PLANNER_OUTPUT_TRUNCATED" if first == "length" else "PLANNER_ENVELOPE_TRANSPORT_INVALID"}],
        "response_integrity": _receipt("first-call", first)}
    return {"producer": "planner_terminal_facts_v1", "scope": {"task_id": "isolated-explanation", "task_revision": 1,
        "planning_epoch_id": "epoch-1", "plan_id": "", "plan_version": "", "step_id": "", "attempt_id": ""},
        "task_summary": "Synthetic offline planning", "planning_epoch": {"planner_calls_total": 2},
        "primary_failure": primary, "validation_issues": [issue],
        "response_integrity": _receipt("latest-call", latest, 32768),
        "attempted_remediation": [{"kind": "raise_limit_and_compact", "status": "completed",
            "output_policy": {"requested_output_limit": 32768}, "result": "length"}],
        "terminal_diagnostic": {"error_code": "PLANNER_LENGTH_RECOVERY_EXHAUSTED", "detail": "Length corrective already used"}}


def _stop(planning):
    boundary = {"node": "planner", "status": "blocked", "outcome": {"type": "terminal",
        "payload": {"planning_terminal": planning, "status": "blocked"}}}
    return managed_stop.build_managed_stop("isolated-explanation", {}, {}, boundary)


@pytest.mark.parametrize("summary_status,skip_reason", [
    ("FAILED", ""), ("SKIPPED", "CONFIG_DISABLED"),
    ("SKIPPED", "GLOBAL_TOKEN_BUDGET_INSUFFICIENT"), ("SKIPPED", "DURABLE_SCOPE_UNCONFIRMED"),
])
def test_held_base_keeps_truncation_policy_and_delivery_truth_without_summary(summary_status, skip_reason):
    planning = _planning()
    stop = _stop(planning)
    stop.update(summary_status=summary_status, summary_skip_reason=skip_reason)
    diagnostic = managed_stop.projection(stop)["terminal_diagnostic"]
    text = diagnostic["reason"]
    for expected in ("first-call", "latest-call", "finish_reason=length", "输出触及 provider 长度边界",
        "16384", "32768", "resolved_not_wire_observed", "服务端实际生效上限=UNKNOWN",
        "PLANNER_LENGTH_RECOVERY_EXHAUSTED", "single_json_object", "incomplete_json", "isolated_fixture",
        "reproduce.sh=skipped / NO_EXECUTION_ATTEMPT", "report.md=not_attempted", "科学交付尚未完成"):
        assert expected in text, expected
    assert stop["primary_cause"]["response_integrity"] == planning["primary_failure"]["response_integrity"]
    assert diagnostic["response_integrity"] == planning["response_integrity"]
    assert diagnostic["attempted_remediation"] == planning["attempted_remediation"]
    assert diagnostic["execution"]["executed"] is False
    assert diagnostic["report_paths"] == {}
    assert "已生成停止报告" not in text
    # Neither historical exception coordinates nor server effective settings
    # are invented from the fixture's usage/requested setting.
    assert "position" not in diagnostic["validation_issues"][0]
    assert "server_effective_output_limit" not in diagnostic["output_policy"]


def test_latest_syntax_failure_does_not_erase_earliest_truncation_or_become_length():
    planning = _planning(latest="stop")
    planning["terminal_diagnostic"]["error_code"] = "PLANNER_REPEATED_FAILURE"
    stop = _stop(planning)
    text = managed_stop.explanation(stop)
    assert "最初 provider：call=first-call；finish_reason=length" in text
    assert "末次 provider：call=latest-call；finish_reason=stop" in text
    assert text.count("输出触及 provider 长度边界") == 1
    assert stop["latest_failure"]["error_code"] == "PLANNER_ENVELOPE_TRANSPORT_INVALID"
    assert stop["stop_reason"]["error_code"] == "PLANNER_REPEATED_FAILURE"


def test_absent_metadata_or_8192_tokens_do_not_imply_truncation_or_historical_policy():
    planning = _planning(first="stop", latest="stop")
    planning["primary_failure"].pop("response_integrity")
    planning["response_integrity"] = {"reported_usage": {"output_tokens": 8192}}
    planning["attempted_remediation"] = []
    planning["terminal_diagnostic"]["error_code"] = "PLANNER_NON_PROGRESS_REPLAY"
    text = managed_stop.explanation(_stop(planning))
    assert "finish_reason=UNKNOWN" in text
    assert "本地请求输出上限=UNKNOWN" in text
    assert "输出触及 provider 长度边界" not in text
    assert "PLANNER_LENGTH_RECOVERY_EXHAUSTED" not in text
    assert "没有已记录的输出长度补救" in text


def test_intermediate_first_truncation_is_preserved_without_relabeling_original_syntax_failure():
    planning = _planning(first="stop", latest="stop")
    planning["first_truncation"] = _receipt("middle-first-length", "length")
    planning["terminal_diagnostic"]["error_code"] = "PLANNER_REPEATED_FAILURE"
    stop = _stop(planning)
    diagnostic = managed_stop.projection(stop)["terminal_diagnostic"]
    assert diagnostic["primary_cause"]["error_code"] == "PLANNER_ENVELOPE_TRANSPORT_INVALID"
    assert diagnostic["primary_cause"]["response_integrity"]["finish_reason"] == "stop"
    assert diagnostic["first_truncation"] == planning["first_truncation"]
    assert "首个截断 provider：call=middle-first-length；finish_reason=length" in diagnostic["reason"]
    assert diagnostic["latest_failure"]["response_integrity"]["finish_reason"] == "stop"


def test_actual_null_policy_values_display_unknown_without_backfilling_metadata(monkeypatch):
    planning = _planning()
    for receipt in (planning["response_integrity"], planning["primary_failure"]["response_integrity"]):
        receipt["resolved_output_policy"].update(requested_output_limit=None,
            wire_output_parameter_key=None, wire_output_parameter_value=None,
            server_effective_output_limit=None)
    stop = _stop(planning)
    text = managed_stop.explanation(stop)
    assert "本地请求输出上限=UNKNOWN" in text
    assert "请求参数 UNKNOWN=UNKNOWN" in text
    assert "服务端实际生效上限=UNKNOWN" in text
    assert "上限=None" not in text
    assert stop["output_policy"]["requested_output_limit"] is None
    assert stop["output_policy"]["server_effective_output_limit"] is None
    rendered = []
    monkeypatch.setattr(bio_agents.output, "print_once", lambda key, text, *args, **kwargs: rendered.append(text))
    agent = SimpleNamespace(planner_progress_enabled=True)
    bio_agents.BioAnalystAgent._planner_progress(agent, "isolated-progress", "attempt_completed",
        {"response_integrity": planning["response_integrity"]})
    bio_agents.BioAnalystAgent._planner_progress(agent, "isolated-progress", "length_remediation",
        {"attempted_remediation": {"kind": "compact_json", "output_policy": stop["output_policy"]}})
    assert len(rendered) == 2
    assert all("本地请求输出上限=UNKNOWN" in item and "上限=None" not in item for item in rendered)


def test_emergency_projection_keeps_typed_origin_latest_and_known_skipped_reproduce():
    planning = _planning(latest="stop")
    held = deepcopy(planning)
    values = {"managed_run_boundary": {"node": "planner", "outcome": {"type": "terminal",
        "payload": {"status": "blocked", "planning_terminal": planning}}}}
    result = managed_stop.emergency_projection(values, TypeError("offline projection failure"))
    diagnostic = result["terminal_diagnostic"]
    assert result["status"] == "blocked"
    assert diagnostic["planning"] == held
    assert diagnostic["summary_llm_calls"] == 0
    assert "first-call" in diagnostic["reason"]
    assert "finish_reason=length" in diagnostic["reason"]
    assert "finish_reason=stop" in diagnostic["reason"]
    assert diagnostic["report_receipts"]["reproduce.sh"] == {"status": "skipped", "reason": "NO_EXECUTION_ATTEMPT"}
    assert diagnostic["secondary_causes"][0]["error_code"] == "MANAGED_STOP_PROJECTION_FAILED"


@pytest.mark.parametrize("finish,expected,forbidden", [
    ("length", "接收结束；输出触及长度边界，本次候选未接纳", "响应完整"),
    ("stop", "接收结束，待解析与校验", "本次候选未接纳"),
    ("UNKNOWN", "接收结束，待解析与校验", "输出触及长度边界"),
])
def test_progress_separates_transport_end_and_candidate_acceptance(monkeypatch, finish, expected, forbidden):
    rendered, observed = [], []
    monkeypatch.setattr(bio_agents.output, "print_once", lambda key, text, *args, **kwargs: rendered.append(text))
    agent = SimpleNamespace(planner_progress_enabled=True,
        planner_progress_callback=lambda event, row: observed.append((event, row)))
    receipt = _receipt("ui-call", finish)
    bio_agents.BioAnalystAgent._planner_progress(agent, "isolated-progress", "attempt_completed",
        {"candidate_ordinal": 1, "elapsed_seconds": 2.5, "response_integrity": receipt})
    assert len(rendered) == 1
    assert expected in rendered[0]
    assert forbidden not in rendered[0]
    assert observed[0][1]["response_integrity"] == receipt
    if finish == "length":
        assert "8192" in rendered[0] and "16384" in rendered[0]
        assert "resolved_not_wire_observed" in rendered[0]


def test_progress_remediation_and_stop_display_recorded_strategy_and_independent_stop(monkeypatch):
    rendered = []
    monkeypatch.setattr(bio_agents.output, "print_once", lambda key, text, *args, **kwargs: rendered.append(text))
    agent = SimpleNamespace(planner_progress_enabled=True)
    bio_agents.BioAnalystAgent._planner_progress(agent, "isolated-progress", "length_remediation",
        {"attempted_remediation": {"kind": "compact_only", "status": "claimed", "output_policy": {"requested_output_limit": 16384}}})
    bio_agents.BioAnalystAgent._planner_progress(agent, "isolated-progress", "planning_stopped",
        {"error_code": "PLANNER_LENGTH_RECOVERY_UNAVAILABLE"})
    assert "compact_only" in rendered[0] and "claimed" in rendered[0]
    assert "仍须通过解析与校验" in rendered[0]
    assert "PLANNER_LENGTH_RECOVERY_UNAVAILABLE" in rendered[1]
    assert not any("retry exhausted" in row for row in rendered)

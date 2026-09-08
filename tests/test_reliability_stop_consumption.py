"""Stop consumption through the existing graph, outer, provider, and stores.

The imported integration harness was reviewed at HEAD
2d42ddd0687df2fe9aa1f1f1eeb113e46af463f5. Only the summary model response is
replaced here; Recovery, causal publication, readers, typed routing, durable
claims, the actual summary caller, and report writers remain production code.
All process/evidence fixtures and artifacts are confined to pytest tmp_path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.workflow.managed_stop import current_managed_stop
from tests.test_common_finalization_workflow import run_execution_case


# Exact, already recorded provider content; tests never reopen its source run.
# Source: session_1788877967 / terminal_summary-0014-6186d773b18b / response.json.
# Its prose is historical/unverified and must not become this fixture's facts.
RECORDED_SUMMARY = (
    '{"explanation":"自动执行与恢复已停止。原始失败为确定性验证拒绝：外层命令返回0'
    '但内层流水线段失败，证据JSON报告no_reads_found指向S1_1.fq.gz路径。恢复阶段发生4次'
    '检查失败（引用未声明、二进制读取禁止、引用类型不匹配）。停止原因为连续2轮无进展'
    '（共4轮模型轮次、10次工具调用）。preflight_inputs步骤执行成功但验证待定且未提交；'
    '其余17个阶段均处于pending。脚本stdout未经独立复核，不能作为数据结论。文件不存在'
    '或读取失败均未经证实。","fact_ids":["origin","stop_reason"]}'
)
NEGATED_PROSE = "文件不存在或数据不兼容并未得到证实；不能声称任务已完成或验证通过。"
FORGED_PROSE = "任务已完成，验证通过，所有步骤成功；已生成最终科学产物。"


class SummaryResponseModel:
    def __init__(self, mode: str):
        self.mode = mode
        self.requests = []
        self.frozen_facts = []
        self.response_content = ""

    async def ainvoke(self, messages, config=None):
        self.requests.append(list(messages))
        assert [message.type for message in messages] == ["system", "human"]
        assert not any(getattr(message, "tool_calls", None) for message in messages)
        facts = json.loads(messages[1].content)
        self.frozen_facts.append(facts)
        assert facts["origin"]["failure_type"] == "execution_failed"
        assert facts["execution"]["returncode"] == 1
        assert facts["stop_reason"]["error_code"] == "RECOVERY_NO_PROGRESS_LIMIT"
        assert any(row["tool_call_id"] == "read-2" for row in facts["inspection_failures"])
        if self.mode == "provider_failure":
            raise ConnectionError("synthetic summary connection failure")
        if self.mode == "parse_failure":
            self.response_content = '{"explanation":'
        elif self.mode == "unknown_fact":
            self.response_content = json.dumps({"fact_ids": ["not_a_current_frozen_fact"]})
        elif self.mode == "selection":
            self.response_content = json.dumps({"fact_ids": ["origin", "stop_reason"]})
        elif self.mode == "recorded_negation":
            self.response_content = RECORDED_SUMMARY
        else:
            self.response_content = json.dumps({
                "explanation": NEGATED_PROSE if self.mode == "negation" else FORGED_PROSE,
                "fact_ids": ["origin", "stop_reason"],
            }, ensure_ascii=False)
        return AIMessage(content=self.response_content)


async def _run(tmp_path: Path, mode: str):
    model = SummaryResponseModel(mode)

    def inject(agent, _fixture):
        agent.terminal_summary_llm = model

    fixture, agent, graph, config, recovery, unused_summary, result, generates = (
        await run_execution_case(tmp_path, inject=inject)
    )
    assert not unused_summary.requests
    stop = current_managed_stop(fixture.reload())
    assert stop, result
    assert len(model.requests) == 1
    assert stop["summary_llm_calls"] == 1
    assert stop["summary_attempted"] is True
    assert stop["origin"]["failure_type"] == "execution_failed"
    assert stop["execution"]["returncode"] == 1
    assert not stop["execution"]["committed"]
    assert stop["stop_reason"]["error_code"] == "RECOVERY_NO_PROGRESS_LIMIT"
    assert stop["stop_reason"]["no_progress_rounds"] == 2
    assert len(recovery.requests) == 3
    assert len(generates) == 1
    assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
    assert stop["inspection_failures"][-1]["tool_call_id"] == "read-2"
    assert "CONTIG_MISMATCH_REASON" in result.reason
    assert "read-2" in result.reason
    assert result.failure_code == "RECOVERY_NO_PROGRESS_LIMIT"
    report = Path(stop["report_paths"]["report.md"]).read_text()
    assert "read-2" in report and "returncode=1" in report
    assert set(stop["report_paths"]) == {"report.md", "stage_status.tsv", "reproduce.sh"}
    for filename, path in stop["report_paths"].items():
        receipt = stop["report_receipts"][filename]
        assert receipt["status"] == "created"
        assert Path(path).is_file()
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == receipt["sha256"]
    # Reenter the real outer finalizer with the same completed graph checkpoint.
    await agent._finalize_managed_terminal(fixture.task_id, graph.get_state(config))
    assert len(model.requests) == 1
    assert current_managed_stop(fixture.reload())["stop_id"] == stop["stop_id"]
    return fixture, graph, config, result, stop, report, model


def test_no_progress_is_typed_unresolved_stop_not_internal_protocol_error(tmp_path):
    async def scenario():
        fixture, graph, config, result, stop, _report, _model = await _run(tmp_path, "negation")
        outcome = graph.get_state(config).values["recovery_outcome"]
        assert outcome["type"] == "blocked", outcome
        assert outcome["payload"]["reason_code"] == "RECOVERY_NO_PROGRESS_LIMIT"
        held = outcome["payload"]["inspection_stop"]
        assert held["resolution"] == "unresolved"
        assert stop["resolution"] == "unresolved"
        assert stop["status"] == result.status == "blocked"
        assert stop["latest_failure"]["tool_call_id"] == "read-2"
        assert not outcome["payload"].get("identity_conflict")
        assert fixture.reload()["task_state"]["managed_run_stop"]["stop_reason"] == stop["stop_reason"]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["negation", "recorded_negation", "selection"])
def test_legal_summary_selection_and_negation_are_not_provider_failures(tmp_path, mode):
    async def scenario():
        _fixture, _graph, _config, _result, stop, report, model = await _run(tmp_path, mode)
        assert stop["summary_status"] == "COMPLETED", stop["secondary_failures"]
        assert stop["summary_consumption_status"] == "GROUNDED_FACT_SELECTION"
        assert stop["summary_selected_fact_ids"] == ["origin", "stop_reason"]
        expected_prose = json.loads(model.response_content).get("explanation", "")
        assert stop.get("summary_unverified_prose", "") == expected_prose
        assert not any(row["stage"] in {"summary.provider", "summary.parse", "summary.facts"}
                       for row in stop["secondary_failures"])
        assert "SUMMARY_UNSUPPORTED_CLAIM" not in json.dumps(stop)
        if mode == "recorded_negation":
            assert len(model.response_content) == 288
            assert hashlib.sha256(model.response_content.encode()).hexdigest() == (
                "8633b8f1388751d87e5db3b0a22529df64e579a2736891a0421a052ba1b2308f"
            )
            # The old prose mentions a different step, counts, and rc. Parsing
            # it is legal; adopting it as this run's facts would be incorrect.
            assert "preflight_inputs步骤执行成功" not in report
            assert "外层命令返回0" not in report
        assert NEGATED_PROSE not in report
        assert FORGED_PROSE not in report

    asyncio.run(scenario())


def test_free_positive_claim_is_not_adopted_as_a_formal_fact(tmp_path):
    async def scenario():
        _fixture, _graph, _config, result, stop, report, _model = await _run(tmp_path, "forged")
        # A fact ID does not validate unrestricted prose. The legacy object may
        # parse and select grounded facts, but its prose cannot override them.
        assert stop["summary_status"] == "COMPLETED"
        assert stop["summary_consumption_status"] == "GROUNDED_FACT_SELECTION"
        assert stop["summary_unverified_prose"] == FORGED_PROSE
        assert stop["summary_selected_fact_ids"] == ["origin", "stop_reason"]
        assert FORGED_PROSE not in stop.get("summary_text", "")
        assert FORGED_PROSE not in result.reason and FORGED_PROSE not in report
        assert stop["execution"]["committed"] is False
        assert "summary.provider" not in {row["stage"] for row in stop["secondary_failures"]}

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,stage,code", [
    ("provider_failure", "summary.provider", "SUMMARY_FAILED"),
    ("parse_failure", "summary.parse", "SUMMARY_RESPONSE_INVALID"),
    ("unknown_fact", "summary.facts", "SUMMARY_FACT_CONSUMPTION_FAILED"),
])
def test_summary_failure_keeps_its_actual_consumption_stage(tmp_path, mode, stage, code):
    async def scenario():
        _fixture, _graph, _config, result, stop, report, _model = await _run(tmp_path, mode)
        assert stop["summary_status"] == "FAILED"
        errors = [row for row in stop["secondary_failures"] if row["stage"].startswith("summary.")]
        assert len(errors) == 1, errors
        assert errors[0]["stage"] == stage
        assert errors[0]["error_code"] == code
        assert errors[0].get("exception_type")
        assert errors[0].get("detail"), errors
        assert stop["stop_reason"]["error_code"] == "RECOVERY_NO_PROGRESS_LIMIT"
        assert "read-2" in result.reason and "read-2" in report
        assert not stop.get("summary_text")

    asyncio.run(scenario())

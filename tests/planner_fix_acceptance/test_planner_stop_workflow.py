"""Actual Planner non-progress stop through typed graph, outer, summary and reports.

Run using the guarded offline candidate bootstrap. The production go, graph,
storage and outer loop are real. Only models and deliberate failing I/O are
substituted; faults are installed after the actual graph has stopped.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

import bio_agents
from agent_core import plan_tracker
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow import managed_stop
from execution.profiling import RunProfiler
from tests.planner_fix_acceptance.test_planner_recorded_workflow import Harness, RecordedModel, F


class PlannerSummaryModel:
    def __init__(self, mode="success"):
        self.mode, self.calls = mode, []

    async def ainvoke(self, messages, config=None):
        self.calls.append(deepcopy(messages))
        assert len(messages) == 2
        assert not any(getattr(m, "tool_calls", None) for m in messages)
        facts = json.loads(messages[1].content)
        assert facts["stop_reason"]["error_code"] == "PLANNER_NON_PROGRESS_REPLAY"
        assert facts["planning"]["no_approved_plan"] is True
        assert facts["execution"]["attempt_id"] == ""
        if self.mode == "provider":
            raise ConnectionError("offline summary deliberately unavailable")
        if self.mode == "timeout":
            await asyncio.sleep(10)
        return AIMessage(content=json.dumps({
            "explanation": "规划因相同候选的契约拒绝而停止，尚无批准计划，本轮没有启动科学步骤。",
            "fact_ids": ["origin", "stop_reason", "planning", "execution"],
        }, ensure_ascii=False))


def _evidence(name, value):
    root = Path(os.environ["BIOMIND_OFFLINE_RESULTS"]).parent / "planner-stop-evidence"
    root.mkdir(exist_ok=True)
    (root / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return root


async def run_planner_stop(tmp_path, mode="success", *, transition=None):
    raw = (F / "original_response.txt").read_text()
    responses = [raw] * 8
    if mode == "format_variation":
        candidate = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        for index in range(1, 8):
            variant = deepcopy(candidate)
            variant["steps"][0]["title"] += " (presentation revision " + str(index) + ")"
            responses[index] = json.dumps(variant, ensure_ascii=False, indent=index)
    model = RecordedModel(responses)
    harness = Harness(tmp_path, model)
    agent = harness.agent
    task_id = "offline-planner-stop-" + mode
    state = harness.bootstrap(task_id)
    summary = PlannerSummaryModel(mode)
    agent.terminal_summary_llm = summary
    agent.terminal_summary_policy = {
        "enabled": mode != "disabled", "timeout_seconds": .05 if mode == "timeout" else 1,
        **({"remaining_tokens": 0} if mode == "budget" else {}),
    }
    agent.profiler = RunProfiler(tmp_path / "task_outputs")
    agent.profiler.start_task(task_id)
    agent.state_manager = None
    before_stop = {}
    fault_hits = {name: 0 for name in ("reader", "store", "writer", "serialize", "load")}
    real_go = agent.go

    async def go(*args, **kwargs):
        result = await real_go(*args, **kwargs)
        snapshot = agent._app_for_task(task_id).get_state({"configurable": {"thread_id": task_id}})
        outcome = snapshot.values["planner_outcome"]
        before_stop.update(load_plan_state(tmp_path, task_id))
        assert outcome["type"] == "harness_error", outcome
        assert outcome["payload"]["reason_code"] == "PLANNER_NON_PROGRESS_REPLAY"
        # Inject failures only after the real graph produced its legitimate
        # terminal. No correct outcome or scope is manufactured by this test.
        if mode == "reader":
            original = StateContractStore.read_object
            def fail(store, ref, *args, **kwargs):
                if kwargs.get("expected_type") == "planner_correction_context":
                    fault_hits["reader"] += 1
                    raise OSError("offline terminal context reader failure")
                return original(store, ref, *args, **kwargs)
            harness.stack.enter_context(patch.object(StateContractStore, "read_object", fail))
        if mode == "store":
            original = plan_tracker._atomic_write_plan_state
            def fail(path, value, **kwargs):
                if value.get("task_state", {}).get("managed_run_stop"):
                    fault_hits["store"] += 1
                    raise OSError("offline stop store failure")
                return original(path, value, **kwargs)
            harness.stack.enter_context(patch.object(plan_tracker, "_atomic_write_plan_state", fail))
        if mode == "writer":
            def fail(*args, **kwargs):
                fault_hits["writer"] += 1
                raise PermissionError("offline report writer failure")
            harness.stack.enter_context(patch.object(managed_stop, "_write_report", fail))
        if mode == "serialize":
            def fail(*args, **kwargs):
                fault_hits["serialize"] += 1
                raise TypeError("offline projection failure")
            harness.stack.enter_context(patch.object(managed_stop, "build_managed_stop", fail))
        if mode == "load":
            original = Path.read_text
            expected_path = plan_tracker.plan_state_path(tmp_path, task_id)
            def fail(path, *args, **kwargs):
                if path == expected_path:
                    fault_hits["load"] += 1
                    raise OSError("offline terminal durable read failure")
                return original(path, *args, **kwargs)
            harness.stack.enter_context(patch.object(Path, "read_text", fail))
        if transition:
            transition(harness, task_id)
        return result
    agent.go = go
    try:
        result = await agent._run_agent_interrupt_loop(state["task_spec"]["goal"], task_id, resume_initial=True)
        snapshot = agent._app_for_task(task_id).get_state({"configurable": {"thread_id": task_id}})
        diagnostic = getattr(agent, "_managed_terminal_results", {}).get(task_id, {}).get("terminal_diagnostic", {})
        durable = load_plan_state(tmp_path, task_id) if mode != "load" else {}
        stop = managed_stop.current_managed_stop(durable)
        record = {"mode": mode, "source": bio_agents.__file__, "result": result.__dict__,
            "typed_outcome": snapshot.values.get("planner_outcome"), "stop": stop,
            "diagnostic": diagnostic, "before_stop": before_stop,
            "provider_calls": len(model.calls), "summary_calls": len(summary.calls), "fault_hits": fault_hits,
            "summary_messages": [[m.content for m in messages] for messages in summary.calls],
            "profiler": agent.profiler.snapshot(task_id)}
        delivery = _evidence(mode, record)
        for name, filename in diagnostic.get("report_paths", {}).items():
            body = Path(filename).read_bytes()
            (delivery / (mode + "-" + name)).write_bytes(body)
        if mode in fault_hits:
            assert fault_hits[mode] > 0, (mode, fault_hits, diagnostic)
            expected_code = {
                "reader": "PLANNER_CONTEXT_DETAILS_UNAVAILABLE", "store": "MANAGED_STOP_PERSIST_FAILED",
                "writer": "REPORT_WRITE_FAILED", "serialize": "MANAGED_STOP_PROJECTION_FAILED",
                "load": "MANAGED_STOP_LOAD_FAILED",
            }[mode]
            assert any(item["error_code"] == expected_code for item in diagnostic["secondary_causes"]), diagnostic
        return result, diagnostic, stop, before_stop, model, summary, agent
    finally:
        await harness.close()


@pytest.mark.parametrize("mode", ["success", "provider", "timeout", "disabled", "budget", "reader", "store", "writer", "serialize", "load", "format_variation"])
def test_real_planner_nonprogress_stop_actual_outer(tmp_path, mode):
    async def scenario():
        result, diagnostic, stop, before, model, summary, agent = await run_planner_stop(tmp_path, mode)
        assert len(model.calls) == 2
        assert before["task_state"]["planning_epoch"]["planner_calls_total"] == 2
        assert not before.get("steps") and not before.get("current_plan", {}).get("plan_snapshot_ref")
        assert result.status == "failed", result
        assert result.failure_code == "PLANNER_NON_PROGRESS_REPLAY", result
        assert result.stage == "planner"
        assert diagnostic["execution"]["attempt_id"] == ""
        assert diagnostic["execution"]["step_id"] == ""
        assert diagnostic["execution"]["executed"] is False
        assert diagnostic["execution"]["validation_status"] == "not_performed"
        assert diagnostic["execution"]["committed"] is False
        assert diagnostic["planning"]["no_approved_plan"] is True
        failure = before["task_state"]["planning_failure"]
        assert diagnostic["validation_issues"] == failure["validation_issues"]
        assert diagnostic["primary_cause"]["validation_issues"] == failure["primary_failure"]["validation_issues"]
        assert diagnostic["primary_cause"]["source"]["candidate_source"]["candidate_ordinal"] == 1
        assert diagnostic["latest_failure"]["candidate_source"]["candidate_ordinal"] == 2
        assert diagnostic["scope"]["planning_epoch_id"] == failure["planning_epoch_id"]
        assert diagnostic["scope"]["task_revision"] == before["current_task"]["task_revision"]
        assert "inspection_required" not in diagnostic["execution"].values()
        assert "尚未形成批准 Plan" in result.reason
        expected_calls = 0 if mode in {"disabled", "budget", "store", "serialize", "load"} else 1
        assert len(summary.calls) == expected_calls
        assert diagnostic["summary_llm_calls"] == expected_calls
        expected = "SKIPPED" if not expected_calls else "FAILED" if mode in {"provider", "timeout"} else "COMPLETED"
        assert diagnostic["summary_status"] == expected
        if mode in {"store", "serialize", "load"}:
            assert diagnostic["persistence_status"] == "unconfirmed"
            assert not diagnostic["report_paths"]
            assert not stop
        else:
            assert stop["scope"]["step_id"] == ""
            assert stop["execution"]["returncode"] is None
            if mode == "writer":
                assert stop["report_receipts"]["report.md"]["status"] == "failed"
                assert not stop["report_paths"]
            else:
                assert stop["report_receipts"]["report.md"]["status"] == "created"
                assert stop["report_receipts"]["stage_status.tsv"]["status"] == "created"
                assert stop["report_receipts"]["reproduce.sh"] == {"status": "skipped", "reason": "NO_EXECUTION_ATTEMPT"}
                assert not (tmp_path / "task_outputs" / before["task_id"] / "reproduce.sh").exists()
            if mode == "reader":
                assert stop["planning"]["context_read_status"] == "unavailable"
                assert any(x["error_code"] == "PLANNER_CONTEXT_DETAILS_UNAVAILABLE" for x in stop["secondary_failures"])
            assert agent.profiler.snapshot(before["task_id"])["status"] == "failed"
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["epoch", "task_revision", "cancelled"])
def test_actual_planner_stop_loses_authority_after_fresh_transition(tmp_path, change):
    async def scenario():
        def transition(harness, task_id):
            current = load_plan_state(tmp_path, task_id)
            if change == "epoch":
                current["task_state"]["planning_epoch"]["planning_epoch_id"] = "new-explicit-epoch"
                current["task_state"]["planning_epoch"]["status"] = "active"
            elif change == "task_revision":
                # A real authoritative TaskSpec update needs its own commit
                # API; this case is implemented below by the actor helper.
                from agent_core.task_spec import task_semantic_hash
                task = deepcopy(current["current_task"]["task_spec"])
                task["goal"] += " Additional explicitly submitted requirement."
                task["revision"] += 1
                plan_tracker.commit_consultant_candidate_state(tmp_path, task_id, {"task_spec": task})
                return
            else:
                plan_tracker.set_task_status(current, "cancelled", "independent cancellation wins")
            save_plan_state(tmp_path, task_id, current)
        result, diagnostic, stop, before, model, summary, agent = await run_planner_stop(tmp_path, "success", transition=transition)
        assert len(model.calls) == 2
        assert not summary.calls and not stop
        assert result.status == ("cancelled" if change == "cancelled" else "stale_result")
        assert not (tmp_path / "task_outputs" / before["task_id"] / "report.md").exists()
    asyncio.run(scenario())

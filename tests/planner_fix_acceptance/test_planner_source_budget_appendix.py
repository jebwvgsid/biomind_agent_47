"""Source-budget contrast, deliberately distinct from natural typed graph retry.

The real source accumulates four refusals; only then does the real graph resume.
This is NOT evidence that natural typed routing allows four repeated candidates:
the workflow test establishes its earlier PLANNER_NON_PROGRESS_REPLAY guard.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

from langchain_core.messages import AIMessage

from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.managed_stop import current_managed_stop
from tests.planner_fix_acceptance.test_planner_recorded_workflow import Harness, RecordedModel, F
from tests.planner_fix_acceptance.test_planner_stop_workflow import _evidence


class SourceBudgetSummary:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append(deepcopy(messages))
        assert len(messages) == 2
        assert not any(getattr(message, "tool_calls", None) for message in messages)
        facts = json.loads(messages[1].content)
        assert facts["stop_reason"]["error_code"] == "PLANNING_EPOCH_EXHAUSTED"
        assert facts["planning"]["no_approved_plan"] is True
        assert facts["execution"]["executed"] is False
        return AIMessage(content=json.dumps({
            "explanation": "规划候选未通过契约检查，现有规划预算停止本轮；尚无批准计划，未执行科学步骤。",
            "fact_ids": ["origin", "stop_reason", "planning", "execution"],
        }, ensure_ascii=False))


def test_real_source_accumulation_then_graph_budget_stop_is_only_a_contrast(tmp_path):
    async def scenario():
        raw = (F / "original_response.txt").read_text()
        model = RecordedModel([raw] * 4)
        harness = Harness(tmp_path, model)
        agent = harness.agent
        task_id = "offline-planner-source-budget-appendix"
        summary = SourceBudgetSummary()
        agent.terminal_summary_llm = summary
        agent.terminal_summary_policy = {
            "enabled": True, "provider_available": True, "requests_allowed": True,
            "remaining_tokens": 100000, "timeout_seconds": 1,
        }
        observations = []
        try:
            initial = harness.bootstrap(task_id)
            for ordinal in range(1, 5):
                # This bypasses typed scheduling explicitly, not admission,
                # parsing, normalization, storage, or any call-count policy.
                update = await agent._schema2_early_sources["planner"](harness.input(task_id))
                durable = load_plan_state(tmp_path, task_id)
                task = durable["task_state"]
                assert len(model.calls) == ordinal
                assert task["planning_epoch"]["planner_calls_total"] == ordinal
                assert task["planning_failure"]["status"] == "unresolved"
                assert not durable.get("steps")
                observations.append({"ordinal": ordinal, "update": update,
                    "epoch": deepcopy(task["planning_epoch"]), "failure": deepcopy(task["planning_failure"])})
            # No terminal, scope, counter or valid Plan is supplied to this
            # real go/typed graph/outer resume. The actual source chooses stop.
            result = await agent._run_agent_interrupt_loop(initial["task_spec"]["goal"], task_id, resume_initial=True)
            snapshot = agent._app_for_task(task_id).get_state({"configurable": {"thread_id": task_id}})
            durable = load_plan_state(tmp_path, task_id)
            stop = current_managed_stop(durable)
            _evidence("source-budget-appendix", {
                "scope": "real source accumulation, then real graph resume; not natural graph retries",
                "source_observations": observations, "outcome": snapshot.values.get("planner_outcome"),
                "result": result.__dict__, "stop": stop, "provider_calls": len(model.calls),
                "summary_messages": [[message.content for message in messages] for messages in summary.calls],
                "profiler": agent.profiler.snapshot(task_id),
            })
            assert len(model.calls) == 4
            assert result.status == "blocked", result
            assert result.failure_code == "PLANNING_EPOCH_EXHAUSTED"
            assert result.stage == "planner"
            assert snapshot.values["planner_outcome"]["type"] == "terminal"
            assert snapshot.values["planner_outcome"]["payload"]["status"] == "blocked"
            assert durable["task_state"]["planning_epoch"]["status"] == "exhausted"
            assert durable["task_state"]["planning_failure"]["status"] == "terminal"
            assert stop["scope"]["task_revision"] == durable["current_task"]["task_revision"]
            assert stop["scope"]["planning_epoch_id"] == observations[-1]["epoch"]["planning_epoch_id"]
            assert stop["primary_cause"]["source"]["candidate_source"]["candidate_ordinal"] == 1
            assert stop["latest_failure"]["candidate_source"]["candidate_ordinal"] == 4
            assert stop["planning"]["validation_issues"] == observations[-1]["failure"]["validation_issues"]
            assert stop["stop_reason"]["error_code"] == "PLANNING_EPOCH_EXHAUSTED"
            assert stop["stop_reason"]["model_turns"] == 4
            assert stop["execution"]["executed"] is False
            assert stop["execution"]["attempt_id"] == ""
            assert len(summary.calls) == stop["summary_llm_calls"] == 1
            assert stop["summary_status"] == "COMPLETED"
            assert stop["report_receipts"]["report.md"]["status"] == "created"
            assert stop["report_receipts"]["stage_status.tsv"]["status"] == "created"
            assert stop["report_receipts"]["reproduce.sh"] == {"status": "skipped", "reason": "NO_EXECUTION_ATTEMPT"}
            assert agent.profiler.snapshot(task_id)["status"] == "blocked"
        finally:
            await harness.close()

    asyncio.run(scenario())

"""Recorded READ removal through real Planner requests, durable reload and graph.

Only model and human IO are substitutes. Existing repeat-stop limits remain active. Both initial rejections use the actual
TypedPlanner and producer at its durable node boundary. A new agent then runs
the real schema-2 graph to PlanReview, without publishing a prebuilt Plan or
altering a retry budget. All recordings come from committed/copied fixtures;
the original session and scientific inputs are never opened.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import bio_agents
import pytest
from agent_core.plan_contract import parse_planner_outcome_envelope
from agent_core.plan_tracker import load_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow.early_nodes import TypedPlanner
from tests.planner_fix_acceptance import test_planner_recorded_workflow as shared
from tests.planner_truncation_acceptance.harness import Model
from tests.test_reliability_planner_consumption import _BASIS, _recording


def _context(root, task, durable):
    failure = durable["task_state"]["planning_failure"]
    return StateContractStore(root, task).read_object(
        failure["context_ref"], expected_type="planner_correction_context",
    )


def _feedback_in_request(context, request):
    """Check the complete stored feedback, not a code or a prompt token count."""
    objects = list(shared.wire_objects(request))
    assert context["feedback"] in objects
    for issue in context["feedback"]["validation_issues"]:
        assert issue in objects, issue
    assert context["raw_candidate"] in [message.get("content") for message in request]


def _original_baseline(context, original_raw, original_issues):
    baselines = context["consumption_review_baselines"]
    matches = [row for row in baselines if row["raw_candidate"] == original_raw]
    assert len(matches) == 1, baselines
    assert matches[0]["validation_issues"] == original_issues
    # The original body must still declare the content dependency. A newer
    # rejected candidate that omitted it cannot replace this semantic basis.
    original = parse_planner_outcome_envelope(matches[0]["raw_candidate"]).plan_candidate_body()
    assert "plan.bam_list" in original["steps"][16]["inputs"]


def _write_receipt(value):
    root = Path(os.environ["BIOMIND_OFFLINE_RESULTS"]).parent / "consumption-workflow-evidence"
    root.mkdir(exist_ok=True)
    (root / ("real-producer-reload-" + str(value["result"]["planner_outcome"]["type"]) + ".json")).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )


@pytest.mark.parametrize("repeat_without_progress", [False, True])
def test_recorded_read_removal_feedback_reaches_next_request_and_new_instance(tmp_path, repeat_without_progress):
    async def scenario():
        recorded = _recording()
        original_raw = recorded["candidate1_raw"]
        deletion_raw = recorded["candidate2_raw"]
        correction = deepcopy(dict(parse_planner_outcome_envelope(deletion_raw).payload))
        correction["steps"][16]["success_criteria"].append(_BASIS)
        correction_raw = json.dumps(correction, ensure_ascii=False)
        task = "offline-consumption-feedback-reload"

        # Bootstrap the recorded TaskSpec through the existing real Consultant
        # acceptance/store seam. The fixture supplies only a completed handoff,
        # never an approved Plan, an execution receipt or a fabricated output.
        fixture_dir = tmp_path / "recorded-task-fixture"
        fixture_dir.mkdir()
        fixture = json.loads((shared.F / "recorded_state.json").read_text())
        fixture["current_task"]["task_spec"] = deepcopy(recorded["task_spec"])
        (fixture_dir / "recorded_state.json").write_text(
            json.dumps(fixture, ensure_ascii=False), encoding="utf-8",
        )

        first = shared.Harness(tmp_path, Model([original_raw, deletion_raw]))
        before = {}
        try:
            with patch.object(shared, "F", fixture_dir):
                initial = first.bootstrap(task)
            adapter = TypedPlanner(
                data_path=str(tmp_path), source=first.agent._schema2_early_sources["planner"],
            )
            first_update = await adapter.run(initial)
            first_state = load_plan_state(tmp_path, task)
            first_context = _context(tmp_path, task, first_state)
            first_issues = first_context["feedback"]["validation_issues"]
            assert first_update["planner_outcome"]["type"] == "planning_continues", first_update
            assert len(first.model.calls) == 1
            assert len(first_issues) == 1, first_issues
            assert first_issues[0]["code"] == "PLAN_ARTIFACT_UNAVAILABLE"
            assert first_issues[0]["consumer_step_id"] == "write_reproduce_script"
            assert first_issues[0]["resource_id"] == "plan.bam_list"
            _original_baseline(first_context, original_raw, first_issues)

            second_update = await adapter.run(first.input(task))
            before = load_plan_state(tmp_path, task)
            second_context = _context(tmp_path, task, before)
            second_issues = second_context["feedback"]["validation_issues"]
            assert second_update["planner_outcome"]["type"] == "planning_continues", second_update
            assert len(first.model.calls) == 2
            _feedback_in_request(first_context, first.model.calls[1])
            assert [issue["code"] for issue in second_issues] == ["PLAN_CONSUMPTION_CHANGE_REVIEW_REQUIRED"]
            assert second_issues[0]["consumer_step_id"] == "write_reproduce_script"
            assert second_issues[0]["resource_id"] == "plan.bam_list"
            assert second_issues[0]["field_path"].startswith("$.steps[16].")
            assert second_issues[0]["actual"] and second_issues[0]["expected"]
            _original_baseline(second_context, original_raw, first_issues)
            assert second_context["raw_candidate"] == deletion_raw
            assert before["task_state"]["planning_epoch"]["planner_calls_total"] == 2
            assert not before.get("steps")
            first_requests = deepcopy(first.model.calls)
        finally:
            await first.close()

        # New instance, same durable epoch. A corrected third candidate may
        # reach review; the repeated rejection obeys the existing independent
        # repeat-stop policy. No fourth request is manufactured past that stop.
        model = Model([deletion_raw if repeat_without_progress else correction_raw])
        second = shared.Harness(tmp_path, model)
        provider_states = []
        provider_contexts = []

        def observe_provider():
            durable = load_plan_state(tmp_path, task)
            provider_states.append(deepcopy(durable))
            provider_contexts.append(_context(tmp_path, task, durable))

        model.observe = observe_provider
        try:
            result = await second.run(second.input(task))
            after = load_plan_state(tmp_path, task)
            _write_receipt({
                "source": bio_agents.__file__,
                "fixture_raw_sha256": {
                    "original": hashlib.sha256(original_raw.encode()).hexdigest(),
                    "deleted_read": hashlib.sha256(deletion_raw.encode()).hexdigest(),
                },
                "first_update": first_update, "second_update": second_update,
                "first_context": first_context, "second_context": second_context,
                "first_requests": first_requests, "before_restart": before,
                "reloaded_requests": model.calls, "provider_states": provider_states,
                "provider_contexts": provider_contexts, "result": result, "after": after,
            })
            assert len(model.calls) == 1, result
            assert len(provider_contexts) == 1
            _feedback_in_request(second_context, model.calls[0])
            _feedback_in_request(first_context, model.calls[0])
            _original_baseline(provider_contexts[0], original_raw, first_issues)
            prefix_count = first_context["fixed_prefix_count"]
            for request in [*first_requests[1:], *model.calls]:
                assert request[:prefix_count] == first_requests[0][:prefix_count]
            before_epoch = before["task_state"]["planning_epoch"]
            after_epoch = after["task_state"]["planning_epoch"]
            assert after_epoch["planning_epoch_id"] == before_epoch["planning_epoch_id"]
            assert after_epoch["planner_calls_total"] == 3
            assert after["task_state"].get("budgets") == before["task_state"].get("budgets")
            assert not (after.get("provenance_state") or {}).get("attempts")
            assert not list(tmp_path.rglob("execution_result.json"))
            if repeat_without_progress:
                assert result["planner_outcome"]["type"] == "terminal", result
                assert result["planner_outcome"]["payload"]["reason_code"] == "PLANNER_REPEATED_FAILURE"
                final_context = _context(tmp_path, task, after)
                _original_baseline(final_context, original_raw, first_issues)
                assert final_context["feedback"]["validation_issues"][0]["code"] == "PLAN_CONSUMPTION_CHANGE_REVIEW_REQUIRED"
                assert not after.get("steps")
            else:
                assert result["planner_outcome"]["type"] == "plan_review_ready", result
                assert result["plan_review_outcome"]["type"] == "approval_pending", result
                assert len(after["steps"]) == 18
                assert after["approval_status"] != "approved"
                assert after["plan_dataflow_receipt"]["status"] == "compiled"
                assert after["task_state"]["plan_review_submission"]
        finally:
            await second.close()

    asyncio.run(scenario())

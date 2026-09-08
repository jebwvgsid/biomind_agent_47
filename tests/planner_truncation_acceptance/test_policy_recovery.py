"""LEN policy revalidation at real durable rejection and provider boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import pytest

from langchain_core.messages import messages_from_dict

from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.planner_output_policy import PlannerOutputPolicyError, planner_input_admission
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow.early_nodes import TypedPlanner
from agent_core.workflow.phase_subgraph import PhaseProviderContext, PHASE_CONTEXT_RUNTIME_KEY
from tests.planner_truncation_acceptance.harness import (
    Harness, Model, complete_candidate, raw_length, receipt, recorded_length, response,
)
from tests.planner_truncation_acceptance.test_workflow import (
    assert_unapproved_unexecuted, first_rejection, length_calls,
)


def official_profile(harness):
    harness.agent.llm_model_id = "deepseek-v4-flash"
    harness.agent.llm_endpoint_id = "https://api.deepseek.com"
    harness.agent.llm_provider_id = "openai"


async def saved_increase(root, task):
    first = Harness(root, Model([recorded_length(1)]))
    official_profile(first)
    try:
        _, _, before = await first_rejection(first, task)
        failure = before["task_state"]["planning_failure"]
        authorization = failure["remediation_authorization"]
        assert authorization["kind"] == "increase_output_limit"
        assert authorization["output_policy"]["requested_output_limit"] == 32768
        context = StateContractStore(root, task).read_object(
            failure["context_ref"], expected_type="planner_correction_context",
        )
        return before, context, first.model.calls[0]
    finally:
        await first.close()


def selected_observation(request):
    events = [json.loads(message["content"]) for message in request
              if message.get("name") == "planner_length_strategy"]
    assert len(events) == 1, events
    assert events[0]["code"] == "PLANNER_LENGTH_REMEDIATION_SELECTED"
    return events[0]


def assert_full_request(harness, context, first_request):
    request = harness.model.calls[0]
    prefix = context["fixed_prefix_count"]
    assert request[:prefix] == first_request[:prefix]
    contents = [str(message.get("content", "")) for message in request]
    assert raw_length(1) in contents
    # The original durable feedback is retained verbatim alongside the new
    # current strategy observation; no corrected historical evidence is made.
    assert context["messages"][-1]["data"]["content"] in contents
    observation = selected_observation(request)
    assert observation["finish_reason"] == "length"
    assert observation["previous_request_output_limit"] == 16384
    assert observation["reported_usage"]["output_tokens"] == 8192
    assert observation["selected_remediation"]["kind"] == "compact_json"
    return observation


def test_saved_increase_falls_back_only_after_full_request_capacity_check(tmp_path):
    """A saved 32k proposal cannot force failure when full input fits at 16k."""
    async def scenario():
        task = "offline-length-capacity-revalidation"
        before, context, first_request = await saved_increase(tmp_path, task)
        second = Harness(tmp_path, Model([response(complete_candidate())]))
        official_profile(second)
        try:
            compactor = second.agent._schema2_phase_compactor
            retained = messages_from_dict(context["messages"])
            initial_policy = second.agent._resolve_planner_output_policy()
            retained_budget = planner_input_admission(
                retained, initial_policy, base_policy=compactor.policy, estimator=compactor.estimator,
            )
            # Synthetic, explicit current application capacity: between the
            # two output reserves with ample room for the strategy observation.
            current_capacity = retained_budget["estimated_input_tokens"] + 24576 + compactor.policy.estimation_margin_tokens
            compactor.policy = replace(compactor.policy, application_context_limit_tokens=current_capacity)
            second.agent.llm_context_limit_tokens = current_capacity
            second.watch_provider(task)
            result = await second.run(second.input(task))
            after = load_plan_state(tmp_path, task)
            receipt("policy-capacity-revalidation", {
                "source": __import__("bio_agents").__file__, "before": before,
                "context": context, "current_capacity": current_capacity,
                "retained_budget": retained_budget, "result": result, "after": after,
                "requests": second.model.calls, "binds": second.model.binds,
                "provider_states": second.provider_states, "manifests": second.manifests(task),
            })
            assert len(second.model.calls) == 1
            assert result["plan_review_outcome"]["type"] == "approval_pending", result
            observation = assert_full_request(second, context, first_request)
            assert observation["next_local_output_limit"] == 16384
            diagnostic = observation["rejected_increase_diagnostic"]
            assert diagnostic["code"] == "PLANNER_OUTPUT_CONTEXT_CAPACITY_INSUFFICIENT"
            assert diagnostic["actual"]["estimated_input_tokens"] > diagnostic["actual"]["safe_input_tokens"]
            authorized = second.provider_states[0]["task_state"]["planning_failure"]["remediation_authorization"]
            assert authorized["kind"] == "compact_json"
            assert authorized["input_admission"]["hard_block"] is False
            assert authorized["output_policy"]["requested_output_limit"] == 16384
            assert [item["extra_body"]["max_tokens"] for item in second.model.binds
                    if "max_tokens" in item.get("extra_body", {})] == [16384]
            assert length_calls(after) == 1
            assert after["task_state"]["planning_epoch"]["planner_calls_total"] == 2
            assert after["task_state"]["planning_epoch"]["planning_epoch_id"] == before["task_state"]["planning_epoch"]["planning_epoch_id"]
            assert_unapproved_unexecuted(after)
        finally:
            await second.close()
    asyncio.run(scenario())


def test_saved_increase_respects_new_durable_task_output_ceiling(tmp_path):
    """Resume must resolve current task authority before the real call claim."""
    async def scenario():
        task = "offline-length-task-cap-revalidation"
        before, context, first_request = await saved_increase(tmp_path, task)
        current = load_plan_state(tmp_path, task)
        current["task_state"]["budgets"]["max_planner_output_tokens"] = 4096
        save_plan_state(tmp_path, task, current)
        second = Harness(tmp_path, Model([response(complete_candidate())]))
        official_profile(second)
        try:
            second.watch_provider(task)
            result = await second.run(second.input(task))
            after = load_plan_state(tmp_path, task)
            receipt("policy-task-cap-revalidation", {
                "source": __import__("bio_agents").__file__, "before": before,
                "result": result, "after": after, "requests": second.model.calls,
                "binds": second.model.binds, "provider_states": second.provider_states,
                "manifests": second.manifests(task),
            })
            assert len(second.model.calls) == 1
            assert result["plan_review_outcome"]["type"] == "approval_pending", result
            observation = assert_full_request(second, context, first_request)
            assert observation["next_local_output_limit"] == 4096
            authorized = second.provider_states[0]["task_state"]["planning_failure"]["remediation_authorization"]
            assert authorized["output_policy"]["requested_output_limit"] == 4096
            assert authorized["input_admission"]["hard_block"] is False
            assert [item["extra_body"]["max_tokens"] for item in second.model.binds
                    if "max_tokens" in item.get("extra_body", {})] == [4096]
            claim = after["task_state"]["planning_epoch"]["call_claim"]
            assert claim["output_policy"]["requested_output_limit"] == 4096
            assert claim["remediation"]["source_occurrence_id"] == before["task_state"]["planning_failure"]["occurrence_receipt"]["id"]
            assert length_calls(after) == 1
            assert after["task_state"]["planning_epoch"]["planner_calls_total"] == 2
            assert after["task_state"]["planning_epoch"]["planning_epoch_id"] == before["task_state"]["planning_epoch"]["planning_epoch_id"]
            assert_unapproved_unexecuted(after)
        finally:
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["task_revision", "epoch"])
@pytest.mark.parametrize("length_recovery", [False, True])
def test_request_preparation_cannot_mutate_a_concurrent_successor(tmp_path, change, length_recovery):
    """Real prepare seam -> authoritative transition -> stale node boundary."""
    async def scenario():
        from agent_core.plan_contract import start_planning_epoch
        from agent_core.plan_tracker import commit_consultant_candidate_state

        task = "offline-prepare-transition-" + change + ("-recovery" if length_recovery else "-initial")
        if length_recovery:
            await saved_increase(tmp_path, task)
        h = Harness(tmp_path, Model([]))
        official_profile(h)
        initial = h.input(task) if length_recovery else h.bootstrap(task)
        phase = PhaseProviderContext(
            task_id=task, phase_name="planner", invocation_id="isolated-prepare-invocation",
            epoch_ordinal=1, provider_seed_contract={}, system_message=None,
            seed_message=None, messages=[],
        )
        initial[PHASE_CONTEXT_RUNTIME_KEY] = phase
        prepare = phase.prepare_messages_with_compaction
        advanced = {}

        async def prepare_then_transition(*args, **kwargs):
            messages = await prepare(*args, **kwargs)
            before = load_plan_state(tmp_path, task)
            if change == "task_revision":
                spec = deepcopy(before["current_task"]["task_spec"])
                spec["revision"] += 1
                spec["goal"] += " Explicit isolated task revision during request preparation."
                current = commit_consultant_candidate_state(tmp_path, task, {"task_spec": spec})
            else:
                current = deepcopy(before)
                old = current["task_state"]["planning_epoch"]
                current["task_state"]["planning_epoch"] = start_planning_epoch(
                    task_revision=current["current_task"]["task_revision"],
                    task_semantic_identity=current["current_task"]["task_semantic_hash"],
                    base_plan_revision=0, parent_epoch_id=old["planning_epoch_id"],
                    start_reason="isolated-authoritative-prepare-successor",
                )
                # The epoch transition deliberately retains an old unresolved
                # failure/auth when present. It may not authorize the old node
                # to write or stop the successor task.
                save_plan_state(tmp_path, task, current)
                current = load_plan_state(tmp_path, task)
            advanced.update(deepcopy(current))
            return messages

        phase.prepare_messages_with_compaction = prepare_then_transition
        try:
            adapter = TypedPlanner(data_path=str(tmp_path), source=h.agent._schema2_early_sources["planner"])
            result = await adapter.run(initial)
            final = load_plan_state(tmp_path, task)
            receipt("prepare-transition-" + change + ("-recovery" if length_recovery else "-initial"), {
                "source": __import__("bio_agents").__file__, "advanced": advanced,
                "result": result, "final": final, "provider_requests": h.model.calls,
            })
            assert advanced, "The real preparation seam must be reached."
            assert h.model.calls == []
            assert result["stale_node_result"]["counts_as_failure"] is False
            assert result["stale_node_result"]["caller_invocation_id"]
            expected_field = "task_revision" if change == "task_revision" else "planning_epoch_id"
            assert expected_field in result["stale_node_result"]["changed_preconditions"]
            assert result["planner_outcome"]["type"] not in {"terminal", "harness_error"}, result
            assert result.get("task_status") not in {"blocked", "failed"}
            assert final == advanced, "Stale preparation must not write authorization, old TaskSpec, counters or a terminal record."
            assert_unapproved_unexecuted(final)
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["task_revision", "epoch"])
def test_prepared_request_policy_error_cannot_stop_a_successor(tmp_path, monkeypatch, change):
    """Real full-input admission fails while another owner advances scope."""
    async def scenario():
        import bio_agents
        from agent_core.plan_contract import start_planning_epoch
        from agent_core.plan_tracker import commit_consultant_candidate_state

        task = "offline-policy-error-after-prepare-" + change
        h = Harness(tmp_path, Model([]))
        official_profile(h)
        initial = h.bootstrap(task)
        phase = PhaseProviderContext(
            task_id=task, phase_name="planner", invocation_id="isolated-policy-prepare-invocation",
            epoch_ordinal=1, provider_seed_contract={}, system_message=None,
            seed_message=None, messages=[],
        )
        initial[PHASE_CONTEXT_RUNTIME_KEY] = phase
        advanced, observed = {}, {}
        real_admission = planner_input_admission

        def admission_then_transition(messages, policy, *, base_policy, estimator=None):
            assert phase.provider_messages, "Actual prepare_messages must precede this admission."
            assert list(messages) == phase.provider_messages
            # Invoke the real budget checker with an explicitly injected small
            # current capacity. No store, outcome, candidate or error is mocked.
            capacity = int(policy["input_reserve_used"]) + base_policy.estimation_margin_tokens + base_policy.safety_headroom_tokens + 512
            limited = replace(base_policy, application_context_limit_tokens=capacity)
            try:
                real_admission(messages, policy, base_policy=limited, estimator=estimator)
            except PlannerOutputPolicyError as exc:
                observed.update(exc.to_diagnostic())
                before = load_plan_state(tmp_path, task)
                if change == "task_revision":
                    spec = deepcopy(before["current_task"]["task_spec"])
                    spec["revision"] += 1
                    spec["goal"] += " Explicit isolated revision during input admission."
                    current = commit_consultant_candidate_state(tmp_path, task, {"task_spec": spec})
                else:
                    current = deepcopy(before)
                    old = current["task_state"]["planning_epoch"]
                    current["task_state"]["planning_epoch"] = start_planning_epoch(
                        task_revision=current["current_task"]["task_revision"],
                        task_semantic_identity=current["current_task"]["task_semantic_hash"],
                        base_plan_revision=0, parent_epoch_id=old["planning_epoch_id"],
                        start_reason="isolated-authoritative-admission-successor",
                    )
                    save_plan_state(tmp_path, task, current)
                    current = load_plan_state(tmp_path, task)
                advanced.update(deepcopy(current))
                raise
            raise AssertionError("The injected capacity must produce a real policy error.")

        monkeypatch.setattr(bio_agents, "planner_input_admission", admission_then_transition)
        try:
            adapter = TypedPlanner(data_path=str(tmp_path), source=h.agent._schema2_early_sources["planner"])
            result = await adapter.run(initial)
            final = load_plan_state(tmp_path, task)
            receipt("policy-error-successor-" + change, {
                "source": bio_agents.__file__, "observed_policy_error": observed,
                "advanced": advanced, "result": result, "final": final,
                "provider_requests": h.model.calls,
            })
            assert observed["code"] == "PLANNER_OUTPUT_CONTEXT_CAPACITY_INSUFFICIENT"
            assert advanced and h.model.calls == []
            assert result["stale_node_result"]["caller_invocation_id"]
            assert result["stale_node_result"]["counts_as_failure"] is False
            assert result["planner_outcome"]["type"] not in {"terminal", "harness_error"}, result
            assert result.get("task_status") not in {"blocked", "failed"}
            assert final == advanced
            assert_unapproved_unexecuted(final)
        finally:
            await h.close()
    asyncio.run(scenario())

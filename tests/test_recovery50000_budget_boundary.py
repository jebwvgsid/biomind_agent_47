"""A3/A7 through the real Node, full input accounting and final adapter.

Only external provider responses and synthetic inherited material are supplied
by the tests. Counts are produced by the actual estimator; no token usage or
remote provider capability is invented. The other SDK tests cover wire JSON.
"""
import asyncio
from copy import deepcopy
import json

from langchain_core.messages import AIMessage, HumanMessage

from agent_core.context_lineage import ContextLineageStore
from agent_core.workflow.context_compaction import ReferenceTokenEstimator, measure_provider_input
from agent_core.workflow.recovery_runtime import configured_recovery_budget_policy
from tests.test_gwas_s4_recovery_integration import CapturedChat, blocked, empty_failure_fixture, make_node, tag


TARGET_INPUT_TOKENS = 118554
MATERIAL_ID = "synthetic-budget-calibration"


def _policy():
    policy = configured_recovery_budget_policy()
    assert policy.requested_output_limit_tokens == policy.output_reserve_tokens == 50000
    assert policy.application_context_limit_tokens == policy.effective_context_limit_tokens == 200000
    # The existing independently reserved margin stays in this arithmetic.
    assert TARGET_INPUT_TOKENS + 50000 + policy.estimation_margin_tokens <= 200000
    return policy


def _no_compaction(*_args, **_kwargs):
    raise AssertionError("The complete measured request fits; compaction must not run")


def _synthetic_body(chars):
    prefix = "Synthetic inherited context for offline capacity acceptance.\n"
    unit = "Recorded synthetic material has no scientific or environment authority.\n"
    return prefix + (unit * (chars // len(unit) + 1))[:chars]


def _complete_measurement(row, policy, estimator, *, replacement_body=None):
    messages = deepcopy(row["messages"])
    assert messages[2]["type"] == "human"
    assert messages[2]["content"].startswith("Synthetic inherited context for offline capacity acceptance.")
    if replacement_body is not None:
        messages[2]["content"] = replacement_body
    return measure_provider_input({
        "system": messages[0], "frozen_inputs": messages[1],
        "inherited_materials": messages[2:3], "local_messages": messages[3:],
        "tool_schema": row["tools"], "pending_results": [], "provider_wrappers": {},
    }, policy=policy, estimator=estimator)


def test_a3_complete_118554_input_does_not_compact_and_adapter_dispatches_50000(tmp_path):
    async def scenario():
        policy = _policy()
        estimator = ReferenceTokenEstimator()

        async def run_at(root, chars):
            fixture, _, _ = await empty_failure_fixture(root)
            business = CapturedChat(lambda messages, *_: blocked(tag(messages, "recovery_seed")))
            compressor = CapturedChat(_no_compaction)
            node = make_node(root, business, compactor=compressor, policy=policy,
                inherited_materials_factory=lambda *_: [HumanMessage(
                    content=_synthetic_body(chars), id=MATERIAL_ID)])
            result = await node.run(fixture.state)
            assert result["recovery_outcome"]["type"] == "blocked", result
            assert len(business.calls) == 1 and not compressor.calls
            return business.calls[0]

        # Capture a real complete request first. Then vary actual content,
        # remeasuring the entire payload; no production budget is substituted.
        baseline = await run_at(tmp_path / "calibrate", 0)
        lower, upper = 0, 1024
        while _complete_measurement(baseline, policy, estimator,
                replacement_body=_synthetic_body(upper)).estimated_input_tokens < TARGET_INPUT_TOKENS:
            upper *= 2
            assert upper <= 4000000, "Unexpected real estimator density"
        while lower < upper:
            middle = (lower + upper) // 2
            measured = _complete_measurement(baseline, policy, estimator,
                replacement_body=_synthetic_body(middle)).estimated_input_tokens
            if measured < TARGET_INPUT_TOKENS:
                lower = middle + 1
            else:
                upper = middle

        # Fresh durable state reaches actual Node admission and final adapter.
        # Root identities differ from calibration, so assert/report the measured
        # value rather than declaring a fabricated exact 118554 token request.
        dispatched = await run_at(tmp_path / "measured", lower)
        complete = _complete_measurement(dispatched, policy, estimator)
        receipt = dispatched["budget"]
        assert complete.estimated_input_tokens == receipt["estimated_input_tokens"]
        assert complete.component_token_counts == receipt["component_token_counts"]
        assert abs(complete.estimated_input_tokens - TARGET_INPUT_TOKENS) <= 1000
        assert not complete.should_compact
        assert dispatched["max_tokens"] == receipt["effective_output_limit_tokens"] == 50000
        assert complete.estimated_input_tokens + 50000 + policy.estimation_margin_tokens <= 200000
        from agent_core.workflow.recovery_agent import recovery_tool_schemas
        assert len(dispatched["tools"]) == len(recovery_tool_schemas())
        assert any(row["function"]["name"] == "exec_command" for row in dispatched["tools"])
        assert all(complete.component_token_counts[key] > 0 for key in
                   ("system", "frozen_inputs", "inherited_materials", "tool_schema"))
        (tmp_path / "a3_complete_budget_receipt.json").write_text(json.dumps({
            "target_input_tokens": TARGET_INPUT_TOKENS,
            "actual_complete_input_tokens": complete.estimated_input_tokens,
            "synthetic_inherited_chars": len(_synthetic_body(lower)),
            "complete_budget": complete.to_dict(), "final_adapter_receipt": receipt,
            "final_adapter_max_tokens": dispatched["max_tokens"],
            "compactor_calls": 0, "external_provider": "scripted leaf, no HTTP request",
        }, indent=2))
    asyncio.run(scenario())


def test_a7_long_truncation_is_durable_then_full_clean_request_is_admitted(tmp_path):
    async def scenario():
        policy = _policy()
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        draft = "TRUNCATED-LONG-DRAFT|" + "d" * 119000
        native = "TRUNCATED-NATIVE-REASONING|" + "r" * 181000
        partial_args = '{"ref_id":"UNFINISHED-ARGUMENT|' + "x" * 4000
        call_id = "never-dispatch-partial-long-output"
        assert len(draft) + len(native) + len(partial_args) > 200000

        def respond(messages, row, ordinal):
            assert row["max_tokens"] == row["budget"]["output_reserve_tokens"] == 50000
            if ordinal == 1:
                return AIMessage(content=draft, additional_kwargs={"reasoning_content": native,
                    "tool_calls": [{"id": call_id, "type": "function", "function": {
                        "name": "failure_inspect", "arguments": partial_args}}]},
                    response_metadata={"finish_reason": "length"})
            assert ordinal == 2
            exact_next_payload = json.dumps(row["messages"], ensure_ascii=False)
            for forbidden in ("TRUNCATED-LONG-DRAFT|", "TRUNCATED-NATIVE-REASONING|",
                              "UNFINISHED-ARGUMENT|", call_id):
                assert forbidden not in exact_next_payload
            assert row["budget"]["estimated_input_tokens"] > 0
            assert row["budget"]["estimated_input_tokens"] + 50000 + policy.estimation_margin_tokens <= 200000
            return blocked(tag(messages, "recovery_seed"), "accepted-after-long-truncation")

        business = CapturedChat(respond)
        compressor = CapturedChat(_no_compaction)
        result = await make_node(tmp_path, business, compactor=compressor, policy=policy).run(fixture.state)
        assert result["recovery_outcome"]["type"] == "blocked", result
        assert len(business.calls) == 2 and not compressor.calls
        events = [json.loads(line) for line in
            (ContextLineageStore(tmp_path, fixture.task_id).root / "events.jsonl").read_text().splitlines()]
        diagnostic = [event["payload"] for event in events if
            event["event_kind"] == "recovery_assistant_message_committed" and event["payload"].get("diagnostic_only")]
        assert len(diagnostic) == 1
        stored = diagnostic[0]["message"]
        assert stored["content"] == draft
        assert stored["additional_kwargs"]["reasoning_content"] == native
        assert stored["tool_calls"][0]["function"]["arguments"] == partial_args
        assert diagnostic[0]["raw_tool_calls"][0]["function"]["arguments"] == partial_args
        assert diagnostic[0]["finish_reason"] == "length"
        assert not any(event["event_kind"] == "recovery_tool_started" for event in events)
        assert sum(event["event_kind"] == "recovery_directive_admitted" for event in events) == 1
        archives = [json.loads(p.read_text()) for p in (tmp_path / "phase_archive/objects").rglob("*.json")]
        episode = next(item for item in archives if item["phase"] == "recovery")
        assert episode["summary"]["business_budget_counters"]["output_resubmission_count"] == 1
        (tmp_path / "a7_long_truncation_receipt.json").write_text(json.dumps({
            "injected_draft_chars": len(draft), "injected_native_reasoning_chars": len(native),
            "injected_partial_arguments_chars": len(partial_args), "diagnostic_exact_equality": True,
            "dispatch_budgets": [call["budget"] for call in business.calls],
            "adapter_max_tokens": [call["max_tokens"] for call in business.calls],
            "resubmission_count": 1, "tool_dispatches": 0, "admitted_directives": 1,
            "external_provider": "scripted length/stop leaves; no claimed remote usage",
        }, indent=2))
    asyncio.run(scenario())

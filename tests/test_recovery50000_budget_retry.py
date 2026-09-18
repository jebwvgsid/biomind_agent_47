"""Real Recovery graph, final application adapter, archive and durable reentry.
Only external provider responses are scripted. No live config/API is read.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from agent_core.context_lineage import ContextLineageStore
from agent_core.workflow.context_compaction import ContextBudgetPolicy
from agent_core.workflow.recovery_agent import RecoveryAgentLoopConfig, recovery_boundary_messages_from_events
from agent_core.workflow.recovery_runtime import configured_recovery_budget_policy, resolve_recovery_budget_policy
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.test_gwas_s4_recovery_integration import (CapturedChat, make_node, empty_failure_fixture, tag, blocked, tool_call)


def cap(**kwargs):
    return ContextBudgetPolicy(output_reserve_tokens=50000, requested_output_limit_tokens=50000,
        output_capability_source="not_verified", **kwargs)


def truncated(reason="length", *, complete_prefix=False):
    calls = [{"id": "never-dispatch", "type": "function", "function": {
        "name": "failure_inspect", "arguments": '{"ref_id":"never"}' if complete_prefix else '{"ref_id":'}}]
    return AIMessage(content="<analysis>incomplete private draft", additional_kwargs={"tool_calls": calls},
        response_metadata={"finish_reason": reason})


@pytest.mark.parametrize("reason", ["length", "max_tokens", "max_output_tokens"])
@pytest.mark.parametrize("complete_prefix", [False, True])
def test_full_resubmission_uses_actual_50000_never_dispatches_partial(tmp_path, reason, complete_prefix):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        def reply(messages, row, ordinal):
            assert row["max_tokens"] == 50000
            assert row["budget"]["requested_output_limit_tokens"] == 50000
            if ordinal == 1: return truncated(reason, complete_prefix=complete_prefix)
            assert all("incomplete private draft" not in str(m.content) for m in messages)
            assert not any(getattr(m, "tool_call_id", None) == "never-dispatch" for m in messages)
            return blocked(tag(messages, "recovery_seed"))
        leaf = CapturedChat(reply, kwargs={"max_tokens": 8192})
        out = await make_node(tmp_path, leaf, policy=cap()).run(fixture.state)
        assert len(leaf.calls) == 2, out
        assert out["recovery_outcome"]["type"] == "blocked", out
        archives = [json.loads(p.read_text()) for p in (tmp_path/"phase_archive/objects").rglob("*.json")]
        episode = next(a for a in archives if a["phase"] == "recovery")
        assert episode["summary"]["business_budget_counters"]["output_resubmission_count"] == 1
        events = list((tmp_path/"task_outputs/task-recovery/context_trace").glob("events.jsonl"))
        # Locate the real trace owner rather than assert an invented log path.
        raw = (ContextLineageStore(tmp_path, fixture.task_id).root/"events.jsonl").read_text()
        assert "incomplete private draft" in raw
        assert '"diagnostic_only": true' in raw
    asyncio.run(scenario())


def test_three_truncations_exhaust_two_resubmissions_and_reentry_is_terminal(tmp_path):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        leaf = CapturedChat(lambda *args: truncated())
        node = make_node(tmp_path, leaf, policy=cap())
        out = await node.run(fixture.state)
        assert len(leaf.calls) == 3, out
        assert "RECOVERY_RESPONSE_INCOMPLETE" in str(out)
        again = await make_node(tmp_path, leaf, policy=cap()).run(fixture.state)
        assert len(leaf.calls) == 3, again
    asyncio.run(scenario())


def test_crash_after_atomic_truncation_checkpoint_preserves_allowance(tmp_path):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        first = CapturedChat(lambda *args: truncated())
        node = make_node(tmp_path, first, policy=cap())
        real_sink = node.event_sink
        def crash(event, payload):
            real_sink(event, payload)
            if event == "recovery_assistant_message_committed":
                raise asyncio.CancelledError()
        node.event_sink = crash
        with pytest.raises(asyncio.CancelledError): await node.run(fixture.state)
        assert len(first.calls) == 1
        after = CapturedChat(lambda *args: truncated())
        out = await make_node(tmp_path, after, policy=cap()).run(fixture.state)
        assert len(after.calls) == 2, out
        assert "RECOVERY_RESPONSE_INCOMPLETE" in str(out)
        assert all("incomplete private draft" not in json.dumps(c["messages"]) for c in after.calls)
    asyncio.run(scenario())


def test_tool_read_between_truncations_does_not_reset_allowance(tmp_path):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        def reply(messages, row, ordinal):
            if ordinal in {1,3,4}: return truncated()
            seed = tag(messages, "recovery_seed")
            return tool_call("failure_inspect", {"ref_id": seed["failure_packet"]["source_outcome_ref"]}, "one-real-read")
        leaf = CapturedChat(reply)
        out = await make_node(tmp_path, leaf, policy=cap()).run(fixture.state)
        assert len(leaf.calls) == 4, out
        assert "RECOVERY_RESPONSE_INCOMPLETE" in str(out)
        archives = [json.loads(p.read_text()) for p in (tmp_path/"phase_archive/objects").rglob("*.json")]
        counts = next(a for a in archives if a["phase"] == "recovery")["summary"]["business_budget_counters"]
        assert counts["output_resubmission_count"] == 2 and counts["tool_call_count"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["content_filter", "cancelled", "error"])
def test_nonlength_stop_never_uses_length_allowance(tmp_path, reason):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        leaf = CapturedChat(lambda *args: AIMessage(content="", response_metadata={"finish_reason": reason}))
        out = await make_node(tmp_path, leaf, policy=cap()).run(fixture.state)
        assert len(leaf.calls) == 1 and "RECOVERY_RESPONSE_INCOMPLETE" in str(out)
    asyncio.run(scenario())


def test_explicit_provider_limit_is_visible_and_inherited_local_cap_is_not_capability():
    class Legacy: max_tokens=8192
    actual = resolve_recovery_budget_policy(cap(known_provider_output_limit_tokens=32768), clients=[Legacy()])
    assert actual.output_reserve_tokens == 32768
    assert actual.requested_output_limit_tokens == 50000
    assert actual.known_provider_output_limit_tokens == 32768
    assert resolve_recovery_budget_policy(cap(), clients=[Legacy()]).output_reserve_tokens == 50000
    assert configured_recovery_budget_policy().requested_output_limit_tokens == 50000
    assert 118554 < actual.safe_input_tokens
    assert cap().safe_input_tokens < 170000


def test_native_reasoning_and_signature_are_budgeted_and_preserved_on_replay():
    original = AIMessage(content="public", additional_kwargs={"reasoning_content":"private-native", "signature":"signature-value"},
        tool_calls=[{"id":"read", "name":"evidence_read", "args": {}}])
    payload = provider_visible_payload(original)
    assert payload["additional_kwargs"] == original.additional_kwargs
    restored = recovery_boundary_messages_from_events([{"event":"recovery_assistant_message_committed", "message_id":"test", "message":payload}])[0]
    assert provider_visible_payload(restored) == payload


def test_cannot_configure_more_than_two_additional_resubmissions():
    with pytest.raises(ValueError): RecoveryAgentLoopConfig(max_output_resubmissions=3)


@pytest.mark.parametrize("transport", ["native_command", "complete_text"])
def test_missing_provider_terminal_never_adopts_complete_prefix(tmp_path, transport):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        def reply(messages, row, ordinal):
            response = blocked(tag(messages, "recovery_seed"))
            if transport == "native_command":
                return AIMessage(content="", tool_calls=[{"id":"never-dispatch", "name":"failure_inspect", "args":{"ref_id":"never"}}])
            return response.model_copy(update={"response_metadata": {}})
        leaf = CapturedChat(reply)
        out = await make_node(tmp_path, leaf, policy=cap()).run(fixture.state)
        assert len(leaf.calls) == 1 and "RECOVERY_RESPONSE_INCOMPLETE" in str(out)
        events = (ContextLineageStore(tmp_path,fixture.task_id).root/"events.jsonl").read_text()
        assert '"event_kind": "recovery_tool_started"' not in events
        assert '"event_kind": "recovery_directive_admitted"' not in events
    asyncio.run(scenario())


def test_large_native_response_is_persisted_without_public_reasoning(tmp_path):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        native_reasoning = "native-private-large-reasoning " * 8000
        def reply(messages, row, ordinal):
            return blocked(tag(messages,"recovery_seed")).model_copy(update={
                "additional_kwargs":{"reasoning_content": native_reasoning}})
        leaf=CapturedChat(reply)
        out=await make_node(tmp_path,leaf,policy=cap()).run(fixture.state)
        assert len(leaf.calls)==1 and out["recovery_outcome"]["type"]=="blocked",out
        root=ContextLineageStore(tmp_path,fixture.task_id).root
        events=[json.loads(x) for x in (root/"events.jsonl").read_text().splitlines()]
        assistant=next(x for x in events if x["event_kind"]=="recovery_assistant_message_committed")
        assert assistant["payload"]["message"]["additional_kwargs"]["reasoning_content"]==native_reasoning
        public=next(x for x in events if x["event_kind"]=="recovery_response_received")
        assert "native-private" not in json.dumps(public)
        assert "native-private" not in str(out)
    asyncio.run(scenario())


def test_compressed_epoch_then_truncation_crash_does_not_reset_resource_counter(tmp_path):
    from agent_core.workflow.recovery_runtime import RecoveryResourcePolicy
    from tests.test_gwas_s4_recovery_integration import HierarchicalResponses
    async def scenario():
        fixture,_,_=await empty_failure_fixture(tmp_path, material_chars=1_100_000)
        summaries = HierarchicalResponses()
        def summarized(messages, row, ordinal):
            response = summaries(messages, row, ordinal)
            from agent_core.workflow.context_compaction import ReferenceTokenEstimator
            output = ReferenceTokenEstimator().estimate(str(response.content)).tokens
            # Explicit simulated provider usage exercises the existing
            # reservation/settlement path; this is not live-provider telemetry.
            return response.model_copy(update={"usage_metadata": {
                "input_tokens":row["budget"]["estimated_input_tokens"], "output_tokens":output,
                "total_tokens":row["budget"]["estimated_input_tokens"]+output}})
        compressor=CapturedChat(summarized)
        resources=RecoveryResourcePolicy(soft_tokens=10_000_000,hard_tokens=20_000_000,
            soft_seconds=600,hard_seconds=900,decision_seconds=30)
        policy=cap(application_context_limit_tokens=350000,compressor_safe_input_tokens=30000)
        leaf=CapturedChat(lambda *a: truncated())
        node=make_node(tmp_path,leaf,compactor=compressor,policy=policy,resource_policy=resources)
        sink=node.event_sink
        def interrupt(event,payload):
            sink(event,payload)
            if event=="recovery_assistant_message_committed" and payload.get("diagnostic_only"):
                raise asyncio.CancelledError()
        node.event_sink=interrupt
        with pytest.raises(asyncio.CancelledError):await node.run(fixture.state)
        assert len(leaf.calls)==1 and compressor.calls
        durable=fixture.reload()
        assert durable["task_state"].get("recovery_context_compaction")
        after=CapturedChat(lambda *a: truncated())
        out=await make_node(tmp_path,after,compactor=compressor,policy=policy,resource_policy=resources).run(fixture.state)
        assert len(after.calls)==2,out
        assert "RECOVERY_RESPONSE_INCOMPLETE" in str(out)
        events=[json.loads(x) for x in (ContextLineageStore(tmp_path,fixture.task_id).root/"events.jsonl").read_text().splitlines()]
        ids={x["event_id"] for x in events}
        for event in events:
            if event["event_kind"]=="recovery_assistant_message_committed" and event["payload"].get("diagnostic_only"):
                feedback=json.loads(event["payload"]["incomplete_response_feedback"]["content"])
                assert feedback["diagnostic_event_id"] in ids
    asyncio.run(scenario())


def test_corrupt_durable_boundary_never_restores_fresh_allowance(tmp_path):
    async def scenario():
        fixture,_,_=await empty_failure_fixture(tmp_path)
        first=CapturedChat(lambda *a:truncated())
        node=make_node(tmp_path,first,policy=cap());sink=node.event_sink
        def interrupt(event,payload):
            sink(event,payload)
            if event=="recovery_assistant_message_committed":raise asyncio.CancelledError()
        node.event_sink=interrupt
        with pytest.raises(asyncio.CancelledError):await node.run(fixture.state)
        path=ContextLineageStore(tmp_path,fixture.task_id).root/"events.jsonl"
        with path.open("a") as handle:handle.write('{"broken":')
        leaf=CapturedChat(lambda *a:pytest.fail("A damaged checkpoint must not become zero retries"))
        out=await make_node(tmp_path,leaf,policy=cap()).run(fixture.state)
        assert not leaf.calls and "RECOVERY_BOUNDARY_EVENT_INVALID" in str(out),out
    asyncio.run(scenario())


def test_valid_full_resubmission_uses_existing_admission_once_and_reenters_without_model(tmp_path):
    async def scenario():
        fixture,_,_=await empty_failure_fixture(tmp_path)
        def reply(messages,row,ordinal):
            if ordinal==1:return truncated()
            seed=tag(messages,"recovery_seed")
            directive={"schema_version":1,"directive_id":"one-repaired-submission",
                "task_id":fixture.task_id,"action":"retry_step","summary":"Regenerate the invalid artifact extractor.",
                "reason_code":"GENERATED_ACTION_FAILED","target_step_id":seed["invocation_scope"]["step_id"],
                "instruction":"Regenerate only this action; preserve formal approvals and unknown Q correspondence.",
                "evidence_refs":[seed["failure_packet"]["source_outcome_ref"]]}
            return AIMessage(content=json.dumps(directive),response_metadata={"finish_reason":"stop"})
        leaf=CapturedChat(reply);node=make_node(tmp_path,leaf,policy=cap())
        out=await node.run(fixture.state)
        assert out["recovery_outcome"]["type"]=="candidate_repair_required",out
        before=fixture.reload();receipts=deepcopy(before["task_state"]["typed_recovery_receipts"])
        assert len(receipts)==1 and len(leaf.calls)==2
        resumed=await make_node(tmp_path,leaf,policy=cap()).run(fixture.state)
        assert resumed["recovery_outcome"]==out["recovery_outcome"]
        assert len(leaf.calls)==2
        assert fixture.reload()["task_state"]["typed_recovery_receipts"]==receipts
        assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
    asyncio.run(scenario())


def test_known_lower_cap_dispatches_effective_value_with_explicit_adjustment(tmp_path):
    async def scenario():
        fixture,_,_=await empty_failure_fixture(tmp_path)
        leaf=CapturedChat(lambda messages,*_:blocked(tag(messages,"recovery_seed")),kwargs={"max_tokens":8192})
        out=await make_node(tmp_path,leaf,policy=cap(known_provider_output_limit_tokens=32768)).run(fixture.state)
        assert len(leaf.calls)==1,out
        request=leaf.calls[0]
        assert request["max_tokens"]==32768
        assert request["budget"]["requested_output_limit_tokens"]==50000
        assert request["budget"]["output_cap_adjustment"]=="explicit_provider_or_resource_limit"
    asyncio.run(scenario())


def test_truncation_checkpoint_across_interpreter_restart(tmp_path):
    """Release driver runs prepare/resume in two guarded interpreter processes."""
    import os
    stage=os.environ.get("R50K_RESTART_PHASE","both")
    root=Path(os.environ.get("R50K_RESTART_ROOT",str(tmp_path)))
    root.mkdir(parents=True,exist_ok=True)
    async def prepare():
        fixture,_,_=await empty_failure_fixture(root)
        leaf=CapturedChat(lambda *a:truncated())
        node=make_node(root,leaf,policy=cap());sink=node.event_sink
        def interrupt(event,payload):
            sink(event,payload)
            if event=="recovery_assistant_message_committed":raise asyncio.CancelledError()
        node.event_sink=interrupt
        with pytest.raises(asyncio.CancelledError):await node.run({"task_id":fixture.task_id})
        assert len(leaf.calls)==1
        (root/"process-before.json").write_text(json.dumps({"pid":os.getpid(),"task_id":fixture.task_id,
            "bootstrap_root":str(__import__("__main__").RUNROOT),"physical_requests":1}))
    async def resume():
        before=json.loads((root/"process-before.json").read_text())
        if stage=="resume":
            assert before["pid"]!=os.getpid()
            assert before["bootstrap_root"]!=str(__import__("__main__").RUNROOT)
        leaf=CapturedChat(lambda *a:truncated())
        out=await make_node(root,leaf,policy=cap()).run({"task_id":before["task_id"]})
        assert len(leaf.calls)==2,out
        assert "RECOVERY_RESPONSE_INCOMPLETE" in str(out)
        assert all(call["max_tokens"]==50000 for call in leaf.calls)
        (root/"process-after.json").write_text(json.dumps({"pid":os.getpid(),"requests_this_process":len(leaf.calls),
            "total_requests":1+len(leaf.calls),"result":out},default=str))
    if stage in {"prepare","both"}:asyncio.run(prepare())
    if stage in {"resume","both"}:asyncio.run(resume())

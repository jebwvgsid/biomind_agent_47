"""S4 uses real Node, transport adapter, archive, CAS and scoped dispatcher.

Only chat provider responses are controlled. All fixtures live in fresh /tmp
stores; no model service, environment discovery or scientific action runs.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.workflow.context_compaction import ContextBudgetPolicy, ReferenceTokenEstimator
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_agent import RecoveryAgentLoopConfig
from agent_core.workflow.recovery_context import RecoveryObservationStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_runtime import RECOVERY_DISPATCH_BUDGET, recovery_dispatch_receipt
from agent_core.workflow.recovery_tools import RecoveryLocalToolDispatcher, build_recovery_inspection_registry
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.test_inspection_evidence import create_inspection_case, publish, reopen
from tests.test_inspection_workflow import make_inspection_fixture
from tests import test_recovery_schema2_node as node_fixtures


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tag(messages, name):
    for message in messages:
        content = str(message.get("content", "")) if isinstance(message, dict) else str(message.content)
        if content.startswith("<" + name):
            return json.loads(content.split("\n", 1)[1].rsplit("\n", 1)[0])
    raise AssertionError(name)


def tool_call(name, args, identity):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identity, "type": "tool_call"}], response_metadata={"finish_reason": "tool_calls"})


def blocked(seed, identity="s4-blocked"):
    return AIMessage(content=canonical({"schema_version": 1, "directive_id": identity,
        "task_id": seed["invocation_scope"]["task_id"], "action": "blocked",
        "summary": "rc=0; formal review did not accept the step; no Commit; Q correspondence remains unknown.",
        "reason_code": "INSPECTION_UNRESOLVED", "evidence_refs": [seed["failure_packet"]["source_outcome_ref"]]}),
        response_metadata={"finish_reason": "stop"})


class CapturedChat:
    """The final external model leaf; bind preserves the actual dispatch kwargs."""
    def __init__(self, respond, *, root=None, kwargs=None):
        self.respond = respond
        self.root = root or self
        self.kwargs = dict(kwargs or {})
        if root is None:
            self.calls = []

    def bind(self, **kwargs):
        return CapturedChat(self.respond, root=self.root, kwargs={**self.kwargs, **kwargs})

    def bind_tools(self, tools):
        return self.bind(tools=deepcopy(tools))

    async def ainvoke(self, messages, config=None, **kwargs):
        scope = RECOVERY_DISPATCH_BUDGET.get()
        assert scope is not None, "Real Node must carry admission to the final adapter"
        options = {**self.kwargs, **kwargs}
        receipt = recovery_dispatch_receipt(messages, stage=scope["stage"], client=self)
        assert options["max_tokens"] == receipt["output_reserve_tokens"] == receipt["effective_output_limit_tokens"]
        assert receipt["estimated_input_tokens"] + options["max_tokens"] + receipt["estimation_margin_tokens"] <= receipt["effective_context_limit_tokens"]
        assert receipt["estimated_input_tokens"] <= receipt["safe_input_tokens"]
        row = {"stage": scope["stage"], "messages": [provider_visible_payload(x) for x in messages],
            "tools": deepcopy(options.get("tools", [])), "max_tokens": options["max_tokens"], "budget": receipt,
            "model": "controlled-s4-provider", "provider_config": {"keys": sorted((config or {}).keys())}}
        self.root.calls.append(row)
        response = self.respond(messages, row, len(self.root.calls))
        if asyncio.iscoroutine(response):
            response = await response
        estimate = ReferenceTokenEstimator().estimate(str(response.content)).tokens
        assert estimate <= options["max_tokens"], (estimate, options["max_tokens"])
        return response


def adapter(root, business, compactor=None):
    agent = node_fixtures.Schema2RecoveryNodeTests._production_adapter_agent(str(root), business)
    agent.recovery_llm = business
    agent.recovery_transport_llm = compactor or business
    agent.llm_timeout_seconds = agent.llm_readonly_timeout_seconds = agent.llm_total_timeout_seconds = 30
    agent.llm_first_chunk_timeout_seconds = agent.llm_stream_idle_timeout_seconds = 30
    return agent


def make_node(root, business, *, compactor=None, policy=None, **kwargs):
    agent = adapter(root, business, compactor)
    return Schema2RecoveryNode(data_path=str(root), model=business,
        compactor=agent._schema2_context_compactor_invoke, compactor_model=agent.recovery_transport_llm,
        provider_invoke=agent._schema2_recovery_provider_invoke, event_sink=agent._schema2_recovery_event_sink,
        context_budget_policy=policy or ContextBudgetPolicy(), **kwargs)


def test_observation_formal_pages_reopen_and_scope_rejections(tmp_path):
    fixture = make_inspection_fixture(tmp_path, task_id="task-recovery")
    asyncio.run(fixture.inspect_handoff())
    durable, seed, registry, dispatcher = reopen(tmp_path)
    store = RecoveryObservationStore(PhaseArchiveStore(tmp_path))
    payload = {"returncode": 0, "content": "BEGIN|" + "large exact observation\n" * 12000 + "|END", "Q_correspondence": "unknown"}
    externalized = store.externalize(seed=seed, request_id="actual-observation-1", tool="failure.inspect",
        source_ref=seed.failure_packet.source_outcome_ref, result=payload)
    ref = externalized["observation_ref"]
    first = dispatcher.dispatch_full("evidence.read", {"evidence_ref": ref, "start_line": 1, "end_line": 8})
    assert first["observation_page"] and len(canonical(first)) < 24000
    assert first["representation"] == "json_chunk_envelope_v1"
    assert first["cursor"]["next_start_line"] == 9
    _, same_seed, _, fresh_dispatcher = reopen(tmp_path)
    second = fresh_dispatcher.dispatch_full("evidence.read", {"evidence_ref": ref, "start_line": 9, "end_line": 16})
    assert second["digest"] == first["digest"] and second["start_line"] == 9
    assert first == fresh_dispatcher.dispatch_full("evidence.read", {"evidence_ref": ref, "start_line": 1, "end_line": 8})
    wrong = seed.to_dict(); wrong["failure_packet"]["attempt_ref"] = "wrong-attempt"
    from agent_core.workflow.stage_contracts import RecoverySeed
    with pytest.raises(ValueError, match="SCOPE|STALE"):
        store.resolve(seed=RecoverySeed.from_mapping(wrong), observation_ref=ref)
    descriptor, body = store.resolve(seed=same_seed, observation_ref=ref)
    assert body == payload
    for field, value in (("digest", "sha256:" + "0" * 64), ("object_revision", 2), ("allowed_tools", ["artifact.read"])):
        altered = deepcopy(descriptor)
        if field == "digest": altered[field] = value
        else: altered["scope"][field] = value
        with pytest.raises(ValueError, match="INDEX_MISMATCH"):
            store.read(seed=same_seed, descriptor=altered)


def test_real_node_externalizes_reads_pages_and_does_not_reexternalize(tmp_path):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path, task_id="task-recovery")
        await fixture.inspect_handoff()
        _, seed, _, _ = reopen(tmp_path)
        reads = []
        def respond(messages, row, ordinal):
            provider_seed = tag(messages, "recovery_seed")
            results = [json.loads(m.content) for m in messages if getattr(m, "type", "") == "tool"]
            if ordinal == 1:
                return tool_call("failure_inspect", {"ref_id": seed.failure_packet.source_outcome_ref}, "large-full-result")
            if ordinal == 2:
                external = results[-1]["result"]
                assert external["status"] == "EXTERNALIZED", external
                reads.append(external["observation_ref"])
                return tool_call("evidence_read", {"evidence_ref": reads[0], "start_line": 1, "end_line": 8}, "page-one")
            if ordinal == 3:
                page = results[-1]["result"]
                assert page.get("observation_page") and page.get("status") != "EXTERNALIZED", page
                return tool_call("evidence_read", {"evidence_ref": reads[0], "start_line": 1, "end_line": 8}, "page-repeat")
            assert results[-1]["result"].get("observation_page")
            return blocked(provider_seed)
        model = CapturedChat(respond)
        node = make_node(tmp_path, model, loop_config=RecoveryAgentLoopConfig(single_observation_chars=8000))
        result = await node.run({"task_id": seed.failure_packet.task_id})
        assert result["recovery_outcome"]["type"] == "blocked", result
        assert len(model.calls) == 4, result
        archives = [json.loads(p.read_text()) for p in (tmp_path / "phase_archive" / "objects").rglob("*.json")]
        observation_archives = [r for r in archives if r.get("phase") == "recovery-observation"]
        assert len(observation_archives) == 1
        final = next(r for r in archives if r["phase"] == "recovery")
        assert final["summary"]["business_budget_counters"]["no_progress_rounds"] == 1
        assert final["summary"]["business_budget_counters"]["tool_call_count"] == 3
        assert all(c["stage"] == "recovery" and len(c["tools"]) == 11 for c in model.calls)
        (tmp_path / "dispatch_receipts.json").write_text(json.dumps(model.calls, indent=2))
    asyncio.run(scenario())


def empty_failure_fixture(root, *, material_chars=0):
    """Actual approved action/provenance, rc0 reconciliation, TypedExecute, Binder.

    The execution leaf records a bounded synthetic stdout. The formal source
    carries Q uncertainty; no immutable published packet is subsequently edited.
    """
    from agent_core.managed_artifacts import reconcile_runtime_outputs
    from agent_core.workflow.execution import TypedExecute
    from agent_core.workflow.recovery_projection import CausalFailurePacketBinder
    fixture = make_inspection_fixture(root, task_id="task-recovery", recorded_stdout=(
        "rc=0; vcf.samples.txt empty; formal validation rejected; Q correspondence unknown.\n"
        + "X" * material_chars))
    record = deepcopy(fixture.execution)
    empty = root / "task_outputs/task-recovery/vcf.samples.txt"
    empty.write_text("")
    contract = {"artifact_id": "vcf-samples", "semantic_handle": "output.vcf_samples", "expected_path": str(empty),
        "artifact_kind": "file", "format": "txt", "validation_contract_id": "file_exists", "validation_contract_version": 1,
        "required": True, "constraint_origin": "physical_hard"}
    rejection = reconcile_runtime_outputs([contract], [{"path": str(empty), "attempt_id": record["attempt_id"],
        "current_attempt_owned": True, "artifact_kind": "file", "format": "txt", "content_valid": False, "size": 0}],
        attempt_id=record["attempt_id"], exit_code=0)
    assert rejection["violations"][0]["code"] == "OBSERVED_ARTIFACT_CONTENT_INVALID"
    record.update(task_id=fixture.task_id, returncode=0, exit_code=0, failure_code="OBSERVED_ARTIFACT_CONTENT_INVALID",
        runtime_output_reconciliation=rejection, validation={"passed": False, "errors": rejection["violations"]},
        scientific_observations={"Q_correspondence": "unknown"})
    async def publish_failure():
        async def process_leaf(_state): return {"execution_evidence": deepcopy(record)}
        durable = fixture.reload()
        state = {**fixture.state, "current_step_id": record["step_id"], "task_mode": "managed",
            "plan_id": durable["plan_id"], "plan_version": durable["plan_version"], "state_generation": durable["state_generation"]}
        typed = await TypedExecute(executor=process_leaf).run(state)
        assert typed["execute_outcome"]["type"] == "execution_failed", typed
        update = CausalFailurePacketBinder(data_path=str(root)).bind(
            {**state, "execution_evidence": record}, "execute_outcome", typed["execute_outcome"])
        fixture.state.update(update)
        return fixture, typed, record
    return publish_failure()


class HierarchicalResponses:
    def __init__(self, summary_chars=8500):
        self.operations = []
        self.requests = []
        self.summary_chars = summary_chars
    def __call__(self, messages, row, ordinal):
        content = messages[1]["content"] if isinstance(messages[1], dict) else messages[1].content
        q = json.loads(content.split("\n", 1)[1].rsplit("\n", 1)[0])["request"]
        op = q.get("operation", "final")
        self.operations.append(op); self.requests.append(deepcopy(q))
        ids = {k: q[k] for k in ("compaction_request_id", "source_epoch_id", "source_manifest_hash")}
        facts = "rc0; empty vcf.samples.txt; validation failed; not Committed; Q correspondence unknown. "
        summary = (facts * (self.summary_chars // len(facts) + 1))[:self.summary_chars]
        if op == "map_source_segment":
            parent, segment = q["parent_source_unit"], q["segment_source_unit"]
            result = {"schema_version": 1, "operation": op, **ids, "segment_unit_id": segment["unit_id"],
                "parent_source_unit_id": parent["unit_id"], "parent_source_content_hash": parent["content_hash"],
                "segment_content_hash": segment["content_hash"], "coverage": segment["coverage"],
                "summary": summary, "missing_details": "Exact input remains at the source ref."}
        elif op == "merge_segment_summaries":
            result = {"schema_version": 1, "operation": op, **ids,
                **{k: q[k] for k in ("merge_request_id", "input_summary_ids", "input_summary_hashes", "coverage_spans")},
                "summary": summary if max(i["level"] for i in q["input_summaries"]) == 0 else facts * 15}
        else:
            selection, units = q["selection_policy"], q["source_units"]
            retained = selection["must_retain_exact_unit_ids"]
            retrieval = [{"source_unit_id": u["unit_id"], "source_ref": u["source_ref"], "skill_id": u["skill_id"],
                "content_hash": u["content_hash"], "coverage": u["coverage"], "residency": "summarized",
                "reason": "Measured capacity pressure", "summary": facts, "missing_details": "Read the original for exact detail.",
                "retrieval_ref": u["provenance"]["retrieval_ref"], "scope": u["scope"]}
                for u in units if u["selected"] and u["unit_id"] not in retained]
            result = {"schema_version": 1, **ids, "mode": selection["mode"], "retain_exact_unit_ids": retained,
                "summary_source_unit_ids": [u["unit_id"] for u in units if u["unit_id"] not in retained],
                "summary": facts, "material_retrieval_state": retrieval, "unresolved_items": ["Q correspondence unknown"],
                "selection_rationale": "Complete measured coverage; all original identities remain recoverable."}
        # The controlled provider obeys the actual final adapter cap, including echoed coverage identities.
        while ReferenceTokenEstimator().estimate(canonical(result)).tokens > row["max_tokens"] and len(result["summary"]) > 150:
            result["summary"] = result["summary"][:-300]
        return AIMessage(content=canonical(result))


def test_real_node_multilevel_compaction_adapter_and_formal_read(tmp_path):
    async def scenario():
        fixture, typed, execution = await empty_failure_fixture(tmp_path, material_chars=1100000)
        original = fixture.reload()
        _, seed, _, _ = reopen(tmp_path)
        assert seed.failure_packet.error_code == "OBSERVED_ARTIFACT_CONTENT_INVALID"
        compactions = HierarchicalResponses()
        compressor = CapturedChat(compactions)
        def business_response(messages, row, ordinal):
            current = tag(messages, "recovery_seed")
            if ordinal == 1:
                manifest = current["inspection_manifest"]
                ref = next(r["ref_id"] for r in manifest if r.get("scope", {}).get("object_kind") == "step_attempt" and r.get("readable"))
                return tool_call("evidence_read", {"evidence_ref": ref, "start_line": 1, "end_line": 8}, "read-after-activation")
            return blocked(current, "s4-after-hierarchy")
        business = CapturedChat(business_response)
        policy = ContextBudgetPolicy(application_context_limit_tokens=350000, output_reserve_tokens=4096,
            compressor_safe_input_tokens=30000)
        node = make_node(tmp_path, business, compactor=compressor, policy=policy)
        result = await node.run({"task_id": fixture.task_id})
        (tmp_path / "multilevel_result.json").write_text(json.dumps(result, default=str, indent=2))
        (tmp_path / "multilevel_dispatch.json").write_text(json.dumps({"business": business.calls, "compactor": compressor.calls}, indent=2))
        assert len(business.calls) == 2, result
        assert compactions.operations.count("map_source_segment") > 2
        merges = [q for q in compactions.requests if q.get("operation") == "merge_segment_summaries"]
        assert len(merges) > 1
        assert any(any(i["level"] >= 1 for i in q["input_summaries"]) for q in merges), [q["input_summary_ids"] for q in merges]
        after = fixture.reload()
        assert original["provenance_state"] == after["provenance_state"]
        assert not any(s["status"] == "completed" for s in after["steps"])
        assert result["recovery_outcome"]["type"] == "blocked"
    asyncio.run(scenario())


def test_historical_32097_seed_normal_node_entry_no_compactor(tmp_path):
    import shutil
    from agent_core.workflow.recovery_agent import project_recovery_seed_for_provider, recovery_seed_capacity
    from agent_core.workflow.recovery_projection import project_recovery_seed
    source = Path(__file__).parent / "fixtures/gwas_s4_history/data"
    shutil.copytree(source, tmp_path, dirs_exist_ok=True)
    task_id = "session_1789648505"
    durable = load_plan_state(str(tmp_path), task_id)
    seed = project_recovery_seed({"task_id": task_id}, durable).recovery_seed
    registry = build_recovery_inspection_registry(seed, durable, phase_store=PhaseArchiveStore(tmp_path))
    provider_seed = project_recovery_seed_for_provider(seed, durable, inspection_manifest=registry.provider_manifest())
    assert len(registry.provider_manifest()) == 27
    reader = RecoveryLocalToolDispatcher(seed=seed, durable=durable, registry=registry, phase_store=PhaseArchiveStore(tmp_path))
    reads = []
    for ref, entry in durable["task_state"]["inspection_ref_registry"].items():
        if entry["scope"]["readable"]:
            page = reader.dispatch_full("evidence.read", {"evidence_ref": ref, "start_line": 1, "end_line": 8})
            assert page["content"] and page["digest"] == entry["digest"]
            reads.append({"kind": entry["scope"]["object_kind"], "status": "read", "ref": ref, "digest": page["digest"]})
        else:
            with pytest.raises(ValueError, match="INSPECTION_REF_NOT_RESOLVABLE"):
                reader.dispatch_full("evidence.read", {"evidence_ref": ref, "start_line": 1, "end_line": 8})
            assert entry["scope"]["unavailable_reason"] == "NOT_APPLICABLE_NOT_PRODUCED"
            reads.append({"kind": entry["scope"]["object_kind"], "status": "not_produced", "ref": ref})
    assert sum(r["status"] == "read" for r in reads) == 8
    assert sum(r["status"] == "not_produced" for r in reads) == 3
    (tmp_path / "historical_ref_reads.json").write_text(json.dumps(reads, indent=2))
    assert recovery_seed_capacity(provider_seed)["chars"] == 32097
    def response(messages, row, ordinal):
        assert len(messages) == 16
        assert len(row["tools"]) == 11
        actual = tag(messages, "recovery_seed")
        assert recovery_seed_capacity(actual)["chars"] == 32097
        assert actual["failure_packet"]["error_code"] == "OBSERVED_ARTIFACT_CONTENT_INVALID"
        return blocked(actual, "s4-historical-safe-entry")
    def no_compactor(*args): raise AssertionError("This historical request fits its complete input budget")
    business, compressor = CapturedChat(response), CapturedChat(no_compactor)
    result = asyncio.run(make_node(tmp_path, business, compactor=compressor).run({"task_id": task_id}))
    (tmp_path / "historical_result.json").write_text(json.dumps(result, default=str, indent=2))
    assert len(business.calls) == 1 and not compressor.calls, result
    assert result["recovery_outcome"]["type"] == "blocked"
    (tmp_path / "historical_dispatch.json").write_text(json.dumps(business.calls, ensure_ascii=False, indent=2))


def large_policy():
    return ContextBudgetPolicy(application_context_limit_tokens=350000, output_reserve_tokens=4096,
        compressor_safe_input_tokens=30000)


def read_current_attempt(messages, row, ordinal):
    current = tag(messages, "recovery_seed")
    ref = next(r["ref_id"] for r in current["inspection_manifest"]
        if r.get("scope", {}).get("object_kind") == "step_attempt" and r.get("readable"))
    return tool_call("evidence_read", {"evidence_ref": ref, "start_line": 1, "end_line": 8}, "durable-read-before-restart")


def test_persistent_epoch_and_observation_restart(tmp_path):
    """Driver runs prepare/resume in separate guarded interpreter processes.

    With no explicit phase this also remains an ordinary single-process
    regression; the release command records distinct prepare/resume PIDs.
    """
    import os
    from agent_core.workflow.recovery_agent import RECOVERY_TOOL_CHECKPOINT_EVENT
    stage = os.environ.get("S4_RESTART_PHASE", "both")
    root = Path(os.environ.get("S4_RESTART_ROOT", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    async def prepare():
        fixture, _, _ = await empty_failure_fixture(root, material_chars=1100000)
        original = fixture.reload()
        compactions = HierarchicalResponses()
        compressor, business = CapturedChat(compactions), CapturedChat(read_current_attempt)
        agent = adapter(root, business, compressor)
        async def event(kind, payload):
            agent._schema2_recovery_event_sink(kind, payload)
            if kind == RECOVERY_TOOL_CHECKPOINT_EVENT:
                raise asyncio.CancelledError("controlled process boundary after durable tool checkpoint")
        node = Schema2RecoveryNode(data_path=str(root), model=business, compactor_model=compressor,
            compactor=agent._schema2_context_compactor_invoke, provider_invoke=agent._schema2_recovery_provider_invoke,
            event_sink=event, context_budget_policy=large_policy())
        with pytest.raises(asyncio.CancelledError):
            await node.run({"task_id": "task-recovery"})
        assert len(business.calls) == 1
        durable = fixture.reload()
        active = durable["task_state"]["recovery_context_compaction"]
        epoch = PhaseArchiveStore(root).read(active["candidate_epoch_archive_ref"]).to_dict()
        maintenance = epoch["summary"]["business_resume_state"]["compaction_maintenance"]
        assert len(maintenance["calls"]) == len(compressor.calls) > 20
        assert maintenance["charged_tokens"] > 0
        (root / "restart_before.json").write_text(json.dumps({"pid": os.getpid(), "bootstrap_root": str(__import__("__main__").RUNROOT), "active": active,
            "maintenance": maintenance, "provenance": original["provenance_state"],
            "business": business.calls, "compactor": compressor.calls}, indent=2))
    async def resume():
        before = json.loads((root / "restart_before.json").read_text())
        if stage == "resume": assert before["bootstrap_root"] != str(__import__("__main__").RUNROOT)
        def response(messages, row, ordinal):
            tool_results = [json.loads(m.content) for m in messages if getattr(m, "type", "") == "tool"]
            assert len(tool_results) == 1, "The committed observation is restored once"
            assert tool_results[0]["result"]["source_ref"].startswith("inspection:")
            current = tag(messages, "recovery_seed")
            assert current["failure_packet"]["error_code"] == "OBSERVED_ARTIFACT_CONTENT_INVALID"
            assert "recovery_context_summary" in canonical(row["messages"])
            return blocked(current, "s4-durable-resume-blocked")
        business = CapturedChat(response)
        def forbidden(*args): raise AssertionError("An active compacted epoch cannot restart maintenance")
        compressor = CapturedChat(forbidden)
        node = make_node(root, business, compactor=compressor, policy=large_policy())
        result = await node.run({"task_id": "task-recovery"})
        (root / "restart_result.json").write_text(json.dumps(result, default=str, indent=2))
        assert len(business.calls) == 1 and not compressor.calls, result
        after = load_plan_state(str(root), "task-recovery")
        assert after["task_state"]["recovery_context_compaction"] == before["active"]
        assert after["provenance_state"] == before["provenance"]
        final_archives = [json.loads(p.read_text()) for p in (root / "phase_archive/objects").rglob("*.json")]
        final = next(d for d in final_archives if d["phase"] == "recovery" and d["summary"].get("final_recovery_directive"))
        counts = final["summary"]["business_budget_counters"]
        assert counts["turn_count"] == 2 and counts["tool_call_count"] == 1, counts
        assert final["summary"]["compaction_maintenance"] == before["maintenance"]
        assert sum(d["phase"] == "recovery-context-epoch" for d in final_archives) == 1
        (root / "restart_after.json").write_text(json.dumps({"pid": os.getpid(), "active": before["active"],
            "counts": counts, "maintenance": before["maintenance"], "business": business.calls,
            "compactor_calls": 0}, indent=2))
    if stage in {"prepare", "both"}: asyncio.run(prepare())
    if stage in {"resume", "both"}: asyncio.run(resume())


def test_real_cas_competitor_leaves_candidate_inactive(tmp_path):
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path, material_chars=1100000)
        original = fixture.reload()
        compactor, business = CapturedChat(HierarchicalResponses()), CapturedChat(read_current_attempt)
        agent = adapter(tmp_path, business, compactor)
        raced = []
        def event(kind, payload):
            agent._schema2_recovery_event_sink(kind, payload)
            if kind == "recovery_compaction_started" and not raced:
                other = fixture.reload()
                other["task_state"]["s4_concurrent_writer"] = {"owner": "controlled independent writer"}
                save_plan_state(str(tmp_path), fixture.task_id, other)
                raced.append(fixture.reload()["state_generation"])
        node = Schema2RecoveryNode(data_path=str(tmp_path), model=business, compactor_model=compactor,
            compactor=agent._schema2_context_compactor_invoke, provider_invoke=agent._schema2_recovery_provider_invoke,
            event_sink=event, context_budget_policy=large_policy())
        result = await node.run({"task_id": fixture.task_id})
        assert result["recovery_outcome"]["payload"]["error_code"] == "RECOVERY_CONTEXT_EPOCH_CAS_CONFLICT", result
        current = fixture.reload()
        assert not current["task_state"].get("recovery_context_compaction")
        assert current["task_state"]["s4_concurrent_writer"] and not business.calls
        assert current["provenance_state"] == original["provenance_state"]
        ref = result["recovery_outcome"]["payload"]["identity_conflict"]["recovery_archive_ref"]
        archive = PhaseArchiveStore(tmp_path).read(ref)
        assert archive.phase == "recovery-context-epoch" and archive.metadata["archive_before_activate"]
        (tmp_path / "cas_receipt.json").write_text(json.dumps({"result": result, "racing_generation": raced,
            "compactor": compactor.calls, "business_calls": 0}, default=str, indent=2))
    asyncio.run(scenario())


def test_closure_in_complete_budget_and_actual_cap(tmp_path):
    from agent_core.workflow.recovery_runtime import RecoveryResourcePolicy
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        def response(messages, row, ordinal):
            assert "<recovery_resource_closure>" in str(messages[-1].content)
            metadata = row["provider_config"]["keys"]
            assert metadata
            return blocked(tag(messages, "recovery_seed"), "s4-closure")
        business = CapturedChat(response).bind(max_tokens=3000)
        compactor = CapturedChat(lambda *a: pytest.fail("Safe closure requires no compaction")).bind(max_tokens=2000)
        node = make_node(tmp_path, business, compactor=compactor,
            resource_policy=RecoveryResourcePolicy(), loop_config=RecoveryAgentLoopConfig(max_llm_turns=1))
        result = await node.run({"task_id": fixture.task_id})
        assert len(business.root.calls) == 1, result
        call = business.root.calls[0]
        assert call["max_tokens"] == 2000
        archives = [json.loads(p.read_text()) for p in (tmp_path / "phase_archive/objects").rglob("*.json")]
        episode = next(d for d in archives if d["phase"] == "recovery" and d["summary"].get("final_recovery_directive"))
        snapshot = episode["summary"]["provider_observability"]["context_budget_checks"][-1]
        assert snapshot["provider_message_count"] == len(call["messages"])
        assert snapshot["budget"]["estimated_input_tokens"] == call["budget"]["estimated_input_tokens"]
        assert snapshot["budget"]["output_reserve_tokens"] == call["max_tokens"]
        (tmp_path / "closure_receipt.json").write_text(json.dumps(call, indent=2))
    asyncio.run(scenario())


def test_adapter_keeps_response_character_limit(tmp_path):
    from agent_core.workflow.recovery_runtime import RecoveryResourcePolicy
    async def scenario():
        fixture, _, _ = await empty_failure_fixture(tmp_path)
        model = CapturedChat(lambda *_: AIMessage(content="x" * 4096))
        node = make_node(tmp_path, model, resource_policy=replace(RecoveryResourcePolicy(), receive_char_limit=128))
        result = await node.run({"task_id": fixture.task_id})
        assert len(model.calls) == 1
        assert "PROVIDER_OUTPUT_LIMIT" in json.dumps(result, default=str), result
        (tmp_path / "receive_limit_receipt.json").write_text(json.dumps(result, default=str, indent=2))
    asyncio.run(scenario())


def test_observation_object_cap_before_decode_and_legacy_archive_unchanged(tmp_path):
    from agent_core.workflow.inspection_evidence import MAX_INSPECTION_OBJECT_BYTES
    fixture = make_inspection_fixture(tmp_path, task_id="task-recovery")
    asyncio.run(fixture.inspect_handoff())
    _, seed, _, dispatcher = reopen(tmp_path)
    phase = PhaseArchiveStore(tmp_path)
    archived = RecoveryObservationStore(phase).externalize(seed=seed, request_id="oversized",
        tool="failure.inspect", source_ref=seed.failure_packet.source_outcome_ref,
        result={"content": "Y" * MAX_INSPECTION_OBJECT_BYTES})
    with pytest.raises(ValueError, match="INSPECTION_OBJECT_SIZE_LIMIT"):
        dispatcher.dispatch_full("evidence.read", {"evidence_ref": archived["observation_ref"], "start_line": 1, "end_line": 1})
    # No new global phase-archive limit is introduced.
    assert phase.read(archived["observation_ref"]).summary["result"]["content"] == "Y" * MAX_INSPECTION_OBJECT_BYTES


def test_direct_callable_requires_actual_cap_and_supported_leaf_receives_it():
    from agent_core.workflow.recovery_runtime import recovery_direct_invoke, recovery_dispatch_budget
    from langchain_core.messages import SystemMessage, HumanMessage
    seen = []
    async def unsupported(messages): seen.append("unsupported"); return AIMessage(content="must not run")
    async def supported(messages, *, max_tokens): seen.append(max_tokens); return AIMessage(content="OK")
    policy = ContextBudgetPolicy(output_reserve_tokens=1024)
    async def scenario():
        with recovery_dispatch_budget(policy, ReferenceTokenEstimator(), stage="recovery"):
            wire = [SystemMessage(content="system"), HumanMessage(content="seed")]
            with pytest.raises(ValueError, match="RECOVERY_OUTPUT_CAP_UNSUPPORTED"):
                await recovery_direct_invoke(unsupported, wire, stage="recovery")
            assert not seen
            result = await recovery_direct_invoke(supported, wire, stage="recovery")
            assert result.content == "OK" and seen == [1024]
    asyncio.run(scenario())


def test_external_observation_new_process_next_page(tmp_path):
    import os
    phase = os.environ.get("S4_RESTART_PHASE", "both")
    root = Path(os.environ.get("S4_RESTART_ROOT", str(tmp_path))) / "external_reader"
    root.mkdir(parents=True, exist_ok=True)
    if phase in {"prepare", "both"}:
        fixture = make_inspection_fixture(root, task_id="task-recovery")
        asyncio.run(fixture.inspect_handoff())
        _, seed, _, dispatcher = reopen(root)
        ref = RecoveryObservationStore(PhaseArchiveStore(root)).externalize(seed=seed,
            request_id="cross-process-observation", tool="failure.inspect",
            source_ref=seed.failure_packet.source_outcome_ref,
            result={"returncode": 0, "validation_passed": False, "Q_correspondence": "unknown",
                "exact_body": "BEGIN|" + "observation boundary\n" * 12000 + "|END"})["observation_ref"]
        first = dispatcher.dispatch_full("evidence.read", {"evidence_ref": ref, "start_line": 1, "end_line": 8})
        assert first["cursor"]["next_start_line"] == 9
        (root / "first_page.json").write_text(json.dumps({"ref": ref, "page": first,
            "bootstrap_root": str(__import__("__main__").RUNROOT)}, indent=2))
    if phase in {"resume", "both"}:
        previous = json.loads((root / "first_page.json").read_text())
        if phase == "resume": assert previous["bootstrap_root"] != str(__import__("__main__").RUNROOT)
        _, seed, _, dispatcher = reopen(root)
        later = dispatcher.dispatch_full("evidence.read", {"evidence_ref": previous["ref"], "start_line": 9, "end_line": 16})
        assert later["digest"] == previous["page"]["digest"]
        assert later["scope"] == previous["page"]["scope"]
        assert later["start_line"] == 9 and later["cursor"]["next_start_line"] == later["end_line"] + 1
        assert later["content"] != previous["page"]["content"]
        assert len(list((root / "phase_archive/objects").rglob("*.json"))) == 1
        (root / "later_page.json").write_text(json.dumps({"page": later,
            "bootstrap_root": str(__import__("__main__").RUNROOT)}, indent=2))

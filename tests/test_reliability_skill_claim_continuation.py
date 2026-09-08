"""Recovery Skill continuation at real Generate, service, cache and store seams.

Providers return recorded strings; no command or scientific process is run.
The old shared Generate fixture's failed attempt is explicitly recorded through
production provenance before its existing typed Recovery handoff is used.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import load_plan_state, record_provenance_event, save_plan_state
from agent_core.recovery import RecoveryStateStore
from tests import test_generate_session_1788681718_production_path as shared


SKILL_REQUEST = {
    "decision": "REQUEST_SKILL_CONTEXT",
    "reason": "Read the currently selected Skill's notes before repairing the approved action.",
    "requests": [{"skill_id": shared.SKILL_ID, "operation": "resource",
                  "resource_path": "references/snpeff_notes.md"}],
}
SKILL_RAW = json.dumps(SKILL_REQUEST, ensure_ascii=False, sort_keys=True)


def _budgets(state):
    task = state.get("task_state") or {}
    return {key: deepcopy(task.get(key)) for key in
            ("budgets", "typed_recovery_budget", "candidate_repair_ledger")}


def _objects(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        if decoded != value:
            yield from _objects(decoded)


async def _fixture():
    fixture = shared.Session1788681718GenerateProductionPathTests(
        methodName="test_adm_01_saved_candidate_reaches_fake_executor_once")
    await fixture.asyncSetUp()
    try:
        task = "reliability-skill-claim"
        state = fixture._approved_generate_state(task)
        durable = load_plan_state(fixture.root, task)
        step = durable["current_step_id"]
        action = {"action_id": "failed-action-external", "step_id": step,
                  "plan_version": durable["plan_version"], "type": "execute",
                  "command_role": "required_validation", "purpose": "validation",
                  "code": "printf 'recorded failure only\\n'"}
        record_provenance_event(durable, "action_bound", {"action": action, "raw_action": action})
        attempt = record_provenance_event(durable, "attempt_started", {
            "action_id": action["action_id"], "step_id": step,
            "attempt_id": "failed-attempt-external"})
        execution = {"task_id": task, "plan_id": durable["plan_id"],
                     "plan_version": durable["plan_version"], "step_id": step,
                     "action_id": action["action_id"], "attempt_id": attempt["attempt_id"],
                     "execution_id": "failed-execution-external", "returncode": 17,
                     "failure_fingerprint": "failure-external-script-fixture",
                     "code": action["code"], "raw_stdout": "", "raw_stderr": "recorded fixture failure"}
        record_provenance_event(durable, "execution_finished", {
            "attempt_id": attempt["attempt_id"], "execution_record": execution})
        save_plan_state(fixture.root, task, durable)
        state, decision, envelope, target = await fixture._install_recovery_handoff(state, external=False)
        fixture.provider.responses = [SKILL_RAW, fixture.saved_response_content]
        fixture.provider.calls.clear()
        actual_provider = fixture.provider.ainvoke
        async def recorded_provider_metadata(messages, **kwargs):
            response = await actual_provider(messages, **kwargs)
            ordinal = len(fixture.provider.calls)
            response.id = "recorded-response-%d" % ordinal
            response.response_metadata = {
                "finish_reason": "stop", "provider_request_id": "recorded-request-%d" % ordinal,
                "recorded_provider_metadata": {"ordinal": ordinal},
            }
            return response
        fixture.provider.ainvoke = recorded_provider_metadata
        return fixture, state, decision, envelope, target
    except BaseException:
        await fixture.asyncTearDown()
        raise


async def _serve(fixture, state, *, cancel_before_handler=False, evidence_dir=None):
    captured = []
    before = []
    cache_reads = []
    real_cache_load = RecoveryStateStore.load_llm_result
    def observed_cache_load(store, *args, **kwargs):
        value = real_cache_load(store, *args, **kwargs)
        cache_reads.append({"context_stage": list(args),
            "cache_identity": deepcopy(kwargs.get("cache_identity") or {}),
            "hit": bool(value.get("raw_response")),
            "raw_sha256": hashlib.sha256(str(value.get("raw_response") or "").encode()).hexdigest()})
        return value
    def record(name, value):
        if evidence_dir is not None:
            (evidence_dir / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2))
    real_handler = fixture.agent._handle_skill_node_decision
    async def handler(**kwargs):
        captured.append(kwargs)
        before.append(load_plan_state(fixture.root, state["task_id"]))
        if cancel_before_handler and len(captured) == 1:
            raise asyncio.CancelledError("offline checkpoint after raw response, before Skill transaction")
        return await real_handler(**kwargs)
    real_serve = fixture.service.serve_context_request
    served = []
    def counted(*args, **kwargs):
        result = real_serve(*args, **kwargs)
        served.append(deepcopy(result[1]))
        return result
    with patch.object(fixture.agent, "_handle_skill_node_decision", handler), patch.object(
            fixture.service, "serve_context_request", counted), patch.object(
                RecoveryStateStore, "load_llm_result", observed_cache_load):
        generate = fixture.agent._schema2_runtime_node_handlers["generate"]
        if cancel_before_handler:
            with pytest.raises(asyncio.CancelledError, match="offline checkpoint"):
                await generate(deepcopy(state))
            checkpoint = load_plan_state(fixture.root, state["task_id"])
            assert checkpoint["task_state"]["recovery_state"]["generation_claim"]["status"] == "provider_completed"
            original_response = captured[0]["response"]
            assert original_response.id == "recorded-response-1"
            assert original_response.response_metadata["recorded_provider_metadata"] == {"ordinal": 1}
            assert len(fixture.provider.calls) == 1 and not served
            raw_entries = json.loads((RecoveryStateStore(fixture.root, state["task_id"]).root / "llm_results.json").read_text())
            cached = next(row for row in raw_entries.values() if row.get("raw_response") == SKILL_RAW)
            assert cached["cache_identity"]["target_generation_id"] == captured[0]["recovery_skill_completion"]["target_generation_id"]
            assert cached["cache_identity"]["provider_request_digest"] == captured[0]["recovery_skill_completion"]["provider_request_digest"]
            assert cached["candidate_fingerprint"] == ""
            record("checkpoint-before-skill-handler", {
                "cached_response": cached, "generation_claim": checkpoint["task_state"]["recovery_state"]["generation_claim"],
                "episode": checkpoint["task_state"].get("generate_context_episode"),
                "provider_calls": len(fixture.provider.calls), "served_count": len(served)})
        first = await generate(deepcopy(state))
        record("skill-handler-result", {"generate_outcome": first.get("generate_outcome"),
            "cache_reads": cache_reads, "provider_calls": len(fixture.provider.calls),
            "served_count": len(served)})
        assert first["generate_outcome"]["type"] == "skill_context_served", first
        assert len(fixture.provider.calls) == 1, "checkpoint resume must adopt cached Skill response without a provider replay"
        assert len(served) == 1
        durable = load_plan_state(fixture.root, state["task_id"])
        cid = durable["task_state"]["skill_continuation_id"]
        assert durable["task_state"]["skill_access_state"]["skill_tool_results"][cid] == served[0]
        budgets, generation = _budgets(durable), durable["state_generation"]
        rounds = deepcopy(durable["task_state"]["skill_access_state"]["context_rounds"])
        duplicate = await real_handler(**captured[-1])
        assert duplicate["generate_exit_reason"] == "skill_completion_already_adopted"
        assert len(served) == 1
        replay = load_plan_state(fixture.root, state["task_id"])
        assert replay["state_generation"] == generation
        assert replay["task_state"]["skill_access_state"]["context_rounds"] == rounds
        assert _budgets(replay) == budgets
    return first, captured[-1], before[-1], durable, served[0]


@pytest.mark.parametrize("cancel_before_handler", [False, True], ids=["continuous", "reload_completed_response"])
def test_actual_generate_skill_continuation_reloads_and_calls_next_provider_once(tmp_path, cancel_before_handler):
    async def run():
        fixture, state, decision, envelope, old_target = await _fixture()
        try:
            baseline = load_plan_state(fixture.root, state["task_id"])
            first, captured, before, durable, result = await _serve(
                fixture, state, cancel_before_handler=cancel_before_handler, evidence_dir=tmp_path)
            recovery = durable["task_state"]["recovery_state"]
            assert recovery["generation_claim"]["status"] == "issued"
            assert recovery["generation_claim"]["parent_target_generation_id"] == old_target
            assert recovery["generation_claim"]["target_generation_id"] != old_target
            assert recovery["active_recovery"]["recovery_id"] == decision.decision_id
            assert recovery["active_recovery"]["envelope_id"] == envelope.immutable_hash
            assert _budgets(durable) == _budgets(before) == _budgets(baseline)
            history = recovery["generation_claim_history"]
            row = next(item for item in history if item.get("skill_completion_event_id"))
            assert row["status"] == "superseded"
            assert row["skill_completion"]["response_digest"] == hashlib.sha256(SKILL_RAW.encode()).hexdigest()
            exact = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            assert row["skill_completion"]["result_digest"] == hashlib.sha256(exact.encode()).hexdigest()
            second = await fixture.agent._schema2_runtime_node_handlers["generate"]({**state, **first})
            assert second["generate_outcome"]["type"] == "action_ready", second
            assert len(fixture.provider.calls) == 2
            notes = result["results"][0]["exact_text"]
            assert any(isinstance(value, str) and notes.strip() in value
                       for value in _objects(fixture.provider.calls[-1])), "exact served Skill result must enter the next real provider request"
            final = load_plan_state(fixture.root, state["task_id"])
            assert final["task_state"]["recovery_state"]["generation_claim"]["status"] == "action_adopted"
            assert len(final["provenance_state"]["attempts"]) == 1, "Generate must not launch or replay the failed attempt"
            (tmp_path / "skill-claim-receipt.json").write_text(json.dumps({
                "completion": captured["recovery_skill_completion"], "claim_history": history,
                "new_claim": recovery["generation_claim"], "served_result": result,
                "provider_requests": fixture.provider.calls, "provider_calls": 2,
                "checkpoint_resume": cancel_before_handler, "scientific_commands": 0,
            }, ensure_ascii=False, indent=2))
        finally:
            await fixture.asyncTearDown()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["step_id", "task_revision", "plan_revision"])
def test_skill_completion_stale_handler_is_rejected_before_service(tmp_path, field):
    async def run():
        fixture, state, _decision, _envelope, _target = await _fixture()
        try:
            _first, captured, _before, durable, _result = await _serve(fixture, state)
            context = captured["node_context"]
            old = getattr(context.binding, field)
            changed = (old + 1) if isinstance(old, int) else "unrelated-step"
            kwargs = {**captured, "node_context": replace(context, binding=replace(context.binding, **{field: changed}))}
            with patch.object(fixture.service, "serve_context_request", side_effect=AssertionError("stale service must not run")) as service:
                update = await fixture.agent._handle_skill_node_decision(**kwargs)
            assert service.call_count == 0
            assert field in update["stale_node_result"]["changed_preconditions"]
            after = load_plan_state(fixture.root, state["task_id"])
            assert after["state_generation"] == durable["state_generation"]
            assert after["task_state"]["recovery_state"] == durable["task_state"]["recovery_state"]
            assert _budgets(after) == _budgets(durable)
        finally:
            await fixture.asyncTearDown()
    asyncio.run(run())


def test_skill_and_claim_transaction_rejects_stale_cas_without_partial_result(tmp_path):
    async def run():
        fixture, state, _decision, _envelope, _target = await _fixture()
        try:
            _first, captured, before, durable, _result = await _serve(fixture, state)
            with pytest.raises(StateCASConflict, match="SKILL_ACCESS_TRANSACTION_STALE"):
                fixture.agent._persist_durable_skill_access(state["task_id"],
                    durable["task_state"]["skill_access_state"],
                    continuation_id=durable["task_state"]["skill_continuation_id"],
                    recovery_skill_completion=captured["recovery_skill_completion"],
                    expected_state_generation=before["state_generation"])
            after = load_plan_state(fixture.root, state["task_id"])
            assert after["state_generation"] == durable["state_generation"]
            assert after["task_state"]["recovery_state"] == durable["task_state"]["recovery_state"]
            assert after["task_state"]["skill_access_state"] == durable["task_state"]["skill_access_state"]
            assert _budgets(after) == _budgets(durable)
        finally:
            await fixture.asyncTearDown()
    asyncio.run(run())

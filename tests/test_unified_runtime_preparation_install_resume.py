"""A verified installation can reopen one terminal, undispatched runtime scope."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from agent_core.action_runtime import capability_contract_hash, stable_digest
from agent_core.plan_tracker import build_plan_state, load_plan_state, save_plan_state, approve_plan_state
from agent_core.runtime_evidence import digest, evaluate_runtime_evidence
from agent_core.step_effects import canonical_step_effect
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.workflow.runtime_evidence_recovery import (
    RuntimeEvidenceRecovery, runtime_generation_source_scope, installation_generation_replay,
)


def _fixture(root, initial="UNKNOWN", after="AVAILABLE", *, partial=False):
    task_id, sid = "same-prefix-install-resume", "fixture/runtime"
    required = ["runtime.tool.bin"] + (["runtime.second.bin"] if partial else [])
    contract = {"capability_id": "fixture.runtime.execute", "selected_skill_id": sid,
        "required_runtime_binding_ids": required, "optional_runtime_binding_ids": [], "required_artifact_binding_ids": []}
    task = create_task_spec("Record a key with the declared runtime.", task_id=task_id, base_dir=root, project_root=root)
    step = {"step_id": "science", "title": "Record key", "action": "execute", "purpose": "analysis", "tool": "python",
        "skill_ids": [sid], "workflow_stage_ids": [], "batch_id": "", "dependencies": [], "inputs": [], "outputs": [],
        "produces_bindings": ["key"], "operation": "choose_key", "execution_capability_id": contract["capability_id"],
        "risk_level": "low", "success_criteria": ["Record the chosen key"]}
    plan = build_plan_state(task_id, json.dumps({"steps": [step], "validation_checklist": [],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}), strict=True)
    plan["steps"][0]["capability_contract_hash"] = capability_contract_hash(contract)
    plan["steps"][0]["step_effect"] = canonical_step_effect(plan["steps"][0])
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True,
        current_task={"task_spec": task, "task_revision": task_spec_revision(task)}, selected_skill_ids=[sid])
    plan["task_state"].update(workflow_routing_schema_version=2, selected_skills=[sid])
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True)
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source="offline_explicit_plan")
    save_plan_state(root, task_id, plan)
    prefix = root / "envs/chosen"
    (prefix / "bin").mkdir(parents=True)
    tool = prefix / "bin/tool"
    subject = {key: digest(key) for key in ("skill_document_digest", "skill_contract_digest", "inspection_contract_digest")}
    subject.update(skill_id=sid, environment_id="chosen", host_id="offline-host",
        environment_fingerprint=digest({"path": str(prefix), "inode": prefix.stat().st_ino}))
    now, phase, calls = [datetime.now(timezone.utc)], ["before"], []

    async def inspect(request):
        calls.append(deepcopy(request))
        availability = initial if phase[0] == "before" else after
        if availability == "ERROR":
            return {"inspection_status": "execution_error", "reason_code": "FIXTURE_PROBE_FAILED"}
        covered = required if phase[0] == "before" else ["runtime.tool.bin"]
        coverage = {bid: {"availability": availability, "output_name": "RESOLVED_" + bid.removeprefix("runtime.").replace(".", "_").upper(),
            "value_kind": "executable_path", "conclusion_complete": availability != "UNKNOWN",
            "reason_code": "FIXTURE_CONFIRMED_MISSING" if availability == "UNAVAILABLE" else ""} for bid in covered}
        return {"inspection_status": "succeeded", "observed_at": now[0].isoformat(),
            "audit": {"skill_id": sid, "env": "chosen", "status": "ready", "resolved_runtime_paths":
                {"RESOLVED_TOOL_BIN": str(tool)} if availability == "AVAILABLE" else {}}, "coverage": coverage}

    service = RuntimeEvidenceRecovery(data_path=str(root), scope_provider=lambda _: deepcopy(subject), inspect_runtime=inspect,
        clock=lambda: now[0], inspection_contract_provider=lambda _: {"contract_id": "fixture-fixed-probe", "contract_digest": digest("probe"),
            "method_digest": subject["inspection_contract_digest"], "permitted": True, "safe_repeat": True,
            "coverage_binding_ids": required, "cache_restore_permitted": False})
    def request():
        return service.create_request({"task_id": task_id}, selected_skill_id=sid, capability_contract=contract,
            missing_binding_ids=required, original_error={"code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"})
    first = request()
    first_result = asyncio.run(service.recover({"task_id": task_id}, first))
    assert first_result["status"] == ("dependency_blocked" if initial == "UNAVAILABLE" else "unresolved"), first_result
    return task_id, sid, contract, service, request, first, prefix, tool, now, phase, calls


def _verified_work_order(root, task_id, sid, contract, prefix):
    plan = load_plan_state(root, task_id)
    request = {"request_id": "explicit-install-work-order", "attempt_id": "install-attempt", "target_env": "chosen",
        "target_prefix": str(prefix), "selected_skills": [sid], "capabilities": [contract["capability_id"]],
        "generation_source_scope": runtime_generation_source_scope(plan)}
    receipt = {**deepcopy(request), "request_hash": stable_digest(request), "status": "available"}
    plan["task_state"].update(installation_request=request, installation_receipt=receipt)
    save_plan_state(root, task_id, plan)
    return request, receipt


@pytest.mark.parametrize("initial", ["UNKNOWN", "UNAVAILABLE"])
def test_same_prefix_verified_installation_reopens_once_without_any_execution_attempt(tmp_path, initial):
    tid, sid, contract, service, request, first, prefix, tool, now, phase, calls = _fixture(tmp_path, initial)
    before = load_plan_state(tmp_path, tid)
    old_episode = deepcopy(before["task_state"]["runtime_recovery"]["episodes"][first["episode_id"]])
    old_records = deepcopy(before["task_state"]["runtime_evidence"]["records"])
    grant, receipt = _verified_work_order(tmp_path, tid, sid, contract, prefix)
    inode = prefix.stat().st_ino
    tool.write_text("verified fixture; not executed here")
    tool.chmod(0o755)
    phase[0] = "after"
    now[0] += timedelta(seconds=1)
    prepared = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    assert prepared["runtime_state"]["execution_epoch"] == before["runtime_state"]["execution_epoch"] + 1
    assert not prepared["provenance_state"]["attempts"]
    event = prepared["task_state"]["recovery_state"]["state_events"][-1]
    assert event["retired_attempt_ids"] == [] and event["authority"] == "installation_receipt"
    assert installation_generation_replay(prepared, grant, receipt) == event
    second = request()
    assert second["request_id"] != first["request_id"]
    assert second["scope"]["subject"] == first["scope"]["subject"]
    assert prefix.stat().st_ino == inode
    result = asyncio.run(service.recover({"task_id": tid}, second))
    assert result["status"] == "evidence_ready", result
    after = load_plan_state(tmp_path, tid)
    assert after["task_state"]["runtime_recovery"]["episodes"][first["episode_id"]] == old_episode
    assert after["task_state"]["runtime_evidence"]["records"][:len(old_records)] == old_records
    assert len(calls) == 2 and not after["provenance_state"]["attempts"]
    assert service.prepare_generation({"task_id": tid}, selected_skill_id=sid,
        capability_contract=contract)["runtime_state"]["execution_epoch"] == prepared["runtime_state"]["execution_epoch"]
    assert request() == second
    assert asyncio.run(service.recover({"task_id": tid}, second))["status"] == "evidence_ready"
    assert len(calls) == 2


@pytest.mark.parametrize("mutation", ["no_receipt", "not_available", "wrong_hash", "stale_source", "wrong_target"])
def test_ordinary_or_unverified_installation_cannot_reset_terminal_runtime_budget(tmp_path, mutation):
    tid, sid, contract, service, request, first, prefix, tool, now, phase, calls = _fixture(tmp_path)
    _verified_work_order(tmp_path, tid, sid, contract, prefix)
    plan = load_plan_state(tmp_path, tid)
    if mutation == "no_receipt": plan["task_state"]["installation_receipt"] = {}
    elif mutation == "not_available": plan["task_state"]["installation_receipt"]["status"] = "verification_failed"
    elif mutation == "wrong_hash": plan["task_state"]["installation_receipt"]["request_hash"] = "wrong"
    else:
        grant = plan["task_state"]["installation_request"]
        if mutation == "stale_source": grant["generation_source_scope"]["execution_epoch"] = "999"
        else: grant["target_env"] = "other"
        plan["task_state"]["installation_receipt"] = {**deepcopy(grant), "request_hash": stable_digest(grant), "status": "available"}
    save_plan_state(tmp_path, tid, plan)
    prepared = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    assert prepared["runtime_state"]["execution_epoch"] == plan["runtime_state"]["execution_epoch"]
    assert request() == first and len(calls) == 1


@pytest.mark.parametrize("after,partial", [("UNKNOWN", False), ("ERROR", False), ("AVAILABLE", True)])
def test_reinspection_failure_unknown_or_partial_result_preserves_negative_facts(tmp_path, after, partial):
    tid, sid, contract, service, request, first, prefix, tool, now, phase, calls = _fixture(tmp_path, "UNAVAILABLE", after, partial=partial)
    _verified_work_order(tmp_path, tid, sid, contract, prefix)
    now[0] += timedelta(seconds=1)
    phase[0] = "after"
    tool.write_text("fixture")
    tool.chmod(0o755)
    service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    second = request()
    result = asyncio.run(service.recover({"task_id": tid}, second))
    assert result["status"] == "dependency_blocked", result
    facts = evaluate_runtime_evidence(load_plan_state(tmp_path, tid)["task_state"], task_id=tid,
        skill_scope=second["scope"]["subject"], now=now[0].isoformat())
    assert facts["runtime.second.bin" if partial else "runtime.tool.bin"]["availability"] == "UNAVAILABLE"
    if partial: assert facts["runtime.tool.bin"]["availability"] == "AVAILABLE"
    assert len(calls) == 2


def test_consumed_work_order_replay_does_not_cover_a_later_source(tmp_path):
    tid, sid, contract, service, request, first, prefix, tool, now, phase, calls = _fixture(tmp_path)
    grant, receipt = _verified_work_order(tmp_path, tid, sid, contract, prefix)
    prepared = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    assert installation_generation_replay(prepared, grant, receipt)
    later = deepcopy(prepared)
    later["provenance_state"]["attempts"]["later"] = {"attempt_id": "later", "step_id": "science"}
    assert installation_generation_replay(later, grant, receipt) == {}
    changed = deepcopy(prepared)
    changed["runtime_state"]["execution_epoch"] += 1
    assert installation_generation_replay(changed, grant, receipt) == {}
    owned = deepcopy(prepared)
    owned["task_state"]["active_execution"] = {"attempt_id": "new-owner"}
    assert installation_generation_replay(owned, grant, receipt) == {}


@pytest.mark.parametrize("initial", ["UNKNOWN", "UNAVAILABLE"])
def test_new_installation_epoch_keeps_previously_retired_attempts_inert(tmp_path, initial):
    from tests.test_unified_runtime_preparation import _recovery_fixture
    from agent_core.workflow.runtime_evidence_recovery import _not_dispatched
    tid, sid, contract, service, original, aid, _ = _recovery_fixture(tmp_path)
    clock = [datetime.now(timezone.utc)]
    service.clock = lambda: clock[0]
    phase, calls = ["before"], []
    prefix = tmp_path / "envs/chosen"
    (prefix / "bin").mkdir(parents=True)
    tool = prefix / "bin/tool"

    async def probe(request):
        calls.append(deepcopy(request))
        availability = initial if phase[0] == "before" else "AVAILABLE"
        return {"inspection_status": "succeeded", "observed_at": clock[0].isoformat(),
            "audit": {"skill_id": sid, "env": "chosen", "status": "ready",
                "resolved_runtime_paths": {"RESOLVED_TOOL_BIN": str(tool)} if availability == "AVAILABLE" else {}},
            "coverage": {"runtime.tool.bin": {"availability": availability, "output_name": "RESOLVED_TOOL_BIN",
                "value_kind": "executable_path", "conclusion_complete": availability != "UNKNOWN",
                "reason_code": "CONFIRMED_MISSING" if availability == "UNAVAILABLE" else ""}}}
    service.inspect_runtime = probe
    prepared = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    prior_attempts = deepcopy(prepared["provenance_state"]["attempts"])
    prior_actions = deepcopy(prepared["provenance_state"]["actions"])
    def request():
        return service.create_request({"task_id": tid}, selected_skill_id=sid, capability_contract=contract,
            missing_binding_ids=["runtime.tool.bin"], original_error={"code": "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"})
    first = request()
    result = asyncio.run(service.recover({"task_id": tid}, first))
    assert result["status"] == ("dependency_blocked" if initial == "UNAVAILABLE" else "unresolved")
    _verified_work_order(tmp_path, tid, sid, contract, prefix)
    tool.write_text("fixture")
    tool.chmod(0o755)
    clock[0] += timedelta(seconds=1)
    phase[0] = "after"
    refreshed = service.prepare_generation({"task_id": tid}, selected_skill_id=sid, capability_contract=contract)
    assert refreshed["runtime_state"]["execution_epoch"] == prepared["runtime_state"]["execution_epoch"] + 1
    assert refreshed["provenance_state"]["attempts"] == prior_attempts
    assert refreshed["provenance_state"]["actions"] == prior_actions
    assert refreshed["task_state"]["recovery_state"]["state_events"][-1]["retired_attempt_ids"] == [aid]
    assert _not_dispatched(refreshed, "science")
    second = request()
    assert second["scope"]["subject"] == first["scope"]["subject"]
    assert asyncio.run(service.recover({"task_id": tid}, second))["status"] == "evidence_ready"
    assert len(calls) == 2

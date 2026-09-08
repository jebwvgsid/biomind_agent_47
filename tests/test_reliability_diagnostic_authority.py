"""Scope regression tests using durable legacy authority and actual step commits.

External Execute is a recorded substitute; no command is run. The late-cursor
case explicitly exercises the existing transition/store seam after a real first
CommitStep, and does not claim a second scientific execution was verified.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import shlex

import pytest

from agent_core.controller_audit import adapt_script_block_to_action, parse_executable_script_block
from agent_core.plan_tracker import (
    advance_plan_after_verified_step, commit_recovery_bundle, save_plan_state,
)
from agent_core.recovery import (
    RecoveryAction, RecoveryCommitBundle, RecoveryDecision, RecoverySourceScope,
    RecoveryStateStore, build_recovery_execution_envelope, finalize_recovery_decision,
    validate_diagnostic_action, validate_diagnostic_probe_code,
)
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.diagnostic_admission import (
    diagnostic_authority_family, diagnostic_binding_subset,
    validate_diagnostic_receipt_action,
)
from agent_core.workflow.execution import TypedExecute
from agent_core.workflow.outcomes import ExecuteOutcome, RecoveryOutcome
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder
from agent_core.workflow.stage_contracts import StageContractError
from tests.test_inspection_workflow import make_inspection_fixture


def _committed_legacy_probe(root):
    fixture = make_inspection_fixture(root, execution_returncode=17)
    durable, execution = fixture.reload(), fixture.execution
    decision = finalize_recovery_decision(
        RecoveryDecision.from_mapping({
            "schema_version": 5, "failure_class": "TOOL_OR_ENV_FAILURE",
            "failure_owner": "RUNTIME",
            "recommended_action": RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
            "reason": "Read a bounded approved input header",
            "root_cause": "Recorded failure needs inspection",
            "evidence": [execution["execution_id"]], "missing_evidence": ["input header"],
            "probe_objective": "Read a bounded approved header", "confidence": 0.9,
        }),
        source_scope=RecoverySourceScope(
            task_id=fixture.task_id, plan_id=durable["plan_id"],
            plan_version=durable["plan_version"], step_id=execution["step_id"],
            failed_action_id=execution["action_id"],
            failed_execution_attempt_id=execution["attempt_id"],
            source_failure_fingerprint="legacy-failure",
        ), recovery_context_fingerprint="legacy-diagnostic-context",
        source_node="execute", expected_state_generation=durable["state_generation"],
    )
    envelope = build_recovery_execution_envelope(
        decision, recovery_context_id="legacy-diagnostic-context",
        step_id=execution["step_id"],
        task_root=RecoveryStateStore(root, fixture.task_id).root.parent,
        allowed_input_roots=[str(root / "inputs")],
    )
    committed = commit_recovery_bundle(root, fixture.task_id, RecoveryCommitBundle(
        decision, envelope, {}, {}, None, durable["state_generation"],
    ))
    assert committed["applied"] is True
    saved = fixture.reload()
    assert saved["task_state"]["recovery_state"]["active_recovery"]["recovery_id"] == decision.decision_id
    assert not saved["task_state"].get("typed_recovery_receipts", {}).get(decision.decision_id)
    command = "head -n 4 " + shlex.quote(str(root / "inputs" / "tiny.vcf"))
    action = adapt_script_block_to_action(
        parse_executable_script_block("<shell>" + command + "</shell>"),
        execution_envelope=envelope.to_dict(), recovery_generated=True,
    )
    return fixture, decision, envelope, action


def test_committed_legacy_probe_keeps_existing_validators_and_typed_execute_return(tmp_path):
    async def run():
        fixture, decision, envelope, action = _committed_legacy_probe(tmp_path)
        saved = fixture.reload()
        assert diagnostic_authority_family(action, saved) == "legacy_committed"
        assert validate_diagnostic_receipt_action(action, saved) == {"authority_family": "legacy_committed"}
        bindings = {"existing-binding": object()}
        assert diagnostic_binding_subset(action, saved, bindings) is bindings
        assert validate_diagnostic_action(action) == (True, "")
        invalid = "head -n 4 " + shlex.quote(str(tmp_path / "inputs" / "tiny.vcf")) + " > " + shlex.quote(str(tmp_path / "outside-scratch.txt"))
        assert validate_diagnostic_probe_code(invalid, envelope.scratch_capability)[0] is False
        result = {
            "task_id": fixture.task_id, "plan_id": saved["plan_id"],
            "plan_version": saved["plan_version"], "step_id": fixture.execution["step_id"],
            "action_id": envelope.action_id, "attempt_id": "recorded-legacy-probe-attempt",
            "command_role": "diagnostic", "returncode": 0, "status": "recorded",
        }
        calls = []
        async def executor(state):
            calls.append(deepcopy(state))
            return {"execution_evidence": result, "diagnostic_evidence": result}
        returned = await TypedExecute(executor=executor, data_path=str(tmp_path)).run({
            "task_id": fixture.task_id, "pending_action": action,
        })
        assert len(calls) == 1
        outcome = returned["execute_outcome"]
        assert outcome["type"] == "diagnostic_completed"
        assert outcome["payload"]["diagnostic_record"] == result
        assert outcome["payload"]["execution_record"] == result
        assert not {"causal_parent_packet_ref", "incident_id", "diagnostic_request_digest"} & outcome["payload"].keys()
        assert fixture.reload()["state_generation"] == saved["state_generation"]
        (tmp_path / "legacy-authority-receipt.json").write_text(json.dumps({
            "decision": decision.to_dict(), "envelope": envelope.to_dict(),
            "outcome": outcome, "external_executor_calls": len(calls),
            "command_executed": False,
        }, indent=2))
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["no_commit", "active_owner", "owner_hash", "step_scope", "plan_scope", "typed_receipt_wrong_kind"])
def test_missing_typed_receipt_never_implies_legacy_authority(tmp_path, mutation):
    fixture, decision, _envelope, action = _committed_legacy_probe(tmp_path)
    saved = fixture.reload()
    damaged = deepcopy(saved)
    recovery = damaged["task_state"]["recovery_state"]
    if mutation == "no_commit":
        recovery.pop("authoritative_commit")
    elif mutation == "active_owner":
        recovery["active_recovery"]["recovery_id"] = "different-owner"
    elif mutation == "owner_hash":
        recovery["active_recovery"]["envelope_id"] = "different-envelope"
    elif mutation == "step_scope":
        damaged["current_step_id"] = "step.next"
    elif mutation == "plan_scope":
        damaged["plan_version"] = "different-plan-version"
    else:
        damaged["task_state"]["typed_recovery_receipts"] = {
            decision.decision_id: {"directive": {"action": "revalidate"}},
        }
    with pytest.raises(StageContractError, match="DIAGNOSTIC_RECEIPT_REQUIRED"):
        diagnostic_authority_family(action, damaged)
    # These are caller-snapshot negative controls, not mutations to the real store.
    assert fixture.reload()["task_state"]["recovery_state"] == saved["task_state"]["recovery_state"]


def _recovery(task_id, step_id):
    return RecoveryOutcome(schema_version=1, type="blocked",
        summary="The scoped failure remains unresolved",
        scope={"task_id": task_id, "step_id": step_id},
        payload={"reason_code": "SCOPED_FAILURE", "blocked_evidence": [],
                 "source_scope": {}, "budget_receipt": {}}).to_dict()


async def _first_commit(root):
    fixture = make_inspection_fixture(root)
    fixture.reviewer.accept_first = True
    original = {**fixture.state, **await fixture.new_supervisor()(deepcopy(fixture.state))}
    assert original["verification_outcome"]["type"] == "verified"
    completed = fixture.execution["step_id"]
    original["recovery_outcome"] = _recovery(fixture.task_id, completed)
    original["generate_seed"] = {"scope": {"task_id": fixture.task_id, "step_id": completed}, "instruction": "prior step repair"}
    committer = CommitStep(data_path=str(root))
    first = await committer.run(original)
    assert first["commit_step_outcome"]["type"] == "next_step_ready"
    assert first["recovery_outcome"] == {} and first["generate_seed"] == {}
    assert fixture.reload()["current_step_id"] == "step.next"
    return fixture, original, committer, first


@pytest.mark.parametrize("handoff_scope", ["successor", "unknown"])
def test_real_commit_receipt_replay_preserves_later_failure_handoff(tmp_path, handoff_scope):
    async def run():
        fixture, original, committer, first = await _first_commit(tmp_path)
        durable = fixture.reload()
        source = ExecuteOutcome(schema_version=1, type="pre_execution_rejected",
            summary="New successor dependency evidence missing",
            scope={"task_id": fixture.task_id, "plan_id": durable["plan_id"],
                   "plan_version": durable["plan_version"], "step_id": "step.next"},
            payload={"violation_owner": "DEPENDENCY", "violation_code": "NEW_SUCCESSOR_FAILURE",
                     "candidate_fingerprint": "new-successor", "command_started": False,
                     "budget_receipt": {}}).to_dict()
        update = CausalFailurePacketBinder(data_path=str(tmp_path)).bind(
            {"task_id": fixture.task_id}, "execute_outcome", source)
        successor = fixture.reload()
        active = successor["task_state"]["active_failure_packet_ref"]
        assert active
        live = {**original, **first, **update, "active_failure_packet_ref": active,
                "recovery_outcome": _recovery(fixture.task_id, "step.next"),
                "generate_seed": {"scope": {"task_id": fixture.task_id, "step_id": "step.next"}, "instruction": "new successor repair"}}
        if handoff_scope == "unknown":
            # Unknown nonempty data cannot be declared consumed by the old scope.
            live["recovery_outcome"].pop("scope")
            live["generate_seed"].pop("scope")
        replay = await committer.run(live)
        assert replay["commit_step_outcome"] == first["commit_step_outcome"]
        merged = {**live, **replay}
        for key in ("recovery_outcome", "generate_seed", "active_failure_packet_ref", "failure_packet"):
            if key in live:
                assert merged[key] == live[key], key
        assert merged["current_step_id"] == "step.next"
        assert fixture.reload()["state_generation"] == successor["state_generation"]
        assert fixture.reload()["task_state"]["active_failure_packet_ref"] == active
        (tmp_path / "late-commit-receipt.json").write_text(json.dumps({
            "first_commit": first, "new_active_packet": active,
            "incoming_handoff": live["recovery_outcome"], "replay": replay,
            "durable_generation": successor["state_generation"],
        }, indent=2))
    asyncio.run(run())


def test_real_old_commit_cannot_rewind_cursor_after_later_durable_transition(tmp_path):
    async def run():
        fixture, original, committer, first = await _first_commit(tmp_path)
        successor = fixture.reload()
        # Explicit narrow transition/store control. This is not a claim that a
        # second scientific runner or second Supervisor has been exercised.
        assert advance_plan_after_verified_step(successor, "offline transition control",
            expected_step_id="step.next", transition_id="later-transition-control",
            legacy_route_projection=False)
        save_plan_state(tmp_path, fixture.task_id, successor)
        later = fixture.reload()
        assert later["current_step_id"] == ""
        replay = await committer.run({**original, "current_step_id": ""})
        assert replay["commit_step_outcome"] == first["commit_step_outcome"]
        assert not {"current_step_id", "recovery_outcome", "generate_seed"} & replay.keys()
        assert fixture.reload()["state_generation"] == later["state_generation"]
        assert fixture.reload()["current_step_id"] == ""
    asyncio.run(run())

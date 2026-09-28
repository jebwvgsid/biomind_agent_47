"""Real publication/read/provider boundaries; all I/O stays in pytest tmp_path.

The source matrix is an exhaustive typed-publication contract test, not a claim
that each accepted source alias is a separate graph edge. The Supervisor case
separately exercises the real producer and Recovery provider request boundary.
Only reviewers/models and an explicitly counted permission fault are replaced.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import re

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_tracker import (
    approve_plan_state, build_plan_state, canonical_task_spec_for_state,
    load_plan_state, record_provenance_event, save_plan_state,
)
from agent_core.recovery import ValidationEvidence
from agent_core.skill_access import (
    SkillAccessService, new_skill_access_state, record_provider_visible_materials,
)
from agent_core.skill_manager import SkillManager
from agent_core.state_contract_store import StateContractStore
from agent_core.step_effects import canonical_step_effect, structured_effect_parse_receipt
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.workflow.inspection_evidence import INSPECTION_BACKEND, InspectionEvidenceError
from agent_core.workflow.outcomes import (
    CommitStepOutcome, ExecuteOutcome, InstallSubagentOutcome, VerificationOutcome,
)
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import (
    CausalFailurePacketBinder, is_recovery_failure_source, project_recovery_seed,
    recovery_source_kind,
)
from agent_core.workflow.recovery_tools import (
    RecoveryLocalToolDispatcher, RecoveryToolError, build_recovery_inspection_registry,
)
from tests.test_inspection_workflow import _tag_json


VERIFICATION_TYPES = (
    "rejected", "incomplete_evidence", "diagnostic_evidence_recorded",
    "verification_error", "inspection_required", "correction_required", "assessment_pending",
)
SOURCE_PAIRS = (
    *(("execute_outcome", kind) for kind in (
        "execution_failed", "diagnostic_completed", "pre_execution_rejected")),
    *((source, kind) for source in ("verification_outcome", "completion_gate_outcome")
      for kind in VERIFICATION_TYPES),
    ("commit_step_outcome", "commit_conflict"),
    ("commit_step_outcome", "contract_rejected"),
    ("install_subagent_outcome", "installation_failed"),
)
CONTRACTS = {
    "execute_outcome": ExecuteOutcome, "verification_outcome": VerificationOutcome,
    "completion_gate_outcome": VerificationOutcome, "commit_step_outcome": CommitStepOutcome,
    "install_subagent_outcome": InstallSubagentOutcome,
}


def _reopen(root, task_id):
    durable = load_plan_state(str(root), task_id)
    seed = project_recovery_seed({"task_id": task_id}, durable).recovery_seed
    archive = PhaseArchiveStore(str(root))
    registry = build_recovery_inspection_registry(seed, durable, phase_store=archive)
    dispatcher = RecoveryLocalToolDispatcher(
        seed=seed, durable=durable, registry=registry, phase_store=archive,
    )
    return durable, seed, registry, dispatcher


def _object_rows(registry):
    return {row["scope"]["object_kind"]: row for row in registry.provider_manifest()
            if row["backend"] == INSPECTION_BACKEND}


def _read_complete(dispatcher, row):
    chunks, start = [], 1
    for _ in range(32):
        page = dispatcher.dispatch_full("evidence.read", {
            "evidence_ref": row["ref_id"], "start_line": start,
        })
        chunks.append(page["content"])
        if not page["truncated"]:
            return json.loads("\n".join(chunks))
        start = page["cursor"]["next_start_line"]
    raise AssertionError("Synthetic evidence unexpectedly exceeded 32 bounded pages")


def _artifact_case(root, *, create_attempt=True):
    """Real TaskSpec approval and attempt; bytes exist before their approval."""
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    text = "@fixture/1\nACGT\n+\nIIII\n"
    paths = {"plain": inputs / "reads.fastq", "compressed": inputs / "reads.fastq.gz",
             "unsupported": inputs / "binary.bin"}
    paths["plain"].write_text(text)
    paths["compressed"].write_bytes(gzip.compress(text.encode(), mtime=0))
    paths["unsupported"].write_bytes(b"\x00\x01\xffunsupported")
    task_id = "publication-fixture"
    task = create_task_spec("Inspect the approved input prefix after a recorded failure.",
        task_id=task_id, base_dir=root, project_root=root,
        structured_source={"input_files": [{"role": role, "path": str(path),
                            "format": "fastq" if role != "unsupported" else "binary"}
                           for role, path in paths.items()]})
    step = {"step_id": "step.inspect", "title": "Inspect approved input", "action": "execute",
            "purpose": "validation", "tool": "bash", "inputs": ["input." + role for role in paths],
            "outputs": [], "dependencies": [], "success_criteria": ["Obtain independent evidence."],
            "skill_ids": [], "workflow_stage_ids": [], "batch_id": "", "risk_level": "low",
            "execution_capability_id": "fixture.inspect", "operation": "inspect"}
    plan = build_plan_state(task_id, json.dumps({"steps": [step], "validation_checklist": [],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}), strict=True)
    for row in plan["steps"]:
        row["step_effect"] = canonical_step_effect(row)
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True,
                current_task={"task_spec": task, "task_revision": task_spec_revision(task)})
    plan["task_state"]["workflow_routing_schema_version"] = 2
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True)
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source="offline_test_authorized_plan")
    save_plan_state(root, task_id, plan)
    plan = load_plan_state(root, task_id)
    action = {"action_id": "action-fixture", "step_id": step["step_id"], "type": "execute",
              "plan_id": plan["plan_id"], "plan_version": plan["plan_version"],
              "purpose": "validation", "command_role": "required_validation",
              "code": "printf 'recorded failure\\n'", "outputs": [],
              "expected_step_effect": deepcopy(plan["steps"][0]["step_effect"])}
    record_provenance_event(plan, "action_bound", {"action": action, "raw_action": action})
    if not create_attempt:
        save_plan_state(root, task_id, plan)
        return {"task_id": task_id, "workflow_routing_schema_version": 2,
                "pending_action": action, "execution_evidence": {}}, paths, text
    attempt = record_provenance_event(plan, "attempt_started", {
        "action_id": action["action_id"], "step_id": action["step_id"]})
    execution = {"schema_version": 1, "task_id": task_id,
        **{key: action[key] for key in ("plan_id", "plan_version", "step_id", "action_id", "code")},
        "attempt_id": attempt["attempt_id"], "execution_id": "execution-fixture",
        "returncode": 17, "execution_status": "failed", "raw_stdout": "recorded failure\n",
        "raw_stderr": "RECORDED_FAILURE_DETAIL", "effect_evidence": {}}
    execution["effect_evidence"]["parser_receipt"] = structured_effect_parse_receipt(
        execution["raw_stdout"], action["expected_step_effect"])
    record_provenance_event(plan, "execution_finished", {
        "attempt_id": attempt["attempt_id"], "execution_record": execution})
    save_plan_state(root, task_id, plan)
    state = {"task_id": task_id, "workflow_routing_schema_version": 2,
             "execution_evidence": execution, "pending_action": action}
    return state, paths, text


def _source(state, source_kind, kind):
    execution = deepcopy(state["execution_evidence"])
    scope = {key: execution[key] for key in (
        "task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id")}
    validation = ValidationEvidence(task_id=scope["task_id"], step_id=scope["step_id"],
        action_id=scope["action_id"], attempt_id=scope["attempt_id"], validation_id="validation-fixture",
        status="INVALID", evidence_complete=True, errors=["RECORDED_VALIDATION_FAILURE"],
        returncode_check={"returncode": 17, "passed": False}).to_dict()
    # The matrix intentionally supplies only source-owned objects. Absent
    # report/semantic bodies must remain unavailable, never empty successes.
    payload = {"execution_record": execution, "diagnostic_record": execution,
        "validation_evidence": validation, "completion_gate": {"passed": False},
        "completion_report": {}, "semantic_decision": {},
        "contract_violations": ["RECORDED_VALIDATION_FAILURE"], "failure_owner": "verification",
        "action_id": scope["action_id"], "attempt_id": scope["attempt_id"], "return_code": 17,
        "failure_fingerprint": "fixture-failure", "progress_snapshot": {},
        "envelope_identity": {"envelope_id": "recorded-diagnostic-envelope"}, "status": "failed",
        "violation_owner": "VERIFICATION", "violation_code": "RECORDED_PRELAUNCH_FAILURE",
        "candidate_fingerprint": "fixture-candidate", "command_started": False, "budget_receipt": {},
        "missing_evidence": ["observation absent"], "eligible_probe_capabilities": [],
        "command_role": "diagnostic", "error_code": "RECORDED_VALIDATOR_ERROR",
        "exception_class": "RecordedValidatorError", "expected_state_generation": 1,
        "current_state_generation": 2, "no_partial_mutation": True,
        "reason_code": "RECORDED_COMMIT_CONTRACT", "failed_invariant": "fixture invariant", "evidence": {}}
    return CONTRACTS[source_kind](schema_version=1, type=kind, scope=scope,
        summary="Recorded publication contract fixture", payload=payload).to_dict()


@pytest.mark.parametrize("source_kind,kind", SOURCE_PAIRS)
def test_every_accepted_typed_source_publishes_real_reloadable_evidence(tmp_path, source_kind, kind):
    state, _paths, _text = _artifact_case(tmp_path)
    source = _source(state, source_kind, kind)
    assert is_recovery_failure_source(source_kind, source)
    CausalFailurePacketBinder(data_path=str(tmp_path)).bind(state, source_kind, source)
    durable, seed, registry, dispatcher = _reopen(tmp_path, state["task_id"])
    active = durable["task_state"]["causal_failure_packets"][durable["task_state"]["active_failure_packet_ref"]]
    assert active["inspection_publication"]["policy"] == "typed_recovery_evidence_v2"
    rows = _object_rows(registry)
    assert set(rows) == {"validation", "completion_report", "semantic_decision", "step_attempt",
                         "execution", "script", "action", "task_context", "plan", "parser_receipt", "diagnostic"}
    stored = durable["task_state"]["inspection_ref_registry"]
    for object_kind, row in rows.items():
        assert row["ref_id"] in seed.failure_packet.inspection_refs
        assert row["scope"]["task_id"] == state["task_id"]
        assert row["scope"]["step_id"] == state["execution_evidence"]["step_id"]
        assert row["scope"]["attempt_id"] == state["execution_evidence"]["attempt_id"]
        assert row["allowed_tools"] == ["evidence.read"]
        if object_kind in {"completion_report", "semantic_decision"}:
            assert row["readable"] is False
            expected_absence = ("NOT_APPLICABLE_NOT_PRODUCED" if source_kind in
                {"execute_outcome", "install_subagent_outcome"} else "HISTORICAL_BODY_UNAVAILABLE")
            assert row["unavailable_reason"] == expected_absence
            continue
        assert row["readable"] is True
        locator = stored[row["ref_id"]]["locator"]
        assert locator["object_ref"] == "object:" + row["digest"]
        wrapper = StateContractStore(str(tmp_path), state["task_id"]).read_object(
            locator["object_ref"], expected_type=locator["object_type"])
        assert _read_complete(dispatcher, row) == wrapper["body"]
    if source_kind in {"execute_outcome", "install_subagent_outcome"}:
        assert not active["failure_source"]["payload"].get("inspection_publication_failures")
    else:
        assert {row["code"] for row in active["failure_source"]["payload"]["inspection_publication_failures"]} == {"HISTORICAL_BODY_UNAVAILABLE"}
    assert _read_complete(dispatcher, rows["script"])["scripts"]["code"] == state["execution_evidence"]["code"]
    assert _read_complete(dispatcher, rows["parser_receipt"])["parser_receipt"] == state["execution_evidence"]["effect_evidence"]["parser_receipt"]


def _select_visible_skill(root, durable):
    skill_id = "fixture/publication"
    text = "---\nname: Publication fixture\ndescription: Offline evidence continuity\n---\n# Method\nEXACT_SELECTED_SKILL_METHOD_SENTINEL\n"
    path = root / "skills" / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    service = SkillAccessService(SkillManager(str(root / "skills")))
    revision = task_spec_revision(canonical_task_spec_for_state(durable))
    access, snapshot, _ = service.ensure_snapshot(new_skill_access_state(), skill_id,
        scope="task", status="active", selected_at_task_revision=revision)
    access, _ = service.load_skill_documents(access, [skill_id], task_revision=revision,
                                             request_id="fixture-visible-skill")
    access = record_provider_visible_materials(access, [{"kind": "skill_document",
        "snapshot_id": snapshot["snapshot_id"], "content_hash": snapshot["content_hash"]}],
        phase="consultant", invocation_id="fixture-consultant", event_id="fixture-visible-skill")
    durable["task_state"]["skill_access_state"] = access
    save_plan_state(str(root), durable["task_id"], durable)
    return text, snapshot, service


class EvidenceRoundTripModel:
    def __init__(self, calls=None):
        self.requests, self.calls = [], calls

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append(deepcopy(list(messages)))
        seed = _tag_json(list(messages), "recovery_seed")
        if len(self.requests) == 1:
            rows = [r for r in seed["inspection_manifest"]
                    if r.get("scope", {}).get("object_kind") in {"script", "parser_receipt", "completion_report"}
                    and r.get("readable")]
            calls = self.calls if self.calls is not None else [
                {"id": "read-" + row["scope"]["object_kind"], "name": "evidence_read",
                 "args": {"evidence_ref": row["ref_id"]}, "type": "tool_call"} for row in rows]
            return AIMessage(content="", tool_calls=calls)
        return AIMessage(content=json.dumps({"schema_version": 1, "directive_id": "evidence-inspected-stop",
            "task_id": seed["invocation_scope"]["task_id"], "action": "blocked",
            "summary": "The recorded failure still requires a separately approved correction.",
            "reason_code": "OFFLINE_RECORDED_FAILURE_UNRESOLVED"}))


def _exact_material(message):
    value = str(message.content)
    head, body = value.split("\n---BEGIN EXACT SOURCE BODY---\n", 1)
    metadata = json.loads(head.split("\n", 1)[1])
    return metadata, body.split("\n---END EXACT SOURCE BODY---\n", 1)[0]






def test_dependency_prelaunch_without_attempt_publishes_absence_honestly(tmp_path):
    state, _paths, _text = _artifact_case(tmp_path, create_attempt=False)
    action = state["pending_action"]
    scope = {"task_id": state["task_id"], **{k: action[k] for k in
             ("plan_id", "plan_version", "step_id", "action_id")}, "attempt_id": ""}
    source = ExecuteOutcome(schema_version=1, type="pre_execution_rejected", scope=scope,
        summary="Approved dependency evidence needs inspection before launch.", payload={
            "violation_owner": "DEPENDENCY", "violation_code": "DEPENDENCY_EVIDENCE_MISSING",
            "candidate_fingerprint": "fixture-unlaunched-candidate", "command_started": False,
            "budget_receipt": {"counts_as_execution": False},
        }).to_dict()
    assert is_recovery_failure_source("execute_outcome", source)
    CausalFailurePacketBinder(data_path=str(tmp_path)).bind(state, "execute_outcome", source)
    _durable, seed, registry, dispatcher = _reopen(tmp_path, state["task_id"])
    assert seed.failure_packet.attempt_ref == ""
    rows = _object_rows(registry)
    assert _read_complete(dispatcher, rows["execution"])["execution_status"] == "not_executed"
    script = _read_complete(dispatcher, rows["script"])["scripts"]
    assert set(script) == {"availability"}
    assert set(script["availability"].values()) == {"HISTORICAL_BODY_UNAVAILABLE"}
    step = _read_complete(dispatcher, rows["step_attempt"])
    assert step["execution_unavailable_reason"] == "NOT_APPLICABLE_NO_ATTEMPT"
    assert step["execution_record"] == {} and step["commit_status"] == "not_committed"
    assert _read_complete(dispatcher, rows["action"])["action"]["action_id"] == action["action_id"]
    for kind in ("validation", "completion_report", "semantic_decision"):
        assert rows[kind]["readable"] is False
        assert rows[kind]["unavailable_reason"] == "NOT_APPLICABLE_NOT_PRODUCED"
    active = _durable["task_state"]["causal_failure_packets"][_durable["task_state"]["active_failure_packet_ref"]]
    assert not active["failure_source"]["payload"].get("inspection_publication_failures")


def test_prior_episode_body_is_rejected_before_object_read_after_real_rebinding(tmp_path, monkeypatch):
    state, _paths, _text = _artifact_case(tmp_path)
    source = _source(state, "execute_outcome", "execution_failed")
    binder = CausalFailurePacketBinder(data_path=str(tmp_path))
    binder.bind(state, "execute_outcome", source)
    _durable, old_seed, old_registry, old_dispatcher = _reopen(tmp_path, state["task_id"])
    old_script = _object_rows(old_registry)["script"]
    changed = deepcopy(source)
    changed["summary"] = "A newly published causal event supersedes the prior inspection episode."
    changed["payload"]["failure_fingerprint"] = "next-event-distinct-fingerprint"
    binder.bind(state, "execute_outcome", changed)
    _current, new_seed, registry, dispatcher = _reopen(tmp_path, state["task_id"])
    assert new_seed.seed_id != old_seed.seed_id
    reads = []
    def must_not_read(*_args, **_kwargs):
        reads.append(True)
        raise AssertionError("A superseded episode must fail before immutable body I/O")
    monkeypatch.setattr(old_dispatcher.inspection_reader.store, "read_object", must_not_read)
    with pytest.raises(InspectionEvidenceError, match="INSPECTION_REF_SCOPE_MISMATCH"):
        _read_complete(old_dispatcher, old_script)
    assert not reads
    assert _read_complete(dispatcher, _object_rows(registry)["script"])["scripts"]["code"] == state["execution_evidence"]["code"]

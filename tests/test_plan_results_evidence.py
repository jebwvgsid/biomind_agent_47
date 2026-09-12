"""EV-1: synthetic parser/collector/gate/report and actual reviewer wire tests.

Run through the allowlisted offline export and guard. No original session or
state loader is used; persisted reviewer fixtures live solely in pytest scratch.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from agent_core.completion_gate import CompletionGate
from agent_core.evidence_projection import (
    artifact_collection_facts, artifact_effect_evidence, runtime_observation_facts,
)
from agent_core.managed_artifacts import _observed_file_metadata, reconcile_runtime_outputs
from agent_core.runtime_evidence import (
    make_runtime_evidence, merge_runtime_evidence, runtime_evidence_state,
)
from agent_core.semantic_completion import CompletionReport, build_completion_report
from agent_core.step_effects import extract_structured_effect_evidence, structured_effect_parse_receipt


def _fixture(tmp_path):
    output = tmp_path / "actual-report.txt"
    output.write_text("synthetic checked data\n", encoding="utf-8")
    path = str(output)
    effect = {"schema_version": 1, "produces_artifacts": ["logical-result"]}
    plan = {
        "task_id": "task-evidence", "plan_id": "plan-evidence", "plan_version": "v1",
        "state_generation": 1, "current_step_id": "step-evidence",
        "workflow_routing_schema_version": 2, "task_state": {},
        "steps": [{"step_id": "step-evidence", "step_effect": effect,
                   "output_artifact_ids": ["artifact-provenance"]}],
        "provenance_state": {
            "artifact_heads": {"artifact-provenance": "artifact-provenance@expected"},
            "artifacts": {"artifact-provenance@expected": {
                "artifact_id": "artifact-provenance", "logical_artifact_id": "logical-result",
                "path": path, "status": "expected", "producer_attempt_id": "", "validation_id": "",
            }}, "attempts": {},
        },
    }
    validation = {
        "task_id": "task-evidence", "step_id": "step-evidence", "action_id": "action-evidence",
        "attempt_id": "attempt-evidence", "validation_id": "validation-evidence", "status": "VALID",
        "returncode_check": {"returncode": 0, "passed": True}, "evidence_complete": True,
    }
    stdout = "BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps({
        "produces_artifacts": [{"identity": "logical-result", "path": path}],
    }) + "\n"
    receipt = structured_effect_parse_receipt(stdout, effect)
    assert receipt["issues"] == []
    claims = extract_structured_effect_evidence(stdout)
    candidate = {**_observed_file_metadata(output), "attempt_id": "attempt-evidence",
                 "current_attempt_owned": True}
    reconciled = reconcile_runtime_outputs([{
        "artifact_id": "logical-result", "semantic_handle": "logical-result",
        "path": path, "artifact_kind": "file", "format": "text",
    }], [candidate], attempt_id="attempt-evidence", exit_code=0)
    assert reconciled["status"] == "accepted", reconciled
    execution = {
        "stdout": stdout, "returncode": 0, "validated_artifacts": [path],
        "observed_output_records": [candidate], "runtime_output_reconciliation": reconciled,
        "reconciled_outputs": deepcopy(reconciled["bindings"]),
        "effect_evidence": {"parser_receipt": receipt, "produces_artifacts": artifact_effect_evidence(
            [path], claims=claims["produces_artifacts"], bindings=reconciled["bindings"],
        )},
    }
    return plan, validation, execution


def _gate(plan):
    raw = CompletionGate.validate_step_evidence(deepcopy(plan), "step-evidence").to_dict()
    return {**raw, "passed": raw["completion_satisfied"], "classification": "step_effect_incomplete"}


def test_e1_actual_marker_collection_registration_and_gate_stay_distinct(tmp_path):
    plan, validation, execution = _fixture(tmp_path)
    before = deepcopy((plan, validation, execution))
    gate = _gate(plan)
    assert gate["passed"] is False and gate["invalid_outputs"] == ["artifact-provenance"]
    report = build_completion_report(plan_state=plan, validation=validation,
                                     execution_evidence=execution, completion_gate=gate)
    view = report.to_dict()
    artifact = view["effect_presence"]["observed"]["produces_artifacts"][0]
    assert artifact["identity"] == "logical-result"
    assert artifact["path"] == execution["validated_artifacts"][0]
    assert "value_present" not in artifact
    collection = view["effect_presence"]["file_collection"]
    assert collection["accepted_set_status"] == "known"
    assert collection["observed_candidates"][0]["content_valid"] is True
    registration = view["effect_presence"]["formal_registration"][0]
    assert registration["identity"] == "logical-result"
    assert registration["status"] == "expected" and registration["registration_state"] == "not_registered"
    assert report.effective_disposition == "inspection_required"
    assert report.deterministic_veto is True and view["completion_gate"] == gate
    assert next(row for row in view["findings"] if row["nature"] == "completion_gate")["affected_field"] == "passed"
    assert CompletionReport.from_mapping(view).to_dict() == view
    assert _gate(plan) == gate
    assert (plan, validation, execution) == before
    # Compare the old path-list payload against the richer projection using
    # the same actual gate. Neither can make expected provenance valid.
    old = deepcopy(execution)
    old["effect_evidence"]["produces_artifacts"] = old["validated_artifacts"]
    prior = build_completion_report(plan_state=plan, validation=validation,
                                    execution_evidence=old, completion_gate=gate)
    assert prior.effective_disposition == report.effective_disposition
    assert prior.predicate_results == report.predicate_results
    assert old["stdout"] == execution["stdout"]


@pytest.mark.parametrize("accepted, expected", [([], "known"), (None, "unknown")])
def test_e2_explicit_empty_or_unknown_acceptance_never_uses_disk_candidates(tmp_path, accepted, expected):
    _, _, execution = _fixture(tmp_path)
    execution["validated_artifacts"] = accepted
    facts = artifact_collection_facts(execution)
    assert facts["source"] == "validated_artifacts"
    assert facts["accepted_set_status"] == expected and facts["accepted"] == []
    assert len(facts["observed_candidates"]) == 1


def test_e2_path_is_not_identity_and_conflicting_explicit_links_are_unknown(tmp_path):
    path = str(tmp_path / "same-name.txt")
    assert artifact_effect_evidence([path]) == [{"identity": "", "path": path, "identity_status": "unknown"}]
    rows = artifact_effect_evidence([path], claims=[{"identity": "result-a", "path": path}],
                                    bindings=[{"artifact_id": "result-b", "observed_path": path}])
    assert rows == [{"identity": "", "path": path, "identity_status": "ambiguous"}]
    # A same basename at another location does not transfer logical identity.
    assert artifact_effect_evidence([path], claims=[{
        "identity": "result-a", "path": str(tmp_path / "another" / "same-name.txt"),
    }])[0]["identity"] == ""


@pytest.mark.parametrize("observation", [{"identity": "check"}, {"identity": "check", "value": None},
                                        {"identity": "check", "value": False}])
def test_e3_observation_value_protocol_is_preserved(tmp_path, observation):
    plan, validation, execution = _fixture(tmp_path)
    plan["steps"][0]["step_effect"]["produces_observations"] = ["check"]
    execution["effect_evidence"]["produces_observations"] = [observation]
    report = build_completion_report(plan_state=plan, validation=validation,
                                     execution_evidence=execution, completion_gate=_gate(plan)).to_dict()
    seen = report["effect_presence"]["observed"]["produces_observations"][0]
    assert seen["value_present"] == ("value" in observation)
    if "value" in observation:
        assert seen["value"] is observation["value"]
    assert "value_present" not in report["effect_presence"]["observed"]["produces_artifacts"][0]


def _runtime_records(task_id):
    records = []
    for env, status, available in [("current-shell", "succeeded", "UNAVAILABLE"),
                                    ("target-env", "execution_error", "UNKNOWN")]:
        scope = {"skill_id": "fixture.inspect", "skill_document_digest": "sha256:" + "a" * 64,
                 "skill_contract_digest": "sha256:" + "b" * 64, "environment_id": env,
                 "environment_fingerprint": "sha256:" + "c" * 64, "host_id": "synthetic-host",
                 "inspection_contract_digest": "sha256:" + "d" * 64}
        records.append(make_runtime_evidence({"skill_id": "fixture.inspect", "checked_env": env},
            task_id=task_id, skill_scope=scope, observed_at="2026-09-12T00:00:00+00:00",
            inspection_status=status, source_ref="synthetic-inspection:" + env,
            coverage={"runtime.target_executable": {"availability": available,
                "conclusion_complete": env == "current-shell",
                "value_kind": "executable_path", "reason_code": "not_on_current_path" if env == "current-shell" else "inspection_execution_error"}}))
    return runtime_evidence_state(merge_runtime_evidence([], records, task_id=task_id,
        accepted_at="2026-09-12T00:00:01+00:00"))


def test_e4_runtime_scope_unknown_and_raw_observations_reach_actual_reviewer(tmp_path):
    async def scenario():
        from agent_core.plan_tracker import save_plan_state
        from supervisor import SupervisorNode
        from tests.test_inspection_workflow import make_inspection_fixture
        from agent_core.workflow.outcomes import VerificationOutcome
        from agent_core.workflow.recovery_projection import build_causal_failure_record
        fixture = make_inspection_fixture(tmp_path / "reviewer", task_id="evidence-reviewer")
        fixture.execution["validated_artifacts"] = []
        durable = fixture.reload()
        records = _runtime_records(fixture.task_id)
        durable["task_state"]["runtime_evidence"] = records
        save_plan_state(fixture.root, fixture.task_id, durable)
        before = deepcopy(fixture.execution)
        node = SupervisorNode(fixture.reviewer, output_dir=str(fixture.root / "task_outputs"))
        await node.run(deepcopy(fixture.state))
        assert len(fixture.reviewer.requests) == 1
        report = fixture.reviewer.reports[0]
        runtime = report["process_outcome"]["runtime_observations"]
        by_environment = {row["scope"]["environment_id"]: row for row in runtime["records"]}
        target = by_environment["target-env"]
        assert target["facts"]["runtime.target_executable"]["availability"] == "UNKNOWN"
        assert target["inspection"] == records["records"][1]["inspection"]
        assert target["inspection"]["coverage_complete"] is False
        assert target["source_ref"] == "synthetic-inspection:target-env"
        assert by_environment["current-shell"]["facts"]["runtime.target_executable"]["availability"] == "UNAVAILABLE"
        assert fixture.execution == before
        # Provider receives the actual immutable report, including facts; this
        # is not an independently manufactured request to the fake provider.
        assert any("inspection_execution_error" in str(message.content)
                   for message in fixture.reviewer.requests[0])
        source = VerificationOutcome(schema_version=1, type="inspection_required",
            summary="Synthetic reviewer requests bounded inspection.",
            scope=dict(report["scope"]), payload={
                "completion_report": report, "completion_gate": report["completion_gate"],
                "validation_evidence": {"validation_id": report["scope"]["validation_id"]},
                "semantic_decision": report["effective_decision"],
                "contract_violations": [], "failure_owner": "SEMANTIC_REVIEW",
            }).to_dict()
        record = build_causal_failure_record("verification_outcome", source, fixture.reload(),
                                             task_id=fixture.task_id)
        compact = record["failure_source"]["payload"]["completion_report"]
        projected = compact["process_outcome"]["runtime_observations"]["records"]
        projected_target = next(row for row in projected if row["scope"]["environment_id"] == "target-env")
        assert projected_target["facts"]["runtime.target_executable"]["availability"] == "UNKNOWN"
        assert projected_target["inspection"]["coverage_complete"] is False
        assert compact["effect_presence"]["file_collection"]["accepted"] == []
        assert compact["effect_presence"]["file_collection"]["accepted_set_status"] == "known"
        assert runtime_observation_facts({"runtime_evidence": records}, task_id="different-task")["records"] == []
        assert runtime_observation_facts({"runtime_evidence": records}, task_id=fixture.task_id,
                                         skill_ids=["different-skill"])["records"] == []
    asyncio.run(scenario())


def test_recorded_output_candidates_and_contracts_reach_existing_recovery_seed():
    from tests.test_recovery_projection import _durable, _execute_failure
    from agent_core.workflow.recovery_projection import build_causal_failure_record, project_recovery_seed
    durable, source = _durable(), _execute_failure()
    execution = source["payload"]["execution_record"]
    execution["action_contract"] = {"controller_output_bindings": [{
        "artifact_id": "logical-filtered", "expected_path": "/synthetic/output/hint.vcf",
        "required": True, "constraint_origin": "IMPLEMENTATION_DETAIL",
    }], "code": "DO_NOT_PROJECT_SCRIPT"}
    execution["observed_output_records"] = [{
        "path": "/synthetic/output/tool-result.vcf", "attempt_id": "attempt-1",
        "current_attempt_owned": True, "content_valid": True, "format": "vcf",
    }]
    execution["runtime_output_reconciliation"] = {"status": "rejected", "violations": [{
        "code": "OUTPUT_PATH_MISMATCH", "message": "Recorded filename differs from the contract.",
        "expected_path": "/synthetic/output/hint.vcf", "observed_path": "/synthetic/output/tool-result.vcf",
    }]}
    original = deepcopy(source)
    record = build_causal_failure_record("execute_outcome", source, durable, task_id="task-recovery")
    durable["task_state"]["causal_failure_packets"] = {record["packet_ref"]: record}
    durable["task_state"]["active_failure_packet_ref"] = record["packet_ref"]
    projection = project_recovery_seed({"task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"]}, durable)
    fact = next(row for row in projection.recovery_seed.to_dict()["failure_packet"]["diagnostic_facts"]
                if row["kind"] == "result_realizations")
    result = fact["metadata"]["result_realizations"]
    assert result["declared_results"][0]["artifact_id"] == "logical-filtered"
    assert result["declared_results"][0]["required"] is True
    assert result["observed_candidates"][0]["path"] == "/synthetic/output/tool-result.vcf"
    assert result["scope"]["attempt_id"] == "attempt-1"
    assert result["reconciliation"]["violations"][0]["detail"] == "Recorded filename differs from the contract."
    assert "message" not in result["reconciliation"]["violations"][0]
    assert "DO_NOT_PROJECT_SCRIPT" not in json.dumps(fact)
    assert "large raw output" not in json.dumps(fact)
    assert source == original

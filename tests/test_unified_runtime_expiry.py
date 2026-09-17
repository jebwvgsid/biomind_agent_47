"""Explicit executable fact expiry dominates directory compatibility paths."""
from datetime import datetime, timedelta, timezone

import pytest

from agent_core.action_runtime import (
    ActionRuntimeContractError, build_runtime_binding_registry,
    capability_contract_hash, resolve_action_runtime_bindings,
)
from agent_core.runtime_evidence import (
    digest, evaluate_runtime_evidence, make_runtime_evidence, merge_runtime_evidence,
    project_runtime_audits, runtime_evidence_state,
)
from agent_core.workflow.runtime_evidence_recovery import RuntimeEvidenceRecovery


def _fixture(tmp_path, mode):
    now = datetime.now(timezone.utc)
    home = tmp_path / "java-home"
    (home / "bin").mkdir(parents=True)
    java = home / "bin/java"
    java.write_text("executable presence fixture; never invoked")
    java.chmod(0o755)
    subject = {key: digest(key) for key in (
        "skill_document_digest", "skill_contract_digest", "environment_fingerprint", "inspection_contract_digest")}
    subject.update(skill_id="fixture/java", environment_id="fixture", host_id="offline-host")
    contract = {"capability_id": "fixture.java.execute", "selected_skill_id": subject["skill_id"],
        "required_runtime_binding_ids": ["runtime.java.bin"], "optional_runtime_binding_ids": [],
        "required_artifact_binding_ids": []}

    def evidence(name, value, *, observed, max_age=3600, coverage=None):
        return make_runtime_evidence({"skill_id": subject["skill_id"], "env": "fixture", "status": "ready",
            "resolved_runtime_paths": {name: str(value)} if value is not None else {}},
            task_id="expiry", skill_scope=subject, observed_at=observed.isoformat(),
            max_age_seconds=max_age, coverage=coverage)

    records = [evidence("RESOLVED_JAVA_HOME", home, observed=now - timedelta(seconds=30))]
    if mode in {"expired", "renewed"}:
        records.append(evidence("RESOLVED_JAVA_BIN", java, observed=now - timedelta(hours=2), max_age=60))
    elif mode == "future":
        records.append(evidence("RESOLVED_JAVA_BIN", java, observed=now + timedelta(minutes=3)))
    elif mode == "unknown":
        records.append(evidence("RESOLVED_JAVA_BIN", None, observed=now - timedelta(seconds=10), coverage={
            "runtime.java.bin": {"availability": "UNKNOWN", "output_name": "RESOLVED_JAVA_BIN",
                "value_kind": "executable_path", "reason_code": "inspection_output_incomplete", "conclusion_complete": False}}))
    if mode == "renewed":
        records.append(evidence("RESOLVED_JAVA_BIN", java, observed=now - timedelta(seconds=5)))
    task = {"runtime_evidence": runtime_evidence_state(merge_runtime_evidence(
        [], records, task_id="expiry", accepted_at=now.isoformat()))}
    facts = evaluate_runtime_evidence(task, task_id="expiry", skill_scope=subject,
        required_binding_ids=contract["required_runtime_binding_ids"], now=now.isoformat())
    audits = project_runtime_audits(task, task_id="expiry", skill_scope=subject, now=now.isoformat())
    return now, java, subject, contract, task, facts, audits


def _resolve(contract, registry):
    return resolve_action_runtime_bindings(selected_skill_id=contract["selected_skill_id"],
        execution_capability_id=contract["capability_id"], approved_capability_contract_hash=capability_contract_hash(contract),
        capability_registry={contract["capability_id"]: contract}, ready_runtime_binding_registry=registry,
        current_action_identity={"action_id": "new-candidate", "plan_id": "plan", "step_id": "science"})


@pytest.mark.parametrize("mode", ["expired", "future", "unknown", "undeclared", "renewed"])
def test_registry_resolver_and_recovery_assessment_share_explicit_fact_authority(tmp_path, mode):
    now, java, subject, contract, task, facts, audits = _fixture(tmp_path, mode)
    allowed = mode in {"undeclared", "renewed"}
    # Both production inputs are exercised: already scoped projection, and the
    # task-owned evidence envelope evaluated by the registry itself.
    registries = [build_runtime_binding_registry({"runtime_audit": audits, "runtime_binding_evaluations": facts}),
        build_runtime_binding_registry({"task_id": "expiry", "task_state": task})]
    for registry in registries:
        if allowed:
            assert _resolve(contract, registry)["effective_runtime_bindings"]["JAVA_BIN"] == str(java)
        else:
            assert registry["runtime.java.bin"]["status"] == "unknown"
            with pytest.raises(ActionRuntimeContractError) as error:
                _resolve(contract, registry)
            assert error.value.code == "REQUIRED_RUNTIME_BINDING_UNAVAILABLE"
            assert error.value.diagnostic["availability"] == "UNKNOWN"
    if mode in {"expired", "future"}:
        assert facts["runtime.java.bin"]["reason_code"] == "evidence_expired_or_future"
        assert facts["runtime.java.bin"]["evidence_ids"] == []
    if mode == "undeclared":
        assert facts["runtime.java.bin"]["reason_code"] == "runtime_evidence_missing"

    async def forbidden_inspection(_):
        raise AssertionError("Assessment must not invoke a runtime probe")
    service = RuntimeEvidenceRecovery(data_path=str(tmp_path), scope_provider=lambda _: subject,
        inspect_runtime=forbidden_inspection, clock=lambda: now)
    scope = {"task_id": "expiry", "plan_id": "plan", "step_id": "science", "subject": subject,
        "selected_skill_id": subject["skill_id"], "execution_capability_id": contract["capability_id"],
        "capability_contract_digest": capability_contract_hash(contract),
        "required_binding_ids": contract["required_runtime_binding_ids"]}
    assessment = service._assessment({"task_state": task}, scope, contract)
    assert assessment["ready"] is allowed
    assert assessment["facts"]["runtime.java.bin"]["availability"] == ("AVAILABLE" if allowed else "UNKNOWN")

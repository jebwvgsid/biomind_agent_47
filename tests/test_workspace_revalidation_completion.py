"""Rejected reuse evidence must request a plan repair, never repeat the write."""

from copy import deepcopy

import pytest

from agent_core.semantic_completion import build_completion_report
from tests.test_semantic_completion import (
    _digest,
    _plan,
    _validation,
    _workspace_execution,
    _write_inputs,
)


def _case(tmp_path, status):
    plan = _plan(_write_inputs(tmp_path), workspace_mutations=[{
        "workspace_ref": "artifact.snpeff_data_dir",
        "mutation": "prepare_database",
    }])
    execution = _workspace_execution()
    receipt = execution["effect_evidence"]["workspace_mutations"].pop()
    receipt.update(attested=False, attestation_status=status)
    receipt["immutable_hash"] = _digest({
        key: value for key, value in receipt.items() if key != "immutable_hash"
    })
    execution["workspace_mutation_attestation"]["receipts"] = [receipt]
    return plan, execution


@pytest.mark.parametrize("status", [
    "idempotent_revalidation_contract_missing",
    "idempotent_revalidation_contract_invalid",
])
def test_rejected_reuse_contract_requests_plan_repair(tmp_path, status):
    plan, execution = _case(tmp_path, status)
    report = build_completion_report(
        plan_state=plan, validation=_validation(), execution_evidence=execution,
    )
    assert report.deterministic_veto is True
    assert report.effective_disposition == "plan_revision_required"
    result = report.predicate_results[0]
    assert result["reason_code"] == "WORKSPACE_REVALIDATION_CONTRACT_REQUIRED"
    assert result["parser_receipt"]["attestation_status"] == status


@pytest.mark.parametrize("tamper", ["hash", "scope", "producer"])
def test_untrusted_reuse_diagnostic_cannot_authorize_plan_repair(tmp_path, tamper):
    plan, execution = _case(tmp_path, "idempotent_revalidation_contract_missing")
    receipt = execution["workspace_mutation_attestation"]["receipts"][0]
    if tamper == "hash":
        receipt["immutable_hash"] = "sha256:tampered"
    else:
        receipt["attempt_id" if tamper == "scope" else "producer"] = "untrusted"
        receipt["immutable_hash"] = _digest({
            key: value for key, value in receipt.items() if key != "immutable_hash"
        })
    report = build_completion_report(
        plan_state=plan, validation=_validation(), execution_evidence=execution,
    )
    assert report.deterministic_veto is True
    assert report.effective_disposition == "correction_required"


def test_unpublished_successful_receipt_is_not_adopted_from_journal(tmp_path):
    plan, execution = _case(tmp_path, "idempotent_revalidation_contract_missing")
    receipt = deepcopy(_workspace_execution()["effect_evidence"]["workspace_mutations"][0])
    execution["workspace_mutation_attestation"]["receipts"] = [receipt]
    report = build_completion_report(
        plan_state=plan, validation=_validation(), execution_evidence=execution,
    )
    assert report.deterministic_veto is True
    assert report.predicate_results[0]["reason_code"] == "WORKSPACE_MUTATION_RECEIPT_MISSING"


def test_verification_limit_requires_replanning_instead_of_identical_retry(tmp_path):
    plan, execution = _case(tmp_path, "idempotent_revalidation_failed")
    receipt = execution["workspace_mutation_attestation"]["receipts"][0]
    receipt["controller_revalidation_evidence_validation"] = {
        "valid": False, "code": "WORKSPACE_REVALIDATION_LIMIT_EXCEEDED",
    }
    receipt["immutable_hash"] = _digest({
        key: value for key, value in receipt.items() if key != "immutable_hash"
    })
    report = build_completion_report(
        plan_state=plan, validation=_validation(), execution_evidence=execution,
    )
    assert report.deterministic_veto is True
    assert report.effective_disposition == "plan_revision_required"
    assert report.predicate_results[0]["reason_code"] == "WORKSPACE_REVALIDATION_LIMIT_EXCEEDED"


def test_readonly_step_does_not_require_mutation_receipts(tmp_path):
    report = build_completion_report(
        plan_state=_plan(_write_inputs(tmp_path)), validation=_validation(),
        execution_evidence={"effect_evidence": {"workspace_mutations": []}},
    )
    assert report.deterministic_veto is False
    assert report.predicate_results == ()

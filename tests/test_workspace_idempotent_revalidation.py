"""Unchanged-workspace adoption requires current Controller content checks."""

import hashlib
import os
from copy import deepcopy
from pathlib import Path

import pytest

from agent_core.workspace_attestation import (
    BOUNDED_SNAPSHOT_COLLECTOR,
    _digest,
    finalize_mutation_attestation,
    prepare_mutation_intents,
    start_mutation_attestation,
    validate_workspace_action_admission,
)
from agent_core.workspace_revalidation import (
    CONTROLLER_REVALIDATION_PRODUCER,
    collect_controller_workspace_revalidation,
)


def _journal(tmp_path: Path, *, contract=True, content=b">fixture\nACGT\n"):
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True)
    fasta = root / "data" / "fixture.fa"
    fasta.write_bytes(content)
    evidence = {
        "schema_version": 1,
        "collector": BOUNDED_SNAPSHOT_COLLECTOR,
        "allow_idempotent_revalidation": True,
        "postconditions": [
            {"kind": "directory_exists", "relative_path": "data"},
            {"kind": "file_nonempty", "relative_path": "data/{binding:genome}.fa"},
        ],
    }
    if contract:
        evidence["idempotent_revalidation"] = {
            "schema_version": 1,
            "verifier": "exact_file_sha256_v1",
            "files": [{
                "relative_path": "data/{binding:genome}.fa",
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            }],
        }
    binding = {
        "canonical_resource_id": "artifact.workspace",
        "resource_kind": "mutable_workspace",
        "plan_handle": "plan.workspace",
        "binding_revision": 1,
        "formal_concrete_path": str(root),
        "evidence_contract": evidence,
    }
    capability = {
        "operation": "prepare",
        "workspace_mutations": [{"workspace_ref": "artifact.workspace", "mutation": "stage_inputs"}],
    }
    action = {"produced_bindings": {"genome": {"type": "string", "value": "fixture"}}}
    admission = validate_workspace_action_admission(action, capability_contract=capability, workspace_bindings=[binding])
    intents = prepare_mutation_intents(
        admission, task_id="task-1", plan_id="plan-1", plan_revision=1,
        step_id="step-1", action_id="action-1", action_digest="action-digest", action=action,
    )
    return start_mutation_attestation(intents, attempt_id="attempt-1"), fasta


def _finalize(journal):
    rows = collect_controller_workspace_revalidation(journal, process_exit_code=0)
    return finalize_mutation_attestation(journal, process_exit_code=0, controller_revalidation_evidence=rows)["receipts"][0]


def test_controller_adopts_matching_unchanged_files_without_claiming_script_access(tmp_path):
    journal, fasta = _journal(tmp_path)
    before = fasta.stat()
    receipt = _finalize(journal)
    assert receipt["attested"] is True
    assert receipt["attestation_status"] == "revalidated_unchanged"
    assert receipt["changed_during_attempt"] is False
    assert receipt["effect"] == "revalidation/adoption"
    assert receipt["runtime_access_evidence"] == {}
    evidence = receipt["controller_revalidation_evidence"]
    assert evidence["producer"] == CONTROLLER_REVALIDATION_PRODUCER
    assert evidence["evidence_kind"] == "attempt_scoped_workspace_content_revalidation"
    assert receipt["controller_revalidation_evidence_validation"]["valid"] is True
    assert fasta.stat().st_mtime_ns == before.st_mtime_ns


def test_exit_zero_and_existing_files_without_strong_contract_is_explicitly_blocked(tmp_path):
    journal, _ = _journal(tmp_path, contract=False)
    receipt = _finalize(journal)
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "idempotent_revalidation_contract_missing"
    finding = receipt["controller_revalidation_evidence_validation"]
    assert finding["code"] == "WORKSPACE_REVALIDATION_CONTRACT_MISSING"
    assert "existence alone" in finding["reason"]


@pytest.mark.parametrize("problem", ["bad_hash", "uncovered_file", "extra_file", "duplicate_file", "unknown_verifier"])
def test_incomplete_or_invalid_contract_cannot_adopt_files(tmp_path, problem):
    journal, _ = _journal(tmp_path)
    intent = journal["intents"][0]
    contract = intent["evidence_contract"]["idempotent_revalidation"]
    if problem == "bad_hash":
        contract["files"][0]["sha256"] = "not-a-hash"
    elif problem == "uncovered_file":
        contract["files"] = []
    elif problem == "extra_file":
        contract["files"][0]["relative_path"] = "../outside.fa"
    elif problem == "duplicate_file":
        contract["files"].append(deepcopy(contract["files"][0]))
    else:
        contract["verifier"] = "trust_stdout"
    receipt = _finalize(journal)
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "idempotent_revalidation_contract_invalid"


def test_same_size_old_file_with_wrong_content_cannot_be_reused(tmp_path):
    journal, _ = _journal(tmp_path)
    contract = journal["intents"][0]["evidence_contract"]["idempotent_revalidation"]
    contract["files"][0]["sha256"] = "sha256:" + hashlib.sha256(b">fixture\nTGCA\n").hexdigest()
    receipt = _finalize(journal)
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "idempotent_revalidation_failed"
    assert "does not match" in receipt["controller_revalidation_evidence_validation"]["reason"]


@pytest.mark.parametrize("field", ["attempt_id", "action_digest", "formal_concrete_path", "verification_contract_hash"])
def test_resealed_evidence_with_wrong_scope_is_rejected(tmp_path, field):
    journal, _ = _journal(tmp_path)
    rows = collect_controller_workspace_revalidation(journal, process_exit_code=0)
    rows[0][field] = "wrong-scope"
    rows[0]["immutable_hash"] = _digest({key: value for key, value in rows[0].items() if key != "immutable_hash"})
    receipt = finalize_mutation_attestation(journal, process_exit_code=0, controller_revalidation_evidence=rows)["receipts"][0]
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "idempotent_revalidation_evidence_invalid"


def test_change_between_collection_and_finalization_is_not_relabelled_as_mutation(tmp_path):
    journal, fasta = _journal(tmp_path)
    rows = collect_controller_workspace_revalidation(journal, process_exit_code=0)
    fasta.write_bytes(b">fixture\nTGCA\n")
    receipt = finalize_mutation_attestation(journal, process_exit_code=0, controller_revalidation_evidence=rows)["receipts"][0]
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "idempotent_revalidation_evidence_invalid"


def test_large_file_change_with_restored_metadata_is_detected_by_second_content_check(tmp_path):
    journal, fasta = _journal(tmp_path, content=b"A" * (1024 * 1024 + 1))
    rows = collect_controller_workspace_revalidation(journal, process_exit_code=0)
    before = fasta.stat()
    with fasta.open("r+b") as stream:
        stream.write(b"T")
    os.utime(fasta, ns=(before.st_atime_ns, before.st_mtime_ns))
    receipt = finalize_mutation_attestation(journal, process_exit_code=0, controller_revalidation_evidence=rows)["receipts"][0]
    assert receipt["changed_during_attempt"] is False
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "idempotent_revalidation_failed"


def test_symlink_in_workspace_parent_cannot_be_revalidated(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    journal, _ = _journal(link)
    # Formal paths containing symlink parents fail even when target files match.
    # prepare_mutation_intents resolves the target, so a stale binding is rejected.
    receipt = _finalize(journal)
    assert receipt["attested"] is False


def test_linked_workspace_ancestor_is_blocked_before_snapshot_or_content_reads(tmp_path, monkeypatch):
    parent = tmp_path / "task"
    parent.mkdir()
    journal, _ = _journal(parent)
    target = tmp_path / "redirected"
    parent.rename(target)
    parent.symlink_to(target, target_is_directory=True)

    def unexpected_read(*args, **kwargs):
        pytest.fail("A linked workspace root must not be traversed or opened.")

    monkeypatch.setattr("agent_core.workspace_revalidation.capture_bounded_workspace_snapshot", unexpected_read)
    monkeypatch.setattr("agent_core.workspace_revalidation.open_approved_resource", unexpected_read)
    rows = collect_controller_workspace_revalidation(journal, process_exit_code=0)
    assert len(rows) == 1
    assert rows[0]["status"] == "blocked"
    assert rows[0]["code"] == "WORKSPACE_REVALIDATION_FAILED"
    assert "ARTIFACT_SYMLINK_DENIED" in rows[0]["reason"]
    assert rows[0]["verified_files"] == []
    # A failed inspection remains explicit even if the final snapshot differs.
    from agent_core.workspace_revalidation import validate_controller_workspace_revalidation
    result = validate_controller_workspace_revalidation(
        rows, intent=journal["intents"][0], after_snapshot={"snapshot_digest": "changed"},
    )
    assert result["valid"] is False
    assert result["code"] == "WORKSPACE_REVALIDATION_FAILED"
    assert "ARTIFACT_SYMLINK_DENIED" in result["reason"]


def test_controller_verification_is_bounded(tmp_path, monkeypatch):
    journal, _ = _journal(tmp_path)
    monkeypatch.setattr("agent_core.workspace_revalidation._MAX_REVALIDATION_BYTES", 3)
    receipt = _finalize(journal)
    assert receipt["attested"] is False
    assert receipt["controller_revalidation_evidence_validation"]["code"] == "WORKSPACE_REVALIDATION_LIMIT_EXCEEDED"


def test_failed_or_interrupted_process_cannot_authorize_adoption(tmp_path):
    journal, _ = _journal(tmp_path)
    for code in [1, None]:
        assert collect_controller_workspace_revalidation(journal, process_exit_code=code) == []
        rows = collect_controller_workspace_revalidation(journal, process_exit_code=0)
        receipt = finalize_mutation_attestation(journal, process_exit_code=code, controller_revalidation_evidence=rows)["receipts"][0]
        assert receipt["attested"] is False


def test_changed_workspace_uses_existing_mutation_attestation(tmp_path):
    journal, fasta = _journal(tmp_path, contract=False)
    fasta.write_bytes(b">fixture\nACGTA\n")
    assert collect_controller_workspace_revalidation(journal, process_exit_code=0) == []
    receipt = _finalize(journal)
    assert receipt["attested"] is True
    assert receipt["attestation_status"] == "mutated_and_validated"


def test_read_only_journal_has_no_revalidation_requirement():
    journal = start_mutation_attestation([], attempt_id="read-only")
    assert collect_controller_workspace_revalidation(journal, process_exit_code=0) == []
    finalized = finalize_mutation_attestation(journal, process_exit_code=0)
    assert finalized["status"] == "completed"
    assert finalized["receipts"] == []

"""P6: read-only startup observations and the real no-clobber report writer."""
from copy import deepcopy
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.workflow import runtime_recovery_reporting as reporting


def _task(root, task_id, target):
    state = load_plan_state(root, task_id)
    spec = create_task_spec("Write a report at the exact user-specified path " + str(target),
        task_id=task_id, base_dir=root, project_root=root, structured_source={"contract_source_type": "user_explicit",
            "expected_outputs": [{"role": "report", "path": str(target), "artifact_kind": "file", "required": True}]})
    state["current_task"] = {"task_spec": spec, "task_revision": task_spec_revision(spec)}
    save_plan_state(root, task_id, state, allow_canonical_task_write=True)
    return load_plan_state(root, task_id)


def _preflight(root, task_id):
    state = load_plan_state(root, task_id)
    before = deepcopy(state)
    result = reporting.report_publication_preflight(state, root / "task_outputs" / task_id)
    assert state == before
    assert result["publication_authorized"] is False
    assert result["final_writer_recheck_required"] is True
    return result


def _publish(root, task_id, target, body="Actual isolated diagnostic.\n"):
    return reporting.publish_report(root, task_id, target, body, owner="test:terminal",
        authorization_ref="explicit-isolated-test-output",
        scope_guard=lambda state: state["task_id"] == task_id)


def test_absent_target_preflight_creates_nothing_and_late_competitor_is_protected(tmp_path):
    target = tmp_path / "requested" / "report.md"
    _task(tmp_path, "new", target)
    result = _preflight(tmp_path, "new")
    assert result["status"] == "ready" and result["targets"][0]["status"] == "absent"
    assert not target.parent.exists()
    target.parent.mkdir()
    target.write_text("A different run arrived after preflight.\n")
    before = target.read_bytes()
    with pytest.raises(reporting.RuntimeReportError, match="RUNTIME_REPORT_EXISTING_FILE_PROTECTED"):
        _publish(tmp_path, "new", target)
    assert target.read_bytes() == before
    assert _preflight(tmp_path, "new")["status"] == "needs_user_decision"


@pytest.mark.parametrize("origin", ["other_task", "unowned_identical_content"])
def test_existing_report_is_not_authorized_by_content_or_foreign_receipt(tmp_path, origin):
    target = tmp_path / "medium" / "report.md"
    body = "Actual isolated diagnostic.\n"
    if origin == "other_task":
        _task(tmp_path, "old", target)
        _publish(tmp_path, "old", target, body)
    else:
        target.parent.mkdir()
        target.write_text(body)
    _task(tmp_path, "new", target)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    result = _preflight(tmp_path, "new")
    assert result["status"] == "needs_user_decision"
    assert result["conflicts"][0]["ownership"] == "unknown_or_other_task"
    assert result["reason_code"] == "RUNTIME_REPORT_EXISTING_FILE_PROTECTED"
    assert result["targets"][0]["path"] == str(target)
    with pytest.raises(reporting.RuntimeReportError, match="RUNTIME_REPORT_EXISTING_FILE_PROTECTED"):
        _publish(tmp_path, "new", target, body)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


def test_current_task_published_receipt_allows_same_operation_reentry_only(tmp_path):
    target = tmp_path / "medium" / "report.md"
    _task(tmp_path, "current", target)
    receipt = _publish(tmp_path, "current", target)
    inode = target.stat().st_ino
    result = _preflight(tmp_path, "current")
    assert result["status"] == "ready"
    assert result["targets"][0]["status"] == "owned_reentry"
    assert result["targets"][0]["publication_status"] == "published"
    assert _publish(tmp_path, "current", target) == receipt
    assert target.stat().st_ino == inode
    with pytest.raises(reporting.RuntimeReportError, match="RUNTIME_REPORT_FROZEN_BODY_CONFLICT"):
        _publish(tmp_path, "current", target, "Changed intent is not authorized by startup observation.\n")


def test_prepared_inode_reentry_is_read_only_then_real_writer_recovers(tmp_path):
    target = tmp_path / "medium" / "report.md"
    _task(tmp_path, "current", target)
    save = reporting.save_plan_state
    def lost_final_receipt(root, task_id, state):
        if any(row.get("status") == "published" for row in state["task_state"].get("report_publications", {}).values()):
            raise OSError("injected receipt persistence interruption")
        return save(root, task_id, state)
    with patch.object(reporting, "save_plan_state", lost_final_receipt), pytest.raises(OSError, match="injected receipt"):
        _publish(tmp_path, "current", target)
    proofs = list(target.parent.glob(".runtime-report-proof-*"))
    assert len(proofs) == 1
    assert proofs[0].stat().st_ino == target.stat().st_ino
    result = _preflight(tmp_path, "current")
    assert result["targets"][0]["publication_status"] == "prepared"
    assert proofs[0].exists(), "Startup preflight must not retire the writer's recovery proof."
    receipt = _publish(tmp_path, "current", target)
    assert receipt["recovered_from"] == "prepared_inode_hardlink"
    assert not proofs[0].exists()


def test_changed_owned_file_cannot_resume_based_on_stale_receipt(tmp_path):
    target = tmp_path / "medium" / "report.md"
    _task(tmp_path, "current", target)
    _publish(tmp_path, "current", target)
    target.write_text("Externally changed after publication.\n")
    result = _preflight(tmp_path, "current")
    assert result["status"] == "needs_user_decision"
    assert result["reason_code"] == "RUNTIME_REPORT_OWNED_FILE_CHANGED"
    with pytest.raises(reporting.RuntimeReportError, match="RUNTIME_REPORT_OWNED_FILE_CHANGED"):
        _publish(tmp_path, "current", target)


def test_identical_content_replacement_during_ownership_read_is_rejected(tmp_path):
    target = tmp_path / "medium" / "report.md"
    _task(tmp_path, "current", target)
    _publish(tmp_path, "current", target)
    state = load_plan_state(tmp_path, "current")
    original_open = reporting.os.open
    replaced = []
    def replace_after_open(name, flags, *args, **kwargs):
        descriptor = original_open(name, flags, *args, **kwargs)
        if name == target.name and kwargs.get("dir_fd") is not None and not replaced:
            body = target.read_bytes()
            target.rename(target.with_name("original-owned-report"))
            target.write_bytes(body)
            replaced.append(True)
        return descriptor
    with patch.object(reporting.os, "open", replace_after_open):
        result = reporting.report_publication_preflight(state, tmp_path / "task_outputs" / "current")
    assert replaced
    assert result["status"] == "needs_user_decision"
    assert result["reason_code"] == "RUNTIME_REPORT_OWNED_FILE_CHANGED"


@pytest.mark.parametrize("symlink_parent", [False, True])
def test_preflight_does_not_follow_symlink_output_or_parent(tmp_path, symlink_parent):
    protected = tmp_path / "protected"
    protected.mkdir()
    original = protected / "report.md"
    original.write_text("Protected unrelated report.\n")
    requested = tmp_path / "requested"
    state = _task(tmp_path, "new", requested / "report.md")
    if symlink_parent:
        requested.symlink_to(protected, target_is_directory=True)
    else:
        requested.mkdir()
        (requested / "report.md").symlink_to(original)
    # The previously canonical path is now a symlink. No contract is mutated
    # to inject a new target after canonicalization.
    result = reporting.report_publication_preflight(state, tmp_path / "task_outputs" / "new")
    assert result["status"] in {"needs_user_decision", "unverified"}, result
    if result["targets"]:
        assert result["targets"][0]["status"] == ("unverified" if symlink_parent else "conflict")
    assert original.read_text() == "Protected unrelated report.\n"

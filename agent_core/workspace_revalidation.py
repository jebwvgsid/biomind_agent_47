"""Controller-owned content checks for explicitly authorized workspace reuse.

Existence and an exit-zero process do not prove input identity.  Reuse requires
an approved ``idempotent_revalidation`` contract naming every file postcondition
and its expected SHA-256.  These are Controller reads, never subprocess access
observations.  The caller must supply its verified, attempt-scoped journal.
"""

from __future__ import annotations

import hashlib
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_core.approved_resource_locator import (
    ResourceLocatorError,
    approved_resource_locator,
    open_approved_resource,
)
from agent_core.workspace_attestation import (
    _RUNTIME_ACCESS_SCOPE_FIELDS,
    _digest,
    _evaluate_postcondition,
    _now,
    capture_bounded_workspace_snapshot,
)


CONTROLLER_REVALIDATION_PRODUCER = "biomind.controller.workspace_revalidator.v1"
_MAX_REVALIDATION_BYTES = 16 * 1024 * 1024 * 1024
_FILE_KINDS = {"file_exists", "file_nonempty"}


class _RevalidationFailure(ValueError):
    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


def _fail(reason: str, code: str = "WORKSPACE_REVALIDATION_CONTRACT_INVALID"):
    raise _RevalidationFailure(code, reason)


def _contract_files(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence_contract = intent.get("evidence_contract") or {}
    contract = evidence_contract.get("idempotent_revalidation")
    if contract is None:
        _fail(
            "Workspace reuse needs an approved exact_file_sha256_v1 contract "
            "covering every file postcondition; existence alone cannot verify inputs.",
            "WORKSPACE_REVALIDATION_CONTRACT_MISSING",
        )
    if not isinstance(contract, Mapping) or (
        contract.get("schema_version") != 1
        or contract.get("verifier") != "exact_file_sha256_v1"
    ):
        _fail("Unsupported workspace reuse verifier or schema version.")
    conditions = intent.get("resolved_postconditions") or []
    originals = evidence_contract.get("postconditions") or []
    if not isinstance(conditions, list) or not conditions:
        _fail("Workspace reuse requires explicit resolved postconditions.")
    root = Path(str(intent.get("formal_concrete_path") or ""))
    if not root.is_absolute() or ".." in root.parts:
        _fail("Workspace reuse requires an absolute formal workspace path.")
    targets: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    for condition in conditions:
        if not isinstance(condition, Mapping):
            _fail("Malformed workspace postcondition.")
        kind = condition.get("kind")
        if kind not in _FILE_KINDS | {"directory_exists"}:
            _fail("Workspace reuse does not support this postcondition kind.")
        relative = str(condition.get("relative_path") or "")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            _fail("Workspace postcondition escapes its formal workspace.")
        target = str(root / path)
        if (
            str(condition.get("formal_target_path") or "") != target
            or str(condition.get("formal_workspace_root") or "") != str(root)
        ):
            _fail("Workspace postcondition path differs from its frozen binding.")
        if kind == "directory_exists":
            continue
        if not relative:
            _fail("A reusable file must be below the formal workspace directory.")
        targets[relative] = {"relative_path": relative, "path": target}
        aliases[relative] = relative
        index = condition.get("postcondition_index")
        if type(index) is int and 0 <= index < len(originals):
            original = originals[index]
            if isinstance(original, Mapping):
                template = str(original.get("relative_path") or "")
                if template in aliases and aliases[template] != relative:
                    _fail("Ambiguous workspace postcondition path template.")
                aliases[template] = relative
    files = contract.get("files")
    if not targets or not isinstance(files, list) or not files:
        _fail("Workspace reuse requires a SHA-256 for every file postcondition.")
    expected: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, Mapping):
            _fail("Malformed workspace file digest contract.")
        relative = aliases.get(str(item.get("relative_path") or ""), "")
        digest = str(item.get("sha256") or "")
        if digest.startswith("sha256:"):
            digest = digest[7:]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            _fail("Workspace reuse requires a complete SHA-256 digest.")
        if relative not in targets or relative in expected:
            _fail("Workspace reuse file digests are duplicated or outside postconditions.")
        expected[relative] = {**targets[relative], "sha256": "sha256:" + digest}
    if set(expected) != set(targets):
        _fail("Workspace reuse file digests do not cover all file postconditions.")
    return [expected[key] for key in sorted(expected)]


def _read_verified_files(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected = _contract_files(intent)
    try:
        approved_resource_locator({"canonical_path": intent["formal_concrete_path"]})
        if not Path(intent["formal_concrete_path"]).is_dir():
            _fail("Formal workspace is not a directory.", "WORKSPACE_REVALIDATION_FAILED")
        checks = [_evaluate_postcondition(item) for item in intent["resolved_postconditions"]]
        if any(item.get("status") != "satisfied" for item in checks):
            _fail("Formal workspace postconditions are not satisfied.", "WORKSPACE_REVALIDATION_FAILED")
        remaining = _MAX_REVALIDATION_BYTES
        verified = []
        for item in expected:
            with open_approved_resource(item["path"]) as stream:
                before = os.fstat(stream.fileno())
                if before.st_size > remaining:
                    _fail("Workspace content exceeds the Controller verification byte limit.", "WORKSPACE_REVALIDATION_LIMIT_EXCEEDED")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = stream.read(min(1024 * 1024, remaining + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    remaining -= len(chunk)
                    if remaining < 0:
                        _fail("Workspace content exceeds the Controller verification byte limit.", "WORKSPACE_REVALIDATION_LIMIT_EXCEEDED")
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                # Check the named path again: an open descriptor can outlive replacement.
                with open_approved_resource(item["path"]) as named_stream:
                    current = os.fstat(named_stream.fileno())
                if identity(before) != identity(after) or identity(after) != identity(current):
                    _fail("Workspace file changed during Controller verification.", "WORKSPACE_REVALIDATION_FAILED")
                observed = "sha256:" + digest.hexdigest()
                if observed != item["sha256"]:
                    _fail("Workspace file content does not match the approved SHA-256: " + item["relative_path"], "WORKSPACE_REVALIDATION_FAILED")
                verified.append({**item, "size": size, "observed_sha256": observed})
        return verified
    except (OSError, ResourceLocatorError) as exc:
        _fail("Workspace cannot be safely read: " + type(exc).__name__, "WORKSPACE_REVALIDATION_FAILED")


def collect_controller_workspace_revalidation(
    journal: Mapping[str, Any], *, process_exit_code: int | None
) -> list[dict[str, Any]]:
    """Read unchanged formal files and return scoped evidence or explicit blockers.

    This API consumes only a Controller-verified journal. Expected hashes must
    come from the approved evidence contract (for staged inputs, their approved
    source digests), never from generated code, stdout, or current file contents.
    Changed workspaces continue through ordinary mutation attestation.
    """
    if process_exit_code != 0:
        return []
    rows = []
    for intent in journal.get("intents", []) or []:
        if intent.get("allow_idempotent_revalidation") is not True:
            continue
        before = intent.get("before_evidence") or {}
        row = {
            "schema_version": 1,
            "producer": CONTROLLER_REVALIDATION_PRODUCER,
            "evidence_kind": "attempt_scoped_workspace_content_revalidation",
            **{field: deepcopy(intent.get(field)) for field in _RUNTIME_ACCESS_SCOPE_FIELDS},
            "verification_contract_hash": _digest(intent.get("evidence_contract") or {}),
            "postcondition_contract_hash": _digest(intent.get("resolved_postconditions") or []),
            "workspace_snapshot_digest": before.get("snapshot_digest"),
            "observed_at": _now(),
        }
        try:
            # The legacy bounded snapshot opens small files while traversing.
            # Reject a linked root/ancestor before allowing that traversal.
            approved_resource_locator({
                "canonical_path": str(intent.get("formal_concrete_path") or "")
            })
        except (OSError, ResourceLocatorError) as exc:
            row.update(
                status="blocked",
                code="WORKSPACE_REVALIDATION_FAILED",
                reason="Workspace root cannot be safely inspected: " + str(getattr(exc, "code", type(exc).__name__)),
                verified_files=[],
            )
            row["immutable_hash"] = _digest(row)
            rows.append(row)
            continue
        snapshot = capture_bounded_workspace_snapshot(str(intent.get("formal_concrete_path") or ""))
        if not snapshot.get("reliable") or not before.get("reliable"):
            continue
        if snapshot.get("snapshot_digest") != before.get("snapshot_digest"):
            continue
        row["workspace_snapshot_digest"] = snapshot["snapshot_digest"]
        try:
            row["verified_files"] = _read_verified_files(intent)
            row.update(status="verified", code="WORKSPACE_REVALIDATION_VERIFIED", reason="Controller verified every formal file against its approved SHA-256.")
        except _RevalidationFailure as exc:
            row.update(status="blocked", code=exc.code, reason=exc.reason, verified_files=[])
        row["immutable_hash"] = _digest(row)
        rows.append(row)
    return rows


def validate_controller_workspace_revalidation(
    rows: Sequence[Mapping[str, Any]], *, intent: Mapping[str, Any], after_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Recheck scope, frozen contracts and contents at receipt finalization."""
    matches = [dict(row) for row in rows if isinstance(row, Mapping) and row.get("intent_id") == intent.get("intent_id")]
    if not matches:
        return {"valid": False, "code": "WORKSPACE_REVALIDATION_EVIDENCE_MISSING", "reason": "No Controller content revalidation was collected for this attempt.", "evidence": {}}
    if len(matches) != 1:
        return {"valid": False, "code": "WORKSPACE_REVALIDATION_EVIDENCE_INVALID", "reason": "Ambiguous Controller revalidation evidence.", "evidence": {}}
    row = matches[0]
    result = {"valid": False, "code": "WORKSPACE_REVALIDATION_EVIDENCE_INVALID", "reason": "Controller revalidation evidence has stale scope or invalid integrity.", "evidence": row}
    if (
        row.get("schema_version") != 1
        or row.get("producer") != CONTROLLER_REVALIDATION_PRODUCER
        or row.get("evidence_kind") != "attempt_scoped_workspace_content_revalidation"
        or any(row.get(field) != intent.get(field) for field in _RUNTIME_ACCESS_SCOPE_FIELDS)
        or row.get("immutable_hash") != _digest({key: value for key, value in row.items() if key != "immutable_hash"})
        or row.get("verification_contract_hash") != _digest(intent.get("evidence_contract") or {})
        or row.get("postcondition_contract_hash") != _digest(intent.get("resolved_postconditions") or [])
    ):
        return result
    if row.get("status") == "blocked":
        return {**result, "code": row.get("code"), "reason": row.get("reason")}
    if (
        row.get("status") != "verified"
        or row.get("workspace_snapshot_digest") != after_snapshot.get("snapshot_digest")
    ):
        return result
    try:
        files = _read_verified_files(intent)
        if files != row.get("verified_files"):
            return {**result, "reason": "Workspace content changed after Controller revalidation."}
    except _RevalidationFailure as exc:
        return {**result, "code": exc.code, "reason": exc.reason}
    return {**result, "valid": True, "code": "WORKSPACE_REVALIDATION_VERIFIED", "reason": "Controller verified current workspace content against the approved contract."}

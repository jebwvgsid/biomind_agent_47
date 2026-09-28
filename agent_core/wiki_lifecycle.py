"""Optional-memory lifecycle decisions and the existing durable ingestion outbox.

Wiki capture does not require scientific completion. Legacy snapshot fields remain readable.
The journal describes optional memory IO, not scientific success. request_ended
is a request boundary, never a completed experiment. Old completion metadata is
kept for compatibility and does not authorize or block current maintenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def terminal_snapshot_projection(
    *,
    task_id: str,
    terminal_status: str,
    task_status: str,
    plan_state: Optional[Mapping[str, Any]] = None,
    consultation_state: Optional[Mapping[str, Any]] = None,
    waiting_business_question: bool = False,
    managed_task: bool = True,
    compiled_contract_verified: bool = False,
) -> Dict[str, Any]:
    plan = dict(plan_state or {})
    consultation = dict(consultation_state or {})
    protocol = dict(consultation.get("finalize_protocol") or {})
    schema = int(protocol.get("schema_version") or 0)
    if schema == 4:
        compiled_ref = str(
            (protocol.get("compile_outcome") or {}).get("compiled_contract_ref") or ""
        )
        compiled_hash = str(
            (protocol.get("compile_outcome") or {}).get("compiled_contract_hash") or ""
        )
        finalize_error = str(protocol.get("terminal_error_code") or "")
        finalize_status = str(protocol.get("finalize_status") or "")
    else:
        final_contract = dict(consultation.get("final_contract") or {})
        compiled_ref = str(final_contract.get("compiled_contract_ref") or "")
        # Legacy v2 kept the immutable contract in graph state and bound it to
        # the protected response artifact instead of a standalone contract ref.
        compiled_hash = str(final_contract.get("compiled_contract_hash") or "")
        if compiled_hash and not compiled_ref:
            compiled_ref = str(final_contract.get("response_artifact_ref") or "legacy-v2-graph-state")
        finalize_error = str(
            protocol.get("terminal_error_code")
            or consultation.get("terminal_error_code")
            or ""
        )
        finalize_status = str(protocol.get("status") or "")

    steps = [item for item in plan.get("steps", []) or [] if isinstance(item, Mapping)]
    # plan_version/plan_revision exist on the initialized empty plan and are
    # not evidence that Planner committed a semantic plan.  Require the
    # durable semantic hash and at least one persisted step; an explicit flag
    # alone is likewise only a projection, not a commit artifact.
    planner_committed = bool(
        steps and str(plan.get("semantic_plan_hash") or "").strip()
    )
    projection = {
        "task_id": str(task_id or ""),
        "terminal_status": str(terminal_status or "").lower(),
        "task_status": str(task_status or "").lower(),
        "managed_task": bool(managed_task),
        "planner_committed": planner_committed,
        "compiled_contract_ref": compiled_ref,
        "compiled_contract_hash": compiled_hash,
        "compiled_contract_verified": bool(compiled_contract_verified),
        "active_finalize_error": finalize_error,
        "finalize_status": finalize_status,
        "waiting_business_question": bool(waiting_business_question),
        "plan_identity": {
            "plan_revision": plan.get("plan_revision", plan.get("plan_version")),
            "task_revision": plan.get("task_revision"),
            "step_ids": [str(item.get("step_id") or "") for item in steps],
            "step_statuses": [str(item.get("status") or "") for item in steps],
        },
    }
    projection["terminal_snapshot_hash"] = _digest(projection)
    return projection


def evaluate_wiki_eligibility(
    snapshot: Mapping[str, Any],
    *,
    decision_kind: str,
) -> Dict[str, Any]:
    """Return a typed, fail-closed offer/ingest decision."""
    kind = str(decision_kind or "").strip()
    if kind not in {"offer", "ingest"}:
        raise ValueError("decision_kind must be offer or ingest")
    status = str(snapshot.get("task_status") or "").lower()
    terminal = str(snapshot.get("terminal_status") or "").lower()
    reason = "eligible"
    eligible = True
    # Optional knowledge capture is independent of scientific completion.
    # Cancellation is a lifecycle fence, not a semantic eligibility Gate.
    if status == 'cancelled' or terminal == 'cancelled':
        eligible, reason = False, 'request_cancelled'
    decision = {
        "schema_version": 1,
        "record_type": f"wiki_{kind}_decision",
        "decision_kind": kind,
        "task_id": str(snapshot.get("task_id") or ""),
        "eligible": eligible,
        "reason": reason,
        "terminal_snapshot_hash": str(snapshot.get("terminal_snapshot_hash") or ""),
        "compiled_contract_ref": str(snapshot.get("compiled_contract_ref") or ""),
        "compiled_contract_hash": str(snapshot.get("compiled_contract_hash") or ""),
        "planner_committed": bool(snapshot.get("planner_committed")),
        "terminal_status": terminal,
        "task_status": status,
        "evidence": deepcopy(dict(snapshot)),
    }
    identity = {key: value for key, value in decision.items() if key != "decision_hash"}
    decision["decision_hash"] = _digest(identity)
    return decision


class WikiLifecycleStore:
    """Durable decision/outbox store scoped to one task."""

    def __init__(self, data_path: str | Path, task_id: str) -> None:
        self.task_id = str(task_id or "").strip()
        if not self.task_id:
            raise ValueError("task_id is required")
        self.root = (
            Path(data_path).resolve()
            / "task_outputs"
            / self.task_id
            / "wiki_lifecycle"
        )

    def persist_decision(self, decision: Mapping[str, Any]) -> Dict[str, Any]:
        row = deepcopy(dict(decision or {}))
        expected_hash = _digest({key: value for key, value in row.items() if key not in {"decision_hash", "committed_at", "decision_ref"}})
        supplied = str(row.get("decision_hash") or "")
        # evaluate_wiki_eligibility hashes the same semantic row before volatile
        # commit metadata.  Recompute when callers supplied a projection built
        # by older code, but never accept a conflicting non-empty hash.
        if supplied and supplied != expected_hash:
            semantic = {key: value for key, value in row.items() if key != "decision_hash"}
            if supplied != _digest(semantic):
                raise ValueError("Wiki eligibility decision hash mismatch")
            expected_hash = supplied
        row["decision_hash"] = expected_hash
        row["committed_at"] = str(row.get("committed_at") or _now())
        path = self.root / "decisions" / f"{row.get('record_type', 'wiki_decision')}-{expected_hash[:24]}.json"
        row["decision_ref"] = str(path)
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if str(existing.get("decision_hash") or "") != expected_hash:
                raise ValueError("Wiki decision artifact conflict")
            return existing
        _atomic_json(path, row)
        return row

    def prepare_outbox(
        self,
        *,
        eligibility_decision: Mapping[str, Any],
        project_id: str,
        operation: str,
        content_hash: str,
    ) -> Dict[str, Any]:
        if not bool(eligibility_decision.get("eligible")):
            raise ValueError("ineligible Wiki decision cannot prepare an outbox item")
        identity = {
            "task_id": self.task_id,
            "project_id": str(project_id or ""),
            "operation": str(operation or "ingest"),
            "terminal_snapshot_hash": str(eligibility_decision.get("terminal_snapshot_hash") or ""),
            "compiled_contract_hash": str(eligibility_decision.get("compiled_contract_hash") or ""),
            "content_hash": str(content_hash or "unknown"),
        }
        outbox_id = "wiki-outbox-" + _digest(identity)[:32]
        path = self.root / "outbox" / f"{outbox_id}.json"
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if dict(existing.get("identity") or {}) != identity:
                raise ValueError("Wiki outbox identity conflict")
            return existing
        row = {
            "schema_version": 1,
            "record_type": "wiki_ingestion_outbox",
            "outbox_id": outbox_id,
            "identity": identity,
            "status": "prepared",
            "eligibility_decision_ref": str(eligibility_decision.get("decision_ref") or ""),
            "eligibility_decision_hash": str(eligibility_decision.get("decision_hash") or ""),
            "prepared_at": _now(),
            "dispatched_at": "",
            "acknowledged_at": "",
            "destination_result": {},
        }
        _atomic_json(path, row)
        return row

    def transition_outbox(
        self,
        outbox_id: str,
        *,
        status: str,
        destination_result: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        target = str(status or "")
        if target not in {"dispatched", "succeeded", "failed", "partial", "outcome_unknown"}:
            raise ValueError("unsupported Wiki outbox transition")
        path = self.root / "outbox" / f"{str(outbox_id or '')}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Unknown Wiki outbox item: {outbox_id}")
        row = json.loads(path.read_text(encoding="utf-8"))
        current = str(row.get("status") or "prepared")
        legal = {
            "prepared": {"dispatched", "failed"},
            "dispatched": {"dispatched", "succeeded", "failed", "partial", "outcome_unknown"},
            "succeeded": {"succeeded"},
            "failed": {"failed", "dispatched"},
            "partial": {"partial", "dispatched"},
            "outcome_unknown": {"outcome_unknown", "dispatched"},
        }
        if target not in legal.get(current, set()):
            raise ValueError(f"illegal Wiki outbox transition: {current}->{target}")
        if current == target:
            return row
        row["status"] = target
        if target == "dispatched":
            row["dispatched_at"] = _now()
        else:
            row["acknowledged_at"] = _now()
            row["destination_result"] = deepcopy(dict(destination_result or {}))
        _atomic_json(path, row)
        return row

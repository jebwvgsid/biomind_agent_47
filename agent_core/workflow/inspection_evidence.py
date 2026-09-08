"""Episode-scoped inspection snapshots over the existing StateContractStore.

Objects are immutable; their handles are published with the causal packet in
PlanTracker's CAS commit.  This module owns neither routing nor step completion.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from agent_core.state_contract_store import StateContractStore, StateContractIntegrityError

INSPECTION_BACKEND = "state_contract_inspection"
INSPECTION_OBJECT_KINDS = frozenset({
    "validation", "completion_report", "semantic_decision", "step_attempt", "execution",
})
INSPECTION_SCOPE_FIELDS = (
    "task_id", "plan_id", "plan_version", "step_id", "attempt_id", "episode_id",
)
MAX_INSPECTION_OBJECT_BYTES = 2 * 1024 * 1024
MAX_INSPECTION_AUTHORITY_BYTES = 16 * 1024 * 1024
MAX_INSPECTION_READ_LINES = 120
MAX_INSPECTION_READ_CHARS = 16_000


class InspectionEvidenceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(code + (": " + detail if detail else ""))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def inspection_scope(packet: Any, episode_id: str) -> dict[str, str]:
    return {
        "task_id": packet.task_id,
        "plan_id": packet.plan_spec_ref.plan_id,
        "plan_version": packet.plan_spec_ref.plan_version,
        "step_id": packet.step_id,
        "attempt_id": packet.attempt_ref,
        "episode_id": str(episode_id),
    }


def _object_type(kind: str) -> str:
    if kind not in INSPECTION_OBJECT_KINDS:
        raise InspectionEvidenceError("INSPECTION_OBJECT_TYPE_INVALID", kind)
    return "recovery_inspection." + kind


def _handle(scope: Mapping[str, Any], kind: str, source_id: str, digest: str = "") -> str:
    return "inspection:sha256:" + hashlib.sha256(_canonical({
        "scope": dict(scope), "kind": kind, "source_object_id": source_id,
        "digest": digest,
    }).encode()).hexdigest()


def _safe_error_detail(exc: Exception) -> str:
    text = str(getattr(exc, "detail", "") or exc)
    for value in (getattr(exc, "filename", None), getattr(exc, "filename2", None)):
        if value:
            text = text.replace(str(value), "[path]")
    # Store errors may embed an absolute path in their message rather than the
    # OSError filename attributes.  The model needs errno/reason, not locators.
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s'\";:,)]*)", "[path]", text)
    return text[:400]


def _assert_storage_path(store: StateContractStore, leaf: Path | None = None) -> None:
    path = leaf or store.object_root
    # Check ancestors before descendants so a symlink above data_path is also
    # denied without opening a byte of the target object.
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise InspectionEvidenceError("INSPECTION_OBJECT_PATH_DENIED")


def read_execution_facts(store: StateContractStore, reference: str) -> dict[str, Any]:
    """Bounded immutable-store read; the caller must still check attempt scope."""
    from agent_core.approved_resource_locator import open_approved_resource
    digest = store._parse_ref(reference, "object")
    path = store.object_root / (digest + ".json")
    _assert_storage_path(store, path)
    with open_approved_resource(path) as stream:
        encoded = stream.read(MAX_INSPECTION_OBJECT_BYTES + 1)
    if len(encoded) > MAX_INSPECTION_OBJECT_BYTES:
        raise InspectionEvidenceError("INSPECTION_OBJECT_SIZE_LIMIT")
    record = json.loads(encoded)
    if hashlib.sha256(_canonical(record).encode()).hexdigest() != digest:
        raise InspectionEvidenceError("INSPECTION_OBJECT_INTEGRITY_ERROR")
    if (not isinstance(record, Mapping) or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1 or record.get("object_type") != "execution.recovery_facts"
            or not isinstance(record.get("payload"), Mapping)):
        raise InspectionEvidenceError("INSPECTION_OBJECT_SCHEMA_INVALID")
    return dict(record["payload"])


def _assert_body_scope(kind: str, body: Mapping[str, Any], scope: Mapping[str, Any]) -> None:
    if type(body.get("schema_version")) is not int or body["schema_version"] != 1:
        raise InspectionEvidenceError("INSPECTION_OBJECT_SCHEMA_INVALID", kind)
    actual = body if kind == "validation" else body.get("scope")
    if kind == "step_attempt":
        actual = body.get("scope")
    if not isinstance(actual, Mapping):
        raise InspectionEvidenceError("INSPECTION_OBJECT_SCOPE_MISSING", kind)
    fields = ("task_id", "step_id", "attempt_id") if kind == "validation" else (
        "task_id", "plan_id", "plan_version", "step_id", "attempt_id",
    )
    for field in fields:
        if str(actual.get(field) or "") != str(scope.get(field) or ""):
            raise InspectionEvidenceError("INSPECTION_OBJECT_SCOPE_MISMATCH", kind + ":" + field)


def assert_current_inspection_scope(
    durable: Mapping[str, Any], scope: Mapping[str, Any], *, require_episode: bool = True,
) -> Mapping[str, Any]:
    """Authorize against current durable identity, never a cached generation.

    The producer checks before publication; readers additionally require the
    published active episode. A retained packet cannot authorize a new attempt.
    """
    if any(not str(scope.get(key) or "") for key in INSPECTION_SCOPE_FIELDS):
        raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISSING")
    for field, value in (
        ("task_id", durable.get("task_id")), ("plan_id", durable.get("plan_id")),
        ("plan_version", durable.get("plan_version")),
        ("step_id", durable.get("current_step_id")),
    ):
        if str(value or "") != str(scope[field]):
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", field)
    raw_steps = durable.get("steps") or []
    rows = raw_steps.values() if isinstance(raw_steps, Mapping) else raw_steps
    step = next((row for row in rows if isinstance(row, Mapping)
                 and str(row.get("step_id") or "") == scope["step_id"]), {})
    task = dict(durable.get("task_state") or {})
    pending = dict(task.get("verification_pending") or {})
    attempts = [str(value) for value in (step.get("verification_attempt_id"),
                pending.get("attempt_id")) if value]
    active_record = (task.get("causal_failure_packets") or {}).get(task.get("active_failure_packet_ref")) or {}
    execution_episode = active_record.get("source_kind") == "execute_outcome"
    if execution_episode:
        attempts = [str((active_record.get("failure_packet") or {}).get("attempt_ref") or "")]
    if (not attempts or any(value != scope["attempt_id"] for value in attempts)
            or (pending.get("step_id") and str(pending["step_id"]) != scope["step_id"])):
        raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", "attempt_id")
    if (not step or str(step.get("status") or "") in {"done", "passed", "completed", "cancelled", "skipped"}
            or str(task.get("status") or "") in {"completed", "cancelled"}):
        raise InspectionEvidenceError("INSPECTION_EPISODE_INACTIVE")
    if not require_episode:
        return {}
    active = str(task.get("active_failure_packet_ref") or "")
    record = (task.get("causal_failure_packets") or {}).get(active) or {}
    packet = record.get("failure_packet") or {}
    plan = packet.get("plan_spec_ref") or {}
    actual = {
        "task_id": packet.get("task_id"), "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("plan_version"), "step_id": packet.get("step_id"),
        "attempt_id": packet.get("attempt_ref"),
        "episode_id": "recovery-seed-" + str(record.get("source_fingerprint") or ""),
    }
    if (not active or record.get("source_kind") not in {"verification_outcome", "execute_outcome"}):
        raise InspectionEvidenceError("INSPECTION_EPISODE_INACTIVE")
    for field in INSPECTION_SCOPE_FIELDS:
        if str(actual.get(field) or "") != str(scope[field]):
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", field)
    return packet


def prepare_inspection_evidence(
    *, data_path: str, durable: Mapping[str, Any], state: Mapping[str, Any],
    source: Mapping[str, Any], packet: Any, episode_id: str,
) -> dict[str, dict[str, Any]]:
    """Write real producer objects, returning unpublished registry descriptors.

    Failures are explicit unavailable descriptors, so one optional storage
    failure does not remove the remaining lawful inspection capabilities.
    """
    scope = inspection_scope(packet, episode_id)
    is_execution = source.get("type") == "execution_failed"
    if not is_execution:
        assert_current_inspection_scope(durable, scope, require_episode=False)
    else:
        for key in ("task_id", "plan_id", "plan_version"):
            if str(durable.get(key) or "") != scope[key]:
                raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", key)
        if str(durable.get("current_step_id") or "") != scope["step_id"]:
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", "step_id")
    payload = dict(source.get("payload") or {})
    steps = durable.get("steps") or []
    rows = steps.values() if isinstance(steps, Mapping) else steps
    step = next((deepcopy(dict(row)) for row in rows if isinstance(row, Mapping)
                 and str(row.get("step_id") or "") == scope["step_id"]), {})
    plan_steps = ((durable.get("current_plan") or {}).get("plan_spec") or {}).get("steps") or []
    plan_rows = plan_steps.values() if isinstance(plan_steps, Mapping) else plan_steps
    contract_step = next((deepcopy(dict(row)) for row in plan_rows if isinstance(row, Mapping)
                          and str(row.get("step_id") or "") == scope["step_id"]), {})
    execution = dict(state.get("execution_evidence") or {})
    if not execution:
        execution = dict(((state.get("execute_outcome") or {}).get("payload") or {}).get("execution_record") or {})
    if not execution:
        execution = dict(((durable.get("provenance_state") or {}).get("attempts") or {}).get(scope["attempt_id"]) or {})
    # A different attempt's execution can never enter the current snapshot.
    execution_mismatch = (
        any(str(execution.get(k) or "") != scope[k]
            for k in ("step_id", "attempt_id") if execution)
        or any(k in execution and str(execution[k] or "") != scope[k]
               for k in ("task_id", "plan_id", "plan_version"))
    )
    snapshot = {
        "schema_version": 1, "scope": scope, "approved_step": contract_step,
        "plan_approval_status": str(durable.get("approval_status") or "unknown"),
        "step_snapshot_unavailable_reason": "" if contract_step else "HISTORICAL_STEP_SNAPSHOT_UNAVAILABLE",
        "execution_record": {} if execution_mismatch else execution,
        "execution_unavailable_reason": "EXECUTION_SCOPE_MISMATCH" if execution_mismatch else ("HISTORICAL_BODY_UNAVAILABLE" if not execution else ""),
        "verification_status": str(step.get("status") or "unknown"),
        "commit_status": "not_committed" if str(step.get("status") or "") != "completed" else "committed",
    }
    objects = [
        ("validation", str((payload.get("validation_evidence") or {}).get("validation_id") or "validation"), payload.get("validation_evidence")),
        ("completion_report", str((payload.get("completion_report") or {}).get("report_id") or "completion_report"), payload.get("completion_report")),
        ("semantic_decision", str((payload.get("semantic_decision") or {}).get("decision_id") or "semantic_decision"), payload.get("semantic_decision")),
        ("step_attempt", scope["step_id"] + ":" + scope["attempt_id"], snapshot),
    ]
    if is_execution:
        record = dict(payload.get("execution_record") or {})
        source_error = None
        if record.get("recovery_execution_facts_ref"):
            try:
                held = read_execution_facts(StateContractStore(data_path, scope["task_id"]), str(record["recovery_execution_facts_ref"]))
                for field in ("plan_id", "plan_version", "step_id", "attempt_id", "action_id"):
                    if str(held.get(field) or "") != str(record.get(field) or ""):
                        raise InspectionEvidenceError("INSPECTION_OBJECT_SCOPE_MISMATCH", field)
                record = {**record, **held}
            except (ValueError, TypeError, OSError) as exc:
                source_error = InspectionEvidenceError(str(getattr(exc, "code", "") or "EXECUTION_DETAILS_UNAVAILABLE"), _safe_error_detail(exc))
        for key in ("plan_id", "plan_version", "step_id", "attempt_id"):
            if str(record.get(key) or "") != scope[key]:
                raise InspectionEvidenceError("INSPECTION_OBJECT_SCOPE_MISMATCH", key)
        # Only recorded execution facts, never a startup file or a reader path.
        from agent_core.context_lineage import redact_text
        facts = {key: record[key] for key in ("execution_id", "attempt_id", "action_id", "returncode", "exit_code", "execution_status", "code", "raw_stdout", "raw_stderr", "stdout", "stderr") if key in record}
        for key, value in list(facts.items()):
            if isinstance(value, str):
                facts[key] = redact_text(value)
        body = {"schema_version": 1, "scope": scope, "execution": facts,
                "claim_status": "script assertions are not independently verified"}
        objects = [("execution", scope["attempt_id"], body)]
    store = StateContractStore(data_path, scope["task_id"])
    entries: dict[str, dict[str, Any]] = {}
    for kind, source_id, raw_body in objects:
        body = deepcopy(dict(raw_body)) if isinstance(raw_body, Mapping) else {}
        reason, detail, object_ref = "", "", ""
        try:
            if is_execution and source_error is not None:
                raise source_error
            if not body:
                raise InspectionEvidenceError("HISTORICAL_BODY_UNAVAILABLE", kind)
            _assert_body_scope(kind, body, scope)
            wrapper = {
                "schema_version": 1, "object_kind": kind, "object_revision": 1,
                "source_object_id": source_id, "scope": scope,
                "producer": "typed_execute" if is_execution else "typed_supervisor", "source_outcome_ref": packet.source_outcome_ref,
                "body": body,
            }
            encoded = _canonical(wrapper).encode("utf-8")
            if len(encoded) > MAX_INSPECTION_OBJECT_BYTES - 1024:
                raise InspectionEvidenceError("INSPECTION_OBJECT_SIZE_LIMIT", kind)
            _assert_storage_path(store)
            object_ref = store.put_object(_object_type(kind), wrapper)
        except (InspectionEvidenceError, StateContractIntegrityError, OSError, ValueError, TypeError) as exc:
            reason = str(getattr(exc, "code", "") or "INSPECTION_OBJECT_PERSIST_FAILED")
            detail = _safe_error_detail(exc)
        digest = object_ref.removeprefix("object:") if object_ref else "sha256:" + hashlib.sha256(_canonical({"scope":scope,"kind":kind,"source_object_id":source_id}).encode()).hexdigest()
        ref = _handle(scope, kind, source_id, digest if object_ref else "")
        entries[ref] = {
            "ref_id": ref, "kind": "evidence", "task_id": scope["task_id"],
            "plan_id": scope["plan_id"], "step_id": scope["step_id"],
            "backend": INSPECTION_BACKEND, "digest": digest,
            "size_hint": len(_canonical(body).encode()) if body else 0,
            "scope": {
                **scope, "access": "current_step", "readable": bool(object_ref),
                "resolvable": bool(object_ref), "object_kind": kind,
                "source_object_id": source_id, "object_revision": 1,
                "allowed_tools": ["evidence.read"], "unavailable_reason": reason,
                "unavailable_detail": detail,
            },
            "locator": {"object_ref": object_ref, "object_type": _object_type(kind)} if object_ref else {},
        }
    return entries


def validate_inspection_descriptor(entry: Mapping[str, Any], expected_scope: Mapping[str, Any]) -> None:
    """Check authorization metadata before opening any object body."""
    if entry.get("backend") != INSPECTION_BACKEND or entry.get("kind") != "evidence":
        raise InspectionEvidenceError("INSPECTION_BACKEND_UNSUPPORTED")
    actual = dict(entry.get("scope") or {})
    for field in INSPECTION_SCOPE_FIELDS:
        top = entry.get(field, actual.get(field))
        if str(actual.get(field) or "") != str(expected_scope.get(field) or "") or str(top or "") != str(expected_scope.get(field) or ""):
            raise InspectionEvidenceError("INSPECTION_REF_SCOPE_MISMATCH", field)
    _object_type(str(actual.get("object_kind") or ""))
    if type(actual.get("object_revision")) is not int or actual["object_revision"] != 1:
        raise InspectionEvidenceError("INSPECTION_OBJECT_REVISION_MISMATCH")
    if actual.get("allowed_tools") != ["evidence.read"]:
        raise InspectionEvidenceError("INSPECTION_TOOL_NOT_ALLOWED")


class InspectionObjectReader:
    """Fixed store accessor.  No provider path, remote locator or raw task dump."""
    def __init__(self, data_path: str, *, scope: Mapping[str, Any]) -> None:
        self.scope = dict(scope)
        self.store = StateContractStore(data_path, self.scope["task_id"])

    def _assert_current_authority(self, entry: Mapping[str, Any], locator: Mapping[str, Any]) -> None:
        # PlanTracker publishes packet+index in this canonical file under its
        # CAS lock/atomic replace. Read it without migration or cached state:
        # load_plan_state can write on read and is inappropriate for a tool.
        path = self.store.root.parent / "plan_state.json"
        _assert_storage_path(self.store, path)
        try:
            if path.stat().st_size > MAX_INSPECTION_AUTHORITY_BYTES:
                raise InspectionEvidenceError("INSPECTION_AUTHORITY_SIZE_LIMIT")
            with path.open("rb") as stream:
                encoded = stream.read(MAX_INSPECTION_AUTHORITY_BYTES + 1)
            if len(encoded) > MAX_INSPECTION_AUTHORITY_BYTES:
                raise InspectionEvidenceError("INSPECTION_AUTHORITY_SIZE_LIMIT")
            durable = json.loads(encoded.decode("utf-8"))
        except InspectionEvidenceError:
            raise
        except OSError as exc:
            raise InspectionEvidenceError("INSPECTION_OBJECT_IO_ERROR", f"{type(exc).__name__}: {_safe_error_detail(exc)}") from exc
        except (ValueError, TypeError) as exc:
            raise InspectionEvidenceError("INSPECTION_AUTHORITY_INVALID") from exc
        if not isinstance(durable, Mapping):
            raise InspectionEvidenceError("INSPECTION_AUTHORITY_INVALID")
        packet = assert_current_inspection_scope(durable, self.scope)
        ref = str(entry["ref_id"])
        if ref not in set(packet.get("inspection_refs") or ()) | set(packet.get("evidence_refs") or ()):
            raise InspectionEvidenceError("INSPECTION_REF_NOT_DECLARED")
        saved = ((durable.get("task_state") or {}).get("inspection_ref_registry") or {}).get(ref)
        keys = ("ref_id", "kind", "task_id", "plan_id", "step_id", "backend", "digest", "size_hint", "scope")
        if (not isinstance(saved, Mapping)
                or any(saved.get(key) != entry.get(key) for key in keys)
                or dict(saved.get("locator") or {}) != dict(locator)):
            raise InspectionEvidenceError("INSPECTION_REF_INDEX_MISMATCH")

    def read(self, entry: Mapping[str, Any], locator: Mapping[str, Any], *, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        validate_inspection_descriptor(entry, self.scope)
        meta = dict(entry["scope"])
        if not meta.get("readable"):
            raise InspectionEvidenceError("INSPECTION_REF_NOT_RESOLVABLE", str(meta.get("unavailable_reason") or "BODY_UNAVAILABLE"))
        kind = str(meta["object_kind"])
        if set(locator) != {"object_ref", "object_type"} or locator.get("object_type") != _object_type(kind):
            raise InspectionEvidenceError("INSPECTION_BACKEND_LOCATOR_INVALID")
        object_ref = str(locator.get("object_ref") or "")
        try:
            digest = StateContractStore._parse_ref(object_ref, "object")
        except StateContractIntegrityError as exc:
            raise InspectionEvidenceError("INSPECTION_BACKEND_LOCATOR_INVALID") from exc
        if entry.get("digest") != "sha256:" + digest or entry.get("ref_id") != _handle(self.scope, kind, str(meta.get("source_object_id") or ""), "sha256:" + digest):
            raise InspectionEvidenceError("INSPECTION_OBJECT_IDENTITY_MISMATCH")
        self._assert_current_authority(entry, locator)
        path = self.store.object_root / (digest + ".json")
        # The generic store verifies the final file; also deny symlinked parent
        # directories here, before any content is read through the accessor.
        _assert_storage_path(self.store, path)
        try:
            if path.stat().st_size > MAX_INSPECTION_OBJECT_BYTES:
                raise InspectionEvidenceError("INSPECTION_OBJECT_SIZE_LIMIT")
            wrapper = self.store.read_object(object_ref, expected_type=_object_type(kind))
        except OSError as exc:
            raise InspectionEvidenceError("INSPECTION_OBJECT_IO_ERROR", f"{type(exc).__name__}: {_safe_error_detail(exc)}") from exc
        except StateContractIntegrityError as exc:
            cause = exc.__cause__
            if isinstance(cause, OSError):
                raise InspectionEvidenceError("INSPECTION_OBJECT_IO_ERROR", f"{type(cause).__name__}: {_safe_error_detail(cause)}") from exc
            raise InspectionEvidenceError("INSPECTION_OBJECT_INTEGRITY_ERROR", _safe_error_detail(exc)) from exc
        if (not isinstance(wrapper, Mapping) or type(wrapper.get("schema_version")) is not int
                or wrapper["schema_version"] != 1 or type(wrapper.get("object_revision")) is not int
                or wrapper["object_revision"] != 1):
            raise InspectionEvidenceError("INSPECTION_OBJECT_SCHEMA_INVALID")
        if wrapper.get("scope") != self.scope or wrapper.get("object_kind") != kind or wrapper.get("source_object_id") != meta.get("source_object_id"):
            raise InspectionEvidenceError("INSPECTION_OBJECT_IDENTITY_MISMATCH")
        body = wrapper.get("body")
        if not isinstance(body, Mapping):
            raise InspectionEvidenceError("INSPECTION_OBJECT_BODY_INVALID")
        _assert_body_scope(kind, body, self.scope)
        try:
            start = int(start_line)
            end = int(end_line) if end_line is not None else start + MAX_INSPECTION_READ_LINES - 1
            if start < 1 or end < start:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise InspectionEvidenceError("RECOVERY_TOOL_ARGUMENT_INVALID", "inspection line range") from exc
        lines = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2).splitlines()
        end = min(end, start + MAX_INSPECTION_READ_LINES - 1, len(lines))
        selected, chars, actual_end = [], 0, start - 1
        for ordinal in range(start - 1, end):
            line = lines[ordinal]
            remaining = MAX_INSPECTION_READ_CHARS - chars
            if len(line) + 1 > remaining:
                if not selected:
                    raise InspectionEvidenceError(
                        "INSPECTION_LINE_REQUIRES_NARROWING",
                        f"line={ordinal + 1}; max_chars={MAX_INSPECTION_READ_CHARS}",
                    )
                break
            selected.append(line);chars += len(line) + 1;actual_end = ordinal + 1
        truncated = actual_end < len(lines) or chars >= MAX_INSPECTION_READ_CHARS
        return {
            "tool": "evidence.read", "source_ref": entry["ref_id"],
            "source_object_id": meta["source_object_id"], "object_kind": kind,
            "object_revision": 1, "digest": entry["digest"], "scope": dict(self.scope),
            "content": "\n".join(selected), "format": "application/json",
            "start_line": start, "end_line": actual_end, "total_lines": len(lines),
            "truncated": truncated, "cursor": {"next_start_line": actual_end + 1} if actual_end < len(lines) else {},
        }

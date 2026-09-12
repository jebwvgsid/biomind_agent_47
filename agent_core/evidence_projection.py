"""Read-only projections of collected files, registered results and runtime facts.

These views neither discover files nor accept artifacts.  Exact links already
recorded by the parser/reconciler are the only source of logical identity.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


def _rows(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _identity(row: Mapping[str, Any]) -> str:
    return str(row.get("identity") or row.get("logical_artifact_id")
               or row.get("artifact_id") or "").strip()


def _path(row: Any) -> str:
    if isinstance(row, Mapping):
        return str(row.get("path") or row.get("observed_path") or "").strip()
    return str(row or "").strip()


def artifact_effect_evidence(
    accepted_paths: Sequence[Any], *, claims: Sequence[Any] = (),
    bindings: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Preserve exact identity/path links without interpreting a path as an ID.

    An explicit empty accepted set remains empty.  Unknown or conflicting
    identities remain visible as such and cannot supply canonical effect IDs.
    The function does not decide whether a collector's acceptance is valid.
    """
    links: dict[str, set[str]] = {}
    for raw in [*_rows(claims), *_rows(bindings), *_rows(accepted_paths)]:
        if isinstance(raw, Mapping) and _path(raw) and _identity(raw):
            links.setdefault(_path(raw), set()).add(_identity(raw))
    result = []
    seen = set()
    for raw in _rows(accepted_paths):
        path = _path(raw)
        if not path or path in seen:
            continue
        seen.add(path)
        identities = links.get(path, set())
        result.append({
            "identity": next(iter(identities)) if len(identities) == 1 else "",
            "path": path,
            "identity_status": ("recorded" if len(identities) == 1
                                else "ambiguous" if identities else "unknown"),
        })
    return result


def artifact_collection_facts(execution: Mapping[str, Any]) -> dict[str, Any]:
    """Select the recorded accepted set by field presence, never disk fallback."""
    reconciliation = execution.get("runtime_output_reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, Mapping) else {}
    source, accepted = "", None
    for key in ("validated_artifacts", "adopted_artifacts"):
        if key in execution:
            source, accepted = key, execution[key]
            break
    if not source and "bindings" in reconciliation:
        source, accepted = "runtime_output_reconciliation.bindings", reconciliation["bindings"]
    candidates = []
    for raw in _rows(execution.get("observed_output_records")):
        if isinstance(raw, Mapping):
            candidates.append({key: deepcopy(raw[key]) for key in (
                "path", "attempt_id", "current_attempt_owned", "exists",
                "artifact_kind", "format", "representation", "size", "size_bytes",
                "content_valid", "content_validation", "format_compatibility",
            ) if key in raw})
    envelope = execution.get("effect_evidence")
    envelope = envelope if isinstance(envelope, Mapping) else {}
    accepted_rows = artifact_effect_evidence(
        _rows(accepted), claims=_rows(envelope.get("produces_artifacts")),
        bindings=_rows(execution.get("reconciled_outputs")) + _rows(reconciliation.get("bindings")),
    )
    return {
        "source": source or "not_recorded",
        "accepted_set_status": ("known" if isinstance(accepted, (list, tuple))
                                else "unknown"),
        "accepted": accepted_rows,
        "observed_candidates": candidates,
        "reconciliation_status": str(reconciliation.get("status") or "unknown"),
        "reconciliation_violations": deepcopy(_rows(reconciliation.get("violations"))),
        "authority": "file_collection_only; formal_registration_and_completion_are_separate",
    }


def artifact_registration_facts(
    plan_state: Mapping[str, Any], *, logical_ids: Sequence[str],
    provenance_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Read the existing provenance heads through explicit recorded identity."""
    provenance = plan_state.get("provenance_state")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    heads = provenance.get("artifact_heads")
    heads = heads if isinstance(heads, Mapping) else {}
    artifacts = provenance.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    wanted = set(logical_ids)
    wanted_provenance = set(provenance_ids)
    result = []
    seen = set()
    for artifact_id, version_id in sorted(heads.items()):
        record = artifacts.get(version_id)
        record = record if isinstance(record, Mapping) else {}
        logical_id = str(record.get("logical_artifact_id") or "")
        if logical_id not in wanted and artifact_id not in wanted_provenance and artifact_id not in wanted:
            continue
        # artifact_id is a provenance identity. Never label it logical unless
        # the approved declaration itself explicitly uses this same identity.
        identity = logical_id or (str(artifact_id) if artifact_id in wanted else "")
        seen.add(identity)
        status = str(record.get("status") or "unknown")
        result.append({
            "identity": identity, "artifact_id": str(artifact_id),
            "artifact_version_id": str(version_id), "path": str(record.get("path") or ""),
            "status": status, "producer_attempt_id": str(record.get("producer_attempt_id") or ""),
            "validation_id": str(record.get("validation_id") or ""),
            "registration_state": ("not_registered" if status == "expected"
                                   else "unknown" if not record else "recorded"),
        })
    for identity in sorted(wanted - seen):
        result.append({"identity": identity, "status": "unknown",
                       "registration_state": "no_linked_provenance_head"})
    return result


def runtime_observation_facts(
    task_state: Mapping[str, Any], *, task_id: str, skill_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Expose scoped recorded UNKNOWN/negative facts, not positive-only audits.

    No current availability is inferred here. Time validity and authorization
    remain the responsibility of the existing runtime evidence consumer.
    """
    from agent_core.runtime_evidence import RuntimeEvidenceError, validate_runtime_evidence

    envelope = task_state.get("runtime_evidence")
    if not isinstance(envelope, Mapping) or not isinstance(envelope.get("records"), list):
        return {"status": "unknown", "reason_code": "runtime_evidence_not_recorded", "records": []}
    result, errors = [], []
    for raw in envelope["records"]:
        try:
            record = validate_runtime_evidence(raw)
        except (RuntimeEvidenceError, TypeError, ValueError) as exc:
            errors.append(str(getattr(exc, "code", "RUNTIME_EVIDENCE_INVALID")))
            continue
        if (record.get("acceptance") or {}).get("task_id") != task_id:
            continue
        if skill_ids and record["scope"].get("skill_id") not in skill_ids:
            continue
        result.append({key: deepcopy(record[key]) for key in (
            "evidence_id", "source_ref", "scope", "observed_at", "expires_at",
            "inspection", "facts", "supersedes",
        ) if key in record})
    return {
        "status": "recorded" if result else "unknown", "records": result,
        "invalid_record_codes": list(dict.fromkeys(errors)),
        "authority": "scoped_recorded_observations; no_global_or_current_availability_inference",
    }

"""Read-only projections of collected files, registered results and runtime facts.

These views neither discover files nor accept artifacts.  Exact links already
recorded by the parser/reconciler are the only source of logical identity.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


def candidate_binding_facts(plan_state: Mapping[str, Any], validation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project immutable candidate values; acceptance has a separate owner."""
    from agent_core.plan_bindings import accepted_plan_binding, PlanBindingError
    provenance = plan_state.get("provenance_state") or {}
    attempt_id = str(validation.get("attempt_id") or "")
    action_id = str(validation.get("action_id") or "")
    attempt = (provenance.get("attempts") or {}).get(attempt_id) or {}
    action = (provenance.get("actions") or {}).get(action_id) or {}
    if (not attempt or not action or attempt.get("action_id") != action_id
            or action.get("revoked_at") or any(
                str(record.get(key) or "") != str(expected or "")
                for record in (attempt, action)
                for key, expected in (("plan_id", plan_state.get("plan_id")),
                                      ("plan_version", plan_state.get("plan_version")),
                                      ("step_id", validation.get("step_id"))))):
        return []
    raw = (action.get("normalized_action") or {}).get("produced_bindings") or {}
    if not isinstance(raw, Mapping):
        return []
    rows = []
    for name, item in raw.items():
        if not isinstance(item, Mapping):
            continue
        try:
            accepted = accepted_plan_binding(plan_state, str(name))
        except (PlanBindingError, ValueError, TypeError):
            accepted = None
        rows.append({"identity": str(name), "value_present": "value" in item,
            **({"value": deepcopy(item["value"])} if "value" in item else {}),
            "value_type": str(item.get("type") or "unknown"),
            "source": "immutable_action.normalized_action.produced_bindings",
            "action_id": action_id, "attempt_id": attempt_id,
            "acceptance_status": "accepted" if accepted and accepted.producer_execution_attempt_id == attempt_id else "not_accepted",
            "accepted_binding_ref": accepted.binding_id if accepted else ""})
    return rows


def dependency_input_scope(plan_state: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
    """Snapshot only declared input identities and their durable versions."""
    ids = list(step.get("input_artifact_ids") or ())
    ids.extend(use.get("resource_ref") for use in step.get("resource_uses", ())
               if isinstance(use, Mapping) and use.get("access") == "read")
    provenance = plan_state.get("provenance_state") or {}
    bindings = plan_state.get("plan_resource_bindings") or {}
    heads = provenance.get("artifact_heads") or {}
    artifacts = provenance.get("artifacts") or {}
    return {str(key): {"binding": deepcopy(bindings.get(key)),
                      "head": deepcopy(heads.get(key)),
                      "artifact": deepcopy(artifacts.get(heads.get(key)) or artifacts.get(key))}
            for key in dict.fromkeys(ids) if key}


def committed_dependency_facts(plan_state: Mapping[str, Any], step: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Only declared, current dependencies. Missing archived facts stay unknown.

    New Commit receipts preserve the exact bounded findings used at acceptance.
    Old receipts can expose their linked validated observation, but never
    manufacture an independent parser receipt that was not saved there.
    """
    steps = {row.get("step_id"): row for row in plan_state.get("steps", ()) if isinstance(row, Mapping)}
    task = plan_state.get("task_state") or {}
    provenance = plan_state.get("provenance_state") or {}
    receipts = task.get("typed_step_commit_receipts") or {}
    result = []
    for name in dict.fromkeys(step.get("depends_on") or step.get("dependencies") or ()):
        dependency = steps.get(name) or {}
        row: dict[str, Any] = {"step_id": name, "status": "missing"}
        matches = [(ref, receipt) for ref, receipt in receipts.items()
            if ((receipt.get("outcome") or {}).get("payload") or {}).get("completed_step_id") == name]
        if not dependency or not matches:
            result.append(row)
            continue
        ref, receipt = matches[-1]
        outcome = receipt.get("outcome") or {}
        projection = receipt.get("successor_projection") or {}
        evidence = receipt.get("dependency_evidence") or {}
        if (dependency.get("status") != "passed"
                or name in (plan_state.get("stale_step_ids") or [])
                or name in (plan_state.get("revalidation_required_step_ids") or [])
                or (outcome.get("scope") or {}).get("task_id") != plan_state.get("task_id")
                or any(projection.get(key) != plan_state.get(key) for key in ("plan_id", "plan_version"))):
            row["status"] = "stale_or_not_applicable"
            result.append(row)
            continue
        if evidence:
            from agent_core.step_effects import canonical_step_effect
            if (evidence.get("step_effect") != canonical_step_effect(dependency)
                    or evidence.get("input_artifact_ids") != list(dependency.get("input_artifact_ids") or ())
                    or evidence.get("plan_revision") != plan_state.get("plan_revision")
                    or evidence.get("input_scope") != dependency_input_scope(plan_state, dependency)):
                row["status"] = "stale_or_not_applicable"
                result.append(row)
                continue
        refs = outcome.get("evidence_refs") or []
        linked = [(key, (provenance.get("validations") or {}).get(key)) for key in refs]
        linked = [(key, value) for key, value in linked if isinstance(value, Mapping) and value.get("step_id") == name]
        if not linked:
            row.update(status="source_unavailable", commit_ref=ref)
            result.append(row)
            continue
        validation_id, validation = linked[0]
        attempt = (provenance.get("attempts") or {}).get(validation.get("attempt_id")) or {}
        if (validation.get("status") not in {"VALID", "VALID_WITH_WARNINGS"}
                or any(attempt.get(key) != plan_state.get(key) for key in ("plan_id", "plan_version"))
                or attempt.get("step_id") != name
                or dependency.get("verification_attempt_id") not in (None, "", validation.get("attempt_id"))):
            row["status"] = "stale_or_not_applicable"
            result.append(row)
            continue
        from agent_core.step_effects import canonical_execution_effect_evidence
        effects = canonical_execution_effect_evidence(validation.get("effect_results") or {})
        row.update(status="committed", commit_ref=ref, validation_ref=validation_id,
            attempt_id=validation.get("attempt_id"), completion_report_ref=(outcome.get("payload") or {}).get("completion_report_ref"),
            observations=deepcopy(effects.get("produces_observations") or []),
            predicate_findings=deepcopy(evidence.get("predicate_findings") or []),
            reviewed_observation_warnings=deepcopy(evidence.get("reviewed_observation_warnings") or {}),
            parser_evidence_status="recorded" if evidence.get("predicate_findings") else "not_saved_in_legacy_receipt",
            applicability="Only the declared observation/predicate and its recorded input scope; no additional scientific conclusion.")
        result.append(row)
    return result


def revalidation_inspection_facts(plan_state: Mapping[str, Any], validation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Bounded read receipts for this exact attempt; never execution authority."""
    import hashlib
    import json
    result, remaining = [], 24000
    receipts = (plan_state.get("task_state") or {}).get("typed_recovery_receipts") or {}
    for decision_id, receipt in receipts.items():
        outcome = receipt.get("outcome") or {}
        scope = outcome.get("scope") or {}
        if outcome.get("type") != "revalidation_required" or any(
                str(scope.get(key) or "") != str(expected or "") for key, expected in (
                    ("task_id", plan_state.get("task_id")), ("plan_id", plan_state.get("plan_id")),
                    ("plan_version", plan_state.get("plan_version")),
                    ("step_id", validation.get("step_id")), ("action_id", validation.get("action_id")),
                    ("attempt_id", validation.get("attempt_id")))):
            continue
        for observation in receipt.get("causal_observation_receipts") or ():
            encoded = json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            row = {"decision_ref": decision_id, "receipt_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                   "archive_ref": receipt.get("recovery_archive_ref"), "authority": "diagnostic_read_only"}
            if len(encoded) <= remaining:
                row["observation"] = deepcopy(observation)
                remaining -= len(encoded)
            else:
                row["status"] = "body_not_projected_budget_requires_targeted_read"
            result.append(row)
            if len(result) >= 16:
                return result
    return result


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
            "acceptance_attempt_id": str(record.get("acceptance_attempt_id") or record.get("producer_attempt_id") or ""),
            "realization": str(record.get("realization") or ""),
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

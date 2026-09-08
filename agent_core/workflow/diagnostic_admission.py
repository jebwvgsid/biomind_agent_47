"""Scoped diagnostic admission over selected Skill operations and existing execution.

This module binds an approved reader operation; it never executes a program or
routes a graph. Native readers run through Generate, code approval and Execute.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from agent_core.approved_resource_locator import open_approved_resource
from agent_core.skill_access import SkillAccessService
from agent_core.workflow.stage_contracts import StageContractError


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def admit_diagnostic_request(*, seed, durable, directive, data_path: str) -> dict[str, Any]:
    """Resolve declared handles; the model supplies no program/path/limits."""
    import yaml
    from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    selected = {row.skill_id: row for row in seed.selected_skill_bundle.skills}
    skill_id, sep, operation_id = str(directive.diagnostic_operation).partition("#")
    if not sep or skill_id not in selected:
        raise StageContractError("DIAGNOSTIC_OPERATION_NOT_AUTHORIZED", directive.diagnostic_operation)
    access = (durable.get("task_state") or {}).get("skill_access_state") or durable.get("skill_access_state") or {}
    ref = selected[skill_id]
    snapshot = SkillAccessService._verified_snapshot(access, ref.snapshot_id)
    if snapshot.get("content_hash") != ref.content_hash or snapshot.get("skill_id") != skill_id:
        raise StageContractError("DIAGNOSTIC_SKILL_REVISION_MISMATCH", skill_id)
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", snapshot["exact_text"], re.S)
    metadata = yaml.safe_load(match.group(1)) if match else {}
    operations = (metadata.get("generate") or (metadata.get("metadata") or {}).get("generate") or {}).get("diagnostic_operations") or []
    found = [row for row in operations if isinstance(row, Mapping) and row.get("operation_id") == operation_id]
    if len(found) != 1:
        raise StageContractError("DIAGNOSTIC_OPERATION_UNSUPPORTED", operation_id)
    operation = dict(found[0])
    if operation.get("read_only") is not True or set(operation) - {"operation_id", "read_only", "argv", "limits", "description"}:
        raise StageContractError("DIAGNOSTIC_OPERATION_CONTRACT_INVALID", operation_id)
    template = operation.get("argv")
    if (not isinstance(template, list) or not template or len(template) > 32
            or any(not isinstance(v, str) or not v or len(v) > 1000 for v in template)):
        raise StageContractError("DIAGNOSTIC_OPERATION_CONTRACT_INVALID", "argv")
    defaults = {"max_wall_seconds": 30, "max_threads": 1, "max_input_bytes": 64 * 1024 * 1024,
                "max_output_bytes": 64 * 1024, "max_files": 1}
    limits = dict(operation.get("limits") or {})
    if set(limits) - set(defaults):
        raise StageContractError("DIAGNOSTIC_OPERATION_CONTRACT_INVALID", "limits")
    for key, maximum in defaults.items():
        value = limits.get(key, maximum)
        if type(value) is not int or not 0 < value <= maximum:
            raise StageContractError("DIAGNOSTIC_OPERATION_LIMIT_INVALID", key)
        limits[key] = value
    if not directive.diagnostic_inputs or len(directive.diagnostic_inputs) > limits["max_files"]:
        raise StageContractError("DIAGNOSTIC_INPUT_SCOPE_INVALID", "input count")
    store = PhaseArchiveStore(data_path)
    registry = build_recovery_inspection_registry(seed, durable, phase_store=store)
    dispatcher = RecoveryLocalToolDispatcher(seed=seed, durable=durable, registry=registry, phase_store=store)
    inputs = []
    for ref_id in directive.diagnostic_inputs:
        entry, locator, path = dispatcher._artifact_locator({"ref_id": ref_id})
        # Authorization and filesystem permission are checked before any code
        # can be generated. This neither decodes nor asserts scientific validity.
        try:
            with open_approved_resource(path) as stream:
                import os
                stat = os.fstat(stream.fileno())
        except PermissionError as exc:
            raise StageContractError("ARTIFACT_PERMISSION_DENIED", "diagnostic input access denied; an alternate representation does not grant permission") from exc
        except FileNotFoundError as exc:
            raise StageContractError("ARTIFACT_NOT_FOUND", "approved diagnostic input was not found during admission") from exc
        except OSError as exc:
            raise StageContractError("DIAGNOSTIC_INPUT_IO_ERROR", type(exc).__name__) from exc
        inputs.append({"ref_id": ref_id, "path": str(path), "locator": dict(locator), "entry": entry.to_dict(),
                       "identity": {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}})
    if sum(row["identity"]["size"] for row in inputs) > limits["max_input_bytes"]:
        raise StageContractError("DIAGNOSTIC_INPUT_BUDGET_EXCEEDED", "approved objects exceed this operation's input size bound; existence is not in doubt")
    replacements = {"{input:" + str(i) + "}": row["path"] for i, row in enumerate(inputs)}
    if set(replacements) != {arg for arg in template if re.fullmatch(r"\{input:\d+\}", arg)}:
        raise StageContractError("DIAGNOSTIC_OPERATION_CONTRACT_INVALID", "all selected inputs must be used exactly through placeholders")
    argv = [replacements.get(arg, arg) for arg in template]
    if any("{" in arg or "}" in arg for arg in argv):
        raise StageContractError("DIAGNOSTIC_OPERATION_CONTRACT_INVALID", "unresolved placeholder")
    request = {"schema_version": 1, "operation": directive.diagnostic_operation,
               "skill_ref": ref.to_dict(), "operation_contract": operation, "argv": argv,
               "approved_code": shlex.join(argv), "inputs": inputs, "limits": limits,
               "coverage": "declared operation over approved objects; whole-file science is not asserted",
               "scope": {k: durable.get(k) for k in ("task_id", "plan_id", "plan_version", "plan_revision", "current_step_id")}}
    request["request_digest"] = digest(request)
    return request


def diagnostic_authority_family(action: Mapping[str, Any], durable: Mapping[str, Any]) -> str:
    """Select explicit durable authority; a missing typed receipt is not legacy."""
    from agent_core.recovery import RecoveryExecutionEnvelope, RecoveryDecision
    from agent_core.plan_tracker import recovery_decision_scope_mismatches
    envelope = RecoveryExecutionEnvelope.from_mapping(action.get("recovery_execution_envelope") or {})
    task = durable.get("task_state") or {}
    receipt = (task.get("typed_recovery_receipts") or {}).get(envelope.recovery_decision_id)
    if receipt is not None:
        if (receipt.get("directive") or {}).get("action") != "run_diagnostic_probe":
            raise StageContractError("DIAGNOSTIC_RECEIPT_REQUIRED", envelope.recovery_decision_id)
        return "typed"
    recovery = task.get("recovery_state") or {}
    authority = recovery.get("authoritative_commit") or {}
    if authority.get("decision") and authority.get("execution_envelope"):
        decision = RecoveryDecision.from_mapping(authority["decision"])
        saved = RecoveryExecutionEnvelope.from_mapping(authority["execution_envelope"])
        active = recovery.get("active_recovery") or {}
        if (decision.identity_finalized and decision.decision_id == envelope.recovery_decision_id
                and saved.to_dict() == envelope.to_dict()
                and active.get("recovery_id") == decision.decision_id
                and active.get("envelope_id") == envelope.immutable_hash
                and not recovery_decision_scope_mismatches(durable, decision)):
            return "legacy_committed"
    raise StageContractError("DIAGNOSTIC_RECEIPT_REQUIRED", envelope.recovery_decision_id)


def validate_diagnostic_receipt_action(action: Mapping[str, Any], durable: Mapping[str, Any], *, code: str | None = None) -> dict[str, Any]:
    """Recheck original request/receipt against the exact action, including HITL edits."""
    from agent_core.recovery import RecoveryExecutionEnvelope
    if diagnostic_authority_family(action, durable) == "legacy_committed":
        # Existing finalized envelope, diagnostic validators and approval own
        # this path. The typed operation contract is not retrofitted to it.
        return {"authority_family": "legacy_committed"}
    envelope = RecoveryExecutionEnvelope.from_mapping(action.get("recovery_execution_envelope") or {})
    task = durable.get("task_state") or {}
    receipt = (task.get("typed_recovery_receipts") or {}).get(envelope.recovery_decision_id) or {}
    if (receipt.get("directive") or {}).get("action") != "run_diagnostic_probe":
        raise StageContractError("DIAGNOSTIC_RECEIPT_REQUIRED", envelope.recovery_decision_id)
    if receipt.get("execution_envelope") != envelope.to_dict() or receipt.get("execution_envelope_digest") != envelope.immutable_hash:
        raise StageContractError("DIAGNOSTIC_ENVELOPE_AUTHORITY_MISMATCH", envelope.recovery_decision_id)
    request = deepcopy(receipt.get("diagnostic_request") or {})
    expected = request.pop("request_digest", "")
    if not expected or expected != digest(request):
        raise StageContractError("DIAGNOSTIC_REQUEST_INTEGRITY_ERROR", envelope.recovery_decision_id)
    for key, value in request["scope"].items():
        if durable.get(key) != value:
            raise StageContractError("DIAGNOSTIC_REQUEST_STALE", key)
    source = request["skill_ref"]
    snapshot = SkillAccessService._verified_snapshot(task.get("skill_access_state") or durable.get("skill_access_state") or {}, source["snapshot_id"])
    if snapshot["content_hash"] != source["content_hash"]:
        raise StageContractError("DIAGNOSTIC_SKILL_REVISION_MISMATCH", source["skill_id"])
    approved = request["approved_code"].strip()
    actual = str(action.get("code") or "") if code is None else code
    argv = list(action.get("argv") or [])
    allowed_argv = (request["argv"], ["bash", "-lc", approved], ["bash", "-c", approved])
    if argv not in allowed_argv or actual.strip() not in {approved, shlex.join(argv)}:
        raise StageContractError("DIAGNOSTIC_OPERATION_PAYLOAD_MISMATCH", "approved reader arguments cannot be replaced by arbitrary code")
    if any(not str(key).startswith("BIOMIND_") for key in (action.get("env") or {})):
        raise StageContractError("DIAGNOSTIC_ENV_NOT_AUTHORIZED", "diagnostics use the existing approved runtime environment")
    for item in request["inputs"]:
        with open_approved_resource(Path(item["path"])) as stream:
            import os
            stat = os.fstat(stream.fileno())
        actual_identity = {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if actual_identity != item["identity"]:
            raise StageContractError("DIAGNOSTIC_INPUT_CHANGED", item["ref_id"])
    request["request_digest"] = expected
    return request


def diagnostic_return_lineage(*, durable, execution, action) -> dict[str, Any]:
    """Only a matching real diagnostic attempt may continue its parent incident."""
    request = validate_diagnostic_receipt_action(action, durable)
    if request.get("authority_family") == "legacy_committed":
        return {}
    envelope = action["recovery_execution_envelope"]
    task = durable.get("task_state") or {}
    receipt = task["typed_recovery_receipts"][envelope["recovery_decision_id"]]
    attempt_id = str(execution.get("attempt_id") or "")
    attempt = ((durable.get("provenance_state") or {}).get("attempts") or {}).get(attempt_id) or {}
    if (not attempt or attempt.get("action_id") != envelope["action_id"] or execution.get("action_id") != envelope["action_id"]):
        raise StageContractError("DIAGNOSTIC_ATTEMPT_IDENTITY_MISMATCH", attempt_id)
    parent = str(receipt["failure_packet_ref"])
    packet = (task.get("causal_failure_packets") or {}).get(parent, {}).get("failure_packet") or {}
    if packet.get("incident_id") != receipt.get("incident_id") or (task.get("causal_failure_incident_heads") or {}).get(receipt.get("incident_id")) != parent:
        raise StageContractError("DIAGNOSTIC_PARENT_SCOPE_STALE", parent)
    return {"incident_id": receipt["incident_id"], "causal_parent_packet_ref": parent,
            "recovery_decision_id": envelope["recovery_decision_id"], "diagnostic_request_digest": request["request_digest"]}


def diagnostic_binding_subset(action, durable, bindings):
    """The probe consumes its admitted inputs, not the main step's outputs."""
    request = validate_diagnostic_receipt_action(action, durable)
    if request.get("authority_family") == "legacy_committed":
        return bindings
    paths = {row["path"] for row in request["inputs"]}
    subset = {key: value for key, value in bindings.items()
              if value.direction == "input" and value.canonical_path in paths}
    if paths != {value.canonical_path for value in subset.values()}:
        raise StageContractError("DIAGNOSTIC_ARTIFACT_BINDING_MISSING", "exact approved operation inputs")
    return subset


def public_diagnostic_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Expose semantic choices and exact approved command, never control metadata."""
    if not request:
        return {}
    return {"operation": request["operation"], "approved_code": request["approved_code"],
            "argv": list(request["argv"]), "limits": dict(request["limits"]),
            "inputs": [{"ref_id": row["ref_id"], "path": row["path"]} for row in request["inputs"]],
            "coverage": request["coverage"],
            "instruction": "Use this exact approved reader command. Produce diagnostic evidence only, then return to Recovery; do not complete the main step."}

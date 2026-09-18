"""Explicit Recovery result choices over existing execution receipts.

This module never discovers a semantic match or performs a file mutation.
An exact local copy remains an ordinary approved Generate/Execute action.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Mapping

from agent_core.managed_artifacts import (
    MANAGED_EXACT, MODEL_SELECTABLE, _observed_file_metadata, _format_compatibility,
    reconcile_runtime_outputs,
)
from agent_core.plan_contract import plan_uses_explicit_results
from agent_core.step_effects import emit_declared_effects
from agent_core.workflow.stage_contracts import StageContractError


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True,
        ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _absolute(value: Any) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute() or path.is_symlink():
        raise StageContractError("RESULT_PATH_NOT_AUTHORIZED", str(value))
    resolved = path.resolve(strict=False)
    if resolved != path:
        raise StageContractError("RESULT_PATH_NOT_CANONICAL", str(value))
    return resolved


def _content_accepted(record: Mapping[str, Any], contract: Mapping[str, Any]) -> bool:
    return record.get("content_valid") is True or (
        contract.get("validation_contract_id") == "file_exists"
        and str(contract.get("format") or "").lower() in {"", "file", "text", "data", "output"}
        and contract.get("artifact_kind") == "file"
        and record.get("artifact_kind") == "file"
        and record.get("exists") is True and record.get("size") == 0)


def _same_observed_file(record: Mapping[str, Any], contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    contract = contract or {}
    path = _absolute(record.get("path"))
    try:
        actual = _observed_file_metadata(path)
    except OSError as exc:
        raise StageContractError("RESULT_SELECTED_FILE_UNAVAILABLE", type(exc).__name__) from exc
    fields = ("artifact_kind", "size", "mtime_ns", "ctime_ns", "content_prefix_sha256")
    if not actual.get("exists") or any(actual.get(key) != record.get(key) for key in fields):
        raise StageContractError("RESULT_SELECTED_FILE_CHANGED", str(path))
    if not _content_accepted(record, contract) or not _content_accepted(actual, contract):
        raise StageContractError("RESULT_SELECTED_CONTENT_INVALID", str(path))
    if record.get("evidence_files"):
        if _evidence_files(path, record["evidence_files"]) != record["evidence_files"]:
            raise StageContractError("RESULT_SELECTED_FILE_CHANGED", str(path))
    return actual


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _evidence_files(root, names):
    """A bounded, explicit helper receipt inventory; never scan input trees."""
    names = sorted(names)
    if not names or len(names) > 257 or any(Path(n).name != n for n in names):
        raise StageContractError("RESULT_EVIDENCE_INVENTORY_INVALID", str(root))
    if not root.is_dir() or root.is_symlink() or sorted(p.name for p in root.iterdir()) != names:
        raise StageContractError("RESULT_EVIDENCE_INVENTORY_CHANGED", str(root))
    result, size = {}, 0
    for name in names:
        p = _absolute(root / name)
        if not p.is_file():
            raise StageContractError("RESULT_EVIDENCE_FILE_INVALID", str(p))
        size += p.stat().st_size
        if size > 32 * 1024 * 1024:
            raise StageContractError("RESULT_EVIDENCE_INVENTORY_TOO_LARGE", str(root))
        result[name] = {"size": p.stat().st_size, "sha256": _content_digest(p)}
    return result


def preflight_result_candidate(execution):
    """Expose an explicitly bound helper receipt for Recovery's selection.

    The helper's observation names exact raw files. This establishes an owned
    evidence candidate, not its correspondence with any formal Plan output.
    The existing result_selections admission still owns that choice.
    """
    binding = execution.get("preflight_evidence_binding") or {}
    if not binding or execution.get("returncode") != 0:
        return None
    from agent_core.action_runtime import validate_manifest_hash
    from agent_core.step_effects import extract_structured_effect_evidence, structured_effect_parse_receipt
    action = execution.get("action_contract") or {}
    policy = action.get("execution_policy_manifest") or {}
    validate_manifest_hash(policy)
    aid, attempt = execution.get("action_id"), execution.get("attempt_id")
    expected = {**(policy.get("evidence_binding") or {}), "action_id": aid,
        "attempt_id": attempt, "policy_manifest_hash": policy.get("manifest_hash"),
        "canonical_path": str(Path(str(binding.get("approved_root") or "")) / str(attempt) / "preflight")}
    if (binding != expected or policy.get("action_id") != aid
            or binding.get("purpose") != "internal_execution_evidence"
            or binding.get("environment_variable") != "BIOMIND_PREFLIGHT_EVIDENCE_DIR"):
        raise StageContractError("RESULT_PREFLIGHT_BINDING_INVALID", str(attempt))
    root = _absolute(binding["canonical_path"])
    raw = str(execution.get("raw_stdout", execution.get("stdout", "")))
    parser = structured_effect_parse_receipt(raw, execution.get("declared_step_effect") or action.get("expected_step_effect") or {})
    prior = (execution.get("effect_evidence") or {}).get("parser_receipt") or {}
    if parser.get("issues") or (prior and prior.get("raw_stdout_sha256") != parser.get("raw_stdout_sha256")):
        raise StageContractError("RESULT_ORIGINAL_EFFECT_EVIDENCE_CHANGED", str(attempt))
    observations = extract_structured_effect_evidence(raw).get("produces_observations") or []
    eligible = [o for o in observations if isinstance(o.get("value"), dict)
        and o["value"].get("all_checks_passed") is True and o["value"].get("full_file_validated") is False]
    if len(eligible) != 1:
        raise StageContractError("RESULT_PREFLIGHT_OBSERVATION_REQUIRED", str(attempt))
    observation = eligible[0]
    receipt_path = _absolute(root / "checks.json")
    if receipt_path.stat().st_size > 2 * 1024 * 1024:
        raise StageContractError("RESULT_PREFLIGHT_RECEIPT_TOO_LARGE", str(receipt_path))
    receipt = json.loads(receipt_path.read_text())
    rows, claimed = receipt.get("checks") or [], observation["value"].get("checks") or []
    if (not rows or len(rows) > 128 or len(rows) != len(claimed)
            or receipt.get("status") != "returned" or receipt.get("unstarted") != 0
            or any(receipt.get(k) != len(rows) for k in ("total", "returned", "passed"))
            or any(receipt.get(k) != observation["value"].get(k) for k in ("status", "total", "returned", "passed", "unstarted"))):
        raise StageContractError("RESULT_PREFLIGHT_RECEIPT_MISMATCH", str(receipt_path))
    names = {"checks.json"}
    for row, claim in zip(rows, claimed):
        if (row.get("returncode") != 0 or row.get("passed") is not True
                or any(row.get(k) != v for k, v in claim.items())):
            raise StageContractError("RESULT_PREFLIGHT_RECEIPT_MISMATCH", str(receipt_path))
        raw_refs = row.get("raw") or {}
        if set(raw_refs) != {"stdout", "stderr"}:
            raise StageContractError("RESULT_PREFLIGHT_RAW_REQUIRED", str(receipt_path))
        for p in raw_refs.values():
            path = _absolute(p)
            if path.parent != root:
                raise StageContractError("RESULT_PREFLIGHT_RAW_SCOPE_INVALID", str(path))
            names.add(path.name)
    files = _evidence_files(root, names)
    return {**_observed_file_metadata(root), "attempt_id": attempt, "current_attempt_owned": True,
        "evidence_binding": deepcopy(binding), "evidence_files": files,
        "observation_identity": observation["identity"], "evidence_ref": str(receipt_path),
        "scope": observation["value"].get("scope"), "full_file_validated": False}


def _check_source(durable, execution, *, decision_id="", approved_code=""):
    """Re-entry cannot turn a replaced attempt or changed input into success."""
    from agent_core.managed_artifacts import managed_input_signature
    if durable.get("approval_status") not in (None, "", "approved"):
        raise StageContractError("RESULT_PLAN_APPROVAL_NOT_CURRENT", str(durable.get("approval_status")))
    provenance = durable.get("provenance_state") or {}
    source = (provenance.get("attempts") or {}).get(execution.get("attempt_id")) or {}
    action_record = (provenance.get("actions") or {}).get(execution.get("action_id")) or {}
    action = action_record.get("normalized_action") or execution.get("action_contract") or action_record
    if action_record.get("revoked_at") or source.get("status") in {"SUPERSEDED", "REVOKED", "CANCELLED"}:
        raise StageContractError("RESULT_SOURCE_ATTEMPT_NOT_ACCEPTABLE", str(execution.get("attempt_id")))
    for other in (provenance.get("attempts") or {}).values():
        if (other.get("step_id") != source.get("step_id")
                or int(other.get("attempt_sequence") or 0) <= int(source.get("attempt_sequence") or 0)):
            continue
        local_record = (provenance.get("actions") or {}).get(other.get("action_id")) or {}
        local = local_record.get("normalized_action") or local_record
        if not (decision_id and local.get("recovery_decision_id") == decision_id
                and str(local.get("code") or "").strip() == approved_code.strip()):
            raise StageContractError("RESULT_SOURCE_ATTEMPT_SUPERSEDED", str(execution.get("attempt_id")))
    held = execution.get("managed_input_signature", source.get("managed_input_signature"))
    if execution.get("preflight_evidence_binding") and held is None:
        raise StageContractError("RESULT_SOURCE_INPUT_IDENTITY_REQUIRED", str(execution.get("attempt_id")))
    if held is not None:
        if (any(isinstance(v, dict) and v.get("complete") is False for v in held.values())
                or managed_input_signature(action) != held):
            raise StageContractError("RESULT_SOURCE_INPUT_CHANGED", str(execution.get("attempt_id")))


def _execution_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    outcome = state.get("execute_outcome") or {}
    execution = (outcome.get("payload") or {}).get("execution_record")
    if not isinstance(execution, Mapping):
        execution = state.get("execution_evidence")
    if not isinstance(execution, Mapping):
        raise StageContractError("RESULT_EXECUTION_RECORD_MISSING", "current execute_outcome.payload.execution_record")
    return deepcopy(dict(execution))


def _directory_copy_code(row):
    """Materialize the existing local publisher for an explicit receipt set."""
    payload = {k: row[k] for k in ("observed_path", "target_path", "decision_id", "artifact_id")}
    payload["files"] = row["observed_record"]["evidence_files"]
    program = '''import hashlib, json, os, shutil, subprocess
from pathlib import Path
spec = SPEC
source, target = Path(spec['observed_path']), Path(spec['target_path'])
stage = target.parent / ('.biomind-publish-' + hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:24])
def check(root):
    assert root.is_dir() and not root.is_symlink(), 'RESULT_PUBLICATION_DIRECTORY_INVALID'
    assert sorted(p.name for p in root.iterdir()) == sorted(spec['files']), 'RESULT_PUBLICATION_INVENTORY_CHANGED'
    for name, identity in spec['files'].items():
        p = root / name
        assert p.is_file() and not p.is_symlink(), 'RESULT_PUBLICATION_FILE_INVALID'
        assert p.stat().st_size == identity['size'], 'RESULT_PUBLICATION_CONTENT_CHANGED'
        assert hashlib.sha256(p.read_bytes()).hexdigest() == identity['sha256'], 'RESULT_PUBLICATION_CONTENT_CHANGED'
check(source)
if not stage.exists():
    assert not target.exists() and not target.is_symlink(), 'RESULT_TARGET_CONFLICT'
    stage.mkdir()
    with (stage / 'owner.json').open('x') as f:
        json.dump(spec, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
assert not stage.is_symlink() and not (stage / 'owner.json').is_symlink(), 'RESULT_PUBLICATION_OWNER_INVALID'
assert json.loads((stage / 'owner.json').read_text()) == spec, 'RESULT_PUBLICATION_OWNER_INVALID'
pending = stage / 'pending'
assert not pending.is_symlink(), 'RESULT_PUBLICATION_DIRECTORY_INVALID'
placement = stage / 'placement.json'
assert not placement.is_symlink(), 'RESULT_PUBLICATION_OWNER_INVALID'
def identity(path):
    st = path.stat()
    return {'device': st.st_dev, 'inode': st.st_ino}
if target.exists() or target.is_symlink():
    assert not pending.exists(), 'RESULT_TARGET_CONFLICT'
    assert placement.is_file() and json.loads(placement.read_text()) == identity(target), 'RESULT_TARGET_OWNER_UNKNOWN'
    check(target)
else:
    pending.mkdir(exist_ok=True)
    assert not pending.is_symlink(), 'RESULT_PUBLICATION_OWNER_INVALID'
    for name in spec['files']:
        p = pending / name
        assert not p.is_symlink(), 'RESULT_PUBLICATION_FILE_INVALID'
        shutil.copyfile(source / name, p)
    check(pending)
    if placement.exists():
        assert json.loads(placement.read_text()) == identity(pending), 'RESULT_PUBLICATION_OWNER_INVALID'
    else:
        with placement.open('x') as f:
            json.dump(identity(pending), f); f.flush(); os.fsync(f.fileno())
    subprocess.run(['/usr/bin/mv', '-T', '--no-clobber', '--', str(pending), str(target)], check=True)
    assert not pending.exists(), 'RESULT_TARGET_CONFLICT'
    check(target)
assert not (stage / 'published.json').is_symlink(), 'RESULT_PUBLICATION_OWNER_INVALID'
with (stage / 'published.json').open('w') as f:
    json.dump({'decision_id': spec['decision_id'], 'target_path': str(target)}, f); f.flush(); os.fsync(f.fileno())
'''.replace('SPEC', repr(payload), 1)
    return shlex.quote(sys.executable) + " -I -S - <<'BIOMIND_RESULT_PUBLICATION'\n" + program + "BIOMIND_RESULT_PUBLICATION"


def _copy_code(rows: list[dict[str, Any]]) -> str:
    lines = ["set -euo pipefail"]
    for row in rows:
        if row["observed_record"].get("artifact_kind") == "directory":
            lines.append(_directory_copy_code(row))
            continue
        source, target = shlex.quote(row["observed_path"]), shlex.quote(row["target_path"])
        # Existing identical targets are checked without overwriting; this is
        # also the response-loss/re-entry path. A conflicting target fails.
        lines.extend([
            f"if [ -e {target} ] || [ -L {target} ]; then",
            f"  test ! -L {target}",
            f"  cmp -s -- {source} {target}",
            "else",
            f"  cp --no-clobber -- {source} {target}",
            f"  cmp -s -- {source} {target}",
            "fi",
        ])
    effect = {"produces_artifacts": [row["artifact_id"] for row in rows]}
    marker = emit_declared_effects(effect, {"produces_artifacts": {
        row["artifact_id"]: {"path": row["target_path"]} for row in rows}})
    lines.append("printf '%s\\n' " + shlex.quote(marker))
    return "\n".join(lines) + "\n"


def admit_result_resolution(*, seed, durable, directive, state, data_path="") -> dict[str, Any]:
    """Check a model's exact choices against the current approved execution.

    All identities, roots and machine fingerprints derive from existing
    records. No source code is accepted from Recovery and no file is copied.
    """
    if not directive.result_selections:
        return {}
    if not plan_uses_explicit_results(durable):
        raise StageContractError("RESULT_SELECTION_REQUIRES_EXPLICIT_PLAN", "result_contract_version=1")
    if not directive.evidence_refs:
        raise StageContractError("RESULT_SELECTION_EVIDENCE_REQUIRED", directive.directive_id)
    execution = _execution_from_state(state)
    if execution.get("recovery_execution_facts_ref") and data_path:
        from agent_core.state_contract_store import StateContractStore
        from agent_core.workflow.inspection_evidence import read_execution_facts
        facts = read_execution_facts(StateContractStore(data_path, durable["task_id"]), execution["recovery_execution_facts_ref"])
        for key in ("plan_id", "plan_version", "step_id", "action_id", "attempt_id"):
            if str(facts.get(key) or "") != str(execution.get(key) or ""):
                raise StageContractError("RESULT_ORIGINAL_EXECUTION_SCOPE_CHANGED", key)
        for key in ("raw_stdout", "raw_stderr", "stdout", "stderr", "code"):
            if key in facts:
                execution[key] = deepcopy(facts[key])
    packet = seed.failure_packet
    attempt_id = str(packet.attempt_ref or "")
    attempt = ((durable.get("provenance_state") or {}).get("attempts") or {}).get(attempt_id) or {}
    if (not attempt_id or not attempt or execution.get("attempt_id") != attempt_id
            or execution.get("action_id") != packet.action_ref
            or attempt.get("action_id") != packet.action_ref
            or execution.get("step_id") != packet.step_id
            or execution.get("plan_id") != durable.get("plan_id")
            or str(execution.get("plan_version") or "") != str(durable.get("plan_version") or "")
            or execution.get("returncode") != 0):
        raise StageContractError("RESULT_SELECTION_SOURCE_SCOPE_INVALID", attempt_id)
    _check_source(durable, execution)
    base_validation = execution.get("validation_before_output_reconciliation")
    if isinstance(base_validation, Mapping) and base_validation.get("passed") is False:
        raise StageContractError("RESULT_BASE_EXECUTION_INVALID", "The original non-location checks failed; a file selection cannot waive them.")
    if (isinstance(execution.get("validation"), Mapping) and execution["validation"].get("passed") is False
            and not isinstance(base_validation, Mapping)):
        raise StageContractError("RESULT_BASE_VALIDATION_UNKNOWN", "The pre-reconciliation validation record is required before a failed check can be superseded.")
    action = (durable.get("provenance_state") or {}).get("actions", {}).get(packet.action_ref) or {}
    action = action.get("normalized_action") or action
    contracts = [deepcopy(dict(row)) for row in action.get("controller_output_bindings", [])
                 if isinstance(row, Mapping)]
    if not contracts:
        # The action contract is part of the immutable execution evidence;
        # identity above must still match the durable action/attempt pair.
        contracts = [deepcopy(dict(row)) for row in (execution.get("action_contract") or {}).get("controller_output_bindings", [])
                     if isinstance(row, Mapping)]
    candidates = execution.get("observed_output_records")
    if not isinstance(candidates, list):
        raise StageContractError("RESULT_OBSERVATIONS_UNKNOWN", attempt_id)
    preflight = preflight_result_candidate(execution)
    if preflight:
        candidates = [r for r in candidates if r.get("path") != preflight["path"]] + [preflight]
        execution["observed_output_records"] = candidates
    inputs = {str(row.get("path") or row.get("canonical_path") or row.get("expected_path") or "")
              for row in action.get("concrete_artifact_bindings", [])
              if isinstance(row, Mapping) and row.get("direction") == "input"}
    rows = []
    paths = set()
    for selection in directive.result_selections:
        artifact_id = selection["artifact_id"]
        matches = [row for row in contracts if str(row.get("artifact_id") or "") == artifact_id]
        if len(matches) != 1:
            raise StageContractError("RESULT_LOGICAL_ID_NOT_DECLARED", artifact_id)
        contract = matches[0]
        if contract.get("result_contract_version") != 1:
            raise StageContractError("RESULT_SELECTION_CONTRACT_VERSION_MISSING", artifact_id)
        source = _absolute(selection["observed_path"])
        target = _absolute(contract.get("expected_path") or contract.get("path"))
        bound_evidence = bool(preflight and preflight["path"] == str(source))
        if ((not source.is_relative_to(target.parent) and not bound_evidence)
                or source == target.parent or str(source) in inputs or str(target) in inputs):
            raise StageContractError("RESULT_PATH_NOT_AUTHORIZED", str(source))
        if str(source) in paths:
            raise StageContractError("RESULT_SELECTION_DUPLICATE_PATH", str(source))
        paths.add(str(source))
        observed = [row for row in candidates if isinstance(row, Mapping)
                    and row.get("path") == str(source)
                    and row.get("attempt_id") == attempt_id
                    and row.get("current_attempt_owned") is True]
        if len(observed) != 1:
            raise StageContractError("RESULT_SELECTED_CANDIDATE_NOT_CURRENT", str(source))
        actual = _same_observed_file(observed[0], contract)
        is_evidence_directory = (bound_evidence and actual.get("artifact_kind") == "directory"
            and contract.get("artifact_kind") == "directory" and contract.get("validation_contract_id") == "directory_exists")
        if not is_evidence_directory and (actual.get("artifact_kind") != "file" or contract.get("artifact_kind") != "file"):
            raise StageContractError("RESULT_LOCAL_FILE_KIND_UNSUPPORTED", artifact_id)
        if _format_compatibility(str(contract.get("format") or ""), str(actual.get("format") or ""), actual["artifact_kind"]) == "incompatible":
            raise StageContractError("RESULT_SELECTED_FORMAT_INCOMPATIBLE", artifact_id)
        operation = selection["operation"]
        policy = str(contract.get("path_policy") or "")
        if operation == "accept_actual" and policy != MODEL_SELECTABLE:
            raise StageContractError("RESULT_EXACT_NAME_REQUIRED", artifact_id)
        if operation == "copy_to_exact" and policy not in {MANAGED_EXACT, MODEL_SELECTABLE}:
            raise StageContractError("RESULT_PATH_POLICY_UNKNOWN", artifact_id)
        if operation == "copy_to_exact" and (source == target or target.exists()):
            raise StageContractError("RESULT_TARGET_CONFLICT", str(target))
        if operation == "copy_to_exact" and not os.access(target.parent, os.W_OK | os.X_OK):
            raise StageContractError("RESULT_OUTPUT_ROOT_NOT_WRITABLE", str(target.parent))
        rows.append({**dict(selection), "target_path": str(target) if operation == "copy_to_exact" else str(source),
                     "source_attempt_id": attempt_id, "decision_id": directive.directive_id,
                     "contract": contract, "observed_record": deepcopy(observed[0])})
    request = {"schema_version": 1, "decision_id": directive.directive_id,
               "scope": {key: str(durable.get(key) or "") for key in ("task_id", "plan_id", "plan_version", "current_step_id", "task_revision", "plan_revision")},
               "approval_identity": _digest({k: durable.get(k) for k in
                   ("approved_plan_hash", "approval_status", "approval_record_ref", "approval_record")}),
               "source_attempt_id": attempt_id, "source_action_id": packet.action_ref,
               "source_execution": execution, "contracts": contracts, "selections": rows,
               "operation": "copy_to_exact" if directive.action == "organize_result" else "accept_actual"}
    if directive.action == "organize_result":
        request.update(language="bash", approved_code=_copy_code(rows))
    request["request_digest"] = _digest(request)
    return request


def get_result_resolution_request(durable: Mapping[str, Any], decision_id: str) -> dict[str, Any]:
    receipt = ((durable.get("task_state") or {}).get("typed_recovery_receipts") or {}).get(decision_id) or {}
    request = deepcopy(receipt.get("result_resolution_request") or {})
    if not request:
        return {}
    expected = request.pop("request_digest", "")
    if expected != _digest(request) or request.get("decision_id") != decision_id:
        raise StageContractError("RESULT_RESOLUTION_RECEIPT_INTEGRITY", decision_id)
    if any(str(durable.get(key) or "") != value for key, value in request["scope"].items()):
        raise StageContractError("RESULT_RESOLUTION_SCOPE_STALE", decision_id)
    if request.get("approval_identity") and request["approval_identity"] != _digest({k: durable.get(k) for k in
            ("approved_plan_hash", "approval_status", "approval_record_ref", "approval_record")}):
        raise StageContractError("RESULT_PLAN_APPROVAL_CHANGED", decision_id)
    _check_source(durable, request["source_execution"], decision_id=decision_id, approved_code=request.get("approved_code", ""))
    request["request_digest"] = expected
    return request


def validate_result_organization_action(action, durable, *, code=None) -> dict[str, Any]:
    decision_id = str(action.get("recovery_decision_id") or (action.get("recovery_execution_envelope") or {}).get("recovery_decision_id") or "")
    request = get_result_resolution_request(durable, decision_id)
    if not request or request["operation"] != "copy_to_exact":
        raise StageContractError("RESULT_ORGANIZATION_RECEIPT_REQUIRED", decision_id)
    approved = request["approved_code"].strip()
    actual = str(action.get("code") or "") if code is None else str(code)
    if actual.strip() != approved:
        raise StageContractError("RESULT_ORGANIZATION_CODE_CHANGED", "Only the admitted local copy may execute; main computation is not part of this action.")
    argv = action.get("argv") or []
    if argv and not (len(argv) == 3 and argv[:2] in (["bash", "-lc"], ["bash", "-c"])
                     and isinstance(argv[2], str) and argv[2].strip() == approved):
        raise StageContractError("RESULT_ORGANIZATION_ARGV_CHANGED", decision_id)
    for row in request["selections"]:
        _same_observed_file(row["observed_record"], row["contract"])
        target = _absolute(row["target_path"])
        if not target.parent.is_dir():
            raise StageContractError("RESULT_OUTPUT_ROOT_UNAVAILABLE", str(target.parent))
    return request


def result_resolution_reconciliation(request, execution=None) -> dict[str, Any]:
    """Produce rechecked facts; caller persists then uses normal verification.

    Existing raw execution/marker/parser evidence is kept in source_execution;
    callers must not replace that historical record with this projection.
    """
    candidates = deepcopy(request["source_execution"]["observed_output_records"])
    selections = request["selections"]
    local_attempt = ""
    if request["operation"] == "copy_to_exact":
        execution = dict(execution or {})
        local_attempt = str(execution.get("attempt_id") or "")
        if (not local_attempt or local_attempt == request["source_attempt_id"]
                or execution.get("returncode") != 0
                or str(execution.get("code") or "").strip() != request["approved_code"].strip()
                or execution.get("recovery_decision_id") != request["decision_id"]):
            raise StageContractError("RESULT_LOCAL_EXECUTION_NOT_CONFIRMED", request["decision_id"])
    for row in selections:
        _same_observed_file(row["observed_record"], row["contract"])
        if local_attempt:
            target = _observed_file_metadata(_absolute(row["target_path"]))
            directory = bool(row["observed_record"].get("evidence_files"))
            if directory:
                if _evidence_files(_absolute(row["target_path"]), row["observed_record"]["evidence_files"]) != row["observed_record"]["evidence_files"]:
                    raise StageContractError("RESULT_LOCAL_COPY_CONTENT_CHANGED", row["artifact_id"])
            elif (not _content_accepted(target, row["contract"]) or target.get("size") != row["observed_record"].get("size")
                    or target.get("content_prefix_sha256") != row["observed_record"].get("content_prefix_sha256")):
                raise StageContractError("RESULT_LOCAL_COPY_NOT_VERIFIED", row["artifact_id"])
            if not directory and _content_digest(_absolute(row["observed_path"])) != _content_digest(_absolute(row["target_path"])):
                raise StageContractError("RESULT_LOCAL_COPY_CONTENT_CHANGED", row["artifact_id"])
            candidates = [item for item in candidates if item.get("path") != target["path"]]
            candidates.append({**target, "attempt_id": request["source_attempt_id"], "current_attempt_owned": True,
                               "source_attempt_id": request["source_attempt_id"], "local_attempt_id": local_attempt})
    reconciled = reconcile_runtime_outputs(request["contracts"], candidates,
        attempt_id=request["source_attempt_id"], exit_code=0, admitted_selections=selections)
    if reconciled.get("status") != "accepted":
        raise StageContractError("RESULT_RESOLUTION_REVALIDATION_REQUIRED", json.dumps(reconciled["violations"], ensure_ascii=False))
    return {"schema_version": 1, "decision_id": request["decision_id"],
            "request_digest": request["request_digest"], "source_attempt_id": request["source_attempt_id"],
            "local_attempt_id": local_attempt, "reconciliation": reconciled,
            "source_execution_id": request["source_execution"].get("execution_id", ""),
            "registration_status": "rechecked_pending_registration"}


def register_result_resolution(durable, request, receipt, *, data_path="") -> dict[str, Any]:
    """Register through existing provenance, then return a fresh review view.

    The immutable source record is not rewritten. No status is set to valid
    or completed: Supervisor and CommitStep still own those transitions.
    """
    from agent_core.evidence_projection import artifact_effect_evidence
    from agent_core.plan_tracker import record_provenance_event
    from agent_core.step_effects import extract_structured_effect_evidence, structured_effect_parse_receipt
    saved = get_result_resolution_request(durable, request["decision_id"])
    if saved != request or receipt.get("request_digest") != request["request_digest"]:
        raise StageContractError("RESULT_RESOLUTION_RECEIPT_INTEGRITY", request["decision_id"])
    source = deepcopy(request["source_execution"])
    if source.get("recovery_execution_facts_ref"):
        if not data_path:
            raise StageContractError("RESULT_ORIGINAL_EXECUTION_FACTS_REQUIRED", request["decision_id"])
        from agent_core.state_contract_store import StateContractStore
        from agent_core.workflow.inspection_evidence import read_execution_facts
        held = read_execution_facts(StateContractStore(data_path, durable["task_id"]), source["recovery_execution_facts_ref"])
        for key in ("task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id"):
            expected = source.get(key, durable.get("task_id") if key == "task_id" else "")
            if str(held.get(key) or "") != str(expected or ""):
                raise StageContractError("RESULT_ORIGINAL_EXECUTION_SCOPE_CHANGED", key)
        for key in ("code", "stdout", "stderr", "raw_stdout", "raw_stderr", "stdout_truncated", "stderr_truncated"):
            if key in held:
                source[key] = deepcopy(held[key])
    actual = receipt["reconciliation"]
    paths = [row["observed_path"] for row in actual["bindings"]]
    base_validation = deepcopy(source.get("validation_before_output_reconciliation") or {})
    original_stdout = str(source["raw_stdout"] if "raw_stdout" in source else source.get("stdout") or "")
    parser = structured_effect_parse_receipt(original_stdout, source.get("declared_step_effect") or {})
    prior_parser = (source.get("effect_evidence") or {}).get("parser_receipt") or {}
    if prior_parser and prior_parser.get("raw_stdout_sha256") != parser.get("raw_stdout_sha256"):
        raise StageContractError("RESULT_ORIGINAL_EFFECT_EVIDENCE_CHANGED", request["decision_id"])
    if parser.get("issues"):
        raise StageContractError("RESULT_ORIGINAL_EFFECT_PROTOCOL_INVALID", request["decision_id"])
    claims = extract_structured_effect_evidence(original_stdout)
    # Re-evaluate the same original command facts with the admitted location.
    # The old output-reconciliation failure remains in the request's original
    # source_execution and is superseded only in this separate review view.
    source.update(reconciled_outputs=deepcopy(actual["bindings"]),
        runtime_output_reconciliation=deepcopy(actual), output_reconciliation_status=actual["status"],
        produced_artifacts=paths, adopted_artifacts=paths, validated_artifacts=paths,
        validation=base_validation,
        result_resolution_receipt=deepcopy(receipt))
    # Preserve the producer's actual process/execution/root-cause status.
    # Only its separately recorded location check is superseded above.
    effect = deepcopy(source.get("effect_evidence") or {})
    effect.update(produces_artifacts=artifact_effect_evidence(paths,
        claims=claims.get("produces_artifacts", []), bindings=actual["bindings"]),
        produces_observations=claims.get("produces_observations", []),
        explicit_side_effects=claims.get("explicit_side_effects", []),
        parser_receipt=deepcopy(prior_parser or parser), result_resolution_ref=request["decision_id"])
    source["effect_evidence"] = effect
    task = durable.setdefault("task_state", {})
    resolved = task.setdefault("result_resolution_receipts", {})
    prior = resolved.get(request["decision_id"])
    if prior:
        if prior != receipt:
            raise StageContractError("RESULT_RESOLUTION_IDENTITY_CONFLICT", request["decision_id"])
        # Commit/FinalAudit read a registered view, but cannot reuse stale
        # file facts if a selected file changed after Supervisor verification.
        for row in request["selections"]:
            _same_observed_file(row["observed_record"], row["contract"])
            if request["operation"] == "copy_to_exact":
                target = _observed_file_metadata(_absolute(row["target_path"]))
                directory = bool(row["observed_record"].get("evidence_files"))
                if (not _content_accepted(target, row["contract"])
                        or (directory and _evidence_files(_absolute(row["target_path"]), row["observed_record"]["evidence_files"]) != row["observed_record"]["evidence_files"])
                        or (not directory and _content_digest(_absolute(row["observed_path"])) != _content_digest(_absolute(row["target_path"])))):
                    raise StageContractError("RESULT_REGISTERED_COPY_CHANGED", row["artifact_id"])
        return source
    # This updates materialized heads via the already existing registration
    # rule. It does not rewrite the raw execution/approval/parser receipt.
    record_provenance_event(durable, "execution_finished", {
        "attempt_id": request["source_attempt_id"], "execution_record": source})
    for row in actual["bindings"]:
        provenance = durable.get("provenance_state") or {}
        for head in provenance.get("artifact_heads", {}).values():
            artifact = provenance.get("artifacts", {}).get(head) or {}
            if (artifact.get("logical_artifact_id") == row["artifact_id"]
                    and artifact.get("producer_attempt_id") == request["source_attempt_id"]):
                artifact["result_resolution_ref"] = request["decision_id"]
                artifact["local_organization_attempt_id"] = receipt["local_attempt_id"]
    durable.setdefault("task_state", {}).setdefault("result_resolution_receipts", {})[request["decision_id"]] = deepcopy(receipt)
    return source


def read_committed_result_resolution(durable, decision_id, commit, *, data_path):
    """Read a completed result through its exact durable CommitStep receipt.

    This is a terminal read path only. The admission getter still requires
    the current live step and cannot use a completed step to grant execution.
    """
    commit_value = commit.to_dict()
    commit_payload = commit_value.get("payload") or {}
    commit_id = str(commit_payload.get("commit_id") or "")
    held = (durable.get("task_state") or {}).get("typed_step_commit_receipts", {}).get(commit_id) or {}
    if held.get("outcome") != commit_value or commit.type != "plan_complete":
        raise StageContractError("RESULT_RESOLUTION_COMMIT_RECEIPT_REQUIRED", decision_id)
    completed_step_id = str(commit_payload.get("completed_step_id") or "")
    scoped = deepcopy(durable)
    scoped["current_step_id"] = completed_step_id
    request = get_result_resolution_request(scoped, decision_id)
    receipt = (durable.get("task_state") or {}).get("result_resolution_receipts", {}).get(decision_id)
    if not request or not receipt or request["scope"]["current_step_id"] != completed_step_id:
        raise StageContractError("RESULT_RESOLUTION_COMMITTED_SCOPE_INVALID", decision_id)
    consumption = (durable.get("task_state") or {}).get("semantic_completion_consumptions", {}).get(
        str(commit_payload.get("semantic_consumption_ref") or "")) or {}
    expected = {"task_id": request["scope"]["task_id"], "plan_id": request["scope"]["plan_id"],
        "plan_version": request["scope"]["plan_version"], "step_id": completed_step_id,
        "action_id": request["source_action_id"], "attempt_id": request["source_attempt_id"]}
    if (consumption.get("commit_id") != commit_id
            or consumption.get("report_id") != commit_payload.get("completion_report_ref")
            or consumption.get("decision_id") != commit_payload.get("semantic_decision_ref")
            or any(str((consumption.get("scope") or {}).get(key) or "") != str(value) for key, value in expected.items())):
        raise StageContractError("RESULT_RESOLUTION_COMMIT_CONSUMPTION_MISMATCH", decision_id)
    local_id = str(receipt.get("local_attempt_id") or "")
    if local_id:
        local = (durable.get("provenance_state") or {}).get("attempts", {}).get(local_id) or {}
        if (local.get("status") != "COMPLETED" or local.get("completion_commit_ref") != commit_id
                or local.get("computational_source_attempt_id") != request["source_attempt_id"]):
            raise StageContractError("RESULT_LOCAL_ORGANIZATION_NOT_COMMITTED", decision_id)
    view = register_result_resolution(scoped, request, receipt, data_path=data_path)
    return request, receipt, view

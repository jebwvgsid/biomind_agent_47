"""Scoped terminal facts and deterministic reports for Supervisor inspection.

This module owns no graph route, scientific execution or step-completion
authority. It consumes already held episode facts, stops only the matching
task lifecycle with the existing CAS writer, and projects the same receipt to
the outer terminal consumers. Runtime recovery has its separate owner.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import (
    canonical_task_spec_for_state, load_plan_state, plan_approval_is_current,
    save_plan_state, set_task_status,
)
from agent_core.task_spec import effective_output_contracts
from agent_core.approved_resource_locator import approved_resource_locator
from agent_core.workflow.outcomes import RecoveryOutcome
from agent_core.workflow.runtime_recovery_reporting import _path, _write_report, publish_report


PRODUCER = "supervisor_inspection_stop_v1"
_SCOPE = ("task_id", "plan_id", "plan_version", "step_id", "attempt_id", "episode_id")
_REPORT_NAMES = frozenset({"report.md", "reproduce.sh", "stage_status.tsv"})
_FINISHED = frozenset({"done", "passed", "completed"})
_SECRET = re.compile(r"(?i)(?:api[_-]?key|authorization\s*[:=]|bearer\s+|password|credential|secret|(?:^|/)\.env(?:$|[./]))")
_CODE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,300}$")
_TECHNICAL = frozenset({"INSPECTION_REF_NOT_RESOLVABLE", "INSPECTION_READ_FAILED",
    "INSPECTION_READER_FAILED", "RECOVERY_PHASE_ARCHIVE_FAILED", "RECOVERY_AGENT_PHASE_FAILED",
    "RECOVERY_EVENT_PERSISTENCE_FAILED", "RECOVERY_DIRECTIVE_APPLY_FAILED",
    "INSPECTION_OBJECT_PERSIST_FAILED", "INSPECTION_OBJECT_IO_ERROR", "HISTORICAL_BODY_UNAVAILABLE"})


def _mapping(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value) if isinstance(value, Mapping) else {}


def _safe(value, limit=1200):
    text = str(value or "").replace("\r", " ").replace("\x00", "")
    return "[redacted]" if _SECRET.search(text) else text[:limit]


def _code(value, default=""):
    text = _safe(value, 300)
    return text if _CODE.fullmatch(text) else default


def _budget_projection(value, depth=0):
    """Read bounded numeric receipts; never carry prompt bodies into CLI facts."""
    if not isinstance(value, Mapping) or depth > 3:
        return {}
    numeric = {"estimated_input_tokens", "safe_input_tokens", "effective_output_limit_tokens",
        "output_reserve_tokens", "application_context_limit_tokens", "estimation_margin_tokens",
        "safety_headroom_tokens", "compressor_safe_input_tokens", "provider_message_count",
        "tool_definition_count"}
    projected = {key: item for key, item in value.items()
                 if key in numeric and type(item) is int and item >= 0}
    projected.update({key: _safe(value[key], 180) for key in (
        "failure_stage", "estimator_name", "estimator_version", "exact_or_estimated") if value.get(key)})
    for key in ("capacity_feedback", "source_request_budget"):
        nested = _budget_projection(value.get(key), depth + 1)
        if nested:
            projected[key] = nested
    return projected


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _steps(durable):
    return [row for row in durable.get("steps") or [] if isinstance(row, Mapping)]


def _active_packet(durable):
    task = _mapping(durable.get("task_state"))
    ref = str(task.get("active_failure_packet_ref") or "")
    record = _mapping(_mapping(task.get("causal_failure_packets")).get(ref))
    return ref, record, _mapping(record.get("failure_packet"))


def _terminal_packet(durable, scope):
    """A legitimate blocked directive can consume the active packet first."""
    task = _mapping(durable.get("task_state"))
    if task.get("active_failure_packet_ref"):
        return _active_packet(durable)
    expected = str(scope.get("episode_id") or "").removeprefix("recovery-seed-")
    receipts = _mapping(task.get("typed_recovery_receipts"))
    matches = [(key, _mapping(row)) for key, row in receipts.items()
        if isinstance(row, Mapping) and str(row.get("source_fingerprint") or "") == expected]
    if not matches or any(_mapping(row.get("outcome")).get("type") != "blocked" for _key, row in matches):
        return "", {}, {}
    for decision_id, row in matches:
        ref = str(row.get("failure_packet_ref") or "")
        consumed = _mapping(_mapping(task.get("consumed_failure_packet_refs")).get(ref))
        if str(consumed.get("decision_id") or "") != str(decision_id):
            continue
        record = _mapping(_mapping(task.get("causal_failure_packets")).get(ref))
        return ref, record, _mapping(record.get("failure_packet"))
    return "", {}, {}


def _runtime_owned(durable):
    task = _mapping(durable.get("task_state"))
    runtime = _mapping(task.get("runtime_recovery"))
    episode = _mapping(_mapping(runtime.get("episodes")).get(runtime.get("active_episode_id")))
    request = _mapping(task.get("pending_runtime_evidence_request") or episode.get("request"))
    scope = _mapping(request.get("scope"))
    expected = {"task_id": durable.get("task_id"), "plan_id": durable.get("plan_id"),
        "plan_version": durable.get("plan_version"), "step_id": durable.get("current_step_id")}
    return bool(request and scope and all(str(scope.get(key) or "") == str(value or "") and str(value or "")
        for key, value in expected.items()) and episode.get("status") not in {"evidence_ready", "resolved", "superseded"})


def _scope_matches(durable, scope):
    if not isinstance(scope, Mapping) or any(not str(scope.get(key) or "") for key in _SCOPE):
        return False
    expected = {"task_id": durable.get("task_id"), "plan_id": durable.get("plan_id"),
        "plan_version": durable.get("plan_version"), "step_id": durable.get("current_step_id")}
    if any(str(scope[key]) != str(value or "") for key, value in expected.items()):
        return False
    task = _mapping(durable.get("task_state"))
    if str(task.get("status") or "") in {"completed", "cancelled"} or _runtime_owned(durable):
        return False
    current = next((row for row in _steps(durable) if row.get("step_id") == scope["step_id"]), {})
    if not current or str(current.get("status") or "") in _FINISHED:
        return False
    _ref, record, packet = _terminal_packet(durable, scope)
    if not record or str(record.get("source_kind") or "") != "verification_outcome":
        return False
    if str(packet.get("failure_type") or "") != "inspection_required":
        return False
    if str(scope["episode_id"]) != "recovery-seed-" + str(record.get("source_fingerprint") or ""):
        return False
    if str(packet.get("attempt_ref") or "") != str(scope["attempt_id"]):
        return False
    attempts = [current.get("verification_attempt_id"),
        _mapping(task.get("verification_pending")).get("attempt_id")]
    return all(not value or str(value) == str(scope["attempt_id"]) for value in attempts)


def _authority(durable):
    current_task = _mapping(durable.get("current_task"))
    return {"task_revision": int(current_task.get("task_revision") or durable.get("task_revision") or 0),
        "plan_revision": int(durable.get("plan_revision") or 0),
        "task_semantic_hash": str(current_task.get("task_semantic_hash") or ""),
        "approved_plan_hash": str(durable.get("approved_plan_hash") or "")}


def _stop_matches(durable, stop):
    return _scope_matches(durable, stop.get("scope")) and (
        not stop.get("authority") or stop["authority"] == _authority(durable))


def current_inspection_stop(durable, *, scope=None):
    """Return only this still-current, durably finalized inspection receipt."""
    stop = _mapping(_mapping(durable.get("task_state")).get("inspection_stop"))
    if stop.get("producer") != PRODUCER or stop.get("persistence_status") != "committed":
        return {}
    if scope is not None and any(str(stop.get("scope", {}).get(k) or "") != str(scope.get(k) or "") for k in _SCOPE):
        return {}
    return deepcopy(stop) if _stop_matches(durable, stop) else {}


def inspection_stop_scope(seed, durable=None):
    """Trusted seed identity for stop construction and node re-entry checks."""
    seed, durable = _mapping(seed), _mapping(durable)
    packet, plan = _mapping(seed.get("failure_packet")), _mapping(seed.get("plan_spec_ref"))
    return {"task_id": _code(packet.get("task_id") or durable.get("task_id")),
        "plan_id": _code(plan.get("plan_id") or durable.get("plan_id")),
        "plan_version": _code(plan.get("plan_version") or durable.get("plan_version")),
        "step_id": _code(packet.get("step_id")), "attempt_id": _code(packet.get("attempt_ref")),
        "episode_id": _code(seed.get("seed_id"))}


def _same_fact_scope(value, scope):
    """Explicit foreign identities never supply current terminal facts."""
    row = _mapping(value)
    return all(not row.get(key) or not scope.get(key) or str(row[key]) == str(scope[key])
               for key in ("task_id", "plan_id", "plan_version", "step_id", "attempt_id"))


def _failure_findings(payload, packet, scope):
    """Bounded public fields from existing producer records, never log guesses.

    This projection does not determine which check is scientifically correct or
    mark anything solved. Its source is the current frozen failure packet only.
    """
    payload = _mapping(payload)
    rows, seen = [], set()
    def add(raw, source_pointer, *, stage="validation"):
        row = _mapping(raw)
        if row.get("passed") is True or row.get("status") in {"passed", "satisfied", "resolved"}:
            return
        metadata = _mapping(row.get("typed_metadata") or row.get("metadata"))
        code = _code(row.get("code") or row.get("error_code") or row.get("reason_code")
                     or metadata.get("code"), "UNKNOWN")
        parser = _mapping(row.get("parser_receipt"))
        detail = _safe(row.get("detail") or row.get("message") or row.get("reason") or parser.get("detail")
                       or metadata.get("detail") or metadata.get("message") or metadata.get("reason"), 1200)
        field = _safe(row.get("field_path") or row.get("affected_field") or metadata.get("affected_field"), 400)
        identity = (code, detail, field)
        if identity in seen or (code == "UNKNOWN" and not detail):
            return
        seen.add(identity)
        rows.append({"code": code, "detail": detail, "field_path": field,
            "source": _safe(row.get("validation_source") or row.get("source") or row.get("parser_id")
                            or metadata.get("parser_id") or source_pointer, 400),
            "source_pointer": _safe(row.get("source_pointer") or source_pointer, 800),
            "stage": _code(row.get("repair_stage") or row.get("stage"), stage),
            "evidence_refs": [_safe(ref, 800) for ref in row.get("evidence_refs") or metadata.get("evidence_refs") or []][:8]})
    index = _mapping(payload.get("producer_failure_index"))
    if not index:
        index = next((_mapping(row.get("metadata")) for row in packet.get("diagnostic_facts") or []
                      if isinstance(row, Mapping) and row.get("kind") == "producer_failure_index"), {})
    if _same_fact_scope(index.get("scope"), scope):
        for row in index.get("checks") or []:
            add(row, "/producer_failure_index/checks")
    report = _mapping(payload.get("completion_report"))
    if _same_fact_scope(report.get("scope"), scope):
        for field in ("predicate_results", "failed_predicates", "findings", "binding_and_receipt_findings"):
            for number, row in enumerate(report.get(field) or []):
                if isinstance(row, Mapping):
                    add(row, f"/completion_report/{field}/{number}", stage="supervisor")
    for number, row in enumerate(payload.get("contract_violations") or []):
        if isinstance(row, Mapping):
            add(row, f"/contract_violations/{number}")
    # Metadata is already scoped by the immutable packet. Keep only explicit
    # producer failures, excluding narrative/old tool return codes/model claims.
    for row in packet.get("diagnostic_facts") or []:
        if not isinstance(row, Mapping) or row.get("kind") not in {
                "contract_violation", "completion_predicate", "binding_and_receipt", "missing_effect"}:
            continue
        metadata = _mapping(row.get("metadata"))
        add({**metadata, "message": metadata.get("detail") or metadata.get("message") or row.get("summary")},
            "/failure_packet/diagnostic_facts/" + str(row.get("fact_id") or ""))
    return rows[:12]


def _execution(durable, source, scope, packet=None):
    payload = _mapping(source.get("payload"))
    report = _mapping(payload.get("completion_report"))
    if not _same_fact_scope(report.get("scope"), scope):
        report = {}
    process = _mapping(report.get("process_outcome"))
    validation = _mapping(payload.get("validation_evidence"))
    record = _mapping(payload.get("execution_record"))
    if any(record.get(key) and scope.get(key) and str(record[key]) != str(scope[key])
           for key in ("task_id", "plan_id", "plan_version", "step_id", "attempt_id")):
        record = {}
    index = _mapping(payload.get("producer_failure_index"))
    packet = _mapping(packet)
    if not index and packet.get("attempt_ref") == scope["attempt_id"] and packet.get("step_id") == scope["step_id"]:
        index = next((_mapping(row.get("metadata")) for row in packet.get("diagnostic_facts") or []
                      if isinstance(row, Mapping) and row.get("kind") == "producer_failure_index"), {})
    if any(_mapping(index.get("scope")).get(key) and scope.get(key)
           and str(index["scope"][key]) != str(scope[key])
           for key in ("task_id", "plan_id", "plan_version", "step_id", "attempt_id")):
        index = {}
    attempt = _mapping(_mapping(_mapping(durable.get("provenance_state")).get("attempts")).get(scope["attempt_id"]))
    if attempt and str(attempt.get("step_id") or "") != scope["step_id"]:
        attempt = {}
    rc = process.get("returncode", record.get("returncode", record.get("exit_code",
        attempt.get("returncode", validation.get("returncode", _mapping(index.get("process")).get("return_code"))))))
    if isinstance(rc, bool) or not isinstance(rc, int):
        rc = None
    execution_status = str(process.get("execution_status") or attempt.get("status") or "unknown").lower()
    executed = bool(rc is not None or execution_status in {"succeeded", "completed", "validated", "validation_passed"})
    current = next((row for row in _steps(durable) if row.get("step_id") == scope["step_id"]), {})
    gate = _mapping(report.get("completion_gate"))
    deterministic = (gate.get("passed") is True and report.get("deterministic_veto") is not True)
    validation_status = process.get("validation_status") or validation.get("status")
    if _mapping(record.get("validation")).get("passed") is False or index.get("validation_acceptance") is False:
        validation_status, deterministic = "failed", False
    elif not validation_status and _mapping(record.get("validation")).get("passed") is True:
        validation_status = "passed"
    script = _safe(attempt.get("executed_script_path"), 1600)
    if script == "[redacted]" or Path(script).name in {"启动.sh", "startup.py"}:
        script = ""
    candidates = []
    observed_candidates = record.get("observed_output_records") or []
    if not observed_candidates:
        for fact in packet.get("diagnostic_facts") or []:
            if not isinstance(fact, Mapping) or fact.get("kind") != "result_realizations":
                continue
            realizations = _mapping(_mapping(fact.get("metadata")).get("result_realizations"))
            if _same_fact_scope(realizations.get("scope"), scope):
                observed_candidates = realizations.get("observed_candidates") or []
                break
    for row in observed_candidates:
        if (not isinstance(row, Mapping) or row.get("attempt_id") != scope["attempt_id"]
                or row.get("current_attempt_owned") is not True):
            continue
        candidates.append({key: _safe(row[key], 800) if isinstance(row[key], str) else row[key]
            for key in ("path", "exists", "artifact_kind", "size", "content_valid")
            if key in row and isinstance(row[key], (str, bool, int))})
    return {"attempt_id": scope["attempt_id"], "step_id": scope["step_id"], "executed": executed,
        "candidate_outputs": candidates[:12], "candidate_output_count": len(candidates),
        "purpose": _code(attempt.get("purpose") or attempt.get("execution_purpose"), "UNKNOWN"),
        "returncode": rc, "deterministic_passed": deterministic,
        "validation_status": _code(validation_status, "unknown"),
        "validation_checks": deepcopy(index.get("checks") or []),
        "reviewer_disposition": _code(report.get("effective_disposition"), "inspection_required"),
        "step_status": _code(current.get("status"), "verification_pending"),
        "committed": str(current.get("status") or "") in _FINISHED,
        "executed_script_ref": script, "command_body_available": False}


def build_inspection_stop(*, seed, durable, source, episode, archive_ref=""):
    """Build bounded immutable facts using the completed episode, never a reader."""
    seed = _mapping(seed)
    packet = _mapping(seed.get("failure_packet"))
    source = _mapping(source)
    payload = _mapping(source.get("payload"))
    scope = inspection_stop_scope(seed, durable)
    error = _mapping(episode.get("terminal_error"))
    audit = _mapping(episode.get("audit"))
    receipts = episode.get("_observation_receipts") or _mapping(episode.get("_resume_state")).get("observation_receipts") or []
    # A diagnostic result continues the same incident. Retain the original
    # causal failure and prior tool errors from its adopted durable receipt.
    origin_packet = packet
    original_source_payload = payload
    task = _mapping(durable.get("task_state"))
    parent_ref = str(payload.get("causal_parent_packet_ref") or "")
    seen = set()
    while parent_ref and parent_ref not in seen:
        seen.add(parent_ref)
        parent_record = _mapping(_mapping(task.get("causal_failure_packets")).get(parent_ref))
        parent = _mapping(parent_record.get("failure_packet"))
        if not parent or parent.get("incident_id") != packet.get("incident_id") or parent.get("step_id") != packet.get("step_id"):
            break
        origin_packet = parent
        original_source_payload = _mapping(_mapping(parent_record.get("failure_source")).get("payload"))
        parent_ref = str(parent.get("supersedes_packet_ref") or "")
    decision = str(payload.get("recovery_decision_id") or "")
    parent_receipt = _mapping(_mapping(task.get("typed_recovery_receipts")).get(decision))
    if parent_receipt.get("incident_id") == packet.get("incident_id") and payload.get("causal_parent_packet_ref") == parent_receipt.get("failure_packet_ref"):
        receipts = [*parent_receipt.get("causal_observation_receipts", []), *receipts]
    failures = []
    for index, raw in enumerate(receipts):
        receipt = _mapping(raw)
        issue = _mapping(receipt.get("error"))
        if not issue:
            continue
        failures.append({"tool_ordinal": index + 1, "tool_call_id": _code(receipt.get("request_id")),
            "tool": _code(receipt.get("tool")), "evidence_ref": _safe(receipt.get("source_ref"), 800),
            "error_code": _code(issue.get("code"), "INSPECTION_TOOL_FAILED"),
            "detail": _safe(issue.get("detail")), "stage": "recovery.inspection"})
    origin = {"source_stage": {"execute_outcome": "execute", "verification_outcome": "supervisor", "completion_gate_outcome": "supervisor", "commit_step_outcome": "commit_step", "install_outcome": "install_subagent", "install_subagent_outcome": "install_subagent"}.get(str(origin_packet.get("source") or ""), "recovery"), "source_owner": _code(origin_packet.get("failure_owner"), "semantic_review"),
        "failure_id": _code(origin_packet.get("failure_id")), "failure_type": _code(origin_packet.get("failure_type")),
        "error_code": _code(origin_packet.get("error_code"), "VERIFICATION_FAILED"),
        "summary": _safe(origin_packet.get("summary")),
        "concerns": [_safe(item, 1800) for item in original_source_payload.get("contract_violations") or []],
        "evidence_refs": [_safe(item, 800) for item in origin_packet.get("evidence_refs") or []]}
    origin_codes = [row.get("metadata", {}).get("return_code")
        for row in origin_packet.get("diagnostic_facts") or []
        if isinstance(row, Mapping) and row.get("kind") == "return_code"]
    origin["returncode"] = next((value for value in origin_codes if type(value) is int), None)
    origin["attempt_id"] = str(origin_packet.get("attempt_ref") or "")
    origin["process_fact_scope"] = "original causal execution; no scientific validity inferred"
    current_problem_scope = {**scope, "attempt_id": str(origin_packet.get("attempt_ref") or scope.get("attempt_id") or "")}
    unresolved_findings = _failure_findings(original_source_payload, origin_packet, current_problem_scope)
    publication_failures = [{"error_code": _code(row.get("code"), "INSPECTION_PUBLICATION_FAILED"),
        "detail": _safe(row.get("detail")), "stage": _code(row.get("stage"), "recovery.inspection_publication"),
        "handle": _safe(row.get("handle"), 800), "source_object_id": _safe(row.get("source_object_id"), 800)}
        for row in payload.get("inspection_publication_failures") or [] if isinstance(row, Mapping)]
    if not publication_failures:
        publication_failures = [{"error_code": _code(_mapping(row.get("metadata")).get("code"), "INSPECTION_PUBLICATION_FAILED"),
            "detail": _safe(row.get("summary")), "stage": "supervisor.inspection_publication"}
            for row in packet.get("diagnostic_facts") or [] if isinstance(row, Mapping) and row.get("kind") == "inspection_publication_failure"]
    reason = _code(error.get("code"), "RECOVERY_DIRECTIVE_MISSING")
    technical = bool(publication_failures) or any(row["error_code"] in _TECHNICAL or (
        row["error_code"].startswith("INSPECTION_OBJECT_") and row["error_code"].endswith(("_MISMATCH", "_INTEGRITY_ERROR")))
        for row in failures) or reason in _TECHNICAL
    task_spec = canonical_task_spec_for_state(durable)
    stop = {"schema_version": 1, "producer": PRODUCER, "scope": scope,
        "authority": _authority(durable),
        "expected_state_generation": int(durable.get("state_generation") or 0),
        "origin": origin, "unresolved_findings": unresolved_findings,
        "inspection_failures": failures, "publication_failures": publication_failures,
        "latest_failure": deepcopy(failures[-1]) if failures else {},
        "inspection_observations": [{"tool": _code(row.get("tool")), "source_ref": _safe(row.get("source_ref"), 800),
            "content_sha256": _safe(row.get("content_sha256"), 100),
            "facts": {key: deepcopy(row.get("result", {}).get(key)) for key in ("exists", "is_file", "size", "hash", "hash_status", "inspection") if key in _mapping(row.get("result"))}}
            for row in receipts if isinstance(row, Mapping) and not row.get("error")],
        "task_summary": _safe(task_spec.get("goal") or task_spec.get("question") or task_spec.get("objective") or task_spec.get("user_request"), 1800),
        "stop_reason": {"error_code": reason, "detail": _safe(error.get("detail")),
            **({"resource_receipt": deepcopy(episode["resource_receipt"])} if episode.get("resource_receipt") else {}),
            "transport_receipt": deepcopy(_mapping(error.get("transport_receipt"))),
            "failure_stage": _code(error.get("failure_stage")),
            **({"budget": _budget_projection(error["budget"])} if error.get("budget") else {}),
            **({"dispatch_status": "not_started"} if error.get("dispatch_status") == "not_started" else {}),
            "no_progress_rounds": int(audit.get("no_progress_rounds") or 0),
            "model_turns": int(audit.get("turn_count") or 0), "tool_calls": int(audit.get("tool_call_count") or 0)},
        "resolution": "unresolved", "automatic_actions_stopped": True,
        "technical_classification": ("recovery_provider_transport_error" if reason == "RECOVERY_PROVIDER_TRANSPORT_FAILED" else "inspection_infrastructure_error" if technical else "inspection_unresolved"),
        "execution": _execution(durable, source, scope, packet),
        "stages": [{"step_id": _code(row.get("step_id")), "title": _safe(row.get("title"), 400),
            "status": _code(row.get("status"), "unknown")} for row in _steps(durable)],
        "archive_ref": _safe(archive_ref, 800), "summary_mode": "deterministic", "summary_llm_calls": 0,
        "persistence_status": "unconfirmed", "reports_status": "not_attempted", "report_paths": {},
        "report_receipts": {}, "secondary_failures": [{"stage": _code(row.get("stage"), "recovery.inspection"),
            "error_code": _code(row.get("error_code") or row.get("code"), "INSPECTION_SECONDARY_FAILURE"),
            **({"detail": _safe(row.get("detail"))} if row.get("detail") else {})}
            for row in episode.get("secondary_failures") or [] if isinstance(row, Mapping)]}
    reception = _mapping(episode.get("reception"))
    for key in ("output_resubmission_count", "output_resubmissions_remaining"):
        value = audit.get(key, reception.get(key))
        if type(value) is int and value >= 0:
            stop["stop_reason"][key] = value
    exhausted = error.get("output_resubmission_exhausted", audit.get("output_resubmission_exhausted",
                          reception.get("output_resubmission_exhausted")))
    if type(exhausted) is bool:
        stop["stop_reason"]["output_resubmission_exhausted"] = exhausted
    response_receipts = reception.get("response_receipts") or audit.get("response_receipts") or []
    if response_receipts and isinstance(response_receipts[-1], Mapping):
        last = response_receipts[-1]
        if last.get("error_code") == reason and last.get("status") == "not_accepted":
            stop["stop_reason"]["finish_reason"] = _code(last.get("finish_reason"), "UNKNOWN")
    stop["model_diagnoses"] = [{"source_message_id": _safe(row.get("source_message_id"), 200),
        "text": _safe(row.get("text"), 4000), "authority": "unverified_model_explanation"}
        for row in reception.get("explanations", []) if isinstance(row, Mapping) and row.get("text")][-12:]
    stop["directive_reception"] = deepcopy(reception.get("response_receipts") or [])
    identity = {key: stop[key] for key in ("scope", "authority", "origin", "unresolved_findings", "inspection_failures", "publication_failures", "stop_reason", "execution", "archive_ref")}
    stop["stop_id"] = "inspection-stop:" + _hash(identity)
    stop["explanation"] = inspection_stop_explanation(stop)
    return stop


def inspection_stop_explanation(stop):
    """Always produce useful text from held facts; no I/O or provider call."""
    execution = _mapping(stop.get("execution"))
    scope = _mapping(stop.get("scope"))
    reason = _mapping(stop.get("stop_reason"))
    leading = "旧 inspection 停止结果已过期，未用于停止当前计划或覆盖更新进度。" if stop.get("resolution") == "stale_scope" else "自动检查已停止，当前问题尚未解决。"
    lines = [leading,
        "原任务：" + (_safe(stop.get("task_summary"), 1800) or "以当前批准任务为准") + "。",
        "当前步骤：" + _safe(scope.get("step_id")) + "；执行 attempt：" + _safe(scope.get("attempt_id")) + "。",
        ("命令已执行，returncode=" + str(execution.get("returncode")) + "。") if execution.get("executed") else "没有可确认的命令执行记录。",
        "确定性检查：" + ("通过" if execution.get("deterministic_passed") else "尚无足够事实确认通过") + "；步骤正式提交：" + ("已提交" if execution.get("committed") else "未提交") + "。"]
    concerns = _mapping(stop.get("origin")).get("concerns") or []
    lines.append("Supervisor 待核实的疑虑：" + ("；".join(_safe(item, 1800) for item in concerns) or _safe(_mapping(stop.get("origin")).get("summary")) or "仍需完成语义复核") + "。")
    for finding in stop.get("unresolved_findings") or []:
        lines.append("当前未解决检查：" + _safe(finding.get("code")) + "；" + _safe(finding.get("detail"))
                     + "；来源=" + _safe(finding.get("source")) + "；位置=" + _safe(finding.get("source_pointer")) + "。")
    failures = stop.get("inspection_failures") or []
    if failures:
        lines.append("实际读取失败：" + "；".join(
            _safe(row.get("tool")) + " [" + _safe(row.get("tool_call_id")) + "] " + _safe(row.get("error_code")) +
            " (ref=" + _safe(row.get("evidence_ref"), 800) + ")" + (": " + _safe(row.get("detail")) if row.get("detail") else "")
            for row in failures) + "。")
    else:
        lines.append("本次没有已记录的工具读取错误；策略仍未形成可接纳的解决决定。")
    if stop.get("publication_failures"):
        lines.append("证据交接问题：" + "；".join(_safe(row.get("error_code")) + ": " + _safe(row.get("detail")) for row in stop["publication_failures"]) + "。")
    lines.append("停止原因：" + _safe(reason.get("error_code")) + "；连续无进展轮次=" + str(reason.get("no_progress_rounds", 0)) +
        "，模型轮次=" + str(reason.get("model_turns", 0)) + "，工具调用=" + str(reason.get("tool_calls", 0)) + "。")
    transport = _mapping(reason.get("transport_receipt"))
    if transport:
        lines.append("Recovery 请求传输：实际尝试=" + str(transport.get("attempt_ordinal")) +
            "/" + str(transport.get("configured_max_attempts")) + "；停止依据=" +
            _safe(transport.get("terminal_failure_reason")) + "；末次错误=" +
            _safe(transport.get("failure_type")) + "。远端处理状态 UNKNOWN。")
    if stop.get("model_diagnoses"):
        lines.append("模型诊断（尚未验证，不代表指令已接纳或动作已执行）：\n" + "\n".join(
            _safe(row.get("text"), 4000) for row in stop["model_diagnoses"]))
    lines.append("读取失败或预算结束不能证明科学依赖不可用、数据不兼容或科学目标已经完成。")
    unfinished = [row for row in stop.get("stages") or [] if row.get("status") not in _FINISHED]
    lines.append("未完成阶段：" + ("；".join(_safe(row.get("step_id")) + "=" + _safe(row.get("status")) for row in unfinished) or "没有可确认的清单") + "。")
    lines.append("尚无本次后续科学结果的完成证明；生成状态报告不等于科学产物通过验收。")
    if stop.get("persistence_status") != "committed":
        lines.append("停止状态尚未确认持久化；本进程不会因此继续自动动作。")
    if stop.get("report_paths"):
        lines.append("已生成报告：" + "；".join(_safe(path, 1600) for path in stop["report_paths"].values()) + "。")
    else:
        lines.append("本次尚未确认生成报告文件。")
    if stop.get("secondary_failures"):
        lines.append("附加问题：" + "；".join(_safe(row.get("error_code")) for row in stop["secondary_failures"]) + "。")
    return "\n\n".join(lines)


def inspection_stop_outcome(stop):
    """Reuse a typed terminal without equating exhausted policy with corruption."""
    value = deepcopy(dict(stop))
    value["explanation"] = inspection_stop_explanation(value)
    refs = list(dict.fromkeys([*(_mapping(value.get("origin")).get("evidence_refs") or []), *([value["archive_ref"]] if value.get("archive_ref") else [])]))
    return RecoveryOutcome(schema_version=1, type="blocked", summary=value["explanation"],
        scope=value["scope"], payload={"reason_code": value["stop_reason"]["error_code"],
            "blocked_evidence": refs, "source_scope": value["scope"], "budget_receipt": value["stop_reason"],
            "inspection_stop": value}, evidence_refs=tuple(refs))


def _secondary(stop, stage, code, exc=None):
    row = {"stage": stage, "error_code": code}
    if exc is not None:
        row["exception_type"] = type(exc).__name__
        row["detail"] = _safe(str(exc), 800)
        if exc.__cause__ is not None:
            row["cause_type"] = type(exc.__cause__).__name__
    if row not in stop["secondary_failures"]:
        stop["secondary_failures"].append(row)


def _planning_report_targets(durable, task_root):
    """Only explicit TaskSpec stop documents; rejected Plan paths confer no authority.

    These are failure explanations, not approved scientific outputs. The normal
    publisher still enforces ownership and protects every unowned existing file.
    """
    spec = canonical_task_spec_for_state(durable)
    outputs = effective_output_contracts(spec)
    by_ref = {str(row.get(k)): row for row in outputs for k in ("artifact_id", "output_id", "semantic_handle") if row.get(k)}
    authorized = {"user_explicit", "user_confirmed"}
    names = {"report": "report.md", "reproduce_script": "reproduce.sh", "stage_status": "stage_status.tsv"}
    result = {}
    for row in outputs:
        name = names.get(row.get("role")) or row.get("required_basename") or row.get("filename")
        if name not in _REPORT_NAMES:
            continue
        if str(row.get("source") or row.get("source_type")) not in authorized:
            raise ValueError("PLANNING_REPORT_USER_AUTHORITY_MISSING")
        if row.get("path_policy") == "managed_exact" and row.get("canonical_path"):
            target = _path(row["canonical_path"])
            if target.name != name:
                raise ValueError("PLANNING_REPORT_FILENAME_CONFLICT")
        elif row.get("path_policy") == "planner_materialized":
            root = by_ref.get(str(row.get("root_ref") or "")) or {}
            if (str(root.get("source") or root.get("source_type")) not in authorized
                    or root.get("artifact_kind") != "directory" or root.get("path_policy") != "managed_exact"
                    or not root.get("canonical_path")):
                raise ValueError("PLANNING_REPORT_ROOT_NOT_AUTHORIZED")
            directory = _path(root["canonical_path"])
            target = _path(directory / name)
            if target.parent != directory:
                raise ValueError("PLANNING_REPORT_PATH_ESCAPE")
        else:
            raise ValueError("PLANNING_REPORT_LOCATION_UNRESOLVED")
        if name in result and result[name] != target:
            raise ValueError("PLANNING_REPORT_OUTPUT_AMBIGUOUS")
        result[name] = target
    if not result:
        # Preserve the existing session-local diagnostic delivery for tasks
        # without any explicit report output. Never substitute it for a known
        # but unauthorized/ambiguous requested location.
        result = {name: _path(task_root / name) for name in _REPORT_NAMES}
    for row in spec.get("inputs", []) or []:
        raw = row.get("canonical_path") or row.get("path")
        if raw:
            input_path = _path(raw)
            if any(target == input_path or (row.get("artifact_kind") == "directory" and target.is_relative_to(input_path))
                   for target in result.values()):
                raise ValueError("PLANNING_REPORT_INPUT_OVERLAP")
    return result


def _report_targets(durable, task_root):
    """Resolve only declared, approved output contracts and materializations."""
    spec = canonical_task_spec_for_state(durable)
    outputs = effective_output_contracts(spec)
    by_ref = {str(row.get(k)): row for row in outputs for k in ("artifact_id", "output_id", "semantic_handle") if row.get(k)}
    materialized = {str(row.get(k)): row for row in durable.get("task_output_materializations") or []
        if isinstance(row, Mapping) for k in ("output_ref", "output_id") if row.get(k)}
    approved = plan_approval_is_current(durable, int(_mapping(durable.get("current_task")).get("task_revision") or durable.get("task_revision") or 0))
    inputs = {str(row.get("canonical_path") or row.get("path") or "") for row in spec.get("inputs") or [] if isinstance(row, Mapping)}
    result = {}
    for row in outputs:
        if str(row.get("artifact_kind") or "file") != "file":
            continue
        material = materialized.get(str(row.get("artifact_id") or "")) or materialized.get(str(row.get("output_id") or "")) or {}
        declared = any(row.get(key) for key in ("canonical_path", "path", "resolved_path"))
        realized = any(material.get(key) for key in ("canonical_path", "path", "resolved_path"))
        root_contract = by_ref.get(str(row.get("root_ref") or "")) or {}
        # Bare report names are relative to the recorded managed task root;
        # planner materializations may use an explicitly declared output root.
        # The existing approval/root checks below still authorize the target.
        trusted_root = str(root_contract.get("canonical_path") or task_root)
        raw = approved_resource_locator(row, trusted_root=trusted_root)["path"] if declared else approved_resource_locator(material, trusted_root=trusted_root)["path"] if realized else str(row.get("filename") or "")
        if declared and realized and approved_resource_locator(material, trusted_root=trusted_root)["path"] != raw:
            raise ValueError("REPORT_LOCATION_CONFLICT")
        if Path(raw).name not in _REPORT_NAMES:
            continue
        target = Path(raw)
        if not target.is_absolute():
            if target.parts != (target.name,):
                raise ValueError("INSPECTION_REPORT_RELATIVE_PATH_REJECTED")
            target = task_root / target.name
        target = _path(target)
        if not target.is_relative_to(task_root):
            root = by_ref.get(str(row.get("root_ref") or "")) or {}
            root_path = str(root.get("canonical_path") or "")
            explicit = (str(row.get("source") or row.get("source_type")) in {"user_explicit", "user_confirmed"}
                and row.get("path_policy") == "managed_exact" and str(row.get("canonical_path") or "") == str(target))
            material_allowed = (row.get("path_policy") == "planner_materialized" and material
                and str(root.get("source") or root.get("source_type")) in {"user_explicit", "user_confirmed"}
                and root.get("path_policy") == "managed_exact" and root_path and target.is_relative_to(_path(root_path)))
            if not approved or not (explicit or material_allowed):
                raise ValueError("INSPECTION_REPORT_OUTPUT_NOT_APPROVED")
        if str(target) in inputs:
            raise ValueError("INSPECTION_REPORT_INPUT_OVERLAP")
        if target.name in result and result[target.name] != target:
            raise ValueError("INSPECTION_REPORT_OUTPUT_AMBIGUOUS")
        result[target.name] = target
    return result



def _internal_report_targets(durable, task_root, stop_id):
    """Use an existing explicit diagnostic directory contract, never guess one.

    A managed task store alone permits state/trace writes; it is not blanket
    permission to invent delivered reports. Directory roles remain ordinary
    canonical output contracts, approved through the existing handoff/Plan.
    """
    revision=int(_mapping(durable.get("current_task")).get("task_revision") or durable.get("task_revision") or 0)
    if not plan_approval_is_current(durable,revision):return {}, "NO_CURRENT_DIAGNOSTIC_AUTHORIZATION"
    spec=canonical_task_spec_for_state(durable)
    roots=[row for row in effective_output_contracts(spec)
        if row.get("artifact_kind")=="directory" and row.get("role") in {"diagnostic_root","diagnostic_directory","report_root"}
        and str(row.get("source") or row.get("source_type")) in {"user_explicit","user_confirmed"}
        and row.get("path_policy")=="managed_exact" and row.get("canonical_path")]
    if len(roots)!=1:return {}, "NO_UNAMBIGUOUS_AUTHORIZED_DIAGNOSTIC_ROOT"
    root=_path(roots[0]["canonical_path"])
    for row in spec.get("inputs",[]) or []:
        raw=row.get("canonical_path") or row.get("path")
        if raw and (root==Path(raw) or (row.get("artifact_kind")=="directory" and root.is_relative_to(Path(raw)))):
            raise ValueError("INTERNAL_REPORT_INPUT_OVERLAP")
    target=root/"managed_stops"/_hash({"task_id":durable.get("task_id"),"stop_id":stop_id})
    return {name:_path(target/name) for name in _REPORT_NAMES}, str(roots[0].get("artifact_id") or roots[0].get("output_id"))


def _report_bodies(stop):
    # File bodies are stable across finalize/reload and do not claim the write
    # of their own files has completed before the writer returns its receipt.
    text_stop = deepcopy(stop)
    text_stop.update(report_paths={}, secondary_failures=[], reports_status="pending")
    text = inspection_stop_explanation(text_stop)
    rows = ["stage\tstatus\toutput_path\tnotes"]
    for row in stop.get("stages") or []:
        rows.append("\t".join((_safe(row.get("step_id")), _safe(row.get("status")), "-", _safe(row.get("title"), 400).replace("\t", " ").replace("\n", " "))))
    rows.append("run\tblocked\t-\t" + stop["stop_reason"]["error_code"] + "; unresolved inspection; no scientific acceptance")
    execution = stop["execution"]
    script = ["#!/bin/sh", "# Inspection stopped; this file does not rerun scientific commands.",
        "# Stop identity: " + stop["stop_id"], "# Task: " + stop["scope"]["task_id"],
        "# Executed attempt: " + execution["attempt_id"] + "; formal step commit is pending."]
    if execution.get("executed_script_ref"):
        script.append("# Recorded executed script reference: " + execution["executed_script_ref"].replace("\n", " "))
    script.append("# Exact executed command body is not available in this bounded stop; it has not been reconstructed.")
    script += ["# Unfinished: " + row["step_id"] + " (" + row["status"] + ")" for row in stop.get("stages") or [] if row["status"] not in _FINISHED]
    script.append("exit 1")
    return {"report.md": "# BioMind inspection stop\n\nStop identity: " + stop["stop_id"] + "\n\n" + text + "\n",
        "stage_status.tsv": "\n".join(rows) + "\n", "reproduce.sh": "\n".join(script) + "\n"}


def finalize_inspection_stop(data_path, stop, *, loader=load_plan_state, saver=save_plan_state):
    """Persist a scoped lifecycle stop, then write only its authorized reports.

    The lifecycle commit precedes report I/O. Its receipt is enough for normal
    reload to stop, even when a later report/store write fails. All late writes
    recheck current identity and use CAS; no step or runtime record is changed.
    """
    value = deepcopy(dict(stop))
    value.setdefault("secondary_failures", [])
    task_id = value["scope"]["task_id"]
    try:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}", str(task_id)):
            raise ValueError("INSPECTION_STOP_TASK_ID_REJECTED")
        durable = loader(data_path, task_id)
        if not _stop_matches(durable, value):
            value["resolution"] = "stale_scope"
            value["persistence_status"] = "not_applied"
            _secondary(value, "finalize.scope", "INSPECTION_STOP_STALE_SCOPE")
            value["explanation"] = inspection_stop_explanation(value)
            return value
        prior = current_inspection_stop(durable, scope=value["scope"])
        if prior:
            if prior.get("stop_id") != value.get("stop_id"):
                _secondary(prior, "finalize.identity", "INSPECTION_STOP_IDENTITY_CONFLICT")
                prior["explanation"] = inspection_stop_explanation(prior)
                return prior
            # A completed same-stop finalization is immutable and does not reset
            # budget, re-enter readers, or publish extra files on reload.
            if prior.get("reports_status") == "finalized":
                return prior
            value = prior
        else:
            # Read-only inspection/compaction can advance the store generation
            # without changing this immutable causal packet or attempt. Scope
            # validation above and the fresh loader generation anchor the CAS.
            value["commit_expected_state_generation"] = int(durable.get("state_generation") or 0)
            candidate = deepcopy(durable)
            value["persistence_status"] = "committed"
            value["explanation"] = inspection_stop_explanation(value)
            set_task_status(candidate, "blocked", value["stop_reason"]["error_code"])
            candidate["task_state"]["inspection_stop"] = deepcopy(value)
            saver(data_path, task_id, candidate)
            durable = loader(data_path, task_id)
    except Exception as exc:
        value["persistence_status"] = "unconfirmed"
        _secondary(value, "finalize.persist", "INSPECTION_STOP_CAS_CONFLICT" if isinstance(exc, StateCASConflict) else "INSPECTION_STOP_PERSIST_FAILED", exc)
        value["explanation"] = inspection_stop_explanation(value)
        return value

    try:
        task_root = _path(Path(data_path).absolute() / "task_outputs" / task_id)
        targets = _report_targets(durable, task_root)
        bodies = _report_bodies(value)
        for name, path in targets.items():
            try:
                fresh = loader(data_path, task_id)
                if not _stop_matches(fresh, value):
                    raise ValueError("INSPECTION_STOP_STALE_SCOPE")
                receipt = publish_report(data_path, task_id, path, bodies[name], owner=value["stop_id"],
                    authorization_ref=_hash(value["authority"]), scope_guard=lambda fresh: _stop_matches(fresh,value))
                value["report_paths"][name] = str(path)
                value["report_receipts"][str(path)] = {**receipt, "stop_id": value["stop_id"], "task_id": task_id}
            except Exception as exc:
                code = _code(getattr(exc, "code", "")) or _code(str(exc)) or "INSPECTION_REPORT_WRITE_FAILED"
                _secondary(value, "report." + name, code, exc)
    except Exception as exc:
        _secondary(value, "report.contract", _code(getattr(exc, "code", "")) or _code(str(exc)) or "INSPECTION_REPORT_CONTRACT_FAILED", exc)
    value["reports_status"] = "finalized"
    value["explanation"] = inspection_stop_explanation(value)
    try:
        fresh = loader(data_path, task_id)
        if not _stop_matches(fresh, value) or current_inspection_stop(fresh, scope=value["scope"]).get("stop_id") != value["stop_id"]:
            raise ValueError("INSPECTION_STOP_STALE_SCOPE")
        fresh["task_state"]["inspection_stop"] = deepcopy(value)
        saver(data_path, task_id, fresh)
    except Exception as exc:
        _secondary(value, "report.persist", "INSPECTION_REPORT_RECEIPT_PERSIST_FAILED", exc)
        value["explanation"] = inspection_stop_explanation(value)
    return value


def inspection_terminal_projection(values, durable=None):
    """One pure projection for status, profiler, and InteractiveRunResult.

    A known newer or runtime-owned durable scope wins. An in-memory stop whose
    persistence failed remains useful when no contradictory scope is known.
    """
    values = _mapping(values)
    known_durable = bool(durable and not durable.get("load_error") and not durable.get("canonical_state_error"))
    if values.get("task_status") in {"cancelled", "completed"} or (not known_durable and (
        values.get("runtime_recovery_result") or values.get("pending_runtime_evidence_request"))):
        return {}
    raw = _mapping(values.get("recovery_outcome"))
    stop = _mapping(_mapping(raw.get("payload")).get("inspection_stop"))
    if stop and (raw.get("type") not in {"blocked", "internal_protocol_error"} or stop.get("producer") != PRODUCER):
        stop = {}
    if known_durable:
        if _runtime_owned(durable):
            return {}
        if stop and not _stop_matches(durable, stop):
            return {}
        if not stop:
            stop = current_inspection_stop(durable)
    if not stop or stop.get("resolution") == "stale_scope":
        return {}
    reason = _mapping(stop.get("stop_reason"))
    refs = list(dict.fromkeys([*(_mapping(stop.get("origin")).get("evidence_refs") or []), *([stop["archive_ref"]] if stop.get("archive_ref") else [])]))
    diagnostic = {"error_code": reason.get("error_code"), "category": "TASK_BLOCKED", "error_owner": "recovery",
        "owner": "recovery", "stage": "recovery.inspection", "detail": inspection_stop_explanation(stop),
        "reason": inspection_stop_explanation(stop), "resolution": stop.get("resolution"),
        "scope": deepcopy(stop["scope"]), "origin": deepcopy(stop.get("origin") or {}),
        "inspection_failures": deepcopy(stop.get("inspection_failures") or []),
        "stop_reason": deepcopy(reason), "execution": deepcopy(stop.get("execution") or {}),
        "primary_cause": deepcopy(stop.get("origin") or {}),
        "unresolved_findings": deepcopy(stop.get("unresolved_findings") or []),
        "secondary_causes": deepcopy(stop.get("publication_failures") or []) + deepcopy(stop.get("inspection_failures") or []) + deepcopy(stop.get("secondary_failures") or []),
        "evidence_refs": refs, "report_paths": deepcopy(stop.get("report_paths") or {}),
        "persistence_status": stop.get("persistence_status"), "stop_id": stop.get("stop_id"),
        "technical_classification": stop.get("technical_classification")}
    if stop.get("report_paths", {}).get("report.md"):
        diagnostic["report_path"] = stop["report_paths"]["report.md"]
    return {"status": "blocked", "terminal_diagnostic": diagnostic, "evidence_refs": refs}


__all__ = ["build_inspection_stop", "current_inspection_stop", "finalize_inspection_stop",
    "inspection_stop_scope", "inspection_stop_explanation", "inspection_stop_outcome", "inspection_terminal_projection"]

"""Causal, transcript-free projection into the typed Recovery boundary.

A failing producer (or its deterministic adapter) creates and persists one
immutable ``FailurePacket`` before the graph enters Recovery.  Recovery resolves
that exact packet; retained outcome channels are deliberately never scanned.
Full execution/validation records stay upstream and are represented only by
bounded facts and immutable references.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import (
    canonical_task_spec_for_state,
    load_plan_state,
    plan_record_hash,
    save_plan_state,
    semantic_plan_hash,
)
from agent_core.task_spec import (
    task_record_hash,
    task_semantic_hash,
    task_spec_is_initialized,
    task_spec_revision,
)
from agent_core.workflow.outcomes import (
    CommitStepOutcome,
    ExecuteOutcome,
    InstallSubagentOutcome,
    VerificationOutcome,
)
from agent_core.workflow.inspection_evidence import prepare_inspection_evidence, InspectionEvidenceError
from agent_core.workflow.stage_contracts import (
    FailurePacket,
    PlanSpecRef,
    RecoverySeed,
    SelectedSkillBundle,
    SelectedSkillRef,
    StageContractError,
    TaskSpecRef,
    freeze,
)


Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]
RuntimeNode = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


CAUSAL_FAILURE_LIFECYCLE_FIELD = "causal_failure_packet_lifecycle"
_CAUSAL_FAILURE_LIFECYCLE_TRANSITIONS = frozenset({
    "active", "resolved", "superseded",
})


_SOURCE_CONTEXT_KEYS = frozenset({
    "messages",
    "message_history",
    "history",
    "conversation",
    "conversation_history",
    "transcript",
    "raw_transcript",
    "prior_transcript",
    "tool_transcript",
    "full_skill_doc",
    "full_skill_docs",
    "full_skill_document",
    "full_skill_documents",
    "skill_doc",
    "skill_docs",
    "skill_document",
    "skill_documents",
    "loaded_skill_documents",
    "exact_text",
    "raw_response",
    "raw_content",
    "system_prompt",
    "prompt",
    "tool_calls",
    "tool_output",
    "tool_outputs",
})

_CURRENT_STEP_FIELDS = (
    "step_id",
    "title",
    "goal",
    "purpose",
    "action",
    "inputs",
    "outputs",
    "dependencies",
    "success_criteria",
    "skill_ids",
    "execution_capability_id",
    "capability_contract_hash",
    "risk_level",
)

_COMMON_RUNTIME_PAYLOAD_FIELDS = (
    "disposition",
    "diagnosis",
    "reason_code",
    "violation_code",
    "summary",
    "decision_id",
    "incident_id",
    "causal_parent_packet_ref", "recovery_decision_id", "diagnostic_request_digest",
    "failure_fingerprint",
    "candidate_fingerprint",
    "failure_owner",
    "strategy",
    "repair_instruction",
    "prior_validation_id",
    "question_id",
    "question",
    "error_code",
    "attempt_id",
    "action_id",
    "envelope_identity",
    "status",
    "return_code",
    "command_started",
    "expected_state_generation",
    "current_state_generation",
    "failed_invariant",
    "no_partial_mutation",
)


def _hash(value: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return digest[:24]


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _reject_recursive_context(value: Any, path: tuple[str, ...] = ()) -> None:
    """Reject conversational/Skill payloads while permitting raw stream evidence.

    Typed execution evidence may legitimately contain ``stdout``/``stderr``;
    those records never enter the projection and are replaced by references.
    Conversation transcripts and full Skill documents have no such exception.
    """

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = _normalized_key(key)
            location = ".".join((*path, key))
            if (
                normalized in _SOURCE_CONTEXT_KEYS
                or normalized.endswith("_transcript")
                or normalized.endswith("_messages")
                or "full_skill_doc" in normalized
            ):
                raise StageContractError("STAGE_CONTEXT_LEAK_FORBIDDEN", location)
            _reject_recursive_context(child, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_recursive_context(child, (*path, str(index)))


_SOURCE_CONTRACTS: Mapping[str, type[Any]] = {
    "execute_outcome": ExecuteOutcome,
    "verification_outcome": VerificationOutcome,
    "completion_gate_outcome": VerificationOutcome,
    "commit_step_outcome": CommitStepOutcome,
    "install_subagent_outcome": InstallSubagentOutcome,
}


def _validated_source(source_kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
    contract = _SOURCE_CONTRACTS.get(str(source_kind or ""))
    if contract is None:
        raise StageContractError(
            "RECOVERY_CAUSAL_SOURCE_UNKNOWN", str(source_kind or "<missing>")
        )
    return contract.from_mapping(value).to_dict()


def recovery_source_kind(
    outcome_field: str, outcome: Mapping[str, Any]
) -> str:
    """Return the producer-owned semantic source kind for one exact outcome."""

    if outcome_field == "verification_outcome":
        payload = dict(outcome.get("payload") or {})
        gate = payload.get("completion_gate")
        if (
            str(outcome.get("type") or "") == "rejected"
            and isinstance(gate, Mapping)
            and gate.get("passed") is False
        ):
            return "completion_gate_outcome"
    return str(outcome_field or "")


def is_recovery_failure_source(
    source_kind: str, source: Mapping[str, Any]
) -> bool:
    """Match only outcome semantics whose graph edge enters Recovery."""

    outcome_type = str(source.get("type") or "")
    payload = dict(source.get("payload") or {})
    if source_kind == "execute_outcome":
        if outcome_type in {"execution_failed", "diagnostic_completed"}:
            return True
        return (
            outcome_type == "pre_execution_rejected"
            and str(payload.get("violation_owner") or "").upper()
            in {"VERIFICATION", "DEPENDENCY"}
        )
    if source_kind in {"verification_outcome", "completion_gate_outcome"}:
        return outcome_type in {
            "rejected", "incomplete_evidence", "diagnostic_evidence_recorded",
            "verification_error", "inspection_required", "correction_required", "assessment_pending",
        }
    if source_kind == "commit_step_outcome":
        return outcome_type in {"commit_conflict", "contract_rejected"}
    if source_kind == "install_subagent_outcome":
        return outcome_type == "installation_failed"
    return False


def _unique_text(*values: Any) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return tuple(result)


def _compact_diagnostic(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "execution_id",
        "execution_attempt_id", "recovery_decision_id", "probe_id", "evidence_fingerprint", "stdout_ref", "stderr_ref",
        "attempt_id",
        "action_id",
        "evidence_ref",
        "diagnostic_id",
        "validation_id",
        "status",
        "returncode",
        "exit_code",
        "failure_fingerprint",
    ):
        item = value.get(key)
        if item not in (None, "", [], {}):
            result[key] = item
    refs = _unique_text(value.get("evidence_refs") or ())
    if refs:
        result["evidence_refs"] = list(refs)
    return result


def _compact_budget(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = (
        "incident_id",
        "ordinal",
        "limit",
        "remaining",
        "consumed",
        "progress_proven",
        "source_fingerprint",
    )
    return {
        key: value[key]
        for key in allowed
        if key in value and value[key] not in (None, "", [], {})
    }


_TYPED_FAILURE_METADATA_ALIASES: Mapping[str, tuple[str, ...]] = {
    "issue_id": ("issue_id", "result_id", "predicate_id", "fact_id"),
    "code": ("code", "error_code", "reason_code", "violation_code"),
    "affected_resource": (
        "affected_resource", "affected_resource_id", "canonical_resource_id",
        "workspace_ref", "resource_id", "observation_id", "binding_id",
    ),
    "formal_path": (
        "formal_path", "formal_concrete_path", "expected_path",
    ),
    "observed_path": (
        "observed_path", "actual_path", "observed_workspace_path",
    ),
    "receipt_reason": (
        "receipt_reason", "attestation_status", "unknown_reason",
    ),
}
_TYPED_RECORD_KEYS = (
    "binding", "binding_issue", "binding_result", "binding_validation",
    "receipt", "receipt_issue", "receipt_result", "receipt_validation",
    "workspace_mutation_receipt", "parser_receipt",
    "runtime_access_evidence", "runtime_access_evidence_validation",
)


def _bounded_scalar(value: Any, *, limit: int = 2048) -> Any:
    if isinstance(value, str):
        from execution.preflight_progress import without_progress
        return without_progress(value)[:limit]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:limit]


def _compact_failure_metadata(*values: Any) -> dict[str, Any]:
    """Project only typed repair facts, never a complete report or receipt."""

    result: dict[str, Any] = {}
    refs: list[str] = []

    def visit(value: Any, *, nested: bool = False) -> None:
        if not isinstance(value, Mapping):
            return
        for canonical, aliases in _TYPED_FAILURE_METADATA_ALIASES.items():
            if canonical in result:
                continue
            for alias in aliases:
                item = value.get(alias)
                if item not in (None, "", [], {}):
                    result[canonical] = _bounded_scalar(item)
                    break
        for ref in _unique_text(
            value.get("evidence_ref"), value.get("evidence_refs") or (),
            value.get("evidence_id"),
        )[:8]:
            if ref not in refs:
                refs.append(ref)

        # A scope mismatch is the typed formal-vs-observed comparison emitted
        # by semantic completion.  Preserve the pair, not the surrounding blob.
        mismatches = value.get("scope_mismatches")
        if isinstance(mismatches, Mapping):
            for path_key in (
                "formal_concrete_path", "formal_path", "expected_path",
                "observed_workspace_path",
            ):
                mismatch = mismatches.get(path_key)
                if isinstance(mismatch, Mapping):
                    expected = mismatch.get("expected")
                    observed = mismatch.get("observed")
                    if expected not in (None, ""):
                        result.setdefault("formal_path", _bounded_scalar(expected))
                    if observed not in (None, ""):
                        result.setdefault("observed_path", _bounded_scalar(observed))
                    break
        for source_key, target_key in (
            ("expected_postcondition_paths", "expected_paths"),
            ("observed_postcondition_paths", "observed_paths"),
            ("expected_paths", "expected_paths"),
            ("observed_paths", "observed_paths"),
        ):
            paths = value.get(source_key)
            if isinstance(paths, (list, tuple)) and paths:
                result.setdefault(target_key, [
                    _bounded_scalar(item, limit=1024) for item in paths[:12]
                ])
        after = value.get("after_evidence")
        if isinstance(after, Mapping) and after.get("root") not in (None, ""):
            result.setdefault("observed_path", _bounded_scalar(after.get("root")))
        if not nested:
            metadata = value.get("metadata")
            if isinstance(metadata, Mapping):
                visit(metadata, nested=True)
            child_keys = _TYPED_RECORD_KEYS
        else:
            # Runtime access evidence is nested under an attester receipt.  It
            # is the only deliberately supported second-level typed record;
            # do not recursively project arbitrary producer payloads.
            child_keys = (
                "runtime_access_evidence",
                "runtime_access_evidence_validation",
            )
        for key in child_keys:
            child = value.get(key)
            if isinstance(child, Mapping):
                visit(child, nested=True)

    for value in values:
        visit(value)
    if refs:
        result["evidence_refs"] = refs[:8]
    return result


def _compact_missing_effects(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(value, key=str)[:12]:
        item = value[key]
        if isinstance(item, (list, tuple)):
            result[str(key)] = [
                _bounded_scalar(child, limit=512) for child in item[:12]
            ]
        elif item not in (None, "", {}, []):
            result[str(key)] = _bounded_scalar(item, limit=512)
    return result


def _bounded_fact_value(value: Any, *, depth: int = 0) -> Any:
    """Bound typed report values without flattening their repair semantics."""

    if isinstance(value, Mapping):
        if depth >= 2:
            return _bounded_scalar(value, limit=512)
        return {
            str(key): _bounded_fact_value(value[key], depth=depth + 1)
            for key in sorted(value, key=str)[:12]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_fact_value(item, depth=depth + 1)
            for item in value[:12]
        ]
    return _bounded_scalar(value, limit=512)


def _compact_typed_findings(value: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in value if isinstance(value, (list, tuple)) else ():
        if not isinstance(raw, Mapping):
            continue
        row = {
            key: _bounded_fact_value(raw[key])
            for key in (
                "issue_id", "nature", "code", "contract_source", "actual",
                "expected", "evidence_refs", "affected_resource",
                "affected_field", "status", "disposition",
                "confidence_or_unknown_reason",
            )
            if raw.get(key) not in (None, "", [], {})
        }
        metadata = _compact_failure_metadata(raw)
        if metadata:
            row["typed_metadata"] = metadata
        if row:
            result.append(row)
        if len(result) >= limit:
            break
    return result


def _compact_binding_receipt_findings(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in value if isinstance(value, (list, tuple)) else ():
        if not isinstance(raw, Mapping):
            continue
        row = {
            key: _bounded_fact_value(raw[key])
            for key in ("issue_id", "status", "code", "affected_resource")
            if raw.get(key) not in (None, "", [], {})
        }
        receipt = raw.get("receipt")
        if isinstance(receipt, Mapping):
            receipt_projection = {
                key: _bounded_fact_value(receipt[key])
                for key in (
                    "receipt_id", "producer", "canonical_resource_id",
                    "formal_concrete_path", "attestation_status",
                    "unknown_reason", "attested", "scope_mismatches",
                    "evidence_refs", "runtime_access_evidence_validation",
                )
                if receipt.get(key) not in (None, "", [], {})
            }
            if receipt_projection:
                row["receipt"] = receipt_projection
        metadata = _compact_failure_metadata(raw)
        if metadata:
            row["typed_metadata"] = metadata
        if row:
            result.append(row)
        if len(result) >= 12:
            break
    return result


def _compact_effect_presence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in ("declared", "observed", "missing", "unexpected", "invalid"):
        if value.get(key) not in (None, "", [], {}):
            result[key] = _bounded_fact_value(value[key])
    collection = value.get("file_collection")
    if isinstance(collection, Mapping):
        result["file_collection"] = {
            key: _bounded_scalar(collection[key], limit=512)
            for key in ("source", "accepted_set_status", "reconciliation_status", "authority")
            if key in collection
        }
        for key in ("accepted", "observed_candidates", "reconciliation_violations"):
            if isinstance(collection.get(key), (list, tuple)):
                # Preserve an explicit [] and the typed identity/path fields.
                result["file_collection"][key] = [
                    _bounded_fact_value(row) for row in collection[key][:12]
                ]
                if len(collection[key]) > 12:
                    result["file_collection"][key + "_omitted"] = len(collection[key]) - 12
    registration = value.get("formal_registration")
    if isinstance(registration, (list, tuple)):
        result["formal_registration"] = [_bounded_fact_value(row) for row in registration[:12]]
        if len(registration) > 12:
            result["formal_registration_omitted"] = len(registration) - 12
    return result


def _compact_runtime_observations(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {key: _bounded_fact_value(value[key]) for key in
              ("status", "reason_code", "authority", "invalid_record_codes") if key in value}
    records = value.get("records")
    if not isinstance(records, (list, tuple)):
        return result
    result["records"] = []
    for raw in records[:12]:
        if not isinstance(raw, Mapping):
            continue
        row = {key: _bounded_fact_value(raw[key]) for key in (
            "evidence_id", "source_ref", "scope", "observed_at", "expires_at",
            "inspection", "facts", "supersedes",
        ) if key in raw}
        result["records"].append(row)
    if len(records) > 12:
        result["records_omitted"] = len(records) - 12
    return result


def _compact_completion_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {
        key: deepcopy(value[key])
        for key in (
            "report_id", "validation_digest", "deterministic_veto",
            "effective_disposition",
        )
        if value.get(key) not in (None, "", [], {})
    }
    scope = value.get("scope")
    if isinstance(scope, Mapping):
        result["scope"] = {
            key: _bounded_scalar(scope[key], limit=512)
            for key in (
                "task_id", "plan_id", "plan_version", "step_id", "action_id",
                "attempt_id", "validation_id", "state_generation",
            )
            if scope.get(key) not in (None, "")
        }
    gate = _compact_completion_gate(value.get("completion_gate"))
    if gate:
        result["completion_gate"] = gate
    process = value.get("process_outcome")
    if isinstance(process, Mapping):
        result["process_outcome"] = {
            key: _bounded_fact_value(process[key])
            for key in (
                "validation_status", "returncode", "returncode_passed",
                "evidence_complete", "command_role", "execution_status",
                "timed_out", "oom_detected", "process_signal",
                "executor_exception",
            )
            if process.get(key) not in (None, "", [], {})
        }
        runtime = _compact_runtime_observations(process.get("runtime_observations"))
        if runtime:
            result["process_outcome"]["runtime_observations"] = runtime
    presence = _compact_effect_presence(value.get("effect_presence"))
    if presence:
        result["effect_presence"] = presence
    receipt_findings = _compact_binding_receipt_findings(
        value.get("binding_and_receipt_findings")
    )
    if receipt_findings:
        result["binding_and_receipt_findings"] = receipt_findings
    review = value.get("semantic_review_requirements")
    if isinstance(review, Mapping):
        result["semantic_review_requirements"] = {
            key: _bounded_fact_value(review[key])
            for key in (
                "required", "policy_source", "required_issue_ids",
                "allowed_issue_ids", "decision_schema", "review_input_status",
                "commit_eligible", "commit_authority",
            )
            if review.get(key) not in (None, "", [], {})
        }
    findings = _compact_typed_findings(value.get("findings"))
    if findings:
        result["findings"] = findings
    rows: list[dict[str, Any]] = []
    for raw in value.get("predicate_results") or ():
        if not isinstance(raw, Mapping) or raw.get("passed") is True:
            continue
        row = {
            key: deepcopy(raw[key])
            for key in (
                "result_id", "predicate_id", "observation_id", "status",
                "passed", "reason_code", "disposition", "parser_id",
            )
            if raw.get(key) not in (None, "", [], {})
        }
        metadata = _compact_failure_metadata(raw)
        if metadata:
            row["typed_metadata"] = metadata
        parser_receipt = raw.get("parser_receipt")
        if isinstance(parser_receipt, Mapping):
            # Registered parser rejection, not a model success claim or full
            # scientific receipt. Keep the actual format/metadata diagnosis.
            row["parser_receipt"] = {key: _bounded_fact_value(parser_receipt[key])
                for key in ("parser_id", "value", "error_code", "detail") if key in parser_receipt}
        for key in ("detail", "message", "field_path", "source_pointer"):
            if key in raw:
                row[key] = _bounded_fact_value(raw[key])
        if row:
            rows.append(row)
        if len(rows) >= 8:
            break
    if rows:
        result["failed_predicates"] = rows
    return result


def _compact_completion_gate(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {
        key: deepcopy(value[key])
        for key in ("passed", "classification", "message", "feedback")
        if value.get(key) not in (None, "", [], {})
    }
    missing = _compact_missing_effects(value.get("missing_effects"))
    if missing:
        result["missing_effects"] = missing
    metadata = _compact_failure_metadata(value)
    if metadata:
        result["typed_metadata"] = metadata
    return result


def _producer_failure_index(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project only registered producer structures, before background truncation.

    A pointer is relative to the immutable outcome payload, not a new authority.
    Unknown observations stay unknown; no search of arbitrary nested JSON/logs.
    """
    execution = payload.get("execution_record") or {}
    execution = execution if isinstance(execution, Mapping) else {}
    rows = []
    def add(raw, pointer, *, code="", stage="validation"):
        raw = dict(raw) if isinstance(raw, Mapping) else {"message": str(raw)}
        rows.append({"source_pointer": pointer, "stage": stage,
            "code": str(raw.get("code") or raw.get("error_code") or raw.get("reason_code") or code or raw.get("message") or "UNKNOWN")[:1200],
            **{("detail" if k == "message" else k): _bounded_fact_value(raw[k]) for k in
                ("message", "detail", "status", "reason", "parser_id", "validation_source", "field_path", "repair_stage", "formal_target_path", "relative_path", "expected", "actual", "observed_state", "failed_invariant", "evidence_refs", "expected_state_generation", "current_state_generation", "no_partial_mutation") if k in raw},
            "observation_status": "recorded" if any(k in raw for k in ("observed_state", "actual")) else "unknown"})
    # Explicit rejection lists take priority over process rc/status/background.
    for container, pointer in ((execution.get("validation"), "/execution_record/validation"),
            (payload.get("validation_evidence"), "/validation_evidence"),
            (payload.get("validation"), "/validation")):
        if isinstance(container, Mapping):
            for i, error in enumerate(container.get("errors") or []):
                add(error, pointer + f"/errors/{i}")
    for key in ("contract_violations", "missing_evidence", "missing_facts"):
        for i, error in enumerate(payload.get(key) or []):
            add(error, f"/{key}/{i}")
    effect = execution.get("effect_evidence") or {}
    if isinstance(effect, Mapping):
        parser = effect.get("parser_receipt") or {}
        if isinstance(parser, Mapping):
            for i, issue in enumerate(parser.get("issues") or []):
                add(issue, f"/execution_record/effect_evidence/parser_receipt/issues/{i}", stage="execution.effect_parser")
    journal = execution.get("workspace_mutation_attestation") or {}
    mutation_rows = journal.get("receipts", []) if isinstance(journal, Mapping) else []
    pointer = "/execution_record/workspace_mutation_attestation/receipts"
    if not mutation_rows:
        mutation_rows = execution.get("workspace_mutations") or []
        pointer = "/execution_record/workspace_mutations"
    for i, receipt in enumerate(mutation_rows):
        if not isinstance(receipt, Mapping):
            continue
        for j, check in enumerate(receipt.get("postcondition_results") or []):
            if isinstance(check, Mapping) and check.get("status") != "satisfied":
                add(check, f"{pointer}/{i}/postcondition_results/{j}",
                    code="WORKSPACE_POSTCONDITION_UNSATISFIED", stage="execution.attestation")
    for key in ("runtime_output_reconciliation", "completion_report", "completion_gate"):
        container = execution.get(key) if key == "runtime_output_reconciliation" else payload.get(key)
        if not isinstance(container, Mapping):
            continue
        for field in ("violations", "findings", "binding_and_receipt_findings"):
            for i, check in enumerate(container.get(field) or []):
                if isinstance(check, Mapping) and check.get("status") not in {"passed", "satisfied"} and check.get("severity") != "warning":
                    prefix = "/execution_record" if key == "runtime_output_reconciliation" else ""
                    add(check, f"{prefix}/{key}/{field}/{i}", stage=key)
    report = payload.get("completion_report")
    if isinstance(report, Mapping):
        for i, check in enumerate(report.get("predicate_results") or []):
            if not isinstance(check, Mapping) or check.get("passed") is True:
                continue
            parser = check.get("parser_receipt")
            parser = parser if isinstance(parser, Mapping) else {}
            add({**dict(check), "detail": check.get("detail") or parser.get("detail") or ""},
                f"/completion_report/predicate_results/{i}", stage="supervisor.predicate")
    evidence = payload.get("evidence") or {}
    if isinstance(evidence, Mapping):
        for i, health in enumerate(evidence.get("runtime_status") or []):
            if isinstance(health, Mapping) and health.get("ok") is False:
                add({k: health[k] for k in ("error_code", "code", "message", "reason", "status") if k in health},
                    f"/evidence/runtime_status/{i}", code="RUNTIME_HEALTHCHECK_FAILED", stage="installation")
    if payload.get("error_code") or payload.get("failed_invariant"):
        add(payload, "/", code=payload.get("error_code", ""), stage="producer")
    if "expected_state_generation" in payload and "current_state_generation" in payload:
        add(payload, "/", code="COMMIT_CAS_CONFLICT", stage="commit.cas")
    if not rows:
        return {}
    rc = payload.get("return_code", execution.get("returncode"))
    validation = payload.get("validation_evidence") or execution.get("validation") or payload.get("validation") or {}
    return {"schema_version": 1, "checks": rows[:12], "total_rejecting_checks": len(rows),
        "unexpanded_index_range": [12, len(rows)] if len(rows) > 12 else [],
        "unexpanded_pointers": [r["source_pointer"] for r in rows[12:28]],
        "pointer_index_omitted": max(0, len(rows) - 28),
        "process": {"return_code": rc, "started": execution.get("command_started", True if rc is not None else "unknown") if execution else payload.get("command_started", "unknown")},
        "scope": {k: execution[k] for k in ("task_id", "step_id", "action_id", "attempt_id") if k in execution},
        "validation_acceptance": validation.get("status", validation.get("passed", "unknown")) if isinstance(validation, Mapping) else "unknown",
        "commit_acceptance": "not_established"}


def _runtime_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: deepcopy(payload[key])
        for key in _COMMON_RUNTIME_PAYLOAD_FIELDS
        if key in payload and payload[key] not in (None, "", [], {})
    }
    index = _producer_failure_index(payload)
    if index:
        result["producer_failure_index"] = index
    for key in ("contract_violations", "missing_facts", "new_evidence_refs"):
        if payload.get(key) not in (None, "", [], {}):
            result[key] = deepcopy(payload[key])
    diagnostic = _compact_diagnostic(payload.get("diagnostic_record"))
    if diagnostic:
        result["diagnostic_record"] = diagnostic
    budget = _compact_budget(payload.get("budget_receipt"))
    if budget:
        result["budget_receipt"] = budget
    blocked = payload.get("blocked_evidence")
    if isinstance(blocked, Mapping):
        result["blocked_evidence"] = deepcopy(dict(blocked))
    completion_report = _compact_completion_report(
        payload.get("completion_report")
    )
    if completion_report:
        result["completion_report"] = completion_report
    completion_gate = _compact_completion_gate(payload.get("completion_gate"))
    if completion_gate:
        result["completion_gate"] = completion_gate
    missing_effects = _compact_missing_effects(payload.get("missing_effects"))
    if missing_effects:
        result["missing_effects"] = missing_effects
    typed_metadata = _compact_failure_metadata(payload)
    if typed_metadata:
        result["typed_failure_metadata"] = typed_metadata
    for key in _TYPED_RECORD_KEYS:
        compact = _compact_failure_metadata(payload.get(key))
        if compact:
            result[key] = compact
    return result


def _runtime_source(source_kind: str, source: Mapping[str, Any]) -> Mapping[str, Any]:
    _reject_recursive_context(source, (source_kind,))
    projected: dict[str, Any] = {
        "type": str(source.get("type") or ""),
        "summary": str(source.get("summary") or ""),
        "scope": {
            key: value
            for key, value in dict(source.get("scope") or {}).items()
            if key in {
                "task_id",
                "plan_id",
                "plan_version",
                "step_id",
                "state_generation",
                "action_id",
                "attempt_id",
                "validation_id",
            }
            and value not in (None, "")
        },
        "payload": _runtime_payload(dict(source.get("payload") or {})),
    }
    refs = _unique_text(source.get("evidence_refs") or ())
    if refs:
        projected["evidence_refs"] = list(refs)
    return projected


def _task_ref(durable: Mapping[str, Any], task_id: str) -> TaskSpecRef:
    current = dict(durable.get("current_task") or {})
    try:
        spec = canonical_task_spec_for_state(durable)
    except Exception as exc:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", f"Recovery TaskSpec: {type(exc).__name__}"
        ) from exc
    if not task_spec_is_initialized(spec):
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery TaskSpec is not initialized"
        )
    spec_task_id = str(spec.get("task_id") or "")
    if spec_task_id and spec_task_id != task_id:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery TaskSpec task_id"
        )
    revision = task_spec_revision(spec)
    semantic_hash = task_semantic_hash(spec)
    record_hash = task_record_hash(spec)
    return TaskSpecRef(
        schema_version=1,
        task_id=task_id,
        task_revision=revision,
        task_semantic_hash=semantic_hash,
        task_record_hash=record_hash,
        task_spec_ref=str(
            current.get("task_spec_ref")
            or current.get("task_snapshot_ref")
            or ""
        ),
    )


def _plan_ref(durable: Mapping[str, Any], task_ref: TaskSpecRef) -> PlanSpecRef:
    task_id = task_ref.task_id
    current = dict(durable.get("current_plan") or {})
    spec = current.get("plan_spec")
    if not isinstance(spec, Mapping) or not spec:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec is not materialized"
        )
    try:
        revisions = {
            int(value)
            for value in (
                current.get("plan_revision"),
                spec.get("plan_revision"),
                durable.get("plan_revision"),
            )
            if value not in (None, "")
        }
    except (TypeError, ValueError) as exc:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec revision is invalid"
        ) from exc
    if len(revisions) > 1:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec revision mismatch"
        )
    plan_revision = next(iter(revisions), 0)
    if plan_revision < 1:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec revision is invalid"
        )
    plan_id = str(spec.get("plan_id") or durable.get("plan_id") or "")
    plan_version = str(
        spec.get("plan_version")
        or durable.get("plan_version")
        or ""
    )
    if not plan_id or not plan_version:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec identity is incomplete"
        )
    if durable.get("plan_id") and str(durable.get("plan_id")) != plan_id:
        raise StageContractError("STAGE_SCOPE_MISMATCH", "Recovery PlanSpec plan_id")
    if durable.get("plan_version") and str(durable.get("plan_version")) != plan_version:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery PlanSpec plan_version"
        )
    semantic_hash = "sha256:" + semantic_plan_hash(dict(spec)).removeprefix("sha256:")
    record_hash = plan_record_hash(dict(spec))
    declared_semantic = str(current.get("plan_semantic_hash") or "")
    declared_record = str(current.get("plan_record_hash") or "")
    if declared_semantic and declared_semantic != semantic_hash:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec semantic hash mismatch"
        )
    if declared_record and declared_record != record_hash:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec record hash mismatch"
        )
    base_revision = int(
        current.get("base_task_revision")
        or spec.get("generated_from_task_revision")
        or 0
    )
    base_hash = str(
        current.get("base_task_semantic_hash")
        or spec.get("base_task_semantic_hash")
        or ""
    )
    if base_revision and base_revision != task_ref.task_revision:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec task revision is stale"
        )
    if base_hash and base_hash != task_ref.task_semantic_hash:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery PlanSpec TaskSpec hash is stale"
        )
    return PlanSpecRef(
        schema_version=1,
        task_id=task_id,
        plan_id=plan_id,
        plan_version=plan_version,
        plan_revision=plan_revision,
        plan_semantic_hash=semantic_hash,
        plan_record_hash=record_hash,
        plan_snapshot_ref=str(
            current.get("plan_snapshot_ref")
            or (durable.get("current_approval") or {}).get("plan_snapshot_ref")
            or ""
        ),
    )


def _selected_skill_refs(durable: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_state = dict(durable.get("task_state") or {})
    candidates = (
        (task_state.get("skill_access_state") or {}).get("selected_skill_refs"),
        task_state.get("selected_skill_refs"),
        (durable.get("skill_access_state") or {}).get("selected_skill_refs"),
        durable.get("selected_skill_refs"),
    )
    for value in candidates:
        if isinstance(value, (list, tuple)) and value:
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _skill_bundle(
    durable: Mapping[str, Any], task_revision: int, step_id: str
) -> SelectedSkillBundle:
    parsed: list[SelectedSkillRef] = []
    for ref in _selected_skill_refs(durable):
        status = str(ref.get("status") or "active").lower()
        scope = str(ref.get("scope") or "task").lower()
        target = str(ref.get("target_step_id") or "")
        if status != "active" or (scope == "step" and target != step_id):
            continue
        _reject_recursive_context(
            ref, ("selected_skill_refs", str(ref.get("skill_id") or "<unknown>"))
        )
        parsed.append(SelectedSkillRef(
            schema_version=1,
            skill_id=str(ref.get("skill_id") or ""),
            snapshot_id=str(ref.get("snapshot_id") or ""),
            content_hash=str(
                ref.get("content_hash") or ref.get("document_digest") or ""
            ),
            scope=scope,
            source_path=str(ref.get("source_path") or ""),
            target_step_id=target if scope == "step" else "",
        ))
    wire = [item.to_dict() for item in parsed]
    return SelectedSkillBundle(
        schema_version=1,
        bundle_id="recovery-skills-" + _hash({
            "task_revision": task_revision,
            "skills": wire,
        }),
        task_revision=task_revision,
        skills=tuple(parsed),
        selection_summary=(
            "Active immutable Skill snapshots applicable to the failed step."
            if parsed
            else "No immutable Skill snapshot is bound to this step."
        ),
    )


def _source_scope(source_kind: str, source: Mapping[str, Any]) -> dict[str, Any]:
    if source_kind.endswith("outcome"):
        return dict(source.get("scope") or {})
    return {
        key: source[key]
        for key in ("task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id")
        if source.get(key) not in (None, "")
    }


def _source_payload(source_kind: str, source: Mapping[str, Any]) -> dict[str, Any]:
    return (
        dict(source.get("payload") or {})
        if source_kind.endswith("outcome")
        else dict(source)
    )


def _assert_scope(
    task_id: str,
    durable: Mapping[str, Any],
    source_kind: str,
    source: Mapping[str, Any],
) -> str:
    scope = _source_scope(source_kind, source)
    payload = _source_payload(source_kind, source)
    record = (
        dict(payload.get("execution_record") or {})
        if isinstance(payload.get("execution_record"), Mapping)
        else {}
    )
    validation = (
        dict(payload.get("validation_evidence") or {})
        if isinstance(payload.get("validation_evidence"), Mapping)
        else {}
    )
    diagnostic = (
        dict(payload.get("diagnostic_record") or {})
        if isinstance(payload.get("diagnostic_record"), Mapping)
        else {}
    )

    def require_one(field: str, *values: Any) -> str:
        observed = {str(value) for value in values if value not in (None, "")}
        if len(observed) > 1:
            raise StageContractError(
                "STAGE_SCOPE_MISMATCH", f"Recovery source {field}"
            )
        return next(iter(observed), "")

    require_one(
        "task_id",
        task_id,
        durable.get("task_id"),
        scope.get("task_id"),
        record.get("task_id"),
        validation.get("task_id"),
        diagnostic.get("task_id"),
    )
    require_one(
        "plan_id",
        durable.get("plan_id"),
        scope.get("plan_id"),
        record.get("plan_id"),
        validation.get("plan_id"),
        diagnostic.get("plan_id"),
    )
    require_one(
        "plan_version",
        durable.get("plan_version"),
        scope.get("plan_version"),
        record.get("plan_version"),
        validation.get("plan_version"),
        diagnostic.get("plan_version"),
    )
    require_one(
        "action_id",
        scope.get("action_id"),
        payload.get("action_id"),
        record.get("action_id"),
        validation.get("action_id"),
        diagnostic.get("action_id"),
    )
    require_one(
        "attempt_id",
        scope.get("attempt_id"),
        payload.get("attempt_id"),
        record.get("attempt_id"),
        validation.get("attempt_id"),
        diagnostic.get("attempt_id"),
    )
    durable_step = str(durable.get("current_step_id") or "")
    step_id = require_one(
        "step_id",
        durable_step,
        scope.get("step_id"),
        payload.get("step_id"),
        record.get("step_id"),
        validation.get("step_id"),
        diagnostic.get("step_id"),
    )
    if not step_id:
        raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery step_id")
    return step_id


def _current_step(durable: Mapping[str, Any], step_id: str) -> dict[str, Any]:
    plan_spec = dict((durable.get("current_plan") or {}).get("plan_spec") or {})
    source = next((
        item
        for item in plan_spec.get("steps", ()) or ()
        if isinstance(item, Mapping) and str(item.get("step_id") or "") == step_id
    ), {})
    if not source:
        raise StageContractError(
            "STAGE_REFERENCE_INVALID", "Recovery current step is absent from PlanSpec"
        )
    result = {
        key: source[key]
        for key in _CURRENT_STEP_FIELDS
        if key in source and source[key] not in (None, "", [], {})
    }
    result["step_id"] = step_id
    if "skill_ids" not in result and source.get("selected_skill_id"):
        result["skill_ids"] = [str(source["selected_skill_id"])]
    return result


def _dependency_ids(step: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    for item in step.get("dependencies", ()) or ():
        if isinstance(item, Mapping):
            value = item.get("step_id") or item.get("dependency_step_id") or item.get("id")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _row_refs(value: Any, *keys: str) -> tuple[str, ...]:
    result: list[str] = []
    items = value if isinstance(value, (list, tuple)) else ()
    for item in items:
        if isinstance(item, Mapping):
            candidate = next((item.get(key) for key in keys if item.get(key)), None)
        else:
            candidate = item
        text = str(candidate or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _dependency_outcomes(
    durable: Mapping[str, Any], current_step: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    steps = {
        str(item.get("step_id") or ""): item
        for item in durable.get("steps", ()) or ()
        if isinstance(item, Mapping) and item.get("step_id")
    }
    receipts = dict((durable.get("task_state") or {}).get("typed_step_commit_receipts") or {})
    result: list[Mapping[str, Any]] = []
    for dependency_id in _dependency_ids(current_step):
        step = dict(steps.get(dependency_id) or {})
        status = str(step.get("status") or "unknown")
        row: dict[str, Any] = {
            "step_id": dependency_id,
            "status": status,
            "summary": str(
                step.get("outcome_summary")
                or step.get("summary")
                or step.get("title")
                or f"Dependency status: {status}."
            ),
        }
        matching_receipt = next((
            (receipt_id, receipt)
            for receipt_id, receipt in receipts.items()
            if isinstance(receipt, Mapping)
            and str(
                ((receipt.get("outcome") or {}).get("payload") or {}).get(
                    "completed_step_id"
                )
                or ""
            ) == dependency_id
        ), None)
        if matching_receipt:
            row["outcome_ref"] = str(matching_receipt[0])
        evidence_refs = _unique_text(step.get("evidence_refs") or ())
        if evidence_refs:
            row["evidence_refs"] = list(evidence_refs)
        artifact_refs = _row_refs(
            step.get("artifacts"), "artifact_id", "content_ref", "path", "id"
        )
        if artifact_refs:
            row["artifact_refs"] = list(artifact_refs)
        binding_refs = _row_refs(
            step.get("accepted_bindings"), "binding_id", "value_ref", "name"
        )
        if binding_refs:
            row["binding_refs"] = list(binding_refs)
        result.append(freeze(row))
    return tuple(result)


def _evidence_refs(source: Mapping[str, Any]) -> tuple[str, ...]:
    payload = dict(source.get("payload") or {})
    execution = payload.get("execution_record")
    diagnostic = payload.get("diagnostic_record")
    validation = payload.get("validation_evidence")
    return _unique_text(
        source.get("evidence_refs") or (),
        execution.get("evidence_refs") if isinstance(execution, Mapping) else (),
        execution.get("execution_id") if isinstance(execution, Mapping) else "",
        diagnostic.get("evidence_refs") if isinstance(diagnostic, Mapping) else (),
        diagnostic.get("evidence_ref") if isinstance(diagnostic, Mapping) else "",
        diagnostic.get("execution_id") if isinstance(diagnostic, Mapping) else "",
        validation.get("evidence_refs") if isinstance(validation, Mapping) else (),
        validation.get("validation_id") if isinstance(validation, Mapping) else "",
    )


def _failure_identity(
    source_kind: str,
    source: Mapping[str, Any],
) -> tuple[str, str, str, bool]:
    payload = _source_payload(source_kind, source)
    outcome_type = str(source.get("type") or payload.get("failure_type") or "recovery_fact")
    if source_kind == "execute_outcome":
        if outcome_type == "execution_failed":
            record = dict(payload.get("execution_record") or {})
            validation_failed = any(
                isinstance(value, Mapping) and value.get("passed") is False
                for value in (record.get("validation"), payload.get("validation"), payload.get("validation_evidence"))
            )
            return (
                outcome_type,
                str(payload.get("failure_owner") or "GENERATED_ACTION"),
                str(record.get("failure_code") or record.get("error_code")
                    or payload.get("failure_code") or payload.get("error_code")
                    or ("VALIDATION_FAILED" if validation_failed else "EXECUTION_FAILED")),
                True,
            )
        if outcome_type == "diagnostic_completed":
            return outcome_type, "DIAGNOSTIC", "DIAGNOSTIC_EVIDENCE_RECORDED", True
        return (
            outcome_type,
            str(payload.get("violation_owner") or "INTERNAL_PROTOCOL"),
            str(payload.get("violation_code") or "PRE_EXECUTION_REJECTED"),
            True,
        )
    if source_kind in {"verification_outcome", "completion_gate_outcome"}:
        owner = str(payload.get("failure_owner") or "VERIFICATION")
        code = str(payload.get("error_code") or {
            "rejected": "VERIFICATION_REJECTED",
            "incomplete_evidence": "VERIFICATION_EVIDENCE_INCOMPLETE",
            "diagnostic_evidence_recorded": "DIAGNOSTIC_EVIDENCE_RECORDED",
            "verification_error": "VERIFICATION_ERROR",
            "harness_error": "VERIFICATION_HARNESS_ERROR",
        }.get(outcome_type, "VERIFICATION_FAILED"))
        # Match the existing TypedRecovery disposition table: only
        # verification_error is terminal internal protocol; other
        # Verification failures enter a bounded corrective path.
        return outcome_type, owner, code, outcome_type != "verification_error"
    if source_kind == "commit_step_outcome":
        if outcome_type == "commit_conflict":
            return outcome_type, "PLAN", "COMMIT_CONFLICT", True
        return (
            outcome_type,
            "INTERNAL_PROTOCOL",
            str(payload.get("reason_code") or "COMMIT_CONTRACT_REJECTED"),
            False,
        )
    if source_kind == "install_subagent_outcome":
        return (
            outcome_type,
            "RUNTIME_CAPABILITY",
            str(payload.get("reason_code") or "INSTALLATION_FAILED"),
            True,
        )

    raise StageContractError("RECOVERY_CAUSAL_SOURCE_UNKNOWN", source_kind)


@dataclass(frozen=True)
class RecoveryProjection:
    """Validated input consumed by ``TypedRecovery.run``."""

    source_kind: str
    source: Mapping[str, Any]
    source_fingerprint: str
    failure_packet: FailurePacket
    recovery_seed: RecoverySeed


def _content_ref(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:sha256:{digest}"


def _diagnostic_facts(
    source_kind: str, source: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    payload = _source_payload(source_kind, source)
    execution = (
        dict(payload.get("execution_record") or {})
        if isinstance(payload.get("execution_record"), Mapping)
        else {}
    )
    return_code = (
        payload.get("return_code")
        if payload.get("return_code") is not None
        else execution.get("returncode")
    )
    facts: list[Mapping[str, Any]] = []
    fact_fingerprints: set[str] = set()

    def add_fact(
        *,
        kind: str,
        summary: str,
        metadata: Mapping[str, Any],
        fact_id: str = "",
    ) -> None:
        if len(facts) >= 16:
            return
        compact = _compact_failure_metadata(metadata)
        for key in (
            "issue_id", "code", "affected_resource", "formal_path",
            "observed_path", "receipt_reason", "evidence_refs",
            "return_code", "status",
        ):
            if key in metadata and key not in compact:
                compact[key] = deepcopy(metadata[key])
        if kind == "result_realizations" and isinstance(metadata.get("result_realizations"), Mapping):
            compact["result_realizations"] = deepcopy(dict(metadata["result_realizations"]))
        identity = {
            "kind": str(kind or "typed_failure"),
            "summary": str(summary or "Typed failure fact.")[:1000],
            "metadata": compact,
        }
        fingerprint = _hash(identity)
        if fingerprint in fact_fingerprints:
            return
        fact_fingerprints.add(fingerprint)
        row: dict[str, Any] = {
            "fact_id": str(
                fact_id
                or compact.get("issue_id")
                or f"failure-fact-{fingerprint}"
            )[:256],
            **identity,
        }
        refs = compact.get("evidence_refs")
        if isinstance(refs, list) and refs:
            row["evidence_ref"] = str(refs[0])
        facts.append(row)

    index = _producer_failure_index(payload)
    if index:
        # Keep the compact owner-produced index intact; generic metadata
        # whitelisting must not erase the first cause again.
        facts.append({"fact_id": "producer-rejecting-checks", "kind": "producer_failure_index",
            "summary": index["checks"][0]["code"], "metadata": index})
    if return_code is not None:
        add_fact(
            fact_id="failure-return-code",
            kind="return_code",
            summary=f"Process return code: {return_code}",
            metadata={"return_code": return_code},
        )
    action = execution.get("action_contract")
    action = dict(action) if isinstance(action, Mapping) else {}
    contracts = action.get("controller_output_bindings")
    candidates = execution.get("observed_output_records")
    if execution.get("preflight_evidence_binding") and not any(
            isinstance(r, Mapping) and r.get("evidence_files") for r in candidates or []):
        # Old checkpoints already own these helper receipts. Expose the same
        # explicit candidate to Recovery without altering the archived result.
        from agent_core.workflow.result_resolution import preflight_result_candidate
        try:
            candidate = preflight_result_candidate(execution)
            if candidate:
                candidates = [*(candidates or []), candidate]
        except (ValueError, OSError, TypeError) as exc:
            add_fact(kind="preflight_registration_evidence", summary="Preflight receipt needs inspection before publication.",
                metadata={"code": str(getattr(exc, "code", "RESULT_PREFLIGHT_EVIDENCE_UNAVAILABLE")), "detail": str(exc)})
    if return_code == 0 and (execution.get("runtime_output_reconciliation") or {}).get("status") == "recovery":
        add_fact(kind="result_registration_pending",
            summary="The command completed with rc=0; formal output acceptance is pending. Inspect current-attempt evidence and prefer result selection/organization plus revalidation over rerunning the helper.",
            metadata={"stage": "post_execution_reconciliation", "return_code": 0})
    if isinstance(contracts, (list, tuple)) or isinstance(candidates, (list, tuple)):
        realizations = {
            "source": "current_execution_record",
            "scope": {key: _bounded_scalar(execution[key], limit=512) for key in
                      ("task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id", "execution_id")
                      if key in execution},
            "authority": "recorded_contracts_and_observations; no_correspondence_or_acceptance_inferred",
        }
        for target, rows, fields in (
            ("declared_results", contracts, ("artifact_id", "semantic_handle", "role", "description", "required",
                "expected_path", "path", "constraint_origin", "path_policy", "output_root", "artifact_kind", "format", "validation_contract_id")),
            ("input_bindings", action.get("controller_input_bindings"), ("artifact_id", "semantic_handle", "role", "path", "canonical_path", "format")),
            ("observed_candidates", candidates, ("path", "attempt_id", "current_attempt_owned", "exists",
                "artifact_kind", "format", "representation", "size", "content_valid", "content_prefix_sha256")),
        ):
            if isinstance(rows, (list, tuple)):
                realizations[target] = [{key: _bounded_fact_value(raw[key]) for key in fields if key in raw}
                    for raw in rows[:12] if isinstance(raw, Mapping)]
                if len(rows) > 12:
                    realizations[target + "_omitted"] = len(rows) - 12
        reconciliation = execution.get("runtime_output_reconciliation")
        if isinstance(reconciliation, Mapping):
            reconciliation_facts = {}
            if "status" in reconciliation:
                reconciliation_facts["status"] = _bounded_scalar(reconciliation["status"])
            violations = reconciliation.get("violations")
            if isinstance(violations, (list, tuple)):
                reconciliation_facts["violations"] = [
                    {**{key: _bounded_fact_value(raw[key]) for key in
                        ("code", "artifact_id", "path", "expected_path", "observed_path", "severity", "detail") if key in raw},
                     **({"detail": _bounded_scalar(raw["message"], limit=1200)}
                        if "message" in raw and "detail" not in raw else {})}
                    for raw in violations[:12] if isinstance(raw, Mapping)
                ]
                if len(violations) > 12:
                    reconciliation_facts["violations_omitted"] = len(violations) - 12
            realizations["reconciliation"] = reconciliation_facts
        add_fact(kind="result_realizations", summary="Recorded result contracts and current execution output candidates.",
                 metadata={"result_realizations": realizations})
    status = payload.get("status")
    if status not in (None, ""):
        add_fact(
            fact_id="failure-status",
            kind="status",
            summary=f"Producer status: {status}",
            metadata={"status": status},
        )

    direct_metadata = _compact_failure_metadata(payload)
    if direct_metadata:
        add_fact(
            kind="typed_failure",
            summary=str(
                direct_metadata.get("code") or "Typed producer failure fact."
            ),
            metadata=direct_metadata,
        )

    for index, raw in enumerate(payload.get("contract_violations") or ()):
        if isinstance(raw, Mapping):
            metadata = _compact_failure_metadata(raw)
            summary = str(
                raw.get("message") or raw.get("summary")
                or metadata.get("code") or "Contract violation."
            )
        else:
            summary = str(raw or "Contract violation.")
            metadata = {"code": summary}
        add_fact(
            fact_id=str(metadata.get("issue_id") or f"contract-violation-{index}"),
            kind="contract_violation",
            summary=summary,
            metadata=metadata,
        )

    report = payload.get("completion_report")
    if isinstance(report, Mapping):
        receipt_findings_by_issue = {
            str(item.get("issue_id") or ""): item
            for item in report.get("binding_and_receipt_findings") or ()
            if isinstance(item, Mapping) and str(item.get("issue_id") or "")
        }
        for index, raw in enumerate(report.get("findings") or ()):
            if not isinstance(raw, Mapping) or str(
                raw.get("status") or ""
            ) == "passed":
                continue
            metadata = _compact_failure_metadata(
                raw,
                receipt_findings_by_issue.get(
                    str(raw.get("issue_id") or ""),
                    {},
                ),
            )
            affected_field = str(raw.get("affected_field") or "")
            if "path" in affected_field.lower():
                if raw.get("expected") not in (None, ""):
                    metadata.setdefault(
                        "formal_path", _bounded_scalar(raw.get("expected"))
                    )
                if raw.get("actual") not in (None, ""):
                    metadata.setdefault(
                        "observed_path", _bounded_scalar(raw.get("actual"))
                    )
            add_fact(
                fact_id=str(
                    metadata.get("issue_id")
                    or raw.get("issue_id")
                    or f"completion-finding-{index}"
                ),
                kind=str(raw.get("nature") or "completion_finding"),
                summary=str(
                    raw.get("code") or raw.get("status")
                    or "Completion finding requires repair."
                ),
                metadata=metadata,
            )
        for index, raw in enumerate(
            report.get("binding_and_receipt_findings") or ()
        ):
            if not isinstance(raw, Mapping) or str(
                raw.get("status") or ""
            ) == "passed":
                continue
            metadata = _compact_failure_metadata(raw)
            add_fact(
                fact_id=str(
                    metadata.get("issue_id")
                    or raw.get("issue_id")
                    or f"binding-receipt-finding-{index}"
                ),
                kind="binding_and_receipt",
                summary=str(
                    raw.get("code") or metadata.get("receipt_reason")
                    or "Binding or receipt finding requires repair."
                ),
                metadata=metadata,
            )
        for index, raw in enumerate(report.get("predicate_results") or ()):
            if not isinstance(raw, Mapping) or raw.get("passed") is True:
                continue
            metadata = _compact_failure_metadata(raw)
            add_fact(
                fact_id=str(
                    metadata.get("issue_id")
                    or raw.get("result_id")
                    or f"completion-predicate-{index}"
                ),
                kind="completion_predicate",
                summary=str(
                    raw.get("reason_code") or raw.get("status")
                    or "Completion predicate failed."
                ),
                metadata=metadata,
            )

    def add_missing_effects(value: Any, *, prefix: str) -> None:
        if not isinstance(value, Mapping):
            return
        for effect_kind in sorted(value, key=str)[:12]:
            raw_items = value[effect_kind]
            items = raw_items if isinstance(raw_items, (list, tuple)) else (raw_items,)
            for index, item in enumerate(items[:12]):
                metadata = (
                    _compact_failure_metadata(item)
                    if isinstance(item, Mapping)
                    else {"affected_resource": _bounded_scalar(item, limit=512)}
                )
                metadata.setdefault("code", "MISSING_REQUIRED_EFFECT")
                metadata.setdefault("issue_id", f"{prefix}:{effect_kind}:{index}")
                add_fact(
                    fact_id=str(metadata["issue_id"]),
                    kind="missing_effect",
                    summary=f"Missing required effect {effect_kind}: {metadata.get('affected_resource', '<unspecified>')}",
                    metadata=metadata,
                )

    add_missing_effects(payload.get("missing_effects"), prefix="missing-effect")
    gate = payload.get("completion_gate")
    if isinstance(gate, Mapping):
        add_missing_effects(
            gate.get("missing_effects"), prefix="completion-gate-missing-effect"
        )

    for key in _TYPED_RECORD_KEYS:
        raw = payload.get(key)
        if not isinstance(raw, Mapping):
            continue
        metadata = _compact_failure_metadata(raw)
        if not metadata:
            continue
        add_fact(
            kind="binding" if "binding" in key else "receipt",
            summary=str(
                metadata.get("code") or metadata.get("receipt_reason")
                or f"Typed {key} failure."
            ),
            metadata=metadata,
        )
    return tuple(freeze(item) for item in facts)


def verification_causal_signature(source_kind: str, source: Mapping[str, Any]) -> str:
    """Identity of a scoped Supervisor assessment, never a general error deduper.

    Only the two Supervisor producer kinds have the report/generation lifecycle
    contract. Other admission, tool and validation producers keep their original
    identity. The original outcome is retained byte-for-byte in its archive.
    """
    if source_kind not in {"verification_outcome", "completion_gate_outcome"}:
        return ""
    payload = source.get("payload") or {}
    report = _compact_completion_report(payload.get("completion_report"))
    scope = source.get("scope") or {}
    keys = ("task_id", "plan_id", "plan_version", "step_id", "action_id", "attempt_id")
    if not report.get("validation_digest") or not all(scope.get(key) for key in keys):
        return ""
    report_scope = report.get("scope") or {}
    if any(str(report_scope.get(key) or "") != str(scope[key]) for key in keys):
        return ""
    # These two report-local fields describe publication, not the assessed
    # execution. Do not drop similarly named fields from nested facts.
    report.pop("report_id", None)
    report["scope"].pop("state_generation", None)
    return _hash({
        "producer": source_kind, "type": source.get("type"),
        "scope": {key: scope[key] for key in keys},
        "report": report,
        "gate": _compact_completion_gate(payload.get("completion_gate")),
        "failure_owner": payload.get("failure_owner"),
        "error_code": payload.get("error_code"),
        "contract_violations": payload.get("contract_violations"),
        "missing_evidence": payload.get("missing_evidence"),
    })


def inherited_verification_cause(
    source_kind: str, source: Mapping[str, Any], durable: Mapping[str, Any],
) -> dict[str, str]:
    """Resolve a matching trusted persisted producer packet in the current Plan.

    Publication changes may append a packet version but cannot create a fresh
    incident/budget. A changed attempt or assessed fact does not match.
    """
    signature = verification_causal_signature(source_kind, source)
    if not signature:
        return {}
    registry = (durable.get("task_state") or {}).get("causal_failure_packets") or {}
    matches = []
    for ref, record in registry.items():
        if not isinstance(record, Mapping) or record.get("source_kind") != source_kind:
            continue
        if verification_causal_signature(source_kind, record.get("failure_source") or {}) != signature:
            continue
        packet = _failure_packet_from_record(str(ref), record)
        if _content_ref("failure-source", record.get("failure_source") or {}) != record.get("failure_source_ref"):
            raise StageContractError("RECOVERY_CAUSAL_SOURCE_REF_MISMATCH", str(ref))
        scope = source.get("scope") or {}
        if (packet.source != source_kind or packet.attempt_ref != scope.get("attempt_id")
                or packet.action_ref != scope.get("action_id") or packet.step_id != scope.get("step_id")):
            raise StageContractError("RECOVERY_CAUSAL_PACKET_SCOPE_MISMATCH", str(ref))
        if not packet.incident_id:
            continue
        if (packet.plan_spec_ref.plan_id != str(durable.get("plan_id") or "")
                or packet.plan_spec_ref.plan_version != str(durable.get("plan_version") or "")
                or packet.plan_spec_ref.plan_revision != int(durable.get("plan_revision") or 1)):
            continue
        _validate_incident_lineage(packet, registry)
        matches.append((packet.packet_version, str(ref), packet))
    if not matches:
        return {}
    # If historical buggy publications already forked an incident, retain the
    # first trusted cause. Normal new packets resolve to its latest head.
    first = matches[0][2].incident_id
    _, ref, packet = max((row for row in matches if row[2].incident_id == first), key=lambda row: row[:2])
    return {"incident_id": packet.incident_id,
            "failure_fingerprint": packet.failure_fingerprint,
            "causal_parent_packet_ref": ref}


def _failure_incident_id(
    *,
    source_kind: str,
    payload: Mapping[str, Any],
    task_id: str,
    step_id: str,
    plan_ref: PlanSpecRef,
    failure_type: str,
    failure_owner: str,
    error_code: str,
    failure_fingerprint: str,
) -> str:
    """Return the stable causal incident sealed into a new schema-1 packet.

    An explicit incident identity is accepted only from the already validated
    producer outcome.  Otherwise the identity is derived from immutable plan
    scope and the producer's causal failure fingerprint.  Attempt/action IDs
    are deliberately excluded so later evidence for the same causal failure
    can become a new packet version instead of a new incident.
    """

    explicit = str(payload.get("incident_id") or "").strip()
    if explicit:
        return explicit
    return "incident-" + _hash({
        "task_id": task_id,
        "plan_id": plan_ref.plan_id,
        "plan_version": plan_ref.plan_version,
        "plan_revision": plan_ref.plan_revision,
        "step_id": step_id,
        "source_kind": source_kind,
        "failure_type": failure_type,
        "failure_owner": failure_owner,
        "error_code": error_code,
        "failure_fingerprint": failure_fingerprint,
    })


def _helper_binding_facts(execution, durable):
    if not isinstance(execution, Mapping):
        return ()
    current = execution.get("action_contract") or {}
    decision = execution.get("recovery_decision_id") or current.get("recovery_decision_id")
    prior_receipt = ((durable.get("task_state") or {}).get("typed_recovery_receipts") or {}).get(decision) or {}
    scope = (prior_receipt.get("outcome") or {}).get("scope") or {}
    provenance = durable.get("provenance_state") or {}
    previous = (provenance.get("actions") or {}).get(scope.get("action_id")) or {}
    previous = previous.get("normalized_action") or {}
    if not current.get("formal_skill_resources") and not previous.get("formal_skill_resources"):
        return ()
    def view(action, aid, attempt, binding=None):
        return {"action_id": aid, "attempt_id": attempt,
            "helper_handles": list(action.get("skill_resource_refs") or []),
            "registered_resources": [r.get("handle") for r in action.get("formal_skill_resources") or []],
            "evidence_binding_declared": bool((action.get("execution_policy_manifest") or {}).get("evidence_binding")),
            "injected_evidence_directory": (binding or {}).get("canonical_path", ""),
            "policy_manifest_hash": (action.get("execution_policy_manifest") or {}).get("manifest_hash", "")}
    metadata = {"current": view(current, execution.get("action_id"), execution.get("attempt_id"), execution.get("preflight_evidence_binding")),
        "model_source_ref": execution.get("model_source_ref", ""),
        "approved_source_ref": execution.get("code_path", ""),
        "source_distinction": "model draft precedes framework materialization; approved source is the executed definition"}
    if previous:
        metadata["previous"] = view(previous, scope.get("action_id"), scope.get("attempt_id"))
        attempt = (provenance.get("attempts") or {}).get(scope.get("attempt_id")) or {}
        path = (attempt.get("executed_script") or {}).get("path") or attempt.get("executed_script_path")
        if path:
            from pathlib import Path
            ref = Path(path).parent / "preflight_evidence_binding.json"
            metadata["previous"]["binding_receipt_ref"] = str(ref)
            try:
                if not ref.is_symlink() and ref.stat().st_size < 65536:
                    binding = json.loads(ref.read_text())
                    if (binding.get("action_id") == scope.get("action_id") and binding.get("attempt_id") == scope.get("attempt_id")
                            and binding.get("policy_manifest_hash") == metadata["previous"]["policy_manifest_hash"]):
                        metadata["previous"]["injected_evidence_directory"] = binding.get("canonical_path", "")
            except (OSError, ValueError):
                pass
    return (freeze({"fact_id": "helper-execution-definition", "kind": "helper_binding_comparison",
        "summary": "Compare the selected helper and actual execution binding before attributing a missing variable to the environment. Resource declarations trigger per-attempt injection; a retry can lose that declaration.",
        "metadata": metadata}),)


def build_causal_failure_record(
    source_kind: str,
    source: Mapping[str, Any],
    durable: Mapping[str, Any],
    *,
    task_id: str = "",
    inspection_refs: tuple[str, ...] = (),
    packet_version: int = 1,
    supersedes_packet_ref: str = "",
    inspection_publication_failures: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build the immutable record owned by one failing producer adapter."""

    raw_source = _validated_source(source_kind, source)
    if not is_recovery_failure_source(source_kind, raw_source):
        raise StageContractError(
            "RECOVERY_CAUSAL_SOURCE_NOT_FAILURE",
            f"{source_kind}:{raw_source.get('type')}",
        )
    resolved_task_id = str(
        task_id
        or (raw_source.get("scope") or {}).get("task_id")
        or durable.get("task_id")
        or ""
    )
    if not resolved_task_id:
        raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery task_id")
    step_id = _assert_scope(
        resolved_task_id, durable, source_kind, raw_source
    )
    task_ref = _task_ref(durable, resolved_task_id)
    plan_ref = _plan_ref(durable, task_ref)
    runtime_source = _runtime_source(source_kind, raw_source)
    raw_payload = _source_payload(source_kind, raw_source)
    payload = _source_payload(source_kind, runtime_source)
    if not payload.get("incident_id"):
        inherited = inherited_verification_cause(source_kind, raw_source, durable)
        if inherited:
            payload = {**payload, **inherited}
            runtime_source = {**runtime_source, "payload": payload}
    failure_type, failure_owner, error_code, retryable = _failure_identity(
        source_kind, raw_source
    )
    scope = _source_scope(source_kind, raw_source)
    execution = raw_payload.get("execution_record")
    attempt_ref = str(
        scope.get("attempt_id")
        or raw_payload.get("attempt_id")
        or (execution.get("attempt_id") if isinstance(execution, Mapping) else "")
        or ""
    )
    action_ref = str(
        scope.get("action_id")
        or raw_payload.get("action_id")
        or (execution.get("action_id") if isinstance(execution, Mapping) else "")
        or ""
    )
    source_outcome_ref = _content_ref("outcome", raw_source)
    source_fingerprint = _hash({"kind": source_kind, "source": raw_source})
    fingerprint = str(
        payload.get("failure_fingerprint")
        or payload.get("candidate_fingerprint")
        or source_fingerprint
    )
    summary = str(
        payload.get("diagnosis")
        or payload.get("reason_code")
        or payload.get("violation_code")
        or runtime_source.get("summary")
        or error_code
    )
    evidence_refs = _evidence_refs(raw_source)
    incident_id = _failure_incident_id(
        source_kind=source_kind,
        payload=payload,
        task_id=resolved_task_id,
        step_id=step_id,
        plan_ref=plan_ref,
        failure_type=failure_type,
        failure_owner=failure_owner,
        error_code=error_code,
        failure_fingerprint=fingerprint,
    )
    packet = FailurePacket(
        schema_version=1,
        failure_id="failure-" + source_fingerprint,
        task_id=resolved_task_id,
        step_id=step_id,
        failure_type=failure_type,
        failure_owner=failure_owner,
        error_code=error_code,
        summary=summary,
        retryable=retryable,
        evidence_refs=evidence_refs,
        source=source_kind,
        source_outcome_ref=source_outcome_ref,
        plan_spec_ref=plan_ref,
        attempt_ref=attempt_ref,
        action_ref=action_ref,
        failure_fingerprint=fingerprint,
        diagnostic_facts=(
            *_diagnostic_facts(source_kind, raw_source),
            *_helper_binding_facts(execution, durable),
            *(freeze({
                "fact_id": "inspection-publication-" + str(index),
                "kind": "inspection_publication_failure",
                "summary": str(failure.get("code") or "INSPECTION_PUBLICATION_FAILED"),
                "metadata": dict(failure),
            }) for index, failure in enumerate(inspection_publication_failures)),
        ),
        inspection_refs=_unique_text(source_outcome_ref, inspection_refs),
        incident_id=incident_id,
        packet_version=packet_version,
        supersedes_packet_ref=supersedes_packet_ref,
    )
    packet_wire = packet.to_dict()
    packet_ref = _content_ref("failure-packet", packet_wire)
    failure_source = deepcopy(runtime_source)
    if inspection_publication_failures:
        failure_source["payload"]["inspection_publication_failures"] = [
            deepcopy(dict(item)) for item in inspection_publication_failures
        ]
    return {
        "schema_version": 1,
        "packet_ref": packet_ref,
        "source_kind": source_kind,
        "source_outcome_ref": source_outcome_ref,
        "source_fingerprint": source_fingerprint,
        "incident_id": packet.incident_id,
        "packet_version": packet.packet_version,
        "supersedes_packet_ref": packet.supersedes_packet_ref,
        "failure_source_ref": _content_ref("failure-source", failure_source),
        "failure_packet": packet_wire,
        "failure_source": failure_source,
    }


def _failure_packet_from_record(
    packet_ref: str,
    record: Mapping[str, Any],
) -> FailurePacket:
    """Validate one stored packet and its additive lineage index fields."""

    resolved = dict(record or {})
    if str(resolved.get("packet_ref") or "") != str(packet_ref or ""):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_REF_MISMATCH", str(packet_ref or "")
        )
    packet = FailurePacket.from_mapping(resolved.get("failure_packet") or {})
    if _content_ref("failure-packet", packet.to_dict()) != str(packet_ref or ""):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_REF_MISMATCH", str(packet_ref or "")
        )
    if packet.incident_id:
        indexed_version = resolved.get("packet_version")
        if type(indexed_version) is not int:
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_LINEAGE_MISMATCH",
                str(packet_ref or ""),
            )
        indexed = {
            "incident_id": str(resolved.get("incident_id") or ""),
            "packet_version": indexed_version,
            "supersedes_packet_ref": str(
                resolved.get("supersedes_packet_ref") or ""
            ),
        }
        expected = {
            "incident_id": packet.incident_id,
            "packet_version": packet.packet_version,
            "supersedes_packet_ref": packet.supersedes_packet_ref,
        }
        if indexed != expected:
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_LINEAGE_MISMATCH",
                str(packet_ref or ""),
            )
    elif any(
        resolved.get(field) not in (None, "", 0)
        for field in (
            "incident_id", "packet_version", "supersedes_packet_ref",
        )
    ):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_LINEAGE_MISMATCH",
            str(packet_ref or ""),
        )
    return packet


def _failure_event_fingerprint(record: Mapping[str, Any]) -> str:
    """Identify causal evidence independently of its incident lineage shell."""

    packet = deepcopy(dict(record.get("failure_packet") or {}))
    for field in (
        "incident_id", "packet_version", "supersedes_packet_ref",
    ):
        packet.pop(field, None)
    return _hash({
        "source_kind": str(record.get("source_kind") or ""),
        "source_fingerprint": str(record.get("source_fingerprint") or ""),
        "failure_packet": packet,
        "failure_source_ref": str(record.get("failure_source_ref") or ""),
    })


def _validate_incident_lineage(
    packet: FailurePacket,
    registry: Mapping[str, Any],
) -> None:
    """Validate the complete monotonic parent chain for one new-style packet."""

    current = packet
    seen: set[str] = set()
    while current.supersedes_packet_ref:
        parent_ref = current.supersedes_packet_ref
        if parent_ref in seen:
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_LINEAGE_CYCLE", parent_ref
            )
        seen.add(parent_ref)
        raw_parent = registry.get(parent_ref)
        if not isinstance(raw_parent, Mapping):
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_PARENT_MISSING", parent_ref
            )
        parent = _failure_packet_from_record(parent_ref, raw_parent)
        if (
            parent.incident_id != current.incident_id
            or parent.packet_version + 1 != current.packet_version
        ):
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_LINEAGE_MISMATCH", parent_ref
            )
        current = parent


def _incident_head(
    *,
    task_state: Mapping[str, Any],
    registry: Mapping[str, Any],
    incident_id: str,
) -> tuple[str, FailurePacket | None]:
    """Resolve the exact current head, with a safe schema-1 backfill path."""

    raw_heads = task_state.get("causal_failure_incident_heads") or {}
    if not isinstance(raw_heads, Mapping):
        raise StageContractError(
            "RECOVERY_CAUSAL_INCIDENT_HEAD_INVALID", incident_id
        )
    head_ref = str(raw_heads.get(incident_id) or "")
    if head_ref:
        raw_head = registry.get(head_ref)
        if not isinstance(raw_head, Mapping):
            raise StageContractError(
                "RECOVERY_CAUSAL_INCIDENT_HEAD_MISSING", head_ref
            )
        packet = _failure_packet_from_record(head_ref, raw_head)
        if packet.incident_id != incident_id:
            raise StageContractError(
                "RECOVERY_CAUSAL_INCIDENT_HEAD_MISMATCH", head_ref
            )
        _validate_incident_lineage(packet, registry)
        return head_ref, packet

    # Packets produced before the head index was committed remain usable.
    # Only explicit lineage-bearing records for this incident participate;
    # legacy packets are intentionally not guessed into an incident.
    candidates: list[tuple[int, str, FailurePacket]] = []
    for raw_ref, raw_record in registry.items():
        if not isinstance(raw_record, Mapping):
            continue
        raw_packet = raw_record.get("failure_packet")
        if (
            not isinstance(raw_packet, Mapping)
            or str(raw_packet.get("incident_id") or "") != incident_id
        ):
            continue
        ref = str(raw_ref or "")
        packet = _failure_packet_from_record(ref, raw_record)
        _validate_incident_lineage(packet, registry)
        candidates.append((packet.packet_version, ref, packet))
    if not candidates:
        return "", None
    candidates.sort(key=lambda item: (item[0], item[1]))
    highest_version = candidates[-1][0]
    highest = [item for item in candidates if item[0] == highest_version]
    if len(highest) != 1:
        raise StageContractError(
            "RECOVERY_CAUSAL_INCIDENT_VERSION_CONFLICT", incident_id
        )
    _, head_ref, packet = highest[0]
    return head_ref, packet


def append_causal_failure_lifecycle_transition(
    task_state: dict[str, Any],
    *,
    packet_ref: str,
    packet: FailurePacket,
    transition: str,
    reason: str,
    related_ref: str = "",
) -> bool:
    """Append one immutable packet-lifecycle transition if it is new.

    Failure packets remain content-addressed and are never annotated in place.
    The separate event stream records which packet currently owns Recovery and
    why an earlier packet stopped owning it.  Semantic duplicate suppression
    keeps CAS retries and idempotent replay from manufacturing extra events;
    timestamps are observation metadata, not transition identity.
    """

    resolved_transition = str(transition or "").strip()
    if resolved_transition not in _CAUSAL_FAILURE_LIFECYCLE_TRANSITIONS:
        raise StageContractError(
            "RECOVERY_CAUSAL_LIFECYCLE_TRANSITION_INVALID",
            resolved_transition or "<missing>",
        )
    resolved_packet_ref = str(packet_ref or "").strip()
    if not resolved_packet_ref:
        raise StageContractError(
            "RECOVERY_CAUSAL_LIFECYCLE_PACKET_REF_MISSING"
        )
    raw_events = task_state.get(CAUSAL_FAILURE_LIFECYCLE_FIELD) or []
    if not isinstance(raw_events, list) or any(
        not isinstance(item, Mapping) for item in raw_events
    ):
        raise StageContractError(
            "RECOVERY_CAUSAL_LIFECYCLE_INVALID",
            CAUSAL_FAILURE_LIFECYCLE_FIELD,
        )
    identity = {
        "packet_ref": resolved_packet_ref,
        "incident_id": str(packet.incident_id or packet.failure_id),
        "packet_version": int(packet.packet_version or 0),
        "transition": resolved_transition,
        "reason": str(reason or "").strip(),
        "related_ref": str(related_ref or "").strip(),
    }
    for item in raw_events:
        if all(item.get(key) == value for key, value in identity.items()):
            return False
    body = {
        "schema_version": 1,
        **identity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    body["event_id"] = "failure-lifecycle-" + _hash(body)
    task_state[CAUSAL_FAILURE_LIFECYCLE_FIELD] = [
        *deepcopy(raw_events),
        body,
    ]
    return True


def has_causal_failure_lifecycle_transition(
    task_state: Mapping[str, Any],
    *,
    packet_ref: str,
    transition: str,
) -> bool:
    raw_events = task_state.get(CAUSAL_FAILURE_LIFECYCLE_FIELD) or []
    return isinstance(raw_events, list) and any(
        isinstance(item, Mapping)
        and str(item.get("packet_ref") or "") == str(packet_ref or "")
        and str(item.get("transition") or "") == str(transition or "")
        for item in raw_events
    )


class CausalFailurePacketBinder:
    """Persist an exact producer packet and expose its graph-state projection."""

    def __init__(
        self,
        *,
        data_path: str,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
    ) -> None:
        self.data_path = str(data_path)
        self.loader = loader
        self.saver = saver

    @staticmethod
    def _state_update(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "active_failure_packet_ref": str(record.get("packet_ref") or ""),
            "failure_packet": deepcopy(dict(record.get("failure_packet") or {})),
            "failure_source": deepcopy(dict(record.get("failure_source") or {})),
        }

    def bind(
        self,
        state: Mapping[str, Any],
        source_kind: str,
        source: Mapping[str, Any],
    ) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery task_id")
        durable = self.loader(self.data_path, task_id)
        step_worker = state.get("step_worker_outcome")
        step_archive_ref = (
            str(step_worker.get("archive_ref") or "")
            if isinstance(step_worker, Mapping)
            else ""
        )
        base_record = build_causal_failure_record(
            source_kind,
            source,
            durable,
            task_id=task_id,
            inspection_refs=((step_archive_ref,) if step_archive_ref else ()),
        )
        inspection_entries: dict[str, dict[str, Any]] = {}
        publication_failures: tuple[Mapping[str, Any], ...] = ()
        # build_causal_failure_record has already validated the typed source
        # and the legal Recovery edge. Publication is shared by those edges;
        # readers authorize the resulting active CAS publication, not a second
        # list of source-kind spellings.
        if is_recovery_failure_source(source_kind, source):
            try:
                inspection_entries = prepare_inspection_evidence(
                    data_path=self.data_path, durable=durable, state=state,
                    source=source,
                    packet=FailurePacket.from_mapping(base_record["failure_packet"]),
                    episode_id="recovery-seed-" + str(base_record["source_fingerprint"]),
                )
            except InspectionEvidenceError as exc:
                # The typed failure event remains the cause even when its
                # inspection publication is unavailable. Do not accept a bad
                # ref, change its scope, or fall back to the previous packet.
                import logging
                logging.getLogger(__name__).exception("Recovery inspection publication rejected")
                pending_attempt = str(((durable.get("task_state") or {}).get("verification_pending") or {}).get("attempt_id") or "")
                source_attempt = str(base_record["failure_packet"].get("attempt_ref") or "")
                if (exc.code == "INSPECTION_REF_SCOPE_MISMATCH"
                    and pending_attempt and pending_attempt != source_attempt
                    and pending_attempt in ((durable.get("provenance_state") or {}).get("attempts") or {}) and any(
                    isinstance(prior, Mapping) and prior.get("source_outcome_ref") == base_record["source_outcome_ref"]
                    for prior in ((durable.get("task_state") or {}).get("causal_failure_packets") or {}).values()
                )):
                    # A previously published event whose attempt authority has
                    # since advanced is a late replay. Preserve its historical
                    # packet and the current head without republishing either.
                    logging.getLogger(__name__).warning("RECOVERY_CAUSAL_REPLAY_SCOPE_STALE: %s", base_record["source_outcome_ref"])
                    return {}
                publication_failures = ({
                    "code": exc.code, "detail": exc.detail,
                    "stage": source_kind.removesuffix("_outcome") + ".inspection_publication",
                    "source_object_id": base_record["source_outcome_ref"],
                    "expected_scope": deepcopy(dict(source.get("scope") or {})),
                    "expected_scope_source": "triggering_typed_failure_outcome",
                    "actual_scope_source": "durable_inspection_authority",
                    "evidence_used": False,
                },)
            publication_failures += tuple({
                "handle": ref,
                "source_object_id": entry["scope"]["source_object_id"],
                "object_kind": entry["scope"]["object_kind"],
                "code": entry["scope"]["unavailable_reason"],
                "detail": entry["scope"]["unavailable_detail"],
                "stage": {"execute_outcome": "execute", "verification_outcome": "supervisor",
                    "completion_gate_outcome": "supervisor", "commit_step_outcome": "commit_step",
                    "install_outcome": "install_subagent", "install_subagent_outcome": "install_subagent"}.get(
                        source_kind, source_kind.removesuffix("_outcome")) + ".inspection_publication",
            } for ref, entry in inspection_entries.items() if not entry["scope"]["readable"] and not entry["scope"]["unavailable_reason"].startswith("NOT_APPLICABLE"))
            base_record = build_causal_failure_record(
                source_kind, source, durable, task_id=task_id,
                inspection_publication_failures=publication_failures,
                inspection_refs=_unique_text(
                    ((step_archive_ref,) if step_archive_ref else ()),
                    tuple(inspection_entries),
                ),
            )
        task_state = dict(durable.get("task_state") or {})
        raw_registry = task_state.get("causal_failure_packets") or {}
        if not isinstance(raw_registry, Mapping):
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_REGISTRY_INVALID", task_id
            )
        registry = dict(raw_registry)
        incident_id = str(base_record.get("incident_id") or "")
        head_ref, head_packet = _incident_head(
            task_state=task_state,
            registry=registry,
            incident_id=incident_id,
        )

        # The exact same producer outcome is an idempotent replay only when it
        # is still the incident head.  Replaying an older source after a newer
        # version exists must not roll the lineage backwards.
        event_fingerprint = _failure_event_fingerprint(base_record)
        for prior_ref, raw_prior in registry.items():
            if (
                not isinstance(raw_prior, Mapping)
            ):
                continue
            raw_packet = raw_prior.get("failure_packet")
            if (
                not isinstance(raw_packet, Mapping)
                or str(raw_packet.get("incident_id") or "") != incident_id
            ):
                continue
            prior_ref_text = str(prior_ref or "")
            _failure_packet_from_record(prior_ref_text, raw_prior)
            if (
                _failure_event_fingerprint(raw_prior) == event_fingerprint
                and head_ref
                and prior_ref_text != head_ref
            ):
                raise StageContractError(
                    "RECOVERY_CAUSAL_PACKET_STALE", prior_ref_text
                )

        if head_packet is None:
            record = base_record
        else:
            raw_head = registry.get(head_ref)
            if not isinstance(raw_head, Mapping):
                raise StageContractError(
                    "RECOVERY_CAUSAL_INCIDENT_HEAD_MISSING", head_ref
                )
            if (
                _failure_event_fingerprint(raw_head) == event_fingerprint
            ):
                record = deepcopy(dict(raw_head))
            else:
                record = build_causal_failure_record(
                    source_kind,
                    source,
                    durable,
                    task_id=task_id,
                    inspection_refs=(
                        _unique_text(
                            ((step_archive_ref,) if step_archive_ref else ()),
                            tuple(inspection_entries),
                        )
                    ),
                    packet_version=head_packet.packet_version + 1,
                    supersedes_packet_ref=head_ref,
                    inspection_publication_failures=publication_failures,
                )

        record["inspection_publication"] = {
            "schema_version": 1, "policy": "typed_recovery_evidence_v2",
            "source_fingerprint": record["source_fingerprint"],
            "refs": sorted(inspection_entries),
        }
        packet_ref = str(record["packet_ref"])
        prior = registry.get(packet_ref)
        if isinstance(prior, Mapping):
            if dict(prior) != record:
                raise StageContractError(
                    "RECOVERY_CAUSAL_PACKET_REF_COLLISION", packet_ref
                )
            raw_heads = task_state.get("causal_failure_incident_heads") or {}
            if (
                isinstance(raw_heads, Mapping)
                and str(raw_heads.get(incident_id) or "") == packet_ref
                and str(task_state.get("active_failure_packet_ref") or "")
                == packet_ref
                and has_causal_failure_lifecycle_transition(
                    task_state,
                    packet_ref=packet_ref,
                    transition="active",
                )
                and all(
                    (task_state.get("inspection_ref_registry") or {}).get(ref) == entry
                    for ref, entry in inspection_entries.items()
                )
            ):
                return self._state_update(record)

        candidate = deepcopy(durable)
        candidate_task = dict(candidate.get("task_state") or {})
        if inspection_entries:
            inspection_index = dict(candidate_task.get("inspection_ref_registry") or {})
            for ref, entry in inspection_entries.items():
                if ref in inspection_index and inspection_index[ref] != entry:
                    raise StageContractError("INSPECTION_REF_COLLISION", ref)
                inspection_index[ref] = deepcopy(entry)
            candidate_task["inspection_ref_registry"] = inspection_index
        candidate_registry = dict(candidate_task.get("causal_failure_packets") or {})
        candidate_registry[packet_ref] = deepcopy(record)
        candidate_task["causal_failure_packets"] = candidate_registry
        candidate_heads = dict(
            candidate_task.get("causal_failure_incident_heads") or {}
        )
        candidate_heads[incident_id] = packet_ref
        candidate_task["causal_failure_incident_heads"] = candidate_heads
        previous_active_ref = str(
            candidate_task.get("active_failure_packet_ref") or ""
        )
        if previous_active_ref and previous_active_ref != packet_ref:
            raw_previous = candidate_registry.get(previous_active_ref)
            if not isinstance(raw_previous, Mapping):
                raise StageContractError(
                    "RECOVERY_CAUSAL_PACKET_MISSING", previous_active_ref
                )
            previous_packet = _failure_packet_from_record(
                previous_active_ref,
                raw_previous,
            )
            append_causal_failure_lifecycle_transition(
                candidate_task,
                packet_ref=previous_active_ref,
                packet=previous_packet,
                transition="superseded",
                reason=(
                    "incident_packet_version_advanced"
                    if previous_packet.incident_id == incident_id
                    else "active_incident_replaced"
                ),
                related_ref=packet_ref,
            )
        append_causal_failure_lifecycle_transition(
            candidate_task,
            packet_ref=packet_ref,
            packet=FailurePacket.from_mapping(record["failure_packet"]),
            transition="active",
            reason="causal_failure_bound",
            related_ref=(
                previous_active_ref
                if previous_active_ref and previous_active_ref != packet_ref
                else ""
            ),
        )
        candidate_task["active_failure_packet_ref"] = packet_ref
        candidate["task_state"] = candidate_task
        try:
            self.saver(self.data_path, task_id, candidate)
        except StateCASConflict:
            refreshed = self.loader(self.data_path, task_id)
            refreshed_task = dict(refreshed.get("task_state") or {})
            refreshed_record = dict(
                (refreshed_task.get("causal_failure_packets") or {}).get(packet_ref)
                or {}
            )
            refreshed_heads = dict(
                refreshed_task.get("causal_failure_incident_heads") or {}
            )
            if (
                refreshed_record == record
                and all(
                    (refreshed_task.get("inspection_ref_registry") or {}).get(ref) == entry
                    for ref, entry in inspection_entries.items()
                )
                and str(refreshed_heads.get(incident_id) or "") == packet_ref
                and str(refreshed_task.get("active_failure_packet_ref") or "")
                == packet_ref
                and has_causal_failure_lifecycle_transition(
                    refreshed_task,
                    packet_ref=packet_ref,
                    transition="active",
                )
            ):
                return self._state_update(record)
            raise
        return self._state_update(record)


def bind_causal_failure_node(
    handler: RuntimeNode,
    *,
    binder: CausalFailurePacketBinder,
    outcome_field: str,
) -> RuntimeNode:
    """Attach producer-owned packet persistence to one validated typed node."""

    async def wrapped(state: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(await handler(state))
        raw = result.get(outcome_field)
        if not isinstance(raw, Mapping):
            return result
        source_kind = recovery_source_kind(outcome_field, raw)
        if is_recovery_failure_source(source_kind, raw):
            result.update(binder.bind(state, source_kind, raw))
        return result

    return wrapped


def _resolve_causal_record(
    state: Mapping[str, Any], durable: Mapping[str, Any]
) -> dict[str, Any] | None:
    task_state = dict(durable.get("task_state") or {})
    raw_registry = task_state.get("causal_failure_packets") or {}
    if not isinstance(raw_registry, Mapping):
        raise StageContractError("RECOVERY_CAUSAL_PACKET_REGISTRY_INVALID")
    registry = dict(raw_registry)
    active_ref = str(task_state.get("active_failure_packet_ref") or "")
    state_ref = str(state.get("active_failure_packet_ref") or "")
    consumed = dict(task_state.get("consumed_failure_packet_refs") or {})

    if state_ref and active_ref and state_ref != active_ref:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_STALE",
            f"edge={state_ref}; active={active_ref}",
        )
    packet_ref = state_ref or active_ref
    if not packet_ref:
        return None
    if state_ref and not active_ref and state_ref not in consumed:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_STALE", f"inactive={state_ref}"
        )
    record = registry.get(packet_ref)
    if not isinstance(record, Mapping):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_MISSING", packet_ref
        )
    resolved = dict(record)
    packet = _failure_packet_from_record(packet_ref, resolved)
    if packet.incident_id:
        raw_heads = task_state.get("causal_failure_incident_heads") or {}
        if not isinstance(raw_heads, Mapping):
            raise StageContractError(
                "RECOVERY_CAUSAL_INCIDENT_HEAD_INVALID",
                packet.incident_id,
            )
        incident_head_ref = str(raw_heads.get(packet.incident_id) or "")
        if incident_head_ref and incident_head_ref != packet_ref:
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_STALE",
                f"packet={packet_ref}; incident_head={incident_head_ref}",
            )
        _validate_incident_lineage(packet, registry)
    if state.get("failure_packet"):
        edge_packet = FailurePacket.from_mapping(state.get("failure_packet") or {})
        if edge_packet.to_dict() != packet.to_dict():
            raise StageContractError(
                "RECOVERY_CAUSAL_PACKET_EDGE_MISMATCH", packet_ref
            )
    source_kind = str(resolved.get("source_kind") or "")
    source = dict(resolved.get("failure_source") or {})
    if _content_ref("failure-source", source) != str(
        resolved.get("failure_source_ref") or ""
    ):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_SOURCE_MISMATCH", packet_ref
        )
    if packet.source != source_kind:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_SOURCE_MISMATCH", packet_ref
        )
    if str(resolved.get("source_outcome_ref") or "") != packet.source_outcome_ref:
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_SOURCE_MISMATCH", packet_ref
        )
    source_incident_id = str(
        _source_payload(source_kind, source).get("incident_id") or ""
    ).strip()
    if (
        packet.incident_id
        and source_incident_id
        and source_incident_id != packet.incident_id
    ):
        raise StageContractError(
            "RECOVERY_CAUSAL_PACKET_INCIDENT_MISMATCH", packet_ref
        )
    return resolved


def project_recovery_seed(
    state: Mapping[str, Any], durable: Mapping[str, Any]
) -> RecoveryProjection | None:
    """Resolve the one edge-bound or durable-active packet without scanning."""

    record = _resolve_causal_record(state, durable)
    if record is None:
        return None
    source_kind = str(record.get("source_kind") or "")
    runtime_source = dict(record.get("failure_source") or {})
    source_fingerprint = str(record.get("source_fingerprint") or "")
    packet = FailurePacket.from_mapping(record.get("failure_packet") or {})
    task_id = str(state.get("task_id") or durable.get("task_id") or "")
    if not task_id:
        raise StageContractError("STAGE_FIELD_REQUIRED", "Recovery task_id")
    task_ref = _task_ref(durable, task_id)
    plan_ref = _plan_ref(durable, task_ref)
    if packet.task_id != task_id or packet.plan_spec_ref != plan_ref:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery causal packet task/plan identity"
        )
    if packet.step_id != str(durable.get("current_step_id") or ""):
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "Recovery causal packet step identity"
        )
    current_step = _current_step(durable, packet.step_id)
    dependencies = _dependency_outcomes(durable, current_step)
    bundle = _skill_bundle(durable, task_ref.task_revision, packet.step_id)
    seed = RecoverySeed(
        schema_version=1,
        seed_id="recovery-seed-" + source_fingerprint,
        task_spec_ref=task_ref,
        plan_spec_ref=plan_ref,
        selected_skill_bundle=bundle,
        current_step=current_step,
        failure_packet=packet,
        dependency_outcomes=dependencies,
        evidence_refs=packet.evidence_refs,
    )
    # A final wire round-trip proves the complete recursive boundary, rather
    # than only the individual component constructors, is leak free.
    RecoverySeed.from_mapping(seed.to_dict())
    return RecoveryProjection(
        source_kind=source_kind,
        source=runtime_source,
        source_fingerprint=source_fingerprint,
        failure_packet=packet,
        recovery_seed=seed,
    )


__all__ = [
    "CAUSAL_FAILURE_LIFECYCLE_FIELD",
    "CausalFailurePacketBinder",
    "RecoveryProjection",
    "append_causal_failure_lifecycle_transition",
    "bind_causal_failure_node",
    "build_causal_failure_record",
    "has_causal_failure_lifecycle_transition",
    "is_recovery_failure_source",
    "project_recovery_seed",
    "recovery_source_kind",
]

"""Stateful, tool-agnostic diagnosis and recovery progress primitives.

The LLM may interpret evidence, but it does not own confidence, progress, or
the retry budget.  Those decisions are derived here from persisted records.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent_core.context_lineage import redact_text
from agent_core.controller_audit import compare_runtime_machine_evidence
from agent_core.plan_tracker import canonical_task_spec_for_state
from agent_core.root_cause import extract_failed_trace_event
from agent_core.task_spec import project_effective_user_facts, resolve_effective_user_facts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _clip(value: Any, limit: int = 12000) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]..."


_SECRET_RE = re.compile(
    r"(?P<prefix>\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|authorization|bearer)\b\s*(?:=|:)\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)


def _redact(value: Any) -> str:
    return redact_text(_text(value))


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|secret|password|passwd|authorization|credential)",
    re.IGNORECASE,
)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact recovery state without serializing arbitrary objects."""
    if depth > 8:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                continue
            result[name] = _sanitize(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(child, depth=depth + 1) for child in value]
    if isinstance(value, str):
        return _redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value)


def _fingerprint(*parts: Any) -> str:
    text = "\n".join(_redact(item).lower() for item in parts)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", text)
    text = re.sub(r"/[^\s/]+(?:/[^\s/]+){2,}", "<path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:20]


def _unique(values: Iterable[Any], limit: Optional[int] = None) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        item = _text(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result[-limit:] if limit else result


@dataclass(frozen=True)
class DiagnosticLimits:
    """Hard bounds for recovery-time, read-only context collection."""

    max_scan_depth: int = 4
    max_files: int = 64
    max_text_bytes_per_file: int = 128 * 1024
    max_total_read_bytes: int = 1024 * 1024
    # Leaves room for the RecoveryJudge policy/schema inside a 32k prompt.
    max_context_chars: int = 26_000
    log_tail_lines: int = 100
    max_error_lines: int = 80


@dataclass
class CurrentStepContext:
    """One controller-owned serialization of the active plan step."""

    step_id: str = ""
    step_type: str = ""
    status: str = ""
    title: str = ""
    purpose: str = ""
    skill_ids: List[str] = field(default_factory=list)
    renderer_id: str = ""
    input_bindings: List[Dict[str, Any]] = field(default_factory=list)
    output_contract: List[Dict[str, Any]] = field(default_factory=list)
    success_criteria: List[Any] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    manifest_status: str = "not_applicable"
    manifest_path: str = ""
    allowed_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return _sanitize(asdict(self))


@dataclass
class RecoveryContext:
    """Canonical, replayable recovery evidence for one execution attempt.

    ``DiagnosticContext`` remains an alias for checkpoint/source compatibility.
    RecoveryJudge, RecoveryPolicy and Generate consume projections of this same
    object rather than constructing independent failure bundles.
    """

    schema_version: str = "3"
    context_id: str = ""
    task_id: str = ""
    step_id: str = ""
    action_id: str = ""
    attempt_id: str = ""
    error_class: str = ""
    error_fingerprint: str = ""
    failure_stage: str = ""
    approved_plan_snapshot_id: str = ""
    candidate_fingerprint: str = ""
    repair_ordinal: int = 0
    authoritative_state_hash: str = ""
    task_spec_summary: Dict[str, Any] = field(default_factory=dict)
    effective_user_facts: Dict[str, Any] = field(default_factory=dict)
    current_step: Dict[str, Any] = field(default_factory=dict)
    action_contract: Dict[str, Any] = field(default_factory=dict)
    execution: Dict[str, Any] = field(default_factory=dict)
    validation: Dict[str, Any] = field(default_factory=dict)
    completion: Dict[str, Any] = field(default_factory=dict)
    progress: Dict[str, Any] = field(default_factory=dict)
    runtime: Dict[str, Any] = field(default_factory=dict)
    machine_audit_findings: List[Dict[str, Any]] = field(default_factory=list)
    available_evidence: List[Dict[str, Any]] = field(default_factory=list)
    manifest: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    recent_attempts: List[Dict[str, Any]] = field(default_factory=list)
    already_tried_strategies: List[str] = field(default_factory=list)
    recovery_decision: Dict[str, Any] = field(default_factory=dict)
    allowed_actions: List[str] = field(default_factory=list)
    log_references: List[str] = field(default_factory=list)
    bounded_log_excerpt: str = ""
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    skill_policy: Dict[str, Any] = field(default_factory=dict)
    diagnosis: Dict[str, Any] = field(default_factory=dict)
    limits: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _sanitize(asdict(self))

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "RecoveryContext":
        """Idempotently migrate persisted DiagnosticContext/checkpoint values."""
        source = dict(value or {})
        if "task_spec_summary" not in source and isinstance(source.get("task"), dict):
            source["task_spec_summary"] = source.get("task")
        if "current_step" not in source and isinstance(source.get("plan"), dict):
            source["current_step"] = source.get("plan")
        if "validation" not in source and isinstance(source.get("reviews"), dict):
            reviews = dict(source.get("reviews") or {})
            source["validation"] = {
                "legacy_supervisor_verdict": reviews.get("supervisor_verdict") or "",
                "errors": [reviews.get("supervisor_reason")] if reviews.get("supervisor_reason") else [],
            }
            source["completion"] = {
                "verdict": reviews.get("completion_gate_verdict") or "",
                "reason": reviews.get("completion_gate_reason") or "",
                "classification": reviews.get("completion_gate_classification") or "",
                "missing_artifacts": reviews.get("completion_gate_missing_artifacts") or [],
            }
        if "artifacts" not in source and isinstance(source.get("artifact_evidence"), dict):
            source["artifacts"] = list(
                (source.get("artifact_evidence") or {}).get("filesystem_snapshot") or []
            )
        if "progress" not in source and isinstance(source.get("recovery_history"), dict):
            history = dict(source.get("recovery_history") or {})
            source["progress"] = {
                key: history.get(key)
                for key in (
                    "progress_detected", "new_artifacts_since_previous_attempt",
                    "code_changed", "contract_changed", "same_failure_count",
                )
                if history.get(key) not in (None, "", [], {})
            }
            source["recent_attempts"] = list(history.get("attempts") or [])[-3:]
            source.setdefault("error_fingerprint", str(history.get("failure_fingerprint") or ""))
        source.setdefault("schema_version", "3")
        kwargs = {
            name: source.get(name)
            for name in cls.__dataclass_fields__
            if name in source
        }
        context = cls(**kwargs)
        if not context.context_id:
            context.context_id = context.stable_context_id()
        return context

    def stable_context_id(self) -> str:
        evidence_identity = hashlib.sha256(
            json.dumps(
                {
                    "execution": self.execution,
                    "runtime": self.runtime,
                    "machine_audit_findings": self.machine_audit_findings,
                    "available_evidence": self.available_evidence,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8", errors="replace")
        ).hexdigest()[:20]
        identity = {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "error_fingerprint": self.error_fingerprint,
            "failure_stage": self.failure_stage,
            "approved_plan_snapshot_id": self.approved_plan_snapshot_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "repair_ordinal": int(self.repair_ordinal or 0),
            "authoritative_state_hash": self.authoritative_state_hash,
            "evidence_identity": evidence_identity,
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        return f"recovery-context-{digest}"

    def prompt_view(self, max_chars: Optional[int] = None) -> Dict[str, Any]:
        """Return a bounded evidence view without truncating task semantics.

        This method remains for legacy diagnostic callers.  RecoveryJudge v2
        receives a Controller-built ``LLMPayload``, but a compatibility caller
        must still never turn a complete constraint set into a misleading
        prefix/tail.  Only evidence/history sections are compacted.  If the
        semantic closure itself does not fit, return a typed capacity result
        rather than a partial semantic view.
        """
        budget = max(2_000, int(max_chars or self.limits.get("max_context_chars") or 32_000))
        value = self.to_dict()
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) <= budget:
            return value

        def compact_evidence(item: Any, *, text_limit: int, list_limit: int) -> Any:
            if isinstance(item, str):
                return _clip(item, text_limit)
            if isinstance(item, dict):
                return {
                    str(key): compact_evidence(
                        child,
                        text_limit=text_limit,
                        list_limit=list_limit,
                    )
                    for key, child in item.items()
                }
            if isinstance(item, list):
                return [
                    compact_evidence(
                        child,
                        text_limit=text_limit,
                        list_limit=list_limit,
                    )
                    for child in item[-list_limit:]
                ]
            return item

        task_summary = dict(value.get("task_spec_summary") or {})
        # Old checkpoints may carry the complete request under compatibility
        # keys.  They are audit material, not a second semantic authority.
        task_summary = {
            key: task_summary.get(key)
            for key in (
                "goal",
                "inputs",
                "output_dir",
                "constraints",
                "required_outputs",
                "output_contracts",
                "confirmed_decisions",
                "unresolved_questions",
            )
            if task_summary.get(key) not in (None, "", [], {})
        }
        semantic_sections = {
            "task_spec_summary": task_summary,
            "effective_user_facts": deepcopy(
                dict(value.get("effective_user_facts") or {})
            ),
            "current_step": deepcopy(dict(value.get("current_step") or {})),
            "action_contract": deepcopy(dict(value.get("action_contract") or {})),
            "allowed_actions": deepcopy(list(value.get("allowed_actions") or [])),
        }
        identity = {
            key: value.get(key)
            for key in (
                "schema_version",
                "context_id",
                "task_id",
                "step_id",
                "action_id",
                "attempt_id",
                "error_class",
                "error_fingerprint",
            )
        }
        evidence_sections = (
            "execution",
            "validation",
            "completion",
            "progress",
            "runtime",
            "manifest",
            "artifacts",
            "recent_attempts",
            "already_tried_strategies",
            "recovery_decision",
            "log_references",
            "bounded_log_excerpt",
            "conflicts",
            "skill_policy",
            "diagnosis",
        )

        # Evidence is excerptable and attempt history is intentionally recent;
        # task/step constraints above are copied byte-for-byte at every level.
        for text_limit, list_limit in ((3_000, 24), (1_200, 12), (480, 6), (160, 3)):
            candidate = {**identity, **deepcopy(semantic_sections)}
            for key in evidence_sections:
                candidate[key] = compact_evidence(
                    value.get(key),
                    text_limit=text_limit,
                    list_limit=list_limit,
                )
            candidate["limits"] = {
                "max_context_chars": budget,
                "context_truncated": True,
                "semantic_projection_complete": True,
                "compaction_scope": "evidence_only",
            }
            if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)) <= budget:
                return candidate

        required_semantic_chars = len(json.dumps(
            {**identity, **semantic_sections},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ))
        return {
            "schema_version": str(value.get("schema_version") or "3"),
            "context_id": value.get("context_id"),
            "task_id": value.get("task_id"),
            "step_id": value.get("step_id"),
            "attempt_id": value.get("attempt_id"),
            "error_fingerprint": value.get("error_fingerprint"),
            "error": {
                "code": "CONTEXT_CAPACITY_EXCEEDED",
                "owner": "controller_projection",
                "message": (
                    "The complete recovery semantic closure exceeds the "
                    "configured context capacity; no partial semantic view "
                    "was produced."
                ),
                "required_semantic_chars": required_semantic_chars,
                "max_context_chars": budget,
            },
            "limits": {
                "max_context_chars": budget,
                "context_truncated": False,
                "semantic_projection_complete": False,
                "capacity_exceeded": True,
            },
        }


# Compatibility name used by pre-Batch-5 imports/checkpoints. It is the same
# model, not a parallel diagnostic context.
DiagnosticContext = RecoveryContext


def build_preaction_recovery_context(
    *,
    task_id: str,
    step_id: str,
    approved_plan_snapshot_id: str,
    authoritative_state_hash: str,
    candidate: Mapping[str, Any],
    candidate_fingerprint: str,
    violations: Sequence[Mapping[str, Any]],
    repair_ordinal: int,
    visible_artifacts: Sequence[Mapping[str, Any]] = (),
    current_step: Optional[Mapping[str, Any]] = None,
) -> RecoveryContext:
    """Build execution-free recovery evidence for a rejected Generate action."""
    normalized_violations = [
        _sanitize(dict(item))
        for item in violations
        if isinstance(item, Mapping)
    ]
    error_payload = {
        "failure_stage": "pre_action_contract",
        "step_id": str(step_id or ""),
        "violations": normalized_violations,
    }
    error_fp = hashlib.sha256(
        json.dumps(
            error_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    ).hexdigest()
    ordinal = max(1, int(repair_ordinal or 1))
    context = RecoveryContext(
        schema_version="4",
        task_id=str(task_id or ""),
        step_id=str(step_id or ""),
        action_id=f"generate-candidate:{candidate_fingerprint}",
        attempt_id=f"pre-action:{ordinal}:{candidate_fingerprint}",
        error_class=str(
            normalized_violations[0].get("code")
            if normalized_violations
            else "PRE_ACTION_CONTRACT_FAILURE"
        ),
        error_fingerprint=error_fp,
        failure_stage="pre_action_contract",
        approved_plan_snapshot_id=str(approved_plan_snapshot_id or ""),
        candidate_fingerprint=str(candidate_fingerprint or ""),
        repair_ordinal=ordinal,
        authoritative_state_hash=str(authoritative_state_hash or ""),
        current_step=_sanitize(dict(current_step or {})),
        action_contract={
            "structured_candidate": _sanitize(dict(candidate or {})),
            "candidate_fingerprint": str(candidate_fingerprint or ""),
        },
        execution={},
        validation={
            "status": "invalid",
            "failure_stage": "pre_action_contract",
            "violations": normalized_violations,
        },
        runtime={
            "approved_plan_snapshot_id": str(approved_plan_snapshot_id or ""),
            "authoritative_state_hash": str(authoritative_state_hash or ""),
            "repair_ordinal": ordinal,
        },
        artifacts=[
            _sanitize(dict(item))
            for item in visible_artifacts
            if isinstance(item, Mapping)
        ],
        recent_attempts=[],
        log_references=[],
        bounded_log_excerpt="",
    )
    context.context_id = context.stable_context_id()
    return context


@dataclass
class StructuredDiagnosis:
    """Evidence-grounded diagnosis consumed by the existing RecoveryJudge."""

    root_cause: str = ""
    failure_owner: str = "DIAGNOSTIC"
    failure_type: str = "unknown"
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.0
    missing_evidence: List[str] = field(default_factory=list)
    repair_strategy: str = ""
    next_action: str = "ESCALATE"
    requires_user_input: bool = False
    evidence_sufficient: bool = False
    collection_complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["confidence"] = max(0.0, min(1.0, float(self.confidence or 0.0)))
        return value


_LARGE_BIO_SUFFIXES = {
    ".bam", ".cram", ".fastq", ".fq", ".cool", ".mcool", ".hic",
}
_SMALL_TEXT_SUFFIXES = {
    ".json", ".tsv", ".csv", ".md", ".txt", ".sh", ".bash", ".py",
    ".r", ".log", ".err", ".out", ".trace", ".yaml", ".yml", ".vcf",
}

_INTERNAL_STATE_FILES = {
    "active_plan.json", "diagnostic_context.json", "execution_records.jsonl",
    "plan_state.json", "run_metrics.json", "run_metrics.md", "recovery_trace.md",
}

_RUNTIME_ENV_KEYS = (
    "CONDA_DEFAULT_ENV", "CONDA_EXE", "CONDA_PREFIX", "ENV_PREFIX", "JAVA_HOME",
    "RESOLVED_ENV_PREFIX", "RESOLVED_JAVA_HOME", "RESOLVED_SNPEFF_JAR", "SNPEFF_JAR",
    "VIRTUAL_ENV",
)


def _read_text_evidence(path_value: Any, limit: int) -> Tuple[str, bool, int]:
    path = Path(str(path_value or "")).expanduser()
    if not str(path_value or "").strip() or not path.is_file():
        return "", False, 0
    try:
        size = int(path.stat().st_size)
        with path.open("rb") as handle:
            raw = handle.read(max(0, limit))
    except OSError:
        return "", False, 0
    return raw.decode("utf-8", errors="replace"), size <= limit, size


def _script_context(body: str, failed_command: str, failed_line: Any, radius: int = 4) -> Dict[str, Any]:
    lines = str(body or "").splitlines()
    if not lines:
        return {}
    line_number = int(failed_line or 0)
    command_token = str(failed_command or "").strip()
    if command_token:
        prefix = command_token[: min(120, len(command_token))]
        for index, line in enumerate(lines, 1):
            if prefix and prefix in line:
                line_number = index
                break
    if line_number <= 0 or line_number > len(lines):
        return {"script_line_count": len(lines), "script_preview": "\n".join(lines[:20])}
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return {
        "failed_script_line": line_number,
        "script_line_count": len(lines),
        "script_context": "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1)),
    }


def _expected_runtime_contract(state: Dict[str, Any]) -> Dict[str, Any]:
    audits = []
    for item in state.get("runtime_audit", []) or []:
        if not isinstance(item, dict):
            continue
        audits.append({
            "skill_id": str(item.get("skill_id") or item.get("skill") or ""),
            "env": str(item.get("checked_env") or item.get("env") or item.get("declared_env") or ""),
            "status": str(item.get("status") or ""),
            "env_runner": str(item.get("env_runner") or ""),
            "healthcheck": str(item.get("healthcheck") or item.get("declared_healthcheck") or ""),
            "resolved_runtime_paths": dict(item.get("resolved_runtime_paths") or {}),
        })
    bindings: Dict[str, str] = {}
    for audit in audits:
        if audit.get("status", "").lower() == "ready":
            bindings.update({str(k): str(v) for k, v in audit.get("resolved_runtime_paths", {}).items()})
    legacy_runtime = state.get("runtime_status") or {}
    legacy_records = legacy_runtime.values() if isinstance(legacy_runtime, dict) else legacy_runtime
    for item in legacy_records or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        audits.append({
            "skill_id": str(item.get("skill_id") or ""),
            "env": str(item.get("env") or ""),
            "status": "ready",
            "env_runner": str(item.get("env_runner") or ""),
            "healthcheck": str(item.get("healthcheck") or ""),
            "resolved_runtime_paths": {},
        })
    return {"audits": audits, "bindings": bindings}


def enrich_execution_evidence(
    state: Dict[str, Any],
    execution_evidence: Dict[str, Any],
    *,
    limits: Optional[DiagnosticLimits] = None,
) -> Dict[str, Any]:
    """Deterministically collect failure evidence already present on disk."""
    limits = limits or DiagnosticLimits()
    state = dict(state or {})
    evidence = dict(execution_evidence or {})
    script_path = str(evidence.get("script_path") or evidence.get("script_file") or evidence.get("code_path") or "")
    script_body, script_complete, script_size = _read_text_evidence(
        script_path, limits.max_text_bytes_per_file
    )
    trace_path = str(evidence.get("trace_file") or "")
    trace_text = str(evidence.get("trace_text") or "")
    if not trace_text and trace_path:
        trace_text, _, _ = _read_text_evidence(trace_path, limits.max_text_bytes_per_file)
    failure = extract_failed_trace_event(trace_text)
    root = dict(evidence.get("root_cause_analysis") or {})
    primary = dict(root.get("primary_failure") or {})
    if primary.get("command") and not failure.get("failed_command"):
        failure["failed_command"] = str(primary.get("command") or "")
        failure["failed_returncode"] = primary.get("exit_code")
        failure["evidence_source"] = "root_cause_analysis"
    if not failure.get("failed_command") and evidence.get("command"):
        failure["failed_command"] = str(evidence.get("command") or "")
        failure["failed_returncode"] = evidence.get("returncode", evidence.get("exit_code"))
        failure["evidence_source"] = "executor"
    failure.update(_script_context(
        script_body,
        str(failure.get("failed_command") or ""),
        failure.get("failed_line_number"),
    ))

    stream_info: Dict[str, Any] = {}
    for stream in ("stdout", "stderr"):
        path = str(evidence.get(f"{stream}_log") or "")
        body, complete, size = _read_text_evidence(path, limits.max_text_bytes_per_file)
        if body:
            stream_info[f"{stream}_full"] = body if complete else ""
            stream_info[f"{stream}_tail"] = "\n".join(body.splitlines()[-limits.log_tail_lines :])
        stream_info[f"{stream}_complete"] = bool(complete)
        stream_info[f"{stream}_bytes"] = size
        stream_info[f"{stream}_log_path"] = path

    runtime_contract = dict(evidence.get("expected_runtime_contract") or {})
    if not runtime_contract:
        runtime_contract = _expected_runtime_contract(state)
    actual_environment = dict(evidence.get("actual_environment") or {})
    actual_environment = {
        key: (
            {
                str(child_key): str(child_value)
                for child_key, child_value in value.items()
            }
            if key in {"resolved_executables", "versions", "injected_bindings"}
            and isinstance(value, Mapping)
            else str(value)
        )
        for key, value in actual_environment.items()
        if key in _RUNTIME_ENV_KEYS
        or key in {"resolved_executables", "versions", "injected_bindings"}
    }
    combined_output = "\n".join([
        str(stream_info.get("stdout_tail") or evidence.get("stdout_summary") or ""),
        str(stream_info.get("stderr_tail") or evidence.get("stderr_summary") or ""),
    ])
    observed_runtime: Dict[str, str] = {}
    for key in _RUNTIME_ENV_KEYS:
        match = re.search(rf"(?:^|\n){re.escape(key)}=(/[^\n\r]+)", combined_output)
        if match:
            observed_runtime[key] = match.group(1).strip()
    prefix_header = re.search(r"===\s*CONDA_PREFIX\s*===\s*\n([^\n]+)", combined_output)
    if prefix_header:
        observed_runtime["CONDA_PREFIX"] = prefix_header.group(1).strip()

    bindings = dict(runtime_contract.get("bindings") or {})
    expected_prefix = str(bindings.get("RESOLVED_ENV_PREFIX") or bindings.get("ENV_PREFIX") or "")
    observed_prefix = str(actual_environment.get("ENV_PREFIX") or actual_environment.get("CONDA_PREFIX") or "")
    uses_login_shell_dynamic_prefix = bool(
        expected_prefix
        and re.search(r"\bbash\s+(?:[^\n]*\s)?-l?c\b", script_body)
        and re.search(r"\$\{?CONDA_PREFIX\}?", script_body)
    )
    runtime_drift = compare_runtime_machine_evidence(
        runtime_contract,
        actual_environment,
    )
    drift_reasons: List[str] = list(runtime_drift.get("reasons") or [])
    if uses_login_shell_dynamic_prefix:
        drift_reasons.append("login_shell_re_resolves_dynamic_conda_prefix")
    runtime_drift.update({
        "detected": bool(drift_reasons),
        "reasons": list(dict.fromkeys(drift_reasons)),
        "expected_env_prefix": expected_prefix,
        "observed_env_prefix": observed_prefix,
        "observed_runtime": observed_runtime,
    })

    manifest_path = str(evidence.get("manifest_path") or (state.get("pending_action") or {}).get("manifest_path") or "")
    manifest: Dict[str, Any] = {}
    manifest_error = ""
    if manifest_path:
        manifest_body, manifest_complete, _ = _read_text_evidence(manifest_path, limits.max_text_bytes_per_file)
        if not manifest_body:
            manifest_error = "manifest_missing_or_empty"
        elif not manifest_complete:
            manifest_error = "manifest_exceeds_diagnostic_read_limit"
        else:
            try:
                parsed = json.loads(manifest_body)
                manifest = {
                    "schema_version": parsed.get("schema_version"),
                    "renderer": parsed.get("renderer") or {},
                    "provenance": parsed.get("provenance") or {},
                    "action_ids": [
                        str(item.get("id") or item.get("action_id") or "")
                        for item in parsed.get("actions", []) or [] if isinstance(item, dict)
                    ],
                }
            except json.JSONDecodeError:
                manifest_error = "manifest_invalid_json"

    evidence.update({
        "script_path": script_path,
        "script_content_path": script_path,
        "script_content_complete": script_complete,
        "script_size": script_size,
        "trace_file": trace_path,
        "trace_tail": "\n".join(trace_text.splitlines()[-limits.log_tail_lines :]),
        "expected_runtime_contract": runtime_contract,
        "actual_environment": actual_environment,
        "runtime_drift": runtime_drift,
        "renderer_manifest_summary": manifest,
        "renderer_manifest_error": manifest_error,
        "action_contract": dict(evidence.get("action_contract") or state.get("pending_action") or {}),
        **failure,
        **stream_info,
    })
    return evidence


def diagnose_execution_evidence(evidence: Dict[str, Any]) -> StructuredDiagnosis:
    """Classify well-supported failures without asking an LLM to invent probes."""
    evidence = dict(evidence or {})
    try:
        returncode = int(evidence.get("returncode") if evidence.get("returncode") is not None else evidence.get("exit_code"))
    except (TypeError, ValueError):
        returncode = None
    failed_command = str(evidence.get("failed_command") or "").strip()
    stderr = str(evidence.get("stderr_full") or evidence.get("stderr_tail") or evidence.get("stderr_summary") or "")
    stdout = str(evidence.get("stdout_tail") or evidence.get("stdout_summary") or "")
    runtime_drift = dict(evidence.get("runtime_drift") or {})
    manifest_error = str(evidence.get("renderer_manifest_error") or "")
    source_evidence = [
        item for item in (
            f"script_source:{evidence.get('script_path')}" if evidence.get("script_path") else "",
            f"shell_trace:{evidence.get('trace_file')}" if evidence.get("trace_file") else "",
            f"failed_command:{failed_command}" if failed_command else "",
            f"exit_code:{returncode}" if returncode is not None else "",
        ) if item
    ]
    if runtime_drift.get("detected"):
        return StructuredDiagnosis(
            root_cause="The generated action crossed a runtime boundary and resolved dynamic environment paths outside the verified RuntimeContract.",
            failure_owner="RUNTIME",
            failure_type="runtime_drift",
            evidence=[*source_evidence, *[f"runtime:{item}" for item in runtime_drift.get("reasons", [])]],
            confidence=0.96,
            repair_strategy="Repair the unchanged step to use verified absolute runtime bindings without a login-shell re-resolution.",
            next_action="PATCH_SCRIPT",
            evidence_sufficient=True,
            collection_complete=True,
        )
    if manifest_error:
        return StructuredDiagnosis(
            root_cause=f"Renderer manifest evidence is unusable: {manifest_error}.",
            failure_owner="RENDERER",
            failure_type="manifest_contract_error",
            evidence=[*source_evidence, f"manifest:{evidence.get('manifest_path') or ''}:{manifest_error}"],
            confidence=0.98,
            repair_strategy="Recover or reconcile the current renderer manifest before executing another action.",
            next_action="RECONCILE_AND_CONTINUE" if manifest_error == "manifest_missing_or_empty" else "PATCH_SCRIPT",
            evidence_sufficient=True,
            collection_complete=True,
        )
    if returncode == 127 or re.search(r"command not found|not found on path|environmentlocationnotfound", "\n".join([stderr, stdout]), re.I):
        contract = dict(evidence.get("expected_runtime_contract") or {})
        missing = [] if contract.get("audits") or contract.get("bindings") else ["expected_runtime_contract"]
        return StructuredDiagnosis(
            root_cause="The generated command could not resolve an executable in the expected runtime.",
            failure_owner="RUNTIME",
            failure_type="command_not_found",
            evidence=source_evidence + ([f"stderr:{_clip(stderr, 1200)}"] if stderr else []),
            confidence=0.94 if not missing else 0.7,
            missing_evidence=missing,
            repair_strategy="Resolve the executable from the verified RuntimeContract and repair the same step.",
            next_action="PATCH_SCRIPT" if not missing else "ESCALATE",
            evidence_sufficient=not missing,
            collection_complete=True,
        )
    if evidence.get("missing_required_input_confirmed"):
        return StructuredDiagnosis(
            root_cause="A required user-owned input is explicitly confirmed missing.",
            failure_owner="INPUT_DATA",
            failure_type="missing_required_input",
            evidence=source_evidence,
            confidence=1.0,
            repair_strategy="Request the missing required input or record a truthful blocked stage.",
            next_action="ASK_USER",
            requires_user_input=True,
            evidence_sufficient=True,
            collection_complete=True,
        )
    if returncode not in {None, 0} and failed_command and (stderr or stdout):
        error_text = stderr or stdout
        return StructuredDiagnosis(
            root_cause="The persisted shell trace identifies a failing command and runtime error for the current generated action.",
            failure_owner="GENERATED_ACTION",
            failure_type="generated_script_failure",
            evidence=[*source_evidence, f"runtime_output:{_clip(error_text, 1600)}"],
            confidence=0.9,
            repair_strategy="Repair the current generated action using the failed line and source-typed runtime evidence, then rerun the same step.",
            next_action="PATCH_SCRIPT",
            evidence_sufficient=True,
            collection_complete=True,
        )
    missing: List[str] = []
    if not evidence.get("script_path"):
        missing.append("failed_script_path")
    if not failed_command:
        missing.append("failed_command")
    if not stderr and not stdout:
        missing.append("tool_error_detail")
    return StructuredDiagnosis(
        root_cause="The execution failed, but bounded local evidence does not establish a repairable root cause.",
        failure_owner="INTERNAL_PROTOCOL",
        failure_type="insufficient_execution_evidence",
        evidence=source_evidence,
        confidence=0.35,
        missing_evidence=missing,
        repair_strategy="Preserve the failure and report the missing internal evidence; do not generate an ad-hoc diagnostic command.",
        next_action="ESCALATE",
        requires_user_input=False,
        evidence_sufficient=False,
        collection_complete=True,
    )


def _bounded_file_snapshot(
    roots: Iterable[Any],
    *,
    limits: DiagnosticLimits,
) -> List[Dict[str, Any]]:
    """Collect bounded metadata and small text previews without running tools."""
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    total_read = 0

    def add(path: Path, root: Path) -> None:
        nonlocal total_read
        if len(rows) >= limits.max_files:
            return
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(root.resolve())
            depth = len(relative.parts)
            stat = resolved.stat()
        except (OSError, ValueError):
            return
        key = str(resolved)
        if key in seen or depth > limits.max_scan_depth:
            return
        seen.add(key)
        row: Dict[str, Any] = {
            "path": key,
            "exists": True,
            "is_file": resolved.is_file(),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "evidence_source": "artifact_snapshot",
        }
        suffixes = [item.lower() for item in resolved.suffixes]
        suffix = suffixes[-1] if suffixes else ""
        compressed = suffix == ".gz"
        logical_suffix = suffixes[-2] if compressed and len(suffixes) > 1 else suffix
        if resolved.is_file() and logical_suffix in _LARGE_BIO_SUFFIXES:
            row["content_policy"] = "metadata_only"
        elif resolved.is_file() and logical_suffix in _SMALL_TEXT_SUFFIXES and not compressed:
            if resolved.name in _INTERNAL_STATE_FILES or resolved.suffix.lower() == ".jsonl":
                row["content_policy"] = "metadata_only_internal_state"
                rows.append(row)
                return
            remaining = max(0, limits.max_total_read_bytes - total_read)
            read_limit = min(limits.max_text_bytes_per_file, remaining)
            if read_limit:
                try:
                    with resolved.open("rb") as handle:
                        raw_body = handle.read(read_limit)
                    body = raw_body.decode("utf-8", errors="replace")
                    total_read += len(raw_body)
                    if logical_suffix in {".log", ".err", ".out", ".trace"}:
                        row["evidence_source"] = "tool_log"
                        lines = body.splitlines()
                        matches = [
                            line for line in lines
                            if re.search(r"error|fatal|exception|traceback|failed", line, re.IGNORECASE)
                        ]
                        row["error_lines"] = matches[-limits.max_error_lines :]
                        row["tail"] = "\n".join(lines[-limits.log_tail_lines :])
                    elif logical_suffix == ".vcf":
                        row["header"] = "\n".join(
                            line for line in body.splitlines() if line.startswith("#")
                        )[:64 * 1024]
                    else:
                        row["preview"] = body
                    row["content_truncated"] = stat.st_size > read_limit
                except OSError:
                    row["read_error"] = True
        rows.append(row)

    for raw_root in roots:
        root = Path(str(raw_root or "")).expanduser()
        if not str(raw_root or "").strip() or not root.exists():
            continue
        if root.is_file():
            add(root, root.parent)
            continue
        queue: List[Tuple[Path, int]] = [(root, 0)]
        while queue and len(rows) < limits.max_files:
            current, depth = queue.pop(0)
            try:
                children = list(itertools.islice(current.iterdir(), limits.max_files))
                children.sort(key=lambda item: item.name)
            except OSError:
                continue
            for path in children:
                add(path, root)
                if path.is_dir() and depth + 1 < limits.max_scan_depth:
                    queue.append((path, depth + 1))
                if len(rows) >= limits.max_files:
                    break
    return rows


def _active_step(
    state: Dict[str, Any],
    plan_state: Dict[str, Any],
    step_id: str = "",
) -> Dict[str, Any]:
    steps = [item for item in plan_state.get("steps", []) or [] if isinstance(item, dict)]
    selected = next(
        (item for item in steps if step_id and str(item.get("step_id") or "") == step_id),
        None,
    )
    if selected is None:
        selected = next(
            (
                item for item in steps
                if str(item.get("status") or "").lower()
                in {"running", "ready", "pending", "diagnosing", "repairing", "retry_pending"}
            ),
            {},
        )
    return dict(selected or {})


def build_current_step_context(
    state: Dict[str, Any],
    plan_state: Optional[Dict[str, Any]] = None,
    *,
    action_contract: Optional[Dict[str, Any]] = None,
    allowed_actions: Optional[Sequence[str]] = None,
) -> CurrentStepContext:
    """Build the only canonical current-step block used by recovery prompts."""
    state = dict(state or {})
    plan_state = dict(plan_state or {})
    action_contract = dict(action_contract or {})
    step_id = str(
        action_contract.get("step_id")
        or state.get("current_step_id")
        or (state.get("execution_evidence") or {}).get("step_id")
        or ""
    )
    step = _active_step(state, plan_state, step_id)
    renderer_execution = dict(step.get("renderer_execution") or {})
    manifest_path = str(
        renderer_execution.get("manifest_path")
        or action_contract.get("manifest_path")
        or ""
    )
    renderer_id = str(
        step.get("renderer_id")
        or renderer_execution.get("renderer_id")
        or action_contract.get("renderer_id")
        or ""
    )
    if manifest_path:
        manifest_status = str(renderer_execution.get("manifest_status") or "registered")
    elif renderer_id or bool(step.get("renderer_composite")):
        manifest_status = "absent"
    else:
        manifest_status = "not_applicable"
    selected_actions = _unique(
        allowed_actions
        or action_contract.get("allowed_actions")
        or state.get("allowed_actions")
        or []
    )
    return CurrentStepContext(
        step_id=str(step.get("step_id") or step_id),
        step_type=str(
            step.get("step_type")
            or ("renderer_composite" if step.get("renderer_composite") else "")
        ),
        status=str(step.get("status") or ""),
        title=str(step.get("title") or step.get("name") or ""),
        purpose=str(step.get("purpose") or "analysis"),
        skill_ids=_unique(step.get("skill_ids") or step.get("skills") or []),
        renderer_id=renderer_id,
        input_bindings=[
            _sanitize(item) if isinstance(item, dict) else {"value": _redact(item)}
            for item in (step.get("input_contracts") or step.get("inputs") or [])
        ][:24],
        output_contract=[
            _sanitize(item) if isinstance(item, dict) else {"value": _redact(item)}
            for item in (step.get("output_contracts") or step.get("outputs") or [])
        ][:24],
        success_criteria=list(step.get("success_criteria") or [])[:24],
        dependencies=_unique(step.get("depends_on") or step.get("dependencies") or []),
        manifest_status=manifest_status,
        manifest_path=manifest_path,
        allowed_actions=selected_actions,
    )


def _validation_projection(state: Dict[str, Any]) -> Dict[str, Any]:
    value = dict(state.get("validation_evidence") or {})
    if not value:
        return {
            "validation_id": "",
            "status": "",
            "deterministic_errors": [],
            "warnings": [],
            "returncode_check": {},
            "manifest_checks": [],
            "artifact_checks": [],
            "failed_success_criteria": [],
            "recoverability_hints": [],
            "llm_advisory": None,
            "llm_advisory_conflict": False,
            "legacy_supervisor_verdict": str(state.get("supervisor_verdict") or ""),
        }
    criteria = [
        dict(item)
        for item in value.get("success_criteria_results", []) or []
        if isinstance(item, dict) and item.get("passed") is not True
    ][:24]
    for index, item in enumerate(criteria, 1):
        item.setdefault("criterion_id", str(item.get("id") or f"criterion-{index}"))
    return _sanitize({
        "validation_id": value.get("validation_id") or "",
        "status": value.get("status") or "",
        "deterministic_errors": [_clip(item, 800) for item in value.get("errors", []) or []][:24],
        "warnings": [_clip(item, 800) for item in value.get("warnings", []) or []][:24],
        "returncode_check": value.get("returncode_check") or {},
        "manifest_checks": list(value.get("manifest_results") or [])[:24],
        "artifact_checks": list(value.get("artifact_results") or [])[:24],
        "failed_success_criteria": criteria,
        "recoverability_hints": list(value.get("recoverability_hints") or [])[:16],
        "llm_advisory": value.get("llm_advisory"),
        "llm_advisory_conflict": bool(value.get("llm_advisory_conflict")),
        "evidence_complete": bool(value.get("evidence_complete")),
        "evidence_source": value.get("evidence_source") or "",
        "legacy_supervisor_verdict": str(state.get("supervisor_verdict") or ""),
    })


def _completion_projection(state: Dict[str, Any]) -> Dict[str, Any]:
    value = dict(state.get("completion_gate_result") or {})
    return _sanitize({
        "passed": value.get("passed"),
        "verdict": value.get("verdict") or "",
        "classification": value.get("classification") or "",
        "reason": value.get("message") or value.get("reason") or "",
        "missing_artifacts": list(value.get("missing_artifacts") or [])[:24],
        "reconciliation": value.get("reconciliation") or {},
    })


def _attempt_family(item: Dict[str, Any]) -> str:
    return str(
        item.get("action_family")
        or item.get("action_type")
        or item.get("action_kind")
        or item.get("command_role")
        or ""
    ).strip().lower()


def _related_attempts(
    history: Sequence[Dict[str, Any]],
    *,
    step_id: str,
    fingerprint: str,
    action_family: str,
) -> List[Dict[str, Any]]:
    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for index, raw in enumerate(history):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item_step = str(item.get("step_id") or item.get("stage_id") or "")
        item_fp = str(
            item.get("error_fingerprint")
            or item.get("failure_fingerprint")
            or item.get("failure_fingerprint_before")
            or ""
        )
        item_family = _attempt_family(item)
        score = 0
        if step_id and item_step == step_id:
            score += 4
        if fingerprint and item_fp == fingerprint:
            score += 3
        if action_family and item_family == action_family:
            score += 2
        if score <= 0:
            continue
        historical_decision = (
            dict(item.get("recovery_decision") or {})
            if isinstance(item.get("recovery_decision"), dict)
            else {}
        )
        scored.append((score, index, {
            "attempt_id": str(item.get("attempt_id") or item.get("execution_id") or ""),
            "step_id": item_step,
            "action_type": str(item.get("action_type") or item.get("action_kind") or ""),
            "command_role": str(item.get("command_role") or ""),
            "strategy_category": str(
                item.get("strategy_category")
                or item.get("recommended_action")
                or item.get("action_type")
                or ""
            ),
            "error_fingerprint": item_fp,
            "returncode": item.get("returncode", item.get("exit_code")),
            "validation_status": str(
                item.get("validation_status")
                or (item.get("validation") or {}).get("status")
                or ""
            ),
            # Durable history keeps the complete decision. The prompt only
            # needs its outcome so the current RecoveryDecision remains the
            # single authoritative decision object.
            "recovery_action": str(
                historical_decision.get("recommended_action")
                or historical_decision.get("next_action")
                or item.get("recommended_action")
                or ""
            ),
            "recovery_decision_id": str(
                historical_decision.get("decision_id") or ""
            ),
            "manifest_delta": item.get("manifest_delta") or {},
            "artifact_delta": item.get("artifact_delta") or {},
            "meaningful_progress": bool(
                item.get("meaningful_progress")
                or str(item.get("progress_classification") or "").lower()
                in {"strong_progress", "moderate_progress", "weak_progress", "resolved"}
            ),
        }))
    scored.sort(key=lambda row: (row[1], row[0]))
    return [_sanitize(row[2]) for row in scored[-3:]]


def build_recovery_context(
    state: Dict[str, Any],
    execution_evidence: Dict[str, Any],
    *,
    plan_state: Optional[Dict[str, Any]] = None,
    task_root: Any = None,
    skill_policy: Optional[Dict[str, Any]] = None,
    recovery_history: Optional[Sequence[Dict[str, Any]]] = None,
    limits: Optional[DiagnosticLimits] = None,
) -> RecoveryContext:
    """Build one bounded recovery view from existing state and durable evidence."""
    limits = limits or DiagnosticLimits()
    state = dict(state or {})
    evidence = enrich_execution_evidence(state, execution_evidence, limits=limits)
    plan_state = dict(plan_state or {})
    # A supplied V2 plan is authoritative and corruption fails closed.  Graph
    # aliases are accepted only when replaying a genuinely legacy state with
    # no durable plan partition.
    task_spec = canonical_task_spec_for_state(plan_state or state)
    step_id = str(evidence.get("step_id") or state.get("current_step_id") or "")
    current_step = _active_step(state, plan_state, step_id)

    try:
        from agent_core.plan_tracker import step_artifact_contracts
        contracts = step_artifact_contracts(current_step)
    except Exception:
        contracts = list(current_step.get("artifacts") or [])

    roots: List[Any] = []
    if task_root:
        roots.append(task_root)
    candidates: List[Any] = [evidence.get("cwd")]
    script_path = str(evidence.get("script_path") or evidence.get("script_file") or "").strip()
    if script_path:
        candidates.append(Path(script_path).parent)
    for candidate in candidates:
        if str(candidate or "").strip() and candidate not in roots:
            roots.append(candidate)
    snapshot = _bounded_file_snapshot(roots, limits=limits)
    observed = list(evidence.get("produced_artifacts") or evidence.get("observed_outputs") or [])
    history = list(recovery_history or state.get("recovery_attempts") or [])
    fingerprint = str(state.get("failure_fingerprint") or evidence.get("failure_fingerprint") or "")
    same_failure_count = sum(
        1 for item in history
        if str(item.get("failure_fingerprint_before") or item.get("failure_fingerprint") or "") == fingerprint
    )
    progress = dict(state.get("recovery_progress") or {})
    action_contract = dict(evidence.get("action_contract") or state.get("pending_action") or {})
    canonical_step = build_current_step_context(
        state,
        plan_state,
        action_contract=action_contract,
        allowed_actions=state.get("allowed_actions") or action_contract.get("allowed_actions") or [],
    )
    effective_user_facts = project_effective_user_facts(
        resolve_effective_user_facts(task_spec),
        audience="recovery",
        step=canonical_step.to_dict(),
        max_chars=6000,
    )
    effective_user_facts.pop("projection_text", None)

    execution = {
        "execution_id": evidence.get("execution_id"),
        "attempt_id": evidence.get("attempt_id"),
        "step_id": step_id,
        "script_path": evidence.get("script_path") or evidence.get("script_file"),
        "code_path": evidence.get("code_path") or evidence.get("approved_code_path"),
        "code_hash": evidence.get("code_hash"),
        # Corrective Generate repairs the immutable model-authored source.  The
        # approved/materialized code remains separately addressable by code_path
        # and script_context for execution forensics.
        "code": evidence.get("generated_code") or evidence.get("code_summary") or "",
        "language": evidence.get("language"),
        "command_role": evidence.get("command_role") or "primary",
        "returncode": evidence.get("returncode", evidence.get("exit_code")),
        "duration_seconds": evidence.get("duration_seconds"),
        "execution_status": evidence.get("execution_status"),
        "stdout_tail": evidence.get("stdout_tail") or evidence.get("stdout_summary") or "",
        "stderr_tail": evidence.get("stderr_full") or evidence.get("stderr_tail") or evidence.get("stderr_summary") or "",
        "stdout_log_path": evidence.get("stdout_log"),
        "stderr_log_path": evidence.get("stderr_log"),
        "stderr_complete": evidence.get("stderr_complete"),
        "stdout_complete": evidence.get("stdout_complete"),
        "failed_line_number": evidence.get("failed_line_number") or evidence.get("failed_script_line"),
        "failed_command": evidence.get("failed_command"),
        "script_context": evidence.get("script_context") or evidence.get("script_preview") or "",
        "trace_file": evidence.get("trace_file"),
        "trace_tail": evidence.get("trace_tail") or "",
        "expected_runtime_contract": evidence.get("expected_runtime_contract") or {},
        "actual_environment": _sanitize(evidence.get("actual_environment") or {}),
        "runtime_drift": evidence.get("runtime_drift") or {},
        "action_id": evidence.get("action_id"),
        "action_kind": evidence.get("action_kind"),
        "action_source": evidence.get("action_source"),
        "action_contract": evidence.get("action_contract") or {},
        "manifest_path": evidence.get("manifest_path"),
        "manifest_action_id": evidence.get("manifest_action_id"),
        "renderer_manifest_summary": evidence.get("renderer_manifest_summary") or {},
        "renderer_manifest_error": evidence.get("renderer_manifest_error") or "",
        "validator": evidence.get("validation") or {},
        "produced_artifacts": observed,
        "fallback_used": bool(evidence.get("fallback_used")),
        "evidence_sources": {
            "code": "script_source",
            "stdout_tail": "stdout",
            "stderr_tail": "stderr",
            "validator": "validator_summary",
        },
    }
    diagnosis = diagnose_execution_evidence(evidence)
    validation = _validation_projection(state)
    completion = _completion_projection(state)
    action_family = _attempt_family(execution)
    recent_attempts = _related_attempts(
        history,
        step_id=canonical_step.step_id,
        fingerprint=fingerprint,
        action_family=action_family,
    )
    log_references = _unique([
        execution.get("script_path"),
        execution.get("code_path"),
        execution.get("stdout_log_path"),
        execution.get("stderr_log_path"),
        execution.get("trace_file"),
        execution.get("manifest_path"),
    ])
    evidence_root = Path(task_root).resolve() if task_root else None
    available_evidence: List[Dict[str, Any]] = []
    evidence_kinds = (
        ("generated_script", (evidence.get("generated_script") or {}).get("path")),
        ("approved_script", (evidence.get("approved_script") or {}).get("path")),
        ("executed_script", (evidence.get("executed_script") or {}).get("path")),
        ("stdout", evidence.get("stdout_log")),
        ("stderr", evidence.get("stderr_log")),
        ("command_trace", evidence.get("trace_file")),
        ("execution_record", evidence.get("execution_record_path")),
        ("failure_evidence", evidence.get("failure_evidence_path")),
    )
    for kind, raw_path in evidence_kinds:
        if not str(raw_path or "").strip():
            continue
        path = Path(str(raw_path)).expanduser()
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(evidence_root).as_posix() if evidence_root else ""
            stat = resolved.stat()
        except (OSError, ValueError):
            continue
        available_evidence.append({
            "kind": kind,
            "relative_path": relative,
            "size": int(stat.st_size),
            "readable": bool(relative),
        })
    machine_findings_by_identity: Dict[str, Dict[str, Any]] = {}
    machine_finding_order: List[str] = []
    for raw_finding in [
        *(evidence.get("controller_machine_audit_findings") or []),
        *((evidence.get("runtime_drift") or {}).get("machine_audit_findings") or []),
    ]:
        if not isinstance(raw_finding, Mapping):
            continue
        finding = _sanitize(raw_finding)
        identity = json.dumps(
            {
                "code": finding.get("code"),
                "expected": finding.get("expected"),
                "observed": finding.get("observed"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if identity not in machine_findings_by_identity:
            machine_finding_order.append(identity)
        previous = machine_findings_by_identity.get(identity) or {}
        if len(finding) >= len(previous):
            machine_findings_by_identity[identity] = finding
    machine_audit_findings = [
        machine_findings_by_identity[identity]
        for identity in machine_finding_order
    ]
    runtime_audit = [
        _sanitize(item) for item in state.get("runtime_audit", []) or [] if isinstance(item, dict)
    ][:8]
    context = RecoveryContext(
        task_id=str(state.get("task_id") or plan_state.get("task_id") or ""),
        step_id=canonical_step.step_id,
        action_id=str(evidence.get("action_id") or evidence.get("execution_id") or ""),
        attempt_id=str(evidence.get("attempt_id") or evidence.get("execution_id") or ""),
        error_class=str(diagnosis.failure_type or ""),
        error_fingerprint=fingerprint,
        task_spec_summary={
            "original_user_request": task_spec.get("original_user_request")
            or task_spec.get("original_request")
            or task_spec.get("goal")
            or state.get("original_user_request")
            or "",
            "goal": task_spec.get("goal") or state.get("requirements_summary") or "",
            "inputs": task_spec.get("inputs") or task_spec.get("input_paths") or [],
            "output_dir": task_spec.get("output_dir") or task_spec.get("declared_output_dir") or "",
            "constraints": task_spec.get("constraints") or [],
            "required_outputs": task_spec.get("required_outputs") or [],
            "requirements_summary": state.get("requirements_summary") or "",
            "task_spec_revision": state.get("task_revision") or task_spec.get("revision") or 0,
        },
        effective_user_facts=effective_user_facts,
        current_step=canonical_step.to_dict(),
        action_contract=_sanitize(action_contract),
        execution=execution,
        validation=validation,
        completion=completion,
        progress=_sanitize({
            **progress,
            "same_failure_count": same_failure_count,
        }),
        runtime={
            "audits": runtime_audit,
            "expected": _sanitize(execution.get("expected_runtime_contract") or {}),
            "actual": _sanitize(execution.get("actual_environment") or {}),
            "drift": _sanitize(execution.get("runtime_drift") or {}),
        },
        machine_audit_findings=machine_audit_findings,
        available_evidence=available_evidence,
        manifest=_sanitize(
            evidence.get("renderer_manifest_summary")
            or state.get("renderer_manifest")
            or {}
        ),
        artifacts=[_sanitize(item) for item in snapshot],
        recent_attempts=recent_attempts,
        already_tried_strategies=_unique(
            item.get("strategy_category") for item in recent_attempts
        ),
        recovery_decision=_sanitize(state.get("recovery_decision") or {}),
        allowed_actions=list(canonical_step.allowed_actions),
        log_references=log_references,
        bounded_log_excerpt=_clip(
            "\n".join(
                item for item in [execution.get("stderr_tail"), execution.get("stdout_tail")] if item
            ),
            6000,
        ),
        conflicts=_sanitize([
            {
                "type": "llm_advisory_conflict",
                "winner": "validation_evidence",
            }
        ] if validation.get("llm_advisory_conflict") else []),
        skill_policy=dict(skill_policy or state.get("skill_policy") or {}),
        diagnosis=diagnosis.to_dict(),
        limits=asdict(limits),
    )
    context.context_id = context.stable_context_id()
    return context


def build_diagnostic_context(
    state: Dict[str, Any],
    execution_evidence: Dict[str, Any],
    **kwargs: Any,
) -> RecoveryContext:
    """Compatibility alias for the canonical Batch-5 RecoveryContext builder."""
    return build_recovery_context(state, execution_evidence, **kwargs)


CATEGORY_REGISTRY = {
    "wrong_environment", "missing_dependency", "dependency_version_mismatch",
    "path_conflict", "executable_mismatch", "unsupported_option", "wrong_subcommand",
    "wrong_argument_order", "wrapper_invocation_bug", "generated_script_bug",
    "shell_quoting_error", "environment_variable_error", "input_missing",
    "input_format_error", "reference_mismatch", "chromosome_name_mismatch",
    "annotation_format_error", "permission_denied", "disk_full", "memory_exhaustion",
    "timeout", "network_failure", "scheduler_failure", "corrupt_file",
    "missing_upstream", "stale_artifact", "user_constraint_conflict",
    "scientific_parameter_conflict", "masked_pipeline_failure", "unknown",
}

CATEGORY_ALIASES = {
    "missing_executable": "missing_dependency",
    "missing_package": "missing_dependency",
    "wrong_path": "path_conflict",
    "wrong_path_or_uri": "path_conflict",
    "format_or_schema_error": "input_format_error",
    "downstream_tool_runtime_error": "unknown",
    "unknown_truncated_log": "unknown",
    "dependency_mismatch": "dependency_version_mismatch",
    "bad_launcher": "executable_mismatch",
    "probable_invocation_or_subcommand_mismatch": "wrong_subcommand",
    "invalid_invocation": "wrong_argument_order",
    "missing_input_or_executable": "input_missing",
    "permission": "permission_denied",
    "resource_exhaustion": "memory_exhaustion",
    "tool_failure": "unknown",
}

RELIABILITY = {
    "executor": 1.00, "argv": 1.00, "stderr": 0.95, "stdout": 0.90,
    "tool_log": 0.95, "script": 0.85, "script_source": 0.70, "shell_trace": 1.00,
    "pipeline_status": 1.00, "artifact": 0.90, "environment": 0.85,
    "probe_result": 0.90, "user_constraint": 0.90, "validator_summary": 0.45,
    "llm_interpretation": 0.40, "llm_inference": 0.40,
}


def normalize_category(category: str) -> str:
    value = _text(category).lower().replace(" ", "_").replace("-", "_")
    value = CATEGORY_ALIASES.get(value, value)
    return value if value in CATEGORY_REGISTRY else "unknown"


@dataclass
class EvidenceItem:
    evidence_id: str
    source_type: str
    source_path: Optional[str] = None
    source_attempt: int = 0
    raw_content: str = ""
    normalized_content: str = ""
    reliability: float = 0.5
    fingerprint: str = ""
    observed_at: str = field(default_factory=_now)
    is_independent: bool = True
    duplicate_of: Optional[str] = None

    def __post_init__(self) -> None:
        self.source_type = _text(self.source_type) or "stdout"
        self.raw_content = _redact(self.raw_content)
        self.normalized_content = self.normalized_content or re.sub(r"\s+", " ", self.raw_content).strip().lower()
        self.reliability = max(0.0, min(1.0, float(self.reliability or RELIABILITY.get(self.source_type, 0.5))))
        self.fingerprint = self.fingerprint or _fingerprint(self.source_type, self.source_path or "", self.normalized_content)

    @classmethod
    def create(cls, source_type: str, content: Any, source_path: Optional[str] = None, source_attempt: int = 0, reliability: Optional[float] = None) -> "EvidenceItem":
        raw = _redact(content)
        content_fingerprint = _fingerprint(raw)
        return cls(
            evidence_id=f"EV_{content_fingerprint[:12]}",
            source_type=source_type,
            source_path=source_path,
            source_attempt=source_attempt,
            raw_content=raw,
            reliability=reliability if reliability is not None else RELIABILITY.get(source_type, 0.5),
            fingerprint=content_fingerprint,
        )

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "EvidenceItem":
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})


@dataclass
class EvidenceAssessment:
    evidence_id: str
    hypothesis_id: str
    relation: str
    strength: str = "moderate"
    directness: str = "direct"
    reason: str = ""
    assessed_at: str = field(default_factory=_now)

    RELATIONS = {"supports", "contradicts", "neutral", "ambiguous"}
    STRENGTHS = {"weak", "moderate", "strong", "decisive"}
    DIRECTNESS = {"direct", "indirect", "inferred"}

    def __post_init__(self) -> None:
        self.relation = _text(self.relation).lower()
        self.strength = _text(self.strength).lower() or "moderate"
        self.directness = _text(self.directness).lower() or "direct"
        if self.relation not in self.RELATIONS:
            raise ValueError(f"Unsupported evidence relation: {self.relation}")
        if self.strength not in self.STRENGTHS:
            raise ValueError(f"Unsupported evidence strength: {self.strength}")
        if self.directness not in self.DIRECTNESS:
            raise ValueError(f"Unsupported evidence directness: {self.directness}")

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "EvidenceAssessment":
        if not isinstance(value, dict):
            raise ValueError("Evidence assessment must be an object")
        forbidden = {"confidence", "new_confidence", "score", "probability"} & set(value)
        if forbidden:
            raise ValueError(f"LLM cannot set confidence fields: {', '.join(sorted(forbidden))}")
        required = {"evidence_id", "hypothesis_id", "relation"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"Evidence assessment missing: {', '.join(sorted(missing))}")
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})


@dataclass
class ConfidenceUpdate:
    hypothesis_id: str
    evidence_id: str
    previous_confidence: float
    new_confidence: float
    delta: float
    relation: str
    calculation: str
    attempt: int = 0
    occurred_at: str = field(default_factory=_now)


@dataclass
class DiagnosticHypothesis:
    hypothesis_id: str
    category: str
    title: str
    description: str
    status: str = "untested"
    confidence: float = 0.0
    prior_confidence: float = 0.0
    evidence_for: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    candidate_probe_ids: List[str] = field(default_factory=list)
    candidate_repair_ids: List[str] = field(default_factory=list)
    related_hypothesis_ids: List[str] = field(default_factory=list)
    created_at_attempt: int = 0
    last_updated_attempt: int = 0
    times_selected: int = 0
    failed_probe_count: int = 0
    confidence_history: List[Dict[str, Any]] = field(default_factory=list)
    superseded_by: Optional[str] = None
    rejection_reason: Optional[str] = None

    def __post_init__(self) -> None:
        self.category = normalize_category(self.category)
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))
        self.prior_confidence = max(0.0, min(1.0, float(self.prior_confidence or 0.0)))
        self.evidence_for = _unique(self.evidence_for)
        self.evidence_against = _unique(self.evidence_against)

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "DiagnosticHypothesis":
        names = cls.__dataclass_fields__
        payload = {name: value[name] for name in names if name in value}
        payload.setdefault("category", value.get("category") or "unknown")
        payload.setdefault("description", value.get("description") or value.get("title") or "unresolved hypothesis")
        payload.setdefault("title", value.get("title") or str(payload["category"]).replace("_", " ").title())
        if "candidate_probe_ids" not in payload and value.get("proposed_probe"):
            payload["candidate_probe_ids"] = list(value.get("proposed_probe") or [])
        if "candidate_repair_ids" not in payload and value.get("proposed_repair"):
            payload["candidate_repair_ids"] = list(value.get("proposed_repair") or [])
        return cls(**payload)


@dataclass
class HypothesisRelation:
    source_id: str
    target_id: str
    relation: str
    reason: str = ""
    created_at: str = field(default_factory=_now)

    RELATIONS = {"competes_with", "depends_on", "refines", "supersedes", "compatible_with"}

    def __post_init__(self) -> None:
        if self.relation not in self.RELATIONS:
            raise ValueError(f"Unsupported hypothesis relation: {self.relation}")


@dataclass
class DiagnosticProbe:
    probe_id: str
    description: str
    command: str
    target_hypothesis_ids: List[str] = field(default_factory=list)
    expected_outcomes: List[Dict[str, Any]] = field(default_factory=list)
    estimated_information_gain: float = 0.0
    execution_cost: float = 0.0
    risk_penalty: float = 0.0
    prerequisites: List[str] = field(default_factory=list)
    already_executed: bool = False
    execution_count: int = 0
    last_result_fingerprint: Optional[str] = None

    def score(self, repetition_penalty: float = 0.0) -> float:
        return float(self.estimated_information_gain) - float(self.execution_cost) - float(self.risk_penalty) - repetition_penalty

    def valid(self) -> bool:
        return bool(self.command.strip() and self.expected_outcomes and self.target_hypothesis_ids)


@dataclass
class ActionSignature:
    action_type: str
    normalized_command: Optional[str] = None
    target_paths: List[str] = field(default_factory=list)
    hypothesis_ids: List[str] = field(default_factory=list)
    repair_hash: Optional[str] = None
    environment_name: Optional[str] = None
    cwd: Optional[str] = None

    @staticmethod
    def normalize_command(command: Any) -> str:
        value = re.sub(r"\s+", " ", _text(command)).strip()
        value = re.sub(r"(?:attempt|run|session)[_-]?\d+", "<id>", value, flags=re.IGNORECASE)
        value = re.sub(r"[\"']([^\"']+)[\"']", r"\1", value)
        return value

    @classmethod
    def create(cls, action_type: str, command: Any = "", target_paths: Optional[Iterable[str]] = None, hypothesis_ids: Optional[Iterable[str]] = None, patches: Optional[Iterable[Dict[str, Any]]] = None, environment_name: Optional[str] = None, cwd: Optional[str] = None) -> "ActionSignature":
        patch_text = json.dumps(list(patches or []), sort_keys=True, ensure_ascii=False)
        return cls(
            action_type=_text(action_type).lower(),
            normalized_command=cls.normalize_command(command) or None,
            target_paths=sorted(_unique(target_paths or [])),
            hypothesis_ids=sorted(_unique(hypothesis_ids or [])),
            repair_hash=_fingerprint(patch_text) if patches else None,
            environment_name=_text(environment_name) or None,
            cwd=_text(cwd) or None,
        )

    def fingerprint(self) -> str:
        return _fingerprint(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False))


@dataclass
class ProgressSnapshot:
    attempt_id: str
    stage_id: str
    active_hypothesis_count: int = 0
    supported_hypothesis_count: int = 0
    confirmed_hypothesis_count: int = 0
    rejected_hypothesis_count: int = 0
    hypothesis_entropy: Optional[float] = None
    top_hypothesis_id: Optional[str] = None
    top_hypothesis_confidence: float = 0.0
    known_failed_command: bool = False
    known_primary_error: bool = False
    failure_fingerprint: Optional[str] = None
    previous_failure_fingerprint: Optional[str] = None
    expected_artifact_count: int = 0
    existing_artifact_count: int = 0
    valid_artifact_count: int = 0
    unique_evidence_count: int = 0
    new_unique_evidence_count: int = 0
    action_type: str = ""
    action_signature: str = ""
    progress_score: float = 0.0
    progress_classification: str = "no_progress"
    no_progress_streak: int = 0
    manifest_status: str = ""
    completed_stage_count: int = 0
    artifact_snapshot_fingerprint: str = ""
    artifact_paths: List[str] = field(default_factory=list)


@dataclass
class ProgressEvent:
    event_id: str
    attempt_id: str
    event_type: str
    value: float
    details: str = ""
    occurred_at: str = field(default_factory=_now)


@dataclass
class ProgressDecision:
    attempt_id: str
    classification: str
    progress_score: float
    action_signature: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    forbidden_next_actions: List[str] = field(default_factory=list)
    recommended_mode: str = "continue"
    reason: str = ""
    no_progress_streak: int = 0
    score: float = 0.0
    new_information: List[str] = field(default_factory=list)
    repeated_actions: List[str] = field(default_factory=list)
    repeated_evidence: List[str] = field(default_factory=list)
    required_strategy_change: bool = False
    recommended_next_mode: str = "continue_probe"
    meaningful_progress: bool = False
    manifest_delta: Dict[str, Any] = field(default_factory=dict)
    artifact_delta: Dict[str, Any] = field(default_factory=dict)
    should_block: bool = False
    step_id: str = ""


class ConfidenceEngine:
    STRENGTH = {"weak": 0.10, "moderate": 0.25, "strong": 0.45, "decisive": 0.80}
    DIRECTNESS = {"direct": 1.00, "indirect": 0.70, "inferred": 0.40}

    def update(self, hypothesis: DiagnosticHypothesis, evidence: EvidenceItem, assessment: EvidenceAssessment, attempt: int = 0) -> ConfidenceUpdate:
        if evidence.evidence_id in hypothesis.evidence_for or evidence.evidence_id in hypothesis.evidence_against:
            return ConfidenceUpdate(hypothesis.hypothesis_id, evidence.evidence_id, hypothesis.confidence, hypothesis.confidence, 0.0, assessment.relation, "duplicate evidence; no update", attempt)
        weight = self.STRENGTH[assessment.strength] * self.DIRECTNESS[assessment.directness] * evidence.reliability
        before = hypothesis.confidence
        if assessment.relation == "supports":
            after = before + weight * (1.0 - before)
            hypothesis.evidence_for.append(evidence.evidence_id)
        elif assessment.relation == "contradicts":
            after = before - weight * before
            hypothesis.evidence_against.append(evidence.evidence_id)
        else:
            after = before
        after = max(0.0, min(1.0, after))
        hypothesis.confidence = after
        if assessment.relation in {"supports", "contradicts"}:
            hypothesis.status = self.status_for(after)
        if assessment.strength == "decisive" and assessment.relation == "supports":
            hypothesis.status = "confirmed"
            hypothesis.confidence = max(after, 0.90)
        if assessment.strength == "decisive" and assessment.relation == "contradicts":
            hypothesis.status = "rejected"
            hypothesis.rejection_reason = assessment.reason or "decisive contradictory evidence"
            hypothesis.confidence = min(after, 0.10)
        update = ConfidenceUpdate(hypothesis.hypothesis_id, evidence.evidence_id, before, hypothesis.confidence, hypothesis.confidence - before, assessment.relation, f"{assessment.relation}: strength={assessment.strength}, directness={assessment.directness}, reliability={evidence.reliability:.2f}", attempt)
        hypothesis.confidence_history.append(asdict(update))
        return update

    @staticmethod
    def status_for(confidence: float) -> str:
        if confidence <= 0.14:
            return "rejected"
        if confidence <= 0.39:
            return "weakened"
        if confidence <= 0.69:
            return "active"
        if confidence <= 0.89:
            return "supported"
        return "confirmed"


class HypothesisManager:
    """Persist and update a bounded set of semantically distinct hypotheses."""

    MAX_ACTIVE = 7
    MAX_TOTAL = 10

    def __init__(self, hypotheses: Optional[Sequence[Dict[str, Any]]] = None, evidence: Optional[Sequence[Dict[str, Any]]] = None, relations: Optional[Sequence[Dict[str, Any]]] = None, probes: Optional[Sequence[Dict[str, Any]]] = None, confidence_updates: Optional[Sequence[Dict[str, Any]]] = None, evidence_assessments: Optional[Sequence[Dict[str, Any]]] = None):
        self.hypotheses: List[DiagnosticHypothesis] = []
        self.evidence: Dict[str, EvidenceItem] = {}
        self.relations: List[HypothesisRelation] = []
        self.probes: Dict[str, DiagnosticProbe] = {}
        self.confidence_updates: List[ConfidenceUpdate] = []
        self.evidence_assessments: List[EvidenceAssessment] = []
        self.engine = ConfidenceEngine()
        for item in evidence or []:
            if isinstance(item, dict):
                try:
                    record = EvidenceItem.from_mapping(item)
                    self.evidence[record.evidence_id] = record
                except (TypeError, ValueError):
                    continue
        for item in hypotheses or []:
            if isinstance(item, dict):
                try:
                    self._append_hypothesis(DiagnosticHypothesis.from_mapping(item))
                except (TypeError, ValueError):
                    continue
        for item in relations or []:
            if isinstance(item, dict):
                try:
                    self.relations.append(HypothesisRelation(**{name: item[name] for name in HypothesisRelation.__dataclass_fields__ if name in item}))
                except (TypeError, ValueError):
                    continue
        for item in probes or []:
            if isinstance(item, dict):
                try:
                    probe = DiagnosticProbe(**{name: item[name] for name in DiagnosticProbe.__dataclass_fields__ if name in item})
                    self.probes[probe.probe_id] = probe
                except (TypeError, ValueError):
                    continue
        for item in confidence_updates or []:
            if isinstance(item, dict):
                try:
                    self.confidence_updates.append(ConfidenceUpdate(**{name: item[name] for name in ConfidenceUpdate.__dataclass_fields__ if name in item}))
                except (TypeError, ValueError):
                    continue
        for item in evidence_assessments or []:
            if isinstance(item, dict):
                try:
                    self.evidence_assessments.append(EvidenceAssessment.from_mapping(item))
                except (TypeError, ValueError):
                    continue

    @staticmethod
    def _semantic_key(category: str, title: str, description: str) -> str:
        words = re.findall(r"[a-z0-9_]+", f"{category} {title} {description}".lower())
        return " ".join(sorted(set(words)))

    def _append_hypothesis(self, hypothesis: DiagnosticHypothesis) -> DiagnosticHypothesis:
        existing = self.find_equivalent(hypothesis.category, hypothesis.title, hypothesis.description)
        if existing:
            existing.related_hypothesis_ids = _unique(existing.related_hypothesis_ids + [hypothesis.hypothesis_id])
            return existing
        self.hypotheses.append(hypothesis)
        return hypothesis

    def find_equivalent(self, category: str, title: str, description: str) -> Optional[DiagnosticHypothesis]:
        category = normalize_category(category)
        key = self._semantic_key(category, title, description)
        candidate_words = set(re.findall(r"[a-z0-9_]+", _text(description).lower()))
        for item in self.hypotheses:
            item_words = set(re.findall(r"[a-z0-9_]+", _text(item.description).lower()))
            overlap = len(candidate_words & item_words) / max(1, min(len(candidate_words), len(item_words)))
            if self._semantic_key(item.category, item.title, item.description) == key or (
                item.category == category and (
                    overlap >= 0.25
                    or (category == "executable_mismatch" and overlap >= 0.15 and bool(candidate_words & item_words & {"launcher", "executable", "path", "version"}))
                )
            ):
                return item
        return None

    def upsert(self, description: str, category: str, confidence: float = 0.0, evidence_for: Optional[List[str]] = None, evidence_against: Optional[List[str]] = None, proposed_probe: Optional[List[str]] = None, proposed_repair: Optional[List[str]] = None, *, title: Optional[str] = None, attempt: int = 0, deterministic: bool = False) -> DiagnosticHypothesis:
        normalized_category = normalize_category(category)
        title = _text(title) or normalized_category.replace("_", " ").title()
        current = self.find_equivalent(normalized_category, title, description)
        if current is None:
            hypothesis_id = f"H_{_fingerprint(normalized_category, title, description)[:10]}"
            prior = max(0.05, min(0.60, float(confidence or 0.0))) if deterministic else 0.20
            current = DiagnosticHypothesis(hypothesis_id, normalized_category, title, _redact(description), prior_confidence=prior, confidence=prior, created_at_attempt=attempt)
            self._append_hypothesis(current)
        if deterministic and confidence:
            current.confidence = max(0.0, min(1.0, float(confidence)))
            current.prior_confidence = current.confidence
            current.status = self.engine.status_for(current.confidence)
        current.evidence_for = _unique(current.evidence_for + list(evidence_for or []), 50)
        current.evidence_against = _unique(current.evidence_against + list(evidence_against or []), 50)
        current.candidate_probe_ids = _unique(current.candidate_probe_ids + list(proposed_probe or []), 20)
        current.candidate_repair_ids = _unique(current.candidate_repair_ids + list(proposed_repair or []), 20)
        current.last_updated_attempt = attempt
        if current.status == "untested":
            current.status = "active"
        return current

    def register_evidence(self, source_type: str, content: Any, source_path: Optional[str] = None, attempt: int = 0, reliability: Optional[float] = None) -> EvidenceItem:
        candidate = EvidenceItem.create(source_type, content, source_path, attempt, reliability)
        for existing in self.evidence.values():
            if existing.fingerprint == candidate.fingerprint:
                candidate.is_independent = False
                candidate.duplicate_of = existing.evidence_id
                return existing
        self.evidence[candidate.evidence_id] = candidate
        return candidate

    def register_analysis(self, analysis: Optional[Dict[str, Any]], context: Optional[Dict[str, Any]] = None, attempt: int = 0) -> List[EvidenceItem]:
        analysis = analysis if isinstance(analysis, dict) else {}
        context = context if isinstance(context, dict) else {}
        records: List[EvidenceItem] = []
        source_records = analysis.get("evidence_records") or []
        for item in source_records:
            if not isinstance(item, dict):
                continue
            source_type = _text(item.get("source")) or "llm_inference"
            source_type = {"script": "script_source", "llm_interpretation": "llm_inference"}.get(source_type, source_type)
            content = item.get("content")
            if content:
                records.append(self.register_evidence(
                    source_type,
                    content,
                    source_path=_text(item.get("path")) or None,
                    attempt=attempt,
                ))
        mappings = (
            ("argv", analysis.get("failed_command") or context.get("command") or context.get("argv")),
            ("stderr", context.get("stderr")),
            ("stdout", context.get("stdout")),
            ("shell_trace", context.get("trace_text")),
            ("pipeline_status", json.dumps(context.get("pipeline_statuses"), ensure_ascii=False) if context.get("pipeline_statuses") else ""),
            ("environment", json.dumps(context.get("environment_snapshot"), ensure_ascii=False) if context.get("environment_snapshot") else ""),
        )
        for source_type, content in mappings:
            if content:
                records.append(self.register_evidence(source_type, content, attempt=attempt))
        for item in analysis.get("related_log_paths", []) or []:
            records.append(self.register_evidence("tool_log", item, source_path=str(item), attempt=attempt))
        return records

    def initialize_from_analysis(self, analysis: Optional[Dict[str, Any]], attempt: int = 0, context: Optional[Dict[str, Any]] = None) -> List[DiagnosticHypothesis]:
        analysis = analysis if isinstance(analysis, dict) else {}
        records = self.register_analysis(analysis, context, attempt)
        text = "\n".join(str(analysis.get(key) or "") for key in ("failed_command", "primary_error_message", "root_cause_category", "tool_name")) .lower()
        candidates: List[Tuple[str, str, str, float]] = []
        category = normalize_category(str(analysis.get("root_cause_category") or "unknown"))
        if category != "unknown":
            candidates.append((category, category.replace("_", " ").title(), str(analysis.get("primary_error_message") or category), 0.45))
        patterns = [
            (r"unknown option|unrecognized option|invalid option", "unsupported_option", "Unsupported option", "The executed tool rejected an option; subcommand or version must be distinguished.", 0.35),
            (r"\bsubcommand\b|\binvocation\b.{0,40}\bmismatch\b|\bargv\b.{0,40}\bmismatch\b|\bcommand\b.{0,40}\bconflict\b", "wrong_subcommand", "Wrong subcommand or dispatch", "The observed tool dispatch may differ from the intended subcommand.", 0.35),
            (r"command not found|not found on path|launcher class not found", "executable_mismatch", "Executable mismatch", "The launcher or executable may not be the one declared by the runtime.", 0.35),
            (r"no such file|does not exist|missing", "input_missing", "Missing input or artifact", "A required path may be missing, stale, or resolved from the wrong root.", 0.30),
            (r"permission denied", "permission_denied", "Permission failure", "The process may lack permission to read or write a required path.", 0.30),
            (r"timeout|timed out|wall time", "timeout", "Timeout", "The action may exceed its execution budget or be stalled.", 0.30),
            (r"segmentation fault|core dumped", "unknown", "Tool runtime crash", "The tool crashed at runtime and needs an isolated diagnostic.", 0.25),
        ]
        for pattern, candidate_category, title, description, prior in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                candidates.append((candidate_category, title, description, prior))
        if not candidates:
            candidates.append(("unknown", "Unresolved execution failure", str(analysis.get("primary_error_message") or "The failure cause is not yet proven."), 0.20))
        created: List[DiagnosticHypothesis] = []
        for candidate_category, title, description, prior in candidates:
            item = self.upsert(description, candidate_category, prior, attempt=attempt, deterministic=True, title=title)
            for record in records:
                if record.source_type in {"argv", "stderr", "shell_trace", "pipeline_status", "environment"}:
                    if record.evidence_id not in item.evidence_for:
                        item.evidence_for.append(record.evidence_id)
            created.append(item)
        active = [item for item in self.hypotheses if item.status not in {"rejected", "superseded", "unresolvable"}]
        for left in active:
            for right in active:
                if left.hypothesis_id < right.hypothesis_id and left.category != right.category:
                    self.add_relation(left.hypothesis_id, right.hypothesis_id, "competes_with", "initial candidate alternatives")
        for index, raw_probe in enumerate(analysis.get("recommended_probes", []) or []):
            if not isinstance(raw_probe, dict) or not _text(raw_probe.get("command")):
                continue
            expected = raw_probe.get("expected_outcomes") or [{
                "condition": _text(raw_probe.get("information_gain")) or "the probe produces a new, distinguishable observation",
                "supports": [],
                "contradicts": [],
            }]
            try:
                information_gain = float(raw_probe.get("estimated_information_gain", raw_probe.get("information_gain_score", 0.5)))
            except (TypeError, ValueError):
                information_gain = 0.5
            self.add_probe(DiagnosticProbe(
                probe_id=str(raw_probe.get("probe_id") or f"P_{_fingerprint(raw_probe.get('command'))[:10]}_{index}"),
                description=_text(raw_probe.get("description") or raw_probe.get("type") or "diagnostic probe"),
                command=_text(raw_probe.get("command")),
                target_hypothesis_ids=[item.hypothesis_id for item in active[:3]],
                expected_outcomes=expected if isinstance(expected, list) else [expected],
                estimated_information_gain=max(0.0, information_gain),
                execution_cost=float(raw_probe.get("execution_cost", 0.1) or 0.1),
                risk_penalty=float(raw_probe.get("risk_penalty", 0.0) or 0.0),
            ))
        return created

    def initialize(self, analysis: Optional[Dict[str, Any]], attempt: int = 0, context: Optional[Dict[str, Any]] = None) -> List[DiagnosticHypothesis]:
        return self.initialize_from_analysis(analysis, attempt=attempt, context=context)

    def add_relation(self, source_id: str, target_id: str, relation: str, reason: str = "") -> HypothesisRelation:
        for existing in self.relations:
            if existing.source_id == source_id and existing.target_id == target_id and existing.relation == relation:
                return existing
        relation_obj = HypothesisRelation(source_id, target_id, relation, reason)
        self.relations.append(relation_obj)
        return relation_obj

    def mark(self, hypothesis_id: str, status: str, reason: str = "") -> None:
        allowed = {"untested", "active", "supported", "confirmed", "weakened", "rejected", "superseded", "unresolvable"}
        if status not in allowed:
            raise ValueError(f"Unsupported hypothesis status: {status}")
        for item in self.hypotheses:
            if item.hypothesis_id == hypothesis_id:
                # A rejected hypothesis is kept rejected until new independent
                # evidence is explicitly assessed; an LLM upsert cannot revive it.
                if item.status == "rejected" and status in {"active", "supported", "confirmed"}:
                    return
                item.status = status
                if reason:
                    item.rejection_reason = _redact(reason)
                return

    def repair_ready(self, hypothesis_id: Optional[str] = None, low_risk: bool = True, user_constraint_conflict: bool = False) -> bool:
        if user_constraint_conflict or not low_risk:
            return False
        item = next((item for item in self.hypotheses if item.hypothesis_id == hypothesis_id), None) if hypothesis_id else max(self.hypotheses, key=lambda item: item.confidence, default=None)
        return bool(item and item.status == "confirmed" and item.confidence >= 0.90 and not item.rejection_reason)

    def add_probe(self, probe: DiagnosticProbe) -> Optional[DiagnosticProbe]:
        if not probe.valid():
            return None
        existing = self.probes.get(probe.probe_id)
        if existing:
            return existing
        self.probes[probe.probe_id] = probe
        for hypothesis_id in probe.target_hypothesis_ids:
            item = next((item for item in self.hypotheses if item.hypothesis_id == hypothesis_id), None)
            if item:
                item.candidate_probe_ids = _unique(item.candidate_probe_ids + [probe.probe_id])
        return probe

    def apply_assessment(self, assessment: EvidenceAssessment, attempt: int = 0) -> Optional[ConfidenceUpdate]:
        evidence = self.evidence.get(assessment.evidence_id)
        hypothesis = next((item for item in self.hypotheses if item.hypothesis_id == assessment.hypothesis_id), None)
        if not evidence or not hypothesis:
            return None
        self.evidence_assessments.append(assessment)
        update = self.engine.update(hypothesis, evidence, assessment, attempt)
        self.confidence_updates.append(update)
        if update.delta and assessment.relation == "contradicts":
            for relation in self.relations:
                if relation.source_id == hypothesis.hypothesis_id and relation.relation == "competes_with":
                    other = next((item for item in self.hypotheses if item.hypothesis_id == relation.target_id), None)
                    if other and other.status not in {"rejected", "superseded"}:
                        other.last_updated_attempt = attempt
                elif relation.target_id == hypothesis.hypothesis_id and relation.relation == "competes_with":
                    other = next((item for item in self.hypotheses if item.hypothesis_id == relation.source_id), None)
                    if other and other.status not in {"rejected", "superseded"}:
                        other.last_updated_attempt = attempt
        return update

    def update(self, assessment: EvidenceAssessment, attempt: int = 0) -> Optional[ConfidenceUpdate]:
        return self.apply_assessment(assessment, attempt=attempt)

    def add_evidence(self, source_type: str, content: Any, source_path: Optional[str] = None, attempt: int = 0, reliability: Optional[float] = None) -> EvidenceItem:
        return self.register_evidence(source_type, content, source_path=source_path, attempt=attempt, reliability=reliability)

    def apply_llm_assessments(self, values: Iterable[Dict[str, Any]], attempt: int = 0) -> List[ConfidenceUpdate]:
        updates: List[ConfidenceUpdate] = []
        for value in values or []:
            try:
                assessment = EvidenceAssessment.from_mapping(value)
                update = self.apply_assessment(assessment, attempt)
                if update:
                    updates.append(update)
            except (TypeError, ValueError):
                continue
        return updates

    def select_probe(self, forbidden: Optional[Iterable[str]] = None) -> Optional[DiagnosticProbe]:
        forbidden_set = {ActionSignature.normalize_command(item) for item in forbidden or []}
        choices: List[Tuple[float, DiagnosticProbe]] = []
        for probe in self.probes.values():
            normalized = ActionSignature.normalize_command(probe.command)
            if not probe.valid() or normalized in forbidden_set:
                continue
            repeat_penalty = 0.75 * probe.execution_count
            if probe.already_executed and probe.last_result_fingerprint:
                repeat_penalty += 2.0
            choices.append((probe.score(repeat_penalty), probe))
        return max(choices, key=lambda item: item[0])[1] if choices and max(choices, key=lambda item: item[0])[0] > 0 else None

    def to_list(self) -> List[Dict[str, Any]]:
        return [asdict(item) for item in self.hypotheses]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hypotheses": self.to_list(),
            "evidence": [asdict(item) for item in self.evidence.values()],
            "relations": [asdict(item) for item in self.relations],
            "probes": [asdict(item) for item in self.probes.values()],
            "confidence_updates": [asdict(item) for item in self.confidence_updates],
            "evidence_assessments": [asdict(item) for item in self.evidence_assessments],
        }


class ProgressTracker:
    """Classify recovery actions from durable evidence, not exit code alone."""

    READONLY = re.compile(r"(?:^|[;&|]\s*)(?:cat|head|tail|ls|find|grep|help|which|type|file|stat)\b", re.IGNORECASE)
    READONLY_ACTIONS = {"inspect_file", "inspect_log"}

    def __init__(self, snapshots: Optional[Sequence[Dict[str, Any]]] = None, events: Optional[Sequence[Dict[str, Any]]] = None, decisions: Optional[Sequence[Dict[str, Any]]] = None):
        self.snapshots: List[ProgressSnapshot] = []
        self.events: List[ProgressEvent] = []
        self.decisions: List[ProgressDecision] = []
        for value in snapshots or []:
            try:
                self.snapshots.append(ProgressSnapshot(**{name: value[name] for name in ProgressSnapshot.__dataclass_fields__ if name in value}))
            except (TypeError, ValueError):
                continue
        for value in events or []:
            try:
                self.events.append(ProgressEvent(**{name: value[name] for name in ProgressEvent.__dataclass_fields__ if name in value}))
            except (TypeError, ValueError):
                continue
        for value in decisions or []:
            try:
                self.decisions.append(ProgressDecision(**{name: value[name] for name in ProgressDecision.__dataclass_fields__ if name in value}))
            except (TypeError, ValueError):
                continue

    @classmethod
    def is_readonly_action(cls, action_type: str) -> bool:
        action = _text(action_type).lower()
        return action in cls.READONLY_ACTIONS or action in {"cat", "head", "tail", "ls", "find", "grep", "help"}

    @classmethod
    def is_readonly_attempt(cls, attempt: Dict[str, Any]) -> bool:
        action = _text(attempt.get("action_type")).lower()
        commands = [_text(value) for value in attempt.get("commands", []) or [] if _text(value)]
        return cls.is_readonly_action(action) or (action == "run_probe" and bool(commands) and all(cls.READONLY.search(command) for command in commands))

    @classmethod
    def score(cls, action_type: str, evidence: Iterable[str], before: str = "", after: str = "", patch_applied: bool = False, artifacts_created: int = 0) -> float:
        score = 0.0
        action = _text(action_type).lower()
        if action in {"patch_file", "replace_command", "regenerate_script"}:
            score += 4
        if patch_applied:
            score += 5
        if before and after and before != after:
            score += 4
        score += 5 * max(0, int(artifacts_created or 0))
        if action in {"inspect_file", "inspect_log", "run_probe"} and any(_text(item) for item in evidence):
            score += 1
        if cls.is_readonly_action(action) and not any(_text(item) for item in evidence):
            return 0.0
        return score

    @staticmethod
    def result(before: str, after: Optional[str], score: float) -> str:
        if after and before and after != before:
            return "success" if score > 0 else "new_failure"
        return "no_progress" if score <= 0 else "same_failure"

    @staticmethod
    def _entropy(hypotheses: Sequence[DiagnosticHypothesis]) -> Optional[float]:
        active = [max(0.0, item.confidence) for item in hypotheses if item.status not in {"rejected", "superseded", "unresolvable"}]
        total = sum(active)
        if not active or total <= 0:
            return None
        return -sum((value / total) * math.log(value / total) for value in active if value > 0)

    def evaluate(self, *, attempt_id: str, stage_id: str, action_type: str, command: str = "", hypotheses: Sequence[DiagnosticHypothesis] = (), evidence: Sequence[EvidenceItem] = (), new_evidence_ids: Optional[Iterable[str]] = None, failure_fingerprint: str = "", previous_failure_fingerprint: str = "", expected_artifacts: int = 0, existing_artifacts: int = 0, valid_artifacts: int = 0, known_failed_command: bool = False, known_primary_error: bool = False, patch_applied: bool = False, repair_verified: bool = False, constraint_conflict: bool = False, target_paths: Optional[Iterable[str]] = None, environment_name: Optional[str] = None, cwd: Optional[str] = None, hypothesis_ids: Optional[Iterable[str]] = None, artifact_snapshot: Optional[Sequence[Dict[str, Any]]] = None, manifest_status: str = "", completed_stage_count: int = 0) -> ProgressDecision:
        existing = next(
            (item for item in self.decisions if str(item.attempt_id or "") == str(attempt_id or "")),
            None,
        )
        if existing is not None:
            return existing
        signature = ActionSignature.create(action_type, command, target_paths, hypothesis_ids, environment_name=environment_name, cwd=cwd)
        sig = signature.fingerprint()
        normalized = signature.normalized_command or ""
        scoped_snapshots = [
            item for item in self.snapshots
            if not str(stage_id or "") or str(item.stage_id or "") == str(stage_id or "")
        ]
        prior = scoped_snapshots[-1] if scoped_snapshots else None
        prior_actions = {item.action_signature for item in scoped_snapshots}
        prior_evidence = {item.evidence_id for item in evidence}
        # The collector marks independent records explicitly.  Do not infer
        # novelty from a command's exit code or from the current row count.
        new_ids = _unique(new_evidence_ids or [])
        duplicate_action = sig in prior_actions
        duplicate_evidence = not new_ids
        events: List[ProgressEvent] = []
        def add(kind: str, value: float, details: str) -> None:
            events.append(ProgressEvent(f"PE_{_fingerprint(attempt_id, kind, details)[:12]}", attempt_id, kind, value, details))
        if known_failed_command and (not prior or not prior.known_failed_command): add("root_command_identified", 5, "failed command is now identified")
        if known_primary_error and (not prior or not prior.known_primary_error): add("root_error_identified", 5, "primary error is now identified")
        if new_ids: add("new_independent_evidence", min(3.0, float(len(new_ids)) * 3), f"new evidence: {','.join(new_ids)}")
        if duplicate_evidence: add("duplicate_evidence", -2, "no new independent evidence")
        if duplicate_action: add("duplicate_action", -3, f"repeated action signature {sig}")
        if previous_failure_fingerprint and failure_fingerprint == previous_failure_fingerprint: add("same_failure", -3, "failure fingerprint unchanged")
        elif previous_failure_fingerprint and failure_fingerprint != previous_failure_fingerprint: add("fingerprint_changed", 3, "failure fingerprint changed")
        if patch_applied: add("repair_applied", 4, "patch was applied")
        if repair_verified: add("repair_verified", 6, "repair validation passed")
        if constraint_conflict: add("constraint_conflict_identified", 4, "user or plan constraint conflict identified")
        snapshot_rows = [
            _sanitize(item) for item in artifact_snapshot or [] if isinstance(item, dict)
        ]
        current_artifacts = {
            str(item.get("path") or item.get("artifact_id") or ""): item
            for item in snapshot_rows
            if str(item.get("path") or item.get("artifact_id") or "")
        }
        prior_paths = set(prior.artifact_paths if prior else current_artifacts)
        current_paths = set(current_artifacts)
        new_artifacts = sorted(current_paths - prior_paths)
        changed_artifacts: List[str] = []
        if prior and prior.artifact_snapshot_fingerprint:
            current_fingerprint = _fingerprint(
                json.dumps(snapshot_rows, ensure_ascii=False, sort_keys=True, default=str)
            )
            if current_fingerprint != prior.artifact_snapshot_fingerprint and not new_artifacts:
                changed_artifacts = sorted(current_paths & prior_paths)
        else:
            current_fingerprint = _fingerprint(
                json.dumps(snapshot_rows, ensure_ascii=False, sort_keys=True, default=str)
            ) if snapshot_rows else ""
        if new_artifacts:
            add("artifact_created", 5, "new artifacts: " + ",".join(new_artifacts[:8]))
        if changed_artifacts:
            add("artifact_changed", 4, "changed artifacts: " + ",".join(changed_artifacts[:8]))
        prior_manifest = str(prior.manifest_status or "") if prior else ""
        if manifest_status and (
            (prior is not None and manifest_status != prior_manifest)
            or (prior is None and manifest_status == "registered")
        ):
            add(
                "manifest_registered" if manifest_status == "registered" else "manifest_changed",
                6,
                f"manifest status changed from {prior_manifest or 'absent'} to {manifest_status}",
            )
        prior_stage_count = int(prior.completed_stage_count or 0) if prior else 0
        if int(completed_stage_count or 0) > prior_stage_count:
            add(
                "completed_stage_advanced",
                6,
                f"completed stages increased from {prior_stage_count} to {int(completed_stage_count or 0)}",
            )
        current_supported = sum(item.status == "supported" for item in hypotheses)
        current_confirmed = sum(item.status == "confirmed" for item in hypotheses)
        current_rejected = sum(item.status == "rejected" for item in hypotheses)
        if prior and current_supported > prior.supported_hypothesis_count:
            add("hypothesis_supported", 3, "an active hypothesis gained support")
        if prior and current_confirmed > prior.confirmed_hypothesis_count:
            add("hypothesis_confirmed", 6, "a hypothesis reached confirmed status")
        if prior and current_rejected > prior.rejected_hypothesis_count:
            add("hypothesis_rejected", 3, "an active hypothesis was rejected")
        current_entropy = self._entropy(hypotheses)
        if prior and current_entropy is not None and prior.hypothesis_entropy is not None and current_entropy < prior.hypothesis_entropy:
            add("new_independent_evidence", 2, "hypothesis entropy decreased")
        existing_artifacts = max(int(existing_artifacts or 0), len(current_paths))
        valid_artifacts = max(int(valid_artifacts or 0), len(current_paths))
        previous_count = prior.existing_artifact_count if prior else 0
        repair_without_artifact_change = action_type in {"patch_file", "replace_command", "rerun_stage", "regenerate_script"} and existing_artifacts == previous_count and bool(prior)
        if existing_artifacts > previous_count and not new_artifacts:
            add("artifact_created", 5, "new valid artifact count")
        elif repair_without_artifact_change: add("regression", -4, "repair action did not increase valid artifacts")
        score = sum(event.value for event in events)
        same_hypotheses = bool(prior and prior.active_hypothesis_count == sum(item.status not in {"rejected", "superseded", "unresolvable"} for item in hypotheses) and prior.top_hypothesis_confidence == (max((item.confidence for item in hypotheses), default=0.0)))
        if repair_without_artifact_change:
            classification = "regression"
        elif action_type in {"inspect_file", "inspect_log", "run_probe"} and duplicate_evidence:
            classification = "no_progress"
        elif same_hypotheses and duplicate_evidence and (not previous_failure_fingerprint or failure_fingerprint == previous_failure_fingerprint):
            classification = "stalled"
        elif score < 0:
            classification = "regression"
        elif score >= 6:
            classification = "strong_progress"
        elif score >= 3:
            classification = "moderate_progress"
        elif score > 0:
            classification = "weak_progress"
        else:
            classification = "no_progress"
        meaningful_progress = any(
            event.event_type in {
                "artifact_created", "artifact_changed", "manifest_registered", "manifest_changed",
                "completed_stage_advanced", "repair_verified", "root_command_identified",
                "root_error_identified", "new_independent_evidence",
            }
            and event.value > 0
            for event in events
        )
        if meaningful_progress and classification in {"no_progress", "stalled", "regression"}:
            classification = "moderate_progress"
        # A changed command is not progress when the manifest, completed
        # stages, artifacts, and independent evidence are unchanged. Keep a
        # task-state no-progress streak even when the detailed classification
        # is ``regression`` (for example, a repair that produced nothing).
        streak = (
            (prior.no_progress_streak + 1) if prior else 1
        ) if not meaningful_progress else 0
        forbidden: List[str] = []
        if duplicate_action or duplicate_evidence:
            forbidden.extend([normalized] if normalized else [])
        if classification in {"no_progress", "stalled", "regression"}:
            forbidden.extend(["cat", "ls", "find", "head", "tail", "grep", "help"])
        forbidden = _unique(forbidden)
        if classification == "regression":
            mode = "rollback_or_block"
            next_mode = "rollback"
        elif classification in {"stalled", "no_progress"} and streak >= 2:
            classification = "exhausted"
            mode = "strategy_change_or_block"
            next_mode = "block"
        elif classification in {"no_progress", "stalled"}:
            mode = "change_probe_or_repair"
            next_mode = "switch_hypothesis"
        else:
            mode = "continue"
            next_mode = "continue_probe"
        new_information = [event.details for event in events if event.event_type in {"new_independent_evidence", "root_command_identified", "root_error_identified", "fingerprint_changed", "artifact_created", "repair_verified"}]
        repeated_actions = [event.details for event in events if event.event_type == "duplicate_action"]
        repeated_evidence = [event.details for event in events if event.event_type == "duplicate_evidence"]
        snapshot = ProgressSnapshot(
            attempt_id=attempt_id, stage_id=stage_id,
            active_hypothesis_count=sum(item.status not in {"rejected", "superseded", "unresolvable"} for item in hypotheses),
            supported_hypothesis_count=sum(item.status == "supported" for item in hypotheses),
            confirmed_hypothesis_count=sum(item.status == "confirmed" for item in hypotheses),
            rejected_hypothesis_count=sum(item.status == "rejected" for item in hypotheses),
            hypothesis_entropy=self._entropy(hypotheses),
            top_hypothesis_id=max(hypotheses, key=lambda item: item.confidence).hypothesis_id if hypotheses else None,
            top_hypothesis_confidence=max((item.confidence for item in hypotheses), default=0.0),
            known_failed_command=known_failed_command, known_primary_error=known_primary_error,
            failure_fingerprint=failure_fingerprint or None, previous_failure_fingerprint=previous_failure_fingerprint or None,
            expected_artifact_count=int(expected_artifacts or 0), existing_artifact_count=int(existing_artifacts or 0), valid_artifact_count=int(valid_artifacts or 0),
            unique_evidence_count=len(prior_evidence), new_unique_evidence_count=len(new_ids), action_type=_text(action_type), action_signature=sig,
            progress_score=score, progress_classification=classification, no_progress_streak=streak,
            manifest_status=str(manifest_status or ""),
            completed_stage_count=int(completed_stage_count or 0),
            artifact_snapshot_fingerprint=current_fingerprint,
            artifact_paths=sorted(current_paths),
        )
        decision = ProgressDecision(
            attempt_id=attempt_id,
            classification=classification,
            progress_score=score,
            action_signature=sig,
            events=[asdict(event) for event in events],
            forbidden_next_actions=forbidden,
            recommended_mode=mode,
            reason=" ; ".join(event.details for event in events) or "No independent diagnostic change",
            no_progress_streak=streak,
            score=score,
            new_information=new_information,
            repeated_actions=repeated_actions,
            repeated_evidence=repeated_evidence,
            required_strategy_change=mode != "continue",
            recommended_next_mode=next_mode,
            meaningful_progress=meaningful_progress,
            should_block=classification == "exhausted",
            step_id=str(stage_id or ""),
            manifest_delta={
                "before": prior_manifest,
                "after": str(manifest_status or ""),
                "completed_stage_count_before": prior_stage_count,
                "completed_stage_count_after": int(completed_stage_count or 0),
            },
            artifact_delta={
                "new_artifacts": new_artifacts,
                "changed_artifacts": changed_artifacts,
                "before_count": len(prior_paths),
                "after_count": len(current_paths),
            },
        )
        self.events.extend(events)
        self.snapshots.append(snapshot)
        self.decisions.append(decision)
        return decision

    def to_dict(self) -> Dict[str, Any]:
        return {"snapshots": [asdict(item) for item in self.snapshots], "events": [asdict(item) for item in self.events], "decisions": [asdict(item) for item in self.decisions]}


def build_progress_evidence(
    state: Dict[str, Any],
    execution_evidence: Dict[str, Any],
    tracker: ProgressTracker,
) -> Dict[str, Any]:
    """Evaluate task-state progress for one attempt through the existing tracker."""
    state = dict(state or {})
    evidence = dict(execution_evidence or {})
    artifact_rows: List[Dict[str, Any]] = []
    for raw_path in evidence.get("produced_artifacts") or evidence.get("observed_outputs") or []:
        path = Path(str(raw_path))
        row: Dict[str, Any] = {"path": str(path)}
        try:
            stat = path.stat()
            row.update({"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        except OSError:
            row.update({"exists": False, "size": 0})
        artifact_rows.append(row)
    manifest = dict(evidence.get("renderer_manifest_summary") or {})
    manifest_path = str(evidence.get("manifest_path") or manifest.get("manifest_path") or "")
    manifest_status = str(
        manifest.get("status")
        or ("registered" if manifest_path and Path(manifest_path).is_file() else "absent")
    )
    completed_ids = list(
        manifest.get("completed_action_ids")
        or manifest.get("completed_actions")
        or []
    )
    progress_step_id = str(evidence.get("step_id") or state.get("current_step_id") or "")
    scoped_snapshots = [
        item for item in tracker.snapshots
        if not progress_step_id or str(item.stage_id or "") == progress_step_id
    ]
    prior_fingerprint = str(scoped_snapshots[-1].failure_fingerprint or "") if scoped_snapshots else ""
    fingerprint = str(
        evidence.get("failure_fingerprint")
        or state.get("failure_fingerprint")
        or ""
    )
    purpose = str(evidence.get("purpose") or "").strip().lower()
    command_role = str(evidence.get("command_role") or "").strip().lower()
    if purpose in {"probe", "diagnostic", "inspection"} or command_role in {
        "probe", "diagnostic", "capability_probe", "optional_probe",
    }:
        action_type = "run_probe"
    else:
        action_type = str(
            evidence.get("action_kind")
            or evidence.get("action_type")
            or evidence.get("command_role")
            or "execution"
        )
    decision = tracker.evaluate(
        attempt_id=str(evidence.get("attempt_id") or evidence.get("execution_id") or "attempt"),
        stage_id=progress_step_id,
        action_type=action_type,
        command=str(evidence.get("command") or evidence.get("code_summary") or ""),
        failure_fingerprint=fingerprint,
        previous_failure_fingerprint=prior_fingerprint,
        expected_artifacts=len(evidence.get("declared_outputs") or []),
        existing_artifacts=sum(1 for item in artifact_rows if item.get("exists")),
        valid_artifacts=sum(
            1 for item in artifact_rows if item.get("exists") and int(item.get("size") or 0) > 0
        ),
        known_failed_command=bool(evidence.get("failed_command")),
        known_primary_error=bool(
            evidence.get("stderr_summary")
            or evidence.get("stderr_tail")
            or evidence.get("root_cause_analysis")
        ),
        artifact_snapshot=artifact_rows,
        manifest_status=manifest_status,
        completed_stage_count=len(completed_ids),
    )
    return _sanitize({
        **asdict(decision),
        "manifest_status_before": decision.manifest_delta.get("before") or "",
        "manifest_status_after": decision.manifest_delta.get("after") or manifest_status,
        "completed_stage_count_before": decision.manifest_delta.get("completed_stage_count_before") or 0,
        "completed_stage_count_after": decision.manifest_delta.get("completed_stage_count_after") or 0,
        "artifact_snapshot_after": artifact_rows,
        "new_artifacts": decision.artifact_delta.get("new_artifacts") or [],
        "changed_artifacts": decision.artifact_delta.get("changed_artifacts") or [],
        "error_fingerprint_history": [
            str(item.failure_fingerprint or "") for item in tracker.snapshots[-3:]
            if str(item.failure_fingerprint or "")
        ],
        "same_strategy_repetition": bool(decision.repeated_actions),
        "same_command_family_repetition": bool(decision.repeated_actions),
        "meaningful_progress": bool(decision.meaningful_progress),
        "no_progress_count": int(decision.no_progress_streak or 0),
    })


def merge_equivalent_hypotheses(hypotheses: Sequence[DiagnosticHypothesis]) -> List[DiagnosticHypothesis]:
    """Return a stable, auditable list without deleting rejected records."""
    manager = HypothesisManager()
    for item in hypotheses or []:
        manager._append_hypothesis(item)
    return manager.hypotheses

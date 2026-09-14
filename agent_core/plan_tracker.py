"""Task-level plan tracking for BioMind execution loops.

The tracker keeps checklist state outside chat messages so memory compression
cannot remove the approved plan or the current execution position.
"""

from __future__ import annotations

from agent_core.execution_authority import is_approved_step_execution, is_recovery_diagnostic_execution

from agent_core.candidate_repair_policy import DEFAULT_CANDIDATE_REPAIRS, candidate_repair_limit

import json
import hashlib
import logging
import os
import re
import tempfile
import fcntl
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent_core.task_spec import (
    TASK_CANONICALIZATION_VERSION,
    canonical_current_task_spec,
    canonical_constraint_hash,
    constraint_requires_plan_projection,
    ensure_task_spec,
    normalize_requirement,
    project_task_spec_to_renderer,
    semantic_constraints_from_task_spec,
    task_record_hash,
    task_semantic_hash,
    task_spec_is_initialized,
    task_spec_revision,
    validate_compiled_constraints,
)
from agent_core.state_contract_store import StateContractStore
from agent_core.phase_timing import phase, timed_phase
from agent_core.runtime_evidence import merge_runtime_evidence, runtime_evidence_state
from agent_core.active_state import (
    ActiveStateInvariantError,
    GenerationMode,
    normal_generation_provenance,
)
from agent_core.recovery_contract import recovery_route_for_disposition
from agent_core.plan_contract import (
    MINIMUM_PLAN_READER_VERSION,
    minimum_reader_for_plan,
    PLAN_CONTRACT_VERSION,
    PermanentInfrastructureError,
    ReaderCompatibilityError,
    StateCASConflict,
    TransientInfrastructureError,
    assert_reader_compatible,
    promote_allocation_intent,
    rewrite_candidate_local_refs,
    structured_plan_candidate_diagnostics,
    typed_task_plan_traceability,
)
from agent_core.workspace_attestation import (
    MUTATION_INTENT_REGISTRATION_SCHEMA_VERSION,
    RUNTIME_ATTESTER_PRODUCER,
    WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
    WorkspaceAttestationError,
    load_verified_mutation_journal_reference,
)

PlanState = Dict[str, Any]
PlanStep = Dict[str, Any]
logger = logging.getLogger("BioMind.PlanTracker")


_LEGACY_TASK_RECORD_FIELDS = {
    "user_directives",
    "effective_user_facts",
    "facts",
    "artifact_bindings",
    "semantic_claims",
    "expected_outputs",
    "change_log",
    "input_alias_audit",
    "semantic_compilation",
    "migration_history",
}


def _externalize_legacy_task_spec_records(
    data_path: str | Path,
    task_id: str,
    state: PlanState,
) -> List[str]:
    """Move legacy history/aliases to immutable objects before canonicalizing.

    This is deliberately a migration-boundary operation.  Canonical TaskSpec
    retains only content-addressed references; raw submissions and discarded
    alias/history bodies are never copied into the current-state document.
    """
    locations: List[Tuple[str, Dict[str, Any]]] = []
    top_level = state.get("task_spec")
    if isinstance(top_level, Mapping):
        locations.append(("task_spec", dict(top_level)))
    current = (state.get("current_task") or {}).get("task_spec")
    if isinstance(current, Mapping):
        locations.append(("current_task.task_spec", dict(current)))
    task_state_spec = (state.get("task_state") or {}).get("task_spec")
    if isinstance(task_state_spec, Mapping):
        locations.append(("task_state.task_spec", dict(task_state_spec)))
    if not locations:
        return []

    store = StateContractStore(data_path, task_id)
    object_refs: List[str] = []
    raw_submission_ref = next(
        (
            str(spec.get("raw_submission_ref") or "")
            for _, spec in locations
            if str(spec.get("raw_submission_ref") or "").startswith("object:sha256:")
        ),
        "",
    )
    envelopes = [
        deepcopy(spec.get("request_envelope"))
        for _, spec in locations
        if isinstance(spec.get("request_envelope"), Mapping)
    ]
    if not raw_submission_ref and envelopes:
        # Store the legacy submission exactly once.  The history object below
        # references this object and intentionally omits the inline envelope.
        raw_submission_ref = store.put_object(
            "raw_user_submission",
            {"legacy_request_envelope": envelopes[0]},
        )
        object_refs.append(raw_submission_ref)

    history: Dict[str, Any] = {}
    for location, spec in locations:
        legacy = {
            field: deepcopy(spec[field])
            for field in sorted(_LEGACY_TASK_RECORD_FIELDS)
            if field in spec and spec[field] not in (None, "", [], {})
        }
        if "request_envelope" in spec:
            legacy["request_envelope_ref"] = raw_submission_ref
        if legacy:
            history[location] = legacy
    if history:
        history_ref = store.put_object(
            "legacy_task_spec_migration",
            {
                "source_schema_versions": {
                    location: int(spec.get("schema_version") or 1)
                    for location, spec in locations
                },
                "legacy_records": history,
            },
        )
        object_refs.append(history_ref)

    if not object_refs and not raw_submission_ref:
        return []
    refs_to_add = sorted(set([*object_refs, raw_submission_ref] if raw_submission_ref else object_refs))

    def attach(spec: Mapping[str, Any]) -> Dict[str, Any]:
        updated = deepcopy(dict(spec))
        refs = {
            str(value)
            for value in updated.get("source_refs", []) or []
            if str(value)
        }
        refs.update(refs_to_add)
        updated["source_refs"] = sorted(refs)
        if raw_submission_ref:
            updated["raw_submission_ref"] = raw_submission_ref
        return updated

    if isinstance(state.get("task_spec"), Mapping):
        state["task_spec"] = attach(state["task_spec"])
    if isinstance((state.get("current_task") or {}).get("task_spec"), Mapping):
        current_task = dict(state.get("current_task") or {})
        current_task["task_spec"] = attach(current_task["task_spec"])
        state["current_task"] = current_task
    if isinstance((state.get("task_state") or {}).get("task_spec"), Mapping):
        task_state = dict(state.get("task_state") or {})
        task_state["task_spec"] = attach(task_state["task_spec"])
        state["task_state"] = task_state
    return refs_to_add


def _current_task_spec(state: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Read CurrentTaskSpec, accepting a legacy alias only for legacy input."""
    raw = dict(state or {})
    if "current_task" in raw:
        current = raw.get("current_task")
        value = current.get("task_spec") if isinstance(current, Mapping) else None
    else:
        value = raw.get("task_spec")
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_in_memory_task_record(state: PlanState) -> None:
    """Complete builder/API records; never heal an explicit persisted mismatch."""
    current = state.get("current_task")
    if not isinstance(current, Mapping):
        return
    payload = current.get("task_spec")
    if "task_revision" in current or not task_spec_is_initialized(payload):
        return
    state["current_task"] = _canonical_task_record(
        payload,
        previous=current,
        generation=int(state.get("state_generation") or 0),
    )
    # This marker never persists. It exists solely for the pre-V2 direct
    # builder API, whose first save historically imported a complete task and
    # plan together. New graph sessions bootstrap through Consultant CAS.
    state["_legacy_builder_task_import_pending"] = True

CANONICAL_TASK_SCHEMA_VERSION = 8
PLAN_SCHEMA_VERSION = 10
PLAN_MIGRATION_VERSION = 10
PLAN_CANONICALIZATION_VERSION = "plan-c14n-v3"
VALIDATION_ISSUE_SCHEMA_VERSION = 1
PLANNING_FAILURE_SCHEMA_VERSION = 1
PROJECTION_COMPLETE = "COMPLETE"
PROJECTION_INCOMPLETE = "INCOMPLETE"
PROJECTION_NOT_APPLICABLE = "NOT_APPLICABLE"
VALIDATION_ISSUE_OWNERS = {
    "planner", "task_spec", "renderer_projection", "external_input", "internal",
}
BLOCKING_VALIDATION_SEVERITIES = {"error", "blocking", "fatal"}


class CanonicalTaskStateError(ValueError):
    """Raised when a V2 CurrentTask record violates payload/revision coherence."""


def _canonical_task_record(
    task_spec: Mapping[str, Any],
    *,
    previous: Optional[Mapping[str, Any]] = None,
    generation: int = 0,
) -> Dict[str, Any]:
    canonical = canonical_current_task_spec(task_spec)
    return {
        "task_spec": deepcopy(canonical),
        "task_revision": task_spec_revision(canonical),
        "task_semantic_hash": task_semantic_hash(canonical),
        "task_record_hash": task_record_hash(canonical),
        "canonicalization_version": TASK_CANONICALIZATION_VERSION,
        "created_generation": int(
            (previous or {}).get("created_generation") or generation or 0
        ),
        "last_mutation_event_id": str(
            (previous or {}).get("last_mutation_event_id") or ""
        ),
        "lifecycle": "initialized",
    }


def canonical_task_spec_for_state(
    state: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Return the sole runtime TaskSpec authority and fail closed for V2.

    A top-level ``task_spec`` is read only when the input is genuinely legacy
    (no ``current_task`` partition and a pre-V2 source schema).  Once a V2
    CurrentTask partition exists, an invalid payload/revision pair is
    corruption; a legacy or graph alias cannot revive it.
    """
    raw = dict(state or {})
    compatibility = raw.get("compatibility")
    source_version = (
        (compatibility or {}).get("source_schema_version")
        if isinstance(compatibility, Mapping)
        else None
    )
    try:
        schema_version = int(
            source_version
            if source_version is not None
            else raw.get("schema_version") or 1
        )
    except (TypeError, ValueError):
        schema_version = 1

    if schema_version < CANONICAL_TASK_SCHEMA_VERSION:
        legacy_current = raw.get("current_task")
        current_payload = (
            legacy_current.get("task_spec")
            if isinstance(legacy_current, Mapping)
            else None
        )
        legacy = (
            current_payload
            if task_spec_is_initialized(current_payload)
            else raw.get("task_spec")
        )
        return (
            canonical_current_task_spec(legacy)
            if task_spec_is_initialized(legacy)
            else {}
        )

    if "current_task" not in raw:
        if schema_version >= CANONICAL_TASK_SCHEMA_VERSION:
            raise CanonicalTaskStateError(
                "CANONICAL_TASK_STATE_CORRUPT: V2 current_task partition is missing"
            )

    current = raw.get("current_task")
    if not isinstance(current, Mapping):
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: current_task must be an object"
        )
    payload = current.get("task_spec")
    try:
        record_revision = int(current.get("task_revision") or 0)
    except (TypeError, ValueError) as exc:
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: task_revision is invalid"
        ) from exc

    if payload is None:
        if record_revision != 0:
            raise CanonicalTaskStateError(
                "CANONICAL_TASK_STATE_CORRUPT: null TaskSpec requires revision 0"
            )
        return {}
    if not task_spec_is_initialized(payload):
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: non-null TaskSpec is not initialized"
        )

    canonical = canonical_current_task_spec(payload)
    payload_revision = task_spec_revision(canonical)
    if record_revision < 1 or record_revision != payload_revision:
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: TaskSpec payload/revision mismatch"
        )
    semantic_hash = str(current.get("task_semantic_hash") or "")
    if semantic_hash and semantic_hash != task_semantic_hash(canonical):
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: TaskSpec semantic hash mismatch"
        )
    record_hash = str(current.get("task_record_hash") or "")
    if record_hash and record_hash != task_record_hash(canonical):
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: TaskSpec record hash mismatch"
        )
    return canonical
PROVENANCE_SCHEMA_VERSION = 1
RENDERER_MANIFEST_SCHEMA_VERSION = 3
MANIFEST_ACTION_KINDS = {
    "primary", "verification", "diagnostic", "recovery", "renderer_invocation",
}
ATTEMPT_CREATED = "CREATED"
ATTEMPT_SCRIPT_READY = "SCRIPT_READY"
ATTEMPT_APPROVED = "APPROVED"
ATTEMPT_STARTING = "STARTING"
ATTEMPT_RUNNING = "RUNNING"
ATTEMPT_PROCESS_EXITED = "PROCESS_EXITED"
ATTEMPT_EVIDENCE_COMMITTED = "EVIDENCE_COMMITTED"
ATTEMPT_VALIDATED = "VALIDATED"
ATTEMPT_VALIDATION_PASSED = "VALIDATION_PASSED"
ATTEMPT_VALIDATION_FAILED = "VALIDATION_FAILED"
ATTEMPT_COMPLETED = "COMPLETED"
ATTEMPT_FAILED = "FAILED"
ATTEMPT_UNKNOWN_AFTER_CRASH = "UNKNOWN_AFTER_CRASH"
ATTEMPT_ACTIVE_STATUSES = {
    ATTEMPT_CREATED,
    ATTEMPT_SCRIPT_READY,
    ATTEMPT_APPROVED,
    ATTEMPT_STARTING,
    ATTEMPT_RUNNING,
    ATTEMPT_UNKNOWN_AFTER_CRASH,
}
PLAN_STEP_FIELDS = (
    "step_id",
    "title",
    "action",
    "purpose",
    "tool",
    "execution_capability_id",
    "capability_contract_hash",
    "inputs",
    "outputs",
    "dependencies",
    "depends_on",
    "step_type",
    "renderer_id",
    # Retained in the semantic hash reader solely so immutable v9 approval
    # identities remain stable. Fresh v10 writers strip both fields.
    "semantic_constraint_ids",
    "semantic_claims",
    "success_criteria",
    "skill_ids",
    "status",
    "risk_level",
)
PLAN_STATUS_PENDING_REVIEW = "pending_review"
PLAN_STATUS_APPROVED = "approved"

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_DIAGNOSING = "diagnosing"
STATUS_REPAIRING = "repairing"
STATUS_RETRY_PENDING = "retry_pending"
STATUS_PENDING_RETRY = STATUS_RETRY_PENDING
STATUS_VERIFICATION_PENDING = "verification_pending"
STATUS_AWAITING_SUPERVISOR = "awaiting_supervisor"
STATUS_WAITING_UPSTREAM = "waiting_upstream"
STATUS_INVALIDATED = "invalidated"
STATUS_DONE = "done"
STATUS_PASSED = "passed"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_SKIPPED_UPSTREAM_BLOCKED = "skipped_upstream_blocked"

TASK_STATUS_RUNNING = "running"
TASK_STATUS_WAITING_USER = "waiting_user"
TASK_STATUS_DIAGNOSING = "diagnosing"
TASK_STATUS_REPAIRING = "repairing"
TASK_STATUS_BLOCKED = "blocked"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_CANCELLED = "cancelled"

TASK_TERMINAL_STATUSES = {
    TASK_STATUS_BLOCKED,
    TASK_STATUS_FAILED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_CANCELLED,
}
STEP_ACTIVE_STATUSES = {
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_DIAGNOSING,
    STATUS_REPAIRING,
    STATUS_RETRY_PENDING,
    STATUS_VERIFICATION_PENDING,
    STATUS_AWAITING_SUPERVISOR,
}
STEP_SUCCESS_STATUSES = {STATUS_DONE, STATUS_PASSED, STATUS_SKIPPED}
STEP_TERMINAL_STATUSES = {
    *STEP_SUCCESS_STATUSES,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_SKIPPED_UPSTREAM_BLOCKED,
    TASK_STATUS_CANCELLED,
}
STEP_RECOVERABLE_STATUSES = {
    STATUS_RETRY_PENDING,
    STATUS_VERIFICATION_PENDING,
    STATUS_WAITING_UPSTREAM,
    STATUS_INVALIDATED,
    STATUS_RUNNING,
    STATUS_FAILED,
}

_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX>!-])\]\s*(?P<title>.+?)\s*$")
_STEP_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:(?:Step\s*(?P<step_a>\d+))|(?P<step_b>\d+)[.)])\s*[:.)-]?\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s*#{2,4}\s+(?P<title>.+?)\s*$")
_STEP_ID_RE = re.compile(r"\bStep(?:\s*[-_]\s*|\s*)(?P<num>\d+)\b", re.IGNORECASE)
_ACTION_FIELD_RE = re.compile(r"(?:^|\|)\s*Action\s*:\s*(?P<value>[A-Za-z_]+)", re.IGNORECASE)
_PURPOSE_FIELD_RE = re.compile(r"(?:^|\|)\s*Purpose\s*:\s*(?P<value>[A-Za-z_]+)", re.IGNORECASE)
_SUCCESS_FIELD_NAMES = (
    "Done",
    "Success",
    "Success criteria",
    "Completion criteria",
    "Verify",
)
_SUCCESS_FIELD_PATTERN = "|".join(re.escape(name) for name in sorted(_SUCCESS_FIELD_NAMES, key=len, reverse=True))
_SUCCESS_FIELD_RE = re.compile(
    rf"(?:^|\|)\s*(?:{_SUCCESS_FIELD_PATTERN})\s*:\s*(?P<value>[^|]*)",
    re.IGNORECASE,
)
_SKILLS_FIELD_RE = re.compile(r"(?:^|\|)\s*Skills\s*:\s*(?P<value>\[[^\]]*\]|[^|]+)", re.IGNORECASE)
_STAGES_FIELD_RE = re.compile(r"(?:^|\|)\s*Stages?\s*:\s*(?P<value>\[[^\]]*\]|[^|]+)", re.IGNORECASE)
_BATCH_FIELD_RE = re.compile(r"(?:^|\|)\s*Batch\s*:\s*(?P<value>[^|]+)", re.IGNORECASE)
_TOOL_FIELD_RE = re.compile(r"(?:^|\|)\s*Tool\s*:\s*(?P<value>[^|]+)", re.IGNORECASE)
_INPUTS_FIELD_RE = re.compile(r"(?:^|\|)\s*Inputs?\s*:\s*(?P<value>[^|]+)", re.IGNORECASE)
_OUTPUTS_FIELD_RE = re.compile(r"(?:^|\|)\s*Outputs?\s*:\s*(?P<value>[^|]+)", re.IGNORECASE)
_DEPENDENCIES_FIELD_RE = re.compile(r"(?:^|\|)\s*(?:Dependencies|Depends on)\s*:\s*(?P<value>[^|]+)", re.IGNORECASE)
_RISK_FIELD_RE = re.compile(r"(?:^|\|)\s*Risk(?: level)?\s*:\s*(?P<value>[^|]+)", re.IGNORECASE)
_SEMANTIC_CLAIMS_FIELD_RE = re.compile(
    r"(?:^|\|)\s*Semantic claims?\s*:\s*",
    re.IGNORECASE,
)

_CONTRACT_PLACEHOLDER_WORDS = {
    "bam", "bcf", "csv", "fasta", "fastq", "file", "files", "gff", "gff3",
    "gtf", "input", "inputs", "java", "path", "paths", "python", "r", "sam",
    "table", "tsv", "vcf", "文件", "输入", "表", "路径",
}

_PROGRESS_START = "<!-- BIOMIND_PLAN_PROGRESS_START -->"
_PROGRESS_END = "<!-- BIOMIND_PLAN_PROGRESS_END -->"

_ARTIFACT_PATH_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9_.+\-/]+"
    r"\.(?:tsv|csv|txt|md|html?|json|ya?ml|vcf(?:\.gz)?|bed(?:graph)?|rds|"
    r"png|pdf|svg|fa|fasta|fq|fastq|gfa|cool|mcool|bam|cram|bai|crai|"
    r"tbi|csi|bin|idx|dict|log|sh|R|r|py))\b"
)
_ARTIFACT_TOKEN_ALIASES = {
    "db": "database",
    "ann": "annotation",
    "prep": "prepare",
}
_ARTIFACT_TOKEN_IGNORED = {
    "artifact", "file", "output", "result", "script", "the",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def plan_output_dir(data_path: str | Path, task_id: str) -> Path:
    return Path(data_path) / "task_outputs" / str(task_id or "default")


def plan_state_path(data_path: str | Path, task_id: str) -> Path:
    return plan_output_dir(data_path, task_id) / "plan_state.json"


def default_plan_file(data_path: str | Path, task_id: str) -> Path:
    return plan_output_dir(data_path, task_id) / "plan.md"


def _status_from_mark(mark: str) -> str:
    if mark in {"x", "X"}:
        return STATUS_DONE
    if mark == ">":
        return STATUS_RUNNING
    if mark == "!":
        return STATUS_BLOCKED
    if mark == "-":
        return STATUS_SKIPPED
    return STATUS_PENDING


def _mark_from_status(status: str) -> str:
    return {
        STATUS_DONE: "x",
        STATUS_PASSED: "x",
        STATUS_RUNNING: ">",
        STATUS_READY: " ",
        STATUS_DIAGNOSING: ">",
        STATUS_REPAIRING: ">",
        STATUS_RETRY_PENDING: ">",
        STATUS_VERIFICATION_PENDING: ">",
        STATUS_AWAITING_SUPERVISOR: ">",
        STATUS_BLOCKED: "!",
        STATUS_FAILED: "!",
        STATUS_SKIPPED: "-",
        STATUS_SKIPPED_UPSTREAM_BLOCKED: "-",
    }.get(str(status), " ")


def _clean_step_title(title: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    return title.rstrip()


def _display_step_title(title: str) -> str:
    value = re.split(
        r"\s+\|\s+(?=(?:Goal|Action|Purpose|Skills|Stage|Batch|Done|Success(?: Criteria)?)\s*:)",
        _clean_step_title(title),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return re.sub(r"^Step\s+\d+\s*:\s*", "", value, flags=re.IGNORECASE)


def step_requires_execution_evidence(step: Dict[str, Any]) -> bool:
    """Return whether a validation step needs local execution evidence, not KB text."""
    value = " ".join(
        [
            str(step.get("title") or ""),
            str(step.get("purpose") or ""),
            *(str(item) for item in step.get("success_criteria", []) or []),
        ]
    ).lower()
    explicit_checks = ("test -", "exists", "non-empty", "nonempty", "readable", "writable")
    local_objects = ("file", "path", "directory", "input", "文件", "路径", "目录", "输入")
    local_checks = ("存在", "非空", "可读", "可写", "完整")
    return any(token in value for token in explicit_checks) or (
        any(token in value for token in local_objects)
        and any(token in value for token in local_checks)
    )


def _step_id_for(title: str, index: int) -> str:
    match = _STEP_ID_RE.search(title or "")
    if match:
        return f"step-{int(match.group('num'))}"
    return f"step-{index}"


def _list_field(value: str) -> List[str]:
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    ignored = {"", "none", "null", "n/a", "na", "[]", "no_skill_needed"}
    values: List[str] = []
    for item in re.split(r"[,，;；\n]+", text):
        cleaned = _valid_contract_text(item)
        if cleaned.lower() in ignored or cleaned in values:
            continue
        values.append(cleaned)
    return values


def _valid_contract_text(value: Any) -> str:
    text = _clean_step_title(str(value or ""))
    previous = None
    while text != previous:
        previous = text
        text = re.sub(r"^(?:\*\*|__|`)+\s*", "", text).strip()
        text = re.sub(r"\s*(?:\*\*|__|`)+$", "", text).strip()
    if text.lower() in {"", "none", "null", "n/a", "na", "-", "unknown", "tbd"}:
        return ""
    # A machine plan needs a concrete path or logical binding, not a display
    # label such as ``VCF`` or ``Java 路径``.  This is parser hygiene over a
    # closed set of artifact-type words; it does not interpret user prose.
    placeholder_tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_+-]+|[\u4e00-\u9fff]+", text)
    }
    if (
        placeholder_tokens
        and placeholder_tokens.issubset(_CONTRACT_PLACEHOLDER_WORDS)
        and not re.search(r"[/\\]|\.[A-Za-z0-9]{1,12}(?:$|[?#])", text)
    ):
        return ""
    return text


class PlanStructureError(ValueError):
    """A Planner response failed the canonical structured-plan contract."""

    def __init__(self, diagnostics: List[Dict[str, Any]], raw: str = "") -> None:
        self.diagnostics = deepcopy(list(diagnostics or []))
        self.raw_hash = hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()
        super().__init__("PLAN_STRUCTURE_INVALID: " + "; ".join(
            str(item.get("message") or item.get("reason") or item)
            for item in self.diagnostics
        ))


def validation_issue(
    code: str,
    *,
    severity: str = "error",
    owner: str = "internal",
    repair_target: str = "",
    field_path: str = "",
    message: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    """Build one stable, route-blind validation observation."""
    normalized_owner = str(owner or "internal").strip().lower()
    if normalized_owner not in VALIDATION_ISSUE_OWNERS:
        normalized_owner = "internal"
    disposition = str(extra.pop("disposition", "") or "").strip().upper()
    if disposition not in {
        "NORMALIZED", "REPAIR_REQUIRED", "PROTOCOL_INVALID", "INTERNAL_INVALID",
    }:
        disposition = (
            "INTERNAL_INVALID" if normalized_owner == "internal" else "REPAIR_REQUIRED"
        )
    body = {
        "code": str(code or "VALIDATION_ERROR").strip().upper(),
        "severity": str(severity or "error").strip().lower(),
        "disposition": disposition,
        "owner": normalized_owner,
        "repair_target": str(repair_target or "").strip(),
        "field_path": str(field_path or "").strip(),
        "message": str(message or "").strip(),
    }
    semantic_extra = _semantic_diagnostic_value(extra)
    violation_code = str(extra.get("violation_code") or body["code"])
    # The display location alone is not an issue identity: two resources, or
    # two conflicting edge pairs, can both fail at ``$.steps``.  Preserve the
    # compiler's complete semantic evidence for candidate-local deduplication.
    # Cross-turn no-progress comparison has its separate event-free projection.
    fingerprint_payload = {
        **body,
        "violation_code": violation_code,
        **semantic_extra,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": VALIDATION_ISSUE_SCHEMA_VERSION,
        **body,
        "violation_code": violation_code,
        "fingerprint": fingerprint,
        **semantic_extra,
    }


_VALIDATION_ROUTE_KEYS = {
    "route", "next_step", "next_route", "target_route", "goto",
    "proposed_route", "effective_route", "dispatch_route",
    "route_resolution", "planner_full_replan", "required_next_action",
    "node_recommended_next_step", "pending_recovery_action",
    "resume_route_hint", "resolved_resume_node", "resume_step",
    "terminal_route", "graph_node", "graph_node_name", "source_graph_node",
    "destination_graph_node", "controller_transition",
    "controller_transition_metadata", "command_goto", "route_event_id",
}
_VALIDATION_VOLATILE_KEYS = {
    "candidate_scope_id", "request_id", "turn_id", "trace_id", "receipt_id",
    "invocation_id", "submission_id", "observation_id", "timestamp",
    "created_at", "updated_at", "observed_at",
}

# Candidate effect declarations legitimately use ``observation_id`` as a
# semantic identity, so it must survive candidate no-progress projections even
# though the same key is volatile in validator observation envelopes.
_CANDIDATE_VOLATILE_KEYS = _VALIDATION_VOLATILE_KEYS - {"observation_id"}

# An explicit empty observed value is evidence, not an omitted field.  Keep
# these bounded diagnostic facts while still dropping empty presentation data.
_VALIDATION_EMPTY_FACT_FIELDS = {
    "actual", "actual_value", "actual_facts", "expected", "expected_contract",
    "required_condition", "accepted_sources", "rejected_sources",
    "recognized_candidate_sources", "dependency_ancestors",
    "producer_step_ids", "source_effect_kinds", "unsupported_effect_ids",
    "actual_issues", "requirements",
}


def _semantic_diagnostic_value(value: Any) -> Any:
    """Remove workflow authority and per-attempt identities from diagnostics.

    Issues describe semantic/protocol facts. Graph routing and volatile request
    identities are deliberately outside that ABI, so equal defects retain an
    equal fingerprint across Planner turns.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_diagnostic_value(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _VALIDATION_ROUTE_KEYS
            and str(key).strip().lower() not in _VALIDATION_VOLATILE_KEYS
            and (
                item not in (None, "", [], {})
                or str(key) in _VALIDATION_EMPTY_FACT_FIELDS
            )
        }
    if isinstance(value, list):
        return [_semantic_diagnostic_value(item) for item in value]
    if isinstance(value, tuple):
        return [_semantic_diagnostic_value(item) for item in value]
    return deepcopy(value)


def _semantic_candidate_value(value: Any) -> Any:
    """Remove control-plane and per-turn metadata from rejected Plan semantics."""
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_candidate_value(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _VALIDATION_ROUTE_KEYS
            and str(key).strip().lower() not in _CANDIDATE_VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_semantic_candidate_value(item) for item in value]
    if isinstance(value, tuple):
        return [_semantic_candidate_value(item) for item in value]
    return deepcopy(value)


def normalize_validation_issue(value: Any) -> Dict[str, Any]:
    raw = (
        _semantic_diagnostic_value(dict(value))
        if isinstance(value, Mapping)
        else {"message": str(value or "")}
    )
    code = raw.get("code") or raw.get("violation_code") or raw.get("field") or "VALIDATION_ERROR"
    owner = raw.get("owner")
    if not owner:
        code_text = str(code).upper()
        if code_text.startswith("RENDERER_"):
            owner = "renderer_projection"
        elif code_text.startswith("PLAN_") or code_text in {
            "REQUIRED_OUTPUT_UNCOVERED", "OUTPUT_PRODUCER_AMBIGUOUS",
            "INVALID_ACTION_FOR_STEP_PURPOSE", "SEMANTIC_CONSTRAINT_UNCOVERED",
            "ACTION", "SUCCESS_CRITERIA", "SEMANTIC_CLAIMS", "INPUTS", "OUTPUTS",
            "DEPENDENCIES", "SKILL_IDS", "STEPS", "ARTIFACT_ROLE_CONFLICT",
        }:
            owner = "planner"
        else:
            owner = "internal"
    step_id = str(raw.get("plan_step_id") or raw.get("step_id") or "")
    field_path = str(raw.get("field_path") or "")
    if not field_path and raw.get("field"):
        field_path = (
            f"plan.steps[{step_id}].{raw.get('field')}"
            if step_id else str(raw.get("field"))
        )
    return validation_issue(
        str(code),
        severity=str(raw.get("severity") or "error"),
        owner=str(owner),
        repair_target=str(raw.get("repair_target") or field_path),
        field_path=field_path,
        message=str(raw.get("message") or raw.get("reason") or raw),
        **{
            key: deepcopy(value)
            for key, value in raw.items()
            if key not in ({
                "code", "severity", "owner", "repair_target",
                "field_path", "message", "reason", "fingerprint", "schema_version",
            } | _VALIDATION_ROUTE_KEYS)
            and (
                value not in (None, "", [], {})
                or key in _VALIDATION_EMPTY_FACT_FIELDS
            )
        },
    )


def _bounded_candidate_semantic_view(
    candidate: Optional[Mapping[str, Any]],
    *,
    max_steps: int = 16,
) -> Dict[str, Any]:
    """Keep enough rejected semantics for correction without copying a Plan."""
    if not isinstance(candidate, Mapping):
        return {}
    steps: List[Dict[str, Any]] = []
    allowed_step_fields = (
        "step_id", "title", "action", "purpose", "tool", "inputs", "outputs",
        "dependencies", "skill_ids", "workflow_stage_ids", "batch_id", "risk_level",
        "execution_capability_id", "operation", "produces_bindings",
        "produces_observations", "workspace_mutations", "explicit_side_effects",
        "step_effect",
    )
    for raw in list(candidate.get("steps") or [])[:max_steps]:
        if not isinstance(raw, Mapping):
            continue
        steps.append({
            key: deepcopy(raw.get(key))
            for key in allowed_step_fields
            if key in raw
        })
    result: Dict[str, Any] = {"steps": steps}
    proposed = []
    for raw in list(candidate.get("proposed_artifacts") or [])[:max_steps]:
        if not isinstance(raw, Mapping):
            continue
        proposed.append({
            key: deepcopy(raw.get(key))
            for key in ("local_ref", "semantic_view", "artifact_contract", "resource_binding")
            if key in raw
        })
    if proposed:
        result["proposed_artifacts"] = proposed
    materializations = []
    for raw in list(candidate.get("task_output_materializations") or [])[:max_steps]:
        if isinstance(raw, Mapping):
            materializations.append({
                "output_ref": str(raw.get("output_ref") or ""),
                "path": str(raw.get("path") or ""),
            })
    if materializations:
        result["task_output_materializations"] = materializations
    # Nested StepEffect metadata is model-authored data.  It must not retain
    # graph destinations or vary the no-progress signature through request IDs.
    return _semantic_candidate_value(result)


def current_planning_failure(
    state: Mapping[str, Any],
    *,
    task_spec_revision_value: Optional[int] = None,
) -> Dict[str, Any]:
    failure = dict((state.get("task_state") or {}).get("planning_failure") or {})
    if str(failure.get("status") or "") != "unresolved":
        return {}
    if (
        task_spec_revision_value is not None
        and int(failure.get("task_spec_revision") or 0)
        != int(task_spec_revision_value)
    ):
        return {}
    return failure


def _planning_failure_machine_issue(value: Any) -> Dict[str, Any]:
    """Reduce one validator result to the durable machine-diagnostic ABI."""
    raw_issue = deepcopy(dict(value)) if isinstance(value, Mapping) else {}
    explicit_message = (
        str(value.get("message") or value.get("reason") or "").strip()
        if isinstance(value, Mapping)
        else str(value or "").strip()
    )
    normalized = normalize_validation_issue(value)
    visible_fields = (
        "code", "violation_code", "message", "severity", "disposition", "field_path",
        "repair_target",
        "step_id", "plan_step_id", "artifact_id", "resource_ref",
        "mutation_id", "mutation_operation", "effect_id", "caused_by",
        "dependency_step_id", "affected_edge_ids", "conflicting_step_ids",
        "unordered_mutator_step_ids", "producer_edges", "conflicting_edges",
        "affected_step_ids", "affected_artifact",
        "affected_output", "expected_identity", "observed_identity",
        "violated_invariant", "required_postcondition", "affected_fields",
        "machine_evidence", "expected_type", "received_type",
        "received_excerpt", "parser_message", "line", "column", "offset",
        "response_excerpt", "constraint_source", "source_ref", "validator_id",
        "consumer_step_id", "producer_step_ids", "actual_value",
        "expected_contract", "possible_effect_kinds", "candidate_sources",
        "expected", "actual", "source", "capability_id",
        "capability_artifact_effects", "unsupported_effects", "unsupported_effect_ids",
        "contract_source", "contract_version", "schema_ref", "requirements",
        "actual_issues",
    )
    issue = _semantic_diagnostic_value({
        key: deepcopy(normalized.get(key))
        for key in visible_fields
        if key in normalized and (
            normalized.get(key) not in (None, "", [], {})
            or key in _VALIDATION_EMPTY_FACT_FIELDS
        )
    })
    if explicit_message:
        issue["message"] = explicit_message[:800]
    else:
        issue.pop("message", None)
    issue["code"] = str(normalized.get("code") or "PLAN_CANDIDATE_INVALID")
    issue["field_path"] = str(normalized.get("field_path") or "$")
    affected_steps = [
        str(item)
        for item in (
            normalized.get("affected_step_ids")
            or [normalized.get("plan_step_id") or normalized.get("step_id")]
        )
        if str(item or "")
    ]
    issue["affected_step_ids"] = affected_steps
    issue.setdefault("affected_artifact", {})
    issue.setdefault("affected_output", {})

    # Dataflow validators already emit a bounded, typed source analysis.  Keep
    # that ABI intact for the next Planner turn instead of reducing it to only
    # a code and a generic message.  The allowlist deliberately excludes the
    # candidate body, raw provider response, logs, and arbitrary validator
    # payloads.  Empty source lists are meaningful evidence (the compiler
    # completed its search and found no source), so copy them from the raw
    # issue after generic diagnostic normalization has removed empty values.
    dataflow_evidence_fields = (
        "not_evaluated",
        "issue_id", "candidate_revision", "validator_id",
        "authority", "blocking", "explicit_contract_source",
        "contract_revision", "resource_id", "canonical_resource_id",
        "binding_evidence", "consumer_step", "consumer_step_id",
        "dependency_ancestors", "recognized_candidate_sources",
        "source_effect_kinds", "accepted_sources", "rejected_sources",
        "producer_step_ids", "affected_fields", "actual_facts",
        "required_condition",
    )
    for key in dataflow_evidence_fields:
        if key not in raw_issue:
            continue
        issue[key] = _semantic_diagnostic_value(deepcopy(raw_issue[key]))
    if (
        str(issue.get("validator_id") or "")
        == "biomind.plan_dataflow_compiler.v1"
        or str(issue.get("source") or "") == "compiled_plan_dataflow"
    ):
        for key in (
            "dependency_ancestors", "recognized_candidate_sources",
            "source_effect_kinds", "accepted_sources", "rejected_sources",
            "producer_step_ids",
        ):
            issue.setdefault(key, [])

    machine_evidence = dict(issue.get("machine_evidence") or {})
    for key in (
        "not_evaluated",
        "dependency_step_id", "consumer_step_id", "producer_step_ids",
        "affected_output_ids", "cycle", "artifact_ref", "reason_code",
        "actual_value", "expected_contract", "selected_skill_id",
        "validator_id", "possible_effect_kinds", "candidate_sources",
        "expected", "actual", "source", "capability_id",
        "violation_code", "unsupported_effect_ids", "canonical_resource_id",
        "resource_id", "mutation_id", "affected_edge_ids", "conflicting_edges",
    ):
        if normalized.get(key) not in (None, "", [], {}):
            machine_evidence.setdefault(key, deepcopy(normalized.get(key)))
    issue["machine_evidence"] = machine_evidence
    return issue


_PLANNER_CORRECTION_STRUCTURE_FIELDS = (
    "action", "purpose", "tool", "inputs", "outputs", "dependencies",
    "skill_ids", "workflow_stage_ids", "batch_id", "risk_level",
    "execution_capability_id", "operation", "produces_bindings",
    "produces_observations", "workspace_mutations", "explicit_side_effects",
    "step_effect", "parameters", "generate_binding_input_contract",
)


def _planning_issue_affected_step_ids(
    issues: Iterable[Mapping[str, Any]],
) -> List[str]:
    """Collect the validator-owned affected subgraph without task semantics."""
    values: set[str] = set()
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        for value in issue.get("affected_step_ids", []) or []:
            if str(value or ""):
                values.add(str(value))
        evidence = dict(issue.get("machine_evidence") or {})
        for key in ("consumer_step_id", "dependency_step_id"):
            if str(evidence.get(key) or ""):
                values.add(str(evidence[key]))
        for key in ("producer_step_ids", "cycle"):
            for value in evidence.get(key, []) or []:
                if str(value or ""):
                    values.add(str(value))
    return sorted(values)


def _candidate_correction_signature(
    candidate: Optional[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
) -> str:
    """Hash only validator-relevant candidate structure for no-progress checks."""
    if not isinstance(candidate, Mapping):
        return ""
    semantic_candidate = _semantic_candidate_value(_candidate_comparison_view(candidate, candidate))
    affected_step_ids = set(_planning_issue_affected_step_ids(issues))
    affected_fields = {
        str(field)
        for issue in issues
        if isinstance(issue, Mapping)
        for field in (issue.get("affected_fields") or [])
        if str(field) in _PLANNER_CORRECTION_STRUCTURE_FIELDS
    }
    if not affected_fields:
        affected_fields = set(_PLANNER_CORRECTION_STRUCTURE_FIELDS)
    projected_steps: List[Dict[str, Any]] = []
    for raw in semantic_candidate.get("steps", []) or []:
        if not isinstance(raw, Mapping):
            continue
        step_id = str(raw.get("step_id") or "")
        if affected_step_ids and step_id not in affected_step_ids:
            continue
        projected_steps.append({
            "step_id": step_id,
            **{
                field: deepcopy(raw.get(field))
                for field in sorted(affected_fields)
                if field in raw
            },
        })
    signature_payload: Dict[str, Any] = {
        "affected_step_ids": sorted(affected_step_ids),
        "affected_fields": sorted(affected_fields),
        "steps": sorted(
            projected_steps,
            key=lambda row: str(row.get("step_id") or ""),
        ),
    }
    # Candidate-local resource identity and lifecycle declarations can repair a
    # step's dataflow without changing its input/output spelling. They belong
    # in the structural signature even when the compiler names affected steps.
    signature_payload["proposed_artifacts"] = [
        {
            key: deepcopy(raw.get(key))
            for key in ("local_ref", "artifact_contract", "resource_binding", "role_associations")
            if key in raw
        }
        for raw in semantic_candidate.get("proposed_artifacts", []) or []
        if isinstance(raw, Mapping)
    ]
    signature_payload["task_output_materializations"] = deepcopy(
        semantic_candidate.get("task_output_materializations") or []
    )
    return hashlib.sha256(
        json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _candidate_comparison_view(value: Any, candidate: Optional[Mapping[str, Any]]) -> Any:
    candidate = dict(candidate or {})
    replacements = {}
    declared = {str(row.get("local_ref")) for row in candidate.get("proposed_artifacts", []) if isinstance(row, Mapping)}
    scope = str(candidate.get("candidate_scope_id") or (candidate.get("candidate_scope") or {}).get("candidate_scope_id") or "")
    for local in declared:
        if scope:
            replacements[f"candidate:{scope}:{local}"] = "candidate-local:" + local
    for row in (candidate.get("plan_resource_bindings") or {}).values():
        if not isinstance(row, Mapping):
            continue
        local = str(row.get("local_ref") or "")
        identity = str(row.get("canonical_resource_id") or "")
        row_scope = str(row.get("candidate_scope_id") or "")
        if local in declared and row_scope and identity == f"candidate:{row_scope}:{local}":
            replacements[identity] = "candidate-local:" + local
    def project(item):
        if isinstance(item, Mapping):
            return {replacements.get(str(k), str(k)): project(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [project(v) for v in item]
        return replacements.get(item, item) if isinstance(item, str) else deepcopy(item)
    return project(value)


def _planning_issue_fingerprint_value(value: Any) -> Any:
    """Remove event-only identity while retaining semantic failure evidence."""

    semantic = _semantic_diagnostic_value(value)
    if isinstance(semantic, Mapping):
        return {
            str(key): _planning_issue_fingerprint_value(item)
            for key, item in semantic.items()
            if str(key) not in {"issue_id", "binding_id"}
        }
    if isinstance(semantic, list):
        return [_planning_issue_fingerprint_value(item) for item in semantic]
    return deepcopy(semantic)


def _planning_issue_fingerprint_view(issue: Mapping[str, Any]) -> Dict[str, Any]:
    """Project one issue onto its route-free, source-complete semantic facts."""

    fields = (
        "code", "violation_code", "severity", "disposition", "field_path", "repair_target",
        "step_id", "plan_step_id", "artifact_id", "resource_ref",
        "mutation_id", "mutation_operation", "effect_id", "caused_by",
        "affected_edge_ids", "conflicting_step_ids", "unordered_mutator_step_ids",
        "producer_edges", "conflicting_edges", "unsupported_effect_ids",
        "contract_source", "constraint_source", "source_ref", "contract_version",
        "expected", "actual", "actual_value", "expected_contract", "source",
        "capability_id", "capability_artifact_effects", "unsupported_effects",
        "affected_step_ids", "affected_artifact", "affected_output",
        "expected_identity", "observed_identity", "violated_invariant",
        "required_postcondition", "affected_fields", "machine_evidence",
        "expected_type", "received_type", "received_excerpt",
        "parser_message", "line", "column", "offset", "response_excerpt",
        "validator_id", "authority", "blocking", "explicit_contract_source",
        "contract_revision", "resource_id", "canonical_resource_id",
        "binding_evidence", "consumer_step", "consumer_step_id",
        "dependency_ancestors", "recognized_candidate_sources",
        "source_effect_kinds", "accepted_sources", "rejected_sources",
        "producer_step_ids", "actual_facts", "required_condition",
    )
    return _planning_issue_fingerprint_value({
        key: deepcopy(issue.get(key))
        for key in fields
        if issue.get(key) not in (None, "", [], {})
    })


def record_planning_failure(
    state: PlanState,
    *,
    validation_issues: Iterable[Any],
    planning_epoch_id: str,
    task_spec_revision_value: int,
    validation_source: str,
    candidate: Optional[Mapping[str, Any]] = None,
    previous_correction: Optional[Mapping[str, Any]] = None,
    requirements: Iterable[Mapping[str, Any]] = (),
    terminal: bool = False,
    terminal_diagnostic: Optional[Mapping[str, Any]] = None,
) -> PlanState:
    """Persist the unique Planner correction capsule in task_state."""
    result = deepcopy(state)
    task_state = dict(result.get("task_state") or {})
    task_state.pop("planner_candidate_rejection", None)
    issues = [_planning_failure_machine_issue(item) for item in validation_issues]
    if not issues:
        issues = [_planning_failure_machine_issue({
            "code": "PLAN_CANDIDATE_INVALID",
            "field_path": "$",
            "machine_evidence": {
                "validation_source": str(
                    validation_source or "planner_candidate_admission"
                ),
            },
        })]
    bounded_candidate = _bounded_candidate_semantic_view(candidate)
    issue_fingerprint_payload = {
        "task_spec_revision": int(task_spec_revision_value),
        "planning_epoch_id": str(planning_epoch_id or ""),
        "validation_source": str(validation_source or "planner_candidate_admission"),
        "issues": [
            _planning_issue_fingerprint_view(_candidate_comparison_view(issue, candidate))
            for issue in issues
        ],
    }
    issue_fingerprint = hashlib.sha256(
        json.dumps(
            issue_fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    candidate_correction_signature = _candidate_correction_signature(
        candidate,
        issues,
    )
    fingerprint_payload = {
        **issue_fingerprint_payload,
        "candidate_correction_signature": candidate_correction_signature,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    epoch = dict(task_state.get("planning_epoch") or {})
    # Scope comparisons to trusted task/protocol/Skill facts; never provider narration.
    comparison_facts = hashlib.sha256(json.dumps({
        "task_revision": task_spec_revision_value,
        "requirements": list(requirements),
        "protocol": epoch.get("planner_response_protocol_version"),
        "task_identity": epoch.get("base_task_semantic_identity"),
    }, sort_keys=True, default=str).encode()).hexdigest()
    seen = list(epoch.get("rejected_candidate_comparisons") or []) if str(epoch.get("planning_epoch_id") or "") == str(planning_epoch_id) else []
    seen_match = next((row for row in seen if row.get("signature") == candidate_correction_signature
                       and row.get("issues") == issue_fingerprint and row.get("facts") == comparison_facts), None)
    prior = dict(task_state.get("planning_failure") or {})
    same_lifecycle = bool(
        str(prior.get("status") or "") == "unresolved"
        and str(prior.get("planning_epoch_id") or "")
        == str(planning_epoch_id or "")
        and int(prior.get("task_spec_revision") or 0)
        == int(task_spec_revision_value)
    )
    same_issue = bool(
        same_lifecycle
        and str(prior.get("issue_fingerprint") or "") == issue_fingerprint
    )
    repeated = bool(same_lifecycle and (seen_match or (
        not epoch.get("planning_epoch_id") and same_issue
        and prior.get("candidate_correction_signature") == candidate_correction_signature
        and prior.get("comparison_facts") == comparison_facts)))
    claim_id = str((epoch.get("call_claim") or {}).get("claim_id") or "")
    if same_lifecycle and claim_id and prior.get("comparison_claim_id") == claim_id and prior.get("failure_fingerprint") == fingerprint:
        return result  # Idempotent replay, not a new rejection/ledger entry.
    seen.append({"signature": candidate_correction_signature, "issues": issue_fingerprint,
                 "facts": comparison_facts, "claim_id": claim_id})
    epoch["rejected_candidate_comparisons"] = seen[-4:]
    task_state["planning_epoch"] = epoch
    attempt = int(prior.get("attempt") or 0) + 1 if same_lifecycle else 1
    capsule: Dict[str, Any] = {
        "schema_version": PLANNING_FAILURE_SCHEMA_VERSION,
        "status": "terminal" if terminal else "unresolved",
        "attempt": max(1, attempt),
        "repeat_count": (
            int(prior.get("repeat_count") or 0) + 1
            if repeated
            else 0
        ),
        "planning_epoch_id": str(planning_epoch_id or ""),
        "task_spec_revision": int(task_spec_revision_value),
        "validation_source": str(validation_source or "planner_candidate_admission"),
        "validation_issues": issues,
        # Compatibility readers retain validation_issues. New readers can
        # distinguish checks that actually ran from desired target contracts.
        "actual_issues": deepcopy(issues),
        "requirements": _semantic_diagnostic_value([
            dict(item) for item in requirements if isinstance(item, Mapping)
        ]),
        "failure_fingerprint": fingerprint,
        "issue_fingerprint": issue_fingerprint,
        "candidate_correction_signature": candidate_correction_signature,
        "comparison_facts": comparison_facts,
        "comparison_claim_id": claim_id,
        "seen_rejection_count": len(seen),
        "cycle_detected": repeated,
        "correction_progress": (
            "no_structural_progress"
            if repeated
            else "affected_structure_changed"
            if same_issue
            else "new_failure_objective"
        ),
        "previous_correction": deepcopy(dict(previous_correction or {})),
        "terminal_diagnostic": deepcopy(dict(terminal_diagnostic or {})),
        "updated_at": _now(),
    }
    if same_lifecycle:
        # Older capsules did not retain the first rejection. Do not re-label
        # their most recent issue as the historical origin on a resumed run.
        capsule["primary_failure"] = deepcopy(prior.get("primary_failure") or {
            "status": "not_persisted", "reason_code": "PLANNER_ORIGIN_NOT_PERSISTED",
        })
        if prior.get("primary_context_ref"):
            capsule["primary_context_ref"] = str(prior["primary_context_ref"])
    else:
        capsule["primary_failure"] = {
            "status": "observed", "task_id": str(result.get("task_id") or ""),
            "planning_epoch_id": str(planning_epoch_id or ""),
            "task_spec_revision": int(task_spec_revision_value),
            "attempt": max(1, attempt), "validation_source": str(validation_source),
            "failure_fingerprint": fingerprint,
            "validation_issues": deepcopy(issues),
        }
    if bounded_candidate:
        capsule["previous_candidate"] = bounded_candidate
    current_plan = dict(result.get("current_plan") or {})
    snapshot_id = str(current_plan.get("plan_snapshot_id") or "")
    if snapshot_id:
        capsule["plan_snapshot_id"] = snapshot_id
        capsule["plan_revision"] = int(
            current_plan.get("plan_revision")
            or result.get("plan_revision")
            or 0
        )
    task_state["planning_failure"] = capsule
    # Keep prior Plan/submission/approval records, but publish this producer's
    # current rejection. Omitted success fields must not select an old receipt.
    from agent_core.planner_loop import new_planner_loop_state
    loop = dict(task_state.get("planner_loop") or new_planner_loop_state(str(result.get("task_id") or "default")))
    loop.update({"phase": "candidate_rejected", "status": "active",
                 "agent_disposition": "continue", "updated_at": _now()})
    task_state["planner_loop"] = loop
    epoch = dict(task_state.get("planning_epoch") or {})
    if (
        not terminal
        and epoch
        and str(epoch.get("status") or "") in {"active", "candidate_rejected"}
        and str(epoch.get("planning_epoch_id") or "")
        == str(planning_epoch_id or "")
    ):
        # A rejected candidate is a correction turn inside the same semantic
        # PlanningEpoch.  Ending the epoch here would reset its call budget and
        # discard failure continuity on the next Planner dispatch.
        epoch["status"] = "active"
        task_state["planning_epoch"] = epoch
    if terminal:
        epoch = dict(task_state.get("planning_epoch") or {})
        epoch["status"] = "exhausted"
        task_state["planning_epoch"] = epoch
        task_state["planner_loop"].update({"status": "terminal", "phase": "failed", "agent_disposition": "terminal"})
    result["task_state"] = task_state
    return result


def terminalize_planning_failure(
    state: PlanState,
    *,
    planning_epoch_id: str,
    task_spec_revision_value: int,
    terminal_diagnostic: Mapping[str, Any],
    fallback_validation_issues: Iterable[Any] = (),
) -> PlanState:
    """Close the current failure without erasing its diagnostic continuity."""
    result = deepcopy(state)
    task_state = dict(result.get("task_state") or {})
    failure = dict(task_state.get("planning_failure") or {})
    same_lifecycle = bool(
        str(failure.get("status") or "") == "unresolved"
        and str(failure.get("planning_epoch_id") or "")
        == str(planning_epoch_id or "")
        and int(failure.get("task_spec_revision") or 0)
        == int(task_spec_revision_value)
    )
    if not same_lifecycle:
        return record_planning_failure(
            result,
            validation_issues=list(fallback_validation_issues),
            planning_epoch_id=planning_epoch_id,
            task_spec_revision_value=task_spec_revision_value,
            validation_source="planning_epoch_budget",
            terminal=True,
            terminal_diagnostic=terminal_diagnostic,
        )
    epoch = dict(task_state.get("planning_epoch") or {})
    epoch["status"] = "exhausted"
    task_state["planning_epoch"] = epoch
    loop = dict(task_state.get("planner_loop") or {})
    loop.update({"status": "terminal", "phase": "failed", "agent_disposition": "terminal"})
    task_state["planner_loop"] = loop
    failure["status"] = "terminal"
    failure["terminal_diagnostic"] = deepcopy(dict(terminal_diagnostic or {}))
    failure["updated_at"] = _now()
    task_state["planning_failure"] = failure
    result["task_state"] = task_state
    return result


def resolve_planning_failure(
    state: PlanState,
    *,
    outcome_code: str = "PLAN_CANDIDATE_ACCEPTED",
) -> PlanState:
    result = deepcopy(state)
    task_state = dict(result.get("task_state") or {})
    task_state.pop("planner_candidate_rejection", None)
    failure = dict(task_state.get("planning_failure") or {})
    if failure:
        failure["status"] = "resolved"
        failure["previous_correction"] = {
            "status": "accepted",
            "outcome_code": str(outcome_code),
        }
        failure["updated_at"] = _now()
        task_state["planning_failure"] = failure
    result["task_state"] = task_state
    return result


def supersede_planning_failure(
    state: PlanState,
    *,
    task_spec_revision_value: int,
) -> PlanState:
    result = deepcopy(state)
    task_state = dict(result.get("task_state") or {})
    task_state.pop("planner_candidate_rejection", None)
    failure = dict(task_state.get("planning_failure") or {})
    if (
        failure
        and str(failure.get("status") or "") == "unresolved"
        and int(failure.get("task_spec_revision") or 0)
        != int(task_spec_revision_value)
    ):
        failure["status"] = "superseded"
        failure["previous_correction"] = {
            "status": "superseded",
            "outcome_code": "TASKSPEC_REVISION_CHANGED",
        }
        failure["updated_at"] = _now()
        task_state["planning_failure"] = failure
    result["task_state"] = task_state
    return result


def _semantic_claims_validation(value: Any) -> Dict[str, Any]:
    if isinstance(value, list):
        payload = value
    else:
        try:
            payload = json.loads(str(value or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "valid": False,
                "constraints": [],
                "errors": [{"field": "semantic_claims", "reason": f"invalid JSON: {exc}"}],
                "raw": deepcopy(value),
            }
    validation = validate_compiled_constraints(payload)
    if not validation.get("valid"):
        return {
            "valid": False,
            "constraints": [],
            "errors": deepcopy(list(validation.get("errors") or [])),
            "raw": deepcopy(payload),
        }
    return {
        "valid": True,
        "constraints": [dict(item) for item in validation.get("constraints", [])],
        "errors": [],
        "raw": deepcopy(payload),
    }


def _semantic_claims_field(value: Any) -> List[Dict[str, Any]]:
    return list(_semantic_claims_validation(value).get("constraints") or [])


def _semantic_claims_from_step_payload(value: Any) -> List[Dict[str, Any]]:
    """Decode the first complete JSON value after an inline claims label.

    Planner checklist fields can follow the JSON array on the same line and
    may themselves contain square brackets.  ``JSONDecoder.raw_decode`` owns
    the boundary so those later fields cannot be consumed as claim content.
    """
    text = str(value or "")
    marker = _SEMANTIC_CLAIMS_FIELD_RE.search(text)
    if not marker:
        return []
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[marker.end():].lstrip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return _semantic_claims_field(payload)


def _string_list(value: Any, *, dict_keys: Tuple[str, ...] = ()) -> List[str]:
    if value in (None, "", [], {}):
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    normalized: List[str] = []
    for item in items:
        candidate = item
        if isinstance(item, dict):
            candidate = next((item.get(key) for key in dict_keys if item.get(key) not in (None, "")), "")
        text = _valid_contract_text(candidate)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _typed_effect_list(value: Any) -> List[Any]:
    """Keep effect evidence contracts intact across Plan normalization."""
    if value in (None, "", [], {}):
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    normalized: List[Any] = []
    seen: set[str] = set()
    for item in items:
        candidate: Any
        if isinstance(item, Mapping):
            candidate = deepcopy(dict(item))
        else:
            candidate = _valid_contract_text(item)
        if candidate in (None, "", [], {}):
            continue
        identity = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(candidate)
    return normalized


def _artifact_tokens(value: Any) -> set[str]:
    """Return stable logical tokens for a path, artifact id, or description."""
    text = Path(str(value or "")).name
    for suffix in Path(text).suffixes:
        if text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)]
    tokens: set[str] = set()
    for token in re.split(r"[^A-Za-z0-9]+", text.lower()):
        if not token or token.isdigit():
            continue
        token = _ARTIFACT_TOKEN_ALIASES.get(token, token)
        if token not in _ARTIFACT_TOKEN_IGNORED:
            tokens.add(token)
    return tokens


def infer_artifact_id(value: Any, artifact_type: str = "") -> str:
    """Infer a conservative logical id for legacy string-only contracts."""
    tokens = sorted(_artifact_tokens(value))
    kind = str(artifact_type or "").strip().lower()
    if not kind:
        suffix = Path(str(value or "")).suffix.lower()
        kind = "script" if suffix in {".sh", ".py", ".r"} else "file"
    if kind and kind not in tokens:
        tokens.append(kind)
    return "_".join(tokens) or "artifact"


def normalize_artifact_contract(
    value: Any,
    *,
    producer_step_id: str = "",
    source: str = "planner",
) -> Dict[str, Any]:
    """Normalize legacy strings and structured artifacts into one contract."""
    raw = dict(value) if isinstance(value, dict) else {"path": str(value or "")}
    path = str(raw.get("path") or raw.get("canonical_path") or raw.get("name") or "").strip()
    artifact_type = str(raw.get("artifact_type") or "").strip().lower()
    if not artifact_type:
        artifact_type = "script" if Path(path).suffix.lower() in {".sh", ".py", ".r"} else "file"
    artifact_id = str(raw.get("artifact_id") or infer_artifact_id(path or raw.get("description"), artifact_type)).strip()
    contract = {
        **raw,
        "artifact_id": artifact_id,
        # Semantic role is explicit provenance.  File suffixes are useful for
        # validators but never decide whether an artifact is an input, output,
        # reference, report, or intermediate.
        "role": str(raw.get("role") or "").strip(),
        "path": path,
        "artifact_type": artifact_type,
        "required": bool(raw.get("required", True)),
        "producer_step_id": str(raw.get("producer_step_id") or producer_step_id or ""),
        "producer": str(raw.get("producer") or ""),
        "validator": str(
            raw.get("validator")
            or (
                "nonempty_executable_script"
                if artifact_type == "script"
                else "directory_exists"
                if artifact_type == "directory"
                else "nonempty_file"
            )
        ),
        "source": str(raw.get("source") or source),
    }
    if raw.get("base_dir"):
        contract["base_dir"] = str(raw.get("base_dir"))
    return contract


def _stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    payload = "|".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8', errors='replace')).hexdigest()[:length]}"


def _provenance_defaults(task_id: str) -> Dict[str, Any]:
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "task_id": str(task_id or "default"),
        "actions": {},
        "attempts": {},
        "artifacts": {},
        "artifact_heads": {},
        "manifests": {},
        "stages": {},
        "validations": {},
        "attempt_counters": {},
        "migration_log": [],
        "updated_at": "",
    }


def _artifact_path(value: Dict[str, Any]) -> str:
    path = Path(str(value.get("path") or "")).expanduser()
    if not path.is_absolute() and value.get("base_dir"):
        path = Path(str(value.get("base_dir"))) / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _canonical_artifact_id(task_id: str, step_id: str, logical_id: str, path: str) -> str:
    return _stable_id("artifact", task_id, step_id or "external", logical_id or "artifact", path)


def _artifact_signature(path_value: Any) -> Dict[str, Any]:
    path = Path(str(path_value or "")).expanduser()
    try:
        if not path.exists():
            return {"path": str(path), "exists": False}
        stat = path.stat()
        signature: Dict[str, Any] = {
            "path": str(path.resolve()),
            "exists": True,
            "is_file": path.is_file(),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
        if path.is_file() and stat.st_size <= 16 * 1024 * 1024:
            signature["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return signature
    except OSError as exc:
        return {"path": str(path), "exists": False, "error": type(exc).__name__}


def _safe_action_payload(value: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        key: deepcopy(item)
        for key, item in dict(value or {}).items()
        if key not in {"messages", "raw_response"}
    }
    code = str(payload.pop("code", "") or "")
    if code:
        payload["code_hash"] = hashlib.sha256(code.encode("utf-8", errors="replace")).hexdigest()
        payload["code_summary"] = code[:2000]
    return payload


@timed_phase("state.provenance_projection")
def _provenance_artifact_definitions(
    state: PlanState, *, step_updates: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Read-only artifact projection; explicit enrichment is returned separately."""
    result: Dict[str, Dict[str, Any]] = {}
    from .plan_contract import plan_uses_explicit_results, plan_result_contracts
    explicit_results = plan_uses_explicit_results(state)
    result_contracts = plan_result_contracts(state) if explicit_results else {}
    task_id = str(state.get("task_id") or "default")
    task_spec = _current_task_spec(state)
    task_outputs = [
        dict(row) for row in task_spec.get("output_contracts", []) or []
        if isinstance(row, Mapping)
    ]
    task_output_by_ref: Dict[str, Dict[str, Any]] = {}
    for row in task_outputs:
        for ref in (row.get("artifact_id"), row.get("output_id"), row.get("semantic_handle")):
            if str(ref or ""):
                task_output_by_ref[str(ref)] = row
    plan_local_by_ref = {
        str(logical_id): dict(row)
        for logical_id, row in dict(state.get("plan_artifacts") or {}).items()
        if isinstance(row, Mapping) and str(logical_id)
    }
    existing_provenance = dict(state.get("provenance_state") or {})
    existing_heads = dict(existing_provenance.get("artifact_heads") or {})
    existing_artifacts = dict(existing_provenance.get("artifacts") or {})

    artifact_allocation_mapping = state.get("artifact_allocation_mapping")
    allocated_mapping = (
        artifact_allocation_mapping.get("local_to_authoritative") or {}
        if isinstance(artifact_allocation_mapping, Mapping)
        else {}
    )
    allocated_local_refs = {
        str(ref) for ref in allocated_mapping if str(ref)
    }

    def without_superseded_untyped_artifacts(artifact_ids: Iterable[Any]) -> List[str]:
        """Drop legacy/unallocated aliases once a typed plan-local edge exists."""
        retained: List[str] = []
        for raw_artifact_id in artifact_ids or []:
            artifact_id = str(raw_artifact_id or "")
            head = str(existing_heads.get(artifact_id) or "")
            record = dict(existing_artifacts.get(head) or {})
            if str(record.get("source") or "") == "planner_text":
                continue
            if (
                str(record.get("source") or "") == "structured_plan"
                and Path(str(record.get("path") or "")).name in allocated_local_refs
            ):
                continue
            if artifact_id:
                retained.append(artifact_id)
        return retained
    materialized_by_ref: Dict[str, Dict[str, Any]] = {}
    for row in state.get("task_output_materializations", []) or []:
        if not isinstance(row, Mapping):
            continue
        for ref in (row.get("output_ref"), row.get("output_id")):
            if str(ref or ""):
                materialized_by_ref[str(ref)] = dict(row)
    for index, raw_step in enumerate(state.get("steps", []) or [], start=1):
        if not isinstance(raw_step, dict):
            continue
        step = normalize_plan_step(deepcopy(raw_step), index)
        step_id = str(step.get("step_id") or "")
        output_ids: List[str] = []
        contracts: List[Dict[str, Any]] = []
        for raw_ref in step.get("outputs", []) or []:
            ref = str(
                raw_ref.get("artifact_id") or raw_ref.get("output_id") or ""
            ) if isinstance(raw_ref, Mapping) else str(raw_ref or "")
            task_output = task_output_by_ref.get(ref)
            plan_local = plan_local_by_ref.get(ref)
            if task_output is None and plan_local is None:
                continue
            if plan_local is not None:
                object_kind = str(plan_local.get("object_kind") or "file")
                path = str(plan_local.get("canonical_path") or "")
                if path:
                    contracts.append(normalize_artifact_contract({
                        "artifact_id": ref,
                        "output_id": ref,
                        "role": str(
                            (plan_local.get("semantic_view") or {}).get("role")
                            or ref
                        ),
                        "path": path,
                        "artifact_type": object_kind,
                        "required": bool(plan_local.get("required", not explicit_results)),
                        "path_policy": str(plan_local.get("path_policy") or "managed_exact"),
                        "validator": str(plan_local.get("validation_contract_id") or (
                            "directory_exists"
                            if object_kind == "directory"
                            else "file_exists"
                        )),
                        "source": "approved_plan_artifact_ledger",
                    }, producer_step_id=step_id, source="approved_plan_artifact_ledger"))
                continue
            output_id = str(task_output.get("output_id") or task_output.get("artifact_id") or ref)
            logical_artifact_id = str(task_output.get("artifact_id") or output_id)
            materialized = (
                materialized_by_ref.get(str(task_output.get("artifact_id") or ""))
                or materialized_by_ref.get(output_id)
                or {}
            )
            approved_result = result_contracts.get(logical_artifact_id) or result_contracts.get(ref) or {}
            path = str(
                task_output.get("canonical_path")
                or materialized.get("canonical_path")
                or materialized.get("path")
                or ""
            )
            if path:
                contracts.append(normalize_artifact_contract({
                    "artifact_id": logical_artifact_id,
                    "output_id": output_id,
                    "role": str(task_output.get("role") or output_id),
                    "path": path,
                    "artifact_type": str(task_output.get("artifact_kind") or "file"),
                    "required": bool(approved_result.get("required", False)) if explicit_results else bool(task_output.get("required", True)),
                    "path_policy": str(approved_result.get("path_policy") or task_output.get("path_policy") or "managed_exact"),
                    "validator": str(
                        task_output.get("validation_contract_id")
                        or (
                            "directory_exists"
                            if str(task_output.get("artifact_kind") or "") == "directory"
                            else "nonempty_file"
                        )
                    ),
                    "source": "approved_plan_result" if explicit_results else "task_spec_output_contract",
                }, producer_step_id=step_id, source="task_spec_output_contract"))
        if not contracts and not explicit_results:
            contracts = step_artifact_contracts(step)
        for contract in contracts:
            path = _artifact_path(contract)
            logical_id = str(contract.get("artifact_id") or contract.get("output_id") or infer_artifact_id(path))
            artifact_id = _canonical_artifact_id(task_id, step_id, logical_id, path)
            result[artifact_id] = {
                **deepcopy(contract),
                "artifact_id": artifact_id,
                "output_id": str(contract.get("output_id") or logical_id),
                "logical_artifact_id": logical_id,
                "path": path,
                "producer_step_id": step_id,
                "consumer_step_ids": [],
                "consumer_stage_ids": [],
            }
            output_ids.append(artifact_id)
        if output_ids:
            typed_plan_local_output = any(
                str(contract.get("source") or "") == "approved_plan_artifact_ledger"
                for contract in contracts
            )
            prior_output_ids = list(step.get("output_artifact_ids", []))
            if typed_plan_local_output:
                prior_output_ids = without_superseded_untyped_artifacts(prior_output_ids)
            step["output_artifact_ids"] = list(dict.fromkeys([*prior_output_ids, *output_ids]))
            if step_updates is not None:
                step_updates.setdefault(index - 1, {})["output_artifact_ids"] = list(step["output_artifact_ids"])

    by_path = {str(item.get("path") or ""): artifact_id for artifact_id, item in result.items()}
    by_logical: Dict[str, List[str]] = {}
    for artifact_id, item in result.items():
        by_logical.setdefault(str(item.get("logical_artifact_id") or ""), []).append(artifact_id)
    for index, raw_step in enumerate(state.get("steps", []) or [], start=1):
        if not isinstance(raw_step, dict):
            continue
        step = normalize_plan_step(deepcopy(raw_step), index)
        matched: List[str] = []
        for value in step.get("inputs", []) or []:
            text = str(value or "")
            try:
                resolved = str(Path(text).expanduser().resolve())
            except OSError:
                resolved = text
            artifact_id = by_path.get(resolved) or by_path.get(text)
            if not artifact_id and text in by_logical and len(by_logical[text]) == 1:
                artifact_id = by_logical[text][0]
            if artifact_id and artifact_id not in matched:
                matched.append(artifact_id)
                result[artifact_id]["consumer_step_ids"].append(str(step.get("step_id") or ""))
                producer_step_id = str(result[artifact_id].get("producer_step_id") or "")
                if producer_step_id and producer_step_id != str(step.get("step_id") or ""):
                    dependencies = list(step.get("depends_on") or [])
                    if producer_step_id not in dependencies:
                        dependencies.append(producer_step_id)
                        step["depends_on"] = dependencies
                        step["dependencies"] = list(dependencies)
                        step.setdefault("dependency_sources", {})[producer_step_id] = "artifact_contract_inferred"
        if matched:
            typed_plan_local_input = any(
                str((result.get(artifact_id) or {}).get("source") or "")
                == "approved_plan_artifact_ledger"
                for artifact_id in matched
            )
            prior_input_ids = list(step.get("input_artifact_ids", []))
            if typed_plan_local_input:
                prior_input_ids = without_superseded_untyped_artifacts(prior_input_ids)
            step["input_artifact_ids"] = list(dict.fromkeys([*prior_input_ids, *matched]))
            if step_updates is not None:
                step_updates.setdefault(index - 1, {}).update({
                    "input_artifact_ids": list(step["input_artifact_ids"]),
                    "dependencies": list(step.get("dependencies") or []),
                    "depends_on": list(step.get("depends_on") or []),
                })
    return result


def ensure_provenance_state(state: PlanState) -> Dict[str, Any]:
    task_id = str(state.get("task_id") or "default")
    state.setdefault("plan_id", _stable_id("plan", task_id))
    raw = state.get("provenance_state")
    provenance = dict(raw) if isinstance(raw, dict) else {}
    defaults = _provenance_defaults(task_id)
    for key, value in defaults.items():
        if key not in provenance or not isinstance(provenance.get(key), type(value)):
            provenance[key] = deepcopy(value)
    provenance["schema_version"] = PROVENANCE_SCHEMA_VERSION
    provenance["task_id"] = task_id
    for version_id, artifact in list(provenance.get("artifacts", {}).items()):
        if not isinstance(artifact, dict):
            continue
        artifact.setdefault("artifact_version_id", str(version_id))
        artifact.setdefault("schema_version", PROVENANCE_SCHEMA_VERSION)
        artifact.setdefault("status", "expected")
        artifact.setdefault("validation_status", "unknown")
        artifact.setdefault("consumer_step_ids", [])
        artifact.setdefault("consumer_stage_ids", [])
        if not any(artifact.get(key) for key in ("producer_step_id", "producer_stage_id", "producer_action_id", "producer_attempt_id")):
            artifact.setdefault("producer_status", "producer_unknown")

    # Preserve existing artifact-link enrichment at this explicit mutation
    # boundary, without leaking any normalizer compatibility fields.
    step_updates: Dict[int, Dict[str, Any]] = {}
    definitions = _provenance_artifact_definitions(state, step_updates=step_updates)
    for index, update in step_updates.items():
        state["steps"][index].update(update)
    for artifact_id, definition in definitions.items():
        if artifact_id in provenance["artifact_heads"]:
            head = provenance["artifact_heads"][artifact_id]
            if head in provenance["artifacts"]:
                record = provenance["artifacts"][head]
                record["consumer_step_ids"] = list(dict.fromkeys([
                    *record.get("consumer_step_ids", []), *definition.get("consumer_step_ids", []),
                ]))
            continue
        version_id = f"{artifact_id}@expected"
        provenance["artifacts"].setdefault(version_id, {
            **definition,
            "artifact_version_id": version_id,
            "producer_manifest_id": "",
            "producer_stage_id": "",
            "producer_action_id": "",
            "producer_attempt_id": "",
            "status": "expected",
            "validation_status": "unknown",
            "validation_id": "",
            "created_at": "",
            "size": 0,
            "digest_or_signature": {},
            "source": str(definition.get("source") or "plan_contract"),
            "schema_version": PROVENANCE_SCHEMA_VERSION,
        })
        provenance["artifact_heads"][artifact_id] = version_id
    state["provenance_state"] = provenance
    return provenance


def _step_by_id(state: PlanState, step_id: str) -> Dict[str, Any]:
    return next((step for step in state.get("steps", []) or [] if isinstance(step, dict) and str(step.get("step_id") or "") == str(step_id)), {})


def _stage_for_action(provenance: Dict[str, Any], action: Dict[str, Any]) -> str:
    manifest_id = str(action.get("manifest_id") or "")
    manifest_action_id = str(action.get("manifest_action_id") or "")
    for stage_id, stage in provenance.get("stages", {}).items():
        if manifest_id and str(stage.get("manifest_id") or "") != manifest_id:
            continue
        if manifest_action_id and str(stage.get("manifest_action_id") or "") == manifest_action_id:
            return str(stage_id)
    return ""


def _pid_is_alive(pid_value: Any) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _attempt_matches_identity(
    item: Mapping[str, Any],
    *,
    step_id: str = "",
    action_id: str = "",
    plan_id: str = "",
    plan_version: str = "",
    execution_epoch: Any = "",
) -> bool:
    expected = {
        "step_id": str(step_id or ""),
        "action_id": str(action_id or ""),
        "plan_id": str(plan_id or ""),
        "plan_version": str(plan_version or ""),
        "execution_epoch": str(execution_epoch if execution_epoch not in (None, "") else ""),
    }
    return all(
        not value or str(item.get(key) or "") == value
        for key, value in expected.items()
    )


def active_attempt(
    state: PlanState,
    step_id: str = "",
    *,
    action_id: str = "",
    plan_id: str = "",
    plan_version: str = "",
    execution_epoch: Any = "",
) -> Dict[str, Any]:
    """Return the one durable active attempt, newest first."""
    provenance = ensure_provenance_state(state)
    current_plan_version = str(state.get("plan_version") or "")
    rows = [
        dict(item)
        for item in provenance.get("attempts", {}).values()
        if isinstance(item, dict)
        and str(item.get("status") or "").upper() in ATTEMPT_ACTIVE_STATUSES
        and (
            not current_plan_version
            or not str(item.get("plan_version") or "")
            or str(item.get("plan_version") or "") == current_plan_version
        )
        and _attempt_matches_identity(
            item,
            step_id=step_id,
            action_id=action_id,
            plan_id=plan_id,
            plan_version=plan_version,
            execution_epoch=execution_epoch,
        )
    ]
    rows.sort(key=lambda item: (str(item.get("created_at") or item.get("started_at") or ""), str(item.get("attempt_id") or "")), reverse=True)
    return rows[0] if rows else {}


def classify_attempt_resume(
    state: PlanState,
    *,
    step_id: str = "",
    action_id: str = "",
    plan_id: str = "",
    plan_version: str = "",
    execution_epoch: Any = "",
) -> Dict[str, Any]:
    """Classify durable attempt recovery without creating or rerunning work."""
    attempt = active_attempt(
        state,
        step_id,
        action_id=action_id,
        plan_id=plan_id,
        plan_version=plan_version,
        execution_epoch=execution_epoch,
    )
    if not attempt:
        provenance = ensure_provenance_state(state)
        pending_validation = [
            dict(item) for item in provenance.get("attempts", {}).values()
            if isinstance(item, dict)
            and str(item.get("status") or "").upper() in {
                ATTEMPT_PROCESS_EXITED, ATTEMPT_EVIDENCE_COMMITTED,
                ATTEMPT_VALIDATED, ATTEMPT_VALIDATION_PASSED,
                ATTEMPT_VALIDATION_FAILED,
            }
            and (
                not str(state.get("plan_version") or "")
                or not str(item.get("plan_version") or "")
                or str(item.get("plan_version") or "") == str(state.get("plan_version") or "")
            )
            and _attempt_matches_identity(
                item,
                step_id=step_id,
                action_id=action_id,
                plan_id=plan_id,
                plan_version=plan_version,
                execution_epoch=execution_epoch,
            )
        ]
        pending_validation.sort(
            key=lambda item: (
                str(item.get("updated_at") or item.get("finished_at") or item.get("created_at") or ""),
                str(item.get("attempt_id") or ""),
            ),
            reverse=True,
        )
        attempt = pending_validation[0] if pending_validation else {}
    if not attempt:
        return {"route": "none", "attempt": {}, "create_new_attempt": True, "reason": "no_active_attempt"}
    status = str(attempt.get("status") or "").upper()
    exit_status_path = Path(str(attempt.get("exit_status_path") or "")) if attempt.get("exit_status_path") else None
    exit_record = {}
    if exit_status_path and exit_status_path.is_file():
        try:
            exit_record = json.loads(exit_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            exit_record = {}
    if status in {ATTEMPT_STARTING, ATTEMPT_RUNNING}:
        if _pid_is_alive(attempt.get("pid")):
            route, reason = "monitor_running_attempt", "running_process_is_alive"
        elif exit_record:
            route, reason = "supervisor", "durable_exit_status_found"
        else:
            route, reason = "reconcile_attempt", "process_missing_without_exit_status"
    elif status in {ATTEMPT_PROCESS_EXITED, ATTEMPT_EVIDENCE_COMMITTED}:
        route, reason = "supervisor", "execution_finished_validation_pending"
    elif status in {ATTEMPT_VALIDATED, ATTEMPT_VALIDATION_PASSED}:
        route, reason = "transition_guard", "validation_committed_plan_transition_pending"
    elif status == ATTEMPT_VALIDATION_FAILED:
        route, reason = "supervisor", "failed_validation_requires_recovery"
    elif status == ATTEMPT_UNKNOWN_AFTER_CRASH:
        route, reason = "reconcile_attempt", "unknown_after_crash"
    elif status in {ATTEMPT_CREATED, ATTEMPT_SCRIPT_READY, ATTEMPT_APPROVED}:
        route, reason = "reconcile_attempt", "prepared_attempt_interrupted_before_start"
    else:
        route, reason = "none", "attempt_terminal"
    return {
        "route": route,
        "attempt": attempt,
        "exit_record": exit_record,
        "create_new_attempt": False,
        "reason": reason,
    }


def record_provenance_event(state: PlanState, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The sole mutation entry for execution lineage stored by PlanTracker."""
    provenance = ensure_provenance_state(state)
    event = str(event_type or "").strip().lower()
    value = dict(payload or {})
    now = _now()

    if event == "action_bound":
        action = dict(value.get("action") or {})
        action_id = str(action.get("action_id") or _stable_id(
            "action", state.get("plan_id"), action.get("step_id"), json.dumps(action, sort_keys=True, default=str)
        ))
        action["action_id"] = action_id
        manifest_path = str(action.get("manifest_path") or "")
        manifest_id = str(action.get("manifest_id") or "")
        if not manifest_id and manifest_path:
            manifest_id = next((mid for mid, item in provenance["manifests"].items() if str(item.get("manifest_path") or "") == manifest_path), "")
        record = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "action_id": action_id,
            "plan_id": str(state.get("plan_id") or ""),
            "plan_version": str(action.get("plan_version") or state.get("plan_version") or ""),
            "execution_epoch": str(action.get("execution_epoch") or ""),
            # Match the digest authority used by prepare_mutation_intents.
            # The candidate fingerprint is the established fallback for older
            # legal actions that do not yet carry a dedicated action_hash.
            "action_hash": str(
                action.get("action_hash")
                or action.get("candidate_fingerprint")
                or ""
            ),
            "step_id": str(action.get("step_id") or ""),
            "stage_id": str(action.get("stage_id") or ""),
            "manifest_id": manifest_id,
            "manifest_action_id": str(action.get("manifest_action_id") or ""),
            "action_type": str(action.get("type") or "execute"),
            "command_role": str(action.get("command_role") or "primary"),
            "source": str(action.get("action_source") or "generate"),
            "status": "bound",
            "revoked_at": "",
            "revocation_reason": "",
            "execution_started": False,
            "raw_action": _safe_action_payload(dict(value.get("raw_action") or action)),
            "normalized_action": _safe_action_payload(action),
            "generated_script": dict(action.get("generated_script") or {}),
            "generated_script_path": str(action.get("generated_script_path") or action.get("script_path") or ""),
            "created_at": now,
            "updated_at": now,
        }
        if not record["stage_id"]:
            record["stage_id"] = _stage_for_action(provenance, record)
        previous = provenance["actions"].get(action_id, {})
        if previous.get("raw_action"):
            record["raw_action"] = deepcopy(previous["raw_action"])
        provenance["actions"][action_id] = {**previous, **record, "created_at": previous.get("created_at") or now}
        result = deepcopy(provenance["actions"][action_id])

    elif event == "action_revoked":
        action_id = str(value.get("action_id") or "")
        action = provenance["actions"].get(action_id)
        if not isinstance(action, dict):
            raise ValueError(f"Unknown provenance action: {action_id}")
        action["status"] = "revoked"
        action["revoked_at"] = now
        action["revocation_reason"] = str(
            value.get("reason") or "pre_execution_revocation"
        )[:1000]
        action["execution_started"] = bool(value.get("execution_started", False))
        action["updated_at"] = now
        result = deepcopy(action)

    elif event in {"attempt_created", "attempt_started"}:
        action_id = str(value.get("action_id") or "")
        action = dict(provenance["actions"].get(action_id) or {})
        if not action:
            raise ValueError(f"Unknown provenance action: {action_id}")
        step_id = str(value.get("step_id") or action.get("step_id") or "")
        explicit_attempt_id = str(value.get("attempt_id") or "")
        if explicit_attempt_id and explicit_attempt_id in provenance["attempts"]:
            existing_attempt = dict(
                provenance["attempts"][explicit_attempt_id]
            )
            expected_scope = {
                "action_id": action_id,
                "step_id": step_id,
                "plan_id": str(state.get("plan_id") or ""),
                "plan_version": str(
                    action.get("plan_version")
                    or state.get("plan_version")
                    or ""
                ),
                "execution_epoch": str(action.get("execution_epoch") or ""),
            }
            conflicts = {
                key: {
                    "expected": expected,
                    "observed": str(existing_attempt.get(key) or ""),
                }
                for key, expected in expected_scope.items()
                if expected
                and str(existing_attempt.get(key) or "") != expected
            }
            if conflicts:
                raise ValueError(
                    "Attempt replay identity conflict: "
                    + json.dumps(conflicts, sort_keys=True)
                )
            result = deepcopy(existing_attempt)
            provenance["updated_at"] = now
            state["provenance_state"] = provenance
            state["updated_at"] = now
            return result
        existing_active = active_attempt(state, step_id)
        if existing_active and str(existing_active.get("attempt_id") or "") != explicit_attempt_id:
            existing_action_id = str(existing_active.get("action_id") or "")
            if existing_action_id == action_id:
                raise ValueError(
                    f"Step {step_id or '<unknown>'} already has active attempt "
                    f"{existing_active.get('attempt_id')}"
                )
            if (
                str(existing_active.get("status") or "").upper()
                in {ATTEMPT_STARTING, ATTEMPT_RUNNING}
                and _pid_is_alive(existing_active.get("pid"))
            ):
                raise ValueError(
                    "Cannot supersede a different action while its process is alive"
                )
            stale_attempt = provenance["attempts"].get(
                str(existing_active.get("attempt_id") or "")
            )
            if isinstance(stale_attempt, dict):
                stale_attempt["status"] = ATTEMPT_FAILED
                stale_attempt["termination_reason"] = "superseded_by_new_action"
                stale_attempt["superseded_by_action_id"] = action_id
                stale_attempt["updated_at"] = now
        counters = provenance["attempt_counters"]
        sequence = int(counters.get(action_id) or 0) + 1
        counters[action_id] = sequence
        attempt_id = explicit_attempt_id or _stable_id("attempt", action_id, sequence)
        if attempt_id in provenance["attempts"]:
            result = deepcopy(provenance["attempts"][attempt_id])
        else:
            step = _step_by_id(state, step_id)
            before = {}
            for artifact_id in step.get("output_artifact_ids", []) or []:
                head = provenance["artifact_heads"].get(artifact_id)
                record = provenance["artifacts"].get(head, {})
                if record.get("path"):
                    before[artifact_id] = _artifact_signature(record["path"])
            record = {
                "schema_version": PROVENANCE_SCHEMA_VERSION,
                "attempt_id": attempt_id,
                "attempt_sequence": sequence,
                "action_id": action_id,
                "plan_id": str(state.get("plan_id") or ""),
                "plan_version": str(state.get("plan_version") or action.get("plan_version") or ""),
                "execution_epoch": str(action.get("execution_epoch") or ""),
                "action_hash": str(action.get("action_hash") or ""),
                "step_id": step_id,
                "stage_id": str(value.get("stage_id") or action.get("stage_id") or ""),
                "generated_script": dict(value.get("generated_script") or {}),
                "approved_script": dict(value.get("approved_script") or {}),
                "executed_script": dict(value.get("executed_script") or {}),
                # A generated candidate path is not evidence that the approved
                # execution wrapper was materialized or that launch began.
                # Only the explicit lifecycle payload may populate this field.
                "executed_script_path": str(value.get("executed_script_path") or ""),
                "execution_record_id": "",
                "created_at": str(value.get("created_at") or now),
                "started_at": str(value.get("started_at") or ""),
                "heartbeat_at": "",
                "finished_at": "",
                "pid": None,
                "pgid": None,
                "stdout_path": str(value.get("stdout_path") or ""),
                "stderr_path": str(value.get("stderr_path") or ""),
                "exit_status_path": str(value.get("exit_status_path") or ""),
                "validation_evidence_ref": "",
                "incident_ref": "",
                "returncode": None,
                "status": ATTEMPT_RUNNING if event == "attempt_started" else ATTEMPT_CREATED,
                "command_role": str(action.get("command_role") or "primary"),
                "produced_artifact_ids": [],
                "artifact_version_ids": [],
                "before_artifacts": before,
                "managed_output_contracts": deepcopy(
                    list(value.get("managed_output_contracts") or [])
                ),
                "managed_output_snapshot_before": deepcopy(
                    dict(value.get("managed_output_snapshot_before") or {})
                ),
            }
            provenance["attempts"][attempt_id] = record
            if record["stage_id"] in provenance.get("stages", {}):
                provenance["stages"][record["stage_id"]]["current_attempt_id"] = attempt_id
                provenance["stages"][record["stage_id"]]["status"] = "running"
            result = deepcopy(record)
        if event == "attempt_started":
            state["execution_cursor_step_id"] = str(result.get("step_id") or "")
            state["execution_status"] = "in_progress"

    elif event == WORKSPACE_MUTATION_INTENT_RECORDED_EVENT:
        # This event is an execution-preparation authority boundary.  Payload
        # fields are intentionally not coerced: an invalid reference must not
        # become valid merely because ``str(value)`` happens to look right.
        attempt_id_value = value.get("attempt_id")
        journal_path_value = value.get("journal_path")
        journal_digest_value = value.get("journal_digest")
        intent_ids_value = value.get("intent_ids")
        for field, field_value in (
            ("attempt_id", attempt_id_value),
            ("journal_path", journal_path_value),
            ("journal_digest", journal_digest_value),
        ):
            if type(field_value) is not str or not field_value.strip():
                raise WorkspaceAttestationError(
                    "MUTATION_INTENT_EVENT_PAYLOAD_INVALID",
                    field=field,
                    observed_type=type(field_value).__name__,
                )
        if type(intent_ids_value) is not list:
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_EVENT_PAYLOAD_INVALID",
                field="intent_ids",
                observed_type=type(intent_ids_value).__name__,
            )
        if any(
            type(intent_id) is not str or not intent_id.strip()
            for intent_id in intent_ids_value
        ):
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_INTENT_IDS_INVALID",
                field="intent_ids",
            )
        if len(set(intent_ids_value)) != len(intent_ids_value):
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_INTENT_IDS_INVALID",
                field="intent_ids",
                reason="duplicate_intent_id",
            )

        attempt_id = attempt_id_value
        attempt = provenance["attempts"].get(attempt_id)
        if not isinstance(attempt, dict):
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_ATTEMPT_UNKNOWN",
                attempt_id=attempt_id,
            )
        action_id = attempt.get("action_id")
        if type(action_id) is not str or not action_id:
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_ACTION_SCOPE_MISMATCH",
                attempt_id=attempt_id,
                reason="attempt_action_id_invalid",
            )
        action = provenance["actions"].get(action_id)
        if not isinstance(action, dict):
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_ACTION_SCOPE_MISMATCH",
                attempt_id=attempt_id,
                action_id=action_id,
                reason="action_unknown",
            )
        task_id = state.get("task_id")
        plan_id = state.get("plan_id")
        step_id = attempt.get("step_id")
        if any(
            type(item) is not str or not item
            for item in (task_id, plan_id, step_id)
        ):
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_PROVENANCE_SCOPE_INVALID",
                attempt_id=attempt_id,
                reason="state_or_attempt_scope_invalid",
            )
        step = _step_by_id(state, step_id)
        scope_mismatches: Dict[str, Any] = {}
        if str(provenance.get("task_id") or "") != task_id:
            scope_mismatches["task_id"] = provenance.get("task_id")
        if attempt.get("plan_id") != plan_id:
            scope_mismatches["attempt.plan_id"] = attempt.get("plan_id")
        if action.get("plan_id") != plan_id:
            scope_mismatches["action.plan_id"] = action.get("plan_id")
        if attempt.get("action_id") != action.get("action_id"):
            scope_mismatches["action_id"] = action.get("action_id")
        if action.get("step_id") != step_id:
            scope_mismatches["action.step_id"] = action.get("step_id")
        if not step:
            scope_mismatches["step_id"] = "unknown_step"
        attempt_action_digest = attempt.get("action_hash")
        normalized_action = action.get("normalized_action")
        normalized_action = (
            normalized_action if isinstance(normalized_action, Mapping) else {}
        )
        action_digest = (
            action.get("action_hash")
            or normalized_action.get("action_hash")
            or normalized_action.get("candidate_fingerprint")
        )
        if (
            attempt_action_digest
            and action_digest
            and attempt_action_digest != action_digest
        ):
            scope_mismatches["action_digest"] = {
                "attempt": attempt_action_digest,
                "action": action_digest,
            }
        trusted_action_digest = attempt_action_digest or action_digest
        if type(trusted_action_digest) is not str or not trusted_action_digest:
            scope_mismatches["action_digest"] = "missing"
        if scope_mismatches:
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_PROVENANCE_SCOPE_MISMATCH",
                attempt_id=attempt_id,
                mismatches=scope_mismatches,
            )

        plan_file_value = state.get("plan_file")
        if type(plan_file_value) is not str or not plan_file_value:
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_PATH_SCOPE_UNAVAILABLE",
                attempt_id=attempt_id,
                reason="plan_file_missing",
            )
        plan_file = Path(plan_file_value)
        if not plan_file.is_absolute():
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_PATH_SCOPE_UNAVAILABLE",
                attempt_id=attempt_id,
                reason="plan_file_not_absolute",
            )
        try:
            task_output_dir = plan_file.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_PATH_SCOPE_UNAVAILABLE",
                attempt_id=attempt_id,
                error_type=type(exc).__name__,
            ) from exc
        if (
            task_output_dir.name != task_id
            or task_output_dir.parent.name != "task_outputs"
        ):
            raise WorkspaceAttestationError(
                "MUTATION_JOURNAL_PATH_SCOPE_UNAVAILABLE",
                attempt_id=attempt_id,
                task_output_dir=str(task_output_dir),
            )
        expected_attempt_dir = task_output_dir / "attempts" / attempt_id
        durable_field = "workspace_mutation_intent_registration"
        existing = attempt.get(durable_field)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise WorkspaceAttestationError(
                    "MUTATION_INTENT_DURABLE_REGISTRATION_INVALID",
                    attempt_id=attempt_id,
                    reason="registration_type_invalid",
                )
            expected_existing_scope = {
                "schema_version": MUTATION_INTENT_REGISTRATION_SCHEMA_VERSION,
                "event_type": WORKSPACE_MUTATION_INTENT_RECORDED_EVENT,
                "producer": RUNTIME_ATTESTER_PRODUCER,
                "task_id": task_id,
                "plan_id": plan_id,
                "plan_revision": state.get("plan_revision"),
                "step_id": step_id,
                "action_id": action_id,
                "action_digest": trusted_action_digest,
                "attempt_id": attempt_id,
                "journal_path": str(
                    expected_attempt_dir / "mutation_intent.json"
                ),
            }
            existing_scope_mismatches = {
                field: {
                    "expected": deepcopy(expected_value),
                    "observed": deepcopy(existing.get(field)),
                }
                for field, expected_value in expected_existing_scope.items()
                if type(existing.get(field)) is not type(expected_value)
                or existing.get(field) != expected_value
            }
            existing_ids = existing.get("intent_ids")
            existing_digest_fields_valid = all(
                type(existing.get(field)) is str
                and str(existing.get(field)).startswith("sha256:")
                and len(str(existing.get(field))) == len("sha256:") + 64
                for field in ("journal_digest", "registration_digest")
            )
            existing_metadata_valid = bool(
                type(existing_ids) is list
                and all(
                    type(intent_id) is str and bool(intent_id.strip())
                    for intent_id in existing_ids
                )
                and len(set(existing_ids)) == len(existing_ids)
                and type(existing.get("journal_status")) is str
                and existing.get("journal_status")
                in {
                    "attempt_started",
                    "not_applicable",
                    "completed",
                    "reconciliation_required",
                }
                and type(existing.get("journal_finished_at")) is str
                and type(existing.get("registered_at")) is str
                and bool(existing.get("registered_at", "").strip())
            )
            if (
                existing_scope_mismatches
                or not existing_digest_fields_valid
                or not existing_metadata_valid
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_INTENT_DURABLE_REGISTRATION_INVALID",
                    attempt_id=attempt_id,
                    mismatches=existing_scope_mismatches,
                    digest_fields_valid=existing_digest_fields_valid,
                    metadata_valid=existing_metadata_valid,
                )
        if isinstance(existing, Mapping) and all(
            deepcopy(existing.get(field)) == deepcopy(observed)
            for field, observed in (
                ("attempt_id", attempt_id_value),
                ("journal_path", journal_path_value),
                ("journal_digest", journal_digest_value),
                ("intent_ids", intent_ids_value),
            )
        ):
            # This exact event was already verified and committed.  Its
            # journal is finalized in place, so a later replay of the original
            # event must not compare the historical whole-file digest with the
            # current receipt-bearing version.  Any consumer that intends to
            # use the current journal still reloads and validates it separately.
            return deepcopy(dict(existing))
        verified_reference = load_verified_mutation_journal_reference(
            journal_path_value,
            expected_attempt_dir=expected_attempt_dir,
            expected_task_id=task_id,
            expected_plan_id=plan_id,
            expected_plan_revision=state.get("plan_revision"),
            expected_step_id=step_id,
            expected_action_id=action_id,
            expected_action_digest=trusted_action_digest,
            expected_attempt_id=attempt_id,
            expected_journal_digest=journal_digest_value,
            expected_intent_ids=intent_ids_value,
        )

        identity_fields = (
            "schema_version",
            "event_type",
            "producer",
            "task_id",
            "plan_id",
            "plan_revision",
            "step_id",
            "action_id",
            "action_digest",
            "attempt_id",
            "journal_path",
            "registration_digest",
            "intent_ids",
        )
        verified_record = verified_reference.to_dict()
        if existing is not None:
            if not isinstance(existing, Mapping) or any(
                deepcopy(existing.get(field))
                != deepcopy(verified_record.get(field))
                for field in identity_fields
            ):
                raise WorkspaceAttestationError(
                    "MUTATION_INTENT_REGISTRATION_CONFLICT",
                    attempt_id=attempt_id,
                    existing_registration_digest=(
                        existing.get("registration_digest")
                        if isinstance(existing, Mapping)
                        else ""
                    ),
                    proposed_registration_digest=verified_record.get(
                        "registration_digest"
                    ),
                )
            # The whole-file digest/status may have advanced through the
            # journal's normal finalize/reconcile lifecycle.  The immutable
            # registration is already authoritative, so do not rewrite its
            # timestamp or the attempt's current/terminal status.
            return deepcopy(dict(existing))

        current_attempt_status = str(
            attempt.get("status") or ATTEMPT_CREATED
        ).upper()
        if (
            current_attempt_status != ATTEMPT_CREATED
            or verified_reference.journal_finished_at
            or verified_reference.journal_status
            not in {"attempt_started", "not_applicable"}
        ):
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_REGISTRATION_LATE",
                attempt_id=attempt_id,
                attempt_status=current_attempt_status,
                journal_status=verified_reference.journal_status,
            )
        if (
            str(action.get("status") or "") != "bound"
            or action.get("revoked_at")
        ):
            raise WorkspaceAttestationError(
                "MUTATION_INTENT_ACTION_NOT_ACTIVE",
                attempt_id=attempt_id,
                action_id=action_id,
                action_status=action.get("status"),
            )
        verified_record["registered_at"] = now
        attempt[durable_field] = verified_record
        result = deepcopy(verified_record)

    elif event == "attempt_state_changed":
        attempt_id = str(value.get("attempt_id") or "")
        attempt = provenance["attempts"].get(attempt_id)
        if not isinstance(attempt, dict):
            raise ValueError(f"Unknown provenance attempt: {attempt_id}")
        status = str(value.get("status") or attempt.get("status") or "").upper()
        allowed = {
            ATTEMPT_CREATED, ATTEMPT_SCRIPT_READY, ATTEMPT_APPROVED, ATTEMPT_STARTING,
            ATTEMPT_RUNNING, ATTEMPT_PROCESS_EXITED, ATTEMPT_EVIDENCE_COMMITTED,
            ATTEMPT_VALIDATED, ATTEMPT_VALIDATION_PASSED, ATTEMPT_VALIDATION_FAILED,
            ATTEMPT_COMPLETED, ATTEMPT_FAILED, ATTEMPT_UNKNOWN_AFTER_CRASH,
        }
        if status not in allowed:
            raise ValueError(f"Unsupported attempt status: {status}")
        current_status = str(attempt.get("status") or ATTEMPT_CREATED).upper()
        transitions = {
            ATTEMPT_CREATED: {ATTEMPT_SCRIPT_READY, ATTEMPT_FAILED, ATTEMPT_UNKNOWN_AFTER_CRASH},
            ATTEMPT_SCRIPT_READY: {ATTEMPT_APPROVED, ATTEMPT_FAILED, ATTEMPT_UNKNOWN_AFTER_CRASH},
            ATTEMPT_APPROVED: {ATTEMPT_STARTING, ATTEMPT_FAILED, ATTEMPT_UNKNOWN_AFTER_CRASH},
            ATTEMPT_STARTING: {ATTEMPT_RUNNING, ATTEMPT_PROCESS_EXITED, ATTEMPT_FAILED, ATTEMPT_UNKNOWN_AFTER_CRASH},
            ATTEMPT_RUNNING: {ATTEMPT_RUNNING, ATTEMPT_PROCESS_EXITED, ATTEMPT_FAILED, ATTEMPT_UNKNOWN_AFTER_CRASH},
            ATTEMPT_PROCESS_EXITED: {ATTEMPT_EVIDENCE_COMMITTED, ATTEMPT_FAILED, ATTEMPT_UNKNOWN_AFTER_CRASH},
            ATTEMPT_EVIDENCE_COMMITTED: {
                ATTEMPT_VALIDATED, ATTEMPT_VALIDATION_PASSED,
                ATTEMPT_VALIDATION_FAILED, ATTEMPT_FAILED,
            },
            ATTEMPT_VALIDATED: {ATTEMPT_COMPLETED, ATTEMPT_FAILED},
            ATTEMPT_VALIDATION_PASSED: {ATTEMPT_COMPLETED, ATTEMPT_FAILED},
            ATTEMPT_VALIDATION_FAILED: {ATTEMPT_FAILED},
            ATTEMPT_UNKNOWN_AFTER_CRASH: {ATTEMPT_PROCESS_EXITED, ATTEMPT_EVIDENCE_COMMITTED, ATTEMPT_FAILED},
            ATTEMPT_COMPLETED: {ATTEMPT_COMPLETED},
            ATTEMPT_FAILED: {ATTEMPT_FAILED},
        }
        if status != current_status and status not in transitions.get(current_status, set()):
            raise ValueError(f"Invalid attempt transition: {current_status} -> {status}")
        for key in (
            "generated_script", "approved_script", "executed_script", "executed_script_path",
            "pid", "pgid", "started_at", "heartbeat_at", "finished_at", "stdout_path",
            "stderr_path", "exit_status_path", "returncode", "validation_evidence_ref", "incident_ref",
        ):
            if key in value:
                attempt[key] = deepcopy(value[key])
        attempt["status"] = status
        attempt["updated_at"] = now
        if status in {ATTEMPT_STARTING, ATTEMPT_RUNNING}:
            state["execution_cursor_step_id"] = str(attempt.get("step_id") or "")
        elif status in {
            ATTEMPT_PROCESS_EXITED,
            ATTEMPT_EVIDENCE_COMMITTED,
            ATTEMPT_VALIDATED,
            ATTEMPT_VALIDATION_PASSED,
            ATTEMPT_VALIDATION_FAILED,
            ATTEMPT_COMPLETED,
            ATTEMPT_FAILED,
            ATTEMPT_UNKNOWN_AFTER_CRASH,
        } and str(state.get("execution_cursor_step_id") or "") == str(attempt.get("step_id") or ""):
            state["execution_cursor_step_id"] = ""
        result = deepcopy(attempt)

    elif event == "execution_finished":
        from .plan_contract import plan_uses_explicit_results
        attempt_id = str(value.get("attempt_id") or "")
        attempt = provenance["attempts"].get(attempt_id)
        if not isinstance(attempt, dict):
            raise ValueError(f"Unknown provenance attempt: {attempt_id}")
        execution = dict(value.get("execution_record") or {})
        action = dict(provenance["actions"].get(str(attempt.get("action_id") or "")) or {})
        role = str(execution.get("command_role") or attempt.get("command_role") or action.get("command_role") or "primary").lower()
        purpose = str(execution.get("purpose") or "").lower()
        temporary_diagnostic = is_recovery_diagnostic_execution(state, execution, action=action.get("normalized_action") or action)
        approved_execution = is_approved_step_execution(state, {**execution, "attempt_id": attempt_id}, action=action.get("normalized_action") or action)
        is_probe = temporary_diagnostic or (not approved_execution and (role in {"diagnostic", "optional_probe"} or purpose in {"probe", "diagnostic"}))
        attempt.update({
            "execution_record_id": str(execution.get("execution_id") or f"exec-{attempt_id}"),
            "executed_script_path": str(execution.get("script_path") or execution.get("script_file") or attempt.get("executed_script_path") or ""),
            "finished_at": str(execution.get("finished_at") or now),
            "returncode": execution.get("exit_code") if execution.get("exit_code") is not None else execution.get("returncode"),
            "status": ATTEMPT_EVIDENCE_COMMITTED,
            "command_role": role,
        })
        # StepEffect completion is evaluated after this graph turn from the
        # durable PlanState.  Keep the immutable expectation separate from the
        # attempt-scoped observed facts and persist both with the concrete
        # attempt; otherwise an outputless observation/binding/workspace step
        # loses its evidence when Supervisor reloads the checkpoint and is
        # falsely treated as incomplete.
        for key in ("declared_step_effect", "step_effect", "effect_evidence"):
            raw_effect_value = execution.get(key)
            if isinstance(raw_effect_value, Mapping):
                attempt[key] = deepcopy(dict(raw_effect_value))
        if "effect_evidence" not in attempt:
            projected_effect_evidence = {
                key: deepcopy(execution.get(key))
                for key in (
                    "produces_artifacts",
                    "produces_bindings",
                    "produces_observations",
                    "workspace_mutations",
                    "explicit_side_effects",
                )
                if key in execution
            }
            if projected_effect_evidence:
                attempt["effect_evidence"] = projected_effect_evidence
        if str(state.get("execution_cursor_step_id") or "") == str(attempt.get("step_id") or ""):
            state["execution_cursor_step_id"] = ""
        state["execution_status"] = "in_progress" if attempt.get("returncode") == 0 else "failed"
        if attempt.get("returncode") == 0 and not is_probe:
            mark_step_verification_pending(
                state,
                step_id=str(attempt.get("step_id") or ""),
                attempt_id=attempt_id,
                evidence="execution evidence committed; Supervisor validation pending",
            )
        if str(attempt.get("stage_id") or "") in provenance.get("stages", {}):
            stage = provenance["stages"][str(attempt.get("stage_id"))]
            stage["current_attempt_id"] = attempt_id
            stage["status"] = (
                STATUS_VERIFICATION_PENDING
                if attempt.get("returncode") == 0 and not is_probe
                else "recorded"
                if attempt.get("returncode") == 0
                else "failed"
            )
        produced_paths = {str(Path(item).expanduser().resolve()) for item in execution.get("produced_artifacts", []) or [] if str(item)}
        reconciled_by_logical = {
            str(item.get("artifact_id") or item.get("logical_artifact_id") or ""): dict(item)
            for item in execution.get("reconciled_outputs", []) or []
            if isinstance(item, Mapping)
            and str(item.get("artifact_id") or item.get("logical_artifact_id") or "")
            and str(item.get("attempt_id") or "") == attempt_id
        }
        for artifact_id in _step_by_id(state, str(attempt.get("step_id") or "")).get("output_artifact_ids", []) or []:
            head = provenance["artifact_heads"].get(artifact_id)
            definition = dict(provenance["artifacts"].get(head) or {})
            logical_id = str(definition.get("logical_artifact_id") or "")
            reconciliation = dict(reconciled_by_logical.get(logical_id) or {})
            predicted_path = str(definition.get("path") or "")
            path = str(reconciliation.get("observed_path") or predicted_path)
            after = _artifact_signature(path)
            before = dict(attempt.get("before_artifacts", {}).get(artifact_id) or {})
            changed = bool(
                after.get("exists")
                and (
                    bool(reconciliation)
                    or after != before
                )
            )
            # An approved effect may produce a formal artifact regardless of
            # its descriptive purpose. Recovery diagnostics have no such grant.
            allow_probe = (not plan_uses_explicit_results(state)
                           and bool(definition.get("allow_probe_producer"))
                           and not temporary_diagnostic)
            if path not in produced_paths or not changed or (is_probe and not allow_probe):
                continue
            version_id = f"{artifact_id}@{attempt_id}"
            artifact = {
                **definition,
                "artifact_id": artifact_id,
                "artifact_version_id": version_id,
                "producer_manifest_id": str(action.get("manifest_id") or ""),
                "producer_stage_id": str(attempt.get("stage_id") or ""),
                "producer_action_id": str(attempt.get("action_id") or ""),
                "producer_attempt_id": attempt_id,
                "path": path,
                "status": "materialized",
                "validation_status": "unknown",
                "validation_id": "",
                "created_at": now,
                "size": int(after.get("size") or 0),
                "digest_or_signature": after,
                "source": "execution_observed",
                "predicted_path": predicted_path,
                "realization": str(reconciliation.get("realization") or "expected"),
                "constraint_origin": str(reconciliation.get("constraint_origin") or ""),
                "representation": str(reconciliation.get("representation") or ""),
                "format": str(reconciliation.get("format") or definition.get("format") or ""),
                "schema_version": PROVENANCE_SCHEMA_VERSION,
            }
            provenance["artifacts"][version_id] = artifact
            provenance["artifact_heads"][artifact_id] = version_id
            if artifact_id not in attempt["produced_artifact_ids"]:
                attempt["produced_artifact_ids"].append(artifact_id)
                attempt["artifact_version_ids"].append(version_id)
        result = {
            **deepcopy(attempt),
            "produced_artifact_ids": list(attempt.get("produced_artifact_ids") or []),
            "artifact_version_ids": list(attempt.get("artifact_version_ids") or []),
        }

    elif event == "validation_recorded":
        from .plan_contract import plan_uses_explicit_results
        attempt_id = str(value.get("attempt_id") or "")
        attempt = provenance["attempts"].get(attempt_id)
        if not isinstance(attempt, dict):
            raise ValueError(f"Unknown provenance attempt: {attempt_id}")
        validation = dict(value.get("validation") or {})
        validation_id = str(validation.get("validation_id") or _stable_id("validation", attempt_id, json.dumps(validation, sort_keys=True, default=str)))
        validation["validation_id"] = validation_id
        validation.setdefault("attempt_id", attempt_id)
        validation.setdefault("step_id", str(attempt.get("step_id") or ""))
        validation.setdefault("stage_id", str(attempt.get("stage_id") or ""))
        validation.setdefault("recorded_at", now)
        provenance["validations"][validation_id] = validation
        attempt["validation_evidence_ref"] = str(value.get("validation_evidence_ref") or validation_id)
        status = str(validation.get("status") or "unknown").lower()
        attempt["status"] = (
            ATTEMPT_VALIDATION_PASSED
            if status in {"valid", "valid_with_warnings", "passed", "pass", "success"}
            else ATTEMPT_VALIDATION_FAILED
        )
        for version_id in attempt.get("artifact_version_ids", []) or []:
            artifact = provenance["artifacts"].get(version_id)
            if not isinstance(artifact, dict):
                continue
            artifact["validation_id"] = validation_id
            artifact["validation_status"] = status
            accepted_with_warnings = (
                plan_uses_explicit_results(state)
                and status == "valid_with_warnings"
                and not any(
                    isinstance(check, Mapping) and check.get("satisfied") is False
                    and str(check.get("path") or check.get("target") or "") == str(artifact.get("path") or "")
                    for check in validation.get("artifact_results", []) or []
                )
            )
            if accepted_with_warnings:
                # The validation record retains its warning status; this
                # artifact's acceptance is separately consumed by downstream
                # bindings and must use their established valid status.
                artifact["validation_status"] = "valid"
            if status in {"valid", "passed", "pass"} or accepted_with_warnings:
                artifact["status"] = "valid"
            elif status in {"invalid", "failed", "fail"}:
                artifact["status"] = "invalid"
        result = deepcopy(validation)

    elif event == "manifest_registered":
        manifest = dict(value.get("manifest") or {})
        manifest_id = str(manifest.get("manifest_id") or _stable_id("manifest", manifest.get("manifest_fingerprint")))
        logical_to_canonical: Dict[str, str] = {}
        for artifact_id, version_id in provenance.get("artifact_heads", {}).items():
            artifact = provenance.get("artifacts", {}).get(version_id, {})
            logical = str(artifact.get("logical_artifact_id") or "")
            if logical:
                logical_to_canonical[logical] = str(artifact_id)
        normalized_stages: List[Dict[str, Any]] = []
        for raw_stage in manifest.get("stages", []) or []:
            stage = deepcopy(raw_stage)
            stage["output_artifact_ids"] = [
                logical_to_canonical.get(str(item), str(item))
                for item in stage.get("output_artifact_ids", []) or []
            ]
            stage["input_artifact_ids"] = [
                logical_to_canonical.get(str(item), str(item))
                for item in stage.get("input_artifact_ids", []) or []
            ]
            normalized_stages.append(stage)
        manifest_record = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "manifest_id": manifest_id,
            "manifest_path": str(value.get("manifest_path") or manifest.get("manifest_path") or ""),
            "manifest_fingerprint": str(manifest.get("manifest_fingerprint") or ""),
            "renderer_id": str((manifest.get("renderer") or {}).get("id") or ""),
            "plan_step_id": str(value.get("step_id") or (manifest.get("provenance") or {}).get("step_id") or ""),
            "producer_action_id": str(value.get("producer_action_id") or (manifest.get("provenance") or {}).get("producer_action_id") or ""),
            "stage_ids": [str(item.get("stage_id") or "") for item in normalized_stages],
            "registered_at": now,
        }
        provenance["manifests"][manifest_id] = manifest_record
        for stage in normalized_stages:
            provenance["stages"][str(stage.get("stage_id") or "")] = deepcopy(stage)
            for artifact_id in stage.get("output_artifact_ids", []) or []:
                head = provenance.get("artifact_heads", {}).get(artifact_id)
                artifact = provenance.get("artifacts", {}).get(head, {})
                if artifact:
                    artifact["producer_manifest_id"] = manifest_id
                    artifact["producer_stage_id"] = str(stage.get("stage_id") or "")
            for artifact_id in stage.get("input_artifact_ids", []) or []:
                head = provenance.get("artifact_heads", {}).get(artifact_id)
                artifact = provenance.get("artifacts", {}).get(head, {})
                if artifact:
                    artifact["consumer_stage_ids"] = list(dict.fromkeys([
                        *artifact.get("consumer_stage_ids", []), str(stage.get("stage_id") or ""),
                    ]))
        result = deepcopy(manifest_record)

    else:
        raise ValueError(f"Unsupported provenance event: {event_type}")

    provenance["updated_at"] = now
    state["provenance_state"] = provenance
    state["updated_at"] = now
    return result


def increment_plan_budget(state: PlanState, bucket: str, amount: int = 1) -> int:
    """Increment one scalar control-plane budget without touching retry buckets."""
    task_state = dict(state.get("task_state") or {})
    budgets = dict(task_state.get("budgets") or {})
    value = max(0, _int(budgets.get(bucket), 0) + int(amount))
    budgets[str(bucket)] = value
    task_state["budgets"] = budgets
    state["task_state"] = task_state
    return value


RECOVERY_EXACTLY_ONCE_COUNTERS = {
    "recovery_judge_calls",
    "primary_execution_dispatches",
    "primary_execution_failures",
    "diagnostic_probes_proposed",
    "diagnostic_probes_validated",
    "diagnostic_probes_dispatched",
    "diagnostic_probes_completed",
    "diagnostic_probes_not_executed",
    "preaction_rejections",
    "payload_repairs",
    "external_handoffs_created",
    "external_jobs_submitted",
}


def record_recovery_counter_once(
    state: PlanState,
    counter: str,
    identity: str,
) -> bool:
    """Charge one recovery lifecycle counter once per stable identity."""
    name = str(counter or "")
    event_id = str(identity or "")
    if name not in RECOVERY_EXACTLY_ONCE_COUNTERS:
        raise ValueError(f"Unsupported recovery lifecycle counter: {name}")
    if not event_id:
        raise ValueError("Recovery lifecycle counter identity is required")
    task_state = dict(state.get("task_state") or {})
    budgets = dict(task_state.get("budgets") or {})
    ledger = dict(budgets.get("recovery_counter_identities") or {})
    identities = [str(item) for item in ledger.get(name) or []]
    if event_id in identities:
        return False
    identities.append(event_id)
    ledger[name] = identities[-10_000:]
    budgets["recovery_counter_identities"] = ledger
    budgets[name] = max(0, _int(budgets.get(name), 0) + 1)
    task_state["budgets"] = budgets
    state["task_state"] = task_state
    return True


def _recovery_state_snapshot(state: PlanState) -> Dict[str, Any]:
    provenance = ensure_provenance_state(state)
    return {
        "current_step_id": str(state.get("current_step_id") or ""),
        "current_stage_id": str(state.get("current_stage_id") or ""),
        "step_statuses": {
            str(step.get("step_id") or ""): str(step.get("status") or "")
            for step in state.get("steps", []) or [] if isinstance(step, dict)
        },
        "stage_statuses": {
            str(stage_id): str(stage.get("status") or "")
            for stage_id, stage in provenance.get("stages", {}).items()
        },
        "artifact_heads": dict(provenance.get("artifact_heads") or {}),
    }


def _step_ancestor_ids(state: PlanState, step_id: str) -> Tuple[set[str], List[str]]:
    graph = {
        str(step.get("step_id") or ""): [str(item) for item in step.get("depends_on", step.get("dependencies", [])) or []]
        for step in state.get("steps", []) or [] if isinstance(step, dict) and str(step.get("step_id") or "")
    }
    ancestors: set[str] = set()
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle: List[str] = []

    def visit(node: str, trail: List[str]) -> None:
        if cycle or node in visited:
            return
        if node in visiting:
            start = trail.index(node) if node in trail else 0
            cycle.extend([*trail[start:], node])
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            ancestors.add(dependency)
            visit(dependency, [*trail, node])
        visiting.remove(node)
        visited.add(node)

    visit(str(step_id), [])
    return ancestors, cycle


def _stage_descendant_ids(provenance: Dict[str, Any], stage_id: str) -> Tuple[set[str], List[str]]:
    stages = provenance.get("stages", {})
    reverse: Dict[str, List[str]] = {str(item): [] for item in stages}
    for child_id, stage in stages.items():
        for dependency in stage.get("depends_on_stage_ids", []) or []:
            reverse.setdefault(str(dependency), []).append(str(child_id))
    descendants: set[str] = set()
    stack = [str(stage_id)]
    while stack:
        parent = stack.pop()
        for child in reverse.get(parent, []):
            if child == str(stage_id):
                return set(), [str(stage_id), parent, child]
            if child not in descendants:
                descendants.add(child)
                stack.append(child)
    return descendants, []


def _invalidate_provenance(
    state: PlanState,
    *,
    step_ids: set[str] | None = None,
    stage_ids: set[str] | None = None,
    invalid_artifact_ids: set[str] | None = None,
) -> List[str]:
    provenance = ensure_provenance_state(state)
    affected_steps = set(step_ids or set())
    affected_stages = set(stage_ids or set())
    explicit_invalid = set(invalid_artifact_ids or set())
    changed: List[str] = []
    affected_attempts: set[str] = set()
    for version_id, artifact in provenance.get("artifacts", {}).items():
        artifact_id = str(artifact.get("artifact_id") or "")
        producer_step = str(artifact.get("producer_step_id") or "")
        producer_stage = str(artifact.get("producer_stage_id") or "")
        if artifact_id not in explicit_invalid and producer_step not in affected_steps and producer_stage not in affected_stages:
            continue
        old_status = str(artifact.get("status") or "")
        if old_status in {"expected", "missing"} and artifact_id not in explicit_invalid:
            continue
        artifact["status"] = "invalid" if artifact_id in explicit_invalid else "stale"
        artifact["invalidated_at"] = _now()
        artifact["invalidation_reason"] = "recovery_rollback"
        if str(artifact.get("producer_attempt_id") or ""):
            affected_attempts.add(str(artifact["producer_attempt_id"]))
        if old_status != artifact["status"]:
            changed.append(artifact_id)
    for attempt_id in affected_attempts:
        attempt = provenance.get("attempts", {}).get(attempt_id)
        if isinstance(attempt, dict):
            attempt["status"] = "invalidated"
            attempt["invalidated_at"] = _now()
    provenance["updated_at"] = _now()
    state["provenance_state"] = provenance
    return list(dict.fromkeys(changed))


def _validate_recovery_artifact_ids(provenance: Dict[str, Any], artifact_ids: set[str]) -> None:
    if not artifact_ids:
        return
    known_ids = set(str(item) for item in provenance.get("artifact_heads", {}))
    known_ids.update(
        str(item.get("artifact_id") or "")
        for item in provenance.get("artifacts", {}).values()
        if isinstance(item, dict) and str(item.get("artifact_id") or "")
    )
    unknown = sorted(artifact_ids - known_ids)
    if unknown:
        raise ValueError("RecoveryDecision references artifacts outside the current task: " + ", ".join(unknown))


def _apply_upstream_step_rollback(state: PlanState, value: Dict[str, Any]) -> Dict[str, Any]:
    failed_step_id = str(value.get("failed_step_id") or value.get("step_id") or "")
    target_id = str(value.get("rollback_to_step_id") or value.get("first_bad_step_id") or "")
    steps = {
        str(step.get("step_id") or ""): step
        for step in state.get("steps", []) or [] if isinstance(step, dict)
    }
    if not failed_step_id or failed_step_id not in steps:
        raise ValueError(f"Rollback failed step does not exist: {failed_step_id or '<missing>'}")
    if not target_id or target_id not in steps:
        raise ValueError(f"Rollback target does not exist: {target_id or '<missing>'}")
    if target_id == failed_step_id:
        raise ValueError("Upstream rollback target must differ from the failed step")
    if str(steps[target_id].get("status") or "") == STATUS_BLOCKED:
        raise ValueError("Rollback target is permanently blocked")
    ancestors, cycle = _step_ancestor_ids(state, failed_step_id)
    if cycle:
        raise ValueError("Plan dependency cycle prevents rollback: " + " -> ".join(cycle))
    provenance_relation = False
    invalid_ids = {str(item) for item in value.get("invalid_artifact_ids", []) or [] if str(item)}
    provenance = ensure_provenance_state(state)
    _validate_recovery_artifact_ids(provenance, invalid_ids)
    for artifact_id in invalid_ids:
        head = provenance.get("artifact_heads", {}).get(artifact_id)
        artifact = provenance.get("artifacts", {}).get(head, {})
        provenance_relation = provenance_relation or (
            str(artifact.get("producer_step_id") or "") == target_id
            and artifact_id in set(steps[failed_step_id].get("input_artifact_ids", []) or [])
        )
    if target_id not in ancestors and not provenance_relation:
        raise ValueError(f"Rollback target {target_id} is not an ancestor or input producer of {failed_step_id}")
    descendants_result = get_step_descendants(state, target_id)
    if descendants_result.get("status") == "cycle_error":
        raise ValueError("Plan dependency cycle prevents rollback")
    descendants = set(descendants_result.get("step_ids") or [])
    affected = descendants if bool(value.get("invalidate_descendants", True)) else {failed_step_id}
    steps[target_id]["status"] = STATUS_RETRY_PENDING
    _append_evidence(steps[target_id], "rollback_target", str(value.get("reason") or "upstream rollback"))
    for step_id in affected:
        if step_id == target_id or step_id not in steps:
            continue
        steps[step_id]["status"] = STATUS_WAITING_UPSTREAM if step_id == failed_step_id else STATUS_INVALIDATED
        _append_evidence(steps[step_id], "invalidated_by_upstream", target_id)
    invalidated_artifacts = _invalidate_provenance(
        state,
        step_ids={target_id, *affected},
        invalid_artifact_ids=invalid_ids,
    )
    from agent_core.plan_bindings import invalidate_producer_bindings

    invalidate_producer_bindings(
        state,
        producer_step_id=target_id,
        reason="upstream producer superseded by Recovery rollback",
    )
    state["current_step_id"] = target_id
    state["current_stage_id"] = ""
    set_task_status(state, TASK_STATUS_REPAIRING, f"rollback to {target_id}")
    return {
        "current_step_id": target_id,
        "current_stage_id": "",
        "affected_step_ids": sorted(affected | {target_id}),
        "affected_stage_ids": [],
        "invalidated_artifact_ids": invalidated_artifacts,
        "rollback_target": target_id,
    }


def _apply_manifest_stage_rewind(state: PlanState, value: Dict[str, Any]) -> Dict[str, Any]:
    provenance = ensure_provenance_state(state)
    manifest_id = str(value.get("manifest_id") or "")
    failed_stage_id = str(value.get("failed_stage_id") or "")
    target_id = str(value.get("rewind_to_stage_id") or "")
    manifest = provenance.get("manifests", {}).get(manifest_id)
    failed = provenance.get("stages", {}).get(failed_stage_id)
    target = provenance.get("stages", {}).get(target_id)
    if not manifest or not failed or not target:
        raise ValueError("Manifest rewind references an unknown manifest or stage")
    if any(str(stage.get("manifest_id") or "") != manifest_id for stage in (failed, target)):
        raise ValueError("Manifest rewind cannot cross manifest boundaries")
    ancestors: set[str] = set()
    stack = list(failed.get("depends_on_stage_ids", []) or [])
    while stack:
        stage_id = str(stack.pop())
        if stage_id in ancestors:
            continue
        ancestors.add(stage_id)
        stage = provenance.get("stages", {}).get(stage_id, {})
        stack.extend(stage.get("depends_on_stage_ids", []) or [])
    if target_id != failed_stage_id and target_id not in ancestors:
        raise ValueError("Manifest rewind target must be the failed stage or one of its ancestors")
    descendants, cycle = _stage_descendant_ids(provenance, target_id)
    if cycle:
        raise ValueError("Manifest stage dependency cycle prevents rewind")
    affected_stages = descendants | {target_id}
    target["status"] = STATUS_RETRY_PENDING
    target["current_attempt_id"] = ""
    for stage_id in descendants:
        stage = provenance.get("stages", {}).get(stage_id)
        if isinstance(stage, dict):
            stage["status"] = STATUS_INVALIDATED
            stage["current_attempt_id"] = ""
    plan_step_id = str(value.get("plan_step_id") or manifest.get("plan_step_id") or target.get("plan_step_id") or "")
    step = _step_by_id(state, plan_step_id)
    if not step:
        raise ValueError("Manifest rewind plan step does not exist")
    execution = dict(step.get("renderer_execution") or {})
    invalid_action_ids = {
        str(provenance.get("stages", {}).get(stage_id, {}).get("manifest_action_id") or "")
        for stage_id in affected_stages
    }
    execution["completed_action_ids"] = [
        str(item) for item in execution.get("completed_action_ids", []) or []
        if str(item) not in invalid_action_ids
    ]
    execution["current_action_id"] = str(target.get("manifest_action_id") or "")
    execution["current_stage_id"] = target_id
    step["renderer_execution"] = execution
    step["status"] = STATUS_RETRY_PENDING
    _append_evidence(step, "manifest_stage_rewind", target_id)
    invalid_ids = {str(item) for item in value.get("invalid_artifact_ids", []) or [] if str(item)}
    _validate_recovery_artifact_ids(provenance, invalid_ids)
    invalidated_artifacts = _invalidate_provenance(
        state,
        stage_ids=affected_stages,
        invalid_artifact_ids=invalid_ids,
    )
    state["current_step_id"] = plan_step_id
    state["current_stage_id"] = target_id
    set_task_status(state, TASK_STATUS_REPAIRING, f"rewind manifest stage {target_id}")
    return {
        "current_step_id": plan_step_id,
        "current_stage_id": target_id,
        "affected_step_ids": [plan_step_id],
        "affected_stage_ids": sorted(affected_stages),
        "invalidated_artifact_ids": invalidated_artifacts,
        "rollback_target": target_id,
    }


_NON_PRIMARY_RECOVERY_COMMAND_ROLES = {
    "probe", "diagnostic", "optional_probe", "capability_probe",
}


def _ordered_primary_step_attempts(
    state: Mapping[str, Any],
    step_id: str,
) -> List[Dict[str, Any]]:
    """Return the single canonical attempt order used by recovery scope checks."""
    provenance = ensure_provenance_state(state)
    indexed = [
        (index, dict(item))
        for index, item in enumerate((provenance.get("attempts") or {}).values())
        if isinstance(item, Mapping)
        and str(item.get("step_id") or "") == str(step_id or "")
        and str(item.get("command_role") or "primary").strip().lower()
        not in _NON_PRIMARY_RECOVERY_COMMAND_ROLES
    ]
    # Timestamps are only second-granular in old checkpoints.  The insertion
    # index is therefore the deterministic tie-breaker; opaque IDs are not time.
    indexed.sort(key=lambda pair: (
        str(
            pair[1].get("created_at")
            or pair[1].get("started_at")
            or pair[1].get("finished_at")
            or pair[1].get("updated_at")
            or ""
        ),
        pair[0],
    ))
    return [item for _, item in indexed]


def _primary_step_attempt_ordinal(
    state: Mapping[str, Any],
    step_id: str,
    attempt_id: str,
) -> int:
    return next(
        (
            index
            for index, item in enumerate(
                _ordered_primary_step_attempts(state, step_id),
                start=1,
            )
            if str(item.get("attempt_id") or item.get("execution_id") or "")
            == str(attempt_id or "")
        ),
        0,
    )


def bind_recovery_generation_authority(
    state: Mapping[str, Any], recovery_state: Mapping[str, Any], *,
    decision_id: str, envelope: Mapping[str, Any], source_attempt_id: str = "",
) -> Dict[str, Any]:
    """Mechanical owner/claim binding shared by admitted legacy and typed calls.

    No routing decision, persistence, budget charge or step transition occurs
    here. The caller commits this value with its validated decision receipt.
    """
    from agent_core.recovery import RecoveryExecutionEnvelope
    checked = RecoveryExecutionEnvelope.from_mapping(envelope)
    step_id = str(state.get("current_step_id") or "")
    if (checked.step_id != step_id or checked.recovery_decision_id != decision_id
            or int(state.get("plan_revision") or 0) <= 0):
        raise ValueError("RECOVERY_GENERATION_AUTHORITY_SCOPE_MISMATCH")
    recovery_state = deepcopy(dict(recovery_state))
    prior_active_recovery = dict(recovery_state.get("active_recovery") or {})
    if (
        prior_active_recovery
        and str(prior_active_recovery.get("status") or "").lower()
        in {"pending", "dispatched"}
        and str(prior_active_recovery.get("recovery_id") or "") != decision_id
    ):
        retired = deepcopy(prior_active_recovery)
        retired.update({
            "status": "closed",
            "closed_reason": "SUPERSEDED_BY_NEWER_RECOVERY_DECISION",
            "closed_at": _now(),
        })
        recovery_state["active_recovery_history"] = [
            *list(recovery_state.get("active_recovery_history") or []),
            retired,
        ]
    target_generation_identity = {
        "task_id": str(state.get("task_id") or ""),
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or ""),
        "step_id": step_id,
        "recovery_decision_id": decision_id,
        "envelope_hash": str(envelope.get("immutable_hash") or ""),
        "operation": "recovery_generate",
    }
    target_generation_id = "generation-" + hashlib.sha256(
        json.dumps(
            target_generation_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    recovery_state["active_recovery"] = {
        "schema_version": 1,
        "recovery_id": decision_id,
        "plan_revision": int(state.get("plan_revision") or 0),
        "step_id": step_id,
        "source_attempt_id": str(
            source_attempt_id or ""
        ),
        "status": "pending",
        "target_route": "generate",
        "envelope_id": str(envelope.get("immutable_hash") or ""),
        "target_generation_id": target_generation_id,
        "created_at": _now(),
    }
    recovery_state["generation_claim"] = {
        "schema_version": 1,
        "target_generation_id": target_generation_id,
        "recovery_id": decision_id,
        "status": "issued",
        "provider_idempotency_key": target_generation_id,
        "remote_delivery_semantics": "at_least_once_without_provider_idempotency",
        "issued_at": _now(),
    }
    recovery_state["active_recovery_envelope"] = envelope
    return recovery_state


def apply_recovery_decision(
    state: PlanState,
    decision: Any,
    *,
    execution_envelope: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist one RecoveryPolicy decision exactly once.

    This is the only API that may couple a recovery transition with budget or
    plan-status mutation. Evidence producers may construct advisory data, but
    they must not increment recovery budgets or alter step status directly.
    """
    value = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision or {})
    if not bool(value.get("identity_finalized")):
        raise ValueError("RecoveryDecision must be finalized before application")
    decision_id = str(value.get("idempotency_key") or value.get("decision_id") or "").strip()
    if not decision_id:
        raise ValueError("RecoveryDecision requires a stable decision_id")
    if str(value.get("decision_owner") or "") != "RecoveryPolicy":
        raise ValueError("Only RecoveryPolicy decisions may be applied")
    value.setdefault("status", "pending")
    if str(value.get("status") or "").strip().lower() != "pending":
        raise ValueError("Only pending RecoveryPolicy transitions may be applied")
    value.setdefault("trigger_event_id", str(value.get("attempt_id") or ""))
    value.setdefault("trigger_node", str(value.get("source_node") or ""))
    value.setdefault("target_route", str(value.get("next_route") or ""))
    value.setdefault("step_attempt", 0)
    decision_kind = str(value.get("kind") or "").strip().upper()
    if decision_kind in {"COMPLETE_STEP", "COMPLETE_TASK"}:
        completion_source = str(value.get("trigger_node") or value.get("source_node") or "").strip().lower()
        if (
            completion_source != "supervisor"
            or str(value.get("reason_code") or "") != "completion_contract_satisfied"
            or not list(value.get("evidence") or [])
        ):
            raise ValueError(
                "COMPLETE_STEP/COMPLETE_TASK requires Supervisor-owned CompletionGate evidence"
            )

    task_state = dict(state.get("task_state") or {})
    recovery_state = dict(task_state.get("recovery_state") or {})
    applied_ids = [
        str(item)
        for item in recovery_state.get("applied_decision_ids") or []
        if str(item)
    ]
    # Old checkpoints may have complete terminal audit evidence but predate the
    # applied_decision_ids index.  Treat every durable historical occurrence as
    # already applied so replay can never recreate route authority.
    historical_decisions = [
        *list(recovery_state.get("decision_history") or []),
        *list(recovery_state.get("transition_history") or []),
        recovery_state.get("last_recovery_decision") or {},
        recovery_state.get("last_consumed_transition") or {},
    ]
    applied_ids = list(dict.fromkeys([
        *applied_ids,
        *[
            str(item.get("decision_id") or item.get("idempotency_key") or "")
            for item in historical_decisions
            if isinstance(item, Mapping)
            and str(item.get("decision_id") or item.get("idempotency_key") or "")
        ],
    ]))
    incident_id = str(value.get("incident_id") or "").strip()
    if not incident_id:
        incident_identity = {
            "task_id": str(value.get("task_id") or state.get("task_id") or ""),
            "plan_id": str(value.get("plan_id") or state.get("plan_id") or ""),
            "plan_version": str(value.get("plan_version") or state.get("plan_version") or ""),
            "step_id": str(value.get("step_id") or state.get("current_step_id") or ""),
            "action_id": str(value.get("action_id") or ""),
            "attempt_id": str(value.get("attempt_id") or ""),
            "failure_fingerprint": str(value.get("failure_fingerprint") or value.get("reason_code") or ""),
        }
        incident_id = "incident-" + hashlib.sha256(
            json.dumps(incident_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        value["incident_id"] = incident_id
    committed_by_incident = dict(recovery_state.get("committed_decision_by_incident") or {})
    committed_decision_id = str(committed_by_incident.get(incident_id) or "")

    def record_rejected(reason: str) -> None:
        """Report a rejected candidate without mutating authoritative state.

        The durable transition/audit owner records the exception outside this
        atomic candidate application. Writing a rejection into ``state``
        before validation made failed decisions observable as partial commits.
        """
        logger.warning(
            "Recovery decision rejected before commit: decision=%s incident=%s reason=%s",
            decision_id,
            incident_id,
            str(reason)[:2000],
        )

    if committed_decision_id and committed_decision_id != decision_id:
        reason = f"incident already has a committed decision: {incident_id} -> {committed_decision_id}"
        record_rejected(reason)
        raise ValueError(reason)
    budgets_before = dict(task_state.get("budgets") or {})
    if decision_id in applied_ids:
        active_transition = dict(recovery_state.get("active_transition") or {})
        active_pending = bool(
            str(active_transition.get("decision_id") or active_transition.get("idempotency_key") or "")
            == decision_id
            and str(active_transition.get("status") or "").strip().lower() == "pending"
        )
        terminal_candidates = [
            dict(item)
            for item in (
                recovery_state.get("last_recovery_decision") or {},
                recovery_state.get("last_consumed_transition") or {},
                *reversed(list(recovery_state.get("transition_history") or [])),
            )
            if isinstance(item, Mapping)
            and str(item.get("decision_id") or item.get("idempotency_key") or "")
            == decision_id
        ]
        terminal_status = next(
            (
                str(item.get("status") or "").strip().lower()
                for item in terminal_candidates
                if str(item.get("status") or "").strip().lower()
                in {"consumed", "superseded", "expired"}
            ),
            "consumed",
        )
        return {
            "applied": False,
            "duplicate": True,
            "decision_id": decision_id,
            "budget_before": budgets_before,
            "budget_after": dict(budgets_before),
            "next_route": str(active_transition.get("target_route") or active_transition.get("next_route") or "") if active_pending else "",
            "transition_status": "pending" if active_pending else terminal_status,
            "current_step_id": str(state.get("current_step_id") or ""),
            "current_stage_id": str(state.get("current_stage_id") or ""),
        }

    expected_task_id = str(value.get("task_id") or "")
    expected_plan_id = str(value.get("plan_id") or "")
    expected_plan_version = str(value.get("plan_version") or "")
    if expected_task_id and expected_task_id != str(state.get("task_id") or ""):
        reason = "RecoveryDecision belongs to a different task"
        record_rejected(reason)
        raise ValueError(reason)
    if expected_plan_id and expected_plan_id != str(state.get("plan_id") or ""):
        reason = "RecoveryDecision belongs to a different plan"
        record_rejected(reason)
        raise ValueError(reason)
    if expected_plan_version and expected_plan_version != str(state.get("plan_version") or ""):
        reason = "RecoveryDecision plan_version is stale"
        record_rejected(reason)
        raise ValueError(reason)
    expected_step_id = str(value.get("step_id") or "")
    current_step_id = str(state.get("current_step_id") or "")
    if expected_step_id and current_step_id and expected_step_id != current_step_id:
        reason = "RecoveryDecision step_id is stale"
        record_rejected(reason)
        raise ValueError(reason)

    trigger_attempt_id = str(value.get("attempt_id") or value.get("trigger_event_id") or "")
    provenance = ensure_provenance_state(state)
    trigger_attempt = dict((provenance.get("attempts") or {}).get(trigger_attempt_id) or {})
    if not int(value.get("step_attempt") or 0):
        derived_ordinal = _primary_step_attempt_ordinal(
            state,
            str(value.get("step_id") or current_step_id),
            trigger_attempt_id,
        )
        value["step_attempt"] = max(
            0,
            _int(
                derived_ordinal
                or trigger_attempt.get("attempt_ordinal")
                or trigger_attempt.get("sequence"),
                0,
            ),
        )

    original_state = state
    state = deepcopy(state)
    state_before = _recovery_state_snapshot(state)

    budget_name = str(value.get("consume_budget") or "").strip()
    allowed_budgets = {
        "action_format_repair_count",
        "generate_attempts_by_step",
        "diagnostic_probe_count",
        "execution_retry_count",
        "no_progress_count",
        "plan_patch_count",
        "stage_rewind_count",
        "upstream_rollback_count",
    }
    if budget_name:
        if budget_name not in allowed_budgets:
            raise ValueError(f"Unsupported recovery budget: {budget_name}")
        task_state = dict(state.get("task_state") or {})
        budgets = dict(task_state.get("budgets") or {})
        step_key = str(value.get("step_id") or "task")
        if budget_name == "generate_attempts_by_step":
            by_step = dict(budgets.get("generate_attempts_by_step") or {})
            by_step[step_key] = _int(by_step.get(step_key), 0) + 1
            budgets["generate_attempts_by_step"] = by_step
        else:
            increment_plan_budget(state, budget_name)
            task_state = dict(state.get("task_state") or {})
            budgets = dict(task_state.get("budgets") or {})
        if budget_name == "action_format_repair_count":
            by_step = dict(budgets.get("action_format_repair_by_step") or {})
            by_step[step_key] = _int(by_step.get(step_key), 0) + 1
            budgets["action_format_repair_by_step"] = by_step
        elif budget_name == "diagnostic_probe_count":
            by_step = dict(budgets.get("diagnostic_probes_by_step") or {})
            by_step[step_key] = _int(by_step.get(step_key), 0) + 1
            budgets["diagnostic_probes_by_step"] = by_step
        if budget_name in {"action_format_repair_count", "generate_attempts_by_step"}:
            invalid_hash = next(
                (
                    str(item).split("=", 1)[1]
                    for item in value.get("evidence") or []
                    if str(item).startswith("invalid_response_hash=")
                ),
                "",
            )
            if invalid_hash:
                hashes_by_step = dict(budgets.get("invalid_action_hashes_by_step") or {})
                hashes = dict(hashes_by_step.get(step_key) or {})
                hashes[invalid_hash] = _int(hashes.get(invalid_hash), 0) + 1
                hashes_by_step[step_key] = hashes
                budgets["invalid_action_hashes_by_step"] = hashes_by_step
        task_state["budgets"] = budgets
        state["task_state"] = task_state

    kind = str(value.get("kind") or "").strip().upper()
    transition = {
        "current_step_id": str(state.get("current_step_id") or ""),
        "current_stage_id": str(state.get("current_stage_id") or ""),
        "affected_step_ids": [],
        "affected_stage_ids": [],
        "invalidated_artifact_ids": [],
        "rollback_target": "",
    }
    if kind == "ROLLBACK_UPSTREAM_STEP":
        transition = _apply_upstream_step_rollback(state, value)
    elif kind == "REWIND_MANIFEST_STAGE":
        transition = _apply_manifest_stage_rewind(state, value)
    step_id = str(value.get("step_id") or "").strip()
    current_step = next(
        (
            item for item in state.get("steps", []) or []
            if isinstance(item, dict) and step_id and str(item.get("step_id") or "") == step_id
        ),
        None,
    )
    if current_step is None:
        current_step = _first_step_by_status(state, tuple(STEP_ACTIVE_STATUSES))

    if current_step is not None:
        if kind == "FORMAT_REPAIR":
            current_step["status"] = STATUS_REPAIRING
        elif kind == "RETRY_CURRENT_ACTION":
            current_step["status"] = STATUS_RETRY_PENDING
        elif kind == "ACCEPT_EXECUTION_FOR_VERIFICATION":
            current_step["status"] = STATUS_VERIFICATION_PENDING
            current_step["verification_attempt_id"] = str(
                value.get("attempt_id") or value.get("trigger_event_id") or ""
            )
        elif kind == "RUN_ALLOWED_PROBE":
            current_step["status"] = STATUS_DIAGNOSING
        elif kind == "ESCALATE":
            # Escalation is a supervisor handoff, not a terminal verdict.
            current_step["status"] = STATUS_AWAITING_SUPERVISOR
        elif kind == "PLAN_PATCH_REQUEST":
            current_step["status"] = STATUS_REPAIRING
        _append_evidence(
            current_step,
            "recovery_decision",
            f"{kind}: {str(value.get('reason_code') or '')}",
        )

    if kind == "BLOCKED":
        block_plan_and_downstream(state, str(value.get("reason") or "RecoveryPolicy blocked the task"))
    elif kind == "ESCALATE":
        set_task_status(state, TASK_STATUS_DIAGNOSING, str(value.get("reason") or "RecoveryPolicy escalation"))
    elif kind == "COMPLETE_STEP":
        advance_plan_after_verified_step(
            state,
            str(value.get("reason") or "completion contract satisfied"),
            expected_step_id=step_id,
            transition_id=decision_id,
        )
    elif kind == "COMPLETE_TASK":
        unfinished = [
            item for item in state.get("steps", []) or []
            if isinstance(item, dict) and str(item.get("status") or "") not in STEP_SUCCESS_STATUSES
        ]
        if unfinished:
            raise ValueError("COMPLETE_TASK cannot be applied while plan steps remain unfinished")
        set_task_status(state, TASK_STATUS_COMPLETED, str(value.get("reason") or "final contract satisfied"))

    if kind in {"COMPLETE_STEP", "COMPLETE_TASK"}:
        attempt_id = str(value.get("attempt_id") or "")
        provenance = ensure_provenance_state(state)
        attempt = provenance.get("attempts", {}).get(attempt_id)
        if isinstance(attempt, dict) and str(attempt.get("status") or "").upper() in {
            ATTEMPT_EVIDENCE_COMMITTED, ATTEMPT_VALIDATED,
            ATTEMPT_VALIDATION_PASSED,
        }:
            attempt["status"] = ATTEMPT_COMPLETED
            attempt["updated_at"] = _now()
            provenance["updated_at"] = _now()
            state["provenance_state"] = provenance

    task_state = dict(state.get("task_state") or {})
    if kind == "ACCEPT_EXECUTION_FOR_VERIFICATION":
        task_state["verification_pending"] = {
            "step_id": str((current_step or {}).get("step_id") or step_id),
            "attempt_id": str(value.get("attempt_id") or value.get("trigger_event_id") or ""),
            "recorded_at": _now(),
        }
        task_state["resume_route_hint"] = "supervisor"
    elif kind == "ESCALATE":
        task_state.pop("verification_pending", None)
        task_state["resume_route_hint"] = "supervisor"
    elif kind in {
        "FORMAT_REPAIR", "RETRY_CURRENT_ACTION", "RUN_ALLOWED_PROBE",
        "PLAN_PATCH_REQUEST", "BLOCKED", "COMPLETE_STEP",
        "COMPLETE_TASK", "REWIND_MANIFEST_STAGE", "ROLLBACK_UPSTREAM_STEP",
    }:
        task_state.pop("verification_pending", None)
        task_state.pop("resume_route_hint", None)
    recovery_state = dict(task_state.get("recovery_state") or {})
    transition_history = list(recovery_state.get("transition_history") or [])
    prior_active = dict(recovery_state.get("active_transition") or {})
    if (
        prior_active
        and str(prior_active.get("status") or "").strip().lower() == "pending"
        and str(prior_active.get("decision_id") or prior_active.get("idempotency_key") or "") != decision_id
    ):
        prior_active.update({
            "status": "superseded",
            "superseded_reason": "SUPERSEDED_BY_NEWER_RECOVERY_DECISION",
            "superseded_at": _now(),
        })
        transition_history.append(prior_active)
    active_transition = deepcopy(value)
    active_transition.update({
        "decision_id": decision_id,
        "idempotency_key": decision_id,
        "status": "pending",
        "trigger_node": str(value.get("trigger_node") or value.get("source_node") or ""),
        "trigger_event_id": str(value.get("trigger_event_id") or value.get("attempt_id") or ""),
        "target_route": str(value.get("target_route") or value.get("next_route") or ""),
        "created_at": str(value.get("created_at") or _now()),
    })
    value.update({
        "status": "pending",
        "trigger_node": active_transition["trigger_node"],
        "trigger_event_id": active_transition["trigger_event_id"],
        "target_route": active_transition["target_route"],
    })
    recovery_state.update({
        "schema_version": 4,
        "phase": "pending_dispatch",
        "status": kind,
        "decision_owner": "RecoveryPolicy",
        "last_recovery_decision": value,
        "last_decision_reason": str(value.get("reason") or ""),
        "current_action_id": str(value.get("action_id") or ""),
        "step_id": step_id,
        "active_transition": active_transition,
        "transition_history": transition_history,
        "decision_history": [
            *list(recovery_state.get("decision_history") or []),
            deepcopy(value),
        ],
        "applied_decision_ids": list(dict.fromkeys([*applied_ids, decision_id])),
        "committed_decision_by_incident": {
            **committed_by_incident,
            incident_id: decision_id,
        },
        "updated_at": _now(),
    })
    if (
        str(active_transition.get("target_route") or "").strip().lower()
        == "generate"
        and kind not in {"COMPLETE_STEP", "COMPLETE_TASK"}
    ):
        envelope = deepcopy(dict(execution_envelope or {}))
        if (
            str(envelope.get("recovery_decision_id") or "") != decision_id
            or dict(envelope.get("source_scope") or {})
            != dict(value.get("source_scope") or {})
        ):
            raise ValueError("Recovery decision and execution envelope scope mismatch")
        recovery_state = bind_recovery_generation_authority(
            state, recovery_state, decision_id=decision_id, envelope=envelope,
            source_attempt_id=str(value.get("attempt_id") or value.get("trigger_event_id") or ""),
        )
    elif kind in {"COMPLETE_STEP", "COMPLETE_TASK"}:
        # Completion decisions own one route dispatch only.  They are audit
        # history, never authority for a later recovery Generate.
        recovery_state["active_recovery"] = {}
        recovery_state["active_recovery_envelope"] = {}
    audit_event = {
        "task_id": str(state.get("task_id") or ""),
        "step_id": step_id,
        "action_id": str(value.get("action_id") or ""),
        "attempt_id": str(value.get("attempt_id") or ""),
        "decision_id": decision_id,
        "incident_id": incident_id,
        "decision_owner": "RecoveryPolicy",
        "decision_kind": kind,
        "reason_code": str(value.get("reason_code") or ""),
        "reason": str(value.get("reason") or "")[:2000],
        "evidence_summary": [str(item)[:500] for item in value.get("evidence") or []][:12],
        "manifest_status": str(dict((current_step or {}).get("renderer_execution") or {}).get("manifest_status") or "") if current_step else "",
        "budget_before": budgets_before,
        "budget_after": dict(task_state.get("budgets") or {}),
        "next_route": str(value.get("next_route") or ""),
        "applied_at": _now(),
        "apply_status": "applied",
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or ""),
        "failed_step_id": str(value.get("failed_step_id") or value.get("step_id") or ""),
        "failed_stage_id": str(value.get("failed_stage_id") or ""),
        "first_bad_step_id": str(value.get("first_bad_step_id") or ""),
        "rollback_target": transition.get("rollback_target") or "",
        "affected_steps": list(transition.get("affected_step_ids") or []),
        "affected_stages": list(transition.get("affected_stage_ids") or []),
        "invalidated_artifacts": list(transition.get("invalidated_artifact_ids") or []),
        "state_before": state_before,
        "state_after": _recovery_state_snapshot(state),
        "evidence_refs": [str(item)[:500] for item in value.get("evidence_refs") or []][:20],
    }
    lifecycle_events = []
    for lifecycle_status in ("created", "validated", "applied"):
        event = dict(audit_event)
        event["apply_status"] = lifecycle_status
        event["recorded_at"] = _now()
        lifecycle_events.append(event)
    recovery_state["decision_audit"] = [
        *list(recovery_state.get("decision_audit") or []),
        *lifecycle_events,
    ]
    task_state["recovery_state"] = recovery_state
    task_state["last_recovery_decision"] = value
    state["task_state"] = task_state
    record_scoped_evidence(
        state,
        "recovery",
        value,
        action_id=str(value.get("action_id") or ""),
        execution_attempt_id=str(value.get("attempt_id") or ""),
        failure_fingerprint=str(value.get("source_failure_fingerprint") or ""),
    )
    normalized = task_status(state)
    if normalized == TASK_STATUS_COMPLETED:
        state["execution_status"] = "completed"
    elif normalized in {TASK_STATUS_FAILED, TASK_STATUS_BLOCKED, TASK_STATUS_CANCELLED}:
        state["execution_status"] = normalized
    elif normalized in {TASK_STATUS_RUNNING, TASK_STATUS_DIAGNOSING, TASK_STATUS_REPAIRING}:
        state.setdefault("execution_status", "not_started")
    state["updated_at"] = _now()
    original_state.clear()
    original_state.update(state)
    return {
        "applied": True,
        "duplicate": False,
        "decision_id": decision_id,
        "budget_before": budgets_before,
        "budget_after": dict(task_state.get("budgets") or {}),
        "next_route": str(value.get("next_route") or ""),
        "audit": audit_event,
        **transition,
    }


def update_recovery_generation_claim(
    state: PlanState,
    *,
    target_generation_id: str,
    status: str,
    action_fingerprint: str = "",
    detail: str = "",
) -> Dict[str, Any]:
    """Advance the durable recovery Generate claim without duplicating adoption."""
    allowed = {
        "issued", "claimed", "provider_completed", "action_adopted",
        "failed", "superseded", "outcome_unknown",
    }
    next_status = str(status or "").strip().lower()
    if next_status not in allowed:
        raise ValueError(f"Unsupported recovery generation claim status: {next_status}")
    task_state = dict(state.get("task_state") or {})
    recovery_state = dict(task_state.get("recovery_state") or {})
    active = dict(recovery_state.get("active_recovery") or {})
    claim = dict(recovery_state.get("generation_claim") or {})
    expected = str(active.get("target_generation_id") or claim.get("target_generation_id") or "")
    if not expected or expected != str(target_generation_id or ""):
        raise ValueError("RECOVERY_GENERATION_TARGET_MISMATCH")
    current_status = str(claim.get("status") or "issued").lower()
    if current_status == "action_adopted":
        if next_status == "action_adopted" and (
            not action_fingerprint
            or str(claim.get("action_fingerprint") or "") == action_fingerprint
        ):
            return {"changed": False, "duplicate": True, **claim}
        # An action may still be rejected by a deterministic Execute TOCTOU
        # preflight before process launch.  That transition revokes its durable
        # pending authority and supersedes the claim in the same PlanState CAS.
        if next_status != "superseded":
            raise ValueError("RECOVERY_GENERATION_ALREADY_ADOPTED")
    transitions = {
        "issued": {"claimed", "failed", "superseded"},
        "claimed": {"provider_completed", "failed", "outcome_unknown", "superseded"},
        "provider_completed": {"action_adopted", "failed", "superseded"},
        "outcome_unknown": {"claimed", "provider_completed", "failed", "superseded"},
        "failed": set(),
        "action_adopted": {"superseded"},
        "superseded": set(),
    }
    if next_status != current_status and next_status not in transitions.get(current_status, set()):
        raise ValueError(
            f"RECOVERY_GENERATION_INVALID_TRANSITION:{current_status}->{next_status}"
        )
    claim.update({
        "schema_version": 1,
        "target_generation_id": expected,
        "recovery_id": str(active.get("recovery_id") or claim.get("recovery_id") or ""),
        "status": next_status,
        "updated_at": _now(),
    })
    if action_fingerprint:
        claim["action_fingerprint"] = str(action_fingerprint)
    if detail:
        claim["detail"] = str(detail)[:2000]
    claim[f"{next_status}_at"] = _now()
    recovery_state["generation_claim"] = claim
    task_state["recovery_state"] = recovery_state
    state["task_state"] = task_state
    return {"changed": next_status != current_status, "duplicate": False, **claim}


def rollover_recovery_generation_claim_after_candidate_rejection(
    state: PlanState,
    *,
    target_generation_id: str,
    rejection_event_id: str,
    candidate_fingerprint: str,
    repair_ordinal: int,
    receipt_ref: str,
) -> Dict[str, Any]:
    """Roll one rejected Recovery Generate claim within the same envelope.

    The provider response has completed, but admission rejected the candidate
    before execution.  The immutable candidate-repair receipt authorizes a new
    provider generation, while the original Recovery decision and execution
    envelope remain the authority for execution mode and scope.  This mutates
    the same PlanState CAS object that receives the candidate-repair ledger.
    """

    task_state = dict(state.get("task_state") or {})
    recovery_state = dict(task_state.get("recovery_state") or {})
    active = dict(recovery_state.get("active_recovery") or {})
    claim = dict(recovery_state.get("generation_claim") or {})
    target_id = str(target_generation_id or "").strip()
    event_id = str(rejection_event_id or "").strip()
    candidate_id = str(candidate_fingerprint or "").strip()
    receipt_id = str(receipt_ref or "").strip()
    ordinal = int(repair_ordinal or 0)
    expected_target = str(
        active.get("target_generation_id")
        or claim.get("target_generation_id")
        or ""
    )
    if not target_id or target_id != expected_target:
        raise ValueError("CANDIDATE_REPAIR_RECOVERY_TARGET_MISMATCH")
    if not event_id:
        raise ValueError("CANDIDATE_REPAIR_REJECTION_EVENT_ID_MISSING")
    if not candidate_id:
        raise ValueError("CANDIDATE_REPAIR_CANDIDATE_FINGERPRINT_MISSING")
    if ordinal < 1:
        raise ValueError("CANDIDATE_REPAIR_ORDINAL_INVALID")
    if not receipt_id:
        raise ValueError("CANDIDATE_REPAIR_RECEIPT_REF_MISSING")
    if not active:
        raise ValueError("CANDIDATE_REPAIR_ACTIVE_RECOVERY_MISSING")
    recovery_id = str(active.get("recovery_id") or "").strip()
    envelope_id = str(active.get("envelope_id") or "").strip()
    envelope = dict(recovery_state.get("active_recovery_envelope") or {})
    if (
        not recovery_id
        or str(active.get("target_route") or "").strip().lower() != "generate"
        or str(active.get("status") or "").strip().lower()
        not in {"pending", "dispatched"}
    ):
        raise ValueError("CANDIDATE_REPAIR_ACTIVE_RECOVERY_NOT_GENERATABLE")
    if (
        str(active.get("step_id") or "").strip()
        != str(state.get("current_step_id") or "").strip()
        or int(active.get("plan_revision") or 0)
        != int(state.get("plan_revision") or 0)
    ):
        raise ValueError("CANDIDATE_REPAIR_ACTIVE_RECOVERY_SCOPE_STALE")
    if (
        not envelope_id
        or str(envelope.get("immutable_hash") or "").strip() != envelope_id
        or str(
            envelope.get("recovery_id")
            or envelope.get("recovery_decision_id")
            or ""
        ).strip() != recovery_id
        or str(envelope.get("step_id") or "").strip()
        != str(state.get("current_step_id") or "").strip()
    ):
        raise ValueError("CANDIDATE_REPAIR_RECOVERY_ENVELOPE_MISMATCH")
    if (
        str(claim.get("target_generation_id") or "").strip() != target_id
        or str(claim.get("recovery_id") or "").strip() != recovery_id
    ):
        raise ValueError("CANDIDATE_REPAIR_RECOVERY_CLAIM_SCOPE_MISMATCH")

    history = [
        deepcopy(dict(item))
        for item in recovery_state.get("generation_claim_history") or []
        if isinstance(item, Mapping)
    ]
    duplicate = next(
        (
            item for item in history
            if str(item.get("candidate_rejection_event_id") or "") == event_id
            and str(item.get("target_generation_id") or "") == target_id
        ),
        None,
    )
    if duplicate is not None:
        current_target = str(
            (recovery_state.get("generation_claim") or {}).get(
                "target_generation_id"
            )
            or ""
        )
        return {
            "changed": False,
            "duplicate": True,
            "prior_target_generation_id": target_id,
            "target_generation_id": current_target,
            "rejection_event_id": event_id,
        }

    current_status = str(claim.get("status") or "issued").strip().lower()
    if current_status not in {"provider_completed", "action_adopted"}:
        raise ValueError(
            "CANDIDATE_REPAIR_RECOVERY_CLAIM_NOT_COMPLETED:"
            + current_status
        )

    update_recovery_generation_claim(
        state,
        target_generation_id=target_id,
        status="superseded",
        detail=(
            "Recovery provider candidate was rejected before execution; "
            f"typed candidate repair owns {event_id}."
        ),
    )
    task_state = dict(state.get("task_state") or {})
    recovery_state = dict(task_state.get("recovery_state") or {})
    claim = dict(recovery_state.get("generation_claim") or {})
    claim.update({
        "candidate_rejection_event_id": event_id,
        "candidate_fingerprint": candidate_id,
        "repair_ordinal": ordinal,
        "receipt_ref": receipt_id,
    })
    history = [
        item for item in history
        if not (
            str(item.get("candidate_rejection_event_id") or "") == event_id
            and str(item.get("target_generation_id") or "") == target_id
        )
    ]
    history.append(deepcopy(claim))

    next_target_identity = {
        "recovery_id": recovery_id,
        "envelope_id": envelope_id,
        "parent_target_generation_id": target_id,
        "candidate_rejection_event_id": event_id,
        "candidate_fingerprint": candidate_id,
        "repair_ordinal": ordinal,
    }
    next_target_id = "generation-" + hashlib.sha256(
        json.dumps(
            next_target_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    now = _now()
    active.update({
        "target_generation_id": next_target_id,
        "updated_at": now,
    })
    recovery_state["active_recovery"] = active
    recovery_state["active_recovery_envelope"] = envelope
    recovery_state["generation_claim_history"] = history
    recovery_state["generation_claim"] = {
        "schema_version": 1,
        "target_generation_id": next_target_id,
        "parent_target_generation_id": target_id,
        "recovery_id": recovery_id,
        "status": "issued",
        "provider_idempotency_key": next_target_id,
        "remote_delivery_semantics": str(
            claim.get("remote_delivery_semantics")
            or "at_least_once_without_provider_idempotency"
        ),
        "candidate_rejection_event_id": event_id,
        "candidate_fingerprint": candidate_id,
        "repair_ordinal": ordinal,
        "receipt_ref": receipt_id,
        "issued_at": now,
    }
    task_state["recovery_state"] = recovery_state
    state["task_state"] = task_state
    state["updated_at"] = now
    return {
        "changed": True,
        "duplicate": False,
        "prior_target_generation_id": target_id,
        "target_generation_id": next_target_id,
        "rejection_event_id": event_id,
        "prior_claim_status": current_status,
        "claim_status": "issued",
        "active_recovery_status": str(active.get("status") or ""),
        "recovery_id": recovery_id,
        "envelope_id": envelope_id,
    }



def continue_recovery_generation_after_code_review(
    state: PlanState, *, action: Mapping[str, Any], decision: str, guidance: str,
) -> Dict[str, Any]:
    """Bind explicit human feedback to one new claim in the revocation CAS.

    Caller owns the actual code-review result. This is neither a mechanical
    candidate rejection nor another Recovery repair; existing budgets survive.
    No execution approval is issued by this continuation.
    """
    if not action.get("recovery_generated"):
        return {"changed": False}
    from agent_core.recovery import RecoveryExecutionEnvelope
    envelope = RecoveryExecutionEnvelope.from_mapping(action.get("recovery_execution_envelope") or {})
    task = deepcopy(dict(state.get("task_state") or {}))
    recovery = dict(task.get("recovery_state") or {})
    active = dict(recovery.get("active_recovery") or {})
    claim = dict(recovery.get("generation_claim") or {})
    pending = dict(task.get("pending_action") or {})
    target = str(claim.get("target_generation_id") or "")
    completion = dict(claim.get("provider_completion_cache_identity") or {})
    approval = deepcopy(dict(action.get("action_approval_identity") or {}))
    if decision not in {"rejected", "guidance", "edited_rejected"} or not str(guidance).strip():
        raise ValueError("RECOVERY_APPROVAL_FEEDBACK_INVALID")
    if (not target or claim.get("status") != "action_adopted"
            or active.get("target_generation_id") != target
            or active.get("target_route") != "generate"
            or active.get("status") not in {"pending", "dispatched"}
            or active.get("recovery_id") != envelope.recovery_decision_id
            or claim.get("recovery_id") != envelope.recovery_decision_id
            or active.get("envelope_id") != envelope.immutable_hash
            or recovery.get("active_recovery_envelope") != envelope.to_dict()):
        raise ValueError("RECOVERY_APPROVAL_CLAIM_SCOPE_MISMATCH")
    if (any(str(action.get(key) or "") != str(state.get(current_key) or "")
            for key, current_key in (("plan_id", "plan_id"), ("plan_version", "plan_version"), ("step_id", "current_step_id")))
            or active.get("step_id") != state.get("current_step_id")
            or int(active.get("plan_revision") or 0) != int(state.get("plan_revision") or 0)
            or envelope.source_scope.get("task_id") != state.get("task_id")):
        raise ValueError("RECOVERY_APPROVAL_SCOPE_STALE")
    canonical_task = canonical_task_spec_for_state(state)
    conversation = state.get("conversation_evidence") or {}
    user_turn = max(0, int(conversation.get("user_turn_count")
        if conversation.get("user_turn_count") is not None else conversation.get("turn_count") or 0))
    _, step = current_plan_step_with_index(state)
    authority = {"task_revision": task_spec_revision(canonical_task),
        "task_semantic_hash": task_semantic_hash(canonical_task),
        "plan_revision": int(state.get("plan_revision") or 0),
        "authoritative_user_turn_revision": user_turn,
        "execution_epoch": str(step.get("execution_epoch") or (state.get("runtime_state") or {}).get("execution_epoch") or "0")}
    if any(str(completion.get(key)) != str(value) for key, value in authority.items()):
        raise ValueError("RECOVERY_APPROVAL_AUTHORITY_STALE")
    if any(str(action.get(key)) != str(value) for key, value in authority.items() if key != "task_semantic_hash"):
        raise ValueError("RECOVERY_APPROVAL_AUTHORITY_STALE")
    if (state.get("approval_record") or {}).get("approval_scope_hash") and validate_action_approval_lineage(
            state, dict(action), task_spec_revision(canonical_task)):
        raise ValueError("RECOVERY_APPROVAL_LINEAGE_STALE")
    candidate_event = "generate-candidate:" + str(completion.get("provider_request_digest") or "")
    if (action.get("action_id") != envelope.action_id or pending.get("action_id") != envelope.action_id
            or action.get("candidate_generation_event_id") != candidate_event
            or pending.get("candidate_generation_event_id") != candidate_event
            or completion.get("target_generation_id") != target
            or not approval.get("approval_identity_hash")
            or pending.get("action_approval_identity") != approval):
        raise ValueError("RECOVERY_APPROVAL_CANDIDATE_MISMATCH")
    for attempt in ((state.get("provenance_state") or {}).get("attempts") or {}).values():
        if attempt.get("action_id") == envelope.action_id and (
                attempt.get("pid") or attempt.get("started_at") or str(attempt.get("status") or "").upper() in {"RUNNING", "SUCCEEDED"}):
            raise ValueError("RECOVERY_APPROVAL_EXECUTION_ALREADY_STARTED")
    receipt = {"schema_version": 1, "source": "execute.code_review",
        "scope": {"task_id": state["task_id"], "plan_id": state["plan_id"],
            "plan_version": state["plan_version"], "step_id": state["current_step_id"]},
        "action_id": envelope.action_id, "candidate_event_id": candidate_event,
        "approval_identity": approval, "decision": decision, "guidance": str(guidance),
        "command_started": False, "parent_target_generation_id": target}
    event_id = "approval-feedback-" + hashlib.sha256(json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    next_target = "generation-" + hashlib.sha256((event_id + envelope.immutable_hash).encode()).hexdigest()[:20]
    update_recovery_generation_claim(state, target_generation_id=target,
        status="superseded", detail="Explicit code-review feedback; candidate remains unapproved.")
    task = dict(state["task_state"])
    recovery = dict(task["recovery_state"])
    old = deepcopy(recovery["generation_claim"])
    old.update(approval_feedback_event_id=event_id, approval_feedback=receipt)
    active["target_generation_id"] = next_target
    recovery.update(active_recovery=active,
        generation_claim_history=[*recovery.get("generation_claim_history", []), old],
        generation_claim={"schema_version": 1, "status": "issued", "target_generation_id": next_target,
            "parent_target_generation_id": target, "recovery_id": envelope.recovery_decision_id,
            "provider_idempotency_key": next_target,
            "remote_delivery_semantics": claim.get("remote_delivery_semantics", "at_least_once_without_provider_idempotency"),
            "approval_feedback_event_id": event_id, "approval_feedback": receipt, "issued_at": _now()})
    task["recovery_state"] = recovery
    state["task_state"] = task
    return {"changed": True, "event_id": event_id, "target_generation_id": next_target}


def rollover_recovery_generation_claim_after_skill_result(
    state: PlanState, *, completion: Mapping[str, Any], continuation_id: str,
) -> Dict[str, Any]:
    """Adopt a real Skill result and authorize its knowledge continuation in one CAS.

    This is transport continuation, not a rejected candidate or a new Recovery
    decision. The original envelope, incident and all business budgets survive.
    """
    event = deepcopy(dict(completion))
    required = ("target_generation_id", "provider_request_digest", "response_digest")
    if any(not str(event.get(key) or "") for key in required):
        raise ValueError("RECOVERY_SKILL_COMPLETION_IDENTITY_MISSING")
    identity = {key: str(event[key]) for key in required}
    event_id = "skill-completion-" + hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    task = dict(state.get("task_state") or {})
    recovery = dict(task.get("recovery_state") or {})
    history = deepcopy(list(recovery.get("generation_claim_history") or []))
    if any(item.get("skill_completion_event_id") == event_id for item in history):
        return {"changed": False, "duplicate": True, "event_id": event_id}
    active = dict(recovery.get("active_recovery") or {})
    claim = dict(recovery.get("generation_claim") or {})
    envelope = dict(recovery.get("active_recovery_envelope") or {})
    target = identity["target_generation_id"]
    held_identity = dict(claim.get("provider_completion_cache_identity") or {})
    if (held_identity.get("target_generation_id") != target
            or held_identity.get("provider_request_digest") != identity["provider_request_digest"]
            or claim.get("provider_response_digest") != identity["response_digest"]):
        raise ValueError("RECOVERY_SKILL_COMPLETION_RECEIPT_MISMATCH")
    if (active.get("target_generation_id") != target
            or claim.get("target_generation_id") != target
            or claim.get("status") != "provider_completed"
            or not active.get("recovery_id")
            or claim.get("recovery_id") != active.get("recovery_id")
            or active.get("target_route") != "generate"
            or active.get("status") not in {"pending", "dispatched"}):
        raise ValueError("RECOVERY_SKILL_COMPLETION_CLAIM_MISMATCH")
    if (active.get("step_id") != state.get("current_step_id")
            or int(active.get("plan_revision") or 0) != int(state.get("plan_revision") or 0)
            or envelope.get("step_id") != state.get("current_step_id")
            or not active.get("envelope_id")
            or active["envelope_id"] != envelope.get("immutable_hash")
            or active["recovery_id"] != (envelope.get("recovery_id") or envelope.get("recovery_decision_id"))):
        raise ValueError("RECOVERY_SKILL_COMPLETION_SCOPE_STALE")
    access = dict(task.get("skill_access_state") or {})
    continuation = dict((access.get("continuations") or {}).get(continuation_id) or {})
    result = dict((access.get("skill_tool_results") or {}).get(continuation_id) or {})
    if (not continuation_id or not result
            or result.get("continuation_id") != continuation_id
            or continuation.get("status") != "served"
            or continuation.get("caller_node") != "corrective_generate"
            or continuation.get("current_step_id") != state.get("current_step_id")
            or int(continuation.get("plan_revision") or 0) != int(state.get("plan_revision") or 0)
            or result.get("status") not in {"COMPLETE", "REQUIRES_NARROWING"}):
        raise ValueError("RECOVERY_SKILL_RESULT_NOT_SERVABLE")
    result_digest = hashlib.sha256(json.dumps(result, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    update_recovery_generation_claim(state, target_generation_id=target,
        status="superseded", detail="A durable scoped Skill result authorizes knowledge continuation.")
    task = dict(state["task_state"])
    recovery = dict(task["recovery_state"])
    old = deepcopy(recovery["generation_claim"])
    old.update(skill_completion_event_id=event_id, skill_completion={**identity,
        "continuation_id": continuation_id, "result_digest": result_digest})
    history.append(old)
    next_target = "generation-" + hashlib.sha256(json.dumps({
        **identity, "envelope_id": active["envelope_id"],
        "recovery_id": active["recovery_id"], "continuation_id": continuation_id,
        "result_digest": result_digest,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    active["target_generation_id"] = next_target
    recovery.update(active_recovery=active, generation_claim_history=history,
        generation_claim={"schema_version": 1, "status": "issued",
            "target_generation_id": next_target, "parent_target_generation_id": target,
            "recovery_id": active["recovery_id"], "provider_idempotency_key": next_target,
            "remote_delivery_semantics": claim.get("remote_delivery_semantics", "at_least_once_without_provider_idempotency"),
            "skill_completion_event_id": event_id, "issued_at": _now()})
    task["recovery_state"] = recovery
    state["task_state"] = task
    return {"changed": True, "duplicate": False, "event_id": event_id,
        "target_generation_id": next_target}


def mark_step_verification_pending(
    state: PlanState,
    *,
    step_id: str = "",
    attempt_id: str = "",
    evidence: str = "execute_success",
) -> bool:
    """Record Execute success without granting step-completion authority."""
    if task_status(state) in TASK_TERMINAL_STATUSES:
        return False
    target = next(
        (
            item for item in state.get("steps", []) or []
            if isinstance(item, dict)
            and step_id
            and str(item.get("step_id") or "") == str(step_id)
        ),
        None,
    )
    if target is None:
        target = _first_step_by_status(state, tuple(STEP_ACTIVE_STATUSES))
    if target is None or str(target.get("status") or "") in STEP_SUCCESS_STATUSES | STEP_TERMINAL_STATUSES:
        return False
    changed = str(target.get("status") or "") != STATUS_VERIFICATION_PENDING
    target["status"] = STATUS_VERIFICATION_PENDING
    target["verification_attempt_id"] = str(attempt_id or "")
    _append_evidence(target, "verification_pending", evidence)
    state["current_step_id"] = str(target.get("step_id") or "")
    task_state = dict(state.get("task_state") or {})
    task_state["verification_pending"] = {
        "step_id": str(target.get("step_id") or ""),
        "attempt_id": str(attempt_id or ""),
        "recorded_at": _now(),
    }
    task_state["resume_route_hint"] = "supervisor"
    task_state["status"] = TASK_STATUS_RUNNING
    state["task_state"] = task_state
    state["updated_at"] = _now()
    return changed


def recovery_transition_eligibility(
    state: PlanState,
    *,
    source_node: str,
    current_step_id: str = "",
    current_attempt_id: str = "",
    current_execution_succeeded: bool = False,
    newly_committed_decision_id: str = "",
) -> Dict[str, Any]:
    """Select the sole pending transition and invalidate stale legacy state."""
    task_state = dict(state.get("task_state") or {})
    recovery_state = dict(task_state.get("recovery_state") or {})
    active = dict(recovery_state.get("active_transition") or {})
    migrated_legacy = False
    # Schema-v4 has exactly one route-authority slot.  A missing active
    # transition in that schema is intentional (consumed/retired), so never
    # promote an audit projection back into authority.  Only pre-lifecycle
    # checkpoints may be promoted here.
    try:
        recovery_schema = int(recovery_state.get("schema_version") or 0)
    except (TypeError, ValueError):
        recovery_schema = 0
    if not active and recovery_schema < 4:
        legacy = dict(
            recovery_state.get("last_recovery_decision")
            or recovery_state.get("decision")
            or task_state.get("last_recovery_decision")
            or {}
        )
        legacy_status = str(legacy.get("status") or "pending").strip().lower()
        if legacy and legacy_status == "pending":
            active = deepcopy(legacy)
            legacy_decision_id = str(
                legacy.get("decision_id") or legacy.get("idempotency_key") or ""
            )
            if not legacy_decision_id:
                legacy_decision_id = "legacy-recovery-" + hashlib.sha256(
                    json.dumps(
                        {
                            "kind": legacy.get("kind") or legacy.get("recommended_action"),
                            "step_id": legacy.get("step_id"),
                            "attempt_id": legacy.get("attempt_id"),
                            "source_node": legacy.get("source_node"),
                            "reason": legacy.get("reason"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()[:20]
            active["decision_id"] = legacy_decision_id
            active["idempotency_key"] = legacy_decision_id
            active.setdefault("trigger_node", str(legacy.get("source_node") or ""))
            active.setdefault("trigger_event_id", str(legacy.get("attempt_id") or ""))
            active.setdefault("target_route", str(legacy.get("next_route") or ""))
            active.setdefault("step_attempt", 0)
            active["status"] = "pending"
            migrated_legacy = True
            accepted_legacy_execute = bool(
                str(active.get("kind") or "").upper() == "CONTINUE"
                and str(active.get("target_route") or active.get("next_route") or "").lower() == "generate"
                and str(active.get("trigger_node") or active.get("source_node") or "").lower() == "execute"
                and str(active.get("recommended_action") or "").upper()
                in {"RECONCILE_AND_CONTINUE", "PASS_WITH_WARNING"}
                and (
                    bool(active.get("primary_execution_succeeded"))
                    or active.get("error_is_real") is False
                )
            )
            if accepted_legacy_execute:
                active.update({
                    "kind": "ACCEPT_EXECUTION_FOR_VERIFICATION",
                    "disposition": "ACCEPT_EXECUTION_FOR_VERIFICATION",
                    "target_route": "supervisor",
                    "next_route": "supervisor",
                    "migration_source": "legacy_continue_execute_acceptance",
                })
            elif str(active.get("kind") or "").upper() == "CONTINUE":
                active.update({
                    "kind": "ESCALATE",
                    "disposition": "INTERNAL_PROTOCOL_ERROR",
                    "target_route": "supervisor",
                    "next_route": "supervisor",
                    "reason_code": "AMBIGUOUS_LEGACY_CONTINUE",
                    "migration_source": "legacy_continue_protocol_error",
                })
            recovery_state["schema_version"] = 4
            recovery_state["active_transition"] = active
            recovery_state["last_recovery_decision"] = deepcopy(active)

    trigger_node_scope = str(
        active.get("trigger_node") or active.get("source_node") or ""
    ).strip().lower()
    trigger_event_scope = str(
        active.get("trigger_event_id") or active.get("attempt_id") or ""
    )
    if trigger_node_scope == "execute" and _int(active.get("step_attempt"), 0) <= 0:
        active["step_attempt"] = _primary_step_attempt_ordinal(
            state,
            str(active.get("step_id") or current_step_id or state.get("current_step_id") or ""),
            trigger_event_scope,
        )
        if migrated_legacy:
            recovery_state["active_transition"] = active
            recovery_state["last_recovery_decision"] = deepcopy(active)
    result = {
        "eligible": False,
        "decision_id": str(active.get("decision_id") or active.get("idempotency_key") or ""),
        "target_route": "",
        "status": str(active.get("status") or ""),
        "reason": "no_pending_recovery_transition",
        "migrated_legacy": migrated_legacy,
    }
    if not active or str(active.get("status") or "").strip().lower() != "pending":
        last_ineligible = dict(recovery_state.get("last_ineligible_transition") or {})
        ineligible_scope_matches = bool(
            not str(last_ineligible.get("trigger_node") or "")
            or str(last_ineligible.get("trigger_node") or "") == str(source_node or "")
        ) and bool(
            not str(last_ineligible.get("step_id") or "")
            or str(last_ineligible.get("step_id") or "") == str(current_step_id or "")
        )
        if (
            not active
            and ineligible_scope_matches
            and str(last_ineligible.get("status") or "") in {"superseded", "expired"}
        ):
            result.update({
                "decision_id": str(last_ineligible.get("decision_id") or ""),
                "status": str(last_ineligible.get("status") or ""),
                "reason": str(last_ineligible.get("reason") or ""),
            })
        task_state["recovery_state"] = recovery_state
        state["task_state"] = task_state
        return result

    decision_id = result["decision_id"]
    if newly_committed_decision_id and decision_id == str(newly_committed_decision_id):
        active["trigger_node"] = str(source_node or "")
        recovery_state["active_transition"] = active

    def invalidate(status: str, reason: str) -> Dict[str, Any]:
        active.update({
            "status": status,
            "superseded_reason": reason,
            f"{status}_at": _now(),
        })
        recovery_state["transition_history"] = [
            *list(recovery_state.get("transition_history") or []),
            deepcopy(active),
        ]
        recovery_state["active_transition"] = {}
        recovery_state["last_ineligible_transition"] = {
            "decision_id": decision_id,
            "status": status,
            "reason": reason,
            "step_id": str(active.get("step_id") or ""),
            "trigger_node": str(active.get("trigger_node") or active.get("source_node") or ""),
            "trigger_event_id": str(active.get("trigger_event_id") or active.get("attempt_id") or ""),
            "encountered_at": _now(),
        }
        last = dict(recovery_state.get("last_recovery_decision") or {})
        if str(last.get("decision_id") or last.get("idempotency_key") or "") == decision_id:
            last.update({
                "status": status,
                "superseded_reason": reason,
                f"{status}_at": active[f"{status}_at"],
            })
            recovery_state["last_recovery_decision"] = last
            task_state["last_recovery_decision"] = deepcopy(last)
        task_state["recovery_state"] = recovery_state
        state["task_state"] = task_state
        return {
            **result,
            "status": status,
            "reason": reason,
        }

    def ignore_current_event(reason: str) -> Dict[str, Any]:
        """Reject this event without destroying a newer pending transition."""
        recovery_state["last_ignored_event"] = {
            "decision_id": decision_id,
            "status": "pending",
            "reason": reason,
            "step_id": str(active.get("step_id") or ""),
            "trigger_node": str(active.get("trigger_node") or active.get("source_node") or ""),
            "trigger_event_id": str(active.get("trigger_event_id") or active.get("attempt_id") or ""),
            "observed_event_id": str(current_attempt_id or ""),
            "encountered_at": _now(),
        }
        task_state["recovery_state"] = recovery_state
        state["task_state"] = task_state
        return {
            **result,
            "status": "pending",
            "reason": reason,
            "ignored_current_event": True,
        }

    missing_scope_fields = [
        field for field, present in (
            ("decision_id", bool(decision_id)),
            ("step_id", bool(str(active.get("step_id") or ""))),
            (
                "step_attempt",
                "step_attempt" in active
                and (
                    str(active.get("trigger_node") or active.get("source_node") or "").strip().lower()
                    != "execute"
                    or _int(active.get("step_attempt"), 0) > 0
                ),
            ),
            ("trigger_node", bool(str(active.get("trigger_node") or active.get("source_node") or ""))),
            ("trigger_event_id", bool(str(active.get("trigger_event_id") or active.get("attempt_id") or ""))),
            ("disposition", bool(str(active.get("disposition") or ""))),
            ("target_route", bool(str(active.get("target_route") or active.get("next_route") or ""))),
        )
        if not present
    ]
    if missing_scope_fields:
        return invalidate(
            "expired",
            "RECOVERY_TRANSITION_SCOPE_MISSING:" + ",".join(missing_scope_fields),
        )

    expected_step_id = str(active.get("step_id") or "")
    if (
        expected_step_id
        and not str(current_step_id or "")
        and decision_id != str(newly_committed_decision_id or "")
    ):
        return invalidate("expired", "CURRENT_STEP_MISSING")
    if (
        expected_step_id
        and current_step_id
        and expected_step_id != str(current_step_id)
        and decision_id != str(newly_committed_decision_id or "")
    ):
        return invalidate("superseded", "SUPERSEDED_BY_CURRENT_STEP")

    trigger_node = str(active.get("trigger_node") or active.get("source_node") or "")
    if trigger_node and trigger_node != str(source_node or ""):
        return invalidate("expired", "TRIGGER_NODE_MISMATCH")

    trigger_event_id = str(active.get("trigger_event_id") or active.get("attempt_id") or "")
    if trigger_event_id and not str(current_attempt_id or ""):
        return invalidate("expired", "CURRENT_EVENT_MISSING")
    provenance = ensure_provenance_state(state)
    current_attempt = dict((provenance.get("attempts") or {}).get(str(current_attempt_id or "")) or {})
    completion_scope_after_advance = bool(
        decision_id == str(newly_committed_decision_id or "")
        and str(active.get("kind") or "").upper()
        in {"COMPLETE_STEP", "COMPLETE_TASK"}
    )
    ordinal_step_id = (
        expected_step_id
        if completion_scope_after_advance and expected_step_id
        else str(current_step_id or expected_step_id)
    )
    matching_attempts = _ordered_primary_step_attempts(
        state,
        ordinal_step_id,
    )
    derived_current_ordinal = next(
        (
            index for index, item in enumerate(matching_attempts, start=1)
            if str(item.get("attempt_id") or "") == str(current_attempt_id or "")
        ),
        0,
    )
    trigger_ordinal = next(
        (
            index for index, item in enumerate(matching_attempts, start=1)
            if str(item.get("attempt_id") or item.get("execution_id") or "")
            == trigger_event_id
        ),
        0,
    )
    if trigger_event_id != str(current_attempt_id or ""):
        if trigger_ordinal and derived_current_ordinal:
            if derived_current_ordinal > trigger_ordinal:
                return invalidate(
                    "superseded",
                    "SUPERSEDED_BY_NEWER_EXECUTION"
                    if str(source_node or "") == "execute"
                    else "SUPERSEDED_BY_NEWER_EVENT",
                )
            return ignore_current_event("CURRENT_EVENT_PRECEDES_RECOVERY_TRANSITION")
        return ignore_current_event("CURRENT_EVENT_ORDER_UNPROVEN")
    current_step_attempt = _int(
        derived_current_ordinal
        or current_attempt.get("attempt_ordinal")
        or current_attempt.get("sequence")
        or 0,
        0,
    )
    expected_step_attempt = _int(active.get("step_attempt"), 0)
    if expected_step_attempt and not current_step_attempt:
        return invalidate("expired", "CURRENT_STEP_ATTEMPT_MISSING")
    if (
        expected_step_attempt
        and current_step_attempt
        and expected_step_attempt != current_step_attempt
    ):
        return invalidate("expired", "STEP_ATTEMPT_SCOPE_MISMATCH")

    persisted_target_route = str(
        active.get("target_route") or active.get("next_route") or ""
    ).strip().lower()
    try:
        target_route = recovery_route_for_disposition(
            str(active.get("disposition") or ""),
            decision_kind=str(active.get("kind") or ""),
            requires_user_input=bool(active.get("requires_user_input")),
        )
    except ValueError:
        active.update({
            "kind": "ESCALATE",
            "disposition": "INTERNAL_PROTOCOL_ERROR",
            "target_route": "supervisor",
            "next_route": "supervisor",
            "reason_code": "RECOVERY_DISPOSITION_INVALID",
        })
        target_route = "supervisor"
        recovery_state["active_transition"] = active
        result["protocol_error"] = "RECOVERY_DISPOSITION_INVALID"
    else:
        if persisted_target_route != target_route:
            original = deepcopy(active)
            active.update({
                "target_route": target_route,
                "next_route": target_route,
                "route_normalization": "derived_from_typed_disposition",
            })
            active.setdefault("route_conflict_payload", original)
            recovery_state["active_transition"] = active
            last = dict(recovery_state.get("last_recovery_decision") or {})
            if str(last.get("decision_id") or last.get("idempotency_key") or "") == decision_id:
                last.update({
                    "target_route": target_route,
                    "next_route": target_route,
                    "route_normalization": "derived_from_typed_disposition",
                })
                recovery_state["last_recovery_decision"] = last
                task_state["last_recovery_decision"] = deepcopy(last)
            recovery_state["decision_audit"] = [
                *list(recovery_state.get("decision_audit") or []),
                {
                    "decision_id": decision_id,
                    "apply_status": "protocol_normalized",
                    "reason": "RECOVERY_ROUTE_DERIVED_FROM_TYPED_DISPOSITION",
                    "persisted_target_route": persisted_target_route,
                    "effective_target_route": target_route,
                    "recorded_at": _now(),
                },
            ]
            result["protocol_normalized"] = True

    task_state["recovery_state"] = recovery_state
    state["task_state"] = task_state
    if (
        str(active.get("kind") or "").upper()
        == "ACCEPT_EXECUTION_FOR_VERIFICATION"
        and target_route == "supervisor"
    ):
        mark_step_verification_pending(
            state,
            step_id=expected_step_id,
            attempt_id=trigger_event_id,
            evidence="accepted Execute awaiting Supervisor validation",
        )
        task_state = dict(state.get("task_state") or {})
        recovery_state = dict(task_state.get("recovery_state") or recovery_state)
    return {
        **result,
        "eligible": True,
        "target_route": target_route,
        "status": "pending",
        "reason": "eligible",
        "transition": deepcopy(active),
    }


def consume_recovery_transition(
    state: PlanState,
    *,
    decision_id: str,
    route_record: Mapping[str, Any],
) -> bool:
    """Consume one active transition while retaining immutable audit history."""
    task_state = dict(state.get("task_state") or {})
    recovery_state = dict(task_state.get("recovery_state") or {})
    active = dict(recovery_state.get("active_transition") or {})
    active_id = str(active.get("decision_id") or active.get("idempotency_key") or "")
    if (
        not active
        or active_id != str(decision_id or "")
        or str(active.get("status") or "").strip().lower() != "pending"
    ):
        return False
    consumed = deepcopy(active)
    consumed.update({
        "status": "consumed",
        "consumed_at": _now(),
        "dispatched_route": str(route_record.get("effective_route") or ""),
        "route_event_id": str(route_record.get("route_event_id") or ""),
    })
    recovery_state["transition_history"] = [
        *list(recovery_state.get("transition_history") or []),
        consumed,
    ]
    recovery_state["active_transition"] = {}
    recovery_state["phase"] = "consumed"
    recovery_state["last_consumed_transition"] = consumed
    last = dict(recovery_state.get("last_recovery_decision") or {})
    if str(last.get("decision_id") or last.get("idempotency_key") or "") == active_id:
        last.update({
            "status": "consumed",
            "consumed_at": consumed["consumed_at"],
        })
        recovery_state["last_recovery_decision"] = last
        task_state["last_recovery_decision"] = deepcopy(last)
    recovery_state["decision_audit"] = [
        *list(recovery_state.get("decision_audit") or []),
        {
            "decision_id": active_id,
            "apply_status": "consumed",
            "recorded_at": consumed["consumed_at"],
            "route_event_id": consumed["route_event_id"],
            "effective_route": consumed["dispatched_route"],
        },
    ]
    active_recovery = dict(recovery_state.get("active_recovery") or {})
    if str(active_recovery.get("recovery_id") or "") == active_id:
        active_recovery.update({
            "status": "dispatched",
            "dispatched_at": consumed["consumed_at"],
            "route_event_id": consumed["route_event_id"],
        })
        recovery_state["active_recovery"] = active_recovery
    task_state["recovery_state"] = recovery_state
    state["task_state"] = task_state
    state["updated_at"] = _now()
    return True


def step_artifact_contracts(
    step: PlanStep,
    *,
    allow_legacy_text_fallback: bool = False,
) -> List[Dict[str, Any]]:
    """Project structured artifacts; enable text extraction only in a legacy adapter."""
    step_id = str(step.get("step_id") or "")
    explicit_artifacts = list(step.get("artifacts") or [])
    values: List[Tuple[Any, str]] = [
        (value, "structured_plan") for value in explicit_artifacts
    ]
    if not values:
        values.extend(
            (value, "structured_plan") for value in step.get("outputs") or []
        )
    if allow_legacy_text_fallback and not explicit_artifacts:
        for criterion in step.get("success_criteria") or []:
            values.extend(
                (match.group(1), "planner_text")
                for match in _ARTIFACT_PATH_RE.finditer(str(criterion or ""))
            )
    result: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for value, source in values:
        contract = normalize_artifact_contract(value, producer_step_id=step_id, source=source)
        key = (str(contract.get("artifact_id") or ""), str(contract.get("path") or ""))
        if not contract.get("path") or key in seen:
            continue
        seen.add(key)
        result.append(contract)
    return result


def record_preaction_contract_failure(
    state: PlanState,
    violation: Mapping[str, Any],
    *,
    error_fingerprint: str,
    candidate_fingerprint: str,
    repair_attempt_count: int = 0,
    termination_reason: str = "",
) -> Dict[str, Any]:
    """Persist immutable primary and bounded latest/terminal pre-action failures."""
    now = _now()
    prior = dict(state.get("failure") or {}) if isinstance(state.get("failure"), Mapping) else {}
    latest_code = str(violation.get("code") or "PRE_ACTION_CONTRACT_FAILURE")
    latest_owner = str(violation.get("owner") or "GENERATED_ACTION").upper()
    latest_message = str(
        violation.get("message") or "Generated action failed its pre-action contract."
    )
    latest_failure = {
        "error_code": latest_code,
        "owner": latest_owner,
        "subsystem": str(violation.get("subowner") or "managed_artifacts"),
        "stage": "pre_action_contract",
        "message": latest_message,
        "fingerprint": str(error_fingerprint or ""),
        "candidate_fingerprint": str(candidate_fingerprint or ""),
        "execution_started": False,
        "attempt_id": "",
        "scope": {"task_id": str(state.get("task_id") or ""), "plan_id": str(state.get("plan_id") or ""),
                  "plan_version": str(state.get("plan_version") or ""), "step_id": str(state.get("current_step_id") or "")},
        "repair_attempt_count": int(repair_attempt_count or 0),
        "recorded_at": now,
    }
    for key in (
        "field_path",
        "artifact_handle",
        "expected",
        "proposed",
        "constraint_origin",
    ):
        if violation.get(key) not in (None, "", []):
            latest_failure[key] = deepcopy(violation.get(key))
    if not str(prior.get("error_code") or ""):
        prior = {
            "error_code": latest_code,
            "owner": latest_owner,
            "subsystem": str(violation.get("subowner") or "managed_artifacts"),
            "stage": "pre_action_contract",
            "message": latest_message,
            "reason": latest_message,
            "current_step": str(state.get("current_step_id") or ""),
            "fingerprint": str(error_fingerprint or ""),
            "candidate_fingerprint": str(candidate_fingerprint or ""),
            "execution_started": False,
            "repair_attempted": bool(repair_attempt_count),
            "repair_exhausted": False,
            # Compatibility aliases remain readable by existing terminal and
            # resume projections, but they do not own the primary diagnosis.
            "failure_type": "generated_action_contract_error",
            "failure_stage": "pre_action_contract",
            "error_owner": latest_owner,
            "detail": latest_message,
            "summary": latest_message,
            "recorded_at": now,
        }
        for target, source in (
            ("logical_artifact", "artifact_handle"),
            ("expected_realization", "expected"),
            ("observed_candidates", "proposed"),
            ("constraint_origin", "constraint_origin"),
        ):
            if violation.get(source) not in (None, "", []):
                prior[target] = deepcopy(violation.get(source))
    prior["latest_failure"] = latest_failure
    prior["last_seen_at"] = now
    prior["repair_attempt_count"] = max(
        int(prior.get("repair_attempt_count") or 0),
        int(repair_attempt_count or 0),
    )
    prior["repair_attempted"] = bool(prior["repair_attempt_count"])
    prior["last_candidate_fingerprint"] = str(candidate_fingerprint or "")
    prior["last_error_fingerprint"] = str(error_fingerprint or "")
    if termination_reason:
        prior["termination_reason"] = str(termination_reason)
        # The original failure keeps its execution identity and execution flag.
        # Only latest/terminal_failure describes this unexecuted candidate.
        prior["repair_exhausted"] = True
        prior["terminal_failure"] = {
            **deepcopy(latest_failure),
            "termination_reason": str(termination_reason),
            "repair_attempt_count": int(prior["repair_attempt_count"]),
        }
        task_state = dict(state.get("task_state") or {})
        task_state["status"] = TASK_STATUS_FAILED
        task_state["status_reason"] = str(
            prior.get("message") or "Generated action failed its pre-action contract."
        )
        state["task_state"] = task_state
        for step in state.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "").lower() in {
                STATUS_RUNNING,
                STATUS_READY,
                STATUS_PENDING,
                STATUS_DIAGNOSING,
            }:
                step["status"] = STATUS_FAILED
                step["updated_at"] = now
                break
    state["failure"] = prior
    state["updated_at"] = now
    return deepcopy(prior)


def record_runtime_output_failure(
    state: PlanState,
    reconciliation: Mapping[str, Any],
    *,
    step_id: str,
    attempt_id: str,
    command_role: str = "",
    repair_exhausted: bool = False,
) -> Dict[str, Any]:
    """Persist typed runtime output failure without flattening it to TASK_BLOCKED."""
    violations = [
        dict(item) for item in reconciliation.get("violations", []) or []
        if isinstance(item, Mapping)
    ]
    if not violations:
        prior = dict(state.get("failure") or {}) if isinstance(state.get("failure"), Mapping) else {}
        if (
            prior.get("subsystem") == "runtime_output_reconciliation"
            and prior.get("execution_started") is True
            and not prior.get("termination_reason")
        ):
            prior["resolved_at"] = _now()
            state["failure"] = prior
        return {}
    first = violations[0]
    now = _now()
    prior = dict(state.get("failure") or {}) if isinstance(state.get("failure"), Mapping) else {}
    if not prior or prior.get("resolved_at"):
        prior = {
            "error_code": str(first.get("code") or "OUTPUT_RECONCILIATION_FAILED"),
            "owner": str(first.get("owner") or "ARTIFACT_CONTRACT").upper(),
            "subsystem": "runtime_output_reconciliation",
            "stage": "post_execution_reconciliation",
            "message": str(first.get("message") or "Runtime output reconciliation failed."),
            "reason": str(first.get("message") or "Runtime output reconciliation failed."),
            "current_step": str(step_id or ""),
            "execution_started": True,
            "repair_attempted": str(command_role or "").lower() == "recovery",
            "repair_exhausted": bool(repair_exhausted),
            "logical_artifact": str(first.get("logical_artifact") or ""),
            "expected_realization": str(first.get("expected_realization") or ""),
            "observed_candidates": deepcopy(list(first.get("observed_candidates") or [])),
            "constraint_origin": str(first.get("constraint_origin") or ""),
            "attempt_id": str(attempt_id or ""),
            "fingerprint": str(reconciliation.get("fingerprint") or ""),
            "recorded_at": now,
        }
    prior["last_seen_at"] = now
    prior["last_attempt_id"] = str(attempt_id or "")
    prior["last_fingerprint"] = str(reconciliation.get("fingerprint") or "")
    prior["repair_attempted"] = bool(
        prior.get("repair_attempted") or str(command_role or "").lower() == "recovery"
    )
    if repair_exhausted:
        prior["repair_exhausted"] = True
        prior["termination_reason"] = "runtime_output_recovery_exhausted"
    state["failure"] = prior
    state["updated_at"] = now
    return deepcopy(prior)


def manifest_artifact_contracts(manifest: Dict[str, Any], manifest_path: str | Path = "") -> List[Dict[str, Any]]:
    """Read the existing renderer manifest format, including legacy `scripts`."""
    if not isinstance(manifest, dict):
        return []
    base_dir = str(manifest.get("outdir") or (Path(manifest_path).parent if manifest_path else ""))
    values: List[Any] = list(manifest.get("artifacts") or [])
    if not values:
        values.extend(
            {
                "path": str(path),
                "artifact_type": "script",
                "validator": "nonempty_executable_script",
                "producer": str(manifest.get("producer") or "renderer_manifest"),
            }
            for path in manifest.get("scripts") or []
        )
    contracts: List[Dict[str, Any]] = []
    for value in values:
        contract = normalize_artifact_contract(value, source="renderer_manifest")
        contract["base_dir"] = str(contract.get("base_dir") or base_dir)
        contract["manifest_path"] = str(manifest_path or "")
        contract["manifest_task_id"] = str(manifest.get("task_id") or "")
        contract["manifest_generated_at"] = str(manifest.get("generated_at") or "")
        contract["producer_action_id"] = str(
            contract.get("producer_action_id") or manifest.get("producer_action_id") or ""
        )
        contracts.append(contract)
    return contracts


def _stable_payload_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def normalize_renderer_manifest(
    manifest: Dict[str, Any],
    manifest_path: str | Path = "",
) -> Dict[str, Any]:
    """Normalize v3 and legacy renderer manifests into one action contract."""
    if not isinstance(manifest, dict):
        return {}
    value = deepcopy(manifest)
    try:
        source_schema_version = int(value.get("schema_version") or 1)
    except (TypeError, ValueError):
        source_schema_version = 1
    outdir = str(value.get("outdir") or (Path(manifest_path).parent if manifest_path else ""))
    scripts = [str(item) for item in value.get("scripts", []) or [] if str(item).strip()]
    artifacts = manifest_artifact_contracts(value, manifest_path)
    by_path = {str(item.get("path") or ""): str(item.get("artifact_id") or "") for item in artifacts}
    by_producer: Dict[str, List[str]] = {}
    for artifact in artifacts:
        producer = str(artifact.get("producer") or "")
        if producer:
            by_producer.setdefault(producer, []).append(str(artifact.get("artifact_id") or ""))

    raw_actions = value.get("actions") if isinstance(value.get("actions"), list) else []
    legacy = source_schema_version < RENDERER_MANIFEST_SCHEMA_VERSION or not raw_actions
    if legacy:
        previous = ""
        raw_actions = []
        for index, script in enumerate(scripts, start=1):
            name = Path(script).stem
            action_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or f"action_{index}"
            raw_actions.append({
                "id": action_id,
                "script": script,
                "kind": "verification" if "preflight" in name.lower() else "primary",
                "depends_on": [previous] if previous else [],
                "expected_artifact_ids": by_producer.get(script, []),
                "success_checks": ["script_exit_zero"],
            })
            previous = action_id

    actions: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous = ""
    for index, raw_action in enumerate(raw_actions, start=1):
        if not isinstance(raw_action, dict):
            continue
        action = deepcopy(raw_action)
        action_id = str(action.get("id") or action.get("action_id") or f"action_{index}").strip()
        action_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", action_id) or f"action_{index}"
        if action_id in seen_ids:
            action_id = f"{action_id}_{index}"
        seen_ids.add(action_id)
        script = str(action.get("script") or action.get("script_path") or "").strip()
        kind = str(action.get("kind") or action.get("action_kind") or "primary").strip().lower()
        if kind not in MANIFEST_ACTION_KINDS:
            kind = "primary"
        dependencies = action.get("depends_on") or action.get("dependencies") or []
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        if legacy and not dependencies and previous:
            dependencies = [previous]
        expected = action.get("expected_artifact_ids") or action.get("expected_artifacts") or []
        if isinstance(expected, (str, dict)):
            expected = [expected]
        expected_ids: List[str] = []
        for item in expected:
            if isinstance(item, dict):
                item_id = str(item.get("artifact_id") or by_path.get(str(item.get("path") or "")) or "")
            else:
                item_id = str(by_path.get(str(item), str(item)))
            if item_id and item_id not in expected_ids:
                expected_ids.append(item_id)
        checks = action.get("success_checks") or ["script_exit_zero"]
        if isinstance(checks, str):
            checks = [checks]
        actions.append({
            **action,
            "id": action_id,
            "script": script,
            "kind": kind,
            "depends_on": [str(item) for item in dependencies if str(item).strip()],
            "expected_artifact_ids": expected_ids,
            "success_checks": [str(item) for item in checks if str(item).strip()],
            "required": bool(action.get("required", True)),
            "enabled": bool(action.get("enabled", True)),
        })
        previous = action_id

    renderer = dict(value.get("renderer") or {}) if isinstance(value.get("renderer"), dict) else {}
    renderer.setdefault("id", str(value.get("renderer_id") or value.get("producer") or "legacy_renderer"))
    provenance = dict(value.get("provenance") or {}) if isinstance(value.get("provenance"), dict) else {}
    provenance.setdefault("task_id", str(value.get("task_id") or ""))
    provenance.setdefault("step_id", str(value.get("producer_step_id") or ""))
    provenance.setdefault("producer_action_id", str(value.get("producer_action_id") or ""))
    requirements = [
        normalize_requirement(item, source="renderer_manifest")
        for item in value.get("requirements", []) or []
        if isinstance(item, (dict, str))
    ]
    declared_outputs = [
        deepcopy(item) if isinstance(item, dict) else {"path": str(item)}
        for item in value.get("outputs", []) or []
        if isinstance(item, (dict, str)) and _output_path_or_name(item)
    ]
    manifest_fingerprint = _stable_payload_hash({
        "renderer": renderer,
        "provenance": provenance,
        "actions": actions,
        "artifacts": [
            {key: item.get(key) for key in ("artifact_id", "path", "validator", "required", "producer")}
            for item in artifacts
        ],
        "outputs": declared_outputs,
        "requirements": requirements,
    })
    manifest_id = str(value.get("manifest_id") or _stable_id("manifest", manifest_fingerprint))
    stage_id_by_action = {
        str(action.get("id") or ""): _stable_id("stage", manifest_id, action.get("id"))
        for action in actions
        if str(action.get("id") or "")
    }
    output_ids_by_action = {
        str(action.get("id") or ""): [str(item) for item in action.get("expected_artifact_ids", []) or []]
        for action in actions
    }
    stages: List[Dict[str, Any]] = []
    for action in actions:
        action_id = str(action.get("id") or "")
        dependencies = [str(item) for item in action.get("depends_on", []) or []]
        declared_inputs = [str(item) for item in action.get("input_artifact_ids", []) or []]
        derived_inputs = [
            artifact_id
            for dependency in dependencies
            for artifact_id in output_ids_by_action.get(dependency, [])
            if artifact_id not in declared_inputs
        ]
        stages.append({
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "stage_id": stage_id_by_action.get(action_id, ""),
            "manifest_id": manifest_id,
            "manifest_action_id": action_id,
            "plan_step_id": str(provenance.get("step_id") or ""),
            "depends_on_stage_ids": [stage_id_by_action[item] for item in dependencies if item in stage_id_by_action],
            "input_artifact_ids": [*declared_inputs, *derived_inputs],
            "output_artifact_ids": list(output_ids_by_action.get(action_id, [])),
            "status": "pending",
            "current_attempt_id": "",
            "dependency_source": "manifest_declared" if declared_inputs else "manifest_dependency_derived",
        })
    normalized = {
        **value,
        "schema_version": RENDERER_MANIFEST_SCHEMA_VERSION,
        "source_schema_version": source_schema_version,
        "manifest_type": str(value.get("manifest_type") or "renderer_artifact_contract"),
        "manifest_path": str(manifest_path or ""),
        "outdir": outdir,
        "scripts": scripts or [str(item.get("script") or "") for item in actions if item.get("script")],
        "artifacts": artifacts,
        "actions": actions,
        "outputs": declared_outputs,
        "requirements": requirements,
        "renderer": renderer,
        "provenance": provenance,
        "manifest_id": manifest_id,
        "stages": stages,
        "legacy_adapted": legacy,
    }
    normalized["manifest_fingerprint"] = manifest_fingerprint
    return normalized


def validate_renderer_manifest_outputs(
    state: PlanState,
    step_id: str,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare approved renderer output bindings with manifest declarations."""
    normalized = normalize_renderer_manifest(manifest, manifest.get("manifest_path", ""))
    step = next(
        (
            item for item in state.get("steps", []) or []
            if isinstance(item, dict) and str(item.get("step_id") or "") == str(step_id)
        ),
        {},
    )
    invocation = dict(step.get("renderer_invocation_contract") or {})
    approved = [
        dict(item) for item in step.get("bound_outputs", []) or []
        if isinstance(item, dict)
    ]
    if not approved:
        return {
            "valid": True,
            "violation_code": "",
            "status": "not_applicable_no_approved_bound_outputs",
            "expected_outputs": [],
            "manifest_outputs": [],
            "missing_outputs": [],
            "unexpected_outputs": [],
        }

    task_spec = canonical_current_task_spec(_current_task_spec(state))
    task_outputs = list(task_spec.get("output_contracts") or [])
    approved_dir = _canonical_output_dir(task_outputs, task_spec) or str(invocation.get("outdir") or "")
    manifest_dir = str(normalized.get("outdir") or approved_dir)
    expected_by_identity = {
        canonical_output_identity(
            item.get("canonical_path") or item.get("required_output"),
            approved_dir,
        ): item
        for item in approved
        if canonical_output_identity(
            item.get("canonical_path") or item.get("required_output"),
            approved_dir,
        )
    }
    explicit_outputs = list(normalized.get("outputs") or [])
    if explicit_outputs:
        manifest_values = explicit_outputs
    else:
        # Legacy manifests did not distinguish final outputs from scripts and
        # intermediates.  They may prove expected paths, but extra artifacts
        # are not promoted to approved outputs.
        manifest_values = [
            item for item in normalized.get("artifacts", []) or []
            if canonical_output_identity(item, manifest_dir) in expected_by_identity
        ]
    actual_by_identity = {
        canonical_output_identity(item, manifest_dir): item
        for item in manifest_values
        if canonical_output_identity(item, manifest_dir)
    }
    missing = sorted(set(expected_by_identity) - set(actual_by_identity))
    unexpected = sorted(set(actual_by_identity) - set(expected_by_identity)) if explicit_outputs else []
    expected_renderer = str(invocation.get("renderer_id") or step.get("renderer_id") or "")
    actual_renderer = str((normalized.get("renderer") or {}).get("id") or "")
    expected_step = str(step.get("step_id") or step_id)
    actual_step = str((normalized.get("provenance") or {}).get("step_id") or "")
    producer_mismatch = bool(
        (expected_renderer and actual_renderer != expected_renderer)
        or (expected_step and actual_step != expected_step)
    )
    valid = not missing and not unexpected and not producer_mismatch
    return {
        "valid": valid,
        "violation_code": "" if valid else "MANIFEST_OUTPUT_MISMATCH",
        "status": "matched" if valid else "mismatch",
        "expected_outputs": sorted(expected_by_identity),
        "manifest_outputs": sorted(actual_by_identity),
        "missing_outputs": missing,
        "unexpected_outputs": unexpected,
        "expected_renderer_id": expected_renderer,
        "manifest_renderer_id": actual_renderer,
        "expected_step_id": expected_step,
        "manifest_step_id": actual_step,
        "producer_mismatch": producer_mismatch,
        "reason": (
            "Approved bound outputs match the renderer manifest."
            if valid
            else "MANIFEST_OUTPUT_MISMATCH: approved renderer outputs and manifest declarations differ."
        ),
    }


def validate_renderer_manifest_provenance(
    manifest: Dict[str, Any],
    *,
    task_id: str,
    step_id: str,
    producer_action_id: str = "",
    allow_legacy_recovery: bool = False,
) -> Dict[str, Any]:
    normalized = normalize_renderer_manifest(manifest, manifest.get("manifest_path", ""))
    provenance = dict(normalized.get("provenance") or {})
    expected = {
        "task_id": str(task_id or ""),
        "step_id": str(step_id or ""),
        "producer_action_id": str(producer_action_id or ""),
    }
    mismatches = [
        key for key, wanted in expected.items()
        if wanted and str(provenance.get(key) or "") and str(provenance.get(key)) != wanted
    ]
    missing = [key for key in ("task_id", "step_id") if expected[key] and not str(provenance.get(key) or "")]
    valid = not mismatches and (not missing or (normalized.get("legacy_adapted") and allow_legacy_recovery))
    return {
        "valid": valid,
        "mismatches": mismatches,
        "missing": missing,
        "legacy_recovery_only": bool(missing and normalized.get("legacy_adapted") and allow_legacy_recovery),
        "reason": (
            f"Renderer manifest provenance mismatch: {', '.join(mismatches)}" if mismatches else
            f"Renderer manifest lacks provenance: {', '.join(missing)}" if missing and not valid else
            "Renderer manifest provenance accepted."
        ),
    }


def register_renderer_manifest(
    state: PlanState,
    step_id: str,
    manifest: Dict[str, Any],
    *,
    manifest_path: str | Path,
    task_id: str,
    producer_action_id: str = "",
    allow_legacy_recovery: bool = False,
) -> Dict[str, Any]:
    """Register one authoritative manifest while preserving same-manifest progress."""
    normalized = normalize_renderer_manifest(manifest, manifest_path)
    provenance = validate_renderer_manifest_provenance(
        normalized,
        task_id=task_id,
        step_id=step_id,
        producer_action_id=producer_action_id,
        allow_legacy_recovery=allow_legacy_recovery,
    )
    if not provenance["valid"]:
        raise ValueError(provenance["reason"])
    step = next(
        (item for item in state.get("steps", []) or [] if isinstance(item, dict) and str(item.get("step_id") or "") == str(step_id)),
        None,
    )
    if not step:
        raise ValueError(f"Unknown plan step for renderer manifest: {step_id}")
    output_gate = validate_renderer_manifest_outputs(state, step_id, normalized)
    if not output_gate["valid"]:
        raise ValueError(
            "MANIFEST_OUTPUT_MISMATCH: "
            + json.dumps(output_gate, ensure_ascii=False, sort_keys=True)
        )
    previous = dict(step.get("renderer_execution") or {})
    same_manifest = str(previous.get("manifest_fingerprint") or "") == str(normalized["manifest_fingerprint"])
    completed = list(previous.get("completed_action_ids") or []) if same_manifest else []
    action_evidence = list(previous.get("action_evidence") or []) if same_manifest else []
    step["renderer_execution"] = {
        "schema_version": RENDERER_MANIFEST_SCHEMA_VERSION,
        "renderer_id": str((normalized.get("renderer") or {}).get("id") or ""),
        "manifest_path": str(Path(manifest_path)),
        "manifest_fingerprint": str(normalized["manifest_fingerprint"]),
        "manifest_id": str(normalized.get("manifest_id") or ""),
        "producer_action_id": str((normalized.get("provenance") or {}).get("producer_action_id") or producer_action_id),
        "completed_action_ids": completed,
        "current_action_id": "",
        "action_evidence": action_evidence,
        "registered_at": _now(),
        "legacy_recovery_only": bool(provenance.get("legacy_recovery_only")),
        "output_contract_gate": output_gate,
    }
    step["updated_at"] = _now()
    reconcile_step_artifacts(step, normalized, manifest_path=manifest_path)
    record_provenance_event(state, "manifest_registered", {
        "manifest": normalized,
        "manifest_path": str(manifest_path),
        "step_id": step_id,
        "producer_action_id": producer_action_id,
    })
    state["updated_at"] = _now()
    return normalized


def next_renderer_manifest_action(manifest: Dict[str, Any], renderer_execution: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_renderer_manifest(manifest, manifest.get("manifest_path", ""))
    completed = {str(item) for item in renderer_execution.get("completed_action_ids", []) or []}
    for action in normalized.get("actions", []) or []:
        action_id = str(action.get("id") or "")
        if not action_id or action_id in completed or not action.get("enabled", True):
            continue
        dependencies = {str(item) for item in action.get("depends_on", []) or []}
        if dependencies <= completed:
            return deepcopy(action)
    return {}


def resolve_renderer_action_script(manifest: Dict[str, Any], action: Dict[str, Any]) -> Path:
    """Resolve only a script declared by the current manifest and contained in its outdir."""
    normalized = normalize_renderer_manifest(manifest, manifest.get("manifest_path", ""))
    action_id = str(action.get("id") or "")
    declared = next((item for item in normalized.get("actions", []) if str(item.get("id") or "") == action_id), None)
    if not declared:
        available = [str(item.get("id") or "") for item in normalized.get("actions", [])]
        raise ValueError(f"Manifest action is not declared: {action_id}; available={available}")
    script = str(declared.get("script") or "").strip()
    if not script:
        raise ValueError(f"Manifest action has no script: {action_id}")
    base = Path(str(normalized.get("outdir") or Path(normalized.get("manifest_path") or ".").parent)).resolve()
    candidate = Path(script)
    candidate = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Manifest script escapes renderer outdir: {candidate}") from exc
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise ValueError(f"Manifest script is missing or empty: {candidate}")
    return candidate


def mark_renderer_action_completed(
    state: PlanState,
    step_id: str,
    action_id: str,
    evidence: Dict[str, Any],
) -> bool:
    step = next(
        (item for item in state.get("steps", []) or [] if isinstance(item, dict) and str(item.get("step_id") or "") == str(step_id)),
        None,
    )
    if not step or not isinstance(step.get("renderer_execution"), dict):
        return False
    execution = step["renderer_execution"]
    completed = list(execution.get("completed_action_ids") or [])
    if action_id not in completed:
        completed.append(action_id)
    execution["completed_action_ids"] = completed
    execution["current_action_id"] = ""
    execution.setdefault("action_evidence", []).append({
        "manifest_action_id": str(action_id),
        "recorded_at": _now(),
        **deepcopy(evidence or {}),
    })
    execution["action_evidence"] = execution["action_evidence"][-100:]
    step["updated_at"] = _now()
    state["updated_at"] = _now()
    return True


def renderer_manifest_complete(manifest: Dict[str, Any], renderer_execution: Dict[str, Any]) -> bool:
    normalized = normalize_renderer_manifest(manifest, manifest.get("manifest_path", ""))
    required = {
        str(item.get("id") or "")
        for item in normalized.get("actions", []) or []
        if item.get("required", True) and item.get("enabled", True)
    }
    completed = {str(item) for item in renderer_execution.get("completed_action_ids", []) or []}
    return bool(required) and required <= completed


def validate_renderer_action_artifacts(
    manifest: Dict[str, Any],
    action: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate only artifacts declared for one manifest action."""
    normalized = normalize_renderer_manifest(manifest, manifest.get("manifest_path", ""))
    artifact_by_id = {
        str(item.get("artifact_id") or ""): item
        for item in normalized.get("artifacts", []) or []
        if str(item.get("artifact_id") or "")
    }
    expected_ids = [str(item) for item in action.get("expected_artifact_ids", []) or []]
    missing_contracts = [item for item in expected_ids if item not in artifact_by_id]
    failed: List[Dict[str, str]] = []
    base = Path(str(normalized.get("outdir") or Path(normalized.get("manifest_path") or ".").parent))
    for artifact_id in expected_ids:
        contract = artifact_by_id.get(artifact_id)
        if not contract:
            continue
        path = Path(str(contract.get("path") or ""))
        candidate = path if path.is_absolute() else base / path
        validator = str(contract.get("validator") or "nonempty_file")
        try:
            if validator == "directory_exists":
                valid = candidate.is_dir()
            elif validator == "file_exists":
                valid = candidate.is_file()
            elif validator == "nonempty_directory":
                valid = candidate.is_dir() and any(candidate.iterdir())
            elif validator == "nonempty_executable_script":
                valid = candidate.is_file() and candidate.stat().st_size > 0 and os.access(candidate, os.X_OK)
            else:
                valid = candidate.is_file() and candidate.stat().st_size > 0
        except OSError:
            valid = False
        if not valid:
            failed.append({
                "artifact_id": artifact_id,
                "path": str(candidate),
                "validator": validator,
            })
    return {
        "passed": not missing_contracts and not failed,
        "missing_contracts": missing_contracts,
        "failed_artifacts": failed,
        "expected_artifact_ids": expected_ids,
    }


def artifact_contract_match_score(planned: Dict[str, Any], rendered: Dict[str, Any]) -> int:
    if str(planned.get("artifact_id") or "") == str(rendered.get("artifact_id") or ""):
        return 100
    planned_tokens = _artifact_tokens(planned.get("artifact_id")) | _artifact_tokens(planned.get("path"))
    rendered_tokens = _artifact_tokens(rendered.get("artifact_id")) | _artifact_tokens(rendered.get("path"))
    if not planned_tokens or not rendered_tokens:
        return 0
    overlap = planned_tokens & rendered_tokens
    if len(overlap) < 2:
        return 0
    score = len(overlap) * 10
    if planned_tokens <= rendered_tokens or rendered_tokens <= planned_tokens:
        score += 20
    if str(planned.get("artifact_type")) == str(rendered.get("artifact_type")):
        score += 5
    return score


def reconcile_step_artifacts(
    step: PlanStep,
    manifest: Dict[str, Any],
    *,
    manifest_path: str | Path = "",
) -> List[Dict[str, Any]]:
    """Reconcile a step with one trusted renderer manifest, preserving history."""
    planned = step_artifact_contracts(step)
    rendered = manifest_artifact_contracts(manifest, manifest_path)
    if not planned or not rendered:
        return []
    reconciled = list(planned)
    events: List[Dict[str, Any]] = []
    for index, old in enumerate(planned):
        old_step_id = str(old.get("producer_step_id") or step.get("step_id") or "")
        scored = sorted(
            (
                (candidate, artifact_contract_match_score(old, candidate))
                for candidate in rendered
                if not candidate.get("producer_step_id")
                or str(candidate.get("producer_step_id")) == old_step_id
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if not scored or scored[0][1] <= 0:
            continue
        best_score = scored[0][1]
        best = [candidate for candidate, score in scored if score == best_score]
        if len(best) != 1:
            continue
        canonical = best[0]
        old_path = str(old.get("path") or "")
        new_path = str(canonical.get("path") or "")
        if not new_path or (old_path == new_path and old.get("source") == "renderer_manifest"):
            continue
        history = list(old.get("history") or [])
        history.append({
            "path": old_path,
            "artifact_id": str(old.get("artifact_id") or ""),
            "source": str(old.get("source") or "planner"),
            "reconciled_at": _now(),
        })
        updated = {
            **old,
            **canonical,
            "producer_step_id": str(old.get("producer_step_id") or step.get("step_id") or ""),
            "declared_path": str(old.get("declared_path") or old_path),
            "source": "renderer_manifest",
            "history": history,
        }
        reconciled[index] = updated
        events.append({
            "event": "artifact_contract_reconciled",
            "step_id": str(step.get("step_id") or ""),
            "artifact_id": str(updated.get("artifact_id") or ""),
            "old_artifact_id": str(old.get("artifact_id") or ""),
            "old_path": old_path,
            "new_path": new_path,
            "source": "renderer_manifest",
            "manifest_path": str(manifest_path or ""),
            "at": _now(),
        })
    if events:
        step["artifacts"] = reconciled
        step["artifact_contract_revision"] = _int(step.get("artifact_contract_revision"), 0) + 1
        step.setdefault("artifact_reconciliation_history", []).extend(deepcopy(events))
        step["updated_at"] = _now()
    return events


def apply_artifact_reconciliation(
    state: PlanState,
    step_id: str,
    manifest: Dict[str, Any],
    *,
    manifest_path: str | Path = "",
) -> List[Dict[str, Any]]:
    """Persist a control-plane contract update without changing plan approval."""
    step = next(
        (item for item in state.get("steps", []) or [] if isinstance(item, dict) and str(item.get("step_id") or "") == str(step_id)),
        None,
    )
    if not step:
        return []
    events = reconcile_step_artifacts(step, manifest, manifest_path=manifest_path)
    if not events:
        return []
    for event in events:
        _append_evidence(step, "artifact_contract_reconciled", json.dumps(event, ensure_ascii=False, sort_keys=True))
    state.setdefault("artifact_reconciliation_history", []).extend(deepcopy(events))
    increment_plan_budget(state, "contract_reconciliation_count")
    state["artifact_contract_revision"] = _int(state.get("artifact_contract_revision"), 0) + 1
    state["updated_at"] = _now()
    return events


def _bounded_plan_manifest_paths(state: PlanState, data_path: str | Path) -> List[Path]:
    """Find manifests referenced by persisted step evidence without broad scans."""
    candidates: List[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen:
            return
        try:
            if resolved.name == "manifest.json" and resolved.is_file() and resolved.stat().st_size <= 2_000_000:
                seen.add(key)
                candidates.append(resolved)
        except OSError:
            return

    for step in state.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        for contract in step_artifact_contracts(step):
            base_dir = str(contract.get("base_dir") or "")
            if base_dir:
                add(Path(base_dir) / "manifest.json")
        for evidence in step.get("evidence", []) or []:
            detail = str(evidence.get("detail") or "") if isinstance(evidence, dict) else str(evidence or "")
            for match in re.finditer(r'"outdir"\s*:\s*("(?:[^"\\]|\\.)*")', detail):
                try:
                    add(Path(json.loads(match.group(1))) / "manifest.json")
                except (json.JSONDecodeError, TypeError):
                    continue
            for match in re.finditer(r"(/[^\s'\"<>|]+/manifest\.json)", detail):
                add(Path(match.group(1)))
            for match in re.finditer(r"(/[^\s'\"<>|]+\.(?:sh|py))", detail):
                add(Path(match.group(1)).parent / "manifest.json")
            if len(candidates) >= 16:
                return candidates
    task_root = plan_output_dir(data_path, str(state.get("task_id") or "default"))
    add(task_root / "manifest.json")
    return candidates[:16]


def reconcile_plan_state_manifests(state: PlanState, data_path: str | Path) -> List[Dict[str, Any]]:
    """Migrate old physical paths from renderer manifests referenced by evidence."""
    all_events: List[Dict[str, Any]] = []
    for manifest_path in _bounded_plan_manifest_paths(state, data_path):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        for step in state.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            events = reconcile_step_artifacts(step, manifest, manifest_path=manifest_path)
            if events:
                all_events.extend(events)
    if not all_events:
        return []
    state.setdefault("artifact_reconciliation_history", []).extend(deepcopy(all_events))
    state["artifact_contract_revision"] = _int(state.get("artifact_contract_revision"), 0) + 1
    increment_plan_budget(state, "contract_reconciliation_count")
    return all_events


def _step_purpose(
    explicit: Any,
    *,
    action: str,
    title: str,
    workflow_stage_ids: List[str],
) -> str:
    """Normalize step intent separately from its controller action."""
    allowed = {"analysis", "validation", "probe", "record_failure", "install", "user_input"}
    declared = str(explicit or "").strip().lower()
    if declared in allowed:
        return declared
    if action == "install":
        return "install"
    if action == "ask_user":
        return "user_input"
    semantic = " ".join([str(title or ""), *workflow_stage_ids]).lower()
    semantic = re.sub(r"[_-]+", " ", semantic)
    if re.search(r"\b(?:validation|validate|verification|verify|preflight|check|audit)\b", semantic):
        return "validation"
    if action == "search":
        return "probe"
    return "analysis"


def normalize_plan_step(
    step: PlanStep,
    index: int = 1,
    *,
    allow_legacy_artifact_inference: bool = False,
) -> PlanStep:
    """Normalize one step in place while retaining unknown legacy fields."""
    if not isinstance(step, dict):
        step = {}
    raw_title = str(step.get("title") or step.get("name") or f"Step {index}")
    parsed = _structured_step_fields(raw_title)
    title = _clean_step_title(raw_title)
    step["step_id"] = str(step.get("step_id") or step.get("id") or _step_id_for(raw_title, index))
    step["title"] = title
    step["action"] = str(step.get("action") or parsed.get("action") or "").strip().lower()
    step["tool"] = str(step.get("tool") or parsed.get("tool") or "").strip()
    step["execution_capability_id"] = str(
        step.get("execution_capability_id") or ""
    ).strip()
    step["operation"] = str(step.get("operation") or "").strip()
    step["capability_contract_hash"] = str(
        step.get("capability_contract_hash") or ""
    ).strip()
    step["skill_ids"] = _string_list(step.get("skill_ids") or parsed.get("skill_ids"))
    # A non-empty machine field is already an explicit scheduling contract.
    # Older/local patches could preserve skill_ids while dropping the parser's
    # derived marker; heal that representational drift deterministically.
    step["skills_declared"] = bool(
        step.get("skills_declared")
        or parsed.get("skills_declared")
        or step["skill_ids"]
    )
    step["workflow_stage_ids"] = _string_list(step.get("workflow_stage_ids") or parsed.get("workflow_stage_ids"))
    step["batch_id"] = str(step.get("batch_id") or parsed.get("batch_id") or "").strip()
    step["purpose"] = _step_purpose(
        step.get("purpose") or parsed.get("purpose"),
        action=step["action"],
        title=title,
        workflow_stage_ids=step["workflow_stage_ids"],
    )
    step["inputs"] = _string_list(
        step.get("inputs") if step.get("inputs") not in (None, "", []) else (step.get("input_contracts") or parsed.get("inputs")),
        dict_keys=("path", "description", "role"),
    )
    step["outputs"] = _string_list(
        step.get("outputs") if isinstance(step.get("outputs"), list) else (step.get("output_contracts") or parsed.get("outputs")),
        dict_keys=("path", "description", "role"),
    )
    step["produces_bindings"] = _typed_effect_list(step.get("produces_bindings"))
    step["produces_observations"] = _typed_effect_list(
        step.get("produces_observations")
    )
    step["explicit_side_effects"] = _typed_effect_list(
        step.get("explicit_side_effects")
    )
    raw_workspace_mutations = step.get("workspace_mutations") or []
    step["workspace_mutations"] = [
        deepcopy(dict(item)) if isinstance(item, Mapping) else str(item).strip()
        for item in raw_workspace_mutations
        if isinstance(item, Mapping) or str(item).strip()
    ] if isinstance(raw_workspace_mutations, list) else []
    if isinstance(step.get("step_effect"), Mapping):
        step["step_effect"] = deepcopy(dict(step["step_effect"]))
    raw_dependencies = _string_list(
        step.get("dependencies") if step.get("dependencies") not in (None, "", []) else (step.get("depends_on") or parsed.get("dependencies"))
    )
    step["dependencies"] = []
    for dependency in raw_dependencies:
        if str(dependency).strip().lower() in {"none", "n/a", "na", "[]", "无"}:
            continue
        match = re.fullmatch(r"\s*step(?:\s*[-_]\s*|\s*)(\d+)\s*", str(dependency), re.IGNORECASE)
        stable_dependency = f"step-{int(match.group(1))}" if match else str(dependency).strip()
        if stable_dependency and stable_dependency not in step["dependencies"]:
            step["dependencies"].append(stable_dependency)
    step["depends_on"] = list(step["dependencies"])
    step["step_type"] = str(step.get("step_type") or ("renderer_composite" if step.get("renderer_id") else "atomic")).strip().lower()
    step["renderer_id"] = str(step.get("renderer_id") or "").strip()
    prior_claim_validation = dict(step.get("semantic_claims_validation") or {})
    if (
        prior_claim_validation.get("valid") is False
        and "semantic_claims_invalid_raw" in step
        and step.get("semantic_claims") in (None, [])
    ):
        raw_semantic_claims = deepcopy(step.get("semantic_claims_invalid_raw"))
    else:
        raw_semantic_claims = (
        step.get("semantic_claims")
        if "semantic_claims" in step
        else parsed.get("semantic_claims") or []
        )
    claim_validation = _semantic_claims_validation(raw_semantic_claims)
    step["semantic_claims"] = list(claim_validation.get("constraints") or [])
    step["semantic_constraint_ids"] = _string_list(step.get("semantic_constraint_ids"))
    step["semantic_claims_validation"] = {
        "valid": bool(claim_validation.get("valid")),
        "errors": deepcopy(list(claim_validation.get("errors") or [])),
    }
    if not claim_validation.get("valid"):
        step["semantic_claims_invalid_raw"] = deepcopy(claim_validation.get("raw"))
    else:
        step.pop("semantic_claims_invalid_raw", None)
    step["input_artifact_ids"] = _string_list(step.get("input_artifact_ids"))
    step["output_artifact_ids"] = _string_list(step.get("output_artifact_ids"))
    dependency_sources = step.get("dependency_sources")
    step["dependency_sources"] = dict(dependency_sources) if isinstance(dependency_sources, dict) else {
        dependency: "planner_declared" for dependency in step["depends_on"]
    }
    criteria = step.get("success_criteria")
    if criteria in (None, "", []):
        criteria = step.get("completion_criteria") or step.get("done") or parsed.get("success_criteria")
    step["success_criteria"] = _string_list(criteria, dict_keys=("criterion", "description", "text"))
    step["status"] = str(step.get("status") or STATUS_PENDING).strip().lower()
    step["risk_level"] = str(step.get("risk_level") or parsed.get("risk_level") or "unspecified").strip().lower()

    explicit_artifacts = step.get("artifacts") if isinstance(step.get("artifacts"), list) else []
    if explicit_artifacts:
        step["artifacts"] = [
            normalize_artifact_contract(
                item,
                producer_step_id=step["step_id"],
                source="structured_plan",
            )
            for item in explicit_artifacts
        ]
    else:
        step["artifacts"] = step_artifact_contracts(
            step,
            allow_legacy_text_fallback=allow_legacy_artifact_inference,
        )
    step.setdefault("artifact_contract_revision", 0)
    step.setdefault("artifact_reconciliation_history", [])
    renderer_execution = step.get("renderer_execution")
    step["renderer_execution"] = dict(renderer_execution) if isinstance(renderer_execution, dict) else {}

    # Legacy consumers remain supported, but canonical fields above own the contract.
    step["input_contracts"] = list(step["inputs"])
    step["output_contracts"] = list(step["outputs"])
    step.setdefault("evidence", [])
    step.setdefault("updated_at", _now())
    return step


def _structured_step_fields(text: str) -> Dict[str, Any]:
    """Extract machine-owned scheduling fields from one checklist item."""
    payload = str(text or "")
    action_match = _ACTION_FIELD_RE.search(payload)
    purpose_match = _PURPOSE_FIELD_RE.search(payload)
    success_match = _SUCCESS_FIELD_RE.search(payload)
    skills_match = _SKILLS_FIELD_RE.search(payload)
    stages_match = _STAGES_FIELD_RE.search(payload)
    batch_match = _BATCH_FIELD_RE.search(payload)
    tool_match = _TOOL_FIELD_RE.search(payload)
    inputs_match = _INPUTS_FIELD_RE.search(payload)
    outputs_match = _OUTPUTS_FIELD_RE.search(payload)
    dependencies_match = _DEPENDENCIES_FIELD_RE.search(payload)
    risk_match = _RISK_FIELD_RE.search(payload)
    batch_values = _list_field(batch_match.group("value")) if batch_match else []
    return {
        "action": str(action_match.group("value") if action_match else "").strip().lower(),
        "purpose": str(purpose_match.group("value") if purpose_match else "").strip().lower(),
        "skill_ids": _list_field(skills_match.group("value")) if skills_match else [],
        "skills_declared": bool(skills_match),
        "workflow_stage_ids": _list_field(stages_match.group("value")) if stages_match else [],
        "batch_id": batch_values[0] if batch_values else "",
        "tool": _valid_contract_text(tool_match.group("value")) if tool_match else "",
        "inputs": _list_field(inputs_match.group("value")) if inputs_match else [],
        "outputs": _list_field(outputs_match.group("value")) if outputs_match else [],
        "dependencies": _list_field(dependencies_match.group("value")) if dependencies_match else [],
        "risk_level": _valid_contract_text(risk_match.group("value")).lower() if risk_match else "unspecified",
        "semantic_claims": _semantic_claims_from_step_payload(payload),
        "success_criteria": (
            [_valid_contract_text(success_match.group("value"))]
            if success_match and _valid_contract_text(success_match.group("value"))
            else []
        ),
    }


def _backfill_step_contracts(
    steps: List[PlanStep],
    *,
    allow_legacy_artifact_inference: bool = False,
) -> List[PlanStep]:
    """Migrate persisted checklist fields without regenerating an approved plan."""
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        normalize_plan_step(
            step,
            index,
            allow_legacy_artifact_inference=allow_legacy_artifact_inference,
        )
    return steps


def _dedupe_steps(steps: List[PlanStep]) -> List[PlanStep]:
    seen_titles: set[str] = set()
    seen_ids: set[str] = set()
    deduped: List[PlanStep] = []
    for step in steps:
        title_key = str(step.get("title", "")).strip().lower()
        step_id = str(step.get("step_id", "")).strip()
        if not title_key or title_key in seen_titles:
            continue
        if step_id in seen_ids:
            step_id = f"step-{len(deduped) + 1}"
            step["step_id"] = step_id
        seen_titles.add(title_key)
        seen_ids.add(step_id)
        deduped.append(step)
    return deduped


def _enrich_plan_steps(steps: List[PlanStep], plan_text: str) -> List[PlanStep]:
    """Capture checklist contracts and enrich them from stable detailed steps.

    The checklist remains the primary scheduler structure.  A matching
    detailed ``Step N`` section may add compatible fields that the concise
    checklist omitted.  Field labels accept the historical bold/non-bold,
    singular/plural Markdown variants.
    """
    lines = str(plan_text or "").splitlines()
    labels = {
        "assumptions": "assumptions",
        "assumption": "assumptions",
        "required constraints": "required_constraints",
        "required constraint": "required_constraints",
        "input contract": "input_contracts",
        "output contract": "output_contracts",
        "environment contract": "environment_contract",
        "action": "action",
        "purpose": "purpose",
        "skills": "skill_ids",
        "skill": "skill_ids",
        "stage": "workflow_stage_ids",
        "stages": "workflow_stage_ids",
        "batch": "batch_id",
        "tool": "tool",
        "input": "inputs",
        "inputs": "inputs",
        "output": "outputs",
        "outputs": "outputs",
        "dependencies": "dependencies",
        "depends on": "depends_on",
        "risk": "risk_level",
        "risk level": "risk_level",
        "allowed fallbacks": "allowed_fallbacks",
        "forbidden fallbacks": "forbidden_fallbacks",
        "semantic claims": "semantic_claims",
        "done": "success_criteria",
        "success": "success_criteria",
        "success criteria": "success_criteria",
        "completion criteria": "success_criteria",
        "verify": "success_criteria",
    }
    label_pattern = "|".join(
        re.escape(value) for value in sorted(labels, key=len, reverse=True)
    )
    field_re = re.compile(
        rf"^\s*(?:[-*]\s*)?(?:\*\*)?(?P<label>{label_pattern})(?:\*\*)?\s*:\s*(?P<value>.*)$",
        re.IGNORECASE,
    )

    def parse_segment(segment: List[str], *, title: str = "") -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        inline_success = _SUCCESS_FIELD_RE.search(title)
        active_multiline_key = (
            "success_criteria"
            if inline_success and not _valid_contract_text(inline_success.group("value"))
            else ""
        )
        for raw in segment:
            # The first line of a primary segment is the step marker itself.
            # An inline empty ``Done:`` on that marker intentionally opens a
            # multiline field; do not mistake ``Step N:`` for a new unknown
            # label and clear the active field before its bullets are read.
            if _CHECKBOX_RE.match(raw) or _STEP_LINE_RE.match(raw):
                continue
            match = field_re.match(raw)
            if not match:
                if re.match(
                    r"^\s*(?:[-*]\s*)?(?:\*\*)?[^:：]{1,60}(?:\*\*)?\s*[:：]",
                    raw,
                ):
                    active_multiline_key = ""
                    continue
                if active_multiline_key:
                    bullet = re.match(r"^\s+[-*]\s+(?P<value>.+?)\s*$", raw)
                    value = _valid_contract_text(bullet.group("value")) if bullet else ""
                    if value:
                        if active_multiline_key in {"input_contracts", "output_contracts"}:
                            values.setdefault(active_multiline_key, []).append({"description": value})
                        elif active_multiline_key in {"inputs", "outputs", "skill_ids", "workflow_stage_ids"}:
                            values.setdefault(active_multiline_key, []).extend(_list_field(value))
                        elif active_multiline_key in {
                            "dependencies", "depends_on", "success_criteria",
                        }:
                            values.setdefault(active_multiline_key, []).append(value)
                continue
            key = labels.get(match.group("label").lower())
            active_multiline_key = key or ""
            value = _valid_contract_text(match.group("value"))
            if not key or not value:
                continue
            if key in {"depends_on", "dependencies"}:
                for dependency in re.findall(r"step[-_ ]?\d+", value, re.IGNORECASE):
                    number = re.search(r"\d+", dependency)
                    stable_id = f"step-{int(number.group(0))}" if number else ""
                    if stable_id and stable_id not in values.setdefault(key, []):
                        values[key].append(stable_id)
            elif key in {"input_contracts", "output_contracts"}:
                values.setdefault(key, []).append({"description": value})
            elif key in {"inputs", "outputs", "skill_ids", "workflow_stage_ids"}:
                values.setdefault(key, []).extend(_list_field(value))
            elif key == "semantic_claims":
                values.setdefault(key, []).extend(_semantic_claims_field(value))
            elif key == "success_criteria":
                values.setdefault(key, []).append(value)
            elif key in {
                "action", "purpose", "batch_id", "tool", "risk_level", "environment_contract",
            }:
                values[key] = value
            else:
                values.setdefault(key, []).append(value)
        return values

    detailed_by_step: Dict[str, Dict[str, Any]] = {}
    detailed_markers: List[Tuple[int, str, int]] = []
    detailed_heading_re = re.compile(
        r"^\s*(?P<marks>#{2,6})\s+(?:\[(?P<stable>step[-_ ]?\d+)\]|Step\s*(?P<number>\d+)\b)",
        re.IGNORECASE,
    )
    for position, line in enumerate(lines):
        match = detailed_heading_re.match(line)
        if not match:
            continue
        raw_id = match.group("stable") or f"step-{match.group('number')}"
        digits = re.search(r"\d+", raw_id)
        if digits:
            detailed_markers.append((
                position,
                f"step-{int(digits.group(0))}",
                len(match.group("marks")),
            ))
    for start, step_id, heading_level in detailed_markers:
        end = len(lines)
        for position in range(start + 1, len(lines)):
            heading = re.match(r"^\s*(?P<marks>#{1,6})\s+", lines[position])
            if heading and len(heading.group("marks")) <= heading_level:
                end = position
                break
        detailed_by_step[step_id] = parse_segment(lines[start:end])

    marker_positions = [
        index for index, line in enumerate(lines)
        if _CHECKBOX_RE.match(line) or _STEP_LINE_RE.match(line)
    ]
    list_fields = {
        "assumptions", "required_constraints", "input_contracts", "output_contracts",
        "inputs", "outputs", "dependencies", "depends_on", "allowed_fallbacks",
        "forbidden_fallbacks", "success_criteria",
        "skill_ids", "workflow_stage_ids", "semantic_claims",
    }
    for index, step in enumerate(steps):
        title = str(step.get("title") or "").lower()
        start = next((pos for pos in marker_positions if title and title[:60] in lines[pos].lower()), None)
        if start is None:
            start = marker_positions[index] if index < len(marker_positions) else 0
        end = next((pos for pos in marker_positions if pos > start), len(lines))
        primary = parse_segment(lines[start:end], title=str(step.get("title") or ""))
        for key, value in primary.items():
            step[key] = value

        detailed = detailed_by_step.get(str(step.get("step_id") or ""), {})
        for key, value in detailed.items():
            if key in list_fields:
                current = list(step.get(key) or [])
                for item in value if isinstance(value, list) else [value]:
                    marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                    if all(
                        json.dumps(existing, ensure_ascii=False, sort_keys=True, default=str) != marker
                        for existing in current
                    ):
                        current.append(item)
                step[key] = current
            elif step.get(key) in (None, "", "unspecified"):
                step[key] = value

        step.setdefault("command_intent", str(step.get("title") or "").strip())
        structured = _structured_step_fields(str(step.get("title") or ""))
        for key, value in structured.items():
            step.setdefault(key, value)
        step.setdefault("required_environment", None)
        step.setdefault("expected_tool_versions", {})
        step.setdefault("input_contracts", [])
        step.setdefault("output_contracts", [])
        step.setdefault("allowed_fallbacks", [])
        step.setdefault("forbidden_fallbacks", [])
        step.setdefault("success_criteria", [])
        step.setdefault("depends_on", [])
        step.setdefault("assumptions", [])
        step.setdefault("required_constraints", [])
        step.setdefault("semantic_claims", [])
        normalize_plan_step(
            step,
            index + 1,
            allow_legacy_artifact_inference=True,
        )
    return steps


def _lines_in_named_section(plan_text: str, section_name: str) -> List[str]:
    """Return lines under a markdown heading whose title contains section_name."""
    wanted = str(section_name or "").strip().lower()
    lines: List[str] = []
    in_section = False
    section_level = 0
    for line in str(plan_text or "").splitlines():
        heading = re.match(r"^\s*(#{2,6})\s+(?P<title>.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group("title").strip().lower()
            if wanted and wanted in title:
                in_section = True
                section_level = level
                continue
            if in_section and level <= section_level:
                in_section = False
        if in_section:
            lines.append(line)
    return lines


def _lines_excluding_named_section(plan_text: str, section_name: str) -> List[str]:
    """Return markdown lines except those under a matching heading."""
    unwanted = str(section_name or "").strip().lower()
    lines: List[str] = []
    in_section = False
    section_level = 0
    for line in str(plan_text or "").splitlines():
        heading = re.match(r"^\s*(#{2,6})\s+(?P<title>.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group("title").strip().lower()
            if unwanted and unwanted in title:
                in_section = True
                section_level = level
                continue
            if in_section and level <= section_level:
                in_section = False
        if not in_section:
            lines.append(line)
    return lines


def parse_plan_steps(plan_text: str) -> List[PlanStep]:
    """Extract executable checklist steps from a planner response.

    Preferred format is "- [ ] Step N: ...". For old plans, fall back to
    explicit "Step N" lines, then numbered list items, then section headings.
    """

    created = _now()
    def _parse_checkbox_lines(lines: List[str]) -> List[PlanStep]:
        parsed: List[PlanStep] = []
        for line in lines:
            match = _CHECKBOX_RE.match(line)
            if not match:
                continue
            title = _clean_step_title(match.group("title"))
            index = len(parsed) + 1
            parsed.append(
                {
                    "step_id": _step_id_for(title, index),
                    "title": title,
                    "status": _status_from_mark(match.group("mark")),
                    "evidence": [],
                    "updated_at": created,
                    **_structured_step_fields(title),
                }
            )
        return parsed

    checkbox_steps = _parse_checkbox_lines(_lines_in_named_section(plan_text, "execution checklist"))
    if checkbox_steps:
        return _enrich_plan_steps(_dedupe_steps(checkbox_steps), plan_text)

    checkbox_steps = _parse_checkbox_lines(_lines_excluding_named_section(plan_text, "validation checklist"))
    if checkbox_steps:
        return _enrich_plan_steps(_dedupe_steps(checkbox_steps), plan_text)

    step_lines: List[Tuple[Optional[int], str]] = []
    for line in str(plan_text or "").splitlines():
        match = _STEP_LINE_RE.match(line)
        if not match:
            continue
        title = _clean_step_title(match.group("title"))
        if not title:
            continue
        number_raw = match.group("step_a") or match.group("step_b")
        number = int(number_raw) if number_raw else None
        step_lines.append((number, title))

    if step_lines:
        steps = []
        for idx, (number, title) in enumerate(step_lines, start=1):
            display_title = title if re.search(r"\bStep\s*\d+\b", title, re.I) else f"Step {number or idx}: {title}"
            steps.append(
                {
                    "step_id": f"step-{number or idx}",
                    "title": display_title,
                    "status": STATUS_PENDING,
                    "evidence": [],
                    "updated_at": created,
                    **_structured_step_fields(display_title),
                }
            )
        return _enrich_plan_steps(_dedupe_steps(steps), plan_text)

    heading_steps: List[PlanStep] = []
    ignored_headings = {
        "requirement analysis",
        "step-by-step execution plan",
        "validation checklist",
        "execution checklist",
        "planner validation warnings",
        "workflow skill validation warnings",
    }
    for line in str(plan_text or "").splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = _clean_step_title(match.group("title"))
        if not title or title.lower() in ignored_headings:
            continue
        index = len(heading_steps) + 1
        heading_steps.append(
            {
                "step_id": f"step-{index}",
                "title": f"Step {index}: {title}",
                "status": STATUS_PENDING,
                "evidence": [],
                "updated_at": created,
                **_structured_step_fields(title),
            }
        )
    return _enrich_plan_steps(_dedupe_steps(heading_steps), plan_text)


def parse_structured_plan(plan_text: str) -> Dict[str, Any]:
    """Strictly decode the canonical Planner JSON object.

    A single whole-response JSON fence is accepted as transport decoration.
    Arbitrary prose, extracted fragments, Markdown headings, and best-effort
    fallback parsing are deliberately rejected.
    """
    raw = str(plan_text or "").strip()
    if raw.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n?(?P<body>.*)\n?```", raw, re.IGNORECASE | re.DOTALL)
        if not match:
            raise PlanStructureError([{
                "field_path": "$",
                "message": "response contains an incomplete or mixed JSON fence",
            }], raw)
        raw = match.group("body").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanStructureError([{
            "field_path": "$",
            "message": f"response is not exactly one JSON object: {exc.msg} at character {exc.pos}",
        }], raw) from exc
    if not isinstance(payload, dict):
        raise PlanStructureError([{
            "field_path": "$",
            "message": "plan must be a JSON object",
        }], raw)
    diagnostics = structured_plan_candidate_diagnostics(payload)
    if diagnostics:
        raise PlanStructureError(diagnostics, raw)
    return deepcopy(payload)


def _constraint_index(task_spec: Mapping[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    return {
        str(item.get("constraint_id") or ""): dict(item)
        for item in semantic_constraints_from_task_spec(task_spec or {})
        if str(item.get("constraint_id") or "")
    }


def resolve_plan_semantic_claims(
    state: PlanState,
    task_spec: Mapping[str, Any] | None,
) -> Tuple[PlanState, List[Dict[str, Any]]]:
    """Resolve legacy Planner references; fresh v10 plans carry none."""
    candidate = deepcopy(state)
    compatibility = dict(candidate.get("compatibility") or {})
    source_schema_version = _int(
        compatibility.get("source_schema_version")
        or candidate.get("schema_version"),
        PLAN_SCHEMA_VERSION,
    )
    if source_schema_version >= PLAN_SCHEMA_VERSION:
        return candidate, []
    index = _constraint_index(task_spec)
    issues: List[Dict[str, Any]] = []
    for position, raw_step in enumerate(candidate.get("steps", []) or []):
        if not isinstance(raw_step, dict):
            continue
        step = normalize_plan_step(raw_step, position + 1)
        step_id = str(step.get("step_id") or "")
        claim_validation = dict(step.get("semantic_claims_validation") or {})
        if not claim_validation.get("valid", True):
            issues.append(validation_issue(
                "SEMANTIC_CLAIMS_INVALID",
                owner="planner",
                repair_target=f"plan.steps[{step_id}].semantic_constraint_ids",
                field_path=f"plan.steps[{position}].semantic_claims",
                message="Planner semantic claims failed schema validation.",
                plan_step_id=step_id,
                evidence=deepcopy(list(claim_validation.get("errors") or [])),
            ))
        resolved: List[Dict[str, Any]] = []
        for ref_index, constraint_id in enumerate(step.get("semantic_constraint_ids") or []):
            constraint = index.get(str(constraint_id))
            if constraint is None:
                issues.append(validation_issue(
                    "SEMANTIC_CONSTRAINT_REFERENCE_INVALID",
                    owner="planner",
                    repair_target=f"plan.steps[{step_id}].semantic_constraint_ids",
                    field_path=f"plan.steps[{position}].semantic_constraint_ids[{ref_index}]",
                    message=f"Unknown TaskSpec semantic constraint ID `{constraint_id}`.",
                    plan_step_id=step_id,
                ))
                continue
            resolved.append(deepcopy(constraint))
        if step.get("semantic_constraint_ids"):
            step["semantic_claims"] = resolved
            step["semantic_claims_validation"] = {"valid": True, "errors": []}
            step.pop("semantic_claims_invalid_raw", None)
        candidate["steps"][position] = step
    return candidate, issues


def _semantic_step(step: PlanStep) -> Dict[str, Any]:
    normalized = normalize_plan_step(dict(step), 1)
    ignored_inline_labels = re.compile(
        rf"^\s*(?:Action|Skills?|Stages?|Batch|Tool|Inputs?|Outputs?|Dependencies|Depends on|Risk(?: level)?|{_SUCCESS_FIELD_PATTERN})\s*:",
        re.IGNORECASE,
    )
    semantic_title = " | ".join(
        part.strip()
        for part in str(normalized.get("title") or "").split("|")
        if part.strip() and not ignored_inline_labels.match(part)
    )
    payload = {field: normalized.get(field) for field in PLAN_STEP_FIELDS if field != "status"}
    if normalized.get("generate_binding_input_contract"):
        payload["generate_binding_input_contract"] = deepcopy(normalized["generate_binding_input_contract"])
    payload["title"] = semantic_title
    payload["selected_skill_id"] = str(normalized.get("selected_skill_id") or "")
    if normalized.get("operation"):
        payload["operation"] = str(normalized.get("operation") or "")
    if isinstance(normalized.get("step_effect"), Mapping):
        payload["step_effect"] = deepcopy(dict(normalized.get("step_effect") or {}))
    payload["bound_outputs"] = deepcopy(list(normalized.get("bound_outputs") or []))
    payload["renderer_invocation_contract"] = deepcopy(
        dict(normalized.get("renderer_invocation_contract") or {})
    )
    payload["artifacts"] = sorted(
        [
            {
                "artifact_id": str(item.get("artifact_id") or ""),
                "role": str(item.get("role") or ""),
                "path": str(item.get("path") or ""),
                "required": bool(item.get("required", True)),
                "producer_step_id": str(item.get("producer_step_id") or ""),
                "producer": str(item.get("producer") or ""),
                "source": str(item.get("source") or ""),
                "validator": str(item.get("validator") or ""),
            }
            for item in normalized.get("artifacts", []) or []
            if isinstance(item, dict)
        ],
        key=lambda item: (item["artifact_id"], item["path"], item["producer_step_id"]),
    )
    return payload


def semantic_plan_hash(state: PlanState) -> str:
    """Hash only execution semantics; progress, evidence, timestamps and aliases are excluded."""
    payload = {
        "steps": [_semantic_step(step) for step in state.get("steps", []) if isinstance(step, dict)],
        "task_output_materializations": deepcopy(
            list(state.get("task_output_materializations") or [])
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_PLAN_RECORD_EXCLUDED_KEYS = {
    "plan_record_hash",
    "plan_semantic_hash",
    "validation_status",
    "validation_issues",
    "plan_validation_state",
    "approval_record",
    "approval_snapshot",
    "runtime_state",
    "execution_provenance",
    "task_spec",
    "task_state",
    "audit",
    "audit_refs",
    "updated_at",
}


def plan_semantic_payload(state: PlanState) -> Dict[str, Any]:
    """Versioned semantic domain used for approval/staleness decisions."""
    return {
        "canonicalization_version": PLAN_CANONICALIZATION_VERSION,
        "steps": [
            _semantic_step(step)
            for step in state.get("steps", []) or []
            if isinstance(step, dict)
        ],
        "task_output_materializations": deepcopy(
            list(state.get("task_output_materializations") or [])
        ),
    }


def plan_record_payload(state: PlanState) -> Dict[str, Any]:
    """PlanSpec record domain, excluding validation/runtime/approval state."""
    return {
        "canonicalization_version": PLAN_CANONICALIZATION_VERSION,
        "record": {
            str(key): deepcopy(value)
            for key, value in sorted(dict(state or {}).items(), key=lambda pair: str(pair[0]))
            if str(key) not in _PLAN_RECORD_EXCLUDED_KEYS
        },
    }


def plan_record_hash(state: PlanState) -> str:
    encoded = json.dumps(
        plan_record_payload(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def has_materialized_plan(state: Mapping[str, Any] | None) -> bool:
    """Return whether canonical state contains a real Plan, not a default shell.

    ``plan_revision`` and ``plan_version`` exist on a fresh Consultant-first
    state, so neither is evidence that Planner has materialized anything. The
    canonical ``current_plan`` partition is created from a non-empty PlanSpec
    and carries both semantic and record identities. A snapshot ref is an even
    stronger durable marker once the validated Plan is committed.
    """
    current_plan = (
        dict((state or {}).get("current_plan") or {})
        if isinstance(state, Mapping)
        else {}
    )
    if not current_plan:
        return False
    if str(current_plan.get("plan_snapshot_ref") or "").strip():
        return True
    plan_spec = current_plan.get("plan_spec")
    return bool(
        isinstance(plan_spec, Mapping)
        and list(plan_spec.get("steps") or [])
        and str(current_plan.get("plan_semantic_hash") or "").strip()
        and str(current_plan.get("plan_record_hash") or "").strip()
    )


def changed_plan_step_ids(previous: PlanState, candidate: PlanState) -> List[str]:
    """Return only steps whose executable semantics changed."""
    old = {
        str(step.get("step_id") or ""): _semantic_step(step)
        for step in previous.get("steps", []) or []
        if isinstance(step, dict) and str(step.get("step_id") or "")
    }
    new = {
        str(step.get("step_id") or ""): _semantic_step(step)
        for step in candidate.get("steps", []) or []
        if isinstance(step, dict) and str(step.get("step_id") or "")
    }
    return sorted(step_id for step_id in set(old) | set(new) if old.get(step_id) != new.get(step_id))


def _violation_record(value: Any) -> Dict[str, Any]:
    """Preserve exact issue identity and location for Planner Repair.

    Legacy prose is still accepted at the compatibility boundary, but new
    validators reach this function as typed validation issues.  In particular,
    this projection must not collapse them back to ``step/field/reason``.
    """
    raw = dict(value) if isinstance(value, Mapping) else {
        "message": str(value or "").strip()
    }
    canonical = normalize_validation_issue(raw)
    reason = str(
        raw.get("message") or raw.get("reason") or canonical.get("message") or ""
    ).strip()
    step_id = str(raw.get("step_id") or raw.get("plan_step_id") or "").strip()
    field_path = str(raw.get("field_path") or canonical.get("field_path") or "").strip()
    field = str(raw.get("field") or "").strip()
    if not step_id and not isinstance(value, Mapping):
        step_match = re.search(r"\b(step[-_ ]?\d+)\b", reason, re.IGNORECASE)
        if step_match:
            digits = re.search(r"\d+", step_match.group(1))
            step_id = f"step-{digits.group(0)}" if digits else ""
    if not field and not isinstance(value, Mapping):
        field_match = re.search(
            r"\b(action|tool|skills?|skill_ids|inputs?|outputs?|dependencies|success_criteria|risk_level)\b",
            reason,
            re.IGNORECASE,
        )
        field = str(field_match.group(1) if field_match else "").lower()

    affected_step_ids: List[str] = []
    for key in ("step_id", "plan_step_id", "consumer_step_id"):
        item = str(raw.get(key) or "").strip()
        if item and item not in affected_step_ids:
            affected_step_ids.append(item)
    for key in ("affected_step_ids", "producer_step_ids", "cycle"):
        for item in raw.get(key, []) or []:
            text = str(item or "").strip()
            if text and text not in affected_step_ids:
                affected_step_ids.append(text)

    excerpt = str(
        raw.get("bounded_excerpt")
        or raw.get("offending_excerpt")
        or raw.get("excerpt")
        or ""
    )[:500]
    record: Dict[str, Any] = {
        "code": str(raw.get("code") or raw.get("violation_code") or canonical.get("code") or "VALIDATION_ERROR"),
        "owner": str(raw.get("owner") or canonical.get("owner") or "planner"),
        "validator_id": str(raw.get("validator_id") or ""),
        "rule_id": str(raw.get("rule_id") or ""),
        "step_id": step_id,
        "affected_step_ids": affected_step_ids,
        "field": field,
        "field_path": field_path,
        "message": reason,
        "reason": reason,
        "fingerprint": str(raw.get("fingerprint") or canonical.get("fingerprint") or ""),
        "bounded_excerpt": excerpt,
        "repair_target": str(raw.get("repair_target") or canonical.get("repair_target") or ""),
    }
    for key in ("expected_contract", "actual_value", "actual_value_summary"):
        if raw.get(key) not in (None, "", [], {}):
            record[key] = deepcopy(raw[key])
    return record


def plan_dependency_closure(state: PlanState, step_ids: List[str]) -> List[str]:
    """Return affected steps plus direct dependency/dependent neighbors.

    Plan repairs are deliberately local.  The closure contains only steps whose
    contracts can be invalidated by replacing one of the requested steps.
    """
    steps = [normalize_plan_step(dict(step), index) for index, step in enumerate(state.get("steps", []), 1) if isinstance(step, dict)]
    known = {str(step.get("step_id") or ""): step for step in steps}
    affected = {str(item) for item in step_ids if str(item) in known}
    if not affected:
        return []
    changed = True
    while changed:
        changed = False
        for step_id, step in known.items():
            dependencies = {str(item) for item in step.get("dependencies", []) or []}
            if step_id in affected and dependencies - affected:
                affected.update(item for item in dependencies if item in known)
                changed = True
            if step_id not in affected and dependencies & affected:
                affected.add(step_id)
                changed = True
    return [str(step.get("step_id")) for step in steps if str(step.get("step_id")) in affected]


def _step_graph(state: PlanState) -> Tuple[Dict[str, List[str]], List[str]]:
    graph: Dict[str, List[str]] = {}
    for index, raw in enumerate(state.get("steps", []) or [], start=1):
        if not isinstance(raw, dict):
            continue
        step = normalize_plan_step(raw, index)
        graph[str(step.get("step_id") or "")] = [str(item) for item in step.get("depends_on", []) or []]
    cycle: List[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: List[str]) -> bool:
        if node in visiting:
            start = trail.index(node) if node in trail else 0
            cycle.extend([*trail[start:], node])
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in graph.get(node, []):
            if dependency in graph and visit(dependency, [*trail, node]):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for node in graph:
        if visit(node, []):
            break
    return graph, cycle


def get_step_dependencies(state: PlanState, step_id: str) -> Dict[str, Any]:
    graph, cycle = _step_graph(state)
    if cycle:
        return {"status": "cycle_error", "step_id": str(step_id), "step_ids": [], "cycle": cycle}
    if str(step_id) not in graph:
        return {"status": "not_found", "step_id": str(step_id), "step_ids": [], "cycle": []}
    return {"status": "ok", "step_id": str(step_id), "step_ids": list(graph[str(step_id)]), "cycle": []}


def get_step_descendants(state: PlanState, step_id: str) -> Dict[str, Any]:
    graph, cycle = _step_graph(state)
    if cycle:
        return {"status": "cycle_error", "step_id": str(step_id), "step_ids": [], "cycle": cycle}
    if str(step_id) not in graph:
        return {"status": "not_found", "step_id": str(step_id), "step_ids": [], "cycle": []}
    descendants: List[str] = []
    frontier = [str(step_id)]
    while frontier:
        parent = frontier.pop(0)
        for candidate, dependencies in graph.items():
            if parent in dependencies and candidate not in descendants:
                descendants.append(candidate)
                frontier.append(candidate)
    return {"status": "ok", "step_id": str(step_id), "step_ids": descendants, "cycle": []}


def _artifact_head(state: PlanState, artifact_id: str) -> Dict[str, Any]:
    provenance = ensure_provenance_state(state)
    version_id = str(provenance.get("artifact_heads", {}).get(str(artifact_id)) or "")
    return dict(provenance.get("artifacts", {}).get(version_id) or {})


def get_artifact_producer(state: PlanState, artifact_id: str) -> Dict[str, Any]:
    record = _artifact_head(state, artifact_id)
    if not record:
        return {"status": "not_found", "artifact_id": str(artifact_id), "producer_status": "missing"}
    producer_status = "known" if record.get("producer_attempt_id") or record.get("producer_step_id") else "producer_unknown"
    return {**record, "producer_status": producer_status}


def get_artifact_consumers(state: PlanState, artifact_id: str) -> Dict[str, Any]:
    record = _artifact_head(state, artifact_id)
    if not record:
        return {"status": "not_found", "artifact_id": str(artifact_id), "step_ids": [], "stage_ids": []}
    return {
        "status": "ok",
        "artifact_id": str(artifact_id),
        "step_ids": list(record.get("consumer_step_ids") or []),
        "stage_ids": list(record.get("consumer_stage_ids") or []),
    }


def get_attempt_artifacts(state: PlanState, attempt_id: str) -> Dict[str, Any]:
    provenance = ensure_provenance_state(state)
    attempt = dict(provenance.get("attempts", {}).get(str(attempt_id)) or {})
    return {
        "status": "ok" if attempt else "not_found",
        "attempt_id": str(attempt_id),
        "artifact_ids": list(attempt.get("produced_artifact_ids") or []),
        "artifact_version_ids": list(attempt.get("artifact_version_ids") or []),
    }


def get_step_input_producers(state: PlanState, step_id: str) -> Dict[str, Any]:
    step = _step_by_id(state, step_id)
    if not step:
        return {"status": "not_found", "step_id": str(step_id), "producers": []}
    producers = [get_artifact_producer(state, artifact_id) for artifact_id in step.get("input_artifact_ids", []) or []]
    return {"status": "ok", "step_id": str(step_id), "producers": producers}


def get_manifest_stage_dependencies(state: PlanState, stage_id: str) -> Dict[str, Any]:
    provenance = ensure_provenance_state(state)
    stages = provenance.get("stages", {})
    stage = dict(stages.get(str(stage_id)) or {})
    if not stage:
        return {"status": "not_found", "stage_id": str(stage_id), "stage_ids": [], "cycle": []}
    graph = {str(key): [str(item) for item in value.get("depends_on_stage_ids", []) or []] for key, value in stages.items()}
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle: List[str] = []

    def visit(node: str, trail: List[str]) -> bool:
        if node in visiting:
            start = trail.index(node) if node in trail else 0
            cycle.extend([*trail[start:], node])
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in graph.get(node, []):
            if dependency in graph and visit(dependency, [*trail, node]):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    visit(str(stage_id), [])
    return {
        "status": "cycle_error" if cycle else "ok",
        "stage_id": str(stage_id),
        "stage_ids": list(stage.get("depends_on_stage_ids") or []) if not cycle else [],
        "cycle": cycle,
    }


def provenance_debug_dump(state: PlanState) -> Dict[str, Any]:
    provenance = deepcopy(ensure_provenance_state(state))
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "task_id": str(state.get("task_id") or ""),
        "plan_id": str(state.get("plan_id") or ""),
        "summary": {
            "action_count": len(provenance.get("actions", {})),
            "attempt_count": len(provenance.get("attempts", {})),
            "artifact_version_count": len(provenance.get("artifacts", {})),
            "manifest_count": len(provenance.get("manifests", {})),
            "stage_count": len(provenance.get("stages", {})),
            "validation_count": len(provenance.get("validations", {})),
        },
        "provenance": provenance,
    }


def _legacy_execution_is_probe(execution: Dict[str, Any]) -> bool:
    purpose = str(execution.get("purpose") or "").lower()
    role = str(execution.get("command_role") or "").lower()
    if purpose in {"probe", "diagnostic"} or role in {"diagnostic", "optional_probe", "capability_probe"}:
        return True
    code = str(
        execution.get("code")
        or execution.get("code_summary")
        or execution.get("generated_code")
        or ""
    ).lower()
    inspection = re.search(
        r"(?:^|[;&|\n]\s*)(?:head|grep|rg|sed|cat|tail|awk|less|more)\b|(?:^|\s)--help(?:\s|$)|(?:^|\s)-h(?:\s|$)",
        code,
    )
    writes = re.search(r"(?:^|\s)(?:>|>>|tee\s|cp\s|mv\s|rm\s)|\s-o\s+\S+", code)
    return bool(inspection and not writes)


def migrate_execution_provenance(state: PlanState, execution_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    provenance = ensure_provenance_state(state)
    digest = _stable_payload_hash([
        {key: item.get(key) for key in ("execution_id", "action_id", "attempt_id", "step_id", "purpose", "command_role", "exit_code")}
        for item in execution_records or [] if isinstance(item, dict)
    ])
    migration_id = f"execution_records:{digest[:20]}"
    already = any(str(item.get("migration_id") or "") == migration_id for item in provenance.get("migration_log", []))
    if already:
        return {"migration_id": migration_id, "applied": False}
    for index, execution in enumerate(execution_records or [], start=1):
        if not isinstance(execution, dict):
            continue
        action_id = str(execution.get("action_id") or execution.get("execution_id") or _stable_id("legacy_action", index))
        attempt_id = str(execution.get("attempt_id") or _stable_id("legacy_attempt", action_id, index))
        role = str(execution.get("command_role") or "primary").lower()
        if _legacy_execution_is_probe(execution):
            role = "diagnostic"
        provenance["actions"].setdefault(action_id, {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "action_id": action_id,
            "plan_id": str(state.get("plan_id") or ""),
            "plan_version": str(execution.get("plan_version") or state.get("plan_version") or ""),
            "step_id": str(execution.get("step_id") or ""),
            "stage_id": "",
            "manifest_id": "",
            "manifest_action_id": str(execution.get("manifest_action_id") or ""),
            "action_type": "execute",
            "command_role": role,
            "source": str(execution.get("action_source") or "legacy_execution_record"),
            "raw_action": {},
            "normalized_action": {},
            "generated_script_path": str(execution.get("script_path") or execution.get("script_file") or ""),
            "created_at": str(execution.get("started_at") or ""),
            "updated_at": str(execution.get("finished_at") or ""),
        })
        provenance["attempts"].setdefault(attempt_id, {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "action_id": action_id,
            "plan_id": str(state.get("plan_id") or ""),
            "step_id": str(execution.get("step_id") or ""),
            "stage_id": "",
            "executed_script_path": str(execution.get("script_path") or execution.get("script_file") or ""),
            "execution_record_id": str(execution.get("execution_id") or action_id),
            "started_at": str(execution.get("started_at") or ""),
            "finished_at": str(execution.get("finished_at") or ""),
            "returncode": execution.get("exit_code") if execution.get("exit_code") is not None else execution.get("returncode"),
            "status": str(execution.get("execution_status") or "unknown"),
            "command_role": role,
            "produced_artifact_ids": [],
            "artifact_version_ids": [],
            "before_artifacts": {},
            "migration_source": "execution_records.jsonl",
        })
        attempt = provenance["attempts"][attempt_id]
        if role != "diagnostic":
            produced_paths = {
                str(Path(item).expanduser().resolve())
                for item in execution.get("produced_artifacts", []) or []
                if str(item)
            }
            for artifact_id, head in list(provenance.get("artifact_heads", {}).items()):
                definition = dict(provenance.get("artifacts", {}).get(head) or {})
                if str(definition.get("path") or "") not in produced_paths:
                    continue
                resolution_ref = str(definition.get("result_resolution_ref") or "")
                resolution_receipt = ((state.get("task_state") or {}).get("result_resolution_receipts") or {}).get(resolution_ref) or {}
                if (resolution_receipt and definition.get("producer_attempt_id") == resolution_receipt.get("source_attempt_id")
                        and attempt_id in {resolution_receipt.get("source_attempt_id"), resolution_receipt.get("local_attempt_id")}):
                    # The current realization was registered and independently
                    # verified after these original execution records. Replaying
                    # their legacy import must not replace its computational
                    # origin with the later local materialization attempt.
                    continue
                version_id = f"{artifact_id}@{attempt_id}"
                signature = _artifact_signature(definition.get("path"))
                provenance["artifacts"].setdefault(version_id, {
                    **definition,
                    "artifact_id": artifact_id,
                    "artifact_version_id": version_id,
                    "producer_action_id": action_id,
                    "producer_attempt_id": attempt_id,
                    "status": "materialized" if signature.get("exists") else "missing",
                    "validation_status": "unknown",
                    "validation_id": "",
                    "created_at": str(execution.get("finished_at") or ""),
                    "size": int(signature.get("size") or 0),
                    "digest_or_signature": signature,
                    "source": "migration_execution_record",
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                })
                provenance["artifact_heads"][artifact_id] = version_id
                if artifact_id not in attempt["produced_artifact_ids"]:
                    attempt["produced_artifact_ids"].append(artifact_id)
                    attempt["artifact_version_ids"].append(version_id)
    provenance["migration_log"].append({
        "migration_id": migration_id,
        "source": "execution_records.jsonl",
        "record_count": len(execution_records or []),
        "recorded_at": _now(),
    })
    provenance["updated_at"] = _now()
    state["provenance_state"] = provenance
    return {"migration_id": migration_id, "applied": True}



def render_structured_plan(state: PlanState) -> str:
    """Render the machine-owned plan without reintroducing planner prose."""
    lines = ["## Execution Checklist"]
    for index, raw in enumerate(state.get("steps", []), 1):
        if not isinstance(raw, dict):
            continue
        step = normalize_plan_step(dict(raw), index)
        mark = _mark_from_status(str(step.get("status") or STATUS_PENDING))
        skills = ",".join(step.get("skill_ids") or [])
        stages = ",".join(step.get("workflow_stage_ids") or []) or "none"
        batch = str(step.get("batch_id") or "none")
        lines.extend([
            f"- [{mark}] [{step.get('step_id')}] {_display_step_title(str(step.get('title') or ''))}",
            f"  - Action: {str(step.get('action') or '').upper()}",
            f"  - Purpose: {str(step.get('purpose') or 'analysis').upper()}",
            f"  - Execution capability: {str(step.get('execution_capability_id') or 'none')}",
            f"  - Operation: {str(step.get('operation') or 'none')}",
            f"  - Skills: [{skills}]",
            "  - Inputs:",
            *([f"    - {item}" for item in step.get("inputs", []) or []] or ["    - []"]),
            "  - Outputs:",
            *([f"    - {item}" for item in step.get("outputs", []) or []] or ["    - []"]),
            "  - Step effect: " + json.dumps(
                step.get("step_effect") or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            f"  - Depends on: [{', '.join(step.get('depends_on') or step.get('dependencies') or [])}]",
            f"  - Success criteria: {'; '.join(step.get('success_criteria') or [])}",
            f"  - Stage: [{stages}]",
            f"  - Batch: {batch}",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _render_output_ownership(state: PlanState) -> List[str]:
    task_spec = ensure_task_spec(_current_task_spec(state))
    outputs = list(task_spec.get("output_contracts") or [])
    output_dir = _canonical_output_dir(outputs, task_spec)
    artifact_bindings = {
        str(item.get("artifact_id") or item.get("output_id") or ""): dict(item)
        for item in outputs
        if isinstance(item, Mapping)
        and str(item.get("artifact_id") or item.get("output_id") or "")
    }
    steps = [
        normalize_plan_step(dict(raw), index)
        for index, raw in enumerate(state.get("steps", []) or [], 1)
        if isinstance(raw, dict)
    ]
    lines: List[str] = []
    for binding in outputs:
        if not isinstance(binding, dict) or str(binding.get("role") or "") == "output_dir":
            continue
        path = str(binding.get("canonical_path") or binding.get("value") or "")
        if not path:
            continue
        identity = canonical_output_identity(binding, output_dir)
        owner = next(
            (
                step
                for step in steps
                if (
                    any(
                        canonical_output_identity(
                            item.get("required_output") or item.get("canonical_path"),
                            output_dir,
                        ) == identity
                        for item in step.get("bound_outputs", []) or []
                        if isinstance(item, dict)
                    )
                    if str(step.get("step_type") or "") == "renderer_composite"
                    else any(
                        canonical_output_identity(
                            artifact_bindings.get(str(item or "")) or item,
                            output_dir,
                        ) == identity
                        for item in step.get("outputs", []) or []
                    )
                )
            ),
            {},
        )
        if owner:
            invocation = dict(owner.get("renderer_invocation_contract") or {})
            renderer_execution = dict(owner.get("renderer_execution") or {})
            renderer_bound = bool(invocation or renderer_execution.get("manifest_path"))
            producer_type = "renderer" if renderer_bound else "plan_step"
            producer_id = str(
                invocation.get("invocation_id")
                or renderer_execution.get("manifest_id")
                or owner.get("step_id")
                or ""
            )
            producer_action = str(owner.get("action") or "").upper()
            if renderer_bound:
                # Internal artifact metadata is descriptive only.  It may
                # identify the producing renderer action after exact output
                # ownership has already been established by ``bound_outputs``;
                # it must never establish ownership by itself.
                artifact = next(
                    (
                        item for item in owner.get("artifacts", []) or []
                        if isinstance(item, dict)
                        and canonical_output_identity(item.get("path"), output_dir) == identity
                    ),
                    {},
                )
                producer_action = str(artifact.get("producer") or producer_action)
        else:
            producer_type = "unassigned"
            producer_id = "none"
            producer_action = "none"
        lines.append(
            f"- `{path}` | producer_type: `{producer_type}` | "
            f"producer_id: `{producer_id}` | action: `{producer_action}`"
        )
    return lines


def render_plan_review_content(state: PlanState, plan_text: str = "") -> str:
    """Render the deterministic approval projection, never Planner prose."""
    identity = plan_identity(state)
    _, next_executable = current_plan_step_with_index(state)
    projection = build_approval_projection(state)
    scope = build_approval_scope(state, projection)
    review_diff = list(state.get("pending_review_diff") or [])
    changed_ids = list(dict.fromkeys(
        str(step_id)
        for item in review_diff if isinstance(item, dict)
        for step_id in item.get("changed_step_ids", []) or []
        if str(step_id)
    ))
    highlighted_lines: List[str] = []
    task_spec = canonical_current_task_spec(_current_task_spec(state))
    for raw in task_spec.get("requirements", []) or []:
        if not isinstance(raw, Mapping):
            continue
        value = raw.get("value")
        if isinstance(value, str):
            text = value
        else:
            text = str(raw.get("source_text") or raw.get("description") or "").strip()
            if not text:
                key = str(raw.get("semantic_key") or raw.get("requirement_id") or "").strip()
                encoded = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                text = f"{key} = {encoded}" if key else encoded
        if not text:
            continue
        importance = re.sub(
            r"[^a-z0-9]+", "_", str(raw.get("strength") or "").lower()
        ).strip("_")
        if importance not in {
            "hard", "required", "preference", "conditional", "advisory",
            "ambiguous",
        }:
            level = re.sub(
                r"[^a-z0-9]+", "_", str(raw.get("level") or "").lower()
            ).strip("_")
            importance = {
                "required": "required",
                "conditional": "conditional",
                "recommended": "preference",
                "optional_validation": "advisory",
                "known_absent": "advisory",
                "unknown": "ambiguous",
            }.get(level, "ambiguous")
        source_refs: List[str] = []
        for ref in (raw.get("source_ref"), raw.get("source_id")):
            ref_text = str(ref or "").strip()
            if ref_text:
                display_ref = (
                    "source-ref-" + hashlib.sha256(
                        ref_text.encode("utf-8")
                    ).hexdigest()[:16]
                    if re.match(
                        r"^(?:sha256:[0-9a-f]{32,}|"
                        r"(?:object|audit|evidence):sha256:[0-9a-f]{16,}|"
                        r"cas:[A-Za-z0-9._:-]+)$",
                        ref_text,
                        flags=re.IGNORECASE,
                    )
                    else ref_text
                )
                if display_ref not in source_refs:
                    source_refs.append(display_ref)
        for provenance in raw.get("provenance", []) or []:
            if not isinstance(provenance, Mapping):
                continue
            ref_text = str(
                provenance.get("source_ref") or provenance.get("source_id") or ""
            ).strip()
            if ref_text:
                display_ref = (
                    "source-ref-" + hashlib.sha256(
                        ref_text.encode("utf-8")
                    ).hexdigest()[:16]
                    if re.match(
                        r"^(?:sha256:[0-9a-f]{32,}|"
                        r"(?:object|audit|evidence):sha256:[0-9a-f]{16,}|"
                        r"cas:[A-Za-z0-9._:-]+)$",
                        ref_text,
                        flags=re.IGNORECASE,
                    )
                    else ref_text
                )
                if display_ref not in source_refs:
                    source_refs.append(display_ref)
        highlighted_lines.append(
            f"- [{importance}] {text} | source_refs: "
            + json.dumps(source_refs, ensure_ascii=False)
        )
    lines = [
        "## Plan Identity",
        f"- Plan ID: `{identity['plan_id']}`",
        f"- Plan Version: `{identity['plan_version']}`",
        f"- Parent Version: `{state.get('parent_plan_version') or 'none'}`",
        f"- Semantic Hash: `{identity['plan_semantic_hash']}`",
        f"- Approval Scope: `{scope['approval_scope_hash']}`",
        f"- Execution Cursor: `{state.get('execution_cursor_step_id') or 'none'}`",
        f"- Next Executable Step: `{(next_executable or {}).get('step_id') or 'none'}`",
        f"- Changed Stable Step IDs: `{', '.join(changed_ids) or 'none'}`",
        "",
        "## Highlighted Constraints",
        *(highlighted_lines or ["- none"]),
        "",
        "## Effective Plan",
    ]
    for step in projection["steps"]:
        lines.extend([
            "",
            f"### [{step['step_id']}] {step['title']}",
            f"- Action: `{step['action']}`",
            f"- Purpose: `{step['purpose']}`",
            f"- Skills: `{', '.join(step['skill_ids']) or 'none'}`",
            f"- Inputs: `{', '.join(step['inputs']) or 'none'}`",
            f"- Outputs: `{', '.join(step['outputs']) or 'none'}`",
            f"- Depends on: `{', '.join(step['depends_on']) or 'none'}`",
            f"- Success criteria: `{'; '.join(step['success_criteria']) or 'none'}`",
        ])
        invocation = dict(step.get("renderer_invocation_contract") or {})
        if invocation:
            lines.extend([
                f"- Renderer arguments: `{json.dumps(invocation.get('arguments') or {}, ensure_ascii=False, sort_keys=True)}`",
                f"- Renderer outdir: `{invocation.get('outdir') or 'none'}`",
                f"- Bound outputs: `{json.dumps(step.get('bound_outputs') or [], ensure_ascii=False, sort_keys=True)}`",
                f"- Renderer manifest: `{invocation.get('manifest_path') or 'none'}`",
            ])
    ownership = _render_output_ownership(state)
    if ownership:
        lines.extend(["", "## Output Ownership", *ownership])
    lines.extend([
        "",
        "## Cross-step Artifact Review",
        "- Does a later step use a file or directory built/generated by an earlier step?",
        "- If yes, is the same explicit artifact present in producer outputs and consumer inputs?",
        "- Is any cross-step state carried only through mktemp, shell variables, cwd assumptions, or prose?",
    ])
    summary = []
    for item in review_diff[:8]:
        if not isinstance(item, dict):
            continue
        reasons = [
            str(reason.get("reason") or reason)
            for reason in item.get("reason", []) or []
            if str(reason)
        ]
        summary.append(
            f"- {item.get('operation') or 'PLAN_CHANGE'}: "
            f"steps={','.join(str(value) for value in item.get('changed_step_ids', []) or []) or 'none'}; "
            f"reason={'; '.join(reasons) or 'not recorded'}"
        )
    if summary:
        lines.extend(["", "## Changes Since Parent Revision", *summary])
    return "\n".join(lines).rstrip() + "\n"


def _plan_defaults(task_id: str, data_path: str | Path | None = None) -> PlanState:
    plan_file = str(default_plan_file(data_path, task_id)) if data_path is not None else ""
    return {
        "task_id": str(task_id or "default"),
        "plan_id": _stable_id("plan", str(task_id or "default")),
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "migration_version": PLAN_MIGRATION_VERSION,
        "plan_contract_version": PLAN_CONTRACT_VERSION,
        "minimum_reader_version": MINIMUM_PLAN_READER_VERSION,
        "migrations_applied": [],
        "created_at": "",
        "updated_at": "",
        "plan_file": plan_file,
        "plan_version": "plan_v1",
        "plan_revision": 1,
        "plan_status": PLAN_STATUS_PENDING_REVIEW,
        "revision_status": "current",
        "approval_status": "awaiting_review",
        "execution_status": "not_started",
        "current_effective_plan_version": "plan_v1",
        "semantic_plan_hash": "",
        "approved_plan_hash": "",
        "approved_plan_revision": 0,
        "approved_task_revision": 0,
        "approval_timestamp": "",
        "approval_record": {},
        "approval_record_ref": "",
        "plan_snapshot_ref": "",
        "current_approval": {},
        "plan_artifacts": {},
        "approval_projection": {},
        "approval_scope": {},
        "validation_issues": [],
        "blocking_validation_issue_fingerprints": [],
        "renderer_projections": [],
        "renderer_projection_issues": [],
        "task_output_materializations": [],
        "validation_convergence_fingerprint": "",
        "validation_convergence_history": [],
        "repair_convergence": {
            "status": "not_validated",
            "cycle_type": "",
            "attempt_count": 0,
            "max_attempts": 6,
            "fingerprint": "",
        },
        "can_request_approval": False,
        "can_generate": False,
        "approval_gate": {
            "validation_complete": False,
            "approval_current": False,
            "can_generate": False,
            "blocking_issue_fingerprints": [],
            "repair_status": "not_validated",
        },
        "generated_from_task_revision": 0,
        "parent_plan_version": "",
        "current_step_id": "",
        "execution_cursor_step_id": "",
        "current_stage_id": "",
        "revision_history": [],
        "stale_step_ids": [],
        "revalidation_required_step_ids": [],
        "revision_budget": {"remaining": 6},
        "selected_skill_ids": [],
        "current_task": {
            "task_spec": None,
            "task_revision": 0,
            "task_semantic_hash": "",
            "task_record_hash": "",
            "canonicalization_version": TASK_CANONICALIZATION_VERSION,
            "created_generation": 0,
            "last_mutation_event_id": "",
            "lifecycle": "uninitialized",
        },
        "conversation_evidence": {"head_ref": "", "turn_count": 0},
        "task_state": {
            "status": TASK_STATUS_RUNNING,
            "planning_failure": {},
            "budgets": {
                "generate_attempts_by_step": {},
                "generate_action_count": 0,
                "generate_action_count_by_step": {},
                "action_format_repair_count": 0,
                "action_format_repair_by_step": {},
                "invalid_action_hashes_by_step": {},
                "execute_attempts_by_step": {},
                "recovery_attempts_by_fingerprint": {},
                "execution_retry_count": 0,
                "script_patch_count": 0,
                "plan_patch_count": 0,
                "stage_rewind_count": 0,
                "upstream_rollback_count": 0,
                "no_op_plan_patch_by_signature": {},
                "diagnostic_probe_count": 0,
                "diagnostic_probes_by_step": {},
                "contract_reconciliation_count": 0,
                "recovery_judge_count": 0,
                "recovery_judge_calls": 0,
                "primary_execution_dispatches": 0,
                "primary_execution_failures": 0,
                "diagnostic_probes_proposed": 0,
                "diagnostic_probes_validated": 0,
                "diagnostic_probes_dispatched": 0,
                "diagnostic_probes_completed": 0,
                "diagnostic_probes_not_executed": 0,
                "preaction_rejections": 0,
                "payload_repairs": 0,
                "external_handoffs_created": 0,
                "external_jobs_submitted": 0,
                "recovery_counter_identities": {},
                "empty_execute_count": 0,
                "same_action_count": 0,
                "same_transition_count": 0,
                "no_progress_count": 0,
                "max_planner_calls_per_epoch": 4,
                "max_candidate_repairs_per_epoch": DEFAULT_CANDIDATE_REPAIRS,
            },
        },
        "failure": {},
        "provenance_state": _provenance_defaults(str(task_id or "default")),
        "steps": [],
    }


def _legacy_plan_was_executing(state: PlanState) -> bool:
    for step in state.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("status") or "").lower() in {STATUS_RUNNING, STATUS_DONE, STATUS_BLOCKED, STATUS_SKIPPED}:
            return True
        for evidence in step.get("evidence", []) or []:
            if isinstance(evidence, dict) and str(evidence.get("event") or "") in {
                "started", "execute_success", "execute_failed", "supervisor_verdict",
                "generate_context_incomplete", "active_skills_derived_from_plan_step",
            }:
                return True
    return False


def _assert_plan_state_reader_compatible(state: Mapping[str, Any]) -> None:
    """Fail closed before migration, canonicalization, dereference, or writes."""
    try:
        source_schema_version = int(state.get("schema_version") or 1)
    except (TypeError, ValueError):
        source_schema_version = 1
    if source_schema_version > PLAN_SCHEMA_VERSION:
        raise ReaderCompatibilityError(
            "PLAN_READER_TOO_OLD: "
            f"schema={source_schema_version}, supported={PLAN_SCHEMA_VERSION}"
        )
    if source_schema_version >= 9:
        if not str(state.get("plan_contract_version") or "") or not str(
            state.get("minimum_reader_version") or ""
        ):
            raise ReaderCompatibilityError(
                "PLAN_READER_COMPATIBILITY_ENVELOPE_MISSING"
            )
        assert_reader_compatible(state)


def _migrate_recovery_transition_lifecycle(state: PlanState) -> bool:
    """Normalize legacy route projections into one scoped lifecycle owner.

    This nested migration is intentionally independent from the Plan schema
    version: incident checkpoints may already be current Plan records while
    still carrying a pre-lifecycle ``CONTINUE -> generate`` recovery payload.
    Immutable legacy payloads remain in decision history; only the explicitly
    active transition is normalized or retired.
    """
    task_state = dict(state.get("task_state") or {})
    recovery = dict(task_state.get("recovery_state") or {})
    try:
        recovery_schema = int(recovery.get("schema_version") or 0)
    except (TypeError, ValueError):
        recovery_schema = 0
    active = dict(recovery.get("active_transition") or {})
    legacy_active_original = (
        deepcopy(active) if active and recovery_schema < 4 else {}
    )
    legacy_candidate: Dict[str, Any] = {}
    if not active and recovery_schema < 4:
        for candidate in (
            recovery.get("last_recovery_decision"),
            recovery.get("decision"),
            task_state.get("last_recovery_decision"),
            state.get("recovery_decision"),
        ):
            if isinstance(candidate, Mapping) and candidate:
                legacy_candidate = deepcopy(dict(candidate))
                break
        if legacy_candidate:
            active = deepcopy(legacy_candidate)

    if not recovery and not active:
        return False

    def _lifecycle_snapshot() -> str:
        # This migration can repair step/task lifecycle fields even when the
        # normalized recovery payload itself is already schema-v4.  Include
        # every field it owns so load_plan_state() persists those repairs.
        return json.dumps(
            {
                "recovery_state": recovery,
                "steps": state.get("steps") or [],
                "task_status": task_state.get("status"),
                "verification_pending": task_state.get("verification_pending"),
                "resume_route_hint": task_state.get("resume_route_hint"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    before = _lifecycle_snapshot()
    recovery["schema_version"] = 4
    recovery.setdefault("decision_history", [])
    recovery.setdefault("transition_history", [])
    recovery.setdefault("decision_audit", [])
    known_applied_ids = [
        str(item)
        for item in recovery.get("applied_decision_ids") or []
        if str(item)
    ]
    for item in (
        *list(recovery.get("decision_history") or []),
        *list(recovery.get("transition_history") or []),
        recovery.get("last_recovery_decision") or {},
        recovery.get("last_consumed_transition") or {},
    ):
        if not isinstance(item, Mapping):
            continue
        known_id = str(item.get("decision_id") or item.get("idempotency_key") or "")
        if known_id and known_id not in known_applied_ids:
            known_applied_ids.append(known_id)
    recovery["applied_decision_ids"] = known_applied_ids

    if not active:
        task_state["recovery_state"] = recovery
        state["task_state"] = task_state
        return before != _lifecycle_snapshot()

    decision_id = str(
        active.get("decision_id") or active.get("idempotency_key") or ""
    ).strip()
    if not decision_id:
        identity = {
            "kind": active.get("kind") or active.get("recommended_action"),
            "step_id": active.get("step_id"),
            "attempt_id": active.get("attempt_id"),
            "source_node": active.get("source_node"),
            "reason_code": active.get("reason_code"),
            "reason": active.get("reason"),
        }
        decision_id = "legacy-recovery-" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]
    active["decision_id"] = decision_id
    active["idempotency_key"] = decision_id
    active.setdefault("status", "pending")
    active.setdefault("step_id", str(state.get("current_step_id") or ""))
    active.setdefault("trigger_event_id", str(active.get("attempt_id") or ""))
    active.setdefault("trigger_node", str(active.get("source_node") or ""))
    active.setdefault("target_route", str(active.get("next_route") or ""))
    active.setdefault("step_attempt", 0)

    legacy_original = legacy_candidate or legacy_active_original
    if legacy_original:
        history_ids = {
            str(item.get("decision_id") or item.get("idempotency_key") or "")
            for item in recovery.get("decision_history", []) or []
            if isinstance(item, Mapping)
        }
        if decision_id not in history_ids:
            original = deepcopy(legacy_original)
            original.setdefault("decision_id", decision_id)
            original.setdefault("idempotency_key", decision_id)
            original["migration_original_payload"] = True
            recovery["decision_history"] = [
                *list(recovery.get("decision_history") or []),
                original,
            ]
        active.setdefault("legacy_payload", deepcopy(legacy_original))

    disposition = str(active.get("disposition") or "").strip().upper()
    if disposition:
        persisted_target = str(
            active.get("target_route") or active.get("next_route") or ""
        ).strip().lower()
        try:
            canonical_target = recovery_route_for_disposition(
                disposition,
                decision_kind=str(active.get("kind") or ""),
                requires_user_input=bool(active.get("requires_user_input")),
            )
        except ValueError:
            route_conflict_original = deepcopy(active)
            active.update({
                "kind": "ESCALATE",
                "disposition": "INTERNAL_PROTOCOL_ERROR",
                "target_route": "supervisor",
                "next_route": "supervisor",
                "reason_code": "RECOVERY_DISPOSITION_INVALID",
                "migration_source": "invalid_disposition_protocol_error",
            })
            canonical_target = "supervisor"
        else:
            route_conflict_original = deepcopy(active) if (
                persisted_target and persisted_target != canonical_target
            ) else {}
            if route_conflict_original:
                active.update({
                    "target_route": canonical_target,
                    "next_route": canonical_target,
                    "route_normalization": "derived_from_typed_disposition",
                    "migration_source": str(
                        active.get("migration_source")
                        or "typed_disposition_route_normalization"
                    ),
                })
        if route_conflict_original:
            active.setdefault("route_conflict_payload", route_conflict_original)
            conflict_ids = {
                str(item.get("decision_id") or item.get("idempotency_key") or "")
                for item in recovery.get("decision_history", []) or []
                if isinstance(item, Mapping)
                and item.get("route_conflict_original_payload")
            }
            if decision_id not in conflict_ids:
                original = deepcopy(route_conflict_original)
                original["route_conflict_original_payload"] = True
                recovery["decision_history"] = [
                    *list(recovery.get("decision_history") or []),
                    original,
                ]
            recovery["decision_audit"] = [
                *list(recovery.get("decision_audit") or []),
                {
                    "decision_id": decision_id,
                    "apply_status": "protocol_normalized",
                    "reason": "RECOVERY_ROUTE_DERIVED_FROM_TYPED_DISPOSITION",
                    "persisted_target_route": persisted_target,
                    "effective_target_route": canonical_target,
                    "recorded_at": _now(),
                },
            ]

    current_step_id = str(state.get("current_step_id") or "")
    current_step = next(
        (
            item for item in state.get("steps", []) or []
            if isinstance(item, dict)
            and str(item.get("step_id") or "") == current_step_id
        ),
        None,
    )
    terminal_task = task_status(state) in TASK_TERMINAL_STATUSES
    expected_step_id = str(active.get("step_id") or "")

    provenance = ensure_provenance_state(state)
    attempts = _ordered_primary_step_attempts(state, current_step_id)
    trigger_event_id = str(
        active.get("trigger_event_id") or active.get("attempt_id") or ""
    )
    trigger_node = str(
        active.get("trigger_node") or active.get("source_node") or ""
    ).lower()
    # Supersession is monotonic, not merely "different ID".  Locate the
    # trigger inside the ordered provenance stream and consider only attempts
    # after it.  If an old checkpoint cannot prove ordering, retaining the
    # pending transition is safer than falsely superseding it with an older
    # success record.
    trigger_index = next(
        (
            index for index, attempt in enumerate(attempts)
            if str(attempt.get("attempt_id") or attempt.get("execution_id") or "")
            == trigger_event_id
        ),
        -1,
    )
    if (
        trigger_node == "execute"
        and trigger_index >= 0
        and _int(active.get("step_attempt"), 0) <= 0
    ):
        active["step_attempt"] = trigger_index + 1
    newer_attempts = attempts[trigger_index + 1:] if trigger_index >= 0 else []
    newer_successful_attempts = []
    for attempt in newer_attempts:
        try:
            returncode = int(attempt.get("returncode"))
        except (TypeError, ValueError):
            returncode = None
        if returncode == 0 and str(attempt.get("status") or "").upper() not in {
            ATTEMPT_FAILED, ATTEMPT_VALIDATION_FAILED,
        }:
            newer_successful_attempts.append(attempt)
    latest_newer_success = (
        newer_successful_attempts[-1] if newer_successful_attempts else {}
    )
    latest_success_id = str(
        latest_newer_success.get("attempt_id")
        or latest_newer_success.get("execution_id")
        or ""
    )
    latest_newer_attempt = newer_attempts[-1] if newer_attempts else {}
    latest_attempt_id = str(
        latest_newer_attempt.get("attempt_id")
        or latest_newer_attempt.get("execution_id")
        or ""
    )

    terminal_status = ""
    terminal_reason = ""
    completion_transition = str(active.get("kind") or "").upper() in {
        "COMPLETE_STEP", "COMPLETE_TASK",
    }
    ordered_steps = [
        item for item in state.get("steps", []) or [] if isinstance(item, Mapping)
    ]
    expected_step_index = next(
        (
            index for index, item in enumerate(ordered_steps)
            if str(item.get("step_id") or "") == expected_step_id
        ),
        -1,
    )
    current_step_index = next(
        (
            index for index, item in enumerate(ordered_steps)
            if str(item.get("step_id") or "") == current_step_id
        ),
        -1,
    )
    expected_step = (
        ordered_steps[expected_step_index] if expected_step_index >= 0 else None
    )
    completion_apply_audited = any(
        isinstance(item, Mapping)
        and str(item.get("decision_id") or "") == decision_id
        and str(item.get("apply_status") or "").lower() == "applied"
        for item in recovery.get("decision_audit", []) or []
    )
    completion_position_valid = bool(
        completion_transition
        and expected_step is not None
        and str(expected_step.get("status") or "") in STEP_SUCCESS_STATUSES
        and (
            (
                str(active.get("kind") or "").upper() == "COMPLETE_STEP"
                and current_step_index in {
                    expected_step_index,
                    expected_step_index + 1,
                }
            )
            or (
                str(active.get("kind") or "").upper() == "COMPLETE_TASK"
                and all(
                    str(item.get("status") or "") in STEP_SUCCESS_STATUSES
                    for item in ordered_steps
                )
            )
        )
    )
    persisted_route_resolution = next(
        (
            dict(item)
            for item in reversed(
                [
                    *list(task_state.get("route_resolution_history") or []),
                    task_state.get("last_route_resolution") or {},
                ]
            )
            if isinstance(item, Mapping)
            and str(item.get("override_decision_id") or "") == decision_id
        ),
        {},
    )
    # Applying COMPLETE_STEP can atomically advance current_step_id before its
    # route event is dispatched.  Preserve only that audited adjacent crash
    # window; a matching persisted route proves dispatch was already committed
    # and must retire the stale active copy instead of replaying it.
    completion_dispatch_pending = bool(
        completion_transition
        and str(active.get("status") or "").lower() == "pending"
        and completion_apply_audited
        and completion_position_valid
        and not persisted_route_resolution
    )
    if persisted_route_resolution:
        terminal_status = "consumed"
        terminal_reason = "ALREADY_DISPATCHED_ROUTE_PERSISTED"
    elif str(active.get("status") or "").lower() in {
        "consumed", "superseded", "expired",
    }:
        terminal_status = str(active.get("status") or "").lower()
        terminal_reason = str(active.get("superseded_reason") or "ALREADY_TERMINAL")
    elif terminal_task and not completion_dispatch_pending:
        terminal_status = "expired"
        terminal_reason = "EXPIRED_TASK_TERMINAL"
    elif (
        expected_step_id
        and current_step_id
        and expected_step_id != current_step_id
        and not completion_dispatch_pending
    ):
        terminal_status = "expired"
        terminal_reason = "EXPIRED_STEP_ADVANCED"
    elif (
        current_step is not None
        and str(current_step.get("status") or "")
        in (STEP_SUCCESS_STATUSES | STEP_TERMINAL_STATUSES)
        and not completion_dispatch_pending
    ):
        terminal_status = "expired"
        terminal_reason = "EXPIRED_STEP_TERMINAL"
    elif (
        trigger_node == "execute"
        and latest_success_id
        and trigger_event_id
        and latest_success_id != trigger_event_id
    ):
        terminal_status = "superseded"
        terminal_reason = "SUPERSEDED_BY_NEWER_EXECUTION"
    elif (
        trigger_node == "execute"
        and latest_attempt_id
        and trigger_event_id
        and latest_attempt_id != trigger_event_id
    ):
        terminal_status = "superseded"
        terminal_reason = "SUPERSEDED_BY_NEWER_STEP_ATTEMPT"

    if terminal_status:
        retired_at = _now()
        retired = deepcopy(active)
        retired.update({
            "status": terminal_status,
            "superseded_reason": terminal_reason,
            f"{terminal_status}_at": retired_at,
        })
        if terminal_status == "consumed":
            retired.update({
                "dispatched_route": str(
                    persisted_route_resolution.get("effective_route") or ""
                ),
                "route_event_id": str(
                    persisted_route_resolution.get("route_event_id") or ""
                ),
            })
        prior_transition_history = list(recovery.get("transition_history") or [])
        if not any(
            isinstance(item, Mapping)
            and str(item.get("decision_id") or item.get("idempotency_key") or "")
            == decision_id
            and str(item.get("status") or "").lower() == terminal_status
            for item in prior_transition_history
        ):
            prior_transition_history.append(retired)
        recovery["transition_history"] = prior_transition_history
        recovery["active_transition"] = {}
        if terminal_status == "consumed":
            recovery["last_consumed_transition"] = deepcopy(retired)
            if not any(
                isinstance(item, Mapping)
                and str(item.get("decision_id") or "") == decision_id
                and str(item.get("apply_status") or "").lower() == "consumed"
                for item in recovery.get("decision_audit", []) or []
            ):
                recovery["decision_audit"] = [
                    *list(recovery.get("decision_audit") or []),
                    {
                        "decision_id": decision_id,
                        "apply_status": "consumed",
                        "recorded_at": retired_at,
                        "route_event_id": retired.get("route_event_id", ""),
                        "effective_route": retired.get("dispatched_route", ""),
                        "reason": terminal_reason,
                    },
                ]
        else:
            recovery["last_ineligible_transition"] = {
                "decision_id": decision_id,
                "status": terminal_status,
                "reason": terminal_reason,
                "step_id": str(active.get("step_id") or ""),
                "trigger_node": str(active.get("trigger_node") or active.get("source_node") or ""),
                "trigger_event_id": str(active.get("trigger_event_id") or active.get("attempt_id") or ""),
                "encountered_at": retired_at,
            }
        last = dict(recovery.get("last_recovery_decision") or active)
        if str(last.get("decision_id") or last.get("idempotency_key") or decision_id) == decision_id:
            last.update({
                "status": terminal_status,
                "superseded_reason": terminal_reason,
                f"{terminal_status}_at": retired[f"{terminal_status}_at"],
            })
            recovery["last_recovery_decision"] = last
        recovery["phase"] = terminal_status
        recovery["status"] = terminal_status.upper()
        if terminal_reason == "SUPERSEDED_BY_NEWER_EXECUTION" and current_step is not None:
            if str(current_step.get("status") or "") not in STEP_SUCCESS_STATUSES | STEP_TERMINAL_STATUSES:
                current_step["status"] = STATUS_VERIFICATION_PENDING
                current_step["verification_attempt_id"] = latest_success_id
                _append_evidence(
                    current_step,
                    "verification_pending",
                    "legacy recovery superseded by newer successful Execute",
                )
                task_state["verification_pending"] = {
                    "step_id": current_step_id,
                    "attempt_id": latest_success_id,
                    "recorded_at": _now(),
                }
                task_state["resume_route_hint"] = "supervisor"
    else:
        legacy_continue = str(active.get("kind") or "").upper() == "CONTINUE"
        accepted_legacy_execute = bool(
            legacy_continue
            and str(active.get("target_route") or active.get("next_route") or "").lower() == "generate"
            and str(active.get("trigger_node") or active.get("source_node") or "").lower() == "execute"
            and str(active.get("recommended_action") or "").upper()
            in {"RECONCILE_AND_CONTINUE", "PASS_WITH_WARNING"}
            and (
                bool(active.get("primary_execution_succeeded"))
                or active.get("error_is_real") is False
            )
        )
        if accepted_legacy_execute:
            active.update({
                "kind": "ACCEPT_EXECUTION_FOR_VERIFICATION",
                "disposition": "ACCEPT_EXECUTION_FOR_VERIFICATION",
                "target_route": "supervisor",
                "next_route": "supervisor",
                "migration_source": "legacy_continue_execute_acceptance",
            })
        elif legacy_continue:
            active.update({
                "kind": "ESCALATE",
                "disposition": "INTERNAL_PROTOCOL_ERROR",
                "target_route": "supervisor",
                "next_route": "supervisor",
                "reason_code": "AMBIGUOUS_LEGACY_CONTINUE",
                "migration_source": "legacy_continue_protocol_error",
            })
        valid_accept_scope = bool(
            str(active.get("kind") or "").upper()
            == "ACCEPT_EXECUTION_FOR_VERIFICATION"
            and str(active.get("disposition") or "").upper()
            == "ACCEPT_EXECUTION_FOR_VERIFICATION"
            and str(active.get("trigger_node") or active.get("source_node") or "").lower()
            == "execute"
            and bool(trigger_event_id)
            and _int(active.get("step_attempt"), 0) > 0
            and str(active.get("target_route") or active.get("next_route") or "").lower()
            == "supervisor"
        )
        if valid_accept_scope and current_step is not None:
            current_step["status"] = STATUS_VERIFICATION_PENDING
            current_step["verification_attempt_id"] = trigger_event_id
            _append_evidence(
                current_step,
                "verification_pending",
                "accepted Execute awaiting Supervisor validation",
            )
            task_state["verification_pending"] = {
                "step_id": current_step_id,
                "attempt_id": trigger_event_id,
                "recorded_at": _now(),
            }
            task_state["resume_route_hint"] = "supervisor"
            task_state["status"] = TASK_STATUS_RUNNING
        active["status"] = "pending"
        recovery["active_transition"] = active
        recovery["last_recovery_decision"] = deepcopy(active)
        recovery["phase"] = "pending_dispatch"
        recovery["status"] = str(active.get("kind") or "PENDING")

    recovery["lifecycle_migrated_at"] = str(
        recovery.get("lifecycle_migrated_at") or _now()
    )
    task_state["recovery_state"] = recovery
    task_state["last_recovery_decision"] = deepcopy(
        recovery.get("last_recovery_decision") or task_state.get("last_recovery_decision") or {}
    )
    state["task_state"] = task_state
    return before != _lifecycle_snapshot()


@timed_phase("state.migration")
def migrate_plan_state(state: PlanState, task_id: str, data_path: str | Path | None = None) -> Tuple[PlanState, List[str]]:
    """Apply idempotent, non-destructive lifecycle/schema migrations."""
    data = state if isinstance(state, dict) else {}
    try:
        source_schema_version = int(data.get("schema_version") or 1)
    except (TypeError, ValueError):
        source_schema_version = 1
    source_compatibility = dict(data.get("compatibility") or {})
    semantic_source_schema_version = _int(
        source_compatibility.get("source_schema_version"),
        source_schema_version,
    )
    try:
        _assert_plan_state_reader_compatible(data)
    except ReaderCompatibilityError as exc:
        data["load_error"] = str(exc)
        data["can_generate"] = False
        data["approval_gate_v2"] = {
            "status": "unverifiable",
            "applicable": False,
            "reason": "reader_incompatible",
        }
        # Reader-first fail closed: do not migrate, normalize, externalize,
        # dereference, or write an unsupported record.
        return data
    legacy_task_spec = data.get("task_spec")
    source_current_task_spec = (
        (data.get("current_task") or {}).get("task_spec")
        if isinstance(data.get("current_task"), Mapping)
        else None
    )
    defaults = _plan_defaults(task_id, data_path)
    applied = list(data.get("migrations_applied") or [])
    newly_applied: List[str] = []
    original_content = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    original_steps = json.dumps(data.get("steps", []), ensure_ascii=False, sort_keys=True, default=str)
    had_plan_status = bool(str(data.get("plan_status") or "").strip())
    prior_migration_version = _int(data.get("migration_version"), 0)

    for key, value in defaults.items():
        if key not in data:
            data[key] = deepcopy(value)
    if (
        source_schema_version < CANONICAL_TASK_SCHEMA_VERSION
        and not task_spec_is_initialized(source_current_task_spec)
        and task_spec_is_initialized(legacy_task_spec)
    ):
        data["current_task"] = _canonical_task_record(
            legacy_task_spec,
            generation=int(data.get("state_generation") or 0),
        )
        newly_applied.append("promote_legacy_taskspec_to_current_task_v8")
    compatibility = dict(data.get("compatibility") or {})
    compatibility.setdefault("source_schema_version", source_schema_version)
    compatibility["read_aliases_enabled"] = source_schema_version < CANONICAL_TASK_SCHEMA_VERSION
    data["compatibility"] = compatibility
    task_state = dict(data.get("task_state") or {})
    task_state.setdefault("status", TASK_STATUS_RUNNING)
    legacy_rejection = dict(task_state.get("planner_candidate_rejection") or {})
    if legacy_rejection and not task_state.get("planning_failure"):
        legacy_status = str(legacy_rejection.get("status") or "rejected")
        task_state["planning_failure"] = {
            "schema_version": PLANNING_FAILURE_SCHEMA_VERSION,
            "status": (
                "terminal"
                if legacy_status == "blocked_non_convergent"
                else "unresolved"
            ),
            "attempt": 1,
            "repeat_count": 0,
            "planning_epoch_id": str(
                (task_state.get("planning_epoch") or {}).get("planning_epoch_id")
                or ""
            ),
            "task_spec_revision": int(
                (data.get("current_task") or {}).get("task_revision")
                or data.get("generated_from_task_revision")
                or 0
            ),
            "validation_source": "legacy_planner_candidate_rejection",
            "validation_issues": [
                _planning_failure_machine_issue(item)
                for item in data.get("validation_issues", []) or []
            ],
            "failure_fingerprint": str(legacy_rejection.get("fingerprint") or ""),
            "previous_correction": {},
            "terminal_diagnostic": deepcopy(dict(
                legacy_rejection.get("terminal_diagnostic") or {}
            )),
            "updated_at": _now(),
        }
        newly_applied.append("planner_failure_owner_convergence")
    task_state.pop("planner_candidate_rejection", None)
    existing_planning_failure = task_state.get("planning_failure")
    if isinstance(existing_planning_failure, Mapping) and existing_planning_failure:
        sanitized_failure = deepcopy(dict(existing_planning_failure))
        sanitized_issues = [
            _planning_failure_machine_issue(item)
            for item in sanitized_failure.get("validation_issues", []) or []
        ]
        if (
            str(sanitized_failure.get("status") or "")
            in {"unresolved", "terminal"}
            and not sanitized_issues
        ):
            sanitized_issues = [_planning_failure_machine_issue({
                "code": "PLAN_CANDIDATE_INVALID",
                "field_path": "$",
                "machine_evidence": {
                    "validation_source": str(
                        sanitized_failure.get("validation_source")
                        or "planner_candidate_admission"
                    ),
                },
            })]
        sanitized_failure["validation_issues"] = sanitized_issues
        sanitized_failure["actual_issues"] = deepcopy(sanitized_issues)
        sanitized_failure["requirements"] = _semantic_diagnostic_value([
            dict(item)
            for item in sanitized_failure.get("requirements", []) or []
            if isinstance(item, Mapping)
        ])
        task_state["planning_failure"] = sanitized_failure
    task_state.setdefault("planning_failure", {})
    budgets = dict(task_state.get("budgets") or {})
    for key, value in defaults["task_state"]["budgets"].items():
        budgets.setdefault(key, deepcopy(value))
    task_state["budgets"] = budgets
    data["task_state"] = task_state
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    data["steps"] = _backfill_step_contracts(
        steps,
        allow_legacy_artifact_inference=(source_schema_version < CANONICAL_TASK_SCHEMA_VERSION),
    )
    if semantic_source_schema_version >= PLAN_SCHEMA_VERSION:
        for step in data["steps"]:
            if not isinstance(step, dict):
                continue
            step.pop("semantic_constraint_ids", None)
            step.pop("semantic_claims", None)
            step.pop("semantic_claims_validation", None)
            step.pop("semantic_claims_invalid_raw", None)
    known_step_ids = {
        str(step.get("step_id") or "") for step in data["steps"] if isinstance(step, dict)
    }
    if str(data.get("current_step_id") or "") not in known_step_ids:
        data["current_step_id"] = ""
    if not str(data.get("current_step_id") or ""):
        _, current = current_plan_step_with_index(data)
        data["current_step_id"] = str(current.get("step_id") or "")
    previous_provenance = json.dumps(data.get("provenance_state", {}), ensure_ascii=False, sort_keys=True, default=str)
    ensure_provenance_state(data)
    if json.dumps(data.get("provenance_state", {}), ensure_ascii=False, sort_keys=True, default=str) != previous_provenance:
        newly_applied.append("initialize_execution_provenance_v1")
    if _migrate_recovery_transition_lifecycle(data):
        newly_applied.append("recovery_transition_lifecycle_v4")

    prior_semantic_hash = str(data.get("semantic_plan_hash") or "")
    prior_approved_hash = str(data.get("approved_plan_hash") or "")
    semantic_hash = semantic_plan_hash(data) if data["steps"] else ""
    data["semantic_plan_hash"] = semantic_hash
    if prior_semantic_hash and prior_semantic_hash != semantic_hash:
        if prior_migration_version < 2:
            newly_applied.append("refresh_semantic_plan_hash_v2")
        if prior_migration_version < 2 and prior_approved_hash == prior_semantic_hash:
            data["approved_plan_hash"] = semantic_hash
        elif prior_approved_hash == prior_semantic_hash:
            data["plan_status"] = PLAN_STATUS_PENDING_REVIEW
            data["approved_plan_hash"] = ""
            data["approved_plan_revision"] = 0
            data["approved_task_revision"] = 0
            data["approval_timestamp"] = ""
    if not had_plan_status:
        data["plan_status"] = PLAN_STATUS_APPROVED if _legacy_plan_was_executing(data) else PLAN_STATUS_PENDING_REVIEW
        newly_applied.append("initialize_plan_lifecycle")
    data.setdefault("revision_status", "current")
    data.setdefault(
        "approval_status",
        "approved" if data.get("plan_status") == PLAN_STATUS_APPROVED else "awaiting_review",
    )
    data.setdefault(
        "execution_status",
        "in_progress" if _legacy_plan_was_executing(data) else "not_started",
    )
    data.setdefault("current_effective_plan_version", str(data.get("plan_version") or "plan_v1"))
    data.setdefault("approval_projection", {})
    data.setdefault("approval_scope", {})
    if data.get("plan_status") == PLAN_STATUS_APPROVED and semantic_hash:
        data.setdefault("approved_plan_hash", semantic_hash)
        if not data.get("approved_plan_hash"):
            data["approved_plan_hash"] = semantic_hash
        if not data.get("approved_plan_revision"):
            data["approved_plan_revision"] = _int(data.get("plan_revision"), 1)
        if not data.get("approved_task_revision"):
            data["approved_task_revision"] = _int(data.get("generated_from_task_revision"), 0)

    if prior_migration_version < 7:
        # Validation, projection completeness, and generation eligibility are
        # derived fields in v7.  Legacy approvals cannot be trusted across this
        # ownership change; they must be deterministically revalidated and then
        # reviewed again rather than being interpreted as executable truth.
        data["validation_issues"] = []
        data["blocking_validation_issue_fingerprints"] = []
        data["renderer_projection_issues"] = []
        data["can_request_approval"] = False
        data["can_generate"] = False
        data["validation_recompute_required"] = True
        data["approval_gate"] = {
            "validation_complete": False,
            "approval_current": False,
            "can_generate": False,
            "blocking_issue_fingerprints": [],
            "repair_status": "migration_revalidation_required",
        }
        data["plan_status"] = PLAN_STATUS_PENDING_REVIEW
        data["approval_status"] = "awaiting_review"
        data["approved_plan_hash"] = ""
        data["approved_plan_revision"] = 0
        data["approved_task_revision"] = 0
        data["approval_timestamp"] = ""
        data["approval_record"] = {}
        data["approval_projection"] = {}
        data["approval_scope"] = {}
        newly_applied.append("recompute_plan_contract_validation_v7")

    if prior_migration_version < 8:
        # V8 introduces logical control-state partitions and dual identity
        # domains.  Legacy aliases remain read-only compatibility inputs for
        # this migration window; no ApprovalSnapshot is fabricated here.
        data.setdefault("state_generation", 0)
        data.setdefault("current_task", {})
        data.setdefault("current_plan", {})
        data.setdefault("plan_validation_state", {})
        data.setdefault("runtime_state", {})
        data.setdefault("approval_snapshot", {})
        data.setdefault("approval_gate_v2", {"status": "missing"})
        newly_applied.append("state_contract_v2_logical_partitions_v8")

    if prior_migration_version < 9:
        # V9 installs the reader-first Plan Contract envelope.  Existing plans
        # are not fabricated into immutable snapshots; the next deterministically
        # valid Plan commit creates the first authoritative snapshot/approval pair.
        data.setdefault("plan_contract_version", PLAN_CONTRACT_VERSION)
        data.setdefault("minimum_reader_version", MINIMUM_PLAN_READER_VERSION)
        data.setdefault("plan_snapshot_ref", "")
        data.setdefault("approval_record_ref", "")
        data.setdefault("current_approval", {})
        data.setdefault("plan_artifacts", {})
        newly_applied.append("plan_contract_reader_envelope_v9")

    if prior_migration_version < 10:
        # V10 retires generic semantic claims/constraint IDs for fresh plans.
        # Historical v9 rows remain readable under compatibility.source_schema_version.
        newly_applied.append("open_world_highlighted_requirements_plan_v10")

    if json.dumps(data["steps"], ensure_ascii=False, sort_keys=True, default=str) != original_steps:
        newly_applied.append("normalize_plan_step_schema")
    data["schema_version"] = PLAN_SCHEMA_VERSION
    data["plan_schema_version"] = PLAN_SCHEMA_VERSION
    data["migration_version"] = PLAN_MIGRATION_VERSION
    for migration in newly_applied:
        if migration not in applied:
            applied.append(migration)
    data["migrations_applied"] = applied
    # Compare the final migration result, including migration metadata. This
    # gate is local to migration; legitimate runtime writes remain unconditional.
    if json.dumps(data, ensure_ascii=False, sort_keys=True, default=str) == original_content:
        return data, []
    if not newly_applied:
        newly_applied.append("normalize_plan_state")
        if "normalize_plan_state" not in applied:
            applied.append("normalize_plan_state")
    return data, newly_applied


def carry_forward_plan_lifecycle(previous: PlanState, candidate: PlanState) -> PlanState:
    """Preserve approval for semantic no-ops; require review for real plan changes."""
    previous, _ = migrate_plan_state(previous, str(previous.get("task_id") or "default"))
    candidate, _ = migrate_plan_state(candidate, str(candidate.get("task_id") or previous.get("task_id") or "default"))
    # A Planner candidate owns PlanSpec only.  Carry the independent durable
    # task/evidence/runtime partitions and CAS generation forward verbatim so
    # creating a plan cannot reset conversation history or canonical state.
    for key in (
        "current_task", "conversation_evidence", "task_state", "audit_index",
        "state_generation", "created_at", "provenance_state",
        "plan_artifacts", "artifact_allocation_mapping",
    ):
        if key in previous:
            candidate[key] = deepcopy(previous[key])
    # The candidate is a fresh Planner-v2/Plan-v10 write.  Historical reader
    # compatibility belongs to the previous snapshot and must not make the new
    # plan re-enter the retired generic semantic-claim validation path.
    candidate["compatibility"] = {
        "source_schema_version": PLAN_SCHEMA_VERSION,
        "read_aliases_enabled": False,
    }
    for step in candidate.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        step.pop("semantic_constraint_ids", None)
        step.pop("semantic_claims", None)
        step.pop("semantic_claims_validation", None)
        step.pop("semantic_claims_invalid_raw", None)
    previous_hash = semantic_plan_hash(previous) if previous.get("steps") else ""
    candidate_hash = semantic_plan_hash(candidate) if candidate.get("steps") else ""
    previous_revision = max(1, _int(previous.get("plan_revision"), 1))
    if previous_hash and previous_hash == candidate_hash:
        candidate["plan_revision"] = previous_revision
        candidate["plan_version"] = str(previous.get("plan_version") or f"plan_v{previous_revision}")
        candidate["current_effective_plan_version"] = candidate["plan_version"]
        previous_steps = {
            str(step.get("step_id") or ""): step
            for step in previous.get("steps", []) or []
            if isinstance(step, dict)
        }
        for step in candidate.get("steps", []) or []:
            old = previous_steps.get(str(step.get("step_id") or ""))
            if not old:
                continue
            for field in ("status", "evidence", "updated_at"):
                if field in old:
                    step[field] = old[field]
        for field in (
            "plan_status",
            "approved_plan_hash",
            "approved_plan_revision",
            "approved_task_revision",
            "approval_timestamp",
            "approval_record",
            "approval_projection",
            "approval_scope",
            "approval_status",
        ):
            candidate[field] = previous.get(field, candidate.get(field))
        candidate["can_request_approval"] = not any(
            isinstance(item, Mapping)
            and str(item.get("severity") or "").lower() in BLOCKING_VALIDATION_SEVERITIES
            for item in candidate.get("validation_issues", []) or []
        )
        candidate["can_generate"] = bool(
            candidate["can_request_approval"]
            and plan_approval_is_current(
                candidate,
                _int(candidate.get("generated_from_task_revision"), 0),
            )
        )
        candidate["approval_gate"] = {
            "validation_complete": bool(candidate["can_request_approval"]),
            "approval_current": bool(candidate["can_generate"]),
            "can_generate": bool(candidate["can_generate"]),
            "blocking_issue_fingerprints": list(candidate.get("blocking_validation_issue_fingerprints") or []),
            "repair_status": str((candidate.get("repair_convergence") or {}).get("status") or "valid"),
        }
    elif previous_hash:
        candidate["plan_revision"] = previous_revision + 1
        candidate["parent_plan_version"] = str(previous.get("plan_version") or f"plan_v{previous_revision}")
        candidate["plan_version"] = f"plan_v{candidate['plan_revision']}"
        candidate["current_effective_plan_version"] = candidate["plan_version"]
        candidate["plan_status"] = PLAN_STATUS_PENDING_REVIEW
        candidate["approved_plan_hash"] = ""
        candidate["approved_plan_revision"] = 0
        candidate["approved_task_revision"] = 0
        candidate["approval_timestamp"] = ""
        candidate["approval_record"] = {}
        candidate["approval_projection"] = {}
        candidate["approval_scope"] = {}
        candidate["approval_status"] = "awaiting_review"
        candidate["can_generate"] = False
        candidate["revision_status"] = "current"
        candidate["current_effective_plan_version"] = str(candidate.get("plan_version") or "")
    candidate["semantic_plan_hash"] = candidate_hash
    return candidate


def _completed_execution_step(step: Mapping[str, Any]) -> bool:
    status = str(step.get("status") or "").strip().lower()
    if status in {"done", "completed", "passed", "skipped"}:
        return True
    return any(
        isinstance(row, Mapping)
        and str(row.get("event") or "") in {
            "execute_success", "validation_passed", "evidence_committed",
        }
        for row in step.get("evidence", []) or []
    )


def _step_machine_transition_signature(
    state: Mapping[str, Any],
    step: Mapping[str, Any],
) -> Dict[str, Any]:
    step_id = str(step.get("step_id") or "")
    usage = [
        {
            "relation": str(row.get("relation") or ""),
            "artifact_ref": deepcopy(dict(row.get("artifact_ref") or {})),
        }
        for row in state.get("artifact_usage", []) or []
        if isinstance(row, Mapping)
        and str(row.get("step_id") or "") == step_id
    ]
    usage.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return {
        "step_id": step_id,
        "typed_artifact_usage": usage,
        "inputs": [] if usage else sorted(str(value) for value in step.get("inputs", []) or []),
        "outputs": [] if usage else sorted(str(value) for value in step.get("outputs", []) or []),
        "dependencies": sorted(
            str(value)
            for value in (step.get("dependencies") or step.get("depends_on") or [])
        ),
    }


def full_replan_transition_issues(
    previous: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Protect executed machine history without comparing natural language."""
    previous_steps = {
        str(step.get("step_id") or ""): dict(step)
        for step in previous.get("steps", []) or []
        if isinstance(step, Mapping) and _completed_execution_step(step)
    }
    candidate_steps = {
        str(step.get("step_id") or ""): dict(step)
        for step in candidate.get("steps", []) or []
        if isinstance(step, Mapping)
    }
    issues: List[Dict[str, Any]] = []
    for step_id, old_step in previous_steps.items():
        new_step = candidate_steps.get(step_id)
        old_signature = _step_machine_transition_signature(previous, old_step)
        if new_step is None:
            issues.append(validation_issue(
                "COMPLETED_PLAN_STEP_REMOVED",
                owner="planner",
                repair_target=f"steps[{step_id}]",
                field_path=f"$.steps[{step_id}]",
                message="Completed execution step is absent from the replacement candidate.",
                affected_step_ids=[step_id],
                machine_evidence={
                    "expected": old_signature,
                    "observed": {"step_id": step_id, "present": False},
                },
            ))
            continue
        new_signature = _step_machine_transition_signature(candidate, new_step)
        if old_signature != new_signature:
            issues.append(validation_issue(
                "COMPLETED_PLAN_STEP_IDENTITY_CHANGED",
                owner="planner",
                repair_target=f"steps[{step_id}]",
                field_path=f"$.steps[{step_id}]",
                message="Completed execution step changed a protected machine identity field.",
                affected_step_ids=[step_id],
                expected_identity=old_signature,
                observed_identity=new_signature,
                machine_evidence={
                    "compared_fields": [
                        "step_id", "typed inputs", "typed outputs", "artifact identity",
                        "dependency identity",
                    ],
                },
            ))
        candidate_evidence = list(new_step.get("evidence") or [])
        if candidate_evidence and candidate_evidence != list(old_step.get("evidence") or []):
            issues.append(validation_issue(
                "COMPLETED_PLAN_EXECUTION_EVIDENCE_CHANGED",
                owner="planner",
                repair_target=f"steps[{step_id}].evidence",
                field_path=f"$.steps[{step_id}].evidence",
                message="Planner candidate attempted to replace completed execution evidence.",
                affected_step_ids=[step_id],
                machine_evidence={
                    "expected_evidence_count": len(old_step.get("evidence") or []),
                    "observed_evidence_count": len(candidate_evidence),
                },
            ))
    return issues


def carry_forward_completed_execution_history(
    previous: PlanState,
    candidate: PlanState,
) -> PlanState:
    """Carry Controller-owned status/evidence after the transition gate passes."""
    issues = full_replan_transition_issues(previous, candidate)
    if issues:
        return derive_plan_validation_state(candidate, issues, record_convergence=False)
    result = deepcopy(candidate)
    old_steps = {
        str(step.get("step_id") or ""): step
        for step in previous.get("steps", []) or []
        if isinstance(step, Mapping) and _completed_execution_step(step)
    }
    for step in result.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        old = old_steps.get(str(step.get("step_id") or ""))
        if not old:
            continue
        for field in ("status", "evidence", "updated_at"):
            if field in old:
                step[field] = deepcopy(old[field])
    return result


def stable_plan_revision_delta(previous: PlanState, candidate: PlanState) -> Dict[str, List[str]]:
    """Classify revision changes by stable step ID without positional inference."""
    old_steps = {
        str(step.get("step_id") or ""): _semantic_step(step)
        for step in previous.get("steps", []) or [] if isinstance(step, dict) and step.get("step_id")
    }
    new_steps = {
        str(step.get("step_id") or ""): _semantic_step(step)
        for step in candidate.get("steps", []) or [] if isinstance(step, dict) and step.get("step_id")
    }
    collapsed = {
        str(step_id)
        for audit in candidate.get("normalization_audit", []) or [] if isinstance(audit, dict)
        for step_id in audit.get("removed_step_ids", []) or []
        if str(audit.get("status") or "") == "collapsed"
    }
    removed = set(old_steps) - set(new_steps)
    return {
        "added_step_ids": sorted(set(new_steps) - set(old_steps)),
        "changed_step_ids": sorted(
            step_id for step_id in set(old_steps) & set(new_steps)
            if old_steps[step_id] != new_steps[step_id]
        ),
        "collapsed_step_ids": sorted(removed & collapsed),
        "removed_step_ids": sorted(removed - collapsed),
        "tombstoned_step_ids": sorted(removed - collapsed),
    }


def plan_identity(state: PlanState) -> Dict[str, str]:
    """Return the immutable identity shared by review, approval, and Generate."""
    return {
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or "plan_v1"),
        "plan_semantic_hash": semantic_plan_hash(state),
    }


def _approval_task_spec_view(state: PlanState) -> Dict[str, Any]:
    spec = ensure_task_spec(_current_task_spec(state))
    binding_fields = (
        "artifact_id", "semantic_handle", "direction", "role", "format", "path_policy",
        "canonical_path", "required_basename", "required", "validation_contract_id",
        "value", "level", "condition", "semantic_key", "operator", "strength", "confidence",
        "ambiguity", "needs_clarification", "source_type", "source_ref", "source_id", "provenance",
    )

    def compact(values: Any) -> List[Dict[str, Any]]:
        return [
            {key: deepcopy(item.get(key)) for key in binding_fields if item.get(key) not in (None, "", [], {})}
            for item in values or [] if isinstance(item, dict)
        ]

    return {
        "goal": str(spec.get("goal") or ""),
        "revision": _int(spec.get("revision"), 0),
        "inputs": compact(spec.get("inputs")),
        "requirements": compact(spec.get("requirements")),
        "output_contracts": compact(spec.get("output_contracts")),
    }


def _approval_output_ownership(state: PlanState) -> List[Dict[str, Any]]:
    owners: Dict[str, List[Dict[str, str]]] = {}
    task_spec = _approval_task_spec_view(state)
    output_dir = _canonical_output_dir(task_spec.get("output_contracts") or [])
    artifact_bindings = {
        str(item.get("artifact_id") or item.get("output_id") or ""): dict(item)
        for item in task_spec.get("output_contracts") or []
        if isinstance(item, Mapping)
        and str(item.get("artifact_id") or item.get("output_id") or "")
    }
    for index, raw in enumerate(state.get("steps", []) or [], 1):
        if not isinstance(raw, dict):
            continue
        step = normalize_plan_step(dict(raw), index)
        renderer_execution = dict(step.get("renderer_execution") or {})
        invocation = dict(step.get("renderer_invocation_contract") or {})
        renderer_bound = bool(invocation or renderer_execution.get("manifest_path"))
        producer_type = "renderer" if renderer_bound else "plan_step"
        producer_id = str(
            invocation.get("invocation_id")
            or renderer_execution.get("manifest_id")
            or step.get("step_id")
        )
        renderer_step = str(step.get("step_type") or "") == "renderer_composite"
        values = (
            [item.get("required_output") or item.get("canonical_path") for item in step.get("bound_outputs", []) or []]
            if renderer_step
            else list(step.get("outputs", []) or [])
        )
        for value in values:
            identity = _output_identity(
                artifact_bindings.get(str(value or "")) or value,
                output_dir,
            )
            if identity:
                owners.setdefault(identity, []).append({
                    "producer_type": producer_type,
                    "producer_id": producer_id,
                    "step_id": str(step.get("step_id") or ""),
                    "action": str(step.get("action") or "").upper(),
                })
    result: List[Dict[str, Any]] = []
    for output in task_spec["output_contracts"]:
        identity = _output_identity(output, output_dir)
        if not identity or str(output.get("role") or "") == "output_dir":
            continue
        result.append({
            "output_id": str(output.get("artifact_id") or output.get("output_id") or identity),
            "path_or_name": str(output.get("path_or_name") or output.get("path") or output.get("filename") or ""),
            "required": bool(output.get("required", True)),
            "producers": owners.get(identity, []),
        })
    return result


def build_approval_projection(state: PlanState) -> Dict[str, Any]:
    """Build the exact controller-owned view a human approves."""
    steps: List[Dict[str, Any]] = []
    for index, raw in enumerate(state.get("steps", []) or [], 1):
        if not isinstance(raw, dict):
            continue
        step = normalize_plan_step(dict(raw), index)
        steps.append({
            "step_id": str(step.get("step_id") or ""),
            "title": _display_step_title(str(step.get("title") or "")),
            "action": str(step.get("action") or "").upper(),
            "purpose": str(step.get("purpose") or ""),
            "execution_capability_id": str(
                step.get("execution_capability_id") or ""
            ),
            "capability_contract_hash": str(
                step.get("capability_contract_hash") or ""
            ),
            **({"generate_binding_input_contract": deepcopy(step["generate_binding_input_contract"])}
               if step.get("generate_binding_input_contract") else {}),
            "skill_ids": list(step.get("skill_ids") or []),
            "inputs": list(step.get("inputs") or []),
            "outputs": list(step.get("outputs") or []),
            **(
                {"operation": str(step.get("operation") or "")}
                if str(step.get("operation") or "") else {}
            ),
            **(
                {"step_effect": deepcopy(dict(step.get("step_effect") or {}))}
                if isinstance(step.get("step_effect"), Mapping)
                and step.get("step_effect") else {}
            ),
            "depends_on": list(step.get("depends_on") or step.get("dependencies") or []),
            "success_criteria": list(step.get("success_criteria") or []),
            "step_type": str(step.get("step_type") or "atomic"),
            "renderer_id": str(step.get("renderer_id") or ""),
            "bound_outputs": deepcopy(list(step.get("bound_outputs") or [])),
            "renderer_invocation_contract": deepcopy(
                dict(step.get("renderer_invocation_contract") or {})
            ),
        })
    return {
        "schema_version": 2,
        "task_id": str(state.get("task_id") or ""),
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or "plan_v1"),
        "steps": steps,
        "output_ownership": _approval_output_ownership(state),
    }


def build_approval_scope(
    state: PlanState,
    approval_projection: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Hash every semantic input that can change the approved execution."""
    projection = deepcopy(approval_projection or build_approval_projection(state))
    task_spec_view = _approval_task_spec_view(state)
    renderer_contracts = [
        {
            "step_id": str(step.get("step_id") or ""),
            "renderer_id": str(step.get("renderer_id") or ""),
            "inputs": list(step.get("inputs") or []),
            "outputs": list(step.get("outputs") or []),
            "bound_outputs": deepcopy(list(step.get("bound_outputs") or [])),
            "renderer_invocation_contract": deepcopy(
                dict(step.get("renderer_invocation_contract") or {})
            ),
        }
        for step in state.get("steps", []) or [] if isinstance(step, dict)
        if step.get("renderer_id") or step.get("renderer_invocation_contract")
    ]
    scope = {
        "task_hash": _stable_payload_hash({"task_id": state.get("task_id"), "goal": task_spec_view.get("goal")}),
        "task_revision": str(task_spec_view.get("revision") or state.get("generated_from_task_revision") or 0),
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or "plan_v1"),
        "plan_hash": semantic_plan_hash(state),
        "task_spec_hash": _stable_payload_hash(task_spec_view),
        "canonical_constraint_hash": str(
            state.get("canonical_constraint_hash")
            or canonical_constraint_hash(_current_task_spec(state))
        ),
        "claim_set_hash": str(state.get("knowledge_claim_set_hash") or ""),
        "input_bindings_hash": _stable_payload_hash(task_spec_view.get("inputs") or []),
        "renderer_invocation_hash": _stable_payload_hash(renderer_contracts),
        "selected_skills_hash": _stable_payload_hash(sorted(str(item) for item in state.get("selected_skill_ids", []) or [])),
        "projection_hash": _stable_payload_hash(projection),
        "projection_version": str(projection.get("schema_version") or 1),
    }
    scope["approval_scope_hash"] = _stable_payload_hash(scope)
    return scope


def approve_plan_state(
    state: PlanState,
    task_revision: int,
    *,
    approval_source: str = "human_plan_review",
    review_event_id: str = "",
    approval_projection: Optional[Dict[str, Any]] = None,
) -> PlanState:
    _normalize_in_memory_task_record(state)
    state, _ = migrate_plan_state(state, str(state.get("task_id") or "default"))
    refresh_state_contract_v2(state)
    blocking = [
        dict(item) for item in state.get("validation_issues", []) or []
        if isinstance(item, Mapping)
        and str(item.get("severity") or "").lower() in BLOCKING_VALIDATION_SEVERITIES
    ]
    non_complete_projections = [
        dict(item) for item in state.get("renderer_projections", []) or []
        if isinstance(item, Mapping)
        and str(item.get("status") or "") != PROJECTION_COMPLETE
    ]
    if blocking or non_complete_projections or str((state.get("repair_convergence") or {}).get("status") or "") == "blocked_non_convergent":
        raise ValueError(
            "PLAN_APPROVAL_BLOCKED: deterministic plan validation has unresolved blocking issues"
        )
    current_hash = semantic_plan_hash(state)
    state["semantic_plan_hash"] = current_hash
    state["plan_status"] = PLAN_STATUS_APPROVED
    state["approved_plan_hash"] = current_hash
    state["approved_plan_revision"] = _int(state.get("plan_revision"), 1)
    state["approved_task_revision"] = max(0, _int(task_revision, 0))
    state["approval_timestamp"] = _now()
    identity = plan_identity(state)
    projection = deepcopy(approval_projection or build_approval_projection(state))
    scope = build_approval_scope(state, projection)
    state["approval_projection"] = projection
    state["approval_scope"] = scope
    state["approved_canonical_constraint_hash"] = scope["canonical_constraint_hash"]
    state["approved_claim_set_hash"] = scope["claim_set_hash"]
    state["approval_status"] = "approved"
    state["revision_status"] = "current"
    state["current_effective_plan_version"] = identity["plan_version"]
    approval_seed = "|".join([
        str(state.get("task_id") or ""), identity["plan_id"],
        identity["plan_version"], identity["plan_semantic_hash"],
        state["approval_timestamp"], str(review_event_id or ""),
    ])
    state["approval_record"] = {
        "schema_version": 1,
        "plan_contract_version": PLAN_CONTRACT_VERSION,
        "minimum_reader_version": minimum_reader_for_plan(state),
        "approval_id": "approval-" + hashlib.sha256(approval_seed.encode("utf-8")).hexdigest()[:20],
        "plan_snapshot_id": str(
            (state.get("current_plan") or {}).get("plan_snapshot_id") or ""
        ),
        "plan_snapshot_ref": str(
            (state.get("current_plan") or {}).get("plan_snapshot_ref")
            or state.get("plan_snapshot_ref")
            or ""
        ),
        "status": "approved",
        "task_id": str(state.get("task_id") or ""),
        "approved_plan_id": identity["plan_id"],
        "approved_plan_version": identity["plan_version"],
        "approved_plan_semantic_hash": identity["plan_semantic_hash"],
        "approved_at": state["approval_timestamp"],
        "created_at": str(
            (state.get("approval_record") or {}).get("created_at")
            or state["approval_timestamp"]
        ),
        "decided_at": state["approval_timestamp"],
        "decision_source": str(approval_source or "human_plan_review"),
        "approval_source": str(approval_source or "human_plan_review"),
        "review_event_id": str(review_event_id or ""),
        "approval_scope_hash": scope["approval_scope_hash"],
        "approval_scope": deepcopy(scope),
        "projection_hash": scope["projection_hash"],
        "canonical_constraint_hash": scope["canonical_constraint_hash"],
        "claim_set_hash": scope["claim_set_hash"],
    }
    task_identity = dict(state.get("current_task") or {})
    plan_identity_v2 = dict(state.get("current_plan") or {})
    if task_identity.get("task_spec"):
        state["approval_snapshot"] = {
            "approval_id": state["approval_record"]["approval_id"],
            "decision": "approved",
            "approved_task_revision": int(task_identity.get("task_revision") or task_revision or 0),
            "approved_task_semantic_hash": str(task_identity.get("task_semantic_hash") or ""),
            "approved_task_record_hash": str(task_identity.get("task_record_hash") or ""),
            "approved_plan_revision": int(plan_identity_v2.get("plan_revision") or state.get("plan_revision") or 1),
            "approved_plan_semantic_hash": str(plan_identity_v2.get("plan_semantic_hash") or ""),
            "approved_plan_record_hash": str(plan_identity_v2.get("plan_record_hash") or ""),
            "approval_scope": {"mode": "full", "scope_hash": None},
            "diagnostic_fingerprints": {
                "execution_contract_hashes": sorted({
                    str((step.get("renderer_invocation_contract") or {}).get("contract_hash") or "")
                    for step in state.get("steps", []) or []
                    if isinstance(step, Mapping)
                    and str((step.get("renderer_invocation_contract") or {}).get("contract_hash") or "")
                }),
            },
            "created_generation": int(state.get("state_generation") or 0),
            "approved_at": state["approval_timestamp"],
            "approval_source": str(approval_source or "human_plan_review"),
            "review_event_id": str(review_event_id or ""),
        }
    state["pending_review_diff"] = []
    state["validation_recompute_required"] = False
    approved_plan_semantic_hash = "sha256:" + current_hash.removeprefix("sha256:")
    state["plan_validation_state"] = {
        **dict(state.get("plan_validation_state") or {}),
        "status": "valid",
        "validated_plan_semantic_hash": approved_plan_semantic_hash,
        "validator_version": "plan-validator-v2",
        "violations": [],
        "validated_at": state["approval_timestamp"],
    }
    state["can_request_approval"] = True
    state["can_generate"] = True
    state["approval_gate"] = {
        "validation_complete": True,
        "approval_current": True,
        "can_generate": True,
        "blocking_issue_fingerprints": [],
        "repair_status": "valid",
    }
    state["approval_gate_v2"] = approval_applicability_v2(state)
    return state


def transition_plan_approval_state(
    state: PlanState,
    status: str,
    *,
    decision_source: str,
    review_event_id: str = "",
) -> PlanState:
    """Create the next immutable approval decision for the current snapshot."""
    normalized = str(status or "").strip().lower()
    if normalized not in {"changes_requested", "rejected", "superseded"}:
        raise ValueError(f"PLAN_APPROVAL_TRANSITION_INVALID: {status}")
    snapshot_ref = str(
        (state.get("current_plan") or {}).get("plan_snapshot_ref")
        or state.get("plan_snapshot_ref")
        or ""
    )
    if not snapshot_ref:
        # Legacy plans have no immutable v1.2.1 approval lineage. They remain
        # non-executable, but the transition is retained in the compatibility
        # record until a new snapshot is committed.
        snapshot_id = ""
    else:
        snapshot_id = str(
            (state.get("current_plan") or {}).get("plan_snapshot_id") or ""
        )
    decided_at = _now()
    prior = dict(state.get("approval_record") or {})
    approval_id = "approval-" + _stable_payload_hash({
        "plan_snapshot_ref": snapshot_ref,
        "prior_approval_id": str(prior.get("approval_id") or ""),
        "status": normalized,
        "review_event_id": str(review_event_id or ""),
        "decided_at": decided_at,
    })[:24]
    state["approval_record"] = {
        "schema_version": 1,
        "plan_contract_version": PLAN_CONTRACT_VERSION,
        "minimum_reader_version": MINIMUM_PLAN_READER_VERSION,
        "approval_id": approval_id,
        "plan_snapshot_id": snapshot_id,
        "plan_snapshot_ref": snapshot_ref,
        "status": normalized,
        "decision_source": str(decision_source or "human_plan_review"),
        "review_event_id": str(review_event_id or ""),
        "approved_task_revision": None,
        "created_at": str(prior.get("created_at") or decided_at),
        "decided_at": decided_at,
    }
    state["approval_status"] = normalized
    state["plan_status"] = PLAN_STATUS_PENDING_REVIEW
    state["approved_plan_hash"] = ""
    state["approved_plan_revision"] = 0
    state["approved_task_revision"] = 0
    state["approval_timestamp"] = ""
    state["approval_snapshot"] = {}
    state.pop("approval_snapshot_ref", None)
    state["can_generate"] = False
    state["approval_gate_v2"] = {
        "status": normalized,
        "applicable": False,
    }
    return state


def approval_semantic_delta(
    state: PlanState,
    *,
    current_constraint_hash: str,
    current_claim_set_hash: str,
) -> Dict[str, Any]:
    """Compare current typed meaning with the human-approved snapshot."""
    scope = dict((state.get("approval_record") or {}).get("approval_scope") or state.get("approval_scope") or {})
    reasons: List[str] = []
    if not scope:
        reasons.append("approval_scope_missing")
    if str(scope.get("canonical_constraint_hash") or "") != str(current_constraint_hash or ""):
        reasons.append("canonical_constraints_changed")
    if str(scope.get("claim_set_hash") or "") != str(current_claim_set_hash or ""):
        reasons.append("semantic_claim_set_changed")
    if str(scope.get("renderer_invocation_hash") or "") != build_approval_scope(state).get("renderer_invocation_hash"):
        reasons.append("renderer_projection_changed")
    return {"changed": bool(reasons), "reasons": reasons}


def plan_approval_is_current(state: PlanState, task_revision: int) -> bool:
    if isinstance(state.get("approval_snapshot"), Mapping) and state.get("approval_snapshot"):
        return bool(approval_applicability_v2(state).get("applicable"))
    if str(state.get("plan_status") or "") != PLAN_STATUS_APPROVED:
        return False
    legacy_current = (
        str(state.get("approved_plan_hash") or "") == semantic_plan_hash(state)
        and _int(state.get("approved_plan_revision"), 0) == _int(state.get("plan_revision"), 1)
        and _int(state.get("approved_task_revision"), 0) == _int(task_revision, 0)
    )
    if not legacy_current:
        return False
    record = state.get("approval_record") or {}
    if not record:
        return True
    identity = plan_identity(state)
    recorded_scope = str(record.get("approval_scope_hash") or "")
    if recorded_scope and recorded_scope != build_approval_scope(state).get("approval_scope_hash"):
        return False
    return (
        str(record.get("approved_plan_id") or "") == identity["plan_id"]
        and str(record.get("approved_plan_version") or "") == identity["plan_version"]
        and str(record.get("approved_plan_semantic_hash") or "") == identity["plan_semantic_hash"]
    )


def validate_action_approval_lineage(
    state: PlanState,
    action: Dict[str, Any],
    task_revision: int,
) -> str:
    """Reject actions not generated from the currently approved semantic scope."""
    if not plan_approval_is_current(state, task_revision):
        return "The plan approval scope is stale or missing."
    scope = build_approval_scope(state)
    expected = {
        "generated_from_plan_hash": scope["plan_hash"],
        "generated_from_approval_scope_hash": scope["approval_scope_hash"],
    }
    missing = [key for key in expected if not str(action.get(key) or "")]
    if missing:
        return "Pending action is missing approval lineage: " + ", ".join(missing)
    mismatches = [key for key, value in expected.items() if str(action.get(key)) != str(value)]
    if mismatches:
        return "Pending action approval scope is stale: " + ", ".join(mismatches)
    return ""


def plan_contract_violations(state: PlanState) -> List[Dict[str, str]]:
    """Return exact machine-contract violations without checking approval state."""
    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    if not steps:
        return [{"step_id": "", "field": "steps", "reason": "Plan has no executable steps."}]
    violations: List[Dict[str, str]] = []
    compatibility = dict(state.get("compatibility") or {})
    legacy_plan_semantics = _int(
        compatibility.get("source_schema_version") or state.get("schema_version"),
        PLAN_SCHEMA_VERSION,
    ) < PLAN_SCHEMA_VERSION
    for index, raw_step in enumerate(steps):
        raw_action = str(raw_step.get("action") or "").strip().lower()
        if raw_action == "search" and step_requires_execution_evidence(raw_step):
            violations.append({
                "violation_code": "INVALID_ACTION_FOR_STEP_PURPOSE",
                "step_id": str(raw_step.get("step_id") or ""),
                "field": "action",
                "reason": "SEARCH is knowledge-base retrieval only; local file validation requires a read-only EXECUTE action.",
            })
        step = normalize_plan_step(dict(raw_step), index + 1)
        step_id = str(step.get("step_id") or "")
        claim_validation = dict(step.get("semantic_claims_validation") or {})
        if legacy_plan_semantics and claim_validation.get("valid") is False:
            violations.append({
                "violation_code": "SEMANTIC_CLAIMS_INVALID",
                "step_id": step_id,
                "field": "semantic_claims",
                "owner": "planner",
                "repair_target": f"plan.steps[{step_id}].semantic_constraint_ids",
                "reason": "Planner semantic claims failed deterministic schema validation.",
                "evidence": deepcopy(list(claim_validation.get("errors") or [])),
            })
        if not str(step.get("action") or "").strip():
            violations.append({
                "step_id": step_id,
                "field": "action",
                "reason": f"steps[{index}].action is required.",
            })
        if not list(step.get("success_criteria") or []):
            violations.append({
                "step_id": step_id,
                "field": "success_criteria",
                "reason": f"steps[{index}].success_criteria is required.",
            })
    task_spec = canonical_current_task_spec(_current_task_spec(state))
    task_output_bindings = {
        str(item.get("artifact_id") or item.get("output_id") or ""): dict(item)
        for item in task_spec.get("output_contracts") or []
        if isinstance(item, Mapping)
        and str(item.get("artifact_id") or item.get("output_id") or "")
    }
    task_input_bindings = {
        str(item.get("artifact_id") or item.get("input_id") or ""): dict(item)
        for item in task_spec.get("inputs") or []
        if isinstance(item, Mapping)
        and str(item.get("artifact_id") or item.get("input_id") or "")
    }
    # Step usage and artifact-reference validity are owned by the typed
    # ArtifactRefKey/DAG validator before commit.  This compatibility
    # validator must not reconstruct consume/produce semantics from whether a
    # value resembles a task input, task output, filename, or ID prefix.
    if task_spec and legacy_plan_semantics:
        plan_claims: List[Dict[str, Any]] = []
        all_inputs: set[str] = set()
        all_outputs: set[str] = set()
        all_skills: set[str] = set()
        for index, raw_step in enumerate(steps, 1):
            step = normalize_plan_step(dict(raw_step), index)
            all_inputs.update(str(item) for item in step.get("inputs") or [])
            all_outputs.update(str(item) for item in step.get("outputs") or [])
            all_skills.update(str(item) for item in step.get("skill_ids") or [])
            plan_claims.extend(dict(item) for item in step.get("semantic_claims") or [] if isinstance(item, Mapping))
            invocation = dict(step.get("renderer_invocation_contract") or {})
            plan_claims.extend(dict(item) for item in invocation.get("semantic_claims") or [] if isinstance(item, Mapping))
        for constraint in semantic_constraints_from_task_spec(task_spec):
            if not constraint_requires_plan_projection(constraint):
                continue
            semantic_key = str(constraint.get("semantic_key") or "")
            category = semantic_key.split(".", 1)[0]
            if category == "required_output":
                # OutputContract validation below is the single authoritative
                # producer/ownership gate; do not emit a duplicate generic
                # semantic-coverage violation for the same artifact.
                continue
            value = constraint.get("value")
            covered = any(
                str(item.get("semantic_key") or "") == semantic_key
                and item.get("value") == value
                for item in plan_claims
            )
            if category == "required_input":
                matching_ids = {
                    artifact_id
                    for artifact_id, binding in task_input_bindings.items()
                    if str(
                        binding.get("canonical_path")
                        or binding.get("path")
                        or binding.get("value")
                        or ""
                    ) == str(value)
                }
                covered = covered or str(value) in all_inputs or bool(
                    matching_ids.intersection(all_inputs)
                )
            elif category == "required_output":
                matching_ids = {
                    artifact_id
                    for artifact_id, binding in task_output_bindings.items()
                    if str(
                        binding.get("canonical_path")
                        or binding.get("path")
                        or binding.get("value")
                        or ""
                    ) == str(value)
                }
                covered = covered or str(value) in all_outputs or bool(
                    matching_ids.intersection(all_outputs)
                )
            elif category == "required_skill":
                covered = covered or str(value) in all_skills
            if not covered:
                violations.append({
                    "violation_code": "SEMANTIC_CONSTRAINT_UNCOVERED",
                    "step_id": "",
                    "field": "semantic_claims",
                    "reason": f"Hard semantic constraint `{semantic_key}` is not projected by any plan step.",
                })
    return violations


def _output_path_or_name(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("canonical_path")
            or value.get("resolved_path")
            or value.get("path_or_name")
            or value.get("path")
            or value.get("filename")
            or value.get("value")
            or value.get("artifact_id")
            or value.get("output_id")
        )
    return str(value or "").strip().strip("`'\"")


def canonical_output_identity(value: Any, canonical_output_dir: str | Path = "") -> str:
    """Return a POSIX-safe lexical identity for a planned output path.

    Relative paths are rooted in the TaskSpec output directory.  No file must
    exist and case is preserved, so same basenames in different directories do
    not collapse into one logical output.
    """
    text = _output_path_or_name(value)
    if not text:
        return ""
    path = os.path.expanduser(text)
    base = os.path.expanduser(str(canonical_output_dir or "").strip())
    if not os.path.isabs(path):
        path = os.path.join(base or os.getcwd(), path)
    return os.path.abspath(os.path.normpath(path))


def _output_identity(value: Any, canonical_output_dir: str | Path = "") -> str:
    """Compatibility wrapper for the canonical path identity."""
    return canonical_output_identity(value, canonical_output_dir)


def _canonical_output_dir(
    output_contracts: List[Dict[str, Any]] | None = None,
    task_spec: Optional[Mapping[str, Any]] = None,
) -> str:
    values = list(output_contracts or [])
    if isinstance(task_spec, Mapping):
        for item in task_spec.get("output_contracts") or []:
            if isinstance(item, Mapping) and item not in values:
                values.append(dict(item))
    for item in values:
        if not isinstance(item, Mapping) or str(item.get("role") or "") != "output_dir":
            continue
        path = str(
            item.get("canonical_path")
            or item.get("resolved_path")
            or item.get("path")
            or item.get("path_or_name")
            or ""
        ).strip()
        if path:
            return canonical_output_identity(path)
    return ""


def _renderer_owned_outputs(record: Dict[str, Any]) -> List[str]:
    values = record.get("owned_outputs") or []
    return [
        str(item.get("canonical_path") or item.get("artifact_id") or item.get("output_id") or "")
        if isinstance(item, dict) else str(item)
        for item in values
        if str(item)
    ]


def collapse_renderer_composite_steps(
    state: PlanState,
    renderer_skill_ids: List[str],
    renderer_records: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[PlanState, List[Dict[str, Any]]]:
    """Normalize a newly generated plan so each renderer owns one Execute step.

    Renderer internals are not independently schedulable plan work. This adapter
    is intentionally limited to untouched steps; once controller-owned renderer
    progress exists, a Planner patch must preserve that step verbatim.
    """
    candidate = deepcopy(state)
    renderer_ids = {str(item) for item in renderer_skill_ids if str(item).strip()}
    if not renderer_ids:
        return candidate, []
    steps = [
        normalize_plan_step(dict(step), index)
        for index, step in enumerate(candidate.get("steps", []), start=1)
        if isinstance(step, dict)
    ]
    audits: List[Dict[str, Any]] = []
    records_by_skill = {
        str(record.get("skill_id") or ""): dict(record)
        for record in (renderer_records or [])
        if isinstance(record, dict) and str(record.get("skill_id") or "")
    }
    for skill_id in sorted(renderer_ids):
        renderer = records_by_skill.get(skill_id, {})
        owned_outputs = _renderer_owned_outputs(renderer)
        matches = [
            (index, step)
            for index, step in enumerate(steps)
            if skill_id in {str(item) for item in step.get("skill_ids", []) or []}
            and str(step.get("action") or "").strip().upper() == "EXECUTE"
        ]
        if not matches:
            continue
        if len(matches) > 1 and any(step.get("renderer_execution") for _, step in matches):
            audits.append({
                "renderer_skill_id": skill_id,
                "status": "preserved_existing_execution",
                "step_ids": [str(step.get("step_id") or "") for _, step in matches],
            })
            continue

        anchor_index, anchor = matches[0]
        merged_ids = {str(step.get("step_id") or "") for _, step in matches}
        for field in ("inputs", "outputs", "success_criteria", "skill_ids"):
            merged: List[Any] = []
            seen: set[str] = set()
            for _, step in matches:
                for item in step.get(field, []) or []:
                    key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                    if key not in seen:
                        seen.add(key)
                        merged.append(deepcopy(item))
            anchor[field] = merged
        dependencies: List[str] = []
        for _, step in matches:
            for dependency in step.get("dependencies", []) or []:
                dependency = str(dependency)
                if dependency and dependency not in merged_ids and dependency not in dependencies:
                    dependencies.append(dependency)
        anchor["dependencies"] = dependencies
        anchor["depends_on"] = list(dependencies)
        anchor["title"] = f"Run renderer-backed workflow for {skill_id}"
        anchor["action"] = "EXECUTE"
        anchor["purpose"] = "analysis"
        anchor["tool"] = "renderer"
        anchor["renderer_composite"] = True
        anchor["step_type"] = "renderer_composite"
        anchor["selected_skill_id"] = skill_id
        anchor["renderer_id"] = str(renderer.get("renderer_id") or anchor.get("renderer_id") or skill_id)
        anchor["renderer_manifest_ref"] = str(
            (anchor.get("renderer_execution") or {}).get("manifest_path")
            or anchor.get("renderer_manifest_ref")
            or ""
        )
        anchor["renderer_manifest_hash"] = str(
            (anchor.get("renderer_execution") or {}).get("manifest_fingerprint")
            or anchor.get("renderer_manifest_hash")
            or ""
        )
        anchor["skills_declared"] = bool(anchor.get("skill_ids"))
        anchor["input_contracts"] = list(anchor.get("inputs") or [])
        anchor["output_contracts"] = list(anchor.get("outputs") or [])
        anchor["artifacts"] = step_artifact_contracts(anchor)

        removed_ids = merged_ids - {str(anchor.get("step_id") or "")}
        steps = [
            step for index, step in enumerate(steps)
            if index == anchor_index or str(step.get("step_id") or "") not in removed_ids
        ]
        anchor_id = str(anchor.get("step_id") or "")
        for step in steps:
            rewritten: List[str] = []
            for dependency in step.get("dependencies", []) or []:
                dependency = anchor_id if str(dependency) in removed_ids else str(dependency)
                if dependency and dependency != str(step.get("step_id") or "") and dependency not in rewritten:
                    rewritten.append(dependency)
            step["dependencies"] = rewritten
            step["depends_on"] = list(rewritten)
        audits.append({
            "renderer_skill_id": skill_id,
            "renderer_id": anchor.get("renderer_id"),
            "status": "collapsed",
            "anchor_step_id": anchor_id,
            "removed_step_ids": sorted(removed_ids),
            "owned_outputs": owned_outputs,
        })

    candidate["steps"] = steps
    if audits:
        candidate.setdefault("normalization_audit", []).extend(audits)
        candidate["semantic_plan_hash"] = semantic_plan_hash(candidate)
        candidate["updated_at"] = _now()
    return candidate, audits


def validate_output_contracts(
    state: PlanState,
    output_contracts: List[Dict[str, Any]],
    *,
    canonical_output_dir: str | Path = "",
    defer_workspace_coverage: bool = False,
) -> List[Dict[str, Any]]:
    """Validate exact required coverage and single materializable ownership."""
    from .plan_contract import plan_result_contracts, plan_uses_explicit_results
    explicit_results = plan_uses_explicit_results(state)
    if explicit_results:
        output_contracts = [
            {**row, "artifact_id": ref}
            for ref, row in plan_result_contracts(state).items()
        ]
    output_dir = str(canonical_output_dir or _canonical_output_dir(output_contracts))
    task_spec = _current_task_spec(state)
    legacy_renderer_mode = bool(
        ((state.get("task_state") or {}).get("legacy_renderer_mode") is True)
        or str((state.get("task_state") or {}).get("execution_contract") or "")
        == "legacy_renderer_v1"
    )
    artifact_bindings = {
        str(item.get("artifact_id") or item.get("output_id") or ""): dict(item)
        for item in (output_contracts if explicit_results else task_spec.get("output_contracts") or output_contracts or [])
        if isinstance(item, Mapping)
        and str(item.get("artifact_id") or item.get("output_id") or "")
    }
    producers: Dict[str, List[str]] = {}
    typed_usage = [
        dict(row) for row in state.get("artifact_usage", []) or []
        if isinstance(row, Mapping)
    ]
    if typed_usage and not legacy_renderer_mode:
        for row in typed_usage:
            artifact_ref = dict(row.get("artifact_ref") or {})
            if (
                str(row.get("relation") or "") != "produce"
                or (str(artifact_ref.get("kind") or "") != "authoritative" and not explicit_results)
            ):
                continue
            identity = str(artifact_ref.get("identity") or "")
            step_id = str(row.get("step_id") or "")
            if identity and step_id and step_id not in producers.setdefault(identity, []):
                producers[identity].append(step_id)
    else:
        # Reader compatibility for plans admitted before typed artifact_usage.
        for index, raw_step in enumerate(state.get("steps", []) or [], start=1):
            if not isinstance(raw_step, dict):
                continue
            step = normalize_plan_step(dict(raw_step), index)
            step_id = str(step.get("step_id") or "")
            outputs = list(step.get("outputs", []) or [])
            for output in outputs:
                output_text = str(output.get("artifact_id") or output.get("output_id") or "") if isinstance(output, Mapping) else str(output or "")
                identity = (
                    _output_identity(output, output_dir)
                    if legacy_renderer_mode
                    else output_text if output_text in artifact_bindings else _output_identity(output, output_dir)
                )
                if identity and step_id not in producers.setdefault(identity, []):
                    producers[identity].append(step_id)

    violations: List[Dict[str, Any]] = []
    for contract in output_contracts or []:
        if not isinstance(contract, dict) or not bool(contract.get("required", True)):
            continue
        if str(contract.get("role") or "") == "output_dir":
            continue
        if contract.get("resource_kind") == "mutable_workspace":
            continue  # State/evidence coverage is checked below, never PRODUCE.
        logical_id = str(contract.get("artifact_id") or contract.get("output_id") or "")
        identity = (
            _output_identity(contract, output_dir)
            if legacy_renderer_mode
            else logical_id or _output_identity(contract, output_dir)
        )
        if not identity:
            continue
        owner_ids = producers.get(identity, [])
        output_id = str(
            contract.get("output_id")
            or contract.get("artifact_id")
            or identity
        )
        common = {
            "output_id": output_id,
            "path_or_name": _output_path_or_name(contract),
            "source_type": str(contract.get("source_type") or contract.get("source") or "unknown"),
            "source_ref": str(contract.get("source_ref") or ""),
            "affected_output_ids": [str(contract.get("artifact_id") or contract.get("output_id") or identity)],
            "affected_step_ids": list(owner_ids),
            "allowed_changed_step_ids": list(owner_ids),
            "affected_artifact": {
                "kind": "authoritative",
                "identity": identity,
            },
            "affected_output": {
                "output_id": output_id,
                "handle": str(contract.get("semantic_handle") or ""),
                "role": str(contract.get("role") or ""),
            },
            "expected_identity": {
                "kind": "authoritative",
                "identity": identity,
            },
            "observed_identity": {
                "kind": "authoritative",
                "identity": identity,
                "producer_step_ids": list(owner_ids),
            },
            "machine_evidence": {
                "typed_usage_available": bool(typed_usage),
                "producer_step_ids": list(owner_ids),
            },
        }
        if not owner_ids:
            violations.append({
                **common,
                "violation_code": "REQUIRED_OUTPUT_UNCOVERED",
                "step_id": "",
                "field": "outputs",
                "reason": f"Required output `{common['path_or_name'] or identity}` has no logical producer.",
            })
        elif len(owner_ids) > 1:
            violations.append({
                **common,
                "violation_code": "OUTPUT_PRODUCER_AMBIGUOUS",
                "step_id": owner_ids[0],
                "field": "outputs",
                "reason": f"Output `{common['path_or_name'] or identity}` has multiple active producers: {', '.join(owner_ids)}.",
            })
    if not defer_workspace_coverage:
        violations.extend(validate_required_workspace_coverage(state))
    return violations


def validate_required_workspace_coverage(state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Require a sealed valid state chain, not merely the presence of MUTATE."""
    from .plan_contract import plan_result_contracts
    from .step_effects import validate_plan_dataflow_receipt
    required = {ref: row for ref, row in plan_result_contracts(state).items()
        if row.get("required") and row.get("resource_kind") == "mutable_workspace"}
    if not required:
        return []
    resources = dict(state.get("plan_resource_bindings") or {})
    receipt = dict(state.get("plan_dataflow_receipt") or {})
    checked = validate_plan_dataflow_receipt(receipt, state, resources)
    valid = checked["valid"] and receipt.get("status") == "compiled" and receipt.get("review_compatible") is True
    edges = list((state.get("compiled_dataflow") or {}).get("edges") or [])
    issues = []
    for ref in required:
        resource = resources.get(ref) or {}
        identity = str(resource.get("canonical_resource_id") or "")
        sources = [edge for edge in edges if edge.get("canonical_resource_id") == identity
            and edge.get("effect_kind") == "MUTATE" and edge.get("available_after_success") is True
            and edge.get("establishes_state")]
        if not valid or not identity or not sources:
            issues.append({"violation_code": "REQUIRED_WORKSPACE_UNCOVERED", "field": "workspace_mutations",
                "field_path": "$.proposed_artifacts", "affected_output_ids": [ref],
                "actual": {"resource_ref": ref, "dataflow_status": receipt.get("status"), "state_sources": sources},
                "expected": "A valid sealed dataflow with ordered, evidenced MUTATE state sources and all consumer prerequisites satisfied.",
                "check_status": "failed" if valid else "not_evaluated", "depends_on": "plan_dataflow",
                "reason": "Required mutable workspace has no verified planning state chain; this does not assert runtime completion."})
    return issues


def normalize_task_output_materializations(
    state: PlanState,
    task_spec: Mapping[str, Any] | None,
) -> Tuple[PlanState, List[Dict[str, Any]]]:
    """Validate Planner-owned filenames without creating another output identity."""
    candidate = deepcopy(state)
    from .plan_contract import plan_uses_explicit_results
    explicit_results = plan_uses_explicit_results(candidate)
    spec = dict(task_spec or _current_task_spec(candidate))
    contracts = [
        dict(row) for row in spec.get("output_contracts", []) or []
        if isinstance(row, Mapping)
    ]
    output_root = _canonical_output_dir(contracts, spec)
    by_ref: Dict[str, Dict[str, Any]] = {}
    for row in contracts:
        for key in (
            row.get("artifact_id"), row.get("output_id"), row.get("semantic_handle")
        ):
            if str(key or ""):
                by_ref[str(key)] = row

    def materialization_diagnostic(
        contract: Optional[Mapping[str, Any]],
        *,
        output_ref: str,
        expected_identity: Mapping[str, Any],
        observed_identity: Mapping[str, Any],
        machine_evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        contract_row = dict(contract or {})
        logical_id = str(
            contract_row.get("artifact_id")
            or contract_row.get("output_id")
            or output_ref
        )
        return {
            "affected_step_ids": [],
            "affected_artifact": {
                "kind": "authoritative" if contract_row else "unresolved",
                "identity": logical_id,
            },
            "affected_output": {
                "handle": str(contract_row.get("semantic_handle") or output_ref),
                "output_id": str(contract_row.get("output_id") or output_ref),
                "role": str(contract_row.get("role") or ""),
            },
            "expected_identity": deepcopy(dict(expected_identity)),
            "observed_identity": deepcopy(dict(observed_identity)),
            "machine_evidence": deepcopy(dict(machine_evidence)),
        }

    normalized: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(candidate.get("task_output_materializations", []) or []):
        if not isinstance(raw, Mapping):
            continue
        output_ref = str(raw.get("output_ref") or "")
        path_text = str(raw.get("path") or "").strip()
        contract = by_ref.get(output_ref)
        if contract is None:
            issues.append(validation_issue(
                "TASK_OUTPUT_REFERENCE_UNKNOWN",
                owner="planner",
                repair_target="$.task_output_materializations",
                field_path=f"$.task_output_materializations[{index}].output_ref",
                message=f"Unknown TaskSpec output reference `{output_ref}`.",
                **materialization_diagnostic(
                    None,
                    output_ref=output_ref,
                    expected_identity={
                        "kind": "authoritative_task_output",
                        "membership": "declared_in_canonical_task_spec",
                    },
                    observed_identity={
                        "kind": "unresolved_output_reference",
                        "identity": output_ref,
                    },
                    machine_evidence={
                        "known_output_refs": sorted(by_ref),
                    },
                ),
            ))
            continue
        output_id = str(contract.get("output_id") or contract.get("artifact_id") or output_ref)
        if output_id in seen:
            issues.append(validation_issue(
                "TASK_OUTPUT_MATERIALIZATION_DUPLICATE",
                owner="planner",
                repair_target="$.task_output_materializations",
                field_path=f"$.task_output_materializations[{index}]",
                message=f"Task output `{output_id}` is materialized more than once.",
                **materialization_diagnostic(
                    contract,
                    output_ref=output_ref,
                    expected_identity={
                        "kind": "authoritative_task_output",
                        "identity": str(contract.get("artifact_id") or output_id),
                        "materialization_cardinality": 1,
                    },
                    observed_identity={
                        "kind": "authoritative_task_output",
                        "identity": str(contract.get("artifact_id") or output_id),
                        "materialization_cardinality": 2,
                    },
                    machine_evidence={"duplicate_index": index},
                ),
            ))
            continue
        seen.add(output_id)
        exact_path = str(contract.get("canonical_path") or "").strip()
        artifact_kind = str(contract.get("artifact_kind") or "file").lower()
        if artifact_kind == "directory" or str(contract.get("role") or "") == "output_dir":
            issues.append(validation_issue(
                "TASK_OUTPUT_MATERIALIZATION_KIND_INVALID",
                owner="planner",
                repair_target="$.task_output_materializations",
                field_path=f"$.task_output_materializations[{index}]",
                message=f"Directory output `{output_id}` cannot receive a Planner filename.",
                **materialization_diagnostic(
                    contract,
                    output_ref=output_ref,
                    expected_identity={
                        "kind": "materializable_task_output",
                        "artifact_kind": "file",
                    },
                    observed_identity={
                        "kind": "authoritative_task_output",
                        "identity": str(contract.get("artifact_id") or output_id),
                        "artifact_kind": artifact_kind,
                    },
                    machine_evidence={"output_role": str(contract.get("role") or "")},
                ),
            ))
            continue
        path = Path(path_text)
        if path.is_absolute() or ".." in path.parts:
            issues.append(validation_issue(
                "TASK_OUTPUT_MATERIALIZATION_PATH_INVALID",
                owner="planner",
                repair_target="$.task_output_materializations",
                field_path=f"$.task_output_materializations[{index}].path",
                message="Planner-owned output paths must be safe relative paths under output_dir.",
                **materialization_diagnostic(
                    contract,
                    output_ref=output_ref,
                    expected_identity={
                        "path_policy": "safe_relative_under_output_root",
                    },
                    observed_identity={"path": path_text},
                    machine_evidence={
                        "is_absolute": path.is_absolute(),
                        "contains_parent_traversal": ".." in path.parts,
                    },
                ),
            ))
            continue
        canonical_path = canonical_output_identity(path_text, output_root)
        if exact_path and canonical_output_identity(exact_path, output_root) != canonical_path:
            issues.append(validation_issue(
                "USER_OUTPUT_PATH_NOT_PRESERVED",
                owner="planner",
                repair_target="$.task_output_materializations",
                field_path=f"$.task_output_materializations[{index}].path",
                message=f"Planner changed the exact TaskSpec path for `{output_id}`.",
                **materialization_diagnostic(
                    contract,
                    output_ref=output_ref,
                    expected_identity={
                        "required_basename": Path(exact_path).name,
                    },
                    observed_identity={"candidate_path": path_text},
                    machine_evidence={"path_policy": str(contract.get("path_policy") or "")},
                ),
            ))
            continue
        normalized.append({
            "output_ref": str(contract.get("artifact_id") or output_ref),
            "output_id": output_id,
            "path": path_text,
            "canonical_path": canonical_path,
            "origin": "planner",
            **({
                "required": bool(raw.get("required", False)),
                "path_policy": str(raw.get("path_policy") or "model_selectable"),
                **{key: deepcopy(raw[key]) for key in ("validator", "description", "source_refs") if key in raw},
            } if explicit_results else {}),
        })

    for contract in contracts:
        output_id = str(contract.get("output_id") or contract.get("artifact_id") or "")
        if (
            not explicit_results
            and bool(contract.get("required", True))
            and str(contract.get("artifact_kind") or "file").lower() == "file"
            and not str(contract.get("canonical_path") or "").strip()
            and str(contract.get("path_policy") or "") == "planner_materialized"
            and output_id not in seen
        ):
            issues.append(validation_issue(
                "TASK_OUTPUT_MATERIALIZATION_MISSING",
                owner="planner",
                repair_target="$.task_output_materializations",
                field_path="$.task_output_materializations",
                message=f"Required task output `{output_id}` needs a Planner-owned relative path.",
                **materialization_diagnostic(
                    contract,
                    output_ref=str(contract.get("semantic_handle") or output_id),
                    expected_identity={
                        "kind": "authoritative_task_output",
                        "identity": str(contract.get("artifact_id") or output_id),
                        "materialization": "one_safe_relative_path",
                    },
                    observed_identity={"materialization": "missing"},
                    machine_evidence={
                        "path_policy": "planner_materialized",
                        "required": True,
                    },
                ),
            ))
    candidate["task_output_materializations"] = normalized
    return candidate, issues


def bind_renderer_invocation_contracts(
    state: PlanState,
    renderer_records: List[Dict[str, Any]],
    task_spec: Optional[Dict[str, Any]],
) -> PlanState:
    """Instantiate deterministic renderer contracts from approved task bindings."""
    candidate = deepcopy(state)
    records = {
        str(item.get("skill_id") or ""): dict(item)
        for item in renderer_records or [] if isinstance(item, dict) and item.get("skill_id")
    }
    candidate["renderer_projections"] = []
    candidate["renderer_projection_issues"] = []
    if not task_spec:
        return candidate
    for index, raw in enumerate(candidate.get("steps", []) or [], 1):
        if not isinstance(raw, dict):
            continue
        step = normalize_plan_step(raw, index)
        if str(step.get("step_type") or "") != "renderer_composite":
            continue
        skill_id = str(step.get("selected_skill_id") or "")
        if not skill_id:
            skill_id = next((str(item) for item in step.get("skill_ids", []) or [] if str(item) in records), "")
        renderer = records.get(skill_id, {})
        if not renderer:
            issue = validation_issue(
                "RENDERER_CONTRACT_MISSING",
                owner="renderer_projection",
                repair_target=f"skill_metadata.renderer[{skill_id}]",
                field_path=f"plan.steps[{index - 1}].renderer_id",
                message=f"No declared renderer contract is available for `{skill_id or step.get('renderer_id')}`.",
                plan_step_id=str(step.get("step_id") or ""),
                renderer_field="renderer_id",
            )
            candidate["renderer_projection_issues"].append(issue)
            continue
        projection = project_task_spec_to_renderer(
            task_spec,
            renderer.get("invocation") or {},
            task_id=str(candidate.get("task_id") or ""),
        )
        projection_record = {
            **deepcopy(projection),
            "plan_step_id": str(step.get("step_id") or ""),
            "skill_id": skill_id,
            "renderer_id": str(renderer.get("renderer_id") or step.get("renderer_id") or ""),
            "projection_hash": _stable_payload_hash(projection),
        }
        step["renderer_projection"] = deepcopy(projection_record)
        candidate["renderer_projections"].append(deepcopy(projection_record))
        if projection.get("status") != PROJECTION_COMPLETE or projection.get("conflicts"):
            step.pop("renderer_invocation_contract", None)
            step["bound_outputs"] = []
            if projection.get("status") == PROJECTION_NOT_APPLICABLE:
                candidate["renderer_projection_issues"].append(validation_issue(
                    "RENDERER_NOT_APPLICABLE",
                    owner="internal",
                    repair_target=f"skill_metadata.renderer[{skill_id}]",
                    field_path=f"plan.steps[{index - 1}].renderer_projection.status",
                    message=(
                        "A renderer-composite plan step selected a renderer whose "
                        "declared invocation contract is not applicable."
                    ),
                    plan_step_id=str(step.get("step_id") or ""),
                    renderer_field="invocation",
                    evidence={"projection_status": PROJECTION_NOT_APPLICABLE},
                ))
            for missing in projection.get("missing_binding_details") or []:
                detail = dict(missing) if isinstance(missing, Mapping) else {"argument": str(missing)}
                candidate["renderer_projection_issues"].append(validation_issue(
                    "RENDERER_MISSING_REQUIRED_BINDING",
                    owner=str(detail.get("owner") or "task_spec"),
                    repair_target=str(detail.get("repair_target") or "task_spec"),
                    field_path=str(detail.get("field_path") or "renderer.invocation"),
                    message=(
                        "Required renderer argument "
                        f"`{detail.get('argument') or '<unknown>'}` has no authoritative binding."
                    ),
                    plan_step_id=str(step.get("step_id") or ""),
                    renderer_field=str(detail.get("argument") or ""),
                    evidence={"projection_status": projection.get("status")},
                ))
            for conflict in projection.get("conflicts") or []:
                detail = dict(conflict) if isinstance(conflict, Mapping) else {"reason": str(conflict)}
                candidate["renderer_projection_issues"].append(validation_issue(
                    "RENDERER_BINDING_CONFLICT",
                    owner="task_spec",
                    repair_target=f"task_spec.inputs[role={detail.get('role') or ''}]",
                    field_path=f"renderer.invocation.{detail.get('role') or ''}",
                    message=str(detail.get("reason") or "Renderer binding candidates conflict."),
                    plan_step_id=str(step.get("step_id") or ""),
                    renderer_field=str(detail.get("role") or ""),
                    evidence=detail,
                ))
            candidate["steps"][index - 1] = step
            continue
        payload = {
            "schema_version": 2,
            "renderer_id": str(renderer.get("renderer_id") or step.get("renderer_id") or ""),
            "producer_step_id": str(step.get("step_id") or ""),
            "arguments": projection.get("arguments") or {},
            "outdir": str(projection.get("outdir") or ""),
            "semantic_claims": deepcopy(list(projection.get("semantic_claims") or [])),
            "outputs": [],
            "bound_outputs": [],
        }
        payload["invocation_id"] = "renderer-invocation-" + _stable_payload_hash(payload)[:20]
        payload["projection_hash"] = _stable_payload_hash(projection)
        payload["source"] = "task_spec_renderer_projection"
        step["renderer_invocation_contract"] = payload
        canonical_inputs: List[str] = []
        for argument, source in dict(projection.get("binding_sources") or {}).items():
            if not isinstance(source, Mapping) or source.get("kind") != "input":
                continue
            value = (projection.get("arguments") or {}).get(argument)
            text = _valid_contract_text(value)
            if text and text not in canonical_inputs:
                canonical_inputs.append(text)
        if canonical_inputs:
            # Renderer projection, not merged Planner prose, owns its concrete
            # input contract.  Runtime dependencies remain in runtime schema.
            step["inputs"] = canonical_inputs
            step["input_contracts"] = list(canonical_inputs)
        candidate["steps"][index - 1] = step
    candidate["renderer_projections"].sort(
        key=lambda item: (str(item.get("plan_step_id") or ""), str(item.get("renderer_id") or ""))
    )
    return candidate


def _field_identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _renderer_binding_for_output(
    contract: Mapping[str, Any],
    renderer: Mapping[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    role = _field_identity(contract.get("role"))
    output_id = str(contract.get("artifact_id") or contract.get("output_id") or "")
    requested_name = Path(_output_path_or_name(contract)).name
    for logical, raw_binding in dict(renderer.get("output_bindings") or {}).items():
        binding = dict(raw_binding or {}) if isinstance(raw_binding, Mapping) else {"mode": str(raw_binding or "")}
        declared = str(
            binding.get("logical_output")
            or binding.get("canonical_path")
            or binding.get("path")
            or binding.get("filename")
            or logical
        )
        if (
            role == _field_identity(logical)
            or output_id == str(logical)
            or (requested_name and Path(declared).name == requested_name)
        ):
            return str(logical), binding
    return "", {}


def _renderer_has_output_capability(
    contract: Mapping[str, Any],
    renderer: Mapping[str, Any],
    logical_output: str,
    binding: Mapping[str, Any],
) -> Tuple[bool, str]:
    requested_name = Path(_output_path_or_name(contract)).name
    role = _field_identity(contract.get("role"))
    declared_name = Path(str(binding.get("canonical_path") or binding.get("filename") or binding.get("path") or "")).name
    for owned in _renderer_owned_outputs(dict(renderer)):
        owned_name = Path(str(owned)).name
        if requested_name and owned_name == requested_name:
            return True, str(owned)
        if declared_name and owned_name == declared_name and role == _field_identity(logical_output):
            return True, str(owned)
    return False, ""


def bind_renderer_output_contracts(
    state: PlanState,
    renderer_records: List[Dict[str, Any]],
    output_contracts: List[Dict[str, Any]],
    task_spec: Optional[Dict[str, Any]] = None,
) -> PlanState:
    """Bind TaskSpec outputs to exact renderer arguments or fixed paths.

    ``owned_outputs`` only authorizes consideration of a capability.  A bound
    producer is written only after the declarative materialization rule proves
    the exact canonical path requested by TaskSpec.
    """
    candidate = deepcopy(state)
    records = {
        str(item.get("skill_id") or ""): dict(item)
        for item in renderer_records or []
        if isinstance(item, dict) and str(item.get("skill_id") or "")
    }
    output_dir = _canonical_output_dir(output_contracts, task_spec)
    violations: List[Dict[str, Any]] = []
    binding_audit: List[Dict[str, Any]] = []
    contracts = [
        dict(item) for item in output_contracts or []
        if isinstance(item, dict) and str(item.get("role") or "") != "output_dir"
    ]
    task_output_constraints: Dict[str, Dict[str, Any]] = {}
    for item in semantic_constraints_from_task_spec(task_spec or {}):
        if not str(item.get("semantic_key") or "").startswith("required_output."):
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        task_output_constraints[value] = dict(item)
        task_output_constraints.setdefault(Path(value).name, dict(item))
    for index, raw in enumerate(candidate.get("steps", []) or [], 1):
        if not isinstance(raw, dict):
            continue
        step = normalize_plan_step(raw, index)
        if str(step.get("step_type") or "") != "renderer_composite":
            continue
        skill_id = str(step.get("selected_skill_id") or "")
        if not skill_id:
            skill_id = next((str(item) for item in step.get("skill_ids", []) or [] if str(item) in records), "")
        renderer = records.get(skill_id, {})
        invocation = dict(step.get("renderer_invocation_contract") or {})
        if not renderer or not invocation:
            continue
        outdir = str(invocation.get("outdir") or output_dir)
        argument_specs = dict((renderer.get("invocation") or {}).get("arguments") or {})
        bound_outputs: List[Dict[str, Any]] = []
        bound_paths: List[str] = []
        arguments = dict(invocation.get("arguments") or {})
        invocation_semantic_claims = [
            dict(item)
            for item in invocation.get("semantic_claims", []) or []
            if isinstance(item, Mapping)
        ]

        for contract in contracts:
            logical_output, binding = _renderer_binding_for_output(contract, renderer)
            capable, owned_capability = _renderer_has_output_capability(
                contract,
                renderer,
                logical_output,
                binding,
            )
            if not capable:
                continue
            requested = _output_path_or_name(contract)
            required_identity = canonical_output_identity(contract, output_dir)
            mode = str(binding.get("mode") or "").strip().lower()
            argument = str(binding.get("argument") or "").strip()
            materialized = ""
            status = "bound"
            reason = ""
            if not binding:
                status = "unresolved"
                reason = "Renderer capability has no declarative output binding metadata."
            elif mode == "argument":
                if not argument or argument not in argument_specs:
                    status = "unresolved"
                    reason = f"Declared output argument `{argument or '<missing>'}` is absent from the renderer invocation schema."
                else:
                    arguments[argument] = requested
                    materialized = canonical_output_identity(requested, outdir)
            elif mode == "fixed":
                fixed = str(binding.get("canonical_path") or binding.get("path") or binding.get("filename") or "").strip()
                if not fixed:
                    status = "unresolved"
                    reason = "Fixed output binding has no deterministic path."
                else:
                    materialized = canonical_output_identity(fixed, outdir)
            else:
                status = "unresolved"
                reason = f"Unsupported renderer output binding mode `{mode or '<missing>'}`."
            if status == "bound" and materialized != required_identity:
                status = "unresolved"
                reason = (
                    f"Renderer materializes `{materialized}` but TaskSpec requires `{required_identity}`."
                )

            audit = {
                "task": str(candidate.get("task_id") or ""),
                "step_id": str(step.get("step_id") or ""),
                "renderer_id": str(renderer.get("renderer_id") or step.get("renderer_id") or ""),
                "required_output": requested,
                "canonical_identity": required_identity,
                "owned_capability": owned_capability,
                "binding_mode": mode,
                "binding_argument": argument,
                "bound_path": materialized,
                "binding_source": "skill_metadata.output_bindings",
                "status": status,
            }
            binding_audit.append(audit)
            logger.info(
                "Renderer output binding: task=%s step=%s renderer=%s required=%s capability=%s mode=%s arg=%s bound=%s source=%s status=%s",
                audit["task"], audit["step_id"], audit["renderer_id"], requested,
                owned_capability or "-", mode or "-", argument or "-",
                materialized or "-", audit["binding_source"], status,
            )
            if status != "bound":
                violations.append({
                    **audit,
                    "violation_code": "RENDERER_OUTPUT_BINDING_UNRESOLVED",
                    "field": "bound_outputs",
                    "output_id": str(contract.get("artifact_id") or contract.get("output_id") or ""),
                    "path_or_name": requested,
                    "affected_output_ids": [str(contract.get("artifact_id") or contract.get("output_id") or required_identity)],
                    "affected_step_ids": [str(step.get("step_id") or "")],
                    "allowed_changed_step_ids": [],
                    "reason": reason,
                })
                continue
            record = {
                "output_id": str(contract.get("artifact_id") or contract.get("output_id") or ""),
                "logical_output": logical_output,
                "required_output": requested,
                "canonical_path": required_identity,
                "materialized_path": materialized,
                "binding_mode": mode,
                "binding_argument": argument,
                "binding_source": "skill_metadata.output_bindings",
                "renderer_id": str(renderer.get("renderer_id") or step.get("renderer_id") or ""),
                "producer_step_id": str(step.get("step_id") or ""),
                "required": bool(contract.get("required", True)),
            }
            bound_outputs.append(record)
            if requested and requested not in bound_paths:
                bound_paths.append(requested)
            task_constraint = (
                task_output_constraints.get(requested)
                or task_output_constraints.get(required_identity)
                or task_output_constraints.get(Path(requested).name)
            )
            semantic_key = str(
                (task_constraint or {}).get("semantic_key")
                or f"required_output.{_field_identity(contract.get('role') or logical_output or 'output')}"
            )
            semantic_value = (task_constraint or {}).get("value", requested)
            output_claim = {
                "semantic_key": semantic_key,
                "operator": "eq",
                "value": semantic_value,
                "strength": "required",
                "confidence": 1.0,
                "source_ref": f"renderer_output_binding:{step.get('step_id') or ''}:{logical_output or contract.get('role') or ''}",
            }
            identity = json.dumps(
                {key: output_claim.get(key) for key in ("semantic_key", "operator", "value")},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if all(
                json.dumps(
                    {key: existing.get(key) for key in ("semantic_key", "operator", "value")},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ) != identity
                for existing in invocation_semantic_claims
            ):
                invocation_semantic_claims.append(output_claim)

        if contracts:
            step["bound_outputs"] = bound_outputs
            step["outputs"] = bound_paths
            step["output_contracts"] = list(bound_paths)
        invocation["arguments"] = arguments
        invocation["outdir"] = outdir
        invocation["outputs"] = list(bound_paths)
        invocation["bound_outputs"] = deepcopy(bound_outputs)
        invocation["semantic_claims"] = invocation_semantic_claims
        invocation["manifest_path"] = canonical_output_identity(
            str(renderer.get("manifest_filename") or "manifest.json"),
            outdir,
        ) if outdir else ""
        invocation["arguments_hash"] = _stable_payload_hash({
            "arguments": arguments,
            "outdir": outdir,
            "outputs": bound_outputs,
        })
        invocation_payload = {
            key: value for key, value in invocation.items()
            if key != "invocation_id"
        }
        invocation["invocation_id"] = "renderer-invocation-" + _stable_payload_hash(invocation_payload)[:20]
        step["renderer_invocation_contract"] = invocation
        candidate["steps"][index - 1] = step

    candidate["output_binding_violations"] = violations
    if binding_audit:
        candidate.setdefault("normalization_audit", []).append({
            "phase": "bind_renderer_output_contracts",
            "bindings": binding_audit,
        })
    candidate["semantic_plan_hash"] = semantic_plan_hash(candidate)
    return candidate


def normalize_plan_output_count_criteria(
    state: PlanState,
    output_contracts: List[Dict[str, Any]],
) -> PlanState:
    """Remove stale machine-generated output counts from review criteria."""
    candidate = deepcopy(state)
    from .plan_contract import plan_uses_explicit_results
    if plan_uses_explicit_results(candidate):
        return candidate
    required_count = sum(
        1 for item in output_contracts or []
        if isinstance(item, dict)
        and bool(item.get("required", True))
        and str(item.get("role") or "") != "output_dir"
    )
    changes: List[Dict[str, Any]] = []
    english = re.compile(r"\ball\s+(?P<count>\d+)\s+(?:required\s+)?output(?:\s+files?|s)\b", re.IGNORECASE)
    chinese = re.compile(r"所有\s*(?P<count>\d+)\s*个\s*输出(?:文件|产物)?\s*均?")
    for index, raw in enumerate(candidate.get("steps", []) or [], 1):
        if not isinstance(raw, dict):
            continue
        step = normalize_plan_step(raw, index)
        normalized_criteria: List[str] = []
        for criterion in step.get("success_criteria", []) or []:
            text = str(criterion)
            counts = [int(value) for value in [
                *(match.group("count") for match in english.finditer(text)),
                *(match.group("count") for match in chinese.finditer(text)),
            ]]
            replaced = english.sub("all required outputs", text)
            replaced = chinese.sub("所有 required outputs 均", replaced)
            if replaced != text:
                changes.append({
                    "violation_code": (
                        "PLAN_OUTPUT_COUNT_MISMATCH"
                        if any(count != required_count for count in counts)
                        else "PLAN_OUTPUT_COUNT_NORMALIZED"
                    ),
                    "step_id": str(step.get("step_id") or ""),
                    "declared_counts": counts,
                    "required_count": required_count,
                    "before": text,
                    "after": replaced,
                })
            normalized_criteria.append(replaced)
        step["success_criteria"] = normalized_criteria
        candidate["steps"][index - 1] = step
    if changes:
        candidate.setdefault("normalization_audit", []).append({
            "phase": "normalize_plan_output_count_criteria",
            "changes": changes,
        })
    return candidate


def _validation_convergence_fingerprint(
    state: PlanState,
    issues: List[Dict[str, Any]],
) -> str:
    payload = {
        "plan_hash": (
            str((state.get("current_plan") or {}).get("plan_semantic_hash") or "")
            if str((state.get("current_plan") or {}).get("plan_snapshot_ref") or "")
            else ""
        ),
        "task_spec_hash": canonical_constraint_hash(_current_task_spec(state)),
        "task_revision": _int(state.get("generated_from_task_revision"), 0),
        "planner_feedback_revision": int(
            ((state.get("task_state") or {}).get("planner_feedback") or {}).get("revision") or 0
        ),
        "issue_fingerprints": sorted(
            str(item.get("fingerprint") or "") for item in issues
        ),
    }
    return _stable_payload_hash(payload)


def derive_plan_validation_state(
    state: PlanState,
    issues: List[Any],
    *,
    record_convergence: bool = True,
    max_repair_attempts: int = 6,
) -> PlanState:
    """Overwrite all controller-derived validation and generation fields."""
    candidate = deepcopy(state)
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in issues:
        issue = normalize_validation_issue(raw)
        fingerprint = str(issue.get("fingerprint") or "")
        if fingerprint and fingerprint not in seen:
            seen.add(fingerprint)
            normalized.append(issue)
    normalized.sort(key=lambda item: (
        str(item.get("severity") or ""),
        str(item.get("owner") or ""),
        str(item.get("code") or ""),
        str(item.get("field_path") or ""),
    ))
    blocking = [
        item for item in normalized
        if str(item.get("severity") or "").lower() in BLOCKING_VALIDATION_SEVERITIES
    ]
    fingerprint = _validation_convergence_fingerprint(candidate, normalized)
    history = [
        dict(item) for item in candidate.get("validation_convergence_history", []) or []
        if isinstance(item, Mapping)
    ]
    non_convergent = False
    cycle_type = ""
    if record_convergence and blocking:
        prior_fingerprints = [str(item.get("fingerprint") or "") for item in history]
        if prior_fingerprints and prior_fingerprints[-1] == fingerprint:
            non_convergent = True
            cycle_type = "A_TO_A"
        elif fingerprint in prior_fingerprints:
            non_convergent = True
            cycle_type = "A_TO_B_TO_A"
        elif len(history) >= max(1, int(max_repair_attempts)):
            non_convergent = True
            cycle_type = "REPAIR_CEILING"
        history.append({
            "fingerprint": fingerprint,
            "plan_hash": semantic_plan_hash(candidate),
            "task_spec_hash": canonical_constraint_hash(_current_task_spec(candidate)),
            "issue_fingerprints": [str(item.get("fingerprint") or "") for item in blocking],
            "recorded_at": _now(),
        })
    candidate["validation_issues"] = normalized
    candidate["blocking_validation_issue_fingerprints"] = [
        str(item.get("fingerprint") or "") for item in blocking
    ]
    candidate["validation_convergence_fingerprint"] = fingerprint
    candidate["validation_convergence_history"] = history[-(max_repair_attempts + 2):]
    candidate["repair_convergence"] = {
        "status": "blocked_non_convergent" if non_convergent else "converging" if blocking else "valid",
        "cycle_type": cycle_type,
        "attempt_count": len(history),
        "max_attempts": int(max_repair_attempts),
        "fingerprint": fingerprint,
    }
    legacy_renderer_mode = bool(
        ((candidate.get("task_state") or {}).get("legacy_renderer_mode") is True)
        or str((candidate.get("task_state") or {}).get("execution_contract") or "")
        == "legacy_renderer_v1"
    )
    non_complete_projection_ids = ([
        str(item.get("plan_step_id") or item.get("renderer_id") or "renderer")
        for item in candidate.get("renderer_projections", []) or []
        if isinstance(item, Mapping)
        and str(item.get("status") or "") != PROJECTION_COMPLETE
    ] if legacy_renderer_mode else [])
    candidate["can_request_approval"] = bool(
        not blocking
        and not non_convergent
        and not non_complete_projection_ids
    )
    task_revision = _int(candidate.get("generated_from_task_revision"), 0)
    candidate["can_generate"] = bool(
        candidate["can_request_approval"]
        and plan_approval_is_current(candidate, task_revision)
    )
    candidate["approval_gate"] = {
        "validation_complete": not blocking,
        "renderer_projections_complete": not non_complete_projection_ids,
        "non_complete_projection_ids": non_complete_projection_ids,
        "approval_current": plan_approval_is_current(candidate, task_revision),
        "can_generate": bool(candidate["can_generate"]),
        "blocking_issue_fingerprints": list(candidate["blocking_validation_issue_fingerprints"]),
        "repair_status": candidate["repair_convergence"]["status"],
    }
    return candidate


def normalize_plan_contracts(
    state: PlanState,
    *,
    renderer_records: Optional[List[Dict[str, Any]]] = None,
    output_contracts: Optional[List[Dict[str, Any]]] = None,
    task_spec: Optional[Dict[str, Any]] = None,
    defer_workspace_coverage: bool = False,
) -> Tuple[PlanState, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run the shared pre-review normalization and output validation pipeline."""
    legacy_renderer_mode = bool(
        ((state.get("task_state") or {}).get("legacy_renderer_mode") is True)
        or str((state.get("task_state") or {}).get("execution_contract") or "")
        == "legacy_renderer_v1"
    )
    records = [dict(item) for item in (renderer_records or []) if isinstance(item, dict)]
    if legacy_renderer_mode:
        renderer_skill_ids = [
            str(item.get("skill_id") or "") for item in records
            if str(item.get("skill_id") or "")
        ]
        normalized, audit = collapse_renderer_composite_steps(
            state, renderer_skill_ids, records
        )
    else:
        normalized = deepcopy(state)
        audit = []
    if task_spec:
        canonical_task = canonical_current_task_spec(task_spec)
        normalized["current_task"] = _canonical_task_record(
            canonical_task,
            previous=dict(normalized.get("current_task") or {}),
            generation=int(normalized.get("state_generation") or 0),
        )
    normalized, semantic_reference_issues = resolve_plan_semantic_claims(
        normalized,
        task_spec or _current_task_spec(normalized),
    )
    if legacy_renderer_mode:
        normalized = bind_renderer_invocation_contracts(normalized, records, task_spec)
        normalized = bind_renderer_output_contracts(
            normalized,
            records,
            list(output_contracts or []),
            task_spec,
        )
    else:
        normalized["renderer_projections"] = []
        normalized["renderer_projection_issues"] = []
        normalized["output_binding_violations"] = []
    normalized, materialization_issues = normalize_task_output_materializations(
        normalized,
        task_spec or _current_task_spec(normalized),
    )
    normalized = normalize_plan_output_count_criteria(normalized, list(output_contracts or []))
    violations: List[Dict[str, Any]] = []
    violations.extend(semantic_reference_issues)
    violations.extend(materialization_issues)
    if legacy_renderer_mode:
        violations.extend(deepcopy(list(normalized.get("renderer_projection_issues") or [])))
        violations.extend(deepcopy(list(normalized.get("output_binding_violations") or [])))
    violations.extend(plan_contract_violations(normalized))
    violations.extend(validate_output_contracts(
        normalized,
        list(output_contracts or []),
        canonical_output_dir=_canonical_output_dir(list(output_contracts or []), task_spec),
        defer_workspace_coverage=defer_workspace_coverage,
    ))
    normalized = derive_plan_validation_state(
        normalized,
        violations,
        record_convergence=False,
    )
    return normalized, audit, deepcopy(list(normalized.get("validation_issues") or []))


def validate_plan_for_generate(state: PlanState, task_revision: int) -> Dict[str, Any]:
    """Return one structured decision for every deterministic Generate plan guard."""
    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    base = {
        "valid": False,
        "reason_code": "",
        "missing_fields": [],
        "recoverable": True,
        "validation_function": "validate_plan_for_generate",
        "plan_status": str(state.get("plan_status") or ""),
        "plan_revision": _int(state.get("plan_revision"), 0),
        "migration_applied": list(state.get("migrations_applied") or []),
    }
    if str(state.get("load_error") or "").strip():
        return {
            **base,
            "reason_code": "plan_state_load_failed",
            "missing_fields": ["plan_state.json"],
            "load_error": str(state.get("load_error")),
        }
    task_status = str((state.get("task_state") or {}).get("status") or TASK_STATUS_RUNNING).strip().lower()
    if task_status in TASK_TERMINAL_STATUSES:
        return {
            **base,
            "reason_code": f"task_{task_status}",
            "recoverable": False,
            "task_status": task_status,
        }
    if not steps:
        return {**base, "reason_code": "plan_missing", "missing_fields": ["steps"]}
    if bool(state.get("validation_recompute_required")):
        return {
            **base,
            "reason_code": "plan_validation_recompute_required",
            "missing_fields": ["validation_issues", "renderer_projections"],
            "repair_owner": "internal",
            "repair_target": "plan_contract_validation",
        }
    blocking_issues = [
        dict(item) for item in state.get("validation_issues", []) or []
        if isinstance(item, Mapping)
        and str(item.get("severity") or "").lower() in BLOCKING_VALIDATION_SEVERITIES
    ]
    if blocking_issues:
        first = blocking_issues[0]
        return {
            **base,
            "reason_code": "plan_validation_blocked",
            "missing_fields": [
                str(item.get("field_path") or item.get("repair_target") or item.get("code") or "plan_contract")
                for item in blocking_issues
            ],
            "validation_issues": blocking_issues,
            "repair_owner": str(first.get("owner") or "internal"),
            "repair_target": str(first.get("repair_target") or ""),
        }
    non_complete_projections = [
        dict(item) for item in state.get("renderer_projections", []) or []
        if isinstance(item, Mapping)
        and str(item.get("status") or "") != PROJECTION_COMPLETE
    ]
    if non_complete_projections:
        return {
            **base,
            "reason_code": "renderer_projection_not_complete",
            "missing_fields": [
                f"renderer.{item.get('renderer_id') or ''}.{field}"
                for item in non_complete_projections
                for field in (item.get("missing_bindings", []) or ["status"])
            ],
            "repair_owner": "renderer_projection",
            "repair_target": "renderer_projection",
        }
    if str((state.get("repair_convergence") or {}).get("status") or "") == "blocked_non_convergent":
        return {
            **base,
            "reason_code": "plan_repair_non_convergent",
            "recoverable": False,
            "repair_owner": "internal",
            "repair_target": "validation_convergence",
        }
    if not plan_approval_is_current(state, task_revision):
        generated_revision = _int(state.get("generated_from_task_revision"), 0)
        if generated_revision and generated_revision != _int(task_revision, 0):
            reason = "stale_task_revision"
        elif state.get("approval_record") or state.get("approved_plan_hash"):
            reason = "plan_approval_stale"
        else:
            reason = "plan_not_approved"
        return {**base, "reason_code": reason}

    current_index, current = current_plan_step_with_index(state)
    if current_index < 0:
        return {**base, "reason_code": "current_step_missing", "recoverable": False}
    step = normalize_plan_step(current, current_index + 1)
    missing_keys = [field for field in PLAN_STEP_FIELDS if field not in step]
    if missing_keys:
        return {
            **base,
            "reason_code": "missing_step_fields",
            "missing_fields": [f"steps[{current_index}].{field}" for field in missing_keys],
            "current_step_id": str(step.get("step_id") or ""),
            "current_step_index": current_index,
        }
    required_empty = []
    if not str(step.get("action") or "").strip():
        required_empty.append(f"steps[{current_index}].action")
    if not list(step.get("success_criteria") or []):
        required_empty.append(f"steps[{current_index}].success_criteria")
    if required_empty:
        reason = "missing_success_criteria" if required_empty == [f"steps[{current_index}].success_criteria"] else "incomplete_step_contract"
        return {
            **base,
            "reason_code": reason,
            "missing_fields": required_empty,
            "current_step_id": str(step.get("step_id") or ""),
            "current_step_index": current_index,
        }
    if state.get("can_generate") is False:
        return {
            **base,
            "reason_code": "controller_generation_gate_closed",
            "repair_owner": "internal",
            "repair_target": "approval_gate",
        }
    return {
        **base,
        "valid": True,
        "reason_code": "valid",
        "current_step_id": str(step.get("step_id") or ""),
        "current_step_index": current_index,
    }


def current_plan_step_with_index(state: PlanState) -> Tuple[int, PlanStep]:
    """Return the current executable step; terminal blockers are never executable."""
    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    cursor = str(state.get("current_step_id") or "")
    if cursor:
        for index, step in enumerate(steps):
            if (
                str(step.get("step_id") or "") == cursor
                and str(step.get("status") or "").strip().lower() in STEP_ACTIVE_STATUSES
            ):
                return index, step
    for status in (
        STATUS_RUNNING,
        STATUS_VERIFICATION_PENDING,
        STATUS_RETRY_PENDING,
        STATUS_REPAIRING,
        STATUS_DIAGNOSING,
        STATUS_READY,
        STATUS_PENDING,
    ):
        for index, step in enumerate(steps):
            if str(step.get("status") or "").strip().lower() == status:
                return index, step
    return -1, {}


def build_plan_state(
    task_id: str,
    plan_text: str,
    plan_file: str | Path | None = None,
    *,
    strict: bool = False,
) -> PlanState:
    created = _now()
    structured = parse_structured_plan(plan_text) if strict else {}
    raw_steps = structured.get("steps") if strict else parse_plan_steps(plan_text)
    state = {
        **_plan_defaults(task_id),
        "created_at": created,
        "updated_at": created,
        "plan_file": str(plan_file) if plan_file else "",
        "steps": [
            normalize_plan_step({**dict(step), "skills_declared": True}, index)
            for index, step in enumerate(raw_steps or [], 1)
            if isinstance(step, Mapping)
        ] if strict else raw_steps,
    }
    if strict:
        for step in state["steps"]:
            step.pop("semantic_constraint_ids", None)
            step.pop("semantic_claims", None)
            step.pop("semantic_claims_validation", None)
            step.pop("semantic_claims_invalid_raw", None)
    if not strict:
        state["schema_version"] = CANONICAL_TASK_SCHEMA_VERSION - 1
        state["plan_schema_version"] = CANONICAL_TASK_SCHEMA_VERSION - 1
        state["migration_version"] = CANONICAL_TASK_SCHEMA_VERSION - 1
    if strict:
        state["plan_document"] = {
            "schema_version": 2,
            "validation_checklist": deepcopy(list(structured.get("validation_checklist") or [])),
            "metadata": deepcopy(dict(structured.get("metadata") or {})),
        }
        state["plan_structure_status"] = "valid"
        state["proposed_artifacts"] = deepcopy(list(
            structured.get("proposed_artifacts") or []
        ))
        state["task_output_materializations"] = deepcopy(list(
            structured.get("task_output_materializations") or []
        ))
    state["semantic_plan_hash"] = semantic_plan_hash(state)
    return state


def _plan_spec_v2_record(state: PlanState) -> Dict[str, Any]:
    """Project the writable PlanSpec away from validation/runtime state."""
    return {
        key: deepcopy(state.get(key))
        for key in (
            "plan_id", "plan_version", "plan_revision", "generated_from_task_revision",
            "base_task_semantic_hash", "selected_skill_ids", "steps", "plan_document",
            "plan_artifacts", "artifact_allocation_mapping", "artifact_usage",
            "task_plan_traceability", "task_output_materializations",
            "plan_compilation_receipt", "step_effect_normalizations",
            "plan_resource_bindings", "compiled_dataflow",
            "plan_dataflow_receipt",
        )
        if state.get(key) not in (None, "", [], {})
    }


def load_current_plan_snapshot(
    data_path: str | Path,
    task_id: str,
    *,
    state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Load the immutable current PlanSnapshot without changing approval state."""
    current = dict(state or load_plan_state(data_path, task_id))
    current_plan = dict(current.get("current_plan") or {})
    current_approval = dict(current.get("current_approval") or {})
    refs = {
        str(value)
        for value in (
            current_approval.get("plan_snapshot_ref"),
            current_plan.get("plan_snapshot_ref"),
            current.get("plan_snapshot_ref"),
        )
        if str(value or "")
    }
    if not refs:
        return {}
    if len(refs) != 1:
        raise ValueError("CURRENT_PLAN_SNAPSHOT_REF_MISMATCH")
    snapshot_ref = next(iter(refs))
    snapshot = StateContractStore(data_path, task_id).read_object(
        snapshot_ref,
        expected_type="plan_snapshot_v121",
    )
    if not isinstance(snapshot, Mapping):
        raise ValueError("CURRENT_PLAN_SNAPSHOT_INVALID")
    assert_reader_compatible(snapshot)
    expected_id = str(current_plan.get("plan_snapshot_id") or "")
    observed_id = str(snapshot.get("plan_snapshot_id") or "")
    if expected_id and observed_id != expected_id:
        raise ValueError("CURRENT_PLAN_SNAPSHOT_ID_MISMATCH")
    return deepcopy(dict(snapshot))


def load_approved_plan_snapshot(
    data_path: str | Path,
    task_id: str,
    *,
    state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Load the immutable PlanSnapshot pinned by the current approval.

    Legacy approved plans may not have a content-addressed snapshot and return
    an empty mapping so their existing TaskSpec-only execution path remains
    available.  Once a snapshot ref exists, any approval/ref/identity mismatch
    is an internal contract conflict and must fail closed.
    """
    current = dict(state or load_plan_state(data_path, task_id))
    current_approval = dict(current.get("current_approval") or {})
    try:
        snapshot = load_current_plan_snapshot(data_path, task_id, state=current)
    except ValueError as exc:
        code = str(exc)
        if code.startswith("CURRENT_PLAN_SNAPSHOT_"):
            raise ValueError(code.replace("CURRENT_PLAN_", "APPROVED_PLAN_", 1)) from exc
        raise
    if not snapshot:
        return {}
    if str(current_approval.get("status") or "") != "approved":
        raise ValueError("APPROVED_PLAN_SNAPSHOT_NOT_APPROVED")
    return snapshot


def approval_applicability_v2(state: PlanState) -> Dict[str, Any]:
    """Dynamically evaluate an immutable ApprovalSnapshot."""
    current_approval = dict(state.get("current_approval") or {})
    current_plan = dict(state.get("current_plan") or {})
    if current_approval:
        if str(current_approval.get("status") or "") != "approved":
            return {
                "status": str(current_approval.get("status") or "missing"),
                "applicable": False,
            }
        if str(current_approval.get("plan_snapshot_ref") or "") != str(
            current_plan.get("plan_snapshot_ref") or ""
        ):
            return {"status": "snapshot_mismatch", "applicable": False}
    snapshot = state.get("approval_snapshot")
    if not isinstance(snapshot, Mapping) or not snapshot:
        return {"status": "missing", "applicable": False}
    if str(snapshot.get("decision") or "") != "approved":
        return {"status": "missing", "applicable": False}
    task_spec = _current_task_spec(state)
    if not task_spec:
        return {"status": "unverifiable", "applicable": False, "reason": "current_task_missing"}
    current_task_hash = task_semantic_hash(task_spec)
    current_plan_hash = "sha256:" + semantic_plan_hash(state).removeprefix("sha256:")
    if str(snapshot.get("approved_task_semantic_hash") or "") != current_task_hash:
        return {"status": "stale_task", "applicable": False}
    if str(snapshot.get("approved_plan_semantic_hash") or "") != current_plan_hash:
        return {"status": "stale_plan", "applicable": False}
    validation = dict(state.get("plan_validation_state") or {})
    if (
        str(validation.get("status") or "") != "valid"
        or str(validation.get("validated_plan_semantic_hash") or "") != current_plan_hash
    ):
        return {
            "status": "unverifiable",
            "applicable": False,
            "reason": "plan_validation_incomplete_or_stale",
        }
    scope = snapshot.get("approval_scope") or {}
    if isinstance(scope, Mapping) and str(scope.get("mode") or "full") != "full":
        recorded = str(scope.get("scope_hash") or "")
        current = str(build_approval_scope(state).get("approval_scope_hash") or "")
        if not recorded or recorded != current:
            return {"status": "scope_mismatch", "applicable": False}
    return {"status": "applicable", "applicable": True}


@timed_phase("state.canonical_projection")
def refresh_state_contract_v2(state: PlanState) -> PlanState:
    """Compile logical V2 partitions and stop persisting writable aliases."""
    current_value = state.get("current_task")
    if not isinstance(current_value, Mapping):
        try:
            source_schema = int(state.get("schema_version") or 1)
        except (TypeError, ValueError):
            source_schema = 1
        legacy_task = state.get("task_spec")
        if source_schema < CANONICAL_TASK_SCHEMA_VERSION and task_spec_is_initialized(legacy_task):
            current_value = _canonical_task_record(
                legacy_task,
                generation=int(state.get("state_generation") or 0),
            )
            state["current_task"] = current_value
        elif source_schema < CANONICAL_TASK_SCHEMA_VERSION:
            current_value = _plan_defaults(
                str(state.get("task_id") or "default")
            )["current_task"]
            state["current_task"] = current_value
        else:
            raise CanonicalTaskStateError(
                "CANONICAL_TASK_STATE_CORRUPT: V2 current_task partition is missing"
            )
    prior_current = dict(current_value)
    task_spec = prior_current.get("task_spec")
    try:
        current_revision = int(prior_current.get("task_revision") or 0)
    except (TypeError, ValueError) as exc:
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: task_revision is invalid"
        ) from exc
    if task_spec_is_initialized(task_spec):
        canonical = canonical_current_task_spec(task_spec)
        if current_revision != task_spec_revision(canonical):
            raise CanonicalTaskStateError(
                "CANONICAL_TASK_STATE_CORRUPT: TaskSpec payload/revision mismatch"
            )
        state["current_task"] = _canonical_task_record(
            canonical,
            previous=prior_current,
            generation=int(state.get("state_generation") or 0),
        )
    elif task_spec is None and current_revision == 0:
        state["current_task"] = {
            "task_spec": None,
            "task_revision": 0,
            "task_semantic_hash": "",
            "task_record_hash": "",
            "canonicalization_version": TASK_CANONICALIZATION_VERSION,
            "created_generation": int(
                prior_current.get("created_generation")
                or state.get("state_generation")
                or 0
            ),
            "last_mutation_event_id": str(
                prior_current.get("last_mutation_event_id") or ""
            ),
            "lifecycle": "uninitialized",
        }
    else:
        raise CanonicalTaskStateError(
            "CANONICAL_TASK_STATE_CORRUPT: task_spec == null iff task_revision == 0"
        )
    state.pop("task_spec", None)
    state.pop("task_semantic_hash", None)
    state.pop("task_record_hash", None)
    task_state = dict(state.get("task_state") or {})
    task_state.pop("task_spec", None)
    state["task_state"] = task_state
    plan_spec = _plan_spec_v2_record(state)
    if plan_spec.get("steps"):
        prior_plan_pointer = dict(state.get("current_plan") or {})
        semantic_hash = "sha256:" + semantic_plan_hash(state).removeprefix("sha256:")
        record_hash = plan_record_hash(plan_spec)
        state["current_plan"] = {
            "plan_spec": plan_spec,
            "plan_revision": int(state.get("plan_revision") or 1),
            "plan_semantic_hash": semantic_hash,
            "plan_record_hash": record_hash,
            "base_task_revision": int(state.get("generated_from_task_revision") or 0),
            "base_task_semantic_hash": str(
                state.get("base_task_semantic_hash")
                or (state.get("current_task") or {}).get("task_semantic_hash")
                or ""
            ),
            "created_generation": int(
                prior_plan_pointer.get("created_generation")
                or state.get("state_generation")
                or 0
            ),
            **{
                key: deepcopy(prior_plan_pointer[key])
                for key in (
                    "plan_snapshot_id", "plan_snapshot_ref",
                    "plan_contract_version", "minimum_reader_version",
                )
                if prior_plan_pointer.get(key) not in (None, "", [], {})
            },
        }
        state["plan_record_hash"] = record_hash
    blocking = [
        deepcopy(dict(item))
        for item in state.get("validation_issues", []) or []
        if isinstance(item, Mapping)
        and str(item.get("severity") or "").lower() in BLOCKING_VALIDATION_SEVERITIES
    ]
    plan_hash = str((state.get("current_plan") or {}).get("plan_semantic_hash") or "")
    state["plan_validation_state"] = {
        "status": "invalid" if blocking or state.get("validation_recompute_required") else "valid",
        "validated_plan_semantic_hash": (
            "" if state.get("validation_recompute_required") else plan_hash
        ),
        "validator_version": "plan-validator-v2",
        "violations": deepcopy(list(state.get("validation_issues") or [])),
        "validation_evidence_refs": deepcopy(
            list((state.get("plan_validation_state") or {}).get("validation_evidence_refs") or [])
        ),
        "validated_at": str(
            (state.get("plan_validation_state") or {}).get("validated_at")
            or state.get("updated_at")
            or ""
        ),
    }
    state["runtime_state"] = {
        **dict(state.get("runtime_state") or {}),
        "status": str(task_state.get("status") or state.get("task_status") or "running"),
        "current_step_id": str(state.get("current_step_id") or ""),
        "current_stage_id": str(state.get("current_stage_id") or ""),
        "budgets": deepcopy(dict(task_state.get("budgets") or {})),
        "execution_epoch": int(
            (state.get("runtime_state") or {}).get("execution_epoch") or 0
        ),
    }
    state["approval_gate_v2"] = approval_applicability_v2(state)
    compatibility = dict(state.get("compatibility") or {})
    compatibility.setdefault(
        "source_schema_version",
        int(state.get("schema_version") or PLAN_SCHEMA_VERSION),
    )
    compatibility["read_aliases_enabled"] = False
    state["compatibility"] = compatibility
    state["plan_contract_version"] = PLAN_CONTRACT_VERSION
    state["minimum_reader_version"] = minimum_reader_for_plan(state)
    return state


@timed_phase("state.load")
def load_plan_state(data_path: str | Path, task_id: str) -> PlanState:
    path = plan_state_path(data_path, task_id)
    if not path.exists():
        return _plan_defaults(task_id, data_path)
    try:
        with phase("state.read_json"):
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = _plan_defaults(task_id, data_path)
        state["load_error"] = "invalid_plan_state_json"
        return state
    if not isinstance(data, dict):
        data = {}
    try:
        _assert_plan_state_reader_compatible(data)
    except ReaderCompatibilityError as exc:
        # This is intentionally before canonical TaskSpec validation, legacy
        # externalization, migration, object dereference, and any persistence.
        # An old reader may report incompatibility but may not reinterpret or
        # rewrite a newer contract.
        data["load_error"] = str(exc)
        data["can_generate"] = False
        data["approval_gate_v2"] = {
            "status": "unverifiable",
            "applicable": False,
            "reason": "reader_incompatible",
        }
        return data
    try:
        source_schema_version = int(data.get("schema_version") or 1)
    except (TypeError, ValueError):
        source_schema_version = 1
    if source_schema_version >= CANONICAL_TASK_SCHEMA_VERSION:
        try:
            canonical_task_spec_for_state(data)
        except CanonicalTaskStateError as exc:
            corrupt = _plan_defaults(task_id, data_path)
            corrupt.update(data)
            corrupt["load_error"] = str(exc)
            corrupt["canonical_state_error"] = {
                "code": "CANONICAL_TASK_STATE_CORRUPT",
                "detail": str(exc),
            }
            corrupt["can_generate"] = False
            corrupt["approval_gate_v2"] = {
                "status": "unverifiable",
                "applicable": False,
                "reason": "canonical_task_state_corrupt",
            }
            return corrupt
    original_load_content = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    _externalize_legacy_task_spec_records(data_path, task_id, data)
    try:
        data, migrations = migrate_plan_state(data, task_id, data_path)
    except Exception as exc:
        fallback = _plan_defaults(task_id, data_path)
        fallback.update(data)
        fallback["load_error"] = f"plan_state_migration_failed:{type(exc).__name__}"
        return fallback
    manifest_events = reconcile_plan_state_manifests(data, data_path)
    if manifest_events:
        migrations = [*migrations, "reconcile_renderer_artifact_manifest"]
        applied = list(data.get("migrations_applied") or [])
        if "reconcile_renderer_artifact_manifest" not in applied:
            applied.append("reconcile_renderer_artifact_manifest")
        data["migrations_applied"] = applied
    records_path = plan_output_dir(data_path, task_id) / "executions" / "execution_records.jsonl"
    if records_path.is_file():
        records: List[Dict[str, Any]] = []
        for line in records_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        migration = migrate_execution_provenance(data, records)
        if migration.get("applied"):
            migrations = [*migrations, "migrate_execution_records_provenance"]
    try:
        refresh_state_contract_v2(data)
        canonical_task_spec_for_state(data)
    except CanonicalTaskStateError as exc:
        data["load_error"] = str(exc)
        data["canonical_state_error"] = {
            "code": "CANONICAL_TASK_STATE_CORRUPT",
            "detail": str(exc),
        }
        data["can_generate"] = False
        data["approval_gate_v2"] = {
            "status": "unverifiable",
            "applicable": False,
            "reason": "canonical_task_state_corrupt",
        }
        return data
    audit_index = data.get("audit_index")
    if isinstance(audit_index, Mapping) and audit_index.get("audit_head"):
        try:
            audit_store = StateContractStore(data_path, task_id)
            with phase("state.verify_chain"):
                audit_store.verify_chain(
                    audit_index,
                    expected_generation=int(data.get("state_generation") or 0),
                )
            raw_ref = str(audit_index.get("raw_submission_ref") or "")
            if raw_ref.startswith("object:sha256:"):
                audit_store.read_object(raw_ref, expected_type="raw_user_submission")
            conversation = data.get("conversation_evidence")
            if isinstance(conversation, Mapping) and conversation.get("head_ref"):
                with phase("state.conversation_evidence"):
                    audit_store.read_conversation_evidence(conversation)
            approval_ref = str(data.get("approval_snapshot_ref") or "")
            if approval_ref.startswith("object:sha256:"):
                persisted_snapshot = audit_store.read_object(
                    approval_ref,
                    expected_type="approval_snapshot",
                )
                if persisted_snapshot != data.get("approval_snapshot"):
                    raise ValueError("APPROVAL_SNAPSHOT_OBJECT_MISMATCH")
            plan_snapshot_ref = str(
                (data.get("current_plan") or {}).get("plan_snapshot_ref")
                or data.get("plan_snapshot_ref")
                or ""
            )
            if plan_snapshot_ref.startswith("object:sha256:"):
                persisted_plan_snapshot = audit_store.read_object(
                    plan_snapshot_ref,
                    expected_type="plan_snapshot_v121",
                )
                assert_reader_compatible(persisted_plan_snapshot)
                if str(persisted_plan_snapshot.get("plan_snapshot_id") or "") != str(
                    (data.get("current_plan") or {}).get("plan_snapshot_id") or ""
                ):
                    raise ValueError("PLAN_SNAPSHOT_POINTER_MISMATCH")
            plan_approval_ref = str(
                (data.get("current_approval") or {}).get("approval_record_ref")
                or data.get("approval_record_ref")
                or ""
            )
            if plan_approval_ref.startswith("object:sha256:"):
                persisted_plan_approval = audit_store.read_object(
                    plan_approval_ref,
                    expected_type="plan_approval_record_v121",
                )
                assert_reader_compatible(persisted_plan_approval)
                if str(persisted_plan_approval.get("plan_snapshot_ref") or "") != plan_snapshot_ref:
                    raise ValueError("PLAN_APPROVAL_SNAPSHOT_POINTER_MISMATCH")
                current_status = str(
                    (data.get("current_approval") or {}).get("status") or ""
                )
                if current_status and current_status != str(
                    persisted_plan_approval.get("status") or ""
                ):
                    raise ValueError("PLAN_APPROVAL_STATUS_POINTER_MISMATCH")
        except Exception as exc:
            data["load_error"] = f"state_contract_audit_invalid:{type(exc).__name__}"
            data["approval_gate_v2"] = {
                "status": "unverifiable",
                "applicable": False,
                "reason": "audit_chain_invalid",
            }
            data["can_generate"] = False
    if migrations and json.dumps(data, ensure_ascii=False, sort_keys=True, default=str) != original_load_content:
        try:
            _atomic_write_plan_state(
                path,
                data,
                allow_canonical_task_write=True,
            )
        except OSError as exc:
            data["load_error"] = f"plan_state_migration_persist_failed:{type(exc).__name__}"
    return data


def bind_plan_to_task_revision(state: PlanState, revision: int) -> PlanState:
    """Bind a generated plan and all of its steps to one effective task revision."""
    try:
        normalized_revision = max(0, int(revision))
    except (TypeError, ValueError):
        normalized_revision = 0
    state["generated_from_task_revision"] = normalized_revision
    task_spec = _current_task_spec(state)
    if task_spec:
        state["base_task_semantic_hash"] = task_semantic_hash(task_spec)
    for step in state.get("steps", []) or []:
        if isinstance(step, dict):
            step["generated_from_task_revision"] = normalized_revision
    return state


class PlannerAdmissionClosed(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("PLANNER_ADMISSION_CLOSED: " + reason)


def planner_admission_closed_reason(state: Mapping[str, Any]) -> str:
    """Existing terminal/cancellation facts revoke this task's Planner authority."""
    task = dict(state.get("task_state") or {})
    status = task_status(dict(state))
    if status in TASK_TERMINAL_STATUSES:
        return status
    revision = int((state.get("current_task") or {}).get("task_revision") or state.get("task_revision") or 0)
    cancellation = dict(task.get("cancellation_receipt") or {})
    if cancellation and int((cancellation.get("scope") or {}).get("task_revision") or 0) == revision:
        return "cancelled"
    failure = dict(task.get("planning_failure") or {})
    if failure.get("status") == "terminal" and int(failure.get("task_spec_revision") or 0) == revision:
        return "planning_terminal"
    return ""


def save_plan_state(
    data_path: str | Path,
    task_id: str,
    state: PlanState,
    *,
    allow_canonical_task_write: bool = False,
    step_effect_capability_registry: Any = None,
    require_planner_active: bool = False,
) -> Path:
    path = plan_state_path(data_path, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _normalize_in_memory_task_record(state)
    legacy_builder_import = bool(
        state.pop("_legacy_builder_task_import_pending", False)
    )
    _externalize_legacy_task_spec_records(data_path, task_id, state)
    state, migrations = migrate_plan_state(state, task_id, data_path)
    state["task_id"] = str(task_id or state.get("task_id") or "default")
    ensure_provenance_state(state)
    state["updated_at"] = _now()
    if not state.get("plan_file"):
        state["plan_file"] = str(default_plan_file(data_path, task_id))
    state["semantic_plan_hash"] = semantic_plan_hash(state) if state.get("steps") else ""
    expected_generation = int(state.get("state_generation") or 0)
    refresh_state_contract_v2(state)
    _atomic_write_plan_state(
        path,
        state,
        expected_generation=expected_generation,
        advance_generation=True,
        allow_canonical_task_write=(
            allow_canonical_task_write
            or legacy_builder_import
            or "promote_legacy_taskspec_to_current_task_v8" in migrations
        ),
        step_effect_capability_registry=step_effect_capability_registry,
        require_planner_active=require_planner_active,
    )
    return path


def recovery_decision_scope_mismatches(
    state: Mapping[str, Any],
    decision: Any,
) -> Dict[str, Dict[str, str]]:
    """Compare a finalized decision scope with the current durable plan.

    COMPLETE_TASK is anchored to the last Supervisor-verified step even though
    COMPLETE_STEP has already cleared the execution cursor.  That one shape is
    current (not stale) only when every durable step is already successful and
    the anchored step is one of those completed steps.
    """
    decision_value = (
        decision.to_dict()
        if hasattr(decision, "to_dict")
        else dict(decision or {})
    )
    scope = dict(decision_value.get("source_scope") or {})
    expected_scope = {
        "task_id": str(state.get("task_id") or ""),
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or ""),
        "step_id": str(state.get("current_step_id") or ""),
    }
    mismatches = {
        key: {"decision": str(scope.get(key) or ""), "current": current}
        for key, current in expected_scope.items()
        if str(scope.get(key) or "") != current
    }
    if (
        str(decision_value.get("kind") or "").upper() == "COMPLETE_TASK"
        and expected_scope["step_id"] == ""
        and "step_id" in mismatches
    ):
        steps = [
            item for item in state.get("steps", []) or []
            if isinstance(item, Mapping)
        ]
        completed_step_ids = {
            str(item.get("step_id") or "")
            for item in steps
            if str(item.get("status") or "") in STEP_SUCCESS_STATUSES
        }
        scoped_step_id = str(scope.get("step_id") or "")
        if (
            steps
            and len(completed_step_ids) == len(steps)
            and scoped_step_id in completed_step_ids
        ):
            mismatches.pop("step_id", None)
    return mismatches


def commit_recovery_bundle(
    data_path: str | Path,
    task_id: str,
    bundle: Any,
) -> Dict[str, Any]:
    """Atomically adopt one finalized Recovery bundle as PlanState truth."""
    from agent_core.recovery import RecoveryCommitBundle

    if not isinstance(bundle, RecoveryCommitBundle):
        raise TypeError("bundle must be a RecoveryCommitBundle")
    bundle.validate()
    decision = bundle.finalized_decision
    decision_value = decision.to_dict()
    current = load_plan_state(data_path, task_id)
    expected_generation = int(bundle.expected_state_generation)
    current_generation = int(current.get("state_generation") or 0)
    recovery_state = dict(
        ((current.get("task_state") or {}).get("recovery_state")) or {}
    )
    applied_ids = {
        str(item)
        for item in recovery_state.get("applied_decision_ids") or []
        if str(item)
    }
    if current_generation != expected_generation:
        if decision.decision_id in applied_ids:
            return {
                "applied": False,
                "duplicate": True,
                "decision_id": decision.decision_id,
                "next_route": "",
                "transition_status": "committed",
                "state_generation": current_generation,
            }
        raise StateCASConflict(
            "STATE_CAS_CONFLICT: expected generation "
            f"{expected_generation}, current generation {current_generation}"
        )

    scope = dict(decision.source_scope or {})
    mismatches = recovery_decision_scope_mismatches(current, decision)
    if mismatches:
        raise ValueError(
            "RECOVERY_BUNDLE_SCOPE_STALE: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    source_attempt_id = str(scope.get("failed_execution_attempt_id") or "")
    source_attempt = dict(
        ((current.get("provenance_state") or {}).get("attempts") or {}).get(
            source_attempt_id
        )
        or {}
    )
    if str(source_attempt.get("status") or "").upper() in {
        "SUPERSEDED", "INVALIDATED", "STALE"
    }:
        raise ValueError("RECOVERY_BUNDLE_SOURCE_ATTEMPT_STALE")

    candidate = deepcopy(current)
    envelope_value = (
        bundle.execution_envelope.to_dict()
        if bundle.execution_envelope is not None
        else {}
    )
    application = apply_recovery_decision(
        candidate,
        decision,
        execution_envelope=envelope_value,
    )
    task_state = dict(candidate.get("task_state") or {})
    committed_recovery = dict(task_state.get("recovery_state") or {})
    active = dict(committed_recovery.get("active_recovery") or {})
    # COMPLETE_STEP advances the plan cursor and may legitimately route to the
    # next Generate node, but it is a completion transition rather than an
    # active recovery generation.  apply_recovery_decision intentionally
    # clears active_recovery for completion decisions, so the recovery-envelope
    # identity postcondition applies only to repair/retry Generate transitions.
    if (
        decision.target_route == "generate"
        and str(decision.kind or "").upper() not in {"COMPLETE_STEP", "COMPLETE_TASK"}
        and (
        str(active.get("recovery_id") or "") != decision.decision_id
        or str(envelope_value.get("recovery_decision_id") or "")
        != decision.decision_id
        )
    ):
        raise ValueError("RECOVERY_BUNDLE_ACTIVE_IDENTITY_MISMATCH")
    committed_recovery["authoritative_commit"] = {
        "schema_version": 1,
        "decision": decision_value,
        "execution_envelope": envelope_value,
        "active_recovery": deepcopy(active),
        "transition": deepcopy(
            committed_recovery.get("active_transition") or {}
        ),
        "budget_reservation": deepcopy(
            dict(bundle.budget_reservation or {})
        ),
        "expected_state_generation": expected_generation,
        "committed_state_generation": expected_generation + 1,
    }
    task_state["recovery_state"] = committed_recovery
    candidate["task_state"] = task_state
    save_plan_state(data_path, task_id, candidate)
    application["state_generation"] = int(candidate.get("state_generation") or 0)
    application["commit_authority"] = "plan_state_cas"
    return application


def initialize_consultant_bootstrap_state(
    data_path: str | Path,
    task_id: str,
    *,
    raw_submission_ref: str,
    conversation_evidence: Mapping[str, Any],
) -> PlanState:
    """Create an explicit uninitialized Consultant-first durable task.

    Raw evidence is durable before the graph starts, but no parser-produced
    semantic object is promoted to canonical truth.  A later validated
    Consultant candidate performs the first TaskSpec commit.
    """
    store = StateContractStore(data_path, task_id)
    store.read_object(raw_submission_ref, expected_type="raw_user_submission")
    store.read_conversation_evidence(conversation_evidence)
    state = load_plan_state(data_path, task_id)
    if task_spec_is_initialized((state.get("current_task") or {}).get("task_spec")):
        return state
    state["current_task"] = {
        "task_spec": None,
        "task_revision": 0,
        "task_semantic_hash": "",
        "task_record_hash": "",
        "canonicalization_version": TASK_CANONICALIZATION_VERSION,
        "created_generation": int(state.get("state_generation") or 0),
        "last_mutation_event_id": "",
        "lifecycle": "uninitialized",
    }
    state["conversation_evidence"] = deepcopy(dict(conversation_evidence))
    audit_index = dict(state.get("audit_index") or {})
    audit_index["raw_submission_ref"] = str(raw_submission_ref)
    state["audit_index"] = audit_index
    save_plan_state(data_path, task_id, state)
    return load_plan_state(data_path, task_id)


def _accept_runtime_records(task_state, records, *, task_id):
    prior = dict(task_state.get("runtime_evidence") or {})
    merged = merge_runtime_evidence(prior.get("records") or [], records, task_id=task_id)
    if merged != list(prior.get("records") or []):
        task_state["runtime_evidence"] = runtime_evidence_state(
            merged, prior_revision=int(prior.get("revision") or 0)
        )
    # Compatibility projections are rebuilt from the evidence, never authority.
    if "runtime_evidence" in task_state:
        task_state.pop("runtime_audit", None)


def commit_runtime_evidence_state(data_path, task_id, records):
    """Accept inspection evidence with the existing task-state CAS boundary."""
    state = load_plan_state(data_path, task_id)
    task_state = deepcopy(dict(state.get("task_state") or {}))
    _accept_runtime_records(task_state, records, task_id=task_id)
    state["task_state"] = task_state
    save_plan_state(data_path, task_id, state)
    return load_plan_state(data_path, task_id)


def commit_consultant_candidate_state(
    data_path: str | Path,
    task_id: str,
    candidate: Mapping[str, Any],
) -> PlanState:
    """CAS-commit validated Consultant semantic and consultation progress."""
    proposed = dict(candidate or {})
    task_spec = proposed.get("task_spec")
    if not task_spec_is_initialized(task_spec):
        raise ValueError("TASKSPEC_BOOTSTRAP_INCOMPLETE: candidate is not initialized")
    state = load_plan_state(data_path, task_id)
    if state.get("canonical_state_error"):
        raise CanonicalTaskStateError(str(state["canonical_state_error"]))
    task_state = dict(state.get("task_state") or {})
    pending_skill_proposal = dict(
        task_state.get("pending_skill_binding_proposal") or {}
    )
    conversation = dict(state.get("conversation_evidence") or {})
    actual_preconditions = {
        "expected_state_generation": int(
            state.get("state_generation") or 0
        ),
        "expected_plan_revision": int(state.get("plan_revision") or 0),
        "expected_user_turn_revision": int(
            conversation.get("user_turn_count")
            if conversation.get("user_turn_count") is not None
            else conversation.get("turn_count") or 0
        ),
        "expected_pending_skill_proposal_id": str(
            pending_skill_proposal.get("proposal_id") or ""
        ),
        "expected_pending_skill_proposal_status": str(
            pending_skill_proposal.get("status") or ""
        ),
        "expected_pending_skill_snapshot_id": str(
            pending_skill_proposal.get("candidate_snapshot_id") or ""
        ),
    }
    for field_name, actual_value in actual_preconditions.items():
        if field_name not in proposed:
            continue
        expected_value = proposed.get(field_name)
        if str(expected_value) != str(actual_value):
            raise StateCASConflict(
                "CONSULTANT_SKILL_TRANSACTION_STALE: "
                f"{field_name}={expected_value!r}/{actual_value!r}"
            )
    current = canonical_task_spec_for_state(state)
    canonical = canonical_current_task_spec(task_spec)
    current_revision = task_spec_revision(current) if current else 0
    candidate_revision = task_spec_revision(canonical)
    semantic_changed = bool(
        not current or task_semantic_hash(current) != task_semantic_hash(canonical)
    )
    expected_revision = current_revision + 1 if semantic_changed else current_revision
    if candidate_revision != expected_revision:
        raise ValueError(
            "TASKSPEC_CANDIDATE_REVISION_INVALID: "
            f"expected={expected_revision}, candidate={candidate_revision}"
        )
    state["current_task"] = _canonical_task_record(
        canonical,
        previous=dict(state.get("current_task") or {}),
        generation=int(state.get("state_generation") or 0),
    )
    _accept_runtime_records(
        task_state, proposed.get("runtime_evidence_records") or [], task_id=task_id
    )
    task_state["consultation_state"] = deepcopy(dict(proposed.get("consultation_state") or {}))
    task_state["selected_skills"] = [
        str(value) for value in proposed.get("selected_skills", []) or [] if str(value)
    ]
    if "skill_access_state" in proposed:
        skill_access_state = proposed.get("skill_access_state")
        if not isinstance(skill_access_state, Mapping):
            raise ValueError("SKILL_ACCESS_STATE_INVALID")
        task_state["skill_access_state"] = deepcopy(dict(skill_access_state))
        selected_skill_refs = proposed.get("selected_skill_refs") or []
        if not isinstance(selected_skill_refs, list) or any(
            not isinstance(item, Mapping) for item in selected_skill_refs
        ):
            raise ValueError("SELECTED_SKILL_REFS_INVALID")
        task_state["selected_skill_refs"] = deepcopy(selected_skill_refs)
    for skill_transaction_field in (
        "pending_skill_binding_proposal",
        "pending_plan_impact_review",
    ):
        if skill_transaction_field not in proposed:
            continue
        value = proposed.get(skill_transaction_field)
        if value in (None, {}):
            task_state.pop(skill_transaction_field, None)
        elif isinstance(value, Mapping):
            task_state[skill_transaction_field] = deepcopy(dict(value))
        else:
            raise ValueError(
                f"{skill_transaction_field.upper()}_INVALID"
            )
    task_state["resolved_bindings"] = deepcopy(dict(proposed.get("resolved_bindings") or {}))
    pending = proposed.get("pending_question")
    task_state["pending_question"] = deepcopy(dict(pending or {}))
    accepted_handoff = proposed.get("accepted_consultant_handoff")
    if accepted_handoff is not None:
        if not isinstance(accepted_handoff, Mapping):
            raise ValueError("ACCEPTED_CONSULTANT_HANDOFF_INVALID")
        task_state["accepted_consultant_handoff"] = deepcopy(
            dict(accepted_handoff)
        )
    planner_feedback = proposed.get("planner_feedback")
    if planner_feedback is not None:
        if not isinstance(planner_feedback, Mapping):
            raise ValueError("PLANNER_FEEDBACK_PROPOSAL_INVALID")
        task_state["planner_feedback"] = deepcopy(dict(planner_feedback))
    for controller_field in (
        "reconciliation_ledger",
        "planner_reconciliation",
        "planning_epoch",
        "pending_review_classification",
        "binding_phase",
        "consultant_commit_rejection",
        "consultant_pending_transition",
        "consultant_no_progress_ledger",
    ):
        if controller_field in proposed:
            value = proposed.get(controller_field)
            if value in (None, {}):
                task_state.pop(controller_field, None)
            elif isinstance(value, Mapping):
                task_state[controller_field] = deepcopy(dict(value))
            else:
                raise ValueError(
                    f"CONTROLLER_TASK_STATE_FIELD_INVALID: {controller_field}"
                )
    state["task_state"] = task_state
    state["selected_skill_ids"] = list(task_state["selected_skills"])
    if "skill_access_state" in proposed:
        state["skill_access_state"] = deepcopy(
            dict(task_state.get("skill_access_state") or {})
        )
        state["selected_skill_refs"] = deepcopy(
            list(task_state.get("selected_skill_refs") or [])
        )
    approval_transition = proposed.get("approval_transition")
    if approval_transition:
        if not isinstance(approval_transition, Mapping):
            raise ValueError("PLAN_APPROVAL_TRANSITION_PROPOSAL_INVALID")
        transition_plan_approval_state(
            state,
            str(approval_transition.get("status") or ""),
            decision_source=str(
                approval_transition.get("decision_source")
                or "consultant_typed_review_classification"
            ),
            review_event_id=str(
                approval_transition.get("review_event_id") or ""
            ),
        )
    save_plan_state(
        data_path,
        task_id,
        state,
        allow_canonical_task_write=True,
    )
    return load_plan_state(data_path, task_id)


def update_task_state_projection(
    data_path: str | Path,
    task_id: str,
    updates: Dict[str, Any],
    *,
    node: str = "",
    next_step: str = "",
) -> PlanState:
    """Atomically persist the compact business state used by checkpoints/resume."""
    state = load_plan_state(data_path, task_id)
    task_state = dict(state.get("task_state") or {})
    projected = dict(updates or {})
    # Runtime projection has no canonical semantic write authority.  The sole
    # writer is commit_consultant_candidate_state().
    if "canonical_task_spec" in projected:
        raise ValueError("CANONICAL_TASKSPEC_WRITE_FORBIDDEN: projection writer")
    projected.pop("task_spec", None)
    projected.pop("runtime_evidence", None)
    projected.pop("runtime_recovery", None)
    if "runtime_evidence" in task_state:
        projected.pop("runtime_audit", None)
    for key, value in projected.items():
        if value is not None:
            task_state[str(key)] = deepcopy(value)
    task_state["state_version"] = int(task_state.get("state_version") or 0) + 1
    task_state["updated_at"] = _now()
    if node or next_step:
        transition = dict(task_state.get("last_transition") or {})
        transition.update({
            "node": str(node or transition.get("node") or ""),
            "next_step": str(next_step or transition.get("next_step") or ""),
            "at": _now(),
        })
        task_state["last_transition"] = transition
    state["task_state"] = task_state
    save_plan_state(data_path, task_id, state)
    return state


def _rewrite_usage_after_allocation(
    rows: Any,
    mapping: Mapping[str, str],
) -> List[Dict[str, Any]]:
    rewritten: List[Dict[str, Any]] = []
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        row = deepcopy(dict(raw))
        ref = dict(row.get("artifact_ref") or {})
        if ref.get("kind") == "candidate_local":
            local_ref = str(ref.get("identity") or "")
            logical_id = str(mapping.get(local_ref) or "")
            if not logical_id:
                raise ValueError(
                    f"ALLOCATION_MAPPING_INCOMPLETE: {local_ref}"
                )
            row["artifact_ref"] = {
                "kind": "authoritative",
                "identity": logical_id,
            }
        rewritten.append(row)
    return rewritten


def _immutable_plan_steps(state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        _semantic_step(dict(step))
        for step in state.get("steps", []) or []
        if isinstance(step, Mapping)
    ]


def _rewrite_plan_resource_bindings_after_allocation(
    resource_bindings: Any,
    allocation_mapping: Mapping[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Extend one compiled resource registry with allocated exact aliases.

    Allocation changes only the controller-owned handle used by persisted Plan
    steps.  It must not infer a new resource identity or silently recompile the
    Planner's explicit alias contract from filenames.  Every allocated handle
    is therefore added mechanically to the same canonical record that already
    contained its candidate-local handle.
    """

    source = {
        str(alias): deepcopy(dict(row))
        for alias, row in dict(resource_bindings or {}).items()
        if str(alias) and isinstance(row, Mapping)
    }
    if not source:
        return {}

    rows_by_identity: Dict[str, Dict[str, Any]] = {}
    aliases_by_identity: Dict[str, set[str]] = {}
    for alias, raw_row in source.items():
        row = deepcopy(raw_row)
        identity = str(row.get("canonical_resource_id") or "").strip()
        if not identity:
            raise ValueError(
                "PLAN_RESOURCE_BINDING_CANONICAL_ID_MISSING_AFTER_ALLOCATION"
            )
        aliases = {
            str(value)
            for value in row.get("aliases", []) or []
            if str(value)
        }
        aliases.update({alias, identity})
        if row.get("role_associations") and row.get("binding_source") == "candidate_local_artifact":
            allocated = {str(value) for local, value in allocation_mapping.items() if str(local) in aliases}
            if len(allocated) != 1:
                raise ValueError("PLAN_RESOURCE_ROLE_ALLOCATION_AMBIGUOUS")
            previous_identity = identity
            identity = next(iter(allocated))
            row["canonical_resource_id"] = identity
            aliases.discard(previous_identity)
            aliases.add(identity)
        for local_ref, allocated_ref in allocation_mapping.items():
            if str(local_ref) in aliases:
                aliases.add(str(allocated_ref))
        prior = rows_by_identity.get(identity)
        if prior is not None:
            comparable_prior = {
                key: value for key, value in prior.items() if key != "aliases"
            }
            comparable_row = {
                key: value for key, value in row.items() if key != "aliases"
            }
            if comparable_prior != comparable_row:
                raise ValueError(
                    "PLAN_RESOURCE_BINDING_CONFLICT_AFTER_ALLOCATION: "
                    + identity
                )
        else:
            rows_by_identity[identity] = row
        aliases_by_identity.setdefault(identity, set()).update(aliases)

    rewritten: Dict[str, Dict[str, Any]] = {}
    for identity in sorted(rows_by_identity):
        row = deepcopy(rows_by_identity[identity])
        row["aliases"] = sorted(aliases_by_identity[identity])
        binding_body = {
            key: deepcopy(value)
            for key, value in row.items()
            if key != "binding_id"
        }
        row["binding_id"] = "plan-resource-binding-" + hashlib.sha256(
            json.dumps(
                binding_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()[:20]
        for alias in row["aliases"]:
            rewritten[alias] = deepcopy(row)
    return {alias: rewritten[alias] for alias in sorted(rewritten)}


def _prepare_atomic_plan_objects(
    *,
    path: Path,
    state: PlanState,
    current_value: Mapping[str, Any],
    store: StateContractStore,
    current_generation: int,
    step_effect_capability_registry: Any = None,
) -> Dict[str, str]:
    """Prewrite one unreachable snapshot/approval pair under the state lock."""
    if state.get("allocation_intent") or state.get("plan_commit_requested"):
        reason = planner_admission_closed_reason(current_value)
        if reason:
            raise PlannerAdmissionClosed(reason)
    previous_plan_hash = str(
        ((current_value.get("current_plan") or {}).get("plan_semantic_hash")) or ""
    )
    intent = state.get("allocation_intent")
    allocation_mapping: Dict[str, str] = {}
    if isinstance(intent, Mapping) and intent:
        managed_root = str(
            path.parent / "plan_artifacts"
        )
        allocation_mapping, bindings = promote_allocation_intent(
            intent,
            task_id=str(state.get("task_id") or path.parent.name),
            managed_root=managed_root,
        )
        effect_mapping = dict(allocation_mapping)
        for local, allocated in allocation_mapping.items():
            resource = dict((state.get("plan_resource_bindings") or {}).get(local) or {})
            if resource.get("role_associations") and resource.get("binding_source") == "candidate_local_artifact":
                effect_mapping[str(resource["canonical_resource_id"])] = allocated
        rewritten = rewrite_candidate_local_refs(
            {"steps": state.get("steps", []) or []},
            effect_mapping,
        )
        state["steps"] = list(rewritten.get("steps") or [])
        state["artifact_usage"] = _rewrite_usage_after_allocation(
            state.get("artifact_usage"), allocation_mapping
        )
        artifacts = dict(state.get("plan_artifacts") or {})
        artifacts.update(bindings)
        state["plan_artifacts"] = artifacts
        state["artifact_allocation_mapping"] = {
            "candidate_scope_id": str(intent.get("candidate_scope_id") or ""),
            "planning_epoch_id": str(intent.get("planning_epoch_id") or ""),
            "local_to_authoritative": dict(sorted(allocation_mapping.items())),
        }
        if state.get("plan_resource_bindings") not in (None, {}, []):
            state["plan_resource_bindings"] = (
                _rewrite_plan_resource_bindings_after_allocation(
                    state.get("plan_resource_bindings"), allocation_mapping
                )
            )
        state.pop("allocation_intent", None)
        state.pop("proposed_artifacts", None)
        state.pop("candidate_scope_id", None)
        state.pop("planning_epoch_id", None)
        if step_effect_capability_registry is not None:
            # Allocation replaces candidate-local artifact identities while
            # the PlanState lock is held. Re-run the idempotent Plan
            # canonicalizer before sealing: it synchronizes derived aliases
            # such as input/output_contracts and provenance artifact IDs with
            # the newly authoritative handles.  Otherwise load_plan_state()
            # performs that synchronization later and immediately makes the
            # just-written receipt stale.
            migrate_plan_state(
                state,
                str(state.get("task_id") or path.parent.name),
                path.parents[2],
            )
            # Seal only after every allocation-derived semantic field is in
            # its final persisted form, so PlanReview validates the exact
            # committed Plan rather than a pre-allocation projection.
            from agent_core.step_effects import (
                compile_plan_dataflow,
                compile_plan_step_effects,
            )

            resource_bindings = {
                str(alias): deepcopy(dict(row))
                for alias, row in dict(
                    state.get("plan_resource_bindings") or {}
                ).items()
                if isinstance(row, Mapping)
            }

            compiled, receipt, issues = compile_plan_step_effects(
                state,
                step_effect_capability_registry,
                allow_legacy_untyped=False,
                resource_bindings=resource_bindings or None,
            )
            dataflow_receipt: Dict[str, Any] = {}
            if not issues:
                # An empty registry is still an authoritative typed dataflow
                # (for example an observation-only step), not a legacy signal.
                compiled, dataflow_receipt, dataflow_issues = (
                    compile_plan_dataflow(
                        compiled,
                        step_effect_capability_registry,
                        resource_bindings=resource_bindings,
                        compile_effect_contracts=False,
                        allow_legacy_untyped=False,
                    )
                )
                issues.extend(dataflow_issues)
            if issues:
                codes = sorted({
                    str(item.get("code") or "PLAN_COMPILATION_INVALID")
                    for item in issues
                    if isinstance(item, Mapping)
                })
                raise ValueError(
                    "PLAN_POST_ALLOCATION_COMPILATION_INVALID: "
                    + ",".join(codes)
                )
            state.clear()
            state.update(compiled)
            state["plan_compilation_receipt"] = deepcopy(receipt)
            state["plan_dataflow_receipt"] = deepcopy(dataflow_receipt)
        state["semantic_plan_hash"] = semantic_plan_hash(state)
        refresh_state_contract_v2(state)

    next_plan_hash = str(
        ((state.get("current_plan") or {}).get("plan_semantic_hash")) or ""
    )
    valid_plan = bool(
        state.get("steps")
        and str((state.get("plan_validation_state") or {}).get("status") or "") == "valid"
    )
    result = {
        "plan_snapshot_ref": str(state.get("plan_snapshot_ref") or ""),
        "approval_record_ref": str(state.get("approval_record_ref") or ""),
    }
    if (
        previous_plan_hash != next_plan_hash
        and valid_plan
        and state.get("plan_commit_requested") is True
    ):
        task_record = dict(state.get("current_task") or {})
        parent_ref = str(
            (current_value.get("current_plan") or {}).get("plan_snapshot_ref")
            or current_value.get("plan_snapshot_ref")
            or ""
        )
        traceability = typed_task_plan_traceability(
            dict(task_record.get("task_spec") or {}),
            list(state.get("steps") or []),
        )
        snapshot_identity_payload = {
            "task_id": str(state.get("task_id") or ""),
            "task_semantic_hash": str(task_record.get("task_semantic_hash") or ""),
            "task_revision": int(task_record.get("task_revision") or 0),
            "plan_semantic_hash": next_plan_hash,
            "plan_revision": int(state.get("plan_revision") or 1),
            "parent_plan_snapshot_ref": parent_ref,
        }
        snapshot_id = "plan-snapshot-" + _stable_payload_hash(
            snapshot_identity_payload
        )[:24]
        snapshot = {
            "schema_version": 1,
            "plan_contract_version": PLAN_CONTRACT_VERSION,
            "minimum_reader_version": minimum_reader_for_plan(state),
            "plan_snapshot_id": snapshot_id,
            **snapshot_identity_payload,
            "plan_record_hash": str(
                (state.get("current_plan") or {}).get("plan_record_hash") or ""
            ),
            "steps": _immutable_plan_steps(state),
            "resolved_artifact_refs": deepcopy(list(state.get("artifact_usage") or [])),
            "plan_local_artifacts": deepcopy(dict(state.get("plan_artifacts") or {})),
            **({"plan_resource_bindings": deepcopy(state["plan_resource_bindings"]), "resource_role_protocol_version": 1}
               if any(row.get("role_associations") for row in dict(state.get("plan_resource_bindings") or {}).values()) else {}),
            "artifact_allocation_mapping": deepcopy(dict(
                state.get("artifact_allocation_mapping") or {}
            )),
            "capability_bindings": deepcopy(list(state.get("selected_skill_ids") or [])),
            "validation": deepcopy(dict(state.get("plan_validation_state") or {})),
            "task_plan_traceability": traceability,
            "created_generation": int(state.get("state_generation") or current_generation),
            "created_at": _now(),
        }
        snapshot_ref = store.put_object("plan_snapshot_v121", snapshot)
        pending_id = "approval-" + _stable_payload_hash({
            "plan_snapshot_ref": snapshot_ref,
            "status": "pending",
        })[:24]
        pending_approval = {
            "schema_version": 1,
            "plan_contract_version": PLAN_CONTRACT_VERSION,
            "minimum_reader_version": minimum_reader_for_plan(state),
            "approval_id": pending_id,
            "plan_snapshot_id": snapshot_id,
            "plan_snapshot_ref": snapshot_ref,
            "status": "pending",
            "decision_source": "human_plan_review",
            "review_event_id": "",
            "approved_task_revision": None,
            "created_at": _now(),
            "decided_at": None,
        }
        approval_ref = store.put_object(
            "plan_approval_record_v121", pending_approval
        )
        current_plan = dict(state.get("current_plan") or {})
        current_plan.update({
            "plan_snapshot_id": snapshot_id,
            "plan_snapshot_ref": snapshot_ref,
            "plan_contract_version": PLAN_CONTRACT_VERSION,
            "minimum_reader_version": minimum_reader_for_plan(state),
        })
        state["current_plan"] = current_plan
        state["plan_snapshot_ref"] = snapshot_ref
        state["approval_record"] = pending_approval
        state["approval_record_ref"] = approval_ref
        state["current_approval"] = {
            "approval_id": pending_id,
            "approval_record_ref": approval_ref,
            "plan_snapshot_ref": snapshot_ref,
            "status": "pending",
        }
        state["approval_snapshot"] = {}
        state.pop("approval_snapshot_ref", None)
        state["approval_status"] = "awaiting_review"
        state["plan_status"] = PLAN_STATUS_PENDING_REVIEW
        state["can_generate"] = False
        state.pop("plan_commit_requested", None)
        task_state = dict(state.get("task_state") or {})
        epoch = dict(task_state.get("planning_epoch") or {})
        if epoch:
            epoch["status"] = "plan_snapshot_committed"
            epoch["committed_plan_snapshot_ref"] = snapshot_ref
            task_state["planning_epoch"] = epoch
            state["task_state"] = task_state
        result.update({
            "plan_snapshot_ref": snapshot_ref,
            "approval_record_ref": approval_ref,
        })
        return result

    approval = state.get("approval_record")
    current_snapshot_ref = str(
        (state.get("current_plan") or {}).get("plan_snapshot_ref")
        or state.get("plan_snapshot_ref")
        or ""
    )
    if isinstance(approval, Mapping) and approval and current_snapshot_ref:
        approval_record = deepcopy(dict(approval))
        approval_record.setdefault("schema_version", 1)
        approval_record.setdefault("plan_contract_version", PLAN_CONTRACT_VERSION)
        approval_record.setdefault("minimum_reader_version", MINIMUM_PLAN_READER_VERSION)
        approval_record.setdefault("plan_snapshot_ref", current_snapshot_ref)
        approval_record.setdefault(
            "plan_snapshot_id",
            str((state.get("current_plan") or {}).get("plan_snapshot_id") or ""),
        )
        approval_record.setdefault(
            "status",
            "approved" if state.get("plan_status") == PLAN_STATUS_APPROVED else "pending",
        )
        approval_ref = store.put_object(
            "plan_approval_record_v121", approval_record
        )
        state["approval_record"] = approval_record
        state["approval_record_ref"] = approval_ref
        state["current_approval"] = {
            "approval_id": str(approval_record.get("approval_id") or ""),
            "approval_record_ref": approval_ref,
            "plan_snapshot_ref": current_snapshot_ref,
            "status": str(approval_record.get("status") or ""),
        }
        result["approval_record_ref"] = approval_ref
    return result


def _atomic_write_plan_state(
    path: Path,
    state: PlanState,
    *,
    expected_generation: Optional[int] = None,
    advance_generation: bool = False,
    allow_canonical_task_write: bool = False,
    step_effect_capability_registry: Any = None,
    require_planner_active: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        with phase("state.lock_wait"):
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        current_generation = 0
        current_value: Mapping[str, Any] = {}
        if path.is_file():
            try:
                current_value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current_value, Mapping):
                    raise PermanentInfrastructureError(
                        "PLAN_STATE_ROOT_INVALID",
                        "authoritative root must be a JSON object",
                    )
                current_generation = int(current_value.get("state_generation") or 0)
            except TimeoutError as exc:
                raise TransientInfrastructureError(
                    "PLAN_STATE_READ_TIMEOUT", type(exc).__name__
                ) from exc
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise PermanentInfrastructureError(
                    "PLAN_STATE_ROOT_INVALID", type(exc).__name__
                ) from exc
        # Under the existing atomic admission lock, cancellation/terminal state
        # takes precedence over a generation conflict. No allocation or pending
        # approval object is written before this check.
        if require_planner_active or state.get("allocation_intent") or state.get("plan_commit_requested"):
            reason = planner_admission_closed_reason(current_value)
            if reason:
                raise PlannerAdmissionClosed(reason)
        if expected_generation is not None and int(expected_generation) != current_generation:
            raise StateCASConflict(
                "STATE_CAS_CONFLICT: expected generation "
                f"{int(expected_generation)}, current generation {current_generation}"
            )
        if advance_generation:
            state["state_generation"] = current_generation + 1
            refresh_state_contract_v2(state)
        current_value = current_value if isinstance(current_value, Mapping) else {}
        previous_task_hash = str(
            ((current_value.get("current_task") or {}).get("task_semantic_hash"))
            or ""
        )
        previous_plan_hash = str(
            ((current_value.get("current_plan") or {}).get("plan_semantic_hash"))
            or ""
        )
        next_task_hash = str(
            ((state.get("current_task") or {}).get("task_semantic_hash"))
            or ""
        )
        if previous_task_hash != next_task_hash and not allow_canonical_task_write:
            raise ValueError(
                "CANONICAL_TASKSPEC_WRITE_FORBIDDEN: use Consultant Controller CAS commit"
            )
        previous_index = dict(current_value.get("audit_index") or {})
        candidate_index = dict(state.get("audit_index") or {})
        store = StateContractStore(
            path.parents[2], str(state.get("task_id") or path.parent.name)
        )
        committed_refs = _prepare_atomic_plan_objects(
            path=path,
            state=state,
            current_value=current_value,
            store=store,
            current_generation=current_generation,
            step_effect_capability_registry=step_effect_capability_registry,
        )
        # Allocation may rewrite candidate-local refs. Recompute the hashes
        # only after that promotion has completed inside this lock.
        next_task_hash = str(
            ((state.get("current_task") or {}).get("task_semantic_hash")) or ""
        )
        next_plan_hash = str(
            ((state.get("current_plan") or {}).get("plan_semantic_hash"))
            or ""
        )
        previous_approval = str(
            ((current_value.get("current_approval") or {}).get("approval_record_ref"))
            or current_value.get("approval_record_ref")
            or ((current_value.get("approval_snapshot") or {}).get("approval_id"))
            or ""
        )
        next_approval = str(
            ((state.get("current_approval") or {}).get("approval_record_ref"))
            or state.get("approval_record_ref")
            or ((state.get("approval_snapshot") or {}).get("approval_id"))
            or ""
        )
        if previous_task_hash != next_task_hash:
            audit_type = "task_mutation"
        elif previous_plan_hash != next_plan_hash:
            audit_type = "plan_mutation"
        elif previous_approval != next_approval:
            audit_type = "approval"
        elif not current_value:
            audit_type = "state_created"
        else:
            audit_type = "runtime_event"
        approval_snapshot_ref = ""
        if isinstance(state.get("approval_snapshot"), Mapping) and state.get("approval_snapshot"):
            approval_snapshot_ref = store.put_object(
                "approval_snapshot",
                dict(state["approval_snapshot"]),
            )
            state["approval_snapshot_ref"] = approval_snapshot_ref
        else:
            state.pop("approval_snapshot_ref", None)
        event_ref = store.append_event(
            event_type=audit_type,
            previous_event_ref=str(previous_index.get("audit_head") or ""),
            payload={
                "generation_before": current_generation,
                "generation_after": int(state.get("state_generation") or current_generation),
                "task_semantic_hash_before": previous_task_hash,
                "task_semantic_hash_after": next_task_hash,
                "plan_semantic_hash_before": previous_plan_hash,
                "plan_semantic_hash_after": next_plan_hash,
                "approval_id_before": previous_approval,
                "approval_id_after": next_approval,
                "approval_snapshot_ref_after": approval_snapshot_ref,
                "plan_snapshot_ref_after": str(
                    committed_refs.get("plan_snapshot_ref")
                    or state.get("plan_snapshot_ref")
                    or ""
                ),
                "approval_record_ref_after": str(
                    committed_refs.get("approval_record_ref")
                    or state.get("approval_record_ref")
                    or ""
                ),
                "task_source_object_refs_after": [
                    str(value)
                    for value in (
                        ((state.get("current_task") or {}).get("task_spec") or {}).get(
                            "source_refs", []
                        )
                        or []
                    )
                    if str(value).startswith("object:sha256:")
                ],
            },
        )
        raw_submission_ref = str(
            ((state.get("current_task") or {}).get("task_spec") or {}).get(
                "raw_submission_ref"
            )
            or previous_index.get("raw_submission_ref")
            or candidate_index.get("raw_submission_ref")
            or ""
        )
        state["audit_index"] = store.next_index(
            previous_index,
            event_ref=event_ref,
            event_type=audit_type,
            raw_submission_ref=raw_submission_ref,
        )
        if state.get("current_task"):
            state["current_task"]["last_mutation_event_id"] = (
                event_ref
                if audit_type == "task_mutation"
                else str((current_value.get("current_task") or {}).get("last_mutation_event_id") or "")
            )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                with phase("state.serialize"):
                    encoded = json.dumps(state, ensure_ascii=False, indent=2)
                with phase("state.write"):
                    handle.write(encoded)
                    handle.flush()
                with phase("state.fsync_file"):
                    os.fsync(handle.fileno())
            with phase("state.replace"):
                os.replace(temporary, path)
            try:
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    with phase("state.fsync_directory"):
                        os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def ensure_plan_state(
    data_path: str | Path,
    task_id: str,
    plan_text: str | None = None,
    plan_file: str | Path | None = None,
) -> PlanState:
    state = load_plan_state(data_path, task_id)
    if state.get("steps"):
        if plan_file and not state.get("plan_file"):
            state["plan_file"] = str(plan_file)
        return state
    if plan_text and str(plan_text).strip() and str(plan_text).strip() != "No plan.":
        state = build_plan_state(task_id, str(plan_text), plan_file=plan_file or default_plan_file(data_path, task_id))
        # This entry point predates Consultant-first canonical state and accepts
        # only natural-language plan prose.  Persist it as an explicit legacy
        # import so compatibility readers remain available without weakening
        # V2 fail-closed semantics.
        state["schema_version"] = PLAN_SCHEMA_VERSION - 1
        state["plan_schema_version"] = PLAN_SCHEMA_VERSION - 1
        state["migration_version"] = PLAN_SCHEMA_VERSION - 1
        save_plan_state(data_path, task_id, state)
    return state


def reset_plan_state(data_path: str | Path, task_id: str) -> None:
    path = plan_state_path(data_path, task_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def reset_plan_for_replanning(data_path: str | Path, task_id: str) -> PlanState:
    """Clear PlanSpec/approval state while preserving task and evidence state."""
    state = load_plan_state(data_path, task_id)
    if state.get("canonical_state_error"):
        raise CanonicalTaskStateError(str(state["canonical_state_error"]))
    canonical_task_spec_for_state(state)
    fresh = _plan_defaults(task_id, data_path)
    reset_keys = {
        "plan_id", "plan_version", "plan_revision", "plan_status",
        "revision_status", "approval_status", "execution_status",
        "current_effective_plan_version", "semantic_plan_hash",
        "approved_plan_hash", "approved_plan_revision",
        "approved_task_revision", "approval_timestamp", "approval_record",
        "approval_projection", "approval_scope", "approval_snapshot",
        "approval_snapshot_ref", "approval_gate", "approval_gate_v2",
        "validation_issues", "blocking_validation_issue_fingerprints",
        "renderer_projections", "renderer_projection_issues",
        "validation_convergence_fingerprint", "validation_convergence_history",
        "repair_convergence", "can_request_approval", "can_generate",
        "generated_from_task_revision", "base_task_semantic_hash",
        "parent_plan_version", "current_plan", "plan_validation_state",
        "current_step_id", "execution_cursor_step_id", "current_stage_id",
        "revision_history", "stale_step_ids", "revalidation_required_step_ids",
        "pending_review_diff", "steps",
    }
    for key in reset_keys:
        if key in fresh:
            state[key] = deepcopy(fresh[key])
        else:
            state.pop(key, None)
    state["plan_file"] = str(default_plan_file(data_path, task_id))
    save_plan_state(data_path, task_id, state)
    return load_plan_state(data_path, task_id)


def _append_evidence(step: PlanStep, event: str, detail: str = "") -> None:
    evidence = step.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    evidence.append(
        {
            "event": str(event),
            "detail": str(detail)[:2000],
            "at": _now(),
        }
    )
    step["evidence"] = evidence[-20:]
    step["updated_at"] = _now()


def _first_step_by_status(state: PlanState, statuses: Tuple[str, ...]) -> Optional[PlanStep]:
    for step in state.get("steps", []) or []:
        if isinstance(step, dict) and step.get("status") in statuses:
            return step
    return None


def restore_step_status_authority(
    state: PlanState,
    expected_statuses: Dict[str, str],
    *,
    allow_controller_start: bool = True,
) -> Dict[str, str]:
    """Undo unauthorized external step-status mutations in one PlanTracker write."""
    attempted: Dict[str, str] = {}
    for step in state.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id") or "")
        if step_id not in expected_statuses:
            continue
        current = str(step.get("status") or "")
        expected = str(expected_statuses[step_id])
        if current == expected:
            continue
        if allow_controller_start and expected in {STATUS_PENDING, STATUS_READY} and current == STATUS_RUNNING:
            continue
        attempted[step_id] = current
        step["status"] = expected
        _append_evidence(step, "authority_violation_restored", f"attempted_status={current}")
    if attempted:
        state["updated_at"] = _now()
    return attempted


def mark_first_pending_running(state: PlanState, evidence: str = "") -> bool:
    if _first_step_by_status(state, tuple(STEP_ACTIVE_STATUSES)):
        return False
    step = _first_step_by_status(state, (STATUS_READY, STATUS_PENDING))
    if not step:
        return False
    step["status"] = STATUS_RUNNING
    state["current_step_id"] = str(step.get("step_id") or "")
    state["current_stage_id"] = ""
    _append_evidence(step, "started", evidence)
    state["updated_at"] = _now()
    return True


def mark_plan_step_done(state: PlanState, evidence: str = "") -> bool:
    step = _first_step_by_status(state, tuple(STEP_ACTIVE_STATUSES))
    if not step:
        return False
    step["status"] = STATUS_DONE
    _append_evidence(step, "done", evidence)
    state["updated_at"] = _now()
    return True


def reopen_last_done_step(state: PlanState, evidence: str = "") -> bool:
    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    for step in reversed(steps):
        if step.get("status") not in {STATUS_DONE, STATUS_PASSED}:
            continue
        step["status"] = STATUS_RUNNING
        state["current_step_id"] = str(step.get("step_id") or "")
        state["current_stage_id"] = ""
        _append_evidence(step, "reopened", evidence)
        set_task_status(state, TASK_STATUS_RUNNING, evidence)
        state["updated_at"] = _now()
        return True
    return False


def mark_plan_step_blocked(state: PlanState, evidence: str = "") -> bool:
    step = _first_step_by_status(state, tuple(STEP_ACTIVE_STATUSES))
    if not step:
        return False
    step["status"] = STATUS_BLOCKED
    _append_evidence(step, "blocked", evidence)
    state["updated_at"] = _now()
    return True


def task_status(state: PlanState) -> str:
    """Return the durable task status stored with the plan checkpoint."""
    task_state = state.get("task_state") if isinstance(state.get("task_state"), dict) else {}
    return str(task_state.get("status") or TASK_STATUS_RUNNING).strip().lower()


def set_task_status(state: PlanState, status: str, reason: str = "") -> bool:
    """Set one formal task status without creating a parallel state store."""
    normalized = str(status or "").strip().lower()
    allowed = {
        TASK_STATUS_RUNNING,
        TASK_STATUS_WAITING_USER,
        TASK_STATUS_DIAGNOSING,
        TASK_STATUS_REPAIRING,
        *TASK_TERMINAL_STATUSES,
    }
    if normalized not in allowed:
        raise ValueError(f"Unsupported task status: {status}")
    task_state = dict(state.get("task_state") or {})
    changed = str(task_state.get("status") or "") != normalized
    task_state["status"] = normalized
    if reason:
        task_state["status_reason"] = str(reason)[:4000]
    task_state["status_updated_at"] = _now()
    state["task_state"] = task_state
    state["updated_at"] = _now()
    return changed


def _append_active_recovery_history(
    recovery: Dict[str, Any],
    item: Mapping[str, Any],
) -> None:
    value = deepcopy(dict(item or {}))
    recovery_id = str(value.get("recovery_id") or "")
    status = str(value.get("status") or "")
    reason = str(value.get("closed_reason") or "")
    history = list(recovery.get("active_recovery_history") or [])
    if not any(
        isinstance(prior, Mapping)
        and str(prior.get("recovery_id") or "") == recovery_id
        and str(prior.get("status") or "") == status
        and str(prior.get("closed_reason") or "") == reason
        for prior in history
    ):
        history.append(value)
    recovery["active_recovery_history"] = history


def normalize_active_recovery_state(state: PlanState) -> Dict[str, Any]:
    """Migrate or retire active recovery without consulting an LLM.

    A legacy flat recovery decision is promoted only when it is explicitly
    pending and matches the current Plan/Step.  Every other legacy decision is
    retained as history only.
    """
    task_state = dict(state.get("task_state") or {})
    recovery = dict(task_state.get("recovery_state") or {})
    active_field_present = "active_recovery" in recovery
    active = dict(recovery.get("active_recovery") or {})
    current_step_id = str(state.get("current_step_id") or "")
    plan_revision = int(state.get("plan_revision") or 0)
    plan_version = str(state.get("plan_version") or "")
    events_before = len(list(recovery.get("state_events") or []))
    changed = False

    def step_status(step_id: str) -> str:
        return next(
            (
                str(item.get("status") or "").strip().lower()
                for item in state.get("steps", []) or []
                if isinstance(item, Mapping)
                and str(item.get("step_id") or "") == str(step_id or "")
            ),
            "",
        )

    def record_event(code: str, value: Mapping[str, Any]) -> None:
        nonlocal changed
        recovery_id = str(
            value.get("recovery_id")
            or value.get("decision_id")
            or value.get("idempotency_key")
            or ""
        )
        identity = {
            "code": code,
            "recovery_id": recovery_id,
            "active_step_id": str(value.get("step_id") or ""),
            "current_step_id": current_step_id,
            "plan_revision": plan_revision,
        }
        event_id = "active-state-" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        events = list(recovery.get("state_events") or [])
        if not any(
            isinstance(item, Mapping)
            and str(item.get("event_id") or "") == event_id
            for item in events
        ):
            events.append({**identity, "event_id": event_id, "recorded_at": _now()})
            recovery["state_events"] = events
            changed = True

    if not active and not active_field_present:
        legacy = (
            dict(state.get("recovery_decision") or {})
            if isinstance(state.get("recovery_decision"), Mapping)
            else {}
        )
        if legacy:
            recovery_id = str(
                legacy.get("recovery_id")
                or legacy.get("decision_id")
                or legacy.get("idempotency_key")
                or ""
            )
            legacy_step_id = str(legacy.get("step_id") or "")
            legacy_status = str(legacy.get("status") or "").strip().lower()
            legacy_target = str(
                legacy.get("target_route") or legacy.get("next_route") or ""
            ).strip().lower()
            legacy_plan_version = str(legacy.get("plan_version") or "")
            try:
                legacy_plan_revision = int(legacy.get("plan_revision") or 0)
            except (TypeError, ValueError):
                legacy_plan_revision = 0
            plan_matches = bool(
                (not legacy_plan_revision or legacy_plan_revision == plan_revision)
                and (not legacy_plan_version or legacy_plan_version == plan_version)
            )
            decision_history = list(recovery.get("decision_history") or [])
            if recovery_id and not any(
                isinstance(item, Mapping)
                and str(item.get("decision_id") or item.get("idempotency_key") or "")
                == recovery_id
                for item in decision_history
            ):
                decision_history.append(deepcopy(legacy))
                recovery["decision_history"] = decision_history
                changed = True
            if (
                recovery_id
                and legacy_status == "pending"
                and legacy_target == "generate"
                and legacy_step_id == current_step_id
                and plan_matches
                and step_status(legacy_step_id) not in STEP_SUCCESS_STATUSES | STEP_TERMINAL_STATUSES
            ):
                envelope = (
                    dict(state.get("recovery_execution_envelope") or {})
                    if isinstance(state.get("recovery_execution_envelope"), Mapping)
                    else {}
                )
                active = {
                    "schema_version": 1,
                    "recovery_id": recovery_id,
                    "plan_revision": plan_revision,
                    "step_id": legacy_step_id,
                    "source_attempt_id": str(
                        legacy.get("attempt_id") or legacy.get("trigger_event_id") or ""
                    ),
                    "status": "pending",
                    "target_route": "generate",
                    "envelope_id": str(envelope.get("immutable_hash") or ""),
                    "migrated_from": "legacy_global_recovery_decision",
                    "created_at": _now(),
                }
                recovery["active_recovery"] = active
                recovery["active_recovery_envelope"] = envelope
                record_event("LEGACY_ACTIVE_RECOVERY_MIGRATED", active)
                changed = True
            else:
                recovery["active_recovery"] = {}
                record_event("LEGACY_RECOVERY_RETAINED_AS_HISTORY", legacy)
                changed = True
            state["recovery_decision"] = {}

    if active:
        active_step_id = str(active.get("step_id") or "")
        try:
            active_plan_revision = int(active.get("plan_revision") or 0)
        except (TypeError, ValueError):
            active_plan_revision = 0
        scope_mismatch = active_step_id != current_step_id
        revision_mismatch = active_plan_revision != plan_revision
        old_step_terminal = step_status(active_step_id) in (
            STEP_SUCCESS_STATUSES | STEP_TERMINAL_STATUSES
        )
        if revision_mismatch or (scope_mismatch and old_step_terminal):
            retired = deepcopy(active)
            retired.update({
                "status": "stale",
                "closed_reason": (
                    "PLAN_REVISION_CHANGED"
                    if revision_mismatch
                    else "STEP_ADVANCED_AFTER_RECOVERY"
                ),
                "closed_at": _now(),
            })
            _append_active_recovery_history(recovery, retired)
            recovery["active_recovery"] = {}
            recovery["active_recovery_envelope"] = {}
            task_state["generation_provenance"] = normal_generation_provenance(
                state,
                source_route="stale_state_normalization",
            ).to_dict()
            record_event("STALE_ACTIVE_RECOVERY_CLOSED", retired)
            active = {}
            changed = True
        elif scope_mismatch:
            raise ActiveStateInvariantError(
                "ACTIVE_RECOVERY_SCOPE_MISMATCH",
                (
                    f"active recovery step {active_step_id} is not terminal "
                    f"while current step is {current_step_id}"
                ),
            )

    if "active_recovery" not in recovery:
        recovery["active_recovery"] = {}
        changed = True
    recovery.setdefault("active_recovery_history", [])
    task_state["recovery_state"] = recovery
    state["task_state"] = task_state
    if changed:
        state["updated_at"] = _now()
    return {
        "changed": changed,
        "active_recovery": deepcopy(dict(recovery.get("active_recovery") or {})),
        "events": deepcopy(list(recovery.get("state_events") or [])[events_before:]),
    }


def clear_step_scoped_recovery(state: PlanState, completed_step_id: str) -> None:
    """Retire active route authority while retaining immutable recovery history."""
    task_state = dict(state.get("task_state") or {})
    recovery = dict(task_state.get("recovery_state") or {})
    recovery_step = str(recovery.get("step_id") or recovery.get("failed_step_id") or "")
    active_recovery = dict(recovery.get("active_recovery") or {})
    active_recovery_matches = bool(
        active_recovery
        and str(active_recovery.get("step_id") or "") == str(completed_step_id or "")
    )
    if active_recovery_matches:
        retired_recovery = deepcopy(active_recovery)
        retired_recovery.update({
            "status": "closed",
            "closed_reason": "STEP_COMPLETED",
            "closed_at": _now(),
        })
        _append_active_recovery_history(recovery, retired_recovery)
        recovery["active_recovery"] = {}
        recovery["active_recovery_envelope"] = {}
        task_state["recovery_state"] = recovery
    if not recovery or (recovery_step and recovery_step != str(completed_step_id or "")):
        verification = dict(task_state.get("verification_pending") or {})
        if (
            not str(verification.get("step_id") or "")
            or str(verification.get("step_id") or "") == str(completed_step_id or "")
        ):
            task_state.pop("verification_pending", None)
            task_state.pop("resume_route_hint", None)
        state["task_state"] = task_state
        return
    recovery["active_recovery"] = {}
    recovery["active_recovery_envelope"] = {}
    active = dict(recovery.get("active_transition") or {})
    if active and str(active.get("status") or "").strip().lower() == "pending":
        active.update({
            "status": "expired",
            "superseded_reason": "STEP_COMPLETED",
            "expired_at": _now(),
        })
        recovery["transition_history"] = [
            *list(recovery.get("transition_history") or []),
            active,
        ]
        active_id = str(
            active.get("decision_id") or active.get("idempotency_key") or ""
        )
        last = dict(
            recovery.get("last_recovery_decision")
            or task_state.get("last_recovery_decision")
            or {}
        )
        last_id = str(last.get("decision_id") or last.get("idempotency_key") or "")
        if active_id and active_id == last_id:
            last.update({
                "status": "expired",
                "superseded_reason": "STEP_COMPLETED",
                "expired_at": active["expired_at"],
            })
            recovery["last_recovery_decision"] = last
            task_state["last_recovery_decision"] = deepcopy(last)
    recovery.update({
        "active_transition": {},
        "phase": "resolved",
        "step_id": "",
        "resolved_step_id": str(completed_step_id or ""),
        "resolved_at": _now(),
    })
    task_state["recovery_state"] = recovery
    task_state["diagnosis_state"] = {}
    task_state["active_execution"] = {}
    task_state["current_attempt"] = ""
    task_state.pop("verification_pending", None)
    task_state.pop("resume_route_hint", None)
    state["task_state"] = task_state


def record_scoped_evidence(
    state: PlanState,
    kind: str,
    payload: Mapping[str, Any],
    *,
    action_id: str = "",
    execution_attempt_id: str = "",
    failure_fingerprint: str = "",
) -> Dict[str, Any]:
    """Append immutable evidence history and move only its active reference."""
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"validation", "completion", "supervisor", "recovery"}:
        raise ValueError(f"Unsupported scoped evidence kind: {normalized_kind}")
    scope = {
        "task_id": str(state.get("task_id") or ""),
        "plan_id": str(state.get("plan_id") or ""),
        "plan_version": str(state.get("plan_version") or ""),
        "step_id": str(state.get("current_step_id") or ""),
        "action_id": str(action_id or payload.get("action_id") or ""),
        "execution_attempt_id": str(
            execution_attempt_id
            or payload.get("execution_attempt_id")
            or payload.get("attempt_id")
            or payload.get("execution_id")
            or ""
        ),
    }
    if failure_fingerprint:
        scope["failure_fingerprint"] = str(failure_fingerprint)
    identity = {
        "kind": normalized_kind,
        "scope": scope,
        "payload": dict(payload or {}),
    }
    record_id = f"{normalized_kind}-" + hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    record = {
        "schema_version": 1,
        "record_id": record_id,
        **scope,
        "payload": deepcopy(dict(payload or {})),
        "recorded_at": _now(),
    }
    task_state = dict(state.get("task_state") or {})
    history_key = f"{normalized_kind}_history"
    history = list(task_state.get(history_key) or [])
    if not any(str(item.get("record_id") or "") == record_id for item in history if isinstance(item, Mapping)):
        history.append(record)
    task_state[history_key] = history
    task_state[f"active_{normalized_kind}_ref"] = {
        "record_id": record_id,
        **scope,
    }
    state["task_state"] = task_state
    return record


def advance_plan_after_verified_step(
    state: PlanState,
    evidence: str = "",
    *,
    expected_step_id: str = "",
    transition_id: str = "",
    legacy_route_projection: bool = True,
) -> bool:
    """Advance exactly one step after deterministic execution/supervisor verification."""
    if task_status(state) in TASK_TERMINAL_STATUSES:
        return False
    if transition_id:
        task_state = dict(state.get("task_state") or {})
        if str(transition_id) in {
            str(item) for item in task_state.get("applied_step_advance_ids") or []
        }:
            return False
    step = next(
        (
            item
            for item in state.get("steps", []) or []
            if isinstance(item, dict)
            and str(item.get("step_id") or "") == str(expected_step_id or "")
            and str(item.get("status") or "") in STEP_ACTIVE_STATUSES
        ),
        None,
    ) if expected_step_id else _first_step_by_status(
        state,
        tuple(STEP_ACTIVE_STATUSES),
    )
    if not step:
        # A verified Execute/Supervisor result is authoritative evidence that
        # the scheduled step ran. Recover from an interrupted or stale
        # pending->running persistence transition instead of rebuilding Plan.
        step = next(
            (
                item
                for item in state.get("steps", []) or []
                if isinstance(item, dict)
                and str(item.get("step_id") or "") == str(expected_step_id or "")
                and str(item.get("status") or "") in {STATUS_READY, STATUS_PENDING}
            ),
            None,
        ) if expected_step_id else _first_step_by_status(
            state,
            (STATUS_READY, STATUS_PENDING),
        )
    if not step:
        return False
    completed_step_id = str(step.get("step_id") or "")
    step["status"] = STATUS_PASSED
    _append_evidence(step, "passed", evidence)
    clear_step_scoped_recovery(state, completed_step_id)
    successful = {
        str(item.get("step_id") or "")
        for item in state.get("steps", []) or []
        if isinstance(item, dict) and str(item.get("status") or "") in STEP_SUCCESS_STATUSES
    }
    for candidate in state.get("steps", []) or []:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("status") or "") not in {STATUS_WAITING_UPSTREAM, STATUS_INVALIDATED}:
            continue
        dependencies = {
            str(item) for item in candidate.get("depends_on", candidate.get("dependencies", [])) or []
            if str(item)
        }
        if dependencies and dependencies.issubset(successful):
            candidate["status"] = STATUS_PENDING
            _append_evidence(candidate, "upstream_revalidated", completed_step_id)
    next_step = _first_step_by_status(state, (STATUS_READY, STATUS_PENDING))
    if next_step:
        next_step["status"] = STATUS_RUNNING
        state["current_step_id"] = str(next_step.get("step_id") or "")
        state["current_stage_id"] = ""
        _append_evidence(next_step, "started", f"advanced after {completed_step_id}")
        set_task_status(state, TASK_STATUS_RUNNING)
    else:
        state["current_step_id"] = ""
        state["current_stage_id"] = ""
    task_state = dict(state.get("task_state") or {})
    recovery = dict(task_state.get("recovery_state") or {})
    recovery["pending_action"] = {}
    recovery["active_recovery"] = {}
    recovery["active_recovery_envelope"] = {}
    task_state["recovery_state"] = recovery
    task_state["active_execution"] = {}
    task_state["current_attempt"] = ""
    task_state["active_validation_ref"] = {}
    task_state["active_completion_ref"] = {}
    task_state["active_supervisor_ref"] = {}
    task_state["active_recovery_ref"] = {}
    if legacy_route_projection:
        task_state["generation_provenance"] = normal_generation_provenance(
            state,
            source_route="step_advance",
        ).to_dict()
        task_state["next_route"] = "generate" if next_step else "supervisor_final_audit"
    else:
        task_state["generation_provenance"] = {
            "schema_version": 2,
            "mode": GenerationMode.NORMAL.value,
            "semantic_trigger": "verified_step_committed",
            "step_id": str(state.get("current_step_id") or ""),
            "plan_revision": int(state.get("plan_revision") or 0),
        }
        task_state.pop("next_route", None)
    if transition_id:
        task_state["applied_step_advance_ids"] = list(dict.fromkeys([
            *list(task_state.get("applied_step_advance_ids") or []),
            str(transition_id),
        ]))
    state["task_state"] = task_state
    for field in (
        "recovery_decision",
        "pending_recovery_action",
        "recovery_execution_envelope",
        "execution_evidence",
        "current_attempt",
    ):
        if field in state:
            state[field] = {} if field != "current_attempt" else ""
    state["generation_mode"] = GenerationMode.NORMAL.value
    # Final task completion is owned by CompletionGate/Supervisor, not by
    # checklist advancement alone.
    state["updated_at"] = _now()
    return True


def block_plan_and_downstream(state: PlanState, evidence: str = "") -> bool:
    """Make BLOCKED durable and prevent downstream steps from being scheduled."""
    changed = False
    blocker_seen = False
    for step in state.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "").strip().lower()
        if status in {STATUS_BLOCKED, STATUS_FAILED}:
            blocker_seen = True
            continue
        if status in {*STEP_ACTIVE_STATUSES, STATUS_PENDING} and not blocker_seen:
            step["status"] = STATUS_BLOCKED
            _append_evidence(step, "blocked", evidence)
            blocker_seen = True
            changed = True
            continue
        if blocker_seen and status in {STATUS_PENDING, STATUS_READY}:
            step["status"] = STATUS_SKIPPED_UPSTREAM_BLOCKED
            _append_evidence(step, "skipped_upstream_blocked", evidence)
            changed = True
    if not blocker_seen:
        existing = _first_step_by_status(state, (STATUS_BLOCKED, STATUS_FAILED))
        blocker_seen = bool(existing)
    changed = set_task_status(state, TASK_STATUS_BLOCKED, evidence) or changed
    task_state = dict(state.get("task_state") or {})
    task_state["pending_action"] = {}
    recovery = dict(task_state.get("recovery_state") or {})
    recovery.update({"phase": "blocked", "status": "BLOCKED", "pending_action": {}})
    task_state["recovery_state"] = recovery
    state["task_state"] = task_state
    return changed


def record_current_step_evidence(state: PlanState, event: str, detail: str = "") -> bool:
    step = _first_step_by_status(state, (*tuple(STEP_ACTIVE_STATUSES), STATUS_PENDING, STATUS_BLOCKED))
    if not step:
        return False
    _append_evidence(step, event, detail)
    state["updated_at"] = _now()
    return True


def attach_execution_evidence_relation(
    state: PlanState,
    step_id: str,
    execution_record: Dict[str, Any],
    *,
    reason: str = "",
) -> bool:
    """Attach verified execution evidence to one active step without completing it.

    The controller calls this only after CompletionGate accepts a
    STEP_COMPLETION_CANDIDATE. Keeping the relation in PlanTracker makes the
    later status transition auditable while preserving PlanTracker as the sole
    owner of checklist state.
    """
    target = next(
        (
            step
            for step in state.get("steps", []) or []
            if isinstance(step, dict) and str(step.get("step_id") or "") == str(step_id or "")
        ),
        None,
    )
    if not target or str(target.get("status") or "") not in STEP_ACTIVE_STATUSES:
        return False

    execution_id = str(
        execution_record.get("execution_id")
        or execution_record.get("action_id")
        or execution_record.get("attempt_id")
        or ""
    )
    script_path = str(
        execution_record.get("script_path")
        or execution_record.get("code_path")
        or execution_record.get("generated_script_path")
        or ""
    )
    stdout = str(
        execution_record.get("raw_stdout")
        or execution_record.get("stdout")
        or execution_record.get("stdout_summary")
        or ""
    )
    relation = {
        "execution_id": execution_id,
        "source_step_id": str(execution_record.get("step_id") or ""),
        "target_step_id": str(step_id or ""),
        "reason": str(reason or "")[:1000],
    }
    detail = (
        f"{script_path}\nSTDOUT:\n{stdout[:5000]}\n"
        f"EVIDENCE_RELATION: {json.dumps(relation, ensure_ascii=False, sort_keys=True)}"
    )
    _append_evidence(target, "execution_evidence_relation", detail)
    state["updated_at"] = _now()
    return True


def _stage_status_rows(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    header: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("\t")]
        if not header:
            header = [part.lower() for part in parts]
            continue
        row = {header[index]: value for index, value in enumerate(parts) if index < len(header)}
        if row:
            rows.append(row)
    return rows


def stage_status_is_terminal(text: str) -> bool:
    rows = _stage_status_rows(text)
    if not rows:
        return False
    saw_terminal = False
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        reason = str(row.get("reason") or row.get("notes") or "").strip().lower()
        combined = f"{status} {reason}"
        if any(term in combined for term in ("pending", "running", "in_progress", "awaiting", "todo")):
            return False
        if any(term in status for term in ("completed", "complete", "blocked", "skipped", "success", "failed")):
            saw_terminal = True
    return saw_terminal


def sync_plan_state_from_stage_status(
    state: PlanState,
    stage_status_path: str | Path,
    evidence: str = "",
) -> int:
    """Mark desynchronised checklist steps terminal from a terminal stage_status.tsv.

    `stage_status.tsv` is the run-level status contract. When it has no pending
    rows, the chat-level checklist should not keep an old early step running and
    force Generate/Supervisor into a final-summary loop.
    """
    path = Path(stage_status_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if not stage_status_is_terminal(text):
        return 0

    changed = 0
    detail = evidence or f"synced from terminal stage_status.tsv: {path}"
    rows = _stage_status_rows(text)
    if any(
        any(token in str(row.get("status") or "").strip().lower() for token in ("blocked", "failed", "error"))
        for row in rows
    ):
        before = [
            str(step.get("status") or "") for step in state.get("steps", []) or []
            if isinstance(step, dict)
        ]
        block_plan_and_downstream(state, detail)
        after = [
            str(step.get("status") or "") for step in state.get("steps", []) or []
            if isinstance(step, dict)
        ]
        return sum(1 for left, right in zip(before, after) if left != right)
    terminal_statuses = set(STEP_TERMINAL_STATUSES)
    while True:
        step = next(
            (
                item for item in state.get("steps", []) or []
                if isinstance(item, dict)
                and str(item.get("status") or "").strip().lower() not in terminal_statuses
            ),
            None,
        )
        if step is None:
            break
        step_id = str(step.get("step_id") or "")
        if not mark_plan_step_done(state, detail):
            break
        # Preserve the historical stage_status reconciliation marker: the
        # RecoveryPolicy authorized completion, while PlanTracker records an
        # externally reconciled terminal row as DONE rather than PASSED.
        completed = next(
            (
                item for item in state.get("steps", []) or []
                if isinstance(item, dict) and str(item.get("step_id") or "") == step_id
            ),
            None,
        )
        if completed is not None:
            completed["status"] = STATUS_DONE
            _append_evidence(completed, "stage_status_sync", detail)
        changed += 1
    if changed:
        state["updated_at"] = _now()
    return changed


def next_action_title(state: PlanState) -> str:
    step = _first_step_by_status(state, tuple(STEP_ACTIVE_STATUSES))
    if step:
        return str(step.get("title", "")).strip()
    step = _first_step_by_status(state, (STATUS_PENDING,))
    if step:
        return str(step.get("title", "")).strip()
    step = _first_step_by_status(state, (STATUS_BLOCKED,))
    if step:
        return f"BLOCKED: {str(step.get('title', '')).strip()}"
    return "No pending steps."


def render_plan_progress(state: PlanState) -> str:
    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    if not steps:
        return "No tracked plan steps."
    lines = ["Plan checklist:"]
    for index, step in enumerate(steps, start=1):
        mark = _mark_from_status(str(step.get("status", STATUS_PENDING)))
        title = str(step.get("title", "")).strip() or f"Step {index}"
        lines.append(f"- [{mark}] {title}")
    lines.append(f"Next step: {next_action_title(state)}")
    return "\n".join(lines)


def _replace_progress_block(text: str, progress: str) -> str:
    block = f"{_PROGRESS_START}\n\n## Plan Progress\n{progress}\n\n{_PROGRESS_END}"
    pattern = re.compile(
        rf"{re.escape(_PROGRESS_START)}.*?{re.escape(_PROGRESS_END)}",
        re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(block, text)
    return text.rstrip() + "\n\n" + block + "\n"


def update_plan_markdown_status(plan_file: str | Path, state: PlanState) -> None:
    path = Path(plan_file)
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    if not steps:
        updated = _replace_progress_block(text, render_plan_progress(state))
        path.write_text(updated, encoding="utf-8")
        return

    step_index = 0
    changed = False
    updated_lines: List[str] = []
    for line in text.splitlines():
        match = _CHECKBOX_RE.match(line)
        if match and step_index < len(steps):
            mark = _mark_from_status(str(steps[step_index].get("status", STATUS_PENDING)))
            old_mark = match.group("mark")
            if old_mark != mark:
                start, end = match.span("mark")
                line = line[:start] + mark + line[end:]
                changed = True
            step_index += 1
        updated_lines.append(line)

    if step_index == 0:
        updated = _replace_progress_block(text, render_plan_progress(state))
        path.write_text(updated, encoding="utf-8")
        return

    if changed:
        path.write_text("\n".join(updated_lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")

"""Deterministic BioMind Plan Contract 1.2.1 primitives.

This module owns structure, identity, convergence, compatibility, and typed
infrastructure boundaries.  It deliberately contains no scientific language
classification and no handle-prefix or filename semantics.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


PLAN_CONTRACT_VERSION = "1.2.1"
MINIMUM_PLAN_READER_VERSION = "1.2.1"
RECONCILIATION_FINGERPRINT_SCHEMA_VERSION = 1
PLANNING_EPOCH_SCHEMA_VERSION = 1
MAX_RECONCILIATION_NONPROGRESS = 2

PLAN_CANDIDATE = "PLAN_CANDIDATE"
SUBMIT_PLAN_FOR_REVIEW = "SUBMIT_PLAN_FOR_REVIEW"
TASKSPEC_RECONCILIATION_REQUIRED = "TASKSPEC_RECONCILIATION_REQUIRED"
RECOGNIZED_PLANNER_OUTCOMES = {
    PLAN_CANDIDATE,
    SUBMIT_PLAN_FOR_REVIEW,
    TASKSPEC_RECONCILIATION_REQUIRED,
}

_PLAN_LOCAL_REF_RE = re.compile(r"^plan\.[A-Za-z][A-Za-z0-9_.-]{0,122}$")
_CONTROLLER_OWNED_CANDIDATE_FIELDS = {
    "candidate_scope_id",
    "logical_artifact_id",
    "artifact_binding_revision",
    "artifact_version_id",
    "canonical_path",
    "cas_token",
    "object_ref",
    "reservation_id",
}

# Closed V1 contract registries.  These values are structural policy IDs;
# neither the Planner nor Controller derives them from prose or filenames.
REGISTERED_OBJECT_KINDS = {"file", "directory"}
REGISTERED_ARTIFACT_LIFECYCLES = {"plan_local"}
REGISTERED_MATERIALIZATION_POLICIES = {"managed_intermediate_v1"}

# One shared structured Planner wire contract.  Prompt examples and the strict
# Plan reader import these values instead of maintaining independent schemas.
LEGACY_STRUCTURED_PLAN_SCHEMA_VERSION = 1
LEGACY_STRUCTURED_PLAN_CONTRACT = "structured_plan_v1"
STRUCTURED_PLAN_SCHEMA_VERSION = 2
STRUCTURED_PLAN_CONTRACT = "structured_plan_v2"
STRUCTURED_PLAN_REQUIRED_TOP_LEVEL_FIELDS = frozenset({
    "steps",
    "validation_checklist",
    "metadata",
})
STRUCTURED_PLAN_ALLOWED_TOP_LEVEL_FIELDS = frozenset({
    *STRUCTURED_PLAN_REQUIRED_TOP_LEVEL_FIELDS,
    "proposed_artifacts",
    "task_output_materializations",
})
STRUCTURED_PLAN_STEP_SCALAR_FIELDS = frozenset({
    "step_id",
    "title",
    "action",
    "purpose",
    "tool",
    "batch_id",
    "risk_level",
})
STRUCTURED_PLAN_STEP_OPTIONAL_SCALAR_FIELDS = frozenset({
    "execution_capability_id",
    "operation",
})
STRUCTURED_PLAN_STEP_LIST_FIELDS = frozenset({
    "inputs",
    "outputs",
    "dependencies",
    "success_criteria",
    "skill_ids",
    "workflow_stage_ids",
})
STRUCTURED_PLAN_STEP_OPTIONAL_LIST_FIELDS = frozenset({
    "produces_bindings",
    "produces_observations",
    "workspace_mutations",
    "explicit_side_effects",
})

_SINGLE_JSON_FENCE_RE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)
_JSON_FENCE_LINE_RE = re.compile(r"(?m)^[ \t]*```")


class PlanContractError(ValueError):
    """Base error for deterministic Plan Contract violations."""


class PlannerEnvelopeError(PlanContractError):
    """Typed Planner transport/envelope failure with provider-safe diagnostics."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        diagnostic: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.code = str(code or "PLANNER_ENVELOPE_INVALID")
        self.detail = str(detail or "")
        self.diagnostic = deepcopy(dict(diagnostic or {}))
        rendered = self.code + (f": {self.detail}" if self.detail else "")
        super().__init__(rendered)

    @property
    def field_path(self) -> str:
        return str(self.diagnostic.get("field_path") or "$")

    def to_diagnostic(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "field_path": self.field_path,
            **deepcopy(self.diagnostic),
        }


class PlannerCandidateError(PlanContractError):
    def __init__(
        self,
        code: str,
        field_path: str,
        detail: str,
        *,
        diagnostic: Optional[Mapping[str, Any]] = None,
    ):
        self.code = str(code)
        self.field_path = str(field_path)
        self.detail = str(detail)
        self.diagnostic = deepcopy(dict(diagnostic or {}))
        super().__init__(f"{self.code}: {self.field_path}: {self.detail}")

    def to_diagnostic(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "field_path": self.field_path,
            "message": self.detail,
            "owner": "planner",
            "disposition": "PROTOCOL_INVALID",
            **deepcopy(self.diagnostic),
        }


class ReaderCompatibilityError(PlanContractError):
    pass


class StateCASConflict(PlanContractError):
    """A successful state read proved the writer's generation is stale."""

    pass


class InfrastructureBoundaryError(Exception):
    """Adapter-certified infrastructure outcome.

    Only instances of this type (or a registered subclass) may be classified
    as infrastructure/transient.  Exception text is intentionally irrelevant.
    """

    def __init__(self, code: str, detail: str = "", *, retry_safe: bool = False):
        self.code = str(code or "INFRASTRUCTURE_ERROR")
        self.detail = str(detail or "")
        self.retry_safe = bool(retry_safe)
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


class TransientInfrastructureError(InfrastructureBoundaryError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code, detail, retry_safe=True)


class PermanentInfrastructureError(InfrastructureBoundaryError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code, detail, retry_safe=False)


def classify_io_exception(exc: BaseException) -> Dict[str, Any]:
    """Classify only adapter-typed failures; unknown exceptions are defects."""
    if isinstance(exc, InfrastructureBoundaryError):
        return {
            "error_class": (
                "TRANSIENT_SYSTEM_ERROR"
                if exc.retry_safe
                else "INTERNAL_CONTROLLER_INVARIANT_ERROR"
            ),
            "code": exc.code,
            "retry_safe": exc.retry_safe,
        }
    return {
        "error_class": "INTERNAL_CONTROLLER_IMPLEMENTATION_ERROR",
        "code": type(exc).__name__,
        "retry_safe": False,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _version_tuple(value: Any) -> Tuple[int, ...]:
    text = str(value or "0").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        raise ReaderCompatibilityError(f"PLAN_READER_VERSION_INVALID: {value!r}")
    return tuple(int(part) for part in text.split("."))


def assert_reader_compatible(
    value: Mapping[str, Any],
    *,
    reader_version: str = PLAN_CONTRACT_VERSION,
) -> None:
    minimum = str(value.get("minimum_reader_version") or "").strip()
    if not minimum:
        return
    if _version_tuple(reader_version) < _version_tuple(minimum):
        raise ReaderCompatibilityError(
            f"PLAN_READER_TOO_OLD: reader={reader_version}, minimum={minimum}"
        )


@dataclass(frozen=True)
class PlannerOutcomeEnvelope:
    outcome_type: str
    payload: Mapping[str, Any]
    legacy_envelope: bool = False

    def plan_candidate_body(self) -> Dict[str, Any]:
        if self.outcome_type not in {PLAN_CANDIDATE, SUBMIT_PLAN_FOR_REVIEW}:
            raise PlannerEnvelopeError("PLANNER_OUTCOME_IS_NOT_PLAN_CANDIDATE")
        body = deepcopy(dict(self.payload))
        body.pop("outcome_type", None)
        return body


def planner_candidate_wire_example() -> Dict[str, Any]:
    """Return an internal validator fixture; never inject it into a prompt."""
    return {
        "outcome_type": PLAN_CANDIDATE,
        "proposed_artifacts": [{
            "local_ref": "plan.fixture_artifact",
            "semantic_view": {
                "role": "fixture_artifact",
                "format": "txt",
                "description": "internal contract fixture",
            },
            "artifact_contract": {
                "object_kind": "file",
                "lifecycle": "plan_local",
                "materialization_policy_id": "managed_intermediate_v1",
            },
        }],
        "steps": [{
            "step_id": "step-1",
            "title": "Internal contract fixture step",
            "action": "execute",
            "purpose": "analysis",
            "tool": "declared_capability",
            "inputs": [],
            "outputs": ["plan.fixture_artifact"],
            "dependencies": [],
            "success_criteria": ["The declared fixture is materialized"],
            "skill_ids": [],
            "workflow_stage_ids": [],
            "batch_id": "",
            "risk_level": "low",
        }],
        "validation_checklist": [{
            "check_id": "check-1",
            "description": "Verify every explicit artifact reference and dependency",
        }],
        "metadata": {
            "schema_version": STRUCTURED_PLAN_SCHEMA_VERSION,
            "planner_contract": STRUCTURED_PLAN_CONTRACT,
        },
    }


def _wire_type(value: Any, *, missing: bool = False) -> str:
    if missing:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _wire_value_at_path(payload: Mapping[str, Any], field_path: str) -> Tuple[bool, Any]:
    current: Any = payload
    for name, index_text in re.findall(r"(?:^\$\.)?([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", field_path):
        if name:
            if not isinstance(current, Mapping) or name not in current:
                return False, None
            current = current[name]
        else:
            index = int(index_text)
            if not isinstance(current, list) or index >= len(current):
                return False, None
            current = current[index]
    return True, current


def structured_plan_candidate_diagnostics(
    payload: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Validate the shared structured Plan body without semantic inference."""
    diagnostics: List[Dict[str, Any]] = []
    metadata_value = payload.get("metadata")
    schema_version = (
        metadata_value.get("schema_version")
        if isinstance(metadata_value, Mapping)
        else None
    )
    legacy_schema = schema_version == LEGACY_STRUCTURED_PLAN_SCHEMA_VERSION
    required_top_level_fields = (
        frozenset({*STRUCTURED_PLAN_REQUIRED_TOP_LEVEL_FIELDS, "semantic_claims"})
        if legacy_schema
        else STRUCTURED_PLAN_REQUIRED_TOP_LEVEL_FIELDS
    )
    allowed_top_level_fields = (
        frozenset({*STRUCTURED_PLAN_ALLOWED_TOP_LEVEL_FIELDS, "semantic_claims"})
        if legacy_schema
        else STRUCTURED_PLAN_ALLOWED_TOP_LEVEL_FIELDS
    )
    step_list_fields = (
        frozenset({*STRUCTURED_PLAN_STEP_LIST_FIELDS, "semantic_constraint_ids"})
        if legacy_schema
        else STRUCTURED_PLAN_STEP_LIST_FIELDS
    )
    for key in required_top_level_fields:
        if key not in payload:
            diagnostics.append({
                "field_path": f"$.{key}",
                "message": "required field is missing",
            })
    for key in sorted(set(payload) - allowed_top_level_fields):
        diagnostics.append({
            "field_path": f"$.{key}",
            "message": "unexpected top-level field",
        })

    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        diagnostics.append({
            "field_path": "$.steps",
            "message": "must be a non-empty array",
        })
        steps = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            diagnostics.append({
                "field_path": f"$.steps[{index}]",
                "message": "must be an object",
            })
            continue
        for field in sorted(
            STRUCTURED_PLAN_STEP_SCALAR_FIELDS | step_list_fields
        ):
            if field not in step:
                diagnostics.append({
                    "field_path": f"$.steps[{index}].{field}",
                    "message": "required field is missing",
                })
        for field in sorted(STRUCTURED_PLAN_STEP_SCALAR_FIELDS):
            if field in step and not isinstance(step.get(field), str):
                diagnostics.append({
                    "field_path": f"$.steps[{index}].{field}",
                    "message": "must be a string",
                })
        for field in sorted(step_list_fields | STRUCTURED_PLAN_STEP_OPTIONAL_LIST_FIELDS):
            if field in step and not isinstance(step.get(field), list):
                diagnostics.append({
                    "field_path": f"$.steps[{index}].{field}",
                    "message": "must be an array",
                })
        if isinstance(step.get("skill_ids"), list) and len(step["skill_ids"]) > 3:
            diagnostics.append({
                "field_path": f"$.steps[{index}].skill_ids",
                "message": "must contain at most three selected skill IDs",
            })
        for field in sorted(STRUCTURED_PLAN_STEP_OPTIONAL_SCALAR_FIELDS):
            if field in step and not isinstance(step.get(field), str):
                diagnostics.append({
                    "field_path": f"$.steps[{index}].{field}",
                    "message": "must be a string",
                })
        unexpected_step_fields = sorted(
            set(step) - (
                STRUCTURED_PLAN_STEP_SCALAR_FIELDS
                | STRUCTURED_PLAN_STEP_OPTIONAL_SCALAR_FIELDS
                | step_list_fields
                | STRUCTURED_PLAN_STEP_OPTIONAL_LIST_FIELDS
                | (frozenset({"semantic_claims"}) if legacy_schema else frozenset())
            )
        )
        for field in unexpected_step_fields:
            diagnostics.append({
                "field_path": f"$.steps[{index}].{field}",
                "message": "unexpected step field",
            })
        if legacy_schema and "semantic_claims" in step:
            diagnostics.append({
                "field_path": f"$.steps[{index}].semantic_claims",
                "message": (
                    "legacy Planner wire accepts constraint references only; "
                    "inline claims are not allowed"
                ),
            })

    if legacy_schema:
        if not isinstance(payload.get("semantic_claims"), list):
            diagnostics.append({
                "field_path": "$.semantic_claims",
                "message": "must be an array",
            })
        if payload.get("semantic_claims") not in (None, []):
            diagnostics.append({
                "field_path": "$.semantic_claims",
                "message": (
                    "semantic truth is TaskSpec-owned; the Planner must not mint claim rows"
                ),
            })

    checklist = payload.get("validation_checklist")
    if not isinstance(checklist, list):
        diagnostics.append({
            "field_path": "$.validation_checklist",
            "message": "must be an array",
        })
    else:
        for index, check in enumerate(checklist):
            if not isinstance(check, Mapping):
                diagnostics.append({
                    "field_path": f"$.validation_checklist[{index}]",
                    "message": "must be an object",
                })
                continue
            for field in ("check_id", "description"):
                if (
                    not isinstance(check.get(field), str)
                    or not str(check.get(field) or "").strip()
                ):
                    diagnostics.append({
                        "field_path": f"$.validation_checklist[{index}].{field}",
                        "message": "must be a non-empty string",
                    })

    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        diagnostics.append({
            "field_path": "$.metadata",
            "message": "must be an object",
        })
    else:
        expected_schema_version = (
            LEGACY_STRUCTURED_PLAN_SCHEMA_VERSION
            if legacy_schema
            else STRUCTURED_PLAN_SCHEMA_VERSION
        )
        expected_contract = (
            LEGACY_STRUCTURED_PLAN_CONTRACT
            if legacy_schema
            else STRUCTURED_PLAN_CONTRACT
        )
        if metadata.get("schema_version") != expected_schema_version:
            diagnostics.append({
                "field_path": "$.metadata.schema_version",
                "message": f"must equal {expected_schema_version}",
            })
        if metadata.get("planner_contract") != expected_contract:
            diagnostics.append({
                "field_path": "$.metadata.planner_contract",
                "message": f"must equal {expected_contract}",
            })

    if "proposed_artifacts" in payload and not isinstance(
        payload.get("proposed_artifacts"), list
    ):
        diagnostics.append({
            "field_path": "$.proposed_artifacts",
            "message": "must be an array",
        })
    materializations = payload.get("task_output_materializations")
    if materializations is not None:
        if not isinstance(materializations, list):
            diagnostics.append({
                "field_path": "$.task_output_materializations",
                "message": "must be an array",
            })
        else:
            for index, row in enumerate(materializations):
                if not isinstance(row, Mapping):
                    diagnostics.append({
                        "field_path": f"$.task_output_materializations[{index}]",
                        "message": "must be an object",
                    })
                    continue
                unexpected = set(row) - {"output_ref", "path"}
                if unexpected:
                    diagnostics.append({
                        "field_path": f"$.task_output_materializations[{index}]",
                        "message": "contains unexpected fields: " + ", ".join(sorted(unexpected)),
                    })
                for field in ("output_ref", "path"):
                    if not isinstance(row.get(field), str) or not str(row.get(field) or "").strip():
                        diagnostics.append({
                            "field_path": f"$.task_output_materializations[{index}].{field}",
                            "message": "must be a non-empty string",
                        })
    for diagnostic in diagnostics:
        field_path = str(diagnostic.get("field_path") or "$")
        present, received = _wire_value_at_path(payload, field_path)
        message = str(diagnostic.get("message") or "")
        expected_type = "value"
        if "array" in message:
            expected_type = "array"
        elif "object" in message:
            expected_type = "object"
        elif "string" in message:
            expected_type = "string"
        elif "required field" in message:
            expected_type = "present"
        elif "must equal" in message:
            expected_type = message.removeprefix("must equal ")
        diagnostic.setdefault("code", "PLAN_STRUCTURE_INVALID")
        diagnostic.setdefault("expected_type", expected_type)
        diagnostic.setdefault("received_type", _wire_type(received, missing=not present))
        if present:
            diagnostic.setdefault("received_excerpt", _canonical_json(received)[:500])
    return diagnostics


def validated_planner_candidate_wire_example() -> Dict[str, Any]:
    """Fail as a Controller invariant if its own prompt contract drifts."""
    example = planner_candidate_wire_example()
    try:
        envelope = parse_planner_outcome_envelope(json.dumps(example))
        body = envelope.plan_candidate_body()
        _keys, _usage, diagnostics = validate_planner_candidate_deterministically(
            body,
            candidate_scope_id="candidate-scope-wire-example",
            authoritative_handles={},
            availability_origins={},
        )
        if diagnostics:
            raise PlanContractError(_canonical_json(diagnostics))
        prepare_allocation_intent(
            body,
            candidate_scope_id="candidate-scope-wire-example",
            planning_epoch_id="planning-epoch-wire-example",
        )
    except PlanContractError as exc:
        raise PermanentInfrastructureError(
            "PLANNER_WIRE_CONTRACT_INVALID",
            str(exc),
        ) from exc
    return example


def normalize_planner_json_transport(raw: str) -> str:
    """Normalize raw JSON or exactly one whole-response JSON fence.

    This is transport-only normalization.  It never searches for a JSON
    fragment, removes surrounding prose, combines multiple fences, or repairs
    malformed JSON.
    """
    text = str(raw or "").strip()
    if not text:
        raise PlannerEnvelopeError(
            "PLANNER_ENVELOPE_EMPTY",
            diagnostic={
                "expected_type": "object",
                "received_type": "empty_string",
                "response_excerpt": "",
            },
        )
    fenced = _SINGLE_JSON_FENCE_RE.fullmatch(text)
    if fenced:
        body = fenced.group("body").strip()
        if _JSON_FENCE_LINE_RE.search(body):
            raise PlannerEnvelopeError(
                "PLANNER_ENVELOPE_TRANSPORT_INVALID",
                "multiple JSON fences",
                diagnostic={
                    "expected_type": "single_json_object",
                    "received_type": "multiple_json_fences",
                    "response_excerpt": text[:500],
                },
            )
        return body
    if _JSON_FENCE_LINE_RE.search(text):
        raise PlannerEnvelopeError(
            "PLANNER_ENVELOPE_TRANSPORT_INVALID",
            "expected raw JSON or one complete JSON fence with no surrounding text",
            diagnostic={
                "expected_type": "single_json_object",
                "received_type": "mixed_text_or_incomplete_fence",
                "response_excerpt": text[:500],
            },
        )
    return text


def parse_planner_outcome_envelope(
    raw: str,
    *,
    allow_legacy_plan: bool = False,
) -> PlannerOutcomeEnvelope:
    """Parse one JSON object and only recognize its top-level discriminator.

    No body schema, semantic reference, or reconciliation request is acted on
    here. Raw JSON and exactly one whole-response JSON fence are transport
    equivalents; prose and multiple/incomplete fences are rejected. Callers
    must evaluate invocation staleness before body validation.
    """
    text = normalize_planner_json_transport(raw)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerEnvelopeError(
            "PLANNER_ENVELOPE_JSON_INVALID",
            f"{exc.msg} at line {exc.lineno} column {exc.colno}",
            diagnostic={
                "parser_message": str(exc.msg),
                "line": int(exc.lineno),
                "column": int(exc.colno),
                "offset": int(exc.pos),
                "expected_type": "object",
                "received_type": "invalid_json",
                "response_excerpt": text[max(0, exc.pos - 160):exc.pos + 340],
            },
        ) from exc
    if not isinstance(value, Mapping):
        raise PlannerEnvelopeError(
            "PLANNER_ENVELOPE_NOT_OBJECT",
            diagnostic={
                "expected_type": "object",
                "received_type": type(value).__name__,
                "received_excerpt": _canonical_json(value)[:500],
            },
        )
    outcome_type = str(value.get("outcome_type") or "").strip()
    legacy = False
    if not outcome_type and allow_legacy_plan and isinstance(value.get("steps"), list):
        outcome_type = PLAN_CANDIDATE
        legacy = True
    if outcome_type not in RECOGNIZED_PLANNER_OUTCOMES:
        raise PlannerEnvelopeError(
            "PLANNER_OUTCOME_UNRECOGNIZED",
            outcome_type or "<missing>",
            diagnostic={
                "field_path": "$.outcome_type",
                "expected_type": (
                    "SUBMIT_PLAN_FOR_REVIEW|PLAN_CANDIDATE|"
                    "TASKSPEC_RECONCILIATION_REQUIRED"
                ),
                "received_type": "string" if outcome_type else "missing",
                "received_excerpt": outcome_type[:200],
            },
        )
    return PlannerOutcomeEnvelope(outcome_type, deepcopy(dict(value)), legacy)


def new_candidate_scope_id() -> str:
    return f"candidate-scope-{uuid.uuid4()}"


@dataclass(frozen=True, order=True)
class ArtifactRefKey:
    kind: str
    identity: str
    candidate_scope_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"authoritative", "candidate_local"}:
            raise PlannerCandidateError(
                "ARTIFACT_REF_KIND_INVALID", "$", f"unsupported kind {self.kind!r}"
            )
        if not self.identity:
            raise PlannerCandidateError(
                "ARTIFACT_REF_IDENTITY_MISSING", "$", "identity is required"
            )
        if self.kind == "candidate_local" and not self.candidate_scope_id:
            raise PlannerCandidateError(
                "CANDIDATE_SCOPE_MISSING", "$", "candidate-local refs require Controller scope"
            )

    @classmethod
    def authoritative(cls, logical_artifact_id: str) -> "ArtifactRefKey":
        return cls("authoritative", str(logical_artifact_id or ""))

    @classmethod
    def candidate_local(cls, scope: str, local_ref: str) -> "ArtifactRefKey":
        return cls("candidate_local", str(local_ref or ""), str(scope or ""))

    def to_dict(self) -> Dict[str, str]:
        result = {"kind": self.kind, "identity": self.identity}
        if self.candidate_scope_id:
            result["candidate_scope_id"] = self.candidate_scope_id
        return result


def _find_forbidden_candidate_field(value: Any, path: str = "$") -> Optional[Tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            field_path = f"{path}.{key}"
            if str(key) in _CONTROLLER_OWNED_CANDIDATE_FIELDS:
                return field_path, str(key)
            found = _find_forbidden_candidate_field(item, field_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_forbidden_candidate_field(item, f"{path}[{index}]")
            if found:
                return found
    return None


def validate_plan_candidate_control_fields(candidate: Mapping[str, Any]) -> None:
    forbidden = _find_forbidden_candidate_field(candidate)
    if forbidden:
        field_path, field = forbidden
        raise PlannerCandidateError(
            "PLANNER_CONTROLLER_FIELD_FORBIDDEN",
            field_path,
            f"Planner may not return Controller-owned field {field}",
        )


def candidate_artifact_ref_keys(
    candidate: Mapping[str, Any],
    *,
    candidate_scope_id: str,
    authoritative_handles: Mapping[str, str],
    authoritative_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[Dict[str, ArtifactRefKey], List[Dict[str, Any]]]:
    """Resolve explicit Plan usage into typed keys without allocating IDs."""
    validate_plan_candidate_control_fields(candidate)
    declarations: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(candidate.get("proposed_artifacts", []) or []):
        if not isinstance(raw, Mapping):
            raise PlannerCandidateError(
                "PROPOSED_ARTIFACT_INVALID",
                f"$.proposed_artifacts[{index}]",
                "must be an object",
                diagnostic={
                    "expected_type": "object",
                    "received_type": _wire_type(raw),
                    "received_excerpt": _canonical_json(raw)[:500],
                },
            )
        local_ref = str(raw.get("local_ref") or "").strip()
        if not _PLAN_LOCAL_REF_RE.fullmatch(local_ref):
            raise PlannerCandidateError(
                "LOCAL_REF_INVALID",
                f"$.proposed_artifacts[{index}].local_ref",
                local_ref or "missing",
                diagnostic={
                    "expected_type": "plan.* handle",
                    "received_type": _wire_type(raw.get("local_ref"), missing="local_ref" not in raw),
                    "received_excerpt": _canonical_json(raw.get("local_ref"))[:500],
                    "machine_evidence": {"required_namespace": "plan.*"},
                },
            )
        if local_ref in declarations:
            raise PlannerCandidateError(
                "LOCAL_REF_DUPLICATE",
                f"$.proposed_artifacts[{index}].local_ref",
                local_ref,
                diagnostic={
                    "observed_identity": {"handle": local_ref},
                    "machine_evidence": {"duplicate_local_ref": local_ref},
                },
            )
        local_name = local_ref.removeprefix("plan.")
        semantic_view = (
            dict(raw.get("semantic_view") or {})
            if isinstance(raw.get("semantic_view"), Mapping)
            else {}
        )
        candidate_aliases = {
            local_ref,
            local_name,
            str(semantic_view.get("role") or ""),
            str(semantic_view.get("input_id") or ""),
            str(semantic_view.get("output_id") or ""),
            str(semantic_view.get("artifact_id") or ""),
        }
        candidate_aliases.discard("")
        for handle, stable_identity in authoritative_handles.items():
            binding = dict((authoritative_bindings or {}).get(str(handle)) or {})
            metadata = dict(binding.get("metadata") or {})
            aliases = {
                str(handle),
                str(handle).split(".", 1)[-1],
                str(stable_identity),
                str(binding.get("stable_identity") or ""),
                str(metadata.get("role") or ""),
                str(metadata.get("input_id") or ""),
                str(metadata.get("output_id") or ""),
                str(metadata.get("artifact_id") or ""),
            }
            aliases.discard("")
            collision_keys = sorted(candidate_aliases.intersection(aliases))
            if not collision_keys:
                continue
            direction = str(binding.get("direction") or "")
            code = (
                "TASK_OUTPUT_SHADOWED_BY_PLAN_LOCAL"
                if direction == "output"
                else "TASK_INPUT_SHADOWED_BY_PLAN_LOCAL"
                if direction == "input"
                else "AUTHORITATIVE_ARTIFACT_SHADOWED_BY_PLAN_LOCAL"
            )
            expected = {
                "kind": "authoritative",
                "identity": str(stable_identity),
                "handle": str(handle),
            }
            observed = {
                "kind": "candidate_local",
                "identity": local_ref,
                "candidate_scope_id": str(candidate_scope_id),
            }
            affected_output = (
                {
                    "handle": str(handle),
                    "output_id": str(metadata.get("output_id") or ""),
                    "role": str(metadata.get("role") or ""),
                }
                if direction == "output"
                else {}
            )
            raise PlannerCandidateError(
                code,
                f"$.proposed_artifacts[{index}].local_ref",
                local_ref,
                diagnostic={
                    "affected_artifact": expected,
                    "affected_output": affected_output,
                    "expected_identity": expected,
                    "observed_identity": observed,
                    "machine_evidence": {
                        "collision_keys": collision_keys,
                        "authoritative_direction": direction,
                    },
                },
            )
        contract = raw.get("artifact_contract")
        if not isinstance(contract, Mapping):
            raise PlannerCandidateError(
                "ARTIFACT_CONTRACT_INVALID",
                f"$.proposed_artifacts[{index}].artifact_contract",
                "must be an object",
                diagnostic={
                    "expected_type": "object",
                    "received_type": _wire_type(contract, missing="artifact_contract" not in raw),
                    "received_excerpt": _canonical_json(contract)[:500],
                },
            )
        declarations[local_ref] = deepcopy(dict(raw))

    keys: Dict[str, ArtifactRefKey] = {
        str(handle): ArtifactRefKey.authoritative(str(identity))
        for handle, identity in authoritative_handles.items()
    }
    keys.update({
        local_ref: ArtifactRefKey.candidate_local(candidate_scope_id, local_ref)
        for local_ref in declarations
    })
    usage: List[Dict[str, Any]] = []
    for step_index, step in enumerate(candidate.get("steps", []) or []):
        if not isinstance(step, Mapping):
            continue
        step_id = str(step.get("step_id") or f"step-{step_index + 1}")
        for field, relation in (("inputs", "consume"), ("outputs", "produce")):
            for ref_index, raw_ref in enumerate(step.get(field, []) or []):
                ref = str(raw_ref or "")
                key = keys.get(ref)
                if key is None:
                    raise PlannerCandidateError(
                        "ARTIFACT_REFERENCE_UNKNOWN",
                        f"$.steps[{step_index}].{field}[{ref_index}]",
                        ref,
                        diagnostic={
                            "affected_step_ids": [step_id],
                            "affected_artifact": {"handle": ref},
                            "expected_identity": {
                                "kind": "registered_artifact_handle",
                                "allowed_namespaces": ["input.*", "output.*", "plan.*"],
                            },
                            "observed_identity": {"handle": ref},
                            "machine_evidence": {
                                "relation": relation,
                                "known_handles": sorted(keys),
                            },
                        },
                    )
                usage.append({
                    "step_id": step_id,
                    "relation": relation,
                    "field": field,
                    "artifact_ref": key.to_dict(),
                })
    for index, row in enumerate(candidate.get("task_output_materializations", []) or []):
        if not isinstance(row, Mapping):
            continue
        ref = str(row.get("output_ref") or "")
        key = keys.get(ref)
        if key is None or key.kind != "authoritative":
            raise PlannerCandidateError(
                "TASK_OUTPUT_REFERENCE_UNKNOWN",
                f"$.task_output_materializations[{index}].output_ref",
                ref,
                diagnostic={
                    "affected_output": {"handle": ref},
                    "expected_identity": {
                        "kind": "authoritative_task_output_handle",
                    },
                    "observed_identity": {"handle": ref},
                    "machine_evidence": {
                        "known_output_handles": sorted(
                            handle
                            for handle, candidate_key in keys.items()
                            if candidate_key.kind == "authoritative"
                            and str(handle).startswith("output.")
                        ),
                    },
                },
            )
    return keys, usage


def validate_candidate_graph_and_flow(
    candidate: Mapping[str, Any],
    *,
    ref_keys: Mapping[str, ArtifactRefKey],
    usage: Sequence[Mapping[str, Any]],
    availability_origins: Mapping[str, str],
    resource_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Compatibility wrapper over the canonical compiled dataflow validator.

    The dependency checks remain here because they require no effect
    interpretation.  READ/PRODUCE/MUTATE availability is delegated to
    ``compile_plan_dataflow`` so this legacy entry point no longer owns a
    second outputs-only producer model.
    """
    steps = [dict(item) for item in candidate.get("steps", []) or [] if isinstance(item, Mapping)]
    step_ids = [str(step.get("step_id") or "") for step in steps]
    issues: List[Dict[str, Any]] = []
    if not step_ids or any(not value for value in step_ids):
        issues.append({"code": "PLAN_STEP_ID_MISSING"})
        return issues
    if len(set(step_ids)) != len(step_ids):
        duplicate_step_ids = sorted({
            step_id for step_id in step_ids if step_ids.count(step_id) > 1
        })
        duplicate_indexes = {
            step_id: [
                index
                for index, candidate_step_id in enumerate(step_ids)
                if candidate_step_id == step_id
            ]
            for step_id in duplicate_step_ids
        }
        issues.append({
            "code": "PLAN_STEP_ID_DUPLICATE",
            "affected_step_ids": duplicate_step_ids,
            "duplicate_step_ids": duplicate_step_ids,
            "duplicate_step_indexes": duplicate_indexes,
        })
        return issues

    known = set(step_ids)
    dependencies: Dict[str, set[str]] = {}
    for step in steps:
        step_id = str(step.get("step_id") or "")
        values = {str(value) for value in step.get("dependencies", []) or [] if str(value)}
        dependencies[step_id] = values
        for dependency in sorted(values):
            if dependency not in known:
                issues.append({
                    "code": "PLAN_DEPENDENCY_UNKNOWN",
                    "step_id": step_id,
                    "dependency_step_id": dependency,
                })
            elif dependency == step_id:
                issues.append({
                    "code": "PLAN_DEPENDENCY_SELF",
                    "step_id": step_id,
                })

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str, trail: Tuple[str, ...] = ()) -> None:
        if step_id in visiting:
            issues.append({
                "code": "PLAN_DEPENDENCY_CYCLE",
                "step_id": step_id,
                "cycle": [*trail, step_id],
            })
            return
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in sorted(dependencies.get(step_id, set())):
            if dependency in known:
                visit(dependency, (*trail, step_id))
        visiting.discard(step_id)
        visited.add(step_id)

    for step_id in step_ids:
        visit(step_id)

    del usage  # Kept in the ABI for persisted callers; no longer a flow authority.
    compiled_bindings = (
        {
            str(alias): deepcopy(dict(row))
            for alias, row in resource_bindings.items()
            if isinstance(row, Mapping)
        }
        if resource_bindings is not None
        else None
    )
    candidate_scope_id = next(
        (
            key.candidate_scope_id
            for key in ref_keys.values()
            if key.kind == "candidate_local" and key.candidate_scope_id
        ),
        "candidate-scope-compatibility",
    )
    authoritative_handles = {
        str(handle): key.identity
        for handle, key in ref_keys.items()
        if key.kind == "authoritative"
    }
    from agent_core.step_effects import compile_plan_dataflow

    _, _, dataflow_issues = compile_plan_dataflow(
        candidate,
        {},
        resource_bindings=compiled_bindings,
        candidate_scope_id=candidate_scope_id,
        authoritative_handles=authoritative_handles,
        availability_origins=availability_origins,
        compile_effect_contracts=False,
        validate_dependencies=False,
    )
    issues.extend(dataflow_issues)
    # Stable de-duplication permits validators to report the same origin
    # conflict through both the producer and same-step checks.
    unique: Dict[str, Dict[str, Any]] = {}
    for issue in issues:
        unique[_canonical_json(issue)] = issue
    return [unique[key] for key in sorted(unique)]


def planner_candidate_body_from_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Project one canonical PlanState back to the shared Planner wire shape.

    Persisted-state revalidation must not be a weaker, state-specific path, so
    this projection deliberately retains only fields owned by the shared
    structured candidate contract.
    """
    plan_document = (
        dict(state.get("plan_document") or {})
        if isinstance(state.get("plan_document"), Mapping)
        else {}
    )
    metadata = dict(plan_document.get("metadata") or {})
    legacy_schema = bool(
        str(metadata.get("schema_version") or STRUCTURED_PLAN_SCHEMA_VERSION)
        == str(LEGACY_STRUCTURED_PLAN_SCHEMA_VERSION)
        or str(metadata.get("planner_contract") or "")
        == LEGACY_STRUCTURED_PLAN_CONTRACT
    )
    step_fields = set(
        STRUCTURED_PLAN_STEP_SCALAR_FIELDS
        | STRUCTURED_PLAN_STEP_OPTIONAL_SCALAR_FIELDS
        | STRUCTURED_PLAN_STEP_LIST_FIELDS
        | STRUCTURED_PLAN_STEP_OPTIONAL_LIST_FIELDS
    )
    if legacy_schema:
        step_fields.add("semantic_constraint_ids")
    steps: List[Dict[str, Any]] = []
    for raw in state.get("steps", []) or []:
        if not isinstance(raw, Mapping):
            continue
        step = {
            key: deepcopy(raw.get(key))
            for key in sorted(step_fields)
            if key in raw
        }
        if legacy_schema:
            # Canonical PlanState v10 has already retired legacy semantic-claim
            # ownership, but the v1 wire reader still requires the compatibility
            # slots to be present.
            step.setdefault("semantic_constraint_ids", [])
        steps.append(step)
    candidate: Dict[str, Any] = {
        "steps": steps,
        "validation_checklist": deepcopy(list(
            plan_document.get("validation_checklist") or []
        )),
        "metadata": deepcopy(metadata),
    }
    if legacy_schema:
        candidate["semantic_claims"] = deepcopy(list(
            state.get("semantic_claims") or []
        ))
    proposed = [
        {
            key: deepcopy(item.get(key))
            for key in (
                "local_ref",
                "object_kind",
                "lifecycle",
                "semantic_view",
                "artifact_contract",
                "materialization_policy_id",
            )
            if key in item
        }
        for item in state.get("proposed_artifacts", []) or []
        if isinstance(item, Mapping)
    ]
    if proposed:
        candidate["proposed_artifacts"] = proposed
    if "task_output_materializations" in state:
        candidate["task_output_materializations"] = [
            {
                "output_ref": str(
                    item.get("output_ref") or item.get("output_id") or ""
                ),
                "path": str(item.get("path") or ""),
            }
            for item in state.get("task_output_materializations", []) or []
            if isinstance(item, Mapping)
        ]
    return candidate


def _candidate_step_field_path(
    candidate: Mapping[str, Any],
    *,
    step_id: str,
    field: str,
    value: str = "",
) -> str:
    for step_index, raw in enumerate(candidate.get("steps", []) or []):
        if not isinstance(raw, Mapping) or str(raw.get("step_id") or "") != step_id:
            continue
        raw_value = raw.get(field)
        values = list(raw_value or []) if isinstance(raw_value, (list, tuple)) else []
        if value:
            try:
                return f"$.steps[{step_index}].{field}[{values.index(value)}]"
            except ValueError:
                pass
        return f"$.steps[{step_index}].{field}"
    return "$.steps"


def _candidate_flow_diagnostic(
    candidate: Mapping[str, Any],
    issue: Mapping[str, Any],
) -> Dict[str, Any]:
    """Attach a stable owner, validator, and best exact field to a flow issue."""
    row = deepcopy(dict(issue))
    code = str(row.get("code") or "PLAN_CANDIDATE_INVALID")
    step_id = str(row.get("step_id") or row.get("consumer_step_id") or "")
    field_path = "$.steps"
    validator_id = str(row.get("validator_id") or "plan_artifact_flow")
    message = str(row.get("message") or code)
    affected_fields: List[str] = []
    violated_invariant = "candidate_satisfies_deterministic_plan_contract"
    required_postcondition = "The returned candidate passes this deterministic validator."
    if code.startswith("PLAN_STEP_"):
        validator_id = "plan_dag"
        affected_fields = ["step_id"]
        violated_invariant = "plan_step_ids_are_present_and_unique"
        required_postcondition = "Every plan step has one unique non-empty step_id."
        field_path = _candidate_step_field_path(
            candidate, step_id=step_id, field="step_id"
        )
    elif code == "PLAN_DEPENDENCY_UNKNOWN":
        validator_id = "plan_dag"
        affected_fields = ["dependencies"]
        violated_invariant = "every_dependency_identifies_an_existing_plan_step"
        required_postcondition = "Every dependency names an existing step in the returned plan."
        dependency = str(row.get("dependency_step_id") or "")
        field_path = _candidate_step_field_path(
            candidate,
            step_id=step_id,
            field="dependencies",
            value=dependency,
        )
        message = f"Dependency {dependency!r} does not identify an existing plan step."
    elif code == "PLAN_DEPENDENCY_SELF":
        validator_id = "plan_dag"
        affected_fields = ["dependencies"]
        violated_invariant = "no_plan_step_depends_on_itself"
        required_postcondition = "No step depends on itself."
        field_path = _candidate_step_field_path(
            candidate,
            step_id=step_id,
            field="dependencies",
            value=step_id,
        )
        message = "A plan step may not depend on itself."
    elif code == "PLAN_DEPENDENCY_CYCLE":
        validator_id = "plan_dag"
        affected_fields = ["dependencies"]
        violated_invariant = "dependency_graph_is_acyclic"
        required_postcondition = "The returned dependency graph contains no cycle."
        field_path = "$.steps"
        message = "Plan dependencies contain a cycle."
    elif code == "PLAN_ARTIFACT_PRODUCER_CARDINALITY":
        affected_fields = list(row.get("affected_fields") or ["outputs"])
        violated_invariant = "each_planned_artifact_has_exactly_one_producer"
        required_postcondition = str(
            row.get("required_condition")
            or "Each planned artifact has exactly one producing step."
        )
        field_path = str(row.get("field_path") or "$.steps")
        message = str(
            row.get("message") or "A planned artifact must have exactly one producer."
        )
    elif code == "PLAN_ARTIFACT_UNAVAILABLE":
        affected_fields = ["inputs", "outputs", "dependencies"]
        violated_invariant = "consumed_artifacts_are_available_from_verified_or_reachable_producers"
        required_postcondition = (
            "Every consumed artifact is a verified input or is produced by exactly "
            "one reachable dependency."
        )
        field_path = str(row.get("field_path") or field_path)
        message = str(row.get("message") or (
            "Consumed artifact is neither a verified task input nor produced "
            "by exactly one reachable dependency."
        ))
    elif code in {
        "PLAN_RESOURCE_UNAVAILABLE",
        "PLAN_RESOURCE_READ_BEFORE_MUTATION",
        "PLAN_MUTATION_PRECONDITION_UNSATISFIED",
        "PLAN_MUTATION_TRANSITION_UNSPECIFIED",
        "PLAN_MUTATION_ORDER_AMBIGUOUS",
        "PLAN_RESOURCE_BINDING_MISSING",
        "PLAN_RESOURCE_KIND_MISMATCH",
        "PLAN_MUTABLE_RESOURCE_DECLARED_AS_OUTPUT",
    }:
        affected_fields = list(row.get("affected_fields") or [
            "inputs", "workspace_mutations", "dependencies"
        ])
        violated_invariant = "compiled_resource_effects_satisfy_explicit_availability_contract"
        required_postcondition = str(
            row.get("required_condition")
            or "Every READ and MUTATE precondition has one explicit reachable source."
        )
        field_path = str(row.get("field_path") or "$.steps")
        message = str(row.get("message") or code)
    elif code == "PLAN_ARTIFACT_ORIGIN_CONFLICT":
        affected_fields = list(
            row.get("affected_fields") or ["inputs", "outputs"]
        )
        violated_invariant = "immutable_artifact_origin_is_unambiguous"
        required_postcondition = str(
            row.get("required_condition")
            or "Every immutable artifact has exactly one authoritative origin."
        )
        field_path = str(row.get("field_path") or "$.steps")
        message = str(
            row.get("message")
            or "Plan declares conflicting origins for one immutable artifact."
        )
    elif step_id:
        field_path = _candidate_step_field_path(
            candidate,
            step_id=step_id,
            field="inputs",
        )
    result = {
        **row,
        "code": code,
        "owner": "planner",
        "validator_id": validator_id,
        "field_path": field_path,
        "repair_target": field_path,
        "message": message,
        "affected_fields": affected_fields,
        "violated_invariant": violated_invariant,
        "required_postcondition": required_postcondition,
        "disposition": "PROTOCOL_INVALID",
    }
    if step_id:
        result.setdefault("step_id", step_id)
    affected_step_ids = [
        str(value)
        for value in (
            row.get("affected_step_ids")
            or row.get("producer_step_ids")
            or ([step_id] if step_id else [])
        )
        if str(value)
    ]
    if row.get("consumer_step_id") and str(row.get("consumer_step_id")) not in affected_step_ids:
        affected_step_ids.append(str(row.get("consumer_step_id")))
    artifact_ref = deepcopy(dict(row.get("artifact_ref") or {}))
    if not artifact_ref and row.get("canonical_resource_id"):
        artifact_ref = {
            "kind": "resource",
            "identity": str(row.get("canonical_resource_id") or ""),
        }
    if not artifact_ref and row.get("artifact_id"):
        artifact_ref = {
            "kind": "authoritative",
            "identity": str(row.get("artifact_id") or ""),
        }
    result.setdefault("affected_step_ids", affected_step_ids)
    result.setdefault("affected_artifact", artifact_ref)
    if code == "PLAN_ARTIFACT_PRODUCER_CARDINALITY":
        result.setdefault("expected_identity", {
            **artifact_ref,
            "producer_cardinality": 1,
        })
        result.setdefault("observed_identity", {
            **artifact_ref,
            "producer_step_ids": list(row.get("producer_step_ids") or []),
        })
    elif code == "PLAN_ARTIFACT_UNAVAILABLE":
        result.setdefault("expected_identity", {
            **artifact_ref,
            "availability": "verified_input_or_one_reachable_dependency_producer",
        })
        result.setdefault("observed_identity", {
            **artifact_ref,
            "consumer_step_id": str(row.get("consumer_step_id") or ""),
            "producer_step_ids": list(row.get("producer_step_ids") or []),
        })
    elif code == "PLAN_ARTIFACT_ORIGIN_CONFLICT":
        result.setdefault("expected_identity", {
            **artifact_ref,
            "origin_count_per_step": 1,
        })
        result.setdefault("observed_identity", {
            **artifact_ref,
            "step_id": step_id,
            "origin_conflict": str(row.get("reason") or ""),
        })
    else:
        result.setdefault("expected_identity", artifact_ref)
        result.setdefault("observed_identity", artifact_ref)
    result.setdefault("machine_evidence", {
        key: deepcopy(value)
        for key, value in row.items()
        if key not in {"message", "reason"}
    })
    return result


def validate_planner_candidate_deterministically(
    candidate: Mapping[str, Any],
    *,
    candidate_scope_id: str,
    authoritative_handles: Mapping[str, str],
    availability_origins: Mapping[str, str],
    authoritative_bindings: Optional[Mapping[str, Mapping[str, Any]]] = None,
    validate_dataflow: bool = True,
    require_declared_references: bool = False,
    capability_ids: Sequence[str] = (),
) -> Tuple[Dict[str, ArtifactRefKey], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run the shared schema/identity path and, by default, dataflow admission.

    Production schema-2 admission uses ``validate_dataflow=False`` for its
    first, side-effect-free structure/identity pass.  It then resolves exact
    handles, compiles capability effects, and runs ``compile_plan_dataflow``
    once over that compiled view.  The default remains ``True`` for callers
    that intentionally use this function as the complete compatibility gate.
    """
    structure = structured_plan_candidate_diagnostics(candidate)
    if structure:
        return {}, [], [
            {
                "code": "PLAN_STRUCTURE_INVALID",
                "owner": "planner",
                "validator_id": "structured_plan_schema",
                "field_path": str(item.get("field_path") or "$"),
                "repair_target": str(item.get("field_path") or "$"),
                "message": str(item.get("message") or "Structured Plan is invalid."),
                "disposition": "PROTOCOL_INVALID",
                "expected_type": str(item.get("expected_type") or "value"),
                "received_type": str(item.get("received_type") or "unknown"),
                "received_excerpt": str(item.get("received_excerpt") or ""),
                "affected_step_ids": [],
                "affected_artifact": {},
                "machine_evidence": {
                    key: deepcopy(value)
                    for key, value in item.items()
                    if key not in {"message"}
                },
            }
            for item in structure
        ]
    try:
        ref_keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id=candidate_scope_id,
            authoritative_handles=authoritative_handles,
            authoritative_bindings=authoritative_bindings,
        )
    except PlannerCandidateError as exc:
        diagnostic = exc.to_diagnostic()
        diagnostic.update({
            "validator_id": "plan_artifact_refs",
            "repair_target": str(exc.field_path or "$"),
        })
        return {}, [], [diagnostic]
    try:
        from agent_core.plan_bindings import (
            PlanBindingError,
            compile_plan_resource_bindings,
        )

        resource_bindings = compile_plan_resource_bindings(
            candidate,
            candidate_scope_id=candidate_scope_id,
            authoritative_handles=authoritative_handles,
            availability_origins=availability_origins,
            authoritative_bindings=authoritative_bindings,
            require_declared_references=require_declared_references,
            capability_ids=capability_ids,
        )
    except PlanBindingError as exc:
        diagnostic = (
            exc.to_dict() if hasattr(exc, "to_dict") else {
                "code": "PLAN_RESOURCE_BINDING_INVALID",
                "message": str(exc),
            }
        )
        if diagnostic.get("validation_issues"):
            return ref_keys, usage, list(diagnostic["validation_issues"])
        diagnostic.setdefault("owner", "planner")
        diagnostic.setdefault("validator_id", "plan_resource_bindings")
        diagnostic.setdefault("field_path", "$.proposed_artifacts")
        diagnostic.setdefault("repair_target", diagnostic["field_path"])
        diagnostic.setdefault("disposition", "PROTOCOL_INVALID")
        return ref_keys, usage, [diagnostic]
    issues = (
        validate_candidate_graph_and_flow(
            candidate,
            ref_keys=ref_keys,
            usage=usage,
            availability_origins=availability_origins,
            resource_bindings=resource_bindings,
        )
        if validate_dataflow
        else []
    )
    return (
        ref_keys,
        usage,
        [_candidate_flow_diagnostic(candidate, issue) for issue in issues],
    )


def prepare_allocation_intent(
    candidate: Mapping[str, Any],
    *,
    candidate_scope_id: str,
    planning_epoch_id: str,
) -> Dict[str, Any]:
    """Prepare a side-effect-free plan-local allocation request."""
    if not candidate_scope_id or not planning_epoch_id:
        raise PlannerCandidateError(
            "ALLOCATION_OWNER_MISSING", "$", "candidate scope and PlanningEpoch are required"
        )
    declarations: List[Dict[str, Any]] = []
    for index, raw in enumerate(candidate.get("proposed_artifacts", []) or []):
        if not isinstance(raw, Mapping):
            raise PlannerCandidateError(
                "PROPOSED_ARTIFACT_INVALID",
                f"$.proposed_artifacts[{index}]",
                "must be an object",
                diagnostic={
                    "expected_type": "object",
                    "received_type": _wire_type(raw),
                    "received_excerpt": _canonical_json(raw)[:500],
                },
            )
        local_ref = str(raw.get("local_ref") or "")
        semantic_view = raw.get("semantic_view")
        contract = raw.get("artifact_contract")
        if not isinstance(semantic_view, Mapping):
            raise PlannerCandidateError(
                "ARTIFACT_SEMANTIC_VIEW_INVALID",
                f"$.proposed_artifacts[{index}].semantic_view",
                "must be an object",
                diagnostic={
                    "expected_type": "object",
                    "received_type": _wire_type(
                        semantic_view,
                        missing="semantic_view" not in raw,
                    ),
                    "received_excerpt": _canonical_json(semantic_view)[:500],
                },
            )
        if not isinstance(contract, Mapping):
            raise PlannerCandidateError(
                "ARTIFACT_CONTRACT_INVALID",
                f"$.proposed_artifacts[{index}].artifact_contract",
                "must be an object",
                diagnostic={
                    "expected_type": "object",
                    "received_type": _wire_type(
                        contract,
                        missing="artifact_contract" not in raw,
                    ),
                    "received_excerpt": _canonical_json(contract)[:500],
                },
            )
        object_kind = str(contract.get("object_kind") or "")
        lifecycle = str(contract.get("lifecycle") or "")
        policy_id = str(contract.get("materialization_policy_id") or "")
        if object_kind not in REGISTERED_OBJECT_KINDS:
            raise PlannerCandidateError(
                "ARTIFACT_OBJECT_KIND_UNREGISTERED",
                f"$.proposed_artifacts[{index}].artifact_contract.object_kind",
                object_kind,
                diagnostic={
                    "expected_type": "registered object kind",
                    "received_type": "string",
                    "received_excerpt": _canonical_json(object_kind),
                    "machine_evidence": {
                        "allowed_values": sorted(REGISTERED_OBJECT_KINDS),
                    },
                },
            )
        if lifecycle not in REGISTERED_ARTIFACT_LIFECYCLES:
            raise PlannerCandidateError(
                "ARTIFACT_LIFECYCLE_UNREGISTERED",
                f"$.proposed_artifacts[{index}].artifact_contract.lifecycle",
                lifecycle,
                diagnostic={
                    "expected_type": "registered artifact lifecycle",
                    "received_type": "string",
                    "received_excerpt": _canonical_json(lifecycle),
                    "machine_evidence": {
                        "allowed_values": sorted(REGISTERED_ARTIFACT_LIFECYCLES),
                    },
                },
            )
        if policy_id not in REGISTERED_MATERIALIZATION_POLICIES:
            raise PlannerCandidateError(
                "ARTIFACT_POLICY_UNREGISTERED",
                f"$.proposed_artifacts[{index}].artifact_contract.materialization_policy_id",
                policy_id,
                diagnostic={
                    "expected_type": "registered materialization policy",
                    "received_type": "string",
                    "received_excerpt": _canonical_json(policy_id),
                    "machine_evidence": {
                        "allowed_values": sorted(REGISTERED_MATERIALIZATION_POLICIES),
                    },
                },
            )
        declarations.append({
            "local_ref": local_ref,
            "artifact_ref": ArtifactRefKey.candidate_local(
                candidate_scope_id, local_ref
            ).to_dict(),
            "semantic_view": deepcopy(dict(semantic_view)),
            "artifact_contract": deepcopy(dict(contract)),
            "resource_binding": deepcopy(dict(raw.get("resource_binding") or {})),
        })
    return {
        "schema_version": 1,
        "candidate_scope_id": str(candidate_scope_id),
        "planning_epoch_id": str(planning_epoch_id),
        "declarations": declarations,
        "status": "prepared",
    }


def promote_allocation_intent(
    intent: Mapping[str, Any],
    *,
    task_id: str,
    managed_root: str,
) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    """Allocate identities/bindings in a caller-held atomic commit section."""
    scope = str(intent.get("candidate_scope_id") or "")
    if not scope or str(intent.get("status") or "") != "prepared":
        raise PlanContractError("ALLOCATION_INTENT_INVALID")
    mapping: Dict[str, str] = {}
    bindings: Dict[str, Dict[str, Any]] = {}
    for raw in intent.get("declarations", []) or []:
        declaration = dict(raw or {})
        local_ref = str(declaration.get("local_ref") or "")
        digest = hashlib.sha256(
            f"{task_id}|{scope}|{local_ref}".encode("utf-8")
        ).hexdigest()[:24]
        logical_id = f"plan-artifact-{digest}"
        contract = dict(declaration.get("artifact_contract") or {})
        resource_binding = dict(declaration.get("resource_binding") or {})
        object_kind = str(contract.get("object_kind") or "")
        canonical_path = f"{str(managed_root).rstrip('/')}/{logical_id}"
        mapping[local_ref] = logical_id
        bindings[logical_id] = {
            "logical_artifact_id": logical_id,
            "artifact_binding_revision": 1,
            "canonical_path": canonical_path,
            "object_kind": object_kind,
            "materialization_policy_id": str(contract.get("materialization_policy_id") or ""),
            "lifecycle": str(contract.get("lifecycle") or ""),
            "availability_origin": AvailabilityOrigin.NOT_YET_AVAILABLE.value,
            "resource_kind": str(
                resource_binding.get("resource_kind")
                or contract.get("resource_kind")
                or "immutable_artifact"
            ),
            "canonical_resource_id": str(
                resource_binding.get("canonical_resource_id")
                or resource_binding.get("resource_id")
                or logical_id
            ),
            "resource_binding": deepcopy(resource_binding),
            "semantic_view": deepcopy(dict(declaration.get("semantic_view") or {})),
            "candidate_scope_id": scope,
            "local_ref": local_ref,
        }
    return mapping, bindings


def rewrite_candidate_local_refs(
    candidate: Mapping[str, Any],
    mapping: Mapping[str, str],
) -> Dict[str, Any]:
    def rewrite_artifact_effect(value: Any) -> Any:
        if isinstance(value, str):
            return str(mapping.get(value, value))
        if not isinstance(value, Mapping):
            return deepcopy(value)
        row = deepcopy(dict(value))
        for key in (
            "artifact_id", "logical_artifact_id", "identity", "id", "name",
        ):
            identity = str(row.get(key) or "")
            if identity in mapping:
                row[key] = str(mapping[identity])
        return row

    def rewrite_workspace_effect(value: Any) -> Any:
        if isinstance(value, str):
            return str(mapping.get(value, value))
        if not isinstance(value, Mapping):
            return deepcopy(value)
        row = deepcopy(dict(value))
        for key in (
            "identity", "workspace_ref", "workspace_id", "artifact_id",
            "object_ref", "name", "id",
        ):
            identity = str(row.get(key) or "")
            if identity in mapping:
                row[key] = str(mapping[identity])
        return row

    result = deepcopy(dict(candidate))
    for step in result.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        for field in ("inputs", "outputs"):
            step[field] = [
                str(mapping.get(str(value), str(value)))
                for value in step.get(field, []) or []
            ]
        if "produces_artifacts" in step:
            step["produces_artifacts"] = [
                rewrite_artifact_effect(value)
                for value in step.get("produces_artifacts", []) or []
            ]
        if "workspace_mutations" in step:
            step["workspace_mutations"] = [
                rewrite_workspace_effect(value)
                for value in step.get("workspace_mutations", []) or []
            ]
        if isinstance(step.get("step_effect"), Mapping):
            effect = deepcopy(dict(step["step_effect"]))
            effect["produces_artifacts"] = [
                rewrite_artifact_effect(value)
                for value in effect.get("produces_artifacts", []) or []
            ]
            effect["workspace_mutations"] = [
                rewrite_workspace_effect(value)
                for value in effect.get("workspace_mutations", []) or []
            ]
            step["step_effect"] = effect
    for declaration in result.get("proposed_artifacts", []) or []:
        if not isinstance(declaration, dict):
            continue
        local_ref = str(declaration.get("local_ref") or "")
        allocated = str(mapping.get(local_ref) or "")
        resource_binding = declaration.get("resource_binding")
        if not allocated or not isinstance(resource_binding, Mapping):
            continue
        binding = deepcopy(dict(resource_binding))
        aliases = {
            str(value)
            for value in binding.get("aliases", []) or []
            if str(value)
        }
        aliases.update({local_ref, allocated})
        binding["aliases"] = sorted(aliases)
        declaration["resource_binding"] = binding
    return result


class AvailabilityOrigin(str, Enum):
    VERIFIED_TASK_INPUT = "verified_task_input"
    ADMITTED_RESUME_VERSION = "admitted_resume_version"
    CURRENT_PLAN_PRODUCED = "current_plan_produced"
    NOT_YET_AVAILABLE = "not_yet_available"


def validate_v1_no_in_place(
    usage: Sequence[Mapping[str, Any]],
    *,
    availability_origins: Mapping[str, str],
) -> List[Dict[str, Any]]:
    producers: Dict[str, List[str]] = {}
    consumers_by_step: Dict[str, set[str]] = {}
    for row in usage:
        ref = dict(row.get("artifact_ref") or {})
        if ref.get("kind") != "authoritative":
            continue
        identity = str(ref.get("identity") or "")
        step_id = str(row.get("step_id") or "")
        if row.get("relation") == "produce":
            producers.setdefault(identity, []).append(step_id)
        elif row.get("relation") == "consume":
            consumers_by_step.setdefault(step_id, set()).add(identity)
    issues: List[Dict[str, Any]] = []
    for identity, step_ids in producers.items():
        origin = str(availability_origins.get(identity) or AvailabilityOrigin.NOT_YET_AVAILABLE.value)
        if origin in {
            AvailabilityOrigin.VERIFIED_TASK_INPUT.value,
            AvailabilityOrigin.ADMITTED_RESUME_VERSION.value,
        } or len(step_ids) != 1:
            issues.append({
                "code": "PLAN_ARTIFACT_ORIGIN_CONFLICT",
                "artifact_id": identity,
                "availability_origin": origin,
                "producer_step_ids": list(step_ids),
            })
        for step_id in step_ids:
            if identity in consumers_by_step.get(step_id, set()):
                issues.append({
                    "code": "PLAN_ARTIFACT_ORIGIN_CONFLICT",
                    "artifact_id": identity,
                    "availability_origin": origin,
                    "producer_step_ids": [step_id],
                    "reason": "same_step_consume_and_produce",
                })
    return issues


def reconciliation_fingerprint(
    outcome: Mapping[str, Any],
    *,
    task_semantic_identity: str,
) -> Tuple[str, Dict[str, Any]]:
    facts = outcome.get("missing_semantic_facts")
    if not isinstance(facts, list) or not facts:
        raise PlannerCandidateError(
            "RECONCILIATION_FACTS_INVALID",
            "$.missing_semantic_facts",
            "must be a non-empty array",
        )
    missing: set[str] = set()
    operations: set[str] = set()
    decisions: set[str] = set()
    for index, raw in enumerate(facts):
        if not isinstance(raw, Mapping):
            raise PlannerCandidateError(
                "RECONCILIATION_FACT_INVALID",
                f"$.missing_semantic_facts[{index}]",
                "must be an object",
            )
        fact_key = str(raw.get("fact_key") or "").strip()
        operation = str(raw.get("affected_operation_id") or "").strip()
        if not fact_key or not operation:
            raise PlannerCandidateError(
                "RECONCILIATION_FACT_ID_MISSING",
                f"$.missing_semantic_facts[{index}]",
                "fact_key and affected_operation_id are required",
            )
        missing.add(fact_key)
        operations.add(operation)
        for decision in raw.get("relevant_confirmed_decision_ids", []) or []:
            if str(decision).strip():
                decisions.add(str(decision).strip())
    payload = {
        "fingerprint_schema_version": RECONCILIATION_FINGERPRINT_SCHEMA_VERSION,
        "task_semantic_identity": str(task_semantic_identity or ""),
        "missing_fact_keys": sorted(missing),
        "affected_operation_ids": sorted(operations),
        "relevant_confirmed_decision_ids": sorted(decisions),
    }
    return _digest(payload), payload


def relevant_reconciliation_evidence_revision(
    outcome: Mapping[str, Any],
    task_spec: Mapping[str, Any],
) -> int:
    """Version only typed evidence related to the reconciliation keys.

    Unscoped conversation/audit growth is deliberately excluded, so an
    unrelated durable turn cannot reset the convergence guard.
    """
    fact_keys: set[str] = set()
    decision_ids: set[str] = set()
    for raw in outcome.get("missing_semantic_facts", []) or []:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("fact_key") or ""):
            fact_keys.add(str(raw.get("fact_key")))
        decision_ids.update(
            str(value)
            for value in raw.get("relevant_confirmed_decision_ids", []) or []
            if str(value)
        )
    evidence: List[Dict[str, Any]] = []
    for field in (
        "confirmed_decisions", "unresolved_questions", "ambiguities",
        "semantic_constraints", "constraints",
    ):
        for raw in task_spec.get(field, []) or []:
            if not isinstance(raw, Mapping):
                continue
            identifiers = {
                str(raw.get(key) or "")
                for key in (
                    "decision_id", "ambiguity_id", "constraint_id",
                    "semantic_key", "fact_key", "requirement_id",
                )
                if str(raw.get(key) or "")
            }
            if not identifiers.intersection(fact_keys | decision_ids):
                continue
            evidence.append({
                "field": field,
                "identifiers": sorted(identifiers),
                "source_ref": str(raw.get("source_ref") or raw.get("source_id") or ""),
                "source_refs": sorted(
                    str(value) for value in raw.get("source_refs", []) or [] if str(value)
                ),
                "provenance": deepcopy(raw.get("provenance") or []),
            })
    if not evidence:
        return 0
    digest = hashlib.sha256(_canonical_json(evidence).encode("utf-8")).hexdigest()
    return int(digest[:15], 16)


def reconciliation_guard_status(
    ledger: Mapping[str, Any] | None,
    *,
    fingerprint: str,
    task_semantic_identity: str,
    relevant_evidence_revision: int,
    limit: int = MAX_RECONCILIATION_NONPROGRESS,
) -> Dict[str, Any]:
    current = dict(ledger or {})
    same_base = bool(
        str(current.get("last_reconciliation_fingerprint") or "") == str(fingerprint)
        and str(current.get("last_reconciliation_task_semantic_identity") or "")
        == str(task_semantic_identity)
        and int(current.get("last_reconciliation_evidence_revision") or 0)
        == int(relevant_evidence_revision)
    )
    count = int(current.get("reconciliation_nonprogress_count") or 0) if same_base else 0
    return {
        "terminal": bool(same_base and count >= int(limit)),
        "same_base": same_base,
        "nonprogress_count": count,
        "limit": int(limit),
    }


def record_reconciliation_outcome(
    ledger: Mapping[str, Any] | None,
    *,
    fingerprint: str,
    task_semantic_revision: int,
    task_semantic_identity: str,
    relevant_evidence_revision: int,
    consultant_outcome: str,
    semantic_changed: bool,
    genuine_user_owned_ambiguity: bool,
) -> Dict[str, Any]:
    status = reconciliation_guard_status(
        ledger,
        fingerprint=fingerprint,
        task_semantic_identity=task_semantic_identity,
        relevant_evidence_revision=relevant_evidence_revision,
    )
    nonprogress = bool(
        not semantic_changed
        and not genuine_user_owned_ambiguity
    )
    count = (
        status["nonprogress_count"] + 1
        if nonprogress and status["same_base"]
        else 1
        if nonprogress
        else 0
    )
    return {
        "last_reconciliation_fingerprint": str(fingerprint),
        "last_reconciliation_task_semantic_revision": int(task_semantic_revision),
        "last_reconciliation_task_semantic_identity": str(task_semantic_identity),
        "last_reconciliation_evidence_revision": int(relevant_evidence_revision),
        "reconciliation_nonprogress_count": count,
        "last_consultant_outcome": str(consultant_outcome or ""),
        "terminal_status": (
            "TASKSPEC_RECONCILIATION_NONPROGRESS"
            if count >= MAX_RECONCILIATION_NONPROGRESS
            else ""
        ),
    }


def start_planning_epoch(
    *,
    task_revision: int,
    task_semantic_identity: str,
    base_plan_revision: int,
    base_plan_snapshot_id: str = "",
    base_plan_semantic_identity: str = "",
    parent_epoch_id: str = "",
    start_reason: str = "initial_planning",
    planner_feedback_revision: int = 0,
) -> Dict[str, Any]:
    return {
        "schema_version": PLANNING_EPOCH_SCHEMA_VERSION,
        "planning_epoch_id": f"planning-epoch-{uuid.uuid4()}",
        "parent_epoch_id": str(parent_epoch_id or ""),
        "start_reason": str(start_reason or "initial_planning"),
        "base_task_revision": int(task_revision),
        "base_task_semantic_identity": str(task_semantic_identity or ""),
        "base_plan_revision": int(base_plan_revision),
        "base_plan_snapshot_id": str(base_plan_snapshot_id or ""),
        "base_plan_semantic_identity": str(base_plan_semantic_identity or ""),
        "base_planner_feedback_revision": int(planner_feedback_revision or 0),
        "planner_calls_total": 0,
        "candidate_repairs": 0,
        "stale_redispatches": 0,
        "status": "active",
    }


def planning_epoch_matches(
    epoch: Mapping[str, Any] | None,
    *,
    task_semantic_identity: str,
    base_plan_snapshot_id: str,
    base_plan_semantic_identity: str = "",
    planner_feedback_revision: int = 0,
) -> bool:
    value = dict(epoch or {})
    return bool(
        value.get("status") == "active"
        and str(value.get("base_task_semantic_identity") or "")
        == str(task_semantic_identity or "")
        and str(value.get("base_plan_snapshot_id") or "")
        == str(base_plan_snapshot_id or "")
        and str(value.get("base_plan_semantic_identity") or "")
        == str(base_plan_semantic_identity or "")
        and int(value.get("base_planner_feedback_revision") or 0)
        == int(planner_feedback_revision or 0)
    )


def typed_task_plan_traceability(
    task_spec: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Build ID/relation-only review edges; never compare semantic prose."""
    input_ids = {
        str(item.get("artifact_id") or item.get("input_id") or "")
        for item in task_spec.get("inputs", []) or []
        if isinstance(item, Mapping)
    }
    output_ids = {
        str(item.get("artifact_id") or item.get("output_id") or "")
        for item in task_spec.get("output_contracts", []) or []
        if isinstance(item, Mapping)
    }
    edges: List[Dict[str, Any]] = []
    for step in steps:
        step_id = str(step.get("step_id") or "")
        for ref in step.get("inputs", []) or []:
            ref_id = str(ref or "")
            if ref_id in input_ids:
                edges.append({
                    "source_id": ref_id, "target_id": step_id,
                    "relation": "task_input_consumed_by_step", "validation_status": "valid",
                })
        for ref in step.get("outputs", []) or []:
            ref_id = str(ref or "")
            if ref_id in output_ids:
                edges.append({
                    "source_id": ref_id, "target_id": step_id,
                    "relation": "task_output_produced_by_step", "validation_status": "valid",
                })
    return sorted(
        edges,
        key=lambda item: (item["relation"], item["source_id"], item["target_id"]),
    )

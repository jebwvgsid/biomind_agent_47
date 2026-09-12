"""Immutable, route-blind contracts exchanged between agent stages.

The parent workflow may persist full local transcripts, but none of those
transcripts cross a stage boundary through this module.  Every contract has a
closed top-level schema and recursively rejects conversation payloads, full
Skill documents, and graph/controller routing metadata.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Mapping


STAGE_CONTRACT_SCHEMA_VERSION = 1


class StageContractError(ValueError):
    """Stable fail-closed error raised by a stage-boundary contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "STAGE_CONTRACT_ERROR")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


_FORBIDDEN_ROUTE_KEYS = frozenset({
    "next_step",
    "next_route",
    "target_route",
    "goto",
    "dispatch_route",
    "effective_route",
    "route_resolution",
    "proposed_route",
    "node_recommended_next_step",
    "required_next_action",
    "pending_recovery_action",
    "graph_node",
    "graph_node_name",
    "source_graph_node",
    "destination_graph_node",
    "controller_route",
    "controller_transition",
    "controller_transition_metadata",
    "command_goto",
    "resume_route_hint",
    "resume_step",
    "terminal_route",
    "pre_guard_effective_route",
    "override_source",
    "override_decision_id",
    "route_event_id",
    "resolved_resume_node",
})

_FORBIDDEN_CONTEXT_KEYS = frozenset({
    "message",
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
    "document",
    "documents",
    "exact_text",
    "raw_response",
    "raw_content",
    "system_prompt",
    "prompt",
    "tool_calls",
    "tool_output",
    "tool_outputs",
    "stdout",
    "stderr",
    "raw_log",
})


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _reject_forbidden_key(key: str, path: tuple[str, ...]) -> None:
    normalized = _normalized_key(key)
    location = ".".join((*path, key))
    if (
        normalized in _FORBIDDEN_ROUTE_KEYS
        or normalized.startswith("graph_node_")
        or normalized.startswith("controller_route_")
        or normalized.startswith("controller_transition_")
        or normalized.endswith("_goto")
    ):
        raise StageContractError("STAGE_ROUTE_FIELD_FORBIDDEN", location)
    if (
        normalized in _FORBIDDEN_CONTEXT_KEYS
        or normalized.endswith("_transcript")
        or normalized.endswith("_messages")
        or "full_skill_doc" in normalized
    ):
        raise StageContractError("STAGE_CONTEXT_LEAK_FORBIDDEN", location)


def _freeze_json(value: Any, *, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise StageContractError(
                    "STAGE_JSON_INVALID",
                    f"{'.'.join(path) or '<root>'} contains a non-string key",
                )
            key = raw_key.strip()
            if not key:
                raise StageContractError(
                    "STAGE_JSON_INVALID", "empty object keys are not allowed"
                )
            _reject_forbidden_key(key, path)
            frozen[key] = _freeze_json(item, path=(*path, key))
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, path=(*path, str(index)))
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StageContractError("STAGE_JSON_INVALID", "non-finite number")
        return value
    raise StageContractError(
        "STAGE_JSON_INVALID", f"unsupported value type: {type(value).__name__}"
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def freeze(value: Any) -> Any:
    """Return an immutable JSON value after applying boundary leak checks."""

    return _freeze_json(value)


def thaw(value: Any) -> Any:
    """Return a detached, mutable JSON representation of a frozen value."""

    return _thaw_json(value)


def _validate_schema(value: Any, contract_name: str) -> int:
    if type(value) is not int:
        raise StageContractError(
            "STAGE_SCHEMA_UNSUPPORTED", f"{contract_name}.schema_version={value}"
        )
    version = value
    if version != STAGE_CONTRACT_SCHEMA_VERSION:
        raise StageContractError(
            "STAGE_SCHEMA_UNSUPPORTED", f"{contract_name}.schema_version={version}"
        )
    return version


def _validate_fields(
    value: Mapping[str, Any],
    *,
    contract_name: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StageContractError(
            "STAGE_OBJECT_REQUIRED", f"{contract_name} received {type(value).__name__}"
        )
    raw = dict(value)
    missing = sorted(required - set(raw))
    unknown = sorted(set(raw) - required - optional)
    if missing:
        raise StageContractError(
            "STAGE_FIELD_REQUIRED", f"{contract_name}: {', '.join(missing)}"
        )
    if unknown:
        raise StageContractError(
            "STAGE_FIELD_UNKNOWN", f"{contract_name}: {', '.join(unknown)}"
        )
    # Scan the complete input before field-specific conversion so a nested
    # leakage key always fails with the stable leakage/route code.
    _freeze_json(raw, path=(contract_name,))
    return raw


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StageContractError("STAGE_FIELD_REQUIRED", field)
    return text


def _optional_text(value: Any) -> str:
    return str(value or "").strip()


def _nonnegative_int(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise StageContractError("STAGE_FIELD_INVALID", f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise StageContractError(
            "STAGE_FIELD_INVALID", f"{field} must be an integer"
        ) from exc
    if parsed < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise StageContractError(
            "STAGE_FIELD_INVALID", f"{field} must be {qualifier}"
        )
    return parsed


def _text_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise StageContractError("STAGE_FIELD_INVALID", f"{field} must be an array")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise StageContractError(
                "STAGE_FIELD_INVALID", f"{field} contains an empty value"
            )
        if text not in result:
            result.append(text)
    return tuple(result)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StageContractError("STAGE_FIELD_INVALID", f"{field} must be an object")
    return _freeze_json(dict(value), path=(field,))


def _mapping_rows(
    value: Any,
    field: str,
    *,
    required: frozenset[str] = frozenset(),
    allowed: frozenset[str] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise StageContractError("STAGE_FIELD_INVALID", f"{field} must be an array")
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise StageContractError(
                "STAGE_FIELD_INVALID", f"{field}[{index}] must be an object"
            )
        raw = dict(item)
        missing = sorted(required - set(raw))
        unknown = sorted(set(raw) - allowed) if allowed is not None else []
        if missing:
            raise StageContractError(
                "STAGE_FIELD_REQUIRED",
                f"{field}[{index}]: {', '.join(missing)}",
            )
        if unknown:
            raise StageContractError(
                "STAGE_FIELD_UNKNOWN",
                f"{field}[{index}]: {', '.join(unknown)}",
            )
        rows.append(_freeze_json(raw, path=(field, str(index))))
    return tuple(rows)


def _json_round_trip(value: Mapping[str, Any]) -> None:
    json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _contract_mapping(value: Any, contract_type: type[Any], field: str) -> Any:
    if isinstance(value, contract_type):
        return value
    if not isinstance(value, Mapping):
        raise StageContractError("STAGE_FIELD_INVALID", f"{field} must be an object")
    return contract_type.from_mapping(value)


class ImmutableStageContract:
    """Shared freeze/thaw API for a concrete stage contract."""

    @classmethod
    def freeze(cls, value: Mapping[str, Any]) -> Any:
        if isinstance(value, cls):
            return value
        return cls.from_mapping(value)

    def thaw(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True)
class TaskSpecRef(ImmutableStageContract):
    schema_version: int
    task_id: str
    task_revision: int
    task_semantic_hash: str
    task_record_hash: str
    task_spec_ref: str = ""

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "task_id", "task_revision", "task_semantic_hash",
        "task_record_hash",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"task_spec_ref"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        object.__setattr__(self, "task_id", _required_text(self.task_id, "TaskSpecRef.task_id"))
        object.__setattr__(self, "task_revision", _nonnegative_int(self.task_revision, "TaskSpecRef.task_revision", positive=True))
        object.__setattr__(self, "task_semantic_hash", _required_text(self.task_semantic_hash, "TaskSpecRef.task_semantic_hash"))
        object.__setattr__(self, "task_record_hash", _required_text(self.task_record_hash, "TaskSpecRef.task_record_hash"))
        object.__setattr__(self, "task_spec_ref", _optional_text(self.task_spec_ref))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TaskSpecRef":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(**{key: raw.get(key, "") for key in (*cls.REQUIRED, *cls.OPTIONAL)})

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "task_semantic_hash": self.task_semantic_hash,
            "task_record_hash": self.task_record_hash,
        }
        if self.task_spec_ref:
            result["task_spec_ref"] = self.task_spec_ref
        return result


@dataclass(frozen=True)
class SelectedSkillRef(ImmutableStageContract):
    schema_version: int
    skill_id: str
    snapshot_id: str
    content_hash: str
    scope: str
    source_path: str = ""
    target_step_id: str = ""
    description: str = ""
    heading_refs: tuple[str, ...] = ()

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "skill_id", "snapshot_id", "content_hash", "scope",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({
        "source_path", "target_step_id", "description", "heading_refs",
    })
    ALLOWED_SCOPES: ClassVar[frozenset[str]] = frozenset({"task", "step"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        for field in ("skill_id", "snapshot_id", "content_hash"):
            object.__setattr__(self, field, _required_text(getattr(self, field), f"SelectedSkillRef.{field}"))
        scope = _required_text(self.scope, "SelectedSkillRef.scope").lower()
        if scope not in self.ALLOWED_SCOPES:
            raise StageContractError("STAGE_FIELD_INVALID", f"SelectedSkillRef.scope={scope}")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "source_path", _optional_text(self.source_path))
        object.__setattr__(self, "target_step_id", _optional_text(self.target_step_id))
        object.__setattr__(self, "description", _optional_text(self.description))
        object.__setattr__(self, "heading_refs", _text_tuple(self.heading_refs, "SelectedSkillRef.heading_refs"))
        if scope == "step" and not self.target_step_id:
            raise StageContractError("STAGE_FIELD_REQUIRED", "SelectedSkillRef.target_step_id")
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SelectedSkillRef":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"],
            skill_id=raw["skill_id"],
            snapshot_id=raw["snapshot_id"],
            content_hash=raw["content_hash"],
            scope=raw["scope"],
            source_path=raw.get("source_path", ""),
            target_step_id=raw.get("target_step_id", ""),
            description=raw.get("description", ""),
            heading_refs=tuple(raw.get("heading_refs") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "skill_id": self.skill_id,
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "scope": self.scope,
        }
        for key in ("source_path", "target_step_id", "description"):
            value = getattr(self, key)
            if value:
                result[key] = value
        if self.heading_refs:
            result["heading_refs"] = list(self.heading_refs)
        return result


@dataclass(frozen=True)
class SelectedSkillBundle(ImmutableStageContract):
    schema_version: int
    bundle_id: str
    task_revision: int
    skills: tuple[SelectedSkillRef, ...]
    selection_summary: str = ""

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "bundle_id", "task_revision", "skills",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"selection_summary"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        object.__setattr__(self, "bundle_id", _required_text(self.bundle_id, "SelectedSkillBundle.bundle_id"))
        object.__setattr__(self, "task_revision", _nonnegative_int(self.task_revision, "SelectedSkillBundle.task_revision", positive=True))
        parsed = tuple(
            _contract_mapping(item, SelectedSkillRef, f"SelectedSkillBundle.skills[{index}]")
            for index, item in enumerate(self.skills)
        )
        # A valid task may require no specialized Skill.  Preserve an explicit
        # empty manifest instead of inventing a synthetic binding.
        identities = [(item.skill_id, item.scope, item.target_step_id) for item in parsed]
        if len(identities) != len(set(identities)):
            raise StageContractError("STAGE_FIELD_INVALID", "SelectedSkillBundle.skills contains duplicate scope identities")
        object.__setattr__(self, "skills", parsed)
        object.__setattr__(self, "selection_summary", _optional_text(self.selection_summary))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SelectedSkillBundle":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        skills = raw.get("skills")
        if isinstance(skills, (str, bytes)) or not isinstance(skills, (list, tuple)):
            raise StageContractError("STAGE_FIELD_INVALID", "SelectedSkillBundle.skills must be an array")
        return cls(
            schema_version=raw["schema_version"],
            bundle_id=raw["bundle_id"],
            task_revision=raw["task_revision"],
            skills=tuple(SelectedSkillRef.from_mapping(item) if isinstance(item, Mapping) else item for item in skills),
            selection_summary=raw.get("selection_summary", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "task_revision": self.task_revision,
            "skills": [item.to_dict() for item in self.skills],
        }
        if self.selection_summary:
            result["selection_summary"] = self.selection_summary
        return result


_INPUT_MANIFEST_FIELDS = frozenset({
    "input_id", "role", "path", "format", "required", "availability",
    "content_ref", "metadata",
})


@dataclass(frozen=True)
class ConsultantSeed(ImmutableStageContract):
    schema_version: int
    seed_id: str
    task_id: str
    request_summary: str
    input_manifest: tuple[Mapping[str, Any], ...]
    task_spec_ref: TaskSpecRef | None = None
    constraints: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    skill_catalog_ref: str = ""

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "seed_id", "task_id", "request_summary", "input_manifest",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({
        "task_spec_ref", "constraints", "evidence_refs", "skill_catalog_ref",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        object.__setattr__(self, "seed_id", _required_text(self.seed_id, "ConsultantSeed.seed_id"))
        object.__setattr__(self, "task_id", _required_text(self.task_id, "ConsultantSeed.task_id"))
        object.__setattr__(self, "request_summary", _required_text(self.request_summary, "ConsultantSeed.request_summary"))
        object.__setattr__(self, "input_manifest", _mapping_rows(self.input_manifest, "ConsultantSeed.input_manifest", required=frozenset({"role"}), allowed=_INPUT_MANIFEST_FIELDS))
        task_ref = None if self.task_spec_ref is None else _contract_mapping(self.task_spec_ref, TaskSpecRef, "ConsultantSeed.task_spec_ref")
        if task_ref is not None and task_ref.task_id != self.task_id:
            raise StageContractError("STAGE_SCOPE_MISMATCH", "ConsultantSeed.task_spec_ref.task_id")
        object.__setattr__(self, "task_spec_ref", task_ref)
        object.__setattr__(self, "constraints", _text_tuple(self.constraints, "ConsultantSeed.constraints"))
        object.__setattr__(self, "evidence_refs", _text_tuple(self.evidence_refs, "ConsultantSeed.evidence_refs"))
        object.__setattr__(self, "skill_catalog_ref", _optional_text(self.skill_catalog_ref))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ConsultantSeed":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], seed_id=raw["seed_id"], task_id=raw["task_id"],
            request_summary=raw["request_summary"], input_manifest=tuple(raw["input_manifest"]),
            task_spec_ref=(TaskSpecRef.from_mapping(raw["task_spec_ref"]) if isinstance(raw.get("task_spec_ref"), Mapping) else raw.get("task_spec_ref")),
            constraints=tuple(raw.get("constraints") or ()), evidence_refs=tuple(raw.get("evidence_refs") or ()),
            skill_catalog_ref=raw.get("skill_catalog_ref", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version, "seed_id": self.seed_id,
            "task_id": self.task_id, "request_summary": self.request_summary,
            "input_manifest": _thaw_json(self.input_manifest),
        }
        if self.task_spec_ref is not None:
            result["task_spec_ref"] = self.task_spec_ref.to_dict()
        if self.constraints:
            result["constraints"] = list(self.constraints)
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        if self.skill_catalog_ref:
            result["skill_catalog_ref"] = self.skill_catalog_ref
        return result


@dataclass(frozen=True)
class ConsultationHandoff(ImmutableStageContract):
    schema_version: int
    handoff_id: str
    task_spec_ref: TaskSpecRef
    selected_skill_bundle: SelectedSkillBundle
    summary: str
    capability_decisions: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "handoff_id", "task_spec_ref", "selected_skill_bundle", "summary",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({
        "capability_decisions", "unresolved_questions", "evidence_refs",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        object.__setattr__(self, "handoff_id", _required_text(self.handoff_id, "ConsultationHandoff.handoff_id"))
        task_ref = _contract_mapping(self.task_spec_ref, TaskSpecRef, "ConsultationHandoff.task_spec_ref")
        bundle = _contract_mapping(self.selected_skill_bundle, SelectedSkillBundle, "ConsultationHandoff.selected_skill_bundle")
        if bundle.task_revision != task_ref.task_revision:
            raise StageContractError("STAGE_SCOPE_MISMATCH", "ConsultationHandoff selected Skill bundle task revision")
        object.__setattr__(self, "task_spec_ref", task_ref)
        object.__setattr__(self, "selected_skill_bundle", bundle)
        object.__setattr__(self, "summary", _required_text(self.summary, "ConsultationHandoff.summary"))
        for field in ("capability_decisions", "unresolved_questions", "evidence_refs"):
            object.__setattr__(self, field, _text_tuple(getattr(self, field), f"ConsultationHandoff.{field}"))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ConsultationHandoff":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], handoff_id=raw["handoff_id"],
            task_spec_ref=TaskSpecRef.from_mapping(raw["task_spec_ref"]),
            selected_skill_bundle=SelectedSkillBundle.from_mapping(raw["selected_skill_bundle"]),
            summary=raw["summary"], capability_decisions=tuple(raw.get("capability_decisions") or ()),
            unresolved_questions=tuple(raw.get("unresolved_questions") or ()), evidence_refs=tuple(raw.get("evidence_refs") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version, "handoff_id": self.handoff_id,
            "task_spec_ref": self.task_spec_ref.to_dict(),
            "selected_skill_bundle": self.selected_skill_bundle.to_dict(), "summary": self.summary,
        }
        for field in ("capability_decisions", "unresolved_questions", "evidence_refs"):
            value = getattr(self, field)
            if value:
                result[field] = list(value)
        return result


_ARTIFACT_MANIFEST_FIELDS = frozenset({
    "artifact_id", "role", "format", "path", "content_ref", "source_step_id",
    "availability", "required", "metadata",
})


@dataclass(frozen=True)
class PlannerSeed(ImmutableStageContract):
    schema_version: int
    seed_id: str
    task_id: str
    consultation_handoff: ConsultationHandoff
    artifact_manifest: tuple[Mapping[str, Any], ...] = ()
    planning_constraints: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "seed_id", "task_id", "consultation_handoff",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({
        "artifact_manifest", "planning_constraints", "evidence_refs",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        object.__setattr__(self, "seed_id", _required_text(self.seed_id, "PlannerSeed.seed_id"))
        object.__setattr__(self, "task_id", _required_text(self.task_id, "PlannerSeed.task_id"))
        handoff = _contract_mapping(self.consultation_handoff, ConsultationHandoff, "PlannerSeed.consultation_handoff")
        if handoff.task_spec_ref.task_id != self.task_id:
            raise StageContractError("STAGE_SCOPE_MISMATCH", "PlannerSeed.consultation_handoff.task_id")
        object.__setattr__(self, "consultation_handoff", handoff)
        object.__setattr__(self, "artifact_manifest", _mapping_rows(self.artifact_manifest, "PlannerSeed.artifact_manifest", required=frozenset({"artifact_id"}), allowed=_ARTIFACT_MANIFEST_FIELDS))
        object.__setattr__(self, "planning_constraints", _text_tuple(self.planning_constraints, "PlannerSeed.planning_constraints"))
        object.__setattr__(self, "evidence_refs", _text_tuple(self.evidence_refs, "PlannerSeed.evidence_refs"))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlannerSeed":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], seed_id=raw["seed_id"], task_id=raw["task_id"],
            consultation_handoff=ConsultationHandoff.from_mapping(raw["consultation_handoff"]),
            artifact_manifest=tuple(raw.get("artifact_manifest") or ()),
            planning_constraints=tuple(raw.get("planning_constraints") or ()), evidence_refs=tuple(raw.get("evidence_refs") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version, "seed_id": self.seed_id,
            "task_id": self.task_id, "consultation_handoff": self.consultation_handoff.to_dict(),
        }
        if self.artifact_manifest:
            result["artifact_manifest"] = _thaw_json(self.artifact_manifest)
        if self.planning_constraints:
            result["planning_constraints"] = list(self.planning_constraints)
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        return result


@dataclass(frozen=True)
class PlanSpecRef(ImmutableStageContract):
    schema_version: int
    task_id: str
    plan_id: str
    plan_version: str
    plan_revision: int
    plan_semantic_hash: str
    plan_record_hash: str
    plan_snapshot_ref: str = ""

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "task_id", "plan_id", "plan_version", "plan_revision",
        "plan_semantic_hash", "plan_record_hash",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"plan_snapshot_ref"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        for field in ("task_id", "plan_id", "plan_version", "plan_semantic_hash", "plan_record_hash"):
            object.__setattr__(self, field, _required_text(getattr(self, field), f"PlanSpecRef.{field}"))
        object.__setattr__(self, "plan_revision", _nonnegative_int(self.plan_revision, "PlanSpecRef.plan_revision", positive=True))
        object.__setattr__(self, "plan_snapshot_ref", _optional_text(self.plan_snapshot_ref))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlanSpecRef":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], task_id=raw["task_id"], plan_id=raw["plan_id"],
            plan_version=raw["plan_version"], plan_revision=raw["plan_revision"],
            plan_semantic_hash=raw["plan_semantic_hash"], plan_record_hash=raw["plan_record_hash"],
            plan_snapshot_ref=raw.get("plan_snapshot_ref", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version, "task_id": self.task_id,
            "plan_id": self.plan_id, "plan_version": self.plan_version,
            "plan_revision": self.plan_revision, "plan_semantic_hash": self.plan_semantic_hash,
            "plan_record_hash": self.plan_record_hash,
        }
        if self.plan_snapshot_ref:
            result["plan_snapshot_ref"] = self.plan_snapshot_ref
        return result


_CURRENT_STEP_FIELDS = frozenset({
    "step_id", "title", "goal", "purpose", "action", "inputs", "outputs",
    "dependencies", "success_criteria", "skill_ids", "execution_capability_id",
    "capability_contract_hash", "risk_level", "metadata",
})
_DEPENDENCY_OUTCOME_FIELDS = frozenset({
    "step_id", "status", "summary", "outcome_ref", "evidence_refs",
    "artifact_refs", "binding_refs",
})
_DEPENDENCY_OUTCOME_REQUIRED = frozenset({"step_id", "status", "summary"})
_BINDING_MANIFEST_FIELDS = frozenset({
    "binding_id", "name", "value", "value_ref", "value_type", "producer_step_id",
    "metadata",
})


def _current_step(value: Any, field: str) -> Mapping[str, Any]:
    row = _mapping(value, field)
    unknown = sorted(set(row) - _CURRENT_STEP_FIELDS)
    if unknown:
        raise StageContractError("STAGE_FIELD_UNKNOWN", f"{field}: {', '.join(unknown)}")
    if not str(row.get("step_id") or "").strip():
        raise StageContractError("STAGE_FIELD_REQUIRED", f"{field}.step_id")
    return row


def _dependency_outcomes(value: Any, field: str) -> tuple[Mapping[str, Any], ...]:
    rows = _mapping_rows(value, field, required=_DEPENDENCY_OUTCOME_REQUIRED, allowed=_DEPENDENCY_OUTCOME_FIELDS)
    identities: list[str] = []
    for index, row in enumerate(rows):
        step_id = _required_text(row.get("step_id"), f"{field}[{index}].step_id")
        _required_text(row.get("status"), f"{field}[{index}].status")
        _required_text(row.get("summary"), f"{field}[{index}].summary")
        identities.append(step_id)
    if len(identities) != len(set(identities)):
        raise StageContractError("STAGE_FIELD_INVALID", f"{field} contains duplicate step_id values")
    return rows


@dataclass(frozen=True)
class StepSeed(ImmutableStageContract):
    schema_version: int
    seed_id: str
    task_spec_ref: TaskSpecRef
    plan_spec_ref: PlanSpecRef
    selected_skill_bundle: SelectedSkillBundle
    current_step: Mapping[str, Any]
    dependency_outcomes: tuple[Mapping[str, Any], ...]
    available_artifacts: tuple[Mapping[str, Any], ...] = ()
    accepted_bindings: tuple[Mapping[str, Any], ...] = ()
    workspace: str = ""

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "seed_id", "task_spec_ref", "plan_spec_ref",
        "selected_skill_bundle", "current_step", "dependency_outcomes",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({
        "available_artifacts", "accepted_bindings", "workspace",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        object.__setattr__(self, "seed_id", _required_text(self.seed_id, "StepSeed.seed_id"))
        task_ref = _contract_mapping(self.task_spec_ref, TaskSpecRef, "StepSeed.task_spec_ref")
        plan_ref = _contract_mapping(self.plan_spec_ref, PlanSpecRef, "StepSeed.plan_spec_ref")
        bundle = _contract_mapping(self.selected_skill_bundle, SelectedSkillBundle, "StepSeed.selected_skill_bundle")
        if task_ref.task_id != plan_ref.task_id or bundle.task_revision != task_ref.task_revision:
            raise StageContractError("STAGE_SCOPE_MISMATCH", "StepSeed task/plan/Skill identities")
        object.__setattr__(self, "task_spec_ref", task_ref)
        object.__setattr__(self, "plan_spec_ref", plan_ref)
        object.__setattr__(self, "selected_skill_bundle", bundle)
        step = _current_step(self.current_step, "StepSeed.current_step")
        outcomes = _dependency_outcomes(self.dependency_outcomes, "StepSeed.dependency_outcomes")
        if str(step.get("step_id")) in {str(item.get("step_id")) for item in outcomes}:
            raise StageContractError("STAGE_FIELD_INVALID", "StepSeed current step cannot be its own dependency outcome")
        object.__setattr__(self, "current_step", step)
        object.__setattr__(self, "dependency_outcomes", outcomes)
        object.__setattr__(self, "available_artifacts", _mapping_rows(self.available_artifacts, "StepSeed.available_artifacts", required=frozenset({"artifact_id"}), allowed=_ARTIFACT_MANIFEST_FIELDS))
        object.__setattr__(self, "accepted_bindings", _mapping_rows(self.accepted_bindings, "StepSeed.accepted_bindings", required=frozenset({"binding_id", "name"}), allowed=_BINDING_MANIFEST_FIELDS))
        object.__setattr__(self, "workspace", _optional_text(self.workspace))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StepSeed":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], seed_id=raw["seed_id"],
            task_spec_ref=TaskSpecRef.from_mapping(raw["task_spec_ref"]),
            plan_spec_ref=PlanSpecRef.from_mapping(raw["plan_spec_ref"]),
            selected_skill_bundle=SelectedSkillBundle.from_mapping(raw["selected_skill_bundle"]),
            current_step=raw["current_step"], dependency_outcomes=tuple(raw["dependency_outcomes"]),
            available_artifacts=tuple(raw.get("available_artifacts") or ()),
            accepted_bindings=tuple(raw.get("accepted_bindings") or ()), workspace=raw.get("workspace", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version, "seed_id": self.seed_id,
            "task_spec_ref": self.task_spec_ref.to_dict(), "plan_spec_ref": self.plan_spec_ref.to_dict(),
            "selected_skill_bundle": self.selected_skill_bundle.to_dict(),
            "current_step": _thaw_json(self.current_step),
            "dependency_outcomes": _thaw_json(self.dependency_outcomes),
        }
        if self.available_artifacts:
            result["available_artifacts"] = _thaw_json(self.available_artifacts)
        if self.accepted_bindings:
            result["accepted_bindings"] = _thaw_json(self.accepted_bindings)
        if self.workspace:
            result["workspace"] = self.workspace
        return result


_DIAGNOSTIC_FACT_FIELDS = frozenset({
    "fact_id", "kind", "summary", "evidence_ref", "confidence", "metadata",
})


@dataclass(frozen=True)
class FailurePacket(ImmutableStageContract):
    schema_version: int
    failure_id: str
    task_id: str
    step_id: str
    failure_type: str
    failure_owner: str
    error_code: str
    summary: str
    retryable: bool
    evidence_refs: tuple[str, ...]
    source: str
    source_outcome_ref: str
    plan_spec_ref: PlanSpecRef | None = None
    attempt_ref: str = ""
    action_ref: str = ""
    failure_fingerprint: str = ""
    diagnostic_facts: tuple[Mapping[str, Any], ...] = ()
    inspection_refs: tuple[str, ...] = ()
    # Schema-1 additive lineage.  Legacy packets omit all three fields and
    # retain their original content-addressed wire representation.  New
    # producer packets seal a stable incident identity, a monotonic version,
    # and the exact packet they supersede without changing the stage schema.
    incident_id: str = ""
    packet_version: int = 0
    supersedes_packet_ref: str = ""

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "failure_id", "task_id", "step_id", "failure_type",
        "failure_owner", "error_code", "summary", "retryable", "evidence_refs",
        "source", "source_outcome_ref",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({
        "plan_spec_ref", "attempt_ref", "action_ref", "failure_fingerprint",
        "diagnostic_facts", "inspection_refs", "incident_id",
        "packet_version", "supersedes_packet_ref",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        for field in (
            "failure_id", "task_id", "step_id", "failure_type",
            "failure_owner", "error_code", "summary", "source",
            "source_outcome_ref",
        ):
            object.__setattr__(self, field, _required_text(getattr(self, field), f"FailurePacket.{field}"))
        if not isinstance(self.retryable, bool):
            raise StageContractError("STAGE_FIELD_INVALID", "FailurePacket.retryable must be boolean")
        object.__setattr__(self, "evidence_refs", _text_tuple(self.evidence_refs, "FailurePacket.evidence_refs"))
        plan_ref = None if self.plan_spec_ref is None else _contract_mapping(self.plan_spec_ref, PlanSpecRef, "FailurePacket.plan_spec_ref")
        if plan_ref is not None and plan_ref.task_id != self.task_id:
            raise StageContractError("STAGE_SCOPE_MISMATCH", "FailurePacket.plan_spec_ref.task_id")
        object.__setattr__(self, "plan_spec_ref", plan_ref)
        for field in ("attempt_ref", "action_ref", "failure_fingerprint"):
            object.__setattr__(self, field, _optional_text(getattr(self, field)))
        object.__setattr__(self, "diagnostic_facts", _mapping_rows(self.diagnostic_facts, "FailurePacket.diagnostic_facts", required=frozenset({"fact_id", "kind", "summary"}), allowed=_DIAGNOSTIC_FACT_FIELDS))
        object.__setattr__(self, "inspection_refs", _text_tuple(self.inspection_refs, "FailurePacket.inspection_refs"))
        incident_id = _optional_text(self.incident_id)
        supersedes_packet_ref = _optional_text(self.supersedes_packet_ref)
        if type(self.packet_version) is not int or self.packet_version < 0:
            raise StageContractError(
                "STAGE_FIELD_INVALID",
                "FailurePacket.packet_version must be a non-negative integer",
            )
        packet_version = int(self.packet_version)
        has_lineage = bool(
            incident_id or packet_version or supersedes_packet_ref
        )
        if has_lineage and (not incident_id or packet_version < 1):
            raise StageContractError(
                "FAILURE_PACKET_LINEAGE_INVALID",
                "incident_id and a positive packet_version are required together",
            )
        if packet_version == 1 and supersedes_packet_ref:
            raise StageContractError(
                "FAILURE_PACKET_LINEAGE_INVALID",
                "the first incident packet cannot supersede another packet",
            )
        if packet_version > 1 and not supersedes_packet_ref:
            raise StageContractError(
                "FAILURE_PACKET_LINEAGE_INVALID",
                "a later incident packet must identify the packet it supersedes",
            )
        object.__setattr__(self, "incident_id", incident_id)
        object.__setattr__(self, "packet_version", packet_version)
        object.__setattr__(
            self, "supersedes_packet_ref", supersedes_packet_ref
        )
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FailurePacket":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], failure_id=raw["failure_id"], task_id=raw["task_id"],
            step_id=raw["step_id"], failure_type=raw["failure_type"], failure_owner=raw["failure_owner"],
            error_code=raw["error_code"], summary=raw["summary"], retryable=raw["retryable"],
            evidence_refs=tuple(raw["evidence_refs"]), source=raw["source"],
            source_outcome_ref=raw["source_outcome_ref"],
            plan_spec_ref=(PlanSpecRef.from_mapping(raw["plan_spec_ref"]) if isinstance(raw.get("plan_spec_ref"), Mapping) else raw.get("plan_spec_ref")),
            attempt_ref=raw.get("attempt_ref", ""), action_ref=raw.get("action_ref", ""),
            failure_fingerprint=raw.get("failure_fingerprint", ""), diagnostic_facts=tuple(raw.get("diagnostic_facts") or ()),
            inspection_refs=tuple(raw.get("inspection_refs") or ()),
            incident_id=raw.get("incident_id", ""),
            packet_version=raw.get("packet_version", 0),
            supersedes_packet_ref=raw.get("supersedes_packet_ref", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version, "failure_id": self.failure_id,
            "task_id": self.task_id, "step_id": self.step_id, "failure_type": self.failure_type,
            "failure_owner": self.failure_owner, "error_code": self.error_code,
            "summary": self.summary, "retryable": self.retryable,
            "evidence_refs": list(self.evidence_refs), "source": self.source,
            "source_outcome_ref": self.source_outcome_ref,
        }
        if self.plan_spec_ref is not None:
            result["plan_spec_ref"] = self.plan_spec_ref.to_dict()
        for field in ("attempt_ref", "action_ref", "failure_fingerprint"):
            value = getattr(self, field)
            if value:
                result[field] = value
        if self.diagnostic_facts:
            result["diagnostic_facts"] = _thaw_json(self.diagnostic_facts)
        if self.inspection_refs:
            result["inspection_refs"] = list(self.inspection_refs)
        if self.incident_id:
            result["incident_id"] = self.incident_id
            result["packet_version"] = self.packet_version
        if self.supersedes_packet_ref:
            result["supersedes_packet_ref"] = self.supersedes_packet_ref
        return result


@dataclass(frozen=True)
class RecoverySeed(ImmutableStageContract):
    schema_version: int
    seed_id: str
    task_spec_ref: TaskSpecRef
    plan_spec_ref: PlanSpecRef
    selected_skill_bundle: SelectedSkillBundle
    current_step: Mapping[str, Any]
    failure_packet: FailurePacket
    dependency_outcomes: tuple[Mapping[str, Any], ...]
    prior_directive_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "seed_id", "task_spec_ref", "plan_spec_ref",
        "selected_skill_bundle", "current_step", "failure_packet", "dependency_outcomes",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"prior_directive_refs", "evidence_refs"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        object.__setattr__(self, "seed_id", _required_text(self.seed_id, "RecoverySeed.seed_id"))
        task_ref = _contract_mapping(self.task_spec_ref, TaskSpecRef, "RecoverySeed.task_spec_ref")
        plan_ref = _contract_mapping(self.plan_spec_ref, PlanSpecRef, "RecoverySeed.plan_spec_ref")
        bundle = _contract_mapping(self.selected_skill_bundle, SelectedSkillBundle, "RecoverySeed.selected_skill_bundle")
        failure = _contract_mapping(self.failure_packet, FailurePacket, "RecoverySeed.failure_packet")
        if len({task_ref.task_id, plan_ref.task_id, failure.task_id}) != 1 or bundle.task_revision != task_ref.task_revision:
            raise StageContractError("STAGE_SCOPE_MISMATCH", "RecoverySeed task/plan/failure/Skill identities")
        step = _current_step(self.current_step, "RecoverySeed.current_step")
        if str(step.get("step_id")) != failure.step_id:
            raise StageContractError("STAGE_SCOPE_MISMATCH", "RecoverySeed failure step")
        object.__setattr__(self, "task_spec_ref", task_ref)
        object.__setattr__(self, "plan_spec_ref", plan_ref)
        object.__setattr__(self, "selected_skill_bundle", bundle)
        object.__setattr__(self, "failure_packet", failure)
        object.__setattr__(self, "current_step", step)
        object.__setattr__(self, "dependency_outcomes", _dependency_outcomes(self.dependency_outcomes, "RecoverySeed.dependency_outcomes"))
        object.__setattr__(self, "prior_directive_refs", _text_tuple(self.prior_directive_refs, "RecoverySeed.prior_directive_refs"))
        object.__setattr__(self, "evidence_refs", _text_tuple(self.evidence_refs, "RecoverySeed.evidence_refs"))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecoverySeed":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], seed_id=raw["seed_id"],
            task_spec_ref=TaskSpecRef.from_mapping(raw["task_spec_ref"]), plan_spec_ref=PlanSpecRef.from_mapping(raw["plan_spec_ref"]),
            selected_skill_bundle=SelectedSkillBundle.from_mapping(raw["selected_skill_bundle"]),
            current_step=raw["current_step"], failure_packet=FailurePacket.from_mapping(raw["failure_packet"]),
            dependency_outcomes=tuple(raw["dependency_outcomes"]),
            prior_directive_refs=tuple(raw.get("prior_directive_refs") or ()), evidence_refs=tuple(raw.get("evidence_refs") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version, "seed_id": self.seed_id,
            "task_spec_ref": self.task_spec_ref.to_dict(), "plan_spec_ref": self.plan_spec_ref.to_dict(),
            "selected_skill_bundle": self.selected_skill_bundle.to_dict(),
            "current_step": _thaw_json(self.current_step), "failure_packet": self.failure_packet.to_dict(),
            "dependency_outcomes": _thaw_json(self.dependency_outcomes),
        }
        if self.prior_directive_refs:
            result["prior_directive_refs"] = list(self.prior_directive_refs)
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        return result


@dataclass(frozen=True)
class RecoveryDirective(ImmutableStageContract):
    schema_version: int
    directive_id: str
    task_id: str
    action: str
    summary: str
    reason_code: str
    target_step_id: str = ""
    instruction: str = ""
    question: str = ""
    requested_slots: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    diagnostic_operation: str = ""
    diagnostic_inputs: tuple[str, ...] = ()
    result_selections: tuple[Mapping[str, Any], ...] = ()

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "directive_id", "task_id", "action", "summary", "reason_code",
    })
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({
        "target_step_id", "instruction", "question", "requested_slots", "evidence_refs", "diagnostic_operation", "diagnostic_inputs", "result_selections",
    })
    ALLOWED_ACTIONS: ClassVar[frozenset[str]] = frozenset({
        "retry_step", "revise_plan", "reconsult", "revalidate", "ask_user", "blocked", "run_diagnostic_probe", "organize_result",
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _validate_schema(self.schema_version, type(self).__name__))
        for field in ("directive_id", "task_id", "summary", "reason_code"):
            object.__setattr__(self, field, _required_text(getattr(self, field), f"RecoveryDirective.{field}"))
        action = _required_text(self.action, "RecoveryDirective.action").lower()
        if action not in self.ALLOWED_ACTIONS:
            raise StageContractError("RECOVERY_ACTION_UNKNOWN", action)
        object.__setattr__(self, "action", action)
        for field in ("target_step_id", "instruction", "question", "diagnostic_operation"):
            object.__setattr__(self, field, _optional_text(getattr(self, field)))
        object.__setattr__(self, "requested_slots", _text_tuple(self.requested_slots, "RecoveryDirective.requested_slots"))
        object.__setattr__(self, "evidence_refs", _text_tuple(self.evidence_refs, "RecoveryDirective.evidence_refs"))
        if action in {"retry_step", "revalidate", "run_diagnostic_probe", "organize_result"} and not self.target_step_id:
            raise StageContractError("STAGE_FIELD_REQUIRED", "RecoveryDirective.target_step_id")
        if action in {"retry_step", "revise_plan", "reconsult", "run_diagnostic_probe", "organize_result"} and not self.instruction:
            raise StageContractError("STAGE_FIELD_REQUIRED", "RecoveryDirective.instruction")
        object.__setattr__(self, "diagnostic_inputs", _text_tuple(self.diagnostic_inputs, "RecoveryDirective.diagnostic_inputs"))
        if action == "run_diagnostic_probe" and (not self.diagnostic_operation or not self.diagnostic_inputs):
            raise StageContractError("STAGE_FIELD_REQUIRED", "RecoveryDirective diagnostic_operation/diagnostic_inputs")
        if action != "run_diagnostic_probe" and (self.diagnostic_operation or self.diagnostic_inputs):
            raise StageContractError("STAGE_FIELD_INVALID", "diagnostic fields require run_diagnostic_probe")
        if action == "ask_user" and (not self.question or not self.requested_slots):
            raise StageContractError("STAGE_FIELD_REQUIRED", "RecoveryDirective ask_user question/requested_slots")
        selections = []
        seen = set()
        if not isinstance(self.result_selections, (list, tuple)) or len(self.result_selections) > 32:
            raise StageContractError("STAGE_FIELD_INVALID", "RecoveryDirective.result_selections")
        for raw in self.result_selections:
            row = _validate_fields(raw, contract_name="RecoveryDirective.result_selections",
                                   required=frozenset({"artifact_id", "observed_path", "operation"}))
            row = {key: _required_text(value, "RecoveryDirective.result_selections." + key)
                   for key, value in row.items()}
            allowed = "accept_actual" if action == "revalidate" else "copy_to_exact"
            if action not in {"revalidate", "organize_result"} or row["operation"] != allowed:
                raise StageContractError("RESULT_SELECTION_ACTION_INVALID", action)
            if row["artifact_id"] in seen:
                raise StageContractError("RESULT_SELECTION_DUPLICATE", row["artifact_id"])
            seen.add(row["artifact_id"])
            selections.append(_freeze_json(row))
        if action == "organize_result" and not selections:
            raise StageContractError("STAGE_FIELD_REQUIRED", "RecoveryDirective.result_selections")
        object.__setattr__(self, "result_selections", tuple(selections))
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecoveryDirective":
        raw = _validate_fields(value, contract_name=cls.__name__, required=cls.REQUIRED, optional=cls.OPTIONAL)
        return cls(
            schema_version=raw["schema_version"], directive_id=raw["directive_id"], task_id=raw["task_id"],
            action=raw["action"], summary=raw["summary"], reason_code=raw["reason_code"],
            target_step_id=raw.get("target_step_id", ""), instruction=raw.get("instruction", ""),
            question=raw.get("question", ""), requested_slots=tuple(raw.get("requested_slots") or ()),
            evidence_refs=tuple(raw.get("evidence_refs") or ()),
            diagnostic_operation=raw.get("diagnostic_operation", ""), diagnostic_inputs=tuple(raw.get("diagnostic_inputs") or ()),
            result_selections=raw.get("result_selections", ()),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version, "directive_id": self.directive_id,
            "task_id": self.task_id, "action": self.action, "summary": self.summary,
            "reason_code": self.reason_code,
        }
        for field in ("target_step_id", "instruction", "question", "diagnostic_operation"):
            value = getattr(self, field)
            if value:
                result[field] = value
        if self.requested_slots:
            result["requested_slots"] = list(self.requested_slots)
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        if self.diagnostic_inputs:
            result["diagnostic_inputs"] = list(self.diagnostic_inputs)
        if self.result_selections:
            result["result_selections"] = [_thaw_json(row) for row in self.result_selections]
        return result


@dataclass(frozen=True)
class GenerateSeed(ImmutableStageContract):
    """Fresh corrective-Generate handoff; never a Recovery transcript carrier."""

    schema_version: int
    seed_id: str
    task_id: str
    step_id: str
    failure_id: str
    failure_packet_ref: str
    source_outcome_ref: str
    recovery_directive: RecoveryDirective
    evidence_refs: tuple[str, ...]
    recovery_archive_ref: str

    REQUIRED: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "seed_id", "task_id", "step_id", "failure_id",
        "failure_packet_ref", "source_outcome_ref", "recovery_directive",
        "evidence_refs", "recovery_archive_ref",
    })

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schema_version", _validate_schema(self.schema_version, type(self).__name__)
        )
        for field_name in (
            "seed_id", "task_id", "step_id", "failure_id",
            "failure_packet_ref", "source_outcome_ref", "recovery_archive_ref",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), f"GenerateSeed.{field_name}"),
            )
        directive = _contract_mapping(
            self.recovery_directive, RecoveryDirective, "GenerateSeed.recovery_directive"
        )
        if directive.task_id != self.task_id:
            raise StageContractError(
                "STAGE_SCOPE_MISMATCH", "GenerateSeed.recovery_directive.task_id"
            )
        if directive.target_step_id and directive.target_step_id != self.step_id:
            raise StageContractError(
                "STAGE_SCOPE_MISMATCH", "GenerateSeed.recovery_directive.target_step_id"
            )
        object.__setattr__(self, "recovery_directive", directive)
        evidence = _text_tuple(self.evidence_refs, "GenerateSeed.evidence_refs")
        if set(directive.evidence_refs) - set(evidence):
            raise StageContractError(
                "STAGE_SCOPE_MISMATCH", "GenerateSeed.recovery_directive.evidence_refs"
            )
        object.__setattr__(self, "evidence_refs", evidence)
        _json_round_trip(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GenerateSeed":
        raw = _validate_fields(
            value,
            contract_name=cls.__name__,
            required=cls.REQUIRED,
        )
        return cls(
            schema_version=raw["schema_version"],
            seed_id=raw["seed_id"],
            task_id=raw["task_id"],
            step_id=raw["step_id"],
            failure_id=raw["failure_id"],
            failure_packet_ref=raw["failure_packet_ref"],
            source_outcome_ref=raw["source_outcome_ref"],
            recovery_directive=RecoveryDirective.from_mapping(raw["recovery_directive"]),
            evidence_refs=tuple(raw["evidence_refs"]),
            recovery_archive_ref=raw["recovery_archive_ref"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "seed_id": self.seed_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "failure_id": self.failure_id,
            "failure_packet_ref": self.failure_packet_ref,
            "source_outcome_ref": self.source_outcome_ref,
            "recovery_directive": self.recovery_directive.to_dict(),
            "evidence_refs": list(self.evidence_refs),
            "recovery_archive_ref": self.recovery_archive_ref,
        }


__all__ = [
    "STAGE_CONTRACT_SCHEMA_VERSION",
    "StageContractError",
    "ImmutableStageContract",
    "freeze",
    "thaw",
    "TaskSpecRef",
    "SelectedSkillRef",
    "SelectedSkillBundle",
    "ConsultantSeed",
    "ConsultationHandoff",
    "PlannerSeed",
    "PlanSpecRef",
    "StepSeed",
    "FailurePacket",
    "RecoverySeed",
    "RecoveryDirective",
    "GenerateSeed",
]

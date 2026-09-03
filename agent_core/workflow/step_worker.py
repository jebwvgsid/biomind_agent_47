"""Replaceable ``StepRequest -> StepOutcome`` worker boundary.

This module deliberately contains no LangGraph node names in backend-facing
contracts.  Node selection is a BioMind-owned mapping performed only by
``route_step_worker_outcome``.
"""

from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, Sequence, cast

from agent_core.managed_artifacts import build_approved_visible_artifact_set
from agent_core.plan_bindings import project_accepted_plan_bindings
from agent_core.plan_tracker import canonical_task_spec_for_state


STEP_REQUEST_SCHEMA_VERSION = 1
STEP_OUTCOME_SCHEMA_VERSION = 1
STEP_WORKER_BACKEND_ENV = "BIOMIND_STEP_WORKER_BACKEND"

StepOutcomeType = Literal[
    "completion_claimed",
    "needs_replan",
    "blocked",
    "harness_error",
    "cancelled",
]

_ALLOWED_OUTCOME_TYPES = {
    "completion_claimed",
    "needs_replan",
    "blocked",
    "harness_error",
    "cancelled",
}
_ALLOWED_BACKENDS = {"legacy", "typed_stub"}
_ACTIVE_STEP_STATUSES = {
    "pending",
    "ready",
    "running",
    "verification_pending",
    "retry_pending",
    "repairing",
    "diagnosing",
}


class StepWorkerContractError(ValueError):
    """Stable fail-closed error at the typed worker boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "STEP_WORKER_CONTRACT_ERROR")
        self.detail = str(detail or "")
        super().__init__(
            self.code if not self.detail else f"{self.code}: {self.detail}"
        )

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StepWorkerContractError(
                "STEP_WORKER_JSON_INVALID", "non-finite numbers are not allowed"
            )
        return value
    raise StepWorkerContractError(
        "STEP_WORKER_JSON_INVALID", f"unsupported value type: {type(value).__name__}"
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _frozen_rows(values: Sequence[Mapping[str, Any]] | None) -> tuple[Mapping[str, Any], ...]:
    rows = tuple(_freeze_json(dict(item)) for item in (values or ()))
    # Validate the public serialized representation once at construction.
    json.dumps(_thaw_json(rows), ensure_ascii=False, sort_keys=True, allow_nan=False)
    return rows


def _required_text(value: Any, field: str, *, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StepWorkerContractError(code, f"{field} is required")
    return text


@dataclass(frozen=True)
class StepRequest:
    schema_version: int
    task_id: str
    plan_id: str
    plan_version: str
    step_id: str
    step_goal: str
    completion_contract: Mapping[str, Any]
    selected_skill_ids: tuple[str, ...]
    resolved_inputs: tuple[Mapping[str, Any], ...]
    accepted_bindings: tuple[Mapping[str, Any], ...]
    available_artifacts: tuple[Mapping[str, Any], ...]
    workspace: str
    attempt_ordinal: int

    def __post_init__(self) -> None:
        if int(self.schema_version) != STEP_REQUEST_SCHEMA_VERSION:
            raise StepWorkerContractError(
                "STEP_REQUEST_SCHEMA_UNSUPPORTED",
                f"schema_version={self.schema_version}",
            )
        for field in ("task_id", "plan_id", "plan_version", "step_id", "step_goal", "workspace"):
            object.__setattr__(
                self,
                field,
                _required_text(
                    getattr(self, field), field, code="STEP_REQUEST_SCOPE_INVALID"
                ),
            )
        try:
            ordinal = int(self.attempt_ordinal)
        except (TypeError, ValueError) as exc:
            raise StepWorkerContractError(
                "STEP_REQUEST_SCOPE_INVALID", "attempt_ordinal must be an integer"
            ) from exc
        if ordinal < 1:
            raise StepWorkerContractError(
                "STEP_REQUEST_SCOPE_INVALID", "attempt_ordinal must be positive"
            )
        object.__setattr__(self, "attempt_ordinal", ordinal)
        object.__setattr__(
            self,
            "selected_skill_ids",
            tuple(dict.fromkeys(str(item).strip() for item in self.selected_skill_ids if str(item).strip())),
        )
        object.__setattr__(self, "completion_contract", _freeze_json(dict(self.completion_contract)))
        object.__setattr__(self, "resolved_inputs", _frozen_rows(self.resolved_inputs))
        object.__setattr__(self, "accepted_bindings", _frozen_rows(self.accepted_bindings))
        object.__setattr__(self, "available_artifacts", _frozen_rows(self.available_artifacts))
        json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "step_id": self.step_id,
            "step_goal": self.step_goal,
            "completion_contract": _thaw_json(self.completion_contract),
            "selected_skill_ids": list(self.selected_skill_ids),
            "resolved_inputs": _thaw_json(self.resolved_inputs),
            "accepted_bindings": _thaw_json(self.accepted_bindings),
            "available_artifacts": _thaw_json(self.available_artifacts),
            "workspace": self.workspace,
            "attempt_ordinal": self.attempt_ordinal,
        }


@dataclass(frozen=True)
class StepOutcome:
    schema_version: int
    type: StepOutcomeType
    summary: str
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    binding_refs: tuple[str, ...] = ()
    blocker: Mapping[str, Any] | None = None
    worker_session_ref: str | None = None

    def __post_init__(self) -> None:
        if int(self.schema_version) != STEP_OUTCOME_SCHEMA_VERSION:
            raise StepWorkerContractError(
                "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                f"unsupported schema_version={self.schema_version}",
            )
        outcome_type = str(self.type or "").strip()
        if outcome_type not in _ALLOWED_OUTCOME_TYPES:
            raise StepWorkerContractError(
                "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                f"unknown outcome type={outcome_type or '<empty>'}",
            )
        object.__setattr__(self, "type", cast(StepOutcomeType, outcome_type))
        object.__setattr__(
            self,
            "summary",
            _required_text(
                self.summary,
                "summary",
                code="STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
            ),
        )
        for field in ("evidence_refs", "artifact_refs", "binding_refs"):
            object.__setattr__(
                self,
                field,
                tuple(str(item).strip() for item in getattr(self, field) if str(item).strip()),
            )
        blocker = None if self.blocker is None else _freeze_json(dict(self.blocker))
        if outcome_type == "blocked" and not blocker:
            raise StepWorkerContractError(
                "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                "blocked outcome requires a structured blocker",
            )
        object.__setattr__(self, "blocker", blocker)
        session_ref = str(self.worker_session_ref or "").strip() or None
        object.__setattr__(self, "worker_session_ref", session_ref)
        json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StepOutcome":
        allowed = {
            "schema_version",
            "type",
            "summary",
            "evidence_refs",
            "artifact_refs",
            "binding_refs",
            "blocker",
            "worker_session_ref",
        }
        required = {"schema_version", "type", "summary"}
        missing = sorted(required - set(value))
        if missing:
            raise StepWorkerContractError(
                "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                "missing required field(s): " + ", ".join(missing),
            )
        extra = sorted(set(value) - allowed)
        if extra:
            raise StepWorkerContractError(
                "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                "unknown field(s): " + ", ".join(extra),
            )
        blocker = value.get("blocker")
        if blocker is not None and not isinstance(blocker, Mapping):
            raise StepWorkerContractError(
                "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
                "blocker must be an object or null",
            )
        return cls(
            schema_version=value.get("schema_version"),
            type=cast(StepOutcomeType, value.get("type")),
            summary=value.get("summary"),
            evidence_refs=tuple(value.get("evidence_refs") or ()),
            artifact_refs=tuple(value.get("artifact_refs") or ()),
            binding_refs=tuple(value.get("binding_refs") or ()),
            blocker=blocker,
            worker_session_ref=value.get("worker_session_ref"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "type": self.type,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
            "binding_refs": list(self.binding_refs),
            "blocker": _thaw_json(self.blocker) if self.blocker is not None else None,
            "worker_session_ref": self.worker_session_ref,
        }


class StepWorkerBackend(Protocol):
    async def run_step(self, request: StepRequest) -> StepOutcome:
        """Run exactly one approved step without receiving mutable AgentState."""


class TypedStubStepWorkerBackend:
    """Offline-only backend used to exercise the replaceable typed boundary."""

    def __init__(self, outcome: StepOutcome | None = None) -> None:
        self.outcome = outcome or StepOutcome(
            schema_version=STEP_OUTCOME_SCHEMA_VERSION,
            type="harness_error",
            summary="typed_stub has no configured outcome",
            blocker={"code": "TYPED_STUB_OUTCOME_NOT_CONFIGURED"},
        )

    async def run_step(self, request: StepRequest) -> StepOutcome:
        if not isinstance(request, StepRequest):
            raise StepWorkerContractError(
                "STEP_REQUEST_SCOPE_INVALID", "backend requires StepRequest"
            )
        return self.outcome


def resolve_step_worker_backend_name(
    configured: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    name = str(
        configured if configured is not None else source.get(STEP_WORKER_BACKEND_ENV, "legacy")
    ).strip().lower()
    name = name or "legacy"
    if name not in _ALLOWED_BACKENDS:
        raise StepWorkerContractError(
            "STEP_WORKER_BACKEND_UNSUPPORTED", f"backend={name}"
        )
    return name


def get_step_worker_backend(
    backend_name: str,
    *,
    override: StepWorkerBackend | None = None,
) -> StepWorkerBackend | None:
    name = resolve_step_worker_backend_name(backend_name)
    if name == "legacy":
        if override is not None:
            raise StepWorkerContractError(
                "STEP_WORKER_BACKEND_INVALID",
                "legacy mode cannot install a typed backend",
            )
        return None
    return override or TypedStubStepWorkerBackend()


def _plan_step_source(
    plan_state: Mapping[str, Any],
    approved_plan_snapshot: Mapping[str, Any] | None,
) -> tuple[Sequence[Mapping[str, Any]], str]:
    snapshot = dict(approved_plan_snapshot or {})
    if snapshot.get("steps"):
        return tuple(item for item in snapshot["steps"] if isinstance(item, Mapping)), "approved_plan_snapshot"
    current_plan = dict(plan_state.get("current_plan") or {})
    plan_spec = dict(current_plan.get("plan_spec") or {})
    if plan_spec.get("steps"):
        return tuple(item for item in plan_spec["steps"] if isinstance(item, Mapping)), "current_plan.plan_spec"
    return tuple(item for item in plan_state.get("steps", ()) if isinstance(item, Mapping)), "plan_state"


def _artifact_projection(
    visible: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "artifact_id": str(artifact_id),
            "direction": str(item.get("direction") or ""),
            "source_authority": str(item.get("source_authority") or ""),
            "runtime_available": bool(item.get("runtime_available")),
            "row": deepcopy(dict(item.get("row") or {})),
        }
        for artifact_id, item in sorted(visible.items())
    )


def build_step_request(
    plan_state: Mapping[str, Any],
    *,
    workspace: str,
    approved_plan_snapshot: Mapping[str, Any] | None = None,
    graph_scope: Mapping[str, Any] | None = None,
    attempt_ordinal: int = 1,
) -> StepRequest:
    """Build one immutable request from approved durable PlanState.

    The optional graph scope is used only to reject stale graph projections; it
    never contributes identity or semantic values to the request.
    """

    durable = dict(plan_state or {})
    task_id = _required_text(
        durable.get("task_id"), "task_id", code="STEP_REQUEST_SCOPE_INVALID"
    )
    if str(durable.get("plan_status") or "") != "approved" or str(
        durable.get("approval_status") or ""
    ) != "approved":
        raise StepWorkerContractError(
            "STEP_REQUEST_SCOPE_INVALID", "current plan is not approved"
        )
    plan_id = _required_text(
        durable.get("plan_id"), "plan_id", code="STEP_REQUEST_SCOPE_INVALID"
    )
    plan_version = _required_text(
        durable.get("plan_version"), "plan_version", code="STEP_REQUEST_SCOPE_INVALID"
    )
    step_id = _required_text(
        durable.get("current_step_id"),
        "current_step_id",
        code="STEP_REQUEST_SCOPE_INVALID",
    )

    runtime_matches = [
        dict(item)
        for item in durable.get("steps", ())
        if isinstance(item, Mapping) and str(item.get("step_id") or "") == step_id
    ]
    if len(runtime_matches) != 1 or str(runtime_matches[0].get("status") or "").lower() not in _ACTIVE_STEP_STATUSES:
        raise StepWorkerContractError(
            "STEP_REQUEST_SCOPE_INVALID",
            f"current step is missing, ambiguous, or inactive: {step_id}",
        )

    source_steps, source_authority = _plan_step_source(durable, approved_plan_snapshot)
    approved_matches = [dict(item) for item in source_steps if str(item.get("step_id") or "") == step_id]
    if len(approved_matches) != 1:
        raise StepWorkerContractError(
            "STEP_REQUEST_SCOPE_INVALID",
            f"current step is not unique in {source_authority}: {step_id}",
        )
    step = approved_matches[0]

    scope = dict(graph_scope or {})
    for field, expected in (
        ("task_id", task_id),
        ("plan_id", plan_id),
        ("plan_version", plan_version),
        ("current_step_id", step_id),
    ):
        observed = str(scope.get(field) or "")
        if observed and observed != expected:
            raise StepWorkerContractError(
                "STEP_REQUEST_SCOPE_INVALID",
                f"stale graph {field}: expected={expected}, observed={observed}",
            )

    snapshot = dict(approved_plan_snapshot or {})
    snapshot_revision = snapshot.get("plan_revision")
    if snapshot_revision not in (None, "") and int(snapshot_revision) != int(
        durable.get("plan_revision") or 0
    ):
        raise StepWorkerContractError(
            "STEP_REQUEST_SCOPE_INVALID", "approved plan revision mismatch"
        )

    task_spec = canonical_task_spec_for_state(durable)
    visible = build_approved_visible_artifact_set(
        task_spec,
        plan_state=durable,
        approved_plan_snapshot=snapshot,
        current_step_id=step_id,
    )
    available_artifacts = _artifact_projection(visible)
    visible_by_alias: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for artifact_id, item in visible.items():
        for alias in (artifact_id, *(item.get("aliases") or ())):
            if str(alias or ""):
                visible_by_alias[str(alias)] = (str(artifact_id), item)

    resolved_inputs: list[dict[str, Any]] = []
    for raw in step.get("inputs", ()) or ():
        if isinstance(raw, Mapping):
            ref = str(
                raw.get("artifact_id")
                or raw.get("input_id")
                or raw.get("semantic_handle")
                or raw.get("path")
                or ""
            )
        else:
            ref = str(raw or "")
        if not ref:
            raise StepWorkerContractError(
                "STEP_REQUEST_SCOPE_INVALID", "current step contains an empty input reference"
            )
        match = visible_by_alias.get(ref)
        if match is None:
            resolved_inputs.append({
                "ref": ref,
                "resolution_status": "approved_step_literal",
                "source_authority": source_authority,
            })
            continue
        artifact_id, item = match
        resolved_inputs.append({
            "ref": ref,
            "artifact_id": artifact_id,
            "resolution_status": "resolved_artifact",
            "source_authority": str(item.get("source_authority") or source_authority),
            "runtime_available": bool(item.get("runtime_available")),
            "row": deepcopy(dict(item.get("row") or {})),
        })

    completion_contract = {
        "source_authority": source_authority,
        "success_criteria": deepcopy(list(step.get("success_criteria") or ())),
        "declared_outputs": deepcopy(list(step.get("outputs") or ())),
        "artifact_contracts": deepcopy(list(step.get("artifacts") or ())),
    }
    step_goal = str(step.get("title") or step.get("purpose") or "").strip()
    selected_skill_ids = tuple(
        str(item)
        for item in (step.get("skill_ids") or durable.get("selected_skill_ids") or ())
        if str(item).strip()
    )
    return StepRequest(
        schema_version=STEP_REQUEST_SCHEMA_VERSION,
        task_id=task_id,
        plan_id=plan_id,
        plan_version=plan_version,
        step_id=step_id,
        step_goal=step_goal,
        completion_contract=completion_contract,
        selected_skill_ids=selected_skill_ids,
        resolved_inputs=tuple(resolved_inputs),
        accepted_bindings=tuple(project_accepted_plan_bindings(durable)),
        available_artifacts=available_artifacts,
        workspace=workspace,
        attempt_ordinal=attempt_ordinal,
    )


async def invoke_step_worker(
    backend: StepWorkerBackend,
    request: StepRequest,
) -> StepOutcome:
    if not isinstance(request, StepRequest):
        raise StepWorkerContractError(
            "STEP_REQUEST_SCOPE_INVALID", "backend invocation requires StepRequest"
        )
    raw = await backend.run_step(request)
    if isinstance(raw, StepOutcome):
        return raw
    if isinstance(raw, Mapping):
        return StepOutcome.from_mapping(raw)
    raise StepWorkerContractError(
        "STEP_WORKER_OUTCOME_PROTOCOL_ERROR",
        f"backend returned {type(raw).__name__}, expected StepOutcome",
    )


def fail_closed_step_outcome(error: StepWorkerContractError) -> StepOutcome:
    return StepOutcome(
        schema_version=STEP_OUTCOME_SCHEMA_VERSION,
        type="harness_error",
        summary=error.code,
        blocker={"code": error.code, "detail": error.detail},
    )


def route_step_worker_outcome(state: Mapping[str, Any]) -> str:
    """Map only the typed outcome to a BioMind-owned semantic destination."""

    raw = state.get("step_worker_outcome")
    try:
        outcome = raw if isinstance(raw, StepOutcome) else StepOutcome.from_mapping(raw or {})
    except (StepWorkerContractError, TypeError, ValueError):
        return "finish"
    return {
        "completion_claimed": "supervisor",
        "needs_replan": "planner",
        "blocked": "finish",
        "harness_error": "finish",
        "cancelled": "finish",
    }[outcome.type]


def plan_review_destination(legacy_destination: str, backend_name: str) -> str:
    """Select the opt-in entry path without changing legacy decisions."""

    destination = str(legacy_destination or "generate")
    if destination == "generate" and resolve_step_worker_backend_name(backend_name) != "legacy":
        return "step_worker"
    return destination

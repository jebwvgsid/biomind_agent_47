"""Phase-local Recovery agent contracts and provider-safe projections.

The stable System policy is deliberately task-independent.  One immutable
``RecoverySeed`` is projected once per invocation into the minimum semantic
facts needed for a first diagnosis; archives, Skill bodies, logs, and earlier
agent transcripts remain behind typed inspection references.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping, Sequence

from agent_core.plan_tracker import canonical_task_spec_for_state
from agent_core.workflow.stage_contracts import RecoverySeed, freeze, thaw


RECOVERY_SYSTEM_PROMPT_VERSION = "recovery-agent-system-v1"
RECOVERY_SEED_NORMAL_TARGET_CHARS = 16_000
RECOVERY_SEED_HARD_LIMIT_CHARS = 32_000

# Keep this a literal, with no formatting or interpolation.  Under a fixed
# prompt version, tool schema, model, provider, and directive schema these
# bytes are identical for every task, plan, step, and failure.
RECOVERY_SYSTEM_PROMPT = """You are the BioMind Recovery Agent. Diagnose one causal failure from an immutable RecoverySeed and bounded, read-only inspection tools.

Objective:
- Identify the shortest evidence-backed diagnosis and the semantic correction required.
- Never execute a repair, emit executable code, mutate TaskSpec or PlanSpec, or choose a graph route.

Failure classes:
REAL_EXECUTION_FAILURE, MISSING_REQUIRED_INPUT, TOOL_OR_ENV_FAILURE, SCRIPT_BUG, PLAN_ERROR, CONTRACT_DRIFT, VALIDATOR_FALSE_POSITIVE, OPTIONAL_CHECK_FAILURE, SCIENTIFIC_WARNING, SUCCESS_WITH_WARNING, AMBIGUOUS.

Failure owners:
PLAN, GENERATED_ACTION, SCRIPT, RUNTIME, DEPENDENCY, ARTIFACT_CONTRACT, RENDERER, INTERNAL_PROTOCOL, VERIFICATION, INPUT_DATA, DIAGNOSTIC, CONSULT, UNKNOWN.

Evidence rules:
- Treat the causal FailurePacket as authoritative for failure identity and source.
- Inspect only opaque refs declared by the frozen Seed and local tool manifest.
- State evidence_sufficient only when cited machine facts support the diagnosis and correction.
- If evidence is insufficient, inspect a narrower declared ref or list the unresolved evidence.
- Do not infer missing runtime facts, scan unrelated history, or expose hidden reasoning.

Tool rules:
- Available tools are failure.inspect, step.inspect, artifact.inspect, artifact.read, skill.headings, skill.read, skill.search, skill.resource, evidence.read, and evidence.glob.
- Tools are read-only. Never pass a filesystem path where an opaque ref is required.
- Only already-selected Skills may be inspected. If a new Skill is required, choose reconsult.
- Keep requests narrow and do not repeat an identical request after it has been rejected twice.

Safety rules:
- Do not provide shell, Python, R, patches, replacement commands, or other executable payloads.
- Do not emit route, node, goto, next_step, next_route, target_route, dispatch_route, or controller fields.
- Do not copy large tool observations into the final result.

Terminal output:
Return exactly one strict RecoveryDirective JSON object and no surrounding prose. Required fields are schema_version, directive_id, task_id, action, summary, and reason_code. Optional fields are target_step_id, instruction, question, requested_slots, and evidence_refs. Allowed actions are retry_step, revise_plan, reconsult, revalidate, ask_user, and blocked. retry_step and revalidate require target_step_id. retry_step, revise_plan, and reconsult require instruction. ask_user requires question and requested_slots. All fields are semantic and route-blind."""


class RecoveryAgentError(ValueError):
    """Stable fail-closed error for Recovery provider-context construction."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "RECOVERY_AGENT_ERROR")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def recovery_seed_digest(seed: RecoverySeed | Mapping[str, Any]) -> str:
    parsed = seed if isinstance(seed, RecoverySeed) else RecoverySeed.from_mapping(seed)
    return "sha256:" + hashlib.sha256(
        _canonical(parsed.to_dict()).encode("utf-8")
    ).hexdigest()


def _rows(value: Any, fields: Sequence[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in value or ():
        if not isinstance(item, Mapping):
            continue
        row = {
            field: deepcopy(item[field])
            for field in fields
            if item.get(field) not in (None, "", [], {})
        }
        if row:
            result.append(row)
    return result


def _hard_constraints(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = (
        "requirement_id", "semantic_key", "category", "operator", "value",
        "text", "requirement_level", "strength", "required", "scope",
    )
    result: list[dict[str, Any]] = []
    candidates = [
        *list(task.get("requirements") or ()),
        *list(task.get("semantic_constraints") or ()),
    ]
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "active").lower()
        if status in {"superseded", "withdrawn", "inactive"}:
            continue
        level = str(item.get("requirement_level") or "").lower()
        strength = str(item.get("strength") or "").lower()
        if not (
            item.get("required") is True
            or level in {"required", "conditional"}
            or strength in {"hard", "required", "conditional"}
        ):
            continue
        row = {
            field: deepcopy(item[field])
            for field in fields
            if item.get(field) not in (None, "", [], {})
        }
        if row:
            result.append(row)
    for field, polarity in (("must_do", "required"), ("must_not_do", "forbidden")):
        for item in task.get(field) or ():
            text = str(item.get("text") if isinstance(item, Mapping) else item).strip()
            if text:
                result.append({"polarity": polarity, "text": text})
    return result


def _task_semantic_slice(durable: Mapping[str, Any]) -> dict[str, Any]:
    task = canonical_task_spec_for_state(durable)
    result: dict[str, Any] = {"goal": str(task.get("goal") or "").strip()}
    constraints = _hard_constraints(task)
    if constraints:
        result["hard_constraints"] = constraints
    inputs = _rows(
        task.get("inputs"),
        ("input_id", "role", "path", "format", "required", "availability"),
    )
    outputs = _rows(
        task.get("output_contracts"),
        ("output_id", "role", "path", "format", "required", "path_policy", "root_ref"),
    )
    if inputs:
        result["input_roles"] = inputs
    if outputs:
        result["output_roles"] = outputs
    return result


def _selected_skill_manifest(seed: RecoverySeed, durable: Mapping[str, Any]) -> dict[str, Any]:
    access = dict(
        (durable.get("task_state") or {}).get("skill_access_state")
        or durable.get("skill_access_state")
        or {}
    )
    resources = [
        dict(item)
        for item in (access.get("resource_snapshots") or {}).values()
        if isinstance(item, Mapping)
    ]
    skills: list[dict[str, Any]] = []
    for skill in seed.selected_skill_bundle.skills:
        row: dict[str, Any] = {
            "skill_id": skill.skill_id,
            "snapshot_id": skill.snapshot_id,
            "scope": skill.scope,
        }
        if skill.target_step_id:
            row["target_step_id"] = skill.target_step_id
        available = [
            {
                "resource_snapshot_id": str(resource.get("resource_snapshot_id") or ""),
                "resource_path": str(resource.get("resource_path") or ""),
            }
            for resource in resources
            if str(resource.get("skill_id") or "") == skill.skill_id
            and str(resource.get("snapshot_id") or "") == skill.snapshot_id
            and resource.get("resource_path")
        ]
        if available:
            row["available_resources"] = available
        skills.append(row)
    return {
        "bundle_id": seed.selected_skill_bundle.bundle_id,
        "manifest_only": True,
        "skills": skills,
    }


def _failure_projection(seed: RecoverySeed) -> dict[str, Any]:
    packet = seed.failure_packet
    result: dict[str, Any] = {
        "failure_id": packet.failure_id,
        "source": packet.source,
        "source_outcome_ref": packet.source_outcome_ref,
        "failure_type": packet.failure_type,
        "failure_owner": packet.failure_owner,
        "error_code": packet.error_code,
        "summary": packet.summary,
        "retryable": packet.retryable,
    }
    if packet.action_ref:
        result["failed_action_ref"] = packet.action_ref
    if packet.attempt_ref:
        result["failed_attempt_ref"] = packet.attempt_ref
    if packet.diagnostic_facts:
        result["diagnostic_facts"] = _rows(
            packet.diagnostic_facts,
            ("fact_id", "kind", "summary", "evidence_ref", "confidence"),
        )
    if packet.evidence_refs:
        result["evidence_refs"] = list(packet.evidence_refs)
    if packet.inspection_refs:
        result["inspection_refs"] = list(packet.inspection_refs)
    return result


def _inspection_manifest(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = ("ref_id", "kind", "backend", "digest", "size_hint", "scope")
    result = _rows(value, fields)
    # Raw ``sha256:`` values are internal cache/concurrency metadata in the
    # shared provider guard.  A typed ref digest stays useful as bare hex while
    # avoiding accidental promotion of a durable CAS token into model control.
    for row in result:
        digest = str(row.get("digest") or "")
        if digest.startswith("sha256:"):
            row["digest"] = digest.removeprefix("sha256:")
    return result


def project_recovery_seed_for_provider(
    seed: RecoverySeed | Mapping[str, Any],
    durable: Mapping[str, Any],
    *,
    inspection_manifest: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build and capacity-check the one frozen provider-visible RecoverySeed."""

    parsed = seed if isinstance(seed, RecoverySeed) else RecoverySeed.from_mapping(seed)
    current_step = {
        key: deepcopy(parsed.current_step[key])
        for key in (
            "step_id", "title", "goal", "purpose", "action", "inputs",
            "outputs", "success_criteria", "risk_level",
        )
        if parsed.current_step.get(key) not in (None, "", [], {})
    }
    dependencies = _rows(
        parsed.dependency_outcomes,
        ("step_id", "status", "summary", "outcome_ref", "evidence_refs", "artifact_refs", "binding_refs"),
    )
    provider_seed: dict[str, Any] = {
        "schema_version": 1,
        "task_semantic_slice": _task_semantic_slice(durable),
        "current_step": current_step,
        "dependencies": dependencies,
        "selected_skill_manifest": _selected_skill_manifest(parsed, durable),
        "failure_packet": _failure_projection(parsed),
        "current_failure_lineage": {
            "failure_id": parsed.failure_packet.failure_id,
            "source_outcome_ref": parsed.failure_packet.source_outcome_ref,
        },
    }
    if parsed.prior_directive_refs:
        provider_seed["same_step_prior"] = {
            "directive_ref": parsed.prior_directive_refs[-1]
        }
    refs = _inspection_manifest(inspection_manifest)
    if refs:
        provider_seed["inspection_manifest"] = refs

    # The stage freezer rejects conversational payloads, full Skill bodies,
    # raw logs, executable streams, and recursive route/controller fields.
    frozen = thaw(freeze(provider_seed))
    encoded = _canonical(frozen)
    if len(encoded) > RECOVERY_SEED_HARD_LIMIT_CHARS:
        raise RecoveryAgentError(
            "RECOVERY_SEED_CAPACITY_EXCEEDED",
            f"chars={len(encoded)} limit={RECOVERY_SEED_HARD_LIMIT_CHARS}",
        )
    return frozen


def recovery_seed_capacity(seed: Mapping[str, Any]) -> dict[str, Any]:
    chars = len(_canonical(dict(seed or {})))
    return {
        "chars": chars,
        "normal_target_chars": RECOVERY_SEED_NORMAL_TARGET_CHARS,
        "hard_limit_chars": RECOVERY_SEED_HARD_LIMIT_CHARS,
        "within_normal_target": chars < RECOVERY_SEED_NORMAL_TARGET_CHARS,
        "within_hard_limit": chars <= RECOVERY_SEED_HARD_LIMIT_CHARS,
    }


__all__ = [
    "RECOVERY_SEED_HARD_LIMIT_CHARS",
    "RECOVERY_SEED_NORMAL_TARGET_CHARS",
    "RECOVERY_SYSTEM_PROMPT",
    "RECOVERY_SYSTEM_PROMPT_VERSION",
    "RecoveryAgentError",
    "project_recovery_seed_for_provider",
    "recovery_seed_capacity",
    "recovery_seed_digest",
]

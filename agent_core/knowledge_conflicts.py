"""Generic knowledge-claim conflict detection and auditable plan revision.

This module deliberately knows nothing about a particular bioinformatics tool.
It turns user constraints, plan assumptions, runtime observations and artifact
checks into claims, compares claims with deterministic rules, and records
revisions without silently changing the scientific goal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agent_core.context_lineage import redact_payload


def _safe_json(value: Any, *, indent: Optional[int] = None, default: Any = None) -> str:
    redacted, _ = redact_payload(value)
    return json.dumps(redacted, ensure_ascii=False, indent=indent, default=default)

CLAIM_SOURCES = {
    "system_policy",
    "user_instruction",
    "task_contract",
    "benchmark_rule",
    "plan",
    "plan_assumption",
    "skill_document",
    "official_tool_documentation",
    "tool_help",
    "runtime_argv",
    "runtime_output",
    "environment_probe",
    "artifact_validation",
    "upstream_stage",
    "recovery_proposal",
    "llm_inference",
}

CLAIM_STRENGTHS = {
    "hard_constraint",
    "required",
    "preferred",
    "assumption",
    "observation",
    "inference",
}

CLAIM_SCOPES = {"task", "workflow", "stage", "command", "artifact", "environment"}
CLAIM_STATUSES = {"active", "superseded", "invalidated", "unverified"}

CONFLICT_TYPES = {
    "USER_PLAN_CONFLICT",
    "USER_RECOVERY_CONFLICT",
    "PLAN_RUNTIME_CONFLICT",
    "PLAN_ENVIRONMENT_CONFLICT",
    "PLAN_ARTIFACT_CONFLICT",
    "SKILL_RUNTIME_CONFLICT",
    "TOOL_BEHAVIOR_CONFLICT",
    "STAGE_CONTRACT_CONFLICT",
    "SCIENTIFIC_METHOD_CONFLICT",
    "STALE_PLAN",
    "RECOVERY_STRATEGY_CONFLICT",
}

RESOLUTION_MODES = {
    "continue_unchanged",
    "require_probe",
    "local_replan",
    "stage_replan",
    "downstream_replan",
    "global_replan",
    "accept_exception",
    "request_user_decision",
    "block",
}

REVISION_SCOPES = {"single_command", "single_step", "stage", "downstream", "workflow"}

# Higher values are authoritative when two claims cannot both be true.
SOURCE_PRIORITY = {
    "system_policy": 100,
    "user_instruction": 95,
    "task_contract": 92,
    "benchmark_rule": 90,
    "artifact_validation": 86,
    "upstream_stage": 84,
    "environment_probe": 82,
    "runtime_argv": 80,
    "runtime_output": 78,
    "official_tool_documentation": 74,
    "tool_help": 72,
    "skill_document": 68,
    "plan": 55,
    "plan_assumption": 48,
    "recovery_proposal": 42,
    "llm_inference": 10,
}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _normalize(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[`\"']", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(_normalize(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()[:16]}"


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


@dataclass
class KnowledgeClaim:
    claim_id: str = ""
    proposition: str = ""
    normalized_proposition: str = ""
    source_type: str = "llm_inference"
    source_reference: Optional[str] = None
    strength: str = "inference"
    scope: str = "task"
    scope_id: Optional[str] = None
    confidence: float = 0.0
    reliability: float = 0.0
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    tool_name: Optional[str] = None
    tool_version: Optional[str] = None
    environment_name: Optional[str] = None
    evidence_ids: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    supersedes_claim_id: Optional[str] = None
    status: str = "active"
    attributes: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.proposition = _clip(self.proposition, 8000).strip()
        self.normalized_proposition = _normalize(self.normalized_proposition or self.proposition)
        if self.source_type not in CLAIM_SOURCES:
            self.source_type = "llm_inference"
        if self.strength not in CLAIM_STRENGTHS:
            self.strength = "inference"
        if self.scope not in CLAIM_SCOPES:
            self.scope = "task"
        if self.status not in CLAIM_STATUSES:
            self.status = "active"
        self.confidence = _safe_float(self.confidence)
        self.reliability = _safe_float(self.reliability)
        self.evidence_ids = [str(item) for item in (self.evidence_ids or []) if str(item).strip()]
        self.attributes = dict(self.attributes or {}) if isinstance(self.attributes, dict) else {}
        if not self.created_at:
            self.created_at = _now()
        if not self.claim_id:
            self.claim_id = _stable_id(
                "claim", self.source_type, self.strength, self.scope, self.scope_id,
                self.normalized_proposition, self.tool_name, self.tool_version, self.environment_name,
            )

    @classmethod
    def from_mapping(cls, value: Any, **defaults: Any) -> "KnowledgeClaim":
        if isinstance(value, cls):
            return value
        data = dict(defaults)
        if isinstance(value, dict):
            data.update({key: item for key, item in value.items() if key in cls.__dataclass_fields__})
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def priority(self) -> int:
        return SOURCE_PRIORITY.get(self.source_type, 0)

    @property
    def hard(self) -> bool:
        return self.strength in {"hard_constraint", "required"}

    @property
    def hard_constraint(self) -> bool:
        """Whether changing this claim would violate a non-negotiable rule."""
        return self.strength == "hard_constraint" or self.source_type == "system_policy"


class ClaimRegistry:
    """Persistent de-duplicated registry of claims participating in decisions."""

    def __init__(self, claims: Optional[Iterable[Any]] = None) -> None:
        self._claims: Dict[str, KnowledgeClaim] = {}
        self._by_fingerprint: Dict[str, str] = {}
        for claim in claims or []:
            self.register(claim)

    @staticmethod
    def fingerprint(claim: KnowledgeClaim) -> str:
        semantic_key = str(claim.attributes.get("semantic_key") or claim.attributes.get("fact_key") or "").strip().lower()
        if semantic_key:
            return _stable_id(
                "claimfp", claim.source_type, claim.strength, claim.scope, claim.scope_id,
                semantic_key, claim.attributes.get("operator", "eq"),
                _canonical_semantic_value(claim.attributes.get("value", claim.proposition), semantic_key),
                claim.tool_name, claim.tool_version, claim.environment_name,
            )
        return _stable_id(
            "claimfp", claim.source_type, claim.strength, claim.scope, claim.scope_id,
            claim.normalized_proposition, claim.tool_name, claim.tool_version, claim.environment_name,
        )

    def register(self, claim: Any = None, *, deduplicate: bool = True, **kwargs: Any) -> KnowledgeClaim:
        if claim is None or kwargs:
            data = dict(kwargs)
            if claim is not None:
                data["proposition"] = claim
            claim = KnowledgeClaim(**data)
        item = KnowledgeClaim.from_mapping(claim)
        fingerprint = self.fingerprint(item)
        if deduplicate and fingerprint in self._by_fingerprint:
            existing = self._claims[self._by_fingerprint[fingerprint]]
            existing.evidence_ids = sorted(set(existing.evidence_ids + item.evidence_ids))
            existing.confidence = max(existing.confidence, item.confidence)
            existing.reliability = max(existing.reliability, item.reliability)
            if item.attributes:
                existing.attributes.update(item.attributes)
            return existing
        if not deduplicate and item.claim_id in self._claims:
            item.claim_id = f"{item.claim_id}_{len(self._claims) + 1}"
        self._claims[item.claim_id] = item
        self._by_fingerprint[fingerprint] = item.claim_id
        if item.supersedes_claim_id:
            self.supersede(item.supersedes_claim_id, item.claim_id)
        return item

    def register_many(self, claims: Iterable[Any]) -> List[KnowledgeClaim]:
        return [self.register(claim) for claim in claims]

    register_claim = register

    def register_proposition(self, proposition: str, source_type: str, *, strength: str = "observation", **kwargs: Any) -> KnowledgeClaim:
        return self.register(KnowledgeClaim(proposition=proposition, source_type=source_type, strength=strength, **kwargs))

    def register_user_constraint(self, proposition: str, **kwargs: Any) -> KnowledgeClaim:
        return self.register_proposition(proposition, "user_instruction", strength="hard_constraint", **kwargs)

    def register_plan_assumption(self, proposition: str, **kwargs: Any) -> KnowledgeClaim:
        return self.register_proposition(proposition, "plan_assumption", strength="assumption", **kwargs)

    def register_runtime_fact(self, proposition: str, **kwargs: Any) -> KnowledgeClaim:
        return self.register_observation(proposition, "runtime_output", strength="observation", **kwargs)

    def register_artifact_fact(self, proposition: str, **kwargs: Any) -> KnowledgeClaim:
        return self.register_observation(proposition, "artifact_validation", strength="observation", scope="artifact", **kwargs)

    def register_observation(
        self,
        proposition: str,
        source_type: str,
        *,
        strength: str = "observation",
        **kwargs: Any,
    ) -> KnowledgeClaim:
        """Register the latest fact without retaining superseded facts as active.

        A claim registry is an audit log, so old observations remain serialized,
        but a newer observation with the same source, scope, and ``fact_key``
        supersedes the prior value. This prevents a corrected probe from being
        permanently contradicted by an obsolete runtime observation.
        """
        attributes = dict(kwargs.pop("attributes", {}) or {})
        fact_key = str(attributes.get("fact_key") or "").strip()
        scope_id = kwargs.get("scope_id")
        value = _normalize(attributes.get("value", proposition))
        prior_ids: List[str] = []
        if fact_key:
            for prior in self.active_claims():
                if (
                    prior.source_type == source_type
                    and prior.scope_id == scope_id
                    and str(prior.attributes.get("fact_key") or "").strip() == fact_key
                    and _normalize(prior.attributes.get("value", prior.proposition)) != value
                ):
                    prior_ids.append(prior.claim_id)
        item = KnowledgeClaim(
            proposition=proposition,
            source_type=source_type,
            strength=strength,
            attributes=attributes,
            **kwargs,
        )
        registered = self.register(item)
        for prior_id in prior_ids:
            self.supersede(prior_id, registered.claim_id)
        return registered

    def get(self, claim_id: str) -> Optional[KnowledgeClaim]:
        return self._claims.get(str(claim_id))

    def all(self) -> List[KnowledgeClaim]:
        return list(self._claims.values())

    def active_claims(self) -> List[KnowledgeClaim]:
        return [claim for claim in self._claims.values() if claim.active]

    def invalidate(self, claim_id: str, *, reason: str = "") -> Optional[KnowledgeClaim]:
        claim = self.get(claim_id)
        if claim:
            claim.status = "invalidated"
            if reason:
                claim.attributes["invalidated_reason"] = _clip(reason, 1000)
        return claim

    def supersede(self, old_claim_id: str, new_claim_id: str) -> None:
        old = self.get(old_claim_id)
        if old and old.status == "active":
            old.status = "superseded"
            old.attributes["superseded_by"] = str(new_claim_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"claims": [claim.to_dict() for claim in self._claims.values()]}

    @classmethod
    def from_mapping(cls, value: Any) -> "ClaimRegistry":
        if isinstance(value, cls):
            return value
        claims = value.get("claims", []) if isinstance(value, dict) else value
        return cls(claims if isinstance(claims, list) else [])

    @classmethod
    def load(cls, root: str | Path) -> "ClaimRegistry":
        path = Path(root) / "conflicts" / "claims.jsonl"
        claims: List[Dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    claims.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(claims)


@dataclass
class KnowledgeConflict:
    conflict_id: str = ""
    conflict_type: str = "PLAN_RUNTIME_CONFLICT"
    left_claim_id: str = ""
    right_claim_id: str = ""
    description: str = ""
    severity: str = "warning"
    status: str = "detected"
    evidence_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0
    affects_step_ids: List[str] = field(default_factory=list)
    affects_artifact_ids: List[str] = field(default_factory=list)
    affects_downstream_step_ids: List[str] = field(default_factory=list)
    user_constraint_involved: bool = False
    hard_constraint_involved: bool = False
    safety_constraint_involved: bool = False
    candidate_resolutions: List[str] = field(default_factory=list)
    required_probe_ids: List[str] = field(default_factory=list)
    detected_at_attempt: int = 0
    resolved_at_attempt: Optional[int] = None
    resolution_id: Optional[str] = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.conflict_type not in CONFLICT_TYPES:
            self.conflict_type = "PLAN_RUNTIME_CONFLICT"
        if self.severity not in {"info", "warning", "blocking", "critical"}:
            self.severity = "warning"
        if self.status not in {"detected", "needs_probe", "resolved", "accepted_exception", "blocked", "superseded"}:
            self.status = "detected"
        self.confidence = _safe_float(self.confidence)
        self.evidence_ids = sorted(set(str(item) for item in self.evidence_ids if str(item).strip()))
        self.affects_step_ids = sorted(set(str(item) for item in self.affects_step_ids if str(item).strip()))
        self.affects_artifact_ids = sorted(set(str(item) for item in self.affects_artifact_ids if str(item).strip()))
        self.affects_downstream_step_ids = sorted(set(str(item) for item in self.affects_downstream_step_ids if str(item).strip()))
        if not self.fingerprint:
            self.fingerprint = _stable_id(
                "conflict", self.conflict_type, self.left_claim_id, self.right_claim_id,
                ",".join(self.affects_step_ids),
            )
        if not self.conflict_id:
            self.conflict_id = self.fingerprint

    @classmethod
    def from_mapping(cls, value: Any) -> "KnowledgeConflict":
        if isinstance(value, cls):
            return value
        data = {key: item for key, item in (value or {}).items() if key in cls.__dataclass_fields__} if isinstance(value, dict) else {}
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def blocking(self) -> bool:
        return self.severity in {"blocking", "critical"} and self.status not in {"resolved", "accepted_exception", "superseded"}


@dataclass
class ConflictResolutionDecision:
    decision_id: str = ""
    conflict_id: str = ""
    decision: str = "require_probe"
    winning_claim_ids: List[str] = field(default_factory=list)
    invalidated_claim_ids: List[str] = field(default_factory=list)
    reason: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    required_probe_ids: List[str] = field(default_factory=list)
    allowed_changes: List[str] = field(default_factory=list)
    forbidden_changes: List[str] = field(default_factory=list)
    affected_step_ids: List[str] = field(default_factory=list)
    affected_downstream_step_ids: List[str] = field(default_factory=list)
    requires_user_approval: bool = False
    requires_plan_revision: bool = False
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.decision not in RESOLUTION_MODES:
            self.decision = "require_probe"
        if not self.decision_id:
            self.decision_id = _stable_id("resolution", self.conflict_id, self.decision)
        self.confidence = _safe_float(self.confidence)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _source_group(source_type: str) -> str:
    if source_type in {"system_policy", "user_instruction", "task_contract", "benchmark_rule"}:
        return "user"
    if source_type in {"plan", "plan_assumption"}:
        return "plan"
    if source_type in {"runtime_argv", "runtime_output"}:
        return "runtime"
    if source_type == "environment_probe":
        return "environment"
    if source_type in {"artifact_validation", "upstream_stage"}:
        return "artifact"
    if source_type == "skill_document":
        return "skill"
    if source_type in {"official_tool_documentation", "tool_help"}:
        return "tool"
    if source_type == "recovery_proposal":
        return "recovery"
    return "inference"


def _claim_semantic_key(claim: KnowledgeClaim) -> str:
    return str(claim.attributes.get("semantic_key") or claim.attributes.get("fact_key") or "").strip().lower()


def _canonical_semantic_value(value: Any, semantic_key: str = "") -> str:
    if isinstance(value, (dict, list, tuple, set)):
        serializable = sorted(value) if isinstance(value, set) else value
        return json.dumps(serializable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    text = str(value or "").strip().strip("`\"'")
    if semantic_key.startswith(("required_input.", "required_output.")) and ("/" in text or "\\" in text):
        text = os.path.normpath(os.path.expanduser(text))
    return text.casefold()


def _claim_operator(claim: KnowledgeClaim) -> str:
    value = str(claim.attributes.get("operator") or "eq").strip().lower()
    return value if value in {"eq", "neq", "contains", "forbid", "require", "prefer", "on_condition"} else ""


def _condition_identity(claim: KnowledgeClaim) -> str:
    return _canonical_semantic_value(claim.attributes.get("condition"))


def _typed_values_contradict(left: KnowledgeClaim, right: KnowledgeClaim, semantic_key: str) -> bool:
    left_op, right_op = _claim_operator(left), _claim_operator(right)
    if not left_op or not right_op:
        return False
    left_value = _canonical_semantic_value(left.attributes.get("value", left.proposition), semantic_key)
    right_value = _canonical_semantic_value(right.attributes.get("value", right.proposition), semantic_key)
    if not left_value or not right_value:
        return False
    if "on_condition" in {left_op, right_op}:
        if left_op != right_op or _condition_identity(left) != _condition_identity(right):
            return False
        return left_value != right_value
    if left_op == "forbid" or right_op == "forbid":
        if left_op == right_op:
            return False
        forbidden = left_value if left_op == "forbid" else right_value
        other = right if left_op == "forbid" else left
        other_op = _claim_operator(other)
        other_value = right_value if left_op == "forbid" else left_value
        if other_op in {"eq", "require", "prefer"}:
            return forbidden == other_value
        if other_op == "contains":
            return forbidden in other_value
        return False
    if left_op == "neq" or right_op == "neq":
        if left_op == right_op:
            return False
        excluded = left_value if left_op == "neq" else right_value
        asserted = right_value if left_op == "neq" else left_value
        return excluded == asserted
    if left_op == "contains" or right_op == "contains":
        if left_op == right_op:
            return left_value != right_value
        container = left_value if left_op == "contains" else right_value
        required = right_value if left_op == "contains" else left_value
        return required not in container
    return left_value != right_value


def _contradicts(left: KnowledgeClaim, right: KnowledgeClaim) -> Tuple[bool, bool]:
    if right.claim_id in left.attributes.get("contradicts_claim_ids", []) or left.claim_id in right.attributes.get("contradicts_claim_ids", []):
        return True, True
    if left.attributes.get("provenance_only") or right.attributes.get("provenance_only"):
        return False, False
    if any(
        claim.attributes.get("needs_clarification")
        or claim.attributes.get("ambiguity") not in (None, "", False, "false", "False", "none")
        for claim in (left, right)
    ):
        return False, False
    left_key, right_key = _claim_semantic_key(left), _claim_semantic_key(right)
    comparable = (
        right_key in {str(item).strip().lower() for item in left.attributes.get("comparable_semantic_keys", [])}
        or left_key in {str(item).strip().lower() for item in right.attributes.get("comparable_semantic_keys", [])}
    )
    if not left_key or not right_key or (left_key != right_key and not comparable):
        return False, False
    return _typed_values_contradict(left, right, left_key), True


def semantic_claim_set_hash(registry: ClaimRegistry | Dict[str, Any] | Iterable[Any]) -> str:
    """Hash active typed claims only; prose retained for audit cannot affect it."""
    if not isinstance(registry, ClaimRegistry):
        registry = ClaimRegistry.from_mapping(registry)
    rows = []
    for claim in registry.active_claims():
        key = _claim_semantic_key(claim)
        if not key or claim.attributes.get("provenance_only"):
            continue
        rows.append({
            "semantic_key": key,
            "operator": _claim_operator(claim),
            "value": _canonical_semantic_value(claim.attributes.get("value", claim.proposition), key),
            "condition": _condition_identity(claim),
            "strength": claim.strength,
            "source_type": claim.source_type,
            "scope": claim.scope,
            "scope_id": claim.scope_id,
            "status": claim.status,
        })
    rows.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _conflict_type(left: KnowledgeClaim, right: KnowledgeClaim) -> Optional[str]:
    groups = {_source_group(left.source_type), _source_group(right.source_type)}
    if groups == {"user", "plan"}:
        if left.attributes.get("scientific_method") or right.attributes.get("scientific_method"):
            return "SCIENTIFIC_METHOD_CONFLICT"
        return "USER_PLAN_CONFLICT"
    if groups == {"user", "recovery"}:
        return "USER_RECOVERY_CONFLICT"
    if groups == {"plan", "runtime"}:
        return "PLAN_RUNTIME_CONFLICT"
    if groups == {"plan", "environment"}:
        return "PLAN_ENVIRONMENT_CONFLICT"
    if groups == {"plan", "artifact"}:
        if left.source_type == "upstream_stage" or right.source_type == "upstream_stage":
            return "STAGE_CONTRACT_CONFLICT"
        return "PLAN_ARTIFACT_CONFLICT"
    if groups == {"skill", "runtime"}:
        return "SKILL_RUNTIME_CONFLICT"
    if groups == {"tool", "runtime"}:
        return "TOOL_BEHAVIOR_CONFLICT"
    if groups == {"plan", "skill"} and (left.attributes.get("method") or right.attributes.get("method")):
        return "SCIENTIFIC_METHOD_CONFLICT"
    if groups == {"artifact", "plan"}:
        return "STAGE_CONTRACT_CONFLICT"
    if groups == {"recovery", "recovery"}:
        return "RECOVERY_STRATEGY_CONFLICT"
    return None


class KnowledgeConflictDetector:
    """Deterministic claim comparison; it never edits a claim or a plan."""

    @staticmethod
    def detect(registry: ClaimRegistry, *, attempt: int = 0, only_step_ids: Optional[Sequence[str]] = None) -> List[KnowledgeConflict]:
        if not isinstance(registry, ClaimRegistry):
            registry = ClaimRegistry.from_mapping(registry)
        claims = [claim for claim in registry.active_claims() if claim.status == "active"]
        allowed_steps = {str(item) for item in (only_step_ids or []) if str(item).strip()}
        result: Dict[str, KnowledgeConflict] = {}
        for index, left in enumerate(claims):
            for right in claims[index + 1:]:
                conflict_type = _conflict_type(left, right)
                if not conflict_type:
                    continue
                contradicted, explicit = _contradicts(left, right)
                if not contradicted:
                    continue
                affected = set()
                for claim in (left, right):
                    if claim.scope == "stage" and claim.scope_id:
                        affected.add(str(claim.scope_id))
                    affected.update(str(item) for item in claim.attributes.get("step_ids", []) if str(item).strip())
                if allowed_steps and not (affected & allowed_steps):
                    continue
                hard = left.hard or right.hard
                hard_constraint = left.hard_constraint or right.hard_constraint
                safety = left.source_type == "system_policy" or right.source_type == "system_policy"
                user_claims = [
                    claim for claim in (left, right)
                    if _source_group(claim.source_type) == "user"
                ]
                user_hard = any(claim.hard for claim in user_claims)
                if safety:
                    severity = "critical"
                elif conflict_type in {"USER_PLAN_CONFLICT", "USER_RECOVERY_CONFLICT"}:
                    # A required plan assertion does not upgrade a user's
                    # preference into a hard constraint.  The typed user-side
                    # strength is authoritative for this boundary.
                    severity = "blocking" if user_hard else "warning"
                elif hard or conflict_type in {
                    "PLAN_RUNTIME_CONFLICT", "PLAN_ARTIFACT_CONFLICT", "STAGE_CONTRACT_CONFLICT",
                    "SCIENTIFIC_METHOD_CONFLICT", "TOOL_BEHAVIOR_CONFLICT", "PLAN_ENVIRONMENT_CONFLICT",
                    "SKILL_RUNTIME_CONFLICT", "RECOVERY_STRATEGY_CONFLICT",
                }:
                    severity = "blocking"
                elif explicit:
                    severity = "warning"
                else:
                    severity = "warning"
                ambiguous = conflict_type in {"TOOL_BEHAVIOR_CONFLICT", "PLAN_ENVIRONMENT_CONFLICT", "SKILL_RUNTIME_CONFLICT", "RECOVERY_STRATEGY_CONFLICT"} or not explicit
                probes = [_stable_id("probe", conflict_type, left.claim_id, right.claim_id)] if ambiguous else []
                status = "needs_probe" if ambiguous else "detected"
                steps = sorted(affected)
                conflict = KnowledgeConflict(
                    conflict_type=conflict_type,
                    left_claim_id=left.claim_id,
                    right_claim_id=right.claim_id,
                    description=f"{left.proposition}  <->  {right.proposition}",
                    severity=severity,
                    status=status,
                    evidence_ids=sorted(set(left.evidence_ids + right.evidence_ids)),
                    confidence=max(left.confidence * left.reliability, right.confidence * right.reliability),
                    affects_step_ids=steps,
                    affects_artifact_ids=sorted({str(item) for claim in (left, right) for item in claim.attributes.get("artifact_ids", [])}),
                    user_constraint_involved=_source_group(left.source_type) == "user" or _source_group(right.source_type) == "user",
                    hard_constraint_involved=hard_constraint,
                    safety_constraint_involved=safety,
                    candidate_resolutions=["verify the conflicting claims with a targeted probe", "revise only the affected plan scope after evidence"],
                    required_probe_ids=probes,
                    detected_at_attempt=attempt,
                )
                result[conflict.fingerprint] = conflict
        return list(result.values())

    @staticmethod
    def detect_stale_plan(registry: ClaimRegistry, *, attempt: int = 0) -> List[KnowledgeConflict]:
        claims = registry.active_claims() if isinstance(registry, ClaimRegistry) else ClaimRegistry.from_mapping(registry).active_claims()
        result: List[KnowledgeConflict] = []
        for plan in claims:
            if plan.source_type not in {"plan", "plan_assumption"}:
                continue
            expected = plan.attributes.get("fingerprint") or plan.attributes.get("plan_fingerprint")
            observed = plan.attributes.get("observed_fingerprint")
            if expected and observed and str(expected) != str(observed):
                runtime = next((claim for claim in claims if claim.source_type in {"runtime_output", "environment_probe", "artifact_validation"} and claim.scope_id == plan.scope_id), None)
                right_id = runtime.claim_id if runtime else "runtime_state"
                result.append(KnowledgeConflict(
                    conflict_type="STALE_PLAN",
                    left_claim_id=plan.claim_id,
                    right_claim_id=right_id,
                    description=f"Plan fingerprint {expected} differs from observed state {observed}.",
                    severity="blocking",
                    status="detected",
                    evidence_ids=plan.evidence_ids + (runtime.evidence_ids if runtime else []),
                    confidence=0.95,
                    affects_step_ids=list(plan.attributes.get("step_ids", [])),
                    hard_constraint_involved=False,
                    candidate_resolutions=["revalidate the affected step before execution", "create a new plan revision"],
                    detected_at_attempt=attempt,
                ))
        return result

    check = detect


class ConflictResolver:
    """Select a safe response without selecting a winner from weak evidence."""

    @staticmethod
    def resolve(conflict: KnowledgeConflict, registry: ClaimRegistry, *, probe_available: bool = True) -> ConflictResolutionDecision:
        registry = registry if isinstance(registry, ClaimRegistry) else ClaimRegistry.from_mapping(registry)
        left, right = registry.get(conflict.left_claim_id), registry.get(conflict.right_claim_id)
        claims = [claim for claim in (left, right) if claim]
        hard_constraints = [claim for claim in claims if claim.hard_constraint]
        winning = sorted(claims, key=lambda claim: (claim.priority, claim.reliability, claim.confidence), reverse=True)
        forbidden = [claim.proposition for claim in hard_constraints]
        decision = "require_probe"
        requires_user = False
        requires_revision = False
        reason = "Conflicting claims require evidence before changing the active plan."
        if conflict.conflict_type == "USER_RECOVERY_CONFLICT" and hard_constraints:
            decision = "require_probe" if probe_available else "request_user_decision"
            requires_user = not probe_available
            reason = "Recovery cannot remove or weaken a user hard constraint; verify an alternative repair first."
        elif conflict.conflict_type == "PLAN_RUNTIME_CONFLICT":
            decision = "require_probe"
            reason = "The runtime command disagrees with the plan; verify the executable, subcommand, and command provenance before changing the plan."
        elif conflict.conflict_type in {"TOOL_BEHAVIOR_CONFLICT", "PLAN_ENVIRONMENT_CONFLICT", "SKILL_RUNTIME_CONFLICT"}:
            decision = "require_probe"
            reason = "Tool, launcher, environment, or skill evidence is inconsistent; identify the executable and version before replanning."
        elif conflict.conflict_type == "STALE_PLAN":
            decision = "stage_replan"
            requires_revision = True
            reason = "The plan was created from a different runtime/input state and cannot execute without revalidation."
        elif conflict.conflict_type in {"PLAN_RUNTIME_CONFLICT", "PLAN_ARTIFACT_CONFLICT"} and not hard_constraints:
            decision = "local_replan"
            requires_revision = True
            reason = "A plan assumption was disproved by direct runtime/artifact evidence; revise the affected step only."
        elif conflict.conflict_type == "STAGE_CONTRACT_CONFLICT":
            decision = "downstream_replan"
            requires_revision = True
            reason = "The upstream/downstream contract changed; invalidate and revalidate dependent steps."
        elif conflict.conflict_type == "SCIENTIFIC_METHOD_CONFLICT":
            decision = "request_user_decision" if hard_constraints else "global_replan"
            requires_user = bool(hard_constraints)
            requires_revision = True
            reason = "The proposed method may not satisfy the requested scientific target."
        elif conflict.conflict_type == "RECOVERY_STRATEGY_CONFLICT":
            decision = "require_probe"
            reason = "Recovery strategies disagree; stop oscillation and collect discriminating evidence."
        elif conflict.blocking:
            decision = "request_user_decision" if conflict.user_constraint_involved and hard_constraints else "block"
            requires_user = bool(conflict.user_constraint_involved and hard_constraints)
            reason = "A blocking constraint conflict has no safe silent winner."
        else:
            decision = "continue_unchanged"
            reason = "The conflict is advisory and does not change a required contract."
        return ConflictResolutionDecision(
            conflict_id=conflict.conflict_id,
            decision=decision,
            winning_claim_ids=[claim.claim_id for claim in winning[:1]] if decision not in {"require_probe", "request_user_decision", "block"} else [],
            invalidated_claim_ids=[],
            reason=reason,
            evidence_ids=list(conflict.evidence_ids),
            required_probe_ids=list(conflict.required_probe_ids),
            allowed_changes=["change the affected command or step after validation"],
            forbidden_changes=forbidden,
            affected_step_ids=list(conflict.affects_step_ids),
            affected_downstream_step_ids=list(conflict.affects_downstream_step_ids),
            requires_user_approval=requires_user,
            requires_plan_revision=requires_revision,
            confidence=0.8 if conflict.status != "needs_probe" else 0.55,
        )

    decide = resolve


@dataclass
class PlanRevision:
    revision_id: str = ""
    parent_plan_version: str = "plan_v1"
    new_plan_version: str = "plan_v2"
    trigger_conflict_ids: List[str] = field(default_factory=list)
    revision_scope: str = "single_step"
    changed_step_ids: List[str] = field(default_factory=list)
    invalidated_step_ids: List[str] = field(default_factory=list)
    revalidation_required_step_ids: List[str] = field(default_factory=list)
    old_assumptions: List[str] = field(default_factory=list)
    new_assumptions: List[str] = field(default_factory=list)
    old_commands: List[str] = field(default_factory=list)
    new_commands: List[str] = field(default_factory=list)
    preserved_constraints: List[str] = field(default_factory=list)
    changed_preferences: List[str] = field(default_factory=list)
    reason: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    requires_user_approval: bool = False
    status: str = "applied"
    blocked_reason: str = ""
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.revision_scope not in REVISION_SCOPES:
            self.revision_scope = "single_step"
        if not self.revision_id:
            self.revision_id = _stable_id("revision", self.parent_plan_version, self.new_plan_version, self.reason, self.new_commands)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PlanRevisionManager:
    """Version and impact manager for the legacy dictionary plan state."""

    def __init__(self, plan_state: Optional[Dict[str, Any]] = None, root: str | Path | None = None, *, task_id: str = "", max_total_revisions: int = 6, max_stage_revisions: int = 3, max_same_plan_failures: int = 2) -> None:
        self.plan_state: Dict[str, Any] = copy.deepcopy(plan_state or {})
        self.root = Path(root) if root else None
        self.task_id = str(task_id or self.plan_state.get("task_id") or "default")
        self.max_total_revisions = int(max_total_revisions)
        self.max_stage_revisions = int(max_stage_revisions)
        self.max_same_plan_failures = int(max_same_plan_failures)
        self.plan_state.setdefault("plan_version", "plan_v1")
        self.plan_state.setdefault("revision_history", [])
        self.plan_state.setdefault("stale_step_ids", [])
        self.plan_state.setdefault("revalidation_required_step_ids", [])
        self.plan_state.setdefault("revision_budget", {"remaining": self.max_total_revisions})

    @property
    def current_version(self) -> str:
        return str(self.plan_state.get("plan_version") or "plan_v1")

    @staticmethod
    def _step_map(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {str(step.get("step_id")): step for step in state.get("steps", []) if isinstance(step, dict) and step.get("step_id")}

    def impact_analysis(self, changed_step_ids: Iterable[str], *, output_contract_changed: bool = False) -> Dict[str, List[str]]:
        changed = {str(item) for item in changed_step_ids if str(item).strip()}
        steps = self._step_map(self.plan_state)
        downstream: set[str] = set()
        frontier = set(changed)
        while frontier:
            next_frontier: set[str] = set()
            for step_id, step in steps.items():
                deps = {str(dep) for dep in step.get("depends_on", []) if str(dep).strip()}
                if deps & frontier and step_id not in changed and step_id not in downstream:
                    downstream.add(step_id)
                    next_frontier.add(step_id)
            frontier = next_frontier
        if output_contract_changed and not downstream:
            ordered = list(steps)
            for step_id in ordered:
                if step_id in changed:
                    downstream.update(ordered[ordered.index(step_id) + 1:])
                    break
        return {"changed_step_ids": sorted(changed), "downstream_step_ids": sorted(downstream), "revalidation_required_step_ids": sorted(changed | downstream)}

    def _budget_ok(self, scope: str) -> Tuple[bool, str]:
        history = [item for item in self.plan_state.get("revision_history", []) if isinstance(item, dict)]
        if len(history) >= self.max_total_revisions:
            return False, "BLOCKED_REPLAN_EXHAUSTED: total plan revision budget reached"
        if scope in {"stage", "downstream", "workflow"}:
            same_scope = [item for item in history if item.get("revision_scope") == scope]
            if len(same_scope) >= self.max_stage_revisions:
                return False, f"BLOCKED_REPLAN_EXHAUSTED: {scope} revision budget reached"
        return True, ""

    def _oscillates(self, signature: str) -> bool:
        history = [item for item in self.plan_state.get("revision_history", []) if isinstance(item, dict)]
        signatures = [str(item.get("signature") or "") for item in history]
        if signature in signatures:
            return True
        if len(signatures) >= 2 and signatures[-2] == signature:
            return True
        return False

    def revise(self, *, new_plan_state: Optional[Dict[str, Any]] = None, trigger_conflict_ids: Optional[Iterable[str]] = None, revision_scope: str = "single_step", changed_step_ids: Optional[Iterable[str]] = None, old_assumptions: Optional[Iterable[str]] = None, new_assumptions: Optional[Iterable[str]] = None, old_commands: Optional[Iterable[str]] = None, new_commands: Optional[Iterable[str]] = None, preserved_constraints: Optional[Iterable[str]] = None, changed_preferences: Optional[Iterable[str]] = None, reason: str = "", evidence_ids: Optional[Iterable[str]] = None, requires_user_approval: bool = False, output_contract_changed: bool = False) -> PlanRevision:
        changed = [str(item) for item in (changed_step_ids or []) if str(item).strip()]
        impact = self.impact_analysis(changed, output_contract_changed=output_contract_changed)
        old_cmds = [str(item) for item in (old_commands or [])]
        new_cmds = [str(item) for item in (new_commands or [])]
        signature = _stable_id("revision_sig", revision_scope, changed, old_cmds, new_cmds, trigger_conflict_ids or [])
        ok, blocked_reason = self._budget_ok(revision_scope)
        if ok and self._oscillates(signature):
            ok = False
            blocked_reason = "PLAN_OSCILLATION: the proposed revision repeats a prior plan state"
        parent = self.current_version
        next_number = int(re.search(r"(\d+)$", parent).group(1)) + 1 if re.search(r"(\d+)$", parent) else len(self.plan_state.get("revision_history", [])) + 2
        new_version = f"plan_v{next_number}"
        revision = PlanRevision(
            parent_plan_version=parent,
            new_plan_version=new_version,
            trigger_conflict_ids=list(trigger_conflict_ids or []),
            revision_scope=revision_scope,
            changed_step_ids=impact["changed_step_ids"],
            invalidated_step_ids=impact["downstream_step_ids"],
            revalidation_required_step_ids=impact["revalidation_required_step_ids"],
            old_assumptions=list(old_assumptions or []),
            new_assumptions=list(new_assumptions or []),
            old_commands=old_cmds,
            new_commands=new_cmds,
            preserved_constraints=list(preserved_constraints or []),
            changed_preferences=list(changed_preferences or []),
            reason=_clip(reason, 4000),
            evidence_ids=list(evidence_ids or []),
            requires_user_approval=requires_user_approval,
            status="applied" if ok else "blocked",
            blocked_reason=blocked_reason,
        )
        old_constraints = list(preserved_constraints or self.plan_state.get("user_constraints", []) or self.plan_state.get("required_constraints", []) or [])
        proposed_state = copy.deepcopy(self.plan_state)
        if new_plan_state:
            proposed_state.update(copy.deepcopy(new_plan_state))
        proposed_text = json.dumps(proposed_state, ensure_ascii=False).lower()
        missing_constraints = [
            str(item) for item in old_constraints
            if str(item).strip() and _normalize(item) not in _normalize(proposed_text)
        ]
        if ok and missing_constraints:
            revision.status = "blocked"
            revision.blocked_reason = "Plan revision would remove hard constraints: " + "; ".join(missing_constraints[:5])
            ok = False
        if not ok:
            self.plan_state["last_revision_blocked_reason"] = blocked_reason
            self._persist(revision, self.plan_state, None, signature)
            return revision
        parent_state = copy.deepcopy(self.plan_state)
        updated = copy.deepcopy(self.plan_state)
        if new_plan_state:
            for key, value in new_plan_state.items():
                if key == "steps" and isinstance(value, list):
                    old_steps = self._step_map(updated)
                    merged_steps = []
                    for step in value:
                        if not isinstance(step, dict):
                            continue
                        old_step = old_steps.get(str(step.get("step_id") or ""), {})
                        merged = copy.deepcopy(old_step)
                        merged.update(copy.deepcopy(step))
                        merged_steps.append(merged)
                    updated["steps"] = merged_steps
                else:
                    updated[key] = copy.deepcopy(value)
        updated["plan_version"] = new_version
        updated["parent_plan_version"] = parent
        updated["stale_step_ids"] = sorted(set(updated.get("stale_step_ids", [])) | set(impact["downstream_step_ids"]))
        updated["revalidation_required_step_ids"] = impact["revalidation_required_step_ids"]
        updated["revision_history"] = list(self.plan_state.get("revision_history", [])) + [{**revision.to_dict(), "signature": signature}]
        updated["revision_budget"] = {"remaining": max(0, self.max_total_revisions - len(updated["revision_history"]))}
        self.plan_state = updated
        self._persist(revision, updated, parent_state, signature)
        return revision

    def mark_step_revalidated(self, step_id: str) -> Dict[str, Any]:
        stale = set(self.plan_state.get("stale_step_ids", []))
        required = set(self.plan_state.get("revalidation_required_step_ids", []))
        stale.discard(str(step_id))
        required.discard(str(step_id))
        self.plan_state["stale_step_ids"] = sorted(stale)
        self.plan_state["revalidation_required_step_ids"] = sorted(required)
        self._persist(None, self.plan_state, None, "revalidation")
        return self.plan_state

    create_revision = revise
    apply_revision = revise

    def persist_active(self) -> None:
        """Persist the current active plan even when no revision was made."""
        self._persist(None, self.plan_state, None, "active")

    def assert_step_executable(self, step_id: str) -> Tuple[bool, str]:
        if str(step_id) in {str(item) for item in self.plan_state.get("stale_step_ids", [])}:
            return False, f"Step {step_id} uses stale plan evidence and requires revalidation."
        if str(step_id) in {str(item) for item in self.plan_state.get("revalidation_required_step_ids", [])}:
            return False, f"Step {step_id} requires contract revalidation after a plan revision."
        return True, ""

    def _persist(self, revision: Optional[PlanRevision], state: Dict[str, Any], source_plan: Optional[Dict[str, Any]], signature: str) -> None:
        if not self.root:
            return
        conflict_root = self.root / "conflicts"
        plans_root = self.root / "plans"
        conflict_root.mkdir(parents=True, exist_ok=True)
        plans_root.mkdir(parents=True, exist_ok=True)
        if revision:
            with (conflict_root / "plan_revisions.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(_safe_json({**revision.to_dict(), "signature": signature}) + "\n")
            with (conflict_root / "plan_impact_analysis.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    _safe_json(
                        {
                            "revision_id": revision.revision_id,
                            "revision_scope": revision.revision_scope,
                            "changed_step_ids": revision.changed_step_ids,
                            "invalidated_step_ids": revision.invalidated_step_ids,
                            "revalidation_required_step_ids": revision.revalidation_required_step_ids,
                            "output_contract_changed": bool(revision.invalidated_step_ids),
                        },
                    )
                    + "\n"
                )
            if revision.status == "blocked":
                return
            parent_file = plans_root / f"{revision.parent_plan_version}.json"
            if not parent_file.exists():
                parent_file.write_text(_safe_json(source_plan or self.plan_state, indent=2), encoding="utf-8")
            (plans_root / f"{revision.new_plan_version}.json").write_text(_safe_json(state, indent=2), encoding="utf-8")
            diff = {
                "parent_plan_version": revision.parent_plan_version,
                "new_plan_version": revision.new_plan_version,
                "revision": revision.to_dict(),
                "changed_steps": revision.changed_step_ids,
                "invalidated_steps": revision.invalidated_step_ids,
            }
            (plans_root / f"{revision.parent_plan_version}_to_{revision.new_plan_version}.diff.json").write_text(_safe_json(diff, indent=2), encoding="utf-8")
        (plans_root / "active_plan.json").write_text(_safe_json(state, indent=2), encoding="utf-8")


class KnowledgeConflictStore:
    """Small JSON/JSONL audit store shared by nodes without credentials."""

    def __init__(self, root: str | Path, task_id: str = "") -> None:
        self.root = Path(root) / "conflicts"
        self.task_id = str(task_id or "")
        self.root.mkdir(parents=True, exist_ok=True)

    def save_registry(self, registry: ClaimRegistry) -> None:
        path = self.root / "claims.jsonl"
        path.write_text("\n".join(_safe_json(item.to_dict()) for item in registry.all()) + ("\n" if registry.all() else ""), encoding="utf-8")

    def append(self, filename: str, values: Iterable[Any]) -> None:
        items = list(values)
        if not items:
            return
        with (self.root / filename).open("a", encoding="utf-8") as handle:
            for value in items:
                payload = value.to_dict() if hasattr(value, "to_dict") else value
                handle.write(_safe_json(payload, default=str) + "\n")

    def save_summary(self, *, conflicts: Iterable[KnowledgeConflict], resolutions: Iterable[ConflictResolutionDecision], plan_state: Optional[Dict[str, Any]] = None, user_hard_constraints: Optional[Iterable[str]] = None) -> None:
        conflicts_list = [item if isinstance(item, KnowledgeConflict) else KnowledgeConflict.from_mapping(item) for item in conflicts]
        resolutions_list = [item if isinstance(item, ConflictResolutionDecision) else ConflictResolutionDecision(**item) for item in resolutions]
        blocking = [item for item in conflicts_list if item.blocking]
        summary = {
            "task_id": self.task_id,
            "unresolved_conflicts": [item.to_dict() for item in conflicts_list if item.status not in {"resolved", "accepted_exception", "superseded"}],
            "blocking_conflicts": [item.conflict_id for item in blocking],
            "user_hard_constraints": [str(item) for item in (user_hard_constraints or []) if str(item).strip()],
            "invalid_plan_assumptions": [item.left_claim_id for item in conflicts_list if item.conflict_type in {"PLAN_RUNTIME_CONFLICT", "PLAN_ARTIFACT_CONFLICT", "STALE_PLAN"}],
            "active_plan_version": (plan_state or {}).get("plan_version", "plan_v1"),
            "stale_steps": (plan_state or {}).get("stale_step_ids", []),
            "revalidation_required_steps": (plan_state or {}).get("revalidation_required_step_ids", []),
            "remaining_replan_budget": (plan_state or {}).get("revision_budget", {}).get("remaining"),
            "resolutions": [item.to_dict() for item in resolutions_list],
        }
        (self.root / "conflict_summary.json").write_text(_safe_json(summary, indent=2), encoding="utf-8")
        (self.root / "stale_steps.json").write_text(
            _safe_json(
                {
                    "stale_steps": summary["stale_steps"],
                    "revalidation_required_steps": summary["revalidation_required_steps"],
                    "active_plan_version": summary["active_plan_version"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def feature_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


__all__ = [
    "CLAIM_SOURCES", "CLAIM_STRENGTHS", "CLAIM_SCOPES", "SOURCE_PRIORITY", "CONFLICT_TYPES", "RESOLUTION_MODES",
    "KnowledgeClaim", "ClaimRegistry", "KnowledgeConflict", "KnowledgeConflictDetector", "ConflictResolutionDecision",
    "ConflictResolver", "PlanRevision", "PlanRevisionManager", "KnowledgeConflictStore", "feature_enabled",
]

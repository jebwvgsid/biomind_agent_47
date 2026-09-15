"""Typed semantic-completion facts between verification and step commit.

The LLM reviewer may select one schema-validated semantic disposition.  It does
not select graph nodes, and its prose is never classified.  Controller-owned
predicates are evaluated independently and take precedence over the reviewer's
assessment. Missing plain observations require explicit review as warnings.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence

from agent_core.genomics_contig_parser import (
    CONTIG_PARSER_ID,
    ContigParserError,
    build_contig_compatibility_receipt,
)
from agent_core.step_effects import (
    StepEffectProtocolError,
    canonical_execution_effect_evidence,
    canonical_step_effect,
)
from agent_core.workspace_attestation import validate_runtime_mutation_receipt
from agent_core.evidence_projection import (
    artifact_collection_facts,
    artifact_registration_facts,
    runtime_observation_facts,
    candidate_binding_facts,
    committed_dependency_facts,
)


SEMANTIC_COMPLETION_SCHEMA_VERSION = 1
SEMANTIC_COMPLETION_EVALUATOR_ID = "biomind.semantic_completion.v1"
COMPLETION_ACCEPTED = "completion_accepted"
SEMANTIC_DISPOSITIONS = frozenset({
    COMPLETION_ACCEPTED,
    "inspection_required",
    "correction_required",
    "plan_revision_required",
    "user_information_required",
    "assessment_pending",
})
NON_COMMIT_DISPOSITIONS = SEMANTIC_DISPOSITIONS - {COMPLETION_ACCEPTED}
ISSUE_DISPOSITIONS = SEMANTIC_DISPOSITIONS | {"advisory_only"}
REVIEW_ASSESSMENTS = frozenset({"ACCEPTABLE", "CONCERN", "UNKNOWN"})
REVIEWER_PROTOCOL_CORRECTIONS = 2
REVIEWER_POLICY_VERSION = "semantic-review-correction-v1"
REVIEWER_ISSUE_FIELDS = frozenset({"issue_id", "disposition", "explanation"})
REVIEWER_RESPONSE_FIELDS = frozenset({"assessment", "disposition", "concerns",
    "suggestions", "confidence", "report_id", "issue_dispositions"})
_REVIEW_DISPOSITIONS = {
    "ACCEPTABLE": frozenset({COMPLETION_ACCEPTED}),
    "CONCERN": frozenset({
        "inspection_required",
        "correction_required",
        "plan_revision_required",
        "user_information_required",
        "assessment_pending",
    }),
    "UNKNOWN": frozenset({
        "inspection_required",
        "user_information_required",
        "assessment_pending",
    }),
}
_SCOPE_KEYS = (
    "task_id",
    "plan_id",
    "plan_version",
    "step_id",
    "state_generation",
    "action_id",
    "attempt_id",
    "validation_id",
)


class SemanticCompletionError(ValueError):
    """Stable failure for malformed report, decision, or predicate contracts."""

    def __init__(self, code: str, detail: str = "", *, path: str = "$", actual: Any = None, expected: Any = None) -> None:
        self.code = str(code or "SEMANTIC_COMPLETION_INVALID")
        self.detail = str(detail or "")
        self.path, self.actual, self.expected = path, actual, expected
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")

    def to_dict(self) -> Dict[str, Any]:
        return {"code":self.code, "detail":self.detail, "field_path":self.path,
                "actual":self.actual, "expected":self.expected}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise SemanticCompletionError(
        "SEMANTIC_COMPLETION_JSON_INVALID", type(value).__name__
    )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SemanticCompletionError(
            "SEMANTIC_COMPLETION_JSON_INVALID", type(exc).__name__
        ) from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_list(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SemanticCompletionError("SEMANTIC_DECISION_FIELD_INVALID", field)
    if any(not isinstance(item, str) for item in value):
        raise SemanticCompletionError("SEMANTIC_DECISION_FIELD_INVALID", field)
    return tuple(item.strip()[:1000] for item in value if item.strip())


def _issue_disposition_list(
    value: Any, *, field: str = "issue_dispositions"
) -> tuple[Dict[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise SemanticCompletionError("SEMANTIC_DECISION_FIELD_INVALID", field)
    normalized: list[Dict[str, str]] = []
    seen: set[str] = set()
    required = REVIEWER_ISSUE_FIELDS
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != required:
            extras = sorted(set(raw)-required) if isinstance(raw, Mapping) else []
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_ISSUE_DISPOSITION_INVALID", "Issue row must use exactly the declared string fields.",
                path=f"$.{field}[{index}]" + ("."+extras[0] if extras else ""),
                actual=sorted(raw) if isinstance(raw, Mapping) else type(raw).__name__, expected=sorted(required),
            )
        if any(not isinstance(raw[key], str) for key in required):
            raise SemanticCompletionError("SEMANTIC_DECISION_ISSUE_DISPOSITION_INVALID", "Issue values must be strings.",
                path=f"$.{field}[{index}]", actual={k:type(raw[k]).__name__ for k in required}, expected="string values")
        issue_id = str(raw.get("issue_id") or "").strip()
        disposition = str(raw.get("disposition") or "").strip().lower()
        explanation = str(raw.get("explanation") or "").strip()[:1000]
        if (
            not issue_id
            or issue_id in seen
            or disposition not in ISSUE_DISPOSITIONS
            or not explanation
        ):
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_ISSUE_DISPOSITION_INVALID",
                issue_id or field,
            )
        seen.add(issue_id)
        normalized.append({
            "issue_id": issue_id,
            "disposition": disposition,
            "explanation": explanation,
        })
    return tuple(normalized)


def completion_scope(
    plan_state: Mapping[str, Any], validation: Mapping[str, Any]
) -> Dict[str, Any]:
    return {
        "task_id": str(plan_state.get("task_id") or validation.get("task_id") or ""),
        "plan_id": str(plan_state.get("plan_id") or ""),
        "plan_version": str(plan_state.get("plan_version") or ""),
        "step_id": str(validation.get("step_id") or plan_state.get("current_step_id") or ""),
        "state_generation": int(plan_state.get("state_generation") or 0),
        "action_id": str(validation.get("action_id") or ""),
        "attempt_id": str(validation.get("attempt_id") or ""),
        "validation_id": str(validation.get("validation_id") or ""),
    }


def _validate_scope(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SemanticCompletionError("SEMANTIC_COMPLETION_SCOPE_INVALID", "scope")
    scope = {key: deepcopy(value.get(key)) for key in _SCOPE_KEYS}
    for key in _SCOPE_KEYS:
        if key == "state_generation":
            try:
                scope[key] = int(scope[key])
            except (TypeError, ValueError) as exc:
                raise SemanticCompletionError(
                    "SEMANTIC_COMPLETION_SCOPE_INVALID", key
                ) from exc
        elif not str(scope[key] or "").strip():
            raise SemanticCompletionError("SEMANTIC_COMPLETION_SCOPE_INVALID", key)
        else:
            scope[key] = str(scope[key]).strip()
    return scope


@dataclass(frozen=True)
class SemanticDecision:
    schema_version: int
    decision_id: str
    scope: Mapping[str, Any]
    source: str
    reviewed_report_id: str
    issue_dispositions: tuple[Mapping[str, str], ...]
    assessment: str
    disposition: str
    concerns: tuple[str, ...]
    suggestions: tuple[str, ...]
    confidence: Optional[float]
    parse_status: str

    @staticmethod
    def _body(
        *,
        scope: Mapping[str, Any],
        source: str,
        reviewed_report_id: str,
        issue_dispositions: Sequence[Mapping[str, str]],
        assessment: str,
        disposition: str,
        concerns: Sequence[str],
        suggestions: Sequence[str],
        confidence: Optional[float],
        parse_status: str,
    ) -> Dict[str, Any]:
        return {
            "schema_version": SEMANTIC_COMPLETION_SCHEMA_VERSION,
            "scope": _validate_scope(scope),
            "source": str(source or "").strip(),
            "reviewed_report_id": str(reviewed_report_id or "").strip(),
            "issue_dispositions": [dict(item) for item in issue_dispositions],
            "assessment": str(assessment or "").strip().upper(),
            "disposition": str(disposition or "").strip().lower(),
            "concerns": [str(item) for item in concerns],
            "suggestions": [str(item) for item in suggestions],
            "confidence": confidence,
            "parse_status": str(parse_status or "").strip(),
        }

    @classmethod
    def create(
        cls,
        *,
        scope: Mapping[str, Any],
        source: str,
        assessment: str,
        disposition: str,
        reviewed_report_id: str = "",
        issue_dispositions: Sequence[Mapping[str, str]] = (),
        concerns: Sequence[str] = (),
        suggestions: Sequence[str] = (),
        confidence: Optional[float] = None,
        parse_status: str = "valid",
    ) -> "SemanticDecision":
        assessment = str(assessment or "").strip().upper()
        disposition = str(disposition or "").strip().lower()
        source = str(source or "").strip()
        reviewed_report_id = str(reviewed_report_id or "").strip()
        normalized_issues = _issue_disposition_list(
            list(issue_dispositions), field="issue_dispositions"
        )
        if assessment not in REVIEW_ASSESSMENTS:
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_ASSESSMENT_INVALID", assessment
            )
        if disposition not in SEMANTIC_DISPOSITIONS:
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_DISPOSITION_INVALID", disposition
            )
        if source == "llm_reviewer" and disposition not in _REVIEW_DISPOSITIONS[assessment]:
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_COMBINATION_INVALID",
                f"{assessment}:{disposition}",
            )
        if source == "llm_reviewer":
            if not reviewed_report_id.startswith("completion-report-"):
                raise SemanticCompletionError(
                    "SEMANTIC_DECISION_REPORT_BINDING_REQUIRED"
                )
            if disposition != COMPLETION_ACCEPTED and not normalized_issues:
                raise SemanticCompletionError(
                    "SEMANTIC_DECISION_ISSUE_DISPOSITION_REQUIRED"
                )
            issue_actions = {item["disposition"] for item in normalized_issues}
            if disposition == COMPLETION_ACCEPTED and any(
                item not in {COMPLETION_ACCEPTED, "advisory_only"}
                for item in issue_actions
            ):
                raise SemanticCompletionError(
                    "SEMANTIC_DECISION_ISSUE_DISPOSITION_CONFLICT"
                )
            if (
                disposition != COMPLETION_ACCEPTED
                and disposition not in issue_actions
            ):
                raise SemanticCompletionError(
                    "SEMANTIC_DECISION_ISSUE_DISPOSITION_CONFLICT"
                )
        if confidence is not None:
            if isinstance(confidence, bool):
                raise SemanticCompletionError(
                    "SEMANTIC_DECISION_CONFIDENCE_INVALID", str(confidence)
                )
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise SemanticCompletionError(
                    "SEMANTIC_DECISION_CONFIDENCE_INVALID", str(confidence)
                ) from exc
            if not 0.0 <= confidence <= 1.0:
                raise SemanticCompletionError(
                    "SEMANTIC_DECISION_CONFIDENCE_INVALID", str(confidence)
                )
        body = cls._body(
            scope=scope,
            source=source,
            reviewed_report_id=reviewed_report_id,
            issue_dispositions=normalized_issues,
            assessment=assessment,
            disposition=disposition,
            concerns=concerns,
            suggestions=suggestions,
            confidence=confidence,
            parse_status=parse_status,
        )
        if not body["source"] or not body["parse_status"]:
            raise SemanticCompletionError("SEMANTIC_DECISION_FIELD_REQUIRED")
        decision_id = "semantic-decision-" + _digest(body).removeprefix("sha256:")
        return cls(
            schema_version=SEMANTIC_COMPLETION_SCHEMA_VERSION,
            decision_id=decision_id,
            scope=MappingProxyType(body["scope"]),
            source=source,
            reviewed_report_id=reviewed_report_id,
            issue_dispositions=tuple(
                MappingProxyType(dict(item)) for item in normalized_issues
            ),
            assessment=assessment,
            disposition=disposition,
            concerns=tuple(str(item) for item in concerns),
            suggestions=tuple(str(item) for item in suggestions),
            confidence=confidence,
            parse_status=str(parse_status),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SemanticDecision":
        if not isinstance(value, Mapping):
            raise SemanticCompletionError("SEMANTIC_DECISION_OBJECT_REQUIRED")
        allowed = {
            "schema_version",
            "decision_id",
            "scope",
            "source",
            "reviewed_report_id",
            "issue_dispositions",
            "assessment",
            "disposition",
            "concerns",
            "suggestions",
            "confidence",
            "parse_status",
        }
        if set(value) - allowed:
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_FIELD_UNKNOWN", ",".join(sorted(set(value) - allowed))
            )
        missing = sorted(allowed - set(value))
        if missing:
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_FIELD_REQUIRED", ",".join(missing)
            )
        try:
            schema_version = int(value.get("schema_version") or 0)
        except (TypeError, ValueError) as exc:
            raise SemanticCompletionError("SEMANTIC_DECISION_SCHEMA_UNSUPPORTED") from exc
        if schema_version != SEMANTIC_COMPLETION_SCHEMA_VERSION:
            raise SemanticCompletionError("SEMANTIC_DECISION_SCHEMA_UNSUPPORTED")
        created = cls.create(
            scope=value.get("scope") or {},
            source=str(value.get("source") or ""),
            reviewed_report_id=str(value.get("reviewed_report_id") or ""),
            issue_dispositions=_issue_disposition_list(
                value.get("issue_dispositions"), field="issue_dispositions"
            ),
            assessment=str(value.get("assessment") or ""),
            disposition=str(value.get("disposition") or ""),
            concerns=_text_list(value.get("concerns"), field="concerns"),
            suggestions=_text_list(value.get("suggestions"), field="suggestions"),
            confidence=value.get("confidence"),
            parse_status=str(value.get("parse_status") or ""),
        )
        if str(value.get("decision_id") or "") != created.decision_id:
            raise SemanticCompletionError("SEMANTIC_DECISION_HASH_MISMATCH")
        return created

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "scope": dict(self.scope),
            "source": self.source,
            "reviewed_report_id": self.reviewed_report_id,
            "issue_dispositions": [
                dict(item) for item in self.issue_dispositions
            ],
            "assessment": self.assessment,
            "disposition": self.disposition,
            "concerns": list(self.concerns),
            "suggestions": list(self.suggestions),
            "confidence": self.confidence,
            "parse_status": self.parse_status,
        }


def reviewer_decision(
    advisory: Any,
    *,
    scope: Mapping[str, Any],
    report_id: str,
    allowed_issue_ids: Sequence[str] = (),
    required_issue_ids: Sequence[str] = (),
    review_required: bool = False,
) -> SemanticDecision:
    """Validate the reviewer JSON without classifying concern prose."""

    if advisory in (None, {}, ""):
        if review_required:
            return SemanticDecision.create(
                scope=scope,
                source="review_policy_pending",
                assessment="UNKNOWN",
                disposition="assessment_pending",
                reviewed_report_id=report_id,
                concerns=("SEMANTIC_REVIEW_REQUIRED_NOT_YET_CONSUMED",),
                parse_status="pending",
            )
        return SemanticDecision.create(
            scope=scope,
            source="controller_default",
            assessment="ACCEPTABLE",
            disposition=COMPLETION_ACCEPTED,
            reviewed_report_id=report_id,
            parse_status="not_requested",
        )
    if not isinstance(advisory, Mapping):
        return SemanticDecision.create(
            scope=scope,
            source="reviewer_unavailable",
            assessment="UNKNOWN",
            disposition="assessment_pending",
            reviewed_report_id=report_id,
            concerns=("SEMANTIC_REVIEW_OBJECT_INVALID",),
            parse_status="invalid",
        )
    try:
        parse_status = str(advisory.get("parse_status") or "valid").strip()
        if parse_status != "valid":
            # Preserve the local lifecycle's failure class and precise cause;
            # a provider cannot set these fields through the strict parser.
            return SemanticDecision.create(scope=scope, reviewed_report_id=report_id,
                source="reviewer_transport" if parse_status=='request_failed_ignored' else "reviewer_protocol",
                assessment="UNKNOWN",disposition="assessment_pending",
                concerns=tuple(str(x) for x in advisory.get('concerns') or ["SEMANTIC_REVIEW_NOT_VALID"]),
                parse_status=parse_status)
        normalized = validate_reviewer_payload(
            advisory,
            allow_parse_status=True,
            expected_report_id=report_id,
            allowed_issue_ids=allowed_issue_ids,
            required_issue_ids=required_issue_ids,
        )
        return SemanticDecision.create(
            scope=scope,
            source="llm_reviewer",
            reviewed_report_id=normalized["report_id"],
            issue_dispositions=normalized["issue_dispositions"],
            assessment=normalized["assessment"],
            disposition=normalized["disposition"],
            concerns=normalized["concerns"],
            suggestions=normalized["suggestions"],
            confidence=normalized["confidence"],
            parse_status=parse_status,
        )
    except SemanticCompletionError as exc:
        return SemanticDecision.create(
            scope=scope,
            source="reviewer_unavailable",
            assessment="UNKNOWN",
            disposition="assessment_pending",
            reviewed_report_id=report_id,
            concerns=(exc.code,),
            parse_status="invalid",
        )


def validate_reviewer_payload(
    value: Mapping[str, Any],
    *,
    allow_parse_status: bool = False,
    expected_report_id: str = "",
    allowed_issue_ids: Sequence[str] = (),
    required_issue_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Strictly validate one provider payload; never inspect its prose."""

    if not isinstance(value, Mapping):
        raise SemanticCompletionError("SEMANTIC_REVIEW_OBJECT_REQUIRED")
    allowed = set(REVIEWER_RESPONSE_FIELDS)
    if allow_parse_status:
        # These are attached only by the local Reviewer lifecycle. They are
        # forbidden in provider payloads (the default parser path).
        allowed.update({"parse_status", "review_policy_receipt", "review_error"})
    extras = sorted(set(value) - allowed)
    if extras:
        raise SemanticCompletionError(
            "SEMANTIC_REVIEW_FIELD_UNKNOWN", ",".join(extras),
            path="$."+extras[0],actual=extras,expected=sorted(allowed),
        )
    required_fields = {
        "assessment", "disposition", "concerns", "suggestions", "confidence"
    }
    if expected_report_id:
        required_fields.update({"report_id", "issue_dispositions"})
    missing = sorted(required_fields - set(value))
    if missing:
        raise SemanticCompletionError(
            "SEMANTIC_REVIEW_FIELD_REQUIRED", ",".join(missing)
        )
    assessment = str(value.get("assessment") or "").strip().upper()
    disposition = str(value.get("disposition") or "").strip().lower()
    if assessment not in REVIEW_ASSESSMENTS:
        raise SemanticCompletionError(
            "SEMANTIC_DECISION_ASSESSMENT_INVALID", assessment
        )
    if disposition not in _REVIEW_DISPOSITIONS[assessment]:
        raise SemanticCompletionError(
            "SEMANTIC_DECISION_COMBINATION_INVALID",
            f"{assessment}:{disposition}",
        )
    concerns = _text_list(value.get("concerns"), field="concerns")
    suggestions = _text_list(value.get("suggestions"), field="suggestions")
    report_id = str(value.get("report_id") or "").strip()
    issues = _issue_disposition_list(
        value.get("issue_dispositions") or (), field="issue_dispositions"
    )
    if expected_report_id and report_id != expected_report_id:
        raise SemanticCompletionError("SEMANTIC_REVIEW_REPORT_ID_MISMATCH")
    allowed_ids = {str(item) for item in allowed_issue_ids if str(item)}
    required_ids = {str(item) for item in required_issue_ids if str(item)}
    supplied_ids = {item["issue_id"] for item in issues}
    if allowed_ids and supplied_ids - allowed_ids:
        raise SemanticCompletionError(
            "SEMANTIC_REVIEW_ISSUE_ID_UNKNOWN",
            ",".join(sorted(supplied_ids - allowed_ids)),
        )
    if required_ids - supplied_ids:
        raise SemanticCompletionError(
            "SEMANTIC_REVIEW_ISSUE_ID_UNCONSUMED",
            ",".join(sorted(required_ids - supplied_ids)),
        )
    issue_actions = {item["disposition"] for item in issues}
    if disposition == COMPLETION_ACCEPTED and any(
        item not in {COMPLETION_ACCEPTED, "advisory_only"}
        for item in issue_actions
    ):
        raise SemanticCompletionError(
            "SEMANTIC_REVIEW_ISSUE_DISPOSITION_CONFLICT"
        )
    if (
        expected_report_id
        and disposition != COMPLETION_ACCEPTED
        and disposition not in issue_actions
    ):
        raise SemanticCompletionError(
            "SEMANTIC_REVIEW_ISSUE_DISPOSITION_CONFLICT"
        )
    confidence = value.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool):
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_CONFIDENCE_INVALID", str(confidence)
            )
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_CONFIDENCE_INVALID", str(confidence)
            ) from exc
        if not 0.0 <= confidence <= 1.0:
            raise SemanticCompletionError(
                "SEMANTIC_DECISION_CONFIDENCE_INVALID", str(confidence)
            )
    return {
        "assessment": assessment,
        "disposition": disposition,
        "report_id": report_id,
        "issue_dispositions": [dict(item) for item in issues],
        "concerns": list(concerns),
        "suggestions": list(suggestions),
        "confidence": confidence,
        "parse_status": "valid",
    }


def reviewer_response_schema() -> Dict[str, Any]:
    """Documentation and correction schema built from the parser's constants."""
    return {"schema":"biomind.semantic_decision.v1", "fields":sorted(REVIEWER_RESPONSE_FIELDS),
        "issue_fields":sorted(REVIEWER_ISSUE_FIELDS), "additional_fields":False,
        "issue_values":"strings", "assessments":sorted(REVIEW_ASSESSMENTS),
        "assessment_dispositions":{k:sorted(v) for k,v in _REVIEW_DISPOSITIONS.items()},
        "issue_dispositions":sorted(ISSUE_DISPOSITIONS), "confidence":"top-level null or number in [0,1]"}


def reviewer_assessment_identity(report: Mapping[str, Any], plan_state: Mapping[str, Any],
                                 source_hashes: Mapping[str, str]) -> Dict[str, Any]:
    """Stable formal facts, not report/inspection wrappers or natural language inference."""
    scope={k:str((report.get("scope") or {}).get(k) or "")
           for k in ("task_id","plan_id","plan_version","step_id","action_id","attempt_id")}
    step=_current_step(plan_state,scope['step_id'])
    facts={"scope":scope,"plan_revision":plan_state.get('plan_revision'),
        "obligations":{"step_effect":canonical_step_effect(step),
            "success_criteria":deepcopy(step.get('success_criteria') or []),
            "completion_predicates":deepcopy(step.get('completion_predicates') or [])},
        "source_hashes":dict(source_hashes),
        "process":{k:v for k,v in (report.get('process_outcome') or {}).items()
                   if k not in {'runtime_observations'}},
        "effects":{k:deepcopy((report.get('effect_presence') or {}).get(k))
                   for k in ('declared','observed','missing','unexpected','invalid','file_collection',
                             'declared_dependency_facts','workspace_contracts')},
        "inspection_sources": sorted({
            (str((row.get("observation") or {}).get("source_ref") or ""),
             str((row.get("observation") or {}).get("content_sha256") or
                 _digest((row.get("observation") or {}).get("result") or {})))
            for row in (report.get("effect_presence") or {}).get("revalidation_inspection_facts", ())
            if row.get("observation") and not (row["observation"].get("error"))}),
        "predicates":[{k:deepcopy(row.get(k)) for k in (
            'predicate_id','operator','authoritative_actual','expected','passed','status','reason_code','disposition')}
            for row in report.get('predicate_results') or []]}
    return {"assessment_id":"assessment:"+_digest(facts),"scope":scope,
            "facts_sha256":_digest(facts),"policy_version":REVIEWER_POLICY_VERSION}


def _current_step(plan_state: Mapping[str, Any], step_id: str) -> Mapping[str, Any]:
    for step in plan_state.get("steps", ()) or ():
        if isinstance(step, Mapping) and str(step.get("step_id") or "") == step_id:
            return step
    raise SemanticCompletionError("SEMANTIC_COMPLETION_STEP_MISSING", step_id)


def _task_input_paths(plan_state: Mapping[str, Any]) -> Dict[str, str]:
    current_task = plan_state.get("current_task")
    current_task = dict(current_task) if isinstance(current_task, Mapping) else {}
    task_spec = current_task.get("task_spec")
    if not isinstance(task_spec, Mapping):
        task_spec = plan_state.get("task_spec")
    task_spec = dict(task_spec) if isinstance(task_spec, Mapping) else {}
    result: Dict[str, str] = {}
    for item in task_spec.get("inputs", ()) or ():
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip()
        path = str(item.get("canonical_path") or item.get("path") or "").strip()
        if role and path:
            result.setdefault(role, path)
    return result


def _observation_map(*values: Any) -> Dict[str, Mapping[str, Any]]:
    projected = canonical_execution_effect_evidence(list(values))
    result: Dict[str, Mapping[str, Any]] = {}
    for item in projected.get("produces_observations", ()) or ():
        if not isinstance(item, Mapping):
            continue
        identity = str(
            item.get("identity")
            or item.get("observation_id")
            or item.get("result_id")
            or ""
        ).strip()
        if identity:
            result[identity] = item
    return result


def _predicate_result_id(value: Mapping[str, Any]) -> str:
    return "predicate-result-" + _digest(value).removeprefix("sha256:")


def _step_effect_contract_error(reason_code: str) -> list[Dict[str, Any]]:
    body: Dict[str, Any] = {
        "schema_version": 1,
        "predicate_id": "step_effect.contract",
        "observation_id": "<step_effect>",
        "operator": "boolean_equals",
        "expected": True,
        "claimed_actual": None,
        "authoritative_actual": None,
        "status": "contract_error",
        "passed": False,
        "reason_code": str(reason_code or "STEP_EFFECT_CONTRACT_INVALID"),
        "disposition": "plan_revision_required",
        "evaluator_id": SEMANTIC_COMPLETION_EVALUATOR_ID,
        "parser_id": "",
        "parser_receipt": {},
    }
    body["result_id"] = _predicate_result_id(body)
    return [body]


def _predicate_applicability(
    predicate: Mapping[str, Any], plan_state: Mapping[str, Any]
) -> tuple[bool, Dict[str, Any]]:
    raw = predicate.get("applicability")
    if raw in (None, {}):
        return True, {"schema_version": 1, "kind": "always", "matched_step_ids": []}
    if not isinstance(raw, Mapping):
        raise SemanticCompletionError("BOOLEAN_PREDICATE_APPLICABILITY_INVALID")
    try:
        schema_version = int(raw.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise SemanticCompletionError(
            "BOOLEAN_PREDICATE_APPLICABILITY_INVALID"
        ) from exc
    kind = str(raw.get("kind") or "").strip()
    capability_ids = raw.get("execution_capability_ids")
    if (
        schema_version != 1
        or kind != "plan_contains_any_execution_capability"
        or not isinstance(capability_ids, (list, tuple))
        or not capability_ids
        or any(not isinstance(item, str) or not item.strip() for item in capability_ids)
    ):
        raise SemanticCompletionError("BOOLEAN_PREDICATE_APPLICABILITY_INVALID")
    declared = tuple(sorted({item.strip() for item in capability_ids}))
    matching = sorted({
        str(step.get("step_id") or "")
        for step in plan_state.get("steps", ()) or ()
        if isinstance(step, Mapping)
        and str(
            step.get("execution_capability_id")
            or step.get("capability_id")
            or ""
        ).strip()
        in declared
        and str(step.get("step_id") or "")
    })
    return bool(matching), {
        "schema_version": 1,
        "kind": kind,
        "execution_capability_ids": list(declared),
        "matched_step_ids": matching,
    }


def evaluate_boolean_predicates(
    *,
    plan_state: Mapping[str, Any],
    validation: Mapping[str, Any],
    execution_evidence: Mapping[str, Any] | None = None,
) -> list[Dict[str, Any]]:
    """Evaluate declared boolean predicates using typed evidence and parsers."""

    scope = completion_scope(plan_state, validation)
    try:
        step = _current_step(plan_state, scope["step_id"])
        effect = canonical_step_effect(step)
    except SemanticCompletionError as exc:
        return _step_effect_contract_error(exc.code)
    except StepEffectProtocolError as exc:
        return _step_effect_contract_error(
            str(getattr(exc, "code", "STEP_EFFECT_CONTRACT_INVALID"))
        )
    provenance = plan_state.get("provenance_state")
    provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
    attempt = dict(
        (provenance.get("attempts") or {}).get(scope["attempt_id"]) or {}
    )
    observations = _observation_map(
        execution_evidence or {},
        attempt.get("effect_evidence") or {},
        validation.get("effect_results") or {},
    )
    input_paths = _task_input_paths(plan_state)
    results: list[Dict[str, Any]] = []
    for declaration in effect.get("produces_observations", ()) or ():
        if not isinstance(declaration, Mapping):
            continue
        predicate = declaration.get("predicate")
        if not isinstance(predicate, Mapping):
            continue
        observation_id = str(declaration.get("identity") or "").strip()
        predicate_id = str(predicate.get("predicate_id") or "").strip()
        operator = str(predicate.get("operator") or "").strip().lower()
        expected = predicate.get("expected")
        failure_disposition = str(
            predicate.get("failure_disposition") or "correction_required"
        ).strip().lower()
        missing_disposition = str(
            predicate.get("missing_disposition") or "inspection_required"
        ).strip().lower()
        parser_id = str(predicate.get("parser_id") or "").strip()
        applicable = True
        applicability_receipt: Dict[str, Any] = {}
        contract_error = ""
        if not observation_id or not predicate_id:
            contract_error = "BOOLEAN_PREDICATE_ID_REQUIRED"
        elif operator != "boolean_equals" or not isinstance(expected, bool):
            contract_error = "BOOLEAN_PREDICATE_SCHEMA_INVALID"
        elif failure_disposition not in NON_COMMIT_DISPOSITIONS:
            contract_error = "BOOLEAN_PREDICATE_FAILURE_DISPOSITION_INVALID"
        elif missing_disposition not in NON_COMMIT_DISPOSITIONS:
            contract_error = "BOOLEAN_PREDICATE_MISSING_DISPOSITION_INVALID"
        else:
            try:
                applicable, applicability_receipt = _predicate_applicability(
                    predicate, plan_state
                )
            except SemanticCompletionError as exc:
                contract_error = exc.code
        observed = observations.get(observation_id)
        claimed_actual = observed.get("value") if isinstance(observed, Mapping) else None
        authoritative_actual: Any = claimed_actual
        parser_receipt: Dict[str, Any] = {}
        status = "passed"
        reason_code = "BOOLEAN_PREDICATE_SATISFIED"
        disposition = COMPLETION_ACCEPTED
        if contract_error:
            status = "contract_error"
            reason_code = contract_error
            disposition = "plan_revision_required"
        elif parser_id:
            if parser_id != CONTIG_PARSER_ID:
                status = "contract_error"
                reason_code = "PREDICATE_PARSER_UNREGISTERED"
                disposition = "plan_revision_required"
            else:
                try:
                    parser_receipt = build_contig_compatibility_receipt(input_paths)
                    authoritative_actual = parser_receipt["value"]
                except ContigParserError as exc:
                    status = "incomplete"
                    reason_code = exc.code
                    disposition = missing_disposition
        if status == "passed" and observed is None:
            status = "incomplete"
            reason_code = "BOOLEAN_PREDICATE_EVIDENCE_MISSING"
            disposition = missing_disposition
        elif status == "passed" and not isinstance(claimed_actual, bool):
            status = "failed"
            reason_code = "BOOLEAN_PREDICATE_CLAIM_TYPE_INVALID"
            disposition = failure_disposition
        elif status == "passed" and not isinstance(authoritative_actual, bool):
            status = "incomplete"
            reason_code = "BOOLEAN_PREDICATE_EVIDENCE_MISSING"
            disposition = missing_disposition
        elif status == "passed" and parser_id and isinstance(claimed_actual, bool) and (
            claimed_actual is not authoritative_actual
        ):
            status = "failed"
            reason_code = "PREDICATE_CLAIM_MISMATCH"
            disposition = failure_disposition
        elif status == "passed" and applicable and authoritative_actual is not expected:
            status = "failed"
            reason_code = "BOOLEAN_PREDICATE_NOT_SATISFIED"
            disposition = failure_disposition
        elif status == "passed" and not applicable:
            reason_code = "BOOLEAN_PREDICATE_NOT_APPLICABLE"
        body: Dict[str, Any] = {
            "schema_version": 1,
            "predicate_id": predicate_id or "<missing>",
            "observation_id": observation_id or "<missing>",
            "operator": operator or "<missing>",
            "expected": expected,
            "claimed_actual": claimed_actual,
            "authoritative_actual": authoritative_actual,
            "status": status,
            "passed": status == "passed",
            "reason_code": reason_code,
            "disposition": disposition,
            "evaluator_id": SEMANTIC_COMPLETION_EVALUATOR_ID,
            "parser_id": parser_id,
            "parser_receipt": parser_receipt,
            "applicable": applicable,
            "applicability_receipt": applicability_receipt,
        }
        body["result_id"] = _predicate_result_id(body)
        results.append(body)
    return results


def evaluate_workspace_mutation_receipts(
    *,
    plan_state: Mapping[str, Any],
    validation: Mapping[str, Any],
    execution_evidence: Mapping[str, Any] | None = None,
) -> list[Dict[str, Any]]:
    """Validate controller-minted workspace receipts against their intents.

    A receipt identifier is only a reference.  It is not proof by itself: the
    producer, attested bit, immutable hash, mutation identity, and complete
    attempt scope must all validate before the mutation can support completion.
    """

    scope = completion_scope(plan_state, validation)
    try:
        step = _current_step(plan_state, scope["step_id"])
        effect = canonical_step_effect(step)
    except SemanticCompletionError as exc:
        return _step_effect_contract_error(exc.code)
    except StepEffectProtocolError as exc:
        return _step_effect_contract_error(
            str(getattr(exc, "code", "STEP_EFFECT_CONTRACT_INVALID"))
        )
    declarations = [
        dict(item) if isinstance(item, Mapping) else {"identity": str(item or "")}
        for item in effect.get("workspace_mutations", ()) or ()
    ]
    if not declarations:
        return []

    execution = (
        dict(execution_evidence)
        if isinstance(execution_evidence, Mapping)
        else {}
    )
    effect_evidence = execution.get("effect_evidence")
    effect_evidence = (
        dict(effect_evidence) if isinstance(effect_evidence, Mapping) else {}
    )
    raw_receipts = (
        effect_evidence.get("workspace_mutations")
        if "workspace_mutations" in effect_evidence
        else execution.get("workspace_mutations")
    )
    receipts = [
        dict(item)
        for item in raw_receipts or ()
        if isinstance(item, Mapping)
    ]
    journal = execution.get("workspace_mutation_attestation")
    journal = dict(journal) if isinstance(journal, Mapping) else {}
    intents = {
        str(item.get("intent_id") or ""): dict(item)
        for item in journal.get("intents", ()) or ()
        if isinstance(item, Mapping) and str(item.get("intent_id") or "")
    }
    expected_scope = {
        "task_id": scope["task_id"],
        "plan_id": scope["plan_id"],
        "plan_revision": int(plan_state.get("plan_revision") or 0),
        "step_id": scope["step_id"],
        "action_id": scope["action_id"],
        "attempt_id": scope["attempt_id"],
    }
    results: list[Dict[str, Any]] = []
    for declaration in declarations:
        identity = str(
            declaration.get("identity")
            or declaration.get("workspace_ref")
            or declaration.get("canonical_resource_id")
            or ""
        ).strip()
        mutation = str(declaration.get("mutation") or "").strip()
        predicate_id = f"workspace_mutation_attestation:{identity}:{mutation}"
        contract_error = ""
        if not identity or not mutation:
            contract_error = "WORKSPACE_MUTATION_CONTRACT_INVALID"
        candidates = [
            receipt
            for receipt in receipts
            if str(
                receipt.get("canonical_resource_id")
                or receipt.get("workspace_ref")
                or receipt.get("identity")
                or ""
            ).strip()
            == identity
            and str(receipt.get("mutation") or "").strip() == mutation
        ]
        receipt = candidates[0] if len(candidates) == 1 else {}
        intent = intents.get(str(receipt.get("intent_id") or ""), {})
        receipt_check: Dict[str, Any] = {}
        scope_mismatches: Dict[str, Any] = {}
        status = "passed"
        reason_code = "WORKSPACE_MUTATION_RECEIPT_VALID"
        disposition = COMPLETION_ACCEPTED
        if contract_error:
            status = "contract_error"
            reason_code = contract_error
            disposition = "plan_revision_required"
        elif not candidates:
            status = "incomplete"
            reason_code = "WORKSPACE_MUTATION_RECEIPT_MISSING"
            disposition = "inspection_required"
        elif len(candidates) != 1:
            status = "failed"
            reason_code = "WORKSPACE_MUTATION_RECEIPT_AMBIGUOUS"
            disposition = "correction_required"
        elif not intent:
            status = "failed"
            reason_code = "WORKSPACE_MUTATION_INTENT_MISSING"
            disposition = "correction_required"
        else:
            receipt_check = validate_runtime_mutation_receipt(receipt, intent=intent)
            scope_mismatches = {
                field: {
                    "expected": expected,
                    "observed": receipt.get(field),
                }
                for field, expected in expected_scope.items()
                if receipt.get(field) != expected
            }
            try:
                binding_revision = int(receipt.get("binding_revision") or 0)
            except (TypeError, ValueError):
                binding_revision = 0
            receipt_shape_valid = bool(
                str(receipt.get("receipt_id") or "").startswith("workspace-receipt-")
                and str(receipt.get("immutable_hash") or "").startswith("sha256:")
                and binding_revision > 0
            )
            if (
                receipt_check.get("valid") is not True
                or scope_mismatches
                or not receipt_shape_valid
            ):
                status = "failed"
                reason_code = "WORKSPACE_MUTATION_RECEIPT_INVALID"
                disposition = "correction_required"
        runtime_access_validation = receipt.get(
            "runtime_access_evidence_validation"
        )
        receipt_summary = {
            "receipt_id": str(receipt.get("receipt_id") or ""),
            "producer": str(receipt.get("producer") or ""),
            "attested": receipt.get("attested") is True,
            "immutable_hash": str(receipt.get("immutable_hash") or ""),
            "intent_id": str(receipt.get("intent_id") or ""),
            "canonical_resource_id": str(
                receipt.get("canonical_resource_id") or ""
            ),
            "binding_revision": receipt.get("binding_revision"),
            "formal_concrete_path": str(
                receipt.get("formal_concrete_path") or ""
            ),
            "resource_kind": str(receipt.get("resource_kind") or ""),
            "plan_handle": str(receipt.get("plan_handle") or ""),
            "operation": str(receipt.get("operation") or ""),
            "mutation": str(receipt.get("mutation") or ""),
            "attestation_status": str(
                receipt.get("attestation_status") or ""
            ),
            "unknown_reason": str(receipt.get("unknown_reason") or ""),
            "runtime_access_evidence_validation": (
                deepcopy(dict(runtime_access_validation))
                if isinstance(runtime_access_validation, Mapping)
                else {}
            ),
            "receipt_validation": receipt_check,
            "scope_mismatches": scope_mismatches,
        }
        body: Dict[str, Any] = {
            "schema_version": 1,
            "predicate_id": predicate_id,
            "observation_id": identity or "<workspace_mutation>",
            "operator": "runtime_attestation_valid",
            "expected": True,
            "claimed_actual": receipt.get("attested") if receipt else None,
            "authoritative_actual": status == "passed",
            "status": status,
            "passed": status == "passed",
            "reason_code": reason_code,
            "disposition": disposition,
            "evaluator_id": SEMANTIC_COMPLETION_EVALUATOR_ID,
            "parser_id": "biomind.runtime_attester.v1",
            "parser_receipt": receipt_summary,
        }
        body["result_id"] = _predicate_result_id(body)
        results.append(body)
    return results


_GATE_FAILURE_DISPOSITIONS = {
    "step_effect_contract_invalid": "plan_revision_required",
    "step_effect_incomplete": "inspection_required",
    "step_effect_evidence_mismatch": "correction_required",
    "step_provenance_invalidated": "inspection_required",
    "step_provenance_not_valid": "correction_required",
}


def evaluate_completion_gate_facts(
    completion_gate: Mapping[str, Any] | None,
) -> list[Dict[str, Any]]:
    """Project a pure CompletionGate result into the shared fact report."""

    if completion_gate in (None, {}):
        return []
    if not isinstance(completion_gate, Mapping) or not isinstance(
        completion_gate.get("passed"), bool
    ):
        return _step_effect_contract_error("COMPLETION_GATE_RESULT_INVALID")
    gate = deepcopy(dict(completion_gate))
    classification = str(gate.get("classification") or "gate_failed").strip()
    passed = gate["passed"] is True
    disposition = (
        COMPLETION_ACCEPTED
        if passed
        else _GATE_FAILURE_DISPOSITIONS.get(
            classification, "inspection_required"
        )
    )
    body: Dict[str, Any] = {
        "schema_version": 1,
        "predicate_id": "completion_gate:" + classification,
        "observation_id": "completion_gate",
        "operator": "completion_gate_passed",
        "expected": True,
        "claimed_actual": gate["passed"],
        "authoritative_actual": gate["passed"],
        "status": "passed" if passed else "failed",
        "passed": passed,
        "reason_code": (
            "COMPLETION_GATE_SATISFIED"
            if passed
            else "COMPLETION_GATE_REJECTED:" + classification
        ),
        "disposition": disposition,
        "evaluator_id": SEMANTIC_COMPLETION_EVALUATOR_ID,
        "parser_id": "biomind.completion_gate.v1",
        "parser_receipt": gate,
    }
    if observation_review_required(gate):
        body.update(severity="warning", status="warning")
    body["result_id"] = _predicate_result_id(body)
    return [body]


def observation_review_required(gate: Mapping[str, Any]) -> bool:
    """Only the gate owner can classify missing plain observations for review."""
    return gate.get("classification") == "step_observation_review_required"


def _blocking_predicate(row: Mapping[str, Any]) -> bool:
    return row.get("passed") is not True and not (
        row.get("severity") == "warning"
        and row.get("operator") == "completion_gate_passed"
        and observation_review_required(row.get("parser_receipt") or {})
    )


def _deterministic_decision(
    results: Sequence[Mapping[str, Any]],
    *,
    scope: Mapping[str, Any],
    report_id: str = "",
) -> Optional[SemanticDecision]:
    failing = [row for row in results if _blocking_predicate(row)]
    if not failing:
        return None
    priority = (
        "plan_revision_required",
        "user_information_required",
        "inspection_required",
        "correction_required",
        "assessment_pending",
    )
    dispositions = {str(row.get("disposition") or "") for row in failing}
    disposition = next((item for item in priority if item in dispositions), "assessment_pending")
    return SemanticDecision.create(
        scope=scope,
        source="deterministic_predicate",
        reviewed_report_id=report_id,
        assessment="CONCERN",
        disposition=disposition,
        concerns=tuple(str(row.get("reason_code") or "PREDICATE_FAILED") for row in failing),
        suggestions=(),
        confidence=1.0,
        parse_status="valid",
    )


def _validation_fact_projection(validation: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the stable, pre-review part of ValidationEvidence.

    The report identity must already exist when the reviewer receives it, so it
    deliberately excludes the review response and volatile elapsed time.  The
    final report still carries hash-validated reviewer/effective decisions and
    the consumption token binds the effective decision at CommitStep.
    """

    excluded = {
        # ValidationEvidence may be normalized once before the provider call
        # and again after it returns.  Its observation timestamp is telemetry,
        # not a completion fact; including it makes the report binding change
        # merely because the call crossed a wall-clock second.
        "created_at",
        "llm_advisory",
        "llm_advisory_conflict",
        "scientific_checks",
        "validation_duration",
    }
    return {
        str(key): deepcopy(value)
        for key, value in validation.items()
        if str(key) not in excluded
    }


_EFFECT_FIELDS = (
    "produces_artifacts",
    "produces_observations",
    "produces_bindings",
    "workspace_mutations",
    "explicit_side_effects",
)


def _effect_identity(value: Any) -> str:
    if not isinstance(value, Mapping):
        return str(value or "").strip()
    for key in (
        "identity",
        "observation_id",
        "artifact_id",
        "binding_id",
        "workspace_ref",
        "canonical_resource_id",
        "effect_id",
        "result_id",
    ):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return ""


def _derive_report_facts(
    *,
    plan_state: Mapping[str, Any],
    validation: Mapping[str, Any],
    execution_evidence: Mapping[str, Any] | None,
    predicate_results: Sequence[Mapping[str, Any]],
    completion_gate: Mapping[str, Any],
    semantic_review_required: bool,
) -> Dict[str, Any]:
    execution = (
        dict(execution_evidence)
        if isinstance(execution_evidence, Mapping)
        else {}
    )
    returncode = validation.get("returncode_check")
    returncode = dict(returncode) if isinstance(returncode, Mapping) else {}
    process_outcome = {
        "validation_status": str(validation.get("status") or ""),
        "returncode": returncode.get("returncode"),
        "returncode_passed": returncode.get("passed") is True,
        "evidence_complete": validation.get("evidence_complete") is True,
        "command_role": str(
            validation.get("command_role")
            or execution.get("command_role")
            or ""
        ),
        "execution_status": str(execution.get("execution_status") or ""),
        "timed_out": bool(execution.get("timed_out")),
        "oom_detected": bool(execution.get("oom_detected")),
        "process_signal": execution.get("process_signal"),
        "executor_exception": str(execution.get("executor_exception") or ""),
    }

    declared: Dict[str, list[str]] = {key: [] for key in _EFFECT_FIELDS}
    step: Mapping[str, Any] = {}
    try:
        scope = completion_scope(plan_state, validation)
        step = _current_step(plan_state, scope["step_id"])
        effect = canonical_step_effect(step)
    except (SemanticCompletionError, StepEffectProtocolError):
        effect = {}
    for key in _EFFECT_FIELDS:
        declared[key] = [
            identity
            for identity in (
                _effect_identity(item) for item in effect.get(key, ()) or ()
            )
            if identity
        ]
    observed_envelope = canonical_execution_effect_evidence([
        execution,
        validation.get("effect_results") or {},
    ])
    collection = artifact_collection_facts(execution)
    path_links = {
        str(item["path"]): str(item["identity"])
        for item in collection["accepted"]
        if item.get("path") and item.get("identity")
    }
    identity_paths: Dict[str, set[str]] = {}
    for path, identity in path_links.items():
        identity_paths.setdefault(identity, set()).add(path)
    known_paths = {
        str(item["path"])
        for item in [*collection["accepted"], *collection["observed_candidates"]]
        if item.get("path")
    }
    observed: Dict[str, list[Dict[str, Any]]] = {key: [] for key in _EFFECT_FIELDS}
    for key in _EFFECT_FIELDS:
        if key == "produces_bindings":
            candidates = candidate_binding_facts(plan_state, validation)
            if candidates:
                observed[key] = candidates
                continue
        for raw in observed_envelope.get(key, ()) or ():
            item = dict(raw) if isinstance(raw, Mapping) else {"identity": str(raw)}
            row: Dict[str, Any] = {"identity": _effect_identity(item)}
            if key == "produces_artifacts":
                path = str(item.get("path") or item.get("observed_path") or "")
                # The legacy canonical effect reader accepts path as an ID
                # alias and consequently removes it from typed rows. Restore
                # only the exact link preserved in the collection record.
                linked_paths = identity_paths.get(row["identity"], set())
                if not path and len(linked_paths) == 1:
                    path = next(iter(linked_paths))
                if not path and row["identity"] not in declared[key] and (
                    row["identity"] in known_paths or not isinstance(raw, Mapping)
                ):
                    path = row["identity"]
                    row["identity"] = path_links.get(path, "")
                if path:
                    row["path"] = path
                row["identity_status"] = "recorded" if row["identity"] else "unknown"
                # Files are checked by collection/provenance, not by the
                # observation value protocol. A path-only historical record
                # is unknown identity evidence, never a missing-value error.
                if (row["identity"] or path) and row not in observed[key]:
                    observed[key].append(row)
                continue
            if key == "workspace_mutations":
                row.update({name: deepcopy(item[name]) for name in (
                    "receipt_id", "receipt_ref", "attestation_status", "status",
                    "postcondition_satisfied", "workspace_id", "binding_revision",
                    "before_evidence", "after_evidence", "postconditions",
                ) if name in item})
                row["evidence_kind"] = "workspace_attestation"
                observed[key].append(row)
                continue
            if "value" in item:
                row.update({
                    "value_present": True,
                    "value": deepcopy(item.get("value")),
                    "value_type": type(item.get("value")).__name__,
                })
            else:
                row["value_present"] = False
            if row["identity"]:
                observed[key].append(row)
    from agent_core.evidence_projection import revalidation_inspection_facts
    effect_presence = {
        "declared": declared,
        "observed": observed,
        "missing": deepcopy(dict(completion_gate.get("missing_effects") or {})),
        "unexpected": deepcopy(dict(completion_gate.get("unexpected_effects") or {})),
        "invalid": deepcopy(dict(completion_gate.get("invalid_effects") or {})),
        "file_collection": collection,
        "formal_registration": artifact_registration_facts(
            plan_state, logical_ids=declared["produces_artifacts"],
            provenance_ids=list(step.get("output_artifact_ids") or []),
        ),
        "completion_verification_source": "completion_gate_and_predicate_results",
        "declared_dependency_facts": committed_dependency_facts(plan_state, step),
        "revalidation_inspection_facts": revalidation_inspection_facts(plan_state, validation),
        "workspace_contracts": deepcopy(effect.get("workspace_mutations") or []),
        "candidate_binding_semantics": "Candidate value presence is not Commit acceptance or biological assembly identity.",
    }
    process_outcome["runtime_observations"] = runtime_observation_facts(
        dict(plan_state.get("task_state") or {}),
        task_id=str(plan_state.get("task_id") or ""),
        skill_ids=list(step.get("selected_skill_ids") or step.get("skill_ids") or []),
    )

    receipt_findings: list[Dict[str, Any]] = []
    findings: list[Dict[str, Any]] = []
    validation_id = str(validation.get("validation_id") or "")
    for raw in predicate_results:
        row = dict(raw)
        result_id = str(row.get("result_id") or "")
        parser_receipt = (
            dict(row.get("parser_receipt") or {})
            if isinstance(row.get("parser_receipt"), Mapping)
            else {}
        )
        evidence_refs = list(dict.fromkeys([
            item
            for item in (
                validation_id,
                result_id,
                str(parser_receipt.get("receipt_id") or ""),
            )
            if item
        ]))
        nature = "explicit_predicate"
        if row.get("operator") == "runtime_attestation_valid":
            nature = "binding_and_receipt"
        elif row.get("operator") == "completion_gate_passed":
            nature = "completion_gate"
        finding = {
            "issue_id": result_id,
            "nature": nature,
            "code": str(row.get("reason_code") or ""),
            "contract_source": str(row.get("predicate_id") or ""),
            "actual": deepcopy(row.get("authoritative_actual")),
            "expected": deepcopy(row.get("expected")),
            "evidence_refs": evidence_refs,
            "affected_resource": str(row.get("observation_id") or ""),
            "affected_field": "passed" if nature == "completion_gate" else "value",
            "status": str(row.get("status") or ""),
            "disposition": str(row.get("disposition") or ""),
            "confidence_or_unknown_reason": (
                1.0
                if str(row.get("status") or "") in {"passed", "failed"}
                else str(row.get("reason_code") or "unknown")
            ),
        }
        if row.get("severity") == "warning":
            finding["severity"] = "warning"
        findings.append(finding)
        if nature == "binding_and_receipt":
            receipt_findings.append({
                "issue_id": result_id,
                "status": finding["status"],
                "code": finding["code"],
                "affected_resource": finding["affected_resource"],
                "receipt": parser_receipt,
            })
    allowed_issue_ids = list(dict.fromkeys([
        validation_id,
        *[str(item.get("issue_id") or "") for item in findings],
    ]))
    required_issue_ids = [
        str(item.get("issue_id") or "")
        for item in findings
        if str(item.get("status") or "") != "passed"
    ]
    semantic_review_requirements = {
        "required": bool(semantic_review_required),
        "policy_source": (
            "explicit_validation_level"
            if semantic_review_required
            else "no_explicit_review_policy"
        ),
        "required_issue_ids": required_issue_ids,
        "allowed_issue_ids": allowed_issue_ids,
        "decision_schema": "biomind.semantic_decision.v1",
        "review_input_status": (
            "assessment_pending" if semantic_review_required else "not_required"
        ),
        "commit_eligible": False,
        "commit_authority": "commit_step_only",
    }
    return {
        "process_outcome": process_outcome,
        "effect_presence": effect_presence,
        "binding_and_receipt_findings": receipt_findings,
        "semantic_review_requirements": semantic_review_requirements,
        "findings": findings,
    }


def _completion_report_id(
    *,
    scope: Mapping[str, Any],
    validation_digest: str,
    predicate_results: Sequence[Mapping[str, Any]],
    completion_gate: Mapping[str, Any],
    process_outcome: Mapping[str, Any],
    effect_presence: Mapping[str, Any],
    binding_and_receipt_findings: Sequence[Mapping[str, Any]],
    semantic_review_requirements: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
) -> str:
    facts = {
        "schema_version": SEMANTIC_COMPLETION_SCHEMA_VERSION,
        "evaluator_id": SEMANTIC_COMPLETION_EVALUATOR_ID,
        "scope": _validate_scope(scope),
        "validation_digest": str(validation_digest),
        "predicate_results": [deepcopy(dict(item)) for item in predicate_results],
        "completion_gate": deepcopy(dict(completion_gate)),
        "process_outcome": deepcopy(dict(process_outcome)),
        "effect_presence": deepcopy(dict(effect_presence)),
        "binding_and_receipt_findings": [
            deepcopy(dict(item)) for item in binding_and_receipt_findings
        ],
        "semantic_review_requirements": deepcopy(
            dict(semantic_review_requirements)
        ),
        "findings": [deepcopy(dict(item)) for item in findings],
    }
    return "completion-report-" + _digest(facts).removeprefix("sha256:")


@dataclass(frozen=True)
class CompletionReport:
    schema_version: int
    report_id: str
    evaluator_id: str
    scope: Mapping[str, Any]
    validation_digest: str
    completion_gate: Mapping[str, Any]
    process_outcome: Mapping[str, Any]
    effect_presence: Mapping[str, Any]
    binding_and_receipt_findings: tuple[Mapping[str, Any], ...]
    semantic_review_requirements: Mapping[str, Any]
    findings: tuple[Mapping[str, Any], ...]
    predicate_results: tuple[Mapping[str, Any], ...]
    reviewer_decision: SemanticDecision
    effective_decision: SemanticDecision
    deterministic_veto: bool
    effective_disposition: str
    consumption_token: str

    @classmethod
    def create(
        cls,
        *,
        scope: Mapping[str, Any],
        validation: Mapping[str, Any],
        predicate_results: Sequence[Mapping[str, Any]],
        reviewer: SemanticDecision,
        effective: SemanticDecision,
        completion_gate: Mapping[str, Any] | None = None,
        process_outcome: Mapping[str, Any] | None = None,
        effect_presence: Mapping[str, Any] | None = None,
        binding_and_receipt_findings: Sequence[Mapping[str, Any]] = (),
        semantic_review_requirements: Mapping[str, Any] | None = None,
        findings: Sequence[Mapping[str, Any]] = (),
        validation_digest_override: str = "",
    ) -> "CompletionReport":
        normalized_scope = _validate_scope(scope)
        if dict(reviewer.scope) != normalized_scope or dict(effective.scope) != normalized_scope:
            raise SemanticCompletionError("SEMANTIC_DECISION_SCOPE_MISMATCH")
        rows = tuple(deepcopy(dict(item)) for item in predicate_results)
        gate = deepcopy(dict(completion_gate or {}))
        process = deepcopy(dict(process_outcome or {}))
        presence = deepcopy(dict(effect_presence or {}))
        receipt_findings = tuple(
            deepcopy(dict(item)) for item in binding_and_receipt_findings
        )
        review_requirements = deepcopy(dict(semantic_review_requirements or {}))
        normalized_findings = tuple(deepcopy(dict(item)) for item in findings)
        deterministic_veto = any(_blocking_predicate(item) for item in rows)
        validation_digest = str(validation_digest_override or "").strip()
        if validation_digest:
            if not validation_digest.startswith("sha256:"):
                raise SemanticCompletionError(
                    "COMPLETION_REPORT_VALIDATION_DIGEST_INVALID"
                )
        else:
            validation_digest = _digest(_validation_fact_projection(validation))
        report_id = _completion_report_id(
            scope=normalized_scope,
            validation_digest=validation_digest,
            predicate_results=rows,
            completion_gate=gate,
            process_outcome=process,
            effect_presence=presence,
            binding_and_receipt_findings=receipt_findings,
            semantic_review_requirements=review_requirements,
            findings=normalized_findings,
        )
        if reviewer.reviewed_report_id and reviewer.reviewed_report_id != report_id:
            raise SemanticCompletionError("SEMANTIC_DECISION_REPORT_ID_MISMATCH")
        deterministic = _deterministic_decision(
            rows, scope=normalized_scope, report_id=report_id
        )
        if deterministic is None:
            if effective.decision_id != reviewer.decision_id:
                raise SemanticCompletionError(
                    "SEMANTIC_EFFECTIVE_DECISION_MISMATCH"
                )
        elif (
            effective.source != "deterministic_predicate"
            or effective.assessment != deterministic.assessment
            or effective.disposition != deterministic.disposition
        ):
            raise SemanticCompletionError("SEMANTIC_EFFECTIVE_DECISION_MISMATCH")
        body = {
            "schema_version": SEMANTIC_COMPLETION_SCHEMA_VERSION,
            "evaluator_id": SEMANTIC_COMPLETION_EVALUATOR_ID,
            "scope": normalized_scope,
            "validation_digest": validation_digest,
            "completion_gate": gate,
            "process_outcome": process,
            "effect_presence": presence,
            "binding_and_receipt_findings": list(receipt_findings),
            "semantic_review_requirements": review_requirements,
            "findings": list(normalized_findings),
            "predicate_results": list(rows),
            "reviewer_decision": reviewer.to_dict(),
            "effective_decision": effective.to_dict(),
            "deterministic_veto": deterministic_veto,
            "effective_disposition": effective.disposition,
        }
        consumption_token = "completion-consumption-" + _digest({
            "report_id": report_id,
            "decision_id": effective.decision_id,
            "scope": normalized_scope,
            "consumer": "commit_step",
        }).removeprefix("sha256:")
        return cls(
            schema_version=SEMANTIC_COMPLETION_SCHEMA_VERSION,
            report_id=report_id,
            evaluator_id=SEMANTIC_COMPLETION_EVALUATOR_ID,
            scope=MappingProxyType(normalized_scope),
            validation_digest=body["validation_digest"],
            completion_gate=MappingProxyType(gate),
            process_outcome=MappingProxyType(process),
            effect_presence=MappingProxyType(presence),
            binding_and_receipt_findings=tuple(
                MappingProxyType(item) for item in receipt_findings
            ),
            semantic_review_requirements=MappingProxyType(review_requirements),
            findings=tuple(
                MappingProxyType(item) for item in normalized_findings
            ),
            predicate_results=tuple(MappingProxyType(row) for row in rows),
            reviewer_decision=reviewer,
            effective_decision=effective,
            deterministic_veto=deterministic_veto,
            effective_disposition=effective.disposition,
            consumption_token=consumption_token,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CompletionReport":
        if not isinstance(value, Mapping):
            raise SemanticCompletionError("COMPLETION_REPORT_OBJECT_REQUIRED")
        allowed = {
            "schema_version",
            "report_id",
            "evaluator_id",
            "scope",
            "validation_digest",
            "completion_gate",
            "process_outcome",
            "effect_presence",
            "binding_and_receipt_findings",
            "semantic_review_requirements",
            "findings",
            "predicate_results",
            "reviewer_decision",
            "effective_decision",
            "deterministic_veto",
            "effective_disposition",
            "consumption_token",
        }
        if set(value) - allowed:
            raise SemanticCompletionError(
                "COMPLETION_REPORT_FIELD_UNKNOWN", ",".join(sorted(set(value) - allowed))
            )
        missing = sorted(allowed - set(value))
        if missing:
            raise SemanticCompletionError(
                "COMPLETION_REPORT_FIELD_REQUIRED", ",".join(missing)
            )
        try:
            schema_version = int(value.get("schema_version") or 0)
        except (TypeError, ValueError) as exc:
            raise SemanticCompletionError("COMPLETION_REPORT_SCHEMA_UNSUPPORTED") from exc
        if schema_version != SEMANTIC_COMPLETION_SCHEMA_VERSION:
            raise SemanticCompletionError("COMPLETION_REPORT_SCHEMA_UNSUPPORTED")
        if str(value.get("evaluator_id") or "") != SEMANTIC_COMPLETION_EVALUATOR_ID:
            raise SemanticCompletionError("COMPLETION_REPORT_EVALUATOR_INVALID")
        rows = value.get("predicate_results")
        if not isinstance(rows, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in rows
        ):
            raise SemanticCompletionError("COMPLETION_REPORT_PREDICATES_INVALID")
        gate = value.get("completion_gate")
        if not isinstance(gate, Mapping):
            raise SemanticCompletionError("COMPLETION_REPORT_GATE_INVALID")
        process = value.get("process_outcome")
        presence = value.get("effect_presence")
        receipt_findings = value.get("binding_and_receipt_findings")
        review_requirements = value.get("semantic_review_requirements")
        findings = value.get("findings")
        if not isinstance(process, Mapping):
            raise SemanticCompletionError("COMPLETION_REPORT_PROCESS_OUTCOME_INVALID")
        if not isinstance(presence, Mapping):
            raise SemanticCompletionError("COMPLETION_REPORT_EFFECT_PRESENCE_INVALID")
        if not isinstance(review_requirements, Mapping):
            raise SemanticCompletionError("COMPLETION_REPORT_REVIEW_POLICY_INVALID")
        if not isinstance(receipt_findings, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in receipt_findings
        ):
            raise SemanticCompletionError("COMPLETION_REPORT_RECEIPTS_INVALID")
        if not isinstance(findings, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in findings
        ):
            raise SemanticCompletionError("COMPLETION_REPORT_FINDINGS_INVALID")
        reviewer = SemanticDecision.from_mapping(value.get("reviewer_decision") or {})
        effective = SemanticDecision.from_mapping(value.get("effective_decision") or {})
        validation_digest = str(value.get("validation_digest") or "")
        if not validation_digest.startswith("sha256:"):
            raise SemanticCompletionError("COMPLETION_REPORT_VALIDATION_DIGEST_INVALID")
        created = cls.create(
            scope=value.get("scope") or {},
            validation={},
            completion_gate=dict(gate),
            process_outcome=dict(process),
            effect_presence=dict(presence),
            binding_and_receipt_findings=[dict(item) for item in receipt_findings],
            semantic_review_requirements=dict(review_requirements),
            findings=[dict(item) for item in findings],
            predicate_results=[dict(item) for item in rows],
            reviewer=reviewer,
            effective=effective,
            validation_digest_override=validation_digest,
        )
        report_id = _completion_report_id(
            scope=created.scope,
            validation_digest=validation_digest,
            predicate_results=created.predicate_results,
            completion_gate=created.completion_gate,
            process_outcome=created.process_outcome,
            effect_presence=created.effect_presence,
            binding_and_receipt_findings=created.binding_and_receipt_findings,
            semantic_review_requirements=created.semantic_review_requirements,
            findings=created.findings,
        )
        consumption_token = "completion-consumption-" + _digest({
            "report_id": report_id,
            "decision_id": effective.decision_id,
            "scope": dict(created.scope),
            "consumer": "commit_step",
        }).removeprefix("sha256:")
        if str(value.get("report_id") or "") != report_id:
            raise SemanticCompletionError("COMPLETION_REPORT_HASH_MISMATCH")
        if str(value.get("consumption_token") or "") != consumption_token:
            raise SemanticCompletionError("COMPLETION_REPORT_CONSUMPTION_HASH_MISMATCH")
        if not isinstance(value.get("deterministic_veto"), bool):
            raise SemanticCompletionError("COMPLETION_REPORT_VETO_INVALID")
        if value.get("deterministic_veto") != created.deterministic_veto:
            raise SemanticCompletionError("COMPLETION_REPORT_VETO_MISMATCH")
        if str(value.get("effective_disposition") or "") != effective.disposition:
            raise SemanticCompletionError("COMPLETION_REPORT_DISPOSITION_MISMATCH")
        return cls(
            schema_version=created.schema_version,
            report_id=report_id,
            evaluator_id=created.evaluator_id,
            scope=created.scope,
            validation_digest=validation_digest,
            completion_gate=created.completion_gate,
            process_outcome=created.process_outcome,
            effect_presence=created.effect_presence,
            binding_and_receipt_findings=created.binding_and_receipt_findings,
            semantic_review_requirements=created.semantic_review_requirements,
            findings=created.findings,
            predicate_results=created.predicate_results,
            reviewer_decision=reviewer,
            effective_decision=effective,
            deterministic_veto=created.deterministic_veto,
            effective_disposition=effective.disposition,
            consumption_token=consumption_token,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "evaluator_id": self.evaluator_id,
            "scope": dict(self.scope),
            "validation_digest": self.validation_digest,
            "completion_gate": dict(self.completion_gate),
            "process_outcome": dict(self.process_outcome),
            "effect_presence": dict(self.effect_presence),
            "binding_and_receipt_findings": [
                dict(item) for item in self.binding_and_receipt_findings
            ],
            "semantic_review_requirements": dict(
                self.semantic_review_requirements
            ),
            "findings": [dict(item) for item in self.findings],
            "predicate_results": [dict(item) for item in self.predicate_results],
            "reviewer_decision": self.reviewer_decision.to_dict(),
            "effective_decision": self.effective_decision.to_dict(),
            "deterministic_veto": self.deterministic_veto,
            "effective_disposition": self.effective_disposition,
            "consumption_token": self.consumption_token,
        }


def build_completion_report(
    *,
    plan_state: Mapping[str, Any],
    validation: Mapping[str, Any],
    execution_evidence: Mapping[str, Any] | None = None,
    completion_gate: Mapping[str, Any] | None = None,
    semantic_review_required: Optional[bool] = None,
) -> CompletionReport:
    scope = completion_scope(plan_state, validation)
    scope = _validate_scope(scope)
    predicate_results = evaluate_boolean_predicates(
        plan_state=plan_state,
        validation=validation,
        execution_evidence=execution_evidence,
    )
    predicate_results.extend(evaluate_workspace_mutation_receipts(
        plan_state=plan_state,
        validation=validation,
        execution_evidence=execution_evidence,
    ))
    predicate_results.extend(evaluate_completion_gate_facts(completion_gate))
    predicate_results = list({
        str(item.get("result_id") or _predicate_result_id(item)): dict(item)
        for item in predicate_results
    }.values())
    gate_value = deepcopy(dict(completion_gate or {}))
    review_required = (
        bool(semantic_review_required)
        if semantic_review_required is not None
        else validation.get("llm_advisory") not in (None, {}, "")
    ) or observation_review_required(gate_value)
    report_facts = _derive_report_facts(
        plan_state=plan_state,
        validation=validation,
        execution_evidence=execution_evidence,
        predicate_results=predicate_results,
        completion_gate=gate_value,
        semantic_review_required=review_required,
    )
    validation_digest = _digest(_validation_fact_projection(validation))
    report_id = _completion_report_id(
        scope=scope,
        validation_digest=validation_digest,
        predicate_results=predicate_results,
        completion_gate=gate_value,
        **report_facts,
    )
    review_contract = report_facts["semantic_review_requirements"]
    allowed_issue_ids = tuple(review_contract["allowed_issue_ids"])
    required_issue_ids = tuple(review_contract["required_issue_ids"])
    advisory = validation.get("llm_advisory")
    # A persisted review request/response advances PlanState's CAS generation.
    # Rebind only a locally receipted valid response whose *complete* report
    # facts hash is unchanged except that bookkeeping generation. Never edit
    # the provider response or relax old-report / stale-evidence validation.
    if isinstance(advisory, Mapping) and advisory.get("parse_status") == "valid":
        receipt = advisory.get("review_policy_receipt") or {}
        assessment = (plan_state.get("task_state") or {}).get("semantic_review_receipts", {}).get(
            receipt.get("assessment_id"), {})
        if assessment.get("producer") == REVIEWER_POLICY_VERSION:
            for request in (assessment.get("requests") or {}).values():
                original_scope = request.get("report_scope") or {}
                if (request.get("status") != "valid"
                        or request.get("decision") != {k:v for k,v in advisory.items() if k not in {"review_policy_receipt", "review_error"}}
                        or any(original_scope.get(k) != scope.get(k) for k in scope if k != "state_generation")):
                    continue
                original_id = _completion_report_id(scope=original_scope,
                    validation_digest=validation_digest, predicate_results=predicate_results,
                    completion_gate=gate_value, **report_facts)
                if original_id == request.get("report_id") == advisory.get("report_id"):
                    advisory = {**dict(advisory), "report_id": report_id}
                    break
    reviewer = reviewer_decision(
        advisory,
        scope=scope,
        report_id=report_id,
        allowed_issue_ids=allowed_issue_ids,
        required_issue_ids=required_issue_ids,
        review_required=review_required,
    )
    effective = _deterministic_decision(
        predicate_results, scope=scope, report_id=report_id
    ) or reviewer
    return CompletionReport.create(
        scope=scope,
        validation=validation,
        completion_gate=gate_value,
        **report_facts,
        predicate_results=predicate_results,
        reviewer=reviewer,
        effective=effective,
        validation_digest_override=validation_digest,
    )


__all__ = [
    "COMPLETION_ACCEPTED",
    "NON_COMMIT_DISPOSITIONS",
    "REVIEW_ASSESSMENTS",
    "SEMANTIC_COMPLETION_EVALUATOR_ID",
    "SEMANTIC_COMPLETION_SCHEMA_VERSION",
    "SEMANTIC_DISPOSITIONS",
    "CompletionReport",
    "SemanticCompletionError",
    "SemanticDecision",
    "build_completion_report",
    "completion_scope",
    "evaluate_boolean_predicates",
    "evaluate_workspace_mutation_receipts",
    "reviewer_decision",
    "validate_reviewer_payload",
]

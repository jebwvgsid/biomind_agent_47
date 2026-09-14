"""Trusted receipts for pre-execution Generate candidate rejection.

The provider proposes an action.  It never creates this receipt.  A Controller
validator issues the receipt from the exact candidate and validation facts,
persists the canonical body in the existing content-addressed state-contract
store, and the schema-2 adapter verifies the stored object before re-entry.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, Mapping, Sequence

from agent_core.state_contract_store import StateContractIntegrityError, StateContractStore


CANDIDATE_REPAIR_RECEIPT_SCHEMA_VERSION = 1
CANDIDATE_REPAIR_RECEIPT_PRODUCER = "biomind.generate_candidate_admission.v1"
CANDIDATE_REPAIR_RECEIPT_OBJECT_TYPE = "candidate_repair_receipt"


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def candidate_repair_fingerprint(candidate: Any) -> str:
    """Identify the complete rejected candidate, including operation/bindings."""

    return _digest({"candidate": deepcopy(candidate)})


def candidate_progress_view(candidate: Any) -> Any:
    """Comparison only: unwrap our diagnostic envelope, never promote an action.

    The complete candidate fingerprint/receipt remains untouched. Only identities
    attached by the execution framework are omitted, and only at the action root;
    resource IDs, evidence versions, parameters and nested values stay significant.
    """
    view = deepcopy(candidate)
    if isinstance(view, Mapping) and view.get("kind") == "malformed_generate_response":
        if isinstance(view.get("parsed_candidate"), Mapping):
            view = deepcopy(dict(view["parsed_candidate"]))
        else:
            return {"unparsed_response_sha256": view.get("response_sha256")}
    if isinstance(view, Mapping):
        view = dict(view)
        for name in ("action_id", "candidate_generation_event_id"):
            view.pop(name, None)
        provenance = view.get("generation_provenance")
        if isinstance(provenance, Mapping):
            view["generation_provenance"] = {
                key: value for key, value in provenance.items()
                if key not in {"recovery_id", "recovery_envelope_id", "source_route"}
            }
    return view


def candidate_progress_assessment(
    *, candidate: Any, representation_targets: Sequence[str],
    binding_facts: Sequence[str] | None, violations: Sequence[Mapping[str, Any]],
    prior_rows: Sequence[Mapping[str, Any]], contract_ref: Mapping[str, Any],
) -> Dict[str, Any]:
    """One ledger decision from actual parser/validator observations.

    Error-code changes are diagnostics, not progress. A rejected novel candidate
    needs a newly satisfied target; no scientific success is inferred here. Full
    admission runs before this function, so a now-valid candidate needs no extra
    repair authorization. Historical rows without observations remain comparable
    by full identity, but cannot supply invented validation facts.
    """
    view = candidate_progress_view(candidate)
    representation = sorted(set(representation_targets))
    bindings = sorted(set(binding_facts)) if binding_facts is not None else None
    comparison = _digest({"candidate": view})
    validation_signature = _digest({"representation": representation,
        "binding": bindings, "contract_ref": dict(contract_ref or {})})
    comparison_context = _digest({"binding": bindings,
        "contract_ref": dict(contract_ref or {})})
    ordered = sorted(prior_rows, key=lambda row: int(row.get("repair_ordinal") or 0))
    previous = dict((ordered[-1] if ordered else {}).get("progress_assessment") or {})
    old_representation = set(previous.get("representation_targets") or [])
    old_bindings = set(previous.get("binding_facts") or [])
    gained_representation = sorted(set(representation) - old_representation)
    gained_bindings = sorted(set(bindings or []) - old_bindings)
    recurrence = any(
        (row.get("progress_assessment") or {}).get("comparison_fingerprint") == comparison
        and ((row.get("progress_assessment") or {}).get("comparison_context_fingerprint")
             or _digest({"binding": (row.get("progress_assessment") or {}).get("binding_facts"),
                         "contract_ref": dict(row.get("contract_ref") or {})})) == comparison_context
        for row in ordered
    )
    legacy_repeat = bool(ordered and not previous and
        ordered[-1].get("candidate_fingerprint") == candidate_repair_fingerprint(candidate))
    prior_targets = list(previous.get("pending_targets") or [])
    pending_targets = sorted({str(item.get("field_path") or
        (item.get("protocol_error") or {}).get("field_path") or "response")
        for item in violations})
    prior_pending = {str(target).removeprefix("$.") for target in prior_targets}
    # Per-key env facts describe optional fields, not extra repair obligations.
    # Adding an unrelated valid field cannot fix the previous rejection, and
    # dropping such a field cannot negate a demonstrated pending-value repair.
    gained_representation = [target for target in gained_representation
        if not target.startswith("execute.env.")
        or target.removeprefix("execute.") in prior_pending]
    lost_representation = {target for target in old_representation - set(representation)
        if not target.startswith("execute.env.")}
    current_env = view.get("env", {}) if isinstance(view, Mapping) else None
    removed_env_targets = sorted(target for target in prior_pending
        if target.startswith("env.") and isinstance(current_env, Mapping)
        and "execute.env_object" in representation and target[len("env."):] not in current_env)
    resolved_targets = sorted(target for target in set(prior_targets) - set(pending_targets)
        if not str(target).removeprefix("$.").startswith("env.")
        or "execute." + str(target).removeprefix("$.") in representation
        or str(target).removeprefix("$.") in removed_env_targets)
    if not ordered:
        proven, basis = True, "initial_rejection"
    elif recurrence or legacy_repeat:
        proven, basis = False, "recurrent_rejected_candidate"
    elif previous and (gained_representation or removed_env_targets) and not lost_representation:
        proven, basis = True, "validated_representation_target"
    elif (previous and bindings is not None and gained_bindings
          and not lost_representation
          and not (old_bindings - set(bindings))):
        proven, basis = True, "validated_binding_target"
    else:
        proven, basis = False, "no_validated_target_progress"
    return {"schema_version": 1, "producer": CANDIDATE_REPAIR_RECEIPT_PRODUCER,
        "progress_proven": proven, "basis": basis,
        "comparison_fingerprint": comparison, "validation_signature": validation_signature,
        "comparison_context_fingerprint": comparison_context,
        "representation_targets": representation, "binding_facts": bindings,
        "binding_axis": "evaluated" if bindings is not None else "not_evaluated",
        "newly_satisfied_targets": (gained_representation + gained_bindings
            + ["removed_optional_field:" + target for target in removed_env_targets]) if ordered else [],
        "resolved_targets": resolved_targets,
        "pending_targets": pending_targets, "scientific_success": "not_assessed"}


def candidate_repair_error_fingerprint(
    *,
    repair_stage: str,
    validation_source: str,
    contract_ref: Mapping[str, Any],
    exact_violations: Sequence[Mapping[str, Any]],
) -> str:
    """Identify the exact deterministic validator observation."""

    return _digest({
        "repair_stage": str(repair_stage or "pre_execution_candidate_admission"),
        "validation_source": str(validation_source or ""),
        "contract_ref": deepcopy(dict(contract_ref or {})),
        "exact_violations": [
            deepcopy(dict(item))
            for item in exact_violations
            if isinstance(item, Mapping)
        ],
    })


def candidate_repair_rejection_event_id(
    *,
    scope: Mapping[str, Any],
    candidate_event_id: str,
    candidate_fingerprint: str,
    error_fingerprint: str,
    repair_stage: str,
) -> str:
    """Return a retry-stable identity for one provider-candidate rejection."""

    identity = {
        "scope": deepcopy(dict(scope or {})),
        "candidate_event_id": str(candidate_event_id or ""),
        "candidate_fingerprint": str(candidate_fingerprint or ""),
        "error_fingerprint": str(error_fingerprint or ""),
        "repair_stage": str(repair_stage or "pre_execution_candidate_admission"),
    }
    return "candidate-rejection:" + _digest(identity)[:24]


class CandidateRepairReceiptError(ValueError):
    """Stable fail-closed error raised by the receipt trust boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "CANDIDATE_REPAIR_RECEIPT_INVALID")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


def candidate_repair_scope(
    plan_state: Mapping[str, Any],
    *,
    task_id: str,
    step_id: str,
) -> Dict[str, Any]:
    """Project the exact durable authority scope consumed by one receipt."""

    current_task = dict(plan_state.get("current_task") or {})
    task_spec = dict(current_task.get("task_spec") or {})
    return {
        "task_id": str(task_id or plan_state.get("task_id") or ""),
        "task_revision": int(
            current_task.get("task_revision")
            or task_spec.get("revision")
            or plan_state.get("task_revision")
            or plan_state.get("approved_task_revision")
            or 0
        ),
        "plan_id": str(plan_state.get("plan_id") or ""),
        "plan_version": str(plan_state.get("plan_version") or ""),
        "plan_revision": int(
            plan_state.get("plan_revision")
            or plan_state.get("approved_plan_revision")
            or 0
        ),
        "step_id": str(step_id or plan_state.get("current_step_id") or ""),
    }


def issue_candidate_repair_receipt(
    *,
    data_path: str,
    task_id: str,
    scope: Mapping[str, Any],
    candidate_fingerprint: str,
    error_fingerprint: str,
    violation_owner: str,
    feedback: str,
    repair_domain: str,
    repair_stage: str,
    repair_ordinal: int,
    validation_source: str,
    contract_ref: Mapping[str, Any],
    exact_violations: Sequence[Mapping[str, Any]],
    previous_candidate: Any,
    protocol_error: Mapping[str, Any] | None = None,
    candidate_event_id: str,
    rejection_event_id: str,
    repair_limit: int,
    progress_proven: bool,
    policy_receipt: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Persist and return one immutable Controller-produced rejection receipt."""

    normalized_scope = candidate_repair_scope(
        scope,
        task_id=str(scope.get("task_id") or task_id),
        step_id=str(scope.get("step_id") or ""),
    )
    candidate_id = str(candidate_fingerprint or "").strip()
    error_id = str(error_fingerprint or "").strip()
    feedback_text = str(feedback or "").strip()
    source = str(validation_source or "").strip()
    source_candidate_event = str(candidate_event_id or "").strip()
    rejection_id = str(rejection_event_id or "").strip()
    violations = [deepcopy(dict(item)) for item in exact_violations if isinstance(item, Mapping)]
    ordinal = int(repair_ordinal or 0)
    limit = int(repair_limit or 0)
    if not normalized_scope["task_id"] or not normalized_scope["step_id"]:
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_SCOPE_INVALID",
            "task_id and step_id are required",
        )
    if not candidate_id or not error_id:
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_IDENTITY_MISSING",
            "candidate and error fingerprints are required",
        )
    if (
        not feedback_text
        or not source
        or not source_candidate_event
        or not rejection_id
        or not violations
        or ordinal < 1
        or limit < 1
        or ordinal > limit
        or progress_proven is not True
    ):
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_EVIDENCE_INCOMPLETE",
            "feedback, source event, validator evidence, and an allowed repair budget are required",
        )
    body: Dict[str, Any] = {
        "schema_version": CANDIDATE_REPAIR_RECEIPT_SCHEMA_VERSION,
        "producer": CANDIDATE_REPAIR_RECEIPT_PRODUCER,
        "scope": normalized_scope,
        "candidate_fingerprint": candidate_id,
        "error_fingerprint": error_id,
        "violation_owner": str(violation_owner or "GENERATED_ACTION").upper(),
        "feedback": feedback_text,
        "repair_domain": str(repair_domain or "generated_semantic"),
        "repair_stage": str(repair_stage or "pre_execution_candidate_admission"),
        "repair_ordinal": ordinal,
        "repair_limit": limit,
        "progress_proven": True,
        "validation_source": source,
        "contract_ref": deepcopy(dict(contract_ref or {})),
        "failure_stage": "pre_execution_candidate_admission",
        "execution_status": "not_started",
        "exact_violations": violations,
        "previous_candidate": deepcopy(previous_candidate),
        "protocol_error": deepcopy(dict(protocol_error or {})),
        "candidate_event_id": source_candidate_event,
        "rejection_event_id": rejection_id,
    }
    if policy_receipt:
        body["policy_receipt"] = deepcopy(dict(policy_receipt))
    body["receipt_id"] = "candidate-repair-receipt:" + _digest(body)[:24]
    receipt_ref = StateContractStore(data_path, task_id).put_object(
        CANDIDATE_REPAIR_RECEIPT_OBJECT_TYPE,
        body,
    )
    return {**body, "receipt_ref": receipt_ref}


def validate_candidate_repair_receipt(
    value: Mapping[str, Any],
    *,
    data_path: str | None,
    task_id: str,
    expected_scope: Mapping[str, Any],
    expected_candidate_fingerprint: str,
    committed_repair_state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Verify schema, producer, scope, identity, ordinal, and durable content."""

    if not isinstance(value, Mapping) or not value:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_MISSING")
    receipt = deepcopy(dict(value))
    try:
        _canonical(receipt).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_NON_CANONICAL"
        ) from exc
    required = {
        "schema_version", "producer", "scope", "candidate_fingerprint",
        "error_fingerprint", "violation_owner", "feedback", "repair_domain",
        "repair_stage", "repair_ordinal", "validation_source", "contract_ref",
        "failure_stage", "execution_status", "exact_violations",
        "previous_candidate", "protocol_error", "rejection_event_id",
        "candidate_event_id", "repair_limit", "progress_proven",
        "receipt_id", "receipt_ref",
    }
    missing = sorted(key for key in required if key not in receipt)
    if missing:
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_INCOMPLETE", ", ".join(missing)
        )
    try:
        schema_version = int(receipt.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_SCHEMA_UNSUPPORTED"
        ) from exc
    if schema_version != CANDIDATE_REPAIR_RECEIPT_SCHEMA_VERSION:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_SCHEMA_UNSUPPORTED")
    if str(receipt.get("producer") or "") != CANDIDATE_REPAIR_RECEIPT_PRODUCER:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_PRODUCER_INVALID")
    if str(receipt.get("failure_stage") or "") != "pre_execution_candidate_admission":
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_STAGE_INVALID")
    if str(receipt.get("execution_status") or "") != "not_started":
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_EXECUTION_STATUS_INVALID")
    if not str(receipt.get("feedback") or "").strip():
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_FEEDBACK_MISSING")
    if (
        not isinstance(receipt.get("exact_violations"), list)
        or not receipt["exact_violations"]
        or not all(
            isinstance(item, Mapping)
            for item in receipt["exact_violations"]
        )
    ):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_VIOLATIONS_MISSING")
    if not isinstance(receipt.get("scope"), Mapping):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_SCOPE_INVALID")
    if not isinstance(receipt.get("contract_ref"), Mapping):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_CONTRACT_REF_INVALID")
    if not isinstance(receipt.get("protocol_error"), Mapping):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_PROTOCOL_ERROR_INVALID")

    observed_scope = dict(receipt.get("scope") or {})
    normalized_expected_scope = candidate_repair_scope(
        expected_scope,
        task_id=str(expected_scope.get("task_id") or task_id),
        step_id=str(expected_scope.get("step_id") or ""),
    )
    if observed_scope != normalized_expected_scope:
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_SCOPE_MISMATCH",
            _canonical({"expected": normalized_expected_scope, "observed": observed_scope}),
        )
    if str(receipt.get("candidate_fingerprint") or "") != str(
        expected_candidate_fingerprint or ""
    ):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_CANDIDATE_MISMATCH")
    try:
        ordinal = int(receipt.get("repair_ordinal") or 0)
        limit = int(receipt.get("repair_limit") or 0)
    except (TypeError, ValueError) as exc:
        raise CandidateRepairReceiptError(
            "CANDIDATE_REPAIR_RECEIPT_BUDGET_INVALID"
        ) from exc
    if ordinal < 1 or limit < 1 or ordinal > limit:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_BUDGET_INVALID")
    if receipt.get("progress_proven") is not True:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_PROGRESS_INVALID")

    expected_candidate_id = candidate_repair_fingerprint(
        receipt.get("previous_candidate")
    )
    if str(receipt.get("candidate_fingerprint") or "") != expected_candidate_id:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_CANDIDATE_INTEGRITY_MISMATCH")
    expected_error_id = candidate_repair_error_fingerprint(
        repair_stage=str(receipt.get("repair_stage") or ""),
        validation_source=str(receipt.get("validation_source") or ""),
        contract_ref=dict(receipt.get("contract_ref") or {}),
        exact_violations=[
            dict(item)
            for item in receipt.get("exact_violations") or []
            if isinstance(item, Mapping)
        ],
    )
    if str(receipt.get("error_fingerprint") or "") != expected_error_id:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_ERROR_INTEGRITY_MISMATCH")

    event_id = str(receipt.get("rejection_event_id") or "")
    if not isinstance(committed_repair_state, Mapping):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_LEDGER_INVALID")
    raw_events = committed_repair_state.get("events")
    if not isinstance(raw_events, Mapping):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_LEDGER_INVALID")
    events = dict(raw_events)
    raw_committed = events.get(event_id)
    if raw_committed is not None and not isinstance(raw_committed, Mapping):
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_LEDGER_INVALID")
    committed = dict(raw_committed or {})
    if not committed:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_NOT_COMMITTED")
    committed_identity = {
        "scope": observed_scope,
        "candidate_event_id": str(receipt.get("candidate_event_id") or ""),
        "candidate_fingerprint": str(receipt.get("candidate_fingerprint") or ""),
        "error_fingerprint": str(receipt.get("error_fingerprint") or ""),
        "repair_stage": str(receipt.get("repair_stage") or ""),
    }
    expected_event_id = candidate_repair_rejection_event_id(**committed_identity)
    if event_id != expected_event_id:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_EVENT_ID_INVALID")

    # A receipt authorizes one corrective transition, not every future
    # transition in the same task/plan/step scope.  Once a later rejection (or
    # terminal budget/no-progress event) is CAS-committed, replaying an older
    # receipt must fail closed even though its immutable object still exists.
    for other_event_id, raw_event in events.items():
        if not isinstance(raw_event, Mapping):
            raise CandidateRepairReceiptError(
                "CANDIDATE_REPAIR_RECEIPT_LEDGER_INVALID"
            )
        other = dict(raw_event)
        if not isinstance(other.get("scope"), Mapping):
            raise CandidateRepairReceiptError(
                "CANDIDATE_REPAIR_RECEIPT_LEDGER_INVALID"
            )
        if dict(other.get("scope") or {}) != observed_scope:
            continue
        try:
            other_ordinal = int(other.get("repair_ordinal") or 0)
        except (TypeError, ValueError) as exc:
            raise CandidateRepairReceiptError(
                "CANDIDATE_REPAIR_RECEIPT_LEDGER_INVALID"
            ) from exc
        if str(other_event_id) != event_id and other_ordinal >= ordinal:
            raise CandidateRepairReceiptError(
                "CANDIDATE_REPAIR_RECEIPT_STALE"
            )
    for key, expected in (
        ("receipt_id", receipt.get("receipt_id")),
        ("receipt_ref", receipt.get("receipt_ref")),
        ("candidate_fingerprint", receipt.get("candidate_fingerprint")),
        ("error_fingerprint", receipt.get("error_fingerprint")),
        ("candidate_event_id", receipt.get("candidate_event_id")),
        ("repair_ordinal", ordinal),
        ("repair_limit", limit),
        ("status", "repairable"),
    ):
        if committed.get(key) != expected:
            raise CandidateRepairReceiptError(
                "CANDIDATE_REPAIR_RECEIPT_LEDGER_MISMATCH", key
            )

    persisted_body = dict(receipt)
    receipt_ref = str(persisted_body.pop("receipt_ref") or "")
    expected_receipt_id = "candidate-repair-receipt:" + _digest({
        key: value for key, value in persisted_body.items() if key != "receipt_id"
    })[:24]
    if str(persisted_body.get("receipt_id") or "") != expected_receipt_id:
        raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_INTEGRITY_MISMATCH")
    if data_path is not None:
        if not receipt_ref:
            raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_REF_MISSING")
        try:
            stored = StateContractStore(data_path, task_id).read_object(
                receipt_ref,
                expected_type=CANDIDATE_REPAIR_RECEIPT_OBJECT_TYPE,
            )
        except (OSError, ValueError, StateContractIntegrityError) as exc:
            raise CandidateRepairReceiptError(
                "CANDIDATE_REPAIR_RECEIPT_REF_INVALID", str(exc)
            ) from exc
        if not isinstance(stored, Mapping) or dict(stored) != persisted_body:
            raise CandidateRepairReceiptError("CANDIDATE_REPAIR_RECEIPT_STORED_BODY_MISMATCH")
    return receipt


def latest_candidate_repair_receipt(data_path, task_id, state, scope):
    ledger = dict((state.get('task_state') or {}).get('candidate_repair_state') or {})
    events = [row for row in (ledger.get('events') or {}).values() if row.get('scope') == scope]
    last = max(events, key=lambda row: int(row.get('repair_ordinal') or 0), default={})
    if not last:
        return {}
    if last.get('status') != 'repairable':
        raise CandidateRepairReceiptError('GENERATE_REPAIR_SCOPE_STOPPED')
    ref = last['receipt_ref']
    body = StateContractStore(data_path, task_id).read_object(ref, expected_type=CANDIDATE_REPAIR_RECEIPT_OBJECT_TYPE)
    return validate_candidate_repair_receipt({**body, 'receipt_ref': ref}, data_path=data_path, task_id=task_id,
        expected_scope=scope, expected_candidate_fingerprint=last['candidate_fingerprint'], committed_repair_state=ledger)


def claim_candidate_generation(data_path, task_id, scope, *, invocation_id, receipt_id=''):
    """Claim the existing ledger's authorization before dispatch, via real CAS.

    No second repair counter: one authorization is the initial candidate or an
    immutable repair receipt. A known tool decision is not a semantic candidate;
    an unknown call is never refunded. Transport retries share this claim.
    """
    from agent_core.plan_tracker import load_plan_state, save_plan_state, StateCASConflict
    for _ in range(3):
        state = load_plan_state(data_path, task_id)
        actual = candidate_repair_scope(state, task_id=task_id, step_id=state.get('current_step_id'))
        if actual != scope:
            raise CandidateRepairReceiptError('CANDIDATE_GENERATION_SCOPE_STALE')
        task = state['task_state']
        if task.get('status') in {'completed','cancelled','failed','blocked','internal_protocol_error'}:
            raise CandidateRepairReceiptError('CANDIDATE_GENERATION_TASK_TERMINAL')
        receipt = latest_candidate_repair_receipt(data_path, task_id, state, scope)
        if receipt.get('receipt_id','') != receipt_id:
            raise CandidateRepairReceiptError('CANDIDATE_GENERATION_RECEIPT_NOT_IN_REQUEST')
        ledger = deepcopy(dict(task.get('candidate_repair_state') or {}))
        from agent_core.candidate_repair_policy import freeze_candidate_policy
        policy = freeze_candidate_policy(ledger, scope, task.get('budgets') or {})
        claims = ledger.setdefault('provider_claims', {})
        authorization = receipt_id or 'initial:' + _digest(scope)
        prior = [row for row in claims.values() if row['authorization'] == authorization]
        active = [row for row in prior if row['status'] != 'noncandidate']
        if active:
            held = active[0]
            if held['status'] != 'completed':
                raise CandidateRepairReceiptError('CANDIDATE_GENERATION_OUTCOME_UNKNOWN', held['claim_id'])
            content = StateContractStore(data_path,task_id).read_object(held['response_ref'],expected_type='generate_candidate_response')
            metadata = content.get('response_metadata')
            safe_metadata = {
                key: value for key, value in (metadata.items() if isinstance(metadata, Mapping) else ())
                if key in {'finish_reason', 'completion_integrity'}
                and isinstance(value, str) and value.strip() and len(value) <= 80
            }
            return {**held,'cached_response':content['content'],
                    'cached_response_metadata':safe_metadata}
        claim_id = 'candidate-call:' + _digest({'authorization':authorization,'continuation':len(prior)})
        claim = {'claim_id':claim_id,'scope':deepcopy(scope),'authorization':authorization,'status':'claimed',
            'invocation_id':invocation_id,'corrective_ordinal':int(receipt.get('repair_ordinal') or 0),'policy_receipt':policy}
        claims[claim_id] = claim
        task['candidate_repair_state'] = ledger
        try:save_plan_state(data_path,task_id,state)
        except StateCASConflict:continue
        return claim
    raise CandidateRepairReceiptError('CANDIDATE_GENERATION_CAS_CONFLICT')


def complete_candidate_generation(data_path, task_id, claim, *, content, noncandidate=False,
                                  response_metadata=None):
    from agent_core.plan_tracker import load_plan_state, save_plan_state, StateCASConflict
    payload = {'content':str(content)}
    safe_metadata = {
        key: value for key, value in (response_metadata.items() if isinstance(response_metadata, Mapping) else ())
        if key in {'finish_reason', 'completion_integrity'}
        and isinstance(value, str) and value.strip() and len(value) <= 80
    }
    if safe_metadata:
        # Preserve known response integrity across cache replay without adding
        # provider context or changing the original content/claim lifecycle.
        payload['response_metadata'] = safe_metadata
    ref = StateContractStore(data_path,task_id).put_object('generate_candidate_response', payload)
    for _ in range(3):
        state=load_plan_state(data_path,task_id)
        ledger=deepcopy(dict(state['task_state'].get('candidate_repair_state') or {}))
        held=ledger.get('provider_claims',{}).get(claim['claim_id'])
        if not held or held.get('invocation_id') != claim['invocation_id']:
            raise CandidateRepairReceiptError('CANDIDATE_GENERATION_CLAIM_MISMATCH')
        if held['status'] != 'claimed':
            if held.get('response_ref')==ref:return
            raise CandidateRepairReceiptError('CANDIDATE_GENERATION_COMPLETION_CONFLICT')
        held.update(status='noncandidate' if noncandidate else 'completed',response_ref=ref)
        state['task_state']['candidate_repair_state']=ledger
        try:save_plan_state(data_path,task_id,state)
        except StateCASConflict:continue
        return
    raise CandidateRepairReceiptError('CANDIDATE_GENERATION_COMPLETION_CAS_CONFLICT')

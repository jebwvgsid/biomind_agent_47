"""Deterministic admission runtime for route-blind Recovery directives.

The model-facing RecoveryAgent owns diagnosis and emits one strict
``RecoveryDirective``.  This module never infers a disposition from failure
text.  It validates the directive against the causal packet, preserves the
existing budget/CAS/idempotency boundary, and maps the semantic action into a
typed ``RecoveryOutcome`` for the graph's fixed conditional edges.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Callable, Mapping

from agent_core.plan_contract import StateCASConflict
from agent_core.plan_tracker import load_plan_state, save_plan_state, bind_recovery_generation_authority
from agent_core.workflow.outcomes import (
    RecoveryOutcome,
    WORKFLOW_ROUTING_SCHEMA_TYPED,
    workflow_routing_schema_version,
)
from agent_core.workflow.recovery_projection import (
    append_causal_failure_lifecycle_transition,
    has_causal_failure_lifecycle_transition,
    project_recovery_seed,
)
from agent_core.workflow.stage_contracts import (
    RecoveryDirective,
    StageContractError,
)


Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:24]


def _full_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical(value).encode("utf-8")
    ).hexdigest()


def revalidation_fingerprint(projection: Any) -> str:
    """Verifier inputs, not report IDs, read handles or model wording."""
    payload = projection.source.get("payload") or {}
    report = payload.get("completion_report") or {}
    if not report.get("validation_digest"):
        return ""
    return _full_hash({
        "attempt": projection.recovery_seed.failure_packet.attempt_ref,
        "validation_digest": report["validation_digest"],
        "gate": payload.get("completion_gate"),
        "disposition": report.get("effective_disposition"),
    })


def revalidation_stop_reason(projection: Any, durable: Mapping[str, Any], limit: int) -> str:
    packet = projection.recovery_seed.failure_packet
    task = durable.get("task_state") or {}
    prior = (task.get("typed_recovery_progress") or {}).get(packet.incident_id) or {}
    if prior.get("action") != "revalidate":
        return ""
    fingerprint = revalidation_fingerprint(projection)
    if fingerprint and fingerprint == prior.get("revalidation_fingerprint"):
        return "RECOVERY_NO_PROGRESS"
    if int((task.get("typed_recovery_budget") or {}).get(packet.incident_id) or 0) >= limit:
        return "RECOVERY_BUDGET_EXHAUSTED"
    return ""


class RecoveryRuntime:
    """Admit one semantic directive and persist one route-blind receipt."""

    BUDGETED_ACTIONS = frozenset({"retry_step", "revise_plan", "run_diagnostic_probe", "organize_result", "revalidate"})

    def __init__(
        self,
        *,
        data_path: str,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
        repair_limit: int = 2,
    ) -> None:
        self.data_path = str(data_path)
        self.loader = loader
        self.saver = saver
        self.repair_limit = max(1, int(repair_limit))

    @staticmethod
    def _result(outcome: RecoveryOutcome) -> dict[str, Any]:
        return {"recovery_outcome": outcome.to_dict()}

    @staticmethod
    def _internal(
        scope: Mapping[str, Any], code: str, detail: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return RecoveryRuntime._result(RecoveryOutcome(
            schema_version=1,
            type="internal_protocol_error",
            summary="Recovery failed closed on an invalid internal protocol.",
            scope=dict(scope),
            payload={
                "error_code": str(code),
                "source_scope": dict(scope),
                "identity_conflict": deepcopy(dict(detail or {})),
            },
        ))

    @staticmethod
    def _scope(task_id: str, durable: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "step_id": str(durable.get("current_step_id") or ""),
            "state_generation": int(durable.get("state_generation") or 0),
        }

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("TYPED_RECOVERY_TASK_ID_REQUIRED")
        durable = self.loader(self.data_path, task_id)
        scope = self._scope(task_id, durable)
        observed_schema = workflow_routing_schema_version(durable, state)
        if observed_schema != WORKFLOW_ROUTING_SCHEMA_TYPED:
            return self._internal(scope, "WORKFLOW_ROUTING_SCHEMA_NOT_TYPED", {
                "observed_schema": observed_schema,
            })

        try:
            projection = project_recovery_seed(state, durable)
        except StageContractError as exc:
            return self._internal(scope, exc.code, {"detail": exc.detail})
        if projection is None:
            return self._internal(scope, "RECOVERY_CAUSAL_PACKET_MISSING")
        seed = projection.recovery_seed
        packet = seed.failure_packet
        if packet.action_ref:
            scope["action_id"] = packet.action_ref
        if packet.attempt_ref:
            scope["attempt_id"] = packet.attempt_ref

        raw_directive = state.get("recovery_directive")
        if not isinstance(raw_directive, Mapping):
            return self._internal(scope, "RECOVERY_DIRECTIVE_MISSING")
        try:
            directive = RecoveryDirective.from_mapping(raw_directive)
        except StageContractError as exc:
            return self._internal(scope, exc.code, {"detail": exc.detail})

        if directive.task_id != packet.task_id:
            return self._internal(scope, "RECOVERY_DIRECTIVE_TASK_MISMATCH", {
                "directive_task_id": directive.task_id,
                "packet_task_id": packet.task_id,
            })
        if directive.target_step_id and directive.target_step_id != packet.step_id:
            return self._internal(scope, "RECOVERY_DIRECTIVE_STEP_MISMATCH", {
                "directive_step_id": directive.target_step_id,
                "packet_step_id": packet.step_id,
            })

        inspected_refs = {
            str(item) for item in state.get("recovery_inspected_refs") or ()
            if str(item)
        }
        declared_refs = {
            packet.source_outcome_ref,
            *packet.evidence_refs,
            *packet.inspection_refs,
            *seed.evidence_refs,
            *inspected_refs,
        }
        undeclared = sorted(set(directive.evidence_refs) - declared_refs)
        if undeclared:
            return self._internal(
                scope,
                "RECOVERY_DIRECTIVE_EVIDENCE_SCOPE_INVALID",
                {"undeclared_evidence_refs": undeclared},
            )

        source = dict(projection.source or {})
        source_payload = (
            dict(source.get("payload") or {})
            if projection.source_kind.endswith("outcome")
            else dict(source)
        )
        source_scope = (
            dict(source.get("scope") or {})
            if projection.source_kind.endswith("outcome")
            else {}
        )
        source_fingerprint = projection.source_fingerprint
        failure_fingerprint = str(
            packet.failure_fingerprint or source_fingerprint
        )
        source_incident_id = str(
            source_payload.get("incident_id") or ""
        ).strip()
        if (
            packet.incident_id
            and source_incident_id
            and packet.incident_id != source_incident_id
        ):
            return self._internal(scope, "RECOVERY_INCIDENT_ID_MISMATCH", {
                "packet_incident_id": packet.incident_id,
                "source_incident_id": source_incident_id,
            })
        # New schema-1 packets make the immutable FailurePacket authoritative.
        # Legacy packets retain the prior source-payload/failure-id fallback.
        incident_id = str(
            packet.incident_id or source_incident_id or packet.failure_id
        )
        failure_packet_ref = str(
            state.get("active_failure_packet_ref")
            or (durable.get("task_state") or {}).get(
                "active_failure_packet_ref"
            )
            or ""
        )
        if packet.incident_id:
            scope["incident_id"] = packet.incident_id
            scope["failure_packet_version"] = packet.packet_version
        decision_id = directive.directive_id
        directive_wire = directive.to_dict()
        directive_digest = _full_hash(directive_wire)

        task_state = dict(durable.get("task_state") or {})
        receipts = dict(task_state.get("typed_recovery_receipts") or {})
        prior = receipts.get(decision_id)
        if isinstance(prior, Mapping):
            if (
                str(prior.get("source_fingerprint") or "") != source_fingerprint
                or str(prior.get("directive_digest") or "") != directive_digest
            ):
                return self._internal(
                    scope,
                    "RECOVERY_DECISION_IDENTITY_CONFLICT",
                    {
                        "decision_id": decision_id,
                        "stored_source_fingerprint": str(prior.get("source_fingerprint") or ""),
                        "observed_source_fingerprint": source_fingerprint,
                        "stored_directive_digest": str(prior.get("directive_digest") or ""),
                        "observed_directive_digest": directive_digest,
                    },
                )
            return self._result(
                RecoveryOutcome.from_mapping(prior.get("outcome") or {})
            )

        ledger = dict(task_state.get("typed_recovery_budget") or {})
        result_resolution_request = {}
        if directive.result_selections:
            from agent_core.workflow.result_resolution import admit_result_resolution
            try:
                result_resolution_request = admit_result_resolution(
                    seed=seed, durable=durable, directive=directive, state=state)
            except StageContractError as exc:
                return self._internal(scope, exc.code, {"detail": exc.detail,
                    "command_started": False, "directive_id": decision_id})
        consumed_before = int(ledger.get(incident_id) or 0)
        consumes_budget = directive.action in self.BUDGETED_ACTIONS
        budget_receipt = {
            "incident_id": incident_id,
            "ordinal": consumed_before + (1 if consumes_budget else 0),
            "limit": self.repair_limit,
            "remaining": max(
                0,
                self.repair_limit - consumed_before - (1 if consumes_budget else 0),
            ),
            "consumed": consumes_budget,
            "source_fingerprint": source_fingerprint,
            **({
                "failure_packet_ref": failure_packet_ref,
                "failure_packet_version": packet.packet_version,
            } if packet.incident_id else {}),
        }

        progress = dict(task_state.get("typed_recovery_progress") or {})
        prior_progress = dict(progress.get(incident_id) or {})
        semantic_progress = {
            "action": directive.action,
            "summary": directive.summary,
            "reason_code": directive.reason_code,
            "instruction": directive.instruction,
            "question": directive.question,
            "diagnostic_operation": directive.diagnostic_operation, "diagnostic_inputs": list(directive.diagnostic_inputs),
            **({"result_selections": directive.to_dict()["result_selections"]}
               if directive.result_selections else {}),
        }
        prior_evidence = {
            str(item) for item in prior_progress.get("evidence_refs") or ()
            if str(item)
        }
        same_diagnosis = (
            str(prior_progress.get("semantic_digest") or "")
            == _full_hash(semantic_progress)
        )
        has_new_evidence = bool(set(directive.evidence_refs) - prior_evidence)

        outcome_type: str
        payload: dict[str, Any]
        if consumes_budget and consumed_before >= self.repair_limit:
            outcome_type = "blocked"
            payload = {
                "reason_code": "RECOVERY_BUDGET_EXHAUSTED",
                "blocked_evidence": {
                    "decision_id": decision_id,
                    "incident_id": incident_id,
                    "failure_fingerprint": failure_fingerprint,
                },
                "source_scope": scope,
                "budget_receipt": {
                    **budget_receipt,
                    "consumed": False,
                    "ordinal": consumed_before,
                },
            }
        elif (revalidation_stop_reason(projection, durable, self.repair_limit)
              or (same_diagnosis and not has_new_evidence)):
            outcome_type = "blocked"
            payload = {
                "reason_code": "RECOVERY_NO_PROGRESS",
                "blocked_evidence": {
                    "decision_id": decision_id,
                    "incident_id": incident_id,
                    "failure_fingerprint": failure_fingerprint,
                },
                "source_scope": scope,
                "budget_receipt": {
                    **budget_receipt,
                    "consumed": False,
                    "ordinal": consumed_before,
                },
            }
        elif directive.action in {"retry_step", "run_diagnostic_probe", "organize_result"}:
            outcome_type = "candidate_repair_required"
            payload = {
                "diagnosis": directive.summary,
                "failure_owner": packet.failure_owner,
                "strategy": "semantic_directive:" + directive.action,
                "repair_instruction": directive.instruction,
                "source_scope": scope,
                "decision_id": decision_id,
                "incident_id": incident_id,
                "failure_fingerprint": failure_fingerprint,
                "budget_receipt": budget_receipt,
            }
        elif directive.action == "revise_plan":
            outcome_type = "plan_revision_required"
            payload = {
                "diagnosis": directive.summary,
                "contract_violations": [directive.instruction],
                "source_scope": scope,
                "decision_id": decision_id,
                "budget_receipt": budget_receipt,
            }
        elif directive.action == "reconsult":
            outcome_type = "reconsultation_required"
            payload = {
                "diagnosis": directive.summary,
                "missing_facts": [directive.instruction],
                "source_scope": scope,
                "decision_id": decision_id,
            }
        elif directive.action == "revalidate":
            if not directive.evidence_refs:
                return self._internal(
                    scope,
                    "REVALIDATION_REQUIRES_NEW_EVIDENCE",
                    {"directive_id": decision_id},
                )
            outcome_type = "revalidation_required"
            payload = {
                "diagnosis": directive.summary,
                "new_evidence_refs": list(directive.evidence_refs),
                "prior_validation_id": str(
                    source_scope.get("validation_id")
                    or source_payload.get("prior_validation_id")
                    or ""
                ),
                "source_scope": scope,
                "decision_id": decision_id,
            }
        elif directive.action == "ask_user":
            outcome_type = "user_input_required"
            payload = {
                "diagnosis": directive.summary,
                "question_id": "question-" + _hash({
                    "directive_id": decision_id,
                    "slots": list(directive.requested_slots),
                }),
                "question": directive.question,
                "owner": "INPUT_DATA",
                "requires_user_input": True,
                "source_scope": scope,
                "decision_id": decision_id,
            }
        elif directive.action == "blocked":
            outcome_type = "blocked"
            payload = {
                "reason_code": directive.reason_code,
                "blocked_evidence": {
                    "diagnosis": directive.summary,
                    "unresolved_evidence": list(directive.evidence_refs),
                },
                "source_scope": scope,
                "budget_receipt": {**budget_receipt, "consumed": False},
            }
        else:  # RecoveryDirective is closed, but fail closed if it ever expands.
            return self._internal(scope, "RECOVERY_ACTION_UNMAPPED", {
                "action": directive.action,
            })

        summaries = {
            "candidate_repair_required": "A bounded changed candidate is required.",
            "plan_revision_required": "The approved Plan contract requires revision.",
            "reconsultation_required": "Authoritative task facts require reconsultation.",
            "revalidation_required": "New diagnostic evidence requires deterministic revalidation.",
            "user_input_required": "Recovery requires an input-owned user fact.",
            "blocked": "Recovery reached a durable terminal blocker.",
        }
        outcome = RecoveryOutcome(
            schema_version=1,
            type=outcome_type,
            summary=summaries[outcome_type],
            scope=scope,
            payload=payload,
            evidence_refs=directive.evidence_refs,
        )

        if consumes_budget and outcome_type in {
            "candidate_repair_required", "plan_revision_required", "revalidation_required"
        }:
            ledger[incident_id] = consumed_before + 1
            task_state["typed_recovery_budget"] = ledger
        if outcome_type != "blocked":
            progress[incident_id] = {
                "action": directive.action,
                "revalidation_fingerprint": revalidation_fingerprint(projection),
                "semantic_digest": _full_hash(semantic_progress),
                "evidence_refs": sorted(prior_evidence | set(directive.evidence_refs)),
                "directive_id": decision_id,
            }
            task_state["typed_recovery_progress"] = progress

        candidate = deepcopy(durable)
        candidate_task = dict(candidate.get("task_state") or {})
        for key in ("typed_recovery_budget", "typed_recovery_progress"):
            if key in task_state:
                candidate_task[key] = deepcopy(task_state[key])
        candidate_receipts = dict(candidate_task.get("typed_recovery_receipts") or {})
        candidate_receipts[decision_id] = {
            "schema_version": 1,
            "source_fingerprint": source_fingerprint,
            "directive_digest": directive_digest,
            "directive": directive_wire,
            "reception": {**deepcopy(state.get("recovery_reception") or {}),
                "admission": {"status": "admitted", "outcome_type": outcome_type,
                    "command_started": False, "directive_id": decision_id}},
            "failure_packet_ref": failure_packet_ref,
            **({
                "incident_id": packet.incident_id,
                "failure_packet_version": packet.packet_version,
                "supersedes_packet_ref": packet.supersedes_packet_ref,
            } if packet.incident_id else {}),
            "recovery_archive_ref": str(state.get("recovery_archive_ref") or ""),
            "causal_observation_receipts": deepcopy(state.get("recovery_causal_observation_receipts") or []),
            "business_resume_state": deepcopy(state.get("recovery_business_resume_state") or {}),
            "outcome_digest": _full_hash(outcome.to_dict()),
            "outcome": outcome.to_dict(),
            **({"result_resolution_request": result_resolution_request}
               if result_resolution_request else {}),
        }
        if outcome_type == "plan_revision_required":
            # Point to the existing admitted body; this is not a second Plan
            # or a new semantic directive. Planner reads it until scope moves.
            candidate_task["planner_revision_request"] = {
                "decision_id": decision_id,
                **{key: str(durable.get(key) or "") for key in
                   ("task_id", "plan_id", "plan_version", "current_step_id")},
            }
        if outcome_type == "candidate_repair_required":
            diagnostic_request = {}
            if directive.action == "run_diagnostic_probe":
                from agent_core.workflow.diagnostic_admission import admit_diagnostic_request
                try:
                    diagnostic_request = admit_diagnostic_request(seed=seed, durable=durable, directive=directive, data_path=self.data_path)
                except StageContractError as exc:
                    if exc.code in {"ARTIFACT_PERMISSION_DENIED", "ARTIFACT_NOT_FOUND", "DIAGNOSTIC_OPERATION_UNSUPPORTED", "DIAGNOSTIC_INPUT_BUDGET_EXCEEDED"}:
                        return self._result(RecoveryOutcome(schema_version=1, type="blocked", scope=scope,
                            summary="The requested diagnostic could not be admitted: " + exc.detail,
                            payload={"reason_code": exc.code, "source_scope": scope,
                                "blocked_evidence": {"decision_id": decision_id, "incident_id": incident_id,
                                    "failure_fingerprint": failure_fingerprint},
                                "failure_stage": "recovery.diagnostic_admission",
                                "detail": exc.detail, "operation": directive.diagnostic_operation,
                                "requested_refs": list(directive.diagnostic_inputs), "command_started": False,
                                "budget_receipt": {**budget_receipt, "consumed": False}}))
                    raise
            from pathlib import Path
            from agent_core.recovery import build_scoped_recovery_execution_envelope
            envelope = build_scoped_recovery_execution_envelope(
                decision_id=decision_id, source_scope=scope,
                strategy="RUN_DIAGNOSTIC_PROBE" if directive.action == "run_diagnostic_probe" else "PATCH_SCRIPT",
                recovery_context_id=seed.seed_id, step_id=packet.step_id,
                task_root=Path(self.data_path) / "task_outputs" / task_id,
                objective=directive.instruction,
                allowed_input_roots=(
                    [item["observed_path"] for item in result_resolution_request["selections"]]
                    if directive.action == "organize_result" else
                    [item["path"] for item in diagnostic_request.get("inputs", [])]),
            )
            if diagnostic_request:
                from dataclasses import replace
                from agent_core.recovery import RecoveryExecutionEnvelope
                envelope = replace(envelope, scratch_capability=replace(envelope.scratch_capability, **diagnostic_request["limits"]), immutable_hash="")
                envelope = RecoveryExecutionEnvelope.from_mapping(envelope.to_dict())
                candidate_receipts[decision_id]["diagnostic_request"] = diagnostic_request
            candidate_task["recovery_state"] = bind_recovery_generation_authority(
                candidate, candidate_task.get("recovery_state") or {},
                decision_id=decision_id, envelope=envelope.to_dict(), source_attempt_id=packet.attempt_ref,
            )
            candidate_receipts[decision_id].update(
                execution_envelope=envelope.to_dict(), execution_envelope_digest=envelope.immutable_hash,
                generation_authority=deepcopy(candidate_task["recovery_state"]["active_recovery"]),
                business_resume_state=deepcopy(state.get("recovery_business_resume_state") or {}),
                causal_observation_receipts=deepcopy(state.get("recovery_causal_observation_receipts") or []),
            )
        candidate_task["typed_recovery_receipts"] = candidate_receipts
        packet_ref = str(
            state.get("active_failure_packet_ref")
            or candidate_task.get("active_failure_packet_ref")
            or ""
        )
        if packet_ref:
            consumed_packets = dict(
                candidate_task.get("consumed_failure_packet_refs") or {}
            )
            consumed_packets[packet_ref] = {
                "decision_id": decision_id,
                "source_fingerprint": source_fingerprint,
                "directive_digest": directive_digest,
                **({
                    "incident_id": packet.incident_id,
                    "failure_packet_version": packet.packet_version,
                    "supersedes_packet_ref": packet.supersedes_packet_ref,
                } if packet.incident_id else {}),
            }
            candidate_task["consumed_failure_packet_refs"] = consumed_packets
            try:
                append_causal_failure_lifecycle_transition(
                    candidate_task,
                    packet_ref=packet_ref,
                    packet=packet,
                    transition="resolved",
                    reason=f"recovery_outcome_committed:{outcome_type}",
                    related_ref=decision_id,
                )
            except StageContractError as exc:
                return self._internal(scope, exc.code, {"detail": exc.detail})
            if str(candidate_task.get("active_failure_packet_ref") or "") == packet_ref:
                candidate_task["active_failure_packet_ref"] = ""
        candidate["task_state"] = candidate_task

        try:
            self.saver(self.data_path, task_id, candidate)
        except StateCASConflict:
            refreshed = self.loader(self.data_path, task_id)
            refreshed_receipt = dict(
                (refreshed.get("task_state") or {}).get("typed_recovery_receipts")
                or {}
            ).get(decision_id)
            if isinstance(refreshed_receipt, Mapping):
                if (
                    str(refreshed_receipt.get("source_fingerprint") or "")
                    == source_fingerprint
                    and str(refreshed_receipt.get("directive_digest") or "")
                    == directive_digest
                    and has_causal_failure_lifecycle_transition(
                        refreshed.get("task_state") or {},
                        packet_ref=failure_packet_ref,
                        transition="resolved",
                    )
                ):
                    return self._result(RecoveryOutcome.from_mapping(
                        refreshed_receipt.get("outcome") or {}
                    ))
            return self._internal(scope, "RECOVERY_RECEIPT_CAS_CONFLICT", {
                "expected_state_generation": int(durable.get("state_generation") or 0),
                "current_state_generation": int(refreshed.get("state_generation") or 0),
            })
        return self._result(outcome)


# Import compatibility for existing schema-2 wiring while callers migrate to
# the more accurate name.  This is not the schema-1 Recovery implementation.
TypedRecovery = RecoveryRuntime


def recovery_harness_error(exc: Exception, state: Mapping[str, Any]) -> RecoveryOutcome:
    return RecoveryOutcome(
        schema_version=1,
        type="harness_error",
        summary="Typed Recovery boundary failed closed.",
        scope={"task_id": str(state.get("task_id") or "unknown")},
        payload={
            "error_code": "TYPED_RECOVERY_HARNESS_ERROR",
            "exception_class": type(exc).__name__,
            "detail": str(exc)[:800],
        },
    )


__all__ = ["RecoveryRuntime", "TypedRecovery", "recovery_harness_error"]

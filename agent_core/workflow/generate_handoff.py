"""Verified Recovery-to-corrective-Generate handoff projection."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from agent_core.workflow.outcomes import RecoveryOutcome
from agent_core.workflow.stage_contracts import (
    FailurePacket,
    GenerateSeed,
    RecoveryDirective,
    StageContractError,
)


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
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def project_corrective_generate_seed(
    recovery_outcome: RecoveryOutcome | Mapping[str, Any],
    durable: Mapping[str, Any],
) -> GenerateSeed:
    """Resolve one admitted directive receipt into a transcript-free seed."""

    outcome = (
        recovery_outcome
        if isinstance(recovery_outcome, RecoveryOutcome)
        else RecoveryOutcome.from_mapping(recovery_outcome)
    )
    if outcome.type != "candidate_repair_required":
        raise StageContractError(
            "GENERATE_RECOVERY_OUTCOME_INVALID", outcome.type
        )
    payload = dict(outcome.to_dict().get("payload") or {})
    decision_id = str(payload.get("decision_id") or "")
    task_state = dict(durable.get("task_state") or {})
    receipt = dict(
        (task_state.get("typed_recovery_receipts") or {}).get(decision_id) or {}
    )
    if not receipt:
        raise StageContractError(
            "GENERATE_RECOVERY_RECEIPT_MISSING", decision_id
        )
    if str(receipt.get("outcome_digest") or "") != _digest(outcome.to_dict()):
        raise StageContractError(
            "GENERATE_RECOVERY_OUTCOME_MISMATCH", decision_id
        )
    directive = RecoveryDirective.from_mapping(receipt.get("directive") or {})
    if directive.directive_id != decision_id:
        raise StageContractError(
            "GENERATE_RECOVERY_DIRECTIVE_MISMATCH", decision_id
        )
    if str(receipt.get("directive_digest") or "") != _digest(directive.to_dict()):
        raise StageContractError(
            "GENERATE_RECOVERY_DIRECTIVE_MISMATCH", decision_id
        )
    packet_ref = str(receipt.get("failure_packet_ref") or "")
    record = dict(
        (task_state.get("causal_failure_packets") or {}).get(packet_ref) or {}
    )
    packet = FailurePacket.from_mapping(record.get("failure_packet") or {})
    archive_ref = str(receipt.get("recovery_archive_ref") or "")
    if not archive_ref:
        raise StageContractError(
            "GENERATE_RECOVERY_ARCHIVE_REF_MISSING", decision_id
        )
    if directive.task_id != packet.task_id:
        raise StageContractError(
            "STAGE_SCOPE_MISMATCH", "corrective Generate task identity"
        )
    if str(durable.get("current_step_id") or "") != packet.step_id:
        raise StageContractError(
            "GENERATE_RECOVERY_PACKET_STALE", packet.step_id
        )
    evidence_refs = tuple(dict.fromkeys((
        *directive.evidence_refs,
        *outcome.evidence_refs,
    )))
    identity = {
        "task_id": packet.task_id,
        "step_id": packet.step_id,
        "failure_id": packet.failure_id,
        "failure_packet_ref": packet_ref,
        "source_outcome_ref": packet.source_outcome_ref,
        "directive": directive.to_dict(),
        "evidence_refs": list(evidence_refs),
        "recovery_archive_ref": archive_ref,
    }
    return GenerateSeed(
        schema_version=1,
        seed_id="generate-seed-" + _digest(identity).removeprefix("sha256:")[:24],
        task_id=packet.task_id,
        step_id=packet.step_id,
        failure_id=packet.failure_id,
        failure_packet_ref=packet_ref,
        source_outcome_ref=packet.source_outcome_ref,
        recovery_directive=directive,
        evidence_refs=evidence_refs,
        recovery_archive_ref=archive_ref,
    )


__all__ = ["project_corrective_generate_seed"]

"""Read the accepted Recovery instruction for the current approved Plan.

This is a projection of the existing admission receipt, not another Plan or
route authority. Merely constructing a request never consumes the receipt.
"""
from copy import deepcopy
from typing import Any, Mapping


class PlannerRevisionContextError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _scope_matches(state: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    scope = dict((receipt.get("outcome") or {}).get("scope") or {})
    for key in ("task_id", "plan_id", "plan_version"):
        if not scope.get(key) or str(scope[key]) != str(state.get(key) or ""):
            return False
    step = str(scope.get("step_id") or "")
    return bool(step and step == str(state.get("current_step_id") or ""))


def accepted_planner_revision(state: Mapping[str, Any]) -> dict[str, Any]:
    task = dict(state.get("task_state") or {})
    receipts = dict(task.get("typed_recovery_receipts") or {})
    reference = dict(task.get("planner_revision_request") or {})
    decision = str(reference.get("decision_id") or "")
    if not decision:
        # Compatibility for an existing admitted receipt, without rewriting
        # it or choosing the newest item by filename/time/dictionary order.
        current_decisions = {str(row.get("directive_id") or "")
            for row in (task.get("typed_recovery_progress") or {}).values()
            if isinstance(row, Mapping)}
        matches = [key for key, row in receipts.items()
            if key in current_decisions and isinstance(row, Mapping)
            and _scope_matches(state, row)
            and (row.get("outcome") or {}).get("type") == "plan_revision_required"
            and ((row.get("reception") or {}).get("admission") or {}).get("status") == "admitted"]
        if not matches:
            return {}
        if len(matches) != 1:
            raise PlannerRevisionContextError("PLANNER_REVISION_CONTEXT_AMBIGUOUS",
                                              "More than one current admitted revision requires an explicit reference.")
        decision = matches[0]
    elif any(str(reference.get(key) or "") != str(state.get(key) or "")
             for key in ("task_id", "plan_id", "plan_version", "current_step_id")):
        return {}
    receipt = receipts.get(decision)
    if not isinstance(receipt, Mapping):
        raise PlannerRevisionContextError("PLANNER_REVISION_RECEIPT_MISSING", decision)
    if not _scope_matches(state, receipt):
        return {}
    admission = dict((receipt.get("reception") or {}).get("admission") or {})
    if admission.get("status") != "admitted" or (receipt.get("outcome") or {}).get("type") != "plan_revision_required":
        return {}
    directive = dict(receipt.get("directive") or {})
    if directive.get("action") != "revise_plan" or not str(directive.get("instruction") or "").strip():
        raise PlannerRevisionContextError("PLANNER_REVISION_BODY_MISSING",
                                          "The admitted revision has no usable instruction; initial planning is not a substitute.")
    if directive.get("task_id") != state.get("task_id"):
        raise PlannerRevisionContextError("PLANNER_REVISION_SCOPE_MISMATCH", decision)
    packet_ref = str(receipt.get("failure_packet_ref") or "")
    record = (task.get("causal_failure_packets") or {}).get(packet_ref)
    if not isinstance(record, Mapping) or not isinstance(record.get("failure_packet"), Mapping):
        raise PlannerRevisionContextError("PLANNER_REVISION_FAILURE_FACTS_MISSING", packet_ref)
    packet = dict(record["failure_packet"])
    if packet.get("task_id") != state.get("task_id") or packet.get("step_id") != (receipt.get("outcome") or {}).get("scope", {}).get("step_id"):
        raise PlannerRevisionContextError("PLANNER_REVISION_FAILURE_SCOPE_MISMATCH", packet_ref)
    return {"decision_id": decision, "directive": deepcopy(directive),
            "failure_packet": deepcopy(packet),
            "failure_source": deepcopy(dict(record.get("failure_source") or {}))}


def planner_revision_focus(revision: Mapping[str, Any]) -> dict[str, Any]:
    """Semantic facts only; private receipt identity stays outside the prompt."""
    if not revision:
        return {}
    directive = dict(revision["directive"])
    packet = dict(revision["failure_packet"])
    source = dict(revision.get("failure_source") or {})
    payload = dict(source.get("payload") or {})
    facts = {key: deepcopy(packet[key]) for key in
             ("failure_type", "failure_owner", "error_code", "summary", "retryable") if key in packet}
    facts["source_kind"] = str(packet.get("source") or "")
    for key in ("completion_gate", "contract_violations", "return_code",
                "runtime_output_reconciliation", "artifact_registration", "runtime_evidence"):
        if key in payload:
            facts[key] = deepcopy(payload[key])
    report = payload.get("completion_report")
    if isinstance(report, Mapping):
        facts["completion_report"] = {key: deepcopy(report[key]) for key in
            ("completion_gate", "effect_presence", "process_outcome", "deterministic_veto",
             "validation_status", "effective_disposition", "commit_eligible") if key in report}
    facts["diagnostic_facts"] = [
        {key: deepcopy(row[key]) for key in ("kind", "summary", "metadata") if key in row}
        for row in packet.get("diagnostic_facts") or [] if isinstance(row, Mapping)]
    return {"mode": "revise_current_plan", "return_complete_plan": True,
            "instruction": str(directive["instruction"]),
            "target_step_key": str(directive.get("target_step_id") or packet.get("step_id") or ""),
            "model_diagnosis": {"summary": str(directive.get("summary") or ""),
                                "reason_code": str(directive.get("reason_code") or "")},
            "recorded_failure_facts": facts,
            "preserve": "Revise the current approved Plan using its recorded progress, user requirements and capability contracts; return one complete candidate."}

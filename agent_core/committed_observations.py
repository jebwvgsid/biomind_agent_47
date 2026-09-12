"""Read accepted observation facts through existing scoped commit receipts.

This projection performs no I/O, inference, result registration or verification.
An execution's stdout or its successful exit alone cannot create an accepted fact.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping

from agent_core.plan_contract import plan_result_contracts, plan_uses_explicit_results
from agent_core.step_effects import canonical_step_effect, canonical_execution_effect_evidence


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identity(value: Any) -> str:
    if isinstance(value, str):
        return value
    row = _mapping(value)
    return next((str(row[key]) for key in ("identity", "observation_id", "result_id", "name", "id")
                 if row.get(key)), "")


def committed_observation_outcomes(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Join exact current Plan receipts, consumptions, validation and attempt.

    Older Plan revisions require an explicit reuse contract before entering this
    view. Merely retaining an old completed step does not authorize its evidence.
    """
    task = _mapping(plan.get("task_state"))
    provenance = _mapping(plan.get("provenance_state"))
    consumptions = _mapping(task.get("semantic_completion_consumptions"))
    validations = _mapping(provenance.get("validations"))
    attempts = _mapping(provenance.get("attempts"))
    steps = {str(row.get("step_id") or ""): row for row in plan.get("steps", ())
             if isinstance(row, Mapping) and str(row.get("status") or "").lower()
             in {"done", "completed", "passed"}}
    rows = []
    for commit_id, receipt in _mapping(task.get("typed_step_commit_receipts")).items():
        receipt = _mapping(receipt)
        outcome = _mapping(receipt.get("outcome"))
        payload = _mapping(outcome.get("payload"))
        consumption = _mapping(consumptions.get(str(payload.get("semantic_consumption_ref") or "")))
        scope = _mapping(consumption.get("scope"))
        if (outcome.get("type") not in {"next_step_ready", "plan_complete"}
                or str(payload.get("commit_id") or "") != str(commit_id)
                or str(consumption.get("commit_id") or "") != str(commit_id)
                or any(not str(scope.get(key) or "") or str(scope[key]) != str(plan.get(key) or "")
                       for key in ("task_id", "plan_id", "plan_version"))
                or _mapping(outcome.get("scope")).get("task_id") != scope.get("task_id")
                or not str(consumption.get("report_id") or "")
                or not str(consumption.get("decision_id") or "")
                or not str(receipt.get("request_fingerprint") or "")
                or consumption.get("report_id") != payload.get("completion_report_ref")
                or consumption.get("decision_id") != payload.get("semantic_decision_ref")
                or consumption.get("request_fingerprint") != receipt.get("request_fingerprint")):
            continue
        step_id = str(payload.get("completed_step_id") or "")
        step = steps.get(step_id)
        if step is None or scope.get("step_id") != step_id:
            continue
        validation_id = str(scope.get("validation_id") or "")
        validation = _mapping(validations.get(validation_id))
        attempt = _mapping(attempts.get(str(scope.get("attempt_id") or "")))
        if (not validation_id or validation_id not in outcome.get("evidence_refs", ())
                or str(validation.get("status") or "").upper() not in {"VALID", "VALID_WITH_WARNINGS"}
                or validation.get("evidence_complete") is not True
                or str(attempt.get("status") or "").lower() != "completed"
                or attempt.get("validation_evidence_ref") != validation_id
                or any(not str(scope.get(key) or "") or str(validation.get(key) or "") != str(scope[key])
                       for key in ("task_id", "step_id", "action_id", "attempt_id", "validation_id"))
                or any(str(attempt.get(key) or "") != str(scope[key])
                       for key in ("plan_id", "plan_version", "step_id", "action_id", "attempt_id"))):
            continue
        declared = {_identity(row) for row in canonical_step_effect(step)["produces_observations"]}
        accepted_refs = set(map(str, payload.get("accepted_effect_refs", ())))
        observations = canonical_execution_effect_evidence({"effect_evidence": validation.get("effect_results")})["produces_observations"]
        accepted = []
        for observation in observations:
            if not isinstance(observation, Mapping) or _identity(observation) not in declared:
                continue
            refs = {str(observation[key]) for key in ("identity", "observation_id", "result_id", "name", "id", "evidence_ref", "receipt_id")
                    if observation.get(key)}
            if refs & accepted_refs:
                accepted.append(deepcopy(dict(observation)))
        if accepted:
            rows.append({"step_id": step_id, "title": str(step.get("title") or step.get("action") or step_id),
                         "scope": deepcopy(dict(scope)), "observations": accepted,
                         "source_refs": [str(commit_id), validation_id, str(consumption["report_id"])]})
    return rows


def no_file_final_response(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Construct the explicit no-file task answer before committing completion.

    Values (including false, zero and null) are reported as recorded facts.
    This does not interpret a negative inspection as a failed execution.
    """
    if not plan_uses_explicit_results(plan) or any(
        row.get("required") is True for row in plan_result_contracts(plan).values()
    ):
        return None
    if not any(canonical_step_effect(step)["produces_observations"]
               for step in plan.get("steps", ()) if isinstance(step, Mapping)):
        # Bindings, workspace changes and other declared effects retain their
        # existing completion path; they do not invent an observation contract.
        return None
    outcomes = committed_observation_outcomes(plan)
    lines, refs = [], []
    for outcome in outcomes:
        for observation in outcome["observations"]:
            body = observation.get("evidence")
            body = body if isinstance(body, Mapping) else observation
            if "value" not in body:
                raise ValueError("FINAL_RESPONSE_OBSERVATION_VALUE_MISSING")
            rendered = json.dumps(body["value"], ensure_ascii=False, sort_keys=True, allow_nan=False)
            lines.append(f'{outcome["title"]} · {_identity(observation)}：{rendered}')
        refs.extend(outcome["source_refs"])
    if not lines:
        raise ValueError("FINAL_RESPONSE_ACCEPTED_OBSERVATION_MISSING")
    return {"text": "\n".join(lines), "source": "accepted_typed_observations",
            "source_refs": list(dict.fromkeys(refs)),
            "scope": {key: str(plan.get(key) or "") for key in ("task_id", "plan_id", "plan_version")}}

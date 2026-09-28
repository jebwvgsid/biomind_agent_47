from __future__ import annotations

import copy
import asyncio
import json
import unittest

from langchain_core.messages import AIMessage

from agent_core.workflow.recovery_projection import (
    build_causal_failure_record,
    project_recovery_seed,
)
from agent_core.workflow.outcomes import VerificationOutcome
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.test_recovery_projection import _durable, _execute_failure


def _seed_fixture():
    durable = _durable()
    record = build_causal_failure_record(
        "execute_outcome", _execute_failure(), durable, task_id="task-recovery"
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    projection = project_recovery_seed({
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"],
        "failure_source": record["failure_source"],
    }, durable)
    assert projection is not None
    return durable, projection.recovery_seed


def _directive() -> str:
    return json.dumps({
        "schema_version": 1,
        "directive_id": "directive-1",
        "task_id": "task-recovery",
        "action": "retry_step",
        "summary": "The generated action failed and must be regenerated.",
        "reason_code": "GENERATED_ACTION_FAILED",
        "target_step_id": "step-1",
        "instruction": "Regenerate the current action using the verified runtime facts.",
        "evidence_refs": ["execution-1"],
    })


def _typed_path_failure_seed():
    durable = _durable()
    outcome = VerificationOutcome(
        schema_version=1,
        type="correction_required",
        summary="Workspace receipt does not prove the formal path.",
        scope={
            "task_id": "task-recovery",
            "plan_id": "plan-recovery",
            "plan_version": "plan-v1",
            "step_id": "step-1",
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "validation_id": "validation-path-1",
        },
        payload={
            "validation_evidence": {"validation_id": "validation-path-1"},
            "completion_gate": {
                "passed": False,
                "classification": "semantic_completion_rejected",
                "missing_effects": {
                    "workspace_mutations": ["artifact.snpeff_data_dir"],
                },
            },
            "completion_report": {
                "report_id": "completion-report-path-1",
                "scope": {
                    "task_id": "task-recovery",
                    "plan_id": "plan-recovery",
                    "plan_version": "plan-v1",
                    "step_id": "step-1",
                    "action_id": "action-1",
                    "attempt_id": "attempt-1",
                    "validation_id": "validation-path-1",
                },
                "validation_digest": "sha256:validation-path-1",
                "deterministic_veto": True,
                "effective_disposition": "correction_required",
                "completion_gate": {
                    "passed": False,
                    "classification": "semantic_completion_rejected",
                    "missing_effects": {
                        "workspace_mutations": [
                            "artifact.snpeff_data_dir"
                        ],
                    },
                },
                "process_outcome": {
                    "validation_status": "VALID",
                    "returncode": 0,
                    "returncode_passed": True,
                    "evidence_complete": True,
                    "command_role": "analysis_execution",
                    "execution_status": "succeeded",
                },
                "effect_presence": {
                    "declared": {
                        "workspace_mutations": [
                            "artifact.snpeff_data_dir"
                        ],
                    },
                    "observed": {"workspace_mutations": []},
                    "missing": {
                        "workspace_mutations": [
                            "artifact.snpeff_data_dir"
                        ],
                    },
                },
                "binding_and_receipt_findings": [{
                    "issue_id": "issue-workspace-path-1",
                    "status": "failed",
                    "code": "WORKSPACE_MUTATION_RECEIPT_INVALID",
                    "affected_resource": "artifact.snpeff_data_dir",
                    "receipt": {
                        "receipt_id": "workspace-receipt-path-1",
                        "producer": "biomind.runtime_attester.v1",
                        "canonical_resource_id": "artifact.snpeff_data_dir",
                        "formal_concrete_path": "/formal/snpeff",
                        "attestation_status": (
                            "idempotent_revalidation_evidence_missing"
                        ),
                        "attested": False,
                        "scope_mismatches": {
                            "formal_concrete_path": {
                                "expected": "/formal/snpeff",
                                "observed": "/wrong/snpeff",
                            },
                        },
                        "evidence_refs": ["evidence:workspace-receipt-1"],
                    },
                }],
                "semantic_review_requirements": {
                    "required": False,
                    "policy_source": "no_explicit_review_policy",
                    "required_issue_ids": ["issue-workspace-path-1"],
                    "allowed_issue_ids": [
                        "validation-path-1", "issue-workspace-path-1"
                    ],
                    "decision_schema": "biomind.semantic_decision.v1",
                    "review_input_status": "not_required",
                    "commit_eligible": False,
                    "commit_authority": "commit_step_only",
                },
                "findings": [{
                    "issue_id": "issue-workspace-path-1",
                    "nature": "binding_and_receipt",
                    "code": "WORKSPACE_MUTATION_RECEIPT_INVALID",
                    "contract_source": (
                        "workspace_mutation_attestation:"
                        "artifact.snpeff_data_dir:prepare_database"
                    ),
                    "actual": "/wrong/snpeff",
                    "expected": "/formal/snpeff",
                    "evidence_refs": ["evidence:workspace-receipt-1"],
                    "affected_resource": "artifact.snpeff_data_dir",
                    "affected_field": "formal_concrete_path",
                    "status": "failed",
                    "disposition": "correction_required",
                    "confidence_or_unknown_reason": 1.0,
                }],
                "predicate_results": [{
                    "result_id": "issue-workspace-path-1",
                    "predicate_id": (
                        "workspace_mutation_attestation:"
                        "artifact.snpeff_data_dir:prepare_database"
                    ),
                    "observation_id": "artifact.snpeff_data_dir",
                    "status": "failed",
                    "passed": False,
                    "reason_code": "WORKSPACE_MUTATION_RECEIPT_INVALID",
                    "disposition": "correction_required",
                    "parser_receipt": {
                        "canonical_resource_id": "artifact.snpeff_data_dir",
                        "formal_concrete_path": "/formal/snpeff",
                        "scope_mismatches": {
                            "formal_concrete_path": {
                                "expected": "/formal/snpeff",
                                "observed": "/wrong/snpeff",
                            },
                        },
                        "attestation_status": (
                            "idempotent_revalidation_evidence_missing"
                        ),
                        "evidence_refs": ["evidence:workspace-receipt-1"],
                    },
                }],
            },
            "semantic_decision": {"disposition": "correction_required"},
            "contract_violations": [
                "WORKSPACE_MUTATION_RECEIPT_INVALID"
            ],
            "failure_owner": "deterministic_predicate",
            "binding": {
                "issue_id": "binding-path-1",
                "code": "BINDING_PATH_MISMATCH",
                "canonical_resource_id": "artifact.snpeff_data_dir",
                "formal_path": "/formal/snpeff",
                "observed_path": "/wrong/snpeff",
            },
            "receipt": {
                "issue_id": "receipt-path-1",
                "reason_code": "WORKSPACE_MUTATION_RECEIPT_INVALID",
                "receipt_reason": "formal_path_not_observed",
            },
        },
        evidence_refs=("evidence:workspace-receipt-1",),
    ).to_dict()
    record = build_causal_failure_record(
        "verification_outcome", outcome, durable, task_id="task-recovery"
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["causal_failure_incident_heads"] = {
        record["incident_id"]: record["packet_ref"]
    }
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    projection = project_recovery_seed(
        {
            "task_id": "task-recovery",
            "active_failure_packet_ref": record["packet_ref"],
            "failure_packet": record["failure_packet"],
        },
        durable,
    )
    assert projection is not None
    return durable, projection.recovery_seed, record


class _Dispatcher:
    def __init__(self, *, repeated: bool = False) -> None:
        self.calls = []
        self.repeated = repeated

    def dispatch(self, tool, args):
        self.calls.append((tool, copy.deepcopy(dict(args))))
        return {
            "status": "ok",
            "evidence_ref": "execution-1",
            "fact": "return code 17",
        }


class _ToolThenDirectiveModel:
    def __init__(self, durable=None):
        self.requests = []
        self.durable = durable

    async def ainvoke(self, messages):
        self.requests.append([provider_visible_payload(item) for item in messages])
        if len(self.requests) == 1:
            if self.durable is not None:
                self.durable["messages"] = ["global history must stay excluded"]
                self.durable["current_task"]["task_spec"]["goal"] = "mutated after seed"
            return AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-1",
                    "name": "failure_inspect",
                    "args": {},
                    "type": "tool_call",
                }],
            )
        return AIMessage(content=_directive())




if __name__ == "__main__":
    unittest.main()

import unittest

from agent_core.plan_tracker import apply_recovery_decision, build_plan_state
from agent_core.recovery import (
    RecoveryDecision,
    RecoveryDecisionKind,
    RecoveryPolicy,
    recovery_route_from_state,
)


class RecoveryPolicyOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.policy = RecoveryPolicy(
            max_attempts=2,
            max_probe_attempts=1,
            max_no_progress=1,
        )

    def _plan(self):
        return build_plan_state(
            "task-a",
            "\n".join([
                "- [>] Step 1: Run action | Action: EXECUTE | Done: result.tsv exists",
                "- [ ] Step 2: Finalize | Action: EXECUTE | Done: report.md exists",
            ]),
        )

    def test_protocol_error_uses_format_budget_only(self):
        decision = self.policy.decide(
            pre_action_evidence={
                "valid": False,
                "repairable": True,
                "violations": ["renderer_invocation_required"],
            },
            action_id="action-1",
            step_id="step-1",
            attempt_id="attempt-1",
            budgets={"action_format_repair_count": 0, "execution_retry_count": 0},
        )

        self.assertEqual(decision.kind, RecoveryDecisionKind.FORMAT_REPAIR.value)
        self.assertEqual(decision.consume_budget, "action_format_repair_count")
        self.assertEqual(decision.reason_code, "internal_action_protocol_error")

    def test_protocol_error_escalates_when_format_budget_is_exhausted(self):
        decision = self.policy.decide(
            pre_action_evidence={
                "valid": False,
                "repairable": True,
                "violations": ["renderer_invocation_required"],
            },
            action_id="action-1",
            attempt_id="attempt-2",
            budgets={"action_format_repair_count": 2},
        )

        self.assertEqual(decision.kind, RecoveryDecisionKind.ESCALATE.value)
        self.assertIsNone(decision.consume_budget)

    def test_nonzero_execution_retries_once_and_uses_execution_budget(self):
        decision = self.policy.decide(
            execution_evidence={"returncode": 1, "command_role": "primary"},
            action_id="action-1",
            attempt_id="attempt-1",
            budgets={"execution_retry_count": 0},
        )

        self.assertEqual(decision.kind, RecoveryDecisionKind.RETRY_CURRENT_ACTION.value)
        self.assertEqual(decision.consume_budget, "execution_retry_count")

    def test_structured_missing_input_blocks_with_evidence(self):
        decision = self.policy.decide(
            pre_action_evidence={
                "valid": False,
                "missing_bindings": ["reference_fasta"],
                "blocked_evidence": ["/data/reference.fa does not exist"],
                "repairable": False,
            },
            action_id="action-1",
            attempt_id="attempt-1",
        )

        self.assertEqual(decision.kind, RecoveryDecisionKind.BLOCKED.value)
        self.assertEqual(decision.blocked_evidence, ["/data/reference.fa does not exist"])

    def test_verified_capability_disallows_repeated_probe(self):
        decision = self.policy.decide(
            pre_action_evidence={"valid": True},
            validation_evidence={"status": "unknown", "recoverability_hints": ["probe"]},
            progress_snapshot={
                "capability_verified": True,
                "meaningful_progress": False,
                "repeated_probe_without_progress": True,
            },
            action_id="action-1",
            attempt_id="attempt-2",
            budgets={"diagnostic_probe_count": 1},
        )

        self.assertNotEqual(decision.kind, RecoveryDecisionKind.RUN_ALLOWED_PROBE.value)
        self.assertEqual(decision.kind, RecoveryDecisionKind.FORMAT_REPAIR.value)

    def test_probe_success_does_not_run_primary_artifact_recovery(self):
        decision = self.policy.decide(
            execution_evidence={"returncode": 0, "command_role": "diagnostic"},
            validation_evidence={"status": "warning", "errors": ["required artifact absent"]},
            action_id="probe-1",
            attempt_id="attempt-1",
        )

        self.assertEqual(decision.kind, RecoveryDecisionKind.CONTINUE.value)
        self.assertIsNone(decision.consume_budget)

    def test_completion_evidence_closes_step_or_task(self):
        step = self.policy.decide(
            completion_evidence={"completion_satisfied": True, "task_complete": False},
            action_id="action-1",
            attempt_id="attempt-1",
        )
        task = self.policy.decide(
            completion_evidence={"completion_satisfied": True, "task_complete": True},
            action_id="action-2",
            attempt_id="attempt-1",
        )

        self.assertEqual(step.kind, RecoveryDecisionKind.COMPLETE_STEP.value)
        self.assertEqual(task.kind, RecoveryDecisionKind.COMPLETE_TASK.value)

    def test_plan_patch_requires_structural_plan_evidence(self):
        decision = self.policy.decide(
            validation_evidence={
                "status": "failed",
                "failure_owner": "PLAN",
                "contract_violations": ["cyclic step dependency"],
            },
            action_id="action-1",
            attempt_id="attempt-1",
        )

        self.assertEqual(decision.kind, RecoveryDecisionKind.PLAN_PATCH_REQUEST.value)

    def test_apply_decision_is_idempotent_and_consumes_one_budget(self):
        plan = self._plan()
        decision = self.policy.decide(
            execution_evidence={"returncode": 1, "command_role": "primary"},
            action_id="action-1",
            step_id="step-1",
            attempt_id="attempt-1",
            budgets={"execution_retry_count": 0},
        )

        first = apply_recovery_decision(plan, decision)
        second = apply_recovery_decision(plan, decision)
        budgets = plan["task_state"]["budgets"]

        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(budgets["execution_retry_count"], 1)

    def test_recovery_decision_v2_migrates_idempotently(self):
        legacy = {
            "schema_version": 2,
            "failure_class": "SCRIPT_BUG",
            "failure_owner": "SCRIPT",
            "recommended_action": "PATCH_SCRIPT",
            "reason": "quote failure",
            "evidence": ["stderr"],
        }

        first = RecoveryDecision.from_mapping(legacy)
        second = RecoveryDecision.from_mapping(first.to_dict())

        self.assertEqual(first.kind, RecoveryDecisionKind.RETRY_CURRENT_ACTION.value)
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(first.migration_source, "recovery_decision_v2")

    def test_conflicting_legacy_fields_are_recorded(self):
        decision = RecoveryDecision.from_mapping({
            "verdict": "continue",
            "action_type": "pass_with_warning",
            "root_cause": "validation warning",
            "confidence": 0.9,
            "evidence": ["validator"],
            "supervisor_verdict": "RETRY",
        })

        self.assertIn("legacy_supervisor_verdict_conflict", decision.migration_conflicts)

    def test_router_does_not_parse_narrative_supervisor_text(self):
        route = recovery_route_from_state({
            "last_supervisor_feedback": {
                "review": "The word RETRY appears in this advisory paragraph."
            },
            "messages": ["WARN: maybe retry later"],
        }, "supervisor")

        self.assertEqual(route, "")

    def test_router_uses_committed_kind_and_source_scope(self):
        decision = self.policy.decide(
            execution_evidence={"returncode": 1, "command_role": "primary"},
            action_id="action-1",
            step_id="step-1",
            attempt_id="attempt-1",
        )
        decision.source_node = "execute"
        state = {"recovery_decision": decision.to_dict()}

        self.assertEqual(recovery_route_from_state(state, "execute"), "generate")
        self.assertEqual(recovery_route_from_state(state, "generate"), "")

    def test_legacy_retry_migration_does_not_consume_budget(self):
        plan = self._plan()
        state = {
            "supervisor_verdict": "RETRY",
            "retry_reason": "legacy checkpoint",
            "current_step_id": "step-1",
        }

        route = recovery_route_from_state(state, "supervisor")

        self.assertEqual(route, "generate")
        self.assertEqual(plan["task_state"]["budgets"]["execution_retry_count"], 0)


if __name__ == "__main__":
    unittest.main()

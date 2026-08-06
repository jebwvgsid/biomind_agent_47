import copy
import unittest

from agent_core.plan_tracker import (
    approve_plan_state,
    build_approval_projection,
    build_approval_scope,
    plan_approval_is_current,
    plan_contract_violations,
    stable_plan_revision_delta,
    validate_action_approval_lineage,
    carry_forward_plan_lifecycle,
)
from agent_core.task_spec import create_task_spec, effective_output_contracts, merge_task_spec


class PlanContractApprovalScopeTests(unittest.TestCase):
    def _state(self):
        spec = create_task_spec(
            "Use data/input.vcf and output report.md.\nOutput directory: out",
            task_id="contract-canary",
            project_root="/tmp/project",
        )
        return {
            "task_id": "contract-canary",
            "plan_id": "plan-contract-canary",
            "plan_version": "plan_v1",
            "plan_revision": 1,
            "plan_status": "pending_review",
            "generated_from_task_revision": 1,
            "selected_skill_ids": ["genomics/example"],
            "task_spec": spec,
            "steps": [{
                "step_id": "step-1",
                "title": "Validate local input",
                "action": "execute",
                "purpose": "validation",
                "tool": "shell",
                "inputs": ["data/input.vcf"],
                "outputs": [],
                "dependencies": [],
                "success_criteria": ["input file exists and is non-empty"],
                "skill_ids": ["genomics/example"],
                "status": "pending",
                "risk_level": "safe",
            }],
        }

    def test_duplicate_output_contract_preserves_all_provenance(self):
        spec = create_task_spec("Output report.md", task_id="p")
        merged = merge_task_spec(
            spec,
            {"output_contracts": [{
                "filename": "report.md",
                "source_type": "benchmark_contract",
                "source_ref": "case.yaml#expected_outputs[0]",
            }]},
            source="benchmark_contract",
            reason="benchmark contract",
            increment_revision=False,
        )
        output = next(item for item in effective_output_contracts(merged) if item["filename"] == "report.md")
        source_types = {item["source_type"] for item in output["provenance"]}
        self.assertIn("user_explicit", source_types)
        self.assertIn("benchmark_contract", source_types)

    def test_approval_scope_invalidates_when_task_spec_changes(self):
        state = self._state()
        projection = build_approval_projection(state)
        approve_plan_state(state, 1, approval_projection=projection)
        self.assertTrue(plan_approval_is_current(state, 1))

        state["task_spec"] = merge_task_spec(
            state["task_spec"],
            {"requirements": [{"requirement_id": "mode", "value": "strict"}]},
            source="user_feedback",
            reason="changed requirement",
        )
        self.assertFalse(plan_approval_is_current(state, 1))

    def test_action_lineage_requires_current_approval_scope(self):
        state = self._state()
        approve_plan_state(state, 1, approval_projection=build_approval_projection(state))
        scope = build_approval_scope(state)
        action = {
            "step_id": "step-1",
            "task_revision": 1,
            "plan_version": "plan_v1",
            "generated_from_plan_hash": scope["plan_hash"],
            "generated_from_approval_scope_hash": scope["approval_scope_hash"],
        }
        self.assertEqual(validate_action_approval_lineage(state, action, 1), "")
        stale = copy.deepcopy(action)
        stale["generated_from_approval_scope_hash"] = "stale"
        self.assertIn("approval scope", validate_action_approval_lineage(state, stale, 1).lower())

    def test_selected_skill_or_renderer_contract_change_stales_approval(self):
        state = self._state()
        state["steps"][0]["renderer_id"] = "renderer-a"
        approve_plan_state(state, 1, approval_projection=build_approval_projection(state))
        state["selected_skill_ids"] = ["genomics/other"]
        self.assertFalse(plan_approval_is_current(state, 1))

        state = self._state()
        state["steps"][0]["renderer_id"] = "renderer-a"
        approve_plan_state(state, 1, approval_projection=build_approval_projection(state))
        state["steps"][0]["renderer_id"] = "renderer-b"
        self.assertFalse(plan_approval_is_current(state, 1))

    def test_search_cannot_validate_local_filesystem(self):
        state = self._state()
        state["steps"][0]["action"] = "search"
        violations = plan_contract_violations(state)
        self.assertTrue(any(item.get("violation_code") == "INVALID_ACTION_FOR_STEP_PURPOSE" for item in violations))

    def test_revision_delta_uses_stable_ids_and_classifies_collapse(self):
        previous = self._state()
        previous["steps"].append({**copy.deepcopy(previous["steps"][0]), "step_id": "step-9"})
        candidate = self._state()
        candidate["steps"][0]["title"] = "Validate canonical input"
        candidate["steps"].append({**copy.deepcopy(candidate["steps"][0]), "step_id": "step-12"})
        candidate["normalization_audit"] = [{"status": "collapsed", "removed_step_ids": ["step-9"]}]
        delta = stable_plan_revision_delta(previous, candidate)
        self.assertEqual(delta["changed_step_ids"], ["step-1"])
        self.assertEqual(delta["added_step_ids"], ["step-12"])
        self.assertEqual(delta["collapsed_step_ids"], ["step-9"])

    def test_semantic_revision_advances_version_and_stales_approval(self):
        previous = self._state()
        approve_plan_state(previous, 1, approval_projection=build_approval_projection(previous))
        candidate = copy.deepcopy(previous)
        candidate["steps"][0]["title"] = "Validate revised input"
        revised = carry_forward_plan_lifecycle(previous, candidate)
        self.assertEqual(revised["plan_version"], "plan_v2")
        self.assertEqual(revised["parent_plan_version"], "plan_v1")
        self.assertEqual(revised["approval_status"], "awaiting_review")


if __name__ == "__main__":
    unittest.main()

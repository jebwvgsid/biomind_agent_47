from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from agent_core.plan_tracker import (
    approval_applicability_v2,
    approve_plan_state,
    load_plan_state,
    plan_record_hash,
    refresh_state_contract_v2,
    save_plan_state,
    semantic_plan_hash,
)
from agent_core.state_contract_store import StateContractStore


def _state() -> dict:
    task_spec = {
        "schema_version": 4,
        "revision": 1,
        "goal": "Create final FASTA",
        "inputs": [{"input_id": "in-1", "role": "source", "path": "/data/in.fa"}],
        "output_contracts": [{"output_id": "out-1", "role": "final", "path": "/out/aa.fa"}],
        "requirements": [{
            "id": "c-1",
            "semantic_key": "required_output.final",
            "operator": "require",
            "value": "output.final",
            "strength": "hard",
            "source_ref": "audit-one",
        }],
        "change_log": [],
    }
    return {
        "schema_version": 8,
        "plan_schema_version": 8,
        "migration_version": 8,
        "state_generation": 0,
        "task_id": "task-1",
        "plan_id": "plan-1",
        "plan_version": "plan_v1",
        "plan_revision": 1,
        "generated_from_task_revision": 1,
        "current_task": {
            "task_spec": task_spec,
            "task_revision": 1,
            "task_semantic_hash": "",
            "task_record_hash": "",
            "lifecycle": "initialized",
        },
        "steps": [{
            "step_id": "step-1",
            "title": "Build final FASTA",
            "action": "execute",
            "purpose": "analysis",
            "tool": "tool",
            "inputs": ["in-1"],
            "outputs": ["out-1"],
            "dependencies": [],
            "depends_on": [],
            "success_criteria": ["valid FASTA"],
            "skill_ids": [],
            "semantic_constraint_ids": ["c-1"],
            "status": "pending",
            "risk_level": "low",
        }],
        "validation_issues": [],
        "renderer_projections": [],
        "repair_convergence": {"status": "valid"},
        "validation_recompute_required": False,
        "task_state": {"status": "running", "budgets": {}},
    }


class ApprovalSnapshotV2Tests(unittest.TestCase):
    def test_snapshot_is_immutable_and_record_only_change_stays_applicable(self) -> None:
        state = _state()
        refresh_state_contract_v2(state)
        approve_plan_state(state, 1)
        snapshot = copy.deepcopy(state["approval_snapshot"])
        semantic_before = state["current_task"]["task_semantic_hash"]
        record_before = state["current_task"]["task_record_hash"]

        state["current_task"]["task_spec"]["requirements"][0]["source_ref"] = "audit-two"
        state["current_task"]["task_spec"]["source_refs"].append("audit-two")
        refresh_state_contract_v2(state)

        self.assertEqual(state["approval_snapshot"], snapshot)
        self.assertEqual(state["current_task"]["task_semantic_hash"], semantic_before)
        self.assertNotEqual(state["current_task"]["task_record_hash"], record_before)
        self.assertEqual(approval_applicability_v2(state)["status"], "applicable")

    def test_task_semantic_change_is_stale_task(self) -> None:
        state = _state()
        refresh_state_contract_v2(state)
        approve_plan_state(state, 1)
        state["current_task"]["task_spec"]["requirements"][0]["value"] = "output.changed"
        refresh_state_contract_v2(state)
        self.assertEqual(approval_applicability_v2(state)["status"], "stale_task")

    def test_plan_semantic_change_is_stale_plan(self) -> None:
        state = _state()
        refresh_state_contract_v2(state)
        approve_plan_state(state, 1)
        state["steps"][0]["tool"] = "different-tool"
        refresh_state_contract_v2(state)
        self.assertEqual(approval_applicability_v2(state)["status"], "stale_plan")

    def test_validation_timestamp_is_outside_plan_hash_domains(self) -> None:
        state = _state()
        refresh_state_contract_v2(state)
        semantic_before = semantic_plan_hash(state)
        record_before = plan_record_hash(state)
        state["plan_validation_state"]["validated_at"] = "later"
        self.assertEqual(semantic_plan_hash(state), semantic_before)
        self.assertEqual(plan_record_hash(state), record_before)

    def test_snapshot_is_content_addressed_and_reused_by_later_runtime_generation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = _state()
            refresh_state_contract_v2(state)
            approve_plan_state(state, 1)
            approved_snapshot = copy.deepcopy(state["approval_snapshot"])
            save_plan_state(root, "task-1", state, allow_canonical_task_write=True)
            first = load_plan_state(root, "task-1")
            first_ref = first["approval_snapshot_ref"]
            store = StateContractStore(root, "task-1")
            self.assertEqual(
                store.read_object(first_ref, expected_type="approval_snapshot"),
                approved_snapshot,
            )

            first["runtime_state"]["status"] = "running"
            save_plan_state(root, "task-1", first)
            second = load_plan_state(root, "task-1")
            self.assertGreater(second["state_generation"], first["state_generation"] - 1)
            self.assertEqual(second["approval_snapshot_ref"], first_ref)
            self.assertEqual(second["approval_snapshot"], approved_snapshot)


class AtomicGenerationTests(unittest.TestCase):
    def test_save_advances_generation_and_rejects_stale_writer(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = _state()
            save_plan_state(root, "task-1", state, allow_canonical_task_write=True)
            first = load_plan_state(root, "task-1")
            self.assertEqual(first["state_generation"], 1)
            stale = copy.deepcopy(first)
            first["task_state"]["status"] = "running"
            save_plan_state(root, "task-1", first)
            second = load_plan_state(root, "task-1")
            self.assertEqual(second["state_generation"], 2)
            stale["task_state"]["status"] = "failed"
            with self.assertRaisesRegex(ValueError, "STATE_CAS_CONFLICT"):
                save_plan_state(root, "task-1", stale)

            state_path = Path(root) / "task_outputs" / "task-1" / "plan_state.json"
            self.assertTrue(state_path.is_file())
            self.assertTrue(state_path.with_suffix(".json.lock").exists())


if __name__ == "__main__":
    unittest.main()

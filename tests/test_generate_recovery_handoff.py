from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from agent_core.workflow.execution import TypedGenerate
from agent_core.workflow.generate_handoff import project_corrective_generate_seed
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.recovery_runtime import RecoveryRuntime
from agent_core.workflow.stage_contracts import GenerateSeed, StageContractError
from tests.test_recovery_projection import _durable, _execute_failure


class _Store:
    def __init__(self, durable):
        self.value = copy.deepcopy(durable)

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(candidate["state_generation"]) + 1


def _directive():
    return {
        "schema_version": 1,
        "directive_id": "directive-handoff",
        "task_id": "task-recovery",
        "action": "retry_step",
        "summary": "The generated action failed.",
        "reason_code": "GENERATED_ACTION_FAILED",
        "target_step_id": "step-1",
        "instruction": "Regenerate only the failed action.",
        "evidence_refs": ["execution-1"],
    }


async def _admitted_fixture():
    durable = _durable()
    record = build_causal_failure_record(
        "execute_outcome", _execute_failure(), durable,
        task_id="task-recovery",
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    state = {
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"],
        "failure_source": record["failure_source"],
        "recovery_directive": _directive(),
        "recovery_archive_ref": "phase-archive:recovery-fixture",
    }
    store = _Store(durable)
    result = await RecoveryRuntime(
        data_path="/fixture", loader=store.load, saver=store.save
    ).run(state)
    return store, result["recovery_outcome"]


class GenerateRecoveryHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_contains_only_directive_refs_and_failure_identity(self) -> None:
        store, recovery_outcome = await _admitted_fixture()
        seed = project_corrective_generate_seed(recovery_outcome, store.value)
        wire = seed.to_dict()
        self.assertEqual(GenerateSeed.from_mapping(wire).to_dict(), wire)
        self.assertEqual(wire["recovery_directive"], _directive())
        self.assertEqual(wire["evidence_refs"], ["execution-1"])
        self.assertEqual(
            wire["recovery_archive_ref"], "phase-archive:recovery-fixture"
        )
        serialized = json.dumps(wire, ensure_ascii=False)
        for forbidden in (
            "You are the BioMind Recovery Agent",
            "recovery_seed",
            "ToolMessage",
            "stdout",
            "stderr",
            "candidate_hypotheses",
            "validator_feedback",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_typed_generate_receives_fresh_seed_not_recovery_episode(self) -> None:
        store, recovery_outcome = await _admitted_fixture()
        captured = []

        async def generator(state):
            captured.append(copy.deepcopy(state))
            return {
                "pending_action": {
                    "type": "execute",
                    "action_id": "action-regenerated",
                    "task_id": "task-recovery",
                    "step_id": "step-1",
                }
            }

        poisoned = {
            "task_id": "task-recovery",
            "current_step_id": "step-1",
            "recovery_outcome": recovery_outcome,
            "recovery_directive": _directive(),
            "recovery_seed": {"raw": "forbidden raw seed"},
            "recovery_tool_messages": ["forbidden tool observation"],
            "recovery_candidate_hypotheses": ["rejected hypothesis"],
            "recovery_validator_feedback": ["validator chatter"],
            "failure_packet": {"summary": "raw packet"},
            "failure_source": {"stdout": "raw stdout", "stderr": "raw stderr"},
            "messages": ["Recovery System", "ToolMessage(raw)"],
        }
        with patch(
            "agent_core.workflow.execution.load_plan_state",
            return_value=copy.deepcopy(store.value),
        ):
            result = await TypedGenerate(
                generator=generator, data_path="/fixture"
            ).run(poisoned)
        self.assertEqual(result["generate_outcome"]["type"], "action_ready")
        self.assertEqual(len(captured), 1)
        self.assertIn("generate_seed", captured[0])
        GenerateSeed.from_mapping(captured[0]["generate_seed"])
        serialized = json.dumps(captured[0], ensure_ascii=False)
        for forbidden in (
            "recovery_outcome", "recovery_seed", "recovery_tool_messages",
            "rejected hypothesis", "validator chatter", "raw stdout",
            "raw stderr", "Recovery System", "ToolMessage(raw)",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_missing_archive_or_tampered_receipt_fails_closed(self) -> None:
        store, recovery_outcome = await _admitted_fixture()
        receipt = store.value["task_state"]["typed_recovery_receipts"][
            "directive-handoff"
        ]
        receipt["recovery_archive_ref"] = ""
        with self.assertRaisesRegex(
            StageContractError, "GENERATE_RECOVERY_ARCHIVE_REF_MISSING"
        ):
            project_corrective_generate_seed(recovery_outcome, store.value)

        store, recovery_outcome = await _admitted_fixture()
        store.value["task_state"]["typed_recovery_receipts"][
            "directive-handoff"
        ]["directive"]["summary"] = "tampered"
        with self.assertRaisesRegex(
            StageContractError, "GENERATE_RECOVERY_DIRECTIVE_MISMATCH"
        ):
            project_corrective_generate_seed(recovery_outcome, store.value)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from langchain_core.messages import AIMessage

from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import build_causal_failure_record
from tests.test_recovery_projection import _durable, _execute_failure


class _Store:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.save_count = 0

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.save_count += 1
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(candidate["state_generation"]) + 1


class _Model:
    def __init__(self):
        self.requests = []

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            return AIMessage(content="", tool_calls=[{
                "id": "failure-call",
                "name": "failure_inspect",
                "args": {},
                "type": "tool_call",
            }])
        return AIMessage(content=json.dumps({
            "schema_version": 1,
            "directive_id": "directive-node",
            "task_id": "task-recovery",
            "action": "retry_step",
            "summary": "The failed generated action requires regeneration.",
            "reason_code": "GENERATED_ACTION_FAILED",
            "target_step_id": "step-1",
            "instruction": "Regenerate only the failed action.",
            "evidence_refs": ["execution-1"],
        }))


class Schema2RecoveryNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_runs_local_agent_then_deterministic_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="schema2-recovery-node-") as root:
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
                "messages": ["global transcript must not enter Recovery"],
            }
            store = _Store(durable)
            model = _Model()
            node = Schema2RecoveryNode(
                data_path=root,
                model=model,
                phase_store=PhaseArchiveStore(root),
                evidence_reader=RecoveryEvidenceReader(root),
                loader=store.load,
                saver=store.save,
            )
            result = await node.run(state)

            self.assertEqual(
                result["recovery_outcome"]["type"],
                "candidate_repair_required",
            )
            self.assertEqual(store.save_count, 1)
            self.assertEqual(len(model.requests), 2)
            self.assertNotIn("_provider_episode", result)
            self.assertNotIn("recovery_directive", result)
            serialized = json.dumps([
                [str(message.content) for message in request]
                for request in model.requests
            ])
            self.assertNotIn("global transcript must not enter Recovery", serialized)

    async def test_node_missing_packet_fails_without_calling_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="schema2-recovery-node-") as root:
            store = _Store(_durable())
            model = _Model()
            result = await Schema2RecoveryNode(
                data_path=root,
                model=model,
                loader=store.load,
                saver=store.save,
            ).run({"task_id": "task-recovery"})
            self.assertEqual(
                result["recovery_outcome"]["payload"]["error_code"],
                "RECOVERY_CAUSAL_PACKET_MISSING",
            )
            self.assertEqual(model.requests, [])


if __name__ == "__main__":
    unittest.main()

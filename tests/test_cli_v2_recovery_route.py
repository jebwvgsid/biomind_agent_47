"""Synthetic production Recovery admission/route equivalence under the export guard.

The only state loader is the in-memory fixture below. Archive and lifecycle
stores receive fresh pytest scratch roots; no original session is available.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import io
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from agent_core.context_lineage import ContextLineageStore
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.recovery_tools import RecoveryLocalToolDispatcher
from agent_core.workflow.runtime_graph import route_recovery_outcome
from agent_core.workflow.scoped_context import provider_visible_payload
from bio_agents import BioAnalystAgent
from tests.test_cli_model_progress_integration import artifact
from tests.test_cli_v2_display import service_writer_callbacks
from tests.test_recovery_projection import _durable, _execute_failure


DIRECTIVE = {
    "schema_version": 1,
    "directive_id": "synthetic-v2-revise-plan",
    "task_id": "task-recovery",
    "action": "revise_plan",
    "summary": "当前已批准步骤的输出约束需要修订。",
    "reason_code": "CONTRACT_DRIFT",
    "target_step_id": "step-1",
    "instruction": "核对当前步骤的输出与选中能力合同，再提交计划审批。",
    "evidence_refs": ["execution-1"],
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class MemoryStore:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.saved = []

    def load(self, _data_path, task_id):
        assert task_id == "task-recovery"
        return deepcopy(self.value)

    def save(self, _data_path, task_id, candidate):
        assert task_id == "task-recovery"
        assert candidate["state_generation"] == self.value["state_generation"]
        self.saved.append(deepcopy(candidate))
        self.value = deepcopy(candidate)
        self.value["state_generation"] += 1


class FixedProvider:
    def __init__(self):
        self.requests = []
        self.schema_hashes = []

    def bind_tools(self, schemas):
        self.schema_hashes.append(digest(schemas))
        return self

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append([provider_visible_payload(item) for item in messages])
        if len(self.requests) == 1:
            return AIMessage(id="synthetic-v2-recovery-1",
                content="我先读取本次失败的现有回执。",
                tool_calls=[{"id": "synthetic-v2-failure-read", "name": "failure_inspect",
                             "args": {}, "type": "tool_call"}],
                response_metadata={"finish_reason": "tool_calls"})
        assert len(self.requests) == 2, "Display must not create another model call"
        return AIMessage(id="synthetic-v2-recovery-2",
            content="回执已读到；我将提交对当前计划约束的修订指令。",
            tool_calls=[{"id": "synthetic-v2-native-submit", "name": "submit_recovery_directive",
                         "args": deepcopy(DIRECTIVE), "type": "tool_call"}],
            response_metadata={"finish_reason": "tool_calls"})


async def scenario(root, mode, durable, state):
    writer = io.StringIO()
    owner = PlainTerminal(writer, enabled=mode != "off",
                          verbosity="verbose" if mode == "verbose" else "compact")
    previous = bind_terminal(owner)
    store = MemoryStore(durable)
    model = FixedProvider()
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = str(root)
    events, dispatches = [], []
    original_dispatch = RecoveryLocalToolDispatcher.dispatch_full

    def observed_dispatch(dispatcher, name, arguments):
        # Spy on the actual safe reader; do not substitute a result or execute
        # the submitted semantic directive as a local tool.
        result = original_dispatch(dispatcher, name, arguments)
        dispatches.append({"tool": name, "arguments": deepcopy(arguments),
                           "result_sha256": digest(result)})
        return result

    def production_sink(event, payload):
        events.append({"event": event, "payload": deepcopy(payload)})
        agent._schema2_recovery_event_sink(event, payload)

    node = Schema2RecoveryNode(data_path=str(root), model=model,
        phase_store=PhaseArchiveStore(str(root)),
        evidence_reader=RecoveryEvidenceReader(str(root)),
        loader=store.load, saver=store.save, event_sink=production_sink)
    try:
        with patch.object(RecoveryLocalToolDispatcher, "dispatch_full", observed_dispatch):
            result = await node.run(deepcopy(state))
        route = route_recovery_outcome(result)
        await owner.drain(timeout=1)
        assert result["recovery_outcome"]["type"] == "plan_revision_required", result
        assert route == "planner"
        assert len(model.requests) == 2
        assert [row["tool"] for row in dispatches] == ["failure.inspect"]
        assert len(model.schema_hashes) == 1
        assert len(store.saved) == 1

        task_state = store.value["task_state"]
        receipt = task_state["typed_recovery_receipts"][DIRECTIVE["directive_id"]]
        assert receipt["directive"] == DIRECTIVE
        assert receipt["outcome"] == result["recovery_outcome"]
        admission = receipt["reception"]["admission"]
        assert admission == {"status": "admitted", "outcome_type": "plan_revision_required",
                             "command_started": False, "directive_id": DIRECTIVE["directive_id"]}
        budget = result["recovery_outcome"]["payload"]["budget_receipt"]
        assert budget["consumed"] is True and budget["ordinal"] == 1
        assert budget["remaining"] == 1
        assert list(task_state["typed_recovery_budget"].values()) == [1]
        for key in ("current_plan", "current_task", "steps"):
            assert store.value[key] == durable[key]

        kinds = [row["event"] for row in events]
        assert kinds.count("recovery_tool_started") == 1
        assert kinds.count("recovery_tool_observation_committed") == 1
        assert kinds.count("recovery_directive_admitted") == 1
        assert kinds.index("recovery_tool_observation_committed") < kinds.index("recovery_directive_admitted")
        checkpoint = next(row["payload"] for row in events
                          if row["event"] == "recovery_tool_observation_committed")
        assert checkpoint["status"] == "completed"
        assert model.requests[1][-1] == checkpoint["message"]
        assert checkpoint["observation"]["result"]["failure_packet"]["failure_id"] == state["failure_packet"]["failure_id"]
        persisted_events = [json.loads(line) for line in
            (ContextLineageStore(str(root), "task-recovery").root / "events.jsonl").read_text().splitlines()]
        persisted_checkpoint = next(row["payload"] for row in persisted_events
                                   if row["event_kind"] == "recovery_tool_observation_committed")
        assert persisted_checkpoint["message"] == checkpoint["message"]
        assert persisted_checkpoint["observation"] == checkpoint["observation"]
        archive = node.phase_store.read(receipt["recovery_archive_ref"])
        assert archive.summary["terminal_state"] == "directive_ready"
        assert archive.metadata["hidden_reasoning_excluded"] is True

        transcript = writer.getvalue()
        if mode == "off":
            assert transcript == ""
        else:
            assert "恢复指令已接纳" in transcript
            assert "本次失败的现有回执" in transcript
            assert "submit_recovery_directive" not in transcript
            assert "任务成功" not in transcript

        business = {"provider_request_sha256": [digest(row) for row in model.requests],
                    "provider_call_count": len(model.requests), "schema_hashes": model.schema_hashes,
                    "actual_dispatches": dispatches,
                    "actual_directive": receipt["directive"],
                    "directive_digest": receipt["directive_digest"],
                    "admission": admission, "typed_outcome": result["recovery_outcome"],
                    "budget_ledger": task_state["typed_recovery_budget"],
                    "route_from_production_function": route, "save_count": len(store.saved)}
        artifact("cli-v2-recovery-route-" + mode + ".json", {
            "fixture": "synthetic; no original session or executable scientific task",
            "source": "Schema2RecoveryNode -> RecoveryAgentLoop -> RecoveryLocalToolDispatcher.failure.inspect -> RecoveryRuntime -> route_recovery_outcome",
            "mode": mode, "business": business, "event_kinds": kinds,
            "persisted_tool_message_sha256": digest(persisted_checkpoint["message"]),
            "persisted_tool_observation_sha256": digest(persisted_checkpoint["observation"]),
            "archive_terminal_state": archive.summary["terminal_state"],
            "transcript": transcript,
            "limits": "Checks accepted route selection; does not invoke Planner or prove its next provider request contains the directive."})
        return business
    finally:
        bind_terminal(previous)
        owner.close()


def test_v2_actual_recovery_admission_budget_and_graph_route_are_display_independent(tmp_path):
    durable = _durable()
    record = build_causal_failure_record("execute_outcome", _execute_failure(), durable,
                                         task_id="task-recovery")
    durable["task_state"]["causal_failure_packets"] = {record["packet_ref"]: record}
    durable["task_state"]["active_failure_packet_ref"] = record["packet_ref"]
    state = {"task_id": "task-recovery", "active_failure_packet_ref": record["packet_ref"],
             "failure_packet": record["failure_packet"], "failure_source": record["failure_source"]}

    async def run():
        service_writer_callbacks(asyncio.get_running_loop())
        values = []
        for mode in ("compact", "verbose", "off"):
            root = tmp_path / mode
            root.mkdir()
            values.append(await scenario(root, mode, durable, state))
        assert values[1:] == [values[0], values[0]]
    asyncio.run(run())

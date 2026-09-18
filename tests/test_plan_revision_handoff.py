"""Actual admitted Recovery -> scoped Planner request on fresh offline tasks."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import io
import json
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from agent_core.plan_tracker import load_plan_state, save_plan_state, semantic_plan_hash
from agent_core.planner_revision import PlannerRevisionContextError, accepted_planner_revision
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.workflow.outcomes import VerificationOutcome
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.runtime_graph import route_planner_outcome, route_recovery_outcome
from tests.ger_fixture_support import configure_protocol
from tests.test_cli_model_progress_integration import artifact


INSTRUCTION = "保留当前已批准计划和完成进度，仅修正当前检查步骤与能力的输出矛盾。"
FACT = "Formal provenance is expected despite accepted file collection."


class RevisionModel:
    def __init__(self, task_id, step_id):
        self.requests = []
        self.directive = {"schema_version": 1, "directive_id": "synthetic-revision-1",
            "task_id": task_id, "action": "revise_plan", "summary": "模型判断：当前检查输出需要修订。",
            "reason_code": "CONTRACT_DRIFT", "target_step_id": step_id,
            "instruction": INSTRUCTION, "evidence_refs": ["synthetic-validation"]}

    def bind_tools(self, schemas):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.requests.append(deepcopy(list(messages)))
        assert len(self.requests) == 1
        return AIMessage(content="我将基于当前计划提交这项修订。", tool_calls=[{
            "id": "synthetic-native-revision", "name": "submit_recovery_directive",
            "args": deepcopy(self.directive), "type": "tool_call",
        }], response_metadata={"finish_reason": "tool_calls"})


async def _scenario(mode):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    case = PlanLocalRealGraphTests(methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider")
    await case.asyncSetUp()
    configure_protocol(case)
    case._configure_one_step_no_output_plan()
    original_plan = case._planner_payload
    def plan_payload():
        payload = original_plan()
        payload["steps"][0]["produces_observations"] = ["approved_command_succeeded"]
        return payload
    case._planner_payload = plan_payload
    owner = PlainTerminal(io.StringIO(), enabled=mode != "off", verbosity="verbose" if mode == "verbose" else "compact")
    previous = bind_terminal(owner)
    requests = []
    initial = True
    revision_calls = 0
    async def provider(messages, *, stage, node_context=None, **kwargs):
        nonlocal revision_calls
        requests.append({"stage": stage, "initial": initial, "messages": [
            {"type": message.type, "content": deepcopy(message.content)} for message in messages],
            "payload": node_context.llm_payload.to_dict() if node_context else {}})
        if stage == "consultant":
            return AIMessage(content=json.dumps(case._consultant_payload(case.root / "unused.md")))
        if stage.startswith("planner"):
            if not initial:
                revision_calls += 1
                if revision_calls == 1:
                    # The real compiler must reject this first candidate; the
                    # subsequent provider request must retain the revision.
                    candidate = case._planner_payload()
                    candidate["steps"][0]["outputs"] = ["undeclared-result"]
                    return AIMessage(content=json.dumps(candidate))
            return AIMessage(content=json.dumps(case._planner_payload()))
        if stage == "generate":
            return AIMessage(content="<shell>printf 'synthetic candidate\\n'</shell>")
        raise AssertionError(stage)
    case.agent._ainvoke_node_llm = provider
    case.agent._ainvoke_llm = provider
    task_id = "synthetic-plan-revision"
    try:
        runner = case._runner_state(task_id)
        app = case.agent._app_for_task(task_id, runner)
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 20}
        stream = app.astream(runner, config=config, stream_mode="updates")
        events = []
        try:
            async for event in stream:
                events.append(event)
                if event.get("generate", {}).get("generate_outcome", {}).get("type") == "action_ready":
                    break
        finally:
            await stream.aclose()
        assert any(row.get("generate", {}).get("generate_outcome", {}).get("type") == "action_ready" for row in events), events
        state = dict(app.get_state(config).values)
        durable = load_plan_state(case.root, task_id)
        assert accepted_planner_revision(durable) == {}
        original_hash = semantic_plan_hash(durable)
        original_approval = deepcopy(durable.get("current_approval") or durable.get("approval_record"))
        step_id = durable["current_step_id"]
        initial_calls = int(durable["task_state"]["planning_epoch"]["planner_calls_total"])
        assert all(INSTRUCTION not in json.dumps(row["messages"], ensure_ascii=False) for row in requests)
        source = VerificationOutcome(schema_version=1, type="rejected", summary=FACT,
            scope={"task_id": task_id, "plan_id": durable["plan_id"], "plan_version": durable["plan_version"],
                   "step_id": step_id, "validation_id": "synthetic-validation"},
            payload={"validation_evidence": {"validation_id": "synthetic-validation"},
                     "completion_gate": {"passed": False, "classification": "step_effect_incomplete", "message": FACT},
                     "contract_violations": [{"code": "SYNTHETIC_EFFECT_CONFLICT", "message": FACT}],
                     "failure_owner": "PLAN"}, evidence_refs=("synthetic-validation",)).to_dict()
        record = build_causal_failure_record("verification_outcome", source, durable, task_id=task_id)
        durable["task_state"]["causal_failure_packets"] = {record["packet_ref"]: record}
        durable["task_state"]["active_failure_packet_ref"] = record["packet_ref"]
        save_plan_state(case.root, task_id, durable)
        model = RevisionModel(task_id, step_id)
        node = Schema2RecoveryNode(data_path=str(case.root), model=model,
            phase_store=PhaseArchiveStore(str(case.root)), evidence_reader=RecoveryEvidenceReader(str(case.root)))
        recovery = await node.run({**state, "active_failure_packet_ref": record["packet_ref"],
            "failure_packet": record["failure_packet"], "failure_source": record["failure_source"]})
        assert route_recovery_outcome(recovery) == "planner", recovery
        admitted = load_plan_state(case.root, task_id)
        revision = accepted_planner_revision(admitted)
        assert revision["directive"]["instruction"] == INSTRUCTION
        assert semantic_plan_hash(admitted) == original_hash
        assert (admitted.get("current_approval") or admitted.get("approval_record")) == original_approval
        # Scope/body negatives consume the actual admitted record; no fake
        # handoff is inserted into graph state or provider messages.
        for key in ("task_id", "plan_id", "plan_version", "current_step_id"):
            stale = deepcopy(admitted); stale[key] = "different-scope"
            assert accepted_planner_revision(stale) == {}
        stale = deepcopy(admitted)
        receipt = stale["task_state"]["typed_recovery_receipts"][model.directive["directive_id"]]
        receipt["reception"]["admission"]["status"] = "not_admitted"
        assert accepted_planner_revision(stale) == {}
        missing = deepcopy(admitted)
        missing["task_state"]["typed_recovery_receipts"][model.directive["directive_id"]]["directive"]["instruction"] = ""
        with pytest.raises(PlannerRevisionContextError, match="PLANNER_REVISION_BODY_MISSING"):
            accepted_planner_revision(missing)
        import bio_agents
        calls_before_missing = len(requests)
        with patch.object(bio_agents, "load_plan_state", return_value=missing):
            with pytest.raises(PlannerRevisionContextError, match="PLANNER_REVISION_BODY_MISSING"):
                await case.agent._schema2_early_node_handlers["planner"]({**state, **recovery})
        assert len(requests) == calls_before_missing
        initial = False
        # The same production scoped wrapper used by the graph performs local
        # correction rounds and freezes/reuses its phase seed.
        update = await case.agent._schema2_early_node_handlers["planner"]({**state, **recovery})
        if route_planner_outcome(update) != "plan_review":
            artifact("plan-revision-debug-" + mode + ".json", {"requests": requests,
                "update": update, "admitted": admitted, "recovery": recovery})
        assert route_planner_outcome(update) == "plan_review", update
        revised_requests = [row for row in requests if not row["initial"] and row["stage"].startswith("planner")]
        assert len(revised_requests) == 2, revised_requests
        for row in revised_requests:
            text = json.dumps(row["messages"], ensure_ascii=False)
            assert text.count(INSTRUCTION) == 1, text
            assert FACT in text and "SYNTHETIC_EFFECT_CONFLICT" in text
            planning = row["payload"]["current_focus"]["planning"]
            assert planning["revision"]["mode"] == "revise_current_plan"
            assert planning["revision"]["instruction"] == INSTRUCTION
            assert "Create an approved plan-local work directory" in text
            assert "approved_command_succeeded" in text
        assert revised_requests[0]["payload"] != revised_requests[1]["payload"]
        now = load_plan_state(case.root, task_id)
        assert int(now["task_state"]["planning_epoch"]["planner_calls_total"]) >= initial_calls + 2
        assert not (now.get("provenance_state") or {}).get("attempts")
        approval = await case.agent._schema2_early_node_handlers["plan_review"]({**state, **recovery, **update})
        assert approval["plan_review_outcome"]["type"] == "approved_for_execution", approval
        await owner.drain(timeout=1)
        result = {"recovery_calls": len(model.requests), "revision_planner_calls": len(revised_requests),
                  "executions": 0, "recovery_route": "planner", "planner_route": "plan_review",
                  "approval_outcome": approval["plan_review_outcome"]["type"]}
        artifact("plan-revision-handoff-" + mode + ".json", {"counts": result,
            "requests": requests, "directive": model.directive, "recovery_outcome": recovery["recovery_outcome"],
            "failure_source": record["failure_source"]})
        return result
    finally:
        bind_terminal(previous)
        owner.close()
        await case.asyncTearDown()


def test_r1_r2_r3_actual_admission_reaches_planner_and_correction_with_scope_isolation():
    async def run():
        results = [await _scenario(mode) for mode in ("compact", "verbose", "off")]
        assert results[1:] == [results[0], results[0]]
    asyncio.run(run())

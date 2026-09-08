"""Real diagnostic approval wait, rejection and explicit outer cancellation.

The sole executed primary command is an external runner substitute. No diagnostic
program is dispatched. Planner, review, store, Recovery, Generate, approval and
the outer interrupt loop are production components over isolated synthetic data.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from copy import deepcopy
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import bio_agents
from agent_core.human_io import HumanInputUnavailable
from agent_core.plan_tracker import load_plan_state, continue_recovery_generation_after_code_review
from agent_core.workflow.managed_stop import current_managed_stop
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from execution.profiling import RunProfiler
from tests import test_reliability_diagnostic_boundaries as boundary
from tests import test_reliability_diagnostic_workflow as base
from tests.test_common_finalization_workflow import SummaryModel


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_diagnostic_approval_wait_then_reject_or_cancel(tmp_path, decision):
    async def scenario():
        fixture = base.gen_fixture.Session1788681718GenerateProductionPathTests(
            methodName="test_adm_01_saved_candidate_reaches_fake_executor_once")
        await fixture.asyncSetUp()
        evidence = tmp_path / "evidence"
        task_id = "diagnostic-approval-" + decision
        events, dispatches, human_requests, go_inputs = [], [], [], []
        model = None
        summary = SummaryModel("ok")
        try:
            _, database, argv = boundary._prepare_sqlite_before_selection(fixture, "sqlite")
            original_sha = hashlib.sha256(database.read_bytes()).hexdigest()
            state = await boundary._approve_with_scoped_report_usage(fixture, task_id, evidence)
            first = json.loads(fixture.saved_response_content.strip().removeprefix("```json").removesuffix("```"))
            code = shlex.join(argv)
            probe = {"type": "execute", "language": "bash", "purpose": "probe",
                     "operation": "check_contig_compatibility", "argv": argv,
                     "code": code, "env": {}, "outputs": []}
            fixture.provider.responses = [json.dumps(first), json.dumps(probe), json.dumps(probe)]
            fixture.provider.calls.clear()
            fixture.agent.middleware.auto_approve_safe = False
            model = boundary._BoundaryRecovery(fixture, task_id, evidence, "approval_boundary")
            recovery = Schema2RecoveryNode(data_path=str(fixture.root), model=model,
                phase_store=fixture.agent.phase_archive_store, skill_service=fixture.service)
            configured = {**{name: fixture.agent._schema2_runtime_node_handlers[name]
                for name in ("generate", "execute", "supervisor", "commit_step", "final_audit")},
                "recovery": recovery.run}
            def observed(name, handler):
                async def invoke(node_state):
                    result = await handler(node_state)
                    events.append({"node": name, "result": deepcopy(result)})
                    base._save(evidence / "graph-events.json", events)
                    if name == "execute" and result.get("execute_outcome", {}).get("type") == "approval_rejected":
                        after_rejection = load_plan_state(fixture.root, task_id)
                        base._save(evidence / "durable-after-approval-rejection.json", after_rejection)
                        assert after_rejection["task_state"]["budgets"] == before["task_state"]["budgets"]
                        assert after_rejection["task_state"]["typed_recovery_budget"] == before["task_state"]["typed_recovery_budget"]
                    return result
                return invoke
            graph = build_schema2_runtime_graph({name: observed(name, handler)
                for name, handler in configured.items()}).compile(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": task_id}, "recursion_limit": 25}
            async def runner(_path, *, lifecycle_callback=None, **_kwargs):
                pending = load_plan_state(fixture.root, task_id)["task_state"]["pending_action"]
                envelope = pending.get("recovery_execution_envelope") or {}
                assert envelope.get("result_contract") != "DIAGNOSTIC_EVIDENCE", "unapproved diagnostic dispatched"
                dispatches.append({"action_id": pending["action_id"], "kind": "synthetic_primary_failure"})
                assert len(dispatches) == 1, "approval response must not dispatch a primary repair"
                if lifecycle_callback:
                    await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
                    await lifecycle_callback("PROCESS_EXITED", {"returncode": 1})
                return "", "Synthetic primary execution failed before this diagnostic approval.", 1
            fixture.agent.terminal_summary_llm = summary
            fixture.agent.terminal_summary_policy = {"enabled": True, "timeout_seconds": 1}
            fixture.agent.state_manager = None
            fixture.agent.profiler = RunProfiler(fixture.root / "task_outputs")
            fixture.agent.profiler.start_task(task_id)
            fixture.agent._app_for_task = lambda _task_id: graph
            started = False
            async def go(value, **_kwargs):
                nonlocal started
                go_inputs.append(str(value))
                if not started:
                    started = True
                    await graph.ainvoke(state, config)
                    primary = graph.get_state(config)
                    assert primary.tasks[0].interrupts[0].value["type"] == "code_review"
                    base._save(evidence / "primary-approved-interrupt.json", primary.tasks[0].interrupts[0].value)
                    await graph.ainvoke(Command(resume="y"), config)
                elif isinstance(value, Command):
                    await graph.ainvoke(value, config)
                else:
                    # Resume of a saved wait only inspects the actual checkpoint;
                    # the outer loop owns the subsequent user response Command.
                    assert graph.get_state(config).tasks[0].interrupts
            fixture.agent.go = go
            async def prompt(_task_id, prompt_type, _text, **kwargs):
                assert prompt_type == "code_review"
                payload = kwargs["payload"]
                human_requests.append(deepcopy(payload))
                assert payload["editable_content"] == code
                assert code in payload["content"]
                if len(human_requests) in (1, 3):
                    raise HumanInputUnavailable("isolated saved approval wait")
                assert len(human_requests) == 2
                return "q" if decision == "cancel" else "n:Do not run this diagnostic candidate."
            fixture.agent._aprompt_user = prompt
            with patch.object(bio_agents, "run_script_path", new=runner):
                waiting = await fixture.agent._run_agent_interrupt_loop("synthetic approval boundary", task_id)
                assert waiting.status == "waiting_user"
                before = load_plan_state(fixture.root, task_id)
                base._save(evidence / "durable-at-diagnostic-wait.json", before)
                pending = before["task_state"]["pending_action"]
                assert pending["recovery_execution_envelope"]["result_contract"] == "DIAGNOSTIC_EVIDENCE"
                assert len(dispatches) == 1 and len(fixture.provider.calls) == 2
                assert not current_managed_stop(before) and not summary.requests
                assert before["task_state"]["typed_recovery_budget"] == {model.incident_id: 1}
                negative_receipts = []
                if decision == "reject":
                    for field, value, expected in (
                        ("step_id", "step.not_the_approved_scope", "RECOVERY_APPROVAL_SCOPE_STALE"),
                        ("candidate_generation_event_id", "generate-candidate:stale-request", "RECOVERY_APPROVAL_CANDIDATE_MISMATCH"),
                    ):
                        probe_state = deepcopy(before)
                        wrong_action = deepcopy(pending)
                        wrong_action[field] = value
                        unchanged = deepcopy(probe_state)
                        with pytest.raises(ValueError, match="^" + expected + "$"):
                            continue_recovery_generation_after_code_review(
                                probe_state, action=wrong_action, decision="rejected",
                                guidance="Do not run this diagnostic candidate.")
                        assert probe_state == unchanged
                        negative_receipts.append({"field": field, "actual": value,
                            "error_code": expected, "state_unchanged": True})
                    base._save(evidence / "real-wait-snapshot-negative-claims.json", negative_receipts)
                    after_negative = load_plan_state(fixture.root, task_id)
                    # Reload refreshes the independent append-only audit head;
                    # it does not belong to the authoritative PlanState CAS.
                    assert {key: value for key, value in after_negative.items() if key != "audit_index"} == {
                        key: value for key, value in before.items() if key != "audit_index"}
                result = await fixture.agent._run_agent_interrupt_loop(None, task_id, resume_initial=True)
            durable = load_plan_state(fixture.root, task_id)
            assert len(dispatches) == len(model.requests) == 1
            assert not summary.requests and not current_managed_stop(durable)
            assert not durable["task_state"].get("typed_step_commit_receipts")
            assert durable["current_step_id"] == "step.check_contigs"
            assert durable["task_state"]["typed_recovery_budget"] == before["task_state"]["typed_recovery_budget"]
            assert not any(row["node"] in {"supervisor", "commit_step"} for row in events)
            attempts = durable["provenance_state"]["attempts"].values()
            started_attempts = [row for row in attempts if row.get("started_at")]
            assert len(started_attempts) == 1 and started_attempts[0]["returncode"] == 1
            if decision == "cancel":
                assert result.status == "cancelled"
                cancellation = durable["task_state"]["cancellation_receipt"]
                assert cancellation["initiator_kind"] == "user"
                assert cancellation["source"] == "interrupt_prompt"
                assert len(human_requests) == len(fixture.provider.calls) == 2
            else:
                assert result.status == "waiting_user"
                rejections = [row["result"]["execute_outcome"] for row in events
                    if row["node"] == "execute" and row["result"]["execute_outcome"]["type"] == "approval_rejected"]
                assert len(rejections) == 1
                assert rejections[0]["payload"]["command_started"] is False
                assert "Do not run this diagnostic candidate" in rejections[0]["payload"]["guidance"]
                assert len(human_requests) == len(fixture.provider.calls) == 3
                assert graph.get_state(config).tasks[0].interrupts[0].value["type"] == "code_review"
                assert not durable["task_state"].get("cancellation_receipt")
                feedback = [row for message in fixture.provider.calls[2]
                    if message.get("type") == "human" for row in base._json_objects(message.get("content"))
                    if row.get("kind") == "code_review_feedback"]
                assert feedback == [{"kind": "code_review_feedback", "decision": "rejected",
                    "guidance": "Do not run this diagnostic candidate.", "command_started": False}]
                before_claim = before["task_state"]["recovery_state"]["generation_claim"]
                after_recovery = durable["task_state"]["recovery_state"]
                after_claim = after_recovery["generation_claim"]
                assert after_claim["target_generation_id"] != before_claim["target_generation_id"]
                assert after_claim["parent_target_generation_id"] == before_claim["target_generation_id"]
                assert after_claim["status"] == "action_adopted"
                history = [row for row in after_recovery["generation_claim_history"]
                    if row.get("target_generation_id") == before_claim["target_generation_id"]]
                assert len(history) == 1 and history[0]["status"] == "superseded"
                receipt = history[0]["approval_feedback"]
                assert receipt == after_claim["approval_feedback"]
                assert receipt["source"] == "execute.code_review"
                assert receipt["decision"] == "rejected" and receipt["command_started"] is False
                assert receipt["guidance"] == feedback[0]["guidance"]
                assert receipt["action_id"] == pending["action_id"]
                assert receipt["candidate_event_id"] == pending["candidate_generation_event_id"]
                assert receipt["approval_identity"] == pending["action_approval_identity"]
                assert receipt["parent_target_generation_id"] == before_claim["target_generation_id"]
                assert after_claim["approval_feedback_event_id"] == history[0]["approval_feedback_event_id"]
                assert after_recovery["active_recovery_envelope"] == before["task_state"]["recovery_state"]["active_recovery_envelope"]
                base._save(evidence / "human-feedback-actual-provider-and-claim-receipt.json", {
                    "actual_provider_call_ordinal": 3, "feedback": feedback,
                    "request_sha256": hashlib.sha256(json.dumps(fixture.provider.calls[2],
                        sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                    "before_claim": before_claim, "after_claim": after_claim,
                    "superseded_claim": history[0], "negative_scope_receipts": negative_receipts,
                    "revocation_cas_budget_preserved": True})
            assert hashlib.sha256(database.read_bytes()).hexdigest() == original_sha
            base._save(evidence / "acceptance.json", {"status": "passed", "decision": decision,
                "initial_outer_status": waiting.status, "final_outer_status": result.status,
                "generate_calls": len(fixture.provider.calls), "recovery_calls": len(model.requests),
                "primary_dispatches": 1, "diagnostic_dispatches": 0, "primary_repair_dispatches": 0,
                "main_step_commits": 0, "summary_calls": 0, "input_sha256": original_sha,
                "input_unchanged": True, "budget_before": before["task_state"]["typed_recovery_budget"],
                "budget_after": durable["task_state"]["typed_recovery_budget"]})
        finally:
            path = fixture.root / "task_outputs" / task_id / "plan_state.json"
            if path.exists():
                base._save(evidence / "durable-before-teardown.json", json.loads(path.read_text()))
            base._save(evidence / "generate-requests.json", fixture.provider.calls)
            base._save(evidence / "human-requests.json", human_requests)
            base._save(evidence / "dispatches.json", dispatches)
            base._save(evidence / "go-inputs.json", go_inputs)
            base._save(evidence / "summary-requests.json", summary.requests)
            await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(), timeout=75))

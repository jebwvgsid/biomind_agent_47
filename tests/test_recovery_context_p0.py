from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage

import agent_core.middleware as middleware_module
import bio_agents
from agent_core.diagnostics import RecoveryContext
from agent_core.node_context import (
    ControlMetadataLeakError,
    ControllerBinding,
    InvocationRegistry,
    LLMPayload,
    NodeContext,
    build_node_context,
)
from agent_core.provider_message_safety import assert_v2_llm_messages_clean
from agent_core.recovery import RecoveryAction, RecoveryStateStore, RecoveryTrigger
from agent_core.recovery_evidence import (
    RecoveryEvidenceAccessError,
    RecoveryEvidenceReader,
    recovery_evidence_root,
)
from agent_core.recovery_provider import (
    build_recovery_evidence_followup,
    guarded_recovery_provider_messages,
)
from agent_core.state_contract_store import StateContractStore
from bio_agents import BioAnalystAgent


def _task_spec() -> dict:
    return {
        "task_id": "task-context-p0",
        "session_id": "session-context-p0",
        "goal": "recover the failed annotation action",
        "requirements": ["preserve the requested output path"],
        "inputs": [],
        "outputs": [],
    }


def _plan_state() -> dict:
    spec = _task_spec()
    return {
        "task_id": spec["task_id"],
        "session_id": spec["session_id"],
        "plan_id": "plan-context-p0",
        "plan_version": "plan-v2",
        "plan_revision": 4,
        "state_generation": 7,
        "current_step_id": "step-2",
        "current_task": {"task_spec": deepcopy(spec)},
        "task_spec": deepcopy(spec),
        "task_state": {"budgets": {}},
        "steps": [
            {
                "step_id": "step-1",
                "status": "done",
                "title": "validate inputs",
                "dependencies": [],
                "inputs": [],
                "outputs": [],
            },
            {
                "step_id": "step-2",
                "status": "running",
                "title": "annotate variants",
                "dependencies": ["step-1"],
                "inputs": [],
                "outputs": [],
            },
        ],
    }


def _approved_plan() -> dict:
    plan = _plan_state()
    return {
        "plan_snapshot_id": "approved-context-p0",
        "plan_id": plan["plan_id"],
        "plan_version": plan["plan_version"],
        "plan_revision": plan["plan_revision"],
        "steps": deepcopy(plan["steps"]),
    }


def _history_row(
    marker: str,
    *,
    step_id: str = "step-2",
    plan_revision: int = 4,
    scoped: bool = True,
) -> dict:
    row = {
        "attempt_id": f"attempt-{marker}",
        "source_attempt_id": f"attempt-{marker}",
        "source_action_id": "action-current",
        "step_id": step_id,
        "action_type": "execute",
        "error_fingerprint": "failure-current",
        "recovery_decision": {
            "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
            "failure_owner": "GENERATED_ACTION",
            "failure_type": "script_bug",
            "root_cause": marker,
        },
    }
    if scoped:
        row.update({
            "task_id": "task-context-p0",
            "session_id": "session-context-p0",
            "plan_id": "plan-context-p0",
            "plan_version": "plan-v2",
            "plan_revision": plan_revision,
        })
    return row


def _decision_payload() -> dict:
    return {
        "schema_version": 4,
        "verdict": "repair",
        "failure_class": "SCRIPT_BUG",
        "failure_owner": "GENERATED_ACTION",
        "failure_type": "script_bug",
        "error_is_real": True,
        "primary_execution_succeeded": False,
        "evidence_sufficient": True,
        "confidence": 0.95,
        "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
        "rerun_required": False,
        "consume_retry_budget": False,
        "user_notification_required": False,
        "reason": "repair the bounded failed action",
        "root_cause": "the generated action used an invalid option",
        "evidence": ["stderr-bounded"],
        "evidence_refs": ["stderr-bounded"],
        "machine_findings_considered": [],
        "missing_evidence": [],
        "repair_strategy": "change only the invalid option",
        "repair_target": "failed action",
        "repair_instruction": "remove the invalid option",
        "next_action": RecoveryAction.PATCH_SCRIPT.value,
        "requires_user_input": False,
    }


class RecoveryHistoryScopeTests(unittest.TestCase):
    def test_provider_history_is_step_plan_failure_scoped_bounded_and_single(self):
        attempts = [
            _history_row("STEP1_UNRELATED", step_id="step-1"),
            _history_row("LEGACY_UNSCOPED", scoped=False),
            _history_row("WRONG_PLAN", plan_revision=3),
            *[_history_row(f"STEP2_RELEVANT_{index}") for index in range(1, 5)],
        ]
        plan = _plan_state()
        original_plan = deepcopy(plan)

        with tempfile.TemporaryDirectory() as data_path:
            store = RecoveryStateStore(data_path, "task-context-p0")
            for row in attempts:
                store.save_attempt_mapping(row)
            durable_before = store.load_attempts()
            context = build_node_context(
                node_type="recovery",
                task_spec=_task_spec(),
                plan_state=plan,
                state_generation=7,
                current_step_id="step-2",
                action_identity="action-current",
                attempt_id="attempt-current",
                failure_fingerprint="failure-current",
                approved_plan_snapshot=_approved_plan(),
                recovery_decisions=durable_before,
                runtime={
                    "recovery_focus": {
                        "previous_action": {"action_type": "execute"},
                        "previous_recovery": [
                            {"root_cause": "DUPLICATE_HISTORY_MUST_NOT_RENDER"}
                        ],
                        "previous_recovery_summary": [
                            {"root_cause": "DUPLICATE_SUMMARY_MUST_NOT_RENDER"}
                        ],
                    }
                },
            )
            durable_after = store.load_attempts()

        payload = context.llm_payload.to_provider_dict()
        prompt = str(
            guarded_recovery_provider_messages(
                payload,
                stage="test.recovery.history.provider_dispatch",
            )[0].content
        )
        history = payload["semantic_history"]["accepted_recovery_decisions"]

        self.assertEqual(durable_after, durable_before)
        self.assertEqual(len(durable_after), len(attempts))
        self.assertEqual(len(history), 3)
        self.assertNotIn("STEP1_UNRELATED", prompt)
        self.assertNotIn("LEGACY_UNSCOPED", prompt)
        self.assertNotIn("WRONG_PLAN", prompt)
        self.assertNotIn("STEP2_RELEVANT_1", prompt)
        for index in range(2, 5):
            self.assertIn(f"STEP2_RELEVANT_{index}", prompt)
        self.assertNotIn("DUPLICATE_HISTORY_MUST_NOT_RENDER", prompt)
        self.assertNotIn("DUPLICATE_SUMMARY_MUST_NOT_RENDER", prompt)
        self.assertNotIn("previous_recovery", prompt)
        self.assertEqual(plan, original_plan)


class RecoveryCompleteMessageSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_r1_static_prompt_leak_is_typed_and_provider_count_stays_zero(self):
        class ProviderSpy:
            calls = 0

            async def ainvoke(self, messages, *args, **kwargs):
                self.calls += 1
                return AIMessage(content=json.dumps(_decision_payload()))

        provider = ProviderSpy()
        dispatches = 0

        def record_dispatch() -> None:
            nonlocal dispatches
            dispatches += 1

        leak = "static sha256:" + "a" * 64
        with patch(
            "agent_core.recovery_provider.PromptManager.get_recovery_v2_prompt",
            return_value=leak,
        ):
            with self.assertRaises(ControlMetadataLeakError) as caught:
                await middleware_module._invoke_recovery_v2(
                    provider,
                    data_path=None,
                    task_id="task-context-p0",
                    plan_state=_plan_state(),
                    step_id="step-2",
                    action_id="action-current",
                    attempt_id="attempt-current",
                    failure_fingerprint="failure-current",
                    _on_provider_dispatch=record_dispatch,
                )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(dispatches, 0)
        self.assertEqual(caught.exception.code, "LLM_CONTROL_METADATA_LEAK")
        self.assertEqual(
            caught.exception.stage,
            "recovery.execute.initial.provider_dispatch",
        )

    async def test_r2_guard_runs_before_provider_metrics_or_call(self):
        payload = LLMPayload({
            "semantic_history": {"task_spec": {"goal": "recover"}},
            "current_focus": {},
        })
        context = NodeContext(
            binding=ControllerBinding(
                invocation_id="recovery-safety-r2",
                node_type="recovery",
                staleness_domain="recovery",
                state_generation=1,
                task_semantic_hash="task-semantic",
                task_record_hash="task-record",
            ),
            llm_payload=payload,
        )
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent._node_invocations = InvocationRegistry()
        agent._ainvoke_llm = AsyncMock(
            return_value=AIMessage(content=json.dumps(_decision_payload()))
        )
        messages = [HumanMessage(content="follow-up sha256:" + "b" * 64)]

        with self.assertRaises(ControlMetadataLeakError):
            await agent._ainvoke_node_llm(
                messages,
                node_context=context,
                stage="recovery_judge",
                task_id="task-context-p0",
            )

        self.assertEqual(agent._ainvoke_llm.await_count, 0)
        self.assertEqual(len(agent._node_invocations._entries), 0)

    async def test_followup_evidence_leak_is_blocked_before_provider_call(self):
        payload = {
            "semantic_history": {"task_spec": {"goal": "recover"}},
            "current_focus": {},
        }
        with tempfile.TemporaryDirectory() as data_path:
            evidence = (
                Path(data_path)
                / "task_outputs"
                / "task-context-p0"
                / "attempts"
                / "attempt-current"
            )
            evidence.mkdir(parents=True)
            (evidence / "stderr.log").write_text(
                "machine digest sha256:" + "c" * 64 + "\n",
                encoding="utf-8",
            )
            response = json.dumps({
                "evidence_request": {
                    "tool": "read",
                    "path": "attempts/attempt-current/stderr.log",
                    "start_line": 1,
                    "end_line": 10,
                }
            })
            with self.assertRaises(ControlMetadataLeakError):
                build_recovery_evidence_followup(
                    payload,
                    response,
                    data_path=data_path,
                    task_id="task-context-p0",
                    stage="test.recovery.followup.provider_dispatch",
                )


class RecoveryEvidenceParityTests(unittest.IsolatedAsyncioTestCase):
    def test_shared_followup_supports_bounded_read_and_glob(self):
        payload = {
            "semantic_history": {"task_spec": {"goal": "recover"}},
            "current_focus": {},
        }
        with tempfile.TemporaryDirectory() as data_path:
            evidence = (
                Path(data_path)
                / "task_outputs"
                / "task-context-p0"
                / "attempts"
                / "attempt-current"
            )
            evidence.mkdir(parents=True)
            (evidence / "stderr.log").write_text(
                "TARGET_BOUNDED_EVIDENCE\nsecond line\n",
                encoding="utf-8",
            )
            requests = [
                {
                    "tool": "read",
                    "path": "attempts/attempt-current/stderr.log",
                    "start_line": 1,
                    "end_line": 1,
                },
                {"tool": "glob", "pattern": "*.log"},
            ]
            followups = [
                build_recovery_evidence_followup(
                    payload,
                    json.dumps({"evidence_request": request}),
                    data_path=data_path,
                    task_id="task-context-p0",
                    stage=f"test.recovery.{request['tool']}.provider_dispatch",
                )
                for request in requests
            ]

        self.assertEqual(followups[0].result["tool"], "read")
        self.assertEqual(
            followups[0].result["content"],
            "TARGET_BOUNDED_EVIDENCE",
        )
        self.assertEqual(followups[1].result["tool"], "glob")
        self.assertEqual(
            followups[1].result["matches"],
            ["attempts/attempt-current/stderr.log"],
        )
        self.assertTrue(all(len(item.messages) == 1 for item in followups))

    def test_machine_state_is_not_available_to_read_or_glob(self):
        with self.assertRaises(RecoveryEvidenceAccessError):
            recovery_evidence_root("", "task-context-p0")
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "task-context-p0"
            (root / "attempts" / "attempt-current").mkdir(parents=True)
            (root / "attempts" / "attempt-current" / "stderr.log").write_text(
                "bounded error\n", encoding="utf-8"
            )
            (root / "plan_state.json").write_text("{}\n", encoding="utf-8")
            (root / "context_trace").mkdir()
            (root / "context_trace" / "request.json").write_text(
                "{}\n", encoding="utf-8"
            )
            reader = RecoveryEvidenceReader(root)

            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("plan_state.json")
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("context_trace/request.json")
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("../outside.log")
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("/etc/passwd")
            globbed = reader.glob("*.json")
            self.assertNotIn("plan_state.json", globbed["matches"])
            self.assertNotIn("context_trace/request.json", globbed["matches"])
            self.assertEqual(
                reader.glob("*.log")["matches"],
                ["attempts/attempt-current/stderr.log"],
            )

    async def test_r1_and_r2_both_execute_one_bounded_read_followup(self):
        r1_messages: list[str] = []

        class R1Provider:
            async def ainvoke(self, messages, *args, **kwargs):
                r1_messages.append(str(messages[0].content))
                if len(r1_messages) == 1:
                    return AIMessage(content=json.dumps({
                        "evidence_request": {
                            "tool": "read",
                            "path": "attempts/attempt-current/stderr.log",
                            "start_line": 1,
                            "end_line": 2,
                        }
                    }))
                return AIMessage(content=json.dumps(_decision_payload()))

        with tempfile.TemporaryDirectory() as data_path:
            evidence_root = (
                Path(data_path)
                / "task_outputs"
                / "task-context-p0"
                / "attempts"
                / "attempt-current"
            )
            evidence_root.mkdir(parents=True)
            (evidence_root / "stderr.log").write_text(
                "TARGET_BOUNDED_EVIDENCE\nsecond line\nthird line\n",
                encoding="utf-8",
            )
            plan = _plan_state()
            with (
                patch.object(
                    middleware_module,
                    "load_plan_state",
                    return_value=plan,
                ),
                patch.object(
                    middleware_module,
                    "load_approved_plan_snapshot",
                    return_value=_approved_plan(),
                ),
            ):
                await middleware_module._invoke_recovery_v2(
                    R1Provider(),
                    data_path=data_path,
                    task_id="task-context-p0",
                    plan_state=plan,
                    step_id="step-2",
                    action_id="action-current",
                    attempt_id="attempt-current",
                    failure_fingerprint="failure-current",
                )

            r2_calls: list[str] = []

            async def r2_provider(messages, **kwargs):
                r2_calls.append(str(messages[0].content))
                if len(r2_calls) == 1:
                    return AIMessage(content=json.dumps({
                        "evidence_request": {
                            "tool": "read",
                            "path": "attempts/attempt-current/stderr.log",
                            "start_line": 1,
                            "end_line": 2,
                        }
                    }))
                return AIMessage(content=json.dumps(_decision_payload()))

            recovery_context = RecoveryContext(
                context_id="recovery-context-p0",
                task_id="task-context-p0",
                step_id="step-2",
                action_id="action-current",
                attempt_id="attempt-current",
                error_fingerprint="failure-current",
                current_step={"step_id": "step-2", "success_criteria": []},
                execution={"returncode": 1, "command": "annotate input.vcf"},
                diagnosis={
                    "failure_owner": "GENERATED_ACTION",
                    "failure_type": "script_bug",
                    "evidence": ["bounded failure"],
                    "evidence_sufficient": True,
                    "collection_complete": False,
                },
            )
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = data_path
            agent.skill_manager = None
            agent.recovery_llm = object()
            agent.recovery_llm_policy = {}
            agent._node_context_semantic_inputs = lambda *args, **kwargs: {}
            agent._ainvoke_node_llm = AsyncMock(side_effect=r2_provider)

            state = {
                "task_id": "task-context-p0",
                "current_step_id": "step-2",
                "execution_evidence": {
                    "execution_id": "execution-current",
                    "attempt_id": "attempt-current",
                    "action_id": "action-current",
                    "step_id": "step-2",
                    "returncode": 1,
                    "execution_status": "failed",
                },
            }
            with (
                patch.object(bio_agents, "load_plan_state", return_value=plan),
                patch.object(bio_agents, "save_plan_state"),
                patch.object(
                    bio_agents,
                    "load_approved_plan_snapshot",
                    return_value=_approved_plan(),
                ),
                patch.object(
                    bio_agents,
                    "canonical_task_spec_for_state",
                    return_value=_task_spec(),
                ),
                patch.object(
                    bio_agents,
                    "evaluate_recovery_trigger",
                    return_value=RecoveryTrigger(
                        required=True,
                        reasons=["structured_execution_failure"],
                        judge_required=True,
                    ),
                ),
                patch.object(
                    bio_agents,
                    "build_recovery_context",
                    return_value=recovery_context,
                ),
                patch.object(bio_agents, "diagnosis_required", return_value=True),
            ):
                result = await agent._adjudicate_post_review_recovery(
                    state,
                    {},
                    "task-context-p0",
                )

        self.assertEqual(len(r1_messages), 2)
        self.assertEqual(len(r2_calls), 2)
        self.assertIn("Evidence protocol:", r1_messages[0])
        self.assertIn("Evidence protocol:", r2_calls[0])
        self.assertIn("TARGET_BOUNDED_EVIDENCE", r1_messages[1])
        self.assertIn("TARGET_BOUNDED_EVIDENCE", r2_calls[1])
        self.assertEqual(
            result["recovery_decision"]["recommended_action"],
            RecoveryAction.PATCH_SCRIPT.value,
        )

    async def test_r1_rejects_a_second_evidence_request_without_third_call(self):
        class RepeatingProvider:
            def __init__(self) -> None:
                self.calls = 0

            async def ainvoke(self, messages, *args, **kwargs):
                self.calls += 1
                return AIMessage(content=json.dumps({
                    "evidence_request": {
                        "tool": "read",
                        "path": "attempts/attempt-current/stderr.log",
                        "start_line": 1,
                        "end_line": 1,
                    }
                }))

        provider = RepeatingProvider()
        with tempfile.TemporaryDirectory() as data_path:
            evidence = (
                Path(data_path)
                / "task_outputs"
                / "task-context-p0"
                / "attempts"
                / "attempt-current"
            )
            evidence.mkdir(parents=True)
            (evidence / "stderr.log").write_text("bounded\n", encoding="utf-8")
            plan = _plan_state()
            with (
                patch.object(
                    middleware_module,
                    "load_plan_state",
                    return_value=plan,
                ),
                patch.object(
                    middleware_module,
                    "load_approved_plan_snapshot",
                    return_value=_approved_plan(),
                ),
            ):
                with self.assertRaises(RecoveryEvidenceAccessError):
                    await middleware_module._invoke_recovery_v2(
                        provider,
                        data_path=data_path,
                        task_id="task-context-p0",
                        plan_state=plan,
                        step_id="step-2",
                        action_id="action-current",
                        attempt_id="attempt-current",
                        failure_fingerprint="failure-current",
                    )

        self.assertEqual(provider.calls, 2)

    async def test_r2_rejects_a_second_evidence_request_without_third_call(self):
        with tempfile.TemporaryDirectory() as data_path:
            evidence = (
                Path(data_path)
                / "task_outputs"
                / "task-context-p0"
                / "attempts"
                / "attempt-current"
            )
            evidence.mkdir(parents=True)
            (evidence / "stderr.log").write_text(
                "bounded\n", encoding="utf-8"
            )
            repeated_request = AIMessage(content=json.dumps({
                "evidence_request": {
                    "tool": "read",
                    "path": "attempts/attempt-current/stderr.log",
                    "start_line": 1,
                    "end_line": 1,
                }
            }))
            plan = _plan_state()
            recovery_context = RecoveryContext(
                context_id="recovery-context-repeat-r2",
                task_id="task-context-p0",
                step_id="step-2",
                action_id="action-current",
                attempt_id="attempt-current",
                error_fingerprint="failure-current",
                current_step={"step_id": "step-2", "success_criteria": []},
                execution={"returncode": 1},
                diagnosis={
                    "failure_owner": "GENERATED_ACTION",
                    "failure_type": "script_bug",
                    "evidence": ["bounded failure"],
                    "evidence_sufficient": True,
                    "collection_complete": False,
                },
            )
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = data_path
            agent.skill_manager = None
            agent.recovery_llm = object()
            agent.recovery_llm_policy = {}
            agent._node_context_semantic_inputs = lambda *args, **kwargs: {}
            agent._ainvoke_node_llm = AsyncMock(return_value=repeated_request)
            state = {
                "task_id": "task-context-p0",
                "current_step_id": "step-2",
                "execution_evidence": {
                    "execution_id": "execution-current",
                    "attempt_id": "attempt-current",
                    "action_id": "action-current",
                    "step_id": "step-2",
                    "returncode": 1,
                    "execution_status": "failed",
                },
            }
            with (
                patch.object(bio_agents, "load_plan_state", return_value=plan),
                patch.object(bio_agents, "save_plan_state"),
                patch.object(
                    bio_agents,
                    "load_approved_plan_snapshot",
                    return_value=_approved_plan(),
                ),
                patch.object(
                    bio_agents,
                    "canonical_task_spec_for_state",
                    return_value=_task_spec(),
                ),
                patch.object(
                    bio_agents,
                    "evaluate_recovery_trigger",
                    return_value=RecoveryTrigger(
                        required=True,
                        reasons=["structured_execution_failure"],
                        judge_required=True,
                    ),
                ),
                patch.object(
                    bio_agents,
                    "build_recovery_context",
                    return_value=recovery_context,
                ),
                patch.object(bio_agents, "diagnosis_required", return_value=True),
            ):
                result = await agent._adjudicate_post_review_recovery(
                    state,
                    {},
                    "task-context-p0",
                )

        self.assertEqual(agent._ainvoke_node_llm.await_count, 2)
        self.assertEqual(
            result["recovery_decision"]["failure_type"],
            "recovery_judge_unavailable",
        )


class RecoveryDeltaProviderCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonempty_r1_r2_capture_uses_shared_guarded_assembly(self):
        r1_messages: list[str] = []
        r2_messages: list[str] = []
        guard_patcher = patch(
            "agent_core.recovery_provider.assert_v2_llm_messages_clean",
            wraps=assert_v2_llm_messages_clean,
        )
        guard_spy = guard_patcher.start()
        self.addCleanup(guard_patcher.stop)

        class R1Provider:
            async def ainvoke(self, messages, *args, **kwargs):
                r1_messages.append(str(messages[0].content))
                return AIMessage(content=json.dumps(_decision_payload()))

        async def r2_provider(messages, **kwargs):
            r2_messages.append(str(messages[0].content))
            return AIMessage(content=json.dumps(_decision_payload()))

        with tempfile.TemporaryDirectory() as data_path:
            plan = _plan_state()
            task_spec = _task_spec()
            task_spec.update({
                "inputs": [{
                    "role": "variant_vcf",
                    "path": "/analysis/inputs/sample.vcf",
                }],
                "outputs": [{
                    "role": "annotated_vcf",
                    "path": "/analysis/results/annotated.vcf",
                }],
            })
            plan["task_spec"] = deepcopy(task_spec)
            plan["current_task"] = {"task_spec": deepcopy(task_spec)}
            plan["selected_skill_ids"] = ["recovery-delta-skill"]
            plan["task_state"]["accepted_consultant_handoff"] = {
                "summary": "CONSULTANT_HANDOFF_DELTA",
                "selected_skills": ["recovery-delta-skill"],
                "requirements": ["preserve the annotated VCF path"],
            }
            plan["task_semantic_hash"] = "sha256:" + "d" * 64
            plan["plan_record_hash"] = "sha256:" + "e" * 64

            contract_store = StateContractStore(data_path, "task-context-p0")
            conversation_index, _ = contract_store.append_conversation_turn(
                {},
                role="user",
                content="USER_TURN_ONE_DELTA",
            )
            conversation_index, _ = contract_store.append_conversation_turn(
                conversation_index,
                role="user",
                content="USER_TURN_TWO_DELTA",
            )
            plan["conversation_evidence"] = conversation_index
            conversation_turns = contract_store.read_conversation_evidence(
                conversation_index
            )

            recovery_store = RecoveryStateStore(data_path, "task-context-p0")
            recovery_store.save_attempt_mapping(
                _history_row("SAME_STEP_DELTA")
            )
            unrelated = _history_row(
                "UNRELATED_STEP_DELTA_" + "sha256:" + "f" * 64,
                step_id="step-1",
            )
            recovery_store.save_attempt_mapping(unrelated)
            recovery_rows = recovery_store.load_attempts()

            skill_context = {
                "selected_skill_refs": [{
                    "skill_id": "recovery-delta-skill",
                    "scope": "task",
                    "status": "active",
                }],
                "available_skill_headings": [{
                    "skill_id": "recovery-delta-skill",
                    "heading_index": [
                        {"heading": "Troubleshooting", "level": 2}
                    ],
                }],
                "full_skill_documents": [{
                    "skill_id": "recovery-delta-skill",
                    "document": "FULL_SKILL_BODY_DELTA",
                }],
            }
            recovery_focus = {
                "current_step": {
                    "title": "annotate variants",
                    "dependencies": ["step-1"],
                },
                "previous_action": {
                    "action_type": "execute",
                    "command": "snpeff -i input.vcf -o annotated.vcf",
                    "concrete_artifact_bindings": [
                        {
                            "semantic_handle": "input.variant_vcf",
                            "direction": "input",
                            "path": "/analysis/inputs/sample.vcf",
                        },
                        {
                            "semantic_handle": "output.annotated_vcf",
                            "direction": "output",
                            "expected_path": (
                                "/analysis/results/annotated.vcf"
                            ),
                        },
                    ],
                },
                "failed_command": "snpeff -i input.vcf -o annotated.vcf",
                "exit_code": 2,
                "bounded_stdout": "STDOUT_DELTA",
                "bounded_stderr": "STDERR_DELTA invalid option",
                "artifact_paths": [
                    {"path": "/analysis/inputs/sample.vcf"},
                    {"expected_path": "/analysis/results/annotated.vcf"},
                ],
                "previous_recovery_summary": [
                    {"root_cause": "DUPLICATE_DELTA_HISTORY"}
                ],
            }
            approved = _approved_plan()
            approved["steps"] = deepcopy(plan["steps"])

            with (
                patch.object(
                    middleware_module, "load_plan_state", return_value=plan
                ),
                patch.object(
                    middleware_module,
                    "load_approved_plan_snapshot",
                    return_value=approved,
                ),
                patch.object(
                    middleware_module,
                    "project_skill_context",
                    return_value=skill_context,
                ),
            ):
                await middleware_module._invoke_recovery_v2(
                    R1Provider(),
                    data_path=data_path,
                    task_id="task-context-p0",
                    plan_state=plan,
                    step_id="step-2",
                    action_id="action-current",
                    attempt_id="attempt-current",
                    failure_fingerprint="failure-current",
                    runtime={"recovery_focus": recovery_focus},
                )

            recovery_context = RecoveryContext(
                context_id="recovery-context-delta",
                task_id="task-context-p0",
                step_id="step-2",
                action_id="action-current",
                attempt_id="attempt-current",
                error_fingerprint="failure-current",
                current_step=deepcopy(recovery_focus["current_step"]),
                action_contract=deepcopy(recovery_focus["previous_action"]),
                execution={
                    "returncode": 2,
                    "failed_command": recovery_focus["failed_command"],
                    "stdout_tail": recovery_focus["bounded_stdout"],
                    "stderr_tail": recovery_focus["bounded_stderr"],
                },
                recent_attempts=recovery_rows,
                diagnosis={
                    "failure_owner": "GENERATED_ACTION",
                    "failure_type": "script_bug",
                    "evidence": ["STDERR_DELTA invalid option"],
                    "evidence_sufficient": True,
                    "collection_complete": False,
                },
            )
            semantic_inputs = {
                "conversation_turns": conversation_turns,
                "consultant_handoff": deepcopy(
                    plan["task_state"]["accepted_consultant_handoff"]
                ),
                "execution_records": [],
                "recovery_decisions": recovery_rows,
            }
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = data_path
            agent.skill_manager = None
            agent.recovery_llm = object()
            agent.recovery_llm_policy = {}
            agent._node_context_semantic_inputs = (
                lambda *args, **kwargs: deepcopy(semantic_inputs)
            )
            agent._skill_context_projection = (
                lambda *args, **kwargs: deepcopy(skill_context)
            )
            agent._ainvoke_node_llm = AsyncMock(side_effect=r2_provider)
            state = {
                "task_id": "task-context-p0",
                "current_step_id": "step-2",
                "execution_evidence": {
                    "execution_id": "execution-current",
                    "attempt_id": "attempt-current",
                    "action_id": "action-current",
                    "step_id": "step-2",
                    "returncode": 2,
                    "execution_status": "failed",
                },
            }
            with (
                patch.object(bio_agents, "load_plan_state", return_value=plan),
                patch.object(bio_agents, "save_plan_state"),
                patch.object(
                    bio_agents,
                    "load_approved_plan_snapshot",
                    return_value=approved,
                ),
                patch.object(
                    bio_agents,
                    "canonical_task_spec_for_state",
                    return_value=task_spec,
                ),
                patch.object(
                    bio_agents,
                    "evaluate_recovery_trigger",
                    return_value=RecoveryTrigger(
                        required=True,
                        reasons=["structured_execution_failure"],
                        judge_required=True,
                    ),
                ),
                patch.object(
                    bio_agents,
                    "build_recovery_context",
                    return_value=recovery_context,
                ),
                patch.object(bio_agents, "diagnosis_required", return_value=True),
            ):
                await agent._adjudicate_post_review_recovery(
                    state,
                    {},
                    "task-context-p0",
                )

        self.assertEqual(len(r1_messages), 1)
        self.assertEqual(len(r2_messages), 1)
        for prompt in (r1_messages[0], r2_messages[0]):
            self.assertIn("USER_TURN_ONE_DELTA", prompt)
            self.assertIn("USER_TURN_TWO_DELTA", prompt)
            self.assertIn("CONSULTANT_HANDOFF_DELTA", prompt)
            self.assertIn("recovery-delta-skill", prompt)
            self.assertIn("Troubleshooting", prompt)
            self.assertIn("annotate variants", prompt)
            self.assertIn("STDOUT_DELTA", prompt)
            self.assertIn("STDERR_DELTA", prompt)
            self.assertIn("/analysis/inputs/sample.vcf", prompt)
            self.assertIn("/analysis/results/annotated.vcf", prompt)
            self.assertIn("SAME_STEP_DELTA", prompt)
            self.assertIn("Evidence protocol:", prompt)
            self.assertNotIn("UNRELATED_STEP_DELTA", prompt)
            self.assertNotIn("DUPLICATE_DELTA_HISTORY", prompt)
            self.assertNotIn("task_semantic_hash", prompt)
            self.assertNotIn("plan_record_hash", prompt)
            self.assertNotIn("sha256:" + "d" * 64, prompt)
            self.assertNotIn("sha256:" + "e" * 64, prompt)
            self.assertNotIn("sha256:" + "f" * 64, prompt)
            self.assertNotIn("FULL_SKILL_BODY_DELTA", prompt)

        self.assertEqual(guard_spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()

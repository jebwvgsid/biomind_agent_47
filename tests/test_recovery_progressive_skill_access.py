from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

import agent_core.middleware as middleware_module
import bio_agents
from bio_agents import BioAnalystAgent
from agent_core.diagnostics import RecoveryContext
from agent_core.recovery import RecoveryAction
from agent_core.recovery import RecoveryTrigger
from agent_core.skill_access import SkillAccessService
from tests.test_attempt_recovery_diagnosis_contract import (
    _recovery_identity_state,
    _recovery_plan_snapshot,
)


def _recovery_decision() -> dict:
    return {
        "schema_version": 4,
        "verdict": "repair",
        "failure_class": "SCRIPT_BUG",
        "failure_owner": "GENERATED_ACTION",
        "failure_type": "runtime_invocation",
        "error_is_real": True,
        "primary_execution_succeeded": False,
        "evidence_sufficient": True,
        "confidence": 0.9,
        "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
        "rerun_required": False,
        "consume_retry_budget": False,
        "user_notification_required": False,
        "reason": "the invocation needs a bounded repair",
        "root_cause": "the invocation omitted a required preparation step",
        "evidence": ["stderr-bounded"],
        "evidence_refs": ["stderr-bounded"],
        "machine_findings_considered": [],
        "missing_evidence": [],
        "repair_strategy": "repair only the invocation preparation",
        "repair_target": "runtime invocation",
        "repair_instruction": "apply the exact bounded guidance",
        "next_action": RecoveryAction.PATCH_SCRIPT.value,
        "requires_user_input": False,
    }


class _FakeSkillManager:
    def __init__(self, root: Path) -> None:
        self.skills_root = root.resolve()
        self.catalog_complete = True
        self.catalog_errors = []
        skill_dir = self.skills_root / "debug-skill"
        self._meta = {
            "_skill_id": "debug-skill",
            "_skill_md": str((skill_dir / "SKILL.md").resolve()),
            "_skill_dir": str(skill_dir.resolve()),
            "name": "Debug Skill",
            "description": "Bounded runtime diagnosis guidance",
            "version": "1",
        }
        self.discovered_skills = [dict(self._meta)]

    def get_skill_meta(self, skill_id: str) -> dict:
        return dict(self._meta) if skill_id == "debug-skill" else {}

    def catalog_status(self) -> dict:
        return {
            "catalog_revision": "catalog-test-1",
            "complete": True,
            "errors": [],
            "skill_count": 1,
        }


class RecoveryProgressiveSkillAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_turn_change_discards_recovery_without_retry_or_evidence_loss(
        self,
    ) -> None:
        class _RecoveryLLM:
            async def ainvoke(self, messages, *args, **kwargs):
                return AIMessage(content=json.dumps(_recovery_decision()))

        initial = _recovery_identity_state()
        initial["conversation_evidence"] = {
            "turn_count": 4,
            "user_turn_count": 2,
        }
        initial["task_state"]["budgets"] = {
            "execution_retry_count": 3,
            "failure_count": 5,
        }
        initial["pending_recovery_action"] = {
            "step_id": "step-b",
            "action_id": "action-a",
            "attempt_id": "attempt-a",
            "type": "patch_script",
        }
        initial["pending_action"] = {
            "step_id": "step-b",
            "action_id": "action-a",
            "attempt_id": "attempt-a",
            "type": "execute",
        }
        latest = deepcopy(initial)
        latest["conversation_evidence"] = {
            "turn_count": 5,
            "user_turn_count": 3,
        }
        expected_attempts = deepcopy(latest["provenance_state"]["attempts"])
        expected_budgets = deepcopy(latest["task_state"]["budgets"])
        holder = {"state": latest}

        def _load(*args, **kwargs):
            return deepcopy(holder["state"])

        def _save(_data_path, _task_id, value):
            holder["state"] = deepcopy(value)
            return deepcopy(value)

        with tempfile.TemporaryDirectory() as temp_root:
            with (
                patch.object(middleware_module, "load_plan_state", side_effect=_load),
                patch.object(middleware_module, "save_plan_state", side_effect=_save),
                patch.object(
                    middleware_module,
                    "load_approved_plan_snapshot",
                    side_effect=[
                        _recovery_plan_snapshot(),
                        _recovery_plan_snapshot(),
                    ],
                ),
            ):
                _message, snapshot = await middleware_module._invoke_recovery_v2(
                    _RecoveryLLM(),
                    data_path=temp_root,
                    task_id="task-b",
                    plan_state=initial,
                    step_id="step-b",
                    action_id="action-a",
                    attempt_id="attempt-a",
                    failure_fingerprint="diagnostic-a",
                )

        stale = snapshot["stale_node_result"]
        self.assertEqual(stale["decision_result"], "STALE_NODE_RESULT")
        self.assertIn(
            "authoritative_user_turn_revision",
            stale["changed_preconditions"],
        )
        self.assertEqual(stale["request_user_turn_revision"], 2)
        self.assertEqual(stale["current_user_turn_revision"], 3)
        self.assertFalse(stale["counts_as_failure"])
        self.assertFalse(stale["consumes_execution_retry"])
        self.assertFalse(stale["consumes_recovery_retry"])
        self.assertEqual(holder["state"]["task_state"]["budgets"], expected_budgets)
        self.assertEqual(
            holder["state"]["provenance_state"]["attempts"],
            expected_attempts,
        )
        self.assertEqual(holder["state"]["pending_recovery_action"], {})
        self.assertEqual(holder["state"]["pending_action"], {})
        self.assertEqual(
            holder["state"]["task_state"]["skill_access_state"]["events"][-1][
                "event"
            ],
            "stale_node_result_discarded",
        )

    async def test_recovery_user_revision_prefers_user_count_and_falls_back_to_turn_count(
        self,
    ) -> None:
        class _RecoveryLLM:
            async def ainvoke(self, messages, *args, **kwargs):
                return AIMessage(content=json.dumps(_recovery_decision()))

        cases = [
            (
                "assistant-only-turn-does-not-stale",
                {"turn_count": 4, "user_turn_count": 2},
                {"turn_count": 5, "user_turn_count": 2},
                False,
            ),
            (
                "legacy-turn-count-fallback-stales",
                {"turn_count": 4},
                {"turn_count": 5},
                True,
            ),
        ]
        for name, request_index, current_index, should_stale in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_root:
                initial = _recovery_identity_state()
                initial["conversation_evidence"] = request_index
                latest = deepcopy(initial)
                latest["conversation_evidence"] = current_index
                holder = {"state": latest}

                def _load(*args, **kwargs):
                    return deepcopy(holder["state"])

                def _save(_data_path, _task_id, value):
                    holder["state"] = deepcopy(value)
                    return deepcopy(value)

                with (
                    patch.object(
                        middleware_module,
                        "load_plan_state",
                        side_effect=_load,
                    ),
                    patch.object(
                        middleware_module,
                        "save_plan_state",
                        side_effect=_save,
                    ),
                    patch.object(
                        middleware_module,
                        "load_approved_plan_snapshot",
                        side_effect=[
                            _recovery_plan_snapshot(),
                            _recovery_plan_snapshot(),
                        ],
                    ),
                ):
                    message, snapshot = await middleware_module._invoke_recovery_v2(
                        _RecoveryLLM(),
                        data_path=temp_root,
                        task_id="task-b",
                        plan_state=initial,
                        step_id="step-b",
                        action_id="action-a",
                        attempt_id="attempt-a",
                        failure_fingerprint="diagnostic-a",
                    )

            if should_stale:
                self.assertEqual(snapshot["decision_result"], "STALE_NODE_RESULT")
                self.assertIn(
                    "authoritative_user_turn_revision",
                    snapshot["stale_node_result"]["changed_preconditions"],
                )
            else:
                self.assertNotIn("stale_node_result", snapshot)
                self.assertEqual(
                    json.loads(message.content)["recommended_action"],
                    RecoveryAction.PATCH_SCRIPT.value,
                )

    async def test_context_request_reinvokes_same_recovery_runner(self) -> None:
        calls: list[str] = []

        class _RecoveryLLM:
            async def ainvoke(self, messages, *args, **kwargs):
                calls.append(str(messages[0].content))
                if len(calls) == 1:
                    return AIMessage(content=json.dumps({
                        "decision": "REQUEST_SKILL_CONTEXT",
                        "reason": "Need the bounded troubleshooting section",
                        "requests": [{
                            "skill_id": "debug-skill",
                            "operation": "section",
                            "heading": "Troubleshooting",
                        }],
                    }))
                return AIMessage(content=json.dumps(_recovery_decision()))

        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "skills"
            skill_dir = root / "debug-skill"
            skill_dir.mkdir(parents=True)
            exact_skill_text = (
                "# Debug Skill\n\n"
                "## Troubleshooting\n"
                "RECOVERY_EXACT_GUIDANCE: create the output parent first.\n"
            )
            (skill_dir / "SKILL.md").write_text(
                exact_skill_text,
                encoding="utf-8",
            )
            manager = _FakeSkillManager(root)
            holder = {"state": _recovery_identity_state()}
            approved = _recovery_plan_snapshot()

            def _load(*args, **kwargs):
                return deepcopy(holder["state"])

            def _save(_data_path, _task_id, value):
                holder["state"] = deepcopy(value)
                return deepcopy(value)

            with (
                patch.object(middleware_module, "load_plan_state", side_effect=_load),
                patch.object(middleware_module, "save_plan_state", side_effect=_save),
                patch.object(
                    middleware_module,
                    "load_approved_plan_snapshot",
                    return_value=approved,
                ),
            ):
                message, snapshot = await middleware_module._invoke_recovery_v2(
                    _RecoveryLLM(),
                    data_path=temp_root,
                    task_id="task-b",
                    plan_state=deepcopy(holder["state"]),
                    step_id="step-b",
                    action_id="action-a",
                    attempt_id="attempt-a",
                    failure_fingerprint="diagnostic-a",
                    skill_manager=manager,
                    selected_skill_ids=["debug-skill"],
                )

        self.assertEqual(len(calls), 2)
        self.assertIn("RECOVERY_EXACT_GUIDANCE", calls[1])
        self.assertEqual(
            json.loads(message.content)["recommended_action"],
            RecoveryAction.PATCH_SCRIPT.value,
        )
        self.assertEqual(len(snapshot.get("skill_access_history") or []), 1)
        access = holder["state"]["task_state"]["skill_access_state"]
        self.assertEqual(access["selected_skill_refs"][0]["status"], "active")
        stored_snapshot = next(iter(access["snapshots"].values()))
        self.assertEqual(stored_snapshot["exact_text"], exact_skill_text)

    async def test_post_review_context_request_stays_in_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "skills"
            skill_dir = root / "debug-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "# Debug Skill\n\n## Troubleshooting\nPOST_REVIEW_EXACT_GUIDANCE\n",
                encoding="utf-8",
            )
            manager = _FakeSkillManager(root)
            initial = _recovery_identity_state()
            initial["plan_revision"] = 1
            holder = {"state": initial}

            def _load(*args, **kwargs):
                return deepcopy(holder["state"])

            def _save(_data_path, _task_id, value):
                holder["state"] = deepcopy(value)
                return deepcopy(value)

            recovery_context = RecoveryContext(
                context_id="recovery-context-b",
                task_id="task-b",
                step_id="step-b",
                action_id="action-a",
                attempt_id="attempt-a",
                error_fingerprint="diagnostic-a",
                current_step={"step_id": "step-b", "success_criteria": []},
                execution={"returncode": 1, "code": "run-action"},
                diagnosis={
                    "failure_owner": "GENERATED_ACTION",
                    "failure_type": "runtime_invocation",
                    "evidence": ["bounded execution failure"],
                    "evidence_sufficient": True,
                    "collection_complete": False,
                },
            )
            request = AIMessage(content=json.dumps({
                "decision": "REQUEST_SKILL_CONTEXT",
                "reason": "Need exact post-review guidance",
                "requests": [{
                    "skill_id": "debug-skill",
                    "operation": "section",
                    "heading": "Troubleshooting",
                }],
            }))
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = temp_root
            agent.skill_manager = manager
            agent.skill_access_service = SkillAccessService(manager)
            agent.recovery_llm = object()
            agent.recovery_llm_policy = {}
            agent._node_context_semantic_inputs = lambda *args, **kwargs: {}
            agent._ainvoke_node_llm = AsyncMock(side_effect=[
                request,
                AIMessage(content=json.dumps(_recovery_decision())),
            ])

            state = {
                "task_id": "task-b",
                "current_step_id": "step-b",
                "active_skills": ["debug-skill"],
                "execution_evidence": {
                    "execution_id": "exec-b",
                    "attempt_id": "attempt-a",
                    "action_id": "action-a",
                    "step_id": "step-b",
                    "returncode": 1,
                    "execution_status": "failed",
                },
            }
            with (
                patch.object(bio_agents, "load_plan_state", side_effect=_load),
                patch.object(bio_agents, "save_plan_state", side_effect=_save),
                patch.object(
                    bio_agents,
                    "load_approved_plan_snapshot",
                    return_value=_recovery_plan_snapshot(),
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
                    "task-b",
                )

        self.assertEqual(agent._ainvoke_node_llm.await_count, 2)
        second_context = agent._ainvoke_node_llm.await_args_list[1].kwargs[
            "node_context"
        ]
        self.assertIn(
            "POST_REVIEW_EXACT_GUIDANCE",
            json.dumps(
                second_context.llm_payload.to_provider_dict(),
                ensure_ascii=False,
            ),
        )
        self.assertEqual(
            result["recovery_decision"]["recommended_action"],
            RecoveryAction.PATCH_SCRIPT.value,
        )
        self.assertNotIn(
            "skill_continuation_id",
            holder["state"]["task_state"],
        )
        continuations = holder["state"]["task_state"][
            "skill_access_state"
        ]["continuations"]
        self.assertEqual(
            next(iter(continuations.values()))["status"],
            "closed",
        )


if __name__ == "__main__":
    unittest.main()

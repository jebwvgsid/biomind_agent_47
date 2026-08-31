import sys
import types
import unittest
from unittest.mock import patch

from langchain_core.messages import SystemMessage

try:
    import langgraph.types  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    langgraph_module = types.ModuleType("langgraph")
    langgraph_types_module = types.ModuleType("langgraph.types")
    langgraph_types_module.interrupt = lambda payload: payload
    sys.modules.setdefault("langgraph", langgraph_module)
    sys.modules["langgraph.types"] = langgraph_types_module

import agent_core.middleware as middleware_module
from agent_core.middleware import error_recovery_middleware


class RecoveryMiddlewareEventScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_early_return_does_not_replay_prior_execution_evidence(self):
        async def execute_without_starting(_state):
            return {
                "messages": [SystemMessage(content="Execute stopped before command start.")],
                "required_next_action": "PLAN_PATCH",
                "next_step": "planner",
            }

        wrapped = error_recovery_middleware(object())(execute_without_starting)
        stale_state = {
            "task_id": "task-event-scope",
            "current_step_id": "step-2",
            "execution_evidence": {
                "execution_id": "attempt-old",
                "attempt_id": "attempt-old",
                "step_id": "step-2",
                "command_role": "primary",
                "returncode": 1,
                "execution_status": "failed",
            },
            "progress_guard_result": {
                "source_node": "execute",
                "step_id": "step-2",
                "attempt_id": "attempt-old",
                "trigger_event_id": "attempt-old",
                "classification": "exhausted",
                "should_block": True,
            },
        }

        with (
            patch.object(middleware_module, "build_progress_evidence") as build_progress,
            patch.object(middleware_module, "evaluate_recovery_trigger") as evaluate_trigger,
        ):
            result = await wrapped(stale_state)

        build_progress.assert_not_called()
        evaluate_trigger.assert_not_called()
        self.assertEqual(result["next_step"], "planner")
        self.assertNotIn("recovery_decision", result)
        self.assertEqual(result["proposed_route"], "planner")
        self.assertEqual(result["recovery_trigger"]["required"], False)
        self.assertEqual(
            result["recovery_trigger"]["reasons"],
            ["current_execution_not_started"],
        )
        guard = result["progress_guard_result"]
        self.assertEqual(guard["source_node"], "execute")
        self.assertEqual(guard["step_id"], "step-2")
        self.assertEqual(guard["attempt_id"], "")
        self.assertEqual(guard["trigger_event_id"], "")
        self.assertEqual(guard["classification"], "not_evaluated")
        self.assertFalse(guard["should_block"])
        self.assertEqual(guard["reason_code"], "CURRENT_EXECUTION_NOT_STARTED")


if __name__ == "__main__":
    unittest.main()
